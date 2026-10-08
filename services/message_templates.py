"""受限的群消息模板；只允许声明过的简单变量。"""

from __future__ import annotations

import json
import string
from collections.abc import Mapping
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..models import ConfigurationError

_FORMATTER = string.Formatter()
NAME_FIELDS = {
    "actor_name",
    "owner_name",
    "sender_name",
    "recipient_name",
    "target_name",
    "subject_name",
}
FIELD_DESCRIPTIONS = {
    "actor_name": "触发指令或执行操作的用户名称",
    "owner_name": "关系原持有者名称（抢夺时为被抢者）",
    "sender_name": "赠送发起者名称",
    "recipient_name": "赠送接收者名称",
    "target_name": "指令指定或 @ 的目标群员名称",
    "subject_name": "本次操作涉及的配偶名称（角色或群友）",
    "name": "抽中、指定或查询的对象名称",
    "names": "当前持有的配偶名称列表",
    "image": "结果图片的插入位置，可代替必要文字变量",
    "role": "玩法名称：老婆、老公或群友",
    "invite_id": "当前赠送邀请编号",
    "accept_keyword": "接受赠送邀请的指令关键词",
    "limit": "待处理赠送邀请数量上限",
    "code": "操作结果代码",
    "detail": "具体错误原因",
    "remaining_seconds": "指令冷却剩余秒数",
    "pages": "列表或排行总页数",
    "page": "当前页码",
    "candidates": "名称匹配到的候选角色列表",
    "title": "当前列表或排行标题",
    "count": "列表或排行记录总数",
    "lines": "已排版的排行内容",
    "amount": "重启后失效的赠送邀请数量",
    "invite_ids": "已超时赠送邀请的编号列表",
    "details": "成员资格变化导致结束的关系或邀请明细",
    "owner_id": "原持有者 QQ 号（兼容旧文案，默认不展示）",
    "days": "活跃度统计窗口天数",
    "score": "当前记录的亲密度或有向好感度数值",
    "messages": "统计窗口内的消息数量",
    "active_days": "统计窗口内有发言的天数",
    "forward_score": "发起者对目标群员的好感度",
    "reverse_score": "目标群员对发起者的好感度",
}
MESSAGE_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "daily_bonds_reply", default=MappingProxyType({})
)


def set_message_values(**values: Any) -> None:
    """每个事件复制自己的上下文，避免并发消息串用用户名。"""
    MESSAGE_CONTEXT.set({**MESSAGE_CONTEXT.get(), **values})


IMAGE_TOKEN = "\ue000image\ue001"
IMAGE_KEYS = {
    "admin_set_wife",
    "draw_wife",
    "draw_husband",
    "draw_member",
    "designated_wife",
    "designated_husband",
    "designated_drawn",
    "capacity_full",
    "capacity_full_single_wife",
    "capacity_full_single_husband",
    "list_wife",
    "list_husband",
    "list_member",
    "rank_intimacy",
    "rank_activity",
    "query_affection",
    "list_empty",
}
EXTRA_FIELDS = {
    "member_redraw": {"owner_id"},
    "owner_ineligible": {"owner_id"},
    "capacity_full": {"name"},
    "capacity_full_single_wife": {"name", "names"},
    "capacity_full_single_husband": {"name", "names"},
    "rank_intimacy": {"title", "lines", "count"},
    "rank_activity": {"title", "lines", "count"},
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
            if not isinstance(template, str) or len(template) > 1000:
                raise ConfigurationError(
                    f"{path} 必须是最多 1000 字符的文本，留空则不发送"
                )
            if not template.strip():
                continue
            if IMAGE_TOKEN in template:
                raise ConfigurationError(f"{path} 不允许内部图片标记")
            if any(ord(char) < 32 and char != "\n" for char in template):
                raise ConfigurationError(f"{path} 不允许控制字符")
            fields = _fields(template, path)
            allowed = (
                _fields(original, path) | NAME_FIELDS | EXTRA_FIELDS.get(key, set())
            )
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
            allowed = (
                _fields(original, path)
                | NAME_FIELDS
                | EXTRA_FIELDS.get(key, set())
                | (EXTRA_FIELDS.get(key, set()) | {"image"} if image else set())
            )
            result[path] = {
                "allowed": sorted(allowed),
                "descriptions": {
                    field: FIELD_DESCRIPTIONS[field] for field in sorted(allowed)
                },
                "required": sorted(_REQUIRED.get(path, set())),
                "image": image,
            }
            if "name" in allowed and key in {
                "rank_intimacy",
                "query_affection",
                "affection_value",
            }:
                result[path]["descriptions"]["name"] = "查询或排行所属的用户名称"
            elif (
                "name" in allowed
                and key.startswith("admin_set_")
                and section == "errors"
            ):
                result[path]["descriptions"]["name"] = "指令中输入的角色名称或别名"
            elif "name" in allowed and key.startswith("capacity_full"):
                result[path]["descriptions"]["name"] = (
                    "当前持有的配偶名称（多段关系时为名称列表）"
                )
    return result


def render_message(
    config: Mapping[str, Any], section: str, key: str, **values: Any
) -> str:
    if not config.get("reply_enabled", {}).get(section, {}).get(key, True):
        return MessageText("")
    template = str(config["messages"][section][key])
    if not template.strip():
        return MessageText("")
    values = {
        **{key: "对象" if key == "subject_name" else "群友" for key in NAME_FIELDS},
        **MESSAGE_CONTEXT.get(),
        **values,
        "image": IMAGE_TOKEN,
    }
    text = template.format_map(values)
    original = _default_messages()[section][key]
    fallback = original.format_map(values).replace(IMAGE_TOKEN, "").strip()
    quote = config.get("reply_quote", {}).get(section, {}).get(key, False)
    return MessageText(text, quote_source=quote, fallback=fallback)
