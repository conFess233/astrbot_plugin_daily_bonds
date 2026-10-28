"""把 0.1.x（schema 1）的配置转换为 0.2.0（schema 2）。

同一转换用于三处：数据库迁移 014 中的全局与群级设置、宿主原生配置页遗留的
`settings`，以及恢复旧版备份。转换只处理改名、合并和删除；新增字段由默认值补齐。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_DEFAULTS_PATH = (
    Path(__file__).resolve().parents[1] / "resources" / "default-config.json"
)

# 0.1.x 曾经的默认文案；升级时若仍是旧默认值，替换为当前默认值，自定义文案保留。
LEGACY_MESSAGES = {
    "titles.rank_activity": "近 {days} 天活跃度排行",
    "titles.normal_slot": "普通",
    "titles.steal_slot": "抢夺",
    "results.draw_wife": "娶到老婆：{name}{image}",
    "results.draw_husband": "娶到老公：{name}{image}",
    "results.draw_member": "娶到群友：{name}{image}",
    "results.designated_wife": "指定老婆：{name}{image}",
    "results.designated_husband": "指定老公：{name}{image}",
    "results.stolen": "抢夺成功",
    "results.steal_failed": "抢夺失败；配偶保留原关系",
    "results.divorced": "关系已解除，你获得一次补抽资格",
    "results.gifted": "赠送完成",
    "results.invite_created": "赠送邀请已创建，编号 {invite_id}；接收者可发送“{accept_keyword} {invite_id}”。",
    "results.gift_accepted": "赠送已接受",
    "results.invite_rejected": "已拒绝赠送",
    "results.invite_cancelled": "已取消赠送",
    "results.admin_set_wife": "设置今日老婆：{name}{image}",
    "notifications.member_redraw": "{name}（持有人 {owner_id} 获得补抽资格）",
    "notifications.owner_ineligible": "{name}（持有人 {owner_id} 已失去资格）",
}
_REMOVED_RESOURCES = {
    "font_id",
    "render_concurrency",
    "render_max_height",
    "send_interval_seconds",
    "render_width",
}
_QUOTABLE = {"results", "errors"}
# 单一容量模型下不会再出现的结果文案。
_REMOVED_MESSAGES = {"results.steal_slot_full", "results.designated_slot_full"}


def _defaults() -> dict[str, Any]:
    return json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))


def is_legacy(value: Mapping[str, Any]) -> bool:
    """判断配置是否仍是 schema 1 的结构。"""

    if value.get("schema_version") == 1:
        return True
    if "reply_enabled" in value or "reply_quote" in value:
        return True
    messages = value.get("messages")
    if isinstance(messages, Mapping):
        for section in _QUOTABLE | {"notifications"}:
            entries = messages.get(section)
            if isinstance(entries, Mapping) and any(
                isinstance(item, str) for item in entries.values()
            ):
                return True
    modes = value.get("modes")
    if isinstance(modes, Mapping):
        for mode in modes.values():
            if isinstance(mode, Mapping) and (
                "steal_slot_capacity" in mode or "designated_capacity" in mode
            ):
                return True
    resources = value.get("resources")
    return isinstance(resources, Mapping) and bool(_REMOVED_RESOURCES & set(resources))


def upgrade_config(
    value: Mapping[str, Any], *, sparse: bool
) -> tuple[dict[str, Any], list[str]]:
    """返回 schema 2 配置与迁移说明；`sparse` 表示群级稀疏覆盖。"""

    result = copy.deepcopy(dict(value))
    notes: list[str] = []
    defaults = _defaults()

    resources = result.get("resources")
    if isinstance(resources, dict):
        for key in sorted(_REMOVED_RESOURCES & set(resources)):
            resources.pop(key)
            if key == "font_id":
                notes.append(
                    "移除 resources.font_id：卡片改由 t2i 渲染，不再选择本地字体。"
                )
        if not resources:
            result.pop("resources")
    display = result.get("display")
    if isinstance(display, dict) and display.pop("rank_max_height", None) is not None:
        notes.append(
            "移除 display.rank_max_height：卡片按 display.rank_merge_rows 分图。"
        )
        if not display:
            result.pop("display")
    weights = result.get("weights")
    if isinstance(weights, dict):
        weights.pop("activity_full_messages", None)
        if not weights:
            result.pop("weights")

    modes = result.get("modes")
    if isinstance(modes, dict):
        for mode, current in modes.items():
            if not isinstance(current, dict):
                continue
            if "steal_slot_capacity" in current:
                slots = current.pop("steal_slot_capacity")
                notes.append(
                    f"modes.{mode}.steal_slot_capacity={slots} 已移除：所有关系共用持有上限。"
                )
            if "designated_capacity" in current:
                current["designated_limit"] = current.pop("designated_capacity")
                notes.append(
                    f"modes.{mode}.designated_capacity 改名为 designated_limit"
                    f"（{current['designated_limit']}）。"
                )

    messages = result.pop("messages", {})
    enabled = result.pop("reply_enabled", {})
    quote = result.pop("reply_quote", {})
    merged: dict[str, Any] = {}
    sections = set(messages) | set(enabled) | set(quote)
    for section in sorted(sections):
        texts = messages.get(section, {}) if isinstance(messages, Mapping) else {}
        if section == "titles":
            titles = {
                key: _current_default(defaults, section, key, text)
                for key, text in texts.items()
            }
            if titles:
                merged["titles"] = titles
            continue
        switches = enabled.get(section, {}) if isinstance(enabled, Mapping) else {}
        quotes = quote.get(section, {}) if isinstance(quote, Mapping) else {}
        entries: dict[str, Any] = {}
        for key in sorted(set(texts) | set(switches) | set(quotes)):
            if f"{section}.{key}" in _REMOVED_MESSAGES:
                continue
            current = texts.get(key)
            if isinstance(current, Mapping):
                # 已是新结构（重复执行时保持幂等）
                entries[key] = dict(current)
                continue
            entry: dict[str, Any] = {}
            if isinstance(current, str):
                entry["text"] = _current_default(defaults, section, key, current)
            if key in switches:
                entry["enabled"] = switches[key]
            if section in _QUOTABLE and key in quotes:
                entry["quote"] = quotes[key]
            if entry:
                entries[key] = entry
        if entries:
            merged[section] = entries
    if merged:
        result["messages"] = merged
    if (
        enabled
        or quote
        or any(
            isinstance(item, str)
            for section, entries in (messages.items() if messages else ())
            if section != "titles"
            for item in entries.values()
        )
    ):
        notes.append("文案、启用与引用合并为每条文案一个对象。")

    if not sparse or "schema_version" in result:
        result["schema_version"] = SCHEMA_VERSION
    return result, notes


def _current_default(
    defaults: Mapping[str, Any], section: str, key: str, text: str
) -> str:
    if LEGACY_MESSAGES.get(f"{section}.{key}") != text:
        return text
    current = defaults["messages"].get(section, {}).get(key)
    if isinstance(current, Mapping):
        return str(current["text"])
    return str(current) if current is not None else text
