"""OneBot 原始消息与戳事件的幂等统计及群友抽取权重。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..models import StorageError
from .storage import SQLiteStorage


class StatisticsService:
    """统计有向亲密度和 UTC 秒级活跃度，二者分别幂等提交。"""

    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def record_message(
        self,
        *,
        scope_id: int,
        raw_event: Mapping[str, Any],
        sender_id: str,
        mentioned_ids: Sequence[str],
        eligible_user_ids: set[str],
        bot_ids: set[str],
        intimacy_enabled: bool,
        activity_enabled: bool,
        active_points: int,
        passive_points: int,
        now: int,
        timezone: str = "Asia/Shanghai",
    ) -> bool:
        """记录一条真人群消息及本次原始 At 段；重放不重复计分。"""

        if not sender_id or sender_id not in eligible_user_ids or sender_id in bot_ids:
            return False
        ZoneInfo(timezone)  # 保留 UTC 秒时间戳，查询时按生效时区计算自然日。
        targets = tuple(
            sorted(
                {
                    str(user_id)
                    for user_id in mentioned_ids
                    if user_id
                    and user_id != sender_id
                    and user_id in eligible_user_ids
                    and user_id not in bot_ids
                }
            )
        )
        key, digest = _event_identity(raw_event, "message", now)
        return self._apply(
            scope_id=scope_id,
            event_key=key,
            payload_hash=digest,
            now=now,
            sender_id=sender_id,
            targets=targets,
            activity=activity_enabled,
            intimacy=intimacy_enabled,
            active_points=active_points,
            passive_points=passive_points,
        )

    def record_poke(
        self,
        *,
        scope_id: int,
        raw_event: Mapping[str, Any],
        source_id: str,
        target_id: str,
        eligible_user_ids: set[str],
        bot_ids: set[str],
        intimacy_enabled: bool,
        active_points: int,
        passive_points: int,
        now: int,
        activity_enabled: bool = True,
        timezone: str = "Asia/Shanghai",
    ) -> bool:
        """合格主动戳计活跃日；只有真人合格双方才记有向分值，不增加消息数。"""

        if (
            not source_id
            or not target_id
            or source_id == target_id
            or source_id not in eligible_user_ids
            or source_id in bot_ids
        ):
            return False
        intimacy = (
            intimacy_enabled
            and target_id in eligible_user_ids
            and target_id not in bot_ids
        )
        if not (activity_enabled or intimacy):
            return False
        ZoneInfo(timezone)
        key, digest = _event_identity(raw_event, "poke", now)
        return self._apply(
            scope_id=scope_id,
            event_key=key,
            payload_hash=digest,
            now=now,
            sender_id=source_id,
            targets=(target_id,),
            activity=False,
            intimacy=intimacy,
            active_points=active_points,
            passive_points=passive_points,
            poke_activity=activity_enabled,
        )

    def pair_intimacy(
        self, scope_id: int, source_id: str, target_id: str
    ) -> dict[str, Any]:
        """读取两个累计有向分值；缺失的方向返回零，不合并为双向总分。"""
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    """SELECT source_id,target_id,score FROM intimacy_edges WHERE scope_id=?
                   AND ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))""",
                    (scope_id, source_id, target_id, target_id, source_id),
                )
                .fetchall()
            )
        scores = {
            (row["source_id"], row["target_id"]): int(row["score"]) for row in rows
        }
        return {
            "source_id": source_id,
            "target_id": target_id,
            "forward_score": scores.get((source_id, target_id), 0),
            "reverse_score": scores.get((target_id, source_id), 0),
        }

    def activity_rows(
        self,
        scope_id: int,
        *,
        now: int,
        window_days: int,
        timezone: str = "Asia/Shanghai",
        eligible_user_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """同一滚动窗口内的消息数与自然日活跃天数；主动戳只贡献活跃日。"""
        if not 1 <= window_days <= 365:
            raise ValueError("window_days 必须在 1 到 365 之间。")
        zone = ZoneInfo(timezone)
        start = now - window_days * 86400
        totals: dict[str, int] = {}
        active_days: dict[str, int] = {}
        day = datetime.fromtimestamp(start, zone).date()
        last_day = datetime.fromtimestamp(now, zone).date()
        # 按自然日边界在数据库聚合，避免把窗口内所有秒桶搬到 Python。
        with self.storage._lock:
            while day <= last_day:
                tomorrow = day + timedelta(days=1)
                lower = max(
                    start + 1, int(datetime.combine(day, time(), zone).timestamp())
                )
                upper = min(
                    now + 1, int(datetime.combine(tomorrow, time(), zone).timestamp())
                )
                rows = (
                    self.storage._connection()
                    .execute(
                        """SELECT user_id,SUM(messages) AS messages FROM (
                       SELECT user_id,message_count AS messages FROM activity_seconds
                       WHERE scope_id=? AND observed_second>=? AND observed_second<?
                       UNION ALL SELECT user_id,0 FROM activity_pokes
                       WHERE scope_id=? AND observed_second>=? AND observed_second<?)
                       GROUP BY user_id""",
                        (scope_id, lower, upper, scope_id, lower, upper),
                    )
                    .fetchall()
                )
                for row in rows:
                    user_id = str(row["user_id"])
                    if (
                        eligible_user_ids is not None
                        and user_id not in eligible_user_ids
                    ):
                        continue
                    totals[user_id] = totals.get(user_id, 0) + int(row["messages"])
                    active_days[user_id] = active_days.get(user_id, 0) + 1
                day = tomorrow
        return sorted(
            [
                {
                    "user_id": user_id,
                    "messages": count,
                    "active_days": active_days[user_id],
                }
                for user_id, count in totals.items()
            ],
            key=lambda item: (-item["messages"], item["user_id"]),
        )

    def intimacy_rows(
        self, scope_id: int, *, eligible_user_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """排行榜与抽取共用本群有效成员之间的亲密度记录，不限个人榜范围。"""
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT source_id,target_id,score FROM intimacy_edges WHERE scope_id=? ORDER BY score DESC,source_id,target_id",
                    (scope_id,),
                )
                .fetchall()
            )
        return [
            dict(row)
            for row in rows
            if eligible_user_ids is None
            or (
                row["source_id"] in eligible_user_ids
                and row["target_id"] in eligible_user_ids
            )
        ]

    def member_scores(
        self,
        scope_id: int,
        actor_id: str,
        candidate_ids: Sequence[str],
        now: int,
        window_days: int,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """读取一个一致性短事务内的有向亲密度与滚动活跃度。"""

        ids = tuple(dict.fromkeys(candidate_ids))
        if not ids:
            return {}, {}
        marks = ",".join("?" for _ in ids)
        with self.storage.transaction(immediate=False) as db:
            edges = db.execute(
                f"SELECT target_id,score FROM intimacy_edges WHERE scope_id=? AND source_id=? AND target_id IN ({marks})",
                (scope_id, actor_id, *ids),
            ).fetchall()
            buckets = db.execute(
                f"""SELECT user_id,SUM(message_count) AS messages FROM activity_seconds
                    WHERE scope_id=? AND user_id IN ({marks}) AND observed_second>? AND observed_second<=?
                    GROUP BY user_id""",
                (scope_id, *ids, now - window_days * 86400, now),
            ).fetchall()
        return (
            {row["target_id"]: int(row["score"]) for row in edges},
            {row["user_id"]: int(row["messages"]) for row in buckets},
        )

    def _apply(
        self,
        *,
        scope_id: int,
        event_key: str,
        payload_hash: str,
        now: int,
        sender_id: str,
        targets: Sequence[str],
        activity: bool,
        intimacy: bool,
        active_points: int,
        passive_points: int,
        poke_activity: bool = False,
    ) -> bool:
        try:
            with self.storage.transaction() as db:
                prior = db.execute(
                    "SELECT payload_hash,stats_applied FROM processed_events WHERE scope_id=? AND event_key=?",
                    (scope_id, event_key),
                ).fetchone()
                if prior:
                    if prior["payload_hash"] != payload_hash:
                        raise StorageError("统计事件键发生载荷冲突。")
                    if prior["stats_applied"]:
                        return False
                else:
                    db.execute(
                        "INSERT INTO processed_events(scope_id,event_key,payload_hash,observed_at,stats_applied) VALUES(?,?,?,?,0)",
                        (scope_id, event_key, payload_hash, now),
                    )
                if activity:
                    db.execute(
                        """INSERT INTO activity_seconds(scope_id,user_id,observed_second,message_count) VALUES(?,?,?,1)
                           ON CONFLICT(scope_id,user_id,observed_second) DO UPDATE SET message_count=message_count+1""",
                        (scope_id, sender_id, now),
                    )
                if poke_activity:
                    db.execute(
                        "INSERT OR IGNORE INTO activity_pokes(scope_id,user_id,observed_second) VALUES(?,?,?)",
                        (scope_id, sender_id, now),
                    )
                if intimacy:
                    for target_id in targets:
                        _add_score(
                            db, scope_id, sender_id, target_id, active_points, now
                        )
                        _add_score(
                            db, scope_id, target_id, sender_id, passive_points, now
                        )
                db.execute(
                    "UPDATE processed_events SET stats_applied=1 WHERE scope_id=? AND event_key=?",
                    (scope_id, event_key),
                )
            return True
        except sqlite3.Error as exc:
            raise StorageError(f"群聊统计事务失败：{exc}") from exc


def _add_score(
    db: sqlite3.Connection,
    scope_id: int,
    source_id: str,
    target_id: str,
    delta: int,
    now: int,
) -> None:
    if delta <= 0:
        return
    db.execute(
        """INSERT INTO intimacy_edges(scope_id,source_id,target_id,score,updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(scope_id,source_id,target_id) DO UPDATE SET score=score+excluded.score,updated_at=excluded.updated_at""",
        (scope_id, source_id, target_id, delta, now),
    )


def _event_identity(raw: Mapping[str, Any], prefix: str, now: int) -> tuple[str, str]:
    payload = json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    event_id = raw.get("message_id") or raw.get("notice_id") or raw.get("id")
    if event_id is None:
        source = json.dumps(
            [
                prefix,
                raw.get("post_type"),
                raw.get("notice_type"),
                raw.get("sub_type"),
                raw.get("user_id"),
                raw.get("target_id"),
                now,
                digest,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event_id = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"{prefix}:{event_id}", digest
