"""互动结果的 LLM 文案：组装请求、清洗输出，失败时保留预设文案。

只处理 render_message 标记了 llm 请求的文本（白名单回复且开启了 llm）。
生成发生在业务提交之后，任何失败都直接返回原预设文本，不影响业务结果。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any

from .llm import LLMService
from .message_templates import IMAGE_TOKEN, MESSAGE_CONTEXT, MessageText

# 每条白名单回复对应的事件说明；变量来自同一次 render_message 的取值。
EVENTS = {
    "draw_wife": "{actor_name}抽中了今日老婆「{name}」",
    "draw_husband": "{actor_name}抽中了今日老公「{name}」",
    "draw_member": "{actor_name}今天娶到了群友「{name}」",
    "draw_member_mutual": "{actor_name}娶到了群友「{name}」，而对方也娶了{actor_name}，双向奔赴",
    "designated_wife": "{actor_name}指定娶了老婆「{name}」",
    "designated_husband": "{actor_name}指定娶了老公「{name}」",
    "force_success": "{actor_name}强娶群友「{name}」成功了",
    "force_failed": "{actor_name}想强娶群友「{subject_name}」，但失败了",
    "stolen": "{actor_name}从{owner_name}手里抢走了「{subject_name}」",
    "steal_failed": "{actor_name}想从{owner_name}手里抢「{subject_name}」，但失败了，对方仍归{owner_name}",
    "gifted": "{sender_name}把「{subject_name}」送给了{recipient_name}",
    "gift_accepted": "{recipient_name}接受了{sender_name}送来的「{subject_name}」",
    "divorced": "{actor_name}和「{subject_name}」解除了今天的关系",
    "mutual_formed": "{owner_name}和{subject_name}互相娶了对方，形成双向奔赴",
}
LLM_REPLY_KEYS = frozenset(EVENTS)
_FACT_FIELDS = (
    "actor_name",
    "name",
    "subject_name",
    "owner_name",
    "sender_name",
    "recipient_name",
    "target_name",
)
_CQ = re.compile(r"\[CQ:[^\]]*\]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\d{5,}")
_QUOTES = "\"'“”‘’「」『』"

SYSTEM_PROMPT = """你是 QQ 群娱乐插件“今日姻缘”的文案助手，为群里刚发生的一件事写一条回复。
要求：
1. 只输出回复正文，一段中文，不超过 {max_chars} 字。
2. 只能使用给出的人物和事件，不编造其他人物、数值或经历。
3. 不使用 @、链接、QQ 号或表情代码。
4. 语气轻松有趣；给了示例时可以参考风格，但不要照抄。"""

# 无论系统提示词是否被替换，都附在事实后面的硬性要求。
FACT_RULES = (
    "写作要求：只描述上面给出的结果，成功就是成功、失败就是失败；"
    "不要写出没有给出的数字，不要说亲密度或好感度上涨、下降，"
    "不要编造关系的后续变化。"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# 声称亲密度/好感度发生变化的说法；插件互动本身不改变亲密度。
_AFFECTION_CHANGE = re.compile(
    r"(亲密|好感|感情|羁绊)[^，。！？,.!?]{0,4}(涨|升|降|跌|增|减|加|掉|飙|暴|翻倍|拉满)"
)


def clean_reply(
    text: str, max_chars: int, allowed_numbers: frozenset[str] | None = None
) -> str | None:
    """清洗模型输出；为空、超长、写出未给出的数字或声称亲密度变化时返回 None。"""

    value = _CQ.sub("", text)
    value = _URL.sub("", value)
    value = _LONG_NUMBER.sub("", value)
    value = value.replace("@", "")
    value = " ".join(value.split()).strip().strip(_QUOTES).strip()
    if not value or len(value) > max_chars:
        return None
    if allowed_numbers is not None and any(
        number not in allowed_numbers for number in _NUMBER.findall(value)
    ):
        return None
    if _AFFECTION_CHANGE.search(value):
        return None
    return value


class LLMReplyWriter:
    """把带 llm 请求的预设文本替换为模型生成的文本。"""

    def __init__(
        self,
        llm: LLMService,
        pair_intimacy: Callable[[int, str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.llm = llm
        self.pair_intimacy = pair_intimacy

    async def rewrite(self, text: str) -> str:
        request = getattr(text, "llm", None)
        if not request or not str(text).strip():
            return text
        context = MESSAGE_CONTEXT.get()
        scope_id, umo = context.get("scope_id"), context.get("umo")
        if scope_id is None or not umo:
            return text
        max_chars = int(request["max_chars"])
        prompt = await self._prompt(request, context)
        # 只允许出现事实里给出的数字（例如亲密度数值），其余数字视为编造。
        allowed = frozenset(_NUMBER.findall(prompt.split(FACT_RULES)[0]))
        generated = await self.llm.generate(
            umo=str(umo),
            scope_id=int(scope_id),
            settings=request["settings"],
            system_prompt=self._system_prompt(request, max_chars),
            prompt=prompt,
            accept=lambda output: clean_reply(output, max_chars, allowed),
        )
        if generated is None:
            return text
        result = MessageText(
            f"{generated}{IMAGE_TOKEN}" if request["has_image"] else generated,
            quote_source=getattr(text, "quote_source", False),
            fallback=generated,
        )
        result.generated = True
        return result

    @staticmethod
    def _system_prompt(request: Mapping[str, Any], max_chars: int) -> str:
        # 管理员自定义的系统提示词为空时使用内置版本；保存时已校验只含 {max_chars}。
        template = str(request.get("system_prompt") or "").strip() or SYSTEM_PROMPT
        prompt = template.format(max_chars=max_chars)
        style = str(request.get("style") or "").strip()
        return f"{prompt}\n\n群管理员给出的风格要求：\n{style}" if style else prompt

    async def _prompt(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> str:
        facts = request["facts"]
        event = EVENTS[request["key"]].format_map(
            {field: facts.get(field, "群友") for field in _FACT_FIELDS}
        )
        lines = [f"刚发生的事：{event}。"]
        intimacy = await self._intimacy_line(context, facts)
        if intimacy:
            lines.append(intimacy)
        lines.append(FACT_RULES)
        event_prompt = str(request.get("event_prompt") or "").strip()
        if event_prompt:
            lines.append(f"群管理员对这类回复的要求：\n{event_prompt}")
        examples = [example for example in request.get("examples", ()) if example]
        if examples:
            lines.append("参考风格的示例回复（不要照抄，也不要沿用其中的数字和情节）：")
            lines.extend(f"- {example}" for example in examples)
        lines.append("请写一条新的回复。")
        return "\n".join(lines)

    async def _intimacy_line(
        self, context: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> str:
        """群友玩法附上双方的亲密度，让文案贴合两人关系；读取失败时省略。"""

        actor_id, subject_id = context.get("actor_id"), context.get("subject_id")
        if (
            self.pair_intimacy is None
            or context.get("subject_kind") != "member"
            or not actor_id
            or not subject_id
            or actor_id == subject_id
        ):
            return ""
        try:
            scores = await asyncio.to_thread(
                self.pair_intimacy, int(context["scope_id"]), actor_id, subject_id
            )
        except Exception:  # noqa: BLE001 - 亲密度只是参考信息
            return ""
        other = facts.get("name") or facts.get("subject_name") or "对方"
        return (
            f"两人现有的亲密度（只用来判断关系远近，这次互动没有改变它）："
            f"{facts.get('actor_name', '发起者')}对{other}为"
            f" {scores['forward_score']}，{other}对{facts.get('actor_name', '发起者')}为"
            f" {scores['reverse_score']}。"
        )
