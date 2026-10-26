"""Post-admission entity ACT selector -> projectile -> same-entity result.

These are short-entity 09/29 and 09/32 records, NOT player 09 records. Offset
+34/+38 select an ACT action and signal; +3 is the object ID. No nearest cast,
owner-normalized object namespace, m114 creator hint, or generated damage.
Raw/static certificates: outputs/compact_entity_selectors_20260908/.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from hashlib import sha256
import math
from types import MappingProxyType

from .damage_attribution import AttributionLayer, attribution_layer, is_deterministic_attribution
from .entity_owner_evidence import entity_owner_evidence_id
from .summon_self_attack_evidence import PhysicalHit, read_self_attack_slot


# template, declared size, ACT ordinal, signal ordinal -> display family,
# copied/self attack (0 means not claimed), projectile, conservative expiry.
# 32005 ACT sha256 8e8cdf85...c50a action50 Attack3_Saliva/signal5 -> 5054;
# independent monster attack 30809 -> player 4352 (existing self-attack spec).
# 33608 ACT sha256 7fff5c9d...23d2 action80 Skill_PlasmaBurst_EX_Landing/2
# -> 11090. This remains the creator's 6319 summon family, NOT a player cast.
# 11090's Shoot+Hit lasts 460 frames (~7.67s). Repeated state observations do
# NOT restart this lifetime; the cap cannot supply missing object evidence.
SELECTOR_SPECS = MappingProxyType({
    (32005, 50, 50, 5): (4352, 4352, 5054, 3.0),
    (33608, 41, 80, 2): (6319, 0, 11090, 8.0),
})


@dataclass(frozen=True, slots=True)
class SelectorRecord:
    entity: int
    object_id: int
    length: int
    action: int
    signal: int
    offset: int
    phase: int
    wire_hash: str


def decode_selector_records(wire: bytes, direction: str):
    """Full top-level framing + unique XOR phase, including invalidations.

    Other short-09 sizes can invalidate an older object but never certify one.
    Positions/observer IDs are NOT used as skill or owner evidence.
    """
    from .protocol import _decode_v576_body, _v576_wire_may_contain_marker
    if (direction != 'server_to_client' or len(wire) < 62
            or int.from_bytes(wire[7:9], 'little') + 3 != len(wire)
            or not _v576_wire_may_contain_marker(wire,
                (b'\x09\x29\0', b'\x09\x32\0', b'\x09\x2d\0'))):
        return ()
    candidates = []
    for phase in range(13):
        body = _decode_v576_body(wire, phase)
        if not 0x1000000 <= int.from_bytes(body[10:14], 'little') < 0xffffffff:
            continue
        cursor, rows, count = 14, [], 0
        while cursor + 7 <= len(body) and count < 128:
            m = cursor + 4
            entity = int.from_bytes(body[cursor:m], 'little')
            length = int.from_bytes(body[m+1:m+3], 'little')
            end = m + 3 + length
            if end > len(body) or not 0 < entity < 0xffffffff:
                break
            if body[m] == 9 and entity < 65536 and length >= 4:
                obj = int.from_bytes(body[m+3:m+7], 'little')
                if 0 < obj < 0xffffffff:
                    supported = length in (41, 50) and body[m+42] == 0
                    action = int.from_bytes(body[m+34:m+38], 'little') if supported else -1
                    signal = int.from_bytes(body[m+38:m+42], 'little') if supported else -1
                    rows.append(SelectorRecord(entity, obj, length, action, signal, m, phase, ''))
            cursor, count = end, count + 1
        if cursor == len(body):
            candidates.append(rows)
    if len(candidates) != 1:
        return ()
    digest = sha256(wire).hexdigest()
    return tuple(replace(row, wire_hash=digest) for row in candidates[0])


@dataclass(frozen=True, slots=True)
class ObjectEvidence:
    record: SelectorRecord
    template: int
    generation: int
    owner: int
    birth_hash: str
    owner_hash: str
    owner_evidence_id: str
    timestamp: float
    order: tuple[int, int, int]
    spec: tuple | None


class CompactEntityObjectLedger:
    """Bounded, scene/life-scoped evidence; never emits hits or player casts."""
    def __init__(self, capacity=4096):
        self.capacity = max(1, capacity)
        self.objects = OrderedDict()

    def reset(self):
        self.objects.clear()

    def observe_packet(self, wire, direction, timestamp, serial, owners):
        if not math.isfinite(timestamp):
            return
        for r in decode_selector_records(wire, direction):
            birth = owners.births.get(r.entity)
            if birth is None:
                continue
            binding = owners.resolve(r.entity, birth[0], timestamp)
            order = (serial, 0, r.offset)
            if (binding is None or not binding.birth_order < binding.owner_order <= order
                    or binding.generation != birth[1] or owners.owner_revoked(r.entity)):
                continue
            spec = SELECTOR_SPECS.get((birth[0], r.length, r.action, r.signal))
            key = (r.entity, birth[1], r.object_id)
            previous = self.objects.get(key)
            if previous is None and spec is None:
                continue  # no positive state to revoke
            evidence_id = entity_owner_evidence_id(binding)
            if previous is not None:
                if order <= previous.order or timestamp < previous.timestamp:
                    continue
                # Position/state retransmission is NOT a new creation. Never
                # extend expiry, even when encoded coordinates have changed.
                if (previous.spec == spec and previous.owner == binding.record.owner_id
                        and previous.birth_hash == binding.birth_fingerprint
                        and previous.owner_hash == binding.record.message_fingerprint):
                    continue
                # An unknown/new selector or owner invalidates this namespace;
                # do not guess a new object generation from packet timing.
                spec = None
            self.objects[key] = ObjectEvidence(r, birth[0], birth[1], binding.record.owner_id,
                binding.birth_fingerprint, binding.record.message_fingerprint, evidence_id,
                timestamp, order, spec)
            self.objects.move_to_end(key)
            while len(self.objects) > self.capacity:
                self.objects.popitem(last=False)

    def annotate(self, event, context, owner_proof, *, pve_confirmed, skill_catalog, job_conflicts):
        if (owner_proof is None or pve_confirmed is not True or event.healing
                or event.amount <= 0 or event.has_real_hit_evidence is not True
                or not str(event.skill_id).isdigit() or int(event.skill_id) <= 0
                or event.attribution_method == 'direct_hit_overflow'
                or is_deterministic_attribution(event.attribution_method)
                or attribution_layer(event.attribution_method) is AttributionLayer.STATUS):
            return event
        r = self.objects.get((owner_proof.entity_id, owner_proof.generation, event.wire_damage_object_id))
        if r is None or r.spec is None:
            return event
        group, attack, projectile, lifetime = r.spec
        if (r.order > context.order or not 0 <= context.timestamp-r.timestamp <= lifetime
                or r.owner != owner_proof.owner_id or r.template != owner_proof.template_id
                or r.birth_hash != owner_proof.birth_fingerprint
                or r.owner_hash != owner_proof.owner_fingerprint
                or r.owner_evidence_id != owner_proof.owner_evidence_id
                or context.generation != owner_proof.generation
                or context.order[2] != owner_proof.record_offset
                or event.wire_record_offset != owner_proof.record_offset
                or sha256(context.wire).hexdigest() != owner_proof.hit_wire_sha256
                or event.source_id != f'0x{owner_proof.owner_id:08X}'
                or event.wire_source_actor_id != owner_proof.entity_id
                or event.wire_entity_id != owner_proof.entity_id
                or event.entity_template_id != owner_proof.template_id
                or event.entity_owner_evidence_id != owner_proof.owner_evidence_id
                or group not in skill_catalog or job_conflicts(r.owner, group)):
            return event
        physical = PhysicalHit('compact-entity-current-proof', owner_proof.hit_wire_sha256,
            context.order, r.record.entity, r.generation, r.template,
            event.wire_target_actor_id, event.amount, event.wire_damage_object_id,
            event.wire_result_group_id, context.wire, context.direction)
        slot = read_self_attack_slot(physical)
        # Exact known/unknown positive self-skill slots are not overwritten.
        # A future bridge may explicitly cross-check them, but never assume.
        if slot is None or slot.monster_skill_id != 0xffffffff:
            return event
        token = ':'.join((r.record.wire_hash, str(r.record.offset), str(projectile),
            str(r.template), str(r.generation), r.birth_hash, r.owner_hash,
            owner_proof.hit_wire_sha256, str(context.order[2])))
        return replace(event, skill_id=str(group), skill_name=skill_catalog[group][0],
            attribution_method='packet_entity_projectile_selector', cast_instance_id='',
            entity_monster_skill_id=slot.monster_skill_id, entity_attack_skill_id=attack,
            entity_attack_evidence_id=sha256(token.encode('ascii')).hexdigest())
