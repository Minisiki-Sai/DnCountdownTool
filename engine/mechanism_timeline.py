from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .acceleration_resistance import (
    acceleration_resistance_group,
    acceleration_resistance_label,
    is_acceleration_resistance_skill,
)

try:
    if os.getenv("DRAGON_NEST_NATIVE_NO_QT") == "1":
        raise ModuleNotFoundError("Qt disabled for the V4.10 native backend")
    from PySide6.QtCore import QRectF, Qt, Signal
    from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPen
    from PySide6.QtWidgets import QInputDialog, QToolTip, QWidget
except ModuleNotFoundError:
    # The V4.10 data service reuses the scheduling functions in this module
    # without constructing the legacy Qt editor. Keep that path independent
    # from PySide6 while preserving the old widget when Qt is installed.
    class _QtPlaceholderMeta(type):
        def __getattr__(cls, _name: str) -> int:
            return 0

    class _QtPlaceholder(metaclass=_QtPlaceholderMeta):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __getattr__(self, _name: str) -> Any:
            return lambda *_args, **_kwargs: None

    class _SignalPlaceholder:
        def connect(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def emit(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    def Signal(*_args: Any, **_kwargs: Any) -> _SignalPlaceholder:
        return _SignalPlaceholder()

    QRectF = QColor = QGuiApplication = QKeySequence = QPainter = QPen = _QtPlaceholder
    QInputDialog = QToolTip = QWidget = _QtPlaceholder
    Qt = _QtPlaceholder


MECHANISM_TEMPLATE_PATH = (
    Path(os.getenv("APPDATA") or Path.home())
    / "DragonNestBuildSimulator"
    / "extreme_mechanism_templates.json"
)

BUILTIN_MECHANISM_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "黑龙竞速轴": [
        {"name": "p1全屏1", "start": 13.0, "end": 32.5},
        {"name": "p1全屏2", "start": 64.0, "end": 83.5},
        {"name": "p1全屏3", "start": 114.5, "end": 134.0},
        {"name": "18x小房间", "start": 161.5, "end": 206.5},
        {"name": "15x小房间", "start": 236.0, "end": 281.0},
        {"name": "10x跑圈", "start": 325.0, "end": 385.0},
        {"name": "混沌", "start": 409.0, "end": 448.0},
        {"name": "5x跑风", "start": 477.5, "end": 536.5},
        {"name": "混沌", "start": 557.5, "end": 594.5},
        {"name": "2x跑风 副本", "start": 612.0, "end": 672.5},
    ],
}


def _provider_job_name(provider: Any) -> str:
    text = str(provider or "").strip()
    prefix, separator, job = text.partition(" ")
    if separator and prefix.startswith("队员"):
        return job.strip()
    return text


def _provider_sort_key(provider: Any) -> tuple[str, str]:
    text = str(provider or "").strip()
    return _provider_job_name(text), text


def normalize_mechanism_intervals(
    intervals: list[dict[str, Any]] | None,
    window: float,
) -> list[dict[str, Any]]:
    """Clamp, sort and de-overlap named no-output intervals."""
    horizon = max(1.0, float(window))
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(intervals or []):
        if not isinstance(raw, dict):
            continue
        try:
            start = max(0.0, min(horizon, float(raw.get("start", 0.0))))
            end = max(0.0, min(horizon, float(raw.get("end", 0.0))))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        if end - start < 0.05:
            continue
        normalized.append({
            "name": str(raw.get("name") or f"机制{index + 1}"),
            "start": round(start, 3),
            "end": round(end, 3),
        })
    normalized.sort(key=lambda row: (row["start"], row["end"], row["name"]))
    result: list[dict[str, Any]] = []
    for row in normalized:
        if result and row["start"] < result[-1]["end"]:
            row = dict(row)
            row["start"] = result[-1]["end"]
        if row["end"] - row["start"] >= 0.05:
            result.append(row)
    return result


def merged_blocked_ranges(
    intervals: list[dict[str, Any]] | None,
    window: float,
) -> list[tuple[float, float]]:
    """Return an unnamed union suitable for simulation and cache keys."""
    ranges: list[list[float]] = []
    for row in normalize_mechanism_intervals(intervals, window):
        start = float(row["start"])
        end = float(row["end"])
        if ranges and start <= ranges[-1][1] + 1e-6:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    return [(start, end) for start, end in ranges]


def blocked_seconds(intervals: list[dict[str, Any]] | None, window: float) -> float:
    return sum(end - start for start, end in merged_blocked_ranges(intervals, window))


def active_seconds(intervals: list[dict[str, Any]] | None, window: float) -> float:
    return max(0.0, float(window) - blocked_seconds(intervals, window))


def active_output_ranges(
    intervals: list[dict[str, Any]] | None,
    window: float,
) -> list[tuple[float, float]]:
    horizon = max(1.0, float(window))
    output: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged_blocked_ranges(intervals, horizon):
        if start > cursor + 1e-6:
            output.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < horizon - 1e-6:
        output.append((cursor, horizon))
    return output


def _range_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(end, right) - max(start, left)) for left, right in ranges)


def _legal_cast_start(
    start: float,
    cast_time: float,
    blocked: list[tuple[float, float]],
    window: float,
) -> float | None:
    current = max(0.0, float(start))
    cast = max(0.01, float(cast_time))
    horizon = max(1.0, float(window))
    for block_start, block_end in blocked:
        if current + cast <= block_start + 1e-6:
            break
        if current < block_end - 1e-6 and current + cast > block_start + 1e-6:
            current = block_end
    return round(current, 3) if current + cast <= horizon + 1e-6 else None


