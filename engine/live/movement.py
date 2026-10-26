from __future__ import annotations

from . import capture_health

import hashlib
import json
import math
import os
import struct
import threading
import zlib
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Callable, Mapping

from .dungeon_map_catalog import (
    is_battle_map,
    load_dungeon_map_catalog,
    resolve_dungeon_map_id,
)
from .boss_hp import load_fixed_boss_hp_profiles
from .movement_map_assets import resolve_movement_map_asset
from .nest_recognizer import canonical_nest_name, resolve_nest_evidence
from .npcap import (
    IDENTITY_METADATA_ADAPTER_INDEX,
    IDENTITY_TRACE_ADAPTER_INDEX,
    PREBATTLE_STATE_ADAPTER_INDEX,
    PROFILE_IDENTITY_ADAPTER_INDEX,
    SESSION_EVIDENCE_ADAPTER_INDEX,
    decode_profile_identity_bindings,
)
from .protocol import (
    PacketRecord,
    V576TransportReassembler,
    decode_captured_frame,
    decode_v576_map_identity,
    decode_v576_monster_spawn,
    decode_v576_movement,
    decode_v576_player_health,
)


_DNDP1_MAGIC = b"DNDP1\x00"
_DNDP1_FRAME_HEADER = struct.Struct("<dBII")
_MAX_CAPTURED_FRAME_BYTES = 16 * 1024 * 1024
_MOVEMENT_SCAN_LOCK = threading.RLock()
MOVEMENT_SCAN_VERSION = 7
_MOVEMENT_CACHE_SCHEMA = "dragon-nest-movement-cache-v1"
_MOVEMENT_CACHE_SUFFIX = ".movementcache"
_MOVEMENT_CACHE_MAX_ENTRIES = 64


# One profile may contain more than one historical route.  Keep those routes
# separate: a terminal-map candidate can select a branch, but must never make
# the older and newer layouts look like one five-room dungeon.
_NEST_ROUTE_VARIANTS: dict[str, tuple[tuple[int, ...], ...]] = {
    "黑龙经典": ((14501, 14502, 14503, 14504),),
    "黑龙硬核": ((17612,),),
    "沙龙经典": ((14221, 14222, 14223),),
    "沙龙硬核": ((14217, 14218, 14219), (17610, 17611)),
    "守卫者巢穴": ((14300, 14301),),
    "阿努阿兰德的试炼巢穴": ((14410, 14411, 14412),),
    "绿龙经典": ((14093, 14094, 14095),),
    "绿龙硬核": ((14090, 14091, 14092), (17608,)),
    "台风金巢穴": ((14100,),),
    "银色新月修炼场": ((881, 882),),
}


# Boss template families come from the unpacked ``monstertable_nest.dnt``
# CustomAI Gate names. A physical map may host several logical Gates, so this
# table maps template evidence to real map IDs instead of assuming that every
# boss starts a new room.
_NEST_ROOM_TEMPLATE_IDS: dict[str, dict[int, frozenset[int]]] = {
    "黑龙经典": {
        14501: frozenset({503611, 503614, 503616, 503617, 503619}),
        14502: frozenset({503621, 503622, 503627, 503628, 503630}),
        14503: frozenset({503601, 503629, 503635, 503639, 503652, 503661}),
        14504: frozenset({503681, 503682, 503683, 503696}),
    },
    "沙龙经典": {
        14221: frozenset({503001, 503002, 503003, 503005, 503007, 503011, 503012}),
        14222: frozenset({
            503015, 503016, 503018, 503019, 503020, 503021, 503022,
            503023, 503031, 503041, 503051, 503054, 503055,
        }),
        14223: frozenset({503061, 503062, 503063, 503064}),
    },
    "沙龙硬核": {
        17610: frozenset({
            4150054, 4150055, 4150056, 4150057, 4150058, 4150059,
            4150060, 4150061, 4150062, 4150063, 4150064, 4150065,
            4150066, 4150067, 4150068, 4150069, 4150070, 4150071,
            4150072,
        }),
        17611: frozenset({4150050, 4150051, 4150052, 4160001}),
    },
    "守卫者巢穴": {
        14300: frozenset({503101, 503121, 503131}),
        14301: frozenset({503141, 503151}),
    },
    "阿努阿兰德的试炼巢穴": {
        14410: frozenset({
            503510, 503511, 503512, 503513, 503516, 503517, 503518,
            503519, 503521, 503522, 503523, 503524, 503562, 503563,
            503564,
        }),
        14411: frozenset({503531, 503532, 503533}),
        14412: frozenset({503551, 503552, 503553, 503554, 503555, 503561}),
    },
    "绿龙经典": {
        14093: frozenset({236001, 236010, 236028}),
        14094: frozenset({236012, 236015, 236027, 236040, 236047}),
        14095: frozenset({236018, 236019, 236020, 236048}),
    },
    "绿龙硬核": {
        14091: frozenset({237051, 237052, 237053, 237054, 237055, 237062}),
        14092: frozenset({237056, 237057}),
        17608: frozenset({
            237051, 237052, 237053, 237054, 237055, 237056, 237057,
            237062,
        }),
    },
}


# These entry templates uniquely identify the current short-route revisions.
# They are only used after the packet resolved the nest with entry-template
# confidence; an ordinary later boss sighting cannot select a route branch.
_NEST_ENTRY_TEMPLATE_ROUTE_MAP: dict[tuple[str, int], int] = {
    ("绿龙硬核", 237056): 17608,
    **{
        ("沙龙硬核", template_id): 17610
        for template_id in (4150064, 4150067, 4150068, 4150069, 4150070)
    },
}


@dataclass(frozen=True, slots=True)
class MovementPoint:
    timestamp_ms: int
    actor_id: str
    x: float
    y: float
    floor_value: float
    local_actor: bool
    packet_signature: str
    source_layout: str


@dataclass(frozen=True, slots=True)
class MovementCapture:
    duration_seconds: float
    points: tuple[MovementPoint, ...]
    map_history: tuple[dict[str, object], ...]
    diagnostics: dict[str, object]


