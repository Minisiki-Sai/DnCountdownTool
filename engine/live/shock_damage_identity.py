from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from typing import Iterable, Sequence

from .models import CombatEvent, SkillParameterEvent
from .damage_attribution import is_locked_attribution


_THUNDER_FLOW_SKILL_ID = "3551"
_LIGHTNING_IMPACT_SKILL_ID = "3202"
_THUNDER_FLOW_PACKET_TOLERANCE_MS = 5
_IMPACT_PULSE_INTERVAL_MS = 2_000
_IMPACT_PULSE_WINDOW_MS = 250
_IMPACT_BURST_SPAN_MS = 25
_IMPACT_PHASE_EARLY_MS = 50
_IMPACT_ATTACK_PARAMETERS = {
    "Skill_LightningCharger_Attack": 8,
    "Skill_LightningCharger_EX_Attack": 10,
}


def _base_actor_id(value: object) -> str:
    """Return the runtime actor portion of an optional ``@template`` id."""

    return str(value or "").strip().partition("@")[0].casefold()


def _is_generic_shock_damage(event: CombatEvent) -> bool:
    skill_id = str(event.skill_id or "").strip().casefold()
    method = str(event.attribution_method or "").strip().casefold()
    return bool(
        not event.healing
        and event.amount > 0
        and (
            skill_id == "periodic:shock"
            or (
                str(event.skill_name or "").strip() == "触电"
                and method.startswith("periodic_")
            )
        )
    )


def is_shock_damage_identity_parameter(event: SkillParameterEvent) -> bool:
    """Return whether a response can identify one otherwise generic shock row.

    Merely applying an electrocution Debuff is intentionally insufficient.
    Thunder Flow has a unique same-generation response and Lightning Impact
    creates a named attack object with a server-declared lifetime.  Those are
    the two packet identities proven by the v576 client and 0808 capture.
    """

    skill_id = str(event.skill_id or event.raw_skill_id or "").strip()
    parameters = str(event.parameters or "").strip()
    if skill_id == _THUNDER_FLOW_SKILL_ID:
        return bool(
            event.effect_class_id == 43
            and parameters in {"1", "1.0"}
            and event.duration_ms == 5_000
        )
    if skill_id == _LIGHTNING_IMPACT_SKILL_ID:
        return bool(
            event.effect_class_id == 62
            and parameters in _IMPACT_ATTACK_PARAMETERS
            and event.duration_ms > 0
        )
    return False


def _reconcile_impact_direct_bursts(
    events: Sequence[CombatEvent],
    parameters: Sequence[SkillParameterEvent],
) -> dict[int, CombatEvent]:
    """Bind complete object blocks, including split and cross-target pulses.

    Each named normal/EX response authorizes one eight/ten-object block per
    phase. Simultaneous target responses can cross-hit: count distinct objects
    across those targets rather than requiring exactly ten rows per target.
    Proven other skills and incomplete blocks are never replaced.
    """
    eligible: dict[str, list[tuple[int, CombatEvent]]] = defaultdict(list)
    for index, event in enumerate(events):
        if (
            event.healing
            or event.amount <= 0
            or (event.has_real_hit_evidence is not True
                and not _is_generic_shock_damage(event))
            or "direct37" not in str(event.wire_damage_layout or "").casefold()
            or not 0 < event.wire_damage_object_id < 0xFFFFFFFF
        ):
            continue
        eligible[str(event.source_id or "").strip().casefold()].append((index, event))
    response_groups: list[list[SkillParameterEvent]] = []
    for response in sorted(parameters, key=lambda row: (
        str(row.source_id).casefold(), str(row.parameters), row.timestamp_ms
    )):
        if not is_shock_damage_identity_parameter(response):
            continue
        if str(response.parameters).strip() not in _IMPACT_ATTACK_PARAMETERS:
            continue
        previous = response_groups[-1][0] if response_groups else None
        if (
            previous is None
            or str(previous.source_id).casefold() != str(response.source_id).casefold()
            or previous.parameters != response.parameters
            or response.timestamp_ms - previous.timestamp_ms > 5
        ):
            response_groups.append([])
        # Repeated packets for one target do not authorize extra object blocks.
        if all(_base_actor_id(row.target_id) != _base_actor_id(response.target_id)
               for row in response_groups[-1]):
            response_groups[-1].append(response)
    replacements: dict[int, CombatEvent] = {}
    for group in response_groups:
        response = group[0]
        expected = _IMPACT_ATTACK_PARAMETERS[str(response.parameters).strip()]
        source = str(response.source_id or "").strip().casefold()
        targets = {_base_actor_id(row.target_id) for row in group}
        start = int(response.timestamp_ms)
        end = min(int(row.timestamp_ms) + int(row.duration_ms) for row in group)
        for phase in range(4):
            phase_start = start + phase * _IMPACT_PULSE_INTERVAL_MS
            if phase_start > end:
                break
            phase_end = min(phase_start + _IMPACT_PULSE_WINDOW_MS, end)
            if phase == 3:
                phase_end = end
            phase_rows = [
                (index, event) for index, event in eligible.get(source, ())
                if _base_actor_id(event.target_id) in targets
                and phase_start - _IMPACT_PHASE_EARLY_MS <= event.timestamp_ms <= phase_end
            ]
            blocked_objects = {
                event.wire_damage_object_id for _, event in phase_rows
                if is_locked_attribution(event.attribution_method)
                and str(event.skill_id) != _LIGHTNING_IMPACT_SKILL_ID
                and not _is_generic_shock_damage(event)
            }
            candidates = sorted(
                (
                    (index, event)
                    for index, event in phase_rows
                    if index not in replacements
                    and event.wire_damage_object_id not in blocked_objects
                ),
                key=lambda row: row[1].timestamp_ms,
            )
            bursts: list[list[tuple[int, CombatEvent]]] = []
            for item in candidates:
                if not bursts or item[1].timestamp_ms - bursts[-1][0][1].timestamp_ms > _IMPACT_BURST_SPAN_MS:
                    bursts.append([])
                bursts[-1].append(item)
            # The final declared pulse can arrive in separate datagrams. Join
            # only a complete, contiguous set of all named response blocks.
            if phase == 3 and len(bursts) > 1:
                ids = {event.wire_damage_object_id for _, event in candidates}
                if (len(ids) == expected * len(group)
                        and max(ids) - min(ids) == len(ids) - 1):
                    bursts = [candidates]
            for burst in bursts:
                object_ids = {event.wire_damage_object_id for _, event in burst}
                if (
                    not object_ids
                    or len(object_ids) % expected != 0
                    or len(object_ids) > expected * len(group)
                    or max(object_ids) - min(object_ids) != len(object_ids) - 1
                ):
                    continue
                for index, event in burst:
                    # Status-classified rows may complete the object's shape,
                    # but retain their factual periodic route in the next pass.
                    if (event.has_real_hit_evidence is not True
                            or is_locked_attribution(event.attribution_method)):
                        continue
                    replacements[index] = replace(
                        event,
                        skill_id=_LIGHTNING_IMPACT_SKILL_ID,
                        skill_name="雷电冲击",
                        attribution_method="named_attack_response_phase",
                        raw_skill_id=event.raw_skill_id or event.skill_id,
                        raw_skill_name=event.raw_skill_name or event.skill_name,
                        raw_attribution_method=(
                            event.raw_attribution_method or event.attribution_method
                        ),
                        cast_instance_id=(
                            f"named-attack:3202:{source}:{_base_actor_id(event.target_id)}:{start}:{phase}"
                        ),
                        attribution_confidence=1.0,
                    )
    return replacements


