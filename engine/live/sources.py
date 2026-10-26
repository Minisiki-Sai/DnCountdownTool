from __future__ import annotations

import json
import os
import re
from bisect import bisect_left, insort
import codecs
import sqlite3
import struct
import hashlib
import queue
import threading
import time
from datetime import datetime
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from .action_parser import action_parser_mode as configured_action_parser_mode
from .skill_plate_damage import SKILL_PLATE_DAMAGE_NAMES
from .boss_action import BossActionEvidence, BossStructuredEventParser
from .debuff_evidence import DebuffEvidenceRegistry, build_debuff_evidence_key
from .effects import (
    boss_effect_duration_ms,
    boss_effect_label,
    boss_effect_rules,
    boss_effect_rules_for_status_packet,
    boss_status_override,
    packet_effect_value_for_rule,
)
from .event_ledger import V576EventLedger
from .healing_attribution import healing_skill_key, load_healing_skill_models
from .inspect_catalog import (
    inspect_record_category,
    inspect_record_detail,
    inspect_record_slot,
    inspect_talisman_slot_multiplier,
    inspect_active_set_details,
    inspect_core_rune_text,
    load_inspect_card_mastery_catalog,
    load_inspect_core_rune_catalog,
    load_inspect_engraving_potence_catalog,
    load_inspect_enchant_catalog,
    load_inspect_glyph_potential_catalog,
    load_inspect_inherent_potential_catalog,
    load_inspect_item_catalog,
    load_inspect_potential_catalog,
    load_inspect_set_catalog,
    load_inspect_suffix_potential_catalog,
)
from .models import (
    BossActionEvent,
    BossSpawnEvent,
    BossHealthEvent,
    MonsterAppearanceEvent,
    BossStatusEvent,
    CombatEvent,
    PartyMemberEvent,
    PlayerHealthEvent,
    PlayerLifecycleEvent,
    PlayerInspectEvent,
    SkillCastEvent,
    SkillParameterEvent,
    SummonRemovalEvent,
)
from .npcap import (
    IDENTITY_METADATA_ADAPTER_INDEX,
    IDENTITY_TRACE_ADAPTER_INDEX,
    PREBATTLE_STATE_ADAPTER_INDEX,
    PROFILE_IDENTITY_ADAPTER_INDEX,
    SESSION_EVIDENCE_ADAPTER_INDEX,
    NpcapError,
    NpcapPacketStream,
    decode_identity_trace_bindings,
    decode_profile_identity_bindings,
    encode_profile_identity_bindings,
    decode_prebattle_state_snapshot,
    set_current_thread_low_priority,
    set_current_thread_normal_priority,
)
from .profile import ClientProfile
from .settlement import SettlementEvent
from .protocol import (
    DecodedV576Message,
    PacketRecord,
    V576TransportReassembler,
    decode_captured_frame,
    decode_v576_boss_actions,
    decode_v576_boss_structured_actions,
    decode_v576_embedded_direct_damage_events,
    decode_v576_status_damage_events,
    decode_v576_healing,
    decode_v576_map_identity,
    decode_v576_monster_spawn,
    decode_v576_payload,
    decode_v576_player_health,
    decode_v576_player_lifecycle,
    decode_v576_structured_actions,
    decode_v576_status_events,
    scan_v576_packet_features,
    V576_PACKET_PRIMARY,
    V576_PACKET_ACTION,
    V576_PACKET_STATUS,
    V576_PACKET_HEALTH,
    V576_PACKET_LIFECYCLE,
    V576_PACKET_BOSS_ACTION,
    V576_PACKET_STATUS_DAMAGE,
    V576_PACKET_HEALING,
    V576_PACKET_EMBEDDED_DIRECT_DAMAGE,
    V576_PACKET_COMPOUND,
    read_pcapng_packets,
    split_v576_payload,
)
from .tcp_profile_codec import (
    DecodedTcpPartyRosterIdentity,
    DecodedTcpPlayerSpawnIdentity,
    decode_registered_tcp_envelope,
    decode_tcp_card_mastery_payload,
    decode_tcp_card_update_payload,
    decode_tcp_glyph_page_request_payload,
    decode_tcp_glyph_page_state_payload,
    decode_tcp_inspect_identity_payload,
    decode_tcp_inspect_request_payload,
    decode_tcp_item_archive_payload,
    decode_tcp_login_identity_payload,
    decode_tcp_party_create_request_payload,
    decode_tcp_party_roster_identity_payload,
    decode_tcp_player_spawn_identity_payload,
    diagnose_tcp_profile_payload,
    is_tcp_profile_payload_shape,
)


_MAX_REASONABLE_HIT_DAMAGE = 1_000_000_000_000
_DAMAGE_OUTLIER_MINIMUM = 100_000_000
_DAMAGE_OUTLIER_BASELINE_MULTIPLIER = 4096
_SELF_STATUS_REQUEST_LENGTHS = frozenset({44, 48})
_PROFILE_RESPONSE_MIN_LENGTH = 1024
_PROFILE_RESPONSE_WINDOW_SECONDS = 0.75
_INCOMING_DAMAGE_LAYOUTS = frozenset(
    {
        # This direct-hit form carries the target and resolved hit value.
        # CombatAggregator later caps overkill against the player's HP ledger.
        # Other player-target layouts are also used by status synchronization
        # and stay excluded until their semantics can be proven separately.
        "s2c-udp-hit-effect-direct37-v576",
        "s2c-udp-hit-effect-direct37-extended-v576",
    }
)