class MovementLedger:
    """Independent, append-only ledger for verified X/Y actor positions.

    The combat decoder never consumes this ledger. This separation is a hard
    invariant: adding or refining a movement layout cannot alter damage,
    healing, Buff state, timelines, or team scores.
    """

    def __init__(self) -> None:
        self._points: list[MovementPoint] = []
        self._last_by_actor: dict[str, MovementPoint] = {}
        self._last_index_by_actor: dict[str, int] = {}
        self._map_candidates: list[dict[str, object]] = []
        self._monster_spawn_events: list[dict[str, object]] = []
        self._session_resource_events: list[dict[str, object]] = []
        self._profile_identity_bindings: list[dict[str, object]] = []
        self._layout_counts: Counter[str] = Counter()
        self.decoded_samples = 0
        self.duplicate_samples = 0
        self.replaced_samples = 0

    @staticmethod
    def _actor_key(actor_id: int) -> str:
        return f"0x{int(actor_id) & 0xFFFFFFFF:08X}"

    @staticmethod
    def _source_priority(point: MovementPoint) -> int:
        layout = point.source_layout.casefold()
        if "local-position-ac4e" in layout:
            return 50
        if "movement-0962" in layout:
            return 40 if point.local_actor else 20
        if "party-position-0120" in layout:
            return 45 if not point.local_actor else 30
        if "entity-position-" in layout:
            return 35
        return 10

    def observe(self, packet: PacketRecord, origin_seconds: float) -> int:
        if str(packet.transport).upper() != "UDP":
            return 0
        direction = packet.inferred_direction()
        monster_spawn = decode_v576_monster_spawn(packet.payload, direction)
        if monster_spawn is not None:
            self._monster_spawn_events.append(
                {
                    "actor_id": self._actor_key(monster_spawn.actor_id),
                    "template_id": int(monster_spawn.template_id),
                    "observed_at": max(
                        0.0,
                        float(packet.timestamp_seconds) - float(origin_seconds),
                    ),
                    "layout": "s2c-udp-monster-spawn-062755-v576",
                }
            )
        map_identity = decode_v576_map_identity(
            packet.payload,
            direction,
            connection_context=packet,
        )
        if map_identity is not None and not bool(map_identity.authoritative):
            entry = resolve_dungeon_map_id(map_identity.map_id)
            if is_battle_map(entry):
                observed_at = max(
                    0.0,
                    float(packet.timestamp_seconds) - float(origin_seconds),
                )
                candidate = {
                    "map_id": int(map_identity.map_id),
                    "map_name": str(entry.display_name or ""),
                    "nest_name": canonical_nest_name(entry.display_name),
                    "observed_at": observed_at,
                    "layout": map_identity.layout,
                    "authoritative": False,
                }
                if not (
                    self._map_candidates
                    and int(self._map_candidates[-1].get("map_id") or 0)
                    == int(map_identity.map_id)
                    and abs(
                        float(self._map_candidates[-1].get("observed_at") or 0.0)
                        - observed_at
                    )
                    < 0.25
                ):
                    self._map_candidates.append(candidate)
        decoded_rows = decode_v576_movement(packet.payload, direction)
        if not decoded_rows:
            return 0
        timestamp_ms = max(
            0,
            round((float(packet.timestamp_seconds) - float(origin_seconds)) * 1000),
        )
        accepted = 0
        for row in decoded_rows:
            self.decoded_samples += 1
            actor_id = self._actor_key(row.actor_id)
            point = MovementPoint(
                timestamp_ms=timestamp_ms,
                actor_id=actor_id,
                x=row.x,
                y=row.y,
                floor_value=row.vertical,
                local_actor=direction == "client_to_server",
                packet_signature=row.packet_signature,
                source_layout=row.layout,
            )
            previous = self._last_by_actor.get(actor_id)
            if previous is not None:
                same_position = (
                    abs(previous.x - point.x) < 1e-9
                    and abs(previous.y - point.y) < 1e-9
                    and abs(previous.floor_value - point.floor_value) < 1e-9
                )
                # Mirrored Npcap frames and repeated movement blocks can carry
                # the same sample a few milliseconds apart. Keep one-second
                # stationary heartbeats so playback still knows the actor is
                # present without retaining every transport duplicate.
                if same_position and timestamp_ms - previous.timestamp_ms < 1000:
                    self.duplicate_samples += 1
                    continue
                if (
                    timestamp_ms == previous.timestamp_ms
                    and math.hypot(previous.x - point.x, previous.y - point.y) < 0.01
                ):
                    self.duplicate_samples += 1
                    continue
                # Several packet families can report the same actor within one
                # render tick. Keep the authoritative stream instead of mixing
                # local, party and snapshot coordinates into a false zig-zag.
                if abs(timestamp_ms - previous.timestamp_ms) <= 75:
                    previous_priority = self._source_priority(previous)
                    current_priority = self._source_priority(point)
                    if current_priority < previous_priority:
                        self.duplicate_samples += 1
                        continue
                    if current_priority > previous_priority:
                        previous_index = self._last_index_by_actor.get(actor_id)
                        if previous_index is not None:
                            self._points[previous_index] = point
                            self._last_by_actor[actor_id] = point
                            previous_direction = (
                                "client_to_server"
                                if previous.local_actor
                                else "server_to_client"
                            )
                            previous_layout_key = (
                                f"{previous.source_layout}:"
                                f"{previous.packet_signature}:{previous_direction}"
                            )
                            self._layout_counts[previous_layout_key] -= 1
                            if self._layout_counts[previous_layout_key] <= 0:
                                del self._layout_counts[previous_layout_key]
                            self._layout_counts[
                                f"{row.layout}:{row.packet_signature}:"
                                f"{direction or 'unknown'}"
                            ] += 1
                            self.replaced_samples += 1
                            accepted += 1
                            continue
            self._points.append(point)
            self._last_by_actor[actor_id] = point
            self._last_index_by_actor[actor_id] = len(self._points) - 1
            self._layout_counts[
                f"{row.layout}:{row.packet_signature}:{direction or 'unknown'}"
            ] += 1
            accepted += 1
        return accepted

    def observe_session_evidence(
        self,
        packet: PacketRecord,
        origin_seconds: float,
    ) -> int:
        """Retain exact player resource fingerprints for reconnect aliases.

        Adapter 253 contains already-verified 21 0C packets.  It is outside
        the combat boundary, but the actor/max-HP/max-MP tuple is exactly the
        evidence needed to join a party-position ID after reconnect.
        """

        accepted = 0
        for row in decode_v576_player_health(
            packet.payload,
            packet.inferred_direction(),
        ):
            if (
                row.layout != "s2c-udp-player-resource-sync-210c-v576"
                or int(row.actor_id) < 0x10000
                or int(row.current_hp) <= 0
                or int(row.current_mp) <= 0
            ):
                continue
            self._session_resource_events.append(
                {
                    "actor_id": self._actor_key(row.actor_id),
                    "max_hp": int(row.current_hp),
                    "max_mp": int(row.current_mp),
                    "observed_at": max(
                        0.0,
                        float(packet.timestamp_seconds) - float(origin_seconds),
                    ),
                    "layout": row.layout,
                }
            )
            accepted += 1
        return accepted

    def observe_profile_identity_bindings(self, payload: bytes) -> int:
        rows = decode_profile_identity_bindings(payload)
        return self.observe_profile_identity_rows(rows)

    def observe_profile_identity_rows(self, rows: object) -> int:
        if not isinstance(rows, (tuple, list)):
            return 0
        accepted = 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            self._profile_identity_bindings.append(dict(row))
            accepted += 1
        return accepted

    def snapshot(
        self,
        *,
        duration_seconds: float,
        map_history: tuple[dict[str, object], ...],
        diagnostics: Mapping[str, object] | None = None,
    ) -> MovementCapture:
        details = dict(diagnostics or {})
        runtime_aliases, alias_evidence = _movement_runtime_aliases(
            tuple(self._points),
            tuple(self._session_resource_events),
            tuple(self._profile_identity_bindings),
        )
        details.update(
            {
                "decoded_samples": self.decoded_samples,
                "accepted_samples": len(self._points),
                "duplicate_samples": self.duplicate_samples,
                "replaced_samples": self.replaced_samples,
                "layouts": dict(sorted(self._layout_counts.items())),
                "map_candidates": tuple(dict(row) for row in self._map_candidates),
                "monster_spawn_events": tuple(
                    dict(row) for row in self._monster_spawn_events
                ),
                "session_resource_events": tuple(
                    dict(row) for row in self._session_resource_events
                ),
                "profile_identity_bindings": tuple(
                    dict(row) for row in self._profile_identity_bindings
                ),
                "runtime_aliases": {
                    **runtime_aliases,
                    **(
                        details.get("runtime_aliases", {})
                        if isinstance(details.get("runtime_aliases"), Mapping)
                        else {}
                    ),
                },
                "runtime_alias_evidence": tuple(alias_evidence) + tuple(
                    dict(row)
                    for row in details.get("runtime_alias_evidence", ())
                    if isinstance(row, dict)
                ),
                "scan_version": MOVEMENT_SCAN_VERSION,
            }
        )
        return MovementCapture(
            duration_seconds=max(0.0, float(duration_seconds)),
            points=tuple(sorted(self._points, key=lambda row: row.timestamp_ms)),
            map_history=map_history,
            diagnostics=details,
        )


