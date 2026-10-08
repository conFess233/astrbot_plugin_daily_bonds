"""按文件名匹配角色的批量图片草稿与逐角色确认导入。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from ..models import StorageError
from .catalog import _atomic_content_write, _inspect_image
from .catalog_admin import _ID, CatalogAdminService
from .storage import SQLiteStorage


class BatchImagesService:
    def __init__(self, storage: SQLiteStorage, data_root: Path) -> None:
        self.storage = storage
        self.stage_root = data_root / "staging" / "imports"
        self.catalog = CatalogAdminService(storage, data_root / "media")
        # ponytail: 串行化本机批量草稿；并行批次吞吐成为瓶颈后再按 job 加锁。
        self._lock = threading.RLock()

    @staticmethod
    def _settings(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("批量导入设置必须是对象。")
        result = {}
        for key in ("male_keywords", "female_keywords"):
            words = value.get(key, [])
            if not isinstance(words, list) or len(words) > 30:
                raise ValueError("检测关键词必须是最多 30 项的数组。")
            result[key] = list(
                dict.fromkeys(CatalogAdminService._text(w, "关键词", 80) for w in words)
            )
        for key in ("male_pool_id", "female_pool_id"):
            pool_id = value.get(key, "")
            if not isinstance(pool_id, str):
                raise TypeError("默认卡池 ID 必须是字符串。")
            result[key] = (
                CatalogAdminService._identifier(pool_id, "默认卡池 ID")
                if pool_id
                else ""
            )
        return result

    def create(
        self,
        *,
        actor: str,
        now: int,
        settings: dict[str, Any],
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        settings = self._settings(settings)
        self._validate_pools(settings)
        resources = limits or {}
        job = {
            "job_id": str(uuid.uuid4()),
            "batch_images": True,
            "settings": settings,
            "items": [],
            "created_at": now,
            "expires_at": now + 86400,
            "limits": {
                "entries": max(1, int(resources.get("import_max_entries", 5000))),
                "bytes": max(
                    1, int(resources.get("import_max_bytes", 100 * 1024 * 1024))
                ),
            },
        }
        with self.storage.transaction() as db:
            db.execute(
                "INSERT INTO maintenance_jobs(id,kind,actor,state,expected_revision,staged_manifest_hash,report_json,created_at,expires_at) VALUES(?,'import',?,'ready','batch_images','',?,?,?)",
                (
                    job["job_id"],
                    actor,
                    json.dumps(job, ensure_ascii=False),
                    now,
                    job["expires_at"],
                ),
            )
        return job

    def _validate_pools(self, settings: dict[str, Any]) -> None:
        with self.storage._lock:
            db = self.storage._connection()
            for gender, mode in (("male", "husband"), ("female", "wife")):
                if not settings[f"{gender}_pool_id"]:
                    continue
                row = db.execute(
                    "SELECT mode,deleted_at FROM pools WHERE id=?",
                    (settings[f"{gender}_pool_id"],),
                ).fetchone()
                if row is None or row["deleted_at"] is not None or row["mode"] != mode:
                    raise ValueError(f"默认{gender}卡池不存在或类型不匹配。")

    def get(self, job_id: str, *, actor: str, now: int) -> dict[str, Any]:
        if not isinstance(job_id, str):
            raise TypeError("作业 ID 无效。")
        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    "SELECT report_json,expires_at FROM maintenance_jobs WHERE id=? AND actor=? AND kind='import'",
                    (job_id, actor),
                )
                .fetchone()
            )
        if row is None or int(row["expires_at"]) <= now:
            raise StorageError("批量导入作业不存在或已过期。")
        job = json.loads(row["report_json"])
        if not job.get("batch_images"):
            raise StorageError("不是批量图片导入作业。")
        for item in job["items"]:
            item["errors"] = [
                error
                for error in item.get("errors", [])
                if error != "角色图片总数超过 20 张，请选择保留的图片"
            ]
        return job

    def refresh(self, job_id: str, *, actor: str, now: int) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id, actor=actor, now=now)
            self._overflow(job)
            self._save(job, actor)
            return job

    def get_image(
        self, job_id: str, row_id: str, *, actor: str, now: int
    ) -> tuple[Path, str] | None:
        job = self.get(job_id, actor=actor, now=now)
        item = next((r for r in job["items"] if r["row_id"] == row_id), None)
        if item is None or item.get("upload_error"):
            return None
        path = self._image_path(job_id, item)
        return (path, item["mime_type"]) if path.is_file() else None

    def _image_path(self, job_id: str, item: dict[str, Any]) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("hash", "")) or item.get(
            "extension"
        ) not in {"png", "jpg", "webp"}:
            raise StorageError("暂存图片元数据无效。")
        root = self.stage_root.resolve()
        path = (
            root / job_id / "images" / f"{item['hash']}.{item['extension']}"
        ).resolve()
        if root / job_id not in path.parents:
            raise StorageError("暂存图片路径超出本批次导入目录。")
        return path

    def record_failure(
        self,
        job_id: str,
        filename: str,
        error: str,
        *,
        actor: str,
        now: int,
        relative_path: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id, actor=actor, now=now)
            if len(job["items"]) >= job["limits"]["entries"]:
                raise ValueError("本批次图片数量超过限制。")
            item = {
                "row_id": str(uuid.uuid4()),
                "filename": self._filename(filename),
                "relative_path": self._relative_path(relative_path),
                "hash": "",
                "byte_size": 0,
                "status": "failed",
                "upload_error": True,
                "excluded": True,
            }
            self._assign(item, job, [])
            item["errors"] = [CatalogAdminService._text(error, "上传错误", 2000)]
            job["items"].append(item)
            self._save(job, actor)
            return job

    def list_jobs(self, *, actor: str, now: int) -> list[dict[str, Any]]:
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT report_json FROM maintenance_jobs WHERE actor=? AND kind='import' AND expires_at>? ORDER BY created_at DESC",
                    (actor, now),
                )
                .fetchall()
            )
        return [
            job
            for row in rows
            if (job := json.loads(row["report_json"])).get("batch_images")
        ]

    def _save(self, job: dict[str, Any], actor: str, db: Any = None) -> None:
        # ponytail: 每次重写草稿 JSON；千图批次频繁编辑成为瓶颈时再改为逐图表。
        if db is None:
            with self.storage.transaction() as connection:
                self._save(job, actor, connection)
            return
        db.execute(
            "UPDATE maintenance_jobs SET report_json=? WHERE id=? AND actor=?",
            (json.dumps(job, ensure_ascii=False), job["job_id"], actor),
        )

    def _characters(self) -> list[dict[str, Any]]:
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT id,name,aliases_json,gender,revision FROM characters WHERE deleted_at IS NULL"
                )
                .fetchall()
            )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "aliases": json.loads(row["aliases_json"]),
                "gender": row["gender"],
                "revision": row["revision"],
            }
            for row in rows
        ]

    @staticmethod
    def _filename(filename: str) -> str:
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 500
            or "\x00" in filename
        ):
            raise ValueError("文件名无效。")
        # 浏览器一般传 basename；保留冒号等合法角色 ID，仅去掉客户端路径。
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not basename or not Path(basename).stem:
            raise ValueError("文件名无效。")
        return basename

    @staticmethod
    def _detect(stem: str, settings: dict[str, Any]) -> tuple[str, str, list[str]]:
        hits = []
        for gender in ("male", "female"):
            for word in settings[f"{gender}_keywords"]:
                for match in re.finditer(re.escape(word), stem, re.IGNORECASE):
                    hits.append((match.start(), match.end(), gender))
        # female 包含 male 时只算较长词；同一跨度配置到两类仍属于冲突。
        hits = [
            h
            for h in hits
            if not any(
                o[0] <= h[0] and o[1] >= h[1] and o[1] - o[0] > h[1] - h[0]
                for o in hits
            )
        ]
        genders = {h[2] for h in hits}
        issues = (
            ["性别关键词冲突"]
            if len(genders) > 1
            else ["未识别性别，暂按男处理"]
            if not genders
            else []
        )
        gender = next(iter(genders)) if len(genders) == 1 else "male"
        clean = stem
        for start, end in sorted({(h[0], h[1]) for h in hits}, reverse=True):
            clean = clean[:start] + " " + clean[end:]
        clean = re.sub(r"[\s_.-]+\d+$", "", clean.strip(" _.-"))
        clean = re.sub(r"[\s_.-]+$", "", clean).strip(" _.-") or stem
        return gender, clean, issues

    @staticmethod
    def _match(text: str, characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [c for c in characters if c["id"] == text.removeprefix("#")]
        return ids or [
            c for c in characters if c["name"] == text or text in c["aliases"]
        ]

    def _assign(
        self,
        item: dict[str, Any],
        job: dict[str, Any],
        characters: list[dict[str, Any]],
    ) -> None:
        stem = Path(item["filename"]).stem
        gender, clean, issues = self._detect(stem, job["settings"])
        # 先只剥离尾标记，避免角色 ID 本身含 male/女等关键词被拆坏。
        suffix_words = "|".join(
            re.escape(w)
            for key in ("male_keywords", "female_keywords")
            for w in job["settings"][key]
        )
        suffix = (
            r"[\s_.-]+(?:\d+"
            + ("|(?i:" + suffix_words + ")" if suffix_words else "")
            + ")$"
        )
        without_suffix = stem
        while (shorter := re.sub(suffix, "", without_suffix)) != without_suffix:
            without_suffix = shorter
        candidates = (
            self._match(stem, characters)
            or self._match(without_suffix, characters)
            or self._match(clean, characters)
        )
        item.update(
            {
                "candidates": [{"id": c["id"], "name": c["name"]} for c in candidates],
                "manual_approved": False,
                "edited": False,
                "errors": [],
            }
        )
        if len(candidates) == 1:
            self._existing(item, candidates[0]["id"])
            item["needs_manual"] = False
            item["warnings"] = []
        else:
            same = next(
                (
                    r
                    for r in job["items"]
                    if not r.get("existing")
                    and r.get("name") == clean
                    and r.get("gender") == gender
                    and r["row_id"] != item["row_id"]
                ),
                None,
            )
            item.update(
                {
                    "target_id": same["target_id"]
                    if same
                    else item["target_id"]
                    if item.get("target_id")
                    and not item.get("existing")
                    and item.get("name") == clean
                    else "char_" + uuid.uuid4().hex[:12],
                    "existing": False,
                    "name": clean,
                    "gender": gender,
                    "pool_ids": [job["settings"][f"{gender}_pool_id"]]
                    if job["settings"][f"{gender}_pool_id"]
                    else [],
                    "enabled": True,
                    "expected_revision": 0,
                    "needs_manual": True,
                    "warnings": (
                        ["角色名称匹配多个候选"] if candidates else ["未匹配到已有角色"]
                    )
                    + issues,
                }
            )

    def _existing(self, item: dict[str, Any], target_id: str) -> None:
        character = self.catalog.get_character(target_id)
        if character is None:
            raise ValueError("选中的已有角色不存在。")
        item.update(
            {
                "target_id": character["id"],
                "existing": True,
                "name": character["name"],
                "gender": character["gender"],
                "pool_ids": [p["id"] for p in character["pools"]],
                "expected_revision": character["revision"],
                "enabled": character["enabled"],
                "needs_manual": False,
                "warnings": [],
            }
        )

    @staticmethod
    def _relative_path(value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) > 2000
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("文件夹路径无效。")
        parts = value.replace("\\", "/").split("/")
        if value and (
            any(p in {"", ".", ".."} for p in parts)
            or value.startswith("/")
            or re.match(r"^[A-Za-z]:", value)
        ):
            raise ValueError("文件夹路径必须是相对路径。")
        return "/".join(parts) if value else ""

    def _overflow(self, job: dict[str, Any], targets: set[str] | None = None) -> None:
        """标记重复来源；仅同角色且无新增卡池时默认跳过，可改关联后重新参与。"""
        characters = {}
        seen = {}
        planned_pools = {}
        with self.storage._lock:
            db = self.storage._connection()
            library = {}
            stored_hashes = {
                str(row[0]) for row in db.execute("SELECT hash FROM media_blobs")
            }
            for row in db.execute(
                "SELECT ci.media_hash,c.id,c.name FROM character_images ci JOIN characters c ON c.id=ci.character_id WHERE c.deleted_at IS NULL"
            ):
                library.setdefault(row["media_hash"], []).append(
                    {"id": row["id"], "name": row["name"]}
                )
        for item in job["items"]:
            if item.get("upload_error"):
                continue
            item["errors"] = [
                e
                for e in item.get("errors", [])
                if e != "角色图片总数超过 20 张，请选择保留的图片"
            ]
            if item.get("duplicate_skip"):
                item.update(status="pending", excluded=False, duplicate_skip=False)
            target = item.get("target_id", "")
            item["duplicate_sources"] = [
                c["name"] for c in library.get(item["hash"], [])
            ]
            if item["hash"] in stored_hashes and not item["duplicate_sources"]:
                item["duplicate_sources"].append("图库已存素材（未关联当前角色）")
            key = (target, item["hash"])
            duplicate = (
                any(c["id"] == target for c in library.get(item["hash"], []))
                or key in seen
            )
            item["duplicate_image"] = duplicate
            if key in seen:
                item["duplicate_sources"].append("本批：" + seen[key])
            if item["status"] in {"pending", "failed"} and not item.get("excluded"):
                if target not in characters:
                    characters[target] = (
                        self.catalog.get_character(target)
                        if _ID.fullmatch(target)
                        else None
                    )
                current = characters[target]
                pools = {p["id"] for p in current["pools"]} if current else set()
                planned = planned_pools.setdefault(target, pools)
                adds_pools = bool(set(item.get("pool_ids", [])) - planned)
                if duplicate and not adds_pools:
                    item.update(status="skipped", excluded=True, duplicate_skip=True)
                else:
                    seen[key] = item["filename"]
                    planned.update(item.get("pool_ids", []))

    def add_image(
        self,
        job_id: str,
        filename: str,
        payload: bytes,
        *,
        actor: str,
        now: int,
        relative_path: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id, actor=actor, now=now)
            filename = self._filename(filename)
            if not payload or len(payload) > 12 * 1024 * 1024:
                raise ValueError("图片文件为空或超过 12 MiB。")
            if (
                len(job["items"]) >= job["limits"]["entries"]
                or sum(r["byte_size"] for r in job["items"]) + len(payload)
                > job["limits"]["bytes"]
            ):
                raise ValueError("本批次图片数量或总字节数超过限制。")
            width, height, ext, mime = _inspect_image(payload, 40_000_000)
            digest = hashlib.sha256(payload).hexdigest()
            item = {
                "row_id": str(uuid.uuid4()),
                "filename": filename,
                "relative_path": self._relative_path(relative_path),
                "hash": digest,
                "extension": ext,
                "width": width,
                "height": height,
                "mime_type": mime,
                "byte_size": len(payload),
                "status": "pending",
                "excluded": False,
            }
            self._assign(item, job, self._characters())
            _atomic_content_write(
                self.stage_root / job_id / "images" / f"{digest}.{ext}", payload
            )
            job["items"].append(item)
            self._overflow(job, {item["target_id"]})
            self._save(job, actor)
            return job

    def update(
        self,
        job_id: str,
        *,
        actor: str,
        now: int,
        items: list[dict[str, Any]] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id, actor=actor, now=now)
            if settings is not None:
                normalized = self._settings(settings)
                self._validate_pools(normalized)
                job["settings"] = normalized
                characters = self._characters()
                for item in job["items"]:
                    if (
                        item["status"] == "pending"
                        and not item.get("upload_error")
                        and not item.get("excluded")
                        and not item.get("edited")
                    ):
                        self._assign(item, job, characters)
            if items is not None:
                if not isinstance(items, list) or len(items) > job["limits"]["entries"]:
                    raise ValueError("图片修改列表无效。")
                lookup = {r["row_id"]: r for r in job["items"]}
                for patch in items:
                    if not isinstance(patch, dict) or patch.get("row_id") not in lookup:
                        raise ValueError("图片行不存在。")
                    item = lookup[patch["row_id"]]
                    if item["status"] == "imported":
                        raise ValueError("已经导入的图片不能修改。")
                    if item.get("upload_error"):
                        if "relative_path" in patch:
                            item["relative_path"] = self._relative_path(
                                patch["relative_path"]
                            )
                        if set(patch) <= {"row_id", "relative_path"}:
                            continue
                        if patch.get("status") != "skipped":
                            raise ValueError("上传失败的图片只能跳过，请重新上传。")
                        item["status"] = "skipped"
                        continue
                    if any(
                        key in patch and patch[key] != item.get(key)
                        for key in (
                            "target_id",
                            "name",
                            "gender",
                            "pool_ids",
                            "enabled",
                        )
                    ):
                        item["manual_approved"] = False
                        item["needs_manual"] = True
                    if "existing" in patch and not isinstance(patch["existing"], bool):
                        raise TypeError("existing 必须是布尔值。")
                    if "target_id" in patch:
                        target = patch["target_id"]
                        if not isinstance(target, str) or len(target) > 128:
                            raise ValueError(
                                "角色 ID 草稿必须是最多 128 字符的字符串。"
                            )
                        if patch.get("existing", item["existing"]):
                            CatalogAdminService._identifier(target, "角色 ID")
                        found = (
                            self.catalog.get_character(target)
                            if _ID.fullmatch(target)
                            else None
                        )
                        if patch.get("existing") is True and found is None:
                            raise ValueError("选中的已有角色不存在。")
                        if patch.get("existing") is False and found is not None:
                            raise ValueError("新角色 ID 已被占用。")
                        if found:
                            self._existing(item, target)
                        else:
                            item.update(
                                {
                                    "target_id": target,
                                    "existing": False,
                                    "expected_revision": 0,
                                }
                            )
                    if item["existing"]:
                        if any(
                            key in patch and patch[key] != item[key]
                            for key in ("name", "gender", "enabled")
                        ):
                            raise ValueError("已有角色不能修改名称、性别或启用状态。")
                    else:
                        for key in ("name", "gender", "pool_ids", "enabled"):
                            if key in patch:
                                item[key] = patch[key]
                        self._validate_draft(item)
                        if (
                            patch.get("manual_approved", item["manual_approved"])
                            is True
                        ):
                            self._validate_new(item)
                    if item["existing"] and "pool_ids" in patch:
                        additions = CatalogAdminService._string_list(
                            patch["pool_ids"],
                            "追加卡池",
                            maximum=100,
                            item_max=64,
                            unique=True,
                        )
                        item["pool_ids"] = list(
                            dict.fromkeys([*item["pool_ids"], *additions])
                        )
                        if item["pool_ids"]:
                            self._validate_new(item)
                    for key in ("manual_approved", "excluded"):
                        if key in patch:
                            if not isinstance(patch[key], bool):
                                raise ValueError(f"{key} 必须是布尔值。")
                            item[key] = patch[key]
                    if "status" in patch:
                        if patch["status"] not in {"pending", "skipped"}:
                            raise ValueError("只能设置待处理或跳过状态。")
                        item["status"] = patch["status"]
                        item["excluded"] = patch["status"] == "skipped"
                        item["duplicate_skip"] = False
                    if "relative_path" in patch:
                        item["relative_path"] = self._relative_path(
                            patch["relative_path"]
                        )
                    item["edited"] = True
                    item["errors"] = []
            self._overflow(job)
            self._save(job, actor)
            return job

    @staticmethod
    def _validate_draft(item: dict[str, Any]) -> None:
        for key, maximum in (("target_id", 128), ("name", 120)):
            if not isinstance(item[key], str) or len(item[key]) > maximum:
                raise ValueError(f"{key} 草稿必须是最多 {maximum} 字符的字符串。")
        if not isinstance(item.get("enabled"), bool):
            raise TypeError("角色启用状态无效。")
        if item["gender"] not in {"female", "male", "unspecified"}:
            raise ValueError("角色性别无效。")
        item["pool_ids"] = CatalogAdminService._string_list(
            item["pool_ids"], "卡池", maximum=100, item_max=64, unique=True
        )
        warning = "新角色 ID 或名称尚未填写完整"
        item["warnings"] = [w for w in item.get("warnings", []) if w != warning]
        if not _ID.fullmatch(item["target_id"]) or not item["name"].strip():
            item["warnings"].append(warning)
            item["needs_manual"] = True

    def _validate_new(self, item: dict[str, Any]) -> None:
        CatalogAdminService._identifier(item["target_id"], "角色 ID")
        item["name"] = CatalogAdminService._text(item["name"], "角色名称", 120)
        if not isinstance(item.get("enabled"), bool):
            raise TypeError("角色启用状态无效。")
        if item["gender"] not in {"female", "male", "unspecified"}:
            raise ValueError("角色性别无效。")
        item["pool_ids"] = CatalogAdminService._string_list(
            item["pool_ids"], "卡池", maximum=100, item_max=64, unique=True
        )
        if not item["pool_ids"]:
            raise ValueError("新角色必须选择卡池。")
        with self.storage._lock:
            db = self.storage._connection()
            for pool_id in item["pool_ids"]:
                row = db.execute(
                    "SELECT mode,deleted_at FROM pools WHERE id=?", (pool_id,)
                ).fetchone()
                expected = {"female": "wife", "male": "husband"}.get(item["gender"])
                if (
                    row is None
                    or row["deleted_at"] is not None
                    or (expected and row["mode"] != expected)
                ):
                    raise ValueError("角色池不存在或与性别不匹配。")

    def commit(
        self,
        job_id: str,
        row_ids: list[str],
        request_id: str,
        *,
        actor: str,
        now: int,
        relative_path: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id, actor=actor, now=now)
            request_id = CatalogAdminService._request_id(request_id)
            row_ids = CatalogAdminService._string_list(
                row_ids,
                "确认图片",
                maximum=job["limits"]["entries"],
                item_max=64,
                unique=True,
            )
            if not row_ids:
                raise ValueError("请选择要确认导入的图片。")
            lookup = {r["row_id"]: r for r in job["items"]}
            if any(row_id not in lookup for row_id in row_ids):
                raise ValueError("所选图片不存在。")
            self._overflow(job)
            fields = (
                "target_id",
                "hash",
                "name",
                "gender",
                "pool_ids",
                "enabled",
                "existing",
                "expected_revision",
                "manual_approved",
                "excluded",
            )
            selected = [
                {"row_id": row_id, **{key: lookup[row_id].get(key) for key in fields}}
                for row_id in sorted(row_ids)
            ]
            digest = hashlib.sha256(
                json.dumps(
                    {"job_id": job_id, "items": selected}, sort_keys=True
                ).encode()
            ).hexdigest()
            with self.storage._lock:
                prior = (
                    self.storage._connection()
                    .execute(
                        "SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint='batch-images/commit' AND request_id=?",
                        (actor, request_id),
                    )
                    .fetchone()
                )
            if prior:
                if prior["request_hash"] != digest:
                    raise StorageError("request_id 已用于不同导入提交。")
                return json.loads(prior["response_json"])
            groups: dict[str, list[dict[str, Any]]] = {}
            skipped_duplicates = 0
            for row_id in row_ids:
                item = lookup[row_id]
                if item.get("duplicate_skip") and item["status"] == "skipped":
                    skipped_duplicates += 1
                    continue
                if item["status"] != "imported":
                    groups.setdefault(item["target_id"], []).append(item)
            imported, failed = [], []
            for target, rows in groups.items():
                try:
                    if any(
                        r.get("upload_error")
                        or r.get("excluded")
                        or r.get("errors")
                        or (r["needs_manual"] and not r["manual_approved"])
                        for r in rows
                    ):
                        raise ValueError("待人工调整的图片必须先审核且不含错误。")
                    pending_group = [
                        r
                        for r in job["items"]
                        if r["target_id"] == target
                        and r["status"] != "imported"
                        and not r.get("excluded")
                    ]
                    if any(r.get("errors") for r in pending_group):
                        raise ValueError("角色图片组含有错误，请先修正或排除失败项。")
                    result = self._commit_group(job, rows, actor, request_id, now)
                    imported.append(result)
                except (ValueError, TypeError, StorageError, OSError) as exc:
                    message = str(exc)
                    for row in rows:
                        row["needs_manual"] = True
                        row["errors"] = [message]
                    failed.append(
                        {
                            "id": target,
                            "row_ids": [r["row_id"] for r in rows],
                            "error": message,
                        }
                    )
                    self._save(job, actor)
            self._overflow(job)
            self._save(job, actor)
            response = {
                "job": job,
                "imported": imported,
                "failed": failed,
                "skipped_duplicates": skipped_duplicates,
            }
            with self.storage.transaction() as db:
                db.execute(
                    "INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,'batch-images/commit',?,?,?,?)",
                    (
                        actor,
                        request_id,
                        digest,
                        json.dumps(response, ensure_ascii=False),
                        now,
                    ),
                )
            return response

    def _commit_group(
        self,
        job: dict[str, Any],
        rows: list[dict[str, Any]],
        actor: str,
        request_id: str,
        now: int,
    ) -> dict[str, Any]:
        first = rows[0]
        if any(
            (r["existing"], r["name"], r["gender"], r["enabled"])
            != (
                first["existing"],
                first["name"],
                first["gender"],
                first["enabled"],
            )
            for r in rows
        ):
            raise ValueError("同一角色图片的档案设置不一致，请统一调整。")
        payloads = []
        for item in rows:
            path = self._image_path(job["job_id"], item)
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != item["hash"]:
                raise StorageError("暂存图片内容已变化，请重新上传。")
            payloads.append((item, payload))
        with self.storage.transaction() as db:
            current = self.catalog.get_character(first["target_id"])
            revision = current["revision"] if current else 0
            if any(r["expected_revision"] != revision for r in rows):
                raise StorageError("角色版本已变化，请重新选择角色并审核。")
            if first["existing"] != bool(current):
                raise StorageError("角色已被删除或 ID 已被占用，请重新选择角色。")
            if current:
                hashes = [r["media_hash"] for r in current["images"]]
                data = {
                    "id": current["id"],
                    "name": current["name"],
                    "gender": current["gender"],
                    "enabled": current["enabled"],
                    "aliases": current["aliases"],
                    "pool_ids": [p["id"] for p in current["pools"]],
                    "provenance": current["provenance"],
                }
            else:
                self._validate_new(first)
                hashes = []
                data = {
                    "id": first["target_id"],
                    "name": first["name"],
                    "gender": first["gender"],
                    "enabled": first["enabled"],
                    "aliases": [],
                    "pool_ids": first["pool_ids"],
                    "provenance": {"batch_images": job["job_id"]},
                }
            data["pool_ids"] = list(
                dict.fromkeys(
                    [*data["pool_ids"], *(p for r in rows for p in r["pool_ids"])]
                )
            )
            if data["pool_ids"] or not current:
                self._validate_new({**first, "pool_ids": data["pool_ids"]})
            previous_hashes = set(hashes)
            hashes = list(dict.fromkeys([*hashes, *(r["hash"] for r in rows)]))
            added_images = len(set(hashes) - previous_hashes)
            for item, payload in payloads:
                relative = Path("sha256") / f"{item['hash']}.{item['extension']}"
                _atomic_content_write(self.catalog.media_root / relative, payload)
                db.execute(
                    "INSERT OR IGNORE INTO media_blobs(hash,relative_path,mime_type,byte_size,width,height,source_url,verified_at) VALUES(?,?,?,?,?,?,NULL,?)",
                    (
                        item["hash"],
                        str(relative),
                        item["mime_type"],
                        item["byte_size"],
                        item["width"],
                        item["height"],
                        now,
                    ),
                )
            data.update({"expected_revision": revision, "image_hashes": hashes})
            result = self.catalog._save_character(db, data, actor, request_id, now)
            for row in rows:
                row.update({"status": "imported", "errors": []})
            for row in job["items"]:
                if (
                    row["target_id"] == first["target_id"]
                    and row["status"] != "imported"
                ):
                    row["existing"] = True
                    row["expected_revision"] = result["revision"]
            self._save(job, actor, db)
        return {
            "id": first["target_id"],
            "name": data["name"],
            "row_ids": [r["row_id"] for r in rows],
            "images": added_images,
            "duplicate_images": len(rows) - added_images,
            "pool_ids": data["pool_ids"],
        }
