"""统计近几天每条回复文案大约触发了多少次，供管理页判断哪些值得改。

只统计写入操作记录的业务结果（抽取、强娶、抢夺、赠送、离婚等）；指令格式
错误、目标不合规这类在业务之前就返回的提示没有记录，不显示次数。
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
from typing import Any

from .message_merge import MESSAGE_ALIASES
from .settings import default_config
from .storage import SQLiteStorage

_SPECIAL = {
    ("draw_designated", "DESIGNATED_DRAWN"): "results.designated",
    ("force", "FORCED"): "results.force_success",
    ("force", "ALREADY_HOLDS"): "results.force_already_holds",
    ("gift", "RELATIONSHIP_LOCKED"): "results.gift_locked",
    ("divorce", "RELATIONSHIP_LOCKED"): "results.divorce_locked",
    ("command_cooldown", "COMMAND_COOLDOWN"): "errors.command_cooldown",
}


@lru_cache(maxsize=1)
def _result_keys() -> frozenset[str]:
    return frozenset(default_config()["messages"]["results"])


def message_path(kind: str, mode: str | None, code: str) -> str | None:
    """操作记录的（类型, 玩法, 结果码）对应的文案路径；对应不上时返回 None。"""

    special = _SPECIAL.get((kind, code))
    if special:
        return special
    if code == "DRAWN":
        return "results.draw_member" if mode == "member" else "results.draw_character"
    if code == "NO_CANDIDATE" and mode == "member":
        return "results.no_candidate_member"
    key = code.lower()
    alias = MESSAGE_ALIASES.get(("results", key))
    if alias is not None:
        return f"{alias[0]}.{alias[1]}"
    if key in _result_keys():
        return f"results.{key}"
    return None


def message_usage(
    storage: SQLiteStorage, scope_id: int | None, now: int, days: int = 7
) -> dict[str, int]:
    """近 days 天各文案的触发次数，键为 messages.<分类>.<键>。"""

    since = now - days * 86400
    with storage.read() as db:
        rows = db.execute(
            """SELECT kind,mode,result_code,COUNT(*) AS total FROM operation_log
               WHERE created_at>=? AND (? IS NULL OR scope_id=?)
               GROUP BY kind,mode,result_code""",
            (since, scope_id, scope_id),
        ).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        path = message_path(str(row["kind"]), row["mode"], str(row["result_code"]))
        if path is not None:
            counts[f"messages.{path}"] += int(row["total"])
    return dict(counts)


def message_defaults() -> dict[str, Any]:
    return default_config()["messages"]
