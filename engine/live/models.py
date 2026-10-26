from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CombatEvent:
    timestamp_ms: int
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    amount: int
    critical: bool = False
    healing: bool = False
    target_id: str = ""
    target_name: str = ""
    attribution_method: str = "recorded"
    target_job: str = ""
    target_max_hp: int = 0
    raw_skill_id: str = ""
    raw_skill_name: str = ""
    raw_attribution_method: str = ""
    cast_instance_id: str = ""
    attribution_confidence: float = 0.0
    # Additional/status damage can name the effect that produced the damage
    # while a different, ordinary Hit triggered that effect.  Keep both
    # identities: ``skill_*`` remains the damage owner (for example
    # 奉献乌鸦), and ``trigger_skill_*`` records the factual attack that
    # caused this particular proc. Empty values mean the trigger is unproven.
    trigger_skill_id: str = ""
    trigger_skill_name: str = ""
    trigger_attribution_method: str = ""
    trigger_attribution_confidence: float = 0.0
    # True only when the decoded wire record proves one game-facing hit.
    # Valid damage such as DoT ticks and mastery/additional-damage procs remains
    # in the damage ledger with this flag false. A direct-hit overflow record
    # keeps this flag true because its wire packet still proves a real hit.
    # None is used only by legacy serialized/synthetic events that predate
    # this evidence bit. Current packet decoders always provide True/False.
    has_real_hit_evidence: bool | None = None
    # Legacy prefix value, retained for diagnostics compatibility. Native
    # review identifies the prefix as random-state input, not a hit-batch ID;
    # it must never link separate messages or object generations.
    wire_result_group_id: int = 0
    # Populated only for a completely validated physical UDP datagram.
    # Bounds sibling-result checks to that message; it is not a cast key.
    wire_packet_sha256: str = ""
    wire_damage_object_id: int = 0
    # Preserve the actual emitting summon separately from the player's ID.
    wire_entity_id: int = 0
    entity_template_id: int = 0
    entity_owner_evidence_id: str = ""
    entity_monster_skill_id: int = 0
    entity_attack_skill_id: int = 0
    entity_attack_evidence_id: str = ""
    # Preserve the decoder-selected source/target BEFORE display-ID, owner or
    # recent-target remapping. This is evidence, not another damage event.
    # Layout+offset refer to this packet only; never dedup by these fields alone.
    wire_source_actor_id: int = 0
    wire_target_actor_id: int = 0
    wire_alternate_target_actor_id: int = 0
    wire_record_offset: int = 0
    wire_damage_layout: str = ""
    wire_critical: bool | None = None
    # Raw evidence from the status-damage family.  ``wire_status_skill_id`` is
    # populated from the fixed trailing field but is authoritative only when
    # it exists in a reviewed data catalog (for example equipment suffixes).
    wire_status_skill_id: int = 0
    wire_status_tail_hex: str = ""
    # Immutable display provenance captured BEFORE a proven Burning Hand
    # result replaces its already-decoded skill identity. Unlike raw_skill_*,
    # this is not the packet's inherited candidate/last-action slot. The
    # confirmation is decided once at split time, not guessed again on merge.
    burning_hand_origin_skill_id: str = ""
    burning_hand_origin_skill_name: str = ""
    burning_hand_origin_attribution_method: str = ""
    burning_hand_origin_confirmed: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CombatEvent":
        return cls(
            timestamp_ms=max(0, int(value.get("timestamp_ms", 0))),
            source_id=str(value.get("source_id", "")).strip(),
            source_name=str(value.get("source_name", "")).strip(),
            job=str(value.get("job", "")).strip(),
            skill_id=str(value.get("skill_id", "")).strip(),
            skill_name=str(value.get("skill_name", "")).strip(),
            amount=max(0, int(value.get("amount", 0))),
            critical=bool(value.get("critical", False)),
            healing=bool(value.get("healing", False)),
            target_id=str(value.get("target_id", "")).strip(),
            target_name=str(value.get("target_name", "")).strip(),
            attribution_method=str(
                value.get("attribution_method", "recorded")
            ).strip()
            or "recorded",
            target_job=str(value.get("target_job", "")).strip(),
            target_max_hp=max(0, int(value.get("target_max_hp", 0))),
            raw_skill_id=str(value.get("raw_skill_id", "")).strip(),
            raw_skill_name=str(value.get("raw_skill_name", "")).strip(),
            raw_attribution_method=str(
                value.get("raw_attribution_method", "")
            ).strip(),
            cast_instance_id=str(value.get("cast_instance_id", "")).strip(),
            attribution_confidence=max(
                0.0,
                min(1.0, float(value.get("attribution_confidence", 0.0) or 0.0)),
            ),
            trigger_skill_id=str(value.get("trigger_skill_id", "")).strip(),
            trigger_skill_name=str(value.get("trigger_skill_name", "")).strip(),
            trigger_attribution_method=str(
                value.get("trigger_attribution_method", "")
            ).strip(),
            trigger_attribution_confidence=max(
                0.0,
                min(
                    1.0,
                    float(
                        value.get("trigger_attribution_confidence", 0.0) or 0.0
                    ),
                ),
            ),
            has_real_hit_evidence=(
                None
                if value.get("has_real_hit_evidence") is None
                else bool(value.get("has_real_hit_evidence"))
            ),
            wire_result_group_id=max(
                0, int(value.get("wire_result_group_id", 0) or 0)
            ),
            wire_packet_sha256=str(value.get("wire_packet_sha256", "") or ""),
            wire_damage_object_id=max(
                0, int(value.get("wire_damage_object_id", 0) or 0)
            ),
            wire_entity_id=max(0, int(value.get("wire_entity_id", 0) or 0)),
            entity_template_id=max(0, int(value.get("entity_template_id", 0) or 0)),
            entity_owner_evidence_id=str(value.get("entity_owner_evidence_id", "") or ""),
            entity_monster_skill_id=max(0, int(value.get("entity_monster_skill_id", 0) or 0)),
            entity_attack_skill_id=max(0, int(value.get("entity_attack_skill_id", 0) or 0)),
            entity_attack_evidence_id=str(value.get("entity_attack_evidence_id", "") or ""),
            wire_source_actor_id=max(0, int(value.get("wire_source_actor_id", 0) or 0)),
            wire_target_actor_id=max(0, int(value.get("wire_target_actor_id", 0) or 0)),
            wire_alternate_target_actor_id=max(
                0, int(value.get("wire_alternate_target_actor_id", 0) or 0)
            ),
            wire_record_offset=max(0, int(value.get("wire_record_offset", 0) or 0)),
            wire_damage_layout=str(value.get("wire_damage_layout", "") or ""),
            wire_critical=(None if value.get("wire_critical") is None
                           else bool(value["wire_critical"])),
            wire_status_skill_id=max(
                0, int(value.get("wire_status_skill_id", 0) or 0)
            ),
            wire_status_tail_hex=str(value.get("wire_status_tail_hex", "") or ""),
            burning_hand_origin_skill_id=str(
                value.get("burning_hand_origin_skill_id", "") or ""
            ).strip(),
            burning_hand_origin_skill_name=str(
                value.get("burning_hand_origin_skill_name", "") or ""
            ).strip(),
            burning_hand_origin_attribution_method=str(
                value.get("burning_hand_origin_attribution_method", "") or ""
            ).strip(),
            burning_hand_origin_confirmed=bool(
                value.get("burning_hand_origin_confirmed", False)
            ),
        )


