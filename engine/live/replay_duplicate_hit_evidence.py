"""Supplement an admitted replay Hit from its byte-identical UDP retry.

This never emits an event, runs the decoder on a duplicate, reorders packets,
or changes an object's lifetime. Unknown/reused generations fail closed.
The retry interval bounds memory; exact bytes and a forward object proof,
not time proximity, choose the skill. Replay-only until live consumers have
an explicit metadata-correction contract.
"""
from collections import OrderedDict, Counter
from dataclasses import dataclass, replace
from hashlib import sha256

from .damage_attribution import is_deterministic_attribution, attribution_layer, AttributionLayer, attribution_rule
from .models import CombatEvent
from .udp_damage_records import inspect_udp_damage_records


def _eligible(e):
    return (isinstance(e, CombatEvent) and not e.healing and e.amount > 0
            and e.has_real_hit_evidence is True and str(e.skill_id).isdecimal()
            and int(e.skill_id) > 0 and e.wire_source_actor_id > 65535
            and not e.wire_entity_id and e.wire_damage_object_id > 0
            and e.source_id == f'0x{e.wire_source_actor_id:08X}'
            and e.target_id.partition('@')[0] == f'0x{e.wire_target_actor_id:08X}'
            and 'direct37' in e.wire_damage_layout
            and e.attribution_method != 'direct_hit_overflow'
            and not attribution_rule(e.attribution_method).locked
            and not is_deterministic_attribution(e.attribution_method)
            and attribution_layer(e.attribution_method) is not AttributionLayer.STATUS)


def _physical(e):
    return (e.wire_record_offset, e.wire_source_actor_id, e.wire_target_actor_id,
            e.amount, e.critical, e.wire_result_group_id, e.wire_damage_object_id)


def _key(packet):
    return (packet.transport, packet.source_ip, packet.destination_ip,
            packet.source_port, packet.destination_port,
            packet.tcp_connection_generation, bytes(packet.payload))


@dataclass(frozen=True)
class Pending:
    timestamp: float
    epoch: int
    serial: int
    rows: tuple


