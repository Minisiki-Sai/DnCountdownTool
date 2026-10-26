from __future__ import annotations

import heapq
from dataclasses import dataclass, replace

from .damage_attribution import (
    is_deterministic_attribution,
    should_replace_attribution,
)
from .damage_object_origins import (
    PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS,
    FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS,
    _DAMAGE_OBJECT_SPECS,
)
from .generated_player_root_action_specs import (
    PLAYER_ROOT_ACTION_SIGNAL_SKILLS,
)
from .generated_player_root_full_action_specs import (
    FULL_PLAYER_ROOT_ACTION_SIGNAL_SKILLS,
)
from .generated_attack_instance_specs import CLIENT_ACT_GENERATED_PROJECTILE_CHILD_EDGES
from .models import CombatEvent
from .skill_plate_damage import (
    CLERIC_GRAVITY_PLATE_ROOT_KEYS, SKILL_PLATE_DAMAGE_NAMES, SKILL_PLATE_PROJECTILE_SKILLS,
)
from .protocol import (
    _V576_BODY_XOR_KEY,
    _V576_XOR_PHASE_BY_SIGNATURE_BYTE,
    _decode_v576_body,
)


_PLAYER_ID_MIN = 0x01000000
_OBJECT_ID_MAX = 0x00FFFFFF
_PROJECTILE_CHILD_EDGES = dict(CLIENT_ACT_GENERATED_PROJECTILE_CHILD_EDGES)

# C2S registrations have no server root-generation broadcast. Their complete
# ACT coordinates prove the originating skill, but only during the already
# reviewed object's lifetime. Unknown lifetimes never acquire a default.
_CLIENT_OBJECT_LIFETIMES: dict[tuple[int, int], float] = {}
_CLIENT_HIT_IDENTITY_LIFETIMES: dict[tuple[int, int], float] = {}
_REVIEWED_HIT_IDENTITY_KEYS: set[tuple[int, int]] = set()
for _client_spec in _DAMAGE_OBJECT_SPECS:
    _client_lifetime_key = (
        _client_spec.projectile_template_id, _client_spec.canonical_skill_id,
    )
    _client_lifetime = float(_client_spec.lifetime_seconds)
    if _client_lifetime > 0:
        _CLIENT_OBJECT_LIFETIMES[_client_lifetime_key] = min(
            _CLIENT_OBJECT_LIFETIMES.get(_client_lifetime_key, _client_lifetime),
            _client_lifetime,
        )
        _CLIENT_HIT_IDENTITY_LIFETIMES[_client_lifetime_key] = min(
            _CLIENT_HIT_IDENTITY_LIFETIMES.get(
                _client_lifetime_key, _client_spec.direct_hit_lifetime_seconds),
            _client_spec.direct_hit_lifetime_seconds,
        )
        if (_client_spec.factual_active_seconds is not None
                or _client_spec.identity_retirement_seconds is not None):
            _REVIEWED_HIT_IDENTITY_KEYS.add(_client_lifetime_key)

# ``A90370.config_row`` is the concrete Projectile/object configuration copied
# into the player's damage object.  Only rows with one canonical client-table
# skill are usable.  Seven jobs contain shared child rows; those particular
# rows still need a parent-lineage split, while their other unique config rows
# remain valid exact object evidence.
_CONFIG_ROW_SKILLS: dict[int, int] = {}
_config_candidates: dict[int, set[int]] = {}
for _spec in _DAMAGE_OBJECT_SPECS:
    _config_candidates.setdefault(int(_spec.projectile_template_id), set()).add(
        int(_spec.canonical_skill_id)
    )
for _row_id, _skills in _config_candidates.items():
    if len(_skills) == 1:
        _CONFIG_ROW_SKILLS[_row_id] = next(iter(_skills))
_CONFIG_ROW_SKILLS.update(SKILL_PLATE_PROJECTILE_SKILLS)
for _full_action_required_config in FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS:
    _CONFIG_ROW_SKILLS.pop(_full_action_required_config, None)

# Native image SHA452092fd...983b, EC168 CDnShockInfectionBlow ctor31FFC0
# and activation320670 create a separate CDnSkill whose action is
# Skill_ShockInfection. Old/current merged Cleric ACT281 signal3 both spawn
# row4004. The static SkillLevel-action generator does not enumerate this
# native-driven passive action, hence keep its independently proved tuple
# separate from the 832 generated ordinary-action tuples.
_NATIVE_SAME_COMPOUND_ROOT_ACTION_SIGNAL_SKILLS = {
    ('雷神', 281, 3, 4004): 3551,
}