@dataclass(frozen=True, slots=True)
class SkillCastEvent:
    timestamp_ms: int
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    packet_kind: str = "skill_cast"
    display_name: str = ""
    variant_action_id: int = 0
    variant_evidence: str = ""
    variant_key: str = ""


@dataclass(frozen=True, slots=True)
class SkillParameterEvent:
    """Raw server skill-response evidence retained for combat-only inference.

    The v576 status response carries the active SkillLevel row, effect class
    and effective parameter.  Extra effect classes can prove damage/action
    glyphs directly; the declared duration and repeated cast timing provide
    independent evidence for duration/cooldown glyphs.  This is deliberately
    not an equipment/profile event: it is produced only by combat traffic.
    """

    timestamp_ms: int
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    target_id: str
    raw_skill_id: str
    skill_level_row_id: int = 0
    effect_class_id: int = 0
    effect_code: str = ""
    parameters: str = ""
    duration_ms: int = 0
    permanent: bool = False
    effect_instance_id: int = 0

    packet_kind: str = "skill_parameter_status"


@dataclass(frozen=True, slots=True)
class SummonRemovalEvent:
    """Authoritative disappearance of one summoned runtime entity.

    Summon-clearing mechanics are not player-Buff clears.  The server applies
    lethal damage to the summon and publishes a signed ``08 6F`` HP state;
    the post-hit value may therefore be below zero on the wire.  Keeping this
    event separate prevents ``1C 1B`` delta notifications or Boss Buff clears
    from prematurely closing summon timers.
    """

    timestamp_ms: int
    summon_id: str
    owner_id: str
    owner_name: str
    owner_job: str
    skill_id: str
    skill_name: str
    previous_hp: int = 0
    raw_current_hp: int = 0
    observed_lethal_damage: int = 0
    packet_kind: str = "summon_lethal_hp_state"
    packet_signature: str = ""
    packet_length: int = 0
    xor_phase: int = 0
    record_offset: int = 0


