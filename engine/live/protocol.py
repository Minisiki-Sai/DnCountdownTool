from __future__ import annotations

import ipaddress
import json
import math
import re
import struct
from bisect import bisect_left, insort
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

from .instant_action_damage import (
    BURNING_HAND_ACTION_EFFECT_CLASS,
    BURNING_HAND_ACTION_EFFECT_CODE,
    BURNING_HAND_ACTION_PARAMETER,
    BURNING_HAND_FILTER_EFFECT_CLASS,
    BURNING_HAND_FILTER_EFFECT_CODE,
    BURNING_HAND_FILTER_PARAMETER,
    BURNING_HAND_SKILL_ID,
)
from .udp_direction import UdpMirrorDirectionTracker, has_ambiguous_action_direction
from .tls_probe import TlsStreamProbe


_PCAPNG_SECTION_HEADER = 0x0A0D0D0A
_PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
_PCAPNG_ENHANCED_PACKET = 0x00000006
_IPV4_LLC_MARKER = b"\x08\x00\x45"
_UINT32_LE = struct.Struct("<I")


def _load_v576_udp_cipher_profile() -> tuple[bytes, bytes, dict[str, object]]:
    """Load the UDP cipher material extracted from the current client.

    The client stores its 13-byte table in decoder order.  Existing ACT UDP
    parsers index an equivalent table eight bytes later because their legacy
    wire-body origin is one byte earlier.  Keeping the normalization explicit
    preserves parser compatibility without treating packet samples as key
    material.
    """

    profile_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "udp_cipher.json"
    )
    fallback_raw = bytes.fromhex("3f2642fd8a723474c8bfcda494")
    profile: dict[str, object] = {}
    try:
        loaded = json.loads(profile_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            profile = loaded
        raw = bytes.fromhex(str(profile["key_hex_client_order"]))
        if len(raw) != 13:
            raise ValueError("invalid v576 UDP key length")
        rotation = int(profile.get("act_legacy_rotation", 8)) % len(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
        raw = fallback_raw
        rotation = 8
        profile = {
            "source_kind": "code_fallback_current_client",
            "key_hex_client_order": raw.hex(),
            "act_legacy_rotation": rotation,
        }
    normalized = raw[rotation:] + raw[:rotation]
    return raw, normalized, profile


_V576_CLIENT_UDP_XOR_KEY, _V576_BODY_XOR_KEY, _V576_UDP_CIPHER_PROFILE = (
    _load_v576_udp_cipher_profile()
)
_V576_BODY_XOR_TABLES = tuple(
    bytes(value ^ key_byte for value in range(256))
    for key_byte in _V576_BODY_XOR_KEY
)
# The client decoder proves that the signature byte determines the 13-byte
# table phase as ``(11 - signature_byte) mod 13``.  Defining all byte values
# removes exhaustive phase probing while retaining the same results for every
# previously calibrated signature.
_V576_XOR_PHASE_BY_SIGNATURE_BYTE = {
    signature_byte: (11 - signature_byte) % len(_V576_BODY_XOR_KEY)
    for signature_byte in range(256)
}
_V576_EMBEDDED_ACTION_PHASE_BY_SIGNATURE: dict[tuple[str, bytes], int] = {}
_V576_BOSS_ACTION_PHASE_BY_SIGNATURE: dict[bytes, int] = {}
_V576_BOSS_STRUCTURED_PHASE_BY_SIGNATURE: dict[bytes, int] = {}
_V576_BODY_OFFSET = 9
_V576_DECODE_CACHE_MAX_PAYLOAD = 1024 * 1024


@lru_cache(maxsize=256)
def _format_ipv4(raw: bytes) -> str:
    return f"{raw[0]}.{raw[1]}.{raw[2]}.{raw[3]}"


@lru_cache(maxsize=16)
def _v576_wire_marker_pattern(markers: tuple[bytes, ...]) -> re.Pattern[bytes]:
    """Match decoded markers in the encrypted wire payload without decoding it."""
    wire_markers = {
        bytes(
            value ^ _V576_BODY_XOR_KEY[(key_start + index) % len(_V576_BODY_XOR_KEY)]
            for index, value in enumerate(marker)
        )
        for marker in markers
        for key_start in range(len(_V576_BODY_XOR_KEY))
    }
    alternatives = b"|".join(
        re.escape(marker)
        for marker in sorted(wire_markers, key=lambda value: (-len(value), value))
    )
    return re.compile(alternatives or b"(?!)")


@lru_cache(maxsize=16384)
def _v576_wire_may_contain_marker(
    payload: bytes,
    markers: tuple[bytes, ...],
    *,
    start_offset: int = 9,
) -> bool:
    """Cheap, lossless prefilter for phase-dependent decoded markers."""
    if len(payload) <= start_offset:
        return False
    return _v576_wire_marker_pattern(markers).search(payload, start_offset) is not None


# ``feed_events`` used to invoke every independent v576 decoder for every UDP
# packet.  Long captures contain hundreds of thousands of state/position
# packets that cannot possibly produce a combat event, so the repeated marker
# scans dominated replay loading.  These flags are a lossless routing hint:
# the individual decoders remain authoritative and still perform all of their
# original structural validation once selected.
V576_PACKET_PRIMARY = 1 << 0
V576_PACKET_ACTION = 1 << 1
V576_PACKET_STATUS = 1 << 2
V576_PACKET_HEALTH = 1 << 3
V576_PACKET_LIFECYCLE = 1 << 4
V576_PACKET_BOSS_ACTION = 1 << 5
V576_PACKET_STATUS_DAMAGE = 1 << 6
V576_PACKET_HEALING = 1 << 7
V576_PACKET_EMBEDDED_DIRECT_DAMAGE = 1 << 8
V576_PACKET_COMPOUND = 1 << 9
V576_PACKET_STATUS_END = 1 << 10

_V576_PACKET_ALL_DYNAMIC = (
    V576_PACKET_ACTION
    | V576_PACKET_STATUS
    | V576_PACKET_HEALTH
    | V576_PACKET_LIFECYCLE
    | V576_PACKET_BOSS_ACTION
    | V576_PACKET_STATUS_DAMAGE
    | V576_PACKET_HEALING
    | V576_PACKET_EMBEDDED_DIRECT_DAMAGE
    | V576_PACKET_COMPOUND
    | V576_PACKET_STATUS_END
)

_V576_ACTION_MARKERS = (
    b"\x0e\x23\x00",
    b"\x0e\x1f\x00",
    b"\x12\x24\x00",
    b"\x12\x20\x00",
)
_V576_DIRECT37_RECORD_MARKERS = (
    (b"\x08\x73\x00", 0x73),
    (b"\x08\x75\x00", 0x75),
)
_V576_DIRECT41_RECORD_MARKERS = (
    (b"\x08\x77\x00", 0x77),
    (b"\x08\x79\x00", 0x79),
)
_V576_DIRECT_RECORD_MARKERS = (
    *_V576_DIRECT37_RECORD_MARKERS,
    *_V576_DIRECT41_RECORD_MARKERS,
)
_V576_DIRECT_MARKER_BYTES = tuple(
    marker for marker, _declared_length in _V576_DIRECT_RECORD_MARKERS
)
_V576_SERVER_EVENT_MARKERS = (
    b"\x08\x6f\x00",
    b"\x21\x0c\x00",
    b"\x1c\x1b\x00\x04",
    b"\x1c\x1b\x00\x05",
    b"\x11\x05\x00",
    b"\x15\x05\x00",
    b"\x03\x14\x00",
    b"\x0e\x16\x00",
    *_V576_ACTION_MARKERS,
    b"\xff\xff\xff\xff",
    b"\x08\x73\x00",
    b"\x08\x75\x00",
    b"\x08\x77\x00",
    b"\x08\x79\x00",
    b"\x00\x00\x00\xff\xff\xff\xff\xff\xff",
    b"\x1c\x1b\x00\x00",
    b"\x1c\x1b\x00\x01",
    b"\x1c\x1b\x00\x02",
    b"\x1c\x1b\x00\x03",
)
_V576_PACKET_MARKER_ROUTES = (
    (b"\x08\x6f\x00", 13, V576_PACKET_HEALTH),
    (b"\x21\x0c\x00", 13, V576_PACKET_HEALTH),
    (
        b"\x1c\x1b\x00\x04",
        9,
        V576_PACKET_HEALTH
        | V576_PACKET_STATUS_DAMAGE
        | V576_PACKET_HEALING
        | V576_PACKET_PRIMARY,
    ),
    (b"\x11\x05\x00", 13, V576_PACKET_LIFECYCLE),
    (b"\x15\x05\x00", 18, V576_PACKET_STATUS_END),
    (b"\x03\x14\x00", 13, V576_PACKET_BOSS_ACTION),
    (b"\x0e\x16\x00", 13, V576_PACKET_BOSS_ACTION),
    (
        b"\x0e\x23\x00",
        14,
        V576_PACKET_ACTION | V576_PACKET_BOSS_ACTION | V576_PACKET_PRIMARY,
    ),
    (
        b"\x0e\x1f\x00",
        14,
        V576_PACKET_ACTION | V576_PACKET_BOSS_ACTION | V576_PACKET_PRIMARY,
    ),
    (
        b"\x12\x24\x00",
        14,
        V576_PACKET_ACTION | V576_PACKET_BOSS_ACTION | V576_PACKET_PRIMARY,
    ),
    (
        b"\x12\x20\x00",
        14,
        V576_PACKET_ACTION | V576_PACKET_BOSS_ACTION | V576_PACKET_PRIMARY,
    ),
    (b"\xff\xff\xff\xff", 24, V576_PACKET_STATUS),
    (
        b"\x08\x73\x00",
        9,
        V576_PACKET_PRIMARY
        | V576_PACKET_COMPOUND
        | V576_PACKET_EMBEDDED_DIRECT_DAMAGE,
    ),
    (
        b"\x08\x75\x00",
        9,
        V576_PACKET_PRIMARY
        | V576_PACKET_COMPOUND
        | V576_PACKET_EMBEDDED_DIRECT_DAMAGE,
    ),
    (
        b"\x08\x77\x00",
        9,
        V576_PACKET_PRIMARY | V576_PACKET_COMPOUND | V576_PACKET_EMBEDDED_DIRECT_DAMAGE,
    ),
    (
        b"\x08\x79\x00",
        9,
        V576_PACKET_PRIMARY | V576_PACKET_COMPOUND | V576_PACKET_EMBEDDED_DIRECT_DAMAGE,
    ),
    (
        b"\x00\x00\x00\xff\xff\xff\xff\xff\xff",
        9,
        V576_PACKET_PRIMARY | V576_PACKET_COMPOUND,
    ),
    (b"\x1c\x1b\x00\x00", 9, V576_PACKET_STATUS_DAMAGE | V576_PACKET_PRIMARY),
    (b"\x1c\x1b\x00\x01", 9, V576_PACKET_STATUS_DAMAGE | V576_PACKET_PRIMARY),
    (b"\x1c\x1b\x00\x02", 9, V576_PACKET_STATUS_DAMAGE | V576_PACKET_PRIMARY),
    (b"\x1c\x1b\x00\x03", 9, V576_PACKET_STATUS_DAMAGE | V576_PACKET_PRIMARY),
    (b"\x1c\x1b\x00\x05", 9, V576_PACKET_STATUS_DAMAGE | V576_PACKET_PRIMARY | V576_PACKET_HEALTH | V576_PACKET_HEALING),
)
_V576_PACKET_ROUTE_BY_MARKER = {
    marker: (start_offset, marker_features)
    for marker, start_offset, marker_features in _V576_PACKET_MARKER_ROUTES
}
_V576_DECODED_SERVER_EVENT_PATTERN = re.compile(
    b"|".join(
        re.escape(marker)
        for marker in sorted(
            _V576_PACKET_ROUTE_BY_MARKER,
            key=lambda value: (-len(value), value),
        )
    )
)
_V576_DECODED_CLIENT_EVENT_PATTERN = re.compile(
    b"|".join(re.escape(marker) for marker in _V576_ACTION_MARKERS)
)


def scan_v576_packet_features(
    payload: bytes,
    direction: str | None,
    transport: str = "UDP",
) -> int:
    """Return a conservative decoder-routing mask for one logical packet.

    A zero result proves that no currently supported fixed layout or
    structural marker is present.  Ambiguous signatures/phases deliberately
    return every dynamic flag, so this optimisation can add work on an
    unusual packet but can never hide one from an authoritative decoder.
    """
    if str(transport or "").upper() == "TCP":
        return V576_PACKET_PRIMARY | _V576_PACKET_ALL_DYNAMIC
    if len(payload) < 9:
        return 0

    signature = payload[5:9]
    features = (
        V576_PACKET_PRIMARY
        if signature in _V576_MESSAGE_LENGTH_BY_SIGNATURE
        else 0
    )
    markers = (
        _V576_ACTION_MARKERS
        if direction == "client_to_server"
        else _V576_SERVER_EVENT_MARKERS
    )
    if not _v576_wire_may_contain_marker(payload, markers, start_offset=9):
        return features

    preferred_phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
    if preferred_phase is None:
        # The wire prefilter proved that at least one supported marker exists,
        # but an unknown signature cannot select a phase safely.
        return features | V576_PACKET_PRIMARY | _V576_PACKET_ALL_DYNAMIC

    decoded = _decode_v576_body(payload, preferred_phase)
    routed = 0
    decoded_pattern = (
        _V576_DECODED_CLIENT_EVENT_PATTERN
        if direction == "client_to_server"
        else _V576_DECODED_SERVER_EVENT_PATTERN
    )
    for marker_match in decoded_pattern.finditer(decoded, 9):
        marker = marker_match.group()
        start_offset, marker_features = _V576_PACKET_ROUTE_BY_MARKER[marker]
        if marker_match.start() >= start_offset:
            routed |= marker_features
        if marker == b"\x00\x00\x00\xff\xff\xff\xff\xff\xff":
            # The long compound marker contains an overlapping FF status
            # marker. The combined regex deliberately consumes the longest
            # form; retain the route that the independent status scan saw.
            routed |= V576_PACKET_STATUS
    if routed:
        return features | routed

    # Signature phases are reliable for normal messages. A few variable
    # families reuse a signature with another phase; retain the old exhaustive
    # behaviour whenever the phase hint and lossless wire scan disagree.
    return features | V576_PACKET_PRIMARY | _V576_PACKET_ALL_DYNAMIC


def _is_valid_v576_damage_amount(amount: int) -> bool:
    if not 0 < amount <= 1_000_000_000_000:
        return False
    # A negative 32-bit protocol field decoded as unsigned appears as
    # 0xFFFFxxxx (roughly 4.295B). It is a state/sentinel value, not damage.
    if 0xFFFF0000 <= amount <= 0xFFFFFFFF:
        return False
    # 1B 00 04 is the low three bytes of an HP/status record marker. Some
    # compound packets place it where a normal hit layout expects damage.
    return amount & 0xFFFFFF != 0x04001B


def _decode_v576_fixed_hit_damage(
    decoded: bytes,
    damage_offset: int,
    payload_length: int,
) -> int:
    """Read a fixed hit amount without joining a signed delta to its flag."""
    raw = decoded[damage_offset : damage_offset + 8]
    damage = int.from_bytes(raw, "little")
    if payload_length == 219:
        low_signed = int.from_bytes(raw[:4], "little", signed=True)
        trailing_flag = int.from_bytes(raw[4:], "little")
        if low_signed < 0 and trailing_flag == 1:
            # This effect variant stores a signed 32-bit HP delta followed by
            # a result flag. Reading both as uint64 creates a fake ~2^33 hit.
            return -low_signed
    return damage

# Receive offload may combine several game messages into one captured UDP
# payload. The signature identifies the size of each logical message.
_V576_MESSAGE_LENGTH_BY_SIGNATURE = {
    b"\x06\x27\x55\x00": 88,
    b"\x65\xc7\x55\x00": 88,
    b"\x87\x06\x2a\x00": 45,
    b"\xf5\x37\x2b\x00": 46,
    b"\x7c\xde\x3c\x00": 63,
    b"\x18\xba\x3d\x00": 64,
    b"\x97\x35\x45\x00": 72,
    b"\xfe\x5c\x46\x00": 73,
    b"\xce\x6c\x47\x00": 74,
    b"\x28\x8a\x4d\x00": 80,
    b"\xe5\xa7\x51\x00": 84,
    b"\xce\x6c\x54\x00": 87,
    b"\xa7\xe5\x58\x00": 91,
    b"\xe5\x47\x5e\x00": 97,
    b"\xf7\xa6\x6f\x00": 114,
    b"\x2e\x2c\x35\x00": 56,
    b"\x92\x90\x36\x00": 57,
    b"\xe5\xe7\x85\x00": 136,
    b"\x65\x67\x89\x00": 140,
    b"\x92\x30\x91\x00": 148,
    b"\x28\x4a\xa8\x00": 171,
    b"\x6e\x2c\x9c\x00": 159,
    b"\xce\x6c\x95\x00": 152,
    b"\x65\xc7\x96\x00": 153,
    b"\xfe\xbc\xa1\x00": 164,
    b"\xd0\x72\xa7\x00": 170,
    b"\x6e\x0c\xa9\x00": 172,
    b"\x92\x30\xab\x00": 174,
    b"\xa7\xe5\xb3\x00": 182,
    b"\x2e\x6c\x9d\x00": 160,
    b"\x7c\x5e\xcb\x00": 206,
    b"\x6e\x0c\xd0\x00": 211,
    b"\x6e\x2c\xd0\x00": 211,
    b"\xe5\xa7\xd3\x00": 214,
    b"\xce\x6c\xd6\x00": 217,
    b"\x65\xc7\xd7\x00": 218,
    b"\x7c\xde\xd8\x00": 219,
    b"\x6e\xcc\xdd\x00": 224,
    b"\xfe\x5c\xe2\x00": 229,
    b"\xfe\xbc\xe2\x00": 229,
    b"\x65\x27\xe4\x00": 231,
}
_V576_MESSAGE_SIGNATURE_PATTERN = re.compile(
    b"|".join(
        re.escape(signature)
        for signature in sorted(_V576_MESSAGE_LENGTH_BY_SIGNATURE)
    )
)


@dataclass(frozen=True, slots=True)
class PacketRecord:
    timestamp_seconds: float
    transport: str
    source_ip: str
    destination_ip: str
    source_port: int
    destination_port: int
    payload: bytes
    captured_length: int
    original_length: int
    tcp_sequence: int | None = None
    tcp_acknowledgment: int | None = None
    tcp_flags: int = 0
    tcp_connection_generation: int = 0
    direction_hint: str | None = None

    def direction(self, server_ip: str, server_port: int | None = None) -> str:
        if self.destination_ip == server_ip and (
            server_port is None or self.destination_port == server_port
        ):
            return "client_to_server"
        if self.source_ip == server_ip and (
            server_port is None or self.source_port == server_port
        ):
            return "server_to_client"
        return "other"

    def inferred_direction(self) -> str | None:
        direction = _infer_packet_direction(self.source_ip, self.destination_ip)
        if direction is not None:
            return direction
        return self.direction_hint if self.direction_hint in {
            "client_to_server", "server_to_client"
        } else None


@dataclass(frozen=True, slots=True)
class DecodedV576Movement:
    """One protocol-verified top-down actor position.

    V576 stores each world component as the client's three-byte compressed
    float. ``vertical`` is retained only so MovementLedger can break paths
    when the game moves an actor between floors; it is deliberately omitted
    from the public two-dimensional replay payload.
    """

    actor_id: int
    x: float
    y: float
    vertical: float
    layout: str
    packet_signature: str
    xor_phase: int
    record_offset: int


@lru_cache(maxsize=256)
def _infer_packet_direction(source_ip: str, destination_ip: str) -> str | None:
    source = ipaddress.ip_address(source_ip)
    destination = ipaddress.ip_address(destination_ip)
    if source.is_private and not destination.is_private:
        return "client_to_server"
    if not source.is_private and destination.is_private:
        return "server_to_client"
    return None


@dataclass(frozen=True, slots=True)
class DecodedV576Message:
    kind: str
    payload_length: int
    layout: str
    skill_id: int | None = None
    damage: int | None = None
    critical: bool | None = None
    source_actor_id: int | None = None
    target_actor_id: int | None = None
    alternate_target_actor_id: int | None = None
    observer_actor_id: int | None = None
    source_name: str = ""
    appended_status_skill_id: int | None = None
    candidate_skill_ids: tuple[int, ...] = ()
    # True only when ``skill_id`` came from a protocol field whose position is
    # fixed relative to this exact damage-result record. This is deliberately
    # separate from ``candidate_skill_ids``: the latter scans a compound
    # payload and may include a neighbouring cast/status skill.
    skill_id_exact: bool = False
    # Exact direct37 wire identifiers. The result-group ID at marker+4 is
    # short lived and can join skill-bearing and skill-less rows from the same
    # server result batch. The object ID at marker+13 is persistent only for
    # projectile/field families and is resolved by DamageObjectOriginLedger.
    damage_result_group_id: int | None = None
    damage_object_id: int | None = None
    periodic_kind: str = ""
    periodic_label: str = ""
    healing: bool = False
    # Byte offset of the exact wire record when one packet contains multiple
    # damage rows. Zero means the legacy single-record decoder did not expose
    # an offset. This lets attribution join equal-damage direct37 rows without
    # relying on value matching alone.
    record_offset: int = 0
    # Exact fourteen-byte tail carried by the 1C 1B status-damage record.
    # Some tails name a skill/effect directly (including equipment suffix
    # states), while others are only protocol constants.  Preserve the bytes
    # here and let a data-driven catalog decide whether the trailing integer
    # is meaningful; never infer an effect from the integer alone.
    status_damage_tail_hex: str = ""


@dataclass(frozen=True, slots=True)
class DecodedV576Status:
    skill_id: int
    target_actor_id: int
    source_actor_id: int
    duration_ms: int
    effect_code: str
    parameters: str
    xor_phase: int
    record_offset: int
    permanent: bool = False
    skill_level_row_id: int | None = None
    effect_class_id: int | None = None
    effect_instance_id: int | None = None


@dataclass(frozen=True, slots=True)
class DecodedV576DamageObjectRegistration:
    """Client registration for one locally simulated damage object.

    The object ID is repeated by later ``direct37`` result rows.  The
    projectile template is the stable bridge into the unpacked client tables
    (``weapontable_projectile -> filetable -> ACT``), so this record can prove
    a damage origin without comparing damage values or guessing by time.
    """

    source_actor_id: int
    damage_object_id: int
    projectile_template_id: int
    effect_resource_id: int
    child_sequence: int
    xor_phase: int
    layout: str = "c2s-udp-damage-object-register-107-v576"
    # Offset of the registration record in the decoded message. Keeping this
    # prevents a registration that is physically after a bundled direct37 row
    # from being treated as if it existed before that hit.
    record_offset: int = 0
    # Alternate server lifecycle markers are admitted only as exact
    # (marker, Projectile RowId) subtypes after corpus-wide packet-skill
    # validation.  Retain that verdict on the concrete registration instead
    # of promoting the entire Projectile template globally.
    packet_validated_subtype: bool = False
    # Some alternate lifecycle records are structurally valid object
    # registrations, but their marker family is not globally skill-stable.
    # These rows are usable only when the stateful origin ledger can bind the
    # concrete object generation to a compatible same-owner action epoch.
    action_qualified_subtype: bool = False
    # Shared child Projectile rows are not globally skill-stable.  They may
    # only be used when the runtime ledger finds the exact client-ACT parent
    # Projectile on the same owner and a still-live object generation.
    parent_qualified_subtype: bool = False
    # Full runtime parent ID from the extended client record, never a nearby
    # template guessed from timestamps.
    parent_damage_object_id: int = 0
    registration_key: str = ""
    # Newly audited shared formats require an independent root on their generation.
    requires_root_action_proof: bool = False


@dataclass(frozen=True, slots=True)
class DecodedV576MonsterSpawn:
    actor_id: int
    template_id: int
    payload_length: int
    layout: str = "s2c-udp-monster-spawn-88-v576"


@dataclass(frozen=True, slots=True)
class DecodedV576MapIdentity:
    """A map identifier carried by a verified v576 layout.

    Server scene packets describe the map the client actually entered or
    left. Client packets only describe a selected/requested destination and
    therefore remain useful diagnostics without being authoritative current
    map evidence.
    """

    map_id: int
    xor_phase: int
    record_offset: int
    layout: str
    key_index: int | None = None
    active: bool = True
    authoritative: bool = True
    scene_state: int | None = None
    scene_flags: int = 0


@dataclass(frozen=True, slots=True)
class DecodedV576LobbyReturn:
    """A verified generic scene-transition acknowledgement.

    The historical class name is retained for API compatibility.  This short
    UDP record is emitted both after leaving a dungeon and between rooms of
    the same dungeon.  Its trailing value is a route code, not a destination
    map ID, so callers must correlate it with a battle-map leave and a later
    scene resource synchronization before claiming a hub return.
    """

    route_code: int
    xor_phase: int
    layout: str


@dataclass(frozen=True, slots=True)
class DecodedV576SceneTeardown:
    """A verified server record emitted while the current scene is removed.

    This record is deliberately not called a lobby return: the same message
    is present both when returning to the Garden and when Black Dragon moves
    between two rooms.  It becomes useful only after the scene-transition
    tracker correlates it with the acknowledgement and the next scene.
    """

    layout: str = "s2c-tcp-scene-teardown-32-v576"


@dataclass(frozen=True, slots=True)
class DecodedV576GatewayRouteCatalogEntry:
    """One route row sent after the client has returned to the gateway.

    ``07 0D`` is not a current-map packet: the server emits many rows and the
    first u32 enumerates available route IDs.  Its value must therefore never
    be interpreted as a Garden/Haven destination.  The structurally verified
    row is still valuable scene evidence because a fresh catalog is loaded
    after leaving a battle scene, while an ordinary in-dungeon room change
    proceeds directly to the next world server.
    """

    route_id: int
    key_index: int
    layout: str = "s2c-tcp-gateway-route-catalog-29-v576"


def decode_v576_gateway_route_catalog(
    payload: bytes,
    direction: str | None = None,
    *,
    connection_context: object | None = None,
) -> DecodedV576GatewayRouteCatalogEntry | None:
    """Decode a strict v576 ``07 0D`` gateway route-catalog envelope."""

    if (
        direction not in (None, "server_to_client")
        or len(payload) != 29
        or int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return None

    from .tcp_profile_codec import decode_registered_server_tcp_envelope

    envelope = decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=lambda plaintext: (
            len(plaintext) == 26
            and plaintext[2:4] == b"\x07\x0d"
            and plaintext[8:12] == b"\x00\x00\x01\x00"
        ),
    )
    if envelope is None:
        return None
    plaintext, key_index, _rounds, _source = envelope
    if (
        len(plaintext) != 26
        or plaintext[2:4] != b"\x07\x0d"
        or plaintext[8:12] != b"\x00\x00\x01\x00"
    ):
        return None
    route_id = _UINT32_LE.unpack_from(plaintext, 4)[0]
    if not 0 < route_id <= 0xFFFF:
        return None
    return DecodedV576GatewayRouteCatalogEntry(
        route_id=route_id,
        key_index=int(key_index),
    )


def decode_v576_scene_teardown(
    payload: bytes,
    direction: str | None = None,
    *,
    connection_context: object | None = None,
) -> DecodedV576SceneTeardown | None:
    """Decode the corpus-verified ``12 24`` scene teardown envelope.

    The complete plaintext shape is required.  Searching merely for opcode
    ``12 24`` would be unsafe because ordinary gameplay payloads can contain
    the same two bytes by coincidence.  This exact 29-byte record repeats at
    every independently observed Guardian/Trial and Black Dragon scene exit.
    """

    if (
        direction not in (None, "server_to_client")
        or len(payload) != 32
        or int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return None

    from .tcp_profile_codec import decode_registered_server_tcp_envelope

    expected_plaintext = bytes.fromhex(
        "1d00122400000000019600000005435d0069711c000000000000000000"
    )
    envelope = decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=lambda plaintext: plaintext == expected_plaintext,
    )
    if envelope is None:
        return None
    plaintext, _key_index, _rounds, _source = envelope
    if plaintext != expected_plaintext:
        return None
    return DecodedV576SceneTeardown()


def decode_v576_lobby_return(
    payload: bytes,
    direction: str | None = None,
) -> DecodedV576LobbyReturn | None:
    """Decode the corpus-verified generic scene-transition acknowledgement."""

    if (
        direction not in (None, "server_to_client")
        or len(payload) != 14
        or payload[5:9] != b"\xb3\x73\x0b\x00"
    ):
        return None
    xor_phase = 1
    decoded = _decode_v576_body(payload, xor_phase)
    route_code = _UINT32_LE.unpack_from(decoded, 10)[0]
    # All independent occurrences in the current corpus use route code 11.
    # Requiring it prevents similarly sized gameplay packets from becoming a
    # scene boundary; route 11 itself still does not identify the destination.
    if route_code != 11:
        return None
    return DecodedV576LobbyReturn(
        route_code=route_code,
        xor_phase=xor_phase,
        layout="s2c-udp-scene-transition-ack-14-v576",
    )


# Preferred semantic aliases.  Keep the old public names above so installed
# extensions written against earlier 4.x builds continue to import cleanly.
DecodedV576SceneTransitionAck = DecodedV576LobbyReturn
decode_v576_scene_transition_ack = decode_v576_lobby_return


def decode_v576_map_identity(
    payload: bytes,
    direction: str | None = None,
    *,
    connection_context: object | None = None,
) -> DecodedV576MapIdentity | None:
    """Decode a map ID without scanning arbitrary packet integers.

    Map IDs also occur coincidentally in positions, counters and encrypted
    payloads. Only layouts confirmed against independent Guardian, Trial,
    Mist and Black Dragon captures are accepted here. The server TCP layouts
    are the authoritative path for non-leaders; the two client-originated
    layouts remain useful corroborating evidence when the local player selects
    a map. Town/hall initialization uses a separate ``03 01`` layout from the
    battle-map ``82 45`` confirmation packet.
    The superficially similar ``C7 E7 10 00`` server record enumerates
    available maps and is intentionally *not* treated as the current map.
    Catalog membership is deliberately checked by the caller so this low
    level protocol function stays independent from packaged game data.
    """

    signature = payload[5:9] if len(payload) >= 9 else b""
    if (
        direction in (None, "server_to_client")
        and len(payload) == 28
        and int.from_bytes(payload[:2], "little") == len(payload)
    ):
        # The 19-byte client 07 E5 record is a map *selection*.  The server
        # answers with this 06 0B load instruction only after the destination
        # has actually been accepted.  In the 2026-09-28 selection/load
        # corpus it carries the concrete practice-mode map ID, even when the
        # later generic monster templates resolve to the classic nest. Solo
        # training-ground loads use the same server opcode and active flag,
        # but have a zero instance count. The last two u32 fields are route/
        # channel metadata, not fixed boolean flags: the 0928 speedrun server
        # sends 13/13 on entry. On returning to the Garden it sends the same
        # opcode with state 9 and destination map 36. Rejecting either shape
        # lost both automatic start and automatic stop despite receiving TCP.
        from .tcp_profile_codec import decode_registered_server_tcp_envelope

        envelope = decode_registered_server_tcp_envelope(
            payload,
            connection_context=connection_context,
            validator=lambda plaintext: (
                len(plaintext) == 25
                and plaintext[2:4] == b"\x06\x0b"
                and 0 < _UINT32_LE.unpack_from(plaintext, 4)[0] <= 65535
                and (
                    plaintext[8] in (1, 9)
                    # Nightmare Dragon hub returns use state 2 (1018 Sea
                    # captures). Keep the exception destination-specific:
                    # other state-2 records have not been verified as loads.
                    or (plaintext[8] == 2
                        and _UINT32_LE.unpack_from(plaintext, 4)[0] == 11120)
                )
                and 0 <= _UINT32_LE.unpack_from(plaintext, 13)[0] <= 16
            ),
        )
        if envelope is not None:
            plaintext, key_index, _rounds, _source = envelope
            return DecodedV576MapIdentity(
                map_id=_UINT32_LE.unpack_from(plaintext, 4)[0],
                xor_phase=-1,
                record_offset=4,
                layout="s2c-tcp-map-load-28-v576",
                key_index=int(key_index),
                scene_state=int(plaintext[8]),
            )

    if (
        direction in (None, "client_to_server")
        and len(payload) == 19
        and signature == b"\x07\xe5\x10\x00"
    ):
        xor_phase = 4
        record_offset = 13
        decoded = _decode_v576_body(payload, xor_phase)
        map_id = _UINT32_LE.unpack_from(decoded, record_offset)[0]
        return DecodedV576MapIdentity(
            map_id=map_id,
            xor_phase=xor_phase,
            record_offset=record_offset,
            layout="c2s-udp-map-entry-19-v576",
            authoritative=False,
        )

    if (
        direction in (None, "client_to_server")
        and len(payload) == 34
        and signature == b"\x6c\xdd\x1f\x00"
    ):
        xor_phase = 7
        record_offset = 10
        decoded = _decode_v576_body(payload, xor_phase)
        # The byte before the destination map is a stable field tag.  It
        # separates the transition record from similarly-sized client input.
        if decoded[9] != 0x15:
            return None
        map_id = _UINT32_LE.unpack_from(decoded, record_offset)[0]
        return DecodedV576MapIdentity(
            map_id=map_id,
            xor_phase=xor_phase,
            record_offset=record_offset,
            layout="c2s-udp-map-transition-34-v576",
            authoritative=False,
        )

    if (
        direction in (None, "server_to_client")
        and len(payload) == 322
        and int.from_bytes(payload[:2], "little") == len(payload)
    ):
        # Every independently observed Guardian/Trial/Black Dragon scene
        # release carries this server-side 0C 05 context record immediately
        # before the generic teardown/transition acknowledgement.  Earlier
        # builds already decrypted the envelope (key index 63), but discarded
        # its map ID.  That made non-leaders depend on the leader-only client
        # selection packet and left their automatic stop detector unarmed.
        #
        # Byte 4 is the verified scene-release record kind and bytes 5..8 are
        # the map being released.  This is source-map/leave evidence, not a
        # destination claim: the Garden/Haven return still requires the later
        # transition + resource/map confirmation chain.
        from .tcp_profile_codec import decode_registered_server_tcp_envelope

        envelope = decode_registered_server_tcp_envelope(
            payload,
            connection_context=connection_context,
            validator=lambda plaintext: (
                len(plaintext) == 319
                and plaintext[2:4] == b"\x0c\x05"
                and plaintext[4] == 0x04
                and _UINT32_LE.unpack_from(plaintext, 5)[0] > 0
            ),
        )
        if envelope is None:
            return None
        plaintext, key_index, _rounds, _source = envelope
        if (
            len(plaintext) != 319
            or plaintext[2:4] != b"\x0c\x05"
            or plaintext[4] != 0x04
        ):
            return None
        map_id = _UINT32_LE.unpack_from(plaintext, 5)[0]
        if map_id <= 0:
            return None
        return DecodedV576MapIdentity(
            map_id=map_id,
            xor_phase=-1,
            record_offset=5,
            layout="s2c-tcp-scene-map-release-322-v576",
            key_index=key_index,
            active=False,
        )

    if (
        direction in (None, "server_to_client")
        and len(payload) == 14
        and int.from_bytes(payload[:2], "little") == len(payload)
    ):
        # Import lazily so the low-level frame module does not make the TCP
        # profile decoder a mandatory import for UDP-only consumers.
        from .tcp_profile_codec import decode_registered_server_tcp_envelope

        envelope = decode_registered_server_tcp_envelope(
            payload,
            connection_context=connection_context,
            validator=lambda plaintext: (
                len(plaintext) == 11
                and plaintext[2:4] == b"\x82\x45"
                and _UINT32_LE.unpack_from(plaintext, 4)[0] > 0
                and plaintext[9] == 0
            ),
        )
        if envelope is None:
            return None
        plaintext, key_index, _rounds, _source = envelope
        # 45 82 is the scene-confirm opcode. Byte 8 is the scene state. The
        # older Black Dragon samples used only state 0/1 and a zero suffix;
        # Guardian/Trial/Mist/Bull world servers also emit later stage states
        # and a small connection-generation flag in byte 10. Byte 9 remains
        # the stable zero separator. Treat only state 1 with zero flags as a
        # proven leave; the other variants are active scene confirmations.
        if len(plaintext) != 11 or plaintext[2:4] != b"\x82\x45":
            return None
        scene_state = int(plaintext[8])
        scene_flags = int.from_bytes(plaintext[9:11], "little")
        if plaintext[9] != 0:
            return None
        map_id = _UINT32_LE.unpack_from(plaintext, 4)[0]
        return DecodedV576MapIdentity(
            map_id=map_id,
            xor_phase=-1,
            record_offset=4,
            layout="s2c-tcp-map-confirm-14-v576",
            key_index=key_index,
            active=not (scene_state == 1 and scene_flags == 0),
            scene_state=scene_state,
            scene_flags=scene_flags,
        )

    if (
        direction in (None, "server_to_client")
        and len(payload) == 40
        and int.from_bytes(payload[:2], "little") == len(payload)
    ):
        # Towns/halls are initialized through a different server scene packet
        # than battle maps. Across the full diagnostic corpus, this exact
        # structure resolved independently to both Saint Haven (11) and the
        # Arena (40); the destination map is the u32 immediately following the
        # 03 01 opcode. Restricting the decoder to the verified envelope length
        # and opcode avoids treating unrelated small integers as map IDs.
        from .tcp_profile_codec import decode_registered_server_tcp_envelope

        envelope = decode_registered_server_tcp_envelope(
            payload,
            connection_context=connection_context,
            validator=lambda plaintext: (
                len(plaintext) == 37
                and plaintext[2:4] == b"\x03\x01"
                and _UINT32_LE.unpack_from(plaintext, 4)[0] > 0
            ),
        )
        if envelope is None:
            return None
        plaintext, key_index, _rounds, _source = envelope
        if len(plaintext) != 37 or plaintext[2:4] != b"\x03\x01":
            return None
        map_id = _UINT32_LE.unpack_from(plaintext, 4)[0]
        return DecodedV576MapIdentity(
            map_id=map_id,
            xor_phase=-1,
            record_offset=4,
            layout="s2c-tcp-town-map-init-40-v576",
            key_index=key_index,
            active=True,
        )

    return None


def decode_v576_town_scene_load(
    payload: bytes,
    direction: str | None = None,
    *,
    connection_context: object | None = None,
) -> bool:
    """Recognize the server's large town-scene initialization record.

    This 03 18 message was present on the Heaven return in all eight
    selection/load tests, including Guardian practice, while the later 03 01
    exact town-map packet was sometimes lost behind TCP reassembly gaps. It
    does not claim a particular town ID and is only an exit signal after a
    server-confirmed battle-map load.
    """

    if (
        direction not in (None, "server_to_client")
        or len(payload) != 3143
        or int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return False
    from .tcp_profile_codec import decode_registered_server_tcp_envelope

    return decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=lambda plaintext: (
            len(plaintext) == 3140
            and plaintext[2:4] == b"\x03\x18"
            and plaintext[4:8] == b"\x01\x00\x00\x00"
        ),
    ) is not None


@dataclass(frozen=True, slots=True)
class DecodedV576BossAction:
    """A server-side action emitted by a confirmed Boss runtime actor."""

    source_actor_id: int
    action_code: int
    variant_code: int
    action_kind: int
    xor_phase: int
    record_offset: int
    payload_length: int
    packet_signature: str
    layout: str = "s2c-udp-boss-action-v576"
    skill_index: int = 0


@dataclass(frozen=True, slots=True)
class DecodedV576PlayerLifecycle:
    actor_id: int
    active: bool
    xor_phase: int
    record_offset: int
    layout: str = "s2c-udp-player-lifecycle-0511-v576"


@dataclass(frozen=True, slots=True)
class DecodedV576PlayerHealth:
    actor_id: int
    current_hp: int
    max_hp: int
    current_mp: int
    max_mp: int
    hp_delta: int
    xor_phase: int
    record_offset: int
    layout: str = "s2c-udp-player-resource-sync-210c-v576"
    # The outer actor at decoded bytes 10..13 identifies the client whose
    # party/resource view produced this server packet.  It is the local
    # player even when the nested HP/MP record belongs to a teammate.
    observer_actor_id: int = 0
    # ``08 6F`` publishes the post-hit HP as a signed integer.  Lethal hits
    # can underflow below zero; ``current_hp`` stays clamped for ordinary
    # player-health consumers, while this field preserves the factual wire
    # value so summon destruction can be distinguished from a delta record's
    # unknown ``current_hp = 0`` placeholder.
    raw_current_hp: int | None = None


def decode_v576_player_health(
    payload: bytes,
    direction: str | None = None,
    known_player_actor_ids: Iterable[int] | None = None,
    observer_actor_ids: Iterable[int] | None = None,
) -> tuple[DecodedV576PlayerHealth, ...]:
    """Decode authoritative party HP states, resource sync and HP deltas.

    The combat HP bar is published as ``actor + 08 6F 00`` with current HP at
    ``actor + 31``. These records are the authoritative source for effective
    life loss: callers must difference adjacent absolute states instead of
    replaying damage/healing notifications. Entry resource records use
    ``actor + 21 0C 00 + uint64 HP + uint32 MP`` and may contain both current
    and maximum values. ``1C 1B 00 04`` remains available as a signed HP
    notification for source attribution only. ``1F 04 00`` is MP, not HP.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 28
    ):
        return ()
    known_actors = frozenset(
        int(value)
        for value in (known_player_actor_ids or ())
        if int(value) > 0
    )
    observer_actors = frozenset(
        int(value)
        for value in (observer_actor_ids or ())
        if int(value) > 0
    )
    markers = (
        b"\x08\x6f\x00",
        b"\x21\x0c\x00",
        b"\x1c\x1b\x00\x04",
        b"\x1c\x1b\x00\x05",
    )
    if not _v576_wire_may_contain_marker(
        payload, markers, start_offset=13
    ):
        return ()

    signature = payload[5:9] if len(payload) >= 9 else b""
    preferred_phase = (
        _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
        if signature
        else None
    )
    phases = (
        (preferred_phase,)
        if preferred_phase is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )

    def records_for_phase(
        xor_phase: int,
    ) -> tuple[DecodedV576PlayerHealth, ...]:
        decoded = _decode_v576_body(payload, xor_phase)
        rows: list[DecodedV576PlayerHealth] = []

        # Party current-HP state. The outer observer at +10 must be the local
        # player when that identity is known; this excludes similarly shaped
        # nested records belonging to another message context.
        state_marker = b"\x08\x6f\x00"
        observer_actor_id = (
            int.from_bytes(decoded[10:14], "little")
            if len(decoded) >= 14
            else 0
        )
        allow_state_records = (
            bool(known_actors)
            and (
                not observer_actors
                or observer_actor_id in observer_actors
            )
        )
        marker_offset = decoded.find(state_marker, 13)
        while marker_offset >= 0:
            actor_offset = marker_offset - 4
            hp_offset = actor_offset + 31
            actor_id = (
                int.from_bytes(decoded[actor_offset:marker_offset], "little")
                if actor_offset >= _V576_BODY_OFFSET
                else 0
            )
            hp = (
                int.from_bytes(
                    decoded[hp_offset : hp_offset + 8],
                    "little",
                    signed=True,
                )
                if hp_offset + 8 <= len(decoded)
                else -50_000_001
            )
            if (
                allow_state_records
                and actor_id in known_actors
                and -50_000_000 <= hp <= 50_000_000
            ):
                rows.append(
                    DecodedV576PlayerHealth(
                        actor_id=actor_id,
                        # Lethal updates underflow below zero on the wire.
                        # The party HP bar is factual zero at that point.
                        current_hp=max(0, hp),
                        max_hp=0,
                        current_mp=0,
                        max_mp=0,
                        hp_delta=0,
                        xor_phase=xor_phase,
                        record_offset=actor_offset,
                        layout="s2c-udp-player-hp-state-086f-v576",
                        observer_actor_id=observer_actor_id,
                        raw_current_hp=hp,
                    )
                )
            marker_offset = decoded.find(state_marker, marker_offset + 1)

        absolute_marker = b"\x21\x0c\x00"
        absolute_by_actor: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        marker_offset = decoded.find(absolute_marker, 13)
        while marker_offset >= 0:
            actor_offset = marker_offset - 4
            value_offset = marker_offset + len(absolute_marker)
            actor_id = (
                int.from_bytes(decoded[actor_offset:marker_offset], "little")
                if actor_offset >= _V576_BODY_OFFSET
                else 0
            )
            hp = (
                int.from_bytes(decoded[value_offset : value_offset + 8], "little")
                if value_offset + 12 <= len(decoded)
                else -1
            )
            mp = (
                int.from_bytes(decoded[value_offset + 8 : value_offset + 12], "little")
                if value_offset + 12 <= len(decoded)
                else -1
            )
            if (
                actor_id > 0
                and 0 < hp <= 50_000_000
                and 0 <= mp <= 50_000_000
            ):
                absolute_by_actor[actor_id].append((actor_offset, hp, mp))
            marker_offset = decoded.find(absolute_marker, marker_offset + 1)

        for actor_id, values in absolute_by_actor.items():
            hp_values = [value[1] for value in values]
            mp_values = [value[2] for value in values]
            rows.append(
                DecodedV576PlayerHealth(
                    actor_id=actor_id,
                    current_hp=min(hp_values),
                    max_hp=max(hp_values),
                    current_mp=min(mp_values),
                    max_mp=max(mp_values),
                    hp_delta=0,
                    xor_phase=xor_phase,
                    record_offset=min(value[0] for value in values),
                    observer_actor_id=observer_actor_id,
                )
            )

        delta_marker = b"\x1c\x1b\x00"
        marker_offset = decoded.find(delta_marker, 13)
        while marker_offset >= 0:
            actor_offset = marker_offset - 4
            if actor_offset < _V576_BODY_OFFSET or marker_offset + 30 > len(decoded):
                marker_offset = decoded.find(delta_marker, marker_offset + 1)
                continue
            actor_id = int.from_bytes(decoded[actor_offset:marker_offset], "little")
            hp_delta = int.from_bytes(
                decoded[marker_offset + 4 : marker_offset + 12],
                "little",
                signed=True,
            )
            tail = decoded[marker_offset + 16 : marker_offset + 30]
            subtype = decoded[marker_offset + 3]
            if (
                actor_id > 0
                and subtype in (4, 5)
                and (not known_actors or actor_id in known_actors)
                and tail[:2] == b"\x00\x01"
                and (subtype == 4 or tail in (
                    bytes.fromhex("00010000000000010000ffffffff"),
                    bytes.fromhex("00010000000000012c00ffffffff"),
                    bytes.fromhex("0001000000000001a100ffffffff"),
                    bytes.fromhex("0001000000000001b100ffffffff"),
                ))
                and 0 < abs(hp_delta) <= 50_000_000
            ):
                rows.append(
                    DecodedV576PlayerHealth(
                        actor_id=actor_id,
                        current_hp=0,
                        max_hp=0,
                        current_mp=0,
                        max_mp=0,
                        hp_delta=hp_delta,
                        xor_phase=xor_phase,
                        record_offset=actor_offset,
                        layout=f"s2c-udp-player-hp-delta-1c1b000{subtype}-v576",
                        observer_actor_id=observer_actor_id,
                    )
                )
            marker_offset = decoded.find(delta_marker, marker_offset + 1)
        return tuple(rows)

    preferred_rows = records_for_phase(phases[0])
    if preferred_rows or preferred_phase is not None:
        return preferred_rows

    by_phase = [records_for_phase(phase) for phase in phases]
    nonempty = [rows for rows in by_phase if rows]
    if len(nonempty) != 1:
        return ()
    return nonempty[0]


def decode_v576_player_lifecycle(
    payload: bytes,
    direction: str | None = None,
    known_player_actor_ids: Iterable[int] | None = None,
) -> tuple[DecodedV576PlayerLifecycle, ...]:
    """Decode the actor ``05 11`` flag used by visible party life state.

    The client clears a remote party member's HP bar when this flag changes
    to zero and restores it when the flag returns to one. The same low-level
    flag can be retransmitted, so this function reports factual wire records;
    death/revive interpretation and deduplication belong to the aggregator.
    """
    # This flag is authoritative only on confirmed server-to-client traffic.
    # Accelerator relays can make both UDP endpoints local, leaving direction
    # unknown.  Some unrelated 45-byte relay packets then happen to match the
    # actor/marker/state layout and would falsely remove every player Buff.
    if (
        direction != "server_to_client"
        or len(payload) < 18
        or not known_player_actor_ids
    ):
        return ()
    known_actors = frozenset(
        int(value) for value in known_player_actor_ids if int(value) > 0
    )
    if not known_actors or not _v576_wire_may_contain_marker(
        payload, (b"\x11\x05\x00",), start_offset=13
    ):
        return ()

    signature = payload[5:9] if len(payload) >= 9 else b""
    preferred_phase = (
        _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
        if signature
        else None
    )
    phases = (
        (preferred_phase,)
        if preferred_phase is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )

    def records_for_phase(
        xor_phase: int,
    ) -> tuple[DecodedV576PlayerLifecycle, ...]:
        decoded = _decode_v576_body(payload, xor_phase)
        marker = b"\x11\x05\x00"
        rows: dict[tuple[int, bool, int], DecodedV576PlayerLifecycle] = {}
        marker_offset = decoded.find(marker, 13)
        while marker_offset >= 0:
            actor_offset = marker_offset - 4
            state_offset = marker_offset + len(marker)
            actor_id = (
                int.from_bytes(decoded[actor_offset:marker_offset], "little")
                if actor_offset >= _V576_BODY_OFFSET
                else 0
            )
            if (
                actor_id in known_actors
                and state_offset < len(decoded)
                and decoded[state_offset] in (0, 1)
            ):
                row = DecodedV576PlayerLifecycle(
                    actor_id=actor_id,
                    active=bool(decoded[state_offset]),
                    xor_phase=xor_phase,
                    record_offset=actor_offset,
                )
                rows[(actor_id, row.active, actor_offset)] = row
            marker_offset = decoded.find(marker, marker_offset + 1)
        return tuple(rows.values())

    preferred_rows = records_for_phase(phases[0])
    if preferred_rows or preferred_phase is not None:
        return preferred_rows

    by_phase = [records_for_phase(phase) for phase in phases]
    nonempty = [rows for rows in by_phase if rows]
    if len(nonempty) != 1:
        return ()
    return nonempty[0]


def decode_v576_monster_spawn(
    payload: bytes,
    direction: str | None = None,
    known_template_ids: Iterable[int] | None = None,
) -> DecodedV576MonsterSpawn | None:
    """Decode the server entity creation record used by v576 monsters.

    Runtime actor IDs are short-lived and may be reused after a stage change.
    Each spawn record therefore has to refresh the actor-to-template mapping.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) != 88
        or payload[5:9] != b"\x06\x27\x55\x00"
    ):
        return None
    decoded = _decode_v576_body(payload, 5)
    actor_id = int.from_bytes(decoded[10:14], "little")
    template_id = int.from_bytes(decoded[18:22], "little")
    if actor_id <= 0 or template_id <= 0:
        return None
    if known_template_ids is not None:
        known = known_template_ids
        if not isinstance(known, (set, frozenset)):
            known = frozenset(int(value) for value in known)
        if template_id not in known:
            return None
    return DecodedV576MonsterSpawn(
        actor_id=actor_id,
        template_id=template_id,
        payload_length=len(payload),
    )


