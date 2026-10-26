"""Unintegrated, read-only summon self-attack evidence candidate.

The caller supplies ONE existing physical hit and a current, independently
validated birth/34-2c owner lifetime. This module returns an annotation, never
creates/merges hits, changes damage, or replaces the physical source entity.
No owner inference, nearest action, creation-skill fallback, or object cache.
The injected catalog is produced/audited separately from local AI/ACT/DNT data.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping

KEY = bytes.fromhex("c8bfcda4943f2642fd8a723474")
Order = tuple[int, int, int]


def decrypt(wire: bytes, phase: int) -> bytes:
    return wire[:9] + bytes(v ^ KEY[(i - phase) % 13]
                            for i, v in enumerate(wire[9:], 9))


def _u32(body: bytes, at: int) -> int:
    return int.from_bytes(body[at:at + 4], "little")


@dataclass(frozen=True, slots=True)
class EntityLife:
    """Trust boundary: caller's guarded birth/owner ledger, NOT a pending claim.

    Fingerprints identify raw full messages; this module does not itself verify
    their ownership semantics. Supply the CURRENT life and invalidation state.
    Old snapshots must not be reused after a conflict, rebirth, or zone reset.
    """
    capture: str
    entity_id: int
    generation: int
    template_id: int
    owner_id: int
    birth_order: Order
    owner_order: Order
    birth_fingerprint: str
    owner_fingerprint: str
    retired_order: Order | None = None
    conflicted: bool = False


@dataclass(frozen=True, slots=True)
class PhysicalHit:
    capture: str
    physical_hit_id: str
    order: Order
    entity_id: int
    generation: int
    template_id: int
    target_id: int
    damage: int
    object_id: int
    result_group: int
    wire: bytes
    direction: str = "server_to_client"


@dataclass(frozen=True, slots=True)
class AttackSpec:
    template_id: int
    monster_skill_id: int
    player_skill_id: int
    proof_id: str
    actions: tuple[str, ...]
    projectile_ids: tuple[int, ...] = ()
    encounter_mode: str = "pve"


@dataclass(frozen=True, slots=True)
class SelfAttackSlot:
    monster_skill_id: int
    context_skill_id: int
    xor_phase: int
    record_offset: int
    record_end: int
    wire_sha256: str


@dataclass(frozen=True, slots=True)
class CompactActionObservation:
    """Diagnostic ONLY: ACT index alignment is not an action->hit join."""
    entity_id: int
    action_code: int
    variant: int
    kind: int
    act_index_hypothesis: str | None
    xor_phase: int
    marker_offset: int
    record_end: int
    raw_record: bytes
    can_attribute_damage: bool = False


def read_compact_action(wire: bytes, marker_offset: int, entity_id: int,
                        act_action_names: tuple[str, ...], *,
                        direction: str = "unknown") -> CompactActionObservation | None:
    """Strict bounded 03/14 observation at an already selected marker.

    Full declared 20-byte payload, both reserved zeros, known short entity,
    and unique phase are mandatory (no signature cache bypass). Names remain
    hypotheses; especially 255 is a sentinel, not a Python negative index.
    No attribution can be produced from these observations alone.
    """
    at, end = marker_offset, marker_offset + 23
    if (direction != 'server_to_client' or not 0 < entity_id < 65536
            or at < 14 or end > len(wire)):
        return None
    matches = []
    for phase in range(13):
        b = decrypt(wire, phase)
        if (b[at:at + 3] != bytes.fromhex('031400')
                or _u32(b, at - 4) != entity_id or b[at + 7:at + 9] != b'\0\0'):
            continue
        # If another recognized record overlaps, the length-based interpretation
        # is ambiguous; do not accept a prefix merely because six bytes fit.
        nested = any(b[n:n + 3] in (
                         b'\x03\x14\0', b'\x08\x73\0', b'\x08\x75\0', b'\x34\x2c\0')
                     and _u32(b, n - 4) == entity_id for n in range(at + 9, end - 2))
        if nested:
            continue
        code = b[at + 3]
        name = act_action_names[code] if code != 255 and code < len(act_action_names) else None
        matches.append(CompactActionObservation(entity_id, code, int.from_bytes(b[at + 4:at + 6], 'little'),
            b[at + 6], name, phase, at, end, b[at - 4:end]))
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class AttackEvidence:
    physical_hit_id: str
    entity_id: int
    generation: int
    template_id: int
    owner_id: int
    monster_skill_id: int
    player_skill_id: int
    object_id: int
    target_id: int
    damage: int
    result_group: int
    wire_sha256: str
    record_offset: int
    xor_phase: int
    skill_slot_offset: int
    context_skill_id: int
    static_proof_id: str
    birth_fingerprint: str
    owner_fingerprint: str
    method: str = "packet_entity_self_attack_m102"


@dataclass(frozen=True, slots=True)
class Decision:
    evidence: AttackEvidence | None
    reason: str


def _has_nested_record(body: bytes, marker: int, end: int) -> bool:
    # Do not borrow m+102 from another result/status record. Recognize a
    # structurally plausible next result; reject ambiguity conservatively.
    for at in range(marker + 36, min(end, len(body) - 3)):
        if at < 4 or not 0 < _u32(body, at - 4) < 0xffffffff:
            continue
        if body[at:at + 3] in (
            b"\x08\x73\0", b"\x08\x75\0", b"\x08\x77\0", b"\x08\x79\0"
        ):
            shift = 0 if body[at + 1] in (0x73, 0x75) else 4
            if at + 27 + shift <= len(body):
                source = _u32(body, at + 8 + shift)
                damage = int.from_bytes(body[at + 19 + shift:at + 27 + shift], "little")
                if 0 < source < 0xffffffff and 0 < damage < 10**13:
                    return True
        if (body[at] == 0x13 and 0x47 <= body[at + 1] <= 0x4e
                and body[at + 2] == 0 and at + 16 <= len(body)
                and 0 < _u32(body, at + 12) < 0xffffffff):
            return True
    return False


def read_self_attack_slot(hit: PhysicalHit) -> SelfAttackSlot | None:
    """Decode only the supplied hit's typed direct37 record, never search hits.

    m+102 is the self monster attack slot. m+114 is retained only as context;
    it often contains the creation skill, and must never substitute for m+102.
    A complete 118/120-byte marker/result and unique XOR phase are required.
    Other damage layouts are deliberately unsupported.
    """
    if (hit.direction != "server_to_client" or not 0 < hit.entity_id < 65536
            or hit.target_id in (0, hit.entity_id, 0xffffffff)
            or not 0 < hit.damage < 10**13 or hit.order[2] < 14):
        return None
    at = hit.order[2]
    if int.from_bytes(hit.wire[7:9], "little") + 3 != len(hit.wire):
        return None
    matches = []
    for phase in range(13):
        body = decrypt(hit.wire, phase)
        marker = body[at:at + 3]
        if marker not in (b"\x08\x73\0", b"\x08\x75\0"):
            continue
        end = at + 3 + int.from_bytes(marker[1:3], "little")
        if end > len(body):
            continue
        # Prove the selected marker is a top-level length-framed record, not
        # bytes inside another record. Also reject malformed companion tails.
        cursor, selected = 14, False
        while cursor + 7 <= len(body):
            marker = cursor + 4
            record_end = marker + 3 + int.from_bytes(body[marker+1:marker+3], "little")
            if record_end > len(body):
                break
            if marker == at and record_end == end:
                selected = True
            cursor = record_end
        if not selected or cursor != len(body):
            continue
        if (_u32(body, at - 4) != hit.target_id
                or _u32(body, at + 8) != hit.entity_id
                or _u32(body, at + 13) != hit.object_id
                or _u32(body, at + 4) != hit.result_group
                or int.from_bytes(body[at + 19:at + 27], "little") != hit.damage
                or body[at + 12] != 0 or body[at + 106:at + 110] != bytes(4)
                or _has_nested_record(body, at, end)):
            continue
        matches.append(SelfAttackSlot(_u32(body, at + 102), _u32(body, at + 114),
                                      phase, at, end, hashlib.sha256(hit.wire).hexdigest()))
    return matches[0] if len(matches) == 1 else None


def annotate_hit(hit: PhysicalHit, life: EntityLife | None,
                 specs: Mapping[tuple[int, int], AttackSpec], *,
                 encounter_mode: str = "unknown") -> Decision:
    if life is None:
        return Decision(None, "missing_observed_birth_owner")
    if (not hit.physical_hit_id or life.capture != hit.capture
            or life.entity_id != hit.entity_id or life.generation != hit.generation
            or life.template_id != hit.template_id or life.generation <= 0):
        return Decision(None, "lifetime_mismatch")
    if (life.conflicted or not 0x1000000 <= life.owner_id < 0xffffffff
            or any(len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
                   for h in (life.birth_fingerprint, life.owner_fingerprint))
            or not life.birth_order <= life.owner_order <= hit.order
            or (life.retired_order is not None and hit.order >= life.retired_order)):
        return Decision(None, "invalid_or_stale_owner_proof")
    slot = read_self_attack_slot(hit)
    if slot is None:
        return Decision(None, "unsupported_or_mismatched_physical_record")
    if slot.monster_skill_id in (0, 0xffffffff):
        return Decision(None, "missing_self_attack_slot_no_creation_fallback")
    spec = specs.get((hit.template_id, slot.monster_skill_id))
    if (spec is None or spec.template_id != hit.template_id
            or spec.monster_skill_id != slot.monster_skill_id
            or spec.player_skill_id <= 0 or not spec.proof_id or not spec.actions):
        return Decision(None, "no_exact_template_monster_skill_static_proof")
    if encounter_mode != spec.encounter_mode:
        return Decision(None, "unvalidated_encounter_mode")
    return Decision(AttackEvidence(
        hit.physical_hit_id, hit.entity_id, hit.generation, hit.template_id,
        life.owner_id, slot.monster_skill_id, spec.player_skill_id,
        hit.object_id, hit.target_id, hit.damage, hit.result_group,
        slot.wire_sha256, slot.record_offset, slot.xor_phase,
        slot.record_offset + 102, slot.context_skill_id, spec.proof_id,
        life.birth_fingerprint, life.owner_fingerprint), "exact_self_attack")


def specs_from_catalog(catalog: Mapping) -> dict[tuple[int, int], AttackSpec]:
    """Fail on conflicting catalog entries; do not last-write-win a family."""
    if catalog.get("schema") != "summon-self-attack-v26.1":
        raise ValueError("Unsupported summon attack catalog schema")
    result = {}
    for row in catalog["accepted"]:
        spec = AttackSpec(row["template"], row["monster_skill"], row["player_skill"],
                          row["proof_id"], tuple(row["actions"]),
                          tuple(row["projectiles"]), row["encounter_mode"])
        key = (spec.template_id, spec.monster_skill_id)
        if key in result and result[key] != spec:
            raise ValueError(f"Conflicting summon attack spec {key}")
        result[key] = spec
    return result