@dataclass(frozen=True, slots=True)
class BossActionEvent:
    """A protocol-backed action performed by a confirmed Boss entity."""

    timestamp_ms: int
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    template_id: int
    action_code: int
    skill_index: int = 0
    variant_code: int = 0
    action_kind: int = 0
    packet_kind: str = "boss_action"
    packet_signature: str = ""
    packet_length: int = 0
    xor_phase: int = 0
    record_offset: int = 0
    mapped: bool = False
    confidence: float = 1.0
    end_timestamp_ms: int = 0
    evidence_types: tuple[str, ...] = ()
    evidence_count: int = 1
    hit_count: int = 0
    status_count: int = 0
    affected_target_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PartyMemberEvent:
    """Passive party identity evidence observed on the wire.

    ``level`` and ``party_position`` stay zero until a packet layout proves
    them.  Keeping them explicitly unknown is safer than presenting a value
    guessed from a skill or an actor id.
    """

    timestamp_ms: int
    actor_id: str
    name: str
    job: str
    level: int = 0
    party_position: int = 0
    source_layout: str = ""
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class PlayerLifecycleEvent:
    """Protocol-backed change to a party member's visible life state."""

    timestamp_ms: int
    target_id: str
    target_name: str
    target_job: str
    active: bool
    packet_kind: str = "player_lifecycle"
    packet_signature: str = ""
    packet_length: int = 0
    xor_phase: int = 0
    record_offset: int = 0


@dataclass(frozen=True, slots=True)
class PlayerHealthEvent:
    """Authoritative HP state published for a party member.

    Combat ``current_hp`` primarily comes from the absolute ``08 6F 00`` HP
    state. ``21 0C 00`` supplies entry current/maximum resources, while
    ``hp_delta`` retains the signed ``1C 1B 00 04`` notification for source
    attribution. Effective life loss is always differenced from adjacent
    absolute combat states when those records are present.
    """

    timestamp_ms: int
    target_id: str
    target_name: str
    target_job: str
    current_hp: int
    max_hp: int = 0
    current_mp: int = 0
    max_mp: int = 0
    hp_delta: int = 0
    packet_kind: str = "player_health"
    packet_signature: str = ""
    packet_length: int = 0
    xor_phase: int = 0
    record_offset: int = 0
    # A verified 21 0C frame captured before the formal dungeon boundary may
    # be replayed from the dedicated session-evidence appendix.  It is valid
    # for base maximum HP, local identity and reconnect matching, but is not a
    # combat-state point and must never advance or mutate the battle timeline.
    session_evidence: bool = False


