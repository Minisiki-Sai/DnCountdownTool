"""Read-only matching of factual HP losses and heals for healer duties.

This does not re-difference HP, attribute healing skills or change event amounts.
Every recovered HP unit can answer at most one earlier loss on the same target.
"""
from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass
from .healing_attribution import NON_HEALING_SKILL_KEYS, healing_skill_key
from .healing_exclusions import contained_in


RESCUE_FULL_SECONDS = 3.0
RESCUE_END_SECONDS = 8.0


def rescue_weight(delay: float) -> float:
    return max(0.0, min(1.0, (RESCUE_END_SECONDS - delay)
                        / (RESCUE_END_SECONDS - RESCUE_FULL_SECONDS)))


def _merge(rows):
    merged = []
    for start, end in sorted(rows):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass
class HealingDemand:
    losses: tuple
    recovered: dict
    need: dict
    recovered_raw: dict | None = None
    unmet_raw: float = 0.0
    unmet_by_loss: dict | None = None

    def needed_during(self, start, end, targets):
        for target in targets:
            rows, ends = self.need.get(target, ((), ()))
            index = bisect_right(ends, start)
            if index < len(rows) and rows[index][0] <= end:
                return True
        return False


def audit_healing_demand(losses, heals, windows, *, health_tracks=(), lifecycle=(),
                         excluded_intervals=None):
    """FIFO HP accounting, separated by recipient and selected phase.

    All factual heals consume demand, including potions/other healers. Only
    attributed amounts will later give the requested healer rescue credit.
    A confirmed death closes demand, so revival cannot rescue an earlier life.
    """
    windows = _merge(windows)
    if not windows:
        return HealingDemand((), {}, {})
    starts = tuple(start for start, _end in windows)
    excluded_intervals = excluded_intervals or {}

    def in_scope(seconds):
        i = bisect_right(starts, seconds) - 1
        return i >= 0 and seconds <= windows[i][1]

    deaths = defaultdict(set)
    resets = defaultdict(set)
    health_need = defaultdict(list)
    for row in lifecycle:
        if not row.active:
            deaths[row.target_id].add(row.timestamp_ms / 1000.0)
    for track in health_tracks:
        points = sorted(track.points, key=lambda p: p.timestamp_seconds)
        for i, point in enumerate(points):
            t = point.timestamp_seconds
            if point.current_hp <= 0:
                deaths[track.target_id].add(t)
            elif track.exact_percent and point.percent is not None:
                if point.percent >= 100.0:
                    resets[track.target_id].add(t)
                elif point.percent > 0:
                    end = points[i + 1].timestamp_seconds if i + 1 < len(points) else windows[-1][1]
                    # A confirmed unhealable interval cannot create a fresh
                    # treatment obligation, even without an incoming record.
                    if not contained_in(track.target_id, t, excluded_intervals,
                                        include_start=True):
                        health_need[track.target_id].append((t, end))

    # A lethal HP edge at the death timestamp cannot itself be rescued.
    losses = tuple(e for e in losses if in_scope(e.timestamp_ms / 1000.0)
                   and e.timestamp_ms / 1000.0 not in deaths[e.target_id]
                   and not contained_in(e.target_id, e.timestamp_ms / 1000.0,
                                        excluded_intervals))
    events = defaultdict(list)
    for i, e in enumerate(losses):
        events[e.target_id].append((e.timestamp_ms / 1000.0, 0, i, e))
    for i, e in enumerate(heals):
        if in_scope(e.timestamp_ms / 1000.0):
            events[e.target_id].append((e.timestamp_ms / 1000.0, 1, i, e))
    for target in set(deaths) | set(resets):
        for i, seconds in enumerate(deaths[target]):
            events[target].append((seconds, 2, i, None))
        for i, seconds in enumerate(resets[target]):
            events[target].append((seconds, 3, i, None))
    for target, intervals in excluded_intervals.items():
        for i, (start, _end) in enumerate(intervals):
            events[target].append((start, 4, i, None))

    recovered = defaultdict(lambda: defaultdict(float))
    recovered_raw = defaultdict(lambda: defaultdict(float))
    need = defaultdict(list)
    unmet_raw = 0.0
    unmet_by_loss = defaultdict(float)
    for target, rows in events.items():
        # End markers also close unresolved need during downtime. No rescue
        # credit may migrate from the previous Boss/phase to the next one.
        rows.extend((end, 5, i, None) for i, (_start, end) in enumerate(windows))
        rows.sort(key=lambda row: row[:3])
        pending = deque()
        opened = None
        for seconds, kind, index, event in rows:
            if kind >= 2:
                # A full-HP sync settles the deficit without attributing a
                # heal.  Ban-heal intervals discard the duty.  A death after
                # an otherwise healable window, or an unfinished phase,
                # leaves factual HP demand unmet.
                if kind in (2, 5):
                    unmet_raw += sum(outstanding for _index, outstanding in pending)
                    for loss_index, outstanding in pending:
                        unmet_by_loss[loss_index] += outstanding
                pending.clear()
            elif kind == 0:
                if not pending:
                    opened = seconds
                pending.append([index, float(event.amount)])
            else:
                remaining = max(0.0, float(event.amount))
                while pending and remaining > 0:
                    loss_index, outstanding = pending[0]
                    amount = min(remaining, outstanding)
                    age = seconds - losses[loss_index].timestamp_ms / 1000.0
                    source = (event.source_id if healing_skill_key(event.skill_name)
                              not in NON_HEALING_SKILL_KEYS else "")
                    recovered[loss_index][source] += amount * rescue_weight(age)
                    recovered_raw[loss_index][source] += amount
                    remaining -= amount
                    pending[0][1] -= amount
                    if pending[0][1] <= 0:
                        pending.popleft()
            if not pending and opened is not None:
                need[target].append((opened, seconds))
                opened = None
    for target, rows in health_need.items():
        # A later lifecycle death may not have a corresponding HP-track point.
        # Do not carry the last low-HP sample through that death/revival edge.
        death_times = sorted(deaths[target])
        for start, end in rows:
            index = bisect_right(death_times, start)
            if index < len(death_times):
                end = min(end, death_times[index])
            pieces = [(start, end)]
            for blocked_start, blocked_end in excluded_intervals.get(target, ()):
                pieces = [(left, min(right, blocked_start))
                          for left, right in pieces if left < blocked_start] + [
                          (max(left, blocked_end), right)
                          for left, right in pieces if right > blocked_end]
            need[target].extend((left, right) for left, right in pieces if right > left)
    projected = {}
    for target, rows in need.items():
        clipped = _merge((max(a, start), min(b, end)) for a, b in rows
                         for start, end in windows if a < end and b > start)
        projected[target] = (clipped, tuple(end for _start, end in clipped))
    return HealingDemand(losses, dict(recovered), projected, dict(recovered_raw), unmet_raw,
                         dict(unmet_by_loss))
