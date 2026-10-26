from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from typing import Any, Iterable


HIGH_HP_COEFFICIENT_ATTR = "生命高于30额外系数%"
LOST_HP_COEFFICIENT_ATTR = "生命低于50额外系数%"
CROW_SKILL = "奉献乌鸦"
DANCE_STAGE_TWO_SKILL = "九天之舞2阶"
PUNISHMENT_SKILL = "惩罚攻击"

# These are the legacy long-axis averages embedded in the regular build and
# skill tables. Live scoring divides them out before applying real Boss HP.
HIGH_HP_EQUIVALENT_POINTS = 24.5
LOST_HP_EQUIVALENT_POINTS = 37.5
PUNISHMENT_EQUIVALENT_MISSING_HP = 18.0

BossHpSegments = tuple[tuple[tuple[float, float], ...], ...]

_BOSS_HP_INDEX_CACHE_LIMIT = 32
_BOSS_HP_INDEX_CACHE: OrderedDict[
    int,
    tuple[
        object,
        tuple[
            tuple[float, float, tuple[float, ...], tuple[float, ...]], ...
        ],
    ],
] = OrderedDict()


def _indexed_boss_hp_segments(
    segments: BossHpSegments | Iterable[Iterable[tuple[float, float]]],
) -> tuple[tuple[float, float, tuple[float, ...], tuple[float, ...]], ...]:
    """Index immutable HP tracks once without changing interpolation rules."""

    cacheable = isinstance(segments, tuple) and all(
        isinstance(segment, tuple) for segment in segments
    )
    cache_key = id(segments)
    if cacheable:
        cached = _BOSS_HP_INDEX_CACHE.get(cache_key)
        if cached is not None and cached[0] is segments:
            _BOSS_HP_INDEX_CACHE.move_to_end(cache_key)
            return cached[1]
    indexed: list[
        tuple[float, float, tuple[float, ...], tuple[float, ...]]
    ] = []
    for raw_segment in segments:
        segment = tuple(raw_segment)
        if not segment:
            continue
        times = tuple(float(point[0]) for point in segment)
        values = tuple(float(point[1]) for point in segment)
        indexed.append((times[0], times[-1], times, values))
    result = tuple(indexed)
    if cacheable:
        _BOSS_HP_INDEX_CACHE[cache_key] = (segments, result)
        _BOSS_HP_INDEX_CACHE.move_to_end(cache_key)
        while len(_BOSS_HP_INDEX_CACHE) > _BOSS_HP_INDEX_CACHE_LIMIT:
            _BOSS_HP_INDEX_CACHE.popitem(last=False)
    return result


