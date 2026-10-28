"""Authenticated AstrBot Plugin Pages APIs for configuration and overview."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from astrbot.api.web import error_response, request

from ..services.admin_corrections import AdminCorrectionService
from ..services.admin_queries import AdminQueryService
from ..services.catalog.admin import CatalogAdminService
from ..services.catalog.batch_images import BatchImagesService
from ..services.catalog.importer import CatalogImportService
from ..services.catalog.sync import CatalogSyncService
from ..services.host_config import HostConfigBridge
from ..services.maintenance import MaintenanceService
from ..services.storage import SQLiteStorage

PLUGIN_NAME = "astrbot_plugin_daily_bonds"
from .batch_routes import BatchRoutes
from .catalog_routes import CatalogRoutes
from .config_routes import ConfigRoutes
from .data_routes import DataRoutes
from .maintenance_routes import MaintenanceRoutes


class WebManager(
    ConfigRoutes, DataRoutes, CatalogRoutes, BatchRoutes, MaintenanceRoutes
):
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

    def _authorize(self) -> tuple[str | None, Any | None]:
        username = request.username
        if not username:
            return None, self._error(
                "UNAUTHENTICATED", "请先登录 AstrBot Dashboard。", 401
            )
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
            exists = (
                self.storage._connection()
                .execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,))
                .fetchone()
            )
        if exists is None:
            raise ValueError("scope_id 不属于当前插件实例。")
        return scope_id

    @staticmethod
    def _error(code: str, message: str, status: int):
        return error_response(
            message,
            status_code=status,
            data={"code": code, "request_id": str(uuid.uuid4())},
        )
