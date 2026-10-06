"""Best-effort cached QQ avatars with fixed-host and redirect validation."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from astrbot.api import logger
from PIL import Image

_ALLOWED_REDIRECT_SUFFIXES = (".qlogo.cn", ".gtimg.cn", ".qpic.cn")
_ALLOWED_REDIRECT_HOSTS = {"qlogo.cn", "gtimg.cn", "qpic.cn"}
_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}


class AvatarCache:
    """Fetch the conventional QQ avatar URL and retain valid stale files on errors."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        ttl_seconds: int = 7 * 86400,
        timeout_seconds: float = 6.0,
        max_total_seconds: float = 8.0,
        max_bytes: int = 1_500_000,
        max_pixels: int = 4_000_000,
    ) -> None:
        self.cache_dir = cache_dir
        self.ttl_seconds = max(0, ttl_seconds)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.max_total_seconds = max(1.0, max_total_seconds)
        self.max_bytes = max(1024, max_bytes)
        self.max_pixels = max(10_000, max_pixels)

    async def get_many(
        self,
        user_ids: list[str] | tuple[str, ...],
        *,
        ttl_seconds: int | None = None,
        max_entries: int = 5000,
    ) -> dict[str, Path | None]:
        ttl = self.ttl_seconds if ttl_seconds is None else max(0, int(ttl_seconds))
        capacity = max(1, int(max_entries))
        valid_ids = tuple(
            dict.fromkeys(user_id for user_id in user_ids if _valid_qq_id(user_id))
        )
        if not valid_ids:
            return {}
        await asyncio.to_thread(self.cache_dir.mkdir, parents=True, exist_ok=True)
        try:
            import aiohttp
        except ImportError:
            logger.warning("今日姻缘：缺少 aiohttp，头像使用缓存或占位。")
            await asyncio.to_thread(
                _prune_files, self.cache_dir, capacity, set(valid_ids)
            )
            return {
                user_id: self._cached_path(user_id) if ttl > 0 else None
                for user_id in valid_ids
            }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=60)
        semaphore = asyncio.Semaphore(8)
        resolved: dict[str, Path | None] = {}
        deadline = asyncio.get_running_loop().time() + self.max_total_seconds
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, raise_for_status=False
        ) as session:
            for offset in range(0, len(valid_ids), 100):
                batch = valid_ids[offset : offset + 100]
                tasks = {
                    user_id: asyncio.create_task(
                        self._get_one(session, semaphore, user_id, ttl)
                    )
                    for user_id in batch
                }
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    if remaining <= 0:
                        break
                    done, pending = await asyncio.wait(
                        tasks.values(), timeout=remaining
                    )
                finally:
                    for task in tasks.values():
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                reverse = {task: user_id for user_id, task in tasks.items()}
                for task in done:
                    user_id = reverse[task]
                    try:
                        result = task.result()
                    except Exception:
                        logger.exception("今日姻缘：头像缓存任务失败，使用缓存或占位。")
                        result = None
                    resolved[user_id] = result if isinstance(result, Path) else None
                if pending:
                    break
        await asyncio.to_thread(_prune_files, self.cache_dir, capacity, set(valid_ids))
        return {
            user_id: resolved.get(user_id)
            or (self._cached_path(user_id) if ttl > 0 else None)
            for user_id in valid_ids
        }

    async def _get_one(
        self,
        session: Any,
        semaphore: asyncio.Semaphore,
        user_id: str,
        ttl_seconds: int | None = None,
    ) -> Path | None:
        ttl = self.ttl_seconds if ttl_seconds is None else max(0, int(ttl_seconds))
        existing = self._cached_path(user_id)
        cached = existing if ttl > 0 else None
        if cached is not None and time.time() - _cache_times(cached)[0] < ttl:
            await asyncio.to_thread(_touch_cache, cached, time.time())
            return cached
        async with semaphore:
            try:
                payload = await self._download(session, user_id)
                width, height, extension = await asyncio.to_thread(
                    _inspect_avatar, payload, self.max_pixels
                )
                if width * height > self.max_pixels:
                    return cached
                target = self.cache_dir / f"{user_id}{extension}"
                downloaded_at = time.time()
                await asyncio.to_thread(_atomic_replace, target, payload)
                await asyncio.to_thread(
                    _write_cache_times, target, downloaded_at, downloaded_at
                )
                if existing is not None and existing != target:
                    existing.unlink(missing_ok=True)
                    _cache_metadata_path(existing).unlink(missing_ok=True)
                return target
            except Exception:
                logger.warning(
                    "今日姻缘：头像获取或缓存失败，使用缓存或占位。", exc_info=True
                )
                if cached is not None:
                    await asyncio.to_thread(_touch_cache, cached, time.time())
                return cached

    async def _download(self, session: Any, user_id: str) -> bytes:
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        for _ in range(4):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not _allowed_host(parsed.hostname or ""):
                raise ValueError("avatar redirect host is not allowed")
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("avatar redirect has no location")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"avatar server returned HTTP {response.status}")
                length = response.content_length
                if length is not None and length > self.max_bytes:
                    raise ValueError("avatar exceeds configured byte limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(16 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("avatar exceeds configured byte limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise ValueError("avatar redirect limit exceeded")

    def _cached_path(self, user_id: str) -> Path | None:
        for extension in _FORMAT_EXTENSIONS.values():
            path = self.cache_dir / f"{user_id}{extension}"
            if path.is_file():
                return path
        return None


def _valid_qq_id(user_id: str) -> bool:
    return user_id.isascii() and user_id.isdecimal() and 5 <= len(user_id) <= 12


def _allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in _ALLOWED_REDIRECT_HOSTS or any(host.endswith(suffix) for suffix in _ALLOWED_REDIRECT_SUFFIXES)


def _inspect_avatar(payload: bytes, max_pixels: int) -> tuple[int, int, str]:
    if not payload:
        raise ValueError("avatar is empty")
    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with Image.open(io.BytesIO(payload)) as image:
        if image.format not in _FORMAT_EXTENSIONS:
            raise ValueError("avatar image format is unsupported")
        width, height = image.size
        if width < 1 or height < 1 or width * height > max_pixels:
            raise ValueError("avatar pixel dimensions are invalid")
        return width, height, _FORMAT_EXTENSIONS[image.format]


def _atomic_replace(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".avatar-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _prune_files(
    cache_dir: Path, max_entries: int, preserve_user_ids: set[str] | None = None
) -> int:
    """Keep only the most recently used numeric QQ avatar entries."""

    if not cache_dir.is_dir():
        return 0
    entries: dict[str, list[Path]] = {}
    for path in cache_dir.iterdir():
        if path.is_file() and path.suffix.lower() in _FORMAT_EXTENSIONS.values() and _valid_qq_id(path.stem):
            entries.setdefault(path.stem, []).append(path)
    if len(entries) <= max_entries:
        return 0
    ordered = sorted(
        entries,
        key=lambda user_id: max((_cache_times(path)[1] for path in entries[user_id]), default=0),
    )
    preserve = preserve_user_ids or set()
    removed = 0
    excess = len(entries) - max_entries
    for user_id in ordered:
        if excess <= 0:
            break
        if user_id in preserve:
            continue
        for path in entries[user_id]:
            try:
                path.unlink(missing_ok=True)
                _cache_metadata_path(path).unlink(missing_ok=True)
            except OSError:
                continue
        removed += 1
        excess -= 1
    return removed


def _cache_metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".cache.json")


def _cache_times(path: Path) -> tuple[float, float]:
    fallback = path.stat().st_mtime
    try:
        value = json.loads(_cache_metadata_path(path).read_text(encoding="utf-8"))
        downloaded_at = float(value["downloaded_at"])
        accessed_at = float(value["accessed_at"])
        if math.isfinite(downloaded_at) and math.isfinite(accessed_at):
            return downloaded_at, accessed_at
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return fallback, fallback


def _write_cache_times(path: Path, downloaded_at: float, accessed_at: float) -> None:
    payload = json.dumps(
        {"downloaded_at": downloaded_at, "accessed_at": accessed_at},
        separators=(",", ":"),
    ).encode("ascii")
    _atomic_replace(_cache_metadata_path(path), payload)


def _touch_cache(path: Path, accessed_at: float) -> None:
    downloaded_at, _ = _cache_times(path)
    _write_cache_times(path, downloaded_at, accessed_at)
