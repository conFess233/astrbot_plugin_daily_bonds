"""关系列表、亲密度/活跃度排行与好感度查询。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..services.message_templates import (
    IMAGE_TOKEN,
    render_message,
)
from ..services.rendering import CardRow
from .replies import RuntimeReply


class ListingMixin:
    """为 CommandRuntime 提供列表与排行查询。"""

    def _rank_data(self, values: Mapping[str, Any]):
        config = values["config"]
        intimacy = values["command"].action == "rank_intimacy"
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
        names = self._member_names(values["scope_id"])
        global_directed = (
            intimacy and config["display"]["intimacy_rank_mode"] == "group_directed"
        )
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

    def _member_names(self, scope_id: int) -> dict[str, str]:
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=?",
                    (scope_id,),
                )
                .fetchall()
            )
        return {
            str(row["user_id"]): str(row["card"] or row["nickname"] or "群友")
            for row in rows
        }

    async def _rank(self, values: Mapping[str, Any]) -> str | RuntimeReply:
        config = values["config"]
        command = values["command"]
        result = await asyncio.to_thread(self._rank_data, values)
        if isinstance(result, str):
            return result
        ranked, title, names, global_directed = result

        def display_name(user_id: str, maximum: int = 12) -> str:
            name = " ".join(names.get(user_id, "群友").split())
            return name if len(name) <= maximum else name[: maximum - 1] + "…"

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
        personal = key == "rank_intimacy" and not global_directed
        if personal:
            ids.add(values["actor_id"])
        group_id = values.get("group_id", "") if key == "rank_activity" else ""
        avatars = await self._avatars(
            list(ids), config, group_ids=[group_id] if group_id else ()
        )
        lines, cards = [], []
        for index, (source, target, score, days) in enumerate(
            selected, (page - 1) * page_size + 1 if pagination else 1
        ):
            if key == "rank_intimacy":
                subject = (
                    f"{names.get(source, '群友')} → {names.get(target, '群友')}"
                    if global_directed
                    else names.get(target, "群友")
                )
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
                    other_avatar_path=avatars.get(target) if global_directed else None,
                    other_avatar_user_id=target if global_directed else "",
                    pair_names=(display_name(source), display_name(target))
                    if global_directed
                    else None,
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
            "max_height": config["display"]["rank_max_height"],
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
        return await self._reply_with_card(
            text,
            image_title if personal else title,
            cards,
            fallback_text=fallback,
            merge_long=True,
            chunk_chars=config["resources"]["text_chunk_chars"],
            **options,
        )

    async def _affection(self, values: Mapping[str, Any]) -> str | RuntimeReply:
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
        names = await asyncio.to_thread(self._member_names, values["scope_id"])
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
        avatars = await self._avatars([actor, target], config)
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
        return await self._reply_with_card(
            text, title, [card], fallback_text=f"{title}\n" + "\n".join(labels)
        )

    def _relationship_rows(self, scope_id: int, period_id: str, mode: str):
        with self.storage._lock:
            rows = (
                self.storage._connection()
                .execute(
                    """SELECT r.mode,r.owner_id,r.subject_id,r.slot_kind,r.acquisition_kind,r.source_owner_id,s.id AS snapshot_id,s.name,s.subject_kind,
                          (SELECT GROUP_CONCAT(mb.relative_path,'|') FROM snapshot_images si
                           JOIN media_blobs mb ON mb.hash=si.media_hash WHERE si.snapshot_id=s.id) AS image_paths
                   FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
                   WHERE r.scope_id=? AND r.period_id=? AND r.mode=? AND r.state='active' """
                    "ORDER BY r.mode,r.owner_id,r.acquired_at,r.id",
                    (scope_id, period_id, mode),
                )
                .fetchall()
            )
            members = {
                str(row["user_id"]): str(row["card"] or row["nickname"] or "群友")
                for row in self.storage._connection()
                .execute(
                    "SELECT user_id,nickname,card FROM members WHERE scope_id=?",
                    (scope_id,),
                )
                .fetchall()
            }
        rows = [dict(row) for row in rows]
        for row in rows:
            if row["subject_kind"] == "member":
                row["name"] = members.get(str(row["subject_id"]), "群友")
        return rows, members

    async def _list_relationships(
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
            return await self._reply_with_card(
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
            if self.renderer.has_cjk_font and IMAGE_TOKEN in text
            else {}
        )
        card_rows = [
            CardRow(
                primary=f"{members.get(str(row['owner_id']), '群友')} → {row['name']}",
                secondary=row["source_label"],
                source_label_parts=row["source_parts"],
                source_avatar_path=avatars.get(str(row.get("source_owner_id") or "")),
                source_user_id=str(row.get("source_owner_id") or ""),
                image_path=self._snapshot_image_path(row["image_paths"])
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
                show_user_ids=False,
            )
            for row in selected_rows
        ]
        return await self._reply_with_card(text, title, card_rows)
