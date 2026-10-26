from __future__ import annotations

import re
import os
from functools import lru_cache
from typing import Any

from .data_loader import as_number, norm
from .job_mechanics import JOB_MECHANICS

_EXACT_ROUND2 = os.environ.get('DN_SCORE_EXACT_ROUND2', '1') == '1'


@lru_cache(maxsize=4096)
def _skill_text_parts(text: str) -> tuple[str, str]:
    """Cache string normalization only; aliases/rules are still read live."""
    exact = re.sub(r"\s+", "", text).upper()
    return exact, re.sub(r"EX$", "", exact)


# Skill-table rows sometimes contain levels that are not available in the
# current classic-server build. Keep these caps in the shared correction
# layer so team setup, extreme-team timelines, and live-DPS scoring resolve
# exactly the same level instead of independently taking the last data row.
TEAM_EFFECT_LEVEL_CAPS = {
    "精神爆发": 4,
    "鸡尾酒": 5,
    "无情打击": 5,
    "光明之呼吸": 6,
    "痊愈之手": 7,
    "掌握之手": 9,
    "掌握之手EX": 9,
}


# This is the shared correction layer for team-effect lifetimes.  Both the
# combat timeline and live-DPS scoring must read the same values.
TEAM_EFFECT_DURATION_OVERRIDES = {
    "飞行冲击EX": 15,
    "精神爆发": 14,
    "变异精神爆发": 14,
    # The acceleration-resistance status lasts for the caster's cooldown, but
    # the actionable team buff does not.  Keep the unambiguous shared values
    # here.  Scorching Flame is job/level dependent and is normalised only in
    # the uploaded packet-backed timeline projection.
    "时间加速": 15,
    "变异时间加速": 15,
    "捆绑射击EX": 9,
    "螺旋踢EX": 7,
    "爆裂踢EX": 9,
    "冰刃EX": 7,
    "地狱火海EX": 11,
    "冰刺": 6,
    "弗罗斯特风": 5,
    "重磅鸭": 20,
    "冲击之手": 20,
    "黄昏之光": 10,
    "连锁闪电": 10,
    "雷电冲击": 5,
    "重击": 10,
    # v576 status packets consistently report a ten-second light-resistance
    # reduction for Electric Arrow.  The old five-second fallback was the
    # source of the short theory window.
    "电光箭": 10,
    # Each Lightning Relic hit leaves a six-second resistance reduction.  The
    # relic's 16.2-second summon lifetime is modeled separately.
    "雷电之手": 6,
    # Blizzard's own description and every observed status packet agree on an
    # eleven-second freeze application.
    "暴风雪": 11,
    # Heaven's Judgment applies a 15-second resistance state; ten seconds was
    # an obsolete table fallback.
    "天堂审判": 15,
    # Holy Ground is a per-hit status lifetime whose effective union is
    # extended below by its later-hit refresh span. Mind Breaker applies one
    # seven-second resistance reduction directly when cast.
    "神圣大地": 1,
    "精神爆破EX": 7,
    # v576 traces carry the actual 15% light-resistance reduction as a
    # twelve-second status.  The skill also emits a separate ~2.5 s shock
    # state; that state is decoded independently and must not shorten this
    # team-effect window.
    "闪电风暴": 12,
    "能量反射": 24,
    "怨恨之像": 11,
    "灵魂之门": 14,
    "切骨之刺EX": 10,
    # v576 status packets consistently report seven seconds for both the base
    # and EX action.  The old eight-second correction was one second too long.
    "旋风斩EX": 7,
    "火墙EX": 15,
    "重磅轰击": 12,
    "灵魂尖叫": 15,
    "致命狂镰": 8,
    "惩罚": 12,
    "致命狂镰EX": 8,
    "惩罚EX": 12,
    "九天之舞2阶": 15,
}


TEAM_EFFECT_PROC_CHANCES = {
    norm(skill): max(0.0, min(1.0, as_number(chance, 1.0)))
    for skill, chance in JOB_MECHANICS.global_value(
        "team.effect_proc_chances", {}
    ).items()
}


def team_effect_proc_chance(skill_name: str) -> float:
    """Return the deterministic theory weight for a chance-based effect."""

    return TEAM_EFFECT_PROC_CHANCES.get(norm(skill_name), 1.0)

# These effects share a base-skill name but have a different lifetime from the
# base effect. Keep the EX suffix here so, for example, Frost's 12-second
# resistance reduction does not become the 4-second Frost EX freeze layer.
TEAM_EFFECT_EXACT_DURATION_OVERRIDES = {
    "霜冻EX": 4,
}