def _wire_fingerprint(packet: PacketRecord) -> str:
    """Return a stable, non-sensitive packet shape identifier."""
    header = packet.payload[5:9].hex() if len(packet.payload) >= 9 else ""
    transport = packet.transport.upper() or "OTHER"
    return (
        f"{transport}:{packet.inferred_direction() or 'other'}:"
        f"{len(packet.payload)}:{header}"
    )


def _probe_utf8_strings(payload: bytes) -> tuple[str, ...]:
    """Extract printable UTF-8 runs without treating them as decoded fields."""
    found: list[str] = []
    for match in re.finditer(rb"(?:[\x80-\xff][\x80-\xbf]{1,3}){2,}", payload):
        try:
            value = match.group(0).decode("utf-8").strip("\x00 \t\r\n")
        except UnicodeDecodeError:
            continue
        if value and value not in found and len(value) <= 64:
            found.append(value)
    return tuple(found[:8])


_PROBE_TEXT_RUN = re.compile(r"[\u3400-\u9fffA-Za-z0-9_·丶・\-（）()]{2,64}")


def _probe_visible_strings(payload: bytes) -> tuple[str, ...]:
    """Extract conservative readable text candidates from an unknown response."""
    sample = payload[:256 * 1024]
    found = list(_probe_utf8_strings(sample))
    for encoding in ("utf-8", "utf-16-le", "gb18030"):
        try:
            decoded = sample.decode(encoding, errors="ignore")
        except (LookupError, UnicodeError):
            continue
        for match in _PROBE_TEXT_RUN.finditer(decoded):
            value = match.group(0).strip("\x00 _-")
            if (
                len(value) >= 2
                and value not in found
                and not value.isdigit()
                and any(character.isalpha() for character in value)
            ):
                found.append(value)
                if len(found) >= 16:
                    return tuple(found)
    return tuple(found)


def _packet_content_fingerprint(packet: PacketRecord) -> str:
    digest = hashlib.blake2s(packet.payload, digest_size=8).hexdigest()
    return f"{_wire_fingerprint(packet)}:{digest}"


def _reverse_flow_matches(request: PacketRecord, response: PacketRecord) -> bool:
    return (
        request.transport.upper() == response.transport.upper()
        and request.source_ip == response.destination_ip
        and request.destination_ip == response.source_ip
        and request.source_port == response.destination_port
        and request.destination_port == response.source_port
    )


def _has_matching_tcp_message_length(payload: bytes) -> bool:
    """Validate the three-byte little-endian length used by v576 TCP messages."""
    return len(payload) >= 3 and int.from_bytes(payload[:3], "little") == len(payload)


def _split_length_framed_tcp_messages(payload: bytes) -> tuple[bytes, ...]:
    """Split consecutive v576 TCP messages by their three-byte length headers."""
    if len(payload) < 3:
        return (payload,)
    parts: list[bytes] = []
    offset = 0
    while offset + 3 <= len(payload):
        message_length = int.from_bytes(payload[offset : offset + 3], "little")
        if message_length < 3 or offset + message_length > len(payload):
            return (payload,)
        parts.append(payload[offset : offset + message_length])
        offset += message_length
    return tuple(parts) if offset == len(payload) and parts else (payload,)


def _self_status_request_actor_id(payload: bytes) -> int | None:
    """Return the actor ID carried by a verified v576 self-status request."""
    if len(payload) not in _SELF_STATUS_REQUEST_LENGTHS:
        return None
    decoded = decode_registered_tcp_envelope(payload)
    if decoded is None:
        return None
    plaintext, _key_index, _rounds, _source = decoded
    if len(plaintext) < 8 or plaintext[2:4] != b"\x04\x03":
        return None
    actor_id = int.from_bytes(plaintext[4:8], "little")
    return actor_id if actor_id > 0 else None


def _is_extreme_damage_outlier(amount: int, samples: tuple[int, ...]) -> bool:
    """Return true only for values far outside an established hit scale."""
    if amount < _DAMAGE_OUTLIER_MINIMUM or len(samples) < 24:
        return False
    ordered = sorted(samples)
    high_index = min(len(ordered) - 1, int(len(ordered) * 0.99))
    high_baseline = max(1, ordered[high_index])
    return amount > high_baseline * _DAMAGE_OUTLIER_BASELINE_MULTIPLIER


def _is_zero_value_action_status_marker(
    effect_code: str,
    parameters: str,
    permanent: bool,
) -> bool:
    """Identify a zero-valued action component, not a lasting player buff."""
    if not permanent or str(effect_code or "").strip().casefold() != "134400":
        return False
    try:
        return abs(float(str(parameters or "").strip())) <= 1e-9
    except ValueError:
        return False


_HOLY_HAMMER_ATTACK_EFFECT_CODES = frozenset(
    {
        # Paladin transformed basic-attack components.
        "134600",
        "134a00",
        "134b00",
        # Crusader transformed basic-attack components.
        "134d00",
        "134e00",
        "134f00",
    }
)


def _is_holy_hammer_attack_status(
    skill_id: int,
    effect_code: str,
    duration_ms: int,
    permanent: bool,
) -> bool:
    """Identify one component of a server-confirmed Holy Hammer attack."""
    return (
        int(skill_id) == 3224
        and permanent
        and int(duration_ms) == 0
        and str(effect_code or "").strip().casefold()
        in _HOLY_HAMMER_ATTACK_EFFECT_CODES
    )


