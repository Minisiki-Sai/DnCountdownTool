from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from typing import Callable

from .models import CombatEvent


# These status-damage skills are not ordinary direct attacks. The server
# emits their bonus result under the effect skill while a nearby factual Hit
# identifies the attack that triggered the proc. ``max_steps`` is the client
# rule's maximum 0.9%-of-trigger-hit units.
_PROC_MAX_STEPS: dict[str, int] = {
    "5118": 50,  # 九天之舞2阶
    "6219": 70,  # 奉献乌鸦
}
_PROC_COMPOSITION_NAMES: dict[str, str] = {
    "5118": "九天之舞2阶（额外伤害）",
    "6219": "奉献乌鸦（额外伤害）",
}
_PROC_DAMAGE_STEP = 0.009
_RATIO_TOLERANCE = 10.0
_BEFORE_MS = 120
_AFTER_MS = 5

_EXISTING_TRIGGER_METHODS: dict[str, tuple[str, float]] = {
    # The extra row and its direct Hit share one decoded server message.
    "supplemental_same_packet": ("same_packet_damage_trigger", 1.0),
    # A reviewed continuation/status generation owns the delayed result.
    "supplemental_continuation": ("continuation_damage_owner", 0.98),
    # This is the legacy bounded recent-cast fallback. Expose it for audit,
    # but its lower confidence makes clear that it is not packet identity.
    "supplemental_cast_owner": ("bounded_recent_cast_candidate", 0.55),
}

_NON_TRIGGER_METHODS = frozenset(
    {
        "status_damage_packet",
        "status_damage_skill",
        "periodic_status_packet",
        "packet_entity_status_damage",
        "supplemental_cast_owner",
        "supplemental_continuation",
        "supplemental_same_packet",
        "direct_hit_overflow",
        "refreshable_pulsing_aura",
    }
)


def _native_trigger_identity(event: CombatEvent) -> tuple[str, str]:
    method = str(event.attribution_method or "").strip().casefold()
    if method == "direct_hit_overflow":
        skill_id = str(event.raw_skill_id or event.skill_id or "").strip()
        skill_name = str(event.raw_skill_name or event.skill_name or "").strip()
    else:
        skill_id = str(event.skill_id or "").strip()
        skill_name = str(event.skill_name or "").strip()
    if (
        skill_id.casefold() in {"", "-1", "unknown", "unresolved"}
        or skill_id.casefold().startswith(("supplemental:", "periodic:"))
        or skill_name in {"", "待识别伤害", "未学习伤害", "额外伤害"}
    ):
        return "", ""
    return skill_id, skill_name


def additional_damage_composition_name(
    skill_id: object,
    fallback: object = "",
) -> str:
    """Return the explicit skill-composition label for a proven team proc."""

    normalized = str(skill_id or "").strip()
    return _PROC_COMPOSITION_NAMES.get(
        normalized,
        str(fallback or normalized).strip(),
    )


def is_linkable_additional_damage(event: CombatEvent) -> bool:
    return bool(
        not event.healing
        and event.amount > 0
        and str(event.skill_id or "").strip() in _PROC_MAX_STEPS
        and str(event.attribution_method or "").strip().casefold()
        == "status_damage_skill"
    )


def _is_factual_trigger_hit(event: CombatEvent) -> bool:
    method = str(event.attribution_method or "").strip().casefold()
    native_skill_id, native_skill_name = _native_trigger_identity(event)
    skill_id = native_skill_id.casefold()
    skill_name = native_skill_name
    return bool(
        not event.healing
        and event.amount > 0
        and event.has_real_hit_evidence is True
        and (
            method not in _NON_TRIGGER_METHODS
            or method == "direct_hit_overflow"
        )
        and not skill_id.startswith(("supplemental:", "periodic:"))
        and skill_id not in {"", "-1", "unknown", "unresolved"}
        and skill_name not in {"", "待识别伤害", "未学习伤害", "额外伤害"}
    )


def _ratio_matches(bonus: int, hit: int, max_steps: int) -> bool:
    step = hit * _PROC_DAMAGE_STEP
    if step <= 0:
        return False
    multiple = max(1, min(max_steps, round(bonus / step)))
    return abs(bonus - step * multiple) <= max(
        _RATIO_TOLERANCE,
        bonus * 0.001,
    )


def _preserve_existing_trigger(event: CombatEvent) -> CombatEvent:
    if event.trigger_skill_id or event.trigger_skill_name:
        return event
    method = str(event.attribution_method or "").strip().casefold()
    trigger_rule = _EXISTING_TRIGGER_METHODS.get(method)
    skill_id = str(event.skill_id or "").strip()
    skill_name = str(event.skill_name or "").strip()
    if (
        trigger_rule is None
        or skill_id.casefold() in {"", "-1", "unknown", "unresolved"}
        or skill_id.startswith(("supplemental:", "periodic:"))
        or skill_name in {"", "待识别伤害", "未学习伤害", "额外伤害"}
    ):
        return event
    trigger_method, confidence = trigger_rule
    return replace(
        event,
        trigger_skill_id=skill_id,
        trigger_skill_name=skill_name,
        trigger_attribution_method=trigger_method,
        trigger_attribution_confidence=confidence,
    )


