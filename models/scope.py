"""作用域与玩法模式类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["wife", "husband", "member"]
SubjectKind = Literal["character", "member"]


@dataclass(frozen=True, slots=True)
class Scope:
    """由平台实例、机器人账号和群号共同组成的独立作用域。"""

    platform_id: str
    self_id: str
    group_id: str
