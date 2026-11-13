"""“我的姻缘”、“姻缘帮助”与“我的图鉴”指令。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import game_rules as rules
from .cards import CardService, data_uri, render_mode
from .command_replies import ReplyBuilder, RuntimeReply
from .game_status import StatusService
from .message_templates import (
    IMAGE_TOKEN,
    MESSAGE_CONTEXT,
    MessageText,
    render_message,
)
from .storage import SQLiteStorage

_MODE_LABELS = {"wife": "老婆", "husband": "老公", "member": "群友"}
_SOURCE_TITLES = {
    "draw": "normal_slot",
    "designated": "designated_slot",
    "gift": "gift_slot",
    "steal": "steal_slot",
    "admin": "admin_slot",
    "force": "force_slot",
}
_COLLECTION_THUMBNAILS = 40


class FeatureHandler:
    """“我的姻缘”、“姻缘帮助”与“我的图鉴”。"""

    def __init__(
        self,
        storage: SQLiteStorage,
        status: StatusService,
        cards: CardService,
        replies: ReplyBuilder,
    ) -> None:
        self.storage = storage
        self.status = status
        self.cards = cards
        self.replies = replies

    async def my_status(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        actor_name = names.get(values["actor_id"], "群友")
        status = await asyncio.to_thread(
            self.status.collect,
            scope_id=values["scope_id"],
            period=values["period"],
            user_id=values["actor_id"],
            config=config,
            member_names=names,
            now=values["now"],
        )
        title = render_message(config, "titles", "status", name=actor_name)
        sections = []
        lines = [f"{title}（{status['reset_at']} 重置）"]
        for item in status["modes"]:
            label = render_message(config, "titles", item["mode"])
            if not item["enabled"]:
                lines.append(f"【{label}】玩法已关闭")
                sections.append({"label": label, "closed": True})
                continue
            relations = []
            for relation in item["relationships"]:
                source = render_message(
                    config,
                    "titles",
                    _SOURCE_TITLES.get(relation["source"], "normal_slot"),
                    owner_name=names.get(relation["source_owner_id"], "群友"),
                )
                facts = [source]
                if relation["protected"]:
                    facts.append("受保护")
                if relation["streak"] >= 2:
                    facts.append(f"连续 {relation['streak']} 天")
                if relation["mutual"]:
                    facts.append(render_message(config, "titles", "mutual"))
                relations.append({"name": relation["name"], "facts": facts})
            quota = [f"普通抽取剩 {item['normal_remaining']} 次"]
            if item["designated_limit"]:
                quota.append(f"指定剩 {item['designated_remaining']} 次")
            if item["steal_enabled"]:
                remaining = item["steal_remaining"]
                quota.append(
                    "抢夺不限次数" if remaining is None else f"抢夺剩 {remaining} 次"
                )
                if item["steal_ready_in"]:
                    quota.append(f"抢夺冷却 {item['steal_ready_in']} 秒")
            if item["force_limit"]:
                quota.append(f"强娶剩 {item['force_remaining']} 次")
            if item["divorce_enabled"]:
                remaining = item["divorce_remaining"]
                quota.append(
                    "离婚不限次数" if remaining is None else f"离婚剩 {remaining} 次"
                )
            lines.append(f"【{label}】{item['held']}/{item['capacity']}")
            lines.extend(
                f"· {relation['name']} [{' · '.join(relation['facts'])}]"
                for relation in relations
            )
            lines.append(" · ".join(quota))
            sections.append(
                {
                    "label": label,
                    "closed": False,
                    "held": item["held"],
                    "capacity": item["capacity"],
                    "relations": relations,
                    "quota": quota,
                }
            )
        invites = []
        if status["invites_sent"]:
            invites.append(f"发出待处理赠送 {status['invites_sent']}")
        if status["invites_received"]:
            invites.append(f"收到待处理赠送 {status['invites_received']}")
        if invites:
            lines.append(" · ".join(invites))
        avatar = (await self.replies.avatars([values["actor_id"]], config)).get(
            values["actor_id"]
        )
        return await self._feature_reply(
            config,
            "my_status",
            "status",
            "status.html",
            lines,
            {
                "title": title,
                "subtitle": f"{status['reset_at']} 重置",
                "avatar": await asyncio.to_thread(data_uri, avatar, 160),
                "initial": actor_name[:1] or "?",
                "sections": sections,
                "invites": invites,
            },
        )

    async def help(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        keywords = config["commands"]["keywords"]

        def first(action: str) -> str:
            return keywords[action][0]

        groups = []
        for mode, draw, steal, gift, divorce, listing in (
            (
                "wife",
                "draw_wife",
                "steal_wife",
                "gift_wife",
                "divorce_wife",
                "list_characters",
            ),
            (
                "husband",
                "draw_husband",
                "steal_husband",
                "gift_husband",
                "divorce_husband",
                "list_husband",
            ),
            (
                "member",
                "draw_member",
                "steal_member",
                "gift_member",
                "divorce_member",
                "list_members",
            ),
        ):
            mode_config = config["modes"][mode]
            if not mode_config["enabled"]:
                continue
            commands = [first(draw)]
            if mode != "member" and mode_config["designated_limit"]:
                commands.append(f"{first(draw)} <名称或#ID>")
            if mode_config["steal_enabled"]:
                commands.append(f"{first(steal)} @持有者")
            if mode == "member" and mode_config["force_limit"]:
                commands.append(f"{first('force_member')} @群友")
            if mode_config["gift_enabled"]:
                commands.append(f"{first(gift)} @接收者")
            if mode_config["divorce_enabled"]:
                commands.append(first(divorce))
            commands.append(first(listing))
            groups.append(
                {"label": render_message(config, "titles", mode), "commands": commands}
            )
        general = [
            first("my_status"),
            first("my_collection"),
            f"{first('character_info')} <名称或#ID>",
            first("group_stats"),
        ]
        if config["display"]["intimacy_rank_enabled"]:
            general.extend([first("rank_intimacy"), first("rank_my_intimacy")])
        if config["display"]["activity_rank_enabled"]:
            general.append(first("rank_activity"))
        general.append(f"{first('query_affection')} @群友")
        if any(
            config["modes"][mode]["gift_enabled"]
            and config["modes"][mode]["gift_mode"] == "confirm"
            for mode in _MODE_LABELS
        ):
            general.extend(
                [first("gift_accept"), first("gift_reject"), first("gift_cancel")]
            )
        groups.append({"label": "通用", "commands": general})
        title = render_message(config, "titles", "help")
        notes = [
            "<参数> 必填、[参数] 可选；@ 需要在 QQ 中选择成员。",
            f"每天 {config['reset']['time']}（{config['reset']['timezone']}）重置。",
        ]
        lines = [title]
        lines.extend(
            f"【{group['label']}】" + " · ".join(group["commands"]) for group in groups
        )
        lines.extend(notes)
        return await self._feature_reply(
            config,
            "help",
            "help",
            "help.html",
            lines,
            {"title": title, "groups": groups, "notes": notes},
            cached=True,
        )

    async def my_collection(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        actor_name = names.get(values["actor_id"], "群友")
        argument = values["command"].argument
        modes = tuple(
            mode
            for mode in ("wife", "husband")
            if config["modes"][mode]["enabled"]
            and (argument is None or _MODE_LABELS[mode] == argument)
        )
        if not modes:
            return render_message(config, "errors", "mode_closed")
        groups = await asyncio.to_thread(
            self.status.collection,
            scope_id=values["scope_id"],
            user_id=values["actor_id"],
            config=config,
            modes=modes,
        )
        title = render_message(config, "titles", "collection", name=actor_name)
        lines = [title]
        media_root = (self.storage.database_path.parent / "media").resolve()
        card_groups = []
        for group in groups:
            label = render_message(config, "titles", group["mode"])
            lines.append(f"【{label}】已收集 {group['collected']}/{group['total']}")
            entries = group["entries"]
            lines.append(
                "、".join(f"{entry['name']}×{entry['times']}" for entry in entries)
                or render_message(config, "titles", "empty")
            )
            thumbnails = []
            for entry in entries[:_COLLECTION_THUMBNAILS]:
                image = None
                if entry["image"]:
                    candidate = (media_root / entry["image"]).resolve()
                    if media_root in candidate.parents:
                        image = candidate
                thumbnails.append(
                    {
                        "name": entry["name"],
                        "times": entry["times"],
                        "image": await asyncio.to_thread(data_uri, image, 200),
                    }
                )
            card_groups.append(
                {
                    "label": label,
                    "collected": group["collected"],
                    "total": group["total"],
                    "percent": round(100 * group["collected"] / group["total"])
                    if group["total"]
                    else 0,
                    "entries": thumbnails,
                    "more": max(0, len(entries) - _COLLECTION_THUMBNAILS),
                }
            )
        return await self._feature_reply(
            config,
            "my_collection",
            "collection",
            "collection.html",
            lines,
            {
                "title": title,
                "groups": card_groups,
                "empty_text": render_message(config, "titles", "empty"),
            },
        )

    async def character_info(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        candidates = await asyncio.to_thread(self.status.character_candidates, config)
        matches = rules.match_candidates(candidates, values["command"].argument)
        if not matches:
            return render_message(
                config, "errors", "character_missing", name=values["command"].argument
            )
        if len(matches) > 1:
            return render_message(
                config,
                "errors",
                "character_ambiguous",
                name=values["command"].argument,
                candidates=rules.candidate_choices(matches),
            )
        info = await asyncio.to_thread(
            self.status.character_info,
            scope_id=values["scope_id"],
            period_id=values["period_id"],
            character_id=matches[0].subject_id,
        )
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        holders = [
            f"{names.get(item['owner_id'], '群友')}"
            f"（{render_message(config, 'titles', item['mode'])}）"
            for item in info["holders"]
        ]
        title = render_message(config, "titles", "character_info", name=info["name"])
        facts = [
            {"label": "ID", "value": f"#{info['id']}"},
            {"label": "别名", "value": "、".join(info["aliases"]) or "无"},
            {"label": "角色池", "value": "、".join(info["pools"]) or "未加入"},
            {"label": "当前持有", "value": "、".join(holders) or "无人持有"},
            {"label": "本群结缘", "value": f"{info['times']} 次"},
            {"label": "收集人数", "value": f"{info['collectors']} 人"},
        ]
        lines = [title, *(f"{item['label']}：{item['value']}" for item in facts)]
        media_root = (self.storage.database_path.parent / "media").resolve()
        image = None
        if info["image"]:
            candidate = (media_root / info["image"]).resolve()
            if media_root in candidate.parents:
                image = candidate
        return await self._feature_reply(
            config,
            "character_info",
            "collection",
            "character.html",
            lines,
            {
                "title": title,
                "name": info["name"],
                "image": await asyncio.to_thread(data_uri, image, 720),
                "facts": facts,
            },
        )

    async def group_stats(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        days = int(config["statistics"]["activity_window_days"])
        stats = await asyncio.to_thread(
            self.status.group_stats,
            scope_id=values["scope_id"],
            period_id=values["period_id"],
            since=values["now"] - days * 86400,
        )
        names = MESSAGE_CONTEXT.get().get("member_names", {})
        group_name = values.get("group_name") or "本群"
        title = render_message(
            config, "titles", "group_stats", group_name=group_name, days=days
        )
        counts = stats["counts"]
        tiles = [
            {"label": f"近 {days} 天抽取", "value": counts["draws"]},
            {
                "label": "抢夺成功 / 尝试",
                "value": f"{counts['steal_success']} / {counts['steal_attempts']}",
            },
            {
                "label": "强娶成功 / 尝试",
                "value": f"{counts['force_success']} / {counts['force_attempts']}",
            },
            {"label": "本周期关系", "value": stats["active"]},
        ]
        sections = [
            {
                "title": "最受欢迎的角色（累计）",
                "rows": [
                    {"name": row["name"], "value": f"{row['times']} 次"}
                    for row in stats["characters"]
                ],
            },
            {
                "title": f"近 {days} 天抽取最多",
                "rows": [
                    {
                        "name": names.get(row["user_id"], "群友"),
                        "value": f"{row['times']} 次",
                    }
                    for row in stats["drawers"]
                ],
            },
            {
                "title": "亲密度最高的组合",
                "rows": [
                    {
                        "name": f"{names.get(row['first'], '群友')} × "
                        f"{names.get(row['second'], '群友')}",
                        "value": str(row["score"]),
                    }
                    for row in stats["pairs"]
                    if row["first"] in names and row["second"] in names
                ],
            },
        ]
        empty = render_message(config, "titles", "empty")
        lines = [title]
        lines.extend(f"{item['label']}：{item['value']}" for item in tiles)
        for section in sections:
            lines.append(f"【{section['title']}】")
            lines.extend(
                f"{index}. {row['name']} · {row['value']}"
                for index, row in enumerate(section["rows"], 1)
            )
            if not section["rows"]:
                lines.append(empty)
        return await self._feature_reply(
            config,
            "group_stats",
            "rank",
            "stats.html",
            lines,
            {"title": title, "tiles": tiles, "sections": sections, "empty_text": empty},
        )

    async def _feature_reply(
        self,
        config: Mapping[str, Any],
        key: str,
        kind: str,
        template: str,
        lines: list[str],
        data: Mapping[str, Any],
        *,
        cached: bool = False,
    ) -> str | RuntimeReply:
        text = render_message(config, "results", key, lines="\n".join(lines))
        if not text.strip():
            return text
        visible = text.replace(IMAGE_TOKEN, "")
        if (
            IMAGE_TOKEN not in text
            or render_mode(kind) != "card"
            or not self.cards.available
        ):
            return RuntimeReply(
                MessageText(
                    visible if visible.strip() else "\n".join(lines),
                    quote_source=getattr(text, "quote_source", False),
                ),
                fallback_text="\n".join(lines),
            )
        try:
            if cached:
                path: Path = await self.cards.render_cached(
                    template,
                    data,
                    self.storage.database_path.parent / "cache" / "cards",
                )
            else:
                path = await self.cards.render_template(template, data)
        except Exception:  # noqa: BLE001 - 渲染降级为文字
            logger.warning(
                "今日姻缘：%s 卡片渲染失败，降级为文字。", key, exc_info=True
            )
            return RuntimeReply("\n".join(lines), fallback_text="\n".join(lines))
        # 缓存的卡片发送后保留，其余发送后删除。
        cleanup = () if cached else (path,)
        return RuntimeReply(text, (path,), cleanup, fallback_text="\n".join(lines))