def _movement_runtime_aliases(
    points: tuple[MovementPoint, ...],
    resource_events: tuple[dict[str, object], ...],
    profile_bindings: tuple[dict[str, object], ...],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Derive only aliases backed by stable profile or exact resource facts."""

    ranges: dict[str, tuple[float, float]] = {}
    for point in points:
        seconds = point.timestamp_ms / 1000.0
        previous = ranges.get(point.actor_id)
        ranges[point.actor_id] = (
            min(previous[0], seconds) if previous else seconds,
            max(previous[1], seconds) if previous else seconds,
        )

    aliases: dict[str, str] = {}
    evidence: list[dict[str, object]] = []

    # A profile id is stable across reconnects. Prefer the runtime id whose
    # coordinate stream starts first as the canonical display track.
    by_profile: dict[int, set[str]] = defaultdict(set)
    for row in profile_bindings:
        try:
            runtime_id = int(row.get("runtime_actor_id") or 0)
            profile_id = int(row.get("profile_id") or 0)
        except (TypeError, ValueError):
            continue
        if runtime_id >= 0x10000 and profile_id > 0:
            by_profile[profile_id].add(f"0x{runtime_id & 0xFFFFFFFF:08X}")
    for profile_id, actor_ids in by_profile.items():
        tracked = [actor_id for actor_id in actor_ids if actor_id in ranges]
        if len(tracked) < 2:
            continue
        canonical = min(tracked, key=lambda actor_id: ranges[actor_id][0])
        for actor_id in tracked:
            if actor_id == canonical:
                continue
            aliases[actor_id] = canonical
            evidence.append(
                {
                    "new_actor_id": actor_id,
                    "canonical_actor_id": canonical,
                    "reason": "matching_stable_profile_id",
                    "profile_id": f"0x{profile_id & 0xFFFFFFFF:08X}",
                }
            )

    # Older captures have no adapter-251 appendix. Their adapter-253 resource
    # evidence still proves a reconnect when the exact HP/MP fingerprint moves
    # from one non-overlapping position stream to exactly one later stream.
    by_signature: dict[tuple[int, int], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in resource_events:
        try:
            actor_id = _canonical_runtime_actor_id(str(row.get("actor_id") or ""), {})
            signature = (int(row.get("max_hp") or 0), int(row.get("max_mp") or 0))
            observed_at = max(0.0, float(row.get("observed_at") or 0.0))
        except (TypeError, ValueError):
            continue
        if actor_id in ranges and signature[0] > 0 and signature[1] > 0:
            by_signature[signature][actor_id].append(observed_at)

    for signature, observations in by_signature.items():
        actor_ids = sorted(
            observations,
            key=lambda actor_id: min(observations[actor_id]),
        )
        if len(actor_ids) != 2:
            continue
        old_actor_id, new_actor_id = actor_ids
        old_range = ranges[old_actor_id]
        new_range = ranges[new_actor_id]
        resource_gap = (
            min(observations[new_actor_id]) - max(observations[old_actor_id])
        )
        # Do not collapse two simultaneously present players who happen to
        # share panel resources. A reconnect stream must replace, not overlap,
        # the old coordinate stream and must be separated by a real absence.
        if (
            resource_gap < 10.0
            or old_range[1] > new_range[0] + 1.0
            or new_actor_id in aliases
        ):
            continue
        aliases[new_actor_id] = aliases.get(old_actor_id, old_actor_id)
        evidence.append(
            {
                "new_actor_id": new_actor_id,
                "canonical_actor_id": aliases[new_actor_id],
                "timestamp": round(min(observations[new_actor_id]), 3),
                "reason": "matching_210c_resource_signature_nonoverlap",
                "resource_signature": signature,
                "old_track_last": round(old_range[1], 3),
                "new_track_first": round(new_range[0], 3),
            }
        )

    return aliases, evidence


def _map_history_row(packet: PacketRecord, origin_seconds: float) -> dict[str, object] | None:
    decoded = decode_v576_map_identity(
        packet.payload,
        packet.inferred_direction(),
        connection_context=packet,
    )
    if decoded is None:
        return None
    # Client-originated map rows describe a selected/current route candidate,
    # not an authoritative scene-entry boundary.  Treating one as a new room
    # used to discard every coordinate before that late packet and could put
    # the remaining path on the wrong minimap.
    if not bool(getattr(decoded, "authoritative", True)):
        return None
    entry = resolve_dungeon_map_id(decoded.map_id)
    if entry is None or not entry.display_name:
        return None
    return {
        "map_id": decoded.map_id,
        "map_name": entry.display_name,
        "nest_name": (
            canonical_nest_name(entry.display_name)
            if is_battle_map(entry)
            else ""
        ),
        "official_name": entry.official_name,
        "map_type": entry.map_type,
        "content_kind": entry.content_kind,
        "observed_at": max(
            0.0,
            float(packet.timestamp_seconds) - float(origin_seconds),
        ),
        "layout": decoded.layout,
        "confidence": "exact_map_id",
        "inferred": False,
        "active": bool(decoded.active),
    }


def _merge_map_history_row(
    history: list[dict[str, object]],
    row: dict[str, object],
) -> None:
    if bool(row.pop("active", True)):
        if (
            history
            and int(history[-1].get("map_id") or 0)
            == int(row.get("map_id") or 0)
            and history[-1].get("left_at") is None
        ):
            return
        history.append(row)
        return
    observed_at = max(0.0, float(row.get("observed_at") or 0.0))
    matching_row = next(
        (
            existing
            for existing in reversed(history)
            if int(existing.get("map_id") or 0) == int(row.get("map_id") or 0)
            and existing.get("left_at") is None
        ),
        None,
    )
    if matching_row is None:
        row["observed_at"] = 0.0 if not history else observed_at
        row["entry_inferred_from_exit"] = True
        history.append(row)
        matching_row = row
    matching_row["left_at"] = observed_at
    matching_row["exit_layout"] = row.get("layout")


def scan_v576_diagnostic_movement(
    path: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> MovementCapture:
    """Scan one DNDP1 capture without invoking the combat event decoder."""

    capture_path = Path(path)
    total_bytes = max(1, capture_path.stat().st_size)
    transport = V576TransportReassembler()
    ledger = MovementLedger()
    history: list[dict[str, object]] = []
    origin: float | None = None
    last_timestamp: float | None = None
    raw_frames = 0
    logical_packets = 0
    movement_packets = 0
    next_progress = 0
    progress_step = max(256 * 1024, total_bytes // 200)

    with capture_path.open("rb") as handle:
        if handle.read(len(_DNDP1_MAGIC)) != _DNDP1_MAGIC:
            raise ValueError("不是有效的 Dragon Nest .dpscap 统计记录")
        while True:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("Movement loading was cancelled")
            header = handle.read(_DNDP1_FRAME_HEADER.size)
            if not header:
                break
            if len(header) != _DNDP1_FRAME_HEADER.size:
                break
            timestamp, adapter, original_length, captured_length = (
                _DNDP1_FRAME_HEADER.unpack(header)
            )
            if captured_length > _MAX_CAPTURED_FRAME_BYTES:
                raise ValueError(".dpscap 包长度异常，文件可能已损坏")
            frame = handle.read(captured_length)
            if len(frame) != captured_length:
                break
            raw_frames += 1
            if adapter == PROFILE_IDENTITY_ADAPTER_INDEX:
                ledger.observe_profile_identity_bindings(frame)
                continue
            if adapter == SESSION_EVIDENCE_ADAPTER_INDEX:
                packet = decode_captured_frame(timestamp, frame, original_length)
                if packet is not None:
                    ledger.observe_session_evidence(
                        packet,
                        origin if origin is not None else float(timestamp),
                    )
                continue
            if adapter in {
                capture_health.ADAPTER_INDEX,
                IDENTITY_TRACE_ADAPTER_INDEX,
                IDENTITY_METADATA_ADAPTER_INDEX,
                PREBATTLE_STATE_ADAPTER_INDEX,
            }:
                continue
            if origin is None:
                origin = float(timestamp)
            last_timestamp = float(timestamp)
            packet = decode_captured_frame(timestamp, frame, original_length)
            if packet is None:
                continue
            for logical_packet in transport.feed(packet):
                logical_packets += 1
                row = _map_history_row(logical_packet, origin)
                if row is not None:
                    _merge_map_history_row(history, row)
                if ledger.observe(logical_packet, origin):
                    movement_packets += 1
            position = handle.tell()
            if progress_callback is not None and position >= next_progress:
                progress_callback(position, total_bytes)
                next_progress = position + progress_step

    # TCP map confirmations can be held until the final reassembly flush. UDP
    # movement itself is normally emitted immediately.
    if origin is not None:
        for logical_packet in transport.flush():
            logical_packets += 1
            row = _map_history_row(logical_packet, origin)
            if row is not None:
                _merge_map_history_row(history, row)
            if ledger.observe(logical_packet, origin):
                movement_packets += 1
    if progress_callback is not None:
        progress_callback(total_bytes, total_bytes)
    duration_seconds = (
        max(0.0, float(last_timestamp) - float(origin))
        if origin is not None and last_timestamp is not None
        else 0.0
    )
    return ledger.snapshot(
        duration_seconds=duration_seconds,
        map_history=tuple(history),
        diagnostics={
            "raw_frames": raw_frames,
            "logical_packets": logical_packets,
            "movement_packets": movement_packets,
        },
    )


def default_movement_cache_directory() -> Path:
    configured = os.environ.get("DRAGON_NEST_MOVEMENT_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "movement_cache"
    )


def _movement_cache_token(
    path_text: str,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> str:
    stable = str(source_hash or "").strip().casefold()
    if stable:
        return hashlib.sha256(f"capture:{stable}".encode("utf-8")).hexdigest()
    metadata = f"{Path(path_text).resolve()}\0{file_size}\0{modified_ns}"
    return hashlib.sha256(metadata.encode("utf-8", errors="surrogatepass")).hexdigest()


def _movement_cache_path(token: str) -> Path:
    return default_movement_cache_directory() / f"{token}{_MOVEMENT_CACHE_SUFFIX}"


def _movement_capture_to_cache(
    capture: MovementCapture,
    *,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> dict[str, object]:
    return {
        "schema": _MOVEMENT_CACHE_SCHEMA,
        "scanVersion": MOVEMENT_SCAN_VERSION,
        "sourceHash": source_hash,
        "fileSize": file_size,
        "modifiedNs": modified_ns,
        "durationSeconds": capture.duration_seconds,
        "points": [
            [
                row.timestamp_ms,
                row.actor_id,
                row.x,
                row.y,
                row.floor_value,
                row.local_actor,
                row.packet_signature,
                row.source_layout,
            ]
            for row in capture.points
        ],
        "mapHistory": list(capture.map_history),
        "diagnostics": capture.diagnostics,
    }


def _movement_capture_from_cache(
    raw: object,
    *,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> MovementCapture | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != _MOVEMENT_CACHE_SCHEMA:
        return None
    if int(raw.get("scanVersion") or 0) != MOVEMENT_SCAN_VERSION:
        return None
    if int(raw.get("fileSize") or -1) != file_size:
        return None
    cached_hash = str(raw.get("sourceHash") or "").strip().casefold()
    if source_hash and cached_hash != source_hash.strip().casefold():
        return None
    if not source_hash and int(raw.get("modifiedNs") or -1) != modified_ns:
        return None
    try:
        points = tuple(
            MovementPoint(
                timestamp_ms=int(row[0]),
                actor_id=str(row[1]),
                x=float(row[2]),
                y=float(row[3]),
                floor_value=float(row[4]),
                local_actor=bool(row[5]),
                packet_signature=str(row[6]),
                source_layout=str(row[7]),
            )
            for row in raw.get("points", ())
            if isinstance(row, (list, tuple)) and len(row) >= 8
        )
        map_history = tuple(
            dict(row)
            for row in raw.get("mapHistory", ())
            if isinstance(row, dict)
        )
        diagnostics = dict(raw.get("diagnostics") or {})
    except (TypeError, ValueError, OverflowError):
        return None
    diagnostics["persistent_cache_hit"] = True
    return MovementCapture(
        duration_seconds=max(0.0, float(raw.get("durationSeconds") or 0.0)),
        points=points,
        map_history=map_history,
        diagnostics=diagnostics,
    )


def _load_persistent_movement_cache(
    token: str,
    *,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> MovementCapture | None:
    path = _movement_cache_path(token)
    try:
        raw = json.loads(zlib.decompress(path.read_bytes()))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, zlib.error):
        return None
    capture = _movement_capture_from_cache(
        raw,
        file_size=file_size,
        modified_ns=modified_ns,
        source_hash=source_hash,
    )
    if capture is not None:
        try:
            path.touch()
        except OSError:
            pass
    return capture


def _save_persistent_movement_cache(
    token: str,
    capture: MovementCapture,
    *,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> None:
    destination = _movement_cache_path(token)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _movement_capture_to_cache(
        capture,
        file_size=file_size,
        modified_ns=modified_ns,
        source_hash=source_hash,
    )
    encoded = zlib.compress(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: list(value) if isinstance(value, (set, tuple)) else str(value),
        ).encode("utf-8"),
        level=3,
    )
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_bytes(encoded)
        temporary.replace(destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        entries = sorted(
            destination.parent.glob(f"*{_MOVEMENT_CACHE_SUFFIX}"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    for stale in entries[_MOVEMENT_CACHE_MAX_ENTRIES:]:
        try:
            stale.unlink()
        except OSError:
            pass


@lru_cache(maxsize=8)
def _cached_movement_capture(
    path_text: str,
    file_size: int,
    modified_ns: int,
    source_hash: str,
) -> tuple[MovementCapture, bool]:
    token = _movement_cache_token(path_text, file_size, modified_ns, source_hash)
    persisted = _load_persistent_movement_cache(
        token,
        file_size=file_size,
        modified_ns=modified_ns,
        source_hash=source_hash,
    )
    if persisted is not None:
        return persisted, True
    result = scan_v576_diagnostic_movement(Path(path_text))
    try:
        _save_persistent_movement_cache(
            token,
            result,
            file_size=file_size,
            modified_ns=modified_ns,
            source_hash=source_hash,
        )
    except OSError:
        pass
    return result, False


def cached_v576_diagnostic_movement(
    path: Path,
    *,
    source_hash: str = "",
) -> tuple[MovementCapture, bool]:
    capture_path = Path(path).expanduser().resolve()
    stat = capture_path.stat()
    # functools.lru_cache does not coalesce concurrent misses. A reopened
    # trajectory tab could otherwise start another full scan of the same
    # 100-300 MB capture while the first request is still running.
    with _MOVEMENT_SCAN_LOCK:
        before = _cached_movement_capture.cache_info().hits
        result, persistent_hit = _cached_movement_capture(
            str(capture_path),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            str(source_hash or "").strip().casefold(),
        )
        memory_hit = _cached_movement_capture.cache_info().hits > before
        return result, memory_hit or persistent_hit


def _map_history_merge_key(row: Mapping[str, object]) -> tuple[object, ...]:
    """Return the identity of one map-history observation.

    Real map IDs are globally stable and can be merged directly. ``map_id=0``
    is different: it is used both for an inferred dungeon entry and for the
    synthetic town-return boundary. Treating both as the same map used to let
    the later return row overwrite the dungeon's entry-template evidence.
    """

    try:
        map_id = max(0, int(row.get("map_id") or 0))
    except (TypeError, ValueError):
        map_id = 0
    if map_id:
        return ("map", map_id)

    content_kind = str(row.get("content_kind") or "").strip().casefold()
    try:
        map_type = max(0, int(row.get("map_type") or 0))
    except (TypeError, ValueError):
        map_type = 0
    if content_kind == "hub_return":
        semantic_kind = "hub_return"
    elif map_type == 3 or content_kind in {"dungeon", "nest"}:
        semantic_kind = "dungeon"
    else:
        semantic_kind = content_kind or f"map_type:{map_type}"

    raw_name = str(row.get("nest_name") or row.get("map_name") or "").strip()
    semantic_name = (
        canonical_nest_name(raw_name).casefold()
        if semantic_kind == "dungeon"
        else raw_name.casefold()
    )
    return ("unresolved_map", semantic_kind, semantic_name)


def _normalize_map_history(
    primary: tuple[dict[str, object], ...] | list[dict[str, object]],
    fallback: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    candidates: list[tuple[float, int, dict[str, object]]] = []
    for source_priority, source in ((1, fallback), (2, primary)):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            try:
                observed_at = max(0.0, float(raw.get("observed_at") or 0.0))
                map_id = max(0, int(raw.get("map_id") or 0))
            except (TypeError, ValueError):
                continue
            row = dict(raw)
            row["observed_at"] = observed_at
            row["map_id"] = map_id
            candidates.append((observed_at, source_priority, row))

    rows: list[dict[str, object]] = []
    priorities: list[int] = []
    for _observed_at, source_priority, row in sorted(
        candidates,
        key=lambda value: (value[0], value[1]),
    ):
        if rows and _map_history_merge_key(rows[-1]) == _map_history_merge_key(row):
            # The replay snapshot and the independent raw scan often confirm
            # the same transition a fraction of a second apart. Keep the
            # earliest protocol observation, while allowing the richer primary
            # row to supply names/catalog metadata. Selecting only ``primary``
            # used to discard earlier rooms whenever it contained one late
            # destination confirmation.
            if source_priority >= priorities[-1]:
                earliest = float(rows[-1].get("observed_at") or 0.0)
                merged = {**rows[-1], **row}
                merged["observed_at"] = earliest
                rows[-1] = merged
                priorities[-1] = source_priority
            continue
        rows.append(row)
        priorities.append(source_priority)
    return tuple(rows)


def _movement_map_type(row: Mapping[str, object]) -> int:
    """Resolve a history row to the same map tier used by combat replay."""

    try:
        explicit = int(row.get("map_type") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit:
        return explicit
    try:
        map_id = int(row.get("map_id") or 0)
    except (TypeError, ValueError):
        map_id = 0
    entry = resolve_dungeon_map_id(map_id)
    if entry is not None:
        return int(entry.map_type or 0)
    # A formal-map entity fallback does not claim an exact room ID, but it
    # does prove that the capture is already inside a configured dungeon.
    if (
        map_id == 0
        and bool(row.get("inferred"))
        and str(row.get("content_kind") or "").strip().casefold()
        == "dungeon"
    ):
        return 3
    return 0


def _configured_nest_routes(
    nest_name: object,
) -> tuple[tuple[object, ...], ...]:
    """Return catalog-backed route variants for one recognized nest.

    The explicit table preserves the known current/historical branches. For
    every other nest, the unpacked Gate graph supplies connected route
    variants. This avoids treating two disconnected revisions with the same
    display name as one invented route while still covering the complete map
    catalog.
    """

    canonical_name = canonical_nest_name(nest_name)
    configured = _NEST_ROUTE_VARIANTS.get(canonical_name, ())
    routes: list[tuple[object, ...]] = []
    for map_ids in configured:
        entries = tuple(
            entry
            for map_id in map_ids
            if (entry := resolve_dungeon_map_id(map_id)) is not None
            and is_battle_map(entry)
            and canonical_nest_name(entry.display_name) == canonical_name
        )
        if len(entries) == len(map_ids):
            routes.append(entries)
    if routes:
        return tuple(routes)

    fallback = {
        entry.map_id: entry
        for entry in load_dungeon_map_catalog().values()
        if is_battle_map(entry)
        and canonical_nest_name(entry.display_name) == canonical_name
    }
    if not fallback:
        return ()

    neighbours: dict[int, set[int]] = {
        map_id: set() for map_id in fallback
    }
    for map_id, entry in fallback.items():
        for destination in entry.gate_map_ids:
            if destination not in fallback:
                continue
            neighbours[map_id].add(destination)
            neighbours[destination].add(map_id)

    variants: list[tuple[object, ...]] = []
    unvisited = set(fallback)
    while unvisited:
        seed = min(unvisited)
        pending = [seed]
        component: set[int] = set()
        while pending:
            map_id = pending.pop()
            if map_id in component:
                continue
            component.add(map_id)
            pending.extend(neighbours[map_id] - component)
        unvisited -= component
        variants.append(
            tuple(fallback[map_id] for map_id in sorted(component))
        )
    return tuple(variants)


def _verified_nest_resolution(
    identities: Mapping[str, Mapping[str, object]],
):
    boss_names: list[str] = []
    template_ids: set[int] = set()
    for raw in identities.values():
        if str(raw.get("kind") or "").strip().casefold() != "boss":
            continue
        name = str(raw.get("name") or "").strip()
        if name:
            boss_names.append(name)
        values = raw.get("templateIds", ())
        if not isinstance(values, (tuple, list, set, frozenset)):
            values = (values,)
        for value in values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                template_ids.add(parsed)
    if not boss_names and not template_ids:
        return None
    resolution = resolve_nest_evidence(
        boss_names,
        template_ids=template_ids,
    )
    if resolution is None or resolution.confidence == "capture_hint":
        return None
    return resolution


def _battle_end_from_history(
    history: tuple[dict[str, object], ...],
) -> float | None:
    for row in history:
        if str(row.get("content_kind") or "").strip().casefold() != "hub_return":
            continue
        try:
            return max(
                0.0,
                float(
                    row.get("battle_left_at")
                    or row.get("observed_at")
                    or 0.0
                ),
            )
        except (TypeError, ValueError):
            continue
    return None


def _apply_verified_battle_exit_bounds(
    history: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Close the preceding Type 3 segment at the verified battle exit."""

    rows = [dict(row) for row in history]
    for index, row in enumerate(rows):
        if str(row.get("content_kind") or "").strip().casefold() != "hub_return":
            continue
        try:
            battle_left_at = max(
                0.0,
                float(
                    row.get("battle_left_at")
                    or row.get("observed_at")
                    or 0.0
                ),
            )
        except (TypeError, ValueError):
            continue
        for previous_index in range(index - 1, -1, -1):
            previous = rows[previous_index]
            if _movement_map_type(previous) != 3:
                continue
            try:
                previous_start = max(
                    0.0,
                    float(previous.get("observed_at") or 0.0),
                )
            except (TypeError, ValueError):
                previous_start = 0.0
            if battle_left_at >= previous_start:
                old_left = previous.get("left_at")
                previous["left_at"] = (
                    min(float(old_left), battle_left_at)
                    if old_left is not None
                    else battle_left_at
                )
            break
    return tuple(rows)


def _infer_verified_nest_map_history(
    history: tuple[dict[str, object], ...],
    identities: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Recover a Type 3 nest label from packet-backed combat identities.

    Some captures begin after the dungeon entry packet and end with only an
    exact town-return packet. Boss spawn/damage identities still carry the
    authoritative monster template IDs. A one-map route can be assigned
    exactly. A multi-map route remains nest-level until packet candidates and
    coordinated loading boundaries prove the individual rooms.
    """

    if any(_movement_map_type(row) == 3 for row in history):
        return history
    resolution = _verified_nest_resolution(identities)
    if resolution is None:
        return history
    routes = _configured_nest_routes(resolution.profile.label)
    if not routes:
        return history
    unique_entries = {
        entry.map_id: entry
        for route in routes
        for entry in route
    }
    battle_end = _battle_end_from_history(history)
    entry = next(iter(unique_entries.values())) if len(unique_entries) == 1 else None
    inferred = {
        "map_id": entry.map_id if entry is not None else 0,
        "map_name": (
            entry.display_name
            if entry is not None
            else resolution.profile.display_name
        ),
        "nest_name": resolution.profile.label,
        "official_name": entry.official_name if entry is not None else "",
        "map_type": entry.map_type if entry is not None else 3,
        "content_kind": entry.content_kind if entry is not None else "dungeon",
        "observed_at": 0.0,
        "layout": (
            "combat-template-single-map-v576"
            if entry is not None
            else "combat-template-nest-route-v576"
        ),
        "confidence": resolution.confidence,
        "inferred": True,
        "movement_map_assignment": (
            "verified_combat_template_single_map"
            if entry is not None
            else "verified_combat_template_nest"
        ),
        "nest_profile_key": resolution.profile.key,
        "template_id": int(resolution.template_id or 0),
        **({"left_at": battle_end} if battle_end is not None else {}),
    }
    return tuple(
        sorted(
            (inferred, *history),
            key=lambda row: float(row.get("observed_at") or 0.0),
        )
    )


def _coordinated_room_boundaries(
    points: tuple[MovementPoint, ...],
    *,
    start_seconds: float,
    before_seconds: float,
) -> list[dict[str, float | int]]:
    """Find loading boundaries from coordinated party-position discontinuities."""

    by_actor: dict[str, list[MovementPoint]] = defaultdict(list)
    timestamps: set[int] = set()
    for point in points:
        try:
            runtime_id = int(point.actor_id[2:], 16)
        except (TypeError, ValueError):
            continue
        if runtime_id < 0x10000:
            continue
        seconds = point.timestamp_ms / 1000.0
        if seconds < start_seconds or seconds > before_seconds:
            continue
        by_actor[point.actor_id].append(point)
        timestamps.add(point.timestamp_ms)
    if len(by_actor) < 2 or len(timestamps) < 2:
        return []
    for actor_points in by_actor.values():
        actor_points.sort(key=lambda row: row.timestamp_ms)

    actor_times = {
        actor_id: [row.timestamp_ms for row in actor_points]
        for actor_id, actor_points in by_actor.items()
    }
    ordered_times = sorted(timestamps)
    minimum_actors = max(2, min(4, len(by_actor)))
    candidates: list[dict[str, float | int]] = []
    for before_ms, after_ms in zip(ordered_times, ordered_times[1:]):
        silence_seconds = (after_ms - before_ms) / 1000.0
        if silence_seconds < 8.0:
            continue
        planar_changes: list[float] = []
        floor_changes: list[float] = []
        for actor_id, actor_points in by_actor.items():
            times = actor_times[actor_id]
            previous_index = bisect_right(times, before_ms) - 1
            following_index = bisect_left(times, after_ms)
            if previous_index < 0 or following_index >= len(actor_points):
                continue
            previous = actor_points[previous_index]
            following = actor_points[following_index]
            if before_ms - previous.timestamp_ms > 30_000:
                continue
            if following.timestamp_ms - after_ms > 30_000:
                continue
            planar_changes.append(
                math.hypot(following.x - previous.x, following.y - previous.y)
            )
            floor_changes.append(abs(following.floor_value - previous.floor_value))
        if len(planar_changes) < minimum_actors:
            continue
        median_planar = float(median(planar_changes))
        median_floor = float(median(floor_changes))
        # Loading screens reposition most of the party.  Long pauses with an
        # unchanged position are cutscenes/mechanics and must not split maps.
        if median_planar < 250.0 and median_floor < 20.0:
            continue
        candidates.append(
            {
                "start_seconds": after_ms / 1000.0,
                "silence_seconds": silence_seconds,
                "median_planar": median_planar,
                "median_floor": median_floor,
                "actor_count": len(planar_changes),
                "score": median_planar + median_floor * 2.0,
            }
        )
    return candidates


def _server_spawn_room_boundaries(
    capture: MovementCapture,
    *,
    nest_name: str,
    route: tuple[object, ...],
    start_seconds: float,
    before_seconds: float,
) -> list[dict[str, object]]:
    """Resolve sequential room changes from server entity-create packets.

    ``06 27 55 00`` is emitted when the server installs the entities for a
    room. Some rooms begin with a population burst before their Boss spawns;
    others publish one unique Boss entry entity. Both are stronger evidence
    than a coordinate jump and cannot be confused with a long mechanic pause.
    """

    raw_events = capture.diagnostics.get("monster_spawn_events", ())
    if not isinstance(raw_events, (tuple, list)) or len(route) < 2:
        return []
    room_templates = _NEST_ROOM_TEMPLATE_IDS.get(nest_name, {})
    if not room_templates:
        return []

    boss_template_ids: set[int] = set()
    for profile in load_fixed_boss_hp_profiles():
        if canonical_nest_name(profile.label) == canonical_nest_name(nest_name):
            boss_template_ids.update(profile.template_ids)

    events: list[tuple[float, int]] = []
    route_template_ids = {
        template_id
        for entry in route
        for template_id in room_templates.get(entry.map_id, frozenset())
    }
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            continue
        try:
            observed_at = max(0.0, float(raw.get("observed_at") or 0.0))
            template_id = int(raw.get("template_id") or 0)
        except (TypeError, ValueError):
            continue
        if (
            observed_at < start_seconds - 0.001
            or observed_at >= before_seconds - 0.001
            or template_id not in route_template_ids
        ):
            continue
        events.append((observed_at, template_id))
    if not events:
        return []

    # Network batching can spread one room's creation records over adjacent
    # timestamps. A 250 ms burst keeps that packet fact intact without moving
    # the boundary to a later player coordinate.
    bursts: list[tuple[float, list[int]]] = []
    for observed_at, template_id in sorted(events):
        if not bursts or observed_at - bursts[-1][0] > 0.25:
            bursts.append((observed_at, [template_id]))
        else:
            bursts[-1][1].append(template_id)

    boundaries: list[dict[str, object]] = []
    cursor = start_seconds
    for route_index, entry in enumerate(route[1:], start=1):
        next_templates = room_templates.get(entry.map_id, frozenset())
        later_templates = {
            template_id
            for later_entry in route[route_index + 1 :]
            for template_id in room_templates.get(
                later_entry.map_id,
                frozenset(),
            )
        }
        selected: dict[str, object] | None = None
        for observed_at, template_ids in bursts:
            if observed_at <= cursor + 0.001:
                continue
            next_count = sum(
                template_id in next_templates for template_id in template_ids
            )
            if not next_count:
                continue
            later_count = sum(
                template_id in later_templates for template_id in template_ids
            )
            boss_markers = sorted(
                {
                    template_id
                    for template_id in template_ids
                    if template_id in next_templates
                    and template_id in boss_template_ids
                }
            )
            population_burst = (
                next_count >= 2
                and next_count >= max(2, later_count * 2)
            )
            if not population_burst and not boss_markers:
                continue
            selected = {
                "map_id": int(entry.map_id),
                "start_seconds": float(observed_at),
                "evidence_kind": (
                    "server_entity_population_boundary"
                    if population_burst
                    else "server_boss_entry_boundary"
                ),
                "spawn_count": int(next_count),
                "boss_template_ids": boss_markers,
            }
            break
        if selected is None:
            break
        boundaries.append(selected)
        cursor = float(selected["start_seconds"])
    return boundaries


def _expand_inferred_multimap_route(
    history: tuple[dict[str, object], ...],
    capture: MovementCapture,
) -> tuple[dict[str, object], ...]:
    """Resolve a proven dungeon route without promoting a client packet to entry."""

    inferred_rows = [
        row
        for row in history
        if int(row.get("map_id") or 0) == 0
        and bool(row.get("inferred"))
        and _movement_map_type(row) == 3
    ]
    exact_battle_rows = [
        row
        for row in history
        if int(row.get("map_id") or 0) > 0 and _movement_map_type(row) == 3
    ]
    if len(inferred_rows) != 1 or exact_battle_rows:
        return history
    inferred = inferred_rows[0]
    inferred_name = canonical_nest_name(
        str(inferred.get("nest_name") or inferred.get("map_name") or "")
    )
    routes = _configured_nest_routes(inferred_name)
    if not routes:
        return history
    raw_candidates = capture.diagnostics.get("map_candidates", ())
    if not isinstance(raw_candidates, (tuple, list)):
        raw_candidates = ()
    selected_candidate: dict[str, object] | None = None
    selected_entry = None
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        entry = resolve_dungeon_map_id(raw.get("map_id"))
        if not is_battle_map(entry):
            continue
        if inferred_name and canonical_nest_name(entry.display_name) != inferred_name:
            continue
        selected_candidate = raw
        selected_entry = entry
    route: tuple[object, ...] | None = None
    if selected_entry is not None:
        for variant in routes:
            candidate_index = next(
                (
                    index
                    for index, entry in enumerate(variant)
                    if entry.map_id == selected_entry.map_id
                ),
                None,
            )
            if candidate_index is not None:
                route = variant[: candidate_index + 1]
                break
    elif len(routes) == 1:
        route = routes[0]
    if not route:
        return history

    boundary_count = len(route) - 1
    evidence_seconds = max(0.0, float(inferred.get("observed_at") or 0.0))
    known_battle_end = (
        inferred.get("left_at")
        if inferred.get("left_at") is not None
        else _battle_end_from_history(history)
    )
    battle_end = min(
        capture.duration_seconds,
        max(
            evidence_seconds,
            float(known_battle_end or capture.duration_seconds),
        ),
    )
    candidate_seconds = battle_end
    if selected_candidate is not None:
        candidate_seconds = min(
            battle_end,
            max(
                evidence_seconds,
                float(selected_candidate.get("observed_at") or battle_end),
            ),
        )
    packet_boundaries = _server_spawn_room_boundaries(
        capture,
        nest_name=inferred_name,
        route=route,
        start_seconds=0.0,
        before_seconds=candidate_seconds,
    )
    if packet_boundaries:
        # Emit only the server-proven contiguous route prefix. Missing later
        # room evidence must not be manufactured from coordinate gaps.
        route = route[: len(packet_boundaries) + 1]
        boundary_count = len(route) - 1
        selected_boundaries = packet_boundaries
    else:
        boundaries = _coordinated_room_boundaries(
            capture.points,
            start_seconds=0.0,
            before_seconds=candidate_seconds,
        )
        if len(boundaries) < boundary_count:
            return history
        selected_boundaries = sorted(
            sorted(
                boundaries,
                key=lambda row: float(row["score"]),
                reverse=True,
            )[:boundary_count],
            key=lambda row: float(row["start_seconds"]),
        )

    # Entity/template evidence can arrive well after the party has already
    # entered the first room. If the party has supplied positions since the
    # beginning and there is no coordinated loading boundary before that late
    # evidence, the complete prefix belongs to room one. A real earlier
    # transition keeps the conservative evidence timestamp and prevents town
    # coordinates from leaking into the dungeon replay.
    has_player_prefix = any(
        point.timestamp_ms / 1000.0 < evidence_seconds
        and int(point.actor_id[2:], 16) >= 0x10000
        for point in capture.points
        if point.actor_id.casefold().startswith("0x")
    )
    has_pre_evidence_boundary = any(
        float(row["start_seconds"]) <= evidence_seconds + 0.001
        for row in selected_boundaries
    )
    layout = str(inferred.get("layout") or "").strip().casefold()
    can_retroactively_apply = bool(inferred.get("nest_profile_key")) or (
        layout.startswith("s2c-udp-formal-map-entity-fallback")
        and int(inferred.get("template_id") or 0) > 0
    )
    start_seconds = (
        0.0
        if (boundary_count > 0 or can_retroactively_apply)
        and has_player_prefix
        and not has_pre_evidence_boundary
        else evidence_seconds
    )

    route_rows: list[dict[str, object]] = []
    starts = [start_seconds] + [
        float(row["start_seconds"]) for row in selected_boundaries
    ]
    for index, (entry, observed_at) in enumerate(zip(route, starts)):
        boundary = selected_boundaries[index - 1] if index else None
        route_rows.append(
            {
                "map_id": entry.map_id,
                "map_name": entry.display_name,
                "nest_name": canonical_nest_name(entry.display_name),
                "official_name": entry.official_name,
                "map_type": entry.map_type,
                "content_kind": entry.content_kind,
                "observed_at": observed_at,
                "layout": "movement-coordinated-room-route-v576",
                "confidence": "verified_route_sequence",
                "inferred": True,
                "movement_map_assignment": (
                    (
                        "candidate_verified_route_start"
                        if selected_entry is not None
                        else "route_start"
                    )
                    if boundary is None
                    else str(
                        boundary.get("evidence_kind")
                        or "coordinated_party_loading_boundary"
                    )
                ),
                **(
                    {
                        "boundary_silence_seconds": round(
                            float(boundary["silence_seconds"]), 3
                        ),
                        "boundary_actor_count": int(boundary["actor_count"]),
                    }
                    if boundary is not None
                    and "silence_seconds" in boundary
                    else {}
                ),
                **(
                    {
                        "boundary_spawn_count": int(
                            boundary.get("spawn_count") or 0
                        ),
                        "boundary_boss_templates": list(
                            boundary.get("boss_template_ids") or ()
                        ),
                    }
                    if boundary is not None
                    and str(boundary.get("evidence_kind") or "").startswith(
                        "server_"
                    )
                    else {}
                ),
                **(
                    {"left_at": float(inferred["left_at"])}
                    if index == len(route) - 1
                    and inferred.get("left_at") is not None
                    else {}
                ),
            }
        )
    expanded: list[dict[str, object]] = []
    for row in history:
        if row is inferred:
            expanded.extend(route_rows)
        else:
            expanded.append(dict(row))
    expanded.sort(key=lambda row: float(row.get("observed_at") or 0.0))
    return tuple(expanded)


def _expand_verified_room_evidence(
    history: tuple[dict[str, object], ...],
    capture: MovementCapture,
    identities: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Expand one verified nest row with packet-backed room evidence.

    Exact map rows, client map candidates, Boss template lifetimes, and
    coordinated party loading boundaries are combined in that order. Only a
    contiguous proven route prefix is emitted, so a catalog route never
    creates an unobserved room by itself.
    """

    battle_rows = [row for row in history if _movement_map_type(row) == 3]
    if len(battle_rows) != 1:
        return history
    anchor = battle_rows[0]
    assignment = str(anchor.get("movement_map_assignment") or "")
    if assignment.startswith(("coordinated_", "boss_template_")):
        return history

    nest_name = canonical_nest_name(
        str(anchor.get("nest_name") or anchor.get("map_name") or "")
    )
    routes = _configured_nest_routes(nest_name)
    if not routes:
        return history
    try:
        exact_map_id = max(0, int(anchor.get("map_id") or 0))
    except (TypeError, ValueError):
        exact_map_id = 0

    raw_candidates = capture.diagnostics.get("map_candidates", ())
    if not isinstance(raw_candidates, (tuple, list)):
        raw_candidates = ()
    candidates: list[tuple[dict[str, object], object]] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        entry = resolve_dungeon_map_id(raw.get("map_id"))
        if not is_battle_map(entry):
            continue
        if canonical_nest_name(entry.display_name) != nest_name:
            continue
        candidates.append((raw, entry))

    selected_route: tuple[object, ...] | None = None
    if exact_map_id > 0:
        selected_route = next(
            (
                route
                for route in routes
                if any(entry.map_id == exact_map_id for entry in route)
            ),
            None,
        )
    if selected_route is None and candidates:
        candidate_map_id = candidates[-1][1].map_id
        selected_route = next(
            (
                route
                for route in routes
                if any(entry.map_id == candidate_map_id for entry in route)
            ),
            None,
        )
    if selected_route is None:
        try:
            template_id = int(anchor.get("template_id") or 0)
        except (TypeError, ValueError):
            template_id = 0
        confidence = str(anchor.get("confidence") or "").strip().casefold()
        route_map_id = (
            _NEST_ENTRY_TEMPLATE_ROUTE_MAP.get((nest_name, template_id), 0)
            if confidence == "entry_template"
            else 0
        )
        if route_map_id:
            selected_route = next(
                (
                    route
                    for route in routes
                    if any(entry.map_id == route_map_id for entry in route)
                ),
                None,
            )
    if selected_route is None and len(routes) == 1:
        selected_route = routes[0]
    if not selected_route:
        return history

    route = selected_route
    if (
        len(route) == 1
        and exact_map_id == route[0].map_id
    ):
        return history
    if exact_map_id > 0:
        anchor_index = next(
            index
            for index, entry in enumerate(route)
            if entry.map_id == exact_map_id
        )
        route = route[anchor_index:]
    if not route:
        return history

    battle_start = max(0.0, float(anchor.get("observed_at") or 0.0))
    if (
        battle_start > 0.0
        and bool(anchor.get("inferred"))
        and str(anchor.get("confidence") or "").strip().casefold()
        == "entry_template"
        and str(anchor.get("layout") or "").strip().casefold()
        == "s2c-udp-formal-map-entity-fallback-v576"
        and not any(
            float(row.get("observed_at") or 0.0) < battle_start
            for row in history
            if row is not anchor
        )
    ):
        # The formal entry entity can be observed well after the map has
        # loaded (for example when the first gate becomes active). With no
        # earlier scene boundary, it labels the coordinate space already
        # present at capture start instead of opening a new room at that time.
        battle_start = 0.0
    direct_starts: dict[int, list[float]] = defaultdict(list)
    if exact_map_id > 0:
        direct_starts[exact_map_id].append(battle_start)
    for raw, entry in candidates:
        if any(route_entry.map_id == entry.map_id for route_entry in route):
            direct_starts[entry.map_id].append(
                max(0.0, float(raw.get("observed_at") or 0.0))
            )

    room_templates = _NEST_ROOM_TEMPLATE_IDS.get(nest_name, {})
    for raw in identities.values():
        values = raw.get("templateIds", ())
        if not isinstance(values, (tuple, list, set, frozenset)):
            values = (values,)
        template_ids: set[int] = set()
        for value in values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                template_ids.add(parsed)
        template_visibility = raw.get("templateVisibility", {})
        if not isinstance(template_visibility, Mapping):
            template_visibility = {}
        for entry in route:
            matched_templates = (
                template_ids & room_templates.get(entry.map_id, frozenset())
            )
            if not matched_templates:
                continue
            visible_starts: list[float] = []
            for template_id in matched_templates:
                template_range = (
                    template_visibility.get(str(template_id))
                    or template_visibility.get(template_id)
                )
                if not isinstance(template_range, Mapping):
                    continue
                try:
                    visible_starts.append(
                        max(0.0, float(template_range.get("visibleFrom")))
                    )
                except (TypeError, ValueError):
                    continue
            if not visible_starts:
                try:
                    visible_starts.append(
                        max(0.0, float(raw.get("visibleFrom")))
                    )
                except (TypeError, ValueError):
                    continue
            direct_starts[entry.map_id].append(min(visible_starts))

    known_battle_end = (
        anchor.get("left_at")
        if anchor.get("left_at") is not None
        else _battle_end_from_history(history)
    )
    battle_end = min(
        capture.duration_seconds,
        max(battle_start, float(known_battle_end or capture.duration_seconds)),
    )
    packet_boundaries = _server_spawn_room_boundaries(
        capture,
        nest_name=nest_name,
        route=route,
        start_seconds=battle_start,
        before_seconds=battle_end,
    )
    packet_starts = {
        int(row["map_id"]): row for row in packet_boundaries
    }
    boundaries = _coordinated_room_boundaries(
        capture.points,
        start_seconds=battle_start,
        before_seconds=battle_end,
    )
    boundary_seconds = sorted(
        float(row["start_seconds"]) for row in boundaries
    )

    starts: list[float] = [battle_start]
    evidence_kinds: list[str] = [
        "exact_map_id" if exact_map_id else "verified_route_start"
    ]
    used_boundaries: set[float] = set()
    for entry in route[1:]:
        packet_boundary = packet_starts.get(int(entry.map_id))
        if packet_boundary is not None:
            observed_at = float(packet_boundary["start_seconds"])
            if observed_at <= starts[-1] + 0.001:
                break
            starts.append(observed_at)
            evidence_kinds.append(
                str(packet_boundary.get("evidence_kind") or "server_entity_boundary")
            )
            continue
        direct = sorted(
            value
            for value in direct_starts.get(entry.map_id, ())
            if value > starts[-1] + 0.001
        )
        if not direct:
            break
        upper = direct[0]
        preceding = [
            value
            for value in boundary_seconds
            if value not in used_boundaries
            and value > starts[-1] + 10.0
            and value <= upper + 1.0
        ]
        if preceding:
            observed_at = preceding[-1]
            used_boundaries.add(observed_at)
            evidence_kind = "coordinated_party_loading_boundary"
        else:
            observed_at = upper
            evidence_kind = (
                "candidate_map_transition"
                if any(
                    candidate_entry.map_id == entry.map_id
                    and abs(float(raw.get("observed_at") or 0.0) - upper) < 0.001
                    for raw, candidate_entry in candidates
                )
                else "boss_template_room_start"
            )
        if observed_at >= battle_end - 0.001:
            break
        starts.append(observed_at)
        evidence_kinds.append(evidence_kind)

    first_room_is_proven = bool(
        exact_map_id
        or any(entry.map_id == route[0].map_id for _, entry in candidates)
        or len(route) == 1
    )
    if len(starts) == 1 and not first_room_is_proven:
        return history

    route_rows: list[dict[str, object]] = []
    for index, (entry, observed_at) in enumerate(zip(route, starts)):
        route_rows.append(
            {
                "map_id": entry.map_id,
                "map_name": entry.display_name,
                "nest_name": canonical_nest_name(entry.display_name),
                "official_name": entry.official_name,
                "map_type": entry.map_type,
                "content_kind": entry.content_kind,
                "observed_at": observed_at,
                "layout": "verified-room-evidence-route-v576",
                "confidence": "verified_route_sequence",
                "inferred": True,
                "movement_map_assignment": evidence_kinds[index],
                **(
                    {"left_at": float(anchor["left_at"])}
                    if index == len(starts) - 1
                    and anchor.get("left_at") is not None
                    else {}
                ),
            }
        )
    expanded: list[dict[str, object]] = []
    for row in history:
        if row is anchor:
            expanded.extend(route_rows)
        else:
            expanded.append(dict(row))
    expanded.sort(key=lambda row: float(row.get("observed_at") or 0.0))
    return tuple(expanded)


def _identity_value(
    identities: Mapping[str, Mapping[str, object]],
    actor_id: str,
) -> dict[str, object]:
    raw = identities.get(actor_id) or identities.get(actor_id.casefold()) or {}
    name = str(raw.get("name") or "").strip()
    job = str(raw.get("job") or "").strip()
    kind = str(raw.get("kind") or "entity").strip() or "entity"
    if name.startswith("角色 0x") or name == actor_id:
        name = ""
    visible_from = raw.get("visibleFrom")
    visible_until = raw.get("visibleUntil")
    return {
        "name": name,
        "job": job,
        "kind": kind,
        "profileId": str(raw.get("profileId") or raw.get("profile_id") or "").strip(),
        "visibleFrom": visible_from,
        "visibleUntil": visible_until,
    }


def _track_break(previous: MovementPoint | None, current: MovementPoint) -> bool:
    if previous is None:
        return True
    elapsed = max(0.0, (current.timestamp_ms - previous.timestamp_ms) / 1000.0)
    distance = math.hypot(current.x - previous.x, current.y - previous.y)
    # Break on missing observations, teleports, map-room swaps, or floor
    # changes. The vertical value is never exposed to the two-dimensional UI.
    return (
        elapsed > 15.0
        or distance > max(1500.0, elapsed * 2000.0)
        or abs(current.floor_value - previous.floor_value) > 500.0
    )


def _isolated_position_spike(
    previous: MovementPoint,
    current: MovementPoint,
    following: MovementPoint,
) -> bool:
    before_seconds = max(0.0, (current.timestamp_ms - previous.timestamp_ms) / 1000.0)
    after_seconds = max(0.0, (following.timestamp_ms - current.timestamp_ms) / 1000.0)
    if before_seconds > 5.0 or after_seconds > 5.0:
        return False
    before = math.hypot(current.x - previous.x, current.y - previous.y)
    after = math.hypot(following.x - current.x, following.y - current.y)
    across = math.hypot(following.x - previous.x, following.y - previous.y)
    before_limit = max(1500.0, before_seconds * 2000.0)
    after_limit = max(1500.0, after_seconds * 2000.0)
    across_limit = max(1500.0, (before_seconds + after_seconds) * 2000.0)
    return (
        before > before_limit
        and after > after_limit
        and across <= across_limit
    )


def _canonical_runtime_actor_id(
    actor_id: str,
    aliases: Mapping[str, str],
) -> str:
    def normalized(value: object) -> str:
        text = str(value or "").strip()
        if text.casefold().startswith("0x"):
            try:
                return f"0x{int(text[2:], 16) & 0xFFFFFFFF:08X}"
            except ValueError:
                return text
        return text

    current = normalized(actor_id)
    if not current:
        return ""
    seen: set[str] = set()
    while current in aliases and current not in seen:
        seen.add(current)
        current = normalized(aliases[current] or current)
    return current


def _normalized_runtime_aliases(capture: MovementCapture) -> dict[str, str]:
    raw = capture.diagnostics.get("runtime_aliases", {})
    if not isinstance(raw, Mapping):
        return {}
    aliases: dict[str, str] = {}
    for source, target in raw.items():
        source_text = _canonical_runtime_actor_id(str(source or ""), {})
        target_text = _canonical_runtime_actor_id(str(target or ""), {})
        if source_text and target_text and source_text != target_text:
            aliases[source_text] = target_text
    for source in tuple(aliases):
        aliases[source] = _canonical_runtime_actor_id(aliases[source], aliases)
    return aliases


def _point_line_distance(
    point: list[float | int],
    start: list[float | int],
    end: list[float | int],
) -> float:
    px, py = float(point[1]), float(point[2])
    sx, sy = float(start[1]), float(start[2])
    ex, ey = float(end[1]), float(end[2])
    dx, dy = ex - sx, ey - sy
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return math.hypot(px - sx, py - sy)
    ratio = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / denominator))
    return math.hypot(px - (sx + ratio * dx), py - (sy + ratio * dy))