def decode_v576_boss_skill_starts(
    payload: bytes,
    direction: str | None,
    known_boss_actor_ids: Iterable[int],
    known_skill_ids: Iterable[int],
) -> tuple[DecodedV576BossAction, ...]:
    """Explicit server skill starts, not ACT-index guesses.

    Calibrated against both 34023 records in 语音测试.dpscap: actor u32,
    0e 16 00, skill u32, mode 01, then 17 bytes of runtime parameters.
    Require a known Boss, catalogued skill and a unique valid cipher phase.
    Marker prefilter keeps unrelated UDP packets off this path.
    """
    marker = b"\x0e\x16\x00"
    if (direction != "server_to_client" or len(payload) < 38
            or not known_boss_actor_ids or not known_skill_ids
            or not _v576_wire_may_contain_marker(payload, (marker,), start_offset=13)):
        return ()
    actors = set(known_boss_actor_ids)
    skills = set(known_skill_ids)
    by_phase = {}
    for phase in range(len(_V576_BODY_XOR_KEY)):
        body = _decode_v576_body(payload, phase)
        rows = []
        offset = body.find(marker, 13)
        while offset >= 0:
            actor = int.from_bytes(body[offset-4:offset], "little")
            skill = int.from_bytes(body[offset+3:offset+7], "little")
            if (offset + 25 <= len(body) and actor in actors and skill in skills
                    and body[offset+7] == 1):
                rows.append(DecodedV576BossAction(
                    source_actor_id=actor, action_code=0, variant_code=0,
                    action_kind=1, xor_phase=phase, record_offset=offset-4,
                    payload_length=len(payload), packet_signature=payload[5:9].hex(),
                    layout="s2c-udp-boss-skill-start-0e16-v576", skill_index=skill))
            offset = body.find(marker, offset+1)
        if rows:
            by_phase[phase] = rows
    return tuple(next(iter(by_phase.values()))) if len(by_phase) == 1 else ()


_SELF_STATE_MARKER = b"\x13\x54\x00"
# 服务端状态实例 ID = 技能号 + 该偏移，用来排除「碰巧匹配」的里程碑字节。
_V576_STATE_INSTANCE_OFFSET = 250_000_000


def decode_v576_self_state_applies(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int],
) -> tuple[tuple[int, int], ...]:
    """Decode server **self-applied** status records (owner is a player).

    梦魇龙巢的换边印记（770008 地脉诅咒）是服务端直接挂在玩家身上的，
    包里**没有 Boss actor**：

        marker 13 54 00 | flag(u8) | owner(u32) | FF FF FF FF
                        | skill(u32) | 实例ID(u32, = skill + 250000000) | EffectClass(u32) | ...

    ``owner`` 是玩家，所以 ``decode_v576_boss_skill_starts`` 那条
    「actor 必须是 Boss」的路永远看不到它。这里用 marker + ``FF FF FF FF``
    + 实例 ID 三重校验，返回 ``(owner_actor_id, skill_index)``，同包去重。
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 30
        or not known_skill_ids
    ):
        return ()
    skills = (
        known_skill_ids
        if isinstance(known_skill_ids, (set, frozenset))
        else frozenset(int(value) for value in known_skill_ids)
    )
    if not skills or not _v576_wire_may_contain_marker(
        payload, (_SELF_STATE_MARKER,), start_offset=9
    ):
        return ()

    by_phase: dict[int, list[tuple[int, int]]] = {}
    for phase in range(len(_V576_BODY_XOR_KEY)):
        body = _decode_v576_body(payload, phase)
        rows: list[tuple[int, int]] = []
        offset = body.find(_SELF_STATE_MARKER, 9)
        while offset >= 0:
            if offset + 20 <= len(body) and body[offset + 8:offset + 12] == b"\xff\xff\xff\xff":
                owner = int.from_bytes(body[offset + 4:offset + 8], "little")
                skill = int.from_bytes(body[offset + 12:offset + 16], "little")
                instance = int.from_bytes(body[offset + 16:offset + 20], "little")
                if (
                    owner
                    and skill in skills
                    and instance == skill + _V576_STATE_INSTANCE_OFFSET
                ):
                    rows.append((owner, skill))
            offset = body.find(_SELF_STATE_MARKER, offset + 1)
        if rows:
            by_phase[phase] = rows
    if not by_phase:
        return ()
    # 同一记录可能在多个 phase 下重复命中，取命中最多的一组（并列时取最小 phase）。
    best_phase = max(sorted(by_phase), key=lambda phase: len(by_phase[phase]))
    return tuple(dict.fromkeys(by_phase[best_phase]))


def decode_v576_boss_actions(
    payload: bytes,
    direction: str | None = None,
    known_boss_actor_ids: Iterable[int] | None = None,
) -> tuple[DecodedV576BossAction, ...]:
    """Decode v576 server actions owned by already-confirmed Boss actors.

    The record is anchored by the runtime actor ID followed by ``03 14 00``.
    A packet signature normally identifies the XOR phase. If that hint fails,
    all phases are probed and accepted only when exactly one phase produces a
    plausible record. This keeps unknown packet families visible without
    turning coincidental bytes into Boss skills.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 20
        or not known_boss_actor_ids
    ):
        return ()
    known_actors = (
        known_boss_actor_ids
        if isinstance(known_boss_actor_ids, (set, frozenset))
        else frozenset(int(value) for value in known_boss_actor_ids)
    )
    if not known_actors or not _v576_wire_may_contain_marker(
        payload, (b"\x03\x14\x00",), start_offset=13
    ):
        return ()

    signature = payload[5:9] if len(payload) >= 9 else b""
    cached_phase = _V576_BOSS_ACTION_PHASE_BY_SIGNATURE.get(signature)
    signature_phase = (
        _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
        if signature
        else None
    )
    preferred_phase = cached_phase if cached_phase is not None else signature_phase

    def records_for_phase(
        xor_phase: int,
    ) -> list[tuple[int, int, int, int, int]]:
        decoded = _decode_v576_body(payload, xor_phase)
        records: set[tuple[int, int, int, int, int]] = set()
        marker = b"\x03\x14\x00"
        marker_offset = decoded.find(marker, 13)
        while marker_offset >= 0:
            actor_offset = marker_offset - 4
            action_offset = marker_offset + len(marker)
            actor_id = (
                int.from_bytes(decoded[actor_offset:marker_offset], "little")
                if actor_offset >= 9
                else 0
            )
            if actor_id in known_actors and action_offset + 6 <= len(decoded):
                action_code = decoded[action_offset]
                variant_code = int.from_bytes(
                    decoded[action_offset + 1 : action_offset + 3], "little"
                )
                action_kind = decoded[action_offset + 3]
                # Every calibrated action record reserves two zero bytes
                # after its kind field. They are a useful structural guard
                # against finding the marker in unrelated payload data.
                if decoded[action_offset + 4 : action_offset + 6] == b"\x00\x00":
                    records.add(
                        (
                            actor_offset,
                            actor_id,
                            action_code,
                            variant_code,
                            action_kind,
                        )
                    )
            marker_offset = decoded.find(marker, marker_offset + 1)
        return sorted(records)

    by_phase: dict[int, list[tuple[int, int, int, int, int]]] = {}
    if preferred_phase is not None:
        preferred_records = records_for_phase(preferred_phase)
        if preferred_records:
            by_phase[preferred_phase] = preferred_records
    if not by_phase:
        for xor_phase in range(len(_V576_BODY_XOR_KEY)):
            if xor_phase == preferred_phase:
                continue
            records = records_for_phase(xor_phase)
            if records:
                by_phase[xor_phase] = records
        if len(by_phase) != 1:
            return ()

    selected_phase = next(iter(by_phase))
    _V576_BOSS_ACTION_PHASE_BY_SIGNATURE[signature] = selected_phase
    signature_text = signature.hex()
    return tuple(
        DecodedV576BossAction(
            source_actor_id=actor_id,
            action_code=action_code,
            variant_code=variant_code,
            action_kind=action_kind,
            xor_phase=selected_phase,
            record_offset=offset,
            payload_length=len(payload),
            packet_signature=signature_text,
        )
        for offset, actor_id, action_code, variant_code, action_kind in by_phase[
            selected_phase
        ]
    )


