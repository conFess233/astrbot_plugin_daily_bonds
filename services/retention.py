"""Bound retention of historical rows while preserving live references."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .avatar_cache import _prune_files
from .storage import SQLiteStorage

# 分批删除每批的行数：单批足够快，不会长时间占用写锁。
DELETE_BATCH_SIZE = 5000
# 只用于统计去重的事件记录保留时长；重放通常在几分钟内发生。
STATS_DEDUPE_SECONDS = 2 * 86400


class RetentionService:
    """Prune expired history in foreign-key order using effective group windows."""

    def __init__(self, storage: SQLiteStorage, data_root: Path) -> None:
        self.storage = storage
        self.data_root = data_root

    def prune(self, now: int) -> dict[str, int]:
        global_config, _ = self.storage.get_settings()
        history_days = int(global_config["history"]["retention_days"])
        dedupe_days = int(global_config["resources"]["dedupe_retention_days"])
        with self.storage.read() as db:
            scope_ids = [
                int(row["id"]) for row in db.execute("SELECT id FROM scopes").fetchall()
            ]
        activity_days = int(global_config["statistics"]["activity_window_days"])
        scope_statistics: dict[int, Mapping[str, Any]] = {}
        for scope_id in scope_ids:
            config, _ = self.storage.get_settings(scope_id)
            scope_statistics[scope_id] = config["statistics"]
            activity_days = max(
                activity_days, int(config["statistics"]["activity_window_days"])
            )

        history_cutoff = now - history_days * 86400
        dedupe_cutoff = now - dedupe_days * 86400
        stats_dedupe_cutoff = max(dedupe_cutoff, now - STATS_DEDUPE_SECONDS)
        activity_cutoff = now - (max(history_days, activity_days) + 1) * 86400
        cooldown_cutoff = now - 86400
        deleted: dict[str, int] = {}
        deleted["intimacy_decay"] = self._decay_intimacy(scope_statistics, now)
        # 好感度评估的每周期累计只在当前周期内有用。
        deleted["sentiment_daily"] = self._delete_batched(
            "sentiment_daily",
            "period_id NOT IN (SELECT id FROM periods WHERE state='current')",
            (),
        )
        # 每条消息都会写入的大表分批删除，批间释放写锁，消息处理不必等整次清理。
        deleted["activity_seconds"] = self._delete_batched(
            "activity_seconds", "observed_second<=?", (activity_cutoff,)
        )
        deleted["activity_pokes"] = self._delete_batched(
            "activity_pokes", "observed_second<=?", (activity_cutoff,)
        )
        # 只用于统计去重的记录很快就不会再被重放；指令记录按配置保留，用于重放返回原结果。
        deleted["processed_events"] = self._delete_batched(
            "processed_events",
            "observed_at<=? AND command_operation_id IS NULL",
            (stats_dedupe_cutoff,),
        ) + self._delete_batched("processed_events", "observed_at<=?", (dedupe_cutoff,))
        # 删除媒体记录与删除文件之间不能插入新的上传，否则会删掉刚被引用的文件。
        with self.storage._lock:
            media_paths: list[str] = []
            expired_jobs: list[tuple[str, str]] = []
            tracked_job_ids: set[str] = set()
            with self.storage.transaction() as db:
                deleted["cooldowns"] = self._delete(
                    db,
                    "DELETE FROM cooldowns WHERE last_command_at<=?",
                    (cooldown_cutoff,),
                )
                deleted["web_request_dedup"] = self._delete(
                    db,
                    "DELETE FROM web_request_dedup WHERE created_at<=?",
                    (dedupe_cutoff,),
                )
                deleted["web_admin_dedup"] = self._delete(
                    db,
                    "DELETE FROM web_admin_dedup WHERE created_at<=?",
                    (dedupe_cutoff,),
                )
                deleted["web_admin_preflights"] = self._delete(
                    db,
                    "DELETE FROM web_admin_preflights WHERE expires_at<=? OR consumed_at IS NOT NULL",
                    (now,),
                )
                deleted["web_admin_action_preflights"] = self._delete(
                    db,
                    "DELETE FROM web_admin_action_preflights WHERE expires_at<=? OR consumed_at IS NOT NULL",
                    (now,),
                )
                expired_jobs = [
                    (str(row["id"]), str(row["kind"]))
                    for row in db.execute(
                        "SELECT id,kind FROM maintenance_jobs WHERE expires_at<? AND state<>'committing'",
                        (now,),
                    ).fetchall()
                ]
                tracked_job_ids = {
                    str(row[0])
                    for row in db.execute("SELECT id FROM maintenance_jobs").fetchall()
                }
                deleted["maintenance_jobs"] = self._delete(
                    db,
                    "DELETE FROM maintenance_jobs WHERE created_at<=? AND expires_at<? AND state<>'committing'",
                    (history_cutoff, now),
                )
                deleted["notification_outbox"] = self._delete(
                    db,
                    "DELETE FROM notification_outbox WHERE state IN ('sent','failed','unknown') AND created_at<=?",
                    (history_cutoff,),
                )
                deleted["gift_invites"] = self._delete(
                    db,
                    "DELETE FROM gift_invites WHERE state<>'pending' AND finalized_at IS NOT NULL AND finalized_at<=?",
                    (history_cutoff,),
                )
                deleted["relationships"] = self._delete(
                    db,
                    """DELETE FROM relationships WHERE state='ended' AND ended_at<=? AND period_id IN
                       (SELECT id FROM periods WHERE state='closed')
                       AND NOT EXISTS (SELECT 1 FROM gift_invites i WHERE i.relationship_id=relationships.id)""",
                    (history_cutoff,),
                )
                deleted["operation_log"] = self._delete(
                    db,
                    """DELETE FROM operation_log WHERE created_at<=?
                       AND id NOT IN (SELECT command_operation_id FROM processed_events WHERE command_operation_id IS NOT NULL)""",
                    (history_cutoff,),
                )
                deleted["daily_counters"] = self._delete(
                    db,
                    """DELETE FROM daily_counters WHERE period_id IN
                       (SELECT id FROM periods WHERE state='closed' AND ends_at<=?)
                       AND NOT EXISTS (SELECT 1 FROM relationships r WHERE r.period_id=daily_counters.period_id AND r.state='active')""",
                    (history_cutoff,),
                )
                deleted["periods"] = self._delete(
                    db,
                    """DELETE FROM periods WHERE state='closed' AND ends_at<=?
                       AND NOT EXISTS (SELECT 1 FROM relationships r WHERE r.period_id=periods.id)
                       AND NOT EXISTS (SELECT 1 FROM daily_counters d WHERE d.period_id=periods.id)
                       AND NOT EXISTS (SELECT 1 FROM gift_invites i WHERE i.period_id=periods.id)
                       AND NOT EXISTS (SELECT 1 FROM operation_log o WHERE o.period_id=periods.id AND o.created_at>?)""",
                    (history_cutoff, history_cutoff),
                )
                deleted["snapshot_images"] = self._delete(
                    db,
                    "DELETE FROM snapshot_images WHERE snapshot_id NOT IN (SELECT snapshot_id FROM relationships)",
                )
                deleted["subject_snapshots"] = self._delete(
                    db,
                    "DELETE FROM subject_snapshots WHERE id NOT IN (SELECT snapshot_id FROM relationships)",
                )
                orphan_media = db.execute(
                    """SELECT hash,relative_path FROM media_blobs WHERE hash NOT IN
                       (SELECT media_hash FROM character_images UNION SELECT media_hash FROM snapshot_images)"""
                ).fetchall()
                for row in orphan_media:
                    media_paths.append(str(row["relative_path"]))
                deleted["media_blobs"] = self._delete(
                    db,
                    """DELETE FROM media_blobs WHERE hash NOT IN
                       (SELECT media_hash FROM character_images UNION SELECT media_hash FROM snapshot_images)""",
                )

            deleted["expired_job_artifacts"] = self._remove_expired_job_artifacts(
                expired_jobs, tracked_job_ids, history_cutoff
            )
            self._remove_media_files(media_paths)
        deleted["avatar_cache"] = _prune_files(
            self.data_root / "avatars",
            int(global_config["resources"]["avatar_cache_max_entries"]),
        )
        self.storage.optimize()
        return deleted

    def _remove_expired_job_artifacts(
        self,
        expired_jobs: list[tuple[str, str]],
        tracked_job_ids: set[str],
        orphan_cutoff: int,
    ) -> int:
        removed = 0
        roots = {
            "import": self.data_root / "staging" / "imports",
            "restore": self.data_root / "staging" / "maintenance",
            "export": self.data_root / "jobs",
            "backup": self.data_root / "jobs",
        }
        known: dict[Path, set[str]] = {}
        for job_id, kind in expired_jobs:
            root = roots.get(kind)
            if root is None:
                continue
            known.setdefault(root, set()).add(job_id)
            if self._remove_child_directory(root, job_id):
                removed += 1

        # Recover directories left behind if the process stopped before recording or
        # removing a job. Age them conservatively so a just-created preview survives.
        for root in (
            self.data_root / "staging" / "imports",
            self.data_root / "staging" / "maintenance",
            self.data_root / "jobs",
        ):
            if not root.is_dir() or root.is_symlink():
                continue
            root_resolved = root.resolve()
            for child in root.iterdir():
                if (
                    child.name in known.get(root, set())
                    or child.name in tracked_job_ids
                    or child.is_symlink()
                ):
                    continue
                if child.is_dir():
                    try:
                        if (
                            int(child.stat().st_mtime) <= orphan_cutoff
                            and root_resolved in child.resolve().parents
                        ):
                            shutil.rmtree(child)
                            removed += 1
                    except OSError:
                        continue
                elif root.name == "jobs" and child.is_file():
                    try:
                        if (
                            int(child.stat().st_mtime) <= orphan_cutoff
                            and root_resolved == child.resolve().parent
                        ):
                            child.unlink()
                            removed += 1
                    except OSError:
                        continue
        return removed

    @staticmethod
    def _remove_child_directory(root: Path, name: str) -> bool:
        if (
            not name
            or Path(name).name != name
            or name in {".", ".."}
            or root.is_symlink()
        ):
            return False
        child = root / name
        if not child.is_dir() or child.is_symlink():
            return False
        try:
            if root.resolve() not in child.resolve().parents:
                return False
            shutil.rmtree(child)
            return True
        except OSError:
            return False

    def _decay_intimacy(
        self, scope_statistics: Mapping[int, Mapping[str, Any]], now: int
    ) -> int:
        """按各群生效配置衰减亲密度；返回被清零删除的记录数。

        每个群记录上次衰减时间，停机或清理失败多日后按经过的整天数一次补齐；
        关闭衰减期间时间照常前移，重新开启时不追溯。
        """

        removed = 0
        for scope_id, statistics in scope_statistics.items():
            percent = int(statistics["intimacy_decay_percent"])
            idle_days = int(statistics["intimacy_decay_idle_days"])
            with self.storage.transaction() as db:
                row = db.execute(
                    "SELECT decayed_at FROM intimacy_decay WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO intimacy_decay(scope_id,decayed_at) VALUES(?,?)",
                        (scope_id, now),
                    )
                    continue
                days = (now - int(row["decayed_at"])) // 86400
                if days < 1:
                    continue
                db.execute(
                    "UPDATE intimacy_decay SET decayed_at=decayed_at+? WHERE scope_id=?",
                    (days * 86400, scope_id),
                )
                if percent <= 0:
                    continue
                # 向下取整：每次至少减 1 分，小分值也会逐渐归零；加一个极小量抵消浮点误差。
                db.execute(
                    """UPDATE intimacy_edges SET score=CAST(score*?+1e-9 AS INTEGER)
                       WHERE scope_id=? AND updated_at<=?""",
                    (
                        (1 - percent / 100) ** days,
                        scope_id,
                        now - idle_days * 86400,
                    ),
                )
                removed += self._delete(
                    db,
                    "DELETE FROM intimacy_edges WHERE scope_id=? AND score=0",
                    (scope_id,),
                )
        return removed

    def _delete_batched(
        self, table: str, condition: str, parameters: tuple[Any, ...]
    ) -> int:
        total = 0
        while True:
            with self.storage.transaction() as db:
                count = self._delete(
                    db,
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE {condition} LIMIT ?)",
                    (*parameters, DELETE_BATCH_SIZE),
                )
            total += count
            if count < DELETE_BATCH_SIZE:
                return total

    @staticmethod
    def _delete(
        db: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()
    ) -> int:
        return int(db.execute(sql, parameters).rowcount)

    def _remove_media_files(self, relative_paths: list[str]) -> None:
        media_root = (self.data_root / "media").resolve()
        for relative in relative_paths:
            path = (media_root / relative).resolve()
            if media_root in path.parents and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    # The database reference is gone; a failed unlink is a harmless orphan.
                    pass