def _simplify_path_group(
    points: list[list[float | int]],
    tolerance: float,
) -> list[list[float | int]]:
    if len(points) <= 2:
        return points
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        best_index = -1
        best_distance = tolerance
        for index in range(start + 1, end):
            distance = _point_line_distance(points[index], points[start], points[end])
            if distance > best_distance:
                best_distance = distance
                best_index = index
        if best_index >= 0:
            keep.add(best_index)
            pending.append((start, best_index))
            pending.append((best_index, end))
    return [points[index] for index in sorted(keep)]


def _overview_points(
    points: list[list[float | int]],
) -> list[list[float | int]]:
    if len(points) <= 2:
        return list(points)
    span_x = max(float(row[1]) for row in points) - min(float(row[1]) for row in points)
    span_y = max(float(row[2]) for row in points) - min(float(row[2]) for row in points)
    tolerance = max(2.0, max(span_x, span_y) / 512.0)
    groups: list[list[list[float | int]]] = []
    current: list[list[float | int]] = []
    for point in points:
        if int(point[3]) == 1 and current:
            groups.append(current)
            current = []
        current.append(point)
    if current:
        groups.append(current)
    result: list[list[float | int]] = []
    for group in groups:
        simplified = _simplify_path_group(group, tolerance)
        if simplified:
            simplified[0] = [*simplified[0][:3], 1]
            result.extend(simplified)
    return result


