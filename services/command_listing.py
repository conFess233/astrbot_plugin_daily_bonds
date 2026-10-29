"""关系列表、亲密度/活跃度排行与好感度查询。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from .avatar_cache import AvatarCache
from .cards import PAIR_LIMIT, CardRow, CardService, render_mode
from .command_replies import ReplyBuilder, RuntimeReply
from .message_templates import (
    IMAGE_TOKEN,
    render_message,
)
from .statistics import StatisticsService
from .storage import SQLiteStorage


class ListingHandler:
    """关系列表、亲密度与活跃度排行、好感度查询。"""

    def __init__(
        self,
        storage: SQLiteStorage,
        statistics: StatisticsService,
        cards: CardService,
        avatar_cache: AvatarCache,
        replies: ReplyBuilder,
    ) -> None:
        self.storage = storage
        self.statistics = statistics
        self.cards = cards
        self.avatar_cache = avatar_cache
        self.replies = replies

    def _rank_data(self, values: Mapping[str, Any]):
        config = values["config"]
        action = values["command"].action
        intimacy = action in {"rank_intimacy", "rank_my_intimacy"}
        if not config["display"][
            "intimacy_rank_enabled" if intimacy else "activity_rank_enabled"
        ]:
            return render_message(
                config,
                "errors",
                "rank_intimacy_disabled" if intimacy else "rank_activity_disabled",
            )
        if not intimacy and not config["statistics"]["activity_enabled"]:
            return render_message(config, "errors", "activity_disabled")
        names = self.replies.member_names(values["scope_id"])
        # 亲密度排行展示全群（双向合并），我的亲密度排行只列出触发者给群友的分值。
        global_directed = action == "rank_intimacy"
        if intimacy:
            rows = self.statistics.intimacy_rows(
                values["scope_id"], eligible_user_ids=values["eligible"]
            )
            ranked = [
                (str(row["source_id"]), str(row["target_id"]), int(row["score"]), 0)
                for row in rows
                if global_directed or row["source_id"] == values["actor_id"]
            ]
            title = render_message(
                config,
                "titles",
                "rank_group_intimacy" if global_directed else "rank_intimacy",
                name=names.get(values["actor_id"], "群友"),
                group_name=values.get("group_name", "本群"),
            )
        else:
            rows = self.statistics.activity_rows(
                values["scope_id"],
                now=values["now"],
                window_days=config["statistics"]["activity_window_days"],
                timezone=config["reset"]["timezone"],
                eligible_user_ids=values["eligible"],
            )
            ranked = [
                (row["user_id"], "", row["messages"], row["active_days"])
                for row in rows
            ]
            title = render_message(
                config,
                "titles",
                "rank_activity",
                days=config["statistics"]["activity_window_days"],
                group_name=values.get("group_name", "本群"),
            )
        return ranked, title, names, global_directed

    async def rank(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        command = values["command"]
        result = await asyncio.to_thread(self._rank_data, values)
        if isinstance(result, str):
            return result
        ranked, title, names, global_directed = result

        def display_name(user_id: str, maximum: int = 12) -> str:
            name = " ".join(names.get(user_id, "群友").split())
            return name if len(name) <= maximum else name[: maximum - 1] + "…"

        if global_directed:
            return await self._pair_rank(values, ranked, title, display_name)
        page_size, page = config["display"]["page_size"], command.page or 1
        pages = max(1, (len(ranked) + page_size - 1) // page_size)
        pagination = config["display"]["pagination_enabled"]
        if pagination and page > pages:
            return render_message(config, "errors", "page_out_of_range", pages=pages)
        selected = (
            ranked[(page - 1) * page_size : page * page_size] if pagination else ranked
        )
        heading = (
            render_message(
                config, "titles", "page", title=title, page=page, pages=pages
            )
            if pagination
            else title
        )
        key = command.action
        summary = render_message(
            config, "titles", "rank_summary", title=heading, count=len(ranked)
        )
        ids = {source for source, _, _, _ in selected} | {
            target for _, target, _, _ in selected if target
        }
        personal = key == "rank_my_intimacy"
        if personal:
            ids.add(values["actor_id"])
        group_id = values.get("group_id", "") if key == "rank_activity" else ""
        avatars = await self.replies.avatars(
            list(ids), config, group_ids=[group_id] if group_id else ()
        )
        lines, cards = [], []
        for index, (source, target, score, days) in enumerate(
            selected, (page - 1) * page_size + 1 if pagination else 1
        ):
            if personal:
                subject = names.get(target, "群友")
                detail = render_message(config, "titles", "intimacy_value", score=score)
            else:
                subject = names.get(source, "群友")
                detail = render_message(
                    config, "titles", "activity_value", messages=score, active_days=days
                )
            lines.append(f"{index}. {subject} · {detail}")
            cards.append(
                CardRow(
                    display_name(target if personal else source),
                    detail,
                    avatar_path=avatars.get(target if personal else source),
                    avatar_user_id=target if personal else source,
                    pair_rank=index,
                    score=score,
                    badge=f"活跃 {days} 天" if key == "rank_activity" else "",
                    highlight=(target if personal else source) == values["actor_id"],
                )
            )
        lines_text = "\n".join(lines) or render_message(config, "titles", "empty")
        fallback = f"{summary}\n{lines_text}"
        text = render_message(
            config,
            "results",
            key,
            title=heading,
            count=len(ranked),
            lines=lines_text,
            group_name=values.get("group_name", "本群"),
            days=config["statistics"]["activity_window_days"],
        )
        options = {
            "ranking": True,
            "subtitle": f"共 {len(ranked)} 条记录"
            + (f" · 第 {page}/{pages} 页" if pagination else ""),
            "range_label": f"统计范围：{config['statistics']['activity_window_days']} 天"
            if key == "rank_activity"
            else "统计范围：累计",
            "max_rows": config["display"]["rank_merge_rows"],
            "empty_text": render_message(config, "titles", "empty"),
        }
        if personal:
            image_title = render_message(
                config,
                "titles",
                "rank_intimacy",
                name=display_name(values["actor_id"]),
                group_name=values.get("group_name", "本群"),
            )
            options.update(
                header_avatar_path=avatars.get(values["actor_id"]),
                header_user_id=values["actor_id"],
            )
        elif group_id:
            options["header_avatar_path"] = avatars.get(f"group-{group_id}")
        return await self.replies.reply_with_card(
            text,
            image_title if personal else title,
            cards,
            fallback_text=fallback,
            merge_long=True,
            chunk_chars=config["resources"]["text_chunk_chars"],
            **options,
        )

    async def _pair_rank(
        self,
        values: Mapping[str, Any],
        ranked: list[tuple[str, str, int, int]],
        title: str,
        display_name: Callable[..., str],
    ) -> str | RuntimeReply:
        """全群亲密度：A→B 与 B→A 合并为一对，按双向总分排序，只展示前 50 对。"""

        config = values["config"]
        if (values["command"].page or 1) > 1:
            return render_message(config, "errors", "page_out_of_range", pages=1)
        directed = {(source, target): score for source, target, score, _ in ranked}
        merged = []
        for left, right in {tuple(sorted(key)) for key in directed}:
            forward = directed.get((left, right), 0)
            reverse = directed.get((right, left), 0)
            # 付出更多的一方放在左边，比例条以主色开头。
            if reverse > forward:
                left, right, forward, reverse = right, left, reverse, forward
            merged.append((left, right, forward, reverse))
        merged.sort(key=lambda pair: (-(pair[2] + pair[3]), -pair[2], pair[0], pair[1]))
        shown = merged[:PAIR_LIMIT]
        avatars = await self.replies.avatars(
            list({user for left, right, _, _ in shown for user in (left, right)}),
            config,
        )
        lines, cards = [], []
        for index, (left, right, forward, reverse) in enumerate(shown, 1):
            left_name, right_name = display_name(left, 16), display_name(right, 16)
            detail = render_message(
                config, "titles", "intimacy_value", score=forward + reverse
            )
            lines.append(
                f"{index}. {left_name} ⇄ {right_name} · {detail}（→{forward} / {reverse}←）"
            )
            cards.append(
                CardRow(
                    f"{left_name} ⇄ {right_name}",
                    detail,
                    avatar_path=avatars.get(left),
                    avatar_user_id=left,
                    other_avatar_path=avatars.get(right),
                    other_avatar_user_id=right,
                    pair_names=(left_name, right_name),
                    pair_scores=(forward, reverse),
                    pair_rank=index,
                    score=forward + reverse,
                    highlight=values.get("actor_id") in (left, right),
                )
            )
        summary = render_message(
            config, "titles", "rank_summary", title=title, count=len(merged)
        )
        lines_text = "\n".join(lines) or render_message(config, "titles", "empty")
        text = render_message(
            config,
            "results",
            "rank_intimacy",
            title=title,
            count=len(merged),
            lines=lines_text,
            group_name=values.get("group_name", "本群"),
        )
        subtitle = f"双向合并 · 共 {len(merged)} 对" + (
            f" · 显示前 {PAIR_LIMIT} 对" if len(merged) > PAIR_LIMIT else ""
        )
        return await self.replies.reply_with_card(
            text,
            title,
            cards,
            fallback_text=f"{summary}\n{lines_text}",
            merge_long=True,
            chunk_chars=config["resources"]["text_chunk_chars"],
            ranking=True,
            pairs=True,
            subtitle=subtitle,
            range_label="统计范围：累计",
            max_rows=config["display"]["rank_merge_rows"],
            empty_text=render_message(config, "titles", "empty"),
        )

    async def affection(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config, actor, target = (
            values["config"],
            values["actor_id"],
            values["command"].target_user_id,
        )
        if not target or target == actor or target not in values["eligible"]:
            return render_message(config, "errors", "affection_target_required")
        scores = await asyncio.to_thread(
            self.statistics.pair_intimacy, values["scope_id"], actor, target
        )
        names = await asyncio.to_thread(self.replies.member_names, values["scope_id"])
        name, target_name = names.get(actor, "群友"), names.get(target, "群友")
        title = render_message(
            config, "titles", "query_affection", name=name, target_name=target_name
        )
        forward, reverse = scores["forward_score"], scores["reverse_score"]
        labels = (
            render_message(
                config,
                "titles",
                "affection_value",
                name=name,
                target_name=target_name,
                score=forward,
            ),
            render_message(
                config,
                "titles",
                "affection_value",
                name=target_name,
                target_name=name,
                score=reverse,
            ),
        )
        text = render_message(
            config,
            "results",
            "query_affection",
            name=name,
            target_name=target_name,
            forward_score=forward,
            reverse_score=reverse,
        )
        avatars = await self.replies.avatars([actor, target], config)
        card = CardRow(
            "",
            pair_names=(name, target_name),
            pair_scores=(forward, reverse),
            pair_score_labels=labels,
            avatar_path=avatars.get(actor),
            other_avatar_path=avatars.get(target),
            avatar_user_id=actor,
            other_avatar_user_id=target,
        )
        return await self.replies.reply_with_card(
            text, title, [card], fallback_text=f"{title}\n" + "\n".join(labels)
        )

    def _relationship_rows(self, scope_id: int, period_id: str, mode: str):
        with self.storage.read() as db:
            rows = db.execute(
                """SELECT r.mode,r.owner_id,r.subject_id,r.slot_kind,r.acquisition_kind,r.source_owner_id,s.id AS snapshot_id,s.name,s.subject_kind,
                          (SELECT GROUP_CONCAT(mb.relative_path,'|') FROM snapshot_images si
                           JOIN media_blobs mb ON mb.hash=si.media_hash WHERE si.snapshot_id=s.id) AS image_paths
                   FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
                   WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.state='active' """
                "ORDER BY r.mode,r.owner_id,r.acquired_at,r.id",
                (scope_id, period_id, mode),
            ).fetchall()
            members = {
                str(row["user_id"]): str(row["card"] or row["nickname"] or "群友")
                for row in db.execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=?",
                    (scope_id,),
                ).fetchall()
            }
        rows = [dict(row) for row in rows]
        for row in rows:
            if row["subject_kind"] == "member":
                row["name"] = members.get(str(row["subject_id"]), "群友")
        return rows, members

    async def list_relationships(
        self,
        scope_id: int,
        period_id: str,
        mode: str,
        page: int | None,
        page_size: int,
        pagination_enabled: bool,
        resources: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> str | RuntimeReply:
        if config is None:
            config = (await asyncio.to_thread(self.storage.get_settings, scope_id))[0]
        if resources is None:
            resources = config["resources"]
        rows, members = await asyncio.to_thread(
            self._relationship_rows, scope_id, period_id, mode
        )
        for row in rows:
            kind = row.get("acquisition_kind", "draw")
            source_id = str(row.get("source_owner_id") or "")
            source_name = members.get(source_id, "群友")
            key = {"gift": "gift_slot", "steal": "steal_slot"}.get(kind, "normal_slot")
            if row.get("slot_kind") == "designated":
                key = "designated_slot"
            row["source_label"] = (
                "["
                + render_message(config, "titles", key, owner_name=source_name)
                + "]"
            )
            row["source_parts"] = None
            if kind in {"gift", "steal"}:
                marker = "\ue002source\ue002"
                marked = (
                    "[" + render_message(config, "titles", key, owner_name=marker) + "]"
                )
                before, separator, after = marked.partition(marker)
                if separator:
                    row["source_parts"] = (
                        before,
                        source_name,
                        after.replace(marker, source_name),
                    )
        entries = [
            f"{row['source_label']} {members.get(str(row['owner_id']), '群友')} → {row['name']}"
            for row in rows
        ]
        title = render_message(config, "titles", f"list_{mode}")
        if not entries:
            return await self.replies.reply_with_card(
                render_message(config, "errors", "list_empty", title=title), title, ()
            )
        if not pagination_enabled:
            selected = entries
            if page is not None:
                title += "（分页已关闭，返回全部）"
        else:
            pages = max(1, (len(entries) + page_size - 1) // page_size)
            selected_page = page or 1
            if selected_page > pages:
                return render_message(
                    config, "errors", "page_out_of_range", pages=pages
                )
            start = (selected_page - 1) * page_size
            selected = entries[start : start + page_size]
            title = render_message(
                config, "titles", "page", title=title, page=selected_page, pages=pages
            )
        text = render_message(
            config,
            "results",
            f"list_{mode}",
            title=title,
            count=len(entries),
            lines="\n".join(selected),
        )
        selected_rows = (
            rows if not pagination_enabled else rows[start : start + page_size]
        )
        avatar_ids = list(
            dict.fromkeys(
                user_id
                for row in selected_rows
                for user_id in (
                    [
                        str(row["owner_id"]),
                        str(row["subject_id"]),
                        str(row.get("source_owner_id") or ""),
                    ]
                    if mode == "member"
                    else [str(row["owner_id"]), str(row.get("source_owner_id") or "")]
                )
                if user_id
            )
        )
        avatars = (
            await self.avatar_cache.get_many(
                avatar_ids,
                ttl_seconds=resources["avatar_cache_ttl_seconds"],
                max_entries=resources["avatar_cache_max_entries"],
            )
            if self.cards.available
            and render_mode("list") == "card"
            and IMAGE_TOKEN in text
            else {}
        )
        card_rows = [
            CardRow(
                primary=f"{members.get(str(row['owner_id']), '群友')} → {row['name']}",
                secondary=row["source_label"],
                source_label_parts=row["source_parts"],
                source_avatar_path=avatars.get(str(row.get("source_owner_id") or "")),
                source_user_id=str(row.get("source_owner_id") or ""),
                image_path=self.replies.snapshot_image_path(row["image_paths"])
                if mode != "member"
                else None,
                avatar_path=avatars.get(str(row["owner_id"])),
                avatar_user_id=str(row["owner_id"]),
                other_avatar_path=avatars.get(str(row["subject_id"]))
                if mode == "member"
                else None,
                other_avatar_user_id=str(row["subject_id"]) if mode == "member" else "",
                pair_names=(
                    members.get(str(row["owner_id"]), "群友"),
                    members.get(str(row["subject_id"]), str(row["name"])),
                )
                if mode == "member"
                else None,
            )
            for row in selected_rows
        ]
        return await self.replies.reply_with_card(text, title, card_rows)
