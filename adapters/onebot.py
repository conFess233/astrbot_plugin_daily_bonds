"""AstrBot 4.28 系 OneBot 11 原始事件与群成员适配。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..models import Scope, StorageError
from ..services.storage import SQLiteStorage


@dataclass(frozen=True, slots=True)
class MemberRecord:
    """从 OneBot 完整群成员响应归一化的身份记录。"""

    user_id: str
    nickname: str
    card: str
    is_known_bot: bool


@dataclass(frozen=True, slots=True)
class RawGroupEvent:
    """只包含插件确实需要的原始群事件字段。"""

    raw: Mapping[str, Any]
    post_type: str
    message_type: str
    sender_id: str
    group_id: str
    message_id: str
    segments: tuple[Mapping[str, Any], ...]
    observed_at: int


class OneBotAdapter:
    """封装平台实例查找、群成员快照和 OneBot 原始段提取。"""

    def __init__(self, context: Any, storage: SQLiteStorage) -> None:
        self.context = context
        self.storage = storage
        self._members: dict[tuple[str, str, str], tuple[float, tuple[MemberRecord, ...]]] = {}
        self._bot_signatures: dict[tuple[str, str, str], frozenset[str]] = {}

    @staticmethod
    def scope(event: Any) -> Scope:
        platform_id = str(event.get_platform_id() or "")
        self_id = str(event.get_self_id() or "")
        group_id = str(event.get_group_id() or "")
        if not platform_id or not self_id or not group_id:
            raise StorageError("该事件缺少平台实例、机器人或群身份。")
        return Scope(platform_id, self_id, group_id)

    @staticmethod
    def raw_group_event(event: Any, now: int | None = None) -> RawGroupEvent:
        message_object = getattr(event, "message_obj", None)
        raw = getattr(message_object, "raw_message", None)
        if not isinstance(raw, Mapping):
            raise StorageError("此 OneBot 事件没有原始载荷，已跳过命令和统计。")
        message = raw.get("message", [])
        if not isinstance(message, list):
            raise StorageError("OneBot 原始 message 不是消息段数组。")
        segments = tuple(segment for segment in message if isinstance(segment, Mapping))
        post_type = str(raw.get("post_type") or "")
        message_type = str(raw.get("message_type") or "")
        sender_id = str(raw.get("user_id") or "")
        group_id = str(raw.get("group_id") or "")
        message_id = str(raw.get("message_id") or "")
        timestamp = raw.get("time")
        try:
            observed_at = int(timestamp) if timestamp is not None else int(now if now is not None else time.time())
        except (TypeError, ValueError):
            observed_at = int(now if now is not None else time.time())
        return RawGroupEvent(raw, post_type, message_type, sender_id, group_id, message_id, segments, observed_at)

    async def group_members(
        self,
        scope: Scope,
        scope_id: int,
        *,
        ttl_seconds: int,
        bot_ids: set[str],
        now: int,
    ) -> tuple[MemberRecord, ...]:
        """取得或复用完整成员快照；失败时只回退到尚未过期的缓存。"""

        key = (scope.platform_id, scope.self_id, scope.group_id)
        cached = self._members.get(key)
        monotonic_now = time.monotonic()
        if cached and ttl_seconds > 0 and monotonic_now < cached[0]:
            self._refresh_bot_classification(key, cached[1], bot_ids | {scope.self_id})
            return self._members[key][1]
        try:
            platform = self.context.get_platform_inst(scope.platform_id)
            if platform is None or platform.meta().name != "aiocqhttp":
                raise StorageError("当前平台实例不是可用的 aiocqhttp OneBot 连接。")
            get_client = getattr(platform, "get_client", None)
            if not callable(get_client):
                raise StorageError("OneBot 平台未提供 get_client 接口。")
            client = get_client()
            member_response = await client.get_group_member_list(group_id=scope.group_id)
            if not isinstance(member_response, list):
                raise StorageError("OneBot 成员列表响应不是数组。")
            members = tuple(_member(row, bot_ids | {scope.self_id}) for row in member_response if isinstance(row, Mapping))
            if not members:
                raise StorageError("OneBot 返回空成员列表，拒绝将整群标记为退群。")
            _save_members(self.storage, scope_id, members, now)
            expiry = monotonic_now + max(0, ttl_seconds)
            self._members[key] = (expiry, members)
            self._bot_signatures[key] = frozenset(bot_ids | {scope.self_id})
            return members
        except Exception as exc:
            if cached and monotonic_now < cached[0]:
                return cached[1]
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"OneBot 成员列表暂时不可用：{type(exc).__name__}") from exc

    def invalidate_members(self, scope: Scope) -> None:
        key = (scope.platform_id, scope.self_id, scope.group_id)
        self._members.pop(key, None)
        self._bot_signatures.pop(key, None)

    def invalidate_all_members(self) -> None:
        self._members.clear()
        self._bot_signatures.clear()

    def _refresh_bot_classification(
        self, key: tuple[str, str, str], members: tuple[MemberRecord, ...], bot_ids: set[str]
    ) -> None:
        signature = frozenset(bot_ids)
        if self._bot_signatures.get(key) == signature:
            return
        scope_id = self.storage.get_scope_id(Scope(*key))
        if scope_id is None:
            return
        with self.storage.transaction() as db:
            db.executemany(
                "UPDATE members SET is_known_bot=? WHERE scope_id=? AND user_id=?",
                [(int(member.user_id in bot_ids), scope_id, member.user_id) for member in members],
            )
        self._members[key] = (
            self._members[key][0],
            tuple(
                MemberRecord(member.user_id, member.nickname, member.card, member.user_id in bot_ids)
                for member in members
            ),
        )
        self._bot_signatures[key] = signature


def mentioned_user_ids(segments: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """提取本次原始消息中的普通 @，忽略 @全体与非法 ID。"""

    result: list[str] = []
    for segment in segments:
        if segment.get("type") != "at":
            continue
        data = segment.get("data")
        qq = data.get("qq") if isinstance(data, Mapping) else None
        value = str(qq) if qq is not None else ""
        if value.isdecimal():
            result.append(value)
    return tuple(dict.fromkeys(result))


def _member(row: Mapping[str, Any], known_bots: set[str]) -> MemberRecord:
    user_id = str(row.get("user_id") or "")
    if not user_id.isdecimal():
        raise StorageError("OneBot 成员列表包含无效 QQ ID。")
    nickname = str(row.get("nickname") or user_id)
    card = str(row.get("card") or "")
    return MemberRecord(user_id, nickname, card, user_id in known_bots)


def _save_members(storage: SQLiteStorage, scope_id: int, members: Sequence[MemberRecord], now: int) -> None:
    with storage.transaction() as db:
        db.execute("UPDATE members SET is_present=0,last_observed_at=? WHERE scope_id=?", (now, scope_id))
        db.executemany(
            """INSERT INTO members(scope_id,user_id,nickname,card,is_present,is_known_bot,last_observed_at)
               VALUES(?,?,?,?,1,?,?) ON CONFLICT(scope_id,user_id) DO UPDATE SET
               nickname=excluded.nickname,card=excluded.card,is_present=1,is_known_bot=excluded.is_known_bot,last_observed_at=excluded.last_observed_at""",
            [(scope_id, member.user_id, member.nickname, member.card, int(member.is_known_bot), now) for member in members],
        )
