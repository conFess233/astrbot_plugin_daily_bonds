"""版本迁移：查看迁移报告，以及把 0.1.x 数据库一键导入并迁移到当前版本。

完整备份 ZIP 继续使用“完整恢复”；这里处理单独的 SQLite 文件，例如升级前自动
生成的 `backups/pre-migration-*.sqlite3` 或旧版本的 `state.sqlite3`。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models.errors import InvalidInputError, StorageError
from .config_upgrade import is_legacy, upgrade_config
from .storage import SQLiteStorage

_PLAN_TTL_SECONDS = 1800
_BACKUP_NAME = re.compile(r"pre-(?:migration|import)-[\w.-]+\.sqlite3")


class MigrationService:
    def __init__(
        self,
        storage: SQLiteStorage,
        on_replaced: Callable[[], None] | None = None,
    ) -> None:
        self.storage = storage
        self.on_replaced = on_replaced
        self.data_root = storage.database_path.parent
        self.backup_dir = self.data_root / "backups"
        self.staging_dir = self.data_root / "staging" / "migration"
        self._plans: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def overview(self) -> dict[str, Any]:
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT id,source,from_version,to_version,report_json,created_at FROM migration_reports ORDER BY created_at DESC LIMIT 20"
                )
                .fetchall()
            )
        backups = []
        if self.backup_dir.is_dir():
            for path in sorted(self.backup_dir.glob("pre-*.sqlite3"), reverse=True):
                if _BACKUP_NAME.fullmatch(path.name):
                    stat = path.stat()
                    backups.append(
                        {
                            "name": path.name,
                            "bytes": stat.st_size,
                            "created_at": int(stat.st_mtime),
                        }
                    )
        return {
            "reports": [
                {
                    "id": row["id"],
                    "source": row["source"],
                    "from_version": row["from_version"],
                    "to_version": row["to_version"],
                    "report": json.loads(row["report_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "backups": backups,
        }

    def preview(
        self,
        *,
        actor: str,
        upload: Path | None = None,
        backup_name: str | None = None,
        now: int,
    ) -> dict[str, Any]:
        """复制来源到暂存区并检查；不修改当前数据。"""

        if (upload is None) == (backup_name is None):
            raise InvalidInputError("请上传数据库文件或选择一个升级前备份。")
        if backup_name is not None:
            if not _BACKUP_NAME.fullmatch(backup_name):
                raise InvalidInputError("备份文件名无效。")
            source = self.backup_dir / backup_name
            if not source.is_file():
                raise InvalidInputError("所选备份不存在。")
        else:
            source = upload
        self._expire(now)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        plan_id = uuid.uuid4().hex
        staged = self.staging_dir / f"{plan_id}.sqlite3"
        shutil.copyfile(source, staged)
        try:
            summary = self._inspect(staged)
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        with self._lock:
            self._plans[plan_id] = {
                "actor": actor,
                "path": staged,
                "created_at": now,
                "summary": summary,
            }
        return {"preview_id": plan_id, **summary}

    def commit(self, plan_id: str, *, actor: str, now: int) -> dict[str, Any]:
        """备份当前数据库后替换为暂存库，并在重新打开时执行迁移；失败时还原。"""

        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None or plan["actor"] != actor:
                raise InvalidInputError("迁移预检已失效，请重新选择文件。")
            if now - plan["created_at"] > _PLAN_TTL_SECONDS:
                self._discard(plan_id)
                raise InvalidInputError("迁移预检已超时，请重新选择文件。")
            self._plans.pop(plan_id)
        staged: Path = plan["path"]
        live = self.storage.database_path
        stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d-%H%M%S")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = self.backup_dir / f"pre-import-{stamp}.sqlite3"
        aside = live.with_name(f"{live.name}.replaced-{plan_id}")
        with self.storage._lock:
            destination = sqlite3.connect(backup)
            try:
                self.storage._connection().backup(destination)
            finally:
                destination.close()
            self.storage.close()
            moved = False
            try:
                for suffix in ("-wal", "-shm"):
                    live.with_name(live.name + suffix).unlink(missing_ok=True)
                live.replace(aside)
                moved = True
                shutil.move(str(staged), live)
                self.storage.open()
            except Exception as exc:
                self.storage.close()
                live.unlink(missing_ok=True)
                if moved:
                    aside.replace(live)
                self.storage.open()
                if isinstance(exc, StorageError):
                    raise
                raise StorageError(f"迁移失败，已恢复原数据：{exc}") from exc
            aside.unlink(missing_ok=True)
            report = (
                self.storage._connection()
                .execute(
                    "SELECT report_json FROM migration_reports ORDER BY created_at DESC LIMIT 1"
                )
                .fetchone()
            )
            self.storage._connection().execute(
                "INSERT INTO migration_reports(id,source,from_version,to_version,report_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    "manual_import",
                    int(plan["summary"]["version"]),
                    int(plan["summary"]["target_version"]),
                    json.dumps(
                        {"actor": actor, "backup": backup.name}, ensure_ascii=False
                    ),
                    now,
                ),
            )
        if self.on_replaced is not None:
            self.on_replaced()
        return {
            "restored": True,
            "backup": backup.name,
            "report": json.loads(report[0]) if report else None,
        }

    def _inspect(self, path: Path) -> dict[str, Any]:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            check = connection.execute("PRAGMA integrity_check").fetchall()
            if len(check) != 1 or check[0][0] != "ok":
                raise InvalidInputError("数据库完整性检查未通过。")
            has_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not has_table:
                raise InvalidInputError("这不是今日姻缘的数据库文件。")
            applied = [
                {"version": int(row["version"]), "sha256": row["checksum"]}
                for row in connection.execute(
                    "SELECT version,checksum FROM schema_migrations ORDER BY version"
                )
            ]
            if not self.storage.accepts_migration_chain(applied):
                raise InvalidInputError(
                    "数据库迁移记录无法识别，可能来自更新或未知的版本。"
                )
            notes: dict[str, list[str]] = {}
            for row in connection.execute("SELECT scope_key,value_json FROM settings"):
                value = json.loads(row["value_json"])
                if is_legacy(value):
                    notes[row["scope_key"]] = upgrade_config(
                        value, sparse=row["scope_key"] != "global"
                    )[1]

            def count(sql: str) -> int:
                return int(connection.execute(sql).fetchone()[0])

            return {
                "version": applied[-1]["version"],
                "target_version": len(
                    list(self.storage.schema_path.glob("[0-9][0-9][0-9]_*.sql"))
                ),
                "scopes": count("SELECT COUNT(*) FROM scopes"),
                "active_relationships": count(
                    "SELECT COUNT(*) FROM relationships WHERE state='active'"
                ),
                "characters": count(
                    "SELECT COUNT(*) FROM characters WHERE deleted_at IS NULL"
                ),
                "settings_notes": notes,
            }
        except sqlite3.DatabaseError as exc:
            raise InvalidInputError(f"无法读取数据库文件：{exc}") from exc
        finally:
            connection.close()

    def _expire(self, now: int) -> None:
        with self._lock:
            for plan_id, plan in list(self._plans.items()):
                if now - plan["created_at"] > _PLAN_TTL_SECONDS:
                    self._discard(plan_id)

    def _discard(self, plan_id: str) -> None:
        plan = self._plans.pop(plan_id, None)
        if plan is not None:
            Path(plan["path"]).unlink(missing_ok=True)
