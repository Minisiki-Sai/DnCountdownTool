"""Per-category publishing policy for the live DPS overlay (lightweight build).

This standalone tool intentionally keeps only the two countdown features the
user needs:

    buffs          增益 / 减益倒计时（个人/团队 buff、Boss 减抗增伤）
    boss_actions   Boss 技能 / 动作（技能倒计时预报的数据源）

Every other decoded event family (damage/DPS, healing, Boss HP, player status,
summons and profile inspection) is dropped at the earliest publish point, so its
downstream accounting, scoring and curves never run.

The two switches stop publishing, not decoding.  Decoding cannot be skipped:
the voice feed (``voice_signal_feed``) is filled inside the decoder from the
same packets and must keep announcing while the overlay windows are hidden.
What the switches remove is the downstream work (aggregation, overlay
snapshots, timers), which is why turning one off is cheap and turning it back
on costs a short "wait for the next event" window (see
``.dev/diag/analyze_toggle_recovery.py`` for the measured numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from .models import (
    BossActionEvent,
    BossHealthEvent,
    BossSpawnEvent,
    BossStatusEvent,
)


CAPTURE_TOGGLE_LABELS: dict[str, str] = {
    "buffs": "增益 / 减益倒计时",
    "boss_actions": "Boss 技能 / 动作倒计时",
}

# 身份类事件：不进首页的开关，始终保留。它们很便宜（沙龙整段 12k 条血量 +
# 6 条出生，约 17 条/秒），但决定了「Boss 是谁、叫什么、哪只在哪一边」——
# 轻量版正是缺了它们，才只能显示「未识别」。
BOSS_IDENTITY_CATEGORY = "boss_identity"

# Event class -> category.  Any event class absent from this map is dropped.
_EVENT_CATEGORY: dict[type, str] = {
    BossStatusEvent: "buffs",
    BossActionEvent: "boss_actions",
    BossHealthEvent: BOSS_IDENTITY_CATEGORY,
    BossSpawnEvent: BOSS_IDENTITY_CATEGORY,
}


@dataclass(frozen=True, slots=True)
class CaptureToggles:
    """Feature-category switches.  Every category defaults to enabled.

    ``False`` stops publishing that category (so its overlay windows and local
    timers go idle); it never stops decoding, so voice announcements keep
    working.
    """

    buffs: bool = True
    boss_actions: bool = True

    @classmethod
    def category_names(cls) -> tuple[str, ...]:
        return tuple(field.name for field in fields(cls))

    @classmethod
    def from_mapping(cls, value: Any) -> "CaptureToggles":
        if not isinstance(value, dict):
            return cls()
        kwargs: dict[str, bool] = {}
        for name in cls.category_names():
            kwargs[name] = bool(value.get(name, True))
        return cls(**kwargs)

    def to_mapping(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in self.category_names()}


def classify_event(event: Any) -> str | None:
    """Return the capture-toggle category key for one decoded event.

    Returns ``None`` for any event family this lightweight build does not
    capture (damage/healing/Boss HP/player status/summons/profiles).
    """
    return _EVENT_CATEGORY.get(type(event))


def should_publish(event: Any, toggles: CaptureToggles | None = None) -> bool:
    """True when this event should reach the aggregator.

    Two independent reasons to drop it:

    * the event family is outside the two countdown categories (always dropped),
    * its category switch is off (user closed that overlay and asked for the
      parsing work to stop as well).

    ``toggles=None`` keeps the category filter only, which is what the offline
    replay tools use.
    """
    category = classify_event(event)
    if category is None:
        return False
    if category == BOSS_IDENTITY_CATEGORY:
        # 身份/血量永远保留：没有它，悬浮窗连「Boss 是谁」都不知道。
        return True
    if toggles is None:
        return True
    return bool(getattr(toggles, category, True))