# SkillLevel's direct action list omits this command-selected air form. Nine
# old/current Kali type29 signals carry skill key 5205 at payload+4 and name
# Skill_ChainClaw_EX_Air in string slots 2 and 11. Its merged ACT191 signal13
# is type15/config5529 with an explicit 3000-ms lifetime. Skill5304 (the EX
# variant) also has BaseSkillID5205. Keep this reviewed edge separate from
# the generated ordinary ACT map, and never name a config5529-only record.
_REVIEWED_COMMAND_ROOT_ACTION_SIGNAL_SKILLS = {
    ('黑暗萨满', 191, 13, 5529): 5205,
}
_CLIENT_HIT_IDENTITY_LIFETIMES[(5529, 5205)] = 3.0
_REVIEWED_HIT_IDENTITY_KEYS.add((5529, 5205))


def _full_action_signal_skill(key) -> int:
    return int(FULL_PLAYER_ROOT_ACTION_SIGNAL_SKILLS.get(
        key, _REVIEWED_COMMAND_ROOT_ACTION_SIGNAL_SKILLS.get(key, 0),
    ))


_SAFE_CONFIG_JOBS = frozenset(
    {
        "剑皇", "月之领主", "狂战士", "毁灭者", "黑暗复仇者",
        "影舞者", "风行者", "银色猎人", "火舞", "时空领主",
        "黑暗女王", "圣骑士", "十字军", "圣徒", "雷神", "重炮手",
        "机械大师", "炼金圣士", "药剂师", "黑暗萨满", "噬魂者",
        "刀锋舞者", "灵魂舞者",
    }
)

# The client ``kali_basic.act`` defines all three fan basic-attack branches with an
# exact type-15 spawn signal:
#
# * ``Attack1_Fan``: merged action 11, signal 13, payload/config row 5500
# * ``Attack2_Fan``: merged action 36, signal 11, payload/config row 5500
# * ``Attack3_Fan``: merged action 38, signal 11, payload/config row 5501
#
# Projectile row 5500 is not globally unique: another job's client table maps
# the same numeric row to skill 1607.  Keep this disambiguation on the complete
# job + ACT coordinate + config tuple instead of weakening the global config
# rule or inheriting a nearby cast name.
_KALI_FAN_BASIC_ATTACK_ID = "basic_attack:kali_fan"
_KALI_FAN_BASIC_ATTACK_NAME = "卡莉扇刃普通攻击"
_KALI_FAN_BASIC_ATTACK_KEYS = frozenset(
    (
        job,
        action_index,
        signal_key,
        config_row,
    )
    for job in ("黑暗萨满", "噬魂者", "刀锋舞者", "灵魂舞者")
    for action_index, signal_key, config_row in (
        (11, 13, 5500), (36, 11, 5500), (38, 11, 5501),
        (33, 7, 5502),  # Jump_Attack_Fan, complete type15 birth tuple
    )
)

# Current Wisp_BlackDragon_HC_Player.act type-15 roots. The concrete high
# Projectile rows and complete local ACT slots identify dungeon mechanics
# independently of the character's ordinary skill catalog. Both payloads
# carry 5000 ms; neither weapon ACT spawns projectile children.
_WISP_TRANSFORM_ROOTS = {
    (27, 8, 73000017): (
        "mechanism:blackdragon_hc_wisp_lm", "黑龙硬核变身左键射击", 5.0,
    ),
    (44, 9, 73000018): (
        "mechanism:blackdragon_hc_wisp_rm", "某铁匠的超电磁炮（黑龙硬核变身）", 5.0,
    ),
}

def _signed(raw: bytes) -> int:
    return int.from_bytes(raw, "little", signed=True)


def _typed_records(payload: bytes, direction: str):
    """Walk one complete v576 message without the audit inventory overhead.

    The general physical-Hit inventory hashes every row and checks all phases,
    which is appropriate for offline audits but too expensive on the live
    path.  Normal signatures encode their phase; only an unknown/invalid
    signature falls back to the remaining phases here.
    """
    if (
        direction != "server_to_client"
        or len(payload) < 21
        or int.from_bytes(payload[7:9], "little") + 3 != len(payload)
    ):
        return ()
    preferred = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(payload[5])
    phases = (
        (preferred,)
        if preferred is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )
    if preferred is not None:
        phases += tuple(
            phase for phase in range(len(_V576_BODY_XOR_KEY))
            if phase != preferred
        )
    for phase in phases:
        body = _decode_v576_body(payload, phase)
        cursor = 14
        rows = []
        valid = True
        while cursor < len(body):
            if len(body) - cursor < 7:
                valid = False
                break
            marker = cursor + 4
            declared = int.from_bytes(body[marker + 1:marker + 3], "little")
            end = marker + 3 + declared
            if end > len(body):
                valid = False
                break
            rows.append((body[marker], marker, body[cursor:end]))
            cursor = end
        if valid and rows and cursor == len(body):
            return tuple(rows)
    return ()


