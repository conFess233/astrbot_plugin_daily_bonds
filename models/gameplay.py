"""玩法事务使用的候选与结果快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Candidate:
    """事务外准备、事务内复核的候选对象快照。"""

    subject_id: str
    subject_kind: str
    name: str
    aliases: tuple[str, ...] = ()
    gender: str = "unspecified"
    source_revision: int | None = None
    image_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BusinessResult:
    """一次业务事务的稳定结果码与返回数据。"""

    code: str
    data: dict[str, Any]
    replayed: bool = False
