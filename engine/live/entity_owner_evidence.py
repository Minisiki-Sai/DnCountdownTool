"""Explicit server entity-owner records, separate from proximity guesses.

The 34/44 record repeats the full owner around the summoned entity's initial
resource values. A record only establishes ownership, NEVER a skill. Skill
provenance must independently use the entity template or its attack chain.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256


# Independently audited DNT -> actual AI branches -> ACT damage signals.
# Original rows: outputs/v25_entity_owner_independent/safe_template_decisions.json.
# Expanded autonomous rows: outputs/v100_summon_attack_catalog_type48_20260910.json.
# A template is admitted here only when every damaging action belongs to one
# player-skill family.  Commandable relics are deliberately excluded: the
# same relic can execute Skill_ShockofRelic, so owner+template proves the
# player but not whether this Hit is the hand's own attack or Shock of Relic.
# Unlisted variants are deliberately not inferred from adjacent template IDs.
SINGLE_FAMILY_ENTITY_SKILLS = {
    30001: 3409, 30501: 3409,
    31001: 1303,
    32002: 4207, 32003: 4208,
    32004: 4209, 32011: 4351,
    32013: 4408,  # audited autonomous Ice Palm AI/ACT closure, two raw captures
    33500: 5014, 33502: 5216, 33503: 5216,
    33601: 6318,
    # Persistent non-damaging Buff companion. Its template has one reviewed
    # player-skill family (6316) and periodically applies internal 2619xx
    # statuses; admitting it here lets the same birth+owner proof resolve the
    # source without treating the short runtime ID as a player or Boss.
    33603: 6316,
    # Spectral Gaze has no MonsterSkillTable row, but its exact learned player
    # action constructs 33604 (ACT signal 48) and that entity's Summon_On ACT
    # is the sole direct-damage branch.  This is a static constructor/entity
    # proof, not a recent-cast guess.
    33604: 6313,
}


@dataclass(frozen=True, slots=True)
class EntityOwnerRecord:
    entity_id: int
    owner_id: int
    observer_id: int
    record_offset: int
    xor_phase: int
    fingerprint: str
    message_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class EntityOwnerBatch:
    """All valid top-level rows, including rows of contradictory entities.

    Only the batch API retains the negative evidence needed to revoke an older
    binding. Invalid framing/ambiguous phase returns None, not a conflict batch.
    """
    records: tuple[EntityOwnerRecord, ...]
    conflicted_entity_ids: frozenset[int]
    message_fingerprint: str
    xor_phase: int


def decode_entity_owner_batch(payload: bytes, direction: str | None = None) -> EntityOwnerBatch | None:
    from .protocol import _decode_v576_body, _v576_wire_may_contain_marker

    if (direction not in (None, 'server_to_client') or len(payload) < 65
            or int.from_bytes(payload[7:9], 'little') + 3 != len(payload)):
        return None
    marker = b'\x34\x2c\x00'
    if not _v576_wire_may_contain_marker(payload, (marker,), start_offset=18):
        return None
    candidates = []
    for phase in range(13):
        phase_candidates = []
        body = _decode_v576_body(payload, phase)
        observer = int.from_bytes(body[10:14], 'little')
        if not 0x1000000 <= observer < 0xffffffff:
            continue
        cursor = 14  # observer header, then actor/u8 opcode/u16 length records
        while cursor + 7 <= len(body):
            at = cursor + 4
            end = at + 3 + int.from_bytes(body[at+1:at+3], 'little')
            if end > len(body):
                break
            if body[at:at+3] != marker:
                cursor = end
                continue
            entity = int.from_bytes(body[at-4:at], 'little')
            owner = int.from_bytes(body[at+3:at+7], 'little')
            owner_repeat = int.from_bytes(body[at+43:end], 'little')
            # Range/duplicate/typed record guards, not a guessed nearby owner.
            if (0 < entity < 0x10000 and 0x1000000 <= owner < 0xffffffff
                    and owner == owner_repeat and body[at+7] == 1):
                phase_candidates.append(EntityOwnerRecord(entity, owner, observer, at, phase,
                                                     body[at-4:end].hex(), sha256(payload).hexdigest()))
            cursor = end
        if cursor == len(body):
            candidates.extend(phase_candidates)
    phases = {row.xor_phase for row in candidates}
    if len(phases) != 1:
        return None
    owners: dict[int, set[int]] = {}
    for row in candidates:
        owners.setdefault(row.entity_id, set()).add(row.owner_id)
    return EntityOwnerBatch(tuple(dict.fromkeys(candidates)),
        frozenset(entity for entity, values in owners.items() if len(values) > 1),
        sha256(payload).hexdigest(), next(iter(phases)))


def decode_entity_owner_records(payload: bytes, direction: str | None = None) -> tuple[EntityOwnerRecord, ...]:
    """Compatibility reader: unambiguous rows only; use batches to see conflicts."""
    batch = decode_entity_owner_batch(payload, direction)
    if batch is None:
        return ()
    return tuple(row for row in batch.records if row.entity_id not in batch.conflicted_entity_ids)


@dataclass(frozen=True, slots=True)
class EntityOwnerBinding:
    entity_id: int
    template_id: int
    generation: int
    born_at: float
    record: EntityOwnerRecord
    birth_order: tuple[int, int, int] = (0, 0, 0)
    owner_order: tuple[int, int, int] = (0, 0, 0)
    birth_fingerprint: str = ""


def entity_owner_evidence_id(binding: EntityOwnerBinding) -> str:
    """Keep inferred permanent ownership distinct from a wire 34/2c row."""
    inferred = (
        binding.record.xor_phase < 0
        and binding.record.fingerprint.startswith("inferred-permanent:")
    )
    kind = "unique-permanent-job" if inferred else "34"
    return f"{kind}:{binding.generation}:{binding.record.fingerprint}"


@dataclass(frozen=True, slots=True)
class EntityOwnerRevocation:
    entity_id: int
    generation: int
    template_id: int
    scene_serial: int
    reason: str


@dataclass(frozen=True, slots=True)
class PendingEntityOwnerRecord:
    """Exact owner row waiting for independent template/lifetime evidence.

    A 34/2c row proves an owner but does not carry the entity template.  Keep
    it quarantined when the 06/27 spawn datagram was lost; it must never be
    exposed through ``resolve`` until a later exact attack key independently
    recovers one and only one summon template.
    """

    record: EntityOwnerRecord
    observed_at: float
    packet_serial: int


@dataclass(slots=True)
class EntityLifeState:
    """One scene-scoped runtime entity generation.

    Runtime actor IDs are reusable.  The generation, scene and birth proof
    therefore form the identity; the numeric actor ID alone never does.
    """

    entity_id: int
    template_id: int
    generation: int
    scene_serial: int
    born_at: float
    birth_source: str
    birth_fingerprint: str
    last_seen_at: float
    owner_id: int | None = None
    retired_at: float | None = None
    retired_reason: str = ""


class EntityOwnerEvidenceLedger:
    """Bounded birth-generation binding; wire repeats cannot renew a life.

    Missing births fail closed. Exact replayed birth/owner datagrams cannot
    establish another generation; their bounded tombstones survive scene reset.
    """

    def __init__(self, max_entities: int = 4096):
        self.max_entities = max(1, int(max_entities))
        self.births: OrderedDict[int, tuple[int, int, float]] = OrderedDict()
        self.bindings: dict[int, EntityOwnerBinding] = {}
        self.birth_evidence: dict[int, tuple[tuple[int, int, int], str]] = {}
        self.conflicts: set[int] = set()
        self.seen_births: OrderedDict[str, None] = OrderedDict()
        self.seen_records: OrderedDict[tuple, tuple[int, int]] = OrderedDict()
        self.revocations: OrderedDict[int, EntityOwnerRevocation] = OrderedDict()
        self.pending_owner_records: OrderedDict[int, PendingEntityOwnerRecord] = OrderedDict()
        self.pending_owner_conflicts: set[int] = set()
        self.active_lives: OrderedDict[int, EntityLifeState] = OrderedDict()
        self.retired_lives: OrderedDict[tuple[int, int, int], EntityLifeState] = OrderedDict()
        self.scene_serial = 1
        self.serial = 0

    def reset(self):
        for entity in self.births:
            self.retire(entity, reason='scene_changed')
        self.births.clear()
        self.bindings.clear()
        self.birth_evidence.clear()
        self.conflicts.clear()
        self.pending_owner_records.clear()
        self.pending_owner_conflicts.clear()
        self.active_lives.clear()
        self.scene_serial += 1

    def _remember_revocation(self, entity_id: int, reason: str):
        template, generation, _born = self.births.get(entity_id, (0, 0, 0.0))
        self.revocations[entity_id] = EntityOwnerRevocation(
            entity_id, generation, template, self.scene_serial, reason)
        self.revocations.move_to_end(entity_id)
        while len(self.revocations) > self.max_entities:
            self.revocations.popitem(last=False)

    def negative_state(self, entity_id: int, *, generation: int | None = None,
                       scene_serial: int | None = None, timestamp: float | None = None) -> str | None:
        """Why cached ownership must NOT be used; None means a current binding.

        Missing/evicted evidence also blocks fallback, so bounded tombstone
        eviction never makes an old cache authoritative again. Even when this
        returns None, use resolve().record.owner_id, not an unchecked cache ID.
        Optional generation/scene arguments refer to the original cached hit.
        """
        if scene_serial is not None and scene_serial != self.scene_serial:
            return 'scene_changed'
        birth = self.births.get(entity_id)
        revoked = self.revocations.get(entity_id)
        if birth is None:
            return revoked.reason if revoked else 'missing_birth'
        _template, current_generation, born = birth
        if generation is not None and generation != current_generation:
            return 'generation_mismatch'
        if entity_id in self.conflicts:
            return revoked.reason if revoked and revoked.generation == current_generation else 'conflicted'
        if timestamp is not None and not 0 <= timestamp-born <= 3600:
            return 'outside_lifetime'
        if entity_id not in self.bindings:
            return 'missing_owner'
        return None

    def owner_revoked(self, entity_id: int) -> bool:
        """Conservative legacy-cache veto, including missing current evidence.

        True is NOT a claim that no owner exists. It means a bare old cache is
        unsafe: missing births/owners also fail closed after bounded tombstone
        eviction. Use negative_state() for diagnostics and resolve() for proof.
        Lifetime expiry additionally requires resolve(..., timestamp).
        """
        return self.negative_state(entity_id) is not None

    def observe_birth(self, entity_id: int, template_id: int, timestamp: float,
                      wire_identity: str = "", packet_serial: int = 0,
                      birth_source: str = "spawn_packet") -> bool:
        if wire_identity and wire_identity in self.seen_births:
            return False
        if wire_identity:
            self.seen_births[wire_identity] = None
            while len(self.seen_births) > self.max_entities * 4:
                self.seen_births.popitem(last=False)
        if entity_id in self.births:
            self.retire(entity_id, reason='replaced', timestamp=timestamp)
        self.serial += 1
        self.births[entity_id] = (template_id, self.serial, timestamp)
        self.birth_evidence[entity_id] = ((packet_serial, 0, 0), wire_identity)
        self.active_lives[entity_id] = EntityLifeState(
            entity_id=entity_id,
            template_id=template_id,
            generation=self.serial,
            scene_serial=self.scene_serial,
            born_at=timestamp,
            birth_source=birth_source,
            birth_fingerprint=wire_identity,
            last_seen_at=timestamp,
        )
        self.active_lives.move_to_end(entity_id)
        self.births.move_to_end(entity_id)
        self.bindings.pop(entity_id, None)
        self.conflicts.discard(entity_id)
        while len(self.births) > self.max_entities:
            old = next(iter(self.births))
            self._remember_revocation(old, 'evicted')
            self.births.pop(old)
            self.bindings.pop(old, None)
            self.birth_evidence.pop(old, None)
            self.conflicts.discard(old)
            self.active_lives.pop(old, None)
        return True

    def observe_hit(self, entity_id: int, template_id: int, timestamp: float) -> EntityLifeState | None:
        """Touch only the currently proven generation; never create a life."""
        binding = self.resolve(entity_id, template_id, timestamp)
        life = self.active_lives.get(entity_id)
        if (binding is None or life is None or life.scene_serial != self.scene_serial
                or life.generation != binding.generation
                or life.template_id != template_id or life.retired_at is not None):
            return None
        life.last_seen_at = max(life.last_seen_at, timestamp)
        life.owner_id = binding.record.owner_id
        self.active_lives.move_to_end(entity_id)
        return life

    def recover_birth_from_attack(
        self,
        entity_id: int,
        template_id: int,
        timestamp: float,
        *,
        attack_evidence_id: str,
        packet_serial: int,
        max_owner_age: float = 30.0,
    ) -> EntityOwnerBinding | None:
        """Recover a lost spawn only from owner row + unique attack template.

        This is deliberately not a recent-cast fallback.  The pending 34/2c
        record proves the player, while the exact native entity attack key and
        static catalog prove one template.  The recovered lifetime begins at
        the owner record, so earlier damage is never retroactively claimed.
        """
        if (entity_id in self.births or entity_id in self.pending_owner_conflicts
                or not attack_evidence_id or not 0 < entity_id < 0x10000
                or template_id <= 0):
            return None
        pending = self.pending_owner_records.get(entity_id)
        if pending is None:
            return None
        age = timestamp - pending.observed_at
        if not 0.0 <= age <= max_owner_age:
            return None
        record = pending.record
        proof = sha256(
            f"recovered-birth:{self.scene_serial}:{entity_id}:{template_id}:"
            f"{record.message_fingerprint}:{record.record_offset}:{attack_evidence_id}".encode("ascii")
        ).hexdigest()
        if not self.observe_birth(
            entity_id,
            template_id,
            pending.observed_at,
            proof,
            pending.packet_serial,
            birth_source="owner_plus_unique_attack_template",
        ):
            return None
        _template, generation, born = self.births[entity_id]
        birth_order = (pending.packet_serial, 0, 0)
        owner_order = (pending.packet_serial, 0, max(1, record.record_offset))
        self.birth_evidence[entity_id] = (birth_order, proof)
        binding = EntityOwnerBinding(
            entity_id,
            template_id,
            generation,
            born,
            record,
            birth_order,
            owner_order,
            proof,
        )
        self.bindings[entity_id] = binding
        life = self.active_lives[entity_id]
        life.owner_id = record.owner_id
        life.last_seen_at = timestamp
        self.pending_owner_records.pop(entity_id, None)
        return binding

    def recover_preexisting_permanent_from_attack(
        self,
        entity_id: int,
        template_id: int,
        owner_id: int,
        timestamp: float,
        *,
        attack_evidence_id: str,
        packet_serial: int,
    ) -> EntityOwnerBinding | None:
        """Start a capture-local life for an already alive permanent summon.

        This path is intentionally narrower than lost-birth recovery.  It is
        used only after the caller has proved a permanent singleton template
        from the entity's exact native attack key and has found exactly one
        compatible active player profile.  The inferred owner record is kept
        visibly distinct from a real 34/2c row, and the life begins at this
        first observed attack, so no earlier unrelated short-ID traffic can be
        reclaimed.
        """
        if (
            entity_id in self.births
            or entity_id in self.pending_owner_conflicts
            or not attack_evidence_id
            or not 0 < entity_id < 0x10000
            or template_id <= 0
            or not 0x1000000 <= owner_id < 0xFFFFFFFF
        ):
            return None
        pending = self.pending_owner_records.get(entity_id)
        if pending is not None and pending.record.owner_id != owner_id:
            return None
        revoked = self.revocations.get(entity_id)
        if revoked is not None and revoked.scene_serial == self.scene_serial:
            return None
        proof = sha256(
            f"preexisting-permanent:{self.scene_serial}:{entity_id}:"
            f"{template_id}:{owner_id}:{attack_evidence_id}".encode("ascii")
        ).hexdigest()
        if not self.observe_birth(
            entity_id,
            template_id,
            timestamp,
            proof,
            packet_serial,
            birth_source="unique_permanent_attack_and_job",
        ):
            return None
        _template, generation, born = self.births[entity_id]
        record = EntityOwnerRecord(
            entity_id=entity_id,
            owner_id=owner_id,
            observer_id=owner_id,
            record_offset=1,
            xor_phase=-1,
            fingerprint=f"inferred-permanent:{proof}",
            message_fingerprint=attack_evidence_id,
        )
        birth_order = (packet_serial, 0, 0)
        owner_order = (packet_serial, 0, 1)
        binding = EntityOwnerBinding(
            entity_id,
            template_id,
            generation,
            born,
            record,
            birth_order,
            owner_order,
            proof,
        )
        self.bindings[entity_id] = binding
        life = self.active_lives[entity_id]
        life.owner_id = owner_id
        life.last_seen_at = timestamp
        self.pending_owner_records.pop(entity_id, None)
        return binding

    def bind_existing_permanent_unique_owner(
        self,
        entity_id: int,
        template_id: int,
        owner_id: int,
        timestamp: float,
        *,
        profile_evidence_id: str,
        packet_serial: int,
    ) -> EntityOwnerBinding | None:
        """Bind an already spawned permanent singleton before its first Hit.

        This is the proactive counterpart of
        :meth:`recover_preexisting_permanent_from_attack`.  The ordinary
        spawn packet has already proved the current entity generation and
        template; the caller additionally proves that exactly one active
        player profile can own that reviewed permanent summon family.  It is
        intentionally unusable for missing births, temporary summons or an
        ambiguous party, and a later contradictory wire owner still revokes
        the inferred binding through ``observe_record``.
        """

        birth = self.births.get(entity_id)
        if (
            birth is None
            or birth[0] != template_id
            or entity_id in self.conflicts
            or entity_id in self.pending_owner_conflicts
            or entity_id in self.bindings
            or not profile_evidence_id
            or not 0 < entity_id < 0x10000
            or template_id <= 0
            or not 0x1000000 <= owner_id < 0xFFFFFFFF
            or not 0.0 <= timestamp - birth[2] <= 3600.0
        ):
            return None
        pending = self.pending_owner_records.get(entity_id)
        if pending is not None and pending.record.owner_id != owner_id:
            return None
        template, generation, born = birth
        proof = sha256(
            f"prewarmed-permanent:{self.scene_serial}:{entity_id}:"
            f"{template_id}:{owner_id}:{profile_evidence_id}".encode("ascii")
        ).hexdigest()
        record = EntityOwnerRecord(
            entity_id=entity_id,
            owner_id=owner_id,
            observer_id=owner_id,
            record_offset=1,
            xor_phase=-1,
            fingerprint=f"inferred-permanent:{proof}",
            message_fingerprint=profile_evidence_id,
        )
        birth_order, birth_fingerprint = self.birth_evidence.get(
            entity_id, ((packet_serial, 0, 0), "")
        )
        owner_order = (max(packet_serial, birth_order[0]), 0, 1)
        binding = EntityOwnerBinding(
            entity_id,
            template,
            generation,
            born,
            record,
            birth_order,
            owner_order,
            birth_fingerprint,
        )
        self.bindings[entity_id] = binding
        life = self.active_lives.get(entity_id)
        if life is not None and life.generation == generation:
            life.owner_id = owner_id
            life.last_seen_at = max(life.last_seen_at, timestamp)
        self.pending_owner_records.pop(entity_id, None)
        return binding

    @staticmethod
    def _record_key(record: EntityOwnerRecord):
        return (record.entity_id, record.message_fingerprint or record.fingerprint,
                record.record_offset, record.owner_id)

    def _remember_record(self, record: EntityOwnerRecord) -> bool:
        key = self._record_key(record)
        if key in self.seen_records:
            return False
        birth = self.births.get(record.entity_id)
        self.seen_records[key] = (record.entity_id, birth[1] if birth else -1)
        while len(self.seen_records) > self.max_entities * 8:
            self.seen_records.popitem(last=False)
        return True

    def observe_batch(self, batch: EntityOwnerBatch, timestamp: float, packet_serial: int = 0):
        """Consume one completely validated batch; conflicting entities are atomic.

        Unambiguous entities in the same message are unaffected. Exact already
        seen rows cannot revoke a later generation or resurrect a missing birth.
        Call this instead of feeding the compatibility reader to observe_record.
        """
        grouped: dict[int, list[EntityOwnerRecord]] = {}
        for row in batch.records:
            grouped.setdefault(row.entity_id, []).append(row)
        for entity, rows in grouped.items():
            if len({row.owner_id for row in rows}) <= 1:
                for row in rows:
                    self.observe_record(row, timestamp, packet_serial)
                continue
            fresh = [row for row in rows if self._record_key(row) not in self.seen_records]
            for row in rows:
                self._remember_record(row)
            birth = self.births.get(entity)
            if not fresh or birth is None or timestamp < birth[2]:
                continue
            birth_order = self.birth_evidence[entity][0]
            if (packet_serial > 0 and birth_order[0] > 0
                    and any((packet_serial, 0, row.record_offset) <= birth_order for row in fresh)):
                continue
            self.retire(entity, reason='conflicted')

    def observe_packet(self, payload: bytes, direction: str | None,
                       timestamp: float, serial: int = 0) -> tuple[EntityOwnerRecord, ...]:
        """Atomic host entry: consume negative evidence, return usable rows only.

        No host cache is written. An invalid packet does not revoke anything.
        Previously replayed rows cannot create a new generation's owner.
        """
        batch = decode_entity_owner_batch(payload, direction)
        if batch is None:
            return ()
        self.observe_batch(batch, timestamp, serial)
        result = []
        for row in batch.records:
            birth = self.births.get(row.entity_id)
            binding = self.resolve(row.entity_id, birth[0], timestamp) if birth else None
            if (row.entity_id not in batch.conflicted_entity_ids and binding is not None
                    and binding.record.owner_id == row.owner_id):
                result.append(row)
        return tuple(result)

    def observe_record(self, record: EntityOwnerRecord, timestamp: float, packet_serial: int = 0):
        birth = self.births.get(record.entity_id)
        if not self._remember_record(record):
            # No later replay can create/renew a binding, particularly after
            # death, scene change, a missing birth, or a genuine new birth.
            return
        if birth is None:
            previous = self.pending_owner_records.get(record.entity_id)
            if previous is not None and previous.record.owner_id != record.owner_id:
                self.pending_owner_conflicts.add(record.entity_id)
                self.pending_owner_records.pop(record.entity_id, None)
                return
            self.pending_owner_records[record.entity_id] = PendingEntityOwnerRecord(
                record, timestamp, packet_serial
            )
            self.pending_owner_records.move_to_end(record.entity_id)
            while len(self.pending_owner_records) > self.max_entities:
                old, _pending = self.pending_owner_records.popitem(last=False)
                self.pending_owner_conflicts.discard(old)
            return
        if record.entity_id in self.conflicts:
            return
        template, generation, born = birth
        birth_order = self.birth_evidence[record.entity_id][0]
        if (timestamp < born or (packet_serial > 0 and birth_order[0] > 0
                and (packet_serial, 0, record.record_offset) <= birth_order)):
            return
        existing = self.bindings.get(record.entity_id)
        if existing and existing.record.owner_id != record.owner_id:
            # Even a late contradictory owner revokes certainty. Do not hide
            # it behind the initialization time gate.
            self.retire(record.entity_id, reason='conflicted')
            return
        # All 470 validated rows arrived <=130 ms after birth. 500 ms allows
        # transport jitter without accepting arbitrary later owner broadcasts.
        if not 0 <= timestamp-born <= .5:
            return
        if existing is None:
            birth_order, birth_fingerprint = self.birth_evidence[record.entity_id]
            self.bindings[record.entity_id] = EntityOwnerBinding(record.entity_id, template,
                generation, born, record, birth_order, (packet_serial, 0, record.record_offset), birth_fingerprint)
            life = self.active_lives.get(record.entity_id)
            if life is not None and life.generation == generation:
                life.owner_id = record.owner_id
                life.last_seen_at = max(life.last_seen_at, timestamp)

    def resolve(self, entity_id: int, template_id: int, timestamp: float) -> EntityOwnerBinding | None:
        binding = self.bindings.get(entity_id)
        if (binding and binding.template_id == template_id
                and 0 <= timestamp-binding.born_at <= 3600
                and entity_id not in self.conflicts):
            return binding
        return None

    def retire(self, entity_id: int, *, reason: str = 'retired', timestamp: float | None = None):
        """Keep a bounded negative tombstone; a genuine fresh birth may replace it."""
        self._remember_revocation(entity_id, reason)
        self.bindings.pop(entity_id, None)
        self.pending_owner_records.pop(entity_id, None)
        self.pending_owner_conflicts.discard(entity_id)
        life = self.active_lives.pop(entity_id, None)
        if life is not None:
            life.retired_at = timestamp if timestamp is not None else life.last_seen_at
            life.retired_reason = reason
            key = (life.scene_serial, life.entity_id, life.generation)
            self.retired_lives[key] = life
            self.retired_lives.move_to_end(key)
            while len(self.retired_lives) > self.max_entities:
                self.retired_lives.popitem(last=False)
        if entity_id in self.births:
            self.conflicts.add(entity_id)
