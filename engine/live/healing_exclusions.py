"""Recipient-specific intervals where a healing response is not assessable."""

from collections import defaultdict
from math import isfinite


# Current client skilltable_monster70 (e.g. 34832) and skilltable_monster
# (e.g. 32138, including its seventh effect slot) use class 275 for the
# no-healing status. Their skill descriptions say "无法受到治疗" / "伤口无法愈合".
# Both skills emit recipient-targeted 13 43 status applications in classic
# Black Dragon and Desert Dragon captures. Match the effect class rather
# than a Boss skill ID so the same rule covers all such skills.
HEAL_BLOCK_EFFECT_CLASS = 275
FATAL_BURST_SECONDS = 1.0
FATAL_BURST_MIN_HP_FRACTION = 0.50


def _merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if not isfinite(start) or end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return tuple(result)


def healing_block_intervals(skill_parameters, party_ids):
    """Use recipient-specific, server-reported status durations, not Boss HP."""
    party = frozenset(party_ids)
    result = defaultdict(list)
    for event in skill_parameters:
        if (event.target_id not in party
                or event.effect_class_id != HEAL_BLOCK_EFFECT_CLASS
                or event.duration_ms <= 0):
            continue
        start = event.timestamp_ms / 1000.0
        result[event.target_id].append((start, start + event.duration_ms / 1000.0))
    return {target: _merge(rows) for target, rows in result.items()}


def fatal_burst_intervals(health_tracks, lifecycle=(), incoming=()):
    """Find exact >50%-max-HP falls to confirmed death within one second.

    Estimated curves are excluded: their synthetic zeros cannot prove an
    unhealable burst. The output is recipient-specific and is not a death-wide
    pardon for injuries that were present before the one-second collapse.
    """
    deaths = defaultdict(set)
    losses = defaultdict(list)
    for event in lifecycle:
        if not event.active:
            deaths[event.target_id].add(event.timestamp_ms / 1000.0)
    for event in incoming:
        if (event.attribution_method == 'incoming_hp_state'
                and event.amount > 0 and event.source_id != event.target_id):
            losses[event.target_id].append((event.timestamp_ms / 1000.0,
                                            event.amount))
    result = defaultdict(list)
    for track in health_tracks:
        if (not track.exact_percent or track.state_source not in ("", "absolute_086f")
                or not isfinite(track.max_hp) or track.max_hp <= 0):
            continue
        points = sorted((point for point in track.points
                         if isfinite(point.timestamp_seconds)
                         and isfinite(point.current_hp)),
                        key=lambda point: point.timestamp_seconds)
        death_times = deaths[track.target_id] | {
            point.timestamp_seconds for point in points if point.current_hp <= 0
        }
        for end in sorted(death_times):
            candidates = [point.timestamp_seconds for point in points
                          if end - FATAL_BURST_SECONDS <= point.timestamp_seconds < end
                          and point.current_hp >= track.max_hp * FATAL_BURST_MIN_HP_FRACTION]
            if candidates:
                result[track.target_id].append((min(candidates), end))
                continue
            # A confirmed death can precede the next absolute HP state. In
            # that case, factual HP-loss records still prove the collapse.
            recent = [(seconds, amount) for seconds, amount in losses[track.target_id]
                      if end - FATAL_BURST_SECONDS <= seconds <= end]
            if recent:
                first_loss = min(seconds for seconds, _amount in recent)
                if (end - first_loss >= .05
                        and sum(amount for _seconds, amount in recent) >=
                        track.max_hp * FATAL_BURST_MIN_HP_FRACTION):
                    result[track.target_id].append((first_loss - .001, end))
    return {target: _merge(rows) for target, rows in result.items()}


def contained_in(target, seconds, intervals, *, include_start=False):
    return any((start <= seconds if include_start else start < seconds) and seconds <= end
               for start, end in intervals.get(target, ()))


def overlaps(target, start, end, intervals):
    return any(start < right and left < end for left, right in intervals.get(target, ()))