_NORMAL_ATTACK_SKILL_ID = 0xFFFFFFFF
_NORMAL_ATTACK_SKILL_NAME = "普通/特殊攻击"
_UNRESOLVED_SKILL_ID = -1
_UNRESOLVED_SKILL_NAME = "待识别伤害"
# Encounter-owned attack rows are real outgoing damage identities, but they
# do not belong to any player's profession tree.  Keep this deliberately
# narrow: only independently audited packet/native result keys may enter the
# registry.  Timing proximity and recent casts must never manufacture one.
_DUNGEON_SPECIAL_SKILL_NAMES = {
    **SKILL_PLATE_DAMAGE_NAMES,
    733042: "某铁匠的超电磁炮",
}
# EX actions can use their own wire ID even though the game and simulator
# treat the enhanced action as the same damaging skill.
_LIVE_DAMAGE_SKILL_ALIASES = {
    41: 40,  # Native Skill_00_DeathNell, same description/level scaling as 40; exact UDP action.

    1554: 1409,  # 鹰击长空EX -> 鹰击长空
    # Silver Hunter's Holy state replaces five learned Hunter actions with
    # separate wire IDs.  They remain the same timeline/damage rows as their
    # ordinary skills, while the raw IDs must stay accepted by every decoder.
    1624: 1601,  # 神圣银色飞弹 -> 银色飞弹
    1625: 1605,  # 神圣银色箭雨 -> 银色箭雨
    1626: 1608,  # 神圣箭矢风暴 -> 箭矢风暴
    1627: 1611,  # 神圣瞬步射击 -> 瞬步射击
    1628: 1615,  # 神圣螺旋飓风 -> 螺旋飓风
    # Mechanical Mode emits self-status actions instead of the ordinary tower
    # action records. Attribute their following direct hits to the simulator's
    # dedicated Mechanical Mode skills, not to the summon versions.
    7305: 48002,  # 机械加农炮 -> 机械炮塔
    7307: 48001,  # 机械链锯内部动作 -> 机械链锯
    # Dark Avenger reports transformed actions with the internal Nightmare
    # IDs. They are alternate forms of the learned base skills, not separate
    # skills in damage statistics or on the cast timeline.
    7601: 7501,
    7602: 7502,
    7603: 7503,
    7605: 7505,
    7608: 7508,
    7611: 7511,
    7612: 7512,
    7613: 7513,
}
# A transformed Dark Avenger can report these base-Warrior integers in hit or
# transition records.  They are not reliable skill identities in that class
# context: treating them literally creates Side Kick/Elbow Drop/Sweeping Kick
# rows that were never cast.  Keep the damage, but let the active transformed
# cast instance own it instead.
_DARK_AVENGER_AMBIGUOUS_BASE_SKILL_IDS = frozenset(
    {2, 5, 6, 7, 8, 9, 10, 15, 20, 28, 29}
)
# v576 creates several player skills as short-lived Lv1 monster actors. Their
# damage packets often contain unrelated integers, so the spawned template is
# the authoritative skill identity. IDs 33607-33614 are ShadowR/ShadowL
# variants created by Shadow Summon.
_SKILL_OBJECT_TEMPLATE_SKILL_IDS: dict[int, tuple[int, ...]] = {
    **{template_id: (3409,) for template_id in range(30001, 30021)},
    **{template_id: (3403,) for template_id in (30101, 30102, 30151, 30152, 30153)},
    **{template_id: (3422,) for template_id in (30201, 30202, 30203, 30251, 30252, 30253, 30254)},
    **{template_id: (3423,) for template_id in (*range(30301, 30312), 30399)},
    **{template_id: (3412,) for template_id in (30401, 30402)},
    **{template_id: (3409,) for template_id in range(30501, 30521)},
    30601: (3409,),  # Mutated Lightning Relic
    503142: (3409,),
    503552: (3409,),
    503555: (3409,),
    30251: (3422,),  # Cure Relic
    30311: (3423,),  # Bind Relic
    30401: (3412,),  # Miracle Relic
    30501: (3409,),  # Lightning Relic EX
    31001: (1303,),  # Cheating Point
    32013: (4408,),  # Ice Palm
    32022: (4410,),  # Magma Wall
    32004: (4209,),  # Cannon Tower
    32005: (4216,),  # Alfredo Duck
    32008: (4015, 4223),  # Alfredo summoned by either engineer command
    32011: (4351,),  # Chainsaw Tower
    32026: (4351,),  # Amplified Chainsaw Tower
    32029: (4351,),  # Mutated Chainsaw Tower
    33101: (2410,),  # Force Mirror
    # Azuna's attack companion and Buff companion both persist across normal
    # scene transitions.  The latter casts monster skill 2619xx every five
    # seconds and refreshes the finite 30-second Breath Buff; the companion is
    # permanent, the Buff itself is not.
    33601: (6318,),
    33603: (6316,),
    **{template_id: (69001, 6319) for template_id in range(33607, 33615)},
}
# Some professions share the same base summon skill ID while creating
# structurally different monster templates.  The template is stronger owner
# evidence than cast proximity in that case.  In particular, Inquisitor
# (job_44) creates the ordinary Lightning Relic actors, while Saint (job_43)
# replaces them with the EX/strengthened actors.  Without this restriction a
# Saint cast can steal every nearby Inquisitor relic (and vice versa) because
# both are recorded as skill 3409.
_SKILL_OBJECT_TEMPLATE_OWNER_PROFILES: dict[int, frozenset[str]] = {
    **{
        template_id: frozenset({"job_44"})
        for template_id in range(30001, 30021)
    },
    **{
        template_id: frozenset({"job_43"})
        for template_id in range(30501, 30521)
    },
}
_PERMANENT_SKILL_OBJECT_TEMPLATE_IDS = frozenset({33601, 33603})
_CAST_REQUIRED_PACKET_SKILL_IDS = frozenset({5220})
_STRUCTURED_ACTION_EXCLUDED_SKILL_IDS = frozenset(
    {
        3426,  # Magic Wand Mastery is passive state evidence, not a cast.
    }
)
# The validated reader separates the struck actor from result-group/object
# identifiers. Never overwrite this wire target with the most recent enemy.
_TRANSIENT_DAMAGE_TARGET_LAYOUTS: frozenset[str] = frozenset()
_PERMANENT_PLAYER_BUFF_SKILLS: dict[int, tuple[str, bool]] = {
    2213: ("火焰光环", True),
    2214: ("寒冰光环", True),
    3211: ("元素光环", True),
    3213: ("判定光环", True),
    5209: ("激增灵魂", False),
}
_PERMANENT_PLAYER_BUFF_TRIGGER_SKILLS: dict[int, int] = {}
_MUTUALLY_EXCLUSIVE_AURA_SKILL_IDS = frozenset(
    {2213, 2214, 3211, 3213}
)
# Mechanical Mode replaces the ordinary tower buttons with internal status
# skills. v576 does not send their usual action record, so these two attacks
# need their self-status application to serve as authoritative cast evidence.
_STATUS_BACKED_MECHANIC_SKILLS = {
    7305: (48002, "机械炮塔", 0.2),
    48002: (48002, "机械炮塔", 0.2),
    7307: (48001, "机械链锯", 3.0),
    48001: (48001, "机械链锯", 3.0),
}
_UNIQUE_MECHANIC_MODE_SKILL_IDS = frozenset(
    definition[0] for definition in _STATUS_BACKED_MECHANIC_SKILLS.values()
)
LiveDpsEvent = (
    CombatEvent
    | SettlementEvent
    | BossStatusEvent
    | BossSpawnEvent
    | BossHealthEvent
    | MonsterAppearanceEvent
    | BossActionEvent
    | SkillCastEvent
    | SkillParameterEvent
    | PartyMemberEvent
    | PlayerHealthEvent
    | PlayerLifecycleEvent
    | PlayerInspectEvent
    | SummonRemovalEvent
)