def decode_v576_boss_structured_actions(
    payload: bytes,
    direction: str | None = None,
    known_boss_actor_ids: Iterable[int] | None = None,
) -> tuple[DecodedV576BossAction, ...]:
    """Extract variable-length action records owned by confirmed Bosses.

    These records use the same structural marker families as player actions,
    but their four-byte action identifiers are not present in the player skill
    catalog. Actor ownership, structural bounds and an unambiguous XOR phase
    are therefore all required before a record is accepted.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 25
        or not known_boss_actor_ids
    ):
        return ()
    known_actors = (
        known_boss_actor_ids
        if isinstance(known_boss_actor_ids, (set, frozenset))
        else frozenset(int(value) for value in known_boss_actor_ids)
    )
    if not known_actors or not _v576_wire_may_contain_marker(
        payload,
        (b"\x0e\x23\x00", b"\x0e\x1f\x00", b"\x12\x24\x00", b"\x12\x20\x00"),
        start_offset=14,
    ):
        return ()

    signature = payload[5:9]
    cached_phase = _V576_BOSS_STRUCTURED_PHASE_BY_SIGNATURE.get(signature)
    signature_phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
    preferred_phase = cached_phase if cached_phase is not None else signature_phase
    markers = (
        b"\x0e\x23\x00",
        b"\x0e\x1f\x00",
        b"\x12\x24\x00",
        b"\x12\x20\x00",
    )

    def records_for_phase(
        xor_phase: int,
    ) -> list[tuple[int, int, int, int, int]]:
        decoded = _decode_v576_body(payload, xor_phase)
        records: set[tuple[int, int, int, int, int]] = set()
        for marker_index, marker in enumerate(markers, start=1):
            marker_offset = decoded.find(marker, 14)
            while marker_offset >= 0 and marker_offset + 7 <= len(decoded):
                actor_offset = marker_offset - 4
                actor_id = (
                    int.from_bytes(decoded[actor_offset:marker_offset], "little")
                    if actor_offset >= 9
                    else 0
                )
                action_code = int.from_bytes(
                    decoded[marker_offset + 3 : marker_offset + 7], "little"
                )
                if (
                    actor_id in known_actors
                    and 0 < action_code <= 0x0FFFFFFF
                ):
                    records.add(
                        (
                            actor_offset,
                            actor_id,
                            action_code,
                            marker_index,
                            marker[0],
                        )
                    )
                marker_offset = decoded.find(marker, marker_offset + 1)
        return sorted(records)

    by_phase: dict[int, list[tuple[int, int, int, int, int]]] = {}
    if preferred_phase is not None:
        records = records_for_phase(preferred_phase)
        if records:
            by_phase[preferred_phase] = records
        else:
            # A known signature already fixes the phase. Most server packets
            # are not Boss actions, so probing all twelve phases here would
            # multiply replay cost without adding independent evidence.
            return ()
    else:
        for xor_phase in range(len(_V576_BODY_XOR_KEY)):
            records = records_for_phase(xor_phase)
            if records:
                by_phase[xor_phase] = records
        if len(by_phase) != 1:
            return ()

    selected_phase = next(iter(by_phase))
    _V576_BOSS_STRUCTURED_PHASE_BY_SIGNATURE[signature] = selected_phase
    signature_text = signature.hex()
    return tuple(
        DecodedV576BossAction(
            source_actor_id=actor_id,
            action_code=action_code,
            variant_code=marker_index,
            action_kind=action_kind,
            xor_phase=selected_phase,
            record_offset=offset,
            payload_length=len(payload),
            packet_signature=signature_text,
            layout="s2c-udp-boss-action-structured-multi-v576",
        )
        for offset, actor_id, action_code, marker_index, action_kind in by_phase[
            selected_phase
        ]
    )


def split_v576_payload(payload: bytes) -> tuple[bytes, ...]:
    """Split an offload batch without losing known messages around unknown ones."""
    if len(payload) < 9:
        return (payload,)
    parts: list[bytes] = []
    offset = 0
    while offset < len(payload):
        if offset + 9 > len(payload):
            parts.append(payload[offset:])
            break
        message_length = _V576_MESSAGE_LENGTH_BY_SIGNATURE.get(
            payload[offset + 5 : offset + 9]
        )
        if message_length is not None and offset + message_length <= len(payload):
            parts.append(payload[offset : offset + message_length])
            offset += message_length
            continue

        # Receive offload can batch known hit records together with message
        # layouts that have not been decoded yet. Preserve the unknown prefix,
        # then resume at the next confidently recognised v576 signature.
        next_offset: int | None = None
        # The signature starts five bytes after a message boundary. Searching
        # the compiled byte pattern is equivalent to the former byte-by-byte
        # dictionary probe, but performs the long unknown-prefix scan in C.
        signature_match = _V576_MESSAGE_SIGNATURE_PATTERN.search(
            payload, offset + 6
        )
        while signature_match is not None:
            candidate = signature_match.start() - 5
            candidate_length = _V576_MESSAGE_LENGTH_BY_SIGNATURE[
                signature_match.group()
            ]
            if candidate + candidate_length <= len(payload):
                next_offset = candidate
                break
            signature_match = _V576_MESSAGE_SIGNATURE_PATTERN.search(
                payload, signature_match.start() + 1
            )
        if next_offset is None:
            parts.append(payload[offset:])
            break
        parts.append(payload[offset:next_offset])
        offset = next_offset
    return tuple(part for part in parts if part) or (payload,)


_TCP_FIN = 0x01
_TCP_SYN = 0x02
_TCP_RST = 0x04
_TCP_PSH = 0x08
_TCP_MESSAGE_BOUNDARY_FLAGS = _TCP_FIN | _TCP_RST


@dataclass(slots=True)
class _TcpSegment:
    packet: PacketRecord
    sequence: int
    payload: bytes
    flags: int


@dataclass(slots=True)
class _TcpFlowState:
    next_sequence: int | None = None
    pending: dict[int, _TcpSegment] = field(default_factory=dict)
    pending_sequences: list[int] = field(default_factory=list)
    pending_revision: int = 0
    application_buffer: bytearray = field(default_factory=bytearray)
    application_timestamp: float = 0.0
    application_packet: PacketRecord | None = None
    last_timestamp: float = 0.0
    # Updated for every captured TCP packet, including retransmissions and
    # out-of-order segments that never reach ``_append_segment``.  The
    # always-on identity listener uses it to retire abandoned connections
    # instead of retaining every lobby/reconnect flow for the whole process.
    last_observed_timestamp: float = 0.0
    gap_started_timestamp: float = 0.0
    gap_last_probe_timestamp: float = 0.0
    gap_last_probe_revision: int = -1
    connection_generation: int = 0


def _packet_with_payload(packet: PacketRecord, payload: bytes) -> PacketRecord:
    return PacketRecord(
        timestamp_seconds=packet.timestamp_seconds,
        transport=packet.transport,
        source_ip=packet.source_ip,
        destination_ip=packet.destination_ip,
        source_port=packet.source_port,
        destination_port=packet.destination_port,
        payload=payload,
        captured_length=packet.captured_length,
        original_length=packet.original_length,
        tcp_sequence=packet.tcp_sequence,
        tcp_acknowledgment=packet.tcp_acknowledgment,
        tcp_flags=packet.tcp_flags,
        tcp_connection_generation=packet.tcp_connection_generation,
        direction_hint=packet.direction_hint,
    )


class V576TransportReassembler:
    """Turn captured UDP datagrams and TCP segments into logical messages.

    Dragon Nest combat events use UDP, while lobby and information-panel
    responses may use TCP. TCP retransmissions and out-of-order segments must
    be resolved before a response can be inspected. A PSH flag marks a socket
    flush, not necessarily the end of a v576 message, so complete application
    frames are emitted from their directional length headers and partial frames
    remain buffered across PSH segments. Client frames use a three-byte length;
    server frames use a two-byte length followed by a rolling sequence byte.
    """

    def __init__(
        self,
        *,
        idle_flush_seconds: float = 0.20,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        gap_recovery_seconds: float | None = None,
        flow_retention_seconds: float | None = None,
    ) -> None:
        self.idle_flush_seconds = max(0.02, float(idle_flush_seconds))
        self.max_buffer_bytes = max(64 * 1024, int(max_buffer_bytes))
        self.gap_recovery_seconds = max(
            self.idle_flush_seconds,
            float(gap_recovery_seconds)
            if gap_recovery_seconds is not None
            else self.idle_flush_seconds,
        )
        self.flow_retention_seconds = (
            max(self.idle_flush_seconds, float(flow_retention_seconds))
            if flow_retention_seconds is not None
            else None
        )
        self._flows: dict[tuple[str, int, str, int], _TcpFlowState] = {}
        self._udp_directions = UdpMirrorDirectionTracker()
        self.tls_probe = TlsStreamProbe()

    @staticmethod
    def _flow_key(packet: PacketRecord) -> tuple[str, int, str, int]:
        return (
            packet.source_ip,
            packet.source_port,
            packet.destination_ip,
            packet.destination_port,
        )

    @staticmethod
    def _logical_packets(packet: PacketRecord) -> tuple[PacketRecord, ...]:
        if packet.transport.upper() == "TCP":
            payloads = V576TransportReassembler._split_tcp_messages(
                packet.payload,
                packet.inferred_direction(),
            )
        else:
            payloads = split_v576_payload(packet.payload)
        if len(payloads) == 1 and payloads[0] is packet.payload:
            return (packet,)
        return tuple(
            _packet_with_payload(packet, payload)
            for payload in payloads
        )

    @staticmethod
    def _declared_tcp_message_length(
        payload: bytes | bytearray,
        offset: int = 0,
        direction: str | None = None,
    ) -> int | None:
        """Read the directional v576 TCP length without consuming its body."""

        if offset < 0 or offset + 3 > len(payload):
            return None
        byte0 = payload[offset]
        byte1 = payload[offset + 1]
        if direction == "server_to_client":
            return byte0 | (byte1 << 8)
        byte2 = payload[offset + 2]
        if direction == "client_to_server":
            return byte0 | (byte1 << 8) | (byte2 << 16)
        else:
            # Accelerators can make both endpoints appear private, preventing
            # IP-based direction inference. A non-zero third byte is then the
            # server rolling sequence; normal short client frames have a zero
            # high length byte. If it is zero, both interpretations agree.
            return (
                byte0 | (byte1 << 8)
                if byte2
                else byte0 | (byte1 << 8) | (byte2 << 16)
            )

    @staticmethod
    def _store_pending_segment(
        state: _TcpFlowState,
        segment: _TcpSegment,
    ) -> None:
        """Insert or replace one pending TCP segment in sequence order."""

        sequence = int(segment.sequence)
        existing = state.pending.get(sequence)
        if existing is not None and len(existing.payload) >= len(segment.payload):
            return
        if existing is None:
            insort(state.pending_sequences, sequence)
        state.pending[sequence] = segment
        state.pending_revision += 1

    @staticmethod
    def _pop_pending_segment(
        state: _TcpFlowState,
        sequence: int,
    ) -> _TcpSegment:
        sequence = int(sequence)
        segment = state.pending.pop(sequence)
        index = bisect_left(state.pending_sequences, sequence)
        if (
            index < len(state.pending_sequences)
            and state.pending_sequences[index] == sequence
        ):
            del state.pending_sequences[index]
        state.pending_revision += 1
        return segment

    @staticmethod
    def _clear_pending_segments(state: _TcpFlowState) -> None:
        if state.pending or state.pending_sequences:
            state.pending_revision += 1
        state.pending.clear()
        state.pending_sequences.clear()

    @staticmethod
    def _replace_pending_segments(
        state: _TcpFlowState,
        pending: dict[int, _TcpSegment],
    ) -> None:
        state.pending = pending
        state.pending_sequences = sorted(pending)
        state.pending_revision += 1

    @classmethod
    def _split_tcp_messages(
        cls,
        payload: bytes,
        direction: str | None = None,
    ) -> tuple[bytes, ...]:
        """Split a complete TCP burst by its directional length headers.

        TCP receive offload often joins several complete v576 messages into
        one PSH burst. Applying the UDP signature splitter to that burst can
        leave a valid profile attached to its following 7/8-byte marker. Only
        split when every declared message consumes the burst exactly; unknown
        or partial layouts therefore remain byte-for-byte intact.
        """
        if len(payload) < 6:
            return (payload,)
        parts: list[bytes] = []
        offset = 0
        while offset < len(payload):
            if offset + 3 > len(payload):
                return (payload,)
            message_length = cls._declared_tcp_message_length(
                payload,
                offset,
                direction,
            )
            if (
                message_length is None
                or message_length < 3
                or offset + message_length > len(payload)
            ):
                return (payload,)
            parts.append(payload[offset : offset + message_length])
            offset += message_length
        return tuple(parts) if len(parts) > 1 else (payload,)

    def _emit_buffer(self, state: _TcpFlowState) -> tuple[PacketRecord, ...]:
        if not state.application_buffer or state.application_packet is None:
            return ()
        packet = state.application_packet
        merged = PacketRecord(
            timestamp_seconds=state.application_timestamp,
            transport="TCP",
            source_ip=packet.source_ip,
            destination_ip=packet.destination_ip,
            source_port=packet.source_port,
            destination_port=packet.destination_port,
            payload=bytes(state.application_buffer),
            captured_length=packet.captured_length,
            original_length=packet.original_length,
            tcp_sequence=packet.tcp_sequence,
            tcp_acknowledgment=packet.tcp_acknowledgment,
            tcp_flags=packet.tcp_flags,
            tcp_connection_generation=packet.tcp_connection_generation,
        )
        state.application_buffer.clear()
        state.application_timestamp = 0.0
        state.application_packet = None
        return self._logical_packets(merged)

    def _emit_complete_frames(
        self, state: _TcpFlowState
    ) -> tuple[PacketRecord, ...]:
        """Emit every complete length-framed message at the buffer front."""
        emitted: list[PacketRecord] = []
        while (
            len(state.application_buffer) >= 3
            and state.application_packet is not None
        ):
            message_length = self._declared_tcp_message_length(
                state.application_buffer,
                0,
                state.application_packet.inferred_direction(),
            )
            if (
                message_length is None
                or message_length < 3
                or message_length > self.max_buffer_bytes
            ):
                break
            if len(state.application_buffer) < message_length:
                break
            packet = state.application_packet
            payload = bytes(state.application_buffer[:message_length])
            del state.application_buffer[:message_length]
            framed = PacketRecord(
                timestamp_seconds=state.application_timestamp,
                transport="TCP",
                source_ip=packet.source_ip,
                destination_ip=packet.destination_ip,
                source_port=packet.source_port,
                destination_port=packet.destination_port,
                payload=payload,
                captured_length=packet.captured_length,
                original_length=packet.original_length,
                tcp_sequence=packet.tcp_sequence,
                tcp_acknowledgment=packet.tcp_acknowledgment,
                tcp_flags=packet.tcp_flags,
                tcp_connection_generation=packet.tcp_connection_generation,
            )
            emitted.extend(self._logical_packets(framed))
        if not state.application_buffer:
            state.application_timestamp = 0.0
            state.application_packet = None
        return tuple(emitted)

    @staticmethod
    def _clear_gap_tracking(state: _TcpFlowState) -> None:
        state.gap_started_timestamp = 0.0
        state.gap_last_probe_timestamp = 0.0
        state.gap_last_probe_revision = -1

    @staticmethod
    def _pending_contiguous_prefix(
        state: _TcpFlowState,
        maximum_bytes: int,
    ) -> tuple[int, bytes, tuple[_TcpSegment, ...]] | None:
        """Return the first contiguous byte range held after a TCP gap.

        Capture loss can remove one or more TCP segments while every later
        segment remains available.  Pending segments may overlap because of
        retransmission or receive offload, so build one de-duplicated prefix
        before looking for an application-frame boundary.
        """
        if not state.pending_sequences:
            return None
        first_sequence = state.pending_sequences[0]
        first_row = state.pending.get(first_sequence)
        if first_row is None:
            return None
        start = first_row.sequence
        end = start
        output = bytearray()
        used: list[_TcpSegment] = []
        for sequence in state.pending_sequences:
            row = state.pending.get(sequence)
            if row is None:
                continue
            if row.sequence > end or len(output) >= maximum_bytes:
                break
            overlap = max(0, end - row.sequence)
            if overlap >= len(row.payload):
                continue
            remaining = maximum_bytes - len(output)
            addition = row.payload[overlap : overlap + remaining]
            if not addition:
                break
            output.extend(addition)
            end += len(addition)
            used.append(row)
            if len(addition) < len(row.payload) - overlap:
                break
        if not output:
            return None
        return start, bytes(output), tuple(used)

    def _tcp_resync_offset(
        self,
        payload: bytes,
        direction: str | None = None,
    ) -> int | None:
        """Find a strongly proven v576 TCP frame boundary after capture loss.

        A random encrypted byte sequence can accidentally resemble one length
        header, so recovery requires at least three consecutive frames whose
        declared lengths consume the available contiguous range exactly.  The
        exact-end requirement is what makes it safe to discard the incomplete
        pre-gap message without weakening normal out-of-order handling.
        """
        if len(payload) < 9:
            return None
        scan_limit = min(len(payload) - 2, self.max_buffer_bytes)
        payload_size = len(payload)

        # The former implementation restarted the complete frame walk at
        # every candidate byte.  After a capture gap that makes the recovery
        # cost quadratic in the buffered TCP range.  Whether an offset reaches
        # the exact end only depends on the already-known suffix beginning at
        # its declared next frame, so compute that suffix once, backwards.
        # ``chain_lengths[offset]`` is zero for an invalid suffix and otherwise
        # contains the number of complete frames ending exactly at payload_size.
        # Only the threshold ``>= 3`` matters, so a saturated byte per offset
        # avoids allocating a Python-int list (roughly 32 MiB at the 4 MiB
        # safety limit) for every recovery probe.
        chain_lengths = bytearray(payload_size + 1)
        chain = chain_lengths
        source = payload
        maximum = self.max_buffer_bytes
        size = payload_size

        # This is deliberately written as three direction-specific loops.
        # Keeping the length reader as a nested helper looked tidy, but one
        # large replay can execute it hundreds of millions of times while
        # recovering TCP gaps.  Inlining the three-byte arithmetic removes
        # that call overhead without changing the boundary proof.
        if direction == "server_to_client":
            for offset in range(size - 3, -1, -1):
                message_length = source[offset] | (source[offset + 1] << 8)
                if message_length < 3 or message_length > maximum:
                    continue
                next_offset = offset + message_length
                if next_offset == size:
                    chain[offset] = 1
                elif next_offset < size and chain[next_offset] > 0:
                    chain[offset] = min(3, chain[next_offset] + 1)
        elif direction == "client_to_server":
            for offset in range(size - 3, -1, -1):
                message_length = (
                    source[offset]
                    | (source[offset + 1] << 8)
                    | (source[offset + 2] << 16)
                )
                if message_length < 3 or message_length > maximum:
                    continue
                next_offset = offset + message_length
                if next_offset == size:
                    chain[offset] = 1
                elif next_offset < size and chain[next_offset] > 0:
                    chain[offset] = min(3, chain[next_offset] + 1)
        else:
            for offset in range(size - 3, -1, -1):
                byte2 = source[offset + 2]
                message_length = (
                    source[offset] | (source[offset + 1] << 8)
                    if byte2
                    else source[offset]
                    | (source[offset + 1] << 8)
                    | (byte2 << 16)
                )
                if message_length < 3 or message_length > maximum:
                    continue
                next_offset = offset + message_length
                if next_offset == size:
                    chain[offset] = 1
                elif next_offset < size and chain[next_offset] > 0:
                    chain[offset] = min(3, chain[next_offset] + 1)

        # Keep the original lowest-offset preference and the same conservative
        # proof requirement of at least three consecutive complete frames.
        for offset in range(scan_limit):
            if chain_lengths[offset] >= 3:
                return offset
        return None

    @staticmethod
    def _is_verified_scene_boundary(
        payloads: tuple[bytes, ...],
        direction: str | None,
        connection_context: object | None = None,
    ) -> bool:
        """Return whether a complete burst proves a scene boundary.

        A client behind an accelerator can lose one TCP segment immediately
        before the server's one-shot map-confirm/town-init packet.  Generic
        gap recovery deliberately requires three complete frames to avoid
        treating encrypted bytes as a length header, but that rule can leave
        the *single* return-to-Heaven frame buffered until capture shutdown.

        Map identity and scene teardown decoders validate the complete wire
        envelope, opcode and registered cipher profile.  They are therefore
        strong enough to form a safe one-frame resynchronisation point.  This
        exception is intentionally limited to those verified scene records.
        The independently verifiable teammate-inspection exception is handled
        separately by ``_is_verified_inspect_boundary``; ordinary combat TCP
        traffic keeps the existing three-frame proof requirement.
        """

        if not payloads:
            return False
        return any(
            decode_v576_map_identity(
                payload,
                direction,
                connection_context=connection_context,
            )
            is not None
            or decode_v576_scene_teardown(
                payload,
                direction,
                connection_context=connection_context,
            )
            is not None
            for payload in payloads
        )

    @staticmethod
    def _is_verified_inspect_boundary(
        payloads: tuple[bytes, ...],
        direction: str | None,
    ) -> bool:
        """Return whether a complete burst independently proves profile traffic.

        A memory-only listener can join an already active TCP connection, and
        packets mirrored by multiple Windows adapters can leave one direction
        parked behind an unrecoverable sequence gap.  Team inspection is a
        special safe resynchronisation point: the client request decrypts to
        the exact ``17 01`` open or ``03 15`` detail opcode, while the server
        response has the calibrated fixed-record profile shape.  Both request
        envelopes are fixed-length, full-key-table verified messages carrying
        a positive profile ID. Accept only those strong signatures; an
        arbitrary complete short frame must keep the normal three-frame gap
        proof requirement.
        """

        if not payloads:
            return False
        # Keep these imports local.  The profile codec imports protocol data
        # types during module initialization, while this path only runs after
        # both modules are fully loaded.
        from .tcp_profile_codec import (
            decode_tcp_inspect_request_payload,
            is_tcp_profile_payload_shape,
        )

        if direction != "server_to_client":
            for payload in payloads:
                request = decode_tcp_inspect_request_payload(payload)
                if request is not None and request.opcode in {
                    b"\x17\x01",
                    b"\x03\x15",
                }:
                    return True
        if direction != "client_to_server":
            return any(is_tcp_profile_payload_shape(payload) for payload in payloads)
        return False

    @staticmethod
    def _is_verified_settlement_boundary(
        payloads: tuple[bytes, ...], direction: str | None,
        connection_context: PacketRecord,
    ) -> bool:
        """A fully validated result can recover a lost pre-result TCP segment.

        Only full cipher/opcode/layout/roster validation is sufficient. A
        reward trigger or arbitrary same-length frame is not a resync proof.
        """
        if direction != "server_to_client":
            return False
        from .settlement import decode_settlement, may_contain_settlement

        return any(may_contain_settlement(payload) and decode_settlement(
            replace(connection_context, payload=payload), 0.0,
        ) is not None for payload in payloads)

    def _recover_stalled_gap(
        self,
        state: _TcpFlowState,
        timestamp_seconds: float,
        *,
        force_probe: bool = False,
    ) -> tuple[PacketRecord, ...]:
        """Resume at a verified application boundary after a persistent gap."""
        if (
            not state.pending
            or not state.gap_started_timestamp
            or timestamp_seconds - state.gap_started_timestamp
            < self.gap_recovery_seconds
        ):
            return ()
        if (
            state.gap_last_probe_timestamp
            and timestamp_seconds - state.gap_last_probe_timestamp
            < self.idle_flush_seconds
        ):
            return ()
        # Mirrored adapters and retransmissions can revisit the exact same
        # pending bytes. The preceding failed proof remains authoritative
        # until that byte range changes, so never rescan an unchanged prefix.
        if state.gap_last_probe_revision == state.pending_revision:
            return ()
        state.gap_last_probe_timestamp = timestamp_seconds
        state.gap_last_probe_revision = state.pending_revision

        prefix = self._pending_contiguous_prefix(state, self.max_buffer_bytes)
        if prefix is None:
            return ()
        prefix_sequence, prefix_payload, used = prefix
        # While a TCP send is still growing, the last captured segment often
        # ends inside an application frame. Exact-end recovery cannot succeed
        # before PSH/FIN/RST, so avoid scanning every intermediate segment.
        # Explicit stale and EOF flushes force one proof attempt for adapters
        # that did not preserve PSH.
        if (
            not force_probe
            and used
            and not (
                int(used[-1].flags)
                & (_TCP_PSH | _TCP_MESSAGE_BOUNDARY_FLAGS)
            )
        ):
            return ()
        recovery_direction = (
            used[0].packet.inferred_direction() if used else None
        )
        recovery_offset = self._tcp_resync_offset(
            prefix_payload,
            recovery_direction,
        )
        if recovery_offset is None and recovery_direction == "server_to_client":
            # A result may be the only surviving frame after a segment gap,
            # with an incomplete pre-result tail at the front of the burst.
            # Probe only the eight exact result sizes, never arbitrary bytes.
            from .settlement import may_contain_settlement
            for size in range(97, 623, 75):
                offset = len(prefix_payload) - size
                if offset < 0:
                    continue
                candidate = prefix_payload[offset:]
                if (int.from_bytes(candidate[:2], "little") == size
                        and may_contain_settlement(candidate)
                        and self._is_verified_settlement_boundary(
                            (candidate,), recovery_direction, used[0].packet,
                        )):
                    recovery_offset = offset
                    break
        if recovery_offset is None:
            return ()

        recovery_sequence = prefix_sequence + recovery_offset
        prefix_end = prefix_sequence + len(prefix_payload)
        boundary_packet = next(
            (
                row.packet
                for row in used
                if row.sequence <= recovery_sequence
                < row.sequence + len(row.payload)
            ),
            used[0].packet,
        )
        recovery_payload = prefix_payload[recovery_offset:]
        recovery_packet = PacketRecord(
            timestamp_seconds=boundary_packet.timestamp_seconds,
            transport="TCP",
            source_ip=boundary_packet.source_ip,
            destination_ip=boundary_packet.destination_ip,
            source_port=boundary_packet.source_port,
            destination_port=boundary_packet.destination_port,
            payload=recovery_payload,
            captured_length=boundary_packet.captured_length,
            original_length=boundary_packet.original_length,
            tcp_sequence=recovery_sequence,
            tcp_acknowledgment=boundary_packet.tcp_acknowledgment,
            # This prefix was rebuilt from several captured segments. Do not
            # inherit transport-boundary flags from just one source segment.
            tcp_flags=int(boundary_packet.tcp_flags)
            & ~(_TCP_SYN | _TCP_PSH | _TCP_MESSAGE_BOUNDARY_FLAGS),
            tcp_connection_generation=boundary_packet.tcp_connection_generation,
        )

        remaining: dict[int, _TcpSegment] = {}
        for row in state.pending.values():
            row_end = row.sequence + len(row.payload)
            if row_end <= prefix_end:
                continue
            sequence = row.sequence
            payload = row.payload
            if sequence < prefix_end:
                payload = payload[prefix_end - sequence :]
                sequence = prefix_end
            candidate = _TcpSegment(row.packet, sequence, payload, row.flags)
            existing = remaining.get(sequence)
            if existing is None or len(candidate.payload) > len(existing.payload):
                remaining[sequence] = candidate

        # The pre-gap application bytes can never be completed once recovery
        # is proven.  Drop only that partial message and the missing range;
        # every complete post-gap frame is preserved below.
        state.application_buffer.clear()
        state.application_timestamp = 0.0
        state.application_packet = None
        self._replace_pending_segments(state, remaining)
        state.next_sequence = recovery_sequence
        self._clear_gap_tracking(state)
        emitted = list(
            self._append_segment(
                state,
                _TcpSegment(
                    recovery_packet,
                    recovery_sequence,
                    recovery_payload,
                    int(recovery_packet.tcp_flags),
                ),
            )
        )
        while state.next_sequence in state.pending:
            pending = self._pop_pending_segment(
                state,
                int(state.next_sequence),
            )
            emitted.extend(self._append_segment(state, pending))
        if state.pending and state.next_sequence is not None:
            state.gap_started_timestamp = min(
                row.packet.timestamp_seconds for row in state.pending.values()
            )
        return tuple(emitted)

    def _append_segment(
        self,
        state: _TcpFlowState,
        segment: _TcpSegment,
    ) -> tuple[PacketRecord, ...]:
        if not state.application_buffer:
            state.application_timestamp = segment.packet.timestamp_seconds
            state.application_packet = segment.packet
        state.application_buffer.extend(segment.payload)
        state.next_sequence = segment.sequence + len(segment.payload)
        state.last_timestamp = segment.packet.timestamp_seconds
        emitted = list(self._emit_complete_frames(state))
        if (
            segment.flags & _TCP_MESSAGE_BOUNDARY_FLAGS
            or len(state.application_buffer) >= self.max_buffer_bytes
        ):
            emitted.extend(self._emit_buffer(state))
        elif segment.flags & _TCP_PSH and state.application_buffer:
            # Unknown/unframed PSH payloads retain the old burst behavior.
            # A valid but incomplete length frame is kept for the next TCP
            # segment, which is required by large card and engraving archives.
            if len(state.application_buffer) < 3:
                emitted.extend(self._emit_buffer(state))
            else:
                declared = self._declared_tcp_message_length(
                    state.application_buffer,
                    0,
                    state.application_packet.inferred_direction()
                    if state.application_packet is not None
                    else None,
                )
                if (
                    declared is None
                    or declared < 3
                    or declared > self.max_buffer_bytes
                ):
                    emitted.extend(self._emit_buffer(state))
        return tuple(emitted)

    def feed(self, packet: PacketRecord) -> tuple[PacketRecord, ...]:
        if packet.transport.upper() != "TCP":
            return tuple(
                recovered
                for logical in self._logical_packets(packet)
                for recovered in self._udp_directions.feed(logical)
            )

        key = self._flow_key(packet)
        if self.tls_probe.is_tls(packet):
            self._flows.pop(key, None)
            self._flows.pop((key[2], key[3], key[0], key[1]), None)
            return ()
        state = self._flows.setdefault(key, _TcpFlowState())
        state.last_observed_timestamp = max(
            state.last_observed_timestamp,
            float(packet.timestamp_seconds),
        )
        flags = int(packet.tcp_flags)
        if flags & _TCP_SYN:
            state.connection_generation += 1
        elif state.connection_generation <= 0:
            # Diagnostic captures often begin after the handshake. Such a
            # partial connection is still one stable selection scope.
            state.connection_generation = 1
        packet = replace(
            packet,
            tcp_connection_generation=state.connection_generation,
        )
        emitted: list[PacketRecord] = []
        if (
            state.application_buffer
            and state.last_timestamp
            and packet.timestamp_seconds - state.last_timestamp
            >= self.idle_flush_seconds
        ):
            # With an unresolved sequence gap this buffer is the truncated
            # front of a message, not an independent unframed packet.  Keep it
            # until the gap either closes or a later frame boundary is proven.
            if not state.pending:
                emitted.extend(self._emit_buffer(state))

        if flags & _TCP_RST:
            emitted.extend(self._emit_buffer(state))
            self._clear_pending_segments(state)
            state.next_sequence = None
            self._clear_gap_tracking(state)

        sequence = packet.tcp_sequence
        if sequence is None:
            if packet.payload:
                emitted.extend(self._logical_packets(packet))
            return tuple(emitted)
        payload_sequence = (int(sequence) + (1 if flags & _TCP_SYN else 0)) & 0xFFFFFFFF
        if flags & _TCP_SYN:
            emitted.extend(self._emit_buffer(state))
            self._clear_pending_segments(state)
            state.next_sequence = payload_sequence
            self._clear_gap_tracking(state)
        if not packet.payload:
            return tuple(emitted)
        if state.next_sequence is None:
            state.next_sequence = payload_sequence

        expected = int(state.next_sequence)
        segment_payload = packet.payload
        if payload_sequence < expected:
            overlap = expected - payload_sequence
            if overlap >= len(segment_payload):
                return tuple(emitted)
            segment_payload = segment_payload[overlap:]
            payload_sequence = expected
        if payload_sequence > expected:
            # A diagnostic capture can miss a small, already completed TCP
            # message while later messages remain intact.  If no application
            # frame or reordered segment is pending and this segment starts
            # with one or more complete protocol frames, resynchronize at the
            # proven frame boundary.  Waiting forever at the old sequence
            # otherwise hides every later inspect profile in replay files.
            direction = packet.inferred_direction()
            framed_parts = self._split_tcp_messages(
                segment_payload,
                direction,
            )
            complete_boundary = bool(framed_parts) and all(
                len(part) >= 3
                and self._declared_tcp_message_length(
                    part,
                    0,
                    direction,
                ) == len(part)
                for part in framed_parts
            )
            verified_scene_boundary = (
                complete_boundary
                and self._is_verified_scene_boundary(
                    framed_parts,
                    direction,
                    packet,
                )
            )
            verified_inspect_boundary = (
                complete_boundary
                and self._is_verified_inspect_boundary(
                    framed_parts,
                    direction,
                )
            )
            verified_resync_boundary = (
                verified_scene_boundary or verified_inspect_boundary
                or (complete_boundary and self._is_verified_settlement_boundary(
                    framed_parts, direction, packet,
                ))
            )
            if (
                complete_boundary
                and (
                    verified_resync_boundary
                    or (
                        not state.application_buffer
                        and not state.pending
                    )
                )
            ):
                if verified_resync_boundary:
                    # A verified map, inspect or result boundary invalidates an
                    # incomplete earlier frame. Keeping it would prevent the
                    # only scene record, profile or result from reaching its
                    # decoder until the TCP connection is recreated.
                    state.application_buffer.clear()
                    state.application_timestamp = 0.0
                    state.application_packet = None
                    self._clear_pending_segments(state)
                state.next_sequence = payload_sequence
                expected = payload_sequence
                self._clear_gap_tracking(state)
            else:
                self._store_pending_segment(
                    state,
                    _TcpSegment(
                        packet,
                        payload_sequence,
                        segment_payload,
                        flags,
                    ),
                )
                if not state.gap_started_timestamp:
                    state.gap_started_timestamp = packet.timestamp_seconds
                emitted.extend(
                    self._recover_stalled_gap(
                        state,
                        packet.timestamp_seconds,
                    )
                )
                return tuple(emitted)

        emitted.extend(
            self._append_segment(
                state,
                _TcpSegment(packet, payload_sequence, segment_payload, flags),
            )
        )
        while state.next_sequence in state.pending:
            pending = self._pop_pending_segment(
                state,
                int(state.next_sequence),
            )
            emitted.extend(self._append_segment(state, pending))
        if not state.pending:
            self._clear_gap_tracking(state)
        return tuple(emitted)

    def flush_stale(self, timestamp_seconds: float) -> tuple[PacketRecord, ...]:
        emitted: list[PacketRecord] = list(self._udp_directions.flush(timestamp_seconds))
        expired_keys: list[tuple[str, int, str, int]] = []
        for key, state in tuple(self._flows.items()):
            emitted.extend(
                self._recover_stalled_gap(
                    state,
                    timestamp_seconds,
                    force_probe=True,
                )
            )
            if (
                state.application_buffer
                and not state.pending
                and timestamp_seconds - state.last_timestamp
                >= self.idle_flush_seconds
            ):
                emitted.extend(self._emit_buffer(state))
            if (
                self.flow_retention_seconds is not None
                and state.last_observed_timestamp
                and timestamp_seconds - state.last_observed_timestamp
                >= self.flow_retention_seconds
            ):
                # A future packet recreates a clean state at its own sequence.
                # This is intentionally opt-in: replay/full combat decoders
                # retain their historical behavior, while the always-on
                # identity listener can discard dead sockets and stale gaps.
                expired_keys.append(key)
        for key in expired_keys:
            self._flows.pop(key, None)
        return tuple(emitted)

    def flush(self) -> tuple[PacketRecord, ...]:
        emitted: list[PacketRecord] = list(self._udp_directions.flush())
        for state in self._flows.values():
            if state.pending:
                recovery_timestamp = max(
                    (
                        row.packet.timestamp_seconds
                        for row in state.pending.values()
                    ),
                    default=state.gap_started_timestamp,
                ) + self.gap_recovery_seconds
                emitted.extend(
                    self._recover_stalled_gap(
                        state,
                        recovery_timestamp,
                        force_probe=True,
                    )
                )
            emitted.extend(self._emit_buffer(state))
        return tuple(emitted)


def _read_options(data: bytes, offset: int, endian: str) -> Iterator[tuple[int, bytes]]:
    while offset + 4 <= len(data):
        code, length = struct.unpack_from(f"{endian}HH", data, offset)
        offset += 4
        if code == 0:
            return
        end = offset + length
        if end > len(data):
            return
        yield code, data[offset:end]
        offset = (end + 3) & ~3


def _timestamp_resolution(idb_body: bytes, endian: str) -> float:
    for code, value in _read_options(idb_body, 8, endian):
        if code != 9 or not value:
            continue
        exponent = value[0]
        if exponent & 0x80:
            return 2.0 ** -(exponent & 0x7F)
        return 10.0 ** -exponent
    return 1e-6


def _iter_pcapng_packets(path: Path) -> Iterator[tuple[float, bytes, int]]:
    data = path.read_bytes()
    offset = 0
    endian = "<"
    timestamp_resolutions: list[float] = []

    while offset + 12 <= len(data):
        raw_type = data[offset : offset + 4]
        if raw_type == struct.pack("<I", _PCAPNG_SECTION_HEADER):
            byte_order_magic = data[offset + 8 : offset + 12]
            if byte_order_magic == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif byte_order_magic == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise ValueError(f"Invalid pcapng byte-order magic at offset {offset}")
            timestamp_resolutions = []

        block_type, block_length = struct.unpack_from(f"{endian}II", data, offset)
        if block_length < 12 or offset + block_length > len(data):
            raise ValueError(f"Invalid pcapng block length at offset {offset}")
        trailing_length = struct.unpack_from(
            f"{endian}I", data, offset + block_length - 4
        )[0]
        if trailing_length != block_length:
            raise ValueError(f"Mismatched pcapng block length at offset {offset}")

        body = data[offset + 8 : offset + block_length - 4]
        if block_type == _PCAPNG_INTERFACE_DESCRIPTION and len(body) >= 8:
            timestamp_resolutions.append(_timestamp_resolution(body, endian))
        elif block_type == _PCAPNG_ENHANCED_PACKET and len(body) >= 20:
            interface_id, timestamp_high, timestamp_low, captured_length, original_length = (
                struct.unpack_from(f"{endian}IIIII", body, 0)
            )
            packet_end = 20 + captured_length
            if packet_end <= len(body):
                resolution = (
                    timestamp_resolutions[interface_id]
                    if interface_id < len(timestamp_resolutions)
                    else 1e-6
                )
                timestamp = ((timestamp_high << 32) | timestamp_low) * resolution
                yield timestamp, body[20:packet_end], original_length

        offset += block_length


def _extract_ipv4_packet(frame: bytes) -> bytes | None:
    def packet_at(ip_at: int) -> bytes | None:
        if ip_at < 0 or ip_at + 20 > len(frame):
            return None
        version_and_ihl = frame[ip_at]
        ihl = (version_and_ihl & 0x0F) * 4
        total_length = int.from_bytes(frame[ip_at + 2 : ip_at + 4], "big")
        protocol = frame[ip_at + 9]
        if (
            version_and_ihl >> 4 == 4
            and ihl >= 20
            and total_length >= ihl
            and ip_at + total_length <= len(frame)
            and protocol in (6, 17)
        ):
            return frame[ip_at : ip_at + total_length]
        return None

    # Npcap's loopback adapter commonly uses DLT_NULL (a four-byte address
    # family followed by the IP packet); some virtual adapters expose raw IP.
    # Supporting both lets the accelerator compatibility probe inspect the
    # game's pre-tunnel localhost flow. Ordinary Ethernet remains unchanged.
    # Normal Npcap Ethernet captures place IPv4 at byte 14. Test that common
    # path first: replay files contain hundreds of thousands of frames, and
    # repeatedly probing raw/loopback layouts plus scanning for the LLC marker
    # was measurable even though every frame used the same Ethernet layout.
    # VLAN, loopback and accelerator adapters retain their existing fallbacks.
    for direct_offset in (14, 0, 4, 18):
        direct = packet_at(direct_offset)
        if direct is not None:
            return direct

    search_at = 0
    while True:
        marker_at = frame.find(_IPV4_LLC_MARKER, search_at)
        if marker_at < 0:
            return None
        ip_at = marker_at + 2
        packet = packet_at(ip_at)
        if packet is not None:
            return packet
        search_at = marker_at + 1


def _decode_ip_packet(
    timestamp: float,
    ip_packet: bytes,
    captured_length: int,
    original_length: int,
) -> PacketRecord | None:
    ihl = (ip_packet[0] & 0x0F) * 4
    protocol = ip_packet[9]
    # IPv4 is already validated by ``_extract_ipv4_packet``. Formatting four
    # bytes directly avoids constructing two ``IPv4Address`` objects for every
    # captured frame while producing the exact same canonical dotted string.
    source_ip = _format_ipv4(ip_packet[12:16])
    destination_ip = _format_ipv4(ip_packet[16:20])

    if protocol == 17:
        if ihl + 8 > len(ip_packet):
            return None
        source_port, destination_port, udp_length = struct.unpack_from(
            ">HHH", ip_packet, ihl
        )
        payload_end = min(len(ip_packet), ihl + max(8, udp_length))
        payload = ip_packet[ihl + 8 : payload_end]
        transport = "UDP"
        tcp_sequence = None
        tcp_acknowledgment = None
        tcp_flags = 0
    elif protocol == 6:
        if ihl + 20 > len(ip_packet):
            return None
        source_port, destination_port = struct.unpack_from(">HH", ip_packet, ihl)
        tcp_sequence, tcp_acknowledgment = struct.unpack_from(">II", ip_packet, ihl + 4)
        tcp_flags = int(ip_packet[ihl + 13])
        tcp_header_length = (ip_packet[ihl + 12] >> 4) * 4
        if tcp_header_length < 20 or ihl + tcp_header_length > len(ip_packet):
            return None
        payload = ip_packet[ihl + tcp_header_length :]
        transport = "TCP"
    else:
        return None

    return PacketRecord(
        timestamp_seconds=timestamp,
        transport=transport,
        source_ip=source_ip,
        destination_ip=destination_ip,
        source_port=source_port,
        destination_port=destination_port,
        payload=payload,
        captured_length=captured_length,
        original_length=original_length,
        tcp_sequence=tcp_sequence,
        tcp_acknowledgment=tcp_acknowledgment,
        tcp_flags=tcp_flags,
    )


def deduplicate_packets(
    packets: Iterable[PacketRecord], window_seconds: float = 3.0
) -> list[PacketRecord]:
    result: list[PacketRecord] = []
    seen_at: dict[tuple[object, ...], float] = {}
    for packet in packets:
        key = (
            packet.transport,
            packet.source_ip,
            packet.destination_ip,
            packet.source_port,
            packet.destination_port,
            packet.payload,
        )
        previous = seen_at.get(key)
        if previous is not None and packet.timestamp_seconds - previous <= window_seconds:
            # Reliable-UDP retries can arrive as a chain. Refresh the timestamp
            # for every copy so a long retry burst cannot leak through merely
            # because it outlived the first packet's window.
            seen_at[key] = packet.timestamp_seconds
            continue
        seen_at[key] = packet.timestamp_seconds
        result.append(packet)
    return result


def read_pcapng_packets(path: str | Path, deduplicate: bool = True) -> list[PacketRecord]:
    capture_path = Path(path)
    packets: list[PacketRecord] = []
    for timestamp, frame, original_length in _iter_pcapng_packets(capture_path):
        ip_packet = _extract_ipv4_packet(frame)
        if ip_packet is None:
            continue
        packet = _decode_ip_packet(timestamp, ip_packet, len(frame), original_length)
        if packet is not None:
            packets.append(packet)
    if deduplicate:
        return deduplicate_packets(packets)
    return packets


def decode_captured_frame(
    timestamp_seconds: float,
    frame: bytes,
    original_length: int | None = None,
) -> PacketRecord | None:
    """Decode one raw PktMon frame for live and replay sources."""
    ip_packet = _extract_ipv4_packet(frame)
    if ip_packet is None:
        return None
    packet = _decode_ip_packet(
        timestamp_seconds,
        ip_packet,
        len(frame),
        len(frame) if original_length is None else original_length,
    )
    if packet is None:
        return None

    # Some accelerator relays expose a UDP wrapper whose payload is a
    # 14-byte relay header followed by the complete Ethernet frame also seen
    # on the virtual adapter.  Decode the inner game packet here, before
    # transport reassembly and actor/self detection, so both adapters produce
    # the same canonical PacketRecord.  The full relay prefix plus a valid,
    # length-closed inner IPv4/TCP-or-UDP packet keeps ordinary game payloads
    # out of this compatibility path.
    relay_prefix = b"\x00\x00\x5e\x00\x00\x00"
    payload = bytes(packet.payload)
    if (
        packet.transport == "UDP"
        and len(payload) >= 14 + 14 + 20
        and payload.startswith(relay_prefix)
    ):
        inner_frame = payload[14:]
        inner_ip_packet = _extract_ipv4_packet(inner_frame)
        if inner_ip_packet is not None:
            inner_packet = _decode_ip_packet(
                timestamp_seconds,
                inner_ip_packet,
                len(inner_frame),
                len(inner_frame),
            )
            if inner_packet is not None:
                return inner_packet
    return packet


def _decode_v576_body_uncached(payload: bytes, xor_phase: int) -> bytes:
    decoded = bytearray(payload)
    key_length = len(_V576_BODY_XOR_KEY)
    # Bytes with the same offset residue use the same XOR byte. Translating
    # those 13 interleaved slices keeps the exact wire transformation while
    # moving the per-byte work from Python into the C implementation of
    # bytes.translate. Large replay files spend most of their load time here.
    for residue in range(key_length):
        start = _V576_BODY_OFFSET + residue
        if start >= len(decoded):
            break
        key_index = (start - xor_phase) % key_length
        decoded[start::key_length] = decoded[start::key_length].translate(
            _V576_BODY_XOR_TABLES[key_index]
        )
    return bytes(decoded)


@lru_cache(maxsize=32)
def _decode_v576_body_cached(payload: bytes, xor_phase: int) -> bytes:
    return _decode_v576_body_uncached(payload, xor_phase)


def _decode_v576_body(payload: bytes, xor_phase: int) -> bytes:
    # Damage, healing, periodic-effect and status decoders inspect the same
    # logical packet independently. The bounded C-level LRU lets those
    # decoders share each XOR phase while keeping memory predictable.
    if len(payload) <= _V576_DECODE_CACHE_MAX_PAYLOAD:
        return _decode_v576_body_cached(payload, xor_phase)
    return _decode_v576_body_uncached(payload, xor_phase)


def _v576_candidate_xor_phases(payload: bytes) -> Iterable[int]:
    if len(payload) > 5:
        known_phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(payload[5])
        if known_phase is not None:
            return (known_phase,)
    return range(len(_V576_BODY_XOR_KEY))


_V576_MOVEMENT_BLOCK_MARKER = b"\x09\x62\x00\x00\x00\x00\x00\x00"
_V576_COMPACT_POSITION_SIGNATURE = b"\xAC\x4E\x20\x00"
_V576_COMPACT_POSITION_XOR_PHASE = 8
_V576_COMPACT_POSITION_PAYLOAD_SIZE = 35
_V576_PARTY_POSITION_MARKER = b"\x01\x20\x00"
_V576_PARTY_POSITION_FIRST_MARKER_OFFSET = 18
_V576_PARTY_POSITION_RECORD_SIZE = 39
_V576_PARTY_POSITION_MIN_PAYLOAD = 53
# Party positions also ride inside unrelated server messages.  The byte after
# the per-record counter is a verified movement state; 0918 classic/hardcore
# captures use the ordinary 1-5 states and the extended 0x11-0x13 states.
# Keeping this allow-list, the following reserved zero byte, and the encoded
# coordinate sentinel check together prevents coincidental ``01 20 00`` bytes
# in skill payloads from becoming player tracks.
_V576_PARTY_POSITION_STATES = frozenset((*range(1, 6), *range(0x11, 0x14)))
_V576_ENTITY_POSITION_OPERATIONS = {
    b"\x08\x6F\x00": "health",
    b"\x08\x73\x00": "hit",
    b"\x08\x75\x00": "hit",
    b"\x08\x77\x00": "hit",
    b"\x08\x79\x00": "hit",
}
def _decode_v576_movement_component(raw: bytes) -> float:
    """Rebuild the IEEE-754 value used by the client's float24 reader.

    Client function ``0x14022F530`` assembles the three bytes in network
    order, preserves the sign/high exponent bits and the upper 22 payload
    bits, then restores the five omitted low bits. Mirroring those operations
    is essential: interpreting the bytes as a fixed-point integer produces
    false 8,000-unit teleports whenever the float sign/exponent changes.
    """

    encoded = int.from_bytes(raw, "big")
    float_bits = (
        (((encoded & 0xFFC00000) << 3) | (encoded & 0x003FFFFF)) << 5
    ) & 0xFFFFFFFF
    return struct.unpack("<f", float_bits.to_bytes(4, "little"))[0]


def _is_v576_movement_component(raw: bytes) -> bool:
    # FF FF FF is the wire sentinel used when a position component is absent.
    # It must be rejected before the compressed float is reconstructed.
    return len(raw) == 3 and raw != b"\xFF\xFF\xFF"


def _is_v576_embedded_party_position(decoded: bytes, marker_offset: int) -> bool:
    if marker_offset < 4 or marker_offset + 18 > len(decoded):
        return False
    if decoded[marker_offset + 7] not in _V576_PARTY_POSITION_STATES:
        return False
    if decoded[marker_offset + 8] != 0:
        return False
    position = decoded[marker_offset + 9 : marker_offset + 18]
    return all(
        _is_v576_movement_component(position[offset : offset + 3])
        for offset in (0, 3, 6)
    )


def decode_v576_movement(
    payload: bytes,
    direction: str | None = None,
) -> tuple[DecodedV576Movement, ...]:
    """Decode only structurally proven actor and target positions.

    V576 uses four independently verified position layouts here:

    * the frequent local compact-position packet (signature ``AC 4E 20 00``),
      whose 35-byte message carries actor id and XYZ at fixed offsets;
    * the local/action movement block (``09 62``), whose actor id is repeated
      44 bytes after the marker;
    * server party-position records (``01 20``), emitted both as a dedicated
      39-byte batch and embedded in otherwise unrelated server messages;
    * actor positions carried by ``08 6F`` health snapshots and
      ``08 73/75/77/79`` hit snapshots.  The target may be a short monster ID
      or a full player runtime ID; the client updates the same actor transform
      in both cases.  These are the target's position, not the attack-area
      coordinates carried by ``38 70/38 74`` records.

    This decoder is intentionally not called by the combat event router:
    movement remains an independent data stream and cannot become damage,
    Buff, healing, or scoring events.
    """

    if len(payload) < 21:
        return ()
    if direction not in (None, "client_to_server", "server_to_client"):
        return ()
    signature = payload[5:9]
    xor_phase = (
        _V576_COMPACT_POSITION_XOR_PHASE
        if signature == _V576_COMPACT_POSITION_SIGNATURE
        else _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
    )
    if xor_phase is None:
        # Do not guess unknown XOR phases for movement. A missing track is
        # safer than drawing a plausible-looking but false path.
        return ()
    decoded = _decode_v576_body(payload, xor_phase)
    rows: list[DecodedV576Movement] = []
    seen: set[tuple[int, bytes]] = set()

    def append_row(
        *,
        actor_id: int,
        position: bytes,
        layout: str,
        record_offset: int,
    ) -> None:
        if len(position) != 9 or actor_id <= 0 or actor_id == 0xFFFFFFFF:
            return
        fingerprint = (actor_id, position)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        rows.append(
            DecodedV576Movement(
                actor_id=actor_id,
                x=_decode_v576_movement_component(position[0:3]),
                vertical=_decode_v576_movement_component(position[3:6]),
                y=_decode_v576_movement_component(position[6:9]),
                layout=layout,
                packet_signature=signature.hex(),
                xor_phase=xor_phase,
                record_offset=record_offset,
            )
        )

    # This compact client position is the primary local-player heartbeat in
    # newer captures and is also the only player position stream in solo
    # instances where the server has no party-position broadcast.  Exact
    # signature, direction and payload size checks keep similarly shaped cast
    # packets out of the movement ledger.
    if (
        direction in (None, "client_to_server")
        and signature == _V576_COMPACT_POSITION_SIGNATURE
        and len(decoded) == _V576_COMPACT_POSITION_PAYLOAD_SIZE
    ):
        compact_actor_id = int.from_bytes(decoded[10:14], "little")
        if compact_actor_id >= 0x10000 and compact_actor_id != 0xFFFFFFFF:
            append_row(
                actor_id=compact_actor_id,
                position=decoded[18:27],
                layout="v576-udp-local-position-ac4e",
                record_offset=10,
            )

    # Server party movement is a fixed header followed by one or more 39-byte
    # records. Checking the complete packet size and every record marker keeps
    # coincidental ``01 20`` bytes in mixed combat messages out of the ledger.
    if (
        direction in (None, "server_to_client")
        and len(decoded) >= _V576_PARTY_POSITION_MIN_PAYLOAD
        and (
            len(decoded) - _V576_PARTY_POSITION_MIN_PAYLOAD
        ) % _V576_PARTY_POSITION_RECORD_SIZE
        == 0
    ):
        record_count = 1 + (
            len(decoded) - _V576_PARTY_POSITION_MIN_PAYLOAD
        ) // _V576_PARTY_POSITION_RECORD_SIZE
        observer_id = int.from_bytes(decoded[10:14], "little")
        record_offsets = [
            _V576_PARTY_POSITION_FIRST_MARKER_OFFSET
            + index * _V576_PARTY_POSITION_RECORD_SIZE
            for index in range(record_count)
        ]
        valid_batch = (
            observer_id >= 0x10000
            and observer_id != 0xFFFFFFFF
            and all(
                decoded[offset : offset + 3] == _V576_PARTY_POSITION_MARKER
                and int.from_bytes(decoded[offset - 4 : offset], "little")
                >= 0x10000
                and int.from_bytes(decoded[offset - 4 : offset], "little")
                != 0xFFFFFFFF
                for offset in record_offsets
            )
        )
        if valid_batch:
            for record_offset in record_offsets:
                append_row(
                    actor_id=int.from_bytes(
                        decoded[record_offset - 4 : record_offset], "little"
                    ),
                    position=decoded[record_offset + 9 : record_offset + 18],
                    layout="v576-udp-party-position-0120",
                    record_offset=record_offset,
                )

    # The dedicated 53+39n packet above is only one carrier for ``01 20``.
    # Real eight-player captures continuously interleave the same 39-byte
    # record with hit, action and state messages at arbitrary offsets.  The old
    # whole-packet length check therefore retained only about one third of the
    # remote-player samples.  Scan every structurally verified record while
    # preserving the stricter dedicated-batch layout when both branches see
    # the same actor/position fingerprint.
    if direction in (None, "server_to_client"):
        marker_offset = _V576_BODY_OFFSET
        while True:
            marker_offset = decoded.find(
                _V576_PARTY_POSITION_MARKER,
                marker_offset,
            )
            if marker_offset < 0:
                break
            next_offset = marker_offset + 1
            if not _is_v576_embedded_party_position(decoded, marker_offset):
                marker_offset = next_offset
                continue
            actor_id = int.from_bytes(
                decoded[marker_offset - 4 : marker_offset],
                "little",
            )
            if actor_id < 0x10000 or actor_id == 0xFFFFFFFF:
                marker_offset = next_offset
                continue
            append_row(
                actor_id=actor_id,
                position=decoded[marker_offset + 9 : marker_offset + 18],
                layout="v576-udp-party-position-0120-embedded",
                record_offset=marker_offset,
            )
            marker_offset = next_offset

    # Health and normal-hit snapshots share one target-coordinate field.  The
    # client applies it to the target actor without branching on ID width, so
    # retain both short monster IDs and full player runtime IDs.  The previous
    # ``entity_id < 0x10000`` guard silently discarded valid player snapshots
    # and made quiet/ranged party members look much more sparsely sampled.
    if direction in (None, "server_to_client") and len(decoded) >= 63:
        entity_operation = _V576_ENTITY_POSITION_OPERATIONS.get(decoded[18:21])
        observer_id = int.from_bytes(decoded[10:14], "little")
        entity_id = int.from_bytes(decoded[14:18], "little")
        if (
            entity_operation is not None
            and observer_id >= 0x10000
            and observer_id != 0xFFFFFFFF
            and 0 < entity_id < 0xFFFFFFFF
        ):
            append_row(
                actor_id=entity_id,
                position=decoded[54:63],
                layout=f"v576-udp-entity-position-{entity_operation}",
                record_offset=14,
            )

    marker_offset = _V576_BODY_OFFSET
    while True:
        marker_offset = decoded.find(
            _V576_MOVEMENT_BLOCK_MARKER,
            marker_offset,
        )
        if marker_offset < 0:
            break
        next_offset = marker_offset + 1
        # actor[-4:] + marker[8] + metadata[8] + xyz[9] + ... + actor[4]
        if marker_offset < 13 or marker_offset + 48 > len(decoded):
            marker_offset = next_offset
            continue
        actor_raw = decoded[marker_offset - 4 : marker_offset]
        if actor_raw != decoded[marker_offset + 44 : marker_offset + 48]:
            marker_offset = next_offset
            continue
        actor_id = int.from_bytes(actor_raw, "little")
        if actor_id < 0x10000 or actor_id == 0xFFFFFFFF:
            marker_offset = next_offset
            continue
        append_row(
            actor_id=actor_id,
            position=decoded[marker_offset + 16 : marker_offset + 25],
            layout="v576-udp-movement-0962",
            record_offset=marker_offset,
        )
        marker_offset = next_offset
    return tuple(rows)


def _embedded_skill_candidates(
    decoded: bytes,
    known_skill_ids: frozenset[int],
    start_offset: int = 80,
    excluded_ranges: tuple[tuple[int, int], ...] = (),
) -> tuple[int, ...]:
    candidates: Counter[int] = Counter()
    first_offset: dict[int, int] = {}
    last_offset: dict[int, int] = {}
    end_offset = len(decoded) - 3
    unpack_uint32 = _UINT32_LE.unpack_from

    if not excluded_ranges:
        for offset in range(start_offset, end_offset):
            # ``unpack_from`` avoids allocating a four-byte slice at every
            # offset. This loop runs millions of times on long captures.
            skill_id = unpack_uint32(decoded, offset)[0]
            if skill_id <= 0 or skill_id not in known_skill_ids:
                continue
            candidates[skill_id] += 1
            first_offset.setdefault(skill_id, offset)
            last_offset[skill_id] = offset
    elif len(excluded_ranges) == 1:
        excluded_start, excluded_end = excluded_ranges[0]
        for offset in range(start_offset, min(end_offset, excluded_start)):
            skill_id = unpack_uint32(decoded, offset)[0]
            if skill_id <= 0 or skill_id not in known_skill_ids:
                continue
            candidates[skill_id] += 1
            first_offset.setdefault(skill_id, offset)
            last_offset[skill_id] = offset
        for offset in range(max(start_offset, excluded_end), end_offset):
            skill_id = unpack_uint32(decoded, offset)[0]
            if skill_id <= 0 or skill_id not in known_skill_ids:
                continue
            candidates[skill_id] += 1
            first_offset.setdefault(skill_id, offset)
            last_offset[skill_id] = offset
    else:
        for offset in range(start_offset, end_offset):
            if any(start <= offset < end for start, end in excluded_ranges):
                continue
            skill_id = unpack_uint32(decoded, offset)[0]
            if skill_id <= 0 or skill_id not in known_skill_ids:
                continue
            candidates[skill_id] += 1
            first_offset.setdefault(skill_id, offset)
            last_offset[skill_id] = offset
    if not candidates:
        return ()
    # Compound packets contain several small counters/state codes that can
    # also be valid low-level Warrior skill IDs.  When a normal skill ID is
    # present, those values are context rather than the damage owner.  Genuine
    # low-ID attacks remain available when no normal-ID candidate exists.
    ranked_ids = (
        [skill_id for skill_id in candidates if skill_id >= 100]
        if any(skill_id >= 100 for skill_id in candidates)
        else list(candidates)
    )
    return tuple(sorted(
        ranked_ids,
        key=lambda skill_id: (
            -candidates[skill_id],
            -last_offset[skill_id],
            first_offset[skill_id],
        ),
    ))


def _embedded_skill_id(
    decoded: bytes,
    known_skill_ids: frozenset[int],
    start_offset: int = 80,
    excluded_ranges: tuple[tuple[int, int], ...] = (),
) -> int | None:
    candidates = _embedded_skill_candidates(
        decoded,
        known_skill_ids,
        start_offset=start_offset,
        excluded_ranges=excluded_ranges,
    )
    return candidates[0] if candidates else None


def _direct_result_fixed_slot_end(decoded: bytes, marker_offset: int) -> int:
    """Bound new direct37 fixed-slot reads by the next recognized record.

    Damage and 13 47..4E response records start four bytes before their
    marker. Only inspect the short region that could overlap +102/+114;
    this is a boundary check, never a search for skill-looking integers.
    Keep the conservative structural checks used by the raw capture audit.
    """
    stop = len(decoded)
    search_start = marker_offset + 36
    # A marker up to three bytes after the last slot can still put its
    # four-byte record prefix inside that slot. Include that boundary.
    marker_limit = min(len(decoded), marker_offset + 122)
    for marker, declared_length in _V576_DIRECT_RECORD_MARKERS:
        damage_delta = 19 if declared_length in (0x73, 0x75) else 23
        cursor = decoded.find(marker, search_start, marker_limit + 2)
        while 0 <= cursor < marker_limit:
            if cursor >= 4 and cursor + damage_delta + 8 <= len(decoded):
                target = _UINT32_LE.unpack_from(decoded, cursor - 4)[0]
                source = _UINT32_LE.unpack_from(decoded, cursor + 8)[0]
                damage = int.from_bytes(
                    decoded[cursor + damage_delta : cursor + damage_delta + 8],
                    "little",
                )
                if target > 0 and source > 0 and _is_valid_v576_damage_amount(damage):
                    stop = min(stop, cursor - 4)
            cursor = decoded.find(marker, cursor + 1, marker_limit + 2)
    for suffix in range(0x47, 0x4F):
        marker = bytes((0x13, suffix, 0))
        cursor = decoded.find(marker, search_start, marker_limit + 2)
        while 0 <= cursor < marker_limit:
            if (
                cursor >= 4
                and cursor + 10 <= len(decoded)
                and _UINT32_LE.unpack_from(decoded, cursor - 4)[0] > 0
                and _UINT32_LE.unpack_from(decoded, cursor + 3)[0] > 0
            ):
                stop = min(stop, cursor - 4)
            cursor = decoded.find(marker, cursor + 1, marker_limit + 2)
    return stop


def _direct_result_skill_id(
    decoded: bytes,
    known_skill_ids: frozenset[int],
    family: str,
    marker_offset: int,
    *,
    record_end: int | None = None,
) -> int | None:
    """Read the owning skill from one direct37/direct41 result record.

    Corpus comparison against independent client/server action rows exposed
    two v576 result layouts. ``direct37`` stores the skill at +102 or +114
    (the branch depends on the result-vector header); ``direct41`` stores it
    at +106. Other skill-looking integers later in the same message belong
    to appended actions/statuses and must not be allowed to relabel the hit.

    Return a value only when the applicable slots agree on one catalogued
    damage skill. Unknown/conflicting layouts fail closed and continue down
    the existing object/status/entity attribution chain.
    """
    if not known_skill_ids or marker_offset < 0:
        return None
    if family in {"direct37", "direct37-player", "direct37-status-continuation"}:
        relative_offsets = (102, 114)
    elif family in {"direct41", "direct41-player", "direct41-status"}:
        relative_offsets = (106,)
    else:
        return None

    if family == "direct37-status-continuation":
        bounded_end = _direct_result_fixed_slot_end(decoded, marker_offset)
        record_end = bounded_end if record_end is None else min(record_end, bounded_end)
    # Existing direct37/direct41 callers retain their established behavior.
    # The short-packet path opts into the same new boundary explicitly.
    slot_end = len(decoded) if record_end is None else min(len(decoded), record_end)
    candidates: list[int] = []
    for relative_offset in relative_offsets:
        offset = marker_offset + relative_offset
        if offset < 0 or offset + 4 > slot_end:
            continue
        skill_id = _UINT32_LE.unpack_from(decoded, offset)[0]
        if skill_id > 0 and skill_id in known_skill_ids:
            candidates.append(skill_id)
    distinct = tuple(dict.fromkeys(candidates))
    return distinct[0] if len(distinct) == 1 else None


def decode_v576_status_events(
    payload: bytes,
    direction: str | None = None,
    known_skill_ids: Iterable[int] = (),
) -> tuple[DecodedV576Status, ...]:
    """Read server-authoritative status applications embedded in v576 packets.

    A status record stores the target and source before the owning skill, then
    carries its server duration and parameter string. This is deliberately
    independent from damage decoding: a missed skill creates no status event,
    while a refresh extends the status by the duration sent by the server.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 50
        or not _v576_wire_may_contain_marker(
            payload, (b"\xff\xff\xff\xff",), start_offset=24
        )
    ):
        return ()
    known_skill_id_set = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(int(skill_id) for skill_id in known_skill_ids)
    )
    found: list[DecodedV576Status] = []
    seen: set[tuple[int, int, int, int, str, str, int | None]] = set()
    status_marker = b"\xff\xff\xff\xff"
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        marker_offset = decoded.find(status_marker, 24)
        while marker_offset >= 0:
            skill_offset = marker_offset + len(status_marker)
            if skill_offset >= len(decoded) - 20:
                break
            # Search from the next byte rather than after the full marker so
            # overlapping FF sequences retain the exact candidates produced
            # by the former byte-by-byte loop.
            marker_offset = decoded.find(status_marker, marker_offset + 1)
            record_code = decoded[skill_offset - 12 : skill_offset - 9]
            if len(record_code) != 3 or record_code[0] != 0x13 or record_code[2] != 0:
                continue
            if decoded[skill_offset - 5] != 0:
                continue
            skill_id = int.from_bytes(
                decoded[skill_offset : skill_offset + 4], "little"
            )
            # Small integers embedded in compound packets are often counters,
            # but valid 0/1st-job status skills also use IDs below 1000 (for
            # example, Cut EX=204 and Heavy Hammer=212). Keep the false-positive
            # guard only for values that are not present in the loaded skill
            # catalogs.
            if skill_id == 0xFFFFFFFF or (
                skill_id < 1000 and skill_id not in known_skill_id_set
            ):
                continue
            raw_duration_ms = int.from_bytes(
                decoded[skill_offset + 12 : skill_offset + 16], "little"
            )
            permanent = raw_duration_ms == 0xFFFFFFFF
            # A tiny reviewed set of action processors uses duration=0. Keep
            # it provisionally until the exact skill/effect/parameter tuple is
            # checked below; every unrelated zero-duration status still fails
            # closed.
            zero_duration_response = raw_duration_ms == 0
            if (
                not permanent
                and not zero_duration_response
                and not 1 <= raw_duration_ms <= 600_000
            ):
                continue
            duration_ms = 0 if permanent or zero_duration_response else raw_duration_ms
            parameter_size = int.from_bytes(
                decoded[skill_offset + 16 : skill_offset + 20], "little"
            )
            if not 0 <= parameter_size <= 256:
                continue
            parameter_end = skill_offset + 20 + parameter_size
            if parameter_end > len(decoded):
                continue
            raw_parameters = decoded[skill_offset + 20 : parameter_end]
            try:
                parameters = raw_parameters.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if any(ord(character) < 32 for character in parameters):
                continue
            target_actor_id = int.from_bytes(
                decoded[skill_offset - 16 : skill_offset - 12], "little"
            )
            source_actor_id = int.from_bytes(
                decoded[skill_offset - 9 : skill_offset - 5], "little"
            )
            if target_actor_id == 0 or source_actor_id == 0:
                continue
            effect_code = record_code.hex()
            skill_level_row_id = int.from_bytes(
                decoded[skill_offset + 4 : skill_offset + 8], "little"
            )
            effect_class_id = int.from_bytes(
                decoded[skill_offset + 8 : skill_offset + 12], "little"
            )
            exact_burning_hand_response = bool(
                skill_id == BURNING_HAND_SKILL_ID
                and (
                    (
                        effect_code == BURNING_HAND_ACTION_EFFECT_CODE
                        and effect_class_id == BURNING_HAND_ACTION_EFFECT_CLASS
                        and parameters == BURNING_HAND_ACTION_PARAMETER
                    )
                    or (
                        effect_code == BURNING_HAND_FILTER_EFFECT_CODE
                        and effect_class_id == BURNING_HAND_FILTER_EFFECT_CLASS
                        and parameters == BURNING_HAND_FILTER_PARAMETER
                    )
                )
            )
            if zero_duration_response and not exact_burning_hand_response:
                continue
            effect_instance_id: int | None = None
            # Effect responses terminate the parameter string with one zero
            # byte and then carry a runtime response instance. Keeping it is
            # useful for lifecycle auditing even though direct37 does not
            # repeat this response ID itself.
            if (
                # 13 5D uses the same response-instance tail for persistent
                # skill-owned area/status generations.  Soul Eater's Grudge
                # Formation (5207) is an observed example: every cast carries
                # ``00 + runtime instance:u32`` after
                # ``Skill_ResentPVEArea_Attack``.  Retaining that ID lets the
                # field ledger distinguish refreshes without relying on a
                # wall-clock damage window.
                # 135200 is the 5213 puppet-routing response.  It carries
                # the same opaque runtime instance tail as 134700, but the
                # old decoder omitted it, losing the only lifecycle token
                # that can be compared with the paired puppet statuses.
                effect_code in {
                    "134a00", "134e00", "134700", "135200", "135d00",
                    BURNING_HAND_ACTION_EFFECT_CODE,
                    BURNING_HAND_FILTER_EFFECT_CODE,
                }
                or skill_id in {2213, 2214, 3211, 3213}
            ) and (
                parameter_end + 5 <= len(decoded)
                and decoded[parameter_end] == 0
            ):
                candidate_instance_id = int.from_bytes(
                    decoded[parameter_end + 1 : parameter_end + 5], "little"
                )
                if candidate_instance_id not in {0, 0xFFFFFFFF}:
                    effect_instance_id = candidate_instance_id
            key = (
                skill_id,
                target_actor_id,
                source_actor_id,
                duration_ms,
                effect_code,
                parameters,
                effect_instance_id,
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(
                DecodedV576Status(
                    skill_id=skill_id,
                    target_actor_id=target_actor_id,
                    source_actor_id=source_actor_id,
                    duration_ms=duration_ms,
                    effect_code=effect_code,
                    parameters=parameters,
                    xor_phase=xor_phase,
                    record_offset=skill_offset,
                    permanent=permanent,
                    skill_level_row_id=skill_level_row_id,
                    effect_class_id=effect_class_id,
                    effect_instance_id=effect_instance_id,
                )
            )
    return tuple(found)


def _decode_v576_wrapped_object_registration(
    payload: bytes, direction: str | None,
) -> DecodedV576DamageObjectRegistration | None:
    # Client/middlebox envelopes preserve the original registration tail.
    # Only audited lengths, owner offsets and XOR phases are accepted.
    layouts = {
        114: (17, 6, False), 117: (20, 9, False),
        # Complete 107-byte registration behind the audited 20-byte relay
        # envelope.  The repeated owner/resource fields below are unchanged.
        127: (30, 6, False),
        112: (10, 11, True), 119: (17, 5, True), 122: (20, 8, True),
        120: (10, 12, False),
        125: (10, 11, True), 132: (17, 5, True), 135: (20, 8, True),
    }
    layout = layouts.get(len(payload))
    if layout is None or direction not in (None, "client_to_server", "server_to_client"):
        return None
    extended_resource = len(payload) == 120
    extended_parent = len(payload) in (125, 132, 135)
    if extended_resource and payload[5:9] != b"\x0c\x6d\x75\x00":
        return None
    owner_offset, expected_phase, parent_shape = layout
    candidates: list[DecodedV576DamageObjectRegistration] = []
    phases = (expected_phase, *(phase for phase in range(len(_V576_BODY_XOR_KEY)) if phase != expected_phase))
    for phase in phases:
        # Most equally sized packets are NOT object registrations. Reject on
        # the audited phase first; the remaining phases only check ambiguity
        # for an otherwise valid registration.
        if phase != expected_phase and not candidates:
            return None
        decoded = _decode_v576_body(payload, phase)
        def u32(relative: int) -> int:
            return _UINT32_LE.unpack_from(decoded, owner_offset + relative)[0]
        owner, object_id, template_id = u32(0), u32(9), u32(13)
        if (
            not 0 < owner < 0xFFFFFFFF or owner != u32(45)
            or decoded[owner_offset + 4:owner_offset + 9] != bytes(5)
            or not 0 < object_id <= 0xFFFFFF
            or not 0 < template_id <= 1_000_000
        ):
            continue
        parent_id = 0
        if parent_shape:
            parent_id, branch = u32(50), u32(54)
            resource, sequence = u32(83 if extended_parent else 70), branch >> 16
            allowed_branches = (3,) if extended_parent else (1, 3) if len(payload) == 112 else (1,)
            if (
                decoded[owner_offset + 49] != 0
                or not 0 < parent_id <= 0xFFFFFF or parent_id == object_id
                or branch & 0xFFFF not in allowed_branches
                or decoded[owner_offset + 58:owner_offset + 66] != b"\xff" * 8
                or not 0 < resource <= 0xFFFF
            ):
                continue
        else:
            word = u32(49)
            resource, sequence = word & 0xFFFF, word >> 16
            if (
                not 0 < resource < 0xFFFF or resource != u32(78 if extended_resource else 65)
                or decoded[owner_offset + 53:owner_offset + 61] != b"\xff" * 8
            ):
                continue
            # The 120-byte form inserts nine opaque bytes and four FF bytes
            # before the old scalar/resource suffix. Do not assign semantics
            # to that extension or accept the unproved FFFF resource variant.
        if extended_resource or extended_parent:
            # This reference can be FF, the owner, or a short entity ID.
            # It is NOT a mandatory damage target or skill owner: retain it
            # in the byte fingerprint but never use it to filter later hits.
            reference = u32(75 if extended_parent else 70)
            if reference != 0xFFFFFFFF and reference != owner and not 0 < reference <= 0xFFFFFF:
                continue
        candidates.append(DecodedV576DamageObjectRegistration(
            source_actor_id=owner, damage_object_id=object_id,
            projectile_template_id=template_id, effect_resource_id=resource,
            child_sequence=sequence, xor_phase=phase,
            layout=f"c2s-udp-damage-object-{'parent' if parent_shape else 'register'}-{len(payload)}-v576",
            record_offset=owner_offset - 1,
            parent_qualified_subtype=parent_shape,
            parent_damage_object_id=parent_id,
            registration_key=decoded[owner_offset:].hex(),
        ))
    if len(candidates) != 1 or candidates[0].xor_phase != expected_phase:
        return None
    return candidates[0]


def _decode_v576_client_compound_beast_root(
    payload: bytes, direction: str | None, transport: str | None,
) -> DecodedV576DamageObjectRegistration | None:
    """A complete local root and a separately bounded companion record.

    The native registration can precede 46/0D, 10/15 or 1E/1A. Validate the
    entire two-record envelope, then pass only the first body's bytes through
    the established native reader. Naming and parent lineage still require
    the actual ACT tuple and current generation; the companion supplies no
    skill. Keep this additional envelope reader on independently reviewed
    Kali creator tuples; configuration numbers alone are not identity.
    """
    forms = {
        (147, b"\x2e\x8c\x90\x00"): 4,
        (155, b"\x18\xba\x98\x00"): 0,
        (160, b"\x2e\x8c\x9d\x00"): 4,
    }
    expected = forms.get((len(payload), payload[5:9]))
    if (expected is None or direction != "client_to_server"
            or str(transport or "").upper() != "UDP"
            or int.from_bytes(payload[7:9], "little") + 3 != len(payload)):
        return None
    candidates = []
    phases = (expected, *(p for p in range(len(_V576_BODY_XOR_KEY)) if p != expected))
    for phase in phases:
        if phase != expected and not candidates:
            return None
        decoded = _decode_v576_body(payload, phase)
        owner = int.from_bytes(decoded[14:18], "little")
        body_size = int.from_bytes(decoded[19:21], "little")
        end = 21 + body_size
        if (not 0x01000000 <= owner < 0xFFFFFFFF
                or int.from_bytes(decoded[10:14], "little") != owner
                or decoded[18] != 9 or body_size not in (93, 106)
                or end + 7 > len(decoded)
                or int.from_bytes(decoded[end:end + 4], "little") != owner):
            continue
        tail_kind = decoded[end + 4]
        tail_size = int.from_bytes(decoded[end + 5:end + 7], "little")
        if ((tail_kind, tail_size) not in {(0x46, 13), (0x10, 21), (0x1E, 26)}
                or end + 7 + tail_size != len(decoded)):
            continue
        native = bytearray(end - 7)
        native[5:9] = b"\x0c\x6d\x68\x00" if body_size == 93 else b"\x0c\x6d\x75\x00"
        native[10:] = decoded[14:18] + decoded[21:end]
        registration = decode_v576_damage_object_registration(
            _decode_v576_body(bytes(native), 12), direction, transport=transport,
        )
        if registration is None or decoded[61] != 0:
            continue
        if (registration.projectile_template_id, registration.effect_resource_id,
                registration.child_sequence) not in {
                    (11075, 224, 16),
                    (5520, 90, 11), (5520, 90, 12), (5520, 90, 13), (5520, 90, 33),
                    (5520, 91, 9), (5520, 91, 10), (5520, 91, 11), (5520, 91, 12),
                }:
            continue
        candidates.append(replace(
            registration, xor_phase=phase, record_offset=18,
            layout=f"c2s-compound-player-root-{len(payload)}-v576",
            registration_key=decoded[14:end].hex(),
        ))
    if len(candidates) != 1 or candidates[0].xor_phase != expected:
        return None
    return candidates[0]


def _decode_v576_client_compound_beast_child(
    payload: bytes, direction: str | None, transport: str | None,
) -> DecodedV576DamageObjectRegistration | None:
    """Read the bounded 09/6F child in the audited two-root C2S datagram.

    Spirit Paper's companion root does not identify the child: its explicit
    parent object foreign key must still pass the existing lineage ledger.
    Normalize only this registration descriptor through the guarded 125-byte
    reader. The physical datagram and the damage message splitter are intact.
    """
    if (direction != "client_to_server" or str(transport or "").upper() != "UDP"
            or len(payload) != 232 or payload[5:9] != b"\x7c\xde\xe5\x00"):
        return None
    decoded = _decode_v576_body(payload, 4)
    owner = int.from_bytes(decoded[14:18], "little")
    if (not 0x01000000 <= owner < 0xFFFFFFFF
            or int.from_bytes(decoded[10:14], "little") != owner
            or decoded[18:26] != b"\x09\x5d\x00" + bytes(5)
            or not 0 < int.from_bytes(decoded[26:30], "little") <= 0xFFFFFF
            or int.from_bytes(decoded[30:34], "little") != 5517
            or decoded[61] != 0 or int.from_bytes(decoded[62:66], "little") != owner
            or decoded[66:70] != b"\xdc\x00\x13\x00"
            or decoded[70:78] != b"\xff" * 8
            or int.from_bytes(decoded[82:86], "little") != 220
            or int.from_bytes(decoded[114:118], "little") != owner
            or decoded[118:126] != b"\x09\x6f\x00" + bytes(5)
            or int.from_bytes(decoded[130:134], "little") != 11076):
        return None
    native_body = bytearray(125)
    native_body[5:9] = b"\x9c\xfd\x7a\x00"
    native_body[10:] = decoded[114:118] + decoded[121:232]
    normalized = _decode_v576_body(bytes(native_body), 11)
    registration = _decode_v576_wrapped_object_registration(normalized, direction)
    if (registration is None or registration.child_sequence not in (66, 67)
            or registration.effect_resource_id != 220):
        return None
    return replace(
        registration, xor_phase=4, record_offset=118,
        layout="c2s-compound-beast-child-232-v576",
    )


def decode_v576_damage_object_registration(
    payload: bytes,
    direction: str | None = None,
    *,
    transport: str | None = None,
) -> DecodedV576DamageObjectRegistration | None:
    """Decode the client object registration later referenced by direct37.

    Controlled v576 captures use a 107-byte client message with signature
    ``0c 6d 68 00``. The source actor and effect resource are each repeated,
    which strongly guards against coincidental small integers.
    """

    # Audited C2S UDP relay frames retain one complete native registration
    # after a 12-byte opaque envelope. Read its object evidence independently;
    # stripping this envelope in the shared message splitter changes how
    # mirrored S2C damage is decoded. Keep normal message decoding unchanged.
    if (
        direction == "client_to_server"
        and str(transport or "").upper() == "UDP"
        and len(payload) in (119, 132)
        and payload[5:9] == b"\x19\x17\x62\x3b"
    ):
        inner = payload[12:]
        expected = {b"\x0c\x6d\x68\x00": 107,
                    b"\x0c\x6d\x75\x00": 120}.get(inner[5:9])
        if expected is None or len(inner) != expected or int.from_bytes(inner[7:9], "little") + 3 != expected:
            return None
        registration = decode_v576_damage_object_registration(inner, direction, transport=transport)
        if registration is None:
            return None
        # Consumers still receive the full original payload. Translate the
        # record position and cipher phase to its absolute outer coordinates.
        return replace(
            registration,
            record_offset=registration.record_offset + 12,
            xor_phase=(registration.xor_phase + 12) % len(_V576_BODY_XOR_KEY),
        )
    if len(payload) in (147, 155, 160):
        return _decode_v576_client_compound_beast_root(payload, direction, transport)
    if len(payload) == 232:
        return _decode_v576_client_compound_beast_child(payload, direction, transport)
    if len(payload) != 107:
        return _decode_v576_wrapped_object_registration(payload, direction)
    if (
        direction not in (None, "client_to_server")
        or payload[5:9] != b"\x0c\x6d\x68\x00"
    ):
        return None
    xor_phase = 12
    decoded = _decode_v576_body(payload, xor_phase)
    source_actor_id = int.from_bytes(decoded[10:14], "little")
    repeated_source_actor_id = int.from_bytes(decoded[55:59], "little")
    damage_object_id = int.from_bytes(decoded[19:23], "little")
    projectile_template_id = int.from_bytes(decoded[23:27], "little")
    resource_word = int.from_bytes(decoded[59:63], "little")
    effect_resource_id = resource_word & 0xFFFF
    child_sequence = (resource_word >> 16) & 0xFFFF
    repeated_effect_resource_id = int.from_bytes(decoded[75:79], "little")
    # These are genuine high-numbered Projectile rows from the Black Dragon
    # hardcore Wisp transformation ACT, not character skill IDs. Its exact
    # type-15 slots use an FFFF suffix. Keep both exceptions on the complete
    # tuple; ordinary registrations retain the repeated-resource guard.
    wisp_transform = (
        direction == "client_to_server"
        and str(transport or "").upper() == "UDP"
        and (effect_resource_id, child_sequence, projectile_template_id)
        in {(27, 8, 73000017), (44, 9, 73000018)}
        and repeated_effect_resource_id in (effect_resource_id, 0xFFFF)
        and source_actor_id >= 0x01000000
        and decoded[14:19] == bytes(5)
        and decoded[54] == 0
        and decoded[62] == 0
        and decoded[63:71] == b"\xff" * 8
    )
    if (
        source_actor_id == 0
        or repeated_source_actor_id != source_actor_id
        or not 1 <= damage_object_id <= 0x00FFFFFF
        or not (1 <= projectile_template_id <= 1_000_000 or wisp_transform)
        or effect_resource_id == 0
        or (repeated_effect_resource_id != effect_resource_id and not wisp_transform)
    ):
        return None
    return DecodedV576DamageObjectRegistration(
        source_actor_id=source_actor_id,
        damage_object_id=damage_object_id,
        projectile_template_id=projectile_template_id,
        effect_resource_id=effect_resource_id,
        child_sequence=child_sequence,
        xor_phase=xor_phase,
        record_offset=9,
        registration_key=decoded[10:].hex(),
    )


def _decode_v576_compound_damage_object_registrations(
    payload: bytes, direction: str | None, transport: str | None,
) -> tuple[DecodedV576DamageObjectRegistration, ...] | None:
    """Read independently bounded client births, without skill inference.

    None means this is not a verified compound envelope; an empty tuple
    rejects ambiguous phases. A complete birth whose native shape is not
    proved retains its owner/UID as a zero-template generation barrier.
    """
    if (direction != "client_to_server"
            or str(transport or "").upper() != "UDP"
            or len(payload) < 28
            or int.from_bytes(payload[7:9], "little") + 3 != len(payload)):
        return None
    preferred = _V576_XOR_PHASE_BY_SIGNATURE_BYTE[payload[5]]
    phases = (preferred, *(p for p in range(len(_V576_BODY_XOR_KEY)) if p != preferred))
    variants: list[tuple[DecodedV576DamageObjectRegistration, ...]] = []
    for phase in phases:
        # Checking only the first length avoids thirteen whole-body decrypts
        # on ordinary C2S movement/action packets. A marker inside an opaque
        # record never supplies a candidate: the full walk below is required.
        first_size = (
            (payload[19] ^ _V576_BODY_XOR_KEY[(19 - phase) % len(_V576_BODY_XOR_KEY)])
            | ((payload[20] ^ _V576_BODY_XOR_KEY[(20 - phase) % len(_V576_BODY_XOR_KEY)]) << 8)
        )
        if not 0 < first_size <= len(payload) - 21:
            continue
        decoded = _decode_v576_body(payload, phase)
        envelope_owner = _UINT32_LE.unpack_from(decoded, 10)[0]
        if not 0x01000000 <= envelope_owner < 0xFFFFFFFF:
            continue
        cursor = 14
        records: list[tuple[int, bytes]] = []
        while cursor < len(decoded):
            if cursor + 7 > len(decoded):
                break
            size = int.from_bytes(decoded[cursor + 5:cursor + 7], "little")
            end = cursor + 7 + size
            owner = _UINT32_LE.unpack_from(decoded, cursor)[0]
            if not size or end > len(decoded) or not 0 < owner < 0xFFFFFFFF:
                break
            records.append((cursor + 4, decoded[cursor:end]))
            cursor = end
        if cursor != len(decoded) or not records:
            continue
        registrations: list[DecodedV576DamageObjectRegistration] = []
        for marker, record in records:
            if record[4] not in (9, 56):
                continue
            body = record[7:]
            owner = _UINT32_LE.unpack_from(record)[0]
            # These fixed fields precede the variable Actor/UID reference.
            # A sender/header owner cannot be borrowed by another record.
            if (owner < 0x01000000 or len(body) < 41
                    or body[4] not in (0, 1)):
                continue
            object_id = _UINT32_LE.unpack_from(body, 5)[0]
            reference_offset = 41 if body[4] == 0 else 37
            if (not 0 < object_id <= 0xFFFFFF
                    or reference_offset + 4 > len(body)
                    or _UINT32_LE.unpack_from(body, reference_offset)[0] != owner):
                continue
            # Reuse the native single-registration validators on an isolated
            # copy. Each body length determines its native envelope, never a
            # character job, packet total size, or a neighbouring root.
            native_layout = {
                93: (107, 12, b"\x0c\x6d\x68\x00"),
                106: (120, 12, b"\x0c\x6d\x75\x00"),
                98: (112, 11, b"\x9c\xfd\x6d\x00"),
                111: (125, 11, b"\x9c\xfd\x7a\x00"),
            }.get(len(body))
            registration = None
            if native_layout is not None:
                native_size, native_phase, signature = native_layout
                native = bytearray(native_size)
                native[5:9] = signature
                native[10:] = record[:4] + body
                wire = _decode_v576_body(bytes(native), native_phase)
                registration = decode_v576_damage_object_registration(
                    wire, "client_to_server", transport="UDP",
                )
                if (registration is not None
                        and (registration.source_actor_id != owner
                             or registration.damage_object_id != object_id)):
                    registration = None
            key = (record[:4] + body).hex()
            if registration is None:
                registrations.append(DecodedV576DamageObjectRegistration(
                    source_actor_id=owner, damage_object_id=object_id,
                    projectile_template_id=0, effect_resource_id=0, child_sequence=0,
                    xor_phase=phase, record_offset=marker,
                    layout="c2s-compound-unverified-object-birth-v576",
                    registration_key=key, requires_root_action_proof=True,
                ))
            else:
                registrations.append(replace(
                    registration, xor_phase=phase, record_offset=marker,
                    layout=f"c2s-compound-object-record-{record[4]}-{len(body)}-v576",
                    registration_key=key,
                ))
        if registrations:
            variants.append(tuple(registrations))
    if len(variants) > 1:
        return ()
    return variants[0] if variants else None


def _decode_v576_kali_fan_optional_controller_registration(
    payload: bytes, direction: str | None, transport: str | None,
) -> DecodedV576DamageObjectRegistration | None:
    """Read the reviewed standalone fan births whose optional ACT is absent.

    Current image 452092fd, AB4150 writer AB45F3/AB468C: +75 is a
    separate signed16 controller result initialized to -1. It is not a
    repetition of the root ACT at +59; +77 starts another DWORD. Keep this
    extension on complete reviewed fan tuples and observed native shapes,
    rather than relaxing the legacy registration reader for every config.
    """
    if (direction != "client_to_server" or str(transport or "").upper() != "UDP"
            or len(payload) != 107 or payload[5:9] != b"\x0c\x6d\x68\x00"
            or int.from_bytes(payload[7:9], "little") + 3 != len(payload)):
        return None
    decoded = _decode_v576_body(payload, 12)
    owner = _UINT32_LE.unpack_from(decoded, 10)[0]
    object_id = _UINT32_LE.unpack_from(decoded, 19)[0]
    config = _UINT32_LE.unpack_from(decoded, 23)[0]
    action = int.from_bytes(decoded[59:61], "little", signed=True)
    signal = int.from_bytes(decoded[61:62], "little", signed=True)
    key = (action, signal, config)
    reviewed_tuple = key in {(11, 13, 5500), (36, 11, 5500), (33, 7, 5502)}
    # These independently serialized scalar/flag/vector values are restricted
    # to the two complete shapes in the five original reviewed registrations.
    # Their widths are native-certified; this does not claim general domains.
    ground_shape = (b"\x00\x00\x96\x44", bytes(16))
    air_shape = (b"\x00\x00\x48\x44", bytes.fromhex("00000000cdccccbe0000803f01000000"))
    observed_shape = (decoded[71:75], decoded[87:103])
    reviewed_tuple = reviewed_tuple and observed_shape == (
        air_shape if key == (33, 7, 5502) else ground_shape
    )
    if (not 0x01000000 <= owner < 0xFFFFFFFF
            or not 0 < object_id <= 0xFFFFFF
            or _UINT32_LE.unpack_from(decoded, 55)[0] != owner
            or decoded[14:19] != bytes(5)
            or decoded[54] != 0 or decoded[62] != 0
            or decoded[63:71] != b"\xff" * 8
            or decoded[75:77] != b"\xff" * 2
            or decoded[77:83] != bytes(6)
            or observed_shape not in (ground_shape, air_shape)
            or decoded[83:87] != decoded[71:75]
            or not math.isfinite(struct.unpack_from("<f", decoded, 103)[0])):
        return None
    return DecodedV576DamageObjectRegistration(
        source_actor_id=owner, damage_object_id=object_id,
        # A fully formed unreviewed birth still supersedes the same UID.
        # Preserve native coordinates/raw key, but grant no config identity.
        projectile_template_id=config if reviewed_tuple else 0,
        effect_resource_id=action, child_sequence=signal, xor_phase=12, record_offset=9,
        layout=("c2s-udp-kali-fan-optional-controller-register-107-v576" if reviewed_tuple
                else "c2s-udp-unverified-optional-controller-birth-107-v576"),
        registration_key=decoded[10:].hex(), requires_root_action_proof=True,
    )


def decode_v576_damage_object_registrations(
    payload: bytes, direction: str | None = None, *, transport: str | None = None,
) -> tuple[DecodedV576DamageObjectRegistration, ...]:
    """Return ordered object births without changing legacy single readers.

    New complete-TLV decoding is C2S UDP only. Legacy standalone/relay and
    wrapped S2C behaviour is retained. Repeated records are kept in physical
    order; downstream generation ledgers decide retransmission idempotence.
    Zero-template compound births are invalidation evidence, not skills.
    """
    compound = _decode_v576_compound_damage_object_registrations(payload, direction, transport)
    if compound is not None:
        return compound
    registration = decode_v576_damage_object_registration(payload, direction, transport=transport)
    if registration is None:
        registration = _decode_v576_kali_fan_optional_controller_registration(payload, direction, transport)
    return (registration,) if registration is not None else ()


_V576_PARENT_OBSERVATION_RECORD_LENGTHS = {
    0x09: frozenset({0x5D, 0x62}),
    0x0E: frozenset({0x1F}), 0x10: frozenset({0x15}),
    0x12: frozenset({0x20}), 0x46: frozenset({0x0D}),
    0x5B: frozenset({0x04}), 0x1E: frozenset({0x1A}),
}


def _decode_v576_server_branch0_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Audited 2022 -> 2023 branch0 observations; not the 3021 branch1 layout.

    Three real root/parent/child/Hit windows validate 0962 in single119 and
    double224 envelopes. Do not extend this to 0966 or mixed record lengths.
    The full owner and parent ID are wire evidence, not a nearest action or
    a current-skill slot. Current generation/typed edge/root proof is still
    mandatory in the ledger. A fingerprint is not a creation/expiry token.
    """
    m = marker_offset
    if len(decoded) == 224 and m in (18, 123) and decoded[5:9] == b"\x6e\xcc\xdd\x00":
        markers = (18, 123)
    elif len(decoded) == 119 and m == 18 and decoded[5:9] == b"\x28\x2a\x74\x00":
        markers = (18,)
    else:
        return None
    # Validate every complete 105-byte record, including the sibling. No
    # following record may supply a missing suffix, owner repeat, or parent.
    for at in markers:
        owner = _UINT32_LE.unpack_from(decoded, at - 4)[0]
        obj = _UINT32_LE.unpack_from(decoded, at + 8)[0]
        parent = _UINT32_LE.unpack_from(decoded, at + 49)[0]
        branch = _UINT32_LE.unpack_from(decoded, at + 53)[0]
        suffix = _UINT32_LE.unpack_from(decoded, at + 69)[0]
        if (
            decoded[at:at + 3] != b"\x09\x62\x00"
            or not 0 < owner < 0xFFFFFFFF
            or owner != _UINT32_LE.unpack_from(decoded, at + 44)[0]
            or decoded[at + 3:at + 8] != bytes(5)
            or not 0 < obj <= 0xFFFFFF
            or _UINT32_LE.unpack_from(decoded, at + 12)[0] != 2023
            or decoded[at + 48] != 0
            or not 0 < parent <= 0xFFFFFF or parent == obj
            or branch & 0xFFFF != 0 or branch >> 16 > 5
            or decoded[at + 57:at + 65] != b"\xff" * 8
            or not 0 < suffix < 0xFFFF
        ):
            return None
    parent = _UINT32_LE.unpack_from(decoded, m + 49)[0]
    suffix = _UINT32_LE.unpack_from(decoded, m + 69)[0]
    sequence = _UINT32_LE.unpack_from(decoded, m + 53)[0] >> 16
    fingerprint = (decoded[m - 4:m] + decoded[m + 3:m + 101]).hex()
    return parent, suffix, sequence, fingerprint


def _decode_v576_server_relay_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Audited relay TLV envelopes; 0966 has a 102-byte body, not 98.

    FFFF and the last four 0966 bytes remain opaque. Neither is a parent
    reference or evidence of a new creation. No new template mapping here.
    """
    m = marker_offset
    if decoded[:1] == b"\xfe":
        start = 21
    elif decoded[:4] == b"\x00\x00\x00\x01":
        start = 24
    else:
        return None
    if len(decoded) < start + 105 or not start + 4 <= m < len(decoded) - 100:
        return None
    observer = _UINT32_LE.unpack_from(decoded, start - 4)[0]
    if not 0x10000 <= observer < 0xFFFFFFFF:
        return None
    # A closed set of independently bounded companion records. No scan for
    # an 09 marker inside another record's opaque body is allowed.
    pairs = {(1, 32), (3, 41), (4, 17), (9, 98), (9, 102),
             (12, 4), (12, 8), (16, 21), (16, 25), (18, 32),
             (30, 30), (76, 9)}
    position, count, selected_end = start, 0, 0
    while position + 7 <= len(decoded) and count < 16:
        owner = _UINT32_LE.unpack_from(decoded, position)[0]
        op = decoded[position + 4]
        size = int.from_bytes(decoded[position + 5:position + 7], "little")
        end = position + 7 + size
        if not 0 < owner < 0xFFFFFFFF or (op, size) not in pairs or end > len(decoded):
            return None
        if position + 4 == m:
            if op != 9 or size not in (98, 102):
                return None
            selected_end = end
        position, count = end, count + 1
    if position != len(decoded) or not selected_end:
        return None
    owner = _UINT32_LE.unpack_from(decoded, m - 4)[0]
    obj = _UINT32_LE.unpack_from(decoded, m + 8)[0]
    tpl = _UINT32_LE.unpack_from(decoded, m + 12)[0]
    parent = _UINT32_LE.unpack_from(decoded, m + 49)[0]
    branch = _UINT32_LE.unpack_from(decoded, m + 53)[0]
    opaque_suffix = _UINT32_LE.unpack_from(decoded, m + 69)[0]
    if (
        owner != _UINT32_LE.unpack_from(decoded, m + 44)[0]
        or decoded[m + 3:m + 8] != bytes(5)
        or not 0 < obj <= 0xFFFFFF or not 0 < tpl <= 0x7FFFFFFF
        or decoded[m + 48] != 0
        or not 0 < parent <= 0xFFFFFF or parent == obj
        or branch & 0xFFFF != 1
        or decoded[m + 57:m + 65] != b"\xff" * 8
        or not 0 < opaque_suffix <= 0xFFFF
    ):
        return None
    key = (decoded[m - 4:m] + decoded[m + 3:selected_end]).hex()
    return parent, opaque_suffix, branch >> 16, key


def _decode_v576_server_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Read a full parent ID from bounded, length-framed object records.

    A returned key is a byte fingerprint, NOT proof of another creation.
    Consumers must keep this server observation from renewing/reviving a
    generation merely because an identical echo arrived after 250 ms.
    The static parent-child edge, same-owner current parent generation and
    independently backed root action remain the ledger's responsibility.
    """
    m = marker_offset
    # Independently checked on all 3,438 saved wire messages: this envelope
    # declares total bytes minus three. Length-walk closure alone would also
    # accept a packet truncated at an entire record boundary.
    relay = _decode_v576_server_relay_parent_tail(decoded, marker_offset)
    if relay is not None:
        return relay
    if len(decoded) < 14 or int.from_bytes(decoded[7:9], "little") + 3 != len(decoded):
        return None
    branch0 = _decode_v576_server_branch0_parent_tail(decoded, marker_offset)
    if branch0 is not None:
        return branch0
    # Eight further captured batch shapes independently verify this framing:
    # owner:u32 + opcode:u8 + payload_length:u16 + opaque payload. Walk from
    # the envelope boundary and require exact closure, never find a marker
    # inside an arbitrary float/coordinate/other unparsed payload.
    cursor = 14
    record_markers = []
    while cursor < len(decoded):
        if cursor + 7 > len(decoded):
            return None
        opcode = decoded[cursor + 4]
        payload_length = int.from_bytes(decoded[cursor + 5:cursor + 7], "little")
        if payload_length not in _V576_PARENT_OBSERVATION_RECORD_LENGTHS.get(opcode, ()):
            return None
        end = cursor + 7 + payload_length
        if end > len(decoded):
            return None
        if opcode == 0x09:
            record_markers.append(cursor + 4)
        cursor = end
    if cursor != len(decoded) or m not in record_markers or decoded[m:m + 3] != b"\x09\x62\x00":
        return None
    # Object siblings must also satisfy the independently known prefix. The
    # payloads of the other audited record types remain entirely opaque.
    for at in record_markers:
        owner = _UINT32_LE.unpack_from(decoded, at - 4)[0]
        if (
            not 0 < owner < 0xFFFFFFFF
            or owner != _UINT32_LE.unpack_from(decoded, at + 44)[0]
            or decoded[at + 3:at + 8] != bytes(5)
            or not 0 < _UINT32_LE.unpack_from(decoded, at + 8)[0] <= 0xFFFFFF
            or not 0 < _UINT32_LE.unpack_from(decoded, at + 12)[0] <= 0x7FFFFFFF
        ):
            return None
    object_id = _UINT32_LE.unpack_from(decoded, m + 8)[0]
    parent_id = _UINT32_LE.unpack_from(decoded, m + 49)[0]
    branch = _UINT32_LE.unpack_from(decoded, m + 53)[0]
    resource_suffix = _UINT32_LE.unpack_from(decoded, m + 69)[0]
    if (
        decoded[m + 48] != 0
        or not 0 < parent_id <= 0xFFFFFF or parent_id == object_id
        or branch & 0xFFFF != 1
        or decoded[m + 57:m + 65] != b"\xff" * 8
        or not 0 < resource_suffix <= 0xFFFF
    ):
        return None
    # Remove only the three-byte operation marker. This is byte-for-byte
    # the 102-byte owner tail of the already audited 112 parent envelope.
    # Coordinates/other dynamic bytes stay opaque and never become IDs.
    fingerprint = (decoded[m - 4:m] + decoded[m + 3:m + 101]).hex()
    return parent_id, resource_suffix, branch >> 16, fingerprint


def _decode_v576_server_shared_mixed_branch0_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Captured ordinary mixed Artillery batches; root proof required.

    Companion owners can differ from both observer and child owner. Their
    complete declared records bound the selected arrow; they do not own it.
    No extension to 0966, relay framing, or unobserved branch0 compositions.
    """
    shapes = {
        # Both 27/17 and 46/17 are complete opaque companion records. The
        # latter occurs before the SAME full parent-bearing arrow record
        # (2026-8-12 Black Dragon, physical frame 5251). Its actor is not the
        # arrow owner. Do not drop the parent merely for this companion type;
        # nor use the companion's payload/owner as action or lineage proof.
        (143, 42, b"\xa7\x05\x8c\x00"): (((0x27, 0x46), 17), (9, 98)),
        (223, 18, b"\x28\x8a\xdc\x00"): ((9, 98), (9, 97)),
        (206, 105, b"\x7c\x1e\xcb\x00"): ((0x10, 25), (1, 28), (4, 13), (9, 98)),
        (174, 18, b"\x92\xd0\xab\x00"): ((9, 98), (1, 32), (0x4B, 9)),
    }
    shape = shapes.get((len(decoded), marker_offset, decoded[5:9]))
    if shape is None or int.from_bytes(decoded[7:9], "little") + 3 != len(decoded):
        return None

    def u32(at: int) -> int:
        return int.from_bytes(decoded[at:at + 4], "little")

    if not 0x10000 <= u32(10) < 0xFFFFFFFF:
        return None
    cursor, selected_end = 14, 0
    for expected_op, expected_size in shape:
        m = cursor + 4
        allowed_ops = expected_op if isinstance(expected_op, tuple) else (expected_op,)
        if (cursor + 7 > len(decoded) or not 0 < u32(cursor) < 0xFFFFFFFF
                or decoded[m] not in allowed_ops
                or int.from_bytes(decoded[m+1:m+3], "little") != expected_size):
            return None
        end = m + 3 + expected_size
        if end > len(decoded):
            return None
        if expected_op == 9:
            if (u32(m-4) != u32(m+44) or decoded[m+3:m+8] != bytes(5)
                    or not 0 < u32(m+8) <= 0xFFFFFF or not 0 < u32(m+12) <= 0x7FFFFFFF):
                return None
            if m == marker_offset:
                if expected_size != 98 or u32(m+12) != 2023:
                    return None
                selected_end = end
        cursor = end
    if cursor != len(decoded) or not selected_end:
        return None
    m = marker_offset
    obj, parent, branch, suffix = u32(m+8), u32(m+49), u32(m+53), u32(m+69)
    if (decoded[m+48] != 0 or not 0 < parent <= 0xFFFFFF or parent == obj
            or branch & 0xFFFF != 0 or branch >> 16 > 5
            or decoded[m+57:m+65] != b"\xff" * 8 or not 0 < suffix <= 0xFFFF):
        return None
    return parent, suffix, branch >> 16, (decoded[m-4:m] + decoded[m+3:selected_end]).hex()


# Complete envelope signatures retained from the raw shared-arrow inventory.
# Composition/order is parsed by record lengths, not selected by this list.
# Unknown envelope headers remain unproved even when their interior looks valid.
_V576_SHARED_ARROW_BATCH_ENVELOPES = frozenset({
    b"\x18\x5a\x98\x00", b"\x18\xba\x98\x00", b"\x28\x2a\x74\x00", b"\x28\x4a\xb5\x00", b"\x28\x4a\xcf\x00", b"\x28\x6a\xc2\x00",
    b"\x28\x8a\x9b\x00", b"\x28\x8a\xdc\x00", b"\x2e\x4c\xc4\x00", b"\x2e\x6c\xb7\x00", b"\x2e\x8c\x83\x00", b"\x65\x27\xca\x00",
    b"\x65\xc7\x89\x00", b"\x6e\x2c\xb6\x00", b"\x6e\xcc\xdd\x00", b"\x7c\x3e\xa4\x00", b"\x7c\x3e\xbe\x00", b"\x7c\x5e\xcb\x00",
    b"\x7c\xde\x97\x00", b"\x92\x30\x84\x00", b"\x92\x30\x9e\x00", b"\x92\xd0\xb8\x00", b"\x97\x35\xe1\x00", b"\x97\xd5\x93\x00",
    b"\x97\xd5\xa0\x00", b"\x97\xd5\xad\x00", b"\x97\xf5\xba\x00", b"\xa7\x05\x7f\x00", b"\xa7\x05\x8c\x00", b"\xa7\xc5\xc0\x00",
    b"\xa7\xe5\xb3\x00", b"\xce\x6c\x88\x00", b"\xce\xac\xe3\x00", b"\xd0\x72\x8d\x00", b"\xd0\x72\x9a\x00", b"\xd0\x92\xb4\x00",
    b"\xd0\xb2\xb4\x00", b"\xe5\x47\xe0\x00", b"\xe5\x87\xc6\x00", b"\xe5\xc7\xc6\x00", b"\xe5\xe7\x78\x00", b"\xfe\x5c\x94\x00",
    b"\xfe\x9c\xbb\x00", b"\xfe\x9c\xe2\x00", b"\xfe\xbc\xa1\x00", b"\xfe\xbc\xae\x00",
})


def _decode_v576_server_framed_shared_arrow_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Read the same branch-0 arrow in a complete length-framed batch.

    The corpus sends independent companion records in varying combinations
    and orders. Their payloads stay opaque. A complete envelope, every record
    boundary, every object prefix and every shared-arrow parent tail must
    validate. The caller MUST still require an independent current root and
    exact same-owner parent generation; this is not a skill/template shortcut.
    0966 adds four opaque trailing bytes, not a different parent identifier.
    """
    if (len(decoded) < 119 or len(decoded) > 65538
            or decoded[5:9] not in _V576_SHARED_ARROW_BATCH_ENVELOPES
            or int.from_bytes(decoded[7:9], "little") + 3 != len(decoded)):
        return None

    def u32(at: int) -> int:
        return int.from_bytes(decoded[at:at + 4], "little")

    if not 0x10000 <= u32(10) < 0xFFFFFFFF:
        return None
    # Existing independently length-framed companion types, plus actual
    # 03/20 and 1e/13 records in the retained 2026-8-12 raw packet inventory.
    lengths = {
        0x01: (28, 32), 0x02: (12, 19), 0x03: (20, 41),
        0x04: (13, 17), 0x05: (26,), 0x09: (93, 97, 98, 102),
        0x0B: (9,), 0x0C: (4, 8), 0x0E: (31, 35), 0x0F: (4,),
        0x10: (21, 25), 0x12: (32,), 0x1E: (13, 14, 18, 22, 26, 30),
        0x23: (4,), 0x27: (17,), 0x46: (13, 17), 0x4B: (9,),
        0x4C: (9,), 0x5B: (4,), 0x73: (12,),
    }
    cursor, count, selected = 14, 0, None
    while cursor + 7 <= len(decoded) and count < 16:
        m = cursor + 4
        opcode = decoded[m]
        size = int.from_bytes(decoded[m + 1:m + 3], "little")
        end = m + 3 + size
        if (not 0 < u32(cursor) < 0xFFFFFFFF
                or size not in lengths.get(opcode, ()) or end > len(decoded)):
            return None
        if opcode == 9:
            owner, obj, template = u32(m - 4), u32(m + 8), u32(m + 12)
            if (owner != u32(m + 44) or decoded[m + 3:m + 8] != bytes(5)
                    or not 0 < obj <= 0xFFFFFF or not 0 < template <= 0x7FFFFFFF):
                return None
            if template == 2023:
                parent, branch, suffix = u32(m + 49), u32(m + 53), u32(m + 69)
                if (size not in (98, 102) or decoded[m + 48] != 0
                        or not 0 < parent <= 0xFFFFFF or parent == obj
                        or branch & 0xFFFF != 0 or branch >> 16 > 5
                        or decoded[m + 57:m + 65] != b"\xff" * 8
                        or not 0 < suffix <= 0xFFFF):
                    return None
                if m == marker_offset:
                    selected = (parent, suffix, branch >> 16,
                                (decoded[m - 4:m] + decoded[m + 3:end]).hex())
        cursor, count = end, count + 1
    return selected if cursor == len(decoded) else None


def _decode_v576_server_rake_branch3_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """5711 branch3: full parent ID, not the branch1 scalar tail layout.

    Three full captures independently join 558 rows to same-owner 5707/5715
    parents. This only exposes wire lineage: a current parent generation and
    independently backed root action are STILL mandatory in the object ledger.
    Floats, the tail scalar and the 0973 trailer do not identify a skill.
    """
    if len(decoded) < 132 or int.from_bytes(decoded[7:9], 'little') + 3 != len(decoded):
        return None
    def u32(at):
        return int.from_bytes(decoded[at:at+4], 'little')
    if not 0x1000000 <= u32(10) < 0xffffffff:
        return None
    # Complete companion records observed in all three raw inventories.
    # Their owners/payloads never provide parent or action evidence.
    lengths = {
        1: (28,32), 2: (15,19), 3: (20,37,41), 4: (13,17),
        9: (93,111,115), 11: (9,), 12: (4,8), 14: (31,35), 15: (4,),
        16: (21,25), 18: (36,), 22: (8,), 30: (13,14,18,22), 35: (4,),
        39: (17,), 70: (13,17), 71: (16,), 73: (16,), 75: (9,), 76: (9,), 115: (12,),
    }
    cursor, count, selected = 14, 0, None
    while cursor + 7 <= len(decoded) and count < 128:
        m = cursor + 4
        op, size = decoded[m], int.from_bytes(decoded[m+1:m+3], 'little')
        end = m + 3 + size
        if not 0 < u32(cursor) < 0xffffffff or size not in lengths.get(op, ()) or end > len(decoded):
            return None
        if op == 9:
            owner, obj, template = u32(m-4), u32(m+8), u32(m+12)
            if (not 0x1000000 <= owner < 0xffffffff or u32(m+44) != owner
                    or decoded[m+3:m+8] != bytes(5) or not 0 < obj <= 0xffffff
                    or not 0 < template <= 0x7fffffff):
                return None
            if size in (111,115):
                parent, branch = u32(m+49), u32(m+53)
                if (template != 5711 or decoded[m+48] != 0
                        or not 0 < parent <= 0xffffff or parent == obj
                        or branch != 0x00010003 or decoded[m+57:m+65] != b'\xff'*8
                        or u32(m+74) != owner):
                    return None
                if m == marker_offset:
                    # No effect-resource ID is claimed from this different tail.
                    selected = (parent, 0, branch >> 16, (decoded[m-4:m]+decoded[m+3:end]).hex())
        cursor, count = end, count + 1
    return selected if cursor == len(decoded) else None


def _decode_v576_server_shared_lineage_parent_tail(
    decoded: bytes, marker_offset: int,
) -> tuple[int, int, int, str] | None:
    """Ordinary shared-child batches, requiring independent root proof.

    Read only complete declared records. 0966's extra four bytes and the
    suffix (including FFFF) remain opaque, never action or creation tokens.
    Called only after the existing audited parent decoders have declined.
    """
    rake = _decode_v576_server_rake_branch3_parent_tail(decoded, marker_offset)
    if rake is not None:
        return rake
    framed = _decode_v576_server_framed_shared_arrow_parent_tail(decoded, marker_offset)
    if framed is not None:
        return framed
    mixed = _decode_v576_server_shared_mixed_branch0_parent_tail(decoded, marker_offset)
    if mixed is not None:
        return mixed
    if len(decoded) < 119 or int.from_bytes(decoded[7:9], "little") + 3 != len(decoded):
        return None

    def u32(at: int) -> int:
        return int.from_bytes(decoded[at:at + 4], "little")

    if not 0x10000 <= u32(10) < 0xFFFFFFFF:
        return None
    # The new branch0 evidence is the same single/double six-arrow family
    # as v24, now with an independently action-proved FFFF scalar. A patched
    # IceSphere record among 3021 siblings is not an Artillery batch.
    branch0_batch = u32(marker_offset + 12) == 2023
    if branch0_batch and not (
        len(decoded) == 119 and marker_offset == 18 and decoded[5:9] == b"\x28\x2a\x74\x00"
        or len(decoded) == 224 and marker_offset in (18, 123) and decoded[5:9] == b"\x6e\xcc\xdd\x00"
    ):
        return None
    # Closed companion formats observed in the saved complete raw windows.
    # Their payloads have no role in choosing owner, parent or skill.
    lengths = {
        0x01: (32,), 0x02: (12, 19), 0x03: (41,), 0x04: (17,), 0x05: (26,),
        0x09: (93, 97, 98, 102), 0x0B: (9,), 0x0C: (4, 8),
        0x0E: (31, 35), 0x0F: (4,), 0x10: (21, 25), 0x12: (32,),
        0x1E: (14, 18, 22, 26, 30), 0x23: (4,), 0x27: (17,), 0x46: (13, 17),
        0x4B: (9,), 0x4C: (9,), 0x5B: (4,), 0x73: (12,),
    }
    if u32(marker_offset + 12) in (5709, 5737):
        # Three independent full-capture inventories expose these complete
        # companions around the SAME branch1 parent-bearing child record.
        # Only their boundaries are used. Do not extend other shared skills,
        # change parent fields, or infer a root from a companion's actor.
        for op, sizes in {
            1:(28,), 2:(15,), 3:(20,37), 4:(13,), 6:(31,), 9:(106,), 10:(4,),
            17:(1,5), 18:(36,), 21:(8,), 22:(8,), 23:(24,), 30:(13,),
            39:(13,), 42:(30,), 50:(28,32), 51:(18,), 71:(16,), 72:(8,),
            73:(16,), 91:(8,), 114:(14,),
        }.items():
            lengths[op] = (*lengths.get(op, ()), *sizes)
    cursor, records, selected = 14, 0, None
    while cursor + 7 <= len(decoded) and records < 16:
        m = cursor + 4
        op = decoded[m]
        size = int.from_bytes(decoded[m + 1:m + 3], "little")
        end = m + 3 + size
        if not 0 < u32(cursor) < 0xFFFFFFFF or size not in lengths.get(op, ()) or end > len(decoded):
            return None
        if op == 9:
            # Validate every object sibling's independent owner/object prefix.
            owner, obj, template = u32(m - 4), u32(m + 8), u32(m + 12)
            if (owner != u32(m + 44) or decoded[m + 3:m + 8] != bytes(5)
                    or not 0 < obj <= 0xFFFFFF or not 0 < template <= 0x7FFFFFFF):
                return None
            if branch0_batch and (template != 2023 or size != 98):
                return None
            if size in (98, 102) and template in (2023, 5709, 5737):
                parent, branch, suffix = u32(m + 49), u32(m + 53), u32(m + 69)
                # Six extension arrows; eight alternating Raindrop children.
                expected_branch, max_sequence = (0, 5) if template == 2023 else (1, 7)
                if (template == 2023 and size != 98
                        or decoded[m + 48] != 0 or not 0 < parent <= 0xFFFFFF or parent == obj
                        or branch & 0xFFFF != expected_branch or branch >> 16 > max_sequence
                        or decoded[m + 57:m + 65] != b"\xff" * 8
                        or not 0 < suffix <= 0xFFFF):
                    return None
                if m == marker_offset:
                    selected = (parent, suffix, branch >> 16,
                                (decoded[m - 4:m] + decoded[m + 3:end]).hex())
        cursor, records = end, records + 1
    return selected if cursor == len(decoded) else None


# Opaque registration-key namespace carried by the existing core bridge.
# It scopes the additional per-Hit guard to this format, not all 11076 rows.
_KALI_11076_PARENT_V28_KEY_PREFIX = "kali-11076-parent-v28:"


def _decode_v576_kali_11076_parent_v28_body(
    decoded: bytes,
) -> tuple[int, int, int, int] | None:
    """Exact 152-byte 096f envelope; no generic shared-tail whitelist.

    Numeric resource proof: actor action 224/signal16 -> 11075; its action3
    /signal66 -> 11076. The scalar moved from M+69 to M+82 in this layout;
    neither scalar nor opaque coordinates identify an action/cast instance.
    """
    if (len(decoded) != 152 or decoded[5:9] != b"\xce\x6c\x95\x00"
            or int.from_bytes(decoded[7:9], "little") + 3 != len(decoded)
            or decoded[18:21] != b"\x46\x0d\x00"
            or decoded[38:41] != b"\x09\x6f\x00"):
        return None
    # Companion: owner@14 + header@18 + 13 bytes ends at34. Selected record:
    # owner@34 + header@38 + 111 bytes ends at152. No unparsed trailing TLV.
    def u32(at: int) -> int:
        return int.from_bytes(decoded[at:at + 4], "little")
    m = 38
    owner, obj, parent = u32(m - 4), u32(m + 8), u32(m + 49)
    scalar = u32(m + 82)
    if (not 0 < owner < 0xFFFFFFFF
            or not owner == u32(10) == u32(14) == u32(m + 44)
            or decoded[m + 3:m + 8] != bytes(5)
            or u32(m + 12) != 11076
            or not 0 < obj <= 0xFFFFFF or not 0 < parent <= 0xFFFFFF
            or parent == obj or decoded[m + 48] != 0
            or u32(m + 53) != (66 << 16 | 3)
            or decoded[m + 57:m + 65] != b"\xff" * 8
            or u32(m + 74) != 0xFFFFFFFF or not 0 < scalar <= 0xFFFF):
        return None
    return owner, obj, parent, scalar


def _decode_v576_kali_11076_parent_v28(
    payload: bytes, direction: str | None,
) -> DecodedV576DamageObjectRegistration | None:
    if (direction != "client_to_server" or len(payload) != 152
            or payload[5:9] != b"\xce\x6c\x95\x00"):
        return None
    decoded = _decode_v576_body(payload, 0)
    fields = _decode_v576_kali_11076_parent_v28_body(decoded)
    if fields is None or any(
        _decode_v576_kali_11076_parent_v28_body(_decode_v576_body(payload, phase))
        is not None for phase in range(1, len(_V576_BODY_XOR_KEY))
    ):
        return None
    owner, obj, parent, scalar = fields
    return DecodedV576DamageObjectRegistration(
        source_actor_id=owner, damage_object_id=obj, projectile_template_id=11076,
        effect_resource_id=scalar, child_sequence=66, xor_phase=0, record_offset=38,
        layout="c2s-udp-kali-parent-096f-152-v28",
        parent_qualified_subtype=True, parent_damage_object_id=parent,
        requires_root_action_proof=True,
        registration_key=_KALI_11076_PARENT_V28_KEY_PREFIX + decoded[34:].hex(),
    )


def decode_v576_server_damage_object_registrations(
    payload: bytes,
    direction: str | None = None,
    known_projectile_template_ids: Iterable[int] = (),
    *,
    include_unmapped: bool = False,
) -> tuple[DecodedV576DamageObjectRegistration, ...]:
    """Decode server broadcasts that bind an actor object to Projectile.

    v576 ``09 5D / 09 61 / 09 6E`` records carry the owner twice, the
    damage-object ID consumed by later direct37 rows, and the exact
    ``weapontable_projectile.RowId``.  The three record kinds describe
    different object-state paths, but their ownership prefix is identical.

    Some accelerator relays invert the inferred UDP direction while leaving
    the complete server record intact.  Direction is therefore only a hint
    for this decoder: the repeated owner, five-byte zero guard, bounded
    runtime object ID and audited Projectile RowId remain mandatory.  A
    five-capture fixed-slot audit found 73/73 exact relay-direction matches
    and no conflicts, in addition to 6755/6755 ordinary server matches.

    By default a caller must provide the audited Projectile RowIds it can map
    to a game skill.  ``include_unmapped`` is reserved for the stateful
    lifecycle ledger: structurally valid unknown records are then returned so
    a reused runtime ID can invalidate its previous known mapping, but the
    unknown template still cannot produce a skill attribution.

    A 205-capture lifecycle audit also proved a small set of exact alternate
    state subtypes.  They are accepted only as audited ``(marker, Projectile
    RowId)`` pairs.  The marker families remain closed by default because the
    same-layout records also contain demonstrated cross-skill conflicts.
    """

    if direction not in (None, "server_to_client", "client_to_server") or len(payload) < 57:
        return ()
    primary_markers = (b"\x09\x5d\x00", b"\x09\x61\x00", b"\x09\x6e\x00")
    audited_alternate_templates = {
        # Full fixed-35 replay: Projectile 11123 is stable on this exact
        # subtype across two captures/two actors (893 native skill rows,
        # zero cross-skill conflicts).  Keep the authorization at the
        # (marker, Projectile) level; other 09/62 rows remain closed.
        b"\x09\x62\x00": frozenset({2214, 11123, 60072}),
        b"\x09\x66\x00": frozenset({92, 2214, 11123, 60072}),
        b"\x09\x6a\x00": frozenset({92, 4004, 4024, 11118}),
        b"\x09\x6f\x00": frozenset({5010, 5025, 5026}),
        b"\x09\x73\x00": frozenset({5010, 5025, 5026, 5084, 11122}),
    }
    # Full-corpus action-qualified rows.  Unlike the packet-validated map
    # above, these pairs do not prove a skill by themselves.  They merely
    # expose owner + object + Projectile to the ledger, which must also find
    # a compatible action epoch before the object may attribute any Hit.
    # This is especially important for Arrow Shower: its long channel emits
    # child arrows well after a generic 2.5-second attachment window.
    action_qualified_alternate_templates = {
        b"\x09\x62\x00": frozenset({2019, 5818}),
        b"\x09\x66\x00": frozenset({5818}),
        b"\x09\x6a\x00": frozenset(
            {63, 2002, 2036, 3025, 3030, 5517, 11084}
        ),
        b"\x09\x6f\x00": frozenset({5714}),
        b"\x09\x73\x00": frozenset({5714}),
    }
    # Client ACT resources expose these as exact parent -> child Projectile
    # edges.  The child template is shared by other skills, so the marker and
    # template alone are intentionally insufficient; the origin ledger must
    # prove a live same-owner parent object before accepting the registration.
    parent_qualified_alternate_templates = {
        b"\x09\x62\x00": frozenset({2023, 3021, 5709, 5737}),
        b"\x09\x66\x00": frozenset({2023, 3021, 5709, 5737}),
        b"\x09\x6f\x00": frozenset({5711}),
        b"\x09\x73\x00": frozenset({5711}),
    }
    markers = primary_markers + tuple(
        dict.fromkeys(
            (
                *audited_alternate_templates,
                *action_qualified_alternate_templates,
                *parent_qualified_alternate_templates,
            )
        )
    )
    if not _v576_wire_may_contain_marker(payload, markers, start_offset=9):
        return ()
    known = frozenset(int(value) for value in known_projectile_template_ids)
    if not known and not include_unmapped:
        return ()

    found: list[DecodedV576DamageObjectRegistration] = []
    seen: set[tuple[int, int, int, int]] = set()
    if include_unmapped or 11076 in known:
        kali_parent = _decode_v576_kali_11076_parent_v28(payload, direction)
        if kali_parent is not None:
            found.append(kali_parent)
            seen.add((kali_parent.source_actor_id, kali_parent.damage_object_id, 11076, 38))
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        for marker_bytes in markers:
            marker_offset = decoded.find(marker_bytes, 9)
            while marker_offset >= 0:
                if marker_offset >= 4 and marker_offset + 48 <= len(decoded):
                    source_actor_id = int.from_bytes(
                        decoded[marker_offset - 4 : marker_offset], "little"
                    )
                    zero_guard = decoded[
                        marker_offset + 3 : marker_offset + 8
                    ]
                    damage_object_id = int.from_bytes(
                        decoded[marker_offset + 8 : marker_offset + 12], "little"
                    )
                    projectile_template_id = int.from_bytes(
                        decoded[marker_offset + 12 : marker_offset + 16], "little"
                    )
                    repeated_source_actor_id = int.from_bytes(
                        decoded[marker_offset + 44 : marker_offset + 48], "little"
                    )
                    key = (
                        source_actor_id,
                        damage_object_id,
                        projectile_template_id,
                        marker_offset,
                    )
                    packet_validated_subtype = projectile_template_id in (
                        audited_alternate_templates.get(marker_bytes, ())
                    )
                    action_qualified_subtype = projectile_template_id in (
                        action_qualified_alternate_templates.get(marker_bytes, ())
                    )
                    parent_qualified_subtype = projectile_template_id in (
                        parent_qualified_alternate_templates.get(marker_bytes, ())
                    )
                    marker_accepts_template = (
                        packet_validated_subtype
                        or action_qualified_subtype
                        or parent_qualified_subtype
                        if marker_bytes in (
                            audited_alternate_templates
                            | action_qualified_alternate_templates
                            | parent_qualified_alternate_templates
                        )
                        else include_unmapped or projectile_template_id in known
                    )
                    if (
                        source_actor_id > 0
                        and repeated_source_actor_id == source_actor_id
                        and zero_guard == b"\0" * 5
                        and 0 < damage_object_id <= 0x00FFFFFF
                        and 0 < projectile_template_id <= 0x7FFFFFFF
                        and marker_accepts_template
                        and (include_unmapped or projectile_template_id in known)
                        and key not in seen
                    ):
                        seen.add(key)
                        parent_fields = (
                            _decode_v576_server_parent_tail(decoded, marker_offset)
                            if parent_qualified_subtype and marker_bytes in (b"\x09\x62\x00", b"\x09\x66\x00")
                            else None
                        )
                        parent_parser = _decode_v576_server_parent_tail
                        requires_root_action_proof = False
                        if parent_fields is None and parent_qualified_subtype:
                            parent_parser = _decode_v576_server_shared_lineage_parent_tail
                            parent_fields = parent_parser(decoded, marker_offset)
                            requires_root_action_proof = parent_fields is not None
                        if parent_fields is not None:
                            valid_phases = sum(
                                parent_parser(
                                    _decode_v576_body(payload, phase), marker_offset,
                                ) is not None
                                for phase in range(len(_V576_BODY_XOR_KEY))
                            )
                            if valid_phases != 1:
                                parent_fields = None
                        found.append(
                            DecodedV576DamageObjectRegistration(
                                source_actor_id=source_actor_id,
                                damage_object_id=damage_object_id,
                                projectile_template_id=projectile_template_id,
                                # Legacy prefix observations keep zero fields;
                                # only the complete audited parent tail adds IDs.
                                effect_resource_id=(parent_fields[1] if parent_fields else 0),
                                child_sequence=(parent_fields[2] if parent_fields else 0),
                                parent_damage_object_id=(parent_fields[0] if parent_fields else 0),
                                registration_key=(parent_fields[3] if parent_fields else ""),
                                requires_root_action_proof=bool(parent_fields and requires_root_action_proof),
                                layout=(
                                    f"s2c-udp-damage-object-parent-observation-{marker_bytes[:2].hex()}-v576"
                                    if parent_fields else "c2s-udp-damage-object-register-107-v576"
                                ),
                                xor_phase=xor_phase,
                                record_offset=marker_offset,
                                packet_validated_subtype=(
                                    packet_validated_subtype
                                ),
                                action_qualified_subtype=(
                                    action_qualified_subtype
                                ),
                                parent_qualified_subtype=(
                                    parent_qualified_subtype
                                ),
                            )
                        )
                marker_offset = decoded.find(marker_bytes, marker_offset + 1)

    # Some valid 09/62 and 09/66 branch-0 child records are carried beside a wider set
    # of companion record types than the historical outer-envelope whitelist.
    # The independent physical inventory already proves one XOR phase and an
    # exact, full-message typed-record walk.  Read the parent from that bounded
    # record only; never search opaque bytes.  The 09/66 form has the same
    # guarded parent prefix and four additional trailing bytes.
    if direction == "server_to_client":
        from .udp_damage_records import inspect_udp_damage_records

        inventory = inspect_udp_damage_records(payload, direction)
        if inventory.complete:
            for typed in inventory.typed_records:
                raw = typed.record_bytes
                if (
                    typed.record_type != 9
                    or typed.declared_length not in (0x62, 0x66)
                    or len(raw) != 4 + 3 + typed.declared_length
                ):
                    continue
                owner = int.from_bytes(raw[0:4], "little")
                obj = int.from_bytes(raw[12:16], "little")
                template = int.from_bytes(raw[16:20], "little")
                repeated_owner = int.from_bytes(raw[48:52], "little")
                parent = int.from_bytes(raw[53:57], "little")
                branch = int.from_bytes(raw[57:61], "little")
                suffix = int.from_bytes(raw[73:77], "little")
                if (
                    template != 2023
                    or owner <= 0
                    or owner == 0xFFFFFFFF
                    or repeated_owner != owner
                    or raw[7:12] != bytes(5)
                    or not 0 < obj <= 0xFFFFFF
                    or raw[52] != 0
                    or not 0 < parent <= 0xFFFFFF
                    or parent == obj
                    or branch & 0xFFFF != 0
                    or branch >> 16 > 5
                    or raw[61:69] != b"\xff" * 8
                    or not 0 < suffix <= 0xFFFF
                    or (not include_unmapped and template not in known)
                ):
                    continue
                key = (owner, obj, template, typed.record_offset)
                enriched = DecodedV576DamageObjectRegistration(
                    source_actor_id=owner,
                    damage_object_id=obj,
                    projectile_template_id=template,
                    effect_resource_id=suffix,
                    child_sequence=branch >> 16,
                    parent_damage_object_id=parent,
                    registration_key=(raw[0:4] + raw[7:]).hex(),
                    requires_root_action_proof=False,
                    layout=(
                        "s2c-udp-damage-object-parent-observation-"
                        f"09{typed.declared_length:02x}-typed-v576"
                    ),
                    xor_phase=inventory.xor_phase,
                    record_offset=typed.record_offset,
                    packet_validated_subtype=False,
                    action_qualified_subtype=False,
                    parent_qualified_subtype=True,
                )
                replaced = False
                for index, current in enumerate(found):
                    current_key = (
                        current.source_actor_id,
                        current.damage_object_id,
                        current.projectile_template_id,
                        current.record_offset,
                    )
                    if current_key == key:
                        if not current.parent_damage_object_id:
                            found[index] = enriched
                        replaced = True
                        break
                if not replaced:
                    found.append(enriched)
    return tuple(found)


@lru_cache(maxsize=64)
def _v576_cached_complete_direct_record_markers(
    payload: bytes,
) -> tuple[int, frozenset[tuple[int, int]]] | None:
    # Cache only small boundary identities, not decoded bodies/actor state.
    # The caller checks the uint16 outer length before admitting a cache key.
    from .udp_damage_records import inspect_udp_damage_records

    inventory = inspect_udp_damage_records(payload, "server_to_client")
    if not inventory.complete:
        return None
    return inventory.xor_phase, frozenset(
        (row.record_offset, row.declared_length)
        for row in inventory.typed_records
        if row.record_type == 8
        and row.declared_length in (0x73, 0x75, 0x77, 0x79)
    )


def _v576_complete_direct_record_markers(
    payload: bytes,
) -> tuple[int, frozenset[tuple[int, int]]] | None:
    """Optional negative proof, never a new legacy hit/admission policy.

    A complete outer envelope and UNIQUE complete typed phase may disprove a
    scanned marker. Incomplete, ambiguous and unparsed legacy formats keep
    their old readers. No numeric/owner/skill filtering defines the boundary.
    The uint16 envelope bounds retained keys to 65538 bytes, with only 64 keys.
    """
    if len(payload) < 21 or int.from_bytes(payload[7:9], "little") + 3 != len(payload):
        return None
    return _v576_cached_complete_direct_record_markers(payload)


def _v576_direct_marker_allowed(
    proof: tuple[int, frozenset[tuple[int, int]]] | None,
    xor_phase: int,
    marker_offset: int,
    declared_length: int,
) -> bool:
    return proof is None or (
        proof[0] == xor_phase and (marker_offset, declared_length) in proof[1]
    )


def _v576_find_next_direct_marker(
    decoded: bytes,
    start_offset: int,
    variants: tuple[tuple[bytes, int], ...],
) -> tuple[int, int]:
    """Find the next exact direct record variant without changing its layout.

    The extended 0075/0079 records retain every established 0073/0077 field
    offset and add only two opaque tail bytes. Returning the declared length
    separately prevents callers from shifting the source/damage/critical slots.
    """
    candidates = (
        (offset, declared_length)
        for marker, declared_length in variants
        for offset in (decoded.find(marker, start_offset),)
        if offset >= 0
    )
    return min(candidates, default=(-1, 0))


def decode_v576_compound_damage(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int],
) -> DecodedV576Message | None:
    """Decode damage records that append a buff/debuff/status payload.

    v576 uses the same structural prefixes for these records while the total
    message length changes with the appended effects. Requiring both the
    damage-prefix sentinels and a catalogued skill ID prevents ordinary state
    synchronization messages from being mistaken for damage.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 57
        or not _v576_wire_may_contain_marker(
            payload,
            (
                *_V576_DIRECT_MARKER_BYTES,
                b"\x00\x00\x00\xff\xff\xff\xff\xff\xff",
            ),
            start_offset=9,
        )
    ):
        return None
    catalog = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(int(value) for value in known_skill_ids)
    )
    if not catalog:
        return None

    direct_marker_proof = (
        _v576_complete_direct_record_markers(payload)
        if _v576_wire_may_contain_marker(
            payload, _V576_DIRECT_MARKER_BYTES, start_offset=9,
        ) else None
    )
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        observer_actor_id = int.from_bytes(decoded[10:14], "little")
        if observer_actor_id == 0:
            continue

        record_candidates: list[tuple[int, int, int | None, str, int]] = []
        extended_record_offsets: set[int] = set()
        marker_offset = 0
        while True:
            marker_offset, declared_length = _v576_find_next_direct_marker(
                decoded, marker_offset, _V576_DIRECT37_RECORD_MARKERS
            )
            if marker_offset < 0:
                break
            if not _v576_direct_marker_allowed(
                direct_marker_proof, xor_phase, marker_offset, declared_length
            ):
                marker_offset += 1
                continue
            if declared_length == 0x75:
                extended_record_offsets.add(marker_offset)
            # Ordinary direct records place source/damage at +8/+19. Summon
            # ticks use FF FF FF in the sentinel where other records use
            # 00 FF FF. Effect-prefixed records use the shifted +11/+20 form.
            direct37_layout = (
                marker_offset + 27 <= len(decoded)
                and decoded[marker_offset + 16 : marker_offset + 19]
                in {b"\x00\xff\xff", b"\xff\xff\xff"}
            )
            # Chain Lightning can emit an otherwise identical continuation
            # record with no skill ID and, in one variant, no FF sentinel.
            # In that form the outer observer/target and inner source all name
            # the casting actor. The marker offset is stable across both seen
            # v576 variants and keeps this from matching ordinary status sync.
            skillless_continuation_layout = (
                marker_offset in {42, 52}
                and marker_offset + 27 <= len(decoded)
                and int.from_bytes(
                    decoded[marker_offset + 8 : marker_offset + 12], "little"
                )
                == observer_actor_id
            )
            # A compact Chain Lightning continuation uses the same direct37
            # record at marker +29. Its outer actor fields identify the caster,
            # while the actual target is stored immediately before the marker.
            # Requiring the exact message family, source/observer identity and
            # a distinct non-zero target keeps similarly shaped status sync
            # packets out of the damage stream.
            skillless_direct29_layout = (
                len(decoded) == 159
                and payload[5:9] == b"\x6e\x2c\x9c\x00"
                and marker_offset == 29
                and marker_offset + 27 <= len(decoded)
                and int.from_bytes(
                    decoded[marker_offset + 8 : marker_offset + 12], "little"
                )
                == observer_actor_id
                and int.from_bytes(decoded[25:29], "little") not in {
                    0,
                    observer_actor_id,
                }
            )
            continuation_target_actor_id = (
                int.from_bytes(
                    decoded[marker_offset - 4 : marker_offset], "little"
                )
                if marker_offset >= 4
                else 0
            )
            # Some continuation hits append a status record. A catalogued
            # skill ID in that trailing status belongs to the applied effect,
            # not to the damage record itself. The outer target repeats the
            # caster in this layout; the real damage target is the actor stored
            # directly before the marker.
            status_appended_continuation_layout = (
                direct37_layout
                and marker_offset in {42, 52}
                and int.from_bytes(decoded[14:18], "little")
                == observer_actor_id
                and int.from_bytes(
                    decoded[marker_offset + 8 : marker_offset + 12], "little"
                )
                == observer_actor_id
                and continuation_target_actor_id not in {
                    0,
                    observer_actor_id,
                }
            )
            marker_target_actor_id = (
                int.from_bytes(
                    decoded[marker_offset - 4 : marker_offset], "little"
                )
                if marker_offset >= 4
                else 0
            )
            marker_source_actor_id = int.from_bytes(
                decoded[marker_offset + 8 : marker_offset + 12], "little"
            )
            marker_damage = int.from_bytes(
                decoded[marker_offset + 19 : marker_offset + 27], "little"
            )
            player_result_variant_layout = (
                marker_source_actor_id > 0x00FFFFFF
                and 0 < marker_target_actor_id <= 0x00FFFFFF
                and marker_target_actor_id != marker_source_actor_id
                and _is_valid_v576_damage_amount(marker_damage)
            )
            if (
                direct37_layout
                or skillless_continuation_layout
                or player_result_variant_layout
            ):
                # direct37 records store the per-hit critical flag 35 bytes
                # after the record marker. Bit 0 is clear for a normal hit and
                # set for a critical hit, including summon and continuation
                # damage such as Lightning Relic and Chain Lightning.
                critical_offset = marker_offset + 35
                record_candidates.append(
                    (
                        marker_offset + 8,
                        marker_offset + 19,
                        critical_offset if critical_offset < len(decoded) else None,
                        (
                            "direct37-continuation29"
                            if skillless_direct29_layout
                            else (
                                "direct37-status-continuation"
                                if status_appended_continuation_layout
                                else (
                                    "direct37-player"
                                    if player_result_variant_layout
                                    and not direct37_layout
                                    else "direct37"
                                )
                            )
                        ),
                        marker_offset,
                    )
                )
            if (
                marker_offset + 28 <= len(decoded)
                and decoded[marker_offset + 18 : marker_offset + 20] == b"\xff\xff"
            ):
                record_candidates.append(
                    (
                        marker_offset + 11,
                        marker_offset + 20,
                        None,
                        "direct-shifted",
                        marker_offset,
                    )
                )
            marker_offset += 1

        marker_offset = 0
        while True:
            marker_offset, declared_length = _v576_find_next_direct_marker(
                decoded, marker_offset, _V576_DIRECT41_RECORD_MARKERS
            )
            if marker_offset < 0:
                break
            if not _v576_direct_marker_allowed(
                direct_marker_proof, xor_phase, marker_offset, declared_length
            ):
                marker_offset += 1
                continue
            if declared_length == 0x79:
                extended_record_offsets.add(marker_offset)
            direct41_layout = (
                marker_offset + 31 <= len(decoded)
                and decoded[marker_offset + 21 : marker_offset + 23] == b"\xff\xff"
            )
            # A v576 174-byte hit+status family replaces the ordinary FF FF
            # sentinel with a small status-vector index. It otherwise keeps
            # the exact direct41 source/damage layout. Straight Line Impact
            # and Heavy Hammer both emit this form on the hit that applies the
            # appended effect, so rejecting it loses a real hit intermittently.
            direct41_status_layout = (
                len(decoded) == 174
                and payload[5:9] == b"\x92\x30\xab\x00"
                and marker_offset == 52
                and decoded[marker_offset + 21 : marker_offset + 23]
                in {b"\x0b\x00", b"\x0d\x00"}
                and int.from_bytes(
                    decoded[marker_offset + 8 : marker_offset + 12], "little"
                )
                == int.from_bytes(decoded[14:18], "little")
            )
            marker_target_actor_id = (
                int.from_bytes(
                    decoded[marker_offset - 4 : marker_offset], "little"
                )
                if marker_offset >= 4
                else 0
            )
            marker_source_actor_id = int.from_bytes(
                decoded[marker_offset + 8 : marker_offset + 12], "little"
            )
            marker_damage = int.from_bytes(
                decoded[marker_offset + 23 : marker_offset + 31], "little"
            )
            player_result_variant_layout = (
                marker_source_actor_id > 0x00FFFFFF
                and 0 < marker_target_actor_id <= 0x00FFFFFF
                and marker_target_actor_id != marker_source_actor_id
                and _is_valid_v576_damage_amount(marker_damage)
            )
            if (
                direct41_layout
                or direct41_status_layout
                or player_result_variant_layout
            ):
                # direct41 stores the per-hit result flags immediately after
                # the four-byte sequence value. Bit 0 at +39 is the critical
                # flag. The old +92 offset belongs to a changing combat-state
                # value and made multi-hit skills such as Ice Blade and
                # Firewall report effectively random critical hits.
                critical_offset = marker_offset + 39
                record_candidates.append(
                    (
                        marker_offset + 8,
                        marker_offset + 23,
                        critical_offset if critical_offset < len(decoded) else None,
                        (
                            "direct41-status"
                            if direct41_status_layout
                            else (
                                "direct41-player"
                                if player_result_variant_layout
                                and not direct41_layout
                                else "direct41"
                            )
                        ),
                        marker_offset,
                    )
                )
            marker_offset += 1

        if (
            len(decoded) >= 57
            and decoded[40:49]
            == b"\x00\x00\x00\xff\xff\xff\xff\xff\xff"
        ):
            record_candidates.append((14, 49, None, "periodic49", 40))

        for (
            source_offset,
            damage_offset,
            critical_offset,
            family,
            marker_offset,
        ) in record_candidates:
            exact_skill_id = _direct_result_skill_id(
                decoded,
                catalog,
                family,
                marker_offset,
            )
            candidate_skill_ids = _embedded_skill_candidates(
                decoded,
                catalog,
                excluded_ranges=((damage_offset, damage_offset + 8),),
            )
            skill_id = (
                exact_skill_id
                if exact_skill_id is not None
                else candidate_skill_ids[0] if candidate_skill_ids else None
            )
            if skill_id is None and (
                family == "direct-shifted" or marker_offset >= 80
            ):
                # A small set of effect-first records stores the owning skill
                # in the prefix immediately before the shifted damage record.
                candidate_skill_ids = _embedded_skill_candidates(
                    decoded[:marker_offset],
                    catalog,
                    start_offset=20,
                    excluded_ranges=((damage_offset, damage_offset + 8),),
                )
                skill_id = (
                    candidate_skill_ids[0] if candidate_skill_ids else None
                )
            appended_status_skill_id: int | None = None
            if family == "direct37-status-continuation" and exact_skill_id is None:
                # Unproved embedded IDs may belong to the appended status
                # vector. Preserve the old fallback only without a bounded,
                # catalogued fixed-slot ID; do not erase exact hit evidence.
                appended_status_skill_id = skill_id
                skill_id = None
                candidate_skill_ids = ()
            source_actor_id = int.from_bytes(
                decoded[source_offset : source_offset + 4], "little"
            )
            damage = int.from_bytes(
                decoded[damage_offset : damage_offset + 8], "little"
            )
            outer_target_actor_id = int.from_bytes(decoded[14:18], "little")
            marker_target_actor_id = (
                int.from_bytes(
                    decoded[marker_offset - 4 : marker_offset], "little"
                )
                if marker_offset >= 4
                else 0
            )
            skill_object_continuation_layout = (
                family == "direct37"
                and 0 < source_actor_id <= 0x00FFFFFF
                and 0 < outer_target_actor_id <= 0x00FFFFFF
                and source_actor_id != outer_target_actor_id
            )
            player_continuation_layout = (
                family in {
                    "direct37",
                    "direct37-player",
                    "direct41",
                    "direct41-player",
                    "direct41-status",
                }
                and source_actor_id > 0x00FFFFFF
                and 0 < marker_target_actor_id <= 0x00FFFFFF
                and marker_target_actor_id != source_actor_id
            )
            if (
                source_actor_id == 0
                or not _is_valid_v576_damage_amount(damage)
            ):
                continue
            # Some multi-hit continuation records carry the same direct-hit
            # structure but omit the owning skill ID. Keep those records and
            # let the stateful event decoder attribute them to an active cast.
            # Other compound families still require an embedded catalogued ID
            # because their payloads can be confused with state sync packets.
            if skill_id is None and not (
                (
                    family == "direct37"
                    and marker_offset in {42, 52}
                    and source_actor_id == observer_actor_id
                )
                or family in {
                    "direct37-continuation29",
                    "direct37-status-continuation",
                }
                or skill_object_continuation_layout
                or player_continuation_layout
            ):
                continue
            # Effect-bearing direct41 records repeat the attacker in the outer
            # target slot.  The struck entity is stored immediately before the
            # damage marker.  Treating the outer value as the Boss made the
            # active target oscillate between a player and several monster
            # IDs, so valid debuffs were filtered from the live status page.
            direct41_embedded_target = (
                family in {"direct41", "direct41-status"}
                and outer_target_actor_id == source_actor_id
                and marker_target_actor_id not in {
                    0,
                    source_actor_id,
                    observer_actor_id,
                }
            )
            player_embedded_target = (
                player_continuation_layout
                and marker_target_actor_id not in {
                    0,
                    source_actor_id,
                    observer_actor_id,
                }
            )
            target_offset = (
                marker_offset - 4
                if (
                    family == "direct37-status-continuation"
                    or direct41_embedded_target
                    or player_embedded_target
                )
                else (25 if family == "direct37-continuation29" else 14)
            )
            return DecodedV576Message(
                kind="damage_hit",
                payload_length=len(payload),
                layout=(
                    (
                        "s2c-udp-hit-effect-direct37-"
                        + (
                            "object-extended-continuation-v576"
                            if marker_offset in extended_record_offsets
                            else "object-continuation-v576"
                        )
                    )
                    if skill_id is None and skill_object_continuation_layout
                    else (
                        f"s2c-udp-hit-effect-{family}-extended-continuation-v576"
                        if marker_offset in extended_record_offsets
                        else f"s2c-udp-hit-effect-{family}-continuation-v576"
                    )
                    if skill_id is None
                    else (
                        f"s2c-udp-hit-effect-{family}-extended-v576"
                        if marker_offset in extended_record_offsets
                        else f"s2c-udp-hit-effect-{family}-v576"
                    )
                ),
                skill_id=skill_id,
                damage=damage,
                critical=(
                    bool(decoded[critical_offset] & 0x01)
                    if critical_offset is not None
                    else None
                ),
                source_actor_id=source_actor_id,
                target_actor_id=int.from_bytes(
                    decoded[target_offset : target_offset + 4], "little"
                ),
                alternate_target_actor_id=(
                    marker_target_actor_id
                    if skill_object_continuation_layout
                    else None
                ),
                observer_actor_id=observer_actor_id,
                appended_status_skill_id=appended_status_skill_id,
                candidate_skill_ids=candidate_skill_ids,
                skill_id_exact=exact_skill_id is not None,
                damage_result_group_id=(
                    int.from_bytes(
                        decoded[marker_offset + 4 : marker_offset + 8],
                        "little",
                    )
                    if family.startswith("direct37")
                    and marker_offset + 8 <= len(decoded)
                    else None
                ),
                damage_object_id=(
                    int.from_bytes(
                        decoded[marker_offset + 13 : marker_offset + 17],
                        "little",
                    )
                    if family.startswith("direct37")
                    and marker_offset + 17 <= len(decoded)
                    else None
                ),
                # Preserve the exact record that produced this damage row.
                # Compound packets can contain several direct37/direct41
                # records plus unrelated status skill IDs; downstream
                # attribution must compare fields relative to this marker
                # instead of scanning the complete payload.
                record_offset=marker_offset,
            )
    return None


