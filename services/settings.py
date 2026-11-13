"""领域配置默认值、稀疏覆盖合成与服务端校验。"""

from __future__ import annotations

import copy
import json
import math
import re
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models.errors import ConfigurationError
from .message_templates import validate_messages

_DEFAULTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "resources"
    / "defaults"
    / "default-config.json"
)

_MODES = ("wife", "husband", "member")
_FORMATTER = string.Formatter()
# 群级覆盖允许的顶层字段；其余字段只能全局配置。
SCOPE_OVERRIDABLE = frozenset(
    {
        "enabled",
        "access",
        "reset",
        "commands",
        "modes",
        "statistics",
        "weights",
        "display",
        "members",
        "messages",
        "render",
        "llm",
        "llm_replies",
        "sentiment",
    }
)
# 可群级覆盖的顶层字段中，仍只能全局设置的子字段（资源与批处理参数）。
GLOBAL_ONLY_FIELDS = {
    "llm": frozenset(
        {"total_timeout_seconds", "call_timeout_seconds", "max_concurrency"}
    ),
    "sentiment": frozenset(
        {
            "batch_interval_seconds",
            "batch_max_messages",
            "buffer_max_messages",
            "global_buffer_max_messages",
            "context_messages",
            "message_max_chars",
        }
    ),
}
RENDER_TYPES = ("draw", "status", "help", "list", "rank", "affection", "collection")
_ACTIONS = (
    "draw_wife",
    "draw_husband",
    "draw_member",
    "steal_wife",
    "steal_husband",
    "steal_member",
    "force_member",
    "gift_wife",
    "gift_husband",
    "gift_member",
    "divorce_character",
    "divorce_wife",
    "divorce_husband",
    "divorce_member",
    "list_characters",
    "list_husband",
    "list_members",
    "rank_intimacy",
    "rank_my_intimacy",
    "rank_activity",
    "gift_accept",
    "gift_reject",
    "gift_cancel",
    "query_affection",
    "my_status",
    "help",
    "my_collection",
    "character_info",
    "group_stats",
)


def default_config() -> dict[str, Any]:
    """返回全新副本，避免业务调用修改共享默认值。"""

    return json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))


def merge_sparse(
    base: Mapping[str, Any], override: Mapping[str, Any], *, _path: str = ""
) -> dict[str, Any]:
    """递归合并对象；数组整体替换，null 不代表继承。"""

    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if key not in result:
            raise ConfigurationError(f"未知配置字段：{key}")
        if isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = merge_sparse(
                result[key], value, _path=f"{_path}.{key}".strip(".")
            )
        elif isinstance(value, Mapping) and not isinstance(result[key], dict):
            raise ConfigurationError(f"配置字段类型错误：{key}")
        else:
            result[key] = copy.deepcopy(value)
    return result