def packet_may_contain_player_root(payload: bytes, direction: str) -> bool:
    """Cheaply walk only encrypted top-level headers for kind 9/56 records.

    Root object messages are only a small fraction of server traffic, but
    their outer message sizes vary with bundled state records.  A length or
    signature whitelist therefore loses valid objects.  This header-only walk
    avoids decrypting record bodies while still selecting every structurally
    complete A90370 candidate for the strict parser above.
    """
    if (
        direction != "server_to_client"
        or len(payload) < 21
        or int.from_bytes(payload[7:9], "little") + 3 != len(payload)
    ):
        return False
    preferred = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(payload[5])
    phases = (
        (preferred,)
        if preferred is not None
        else tuple(range(len(_V576_BODY_XOR_KEY)))
    )
    if preferred is not None:
        phases += tuple(
            phase for phase in range(len(_V576_BODY_XOR_KEY))
            if phase != preferred
        )

    def decoded_byte(offset: int, phase: int) -> int:
        if offset < 9:
            return payload[offset]
        return payload[offset] ^ _V576_BODY_XOR_KEY[
            (offset - phase) % len(_V576_BODY_XOR_KEY)
        ]

    for phase in phases:
        cursor = 14
        while cursor < len(payload):
            if len(payload) - cursor < 7:
                break
            marker = cursor + 4
            kind = decoded_byte(marker, phase)
            declared = decoded_byte(marker + 1, phase) | (
                decoded_byte(marker + 2, phase) << 8
            )
            end = marker + 3 + declared
            if end > len(payload):
                break
            if kind in (9, 56):
                return True
            cursor = end
        if cursor == len(payload):
            return False
    return False


@dataclass(frozen=True, slots=True)
class _PlayerRootRecord:
    object_id: int
    reference_id: int
    config_row: int
    action_index: int
    signal_key: int
    reference_branch: int = 0
    parent_object_id: int = 0


def _parse_player_root(record: bytes) -> _PlayerRootRecord | None:
    """Return the exact object and ACT coordinates of an A90370 root.

    The parser consumes all variable branches instead of looking for byte
    patterns.  This prevents an equipment/item branch from being mistaken for
    a player-created damage object.
    """
    if (
        len(record) < 7
        or record[4] not in (9, 56)
        or int.from_bytes(record[5:7], "little") + 7 != len(record)
    ):
        return None
    body = record[7:]
    at = 0

    def take(count: int) -> bytes:
        nonlocal at
        if count < 0 or at + count > len(body):
            raise ValueError("truncated A90370 field")
        value = body[at:at + count]
        at += count
        return value

    try:
        take(4)  # opaque seed
        equipment_branch = take(1)[0]
        if equipment_branch not in (0, 1):
            return None
        object_id = int.from_bytes(take(4), "little")
        config_row = int.from_bytes(take(4), "little") if equipment_branch == 0 else 0
        for size in (9, 6, 6, 6):
            take(size)
        reference_branch = _signed(take(1))
        if reference_branch not in (0, 1):
            return None
        reference_id = int.from_bytes(take(4), "little")
        parent_object_id = 0
        if reference_branch == 1:
            uid_branch = take(1)[0]
            if uid_branch not in (0, 1):
                return None
            reference = take(8 if uid_branch else 4)
            if uid_branch == 0:
                parent_object_id = int.from_bytes(reference, "little")
        action_index = _signed(take(2))
        signal_key = _signed(take(1))
        take(1)  # inline definition branch
    except ValueError:
        return None
    return _PlayerRootRecord(
        object_id,
        reference_id,
        config_row,
        action_index,
        signal_key,
        reference_branch,
        parent_object_id,
    )


@dataclass(frozen=True, slots=True)
class _Generation:
    generation: int
    timestamp: float
    packet_serial: int
    record_offset: int
    skill_id: int
    evidence: str = "action-spawn"
    config_row: int = 0
    action_index: int = -1
    signal_key: int = -1
    parent_object_id: int = 0
    parent_generation: int = 0
    full_action_birth_timestamp: float | None = None
    full_action_scene_epoch: int = 0
    full_action_eligible: bool = True