# A single cast can apply effects with different lifetimes. Skill-only
# corrections are therefore not sufficient for consumers that flatten the
# cast into display/evaluation windows. Keep those exceptions keyed by both
# the canonical cast and the concrete effect.
TEAM_EFFECT_KEY_DURATION_OVERRIDES = {
    ("霜冻", "冰冻层"): 4,
    ("精神爆破", "减光抗%"): 7,
    ("神圣大地", "减光抗%"): 1,
}

# These lingering attacks reapply an effect after the casting animation has
# ended. The value is the span from the first application to the final refresh,
# not a replacement for the per-application status duration.
TEAM_EFFECT_REFRESH_TAILS = {
    # 0928竞速第五把: 24 casts, median first-to-last 15 s fire-resist
    # application is about 2.8 s. Each damaging magma hit refreshes it.
    ("漫长熔浆", "减火抗%"): 2.8,
    ("上升重力", "减暗抗%"): 5.0,
    # Consecration refreshes a one-second reduction roughly every 0.5s. Live
    # v576 traces place the final refresh about ten seconds after the first.
    ("神圣大地", "减光抗%"): 10.0,
    # Multi-hit skills below reapply the same status after their first hit.
    # Values are medians from long v576 captures and represent first-to-last
    # application spans, not an extension of each individual status packet.
    ("螺旋飓风", "增伤%"): 1.1,
    ("飓风狂射", "减光抗%"): 2.0,
    ("C2H5OH", "减全抗%"): 4.8,
    ("冰刺", "减水抗%"): 2.3,
    ("旋风斩", "减致命抵抗%"): 4.8,
    ("霜冻", "冰冻层"): 1.6,
    ("爱情病毒", "减暗抗%"): 0.9,
    ("连锁闪电", "减光抗%"): 1.1,
    ("天降雨点", "减暗抗%"): 0.5,
    ("日光焰", "增伤%"): 1.9,
    ("冰河", "冰冻层"): 1.6,
    ("暴风雪", "冰冻层"): 3.3,
    ("电光箭", "减光抗%"): 0.41,
    ("天堂审判", "减光抗%"): 2.6,
    ("重锤", "增伤%"): 0.85,
    ("灵魂之刃", "减火抗%"): 0.88,
}


# First status application is not always the cast start or cast end.  Each
# value is ``(cast phase, post-phase delay seconds)``.  A phase of 0.0 is the
# cast start and 1.0 is the cast end.  Animation-bound hits scale with the
# simulated action duration; projectile travel after the cast remains a fixed
# delay.  Keeping this keyed by the concrete effect prevents Frost's freeze
# timing from changing its separate water-resistance lane.
TEAM_EFFECT_APPLICATION_TIMINGS = {
    # 0928竞速第五把: 16 release-to-134800 observations start a median 0.427 s
    # after the cast packet; the base 1.48 s action places this near phase 0.29.
    # Later ghost hits do not refresh the EX door's 30% dark-resistance state.
    ("灵魂之门", "减暗抗%"): (0.29, 0.0),
    ("漫长熔浆", "减火抗%"): (0.0, 0.09),
    ("螺旋飓风", "增伤%"): (0.83, 0.0),
    ("飓风狂射", "减光抗%"): (1.0, 0.26),
    ("C2H5OH", "减全抗%"): (0.48, 0.0),
    ("冰刺", "减水抗%"): (0.37, 0.0),
    ("旋风斩", "减致命抵抗%"): (0.32, 0.0),
    ("霜冻", "冰冻层"): (0.47, 0.0),
    ("爱情病毒", "减暗抗%"): (1.0, 0.04),
    ("连锁闪电", "减光抗%"): (0.78, 0.0),
    ("天降雨点", "减暗抗%"): (1.0, 0.74),
    ("日光焰", "增伤%"): (0.64, 0.0),
    ("冰河", "冰冻层"): (0.325, 0.0),
    ("暴风雪", "冰冻层"): (0.236, 0.0),
    ("电光箭", "减光抗%"): (0.49, 0.0),
    ("天堂审判", "减光抗%"): (0.229, 0.0),
    ("重锤", "增伤%"): (0.49, 0.0),
    ("灵魂之刃", "减火抗%"): (0.54, 0.0),
}


# Freeze duration is part of these skills' level description.  Prefer that
# selected-level value over the static status-catalog fallback.  This matters
# most for Ice River, whose freeze grows from 3s to 8s with skill level.
_DESCRIPTION_DURATION_EFFECTS = frozenset(
    {
        ("冰河", "冰冻层"),
        ("暴风雪", "冰冻层"),
    }
)


# Most legacy debuffs begin affecting the target during their animation while
# their configured lifetime is counted after the animation. Mind Breaker is an
# exception: its seven-second lifetime is the complete cast-to-expiry window.
_TEAM_EFFECT_DURATION_FROM_APPLICATION = frozenset({"精神爆破"})


