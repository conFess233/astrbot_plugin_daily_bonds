"""解析后的指令。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """完整语法解析后的命令，不包含任何数据库状态。"""

    action: str
    argument: str | None = None
    target_user_id: str | None = None
    page: int | None = None
