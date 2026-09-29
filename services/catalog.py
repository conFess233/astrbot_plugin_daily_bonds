"""静态角色目录导入与受信任来源的素材校验下载。"""

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

_IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}


def load_catalog_directory(root: Path) -> dict[str, Any]:
    """Read the packaged one-file-per-entity catalog."""

    metadata = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    packs = [metadata, *(json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*-catalog.json")))]
    characters = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "characters").glob("*.json"))]
    pools = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "pools").glob("*.json"))]
    if not characters or not pools or len({item.get("id") for item in characters}) != len(characters) or len({item.get("id") for item in pools}) != len(pools):
        raise StorageError("内置角色目录为空或包含重复 ID。")
    memberships: dict[str, list[str]] = {item["id"]: [] for item in characters}
    for pool in pools:
        for character_id in pool.get("character_ids", []):
            if character_id not in memberships:
                raise StorageError(f"角色池引用了不存在的角色：{character_id}")
            memberships[character_id].append(pool["id"])
    return {**metadata, "packs": packs, "characters": [dict(item, pool_ids=memberships[item["id"]]) for item in characters], "pools": pools}


def catalog_source_json(item: dict[str, Any], kind: str) -> str:
    return json.dumps({key: value for key, value in item.items() if key != "pool_ids"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        packs = manifest.get("packs", [])
        if not isinstance(pools, list) or not isinstance(characters, list) or not isinstance(packs, list) or not packs or any(not isinstance(pack, dict) or not isinstance(pack.get("pack_id"), str) or not isinstance(pack.get("pack_version"), str) for pack in packs):
            raise StorageError("内置角色清单格式无效。")
        added_characters = 0
        added_pools = 0
        with self.storage.transaction() as db:
            inserted_pools: set[str] = set()
            for pool in pools:
                if pool.get("mode") not in {"wife", "husband"}:
                    raise StorageError(f"无效的内置角色池：{pool.get('id')}")
                result = db.execute(
                    "INSERT OR IGNORE INTO pools(id,name,mode,builtin,revision) VALUES(?,?,?,1,1)",
                    (pool["id"], pool["name"], pool["mode"]),
                )
                added_pools += max(result.rowcount, 0)
                if result.rowcount:
                    inserted_pools.add(pool["id"])
                self._insert_baseline(db, "pool", pool, now)
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
                self._insert_baseline(db, "character", character, now)
                for pool_id in character.get("pool_ids", []):
                    existing_pool = db.execute("SELECT mode,deleted_at FROM pools WHERE id=?", (pool_id,)).fetchone()
                    expected_mode = "wife" if character["gender"] == "female" else "husband" if character["gender"] == "male" else None
                    if (result.rowcount or pool_id in inserted_pools) and existing_pool and existing_pool["deleted_at"] is None and (expected_mode is None or existing_pool["mode"] == expected_mode):
                        db.execute("INSERT OR IGNORE INTO pool_members(pool_id,character_id) VALUES(?,?)", (pool_id, character["id"]))
            for pack in packs:
                pack_id, version = pack["pack_id"], pack["pack_version"]
                db.execute("""INSERT INTO catalog_packs(pack_id,pack_version,installed_at) VALUES(?,?,?)
                              ON CONFLICT(pack_id) DO UPDATE SET pack_version=excluded.pack_version,installed_at=excluded.installed_at
                              WHERE catalog_packs.pack_version<>excluded.pack_version""", (pack_id, version, now))
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
        from .catalog_import import _PublicResolver

        connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False, limit=max(1, concurrency))
        downloaded = 0
        already_present = 0
        failures: list[str] = []
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, raise_for_status=False, trust_env=False) as session:
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
            self._manifest = load_catalog_directory(self.manifest_path)
        return self._manifest

    @staticmethod
    def _insert_baseline(db: Any, kind: str, item: dict[str, Any], now: int) -> None:
        source = catalog_source_json(item, kind)
        db.execute(
            "INSERT OR IGNORE INTO catalog_sync_baselines(entity_kind,entity_id,source_json,source_sha,commit_sha,updated_at) VALUES(?,?,?,?,?,?)",
            (kind, item["id"], source, hashlib.sha256(source.encode()).hexdigest(), "bundled", now),
        )

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
        try:
            _validate_image_url(current)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
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


def _validate_image_url(url: str) -> str:
    from .catalog_import import _validate_public_url

    _validate_public_url(url)
    if urlparse(url).scheme != "https":
        raise ValueError("图片 URL 必须使用 HTTPS。")
    return url


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
