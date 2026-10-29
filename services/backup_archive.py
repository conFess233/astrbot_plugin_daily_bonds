"""完整备份 ZIP 的暂存解包、路径校验与文件哈希：不访问数据库。"""

from __future__ import annotations

import hashlib
import json
import stat
import struct
import zipfile
from pathlib import Path
from typing import Any

BACKUP_FORMAT = "astrbot-daily-bonds-backup"
_MAX_PATH = 512


def stage_backup(
    source: Path, stage: Path, *, max_entries: int, max_expanded: int
) -> tuple[dict[str, Any], list[str]]:
    preflight_zip_count(source, max_entries)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > max_entries:
            raise ValueError(
                "backup entry count is empty or exceeds the configured limit"
            )
        entries: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        total = 0
        for info in infos:
            name = safe_name(info.filename)
            if info.is_dir():
                continue
            if name.casefold() in folded:
                raise ValueError("backup contains duplicate or case-colliding paths")
            folded.add(name.casefold())
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or info.flag_bits & 1:
                raise ValueError("backup may not contain symlinks or encrypted files")
            total += info.file_size
            if total > max_expanded:
                raise ValueError("backup expanded size exceeds the configured limit")
            entries[name] = info
        if "backup.json" not in entries or "database.sqlite3" not in entries:
            raise ValueError("backup must contain backup.json and database.sqlite3")
        manifest = json.loads(archive.read(entries["backup.json"]))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != BACKUP_FORMAT
            or manifest.get("schema_version") != 1
        ):
            raise ValueError("unsupported backup format or schema version")
        expected = manifest.get("files")
        if not isinstance(expected, dict) or set(expected) != set(entries) - {
            "backup.json"
        }:
            raise ValueError("backup file list does not match its manifest")
        names = []
        for name, info in entries.items():
            if name == "backup.json":
                continue
            spec = expected[name]
            if (
                not isinstance(spec, dict)
                or int(spec.get("bytes", -1)) != info.file_size
            ):
                raise ValueError(f"backup size mismatch for {name}")
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as reader, destination.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    writer.write(chunk)
            if size != info.file_size or digest.hexdigest() != spec.get("sha256"):
                raise ValueError(f"backup hash mismatch for {name}")
            names.append(name)
        return manifest, names


def preflight_zip_count(source: Path, max_entries: int) -> None:
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
        total_entries in {0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ValueError("ZIP64 archives exceed the supported entry-count envelope")
    if total_entries > max_entries:
        raise ValueError("backup entry count exceeds the configured limit")
    if central_size > min(16 * 1024 * 1024, max_entries * 4096):
        raise ValueError(
            "ZIP central directory is too large for its configured entry-count limit"
        )
    if central_offset + central_size > size - tail_size + offset:
        raise ValueError("ZIP central directory is outside the uploaded file")


def safe_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PATH
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or ":" in value
    ):
        raise ValueError("backup contains an absolute or invalid path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("backup path contains an empty, dot, or parent segment")
    return "/".join(parts)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} is empty or too long")
    return value.strip()
