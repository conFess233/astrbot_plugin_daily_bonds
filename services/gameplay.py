"""三种玩法的抽取、抢夺、赠送、邀请和离婚事务。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from ..models.gameplay import BusinessResult, Candidate
from ..models.scope import Mode
from .gameplay_base import _candidate_dict, math_probability
from .gameplay_gift import GiftMixin
from .statistics import StatisticsService


class GameplayService(GiftMixin):
    """共享 Handler/Web 调用的事务服务；随机源与时钟均可注入。"""

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
        activity_full_messages: int
        | None = None,  # 兼容旧服务调用；此参数不再影响权重。
        activity_window_days: int = 7,
        activity_timezone: str = "Asia/Shanghai",
        eligible_user_ids: set[str] | None = None,
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
            # 持有上限由所有来源的关系共用。
            active = self._owner_relationships(
                db, scope_id, period_id, mode, owner_id, None
            )
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
            intimacy: dict[str, int] = {}
            activity: dict[str, int] = {}
            if mode == "member" and available:
                # 在同一写事务内复查统计资格，避免仅在 Handler 预筛选。
                present = {
                    str(row["user_id"])
                    for row in db.execute(
                        "SELECT user_id FROM members WHERE scope_id=? AND is_present=1 AND is_known_bot=0",
                        (scope_id,),
                    )
                }
                if eligible_user_ids is not None:
                    present &= eligible_user_ids
                statistics = StatisticsService(self.storage)
                edges = statistics.intimacy_rows(scope_id, eligible_user_ids=present)
                rows = statistics.activity_rows(
                    scope_id,
                    now=now,
                    window_days=activity_window_days,
                    timezone=activity_timezone,
                    eligible_user_ids=present,
                )
                participants = {str(row["user_id"]) for row in rows}
                participants.update(
                    str(row[key]) for row in edges for key in ("source_id", "target_id")
                )
                available = [
                    candidate
                    for candidate in available
                    if candidate.subject_id in participants
                ]
                intimacy = {
                    str(row["target_id"]): int(row["score"])
                    for row in edges
                    if row["source_id"] == owner_id
                }
                activity = {
                    str(row["user_id"]): int(row["active_days"]) for row in rows
                }
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
            picked = self._pick(
                available,
                mode,
                owner_id,
                intimacy,
                activity,
                weights or {},
                activity_window_days,
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
        limit: int,
        pool_ids: Sequence[str] = (),
        unique: bool = True,
        operation_id: str | None = None,
        event_key: str | None = None,
        payload_hash: str = "",
        config_revision: str = "",
        now: int,
    ) -> BusinessResult:
        """指定角色：占用共同持有上限，按独立次数计数，不消耗普通额度或补抽资格。"""

        if (
            mode not in {"wife", "husband"}
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (capacity, limit)
            )
            or not 1 <= capacity <= 100
            or not 0 <= limit <= 100
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
                db, scope_id, period_id, mode, owner_id, None
            )
            counter = self._counter(db, scope_id, period_id, mode, owner_id)
            if limit == 0:
                code = "DESIGNATED_DISABLED"
            elif len(active) >= capacity:
                code, data = "CAPACITY_FULL", {"relationships": active}
            elif int(counter["designated_draws"]) >= limit:
                code, data = "NO_DESIGNATED_QUOTA", {"capacity": limit}
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
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "INVALID_TARGET",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            actor_relationships = self._owner_relationships(
                db, scope_id, period_id, mode, actor_id, None
            )
            if len(actor_relationships) >= capacity:
                code = "CAPACITY_FULL"
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    code,
                    {"relationships": actor_relationships},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            counter = self._counter(db, scope_id, period_id, mode, actor_id)
            if attempt_limit and int(counter["steal_attempts"]) >= attempt_limit:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "STEAL_LIMIT",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if (
                cooldown_seconds
                and counter["last_steal_at"] is not None
                and now - int(counter["last_steal_at"]) < cooldown_seconds
            ):
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "STEAL_COOLDOWN",
                    {"available_at": int(counter["last_steal_at"]) + cooldown_seconds},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            target_relations = self._owner_relationships(
                db, scope_id, period_id, mode, target_id
            )
            target_relation = self._choose_owned(
                target_relations, relation_id, subject_id
            )
            if target_relation is None:
                if self._locked_relationship(
                    db, scope_id, period_id, mode, target_id, relation_id, subject_id
                ):
                    return self._record_result(
                        db,
                        operation_id,
                        scope_id,
                        period_id,
                        mode,
                        actor_id,
                        "steal",
                        "STEAL_SLOT_LOCKED",
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
                    actor_id,
                    "steal",
                    "NO_TARGET_RELATIONSHIP",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            target_counter = self._counter(db, scope_id, period_id, mode, target_id)
            if (
                target_protection_limit
                and int(target_counter["stolen_successes"]) >= target_protection_limit
            ):
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "TARGET_PROTECTED",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if mode == "member" and target_relation["subject_id"] == actor_id:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "INVALID_TARGET",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            if not math_probability(probability):
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "INVALID_PROBABILITY",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            success = self.chooser.random() < probability
            db.execute(
                """UPDATE daily_counters SET steal_attempts=steal_attempts+1,last_steal_at=?
                   WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?""",
                (now, scope_id, period_id, mode, actor_id),
            )
            if not success:
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    actor_id,
                    "steal",
                    "STEAL_FAILED",
                    {
                        "probability": probability,
                        "relationship_id": target_relation["id"],
                        "subject_id": target_relation["subject_id"],
                        "old_owner_id": target_id,
                    },
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            self._transfer(
                db,
                target_relation,
                actor_id,
                capacity,
                now,
                "stolen",
                "steal",
            )
            db.execute(
                "UPDATE daily_counters SET stolen_successes=stolen_successes+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, target_id),
            )
            self._issue_credit(
                db,
                scope_id,
                period_id,
                mode,
                target_id,
                operation_id,
                "stolen",
                target_relation["id"],
                now,
            )
            self._invalidate_relation_invites(
                db, target_relation["id"], now, "relationship_stolen"
            )
            return self._record_result(
                db,
                operation_id,
                scope_id,
                period_id,
                mode,
                actor_id,
                "steal",
                "STOLEN",
                {
                    "relationship_id": target_relation["id"],
                    "subject_id": target_relation["subject_id"],
                    "old_owner_id": target_id,
                },
                config_revision,
                now,
                event_key,
                payload_hash,
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
                return self._record_result(
                    db,
                    operation_id,
                    scope_id,
                    period_id,
                    mode,
                    owner_id,
                    "divorce",
                    "DIVORCE_LIMIT",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            relations = self._owner_relationships(
                db, scope_id, period_id, mode, owner_id
            )
            relationship = self._choose_owned(relations, relationship_id, subject_id)
            if relationship is None:
                if self._locked_relationship(
                    db, scope_id, period_id, mode, owner_id, relationship_id, subject_id
                ):
                    return self._record_result(
                        db,
                        operation_id,
                        scope_id,
                        period_id,
                        mode,
                        owner_id,
                        "divorce",
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
                    owner_id,
                    "divorce",
                    "RELATIONSHIP_NOT_FOUND",
                    {},
                    config_revision,
                    now,
                    event_key,
                    payload_hash,
                )
            db.execute(
                "UPDATE relationships SET state='ended',ended_at=?,end_reason='divorce',version=version+1 WHERE id=? AND state='active'",
                (now, relationship["id"]),
            )
            db.execute(
                "UPDATE daily_counters SET divorces=divorces+1 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, owner_id),
            )
            self._issue_credit(
                db,
                scope_id,
                period_id,
                mode,
                owner_id,
                operation_id,
                "divorce",
                relationship["id"],
                now,
            )
            self._invalidate_relation_invites(
                db, relationship["id"], now, "relationship_divorced"
            )
            return self._record_result(
                db,
                operation_id,
                scope_id,
                period_id,
                mode,
                owner_id,
                "divorce",
                "DIVORCED",
                {
                    "relationship_id": relationship["id"],
                    "subject_id": relationship["subject_id"],
                    "snapshot_id": relationship["snapshot_id"],
                },
                config_revision,
                now,
                event_key,
                payload_hash,
            )
