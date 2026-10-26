from __future__ import annotations

from bisect import bisect_left
from typing import Iterable


DEFAULT_FREEZE_BREAK_HITS = 100
ALCHEMIST_FREEZE_BREAK_HITS = 200


def advance_freeze_hit_count(
    hit_timestamp: float,
    start_timestamp: float,
    end_timestamp: float,
    hits_since_refresh: int,
    hit_limit: int,
) -> tuple[int, bool]:
    """Apply one team hit to one independently tracked freeze layer."""

    if not start_timestamp <= hit_timestamp < end_timestamp:
        return hits_since_refresh, False
    updated = hits_since_refresh + 1
    return updated, updated >= max(1, hit_limit)


def hit_limited_freeze_intervals(
    applications: Iterable[tuple[float, float]],
    hit_timestamps: Iterable[float],
    hit_limit: int = DEFAULT_FREEZE_BREAK_HITS,
) -> tuple[tuple[float, float], ...]:
    """Project one freeze skill's coverage through a factual team-Hit stream.

    Applications belong to one skill/effect identity. Reapplying that identity
    while it is active refreshes its natural end and resets its Hit durability;
    other freeze skills call this engine independently and therefore retain
    their own counters. Every active layer still consumes the same team hits.
    """

    rows = sorted(
        (float(start), float(end))
        for start, end in applications
        if float(end) > float(start)
    )
    if not rows:
        return ()
    hits = sorted(float(value) for value in hit_timestamps)
    limit = max(1, int(hit_limit))
    covered: list[tuple[float, float]] = []
    active_start: float | None = None
    active_end = 0.0
    active_hits = 0
    hit_index = 0

    def close_active(end: float) -> None:
        nonlocal active_start, active_end, active_hits
        if active_start is not None and end > active_start:
            covered.append((active_start, end))
        active_start = None
        active_end = 0.0
        active_hits = 0

    def advance_until(stop: float) -> None:
        nonlocal active_hits, hit_index
        if active_start is None:
            return
        while hit_index < len(hits):
            timestamp = hits[hit_index]
            if timestamp >= stop or timestamp >= active_end:
                break
            active_hits, shattered = advance_freeze_hit_count(
                timestamp,
                active_start,
                active_end,
                active_hits,
                limit,
            )
            hit_index += 1
            if shattered:
                close_active(timestamp)
                return
        if active_start is not None and active_end <= stop:
            close_active(active_end)

    for start, natural_end in rows:
        advance_until(start)
        if active_start is None:
            active_start = start
            active_end = natural_end
            active_hits = 0
            hit_index = bisect_left(hits, start, lo=hit_index)
            continue
        # The same skill refreshed before its previous layer expired.
        active_end = max(active_end, natural_end)
        active_hits = 0

    advance_until(float("inf"))
    if active_start is not None:
        close_active(active_end)

    # Refreshes can produce touching pieces; expose canonical coverage rows.
    merged: list[list[float]] = []
    for start, end in covered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def refreshing_freeze_applications(
    coverage_intervals: Iterable[tuple[float, float]],
    application_duration: float,
    refresh_interval: float,
) -> tuple[tuple[float, float], ...]:
    """Expand one persistent attack's union window into status refreshes.

    ``coverage_intervals`` already describes the natural union from the first
    application through the final application's expiry.  Reconstructing the
    intermediate applications lets the Hit-limit engine reset durability on
    every refresh instead of treating the whole persistent attack as one layer.
    """

    duration = max(0.0, float(application_duration))
    interval = max(0.0, float(refresh_interval))
    if duration <= 0 or interval <= 0:
        return tuple(coverage_intervals)

    result: list[tuple[float, float]] = []
    for raw_start, raw_end in coverage_intervals:
        start = float(raw_start)
        end = float(raw_end)
        if end <= start:
            continue
        final_refresh = max(start, end - duration)
        cursor = start
        while cursor < final_refresh - 1e-9:
            result.append((cursor, min(end, cursor + duration)))
            cursor += interval
        result.append((final_refresh, end))
    return tuple(result)