from .replay_normalization import bind_host_globals as _bind_replay_normalization
from .replay_normalization import (
    _live_target_identity,
    _canonical_live_skill_id,
    _canonicalize_live_message,
    _supplement_boss_statuses_from_casts,
    _supplement_player_buffs_from_casts,
    _backfill_preexisting_maintained_player_buffs,
    _canonicalize_replay_actor_context,
    _ReplayPlayerSegment,
    _is_generic_replay_actor_name,
    _replay_skill_signature_similarity,
    _replay_rejoined_player_aliases,
    _merge_rejoined_replay_players_once,
    _merge_rejoined_replay_players,
    _backfill_permanent_skill_object_history,
)











_PREEXISTING_MAINTAINED_PLAYER_BUFF_KEYS = frozenset(
    {
        "鸡尾酒",
        "突袭",
    }
)




















_CONTINUATION_DAMAGE_WINDOWS = {
    # Chain Lightning continues producing server hit records after the player
    # has already started another action. Those continuation records omit the
    # skill ID, so the cast remains their owner for the full bounce window.
    3019: 4.5,
    # Ice Spike emits five skill-ID-free periodic damage records beside its
    # independently refreshed resistance status.
    2211: 4.5,
    # Ice Shot has a delayed second hit whose packet can carry only the status
    # owner ID. Both hits belong to the same cast.
    2034: 3.2,
    # Spirit Paper and Beast Spirit apply Curse while their own projectiles
    # continue dealing damage. Those later projectile records omit the skill
    # ID and were previously swallowed by the generic Curse damage bucket.
    5214: 1.6,
    5217: 6.5,
    # Grudge Formation remains on the field and deals eight delayed hits. The
    # final five records carry no usable skill ID, but still belong to the cast.
    5207: 9.5,
    # Eagle Strike lands after the action has already allowed the Windwalker
    # to chain into Double Somersault. In that combo its impact record can omit
    # the owning skill ID, so the later button press must not steal the hit.
    1409: 1.25,
    1554: 1.25,
    # Twilight Light can hand control back shortly before its final server
    # hit records arrive.  Those records carry no usable skill ID, so a
    # following Illusion Attack used to steal the tail of every cast.
    6701: 1.55,
}

