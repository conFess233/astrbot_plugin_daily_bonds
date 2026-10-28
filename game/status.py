"""个人状态、连续结缘、互选与图鉴的只读查询。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..services.storage import SQLiteStorage
from . import rules

MODES = ("wife", "husband", "member")


class StatusService:
    """汇总一名用户当前周期的关系、额度与收集进度。"""

    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def collect(
        self,
        *,
        scope_id: int,
        period: Mapping[str, Any],
        user_id: str,
        config: Mapping[str, Any],
        member_names: Mapping[str, str],
        now: int,
    ) -> dict[str, Any]:
        period_id = str(period["id"])
        with self.storage._lock:
            db = self.storage._connection()
            modes = []
            for mode in MODES:
                mode_config = config["modes"][mode]
                rows = db.execute(
                    """SELECT r.id,r.subject_id,r.subject_kind,r.slot_kind,r.acquisition_kind,r.source_owner_id,s.name
                       FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
                       WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=? AND r.state='active'
                       ORDER BY r.acquired_at,r.id""",
                    (scope_id, period_id, mode, user_id),
                ).fetchall()
                counter = db.execute(
                    "SELECT * FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                    (scope_id, period_id, mode, user_id),
                ).fetchone()
                credits = int(
                    db.execute(
                        "SELECT COUNT(*) FROM redraw_credits WHERE scope_id=? AND period_id=? AND mode=? AND user_id=? AND state='available'",
                        (scope_id, period_id, mode, user_id),
                    ).fetchone()[0]
                )
                used = dict(counter) if counter else {}
                relationships = []
                for row in rows:
                    name = (
                        member_names.get(str(row["subject_id"]), str(row["name"]))
                        if row["subject_kind"] == "member"
                        else str(row["name"])
                    )
                    relationships.append(
                        {
                            "name": name,
                            "source": _source_key(row),
                            "source_owner_id": str(row["source_owner_id"] or ""),
                            "protected": row["slot_kind"] in {"steal", "designated"},
                            "streak": self._streak(
                                db,
                                scope_id,
                                mode,
                                user_id,
                                str(row["subject_id"]),
                                int(period["sequence"]),
                            ),
                        }
                    )
                capacity = int(mode_config["capacity"])
                normal_draws = int(used.get("normal_draws", 0))
                designated_limit = (
                    None if mode == "member" else int(mode_config["designated_limit"])
                )
                modes.append(
                    {
                        "mode": mode,
                        "enabled": bool(mode_config["enabled"]),
                        "capacity": capacity,
                        "held": len(relationships),
                        "relationships": relationships,
                        "normal_remaining": rules.normal_draw_remaining(
                            capacity, normal_draws
                        ),
                        "credits": credits,
                        "can_draw": rules.can_draw(
                            held=len(relationships),
                            capacity=capacity,
                            normal_draws=normal_draws,
                            credits=credits,
                        ),
                        # 指定次数为 0 表示关闭（不是不限）；群友玩法没有指定。
                        "designated_limit": designated_limit,
                        "designated_remaining": None
                        if designated_limit is None
                        else max(
                            0, designated_limit - int(used.get("designated_draws", 0))
                        ),
                        "steal_enabled": bool(mode_config["steal_enabled"]),
                        "steal_remaining": rules.limit_remaining(
                            int(mode_config["steal_attempt_limit"]),
                            int(used.get("steal_attempts", 0)),
                        ),
                        "steal_ready_in": rules.cooldown_remaining(
                            used.get("last_steal_at"),
                            int(mode_config["steal_cooldown_seconds"]),
                            now,
                        ),
                        "divorce_remaining": rules.limit_remaining(
                            int(mode_config["divorce_limit"]),
                            int(used.get("divorces", 0)),
                        ),
                    }
                )
            sent = int(
                db.execute(
                    "SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND sender_id=? AND state='pending'",
                    (scope_id, period_id, user_id),
                ).fetchone()[0]
            )
            received = int(
                db.execute(
                    "SELECT COUNT(*) FROM gift_invites WHERE scope_id=? AND period_id=? AND recipient_id=? AND state='pending'",
                    (scope_id, period_id, user_id),
                ).fetchone()[0]
            )
        return {
            "modes": modes,
            "invites_sent": sent,
            "invites_received": received,
            "reset_at": rules.format_reset(
                int(period["ends_at"]), config["reset"]["timezone"], now
            ),
        }

    def streak(
        self, *, scope_id: int, mode: str, owner_id: str, subject_id: str, sequence: int
    ) -> int:
        with self.storage._lock:
            return self._streak(
                self.storage._connection(),
                scope_id,
                mode,
                owner_id,
                subject_id,
                sequence,
            )

    @staticmethod
    def _streak(db, scope_id, mode, owner_id, subject_id, sequence) -> int:
        """从当前周期往前数，连续持有同一对象的周期数（受历史保留天数限制）。"""

        rows = db.execute(
            """SELECT DISTINCT p.sequence FROM relationships r
               JOIN periods p ON p.scope_id=r.scope_id AND p.id=r.period_id
               WHERE r.scope_id=? AND r.mode=? AND r.owner_id=? AND r.subject_id=? AND p.sequence<=?
               ORDER BY p.sequence DESC""",
            (scope_id, mode, owner_id, subject_id, sequence),
        ).fetchall()
        count = 0
        expected = sequence
        for row in rows:
            if int(row[0]) != expected:
                break
            count += 1
            expected -= 1
        return count

    def is_mutual(
        self, *, scope_id: int, period_id: str, owner_id: str, subject_id: str
    ) -> bool:
        """群友玩法中，对方也持有自己时为双向奔赴。"""

        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    "SELECT 1 FROM relationships WHERE scope_id=? AND period_id=? AND mode='member' AND owner_id=? AND subject_id=? AND state='active' LIMIT 1",
                    (scope_id, period_id, subject_id, owner_id),
                )
                .fetchone()
            )
        return row is not None

    def intimacy(self, *, scope_id: int, source_id: str, target_id: str) -> int:
        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    "SELECT score FROM intimacy_edges WHERE scope_id=? AND source_id=? AND target_id=?",
                    (scope_id, source_id, target_id),
                )
                .fetchone()
            )
        return int(row[0]) if row else 0

    def collection(
        self,
        *,
        scope_id: int,
        user_id: str,
        config: Mapping[str, Any],
        modes: tuple[str, ...] = ("wife", "husband"),
    ) -> list[dict[str, Any]]:
        """按玩法列出已启用角色池中的收集进度；已停用或移出池的角色仍计入已收集。"""

        result = []
        with self.storage._lock:
            db = self.storage._connection()
            for mode in modes:
                pool_ids = list(config["modes"][mode]["pool_ids"])
                total_ids: set[str] = set()
                if pool_ids:
                    marks = ",".join("?" for _ in pool_ids)
                    total_ids = {
                        str(row[0])
                        for row in db.execute(
                            f"""SELECT DISTINCT c.id FROM characters c
                                JOIN pool_members pm ON pm.character_id=c.id
                                JOIN pools p ON p.id=pm.pool_id AND p.deleted_at IS NULL
                                WHERE c.enabled=1 AND c.deleted_at IS NULL AND p.mode=? AND p.id IN ({marks})""",
                            (mode, *pool_ids),
                        )
                    }
                rows = db.execute(
                    """SELECT e.character_id,e.times,e.first_at,e.last_at,c.name,c.aliases_json,
                              (SELECT mb.relative_path FROM character_images ci
                               JOIN media_blobs mb ON mb.hash=ci.media_hash
                               WHERE ci.character_id=e.character_id ORDER BY ci.ordinal LIMIT 1) AS image
                       FROM collection_entries e LEFT JOIN characters c ON c.id=e.character_id
                       WHERE e.scope_id=? AND e.owner_id=? AND e.mode=?
                       ORDER BY e.times DESC,e.last_at DESC""",
                    (scope_id, user_id, mode),
                ).fetchall()
                entries = [
                    {
                        "id": str(row["character_id"]),
                        "name": str(row["name"] or row["character_id"]),
                        "aliases": json.loads(row["aliases_json"] or "[]"),
                        "times": int(row["times"]),
                        "image": row["image"],
                    }
                    for row in rows
                ]
                collected = sum(1 for entry in entries if entry["id"] in total_ids)
                result.append(
                    {
                        "mode": mode,
                        "collected": collected,
                        "total": len(total_ids),
                        "entries": entries,
                    }
                )
        return result


def _source_key(row: Mapping[str, Any]) -> str:
    if row["slot_kind"] == "designated":
        return "designated"
    return str(row["acquisition_kind"] or "draw")
