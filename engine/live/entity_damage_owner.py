"""Read-only owner annotation of ONE already emitted outgoing damage event.

No admission, target repair, skill proof, cast inference, cache or ledger writes.
Unknown/FFFF attacks are supported because attack IDs are not ownership evidence.
Only the audited complete typed direct37 physical-result family is supported here.

Trust boundary: supply the CURRENT ledger and the hit's generation/order captured
at its original record boundary, never the latest generation of an older queued
hit. The caller must invalidate unknown-template births and contradictory owner
batches, and deduplicate old wire replays upstream. This helper cannot recover
invalidation records already discarded by the caller's parser. Call after the
existing outgoing admission decision, before the original final history write.

``summon_template_ids`` means independently identified summon entity kinds, NOT
single-skill templates; it carries no mapping to a skill. Known ordinary monsters
are excluded. Confirmed nonplayer targets need not be Bosses. Missing player
names/jobs do not invalidate an otherwise complete explicit owner proof.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
import math

from .entity_owner_evidence import (
    EntityOwnerBinding,
    EntityOwnerEvidenceLedger,
    entity_owner_evidence_id,
)
from .models import CombatEvent
from .summon_self_attack_evidence import PhysicalHit, decrypt, read_self_attack_slot, _has_nested_record
from .udp_damage_records import inspect_udp_damage_records

Order = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class EntityOwnerHitContext:
    """Original physical record context; never synthesized from a later cache."""

    wire: bytes
    direction: str
    timestamp: float
    order: Order
    generation: int


@dataclass(frozen=True, slots=True)
class EntityDamageOwnerEvidence:
    entity_id: int
    template_id: int
    generation: int
    owner_id: int
    hit_wire_sha256: str
    record_offset: int
    birth_fingerprint: str
    owner_fingerprint: str
    owner_evidence_id: str
    # This annotation MUST NOT upgrade the event's skill attribution method.
    skill_proven: bool = False


@dataclass(frozen=True, slots=True)
class EntityDamageOwnerDecision:
    event: CombatEvent
    evidence: EntityDamageOwnerEvidence | None
    reason: str


def _is_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _actor_id(value: str) -> int | None:
    try:
        # Only the established source/monster display representation is accepted.
        text = value.split('@', 1)[0]
        return int(text, 16) if text.startswith('0x') else None
    except (AttributeError, ValueError):
        return None


def _valid_order(value: Order, *, marker: bool = False) -> bool:
    return (isinstance(value, tuple) and len(value) == 3
            and all(isinstance(n, int) and not isinstance(n, bool) for n in value)
            and value[0] > 0 and value[1] >= 0 and value[2] >= (18 if marker else 0))


def resolve_current_entity_owner(
    ledger: EntityOwnerEvidenceLedger,
    entity_id: int,
    template_id: int,
    timestamp: float,
    *,
    order: Order,
    generation: int,
    entity_templates: Mapping[int, object],
    known_player_actor_ids: Collection[int] = (),
) -> EntityOwnerBinding | None:
    """Skill/target-independent getter; no cache, job or attack-catalog lookup.

    Caller separately classifies the source as a summon and target as nonplayer.
    This getter returns ownership only, never permission to emit damage.
    """
    current = entity_templates.get(entity_id)
    if (not 0 < entity_id < 65536 or entity_id in known_player_actor_ids
            or getattr(current, 'template_id', current) != template_id
            or getattr(current, 'boss', False) or not math.isfinite(timestamp)
            or not _valid_order(order, marker=True) or generation <= 0):
        return None
    binding = ledger.resolve(entity_id, template_id, timestamp)
    if binding is None or ledger.owner_revoked(entity_id):
        return None
    r = binding.record
    if (binding.entity_id != entity_id or binding.generation != generation
            or ledger.births.get(entity_id) != (template_id, generation, binding.born_at)
            or ledger.birth_evidence.get(entity_id) != (binding.birth_order, binding.birth_fingerprint)
            or r.entity_id != entity_id or not 0x1000000 <= r.owner_id < 0xffffffff
            or r.owner_id in entity_templates
            or not _is_hash(binding.birth_fingerprint) or not _is_hash(r.message_fingerprint)
            or not _valid_order(binding.birth_order) or not _valid_order(binding.owner_order, marker=True)
            or r.record_offset != binding.owner_order[2]
            or not binding.birth_order < binding.owner_order <= order):
        return None
    try:
        raw = bytes.fromhex(r.fingerprint)
    except (TypeError, ValueError):
        return None
    if (len(raw) != 51 or raw[4:7] != b'\x34\x2c\0' or raw[11] != 1
            or int.from_bytes(raw[:4], 'little') != entity_id
            or int.from_bytes(raw[7:11], 'little') != r.owner_id
            or int.from_bytes(raw[47:51], 'little') != r.owner_id):
        return None
    return binding


def annotate_existing_entity_damage(
    event: CombatEvent,
    context: EntityOwnerHitContext,
    *,
    admitted_outgoing: bool,
    ledger: EntityOwnerEvidenceLedger,
    entity_templates: Mapping[int, object],
    summon_template_ids: Collection[int],
    confirmed_nonplayer_target_ids: Collection[int],
    known_player_actor_ids: Collection[int],
    owner_display_context: Mapping[int, tuple[str, str]] | None = None,
) -> EntityDamageOwnerDecision:
    """Return a same-record copy with owner identity/evidence only, or unchanged.

    Owner display context is an optional read-only mapping. If the owner changes
    and no name/job is known, clear the previous owner's labels instead of keeping
    false identity information. No name, job or skill observation is performed.
    Rejection returns the exact original event object. False/None real-hit flags
    are preserved, not used as an admission gate and never promoted to True.
    """
    def reject(reason: str) -> EntityDamageOwnerDecision:
        return EntityDamageOwnerDecision(event, None, reason)

    if (not isinstance(event, CombatEvent) or admitted_outgoing is not True
            or event.healing or event.amount <= 0 or event.attribution_method == 'incoming_damage'
            or _actor_id(event.source_id) is None):
        return reject('not_existing_outgoing_damage')
    if (context.direction != 'server_to_client' or not math.isfinite(context.timestamp)
            or not _valid_order(context.order, marker=True) or context.generation <= 0):
        return reject('invalid_original_record_context')
    entity, target = event.wire_source_actor_id, event.wire_target_actor_id
    if (not 0 < entity < 0x10000 or entity in known_player_actor_ids
            or event.wire_record_offset != context.order[2]
            or event.wire_entity_id not in (0, entity)):
        return reject('mismatched_wire_entity')
    source_template = entity_templates.get(entity)
    template = getattr(source_template, 'template_id', source_template)
    if (template is None or template not in summon_template_ids
            or getattr(source_template, 'boss', False)
            or event.entity_template_id not in (0, template)):
        return reject('not_a_current_identified_summon')
    if (target not in confirmed_nonplayer_target_ids or target in known_player_actor_ids
            or target in (0, entity, 0xffffffff) or _actor_id(event.target_id) != target):
        return reject('unconfirmed_or_player_or_remapped_target')
    if (event.wire_critical is None or event.critical != event.wire_critical):
        return reject('mismatched_physical_critical')

    binding = resolve_current_entity_owner(ledger, entity, template, context.timestamp,
        order=context.order, generation=context.generation, entity_templates=entity_templates,
        known_player_actor_ids=known_player_actor_ids)
    if binding is None:
        return reject('missing_or_invalidated_current_owner')
    birth = ledger.births.get(entity)
    birth_evidence = ledger.birth_evidence.get(entity)
    if (birth != (template, binding.generation, binding.born_at)
            or binding.entity_id != entity or binding.generation != context.generation
            or birth_evidence != (binding.birth_order, binding.birth_fingerprint)
            or entity in ledger.conflicts):
        return reject('birth_generation_mismatch')
    record = binding.record
    owner = record.owner_id
    if (record.entity_id != entity or not 0x1000000 <= owner < 0xffffffff
            or owner in entity_templates or owner == target):
        return reject('owner_is_not_a_player_identity')
    if (not _is_hash(binding.birth_fingerprint) or not _is_hash(record.message_fingerprint)
            or not _valid_order(binding.birth_order) or not _valid_order(binding.owner_order, marker=True)
            or binding.owner_order[2] != record.record_offset
            or not binding.birth_order < binding.owner_order <= context.order):
        return reject('missing_or_out_of_order_owner_proof')
    try:
        raw = bytes.fromhex(record.fingerprint)
    except (TypeError, ValueError):
        return reject('invalid_repeated_owner_record')
    if (len(raw) != 51 or raw[4:7] != b'\x34\x2c\0'
            or int.from_bytes(raw[:4], 'little') != entity or raw[11] != 1
            or int.from_bytes(raw[7:11], 'little') != owner
            or int.from_bytes(raw[47:51], 'little') != owner):
        return reject('invalid_repeated_owner_record')

    # Reuse only the exact physical reader, NEVER annotate_hit or an attack spec.
    # read_self_attack_slot returns a structural slot even when m102 is FFFFFFFF.
    wire_hash = sha256(context.wire).hexdigest()
    hit = PhysicalHit('owner-only', f'{wire_hash}:{context.order[2]}', context.order,
        entity, context.generation, template, target, event.amount,
        event.wire_damage_object_id, event.wire_result_group_id,
        context.wire, context.direction)
    slot = read_self_attack_slot(hit)
    if slot is None:
        # Ownership does not require an attack-slot interpretation. The 0075
        # result in the Sand Dragon capture carries a nonzero suffix field;
        # the old self-attack reader's reserved-zero guard rejects it. Prove
        # its existing physical Hit with the independent full-record reader,
        # without reading that suffix as a skill or changing admission.
        inventory = inspect_udp_damage_records(context.wire, context.direction)
        rows = [r for r in inventory.damage_records if r.admissible_fields
            and r.family == 'direct37' and r.declared_length == 0x75
            and r.record_offset == context.order[2]
            and r.source_actor_id == entity and r.target_actor_id == target
            and r.damage == event.amount and r.critical == event.critical
            and r.damage_object_id == event.wire_damage_object_id
            and r.damage_result_group_id == event.wire_result_group_id]
        if not inventory.complete or len(rows) != 1:
            return reject('unsupported_or_mismatched_physical_record')
        body = decrypt(context.wire, rows[0].xor_phase)
        if _has_nested_record(body, rows[0].record_offset, rows[0].record_end):
            return reject('unsupported_or_mismatched_physical_record')
    else:
        body = decrypt(context.wire, slot.xor_phase)
    if bool(body[context.order[2]+35] & 1) != event.critical:
        return reject('mismatched_physical_critical')
    # Avoid attributing to an owner record from an unrelated message/order slot.
    if (binding.owner_order[:2] == context.order[:2]
            and record.message_fingerprint != wire_hash):
        return reject('same_packet_owner_fingerprint_mismatch')
    owner_id = f'0x{owner:08X}'
    evidence_id = entity_owner_evidence_id(binding)
    if event.entity_owner_evidence_id and event.entity_owner_evidence_id != evidence_id:
        return reject('existing_owner_evidence_conflict')
    if event.source_id != owner_id and (
            event.entity_attack_evidence_id or event.cast_instance_id.startswith('damage-object:')):
        return reject('existing_skill_chain_owner_conflict')
    source_name, job = event.source_name, event.job
    if event.source_id != owner_id:
        source_name, job = (owner_display_context or {}).get(owner, (f'角色 {owner_id}', ''))
    updated = replace(event, source_id=owner_id, source_name=source_name, job=job,
                      wire_entity_id=entity, entity_template_id=template,
                      entity_owner_evidence_id=evidence_id)
    evidence = EntityDamageOwnerEvidence(entity, template, binding.generation, owner,
        wire_hash, context.order[2], binding.birth_fingerprint,
        record.message_fingerprint, evidence_id)
    return EntityDamageOwnerDecision(updated, evidence, 'exact_current_entity_owner_only')


# Initial review-name compatibility; the production integration name is above.
annotate_existing_entity_owner = annotate_existing_entity_damage