# Some skills overlap in a real rotation and even reuse the same skill-less
# hit packet families. Their measured hit rhythm supplies the missing identity
# without falling back to whichever button happened to be pressed last.
_CONTINUATION_DAMAGE_PROFILES = {
    1409: {
        "delays": (0.583,),
        "tolerance": 0.34,
        "signatures": frozenset({(140, b"\x65\x67\x89\x00")}),
    },
    1554: {
        "delays": (0.583,),
        "tolerance": 0.34,
        "signatures": frozenset({(140, b"\x65\x67\x89\x00")}),
    },
    5214: {
        "delays": (
            0.40,
            0.45,
            0.45,
            0.58,
            0.65,
            0.79,
            0.89,
            0.99,
            0.99,
            1.05,
            1.05,
            1.19,
            1.19,
            1.29,
            1.29,
        ),
        "tolerance": 0.22,
        "signatures": frozenset(
            {
                (148, b"\x92\x30\x91\x00"),
                (226, b"\x92\xd0\xdf\x00"),
            }
        ),
    },
    5217: {
        "delays": (
            1.08,
            1.87,
            3.07,
            3.17,
            3.37,
            3.53,
            3.67,
            3.67,
            3.67,
            3.88,
            4.03,
            4.87,
            4.87,
            4.98,
            4.98,
            5.17,
            5.17,
            5.38,
            5.38,
            5.48,
            5.48,
            5.67,
            5.67,
            5.87,
            5.87,
        ),
        "tolerance": 0.25,
        "signatures": frozenset(
            {
                (148, b"\x92\x30\x91\x00"),
                (226, b"\x92\xd0\xdf\x00"),
                (227, b"\xe5\xa7\xe0\x00"),
            }
        ),
    },
    5207: {
        "delays": (1.21, 2.26, 3.26, 4.32, 5.32, 6.37, 7.40, 8.52),
        "tolerance": 0.36,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (214, b"\xe5\x47\xd3\x00"),
                (215, b"\x97\x35\xd4\x00"),
            }
        ),
    },
}

# Area skills can keep emitting records after their ordinary direct-cast
# attribution window has ended.  Some fields overlap in normal rotations, so
# a later cast must not replace an earlier field that is still dealing damage.
# Skill-specific packet families win; genuinely ambiguous shared families stay
# unresolved instead of corrupting both skills' shares.
_SUSTAINED_CAST_DAMAGE_RULES = {
    # Arrow Storm EX keeps producing its appended volley after the ordinary
    # direct-action window has closed. These records carry only generic hit
    # packet families, but the measured 3.0-7.0 second window is unique to the
    # still-active Arrow Storm cast.
    1207: {
        "result_skill_id": 1207,
        "min_delay": 1.75,
        "max_delay": 7.0,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (147, b"\x2e\x8c\x90\x00"),
                (148, b"\x92\x30\x91\x00"),
                (159, b"\x6e\x2c\x9c\x00"),
                (160, b"\x2e\x6c\x9d\x00"),
                (170, b"\xd0\x72\xa7\x00"),
                (172, b"\x6e\x0c\xa9\x00"),
                (182, b"\xa7\xe5\xb3\x00"),
                (184, b"\x28\x0a\xb5\x00"),
                (194, b"\x18\x7a\xbf\x00"),
                (199, b"\x2e\x6c\xc4\x00"),
                (208, b"\xa7\x65\xcd\x00"),
                (213, b"\x92\x30\xd2\x00"),
                (214, b"\xe5\x47\xd3\x00"),
                (215, b"\x97\x35\xd4\x00"),
                (216, b"\xfe\x5c\xd5\x00"),
                (217, b"\xce\x6c\xd6\x00"),
                (218, b"\x65\xc7\xd7\x00"),
                (218, b"\x65\x87\xd7\x00"),
                (223, b"\x28\x6a\xdc\x00"),
                (225, b"\x2e\x2d\xde\x00"),
                (225, b"\x2e\x6c\xde\x00"),
                (226, b"\x92\xd0\xdf\x00"),
                (227, b"\xe5\xa7\xe0\x00"),
                (229, b"\xfe\xbc\xe2\x00"),
                (230, b"\xce\x8c\xe3\x00"),
            }
        ),
    },
    # Siege Stance deals its normal arrows throughout the fixed four-second
    # stance. The dominant v576 family contains no skill ID, so keep it tied
    # to the still-active cast instead of exposing it as generic extra damage.
    1220: {
        "result_skill_id": 1220,
        "min_delay": 1.75,
        "max_delay": 4.5,
        "signatures": frozenset({(218, b"\x65\x87\xd7\x00")}),
    },
    2401: {
        "result_skill_id": 2401,
        "min_delay": 0.0,
        "max_delay": 8.0,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (212, b"\x2e\x8c\xd1\x00"),
                (224, b"\x6e\x2c\xdd\x00"),
            }
        ),
    },
    2556: {
        "result_skill_id": 2401,
        "min_delay": 0.0,
        "max_delay": 8.0,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (212, b"\x2e\x8c\xd1\x00"),
                (224, b"\x6e\x2c\xdd\x00"),
            }
        ),
    },
    # These registered-object skills are resolved only through Projectile
    # template -> damage_object_id -> direct37. Keep their old packet-family
    # rows solely as ambiguity suppressors; timing/signature similarity can
    # never assign their damage.
    2404: {
        "result_skill_id": 2404,
        "min_delay": 3.0,
        "max_delay": 5.2,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
                (170, b"\xd0\x72\xa7\x00"),
            }
        ),
    },
    2554: {
        "result_skill_id": 2404,
        "min_delay": 3.0,
        "max_delay": 5.2,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
                (170, b"\xd0\x72\xa7\x00"),
            }
        ),
    },
    2405: {
        "result_skill_id": 2405,
        "min_delay": 3.0,
        "max_delay": 8.0,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
                (170, b"\xd0\x72\xa7\x00"),
            }
        ),
    },
    2555: {
        "result_skill_id": 2405,
        "min_delay": 3.0,
        "max_delay": 8.0,
        "suppression_only": True,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
                (170, b"\xd0\x72\xa7\x00"),
            }
        ),
    },
}

