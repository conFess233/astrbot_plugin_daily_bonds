"""赠送、邀请应答事务。"""

from __future__ import annotations

import secrets
import uuid

from ..models.errors import StorageError
from ..models.gameplay import BusinessResult
from ..models.scope import Mode
from .gameplay_base import GameplayBase


class GiftMixin(GameplayBase):
    """赠送与邀请应答。"""

    def gift(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: Mode,
        sender_id: str,
        recipient_id: str,
        capacity: int,
        relationship_id: str | None = None,
        subject_id: str | None = None,
        confirm: bool = False,
        timeout_seconds: int = 60,
        pending_limit: int = 20,
        gift_enabled: bool = True,
        recipient_umo: str | None = None,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """直接转移配偶，或创建不锁定对象的待确认邀请。"""

        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            if not gift_enabled:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "MODE_DISABLED",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if sender_id == recipient_id:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "INVALID_TARGET",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            sender_relations = self._owner_relationships(
                db, scope_id, period_id, mode, sender_id
            )
            relationship = self._choose_owned(
                sender_relations, relationship_id, subject_id
            )
            if relationship is None:
                if self._locked_relationship(
                    db,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    relationship_id,
                    subject_id,
                ):
                    return self._record_result(
                        db,
                        operation_id,
                        scope_id,
                        period_id,
                        mode,
                        sender_id,
                        "gift",
                        "RELATIONSHIP_LOCKED",
                        {},
                        config_revision,
                        now,
                        event_key,
                        payload_hash,
                    )
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "RELATIONSHIP_NOT_FOUND",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            recipient_relations = self._owner_relationships(
                db, scope_id, period_id, mode, recipient_id, None
            )
            if len(recipient_relations) >= capacity:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "CAPACITY_FULL",
                    {"relationships": recipient_relations},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if mode == "member" and relationship["subject_id"] == recipient_id:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "INVALID_TARGET",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if not confirm:
                result_relationship = self._transfer(
                    db, relationship, recipient_id, capacity, now, "gifted"
                )
                self._invalidate_relation_invites(
                    db, relationship["id"], now, "relationship_gifted"
                )
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "GIFTED",
                    {
                        "relationship_id": relationship["id"],
                        "new_relationship_id": result_relationship,
                        "recipient_id": recipient_id,
                        "subject_id": relationship["subject_id"],
                    },
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            pending = db.execute(
                "SELECT id FROM gift_invites WHERE relationship_id=? AND state='pending'",
                (relationship["id"],),
            ).fetchone()
            if pending:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "INVITE_ALREADY_PENDING",
                    {"invite_id": pending["id"]},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if timeout_seconds < 1:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "INVALID_TIMEOUT",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            pending_count = db.execute(
                "SELECT COUNT(*) AS count FROM gift_invites WHERE scope_id=? AND sender_id=? AND state='pending'",
                (scope_id, sender_id),
            ).fetchone()["count"]
            if pending_limit > 0 and int(pending_count) >= pending_limit:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    sender_id,
                    "gift",
                    "INVITE_LIMIT",
                    {"limit": pending_limit},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            while True:
                invite_id = str(secrets.randbelow(900_000_000_000) + 100_000_000_000)
                if (
                    db.execute(
                        "SELECT 1 FROM gift_invites WHERE id=?", (invite_id,)
                    ).fetchone()
                    is None
                ):
                    break
            expires_at = now + timeout_seconds
            db.execute(
                """INSERT INTO gift_invites(id,scope_id,period_id,mode,relationship_id,relationship_version,sender_id,recipient_id,state,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    invite_id,
                    scope_id,
                    period_id,
                    mode,
                    relationship["id"],
                    relationship["version"],
                    sender_id,
                    recipient_id,
                    now,
                    expires_at,
                ),
            )
            if recipient_umo:
                self._outbox(
                    db,
                    scope_id,
                    f"gift-invite:{invite_id}",
                    recipient_umo,
                    {
                        "kind": "gift_invite",
                        "invite_id": invite_id,
                        "sender_id": sender_id,
                        "recipient_id": recipient_id,
                        "text": f"收到来自群友 {sender_id} 的赠送邀请，编号 {invite_id}；请在发起邀请的群内按消息提示处理。",
                    },
                    now,
                )
            return self._record_result(
                db,
                operation_id,
                scope_id,
                period_id,
                mode,
                sender_id,
                "gift",
                "INVITE_CREATED",
                {
                    "invite_id": invite_id,
                    "recipient_id": recipient_id,
                    "expires_at": expires_at,
                    "subject_id": relationship["subject_id"],
                },
                config_revision,
                now,
                event_key,
                payload_hash,
            )

    def answer_invite(
        self,
        *,
        scope_id: int,
        period_id: str,
        invite_id: str,
        actor_id: str,
        action: str,
        capacity: int,
        mode_enabled: bool,
        gift_enabled: bool,
        sender_allowed: bool,
        recipient_allowed: bool,
        recipient_umo: str | None = None,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """接受、拒绝或取消邀请；终态单调且操作有归属校验。"""

        if action not in {"accept", "reject", "cancel"}:
            raise ValueError("action must be accept, reject or cancel")
        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            invite = db.execute(
                "SELECT * FROM gift_invites WHERE id=? AND scope_id=?",
                (invite_id, scope_id),
            ).fetchone()
            if invite is None:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    None,
                    actor_id,
                    f"gift_{action}",
                    "INVITE_INVALID",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            expected_actor = (
                invite["recipient_id"]
                if action in {"accept", "reject"}
                else invite["sender_id"]
            )
            if actor_id != expected_actor:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    invite["mode"],
                    actor_id,
                    f"gift_{action}",
                    "INVITE_NOT_OWNED",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if invite["state"] != "pending":
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    invite["mode"],
                    actor_id,
                    f"gift_{action}",
                    "INVITE_INVALID",
                    {"state": invite["state"]},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if action == "cancel":
                state, reason = "cancelled", "sender_cancelled"
            elif action == "reject":
                state, reason = "rejected", "recipient_rejected"
            else:
                state, reason = "accepted", None
            if action == "accept":
                if now >= int(invite["expires_at"]):
                    state, reason = "expired", "timeout"
                elif invite["period_id"] != period_id:
                    state, reason = "invalidated", "period_changed"
                else:
                    relation = db.execute(
                        "SELECT * FROM relationships WHERE id=? AND scope_id=? AND period_id=? AND mode=? AND slot_kind='normal' AND state='active'",
                        (
                            invite["relationship_id"],
                            scope_id,
                            period_id,
                            invite["mode"],
                        ),
                    ).fetchone()
                    if (
                        relation is None
                        or relation["owner_id"] != invite["sender_id"]
                        or int(relation["version"])
                        != int(invite["relationship_version"])
                    ):
                        state, reason = "invalidated", "relationship_changed"
                    elif not mode_enabled or not gift_enabled:
                        state, reason = "invalidated", "gift_or_mode_disabled"
                    elif not sender_allowed or not recipient_allowed:
                        state, reason = "invalidated", "participant_ineligible"
                    elif (
                        invite["mode"] == "member"
                        and relation["subject_id"] == invite["recipient_id"]
                    ):
                        state, reason = "invalidated", "self_relationship"
                    elif (
                        len(
                            self._owner_relationships(
                                db,
                                scope_id,
                                period_id,
                                invite["mode"],
                                invite["recipient_id"],
                                None,
                            )
                        )
                        >= capacity
                    ):
                        state, reason = "invalidated", "recipient_full"
            updated = db.execute(
                "UPDATE gift_invites SET state=?,finalized_at=?,reason=? WHERE id=? AND state='pending'",
                (state, now, reason, invite_id),
            )
            if updated.rowcount != 1:
                raise StorageError("邀请状态已由并发操作完成。")
            new_relationship_id = None
            if action == "accept" and state == "accepted":
                relation = db.execute(
                    "SELECT * FROM relationships WHERE id=?",
                    (invite["relationship_id"],),
                ).fetchone()
                new_relationship_id = self._transfer(
                    db, relation, invite["recipient_id"], capacity, now, "gifted"
                )
                db.execute(
                    "UPDATE gift_invites SET result_relationship_id=? WHERE id=?",
                    (new_relationship_id, invite_id),
                )
            elif recipient_umo:
                final_text = {
                    "cancelled": "已取消",
                    "rejected": "已拒绝",
                    "expired": "已过期",
                    "invalidated": "已失效",
                }.get(state, "已处理")
                self._outbox(
                    db,
                    scope_id,
                    f"gift-final:{invite_id}",
                    recipient_umo,
                    {
                        "kind": "gift_invite_final",
                        "invite_id": invite_id,
                        "state": state,
                        "reason": reason,
                        "text": f"赠送邀请 {invite_id} {final_text}。",
                    },
                    now,
                )
            result_code = (
                "GIFT_ACCEPTED"
                if state == "accepted"
                else (
                    "INVITE_EXPIRED"
                    if state == "expired"
                    else "INVITE_INVALIDATED"
                    if state == "invalidated"
                    else f"INVITE_{state.upper()}"
                )
            )
            return self._record_result(
                db,
                operation_id,
                scope_id,
                period_id,
                invite["mode"],
                actor_id,
                f"gift_{action}",
                result_code,
                {
                    "invite_id": invite_id,
                    "state": state,
                    "reason": reason,
                    "new_relationship_id": new_relationship_id,
                },
                config_revision,
                now,
                event_key,
                payload_hash,
            )
