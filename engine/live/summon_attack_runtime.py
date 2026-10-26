"""Bind an EXISTING real hit to an entity's own independently proved attack.

No timers, raw-hit emission, new owner cache, or inferred player casts. The
full per-hit audit remains reconstructible from the original capture.
"""
from hashlib import sha256
from dataclasses import replace
from types import MappingProxyType

from .generated_summon_self_attack_specs import SUMMON_SELF_ATTACK_SPECS as _BASE_SPECS
from .generated_mirror_self_attack_specs import MIRROR_SELF_ATTACK_SPECS
from .entity_owner_evidence import entity_owner_evidence_id
from .summon_self_attack_evidence import (
    EntityLife, PhysicalHit, annotate_hit, decrypt, read_self_attack_slot, KEY,
)

def _same_semantics(left, right):
    return (
        left.template_id == right.template_id
        and left.monster_skill_id == right.monster_skill_id
        and left.player_skill_id == right.player_skill_id
        and left.actions == right.actions
        and left.projectile_ids == right.projectile_ids
        and left.encounter_mode == right.encounter_mode
    )


if any(key in _BASE_SPECS and not _same_semantics(_BASE_SPECS[key], spec)
       for key, spec in MIRROR_SELF_ATTACK_SPECS.items()):
    raise ValueError("Conflicting independently audited entity attack specs")
# Prefer the independently replayed two-capture certificate on an otherwise
# identical row; the static catalog still supplies every newly expanded pair.
SUMMON_SELF_ATTACK_SPECS = MappingProxyType({**_BASE_SPECS, **MIRROR_SELF_ATTACK_SPECS})
_TEMPLATES = frozenset(key[0] for key in SUMMON_SELF_ATTACK_SPECS)


def _unique_recovery_specs():
    by_attack = {}
    ambiguous = set()
    for (template_id, monster_skill_id), spec in SUMMON_SELF_ATTACK_SPECS.items():
        identity = (template_id, spec.player_skill_id)
        previous = by_attack.get(monster_skill_id)
        if previous is not None and previous != identity:
            ambiguous.add(monster_skill_id)
        else:
            by_attack[monster_skill_id] = identity
    return MappingProxyType({
        attack: identity
        for attack, identity in by_attack.items()
        if attack not in ambiguous
    })


# A missing 06/27 spawn can be repaired only when the result's native entity
# attack key selects exactly one template and one parent skill in the audited
# static catalog.  Shared keys remain unresolved.
UNIQUE_RECOVERY_SPEC_BY_MONSTER_SKILL = _unique_recovery_specs()

# Only this audited singleton companion is permanently alive across normal
# combat transitions and may therefore predate the beginning of a capture.
# Ordinary summons still require their birth plus exact owner record.
_PREEXISTING_PERMANENT_SUMMON_SKILLS = MappingProxyType({
    33601: 6318,
    33603: 6316,
})


