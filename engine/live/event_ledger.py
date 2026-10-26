from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2b

from .protocol import PacketRecord


_DEFAULT_MAX_EVENTS = 262_144
_DEFAULT_LEGACY_WINDOW_SECONDS = 3.0
# A game accelerator can expose the same datagram on both its localhost relay
# and the physical/tunnel adapter.  Npcap timestamps those mirror copies only
# a fraction of a millisecond apart.  Keep this deliberately narrow so two
# genuine protocol messages on independent connections are never collapsed.
_MIRROR_COPY_WINDOW_SECONDS = 0.005


def v576_wire_event_id(payload: bytes) -> int | None:
    """Return the reliable-UDP message identity carried before the signature.

    In v576 the first five bytes are not body-XOR encrypted. Retransmissions
    preserve them, while a new logical message receives a new value even when
    actor, skill, damage and critical state are identical.
    """
    if len(payload) < 9 or payload[:5] == b"\x00\x00\x00\x00\x00":
        return None
    return int.from_bytes(payload[:5], "little")


@dataclass(frozen=True, slots=True)
class EventLedgerStats:
    accepted: int
    event_id_duplicates: int
    fallback_duplicates: int
    mirror_duplicates: int
    tracked_events: int


class V576EventLedger:
    """Bounded O(1) ledger for reliable-UDP logical messages.

    ``event_id`` is the default. ``legacy`` retains the old rolling full-packet
    time-window behavior and is intentionally kept as a rollback switch.
    """

    def __init__(
        self,
        mode: str | None = None,
        *,
        max_events: int = _DEFAULT_MAX_EVENTS,
        legacy_window_seconds: float = _DEFAULT_LEGACY_WINDOW_SECONDS,
        event_id_retry_window_seconds: float = _DEFAULT_LEGACY_WINDOW_SECONDS,
    ) -> None:
        configured = (
            mode
            if mode is not None
            else os.environ.get("DRAGON_NEST_DPS_DEDUP_MODE", "event_id")
        )
        self.mode = "legacy" if str(configured).strip().lower() == "legacy" else "event_id"
        self.max_events = max(1_024, int(max_events))
        self.legacy_window_seconds = max(0.01, float(legacy_window_seconds))
        self.event_id_retry_window_seconds = max(
            0.01, float(event_id_retry_window_seconds)
        )
        self._connections: dict[tuple[object, ...], int] = {}
        self._event_ids: OrderedDict[
            tuple[int, int], tuple[float, bytes]
        ] = OrderedDict()
        self._mirror_event_ids: OrderedDict[
            tuple[str, int, bytes],
            tuple[float, tuple[object, ...], str | None],
        ] = OrderedDict()
        self._fallback_seen_at: dict[tuple[object, ...], float] = {}
        self._accepted = 0
        self._event_id_duplicates = 0
        self._fallback_duplicates = 0
        self._mirror_duplicates = 0

    @staticmethod
    def _connection_key(packet: PacketRecord) -> tuple[object, ...]:
        return (
            packet.transport,
            packet.source_ip,
            packet.destination_ip,
            packet.source_port,
            packet.destination_port,
        )

    def _connection_id(self, packet: PacketRecord) -> int:
        key = self._connection_key(packet)
        connection_id = self._connections.get(key)
        if connection_id is None:
            connection_id = len(self._connections) + 1
            self._connections[key] = connection_id
        return connection_id

    def _accept_event_id(self, packet: PacketRecord) -> bool | None:
        wire_id = v576_wire_event_id(packet.payload)
        if wire_id is None:
            return None
        timestamp = packet.timestamp_seconds
        fingerprint = blake2b(packet.payload, digest_size=8).digest()
        connection_key = self._connection_key(packet)
        direction = packet.inferred_direction()
        mirror_key = (str(packet.transport).upper(), wire_id, fingerprint)
        previous_mirror = self._mirror_event_ids.get(mirror_key)
        if previous_mirror is not None:
            previous_timestamp, previous_connection, previous_direction = (
                previous_mirror
            )
            directions_compatible = (
                direction is None
                or previous_direction is None
                or direction == previous_direction
            )
            if (
                connection_key != previous_connection
                and directions_compatible
                # Capture threads from two adapters enqueue independently, so
                # the later-written mirror can carry a slightly earlier Npcap
                # timestamp.  Absolute proximity is the reliable property.
                and abs(timestamp - previous_timestamp)
                <= _MIRROR_COPY_WINDOW_SECONDS
            ):
                # Seed this mirrored connection as well.  If only this side
                # later exposes a reliable-UDP retry, the ordinary per-flow
                # ledger must still remember that the logical message was
                # already consumed.
                connection_event_key = (
                    self._connection_id(packet),
                    wire_id,
                )
                self._event_ids[connection_event_key] = (
                    timestamp,
                    fingerprint,
                )
                self._event_ids.move_to_end(connection_event_key)
                while len(self._event_ids) > self.max_events:
                    self._event_ids.popitem(last=False)
                self._mirror_duplicates += 1
                return False
        self._mirror_event_ids[mirror_key] = (
            timestamp,
            connection_key,
            direction,
        )
        self._mirror_event_ids.move_to_end(mirror_key)
        while len(self._mirror_event_ids) > self.max_events:
            self._mirror_event_ids.popitem(last=False)

        key = (self._connection_id(packet), wire_id)
        previous = self._event_ids.get(key)
        if (
            previous is not None
            and previous[1] == fingerprint
            and 0.0 <= timestamp - previous[0] <= self.event_id_retry_window_seconds
        ):
            # Refresh the retry window so a rolling retransmission burst does
            # not leak through. Wire IDs are reused during long fights, so the
            # ID alone cannot be kept as a permanent session-wide tombstone.
            self._event_ids[key] = (timestamp, fingerprint)
            self._event_ids.move_to_end(key)
            self._event_id_duplicates += 1
            return False
        self._event_ids[key] = (timestamp, fingerprint)
        self._event_ids.move_to_end(key)
        while len(self._event_ids) > self.max_events:
            self._event_ids.popitem(last=False)
        self._accepted += 1
        return True

    def _accept_legacy(self, packet: PacketRecord) -> bool:
        key = self._connection_key(packet) + (packet.payload,)
        previous = self._fallback_seen_at.get(key)
        timestamp = packet.timestamp_seconds
        if (
            previous is not None
            and 0.0 <= timestamp - previous <= self.legacy_window_seconds
        ):
            # Refresh on every retry so a rolling burst cannot leak through.
            self._fallback_seen_at[key] = timestamp
            self._fallback_duplicates += 1
            return False
        self._fallback_seen_at[key] = timestamp
        if len(self._fallback_seen_at) > 5_000:
            cutoff = timestamp - self.legacy_window_seconds
            self._fallback_seen_at = {
                item: seen_at
                for item, seen_at in self._fallback_seen_at.items()
                if seen_at >= cutoff
            }
        self._accepted += 1
        return True

    def accept(self, packet: PacketRecord) -> bool:
        if self.mode == "event_id":
            decision = self._accept_event_id(packet)
            if decision is not None:
                return decision
        return self._accept_legacy(packet)

    @property
    def stats(self) -> EventLedgerStats:
        return EventLedgerStats(
            accepted=self._accepted,
            event_id_duplicates=self._event_id_duplicates,
            fallback_duplicates=self._fallback_duplicates,
            mirror_duplicates=self._mirror_duplicates,
            tracked_events=len(self._event_ids),
        )