def effective_config(
    global_value: Mapping[str, Any], override: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """合成并验证一个不可共享的生效配置快照。"""

    config = merge_sparse(global_value, override or {})
    validate_config(config)
    return config


def validate_config(config: Mapping[str, Any]) -> None:
    """校验完整领域配置，包括严格数值类型和关键词冲突。"""

    defaults = default_config()
    _check_shape(config, defaults, "config")
    if config["schema_version"] != 2 or isinstance(config["schema_version"], bool):
        raise ConfigurationError("schema_version 必须为 2")

    _enum(config, "access.groups.mode", {"unrestricted", "blacklist", "whitelist"})
    _enum(config, "access.users.mode", {"unrestricted", "blacklist", "whitelist"})
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
        "access.extra_admin_ids",
        "commands.extra_prefixes",
    ):
        values = _value(config, path)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ConfigurationError(f"{path} 必须是非空字符串组成的数组")
        if len(values) != len(set(values)):
            raise ConfigurationError(f"{path} 不可包含重复项")
    for user_id in (
        config["access"]["users"]["ids"]
        + config["access"]["extra_bot_ids"]
        + config["access"]["extra_admin_ids"]
    ):
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
        "statistics.reply_active_points": (0, 100000),
        "statistics.reply_passive_points": (0, 100000),
        "statistics.poke_active_points": (0, 100000),
        "statistics.poke_passive_points": (0, 100000),
        "statistics.activity_window_days": (1, 365),
        "statistics.intimacy_decay_percent": (0, 100),
        "statistics.intimacy_decay_idle_days": (0, 365),
        "display.page_size": (1, 100),
        "display.rank_merge_rows": (1, 100),
        "members.cache_ttl_seconds": (0, 86400),
        "history.retention_days": (1, 3650),
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
        for field in ("steal_intimacy_bonus", "steal_probability_max"):
            _finite_number(config, f"modes.{mode}.{field}")
            if not 0 <= current[field] <= 1:
                raise ConfigurationError(f"modes.{mode}.{field} 必须在 0～1 之间")
        if current["gift_mode"] not in {"direct", "confirm"}:
            raise ConfigurationError(f"modes.{mode}.gift_mode 无效")
        if mode in {"wife", "husband"}:
            _integer(config, f"modes.{mode}.designated_limit", 0, 100)
            _boolean(config, f"modes.{mode}.designated_unique")
            pools = current["pool_ids"]
            if not isinstance(pools, list) or any(
                not isinstance(pool, str) or not pool for pool in pools
            ):
                raise ConfigurationError(f"modes.{mode}.pool_ids 必须是字符串数组")
            if len(pools) != len(set(pools)):
                raise ConfigurationError(f"modes.{mode}.pool_ids 不可重复")
        else:
            if "pool_ids" in current:
                raise ConfigurationError("member 玩法不支持 pool_ids")
            _integer(config, "modes.member.force_limit", 0, 100)
            for field in (
                "force_probability",
                "force_intimacy_bonus",
                "force_probability_max",
            ):
                _finite_number(config, f"modes.member.{field}")
                if not 0 <= current[field] <= 1:
                    raise ConfigurationError(f"modes.member.{field} 必须在 0～1 之间")

    resource_ranges = {
        "http_timeout_seconds": (1, 120),
        "image_max_bytes": (1, 52428800),
        "image_max_pixels": (1, 100000000),
        "import_max_bytes": (1, 1073741824),
        "import_max_entries": (1, 50000),
        "import_max_expanded_bytes": (1, 2147483648),
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
    if (
        config["resources"]["import_max_expanded_bytes"]
        < config["resources"]["import_max_bytes"]
    ):
        raise ConfigurationError("导入展开上限不能小于上传上限")

    _validate_llm(config)
    _validate_sentiment(config)
    _enum(config, "render.mode", {"card", "text"})
    for kind in RENDER_TYPES:
        _enum(config, f"render.types.{kind}", {"inherit", "card", "text"})
    batch = config["batch_import"]
    for key in ("male_keywords", "female_keywords"):
        values = batch[key]
        if (
            not isinstance(values, list)
            or len(values) > 30
            or any(
                not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
                for value in values
            )
        ):
            raise ConfigurationError(
                f"batch_import.{key} 必须是最多30项、每项1～80字符的文本数组"
            )
    for key in ("male_pool_id", "female_pool_id"):
        if not isinstance(batch[key], str) or (
            batch[key] and not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", batch[key])
        ):
            raise ConfigurationError(f"batch_import.{key} 必须为空或有效卡池 ID")
    validate_keywords(config["commands"]["keywords"])
    validate_messages(config["messages"], defaults["messages"])


def _validate_llm(config: Mapping[str, Any]) -> None:
    _enum(config, "llm.provider_mode", {"follow", "custom"})
    _enum(config, "llm.persona_mode", {"follow", "custom", "none"})
    providers = config["llm"]["provider_ids"]
    if (
        not isinstance(providers, list)
        or len(providers) > 5
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200
            for item in providers
        )
    ):
        raise ConfigurationError("llm.provider_ids 必须是最多 5 个非空模型 ID")
    if len(providers) != len(set(providers)):
        raise ConfigurationError("llm.provider_ids 不可重复")
    if config["llm"]["provider_mode"] == "custom" and not providers:
        raise ConfigurationError("指定模型时至少选择一个模型")
    persona = config["llm"]["persona_id"]
    if not isinstance(persona, str) or len(persona) > 200:
        raise ConfigurationError("llm.persona_id 必须是最多 200 字符的文本")
    if config["llm"]["persona_mode"] == "custom" and not persona.strip():
        raise ConfigurationError("指定人格时必须选择人格")
    for path, bounds in {
        "llm.total_timeout_seconds": (1, 60),
        "llm.call_timeout_seconds": (1, 60),
        "llm.max_concurrency": (1, 16),
        "llm.group_calls_per_minute": (1, 600),
        "llm_replies.max_chars": (10, 500),
    }.items():
        _integer(config, path, *bounds)
    if config["llm"]["call_timeout_seconds"] > config["llm"]["total_timeout_seconds"]:
        raise ConfigurationError("单次调用超时不能大于总时限")
    style = config["llm_replies"]["style_prompt"]
    if not isinstance(style, str) or len(style) > 1000:
        raise ConfigurationError("llm_replies.style_prompt 最多 1000 字符")
    system = config["llm_replies"]["system_prompt"]
    if not isinstance(system, str) or len(system) > 3000:
        raise ConfigurationError("llm_replies.system_prompt 最多 3000 字符")
    try:
        fields = {
            field for _, field, _, _ in _FORMATTER.parse(system) if field is not None
        }
    except ValueError as exc:
        raise ConfigurationError(
            "llm_replies.system_prompt 的大括号不匹配；输出字面大括号请写 {{ 或 }}"
        ) from exc
    if fields - {"max_chars"}:
        raise ConfigurationError(
            "llm_replies.system_prompt 只能使用变量 {max_chars}；输出字面大括号请写 {{ 或 }}"
        )