def _unique_active_profile_owner(host, player_skill_id):
    """Return one packet-confirmed compatible player, never a nearest caster."""
    skill_profiles = set(
        getattr(host, "skill_profiles", {}).get(player_skill_id, ())
    )
    if not skill_profiles:
        return None
    candidates = {
        int(actor_id)
        for actor_id, actor_profiles in getattr(
            host, "actor_skill_profiles", {}
        ).items()
        if int(actor_id) > 0xFFFF
        and int(actor_id) not in getattr(host, "monster_entities", {})
        and skill_profiles.intersection(actor_profiles)
        and not host._action_conflicts_with_confirmed_job(
            int(actor_id), player_skill_id
        )
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def prewarm_permanent_summon_lives(host, timestamp):
    """Bind reviewed permanent summons before their first combat attack.

    The probe decoder may already have the exact spawn/template generation
    when formal combat recording begins, while the owner row was emitted much
    earlier.  For the tiny audited permanent-singleton set only, combine that
    current spawn with one uniquely compatible active player profile.  This
    makes the hand-off snapshot useful immediately and still fails closed for
    duplicate eligible jobs, missing births and temporary summons.
    """

    ledger = getattr(host, "entity_owner_evidence", None)
    if ledger is None:
        return 0
    try:
        observed_at = float(timestamp)
    except (TypeError, ValueError):
        return 0
    if observed_at <= 0.0:
        return 0
    packet_serial = int(
        getattr(host, "_damage_object_packet_serial", 0) or 0
    )
    warmed = 0
    for entity_id, template in tuple(
        getattr(host, "monster_entities", {}).items()
    ):
        template_id = int(getattr(template, "template_id", 0) or 0)
        player_skill_id = _PREEXISTING_PERMANENT_SUMMON_SKILLS.get(
            template_id
        )
        if (
            player_skill_id is None
            or bool(getattr(template, "boss", False))
            or int(entity_id) in getattr(host, "summon_lethal_actor_ids", set())
        ):
            continue
        existing = ledger.resolve(int(entity_id), template_id, observed_at)
        if existing is not None:
            getattr(host, "summon_owners", {})[int(entity_id)] = (
                int(existing.record.owner_id),
                player_skill_id,
            )
            continue
        owner_id = _unique_active_profile_owner(host, player_skill_id)
        if owner_id is None:
            continue
        profile_evidence_id = sha256(
            f"unique-permanent-profile:{ledger.scene_serial}:{entity_id}:"
            f"{template_id}:{owner_id}:{player_skill_id}".encode("ascii")
        ).hexdigest()
        binding = ledger.bind_existing_permanent_unique_owner(
            int(entity_id),
            template_id,
            owner_id,
            observed_at,
            profile_evidence_id=profile_evidence_id,
            packet_serial=packet_serial,
        )
        if binding is None:
            continue
        getattr(host, "summon_owners", {})[int(entity_id)] = (
            owner_id,
            player_skill_id,
        )
        warmed += 1
    return warmed


def recover_missing_summon_life(host, packet, decoded):
    """Recover a dropped birth/template datagram without cast proximity.

    Required evidence is: a quarantined exact 34/2c owner row for this short
    actor, one complete typed direct37 result, and a native monster attack key
    that selects a unique audited summon template.  The recovered generation
    starts at the owner row and cannot claim older hits.
    """
    if getattr(decoded, "kind", None) != "damage_hit":
        return None
    entity = int(getattr(decoded, "source_actor_id", 0) or 0)
    target = int(getattr(decoded, "target_actor_id", 0) or 0)
    ledger = getattr(host, "entity_owner_evidence", None)
    if (ledger is None or not 0 < entity < 0x10000
            or entity in getattr(host, "monster_entities", {})
            or entity in getattr(host, "skill_actor_ids", set())
            or entity in getattr(host, "local_actor_ids", set())
            or entity in getattr(host, "known_actor_names", {})
            or target not in getattr(host, "monster_entities", {})):
        return None
    order = (getattr(host, "_damage_object_packet_serial", 0), 0,
             int(getattr(decoded, "record_offset", 0) or 0))
    wire_hash = sha256(packet.payload).hexdigest()
    physical = PhysicalHit(
        str(id(ledger)), f"{wire_hash}:{order[2]}", order, entity, 0, 0,
        target, int(getattr(decoded, "damage", 0) or 0),
        int(getattr(decoded, "damage_object_id", 0) or 0),
        int(getattr(decoded, "damage_result_group_id", 0) or 0),
        packet.payload, packet.inferred_direction(),
    )
    slot = read_self_attack_slot(physical)
    if slot is None:
        return None
    recovered = UNIQUE_RECOVERY_SPEC_BY_MONSTER_SKILL.get(slot.monster_skill_id)
    if recovered is None:
        return None
    template_id, player_skill_id = recovered
    template = getattr(host, "monster_templates", {}).get(template_id)
    if template is None or getattr(template, "boss", False):
        return None
    attack_evidence_id = sha256(
        f"unique-summon-attack:{wire_hash}:{order[2]}:{slot.monster_skill_id}:"
        f"{template_id}:{player_skill_id}".encode("ascii")
    ).hexdigest()
    preexisting_recovered = False
    binding = ledger.recover_birth_from_attack(
        entity,
        template_id,
        packet.timestamp_seconds,
        attack_evidence_id=attack_evidence_id,
        packet_serial=order[0],
    )
    if (
        binding is None
        and _PREEXISTING_PERMANENT_SUMMON_SKILLS.get(template_id)
        == player_skill_id
    ):
        owner = _unique_active_profile_owner(host, player_skill_id)
        if owner is not None:
            binding = ledger.recover_preexisting_permanent_from_attack(
                entity,
                template_id,
                owner,
                packet.timestamp_seconds,
                attack_evidence_id=attack_evidence_id,
                packet_serial=order[0],
            )
            preexisting_recovered = binding is not None
    if binding is None:
        return None

    # Install exactly the recovered current generation.  These are the same
    # stale actor caches cleared by an ordinary spawn packet.
    host.monster_entities[entity] = template
    host.boss_actor_ids.discard(entity)
    host.skill_actor_ids.discard(entity)
    host.local_actor_ids.discard(entity)
    host.actor_jobs.pop(entity, None)
    host.actor_skill_profiles.pop(entity, None)
    host.summon_current_hp.pop(entity, None)
    host.summon_lethal_actor_ids.discard(entity)
    for object_key in tuple(host.recent_skill_object_owners):
        if entity in object_key:
            host.recent_skill_object_owners.pop(object_key, None)
    owner = binding.record.owner_id
    host.summon_owners[entity] = (owner, player_skill_id)
    ledger.observe_hit(entity, template_id, packet.timestamp_seconds)
    return (
        owner,
        player_skill_id,
        template_id,
        binding.generation,
        preexisting_recovered,
    )


def repair_proven_entity_target(host, packet, decoded):
    """Repair only an existing result whose exact entity attack proves m-4.

    The legacy compound reader uses outer+14 for some short-source records;
    this is an owner/context ID in the audited self-attack records. A raw
    marker-target candidate alone is insufficient: resolve the complete
    current birth/owner/attack/PvE proof before changing the primary identity.
    No record is emitted here and other compound families are untouched.
    """
    if getattr(decoded, "kind", None) != "damage_hit":
        return decoded
    template = host.monster_entities.get(decoded.source_actor_id)
    at = getattr(decoded, "record_offset", 0)
    wire = packet.payload
    if (template is None or template.template_id not in _TEMPLATES
            or at < 14 or at + 118 > len(wire)
            or packet.inferred_direction() != "server_to_client"):
        return decoded
    candidates = []
    for phase in range(13):
        body = decrypt(wire, phase)
        marker = body[at:at + 3]
        if marker not in (b"\x08\x73\0", b"\x08\x75\0"):
            continue
        if at + 3 + int.from_bytes(marker[1:3], "little") > len(body):
            continue
        target = int.from_bytes(body[at-4:at], "little")
        if target == decoded.target_actor_id:
            continue
        candidate = replace(decoded, target_actor_id=target, alternate_target_actor_id=None)
        if resolve_existing_summon_hit(host, packet, candidate) is not None:
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else decoded


def resolve_existing_summon_hit(host, packet, decoded):
    entity = getattr(decoded, "source_actor_id", None)
    template = host.monster_entities.get(entity)
    ledger = getattr(host, "entity_owner_evidence", None)
    if (ledger is None or template is None or template.boss
            or template.template_id not in _TEMPLATES):
        return None
    # Independent PvE evidence, not the successful summon/skill match itself.
    # Unknown targets/PvP remain unchanged until their mode is proved.
    target = host.monster_entities.get(getattr(decoded, "target_actor_id", None))
    if target is None or not target.boss:
        return None
    binding = ledger.resolve(entity, template.template_id, packet.timestamp_seconds)
    if binding is None or binding.record.owner_id in host.monster_entities:
        return None
    order = (getattr(host, "_damage_object_packet_serial", 0), 0,
             getattr(decoded, "record_offset", 0))
    scope = str(id(ledger))  # Both objects are built from this current ledger only.
    wire_hash = sha256(packet.payload).hexdigest()
    physical_id = f"{wire_hash}:{order[2]}"
    hit = PhysicalHit(scope, physical_id, order, entity, binding.generation,
        template.template_id, decoded.target_actor_id, decoded.damage,
        getattr(decoded, "damage_object_id", 0) or 0,
        getattr(decoded, "damage_result_group_id", 0) or 0,
        packet.payload, packet.inferred_direction())
    life = EntityLife(scope, entity, binding.generation, template.template_id,
        binding.record.owner_id, binding.birth_order, binding.owner_order,
        binding.birth_fingerprint, binding.record.message_fingerprint)
    evidence = annotate_hit(hit, life, SUMMON_SELF_ATTACK_SPECS,
                            encounter_mode="pve").evidence
    if evidence is None:
        return None
    # A clone's copied attack is not another cast of its owner's base skill.
    group = 6319 if 33607 <= template.template_id <= 33614 else evidence.player_skill_id
    if host._action_conflicts_with_confirmed_job(evidence.owner_id, group):
        return None
    token = ":".join((physical_id, str(binding.generation),
        binding.birth_fingerprint, binding.record.message_fingerprint,
        evidence.static_proof_id))
    metadata = dict(wire_entity_id=entity, entity_template_id=template.template_id,
        entity_owner_evidence_id=entity_owner_evidence_id(binding),
        entity_monster_skill_id=evidence.monster_skill_id,
        entity_attack_skill_id=evidence.player_skill_id,
        entity_attack_evidence_id=sha256(token.encode("ascii")).hexdigest())
    return evidence.owner_id, group, metadata