def reconcile_shock_damage_identity(
    events: Sequence[CombatEvent],
    parameter_events: Iterable[SkillParameterEvent],
) -> tuple[CombatEvent, ...]:
    """Restore skill identity which the periodic-kind decoder intentionally lost.

    The wire decoder first classifies these rows as the factual damage family
    ``shock``.  This second pass runs after adjacent skill-response records have
    arrived, so it can preserve two stronger identities without using a recent
    cast guess:

    * Thunder Flow: same source, target and compound response generation
      (the response may trail the damage row by a few milliseconds).
    * Lightning Impact: same source/target inside the named attack object's
      declared lifetime.

    All other electrocution rows stay generic.  In particular, a target merely
    having an electrocution Debuff never proves either damage skill.
    """

    parameter_rows = tuple(parameter_events)
    direct_replacements = _reconcile_impact_direct_bursts(events, parameter_rows)
    flow_times: dict[tuple[str, str], list[int]] = defaultdict(list)
    impact_windows: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for parameter in parameter_rows:
        if not is_shock_damage_identity_parameter(parameter):
            continue
        key = (
            str(parameter.source_id or "").strip().casefold(),
            _base_actor_id(parameter.target_id),
        )
        skill_id = str(
            parameter.skill_id or parameter.raw_skill_id or ""
        ).strip()
        if skill_id == _THUNDER_FLOW_SKILL_ID:
            flow_times[key].append(int(parameter.timestamp_ms))
        else:
            start = int(parameter.timestamp_ms)
            impact_windows[key].append(
                (start, start + max(0, int(parameter.duration_ms)))
            )

    for rows in flow_times.values():
        rows.sort()
    for rows in impact_windows.values():
        rows.sort()

    reconciled: list[CombatEvent] = []
    for index, original_event in enumerate(events):
        event = direct_replacements.get(index, original_event)
        if not _is_generic_shock_damage(event):
            reconciled.append(event)
            continue
        key = (
            str(event.source_id or "").strip().casefold(),
            _base_actor_id(event.target_id),
        )
        timestamp_ms = int(event.timestamp_ms)
        flow = flow_times.get(key, ())
        lower = bisect_left(
            flow, timestamp_ms - _THUNDER_FLOW_PACKET_TOLERANCE_MS
        )
        upper = bisect_right(
            flow, timestamp_ms + _THUNDER_FLOW_PACKET_TOLERANCE_MS
        )
        if lower < upper:
            reconciled.append(
                replace(
                    event,
                    skill_id=_THUNDER_FLOW_SKILL_ID,
                    skill_name="冲击流",
                    attribution_method="status_damage_skill",
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method or event.attribution_method
                    ),
                    attribution_confidence=1.0,
                )
            )
            continue

        windows = impact_windows.get(key, ())
        position = bisect_right(windows, (timestamp_ms, 1 << 62))
        active_impact = any(
            start <= timestamp_ms <= end
            for start, end in windows[max(0, position - 3) : position]
        )
        if active_impact:
            reconciled.append(
                replace(
                    event,
                    skill_id=_LIGHTNING_IMPACT_SKILL_ID,
                    skill_name="雷电冲击",
                    attribution_method="periodic_status_packet",
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method or event.attribution_method
                    ),
                    attribution_confidence=1.0,
                )
            )
            continue
        reconciled.append(event)
    return tuple(reconciled)
