"""玩法事务共用的候选筛选、关系查询、计数与幂等记录。"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any

from ..models.errors import StorageError
from ..models.gameplay import BusinessResult, Candidate
from ..models.scope import Mode
from .message_templates import render_message
from .storage import SQLiteStorage


class GameplayBase:
    """玩法事务的共享状态与内部工具；随机源与时钟均可注入。"""

    def __init__(
        self, storage: SQLiteStorage, chooser: random.Random | None = None
    ) -> None:
        self.storage = storage
        self.chooser = chooser or random.SystemRandom()

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
                    member["card"] or member["nickname"] or "群友",
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
        activity_window_days: int,
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
        baseline = [
            min(
                maximum,
                base
                + min(max(0.0, (maximum - base) / per_intimacy), points) * per_intimacy,
            )
            if per_intimacy
            else base
            for points in intimacy_points
        ]
        activity_factor = [
            floor + (1.0 - floor) * min(count / max(1, activity_window_days), 1.0)
            for count in activity_counts
        ]
        return self.chooser.choices(
            list(candidates),
            weights=[a * b for a, b in zip(baseline, activity_factor)],
            k=1,
        )[0]

    def _snapshot(self, db: sqlite3.Connection, candidate: Candidate, now: int) -> str:
        snapshot_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,source_revision,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                snapshot_id,
                candidate.subject_kind,
                candidate.subject_id,
                candidate.name,
                json.dumps(candidate.aliases, ensure_ascii=False),
                candidate.source_revision,
                now,
            ),
        )
        for digest in dict.fromkeys(candidate.image_hashes):
            db.execute(
                "INSERT INTO snapshot_images(snapshot_id,media_hash) VALUES(?,?)",
                (snapshot_id, digest),
            )
        return snapshot_id

    def _transfer(
        self,
        db: sqlite3.Connection,
        relationship: sqlite3.Row,
        new_owner: str,
        capacity: int,
        now: int,
        reason: str,
        slot_kind: str = "normal",
    ) -> str:
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
        db.execute(
            "UPDATE relationships SET acquisition_kind=?,source_owner_id=? WHERE id=?",
            (
                "gift" if reason == "gifted" else "steal",
                relationship["owner_id"],
                new_id,
            ),
        )
        return new_id

    def _owner_relationships(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        period_id: str,
        mode: str,
        owner_id: str,
        slot_kind: str | None = "normal",
    ) -> list[dict[str, Any]]:
        """返回持有者的当前关系；`slot_kind=None` 表示全部来源（用于容量判断）。"""

        slot_filter = "" if slot_kind is None else " AND r.slot_kind=?"
        rows = db.execute(
            f"""SELECT r.id,r.scope_id,r.period_id,r.mode,r.owner_id,r.subject_id,r.subject_kind,r.snapshot_id,r.version,r.slot_kind,s.name,s.aliases_json
               FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
               WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=?{slot_filter} AND r.state='active'
               ORDER BY r.acquired_at,r.id""",
            (scope_id, period_id, mode, owner_id)
            + (() if slot_kind is None else (slot_kind,)),
        ).fetchall()
        return [
            dict(row) | {"aliases": json.loads(row["aliases_json"])} for row in rows
        ]

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

    def _choose_owned(
        self,
        relations: Sequence[dict[str, Any]],
        relation_id: str | None,
        subject_id: str | None,
    ) -> dict[str, Any] | None:
        matches = [
            row
            for row in relations
            if (relation_id is None or row["id"] == relation_id)
            and (subject_id is None or row["subject_id"] == subject_id)
        ]
        if not matches:
            return None
        return (
            self.chooser.choice(list(matches))
            if relation_id is None and subject_id is None
            else matches[0]
        )

    def _counter(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        period_id: str,
        mode: str,
        user_id: str,
    ) -> sqlite3.Row:
        db.execute(
            "INSERT OR IGNORE INTO daily_counters(scope_id,period_id,mode,user_id) VALUES(?,?,?,?)",
            (scope_id, period_id, mode, user_id),
        )
        return db.execute(
            "SELECT * FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
            (scope_id, period_id, mode, user_id),
        ).fetchone()

    def _current_period(
        self, db: sqlite3.Connection, scope_id: int, period_id: str
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM periods WHERE id=? AND scope_id=? AND state='current'",
            (period_id, scope_id),
        ).fetchone()
        if row is None:
            raise StorageError("请求的周期已结束或不属于当前群。")
        return row

    def _issue_credit(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        period_id: str,
        mode: Mode,
        user_id: str,
        operation_id: str,
        reason: str,
        relation_id: str,
        now: int,
    ) -> None:
        source_id = f"{operation_id}:{relation_id}"
        db.execute(
            "INSERT OR IGNORE INTO redraw_credits(id,scope_id,period_id,mode,user_id,source_operation_id,reason,state,issued_at) VALUES(?,?,?,?,?,?,?,'available',?)",
            (
                str(uuid.uuid4()),
                scope_id,
                period_id,
                mode,
                user_id,
                source_id,
                reason,
                now,
            ),
        )

    def _invalidate_relation_invites(
        self, db: sqlite3.Connection, relation_id: str, now: int, reason: str
    ) -> None:
        db.execute(
            "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason=? WHERE relationship_id=? AND state='pending'",
            (now, reason, relation_id),
        )

    def _outbox(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        dedupe_key: str,
        umo: str,
        payload: dict[str, Any],
        now: int,
    ) -> None:
        if payload.get("kind") in {"gift_invite", "gift_invite_final"}:
            invite = db.execute(
                "SELECT * FROM gift_invites WHERE scope_id=? AND id=?",
                (scope_id, payload["invite_id"]),
            ).fetchone()
            if invite:
                config, _ = self.storage.get_settings(scope_id)
                names = {
                    str(row["user_id"]): row["card"] or row["nickname"] or "群友"
                    for row in db.execute(
                        "SELECT user_id,card,nickname FROM members WHERE scope_id=?",
                        (scope_id,),
                    )
                }
                relation = db.execute(
                    "SELECT r.subject_id,r.subject_kind,s.name FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id WHERE r.scope_id=? AND r.id=?",
                    (scope_id, invite["relationship_id"]),
                ).fetchone()
                subject = (
                    names.get(str(relation["subject_id"]), "群友")
                    if relation and relation["subject_kind"] == "member"
                    else relation["name"]
                    if relation
                    else "对象"
                )
                state = payload.get("state", "pending")
                key = (
                    "invite_created"
                    if state == "pending"
                    else "gift_accepted"
                    if state == "accepted"
                    else "invite_" + state
                )
                text = render_message(
                    config,
                    "results",
                    key,
                    actor_name=names.get(
                        str(
                            invite["sender_id"]
                            if state in {"pending", "cancelled"}
                            else invite["recipient_id"]
                        ),
                        "群友",
                    ),
                    sender_name=names.get(str(invite["sender_id"]), "群友"),
                    recipient_name=names.get(str(invite["recipient_id"]), "群友"),
                    subject_name=subject,
                    name=subject,
                    invite_id=invite["id"],
                    accept_keyword=config["commands"]["keywords"]["gift_accept"][0],
                )
                if not text.strip():
                    return
                payload["text"] = (
                    text if state == "pending" else f"赠送邀请 {invite['id']}：{text}"
                )
        db.execute(
            "INSERT OR IGNORE INTO notification_outbox(id,scope_id,dedupe_key,umo,payload_json,state,created_at) VALUES(?,?,?,?,?,'pending',?)",
            (
                str(uuid.uuid4()),
                scope_id,
                dedupe_key,
                umo,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )

    def _replay(
        self,
        db: sqlite3.Connection,
        scope_id: int,
        event_key: str | None,
        payload_hash: str,
    ) -> BusinessResult | None:
        if not event_key:
            return None
        row = db.execute(
            "SELECT payload_hash,command_operation_id FROM processed_events WHERE scope_id=? AND event_key=?",
            (scope_id, event_key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise StorageError("同一事件键对应不同消息载荷，拒绝重复执行。")
        if not row["command_operation_id"]:
            return None
        operation = db.execute(
            "SELECT result_code,data_json FROM operation_log WHERE id=?",
            (row["command_operation_id"],),
        ).fetchone()
        if operation is None:
            raise StorageError("幂等事件的操作记录缺失。")
        data = json.loads(operation["data_json"])
        return BusinessResult(operation["result_code"], data, replayed=True)

    def _record_result(
        self,
        db: sqlite3.Connection,
        operation_id: str,
        scope_id: int,
        period_id: str,
        mode: str | None,
        actor_id: str,
        kind: str,
        code: str,
        data: dict[str, Any],
        config_revision: str,
        now: int,
        event_key: str | None,
        payload_hash: str,
    ) -> BusinessResult:
        result = BusinessResult(code, data)
        self._record(
            db,
            operation_id,
            scope_id,
            period_id,
            mode,
            actor_id,
            kind,
            result,
            config_revision,
            now,
            event_key,
            payload_hash,
        )
        return result

    def _record(
        self,
        db: sqlite3.Connection,
        operation_id: str,
        scope_id: int,
        period_id: str,
        mode: str | None,
        actor_id: str,
        kind: str,
        result: BusinessResult,
        config_revision: str,
        now: int,
        event_key: str | None,
        payload_hash: str,
    ) -> None:
        db.execute(
            """INSERT OR IGNORE INTO operation_log(id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,created_at)
               VALUES(?,?,?,?,?,?,'qq',?,?,?,?)""",
            (
                operation_id,
                scope_id,
                period_id,
                mode,
                kind,
                actor_id,
                result.code,
                config_revision,
                json.dumps(result.data, ensure_ascii=False, allow_nan=False),
                now,
            ),
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

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )
