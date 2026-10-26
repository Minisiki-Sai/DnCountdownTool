"""Client-native per-result skill keys for v576 damage attribution.

The in-game damage meter first uses the skill key stored on the concrete
damage result and, when that field is absent, its bounded fallback key.  This
module reproduces only that small read contract.  It never scans neighbouring
records, carries a key to a later hit, or emits another damage row.

Some summoned attacks use an internal MonsterSkill/attack RowId rather than a
player-facing SkillTable RowId.  Only mappings backed by a unique static table
edge are admitted here.  Ambiguous same-profession mappings deliberately stay
unresolved until the entity/projectile context disambiguates them.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256

from .damage_attribution import should_replace_attribution
from .damage_wire_evidence import has_inferred_status_object_hit
from .models import CombatEvent
from .udp_damage_records import UdpDamageRecord, inspect_udp_damage_records


# Unique foreign-key or unique composite paths from the audited client tables.
# Evidence: outputs/static_internal_attack_parent_v55_20260909 and the
# expanded exact AI/ACT catalog
# outputs/v97_summon_attack_catalog_expanded_20260910.json. Entries whose
# table relation was same-profession/cross-profession ambiguous are excluded.
STATIC_INTERNAL_ATTACK_PARENT: Mapping[int, int] = {
    4701: 4602,
    4702: 4601,
    4706: 4703,
    4707: 4704,
    4708: 4705,
    4709: 4710,
    4771: 4761,
    4772: 4762,
    4773: 4763,
    5430: 5420,
    6800: 6207,
    6801: 6211,
    6802: 6502,
    6806: 6217,
    10400: 10115,
    30611: 3403,
    30612: 3409,
    30613: 3422,
    30614: 3423,
    30615: 3412,
    30616: 3409,
    30617: 3403,
    30618: 3422,
    30619: 3423,
    30706: 2410,
    30801: 1303,
    30802: 1303,
    30805: 4207,
    30806: 4208,
    30807: 4209,
    30808: 4351,
    30809: 4216,
    30810: 4405,
    30811: 4405,
    30812: 4405,
    30820: 5216,
    30821: 5014,
    30823: 5216,
    30824: 5216,
    261731: 4220,
    261732: 4815,
    # Azuna's server-side entity attacks are SkillTable internal rows.  They
    # all belong to the persistent entity created by PactOfAzna (6318), not
    # to whatever player cast happened to be nearest in time.  Keeping these
    # narrow, observed rows here also lets a contradictory entity-owner proof
    # invalidate a stale timing label without moving the damage to a different
    # player.
    261801: 6318,
    262101: 6318,
    262301: 6318,
    262302: 6318,
    **{internal_skill: 4404 for internal_skill in range(260801, 260818)},
    291172: 3412,
    291373: 4351,
    291400: 4218,
    292224: 3423,
    400601: 5118,
}


@dataclass(frozen=True, slots=True)
class NativeDamageSkillKey:
    family: str
    record_offset: int
    source_kind: int
    common_cc: int
    field: str
    raw_key: int
    skill_id: int
    wire_record_sha256: str


def _u32(record: UdpDamageRecord, marker_relative_offset: int) -> int:
    # record_bytes begins four bytes before the type-08 marker.
    start = 4 + marker_relative_offset
    if start < 0 or start + 4 > len(record.record_bytes):
        return 0
    return int.from_bytes(record.record_bytes[start : start + 4], "little")


def _selected_native_key(record: UdpDamageRecord) -> tuple[str, int, int, int] | None:
    raw = record.record_bytes
    # common15 follows the UID width: direct37 raw100, direct41 raw104.
    common_cc_offset = 104 if record.family == "direct41" else 100
    if len(raw) <= common_cc_offset:
        return None
    source_kind = int.from_bytes(raw[11:12], "little", signed=True)
    common_cc = int(raw[common_cc_offset])
    primary_offset = 102 if record.family == "direct37" else 106
    fallback_offset = 114 if record.family == "direct37" else 118
    primary = _u32(record, primary_offset)
    fallback = _u32(record, fallback_offset)

    # Current client RVA 0x6BECB1 uses TEST ESI,ESI / JG, a signed comparison.
    # 0xFFFFFFFF is -1 (missing result identity), never a positive skill key.
    # This is the branch used by the client result object.  A positive unknown
    # primary key is still authoritative for this row and must block fallback;
    # otherwise a convenient fallback could silently relabel a different hit.
    if source_kind == 1 and common_cc == 0:
        if 0 < primary <= 0x7FFFFFFF:
            return "primary90", primary, source_kind, common_cc
        if primary > 0x7FFFFFFF:
            # The client's negative-key route consults object/local Skill
            # state before its conditional A4 branch. Those predicates are
            # not established by this physical row; a plausible A4 integer
            # alone must not bypass that missing state. Exact object/entity
            # routes can still resolve the row independently.
            return None
    if fallback > 0:
        return "fallbackA4", fallback, source_kind, common_cc
    return None


def _physical_record(
    payload: bytes,
    direction: str | None,
    *,
    family: str,
    record_offset: int,
    source_actor_id: int,
    damage: int,
    critical: bool,
) -> UdpDamageRecord | None:
    inventory = _cached_inventory(bytes(payload), direction)
    if not inventory.complete or family not in {"direct37", "direct41"}:
        return None
    matches = [
        row
        for row in inventory.damage_records
        if row.admissible_fields
        and row.family == family
        and row.record_offset == record_offset
        and row.source_actor_id == source_actor_id
        and row.damage == damage
        and row.critical == bool(critical)
    ]
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=256)
def _cached_inventory(payload: bytes, direction: str | None):
    """Share one strict physical walk across bundled hits in the same packet."""
    return inspect_udp_damage_records(payload, direction)


def resolve_native_damage_skill_key(
    payload: bytes,
    direction: str | None,
    *,
    wire_layout: str,
    record_offset: int,
    source_actor_id: int,
    damage: int,
    critical: bool,
    skill_catalog: Collection[int],
    damage_skill_ids: Collection[int],
    live_aliases: Mapping[int, int] | None = None,
    external_damage_skill_ids: Collection[int] = (),
) -> NativeDamageSkillKey | None:
    """Resolve one exact physical hit to the key used by the client meter."""
    family = (
        "direct41"
        if "direct41" in str(wire_layout)
        else "direct37"
        if "direct37" in str(wire_layout)
        else ""
    )
    if not family or record_offset <= 0 or source_actor_id <= 0 or damage <= 0:
        return None
    record = _physical_record(
        payload,
        direction,
        family=family,
        record_offset=record_offset,
        source_actor_id=source_actor_id,
        damage=damage,
        critical=critical,
    )
    if record is None:
        return None
    selected = _selected_native_key(record)
    if selected is None:
        return None
    field, raw_key, source_kind, common_cc = selected
    mapped = STATIC_INTERNAL_ATTACK_PARENT.get(raw_key, raw_key)
    skill_id = int((live_aliases or {}).get(mapped, mapped))
    external = skill_id in external_damage_skill_ids
    # The client meter selects a positive primary result key before consulting
    # any object/actor fallback. Whether our *simulator* has a damage panel for
    # that learned skill is unrelated to this native result identity. Buffs,
    # transformations and attacks omitted from the simulator's panels can
    # still own a physical Hit (e.g. Avatar, Silver Hunter, Freezco). Keep the
    # panel gate for the weaker A4 route; the primary route still needs the
    # exact admitted record, recognized key and caller's job/owner proof.
    native_primary = field == "primary90"
    if skill_id <= 0 or (
        not external
        and (skill_id not in skill_catalog
             or (skill_id not in damage_skill_ids and not native_primary))
    ):
        return None
    return NativeDamageSkillKey(
        family=family,
        record_offset=record.record_offset,
        source_kind=source_kind,
        common_cc=common_cc,
        field=field,
        raw_key=raw_key,
        skill_id=skill_id,
        wire_record_sha256=record.wire_record_sha256,
    )


_FOREIGN_INTERNAL_ATTACK_PARENT: Mapping[int, int] = {
    261801: 6318,
    262101: 6318,
    262301: 6318,
    262302: 6318,
}
_SAINT_RELIC_BUCKETS = frozenset({3409, 3423})


def clear_foreign_internal_attack_bucket(
    event: CombatEvent,
    payload: bytes,
    direction: str | None,
) -> CombatEvent:
    """Remove a disproven Saint label using this hit's exact native m+102.

    Some accelerated/reconnect captures omit the entity birth while legacy
    recent-summon recovery still supplies a Saint relic name.  The physical
    direct37 row itself can nevertheless carry an internal Azuna attack key.
    That key proves the Saint skill name false, but without a current owner
    generation it does not authorize moving the damage to another player.
    """
    try:
        old_skill_id = int(event.skill_id)
    except (TypeError, ValueError):
        return event
    if (
        old_skill_id not in _SAINT_RELIC_BUCKETS
        or event.healing
        or event.amount <= 0
        or event.has_real_hit_evidence is not True
        or "direct37" not in str(event.wire_damage_layout)
        or event.wire_record_offset <= 0
        or event.wire_source_actor_id <= 0
    ):
        return event
    record = _physical_record(
        bytes(payload),
        direction,
        family="direct37",
        record_offset=event.wire_record_offset,
        source_actor_id=event.wire_source_actor_id,
        damage=event.amount,
        critical=event.critical,
    )
    if record is None:
        return event
    raw_key = _u32(record, 102)
    parent_skill_id = _FOREIGN_INTERNAL_ATTACK_PARENT.get(raw_key)
    if parent_skill_id is None:
        return event
    token = ":".join(
        (
            "foreign-internal-attack-v79",
            record.wire_record_sha256,
            str(event.wire_record_offset),
            str(raw_key),
            str(parent_skill_id),
            str(old_skill_id),
        )
    )
    return replace(
        event,
        skill_id="-1",
        skill_name="待识别伤害",
        attribution_method="unresolved_foreign_internal_attack_conflict",
        cast_instance_id="",
        attribution_confidence=0.0,
        entity_monster_skill_id=raw_key,
        entity_attack_skill_id=parent_skill_id,
        entity_attack_evidence_id=sha256(token.encode("ascii")).hexdigest(),
    )


def annotate_native_entity_attack(
    event: CombatEvent,
    context: object,
    owner_proof: object,
    *,
    pve_confirmed: bool,
    skill_catalog: Mapping[int, tuple],
    damage_skill_ids: Collection[int],
    live_aliases: Mapping[int, int],
    job_conflicts,
) -> CombatEvent:
    """Apply a native result key after exact current entity-owner proof."""
    inferred_physical_hit = bool(
        owner_proof is not None and pve_confirmed is True
        and has_inferred_status_object_hit(event, context.wire, context.direction)
    )
    if (
        owner_proof is None
        or pve_confirmed is not True
        or event.healing
        or event.amount <= 0
        or (event.has_real_hit_evidence is not True and not inferred_physical_hit)
        or not should_replace_attribution(
            event.attribution_method, "packet_entity_native_attack_key",
            physical_hit_proven=inferred_physical_hit,
        )
    ):
        return event
    if (
        event.source_id != f"0x{owner_proof.owner_id:08X}"
        or event.wire_source_actor_id != owner_proof.entity_id
        or event.wire_entity_id != owner_proof.entity_id
        or event.entity_template_id != owner_proof.template_id
        or event.entity_owner_evidence_id != owner_proof.owner_evidence_id
        or context.generation != owner_proof.generation
        or context.order[2] != owner_proof.record_offset
        or event.wire_record_offset != owner_proof.record_offset
        or sha256(context.wire).hexdigest() != owner_proof.hit_wire_sha256
    ):
        return event
    evidence = resolve_native_damage_skill_key(
        context.wire,
        context.direction,
        wire_layout=event.wire_damage_layout,
        record_offset=event.wire_record_offset,
        source_actor_id=event.wire_source_actor_id,
        damage=event.amount,
        critical=event.critical,
        skill_catalog=skill_catalog,
        damage_skill_ids=damage_skill_ids,
        live_aliases=live_aliases,
    )
    if evidence is None:
        return event
    if job_conflicts(owner_proof.owner_id, evidence.skill_id):
        # The current entity generation and the native attack row are exact,
        # but the captured owner points at a profession that cannot own the
        # resolved skill.  This proves the old display/timing skill is wrong;
        # it does *not* prove which missing owner should receive the damage.
        # Preserve the physical hit and current source owner, clear only the
        # false skill bucket, and retain the contradictory native key for a
        # later reliable owner/reconnect repair.
        token = ":".join(
            (
                "native-result-owner-conflict-v79",
                owner_proof.hit_wire_sha256,
                str(event.wire_record_offset),
                str(owner_proof.generation),
                owner_proof.birth_fingerprint,
                owner_proof.owner_fingerprint,
                evidence.field,
                str(evidence.raw_key),
                str(evidence.skill_id),
            )
        )
        return replace(
            event,
            skill_id="-1",
            skill_name="待识别伤害",
            attribution_method="unresolved_entity_native_attack_owner_conflict",
            cast_instance_id="",
            entity_monster_skill_id=evidence.raw_key,
            entity_attack_skill_id=evidence.skill_id,
            entity_attack_evidence_id=sha256(token.encode("ascii")).hexdigest(),
        )
    info = skill_catalog.get(evidence.skill_id)
    if not info:
        return event
    token = ":".join(
        (
            "native-result-key-v57",
            owner_proof.hit_wire_sha256,
            str(event.wire_record_offset),
            str(owner_proof.generation),
            owner_proof.birth_fingerprint,
            owner_proof.owner_fingerprint,
            evidence.field,
            str(evidence.raw_key),
            str(evidence.skill_id),
        )
    )
    return replace(
        event,
        skill_id=str(evidence.skill_id),
        skill_name=str(info[0]),
        attribution_method="packet_entity_native_attack_key",
        has_real_hit_evidence=True,
        cast_instance_id="",
        entity_monster_skill_id=evidence.raw_key,
        entity_attack_skill_id=evidence.skill_id,
        entity_attack_evidence_id=sha256(token.encode("ascii")).hexdigest(),
    )
