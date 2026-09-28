"""Revision checked catalog editing and content-addressed media management."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..models import StorageError
from .catalog import _atomic_content_write, _inspect_image
from .storage import SQLiteStorage


_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_GENDERS = {"female", "male", "unspecified"}
_MODES = {"wife", "husband"}
_MAX_NAME = 120
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_MAX_PIXELS = 40_000_000


class CatalogAdminService:
    def __init__(self, storage: SQLiteStorage, media_root: Path) -> None:
        self.storage = storage
        self.media_root = media_root

    def list_characters(self, *, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, min(int(offset), 1_000_000))
        term = query.strip()[:100]
        with self.storage._lock:
            db = self.storage._connection()
            where = "c.deleted_at IS NULL"
            args: list[Any] = []
            if term:
                where += " AND (c.id LIKE ? OR c.name LIKE ? OR c.aliases_json LIKE ?)"
                args.extend([f"%{term}%"] * 3)
            total = int(db.execute(f"SELECT COUNT(*) FROM characters c WHERE {where}", args).fetchone()[0])
            rows = db.execute(
                f"""SELECT c.id,c.name,c.aliases_json,c.gender,c.enabled,c.revision,c.provenance_json,
                           (SELECT COUNT(*) FROM character_images ci WHERE ci.character_id=c.id) AS image_count
                    FROM characters c WHERE {where} ORDER BY c.name,c.id LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()
        return {"items": [self._character_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def get_character(self, character_id: str) -> dict[str, Any] | None:
        with self.storage._lock:
            db = self.storage._connection()
            row = db.execute("SELECT * FROM characters WHERE id=? AND deleted_at IS NULL", (character_id,)).fetchone()
            if row is None:
                return None
            images = db.execute(
                "SELECT ci.media_hash,ci.ordinal,mb.mime_type,mb.width,mb.height,mb.byte_size FROM character_images ci JOIN media_blobs mb ON mb.hash=ci.media_hash WHERE ci.character_id=? ORDER BY ci.ordinal,ci.media_hash",
                (character_id,),
            ).fetchall()
            pools = db.execute(
                "SELECT p.id,p.name,p.mode FROM pools p JOIN pool_members pm ON pm.pool_id=p.id WHERE pm.character_id=? AND p.deleted_at IS NULL ORDER BY p.mode,p.name",
                (character_id,),
            ).fetchall()
            snapshots = int(db.execute(
                "SELECT COUNT(*) FROM relationships r WHERE r.subject_kind='character' AND r.subject_id=?", (character_id,)
            ).fetchone()[0])
        result = self._character_dict(row)
        result.update({"images": [dict(item) for item in images], "pools": [dict(item) for item in pools], "snapshot_references": snapshots})
        return result

    def list_pools(self) -> list[dict[str, Any]]:
        with self.storage._lock:
            rows = self.storage._connection().execute(
                """SELECT p.id,p.name,p.mode,p.builtin,p.revision,COUNT(c.id) AS character_count,
                          COALESCE(GROUP_CONCAT(c.id, char(31)), '') AS character_ids
                   FROM pools p LEFT JOIN pool_members pm ON pm.pool_id=p.id
                   LEFT JOIN characters c ON c.id=pm.character_id AND c.deleted_at IS NULL
                   WHERE p.deleted_at IS NULL GROUP BY p.id ORDER BY p.mode,p.name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def save_character(self, payload: Mapping[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        character_id = self._identifier(payload.get("id"), "角色 ID")
        name = self._text(payload.get("name"), "角色名称", _MAX_NAME)
        gender = payload.get("gender")
        enabled = payload.get("enabled")
        aliases = payload.get("aliases", [])
        pool_ids = payload.get("pool_ids", [])
        image_hashes = payload.get("image_hashes", [])
        provenance = payload.get("provenance", {})
        expected = payload.get("expected_revision")
        request_id = self._request_id(payload.get("request_id"))
        if gender not in _GENDERS or not isinstance(enabled, bool):
            raise ValueError("角色性别或启用状态无效。")
        aliases = self._string_list(aliases, "别名", maximum=30, item_max=80)
        pool_ids = self._string_list(pool_ids, "角色池", maximum=100, item_max=64, unique=True)
        image_hashes = self._string_list(image_hashes, "图片", maximum=20, item_max=64, unique=True)
        if any(not _HASH.fullmatch(value) for value in image_hashes):
            raise ValueError("图片 SHA-256 格式无效。")
        if not isinstance(provenance, dict) or len(json.dumps(provenance, ensure_ascii=False)) > 20_000:
            raise ValueError("来源信息格式无效或超过大小限制。")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision 必须是非负整数。")
        normalized = {"id": character_id, "name": name, "gender": gender, "enabled": enabled,
                      "aliases": aliases, "pool_ids": pool_ids, "image_hashes": image_hashes, "provenance": provenance,
                      "expected_revision": expected}
        return self._mutate("character/save", request_id, actor, normalized, now, lambda db: self._save_character(db, normalized, actor, request_id, now))

    def _save_character(self, db: Any, data: dict[str, Any], actor: str, request_id: str, now: int) -> dict[str, Any]:
        row = db.execute("SELECT revision,deleted_at,provenance_json FROM characters WHERE id=?", (data["id"],)).fetchone()
        current = int(row["revision"]) if row else 0
        if row and row["deleted_at"] is not None:
            raise StorageError("已删除角色不能直接复用 ID。")
        if current != data["expected_revision"]:
            raise StorageError(f"角色版本冲突：预期 {data['expected_revision']}，当前 {current}。")
        mode = "wife" if data["gender"] == "female" else "husband" if data["gender"] == "male" else None
        if data["pool_ids"]:
            marks = ",".join("?" for _ in data["pool_ids"])
            pools = db.execute(f"SELECT id,mode,deleted_at FROM pools WHERE id IN ({marks})", data["pool_ids"]).fetchall()
            if len(pools) != len(data["pool_ids"]) or any(p["deleted_at"] is not None or (mode and p["mode"] != mode) for p in pools):
                raise ValueError("角色池不存在、已删除或与角色性别不匹配。")
        if data["image_hashes"]:
            marks = ",".join("?" for _ in data["image_hashes"])
            count = int(db.execute(f"SELECT COUNT(*) FROM media_blobs WHERE hash IN ({marks})", data["image_hashes"]).fetchone()[0])
            if count != len(data["image_hashes"]):
                raise ValueError("角色引用了尚未上传的图片素材。")
        provenance = dict(json.loads(row["provenance_json"])) if row else {}
        provenance.update(data["provenance"])
        revision = current + 1
        db.execute(
            """INSERT INTO characters(id,name,aliases_json,gender,enabled,revision,provenance_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases_json=excluded.aliases_json,
               gender=excluded.gender,enabled=excluded.enabled,revision=excluded.revision,provenance_json=excluded.provenance_json""",
            (data["id"], data["name"], json.dumps(data["aliases"], ensure_ascii=False), data["gender"], int(data["enabled"]), revision, json.dumps(provenance, ensure_ascii=False)),
        )
        db.execute("DELETE FROM character_images WHERE character_id=?", (data["id"],))
        db.executemany("INSERT INTO character_images(character_id,media_hash,ordinal) VALUES(?,?,?)",
                       [(data["id"], digest, ordinal) for ordinal, digest in enumerate(data["image_hashes"])])
        db.execute("DELETE FROM pool_members WHERE character_id=?", (data["id"],))
        db.executemany("INSERT INTO pool_members(pool_id,character_id) VALUES(?,?)", [(pool_id, data["id"]) for pool_id in data["pool_ids"]])
        response = {"id": data["id"], "revision": revision}
        self._audit(db, actor, "character.save", "character", data["id"], request_id, "", {"revision": revision}, now)
        return response

    def save_pool(self, payload: Mapping[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        data = {"id": self._identifier(payload.get("id"), "角色池 ID"), "name": self._text(payload.get("name"), "角色池名称", _MAX_NAME),
                "mode": payload.get("mode"), "character_ids": self._string_list(payload.get("character_ids", []), "角色", maximum=5000, item_max=64, unique=True),
                "expected_revision": payload.get("expected_revision")}
        data["request_id"] = self._request_id(payload.get("request_id"))
        if data["mode"] not in _MODES or isinstance(data["expected_revision"], bool) or not isinstance(data["expected_revision"], int) or data["expected_revision"] < 0:
            raise ValueError("角色池类型或版本无效。")
        return self._mutate("pool/save", data["request_id"], actor, data, now, lambda db: self._save_pool(db, data, actor, now))

    def _save_pool(self, db: Any, data: dict[str, Any], actor: str, now: int) -> dict[str, Any]:
        row = db.execute("SELECT revision,deleted_at,builtin FROM pools WHERE id=?", (data["id"],)).fetchone()
        current = int(row["revision"]) if row else 0
        if row and row["deleted_at"] is not None:
            raise StorageError("已删除角色池不能直接复用 ID。")
        if current != data["expected_revision"]:
            raise StorageError(f"角色池版本冲突：预期 {data['expected_revision']}，当前 {current}。")
        if data["character_ids"]:
            marks = ",".join("?" for _ in data["character_ids"])
            rows = db.execute(f"SELECT id,gender,deleted_at FROM characters WHERE id IN ({marks})", data["character_ids"]).fetchall()
            expected_gender = "female" if data["mode"] == "wife" else "male"
            if len(rows) != len(data["character_ids"]) or any(r["deleted_at"] is not None or r["gender"] not in (expected_gender, "unspecified") for r in rows):
                raise ValueError("角色池包含不存在、已删除或性别不匹配的角色。")
        revision = current + 1
        db.execute("INSERT INTO pools(id,name,mode,builtin,revision) VALUES(?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,mode=excluded.mode,revision=excluded.revision",
                   (data["id"], data["name"], data["mode"], revision))
        db.execute("DELETE FROM pool_members WHERE pool_id=?", (data["id"],))
        db.executemany("INSERT INTO pool_members(pool_id,character_id) VALUES(?,?)", [(data["id"], character_id) for character_id in data["character_ids"]])
        self._audit(db, actor, "pool.save", "pool", data["id"], data["request_id"], "", {"revision": revision}, now)
        return {"id": data["id"], "revision": revision}

    def delete_preview(self, kind: str, entity_id: str, *, actor: str, now: int) -> dict[str, Any]:
        if kind not in {"character", "pool"}:
            raise ValueError("删除目标类型无效。")
        table = "characters" if kind == "character" else "pools"
        with self.storage.transaction() as db:
            row = db.execute(f"SELECT revision,deleted_at FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise ValueError("目标不存在或已删除。")
            if kind == "character":
                impact = {"pool_memberships": int(db.execute("SELECT COUNT(*) FROM pool_members WHERE character_id=?", (entity_id,)).fetchone()[0]),
                          "relationships": int(db.execute("SELECT COUNT(*) FROM relationships WHERE subject_kind='character' AND subject_id=?", (entity_id,)).fetchone()[0]),
                          "snapshots_retained": True}
            else:
                settings_references = []
                settings_revision_snapshot = []
                for setting in db.execute("SELECT scope_key,value_json,revision FROM settings").fetchall():
                    value = json.loads(setting["value_json"])
                    settings_revision_snapshot.append({"scope_key": setting["scope_key"], "revision": int(setting["revision"])})
                    referenced_modes = [mode for mode in _MODES if entity_id in value.get("modes", {}).get(mode, {}).get("pool_ids", [])]
                    if referenced_modes:
                        settings_references.append({"scope_key": setting["scope_key"], "revision": int(setting["revision"]), "modes": referenced_modes})
                impact = {"pool_memberships": int(db.execute("SELECT COUNT(*) FROM pool_members WHERE pool_id=?", (entity_id,)).fetchone()[0]),
                          "settings_references": settings_references, "settings_revision_snapshot": settings_revision_snapshot,
                          "active_relationships_unchanged": True}
            preflight_id = str(uuid.uuid4())
            db.execute("INSERT INTO web_admin_preflights(id,actor_username,entity_kind,entity_id,revision,impact_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                       (preflight_id, actor, kind, entity_id, int(row["revision"]), json.dumps(impact), now, now + 600))
        return {"preflight_id": preflight_id, "entity_kind": kind, "entity_id": entity_id, "revision": int(row["revision"]), "impact": impact, "expires_at": now + 600}

    def delete(self, payload: Mapping[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        kind, entity_id = payload.get("entity_kind"), self._identifier(payload.get("entity_id"), "目標 ID")
        preflight_id = payload.get("preflight_id")
        reason = self._text(payload.get("reason"), "删除原因", 500)
        request_id = self._request_id(payload.get("request_id"))
        if kind not in {"character", "pool"} or not isinstance(preflight_id, str):
            raise ValueError("删除预检凭据无效。")
        data = {"entity_kind": kind, "entity_id": entity_id, "preflight_id": preflight_id, "reason": reason}
        return self._mutate("catalog/delete", request_id, actor, data, now, lambda db: self._delete(db, data, actor, request_id, now))

    def _delete(self, db: Any, data: dict[str, Any], actor: str, request_id: str, now: int) -> dict[str, Any]:
        token = db.execute("SELECT * FROM web_admin_preflights WHERE id=? AND actor_username=?", (data["preflight_id"], actor)).fetchone()
        if token is None or token["entity_kind"] != data["entity_kind"] or token["entity_id"] != data["entity_id"] or int(token["expires_at"]) < now or token["consumed_at"] is not None:
            raise StorageError("删除预检已过期、已使用或与目标不匹配，请重新预检。")
        table = "characters" if data["entity_kind"] == "character" else "pools"
        row = db.execute(f"SELECT revision,deleted_at FROM {table} WHERE id=?", (data["entity_id"],)).fetchone()
        if row is None or row["deleted_at"] is not None or int(row["revision"]) != int(token["revision"]):
            raise StorageError("删除目标已变化，请重新预检。")
        if data["entity_kind"] == "pool":
            impact = json.loads(token["impact_json"])
            snapshot = impact.get("settings_revision_snapshot", [])
            if len(snapshot) != int(db.execute("SELECT COUNT(*) FROM settings").fetchone()[0]):
                raise StorageError("角色池配置作用域已变化，请重新预检。")
            for reference in snapshot:
                current = db.execute("SELECT revision FROM settings WHERE scope_key=?", (reference["scope_key"],)).fetchone()
                if current is None or int(current["revision"]) != int(reference["revision"]):
                    raise StorageError("角色池配置引用已变化，请重新预检。")
        if data["entity_kind"] == "character":
            db.execute("UPDATE characters SET enabled=0,deleted_at=?,revision=revision+1 WHERE id=?", (now, data["entity_id"]))
            db.execute("DELETE FROM pool_members WHERE character_id=?", (data["entity_id"],))
        else:
            db.execute("UPDATE pools SET deleted_at=?,revision=revision+1 WHERE id=?", (now, data["entity_id"]))
            db.execute("DELETE FROM pool_members WHERE pool_id=?", (data["entity_id"],))
            self._remove_pool_from_settings(db, data["entity_id"], now, actor)
        db.execute("UPDATE web_admin_preflights SET consumed_at=? WHERE id=?", (now, data["preflight_id"]))
        self._audit(db, actor, f"{data['entity_kind']}.delete", data["entity_kind"], data["entity_id"], request_id, data["reason"], json.loads(token["impact_json"]), now)
        return {"deleted": True, "entity_id": data["entity_id"]}

    @staticmethod
    def _remove_pool_from_settings(db: Any, pool_id: str, now: int, actor: str) -> None:
        for row in db.execute("SELECT scope_key,value_json,revision FROM settings").fetchall():
            value = json.loads(row["value_json"])
            changed = False
            for mode in _MODES:
                pools = value.get("modes", {}).get(mode, {}).get("pool_ids")
                if isinstance(pools, list) and pool_id in pools:
                    value["modes"][mode]["pool_ids"] = [item for item in pools if item != pool_id]
                    changed = True
            if changed:
                db.execute("UPDATE settings SET value_json=?,revision=revision+1,updated_at=?,updated_by=? WHERE scope_key=?",
                           (json.dumps(value, ensure_ascii=False), now, actor, row["scope_key"]))

    def upload_media(self, payload: bytes, *, now: int) -> dict[str, Any]:
        if not payload or len(payload) > _MAX_IMAGE_BYTES:
            raise ValueError("图片文件为空或超过 12 MiB 限制。")
        width, height, extension, mime_type = _inspect_image(payload, _MAX_PIXELS)
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("sha256") / f"{digest}.{extension}"
        target = self.media_root / relative
        _atomic_content_write(target, payload)
        with self.storage.transaction() as db:
            db.execute("INSERT OR IGNORE INTO media_blobs(hash,relative_path,mime_type,byte_size,width,height,source_url,verified_at) VALUES(?,?,?,?,?,?,NULL,?)",
                       (digest, relative.as_posix(), mime_type, len(payload), width, height, now))
        return {"hash": digest, "mime_type": mime_type, "byte_size": len(payload), "width": width, "height": height}

    def get_media(self, digest: str) -> tuple[Path, str] | None:
        if not _HASH.fullmatch(digest):
            return None
        with self.storage._lock:
            row = self.storage._connection().execute("SELECT relative_path,mime_type FROM media_blobs WHERE hash=?", (digest,)).fetchone()
        if row is None:
            return None
        target = (self.media_root / row["relative_path"]).resolve()
        root = self.media_root.resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target, str(row["mime_type"])

    def catalog_summary(self) -> dict[str, int]:
        with self.storage._lock:
            db = self.storage._connection()
            characters = int(db.execute("SELECT COUNT(*) FROM characters WHERE deleted_at IS NULL").fetchone()[0])
            enabled = int(db.execute("SELECT COUNT(*) FROM characters WHERE deleted_at IS NULL AND enabled=1").fetchone()[0])
            pools = int(db.execute("SELECT COUNT(*) FROM pools WHERE deleted_at IS NULL").fetchone()[0])
            complete = int(db.execute("SELECT COUNT(DISTINCT c.id) FROM characters c JOIN character_images ci ON ci.character_id=c.id WHERE c.deleted_at IS NULL AND c.enabled=1").fetchone()[0])
        return {"characters": characters, "enabled_characters": enabled, "pools": pools, "enabled_with_images": complete}

    def _mutate(self, endpoint: str, request_id: str, actor: str, data: dict[str, Any], now: int, operation: Any) -> dict[str, Any]:
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self.storage.transaction() as db:
            prior = db.execute("SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint=? AND request_id=?", (actor, endpoint, request_id)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise StorageError("request_id 已用于不同内容。")
                return json.loads(prior["response_json"])
            response = operation(db)
            db.execute("INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                       (actor, endpoint, request_id, digest, json.dumps(response, ensure_ascii=False), now))
            return response

    @staticmethod
    def _audit(db: Any, actor: str, action: str, kind: str, entity_id: str, request_id: str, reason: str, details: dict[str, Any], now: int) -> None:
        db.execute("INSERT INTO web_admin_audit(id,actor_username,action,entity_kind,entity_id,request_id,reason,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), actor, action, kind, entity_id, request_id, reason, json.dumps(details, ensure_ascii=False), now))

    @staticmethod
    def _character_dict(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "aliases": json.loads(row["aliases_json"]), "gender": row["gender"],
                "enabled": bool(row["enabled"]), "revision": int(row["revision"]), "provenance": json.loads(row["provenance_json"]),
                "image_count": int(row["image_count"]) if "image_count" in row.keys() else 0}

    @staticmethod
    def _identifier(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"{label} 格式无效，只能使用小写字母、数字、点、下划线和连字符。")
        return value

    @staticmethod
    def _request_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("request_id 必须是 1 到 128 个字符。")
        return value.strip()

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValueError(f"{label}不能为空且最多 {maximum} 个字符。")
        return value.strip()

    @classmethod
    def _string_list(cls, value: Any, label: str, *, maximum: int, item_max: int, unique: bool = False) -> list[str]:
        if not isinstance(value, list) or len(value) > maximum:
            raise ValueError(f"{label}列表无效或项目过多。")
        result = [cls._text(item, label, item_max) for item in value]
        if unique and len(result) != len(set(result)):
            raise ValueError(f"{label}不能重复。")
        return result
