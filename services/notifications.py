"""邀请终态通知的持久化 outbox 与发送结果跟踪。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from .message_templates import render_message


class NotificationService:
    """以数据库终态为准发送通知；不因网络错误重做关系事务。"""

    def __init__(self, context: Any, storage: Any) -> None:
        self.context = context
        self.storage = storage

    async def invalidate_pending_on_restart(self, now: int) -> int:
        return await asyncio.to_thread(self._invalidate_pending_on_restart, now)

    def _invalidate_pending_on_restart(self, now: int) -> int:
        with self.storage.transaction() as db:
            groups = db.execute(
                "SELECT scope_id,umo,COUNT(*) AS amount FROM gift_invites i JOIN scopes s ON s.id=i.scope_id WHERE i.state='pending' GROUP BY scope_id,umo"
            ).fetchall()
            db.execute(
                "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason='restart' WHERE state='pending'",
                (now,),
            )
            for row in groups:
                scope_id = int(row["scope_id"])
                config = self.storage.get_settings(scope_id)[0]
                self._queue(
                    db,
                    scope_id,
                    str(row["umo"]),
                    f"restart:{row['scope_id']}:{uuid.uuid4().hex}",
                    render_message(config, "notifications", "restart_invalidated", amount=int(row["amount"])),
                    now,
                )
            db.execute(
                "UPDATE notification_outbox SET state='unknown',error_summary='发送中断于插件重载' WHERE state='sending'"
            )
        return sum(int(row["amount"]) for row in groups)

    async def expire_due(self, now: int) -> int:
        return await asyncio.to_thread(self._expire_due, now)

    def _expire_due(self, now: int) -> int:
        with self.storage.transaction() as db:
            rows = db.execute(
                """SELECT i.scope_id,s.umo,i.id FROM gift_invites i JOIN scopes s ON s.id=i.scope_id
                   WHERE i.state='pending' AND i.expires_at<=? ORDER BY i.scope_id,i.id""",
                (now,),
            ).fetchall()
            if not rows:
                return 0
            updated = db.execute(
                "UPDATE gift_invites SET state='expired',finalized_at=?,reason='timeout' WHERE state='pending' AND expires_at<=?",
                (now, now),
            ).rowcount
            by_scope: dict[tuple[int, str], list[str]] = {}
            for row in rows:
                by_scope.setdefault((int(row["scope_id"]), str(row["umo"])), []).append(str(row["id"]))
            for (scope_id, umo), invite_ids in by_scope.items():
                config = self.storage.get_settings(scope_id)[0]
                self._queue(
                    db,
                    scope_id,
                    umo,
                    f"expired:{scope_id}:{uuid.uuid4().hex}",
                    render_message(config, "notifications", "invite_expired", invite_ids="、".join(invite_ids)),
                    now,
                )
            return int(updated)

    async def send_pending(self, limit: int = 50) -> int:
        items = await asyncio.to_thread(self._claim, limit, int(time.time()))
        sent = 0
        for item in items:
            ok = False
            error = None
            try:
                from astrbot.api.event import MessageChain

                ok = bool(await self.context.send_message(item["umo"], MessageChain().message(item["text"])))
            except Exception as exc:
                error = type(exc).__name__
            await asyncio.to_thread(self._finish, item["id"], ok, error, int(time.time()))
            sent += int(ok)
        return sent

    def _claim(self, limit: int, now: int) -> list[dict[str, str]]:
        result = []
        with self.storage.transaction() as db:
            rows = db.execute(
                "SELECT id,umo,payload_json FROM notification_outbox WHERE state='pending' ORDER BY created_at,id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    text = payload.get("text") if isinstance(payload, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("notification payload has no text")
                except (json.JSONDecodeError, TypeError, ValueError):
                    db.execute(
                        "UPDATE notification_outbox SET state='failed',attempted_at=?,error_summary='invalid_payload' WHERE id=? AND state='pending'",
                        (now, row["id"]),
                    )
                    continue
                claimed = db.execute(
                    "UPDATE notification_outbox SET state='sending',attempted_at=? WHERE id=? AND state='pending'",
                    (now, row["id"]),
                )
                if claimed.rowcount == 1:
                    result.append({"id": str(row["id"]), "umo": str(row["umo"]), "text": text})
        return result

    def _finish(self, item_id: str, ok: bool, error: str | None, now: int) -> None:
        with self.storage.transaction() as db:
            db.execute(
                "UPDATE notification_outbox SET state=?,sent_at=?,error_summary=? WHERE id=? AND state='sending'",
                ("sent" if ok else "failed", now if ok else None, error, item_id),
            )

    @staticmethod
    def _queue(db: Any, scope_id: int, umo: str, dedupe_key: str, text: str, now: int) -> None:
        if not text.strip():
            return
        db.execute(
            """INSERT OR IGNORE INTO notification_outbox(id,scope_id,dedupe_key,umo,payload_json,state,created_at)
               VALUES(?,?,?,?,?,'pending',?)""",
            (str(uuid.uuid4()), scope_id, dedupe_key, umo, json.dumps({"text": text}, ensure_ascii=False), now),
        )
