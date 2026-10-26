from __future__ import annotations

from .event_platform import event_platform_for

from functools import lru_cache


_LIMITED_RECORDED_TEAM_BUFF_KEYS = frozenset({"九天之舞2阶"})


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


def _support_completion_is_scored(completion_ratio: float) -> bool:
    return (
        max(0.0, as_number(completion_ratio)) + 1e-9
        >= _SUPPORT_SCORE_MIN_COMPLETION
    )

def _in_intervals(seconds: float, intervals: Iterable[tuple[float, float]]) -> bool:
    # All interval builders in this module return start-sorted, merged rows.
    # Counterfactual scoring calls this millions of times, so use a binary
    # lookup for materialized rows while retaining the generic iterable path.
    if isinstance(intervals, (tuple, list)):
        if not intervals:
            return False
        tolerance = 1e-6
        index = bisect_right(intervals, (seconds + tolerance, math.inf)) - 1
        if index < 0:
            return False
        start, end = intervals[index]
        return start - tolerance <= seconds <= end + tolerance
    return any(start - 1e-6 <= seconds <= end + 1e-6 for start, end in intervals)

def _timing_efficiency(
    actual_casts: Iterable[_ValuedCast],
    theory_casts: Iterable[_TheoreticalCast],
    high_value_keys: set[str],
    burst_windows: Iterable[tuple[float, float]],
) -> float:
    """Measure placement quality without charging missing casts a second time."""
    windows = tuple(burst_windows)
    if not windows or not high_value_keys:
        return 100.0

    actual_rows = tuple(row for row in actual_casts if row.key in high_value_keys)
    theory_rows = tuple(row for row in theory_casts if row.key in high_value_keys)
    theory_total = sum(row.power for row in theory_rows)
    theory_in_window = sum(
        row.power for row in theory_rows if _in_intervals(row.seconds, windows)
    )
    if theory_total <= 1e-9 or theory_in_window <= 1e-9:
        return 100.0

    actual_total = sum(row.power for row in actual_rows)
    if actual_total <= 1e-9:
        return 0.0
    actual_in_window = sum(
        row.power for row in actual_rows if _in_intervals(row.seconds, windows)
    )
    actual_share = actual_in_window / actual_total
    theory_share = theory_in_window / theory_total
    return _clamp_score(actual_share / theory_share * 100.0)

def _merge_intervals(
    intervals: Iterable[tuple[float, float]], *, join_gap: float = 0.0
) -> tuple[tuple[float, float], ...]:
    rows = sorted((max(0.0, start), max(start, end)) for start, end in intervals)
    merged: list[list[float]] = []
    for start, end in rows:
        if not merged or start > merged[-1][1] + join_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)

def _clip_intervals(
    intervals: Iterable[tuple[float, float]], end_seconds: float
) -> tuple[tuple[float, float], ...]:
    return _merge_intervals(
        (max(0.0, start), min(end_seconds, end))
        for start, end in intervals
        if end > 0 and start < end_seconds and min(end_seconds, end) > max(0.0, start)
    )

def _bounded_recorded_team_buff_intervals(
    snapshot: CombatSnapshot,
    source_id: str,
    skill_key: str,
    duration: float,
    intervals: Iterable[tuple[float, float]],
    end_seconds: float,
    *,
    target_id: str = "",
) -> tuple[tuple[float, float], ...]:
    """Cap protocol-open-ended buffs using each captured application time."""
    captured = _clip_intervals(intervals, end_seconds)
    if skill_key not in _LIMITED_RECORDED_TEAM_BUFF_KEYS or duration <= 0:
        return captured
    application_starts = sorted(
        {
            max(0.0, as_number(event.timestamp_seconds))
            for event in event_platform_for(snapshot).player_buff_events
            if str(getattr(event, "source_id", "")) == source_id
            and canonical_skill(getattr(event, "skill_name", "")) == skill_key
            and (
                not target_id
                or str(getattr(event, "target_id", "")) == target_id
            )
            and norm(getattr(event, "operation", "apply")) != "remove"
        }
    )
    if application_starts:
        return _clip_intervals(
            (
                (start, min(end_seconds, start + duration))
                for start in application_starts
                if start < end_seconds
            ),
            end_seconds,
        )
    return _clip_intervals(
        (
            (start, min(end, start + duration))
            for start, end in captured
            if end > start
        ),
        end_seconds,
    )

def _intersect_intervals(
    intervals: Iterable[tuple[float, float]],
    allowed: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    source = _merge_intervals(intervals)
    windows = _merge_intervals(allowed)
    result: list[tuple[float, float]] = []
    source_index = 0
    window_index = 0
    while source_index < len(source) and window_index < len(windows):
        start = max(source[source_index][0], windows[window_index][0])
        end = min(source[source_index][1], windows[window_index][1])
        if end > start + 1e-9:
            result.append((start, end))
        if source[source_index][1] <= windows[window_index][1] + 1e-9:
            source_index += 1
        else:
            window_index += 1
    return _merge_intervals(result)

def _subtract_intervals(
    intervals: Iterable[tuple[float, float]],
    excluded: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    """Remove already-claimed coverage from a set of effect intervals."""
    source = _merge_intervals(intervals)
    blocked = _merge_intervals(excluded)
    if not source or not blocked:
        return source
    result: list[tuple[float, float]] = []
    for start, end in source:
        cursor = start
        for block_start, block_end in blocked:
            if block_end <= cursor + 1e-9:
                continue
            if block_start >= end - 1e-9:
                break
            if block_start > cursor + 1e-9:
                result.append((cursor, min(end, block_start)))
            cursor = max(cursor, block_end)
            if cursor >= end - 1e-9:
                break
        if cursor < end - 1e-9:
            result.append((cursor, end))
    return _merge_intervals(result)

def _allocate_capped_interval_layers(
    intervals: Iterable[tuple[float, float]],
    claimed_layers: list[tuple[tuple[float, float], ...]],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Allocate an effect to the first free layer at every point in time."""
    pending = _merge_intervals(intervals)
    allocated: list[tuple[tuple[float, float], ...]] = []
    for index, occupied in enumerate(claimed_layers):
        if not pending:
            break
        free = _subtract_intervals(pending, occupied)
        overlap = _intersect_intervals(pending, occupied)
        if free:
            claimed_layers[index] = _merge_intervals((*occupied, *free))
            allocated.append(free)
        pending = overlap
    return tuple(allocated)

def _interval_duration(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in _merge_intervals(intervals))

def _coverage_in_windows(
    intervals: Iterable[tuple[float, float]],
    output_windows: Iterable[tuple[float, float]],
) -> float:
    active = _interval_duration(output_windows)
    if active <= 1e-9:
        return 0.0
    covered = _interval_duration(_intersect_intervals(intervals, output_windows))
    return max(0.0, min(1.0, covered / active))

@lru_cache(maxsize=1)
def _builtin_skill_builds() -> dict[str, Any]:
    if not _DEFAULT_BUILD_PATH.exists():
        return {}
    try:
        value = json.loads(_DEFAULT_BUILD_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
