"""Preview and commit JSON/ZIP catalog packs without extracting untrusted paths."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import struct
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

from PIL import UnidentifiedImageError

from ..models import StorageError
from .catalog import _atomic_content_write, _inspect_image
from .storage import SQLiteStorage


_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_GENDERS = {"female", "male", "unspecified"}
_MODES = {"wife", "husband"}
_REDIRECTS = {301, 302, 303, 307, 308}


class CatalogImportService:
    def __init__(self, storage: SQLiteStorage, data_root: Path) -> None:
        self.storage = storage
        self.data_root = data_root
        self.job_root = data_root / "jobs"
        self.staging_root = data_root / "staging" / "imports"
        self.media_root = data_root / "media"

    async def preview_file(self, source: Path, *, actor: str, now: int, limits: dict[str, int], timeout: float) -> dict[str, Any]:
        max_bytes = int(limits["import_max_bytes"])
        max_entries = int(limits["import_max_entries"])
        max_expanded = int(limits["import_max_expanded_bytes"])
        max_image_bytes = int(limits["image_max_bytes"])
        max_pixels = int(limits["image_max_pixels"])
        if not source.is_file() or source.stat().st_size <= 0 or source.stat().st_size > max_bytes:
            raise ValueError("导入文件为空或超过配置的压缩/JSON 字节限制")
        archive: zipfile.ZipFile | None = None
        job_id = str(uuid.uuid4())
        stage = self.staging_root / job_id
        stage.mkdir(parents=True, exist_ok=False)
        try:
            manifest, members, archive = self._read_package(source, max_bytes, max_entries, max_expanded)
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ValueError("manifest 必须schema_version=1 JSON 对象")
            pack_id = self._text(manifest.get("pack_id"), "pack_id", 128)
            pack_version = self._text(manifest.get("pack_version"), "pack_version", 128)
            pools_raw = manifest.get("pools")
            characters_raw = manifest.get("characters")
            if not isinstance(pools_raw, list) or not isinstance(characters_raw, list):
                raise ValueError("manifest 必须包含 pools characters 数组")
            if len(pools_raw) + len(characters_raw) > max_entries:
                raise ValueError("目录项目数超import_max_entries")

            pool_rows, pool_ids, pool_errors = self._validate_pools(pools_raw)
            duplicate_ids = self._duplicate_character_ids(characters_raw)
            expanded_bytes = sum(item.file_size for item in members.values())
            if expanded_bytes > max_expanded:
                raise ValueError("ZIP 解压总大小超过限制")
            planned_characters: list[dict[str, Any]] = []
            image_total = 0
            fetcher = _SafeImageFetcher(max_image_bytes=max_image_bytes, timeout=timeout)
            try:
                for index, raw in enumerate(characters_raw):
                    try:
                        character = self._validate_character(raw, duplicate_ids)
                        image_plans: list[dict[str, Any]] = []
                        for image in character.pop("_images_raw"):
                            if image.get("source_url"):
                                payload, final_url, response_mime = await fetcher.fetch(str(image["source_url"]))
                                source_description = {"source_url": final_url}
                            else:
                                relative = image.get("path", image.get("relative_path"))
                                member_name = self._zip_image_path(relative, members)
                                if archive is None:
                                    raise ValueError("JSON 素材包的图片必须提供 source_url")
                                info = members[member_name]
                                if info.file_size > max_image_bytes:
                                    raise ValueError("图片文件超过 image_max_bytes")
                                with archive.open(info, "r") as stream:
                                    payload = stream.read(max_image_bytes + 1)
                                if len(payload) != info.file_size or len(payload) > max_image_bytes:
                                    raise ValueError("图片读取大小ZIP 清单不一致或超出限制")
                                source_description = {"path": member_name}
                            if archive is None or "source_url" in source_description:
                                expanded_bytes += len(payload)
                            if expanded_bytes > max_expanded:
                                raise ValueError("下载素材展开后超import_max_expanded_bytes")
                            digest = hashlib.sha256(payload).hexdigest()
                            declared_hash = image.get("sha256")
                            if declared_hash is not None and (not isinstance(declared_hash, str) or not _HASH.fullmatch(declared_hash) or declared_hash != digest):
                                raise ValueError("素材 SHA-256 缺失格式正确性或与内容不匹配")
                            width, height, extension, mime_type = await asyncio.to_thread(_inspect_image, payload, max_pixels)
                            declared_mime = image.get("mime_type")
                            if declared_mime is not None and not _mime_matches(declared_mime, mime_type):
                                raise ValueError("declared image MIME type does not match its content")
                            if image.get("source_url") and not _mime_matches(response_mime, mime_type):
                                raise ValueError("URL response MIME type does not match the decoded image")
                            for key, actual in (("width", width), ("height", height), ("bytes", len(payload))):
                                if image.get(key) is not None and (isinstance(image[key], bool) or int(image[key]) != actual):
                                    raise ValueError(f"素材 {key} 与实际内容不匹配")
                            staged = stage / "images" / f"{digest}.{extension}"
                            if not staged.exists():
                                await asyncio.to_thread(_atomic_content_write, staged, payload)
                            image_plans.append({"sha256": digest, "width": width, "height": height, "bytes": len(payload),
                                                "mime_type": mime_type, "extension": extension, **source_description})
                            image_total += 1
                        if character["enabled"] and not image_plans:
                            raise ValueError("没有可用图片的角色必disabled；待补档案可保留")
                        character["images"] = image_plans
                        character["unresolved_pool_ids"] = [pool_id for pool_id in character["pool_ids"] if pool_id not in pool_ids and not self._pool_exists(pool_id)]
                        character["_index"] = index
                        planned_characters.append(character)
                    except Exception as exc:
                        planned_characters.append({"_index": index, "id": raw.get("id") if isinstance(raw, dict) else None,
                                                   "name": raw.get("name") if isinstance(raw, dict) else None,
                                                   "status": "invalid", "errors": [str(exc)]})
            finally:
                await fetcher.close()

            with self.storage._lock:
                db = self.storage._connection()
                for item in planned_characters:
                    if item.get("status") == "invalid":
                        continue
                    row = db.execute("SELECT revision,deleted_at FROM characters WHERE id=?", (item["id"],)).fetchone()
                    item["status"] = "conflict" if row is not None else "new"
                    item["existing_revision"] = int(row["revision"]) if row else 0
                    item["deleted_id"] = bool(row and row["deleted_at"] is not None)
                    if item["deleted_id"]:
                        item["status"] = "blocked_deleted_id"
                    if row:
                        current = db.execute("SELECT name,aliases_json,gender,enabled FROM characters WHERE id=?", (item["id"],)).fetchone()
                        old_pools = [str(value[0]) for value in db.execute("SELECT pool_id FROM pool_members WHERE character_id=? ORDER BY pool_id", (item["id"],)).fetchall()]
                        old_images = [str(value[0]) for value in db.execute("SELECT media_hash FROM character_images WHERE character_id=? ORDER BY ordinal,media_hash", (item["id"],)).fetchall()]
                        item["existing"] = {"name": current["name"], "aliases": json.loads(current["aliases_json"]),
                                             "gender": current["gender"], "enabled": bool(current["enabled"]),
                                             "pool_ids": old_pools, "image_hashes": old_images}
                        item["diff"] = {"name": {"before": current["name"], "after": item["name"]},
                                         "aliases": {"before": json.loads(current["aliases_json"]), "after": item["aliases"]},
                                         "gender": {"before": current["gender"], "after": item["gender"]},
                                         "enabled": {"before": bool(current["enabled"]), "after": item["enabled"]},
                                         "pool_ids": {"before": old_pools, "after": item["pool_ids"]}}
                    item["valid"] = True
                for pool in pool_rows:
                    if pool["id"] in pool_errors:
                        continue
                    row = db.execute("SELECT mode,deleted_at,revision FROM pools WHERE id=?", (pool["id"],)).fetchone()
                    pool["status"] = "conflict" if row else "new"
                    pool["existing_mode"] = row["mode"] if row else None
                    pool["deleted_id"] = bool(row and row["deleted_at"] is not None)
                    if pool["deleted_id"] or (row and row["mode"] != pool["mode"]):
                        pool["status"] = "blocked_conflict"
            for pool in pool_rows:
                if pool["id"] in pool_errors:
                    pool["status"] = "invalid"
                    pool["errors"] = [pool_errors[pool["id"]]]

            for item in planned_characters:
                if item.get("valid"):
                    expected_mode = "wife" if item["gender"] == "female" else "husband" if item["gender"] == "male" else None
                    unresolved = []
                    for pool_id in item["pool_ids"]:
                        imported_pool = next((pool for pool in pool_rows if pool["id"] == pool_id), None)
                        if imported_pool:
                            if imported_pool.get("status") == "invalid" or (expected_mode and imported_pool["mode"] != expected_mode):
                                unresolved.append(pool_id)
                            continue
                        existing_pool = self.storage._connection().execute("SELECT mode,deleted_at FROM pools WHERE id=?", (pool_id,)).fetchone()
                        if existing_pool is None or existing_pool["deleted_at"] is not None or (expected_mode and existing_pool["mode"] != expected_mode):
                            unresolved.append(pool_id)
                    item["unresolved_pool_ids"] = unresolved
                    item["status_before_commit"] = item.pop("status")
                    item.pop("deleted_id", None)
            plan = {"schema_version": 1, "pack_id": pack_id, "pack_version": pack_version,
                    "pools": pool_rows, "characters": planned_characters,
                    "created_at": now, "image_count": image_total}
            for item in planned_characters:
                if item.get("valid"):
                    item["_pack_id"] = pack_id
                    item["_pack_version"] = pack_version
            plan_bytes = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            plan_hash = hashlib.sha256(plan_bytes).hexdigest()
            plan_path = stage / "plan.json"
            await asyncio.to_thread(_atomic_content_write, plan_path, plan_bytes)
            summary = self._summary(plan)
            report = {"summary": summary, "items": self._public_items(plan)}
            with self.storage.transaction() as db:
                db.execute("INSERT INTO maintenance_jobs(id,kind,actor,state,expected_revision,staged_manifest_hash,report_json,created_at,expires_at) VALUES(?,'import',?,'ready',?,?,?,?,?)",
                           (job_id, actor, "catalog", plan_hash, json.dumps(report, ensure_ascii=False), now, now + 86400))
            return {"job_id": job_id, "pack_id": pack_id, "pack_version": pack_version, **summary,
                    "items": report["items"], "expires_at": now + 86400}
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            if archive is not None:
                archive.close()

    def commit(self, payload: dict[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        job_id = self._text(payload.get("job_id"), "job_id", 64)
        request_id = self._text(payload.get("request_id"), "request_id", 128)
        conflicts = payload.get("conflicts", {})
        if not isinstance(conflicts, dict) or len(conflicts) > 5000 or any(value not in {"skip", "update"} for value in conflicts.values()):
            raise ValueError("conflicts 必须是角ID skip/update 的映射")
        image_strategy = payload.get("image_strategy", "append")
        if image_strategy not in {"append", "replace"}:
            raise ValueError("image_strategy 只能append replace")
        digest = hashlib.sha256(json.dumps({"job_id": job_id, "conflicts": conflicts, "image_strategy": image_strategy},
                                           sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with self.storage.transaction() as db:
            previous = db.execute("SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint='imports/commit' AND request_id=?", (actor, request_id)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise StorageError("request_id 已用于不同导入提交")
                return json.loads(previous["response_json"])
            job = db.execute("SELECT * FROM maintenance_jobs WHERE id=? AND kind='import' AND actor=?", (job_id, actor)).fetchone()
            if job is None or int(job["expires_at"]) < now:
                raise StorageError("导入作业不存在或已过期")
            if job["state"] not in {"ready", "committing"}:
                raise StorageError("导入作业已经终止提交，需重新预检")
            if job["state"] == "ready":
                db.execute("UPDATE maintenance_jobs SET state='committing' WHERE id=? AND state='ready'", (job_id,))
        stage = self.staging_root / job_id
        plan_path = stage / "plan.json"
        try:
            plan_bytes = plan_path.read_bytes()
        except OSError as exc:
            raise StorageError("导入暂存文件已丢失，请重新预检") from exc
        if hashlib.sha256(plan_bytes).hexdigest() != job["staged_manifest_hash"]:
            raise StorageError("导入预检清单哈希校验失败")
        plan = json.loads(plan_bytes)
        report = json.loads(job["report_json"])
        results = report.get("results", {})
        # Pools are inserted only when absent. Existing pools and their local mappings remain administrator-owned.
        for pool in plan["pools"]:
            if pool.get("status") in {"invalid", "blocked_conflict"}:
                continue
            with self.storage.transaction() as db:
                existing = db.execute("SELECT deleted_at,mode FROM pools WHERE id=?", (pool["id"],)).fetchone()
                if existing is None:
                    db.execute("INSERT INTO pools(id,name,mode,builtin,revision) VALUES(?,?,?,0,1)", (pool["id"], pool["name"], pool["mode"]))
                    results[f"pool:{pool['id']}"] = {"status": "created"}
                else:
                    results.setdefault(f"pool:{pool['id']}", {"status": "kept_existing"})
                self._save_report(db, job_id, report, results)

        pool_ids = {pool["id"] for pool in plan["pools"] if pool.get("status") not in {"invalid", "blocked_conflict"}}
        for item in plan["characters"]:
            character_id = item.get("id")
            if not item.get("valid"):
                results[f"character:{item.get('_index')}"] = {"id": character_id, "status": "invalid", "errors": item.get("errors", [])}
                self._write_report(job_id, report, results)
                continue
            key = f"character:{character_id}"
            if key in results and results[key].get("status") in {"created", "updated", "skipped", "revision_conflict", "blocked_deleted_id"}:
                continue
            decision = conflicts.get(character_id, "skip") if item.get("status_before_commit") == "conflict" else "update"
            if item.get("status_before_commit") == "blocked_deleted_id":
                results[key] = {"id": character_id, "status": "blocked_deleted_id"}
                self._write_report(job_id, report, results)
                continue
            if item.get("status_before_commit") == "conflict" and decision == "skip":
                results[key] = {"id": character_id, "status": "skipped"}
                self._write_report(job_id, report, results)
                continue
            try:
                result = self._commit_character(job_id, item, pool_ids, image_strategy, actor, now, report, results)
                results[key] = {"id": character_id, **result}
            except StorageError as exc:
                results[key] = {"id": character_id, "status": "revision_conflict", "message": str(exc)}
                self._write_report(job_id, report, results)

        summary = self._result_summary(plan, results)
        response = {"job_id": job_id, **summary, "results": results}
        with self.storage.transaction() as db:
            db.execute("UPDATE maintenance_jobs SET state='completed',report_json=? WHERE id=?", (json.dumps({"summary": summary, "items": self._public_items(plan), "results": results}, ensure_ascii=False), job_id))
            db.execute("INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                       (actor, "imports/commit", request_id, digest, json.dumps(response, ensure_ascii=False), now))
        return response

    def get_job(self, job_id: str, *, actor: str) -> dict[str, Any] | None:
        with self.storage._lock:
            row = self.storage._connection().execute("SELECT id,kind,state,report_json,created_at,expires_at FROM maintenance_jobs WHERE id=? AND actor=?", (job_id, actor)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["report"] = json.loads(data.pop("report_json"))
        return data

    def _commit_character(self, job_id: str, item: dict[str, Any], valid_pool_ids: set[str], image_strategy: str,
                          actor: str, now: int, report: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
        character_id = item["id"]
        staged_images = []
        for image in item["images"]:
            ext = image["extension"]
            path = self.staging_root / job_id / "images" / f"{image['sha256']}.{ext}"
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != image["sha256"]:
                raise StorageError("角色素材暂存文件缺失或哈希不匹配")
            staged_images.append((image, path))
        with self.storage.transaction() as db:
            existing = db.execute("SELECT revision,deleted_at,provenance_json FROM characters WHERE id=?", (character_id,)).fetchone()
            expected = int(item.get("existing_revision", 0))
            current = int(existing["revision"]) if existing else 0
            if current != expected:
                raise StorageError(f"角色 {character_id} 版本已变化：预检 {expected}，当{current}")
            if existing and existing["deleted_at"] is not None:
                raise StorageError(f"角色 {character_id} 已软删除")
            image_hashes = [image["sha256"] for image, _path in staged_images]
            if existing and image_strategy == "append":
                old_hashes = [row[0] for row in db.execute("SELECT media_hash FROM character_images WHERE character_id=? ORDER BY ordinal,media_hash", (character_id,)).fetchall()]
                image_hashes = list(dict.fromkeys([*old_hashes, *image_hashes]))
            provenance = json.loads(existing["provenance_json"]) if existing else {}
            provenance.update(item.get("provenance", {}))
            provenance["import_pack"] = {"pack_id": item.get("_pack_id"), "pack_version": item.get("_pack_version")}
            provenance["import_image_sources"] = [{key: image[key] for key in ("sha256", "source_url", "path") if key in image} for image in item["images"]]
            revision = current + 1
            db.execute("""INSERT INTO characters(id,name,aliases_json,gender,enabled,revision,provenance_json) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases_json=excluded.aliases_json,gender=excluded.gender,
                       enabled=excluded.enabled,revision=excluded.revision,provenance_json=excluded.provenance_json""",
                       (character_id, item["name"], json.dumps(item["aliases"], ensure_ascii=False), item["gender"], int(item["enabled"]), revision, json.dumps(provenance, ensure_ascii=False)))
            self.media_root.mkdir(parents=True, exist_ok=True)
            for image, staged in staged_images:
                extension = image["extension"]
                relative = Path("sha256") / f"{image['sha256']}.{extension}"
                target = self.media_root / relative
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != image["sha256"]:
                    _atomic_content_write(target, staged.read_bytes())
                db.execute("INSERT OR IGNORE INTO media_blobs(hash,relative_path,mime_type,byte_size,width,height,source_url,verified_at) VALUES(?,?,?,?,?,?,?,?)",
                           (image["sha256"], relative.as_posix(), image["mime_type"], image["bytes"], image["width"], image["height"], image.get("source_url"), now))
            db.execute("DELETE FROM character_images WHERE character_id=?", (character_id,))
            db.executemany("INSERT INTO character_images(character_id,media_hash,ordinal) VALUES(?,?,?)", [(character_id, digest, index) for index, digest in enumerate(image_hashes)])
            joined_pools, unresolved, wanted_pool_ids = [], [], set()
            for pool_id in item["pool_ids"]:
                row = db.execute("SELECT mode,deleted_at FROM pools WHERE id=?", (pool_id,)).fetchone()
                mode = "wife" if item["gender"] == "female" else "husband" if item["gender"] == "male" else None
                if pool_id in valid_pool_ids or (row and row["deleted_at"] is None and (mode is None or row["mode"] == mode)):
                    if row and row["deleted_at"] is None and (mode is None or row["mode"] == mode):
                        db.execute("INSERT OR IGNORE INTO pool_members(pool_id,character_id) VALUES(?,?)", (pool_id, character_id))
                        joined_pools.append(pool_id)
                        wanted_pool_ids.add(pool_id)
                else:
                    unresolved.append(pool_id)
            removed_pools = []
            for row in db.execute("SELECT pool_id FROM pool_members WHERE character_id=?", (character_id,)).fetchall():
                pool_id = str(row["pool_id"])
                if pool_id not in wanted_pool_ids:
                    db.execute("DELETE FROM pool_members WHERE pool_id=? AND character_id=?", (pool_id, character_id))
                    removed_pools.append(pool_id)
            result = {"status": "updated" if existing else "created", "revision": revision,
                      "images": len(image_hashes), "joined_pools": joined_pools, "removed_pools": removed_pools,
                      "unresolved_pool_ids": unresolved}
            results[f"character:{character_id}"] = {"id": character_id, **result}
            self._save_report(db, job_id, report, results)
            return result

    def _read_package(self, source: Path, max_bytes: int, max_entries: int, max_expanded: int):
        with source.open("rb") as stream:
            is_zip = source.stat().st_size >= 4 and stream.read(4).startswith(b"PK\x03\x04")
        if is_zip:
            _preflight_zip_count(source, max_entries)
            archive = zipfile.ZipFile(source)
            infos = archive.infolist()
            if len(infos) > max_entries:
                archive.close()
                raise ValueError("ZIP 条目数量超过 import_max_entries")
            by_name: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            expanded = 0
            for info in infos:
                name = self._safe_zip_name(info.filename)
                if info.is_dir():
                    if name != "images" and not name.startswith("images/"):
                        archive.close()
                        raise ValueError("ZIP 只允images/ 目录")
                    continue
                if name in by_name or name.casefold() in folded:
                    archive.close()
                    raise ValueError("ZIP 包含重复或大小写冲突的路径")
                folded.add(name.casefold())
                by_name[name] = info
                expanded += int(info.file_size)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    archive.close()
                    raise ValueError("ZIP 不允许符号链接")
                if info.flag_bits & 0x1:
                    archive.close()
                    raise ValueError("ZIP 不允许加密条目")
                if expanded > max_expanded:
                    archive.close()
                    raise ValueError("ZIP 解压总大小超过限制")
                if name != "manifest.json" and not name.startswith("images/"):
                    archive.close()
                    raise ValueError("ZIP 只允许根 manifest.json images/ 下的素材")
            if "manifest.json" not in by_name:
                archive.close()
                raise ValueError("ZIP 根目录缺manifest.json")
            info = by_name["manifest.json"]
            if info.file_size > max_bytes:
                archive.close()
                raise ValueError("manifest.json 超过限制")
            manifest = self._parse_json(archive.read(info))
            return manifest, by_name, archive
        raw = source.read_bytes()
        if len(raw) > max_bytes:
            raise ValueError("JSON 包超import_max_bytes")
        return self._parse_json(raw), {}, None

    @staticmethod
    def _parse_json(raw: bytes) -> Any:
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"JSON 字段重复：{key}")
                result[key] = value
            return result
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=object_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法 JSON 常量：{value}")))

    @staticmethod
    def _safe_zip_name(value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value or value.startswith("/") or ":" in value:
            raise ValueError("ZIP 包含绝对路径或非法路径")
        raw_parts = value.split("/")
        allowed_trailing_slash = raw_parts[-1] == ""
        checked_parts = raw_parts[:-1] if allowed_trailing_slash else raw_parts
        if not checked_parts or any(part in {"", ".", ".."} for part in checked_parts):
            raise ValueError("ZIP 路径不能包含空段、`.` `..`")
        return "/".join(checked_parts)

    @staticmethod
    def _zip_image_path(value: Any, members: dict[str, zipfile.ZipInfo]) -> str:
        if not isinstance(value, str):
            raise ValueError("ZIP 图片必须给出 images/ 下的相对路径")
        name = CatalogImportService._safe_zip_name(value)
        if not name.startswith("images/") or name not in members or members[name].is_dir():
            raise ValueError("清单引用的图片路径不存在或不images/ 下")
        return name

    @classmethod
    def _validate_pools(cls, raw: list[Any]) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
        rows, ids, errors = [], set(), {}
        for index, pool in enumerate(raw):
            try:
                if not isinstance(pool, dict):
                    raise ValueError("角色池必须是 JSON 对象")
                pool_id = cls._identifier(pool.get("id"), "角色ID")
                if pool_id in ids:
                    raise ValueError("角色ID 重复")
                ids.add(pool_id)
                mode = pool.get("mode")
                if mode not in _MODES:
                    raise ValueError("角色mode 必须wife husband")
                rows.append({"id": pool_id, "name": cls._text(pool.get("name"), "pool name", 120), "mode": mode, "_index": index})
            except (ValueError, TypeError) as exc:
                key = str(pool.get("id", index)) if isinstance(pool, dict) else str(index)
                errors[key] = str(exc)
                rows.append({"id": key, "name": "", "mode": "wife", "_index": index, "status": "invalid"})
        return rows, ids, errors

    @classmethod
    def _validate_character(cls, raw: Any, duplicates: set[str]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("角色必须JSON 对象")
        character_id = cls._identifier(raw.get("id"), "角色 ID")
        if character_id in duplicates:
            raise ValueError("同一manifest 内存在重复角ID")
        gender = raw.get("gender")
        if gender not in _GENDERS:
            raise ValueError("角色 gender 无效")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("角色 enabled 必须是布尔值")
        aliases = cls._string_list(raw.get("aliases", []), "角色别名", 30, 80)
        pool_ids = cls._string_list(raw.get("pool_ids", []), "角色ID", 100, 128)
        images = raw.get("images", [])
        if not isinstance(images, list) or len(images) > 20 or any(not isinstance(item, dict) for item in images):
            raise ValueError("角色 images 必须是最20 张图片的数组")
        if enabled and not images:
            raise ValueError("启用角色必须含图片")
        for image in images:
            has_url = isinstance(image.get("source_url"), str) and bool(image["source_url"])
            has_path = isinstance(image.get("path", image.get("relative_path")), str)
            if has_url == has_path:
                raise ValueError("每张图片必须恰好提供 source_url ZIP 相对 path")
        provenance = {key: value for key, value in raw.items() if key not in {"id", "name", "aliases", "gender", "enabled", "pool_ids", "images"}}
        if len(json.dumps(provenance, ensure_ascii=False, allow_nan=False)) > 20_000:
            raise ValueError("角色来源信息超过大小限制")
        return {"id": character_id, "name": cls._text(raw.get("name"), "角色名称", 120), "aliases": aliases,
                "gender": gender, "enabled": enabled, "pool_ids": pool_ids, "provenance": provenance, "_images_raw": images}

    def _pool_exists(self, pool_id: str) -> bool:
        with self.storage._lock:
            return self.storage._connection().execute("SELECT 1 FROM pools WHERE id=? AND deleted_at IS NULL", (pool_id,)).fetchone() is not None

    @staticmethod
    def _duplicate_character_ids(characters: list[Any]) -> set[str]:
        counts: dict[str, int] = {}
        for item in characters:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                counts[item["id"]] = counts.get(item["id"], 0) + 1
        return {item for item, count in counts.items() if count > 1}

    @staticmethod
    def _string_list(value: Any, label: str, maximum: int, item_max: int) -> list[str]:
        if not isinstance(value, list) or len(value) > maximum:
            raise ValueError(f"{label}数组无效或项目过多")
        result = [CatalogImportService._text(item, label, item_max) for item in value]
        if len(result) != len(set(result)):
            raise ValueError(f"{label}不能重复")
        return result

    @staticmethod
    def _identifier(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"{label}格式无效")
        return value

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValueError(f"{label}不能为空且最{maximum} 个字符")
        return value.strip()

    @staticmethod
    def _summary(plan: dict[str, Any]) -> dict[str, int]:
        chars = plan["characters"]
        pools = plan["pools"]
        return {"characters_total": len(chars), "characters_valid": sum(bool(item.get("valid")) for item in chars),
                "characters_invalid": sum(not item.get("valid") for item in chars),
                "characters_conflict": sum(item.get("status_before_commit") == "conflict" for item in chars),
                "pools_total": len(pools), "pools_invalid": sum(item.get("status") == "invalid" for item in pools),
                "images_staged": int(plan["image_count"])}

    @staticmethod
    def _public_items(plan: dict[str, Any]) -> dict[str, Any]:
        return {"pools": [{key: value for key, value in pool.items() if not key.startswith("_")} for pool in plan["pools"]],
                "characters": [{key: value for key, value in item.items() if not key.startswith("_") or key == "status_before_commit"} for item in plan["characters"]]}

    @staticmethod
    def _result_summary(plan: dict[str, Any], results: dict[str, Any]) -> dict[str, int]:
        statuses = [item.get("status") for item in results.values()]
        return {"created": statuses.count("created"), "updated": statuses.count("updated"),
                "skipped": statuses.count("skipped"), "conflicts": statuses.count("revision_conflict") + statuses.count("blocked_deleted_id"),
                "invalid": statuses.count("invalid"), "pools_created": sum(key.startswith("pool:") and value.get("status") == "created" for key, value in results.items())}

    def _save_report(self, db: Any, job_id: str, report: dict[str, Any], results: dict[str, Any]) -> None:
        report["results"] = results
        db.execute("UPDATE maintenance_jobs SET report_json=? WHERE id=?", (json.dumps(report, ensure_ascii=False), job_id))

    def _write_report(self, job_id: str, report: dict[str, Any], results: dict[str, Any]) -> None:
        with self.storage.transaction() as db:
            self._save_report(db, job_id, report, results)


class _SafeImageFetcher:
    def __init__(self, *, max_image_bytes: int, timeout: float) -> None:
        self.max_image_bytes = max_image_bytes
        self.timeout = timeout
        self.session = None

    async def fetch(self, url: str) -> tuple[bytes, str, str]:
        try:
            import aiohttp
        except ModuleNotFoundError as exc:
            raise ValueError("URL 图片导入需要安requirements.txt 中声明的 aiohttp") from exc
        if self.session is None:
            connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False, limit=4)
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), connector=connector, trust_env=False)
        current = _validate_public_url(url)
        for _attempt in range(6):
            async with self.session.get(current, allow_redirects=False, headers={"Accept": "image/*"}) as response:
                if response.status in _REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("素材 URL 跳转缺少 Location")
                    current = _validate_public_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise ValueError(f"素材 URL 返回 HTTP {response.status}")
                response_mime = response.content_type.lower()
                if not response_mime.startswith("image/"):
                    raise ValueError("URL response did not declare an image MIME type")
                length = response.content_length
                if length is not None and length > self.max_image_bytes:
                    raise ValueError("URL 图片超过 image_max_bytes")
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self.max_image_bytes:
                        raise ValueError("URL 图片超过 image_max_bytes")
                    chunks.append(chunk)
                if not total:
                    raise ValueError("URL 图片内容为空")
                return b"".join(chunks), current, response_mime
        raise ValueError("素材 URL 重定向次数超过限制")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()


def _validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("素材 URL 无效或过长")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("素材 URL 只允许无凭据HTTP/HTTPS 地址")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("素材 URL 端口无效") from exc
    if port not in (None, 80, 443):
        raise ValueError("素材 URL 只允许标HTTP/HTTPS 端口")
    try:
        address = ipaddress.ip_address(parts.hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("素材 URL 不能指向非公IP 地址")
    return url


def _mime_matches(declared: Any, actual: str) -> bool:
    if not isinstance(declared, str):
        return False
    normalized = declared.split(";", 1)[0].strip().lower()
    aliases = {"image/jpg": "image/jpeg", "image/x-png": "image/png"}
    return aliases.get(normalized, normalized) == actual.lower()


def _preflight_zip_count(source: Path, max_entries: int) -> None:
    size = source.stat().st_size
    tail_size = min(size, 22 + 65_535)
    with source.open("rb") as stream:
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError("ZIP end-of-directory record is missing or invalid")
    record = tail[offset:offset + 22]
    _signature, disk, central_disk, disk_entries, total_entries, central_size, central_offset, comment_size = struct.unpack("<4s4H2LH", record)
    if len(tail) - offset != 22 + comment_size or disk != 0 or central_disk != 0 or disk_entries != total_entries:
        raise ValueError("multi-disk or malformed ZIP archives are not supported")
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        raise ValueError("ZIP64 archives exceed the supported entry-count envelope")
    if total_entries > max_entries:
        raise ValueError("ZIP entry count exceeds import_max_entries")
    if central_size > min(16 * 1024 * 1024, max_entries * 4096):
        raise ValueError("ZIP central directory is too large for its configured entry-count limit")
    if central_offset + central_size > size - tail_size + offset:
        raise ValueError("ZIP central directory is outside the uploaded file")


class _PublicResolver:
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            ipaddress.ip_address(host)
            addresses = [(host, family)]
        except ValueError:
            rows = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
            addresses = [(row[4][0], row[0]) for row in rows]
        if not addresses:
            raise OSError("素材服务器域名没DNS 地址")
        result, seen = [], set()
        for address, address_family in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise OSError("素材 URL 解析到非公网地址，已拒绝以防 SSRF")
            if str(ip) in seen:
                continue
            seen.add(str(ip))
            result.append({"hostname": host, "host": str(ip), "port": port, "family": address_family, "proto": 0, "flags": 0})
        return result

    async def close(self) -> None:
        return None