def lightweight_cdr_buff_schedule(
    specs: list[dict[str, Any]],
    intervals: list[dict[str, Any]] | None,
    window: float,
    preferred_starts: dict[str, float] | None = None,
    locked_starts: dict[tuple[str, str, int], float] | None = None,
) -> list[dict[str, Any]]:
    """Create one shared CDR-buff schedule without running any damage timelines."""
    horizon = max(1.0, float(window))
    blocked = merged_blocked_ranges(intervals, horizon)
    active_ranges = active_output_ranges(intervals, horizon)
    preferred = {str(key): max(0.0, float(value)) for key, value in (preferred_starts or {}).items()}
    locks = {
        (str(provider).strip(), str(skill).strip(), int(occurrence)): max(0.0, float(start))
        for (provider, skill, occurrence), start in (locked_starts or {}).items()
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for raw in specs:
        skill = str(raw.get("skill") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        cooldown = max(0.01, float(raw.get("cooldown") or 0.0))
        duration = max(0.0, float(raw.get("duration") or 0.0))
        cast_time = max(0.01, float(raw.get("cast_time") or 0.5))
        resistance_group = acceleration_resistance_group(skill)
        if not resistance_group or not provider or duration <= 0:
            continue
        key = (provider, skill)
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(resistance_group, []).append({
            "provider": provider,
            "skill": skill,
            "cooldown": cooldown,
            "duration": duration,
            "cast_time": cast_time,
        })

    schedule: list[dict[str, Any]] = []
    for resistance_group in ("burst_resistance", "time_accel_resistance"):
        providers = sorted(
            grouped.get(resistance_group, []),
            key=lambda row: (
                preferred.get(row["skill"], 0.0),
                _provider_sort_key(row["provider"]),
                row["skill"],
            ),
        )
        if not providers:
            continue
        provider_state: list[dict[str, Any]] = []
        for index, spec in enumerate(providers):
            base_start = preferred.get(spec["skill"], 0.0)
            phase = spec["cooldown"] * index / len(providers)
            first_ready = base_start + phase
            provider_state.append({
                **spec,
                "ready": first_ready,
                "occurrence": 0,
            })
        resistance_ready = 0.0
        guard = 0
        while guard < 10000:
            guard += 1
            viable = [
                state
                for state in provider_state
                if state["ready"] < horizon - 1e-6
            ]
            if not viable:
                break
            state = min(viable, key=lambda row: (row["ready"], _provider_sort_key(row["provider"])))
            cast_time = state["cast_time"]
            duration = state["duration"]
            earliest = max(state["ready"], resistance_ready)
            lock_key = (state["provider"], state["skill"], state["occurrence"])
            locked_start = locks.get(lock_key)
            candidates = {max(earliest, locked_start)} if locked_start is not None else {earliest}
            if locked_start is None:
                for left, right in active_ranges:
                    candidates.update((left - cast_time, left, right - duration - cast_time))
            best: tuple[float, float] | None = None
            for candidate in candidates:
                if candidate < earliest - 1e-6:
                    continue
                legal = _legal_cast_start(candidate, cast_time, blocked, horizon)
                if legal is None or legal < earliest - 1e-6:
                    continue
                effect_start = legal + cast_time
                effect_end = min(horizon, effect_start + duration)
                coverage = _range_overlap(effect_start, effect_end, active_ranges)
                score = (coverage, -legal)
                if best is None or score > (best[0], -best[1]):
                    best = (coverage, legal)
            if best is None:
                state["ready"] = horizon
                continue
            cast_start = best[1]
            cast_end = min(horizon, cast_start + cast_time)
            effect_start = cast_end
            effect_end = min(horizon, effect_start + duration)
            if effect_end <= effect_start + 1e-6:
                state["ready"] = horizon
                continue
            schedule.append({
                "provider": state["provider"],
                "skill": state["skill"],
                "stack_group": resistance_group,
                "occurrence": state["occurrence"],
                "cast_start": round(cast_start, 3),
                "cast_end": round(cast_end, 3),
                "effect_start": round(effect_start, 3),
                "effect_end": round(effect_end, 3),
                "cooldown": state["cooldown"],
                "duration": duration,
                "cast_time": cast_time,
                "active_coverage": round(_range_overlap(effect_start, effect_end, active_ranges), 3),
                "locked": locked_start is not None,
                "resistance_group": resistance_group,
                "resistance_label": acceleration_resistance_label(resistance_group),
                "resistance_start": round(cast_start, 3),
                "resistance_end": round(min(horizon, cast_start + state["cooldown"]), 3),
            })
            state["occurrence"] += 1
            state["ready"] = cast_start + state["cooldown"]
            resistance_ready = cast_start + state["cooldown"]
    return sorted(
        schedule,
        key=lambda row: (row["cast_start"], row["skill"], _provider_sort_key(row["provider"])),
    )


def _timeline_damage_density(
    timelines: list[dict[str, Any]] | None,
    window: float,
    bin_seconds: float,
) -> tuple[dict[str, list[float]], list[float]]:
    """Convert skill segments into a compact per-job damage-density curve."""
    horizon = max(1.0, float(window))
    step = max(0.25, float(bin_seconds))
    size = max(1, int(horizon / step + 0.999999))
    by_job: dict[str, list[float]] = {}
    total = [0.0] * size
    for result in timelines or []:
        job = str(result.get("job") or "").strip()
        if not job:
            continue
        density = by_job.setdefault(job, [0.0] * size)
        damage_panel = max(0.0, float(result.get("damage_panel") or 0.0))
        hand = max(0.0, float(result.get("hand_multiplier") or 1.0))
        for segment in result.get("segments", []):
            if segment.get("timeline_hidden"):
                continue
            start = max(0.0, min(horizon, float(segment.get("start") or 0.0)))
            end = max(start, min(horizon, float(segment.get("end") or start)))
            output = max(0.0, float(segment.get("actual_damage") or 0.0))
            if output <= 0:
                output = max(0.0, float(segment.get("effective_score") or 0.0)) * damage_panel / 100.0 * hand
            if output <= 0:
                continue
            if end <= start + 1e-6:
                end = min(horizon, start + step)
            first = max(0, min(size - 1, int(start / step)))
            last = max(first, min(size - 1, int(max(start, end - 1e-9) / step)))
            duration = max(step, end - start)
            rate = output / duration
            for index in range(first, last + 1):
                left = index * step
                right = min(horizon, left + step)
                overlap = max(0.0, min(end, right) - max(start, left))
                if overlap <= 0 and first == last:
                    overlap = min(step, duration)
                value = rate * overlap
                density[index] += value
                total[index] += value
    return by_job, total


def _density_integral(
    density: list[float],
    start: float,
    end: float,
    bin_seconds: float,
) -> float:
    if end <= start + 1e-9 or not density:
        return 0.0
    step = max(0.25, float(bin_seconds))
    first = max(0, min(len(density) - 1, int(max(0.0, start) / step)))
    last = max(first, min(len(density) - 1, int(max(start, end - 1e-9) / step)))
    value = 0.0
    for index in range(first, last + 1):
        left = index * step
        right = left + step
        overlap = max(0.0, min(end, right) - max(start, left))
        if overlap > 0:
            value += density[index] * overlap / step
    return value


def _reserved_cast_start(
    start: float,
    cast_time: float,
    reservations: list[tuple[float, float]],
    blocked: list[tuple[float, float]],
    window: float,
) -> float | None:
    current = max(0.0, float(start))
    cast = max(0.01, float(cast_time))
    for _guard in range(64):
        legal = _legal_cast_start(current, cast, blocked, window)
        if legal is None:
            return None
        conflict = next(
            (
                (left, right)
                for left, right in sorted(reservations)
                if legal < right - 1e-6 and legal + cast > left + 1e-6
            ),
            None,
        )
        if conflict is None:
            return legal
        current = conflict[1]
    return None


def _team_buff_schedule_score(
    schedule: list[dict[str, Any]],
    by_job_density: dict[str, list[float]],
    total_density: list[float],
    window: float,
    bin_seconds: float,
) -> float:
    """Score a complete schedule using multiplicative team gains and cast cost."""
    if not schedule or not total_density:
        return 0.0
    horizon = max(1.0, float(window))
    step = max(0.25, float(bin_seconds))
    size = len(total_density)
    multipliers = {job: [1.0] * size for job in by_job_density}

    for row in schedule:
        effect_start = max(0.0, float(row.get("effect_start") or 0.0))
        effect_end = min(horizon, float(row.get("effect_end") or 0.0))
        if effect_end <= effect_start + 1e-9:
            continue
        first = max(0, min(size - 1, int(effect_start / step)))
        last = max(first, min(size - 1, int(max(effect_start, effect_end - 1e-9) / step)))
        for job, raw_weight in (row.get("job_weights") or {}).items():
            values = multipliers.get(str(job).strip())
            weight = max(0.0, float(raw_weight or 0.0))
            if values is None or weight <= 1e-12:
                continue
            factor = 1.0 + weight
            for index in range(first, last + 1):
                values[index] *= factor

    score = 0.0
    for job, density in by_job_density.items():
        multiplier = multipliers[job]
        score += sum(
            value * max(0.0, multiplier[index] - 1.0)
            for index, value in enumerate(density)
        )
    for row in schedule:
        cast_start = max(0.0, float(row.get("cast_start") or 0.0))
        cast_end = min(horizon, float(row.get("cast_end") or cast_start))
        provider_density = by_job_density.get(
            _provider_job_name(row.get("provider")),
            total_density,
        )
        score -= _density_integral(provider_density, cast_start, cast_end, step)
    return score


def _restricted_joint_optimize_team_buff_schedule(
    schedule: list[dict[str, Any]],
    by_job_density: dict[str, list[float]],
    total_density: list[float],
    active_ranges: list[tuple[float, float]],
    blocked: list[tuple[float, float]],
    window: float,
    bin_seconds: float,
    *,
    max_passes: int = 2,
) -> list[dict[str, Any]]:
    """Improve a stable schedule without changing casts, groups or legality."""
    if not schedule or not total_density:
        return schedule
    horizon = max(1.0, float(window))
    step = max(0.25, float(bin_seconds))
    size = len(total_density)
    multipliers = {job: [1.0] * size for job in by_job_density}

    def interval_bins(start: float, end: float):
        if end <= start + 1e-9:
            return
        first = max(0, min(size - 1, int(max(0.0, start) / step)))
        last = max(first, min(size - 1, int(max(start, end - 1e-9) / step)))
        for index in range(first, last + 1):
            left = index * step
            right = min(horizon, left + step)
            overlap = max(0.0, min(end, right) - max(start, left))
            if overlap > 1e-9:
                yield index, overlap / step

    def apply_row(row: dict[str, Any], *, add: bool) -> None:
        weights = row.get("job_weights") or {}
        for job, raw_weight in weights.items():
            values = multipliers.get(str(job).strip())
            weight = max(0.0, float(raw_weight or 0.0))
            factor = 1.0 + weight
            if values is None or factor <= 1.0 + 1e-12:
                continue
            for index, _fraction in interval_bins(
                float(row.get("effect_start") or 0.0),
                float(row.get("effect_end") or 0.0),
            ):
                if add:
                    values[index] *= factor
                else:
                    values[index] /= factor

    def utility(
        row: dict[str, Any],
        cast_start: float,
        marginal_density: list[float],
    ) -> float:
        cast_time = max(0.01, float(row.get("cast_time") or 0.5))
        duration = max(0.0, float(row.get("duration") or 0.0))
        cast_end = min(horizon, cast_start + cast_time)
        effect_start = cast_end
        effect_end = min(horizon, effect_start + duration)
        gain = _density_integral(
            marginal_density,
            effect_start,
            effect_end,
            step,
        )
        provider_job = _provider_job_name(row.get("provider"))
        provider_density = by_job_density.get(provider_job, total_density)
        cast_cost = _density_integral(provider_density, cast_start, cast_end, step)
        return gain - cast_cost

    for row in schedule:
        apply_row(row, add=True)

    for _pass in range(max(0, int(max_passes))):
        changed = False
        for row in sorted(schedule, key=lambda item: float(item.get("cast_start") or 0.0)):
            if row.get("locked") or not row.get("job_weights"):
                continue
            provider = str(row.get("provider") or "").strip()
            skill = str(row.get("skill") or "").strip()
            group = str(row.get("stack_group") or skill).strip()
            resistance_group = str(row.get("resistance_group") or "").strip()
            occurrence = int(row.get("occurrence") or 0)
            cooldown = max(0.01, float(row.get("cooldown") or 0.0))
            cast_time = max(0.01, float(row.get("cast_time") or 0.5))
            duration = max(0.0, float(row.get("duration") or 0.0))
            current_start = max(0.0, float(row.get("cast_start") or 0.0))
            apply_row(row, add=False)

            same_skill = [
                other for other in schedule
                if other is not row
                and str(other.get("provider") or "").strip() == provider
                and str(other.get("skill") or "").strip() == skill
            ]
            previous = max(
                (
                    other for other in same_skill
                    if int(other.get("occurrence") or 0) < occurrence
                ),
                key=lambda item: int(item.get("occurrence") or 0),
                default=None,
            )
            following = min(
                (
                    other for other in same_skill
                    if int(other.get("occurrence") or 0) > occurrence
                ),
                key=lambda item: int(item.get("occurrence") or 0),
                default=None,
            )
            earliest = (
                max(0.0, float(previous.get("cast_start") or 0.0) + cooldown)
                if previous is not None
                else 0.0
            )
            latest = (
                float(following.get("cast_start") or horizon) - cooldown
                if following is not None
                else horizon - cast_time
            )
            weighted_density = [0.0] * size
            for job, raw_weight in (row.get("job_weights") or {}).items():
                job_key = str(job).strip()
                source = by_job_density.get(job_key)
                other_multiplier = multipliers.get(job_key)
                weight = max(0.0, float(raw_weight or 0.0))
                if source is None or other_multiplier is None or weight <= 0:
                    continue
                for index, value in enumerate(source):
                    weighted_density[index] += value * weight * other_multiplier[index]
            if latest < earliest - 1e-6:
                row["estimated_gain"] = round(
                    utility(row, current_start, weighted_density),
                    3,
                )
                apply_row(row, add=True)
                continue
            candidates = {current_start, earliest, latest}
            for offset in (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0):
                candidates.add(current_start + offset)
            for left, right in active_ranges:
                candidates.update({
                    left - cast_time,
                    right - duration - cast_time,
                })
            peak_limit = 20 if horizon <= 180.0 + 1e-6 else 10
            peak_indexes = sorted(
                range(size),
                key=lambda index: weighted_density[index],
                reverse=True,
            )[:peak_limit]
            candidates.update(index * step - cast_time for index in peak_indexes)
            provider_reservations = [
                (
                    max(0.0, float(other.get("cast_start") or 0.0)),
                    max(0.0, float(other.get("cast_end") or 0.0)),
                )
                for other in schedule
                if other is not row
                and str(other.get("provider") or "").strip() == provider
            ]
            group_windows = [
                (
                    max(
                        0.0,
                        float(
                            other.get("resistance_start")
                            if resistance_group
                            else other.get("effect_start")
                            or 0.0
                        ),
                    ),
                    max(
                        0.0,
                        float(
                            other.get("resistance_end")
                            if resistance_group
                            else other.get("effect_end")
                            or 0.0
                        ),
                    ),
                )
                for other in schedule
                if other is not row
                and str(other.get("stack_group") or other.get("skill") or "").strip() == group
            ]
            best_start = current_start
            best_utility = utility(row, current_start, weighted_density)
            for candidate in candidates:
                candidate = max(earliest, min(latest, float(candidate)))
                legal = _reserved_cast_start(
                    candidate,
                    cast_time,
                    provider_reservations,
                    blocked,
                    horizon,
                )
                if legal is None or legal < earliest - 1e-6 or legal > latest + 1e-6:
                    continue
                effect_start = legal + cast_time
                effect_end = min(horizon, effect_start + duration)
                occupied_start = legal if resistance_group else effect_start
                occupied_end = (
                    legal + cooldown if resistance_group else effect_end
                )
                if any(
                    occupied_start < right - 1e-6
                    and occupied_end > left + 1e-6
                    for left, right in group_windows
                ):
                    continue
                candidate_utility = utility(row, legal, weighted_density)
                if (candidate_utility, -legal) > (best_utility + 1e-6, -best_start):
                    best_start = legal
                    best_utility = candidate_utility
            if abs(best_start - current_start) > 1e-6:
                changed = True
                row["cast_start"] = round(best_start, 3)
                row["cast_end"] = round(min(horizon, best_start + cast_time), 3)
                row["effect_start"] = row["cast_end"]
                row["effect_end"] = round(min(horizon, row["effect_start"] + duration), 3)
                if resistance_group:
                    row["resistance_start"] = row["cast_start"]
                    row["resistance_end"] = round(
                        min(horizon, best_start + cooldown),
                        3,
                    )
                row["active_coverage"] = round(
                    _range_overlap(row["effect_start"], row["effect_end"], active_ranges),
                    3,
                )
            row["estimated_gain"] = round(best_utility, 3)
            apply_row(row, add=True)
        if not changed:
            break
    return sorted(
        schedule,
        key=lambda row: (row["cast_start"], row["skill"], _provider_sort_key(row["provider"])),
    )


def lightweight_team_buff_schedule(
    specs: list[dict[str, Any]],
    intervals: list[dict[str, Any]] | None,
    window: float,
    timelines: list[dict[str, Any]] | None,
    *,
    seed_schedule: list[dict[str, Any]] | None = None,
    preferred_starts: dict[str, float] | None = None,
    locked_starts: dict[tuple[str, str, int], float] | None = None,
    joint_optimize: bool = True,
) -> list[dict[str, Any]]:
    """Build a stable team-buff axis, then apply restricted joint optimization.

    Stage one greedily creates a legal, deterministic schedule. Stage two only
    shifts those casts inside cooldown, provider, stack-group and mechanic
    constraints. It scores all active buffs multiplicatively and falls back to
    stage one if the restricted search cannot improve the complete schedule.
    """
    horizon = max(1.0, float(window))
    bin_seconds = 0.5 if horizon <= 180.0 + 1e-6 else 1.0
    blocked = merged_blocked_ranges(intervals, horizon)
    active_ranges = active_output_ranges(intervals, horizon)
    preferred = {
        str(key).strip(): max(0.0, float(value))
        for key, value in (preferred_starts or {}).items()
    }
    locks = {
        (str(provider).strip(), str(skill).strip(), int(occurrence)): max(0.0, float(start))
        for (provider, skill, occurrence), start in (locked_starts or {}).items()
    }
    by_job_density, total_density = _timeline_damage_density(timelines, horizon, bin_seconds)
    for left, right in blocked:
        first = max(0, int(left / bin_seconds))
        last = min(len(total_density), int(right / bin_seconds + 0.999999))
        for density in [total_density, *by_job_density.values()]:
            for index in range(first, last):
                density[index] = 0.0

    schedule = [dict(row) for row in (seed_schedule or [])]
    provider_reservations: dict[str, list[tuple[float, float]]] = {}
    group_windows: dict[str, list[tuple[float, float]]] = {}
    seeded_skills: set[tuple[str, str]] = set()
    for row in schedule:
        provider = str(row.get("provider") or "").strip()
        skill = str(row.get("skill") or "").strip()
        resistance_group = acceleration_resistance_group(skill)
        group = resistance_group or str(row.get("stack_group") or skill).strip()
        row["stack_group"] = group
        if resistance_group:
            row["resistance_group"] = resistance_group
        cast_start = max(0.0, float(row.get("cast_start") or 0.0))
        cast_end = max(cast_start, float(row.get("cast_end") or cast_start))
        effect_start = max(cast_end, float(row.get("effect_start") or cast_end))
        effect_end = max(effect_start, float(row.get("effect_end") or effect_start))
        provider_reservations.setdefault(provider, []).append((cast_start, cast_end))
        group_windows.setdefault(group, []).append(
            (
                cast_start,
                max(cast_start, float(row.get("resistance_end") or cast_start + float(row.get("cooldown") or 0.0))),
            )
            if resistance_group
            else (effect_start, effect_end)
        )
        seeded_skills.add((provider, skill))

    group_candidates: dict[str, set[str]] = {}
    for raw in specs:
        skill = str(raw.get("skill") or "").strip()
        if not skill:
            continue
        group_candidates.setdefault(skill, set()).add(
            str(raw.get("stack_group") or skill).strip()
        )
    canonical_group = {
        skill: min(groups, key=lambda group: (group == skill, group))
        for skill, groups in group_candidates.items()
    }

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in specs:
        provider = str(raw.get("provider") or "").strip()
        skill = str(raw.get("skill") or "").strip()
        if not provider or not skill or (provider, skill) in seeded_skills:
            continue
        cooldown = max(0.01, float(raw.get("cooldown") or 0.0))
        duration = max(0.0, float(raw.get("duration") or 0.0))
        cast_time = max(0.01, float(raw.get("cast_time") or 0.5))
        if duration <= 0 or cooldown <= 0:
            continue
        key = (provider, skill)
        if key in seen:
            continue
        seen.add(key)
        job_weights = {
            str(job).strip(): max(0.0, float(value))
            for job, value in dict(raw.get("job_weights") or {}).items()
            if str(job).strip() and float(value or 0.0) > 1e-9
        }
        strength = max(0.001, float(raw.get("strength") or 1.0))
        resistance_group = acceleration_resistance_group(skill)
        normalized.append({
            "provider": provider,
            "skill": skill,
            # The same named team effect never stacks, even if a malformed
            # source row supplied different group labels for two providers.
            "stack_group": resistance_group or canonical_group.get(skill, skill),
            "resistance_group": resistance_group,
            "cooldown": cooldown,
            "duration": duration,
            "cast_time": cast_time,
            "strength": strength,
            "job_weights": job_weights,
        })

    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in normalized:
        groups.setdefault(spec["stack_group"], []).append(spec)

    def density_for(spec: dict[str, Any]) -> list[float]:
        weights = spec["job_weights"]
        if not weights:
            return [value * spec["strength"] for value in total_density]
        result = [0.0] * len(total_density)
        for job, weight in weights.items():
            source = by_job_density.get(job)
            if source is None:
                continue
            for index, value in enumerate(source):
                result[index] += value * weight
        return result

    group_order = sorted(
        groups,
        key=lambda group: max(
            sum(density_for(spec)) for spec in groups[group]
        ),
        reverse=True,
    )
    for group in group_order:
        states = []
        for spec in sorted(
            groups[group],
            key=lambda row: (-row["strength"], _provider_sort_key(row["provider"])),
        ):
            density = density_for(spec)
            peak_limit = 24 if horizon <= 180.0 + 1e-6 else 12
            # A long plateau near the start can occupy every global top-bin
            # slot. Keep density transition points as well, otherwise later
            # burst phases never become candidates and a recurring buff may
            # incorrectly stop after its first cast.
            transition_indices = {
                index
                for index, value in enumerate(density)
                if value > 1e-9
                and (
                    index == 0
                    or abs(value - density[index - 1]) > 1e-9
                )
            }
            peak_indices = transition_indices | set(sorted(
                range(len(density)),
                key=lambda index: density[index],
                reverse=True,
            )[:peak_limit])
            peak_cast_candidates = tuple(
                index * bin_seconds - spec["cast_time"]
                for index in sorted(peak_indices)
            )
            states.append({
                **spec,
                # A preferred start is a candidate phase, not a lower bound.
                # Mechanism axes must be free to move the first cast to an
                # earlier damage peak and then continue using the buff by CD.
                "ready": 0.0,
                "preferred_start": preferred.get(spec["skill"], 0.0),
                "occurrence": 0,
                "density": density,
                "peak_cast_candidates": peak_cast_candidates,
            })
        existing = group_windows.setdefault(group, [])
        previous_effect_end = max((right for _left, right in existing), default=0.0)
        guard = 0
        while guard < 10000:
            guard += 1
            viable = [state for state in states if state["ready"] < horizon - 1e-6]
            if not viable:
                break
            state = min(
                viable,
                key=lambda row: (row["ready"], -row["strength"], _provider_sort_key(row["provider"])),
            )
            ready = max(
                state["ready"],
                previous_effect_end
                if state.get("resistance_group")
                else previous_effect_end - state["cast_time"],
            )
            lock_key = (state["provider"], state["skill"], state["occurrence"])
            locked = locks.get(lock_key)
            candidates = {max(ready, locked)} if locked is not None else {ready}
            if locked is None:
                preferred_candidate = (
                    state["preferred_start"]
                    + state["occurrence"] * state["cooldown"]
                )
                if ready - 1e-6 <= preferred_candidate < horizon - 1e-6:
                    candidates.add(preferred_candidate)
                for left, right in active_ranges:
                    candidates.update({
                        left - state["cast_time"],
                        right - state["duration"] - state["cast_time"],
                    })
                candidates.update(state["peak_cast_candidates"])

            mean_density = sum(state["density"]) / max(1, len(state["density"]))
            reference_gain = mean_density * max(bin_seconds, state["duration"])
            evaluated: list[tuple[float, float, float, float, bool]] = []
            for candidate in candidates:
                if candidate < ready - 1e-6:
                    continue
                cast_start = _reserved_cast_start(
                    candidate,
                    state["cast_time"],
                    provider_reservations.get(state["provider"], []),
                    blocked,
                    horizon,
                )
                if cast_start is None or cast_start < ready - 1e-6:
                    continue
                cast_end = min(horizon, cast_start + state["cast_time"])
                effect_start = cast_end
                effect_end = min(horizon, effect_start + state["duration"])
                if effect_end <= effect_start + 1e-6:
                    continue
                benefit = _density_integral(
                    state["density"], effect_start, effect_end, bin_seconds
                )
                provider_job = state["provider"].split(" ", 1)[-1].strip()
                provider_density = by_job_density.get(provider_job, total_density)
                cast_cost = _density_integral(
                    provider_density, cast_start, cast_end, bin_seconds
                )
                # Waiting through a boss mechanic is free: cooldowns keep
                # flowing while nobody can deal damage. Only active output
                # time spent waiting is a real scheduling cost.
                active_delay = _range_overlap(ready, cast_start, active_ranges)
                # Delaying through usable output time may reduce the number of
                # casts available over the whole encounter. Price that lost
                # fraction of a use at its average expected benefit. Waiting
                # through a stop interval remains free because no output was
                # sacrificed there.
                delay_cost = reference_gain * active_delay / max(
                    state["cooldown"], state["duration"], 1.0
                )
                active_coverage = _range_overlap(effect_start, effect_end, active_ranges)
                score = benefit - cast_cost - delay_cost
                fully_active = (
                    effect_end - effect_start >= state["duration"] - 1e-6
                    and active_coverage >= state["duration"] - 1e-6
                )
                evaluated.append((score, benefit, active_coverage, cast_start, fully_active))
            if not evaluated:
                state["ready"] = horizon
                continue
            best = max(
                evaluated,
                key=lambda row: (row[0], row[1], row[2], -row[3]),
            )
            if best is None or (best[1] <= 1e-9 and locked is None):
                state["ready"] = horizon
                continue
            cast_start = best[3]
            cast_end = min(horizon, cast_start + state["cast_time"])
            effect_start = cast_end
            effect_end = min(horizon, effect_start + state["duration"])
            row = {
                "provider": state["provider"],
                "skill": state["skill"],
                "stack_group": group,
                "occurrence": state["occurrence"],
                "cast_start": round(cast_start, 3),
                "cast_end": round(cast_end, 3),
                "effect_start": round(effect_start, 3),
                "effect_end": round(effect_end, 3),
                "cooldown": state["cooldown"],
                "duration": state["duration"],
                "cast_time": state["cast_time"],
                "active_coverage": round(best[2], 3),
                "estimated_gain": round(best[0], 3),
                "job_weights": dict(state["job_weights"]),
                "locked": locked is not None,
            }
            if state.get("resistance_group"):
                row.update({
                    "resistance_group": state["resistance_group"],
                    "resistance_label": acceleration_resistance_label(
                        state["resistance_group"]
                    ),
                    "resistance_start": round(cast_start, 3),
                    "resistance_end": round(
                        min(horizon, cast_start + state["cooldown"]),
                        3,
                    ),
                })
            schedule.append(row)
            provider_reservations.setdefault(state["provider"], []).append((cast_start, cast_end))
            occupied_end = (
                cast_start + state["cooldown"]
                if state.get("resistance_group")
                else effect_end
            )
            existing.append((cast_start, occupied_end) if state.get("resistance_group") else (effect_start, effect_end))
            previous_effect_end = occupied_end
            state["occurrence"] += 1
            state["ready"] = cast_start + state["cooldown"]

    stable_schedule = sorted(
        schedule,
        key=lambda row: (row["cast_start"], row["skill"], _provider_sort_key(row["provider"])),
    )
    for row in stable_schedule:
        row["stable_cast_start"] = round(float(row.get("cast_start") or 0.0), 3)
        row["optimizer_stage"] = "stable"
    if not joint_optimize:
        return stable_schedule

    stable_score = _team_buff_schedule_score(
        stable_schedule,
        by_job_density,
        total_density,
        horizon,
        bin_seconds,
    )
    optimized = _restricted_joint_optimize_team_buff_schedule(
        [dict(row) for row in stable_schedule],
        by_job_density,
        total_density,
        active_ranges,
        blocked,
        horizon,
        bin_seconds,
        max_passes=2 if horizon <= 180.0 + 1e-6 else 1,
    )
    optimized_score = _team_buff_schedule_score(
        optimized,
        by_job_density,
        total_density,
        horizon,
        bin_seconds,
    )
    if optimized_score + 1e-6 < stable_score:
        return stable_schedule
    for row in optimized:
        row["optimizer_stage"] = "stable+restricted_joint"
        row["optimization_delta"] = round(
            float(row.get("cast_start") or 0.0)
            - float(row.get("stable_cast_start") or 0.0),
            3,
        )
    return optimized


def mechanism_signature(
    intervals: list[dict[str, Any]] | None,
    window: float,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (round(start, 3), round(end, 3))
        for start, end in merged_blocked_ranges(intervals, window)
    )


def load_mechanism_templates() -> dict[str, list[dict[str, Any]]]:
    result = copy.deepcopy(BUILTIN_MECHANISM_TEMPLATES)
    try:
        payload = json.loads(MECHANISM_TEMPLATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(payload, dict):
        return result
    for name, intervals in payload.items():
        if str(name).strip() and isinstance(intervals, list):
            result[str(name).strip()] = copy.deepcopy(intervals)
    return result


def save_mechanism_templates(templates: dict[str, list[dict[str, Any]]]) -> None:
    MECHANISM_TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MECHANISM_TEMPLATE_PATH.write_text(
        json.dumps(templates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class MechanismTimelineEditor(QWidget):
    """Visual editor for named no-output intervals on a fixed encounter axis."""

    intervalsChanged = Signal(object)
    selectionChanged = Signal(int)
    historyChanged = Signal(bool, bool)
    timeHovered = Signal(float)
    intervalSelectionChanged = Signal(float, float, str)

    def __init__(self, window_seconds: float = 125.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.window_seconds = max(1.0, float(window_seconds))
        self.intervals: list[dict[str, Any]] = []
        self.selected_index = -1
        self._undo: list[list[dict[str, Any]]] = []
        self._redo: list[list[dict[str, Any]]] = []
        self._drag_mode = ""
        self._drag_index = -1
        self._drag_anchor = 0.0
        self._drag_origin: dict[str, Any] | None = None
        self._interaction_before: list[dict[str, Any]] | None = None
        self._hover_seconds: float | None = None
        self._selecting_range = False
        self._selection_anchor_seconds = 0.0
        self._selection_range: tuple[float, float] | None = None
        self.row_seconds = 180.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._refresh_height()
        self.setToolTip(
            "悬停查看时刻；Shift+拖动选择统计区间；普通拖动仍用于创建或调整机制区间"
        )

    @staticmethod
    def _copy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return copy.deepcopy(rows)

    def set_window_seconds(self, seconds: float) -> None:
        self.window_seconds = max(1.0, float(seconds))
        self.intervals = normalize_mechanism_intervals(self.intervals, self.window_seconds)
        self._refresh_height()
        self.update()

    def _row_count(self) -> int:
        return max(1, int((self.window_seconds + self.row_seconds - 1e-6) // self.row_seconds))

    def _refresh_height(self) -> None:
        height = 48 + self._row_count() * 62 + 28
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)

    def set_intervals(self, intervals: list[dict[str, Any]], *, record: bool = False) -> None:
        before = self._copy_rows(self.intervals)
        normalized = normalize_mechanism_intervals(intervals, self.window_seconds)
        if normalized == self.intervals:
            return
        if record:
            self._push_undo(before)
        self.intervals = normalized
        self.selected_index = min(self.selected_index, len(self.intervals) - 1)
        self._emit_change()

    def add_interval(self, start: float | None = None, end: float | None = None) -> None:
        duration = min(10.0, self.window_seconds)
        if start is None:
            start = self.intervals[-1]["end"] + 2.0 if self.intervals else min(20.0, self.window_seconds / 3)
        if end is None:
            end = min(self.window_seconds, float(start) + duration)
        rows = self._copy_rows(self.intervals)
        rows.append({"name": f"机制{len(rows) + 1}", "start": start, "end": end})
        self.set_intervals(rows, record=True)
        self.selected_index = len(self.intervals) - 1
        self.selectionChanged.emit(self.selected_index)

    def remove_selected(self) -> None:
        if not (0 <= self.selected_index < len(self.intervals)):
            return
        before = self._copy_rows(self.intervals)
        del self.intervals[self.selected_index]
        self._push_undo(before)
        self.selected_index = min(self.selected_index, len(self.intervals) - 1)
        self._emit_change()

    def clear_intervals(self) -> None:
        if not self.intervals:
            return
        self._push_undo(self._copy_rows(self.intervals))
        self.intervals = []
        self.selected_index = -1
        self._emit_change()

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self._copy_rows(self.intervals))
        self.intervals = self._undo.pop()
        self.selected_index = min(self.selected_index, len(self.intervals) - 1)
        self._emit_change(push_history=False)

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self._copy_rows(self.intervals))
        self.intervals = self._redo.pop()
        self.selected_index = min(self.selected_index, len(self.intervals) - 1)
        self._emit_change(push_history=False)

    def _push_undo(self, rows: list[dict[str, Any]]) -> None:
        self._undo.append(self._copy_rows(rows))
        self._undo = self._undo[-60:]
        self._redo.clear()
        self.historyChanged.emit(bool(self._undo), bool(self._redo))

    def _emit_change(self, *, push_history: bool = False) -> None:
        del push_history
        self.intervals = normalize_mechanism_intervals(self.intervals, self.window_seconds)
        self.intervalsChanged.emit(self._copy_rows(self.intervals))
        self.historyChanged.emit(bool(self._undo), bool(self._redo))
        self.update()

    def _track_rects(self) -> list[QRectF]:
        width = max(80, self.width() - 76)
        return [QRectF(58, 38 + row * 62, width, 38) for row in range(self._row_count())]

    def _track_rect(self) -> QRectF:
        return self._track_rects()[0]

    def _track_row_at(self, x: float, y: float) -> int:
        for row, rect in enumerate(self._track_rects()):
            if rect.contains(x, y):
                return row
        return -1

    def _time_at_position(
        self,
        x: float,
        y: float,
        modifiers: Qt.KeyboardModifiers = Qt.NoModifier,
    ) -> float:
        row = self._track_row_at(x, y)
        if row < 0:
            row = max(0, min(self._row_count() - 1, int((y - 38) // 62)))
        rect = self._track_rects()[row]
        ratio = max(0.0, min(1.0, (x - rect.left()) / max(1.0, rect.width())))
        raw = row * self.row_seconds + ratio * self.row_seconds
        step = 0.1 if modifiers & Qt.AltModifier else 0.5
        return max(0.0, min(self.window_seconds, round(raw / step) * step))

    def _time_at_x(self, x: float, modifiers: Qt.KeyboardModifiers = Qt.NoModifier) -> float:
        return self._time_at_position(x, self._track_rect().center().y(), modifiers)

    def _x_at_time(self, seconds: float) -> float:
        seconds = max(0.0, min(self.window_seconds, seconds))
        row = min(self._row_count() - 1, int(seconds // self.row_seconds))
        local = seconds - row * self.row_seconds
        rect = self._track_rects()[row]
        return rect.left() + local / self.row_seconds * rect.width()

    def _interval_rects(self, row: dict[str, Any]) -> list[QRectF]:
        start = max(0.0, float(row["start"]))
        end = min(self.window_seconds, float(row["end"]))
        output: list[QRectF] = []
        first_row = min(self._row_count() - 1, int(start // self.row_seconds))
        last_probe = max(start, end - 1e-6)
        last_row = min(self._row_count() - 1, int(last_probe // self.row_seconds))
        tracks = self._track_rects()
        for track_row in range(first_row, last_row + 1):
            row_start = track_row * self.row_seconds
            left_time = max(start, row_start)
            right_time = min(end, row_start + self.row_seconds)
            if right_time <= left_time + 1e-9:
                continue
            track = tracks[track_row]
            left = track.left() + (left_time - row_start) / self.row_seconds * track.width()
            right = track.left() + (right_time - row_start) / self.row_seconds * track.width()
            output.append(QRectF(left, track.top(), max(4.0, right - left), track.height()))
        return output

    def _interval_rect(self, row: dict[str, Any]) -> QRectF:
        rects = self._interval_rects(row)
        return rects[0] if rects else QRectF()

    def clear_interval_selection(self) -> None:
        self._selection_range = None
        self.intervalSelectionChanged.emit(0.0, 0.0, "区间：未选择")
        self.update()

    def _selection_summary(self) -> str:
        if self._selection_range is None:
            return "区间：未选择"
        start, end = sorted(self._selection_range)
        duration = max(0.0, end - start)
        stopped = sum(
            max(
                0.0,
                min(end, float(row["end"]))
                - max(start, float(row["start"])),
            )
            for row in self.intervals
        )
        return (
            f"区间 {start:.2f}s - {end:.2f}s（{duration:.2f}s）  "
            f"停止输出 {stopped:.2f}s  可输出 {max(0.0, duration - stopped):.2f}s"
        )

    def _emit_interval_selection(self) -> None:
        if self._selection_range is None:
            self.intervalSelectionChanged.emit(0.0, 0.0, "区间：未选择")
            return
        start, end = sorted(self._selection_range)
        self.intervalSelectionChanged.emit(start, end, self._selection_summary())

    def _draw_interval_selection(self, painter: QPainter) -> None:
        if self._selection_range is None:
            return
        start, end = sorted(self._selection_range)
        parts = self._interval_rects({"start": start, "end": end})
        fill = QColor("#0f8fa6")
        fill.setAlpha(34)
        for rect in parts:
            painter.fillRect(rect, fill)
            painter.setPen(QPen(QColor("#0f8fa6"), 1, Qt.DashLine))
            painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        if parts and parts[0].width() >= 100.0:
            rect = parts[0]
            summary_rect = QRectF(
                rect.left() + 4.0,
                rect.top() + 4.0,
                rect.width() - 8.0,
                20.0,
            )
            painter.fillRect(summary_rect, QColor("#e7f5f7"))
            painter.setPen(QColor("#0f6575"))
            painter.drawText(
                summary_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    self._selection_summary(),
                    Qt.ElideRight,
                    max(1, int(summary_rect.width() - 8.0)),
                ),
            )

    def _draw_crosshair(self, painter: QPainter) -> None:
        if self._hover_seconds is None:
            return
        seconds = max(0.0, min(self.window_seconds, self._hover_seconds))
        row = min(self._row_count() - 1, int(seconds // self.row_seconds))
        track = self._track_rects()[row]
        x = self._x_at_time(seconds)
        painter.setPen(QPen(QColor("#0f8fa6"), 1, Qt.DashLine))
        painter.drawLine(x, track.top(), x, track.bottom())
        label_rect = QRectF(
            max(track.left(), min(track.right() - 64.0, x - 32.0)),
            track.top() - 23.0,
            64.0,
            20.0,
        )
        painter.fillRect(label_rect, QColor("#e7f5f7"))
        painter.setPen(QColor("#0f6575"))
        painter.drawText(label_rect, Qt.AlignCenter, f"{seconds:.2f}s")

    def _hit_interval(self, x: float, y: float) -> tuple[int, str]:
        for index in range(len(self.intervals) - 1, -1, -1):
            rects = self._interval_rects(self.intervals[index])
            for part_index, rect in enumerate(rects):
                if not rect.adjusted(-4, -3, 4, 3).contains(x, y):
                    continue
                if part_index == 0 and abs(x - rect.left()) <= 8:
                    return index, "resize_left"
                if part_index == len(rects) - 1 and abs(x - rect.right()) <= 8:
                    return index, "resize_right"
                return index, "move"
        return -1, ""

    def _bounded_values(self, index: int, start: float, end: float) -> tuple[float, float]:
        minimum = 0.0 if index <= 0 else float(self.intervals[index - 1]["end"])
        maximum = self.window_seconds if index + 1 >= len(self.intervals) else float(self.intervals[index + 1]["start"])
        start = max(minimum, min(maximum - 0.1, start))
        end = max(start + 0.1, min(maximum, end))
        return round(start, 3), round(end, 3)

    def _free_move_values(self, start: float, end: float, delta: float) -> tuple[float, float]:
        """Move an interval across neighboring intervals while retaining its width."""
        duration = max(0.1, float(end) - float(start))
        moved_start = max(0.0, min(self.window_seconds - duration, float(start) + float(delta)))
        return round(moved_start, 3), round(moved_start + duration, 3)

    def _settle_moved_interval(self, index: int, original_start: float) -> dict[str, Any] | None:
        """Snap an overlapping drop beyond the crossed interval without shrinking it."""
        if not (0 <= index < len(self.intervals)):
            return None
        moved = dict(self.intervals[index])
        duration = max(0.1, float(moved["end"]) - float(moved["start"]))
        moving_right = float(moved["start"]) >= float(original_start)
        others = [dict(row) for row_index, row in enumerate(self.intervals) if row_index != index]
        others.sort(key=lambda row: (float(row["start"]), float(row["end"])))
        candidate = float(moved["start"])
        if moving_right:
            for row in others:
                if candidate < float(row["end"]) and candidate + duration > float(row["start"]):
                    candidate = float(row["end"])
        else:
            for row in reversed(others):
                if candidate < float(row["end"]) and candidate + duration > float(row["start"]):
                    candidate = float(row["start"]) - duration
        candidate = max(0.0, min(self.window_seconds - duration, candidate))
        moved["start"] = round(candidate, 3)
        moved["end"] = round(candidate + duration, 3)
        self.intervals = others + [moved]
        return moved

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#f8fafc"))
        tracks = self._track_rects()
        for track_row, track in enumerate(tracks):
            row_start = track_row * self.row_seconds
            row_end = min(self.window_seconds, row_start + self.row_seconds)
            painter.setPen(QPen(QColor("#8fbca7"), 1))
            painter.setBrush(QColor("#dff3e8"))
            painter.drawRoundedRect(track, 7, 7)
            painter.setPen(QColor("#356b52"))
            painter.drawText(QRectF(4, track.top(), 48, track.height()), Qt.AlignRight | Qt.AlignVCenter, f"{track_row + 1}行")
            for tick in range(0, 181, 30):
                absolute = row_start + tick
                if absolute > row_end + 1e-6:
                    continue
                x = track.left() + tick / self.row_seconds * track.width()
                painter.setPen(QPen(QColor("#a9b7c6"), 1))
                painter.drawLine(int(x), int(track.top()), int(x), int(track.bottom()))
                painter.setPen(QColor("#526170"))
                painter.drawText(QRectF(x - 22, track.top() - 20, 44, 18), Qt.AlignCenter, f"{int(absolute)}s")

        for index, row in enumerate(self.intervals):
            selected = index == self.selected_index
            rects = self._interval_rects(row)
            for part_index, rect in enumerate(rects):
                painter.setPen(QPen(QColor("#be3f50" if selected else "#c96770"), 2 if selected else 1))
                painter.setBrush(QColor("#f1c9cd" if selected else "#f5dadd"))
                painter.drawRoundedRect(rect, 6, 6)
                painter.setPen(QColor("#7f2633"))
                text = painter.fontMetrics().elidedText(str(row["name"]), Qt.ElideRight, max(10, int(rect.width() - 12)))
                painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignCenter, text)
                if not selected:
                    continue
                painter.setBrush(QColor("#be3f50"))
                painter.setPen(Qt.NoPen)
                if part_index == 0:
                    painter.drawRoundedRect(QRectF(rect.left() - 3, rect.top() + 8, 6, rect.height() - 16), 2, 2)
                if part_index == len(rects) - 1:
                    painter.drawRoundedRect(QRectF(rect.right() - 3, rect.top() + 8, 6, rect.height() - 16), 2, 2)

        self._draw_interval_selection(painter)
        self._draw_crosshair(painter)

        painter.setPen(QColor("#64748b"))
        painter.drawText(
            QRectF(tracks[0].left(), tracks[-1].bottom() + 6, tracks[0].width(), 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            "拖动空白处创建停止区间；双击改名；Delete/Backspace删除；Ctrl+C/V复制粘贴；Alt精确到0.1秒",
        )

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        x = event.position().x()
        y = event.position().y()
        self.setFocus(Qt.MouseFocusReason)
        if (
            event.modifiers() & Qt.ShiftModifier
            and self._track_row_at(x, y) >= 0
        ):
            seconds = self._time_at_position(x, y, Qt.NoModifier)
            self._selecting_range = True
            self._selection_anchor_seconds = seconds
            self._selection_range = (seconds, seconds)
            self._hover_seconds = seconds
            self.setCursor(Qt.CrossCursor)
            QToolTip.hideText()
            self.update()
            event.accept()
            return
        index, mode = self._hit_interval(x, y)
        self._interaction_before = self._copy_rows(self.intervals)
        if index >= 0:
            self.selected_index = index
            self._drag_index = index
            self._drag_mode = mode
            self._drag_anchor = self._time_at_position(x, y, event.modifiers())
            self._drag_origin = dict(self.intervals[index])
        elif self._track_row_at(x, y) >= 0:
            start = self._time_at_position(x, y, event.modifiers())
            self.intervals.append({"name": f"机制{len(self.intervals) + 1}", "start": start, "end": min(self.window_seconds, start + 0.5)})
            self.intervals.sort(key=lambda row: row["start"])
            self.selected_index = next(
                index for index, row in enumerate(self.intervals)
                if abs(float(row["start"]) - start) < 1e-6
            )
            self._drag_index = self.selected_index
            self._drag_mode = "create"
            self._drag_anchor = start
            self._drag_origin = dict(self.intervals[self.selected_index])
        else:
            self.selected_index = -1
        self.selectionChanged.emit(self.selected_index)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        y = event.position().y()
        if self._selecting_range:
            seconds = self._time_at_position(x, y, Qt.NoModifier)
            self._selection_range = tuple(
                sorted((self._selection_anchor_seconds, seconds))
            )
            self._hover_seconds = seconds
            self._emit_interval_selection()
            QToolTip.showText(
                event.globalPosition().toPoint(), self._selection_summary(), self
            )
            self.update()
            event.accept()
            return
        if self._drag_mode and 0 <= self._drag_index < len(self.intervals):
            current = self._time_at_position(x, y, event.modifiers())
            origin = self._drag_origin or self.intervals[self._drag_index]
            start = float(origin["start"])
            end = float(origin["end"])
            if self._drag_mode == "move":
                delta = current - self._drag_anchor
                start, end = self._free_move_values(start, end, delta)
            elif self._drag_mode == "resize_left":
                start = current
            elif self._drag_mode in {"resize_right", "create"}:
                start = min(self._drag_anchor, current) if self._drag_mode == "create" else start
                end = max(self._drag_anchor, current) if self._drag_mode == "create" else current
            if self._drag_mode != "move":
                start, end = self._bounded_values(self._drag_index, start, end)
            self.intervals[self._drag_index]["start"] = start
            self.intervals[self._drag_index]["end"] = end
            self.update()
            event.accept()
            return
        if self._track_row_at(x, y) >= 0:
            self._hover_seconds = self._time_at_position(x, y, Qt.NoModifier)
            self.timeHovered.emit(self._hover_seconds)
            self.update()
        else:
            self._hover_seconds = None
        index, _mode = self._hit_interval(x, y)
        if index >= 0:
            row = self.intervals[index]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{row['name']}\n{row['start']:.1f}s - {row['end']:.1f}s\n持续 {row['end'] - row['start']:.1f}s",
                self,
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._selecting_range:
            self._selecting_range = False
            seconds = self._time_at_position(
                event.position().x(), event.position().y(), Qt.NoModifier
            )
            self._selection_range = tuple(
                sorted((self._selection_anchor_seconds, seconds))
            )
            self._hover_seconds = seconds
            self._emit_interval_selection()
            QToolTip.showText(
                event.globalPosition().toPoint(), self._selection_summary(), self
            )
            self.unsetCursor()
            self.update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag_mode:
            before = self._interaction_before or []
            moved_row = None
            if self._drag_mode == "move" and 0 <= self._drag_index < len(self.intervals):
                moved_row = self._settle_moved_interval(
                    self._drag_index,
                    float((self._drag_origin or {}).get("start", 0.0)),
                )
            self.intervals = normalize_mechanism_intervals(self.intervals, self.window_seconds)
            if moved_row is not None:
                moved_name = str(moved_row.get("name"))
                moved_start = float(moved_row.get("start", 0.0))
                moved_end = float(moved_row.get("end", 0.0))
                self.selected_index = min(
                    range(len(self.intervals)),
                    key=lambda index: (
                        0 if str(self.intervals[index].get("name")) == moved_name else 1,
                        abs(float(self.intervals[index].get("start", 0.0)) - moved_start)
                        + abs(float(self.intervals[index].get("end", 0.0)) - moved_end),
                    ),
                    default=-1,
                )
            if self.intervals != before:
                self._push_undo(before)
                self._emit_change()
                self.selectionChanged.emit(self.selected_index)
            self._drag_mode = ""
            self._drag_index = -1
            self._drag_origin = None
            self._interaction_before = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._selecting_range:
            self._hover_seconds = None
            QToolTip.hideText()
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key_Backspace, Qt.Key_Delete}:
            if 0 <= self.selected_index < len(self.intervals):
                self.remove_selected()
                event.accept()
                return
        if event.matches(QKeySequence.Copy):
            if 0 <= self.selected_index < len(self.intervals):
                payload = json.dumps(self.intervals[self.selected_index], ensure_ascii=False)
                QGuiApplication.clipboard().setText(f"DN_MECHANISM_INTERVAL:{payload}")
                event.accept()
                return
        if event.matches(QKeySequence.Paste):
            text = QGuiApplication.clipboard().text()
            if text.startswith("DN_MECHANISM_INTERVAL:"):
                try:
                    source = json.loads(text.split(":", 1)[1])
                except json.JSONDecodeError:
                    source = None
                if isinstance(source, dict):
                    duration = max(0.1, float(source.get("end", 0)) - float(source.get("start", 0)))
                    cursor = (
                        float(self.intervals[self.selected_index]["end"]) + 1.0
                        if 0 <= self.selected_index < len(self.intervals)
                        else 0.0
                    )
                    for row in self.intervals:
                        if cursor + duration <= float(row["start"]) - 1e-6:
                            break
                        if cursor < float(row["end"]):
                            cursor = float(row["end"]) + 1.0
                    if cursor + duration > self.window_seconds:
                        cursor = max(0.0, self.window_seconds - duration)
                    rows = self._copy_rows(self.intervals)
                    rows.append({
                        "name": f"{str(source.get('name') or '机制')} 副本",
                        "start": cursor,
                        "end": min(self.window_seconds, cursor + duration),
                    })
                    self.set_intervals(rows, record=True)
                    self.selected_index = next(
                        (index for index, row in enumerate(self.intervals) if row["name"].endswith("副本")),
                        len(self.intervals) - 1,
                    )
                    self.selectionChanged.emit(self.selected_index)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index, _mode = self._hit_interval(event.position().x(), event.position().y())
        if index < 0:
            super().mouseDoubleClickEvent(event)
            return
        current = str(self.intervals[index]["name"])
        name, accepted = QInputDialog.getText(self, "区间名称", "机制名称", text=current)
        if accepted and name.strip() and name.strip() != current:
            before = self._copy_rows(self.intervals)
            self.intervals[index]["name"] = name.strip()
            self._push_undo(before)
            self._emit_change()
        event.accept()
