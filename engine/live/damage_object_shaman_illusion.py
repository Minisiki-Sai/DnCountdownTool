"""Opt-in V576 ACT-ordinal/signal-index proofs for 5520 and 5826.

No global template whitelist changes. The server 0961 tail is an ACT action
ordinal + type15 signal ordinal, NOT a cast-instance token. The ordinal is
resolved in the ordered actor ACT manifest, then in the exact signal array.
An independent same-owner root action, a bounded object generation and an
unchanged raw result key are mandatory. Fixed-slot conflicts fail closed.
See outputs/v25_shaman_illusion_20260907/REPORT.md for raw/resource evidence.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Iterable

from .protocol import (
    _decode_v576_body, _v576_candidate_xor_phases, _v576_wire_may_contain_marker,
    decode_v576_embedded_direct_damage_events,
    decode_v576_server_damage_object_registrations,
)


# Actor6 / Kali.act: 45 basic + 39 skill; Scremer local ordinal6/7.
# Actor7 / Assassin.act: 493 preceding actions; Abysswalker local2/11.
# Explicit SkillLevel processors and type1/index1 continuation edges resolve
# these type15 records. The rejected global 5826 mapping remains rejected.
SIGNAL_BINDINGS = {
    (5520, 90): (5204, frozenset({11, 12, 13, 33})),
    (5520, 91): (5204, frozenset({9, 10, 11, 12})),
    (5826, 495): (6308, frozenset({17, 18, 19, 20, 21, 22})),
    (5826, 504): (6308, frozenset({42, 43, 44, 45, 46, 47})),
}

# Necessary wire markers only, never positive evidence. Keep the registration
# tuple/order identical to the shared reader: its existing bounded marker and
# decoded-body caches can then be reused after the main decoder's pass. Unknown
# registrations (including alternate families) must still revoke old proofs.
_SIGNAL_MARKERS = (b"\x0e\x23\x00", b"\x12\x24\x00", b"\x09\x61\x00")
_REGISTRATION_MARKERS = (
    b"\x09\x5d\x00", b"\x09\x61\x00", b"\x09\x6e\x00",
    b"\x09\x62\x00", b"\x09\x66\x00", b"\x09\x6a\x00",
    b"\x09\x6f\x00", b"\x09\x73\x00",
)
_HIT_MARKERS = (b"\x08\x73\x00", b"\x08\x75\x00")
_SIGNAL_CACHE_MAX_PAYLOAD = 65536


@dataclass(frozen=True)
class SignalRegistration:
    owner: int
    object_id: int
    template: int
    action_ordinal: int
    signal_ordinal: int
    skill: int
    offset: int
    phase: int


@dataclass(frozen=True)
class RootAction:
    owner: int
    skill: int
    raw_skill: int
    enhancement: int
    offset: int
    phase: int


def _u32(b: bytes, offset: int) -> int:
    return int.from_bytes(b[offset:offset + 4], "little")


def _records(body: bytes) -> tuple[tuple[int, int, int], ...]:
    """Closed standard server TLV envelope; relay variants are unapproved."""
    if len(body) < 21 or int.from_bytes(body[7:9], "little") + 3 != len(body):
        return ()
    if not 0 < _u32(body, 10) < 0xFFFFFFFF:
        return ()
    rows = []
    cursor = 14
    while cursor < len(body):
        if cursor + 7 > len(body) or len(rows) >= 64:
            return ()
        owner = _u32(body, cursor)
        op = body[cursor + 4]
        size = int.from_bytes(body[cursor + 5:cursor + 7], "little")
        end = cursor + 7 + size
        if not 0 < owner < 0xFFFFFFFF or not size or end > len(body):
            return ()
        rows.append((cursor + 4, op, size))
        cursor = end
    return tuple(rows)


def decode_signal_evidence(payload: bytes, direction: str | None):
    """Return only uniquely decoded complete root actions and 0961 tails."""
    # Use actual server packets. No guessing the direction of accelerator
    # client/server ambiguous 73/80-byte action formats.
    # Length bytes precede the XOR body and are identical in every phase.
    # A negative encrypted-marker search covers all 13 key residues, not only
    # a guessed signature phase. Positives still pass the unchanged parser.
    if (direction != "server_to_client" or len(payload) < 21
            or int.from_bytes(payload[7:9], "little") + 3 != len(payload)
            or not _v576_wire_may_contain_marker(payload, _SIGNAL_MARKERS, start_offset=18)):
        return (), ()
    if len(payload) <= _SIGNAL_CACHE_MAX_PAYLOAD:
        return _read_signal_evidence_cached(payload)
    return _read_signal_evidence(payload)


@lru_cache(maxsize=32)
def _read_signal_evidence_cached(payload: bytes):
    # Immutable roots/registrations only; no lifetime or owner state is cached.
    return _read_signal_evidence(payload)


def _read_signal_evidence(payload: bytes):
    variants = []
    for phase in _v576_candidate_xor_phases(payload):
        b = _decode_v576_body(payload, phase)
        records = _records(b)
        if not records:
            continue
        roots, registrations = [], []
        for m, op, size in records:
            owner = _u32(b, m - 4)
            if (op, size) in {(14, 35), (18, 36)}:
                skill = _u32(b, m + 3)
                enhancement = _u32(b, m + 21) if op == 14 else 0
                # Base IllusionStrike alone does not prove the EX manifest.
                valid = (skill == 6308 and enhancement == 6705) or skill == 6705
                valid |= skill == 5204
                if valid:
                    roots.append(RootAction(owner, 6308 if skill == 6705 else skill,
                                            skill, enhancement, m, phase))
            if (op, size) != (9, 97):
                continue
            obj, template = _u32(b, m + 8), _u32(b, m + 12)
            action = int.from_bytes(b[m + 48:m + 50], "little")
            signal = int.from_bytes(b[m + 50:m + 52], "little")
            binding = SIGNAL_BINDINGS.get((template, action))
            if (
                binding is None or signal not in binding[1]
                or b[m + 3:m + 8] != bytes(5)
                or owner != _u32(b, m + 44)
                or not 0 < obj <= 0xFFFFFF
                or b[m + 52:m + 60] != b"\xff" * 8
                or _u32(b, m + 64) != action
            ):
                continue
            registrations.append(SignalRegistration(owner, obj, template, action,
                                                     signal, binding[0], m, phase))
        if roots or registrations:
            variants.append((tuple(roots), tuple(registrations)))
    return variants[0] if len(variants) == 1 else ((), ())


@dataclass
class _Origin:
    registration: SignalRegistration
    born: float
    expires: float
    root_time: float
    root_serial: int
    registration_serial: int
    generation: int
    revoked: bool = False


@dataclass(frozen=True)
class HitProof:
    owner: int
    target: int
    object_id: int
    result_group: int
    damage: int
    critical: bool
    skill: int
    template: int
    action_ordinal: int
    signal_ordinal: int
    generation: int
    root_serial: int
    registration_serial: int
    hit_serial: int
    hit_offset: int


class ShamanIllusionSignalLedger:
    """Bounded supplemental ledger; never changes damage or Hit eligibility.

    A server echo does not renew a generation. Changed opaque bytes do not
    make a new creation. Reuse/unknown registrations/fixed conflicts revoke
    proof. New generations require a different independently observed root.
    """
    ROOT_ATTACH_SECONDS = 2.5
    OBJECT_SECONDS = 8.0  # unchanged from the frozen v24 default
    RETENTION_SECONDS = 16.0
    LIMIT = 4096

    def __init__(self):
        self.reset()

    def reset(self):
        self.roots = []
        self.origins = {}
        self.serial = 0
        self.generation = 0
        self.high_water = float('-inf')
        self.quarantine_until = float('-inf')
        self.diagnostics = Counter()

    def advance(self, timestamp: float) -> bool:
        """Clock/expiry bookkeeping only; never inspect a transport payload.

        TCP must retain this small step so skipping its UDP readers cannot
        change proof serials, out-of-order rejection or object expiry.
        """
        self.serial += 1
        # Out-of-order recovery cannot move births/lifetimes backwards.
        if timestamp < self.high_water:
            self.diagnostics['out_of_order'] += 1
            return False
        self.high_water = timestamp
        self.roots = [r for r in self.roots if timestamp - r[0] <= self.ROOT_ATTACH_SECONDS]
        self.origins = {k:v for k,v in self.origins.items()
                        if timestamp <= v.expires + self.RETENTION_SECONDS}
        return True

    def process(self, payload: bytes, direction: str | None, timestamp: float,
                known_skills: Iterable[int]) -> tuple[HitProof, ...]:
        if not self.advance(timestamp):
            return ()
        roots, regs = decode_signal_evidence(payload, direction)
        # Match the shared readers' necessary conditions exactly. These calls
        # reuse their cached wire-marker results, without skipping conflicts
        # merely because there is currently no live supported object.
        known_regs = ()
        if (direction in (None, "server_to_client", "client_to_server") and len(payload) >= 57
                and _v576_wire_may_contain_marker(payload, _REGISTRATION_MARKERS, start_offset=9)):
            known_regs = decode_v576_server_damage_object_registrations(
                payload, direction, (), include_unmapped=True)
        hits = ()
        if (direction in (None, "server_to_client") and len(payload) >= 57
                and _v576_wire_may_contain_marker(payload, _HIT_MARKERS, start_offset=9)):
            hits = decode_v576_embedded_direct_damage_events(payload, direction, known_skills)
        typed = {(r.owner, r.object_id, r.offset): r for r in regs}
        queue = [(r.offset, 0, r) for r in roots]
        queue += [(r.record_offset, 1, r) for r in known_regs]
        queue += [(h.record_offset, 2, h) for h in hits]
        proven = []
        for offset, kind, value in sorted(queue, key=lambda row: (row[0], row[1])):
            if kind == 0:
                # Exact repeated action messages are echoes, not new roots.
                if not any(r[2].owner == value.owner and r[2].skill == value.skill
                           and abs(r[0] - timestamp) <= 0.12 for r in self.roots):
                    self.roots.append((timestamp, self.serial, value))
                continue
            key = (value.source_actor_id, value.damage_object_id)
            previous = self.origins.get(key)
            if kind == 1:
                reg = typed.get((*key, offset))
                if reg is None:
                    # A new unsupported observation cannot preserve our
                    # narrower action/signal proof merely by reusing the
                    # same template. Leave ordinary v24 attribution alone.
                    if previous:
                        previous.revoked = True
                    continue
                candidates = [r for r in self.roots if r[2].owner == reg.owner
                              and r[2].skill == reg.skill and r[0] <= timestamp]
                if len(candidates) != 1:
                    self.diagnostics['missing_or_ambiguous_root'] += 1
                    continue
                root_time, root_serial, root = candidates[0]
                if previous:
                    same = (previous.registration.template, previous.registration.action_ordinal,
                            previous.registration.signal_ordinal) == (reg.template, reg.action_ordinal, reg.signal_ordinal)
                    if same:
                        continue  # cannot renew or resurrect an expired observation
                    if timestamp <= previous.expires or root_serial == previous.root_serial:
                        previous.revoked = True
                        self.diagnostics['conflicting_registration'] += 1
                        continue
                if timestamp < self.quarantine_until:
                    continue
                self.generation += 1
                self.origins[key] = _Origin(reg, timestamp, timestamp + self.OBJECT_SECONDS,
                                           root_time, root_serial, self.serial, self.generation)
                continue
            h = value
            if previous is None or previous.revoked or not previous.born <= timestamp <= previous.expires:
                continue
            if h.skill_id_exact and h.skill_id not in (None, previous.registration.skill):
                previous.revoked = True
                self.diagnostics['fixed_skill_conflict'] += 1
                continue
            if not h.damage or h.healing or h.periodic_kind:
                continue
            r = previous.registration
            proven.append(HitProof(r.owner, h.target_actor_id, r.object_id,
                                   h.damage_result_group_id, h.damage, bool(h.critical),
                                   r.skill, r.template, r.action_ordinal, r.signal_ordinal,
                                   previous.generation, previous.root_serial,
                                   previous.registration_serial, self.serial, offset))
        if len(self.origins) > self.LIMIT or len(self.roots) > self.LIMIT:
            self.origins.clear()
            self.roots.clear()
            self.quarantine_until = timestamp + self.OBJECT_SECONDS + self.RETENTION_SECONDS
            self.diagnostics['capacity_quarantine'] += 1
        return tuple(proven)


def rewrite_events(events, proofs: tuple[HitProof, ...], catalog):
    """Join full raw keys; keep inferred/unknown owners and overflow untouched."""
    from .models import CombatEvent
    grouped = {}
    for proof in proofs:
        key = (proof.owner, proof.target, proof.object_id, proof.result_group,
               proof.damage, proof.critical)
        grouped.setdefault(key, []).append(proof)
    result = []
    for event in events:
        if not isinstance(event, CombatEvent) or event.healing or event.has_real_hit_evidence is not True:
            result.append(event)
            continue
        try:
            owner = int(event.source_id.split('@')[0], 0)
            target = int(event.target_id.split('@')[0], 0)
        except ValueError:
            result.append(event)
            continue
        key = (owner, target, event.wire_damage_object_id, event.wire_result_group_id,
               event.amount, event.critical)
        options = grouped.get(key, [])
        if (len(options) != 1 or event.attribution_method == 'direct_hit_overflow'
                or getattr(event, 'wire_entity_id', 0) or getattr(event, 'entity_owner_evidence_id', '')):
            result.append(event)
            continue
        proof = options[0]
        info = catalog.get(proof.skill)
        if info is None:
            result.append(event)
            continue
        result.append(replace(event, skill_id=str(proof.skill), skill_name=info[0],
                              attribution_method='damage_object_projectile_link',
                              attribution_confidence=1.0,
                              cast_instance_id=(f'act-signal:{proof.owner:08X}:{proof.object_id}:'
                                                f'{proof.generation}:{proof.action_ordinal}:'
                                                f'{proof.signal_ordinal}'),
                              raw_skill_id=event.raw_skill_id,
                              raw_skill_name=event.raw_skill_name,
                              raw_attribution_method=event.raw_attribution_method))
    return result


def install_decoder_adapter(decoder):
    """Explicit opt-in, per decoder; no shared module/global modifications."""
    if hasattr(decoder, 'v25_shaman_illusion_ledger'):
        return decoder.v25_shaman_illusion_ledger
    ledger = ShamanIllusionSignalLedger()
    decoder.v25_shaman_illusion_ledger = ledger
    feed = decoder.feed_events
    reset = decoder.damage_object_origins.reset

    def reset_with_proof():
        ledger.reset()
        return reset()

    def feed_with_proof(packet):
        # Main feed owns scene/connection resets and must always run first.
        events = feed(packet)
        if str(packet.transport or "").upper() != "UDP":
            ledger.advance(packet.timestamp_seconds)
            return events
        proofs = ledger.process(packet.payload, packet.inferred_direction(),
                                packet.timestamp_seconds, decoder.damage_skill_ids)
        return rewrite_events(events, proofs, decoder.skill_catalog) if proofs else events

    decoder.damage_object_origins.reset = reset_with_proof
    decoder.feed_events = feed_with_proof
    return ledger
