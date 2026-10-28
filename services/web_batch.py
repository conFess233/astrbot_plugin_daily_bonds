"""批量角色图导入的管理 API。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request
from PIL import Image, ImageOps

from ..models.errors import ConfigurationError, InvalidInputError, StorageError
from .settings import validate_config


class BatchRoutes:
    """批量图片任务的创建、上传、修正与提交。"""

    async def batch_images_get(self):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            job_id = request.query.get("job_id")
            if job_id:
                data = await asyncio.to_thread(
                    self.batch_images.refresh, job_id, actor=actor, now=int(time.time())
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

    async def batch_images_failure(self):
        return await self._batch_change("record_failure")

    async def _batch_change(self, method: str):
        actor, denied = self._authorize()
        if denied:
            return denied
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise InvalidInputError("请求内容必须是 JSON 对象。")
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
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
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
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
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
                raise InvalidInputError("默认检测设置必须是 JSON 对象。")
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
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
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
        return {"settings": current["batch_import"], "revision": revision}


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
