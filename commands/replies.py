"""业务结果到回复文本、图片卡片与消息链的转换。"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..services.message_templates import (
    IMAGE_TOKEN,
    MESSAGE_CONTEXT,
    MessageText,
    render_message,
    set_message_values,
)
from ..services.rendering import CardRenderer, CardRow


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    text: str
    image_paths: tuple[Path, ...] = ()
    cleanup_paths: tuple[Path, ...] = ()
    starts_cooldown: bool = True
    fallback_text: str = ""
    merge_forward: bool = False
    text_chunks: tuple[str, ...] = ()


class ReplyMixin:
    """为 CommandRuntime 提供结果格式化与卡片回复。"""

    async def _reply_with_card(
        self,
        text: str,
        title: str,
        rows: Sequence[CardRow],
        *,
        fallback_text: str = "",
        merge_long: bool = False,
        chunk_chars: int = 1500,
        **options: Any,
    ) -> RuntimeReply:
        if not text.strip():
            return RuntimeReply(text)
        fallback = (
            fallback_text
            or text.replace(IMAGE_TOKEN, "")
            or getattr(text, "fallback", "")
        )
        images = ()
        if IMAGE_TOKEN in text:
            try:
                images = await asyncio.to_thread(
                    self.renderer.render,
                    title,
                    rows,
                    cache_key=str(uuid.uuid4()),
                    font_id=MESSAGE_CONTEXT.get().get("font_id", "auto"),
                    **options,
                )
            except Exception:  # noqa: BLE001 - 渲染降级为文字
                logger.exception("今日姻缘：卡片渲染失败，降级为文字。")
        visible = text.replace(IMAGE_TOKEN, "")
        if not images and not visible.strip():
            visible = fallback
        message = (
            text
            if images
            else MessageText(
                visible,
                quote_source=getattr(text, "quote_source", False),
                fallback=fallback,
            )
        )
        limit = CardRenderer.page_capacity(
            max_height=int(options.get("max_height", 2000)),
            max_rows=int(options.get("max_rows", 20)),
            ranking=bool(options.get("ranking")),
            has_header_avatar=bool(
                options.get("header_user_id") or options.get("header_avatar_path")
            ),
        )
        merged = merge_long and (len(rows) > limit or len(images) > 1)
        chunks: list[str] = []
        if merged and not images:
            current: list[str] = []
            for line in visible.splitlines():
                for offset in range(0, max(1, len(line)), chunk_chars):
                    part = line[offset : offset + chunk_chars]
                    if current and (
                        len(current) >= limit
                        or sum(map(len, current)) + len(current) + len(part)
                        > chunk_chars
                    ):
                        chunks.append("\n".join(current))
                        current = []
                    current.append(part)
            if current:
                chunks.append("\n".join(current))
        return RuntimeReply(
            message,
            images,
            images,
            fallback_text=fallback,
            merge_forward=merged,
            text_chunks=tuple(chunks),
        )

    async def _result_reply(
        self,
        code: str,
        data: Mapping[str, Any],
        mode: str,
        config: Mapping[str, Any],
        *,
        accept_keyword: str = "接受赠送",
    ) -> str | RuntimeReply:
        relation_id = data.get("relationship_id")
        if data.get("invite_id"):
            scope_id = MESSAGE_CONTEXT.get().get("scope_id")
            invite = (
                await asyncio.to_thread(self._invite, scope_id, str(data["invite_id"]))
                if scope_id is not None
                else None
            )
            if invite:
                relation_id = relation_id or invite["relationship_id"]
                names = MESSAGE_CONTEXT.get().get("member_names", {})
                set_message_values(
                    sender_name=names.get(str(invite["sender_id"]), "群友"),
                    recipient_name=names.get(str(invite["recipient_id"]), "群友"),
                )
        if relation_id:
            relation = await asyncio.to_thread(
                self._relationship_display, str(relation_id)
            )
            if relation:
                set_message_values(subject_name=relation["name"])
        if data.get("candidate"):
            set_message_values(subject_name=data["candidate"]["name"])
        text = self._format_result(
            code, data, mode, config, accept_keyword=accept_keyword
        )
        if code == "CAPACITY_FULL":
            relationships = data.get("relationships", ())
            names = "、".join(str(item["name"]) for item in relationships)
            if mode in {"wife", "husband"} and config["modes"][mode]["capacity"] == 1:
                text = render_message(
                    config,
                    "results",
                    f"capacity_full_single_{mode}",
                    name=names,
                    names=names,
                )
            if IMAGE_TOKEN not in text or not relationships:
                return RuntimeReply(text, starts_cooldown=False)
            details = [
                await asyncio.to_thread(self._relationship_display, str(item["id"]))
                for item in relationships
            ]
            avatars = await self._avatars(
                [
                    item["subject_id"]
                    for item in details
                    if item and item["subject_kind"] == "member"
                ],
                config,
            )
            rows = [
                CardRow(
                    str(item["name"]),
                    image_path=self._snapshot_image_path(item["image_paths"]),
                    avatar_path=avatars.get(item["subject_id"]),
                )
                for item in details
                if item
            ]
            if len(rows) == 1:
                image = rows[0].image_path or rows[0].avatar_path
                return RuntimeReply(
                    text, (image,) if image else (), starts_cooldown=False
                )
            card = await self._reply_with_card(
                text, render_message(config, "titles", "existing"), rows
            )
            return RuntimeReply(
                card.text,
                card.image_paths,
                card.cleanup_paths,
                starts_cooldown=False,
                fallback_text=card.fallback_text,
            )
        if code in {"DRAWN", "DESIGNATED_DRAWN"} and IMAGE_TOKEN in text:
            item = await asyncio.to_thread(
                self._relationship_display, str(data["relationship_id"])
            )
            if item is not None:
                image = (
                    (await self._avatars([item["subject_id"]], config)).get(
                        item["subject_id"]
                    )
                    if item["subject_kind"] == "member"
                    else self._snapshot_image_path(item["image_paths"])
                )
                return RuntimeReply(text, (image,) if image else ())
        successful = code in {
            "DRAWN",
            "DESIGNATED_DRAWN",
            "STEAL_FAILED",
            "STOLEN",
            "DIVORCED",
            "GIFTED",
            "INVITE_CREATED",
            "GIFT_ACCEPTED",
            "INVITE_REJECTED",
            "INVITE_CANCELLED",
        }
        return RuntimeReply(text, starts_cooldown=successful)

    def _relationship_display(self, relationship_id: str) -> dict[str, Any] | None:
        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    """SELECT r.scope_id,r.subject_id,r.subject_kind,s.name,
                          (SELECT GROUP_CONCAT(mb.relative_path,'|') FROM snapshot_images si
                           JOIN media_blobs mb ON mb.hash=si.media_hash WHERE si.snapshot_id=s.id) AS image_paths
                   FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id WHERE r.id=?""",
                    (relationship_id,),
                )
                .fetchone()
            )
        if row is None:
            return None
        result = dict(row)
        if result["subject_kind"] == "member":
            result["name"] = self._member_names(result["scope_id"]).get(
                str(result["subject_id"]), "群友"
            )
        return result

    async def _avatars(
        self,
        user_ids: list[str],
        config: Mapping[str, Any],
        *,
        group_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Path | None]:
        if not user_ids and not group_ids:
            return {}
        resources = config["resources"]
        try:
            return await self.avatar_cache.get_many(
                user_ids,
                ttl_seconds=resources["avatar_cache_ttl_seconds"],
                max_entries=resources["avatar_cache_max_entries"],
                group_ids=group_ids,
            )
        except Exception:  # noqa: BLE001 - 头像降级为占位图
            logger.exception("今日姻缘：头像读取失败，使用占位图。")
            return {}

    def _snapshot_image_path(self, image_paths: str | None) -> Path | None:
        if not image_paths:
            return None
        media_root = (self.storage.database_path.parent / "media").resolve()
        candidates = image_paths.split("|")
        random.shuffle(candidates)
        for relative in candidates:
            candidate = (media_root / relative).resolve()
            if media_root in candidate.parents and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _format_result(
        code: str,
        data: Mapping[str, Any],
        mode: str,
        config: Mapping[str, Any],
        *,
        accept_keyword: str = "接受赠送",
    ) -> str:
        if code in {"DRAWN", "DESIGNATED_DRAWN"}:
            return render_message(
                config,
                "results",
                "admin_set_wife"
                if data.get("admin_set")
                else f"designated_{mode}"
                if code == "DESIGNATED_DRAWN"
                else f"draw_{mode}",
                name=data["candidate"]["name"],
            )
        if code == "INVITE_CREATED":
            return render_message(
                config,
                "results",
                "invite_created",
                invite_id=data["invite_id"],
                accept_keyword=accept_keyword,
            )
        if code == "CAPACITY_FULL":
            names = "、".join(
                str(item["name"]) for item in data.get("relationships", ())
            )
            return render_message(
                config, "results", "capacity_full", names=names, name=names
            )
        if code == "STEAL_SLOT_FULL":
            return render_message(
                config,
                "results",
                "steal_slot_full",
                role=render_message(config, "titles", mode),
            )
        key = code.lower()
        if key in config["messages"]["results"]:
            return render_message(
                config,
                "results",
                key,
                invite_id=data.get("invite_id"),
                limit=data.get("limit"),
            )
        return render_message(config, "results", "unknown_result", code=code)
