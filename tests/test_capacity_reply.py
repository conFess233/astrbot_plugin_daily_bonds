"""Full-capacity draw replies for character modes."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_daily_bonds.services.command_runtime import CommandRuntime, RuntimeReply
from astrbot_plugin_daily_bonds.services.host_config import HostConfigBridge
from astrbot_plugin_daily_bonds.services.settings import default_config
from astrbot_plugin_daily_bonds.services.storage import SQLiteStorage


class CapacityReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = CommandRuntime.__new__(CommandRuntime)
        self.config = default_config()
        self.data = {"relationships": [{"id": "relation", "name": "某角色"}]}

    def reply(self, mode: str) -> RuntimeReply:
        return asyncio.run(self.runtime._result_reply("CAPACITY_FULL", self.data, mode, self.config))

    def test_single_capacity_uses_role_specific_text_without_card(self) -> None:
        for mode, expected in (("wife", "你今日已有老婆啦，请明日再来。"), ("husband", "你今日已有老公啦，请明日再来。")):
            with self.subTest(mode=mode):
                reply = self.reply(mode)
                self.assertEqual(reply.text, expected)
                self.assertEqual(reply.image_paths, ())
                self.assertFalse(reply.starts_cooldown)

    def test_larger_capacity_keeps_names_as_text_only(self) -> None:
        self.config["modes"]["wife"]["capacity"] = 2
        reply = self.reply("wife")
        self.assertEqual(reply.text, "持有名额已满：某角色")
        self.assertEqual(reply.image_paths, ())
        self.assertFalse(reply.starts_cooldown)

    def test_single_capacity_text_can_be_configured(self) -> None:
        self.config["messages"]["results"]["capacity_full_single_wife"] = "今天已有老婆"
        self.assertEqual(self.reply("wife").text, "今天已有老婆")

    def test_existing_settings_gain_new_message_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(Path(temp) / "state.sqlite3")
            storage.open()
            try:
                previous = default_config()
                previous["messages"]["results"].pop("capacity_full_single_wife")
                previous["messages"]["results"].pop("capacity_full_single_husband")
                with storage.transaction() as db:
                    db.execute(
                        "INSERT INTO settings(scope_key,revision,schema_version,value_json,updated_at,updated_by) VALUES('global',1,6,?,?,?)",
                        (json.dumps(previous, ensure_ascii=False), 1, "old"),
                    )
                current, _revision = storage.get_settings()
                self.assertEqual(current["messages"]["results"]["capacity_full_single_wife"], "你今日已有老婆啦，请明日再来。")
                native = {"settings": previous, "_settings_revision": 1}
                reconciled = HostConfigBridge(native, storage).reconcile()
                self.assertEqual(reconciled, current)
                self.assertEqual(native["settings"], current)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
