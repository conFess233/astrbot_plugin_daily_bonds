"""玩法规则的纯计算：额度、冷却、概率与重置时间。

聊天回复、“我的姻缘”和管理页查询共用这些函数，避免各处口径不一致。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def normal_draw_remaining(capacity: int, normal_draws: int) -> int:
    """普通抽取每周期共 `capacity` 次。"""

    return max(0, capacity - normal_draws)


def can_draw(*, held: int, capacity: int, normal_draws: int, credits: int) -> bool:
    """有空余持有名额，且还有普通次数或补抽资格。"""

    return held < capacity and (credits > 0 or normal_draws < capacity)


def limit_remaining(limit: int, used: int) -> int | None:
    """上限为 0 表示不限次数，返回 None。"""

    return None if limit == 0 else max(0, limit - used)


def cooldown_remaining(last_at: int | None, cooldown: int, now: int) -> int:
    if not last_at or cooldown <= 0:
        return 0
    return max(0, last_at + cooldown - now)


def steal_probability(
    base: float, intimacy_bonus: float, maximum: float, intimacy: int
) -> float:
    """基础成功率加上“抢夺者 → 持有者”亲密度加成。

    上限只约束加成部分：基础成功率本身高于上限时保持基础成功率。
    """

    boosted = base + intimacy_bonus * max(0, intimacy)
    return min(1.0, max(0.0, min(boosted, max(maximum, base))))


def format_reset(ends_at: int, timezone: str, now: int) -> str:
    """把周期结束时刻写成“今天 00:00 / 明天 00:00 / 3月5日 00:00”。"""

    zone = ZoneInfo(timezone)
    target = datetime.fromtimestamp(ends_at, zone)
    today = datetime.fromtimestamp(now, zone).date()
    clock = target.strftime("%H:%M")
    delta = (target.date() - today).days
    if delta == 0:
        return f"今天 {clock}"
    if delta == 1:
        return f"明天 {clock}"
    return f"{target.month}月{target.day}日 {clock}"