@dataclass(frozen=True, slots=True)
class PlayerInspectEvent:
    """A passive response candidate for the in-game information panel.

    The raw response is retained for calibration.  No equipment or panel
    value is populated until its byte offsets have been verified against a
    sample, so an unknown packet cannot corrupt combat statistics.
    """

    timestamp_ms: int
    target_id: str
    target_name: str
    job: str
    level: int = 0
    party_position: int = 0
    status: str = "待样本确认"
    # Opaque, session-local identifier for one explicit in-game inspect
    # request.  Several profile fragments can belong to the same request; the
    # desktop overlay uses this key to honour a user's close action until the
    # next request instead of reopening for every late fragment.
    inspection_key: str = ""
    request_fingerprint: str = ""
    response_layout: str = ""
    fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    equipment: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    cards: tuple[tuple[str, str, str, str, str, str], ...] = field(
        default_factory=tuple
    )
    stats: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    raw_payload_hex: str = ""


@dataclass(frozen=True, slots=True)
class MonsterAppearanceEvent:
    timestamp_ms: int
    target_id: str
    template_id: int


@dataclass(frozen=True, slots=True)
class BossHealthEvent:
    timestamp_ms: int
    target_id: str
    target_name: str
    target_job: str
    target_max_hp: int
    current_hp: int
    layout: str = ""
    part_id: int = 0


@dataclass(frozen=True, slots=True)
class BossSpawnEvent:
    timestamp_ms: int
    target_id: str
    target_name: str
    target_job: str
    target_max_hp: int


@dataclass(frozen=True, slots=True)
class BossStatusEvent:
    timestamp_ms: int
    target_id: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    effect_key: str
    effect_label: str
    value: float | None
    duration_ms: int
    status_kind: str
    effect_code: str = ""
    parameters: str = ""
    evidence_key: str = ""
    stackable: bool = False
    stack_group: str = ""
    operation: str = "apply"
    target_name: str = ""
    target_job: str = ""
    target_scope: str = "boss"
    target_max_hp: int = 0
    permanent: bool = False
    # Hybrid live display provenance. Packet values/timing always win after
    # semantic validation; reviewed data is retained as an explicit fallback.
    value_source: str = ""
    duration_source: str = ""
    # True only for a packet-verified effect that was already active when the
    # formal combat boundary was accepted.  It starts coverage at battle 0s,
    # but is not another cast and must not manufacture cast/CD evidence.
    prebattle_inherited: bool = False

    # Recipient-local UDP lifecycle token. It is not a skill or cast ID.
    effect_instance_id: int = 0
    # Keep same-code aura components (e.g. fire and ice attack) independent.
    effect_class_id: int = 0
    # Preserve the original response when a provisional Boss projection used
    # reviewed/fallback parameters or duration. Late identity correction must
    # recover the packet, not recycle a Boss-specific interpretation.
    raw_parameters: str | None = None
    raw_duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    skill_id: str
    name: str
    damage: int
    healing: int
    hits: int
    critical_hits: int
    share: float
    attribution: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    # Independent, protocol-backed critical rolls. Damage packets without a
    # real hit (proc/DoT/status damage) remain in hits but not in this count.
    critical_samples: int | None = None
    # (trigger skill id, trigger skill name, proc count, proc damage,
    #  attribution method, evidence grade)
    trigger_skills: tuple[tuple[str, str, int, int, str, str], ...] = field(
        default_factory=tuple
    )
    # Release-count evidence is deliberately separate from Hit count.  A
    # release may be backed by a real action/input packet or by a recovered
    # combo/action record; damage-only rows, passives and summons may have no
    # reliable release boundary at all.
    cast_count: int = 0
    direct_cast_count: int = 0
    inferred_cast_count: int = 0
    average_hits: float | None = None


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    source_id: str
    name: str
    job: str
    damage: int
    healing: int
    dps: float
    hits: int
    critical_hits: int
    critical_rate: float
    active_seconds: float
    skills: tuple[SkillSnapshot, ...] = field(default_factory=tuple)
    damage_series: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    incoming_damage: int = 0
    critical_samples: int | None = None


