"""公网图片下载、URL 与压缩包成员的安全校验。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

_REDIRECTS = {301, 302, 303, 307, 308}


class _SafeImageFetcher:
    def __init__(self, *, max_image_bytes: int, timeout: float) -> None:
        self.max_image_bytes = max_image_bytes
        self.timeout = timeout
        self.session = None

    async def fetch(self, url: str) -> tuple[bytes, str, str]:
        try:
            import aiohttp
        except ModuleNotFoundError as exc:
            raise ValueError(
                "URL 图片导入需要安requirements.txt 中声明的 aiohttp"
            ) from exc
        if self.session is None:
            connector = aiohttp.TCPConnector(
                resolver=_PublicResolver(), use_dns_cache=False, limit=4
            )
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=connector,
                trust_env=False,
            )
        current = _validate_public_url(url)
        for _attempt in range(6):
            async with self.session.get(
                current, allow_redirects=False, headers={"Accept": "image/*"}
            ) as response:
                if response.status in _REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("素材 URL 跳转缺少 Location")
                    current = _validate_public_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise ValueError(f"素材 URL 返回 HTTP {response.status}")
                response_mime = response.content_type.lower()
                if not response_mime.startswith("image/"):
                    raise ValueError("URL response did not declare an image MIME type")
                length = response.content_length
                if length is not None and length > self.max_image_bytes:
                    raise ValueError("URL 图片超过 image_max_bytes")
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self.max_image_bytes:
                        raise ValueError("URL 图片超过 image_max_bytes")
                    chunks.append(chunk)
                if not total:
                    raise ValueError("URL 图片内容为空")
                return b"".join(chunks), current, response_mime
        raise ValueError("素材 URL 重定向次数超过限制")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()


def _validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("素材 URL 无效或过长")
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("素材 URL 只允许无凭据HTTP/HTTPS 地址")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("素材 URL 端口无效") from exc
    if port not in (None, 80, 443):
        raise ValueError("素材 URL 只允许标HTTP/HTTPS 端口")
    try:
        address = ipaddress.ip_address(parts.hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("素材 URL 不能指向非公IP 地址")
    return url


def _mime_matches(declared: Any, actual: str) -> bool:
    if not isinstance(declared, str):
        return False
    normalized = declared.split(";", 1)[0].strip().lower()
    aliases = {"image/jpg": "image/jpeg", "image/x-png": "image/png"}
    return aliases.get(normalized, normalized) == actual.lower()


def _preflight_zip_count(source: Path, max_entries: int) -> None:
    size = source.stat().st_size
    tail_size = min(size, 22 + 65_535)
    with source.open("rb") as stream:
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError("ZIP end-of-directory record is missing or invalid")
    record = tail[offset : offset + 22]
    (
        _signature,
        disk,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack("<4s4H2LH", record)
    if (
        len(tail) - offset != 22 + comment_size
        or disk != 0
        or central_disk != 0
        or disk_entries != total_entries
    ):
        raise ValueError("multi-disk or malformed ZIP archives are not supported")
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ValueError("ZIP64 archives exceed the supported entry-count envelope")
    if total_entries > max_entries:
        raise ValueError("ZIP entry count exceeds import_max_entries")
    if central_size > min(16 * 1024 * 1024, max_entries * 4096):
        raise ValueError(
            "ZIP central directory is too large for its configured entry-count limit"
        )
    if central_offset + central_size > size - tail_size + offset:
        raise ValueError("ZIP central directory is outside the uploaded file")


class _PublicResolver:
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            ipaddress.ip_address(host)
            addresses = [(host, family)]
        except ValueError:
            rows = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
            addresses = [(row[4][0], row[0]) for row in rows]
        if not addresses:
            raise OSError("素材服务器域名没DNS 地址")
        result, seen = [], set()
        for address, address_family in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise OSError("素材 URL 解析到非公网地址，已拒绝以防 SSRF")
            if str(ip) in seen:
                continue
            seen.add(str(ip))
            result.append(
                {
                    "hostname": host,
                    "host": str(ip),
                    "port": port,
                    "family": address_family,
                    "proto": 0,
                    "flags": 0,
                }
            )
        return result

    async def close(self) -> None:
        return None


def _read_archive_image(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int
) -> bytes:
    with archive.open(info, "r") as stream:
        return stream.read(max_bytes + 1)
