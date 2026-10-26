from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable


_MASK32 = 0xFFFFFFFF
_XTEA_DELTA = 0x9E3779B9
_PROFILE_RECORD_SIZE = 171
_PROFILE_RECORD_OFFSET = 19
_COMPACT_PROFILE_RECORD_OFFSET = 14
_CURRENT_COMPACT_PROFILE_RECORD_OFFSET = 58
_PROFILE_REGISTRY_SCHEMA = 2
_PROFILE_PROTOCOL_VERSION = "v576"
_SERVER_FLOW_HINT_LIMIT = 2048

# Keep a code fallback so source and frozen builds still decode known samples if
# the optional data file is missing. The JSON registry is the authoritative,
# extensible copy and the per-user cache stores structurally proven discoveries.
_V576_PROFILE_KEYS_FALLBACK: dict[int, tuple[int, int, int, int]] = {
    8: (0x136C8DDC, 0x00000000, 0x00000000, 0x49FD9981),
    11: (0xF8C04F4F, 0x78C5447D, 0xA3093680, 0xDCB283B1),
    12: (0x4AEC318C, 0x00000000, 0x00000000, 0x3C199BB9),
    29: (0x622C5FB4, 0x1F3D09E3, 0x09765819, 0x326EB5ED),
    33: (0x3BC7B2D6, 0x4D68CE27, 0x7748B328, 0x6AC92EDC),
    35: (0x1D02CA19, 0x3FA6BD2F, 0x407F5456, 0x33E8A7C9),
    36: (0x61470C9F, 0x00000000, 0x00000000, 0x1243943A),
    37: (0x1F793EA7, 0x21307BDE, 0x6CCB32BA, 0x091F4F2A),
    39: (0x199A2DC3, 0x5882C933, 0x1BE5F4C6, 0x5A155328),
    41: (0x77D5F924, 0x0A1EEA90, 0x39720E97, 0x1DE53332),
    45: (0x4A96445C, 0x53326261, 0x631DABB1, 0x6DE5C807),
    63: (0x1EC768CC, 0x0F4CEBE5, 0x095363E9, 0x6997CB1B),
    64: (0x43946933, 0x00000000, 0x00000000, 0x5CBE8FD3),
    76: (0x544E4A20, 0x00000000, 0x00000000, 0x2360DB73),
    93: (0x303E38C9, 0x242FB265, 0x610140F9, 0x078BDAC8),
    98: (0x1C5DBCAE, 0x00000000, 0x00000000, 0x309F1DBF),
    111: (0x30BF7B9B, 0x47369360, 0x03BFDC5A, 0x17C950F2),
    115: (0x3AE14C4B, 0x3A2BE9BC, 0x4DA966C3, 0x52BCAF32),
    117: (0x19269270, 0x7A6E13ED, 0x36875490, 0x5E695E47),
    118: (0x435FAE06, 0x00000000, 0x00000000, 0x202BF14B),
    119: (0x3FA41809, 0x38935306, 0x7B95E0D8, 0x15CAB61D),
    121: (0x483E899D, 0x1FC643A9, 0x54BCC06E, 0x50EA286E),
    123: (0x485357EF, 0x52AA5EAB, 0x22187476, 0x7A09A502),
    133: (0x5AB419A1, 0x177C1529, 0x678AE764, 0x006BF442),
    199: (0x0B754080, 0x28383489, 0x6C4CB16C, 0x3BB8100F),
    203: (0x3852FB91, 0x109A3EFE, 0x69618B69, 0x5C2DA409),
    204: (0x26C83422, 0x00000000, 0x00000000, 0x53EFE104),
    205: (0x1F43903A, 0x66CEADD7, 0x7CDC005B, 0x697F006E),
    207: (0x11EE360F, 0x1A52D04D, 0x2C2E2607, 0x6F4BC900),
    209: (0x089F7538, 0x4AD6C914, 0x4C0CB61B, 0x5E3905BC),
    211: (0x7CC12767, 0x39ECAF9E, 0x12C191D6, 0x77AE5030),
    213: (0x634C0F21, 0x27D53DBC, 0x77E21FA4, 0x6B1CF85F),
    217: (0x7E227657, 0x121408A4, 0x3F7799D6, 0x7D519C10),
    221: (0x5CDB0225, 0x38F95C34, 0x17D49DEA, 0x4B037A0E),
}

_BUNDLED_KEY_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "profiles"
    / "tcp_profile_keys.json"
)
_BUNDLED_FULL_KEY_TABLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "profiles"
    / "tcp_key_table_full.json"
)
_USER_KEY_CACHE_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "DragonNestBuildSimulator"
    / "tcp_profile_keys_v576.user.json"
)
_KEY_REGISTRY_LOCK = threading.RLock()
_KEY_REGISTRY: dict[int, tuple[int, int, int, int]] | None = None
_KEY_SOURCES: dict[int, str] = {}
_KEY_CANDIDATES: dict[int, list[_TcpKeyCandidate]] = {}
_USER_KEY_INDEXES: set[int] = set()
_USER_KEY_WORDS: set[tuple[int, tuple[int, int, int, int]]] = set()
_RECORDED_UNKNOWN_SHAPES: set[tuple[int, int]] = set()
_SERVER_FLOW_KEY_HINTS: OrderedDict[
    tuple[tuple[object, ...], int], tuple[int, int, int, int]
] = OrderedDict()
_SERVER_ENDPOINT_KEY_SUCCESSES: Counter[
    tuple[tuple[object, ...], int, tuple[int, int, int, int]]
] = Counter()


@dataclass(frozen=True, slots=True)
class _TcpKeyCandidate:
    """One key candidate, optionally scoped to an envelope shape/direction.

    The protocol uses ``body_size % 256`` as its key index.  It is therefore
    not a unique message length, and independent world-server families can
    legitimately use different short-round keys at the same index.  Keeping
    candidates instead of overwriting a dictionary slot is required for both
    modulo collisions and server-connection key variants.
    """

    words: tuple[int, int, int, int]
    source: str
    body_sizes: tuple[int, ...] = ()
    directions: tuple[str, ...] = ()

    def applies_to(self, body_size: int, direction: str) -> bool:
        return (
            (not self.body_sizes or int(body_size) in self.body_sizes)
            and (not self.directions or direction in self.directions)
        )


@dataclass(frozen=True, slots=True)
class TcpProfileRecordProbe:
    """A structurally verified record with deliberately neutral field names."""

    offset: int
    group_index: int
    record_index: int
    type_code: int
    instance_token: str
    field_26: int
    field_27: int = 0
    field_29: int = 0
    field_30: int = 0
    field_49: int = 0
    field_50: int = -1
    field_83: int = 0
    field_122: int = 0
    dynamic_attributes: tuple[tuple[int, float], ...] = ()

    @property
    def template_id(self) -> int:
        return self.type_code

    @property
    def reseal_count(self) -> int:
        """Card reroll count; this is not the card mastery level."""
        return self.field_30

    @property
    def enhance_level(self) -> int:
        return self.field_26

    @property
    def accessory_potential_index(self) -> int:
        """Selected potential row for accessory records.

        The wire byte packs the one-based potential row into its low six
        bits.  The high two bits are instance flags.  Treating the complete
        byte as a row number made flagged selections such as ``0x5e`` look
        like row 94 even though the client selects row 30.
        """
        return self.field_27 & 0x3F

    @property
    def equipment_potential_index(self) -> int:
        """Selected potential row for armor and weapon records."""
        return self.field_27 & 0x3F

    @property
    def potential_instance_flags(self) -> int:
        """The two non-selector bits packed into the potential byte."""
        return self.field_27 >> 6

    @property
    def accessory_variant(self) -> int:
        return self.field_29

    @property
    def engraving_extra_selector(self) -> tuple[int, int]:
        """Return the two raw bytes used by exact imprint calibrations.

        A generic client renderer can consume ``field_29`` as a potence-table
        index, but that does not prove the pair's engraving semantics.  The
        same values have different verified meanings on different templates,
        so production resolution keeps both bytes and the template ID.
        """
        return self.field_29, self.field_30

    @property
    def suffix_index(self) -> int | None:
        """Compatibility view of the TypeParam3 one-based selector."""
        return self.field_50 if self.field_50 > 0 else None

    @property
    def type_param1_selector(self) -> int:
        return self.field_49

    @property
    def type_param3_selector(self) -> int:
        return self.field_50

    @property
    def core_rune_param_id(self) -> int:
        return self.field_83

    @property
    def core_rune_coefficient_rank(self) -> int:
        return self.field_122


