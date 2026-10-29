"""将 OneBot 群消息路由到配置、统计和玩法服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..models.errors import CommandSyntaxError, StorageError
from ..models.gameplay import Candidate
from ..models.scope import Scope
from . import game_rules as rules
from .access import AccessService
from .admin_permissions import may_set_wife
from .avatar_cache import AvatarCache
from .cards import CardService, RenderHtml
from .command_actions import ActionHandler
from .command_features import FeatureHandler
from .command_listing import ListingHandler
from .command_parser import parse_command
from .command_replies import ReplyBuilder, RuntimeReply
from .game_status import StatusService
from .gameplay import GameplayService
from .members import reconcile_member_eligibility
from .message_templates import (
    MESSAGE_CONTEXT,
    render_message,
    set_message_values,
)
from .onebot import OneBotAdapter, mentioned_user_ids
from .statistics import StatisticsService
from .storage import SQLiteStorage

# 不属于单一玩法的查询指令：不受玩法开关限制。
GENERAL_ACTIONS = frozenset({"my_status", "help", "my_collection"})
# 玩法关闭后仍可查询，并能结束已有关系与邀请。
MODE_CLOSED_ALLOWED = frozenset(
    {
        "list_characters",
        "list_husband",
        "list_members",
        "rank_intimacy",
        "rank_my_intimacy",
        "rank_activity",
        "query_affection",
        "divorce_character",
        "divorce_wife",
        "divorce_husband",
        "divorce_member",
        "gift_accept",
        "gift_reject",
        "gift_cancel",
        *GENERAL_ACTIONS,
    }
)
# 回应邀请与管理员设置不受通用冷却限制。
COOLDOWN_EXEMPT = frozenset({"gift_accept", "gift_reject", "gift_cancel", "set_wife"})


@dataclass(frozen=True, slots=True)
class _ActionContext:
    """一条已识别指令在业务操作中共用的身份、周期与幂等信息。"""

    scope_id: int
    period: Any
    period_id: str
    actor_id: str
    eligible: frozenset[str]
    revision: str
    event_key: str
    payload_hash: str
    now: int


class CommandRuntime:
    """为每条群消息建立配置、成员、统计和玩法上下文。"""

    def __init__(
        self,
        *,
        context: Any,
        storage: SQLiteStorage,
        adapter: OneBotAdapter,
        access: AccessService,
        gameplay: GameplayService,
        statistics: StatisticsService,
        render_html: RenderHtml | None = None,
    ) -> None:
        self.context = context
        self.storage = storage
        self.adapter = adapter
        self.access = access
        self.gameplay = gameplay
        self.statistics = statistics
        self.cards = CardService(render_html)
        self.status = StatusService(storage)
        self.avatar_cache = AvatarCache(storage.database_path.parent / "avatars")
        # 回复、列表、个人查询与关系操作各自持有所需服务，跨组件调用经由属性。
        self.replies = ReplyBuilder(storage, self.cards, self.avatar_cache)
        self.listing = ListingHandler(
            storage, statistics, self.cards, self.avatar_cache, self.replies
        )
        self.features = FeatureHandler(storage, self.status, self.cards, self.replies)
        self.actions = ActionHandler(
            storage, gameplay, self.status, self.replies, self.listing, self.features
        )
        self._eligibility: dict[int, tuple[str, frozenset[str]]] = {}
        # 作用域编号与会话标识在运行期很少变化，避免每条消息都写库。
        self._scope_ids: dict[Scope, tuple[int, str]] = {}
        # 成员快照在缓存期内是同一个对象；配合设置修订号复用名称表与准入集合，
        # 大群每条消息不必重新遍历全部成员。
        self._member_views: dict[
            int,
            tuple[tuple[Any, ...], tuple[int, int], dict[str, str], frozenset[str]],
        ] = {}

    async def handle_group_message(self, event: Any) -> str | RuntimeReply | None:
        token = MESSAGE_CONTEXT.set({})
        try:
            return await self._handle_group_message(event)
        finally:
            MESSAGE_CONTEXT.reset(token)

    async def _handle_group_message(self, event: Any) -> str | RuntimeReply | None:
        if not str(getattr(event, "get_group_id", lambda: "")() or ""):
            return None
        try:
            platform = self.context.get_platform_inst(str(event.get_platform_id()))
        except Exception:  # noqa: BLE001 - 宿主接口边界：失败时跳过或降级
            logger.warning("今日姻缘：读取宿主平台失败，跳过本次事件。", exc_info=True)
            return None
        if platform is None or platform.meta().name != "aiocqhttp":
            return None
        now = int(time.time())
        scope = self.adapter.scope(event)
        raw_event = self.adapter.raw_group_event(event, now)
        if raw_event.post_type not in {"message", "notice"}:
            return None
        if raw_event.group_id != scope.group_id or not raw_event.sender_id:
            raise StorageError("OneBot 群消息身份字段不一致。")
        is_poke = (
            raw_event.post_type == "notice"
            and raw_event.raw.get("notice_type") == "notify"
            and raw_event.raw.get("sub_type") == "poke"
            and bool(str(raw_event.raw.get("target_id") or ""))
        )
        is_member_change = raw_event.post_type == "notice" and raw_event.raw.get(
            "notice_type"
        ) in {"group_decrease", "group_increase"}
        if raw_event.post_type == "notice" and not (is_poke or is_member_change):
            return None
        if raw_event.post_type == "message" and raw_event.message_type != "group":
            return None
        if is_member_change:
            self.adapter.invalidate_members(scope)

        umo = str(event.unified_msg_origin)
        cached_scope = self._scope_ids.get(scope)
        if cached_scope is not None and cached_scope[1] == umo:
            scope_id = cached_scope[0]
        else:
            scope_id = await asyncio.to_thread(
                self.storage.ensure_scope, scope, umo, now
            )
            self._scope_ids[scope] = (scope_id, umo)
        set_message_values(scope_id=scope_id)
        loaded = await asyncio.to_thread(self._load_scope_state, scope, scope_id, now)
        if loaded is None:
            return None
        global_config, global_revision, config, local_revision, period = loaded

        bot_ids = self.access.bot_ids(config, scope.self_id)
        members = await self.adapter.group_members(
            scope,
            scope_id,
            ttl_seconds=config["members"]["cache_ttl_seconds"],
            bot_ids=bot_ids,
            now=now,
        )
        names, eligible = self._member_view(
            scope_id, members, config, (global_revision, local_revision)
        )
        set_message_values(
            render=config["render"],
            actor_name=names.get(raw_event.sender_id, "群友"),
            member_names=names,
        )
        set_message_values(
            reset_at=rules.format_reset(
                int(period["ends_at"]), config["reset"]["timezone"], now
            )
        )
        eligibility = (str(period["id"]), frozenset(eligible))
        if is_member_change or self._eligibility.get(scope_id) != eligibility:
            await asyncio.to_thread(
                reconcile_member_eligibility,
                self.storage,
                scope_id=scope_id,
                period_id=eligibility[0],
                eligible_user_ids=eligible,
                now=now,
            )
            self._eligibility[scope_id] = eligibility
        if is_member_change:
            return None
        if is_poke:
            await asyncio.to_thread(
                self.statistics.record_poke,
                scope_id=scope_id,
                raw_event=raw_event.raw,
                source_id=raw_event.sender_id,
                target_id=str(raw_event.raw["target_id"]),
                eligible_user_ids=eligible,
                bot_ids=bot_ids,
                intimacy_enabled=config["statistics"]["intimacy_enabled"],
                activity_enabled=config["statistics"]["activity_enabled"],
                timezone=config["reset"]["timezone"],
                active_points=config["statistics"]["poke_active_points"],
                passive_points=config["statistics"]["poke_passive_points"],
                now=raw_event.observed_at,
            )
            return None
        stats_enabled = bool(
            config["statistics"]["intimacy_enabled"]
            or config["statistics"]["activity_enabled"]
        )
        if stats_enabled:
            await asyncio.to_thread(
                self.statistics.record_message,
                scope_id=scope_id,
                raw_event=raw_event.raw,
                sender_id=raw_event.sender_id,
                mentioned_ids=mentioned_user_ids(raw_event.segments),
                eligible_user_ids=eligible,
                bot_ids=bot_ids,
                intimacy_enabled=config["statistics"]["intimacy_enabled"],
                activity_enabled=config["statistics"]["activity_enabled"],
                active_points=config["statistics"]["mention_active_points"],
                timezone=config["reset"]["timezone"],
                passive_points=config["statistics"]["mention_passive_points"],
                now=raw_event.observed_at,
            )
        if raw_event.sender_id not in eligible:
            return None

        prefixes = list(config["commands"]["extra_prefixes"])
        if config["commands"]["allow_host_prefix"]:
            try:
                host_prefix = self.context.get_config().get("wake_prefix", [])
            except Exception:  # noqa: BLE001 - 宿主接口边界：失败时跳过或降级
                logger.warning(
                    "今日姻缘：读取宿主命令前缀失败，使用裸关键词。", exc_info=True
                )
                host_prefix = []
            if isinstance(host_prefix, str) and host_prefix:
                prefixes.append(host_prefix)
            elif isinstance(host_prefix, (list, tuple)):
                prefixes.extend(
                    prefix
                    for prefix in host_prefix
                    if isinstance(prefix, str) and prefix
                )
        try:
            command = parse_command(
                raw_event.segments,
                config["commands"]["keywords"],
                bot_ids=bot_ids,
                prefixes=prefixes,
                allow_bare=config["commands"]["allow_bare"],
                allow_leading_bot_mention=config["commands"][
                    "allow_leading_bot_mention"
                ],
                allow_prefix=True,
            )
        except CommandSyntaxError as exc:
            event.should_call_llm(True)
            return render_message(config, "errors", "syntax_error", detail=str(exc))
        if command is None:
            return None
        # 已识别为本插件指令：即使回复被关闭或事件重放而不发送，也不让唤醒消息
        # 落入宿主默认 LLM；只阻止默认 LLM，不停止其他插件。
        event.should_call_llm(True)
        if command.action == "set_wife" and not may_set_wife(
            event, raw_event.sender_id, global_config
        ):
            return render_message(config, "errors", "admin_set_denied")

        revision = f"{global_revision}:{local_revision}"
        event_key, payload_hash = _event_key(raw_event.raw)
        mode = _action_mode(command.action)
        if mode is None:
            return None
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        set_message_values(target_name=names.get(command.target_user_id, "群友"))
        mode_config = config["modes"][mode]
        if not mode_config["enabled"] and command.action not in MODE_CLOSED_ALLOWED:
            return render_message(config, "errors", "mode_closed")

        cooldown_seconds = config["commands"]["cooldown_seconds"]
        cooldown_claimed = bool(
            cooldown_seconds and command.action not in COOLDOWN_EXEMPT
        )
        if cooldown_claimed:
            allowed, remaining, replay = await asyncio.to_thread(
                self._claim_command_cooldown,
                scope_id,
                raw_event.sender_id,
                event_key,
                payload_hash,
                cooldown_seconds,
                raw_event.observed_at,
                str(period["id"]),
                revision,
            )
            if replay:
                return None
            if not allowed:
                return render_message(
                    config, "errors", "command_cooldown", remaining_seconds=remaining
                )

        ctx = _ActionContext(
            scope_id=scope_id,
            period=period,
            period_id=str(period["id"]),
            actor_id=raw_event.sender_id,
            eligible=eligible,
            revision=revision,
            event_key=event_key,
            payload_hash=payload_hash,
            now=raw_event.observed_at,
        )
        reply: str | RuntimeReply | None = None
        try:
            if command.action == "set_wife":
                reply = await self._set_wife(command, ctx, mode_config, config)
            elif command.action.startswith("draw_"):
                reply = await self._draw(command, ctx, mode, mode_config, config)
            else:
                reply = await self.actions.query_or_action(
                    command=command,
                    mode=mode,
                    scope_id=scope_id,
                    period_id=ctx.period_id,
                    period=period,
                    actor_id=raw_event.sender_id,
                    group_id=scope.group_id,
                    group_name=await self.adapter.group_name(scope)
                    if command.action in {"rank_intimacy", "rank_activity"}
                    else "",
                    config=config,
                    revision=revision,
                    event_key=event_key,
                    payload_hash=payload_hash,
                    now=raw_event.observed_at,
                    eligible=eligible,
                )
        except (StorageError, ValueError) as exc:
            reply = render_message(config, "errors", "operation_error", detail=str(exc))
        finally:
            if cooldown_claimed and not (
                isinstance(reply, RuntimeReply) and reply.starts_cooldown
            ):
                await asyncio.to_thread(
                    self._release_command_cooldown,
                    scope_id,
                    raw_event.sender_id,
                    raw_event.observed_at,
                )
        return reply

    async def _set_wife(
        self,
        command: Any,
        ctx: _ActionContext,
        mode_config: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> str | RuntimeReply | None:
        """管理员直接为自己设置今日老婆；名称不唯一时列出候选。"""

        candidates = await asyncio.to_thread(
            self._candidates, ctx.scope_id, "wife", mode_config, ctx.eligible
        )
        matches = rules.match_candidates(candidates, command.argument)
        if not matches:
            return render_message(
                config, "errors", "admin_set_missing", name=command.argument
            )
        if len(matches) > 1:
            return render_message(
                config,
                "errors",
                "admin_set_ambiguous",
                name=command.argument,
                candidates=rules.candidate_choices(matches),
            )
        target = matches[0]
        result = await asyncio.to_thread(
            self.gameplay.set_wife,
            scope_id=ctx.scope_id,
            period_id=ctx.period_id,
            owner_id=ctx.actor_id,
            character_id=target.subject_id,
            pool_ids=mode_config["pool_ids"],
            event_key=ctx.event_key,
            payload_hash=ctx.payload_hash,
            config_revision=ctx.revision,
            now=ctx.now,
        )
        if result.code == "ALREADY_HELD":
            return render_message(config, "results", "already_held", name=target.name)
        if result.code == "NO_CANDIDATE":
            return render_message(
                config, "errors", "admin_set_unavailable", name=target.name
            )
        return await self.replies.result_reply(
            result.code, {**result.data, "admin_set": True}, "wife", config
        )

    async def _draw(
        self,
        command: Any,
        ctx: _ActionContext,
        mode: str,
        mode_config: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> str | RuntimeReply | None:
        """普通抽取，或带参数时指定角色。"""

        candidates = await asyncio.to_thread(
            self._candidates, ctx.scope_id, mode, mode_config, ctx.eligible
        )
        if command.argument and mode in {"wife", "husband"}:
            selected = rules.match_candidates(candidates, command.argument)
            if not selected:
                return render_message(config, "errors", "designated_missing")
            if len(selected) > 1:
                return render_message(
                    config,
                    "errors",
                    "designated_ambiguous",
                    candidates=rules.candidate_choices(selected),
                )
            result = await asyncio.to_thread(
                self.gameplay.draw_designated,
                scope_id=ctx.scope_id,
                period_id=ctx.period_id,
                mode=mode,
                owner_id=ctx.actor_id,
                candidates=selected,
                capacity=mode_config["capacity"],
                limit=mode_config["designated_limit"],
                unique=mode_config["designated_unique"],
                pool_ids=mode_config["pool_ids"],
                event_key=ctx.event_key,
                payload_hash=ctx.payload_hash,
                config_revision=ctx.revision,
                now=ctx.now,
            )
            return await self.replies.result_reply(
                result.code, result.data, mode, config
            )
        result = await asyncio.to_thread(
            self.gameplay.draw,
            scope_id=ctx.scope_id,
            period_id=ctx.period_id,
            mode=mode,
            owner_id=ctx.actor_id,
            candidates=candidates,
            capacity=mode_config["capacity"],
            pool_ids=mode_config.get("pool_ids", ()),
            weights=config["weights"],
            activity_timezone=config["reset"]["timezone"],
            eligible_user_ids=ctx.eligible,
            activity_window_days=config["statistics"]["activity_window_days"],
            event_key=ctx.event_key,
            payload_hash=ctx.payload_hash,
            config_revision=ctx.revision,
            now=ctx.now,
        )
        data = result.data
        if result.code == "DRAWN":
            data = await self._draw_facts(
                dict(data), ctx.scope_id, ctx.period, mode, ctx.actor_id
            )
        return await self.replies.result_reply(result.code, data, mode, config)

    async def _draw_facts(
        self,
        data: dict[str, Any],
        scope_id: int,
        period: Any,
        mode: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """为抽取结果补充连续结缘天数与群友双向奔赴。"""

        subject_id = str(data["candidate"]["subject_id"])
        facts: list[str] = []
        if mode == "member" and await asyncio.to_thread(
            self.status.is_mutual,
            scope_id=scope_id,
            period_id=str(period["id"]),
            owner_id=owner_id,
            subject_id=subject_id,
        ):
            data["mutual"] = True
            facts.append("双向奔赴")
        streak = await asyncio.to_thread(
            self.status.streak,
            scope_id=scope_id,
            mode=mode,
            owner_id=owner_id,
            subject_id=subject_id,
            sequence=int(period["sequence"]),
        )
        if streak >= 2:
            facts.append(f"连续 {streak} 天")
        data["facts"] = facts
        return data

    def reset(self) -> None:
        """恢复数据库后清除与旧成员及关系状态关联的快照。"""
        self.adapter.invalidate_all_members()
        self._eligibility.clear()
        self._scope_ids.clear()
        self._member_views.clear()

    def _load_scope_state(
        self, scope: Scope, scope_id: int, now: int
    ) -> tuple[dict[str, Any], int, dict[str, Any], int, Any] | None:
        """在一次线程切换内读取两级设置并确认当前周期；本群不处理时返回 None。"""

        global_config, global_revision = self.storage.get_settings()
        if not self.access.group_allowed(global_config, scope):
            return None
        config, local_revision = self.storage.get_settings(scope_id)
        if not config["enabled"]:
            return None
        period = self.storage.ensure_current_period(
            scope_id, now, config["reset"]["timezone"], config["reset"]["time"]
        )
        return global_config, global_revision, config, local_revision, period

    def _member_view(
        self,
        scope_id: int,
        members: tuple[Any, ...],
        config: Mapping[str, Any],
        revisions: tuple[int, int],
    ) -> tuple[dict[str, str], frozenset[str]]:
        """返回成员显示名与可参与玩法的成员；快照和设置未变时复用上次结果。"""

        cached = self._member_views.get(scope_id)
        if cached is not None and cached[0] is members and cached[1] == revisions:
            return cached[2], cached[3]
        names = {
            member.user_id: member.card or member.nickname or "群友"
            for member in members
        }
        eligible = frozenset(
            member.user_id
            for member in members
            if not member.is_known_bot
            and self.access.user_allowed(config, member.user_id)
        )
        self._member_views[scope_id] = (members, revisions, names, eligible)
        return names, eligible

    def _candidates(
        self,
        scope_id: int,
        mode: str,
        mode_config: Mapping[str, Any],
        eligible: set[str],
    ) -> list[Candidate]:
        with self.storage.read() as db:
            if mode == "member":
                rows = db.execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=? AND is_present=1",
                    (scope_id,),
                ).fetchall()
                return [
                    Candidate(
                        str(row["user_id"]),
                        "member",
                        str(row["card"] or row["nickname"] or "群友"),
                    )
                    for row in rows
                    if str(row["user_id"]) in eligible
                ]
            pool_ids = list(mode_config["pool_ids"])
            if not pool_ids:
                return []
            marks = ",".join("?" for _ in pool_ids)
            rows = db.execute(
                f"""SELECT c.id,c.name,c.aliases_json,c.gender,c.revision,
                           GROUP_CONCAT(DISTINCT ci.media_hash) AS image_hashes
                    FROM characters c
                    JOIN pool_members pm ON pm.character_id=c.id
                    JOIN pools p ON p.id=pm.pool_id AND p.deleted_at IS NULL
                    LEFT JOIN character_images ci ON ci.character_id=c.id
                    WHERE c.enabled=1 AND c.deleted_at IS NULL AND p.mode=? AND p.id IN ({marks})
                    GROUP BY c.id ORDER BY c.id""",
                (mode, *pool_ids),
            ).fetchall()
        return [
            Candidate(
                str(row["id"]),
                "character",
                str(row["name"]),
                tuple(json.loads(row["aliases_json"])),
                str(row["gender"]),
                int(row["revision"]),
                tuple(filter(None, str(row["image_hashes"] or "").split(","))),
            )
            for row in rows
        ]

    def _claim_command_cooldown(
        self,
        scope_id: int,
        user_id: str,
        event_key: str,
        payload_hash: str,
        cooldown_seconds: int,
        now: int,
        period_id: str,
        config_revision: str,
    ) -> tuple[bool, int, bool]:
        with self.storage.transaction() as db:
            processed = db.execute(
                "SELECT payload_hash,command_operation_id FROM processed_events WHERE scope_id=? AND event_key=?",
                (scope_id, event_key),
            ).fetchone()
            if processed:
                if processed["payload_hash"] != payload_hash:
                    raise StorageError("命令事件键发生载荷冲突。")
                if processed["command_operation_id"]:
                    return True, 0, True
            cooldown = db.execute(
                "SELECT last_command_at FROM cooldowns WHERE scope_id=? AND user_id=?",
                (scope_id, user_id),
            ).fetchone()
            if cooldown:
                remaining = cooldown_seconds - (now - int(cooldown["last_command_at"]))
                if remaining > 0:
                    operation_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"daily-bonds:{scope_id}:{event_key}:cooldown",
                        )
                    )
                    db.execute(
                        """INSERT OR IGNORE INTO operation_log(id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,created_at)
                           VALUES(?,?,?,NULL,'command_cooldown',?,'qq','COMMAND_COOLDOWN',?,?,?)""",
                        (
                            operation_id,
                            scope_id,
                            period_id,
                            user_id,
                            config_revision,
                            json.dumps(
                                {"remaining_seconds": remaining}, ensure_ascii=False
                            ),
                            now,
                        ),
                    )
                    if processed:
                        db.execute(
                            "UPDATE processed_events SET command_operation_id=? WHERE scope_id=? AND event_key=?",
                            (operation_id, scope_id, event_key),
                        )
                    else:
                        db.execute(
                            "INSERT INTO processed_events(scope_id,event_key,payload_hash,observed_at,command_operation_id) VALUES(?,?,?,?,?)",
                            (scope_id, event_key, payload_hash, now, operation_id),
                        )
                    return False, remaining, False
            db.execute(
                """INSERT INTO cooldowns(scope_id,user_id,last_command_at) VALUES(?,?,?)
                   ON CONFLICT(scope_id,user_id) DO UPDATE SET last_command_at=excluded.last_command_at""",
                (scope_id, user_id, now),
            )
            return True, 0, False

    def _release_command_cooldown(
        self, scope_id: int, user_id: str, claimed_at: int
    ) -> None:
        with self.storage.transaction() as db:
            db.execute(
                "DELETE FROM cooldowns WHERE scope_id=? AND user_id=? AND last_command_at=?",
                (scope_id, user_id, claimed_at),
            )


def _action_mode(action: str) -> str | None:
    if action == "set_wife":
        return "wife"
    if action.endswith("wife") or action == "list_characters":
        return "wife"
    if action.endswith("husband"):
        return "husband"
    if action.endswith("member") or action == "list_members":
        return "member"
    if action in {
        "divorce_character",
        "rank_intimacy",
        "rank_my_intimacy",
        "rank_activity",
        "query_affection",
        "gift_accept",
        "gift_reject",
        "gift_cancel",
        *GENERAL_ACTIONS,
    }:
        return "wife"
    return None


def _event_key(raw: Mapping[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    message_id = str(raw.get("message_id") or "")
    return (f"message:{message_id}" if message_id else f"message-hash:{digest}"), digest