# Lightning Impact applies Shock for ten seconds.  A few shock ticks omit the
# status reference entirely, even while neighbouring records retain it.  This
# cast-backed fallback restores only the packet families observed for those
# missing ticks and still reports them in the generic Shock row.
_PERIODIC_CAST_DAMAGE_FALLBACKS = {
    3202: {
        "result_skill_id": 3202,
        "kind": "shock",
        "label": "触电",
        "min_delay": 1.5,
        "max_delay": 10.25,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
            }
        ),
    },
    3355: {
        "result_skill_id": 3202,
        "kind": "shock",
        "label": "触电",
        "min_delay": 1.5,
        "max_delay": 10.25,
        "signatures": frozenset(
            {
                (136, b"\xe5\xe7\x85\x00"),
                (148, b"\x92\x30\x91\x00"),
            }
        ),
    },
}

# Holy Punishment is a player-owned pulsing aura rather than a status DOT.
# Its v576 hit records omit the skill ID and can therefore be mistaken for
# the latest direct action or for an overlapping Shock state.  A confirmed
# cast activates one non-stacking 20-second aura; each target can receive at
# most roughly two anonymous pulses per second.  Explicit skill IDs and the
# measured continuation profiles are resolved before this fallback.
_REFRESHABLE_PULSING_AURA_RULES = {
    3205: {
        # The active button remains the base skill ID after learning EX.
        # Never infer EX damage from that cast alone: require the exact
        # self-status component emitted only by the learned EX passive.
        "result_skill_id": 3306,
        "requires_exact_ex_status": True,
        "duration": 20.0,
        "min_delay": 0.30,
        "min_hit_gap": 0.38,
        "status_effect_class": 62,
        "status_parameters": "Skill_DivinePunishment_EX_Attack",
    },
    3306: {
        # 3205 is the non-EX buff. The unpacked skill tree and SkillLevel
        # table identify 3306 as the EX skill that owns the 60% pulse.
        "result_skill_id": 3306,
        "requires_exact_ex_status": False,
        "duration": 20.0,
        "min_delay": 0.30,
        "min_hit_gap": 0.38,
        "status_effect_class": 62,
        "status_parameters": "Skill_DivinePunishment_EX_Attack",
    },
}


@dataclass(slots=True)
class _LearnedDamageTimingProfile:
    delays: list[float] = field(default_factory=list)
    signatures: Counter[str] = field(default_factory=Counter)

    def add(self, delay: float, signature: str) -> None:
        self.delays.append(round(float(delay), 4))
        if len(self.delays) > 256:
            del self.delays[: len(self.delays) - 256]
        self.signatures[signature] += 1


@dataclass(frozen=True, slots=True)
class _PeriodicDamageRule:
    source_skill_id: int
    source_skill_name: str
    kind: str
    label: str
    effect_class: int
    variants: frozenset[tuple[str, int]] = frozenset()

    def matches(self, parameters: str, duration_ms: int) -> bool:
        if not self.variants:
            return True
        normalized = _normalize_status_parameters(parameters)
        return any(
            candidate_parameters == normalized
            and abs(candidate_duration - int(duration_ms)) <= 250
            for candidate_parameters, candidate_duration in self.variants
        )


@dataclass(frozen=True, slots=True)
class _StatusEffectDefinition:
    display_name: str
    source_type: str
    rule_key: str = ""
    fallback_value: float | None = None
    fallback_duration_ms: int | None = None
    stackable: bool = False
    stack_group: str = ""


@dataclass(frozen=True, slots=True)
class _ResolvedBossDebuff:
    effect_key: str
    value: float | None
    duration_ms: int
    parameters: str
    stackable: bool = False
    stack_group: str = ""
    value_source: str = ""
    duration_source: str = ""

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.effect_key,
            self.stackable,
            self.stack_group,
        )


@dataclass(frozen=True, slots=True)
class _MonsterTemplate:
    template_id: int
    name: str
    level: int
    hp: int
    physical_defense: int
    magic_defense: int
    critical_resistance: int
    fire_resistance: float
    ice_resistance: float
    light_resistance: float
    dark_resistance: float
    actor_table_id: int
    boss: bool


@dataclass(slots=True)
class _ActivePeriodicDamage:
    source_actor_id: int
    target_actor_id: int
    raw_skill_id: int
    source_skill_id: int
    source_skill_name: str
    kind: str
    label: str
    effect_code: str
    applied_at: float
    expires_at: float


@dataclass(slots=True)
class _ActivePulsingAura:
    """Server-confirmed lifetime for one player-owned pulsing aura.

    The individual direct37 rows emitted by these auras use short-lived
    result-object IDs and do not repeat the status instance ID.  The status
    response can therefore prove START/REFRESH and the expiry boundary, but
    it cannot by itself turn every pulse into a deterministic object-chain
    attribution.
    """

    source_actor_id: int
    raw_skill_id: int
    source_skill_id: int
    effect_instance_id: int
    applied_at: float
    expires_at: float


_SUPPLEMENTAL_DIRECT_CAST_WINDOW_SECONDS = 1.75

# Some EX actions emit both their game-facing hit records and additional
# damage records through the same generic packet families. The in-game skill
# statistics count only the fixed direct-hit budget under the active skill;
# overflow is shown as additional damage. Limits are per target so legitimate
# multi-target hits are not discarded.
_DIRECT_HIT_LIMITS_PER_TARGET = {
    1003: 7,  # Multi Shot
    1202: 32,  # Aerial Chain Shot EX
    48002: 1,  # Mechanical Cannon emits one direct hit per status-backed cast.
}

