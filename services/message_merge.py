"""回复文案的合并与拆分（0.4.0）：业务结果码不变，只调整可配置的文案条目。

- 合并：同一句话按场景拆成多条的，并成一条，场景差异用变量自动填入。渲染时
  旧键经 MESSAGE_ALIASES 改用合并后的条目，并补上对应变量的值。
- 拆分：一条文案对应不同场景的，拆成各自的条目（迁移时复制原自定义）。
- 系统提示：配置错误、未知结果等不再开放配置，使用固定文字。

迁移 022 调用 merge_messages 改写已保存的配置，并返回升级报告。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

# 合并后的条目 → 按优先级排列的来源 (旧路径, 自动填的变量)。几条自定义不同时
# 取排在前面（实际更常触发）的一条。
MERGES: dict[str, list[tuple[str, dict[str, str]]]] = {
    "errors.target_required": [
        ("errors.force_target_required", {}),
        ("errors.steal_target_required", {}),
        ("errors.gift_target_required", {}),
        ("errors.affection_target_required", {}),
    ],
    "errors.target_invalid": [
        ("errors.target_is_self", {"reason": "这是你自己"}),
        ("errors.target_is_bot", {"reason": "ta 是机器人"}),
        (
            "errors.target_ineligible",
            {"reason": "ta 暂时不能参与（可能被管理员排除、已退群，或插件还没记录到 ta）"},
        ),
        ("results.steal_own_partner", {"reason": "ta 娶的就是你，不用抢"}),
        ("results.gift_to_subject", {"reason": "不能把 ta 送给 ta 本人"}),
        ("results.invalid_target", {"reason": "这个指令不能对 ta 使用"}),
    ],
    "errors.feature_disabled": [
        ("errors.mode_closed", {"feature": "这个玩法"}),
        ("errors.force_disabled", {"feature": "强娶"}),
        ("errors.steal_disabled", {"feature": "抢夺"}),
        ("errors.gift_disabled", {"feature": "赠送"}),
        ("errors.divorce_disabled", {"feature": "离婚"}),
        ("results.designated_disabled", {"feature": "指定抽取"}),
        ("results.mode_disabled", {"feature": "赠送"}),
        ("errors.rank_intimacy_disabled", {"feature": "亲密度排行"}),
        ("errors.rank_activity_disabled", {"feature": "活跃度排行"}),
    ],
    "results.quota_used_up": [
        ("results.force_limit", {"action": "强娶"}),
        ("results.steal_limit", {"action": "抢夺"}),
        ("results.divorce_limit", {"action": "离婚"}),
        ("results.no_designated_quota", {"action": "指定"}),
    ],
    "results.capacity_full_single": [
        ("results.capacity_full_single_wife", {"role": "老婆"}),
        ("results.capacity_full_single_husband", {"role": "老公"}),
    ],
    "results.invite_invalid": [
        ("results.invite_expired", {}),
        ("results.invite_invalidated", {}),
        ("results.invite_invalid", {}),
    ],
    "errors.relation_ambiguous": [
        ("errors.steal_relation_ambiguous", {}),
        ("errors.gift_relation_ambiguous", {}),
        ("errors.divorce_ambiguous", {}),
    ],
    "errors.character_ambiguous": [
        ("errors.designated_ambiguous", {}),
        ("errors.character_info_ambiguous", {}),
        ("errors.admin_set_ambiguous", {}),
    ],
    "errors.relation_missing": [
        ("errors.steal_relation_missing", {}),
        ("errors.gift_relation_missing", {}),
    ],
    "errors.character_missing": [
        ("errors.designated_missing", {}),
        ("errors.character_info_missing", {}),
        ("errors.admin_set_missing", {}),
    ],
    "results.draw_character": [
        ("results.draw_wife", {"role": "老婆"}),
        ("results.draw_husband", {"role": "老公"}),
    ],
    "results.designated": [
        ("results.designated_wife", {"role": "老婆"}),
        ("results.designated_husband", {"role": "老公"}),
    ],
}

# 必须列出候选项的条目：合并来的文案没有 {candidates} 时补在末尾。
_NEEDS_CANDIDATES = frozenset({"errors.relation_ambiguous", "errors.character_ambiguous"})

# 旧路径的其他说法：替换为变量时一并识别（例如“解除关系”也算离婚）。
_SYNONYMS = {"离婚": ("解除关系",), "指定抽取": ("指定",)}

# 拆分：旧条目复制到新的多个条目；之后各自修改。
SPLITS: dict[str, list[str]] = {
    "results.relationship_locked": ["results.divorce_locked", "results.gift_locked"],
}

# 不再开放配置的系统提示，使用固定文字。
SYSTEM_MESSAGES: dict[str, str] = {
    "results.invalid_probability": "成功率配置无效，请联系管理员检查设置。",
    "results.invalid_timeout": "赠送超时配置无效，请联系管理员检查设置。",
    "results.unknown_result": "操作未完成：{code}",
    "errors.operation_error": "{detail}",
    "errors.syntax_error": "{detail}",
}

# 0.3.2 的默认文案（被合并、拆分或移除的条目）；与之相同即视为没有自定义。
OLD_DEFAULTS: dict[str, str] = {
    "errors.steal_target_required": "请 @ 一位当前群内且允许参与的持有者。",
    "errors.force_target_required": "请 @ 一位当前群内且允许参与的其他成员。",
    "errors.gift_target_required": "请 @ 一位当前群内且允许参与的接收者。",
    "errors.affection_target_required": "请 @ 一位当前群内且允许参与的其他成员。",
    "errors.target_is_self": "不能对自己使用这个指令。",
    "errors.target_is_bot": "不能对机器人使用这个指令。",
    "errors.target_ineligible": "{target_name}暂时不能参与（可能被管理员排除、已退群，或插件还没记录到 ta），换个人试试。",
    "results.invalid_target": "不能对这个对象使用这个指令。",
    "results.steal_own_partner": "{owner_name}娶的就是你，不用抢啦。",
    "results.gift_to_subject": "不能把{recipient_name}送给 ta 本人。",
    "errors.mode_closed": "这个玩法当前已关闭。",
    "errors.force_disabled": "本群当前不允许强娶。",
    "errors.steal_disabled": "这个玩法当前不允许抢夺。",
    "errors.gift_disabled": "这个玩法当前不允许赠送。",
    "errors.divorce_disabled": "这个玩法当前不允许解除关系。",
    "results.designated_disabled": "指定抽取当前已关闭。",
    "results.mode_disabled": "这个玩法当前已关闭。",
    "errors.rank_intimacy_disabled": "亲密度排行当前已关闭。",
    "errors.rank_activity_disabled": "活跃度排行当前已关闭。",
    "results.force_limit": "本周期强娶次数已用完，{reset_at} 重置。",
    "results.steal_limit": "本周期抢夺次数已用完，{reset_at} 重置。",
    "results.divorce_limit": "本周期解除关系次数已用完，{reset_at} 重置。",
    "results.no_designated_quota": "本周期指定次数已用完，{reset_at} 重置。",
    "results.capacity_full_single_wife": "你本周期已有老婆{names}啦，{reset_at} 重置后再来吧。",
    "results.capacity_full_single_husband": "你本周期已有老公{names}啦，{reset_at} 重置后再来吧。",
    "results.invite_expired": "赠送邀请已过期。",
    "results.invite_invalidated": "赠送邀请已失效。",
    "results.invite_invalid": "赠送邀请已无效。",
    "errors.steal_relation_ambiguous": "该持有者有多段匹配的关系，请写明名称或 #ID：\n{candidates}",
    "errors.gift_relation_ambiguous": "你有多段可赠送的关系，请写明要送出哪一段：\n{candidates}",
    "errors.divorce_ambiguous": "找到多段匹配关系，请用“离婚老婆/离婚老公/离婚群友”指定玩法和名称或 ID：",
    "errors.designated_ambiguous": "匹配到多个角色，请使用明确 ID：\n{candidates}",
    "errors.character_info_ambiguous": "匹配到多个角色，请使用明确 ID：\n{candidates}",
    "errors.admin_set_ambiguous": "匹配到多个角色：{name}；请使用明确 ID：\n{candidates}",
    "errors.steal_relation_missing": "{target_name}没有叫这个名字的关系；不写名称时会随机抢一段。",
    "errors.gift_relation_missing": "你没有匹配的关系；发送“我的姻缘”查看当前关系。",
    "errors.designated_missing": "未找到当前角色池内可指定的角色；请使用精确名称、别名或 ID。",
    "errors.character_info_missing": "未找到本群角色池内的角色；请检查名称、别名或 ID。",
    "errors.admin_set_missing": "未找到可用的今日老婆角色：{name}",
    "results.draw_wife": "{actor_name}娶到老婆：{name}{image}",
    "results.draw_husband": "{actor_name}娶到老公：{name}{image}",
    "results.designated_wife": "{actor_name}指定老婆：{name}{image}",
    "results.designated_husband": "{actor_name}指定老公：{name}{image}",
    "results.relationship_locked": "这段关系是抢来、强娶、指定、别人送的或管理员设置的，本周期受保护，不能离婚或转送；{reset_at} 重置后恢复。",
    "results.invalid_probability": "抢夺概率配置无效。",
    "results.invalid_timeout": "赠送超时配置无效。",
    "results.unknown_result": "操作未完成：{code}",
    "errors.operation_error": "{detail}",
    "errors.syntax_error": "{detail}",
}

# 渲染时：旧 (分类, 键) → (合并后的分类, 键, 变量)。
MESSAGE_ALIASES: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {
    tuple(source.split(".", 1)): (*target.split(".", 1), values)  # type: ignore[misc]
    for target, sources in MERGES.items()
    for source, values in sources
    if source != target
}
# 结果码 RELATIONSHIP_LOCKED 在离婚中出现时（赠送由调用方改用 gift_locked）。
MESSAGE_ALIASES[("results", "relationship_locked")] = ("results", "divorce_locked", {})
REMOVED_PATHS = frozenset(
    {source for sources in MERGES.values() for source, _ in sources}
    - set(MERGES)
    | set(SPLITS)
    | set(SYSTEM_MESSAGES)
)


def _known_defaults(path: str, extra: Mapping[str, Any]) -> set[str]:
    """该路径曾经的默认文案（含更早版本），用于判断是否自定义过。"""

    olds = {OLD_DEFAULTS[path]} if path in OLD_DEFAULTS else set()
    for table in extra.values():
        value = table.get(path)
        if isinstance(value, str):
            olds.add(value)
        elif isinstance(value, tuple):
            olds.update(value)
    return olds


def _customized(entry: Any, path: str, history: Mapping[str, Any]) -> bool:
    if not isinstance(entry, Mapping):
        return False
    text_changed = "text" in entry and entry["text"] not in _known_defaults(
        path, history
    )
    return bool(
        text_changed
        or entry.get("alternatives")
        or entry.get("enabled") is False
        or entry.get("llm")
        or entry.get("llm_prompt")
    )


def _generalize(text: str, values: Mapping[str, str]) -> str:
    """把文案里写死的场景词换成变量，例如“老婆”→ {role}。"""

    for name, word in values.items():
        for candidate in (word, *_SYNONYMS.get(word, ())):
            if candidate and candidate in text:
                return text.replace(candidate, "{" + name + "}", 1)
    return text


def _with_candidates(text: str) -> str:
    if "{candidates}" in text or not text.strip():
        return text
    return text.rstrip().rstrip("：:。") + "：\n{candidates}"


def _same_after_generalize(entries: list[tuple[dict[str, Any], dict[str, str]]]) -> bool:
    texts = {
        tuple(
            _generalize(str(text), values)
            for text in [entry.get("text", ""), *entry.get("alternatives", [])]
        )
        for entry, values in entries
    }
    return len(texts) == 1


def _get(messages: Mapping[str, Any], path: str) -> Any:
    section, key = path.split(".", 1)
    entries = messages.get(section)
    return entries.get(key) if isinstance(entries, Mapping) else None


def _put(messages: dict[str, Any], path: str, entry: Any) -> None:
    section, key = path.split(".", 1)
    messages.setdefault(section, {})[key] = entry


def _drop(messages: dict[str, Any], path: str) -> None:
    section, key = path.split(".", 1)
    entries = messages.get(section)
    if isinstance(entries, dict):
        entries.pop(key, None)


def merge_messages(
    value: dict[str, Any], history: Mapping[str, Any], labels: Mapping[str, str]
) -> list[str]:
    """就地改写一份已保存配置的文案部分；返回给管理员看的变化说明。

    history 是 {名称: {路径: 旧默认文案}} 的旧默认值表，labels 把路径翻译成
    管理页名称。全局配置和群级稀疏覆盖都适用：没有出现的条目保持缺省。
    """

    messages = value.get("messages")
    if not isinstance(messages, dict):
        return []
    notes: list[str] = []

    def name(path: str) -> str:
        return labels.get(path, path)

    for target, sources in MERGES.items():
        present = [
            (path, values, copy.deepcopy(_get(messages, path)))
            for path, values in sources
            if _get(messages, path) is not None
        ]
        for path, _values in sources:
            if path != target:
                _drop(messages, path)
        if not present:
            continue
        custom = [item for item in present if _customized(item[2], item[0], history)]
        if not custom:
            # 都是默认文案：只保留开关（启用、引用），文案取新的默认值。
            _drop(messages, target)
            primary = present[0][2]
            if isinstance(primary, Mapping):
                kept = {
                    flag: primary[flag]
                    for flag in ("enabled", "quote")
                    if flag in primary
                }
                if kept:
                    _put(messages, target, kept)
            continue
        path, values, entry = custom[0]
        if len(custom) > 1 and not _same_after_generalize(
            [(item[2], item[1]) for item in custom]
        ):
            others = "；".join(
                f"{name(item[0])}：“{item[2].get('text', '')}”" for item in custom[1:]
            )
            notes.append(
                f"{name(target)}：由多条合并，采用了“{name(path)}”的文案；"
                f"未采用的自定义文案为 {others}，需要时请手动调整。"
            )
        elif path != target:
            notes.append(f"{name(target)}：合并后沿用“{name(path)}”的自定义文案。")
        for field in ("text", "alternatives"):
            if field not in entry:
                continue
            texts = [entry[field]] if field == "text" else list(entry[field])
            texts = [_generalize(str(text), values) for text in texts]
            if target in _NEEDS_CANDIDATES:
                # 旧的离婚歧义提示由代码另外拼接候选；合并后统一放进文案。
                texts = [_with_candidates(text) for text in texts]
            entry[field] = texts[0] if field == "text" else texts
        _put(messages, target, entry)

    for source, targets in SPLITS.items():
        entry = _get(messages, source)
        _drop(messages, source)
        if entry is None:
            continue
        for target in targets:
            _put(messages, target, copy.deepcopy(entry))
        if _customized(entry, source, history):
            notes.append(
                f"{name(source)}：已拆成“{'”“'.join(name(t) for t in targets)}”，"
                "原自定义文案复制到了每一条，请检查是否都合适。"
            )

    for path in SYSTEM_MESSAGES:
        entry = _get(messages, path)
        _drop(messages, path)
        if entry is not None and _customized(entry, path, history):
            notes.append(f"{name(path)}：改为固定的系统提示，原自定义文案不再使用。")

    for section in [key for key, entries in messages.items() if entries == {}]:
        del messages[section]
    if messages == {}:
        del value["messages"]
    return notes


# 升级报告里使用的名称，与管理页一致。
PATH_LABELS: dict[str, str] = {
    "errors.target_required": "没有 @ 人",
    "errors.target_invalid": "不能选这个人",
    "errors.feature_disabled": "功能已关闭",
    "results.quota_used_up": "次数用完",
    "results.capacity_full_single": "已有老婆/老公",
    "results.invite_invalid": "赠送邀请失效",
    "errors.relation_ambiguous": "关系有多段匹配",
    "errors.character_ambiguous": "角色有多个匹配",
    "errors.relation_missing": "找不到关系",
    "errors.character_missing": "找不到角色",
    "results.draw_character": "抽到老婆/老公",
    "results.designated": "指定老婆/老公成功",
    "results.divorce_locked": "受保护关系不能离婚",
    "results.gift_locked": "受保护关系不能转送",
    "errors.force_target_required": "强娶没有 @ 目标",
    "errors.steal_target_required": "抢夺没有 @ 目标",
    "errors.gift_target_required": "赠送没有 @ 目标",
    "errors.affection_target_required": "亲密度查询没有 @ 目标",
    "errors.target_is_self": "目标是自己",
    "errors.target_is_bot": "目标是机器人",
    "errors.target_ineligible": "目标暂时不能参与",
    "results.invalid_target": "目标无效",
    "results.steal_own_partner": "对方的群友就是自己",
    "results.gift_to_subject": "把群友送给 ta 本人",
    "errors.mode_closed": "玩法关闭",
    "errors.force_disabled": "强娶关闭",
    "errors.steal_disabled": "抢夺关闭",
    "errors.gift_disabled": "赠送关闭",
    "errors.divorce_disabled": "解除关系关闭",
    "results.designated_disabled": "指定抽取关闭",
    "results.mode_disabled": "玩法已关闭",
    "errors.rank_intimacy_disabled": "亲密度排行关闭",
    "errors.rank_activity_disabled": "活跃度排行关闭",
    "results.force_limit": "强娶次数用完",
    "results.steal_limit": "抢夺次数用完",
    "results.divorce_limit": "解除关系次数用完",
    "results.no_designated_quota": "指定次数用完",
    "results.capacity_full_single_wife": "已有老婆",
    "results.capacity_full_single_husband": "已有老公",
    "results.invite_expired": "赠送邀请过期",
    "results.invite_invalidated": "赠送邀请失效",
    "errors.steal_relation_ambiguous": "抢夺关系不明确",
    "errors.gift_relation_ambiguous": "赠送关系不明确",
    "errors.divorce_ambiguous": "离婚关系不明确",
    "errors.designated_ambiguous": "指定角色有多个匹配",
    "errors.character_info_ambiguous": "角色有多个匹配",
    "errors.admin_set_ambiguous": "设置老婆角色不明确",
    "errors.steal_relation_missing": "抢夺关系未找到",
    "errors.gift_relation_missing": "赠送关系未找到",
    "errors.designated_missing": "未找到指定角色",
    "errors.character_info_missing": "未找到角色",
    "errors.admin_set_missing": "设置老婆未找到角色",
    "results.draw_wife": "抽到老婆",
    "results.draw_husband": "抽到老公",
    "results.designated_wife": "指定老婆成功",
    "results.designated_husband": "指定老公成功",
    "results.relationship_locked": "受保护关系不可操作",
    "results.invalid_probability": "抢夺概率无效",
    "results.invalid_timeout": "赠送超时无效",
    "results.unknown_result": "其他操作结果",
    "errors.operation_error": "操作错误",
    "errors.syntax_error": "指令格式错误",
}
