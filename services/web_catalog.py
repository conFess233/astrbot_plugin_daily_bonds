"""角色、角色池、图片、仓库同步与角色包导入的管理 API。"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request
from PIL import Image, ImageOps

from ..models.errors import StorageError
from .catalog.admin import CatalogAdminService


class CatalogRoutes:
    """角色目录的查询与编辑。"""

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
                pool_id=str(request.query.get("pool") or ""),
                gender=str(request.query.get("gender") or ""),
                status=str(request.query.get("status") or ""),
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
            size = self._query_integer("size", 1600, 64, 1600)
            src = await asyncio.to_thread(
                _preview_media_data, self.catalog, media_hash, size
            )
            if src is None:
                return self._error("NOT_FOUND", "图片不存在。", 404)
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "src": src,
                    },
                }
            )
        except (OSError, ValueError, Image.DecompressionBombError):
            return self._error("INVALID_IMAGE", "图片无法预览。", 422)
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取图片失败。", 500)

    async def catalog_sync_check(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await self.catalog_sync.check(
                actor=username, force=payload.get("force") is True
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
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
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
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
        except Exception:  # noqa: BLE001 - API 边界：记录堆栈并返回错误
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
            data = await asyncio.to_thread(
                self.imports.commit, payload, actor=username, now=int(time.time())
            )
            return json_response(
                {"ok": True, "data": data, "request_id": payload.get("request_id")}
            )
        except (ValueError, TypeError) as exc:
            return self._error("IMPORT_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if any(
                fragment in message
                for fragment in ("request_id", "过期", "已终止", "哈希", "缺失")
            ):
                return self._error("IMPORT_CONFLICT", message, 409)
            return self._error("IMPORT_FAILED", message, 500)

    def _catalog_storage_error(self, exc: StorageError):
        message = str(exc)
        if any(
            fragment in message
            for fragment in ("版本冲突", "request_id", "删除预检", "删除目标已变化")
        ):
            return self._error("CATALOG_CONFLICT", message, 409)
        if any(
            fragment in message for fragment in ("不存在", "已删除", "不能", "匹配")
        ):
            return self._error("CATALOG_INVALID", message, 400)
        return self._error("INTERNAL_ERROR", "目录保存失败，请刷新后重试。", 500)


def _preview_media_data(
    catalog: CatalogAdminService, media_hash: str, size: int = 1600
) -> str | None:
    result = catalog.get_media(media_hash)
    if result is None:
        return None
    path, _ = result
    return _encoded_preview(str(path), path.stat().st_mtime_ns, size)


# 角色列表一页上百张封面：小尺寸缩略图按文件与尺寸缓存，并用 WebP 减小体积。
@functools.lru_cache(maxsize=512)
def _encoded_preview(path: str, mtime_ns: int, size: int) -> str:
    del mtime_ns  # 仅参与缓存键：文件被替换后重新生成
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        output = io.BytesIO()
        if size >= 1600:
            image.save(output, format="PNG", optimize=True)
            mime = "image/png"
        else:
            image.save(output, format="WEBP", quality=82, method=4)
            mime = "image/webp"
    return f"data:{mime};base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _upload_media_file(
    catalog: CatalogAdminService, path: str, now: int
) -> dict[str, Any]:
    with open(path, "rb") as stream:
        payload = stream.read(12 * 1024 * 1024 + 1)
    return catalog.upload_media(payload, now=now)
