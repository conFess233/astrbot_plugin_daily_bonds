"""Audited preview/commit flows for explicit administrative corrections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Mapping

from ..models import StorageError
from .storage import SQLiteStorage


_COUNTERS = {"normal_draws", "steal_attempts", "stolen_successes", "divorces"}
_MODES = {"wife", "husband", "member"}


class AdminCorrectionService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def preview(self, payload: Mapping[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        action = payload.get("action")
        if action not in {"relationship_end", "credit_grant", "intimacy_set", "activity_set", "counter_reset"}:
            raise ValueError("纠错操作类型无效。")
        scope_id = payload.get("scope_id")
        if isinstance(scope_id, bool) or not isinstance(scope_id, int) or scope_id < 1:
            raise ValueError("scope_id 必须是插件内的整数作用域。")
        reason = self._text(payload.get("reason"), "纠错理由", 500)
        spec = self._normalize_spec(action, payload)
        config_revision = self._config_revision(scope_id)
        with self.storage.transaction() as db:
            if db.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone() is None:
                raise ValueError("scope_id 不属于当前插件实例。")
            target, before, after = self._capture(db, scope_id, action, spec, reason, now)
            preflight_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO web_admin_action_preflights(id,actor_username,scope_id,action,target_key,reason,spec_json,before_json,after_json,config_revision,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (preflight_id, actor, scope_id, action, target, reason, json.dumps(spec, ensure_ascii=False),
                 json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True),
                 config_revision, now, now + 600),
            )
        return {"preflight_id": preflight_id, "action": action, "scope_id": scope_id, "target": target,
                "reason": reason, "before": before, "after": after, "config_revision": config_revision, "expires_at": now + 600}

    def commit(self, payload: Mapping[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        preflight_id = self._text(payload.get("preflight_id"), "preflight_id", 64)
        request_id = self._text(payload.get("request_id"), "request_id", 128)
        expected_revision = self._text(payload.get("expected_revision"), "expected_revision", 128)
        request_body = {"preflight_id": preflight_id, "expected_revision": expected_revision}
        request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.storage.transaction() as db:
            prior = db.execute("SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint='admin/commit' AND request_id=?", (actor, request_id)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise StorageError("request_id 已用于不同纠错提交。")
                return json.loads(prior["response_json"])
            row = db.execute("SELECT * FROM web_admin_action_preflights WHERE id=? AND actor_username=?", (preflight_id, actor)).fetchone()
            if row is None or int(row["expires_at"]) < now or row["consumed_at"] is not None:
                raise StorageError("纠错预检不存在、已过期或已提交，请重新预检。")
            config_revision = self._config_revision(int(row["scope_id"]))
            if expected_revision != config_revision or expected_revision != row["config_revision"]:
                raise StorageError("配置版本已变化，请重新预检。")
            spec = json.loads(row["spec_json"])
            target, before, after = self._capture(db, int(row["scope_id"]), row["action"], spec, row["reason"], now)
            if target != row["target_key"] or before != json.loads(row["before_json"]):
                raise StorageError("目标数据已变化，请重新预检。")
            self._apply(db, row["action"], int(row["scope_id"]), spec, after, actor, request_id, config_revision, now)
            db.execute("UPDATE web_admin_action_preflights SET consumed_at=? WHERE id=?", (now, preflight_id))
            response = {"applied": True, "action": row["action"], "scope_id": int(row["scope_id"]), "target": target,
                        "before": before, "after": after, "config_revision": config_revision}
            details = json.dumps({"before": before, "after": after}, ensure_ascii=False)
            db.execute("INSERT INTO web_admin_audit(id,actor_username,action,entity_kind,entity_id,request_id,reason,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), actor, row["action"], "correction", target, request_id, row["reason"], details, now))
            period = db.execute("SELECT id FROM periods WHERE scope_id=? AND state='current'", (row["scope_id"],)).fetchone()
            db.execute("INSERT INTO operation_log(id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), row["scope_id"], period["id"] if period else None, spec.get("mode"), row["action"], actor, "dashboard", "ADMIN_APPLIED", config_revision, details, row["reason"], now))
            db.execute("INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                       (actor, "admin/commit", request_id, request_hash, json.dumps(response, ensure_ascii=False), now))
            return response

    def _normalize_spec(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action == "relationship_end":
            relationship_id = self._text(payload.get("relationship_id"), "relationship_id", 128)
            compensation = payload.get("compensation", 0)
            if isinstance(compensation, bool) or not isinstance(compensation, int) or not 0 <= compensation <= 10:
                raise ValueError("compensation 必须在 0 到 10 之间。")
            return {"relationship_id": relationship_id, "compensation": compensation}
        if action == "credit_grant":
            return {"user_id": self._text(payload.get("user_id"), "user_id", 128),
                    "mode": self._mode(payload.get("mode")), "quantity": self._integer(payload.get("quantity"), "quantity", 1, 10)}
        if action == "intimacy_set":
            source = self._text(payload.get("source_id"), "source_id", 128)
            target = self._text(payload.get("target_id"), "target_id", 128)
            if source == target:
                raise ValueError("亲密度源与目标不能相同。")
            return {"source_id": source, "target_id": target, "score": self._integer(payload.get("score"), "score", 0, 2_000_000_000)}
        if action == "activity_set":
            return {"user_id": self._text(payload.get("user_id"), "user_id", 128),
                    "observed_second": self._integer(payload.get("observed_second"), "observed_second", 0, 9_999_999_999),
                    "message_count": self._integer(payload.get("message_count"), "message_count", 0, 2_000_000_000)}
        mode = self._mode(payload.get("mode"))
        user_id = self._text(payload.get("user_id"), "user_id", 128)
        fields = payload.get("fields")
        if not isinstance(fields, list) or not fields or any(field not in _COUNTERS for field in fields) or len(set(fields)) != len(fields):
            raise ValueError("fields 必须选择至少一项可重置计数。")
        return {"mode": mode, "user_id": user_id, "fields": sorted(fields)}

    def _capture(self, db: sqlite3.Connection, scope_id: int, action: str, spec: dict[str, Any], reason: str, now: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if action == "relationship_end":
            row = db.execute("SELECT id,period_id,mode,owner_id,state,version,ended_at,end_reason FROM relationships WHERE id=? AND scope_id=?", (spec["relationship_id"], scope_id)).fetchone()
            if row is None or row["state"] != "active":
                raise ValueError("关系不存在或已结束。")
            before = dict(row)
            pending_invites = int(db.execute("SELECT COUNT(*) FROM gift_invites WHERE relationship_id=? AND state='pending'", (spec["relationship_id"],)).fetchone()[0])
            before["pending_invites"] = pending_invites
            after = {"state": "ended", "ended_at": now, "end_reason": reason, "compensation": spec["compensation"], "invalidated_invites": pending_invites}
            return str(row["id"]), before, after
        if action == "credit_grant":
            period = self._current_period(db, scope_id)
            target = f"{period}:{spec['mode']}:{spec['user_id']}"
            before = {"available_credits": self._credit_count(db, scope_id, period, spec["mode"], spec["user_id"])}
            return target, before, {"period_id": period, "quantity": spec["quantity"], "available_credits": before["available_credits"] + spec["quantity"]}
        if action == "intimacy_set":
            row = db.execute("SELECT score,updated_at FROM intimacy_edges WHERE scope_id=? AND source_id=? AND target_id=?", (scope_id, spec["source_id"], spec["target_id"])).fetchone()
            target = f"{spec['source_id']}->{spec['target_id']}"
            before = {"score": int(row["score"]) if row else 0, "exists": row is not None}
            return target, before, {"score": spec["score"], "exists": True}
        if action == "activity_set":
            row = db.execute("SELECT message_count FROM activity_seconds WHERE scope_id=? AND user_id=? AND observed_second=?", (scope_id, spec["user_id"], spec["observed_second"])).fetchone()
            target = f"{spec['user_id']}@{spec['observed_second']}"
            before = {"message_count": int(row["message_count"]) if row else 0, "exists": row is not None}
            return target, before, {"message_count": spec["message_count"], "exists": True}
        period = self._current_period(db, scope_id)
        row = db.execute("SELECT normal_draws,steal_attempts,stolen_successes,divorces FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?", (scope_id, period, spec["mode"], spec["user_id"])).fetchone()
        before = {"exists": row is not None, **{field: int(row[field]) if row else 0 for field in spec["fields"]}}
        return f"{period}:{spec['mode']}:{spec['user_id']}", before, {"exists": True, "period_id": period, **{field: 0 for field in spec["fields"]}}

    def _apply(self, db: sqlite3.Connection, action: str, scope_id: int, spec: dict[str, Any], after: dict[str, Any], actor: str, request_id: str, revision: str, now: int) -> None:
        operation_id = str(uuid.uuid4())
        if action == "relationship_end":
            db.execute("UPDATE relationships SET state='ended',ended_at=?,end_reason=?,version=version+1 WHERE id=? AND scope_id=? AND state='active'", (now, "admin:" + str(after["end_reason"]), spec["relationship_id"], scope_id))
            db.execute("UPDATE gift_invites SET state='invalidated',finalized_at=?,reason='relationship-ended' WHERE relationship_id=? AND state='pending'", (now, spec["relationship_id"]))
            if spec["compensation"]:
                row = db.execute("SELECT period_id,mode,owner_id FROM relationships WHERE id=?", (spec["relationship_id"],)).fetchone()
                self._issue_credits(db, scope_id, row["period_id"], row["mode"], row["owner_id"], spec["compensation"], operation_id, now)
        elif action == "credit_grant":
            self._issue_credits(db, scope_id, after["period_id"], spec["mode"], spec["user_id"], spec["quantity"], operation_id, now)
        elif action == "intimacy_set":
            db.execute("INSERT INTO intimacy_edges(scope_id,source_id,target_id,score,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(scope_id,source_id,target_id) DO UPDATE SET score=excluded.score,updated_at=excluded.updated_at",
                       (scope_id, spec["source_id"], spec["target_id"], spec["score"], now))
        elif action == "activity_set":
            db.execute("INSERT INTO activity_seconds(scope_id,user_id,observed_second,message_count) VALUES(?,?,?,?) ON CONFLICT(scope_id,user_id,observed_second) DO UPDATE SET message_count=excluded.message_count",
                       (scope_id, spec["user_id"], spec["observed_second"], spec["message_count"]))
        else:
            period = after["period_id"] if "period_id" in after else self._current_period(db, scope_id)
            row = db.execute("SELECT 1 FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?", (scope_id, period, spec["mode"], spec["user_id"])).fetchone()
            if row is None:
                db.execute("INSERT INTO daily_counters(scope_id,period_id,mode,user_id) VALUES(?,?,?,?)", (scope_id, period, spec["mode"], spec["user_id"]))
            for field in spec["fields"]:
                db.execute(f"UPDATE daily_counters SET {field}=0 WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?", (scope_id, period, spec["mode"], spec["user_id"]))

    @staticmethod
    def _issue_credits(db: sqlite3.Connection, scope_id: int, period_id: str, mode: str, user_id: str, quantity: int, operation_id: str, now: int) -> None:
        for index in range(quantity):
            db.execute("INSERT INTO redraw_credits(id,scope_id,period_id,mode,user_id,source_operation_id,reason,state,issued_at) VALUES(?,?,?,?,?,?,'admin_grant','available',?)",
                       (str(uuid.uuid4()), scope_id, period_id, mode, user_id, f"{operation_id}:{index}", now))

    def _config_revision(self, scope_id: int) -> str:
        _global, global_revision = self.storage.get_settings()
        _scope, scope_revision = self.storage.get_settings(scope_id)
        return f"global:{global_revision};scope:{scope_revision}"

    @staticmethod
    def _current_period(db: sqlite3.Connection, scope_id: int) -> str:
        row = db.execute("SELECT id FROM periods WHERE scope_id=? AND state='current'", (scope_id,)).fetchone()
        if row is None:
            raise ValueError("该作用域没有当前周期。")
        return str(row["id"])

    @staticmethod
    def _credit_count(db: sqlite3.Connection, scope_id: int, period_id: str, mode: str, user_id: str) -> int:
        return int(db.execute("SELECT COUNT(*) FROM redraw_credits WHERE scope_id=? AND period_id=? AND mode=? AND user_id=? AND state='available'", (scope_id, period_id, mode, user_id)).fetchone()[0])

    @staticmethod
    def _mode(value: Any) -> str:
        if value not in _MODES:
            raise ValueError("mode 无效。")
        return value

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValueError(f"{label}不能为空且最多 {maximum} 个字符。")
        return value.strip()

    @staticmethod
    def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{label} 必须是 {minimum} 到 {maximum} 之间的整数。")
        return value
