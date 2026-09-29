"""从 OneBot 原始消息段中完整解析命令，不读取配置库或用户资料。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..models import CommandSyntaxError, ParsedCommand

_AT_TOKEN = re.compile(r"<@([0-9]+)>")
_INVITE_ID = re.compile(r"[0-9]{1,20}")
_PAGE = re.compile(r"[1-9][0-9]{0,5}")


@dataclass(frozen=True, slots=True)
class _Part:
    kind: str
    value: str


def parse_command(
    segments: Sequence[Mapping[str, Any]],
    keywords: Mapping[str, Sequence[str]],
    *,
    bot_ids: set[str] | frozenset[str] = frozenset(),
    prefixes: Sequence[str] = (),
    allow_bare: bool = True,
    allow_leading_bot_mention: bool = True,
    allow_prefix: bool = True,
) -> ParsedCommand | None:
    """解析一条消息。普通聊天返回 None，命中关键词但格式错误则报错。"""

    parts = _parts(segments)
    while parts and parts[0].kind == "reply":
        parts.pop(0)
    if not parts:
        return None
    if all(part.kind == "text" for part in parts):
        raw_text = "".join(part.value for part in parts).strip()
        admin_match = re.fullmatch(r"/设置老婆(?:\s+(.*))?", raw_text)
        if admin_match:
            name = (admin_match.group(1) or "").strip()
            if not name or len(name) > 80:
                raise CommandSyntaxError("请填写 1～80 字符的角色名或别名。")
            return ParsedCommand("set_wife", argument=name)

    if parts[0].kind == "at" and parts[0].value in bot_ids and allow_leading_bot_mention:
        parts.pop(0)
        _trim_leading_text(parts)
    prefix_set = tuple(sorted((item for item in prefixes if item), key=len, reverse=True))
    if allow_prefix:
        _consume_prefix(parts, prefix_set)
        if parts and parts[0].kind == "at" and parts[0].value in bot_ids and allow_leading_bot_mention:
            parts.pop(0)
            _trim_leading_text(parts)
            _consume_prefix(parts, prefix_set)
    leading_target: str | None = None
    if parts and parts[0].kind == "at" and parts[0].value not in bot_ids:
        leading_target = parts.pop(0).value
        _trim_leading_text(parts)
    if not allow_bare and not _was_prefixed(segments, bot_ids, prefix_set):
        return None

    rendered = "".join(_render(part) for part in parts).strip()
    if not rendered:
        return None
    candidates: list[tuple[str, str]] = []
    for action, aliases in keywords.items():
        for alias in aliases:
            if rendered == alias or rendered.startswith(alias + " ") or rendered.startswith(alias + "<@"):
                candidates.append((alias, action))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (len(item[0]), item[0]), reverse=True)
    longest = len(candidates[0][0])
    matches = [(alias, action) for alias, action in candidates if len(alias) == longest]
    if len({action for _, action in matches}) > 1:
        raise CommandSyntaxError("这条命令的关键词存在配置冲突，请联系管理员。")
    alias, action = matches[0]
    suffix = rendered[len(alias):].strip()
    if leading_target is not None:
        if action not in {"steal_wife", "steal_husband", "steal_member"}:
            raise CommandSyntaxError("此命令不接受开头的目标 @。")
        suffix = f"<@{leading_target}>" + (f" {suffix}" if suffix else "")
    return _parse_action(action, suffix)


def _parts(segments: Sequence[Mapping[str, Any]]) -> list[_Part]:
    result: list[_Part] = []
    for segment in segments:
        kind = segment.get("type")
        data = segment.get("data", {})
        if kind == "text":
            value = data.get("text", "") if isinstance(data, Mapping) else ""
            if not isinstance(value, str):
                raise CommandSyntaxError("消息文本格式无效。")
            result.append(_Part("text", value))
        elif kind == "at":
            qq = data.get("qq") if isinstance(data, Mapping) else None
            if qq == "all":
                result.append(_Part("at_other", "all"))
            elif qq is not None and str(qq).isdecimal():
                result.append(_Part("at", str(qq)))
            else:
                result.append(_Part("at_other", ""))
        elif kind == "reply":
            result.append(_Part("reply", ""))
        else:
            result.append(_Part("other", str(kind or "unknown")))
    return result


def _consume_prefix(parts: list[_Part], prefixes: Sequence[str]) -> bool:
    _trim_leading_text(parts)
    if not parts or parts[0].kind != "text":
        return False
    text = parts[0].value
    for prefix in prefixes:
        if text.startswith(prefix):
            parts[0] = _Part("text", text[len(prefix):])
            _trim_leading_text(parts)
            return True
    return False


def _trim_leading_text(parts: list[_Part]) -> None:
    while parts and parts[0].kind == "text":
        value = parts[0].value.lstrip()
        if value:
            parts[0] = _Part("text", value)
            return
        parts.pop(0)


def _was_prefixed(segments: Sequence[Mapping[str, Any]], bot_ids: set[str] | frozenset[str], prefixes: Sequence[str]) -> bool:
    parts = _parts(segments)
    while parts and parts[0].kind == "reply":
        parts.pop(0)
    if parts and parts[0].kind == "at" and parts[0].value in bot_ids:
        parts.pop(0)
        _trim_leading_text(parts)
    return bool(parts and parts[0].kind == "text" and any(parts[0].value.startswith(prefix) for prefix in prefixes))


def _render(part: _Part) -> str:
    if part.kind == "text":
        return part.value
    if part.kind == "at":
        return f"<@{part.value}>"
    if part.kind == "reply":
        return ""
    return "<UNSUPPORTED_SEGMENT>"


def _parse_action(action: str, suffix: str) -> ParsedCommand:
    if action not in {"steal_wife", "steal_husband", "steal_member", "gift_wife", "gift_husband", "gift_member", "divorce_character", "divorce_wife", "divorce_husband", "divorce_member"} and ("<@" in suffix or "<UNSUPPORTED_SEGMENT>" in suffix):
        raise CommandSyntaxError("命令中包含多余 @ 或不支持的消息段。")
    if action in {"draw_wife", "draw_husband", "draw_member"}:
        _no_suffix(action, suffix)
        return ParsedCommand(action)
    if action in {"list_characters", "list_husband", "list_members", "rank_intimacy", "rank_activity"}:
        if not suffix:
            return ParsedCommand(action)
        if not _PAGE.fullmatch(suffix):
            raise CommandSyntaxError("页码必须是正整数。")
        return ParsedCommand(action, page=int(suffix))
    if action in {"gift_accept", "gift_reject", "gift_cancel"}:
        if not suffix:
            return ParsedCommand(action)
        if not _INVITE_ID.fullmatch(suffix):
            raise CommandSyntaxError("邀请编号必须是数字。")
        return ParsedCommand(action, argument=suffix)
    if action in {"steal_wife", "steal_husband", "steal_member"}:
        target, argument = _target_and_argument(suffix, optional_argument=True)
        return ParsedCommand(action, argument=argument, target_user_id=target)
    if action in {"gift_wife", "gift_husband", "gift_member"}:
        target, argument = _target_and_argument(suffix, optional_argument=True)
        return ParsedCommand(action, argument=argument, target_user_id=target)
    if action in {"divorce_character", "divorce_wife", "divorce_husband", "divorce_member"}:
        if not suffix:
            return ParsedCommand(action)
        target, rest = _leading_at(suffix)
        if target is not None:
            if rest:
                raise CommandSyntaxError("@目标后不能再填写名称或 ID。")
            return ParsedCommand(action, target_user_id=target)
        if "<@" in suffix or "<UNSUPPORTED_SEGMENT>" in suffix:
            raise CommandSyntaxError("命令中包含多余 @ 或不支持的消息段。")
        return ParsedCommand(action, argument=suffix)
    raise CommandSyntaxError("该命令尚未配置语法。")


def _target_and_argument(suffix: str, *, optional_argument: bool) -> tuple[str, str | None]:
    target, rest = _leading_at(suffix)
    if target is None:
        match = re.match(r"^(.+?)\s+<@([0-9]+)>(?:\s+(.*))?$", suffix)
        if match:
            target, rest = match.group(2), (match.group(3) or "")
        else:
            raise CommandSyntaxError("请通过原始 @消息段 指定目标用户。")
    argument = rest.strip() or None
    if argument is not None and ("<@" in argument or "<UNSUPPORTED_SEGMENT>" in argument):
        raise CommandSyntaxError("命令中包含多余 @ 或不支持的消息段。")
    if not optional_argument and argument:
        raise CommandSyntaxError("命令包含多余参数。")
    return target, argument


def _leading_at(suffix: str) -> tuple[str | None, str]:
    match = re.match(r"^<@([0-9]+)>(?:\s+(.*))?$", suffix)
    if not match:
        return None, suffix
    return match.group(1), (match.group(2) or "").strip()


def _no_suffix(action: str, suffix: str) -> None:
    if suffix:
        raise CommandSyntaxError(f"{action} 命令不接受额外参数。")
