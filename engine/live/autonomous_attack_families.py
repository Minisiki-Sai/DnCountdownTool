"""Post-admission skill evidence for independently audited autonomous entities.

Not a general summon-template fallback. These audited AI/ACT closures have one
player skill family; their raw 08/73 result may legitimately carry FFFFFFFF in
the self-attack slot. Unknown positive slots, PvP and unproved lives fail closed.
The caller supplies the owner proof obtained for THIS event in the current
ledger, immediately after existing damage admission. Never call with a cache.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from types import MappingProxyType

from .damage_attribution import attribution_layer, AttributionLayer, is_deterministic_attribution
from .summon_self_attack_evidence import PhysicalHit, read_self_attack_slot
from .dungeon_map_catalog import resolve_dungeon_map_id, is_battle_map
from .summon_attack_runtime import SUMMON_SELF_ATTACK_SPECS
from .pve_scene_evidence import confirmed_scene_target
from .duck_rune_self_attack import RUNE_DUCK_SELF_ATTACK_SPECS


# Resource closure + raw anchors:
# outputs/v26_engineer_towers_20260908/{REPORT.md,machine_map_extension.json}
# outputs/v26_magmawall_family_closure_20260908/{REPORT.md,machine_map_proposal.json}
# The wall proof is canonical-family reachability in the resource-declared
# routing model, NOT proof of the exact EX/Clone branch or native-engine
# completeness. All bound reachable 32022 creators normalize to 4410; the
# unbound Clone has no incoming edge in the complete audited resource graph.
# Intrinsic-only AI/ACT attacks retain that family. m114 is only a redundant
# consistency guard below, never a general creation-skill -> current-attack map.
# outputs/magma_canonical_v37_20260908/{REPORT.md,evidence.json}
AUTONOMOUS_ATTACK_FAMILIES = MappingProxyType({
    32002: (4207, frozenset({0xffffffff, 30805}), 'tower-v26-32002'),
    32003: (4208, frozenset({0xffffffff, 30806}), 'tower-v26-32003'),
    32029: (4351, frozenset({0xffffffff, 293166}), 'tower-v26-32029'),
    32015: (4410, frozenset({0xffffffff}), 'magmawall-v26-32015'),
    32022: (4410, frozenset({0xffffffff}), 'magmawall-v37-32022-canonical-resource-closure'),
})

# An explicit own-skill ID can establish its DNT display identity without
# claiming a copied player move. The exact installed AI skill 262494 and 6319
# share NameID 1000065539; 6319 is the unique player skill with that name key.
# This is NOT a whole-clone fallback, a context/m114 alias, or an animation
# suffix match. The EX -> Landing edge remains unproved and is not asserted.
DIRECT_ENTITY_SKILL_IDENTITIES = MappingProxyType({
    # 262494 is the clone AI's own Shadow Summon attack slot.  The eight
    # ShadowR/ShadowL bodies are interchangeable spawn variants of the same
    # 136900 generation; the current entity birth + explicit 34/2c owner
    # record below remains mandatory for every Hit, so this cannot cross to a
    # clone owned by another Dark/Light actor.
    **{
        (template, 262494): (
            6319,
            '9a363626a925d0e361eb5ca8e477d1cbfd20fe7cbd88f06b01d1a648033b1cab',
        )
        for template in range(33607, 33615)
    },
})


def confirmed_pve_target(host, target_id, packet=None):
    """Boss, known dungeon monster, or an independently player-proven target.

    Generic damage history is not sufficient.  The narrow direct-player set is
    populated only when the original wire source is an identified player (not
    a summon/object), and the target is still rejected if it is any known
    player.  This covers captures where the server omits the target's monster
    birth row but ordinary player attacks have already established the boss.
    """
    target = host.monster_entities.get(target_id)
    if any(target_id in getattr(host, key, ()) for key in
           ('skill_actor_ids','local_actor_ids','known_actor_names')):
        return False
    if target is None:
        return bool(
            target_id in getattr(host, 'direct_player_confirmed_damage_target_ids', ())
            and target_id == getattr(host, 'boss_actor_id', None)
        )
    if target.boss:
        return True
    if confirmed_scene_target(host,target_id,packet):
        return True
    map_id = getattr(host,'detected_map_id',0)
    if not getattr(host,'detected_map_active',False) or not is_battle_map(resolve_dungeon_map_id(map_id)):
        return False
    history = getattr(host,'detected_map_history',())
    if not history:
        return False
    row = history[-1]
    return bool(row.get('map_id') == map_id and row.get('left_at') is None
        and row.get('confidence') == 'exact_map_id' and row.get('inferred') is False
        and str(row.get('layout','')).startswith('s2c-'))


def confirmed_registered_monster_from_player_hit(host, target_id):
    """Narrow PvE proof for post-admission autonomous-family annotation.

    This deliberately does not broaden :func:`confirmed_pve_target`, which is
    also consumed by native-key and selector routes with different negative
    gates.  The target must already be a server-registered monster and must
    independently have received a real direct Hit from a known player.
    """
    target = getattr(host, 'monster_entities', {}).get(target_id)
    if target is None:
        return False
    if any(target_id in getattr(host, key, ()) for key in
           ('skill_actor_ids', 'local_actor_ids', 'known_actor_names')):
        return False
    return target_id in getattr(
        host, 'direct_player_confirmed_damage_target_ids', ()
    )


def annotate_exact_current_attack(event, context, owner_proof, *, pve_confirmed,
                                  skill_catalog, job_conflicts):
    """The existing exact self-skill table, after original output admission.

    This covers known non-Boss monsters only when the *scene* independently
    proves PvE. It never changes the primary decoder or target-repair gate.
    """
    if (owner_proof is None or pve_confirmed is not True or event.healing
            or event.amount <= 0 or event.has_real_hit_evidence is not True
            or not str(event.skill_id).isdigit() or int(event.skill_id) <= 0
            or event.attribution_method == 'direct_hit_overflow'
            or is_deterministic_attribution(event.attribution_method)
            or attribution_layer(event.attribution_method) is AttributionLayer.STATUS):
        return event
    if (event.source_id != f'0x{owner_proof.owner_id:08X}'
            or event.wire_source_actor_id != owner_proof.entity_id
            or event.wire_entity_id != owner_proof.entity_id
            or event.entity_template_id != owner_proof.template_id
            or event.entity_owner_evidence_id != owner_proof.owner_evidence_id
            or context.generation != owner_proof.generation
            or context.order[2] != owner_proof.record_offset
            or event.wire_record_offset != owner_proof.record_offset
            or sha256(context.wire).hexdigest() != owner_proof.hit_wire_sha256):
        return event
    hit=PhysicalHit('current-exact-owner-proof',
        f'{owner_proof.hit_wire_sha256}:{context.order[2]}', context.order,
        owner_proof.entity_id, owner_proof.generation, owner_proof.template_id,
        event.wire_target_actor_id, event.amount,event.wire_damage_object_id,
        event.wire_result_group_id,context.wire,context.direction)
    slot=read_self_attack_slot(hit)
    spec=SUMMON_SELF_ATTACK_SPECS.get((hit.template_id,slot.monster_skill_id)) if slot else None
    if spec is None and slot is not None:
        # These new rune proofs are metadata-only after existing admission;
        # deliberately not added to the primary decoder's target/admission map.
        spec=RUNE_DUCK_SELF_ATTACK_SPECS.get((hit.template_id,slot.monster_skill_id))
    identity=DIRECT_ENTITY_SKILL_IDENTITIES.get((hit.template_id,slot.monster_skill_id)) if slot else None
    if spec is not None and spec.encounter_mode == 'pve':
        group=6319 if 33607 <= hit.template_id <= 33614 else spec.player_skill_id
        attack,proof,method=spec.player_skill_id,spec.proof_id,'packet_entity_self_attack'
    elif identity is not None:
        group,proof=identity
        attack,method=0,'packet_entity_skill_id'
    else:
        return event
    info=skill_catalog.get(group)
    if not info or job_conflicts(owner_proof.owner_id,group):
        return event
    token=':'.join((proof,owner_proof.hit_wire_sha256,str(context.order[2]),
        str(context.generation),owner_proof.birth_fingerprint,owner_proof.owner_fingerprint))
    return replace(event,skill_id=str(group),skill_name=info[0],
        attribution_method=method,cast_instance_id='',
        entity_monster_skill_id=slot.monster_skill_id,entity_attack_skill_id=attack,
        entity_attack_evidence_id=sha256(token.encode('ascii')).hexdigest())


def annotate_autonomous_attack(event, context, owner_proof, *, pve_confirmed,
                               skill_name, job_conflicts=False):
    """Annotate an already named real hit; preserve all physical/owner fields."""
    if (owner_proof is None or pve_confirmed is not True or job_conflicts
            or event.has_real_hit_evidence is not True or event.healing
            or event.amount <= 0 or not str(event.skill_id).isdigit()
            or int(event.skill_id) <= 0 or not skill_name
            or event.attribution_method == 'direct_hit_overflow'
            or attribution_layer(event.attribution_method) is AttributionLayer.STATUS
            or is_deterministic_attribution(event.attribution_method)):
        return event
    spec = AUTONOMOUS_ATTACK_FAMILIES.get(owner_proof.template_id)
    if spec is None:
        return event
    skill, accepted_slots, certificate = spec
    if (event.source_id != f'0x{owner_proof.owner_id:08X}'
            or event.wire_source_actor_id != owner_proof.entity_id
            or event.wire_entity_id != owner_proof.entity_id
            or event.entity_template_id != owner_proof.template_id
            or event.entity_owner_evidence_id != owner_proof.owner_evidence_id
            or context.generation != owner_proof.generation
            or context.order[2] != owner_proof.record_offset
            or event.wire_record_offset != owner_proof.record_offset
            or sha256(context.wire).hexdigest() != owner_proof.hit_wire_sha256):
        return event
    physical = PhysicalHit('current-owner-proof',
        f'{owner_proof.hit_wire_sha256}:{context.order[2]}', context.order,
        owner_proof.entity_id, owner_proof.generation, owner_proof.template_id,
        event.wire_target_actor_id, event.amount, event.wire_damage_object_id,
        event.wire_result_group_id, context.wire, context.direction)
    slot = read_self_attack_slot(physical)
    if slot is None or slot.monster_skill_id not in accepted_slots:
        return event
    if owner_proof.template_id == 32022 and slot.context_skill_id != 4410:
        # Unknown/contradictory context cannot extend the resource certificate.
        # This does not infer m114 semantics or borrow an earlier hit's value.
        return event
    token = ':'.join((certificate, owner_proof.hit_wire_sha256,
        str(context.order[2]), str(owner_proof.generation),
        owner_proof.birth_fingerprint, owner_proof.owner_fingerprint))
    return replace(event, skill_id=str(skill), skill_name=skill_name,
        attribution_method='packet_entity_template_owner', cast_instance_id='',
        entity_monster_skill_id=slot.monster_skill_id,
        entity_attack_skill_id=skill,
        entity_attack_evidence_id=sha256(token.encode('ascii')).hexdigest())