def compact_boss_hp_segments(
    segments: BossHpSegments | Iterable[Iterable[tuple[float, float]]],
    bucket_seconds: float = 0.10,
) -> BossHpSegments:
    """Bound cross-process payload size while retaining HP threshold crossings."""

    bucket = max(0.01, float(bucket_seconds))
    thresholds = (60.0, 50.0, 30.0)
    compacted: list[tuple[tuple[float, float], ...]] = []
    for raw_segment in segments:
        points = tuple(
            (float(second), max(0.0, min(100.0, float(hp))))
            for second, hp in raw_segment
        )
        if len(points) <= 2:
            if points:
                compacted.append(points)
            continue
        kept: list[tuple[float, float]] = [points[0]]
        pending = points[1]
        bucket_index = int(points[1][0] // bucket)
        previous_hp = points[0][1]
        for point in points[1:-1]:
            current_bucket = int(point[0] // bucket)
            crossed_threshold = any(
                (previous_hp - threshold) * (point[1] - threshold) <= 0
                and abs(previous_hp - point[1]) > 1e-12
                for threshold in thresholds
            )
            if current_bucket != bucket_index or crossed_threshold:
                if pending[0] > kept[-1][0] + 1e-9:
                    kept.append(pending)
                bucket_index = current_bucket
            pending = point
            previous_hp = point[1]
        if pending[0] > kept[-1][0] + 1e-9:
            kept.append(pending)
        if points[-1][0] > kept[-1][0] + 1e-9:
            kept.append(points[-1])
        else:
            kept[-1] = points[-1]
        compacted.append(tuple(kept))
    return tuple(compacted)


def boss_hp_percent_at(
    segments: BossHpSegments | Iterable[Iterable[tuple[float, float]]],
    seconds: float,
) -> float | None:
    """Return HP for the most recently started active Boss phase.

    A full-run score can contain a long main-Boss phase overlapped by a later
    add/phase Boss.  The previous first-match lookup kept using the older main
    Boss track throughout that overlap, so HP-conditional skills were correct
    when scoring one Boss but wrong for the all-Boss scope.  Prefer the newest
    active phase and fall back to the older phase after the newer one ends.
    """

    current = max(0.0, float(seconds))
    selected: tuple[float, tuple[float, ...], tuple[float, ...]] | None = None
    for start, end, times, values in _indexed_boss_hp_segments(segments):
        if current < start - 1e-9:
            continue
        if current > end + 1e-9:
            continue
        if selected is None or start >= selected[0] - 1e-9:
            selected = (start, times, values)
    if selected is None:
        return None
    _start, times, values = selected
    index = bisect_right(times, current + 1e-9) - 1
    if index < 0:
        return None
    return max(0.0, min(100.0, values[index]))


def high_hp_bonus_percent(coefficient: float, hp_percent: float | None) -> float:
    if hp_percent is None:
        return 0.0
    return max(0.0, float(coefficient)) * max(0.0, float(hp_percent) - 30.0)


def lost_hp_bonus_percent(coefficient: float, hp_percent: float | None) -> float:
    if hp_percent is None:
        return 0.0
    lost_hp = min(50.0, max(0.0, 100.0 - float(hp_percent)))
    return max(0.0, float(coefficient)) * lost_hp


def punishment_coefficient(level: int | float) -> float:
    resolved_level = max(1, int(float(level or 1)))
    return min(6.0, 2.5 + resolved_level * 0.5)


def punishment_multiplier(level: int | float, hp_percent: float | None) -> float:
    if hp_percent is None:
        return 1.0
    missing_below_threshold = max(0.0, 60.0 - float(hp_percent))
    return 1.0 + punishment_coefficient(level) * missing_below_threshold / 100.0


def punishment_equivalent_multiplier(level: int | float) -> float:
    return (
        1.0
        + punishment_coefficient(level)
        * PUNISHMENT_EQUIVALENT_MISSING_HP
        / 100.0
    )


def conditional_team_buff_coefficient(skill_name: Any, level: int | float) -> float:
    skill = str(skill_name or "").strip().upper().replace(" ", "")
    skill = skill[:-2] if skill.endswith("EX") else skill
    resolved_level = max(1, int(float(level or 1)))
    if skill == CROW_SKILL.upper():
        return min(1.1, 0.5 + resolved_level * 0.1)
    if skill == DANCE_STAGE_TWO_SKILL.upper():
        return min(1.35, 0.30 + resolved_level * 0.15)
    return 0.0


def conditional_team_buff_gain_rate(
    skill_name: Any,
    coefficient: float,
    hp_percent: float | None,
) -> float:
    skill = str(skill_name or "").strip().upper().replace(" ", "")
    skill = skill[:-2] if skill.endswith("EX") else skill
    if skill == CROW_SKILL.upper():
        return high_hp_bonus_percent(coefficient, hp_percent) / 100.0
    if skill == DANCE_STAGE_TWO_SKILL.upper():
        return lost_hp_bonus_percent(coefficient, hp_percent) / 100.0
    return 0.0


def conditional_panel_correction(
    attrs: dict[str, Any],
    hp_percent: float | None,
) -> float:
    """Replace regular-simulator HP averages with the live Boss HP value."""

    correction = 1.0
    high_coefficient = max(0.0, float(attrs.get(HIGH_HP_COEFFICIENT_ATTR, 0.0) or 0.0))
    if high_coefficient > 0:
        equivalent = 1.0 + high_coefficient * HIGH_HP_EQUIVALENT_POINTS / 100.0
        actual = 1.0 + high_hp_bonus_percent(high_coefficient, hp_percent) / 100.0
        correction *= actual / equivalent
    lost_coefficient = max(0.0, float(attrs.get(LOST_HP_COEFFICIENT_ATTR, 0.0) or 0.0))
    if lost_coefficient > 0:
        equivalent = 1.0 + lost_coefficient * LOST_HP_EQUIVALENT_POINTS / 100.0
        actual = 1.0 + lost_hp_bonus_percent(lost_coefficient, hp_percent) / 100.0
        correction *= actual / equivalent
    return max(0.0, correction)


def punishment_correction(level: int | float, hp_percent: float | None) -> float:
    return punishment_multiplier(level, hp_percent) / max(
        1e-9, punishment_equivalent_multiplier(level)
    )