def _segment_track_payload(
    points: list[MovementPoint],
    identities: Mapping[str, Mapping[str, object]],
    runtime_aliases: Mapping[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_actor: dict[str, list[MovementPoint]] = defaultdict(list)
    for point in points:
        actor_id = _canonical_runtime_actor_id(point.actor_id, runtime_aliases)
        if actor_id:
            by_actor[actor_id].append(point)
    confirmed_local_actor_ids = {
        actor_id
        for actor_id, actor_points in by_actor.items()
        if _identity_value(identities, actor_id)["kind"] == "player"
        and any(row.local_actor for row in actor_points)
    }
    tracks: list[dict[str, object]] = []
    diagnostic_tracks: list[dict[str, object]] = []
    for actor_id, actor_points in by_actor.items():
        actor_points.sort(key=lambda row: (row.timestamp_ms, row.actor_id))
        identity = _identity_value(identities, actor_id)
        try:
            runtime_id = int(actor_id[2:], 16) if actor_id.casefold().startswith("0x") else 0
        except ValueError:
            runtime_id = 0
        if identity["kind"] == "boss":
            visible_from = identity.get("visibleFrom")
            visible_until = identity.get("visibleUntil")
            actor_points = [
                point
                for point in actor_points
                if (
                    visible_from is None
                    or point.timestamp_ms / 1000.0 >= float(visible_from)
                )
                and (
                    visible_until is None
                    or point.timestamp_ms / 1000.0 <= float(visible_until)
                )
            ]
            if not actor_points:
                continue
        # Runtime-id shape is not identity evidence. Only the local actor,
        # packet-confirmed party players and lifecycle-confirmed Bosses belong
        # in the public replay. Unknown actors remain available as compact
        # diagnostics without distorting map bounds or visible track counts.
        is_local = any(row.local_actor for row in actor_points)
        unconfirmed_local_candidate = (
            is_local
            and identity["kind"] not in {"player", "boss"}
            and bool(confirmed_local_actor_ids)
        )
        if (
            identity["kind"] not in {"player", "boss"}
            and (not is_local or unconfirmed_local_candidate)
        ):
            diagnostic_tracks.append(
                {
                    "actorId": actor_id,
                    "reason": (
                        "unconfirmed_local_candidate"
                        if unconfirmed_local_candidate
                        else "unconfirmed_entity"
                    ),
                    "pointCount": len(actor_points),
                    "firstSeconds": round(actor_points[0].timestamp_ms / 1000.0, 3),
                    "lastSeconds": round(actor_points[-1].timestamp_ms / 1000.0, 3),
                    "sourceLayouts": sorted({row.source_layout for row in actor_points}),
                }
            )
            continue
        if 0 < runtime_id < 0x10000 and identity["kind"] != "boss":
            continue
        name = str(identity["name"])
        job = str(identity["job"])
        kind = "player" if is_local and identity["kind"] == "entity" else str(identity["kind"])
        if not name:
            name = job or ("本机角色" if is_local else actor_id)
        isolated_breaks: set[int] = set()
        for index in range(1, len(actor_points) - 1):
            if _isolated_position_spike(
                actor_points[index - 1],
                actor_points[index],
                actor_points[index + 1],
            ):
                isolated_breaks.update((index, index + 1))
        compact_points: list[list[float | int]] = []
        previous: MovementPoint | None = None
        for index, point in enumerate(actor_points):
            starts_new_path = (
                _track_break(previous, point)
                or index in isolated_breaks
                or (previous is not None and previous.actor_id != point.actor_id)
            )
            compact_points.append(
                [
                    round(point.timestamp_ms / 1000.0, 3),
                    round(point.x, 3),
                    round(point.y, 3),
                    1 if starts_new_path else 0,
                ]
            )
            previous = point
        visible_from = identity.get("visibleFrom")
        visible_until = identity.get("visibleUntil")
        if identity["kind"] == "boss":
            # Boss health/hit messages can be sparse during mechanics, so a
            # per-sample timeout would make a stationary Boss blink out. Keep
            # it visible continuously from the first to the last verified
            # body-position sample. After the final sample, a short grace
            # covers the death animation without leaving every old stage Boss
            # visible for the rest of a long replay.
            if visible_from is None:
                visible_from = actor_points[0].timestamp_ms / 1000.0
            if visible_until is None:
                visible_until = actor_points[-1].timestamp_ms / 1000.0 + 3.0
        tracks.append(
            {
                "actorId": actor_id,
                "name": name,
                "job": job,
                "kind": kind,
                "isLocal": is_local,
                "profileId": identity["profileId"],
                "runtimeAliases": sorted(
                    {
                        row.actor_id
                        for row in actor_points
                        if _canonical_runtime_actor_id(row.actor_id, {}) != actor_id
                    }
                ),
                "samplingMode": "event" if kind == "boss" else "movement",
                "pointCount": len(compact_points),
                "points": compact_points,
                "overviewPoints": _overview_points(compact_points),
                **(
                    {"visibleFrom": round(float(visible_from), 3)}
                    if visible_from is not None
                    else {}
                ),
                **(
                    {"visibleUntil": round(float(visible_until), 3)}
                    if visible_until is not None
                    else {}
                ),
            }
        )
    rank = {"player": 0, "boss": 1, "entity": 2}
    tracks.sort(
        key=lambda row: (
            0 if row["isLocal"] else 1,
            rank.get(str(row["kind"]), 3),
            str(row["job"]),
            str(row["name"]),
        )
    )
    diagnostic_tracks.sort(key=lambda row: (-int(row["pointCount"]), str(row["actorId"])))
    return tracks, diagnostic_tracks


def _segment_bounds(points: list[MovementPoint]) -> dict[str, float] | None:
    if not points:
        return None
    min_x = min(row.x for row in points)
    max_x = max(row.x for row in points)
    min_y = min(row.y for row in points)
    max_y = max(row.y for row in points)
    pad = max(40.0, max(max_x - min_x, max_y - min_y) * 0.08)
    return {
        "minX": round(min_x - pad, 3),
        "maxX": round(max_x + pad, 3),
        "minY": round(min_y - pad, 3),
        "maxY": round(max_y + pad, 3),
    }


def _track_bounds(tracks: list[dict[str, object]]) -> dict[str, float] | None:
    coordinates = [
        (float(point[1]), float(point[2]))
        for track in tracks
        for point in track.get("points", ())
        if isinstance(point, (list, tuple)) and len(point) >= 3
    ]
    if not coordinates:
        return None
    min_x = min(row[0] for row in coordinates)
    max_x = max(row[0] for row in coordinates)
    min_y = min(row[1] for row in coordinates)
    max_y = max(row[1] for row in coordinates)
    pad = max(40.0, max(max_x - min_x, max_y - min_y) * 0.08)
    return {
        "minX": round(min_x - pad, 3),
        "maxX": round(max_x + pad, 3),
        "minY": round(min_y - pad, 3),
        "maxY": round(max_y + pad, 3),
    }


def _projection_quality(asset: object, tracks: list[dict[str, object]]) -> dict[str, object]:
    if asset is None:
        return {
            "status": "coordinate_plane",
            "sampleCount": 0,
            "outOfBoundsCount": 0,
            "outOfBoundsRate": 0.0,
        }
    sample_count = 0
    outside = 0
    width = max(0.0, float(getattr(asset, "coordinate_width", 0.0) or 0.0))
    height = max(0.0, float(getattr(asset, "coordinate_height", 0.0) or 0.0))
    min_x = float(getattr(asset, "coordinate_min_x", 0.0) or 0.0)
    min_y = float(getattr(asset, "coordinate_min_y", 0.0) or 0.0)
    swap_xy = bool(getattr(asset, "swap_xy", False))
    if width <= 0.0 or height <= 0.0:
        return {
            "status": "coordinate_plane",
            "sampleCount": 0,
            "outOfBoundsCount": 0,
            "outOfBoundsRate": 0.0,
        }
    for track in tracks:
        for point in track.get("points", ()):
            if not isinstance(point, (list, tuple)) or len(point) < 3:
                continue
            raw_x, raw_y = float(point[1]), float(point[2])
            projected_x, projected_y = (raw_y, raw_x) if swap_xy else (raw_x, raw_y)
            normalized_x = (projected_x - min_x) / width
            normalized_y = (projected_y - min_y) / height
            sample_count += 1
            if not (-0.005 <= normalized_x <= 1.005 and -0.005 <= normalized_y <= 1.005):
                outside += 1
    ratio = outside / sample_count if sample_count else 0.0
    status = "normal" if ratio < 0.01 else ("review" if ratio <= 0.05 else "fallback")
    return {
        "status": status,
        "sampleCount": sample_count,
        "outOfBoundsCount": outside,
        "outOfBoundsRate": round(ratio, 6),
    }


def _map_evidence(
    definition: Mapping[str, object],
    *,
    map_available: bool,
    projection_status: str,
) -> tuple[str, str]:
    if not map_available or projection_status == "fallback":
        if projection_status == "fallback":
            return "coordinate_plane", "坐标越界，已回退坐标平面"
        return "coordinate_plane", "仅坐标平面"
    confidence = str(definition.get("confidence") or "").strip().casefold()
    inferred = bool(definition.get("inferred"))
    if confidence == "exact_map_id" and not inferred:
        return "server_exact", "服务端精确地图"
    assignment = str(
        definition.get("movement_map_assignment") or ""
    ).strip().casefold()
    if assignment.startswith("server_"):
        return "server_room_boundary", "服务端房间实体换图证据"
    return "route_inferred", "副本路线 + 加载边界推断"


def build_movement_payload(
    capture: MovementCapture,
    *,
    source_hash: str,
    source_file: str,
    identities: Mapping[str, Mapping[str, object]] | None = None,
    map_history: tuple[dict[str, object], ...] | list[dict[str, object]] = (),
    cache_hit: bool = False,
) -> dict[str, object]:
    identities = identities or {}
    runtime_aliases = _normalized_runtime_aliases(capture)
    history = _normalize_map_history(map_history, capture.map_history)
    history = _infer_verified_nest_map_history(history, identities)
    history = _apply_verified_battle_exit_bounds(history)
    history = _expand_inferred_multimap_route(history, capture)
    history = _expand_verified_room_evidence(history, capture, identities)
    if (
        len(history) == 1
        and capture.points
        and _movement_map_type(history[0]) == 3
        and bool(history[0].get("entry_inferred_from_exit"))
    ):
        # Some client-to-server transition layouts confirm the currently
        # loaded map only near the end of a run.  If every verified position
        # predates that lone confirmation, it is a late label for the same
        # coordinate space rather than a new empty map segment.  Apply it
        # retroactively so the recorded path can use the real unpacked map.
        last_point_seconds = max(
            row.timestamp_ms / 1000.0 for row in capture.points
        )
        if float(history[0].get("observed_at") or 0.0) > last_point_seconds:
            retroactive = dict(history[0])
            retroactive["observed_at"] = 0.0
            retroactive["movement_map_assignment"] = "late_confirmation"
            history = (retroactive,)
    end_seconds = max(
        capture.duration_seconds,
        max((row.timestamp_ms / 1000.0 for row in capture.points), default=0.0),
        max((float(row.get("observed_at") or 0.0) for row in history), default=0.0),
        max((float(row.get("left_at") or 0.0) for row in history), default=0.0),
    )
    # Keep non-battle rows in the boundary list so a following town/garden
    # transition closes the preceding dungeon segment at the right T+ time.
    # They are deliberately not emitted: MovementLedger is a combat replay,
    # not a town movement recorder.
    definitions = [dict(row) for row in history]

    segments: list[dict[str, object]] = []
    for index, definition in enumerate(definitions):
        start_seconds = max(0.0, float(definition.get("observed_at") or 0.0))
        next_start = (
            max(start_seconds, float(definitions[index + 1].get("observed_at") or 0.0))
            if index + 1 < len(definitions)
            else end_seconds
        )
        left_at = definition.get("left_at")
        segment_end = min(
            next_start,
            max(start_seconds, float(left_at)),
        ) if left_at is not None else next_start
        if _movement_map_type(definition) != 3:
            continue
        is_last = index + 1 == len(definitions)
        points = [
            row
            for row in capture.points
            if row.timestamp_ms / 1000.0 >= start_seconds
            and (
                row.timestamp_ms / 1000.0 < segment_end
                or (is_last and row.timestamp_ms / 1000.0 <= segment_end + 1e-6)
            )
        ]
        # Suppress empty transient maps while retaining the final recognized
        # destination, where an older capture may simply have no movement rows.
        if not points and not is_last:
            continue
        map_id = int(definition.get("map_id") or 0)
        asset = resolve_movement_map_asset(map_id)
        tracks, diagnostic_tracks = _segment_track_payload(
            points,
            identities,
            runtime_aliases,
        )
        projection = _projection_quality(asset, tracks)
        projection_status = str(projection["status"])
        map_available = asset is not None and projection_status in {"normal", "review"}
        map_evidence_tier, map_evidence_label = _map_evidence(
            definition,
            map_available=map_available,
            projection_status=projection_status,
        )
        segments.append(
            {
                "key": f"{index}:{map_id}:{round(start_seconds, 3)}",
                "mapId": map_id,
                "mapType": 3,
                "mapName": str(
                    definition.get("map_name")
                    or definition.get("nest_name")
                    or "未识别地图"
                ),
                "officialName": str(definition.get("official_name") or ""),
                "contentKind": str(definition.get("content_kind") or ""),
                "mapConfidence": str(definition.get("confidence") or ""),
                "mapAssignment": str(
                    definition.get("movement_map_assignment") or ""
                ),
                "mapEvidenceTier": map_evidence_tier,
                "mapEvidenceLabel": map_evidence_label,
                "startSeconds": round(start_seconds, 3),
                "endSeconds": round(segment_end, 3),
                "pointCount": sum(int(row["pointCount"]) for row in tracks),
                "trackCount": len(tracks),
                "diagnosticTrackCount": len(diagnostic_tracks),
                "diagnosticPointCount": sum(
                    int(row["pointCount"]) for row in diagnostic_tracks
                ),
                "diagnosticTracks": diagnostic_tracks,
                "bounds": _track_bounds(tracks),
                "projection": projection,
                "map": (
                    {
                        "available": map_available,
                        "assetAvailable": True,
                        **(
                            {"fallbackReason": "coordinate_out_of_bounds"}
                            if projection_status == "fallback"
                            else {}
                        ),
                        "gridDirectory": asset.grid_directory,
                        "imageWidth": asset.image_width,
                        "imageHeight": asset.image_height,
                        "worldWidth": asset.world_width,
                        "worldHeight": asset.world_height,
                        "cellSize": asset.cell_size,
                        "coordinateWidth": asset.coordinate_width,
                        "coordinateHeight": asset.coordinate_height,
                        "coordinateMinX": asset.coordinate_min_x,
                        "coordinateMinY": asset.coordinate_min_y,
                        "swapXY": asset.swap_xy,
                        "invertX": asset.invert_x,
                        "invertY": asset.invert_y,
                    }
                    if asset is not None
                    else {"available": False, "assetAvailable": False}
                ),
                "tracks": tracks,
            }
        )

    point_count = sum(int(row["pointCount"]) for row in segments)
    actor_ids = {
        str(track["actorId"])
        for segment in segments
        for track in segment["tracks"]
    }
    return {
        "schema": "movement-ledger-v1",
        "state": "ready" if point_count else "no_movement_evidence",
        "message": (
            "已读取 Type 3 战斗地图中协议校验通过的二维坐标"
            if point_count
            else "该记录没有可归入 Type 3 战斗地图的可靠移动坐标；不会用推测数据绘制轨迹"
        ),
        "sourceHash": source_hash,
        "sourceFile": source_file,
        "durationSeconds": round(end_seconds, 3),
        "segmentCount": len(segments),
        "trackCount": len(actor_ids),
        "pointCount": point_count,
        "coordinateSystem": {
            "axes": ["x", "y"],
            "verticalIncluded": False,
            "wireEncoding": "client-float24-ieee754-truncated",
            "wireBytesPerComponent": 3,
            "discardedMantissaBits": 5,
            "mapYInverted": True,
        },
        "segments": segments,
        "diagnostics": {**capture.diagnostics, "cache_hit": cache_hit},
    }
