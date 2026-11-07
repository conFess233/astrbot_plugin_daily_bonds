"""Read-only, scope-bound queries for the authenticated management page."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .statistics import StatisticsService
from .storage import SQLiteStorage


class AdminQueryService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def dashboard(
        self, scope_id: int | None, *, days: int, timezone: str, now: int
    ) -> dict[str, Any]:
        """看板数据：操作次数来自操作记录，最长等于历史保留天数；角色热度为累计。"""

        zone = ZoneInfo(timezone)
        today = datetime.fromtimestamp(now, zone).date()
        dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        since = int(
            datetime(
                dates[0].year, dates[0].month, dates[0].day, tzinfo=zone
            ).timestamp()
        )
        where = "" if scope_id is None else " AND scope_id=?"
        params = (since,) if scope_id is None else (since, scope_id)
        with self.storage.read() as db:
            results = db.execute(
                f"""SELECT result_code,COUNT(*) AS amount,COUNT(DISTINCT actor_id) AS actors
                    FROM operation_log WHERE created_at>=? AND actor_type='qq'{where}
                    GROUP BY result_code""",
                params,
            ).fetchall()
            actors = db.execute(
                f"SELECT COUNT(DISTINCT actor_id) FROM operation_log WHERE created_at>=? AND actor_type='qq'{where}",
                params,
            ).fetchone()[0]
            # 按小时聚合后再换算成本地日期，夏令时切换日也能正确归属。
            hours = db.execute(
                f"""SELECT created_at/3600 AS hour,COUNT(*) AS amount FROM operation_log
                    WHERE created_at>=? AND result_code IN ('DRAWN','DESIGNATED_DRAWN','FORCED'){where}
                    GROUP BY hour""",
                params,
            ).fetchall()
            collection_where = "" if scope_id is None else "WHERE e.scope_id=?"
            characters = db.execute(
                f"""SELECT COALESCE(c.name,e.character_id) AS name,SUM(e.times) AS times
                    FROM collection_entries e LEFT JOIN characters c ON c.id=e.character_id
                    {collection_where} GROUP BY e.character_id
                    ORDER BY times DESC,name LIMIT 10""",
                () if scope_id is None else (scope_id,),
            ).fetchall()
            groups = (
                db.execute(
                    """SELECT s.group_id,COUNT(*) AS amount FROM operation_log o
                       JOIN scopes s ON s.id=o.scope_id
                       WHERE o.created_at>=? AND o.actor_type='qq'
                       GROUP BY o.scope_id ORDER BY amount DESC,s.group_id LIMIT 10""",
                    (since,),
                ).fetchall()
                if scope_id is None
                else []
            )
        amounts = {str(row["result_code"]): int(row["amount"]) for row in results}
        by_date: Counter[str] = Counter()
        for row in hours:
            local = datetime.fromtimestamp(int(row["hour"]) * 3600, zone).date()
            by_date[local.isoformat()] += int(row["amount"])
        return {
            "days": days,
            "timezone": timezone,
            "totals": {
                "draws": sum(by_date.values()),
                "active_users": int(actors),
                "steal_success": amounts.get("STOLEN", 0),
                "steal_attempts": amounts.get("STOLEN", 0)
                + amounts.get("STEAL_FAILED", 0),
                "force_success": amounts.get("FORCED", 0),
                "force_attempts": amounts.get("FORCED", 0)
                + amounts.get("FORCE_FAILED", 0),
                "gifts": amounts.get("GIFTED", 0) + amounts.get("GIFT_ACCEPTED", 0),
                "divorces": amounts.get("DIVORCED", 0),
            },
            "trend": [
                {"date": item.isoformat(), "draws": by_date.get(item.isoformat(), 0)}
                for item in dates
            ],
            "characters": [
                {"name": str(row["name"]), "value": int(row["times"])}
                for row in characters
            ],
            "groups": [
                {"name": f"群 {row['group_id']}", "value": int(row["amount"])}
                for row in groups
            ],
        }

    def relationships(
        self,
        scope_id: int,
        *,
        period_id: str | None,
        mode: str | None,
        query: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where = ["r.scope_id=?"]
        args: list[Any] = [scope_id]
        if period_id:
            where.append("r.period_id=?")
            args.append(period_id)
        else:
            where.append("p.state='current'")
        if mode:
            if mode not in {"wife", "husband", "member"}:
                raise ValueError("mode 必须是 wife、husband 或 member。")
            where.append("r.mode=?")
            args.append(mode)
        term = query.strip()[:100]
        if term:
            where.append("(r.owner_id LIKE ? OR r.subject_id LIKE ? OR ss.name LIKE ?)")
            args.extend([f"%{term}%"] * 3)
        clause = " AND ".join(where)
        with self.storage.read() as db:
            total = int(
                db.execute(
                    f"SELECT COUNT(*) FROM relationships r JOIN periods p ON p.id=r.period_id AND p.scope_id=r.scope_id JOIN subject_snapshots ss ON ss.id=r.snapshot_id WHERE {clause}",
                    args,
                ).fetchone()[0]
            )
            rows = db.execute(
                f"""SELECT r.id,r.period_id,r.mode,r.owner_id,r.subject_kind,r.subject_id,r.slot_kind,
                           CASE WHEN r.special_source='admin' THEN '管理员设置' WHEN r.special_source='force' THEN '强娶' WHEN r.slot_kind='steal' THEN '抢夺' WHEN r.slot_kind='designated' THEN '指定' ELSE '普通' END AS slot_label,
                           r.state,r.acquired_at,r.ended_at,
                           r.end_reason,r.version,ss.name AS snapshot_name,ss.source_revision,p.starts_at,p.ends_at,
                           COALESCE((SELECT GROUP_CONCAT(si.media_hash,char(31)) FROM snapshot_images si WHERE si.snapshot_id=ss.id),'') AS image_hashes
                    FROM relationships r JOIN periods p ON p.id=r.period_id AND p.scope_id=r.scope_id
                    JOIN subject_snapshots ss ON ss.id=r.snapshot_id WHERE {clause}
                    ORDER BY r.acquired_at DESC,r.id DESC LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["image_hashes"] = (
                item["image_hashes"].split(chr(31)) if item["image_hashes"] else []
            )
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def quota(
        self, scope_id: int, mode: str, user_id: str, *, period_id: str | None
    ) -> dict[str, Any]:
        if (
            mode not in {"wife", "husband", "member"}
            or not user_id
            or len(user_id) > 128
        ):
            raise ValueError("mode 或 user_id 无效。")
        with self.storage._lock:
            db = self.storage._connection()
            period = self._period(db, scope_id, period_id)
            if period is None:
                raise ValueError("所选作用域没有当前周期或周期不存在。")
            period_id = str(period["id"])
            counter = db.execute(
                "SELECT normal_draws,designated_draws,steal_attempts,stolen_successes,divorces,last_steal_at FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, user_id),
            ).fetchone()
            active = int(
                db.execute(
                    "SELECT COUNT(*) FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=? AND slot_kind='normal' AND state='active'",
                    (scope_id, period_id, mode, user_id),
                ).fetchone()[0]
            )
            stolen = int(
                db.execute(
                    "SELECT COUNT(*) FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=? AND slot_kind='steal' AND state='active'",
                    (scope_id, period_id, mode, user_id),
                ).fetchone()[0]
            )
            designated = int(
                db.execute(
                    "SELECT COUNT(*) FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=? AND slot_kind='designated' AND state='active'",
                    (scope_id, period_id, mode, user_id),
                ).fetchone()[0]
            )
            sent = int(
                db.execute(
                    "SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND sender_id=? AND state='pending'",
                    (scope_id, period_id, user_id),
                ).fetchone()[0]
            )
            received = int(
                db.execute(
                    "SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND recipient_id=? AND state='pending'",
                    (scope_id, period_id, user_id),
                ).fetchone()[0]
            )
        config, revision = self.storage.get_settings(scope_id)
        settings = config["modes"][mode]
        counter_value = (
            dict(counter)
            if counter
            else {
                "normal_draws": 0,
                "designated_draws": 0,
                "steal_attempts": 0,
                "stolen_successes": 0,
                "divorces": 0,
                "last_steal_at": None,
            }
        )
        capacity = int(settings["capacity"])
        designated_limit = int(settings.get("designated_limit", 0))
        counter_value.update(
            {
                "active_relationships": active,
                "active_steal_relationships": stolen,
                "active_designated_relationships": designated,
                "designated_limit": designated_limit,
                "designated_quota_remaining": max(
                    0, designated_limit - int(counter_value["designated_draws"])
                ),
                "capacity": capacity,
                "normal_quota_remaining": max(
                    0, capacity - int(counter_value["normal_draws"])
                ),
                "pending_invites_sent": sent,
                "pending_invites_received": received,
            }
        )
        return {
            "scope_id": scope_id,
            "period": dict(period),
            "mode": mode,
            "user_id": user_id,
            "data": counter_value,
            "config_revision": revision,
        }

    def intimacy(
        self, scope_id: int, *, source_id: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        with self.storage.read() as db:
            if source_id:
                total = int(
                    db.execute(
                        "SELECT COUNT(*) FROM intimacy_edges WHERE scope_id=? AND source_id=?",
                        (scope_id, source_id),
                    ).fetchone()[0]
                )
                rows = db.execute(
                    "SELECT source_id,target_id,score,updated_at FROM intimacy_edges WHERE scope_id=? AND source_id=? ORDER BY score DESC,target_id LIMIT ? OFFSET ?",
                    (scope_id, source_id, limit, offset),
                ).fetchall()
            else:
                total = int(
                    db.execute(
                        "SELECT COUNT(*) FROM intimacy_edges WHERE scope_id=?",
                        (scope_id,),
                    ).fetchone()[0]
                )
                rows = db.execute(
                    "SELECT source_id,target_id,score,updated_at FROM intimacy_edges WHERE scope_id=? ORDER BY score DESC,source_id,target_id LIMIT ? OFFSET ?",
                    (scope_id, limit, offset),
                ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "directed": True,
        }

    def activity(
        self,
        scope_id: int,
        *,
        user_id: str | None,
        window_days: int,
        now: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if not 1 <= window_days <= 365:
            raise ValueError("window_days 必须在 1 到 365 之间。")
        start = now - window_days * 86400
        config, _revision = self.storage.get_settings(scope_id)
        ranked = StatisticsService(self.storage).activity_rows(
            scope_id,
            now=now,
            window_days=window_days,
            timezone=config["reset"]["timezone"],
        )
        if user_id:
            ranked = [row for row in ranked if row["user_id"] == user_id]
        with self.storage.read() as db:
            args: list[Any] = [scope_id, start, now]
            user_clause = ""
            if user_id:
                user_clause = " AND user_id=?"
                args.append(user_id)
            rows = db.execute(
                f"""SELECT user_id,MIN(observed_second) AS first_observed,MAX(observed_second) AS last_observed
                    FROM activity_seconds WHERE scope_id=? AND observed_second>? AND observed_second<=?{user_clause}
                    GROUP BY user_id""",
                args,
            ).fetchall()
            coverage = db.execute(
                f"SELECT MIN(observed_second),MAX(observed_second),SUM(message_count) FROM activity_seconds WHERE scope_id=? AND observed_second>? AND observed_second<=?{user_clause}",
                args,
            ).fetchone()
        bounds = {row["user_id"]: row for row in rows}
        items = []
        for row in ranked[offset : offset + limit]:
            observed = bounds.get(row["user_id"])
            items.append(
                {
                    **row,
                    "first_observed": observed["first_observed"] if observed else None,
                    "last_observed": observed["last_observed"] if observed else None,
                }
            )
        return {
            "items": items,
            "total": len(ranked),
            "limit": limit,
            "offset": offset,
            "window_days": window_days,
            "window_start": start,
            "window_end": now,
            "coverage": {
                "first_observed": coverage[0],
                "last_observed": coverage[1],
                "messages": int(coverage[2] or 0),
            },
        }

    def invites(
        self, scope_id: int, *, state: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        where = ["i.scope_id=?"]
        args: list[Any] = [scope_id]
        if state:
            allowed = {
                "pending",
                "accepted",
                "rejected",
                "cancelled",
                "expired",
                "invalidated",
            }
            if state not in allowed:
                raise ValueError("邀请状态无效。")
            where.append("i.state=?")
            args.append(state)
        clause = " AND ".join(where)
        with self.storage.read() as db:
            total = int(
                db.execute(
                    f"SELECT COUNT(*) FROM gift_invites i WHERE {clause}", args
                ).fetchone()[0]
            )
            rows = db.execute(
                f"""SELECT i.id,i.period_id,i.mode,i.relationship_id,i.sender_id,i.recipient_id,i.state,i.created_at,i.expires_at,
                           i.finalized_at,i.reason,i.result_relationship_id,i.relationship_version,ss.name AS snapshot_name
                    FROM gift_invites i JOIN relationships r ON r.id=i.relationship_id JOIN subject_snapshots ss ON ss.id=r.snapshot_id
                    WHERE {clause} ORDER BY i.created_at DESC,i.id DESC LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def history(
        self,
        scope_id: int,
        *,
        since: int | None,
        until: int | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where = ["scope_id=?"]
        args: list[Any] = [scope_id]
        if since is not None:
            where.append("created_at>=?")
            args.append(since)
        if until is not None:
            where.append("created_at<=?")
            args.append(until)
        clause = " AND ".join(where)
        with self.storage.read() as db:
            total = int(
                db.execute(
                    f"SELECT COUNT(*) FROM operation_log WHERE {clause}", args
                ).fetchone()[0]
            )
            rows = db.execute(
                f"SELECT * FROM operation_log WHERE {clause} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json"))
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _period(db: Any, scope_id: int, period_id: str | None):
        if period_id:
            return db.execute(
                "SELECT id,sequence,starts_at,ends_at,timezone,reset_time,state FROM periods WHERE scope_id=? AND id=?",
                (scope_id, period_id),
            ).fetchone()
        return db.execute(
            "SELECT id,sequence,starts_at,ends_at,timezone,reset_time,state FROM periods WHERE scope_id=? AND state='current'",
            (scope_id,),
        ).fetchone()
