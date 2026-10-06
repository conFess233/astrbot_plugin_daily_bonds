"""按图片占位符顺序组装普通回复或合并转发。"""

from __future__ import annotations

from typing import Any

from astrbot.api.message_components import Image, Node, Nodes, Plain, Reply

from .command_runtime import RuntimeReply
from .message_templates import IMAGE_TOKEN


def reply_chain(response: str | RuntimeReply, event: Any) -> list[Any]:
    reply = response if isinstance(response, RuntimeReply) else RuntimeReply(response)
    images = list(reply.image_paths) if IMAGE_TOKEN in reply.text else []
    groups: list[list[Any]] = []
    if images:
        for index, text in enumerate(reply.text.split(IMAGE_TOKEN)):
            if index:
                groups.extend([[Image.fromFileSystem(str(path))] for path in images])
            if text.strip():
                groups.append([Plain(text)])
    else:
        text = reply.text.replace(IMAGE_TOKEN, "")
        if not text.strip():
            text = reply.fallback_text or getattr(reply.text, "fallback", "")
        chunks = (
            reply.text_chunks if reply.merge_forward and reply.text_chunks else (text,)
        )
        groups = [[Plain(chunk)] for chunk in chunks if chunk.strip()]
    if not groups:
        return []
    quote = None
    if getattr(reply.text, "quote_source", False):
        message = getattr(event, "message_obj", None)
        raw = getattr(message, "raw_message", {})
        message_id = getattr(message, "message_id", None) or raw.get("message_id")
        if message_id:
            quote = Reply(id=str(message_id))
    if reply.merge_forward:
        if quote is not None:
            groups[0].insert(0, quote)
        return [
            Nodes(
                [
                    Node(uin=str(event.get_self_id()), name="今日姻缘", content=group)
                    for group in groups
                ]
            )
        ]
    chain = [component for group in groups for component in group]
    if quote is not None:
        chain.insert(0, quote)
    return chain
