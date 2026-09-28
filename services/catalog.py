"""鸣潮静态种子导入与官方素材校验下载。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

try:
    import aiohttp
except ModuleNotFoundError:  # Keep local image validation usable without the optional downloader runtime.
    aiohttp = None  # type: ignore[assignment]
from PIL import Image, UnidentifiedImageError

from ..models import StorageError
from .storage import SQLiteStorage

_PACK_ID = "wuwa_builtin"
_ALLOWED_HOSTS = frozenset({"guide-res.aki-game.net"})
_IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}


@dataclass(frozen=True, slots=True)
class DownloadReport:
    """一轮素材下载的结果摘要。"""

    downloaded: int
    already_present: int
    unavailable: tuple[str, ...]


class CatalogService:
    """将静态角色身份与按内容寻址的本地图像写入插件数据目录。"""

    def __init__(self, storage: SQLiteStorage, manifest_path: Path, media_root: Path) -> None:
        self.storage = storage
        self.manifest_path = manifest_path
        self.media_root = media_root
        self._manifest: dict[str, Any] | None = None

    def seed_builtin_catalog(self, now: int) -> tuple[int, int]:
        """插入缺失的内置角色和池；已存在的管理员数据绝不覆盖。"""

        manifest = self._load_manifest()
        pools = manifest.get("pools")
        characters = manifest.get("characters")
        if manifest.get("pack_id") != _PACK_ID or not isinstance(pools, list) or not isinstance(characters, list):
            raise StorageError("内置鸣潮角色清单格式无效。")
        added_characters = 0
        added_pools = 0
        with self.storage.transaction() as db:
            installed = db.execute("SELECT pack_version FROM catalog_packs WHERE pack_id=?", (_PACK_ID,)).fetchone()
            for pool in pools:
                if pool.get("mode") not in {"wife", "husband"}:
                    raise StorageError(f"无效的内置角色池：{pool.get('id')}")
                result = db.execute(
                    "INSERT OR IGNORE INTO pools(id,name,mode,builtin,revision) VALUES(?,?,?,1,1)",
                    (pool["id"], pool["name"], pool["mode"]),
                )
                added_pools += max(result.rowcount, 0)
            for character in characters:
                self._validate_character(character)
                provenance = {
                    key: character.get(key)
                    for key in ("game", "gender_basis", "source_role_ids", "release_status", "material_status", "source_url", "verified_on")
                }
                result = db.execute(
                    """INSERT OR IGNORE INTO characters(id,name,aliases_json,gender,enabled,revision,provenance_json)
                       VALUES(?,?,?,?,?,1,?)""",
                    (
                        character["id"], character["name"],
                        json.dumps(character.get("aliases", []), ensure_ascii=False),
                        character["gender"], int(character["enabled"]),
                        json.dumps(provenance, ensure_ascii=False),
                    ),
                )
                added_characters += max(result.rowcount, 0)
                for pool_id in character.get("pool_ids", []):
                    existing_pool = db.execute("SELECT mode,deleted_at FROM pools WHERE id=?", (pool_id,)).fetchone()
                    expected_mode = "wife" if character["gender"] == "female" else "husband"
                    if existing_pool and existing_pool["deleted_at"] is None and existing_pool["mode"] == expected_mode:
                        db.execute("INSERT OR IGNORE INTO pool_members(pool_id,character_id) VALUES(?,?)", (pool_id, character["id"]))
            version = str(manifest.get("pack_version", "unknown"))
            if installed is None:
                db.execute("INSERT INTO catalog_packs(pack_id,pack_version,installed_at) VALUES(?,?,?)", (_PACK_ID, version, now))
            elif installed["pack_version"] != version:
                db.execute("UPDATE catalog_packs SET pack_version=?,installed_at=? WHERE pack_id=?", (version, now, _PACK_ID))
        return added_characters, added_pools

    async def download_builtin_images(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        max_pixels: int,
        concurrency: int,
    ) -> DownloadReport:
        """下载缺失图像，校验 URL、体积、SHA-256、格式、像素及清单尺寸。"""

        if aiohttp is None:
            raise StorageError("下载内置图片需要安装 requirements.txt 中声明的 aiohttp。")
        manifest = self._load_manifest()
        jobs: list[tuple[str, dict[str, Any]]] = []
        for character in manifest["characters"]:
            for image in character.get("images", []):
                if not self._image_is_available(image["sha256"]):
                    jobs.append((character["id"], image))
        if not jobs:
            return DownloadReport(0, 0, ())
        semaphore = asyncio.Semaphore(max(1, concurrency))
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        connector = aiohttp.TCPConnector(limit=max(1, concurrency), ttl_dns_cache=60)
        downloaded = 0
        already_present = 0
        failures: list[str] = []
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, raise_for_status=False) as session:
            async def run_one(character_id: str, spec: dict[str, Any]) -> tuple[str, str | None]:
                async with semaphore:
                    try:
                        saved = await self._download_one(session, character_id, spec, max_bytes, max_pixels)
                        return ("downloaded" if saved else "present", None)
                    except Exception as exc:
                        return "failed", f"{character_id}: {type(exc).__name__}: {exc}"

            results = await asyncio.gather(*(run_one(character_id, spec) for character_id, spec in jobs))
        for status, error in results:
            if status == "downloaded":
                downloaded += 1
            elif status == "present":
                already_present += 1
            elif error:
                failures.append(error)
        return DownloadReport(downloaded, already_present, tuple(failures))

    def _load_manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return self._manifest

    @staticmethod
    def _validate_character(character: dict[str, Any]) -> None:
        required = {"id", "name", "gender", "enabled", "pool_ids", "images"}
        if not required.issubset(character):
            raise StorageError("内置角色清单缺少必要字段。")
        if character["gender"] not in {"female", "male", "unspecified"}:
            raise StorageError(f"角色性别字段无效：{character['id']}")
        if not isinstance(character["enabled"], bool) or not isinstance(character["pool_ids"], list) or not isinstance(character["images"], list):
            raise StorageError(f"角色字段类型无效：{character['id']}")
        for image in character["images"]:
            digest = image.get("sha256", "")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise StorageError(f"角色图片摘要无效：{character['id']}")

    def _image_is_available(self, digest: str) -> bool:
        row = self.storage._connection().execute("SELECT relative_path FROM media_blobs WHERE hash=?", (digest,)).fetchone()
        if row is None:
            return False
        candidate = (self.media_root / row["relative_path"]).resolve()
        root = self.media_root.resolve()
        if root not in candidate.parents:
            raise StorageError("图片相对路径越过媒体目录。")
        return candidate.is_file()

    async def _download_one(
        self,
        session: aiohttp.ClientSession,
        character_id: str,
        spec: dict[str, Any],
        max_bytes: int,
        max_pixels: int,
    ) -> bool:
        digest = spec["sha256"]
        if self._image_is_available(digest):
            return False
        url = spec["source_url"]
        payload, final_url = await _fetch_limited(session, url, max_bytes)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StorageError("下载图片的 SHA-256 不匹配。")
        width, height, extension, mime_type = await asyncio.to_thread(_inspect_image, payload, max_pixels)
        if width != int(spec["width"]) or height != int(spec["height"]):
            raise StorageError(f"图片尺寸与清单不符：预期 {spec['width']}x{spec['height']}，实际 {width}x{height}")
        if int(spec["bytes"]) != len(payload):
            raise StorageError("下载图片的字节数与核验清单不符。")
        relative_path = Path("sha256") / f"{digest}.{extension}"
        target = self.media_root / relative_path
        await asyncio.to_thread(_atomic_content_write, target, payload)
        with self.storage.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO media_blobs(hash,relative_path,mime_type,byte_size,width,height,source_url,verified_at)
                   VALUES(?,?,?,?,?,?,?,unixepoch())""",
                (digest, relative_path.as_posix(), mime_type, len(payload), width, height, final_url),
            )
            db.execute("INSERT OR IGNORE INTO character_images(character_id,media_hash,ordinal) VALUES(?,?,0)", (character_id, digest))
        return True


