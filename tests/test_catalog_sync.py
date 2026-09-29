"""Catalog directory and selected repository sync regression checks."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_daily_bonds.services.catalog import CatalogService, load_catalog_directory
from astrbot_plugin_daily_bonds.services.catalog_sync import CatalogSyncService
from astrbot_plugin_daily_bonds.services.storage import SQLiteStorage


class CatalogSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1] / "resources"
        self.storage = SQLiteStorage(Path(self.temp.name) / "state.sqlite3")
        self.storage.open()
        CatalogService(self.storage, self.root, Path(self.temp.name) / "media").seed_builtin_catalog(1)
        self.sync = CatalogSyncService(self.storage)
        bundled = load_catalog_directory(self.root)
        self.remote = {"commit_sha": "a" * 40,
                       "characters": {item["id"]: {key: value for key, value in item.items() if key != "pool_ids"} for item in bundled["characters"]},
                       "pools": {item["id"]: item for item in bundled["pools"]}}
        async def fetch():
            return copy.deepcopy(self.remote)
        self.sync._fetch_remote = fetch

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_packaged_directory_and_clean_baseline(self) -> None:
        db = self.storage._connection()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 55)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM pools").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM catalog_sync_baselines").fetchone()[0], 57)
        self.assertEqual(asyncio.run(self.sync.check(actor="admin"))["updates"], 0)

    def test_selected_new_items_and_missing_dependency(self) -> None:
        self.remote["characters"]["wuwa:test"] = {"id": "wuwa:test", "name": "测试角色", "aliases": [], "gender": "female", "enabled": False, "images": []}
        self.remote["pools"]["wuwa_test"] = {"id": "wuwa_test", "name": "测试池", "mode": "wife", "character_ids": ["wuwa:test"]}
        plan = asyncio.run(self.sync.check(actor="admin"))
        self.assertEqual({(item["kind"], item["id"], item["status"]) for item in plan["items"]},
                         {("character", "wuwa:test", "new"), ("pool", "wuwa_test", "new")})
        with self.assertRaisesRegex(ValueError, "请先勾选"):
            asyncio.run(self.sync.commit({"preview_id": plan["preview_id"], "characters": [], "pools": ["wuwa_test"]}, actor="admin"))
        self.assertIsNone(self.storage._connection().execute("SELECT id FROM pools WHERE id='wuwa_test'").fetchone())
        asyncio.run(self.sync.commit({"preview_id": plan["preview_id"], "characters": ["wuwa:test"], "pools": ["wuwa_test"]}, actor="admin"))
        self.assertEqual(self.storage._connection().execute("SELECT COUNT(*) FROM pool_members WHERE pool_id='wuwa_test'").fetchone()[0], 1)

    def test_local_conflict_and_upstream_removal(self) -> None:
        identifier = next(iter(self.remote["characters"]))
        self.remote["characters"][identifier]["name"] = "仓库新版"
        with self.storage.transaction() as db:
            db.execute("UPDATE characters SET name='本地改动',revision=revision+1 WHERE id=?", (identifier,))
        plan = asyncio.run(self.sync.check(actor="admin"))
        self.assertEqual(next(item for item in plan["items"] if item["id"] == identifier)["status"], "conflict")
        self.remote["characters"].pop(identifier)
        for pool in self.remote["pools"].values():
            pool["character_ids"] = [value for value in pool["character_ids"] if value != identifier]
        plan = asyncio.run(self.sync.check(actor="admin", force=True))
        self.assertEqual(next(item for item in plan["items"] if item["id"] == identifier)["status"], "removed")
        self.assertIsNotNone(self.storage._connection().execute("SELECT id FROM characters WHERE id=?", (identifier,)).fetchone())

    def test_image_failure_leaves_batch_unchanged(self) -> None:
        self.remote["characters"]["wuwa:test"] = {"id": "wuwa:test", "name": "测试角色", "aliases": [], "gender": "female", "enabled": False, "images": []}
        plan = asyncio.run(self.sync.check(actor="admin"))
        async def fail(_selected):
            raise ValueError("图片校验失败")
        self.sync._download_images = fail
        with self.assertRaisesRegex(ValueError, "图片校验失败"):
            asyncio.run(self.sync.commit({"preview_id": plan["preview_id"], "characters": ["wuwa:test"], "pools": []}, actor="admin"))
        self.assertIsNone(self.storage._connection().execute("SELECT id FROM characters WHERE id='wuwa:test'").fetchone())
