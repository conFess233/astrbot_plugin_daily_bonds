"""抢夺、赠送、离婚与邀请处理等关系操作指令。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..models import ParsedCommand
from ..services.message_templates import (
    MESSAGE_CONTEXT,
    MessageText,
    render_message,
    set_message_values,
)
from .replies import RuntimeReply


class ActionMixin:
    """为 CommandRuntime 提供关系操作与查询分发。"""

    async def _query_or_action(self, **values: Any) -> str | RuntimeReply | None:
        command: ParsedCommand = values["command"]
        if command.action in {"list_characters", "list_husband", "list_members"}:
            return await self._list_relationships(
                values["scope_id"],
                values["period_id"],
                values["mode"],
                values["command"].page,
                values["config"]["display"]["page_size"],
                values["config"]["display"]["pagination_enabled"],
                values["config"]["resources"],
                values["config"],
            )
        if command.action in {"rank_intimacy", "rank_activity"}:
            return await self._rank(values)
        if command.action == "query_affection":
            return await self._affection(values)
        mode = values["mode"]
        mode_config = values["config"]["modes"][mode]
        config = values["config"]
        actor_id = values["actor_id"]
        target_id = command.target_user_id
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        set_message_values(
            owner_name=names.get(
                target_id if command.action.startswith("steal_") else actor_id, "群友"
            ),
            sender_name=names.get(actor_id, "群友"),
            recipient_name=names.get(target_id, "群友"),
            target_name=names.get(target_id, "群友"),
        )
        if command.action.startswith("steal_"):
            if not mode_config["steal_enabled"]:
                return render_message(config, "errors", "steal_disabled")
            if not target_id or target_id not in values["eligible"]:
                return render_message(config, "errors", "steal_target_required")
            relation_id = await asyncio.to_thread(
                self._resolve_relation,
                values["scope_id"],
                values["period_id"],
                mode,
                target_id,
                command.argument,
            )
            if command.argument and relation_id is None:
                return render_message(config, "errors", "steal_relation_ambiguous")
            if relation_id:
                relation = await asyncio.to_thread(
                    self._relationship_display, relation_id
                )
                if relation:
                    set_message_values(subject_name=relation["name"])
            result = await asyncio.to_thread(
                self.gameplay.steal,
                scope_id=values["scope_id"],
                period_id=values["period_id"],
                mode=mode,
                actor_id=actor_id,
                target_id=target_id,
                capacity=mode_config["capacity"],
                probability=mode_config["steal_probability"],
                attempt_limit=mode_config["steal_attempt_limit"],
                cooldown_seconds=mode_config["steal_cooldown_seconds"],
                target_protection_limit=mode_config["stolen_limit"],
                relation_id=relation_id,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            return await self._result_reply(
                result.code, result.data, mode, values["config"]
            )
        if command.action.startswith("gift_") and command.action not in {
            "gift_accept",
            "gift_reject",
            "gift_cancel",
        }:
            if not mode_config["gift_enabled"]:
                return render_message(config, "errors", "gift_disabled")
            if not target_id or target_id not in values["eligible"]:
                return render_message(config, "errors", "gift_target_required")
            relation_id = await asyncio.to_thread(
                self._resolve_relation,
                values["scope_id"],
                values["period_id"],
                mode,
                actor_id,
                command.argument,
            )
            if command.argument and relation_id is None:
                return render_message(config, "errors", "gift_relation_ambiguous")
            if relation_id:
                relation = await asyncio.to_thread(
                    self._relationship_display, relation_id
                )
                if relation:
                    set_message_values(subject_name=relation["name"])
            result = await asyncio.to_thread(
                self.gameplay.gift,
                scope_id=values["scope_id"],
                period_id=values["period_id"],
                mode=mode,
                sender_id=actor_id,
                recipient_id=target_id,
                capacity=mode_config["capacity"],
                relationship_id=relation_id,
                confirm=mode_config["gift_mode"] == "confirm",
                timeout_seconds=mode_config["gift_timeout_seconds"],
                pending_limit=values["config"]["resources"]["pending_invites_per_user"],
                gift_enabled=True,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            accept_keyword = values["config"]["commands"]["keywords"]["gift_accept"][0]
            return await self._result_reply(
                result.code,
                result.data,
                mode,
                values["config"],
                accept_keyword=accept_keyword,
            )
        if command.action in {
            "divorce_character",
            "divorce_wife",
            "divorce_husband",
            "divorce_member",
        }:
            allowed_modes = (
                ("wife", "husband", "member")
                if command.action == "divorce_character"
                else (mode,)
            )
            matches = await asyncio.to_thread(
                self._divorce_matches,
                values["scope_id"],
                values["period_id"],
                actor_id,
                allowed_modes,
                command.argument,
                target_id,
            )
            if not matches:
                return render_message(config, "errors", "divorce_relation_missing")
            if len(matches) > 1:
                labels = {"wife": "老婆", "husband": "老公", "member": "群友"}
                candidates = "\n".join(
                    f"- {labels[item['mode']]}：{item['name']}"
                    + (
                        "（请 @ 对应群友）"
                        if item["mode"] == "member"
                        else f"（{item['subject_id']}）"
                    )
                    for item in matches
                )
                prefix = render_message(config, "errors", "divorce_ambiguous")
                if not prefix.strip():
                    return prefix
                return MessageText(
                    f"{prefix}\n{candidates}",
                    quote_source=getattr(prefix, "quote_source", False),
                )
            set_message_values(subject_name=matches[0]["name"])
            if matches[0]["slot_kind"] != "normal":
                return render_message(config, "results", "relationship_locked")
            mode = str(matches[0]["mode"])
            relation_id = str(matches[0]["id"])
            mode_config = config["modes"][mode]
            if not mode_config["divorce_enabled"]:
                return render_message(config, "errors", "divorce_disabled")
            result = await asyncio.to_thread(
                self.gameplay.divorce,
                scope_id=values["scope_id"],
                period_id=values["period_id"],
                mode=mode,
                owner_id=actor_id,
                divorce_limit=mode_config["divorce_limit"],
                relationship_id=relation_id,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            return await self._result_reply(
                result.code, result.data, mode, values["config"]
            )
        if command.action in {"gift_accept", "gift_reject", "gift_cancel"}:
            action = {
                "gift_accept": "accept",
                "gift_reject": "reject",
                "gift_cancel": "cancel",
            }[command.action]
            invite_id = command.argument
            if invite_id is None:
                invite_id = await asyncio.to_thread(
                    self._single_pending_invite, values["scope_id"], actor_id, action
                )
                if invite_id == "multiple":
                    return render_message(config, "errors", "invite_multiple")
            if invite_id is None:
                return render_message(config, "errors", "invite_none")
            invite = await asyncio.to_thread(
                self._invite, values["scope_id"], invite_id
            )
            if invite is None:
                return render_message(config, "errors", "invite_invalid_id")
            relation = await asyncio.to_thread(
                self._relationship_display, str(invite["relationship_id"])
            )
            set_message_values(
                sender_name=names.get(str(invite["sender_id"]), "群友"),
                recipient_name=names.get(str(invite["recipient_id"]), "群友"),
                subject_name=str(relation["name"]) if relation else "对象",
            )
            mode = str(invite["mode"])
            mode_config = values["config"]["modes"][mode]
            eligible = values["eligible"]
            result = await asyncio.to_thread(
                self.gameplay.answer_invite,
                scope_id=values["scope_id"],
                period_id=values["period_id"],
                invite_id=invite_id,
                actor_id=actor_id,
                action=action,
                capacity=mode_config["capacity"],
                mode_enabled=mode_config["enabled"],
                gift_enabled=mode_config["gift_enabled"],
                sender_allowed=str(invite["sender_id"]) in eligible
                if action == "accept"
                else True,
                recipient_allowed=str(invite["recipient_id"]) in eligible
                if action == "accept"
                else True,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            return await self._result_reply(
                result.code, result.data, mode, values["config"]
            )
        return None

    def _divorce_matches(
        self,
        scope_id: int,
        period_id: str,
        owner_id: str,
        modes: tuple[str, ...],
        query: str | None,
        target_user_id: str | None,
    ) -> list[dict[str, str]]:
        marks = ",".join("?" for _ in modes)
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
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
                )
                .fetchall()
            )
        matches: list[dict[str, str]] = []
        normalized = query.removeprefix("#") if query else None
        for row in rows:
            if target_user_id is not None:
                selected = (
                    row["mode"] == "member" and str(row["subject_id"]) == target_user_id
                )
            elif query is None:
                selected = row["slot_kind"] == "normal"
            else:
                names = {str(row["name"]), *json.loads(row["aliases_json"])}
                if row["mode"] == "member":
                    names.update(
                        str(row[key]) for key in ("nickname", "card") if row[key]
                    )
                selected = str(row["subject_id"]) == normalized or query in names
            if selected:
                matches.append(
                    {
                        key: str(row[key])
                        for key in ("id", "mode", "subject_id", "name", "slot_kind")
                    }
                )
        normal = [item for item in matches if item["slot_kind"] == "normal"]
        return normal or matches[:1]

    def _resolve_relation(
        self, scope_id: int, period_id: str, mode: str, owner_id: str, query: str | None
    ) -> str | None:
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    """SELECT r.id,r.subject_id,r.slot_kind,s.name,s.aliases_json FROM relationships r
                   JOIN subject_snapshots s ON s.id=r.snapshot_id
                   WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=? AND r.state='active'
                   ORDER BY r.acquired_at,r.id""",
                    (scope_id, period_id, mode, owner_id),
                )
                .fetchall()
            )
        if not query:
            return None
        normalized = query.removeprefix("#")
        matches = [
            row
            for row in rows
            if str(row["subject_id"]) == normalized
            or query == str(row["name"])
            or query in json.loads(row["aliases_json"])
        ]
        normal = [row for row in matches if row["slot_kind"] == "normal"]
        return (
            str(normal[0]["id"])
            if len(normal) == 1
            else str(matches[0]["id"])
            if matches and not normal
            else None
        )

    def _single_pending_invite(
        self, scope_id: int, actor_id: str, action: str
    ) -> str | None:
        column = "sender_id" if action == "cancel" else "recipient_id"
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    f"SELECT id FROM gift_invites WHERE scope_id=? AND {column}=? AND state='pending' ORDER BY created_at,id",
                    (scope_id, actor_id),
                )
                .fetchall()
            )
        if len(rows) > 1:
            return "multiple"
        return str(rows[0]["id"]) if rows else None

    def _invite(self, scope_id: int, invite_id: str):
        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    "SELECT * FROM gift_invites WHERE scope_id=? AND id=?",
                    (scope_id, invite_id),
                )
                .fetchone()
            )
        return row
