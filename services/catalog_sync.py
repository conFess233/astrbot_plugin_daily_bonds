"""Review and atomically apply selected catalog changes from the official repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ModuleNotFoundError:
    aiohttp = None  # type: ignore[assignment]

from ..models import StorageError
from .catalog import (
    _atomic_content_write,
    _fetch_limited,
    _inspect_image,
    _validate_image_url,
    catalog_source_json,
)
from .catalog_import import _PublicResolver
from .storage import SQLiteStorage

_REPOSITORY = "conFess233/astrbot_plugin_daily_bonds"
_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORIES = ("resources/characters/", "resources/pools/")
_MAX_JSON_BYTES = 50 * 1024 * 1024


class CatalogSyncService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage
        self.media_root = storage.database_path.parent / "media"
        self._plans: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._remote_cache: tuple[int, dict[str, Any]] | None = None

    async def check(self, *, actor: str, force: bool = False) -> dict[str, Any]:
        now = int(time.time())
        if not force and self._remote_cache and now - self._remote_cache[0] < 300:
            remote = self._remote_cache[1]
        else:
            try:
                remote = await self._fetch_remote()
            except (ValueError, StorageError):
                raise
            except Exception as exc:
                raise ValueError("读取仓库角色配置失败，请稍后重试。") from exc
            self._remote_cache = (now, remote)
        items = await asyncio.to_thread(self._compare, remote)
        token = str(uuid.uuid4())
        self._plans[token] = {
            "actor": actor,
            "remote": remote,
            "items": items,
            "expires": now + 600,
        }
        self._plans = {
            key: value for key, value in self._plans.items() if value["expires"] >= now
        }
        public = [
            {
                key: value
                for key, value in item.items()
                if key not in {"source", "revision"}
            }
            for item in items
        ]
        return {
            "preview_id": token,
            "commit_sha": remote["commit_sha"],
            "items": public,
            "unavailable": bool(remote.get("unavailable")),
            "updates": sum(
                item["status"] in {"new", "update", "conflict"} for item in items
            ),
            "expires_at": now + 600,
        }

    async def commit(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        token = payload.get("preview_id")
        if not isinstance(token, str):
            raise ValueError("请先检查仓库更新。")
        plan = self._plans.get(token)
        if plan is None or plan["actor"] != actor or plan["expires"] < int(time.time()):
            raise ValueError("同步预览已过期，请重新检查。")
        requested_characters = payload.get("characters", [])
        requested_pools = payload.get("pools", [])
        if (
            not isinstance(requested_characters, list)
            or not isinstance(requested_pools, list)
            or any(
                not isinstance(value, str)
                for value in requested_characters + requested_pools
            )
        ):
            raise ValueError("请选择要同步的角色和角色池。")
        if len(set(requested_characters)) != len(requested_characters) or len(
            set(requested_pools)
        ) != len(requested_pools):
            raise ValueError("同步选择包含重复 ID。")
        chosen = {
            (kind, identifier)
            for kind, values in (
                ("character", requested_characters),
                ("pool", requested_pools),
            )
            for identifier in values
        }
        options = {(item["kind"], item["id"]): item for item in plan["items"]}
        if not chosen or any(
            key not in options
            or options[key]["status"] not in {"new", "update", "conflict"}
            for key in chosen
        ):
            raise ValueError("同步选择为空或包含无效项目。")
        async with self._lock:
            if self._plans.get(token) is not plan:
                raise ValueError("同步预览已被使用，请重新检查。")
            await asyncio.to_thread(self._validate_dependencies, chosen, options)
            selected = [options[key] for key in sorted(chosen)]
            try:
                images = await self._download_images(selected)
            except (ValueError, StorageError):
                raise
            except Exception as exc:
                raise ValueError(
                    "所选角色图片下载失败，整批未写入，请稍后重试。"
                ) from exc
            await asyncio.to_thread(
                self._commit_selected,
                selected,
                chosen,
                options,
                images,
                plan,
                actor,
                requested_characters,
                requested_pools,
            )
            self._plans.pop(token, None)
            return {
                "commit_sha": plan["remote"]["commit_sha"],
                "characters": len(requested_characters),
                "pools": len(requested_pools),
                "images_added": len(images),
            }

    def _commit_selected(
        self,
        selected,
        chosen,
        options,
        images,
        plan,
        actor,
        requested_characters,
        requested_pools,
    ):
        now = int(time.time())
        with self.storage.transaction() as db:
            for item in selected:
                table = "characters" if item["kind"] == "character" else "pools"
                row = db.execute(
                    f"SELECT revision,deleted_at FROM {table} WHERE id=?", (item["id"],)
                ).fetchone()
                current = int(row["revision"]) if row else 0
                if current != item["revision"] or (
                    row and row["deleted_at"] is not None
                ):
                    raise StorageError(f"{item['id']} 已变化，请重新检查仓库更新。")
            self._validate_dependencies(chosen, options)
            for item in selected:
                if item["kind"] == "character":
                    self._apply_character(db, item, images, now)
            for item in selected:
                if item["kind"] == "pool":
                    self._apply_pool(db, item)
            for item in selected:
                source = catalog_source_json(item["source"], item["kind"])
                db.execute(
                    """INSERT INTO catalog_sync_baselines(entity_kind,entity_id,source_json,source_sha,commit_sha,updated_at)
                              VALUES(?,?,?,?,?,?) ON CONFLICT(entity_kind,entity_id) DO UPDATE SET
                              source_json=excluded.source_json,source_sha=excluded.source_sha,
                              commit_sha=excluded.commit_sha,updated_at=excluded.updated_at""",
                    (
                        item["kind"],
                        item["id"],
                        source,
                        hashlib.sha256(source.encode()).hexdigest(),
                        plan["remote"]["commit_sha"],
                        now,
                    ),
                )
            db.execute(
                "INSERT INTO web_admin_audit(id,actor_username,action,entity_kind,entity_id,request_id,reason,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    actor,
                    "catalog.sync",
                    "catalog",
                    plan["remote"]["commit_sha"],
                    str(uuid.uuid4()),
                    "",
                    json.dumps(
                        {"characters": requested_characters, "pools": requested_pools},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    async def _fetch_remote(self) -> dict[str, Any]:
        if aiohttp is None:
            raise StorageError("检查仓库更新需要安装 requirements.txt 中的 aiohttp。")
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "astrbot-plugin-daily-bonds",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            repository = await self._json(
                session, f"https://api.github.com/repos/{_REPOSITORY}", _MAX_JSON_BYTES
            )
            branch = repository.get("default_branch")
            if not isinstance(branch, str) or not re.fullmatch(
                r"[A-Za-z0-9._/-]{1,100}", branch
            ):
                raise ValueError("仓库默认分支信息无效。")
            commit = await self._json(
                session,
                f"https://api.github.com/repos/{_REPOSITORY}/commits/{branch}",
                _MAX_JSON_BYTES,
            )
            sha = commit.get("sha")
            tree_sha = commit.get("commit", {}).get("tree", {}).get("sha")
            if (
                not isinstance(sha, str)
                or not _SHA.fullmatch(sha)
                or not isinstance(tree_sha, str)
                or not _SHA.fullmatch(tree_sha)
            ):
                raise ValueError("仓库提交信息无效。")
            tree = await self._json(
                session,
                f"https://api.github.com/repos/{_REPOSITORY}/git/trees/{tree_sha}?recursive=1",
                _MAX_JSON_BYTES,
            )
            if tree.get("truncated") or not isinstance(tree.get("tree"), list):
                raise ValueError("仓库目录列表不完整。")
            entries = [
                item
                for item in tree["tree"]
                if item.get("type") == "blob"
                and isinstance(item.get("path"), str)
                and item["path"].endswith(".json")
                and any(
                    item["path"].startswith(directory)
                    and "/" not in item["path"][len(directory) :]
                    for directory in _DIRECTORIES
                )
            ]
            if (
                len(entries) > 1000
                or any(
                    not isinstance(item.get("size"), int) or item["size"] > _MAX_JSON_BYTES
                    for item in entries
                )
                or sum(item["size"] for item in entries) > _MAX_JSON_BYTES
            ):
                raise ValueError("仓库角色配置文件数量或体积超出限制。")
            paths = sorted(item["path"] for item in entries)
            if any(
                not re.fullmatch(
                    r"resources/(?:characters|pools)/[A-Za-z0-9_.-]+\.json", path
                )
                for path in paths
            ):
                raise ValueError("仓库角色配置文件名无效。")
            if not any(path.startswith(_DIRECTORIES[0]) for path in paths) or not any(
                path.startswith(_DIRECTORIES[1]) for path in paths
            ):
                return {
                    "commit_sha": sha,
                    "characters": {},
                    "pools": {},
                    "unavailable": True,
                }
            semaphore = asyncio.Semaphore(8)

            async def one(path: str) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    source = await self._json(
                        session,
                        f"https://raw.githubusercontent.com/{_REPOSITORY}/{sha}/{path}",
                        _MAX_JSON_BYTES,
                    )
                    return path, source

            pairs = await asyncio.gather(*(one(path) for path in paths))
        characters: dict[str, dict[str, Any]] = {}
        pools: dict[str, dict[str, Any]] = {}
        for path, item in pairs:
            kind = "character" if path.startswith(_DIRECTORIES[0]) else "pool"
            self._validate_source(item, kind)
            target = characters if kind == "character" else pools
            if item["id"] in target:
                raise ValueError(f"仓库目录包含重复 ID：{item['id']}")
            target[item["id"]] = item
        for pool in pools.values():
            for identifier in pool["character_ids"]:
                if identifier not in characters:
                    raise ValueError(f"仓库角色池引用了缺失角色：{identifier}")
                gender = characters[identifier]["gender"]
                if gender not in (
                    {"female", "unspecified"}
                    if pool["mode"] == "wife"
                    else {"male", "unspecified"}
                ):
                    raise ValueError(f"仓库角色池与角色性别不匹配：{identifier}")
        return {"commit_sha": sha, "characters": characters, "pools": pools}

    @staticmethod
    async def _json(session: aiohttp.ClientSession, url: str, limit: int) -> dict[str, Any]:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"读取仓库失败（HTTP {response.status}）。")
            if response.content_length and response.content_length > limit:
                raise ValueError("仓库配置文件超出大小限制。")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("仓库配置文件超出大小限制。")
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("仓库配置文件不是 JSON 对象。")
        return parsed

    @staticmethod
    def _validate_source(item: dict[str, Any], kind: str) -> None:
        identifier = item.get("id")
        name = item.get("name")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError("仓库角色或角色池的 ID、名称无效。")
        if kind == "pool":
            members = item.get("character_ids")
            if item.get("mode") not in {"wife", "husband"} or not isinstance(members, list) or len(members) > 5000 or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in members) or len(set(members)) != len(members):
                raise ValueError(f"仓库角色池配置无效：{identifier}")
        else:
            aliases = item.get("aliases", [])
            images = item.get("images", [])
            if (
                item.get("gender") not in {"female", "male", "unspecified"}
                or not isinstance(item.get("enabled"), bool)
                or "images" not in item
                or not isinstance(aliases, list)
                or len(aliases) > 30
                or any(
                    not isinstance(value, str) or len(value) > 80 for value in aliases
                )
                or not isinstance(images, list)
            ):
                raise ValueError(f"仓库角色配置无效：{identifier}")
            if item["enabled"] and not images:
                raise ValueError(f"启用角色缺少图片：{identifier}")
            for image in images:
                if not isinstance(image, dict) or not isinstance(image.get("sha256"), str) or not _DIGEST.fullmatch(image["sha256"]) or not isinstance(image.get("source_url"), str) or any(isinstance(image.get(key), bool) or not isinstance(image.get(key), int) or image[key] < 1 for key in ("width", "height", "bytes")):
                    raise ValueError(f"仓库角色图片配置无效：{identifier}")
                try:
                    _validate_image_url(image["source_url"])
                except ValueError as exc:
                    raise ValueError(f"仓库角色图片 URL 无效：{identifier}：{exc}") from exc

    def _compare(self, remote: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        if remote.get("unavailable"):
            return result
        with self.storage._lock:
            db = self.storage._connection()
            for kind, entities in (("character", remote["characters"]), ("pool", remote["pools"])):
                for identifier, source in entities.items():
                    source_json = catalog_source_json(source, kind)
                    baseline = db.execute("SELECT source_json FROM catalog_sync_baselines WHERE entity_kind=? AND entity_id=?", (kind, identifier)).fetchone()
                    if baseline and baseline["source_json"] == source_json:
                        continue
                    local, revision = self._local(db, kind, identifier)
                    if local is None:
                        status = "new"
                    elif not baseline:
                        status = "conflict"
                    else:
                        old = json.loads(baseline["source_json"])
                        status = "update" if self._core(old, kind) == self._core(local, kind) else "conflict"
                    if local and local.get("deleted_at") is not None:
                        status = "blocked"
                    changes = self._diff(local, source, kind)
                    result.append({"kind": kind, "id": identifier, "name": source["name"], "status": status,
                                   "changes": changes, "revision": revision, "source": source})
            for baseline in db.execute("SELECT entity_kind,entity_id,source_json FROM catalog_sync_baselines").fetchall():
                kind, identifier = baseline["entity_kind"], baseline["entity_id"]
                collection = remote["characters"] if kind == "character" else remote["pools"]
                if identifier not in collection:
                    old = json.loads(baseline["source_json"])
                    result.append({"kind": kind, "id": identifier, "name": old.get("name", identifier),
                                   "status": "removed", "changes": {"fields": {}, "note": "仓库已移除；本地保留"}})
        return result

    @staticmethod
    def _local(db: Any, kind: str, identifier: str) -> tuple[dict[str, Any] | None, int]:
        if kind == "character":
            row = db.execute("SELECT name,aliases_json,gender,enabled,revision,deleted_at,provenance_json FROM characters WHERE id=?", (identifier,)).fetchone()
            if not row:
                return None, 0
            images = [value[0] for value in db.execute("SELECT media_hash FROM character_images WHERE character_id=?", (identifier,)).fetchall()]
            return {"name": row["name"], "aliases": json.loads(row["aliases_json"]), "gender": row["gender"], "enabled": bool(row["enabled"]), "images": images, "provenance": json.loads(row["provenance_json"]), "deleted_at": row["deleted_at"]}, int(row["revision"])
        row = db.execute("SELECT name,mode,revision,deleted_at FROM pools WHERE id=?", (identifier,)).fetchone()
        if not row:
            return None, 0
        members = [value[0] for value in db.execute("SELECT character_id FROM pool_members WHERE pool_id=? ORDER BY character_id", (identifier,)).fetchall()]
        return {"name": row["name"], "mode": row["mode"], "character_ids": members, "deleted_at": row["deleted_at"]}, int(row["revision"])

    @staticmethod
    def _core(item: dict[str, Any], kind: str) -> dict[str, Any]:
        keys = ("name", "aliases", "gender", "enabled") if kind == "character" else ("name", "mode", "character_ids")
        core = {key: sorted(item.get(key, [])) if key == "character_ids" else item.get(key) for key in keys}
        if kind == "character":
            provenance = item.get("provenance", item)
            core["provenance"] = {key: provenance.get(key) for key in ("game", "gender_basis", "source_role_ids", "release_status", "material_status", "source_url", "verified_on") if key in item or key in provenance}
        return core

    @staticmethod
    def _diff(local: dict[str, Any] | None, source: dict[str, Any], kind: str) -> dict[str, Any]:
        if kind == "character":
            fields = {key: {"local": local.get(key) if local else None, "remote": source.get(key)}
                      for key in ("name", "aliases", "gender", "enabled") if not local or local.get(key) != source.get(key)}
            old_images = set(local.get("images", [])) if local else set()
            return {"fields": fields, "images_to_add": sum(image["sha256"] not in old_images for image in source["images"])}
        old = set(local.get("character_ids", [])) if local else set()
        new = set(source["character_ids"])
        return {"fields": {key: {"local": local.get(key) if local else None, "remote": source.get(key)}
                           for key in ("name", "mode") if not local or local.get(key) != source.get(key)},
                "members_to_add": sorted(new - old), "members_to_remove": sorted(old - new)}

    def _validate_dependencies(self, chosen: set[tuple[str, str]], options: dict[tuple[str, str], dict[str, Any]]) -> None:
        with self.storage._lock:
            db = self.storage._connection()
            for kind, identifier in chosen:
                if kind != "pool":
                    continue
                pool = options[(kind, identifier)]["source"]
                missing = []
                incompatible = []
                for character_id in pool["character_ids"]:
                    if ("character", character_id) in chosen:
                        gender = options[("character", character_id)]["source"]["gender"]
                        if gender not in ({"female", "unspecified"} if pool["mode"] == "wife" else {"male", "unspecified"}):
                            incompatible.append(character_id)
                        continue
                    row = db.execute("SELECT deleted_at,gender FROM characters WHERE id=?", (character_id,)).fetchone()
                    if row is None or row["deleted_at"] is not None:
                        missing.append(character_id)
                    elif row["gender"] not in ({"female", "unspecified"} if pool["mode"] == "wife" else {"male", "unspecified"}):
                        incompatible.append(character_id)
                if missing:
                    raise ValueError(f"角色池 {identifier} 缺少角色，请先勾选：{'、'.join(missing[:20])}")
                if incompatible:
                    raise ValueError(f"角色池 {identifier} 包含性别不匹配的角色：{'、'.join(incompatible[:20])}")

    def _missing_images(self, specs):
        with self.storage._lock:
            db = self.storage._connection()
            specs = {
                digest: spec
                for digest, spec in specs.items()
                if not (
                    row := db.execute(
                        "SELECT relative_path FROM media_blobs WHERE hash=?", (digest,)
                    ).fetchone()
                )
                or not (self.media_root / row["relative_path"]).is_file()
            }
        config, _ = self.storage.get_settings()
        return specs, config

    async def _download_images(
        self, selected: list[dict[str, Any]]
    ) -> dict[str, tuple[dict[str, Any], bytes, str, str]]:
        specs = {
            image["sha256"]: image
            for item in selected
            if item["kind"] == "character"
            for image in item["source"]["images"]
        }
        specs, config = await asyncio.to_thread(self._missing_images, specs)
        max_bytes = int(config["resources"]["image_max_bytes"])
        max_pixels = int(config["resources"]["image_max_pixels"])
        if sum(spec["bytes"] for spec in specs.values()) > 64 * 1024 * 1024:
            raise ValueError("所选图片总量超过 64 MiB，请分批同步。")
        if not specs:
            return {}
        if aiohttp is None:
            raise StorageError("同步图片需要安装 requirements.txt 中的 aiohttp。")
        timeout = aiohttp.ClientTimeout(total=30)
        results: dict[str, tuple[dict[str, Any], bytes, str, str]] = {}
        connector = aiohttp.TCPConnector(
            resolver=_PublicResolver(), use_dns_cache=False, limit=4
        )
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            raise_for_status=False,
            trust_env=False,
        ) as session:
            semaphore = asyncio.Semaphore(4)

            async def one(digest: str, spec: dict[str, Any]) -> None:
                async with semaphore:
                    payload, _final_url = await _fetch_limited(
                        session, spec["source_url"], max_bytes
                    )
                    if (
                        hashlib.sha256(payload).hexdigest() != digest
                        or len(payload) != spec["bytes"]
                    ):
                        raise ValueError(f"图片校验失败：{digest[:12]}")
                    width, height, extension, mime = await asyncio.to_thread(
                        _inspect_image, payload, max_pixels
                    )
                    if (width, height) != (spec["width"], spec["height"]):
                        raise ValueError(f"图片尺寸不符：{digest[:12]}")
                    results[digest] = (spec, payload, extension, mime)

            await asyncio.gather(*(one(digest, spec) for digest, spec in specs.items()))
        return results

    def _apply_character(self, db: Any, item: dict[str, Any], images: dict[str, tuple[dict[str, Any], bytes, str, str]], now: int) -> None:
        source = item["source"]
        old = db.execute("SELECT provenance_json FROM characters WHERE id=?", (item["id"],)).fetchone()
        provenance = json.loads(old["provenance_json"]) if old else {}
        provenance.update({key: source[key] for key in ("game", "gender_basis", "source_role_ids", "release_status", "material_status", "source_url", "verified_on") if key in source})
        db.execute("""INSERT INTO characters(id,name,aliases_json,gender,enabled,revision,provenance_json)
                      VALUES(?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases_json=excluded.aliases_json,
                      gender=excluded.gender,enabled=excluded.enabled,revision=characters.revision+1,provenance_json=excluded.provenance_json""",
                   (item["id"], source["name"], json.dumps(source.get("aliases", []), ensure_ascii=False), source["gender"], int(source["enabled"]), json.dumps(provenance, ensure_ascii=False)))
        existing = {row[0] for row in db.execute("SELECT media_hash FROM character_images WHERE character_id=?", (item["id"],)).fetchall()}
        ordinal = int(db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM character_images WHERE character_id=?", (item["id"],)).fetchone()[0])
        for image in source["images"]:
            digest = image["sha256"]
            if digest in images:
                spec, payload, extension, mime = images[digest]
                relative = Path("sha256") / f"{digest}.{extension}"
                _atomic_content_write(self.media_root / relative, payload)
                db.execute("""INSERT INTO media_blobs(hash,relative_path,mime_type,byte_size,width,height,source_url,verified_at) VALUES(?,?,?,?,?,?,?,?)
                              ON CONFLICT(hash) DO UPDATE SET relative_path=excluded.relative_path,mime_type=excluded.mime_type,
                              byte_size=excluded.byte_size,width=excluded.width,height=excluded.height,
                              source_url=excluded.source_url,verified_at=excluded.verified_at""",
                           (digest, relative.as_posix(), mime, len(payload), spec["width"], spec["height"], spec["source_url"], now))
            if digest not in existing:
                db.execute("INSERT INTO character_images(character_id,media_hash,ordinal) VALUES(?,?,?)", (item["id"], digest, ordinal))
                existing.add(digest)
                ordinal += 1

    @staticmethod
    def _apply_pool(db: Any, item: dict[str, Any]) -> None:
        source = item["source"]
        db.execute("""INSERT INTO pools(id,name,mode,builtin,revision) VALUES(?,?,?,1,1)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,mode=excluded.mode,revision=pools.revision+1""",
                   (item["id"], source["name"], source["mode"]))
        db.execute("DELETE FROM pool_members WHERE pool_id=?", (item["id"],))
        db.executemany("INSERT INTO pool_members(pool_id,character_id) VALUES(?,?)", [(item["id"], identifier) for identifier in source["character_ids"]])
