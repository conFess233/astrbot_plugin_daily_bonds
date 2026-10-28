"""导出、备份与恢复任务的管理 API。"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import json_response, request

from ..models import StorageError


class MaintenanceRoutes:
    """后台任务的创建、下载与完整恢复。"""

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
            data = await asyncio.to_thread(
                self.maintenance.create_export,
                payload,
                actor=username,
                now=int(time.time()),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
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
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
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
                except Exception:  # noqa: BLE001 - 宿主配置同步失败需单独提示，P3 移除同步层
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