def _validate_sentiment(config: Mapping[str, Any]) -> None:
    _boolean(config, "sentiment.enabled")
    _boolean(config, "sentiment.send_names")
    _enum(config, "sentiment.mode", {"bidirectional", "positive_only"})
    extra = config["sentiment"]["extra_prompt"]
    if not isinstance(extra, str) or len(extra) > 1000:
        raise ConfigurationError("sentiment.extra_prompt 最多 1000 字符")
    for path, bounds in {
        "sentiment.daily_cap": (0, 100000),
        "sentiment.base_points": (0, 100),
        "sentiment.diminish_after": (0, 100000),
        "sentiment.name_min_chars": (1, 32),
        "sentiment.adjacency_seconds": (0, 600),
        "sentiment.min_chars": (0, 50),
        "sentiment.batch_interval_seconds": (60, 86400),
        "sentiment.batch_max_messages": (5, 200),
        "sentiment.buffer_max_messages": (10, 1000),
        "sentiment.global_buffer_max_messages": (100, 100000),
        "sentiment.context_messages": (0, 50),
        "sentiment.message_max_chars": (20, 1000),
    }.items():
        _integer(config, path, *bounds)
    for path, (minimum, maximum) in {
        "sentiment.attitude_factor": (0, 100),
        "sentiment.passive_ratio": (0, 1),
        "sentiment.min_confidence": (0, 1),
    }.items():
        _finite_number(config, path)
        if not minimum <= _value(config, path) <= maximum:
            raise ConfigurationError(f"{path} 必须在 {minimum}～{maximum} 之间")
    current = config["sentiment"]
    if current["batch_max_messages"] > current["buffer_max_messages"]:
        raise ConfigurationError("每批条数不能大于单群缓冲上限")
    if current["buffer_max_messages"] > current["global_buffer_max_messages"]:
        raise ConfigurationError("单群缓冲上限不能大于全部群合计上限")


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
            if (
                normalized != keyword
                or not 1 <= len(keyword) <= 32
                or "\n" in keyword
                or "\r" in keyword
            ):
                raise ConfigurationError(
                    f"commands.keywords.{action} 别名必须为 1～32 字符且无首尾空格/换行"
                )
            if keyword.startswith("#") or keyword.isdecimal():
                raise ConfigurationError(
                    f"命令别名不能与角色ID或邀请编号文法冲突：{keyword}"
                )
            if keyword in cleaned:
                continue
            cleaned.add(keyword)
            previous = seen.get(keyword)
            if previous and previous != action:
                raise ConfigurationError(
                    f"命令别名“{keyword}”同时指向 {previous} 和 {action}"
                )
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
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ConfigurationError(f"{path} 必须是 {minimum}～{maximum} 的整数")


def _finite_number(config: Mapping[str, Any], path: str) -> None:
    value = _value(config, path)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ConfigurationError(f"{path} 必须是有限数值")


def _enum(config: Mapping[str, Any], path: str, choices: set[str]) -> None:
    value = _value(config, path)
    if not isinstance(value, str) or value not in choices:
        raise ConfigurationError(
            f"{path} 必须为以下值之一：{', '.join(sorted(choices))}"
        )