# These records carry real damage, but do not prove one game-facing hit for
# hit-count mechanics such as freeze shatter. ``direct_hit_overflow`` is not in
# this set: it is still backed by a direct-hit wire record; only its ownership
# is moved to the additional-damage display bucket after the per-cast profile
# budget is exhausted.
_NON_REAL_HIT_EVIDENCE_METHODS = frozenset(
    {
        "action_free_special_attack",
        "periodic_cast_fallback",
        "periodic_status",
        "periodic_status_packet",
        "refreshable_pulsing_aura",
        "status_damage_packet",
        "supplemental_cast_owner",
        "supplemental_continuation",
        "supplemental_same_packet",
    }
)


def _has_real_hit_evidence(layout: object, attribution_method: object) -> bool:
    """Return whether one damage event has protocol-backed real-hit evidence.

    This intentionally answers a narrower question than "is the damage
    factual".  HP/status damage and additional-damage procs remain factual
    damage while returning False here, so later hit-count mechanics can choose
    the stricter stream without changing damage totals or scoring today.
    """

    method = str(attribution_method or "").strip().casefold()
    if method in _NON_REAL_HIT_EVIDENCE_METHODS:
        return False
    packet_layout = str(layout or "").strip().casefold()
    if not packet_layout.startswith("s2c-udp-"):
        return False
    if "periodic" in packet_layout or "status-damage" in packet_layout:
        return False
    return (
        packet_layout.startswith("s2c-udp-hit-")
        or packet_layout.startswith("s2c-udp-hit-effect-")
        or packet_layout == "s2c-udp-projectile-direct37-v576"
    )


def _normalize_status_parameters(value: str) -> str:
    tokens: list[str] = []
    for raw_token in str(value or "").split(";"):
        token = raw_token.strip()
        if not token:
            tokens.append("")
            continue
        try:
            number = float(token)
        except ValueError:
            tokens.append(token)
            continue
        tokens.append(f"{number:.12g}")
    return ";".join(tokens)


_PROFILE_LEARNING_METHODS = frozenset(
    {
        "packet_skill_id",
        "appended_status_first_hit",
        "continuation_window",
        "unique_active_cast",
    }
)

# Some summons apply their status through an internal effect skill rather than
# the player-facing skill ID. The packet still carries the summon actor, so the
# owner mapping remains the primary source and this table is the deterministic
# fallback before the summon has produced its first damage record.
_STATUS_SKILL_ALIASES = {
    30612: 3409,  # 雷电之手的召唤物攻击
    30613: 3422,  # 痊愈之手的光攻增益
    30614: 3423,  # 掌握之手的召唤物状态
    30615: 3412,  # 奇迹之手的保护状态
    30616: 3409,  # 雷电之手的减光抗
    30619: 3423,  # 掌握之手EX的团队增伤
    30706: 2410,  # 能量镜持续刷新的能量反射魔法增伤
    30813: 4410,  # 岩浆墙的召唤物状态
    30810: 4405,  # 魔法草的火焰攻击
    30812: 4405,  # 魔法草的中毒攻击
    30820: 5216,  # 灵魂之门的召唤物状态
    30821: 5216,
    30822: 5216,
    400601: 5118,  # 九天之舞2阶的团队增益状态
}
# The permanent Azuna Buff companion (template 33603) uses one internal
# monster-skill ID per learned level.  These rows all describe the same
# player-facing Breath skill and carry the real 30-second duration/value in
# the status response.
_AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS = frozenset(range(261901, 261931))
_AZUNA_BREATH_REFRESH_INTERVAL_SECONDS = 5.0
_AZUNA_BREATH_DURATION_MS = 30_000
_TEAM_BUFF_INTERNAL_STATUS_SKILL_IDS = frozenset({400601})

# Some party-wide buffs are mirrored once per recipient with that recipient as
# the packet source, followed by the authoritative caster-to-party fanout.  The
# mirror is a transport detail, not a personal buff.  Keep this list limited to
# signatures confirmed from captures so unrelated self states are untouched.
# These party-wide effects are emitted once per recipient with that recipient
# in the packet's source field.  Their signatures are capture-verified; using
# the newest matching real cast repairs the source without touching unrelated
# self states that happen to share an effect code.
_TEAM_BUFF_RECIPIENT_MIRROR_SIGNATURES = {
    6009: ("134800", 300_000, (10.0,)),  # 突袭
    6219: ("135800", 15_000, ()),  # 奉献乌鸦
}
_TEAM_BUFF_RECIPIENT_MIRROR_SOURCE_WINDOW_SECONDS = 5.0

# Summoned actors use short, session-local IDs and may emit their own cast or
# hit packets before an internal status packet reveals their owner.  Associate
# those actors with the newest matching player cast instead of exposing each
# summon as another party member.  Names are used here so EX and job-specific
# IDs can be discovered from the bundled skill catalog without protocol magic
# numbers leaking into the attribution logic.
_SUMMON_CREATION_SKILL_NAMES = frozenset(
    {
        "雷电之手",
        "治愈之手",
        "痊愈之手",
        "掌握之手",
        "奇迹之手",
        "炮塔",
        "加特林机枪塔",
        "寒冰喷射塔",
        "链锯之塔",
        "重磅鸭",
        "魔法草",
        "灵魂之门",
        "怨恨之像",
        "召唤傀儡",
        "阿兹那之息",
        "阿兹那之约",
        "暗影召唤术",
        "瞒天过海",
    }
)
_SUMMON_ACTOR_ID_MAX = 0x00FFFFFF
_SUMMON_SPAWN_WINDOW_SECONDS = 8.0
_CURE_RELIC_SKILL_ID = 3422
_CURE_RELIC_LIGHT_BUFF_VALUES = frozenset({3.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0})
_CURE_RELIC_REFRESH_SOURCE_WINDOW_SECONDS = 22.0

