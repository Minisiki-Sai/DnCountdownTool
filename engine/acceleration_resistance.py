from __future__ import annotations

import re
from typing import Any, Iterable


BURST_RESISTANCE_GROUP = "burst_resistance"
TIME_ACCEL_RESISTANCE_GROUP = "time_accel_resistance"

RESISTANCE_GROUP_LABELS = {
    BURST_RESISTANCE_GROUP: "爆发抵抗",
    TIME_ACCEL_RESISTANCE_GROUP: "时间加速抵抗",
}

RESISTANCE_GROUP_SKILLS = {
    BURST_RESISTANCE_GROUP: frozenset({
        "精神爆发",
        "变异精神爆发",
        "灼热火焰",
        "变异猎人之眼",
    }),
    TIME_ACCEL_RESISTANCE_GROUP: frozenset({
        "时间加速",
        "变异时间加速",
    }),
}


def _skill_name(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    text = re.sub(r"(?:EX)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[（(]PVE[）)]$", "", text, flags=re.IGNORECASE)
    return text


def acceleration_resistance_group(skill: Any) -> str:
    name = _skill_name(skill)
    for group, skills in RESISTANCE_GROUP_SKILLS.items():
        if name in skills:
            return group
    return ""


def acceleration_resistance_label(group_or_skill: Any) -> str:
    value = str(group_or_skill or "").strip()
    group = value if value in RESISTANCE_GROUP_LABELS else acceleration_resistance_group(value)
    return RESISTANCE_GROUP_LABELS.get(group, "")


def is_acceleration_resistance_skill(skill: Any) -> bool:
    return bool(acceleration_resistance_group(skill))


def _provider_sort_key(provider: str) -> tuple[int, str]:
    match = re.search(r"队员\s*(\d+)", provider)
    return (int(match.group(1)) if match else 9999, provider)


def _normalized_blocked_ranges(
    values: Iterable[Iterable[Any]] | None,
    window: float,
) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for value in values or ():
        try:
            start, end = value
        except (TypeError, ValueError):
            continue
        left = max(0.0, min(window, float(start or 0.0)))
        right = max(left, min(window, float(end or 0.0)))
        if right > left + 1e-6:
            rows.append((left, right))
    rows.sort()
    merged: list[tuple[float, float]] = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _legal_cast_start(
    start: float,
    cast_time: float,
    blocked: list[tuple[float, float]],
    window: float,
) -> float | None:
    current = max(0.0, start)
    duration = max(0.0, cast_time)
    for left, right in blocked:
        if current + duration <= left + 1e-6:
            break
        if current < right - 1e-6 and current + duration > left + 1e-6:
            current = right
    return current if current + duration <= window + 1e-6 else None


def schedule_acceleration_resistance(
    specs: list[dict[str, Any]],
    window: float,
    *,
    preferred_starts: dict[str, float] | None = None,
    explicit_intervals: dict[str, list[Iterable[Any]]] | None = None,
    blocked_ranges: Iterable[Iterable[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Schedule all acceleration skills against their shared resistance locks.

    A successful cast creates a half-open resistance window ``[start, start+CD)``.
    Candidates that would fall inside that window are delayed to its end; the
    returned rows retain the requested time so callers can explain the delay.
    """

    horizon = max(0.0, float(window or 0.0))
    if horizon <= 0:
        return []
    preferred = {
        str(key).strip(): max(0.0, float(value or 0.0))
        for key, value in (preferred_starts or {}).items()
    }
    explicit = explicit_intervals or {}
    blocked = _normalized_blocked_ranges(blocked_ranges, horizon)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for order, raw in enumerate(specs):
        provider = str(raw.get("provider") or "").strip()
        skill = str(raw.get("skill") or "").strip()
        group = acceleration_resistance_group(skill)
        cooldown = max(0.01, float(raw.get("cooldown") or 0.0))
        duration = max(0.0, float(raw.get("duration") or 0.0))
        cast_time = max(0.0, float(raw.get("cast_time") or 0.0))
        key = (provider, _skill_name(skill))
        if not provider or not group or duration <= 0 or key in seen:
            continue
        seen.add(key)
        normalized.append({
            **raw,
            "provider": provider,
            "skill": skill,
            "group": group,
            "cooldown": cooldown,
            "duration": duration,
            "cast_time": cast_time,
            "source_order": order,
        })

    candidates: list[dict[str, Any]] = []
    by_skill: dict[str, list[dict[str, Any]]] = {}
    for spec in normalized:
        by_skill.setdefault(_skill_name(spec["skill"]), []).append(spec)
    for skill_name, skill_specs in by_skill.items():
        skill_specs.sort(
            key=lambda row: (
                int(row.get("provider_order", 9999)),
                _provider_sort_key(row["provider"]),
                int(row["source_order"]),
            )
        )
        explicit_rows = explicit.get(skill_name) or explicit.get(skill_specs[0]["skill"])
        parsed_explicit: list[tuple[float, float]] = []
        if isinstance(explicit_rows, list):
            for raw_interval in explicit_rows:
                try:
                    start, end = raw_interval
                except (TypeError, ValueError):
                    continue
                start_value = max(0.0, float(start or 0.0))
                end_value = max(start_value, float(end or start_value))
                if start_value < horizon - 1e-6 and end_value > start_value + 1e-6:
                    parsed_explicit.append((start_value, end_value))
        if parsed_explicit:
            for occurrence, (start, end) in enumerate(parsed_explicit):
                spec = skill_specs[occurrence % len(skill_specs)]
                candidates.append({
                    **spec,
                    "requested_start": start,
                    "duration": max(0.0, end - start),
                    "occurrence": occurrence // len(skill_specs),
                    "explicit": True,
                })
            continue

        for provider_index, spec in enumerate(skill_specs):
            base_start = preferred.get(
                skill_name,
                preferred.get(spec["group"], 0.0),
            )
            requested = base_start + spec["cooldown"] * provider_index / len(skill_specs)
            occurrence = 0
            while requested < horizon - 1e-6 and occurrence < 1000:
                candidates.append({
                    **spec,
                    "requested_start": requested,
                    "occurrence": occurrence,
                    "explicit": False,
                })
                occurrence += 1
                requested += spec["cooldown"]

    candidates.sort(key=lambda row: (
        float(row["requested_start"]),
        0 if row.get("explicit") else 1,
        int(row.get("provider_order", 9999)),
        _provider_sort_key(row["provider"]),
        int(row["source_order"]),
        int(row["occurrence"]),
    ))
    group_ready: dict[str, float] = {}
    provider_ready: dict[tuple[str, str], float] = {}
    schedule: list[dict[str, Any]] = []
    for candidate in candidates:
        group = candidate["group"]
        provider_key = (candidate["provider"], _skill_name(candidate["skill"]))
        requested = max(0.0, float(candidate["requested_start"]))
        earliest = max(
            requested,
            group_ready.get(group, 0.0),
            provider_ready.get(provider_key, 0.0),
        )
        cast_start = _legal_cast_start(
            earliest,
            candidate["cast_time"],
            blocked,
            horizon,
        )
        if cast_start is None or cast_start >= horizon - 1e-6:
            continue
        cast_end = min(horizon, cast_start + candidate["cast_time"])
        effect_start = cast_end
        effect_end = min(horizon, effect_start + candidate["duration"])
        if effect_end <= effect_start + 1e-6:
            continue
        resistance_end = cast_start + candidate["cooldown"]
        row = {
            key: value
            for key, value in candidate.items()
            if key not in {"source_order"}
        }
        row.update({
            "cast_start": round(cast_start, 6),
            "cast_end": round(cast_end, 6),
            "effect_start": round(effect_start, 6),
            "effect_end": round(effect_end, 6),
            "resistance_group": group,
            "resistance_label": acceleration_resistance_label(group),
            "resistance_start": round(cast_start, 6),
            "resistance_end": round(min(horizon, resistance_end), 6),
            "delayed_by_resistance": cast_start > requested + 1e-6,
            "delay_seconds": round(max(0.0, cast_start - requested), 6),
        })
        schedule.append(row)
        group_ready[group] = resistance_end
        provider_ready[provider_key] = cast_start + candidate["cooldown"]
    return schedule


def resistance_windows_from_schedule(
    schedule: Iterable[dict[str, Any]],
    window: float,
) -> list[dict[str, Any]]:
    horizon = max(0.0, float(window or 0.0))
    rows: list[dict[str, Any]] = []
    for cast in schedule:
        group = str(cast.get("resistance_group") or "").strip()
        start = max(0.0, min(horizon, float(cast.get("resistance_start") or 0.0)))
        end = max(start, min(horizon, float(cast.get("resistance_end") or start)))
        if not group or end <= start + 1e-6:
            continue
        skill = str(cast.get("skill") or "").strip()
        provider = str(cast.get("provider") or "").strip()
        label = acceleration_resistance_label(group)
        rows.append({
            "name": label,
            "skill": label,
            "source": skill,
            "provider": provider,
            "group": group,
            "category": "acceleration_resistance",
            "start": round(start, 6),
            "end": round(end, 6),
            "color": "#697386",
            "detail": f"{skill}施加；抵抗期间不能再次释放或获得同组加速效果",
        })
    return rows