@dataclass(frozen=True, slots=True)
class DamageTargetSnapshot:
    target_id: str
    damage: int
    hits: int
    share: float
    target_name: str = ""
    target_job: str = ""
    target_max_hp: int = 0
    first_ms: int = 0
    last_ms: int = 0


@dataclass(frozen=True, slots=True)
class BossEffectSnapshot:
    key: str
    label: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    value: float | None
    status_kind: str
    effect_code: str
    parameters: str
    data_source: str
    start_seconds: float
    end_seconds: float
    last_refresh_seconds: float
    refresh_count: int
    active: bool
    uptime_seconds: float
    coverage: float
    target_id: str = ""
    target_name: str = ""
    target_job: str = ""
    intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    # ``_effect_snapshots`` clips an open-ended server status to the current
    # snapshot time so that series/coverage calculations remain bounded.  The
    # overlay still needs to know that this was an open-ended interval; without
    # the marker it advances its local clock a few milliseconds past the clip
    # and incorrectly removes a live permanent effect.
    open_ended: bool = False
    value_source: str = ""
    duration_source: str = ""
    stackable: bool = False
    stack_group: str = ""
    effective_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    effective_coverage: float = 0.0
    effective_state: str = ""
    effective_reason: str = ""


@dataclass(frozen=True, slots=True)
class StatusEventSnapshot:
    timestamp_seconds: float
    target_id: str
    target_name: str
    target_job: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    key: str
    label: str
    value: float | None
    status_kind: str
    effect_code: str
    parameters: str
    duration_seconds: float
    end_seconds: float
    active: bool
    operation: str = "apply"
    value_source: str = ""
    duration_source: str = ""
    effective_state: str = ""
    effective_reason: str = ""


@dataclass(frozen=True, slots=True)
class PlayerBuffSeriesSnapshot:
    target_id: str
    target_name: str
    target_job: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    key: str
    label: str
    value: float | None
    status_kind: str
    intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    # Needed by the display projection when one skill also emits finite rows.
    open_ended: bool = False
    value_source: str = ""
    duration_source: str = ""
    # These describe the factual receive ledger, never a cast-based fan-out.
    effect_code: str = ""
    effect_instance_id: int = 0
    state_source: str = ""


@dataclass(frozen=True, slots=True)
class BossHpPointSnapshot:
    timestamp_seconds: float
    current_hp: int | None
    current_percent: float | None
    delta_hp: int
    damage: int
    healing: int
    source: str = ""


