"""领域异常。"""

from __future__ import annotations


class DomainError(Exception):
    """可向调用入口转换为用户提示的领域错误。"""


class ConfigurationError(DomainError):
    """配置类型、取值或别名存在冲突。"""


class CommandSyntaxError(DomainError):
    """消息已命中插件关键词，但不符合完整命令语法。"""


class StorageError(DomainError):
    """数据库状态无法安全读取、迁移或写入。"""


class InvalidInputError(ValueError):
    """外部输入（请求体、导入文件、仓库配置）的结构不符合要求。

    继承 ValueError，沿用各入口已有的“参数错误”转换。
    """
