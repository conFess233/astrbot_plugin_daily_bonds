"""角色包 JSON/ZIP 的读取与字段校验：纯函数，不访问数据库。"""

from __future__ import annotations

import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

from ...models.errors import InvalidInputError
from .safe_fetch import _preflight_zip_count

_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_GENDERS = {"female", "male", "unspecified"}
_MODES = {"wife", "husband"}


def read_package(source: Path, max_bytes: int, max_entries: int, max_expanded: int):
    with source.open("rb") as stream:
        is_zip = source.stat().st_size >= 4 and stream.read(4).startswith(b"PK\x03\x04")
    if is_zip:
        _preflight_zip_count(source, max_entries)
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
        if len(infos) > max_entries:
            archive.close()
            raise ValueError("ZIP 条目数量超过 import_max_entries")
        by_name: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        expanded = 0
        for info in infos:
            name = safe_zip_name(info.filename)
            if info.is_dir():
                if name != "images" and not name.startswith("images/"):
                    archive.close()
                    raise ValueError("ZIP 只允images/ 目录")
                continue
            if name in by_name or name.casefold() in folded:
                archive.close()
                raise ValueError("ZIP 包含重复或大小写冲突的路径")
            folded.add(name.casefold())
            by_name[name] = info
            expanded += int(info.file_size)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                archive.close()
                raise ValueError("ZIP 不允许符号链接")
            if info.flag_bits & 0x1:
                archive.close()
                raise ValueError("ZIP 不允许加密条目")
            if expanded > max_expanded:
                archive.close()
                raise ValueError("ZIP 解压总大小超过限制")
            if name != "manifest.json" and not name.startswith("images/"):
                archive.close()
                raise ValueError("ZIP 只允许根 manifest.json images/ 下的素材")
        if "manifest.json" not in by_name:
            archive.close()
            raise ValueError("ZIP 根目录缺manifest.json")
        info = by_name["manifest.json"]
        if info.file_size > max_bytes:
            archive.close()
            raise ValueError("manifest.json 超过限制")
        manifest = parse_json(archive.read(info))
        return manifest, by_name, archive
    raw = source.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("JSON 包超import_max_bytes")
    return parse_json(raw), {}, None


def parse_json(raw: bytes) -> Any:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 字段重复：{key}")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8-sig"),
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"非法 JSON 常量：{value}")
        ),
    )


def safe_zip_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or ":" in value
    ):
        raise ValueError("ZIP 包含绝对路径或非法路径")
    raw_parts = value.split("/")
    allowed_trailing_slash = raw_parts[-1] == ""
    checked_parts = raw_parts[:-1] if allowed_trailing_slash else raw_parts
    if not checked_parts or any(part in {"", ".", ".."} for part in checked_parts):
        raise ValueError("ZIP 路径不能包含空段、`.` `..`")
    return "/".join(checked_parts)


def zip_image_path(value: Any, members: dict[str, zipfile.ZipInfo]) -> str:
    if not isinstance(value, str):
        raise InvalidInputError("ZIP 图片必须给出 images/ 下的相对路径")
    name = safe_zip_name(value)
    if not name.startswith("images/") or name not in members or members[name].is_dir():
        raise ValueError("清单引用的图片路径不存在或不images/ 下")
    return name


def validate_pools(
    raw: list[Any],
) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
    rows, ids, errors = [], set(), {}
    for index, pool in enumerate(raw):
        try:
            if not isinstance(pool, dict):
                raise InvalidInputError("角色池必须是 JSON 对象")
            pool_id = identifier(pool.get("id"), "角色ID")
            if pool_id in ids:
                raise ValueError("角色ID 重复")
            ids.add(pool_id)
            mode = pool.get("mode")
            if mode not in _MODES:
                raise ValueError("角色mode 必须wife husband")
            rows.append(
                {
                    "id": pool_id,
                    "name": text(pool.get("name"), "pool name", 120),
                    "mode": mode,
                    "_index": index,
                }
            )
        except (ValueError, TypeError) as exc:
            key = str(pool.get("id", index)) if isinstance(pool, dict) else str(index)
            errors[key] = str(exc)
            rows.append(
                {
                    "id": key,
                    "name": "",
                    "mode": "wife",
                    "_index": index,
                    "status": "invalid",
                }
            )
    return rows, ids, errors


def validate_character(raw: Any, duplicates: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidInputError("角色必须JSON 对象")
    character_id = identifier(raw.get("id"), "角色 ID")
    if character_id in duplicates:
        raise ValueError("同一manifest 内存在重复角ID")
    gender = raw.get("gender")
    if gender not in _GENDERS:
        raise ValueError("角色 gender 无效")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InvalidInputError("角色 enabled 必须是布尔值")
    aliases = string_list(raw.get("aliases", []), "角色别名", 30, 80)
    pool_ids = string_list(raw.get("pool_ids", []), "角色ID", 100, 128)
    images = raw.get("images", [])
    if not isinstance(images, list) or any(
        not isinstance(item, dict) for item in images
    ):
        raise ValueError("角色 images 必须是图片对象数组")
    if enabled and not images:
        raise ValueError("启用角色必须含图片")
    for image in images:
        has_url = isinstance(image.get("source_url"), str) and bool(image["source_url"])
        has_path = isinstance(image.get("path", image.get("relative_path")), str)
        if has_url == has_path:
            raise ValueError("每张图片必须恰好提供 source_url ZIP 相对 path")
    provenance = {
        key: value
        for key, value in raw.items()
        if key
        not in {"id", "name", "aliases", "gender", "enabled", "pool_ids", "images"}
    }
    if len(json.dumps(provenance, ensure_ascii=False, allow_nan=False)) > 20_000:
        raise ValueError("角色来源信息超过大小限制")
    return {
        "id": character_id,
        "name": text(raw.get("name"), "角色名称", 120),
        "aliases": aliases,
        "gender": gender,
        "enabled": enabled,
        "pool_ids": pool_ids,
        "provenance": provenance,
        "_images_raw": images,
    }


def duplicate_character_ids(characters: list[Any]) -> set[str]:
    counts: dict[str, int] = {}
    for item in characters:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            counts[item["id"]] = counts.get(item["id"], 0) + 1
    return {item for item, count in counts.items() if count > 1}


def string_list(value: Any, label: str, maximum: int, item_max: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label}数组无效或项目过多")
    result = [text(item, label, item_max) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{label}不能重复")
    return result


def identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label}格式无效")
    return value


def text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{label}不能为空且最{maximum} 个字符")
    return value.strip()