_REFRESHING_TEAM_BUFF_RULES = {
    # The relic exists for 18s and refreshes an 18s buff every 2s. Nine
    # applications at 0/2/.../16 form one continuous 34-second interval.
    "痊愈之手": (18.0, 2.0),
}


_TEAM_EFFECT_SKILL_ALIASES = {
    "神圣螺旋飓风": "螺旋飓风",
    # The planner exposes Frost EX's triggered lane as a synthetic action.
    "霜冻EX叠冰": "霜冻",
}


def _canonical_skill(value: Any) -> str:
    if _EXACT_ROUND2:
        text, without_ex = _skill_text_parts(norm(value))
    else:
        text = re.sub(r"\s+", "", norm(value)).upper()
        without_ex = re.sub(r"EX$", "", text)
    return _TEAM_EFFECT_SKILL_ALIASES.get(
        text,
        _TEAM_EFFECT_SKILL_ALIASES.get(without_ex, without_ex),
    )


def _exact_skill(value: Any) -> str:
    if _EXACT_ROUND2:
        return _skill_text_parts(norm(value))[0]
    return re.sub(r"\s+", "", norm(value)).upper()


_DURATION_BY_CANONICAL_SKILL = {
    _canonical_skill(skill): max(0.0, as_number(duration))
    for skill, duration in TEAM_EFFECT_DURATION_OVERRIDES.items()
}
_DURATION_BY_EXACT_SKILL = {
    _exact_skill(skill): max(0.0, as_number(duration))
    for skill, duration in TEAM_EFFECT_EXACT_DURATION_OVERRIDES.items()
}
_DURATION_BY_SKILL_AND_EFFECT = {
    (_canonical_skill(skill), norm(effect)): max(0.0, as_number(duration))
    for (skill, effect), duration in TEAM_EFFECT_KEY_DURATION_OVERRIDES.items()
}
_REFRESH_TAIL_BY_SKILL_AND_EFFECT = {
    (_canonical_skill(skill), norm(effect)): max(0.0, as_number(duration))
    for (skill, effect), duration in TEAM_EFFECT_REFRESH_TAILS.items()
}
_APPLICATION_TIMING_BY_SKILL_AND_EFFECT = {
    (_canonical_skill(skill), norm(effect)): (
        min(1.0, max(0.0, as_number(phase))),
        max(0.0, as_number(delay)),
    )
    for (skill, effect), (phase, delay) in TEAM_EFFECT_APPLICATION_TIMINGS.items()
}
_REFRESH_RULE_BY_CANONICAL_SKILL = {
    _canonical_skill(skill): rule
    for skill, rule in _REFRESHING_TEAM_BUFF_RULES.items()
}
_DURATION_FROM_APPLICATION_BY_CANONICAL_SKILL = frozenset(
    _canonical_skill(skill) for skill in _TEAM_EFFECT_DURATION_FROM_APPLICATION
)


def corrected_team_effect_duration(skill_name: str, duration: float) -> float:
    corrected = team_effect_duration_override(skill_name)
    return corrected or max(0.0, as_number(duration))


def corrected_team_effect_duration_for_effect(
    skill_name: str,
    effect_key: str,
    duration: float,
) -> float:
    corrected = _DURATION_BY_SKILL_AND_EFFECT.get(
        (_canonical_skill(skill_name), norm(effect_key)),
        0.0,
    )
    if corrected > 0:
        return corrected
    return corrected_team_effect_duration(skill_name, duration)


def resolved_team_effect_duration_for_effect(
    skill_name: str,
    effect_key: str,
    rule_duration: float,
    described_duration: float = 0.0,
) -> float:
    """Resolve a theory duration from status fallback and selected skill level."""
    key = (_canonical_skill(skill_name), norm(effect_key))
    described = max(0.0, as_number(described_duration))
    fallback = max(0.0, as_number(rule_duration))
    raw_duration = (
        described
        if key in _DESCRIPTION_DURATION_EFFECTS and described > 0
        else fallback or described
    )
    return corrected_team_effect_duration_for_effect(
        skill_name,
        effect_key,
        raw_duration,
    )


def team_effect_duration_override(skill_name: str) -> float:
    exact = _DURATION_BY_EXACT_SKILL.get(_exact_skill(skill_name), 0.0)
    if exact > 0:
        return exact
    return _DURATION_BY_CANONICAL_SKILL.get(_canonical_skill(skill_name), 0.0)


