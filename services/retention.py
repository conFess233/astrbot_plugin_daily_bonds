"""Bound retention of historical rows while preserving live references."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .avatar_cache import _prune_files
from .storage import SQLiteStorage


class RetentionService:
    """Prune expired history in foreign-key order using effective group windows."""

    def __init__(self, storage: SQLiteStorage, data_root: Path) -> None:
        self.storage = storage
        self.data_root = data_root

    def prune(self, now: int) -> dict[str, int]:
        with self.storage._lock:
            global_config, _ = self.storage.get_settings()
            history_days = int(global_config["history"]["retention_days"])
            dedupe_days = int(global_config["resources"]["dedupe_retention_days"])
            scope_ids = [
                int(row["id"])
                for row in self.storage._connection()
                .execute("SELECT id FROM scopes")
                .fetchall()
            ]
            activity_days = int(global_config["statistics"]["activity_window_days"])
            for scope_id in scope_ids:
                config, _ = self.storage.get_settings(scope_id)
                activity_days = max(
                    activity_days, int(config["statistics"]["activity_window_days"])
                )

            history_cutoff = now - history_days * 86400
            dedupe_cutoff = now - dedupe_days * 86400
            activity_cutoff = now - (max(history_days, activity_days) + 1) * 86400
            cooldown_cutoff = now - 86400
            deleted: dict[str, int] = {}
            media_paths: list[str] = []
            expired_jobs: list[tuple[str, str]] = []
            tracked_job_ids: set[str] = set()
            with self.storage.transaction() as db:
                deleted["activity_seconds"] = self._delete(
                    db,
                    "DELETE FROM activity_seconds WHERE observed_second<=?",
                    (activity_cutoff,),
                )
                deleted["activity_pokes"] = self._delete(
                    db,
                    "DELETE FROM activity_pokes WHERE observed_second<=?",
                    (activity_cutoff,),
                )
                deleted["processed_events"] = self._delete(
                    db,
                    "DELETE FROM processed_events WHERE observed_at<=?",
                    (dedupe_cutoff,),
                )
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
                deleted["redraw_credits"] = self._delete(
                    db,
                    """DELETE FROM redraw_credits WHERE issued_at<=? AND period_id IN
                       (SELECT id FROM periods WHERE state='closed')""",
                    (history_cutoff,),
                )
                deleted["relationships"] = self._delete(
                    db,
                    """DELETE FROM relationships WHERE state='ended' AND ended_at<=? AND period_id IN
                       (SELECT id FROM periods WHERE state='closed')
                       AND NOT EXISTS (SELECT 1 FROM redraw_credits c WHERE c.consumed_relationship_id=relationships.id)
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
                       AND NOT EXISTS (SELECT 1 FROM redraw_credits c WHERE c.period_id=periods.id)
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
            return deleted

    def _remove_expired_job_artifacts(
        self, expired_jobs: list[tuple[str, str]], tracked_job_ids: set[str], orphan_cutoff: int
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
        for root in (self.data_root / "staging" / "imports", self.data_root / "staging" / "maintenance", self.data_root / "jobs"):
            if not root.is_dir() or root.is_symlink():
                continue
            root_resolved = root.resolve()
            for child in root.iterdir():
                if child.name in known.get(root, set()) or child.name in tracked_job_ids or child.is_symlink():
                    continue
                if child.is_dir():
                    try:
                        if int(child.stat().st_mtime) <= orphan_cutoff and root_resolved in child.resolve().parents:
                            shutil.rmtree(child)
                            removed += 1
                    except OSError:
                        continue
                elif root.name == "jobs" and child.is_file():
                    try:
                        if int(child.stat().st_mtime) <= orphan_cutoff and root_resolved == child.resolve().parent:
                            child.unlink()
                            removed += 1
                    except OSError:
                        continue
        return removed

    @staticmethod
    def _remove_child_directory(root: Path, name: str) -> bool:
        if not name or Path(name).name != name or name in {".", ".."} or root.is_symlink():
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

    @staticmethod
    def _delete(db: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> int:
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