async def _fetch_limited(session: aiohttp.ClientSession, initial_url: str, max_bytes: int) -> tuple[bytes, str]:
    current = initial_url
    for _ in range(5):
        parsed = urlparse(current)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS or parsed.username or parsed.password:
            raise StorageError("内置图片 URL 必须指向受信任的 HTTPS 官方资源域名。")
        async with session.get(current, allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise StorageError("图片服务返回了不完整的重定向。")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise StorageError(f"圖片下载 HTTP 状态异常：{response.status}")
            length = response.content_length
            if length is not None and length > max_bytes:
                raise StorageError("图片超过配置的体积上限。")
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > max_bytes:
                    raise StorageError("图片超过配置的体积上限。")
                chunks.append(chunk)
            return b"".join(chunks), current
    raise StorageError("图片重定向次数超过上限。")


def _inspect_image(payload: bytes, max_pixels: int) -> tuple[int, int, str, str]:
    try:
        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            image_format = image.format
            if width < 1 or height < 1 or width * height > max_pixels:
                raise StorageError("图片像素数超过配置上限。")
            image.verify()
        if image_format not in _IMAGE_FORMATS:
            raise StorageError("只接受 PNG、JPEG 或 WebP 图片。")
        extension, mime_type = _IMAGE_FORMATS[image_format]
        return width, height, extension, mime_type
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise StorageError(f"图片解码失败：{exc}") from exc


def _atomic_content_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=".media-", dir=target.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            pass
        except OSError:
            if not target.exists():
                os.replace(temporary_name, target)
                temporary_name = ""
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