@dataclass(frozen=True, slots=True)
class _ClientGeneration:
    timestamp: float
    packet_serial: int
    record_offset: int
    config_row: int
    action_index: int
    signal_key: int
    player_act_branch: bool
    fingerprint: str


def _at_or_before(
    left_serial: int,
    left_offset: int,
    right_serial: int,
    right_offset: int,
) -> bool:
    return left_serial < right_serial or (
        left_serial == right_serial and left_offset <= right_offset
    )


class PlayerRootActionObjectLedger:
    """Mirror the client skill copied into a newly born player damage object.

    The assignment is frozen on ``owner + object id + generation``.  Skill
    naming uses either the concrete config row or the exact action/signal
    coordinates embedded in that same birth record; cast time is never used.
    """

    def __init__(self) -> None:
        self._generation_counts: dict[tuple[int, int], int] = {}
        self._generations: dict[tuple[int, int], list[_Generation]] = {}
        self._latest_timestamp = 0.0
        # Expiration used to scan every live owner/object bucket after every
        # root-object packet.  Large captures contain thousands of buckets,
        # turning that maintenance into quadratic work.  The heap lets us
        # visit only generations whose ten-minute retention window elapsed.
        self._expiry_heap: list[tuple[float, int, int, int]] = []
        # C2S is intentionally separate from the established S2C policy.
        # Retain its last birth, including unknown/UID births, for the scene;
        # dropping expired rows would let a retransmission revive an object.
        self._client_generations: dict[tuple[int, int], _ClientGeneration] = {}
        self._client_seen_registrations: set[tuple[int, int, str]] = set()
        self._client_character_name = ''
        self._object_scene_serial = 0
        self._full_action_scene_epoch = 0
        self._full_action_first_births: dict[tuple[int, int, bytes], float] = {}
        self._full_action_latest_records: dict[tuple[int, int], bytes] = {}

    def reset_scene(self) -> None:
        """Retire every object proof at an authoritative scene boundary.

        Config-only roots and reviewed legacy ACT tuples are scene-scoped
        too. Resetting only the newer client/full-ACT policies leaves those
        older generations able to name a later scene's reused runtime UID.
        Clear their registration and replay state together. The scene suffix
        keeps new generation counters distinct from historical evidence IDs.
        """
        self._object_scene_serial += 1
        self._generation_counts.clear()
        self._generations.clear()
        self._latest_timestamp = 0.0
        self._expiry_heap.clear()
        self.reset_client()
        self._client_character_name = ''
        self.reset_full_action_scene()

    def _scene_evidence_suffix(self) -> str:
        return f":scene:{self._object_scene_serial}" if self._object_scene_serial else ""

    def reset_full_action_scene(self) -> None:
        """Retire only the new full-ACT S2C policy at scene teardown."""
        self._full_action_scene_epoch += 1
        self._full_action_first_births.clear()
        self._full_action_latest_records.clear()

    def reset_client(self) -> None:
        """Forget local object identities at an authoritative scene boundary."""
        self._client_generations.clear()
        self._client_seen_registrations.clear()

    def invalidate_client_object(self, owner: int, object_id: int) -> None:
        """Keep a negative generation barrier for an independently removed object."""
        key = (int(owner), int(object_id))
        row = self._client_generations.get(key)
        if row is not None:
            self._client_generations[key] = replace(row, player_act_branch=False)

    def observe_client_identity(self, name: str) -> None:
        """Retire local births only when the strict C2S identity name changes."""
        name = str(name or '').strip()
        if not name:
            return
        if self._client_character_name and self._client_character_name != name:
            self.reset_client()
        self._client_character_name = name

    def observe_client_registration(
        self, registration, payload: bytes, timestamp: float, packet_serial: int,
    ) -> bool:
        """Consume one fully guarded C2S registration as an ACT object birth.

        The existing registration reader has checked repeated owner/resource
        fields and the complete wire shape. The owner-prefixed body is the
        same variable reference structure consumed by the S2C root parser;
        neither its UID branch nor its numeric config alone identifies a
        player skill. Even such unidentifiable births supersede older ones.
        """
        owner = int(registration.source_actor_id)
        object_id = int(registration.damage_object_id)
        if owner < _PLAYER_ID_MIN or not 0 < object_id <= _OBJECT_ID_MAX:
            return False
        key = (owner, object_id)
        fingerprint = str(registration.registration_key)
        registration_identity = (owner, object_id, fingerprint)
        if registration_identity in self._client_seen_registrations:
            return False  # retransmission cannot refresh the original born_at
        self._client_seen_registrations.add(registration_identity)
        decoded = _decode_v576_body(payload, int(registration.xor_phase))
        owner_offset = int(registration.record_offset) + 1
        root = None
        if registration.layout.startswith("c2s-compound-"):
            # Every object owns its complete TLV. A packet can contain roots
            # and children from independent actions; no first-record shortcut.
            marker = int(registration.record_offset)
            start = marker - 4
            if (start >= 14 and marker + 3 <= len(decoded)
                    and decoded[marker] in (9, 56)
                    and int.from_bytes(decoded[start:marker], 'little') == owner):
                record_end = marker + 3 + int.from_bytes(decoded[marker + 1:marker + 3], 'little')
                if marker + 3 < record_end <= len(decoded):
                    root = _parse_player_root(decoded[start:record_end])
        elif (0 <= owner_offset <= len(decoded) - 4
                and int.from_bytes(decoded[owner_offset:owner_offset + 4], 'little') == owner):
            body = decoded[owner_offset + 4:]
            record = owner.to_bytes(4, 'little') + b'\x09' + len(body).to_bytes(2, 'little') + body
            root = _parse_player_root(record)
        valid = bool(root is not None and root.object_id == object_id
                     and root.reference_id == owner and root.reference_branch == 0
                     and root.config_row == int(registration.projectile_template_id))
        self._client_generations[key] = _ClientGeneration(
            float(timestamp), int(packet_serial), int(registration.record_offset),
            root.config_row if valid else 0,
            root.action_index if valid else -1,
            root.signal_key if valid else -1,
            valid, fingerprint,
        )
        return True

    def annotate_client(
        self, event: CombatEvent, *, timestamp: float, packet_serial: int,
        job_conflicts, skill_catalog,
    ) -> CombatEvent:
        """Name a local Hit using a current complete ACT registration tuple."""
        if (event.healing or event.amount <= 0 or event.has_real_hit_evidence is not True
                or event.wire_source_actor_id < _PLAYER_ID_MIN
                or not 0 < event.wire_target_actor_id < _PLAYER_ID_MIN
                or not 0 < event.wire_damage_object_id <= _OBJECT_ID_MAX
                or is_deterministic_attribution(event.attribution_method)):
            return event
        key = (event.wire_source_actor_id, event.wire_damage_object_id)
        row = self._client_generations.get(key)
        if row is None or not row.player_act_branch or not _at_or_before(
            row.packet_serial, row.record_offset, int(packet_serial), event.wire_record_offset,
        ):
            return event
        skill_id = _full_action_signal_skill(
            (event.job, row.action_index, row.signal_key, row.config_row),
        )
        mechanism = _WISP_TRANSFORM_ROOTS.get((row.action_index, row.signal_key, row.config_row))
        kali_fan_basic_attack = (event.job, row.action_index, row.signal_key, row.config_row) in _KALI_FAN_BASIC_ATTACK_KEYS
        lifetime = (mechanism[2] if mechanism else
                    0.0 if kali_fan_basic_attack else
                    _CLIENT_HIT_IDENTITY_LIFETIMES.get((row.config_row, skill_id), 0.0))
        age = float(timestamp) - row.timestamp
        # The post-admitted server fan route has no reviewed finite lifetime.
        # Use that same identity contract here: a full exact job/ACT tuple,
        # first birth, nonnegative age, and current generation/scene barriers.
        # Never borrow another profession's lifetime for shared config5500.
        if age < 0 or (not kali_fan_basic_attack and (
                (skill_id <= 0 and mechanism is None) or lifetime <= 0 or age > lifetime)):
            return event
        # A newer server root, including an unresolved root, is a generation
        # barrier too. Never reuse a client birth across another object birth.
        if any(_at_or_before(r.packet_serial, r.record_offset, int(packet_serial), event.wire_record_offset)
               and (r.packet_serial, r.record_offset) > (row.packet_serial, row.record_offset)
               for r in self._generations.get(key, ())):
            return event
        if mechanism:
            skill_id_text, skill_name = mechanism[:2]
        elif kali_fan_basic_attack:
            skill_id_text, skill_name = _KALI_FAN_BASIC_ATTACK_ID, _KALI_FAN_BASIC_ATTACK_NAME
        else:
            if job_conflicts(event.wire_source_actor_id, skill_id):
                return event
            catalog = skill_catalog.get(skill_id)
            if not isinstance(catalog, (tuple, list)) or not catalog or not str(catalog[0]).strip():
                return event
            skill_id_text, skill_name = str(skill_id), str(catalog[0])
        return replace(
            event, skill_id=skill_id_text, skill_name=skill_name,
            attribution_method='player_root_action_signal_object',
            attribution_confidence=1.0,
            cast_instance_id=(f'damage-object:c2s-act:{event.wire_source_actor_id:08X}:'
                              f'{event.wire_damage_object_id}:{row.packet_serial}:'
                              f'{row.action_index}:{row.signal_key}:{row.config_row}'
                              f'{self._scene_evidence_suffix()}'),
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=event.raw_attribution_method or event.attribution_method,
        )

    def may_observe_object_birth(self, timestamp: float) -> bool:
        """Compatibility hook; root records are selected by their own header.

        Older code used recent action time to decide whether another packet
        might contain an object birth.  The header walker now selects every
        kind 9/56 record directly, so timing is not part of attribution or of
        packet admission.
        """
        return False

    def observe_packet(
        self,
        payload: bytes,
        direction: str,
        timestamp: float,
        packet_serial: int,
    ) -> None:
        records = _typed_records(payload, direction)
        if not records:
            return
        self._latest_timestamp = max(self._latest_timestamp, float(timestamp))
        for kind, offset, record in records:
            if len(record) < 7:
                continue
            owner = int.from_bytes(record[:4], "little")
            if kind not in (9, 56) or owner < _PLAYER_ID_MIN:
                continue
            parsed = _parse_player_root(record)
            if parsed is None:
                continue
            object_id = parsed.object_id
            reference_id = parsed.reference_id
            config_row = parsed.config_row
            if not (0 < object_id <= _OBJECT_ID_MAX and reference_id == owner):
                continue
            key = (owner, object_id)
            full_identity = (owner, object_id, record)
            seen_full_birth = full_identity in self._full_action_first_births
            full_eligible = (not seen_full_birth or self._full_action_latest_records.get(key) == record)
            full_born_at = self._full_action_first_births.setdefault(full_identity, float(timestamp))
            if full_eligible:
                self._full_action_latest_records[key] = record
            generation = self._generation_counts.get(key, 0) + 1
            self._generation_counts[key] = generation
            config_skill_id = int(_CONFIG_ROW_SKILLS.get(config_row, 0)) if parsed.reference_branch == 0 else 0
            skill_id = config_skill_id
            evidence = "config" if config_skill_id > 0 else ""
            parent_generation = 0
            if parsed.reference_branch == 1 and 0 < parsed.parent_object_id <= _OBJECT_ID_MAX:
                parent = next((row for row in reversed(self._generations.get(
                    (owner, parsed.parent_object_id), ()
                )) if _at_or_before(row.packet_serial, row.record_offset, packet_serial, offset - 1)), None)
                # The indexed reference is an exact object foreign key. A UID
                # reference is a weapon/item branch and supplies no player ACT
                # identity. Only a current root with a reviewed Projectile ACT
                # child edge and the same unique canonical family can qualify.
                if (parent is not None and parent.evidence == "config"
                        and config_row in _PROJECTILE_CHILD_EDGES.get(parent.config_row, ())
                        and _CONFIG_ROW_SKILLS.get(config_row) == parent.skill_id):
                    skill_id, evidence = parent.skill_id, "parent-config"
                    parent_generation = parent.generation
            row = _Generation(
                generation,
                float(timestamp),
                int(packet_serial),
                offset,
                skill_id,
                evidence,
                config_row,
                parsed.action_index if parsed.reference_branch == 0 else -1,
                parsed.signal_key if parsed.reference_branch == 0 else -1,
                parsed.parent_object_id,
                parent_generation,
                full_born_at,
                self._full_action_scene_epoch,
                full_eligible,
            )
            bucket = self._generations.setdefault(key, [])
            bucket.append(row)
            # This is equivalent to the old whole-ledger prune after the
            # packet, but bounds the only bucket that can have grown.
            if len(bucket) > 8:
                del bucket[:-8]
            heapq.heappush(
                self._expiry_heap,
                (row.timestamp + 10 * 60.0, owner, object_id, generation),
            )
        self._prune()

    def _prune(self) -> None:
        cutoff = self._latest_timestamp - 10 * 60.0
        expired_keys: set[tuple[int, int]] = set()
        # The previous implementation retained rows exactly on the ten-minute
        # boundary, hence the strict comparison here rather than <=.
        while self._expiry_heap and self._expiry_heap[0][0] < self._latest_timestamp:
            _, owner, object_id, _generation = heapq.heappop(self._expiry_heap)
            expired_keys.add((owner, object_id))
        for key in expired_keys:
            rows = self._generations.get(key)
            if not rows:
                continue
            retained = [row for row in rows if row.timestamp >= cutoff]
            if retained:
                self._generations[key] = retained
            else:
                self._generations.pop(key, None)

    def annotate(
        self,
        event: CombatEvent,
        *,
        packet_serial: int,
        timestamp: float | None = None,
        job_conflicts,
        skill_catalog,
        require_current_lifetime: bool = False,
    ) -> CombatEvent:
        if (
            event.healing
            or event.amount <= 0
            or event.has_real_hit_evidence is not True
            or event.wire_source_actor_id < _PLAYER_ID_MIN
            or not 0 < event.wire_target_actor_id < _PLAYER_ID_MIN
            or event.wire_damage_object_id <= 0
        ):
            return event
        rows = self._generations.get(
            (event.wire_source_actor_id, event.wire_damage_object_id), ()
        )
        boundary_offset = max(0, int(event.wire_record_offset))
        generation = next(
            (
                row for row in reversed(rows)
                if _at_or_before(
                    row.packet_serial,
                    row.record_offset,
                    int(packet_serial),
                    boundary_offset,
                )
            ),
            None,
        )
        if generation is None:
            return event
        if generation.evidence == "parent-config":
            parent = next((row for row in reversed(self._generations.get(
                (event.wire_source_actor_id, generation.parent_object_id), ()
            )) if _at_or_before(row.packet_serial, row.record_offset, int(packet_serial), boundary_offset)), None)
            if parent is None or parent.generation != generation.parent_generation:
                return event
        action_signal_skill_id = int(
            PLAYER_ROOT_ACTION_SIGNAL_SKILLS.get(
                action_signal_key := (
                    event.job, generation.action_index,
                    generation.signal_key, generation.config_row,
                ),
                0,
            )
        )
        full_action_signal = False
        if (action_signal_skill_id <= 0 and timestamp is not None
                and generation.full_action_eligible
                and generation.full_action_scene_epoch == self._full_action_scene_epoch
                and not is_deterministic_attribution(event.attribution_method)):
            full_skill_id = _full_action_signal_skill(action_signal_key)
            lifetime = _CLIENT_HIT_IDENTITY_LIFETIMES.get((generation.config_row, full_skill_id), 0.0)
            full_born_at = (generation.full_action_birth_timestamp
                            if generation.full_action_birth_timestamp is not None else generation.timestamp)
            full_catalog_row = skill_catalog.get(full_skill_id)
            if (full_skill_id > 0 and lifetime > 0
                    and isinstance(full_catalog_row, (tuple, list)) and full_catalog_row
                    and str(full_catalog_row[0]).strip()
                    and 0 <= float(timestamp) - full_born_at <= lifetime):
                action_signal_skill_id = full_skill_id
                full_action_signal = True
            # No verified cross-packet lifetime has been established for the
            # passive's projectile. Its complete root may prove only a Hit in
            # the exact same compound, after the root's record boundary.
            native_skill_id = _NATIVE_SAME_COMPOUND_ROOT_ACTION_SIGNAL_SKILLS.get(action_signal_key, 0)
            native_catalog = skill_catalog.get(native_skill_id)
            if (action_signal_skill_id <= 0 and native_skill_id > 0
                    and generation.packet_serial == int(packet_serial)
                    and generation.record_offset < boundary_offset
                    and full_born_at == float(timestamp)
                    and isinstance(native_catalog, (tuple, list)) and native_catalog
                    and str(native_catalog[0]).strip()):
                action_signal_skill_id = native_skill_id
                full_action_signal = True
        mechanism = _WISP_TRANSFORM_ROOTS.get((
            generation.action_index, generation.signal_key, generation.config_row,
        ))
        if mechanism is not None and not (
            timestamp is not None and generation.full_action_eligible
            and generation.full_action_scene_epoch == self._full_action_scene_epoch
            and 0 <= float(timestamp) - (
                generation.full_action_birth_timestamp
                if generation.full_action_birth_timestamp is not None else generation.timestamp
            ) <= mechanism[2]
        ):
            mechanism = None
        kali_fan_basic_attack = action_signal_key in _KALI_FAN_BASIC_ATTACK_KEYS
        gravity_skill_plate = action_signal_key in CLERIC_GRAVITY_PLATE_ROOT_KEYS
        if gravity_skill_plate:
            action_signal_skill_id = 80005
        skill_id = action_signal_skill_id or generation.skill_id
        evidence = (
            "action-signal"
            if action_signal_skill_id > 0 or kali_fan_basic_attack or mechanism is not None
            else generation.evidence
        )
        if skill_id <= 0 and not kali_fan_basic_attack and mechanism is None:
            return event
        if mechanism is None:
            # A retained root is not necessarily a live damage object. Only
            # its cache retention window does not extend its audited identity.
            # Fan identity has no dedicated reviewed lifetime and cannot borrow
            # the lifetime of another job sharing its numeric configuration.
            lifetime = (0.0 if kali_fan_basic_attack else
                        _CLIENT_HIT_IDENTITY_LIFETIMES.get((generation.config_row, skill_id), 0.0))
            born_at = (generation.full_action_birth_timestamp
                       if generation.full_action_birth_timestamp is not None else generation.timestamp)
            if require_current_lifetime and (timestamp is None or lifetime <= 0):
                return event
            if (timestamp is not None and lifetime > 0
                    and (require_current_lifetime
                         or (generation.config_row, skill_id) in _REVIEWED_HIT_IDENTITY_KEYS)
                    and not 0 <= float(timestamp) - born_at <= lifetime):
                return event
            if generation.evidence == "parent-config" and timestamp is not None:
                parent_lifetime = (_CLIENT_OBJECT_LIFETIMES if require_current_lifetime
                                   else _CLIENT_HIT_IDENTITY_LIFETIMES).get((parent.config_row, skill_id), 0.0)
                if (not require_current_lifetime
                        and (parent.config_row, skill_id) not in _REVIEWED_HIT_IDENTITY_KEYS):
                    parent_lifetime = 0.0
                parent_born_at = (parent.full_action_birth_timestamp
                                 if parent.full_action_birth_timestamp is not None else parent.timestamp)
                if ((require_current_lifetime and parent_lifetime <= 0)
                        or (parent_lifetime > 0
                            and not 0 <= float(timestamp) - parent_born_at <= parent_lifetime)):
                    return event
        candidate_method = (
            "player_child_config_object"
            if evidence == "parent-config"
            else
            "player_root_action_signal_object"
            if evidence == "action-signal"
            else "player_root_config_object"
            if evidence == "config"
            else "player_root_action_object"
        )
        if is_deterministic_attribution(event.attribution_method) and not (
            evidence == "action-signal"
            and should_replace_attribution(event.attribution_method, candidate_method)
        ):
            return event
        if (evidence == "config" and event.job not in _SAFE_CONFIG_JOBS
                and generation.config_row not in SKILL_PLATE_PROJECTILE_SKILLS):
            # The old guard disabled every config-backed object for seven jobs
            # merely because a few of their child Projectile rows are shared.
            # Keep those demonstrated shared rows behind their parent-lineage
            # gate, but retain the exact owner+object+generation evidence for
            # all other uniquely mapped config rows.
            if (
                generation.config_row <= 0
                or generation.config_row
                in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
            ):
                return event
        if (gravity_skill_plate or
                SKILL_PLATE_PROJECTILE_SKILLS.get(generation.config_row) == skill_id):
            skill_id_text = str(skill_id)
            skill_name = SKILL_PLATE_DAMAGE_NAMES[skill_id]
        elif mechanism is not None:
            skill_id_text, skill_name = mechanism[:2]
        elif kali_fan_basic_attack:
            # This identity comes from the base-class ACT action itself, not a
            # learned numeric skill row, so it deliberately bypasses the
            # numeric job/skill catalog check.  The exact job tuple above is
            # the fail-closed class guard.
            skill_id_text = _KALI_FAN_BASIC_ATTACK_ID
            skill_name = _KALI_FAN_BASIC_ATTACK_NAME
        else:
            if job_conflicts(event.wire_source_actor_id, skill_id):
                return event
            catalog_row = skill_catalog.get(skill_id)
            skill_id_text = str(skill_id)
            skill_name = (
                str(catalog_row[0])
                if isinstance(catalog_row, (tuple, list)) and catalog_row
                else skill_id_text
            )
        return replace(
            event,
            skill_id=skill_id_text,
            skill_name=skill_name,
            attribution_method=candidate_method,
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=(
                event.raw_attribution_method or event.attribution_method
            ),
            cast_instance_id=(
                f"damage-object:s2c-act:{event.wire_source_actor_id:08X}:"
                f"{event.wire_damage_object_id}:{generation.generation}"
                if full_action_signal else
                f"player-child-object:{event.wire_source_actor_id:08X}:"
                f"{event.wire_damage_object_id}:{generation.generation}:"
                f"{generation.parent_object_id}:{generation.parent_generation}"
                if evidence == "parent-config" else
                f"player-root-object:{event.wire_source_actor_id:08X}:"
                f"{event.wire_damage_object_id}:{generation.generation}"
            ) + self._scene_evidence_suffix(),
            attribution_confidence=1.0,
        )