@dataclass(frozen=True, slots=True)
class DecodedTcpProfile:
    plaintext: bytes
    profile_id: int
    group_counts: tuple[int, ...]
    key_index: int
    rounds: int
    zero_ratio: float
    records: tuple[TcpProfileRecordProbe, ...]
    key_source: str = "bundled"


@dataclass(frozen=True, slots=True)
class DecodedTcpGlyphPageSelection:
    """A verified heraldry-page request or authoritative server state.

    The full login archive contains both saved heraldry pages.  It does not
    encode which page is active in the item-record groups themselves; the
    client uses the separate ``03 0A`` request / ``03 1D`` server-state pair.
    """

    plaintext: bytes
    page_index: int
    actor_id: int
    authoritative: bool
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class TcpProfileDecodeDiagnostic:
    decoded: DecodedTcpProfile | None
    status: str
    message: str
    body_size: int
    key_index: int
    rounds: int
    attempted_keys: int = 0


@dataclass(frozen=True, slots=True)
class DecodedTcpCardUpdate:
    """A verified single-card equip or unequip response."""

    plaintext: bytes
    action_code: int
    record_offset: int
    record_index: int
    template_id: int
    instance_token: str
    key_index: int
    rounds: int
    key_source: str

    @property
    def equipped(self) -> bool:
        return self.action_code == 0x39


@dataclass(frozen=True, slots=True)
class DecodedTcpCardMastery:
    """Card-mastery levels from the authoritative S2C ``0D 19`` array."""

    plaintext: bytes
    entries: tuple[tuple[int, int], ...]
    key_index: int
    rounds: int
    key_source: str
    source: str = "server_0d19"


@dataclass(frozen=True, slots=True)
class DecodedTcpLoginIdentity:
    """Authoritative class lineage sent when the selected character logs in."""

    plaintext: bytes
    job_class_codes: tuple[int, ...]
    actor_marker: int
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class DecodedTcpPlayerSpawnIdentity:
    """Authoritative IDs, name and profession from a player spawn row."""

    plaintext: bytes
    profile_id: int
    runtime_actor_id: int
    character_name: str
    job_class_code: int
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class DecodedTcpPartyRosterIdentity:
    """One named party-member row from the S2C ``22 10`` status stream.

    This message carries the character name and the exact second-job code but
    no runtime actor ID.  Consumers must therefore join it conservatively with
    independently observed party actors; the codec intentionally does not
    guess that relationship.
    """

    plaintext: bytes
    character_name: str
    job_class_code: int
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class DecodedTcpItemArchive:
    """A complete login-time item archive outside the compact profile table."""

    plaintext: bytes
    archive_kind: str
    opcode: bytes
    records: tuple[TcpProfileRecordProbe, ...]
    key_index: int
    rounds: int
    key_source: str
    zero_ratio: float


@dataclass(frozen=True, slots=True)
class DecodedTcpInspectRequest:
    """A verified character-inspect request carrying the target profile ID."""

    plaintext: bytes
    opcode: bytes
    profile_id: int
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class DecodedTcpInspectIdentity:
    """The short server acknowledgement for an inspect identity request."""

    plaintext: bytes
    profile_id: int
    identity_flags: int
    profile_marker: int
    key_index: int
    rounds: int
    key_source: str


@dataclass(frozen=True, slots=True)
class DecodedTcpPartyCreateRequest:
    """A verified party-create request carrying its visible default title.

    The request contains the character name only when the unedited title uses
    ``<character>的队伍``. It does not contain a persistent profile ID; the
    server acknowledgement carries a newly allocated party instance ID.
    """

    plaintext: bytes
    party_mode: int
    max_members: int
    title: str
    owner_name: str
    key_index: int
    rounds: int
    key_source: str


def _parse_key_words(value: object) -> tuple[int, int, int, int] | None:
    if isinstance(value, dict):
        value = value.get("words")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    words: list[int] = []
    try:
        for item in value:
            words.append(int(item, 0) if isinstance(item, str) else int(item))
    except (TypeError, ValueError):
        return None
    if any(word < 0 or word > _MASK32 for word in words):
        return None
    return tuple(words)  # type: ignore[return-value]


def _parse_key_candidate(
    value: object,
    *,
    default_source: str,
) -> _TcpKeyCandidate | None:
    words = _parse_key_words(value)
    if words is None:
        return None
    source = default_source
    body_sizes: tuple[int, ...] = ()
    directions: tuple[str, ...] = ()
    if isinstance(value, dict):
        source = str(value.get("source") or default_source)
        raw_body_sizes = value.get("body_sizes", ())
        if isinstance(raw_body_sizes, (list, tuple)):
            body_sizes = tuple(
                sorted(
                    {
                        int(item)
                        for item in raw_body_sizes
                        if isinstance(item, (int, str))
                        and str(item).lstrip("-").isdigit()
                        and int(item) >= 0
                    }
                )
            )
        raw_directions = value.get("directions", ())
        if isinstance(raw_directions, (list, tuple)):
            directions = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in raw_directions
                    if str(item).strip()
                    in {"client_to_server", "server_to_client"}
                )
            )
    return _TcpKeyCandidate(
        words=words,
        source=source,
        body_sizes=body_sizes,
        directions=directions,
    )


