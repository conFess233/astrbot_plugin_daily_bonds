"""受限的群消息模板；只允许声明过的简单变量。"""

from __future__ import annotations

import json
import string
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..models import ConfigurationError

_FORMATTER = string.Formatter()
IMAGE_TOKEN = "\ue000image\ue001"
IMAGE_KEYS = {
    "admin_set_wife",
    "draw_wife", "draw_husband", "draw_member", "designated_wife", "designated_husband",
    "designated_drawn", "capacity_full", "capacity_full_single_wife", "capacity_full_single_husband",
    "list_wife", "list_husband", "list_member", "rank_intimacy", "rank_activity", "query_affection", "list_empty",
}
EXTRA_FIELDS = {
    "capacity_full": {"name"}, "capacity_full_single_wife": {"name", "names"},
    "capacity_full_single_husband": {"name", "names"},
    "rank_intimacy": {"title", "lines", "count"}, "rank_activity": {"title", "lines", "count"},
    "query_affection": {"name", "target_name", "forward_score", "reverse_score"},
}
_REQUIRED = {
    "messages.results.draw_wife": {"name"},
    "messages.results.draw_husband": {"name"},
    "messages.results.draw_member": {"name"},
    "messages.results.capacity_full": {"names"},
    "messages.results.steal_slot_full": {"role"},
    "messages.results.invite_created": {"invite_id", "accept_keyword"},
    "messages.errors.page_out_of_range": {"pages"},
    "messages.notifications.invite_expired": {"invite_ids"},
}


def _fields(template: str, path: str) -> set[str]:
    fields: set[str] = set()
    try:
        for _, field, spec, conversion in _FORMATTER.parse(template):
            if field is None:
                continue
            if not field.isidentifier() or spec or conversion:
                raise ConfigurationError(f"{path} 仅允许简单变量，如 {{name}}")
            fields.add(field)
    except ValueError as exc:
        raise ConfigurationError(f"{path} 的大括号不匹配") from exc
    return fields


def validate_messages(messages: Mapping[str, Any], defaults: Mapping[str, Any]) -> None:
    for section, entries in defaults.items():
        for key, original in entries.items():
            path = f"messages.{section}.{key}"
            template = messages[section][key]
            if (
                not isinstance(template, str)
                or not template.strip()
                or len(template) > 1000
            ):
                raise ConfigurationError(f"{path} 必须是 1～1000 字符的非空文本")
            if IMAGE_TOKEN in template:
                raise ConfigurationError(f"{path} 不允许内部图片标记")
            if any(ord(char) < 32 and char != "\n" for char in template):
                raise ConfigurationError(f"{path} 不允许控制字符")
            fields = _fields(template, path)
            allowed = _fields(original, path)
            if key in IMAGE_KEYS and section in {"results", "errors"}:
                allowed |= {"image"} | EXTRA_FIELDS.get(key, set())
            invalid = fields - allowed
            if invalid:
                raise ConfigurationError(
                    f"{path} 含有未知变量：{', '.join(sorted(invalid))}"
                )
            missing = _REQUIRED.get(path, set()) - fields
            if section == "results" and key in IMAGE_KEYS and "image" in fields:
                missing = set()
            if missing:
                raise ConfigurationError(
                    f"{path} 缺少必要变量：{', '.join(sorted(missing))}"
                )


class MessageText(str):
    """保留模板的引用开关，兼容已有文本与序列化调用。"""

    def __new__(cls, text: str, *, quote_source: bool = False, fallback: str = ""):
        value = super().__new__(cls, text)
        value.quote_source = quote_source
        value.fallback = fallback
        return value


@lru_cache(maxsize=1)
def _default_messages() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "resources" / "default-config.json"
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def template_fields() -> dict[str, dict[str, Any]]:
    """向管理页提供同一份模板变量规则，避免前后端重复维护。"""
    result = {}
    for section, entries in _default_messages().items():
        for key, original in entries.items():
            path = f"messages.{section}.{key}"
            image = section in {"results", "errors"} and key in IMAGE_KEYS
            allowed = _fields(original, path) | (
                EXTRA_FIELDS.get(key, set()) | {"image"} if image else set()
            )
            result[path] = {
                "allowed": sorted(allowed),
                "required": sorted(_REQUIRED.get(path, set())),
                "image": image,
            }
    return result


def render_message(
    config: Mapping[str, Any], section: str, key: str, **values: Any
) -> str:
    values["image"] = IMAGE_TOKEN
    template = str(config["messages"][section][key])
    text = template.format_map(values)
    original = _default_messages()[section][key]
    fallback = original.format_map(values).replace(IMAGE_TOKEN, "").strip()
    quote = config.get("reply_quote", {}).get(section, {}).get(key, False)
    return MessageText(text, quote_source=quote, fallback=fallback)
