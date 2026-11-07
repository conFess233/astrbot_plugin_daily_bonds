"""AstrBot 4.28 系 OneBot 11 原始事件与群成员适配。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..models.errors import StorageError
from ..models.scope import Scope
from .storage import SQLiteStorage

# 成员列表拉取失败时，最多沿用多久之前成功拉取的名单，以及失败后多久再试。
MEMBER_STALE_GRACE_SECONDS = 3600
MEMBER_RETRY_SECONDS = 30
# 群名缓存时长：改名后最迟这么久在排行标题中生效。
GROUP_NAME_TTL_SECONDS = 600


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
        self._members: dict[
            tuple[str, str, str], tuple[float, tuple[MemberRecord, ...]]
        ] = {}
        self._bot_signatures: dict[tuple[str, str, str], frozenset[str]] = {}
        # 上次成功拉取的时间，限制失败时沿用旧名单的时长。
        self._fetched_at: dict[tuple[str, str, str], float] = {}
        self._refresh_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._group_names: dict[tuple[str, str, str], tuple[float, str]] = {}

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
            observed_at = (
                int(timestamp)
                if timestamp is not None
                else int(now if now is not None else time.time())
            )
        except (TypeError, ValueError):
            observed_at = int(now if now is not None else time.time())
        return RawGroupEvent(
            raw,
            post_type,
            message_type,
            sender_id,
            group_id,
            message_id,
            segments,
            observed_at,
        )

    async def group_name(self, scope: Scope) -> str:
        """群名只用于排行标题，缓存一段时间；读取失败时沿用上次的群名。"""

        key = (scope.platform_id, scope.self_id, scope.group_id)
        cached = self._group_names.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        try:
            platform = self.context.get_platform_inst(scope.platform_id)
            info = await platform.get_client().get_group_info(group_id=scope.group_id)
            name = str(info.get("group_name") or "本群").strip() or "本群"
        except Exception:  # noqa: BLE001 - OneBot 接口边界：降级为默认群名
            logger.warning(
                "今日姻缘：读取群名失败，排行榜使用旧群名或“本群”。", exc_info=True
            )
            return cached[1] if cached else "本群"
        self._group_names[key] = (time.monotonic() + GROUP_NAME_TTL_SECONDS, name)
        return name

    async def group_members(
        self,
        scope: Scope,
        scope_id: int,
        *,
        ttl_seconds: int,
        bot_ids: set[str],
        now: int,
    ) -> tuple[MemberRecord, ...]:
        """取得或复用完整成员快照；拉取失败时在宽限期内沿用上次成功的名单。"""

        key = (scope.platform_id, scope.self_id, scope.group_id)
        cached = self._members.get(key)
        if cached and ttl_seconds > 0 and time.monotonic() < cached[0]:
            return await self._classified(key, cached, scope, bot_ids)
        # 缓存过期时同一群的并发消息只拉取一次，其余等待结果。
        async with self._refresh_locks.setdefault(key, asyncio.Lock()):
            cached = self._members.get(key)
            if cached and ttl_seconds > 0 and time.monotonic() < cached[0]:
                return await self._classified(key, cached, scope, bot_ids)
            try:
                members = await self._fetch_members(scope, bot_ids)
                await asyncio.to_thread(
                    _save_members, self.storage, scope_id, members, now
                )
            except Exception as exc:
                fetched_at = self._fetched_at.get(key)
                monotonic_now = time.monotonic()
                if (
                    cached
                    and fetched_at is not None
                    and monotonic_now - fetched_at < MEMBER_STALE_GRACE_SECONDS
                ):
                    # 短时间内不再重试，避免每条消息都等一次接口超时。
                    self._members[key] = (
                        monotonic_now + MEMBER_RETRY_SECONDS,
                        cached[1],
                    )
                    logger.warning(
                        "今日姻缘：群 %s 成员列表拉取失败，沿用 %d 秒前的名单。",
                        scope.group_id,
                        int(monotonic_now - fetched_at),
                        exc_info=True,
                    )
                    return cached[1]
                if isinstance(exc, StorageError):
                    raise
                raise StorageError(
                    f"OneBot 成员列表暂时不可用：{type(exc).__name__}"
                ) from exc
            monotonic_now = time.monotonic()
            self._members[key] = (monotonic_now + max(0, ttl_seconds), members)
            self._fetched_at[key] = monotonic_now
            self._bot_signatures[key] = frozenset(bot_ids | {scope.self_id})
            return members

    async def _classified(
        self,
        key: tuple[str, str, str],
        cached: tuple[float, tuple[MemberRecord, ...]],
        scope: Scope,
        bot_ids: set[str],
    ) -> tuple[MemberRecord, ...]:
        signature = frozenset(bot_ids | {scope.self_id})
        if self._bot_signatures.get(key) == signature:
            return cached[1]
        members = await asyncio.to_thread(
            self._refresh_bot_classification, key, cached[1], set(signature)
        )
        if self._members.get(key) is cached:
            self._members[key] = (cached[0], members)
            self._bot_signatures[key] = signature
        return members

    async def _fetch_members(
        self, scope: Scope, bot_ids: set[str]
    ) -> tuple[MemberRecord, ...]:
        platform = self.context.get_platform_inst(scope.platform_id)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise StorageError("当前平台实例不是可用的 aiocqhttp OneBot 连接。")
        get_client = getattr(platform, "get_client", None)
        if not callable(get_client):
            raise StorageError("OneBot 平台未提供 get_client 接口。")
        member_response = await get_client().get_group_member_list(
            group_id=scope.group_id
        )
        if not isinstance(member_response, list):
            raise StorageError("OneBot 成员列表响应不是数组。")
        members = tuple(
            _member(row, bot_ids | {scope.self_id})
            for row in member_response
            if isinstance(row, Mapping)
        )
        if not members:
            raise StorageError("OneBot 返回空成员列表，拒绝将整群标记为退群。")
        return members

    def invalidate_members(self, scope: Scope) -> None:
        """成员变动后下一条消息重新拉取；旧名单留作拉取失败时的回退。"""

        key = (scope.platform_id, scope.self_id, scope.group_id)
        cached = self._members.get(key)
        if cached:
            self._members[key] = (0.0, cached[1])
        self._bot_signatures.pop(key, None)

    def invalidate_all_members(self) -> None:
        self._members.clear()
        self._bot_signatures.clear()
        self._fetched_at.clear()

    def _refresh_bot_classification(
        self,
        key: tuple[str, str, str],
        members: tuple[MemberRecord, ...],
        bot_ids: set[str],
    ) -> tuple[MemberRecord, ...]:
        scope_id = self.storage.get_scope_id(Scope(*key))
        if scope_id is None:
            return members
        with self.storage.transaction() as db:
            db.executemany(
                "UPDATE members SET is_known_bot=? WHERE scope_id=? AND user_id=?",
                [
                    (int(member.user_id in bot_ids), scope_id, member.user_id)
                    for member in members
                ],
            )
        return tuple(
            MemberRecord(
                member.user_id, member.nickname, member.card, member.user_id in bot_ids
            )
            for member in members
        )


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


def _save_members(
    storage: SQLiteStorage, scope_id: int, members: Sequence[MemberRecord], now: int
) -> None:
    with storage.transaction() as db:
        db.execute(
            "UPDATE members SET is_present=0,last_observed_at=? WHERE scope_id=?",
            (now, scope_id),
        )
        db.executemany(
            """INSERT INTO members(scope_id,user_id,nickname,card,is_present,is_known_bot,last_observed_at)
               VALUES(?,?,?,?,1,?,?) ON CONFLICT(scope_id,user_id) DO UPDATE SET
               nickname=excluded.nickname,card=excluded.card,is_present=1,is_known_bot=excluded.is_known_bot,last_observed_at=excluded.last_observed_at""",
            [
                (
                    scope_id,
                    member.user_id,
                    member.nickname,
                    member.card,
                    int(member.is_known_bot),
                    now,
                )
                for member in members
            ],
        )
