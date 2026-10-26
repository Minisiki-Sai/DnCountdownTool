"""Conservative capture-evidence recognition of Barbarian AppendHit (501).

Both subtype4 and subtype5 B1 require damage-pattern evidence. A subtype is
not a skill identity; self HP costs remain excluded.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Mapping, Sequence

from .models import CombatEvent


def infer_combo_damage(events: Sequence[CombatEvent], jobs: Mapping[str, str]):
    direct = defaultdict(list)
    for index, event in enumerate(events):
        if (event.has_real_hit_evidence is True and not event.healing
                and event.amount > 0 and not event.wire_status_tail_hex):
            direct[(event.source_id, event.target_id)].append((event.timestamp_ms, index))
    for rows in direct.values():
        rows.sort()
    times = {key: [t for t, _ in rows] for key, rows in direct.items()}
    cohorts = defaultdict(list)
    result = {}
    for index, event in enumerate(events):
        if (jobs.get(event.source_id, event.job) != "狂战士"
                or event.healing or event.amount <= 0
                or not event.target_id or event.target_id == event.source_id
                or (event.wire_source_actor_id and event.wire_source_actor_id == event.wire_target_actor_id)
                or event.has_real_hit_evidence is not False
                or event.wire_status_tail_hex.lower() != "0001000000000001b100ffffffff"
                or event.wire_status_skill_id != 0xFFFFFFFF
                or not any(f"status-damage-subtype{subtype}-" in event.wire_damage_layout
                           for subtype in (4, 5))):
            continue
        key = (event.source_id, event.target_id)
        rows, stamps = direct.get(key, ()), times.get(key, ())
        nearby = rows[bisect_left(stamps, event.timestamp_ms - 150):
                      bisect_right(stamps, event.timestamp_ms + 5)]
        if not nearby:
            continue
        # Packed direct damage may be quantized; a strict decimal comparison
        # otherwise leaves percentage procs masquerading as flat attack procs.
        if any(abs(events[j].amount * .1 - event.amount) <= max(32, event.amount * .001)
               for _, j in nearby):
            continue
        _, trigger = min(nearby, key=lambda row: (abs(row[0] - event.timestamp_ms), row[1]))
        cohorts[(event.source_id, event.amount)].append((index, trigger))
    for rows in cohorts.values():
        stamps = {events[i].timestamp_ms for i, _ in rows}
        damage = [events[j].amount for _, j in rows]
        # Repeated values across independently timed attacks with materially
        # different direct damage establish the max-attack plateau. A lone
        # record remains unknown rather than being granted the nearest plateau.
        if (len(stamps) < 2 or max(stamps) - min(stamps) < 400
                or max(damage) < min(damage) * 1.2):
            continue
        for index, trigger in rows:
            result[index] = (trigger, len(stamps))
    return result


def separate_combo_damage(events: Sequence[CombatEvent]) -> tuple[CombatEvent, ...]:
    """Give comparator ledgers the same passive identity as ACT composition.

    Keep every wire record/amount, but discard a nearby active cast's inferred
    ownership. The passive is auxiliary damage, never an extra native Hit.
    """
    from dataclasses import replace
    rows = tuple(events)
    inferred = infer_combo_damage(rows, {})
    result = []
    for index, event in enumerate(rows):
        if index not in inferred or event.skill_id == "501":
            result.append(event)
            continue
        trigger_index, sample_count = inferred[index]
        trigger = rows[trigger_index] if trigger_index >= 0 else None
        result.append(replace(
            event, skill_id="501", skill_name="连击", cast_instance_id="",
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=event.raw_attribution_method or event.attribution_method,
            attribution_method=("barbarian_combo_subtype5_signature" if sample_count == 0 else "barbarian_combo_max_attack_evidence"),
            attribution_confidence=.90, has_real_hit_evidence=False,
            trigger_skill_id=trigger.skill_id if trigger else "",
            trigger_skill_name=trigger.skill_name if trigger else "",
        ))
    return tuple(result)
