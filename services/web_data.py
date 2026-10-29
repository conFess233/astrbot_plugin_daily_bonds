"""概览、群数据查询与管理员纠错的管理 API。"""

from __future__ import annotations

import asyncio
import time
import uuid

from astrbot.api.web import json_response, request

from ..models.errors import StorageError


class DataRoutes:
    """按群查询关系、配额、统计与操作记录。"""

    async def overview(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._query_scope_id)
        except ValueError as exc:
            return self._error("INVALID_SCOPE", str(exc), 400)
        return await asyncio.to_thread(self._overview, scope_id)

    def _overview(self, scope_id: int | None):
        with self.storage.read() as db:
            scope_where = " WHERE scope_id=?" if scope_id is not None else ""
            scope_args = (scope_id,) if scope_id is not None else ()
            scopes = (
                1
                if scope_id is not None
                else int(db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0])
            )
            active = int(
                db.execute(
                    f"SELECT COUNT(*) FROM relationships{scope_where}{' AND' if scope_where else ' WHERE'} state='active'",
                    scope_args,
                ).fetchone()[0]
            )
            pending = int(
                db.execute(
                    f"SELECT COUNT(*) FROM gift_invites{scope_where}{' AND' if scope_where else ' WHERE'} state='pending'",
                    scope_args,
                ).fetchone()[0]
            )
            current_periods = int(
                db.execute(
                    f"SELECT COUNT(*) FROM periods{scope_where}{' AND' if scope_where else ' WHERE'} state='current'",
                    scope_args,
                ).fetchone()[0]
            )
            period = (
                db.execute(
                    "SELECT id,sequence,starts_at,ends_at,timezone,reset_time FROM periods WHERE scope_id=? AND state='current'",
                    (scope_id,),
                ).fetchone()
                if scope_id is not None
                else None
            )
            settings_rows = db.execute(
                "SELECT scope_key,revision FROM settings ORDER BY scope_key"
            ).fetchall()
            failed_notifications = int(
                db.execute(
                    f"SELECT COUNT(*) FROM notification_outbox{scope_where}{' AND' if scope_where else ' WHERE'} state IN ('failed','unknown')",
                    scope_args,
                ).fetchone()[0]
            )
            failed_jobs = int(
                db.execute(
                    "SELECT COUNT(*) FROM maintenance_jobs WHERE state IN ('failed','unknown')"
                ).fetchone()[0]
            )
        global_revision = next(
            (
                int(row["revision"])
                for row in settings_rows
                if row["scope_key"] == "global"
            ),
            0,
        )
        catalog = self.catalog.catalog_summary()
        scope_revision, enabled_pool_count = None, None
        if scope_id is not None:
            effective, scope_revision = self.storage.get_settings(scope_id)
            enabled_pool_count = len(
                set(
                    effective["modes"]["wife"]["pool_ids"]
                    + effective["modes"]["husband"]["pool_ids"]
                )
            )
        return json_response(
            {
                "ok": True,
                "data": {
                    "scope_id": scope_id,
                    "scopes": scopes,
                    "active_relationships": active,
                    "pending_invites": pending,
                    "current_periods": current_periods,
                    "current_period": dict(period) if period else None,
                    "global_revision": global_revision,
                    "settings_scopes": len(settings_rows),
                    "scope_revision": scope_revision,
                    "enabled_pool_count": enabled_pool_count,
                    "failed_notifications": failed_notifications,
                    "failed_jobs": failed_jobs,
                    "catalog": catalog,
                },
                "request_id": str(uuid.uuid4()),
            }
        )

    async def scopes(self):
        _, denied = self._authorize()
        if denied:
            return denied
        return await asyncio.to_thread(self._scopes)

    def _scopes(self):
        with self.storage.read() as db:
            rows = db.execute(
                "SELECT id,platform_id,self_id,group_id,created_at FROM scopes ORDER BY platform_id,self_id,group_id LIMIT 500"
            ).fetchall()
        return json_response(
            {
                "ok": True,
                "data": [dict(row) for row in rows],
                "request_id": str(uuid.uuid4()),
            }
        )

    async def relationships(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            period_id = self._optional_query_text("period_id", 128)
            mode = self._optional_query_text("mode", 16)
            data = await asyncio.to_thread(
                self.queries.relationships,
                scope_id,
                period_id=period_id,
                mode=mode,
                query=str(request.query.get("q") or ""),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def user_quota(self, user_id: str):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            mode = self._optional_query_text("mode", 16) or "wife"
            period_id = self._optional_query_text("period_id", 128)
            data = await asyncio.to_thread(
                self.queries.quota, scope_id, mode, user_id, period_id=period_id
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def intimacy(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            data = await asyncio.to_thread(
                self.queries.intimacy,
                scope_id,
                source_id=self._optional_query_text("source_id", 128),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def activity(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._required_scope_id)
            data = await asyncio.to_thread(
                self.queries.activity,
                scope_id,
                user_id=self._optional_query_text("user_id", 128),
                window_days=self._query_integer("window_days", 7, 1, 365),
                now=int(time.time()),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def invites(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(
                self.queries.invites,
                await asyncio.to_thread(self._required_scope_id),
                state=self._optional_query_text("state", 16),
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def history(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            since = (
                self._query_integer("since", 0, 0, 9_999_999_999)
                if request.query.get("since") not in (None, "")
                else None
            )
            until = (
                self._query_integer("until", 0, 0, 9_999_999_999)
                if request.query.get("until") not in (None, "")
                else None
            )
            if since is not None and until is not None and until < since:
                raise ValueError("until 不得早于 since。")
            data = await asyncio.to_thread(
                self.queries.history,
                await asyncio.to_thread(self._required_scope_id),
                since=since,
                until=until,
                limit=self._query_integer("limit", 50, 1, 100),
                offset=self._query_integer("offset", 0, 0, 1_000_000),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, StorageError) as exc:
            return self._error("INVALID_QUERY", str(exc), 400)

    async def admin_preview(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            scope_id = await asyncio.to_thread(self._requested_scope_id, payload)
            if scope_id is None:
                raise ValueError("纠错操作必须指定群作用域。")
            normalized = dict(payload)
            normalized["scope_id"] = scope_id
            data = await asyncio.to_thread(
                self.corrections.preview,
                normalized,
                actor=username,
                now=int(time.time()),
            )
            return json_response(
                {"ok": True, "data": data, "request_id": str(uuid.uuid4())}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CORRECTION_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "纠错预检失败。", 500)

    async def admin_commit(self):
        username, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            data = await asyncio.to_thread(
                self.corrections.commit, payload, actor=username, now=int(time.time())
            )
            return json_response(
                {"ok": True, "data": data, "request_id": payload.get("request_id")}
            )
        except (ValueError, TypeError) as exc:
            return self._error("CORRECTION_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if any(
                fragment in message
                for fragment in ("已变化", "版本", "request_id", "预检")
            ):
                return self._error("CORRECTION_CONFLICT", message, 409)
            return self._error("INTERNAL_ERROR", "纠错提交失败。", 500)