def decode_v576_embedded_direct_damage_events(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int] = (),
) -> tuple[DecodedV576Message, ...]:
    """Return every actor-to-entity direct37 record in one message.

    A crowded v576 message can bundle an ordinary hit with one or more
    direct37 result records.  ``decode_v576_payload`` intentionally returns a
    single primary message, so the trailing records need an independent pass
    to keep the raw damage ledger complete. Actor ownership and target
    validity are checked by the stateful decoder; this is important for
    short-ID summons such as towers, Azuna and relics.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 57
        or not _v576_wire_may_contain_marker(
            payload,
            tuple(marker for marker, _length in _V576_DIRECT37_RECORD_MARKERS),
            start_offset=9,
        )
    ):
        return ()

    catalog = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(int(skill_id) for skill_id in known_skill_ids)
    )
    direct_marker_proof = _v576_complete_direct_record_markers(payload)
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        observer_actor_id = int.from_bytes(decoded[10:14], "little")
        if observer_actor_id == 0:
            continue

        found: list[DecodedV576Message] = []
        marker_offsets: list[tuple[int, int]] = []
        marker_offset, declared_length = _v576_find_next_direct_marker(
            decoded, 0, _V576_DIRECT37_RECORD_MARKERS
        )
        while marker_offset >= 0:
            if _v576_direct_marker_allowed(
                direct_marker_proof, xor_phase, marker_offset, declared_length
            ):
                marker_offsets.append((marker_offset, declared_length))
            marker_offset, declared_length = _v576_find_next_direct_marker(
                decoded, marker_offset + 1, _V576_DIRECT37_RECORD_MARKERS
            )
        for marker_index, (marker_offset, declared_length) in enumerate(marker_offsets):
            if marker_offset < 4 or marker_offset + 27 > len(decoded):
                continue

            target_actor_id = int.from_bytes(
                decoded[marker_offset - 4 : marker_offset], "little"
            )
            source_actor_id = int.from_bytes(
                decoded[marker_offset + 8 : marker_offset + 12], "little"
            )
            damage = int.from_bytes(
                decoded[marker_offset + 19 : marker_offset + 27], "little"
            )
            if (
                source_actor_id > 0
                and 0 < target_actor_id <= 0x00FFFFFF
                and target_actor_id != source_actor_id
                and _is_valid_v576_damage_amount(damage)
            ):
                # A result-vector skill belongs to this direct37 row only when
                # its fixed slot is reached before another direct37 marker.
                # Without this boundary check the first row of a compound
                # packet can accidentally read an integer from the next hit
                # and acquire a false, but catalogued, skill ID.
                next_marker_offset = (
                    marker_offsets[marker_index + 1][0]
                    if marker_index + 1 < len(marker_offsets)
                    else len(decoded)
                )
                exact_skill_id = _direct_result_skill_id(
                    decoded,
                    catalog,
                    "direct37",
                    marker_offset,
                )
                if exact_skill_id is not None:
                    exact_offsets = (
                        marker_offset + relative
                        for relative in (102, 114)
                    )
                    if not any(
                        offset + 4 <= next_marker_offset
                        and int.from_bytes(
                            decoded[offset : offset + 4], "little"
                        )
                        == exact_skill_id
                        for offset in exact_offsets
                    ):
                        exact_skill_id = None
                critical_offset = marker_offset + 35
                found.append(
                    DecodedV576Message(
                        kind="damage_hit",
                        payload_length=len(payload),
                        layout=(
                            "s2c-udp-hit-effect-direct37-extended-bundled-v576"
                            if declared_length == 0x75
                            else "s2c-udp-hit-effect-direct37-bundled-v576"
                        ),
                        skill_id=exact_skill_id,
                        damage=damage,
                        critical=(
                            bool(decoded[critical_offset] & 0x01)
                            if critical_offset < len(decoded)
                            else None
                        ),
                        source_actor_id=source_actor_id,
                        target_actor_id=target_actor_id,
                        observer_actor_id=observer_actor_id,
                        skill_id_exact=exact_skill_id is not None,
                        damage_result_group_id=int.from_bytes(
                            decoded[
                                marker_offset + 4 : marker_offset + 8
                            ],
                            "little",
                        ),
                        damage_object_id=int.from_bytes(
                            decoded[
                                marker_offset + 13 : marker_offset + 17
                            ],
                            "little",
                        ),
                        record_offset=marker_offset,
                    )
                )
        if found:
            return tuple(found)
    return ()


def decode_v576_embedded_direct41_damage_events(
    payload: bytes, direction: str | None, known_skill_ids: Iterable[int] = (),
) -> tuple[DecodedV576Message, ...]:
    """Enumerate complete player direct41 rows; do not scan arbitrary bytes.

    Unknown record layouts and entity attack variants keep their old admission
    policy. Object/group slots are not imported from direct37 by analogy.
    """
    if (direction != "server_to_client"
            or not _v576_wire_may_contain_marker(
                payload,
                tuple(marker for marker, _length in _V576_DIRECT41_RECORD_MARKERS),
                start_offset=18,
            )):
        return ()
    from .udp_damage_records import inspect_udp_damage_records
    inventory = inspect_udp_damage_records(payload, direction)
    catalog = frozenset(known_skill_ids)
    result = []
    for row in inventory.damage_records:
        if (row.family != "direct41" or not row.admissible_fields
                or not 0x00FFFFFF < row.source_actor_id < 0xFFFFFFFF
                or not 0 < row.target_actor_id <= 0x00FFFFFF):
            continue
        skill = next((value for offset, value in row.raw_skill_slots
                      if offset == 106 and value in catalog), None)
        result.append(DecodedV576Message(
            kind="damage_hit", payload_length=len(payload),
            layout=(
                "s2c-udp-hit-effect-direct41-extended-bundled-v576"
                if row.declared_length == 0x79
                else "s2c-udp-hit-effect-direct41-bundled-v576"
            ), skill_id=skill,
            damage=row.damage, critical=row.critical, source_actor_id=row.source_actor_id,
            target_actor_id=row.target_actor_id, observer_actor_id=row.observer_actor_id,
            skill_id_exact=skill is not None, record_offset=row.record_offset,
        ))
    return tuple(result)


def decode_v576_projectile_damage(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int] | None = None,
) -> DecodedV576Message | None:
    """Decode short hits emitted by a persistent skill object.

    Ice Shot and Ice Spike create a projectile or a short-lived damage object.
    Its hit record names the player as the source but stores the struck object
    beside the direct37 marker instead of in the ordinary outer target field.
    The two observed v576 wire families share that layout.
    Some contain a registered skill at direct37 +102/+114. Read only those
    bounded slots; without a supplied damage-skill whitelist, keep the old
    unknown skill. The struck target is at marker-4, the result-group at +4,
    and the damage-object ID at +13, as in the supplemental direct37 reader.
    """
    if direction not in (None, "server_to_client"):
        return None
    layouts = {
        (148, b"\x92\x30\x91\x00"): 8,
        (171, b"\x28\x4a\xa8\x00"): 10,
    }
    xor_phase = layouts.get((len(payload), payload[5:9]))
    if xor_phase is None:
        return None
    decoded = _decode_v576_body(payload, xor_phase)
    observer_actor_id = int.from_bytes(decoded[10:14], "little")
    marker_offset, declared_length = _v576_find_next_direct_marker(
        decoded, 0, _V576_DIRECT37_RECORD_MARKERS
    )
    if marker_offset < 0:
        return None
    direct_marker_proof = _v576_complete_direct_record_markers(payload)
    while marker_offset >= 0 and not _v576_direct_marker_allowed(
        direct_marker_proof, xor_phase, marker_offset, declared_length,
    ):
        marker_offset, declared_length = _v576_find_next_direct_marker(
            decoded, marker_offset + 1, _V576_DIRECT37_RECORD_MARKERS
        )
    if marker_offset < 4 or marker_offset + 43 > len(decoded):
        return None
    target_actor_id = int.from_bytes(
        decoded[marker_offset - 4 : marker_offset], "little"
    )
    source_actor_id = int.from_bytes(
        decoded[marker_offset + 8 : marker_offset + 12], "little"
    )
    damage = int.from_bytes(
        decoded[marker_offset + 19 : marker_offset + 27], "little"
    )
    if (
        observer_actor_id == 0
        or source_actor_id != observer_actor_id
        or target_actor_id in {0, source_actor_id}
        or not _is_valid_v576_damage_amount(damage)
    ):
        return None
    critical_offset = marker_offset + 35
    catalog = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(int(value) for value in known_skill_ids)
        if known_skill_ids is not None
        else frozenset()
    )
    exact_skill_id = (
        _direct_result_skill_id(
            decoded,
            catalog,
            "direct37",
            marker_offset,
            record_end=_direct_result_fixed_slot_end(decoded, marker_offset),
        )
        if catalog
        else None
    )
    return DecodedV576Message(
        kind="damage_hit",
        payload_length=len(payload),
        layout="s2c-udp-projectile-direct37-v576",
        skill_id=exact_skill_id,
        skill_id_exact=exact_skill_id is not None,
        damage=damage,
        critical=(
            bool(decoded[critical_offset] & 0x01)
            if critical_offset < len(decoded)
            else None
        ),
        source_actor_id=source_actor_id,
        target_actor_id=target_actor_id,
        observer_actor_id=observer_actor_id,
        damage_result_group_id=int.from_bytes(
            decoded[marker_offset + 4 : marker_offset + 8], "little"
        ),
        damage_object_id=int.from_bytes(
            decoded[marker_offset + 13 : marker_offset + 17], "little"
        ),
        record_offset=marker_offset,
    )


_V576_STATUS_DAMAGE_TAILS = frozenset(
    bytes.fromhex(value)
    for value in (
        "0001000000000001b100ffffffff",
        "0001010000000001b1004b180000",
        "0001040000000001b1004b180000",
        "0001000000000001b1004b180000",
        "00010000000001014501ffffffff",
        "00010000000001012a00ffffffff",
        "00010000000000012c00ffffffff",
        "0001020000000001b1004b180000",
        # Ecstatic Dance Lv.2 names its team-buff status skill in the same
        # authoritative HP-delta record.  All four result flags are damage;
        # the canonical displayed damage skill is 5118.
        "0001000000000001b100d91c0600",
        "0001010000000001b100d91c0600",
        "0001020000000001b100d91c0600",
        "0001040000000001b100d91c0600",
        # Swordmaster delayed/derived damage uses the same signed Boss HP
        # delta record with a different v576 result code (0x012f). Only the
        # 0x0101 result is included by the official damage ledger; the 0x0100
        # and 0x0104 state snapshots must not be counted as another hit.
        "0001010000000001b1002f010000",
        # Berserker Combo emits one fixed supplemental hit for each trigger.
        "0001000000000001a100ffffffff",
    )
)

_V576_SWORDMASTER_RESULT_VARIANT_TAILS = frozenset(
    bytes.fromhex(value)
    for value in (
        "0001000000000001b1002f010000",
        "0001040000000001b1002f010000",
    )
)

_V576_STATUS_DAMAGE_SKILL_IDS = {
    bytes.fromhex("0001000000000001a100ffffffff"): 501,
    bytes.fromhex("0001000000000001b1004b180000"): 6219,
    bytes.fromhex("0001010000000001b1004b180000"): 6219,
    bytes.fromhex("0001020000000001b1004b180000"): 6219,
    bytes.fromhex("0001040000000001b1004b180000"): 6219,
    bytes.fromhex("0001000000000001b100d91c0600"): 5118,
    bytes.fromhex("0001010000000001b100d91c0600"): 5118,
    bytes.fromhex("0001020000000001b100d91c0600"): 5118,
    bytes.fromhex("0001040000000001b100d91c0600"): 5118,
}


def decode_v576_status_damage_events(
    payload: bytes,
    direction: str | None,
    *,
    include_swordmaster_result_variants: bool = False,
) -> tuple[DecodedV576Message, ...]:
    """Decode every negative HP/status damage record in one server message."""
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 48
        or not _v576_wire_may_contain_marker(
            payload,
            tuple(bytes((0x1C, 0x1B, 0x00, subtype)) for subtype in range(6)),
            start_offset=9,
        )
    ):
        return ()
    found: list[DecodedV576Message] = []
    seen: set[tuple[int, int, int, int, int]] = set()
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        observer_actor_id = int.from_bytes(decoded[10:14], "little")
        if observer_actor_id == 0:
            continue
        for subtype in range(6):
            marker = bytes((0x1C, 0x1B, 0x00, subtype))
            marker_offset = 9
            while True:
                marker_offset = decoded.find(marker, marker_offset)
                if marker_offset < 0:
                    break
                # target(4), marker(4), signed HP delta(8), source(4), tail(14)
                if marker_offset < 4 or marker_offset + 30 > len(decoded):
                    marker_offset += 1
                    continue
                target_actor_id = int.from_bytes(
                    decoded[marker_offset - 4 : marker_offset], "little"
                )
                signed_delta = int.from_bytes(
                    decoded[marker_offset + 4 : marker_offset + 12],
                    "little",
                    signed=True,
                )
                source_actor_id = int.from_bytes(
                    decoded[marker_offset + 12 : marker_offset + 16], "little"
                )
                tail = decoded[marker_offset + 16 : marker_offset + 30]
                fingerprint = (
                    xor_phase,
                    marker_offset,
                    target_actor_id,
                    source_actor_id,
                    signed_delta,
                )
                is_swordmaster_result_variant = (
                    tail in _V576_SWORDMASTER_RESULT_VARIANT_TAILS
                )
                if (
                    # Verified subtype5 B1 HP-delta results in 1018 captures.
                    # Do not admit self HP costs or unreviewed subtype5 tails.
                    (subtype != 5 or (
                        tail == bytes.fromhex("0001000000000001b100ffffffff")
                        and source_actor_id != target_actor_id
                    ))
                    and source_actor_id != 0
                    and target_actor_id != 0
                    and -1_000_000_000_000 <= signed_delta < 0
                    and _is_valid_v576_damage_amount(-signed_delta)
                    and (
                        tail in _V576_STATUS_DAMAGE_TAILS
                        or (
                            include_swordmaster_result_variants
                            and is_swordmaster_result_variant
                        )
                    )
                    and fingerprint not in seen
                ):
                    seen.add(fingerprint)
                    found.append(
                        DecodedV576Message(
                            kind="damage_hit",
                            payload_length=len(payload),
                            layout=(
                                "s2c-udp-status-damage-"
                                f"subtype{subtype}"
                                + (
                                    "-swordmaster-result-variant"
                                    if is_swordmaster_result_variant
                                    else ""
                                )
                                + "-v576"
                            ),
                            damage=-signed_delta,
                            critical=False,
                            skill_id=(
                                303
                                if is_swordmaster_result_variant
                                else _V576_STATUS_DAMAGE_SKILL_IDS.get(tail)
                            ),
                            source_actor_id=source_actor_id,
                            target_actor_id=target_actor_id,
                            observer_actor_id=observer_actor_id,
                            periodic_kind="burn" if subtype == 0 else "",
                            periodic_label="烧伤" if subtype == 0 else "",
                            appended_status_skill_id=int.from_bytes(
                                tail[10:14], "little"
                            ),
                            status_damage_tail_hex=tail.hex(),
                            record_offset=marker_offset,
                        )
                    )
                marker_offset += 1
    return tuple(found)


def decode_v576_periodic_status_damage(
    payload: bytes,
    direction: str | None,
) -> DecodedV576Message | None:
    """Return the first legacy subtype-zero Burn record, if present."""
    for decoded in decode_v576_status_damage_events(payload, direction):
        if decoded.periodic_kind == "burn" and decoded.skill_id is None:
            return DecodedV576Message(
                kind=decoded.kind,
                payload_length=decoded.payload_length,
                layout="s2c-udp-periodic-burn-v576",
                damage=decoded.damage,
                critical=decoded.critical,
                source_actor_id=decoded.source_actor_id,
                target_actor_id=decoded.target_actor_id,
                observer_actor_id=decoded.observer_actor_id,
                periodic_kind=decoded.periodic_kind,
                periodic_label=decoded.periodic_label,
            )
    return None


def decode_v576_healing(
    payload: bytes,
    direction: str | None,
) -> tuple[DecodedV576Message, ...]:
    """Decode authoritative positive HP deltas sent by the v576 server.

    Healing is not represented by the ordinary damage-hit layouts.  The
    server emits one or more 34-byte HP-change records containing target,
    effect 6940, a signed delta and the target repeated as the state owner.
    Positive deltas are effective healing; zero records describe unchanged
    party members and negative deltas belong to damage/state synchronization.
    """
    if (
        direction not in (None, "server_to_client")
        or len(payload) < 48
        or not _v576_wire_may_contain_marker(
            payload, (b"\x1c\x1b\x00\x04", b"\x1c\x1b\x00\x05"), start_offset=14
        )
    ):
        return ()
    marker = b"\x1c\x1b\x00"
    required_tail = b"\x00\x01\x00\x00\x00\x00\x00\x01\x00\x00\xff\xff\xff\xff"
    found: list[DecodedV576Message] = []
    seen: set[tuple[int, int, int]] = set()
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        observer_actor_id = int.from_bytes(decoded[10:14], "little")
        if observer_actor_id == 0:
            continue
        marker_offset = 14
        while True:
            marker_offset = decoded.find(marker, marker_offset)
            if marker_offset < 0:
                break
            if marker_offset < 4 or marker_offset + 30 > len(decoded):
                marker_offset += 1
                continue
            target_actor_id = int.from_bytes(
                decoded[marker_offset - 4 : marker_offset], "little"
            )
            healing = int.from_bytes(
                decoded[marker_offset + 4 : marker_offset + 12],
                "little",
                signed=True,
            )
            repeated_target = int.from_bytes(
                decoded[marker_offset + 12 : marker_offset + 16], "little"
            )
            tail = decoded[marker_offset + 16 : marker_offset + 30]
            fingerprint = (target_actor_id, healing, marker_offset)
            if (
                target_actor_id != 0
                and decoded[marker_offset + 3] in (4, 5)
                and repeated_target == target_actor_id
                and 0 < healing <= 1_000_000_000_000
                and tail == required_tail
                and fingerprint not in seen
            ):
                seen.add(fingerprint)
                found.append(
                    DecodedV576Message(
                        kind="healing",
                        payload_length=len(payload),
                        layout=("s2c-udp-healing-hp-delta-05-v576"
                                if decoded[marker_offset + 3] == 5
                                else "s2c-udp-healing-hp-delta-v576"),
                        damage=healing,
                        critical=False,
                        source_actor_id=observer_actor_id,
                        target_actor_id=target_actor_id,
                        observer_actor_id=observer_actor_id,
                        healing=True,
                    )
                )
            marker_offset += 1
    return tuple(found)


def decode_v576_payload(
    payload: bytes,
    direction: str | None = None,
    known_skill_ids: Iterable[int] | None = None,
    known_action_skill_ids: Iterable[int] | None = None,
    known_action_actor_ids: Iterable[int] | None = None,
    packet_features: int | None = None,
) -> DecodedV576Message | None:
    payload_length = len(payload)
    unresolved_action_direction = (
        direction is None and has_ambiguous_action_direction(payload)
    )
    if (
        payload_length == 114
        and direction in (None, "client_to_server")
        and payload[5:9] == b"\xf7\xa6\x6f\x00"
    ):
        decoded = _decode_v576_body(payload, 11)
        raw_name = decoded[14:73].split(b"\x00", 1)[0]
        try:
            source_name = raw_name.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        if not source_name:
            return None
        return DecodedV576Message(
            kind="player_identity",
            payload_length=payload_length,
            layout="c2s-udp-player-identity-114-v576",
            source_name=source_name,
        )

    # Some skills use a compact input/action record instead of a normal cast.
    # Most records carry the active skill at offset 14. Double Somersault is
    # the exception: its follow-up action code identifies the derived skill
    # even when that field still contains the predecessor/context skill.
    if (
        payload_length == 46
        and direction in (None, "client_to_server")
        and payload[5:9] == b"\xf5\x37\x2b\x00"
    ):
        decoded = _decode_v576_body(payload, 0)
        source_actor_id = int.from_bytes(decoded[10:14], "little")
        if decoded[18:21] == b"\x0f\x4b\x01":
            skill_id = 1411
            if (
                known_action_skill_ids is not None
                and skill_id not in known_action_skill_ids
            ):
                return None
            return DecodedV576Message(
                kind="skill_cast",
                payload_length=payload_length,
                layout="c2s-udp-combo-input-46-v576",
                skill_id=skill_id,
                source_actor_id=source_actor_id,
            )
        skill_id = int.from_bytes(decoded[14:18], "little")
        known_actor = (
            known_action_actor_ids is None
            or source_actor_id in known_action_actor_ids
        )
        if (
            source_actor_id
            and known_actor
            and known_action_skill_ids is not None
            and skill_id in known_action_skill_ids
        ):
            return DecodedV576Message(
                kind="skill_cast",
                payload_length=payload_length,
                layout="c2s-udp-skill-input-46-v576",
                skill_id=skill_id,
                source_actor_id=source_actor_id,
            )

    # Stance/channel inputs such as Surround use a wider client action record.
    # The server only echoes part of these actions, so the typed client record
    # can prove a cast independently of server parameter/status envelopes.
    if (
        payload_length == 74
        and direction in (None, "client_to_server")
        and payload[5:9] == b"\xce\x6c\x47\x00"
    ):
        decoded = _decode_v576_body(payload, 0)
        if decoded[40:43] == b"\x0e\x1f\x00":
            skill_id = int.from_bytes(decoded[43:47], "little")
            if (
                known_action_skill_ids is None
                or skill_id in known_action_skill_ids
            ):
                return DecodedV576Message(
                    kind="skill_cast",
                    payload_length=payload_length,
                    layout="c2s-udp-cast-74-v576",
                    skill_id=skill_id,
                    source_actor_id=int.from_bytes(decoded[10:14], "little"),
                )

    cast_layouts = {
        45: (b"\x87\x06\x2a\x00", 6, 14, "c2s-udp-cast-45-v576"),
        # EX/quick-combo casts carry the base skill at offset 21 and the
        # enhancement skill later in the same record.  The base action is the
        # timeline owner (for example Soul Gate 5216 + Soul Gate EX 5405).
        63: (b"\x7c\xde\x3c\x00", 4, 21, "c2s-udp-combo-cast-63-v576"),
        64: (b"\x18\xba\x3d\x00", 0, 21, "c2s-udp-cast-64-v576"),
        72: (b"\x97\x35\x45\x00", 3, 21, "c2s-udp-cast-72-v576"),
        73: (b"\xfe\x5c\x46\x00", 4, 21, "c2s-udp-cast-73-v576"),
        80: (b"\x28\x8a\x4d\x00", 10, 21, "c2s-udp-cast-80-v576"),
        84: (b"\xe5\xa7\x51\x00", 3, 21, "c2s-udp-cast-84-v576"),
        91: (b"\xa7\xe5\x58\x00", 0, 21, "c2s-udp-cast-91-v576"),
        97: (b"\xe5\x47\x5e\x00", 3, 21, "c2s-udp-cast-97-v576"),
        170: (b"\xd0\x72\xa7\x00", 11, 21, "c2s-udp-cast-170-v576"),
    }
    cast_layout = cast_layouts.get(payload_length)
    if (
        cast_layout is not None
        and not unresolved_action_direction
        and direction in (None, "client_to_server")
        and payload[5:9] == cast_layout[0]
    ):
        _, xor_phase, skill_offset, layout = cast_layout
        decoded = _decode_v576_body(payload, xor_phase)
        required_action_markers = {
            64: {b"\x12\x20\x00"},
            72: {b"\x0e\x1f\x00"},
            # These envelopes also contain non-action 02 0f / 03 25 records.
            # A coincidental catalogued integer must not establish a cast/job.
            73: {b"\x0e\x1f\x00", b"\x12\x20\x00"},
            80: {b"\x0e\x1f\x00"},
            84: {b"\x0e\x1f\x00"},
            91: {b"\x0e\x1f\x00"},
            97: {b"\x12\x20\x00"},
            170: {b"\x0e\x1f\x00"},
        }
        required_markers = required_action_markers.get(payload_length)
        if required_markers is None or decoded[18:21] in required_markers:
            skill_id = int.from_bytes(
                decoded[skill_offset : skill_offset + 4], "little"
            )
            if (
                known_action_skill_ids is None
                or skill_id in known_action_skill_ids
            ):
                return DecodedV576Message(
                    kind="skill_cast",
                    payload_length=payload_length,
                    layout=layout,
                    skill_id=skill_id,
                    source_actor_id=int.from_bytes(decoded[10:14], "little"),
                )
        # This signature can carry a shifted combo/action record. Continue to
        # the structural decoders instead of discarding the whole message.

    action_layouts = {
        # Compact server action echoes carry a verified actor/action marker.
        # Do NOT infer casts from 211/214-byte envelopes at offset 64: those
        # also carry status records (13 xx / 1c 1b). The header actor and an
        # embedded effect's skill may belong to DIFFERENT players. Combining
        # a Mercenary header with Crusader's Magic Breath locked the wrong job.
        # Genuine nested actions still go through the structural decoders.
        56: (b"\x2e\x2c\x35\x00", 4, 14, 21, "s2c-udp-action-56-v576"),
        # Chained/derived actions such as Double Somersault use a one-byte
        # longer compact record.
        57: (b"\x92\x90\x36\x00", 8, 14, 21, "s2c-udp-action-57-v576"),
        # These signatures are also used by client cast records.  Direction
        # disambiguates the server echo, whose actor field is at offset 14.
        73: (b"\xfe\x5c\x46\x00", 4, 14, 21, "s2c-udp-action-73-v576"),
        80: (b"\x28\x8a\x4d\x00", 10, 14, 21, "s2c-udp-action-80-v576"),
    }
    action_layout = action_layouts.get(payload_length)
    if (
        action_layout is not None
        and not unresolved_action_direction
        and direction in (None, "server_to_client")
        and payload[5:9] == action_layout[0]
    ):
        _, xor_phase, actor_offset, skill_offset, layout = action_layout
        decoded = _decode_v576_body(payload, xor_phase)
        compact_action_markers = {
            56: {b"\x0e\x23\x00"},
            57: {b"\x12\x24\x00"},
            73: {b"\x0e\x1f\x00", b"\x12\x20\x00"},
            80: {b"\x0e\x1f\x00"},
        }
        allowed_markers = compact_action_markers.get(payload_length)
        marker_is_valid = (
            allowed_markers is not None and decoded[18:21] in allowed_markers
        )
        if marker_is_valid:
            skill_id = int.from_bytes(
                decoded[skill_offset : skill_offset + 4], "little"
            )
            skill_is_valid = (
                known_action_skill_ids is None
                or skill_id in known_action_skill_ids
            )
            if skill_is_valid:
                return DecodedV576Message(
                    kind="skill_action",
                    payload_length=payload_length,
                    layout=layout,
                    skill_id=skill_id,
                    source_actor_id=int.from_bytes(
                        decoded[actor_offset : actor_offset + 4], "little"
                    ),
                    observer_actor_id=int.from_bytes(decoded[10:14], "little"),
                )
        # Crowded-fight messages can reuse a compact packet signature while
        # moving the action record deeper into the body. Fall through so the
        # structural embedded-action decoder can recover that shifted record.

    projectile = decode_v576_projectile_damage(payload, direction, known_skill_ids)
    if projectile is not None:
        return projectile

    # Structural decoding must run before fixed-length decoding. Several v576
    # effect-bearing records reuse a fixed packet length/signature but shift the
    # source and damage fields (notably multi-relic Shock of Relic hits).
    if known_skill_ids is not None and (
        packet_features is None
        or packet_features & V576_PACKET_COMPOUND
    ):
        compound = decode_v576_compound_damage(payload, direction, known_skill_ids)
        if compound is not None:
            return compound

    # A server message may carry a damage record and one or more HP updates.
    # Damage is the primary event here; feed_events independently emits every
    # healing delta from the same packet, so checking healing after compound
    # decoding preserves both sides instead of dropping the hit.
    healing_events = (
        decode_v576_healing(payload, direction)
        if packet_features is None
        or packet_features & V576_PACKET_HEALING
        else ()
    )
    if healing_events:
        return healing_events[0]

    # Server hit packets carry the local observer/session ID separately from the
    # actor that caused the damage. Some continuation layouts omit the skill ID;
    # the replay source links those hits to the actor's latest action message.
    hit_layouts = {
        136: (
            b"\xe5\xe7\x85\x00",
            3,
            26,
            37,
            132,
            53,
            "s2c-udp-hit-136-v576",
        ),
        140: (
            b"\x65\x67\x89\x00",
            1,
            26,
            41,
            124,
            57,
            "s2c-udp-hit-140-v576",
        ),
        152: (
            b"\xce\x6c\x95\x00",
            0,
            26,
            41,
            124,
            57,
            "s2c-udp-hit-152-v576",
        ),
        160: (
            b"\x2e\x6c\x9d\x00",
            4,
            26,
            37,
            None,
            53,
            "s2c-udp-periodic-hit-160-v576",
        ),
        148: (
            b"\x92\x30\x91\x00",
            8,
            14,
            49,
            144,
            None,
            "s2c-udp-hit-148-v576",
        ),
        164: (
            b"\xfe\xbc\xa1\x00",
            4,
            14,
            53,
            136,
            69,
            "s2c-udp-hit-164-v576",
        ),
        218: (
            b"\x65\xc7\xd7\x00",
            1,
            104,
            119,
            202,
            135,
            "s2c-udp-hit-218-v576",
        ),
        219: (
            b"\x7c\xde\xd8\x00",
            4,
            26,
            37,
            152,
            53,
            "s2c-udp-hit-effect-219-v576",
        ),
        229: (
            b"\xfe\x5c\xe2\x00",
            4,
            26,
            41,
            124,
            57,
            "s2c-udp-hit-229-v576",
        ),
    }
    hit_layout = hit_layouts.get(payload_length)
    if (
        hit_layout is not None
        and direction in (None, "server_to_client")
        and payload[5:9] == hit_layout[0]
    ):
        (
            _,
            xor_phase,
            source_offset,
            damage_offset,
            skill_offset,
            critical_offset,
            layout,
        ) = hit_layout
        decoded = _decode_v576_body(payload, xor_phase)
        # The audited 148-byte fallback can resurrect a rejected compound
        # marker by reading that same damage slot. Do not apply this rule to
        # unrelated fixed/status formats whose own record semantics differ.
        if payload_length == 148:
            for declared_length, relative_damage_offset in (
                (0x73, 19),
                (0x75, 19),
                (0x77, 23),
                (0x79, 23),
            ):
                marker_offset = damage_offset - relative_damage_offset
                if (marker_offset >= 4
                        and decoded[marker_offset:marker_offset + 3]
                        == bytes((8, declared_length, 0))
                        and not _v576_direct_marker_allowed(
                            _v576_complete_direct_record_markers(payload),
                            xor_phase, marker_offset, declared_length,
                        )):
                    return None
        if payload_length == 164:
            # The 164-byte signature is also used by actor-state snapshots.
            # Real hit records observed in v576 carry record kind 0x0515 or
            # 0x0526 at offset 18; accepting every packet with this signature
            # can turn coordinates/state values into enormous fake damage.
            record_kind = int.from_bytes(decoded[18:20], "little")
            if record_kind not in (0x0515, 0x0526):
                return None
        skill_id = (
            int.from_bytes(decoded[skill_offset : skill_offset + 4], "little")
            if skill_offset is not None
            else None
        )
        if skill_id is not None and (
            skill_id <= 0
            or skill_id == 0xFFFFFFFF
            or (
                known_skill_ids is not None
                and skill_id not in known_skill_ids
            )
        ):
            skill_id = None
        if payload_length == 152 and skill_id is None:
            # This signature is also emitted by a 152-byte actor-state
            # snapshot. Its coordinate/state field looks like one enormous
            # hit, but unlike a real hit it carries no catalogued skill ID.
            return None
        source_actor_id = int.from_bytes(
            decoded[source_offset : source_offset + 4], "little"
        )
        target_actor_id = int.from_bytes(decoded[14:18], "little")
        damage = _decode_v576_fixed_hit_damage(
            decoded,
            damage_offset,
            payload_length,
        )
        if (
            source_actor_id == 0
            or target_actor_id == 0
            or not _is_valid_v576_damage_amount(damage)
        ):
            return None
        return DecodedV576Message(
            kind="damage_hit",
            payload_length=payload_length,
            layout=layout,
            skill_id=skill_id,
            damage=damage,
            critical=(bool(decoded[critical_offset] & 0x01)
                      if critical_offset is not None else None),
            source_actor_id=source_actor_id,
            target_actor_id=target_actor_id,
            observer_actor_id=int.from_bytes(decoded[10:14], "little"),
        )
    periodic_status_damage = (
        decode_v576_periodic_status_damage(payload, direction)
        if packet_features is None
        or packet_features & V576_PACKET_STATUS_DAMAGE
        else None
    )
    if periodic_status_damage is not None:
        return periodic_status_damage
    if unresolved_action_direction:
        # Damage/status consumers retain the packet. Only a cast with two
        # different possible actor offsets is withheld until direction is proven.
        return None
    embedded_action = (
        _decode_v576_embedded_action(
            payload,
            direction,
            known_action_skill_ids,
        )
        if packet_features is None
        or packet_features & V576_PACKET_ACTION
        else None
    )
    if embedded_action is not None:
        return embedded_action
    embedded_client_action = (
        _decode_v576_embedded_client_action(
            payload,
            direction,
            known_action_skill_ids,
            known_action_actor_ids,
        )
        if packet_features is None
        or packet_features & V576_PACKET_ACTION
        else None
    )
    if embedded_client_action is not None:
        return embedded_client_action
    return None


def _decode_v576_embedded_action(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int] | None,
) -> DecodedV576Message | None:
    """Decode action records nested in variable-length server messages.

    v576 does not always emit the standalone 56/57/73/80-byte action echo.
    Combo, movement and crowded-fight messages can carry the same actor/action
    record at a shifted offset.  The record is structurally stable: actor ID,
    a three-byte action mode, then skill ID.  Only confirmed action modes and
    catalogued player skills are accepted so status and damage records are not
    mistaken for casts.
    """
    if direction not in (None, "server_to_client") or known_skill_ids is None:
        return None
    known = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(known_skill_ids)
    )
    action_markers = (
        b"\x0e\x23\x00",
        b"\x0e\x1f\x00",
        b"\x12\x24\x00",
        b"\x12\x20\x00",
    )

    if (
        not known
        or len(payload) < 25
        or not _v576_wire_may_contain_marker(
            payload, action_markers, start_offset=14
        )
    ):
        return None
    candidates: set[tuple[int, int, int]] = set()
    signature = payload[5:9]
    phase_key = (direction or "", signature)
    cached_phase = _V576_EMBEDDED_ACTION_PHASE_BY_SIGNATURE.get(phase_key)
    signature_phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
    preferred_phase = cached_phase if cached_phase is not None else signature_phase
    phases = (
        (preferred_phase,)
        if preferred_phase is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )
    for xor_phase in phases:
        decoded = _decode_v576_body(payload, xor_phase)
        for marker in action_markers:
            marker_offset = decoded.find(marker, 14)
            while marker_offset >= 4:
                actor_offset = marker_offset - 4
                skill_offset = marker_offset + 3
                skill_id = int.from_bytes(
                    decoded[skill_offset : skill_offset + 4], "little"
                )
                if skill_id in known:
                    source_actor_id = int.from_bytes(
                        decoded[actor_offset : actor_offset + 4], "little"
                    )
                    if source_actor_id:
                        candidates.add((source_actor_id, skill_id, xor_phase))
                marker_offset = decoded.find(marker, marker_offset + 1)
    # A cached signature phase is only a speed hint. A few variable-length
    # action messages reuse a signature with another phase; retry every phase
    # before declaring that the action is absent.
    if not candidates and preferred_phase is not None:
        for xor_phase in range(len(_V576_BODY_XOR_KEY)):
            if xor_phase == preferred_phase:
                continue
            decoded = _decode_v576_body(payload, xor_phase)
            for marker in action_markers:
                marker_offset = decoded.find(marker, 14)
                while marker_offset >= 4:
                    actor_offset = marker_offset - 4
                    skill_offset = marker_offset + 3
                    skill_id = int.from_bytes(
                        decoded[skill_offset : skill_offset + 4], "little"
                    )
                    if skill_id in known:
                        source_actor_id = int.from_bytes(
                            decoded[actor_offset : actor_offset + 4], "little"
                        )
                        if source_actor_id:
                            candidates.add((source_actor_id, skill_id, xor_phase))
                    marker_offset = decoded.find(marker, marker_offset + 1)

    identities = {(actor_id, skill_id) for actor_id, skill_id, _ in candidates}
    if len(identities) != 1:
        return None
    source_actor_id, skill_id = identities.pop()
    candidate_phases = {
        phase
        for actor_id, candidate_skill_id, phase in candidates
        if actor_id == source_actor_id and candidate_skill_id == skill_id
    }
    if len(candidate_phases) == 1:
        _V576_EMBEDDED_ACTION_PHASE_BY_SIGNATURE[phase_key] = next(
            iter(candidate_phases)
        )
    return DecodedV576Message(
        kind="skill_action",
        payload_length=len(payload),
        layout="s2c-udp-action-embedded-v576",
        skill_id=skill_id,
        source_actor_id=source_actor_id,
    )


def _decode_v576_embedded_client_action(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int] | None,
    known_actor_ids: Iterable[int] | None,
) -> DecodedV576Message | None:
    """Recover structurally valid client actions from new packet lengths.

    Client combo, movement and buff actions do not all use the fixed cast
    layouts above.  Once an actor has been independently observed, the stable
    actor/action-mode/skill fields are sufficient to recover these variants.
    Requiring one known actor and one unambiguous catalogued skill keeps state
    and input packets from becoming false timeline casts.
    """
    if (
        direction not in (None, "client_to_server")
        or known_skill_ids is None
        or known_actor_ids is None
        or len(payload) < 25
    ):
        return None
    known_skills = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(known_skill_ids)
    )
    known_actors = (
        known_actor_ids
        if isinstance(known_actor_ids, frozenset)
        else frozenset(known_actor_ids)
    )
    action_markers = (
        b"\x0e\x23\x00",
        b"\x0e\x1f\x00",
        b"\x12\x24\x00",
        b"\x12\x20\x00",
    )

    if (
        not known_skills
        or not known_actors
        or not _v576_wire_may_contain_marker(
            payload, action_markers, start_offset=14
        )
    ):
        return None
    candidates: set[tuple[int, int]] = set()
    for xor_phase in range(len(_V576_BODY_XOR_KEY)):
        decoded = _decode_v576_body(payload, xor_phase)
        source_actor_id = int.from_bytes(decoded[10:14], "little")
        if source_actor_id not in known_actors:
            continue
        for marker in action_markers:
            marker_offset = decoded.find(marker, 14)
            while marker_offset >= 0 and marker_offset + 7 <= len(decoded):
                skill_id = int.from_bytes(
                    decoded[marker_offset + 3 : marker_offset + 7], "little"
                )
                if skill_id in known_skills:
                    candidates.add((source_actor_id, skill_id))
                marker_offset = decoded.find(marker, marker_offset + 1)

    if len(candidates) != 1:
        return None
    source_actor_id, skill_id = candidates.pop()
    return DecodedV576Message(
        kind="skill_cast",
        payload_length=len(payload),
        layout="c2s-udp-action-embedded-v576",
        skill_id=skill_id,
        source_actor_id=source_actor_id,
    )


def decode_v576_structured_actions(
    payload: bytes,
    direction: str | None,
    known_skill_ids: Iterable[int] | None,
    known_actor_ids: Iterable[int] | None,
    *,
    allow_unknown_actor_ids: bool = False,
) -> tuple[DecodedV576Message, ...]:
    """Return every independently confirmed action record in one message.

    The legacy decoder intentionally returns one primary message. Some server
    messages contain several actor/action records, so the structured parser is
    additive: it extracts all valid records while damage/status decoding keeps
    using the unchanged primary message.
    """
    if (
        direction not in {"client_to_server", "server_to_client"}
        or known_skill_ids is None
        or (known_actor_ids is None and not allow_unknown_actor_ids)
        or len(payload) < 25
    ):
        return ()
    known_skills = (
        known_skill_ids
        if isinstance(known_skill_ids, frozenset)
        else frozenset(known_skill_ids)
    )
    known_actors = (
        known_actor_ids
        if isinstance(known_actor_ids, frozenset)
        else frozenset(known_actor_ids or ())
    )
    action_markers = (
        b"\x0e\x23\x00",
        b"\x0e\x1f\x00",
        b"\x12\x24\x00",
        b"\x12\x20\x00",
    )

    def _client_record_actor(
        client_actor_id: int,
        marker_actor_id: int,
    ) -> int:
        """Choose a C2S actor without promoting an accelerator context token.

        The ordinary structured form stores the actor in the fixed header and
        has no plausible actor immediately before the action marker.  A second
        form seen through accelerator relays stores a context token in that
        header slot and the real actor beside the record.  When both values
        look like runtime actor IDs but disagree, an unknown pair is
        deliberately left unresolved until another packet confirms one of
        them.  Structured actions are supplemental, so rejecting that
        ambiguous source is safer than poisoning the persistent local-player
        identity used by the Buff overlay.
        """

        if marker_actor_id in known_actors:
            return marker_actor_id
        if client_actor_id in known_actors:
            return client_actor_id
        marker_plausible = marker_actor_id >= 0x01000000
        client_plausible = client_actor_id >= 0x01000000
        if (
            marker_plausible
            and client_plausible
            and marker_actor_id != client_actor_id
        ):
            return 0
        if marker_plausible:
            return marker_actor_id
        return client_actor_id

    if (
        not known_skills
        or (not known_actors and not allow_unknown_actor_ids)
        or not _v576_wire_may_contain_marker(
            payload, action_markers, start_offset=14
        )
    ):
        return ()

    signature = payload[5:9]
    phase_key = (direction or "", signature)
    cached_phase = _V576_EMBEDDED_ACTION_PHASE_BY_SIGNATURE.get(phase_key)
    signature_phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(signature[0])
    preferred_phase = cached_phase if cached_phase is not None else signature_phase
    phases = (
        (preferred_phase,)
        if preferred_phase is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )
    by_phase: dict[int, list[tuple[int, int, int]]] = {}
    for xor_phase in phases:
        decoded = _decode_v576_body(payload, xor_phase)
        records: set[tuple[int, int, int]] = set()
        client_actor_id = int.from_bytes(decoded[10:14], "little")
        for marker in action_markers:
            marker_offset = decoded.find(marker, 14)
            while marker_offset >= 0 and marker_offset + 7 <= len(decoded):
                skill_id = int.from_bytes(
                    decoded[marker_offset + 3 : marker_offset + 7], "little"
                )
                marker_actor_id = (
                    int.from_bytes(
                        decoded[marker_offset - 4 : marker_offset], "little"
                    )
                    if marker_offset >= 4
                    else 0
                )
                if direction == "server_to_client":
                    source_actor_id = marker_actor_id
                else:
                    # Most client action packets carry the local actor at the
                    # fixed header offset.  A second, variable-length form
                    # carries an action/context token there and places the real
                    # actor directly before each action marker.  Prefer that
                    # record-local value only when it is independently known;
                    # otherwise a context token can pollute ``local_actor_ids``
                    # and make the Buff overlay lose its self target.
                    source_actor_id = _client_record_actor(
                        client_actor_id,
                        marker_actor_id,
                    )
                actor_confirmed = source_actor_id in known_actors
                actor_discoverable = (
                    allow_unknown_actor_ids
                    and source_actor_id >= 0x01000000
                )
                if (
                    source_actor_id
                    and skill_id in known_skills
                    and (actor_confirmed or actor_discoverable)
                ):
                    records.add((marker_offset, source_actor_id, skill_id))
                marker_offset = decoded.find(marker, marker_offset + 1)
        if records:
            by_phase[xor_phase] = sorted(records)

    used_phase_fallback = False
    if not by_phase and preferred_phase is not None:
        # Phase caches are an optimisation, not evidence. Recover variable
        # action records whose packet signature is reused with another phase.
        used_phase_fallback = True
        for xor_phase in range(len(_V576_BODY_XOR_KEY)):
            if xor_phase == preferred_phase:
                continue
            decoded = _decode_v576_body(payload, xor_phase)
            records: set[tuple[int, int, int]] = set()
            client_actor_id = int.from_bytes(decoded[10:14], "little")
            for marker in action_markers:
                marker_offset = decoded.find(marker, 14)
                while marker_offset >= 0 and marker_offset + 7 <= len(decoded):
                    skill_id = int.from_bytes(
                        decoded[marker_offset + 3 : marker_offset + 7], "little"
                    )
                    marker_actor_id = (
                        int.from_bytes(
                            decoded[marker_offset - 4 : marker_offset], "little"
                        )
                        if marker_offset >= 4
                        else 0
                    )
                    source_actor_id = (
                        marker_actor_id
                        if direction == "server_to_client"
                        else _client_record_actor(
                            client_actor_id,
                            marker_actor_id,
                        )
                    )
                    actor_confirmed = source_actor_id in known_actors
                    actor_discoverable = (
                        allow_unknown_actor_ids
                        and source_actor_id >= 0x01000000
                    )
                    if (
                        source_actor_id
                        and skill_id in known_skills
                        and (actor_confirmed or actor_discoverable)
                    ):
                        records.add((marker_offset, source_actor_id, skill_id))
                    marker_offset = decoded.find(marker, marker_offset + 1)
            if records:
                by_phase[xor_phase] = sorted(records)
        if not by_phase:
            return ()
    if (preferred_phase is None or used_phase_fallback) and len(by_phase) != 1:
        # An unknown signature producing plausible records under more than one
        # XOR phase is ambiguous and remains on the legacy/fallback path. The
        # same rule applies when a cached/signature phase failed and the
        # decoder had to probe every phase.
        return ()
    selected_phase = preferred_phase if preferred_phase in by_phase else next(iter(by_phase))
    _V576_EMBEDDED_ACTION_PHASE_BY_SIGNATURE[phase_key] = selected_phase
    layout = (
        "s2c-udp-action-structured-multi-v576"
        if direction == "server_to_client"
        else "c2s-udp-action-structured-multi-v576"
    )
    result: list[DecodedV576Message] = []
    seen: set[tuple[int, int]] = set()
    for _offset, source_actor_id, skill_id in by_phase[selected_phase]:
        identity = (source_actor_id, skill_id)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            DecodedV576Message(
                kind=("skill_action" if direction == "server_to_client" else "skill_cast"),
                payload_length=len(payload),
                layout=layout,
                skill_id=skill_id,
                source_actor_id=source_actor_id,
            )
        )
    return tuple(result)
