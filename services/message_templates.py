"""受限的群消息模板；只允许声明过的简单变量。"""

from __future__ import annotations

import json
import random
import string
from collections.abc import Mapping
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..models.errors import ConfigurationError

_FORMATTER = string.Formatter()
# 每条回复（含 text）最多的预设数量，管理页 + 号使用同一上限。
MAX_REPLY_VARIANTS = 20
NAME_FIELDS = {
    "actor_name",
    "owner_name",
    "sender_name",
    "recipient_name",
    "target_name",
    "subject_name",
}
# 每条群消息都会提供的上下文变量，所有文案均可使用。
CONTEXT_FIELDS = {"reset_at"}
FIELD_DESCRIPTIONS = {
    "reset_at": "下次周期重置时间，如“明天 00:00”",
    "group_name": "当前群名称，读取失败时使用本群",
    "群名": "当前群名称（与 group_name 相同）",
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
    "invite_ids": "已超时赠送邀请的编号列表",
    "details": "成员资格变化导致结束的关系或邀请明细",
    "owner_id": "原持有者 QQ 号（兼容旧文案，默认不展示）",
    "days": "活跃度统计窗口天数",
    "score": "当前记录的亲密度数值",
    "messages": "统计窗口内的消息数量",
    "active_days": "统计窗口内有发言的天数",
    "forward_score": "发起者对目标群员的亲密度",
    "reverse_score": "目标群员对发起者的亲密度",
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
    "draw_member_mutual",
    "my_status",
    "help",
    "my_collection",
    "character_info",
    "group_stats",
    "force_success",
    "designated_wife",
    "designated_husband",
    "capacity_full",
    "capacity_full_single_wife",
    "capacity_full_single_husband",
    "list_wife",
    "list_husband",
    "list_member",
    "rank_intimacy",
    "rank_my_intimacy",
    "rank_activity",
    "query_affection",
    "list_empty",
}
EXTRA_FIELDS = {
    "member_removed": {"owner_id"},
    "owner_ineligible": {"owner_id"},
    "capacity_full": {"name"},
    "capacity_full_single_wife": {"name", "names"},
    "capacity_full_single_husband": {"name", "names"},
    "rank_intimacy": {"title", "lines", "count", "group_name", "群名"},
    "rank_my_intimacy": {"title", "lines", "count", "group_name", "群名"},
    "rank_activity": {"title", "lines", "count", "days", "group_name", "群名"},
    "rank_group_intimacy": {"group_name", "群名"},
    "query_affection": {"name", "target_name", "forward_score", "reverse_score"},
    "group_stats": {"group_name", "群名", "days"},
}
_REQUIRED = {
    "messages.results.draw_wife": {"name"},
    "messages.results.draw_husband": {"name"},
    "messages.results.draw_member": {"name"},
    "messages.results.draw_member_mutual": {"name"},
    "messages.results.capacity_full": {"names"},
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


def entry_text(entry: Any) -> str:
    """标题是纯文本，其余文案是 {text, alternatives, enabled, quote} 对象。"""

    return str(entry["text"]) if isinstance(entry, Mapping) else str(entry)


def entry_templates(entry: Any) -> list[str]:
    """返回可发送的全部文案；留空的条目不参与随机。"""

    if not isinstance(entry, Mapping):
        texts = [entry]
    else:
        texts = [entry["text"], *entry.get("alternatives", ())]
    return [str(text) for text in texts if str(text).strip()]


def _validate_template(
    template: Any, path: str, label: str, section: str, key: str, original: str
) -> None:
    if not isinstance(template, str) or len(template) > 1000:
        raise ConfigurationError(f"{label} 必须是最多 1000 字符的文本，留空则不发送")
    if not template.strip():
        return
    if IMAGE_TOKEN in template:
        raise ConfigurationError(f"{label} 不允许内部图片标记")
    if any(ord(char) < 32 and char != "\n" for char in template):
        raise ConfigurationError(f"{label} 不允许控制字符")
    fields = _fields(template, label)
    allowed = (
        _fields(original, path)
        | NAME_FIELDS
        | CONTEXT_FIELDS
        | EXTRA_FIELDS.get(key, set())
    )
    if key in IMAGE_KEYS and section in {"results", "errors"}:
        allowed |= {"image"} | EXTRA_FIELDS.get(key, set())
    invalid = fields - allowed
    if invalid:
        raise ConfigurationError(f"{label} 含有未知变量：{', '.join(sorted(invalid))}")
    missing = _REQUIRED.get(path, set()) - fields
    if section == "results" and key in IMAGE_KEYS and "image" in fields:
        missing = set()
    if missing:
        raise ConfigurationError(f"{label} 缺少必要变量：{', '.join(sorted(missing))}")


def validate_messages(messages: Mapping[str, Any], defaults: Mapping[str, Any]) -> None:
    for section, entries in defaults.items():
        for key, original_entry in entries.items():
            path = f"messages.{section}.{key}"
            entry = messages[section][key]
            original = entry_text(original_entry)
            if not isinstance(original_entry, Mapping):
                _validate_template(entry, path, path, section, key, original)
                continue
            if not isinstance(entry, Mapping) or set(entry) != set(original_entry):
                raise ConfigurationError(f"{path} 必须包含 {sorted(original_entry)}")
            for flag in set(entry) - {"text", "alternatives", "llm_prompt"}:
                if not isinstance(entry[flag], bool):
                    raise ConfigurationError(f"{path}.{flag} 必须是布尔值")
            if "llm_prompt" in entry:
                prompt = entry["llm_prompt"]
                if (
                    not isinstance(prompt, str)
                    or len(prompt) > 500
                    or any(ord(char) < 32 and char != "\n" for char in prompt)
                ):
                    raise ConfigurationError(
                        f"{path}.llm_prompt 必须是最多 500 字符、不含控制字符的文本"
                    )
            alternatives = entry["alternatives"]
            if len(alternatives) > MAX_REPLY_VARIANTS - 1:
                raise ConfigurationError(f"{path} 最多 {MAX_REPLY_VARIANTS} 条回复")
            _validate_template(entry["text"], path, path, section, key, original)
            # 编号与管理页的条目标签一致：text 是第 1 条。
            for number, template in enumerate(alternatives, start=2):
                label = f"{path} 第 {number} 条回复"
                _validate_template(template, path, label, section, key, original)


class MessageText(str):
    """保留模板的引用开关，兼容已有文本与序列化调用。"""

    def __new__(
        cls,
        text: str,
        *,
        quote_source: bool = False,
        fallback: str = "",
        llm: Mapping[str, Any] | None = None,
    ):
        value = super().__new__(cls, text)
        value.quote_source = quote_source
        value.fallback = fallback
        # 开启了 LLM 文案的回复携带生成请求，由异步回复流程替换文本。
        value.llm = llm
        value.generated = False
        return value


@lru_cache(maxsize=1)
def _default_messages() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "resources"
        / "defaults"
        / "default-config.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def template_fields() -> dict[str, dict[str, Any]]:
    """向管理页提供同一份模板变量规则，避免前后端重复维护。"""
    result = {}
    for section, entries in _default_messages().items():
        for key, original_entry in entries.items():
            original = entry_text(original_entry)
            path = f"messages.{section}.{key}"
            image = section in {"results", "errors"} and key in IMAGE_KEYS
            allowed = (
                _fields(original, path)
                | NAME_FIELDS
                | CONTEXT_FIELDS
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
                "rank_my_intimacy",
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
    entry = config["messages"][section][key]
    if isinstance(entry, Mapping) and not entry.get("enabled", True):
        return MessageText("")
    templates = entry_templates(entry)
    if not templates:
        return MessageText("")
    template = random.choice(templates)
    explicit = {**MESSAGE_CONTEXT.get(), **values}
    values = {
        "group_name": "本群",
        **{key: "对象" if key == "subject_name" else "群友" for key in NAME_FIELDS},
        "reset_at": "下次重置",
        **MESSAGE_CONTEXT.get(),
        **values,
        "image": IMAGE_TOKEN,
    }
    values["群名"] = values["group_name"]
    text = template.format_map(values)
    original = entry_text(_default_messages()[section][key])
    fallback = original.format_map(values).replace(IMAGE_TOKEN, "").strip()
    quote = bool(entry.get("quote", False)) if isinstance(entry, Mapping) else False
    llm = None
    if isinstance(entry, Mapping) and entry.get("llm") and "llm" in config:
        llm = {
            "key": key,
            "facts": {
                field: str(explicit[field])
                for field in (*NAME_FIELDS, "name")
                if explicit.get(field) not in (None, "")
            },
            # 预设文案默认不发给模型，避免照着套写或沿用其中的说法；可在管理页打开。
            "examples": _examples(templates[:5], values)
            if config["llm_replies"].get("use_examples")
            else [],
            "event_prompt": str(entry.get("llm_prompt") or "").strip(),
            "has_image": IMAGE_TOKEN in text,
            "settings": config["llm"],
            "system_prompt": config["llm_replies"]["system_prompt"],
            "style": config["llm_replies"]["style_prompt"],
            "max_chars": config["llm_replies"]["max_chars"],
        }
    return MessageText(text, quote_source=quote, fallback=fallback, llm=llm)


def _examples(templates: list[str], values: Mapping[str, Any]) -> list[str]:
    """预设文案代入本次变量后作为模型的风格示例；无法代入的条目跳过。"""

    result = []
    for template in templates:
        try:
            result.append(template.format_map(values).replace(IMAGE_TOKEN, "").strip())
        except (KeyError, ValueError, IndexError):
            continue
    return result