_HEALING_SKILL_MODELS = load_healing_skill_models()
_HEALING_SKILL_NAMES = frozenset(
    model.name for model in _HEALING_SKILL_MODELS
)
_HEALING_CAST_WINDOWS = {
    healing_skill_key(model.name): max(
        0.5,
        model.active_window_ms() / 1000.0,
    )
    for model in _HEALING_SKILL_MODELS
}


@lru_cache(maxsize=4096)
def _summon_skill_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("EX", "")


def _skill_branch_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("ＥＸ", "EX")


_SKILL_PLATE_SECOND_JOB_AFFINITY = {
    # 神圣暴风没有独立 EX 名称，但其二转强化机制属于雷神分支。
    _skill_branch_key("神圣暴风"): "雷神",
}


def _skill_display_name(
    skill_catalog: dict[int, tuple[str, str, int]], skill_id: int
) -> str:
    if skill_id == _NORMAL_ATTACK_SKILL_ID:
        return _NORMAL_ATTACK_SKILL_NAME
    if skill_id in _DUNGEON_SPECIAL_SKILL_NAMES:
        return _DUNGEON_SPECIAL_SKILL_NAMES[skill_id]
    return skill_catalog.get(skill_id, (str(skill_id), "", 99))[0]


from .capture_sources import bind_host_globals as _bind_capture_sources
from .capture_sources import (
    CombatEventSource,
    JsonlReplaySource,
    V576PcapReplaySource,
    V576DiagnosticReplaySource,
    scan_v576_diagnostic_map_history,
    V576PktmonTextStreamSource,
    auto_capture_confirmation_reason,
    V576AutoCaptureProbe,
    V576NpcapSource,
    V576MemorySource,
)





from .protocol_catalogs import bind_host_globals as _bind_protocol_catalogs
from .protocol_catalogs import (
    _load_monster_templates,
    _load_boss_action_catalog,
    _load_boss_skill_catalog,
    _load_player_buff_clear_boss_skill_ids,
    _load_skill_catalog,
    _load_damage_skill_ids,
    _load_status_skill_catalog,
    _load_team_buff_skill_names,
    _load_status_effect_catalog,
    _load_periodic_damage_catalog,
    _load_skill_profiles,
    _load_profile_jobs,
    _load_inspect_job_codes,
    _load_profile_lineages,
)

































from .wire.actor_state import V576ActorStateMixin, bind_host_globals as _bind_V576ActorStateMixin
from .wire.profile_decoder import V576ProfileDecoderMixin, bind_host_globals as _bind_V576ProfileDecoderMixin
from .wire.combat_decoder import V576CombatDecoderMixin, bind_host_globals as _bind_V576CombatDecoderMixin
from .wire.status_decoder import V576StatusDecoderMixin, bind_host_globals as _bind_V576StatusDecoderMixin

from .wire.core_decoder import (
    V576DecoderCoreMixin,
    bind_host_globals as _bind_V576DecoderCoreMixin,
)


class _V576PacketEventDecoder(
    V576DecoderCoreMixin,
    V576ActorStateMixin,
    V576ProfileDecoderMixin,
    V576CombatDecoderMixin,
    V576StatusDecoderMixin,
):
    pass


# V430_V576_CORE_SPLIT_APPLIED











_bind_replay_normalization(globals())

_bind_protocol_catalogs(globals())

_bind_capture_sources(globals())

# V430_SPLIT_MODULES_APPLIED
_bind_V576ActorStateMixin(globals())
_bind_V576ProfileDecoderMixin(globals())
_bind_V576CombatDecoderMixin(globals())
_bind_V576StatusDecoderMixin(globals())
_bind_V576DecoderCoreMixin(globals())


# Keep the historical ``live.sources`` module a true compatibility
# facade.  Tests and third-party integrations have always patched decoder or
# capture dependencies through this module.  Methods moved to the split
# modules resolve globals in their defining module, so forward later facade
# assignments to every split implementation as well.
import sys as _compat_sys
import types as _compat_types
from . import capture_sources as _split_capture_sources
from . import protocol_catalogs as _split_protocol_catalogs
from . import replay_normalization as _split_replay_normalization
from .wire import core_decoder as _split_core_decoder
from .wire import actor_state as _split_actor_state
from .wire import combat_decoder as _split_combat_decoder
from .wire import profile_decoder as _split_profile_decoder
from .wire import status_decoder as _split_status_decoder


class _SourcesCompatibilityModule(_compat_types.ModuleType):
    _split_targets = (
        _split_capture_sources,
        _split_protocol_catalogs,
        _split_replay_normalization,
        _split_core_decoder,
        _split_actor_state,
        _split_combat_decoder,
        _split_profile_decoder,
        _split_status_decoder,
    )

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in self._split_targets:
            if hasattr(target, name):
                setattr(target, name, value)


_compat_sys.modules[__name__].__class__ = _SourcesCompatibilityModule
