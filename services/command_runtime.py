"""将 OneBot 群消息路由到配置、统计和玩法服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..adapters.onebot import OneBotAdapter, mentioned_user_ids
from ..models import CommandSyntaxError, ParsedCommand, Scope, StorageError
from ..utils.command_parser import parse_command
from .access import AccessService
from .avatar_cache import AvatarCache
from .gameplay import Candidate, GameplayService
from .member_lifecycle import reconcile_member_eligibility
from .message_templates import render_message
from .rendering import CardRenderer, CardRow
from .statistics import StatisticsService
from .storage import SQLiteStorage


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    text: str
    image_paths: tuple[Path, ...] = ()
    cleanup_paths: tuple[Path, ...] = ()
    starts_cooldown: bool = True


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
    ) -> None:
        self.context = context
        self.storage = storage
        self.adapter = adapter
        self.access = access
        self.gameplay = gameplay
        self.statistics = statistics
        self.renderer = CardRenderer(storage.database_path.parent / "renders")
        self.avatar_cache = AvatarCache(storage.database_path.parent / "avatars")

    async def handle_group_message(self, event: Any) -> str | RuntimeReply | None:
        if not str(getattr(event, "get_group_id", lambda: "")() or ""):
            return None
        try:
            platform = self.context.get_platform_inst(str(event.get_platform_id()))
        except Exception:
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
        is_member_change = (
            raw_event.post_type == "notice"
            and raw_event.raw.get("notice_type") in {"group_decrease", "group_increase"}
        )
        if raw_event.post_type == "notice" and not (is_poke or is_member_change):
            return None
        if raw_event.post_type == "message" and raw_event.message_type != "group":
            return None
        if is_member_change:
            self.adapter.invalidate_members(scope)

        scope_id = self.storage.ensure_scope(scope, str(event.unified_msg_origin), now)
        global_config, global_revision = self.storage.get_settings()
        if not self.access.group_allowed(global_config, scope):
            return None
        config, local_revision = self.storage.get_settings(scope_id)
        if not config["enabled"]:
            return None

        bot_ids = self.access.bot_ids(config, scope.self_id)
        members = await self.adapter.group_members(
            scope,
            scope_id,
            ttl_seconds=config["members"]["cache_ttl_seconds"],
            bot_ids=bot_ids,
            now=now,
        )
        eligible = {
            member.user_id
            for member in members
            if not member.is_known_bot and self.access.user_allowed(config, member.user_id)
        }
        period = await _thread_call(
            self.storage.ensure_current_period,
            scope_id,
            now,
            config["reset"]["timezone"],
            config["reset"]["time"],
        )
        await _thread_call(
            reconcile_member_eligibility,
            self.storage,
            scope_id=scope_id,
            period_id=str(period["id"]),
            eligible_user_ids=eligible,
            now=now,
        )
        if is_member_change:
            return None
        if is_poke:
            await _thread_call(
                self.statistics.record_poke,
                scope_id=scope_id,
                raw_event=raw_event.raw,
                source_id=raw_event.sender_id,
                target_id=str(raw_event.raw["target_id"]),
                eligible_user_ids=eligible,
                bot_ids=bot_ids,
                intimacy_enabled=config["statistics"]["intimacy_enabled"],
                active_points=config["statistics"]["poke_active_points"],
                passive_points=config["statistics"]["poke_passive_points"],
                now=raw_event.observed_at,
            )
            return None
        stats_enabled = bool(config["statistics"]["intimacy_enabled"] or config["statistics"]["activity_enabled"])
        if stats_enabled:
            await _thread_call(
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
                passive_points=config["statistics"]["mention_passive_points"],
                now=raw_event.observed_at,
            )
        if raw_event.sender_id not in eligible:
            return None

        prefixes = list(config["commands"]["extra_prefixes"])
        if config["commands"]["allow_host_prefix"]:
            try:
                host_prefix = self.context.get_config().get("wake_prefix", [])
            except Exception:
                host_prefix = []
            if isinstance(host_prefix, str) and host_prefix:
                prefixes.append(host_prefix)
            elif isinstance(host_prefix, (list, tuple)):
                prefixes.extend(prefix for prefix in host_prefix if isinstance(prefix, str) and prefix)
        try:
            command = parse_command(
                raw_event.segments,
                config["commands"]["keywords"],
                bot_ids=bot_ids,
                prefixes=prefixes,
                allow_bare=config["commands"]["allow_bare"],
                allow_leading_bot_mention=config["commands"]["allow_leading_bot_mention"],
                allow_prefix=True,
            )
        except CommandSyntaxError as exc:
            return render_message(config, "errors", "syntax_error", detail=str(exc))
        if command is None:
            return None

        revision = f"{global_revision}:{local_revision}"
        event_key, payload_hash = _event_key(raw_event.raw)
        mode = _action_mode(command.action)
        if mode is None:
            return None
        mode_config = config["modes"][mode]
        lifecycle_actions = {"divorce_character", "divorce_wife", "divorce_husband", "divorce_member", "gift_accept", "gift_reject", "gift_cancel"}
        if not mode_config["enabled"] and command.action not in {"list_characters", "list_husband", "list_members", "rank_intimacy", "rank_activity", *lifecycle_actions}:
            return render_message(config, "errors", "mode_closed")

        cooldown_seconds = config["commands"]["cooldown_seconds"]
        cooldown_claimed = bool(cooldown_seconds and command.action not in {"gift_accept", "gift_reject", "gift_cancel"})
        if cooldown_claimed:
            allowed, remaining, replay = await _thread_call(
                self._claim_command_cooldown,
                scope_id,
                raw_event.sender_id,
                event_key,
                payload_hash,
                cooldown_seconds,
                raw_event.observed_at,
                str(period["id"]),
                f"{global_revision}:{local_revision}",
            )
            if replay:
                return None
            if not allowed:
                return render_message(config, "errors", "command_cooldown", remaining_seconds=remaining)

        reply: str | RuntimeReply | None = None
        try:
            if command.action.startswith("draw_"):
                candidates = await _thread_call(self._candidates, scope_id, mode, mode_config, eligible)
                result = await _thread_call(
                    self.gameplay.draw,
                    scope_id=scope_id,
                    period_id=str(period["id"]),
                    mode=mode,
                    owner_id=raw_event.sender_id,
                    candidates=candidates,
                    capacity=mode_config["capacity"],
                    pool_ids=mode_config.get("pool_ids", ()),
                    weights=config["weights"],
                    activity_full_messages=config["weights"]["activity_full_messages"],
                    activity_window_days=config["statistics"]["activity_window_days"],
                    event_key=event_key,
                    payload_hash=payload_hash,
                    config_revision=revision,
                    now=raw_event.observed_at,
                )
                reply = await self._result_reply(result.code, result.data, mode, config)
            else:
                reply = await self._query_or_action(
                    command=command,
                    mode=mode,
                    scope_id=scope_id,
                    period_id=str(period["id"]),
                    actor_id=raw_event.sender_id,
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
            if cooldown_claimed and not (isinstance(reply, RuntimeReply) and reply.starts_cooldown):
                await _thread_call(self._release_command_cooldown, scope_id, raw_event.sender_id, raw_event.observed_at)
        return reply

    def _candidates(
        self,
        scope_id: int,
        mode: str,
        mode_config: Mapping[str, Any],
        eligible: set[str],
    ) -> list[Candidate]:
        with self.storage._lock:
            db = self.storage._connection()
            if mode == "member":
                rows = db.execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=? AND is_present=1",
                    (scope_id,),
                ).fetchall()
                return [
                    Candidate(str(row["user_id"]), "member", str(row["card"] or row["nickname"] or row["user_id"]))
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

    async def _query_or_action(self, **values: Any) -> str | RuntimeReply | None:
        command: ParsedCommand = values["command"]
        if command.action in {"list_characters", "list_husband", "list_members"}:
            return await self._list_relationships(
                values["scope_id"], values["period_id"], values["mode"], values["command"].page,
                values["config"]["display"]["page_size"], values["config"]["display"]["pagination_enabled"],
                values["config"]["resources"], values["config"],
            )
        if command.action in {"rank_intimacy", "rank_activity"}:
            return await self._rank(values)
        mode = values["mode"]
        mode_config = values["config"]["modes"][mode]
        config = values["config"]
        actor_id = values["actor_id"]
        target_id = command.target_user_id
        if command.action.startswith("steal_"):
            if not mode_config["steal_enabled"]:
                return render_message(config, "errors", "steal_disabled")
            if not target_id or target_id not in values["eligible"]:
                return render_message(config, "errors", "steal_target_required")
            relation_id = await self._resolve_relation(
                values["scope_id"], values["period_id"], mode, target_id, command.argument
            )
            if command.argument and relation_id is None:
                return render_message(config, "errors", "steal_relation_ambiguous")
            result = await _thread_call(
                self.gameplay.steal,
                scope_id=values["scope_id"], period_id=values["period_id"], mode=mode,
                actor_id=actor_id, target_id=target_id, capacity=mode_config["capacity"],
                probability=mode_config["steal_probability"], attempt_limit=mode_config["steal_attempt_limit"],
                cooldown_seconds=mode_config["steal_cooldown_seconds"],
                target_protection_limit=mode_config["stolen_limit"], relation_id=relation_id,
                steal_slot_capacity=mode_config["steal_slot_capacity"],
                event_key=values["event_key"], payload_hash=values["payload_hash"],
                config_revision=values["revision"], now=values["now"],
            )
            return await self._result_reply(result.code, result.data, mode, values["config"])
        if command.action.startswith("gift_") and command.action not in {"gift_accept", "gift_reject", "gift_cancel"}:
            if not mode_config["gift_enabled"]:
                return render_message(config, "errors", "gift_disabled")
            if not target_id or target_id not in values["eligible"]:
                return render_message(config, "errors", "gift_target_required")
            relation_id = await self._resolve_relation(
                values["scope_id"], values["period_id"], mode, actor_id, command.argument
            )
            if command.argument and relation_id is None:
                return render_message(config, "errors", "gift_relation_ambiguous")
            result = await _thread_call(
                self.gameplay.gift,
                scope_id=values["scope_id"], period_id=values["period_id"], mode=mode,
                sender_id=actor_id, recipient_id=target_id, capacity=mode_config["capacity"],
                relationship_id=relation_id, confirm=mode_config["gift_mode"] == "confirm",
                timeout_seconds=mode_config["gift_timeout_seconds"],
                pending_limit=values["config"]["resources"]["pending_invites_per_user"],
                gift_enabled=True, event_key=values["event_key"], payload_hash=values["payload_hash"],
                config_revision=values["revision"], now=values["now"],
            )
            accept_keyword = values["config"]["commands"]["keywords"]["gift_accept"][0]
            return await self._result_reply(result.code, result.data, mode, values["config"], accept_keyword=accept_keyword)
        if command.action in {"divorce_character", "divorce_wife", "divorce_husband", "divorce_member"}:
            allowed_modes = ("wife", "husband", "member") if command.action == "divorce_character" else (mode,)
            matches = await self._divorce_matches(
                values["scope_id"], values["period_id"], actor_id, allowed_modes,
                command.argument, target_id,
            )
            if not matches:
                return render_message(config, "errors", "divorce_relation_missing")
            if len(matches) > 1:
                labels = {"wife": "老婆", "husband": "老公", "member": "群友"}
                candidates = "\n".join(
                    f"- {labels[item['mode']]}：{item['name']}（{item['subject_id']}）"
                    for item in matches
                )
                return f"{render_message(config, 'errors', 'divorce_ambiguous')}\n{candidates}"
            if matches[0]["slot_kind"] == "steal":
                return render_message(config, "results", "relationship_locked")
            mode = str(matches[0]["mode"])
            relation_id = str(matches[0]["id"])
            mode_config = config["modes"][mode]
            if not mode_config["divorce_enabled"]:
                return render_message(config, "errors", "divorce_disabled")
            result = await _thread_call(
                self.gameplay.divorce,
                scope_id=values["scope_id"], period_id=values["period_id"], mode=mode,
                owner_id=actor_id, divorce_limit=mode_config["divorce_limit"],
                relationship_id=relation_id, event_key=values["event_key"],
                payload_hash=values["payload_hash"], config_revision=values["revision"], now=values["now"],
            )
            return await self._result_reply(result.code, result.data, mode, values["config"])
        if command.action in {"gift_accept", "gift_reject", "gift_cancel"}:
            action = {"gift_accept": "accept", "gift_reject": "reject", "gift_cancel": "cancel"}[command.action]
            invite_id = command.argument
            if invite_id is None:
                invite_id = await self._single_pending_invite(values["scope_id"], actor_id, action)
                if invite_id == "multiple":
                    return render_message(config, "errors", "invite_multiple")
            if invite_id is None:
                return render_message(config, "errors", "invite_none")
            invite = await self._invite(values["scope_id"], invite_id)
            if invite is None:
                return render_message(config, "errors", "invite_invalid_id")
            mode = str(invite["mode"])
            mode_config = values["config"]["modes"][mode]
            eligible = values["eligible"]
            result = await _thread_call(
                self.gameplay.answer_invite,
                scope_id=values["scope_id"], period_id=values["period_id"], invite_id=invite_id,
                actor_id=actor_id, action=action, capacity=mode_config["capacity"],
                mode_enabled=mode_config["enabled"], gift_enabled=mode_config["gift_enabled"],
                sender_allowed=str(invite["sender_id"]) in eligible if action == "accept" else True,
                recipient_allowed=str(invite["recipient_id"]) in eligible if action == "accept" else True,
                event_key=values["event_key"], payload_hash=values["payload_hash"],
                config_revision=values["revision"], now=values["now"],
            )
            return await self._result_reply(result.code, result.data, mode, values["config"])
        return None

    async def _divorce_matches(
        self, scope_id: int, period_id: str, owner_id: str, modes: tuple[str, ...],
        query: str | None, target_user_id: str | None,
    ) -> list[dict[str, str]]:
        marks = ",".join("?" for _ in modes)
        with self.storage._lock:
            rows = self.storage._connection().execute(
                f"""SELECT r.id,r.mode,r.subject_id,r.slot_kind,s.name,s.aliases_json,
                           m.nickname,m.card
                    FROM relationships r
                    JOIN subject_snapshots s ON s.id=r.snapshot_id
                    LEFT JOIN members m ON m.scope_id=r.scope_id AND m.user_id=r.subject_id
                       AND m.is_present=1 AND r.mode='member'
                    WHERE r.scope_id=? AND r.period_id=? AND r.owner_id=? AND r.state='active'
                      AND r.mode IN ({marks})
                    ORDER BY r.acquired_at,r.id""",
                (scope_id, period_id, owner_id, *modes),
            ).fetchall()
        matches: list[dict[str, str]] = []
        normalized = query.removeprefix("#") if query else None
        for row in rows:
            if target_user_id is not None:
                selected = row["mode"] == "member" and str(row["subject_id"]) == target_user_id
            elif query is None:
                selected = row["slot_kind"] == "normal"
            else:
                names = {str(row["name"]), *json.loads(row["aliases_json"])}
                if row["mode"] == "member":
                    names.update(str(row[key]) for key in ("nickname", "card") if row[key])
                selected = str(row["subject_id"]) == normalized or query in names
            if selected:
                matches.append({key: str(row[key]) for key in ("id", "mode", "subject_id", "name", "slot_kind")})
        return matches

    async def _resolve_relation(
        self, scope_id: int, period_id: str, mode: str, owner_id: str, query: str | None
    ) -> str | None:
        with self.storage._lock:
            rows = self.storage._connection().execute(
                """SELECT r.id,r.subject_id,s.name,s.aliases_json FROM relationships r
                   JOIN subject_snapshots s ON s.id=r.snapshot_id
                   WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=? AND r.state='active'
                   ORDER BY r.acquired_at,r.id""",
                (scope_id, period_id, mode, owner_id),
            ).fetchall()
        if not query:
            return None
        normalized = query.removeprefix("#")
        matches = [
            str(row["id"])
            for row in rows
            if str(row["subject_id"]) == normalized
            or query == str(row["name"])
            or query in json.loads(row["aliases_json"])
        ]
        return matches[0] if len(matches) == 1 else None

    async def _single_pending_invite(self, scope_id: int, actor_id: str, action: str) -> str | None:
        column = "sender_id" if action == "cancel" else "recipient_id"
        with self.storage._lock:
            rows = self.storage._connection().execute(
                f"SELECT id FROM gift_invites WHERE scope_id=? AND {column}=? AND state='pending' ORDER BY created_at,id",
                (scope_id, actor_id),
            ).fetchall()
        if len(rows) > 1:
            return "multiple"
        return str(rows[0]["id"]) if rows else None

    async def _invite(self, scope_id: int, invite_id: str):
        with self.storage._lock:
            row = self.storage._connection().execute(
                "SELECT * FROM gift_invites WHERE scope_id=? AND id=?", (scope_id, invite_id)
            ).fetchone()
        return row

    async def _user_allowed(self, config: Mapping[str, Any], user_id: str) -> bool:
        return bool(user_id) and self.access.user_allowed(config, user_id)

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
                    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"daily-bonds:{scope_id}:{event_key}:cooldown"))
                    db.execute(
                        """INSERT OR IGNORE INTO operation_log(id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,created_at)
                           VALUES(?,?,?,NULL,'command_cooldown',?,'qq','COMMAND_COOLDOWN',?,?,?)""",
                        (operation_id, scope_id, period_id, user_id, config_revision,
                         json.dumps({"remaining_seconds": remaining}, ensure_ascii=False), now),
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

    def _release_command_cooldown(self, scope_id: int, user_id: str, claimed_at: int) -> None:
        with self.storage.transaction() as db:
            db.execute(
                "DELETE FROM cooldowns WHERE scope_id=? AND user_id=? AND last_command_at=?",
                (scope_id, user_id, claimed_at),
            )

    async def _rank(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        command: ParsedCommand = values["command"]
        config = values["config"]
        if command.action == "rank_intimacy":
            if not config["display"]["intimacy_rank_enabled"]:
                return render_message(config, "errors", "rank_intimacy_disabled")
            global_directed = config["display"]["intimacy_rank_mode"] == "group_directed"
            with self.storage._lock:
                rows = self.storage._connection().execute(
                    "SELECT source_id,target_id,score FROM intimacy_edges WHERE scope_id=? ORDER BY score DESC,source_id,target_id",
                    (values["scope_id"],),
                ).fetchall()
            if global_directed:
                ranked = [(str(row["source_id"]), str(row["target_id"]), int(row["score"])) for row in rows
                          if row["source_id"] in values["eligible"] and row["target_id"] in values["eligible"]]
                title = "全群有向亲密度排行"
            else:
                ranked = [(values["actor_id"], str(row["target_id"]), int(row["score"])) for row in rows
                          if row["source_id"] == values["actor_id"] and row["target_id"] in values["eligible"]]
                title = "个人亲密度排行"
        else:
            if not config["display"]["activity_rank_enabled"]:
                return render_message(config, "errors", "rank_activity_disabled")
            if not config["statistics"]["activity_enabled"]:
                return render_message(config, "errors", "activity_disabled")
            threshold = values["now"] - config["statistics"]["activity_window_days"] * 86400
            with self.storage._lock:
                rows = self.storage._connection().execute(
                    """SELECT user_id,SUM(message_count) AS messages FROM activity_seconds
                       WHERE scope_id=? AND observed_second>? AND observed_second<=?
                       GROUP BY user_id ORDER BY messages DESC,user_id""",
                    (values["scope_id"], threshold, values["now"]),
                ).fetchall()
            ranked = [(str(row["user_id"]), "", int(row["messages"])) for row in rows
                      if row["user_id"] in values["eligible"]]
            title = f"近 {config['statistics']['activity_window_days']} 天活跃度排行"
        with self.storage._lock:
            member_rows = self.storage._connection().execute(
                "SELECT user_id,nickname,card FROM members WHERE scope_id=?", (values["scope_id"],)
            ).fetchall()
        names = {str(row["user_id"]): str(row["nickname"] or row["card"] or row["user_id"]) for row in member_rows}
        page_size = config["display"]["page_size"]
        page = command.page or 1
        pages = max(1, (len(ranked) + page_size - 1) // page_size)
        if config["display"]["pagination_enabled"] and page > pages:
            return render_message(config, "errors", "page_out_of_range", pages=pages)
        selected = ranked if not config["display"]["pagination_enabled"] else ranked[(page - 1) * page_size : page * page_size]
        avatar_user_ids = {source for source, _, _ in selected}
        if command.action == "rank_intimacy":
            avatar_user_ids.update(target for _, target, _ in selected if target)
        resources = config["resources"]
        avatars = await self.avatar_cache.get_many(
            list(avatar_user_ids), ttl_seconds=resources["avatar_cache_ttl_seconds"],
            max_entries=resources["avatar_cache_max_entries"],
        ) if self.renderer.has_cjk_font else {}
        lines = []
        card_rows: list[CardRow] = []
        for index, (source, target, score) in enumerate(selected, start=(page - 1) * page_size + 1 if config["display"]["pagination_enabled"] else 1):
            if command.action == "rank_intimacy":
                subject = f"{names.get(source, source)} → {names.get(target, target)}" if global_directed else names.get(target, target)
                qq_ids = f"{source} → {target}" if global_directed else target
            else:
                subject = names.get(source, source)
                qq_ids = source
            lines.append(f"{index}. {subject}（QQ {qq_ids}）：{score}")
            value_label = "亲密度" if command.action == "rank_intimacy" else "消息数"
            avatar = avatars.get(target if command.action == "rank_intimacy" and not global_directed else source)
            other_avatar = avatars.get(target) if command.action == "rank_intimacy" and global_directed else None
            card_rows.append(CardRow(
                f"{index}. {subject}",
                f"{value_label}：{score}" if command.action == "rank_intimacy" and global_directed else f"QQ {qq_ids} · {value_label}：{score}",
                avatar_path=avatar, other_avatar_path=other_avatar,
                avatar_user_id=target if command.action == "rank_intimacy" and not global_directed else source,
                other_avatar_user_id=target if command.action == "rank_intimacy" and global_directed else "",
                pair_names=(names.get(source, source), names.get(target, target)) if command.action == "rank_intimacy" and global_directed else None,
                pair_rank=index if command.action == "rank_intimacy" and global_directed else 0,
            ))
        heading = f"{title} 第 {page}/{pages} 页" if config["display"]["pagination_enabled"] else title
        if command.page is not None and not config["display"]["pagination_enabled"]:
            heading += "（分页已关闭，返回全部）"
        text = f"{heading}（{len(ranked)}）\n" + ("\n".join(lines) if lines else "暂无记录。")
        return await self._reply_with_card(text, heading, card_rows)

    async def _list_relationships(
        self,
        scope_id: int,
        period_id: str,
        mode: str,
        page: int | None,
        page_size: int,
        pagination_enabled: bool,
        resources: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> str | RuntimeReply:
        if config is None:
            config = self.storage.get_settings(scope_id)[0]
        if resources is None:
            resources = config["resources"]
        with self.storage._lock:
            modes = (mode,)
            marks = ",".join("?" for _ in modes)
            rows = self.storage._connection().execute(
                """SELECT r.mode,r.owner_id,r.subject_id,r.slot_kind,s.id AS snapshot_id,s.name,s.subject_kind,
                          (SELECT GROUP_CONCAT(mb.relative_path,'|') FROM snapshot_images si
                           JOIN media_blobs mb ON mb.hash=si.media_hash WHERE si.snapshot_id=s.id) AS image_paths
                   FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
                   WHERE r.scope_id=? AND r.period_id=? AND r.mode IN (""" + marks + ") AND r.state='active' "
                   "ORDER BY r.mode,r.owner_id,r.acquired_at,r.id",
                (scope_id, period_id, *modes),
            ).fetchall()
            members = {
                str(row["user_id"]): str(row["card"] or row["nickname"] or row["user_id"])
                for row in self.storage._connection().execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=?", (scope_id,)
                ).fetchall()
            }
        mode_labels = {"wife": "老婆", "husband": "老公", "member": "群友"}
        entries = [
            f"[{mode_labels[str(row['mode'])]}{'·抢夺' if row['slot_kind'] == 'steal' else ''}] {members.get(str(row['owner_id']), row['owner_id'])} → {row['name']}"
            for row in rows
        ]
        avatar_ids = list(dict.fromkeys(
            user_id for row in rows
            for user_id in ([str(row["owner_id"]), str(row["subject_id"])] if mode == "member" else [str(row["owner_id"])])
        ))
        avatars = await self.avatar_cache.get_many(
            avatar_ids, ttl_seconds=resources["avatar_cache_ttl_seconds"],
            max_entries=resources["avatar_cache_max_entries"],
        ) if self.renderer.has_cjk_font else {}
        card_rows = [
            CardRow(
                primary=f"{members.get(str(row['owner_id']), row['owner_id'])} → {row['name']}",
                secondary=f"{mode_labels[str(row['mode'])]} · {'抢夺' if row['slot_kind'] == 'steal' else '普通'} · {row['subject_kind']}",
                image_path=self._snapshot_image_path(row["image_paths"]) if mode != "member" else None,
                avatar_path=avatars.get(str(row["owner_id"])),
                avatar_user_id=str(row["owner_id"]),
                other_avatar_path=avatars.get(str(row["subject_id"])) if mode == "member" else None,
                other_avatar_user_id=str(row["subject_id"]) if mode == "member" else "",
                pair_names=(members.get(str(row["owner_id"]), str(row["owner_id"])),
                            members.get(str(row["subject_id"]), str(row["name"]))) if mode == "member" else None,
            )
            for row in rows
        ]
        title = {"wife": "老婆列表", "husband": "老公列表", "member": "群友配偶列表"}[mode]
        if not entries:
            return await self._reply_with_card(render_message(config, "errors", "list_empty", title=title), title, ())
        if not pagination_enabled:
            selected = entries
            if page is not None:
                title += "（分页已关闭，返回全部）"
        else:
            pages = max(1, (len(entries) + page_size - 1) // page_size)
            selected_page = page or 1
            if selected_page > pages:
                return render_message(config, "errors", "page_out_of_range", pages=pages)
            start = (selected_page - 1) * page_size
            selected = entries[start : start + page_size]
            title += f" 第 {selected_page}/{pages} 页"
        text = f"{title}（共 {len(entries)}）\n" + "\n".join(selected)
        selected_rows = card_rows if not pagination_enabled else card_rows[(selected_page - 1) * page_size : selected_page * page_size]
        return await self._reply_with_card(text, title, selected_rows)

    async def _reply_with_card(self, text: str, title: str, rows: Sequence[CardRow]) -> RuntimeReply:
        try:
            images = await asyncio.to_thread(self.renderer.render, title, rows, cache_key=str(uuid.uuid4()))
        except Exception:
            logger.exception("Failed to render a list card; returning its text version")
            images = ()
        return RuntimeReply(text, images, images)

    async def _result_reply(
        self, code: str, data: Mapping[str, Any], mode: str, config: Mapping[str, Any],
        *, accept_keyword: str = "接受赠送",
    ) -> str | RuntimeReply:
        text = self._format_result(code, data, mode, config, accept_keyword=accept_keyword)
        if code == "CAPACITY_FULL" and mode in {"wife", "husband"}:
            if config["modes"][mode]["capacity"] == 1:
                text = render_message(config, "results", f"capacity_full_single_{mode}")
            return RuntimeReply(text, starts_cooldown=False)
        if code == "CAPACITY_FULL" and data.get("relationships"):
            relationships = data["relationships"]
            names = [str(item["name"]) for item in relationships]
            text = render_message(config, "results", "capacity_full", names="、".join(names))
            details = [self._relationship_display(str(item["id"])) for item in relationships]
            member_ids = [item["subject_id"] for item in details if item and item["subject_kind"] == "member"]
            avatars = await self._avatars(member_ids, config)
            rows = [
                CardRow(
                    primary=str(item["name"]),
                    secondary={"wife": "老婆", "husband": "老公", "member": "群友"}[mode],
                    image_path=self._snapshot_image_path(item["image_paths"]),
                    avatar_path=avatars.get(item["subject_id"]),
                )
                for item in details if item
            ]
            card = await self._reply_with_card(text, "已有关系", rows)
            return RuntimeReply(card.text, card.image_paths, card.cleanup_paths, starts_cooldown=False)
        if code == "DRAWN":
            item = self._relationship_display(str(data["relationship_id"]))
            if item is None:
                return RuntimeReply(text)
            if item["subject_kind"] == "member":
                image_path = (await self._avatars([item["subject_id"]], config)).get(item["subject_id"])
            else:
                image_path = self._snapshot_image_path(item["image_paths"])
            if image_path is not None:
                return RuntimeReply(text, (image_path,))
        if code in {"DRAWN", "STEAL_FAILED", "STOLEN", "DIVORCED", "GIFTED", "INVITE_CREATED", "GIFT_ACCEPTED", "INVITE_REJECTED", "INVITE_CANCELLED"}:
            return RuntimeReply(text)
        return text

    def _relationship_display(self, relationship_id: str) -> dict[str, Any] | None:
        with self.storage._lock:
            row = self.storage._connection().execute(
                """SELECT r.subject_id,r.subject_kind,s.name,
                          (SELECT GROUP_CONCAT(mb.relative_path,'|') FROM snapshot_images si
                           JOIN media_blobs mb ON mb.hash=si.media_hash WHERE si.snapshot_id=s.id) AS image_paths
                   FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id WHERE r.id=?""",
                (relationship_id,),
            ).fetchone()
        return dict(row) if row else None

    async def _avatars(self, user_ids: list[str], config: Mapping[str, Any]) -> dict[str, Path | None]:
        if not user_ids:
            return {}
        resources = config["resources"]
        try:
            return await self.avatar_cache.get_many(
                user_ids, ttl_seconds=resources["avatar_cache_ttl_seconds"],
                max_entries=resources["avatar_cache_max_entries"],
            )
        except Exception:
            logger.exception("Failed to load member avatar; returning text")
            return {}

    def _snapshot_image_path(self, image_paths: str | None) -> Path | None:
        if not image_paths:
            return None
        media_root = (self.storage.database_path.parent / "media").resolve()
        candidates = image_paths.split("|")
        random.shuffle(candidates)
        for relative in candidates:
            candidate = (media_root / relative).resolve()
            if media_root in candidate.parents and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _format_result(
        code: str, data: Mapping[str, Any], mode: str, config: Mapping[str, Any],
        *, accept_keyword: str = "接受赠送"
    ) -> str:
        if code == "DRAWN":
            candidate = data["candidate"]
            return render_message(config, "results", f"draw_{mode}", name=candidate["name"])
        if code == "INVITE_CREATED":
            return render_message(config, "results", "invite_created",
                                  invite_id=data["invite_id"], accept_keyword=accept_keyword)
        if code == "CAPACITY_FULL":
            names = "、".join(str(item["name"]) for item in data.get("relationships", ()))
            return render_message(config, "results", "capacity_full", names=names)
        if code == "STEAL_SLOT_FULL":
            return render_message(config, "results", "steal_slot_full", role={"wife": "老婆", "husband": "老公", "member": "群友"}[mode])
        key = code.lower()
        if key in config["messages"]["results"]:
            return render_message(config, "results", key,
                                  invite_id=data.get("invite_id"), limit=data.get("limit"))
        return render_message(config, "results", "unknown_result", code=code)


async def _thread_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)


def _action_mode(action: str) -> str | None:
    if action.endswith("wife") or action == "list_characters":
        return "wife"
    if action.endswith("husband"):
        return "husband"
    if action.endswith("member") or action == "list_members":
        return "member"
    if action in {"divorce_character", "rank_intimacy", "rank_activity", "gift_accept", "gift_reject", "gift_cancel"}:
        return "wife"
    return None


def _event_key(raw: Mapping[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    message_id = str(raw.get("message_id") or "")
    return (f"message:{message_id}" if message_id else f"message-hash:{digest}"), digest
