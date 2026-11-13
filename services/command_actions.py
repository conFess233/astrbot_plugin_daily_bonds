"""抢夺、赠送、离婚与邀请处理等关系操作指令。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..models.command import ParsedCommand
from ..models.gameplay import Candidate
from . import game_rules as rules
from .command_features import FeatureHandler
from .command_listing import ListingHandler
from .command_replies import ReplyBuilder, RuntimeReply
from .game_status import StatusService
from .gameplay import GameplayService
from .message_templates import (
    MESSAGE_CONTEXT,
    MessageText,
    render_message,
    set_message_values,
)
from .storage import SQLiteStorage

_MODE_LABELS = {"wife": "老婆", "husband": "老公", "member": "群友"}


class ActionHandler:
    """抽取以外的关系操作（抢夺、赠送、离婚）与查询指令分发。"""

    def __init__(
        self,
        storage: SQLiteStorage,
        gameplay: GameplayService,
        status: StatusService,
        replies: ReplyBuilder,
        listing: ListingHandler,
        features: FeatureHandler,
    ) -> None:
        self.storage = storage
        self.gameplay = gameplay
        self.status = status
        self.replies = replies
        self.listing = listing
        self.features = features

    def _other_mode_hint(
        self, values: Mapping[str, Any], mode: str, target_id: str
    ) -> str:
        """对方在别的玩法里有可抢的关系时，提示改用对应的抢夺指令。"""

        config = values["config"]
        with self.storage.read() as db:
            rows = db.execute(
                """SELECT DISTINCT mode FROM relationships
                   WHERE scope_id=? AND period_id=? AND owner_id=? AND state='active'
                   AND slot_kind='normal' AND mode<>?""",
                (values["scope_id"], values["period_id"], target_id, mode),
            ).fetchall()
        keywords = [
            config["commands"]["keywords"][f"steal_{row['mode']}"][0]
            for row in rows
            if config["modes"][row["mode"]]["enabled"]
            and config["modes"][row["mode"]]["steal_enabled"]
        ]
        if not keywords:
            return ""
        return "可以试试" + "或".join(f"“{keyword}”" for keyword in keywords) + "。"

    async def _target_problem(
        self, values: Mapping[str, Any], target_id: str
    ) -> str | None:
        """强娶、抢夺、赠送的目标不合规时返回对应提示的文案键。"""

        if target_id == values["actor_id"]:
            return "target_is_self"
        if target_id in values["eligible"]:
            return None
        if target_id in values["config"]["access"]["extra_bot_ids"]:
            return "target_is_bot"
        is_bot = await asyncio.to_thread(
            self._known_bot, int(values["scope_id"]), target_id
        )
        return "target_is_bot" if is_bot else "target_ineligible"

    def _member_holder(
        self, scope_id: int, period_id: str, subject_id: str
    ) -> str | None:
        """当前娶了这位群友的人；没人娶时返回 None。"""

        with self.storage.read() as db:
            row = db.execute(
                """SELECT owner_id FROM relationships WHERE scope_id=? AND period_id=?
                   AND mode='member' AND subject_id=? AND state='active' LIMIT 1""",
                (scope_id, period_id, subject_id),
            ).fetchone()
        return str(row["owner_id"]) if row else None

    def _known_bot(self, scope_id: int, user_id: str) -> bool:
        with self.storage.read() as db:
            row = db.execute(
                "SELECT is_known_bot FROM members WHERE scope_id=? AND user_id=?",
                (scope_id, user_id),
            ).fetchone()
        return bool(row and row["is_known_bot"])

    async def _steal_probability(
        self,
        scope_id: int,
        actor_id: str,
        target_id: str | None,
        mode_config: Mapping[str, Any],
        prefix: str = "steal",
    ) -> float:
        """亲密度加成为 0 时直接使用基础成功率，避免多余查询。"""

        base = float(mode_config[f"{prefix}_probability"])
        bonus = float(mode_config[f"{prefix}_intimacy_bonus"])
        if not bonus or not target_id:
            return base
        intimacy = await asyncio.to_thread(
            self.status.intimacy,
            scope_id=scope_id,
            source_id=actor_id,
            target_id=target_id,
        )
        return rules.steal_probability(
            base, bonus, float(mode_config[f"{prefix}_probability_max"]), intimacy
        )

    async def query_or_action(self, **values: Any) -> str | RuntimeReply | None:
        command: ParsedCommand = values["command"]
        if command.action in {"list_characters", "list_husband", "list_members"}:
            return await self.listing.list_relationships(
                values["scope_id"],
                values["period_id"],
                values["mode"],
                values["command"].page,
                values["config"]["display"]["page_size"],
                values["config"]["display"]["pagination_enabled"],
                values["config"]["resources"],
                values["config"],
            )
        if command.action in {"rank_intimacy", "rank_my_intimacy", "rank_activity"}:
            return await self.listing.rank(values)
        if command.action == "query_affection":
            return await self.listing.affection(values)
        if command.action == "my_status":
            return await self.features.my_status(values)
        if command.action == "help":
            return await self.features.help(values)
        if command.action == "my_collection":
            return await self.features.my_collection(values)
        if command.action == "character_info":
            return await self.features.character_info(values)
        if command.action == "group_stats":
            return await self.features.group_stats(values)
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
            role=_MODE_LABELS[mode],
            hint="",
        )
        if command.action == "force_member":
            if not mode_config["force_limit"]:
                return render_message(config, "errors", "force_disabled")
            if not target_id:
                return render_message(config, "errors", "force_target_required")
            problem = await self._target_problem(values, target_id)
            if problem:
                return render_message(config, "errors", problem)
            set_message_values(subject_name=names.get(target_id, "群友"))
            probability = await self._steal_probability(
                values["scope_id"], actor_id, target_id, mode_config, "force"
            )
            # 对方已被别人娶走时转为争夺：成功率减去持有者按同一公式算出的成功率。
            holder_id = await asyncio.to_thread(
                self._member_holder, values["scope_id"], values["period_id"], target_id
            )
            holder_probabilities = {}
            if holder_id and holder_id != actor_id:
                defender = await self._steal_probability(
                    values["scope_id"], holder_id, target_id, mode_config, "force"
                )
                holder_probabilities[holder_id] = rules.contested_probability(
                    probability, defender
                )
            result = await asyncio.to_thread(
                self.gameplay.force_marry,
                scope_id=values["scope_id"],
                period_id=values["period_id"],
                actor_id=actor_id,
                target_id=target_id,
                capacity=mode_config["capacity"],
                limit=mode_config["force_limit"],
                probability=probability,
                holder_probabilities=holder_probabilities,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            if result.data.get("owner_id"):
                set_message_values(
                    owner_name=names.get(str(result.data["owner_id"]), "群友")
                )
            if result.code == "ALREADY_HOLDS":
                # 结果码与抢夺共用，文案按强娶单独设置。
                return render_message(config, "results", "force_already_holds")
            reply = await self.replies.result_reply(
                result.code, result.data, mode, config
            )
            if result.code == "FORCED":
                return await self._with_mutual(reply, values, mode, actor_id, target_id)
            return reply
        if command.action.startswith("steal_"):
            if not mode_config["steal_enabled"]:
                return render_message(config, "errors", "steal_disabled")
            if not target_id:
                return render_message(config, "errors", "steal_target_required")
            problem = await self._target_problem(values, target_id)
            if problem:
                return render_message(config, "errors", problem)
            relation_id, matches = await asyncio.to_thread(
                self._resolve_relation,
                values["scope_id"],
                values["period_id"],
                mode,
                target_id,
                command.argument,
            )
            if command.argument and relation_id is None:
                return (
                    render_message(
                        config,
                        "errors",
                        "steal_relation_ambiguous",
                        candidates=_choices(matches),
                    )
                    if matches
                    else render_message(config, "errors", "steal_relation_missing")
                )
            if relation_id:
                relation = await asyncio.to_thread(
                    self.replies.relationship_display, relation_id
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
                probability=await self._steal_probability(
                    values["scope_id"], actor_id, target_id, mode_config
                ),
                attempt_limit=mode_config["steal_attempt_limit"],
                cooldown_seconds=mode_config["steal_cooldown_seconds"],
                target_protection_limit=mode_config["stolen_limit"],
                relation_id=relation_id,
                event_key=values["event_key"],
                payload_hash=values["payload_hash"],
                config_revision=values["revision"],
                now=values["now"],
            )
            if result.code == "NO_TARGET_RELATIONSHIP":
                set_message_values(
                    hint=await asyncio.to_thread(
                        self._other_mode_hint, values, mode, target_id
                    )
                )
            reply = await self.replies.result_reply(
                result.code, result.data, mode, values["config"]
            )
            if result.code == "STOLEN":
                return await self._with_mutual(
                    reply, values, mode, actor_id, str(result.data["subject_id"])
                )
            return reply
        if command.action.startswith("gift_") and command.action not in {
            "gift_accept",
            "gift_reject",
            "gift_cancel",
        }:
            if not mode_config["gift_enabled"]:
                return render_message(config, "errors", "gift_disabled")
            if not target_id:
                return render_message(config, "errors", "gift_target_required")
            problem = await self._target_problem(values, target_id)
            if problem:
                return render_message(config, "errors", problem)
            relation_id, matches = await asyncio.to_thread(
                self._resolve_relation,
                values["scope_id"],
                values["period_id"],
                mode,
                actor_id,
                command.argument,
                require_choice=True,
            )
            if relation_id is None and matches:
                return render_message(
                    config,
                    "errors",
                    "gift_relation_ambiguous",
                    candidates=_choices(matches),
                )
            if command.argument and relation_id is None:
                return render_message(config, "errors", "gift_relation_missing")
            if relation_id:
                relation = await asyncio.to_thread(
                    self.replies.relationship_display, relation_id
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
            if result.code == "RELATIONSHIP_LOCKED":
                return render_message(config, "results", "gift_locked")
            accept_keyword = values["config"]["commands"]["keywords"]["gift_accept"][0]
            reply = await self.replies.result_reply(
                result.code,
                result.data,
                mode,
                values["config"],
                accept_keyword=accept_keyword,
            )
            if result.code == "GIFTED":
                return await self._with_mutual(
                    reply, values, mode, target_id, str(result.data["subject_id"])
                )
            return reply
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
                labels = _MODE_LABELS
                candidates = "\n".join(
                    f"- {labels[item['mode']]}：{item['name']}"
                    + (
                        "（请 @ 对应群友）"
                        if item["mode"] == "member"
                        else f"（{item['subject_id']}）"
                    )
                    for item in matches
                )
                return render_message(
                    config, "errors", "relation_ambiguous", candidates=candidates
                )
            set_message_values(subject_name=matches[0]["name"])
            if matches[0]["slot_kind"] != "normal":
                return render_message(config, "results", "divorce_locked")
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
            return await self.replies.result_reply(
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
                self.replies.invite, values["scope_id"], invite_id
            )
            if invite is None:
                return render_message(config, "errors", "invite_invalid_id")
            relation = await asyncio.to_thread(
                self.replies.relationship_display, str(invite["relationship_id"])
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
            reply = await self.replies.result_reply(
                result.code, result.data, mode, values["config"]
            )
            if result.code == "GIFT_ACCEPTED" and relation:
                return await self._with_mutual(
                    reply, values, mode, actor_id, str(relation["subject_id"])
                )
            return reply
        return None

    async def _with_mutual(
        self,
        reply: str | RuntimeReply,
        values: Mapping[str, Any],
        mode: str,
        owner_id: str,
        subject_id: str,
    ) -> str | RuntimeReply:
        """抢夺或赠送促成双向奔赴时，在回复后追加一行提示。"""

        if mode != "member" or not isinstance(reply, RuntimeReply):
            return reply
        if not reply.text.strip() or not await asyncio.to_thread(
            self.status.is_mutual,
            scope_id=values["scope_id"],
            period_id=values["period_id"],
            owner_id=owner_id,
            subject_id=subject_id,
        ):
            return reply
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        notice = await self.replies.rewrite(
            render_message(
                values["config"],
                "results",
                "mutual_formed",
                owner_name=names.get(owner_id, "群友"),
                subject_name=names.get(subject_id, "群友"),
            )
        )
        if not notice.strip():
            return reply
        return replace(
            reply,
            text=MessageText(
                f"{reply.text}\n{notice}",
                quote_source=getattr(reply.text, "quote_source", False),
            ),
        )

    def _owned_rows(
        self, scope_id: int, period_id: str, owner_id: str, modes: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """当前关系及其可匹配的名称；群友用当前名片和昵称，旧名字仍可匹配。"""

        marks = ",".join("?" for _ in modes)
        with self.storage.read() as db:
            rows = db.execute(
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
        return [
            {
                "id": str(row["id"]),
                "mode": str(row["mode"]),
                "subject_id": str(row["subject_id"]),
                "slot_kind": str(row["slot_kind"]),
                "name": str(row["card"] or row["nickname"] or row["name"]),
                "aliases": tuple(
                    filter(
                        None,
                        (
                            *json.loads(row["aliases_json"]),
                            row["name"],
                            row["card"],
                            row["nickname"],
                        ),
                    )
                ),
            }
            for row in rows
        ]

    @staticmethod
    def _match_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """与指定角色相同的匹配规则：短 ID、忽略大小写，最后按包含匹配。"""

        candidates = {
            row["subject_id"]: Candidate(
                row["subject_id"], "character", row["name"], row["aliases"]
            )
            for row in rows
        }
        matched = {
            item.subject_id
            for item in rules.match_candidates(list(candidates.values()), query)
        }
        return [row for row in rows if row["subject_id"] in matched]

    def _divorce_matches(
        self,
        scope_id: int,
        period_id: str,
        owner_id: str,
        modes: tuple[str, ...],
        query: str | None,
        target_user_id: str | None,
    ) -> list[dict[str, Any]]:
        rows = self._owned_rows(scope_id, period_id, owner_id, modes)
        if target_user_id is not None:
            matches = [
                row
                for row in rows
                if row["mode"] == "member" and row["subject_id"] == target_user_id
            ]
        elif query is None:
            matches = rows
        else:
            matches = self._match_rows(rows, query)
        # 优先普通关系；只剩受保护关系时留一条，由调用方回复“受保护”。
        normal = [item for item in matches if item["slot_kind"] == "normal"]
        return normal or matches[:1]

    def _resolve_relation(
        self,
        scope_id: int,
        period_id: str,
        mode: str,
        owner_id: str,
        query: str | None,
        *,
        require_choice: bool = False,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """返回唯一选中的关系；不唯一时 ID 为空，并附上供用户选择的候选。

        抢夺不写对象时随机，赠送（`require_choice`）则要求有多段时明确指定。
        受保护的关系也能被选中，由玩法事务回复“受保护”。
        """

        rows = self._owned_rows(scope_id, period_id, owner_id, (mode,))
        if not query and not require_choice:
            return None, []
        matches = self._match_rows(rows, query) if query else rows
        normal = [row for row in matches if row["slot_kind"] == "normal"]
        if len(normal) == 1:
            return normal[0]["id"], []
        if normal:
            return None, normal
        return (matches[0]["id"], []) if query and matches else (None, [])

    def _single_pending_invite(
        self, scope_id: int, actor_id: str, action: str
    ) -> str | None:
        column = "sender_id" if action == "cancel" else "recipient_id"
        with self.storage.read() as db:
            rows = db.execute(
                f"SELECT id FROM gift_invites WHERE scope_id=? AND {column}=? AND state='pending' ORDER BY created_at,id",
                (scope_id, actor_id),
            ).fetchall()
        if len(rows) > 1:
            return "multiple"
        return str(rows[0]["id"]) if rows else None


def _choices(rows: list[dict[str, Any]]) -> str:
    """歧义提示中的候选列表：每行“名称 · #ID”。"""

    return "\n".join(f"{row['name']} · #{row['subject_id']}" for row in rows)
