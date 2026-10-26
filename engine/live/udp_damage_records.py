"""Independent, unintegrated v576 UDP physical-record inventory.

Only complete top-level type-08 physical damage records are interpreted.  The
0075/0079 forms are the 0073/0077 layouts with a two-byte opaque tail; their
damage fields do not move.
The entire declared message, every companion record, and all 13 XOR phases
are checked first. No legacy protocol reader, signature/phase cache, actor
state, skill catalog, recent target, or owner inference is consulted.

This is NOT an emission/attribution policy. Unknown typed records are retained
in the inventory, never promoted to hits (in particular type 07 is not 08).
Invalid field values remain visible as rejected candidates. Existing legacy
readers are deliberately unaffected, including their short-prefix formats.

direct41's object-ID interpretation is unproven: damage_object_id is None,
not zero and not the direct37 slot. Its eight +13..+21 bytes remain opaque.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

_KEY = bytes.fromhex("c8bfcda4943f2642fd8a723474")
_XOR_TABLES = tuple(
    bytes(value ^ key_byte for value in range(256))
    for key_byte in _KEY
)
_DAMAGE_LAYOUTS = {
    0x73: ("direct37", 19, 35),
    0x75: ("direct37", 19, 35),
    0x77: ("direct41", 23, 39),
    0x79: ("direct41", 23, 39),
}


@dataclass(frozen=True, slots=True)
class UdpTypedRecord:
    record_start: int
    record_offset: int
    record_end: int
    record_type: int
    declared_length: int
    # Plaintext includes the four-byte actor/target prefix, not just marker.
    record_bytes: bytes
    wire_record_sha256: str


@dataclass(frozen=True, slots=True)
class UdpDamageRecord:
    family: str
    record_start: int
    record_offset: int
    record_end: int
    declared_length: int
    source_actor_id: int
    target_actor_id: int
    observer_actor_id: int
    damage: int
    critical: bool
    result_flags: int
    damage_result_group_id: int
    damage_object_id: int | None
    opaque_object_context: bytes
    # Raw fixed-slot integers only; NOT certified player/self-attack skills.
    raw_skill_slots: tuple[tuple[int, int], ...]
    xor_phase: int
    wire_sha256: str
    wire_record_sha256: str
    record_bytes: bytes
    rejection_reasons: tuple[str, ...]

    @property
    def admissible_fields(self) -> bool:
        """Necessary numeric checks only; no claim of owner, target or Hit proof."""
        return not self.rejection_reasons

    @property
    def physical_key(self) -> tuple[int, int, int, int, bool, int, int | None]:
        """Within ONE message. Never use this to deduplicate repeated packets."""
        return (self.record_offset, self.source_actor_id, self.target_actor_id,
                self.damage, self.critical, self.damage_result_group_id,
                self.damage_object_id)


@dataclass(frozen=True, slots=True)
class UdpDamageInventory:
    wire_payload: bytes
    wire_sha256: str
    xor_phase: int | None
    typed_records: tuple[UdpTypedRecord, ...]
    damage_records: tuple[UdpDamageRecord, ...]
    rejection_reason: str = ""

    @property
    def complete(self) -> bool:
        return self.xor_phase is not None and not self.rejection_reason


def _body(payload: bytes, phase: int) -> bytes:
    decoded = bytearray(payload)
    key_length = len(_KEY)
    # Transform the thirteen interleaved XOR lanes in C.  This is the same
    # byte-for-byte operation as the former Python generator, while retaining
    # this audit reader's independence from the production decoder/cache.
    for residue in range(key_length):
        start = 9 + residue
        if start >= len(decoded):
            break
        key_index = (start - phase) % key_length
        decoded[start::key_length] = decoded[start::key_length].translate(
            _XOR_TABLES[key_index]
        )
    return bytes(decoded)


def _walk(body: bytes) -> tuple[tuple[int, int, int, int, int], ...] | None:
    """Walk ONLY declared boundaries from byte 14. Never search for markers.

    Unknown record types/lengths remain opaque. A zero-length opaque record
    still consumes its seven-byte prefix/header; it cannot stall this walk.
    Its presence does not establish any damage interpretation.
    """
    cursor = 14
    records = []
    while cursor < len(body):
        if len(body) - cursor < 7:
            return None
        marker = cursor + 4
        declared = int.from_bytes(body[marker+1:marker+3], "little")
        end = marker + 3 + declared
        if end > len(body):
            return None
        records.append((cursor, marker, end, body[marker], declared))
        cursor = end
    return tuple(records) if records else None


def _damage_record(record: UdpTypedRecord, body: bytes, phase: int,
                   wire_hash: str) -> UdpDamageRecord:
    m = record.record_offset
    family, amount_offset, critical_offset = _DAMAGE_LAYOUTS[record.declared_length]
    u32 = lambda offset: int.from_bytes(body[offset:offset+4], "little")
    source, target = u32(m+8), u32(m-4)
    amount = int.from_bytes(body[m+amount_offset:m+amount_offset+8], "little")
    flags = body[m+critical_offset]
    reasons = []
    if source in (0, 0xFFFFFFFF):
        reasons.append("invalid_source_actor")
    if target in (0, 0xFFFFFFFF):
        reasons.append("invalid_target_actor")
    if source == target:
        reasons.append("self_target")
    # Match only established numeric sentinel checks, never infer a missing
    # amount or truncate a uint64 into a convenient uint32.
    if not 0 < amount <= 1_000_000_000_000:
        reasons.append("amount_out_of_supported_range")
    if 0xFFFF0000 <= amount <= 0xFFFFFFFF:
        reasons.append("signed32_sentinel")
    if amount & 0xFFFFFF == 0x04001B:
        reasons.append("status_marker_amount")
    skills = (102, 114) if family == "direct37" else (106,)
    return UdpDamageRecord(
        family=family, record_start=record.record_start, record_offset=m,
        record_end=record.record_end, declared_length=record.declared_length,
        source_actor_id=source, target_actor_id=target, observer_actor_id=u32(10),
        damage=amount, critical=bool(flags & 1), result_flags=flags,
        damage_result_group_id=u32(m+4),
        damage_object_id=u32(m+13) if family == "direct37" else None,
        opaque_object_context=body[m+13:m+21] if family == "direct41" else b"",
        raw_skill_slots=tuple((offset, u32(m+offset)) for offset in skills),
        xor_phase=phase, wire_sha256=wire_hash,
        wire_record_sha256=record.wire_record_sha256,
        record_bytes=record.record_bytes, rejection_reasons=tuple(reasons),
    )


def inspect_udp_damage_records(payload: bytes, direction: str | None = None) -> UdpDamageInventory:
    """Return a lossless inventory or a fail-closed message rejection reason.

    Explicit server-to-client direction is required. Uniqueness considers
    EVERY complete typed phase, not just phases containing a desired hit.
    Thus a second complete interpretation cannot be hidden by hit filtering.
    Every type-08 record is in typed_records; only the exact 0073/0075 and
    0077/0079 damage lengths are field-decoded. Callers must count unsupported 08 competitors
    when proving unique primary identity, and retain invalid-field candidates
    in audit denominators instead of silently dropping them.
    """
    wire = bytes(payload)
    wire_hash = sha256(wire).hexdigest()

    def rejected(reason: str) -> UdpDamageInventory:
        return UdpDamageInventory(wire, wire_hash, None, (), (), reason)

    if direction != "server_to_client":
        return rejected("unproven_server_to_client_direction")
    if len(wire) < 21:
        return rejected("message_too_short")
    if int.from_bytes(wire[7:9], "little") + 3 != len(wire):
        return rejected("outer_declared_length_mismatch")
    candidates = []
    for phase in range(len(_KEY)):
        body = _body(wire, phase)
        records = _walk(body)
        if records is not None:
            candidates.append((phase, body, records))
            if len(candidates) > 1:
                return rejected("nonunique_complete_typed_phase")
    if not candidates:
        return rejected("incomplete_typed_record_sequence")
    phase, body, boundaries = candidates[0]
    records = tuple(UdpTypedRecord(start, marker, end, kind, declared,
                                   body[start:end], sha256(wire[start:end]).hexdigest())
                    for start, marker, end, kind, declared in boundaries)
    damage = tuple(_damage_record(record, body, phase, wire_hash) for record in records
                   if record.record_type == 8 and record.declared_length in _DAMAGE_LAYOUTS)
    return UdpDamageInventory(wire, wire_hash, phase, records, damage)


def decode_udp_damage_records(payload: bytes, direction: str | None = None) -> tuple[UdpDamageRecord, ...]:
    """Convenience physical enumeration; retains invalid-field rows for audit.

    Use inspect_udp_damage_records when a rejection reason or unsupported
    record inventory is required. Neither function emits CombatEvents.
    """
    return inspect_udp_damage_records(payload, direction).damage_records
