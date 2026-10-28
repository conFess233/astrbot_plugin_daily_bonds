"""版本迁移、用户状态与回复形式预览的管理 API。"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request

from ..models.errors import StorageError
from .game_status import StatusService
from .message_templates import MESSAGE_CONTEXT, set_message_values


class MigrationRoutes:
    """一键迁移旧版数据库、查询用户今日状态、预览卡片与纯文本。"""

    async def migration_overview(self):
        _actor, denied = self._authorize()
        if denied:
            return denied
        data = await asyncio.to_thread(self.migration.overview)
        return json_response({"ok": True, "data": data})

    async def migration_preview(self):
        """上传数据库文件并预检。"""

        username, denied = self._authorize()
        if denied:
            return denied
        temporary: str | None = None
        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None or not callable(getattr(upload, "save", None)):
                return self._error("MISSING_FILE", "请选择数据库文件。", 400)
            incoming = self.migration.staging_dir
            incoming.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="upload-", suffix=".sqlite3", dir=incoming, delete=False
            ) as stream:
                temporary = stream.name
            await upload.save(temporary)
            return await self._migration_preview(username, upload=Path(temporary))
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    async def migration_preview_backup(self):
        """选择一个升级前备份并预检。"""

        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        name = payload.get("backup") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            return self._error("MIGRATION_INVALID", "请选择一个升级前备份。", 400)
        return await self._migration_preview(username, backup_name=name)

    async def _migration_preview(self, username: str, **source: Any):
        try:
            data = await asyncio.to_thread(
                self.migration.preview, actor=username, now=int(time.time()), **source
            )
            return json_response({"ok": True, "data": data})
        except ValueError as exc:
            return self._error("MIGRATION_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("MIGRATION_CONFLICT", str(exc), 409)
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
            logger.exception("今日姻缘：迁移预检失败。")
            return self._error("INTERNAL_ERROR", "迁移预检失败，请检查文件。", 500)

    async def migration_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json(default={})
            plan_id = payload.get("preview_id") if isinstance(payload, dict) else None
            if not isinstance(plan_id, str):
                return self._error("MIGRATION_INVALID", "缺少迁移预检编号。", 400)
            data = await asyncio.to_thread(
                self.migration.commit, plan_id, actor=username, now=int(time.time())
            )
            return json_response({"ok": True, "data": data})
        except ValueError as exc:
            return self._error("MIGRATION_INVALID", str(exc), 400)
        except StorageError as exc:
            return self._error("MIGRATION_FAILED", str(exc), 409)

    async def user_status(self):
        _actor, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = self._required_scope_id()
            user_id = (request.query.get("user_id") or "").strip()
            if not user_id.isdecimal():
                return self._error("STATUS_INVALID", "请填写 QQ 号。", 400)
            data = await asyncio.to_thread(self._user_status, scope_id, user_id)
            return json_response({"ok": True, "data": data})
        except ValueError as exc:
            return self._error("STATUS_INVALID", str(exc), 400)

    def _user_status(self, scope_id: int, user_id: str) -> dict[str, Any]:
        with self.storage._lock:
            db = self.storage._connection()
            period = db.execute(
                "SELECT * FROM periods WHERE scope_id=? AND state='current'",
                (scope_id,),
            ).fetchone()
            members = {
                str(row["user_id"]): str(row["card"] or row["nickname"] or "群友")
                for row in db.execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=?",
                    (scope_id,),
                )
            }
        if period is None:
            raise ValueError("该群还没有当前周期，等群内有新消息后再查询。")
        config, _revision = self.storage.get_settings(scope_id)
        status = StatusService(self.storage).collect(
            scope_id=scope_id,
            period=dict(period),
            user_id=user_id,
            config=config,
            member_names=members,
            now=int(time.time()),
        )
        return {"name": members.get(user_id, "群友"), **status}

    async def render_preview(self):
        """用所选范围的配置渲染“姻缘帮助”，同时返回卡片和纯文本两种形式。"""

        _actor, denied = self._authorize()
        if denied:
            return denied
        runtime = self.runtime
        if runtime is None:
            return self._error("PREVIEW_UNAVAILABLE", "插件尚未完成初始化。", 503)
        try:
            scope_id = self._query_scope_id()
            config, _revision = await asyncio.to_thread(
                self.storage.get_settings, scope_id
            )
        except ValueError as exc:
            return self._error("PREVIEW_INVALID", str(exc), 400)
        result: dict[str, Any] = {}
        for mode in ("text", "card"):
            token = MESSAGE_CONTEXT.set({})
            try:
                set_message_values(render={"mode": mode, "types": {}})
                reply = await runtime._help({"config": config})
            finally:
                MESSAGE_CONTEXT.reset(token)
            if mode == "text":
                result["text"] = str(getattr(reply, "fallback_text", "") or reply)
                continue
            paths = getattr(reply, "image_paths", ())
            if not paths:
                result["image"] = None
                result["image_error"] = (
                    "卡片渲染不可用或失败，群内会改发纯文本。"
                    if runtime.cards.available
                    else "未连接 AstrBot 渲染服务。"
                )
                continue
            try:
                payload = await asyncio.to_thread(Path(paths[0]).read_bytes)
                result["image"] = (
                    "data:image/jpeg;base64," + base64.b64encode(payload).decode()
                )
            finally:
                for path in getattr(reply, "cleanup_paths", ()):
                    Path(path).unlink(missing_ok=True)
        return json_response({"ok": True, "data": result})