def _read_registry_file(
    path: Path,
    *,
    default_source: str = "registry",
) -> dict[int, tuple[_TcpKeyCandidate, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    raw_keys = payload.get("keys", {})
    if not isinstance(raw_keys, dict):
        return {}
    candidates_by_index: dict[int, tuple[_TcpKeyCandidate, ...]] = {}
    for raw_index, raw_key in raw_keys.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if not 0 <= index <= 255:
            continue
        raw_candidates: object = (
            raw_key.get("candidates")
            if isinstance(raw_key, dict) and "candidates" in raw_key
            else (raw_key,)
        )
        if not isinstance(raw_candidates, (list, tuple)):
            continue
        parsed: list[_TcpKeyCandidate] = []
        for raw_candidate in raw_candidates:
            candidate = _parse_key_candidate(
                raw_candidate,
                default_source=default_source,
            )
            if candidate is None:
                continue
            if any(
                existing.words == candidate.words
                and existing.body_sizes == candidate.body_sizes
                and existing.directions == candidate.directions
                for existing in parsed
            ):
                continue
            parsed.append(candidate)
        if parsed:
            candidates_by_index[index] = tuple(parsed)
    return candidates_by_index


def _append_key_candidate(
    candidates: dict[int, list[_TcpKeyCandidate]],
    index: int,
    candidate: _TcpKeyCandidate,
) -> None:
    rows = candidates.setdefault(int(index), [])
    for position, existing in enumerate(rows):
        if existing.words == candidate.words and existing.source == "bundled":
            # The code fallback is deliberately broad. Let the authoritative
            # registry replace its label without narrowing legacy usage.
            rows[position] = _TcpKeyCandidate(
                words=existing.words,
                source=candidate.source,
                body_sizes=existing.body_sizes,
                directions=existing.directions,
            )
            return
        if (
            existing.words == candidate.words
            and existing.body_sizes == candidate.body_sizes
            and existing.directions == candidate.directions
        ):
            # Prefer the explicit JSON/user source over the code fallback.
            if existing.source == "bundled" and candidate.source != "bundled":
                rows[position] = candidate
            return
    rows.append(candidate)


def _key_registry() -> tuple[dict[int, tuple[int, int, int, int]], dict[int, str]]:
    global _KEY_REGISTRY
    with _KEY_REGISTRY_LOCK:
        if _KEY_REGISTRY is not None:
            return _KEY_REGISTRY, _KEY_SOURCES
        candidates: dict[int, list[_TcpKeyCandidate]] = {}
        # The complete 256-row table is extracted from the current v576
        # client and is the authoritative exact-index baseline.  Older
        # capture-calibrated rows remain available only as fallback variants
        # for semantic validators and protocol-transition diagnostics.
        for path, default_source in (
            (_BUNDLED_FULL_KEY_TABLE_PATH, "current_client_full_table"),
            (_BUNDLED_KEY_REGISTRY_PATH, "bundled_registry"),
        ):
            loaded = _read_registry_file(path, default_source=default_source)
            for index, rows in loaded.items():
                for candidate in rows:
                    _append_key_candidate(candidates, index, candidate)
        for index, words in _V576_PROFILE_KEYS_FALLBACK.items():
            _append_key_candidate(
                candidates,
                index,
                _TcpKeyCandidate(words=words, source="bundled"),
            )
        loaded_user = _read_registry_file(
            _USER_KEY_CACHE_PATH,
            default_source="user_cache",
        )
        for index, rows in loaded_user.items():
            for candidate in rows:
                _append_key_candidate(candidates, index, candidate)
        _USER_KEY_INDEXES.update(loaded_user)
        _USER_KEY_WORDS.update(
            (index, candidate.words)
            for index, rows in loaded_user.items()
            for candidate in rows
        )
        keys = {
            index: rows[0].words
            for index, rows in candidates.items()
            if rows
        }
        sources = {
            index: rows[0].source
            for index, rows in candidates.items()
            if rows
        }
        _KEY_REGISTRY = keys
        _KEY_SOURCES.clear()
        _KEY_SOURCES.update(sources)
        _KEY_CANDIDATES.clear()
        _KEY_CANDIDATES.update(candidates)
        return _KEY_REGISTRY, _KEY_SOURCES


def _registered_key_candidates(
    key_index: int,
    *,
    body_size: int,
    direction: str,
    include_compatible_indexes: bool = False,
) -> list[tuple[tuple[int, int, int, int], str]]:
    _key_registry()
    rounds = (int(body_size) % 2) + 1
    ordered_indexes = [int(key_index)]
    if include_compatible_indexes:
        ordered_indexes.extend(
            index
            for index in _KEY_CANDIDATES
            if index != int(key_index) and (index % 2) + 1 == rounds
        )
    output: list[tuple[tuple[int, int, int, int], str]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for index in ordered_indexes:
        applicable = [
            candidate
            for candidate in _KEY_CANDIDATES.get(index, ())
            if candidate.applies_to(int(body_size), direction)
        ]
        # A packet-family calibration constrained by both direction and exact
        # body size is stronger for that envelope than the generic 256-row
        # client baseline.  This matters across lobby/world-server changes:
        # both keys can decrypt to a length-valid header, so first-match alone
        # would select semantically wrong plaintext after a scene transition.
        applicable.sort(
            key=lambda candidate: (
                0 if candidate.body_sizes else 1,
                0 if candidate.directions else 1,
            )
        )
        for candidate in applicable:
            if candidate.words in seen or not candidate.applies_to(
                int(body_size), direction
            ):
                continue
            seen.add(candidate.words)
            source = candidate.source
            if index != int(key_index):
                source = f"{source}:candidate_index_{index}"
            output.append((candidate.words, source))
    return output


def _write_user_registry() -> None:
    _key_registry()
    user_indexes = sorted(_USER_KEY_INDEXES)
    try:
        _USER_KEY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, object] = {}
        if _USER_KEY_CACHE_PATH.exists():
            try:
                loaded = json.loads(_USER_KEY_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        existing.update(
            {
                "schema_version": _PROFILE_REGISTRY_SCHEMA,
                "protocol_version": _PROFILE_PROTOCOL_VERSION,
                "keys": {
                    str(index): {
                        "candidates": [
                            {
                                "words": [
                                    f"0x{word:08X}" for word in candidate.words
                                ],
                                "source": candidate.source,
                                **(
                                    {"body_sizes": list(candidate.body_sizes)}
                                    if candidate.body_sizes
                                    else {}
                                ),
                                **(
                                    {"directions": list(candidate.directions)}
                                    if candidate.directions
                                    else {}
                                ),
                            }
                            for candidate in _KEY_CANDIDATES.get(index, ())
                            if (index, candidate.words) in _USER_KEY_WORDS
                        ]
                    }
                    for index in user_indexes
                    if any(
                        candidate_index == index
                        for candidate_index, _words in _USER_KEY_WORDS
                    )
                },
            }
        )
        temporary = _USER_KEY_CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(_USER_KEY_CACHE_PATH)
    except OSError:
        # A read-only portable folder must not prevent profile decoding.
        return


def register_tcp_profile_key(
    key_index: int,
    key: tuple[int, int, int, int],
    *,
    source: str = "manual_calibration",
    persist: bool = True,
) -> None:
    """Register one structurally proven v576 key without changing the parser."""
    index = int(key_index)
    words = tuple(int(word) & _MASK32 for word in key)
    if not 0 <= index <= 255 or len(words) != 4:
        raise ValueError("invalid TCP profile key")
    with _KEY_REGISTRY_LOCK:
        keys, sources = _key_registry()
        candidate = _TcpKeyCandidate(
            words=words,  # type: ignore[arg-type]
            source=str(source or "manual_calibration"),
        )
        _append_key_candidate(_KEY_CANDIDATES, index, candidate)
        if index not in keys:
            keys[index] = words  # type: ignore[assignment]
            sources[index] = candidate.source
        _USER_KEY_INDEXES.add(index)
        _USER_KEY_WORDS.add((index, words))  # type: ignore[arg-type]
        if persist:
            _write_user_registry()


def _reset_tcp_profile_key_registry_for_tests() -> None:
    global _KEY_REGISTRY
    with _KEY_REGISTRY_LOCK:
        _KEY_REGISTRY = None
        _KEY_SOURCES.clear()
        _KEY_CANDIDATES.clear()
        _USER_KEY_INDEXES.clear()
        _USER_KEY_WORDS.clear()
        _RECORDED_UNKNOWN_SHAPES.clear()
        _SERVER_FLOW_KEY_HINTS.clear()
        _SERVER_ENDPOINT_KEY_SUCCESSES.clear()
        _cached_decrypt_xtea_block.cache_clear()
        _cached_decrypt_tcp_profile_body.cache_clear()


def _xtea_mix(value: int) -> int:
    return ((((value << 4) & _MASK32) ^ (value >> 5)) + value) & _MASK32


def _decrypt_xtea_block(
    block: bytes,
    key: tuple[int, int, int, int],
    rounds: int,
) -> bytes:
    left, right = struct.unpack("<2I", block)
    running_sum = (rounds * _XTEA_DELTA) & _MASK32
    for _ in range(rounds):
        right = (
            right
            - (_xtea_mix(left) ^ ((running_sum + key[(running_sum >> 11) & 3]) & _MASK32))
        ) & _MASK32
        running_sum = (running_sum - _XTEA_DELTA) & _MASK32
        left = (
            left
            - (_xtea_mix(right) ^ ((running_sum + key[running_sum & 3]) & _MASK32))
        ) & _MASK32
    return struct.pack("<2I", left, right)


@lru_cache(maxsize=8192)
def _cached_decrypt_xtea_block(
    block: bytes, key: tuple[int, int, int, int], rounds: int,
) -> bytes:
    # XTEA blocks are independent. Repeated padding/fields and repeated
    # observers need not decrypt the same immutable block again.
    return _decrypt_xtea_block(block, key, rounds)


def _decrypt_tcp_profile_body_uncached(
    body: bytes,
    key: tuple[int, int, int, int],
    rounds: int,
) -> bytes:
    full_size = len(body) - (len(body) % 8)
    output = bytearray(len(body))
    decrypt_block = (
        _cached_decrypt_xtea_block
        if type(body) is bytes and type(key) is tuple and type(rounds) is int
        and all(type(word) is int for word in key)
        else _decrypt_xtea_block
    )
    for offset in range(0, full_size, 8):
        output[offset : offset + 8] = decrypt_block(
            body[offset : offset + 8], key, rounds
        )
    if full_size < len(body):
        key_bytes = struct.pack("<4I", *key)
        for index, value in enumerate(body[full_size:]):
            output[full_size + index] = value ^ key_bytes[index]
    return bytes(output)


@lru_cache(maxsize=128)
def _cached_decrypt_tcp_profile_body(
    body: bytes, key: tuple[int, int, int, int], rounds: int,
) -> bytes:
    return _decrypt_tcp_profile_body_uncached(body, key, rounds)


def decrypt_tcp_profile_body(
    body: bytes,
    key: tuple[int, int, int, int],
    rounds: int,
) -> bytes:
    """Decrypt exactly; cache only immutable, bounded-size cryptographic inputs.

    Neither connection identity nor a selected key index is a sufficient cache
    key: the full ciphertext, actual key words and rounds are always included.
    The caller must still run its own structure/identity validation. Mutable
    buffers/keys retain the uncached API behavior, including subsequent edits.
    """
    if (
        type(body) is bytes and len(body) <= 8192
        and type(key) is tuple and type(rounds) is int
        and all(type(word) is int for word in key)
    ):
        return _cached_decrypt_tcp_profile_body(body, key, rounds)
    return _decrypt_tcp_profile_body_uncached(body, key, rounds)


def _server_connection_features(
    context: object | None,
) -> tuple[tuple[object, ...] | None, tuple[object, ...] | None]:
    """Return exact-flow and server-endpoint keys from a packet-like object."""

    if context is None:
        return None, None
    try:
        source_ip = str(getattr(context, "source_ip"))
        destination_ip = str(getattr(context, "destination_ip"))
        source_port = int(getattr(context, "source_port"))
        destination_port = int(getattr(context, "destination_port"))
    except (AttributeError, TypeError, ValueError):
        return None, None
    generation = int(getattr(context, "tcp_connection_generation", 0) or 0)
    flow = (
        source_ip,
        source_port,
        destination_ip,
        destination_port,
        generation,
    )
    endpoint = (source_ip, source_port)
    return flow, endpoint


def _ordered_server_key_candidates(
    key_index: int,
    body_size: int,
    context: object | None,
) -> list[tuple[tuple[int, int, int, int], str]]:
    candidates = _registered_key_candidates(
        key_index,
        body_size=body_size,
        direction="server_to_client",
        include_compatible_indexes=False,
    )
    flow, endpoint = _server_connection_features(context)
    with _KEY_REGISTRY_LOCK:
        pinned = (
            _SERVER_FLOW_KEY_HINTS.get((flow, int(body_size)))
            if flow is not None
            else None
        )
        endpoint_counts = {
            words: (
                _SERVER_ENDPOINT_KEY_SUCCESSES.get(
                    (endpoint, int(body_size), words), 0
                )
                if endpoint is not None
                else 0
            )
            for words, _source in candidates
        }

    def rank(row: tuple[tuple[int, int, int, int], str]) -> tuple[int, int]:
        words, _source = row
        if pinned == words:
            return (0, 0)
        endpoint_successes = endpoint_counts.get(words, 0)
        return (1, -int(endpoint_successes))

    return sorted(candidates, key=rank)


def _remember_server_key_success(
    context: object | None,
    body_size: int,
    words: tuple[int, int, int, int],
) -> None:
    flow, endpoint = _server_connection_features(context)
    with _KEY_REGISTRY_LOCK:
        if flow is not None:
            hint_key = (flow, int(body_size))
            _SERVER_FLOW_KEY_HINTS[hint_key] = words
            _SERVER_FLOW_KEY_HINTS.move_to_end(hint_key)
            while len(_SERVER_FLOW_KEY_HINTS) > _SERVER_FLOW_HINT_LIMIT:
                _SERVER_FLOW_KEY_HINTS.popitem(last=False)
        if endpoint is not None:
            _SERVER_ENDPOINT_KEY_SUCCESSES[
                (endpoint, int(body_size), words)
            ] += 1


def decode_registered_tcp_envelope(
    payload: bytes,
) -> tuple[bytes, int, int, str] | None:
    """Decrypt a length-framed TCP message with a registered v576 key.

    Unlike :func:`decode_tcp_profile_payload`, this helper only validates the
    common encrypted envelope.  It is used for small inspect/status requests,
    whose plaintext is not a profile-record table.
    """
    if len(payload) < 7 or int.from_bytes(payload[:3], "little") != len(payload):
        return None
    body = payload[3:]
    key_index = len(body) % 256
    rounds = (len(body) % 2) + 1
    candidates = _registered_key_candidates(
        key_index,
        body_size=len(body),
        direction="client_to_server",
        # A complete table exists, so trying keys from same-parity indexes can
        # only create header-length false positives on unrelated TCP traffic.
        include_compatible_indexes=False,
    )

    for key, source in candidates:
        plaintext = decrypt_tcp_profile_body(body, key, rounds)
        if (
            len(plaintext) >= 4
            and int.from_bytes(plaintext[:2], "little") == len(body)
        ):
            return plaintext, key_index, rounds, source
    return None


def decode_registered_server_tcp_envelope(
    payload: bytes,
    *,
    connection_context: object | None = None,
    validator: Callable[[bytes], bool] | None = None,
) -> tuple[bytes, int, int, str] | None:
    """Decrypt a server-originated v576 TCP frame.

    Client messages store their total length in all three header bytes. Server
    messages instead use a two-byte total length followed by a rolling sequence
    byte; the encrypted body still starts at byte three.  Keeping this decoder
    separate prevents the rolling byte from being mistaken for the high byte
    of the message length.
    """

    if len(payload) < 7 or int.from_bytes(payload[:2], "little") != len(payload):
        return None
    body = payload[3:]
    key_index = len(body) % 256
    rounds = (len(body) % 2) + 1
    candidates = _ordered_server_key_candidates(
        key_index,
        len(body),
        connection_context,
    )
    for key, source in candidates:
        plaintext = decrypt_tcp_profile_body(body, key, rounds)
        if (
            len(plaintext) < 4
            or int.from_bytes(plaintext[:2], "little") != len(body)
        ):
            continue
        if validator is not None and not validator(plaintext):
            continue
        _remember_server_key_success(connection_context, len(body), key)
        return plaintext, key_index, rounds, source
    return None


def decode_tcp_glyph_page_request_payload(
    payload: bytes,
) -> DecodedTcpGlyphPageSelection | None:
    """Decode the exact client ``03 0A`` heraldry-page switch request."""

    if len(payload) != 12:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if (
        len(plaintext) != 9
        or plaintext[2:4] != b"\x03\x0A"
        or int.from_bytes(plaintext[4:8], "little") != 0
        or plaintext[8] > 1
    ):
        return None
    return DecodedTcpGlyphPageSelection(
        plaintext=plaintext,
        page_index=int(plaintext[8]),
        actor_id=0,
        authoritative=False,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_glyph_page_state_payload(
    payload: bytes,
    *,
    connection_context: object | None = None,
) -> DecodedTcpGlyphPageSelection | None:
    """Decode the authoritative server ``03 1D`` active heraldry page."""

    if len(payload) != 12:
        return None
    decoded = decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=lambda value: (
            len(value) == 9
            and value[2:4] == b"\x03\x1D"
            and value[8] <= 1
        ),
    )
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    actor_id = int.from_bytes(plaintext[4:8], "little")
    if actor_id <= 0:
        return None
    return DecodedTcpGlyphPageSelection(
        plaintext=plaintext,
        page_index=int(plaintext[8]),
        actor_id=actor_id,
        authoritative=True,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_login_identity_payload(
    payload: bytes,
) -> DecodedTcpLoginIdentity | None:
    """Decode the v576 character-login identity record.

    Opcode ``03 02`` carries the full class path in bytes 56..59.  Confirmed
    samples include ``3,3,17,36`` (Sorceress/Elementalist/Ice Witch) and
    ``6,6,54,56`` (Kali/Screamer/Soul Eater).  This field is more reliable
    than inferring the second job from an equipped skill-plate preset, because
    inactive plate pages can belong to a different specialization.
    """

    # The decrypted login row is exactly 389 bytes, preceded by the three-byte
    # encrypted-message header.  Checking the wire length first is important:
    # this observer runs for every TCP message and previously decrypted every
    # unrelated gameplay message before rejecting it below.
    if len(payload) != 392:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if len(plaintext) != 389 or plaintext[2:4] != b"\x03\x02":
        return None
    raw_codes = tuple(int(value) for value in plaintext[56:60] if value)
    if len(raw_codes) < 2 or any(value > 127 for value in raw_codes):
        return None
    # The first two bytes are the base class repeated by the protocol.  Keep
    # one copy while preserving the first/second-job order that follows.
    job_class_codes = tuple(dict.fromkeys(raw_codes))
    if len(job_class_codes) < 2:
        return None
    return DecodedTcpLoginIdentity(
        plaintext=plaintext,
        job_class_codes=job_class_codes,
        actor_marker=int.from_bytes(plaintext[16:20], "little"),
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_player_spawn_identity_payload(
    payload: bytes,
) -> DecodedTcpPlayerSpawnIdentity | None:
    """Decode the v576 ``03 03`` player-spawn identity record.

    The stable prefix starts with the profile ID, the scene/runtime actor ID
    and a 20-code-unit UTF-16LE character-name field. Byte 76 is the exact
    ``_NeedJobClass`` profession ID. It has been cross-checked in current
    1805-byte records for Berserker (25), Moonlord (24), Gear Master (48) and
    Ice Witch (36), including two independent Berserker characters.

    Later clients append optional appearance/equipment state to this message
    (1757 bytes before the September update, 1805 bytes afterwards), so
    identity extraction must not depend on one exact tail size. The profile ID
    is used by character-inspect requests while combat packets use the runtime
    ID, so retaining both is required to display one name and profession in
    both views before the player is inspected.
    """

    # Server frames use the low two length bytes plus a rolling third byte in
    # some streams.  Older captures happened to carry zero in that byte, so
    # accept either verified envelope representation.  The exact key-index
    # lookup below keeps this prefix-based parser cheap for unrelated messages.
    identity_prefix_size = 52
    # Current 03 03 rows are large spawn records (the September client uses an
    # 1808-byte wire message).  Tiny control/gameplay messages cannot possibly
    # contain the stable prefix plus the profession byte.  Keep the upper
    # bound deliberately generous for later appearance-tail extensions.
    if not 512 <= len(payload) <= 8192:
        return None
    declared_length_3 = int.from_bytes(payload[:3], "little")
    declared_length_2 = int.from_bytes(payload[:2], "little")
    if declared_length_3 != len(payload) and declared_length_2 != len(payload):
        return None
    body = payload[3:]
    key_index = len(body) % 256
    rounds = (len(body) % 2) + 1
    exact_candidates = _registered_key_candidates(
        key_index,
        body_size=len(body),
        direction="server_to_client",
        include_compatible_indexes=False,
    )
    decoded = None
    for exact_key, exact_source in exact_candidates:
        # XTEA blocks are independent.  Validate only the stable identity
        # prefix first; decrypting the whole 1.8KB body for every unrelated
        # server message was the main source of the long-running TCP backlog.
        prefix_size = min(len(body), 80)
        plaintext_prefix = decrypt_tcp_profile_body(
            body[:prefix_size], exact_key, rounds
        )
        if (
            len(plaintext_prefix) >= 77
            and int.from_bytes(plaintext_prefix[:2], "little") == len(body)
            and plaintext_prefix[2:4] == b"\x03\x03"
        ):
            plaintext = decrypt_tcp_profile_body(body, exact_key, rounds)
            decoded = plaintext, key_index, rounds, exact_source
            break
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if (
        len(plaintext) < identity_prefix_size
        or int.from_bytes(plaintext[:2], "little") != len(plaintext)
        or plaintext[2:4] != b"\x03\x03"
    ):
        return None
    profile_id = int.from_bytes(plaintext[4:8], "little")
    runtime_actor_id = int.from_bytes(plaintext[8:12], "little")
    if profile_id <= 0 or runtime_actor_id <= 0:
        return None

    raw_name = plaintext[12:52]
    terminator = len(raw_name)
    for offset in range(0, len(raw_name) - 1, 2):
        if raw_name[offset : offset + 2] == b"\x00\x00":
            terminator = offset
            break
    try:
        character_name = raw_name[:terminator].decode("utf-16le").strip()
    except UnicodeDecodeError:
        return None
    if (
        not character_name
        or len(character_name) > 20
        or any(not character.isprintable() for character in character_name)
    ):
        return None
    # This position is part of the stable 03 03 identity header, not the
    # variable appearance/equipment tail. Unknown/new codes remain available
    # on the decoded row but are ignored by the caller until the job catalogue
    # contains them.
    job_class_code = int(plaintext[76]) if len(plaintext) > 76 else 0
    return DecodedTcpPlayerSpawnIdentity(
        plaintext=plaintext,
        profile_id=profile_id,
        runtime_actor_id=runtime_actor_id,
        character_name=character_name,
        job_class_code=job_class_code,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_party_roster_identity_payload(
    payload: bytes,
    *,
    connection_context: object | None = None,
) -> DecodedTcpPartyRosterIdentity | None:
    """Decode the repeating S2C ``22 10`` party status/name row.

    The encrypted body is exactly 108 bytes.  Bytes 4..7 contain the unpacked
    ``_NeedJobClass`` code and bytes 8..51 are a fixed-width UTF-8 character
    name.  The remaining counters are deliberately ignored here: they are not
    required for identity recovery and their gameplay meaning is still under
    independent audit.
    """

    if len(payload) != 111:
        return None

    def valid(plaintext: bytes) -> bool:
        return len(plaintext) == 108 and plaintext[2:4] == b"\x22\x10"

    decoded = decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=valid,
    )
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    job_class_code = int.from_bytes(plaintext[4:8], "little")
    if not 0 < job_class_code <= 255:
        return None
    raw_name = plaintext[8:52].split(b"\x00", 1)[0]
    try:
        character_name = raw_name.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    if (
        not character_name
        or len(character_name) > 32
        or any(not character.isprintable() for character in character_name)
    ):
        return None
    return DecodedTcpPartyRosterIdentity(
        plaintext=plaintext,
        character_name=character_name,
        job_class_code=job_class_code,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_card_mastery_payload(
    payload: bytes,
    *,
    connection_context: object | None = None,
) -> DecodedTcpCardMastery | None:
    """Decode the server's sparse ``(cardskill ID, level)`` mastery array."""

    # Wire length is 3 bytes plus an 8-byte header and N eight-byte rows.
    # Apply this gate before XTEA so ordinary server traffic stays cheap.
    if (
        len(payload) < 11
        or len(payload) > 11 + 18 * 8
        or (len(payload) - 11) % 8 != 0
        or int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return None

    def valid_plaintext(value: bytes) -> bool:
        if len(value) < 8 or value[2:4] != b"\x0D\x19":
            return False
        count = int.from_bytes(value[4:8], "little")
        return 0 <= count <= 18 and len(value) == 8 + count * 8

    decoded = decode_registered_server_tcp_envelope(
        payload,
        connection_context=connection_context,
        validator=valid_plaintext,
    )
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    count = int.from_bytes(plaintext[4:8], "little")
    entries: list[tuple[int, int]] = []
    previous_id = 0
    for index in range(count):
        offset = 8 + index * 8
        mastery_id = int.from_bytes(plaintext[offset : offset + 4], "little")
        level = int.from_bytes(plaintext[offset + 4 : offset + 8], "little")
        if mastery_id <= previous_id or mastery_id > 4096 or not 1 <= level <= 100:
            return None
        previous_id = mastery_id
        entries.append((mastery_id, level))
    return DecodedTcpCardMastery(
        plaintext=plaintext,
        entries=tuple(entries),
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_inspect_request_payload(
    payload: bytes,
) -> DecodedTcpInspectRequest | None:
    """Decode the three v576 requests emitted while viewing a character.

    The encrypted wire bytes previously exposed only a session-local token.
    The verified plaintext carries the actual target profile ID at offset 4.
    """
    if len(payload) != 11:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if len(plaintext) != 8 or plaintext[2:4] not in {
        b"\x17\x01",
        b"\x03\x15",
        b"\x07\x3d",
    }:
        return None
    profile_id = int.from_bytes(plaintext[4:8], "little")
    if profile_id <= 0:
        return None
    return DecodedTcpInspectRequest(
        plaintext=plaintext,
        opcode=plaintext[2:4],
        profile_id=profile_id,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_party_create_request_payload(
    payload: bytes,
) -> DecodedTcpPartyCreateRequest | None:
    """Decode the v576 request used to create a four/eight-player party."""
    if len(payload) != 79:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if len(plaintext) != 76 or plaintext[2:4] != b"\x06\x01":
        return None
    max_members = int(plaintext[16])
    if max_members not in {4, 8}:
        return None

    title_bytes = plaintext[34:76]
    title_end = len(title_bytes)
    for offset in range(0, len(title_bytes) - 1, 2):
        if title_bytes[offset : offset + 2] == b"\x00\x00":
            title_end = offset
            break
    try:
        title = title_bytes[:title_end].decode("utf-16le").strip()
    except UnicodeDecodeError:
        return None
    if not title:
        return None
    owner_name = title[: -len("的队伍")].strip() if title.endswith("的队伍") else ""
    return DecodedTcpPartyCreateRequest(
        plaintext=plaintext,
        party_mode=int.from_bytes(plaintext[4:8], "little"),
        max_members=max_members,
        title=title,
        owner_name=owner_name,
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_inspect_identity_payload(
    payload: bytes,
) -> DecodedTcpInspectIdentity | None:
    """Decode the fixed 29-byte response paired with opcode ``17 01``."""
    if len(payload) != 32:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if len(plaintext) != 29 or plaintext[2:4] != b"\x17\x00":
        return None
    profile_id = int.from_bytes(plaintext[4:8], "little")
    if profile_id <= 0:
        return None
    return DecodedTcpInspectIdentity(
        plaintext=plaintext,
        profile_id=profile_id,
        identity_flags=int.from_bytes(plaintext[8:12], "little"),
        profile_marker=int.from_bytes(plaintext[21:29], "little"),
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_card_update_payload(payload: bytes) -> DecodedTcpCardUpdate | None:
    """Decode the v576 single-card change response calibrated by personal data 5.

    The card page uses a small independent message instead of the 171-byte
    character-profile table. Equip and unequip responses place the changed
    item record at different verified offsets, so both layouts are accepted.
    """
    if len(payload) != 352:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if (
        len(plaintext) != 349
        or plaintext[2:4] != b"\x07\x31"
        or plaintext[4] not in {0x38, 0x39}
        or plaintext.count(0) / len(plaintext) < 0.80
    ):
        return None
    action_code = plaintext[4]
    record_offset = 9 if action_code == 0x39 else 178
    record_index = int.from_bytes(
        plaintext[record_offset : record_offset + 2], "little"
    )
    template_id = int.from_bytes(
        plaintext[record_offset + 2 : record_offset + 6], "little"
    )
    instance_token = plaintext[record_offset + 6 : record_offset + 22]
    if template_id <= 0 or not any(instance_token):
        return None
    return DecodedTcpCardUpdate(
        plaintext=plaintext,
        action_code=action_code,
        record_offset=record_offset,
        record_index=record_index,
        template_id=template_id,
        instance_token=instance_token.hex().upper(),
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
    )


def decode_tcp_item_archive_payload(payload: bytes) -> DecodedTcpItemArchive | None:
    """Decode complete card and engraving archives observed during login.

    Both layouts use an eight-byte header followed by an explicit count of
    171-byte item records. Validation is deliberately strict so unrelated TCP
    messages cannot be presented as player equipment.
    """
    # 3-byte wire header + 8-byte archive header + N fixed 171-byte records.
    # Reject other server traffic before attempting any XTEA work.
    if (
        len(payload) < 11 + _PROFILE_RECORD_SIZE
        or (len(payload) - 11) % _PROFILE_RECORD_SIZE != 0
        or (len(payload) - 11) // _PROFILE_RECORD_SIZE > 128
    ):
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, key_index, rounds, key_source = decoded
    if len(plaintext) < 8 or int.from_bytes(plaintext[:2], "little") != len(
        plaintext
    ):
        return None
    layout = {
        b"\x07\x7f": ("cards", 4),
        b"\x07\x94": ("engravings", 3),
    }.get(plaintext[2:4])
    if layout is None:
        return None
    archive_kind, group_index = layout
    record_count = int(plaintext[4])
    if not 1 <= record_count <= 128:
        return None
    if len(plaintext) != 8 + record_count * _PROFILE_RECORD_SIZE:
        return None
    zero_ratio = plaintext.count(0) / len(plaintext)
    if zero_ratio < 0.65:
        return None

    records: list[TcpProfileRecordProbe] = []
    previous_index = -1
    offset = 8
    for _ in range(record_count):
        record = plaintext[offset : offset + _PROFILE_RECORD_SIZE]
        record_index = int.from_bytes(record[:2], "little")
        template_id = int.from_bytes(record[2:6], "little")
        instance_token = record[6:22]
        if (
            record_index <= previous_index
            or template_id <= 0
            or not any(instance_token)
        ):
            return None
        previous_index = record_index
        records.append(
            TcpProfileRecordProbe(
                offset=offset,
                group_index=group_index,
                record_index=record_index,
                type_code=template_id,
                instance_token=instance_token.hex().upper(),
                field_26=record[26],
                field_27=record[27],
                field_29=record[29],
                field_30=record[30],
                field_49=record[49],
                field_50=int.from_bytes(record[50:51], "little", signed=True),
                field_83=int.from_bytes(record[83:85], "little"),
                field_122=record[122],
                dynamic_attributes=_record_dynamic_attributes(record),
            )
        )
        offset += _PROFILE_RECORD_SIZE
    return DecodedTcpItemArchive(
        plaintext=plaintext,
        archive_kind=archive_kind,
        opcode=plaintext[2:4],
        records=tuple(records),
        key_index=key_index,
        rounds=rounds,
        key_source=key_source,
        zero_ratio=zero_ratio,
    )


def is_tcp_profile_payload_shape(payload: bytes) -> bool:
    """Return whether a framed message can contain the calibrated profile table.

    Inspected-player responses use a 19-byte header. Login-time self profiles
    use either the legacy 14-byte compact header or the current 58-byte
    compact header. All are followed by fixed 171-byte item records. Checking
    those invariants before key probing keeps unrelated periodic messages out
    of the profile decoder.
    """
    if len(payload) < 3 or (
        int.from_bytes(payload[:3], "little") != len(payload)
        and int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return False
    body_size = len(payload) - 3
    return any(
        body_size >= record_offset + 3 * _PROFILE_RECORD_SIZE
        and (body_size - record_offset) % _PROFILE_RECORD_SIZE == 0
        for record_offset in (
            _PROFILE_RECORD_OFFSET,
            _COMPACT_PROFILE_RECORD_OFFSET,
            _CURRENT_COMPACT_PROFILE_RECORD_OFFSET,
        )
    )


def _trim_trailing_zero_counts(values: bytes | list[int]) -> tuple[int, ...]:
    """Keep empty middle groups so later group numbers never shift left."""

    counts = [int(value) for value in values]
    while counts and counts[-1] == 0:
        counts.pop()
    return tuple(counts)


def _profile_layout(plaintext: bytes) -> tuple[int, tuple[int, ...], int]:
    """Return record offset, group counts and profile ID for known layouts."""
    if len(plaintext) >= _PROFILE_RECORD_OFFSET:
        primary_counts = list(_trim_trailing_zero_counts(plaintext[12:16]))
        if (
            primary_counts
            and primary_counts[0]
            and all(count <= 64 for count in primary_counts)
        ):
            # Before the talisman update, an optional fifth group used byte
            # 18. The current response keeps engraving count at byte 18 and
            # inserts the equipped-talisman count at byte 17. Try only the
            # structurally exact combinations so ordinary header bytes can
            # never be mistaken for record counts.
            extra_17 = int(plaintext[17])
            extra_18 = int(plaintext[18])
            extra_candidates: list[tuple[int, ...]] = [()]
            if 0 < extra_18 <= 64:
                extra_candidates.append((extra_18,))
            if 0 < extra_17 <= 64:
                extra_candidates.append((extra_17,))
                if 0 < extra_18 <= 64:
                    extra_candidates.append((extra_17, extra_18))
            for extras in extra_candidates:
                group_counts = primary_counts + list(extras)
                expected_size = (
                    _PROFILE_RECORD_OFFSET
                    + sum(group_counts) * _PROFILE_RECORD_SIZE
                )
                if expected_size == len(plaintext):
                    return (
                        _PROFILE_RECORD_OFFSET,
                        tuple(group_counts),
                        int.from_bytes(plaintext[4:8], "little"),
                    )

    # Login-time self profiles carry the ordinary group counts at bytes 4..7
    # but no stable profile ID.  The September client extended the compact
    # header from 14 to 58 bytes while keeping the same 171-byte records.
    if (
        len(plaintext) >= _COMPACT_PROFILE_RECORD_OFFSET
        and int.from_bytes(plaintext[:2], "little") == len(plaintext)
        and plaintext[2:4] == b"\x07\x03"
    ):
        group_counts = _trim_trailing_zero_counts(plaintext[4:8])
        if group_counts and group_counts[0] and all(
            count <= 64 for count in group_counts
        ):
            for record_offset in (
                _CURRENT_COMPACT_PROFILE_RECORD_OFFSET,
                _COMPACT_PROFILE_RECORD_OFFSET,
            ):
                if (
                    record_offset + sum(group_counts) * _PROFILE_RECORD_SIZE
                    == len(plaintext)
                ):
                    return record_offset, group_counts, 0

    return 0, (), 0


def _profile_group_counts(plaintext: bytes) -> tuple[int, ...]:
    return _profile_layout(plaintext)[1]


def _record_probes(plaintext: bytes) -> tuple[TcpProfileRecordProbe, ...]:
    record_offset, group_counts, _profile_id = _profile_layout(plaintext)
    if not group_counts:
        return ()
    records: list[TcpProfileRecordProbe] = []
    offset = record_offset
    for group_index, record_count in enumerate(group_counts):
        previous_index = -1
        for _ in range(record_count):
            record = plaintext[offset : offset + _PROFILE_RECORD_SIZE]
            record_index = int.from_bytes(record[:2], "little")
            if record_index <= previous_index:
                return ()
            previous_index = record_index
            records.append(
                TcpProfileRecordProbe(
                    offset=offset,
                    group_index=group_index,
                    record_index=record_index,
                    type_code=int.from_bytes(record[2:6], "little"),
                    instance_token=record[6:22].hex().upper(),
                    field_26=record[26],
                    field_27=record[27],
                    field_29=record[29],
                    field_30=record[30],
                    field_49=record[49],
                    field_50=int.from_bytes(record[50:51], "little", signed=True),
                    field_83=int.from_bytes(record[83:85], "little"),
                    field_122=record[122],
                    dynamic_attributes=_record_dynamic_attributes(record),
                )
            )
            offset += _PROFILE_RECORD_SIZE
    return tuple(records)


def _record_dynamic_attributes(record: bytes) -> tuple[tuple[int, float], ...]:
    """Read the six attribute/value pairs exposed by the v576 info panel."""
    attributes: list[tuple[int, float]] = []
    for index in range(6):
        value = struct.unpack_from("<f", record, 59 + index * 4)[0]
        if not math.isfinite(value) or abs(value) < 1e-7:
            continue
        attributes.append((record[53 + index], value))
    return tuple(attributes)


def _decode_with_key(
    body: bytes,
    key_index: int,
    rounds: int,
    key: tuple[int, int, int, int],
    key_source: str,
) -> DecodedTcpProfile | None:
    plaintext = decrypt_tcp_profile_body(body, key, rounds)
    zero_ratio = plaintext.count(0) / max(1, len(plaintext))
    _record_offset, group_counts, profile_id = _profile_layout(plaintext)
    records = _record_probes(plaintext)
    # Length, record order and sparsity are checked together so a stale key
    # fails closed after a patch instead of displaying plausible-looking junk.
    if zero_ratio < 0.65 or len(records) < 3 or not group_counts:
        return None
    return DecodedTcpProfile(
        plaintext=plaintext,
        profile_id=profile_id,
        group_counts=group_counts,
        key_index=key_index,
        rounds=rounds,
        zero_ratio=zero_ratio,
        records=records,
        key_source=key_source,
    )


def _one_round_zero_block_keys(
    body: bytes,
) -> tuple[tuple[int, int, int, int], ...]:
    """Recover one-round keys from repeated encrypted all-zero blocks.

    Profile records are sparse and contain hundreds of all-zero 8-byte blocks.
    With one XTEA round, encrypting zero exposes key word 0 directly and makes
    word 3 algebraically recoverable. Every candidate is still required to
    pass the complete profile-structure validation before it can be learned.
    """
    blocks = Counter(
        body[offset : offset + 8]
        for offset in range(0, len(body) - (len(body) % 8), 8)
    )
    candidates: list[tuple[int, int, int, int]] = []
    for block, count in blocks.most_common(12):
        if count < 3 or len(block) != 8:
            continue
        left, right = struct.unpack("<2I", block)
        key3 = ((right ^ _xtea_mix(left)) - _XTEA_DELTA) & _MASK32
        candidate = (left, 0, 0, key3)
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _remember_unknown_shape(
    diagnostic: TcpProfileDecodeDiagnostic,
    body: bytes,
    *,
    expected_profile_id: int = 0,
) -> None:
    """Persist privacy-safe recovery evidence for an uncovered key.

    Two-round keys cannot be derived from the repeated zero block alone.  A
    paired inspect request supplies the plaintext profile ID, so retaining the
    encrypted header and the most frequent encrypted block is enough to make
    a later offline recovery deterministic without saving any decrypted item
    or character data in the key cache.
    """
    shape = (diagnostic.body_size, diagnostic.key_index)
    with _KEY_REGISTRY_LOCK:
        if shape in _RECORDED_UNKNOWN_SHAPES:
            return
        _RECORDED_UNKNOWN_SHAPES.add(shape)
        try:
            existing: dict[str, object] = {}
            if _USER_KEY_CACHE_PATH.exists():
                loaded = json.loads(_USER_KEY_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            raw_shapes = existing.get("unknown_shapes", [])
            shapes = list(raw_shapes) if isinstance(raw_shapes, list) else []
            fingerprint = hashlib.sha256(body).hexdigest()[:16].upper()
            recovery_hint: dict[str, object] = {
                "encrypted_header": body[:8].hex().upper(),
            }
            if expected_profile_id > 0:
                recovery_hint["expected_profile_id"] = (
                    f"0x{int(expected_profile_id) & _MASK32:08X}"
                )
            aligned_blocks = Counter(
                body[offset : offset + 8]
                for offset in range(0, len(body) - (len(body) % 8), 8)
            )
            if aligned_blocks:
                repeated_block, repeated_count = aligned_blocks.most_common(1)[0]
                if repeated_count >= 3:
                    recovery_hint.update(
                        {
                            "repeated_block": repeated_block.hex().upper(),
                            "repeated_block_count": repeated_count,
                        }
                    )
            matching_row = next(
                (
                    row
                    for row in shapes
                    if isinstance(row, dict)
                    and row.get("body_size") == diagnostic.body_size
                    and row.get("key_index") == diagnostic.key_index
                    and row.get("fingerprint") == fingerprint
                ),
                None,
            )
            if matching_row is None:
                matching_row = {
                    "body_size": diagnostic.body_size,
                    "key_index": diagnostic.key_index,
                    "rounds": diagnostic.rounds,
                    "fingerprint": fingerprint,
                    "status": diagnostic.status,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                shapes.append(matching_row)
            matching_row.update(recovery_hint)
            existing.update(
                {
                    "schema_version": _PROFILE_REGISTRY_SCHEMA,
                    "protocol_version": _PROFILE_PROTOCOL_VERSION,
                    "unknown_shapes": shapes[-64:],
                }
            )
            _USER_KEY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = _USER_KEY_CACHE_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(_USER_KEY_CACHE_PATH)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return


def diagnose_tcp_profile_payload(
    payload: bytes,
    *,
    record_unknown: bool = False,
    expected_profile_id: int = 0,
) -> TcpProfileDecodeDiagnostic:
    """Decode a profile and explain precisely why an unknown shape failed."""
    if len(payload) < 3 or (
        int.from_bytes(payload[:3], "little") != len(payload)
        and int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return TcpProfileDecodeDiagnostic(
            decoded=None,
            status="invalid_envelope",
            message="响应长度头不完整或与正文不一致",
            body_size=max(0, len(payload) - 3),
            key_index=-1,
            rounds=0,
        )

    body = payload[3:]
    body_size = len(body)
    key_index = body_size % 256
    rounds = (body_size % 2) + 1
    _key_registry()
    candidates = _registered_key_candidates(
        key_index,
        body_size=body_size,
        direction="server_to_client",
        include_compatible_indexes=False,
    )
    exact_words = {
        candidate.words
        for candidate in _KEY_CANDIDATES.get(key_index, ())
        if candidate.applies_to(body_size, "server_to_client")
    }

    if rounds == 1:
        candidates.extend(
            (candidate, "auto_zero_block")
            for candidate in _one_round_zero_block_keys(body)
            if all(candidate != existing for existing, _source in candidates)
        )

    for attempted, (candidate, source) in enumerate(candidates, start=1):
        decoded = _decode_with_key(
            body, key_index, rounds, candidate, source
        )
        if decoded is None:
            continue
        if candidate not in exact_words:
            register_tcp_profile_key(
                key_index,
                candidate,
                source=source,
                persist=True,
            )
        return TcpProfileDecodeDiagnostic(
            decoded=decoded,
            status="decoded",
            message=(
                f"V576 已解密：正文 {body_size} 字节，密钥索引 {key_index}，"
                f"{rounds} 轮，来源 {source}"
            ),
            body_size=body_size,
            key_index=key_index,
            rounds=rounds,
            attempted_keys=attempted,
        )

    status = "key_validation_failed" if exact_words else "uncovered_key"
    message = (
        f"V576 密钥索引 {key_index} 已存在，但档案结构校验失败"
        if exact_words
        else f"V576 密钥索引 {key_index} 尚未覆盖"
    )
    diagnostic = TcpProfileDecodeDiagnostic(
        decoded=None,
        status=status,
        message=(
            f"{message}（正文 {body_size} 字节，{rounds} 轮，"
            f"已验证 {len(candidates)} 个候选）"
        ),
        body_size=body_size,
        key_index=key_index,
        rounds=rounds,
        attempted_keys=len(candidates),
    )
    if record_unknown:
        _remember_unknown_shape(
            diagnostic,
            body,
            expected_profile_id=expected_profile_id,
        )
    return diagnostic


def decode_tcp_profile_payload(payload: bytes) -> DecodedTcpProfile | None:
    """Decode a v576 character-inspect payload with adaptive key selection."""
    return diagnose_tcp_profile_payload(payload).decoded
