"""Regression checks for divorce selection and current-period bulk resets."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_daily_bonds.models import ParsedCommand, StorageError
from astrbot_plugin_daily_bonds.services.admin_corrections import AdminCorrectionService
from astrbot_plugin_daily_bonds.services.command_runtime import CommandRuntime
from astrbot_plugin_daily_bonds.services.gameplay import GameplayService
from astrbot_plugin_daily_bonds.services.host_config import _matches_migrated_defaults
from astrbot_plugin_daily_bonds.services.storage import SQLiteStorage
from astrbot_plugin_daily_bonds.utils.command_parser import parse_command
from astrbot_plugin_daily_bonds.services.settings import default_config


class DivorceResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = SQLiteStorage(Path(self.temp.name) / "state.sqlite3")
        self.storage.open()
        with self.storage.transaction() as db:
            db.execute("INSERT INTO scopes(id,platform_id,self_id,group_id,umo,created_at) VALUES(1,'onebot','99','100','umo',1)")
            db.execute("INSERT INTO periods(id,scope_id,sequence,starts_at,ends_at,timezone,reset_time,state) VALUES('today',1,1,1,9999999999,'Asia/Tokyo','00:00','current')")
            db.execute("INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,created_at) VALUES('snap-wife','character','c1','同名','[]',1)")
            db.execute("INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,created_at) VALUES('snap-member','member','200','旧群名','[]',1)")
            db.execute("INSERT INTO relationships(id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at) VALUES('wife-relation',1,'today','wife','owner','character','c1','snap-wife','active',2)")
            db.execute("INSERT INTO relationships(id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at) VALUES('member-relation',1,'today','member','owner','member','200','snap-member','active',3)")
            db.execute("INSERT INTO members(scope_id,user_id,nickname,card,is_present,is_known_bot,last_observed_at) VALUES(1,'200','同名','新群名',1,0,3)")
            db.execute("INSERT INTO daily_counters(scope_id,period_id,mode,user_id,normal_draws,steal_attempts,stolen_successes,divorces,last_steal_at) VALUES(1,'today','wife','owner',2,3,1,1,4)")
            db.execute("INSERT INTO redraw_credits(id,scope_id,period_id,mode,user_id,source_operation_id,reason,state,issued_at) VALUES('credit',1,'today','wife','owner','source','admin_grant','available',3)")
            db.execute("INSERT INTO gift_invites(id,scope_id,period_id,mode,relationship_id,relationship_version,sender_id,recipient_id,state,created_at,expires_at) VALUES('invite',1,'today','wife','wife-relation',1,'owner','friend','pending',3,9999999999)")
        self.runtime = CommandRuntime.__new__(CommandRuntime)
        self.runtime.storage = self.storage
        self.runtime.gameplay = GameplayService(self.storage)
        self.admin = AdminCorrectionService(self.storage)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_divorce_commands_and_name_matching(self) -> None:
        keywords = default_config()["commands"]["keywords"]
        for message in ("离婚", "离婚老婆", "踹群友", "离婚群友 新群名", "离婚群友 200"):
            self.assertIsNotNone(parse_command([{"type": "text", "data": {"text": message}}], keywords))
        parsed = parse_command([{"type": "text", "data": {"text": "离婚群友 "}}, {"type": "at", "data": {"qq": "200"}}], keywords)
        self.assertEqual(parsed.target_user_id, "200")
        matches = asyncio.run(self.runtime._divorce_matches(1, "today", "owner", ("wife", "husband", "member"), "同名", None))
        self.assertEqual({row["id"] for row in matches}, {"wife-relation", "member-relation"})
        for name in ("旧群名", "新群名", "200"):
            matches = asyncio.run(self.runtime._divorce_matches(1, "today", "owner", ("member",), name, None))
            self.assertEqual([row["id"] for row in matches], ["member-relation"])
        matches = asyncio.run(self.runtime._divorce_matches(1, "today", "owner", ("member",), None, "200"))
        self.assertEqual([row["id"] for row in matches], ["member-relation"])

    def test_generic_divorce_prompts_then_selects_only_relationship(self) -> None:
        config = default_config()
        def run(command: ParsedCommand) -> object:
            return asyncio.run(self.runtime._query_or_action(
                command=command, mode="wife", scope_id=1, period_id="today", actor_id="owner",
                config=config, revision="test", event_key=f"event-{command.action}",
                payload_hash="test", now=10, eligible={"owner", "200"},
            ))
        prompt = run(ParsedCommand("divorce_character"))
        self.assertIn("老婆", prompt)
        self.assertIn("群友", prompt)
        db = self.storage._connection()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM relationships WHERE state='active'").fetchone()[0], 2)
        with self.storage.transaction() as transaction:
            transaction.execute("UPDATE relationships SET state='ended',ended_at=9,end_reason='test' WHERE id='wife-relation'")
        reply = run(ParsedCommand("divorce_character"))
        self.assertIn("关系已解除", reply.text)
        self.assertEqual(db.execute("SELECT state FROM relationships WHERE id='member-relation'").fetchone()[0], "ended")

    def test_mode_specific_divorce_selects_single_relation(self) -> None:
        reply = asyncio.run(self.runtime._query_or_action(
            command=ParsedCommand("divorce_member"), mode="member", scope_id=1,
            period_id="today", actor_id="owner", config=default_config(),
            revision="test", event_key="member-divorce", payload_hash="test",
            now=10, eligible={"owner", "200"},
        ))
        self.assertIn("关系已解除", reply.text)
        db = self.storage._connection()
        self.assertEqual(db.execute("SELECT state FROM relationships WHERE id='member-relation'").fetchone()[0], "ended")
        self.assertEqual(db.execute("SELECT state FROM relationships WHERE id='wife-relation'").fetchone()[0], "active")

    def test_independent_resets_keep_invite_credit_and_history(self) -> None:
        plan = self.admin.preview({"action": "period_reset_relations", "scope_id": 1, "mode": "wife", "reason": "测试"}, actor="admin", now=10)
        self.assertEqual(plan["before"]["active_relationships"], 1)
        self.assertEqual(plan["before"]["pending_invites_retained"], 1)
        self.admin.commit({"preflight_id": plan["preflight_id"], "expected_revision": plan["config_revision"], "request_id": "relations"}, actor="admin", now=11)
        db = self.storage._connection()
        self.assertEqual(db.execute("SELECT state FROM relationships WHERE id='wife-relation'").fetchone()[0], "ended")
        self.assertEqual(db.execute("SELECT state FROM relationships WHERE id='member-relation'").fetchone()[0], "active")
        self.assertEqual(db.execute("SELECT normal_draws FROM daily_counters WHERE mode='wife'").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT state FROM gift_invites WHERE id='invite'").fetchone()[0], "pending")
        self.assertEqual(db.execute("SELECT state FROM redraw_credits WHERE id='credit'").fetchone()[0], "available")

        plan = self.admin.preview({"action": "period_reset_counters", "scope_id": 1, "mode": "all", "reason": "测试"}, actor="admin", now=12)
        self.admin.commit({"preflight_id": plan["preflight_id"], "expected_revision": plan["config_revision"], "request_id": "counters"}, actor="admin", now=13)
        self.assertEqual(tuple(db.execute("SELECT normal_draws,steal_attempts,stolen_successes,divorces,last_steal_at FROM daily_counters WHERE mode='wife'").fetchone()), (0, 0, 0, 0, None))
        self.assertEqual(db.execute("SELECT state FROM gift_invites WHERE id='invite'").fetchone()[0], "pending")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM web_admin_audit").fetchone()[0], 2)

    def test_invite_is_invalidated_only_when_accepted_after_reset(self) -> None:
        plan = self.admin.preview({"action": "period_reset_relations", "scope_id": 1, "mode": "wife", "reason": "测试"}, actor="admin", now=10)
        self.admin.commit({"preflight_id": plan["preflight_id"], "expected_revision": plan["config_revision"], "request_id": "relations"}, actor="admin", now=11)
        db = self.storage._connection()
        self.assertEqual(db.execute("SELECT state FROM gift_invites WHERE id='invite'").fetchone()[0], "pending")
        result = self.runtime.gameplay.answer_invite(
            scope_id=1, period_id="today", invite_id="invite", actor_id="friend", action="accept",
            capacity=1, mode_enabled=True, gift_enabled=True, sender_allowed=True,
            recipient_allowed=True, now=12,
        )
        self.assertEqual(result.code, "INVITE_INVALIDATED")
        self.assertEqual(db.execute("SELECT reason FROM gift_invites WHERE id='invite'").fetchone()[0], "relationship_changed")

    def test_preview_detects_changed_relation(self) -> None:
        plan = self.admin.preview({"action": "period_reset_relations", "scope_id": 1, "mode": "member", "reason": "测试"}, actor="admin", now=10)
        with self.storage.transaction() as db:
            db.execute("UPDATE relationships SET version=version+1 WHERE id='member-relation'")
        with self.assertRaisesRegex(StorageError, "目标数据已变化"):
            self.admin.commit({"preflight_id": plan["preflight_id"], "expected_revision": plan["config_revision"], "request_id": "stale"}, actor="admin", now=11)

    def test_upgrade_preserves_old_preflights_and_adds_default_alias(self) -> None:
        old_config = default_config()
        old_config["commands"]["keywords"]["divorce_member"] = ["踹群友"]
        with self.storage.transaction() as db:
            db.execute("DROP TABLE web_admin_action_preflights")
            db.execute(Path(__file__).resolve().parents[1].joinpath("services/migrations/005_admin_correction_preflights.sql").read_text())
            db.execute("DELETE FROM schema_migrations WHERE version=6")
            db.execute(
                "INSERT INTO settings(scope_key,revision,schema_version,value_json,updated_at,updated_by) VALUES('global',1,5,?,?,?)",
                (json.dumps(old_config, ensure_ascii=False), 1, "old"),
            )
            db.execute(
                "INSERT INTO web_admin_action_preflights(id,actor_username,scope_id,action,target_key,reason,spec_json,before_json,after_json,config_revision,created_at,expires_at) VALUES('old','admin',1,'counter_reset','target','reason','{}','{}','{}','old',1,100)")
        self.storage.close()
        self.storage.open()
        config, _revision = self.storage.get_settings()
        self.assertEqual(config["commands"]["keywords"]["divorce_member"], ["踹群友", "离婚群友"])
        self.assertTrue(_matches_migrated_defaults(old_config, config))
        self.assertEqual(self.storage._connection().execute("SELECT action FROM web_admin_action_preflights WHERE id='old'").fetchone()[0], "counter_reset")


if __name__ == "__main__":
    unittest.main()
