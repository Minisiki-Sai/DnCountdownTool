"""Conservative, offline attribution of verified player-skill HP costs.

An anonymous HP loss is not automatically self damage. The reviewed v576
Berserker class-mastery row 507 applies [16/-0.05] on skill 412: consume 5%
maximum HP when casting Counter Roar. Match that cost only to an observed
loss, never manufacture damage from a cast or a missing packet.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from typing import Iterable, Mapping

from .models import CombatEvent, SkillCastEvent


_MATCH_WINDOW_MS = 350
_UNKNOWN_HP_SOURCES = frozenset({"", "__incoming_hp_delta__", "__incoming_hp_state__"})


def annotate_skill_hp_costs(
    losses: Iterable[CombatEvent],
    casts: Iterable[SkillCastEvent],
    maximum_hp: Mapping[str, int],
    actor_jobs: Mapping[str, str],
    direct_damage: Iterable[CombatEvent] = (),
    max_hp_buff_windows: Mapping[str, tuple[tuple[int, int], ...]] | None = None,
) -> list[CombatEvent]:
    """Attach a self source only after timing, job and amount all agree."""
    costs: dict[str, dict[int, SkillCastEvent]] = defaultdict(dict)
    for cast in casts:
        job = actor_jobs.get(cast.source_id) or cast.job
        if (
            job not in {"狂战士", "狂战"}
            or str(cast.skill_id) != "412"
            or maximum_hp.get(cast.source_id, 0) <= 0
        ):
            continue
        costs[cast.source_id].setdefault(cast.timestamp_ms, cast)
    times = {source_id: sorted(rows) for source_id, rows in costs.items()}
    # A direct incoming damage packet defeats the anonymous-cost hypothesis.
    # Boss/environment packets can lack a named actor too; they still count.
    direct_times: dict[tuple[str, int], list[int]] = defaultdict(list)
    for event in direct_damage:
        if event.amount > 0 and event.source_id != event.target_id:
            direct_times[(event.target_id, event.amount)].append(event.timestamp_ms)
    for rows in direct_times.values():
        rows.sort()
    ordered_losses = sorted(losses, key=lambda event: event.timestamp_ms)
    # The canonical HP baseline intentionally excludes Ghost Soul. Its
    # effective maximum cannot be recovered by multiplying that baseline:
    # gear HP% and the temporary HP% can share an additive lane. Admit an
    # alternate charge only with a proven HP-boost window, at least two
    # baseline charges and three distinct matching casts at the same amount.
    baseline_casts: dict[str, set[int]] = defaultdict(set)
    boosted_casts: dict[tuple[str, int], set[int]] = defaultdict(set)
    for loss in ordered_losses:
        source_id = loss.target_id
        timestamps = times.get(source_id, [])
        end = bisect_right(timestamps, loss.timestamp_ms)
        if not end or loss.source_id not in _UNKNOWN_HP_SOURCES:
            continue
        cast_at = timestamps[end - 1]
        if loss.timestamp_ms - cast_at > _MATCH_WINDOW_MS:
            continue
        blockers = direct_times.get((source_id, loss.amount), [])
        if bisect_left(blockers, loss.timestamp_ms - _MATCH_WINDOW_MS) < bisect_right(
            blockers, loss.timestamp_ms + _MATCH_WINDOW_MS
        ):
            continue
        expected = maximum_hp.get(source_id, 0) * 0.05
        if abs(loss.amount - expected) <= max(2.0, expected * 0.001):
            baseline_casts[source_id].add(cast_at)
        elif (
            expected < loss.amount <= expected * 2
            and any(start <= loss.timestamp_ms <= finish for start, finish in
                    (max_hp_buff_windows or {}).get(source_id, ()))
        ):
            boosted_casts[(source_id, loss.amount)].add(cast_at)
    verified_boosted_amounts = {
        key for key, cast_times in boosted_casts.items()
        if len(cast_times) >= 3 and len(baseline_casts[key[0]]) >= 2
    }
    used: set[tuple[str, int]] = set()
    result: list[CombatEvent] = []
    for loss in ordered_losses:
        source_id = loss.target_id
        candidate_times = times.get(source_id, [])
        expected = maximum_hp.get(source_id, 0) * 0.05
        tolerance = max(2.0, expected * 0.001)
        blockers = direct_times.get((source_id, loss.amount), [])
        blocked = bisect_left(blockers, loss.timestamp_ms - _MATCH_WINDOW_MS) < bisect_right(
            blockers, loss.timestamp_ms + _MATCH_WINDOW_MS
        )
        verified_boost = (
            (source_id, loss.amount) in verified_boosted_amounts
            and any(start <= loss.timestamp_ms <= finish for start, finish in
                    (max_hp_buff_windows or {}).get(source_id, ()))
        )
        if (
            loss.source_id not in _UNKNOWN_HP_SOURCES
            or loss.amount <= 0
            or (abs(loss.amount - expected) > tolerance and not verified_boost)
            or blocked
        ):
            result.append(loss)
            continue
        start = bisect_left(candidate_times, loss.timestamp_ms - _MATCH_WINDOW_MS)
        end = bisect_right(candidate_times, loss.timestamp_ms)
        matched_at = next(
            (timestamp for timestamp in reversed(candidate_times[start:end])
             if (source_id, timestamp) not in used),
            None,
        )
        if matched_at is None:
            result.append(loss)
            continue
        used.add((source_id, matched_at))
        cast = costs[source_id][matched_at]
        result.append(replace(
            loss,
            source_id=source_id,
            source_name=cast.source_name or loss.target_name,
            job=actor_jobs.get(source_id) or cast.job,
            skill_id=cast.skill_id,
            skill_name=f"{cast.skill_name or '反击怒吼'}（自身生命消耗）",
            raw_skill_id=loss.raw_skill_id or loss.skill_id,
            raw_skill_name=loss.raw_skill_name or loss.skill_name,
        ))
    return result
