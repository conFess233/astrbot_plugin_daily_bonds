"""Regression checks for divorce selection and current-period bulk resets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_reset_without_reason_records_automatic_audit(self) -> None:
        plan = self.admin.preview({"action": "period_reset_counters", "scope_id": 1, "mode": "wife"}, actor="admin", now=10)
        self.assertIn("重置每日数据", plan["reason"])
        self.admin.commit({"preflight_id": plan["preflight_id"], "expected_revision": plan["config_revision"], "request_id": "automatic-reason"}, actor="admin", now=11)
        self.assertEqual(self.storage._connection().execute("SELECT reason FROM web_admin_audit").fetchone()[0], plan["reason"])

    def test_steal_slot_is_separate_locked_and_full_does_not_count(self) -> None:
        with self.storage.transaction() as db:
            db.execute("INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,created_at) VALUES('snap-other','character','c2','另一角色','[]',1)")
            db.execute("INSERT INTO relationships(id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at) VALUES('normal-owned',1,'today','wife','thief','character','c2','snap-other','active',4)")
        args = dict(scope_id=1, period_id="today", mode="wife", actor_id="thief", target_id="owner",
                    capacity=1, steal_slot_capacity=1, probability=1.0, attempt_limit=3,
                    cooldown_seconds=0, target_protection_limit=0)
        result = self.runtime.gameplay.steal(**args, now=10)
        self.assertEqual(result.code, "STOLEN")
        db = self.storage._connection()
        stolen = db.execute("SELECT id,slot_kind FROM relationships WHERE owner_id='thief' AND subject_id='c1' AND state='active'").fetchone()
        self.assertEqual(stolen["slot_kind"], "steal")
        self.assertEqual(db.execute("SELECT slot_kind FROM relationships WHERE id='normal-owned'").fetchone()[0], "normal")
        self.assertEqual(self.runtime.gameplay.steal(**args, now=11).code, "STEAL_SLOT_FULL")
        self.assertEqual(db.execute("SELECT steal_attempts FROM daily_counters WHERE mode='wife' AND user_id='thief'").fetchone()[0], 1)
        self.assertEqual(self.runtime.gameplay.divorce(scope_id=1, period_id="today", mode="wife", owner_id="thief", divorce_limit=0, relationship_id=stolen["id"], now=12).code, "RELATIONSHIP_LOCKED")
        self.assertEqual(self.runtime.gameplay.gift(scope_id=1, period_id="today", mode="wife", sender_id="thief", recipient_id="friend", capacity=1, relationship_id=stolen["id"], now=13).code, "RELATIONSHIP_LOCKED")
        self.assertEqual(self.runtime.gameplay.steal(**{**args, "actor_id": "other", "target_id": "thief", "relation_id": stolen["id"]}, now=14).code, "STEAL_SLOT_LOCKED")
        matches = asyncio.run(self.runtime._divorce_matches(1, "today", "thief", ("wife",), None, None))
        self.assertEqual([item["id"] for item in matches], ["normal-owned"])
        plan = self.admin.preview({"action": "period_reset_relations", "scope_id": 1, "mode": "wife"}, actor="admin", now=15)
        self.assertEqual(plan["before"]["steal_slot_relationships"], 1)

    def test_zero_steal_slots_use_legacy_normal_capacity(self) -> None:
        args = dict(scope_id=1, period_id="today", mode="wife", actor_id="thief", target_id="owner",
                    capacity=1, steal_slot_capacity=0, probability=1.0, attempt_limit=3,
                    cooldown_seconds=0, target_protection_limit=0)
        self.assertEqual(self.runtime.gameplay.steal(**args, now=10).code, "STOLEN")
        row = self.storage._connection().execute("SELECT id,slot_kind FROM relationships WHERE owner_id='thief' AND state='active'").fetchone()
        self.assertEqual(row["slot_kind"], "normal")
        self.assertEqual(self.runtime.gameplay.divorce(scope_id=1, period_id="today", mode="wife", owner_id="thief", divorce_limit=0, relationship_id=row["id"], now=11).code, "DIVORCED")

    def test_failed_steal_does_not_fill_slot(self) -> None:
        args = dict(scope_id=1, period_id="today", mode="wife", actor_id="thief", target_id="owner",
                    capacity=1, steal_slot_capacity=1, attempt_limit=3,
                    cooldown_seconds=0, target_protection_limit=0)
        self.assertEqual(self.runtime.gameplay.steal(**args, probability=0.0, now=10).code, "STEAL_FAILED")
        self.assertEqual(self.runtime.gameplay.steal(**args, probability=1.0, now=11).code, "STOLEN")
        self.assertEqual(self.storage._connection().execute("SELECT steal_attempts FROM daily_counters WHERE mode='wife' AND user_id='thief'").fetchone()[0], 2)

    def test_wife_and_husband_lists_are_separate_and_mark_steals(self) -> None:
        with self.storage.transaction() as db:
            db.execute("INSERT INTO subject_snapshots(id,subject_kind,subject_id,name,aliases_json,created_at) VALUES('snap-husband','character','c3','老公角色','[]',1)")
            db.execute("INSERT INTO relationships(id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at,slot_kind) VALUES('husband-relation',1,'today','husband','owner','character','c3','snap-husband','active',4,'steal')")
        self.runtime.renderer = SimpleNamespace(has_cjk_font=False, render=lambda *_args, **_kwargs: ())
        config = default_config()
        wife = asyncio.run(self.runtime._list_relationships(1, "today", "wife", None, 10, True, config=config))
        husband = asyncio.run(self.runtime._list_relationships(1, "today", "husband", None, 10, True, config=config))
        self.assertIn("老婆列表", wife.text)
        self.assertIn("同名", wife.text)
        self.assertNotIn("老公角色", wife.text)
        self.assertIn("老公列表", husband.text)
        self.assertIn("抢夺", husband.text)
        self.assertNotIn("同名", husband.text)
        keywords = config["commands"]["keywords"]
        self.assertEqual(parse_command([{"type": "text", "data": {"text": "老婆列表"}}], keywords).action, "list_characters")
        self.assertEqual(parse_command([{"type": "text", "data": {"text": "老公列表"}}], keywords).action, "list_husband")

    def test_member_list_card_contains_both_member_avatars_and_names(self) -> None:
        captured = []
        def render(_title, rows, **_kwargs):
            captured.extend(rows)
            return ()
        class AvatarCache:
            async def get_many(self, user_ids, **_kwargs):
                self.user_ids = set(user_ids)
                return {identifier: None for identifier in user_ids}
        with self.storage.transaction() as db:
            db.execute("INSERT INTO members(scope_id,user_id,nickname,card,is_present,is_known_bot,last_observed_at) VALUES(1,'owner','持有者','',1,0,3)")
        self.runtime.renderer = SimpleNamespace(has_cjk_font=True, render=render)
        self.runtime.avatar_cache = AvatarCache()
        asyncio.run(self.runtime._list_relationships(1, "today", "member", None, 10, True, config=default_config()))
        self.assertEqual(self.runtime.avatar_cache.user_ids, {"owner", "200"})
        self.assertEqual(captured[0].pair_names, ("持有者", "新群名"))
        self.assertEqual((captured[0].avatar_user_id, captured[0].other_avatar_user_id), ("owner", "200"))

    def test_upgrade_preserves_old_preflights_and_adds_default_alias(self) -> None:
        old_config = default_config()
        old_config["commands"]["keywords"]["divorce_member"] = ["踹群友"]
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        migrations = Path(__file__).resolve().parents[1] / "services" / "migrations"
        with sqlite3.connect(legacy_path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for version, name in enumerate(("001_initial.sql", "002_catalog_packs.sql", "003_web_request_dedup.sql", "004_catalog_admin.sql", "005_admin_correction_preflights.sql"), start=1):
                source = (migrations / name).read_text(encoding="utf-8")
                db.executescript(source)
                db.execute("INSERT INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)", (version, 1, hashlib.sha256((migrations / name).read_bytes()).hexdigest()))
            db.execute("INSERT INTO scopes(id,platform_id,self_id,group_id,umo,created_at) VALUES(1,'onebot','99','100','umo',1)")
            db.execute(
                "INSERT INTO settings(scope_key,revision,schema_version,value_json,updated_at,updated_by) VALUES('global',1,5,?,?,?)",
                (json.dumps(old_config, ensure_ascii=False), 1, "old"),
            )
            db.execute(
                "INSERT INTO web_admin_action_preflights(id,actor_username,scope_id,action,target_key,reason,spec_json,before_json,after_json,config_revision,created_at,expires_at) VALUES('old','admin',1,'counter_reset','target','reason','{}','{}','{}','old',1,100)")
        legacy = SQLiteStorage(legacy_path)
        legacy.open()
        try:
            config, _revision = legacy.get_settings()
            self.assertEqual(config["commands"]["keywords"]["divorce_member"], ["踹群友", "离婚群友"])
            self.assertTrue(_matches_migrated_defaults(old_config, config))
            self.assertEqual(legacy._connection().execute("SELECT action FROM web_admin_action_preflights WHERE id='old'").fetchone()[0], "counter_reset")
            self.assertEqual(legacy._connection().execute("SELECT slot_kind FROM relationships LIMIT 1").description[0][0], "slot_kind")
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
