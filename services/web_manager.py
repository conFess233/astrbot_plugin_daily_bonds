"""Authenticated AstrBot Plugin Pages APIs for configuration and overview."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from PIL import Image, ImageOps

from ..models import ConfigurationError, StorageError
from .admin_corrections import AdminCorrectionService
from .admin_queries import AdminQueryService
from .batch_images import BatchImagesService
from .catalog_admin import CatalogAdminService
from .catalog_import import CatalogImportService
from .catalog_sync import CatalogSyncService
from .host_config import HostConfigBridge
from .maintenance import MaintenanceService
from .message_templates import template_fields
from .settings import effective_config, validate_config
from .storage import SQLiteStorage
from .fonts import available_fonts, font_preview

PLUGIN_NAME = "astrbot_plugin_daily_bonds"


class WebManager:
    """Register authenticated, revision-checked management APIs."""

    def __init__(
        self, context: Any, storage: SQLiteStorage, host_config: HostConfigBridge
    ) -> None:
        self.context = context
        self.storage = storage
        self.catalog = CatalogAdminService(
            storage, storage.database_path.parent / "media"
        )
        self.queries = AdminQueryService(storage)
        self.corrections = AdminCorrectionService(storage)
        self.imports = CatalogImportService(storage, storage.database_path.parent)
        self.batch_images = BatchImagesService(storage, storage.database_path.parent)
        self._batch_previews = asyncio.Semaphore(2)
        self.catalog_sync = CatalogSyncService(storage)
        self.maintenance = MaintenanceService(storage, storage.database_path.parent)
        self.runtime_reset = None
        self.host_config = host_config

    def register(self) -> None:
        routes = (
            ("fonts", self.fonts, ["GET"], "List server fonts"),
            ("fonts/preview", self.preview_font, ["GET"], "Preview image font"),
            ("overview", self.overview, ["GET"], "Today Bonds overview"),
            ("scopes", self.scopes, ["GET"], "Today Bonds scopes"),
            ("config", self.get_config, ["GET"], "Read Today Bonds configuration"),
            (
                "config/preview",
                self.preview_config,
                ["POST"],
                "Validate a configuration draft",
            ),
            ("config/save", self.save_config, ["POST"], "Save a configuration draft"),
            ("characters", self.characters, ["GET"], "List catalog characters"),
            ("character", self.character, ["GET"], "Read catalog character by ID"),
            (
                "characters/<character_id>",
                self.character,
                ["GET"],
                "Read catalog character",
            ),
            (
                "characters/save",
                self.save_character,
                ["POST"],
                "Create or update catalog character",
            ),
            (
                "characters/delete-preview",
                self.character_delete_preview,
                ["POST"],
                "Preview character deletion",
            ),
            (
                "characters/delete",
                self.character_delete,
                ["POST"],
                "Soft-delete catalog character",
            ),
            ("pools", self.pools, ["GET"], "List catalog pools"),
            ("pools/save", self.save_pool, ["POST"], "Create or update catalog pool"),
            (
                "pools/delete-preview",
                self.pool_delete_preview,
                ["POST"],
                "Preview pool deletion",
            ),
            ("pools/delete", self.pool_delete, ["POST"], "Soft-delete catalog pool"),
            (
                "media/upload",
                self.upload_media,
                ["POST"],
                "Upload and validate catalog image",
            ),
            (
                "media/preview/<media_hash>",
                self.preview_media,
                ["GET"],
                "Preview protected catalog image",
            ),
            (
                "media/<media_hash>",
                self.get_media,
                ["GET"],
                "Read protected catalog image",
            ),
            ("relationships", self.relationships, ["GET"], "List scoped relationships"),
            (
                "users/<user_id>/quota",
                self.user_quota,
                ["GET"],
                "Read scoped user quota",
            ),
            (
                "statistics/intimacy",
                self.intimacy,
                ["GET"],
                "Read directed intimacy scores",
            ),
            (
                "statistics/activity",
                self.activity,
                ["GET"],
                "Read activity window and coverage",
            ),
            ("invites", self.invites, ["GET"], "List scoped gift invitations"),
            ("history", self.history, ["GET"], "List scoped operation history"),
            (
                "admin/preview",
                self.admin_preview,
                ["POST"],
                "Preview an audited administrative correction",
            ),
            (
                "admin/commit",
                self.admin_commit,
                ["POST"],
                "Commit a preflighted administrative correction",
            ),
            (
                "imports/preview",
                self.import_preview,
                ["POST"],
                "Validate a JSON or ZIP catalog import",
            ),
            (
                "imports/commit",
                self.import_commit,
                ["POST"],
                "Commit selected valid catalog items",
            ),
            (
                "batch-images",
                self.batch_images_get,
                ["GET"],
                "Read owned batch image jobs and defaults",
            ),
            (
                "batch-images/create",
                self.batch_images_create,
                ["POST"],
                "Create a batch image draft",
            ),
            (
                "batch-images/update",
                self.batch_images_update,
                ["POST"],
                "Save editable image draft rows",
            ),
            (
                "batch-images/commit",
                self.batch_images_commit,
                ["POST"],
                "Commit selected image draft rows",
            ),
            (
                "batch-images/failure",
                self.batch_images_failure,
                ["POST"],
                "Record an individual upload error",
            ),
            (
                "batch-images/defaults",
                self.batch_images_defaults,
                ["POST"],
                "Save global batch detection defaults",
            ),
            (
                "batch-images/<job_id>/upload",
                self.batch_images_upload,
                ["POST"],
                "Upload one staged batch image",
            ),
            (
                "batch-images/<job_id>/preview/<row_id>",
                self.batch_images_preview,
                ["GET"],
                "Read protected staged thumbnail",
            ),
            (
                "catalog-sync/check",
                self.catalog_sync_check,
                ["POST"],
                "Check official catalog updates",
            ),
            (
                "catalog-sync/commit",
                self.catalog_sync_commit,
                ["POST"],
                "Apply selected official catalog updates",
            ),
            ("jobs/<job_id>", self.get_job, ["GET"], "Read an owned maintenance job"),
            (
                "exports/create",
                self.create_export,
                ["POST"],
                "Create an owned catalog role-pack export",
            ),
            (
                "backups/create",
                self.create_backup,
                ["POST"],
                "Create a consistent complete backup",
            ),
            (
                "downloads/<job_id>",
                self.download_job,
                ["GET"],
                "Download an owned export or backup",
            ),
            (
                "restore/preview",
                self.restore_preview,
                ["POST"],
                "Validate a full backup before restore",
            ),
            (
                "restore/commit",
                self.restore_commit,
                ["POST"],
                "Restore a confirmed full backup",
            ),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", handler, methods, description
            )

    async def batch_images_get(self):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            job_id = request.query.get("job_id")
            if job_id:
                data = await asyncio.to_thread(
                    self.batch_images.get, job_id, actor=actor, now=int(time.time())
                )
            else:
                data = await asyncio.to_thread(self._batch_options, actor)
            return json_response({"ok": True, "data": data})
        except (ValueError, TypeError, StorageError) as exc:
            return self._error("BATCH_UNAVAILABLE", str(exc), 409)

    def _batch_options(self, actor: str) -> dict[str, Any]:
        config, revision = self.storage.get_settings()
        jobs = self.batch_images.list_jobs(actor=actor, now=int(time.time()))
        summaries = [
            {
                "job_id": job["job_id"],
                "created_at": job["created_at"],
                "expires_at": job["expires_at"],
                "count": len(job["items"]),
            }
            for job in jobs
            if any(item["status"] in {"pending", "failed"} for item in job["items"])
        ]
        return {
            "settings": config["batch_import"],
            "revision": revision,
            "jobs": summaries,
            "pools": self.catalog.list_pools(),
            "limits": config["resources"],
        }

    async def batch_images_create(self):
        return await self._batch_change("create")

    async def batch_images_update(self):
        return await self._batch_change("update")

    async def batch_images_commit(self):
        return await self._batch_change("commit")

    async def fonts(self):
        actor, denied = self._authorize()
        if denied:
            return denied
        fonts = await asyncio.to_thread(available_fonts)
        return json_response(
            {"ok": True, "data": [{"id": f["id"], "name": f["name"]} for f in fonts]}
        )

    async def preview_font(self):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(
                font_preview, request.query.get("font_id", "auto")
            )
            return json_response({"ok": True, "data": data})
        except (ValueError, OSError) as exc:
            return self._error(
                "FONT_UNAVAILABLE", f"字体预览失败：{exc}", 400
            )

    async def batch_images_failure(self):
        return await self._batch_change("record_failure")

    async def _batch_change(self, method: str):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError("请求内容必须是 JSON 对象。")
            if method == "create":
                config, _revision = await asyncio.to_thread(self.storage.get_settings)
                args = {
                    "settings": payload.get("settings", config["batch_import"]),
                    "limits": config["resources"],
                }
            elif method == "update":
                args = {
                    "job_id": payload.get("job_id"),
                    "items": payload.get("items"),
                    "settings": payload.get("settings"),
                }
            elif method == "commit":
                if payload.get("confirm") is not True:
                    raise ValueError("请核对预览后确认导入。")
                args = {
                    "job_id": payload.get("job_id"),
                    "row_ids": payload.get("row_ids"),
                    "request_id": payload.get("request_id"),
                }
            else:
                args = {
                    "job_id": payload.get("job_id"),
                    "filename": payload.get("filename"),
                    "relative_path": payload.get("relative_path", ""),
                    "error": payload.get("error"),
                }
            data = await asyncio.to_thread(
                getattr(self.batch_images, method),
                **args,
                actor=actor,
                now=int(time.time()),
            )
            if method == "record_failure":
                data = {"job_id": data["job_id"], "item": data["items"][-1]}
            return json_response({"ok": True, "data": data})
        except (ValueError, TypeError) as exc:
            return self._error("BATCH_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("BATCH_CONFLICT", str(exc), 409)
        except Exception:
            logger.exception("今日姻缘：批量图片导入操作失败。")
            return self._error("BATCH_FAILED", "批量导入操作失败，草稿仍保留。", 500)

    async def batch_images_upload(self, job_id: str):
        actor, denied = self._authorize()
        if denied:
            return denied
        temporary = None
        filename = "未命名图片"
        relative_path = request.query.get("relative_path", "")
        try:
            await asyncio.to_thread(
                self.batch_images.get, job_id, actor=actor, now=int(time.time())
            )
            files = await request.files()
            upload = files.get("file")
            if upload is None or not callable(getattr(upload, "save", None)):
                raise ValueError("请选择图片文件。")
            filename = getattr(upload, "filename", None) or filename
            with tempfile.NamedTemporaryFile(
                prefix="batch-image-", suffix=".upload", delete=False
            ) as stream:
                temporary = stream.name
            await upload.save(temporary)
            payload = await asyncio.to_thread(_read_batch_upload, temporary)
            data = await asyncio.to_thread(
                self.batch_images.add_image,
                job_id,
                filename,
                payload,
                relative_path=relative_path,
                actor=actor,
                now=int(time.time()),
            )
            return json_response(
                {
                    "ok": True,
                    "data": {"job_id": data["job_id"], "item": data["items"][-1]},
                }
            )
        except (
            ValueError,
            TypeError,
            StorageError,
            OSError,
            Image.DecompressionBombError,
        ) as exc:
            try:
                data = await asyncio.to_thread(
                    self.batch_images.record_failure,
                    job_id,
                    filename,
                    str(exc),
                    relative_path=relative_path,
                    actor=actor,
                    now=int(time.time()),
                )
                return json_response(
                    {
                        "ok": True,
                        "data": {"job_id": data["job_id"], "item": data["items"][-1]},
                    }
                )
            except (ValueError, TypeError) as failure:
                return self._error("BATCH_UPLOAD_INVALID", str(failure), 400)
            except StorageError as failure:
                return self._error("BATCH_CONFLICT", str(failure), 409)
        except Exception:
            logger.exception("今日姻缘：批量图片上传失败。")
            return self._error(
                "BATCH_UPLOAD_FAILED", "上传失败，请重新选择该图片；其他条目保留。", 500
            )
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    async def batch_images_preview(self, job_id: str, row_id: str):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            image = await asyncio.to_thread(
                self.batch_images.get_image,
                job_id,
                row_id,
                actor=actor,
                now=int(time.time()),
            )
            if image is None:
                return self._error(
                    "BATCH_IMAGE_MISSING", "暂存图片不存在或已过期。", 404
                )
            async with self._batch_previews:
                src = await asyncio.to_thread(_batch_thumbnail, image[0])
            return json_response({"ok": True, "data": {"src": src}})
        except (
            ValueError,
            TypeError,
            StorageError,
            OSError,
            Image.DecompressionBombError,
        ) as exc:
            return self._error("BATCH_PREVIEW_FAILED", str(exc), 400)

    async def batch_images_defaults(self):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError("默认检测设置必须是 JSON 对象。")
            data = await asyncio.to_thread(self._save_batch_defaults, payload, actor)
            if not data.pop("host_synced"):
                return self._error(
                    "BATCH_DEFAULTS_SYNC_FAILED",
                    "默认设置已写入运行库，但宿主同步失败；请重试本次保存。",
                    503,
                )
            return json_response({"ok": True, "data": data})
        except (ValueError, TypeError, ConfigurationError) as exc:
            return self._error("BATCH_DEFAULTS_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("BATCH_DEFAULTS_CONFLICT", str(exc), 409)
        except Exception:
            logger.exception("今日姻缘：保存全局检测设置失败。")
            return self._error(
                "BATCH_DEFAULTS_FAILED", "保存检测默认值失败，请刷新检查当前配置。", 500
            )

    def _save_batch_defaults(
        self, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        settings = self.batch_images._settings(payload.get("settings"))
        self.batch_images._validate_pools(settings)
        revision = payload.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("配置版本必须是非负整数。")
        request_id = self.catalog._request_id(payload.get("request_id"))
        digest = hashlib.sha256(
            json.dumps(settings, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.storage._lock:
            config, _revision = self.storage.get_settings()
            config["batch_import"] = settings
            validate_config(config)
            self.storage.save_settings(
                None,
                config,
                expected_revision=revision,
                updated_by=actor,
                now=int(time.time()),
                request_id=request_id,
                request_hash=digest,
            )
            current, revision = self.storage.get_settings()
            try:
                self.host_config.mirror(current, revision)
                synced = True
            except Exception:
                logger.exception("今日姻缘：全局检测设置已保存，但宿主同步失败。")
                synced = False
        return {
            "settings": current["batch_import"],
            "revision": revision,
            "host_synced": synced,
        }

    async def overview(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._query_scope_id)
        except ValueError as exc:
            return self._error("INVALID_SCOPE", str(exc), 400)
        return await asyncio.to_thread(self._overview, scope_id)

    def _overview(self, scope_id: int | None):
        with self.storage._lock:
            db = self.storage._connection()
            scope_where = " WHERE scope_id=?" if scope_id is not None else ""
            scope_args = (scope_id,) if scope_id is not None else ()
            scopes = (
                1
                if scope_id is not None
                else int(db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0])
            )
            active = int(
                db.execute(
                    f"SELECT COUNT(*) FROM relationships{scope_where}{' AND' if scope_where else ' WHERE'} state='active'",
                    scope_args,
                ).fetchone()[0]
            )
            pending = int(
                db.execute(
                    f"SELECT COUNT(*) FROM gift_invites{scope_where}{' AND' if scope_where else ' WHERE'} state='pending'",
                    scope_args,
                ).fetchone()[0]
            )
            current_periods = int(
                db.execute(
                    f"SELECT COUNT(*) FROM periods{scope_where}{' AND' if scope_where else ' WHERE'} state='current'",
                    scope_args,
                ).fetchone()[0]
            )
            period = (
                db.execute(
                    "SELECT id,sequence,starts_at,ends_at,timezone,reset_time FROM periods WHERE scope_id=? AND state='current'",
                    (scope_id,),
                ).fetchone()
                if scope_id is not None
                else None
            )
            settings_rows = db.execute(
                "SELECT scope_key,revision FROM settings ORDER BY scope_key"
            ).fetchall()
            failed_notifications = int(
                db.execute(
                    f"SELECT COUNT(*) FROM notification_outbox{scope_where}{' AND' if scope_where else ' WHERE'} state IN ('failed','unknown')",
                    scope_args,
                ).fetchone()[0]
            )
            failed_jobs = int(
                db.execute(
                    "SELECT COUNT(*) FROM maintenance_jobs WHERE state IN ('failed','unknown')"
                ).fetchone()[0]
            )
        global_revision = next(
            (
                int(row["revision"])
                for row in settings_rows
                if row["scope_key"] == "global"
            ),
            0,
        )
        catalog = self.catalog.catalog_summary()
        scope_revision, enabled_pool_count = None, None
        if scope_id is not None:
            effective, scope_revision = self.storage.get_settings(scope_id)
            enabled_pool_count = len(
                set(
                    effective["modes"]["wife"]["pool_ids"]
                    + effective["modes"]["husband"]["pool_ids"]
                )
            )
        return json_response(
            {
                "ok": True,
                "data": {
                    "scope_id": scope_id,
                    "scopes": scopes,
                    "active_relationships": active,
                    "pending_invites": pending,
                    "current_periods": current_periods,
                    "current_period": dict(period) if period else None,
                    "global_revision": global_revision,
                    "settings_scopes": len(settings_rows),
                    "scope_revision": scope_revision,
                    "enabled_pool_count": enabled_pool_count,
                    "failed_notifications": failed_notifications,
                    "failed_jobs": failed_jobs,
                    "catalog": catalog,
                },
                "request_id": str(uuid.uuid4()),
            }
        )

    async def scopes(self):
        _, denied = self._authorize()
        if denied:
            return denied
        return await asyncio.to_thread(self._scopes)

    def _scopes(self):
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT id,platform_id,self_id,group_id,created_at FROM scopes ORDER BY platform_id,self_id,group_id LIMIT 500"
                )
                .fetchall()
            )
        return json_response(
            {
                "ok": True,
                "data": [dict(row) for row in rows],
                "request_id": str(uuid.uuid4()),
            }
        )

    async def characters(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            limit = self._query_integer("limit", 50, 1, 100)
            offset = self._query_integer("offset", 0, 0, 1_000_000)
            data = await asyncio.to_thread(
                self.catalog.list_characters,
                query=str(request.query.get("q") or ""),
                limit=limit,
                offset=offset,
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def character(self, character_id: str | None = None):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            character_id = character_id or request.query.get("id")
            if not isinstance(character_id, str) or not character_id.strip():
                return self._error("INVALID_QUERY", "角色 ID 不能为空。", 400)
            data = await asyncio.to_thread(self.catalog.get_character, character_id)
            if data is None:
                return self._error("NOT_FOUND", "角色不存在。", 404)
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取角色失败。", 500)

    async def save_character(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(
                self.catalog.save_character,
                payload,
                actor=username,
                now=int(time.time()),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": payload.get("request_id")}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CATALOG_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._catalog_storage_error(exc)

    async def character_delete_preview(self):
        return await self._delete_preview("character")

    async def character_delete(self):
        return await self._delete_catalog_entity("character")

    async def pools(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            return json_response(
                {
                    "ok": True,
                    "data": await asyncio.to_thread(self.catalog.list_pools),
                    "request_id": str(uuid.uuid4()),
                }
            )
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取角色池失败。", 500)

    async def save_pool(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(
                self.catalog.save_pool, payload, actor=username, now=int(time.time())
            )
            return json_response(
                {"ok": True, "data": data, "request_id": payload.get("request_id")}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CATALOG_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._catalog_storage_error(exc)

    async def pool_delete_preview(self):
        return await self._delete_preview("pool")

    async def pool_delete(self):
        return await self._delete_catalog_entity("pool")

    async def _delete_preview(self, kind: str):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(
                self.catalog.delete_preview,
                kind,
                str(payload.get("id", "")),
                actor=username,
                now=int(time.time()),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CATALOG_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "无法预检删除操作。", 500)

    async def _delete_catalog_entity(self, kind: str):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        if payload.get("entity_kind") != kind:
            return self._error("INVALID_BODY", "删除目标类型不匹配。", 400)
        try:
            data = await asyncio.to_thread(
                self.catalog.delete, payload, actor=username, now=int(time.time())
            )
            return json_response(
                {"ok": True, "data": data, "request_id": payload.get("request_id")}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CATALOG_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._catalog_storage_error(exc)

    async def upload_media(self):
        username, denied = self._authorize()
        if denied:
            return denied
        del username
        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None or not callable(getattr(upload, "save", None)):
                return self._error("MISSING_FILE", "请选择图片文件。", 400)
            await asyncio.to_thread(
                self.catalog.media_root.mkdir, parents=True, exist_ok=True
            )
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="upload-",
                    suffix=".tmp",
                    dir=self.catalog.media_root,
                    delete=False,
                ) as stream:
                    temporary = stream.name
                await upload.save(temporary)
                data = await asyncio.to_thread(
                    _upload_media_file, self.catalog, temporary, int(time.time())
                )
                return json_response(
                    {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
                )
            finally:
                if temporary:
                    try:
                        import os

                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        except (ValueError, TypeError) as exc:
            return self._error("MEDIA_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "图片保存失败。", 500)

    async def get_media(self, media_hash: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            result = await asyncio.to_thread(self.catalog.get_media, media_hash)
            if result is None:
                return self._error("NOT_FOUND", "图片不存在。", 404)
            path, mime_type = result
            from astrbot.api.web import file_response

            return file_response(path, filename=path.name, content_type=mime_type)
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取图片失败。", 500)

    async def preview_media(self, media_hash: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            src = await asyncio.to_thread(_preview_media_data, self.catalog, media_hash)
            if src is None:
                return self._error("NOT_FOUND", "图片不存在。", 404)
            return json_response({"ok": True, "data": {
                "src": src,
            }})
        except (OSError, ValueError, Image.DecompressionBombError):
            return self._error("INVALID_IMAGE", "图片无法预览。", 422)
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取图片失败。", 500)

    async def relationships(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            period_id = self._optional_query_text("period_id", 128)
            mode = self._optional_query_text("mode", 16)
            data = await asyncio.to_thread(
                self.queries.relationships,
                scope_id,
                period_id=period_id,
                mode=mode,
                query=str(request.query.get("q") or ""),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def user_quota(self, user_id: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            mode = self._optional_query_text("mode", 16) or "wife"
            period_id = self._optional_query_text("period_id", 128)
            data = await asyncio.to_thread(
                self.queries.quota, scope_id, mode, user_id, period_id=period_id
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def intimacy(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            data = await asyncio.to_thread(
                self.queries.intimacy,
                scope_id,
                source_id=self._optional_query_text("source_id", 128),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def activity(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            data = await asyncio.to_thread(
                self.queries.activity,
                scope_id,
                user_id=self._optional_query_text("user_id", 128),
                window_days=self._query_integer("window_days", 7, 1, 365),
                now=int(time.time()),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def invites(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(
                self.queries.invites,
                await asyncio.to_thread(self._required_scope_id),
                state=self._optional_query_text("state", 16),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def history(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            since = (
                self._query_integer("since", 0, 0, 9_999_999_999)
                if request.query.get("since") not in (None, "")
                else None
            )
            until = (
                self._query_integer("until", 0, 0, 9_999_999_999)
                if request.query.get("until") not in (None, "")
                else None
            )
            if since is not None and until is not None and until < since:
                raise ValueError("until 不得早于 since。")
            data = await asyncio.to_thread(
                self.queries.history,
                await asyncio.to_thread(self._required_scope_id),
                since=since,
                until=until,
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def admin_preview(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            scope_id = await asyncio.to_thread(self._requested_scope_id, payload)
            if scope_id is None:
                raise ValueError("纠错操作必须指定群作用域。")
            normalized = dict(payload)
            normalized["scope_id"] = scope_id
            data = await asyncio.to_thread(
                self.corrections.preview,
                normalized,
                actor=username,
                now=int(time.time()),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CORRECTION_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "纠错预检失败。", 500)

    async def admin_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(self.corrections.commit, payload, actor=username, now=int(time.time()))
            return json_response({"ok": True, "data": data, "request_id": payload.get("request_id")})
        except (ValueError, TypeError) as exc:
            return self._error("CORRECTION_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if any(fragment in message for fragment in ("已变化", "版本", "request_id", "预检")):
                return self._error("CORRECTION_CONFLICT", message, 409)
            return self._error("INTERNAL_ERROR", "纠错提交失败。", 500)

    async def catalog_sync_check(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await self.catalog_sync.check(actor=username, force=payload.get("force") is True)
            return json_response({"ok": True, "data": data, "request_id": str(uuid.uuid4())})
        except (ValueError, StorageError, OSError, asyncio.TimeoutError) as exc:
            return self._error("CATALOG_SYNC_CHECK_FAILED", str(exc), 400)

    async def catalog_sync_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await self.catalog_sync.commit(payload, actor=username)
            return json_response({"ok": True, "data": data, "request_id": str(uuid.uuid4())})
        except (ValueError, StorageError, OSError, asyncio.TimeoutError) as exc:
            return self._error("CATALOG_SYNC_COMMIT_FAILED", str(exc), 400)

    async def import_preview(self):
        username, denied = self._authorize()
        if denied:
            return denied
        temporary: str | None = None
        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None or not callable(getattr(upload, "save", None)):
                return self._error("MISSING_FILE", "请选择 JSON 或 ZIP 角色包。", 400)
            incoming = self.imports.data_root / "staging" / "incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="catalog-", suffix=".upload", dir=incoming, delete=False
            ) as stream:
                temporary = stream.name
            await upload.save(temporary)
            config, _revision = await asyncio.to_thread(self.storage.get_settings)
            data = await self.imports.preview_file(
                Path(temporary),
                actor=username,
                now=int(time.time()),
                limits=config["resources"],
                timeout=float(config["resources"]["http_timeout_seconds"]),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("IMPORT_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("IMPORT_CONFLICT", str(exc), 409)
        except Exception:
            logger.exception("今日姻缘：角色包导入预检失败。")
            return self._error(
                "INTERNAL_ERROR", "导入预检失败，请检查文件后重试。", 500
            )
        finally:
            if temporary:
                try:
                    import os

                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    async def import_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(self.imports.commit, payload, actor=username, now=int(time.time()))
            return json_response({"ok": True, "data": data, "request_id": payload.get("request_id")})
        except (ValueError, TypeError) as exc:
            return self._error("IMPORT_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if any(fragment in message for fragment in ("request_id", "过期", "已终止", "哈希", "缺失")):
                return self._error("IMPORT_CONFLICT", message, 409)
            return self._error("IMPORT_FAILED", message, 500)

    async def get_job(self, job_id: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(
                self.imports.get_job, job_id, actor=request.username
            )
            if data is None:
                return self._error(
                    "NOT_FOUND", "作业不存在或不属于当前 Dashboard 用户。", 404
                )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取作业状态失败。", 500)

    async def create_export(self):
        username, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return self._error("INVALID_PAYLOAD", "导出请求必须是 JSON 对象。", 400)
            data = await asyncio.to_thread(self.maintenance.create_export, payload, actor=username, now=int(time.time()))
            return json_response({"ok": True, "data": data, "request_id": str(uuid.uuid4())})
        except (ValueError, TypeError) as exc:
            return self._error("EXPORT_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("EXPORT_FAILED", str(exc), 409)

    async def create_backup(self):
        username, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return self._error("INVALID_PAYLOAD", "备份请求必须是 JSON 对象。", 400)
            _config, revision = await asyncio.to_thread(self.storage.get_settings)
            data = await asyncio.to_thread(
                self.maintenance.create_backup,
                actor=username,
                now=int(time.time()),
                keep_count=int(_config["resources"].get("backup_keep_count", 10)),
                request_id=payload.get("request_id"),
            )
            data["config_revision"] = revision
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("BACKUP_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("BACKUP_FAILED", str(exc), 409)

    async def download_job(self, job_id: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            result = await asyncio.to_thread(
                self.maintenance.get_artifact, job_id, actor=request.username
            )
            if result is None:
                return self._error(
                    "NOT_FOUND", "导出或备份作业不存在或不属于当前用户。", 404
                )
            path, filename = result
            from astrbot.api.web import file_response

            return file_response(
                path, filename=filename, content_type="application/zip"
            )
        except StorageError as exc:
            return self._error("ARTIFACT_INVALID", str(exc), 409)

    async def restore_preview(self):
        username, denied = self._authorize()
        if denied:
            return denied
        temporary: str | None = None
        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None or not callable(getattr(upload, "save", None)):
                return self._error("MISSING_FILE", "请选择完整备份 ZIP。", 400)
            incoming = self.maintenance.data_root / "staging" / "incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="restore-", suffix=".upload", dir=incoming, delete=False
            ) as stream:
                temporary = stream.name
            await upload.save(temporary)
            config, _revision = await asyncio.to_thread(self.storage.get_settings)
            data = await asyncio.to_thread(
                self.maintenance.preview_restore,
                Path(temporary),
                actor=username,
                now=int(time.time()),
                max_bytes=int(config["resources"]["import_max_bytes"]),
                max_entries=int(config["resources"]["import_max_entries"]),
                max_expanded=int(config["resources"]["import_max_expanded_bytes"]),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("RESTORE_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("RESTORE_CONFLICT", str(exc), 409)
        except Exception:
            logger.exception("今日姻缘：完整备份预检失败。")
            return self._error(
                "INTERNAL_ERROR", "备份预检失败，请检查文件后重试。", 500
            )
        finally:
            if temporary:
                try:
                    import os

                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    async def restore_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return self._error("INVALID_PAYLOAD", "恢复请求必须是 JSON 对象。", 400)
            config, _revision = await asyncio.to_thread(self.storage.get_settings)
            data = await asyncio.to_thread(
                self.maintenance.commit_restore,
                payload,
                actor=username,
                now=int(time.time()),
                keep_count=int(config["resources"].get("backup_keep_count", 10)),
            )
            if data.get("restored"):
                if self.runtime_reset is not None:
                    self.runtime_reset()
                try:
                    await asyncio.to_thread(self._mirror_current_config)
                except Exception:
                    logger.exception("今日姻缘：数据已恢复，但宿主配置同步失败。")
                    return self._error(
                        "RESTORE_CONFIG_SYNC_FAILED",
                        "数据已恢复，但同步到 AstrBot 配置失败；请重试本次提交以完成同步。",
                        503,
                    )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("RESTORE_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("RESTORE_CONFLICT", str(exc), 409)

    def _mirror_current_config(self) -> None:
        with self.storage._lock:
            config, revision = self.storage.get_settings()
            self.host_config.mirror(config, revision)

    @staticmethod
    def _query_integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = request.query.get(name)
        if raw in (None, ""):
            return default
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"{name} 必须是十进制整数。")
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 超出允许范围。")
        return value

    def _required_scope_id(self) -> int:
        scope_id = self._query_scope_id()
        if scope_id is None:
            raise ValueError("请指定属于当前插件实例的 scope_id。")
        return scope_id

    @staticmethod
    def _optional_query_text(name: str, maximum: int) -> str | None:
        raw = request.query.get(name)
        if raw in (None, ""):
            return None
        if not isinstance(raw, str) or len(raw) > maximum or "\x00" in raw:
            raise ValueError(f"{name} 格式无效。")
        return raw

    def _catalog_storage_error(self, exc: StorageError):
        message = str(exc)
        if any(fragment in message for fragment in ("版本冲突", "request_id", "删除预检", "删除目标已变化")):
            return self._error("CATALOG_CONFLICT", message, 409)
        if any(fragment in message for fragment in ("不存在", "已删除", "不能", "匹配")):
            return self._error("CATALOG_INVALID", message, 400)
        return self._error("INTERNAL_ERROR", "目录保存失败，请刷新后重试。", 500)

    async def get_config(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._query_scope_id)
            global_value, global_revision = await asyncio.to_thread(
                self.storage.get_settings
            )
            if scope_id is None:
                value, revision, override = global_value, global_revision, None
            else:
                value, revision = await asyncio.to_thread(
                    self.storage.get_settings, scope_id
                )
                override = await asyncio.to_thread(self._stored_override, scope_id)
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "global": global_value,
                        "override": override,
                        "effective": value,
                        "template_fields": template_fields(),
                        # AstrBot's Plugin Page bridge may unwrap the outer API envelope.
                        # Keep the revision beside the config payload so the UI can
                        # normalize either wrapped or unwrapped responses.
                        "revision": {"global": global_revision, "scope": revision},
                    },
                    "revision": {"global": global_revision, "scope": revision},
                    "request_id": str(uuid.uuid4()),
                }
            )
        except ValueError as exc:
            return self._error("INVALID_SCOPE", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取配置失败，请稍后重试。", 500)

    async def preview_config(self):
        _, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            scope_id, stored, effective = await asyncio.to_thread(
                self._prepare_draft, payload
            )
            impacted_scopes = await asyncio.to_thread(
                self._validate_effective_scopes, scope_id, effective
            )
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "valid": True,
                        "stored_value": stored,
                        "effective": effective,
                        "affected_scopes": impacted_scopes,
                    },
                    "request_id": str(uuid.uuid4()),
                }
            )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return self._error("CONFIG_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "配置预检失败，请刷新后重试。", 500)

    async def save_config(self):
        username, denied = self._authorize()
        if denied:
            return denied
        if username is None:
            return username
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        request_id = payload.get("request_id")
        expected_revision = payload.get("expected_revision")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 128
        ):
            return self._error(
                "INVALID_REQUEST_ID", "request_id 必须是 1 到 128 个字符。", 400
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return self._error(
                "INVALID_REVISION", "expected_revision 必须是非负整数。", 400
            )
        try:
            scope_id = await asyncio.to_thread(self._requested_scope_id, payload)
            draft_hash = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            replay = await asyncio.to_thread(
                self.storage.get_settings_request_replay,
                scope_id,
                username,
                request_id.strip(),
                draft_hash,
            )
            if replay is not None:
                if scope_id is None:
                    saved_global, saved_revision = await asyncio.to_thread(
                        self.storage.get_settings
                    )
                    try:
                        await asyncio.to_thread(
                            self.host_config.mirror, saved_global, saved_revision
                        )
                    except Exception:
                        logger.exception(
                            "今日姻缘：全局配置已保存，但宿主配置同步失败。"
                        )
                        return self._error(
                            "CONFIG_SYNC_FAILED",
                            "全局配置已保存，但同步到 AstrBot 配置失败；请重试。",
                            503,
                        )
                return json_response(
                    {
                        "ok": True,
                        "data": {
                            "scope_id": scope_id,
                            "effective": payload.get("draft"),
                            "revision": {
                                "global": replay["global_revision"],
                                "scope": replay["revision"],
                            },
                        },
                        "revision": {
                            "global": replay["global_revision"],
                            "scope": replay["revision"],
                        },
                        "request_id": request_id.strip(),
                    }
                )
            scope_id, stored, effective = await asyncio.to_thread(
                self._prepare_draft, payload
            )
            await asyncio.to_thread(
                self._validate_effective_scopes, scope_id, effective
            )
            revision = await asyncio.to_thread(
                self.storage.save_settings,
                scope_id,
                stored,
                expected_revision=expected_revision,
                now=int(time.time()),
                updated_by=username,
                request_id=request_id.strip(),
                request_hash=draft_hash,
            )
            if scope_id is None:
                try:
                    await asyncio.to_thread(self.host_config.mirror, stored, revision)
                except Exception:
                    logger.exception("今日姻缘：全局配置已保存，但宿主配置同步失败。")
                    return self._error(
                        "CONFIG_SYNC_FAILED",
                        "全局配置已保存，但同步到 AstrBot 配置失败；请重试。",
                        503,
                    )
            saved_request = await asyncio.to_thread(
                self.storage.get_settings_request_replay,
                scope_id,
                username,
                request_id.strip(),
                draft_hash,
            )
            global_revision = (
                saved_request["global_revision"] if saved_request else revision
            )
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "effective": effective,
                        "revision": {"global": global_revision, "scope": revision},
                    },
                    "revision": {"global": global_revision, "scope": revision},
                    "request_id": request_id.strip(),
                }
            )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return self._error("CONFIG_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if "版本冲突" in message:
                return self._error("CONFIG_CONFLICT", message, 409)
            if "request_id" in message:
                return self._error("REQUEST_ID_CONFLICT", message, 409)
            if any(
                fragment in message
                for fragment in ("仅允许全局设置", "群覆盖", "用户规则")
            ):
                return self._error("CONFIG_INVALID", message, 400)
            return self._error(
                "INTERNAL_ERROR", "配置保存失败，请刷新后检查状态。", 500
            )

    def _authorize(self) -> tuple[str | None, Any | None]:
        username = request.username
        if not username:
            return None, self._error("UNAUTHENTICATED", "请先登录 AstrBot Dashboard。", 401)
        return username, None

    def _query_scope_id(self) -> int | None:
        raw = request.query.get("scope_id")
        if raw in (None, "", "global"):
            return None
        try:
            scope_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("scope_id 必须是当前插件中的整数作用域。") from exc
        with self.storage._lock:
            exists = self.storage._connection().execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone()
        if exists is None:
            raise ValueError("scope_id 不属于当前插件实例。")
        return scope_id

    def _stored_override(self, scope_id: int) -> dict[str, Any]:
        with self.storage._lock:
            row = self.storage._connection().execute(
                "SELECT value_json FROM settings WHERE scope_key=?", (f"scope:{scope_id}",)
            ).fetchone()
        return json.loads(row["value_json"]) if row else {}

    def _prepare_draft(
        self, payload: Mapping[str, Any]
    ) -> tuple[int | None, dict[str, Any], dict[str, Any]]:
        scope_id = self._requested_scope_id(payload)
        draft = payload.get("draft")
        if not isinstance(draft, dict):
            raise ValueError("draft 必须是完整配置对象。")
        if scope_id is None:
            validate_config(draft)
            return None, draft, draft
        global_value, _ = self.storage.get_settings()
        supplied_override = payload.get("override")
        if supplied_override is None:
            sparse = _sparse_diff(global_value, draft)
        elif isinstance(supplied_override, dict):
            sparse = supplied_override
            if effective_config(global_value, sparse) != draft:
                raise ConfigurationError("群覆盖与配置草稿不一致，请刷新后重试。")
            _validate_scope_override(sparse)
        else:
            raise ConfigurationError("群覆盖必须是对象。")
        effective = effective_config(global_value, sparse)
        validate_config(effective)
        return scope_id, sparse, effective

    def _requested_scope_id(self, payload: Mapping[str, Any]) -> int | None:
        raw_scope = payload.get("scope_id")
        if raw_scope in (None, "", "global"):
            return None
        if isinstance(raw_scope, int) and not isinstance(raw_scope, bool):
            scope_id = raw_scope
        elif isinstance(raw_scope, str) and raw_scope.isascii() and raw_scope.isdecimal():
            scope_id = int(raw_scope)
        else:
            raise ValueError("scope_id 必须是整数或 global。")
        with self.storage._lock:
            exists = self.storage._connection().execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone()
        if exists is None:
            raise ValueError("scope_id 不属于当前插件实例。")
        return scope_id

    def _validate_effective_scopes(self, changed_scope_id: int | None, changed_value: Mapping[str, Any]) -> list[int]:
        if changed_scope_id is not None:
            validate_config(changed_value)
            return [changed_scope_id]
        with self.storage._lock:
            scope_ids = [int(row[0]) for row in self.storage._connection().execute("SELECT id FROM scopes ORDER BY id")]
        affected: list[int] = []
        for scope_id in scope_ids:
            override = self._stored_override(scope_id)
            effective = effective_config(changed_value, override)
            validate_config(effective)
            affected.append(scope_id)
        return affected

    @staticmethod
    def _error(code: str, message: str, status: int):
        return error_response(message, status_code=status, data={"code": code, "request_id": str(uuid.uuid4())})


def _sparse_diff(base: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    if set(base) != set(value):
        raise ConfigurationError("配置字段与当前版本不匹配。请刷新管理页后重试。")
    difference: dict[str, Any] = {}
    for key, base_value in base.items():
        changed = value[key]
        if isinstance(base_value, dict) and isinstance(changed, dict):
            nested = _sparse_diff(base_value, changed)
            if nested:
                difference[key] = nested
        elif changed != base_value:
            difference[key] = changed
    return difference


def _validate_scope_override(value: Mapping[str, Any]) -> None:
    allowed = {
        "enabled",
        "access",
        "reset",
        "commands",
        "modes",
        "statistics",
        "weights",
        "display",
        "members",
        "messages",
        "reply_quote",
        "reply_enabled",
    }
    if set(value) - allowed:
        raise ConfigurationError("群覆盖包含仅允许全局设置的字段。")
    access = value.get("access", {})
    if not isinstance(access, dict) or set(access) - {"users", "extra_bot_ids"}:
        raise ConfigurationError("群覆盖只能设置用户规则和补充机器人名单。")


def _preview_media_data(catalog: CatalogAdminService, media_hash: str) -> str | None:
    result = catalog.get_media(media_hash)
    if result is None:
        return None
    path, _ = result
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _upload_media_file(catalog: CatalogAdminService, path: str, now: int) -> dict[str, Any]:
    with open(path, "rb") as stream:
        payload = stream.read(12 * 1024 * 1024 + 1)
    return catalog.upload_media(payload, now=now)


def _read_batch_upload(path: str) -> bytes:
    with open(path, "rb") as stream:
        return stream.read(12 * 1024 * 1024 + 1)


def _batch_thumbnail(path: Path) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((320, 320))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        output = io.BytesIO()
        image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(
        "ascii"
    )
