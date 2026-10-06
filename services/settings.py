"""领域配置默认值、稀疏覆盖合成与服务端校验。"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import ConfigurationError
from .message_templates import validate_messages

_DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "resources" / "default-config.json"
_MODES = ("wife", "husband", "member")
_ACTIONS = (
    "draw_wife", "draw_husband", "draw_member", "steal_wife", "steal_husband",
    "steal_member", "gift_wife", "gift_husband", "gift_member", "divorce_character",
    "divorce_wife", "divorce_husband", "divorce_member", "list_characters", "list_husband",
    "list_members", "rank_intimacy", "rank_activity", "gift_accept", "gift_reject",
    "gift_cancel", "query_affection",
)


def default_config() -> dict[str, Any]:
    """返回全新副本，避免业务调用修改共享默认值。"""

    return json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))


def merge_sparse(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并对象；数组整体替换，null 不代表继承。"""

    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if key not in result:
            raise ConfigurationError(f"未知配置字段：{key}")
        if isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = merge_sparse(result[key], value)
        elif isinstance(value, Mapping) and not isinstance(result[key], dict):
            raise ConfigurationError(f"配置字段类型错误：{key}")
        else:
            result[key] = copy.deepcopy(value)
    return result


def effective_config(global_value: Mapping[str, Any], override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """合成并验证一个不可共享的生效配置快照。"""

    config = merge_sparse(global_value, override or {})
    validate_config(config)
    return config


def validate_config(config: Mapping[str, Any]) -> None:
    """校验完整领域配置，包括严格数值类型和关键词冲突。"""

    defaults = default_config()
    _check_shape(config, defaults, "config")
    if config["schema_version"] != 1 or isinstance(config["schema_version"], bool):
        raise ConfigurationError("schema_version 必须为 1")

    _enum(config, "access.groups.mode", {"unrestricted", "blacklist", "whitelist"})
    _enum(config, "access.users.mode", {"unrestricted", "blacklist", "whitelist"})
    _enum(config, "display.intimacy_rank_mode", {"personal", "group_directed"})
    for path in (
        "enabled",
        "commands.allow_bare",
        "commands.allow_leading_bot_mention",
        "commands.allow_host_prefix",
        "statistics.intimacy_enabled",
        "statistics.activity_enabled",
        "display.pagination_enabled",
        "display.intimacy_rank_enabled",
        "display.activity_rank_enabled",
    ):
        _boolean(config, path)

    for path in (
        "access.groups.ids",
        "access.users.ids",
        "access.extra_bot_ids",
        "commands.extra_prefixes",
    ):
        values = _value(config, path)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ConfigurationError(f"{path} 必须是非空字符串组成的数组")
        if len(values) != len(set(values)):
            raise ConfigurationError(f"{path} 不可包含重复项")
    for user_id in config["access"]["users"]["ids"] + config["access"]["extra_bot_ids"]:
        if not user_id.isdecimal():
            raise ConfigurationError("用户和机器人名单必须使用 QQ 数字字符串")

    timezone = config["reset"]["timezone"]
    if not isinstance(timezone, str):
        raise ConfigurationError("reset.timezone 必须是 IANA 时区名称")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(f"无效的 IANA 时区：{timezone}") from exc
    if not isinstance(config["reset"]["time"], str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", config["reset"]["time"]
    ):
        raise ConfigurationError("reset.time 必须使用 HH:mm 格式")

    ranges = {
        "commands.cooldown_seconds": (0, 86400),
        "statistics.mention_active_points": (0, 100000),
        "statistics.mention_passive_points": (0, 100000),
        "statistics.poke_active_points": (0, 100000),
        "statistics.poke_passive_points": (0, 100000),
        "statistics.activity_window_days": (1, 365),
        "display.page_size": (1, 100),
        "display.rank_merge_rows": (1, 100),
        "display.rank_max_height": (300, 16000),
        "members.cache_ttl_seconds": (0, 86400),
        "history.retention_days": (1, 3650),
        "weights.activity_full_messages": (1, 100000000),
    }
    for path, (minimum, maximum) in ranges.items():
        _integer(config, path, minimum, maximum)
    for path in (
        "weights.base",
        "weights.per_intimacy_point",
        "weights.maximum",
        "weights.activity_floor",
    ):
        _finite_number(config, path)
    weights = config["weights"]
    if not 0 < weights["base"] <= weights["maximum"] <= 1_000_000:
        raise ConfigurationError("weights 必须满足 0 < base <= maximum <= 1000000")
    if not 0 <= weights["per_intimacy_point"] <= 1_000_000:
        raise ConfigurationError("weights.per_intimacy_point 必须在 0～1000000 之间")
    if not 0 < weights["activity_floor"] <= 1:
        raise ConfigurationError("weights.activity_floor 必须在 (0,1] 之间")

    for mode in _MODES:
        current = config["modes"][mode]
        for field in ("enabled", "steal_enabled", "gift_enabled", "divorce_enabled"):
            _boolean(config, f"modes.{mode}.{field}")
        _integer(config, f"modes.{mode}.capacity", 1, 100)
        _integer(config, f"modes.{mode}.steal_slot_capacity", 0, 100)
        for field in ("steal_attempt_limit", "stolen_limit", "divorce_limit"):
            _integer(config, f"modes.{mode}.{field}", 0, 100000)
        for field in ("steal_cooldown_seconds", "gift_timeout_seconds"):
            _integer(
                config,
                f"modes.{mode}.{field}",
                0 if field.startswith("steal") else 1,
                86400,
            )
        _finite_number(config, f"modes.{mode}.steal_probability")
        if not 0 <= current["steal_probability"] <= 1:
            raise ConfigurationError(f"modes.{mode}.steal_probability 必须在 0～1 之间")
        if current["gift_mode"] not in {"direct", "confirm"}:
            raise ConfigurationError(f"modes.{mode}.gift_mode 无效")
        if mode in {"wife", "husband"}:
            _integer(config, f"modes.{mode}.designated_capacity", 0, 100)
            _boolean(config, f"modes.{mode}.designated_unique")
            pools = current["pool_ids"]
            if not isinstance(pools, list) or any(
                not isinstance(pool, str) or not pool for pool in pools
            ):
                raise ConfigurationError(f"modes.{mode}.pool_ids 必须是字符串数组")
            if len(pools) != len(set(pools)):
                raise ConfigurationError(f"modes.{mode}.pool_ids 不可重复")
        elif "pool_ids" in current:
            raise ConfigurationError("member 玩法不支持 pool_ids")

    resource_ranges = {
        "http_timeout_seconds": (1, 120),
        "image_max_bytes": (1, 52428800),
        "image_max_pixels": (1, 100000000),
        "import_max_bytes": (1, 1073741824),
        "import_max_entries": (1, 50000),
        "import_max_expanded_bytes": (1, 2147483648),
        "render_width": (320, 2000),
        "render_max_height": (1000, 16000),
        "render_concurrency": (1, 8),
        "text_chunk_chars": (100, 4000),
        "pending_invites_per_user": (1, 100),
        "avatar_cache_ttl_seconds": (0, 604800),
        "avatar_cache_max_entries": (1, 100000),
        "dedupe_retention_days": (1, 365),
        "backup_keep_count": (1, 100),
        "download_concurrency": (1, 16),
    }
    for field, bounds in resource_ranges.items():
        _integer(config, f"resources.{field}", *bounds)
    _finite_number(config, "resources.send_interval_seconds")
    if not 0 <= config["resources"]["send_interval_seconds"] <= 10:
        raise ConfigurationError("resources.send_interval_seconds 必须在 0～10 之间")
    if (
        config["resources"]["import_max_expanded_bytes"]
        < config["resources"]["import_max_bytes"]
    ):
        raise ConfigurationError("导入展开上限不能小于上传上限")

    for section, entries in config["reply_quote"].items():
        for key in entries:
            _boolean(config, f"reply_quote.{section}.{key}")
    validate_keywords(config["commands"]["keywords"])
    validate_messages(config["messages"], defaults["messages"])


def validate_keywords(keywords: Mapping[str, Any]) -> None:
    """拒绝格式危险或跨动作完全冲突的命令别名。"""

    if set(keywords) != set(_ACTIONS):
        raise ConfigurationError("commands.keywords 必须包含全部动作且不能包含未知动作")
    seen: dict[str, str] = {}
    for action in _ACTIONS:
        values = keywords[action]
        if not isinstance(values, list) or not 1 <= len(values) <= 20:
            raise ConfigurationError(f"commands.keywords.{action} 必须有 1～20 个别名")
        cleaned: set[str] = set()
        for keyword in values:
            if not isinstance(keyword, str):
                raise ConfigurationError(f"commands.keywords.{action} 别名必须是文本")
            normalized = keyword.strip()
            if normalized != keyword or not 1 <= len(keyword) <= 32 or "\n" in keyword or "\r" in keyword:
                raise ConfigurationError(f"commands.keywords.{action} 别名必须为 1～32 字符且无首尾空格/换行")
            if keyword.startswith("#") or keyword.isdecimal():
                raise ConfigurationError(f"命令别名不能与角色ID或邀请编号文法冲突：{keyword}")
            if keyword in cleaned:
                continue
            cleaned.add(keyword)
            previous = seen.get(keyword)
            if previous and previous != action:
                raise ConfigurationError(f"命令别名“{keyword}”同时指向 {previous} 和 {action}")
            seen[keyword] = action


def _check_shape(value: Any, template: Any, path: str) -> None:
    if isinstance(template, dict):
        if not isinstance(value, Mapping) or set(value) != set(template):
            raise ConfigurationError(f"{path} 的字段结构不完整或包含未知字段")
        for key, child in template.items():
            _check_shape(value[key], child, f"{path}.{key}")
    elif isinstance(template, bool):
        _expect(isinstance(value, bool), path)
    elif isinstance(template, int):
        _expect(isinstance(value, int) and not isinstance(value, bool), path)
    elif isinstance(template, float):
        _expect(isinstance(value, (int, float)) and not isinstance(value, bool), path)
    elif isinstance(template, list):
        _expect(isinstance(value, list), path)
    elif template is not None:
        _expect(isinstance(value, type(template)), path)


def _value(config: Mapping[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        current = current[part]
    return current


def _expect(condition: bool, path: str) -> None:
    if not condition:
        raise ConfigurationError(f"配置字段类型错误：{path}")


def _boolean(config: Mapping[str, Any], path: str) -> None:
    _expect(isinstance(_value(config, path), bool), path)


def _integer(config: Mapping[str, Any], path: str, minimum: int, maximum: int) -> None:
    value = _value(config, path)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{path} 必须是 {minimum}～{maximum} 的整数")


def _finite_number(config: Mapping[str, Any], path: str) -> None:
    value = _value(config, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigurationError(f"{path} 必须是有限数值")


def _enum(config: Mapping[str, Any], path: str, choices: set[str]) -> None:
    value = _value(config, path)
    if not isinstance(value, str) or value not in choices:
        raise ConfigurationError(f"{path} 必须为以下值之一：{', '.join(sorted(choices))}")
