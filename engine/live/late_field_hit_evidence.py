"""Proven last-two-second object joins AFTER normal damage/Hit admission.

No new damage, real-hit flags, entities, casts or primary object lifetimes.
The archived origin still has to pass every owner/generation/revocation gate.
"""
from dataclasses import replace

from .damage_attribution import AttributionLayer, attribution_layer, is_deterministic_attribution
from .models import CombatEvent
from .udp_damage_records import inspect_udp_damage_records


def eligible(event):
    return (isinstance(event, CombatEvent) and not event.healing and event.amount > 0
            and event.has_real_hit_evidence is True
            and str(event.skill_id).isdigit() and int(event.skill_id) > 0
            and event.wire_source_actor_id > 65535 and not event.wire_entity_id
            and event.source_id == f'0x{event.wire_source_actor_id:08X}'
            # Boss display IDs carry @template to distinguish reused runtime
            # IDs. The exact wire target is checked again against THIS record.
            and event.target_id.partition('@')[0] == f'0x{event.wire_target_actor_id:08X}'
            and event.wire_damage_object_id > 0
            and 'direct37' in event.wire_damage_layout
            and event.attribution_method != 'direct_hit_overflow'
            and not is_deterministic_attribution(event.attribution_method)
            and attribution_layer(event.attribution_method) is not AttributionLayer.STATUS)


def annotate_late_field_hits(events, packet, ledger, packet_serial, *, skill_catalog, job_conflicts):
    candidates = [(i, e) for i, e in enumerate(events) if eligible(e)]
    if not candidates:
        return events
    # Most unrelated packets fail the cheap object/lifetime lookup first.
    matches = []
    for index, event in candidates:
        proof = ledger.attribute_direct37(source_actor_id=event.wire_source_actor_id,
            timestamp=packet.timestamp_seconds, layout=event.wire_damage_layout,
            payload=packet.payload, direction=packet.inferred_direction(), damage=event.amount,
            record_offset=event.wire_record_offset, packet_serial=packet_serial,
            _post_admitted_late=True)
        if (proof is not None and proof.deterministically_validated
                and proof.skill_id in skill_catalog
                and not job_conflicts(proof.owner_actor_id, proof.skill_id)):
            matches.append((index, event, proof))
    if not matches:
        return events
    inventory = inspect_udp_damage_records(packet.payload, packet.inferred_direction())
    if not inventory.complete:
        return events
    output = list(events)
    for index, event, proof in matches:
        key = (event.wire_record_offset, event.wire_source_actor_id,
               event.wire_target_actor_id, event.amount, event.critical,
               event.wire_result_group_id, event.wire_damage_object_id)
        rows = [r for r in inventory.damage_records
                if r.admissible_fields and r.family == 'direct37' and r.physical_key == key]
        allowed = {0, 0xffffffff, 5218} if proof.projectile_template_id == 5508 else {0, 0xffffffff, 6401, 6553}
        if len(rows) != 1 or any(v not in allowed for _, v in rows[0].raw_skill_slots):
            continue
        # Annotation only, including preserved raw skill metadata and hit bit.
        output[index] = replace(event, skill_id=str(proof.skill_id),
            skill_name=skill_catalog[proof.skill_id][0],
            attribution_method='damage_object_projectile_link',
            cast_instance_id=(
                f'damage-object:{proof.owner_actor_id:08X}:'
                f'{proof.damage_object_id}:{proof.generation}'
            ))
    return output
