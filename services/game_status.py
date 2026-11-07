"""个人状态、连续结缘、互选与图鉴的只读查询。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models.gameplay import Candidate
from . import game_rules as rules
from .storage import SQLiteStorage

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
        with self.storage.read() as db:
            modes = []
            for mode in MODES:
                mode_config = config["modes"][mode]
                rows = db.execute(
                    """SELECT r.id,r.subject_id,r.subject_kind,r.slot_kind,r.acquisition_kind,r.special_source,r.source_owner_id,s.name
                       FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
                       WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.owner_id=? AND r.state='active'
                       ORDER BY r.acquired_at,r.id""",
                    (scope_id, period_id, mode, user_id),
                ).fetchall()
                counter = db.execute(
                    "SELECT * FROM daily_counters WHERE scope_id=? AND period_id=? AND mode=? AND user_id=?",
                    (scope_id, period_id, mode, user_id),
                ).fetchone()
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
                            "mutual": row["subject_kind"] == "member"
                            and self._mutual(
                                db,
                                scope_id,
                                period_id,
                                user_id,
                                str(row["subject_id"]),
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
                        "force_limit": int(mode_config.get("force_limit", 0)),
                        "force_remaining": max(
                            0,
                            int(mode_config.get("force_limit", 0))
                            - int(used.get("force_attempts", 0)),
                        ),
                        "divorce_enabled": bool(mode_config["divorce_enabled"]),
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
        with self.storage.read() as db:
            return self._streak(db, scope_id, mode, owner_id, subject_id, sequence)

    @staticmethod
    def _streak(db, scope_id, mode, owner_id, subject_id, sequence) -> int:
        """从当前周期往前数，连续持有同一对象的周期数（受历史保留天数限制）。

        前后周期必须首尾相接：群里停摆多日后周期序号仍然相邻，但不算连续。
        """

        rows = db.execute(
            """SELECT DISTINCT p.sequence,p.starts_at,p.ends_at FROM relationships r
               JOIN periods p ON p.scope_id=r.scope_id AND p.id=r.period_id
               WHERE r.scope_id=? AND r.mode=? AND r.owner_id=? AND r.subject_id=? AND p.sequence<=?
               ORDER BY p.sequence DESC""",
            (scope_id, mode, owner_id, subject_id, sequence),
        ).fetchall()
        count = 0
        expected = sequence
        next_start = None
        for row in rows:
            if int(row["sequence"]) != expected or (
                next_start is not None and int(row["ends_at"]) != next_start
            ):
                break
            count += 1
            expected -= 1
            next_start = int(row["starts_at"])
        return count

    @staticmethod
    def _mutual(db, scope_id, period_id, owner_id, subject_id) -> bool:
        row = db.execute(
            "SELECT 1 FROM relationships WHERE scope_id=? AND period_id=? AND mode='member' AND owner_id=? AND subject_id=? AND state='active' LIMIT 1",
            (scope_id, period_id, subject_id, owner_id),
        ).fetchone()
        return row is not None

    def is_mutual(
        self, *, scope_id: int, period_id: str, owner_id: str, subject_id: str
    ) -> bool:
        """群友玩法中，对方也持有自己时为双向奔赴。"""

        with self.storage.read() as db:
            return self._mutual(db, scope_id, period_id, owner_id, subject_id)

    def intimacy(self, *, scope_id: int, source_id: str, target_id: str) -> int:
        with self.storage.read() as db:
            row = db.execute(
                "SELECT score FROM intimacy_edges WHERE scope_id=? AND source_id=? AND target_id=?",
                (scope_id, source_id, target_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def character_candidates(self, config: Mapping[str, Any]) -> list[Candidate]:
        """本群启用的角色池里所有未删除的角色，停用的也能查询。"""

        pool_ids = [
            pool_id
            for mode in ("wife", "husband")
            for pool_id in config["modes"][mode]["pool_ids"]
        ]
        if not pool_ids:
            return []
        marks = ",".join("?" for _ in pool_ids)
        with self.storage.read() as db:
            rows = db.execute(
                f"""SELECT DISTINCT c.id,c.name,c.aliases_json FROM characters c
                    JOIN pool_members pm ON pm.character_id=c.id
                    JOIN pools p ON p.id=pm.pool_id AND p.deleted_at IS NULL
                    WHERE c.deleted_at IS NULL AND p.id IN ({marks})""",
                pool_ids,
            ).fetchall()
        return [
            Candidate(
                str(row["id"]),
                "character",
                str(row["name"]),
                tuple(json.loads(row["aliases_json"])),
            )
            for row in rows
        ]

    def character_info(
        self, *, scope_id: int, period_id: str, character_id: str
    ) -> dict[str, Any]:
        with self.storage.read() as db:
            character = db.execute(
                """SELECT c.id,c.name,c.aliases_json,
                          (SELECT mb.relative_path FROM character_images ci
                           JOIN media_blobs mb ON mb.hash=ci.media_hash
                           WHERE ci.character_id=c.id ORDER BY ci.ordinal LIMIT 1) AS image
                   FROM characters c WHERE c.id=?""",
                (character_id,),
            ).fetchone()
            pools = db.execute(
                """SELECT p.name FROM pools p JOIN pool_members pm ON pm.pool_id=p.id
                   WHERE pm.character_id=? AND p.deleted_at IS NULL ORDER BY p.name""",
                (character_id,),
            ).fetchall()
            holders = db.execute(
                """SELECT owner_id,mode FROM relationships WHERE scope_id=? AND period_id=?
                   AND subject_id=? AND state='active' ORDER BY acquired_at,id""",
                (scope_id, period_id, character_id),
            ).fetchall()
            totals = db.execute(
                "SELECT COALESCE(SUM(times),0),COUNT(DISTINCT owner_id) FROM collection_entries WHERE scope_id=? AND character_id=?",
                (scope_id, character_id),
            ).fetchone()
        return {
            "id": str(character["id"]),
            "name": str(character["name"]),
            "aliases": json.loads(character["aliases_json"]),
            "image": character["image"],
            "pools": [str(row[0]) for row in pools],
            "holders": [
                {"owner_id": str(row["owner_id"]), "mode": str(row["mode"])}
                for row in holders
            ],
            "times": int(totals[0]),
            "collectors": int(totals[1]),
        }

    def group_stats(
        self, *, scope_id: int, period_id: str, since: int, limit: int = 5
    ) -> dict[str, Any]:
        """本群统计；操作次数来自操作记录，受历史保留天数限制。"""

        with self.storage.read() as db:
            results = db.execute(
                """SELECT result_code,COUNT(*) AS amount FROM operation_log
                   WHERE scope_id=? AND created_at>? AND actor_type='qq'
                   GROUP BY result_code""",
                (scope_id, since),
            ).fetchall()
            drawers = db.execute(
                """SELECT actor_id,COUNT(*) AS times FROM operation_log
                   WHERE scope_id=? AND created_at>? AND result_code IN ('DRAWN','DESIGNATED_DRAWN')
                   GROUP BY actor_id ORDER BY times DESC,actor_id LIMIT ?""",
                (scope_id, since, limit),
            ).fetchall()
            characters = db.execute(
                """SELECT COALESCE(c.name,e.character_id) AS name,SUM(e.times) AS times
                   FROM collection_entries e LEFT JOIN characters c ON c.id=e.character_id
                   WHERE e.scope_id=? GROUP BY e.character_id ORDER BY times DESC,name LIMIT ?""",
                (scope_id, limit),
            ).fetchall()
            pairs = db.execute(
                """SELECT MIN(source_id,target_id) AS first,MAX(source_id,target_id) AS second,
                          SUM(score) AS score FROM intimacy_edges WHERE scope_id=?
                   GROUP BY first,second ORDER BY score DESC,first,second LIMIT ?""",
                (scope_id, limit * 4),
            ).fetchall()
            active = int(
                db.execute(
                    "SELECT COUNT(*) FROM relationships WHERE scope_id=? AND period_id=? AND state='active'",
                    (scope_id, period_id),
                ).fetchone()[0]
            )
        amounts = {str(row["result_code"]): int(row["amount"]) for row in results}
        return {
            "counts": {
                "draws": amounts.get("DRAWN", 0) + amounts.get("DESIGNATED_DRAWN", 0),
                "steal_success": amounts.get("STOLEN", 0),
                "steal_attempts": amounts.get("STOLEN", 0)
                + amounts.get("STEAL_FAILED", 0),
                "force_success": amounts.get("FORCED", 0),
                "force_attempts": amounts.get("FORCED", 0)
                + amounts.get("FORCE_FAILED", 0),
            },
            "active": active,
            "drawers": [
                {"user_id": str(row["actor_id"]), "times": int(row["times"])}
                for row in drawers
            ],
            "characters": [
                {"name": str(row["name"]), "times": int(row["times"])}
                for row in characters
            ],
            "pairs": [
                {
                    "first": str(row["first"]),
                    "second": str(row["second"]),
                    "score": int(row["score"]),
                }
                for row in pairs
            ],
        }

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
        with self.storage.read() as db:
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
    if row["special_source"]:
        return str(row["special_source"])
    if row["slot_kind"] == "designated":
        return "designated"
    return str(row["acquisition_kind"] or "draw")
