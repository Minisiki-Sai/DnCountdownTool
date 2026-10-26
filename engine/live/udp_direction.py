"""Bounded direction evidence for byte-identical accelerator mirror packets."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import PacketRecord


def has_ambiguous_action_direction(payload: bytes) -> bool:
    # These signatures describe both a client cast (actor at 10) and a server
    # broadcast (observer at 10, caster at 14). Length alone cannot choose.
    return (len(payload), payload[5:9]) in {
        (73, b"\xfe\x5c\x46\x00"),
        (80, b"\x28\x8a\x4d\x00"),
    }


class UdpMirrorDirectionTracker:
    """Learn relay directions from exact, time-adjacent physical copies.

    No IP/port is rewritten. Two different wire messages must corroborate a
    flow before its direction can be reused for nonambiguous records. Shared
    73/80 action layouts always need physical direction or an exact mirror,
    and wait for at most 5ms of capture time; damage, Buff
    and health packets are not held back. All state is bounded and expires.
    """

    mirror_window = 0.005
    flow_ttl = 30.0
    max_recent = 512
    max_flows = 128
    max_pending = 32

    def __init__(self) -> None:
        self._recent: OrderedDict[bytes, PacketRecord] = OrderedDict()
        self._flows: OrderedDict[tuple, tuple[str, float, bytes, int]] = OrderedDict()
        self._pending: OrderedDict[tuple, PacketRecord] = OrderedDict()

    @staticmethod
    def _flow(packet: PacketRecord) -> tuple:
        return (packet.source_ip, packet.source_port,
                packet.destination_ip, packet.destination_port)

    def _learn(self, packet: PacketRecord, direction: str, timestamp: float) -> None:
        key = self._flow(packet)
        previous = self._flows.get(key)
        token = packet.payload[:5]
        if previous is None or abs(timestamp - previous[1]) > self.flow_ttl:
            evidence = (direction, timestamp, token, 1)
        elif previous[0] != direction:
            # A multiplexed or repurposed relay must not be assigned whichever
            # direction happened to win last. Exact per-packet matches still work.
            evidence = ("", max(timestamp, previous[1]), token, 0)
        else:
            evidence = (direction, max(timestamp, previous[1]), token,
                        min(2, previous[3] + int(token != previous[2])))
        self._flows[key] = evidence
        self._flows.move_to_end(key)
        while len(self._flows) > self.max_flows:
            self._flows.popitem(last=False)

    def _direction(self, packet: PacketRecord) -> str | None:
        if has_ambiguous_action_direction(packet.payload):
            # A multiplexed relay can change direction between messages.
            # Choosing the actor-at-10/actor-at-14 action layout from a flow
            # vote would manufacture a root action for the observer/caster.
            return None
        evidence = self._flows.get(self._flow(packet))
        if (evidence and evidence[0] and evidence[3] >= 2
                and abs(packet.timestamp_seconds - evidence[1]) <= self.flow_ttl):
            return evidence[0]
        return None

    def flush(self, timestamp: float | None = None) -> tuple[PacketRecord, ...]:
        emitted = []
        for key, packet in tuple(self._pending.items()):
            if timestamp is None or timestamp - packet.timestamp_seconds > self.mirror_window:
                emitted.append(packet)
                del self._pending[key]
        return tuple(emitted)

    def feed(self, packet: PacketRecord) -> tuple[PacketRecord, ...]:
        emitted = list(self.flush(packet.timestamp_seconds))
        payload = packet.payload
        if has_ambiguous_action_direction(payload) and packet.direction_hint:
            # A bare hint has no per-message proof provenance. Revalidate
            # physical IPs or an exact recent mirror; never recycle a hint
            # inherited from a flow or an earlier pass through this tracker.
            packet = replace(packet, direction_hint=None)
        direct = packet.inferred_direction()
        # Do not propagate our own hint as independent corroborating evidence.
        trusted = direct if not packet.direction_hint else None
        eligible = 9 <= len(payload) <= 2048 and payload[:5] != b"\0" * 5
        previous = self._recent.get(payload) if eligible else None
        matched = (
            previous is not None
            and self._flow(previous) != self._flow(packet)
            and abs(packet.timestamp_seconds - previous.timestamp_seconds) <= self.mirror_window
        )
        if matched:
            previous_direction = previous.inferred_direction()
            if trusted and previous_direction is None:
                self._learn(previous, trusted, packet.timestamp_seconds)
                pending = self._pending.pop((self._flow(previous), payload), None)
                if pending is not None:
                    emitted.append(replace(pending, direction_hint=trusted))
            elif direct is None and previous_direction and not previous.direction_hint:
                self._learn(packet, previous_direction, packet.timestamp_seconds)
                direct = previous_direction

        if eligible:
            # Retain the physical copy when a later mirror arrives, so another
            # relay can match the same independently known direction as well.
            if not matched or trusted or previous.inferred_direction() is None:
                self._recent[payload] = packet
            self._recent.move_to_end(payload)
            while len(self._recent) > self.max_recent:
                self._recent.popitem(last=False)

        if direct is None:
            direct = self._direction(packet)
        if direct and packet.inferred_direction() is None:
            packet = replace(packet, direction_hint=direct)
        if direct is None and has_ambiguous_action_direction(payload):
            key = (self._flow(packet), payload)
            self._pending.setdefault(key, packet)
            while len(self._pending) > self.max_pending:
                emitted.append(self._pending.popitem(last=False)[1])
        else:
            emitted.append(packet)
        return tuple(emitted)