class ReplayDuplicateHitEvidence:
    def __init__(self, *, max_entries=2048, max_bytes=2*1024*1024, retry_seconds=3.0,
                 max_seen_objects=65536):
        self.pending = OrderedDict()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.retry_seconds = retry_seconds
        self.bytes = 0
        self.stats = Counter()
        self.seen_objects = set()
        self.max_seen_objects = max_seen_objects
        self.epoch = None
        self.history_exhausted = False

    def _drop(self, key):
        if self.pending.pop(key, None) is not None:
            self.bytes -= len(key[-1])

    def _prune(self, now, epoch):
        if epoch != self.epoch:
            self.pending.clear()
            self.bytes = 0
            self.seen_objects.clear()
            self.history_exhausted = False
            self.epoch = epoch
        for key, row in tuple(self.pending.items()):
            if row.epoch != epoch or now-row.timestamp > self.retry_seconds:
                self._drop(key)

    def remember(self, packet, events, start_index, decoder):
        ledger = decoder.damage_object_origins
        self._prune(packet.timestamp_seconds, ledger.evidence_epoch)
        if packet.transport != 'UDP' or packet.inferred_direction() != 'server_to_client':
            return
        key = _key(packet)
        # An independently admitted repeated message starts a new observation;
        # never borrow a row from a previous logical message occurrence.
        self._drop(key)
        candidates = []
        touched = set()
        for index in range(start_index, len(events)):
            e = events[index]
            objkey = (getattr(e, 'wire_source_actor_id', 0), getattr(e, 'wire_damage_object_id', 0))
            if isinstance(e, CombatEvent) and objkey[0] > 65535 and objkey[1] > 0:
                touched.add(objkey)
            if (_eligible(e) and objkey not in self.seen_objects
                    and not ledger._generation_counters.get(objkey, 0)
                    and objkey not in ledger._origins
                    and objkey not in ledger._last_parent_state_identities
                    and objkey not in ledger._unknown_registration_invalidations
                    and not ledger._parent_state_history_gap_until
                    and not any(k[:2] == objkey for k in ledger._revoked_origin_generations)):
                candidates.append((index, e))
        if self.history_exhausted:
            return
        if len(self.seen_objects) + len(touched - self.seen_objects) > self.max_seen_objects:
            # Dropping old identities must not turn a reused ID into a first
            # observation. Fail closed for this epoch; never grow unbounded.
            self.history_exhausted = True
            self.seen_objects.clear()
            self.pending.clear()
            self.bytes = 0
            self.stats['history_capacity_reached'] += 1
            return
        self.seen_objects.update(touched)
        if not candidates:
            return
        inventory = inspect_udp_damage_records(packet.payload, packet.inferred_direction())
        if not inventory.complete:
            return
        rows = []
        for index, e in candidates:
            hits = [r for r in inventory.damage_records if r.admissible_fields
                    and r.family == 'direct37' and r.physical_key == _physical(e)]
            # Both native slots must be absent; do not override native evidence.
            if (len(hits) == 1 and e.wire_critical == hits[0].critical
                    and all(v in (0, 0xffffffff) for _, v in hits[0].raw_skill_slots)):
                rows.append((index, e, hits[0].wire_record_sha256))
        if not rows or len(key[-1]) > self.max_bytes:
            return
        while self.pending and (len(self.pending) >= self.max_entries
                                or self.bytes + len(key[-1]) > self.max_bytes):
            self._drop(next(iter(self.pending)))
            self.stats['evicted'] += 1
        self.pending[key] = Pending(packet.timestamp_seconds, ledger.evidence_epoch,
                                    decoder._damage_object_packet_serial, tuple(rows))
        self.bytes += len(key[-1])
        self.stats['pending_hits'] += len(rows)
        self.stats['peak_entries'] = max(self.stats['peak_entries'], len(self.pending))
        self.stats['peak_payload_bytes'] = max(self.stats['peak_payload_bytes'], self.bytes)

    def supplement(self, packet, events, decoder):
        ledger = decoder.damage_object_origins
        self._prune(packet.timestamp_seconds, ledger.evidence_epoch)
        key = _key(packet)
        pending = self.pending.get(key)
        if (pending is None or self.history_exhausted
                or ledger._parent_state_history_gap_until
                or packet.inferred_direction() != 'server_to_client'
                or not 0 <= packet.timestamp_seconds-pending.timestamp <= self.retry_seconds):
            return
        for index, original, record_hash in pending.rows:
            if index >= len(events) or events[index] is not original:
                continue
            objkey = (original.wire_source_actor_id, original.wire_damage_object_id)
            origin = ledger._origins.get(objkey)
            if (origin is None or origin.generation != 1
                    or ledger._generation_counters.get(objkey) != 1
                    or origin.registered_at < pending.timestamp
                    or origin.registered_packet_serial <= pending.serial
                    or origin.parent_damage_object_id or origin.requires_root_action_proof
                    or origin.parent_proof_dependencies
                    or origin.action_qualified_subtype or origin.parent_qualified_subtype
                    or objkey in ledger._unknown_registration_invalidations
                    or (*objkey, 1) in ledger._revoked_origin_generations):
                continue
            proof = ledger.attribute_direct37(source_actor_id=original.wire_source_actor_id,
                timestamp=packet.timestamp_seconds, layout=original.wire_damage_layout,
                payload=packet.payload, direction=packet.inferred_direction(),
                damage=original.amount, record_offset=original.wire_record_offset,
                packet_serial=decoder._damage_object_packet_serial+1, _evidence_only=True)
            if (proof is None or not proof.deterministically_validated
                    or not (proof.client_act_validated or proof.packet_validated)
                    or proof.skill_id not in decoder.skill_catalog
                    or decoder._action_conflicts_with_confirmed_job(proof.owner_actor_id, proof.skill_id)):
                continue
            provenance = ':'.join(('duplicate-object', str(pending.epoch),
                sha256(packet.payload).hexdigest(), record_hash, str(origin.generation),
                str(origin.registered_packet_serial), str(origin.registered_record_offset)))
            events[index] = replace(original, skill_id=str(proof.skill_id),
                skill_name=decoder.skill_catalog[proof.skill_id][0],
                attribution_method='damage_object_projectile_link',
                cast_instance_id=provenance, attribution_confidence=1.0)
            self.stats['confirmed_hits'] += 1
        # A retry is not a refresh; further copies cannot extend the opportunity.
        if all(events[i] is not e for i, e, _ in pending.rows):
            self._drop(key)
