"""Reviewed, single-result S2C transactions for Lightning Storm damage.

3407 explicitly applies its EC43 to the damaged target (EffectApplyType=1).
Attribution additionally requires the exact reviewed skill/parameters and a
unique physical result in the SAME complete datagram. There is no state,
time window, recent-cast or future-UID fallback.

3551 is deliberately excluded: EC168 creates an independent proc skill, but
its EC43 may accompany an independently proven parent-skill object. A unique
Hit plus this state alone cannot distinguish parent damage from proc damage.
3557/EC177 describes additional damage, and never identifies its primary Hit.
The packet pairing is capture evidence; the client image independently proves
the skill/effect identities, not a server transaction writer.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from .damage_attribution import is_deterministic_attribution
from .models import CombatEvent
from .player_root_action_objects import _typed_records
from .udp_damage_records import inspect_udp_damage_records


COMPOUND_RESPONSE_METHOD = "packet_compound_status_response"
_REVIEWED = {
    (3407, 12000, "1;-0.15"): "闪电风暴",
}


def _ec43_record(raw: bytes):
    if len(raw) < 37 or raw[4] != 19:
        return None
    if int.from_bytes(raw[24:28], "little") != 43:
        return None
    size = int.from_bytes(raw[32:36], "little")
    if size > len(raw) - 37 or raw[36 + size] != 0:
        return None
    try:
        parameters = raw[36:36 + size].decode("ascii")
    except UnicodeDecodeError:
        return None
    source = int.from_bytes(raw[7:11], "little")
    target = int.from_bytes(raw[:4], "little")
    skill = int.from_bytes(raw[16:20], "little")
    duration = int.from_bytes(raw[28:32], "little", signed=True)
    return source, target, skill, duration, parameters


def _eligible(event: object) -> bool:
    return bool(
        isinstance(event, CombatEvent)
        and not event.healing and event.amount > 0
        # 3407 belongs to the Priest base tree (NeedJob22). Both current
        # specializations can emit the same reviewed server transaction.
        and event.job in {"雷神", "圣徒"}
        and (
            event.has_real_hit_evidence is True
            or (event.has_real_hit_evidence is False
                and event.skill_id == "periodic:shock"
                and event.attribution_method in {"periodic_status", "periodic_status_packet"})
        )
        and "direct37" in event.wire_damage_layout
        and event.wire_source_actor_id > 0xFFFF
        and 0 < event.wire_target_actor_id <= 0xFFFF
        and event.wire_record_offset > 0
    )


def attribute_compound_skill_responses(events, packet, *, job_conflicts=None):
    """Relabel existing results only; retain all physical facts and cast counts."""
    rows = list(events)
    eligible = [(index, event) for index, event in enumerate(rows) if _eligible(event)]
    if (not eligible or str(packet.transport).upper() != "UDP"
            or packet.inferred_direction() != "server_to_client"):
        return rows
    # The production typed walker is only a cheap prefilter. The independent
    # inventory below checks every phase and the complete declared message.
    typed = _typed_records(packet.payload, "server_to_client")
    if not any(kind == 19 and (response := _ec43_record(raw))
               and response[2:] in _REVIEWED for kind, _offset, raw in typed):
        return rows
    inventory = inspect_udp_damage_records(packet.payload, "server_to_client")
    if not inventory.complete:
        return rows
    responses = defaultdict(list)
    primary = defaultdict(list)
    for record in inventory.typed_records:
        raw = record.record_bytes
        if record.record_type == 19:
            response = _ec43_record(raw)
            if response:
                responses[response[:2]].append((record.record_offset, response[2:]))
            elif len(raw) >= 28 and int.from_bytes(raw[24:28], "little") == 43:
                # An opaque/malformed EC43 is still a competing response.
                pair = (int.from_bytes(raw[7:11], "little"),
                        int.from_bytes(raw[:4], "little"))
                responses[pair].append((record.record_offset, None))
        elif record.record_type == 8 and len(raw) >= 16:
            # Unsupported/invalid type-08 rows still compete for identity.
            pair = (int.from_bytes(raw[12:16], "little"),
                    int.from_bytes(raw[:4], "little"))
            primary[pair].append(record.record_offset)
    for pair, response_rows in responses.items():
        if len(response_rows) != 1 or len(primary[pair]) != 1:
            continue
        status_offset, signature = response_rows[0]
        if signature not in _REVIEWED:
            continue
        skill_id = signature[0]
        if job_conflicts and job_conflicts(pair[0], skill_id):
            continue
        physical = [hit for hit in inventory.damage_records
                    if (hit.source_actor_id, hit.target_actor_id) == pair]
        if len(physical) != 1:
            continue
        hit = physical[0]
        if (not hit.admissible_fields or hit.family != "direct37"
                or dict(hit.raw_skill_slots).get(102) != 0xFFFFFFFF
                or any(value not in (0, 0xFFFFFFFF) for _, value in hit.raw_skill_slots)):
            continue
        matches = [(index, event) for index, event in eligible
                   if (event.wire_source_actor_id, event.wire_target_actor_id) == pair
                   and event.wire_record_offset == hit.record_offset
                   and event.amount == hit.damage and event.critical == hit.critical
                   and event.wire_result_group_id == hit.damage_result_group_id
                   and event.wire_damage_object_id == hit.damage_object_id]
        if len(matches) != 1:
            continue
        index, event = matches[0]
        if (event.skill_id == str(skill_id) and event.skill_name == _REVIEWED[signature]
                and is_deterministic_attribution(event.attribution_method)):
            continue
        # A fixed key, object generation or entity proof vetoes a contradictory
        # response. Old generic shock/nearby-status labels can be corrected by
        # this exact packet certificate (the old +/-5 ms path conflated packets).
        if (is_deterministic_attribution(event.attribution_method)
                and event.attribution_method not in
                {"periodic_status_packet", "status_damage_skill"}):
            continue
        evidence = (f"compound-status:{skill_id}:{inventory.wire_sha256}:"
                    f"{pair[0]:08X}:{pair[1]:08X}:{hit.record_offset}:{status_offset}")
        rows[index] = replace(
            event, skill_id=str(skill_id), skill_name=_REVIEWED[signature],
            attribution_method=COMPOUND_RESPONSE_METHOD,
            cast_instance_id=evidence, attribution_confidence=1.0,
            # Some legacy shock decodes marked a genuine top-level type-08
            # row as a status tick. The complete inventory and every matching
            # physical field above now independently prove this exact Hit.
            has_real_hit_evidence=True,
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=event.raw_attribution_method or event.attribution_method,
        )
    return rows