def team_effect_refresh_tail(skill_name: str, effect_key: str = "") -> float:
    """Return the post-application refresh span for one team effect."""
    skill_key = _canonical_skill(skill_name)
    if effect_key:
        return _REFRESH_TAIL_BY_SKILL_AND_EFFECT.get(
            (skill_key, norm(effect_key)), 0.0
        )
    return max(
        (
            tail
            for (candidate_skill, _candidate_effect), tail in
            _REFRESH_TAIL_BY_SKILL_AND_EFFECT.items()
            if candidate_skill == skill_key
        ),
        default=0.0,
    )


def _team_effect_attr_keys(attrs: Any) -> tuple[str, ...]:
    if not isinstance(attrs, dict):
        return ()
    return tuple(
        key
        for raw_key, raw_value in attrs.items()
        if (key := norm(raw_key))
        and not key.startswith("__")
        and abs(as_number(raw_value)) > 1e-9
    )


def team_effect_refresh_tail_for_attrs(skill_name: str, attrs: Any) -> float:
    """Return only refresh tails belonging to the action's concrete effects."""
    keys = _team_effect_attr_keys(attrs)
    if not keys:
        return team_effect_refresh_tail(skill_name)
    return max(
        (team_effect_refresh_tail(skill_name, key) for key in keys),
        default=0.0,
    )


def team_effect_duration_from_application(skill_name: str) -> bool:
    return (
        _canonical_skill(skill_name)
        in _DURATION_FROM_APPLICATION_BY_CANONICAL_SKILL
    )


def effective_team_effect_duration_for_effect(
    skill_name: str,
    effect_key: str,
    duration: float,
) -> float:
    """Return one cast's union lifetime when later hits refresh the effect."""
    corrected = corrected_team_effect_duration_for_effect(
        skill_name, effect_key, duration
    )
    return corrected + team_effect_refresh_tail(skill_name, effect_key)


def team_effect_application_window_for_effect(
    skill_name: str,
    effect_key: str,
    cast_start: float,
    cast_end: float,
    duration: float,
    *,
    starts_on_cast: bool = True,
    triggered_only: bool = False,
) -> tuple[float, float, float]:
    """Return ``(first application, final expiry, refresh span)`` for one cast.

    Legacy effects retain their original cast-inclusive display convention.
    Effects with packet-confirmed hit timing use their real first application
    and the final refresh plus one complete per-application duration.
    """
    left = max(0.0, as_number(cast_start))
    right = max(left, as_number(cast_end, left))
    corrected_duration = corrected_team_effect_duration_for_effect(
        skill_name,
        effect_key,
        duration,
    )
    refresh_tail = team_effect_refresh_tail(skill_name, effect_key)
    timing = _APPLICATION_TIMING_BY_SKILL_AND_EFFECT.get(
        (_canonical_skill(skill_name), norm(effect_key))
    )
    if timing is not None:
        cast_phase, delay = timing
        application = left + (right - left) * cast_phase + delay
        return (
            application,
            application + corrected_duration + refresh_tail,
            refresh_tail,
        )

    application = left if starts_on_cast and not triggered_only else right
    if refresh_tail > 0:
        # Existing lingering-area rules were calibrated from cast end.
        application = right
    expires_from = (
        application
        if triggered_only or team_effect_duration_from_application(skill_name)
        else right
    )
    return (
        application,
        expires_from + corrected_duration + refresh_tail,
        refresh_tail,
    )


def team_effect_application_window_for_attrs(
    skill_name: str,
    attrs: Any,
    cast_start: float,
    cast_end: float,
    duration: float,
    *,
    starts_on_cast: bool = True,
    triggered_only: bool = False,
) -> tuple[float, float, float]:
    """Return the union application window for the effects carried by an action."""
    keys = _team_effect_attr_keys(attrs)
    if not keys:
        return team_effect_application_window_for_effect(
            skill_name,
            "",
            cast_start,
            cast_end,
            duration,
            starts_on_cast=starts_on_cast,
            triggered_only=triggered_only,
        )
    windows = [
        team_effect_application_window_for_effect(
            skill_name,
            key,
            cast_start,
            cast_end,
            duration,
            starts_on_cast=starts_on_cast,
            triggered_only=triggered_only,
        )
        for key in keys
    ]
    return (
        min(start for start, _end, _tail in windows),
        max(end for _start, end, _tail in windows),
        max(tail for _start, _end, tail in windows),
    )


def effective_team_buff_duration(skill_name: str, duration: float) -> float:
    duration = corrected_team_effect_duration(skill_name, duration)
    summon_duration, refresh_interval = _REFRESH_RULE_BY_CANONICAL_SKILL.get(
        _canonical_skill(skill_name), (0.0, 0.0)
    )
    if summon_duration <= 0 or refresh_interval <= 0 or duration <= 0:
        return duration
    return summon_duration + duration - refresh_interval
