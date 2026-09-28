"""Read-only, scope-bound queries for the authenticated management page."""

from __future__ import annotations

import json
from typing import Any

from ..models import StorageError
from .storage import SQLiteStorage


class AdminQueryService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def relationships(self, scope_id: int, *, period_id: str | None, mode: str | None, query: str, limit: int, offset: int) -> dict[str, Any]:
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
        with self.storage._lock:
            db = self.storage._connection()
            total = int(db.execute(
                f"SELECT COUNT(*) FROM relationships r JOIN periods p ON p.id=r.period_id AND p.scope_id=r.scope_id JOIN subject_snapshots ss ON ss.id=r.snapshot_id WHERE {clause}", args
            ).fetchone()[0])
            rows = db.execute(
                f"""SELECT r.id,r.period_id,r.mode,r.owner_id,r.subject_kind,r.subject_id,r.state,r.acquired_at,r.ended_at,
                           r.end_reason,r.version,ss.name AS snapshot_name,ss.source_revision,p.starts_at,p.ends_at,
                           COALESCE((SELECT GROUP_CONCAT(si.media_hash,char(31)) FROM snapshot_images si WHERE si.snapshot_id=ss.id),'') AS image_hashes
                    FROM relationships r JOIN periods p ON p.id=r.period_id AND p.scope_id=r.scope_id
                    JOIN subject_snapshots ss ON ss.id=r.snapshot_id WHERE {clause}
                    ORDER BY r.acquired_at DESC,r.id DESC LIMIT ? OFFSET ?""", (*args, limit, offset)
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["image_hashes"] = item["image_hashes"].split(chr(31)) if item["image_hashes"] else []
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def quota(self, scope_id: int, mode: str, user_id: str, *, period_id: str | None) -> dict[str, Any]:
        if mode not in {"wife", "husband", "member"} or not user_id or len(user_id) > 128:
            raise ValueError("mode 或 user_id 无效。")
        with self.storage._lock:
            db = self.storage._connection()
            period = self._period(db, scope_id, period_id)
            if period is None:
                raise ValueError("所选作用域没有当前周期或周期不存在。")
            period_id = str(period["id"])
            counter = db.execute(
                "SELECT normal_draws,steal_attempts,stolen_successes,divorces,last_steal_at FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                (scope_id, period_id, mode, user_id),
            ).fetchone()
            active = int(db.execute(
                "SELECT COUNT(*) FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=? AND state='active'",
                (scope_id, period_id, mode, user_id),
            ).fetchone()[0])
            credits = db.execute(
                "SELECT reason,state,COUNT(*) AS amount FROM redraw_credits WHERE scope_id=? AND period_id=? AND mode=? AND user_id=? GROUP BY reason,state",
                (scope_id, period_id, mode, user_id),
            ).fetchall()
            sent = int(db.execute("SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND sender_id=? AND state='pending'", (scope_id, period_id, user_id)).fetchone()[0])
            received = int(db.execute("SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND recipient_id=? AND state='pending'", (scope_id, period_id, user_id)).fetchone()[0])
        config, revision = self.storage.get_settings(scope_id)
        settings = config["modes"][mode]
        counter_value = dict(counter) if counter else {"normal_draws": 0, "steal_attempts": 0, "stolen_successes": 0, "divorces": 0, "last_steal_at": None}
        credit_rows = [dict(row) for row in credits]
        available = sum(int(row["amount"]) for row in credit_rows if row["state"] == "available")
        capacity = int(settings["capacity"])
        counter_value.update({"active_relationships": active, "capacity": capacity, "normal_quota_remaining": max(0, capacity - int(counter_value["normal_draws"])),
                              "available_redraw_credits": available, "redraw_credits": credit_rows,
                              "pending_invites_sent": sent, "pending_invites_received": received})
        return {"scope_id": scope_id, "period": dict(period), "mode": mode, "user_id": user_id, "data": counter_value, "config_revision": revision}

    def intimacy(self, scope_id: int, *, source_id: str | None, limit: int, offset: int) -> dict[str, Any]:
        with self.storage._lock:
            db = self.storage._connection()
            if source_id:
                total = int(db.execute("SELECT COUNT(*) FROM intimacy_edges WHERE scope_id=? AND source_id=?", (scope_id, source_id)).fetchone()[0])
                rows = db.execute("SELECT source_id,target_id,score,updated_at FROM intimacy_edges WHERE scope_id=? AND source_id=? ORDER BY score DESC,target_id LIMIT ? OFFSET ?", (scope_id, source_id, limit, offset)).fetchall()
            else:
                total = int(db.execute("SELECT COUNT(*) FROM intimacy_edges WHERE scope_id=?", (scope_id,)).fetchone()[0])
                rows = db.execute("SELECT source_id,target_id,score,updated_at FROM intimacy_edges WHERE scope_id=? ORDER BY score DESC,source_id,target_id LIMIT ? OFFSET ?", (scope_id, limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset, "directed": True}

    def activity(self, scope_id: int, *, user_id: str | None, window_days: int, now: int, limit: int, offset: int) -> dict[str, Any]:
        if not 1 <= window_days <= 365:
            raise ValueError("window_days 必须在 1 到 365 之间。")
        start = now - window_days * 86400
        with self.storage._lock:
            db = self.storage._connection()
            args: list[Any] = [scope_id, start, now]
            user_clause = ""
            if user_id:
                user_clause = " AND user_id=?"
                args.append(user_id)
            total = int(db.execute(f"SELECT COUNT(DISTINCT user_id) FROM activity_seconds WHERE scope_id=? AND observed_second>? AND observed_second<=?{user_clause}", args).fetchone()[0])
            rows = db.execute(
                f"""SELECT user_id,SUM(message_count) AS messages,MIN(observed_second) AS first_observed,MAX(observed_second) AS last_observed
                    FROM activity_seconds WHERE scope_id=? AND observed_second>? AND observed_second<=?{user_clause}
                    GROUP BY user_id ORDER BY messages DESC,user_id LIMIT ? OFFSET ?""", (*args, limit, offset)
            ).fetchall()
            coverage = db.execute(f"SELECT MIN(observed_second),MAX(observed_second),SUM(message_count) FROM activity_seconds WHERE scope_id=? AND observed_second>? AND observed_second<=?{user_clause}", args).fetchone()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset,
                "window_days": window_days, "window_start": start, "window_end": now,
                "coverage": {"first_observed": coverage[0], "last_observed": coverage[1], "messages": int(coverage[2] or 0)}}

    def invites(self, scope_id: int, *, state: str | None, limit: int, offset: int) -> dict[str, Any]:
        where = ["i.scope_id=?"]
        args: list[Any] = [scope_id]
        if state:
            allowed = {"pending", "accepted", "rejected", "cancelled", "expired", "invalidated"}
            if state not in allowed:
                raise ValueError("邀请状态无效。")
            where.append("i.state=?")
            args.append(state)
        clause = " AND ".join(where)
        with self.storage._lock:
            db = self.storage._connection()
            total = int(db.execute(f"SELECT COUNT(*) FROM gift_invites i WHERE {clause}", args).fetchone()[0])
            rows = db.execute(
                f"""SELECT i.id,i.period_id,i.mode,i.relationship_id,i.sender_id,i.recipient_id,i.state,i.created_at,i.expires_at,
                           i.finalized_at,i.reason,i.result_relationship_id,i.relationship_version,ss.name AS snapshot_name
                    FROM gift_invites i JOIN relationships r ON r.id=i.relationship_id JOIN subject_snapshots ss ON ss.id=r.snapshot_id
                    WHERE {clause} ORDER BY i.created_at DESC,i.id DESC LIMIT ? OFFSET ?""", (*args, limit, offset)
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def history(self, scope_id: int, *, since: int | None, until: int | None, limit: int, offset: int) -> dict[str, Any]:
        where = ["scope_id=?"]
        args: list[Any] = [scope_id]
        if since is not None:
            where.append("created_at>=?")
            args.append(since)
        if until is not None:
            where.append("created_at<=?")
            args.append(until)
        clause = " AND ".join(where)
        with self.storage._lock:
            db = self.storage._connection()
            total = int(db.execute(f"SELECT COUNT(*) FROM operation_log WHERE {clause}", args).fetchone()[0])
            rows = db.execute(f"SELECT * FROM operation_log WHERE {clause} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json"))
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _period(db: Any, scope_id: int, period_id: str | None):
        if period_id:
            return db.execute("SELECT id,sequence,starts_at,ends_at,timezone,reset_time,state FROM periods WHERE scope_id=? AND id=?", (scope_id, period_id)).fetchone()
        return db.execute("SELECT id,sequence,starts_at,ends_at,timezone,reset_time,state FROM periods WHERE scope_id=? AND state='current'", (scope_id,)).fetchone()