def link_additional_damage_triggers(
    events: tuple[CombatEvent, ...],
    bucket: Callable[[CombatEvent], tuple[str, str]],
) -> tuple[CombatEvent, ...]:
    """Attach proc damage to one independently proven triggering skill.

    Time proximity narrows candidates but never selects between competing
    skills on its own. A unique skill in the same actor/target window is
    accepted directly; competing skills require the client-derived damage
    ratio to leave exactly one identity. Otherwise the proc stays unlinked.
    """

    events = tuple(_preserve_existing_trigger(event) for event in events)
    trigger_rows: dict[tuple[str, str], list[tuple[int, int, str, str]]] = (
        defaultdict(list)
    )
    linked_sibling_rows: dict[
        tuple[str, str], list[tuple[int, str, str, float]]
    ] = defaultdict(list)
    bonus_indexes: list[int] = []
    for index, event in enumerate(events):
        if is_linkable_additional_damage(event):
            bonus_indexes.append(index)
            continue
        if event.trigger_skill_id or event.trigger_skill_name:
            linked_sibling_rows[(event.source_id, event.target_id)].append(
                (
                    event.timestamp_ms,
                    event.trigger_skill_id,
                    event.trigger_skill_name,
                    float(event.trigger_attribution_confidence or 0.0),
                )
            )
        if not _is_factual_trigger_hit(event):
            continue
        native_skill_id, native_skill_name = _native_trigger_identity(event)
        skill_id, skill_name = (
            (native_skill_id, native_skill_name)
            if native_skill_id and native_skill_name
            else bucket(event)
        )
        trigger_rows[(event.source_id, event.target_id)].append(
            (event.timestamp_ms, event.amount, skill_id, skill_name)
        )

    if not bonus_indexes:
        return events
    timestamps: dict[tuple[str, str], list[int]] = {}
    for key, rows in trigger_rows.items():
        rows.sort(key=lambda row: row[0])
        timestamps[key] = [row[0] for row in rows]
    sibling_timestamps: dict[tuple[str, str], list[int]] = {}
    for key, rows in linked_sibling_rows.items():
        rows.sort(key=lambda row: row[0])
        sibling_timestamps[key] = [row[0] for row in rows]

    linked = list(events)
    for index in bonus_indexes:
        bonus = events[index]
        rows = trigger_rows.get((bonus.source_id, bonus.target_id), ())
        stamps = timestamps.get((bonus.source_id, bonus.target_id), ())
        candidates = (
            rows[
                bisect_left(stamps, bonus.timestamp_ms - _BEFORE_MS) :
                bisect_right(stamps, bonus.timestamp_ms + _AFTER_MS)
            ]
            if rows
            else ()
        )
        identities = {(row[2], row[3]) for row in candidates}
        method = "same_actor_target_unique_hit"
        confidence = 0.96
        if len(identities) > 1:
            max_steps = _PROC_MAX_STEPS[str(bonus.skill_id)]
            identities = {
                (skill_id, skill_name)
                for _timestamp, amount, skill_id, skill_name in candidates
                if _ratio_matches(bonus.amount, amount, max_steps)
            }
            method = "same_actor_target_hit_ratio"
            confidence = 1.0
        if len(identities) != 1:
            sibling_rows = linked_sibling_rows.get(
                (bonus.source_id, bonus.target_id), ()
            )
            sibling_stamps = sibling_timestamps.get(
                (bonus.source_id, bonus.target_id), ()
            )
            siblings = sibling_rows[
                bisect_left(sibling_stamps, bonus.timestamp_ms - _BEFORE_MS) :
                bisect_right(sibling_stamps, bonus.timestamp_ms + _AFTER_MS)
            ]
            sibling_identities = {
                (skill_id, skill_name)
                for _timestamp, skill_id, skill_name, _confidence in siblings
                if skill_id or skill_name
            }
            if len(sibling_identities) != 1:
                continue
            identities = sibling_identities
            method = "same_actor_target_unique_linked_sibling"
            # One weak recent-cast row is not enough on its own.  The explicit
            # Crow/Nine-Heavens packet, identical actor+target, <=120 ms gap
            # and a single agreeing trigger identity make the combined link
            # suitable for composition, while conflicting siblings still fail
            # closed.
            confidence = 0.95
        trigger_skill_id, trigger_skill_name = next(iter(identities))
        if (
            bonus.trigger_skill_id == trigger_skill_id
            and bonus.trigger_skill_name == trigger_skill_name
        ):
            continue
        linked[index] = replace(
            bonus,
            trigger_skill_id=trigger_skill_id,
            trigger_skill_name=trigger_skill_name,
            trigger_attribution_method=method,
            trigger_attribution_confidence=confidence,
        )
    return tuple(linked)
