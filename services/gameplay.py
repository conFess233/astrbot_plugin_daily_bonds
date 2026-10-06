"""三种玩法的抽取、抢夺、赠送、邀请和离婚事务。"""

from __future__ import annotations

import json
import random
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from ..models import Mode, StorageError
from .storage import SQLiteStorage


@dataclass(frozen=True, slots=True)
class Candidate:
    """事务外准备、事务内复核的候选对象快照。"""

    subject_id: str
    subject_kind: str
    name: str
    aliases: tuple[str, ...] = ()
    gender: str = "unspecified"
    source_revision: int | None = None
    image_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BusinessResult:
    """一次业务事务的稳定结果码与返回数据。"""

    code: str
    data: dict[str, Any]
    replayed: bool = False


class GameplayService:
    """共享 Handler/Web 调用的事务服务；随机源与时钟均可注入。"""

    def __init__(self, storage: SQLiteStorage, chooser: random.Random | None = None) -> None:
        self.storage = storage
        self.chooser = chooser or random.SystemRandom()

    def draw(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: Mode,
        owner_id: str,
        candidates: Sequence[Candidate],
        capacity: int,
        pool_ids: Sequence[str] = (),
        weights: dict[str, float] | None = None,
        activity_full_messages: int = 50,
        activity_window_days: int = 7,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """抽取一个对象；优先消耗最早生成的补抽资格。"""

        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            active = self._owner_relationships(db, scope_id, period_id, mode, owner_id)
            if len(active) >= capacity:
                result = BusinessResult("CAPACITY_FULL", {"relationships": active})
                self._record(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    owner_id,
                    "draw",
                    result,
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
                return result

            credit = db.execute(
                """SELECT id FROM redraw_credits WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?
                   AND state='available' ORDER BY issued_at,id LIMIT 1""",
                (scope_id, period_id, mode, owner_id),
            ).fetchone()
            counter = self._counter(db, scope_id, period_id, mode, owner_id)
            if credit is None and int(counter["normal_draws"]) >= capacity:
                result = BusinessResult(
                    "NO_DRAW_QUOTA", {"held": len(active), "capacity": capacity}
                )
                self._record(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    owner_id,
                    "draw",
                    result,
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
                return result

            available = self._valid_candidates(
                db, scope_id, period_id, mode, owner_id, candidates, pool_ids
            )
            if not available:
                result = BusinessResult("NO_CANDIDATE", {})
                self._record(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    owner_id,
                    "draw",
                    result,
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
                return result
            intimacy: dict[str, int] = {}
            activity: dict[str, int] = {}
            if mode == "member":
                subject_ids = [candidate.subject_id for candidate in available]
                marks = ",".join("?" for _ in subject_ids)
                intimacy = {
                    row["target_id"]: int(row["score"])
                    for row in db.execute(
                        f"SELECT target_id,score FROM intimacy_edges WHERE scope_id=? AND source_id=? AND target_id IN ({marks})",
                        (scope_id, owner_id, *subject_ids),
                    ).fetchall()
                }
                activity = {
                    row["user_id"]: int(row["messages"])
                    for row in db.execute(
                        f"""SELECT user_id,SUM(message_count) AS messages FROM activity_seconds
                            WHERE scope_id=? AND user_id IN ({marks}) AND observed_second>? AND observed_second<=?
                            GROUP BY user_id""",
                        (
                            scope_id,
                            *subject_ids,
                            now - activity_window_days * 86400,
                            now,
                        ),
                    ).fetchall()
                }
            picked = self._pick(
                available,
                mode,
                owner_id,
                intimacy,
                activity,
                weights or {},
                activity_full_messages,
            )
            snapshot_id = self._snapshot(db, picked, now)
            relationship_id = self.storage.create_relationship(
                scope_id=scope_id,
                period_id=period_id,
                mode=mode,
                owner_id=owner_id,
                subject_kind=picked.subject_kind,
                subject_id=picked.subject_id,
                snapshot_id=snapshot_id,
                capacity=capacity,
                now=now,
            )
            if credit:
                db.execute(
                    "UPDATE redraw_credits SET state='consumed',consumed_at=?,consumed_relationship_id=? WHERE id=? AND state='available'",
                    (now, relationship_id, credit["id"]),
                )
            else:
                db.execute(
                    "UPDATE daily_counters SET normal_draws=normal_draws+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                    (scope_id, period_id, mode, owner_id),
                )
            result = BusinessResult(
                "DRAWN",
                {
                    "relationship_id": relationship_id,
                    "candidate": _candidate_dict(picked),
                    "credit_id": credit["id"] if credit else None,
                },
            )
            self._record(
                db,
                operation_id,
                scope_id,
                period_id,
                mode,
                owner_id,
                "draw",
                result,
                config_revision,
                now,
                event_key,
                payload_hash,
            )
            return result

    def set_wife(
        self,
        *,
        scope_id: int,
        period_id: str,
        owner_id: str,
        character_id: str,
        pool_ids: Sequence[str],
        event_key: str,
        payload_hash: str,
        config_revision: str,
        now: int,
    ) -> BusinessResult:
        """Create an administrator-selected wife without consuming draw quota."""

        operation_id = str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            if not pool_ids:
                result = BusinessResult("NO_CANDIDATE", {})
            else:
                marks = ",".join("?" for _ in pool_ids)
                row = db.execute(
                    f"""SELECT c.id,c.name,c.aliases_json,c.gender,c.revision
                        FROM characters c JOIN pool_members pm ON pm.character_id=c.id
                        JOIN pools p ON p.id=pm.pool_id
                        WHERE c.id=? AND c.enabled=1 AND c.deleted_at IS NULL
                          AND p.mode='wife' AND p.deleted_at IS NULL AND p.id IN ({marks})
                        LIMIT 1""",
                    (character_id, *pool_ids),
                ).fetchone()
                if row is None:
                    result = BusinessResult("NO_CANDIDATE", {})
                else:
                    images = tuple(
                        value[0]
                        for value in db.execute(
                            """SELECT ci.media_hash FROM character_images ci
                           JOIN media_blobs mb ON mb.hash=ci.media_hash
                           WHERE ci.character_id=? ORDER BY ci.ordinal,ci.media_hash""",
                            (character_id,),
                        ).fetchall()
                    )
                    existing = db.execute(
                        """SELECT id FROM relationships WHERE scope_id=? AND period_id=?
                           AND mode='wife' AND owner_id=? AND subject_id=? AND state='active'""",
                        (scope_id, period_id, owner_id, character_id),
                    ).fetchone()
                    if existing is not None:
                        result = BusinessResult(
                            "ALREADY_HELD", {"relationship_id": existing["id"]}
                        )
                    elif not images:
                        result = BusinessResult("NO_CANDIDATE", {})
                    else:
                        candidate = Candidate(
                            str(row["id"]),
                            "character",
                            str(row["name"]),
                            tuple(json.loads(row["aliases_json"])),
                            str(row["gender"]),
                            int(row["revision"]),
                            images,
                        )
                        snapshot_id = self._snapshot(db, candidate, now)
                        relationship_id = self.storage.create_relationship(
                            scope_id=scope_id,
                            period_id=period_id,
                            mode="wife",
                            owner_id=owner_id,
                            subject_kind="character",
                            subject_id=character_id,
                            snapshot_id=snapshot_id,
                            capacity=1,
                            now=now,
                            ignore_capacity=True,
                        )
                        result = BusinessResult(
                            "DRAWN",
                            {
                                "relationship_id": relationship_id,
                                "candidate": _candidate_dict(candidate),
                                "credit_id": None,
                            },
                        )
            self._record(
                db,
                operation_id,
                scope_id,
                period_id,
                "wife",
                owner_id,
                "set_wife",
                result,
                config_revision,
                now,
                event_key,
                payload_hash,
            )
            return result

    def draw_designated(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: Mode,
        owner_id: str,
        candidates: Sequence[Candidate],
        capacity: int,
        pool_ids: Sequence[str] = (),
        unique: bool = True,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """指定角色独立占用槽位和成功次数，不消耗普通抽取或补抽资格。"""

        if (
            mode not in {"wife", "husband"}
            or isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 0 <= capacity <= 100
            or not isinstance(unique, bool)
        ):
            raise ValueError("指定玩法、容量或独占开关无效。")
        if len(candidates) != 1:
            raise ValueError("指定抽取必须先解析为唯一角色。")
        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            code, data = "DESIGNATED_DRAWN", {}
            active = self._owner_relationships(
                db, scope_id, period_id, mode, owner_id, "designated"
            )
            counter = self._counter(db, scope_id, period_id, mode, owner_id)
            if capacity == 0:
                code = "DESIGNATED_DISABLED"
            elif len(active) >= capacity:
                code, data = (
                    "DESIGNATED_SLOT_FULL",
                    {"relationships": active, "capacity": capacity},
                )
            elif int(counter["designated_draws"]) >= capacity:
                code, data = "NO_DESIGNATED_QUOTA", {"capacity": capacity}
            else:
                available = self._valid_candidates(
                    db,
                    scope_id,
                    period_id,
                    mode,
                    owner_id,
                    candidates,
                    pool_ids,
                    require_unoccupied=False,
                )
                if not available:
                    code = "NO_CANDIDATE"
                else:
                    picked = available[0]
                    occupied = (
                        unique
                        and db.execute(
                            "SELECT 1 FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND subject_id=? AND state='active' LIMIT 1",
                            (scope_id, period_id, mode, picked.subject_id),
                        ).fetchone()
                        is not None
                    )
                    if occupied:
                        code, data = (
                            "DESIGNATED_OCCUPIED",
                            {"name": picked.name, "subject_id": picked.subject_id},
                        )
                    else:
                        snapshot_id = self._snapshot(db, picked, now)
                        relationship_id = self.storage.create_relationship(
                            scope_id=scope_id,
                            period_id=period_id,
                            mode=mode,
                            owner_id=owner_id,
                            subject_kind=picked.subject_kind,
                            subject_id=picked.subject_id,
                            snapshot_id=snapshot_id,
                            capacity=capacity,
                            now=now,
                            slot_kind="designated",
                        )
                        db.execute(
                            "UPDATE daily_counters SET designated_draws=designated_draws+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                            (scope_id, period_id, mode, owner_id),
                        )
                        data = {
                            "relationship_id": relationship_id,
                            "candidate": _candidate_dict(picked),
                            "slot_kind": "designated",
                        }
            return self._record_result(
                db,
                operation_id,
                scope_id,
                period_id,
                mode,
                owner_id,
                "draw_designated",
                code,
                data,
                config_revision,
                now,
                event_key,
                payload_hash,
            )

    def steal(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: Mode,
        actor_id: str,
        target_id: str,
        capacity: int,
        probability: float,
        attempt_limit: int,
        cooldown_seconds: int,
        target_protection_limit: int,
        steal_slot_capacity: int = 0,
        relation_id: str | None = None,
        subject_id: str | None = None,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """执行一次合法抢夺；概率失败也持久化次数和冷却。"""

        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            if actor_id == target_id:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "INVALID_TARGET", {}, config_revision, now, event_key, payload_hash)
            destination = "steal" if steal_slot_capacity > 0 else "normal"
            actor_relationships = self._owner_relationships(db, scope_id, period_id, mode, actor_id, destination)
            if len(actor_relationships) >= (steal_slot_capacity or capacity):
                code = "STEAL_SLOT_FULL" if steal_slot_capacity > 0 else "CAPACITY_FULL"
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", code, {"relationships": actor_relationships}, config_revision, now, event_key, payload_hash)
            counter = self._counter(db, scope_id, period_id, mode, actor_id)
            if attempt_limit and int(counter["steal_attempts"]) >= attempt_limit:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "STEAL_LIMIT", {}, config_revision, now, event_key, payload_hash)
            if cooldown_seconds and counter["last_steal_at"] is not None and now - int(counter["last_steal_at"]) < cooldown_seconds:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "STEAL_COOLDOWN", {"available_at": int(counter["last_steal_at"]) + cooldown_seconds}, config_revision, now, event_key, payload_hash)
            target_relations = self._owner_relationships(db, scope_id, period_id, mode, target_id)
            target_relation = self._choose_owned(target_relations, relation_id, subject_id)
            if target_relation is None:
                if self._locked_relationship(db, scope_id, period_id, mode, target_id, relation_id, subject_id):
                    return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "STEAL_SLOT_LOCKED", {}, config_revision, now, event_key, payload_hash)
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "NO_TARGET_RELATIONSHIP", {}, config_revision, now, event_key, payload_hash)
            target_counter = self._counter(db, scope_id, period_id, mode, target_id)
            if target_protection_limit and int(target_counter["stolen_successes"]) >= target_protection_limit:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "TARGET_PROTECTED", {}, config_revision, now, event_key, payload_hash)
            if mode == "member" and target_relation["subject_id"] == actor_id:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "INVALID_TARGET", {}, config_revision, now, event_key, payload_hash)
            if not math_probability(probability):
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "INVALID_PROBABILITY", {}, config_revision, now, event_key, payload_hash)
            success = self.chooser.random() < probability
            db.execute(
                """UPDATE daily_counters SET steal_attempts=steal_attempts+1,last_steal_at=?
                   WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?""",
                (now, scope_id, period_id, mode, actor_id),
            )
            if not success:
                return self._record_result(db, operation_id, scope_id, period_id, mode, actor_id, "steal", "STEAL_FAILED", {"probability": probability}, config_revision, now, event_key, payload_hash)
            self._transfer(db, target_relation, actor_id, steal_slot_capacity or capacity, now, "stolen", destination)
            db.execute(
                "UPDATE daily_counters SET stolen_successes=stolen_successes+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, target_id),
            )
            self._issue_credit(db, scope_id, period_id, mode, target_id, operation_id, "stolen", target_relation["id"], now)
            self._invalidate_relation_invites(db, target_relation["id"], now, "relationship_stolen")
            return self._record_result(
                db, operation_id, scope_id, period_id, mode, actor_id, "steal", "STOLEN",
                {"relationship_id": target_relation["id"], "subject_id": target_relation["subject_id"], "old_owner_id": target_id},
                config_revision, now, event_key, payload_hash,
            )

    def divorce(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: Mode,
        owner_id: str,
        divorce_limit: int,
        relationship_id: str | None = None,
        subject_id: str | None = None,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """主动解除一段指定关系，并发放来源唯一的补抽资格。"""

        operation_id = operation_id or str(uuid.uuid4())
        with self.storage.transaction() as db:
            replay = self._replay(db, scope_id, event_key, payload_hash)
            if replay:
                return replay
            self._current_period(db, scope_id, period_id)
            counter = self._counter(db, scope_id, period_id, mode, owner_id)
            if divorce_limit and int(counter["divorces"]) >= divorce_limit:
                return self._record_result(db, operation_id, scope_id, period_id, mode, owner_id, "divorce", "DIVORCE_LIMIT", {}, config_revision, now, event_key, payload_hash)
            relations = self._owner_relationships(db, scope_id, period_id, mode, owner_id)
            relationship = self._choose_owned(relations, relationship_id, subject_id)
            if relationship is None:
                if self._locked_relationship(db, scope_id, period_id, mode, owner_id, relationship_id, subject_id):
                    return self._record_result(db, operation_id, scope_id, period_id, mode, owner_id, "divorce", "RELATIONSHIP_LOCKED", {}, config_revision, now, event_key, payload_hash)
                return self._record_result(db, operation_id, scope_id, period_id, mode, owner_id, "divorce", "RELATIONSHIP_NOT_FOUND", {}, config_revision, now, event_key, payload_hash)
            db.execute(
                "UPDATE relationships SET state='ended',ended_at=?,end_reason='divorce',version=version+1 WHERE id=? AND state='active'",
                (now, relationship["id"]),
            )
            db.execute(
                "UPDATE daily_counters SET divorces=divorces+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, owner_id),
            )
            self._issue_credit(db, scope_id, period_id, mode, owner_id, operation_id, "divorce", relationship["id"], now)
            self._invalidate_relation_invites(db, relationship["id"], now, "relationship_divorced")
            return self._record_result(
                db, operation_id, scope_id, period_id, mode, owner_id, "divorce", "DIVORCED",
                {"relationship_id": relationship["id"], "subject_id": relationship["subject_id"], "snapshot_id": relationship["snapshot_id"]},
                config_revision, now, event_key, payload_hash,
            )

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
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "MODE_DISABLED", {}, config_revision, now, event_key, payload_hash)
            if sender_id == recipient_id:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVALID_TARGET", {}, config_revision, now, event_key, payload_hash)
            sender_relations = self._owner_relationships(db, scope_id, period_id, mode, sender_id)
            relationship = self._choose_owned(sender_relations, relationship_id, subject_id)
            if relationship is None:
                if self._locked_relationship(db, scope_id, period_id, mode, sender_id, relationship_id, subject_id):
                    return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "RELATIONSHIP_LOCKED", {}, config_revision, now, event_key, payload_hash)
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "RELATIONSHIP_NOT_FOUND", {}, config_revision, now, event_key, payload_hash)
            recipient_relations = self._owner_relationships(db, scope_id, period_id, mode, recipient_id)
            if len(recipient_relations) >= capacity:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "CAPACITY_FULL", {"relationships": recipient_relations}, config_revision, now, event_key, payload_hash)
            if mode == "member" and relationship["subject_id"] == recipient_id:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVALID_TARGET", {}, config_revision, now, event_key, payload_hash)
            if not confirm:
                result_relationship = self._transfer(db, relationship, recipient_id, capacity, now, "gifted")
                self._invalidate_relation_invites(db, relationship["id"], now, "relationship_gifted")
                return self._record_result(
                    db, operation_id, scope_id, period_id, mode, sender_id, "gift", "GIFTED",
                    {"relationship_id": relationship["id"], "new_relationship_id": result_relationship, "recipient_id": recipient_id, "subject_id": relationship["subject_id"]},
                    config_revision, now, event_key, payload_hash,
                )
            pending = db.execute("SELECT id FROM gift_invites WHERE relationship_id=? AND state='pending'", (relationship["id"],)).fetchone()
            if pending:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVITE_ALREADY_PENDING", {"invite_id": pending["id"]}, config_revision, now, event_key, payload_hash)
            if timeout_seconds < 1:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVALID_TIMEOUT", {}, config_revision, now, event_key, payload_hash)
            pending_count = db.execute(
                "SELECT COUNT(*) AS count FROM gift_invites WHERE scope_id=? AND sender_id=? AND state='pending'",
                (scope_id, sender_id),
            ).fetchone()["count"]
            if pending_limit > 0 and int(pending_count) >= pending_limit:
                return self._record_result(db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVITE_LIMIT", {"limit": pending_limit}, config_revision, now, event_key, payload_hash)
            while True:
                invite_id = str(secrets.randbelow(900_000_000_000) + 100_000_000_000)
                if db.execute("SELECT 1 FROM gift_invites WHERE id=?", (invite_id,)).fetchone() is None:
                    break
            expires_at = now + timeout_seconds
            db.execute(
                """INSERT INTO gift_invites(id,scope_id,period_id,mode,relationship_id,relationship_version,sender_id,recipient_id,state,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
                (invite_id, scope_id, period_id, mode, relationship["id"], relationship["version"], sender_id, recipient_id, now, expires_at),
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
                db, operation_id, scope_id, period_id, mode, sender_id, "gift", "INVITE_CREATED",
                {"invite_id": invite_id, "recipient_id": recipient_id, "expires_at": expires_at, "subject_id": relationship["subject_id"]},
                config_revision, now, event_key, payload_hash,
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
            invite = db.execute("SELECT * FROM gift_invites WHERE id=? AND scope_id=?", (invite_id, scope_id)).fetchone()
            if invite is None:
                return self._record_result(db, operation_id, scope_id, period_id, None, actor_id, f"gift_{action}", "INVITE_INVALID", {}, config_revision, now, event_key, payload_hash)
            expected_actor = invite["recipient_id"] if action in {"accept", "reject"} else invite["sender_id"]
            if actor_id != expected_actor:
                return self._record_result(db, operation_id, scope_id, period_id, invite["mode"], actor_id, f"gift_{action}", "INVITE_NOT_OWNED", {}, config_revision, now, event_key, payload_hash)
            if invite["state"] != "pending":
                return self._record_result(db, operation_id, scope_id, period_id, invite["mode"], actor_id, f"gift_{action}", "INVITE_INVALID", {"state": invite["state"]}, config_revision, now, event_key, payload_hash)
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
                        (invite["relationship_id"], scope_id, period_id, invite["mode"]),
                    ).fetchone()
                    if relation is None or relation["owner_id"] != invite["sender_id"] or int(relation["version"]) != int(invite["relationship_version"]):
                        state, reason = "invalidated", "relationship_changed"
                    elif not mode_enabled or not gift_enabled:
                        state, reason = "invalidated", "gift_or_mode_disabled"
                    elif not sender_allowed or not recipient_allowed:
                        state, reason = "invalidated", "participant_ineligible"
                    elif invite["mode"] == "member" and relation["subject_id"] == invite["recipient_id"]:
                        state, reason = "invalidated", "self_relationship"
                    elif len(self._owner_relationships(db, scope_id, period_id, invite["mode"], invite["recipient_id"])) >= capacity:
                        state, reason = "invalidated", "recipient_full"
            updated = db.execute(
                "UPDATE gift_invites SET state=?,finalized_at=?,reason=? WHERE id=? AND state='pending'",
                (state, now, reason, invite_id),
            )
            if updated.rowcount != 1:
                raise StorageError("邀请状态已由并发操作完成。")
            new_relationship_id = None
            if action == "accept" and state == "accepted":
                relation = db.execute("SELECT * FROM relationships WHERE id=?", (invite["relationship_id"],)).fetchone()
                new_relationship_id = self._transfer(db, relation, invite["recipient_id"], capacity, now, "gifted")
                db.execute("UPDATE gift_invites SET result_relationship_id=? WHERE id=?", (new_relationship_id, invite_id))
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
            result_code = "GIFT_ACCEPTED" if state == "accepted" else ("INVITE_EXPIRED" if state == "expired" else "INVITE_INVALIDATED" if state == "invalidated" else f"INVITE_{state.upper()}")
            return self._record_result(
                db, operation_id, scope_id, period_id, invite["mode"], actor_id, f"gift_{action}", result_code,
                {"invite_id": invite_id, "state": state, "reason": reason, "new_relationship_id": new_relationship_id},
                config_revision, now, event_key, payload_hash,
            )

    def _valid_candidates(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        period_id: str,
        mode: Mode,
        owner_id: str,
        candidates: Sequence[Candidate],
        allowed_pool_ids: Sequence[str],
        *,
        require_unoccupied: bool = True,
    ) -> list[Candidate]:
        output: list[Candidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.subject_id in seen:
                continue
            seen.add(candidate.subject_id)
            if mode == "member":
                if (
                    candidate.subject_kind != "member"
                    or candidate.subject_id == owner_id
                ):
                    continue
                member = db.execute(
                    "SELECT nickname,card,is_present,is_known_bot FROM members WHERE scope_id=? AND user_id=?",
                    (scope_id, candidate.subject_id),
                ).fetchone()
                if member is None or not member["is_present"] or member["is_known_bot"]:
                    continue
                candidate = Candidate(
                    candidate.subject_id,
                    "member",
                    member["card"] or member["nickname"] or candidate.subject_id,
                    (),
                    "unspecified",
                    None,
                    (),
                )
            else:
                if candidate.subject_kind != "character":
                    continue
                if not allowed_pool_ids:
                    continue
                character = db.execute(
                    "SELECT name,aliases_json,gender,enabled,deleted_at,revision FROM characters WHERE id=?",
                    (candidate.subject_id,),
                ).fetchone()
                if (
                    character is None
                    or not character["enabled"]
                    or character["deleted_at"] is not None
                ):
                    continue
                pool_marks = ",".join("?" for _ in allowed_pool_ids)
                pool_match = db.execute(
                    f"""SELECT 1 FROM pool_members pm JOIN pools p ON p.id=pm.pool_id
                        WHERE pm.character_id=? AND pm.pool_id IN ({pool_marks}) AND p.mode=?
                        AND p.deleted_at IS NULL LIMIT 1""",
                    (candidate.subject_id, *allowed_pool_ids, mode),
                ).fetchone()
                if pool_match is None:
                    continue
                active_images = {
                    row["media_hash"]
                    for row in db.execute(
                        """SELECT ci.media_hash FROM character_images ci JOIN media_blobs mb ON mb.hash=ci.media_hash
                       WHERE ci.character_id=?""",
                        (candidate.subject_id,),
                    ).fetchall()
                }
                candidate = Candidate(
                    candidate.subject_id,
                    "character",
                    character["name"],
                    tuple(json.loads(character["aliases_json"])),
                    character["gender"],
                    int(character["revision"]),
                    tuple(sorted(active_images)),
                )
                if not candidate.image_hashes:
                    continue
            occupied = (
                require_unoccupied
                and db.execute(
                    "SELECT 1 FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND subject_id=? AND state='active'",
                    (scope_id, period_id, mode, candidate.subject_id),
                ).fetchone()
            )
            if not occupied:
                output.append(candidate)
        return output

    def _pick(
        self,
        candidates: Sequence[Candidate],
        mode: Mode,
        owner_id: str,
        intimacy: dict[str, int],
        activity: dict[str, int],
        weights: dict[str, float],
        activity_full_messages: int,
    ) -> Candidate:
        if mode != "member" or len(candidates) == 1:
            return self.chooser.choice(list(candidates))
        base = float(weights.get("base", 1.0))
        per_intimacy = float(weights.get("per_intimacy_point", 0.01))
        maximum = float(weights.get("maximum", 5.0))
        floor = float(weights.get("activity_floor", 0.2))
        target = [candidate.subject_id for candidate in candidates]
        intimacy_points = [max(0, int(intimacy.get(user_id, 0))) for user_id in target]
        activity_counts = [max(0, int(activity.get(user_id, 0))) for user_id in target]
        baseline = [min(maximum, base + min(max(0.0, (maximum - base) / per_intimacy), points) * per_intimacy) if per_intimacy else base for points in intimacy_points]
        activity_factor = [floor + (1.0 - floor) * min(count / max(1, activity_full_messages), 1.0) for count in activity_counts]
        return self.chooser.choices(list(candidates), weights=[a * b for a, b in zip(baseline, activity_factor)], k=1)[0]

    def _snapshot(self, db: sqlite3.Connection, candidate: Candidate, now: int) -> str:
        snapshot_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,source_revision,created_at) VALUES(?,?,?,?,?,?,?)",
            (snapshot_id, candidate.subject_kind, candidate.subject_id, candidate.name, json.dumps(candidate.aliases, ensure_ascii=False), candidate.source_revision, now),
        )
        for digest in dict.fromkeys(candidate.image_hashes):
            db.execute("INSERT INTO snapshot_images(snapshot_id,media_hash) VALUES(?,?)", (snapshot_id, digest))
        return snapshot_id

    def _transfer(self, db: sqlite3.Connection, relationship: sqlite3.Row, new_owner: str, capacity: int, now: int, reason: str, slot_kind: str = "normal") -> str:
        db.execute(
            "UPDATE relationships SET state='ended',ended_at=?,end_reason=?,version=version+1 WHERE id=? AND state='active'",
            (now, reason, relationship["id"]),
        )
        new_id = self.storage.create_relationship(
            scope_id=int(relationship["scope_id"]),
            period_id=relationship["period_id"],
            mode=relationship["mode"],
            owner_id=new_owner,
            subject_kind=relationship["subject_kind"],
            subject_id=relationship["subject_id"],
            snapshot_id=relationship["snapshot_id"],
            capacity=capacity,
            now=now,
            slot_kind=slot_kind,
        )
        return new_id

    def _owner_relationships(self, db: sqlite3.Connection, scope_id: int, period_id: str, mode: str, owner_id: str, slot_kind: str = "normal") -> list[dict[str, Any]]:
        rows = db.execute(
            """SELECT r.id,r.scope_id,r.period_id,r.mode,r.owner_id,r.subject_id,r.subject_kind,r.snapshot_id,r.version,s.name,s.aliases_json
               FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
               WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=? AND r.slot_kind=? AND r.state='active'
               ORDER BY r.acquired_at,r.id""",
            (scope_id, period_id, mode, owner_id, slot_kind),
        ).fetchall()
        return [dict(row) | {"aliases": json.loads(row["aliases_json"])} for row in rows]

    @staticmethod
    def _locked_relationship(
        db: sqlite3.Connection,
        scope_id: int,
        period_id: str,
        mode: str,
        owner_id: str,
        relationship_id: str | None,
        subject_id: str | None,
    ) -> bool:
        if relationship_id is None and subject_id is None:
            qualifier = ""
            params = (scope_id, period_id, mode, owner_id)
        else:
            column, value = (
                ("id", relationship_id)
                if relationship_id is not None
                else ("subject_id", subject_id)
            )
            qualifier = f" AND {column}=?"
            params = (scope_id, period_id, mode, owner_id, value)
        return (
            db.execute(
                f"""SELECT 1 FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=?
                AND slot_kind IN ('steal','designated') AND state='active'{qualifier} LIMIT 1""",
                params,
            ).fetchone()
            is not None
        )

    def _choose_owned(self, relations: Sequence[dict[str, Any]], relation_id: str | None, subject_id: str | None) -> dict[str, Any] | None:
        matches = [row for row in relations if (relation_id is None or row["id"] == relation_id) and (subject_id is None or row["subject_id"] == subject_id)]
        if not matches:
            return None
        return self.chooser.choice(list(matches)) if relation_id is None and subject_id is None else matches[0]

    def _counter(self, db: sqlite3.Connection, scope_id: int, period_id: str, mode: str, user_id: str) -> sqlite3.Row:
        db.execute(
            "INSERT OR IGNORE INTO daily_counters(scope_id,period_id,mode,user_id) VALUES(?,?,?,?)",
            (scope_id, period_id, mode, user_id),
        )
        return db.execute(
            "SELECT * FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
            (scope_id, period_id, mode, user_id),
        ).fetchone()

    def _current_period(self, db: sqlite3.Connection, scope_id: int, period_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM periods WHERE id=? AND scope_id=? AND state='current'", (period_id, scope_id)).fetchone()
        if row is None:
            raise StorageError("请求的周期已结束或不属于当前群。")
        return row

    def _issue_credit(self, db: sqlite3.Connection, scope_id: int, period_id: str, mode: Mode, user_id: str, operation_id: str, reason: str, relation_id: str, now: int) -> None:
        source_id = f"{operation_id}:{relation_id}"
        db.execute(
            "INSERT OR IGNORE INTO redraw_credits(id,scope_id,period_id,mode,user_id,source_operation_id,reason,state,issued_at) VALUES(?,?,?,?,?,?,?,'available',?)",
            (str(uuid.uuid4()), scope_id, period_id, mode, user_id, source_id, reason, now),
        )

    def _invalidate_relation_invites(self, db: sqlite3.Connection, relation_id: str, now: int, reason: str) -> None:
        db.execute(
            "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason=? WHERE relationship_id=? AND state='pending'",
            (now, reason, relation_id),
        )

    def _outbox(self, db: sqlite3.Connection, scope_id: int, dedupe_key: str, umo: str, payload: dict[str, Any], now: int) -> None:
        db.execute(
            "INSERT OR IGNORE INTO notification_outbox(id,scope_id,dedupe_key,umo,payload_json,state,created_at) VALUES(?,?,?,?,?,'pending',?)",
            (str(uuid.uuid4()), scope_id, dedupe_key, umo, json.dumps(payload, ensure_ascii=False), now),
        )

    def _replay(self, db: sqlite3.Connection, scope_id: int, event_key: str | None, payload_hash: str) -> BusinessResult | None:
        if not event_key:
            return None
        row = db.execute("SELECT payload_hash,command_operation_id FROM processed_events WHERE scope_id=? AND event_key=?", (scope_id, event_key)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise StorageError("同一事件键对应不同消息载荷，拒绝重复执行。")
        if not row["command_operation_id"]:
            return None
        operation = db.execute("SELECT result_code,data_json FROM operation_log WHERE id=?", (row["command_operation_id"],)).fetchone()
        if operation is None:
            raise StorageError("幂等事件的操作记录缺失。")
        data = json.loads(operation["data_json"])
        return BusinessResult(operation["result_code"], data, replayed=True)

    def _record_result(
        self, db: sqlite3.Connection, operation_id: str, scope_id: int, period_id: str, mode: str | None,
        actor_id: str, kind: str, code: str, data: dict[str, Any], config_revision: str, now: int,
        event_key: str | None, payload_hash: str,
    ) -> BusinessResult:
        result = BusinessResult(code, data)
        self._record(db, operation_id, scope_id, period_id, mode, actor_id, kind, result, config_revision, now, event_key, payload_hash)
        return result

    def _record(
        self, db: sqlite3.Connection, operation_id: str, scope_id: int, period_id: str, mode: str | None,
        actor_id: str, kind: str, result: BusinessResult, config_revision: str, now: int,
        event_key: str | None, payload_hash: str,
    ) -> None:
        db.execute(
            """INSERT OR IGNORE INTO operation_log(id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,created_at)
               VALUES(?,?,?,?,?,?,'qq',?,?,?,?)""",
            (operation_id, scope_id, period_id, mode, kind, actor_id, result.code, config_revision, json.dumps(result.data, ensure_ascii=False, allow_nan=False), now),
        )
        if event_key:
            db.execute(
                "INSERT OR IGNORE INTO processed_events(scope_id,event_key,payload_hash,observed_at,command_operation_id) VALUES(?,?,?,?,?)",
                (scope_id, event_key, payload_hash, now, operation_id),
            )
            db.execute(
                "UPDATE processed_events SET command_operation_id=? WHERE scope_id=? AND event_key=? AND command_operation_id IS NULL",
                (operation_id, scope_id, event_key),
            )


def _candidate_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "subject_id": candidate.subject_id,
        "subject_kind": candidate.subject_kind,
        "name": candidate.name,
        "aliases": list(candidate.aliases),
        "gender": candidate.gender,
        "image_hashes": list(candidate.image_hashes),
    }


def math_probability(value: float) -> bool:
    """拒绝 NaN/Infinity 和范围外概率。"""

    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and 0 <= value <= 1 and value not in {float("inf"), float("-inf")}