@dataclass(frozen=True, slots=True)
class BossHpTrackSnapshot:
    instance_id: str
    target_id: str
    target_name: str
    target_job: str
    max_hp: int
    current_hp: int | None
    current_percent: float | None
    total_damage: int
    total_healing: int
    overkill_damage: int
    first_seconds: float
    last_seconds: float
    confidence: str
    complete: bool
    points: tuple[BossHpPointSnapshot, ...] = field(default_factory=tuple)
    # A server trigger may complete an encounter without carrying another
    # player-damage row.  Preserve that provenance so the UI can close the HP
    # curve without adding the residual to team or player damage totals.
    completion_source: str = ""
    completion_timestamp_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BossEffectViewSnapshot:
    effects: tuple[BossEffectSnapshot, ...] = field(default_factory=tuple)
    summary: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    series: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ] = field(default_factory=tuple)
    events: tuple[StatusEventSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PlayerHealthPointSnapshot:
    timestamp_seconds: float
    current_hp: int
    percent: float | None = None


@dataclass(frozen=True, slots=True)
class PlayerHealthTrackSnapshot:
    target_id: str
    target_name: str
    target_job: str
    current_hp: int
    max_hp: int
    current_percent: float | None
    max_hp_source: str
    exact_percent: bool
    points: tuple[PlayerHealthPointSnapshot, ...] = field(default_factory=tuple)
    state_source: str = ""


@dataclass(frozen=True, slots=True)
class CombatSnapshot:
    elapsed_seconds: float
    total_damage: int
    total_healing: int
    players: tuple[PlayerSnapshot, ...]
    damage_series: tuple[tuple[int, int], ...]
    damage_targets: tuple[DamageTargetSnapshot, ...] = field(default_factory=tuple)
    boss_effects: tuple[BossEffectSnapshot, ...] = field(default_factory=tuple)
    boss_effect_summary: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    boss_effect_series: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ] = field(default_factory=tuple)
    boss_effect_current_seconds: float = 0.0
    boss_effect_events: tuple[StatusEventSnapshot, ...] = field(default_factory=tuple)
    player_buffs: tuple[BossEffectSnapshot, ...] = field(default_factory=tuple)
    player_buff_events: tuple[StatusEventSnapshot, ...] = field(default_factory=tuple)
    player_buff_series: tuple[PlayerBuffSeriesSnapshot, ...] = field(
        default_factory=tuple
    )
    combat_events: tuple[CombatEvent, ...] = field(default_factory=tuple)
    incoming_damage_events: tuple[CombatEvent, ...] = field(default_factory=tuple)
    skill_cast_events: tuple[SkillCastEvent, ...] = field(default_factory=tuple)
    # Complete raw status-response evidence used to infer the skill glyphs
    # actually active for each player in this capture.  It is not rendered as
    # a Buff/debuff and does not alter combat totals by itself.
    skill_parameter_events: tuple[SkillParameterEvent, ...] = field(
        default_factory=tuple
    )
    boss_action_events: tuple[BossActionEvent, ...] = field(default_factory=tuple)
    # Explicit server-backed clear-positive-effects actions.  These are kept
    # separate from ordinary Boss actions because they reset each affected
    # player's finite Buff state and therefore change cast timing and scoring.
    player_buff_clear_events: tuple[BossActionEvent, ...] = field(
        default_factory=tuple
    )
    # Exact summon deaths/removals decoded from signed 08 6F HP states.  This
    # stays separate from player Buff clears: scoring uses it to truncate the
    # remaining periodic damage and the theory planner uses it to free summon
    # capacity for a factual replacement cast.
    summon_removal_events: tuple[SummonRemovalEvent, ...] = field(
        default_factory=tuple
    )
    dps_elapsed_seconds: float = 0.0
    raw_damage_total: int = 0
    attributed_damage_total: int = 0
    unresolved_damage_total: int = 0
    inferred_cast_count: int = 0
    reassigned_hit_count: int = 0
    raw_healing_total: int = 0
    factual_healing_total: int = 0
    attributed_healing_total: int = 0
    unattributed_healing_total: int = 0
    suppressed_overheal_total: int = 0
    healing_matched_event_count: int = 0
    healing_unattributed_event_count: int = 0
    healing_ambiguous_event_count: int = 0
    healing_attribution_methods: tuple[tuple[str, int], ...] = field(
        default_factory=tuple
    )
    boss_hp_tracks: tuple[BossHpTrackSnapshot, ...] = field(default_factory=tuple)
    party_members: tuple[PartyMemberEvent, ...] = field(default_factory=tuple)
    player_profiles: tuple[PlayerInspectEvent, ...] = field(default_factory=tuple)
    player_health_tracks: tuple[PlayerHealthTrackSnapshot, ...] = field(
        default_factory=tuple
    )
    player_lifecycle_events: tuple[PlayerLifecycleEvent, ...] = field(
        default_factory=tuple
    )
    # Live overlays must keep runtime Boss actors separate.  The canonical
    # ``boss_effects`` view deliberately merges replacement actor IDs of the
    # same encounter for scoring/coverage, which is incorrect for a real-time
    # status panel immediately after the server switches Boss IDs.
    boss_runtime_targets: tuple[DamageTargetSnapshot, ...] = field(
        default_factory=tuple
    )
    boss_runtime_effects: tuple[BossEffectSnapshot, ...] = field(
        default_factory=tuple
    )
    # Independent session result. Never used to extend the combat/output clock.
    instance_completion: dict[str, object] = field(default_factory=dict)
