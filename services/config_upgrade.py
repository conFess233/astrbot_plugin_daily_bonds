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
    Path(__file__).resolve().parents[1]
    / "resources"
    / "defaults"
    / "default-config.json"
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
    # 0.1.13 默认文案：0.2.0 补充了重置时间与下一步提示。
    "results.capacity_full": "持有名额已满：{names}",
    "results.relationship_locked": "抢夺或指定槽位关系不可主动解除或赠送。",
    "results.steal_slot_locked": "对方的抢夺或指定槽位关系不可抢夺。",
    "results.capacity_full_single_wife": "你今日已有老婆啦，请明日再来。",
    "results.capacity_full_single_husband": "你今日已有老公啦，请明日再来。",
    "results.no_draw_quota": "今天的普通抽取额度已用完",
    "results.no_candidate": "当前没有可抽取对象",
    "results.steal_cooldown": "抢夺冷却中",
    "results.steal_limit": "本周期抢夺尝试次数已用完",
    "results.divorce_limit": "本周期解除关系次数已用完",
    "results.relationship_not_found": "没有找到可处理的关系",
    "results.no_designated_quota": "本周期指定次数已用完。",
    "results.designated_relationship_locked": "指定槽位关系不可抢夺、赠送或主动解除。",
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
_REMOVED_MESSAGES = {
    "results.steal_slot_full",
    "results.designated_slot_full",
    "results.designated_drawn",
    "results.designated_relationship_locked",
    "notifications.restart_invalidated",
    "notifications.member_redraw",
}
# 0.2.2 的默认文案：删除补抽并统一术语后改写；自定义文案保留。
SUPERSEDED_MESSAGES: dict[str, str | tuple[str, ...]] = {
    "results.capacity_full": "持有名额已满：{names}。可以先发送“离婚”解除一段普通关系。",
    "results.relationship_locked": (
        "抢来的和指定的关系当天受保护，不能主动解除或赠送。",
        "抢来的和指定的关系本周期受保护，不能主动解除或赠送。",
    ),
    "results.steal_slot_locked": (
        "对方这段关系是抢来的或指定的，当天受保护，不能抢夺。",
        "对方这段关系是抢来的或指定的，本周期受保护，不能抢夺。",
    ),
    "results.capacity_full_single_wife": "你今日已有老婆{names}啦，{reset_at} 重置后再来吧。",
    "results.capacity_full_single_husband": "你今日已有老公{names}啦，{reset_at} 重置后再来吧。",
    "results.no_draw_quota": "今天的普通抽取次数已用完，{reset_at} 重置；离婚可获得一次补抽资格。",
    "results.divorced": "{actor_name}与{subject_name}的关系已解除，获得一次补抽资格。",
    "results.stolen": "{actor_name}从{owner_name}处抢到了{subject_name}。",
    "results.invite_expired": "赠送邀请已过期",
    "results.invite_invalidated": "赠送邀请已失效",
    "results.invite_not_owned": "你无权处理这条邀请",
    "results.target_protected": (
        "该持有者本周期已受抢夺保护",
        "该持有者本周期已受抢夺保护。",
    ),
    "results.invalid_target": (
        "目标无效",
        "目标无效。",
    ),
    "results.no_target_relationship": (
        "目标没有可抢夺的关系",
        "目标没有可抢夺的关系。",
    ),
    "results.mode_disabled": "这个玩法当前已关闭",
    "results.invalid_probability": "抢夺概率配置无效",
    "results.invalid_timeout": "赠送超时配置无效",
    "results.invite_already_pending": "该关系已有待处理赠送邀请，编号 {invite_id}",
    "results.invite_limit": "待处理赠送邀请已达上限 {limit}",
    "results.invite_invalid": "赠送邀请已无效",
    "results.already_held": "你今天已持有 {name}。",
    "errors.steal_relation_ambiguous": "该持有者没有唯一匹配的配偶；请使用准确名称或 #ID。",
    "errors.gift_relation_ambiguous": "你没有唯一匹配的配偶；请使用准确名称或 #ID。",
    "titles.list_member": "群友配偶列表",
    "titles.rank_group_intimacy": "{群名}的亲密度排行",
    "titles.rank_activity": "{群名}的活跃度排行",
    "titles.query_affection": "{name}与{target_name}的好感度",
    # 0.3.0 的默认文案：误导或缺少引导，0.3.1 改写。
    "results.relationship_not_found": "没有找到可处理的关系；发送“我的姻缘”查看当前关系。",
    "errors.divorce_relation_missing": "没有找到匹配的当前关系。",
    "errors.steal_relation_missing": "该持有者没有匹配的关系，请检查名称或 #ID。",
}


# 以前的默认配置值；已保存配置里仍是旧默认值时换成当前默认值，自定义值保留。
SUPERSEDED_VALUES: dict[str, tuple[Any, ...]] = {
    # 0.3.0：每批 50 条在 5 秒超时下经常作废，降为 20 条。
    "sentiment.batch_max_messages": (50,),
}


def refresh_superseded_values(value: dict[str, Any]) -> bool:
    """就地把仍是旧默认值的配置项换成当前默认值；返回是否有改动。"""

    defaults = _defaults()
    changed = False
    for path, olds in SUPERSEDED_VALUES.items():
        section, key = path.split(".", 1)
        current = value.get(section)
        if not isinstance(current, dict) or key not in current:
            continue
        default = defaults[section][key]
        if current[key] in olds and current[key] != default:
            current[key] = default
            changed = True
    return changed


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
    if isinstance(display, dict):
        if display.pop("rank_max_height", None) is not None:
            notes.append(
                "移除 display.rank_max_height：卡片按 display.rank_merge_rows 分图。"
            )
        if display.pop("intimacy_rank_mode", None) is not None:
            notes.append(
                "移除 display.intimacy_rank_mode：亲密度排行固定展示全群，"
                "个人排行改用“我的亲密度排行”。"
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


def refresh_superseded_messages(value: dict[str, Any]) -> bool:
    """就地删除废弃文案并替换仍是旧默认值的文案；返回是否有改动。"""

    messages = value.get("messages")
    if not isinstance(messages, dict):
        return False
    defaults = _defaults()["messages"]
    changed = False
    for path in _REMOVED_MESSAGES:
        section, key = path.split(".", 1)
        entries = messages.get(section)
        if isinstance(entries, dict) and key in entries:
            del entries[key]
            changed = True
    for path, old in SUPERSEDED_MESSAGES.items():
        section, key = path.split(".", 1)
        entries = messages.get(section)
        if not isinstance(entries, dict):
            continue
        entry = entries.get(key)
        current = defaults[section][key]
        olds = (old,) if isinstance(old, str) else old
        if isinstance(entry, dict) and entry.get("text") in olds:
            if entry["text"] != current["text"]:
                entry["text"] = current["text"]
                changed = True
        elif isinstance(entry, str) and entry in olds:
            entries[key] = current
            changed = True
    for section in [name for name, entries in messages.items() if entries == {}]:
        del messages[section]
    if messages == {}:
        del value["messages"]
    return changed
