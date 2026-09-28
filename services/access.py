"""群级门禁、用户名单与机器人身份判定。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import Scope


class AccessService:
    """统一执行宿主总开关之外的插件群/用户准入规则。"""

    @staticmethod
    def group_key(scope: Scope) -> str:
        """使用无歧义的 JSON 三元组标识完整群作用域。"""

        return json.dumps([scope.platform_id, scope.self_id, scope.group_id], ensure_ascii=False, separators=(",", ":"))

    def group_allowed(self, config: Mapping[str, Any], scope: Scope) -> bool:
        if not config["enabled"]:
            return False
        groups = config["access"]["groups"]
        if groups["mode"] == "unrestricted":
            return True
        listed = self.group_key(scope) in groups["ids"]
        return listed if groups["mode"] == "whitelist" else not listed

    @staticmethod
    def user_allowed(config: Mapping[str, Any], user_id: str) -> bool:
        users = config["access"]["users"]
        listed = user_id in users["ids"]
        if users["mode"] == "unrestricted":
            return True
        return listed if users["mode"] == "whitelist" else not listed

    @staticmethod
    def bot_ids(config: Mapping[str, Any], self_id: str, host_bot_ids: set[str] | None = None) -> set[str]:
        return {self_id, *config["access"]["extra_bot_ids"], *(host_bot_ids or set())}
