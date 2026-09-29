"""受限的群消息模板；只允许声明过的简单变量。"""

from __future__ import annotations

import string
from collections.abc import Mapping
from typing import Any

from ..models import ConfigurationError

_FORMATTER = string.Formatter()
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
            if not isinstance(template, str) or not template.strip() or len(template) > 500:
                raise ConfigurationError(f"{path} 必须是 1～500 字符的非空文本")
            if any(ord(char) < 32 and char != "\n" for char in template):
                raise ConfigurationError(f"{path} 不允许控制字符")
            fields = _fields(template, path)
            invalid = fields - _fields(original, path)
            if invalid:
                raise ConfigurationError(f"{path} 含有未知变量：{', '.join(sorted(invalid))}")
            missing = _REQUIRED.get(path, set()) - fields
            if missing:
                raise ConfigurationError(f"{path} 缺少必要变量：{', '.join(sorted(missing))}")


def render_message(config: Mapping[str, Any], section: str, key: str, **values: Any) -> str:
    return str(config["messages"][section][key]).format_map(values)
