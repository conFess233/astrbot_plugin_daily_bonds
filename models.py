"""今日姻缘插件共享的类型与领域异常。"""

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


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """完整语法解析后的命令，不包含任何数据库状态。"""

    action: str
    argument: str | None = None
    target_user_id: str | None = None
    page: int | None = None


class DomainError(Exception):
    """可向调用入口转换为用户提示的领域错误。"""


class ConfigurationError(DomainError):
    """配置类型、取值或别名存在冲突。"""


class CommandSyntaxError(DomainError):
    """消息已命中插件关键词，但不符合完整命令语法。"""


class StorageError(DomainError):
    """数据库状态无法安全读取、迁移或写入。"""
