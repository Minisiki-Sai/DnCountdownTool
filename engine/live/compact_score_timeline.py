from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from ..team_support_rules import team_effect_duration_override
from .summon_presence import summon_overlay_spec
from .score_timeline_display import encode_score_timeline_display
from .aura_state import is_aura_row, is_aura_damage_series, is_inferred_aura_row


COMPACT_SCORE_TIMELINE_VERSION = 1
COMPACT_SCORE_TIMELINE_QUANTUM = 100  # centiseconds
MAX_COMPACT_SCORE_TIMELINE_BYTES = 320 * 1024

_MAX_SKILL_EVENTS = 6000
_MAX_EFFECT_EVENTS = 3000
_MAX_SUMMON_EVENTS = 1200
_MAX_OUTPUT_WINDOWS = 2048
_MAX_NAMES = 1024

# Effect lane codes are part of compact format v1.  Keep them stable: the web
# client decodes these numbers without carrying the verbose status objects.
EFFECT_ACTUAL_BUFF = 0
EFFECT_THEORY_BUFF = 1
EFFECT_ACTUAL_DEBUFF = 2
EFFECT_THEORY_DEBUFF = 3
EFFECT_ACTUAL_PERSONAL = 4


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _number(value: object, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def _text(value: object, fallback: str = "") -> str:
    result = str(value or "").strip()
    if not result:
        return fallback
    # Control characters do not help a timeline and make JSON/UI diagnostics
    # harder to read.  Preserve ordinary Chinese punctuation and whitespace.
    result = re.sub(r"[\x00-\x1f\x7f]+", " ", result)
    return re.sub(r"\s+", " ", result).strip()[:128]


def _name_key(value: object) -> str:
    return re.sub(
        r"[\s·：:()（）【】\[\]_\-]+",
        "",
        _text(value).upper().replace("ＥＸ", "EX").replace("EX", ""),
    )


# Packet-backed acceleration rows can carry the cooldown-length resistance
# interval while retaining the source skill's "...buff" label.  These are the
# real gain lifetimes that the uploaded timeline must render.  Keep this local
# to the upload projection because Scorching Flame's simulator duration is
# job/level dependent (the current Fire Dance default is level 15 / 14 s).
_UPLOADED_ACCELERATION_GAIN_DURATIONS = {
    _name_key("精神爆发"): 14.0,
    _name_key("变异精神爆发"): 14.0,
    _name_key("灼热火焰"): 14.0,
    _name_key("时间加速"): 15.0,
    _name_key("变异时间加速"): 15.0,
}


def _ticks(value: float) -> int:
    return max(0, round(max(0.0, value) * COMPACT_SCORE_TIMELINE_QUANTUM))


def _cast_bounds(row: Mapping[str, Any]) -> tuple[float, float]:
    if "timestamp_ms" in row:
        start = max(0.0, _number(row.get("timestamp_ms")) / 1000.0)
    else:
        start = max(0.0, _number(row.get("seconds", row.get("start"))))
    if "end_timestamp_ms" in row:
        end = _number(row.get("end_timestamp_ms")) / 1000.0
    else:
        end = _number(row.get("end_seconds", row.get("end")), start)
    if end <= start:
        end = start + max(0.08, _number(row.get("cast_time"), 0.3))
    return start, max(start + 0.08, end)


def _cast_name(row: Mapping[str, Any]) -> str:
    return _text(row.get("skill_name") or row.get("skill"), "技能")


def _cast_flags(row: Mapping[str, Any]) -> int:
    flags = 0
    if row.get("high_value") is True:
        flags |= 1
    if row.get("in_burst_window") is True:
        flags |= 2
    if row.get("display_only") is True:
        flags |= 4
    return flags


def _cast_events(
    value: object,
    *,
    duration_seconds: float,
) -> list[tuple[float, float, str, int]]:
    result: list[tuple[float, float, str, int]] = []
    for row in _rows(value):
        start, end = _cast_bounds(row)
        if duration_seconds > 0:
            if start > duration_seconds + 1.0:
                continue
            end = min(end, duration_seconds)
        if end <= start:
            continue
        result.append((start, end, _cast_name(row), _cast_flags(row)))
    return sorted(result, key=lambda event: (event[0], event[1], event[2]))


def _intervals(row: Mapping[str, Any], *keys: str) -> list[tuple[float, float]]:
    for key in keys:
        value = row.get(key)
        if not isinstance(value, (list, tuple)) or not value:
            continue
        result: list[tuple[float, float]] = []
        for interval in value:
            if not isinstance(interval, (list, tuple)) or len(interval) < 2:
                continue
            start = max(0.0, _number(interval[0]))
            end = max(start, _number(interval[1]))
            if end > start + 0.0001:
                result.append((start, end))
        if result:
            return result
    return []


def _effect_name(row: Mapping[str, Any]) -> str:
    skill = _text(row.get("skill_name"))
    effect = _text(row.get("effect_key") or row.get("label") or row.get("key"))
    if skill and effect and _name_key(effect) not in _name_key(skill):
        return _text(f"{skill} · {effect}")
    return skill or effect or "Buff"


def _actual_player_buff_intervals(
    row: Mapping[str, Any],
) -> list[tuple[float, float]]:
    """Return display intervals for a packet-backed player buff.

    Acceleration skills now also apply a cooldown-length resistance status.
    Some v576 packet variants expose that resistance duration on a row whose
    source skill/label still says ``精神爆发增益`` (and equivalents).  Uploading
    the raw interval therefore rendered a 38.72/45/100-second *gain* bar even
    though the real gain lasts only 14/14/15 seconds.

    The upload-specific acceleration table (then the shared effect-duration
    table for other effects) is authoritative for the gain.  Clamp only the
    end of each observed interval so deaths, clears, map changes and capture
    truncation can still produce a legitimately shorter bar.  Other skills
    without a known duration retain their packet interval unchanged.
    """

    intervals = _intervals(row, "intervals")
    effect_identity = _text(
        " ".join(
            _text(row.get(key))
            for key in ("effect_key", "label", "key", "status_kind", "kind")
        )
    )
    if "抵抗" in effect_identity:
        return intervals
    skill_name = _text(row.get("skill_name") or row.get("skill"))
    duration = _UPLOADED_ACCELERATION_GAIN_DURATIONS.get(
        _name_key(skill_name),
        team_effect_duration_override(skill_name),
    )
    duration = max(0.0, duration)
    if duration <= 0.0:
        return intervals
    return [
        (start, min(end, start + duration))
        for start, end in intervals
        if min(end, start + duration) > start + 0.0001
    ]


def _is_debuff(row: Mapping[str, Any]) -> bool:
    value = " ".join(
        _text(row.get(key))
        for key in ("kind", "status_kind", "effect_key", "label", "key")
    ).lower()
    return (
        "debuff" in value
        or "减抗" in value
        or "减防" in value
        or "减攻击" in value
        or "减致命" in value
        or "增伤" in value
        or "冰冻" in value
        or "叠冰" in value
    )


def _merged_effect_events(
    detail: Mapping[str, Any],
    *,
    duration_seconds: float,
) -> list[tuple[float, float, str, int]]:
    grouped: dict[tuple[str, int], list[tuple[float, float, str, int]]] = defaultdict(list)

    def add(row: Mapping[str, Any], intervals: Iterable[tuple[float, float]], lane: int) -> None:
        name = _effect_name(row)
        for start, end in intervals:
            if duration_seconds > 0:
                if start > duration_seconds + 1.0:
                    continue
                end = min(end, duration_seconds)
            if end <= start + 0.0001:
                continue
            event = (start, end, name, lane)
            grouped[(_name_key(name), lane)].append(event)

    for row in _rows(detail.get("support_details")):
        debuff = _is_debuff(row)
        add(
            row,
            _intervals(row, "actual_display_intervals", "actual_intervals"),
            EFFECT_ACTUAL_DEBUFF if debuff else EFFECT_ACTUAL_BUFF,
        )
        add(
            row,
            _intervals(row, "theory_display_intervals", "theory_intervals"),
            EFFECT_THEORY_DEBUFF if debuff else EFFECT_THEORY_BUFF,
        )

    for row in _rows(detail.get("_compact_player_buffs")):
        if is_inferred_aura_row(row):
            continue
        if is_aura_row(row) and not is_aura_damage_series(row):
            continue
        personal = (
            _text(row.get("source_id")).upper()
            and _text(row.get("source_id")).upper() == _text(row.get("target_id")).upper()
        )
        lane = EFFECT_ACTUAL_PERSONAL if personal else EFFECT_ACTUAL_BUFF
        add(row, _actual_player_buff_intervals(row), lane)

    result: list[tuple[float, float, str, int]] = []
    for events in grouped.values():
        ordered = sorted(events, key=lambda event: (event[0], event[1]))
        for event in ordered:
            previous = result[-1] if result else None
            if (
                previous is not None
                and _name_key(previous[2]) == _name_key(event[2])
                and previous[3] == event[3]
                and event[0] <= previous[1] + 0.05
            ):
                result[-1] = (previous[0], max(previous[1], event[1]), previous[2], previous[3])
            else:
                result.append(event)
    return sorted(result, key=lambda event: (event[0], event[3], event[2]))


def _summon_events(
    casts: Sequence[Mapping[str, Any]],
    *,
    job: str,
    duration_seconds: float,
    lane: int,
) -> list[tuple[float, float, str, int]]:
    active: dict[str, list[list[Any]]] = defaultdict(list)
    result: list[list[Any]] = []
    last_cast: dict[str, float] = {}
    ordered = sorted(casts, key=lambda row: _cast_bounds(row)[0])
    for row in ordered:
        spec = summon_overlay_spec(_cast_name(row))
        if spec is None:
            continue
        start, _cast_end = _cast_bounds(row)
        if duration_seconds > 0 and start > duration_seconds + 1.0:
            continue
        previous = last_cast.get(spec.key)
        if previous is not None and abs(start - previous) <= 0.25:
            continue
        last_cast[spec.key] = start
        group = spec.shared_group or spec.key
        candidates = [event for event in active[group] if float(event[1]) > start]
        active[group] = candidates
        capacity = spec.shared_group_max if spec.shared_group else spec.max_instances
        capacity = max(1, int(capacity or 1))
        while len(candidates) >= capacity:
            oldest = min(candidates, key=lambda event: (float(event[0]), float(event[1])))
            oldest[1] = max(float(oldest[0]) + 0.08, start)
            candidates.remove(oldest)
        end = start + max(0.1, spec.duration_for_job(job))
        if duration_seconds > 0:
            end = min(end, duration_seconds)
        if end <= start:
            continue
        event: list[Any] = [start, end, spec.skill_name, lane]
        result.append(event)
        candidates.append(event)
    return [
        (float(start), float(end), str(name), int(event_lane))
        for start, end, name, event_lane in result
        if float(end) > float(start)
    ]


def _timeline_duration(detail: Mapping[str, Any]) -> float:
    duration = max(
        0.0,
        _number(detail.get("_compact_duration_seconds")),
        _number(detail.get("elapsed_seconds")),
    )
    for key in ("_compact_actual_casts", "actual_timeline", "theory_timeline"):
        for row in _rows(detail.get(key)):
            _start, end = _cast_bounds(row)
            duration = max(duration, end)
    for _start, end in _intervals(detail, "output_windows"):
        duration = max(duration, end)
    return min(24 * 60 * 60.0, duration)


def _output_windows(
    detail: Mapping[str, Any],
    *,
    duration_seconds: float,
) -> list[tuple[float, float]]:
    """Return the exact output windows used by the local score calculation."""

    rows = sorted(_intervals(detail, "output_windows"))
    result: list[tuple[float, float]] = []
    for start, end in rows:
        if duration_seconds > 0:
            if start > duration_seconds + 1.0:
                continue
            end = min(end, duration_seconds)
        if end <= start + 0.0001:
            continue
        if result and start <= result[-1][1] + 0.001:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _encode_section(
    events: Sequence[tuple[float, float, str, int]],
    intern_name,
    *,
    include_lane: bool,
) -> list[list[int]]:
    result: list[list[int]] = []
    previous_start = 0
    for start, end, name, extra in sorted(events, key=lambda event: (event[0], event[1], event[2])):
        start_tick = _ticks(start)
        duration_tick = max(1, _ticks(end) - start_tick)
        row = [start_tick - previous_start, duration_tick, intern_name(name)]
        if include_lane or extra:
            row.append(int(extra))
        result.append(row)
        previous_start = start_tick
    return result


def _encode_output_windows(
    windows: Sequence[tuple[float, float]],
) -> list[list[int]]:
    result: list[list[int]] = []
    previous_start = 0
    for start, end in windows:
        start_tick = _ticks(start)
        duration_tick = max(1, _ticks(end) - start_tick)
        result.append([start_tick - previous_start, duration_tick])
        previous_start = start_tick
    return result


def _payload_from_sections(
    duration_seconds: float,
    sections: Mapping[str, Sequence[tuple[float, float, str, int]]],
    truncated: Iterable[str],
    *,
    output_windows: Sequence[tuple[float, float]] = (),
) -> dict[str, Any]:
    names: list[str] = []
    indexes: dict[str, int] = {}

    def intern_name(value: object) -> int:
        name = _text(value, "技能")
        index = indexes.get(name)
        if index is not None:
            return index
        if len(names) >= _MAX_NAMES:
            name = "其他技能"
            index = indexes.get(name)
            if index is not None:
                return index
        index = len(names)
        names.append(name)
        indexes[name] = index
        return index

    payload: dict[str, Any] = {
        "v": COMPACT_SCORE_TIMELINE_VERSION,
        "q": COMPACT_SCORE_TIMELINE_QUANTUM,
        "d": _ticks(duration_seconds),
    }
    for key in ("a", "t", "e", "s"):
        events = sections.get(key, ())
        if events:
            payload[key] = _encode_section(
                events,
                intern_name,
                include_lane=key in {"e", "s"},
            )
    if output_windows:
        # Optional v1 extension. Older clients safely ignore it; newer clients
        # can show the exact effective-output duration without rebuilding the
        # score or guessing from sparse casts.
        payload["o"] = _encode_output_windows(output_windows)
    payload["n"] = names
    truncated_keys = sorted(set(truncated))
    if truncated_keys:
        payload["x"] = truncated_keys
    return payload


def compact_score_timeline_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def build_compact_score_timeline(value: object) -> dict[str, Any]:
    """Build the history-only timeline without changing any score input.

    The format stores only timestamps, durations, names and lane flags.  It is
    intentionally produced after scoring and therefore cannot affect either
    the actual or theoretical score calculation.
    """

    detail = _mapping(value)
    existing = detail.get("compact_timeline")
    if isinstance(existing, dict) and existing.get("v") == COMPACT_SCORE_TIMELINE_VERSION:
        return dict(existing)

    duration = _timeline_duration(detail)
    actual_rows = _rows(detail.get("_compact_actual_casts")) or _rows(
        detail.get("actual_timeline")
    )
    theory_rows = _rows(detail.get("theory_timeline"))
    sections: dict[str, list[tuple[float, float, str, int]]] = {
        "a": _cast_events(actual_rows, duration_seconds=duration),
        "t": _cast_events(theory_rows, duration_seconds=duration),
        "e": _merged_effect_events(detail, duration_seconds=duration),
        "s": [
            *_summon_events(
                actual_rows,
                job=_text(detail.get("job")),
                duration_seconds=duration,
                lane=0,
            ),
            *_summon_events(
                theory_rows,
                job=_text(detail.get("job")),
                duration_seconds=duration,
                lane=1,
            ),
        ],
    }
    output_windows = _output_windows(detail, duration_seconds=duration)
    limits = {
        "a": _MAX_SKILL_EVENTS,
        "t": _MAX_SKILL_EVENTS,
        "e": _MAX_EFFECT_EVENTS,
        "s": _MAX_SUMMON_EVENTS,
    }
    truncated: set[str] = set()
    for key, limit in limits.items():
        if len(sections[key]) > limit:
            sections[key] = sections[key][:limit]
            truncated.add(key)

    if len(output_windows) > _MAX_OUTPUT_WINDOWS:
        output_windows = output_windows[:_MAX_OUTPUT_WINDOWS]
        truncated.add("o")

    payload = _payload_from_sections(
        duration,
        sections,
        truncated,
        output_windows=output_windows,
    )
    # The ordinary 40-minute record is far below this guard.  The loop only
    # protects CloudBase from pathological/corrupt captures while retaining a
    # usable prefix and explicitly marking the affected lanes.
    while compact_score_timeline_size(payload) > MAX_COMPACT_SCORE_TIMELINE_BYTES:
        candidates = [key for key in ("a", "t", "e", "s") if len(sections[key]) > 16]
        if len(output_windows) > 16:
            candidates.append("o")
        if not candidates:
            return {}
        key = max(
            candidates,
            key=lambda item: len(output_windows) if item == "o" else len(sections[item]),
        )
        if key == "o":
            keep = max(16, math.floor(len(output_windows) * 0.90))
            output_windows = output_windows[:keep]
        else:
            keep = max(16, math.floor(len(sections[key]) * 0.90))
            sections[key] = sections[key][:keep]
        truncated.add(key)
        payload = _payload_from_sections(
            duration,
            sections,
            truncated,
            output_windows=output_windows,
        )
    context = detail.get("_compact_display_context")
    if isinstance(context, dict):
        encoded = encode_score_timeline_display(context)
        if encoded:
            payload["p"] = encoded
        if not encoded or compact_score_timeline_size(payload) > MAX_COMPACT_SCORE_TIMELINE_BYTES:
            payload.pop("p", None)
            payload["x"] = sorted(set(payload.get("x", ())) | {"p"})
    return payload if any(payload.get(key) for key in ("a", "t", "e", "s", "o", "p")) else {}
