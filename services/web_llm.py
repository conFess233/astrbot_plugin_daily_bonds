"""AI 功能的管理 API：可选模型与人格、文案试生成、好感度评估记录。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from typing import Any

from astrbot.api.web import json_response, request

from ..models.errors import ConfigurationError
from .llm_replies import LLM_REPLY_KEYS, LLMReplyWriter
from .llm_replies import SYSTEM_PROMPT as REPLY_SYSTEM_PROMPT
from .message_templates import MESSAGE_CONTEXT, render_message
from .settings import default_config, merge_sparse, validate_config

# 试生成使用的示例人物，与回复预览一致；不读取真实群成员。
_SAMPLE_VALUES = {
    "actor_name": "小明",
    "name": "示例角色",
    "subject_name": "示例角色",
    "owner_name": "小红",
    "sender_name": "小明",
    "recipient_name": "小红",
    "target_name": "示例群友",
}


class LLMRoutes:
    """模型选择、试生成与评估记录。"""

    async def llm_options(self):
        _, denied = self._authorize()
        if denied:
            return denied
        llm = self.llm
        return json_response(
            {
                "ok": True,
                "data": {
                    "available": llm is not None,
                    "providers": llm.list_providers() if llm else [],
                    "personas": llm.list_personas() if llm else [],
                    # 内置的文案系统提示词，管理页用于占位与“填入内置提示词”。
                    "reply_system_prompt": REPLY_SYSTEM_PROMPT,
                },
                "request_id": str(uuid.uuid4()),
            }
        )

    async def llm_preview(self):
        """用草稿配置与示例人物实际调用一次模型；结果只返回页面，不发送到群。"""

        _, denied = self._authorize()
        if denied:
            return denied
        if self.llm is None:
            return self._error("LLM_UNAVAILABLE", "插件尚未就绪，暂时无法试生成。", 503)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        key = payload.get("key")
        draft = payload.get("draft")
        if key not in LLM_REPLY_KEYS:
            return self._error("INVALID_KEY", "这条回复不支持 AI 生成。", 400)
        if not isinstance(draft, dict):
            return self._error("INVALID_BODY", "draft 必须是完整配置对象。", 400)
        try:
            config = merge_sparse(default_config(), draft)
            config["messages"]["results"][key]["llm"] = True
            config["messages"]["results"][key]["enabled"] = True
            validate_config(config)
            scope_id, umo = await asyncio.to_thread(
                self._preview_session, payload.get("scope_id")
            )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return self._error("CONFIG_INVALID", str(exc), 400)
        if config["llm"]["provider_mode"] == "follow" and not umo:
            return self._error(
                "NO_SESSION",
                "“跟随会话”需要至少一个已使用过插件的群；请先选择群或改为指定模型。",
                400,
            )
        token = MESSAGE_CONTEXT.set(
            {"scope_id": scope_id, "umo": umo or "preview", **_SAMPLE_VALUES}
        )
        try:
            preset = render_message(config, "results", key, name=_SAMPLE_VALUES["name"])
            result = await LLMReplyWriter(self.llm).rewrite(preset)
        finally:
            MESSAGE_CONTEXT.reset(token)
        if not getattr(result, "generated", False):
            return self._error(
                "LLM_FAILED",
                "生成失败、超时或达到频率上限；群里会改发预设文案。请检查模型配置后重试。",
                502,
            )
        return json_response(
            {
                "ok": True,
                "data": {"text": str(result).replace("image", "").strip()},
                "request_id": str(uuid.uuid4()),
            }
        )

    def _preview_session(self, raw_scope: Any) -> tuple[int, str]:
        """试生成使用所选群的会话；全局配置下使用最近加入的群。"""

        with self.storage.read() as db:
            if raw_scope not in (None, "", "global"):
                row = db.execute(
                    "SELECT id,umo FROM scopes WHERE id=?", (int(raw_scope),)
                ).fetchone()
                if row is None:
                    raise ValueError("scope_id 不属于当前插件实例。")
            else:
                row = db.execute(
                    "SELECT id,umo FROM scopes ORDER BY id DESC LIMIT 1"
                ).fetchone()
        # 作用域编号用于每群频率计数；没有群时用 0 单独计数。
        return (int(row["id"]), str(row["umo"] or "")) if row else (0, "")

    async def sentiment_batches(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._query_scope_id)
            rows = await asyncio.to_thread(self._sentiment_rows, scope_id)
        except ValueError as exc:
            return self._error("INVALID_SCOPE", str(exc), 400)
        return json_response(
            {"ok": True, "data": rows, "request_id": str(uuid.uuid4())}
        )

    def _sentiment_rows(self, scope_id: int | None) -> list[dict[str, Any]]:
        """最近 30 个评估批次：时间、对数、加减合计与每对的变化（用成员名称显示）。"""

        with self.storage.read() as db:
            rows = db.execute(
                """SELECT o.created_at,o.scope_id,o.data_json,s.group_id FROM operation_log o
                   JOIN scopes s ON s.id=o.scope_id
                   WHERE o.kind='sentiment_adjust' AND (? IS NULL OR o.scope_id=?)
                   ORDER BY o.created_at DESC LIMIT 30""",
                (scope_id, scope_id),
            ).fetchall()
            names: dict[tuple[int, str], str] = {}
            for row in db.execute(
                """SELECT scope_id,user_id,card,nickname FROM members
                   WHERE scope_id IN (SELECT DISTINCT scope_id FROM operation_log
                                      WHERE kind='sentiment_adjust')"""
            ).fetchall():
                names[(int(row["scope_id"]), str(row["user_id"]))] = str(
                    row["card"] or row["nickname"] or row["user_id"]
                )
        result = []
        for row in rows:
            data: Mapping[str, Any] = json.loads(row["data_json"])
            items = data.get("adjustments", [])
            scope = int(row["scope_id"])
            result.append(
                {
                    "created_at": int(row["created_at"]),
                    "pairs": len(items),
                    "gained": sum(max(int(item["delta"]), 0) for item in items),
                    "lost": sum(max(-int(item["delta"]), 0) for item in items),
                    "details": "；".join(
                        f"{names.get((scope, item['source_id']), item['source_id'])}"
                        f"→{names.get((scope, item['target_id']), item['target_id'])}"
                        f" {int(item['delta']):+d}"
                        for item in items
                    ),
                }
            )
        return result
