from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
from typing import Mapping, Sequence

from .models import CombatEvent, SkillCastEvent
from .barbarian_combo_damage import infer_combo_damage


# This module is deliberately not imported by the combat ledger. It is an
# audit-only, shadow attribution pass: callers receive a parallel table and
# production CombatEvent rows are never replaced or mutated.

_BEFORE_MS = 120
_AFTER_MS = 5

# Raw status/HP-delta rows are direct audit candidates.  The composition layer
# additionally folds a small, explicitly shared set of supplemental routes
# into its visible extra-damage bucket; those routes are admitted below too,
# but still have to satisfy an effect rule instead of being renamed wholesale.
_STATUS_AUDIT_METHODS = frozenset(
    {"status_damage_packet", "status_damage_skill", "periodic_status_packet"}
)
# These decoder routes are *candidates* for an independent effect result, not
# proof that the record is additional damage.  The same routes also transport
# native skill continuations/projectiles whose packet does not repeat the
# originating skill id.  Keep them in the shadow matcher, but do not put an
# unmatched row into the visible ``额外伤害`` bucket merely because its owner
# came from a bounded cast/same-packet inference.
_EFFECT_CANDIDATE_METHODS = frozenset(
    {
        "supplemental_cast_owner",
        "supplemental_same_packet",
    }
)
_NATIVE_SUPPLEMENTAL_METHODS = _EFFECT_CANDIDATE_METHODS | frozenset(
    {"supplemental_continuation", "direct_hit_overflow"}
)
_PERIODIC_METHODS = frozenset(
    {"periodic_status_packet", "refreshable_pulsing_aura"}
)
_NON_TRIGGER_METHODS = _STATUS_AUDIT_METHODS | frozenset(
    {
        "supplemental_cast_owner",
        "supplemental_continuation",
        "supplemental_same_packet",
        "direct_hit_overflow",
        "packet_entity_status_damage",
        "refreshable_pulsing_aura",
    }
)


@dataclass(frozen=True, slots=True)
class ShadowEffectRule:
    effect_skill_id: str
    effect_name: str
    effect_type: str
    job: str = ""
    fixed_ratio: float = 0.0
    fixed_amounts: tuple[int, ...] = ()
    match_any_amount: bool = False
    stepped_ratio: float = 0.0
    max_steps: int = 0
    requires_critical: bool = False
    # When several effects share the same ratio and only one physical extra
    # row exists, a guaranteed-on-eligible-hit effect must win over a chance
    # proc.  This is rule metadata rather than a skill-id special case, so the
    # same arbitration can be reused by every profession/team effect.
    guaranteed_when_eligible: bool = False
    trigger_skill_ids: tuple[str, ...] = ()
    excluded_trigger_skill_ids: tuple[str, ...] = ()
    activation_skill_ids: tuple[str, ...] = ()
    activation_hit_skill_ids: tuple[str, ...] = ()
    # Some owner-credited target effects are opened only by the owner's hit.
    # Subsequent damage may come from another actor on the same target.
    activation_hit_requires_effect_owner: bool = False
    activation_duration_ms: int = 0
    activation_scope: str = "self"
    # A cast only proves that the effect was attempted. Team effects can be
    # resisted, out of range or already expired for one recipient, so rules
    # with this flag always require that recipient's observed Buff interval.
    # A missing effect channel is uncertainty, not permission to grant the
    # Buff to the whole party from a cast or a coincidental damage ratio.
    requires_observed_activation_state: bool = False
    # Damage emitted by an owned summon/object is credited to its player, but
    # that accounting remap does not make the entity a recipient of the
    # player's Buff.  Recipient-scoped proc Buffs therefore do not inherit to
    # owned entities unless a reviewed effect explicitly opts in.
    activation_inherits_to_owned_entities: bool = False
    wire_layout_contains: str = ""
    minimum_interval_ms: int = 0
    condition_unverified: str = ""
    required_target_status_labels: tuple[str, ...] = ()
    standalone_wire_signature: bool = False
    explicit_only: bool = False
    allow_cross_target: bool = False
    allow_cross_source_same_target: bool = False
    allow_confirmed_cross_source_effect_trigger: bool = False
    effect_source_when_activated: bool = False
    # Most proc damage belongs to the factual Hit which triggered it in the
    # optional composition view.  Owner-side Debuffs such as Bone Crash are
    # different: the damage is credited to the Debuff caster even when a
    # teammate supplied the triggering Hit, so it must stay with the effect
    # skill instead of being moved to the teammate's skill.
    composition_destination: str = "trigger_skill"


@dataclass(frozen=True, slots=True)
class ShadowAttribution:
    event_index: int
    status: str
    confidence: float
    effect_type: str
    effect_skill_id: str
    effect_name: str
    trigger_event_index: int = -1
    trigger_skill_id: str = ""
    trigger_skill_name: str = ""
    trigger_damage: int = 0
    delta_ms: int = 0
    observed_ratio: float = 0.0
    evidence: str = ""
    reason: str = ""
    effect_candidates: tuple[str, ...] = ()
    condition_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActivationIndex:
    global_times: Mapping[str, tuple[int, ...]]
    source_times: Mapping[tuple[str, str], tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class _EffectIntervalIndex:
    channel_provided: bool
    intervals: Mapping[tuple[str, str], tuple[tuple[int, int], ...]]


@dataclass(frozen=True, slots=True)
class _EquipmentSuffixDamageDefinition:
    skill_id: str
    group_id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class _TargetStatusIndex:
    channel_provided: bool
    intervals: Mapping[tuple[str, str], tuple[tuple[float, float], ...]]
    covered_targets: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PreparedEvent:
    rules: tuple[ShadowEffectRule, ...]
    explicit_rules: tuple[ShadowEffectRule, ...]
    eligible: tuple[
        tuple[
            ShadowEffectRule,
            list[tuple[float, int, int, CombatEvent]],
            str,
            bool,
        ],
        ...,
    ]


# Trial rules are enabled only when the client description exposes a numeric
# signature that can be checked independently. Rules whose condition cannot
# yet be observed on the wire remain candidates even after a unique ratio
# match. Skills such as Bone Crash and Shock Transition stay in the inventory
# until their flat-stat/status signatures are proven.
SHADOW_EFFECT_RULES: tuple[ShadowEffectRule, ...] = (
    # Punishing Attack's two damage results are transported in the
    # status-damage family.  Newer subtype1/3/4 layouts carry the authoritative
    # status skill id 303 directly, so the skill id (rather than one historical
    # swordmaster layout name) is the stable discriminator.  Its execute
    # scaling depends on target HP and therefore cannot be reconstructed as a
    # fixed percentage of the first Hit.
    ShadowEffectRule(
        "303", "惩罚攻击追加伤害", "技能追加伤害", job="剑皇",
        match_any_amount=True, explicit_only=True,
        composition_destination="effect_skill",
    ),
    # Water Moon Dance is a skill-local proc, not an unrelated job Buff.  The
    # client description states: critical Water Moon Dance Hits have an 80%
    # chance to deal 18% extra damage.  A synthetic effect id deliberately
    # prevents the decoder's low-confidence recent-cast skill id from being
    # treated as authoritative; the extra row must still pass the critical,
    # same-skill and 18% formula checks.
    ShadowEffectRule(
        "351:extra", "水月神舞额外伤害", "技能追加伤害", job="月之领主",
        fixed_ratio=0.18, requires_critical=True,
        trigger_skill_ids=("351",),
    ),
    ShadowEffectRule(
        "1301", "致命暴击", "被动", job="狙翎", fixed_ratio=0.30,
        requires_critical=True,
    ),
    ShadowEffectRule(
        "503", "连骨撞击附加伤害", "Debuff", job="狂战士",
        fixed_amounts=(7000,),
        # Bone Crash is a target Debuff, not a self/team Buff.  Its successful
        # Hit starts a 15-second window on that exact target, and every party
        # member's factual Hit can trigger a proc credited to the debuff owner.
        activation_hit_skill_ids=("503",),
        activation_duration_ms=15_000,
        allow_cross_source_same_target=True,
        effect_source_when_activated=True,
        composition_destination="effect_skill",
    ),
    ShadowEffectRule(
        "557", "力量掌握", "Buff", job="毁灭者", fixed_ratio=0.10,
        activation_skill_ids=("402", "412", "417"),
        activation_duration_ms=10_000,
    ),
    ShadowEffectRule(
        "2557", "重力掌控者", "被动", job="黑暗女王", fixed_ratio=0.30,
        # In the captured subtype3 stream the result is credited to the
        # Gravity Mastery owner, while its 30% base can be another actor's
        # factual hit. Require a *same-target, owner-cast* gravity hit in the
        # packet window: this is not unrestricted team-wide uptime.
        activation_hit_skill_ids=("2401", "2553"),
        activation_hit_requires_effect_owner=True,
        activation_duration_ms=120,
        allow_cross_source_same_target=True,
        allow_confirmed_cross_source_effect_trigger=True,
        # The same anonymous sentinel is also used by subtype4 rows whose
        # value can be 10% of a native hit. They are not this 30% passive.
        wire_layout_contains="status-damage-subtype3",
    ),
    ShadowEffectRule(
        "3557", "雷暴之息", "被动", job="雷神", fixed_ratio=0.12,
        required_target_status_labels=("触电",),
    ),
    ShadowEffectRule(
        "3551", "冲击流", "被动", job="雷神", explicit_only=True,
        required_target_status_labels=("触电",),
        condition_unverified=(
            "30%概率且按魔法攻击面板结算；未具名状态伤害没有属性面板时保持未归因"
        ),
    ),
    ShadowEffectRule(
        "5307", "复仇之力", "被动", job="黑暗萨满", fixed_ratio=0.10,
        excluded_trigger_skill_ids=("5216",),
    ),
    ShadowEffectRule(
        "5407", "伤痛之力", "被动", job="噬魂者", fixed_ratio=0.10,
        required_target_status_labels=("诅咒状态", "诅咒触发", "幽灵诅咒"),
    ),
    ShadowEffectRule(
        "6009", "突袭", "Buff", fixed_ratio=0.10,
        requires_critical=True,
        guaranteed_when_eligible=True,
        activation_skill_ids=("6009",),
        activation_duration_ms=300_000,
        activation_scope="team",
        requires_observed_activation_state=True,
    ),
    # The packet tail names these two effects authoritatively. Their trigger
    # may be a sibling Boss entity, so the audit searches all targets hit by
    # the same player in the narrow packet window.
    ShadowEffectRule(
        "5118", "九天之舞2阶", "Buff", stepped_ratio=0.009,
        max_steps=50, explicit_only=True, allow_cross_target=True,
    ),
    ShadowEffectRule(
        "6219", "奉献乌鸦", "Buff", stepped_ratio=0.009,
        max_steps=70, explicit_only=True, allow_cross_target=True,
    ),
    # The status tail can name this proc, but its flat/max-attack formula
    # cannot be reconstructed from the triggering Hit damage.
    ShadowEffectRule(
        "501", "连击", "被动", job="狂战士", explicit_only=True,
        composition_destination="effect_skill",
        condition_unverified=(
            "70级数据为70%发动率、2秒冷却，伤害取决于最大物攻；"
            "匿名结果须经同源固定伤害平台与真实攻击交叉验证"
        ),
    ),
    ShadowEffectRule(
        "6401", "天昏地暗", "Debuff", job="影",
        activation_skill_ids=("6401", "6553"),
        activation_duration_ms=12_000,
        wire_layout_contains="status-damage-subtype3",
        standalone_wire_signature=True,
        condition_unverified=(
            "额外30%按普通攻击面板结算，不能用触发Hit比例反推；"
            "接受包内明确技能号，或施法窗口内的subtype3独立固定值记录"
        ),
    ),
)


_EQUIPMENT_SUFFIX_DAMAGE_GROUPS = frozenset(
    {0, 1, 5, 6, 10, 12, 17, 18, 19}
)


@lru_cache(maxsize=1)
def _equipment_suffix_damage_catalog(
) -> dict[str, _EquipmentSuffixDamageDefinition]:
    """Load the 9 damage-producing equipment-suffix families (513 IDs)."""

    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "status_effect_catalog.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    groups = data.get("groups", {}) if isinstance(data, dict) else {}
    statuses = data.get("statuses", {}) if isinstance(data, dict) else {}
    result: dict[str, _EquipmentSuffixDamageDefinition] = {}
    for raw_skill_id, row in statuses.items() if isinstance(statuses, dict) else ():
        if not isinstance(row, dict):
            continue
        try:
            group_id = int(row.get("group"))
        except (TypeError, ValueError):
            continue
        if group_id not in _EQUIPMENT_SUFFIX_DAMAGE_GROUPS:
            continue
        group = groups.get(str(group_id), {}) if isinstance(groups, dict) else {}
        display_name = str(
            group.get("display_name") if isinstance(group, dict) else ""
        ).strip()
        result[str(raw_skill_id)] = _EquipmentSuffixDamageDefinition(
            str(raw_skill_id), group_id, display_name or "装备后缀额外伤害"
        )
    return result


def _build_target_status_index(
    boss_effects: Sequence[object] | None,
    events: Sequence[CombatEvent] = (),
) -> _TargetStatusIndex:
    if boss_effects is None:
        return _TargetStatusIndex(False, {}, frozenset())
    raw: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for effect in boss_effects:
        target_id = str(getattr(effect, "target_id", "") or "")
        labels = {
            str(getattr(effect, "label", "") or "").strip(),
            str(getattr(effect, "key", "") or "").strip(),
        }
        labels.discard("")
        intervals = tuple(getattr(effect, "intervals", ()) or ())
        if not intervals:
            intervals = ((
                float(getattr(effect, "start_seconds", 0.0) or 0.0),
                float(getattr(effect, "end_seconds", 0.0) or 0.0),
            ),)
        normalized = [
            (min(float(start), float(end)), max(float(start), float(end)))
            for start, end in intervals
        ]
        for label in labels:
            raw.setdefault((target_id, label), []).extend(normalized)

    # The generic Boss-effect projection does not expose every damaging status
    # lifecycle.  In particular, v576 often reports only a short internal
    # lightning interval even though the factual Hit has applied a 5--15s
    # electrocution state.  Supplement the packet intervals from the reviewed
    # periodic-status catalog, but only after a real Hit on that exact target;
    # a cast alone is not evidence that the target received the state.
    for event in events:
        if not _is_factual_trigger(event) or not event.target_id:
            continue
        for label, duration_ms in _target_statuses_from_hit(event.skill_id):
            start = event.timestamp_ms / 1000.0
            end = start + duration_ms / 1000.0
            raw.setdefault((event.target_id, label), []).append((start, end))

    merged: dict[tuple[str, str], tuple[tuple[float, float], ...]] = {}
    for key, intervals in raw.items():
        ordered = sorted(intervals)
        compact: list[list[float]] = []
        for start, end in ordered:
            if compact and start <= compact[-1][1]:
                compact[-1][1] = max(compact[-1][1], end)
            else:
                compact.append([start, end])
        merged[key] = tuple((start, end) for start, end in compact)
    return _TargetStatusIndex(
        True,
        merged,
        frozenset(target_id for target_id, _label in merged),
    )


@lru_cache(maxsize=1)
def _periodic_target_status_catalog(
) -> dict[str, tuple[tuple[str, int], ...]]:
    """Return reviewed target-state lifetimes keyed by the hitting skill.

    Multiple table rows can describe level/EX variants of one source skill.
    Without a trustworthy skill-level field in a damage row, the longest
    reviewed PVE lifetime is used.  The subsequent exact damage-ratio check is
    still mandatory, so this index supplies condition evidence rather than a
    damage identity by itself.
    """

    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "periodic_damage_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    skills = data.get("skills", {}) if isinstance(data, dict) else {}
    durations: dict[tuple[str, str], int] = {}
    for rows in skills.values() if isinstance(skills, dict) else ():
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            source_skill_id = str(row.get("source_skill_id") or "").strip()
            label = str(row.get("label") or "").strip()
            if not source_skill_id or not label:
                continue
            if "触电" in label:
                label = "触电"
            elif "诅咒" in label:
                label = "诅咒状态"
            variants = row.get("variants", ())
            maximum = 0
            for variant in variants if isinstance(variants, list) else ():
                if not isinstance(variant, dict):
                    continue
                try:
                    maximum = max(maximum, int(variant.get("duration_ms") or 0))
                except (TypeError, ValueError):
                    continue
            if maximum > 0:
                key = (source_skill_id, label)
                durations[key] = max(durations.get(key, 0), maximum)
    result: dict[str, list[tuple[str, int]]] = {}
    for (skill_id, label), duration_ms in durations.items():
        result.setdefault(skill_id, []).append((label, duration_ms))
    return {
        skill_id: tuple(sorted(rows)) for skill_id, rows in result.items()
    }


def _target_statuses_from_hit(skill_id: str) -> tuple[tuple[str, int], ...]:
    return _periodic_target_status_catalog().get(str(skill_id or "").strip(), ())


def _target_status_condition(
    rule: ShadowEffectRule,
    event: CombatEvent,
    status_index: _TargetStatusIndex,
) -> tuple[bool, str]:
    """Return whether a target-state condition is satisfied and its note.

    ``None`` means that the caller did not provide the Boss state channel, so
    the old audit-only behaviour is retained as a condition candidate.  An
    explicit empty/complete channel means the required state was not observed
    and the rule must not match.
    """

    if not rule.required_target_status_labels:
        return True, rule.condition_unverified
    labels = frozenset(rule.required_target_status_labels)
    if not status_index.channel_provided:
        return True, f"尚未接入Boss状态通道验证：{'/'.join(labels)}"
    # Boss status projection is authoritative only for targets which it
    # actually tracks. Adds and secondary entities commonly have factual Hit
    # and extra-damage rows but no BossEffect row at all. Missing coverage must
    # not be treated as a proved absence of curse/electrocution; the job,
    # exact client ratio and unique factual Hit still have to pass below.
    if event.target_id not in status_index.covered_targets:
        return True, ""
    event_seconds = event.timestamp_ms / 1000.0
    for label in labels:
        intervals = status_index.intervals.get((event.target_id, label), ())
        # Status interval lists are small and merged.  The important saving is
        # avoiding a full scan over every Boss effect for every damage row.
        if any(start <= event_seconds <= end for start, end in intervals):
            return True, ""
    return False, f"Boss状态通道未证明目标当时处于{'/'.join(labels)}"


def _normalized_job(event: CombatEvent, jobs_by_source: Mapping[str, str]) -> str:
    return str(jobs_by_source.get(event.source_id) or event.job or "").strip()


def _is_periodic(event: CombatEvent) -> bool:
    method = str(event.attribution_method or "").strip().casefold()
    skill_id = str(event.skill_id or "").strip().casefold()
    return method in _PERIODIC_METHODS or skill_id.startswith("periodic:")


def is_folded_additional_damage_event(event: CombatEvent) -> bool:
    """Return whether the wire identity itself proves an extra-damage bucket.

    A resolved numeric skill owned through ``supplemental_cast_owner`` or
    ``supplemental_same_packet`` remains native skill damage until an effect
    rule independently proves otherwise.  This distinction prevents every
    profession's delayed/projectile Hits from inflating
    ``额外伤害（未确定）``.
    """

    method = str(event.attribution_method or "").strip().casefold()
    skill_id = str(event.skill_id or "").strip().casefold()
    return bool(
        not event.healing
        and method in _EFFECT_CANDIDATE_METHODS
        and skill_id.startswith("supplemental:")
    )


def is_intrinsic_additional_damage_event(event: CombatEvent) -> bool:
    """Return whether a record is an independent effect-result on the wire.

    This is deliberately narrower than the shadow candidate set.  Status
    damage packets are independent result records; a decoder-owned native
    continuation, recent-cast candidate or direct-hit overflow is not.
    """

    method = str(event.attribution_method or "").strip().casefold()
    skill_id = str(event.skill_id or "").strip().casefold()
    # A subtype3 result with the all-ones status marker has no native skill
    # identity.  ``supplemental_cast_owner`` only supplies a nearby owner;
    # it must not turn this independent status result into another Hit of that
    # owner's last cast.  Other supplemental continuations (notably subtype4
    # native skill segments) do not pass this narrow wire-evidence check.
    anonymous_status_result = bool(
        method == "supplemental_cast_owner"
        and event.has_real_hit_evidence is not True
        and "status-damage-subtype3" in str(event.wire_damage_layout or "")
        and event.wire_status_skill_id == 0xFFFFFFFF
    )
    return bool(
        not event.healing
        and event.amount > 0
        and not _is_periodic(event)
        and (
            method in _STATUS_AUDIT_METHODS
            or skill_id.startswith("supplemental:status_damage")
            or anonymous_status_result
        )
    )


def native_supplemental_skill_identity(event: CombatEvent) -> tuple[str, str]:
    """Return the retained native owner of a supplemental transport row."""

    method = str(event.attribution_method or "").strip().casefold()
    if method not in _NATIVE_SUPPLEMENTAL_METHODS:
        return "", ""
    if method == "direct_hit_overflow":
        skill_id = str(event.raw_skill_id or event.skill_id or "").strip()
        skill_name = str(event.raw_skill_name or event.skill_name or "").strip()
    else:
        skill_id = str(event.skill_id or "").strip()
        skill_name = str(event.skill_name or "").strip()
    if (
        skill_id.casefold() in {"", "-1", "unknown", "unresolved"}
        or skill_id.casefold().startswith(("supplemental:", "periodic:"))
        or skill_name in {"", "待识别伤害", "未学习伤害", "额外伤害"}
    ):
        return "", ""
    return skill_id, skill_name


def is_native_supplemental_damage_event(event: CombatEvent) -> bool:
    """Return whether a formerly folded route still owns a native skill.

    This helper also lets the presentation layer repair snapshots produced by
    older caches, where these rows were already accumulated under the generic
    supplemental bucket before the semantic split was introduced.
    """

    return bool(
        not event.healing
        and event.amount > 0
        and any(native_supplemental_skill_identity(event))
    )


def _is_additional_candidate(event: CombatEvent) -> bool:
    method = str(event.attribution_method or "").strip().casefold()
    skill_id = str(event.skill_id or "").strip().casefold()
    return bool(
        not event.healing
        and event.amount > 0
        and (
            method in _STATUS_AUDIT_METHODS
            or skill_id.startswith("periodic:")
            or method in _EFFECT_CANDIDATE_METHODS
            or is_folded_additional_damage_event(event)
        )
    )


def _is_factual_trigger(event: CombatEvent) -> bool:
    method = str(event.attribution_method or "").strip().casefold()
    native_skill_id, native_skill_name = native_supplemental_skill_identity(event)
    skill_id = str(native_skill_id or event.skill_id or "").strip().casefold()
    skill_name = str(native_skill_name or event.skill_name or "").strip()
    return bool(
        not event.healing
        and event.amount > 0
        and event.has_real_hit_evidence is True
        and (
            method not in _NON_TRIGGER_METHODS
            or bool(native_skill_id and native_skill_name)
        )
        and not skill_id.startswith(("supplemental:", "periodic:"))
        and skill_id not in {"", "-1", "unknown", "unresolved"}
        and skill_name not in {"", "待识别伤害", "未学习伤害", "额外伤害"}
    )


def _is_periodic_damage_trigger(event: CombatEvent) -> bool:
    """Return whether a real periodic damage tick can trigger a proc.

    Periodic damage remains a status-damage row in the composition; this only
    lets a *separate* additional-damage record point back to that factual tick.
    Soul Eater's Pain Power is a common example: a curse tick can immediately
    produce an independent 10% proc even though the curse tick itself must not
    be reclassified as additional damage.
    """

    method = str(event.attribution_method or "").strip().casefold()
    skill_id = str(event.skill_id or "").strip().casefold()
    return bool(
        not event.healing
        and event.amount > 0
        and skill_id.startswith("periodic:")
        and method in {"periodic_status", "periodic_status_packet"}
        and str(event.skill_name or "").strip()
    )


def _is_proc_trigger(event: CombatEvent) -> bool:
    return _is_factual_trigger(event) or _is_periodic_damage_trigger(event)


def _ratio_error(extra: int, direct: int, rule: ShadowEffectRule) -> float | None:
    if rule.match_any_amount:
        return 0.0
    if rule.fixed_amounts:
        return 0.0 if extra in rule.fixed_amounts else None
    if direct <= 0:
        return None
    if rule.fixed_ratio > 0:
        expected = direct * rule.fixed_ratio
        tolerance = max(10.0, extra * 0.001)
        if abs(extra - expected) > tolerance:
            return None
        return abs(extra - expected) / max(1.0, expected)
    if rule.stepped_ratio > 0 and rule.max_steps > 0:
        step = direct * rule.stepped_ratio
        if step <= 0:
            return None
        multiple = max(1, min(rule.max_steps, round(extra / step)))
        expected = step * multiple
        # The server stores integer damage after several percentage stages.
        # A fixed 10-point tolerance rejected otherwise exact large Crow/Nine
        # Heavens rows solely because of integer rounding.  Use the same
        # relative tolerance as fixed-ratio effects while retaining a strict
        # 10-point floor for small hits.
        tolerance = max(10.0, extra * 0.001)
        if abs(extra - expected) > tolerance:
            return None
        return abs(extra - expected) / max(1.0, expected)
    return None


def _critical_proven(event: CombatEvent) -> bool:
    value = event.wire_critical if event.wire_critical is not None else event.critical
    return bool(value)


def _build_activation_index(
    cast_events: Sequence[SkillCastEvent],
) -> _ActivationIndex:
    global_times: dict[str, list[int]] = {}
    source_times: dict[tuple[str, str], list[int]] = {}
    for cast in cast_events:
        skill_id = str(cast.skill_id or "").strip()
        if not skill_id:
            continue
        global_times.setdefault(skill_id, []).append(cast.timestamp_ms)
        source_times.setdefault((skill_id, cast.source_id), []).append(cast.timestamp_ms)
    return _ActivationIndex(
        {key: tuple(sorted(values)) for key, values in global_times.items()},
        {key: tuple(sorted(values)) for key, values in source_times.items()},
    )


def _build_effect_interval_index(
    effect_rows: Sequence[object] | None,
) -> _EffectIntervalIndex:
    if effect_rows is None:
        return _EffectIntervalIndex(False, {})
    raw: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in effect_rows:
        skill_id = str(getattr(row, "skill_id", "") or "").strip()
        target_id = str(getattr(row, "target_id", "") or "").strip()
        if not skill_id or not target_id:
            continue
        intervals = tuple(getattr(row, "intervals", ()) or ())
        if not intervals:
            intervals = ((
                float(getattr(row, "start_seconds", 0.0) or 0.0),
                float(getattr(row, "end_seconds", 0.0) or 0.0),
            ),)
        for start, end in intervals:
            start_ms = max(0, round(min(float(start), float(end)) * 1000))
            end_ms = max(start_ms, round(max(float(start), float(end)) * 1000))
            raw.setdefault((skill_id, target_id), []).append((start_ms, end_ms))

    merged: dict[tuple[str, str], tuple[tuple[int, int], ...]] = {}
    for key, intervals in raw.items():
        compact: list[list[int]] = []
        for start_ms, end_ms in sorted(intervals):
            if compact and start_ms <= compact[-1][1] + 1:
                compact[-1][1] = max(compact[-1][1], end_ms)
            else:
                compact.append([start_ms, end_ms])
        merged[key] = tuple((start, end) for start, end in compact)
    return _EffectIntervalIndex(True, merged)


def _activation_matches(
    rule: ShadowEffectRule,
    event: CombatEvent,
    activation_index: _ActivationIndex,
    effect_interval_index: _EffectIntervalIndex,
) -> bool:
    if not rule.activation_skill_ids:
        return True
    # Team/self Buff responses can predate the first captured cast.  Their
    # target-specific intervals are stronger than a recent-cast guess and are
    # essential for inherited pre-battle states such as Raid (突袭).
    for skill_id in rule.activation_skill_ids:
        if any(
            start_ms <= event.timestamp_ms <= end_ms
            for start_ms, end_ms in effect_interval_index.intervals.get(
                (skill_id, event.source_id), ()
            )
        ):
            return True
    # Recipient-scoped Buffs must never fall back to a team cast.  Apart from
    # resistance/range/expiry, an old or truncated replay may contain no Buff
    # channel at all; treating that absence as full-party uptime used to turn
    # unrelated 10% effects (for example Strength Mastery) into Raid.
    if rule.requires_observed_activation_state:
        return False
    lower = event.timestamp_ms - max(0, rule.activation_duration_ms)
    for skill_id in rule.activation_skill_ids:
        if rule.activation_scope == "self":
            times = activation_index.source_times.get((skill_id, event.source_id), ())
        else:
            times = activation_index.global_times.get(skill_id, ())
        index = bisect_left(times, lower)
        if index < len(times) and times[index] <= event.timestamp_ms:
            return True
    return False


def _candidate_rules(
    event: CombatEvent,
    jobs_by_source: Mapping[str, str],
    activation_index: _ActivationIndex,
    effect_interval_index: _EffectIntervalIndex,
) -> tuple[ShadowEffectRule, ...]:
    skill_id = str(event.skill_id or "").strip()
    method = str(event.attribution_method or "").strip().casefold()
    # These two routes are displayed in the extra-damage bucket, but the
    # decoder has already proved that they are native skill-budget overflow
    # rather than an anonymous Buff/passive/Debuff proc.  They still enter the
    # audit so the UI can label them as unresolved instead of leaving a bare
    # ``额外伤害`` row, but a coincidental percentage must never rename
    # them as an effect.
    if method == "direct_hit_overflow":
        return ()
    explicit = tuple(
        rule
        for rule in SHADOW_EFFECT_RULES
        if skill_id == rule.effect_skill_id
        and (
            not rule.wire_layout_contains
            or rule.wire_layout_contains
            in str(event.wire_damage_layout or "")
        )
    )
    if explicit:
        return explicit
    job = _normalized_job(event, jobs_by_source)
    return tuple(
        rule
        for rule in SHADOW_EFFECT_RULES
        if not rule.explicit_only
        and (not rule.job or rule.job == job)
        and _activation_matches(
            rule, event, activation_index, effect_interval_index
        )
    )


def _candidate_rows(
    rule: ShadowEffectRule,
    event: CombatEvent,
    direct_rows_by_target: Mapping[tuple[str, str], list[tuple[int, int, CombatEvent]]],
    direct_times_by_target: Mapping[tuple[str, str], list[int]],
    direct_rows_by_source: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_by_source: Mapping[str, list[int]],
    direct_rows_for_target: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_for_target: Mapping[str, list[int]],
) -> list[tuple[float, int, int, CombatEvent]]:
    if (
        rule.wire_layout_contains
        and rule.wire_layout_contains not in str(event.wire_damage_layout or "")
    ):
        return []
    if not _target_activation_hit_matches(
        rule, event, direct_rows_for_target, direct_times_for_target
    ):
        return []
    if rule.allow_cross_source_same_target:
        rows = direct_rows_for_target.get(event.target_id, ())
        stamps = direct_times_for_target.get(event.target_id, ())
    elif rule.allow_cross_target:
        rows = direct_rows_by_source.get(event.source_id, ())
        stamps = direct_times_by_source.get(event.source_id, ())
    else:
        key = (event.source_id, event.target_id)
        rows = direct_rows_by_target.get(key, ())
        stamps = direct_times_by_target.get(key, ())
    if not rows:
        return []
    lo = bisect_left(stamps, event.timestamp_ms - _BEFORE_MS)
    hi = bisect_right(stamps, event.timestamp_ms + _AFTER_MS)
    matches: list[tuple[float, int, int, CombatEvent]] = []
    for timestamp_ms, trigger_index, trigger in rows[lo:hi]:
        if (
            rule.activation_skill_ids
            and not rule.activation_inherits_to_owned_entities
            and trigger.wire_entity_id > 0
            and bool(trigger.entity_owner_evidence_id)
        ):
            # Owner attribution and effect eligibility are separate layers.
            # Keep the summon Hit in the owner's damage total, but do not let
            # a Buff observed on the owner manufacture a proc on the entity.
            continue
        if rule.requires_critical and not _critical_proven(trigger):
            continue
        if rule.trigger_skill_ids and trigger.skill_id not in rule.trigger_skill_ids:
            continue
        if trigger.skill_id in rule.excluded_trigger_skill_ids:
            continue
        error = _ratio_error(event.amount, trigger.amount, rule)
        if error is None:
            continue
        matches.append((error, abs(event.timestamp_ms - timestamp_ms), trigger_index, trigger))
    return matches


def _target_activation_hit_matches(
    rule: ShadowEffectRule,
    event: CombatEvent,
    direct_rows_for_target: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_for_target: Mapping[str, list[int]],
) -> bool:
    if not rule.activation_hit_skill_ids:
        return True
    activation_rows = direct_rows_for_target.get(event.target_id, ())
    activation_times = direct_times_for_target.get(event.target_id, ())
    lower = event.timestamp_ms - max(0, rule.activation_duration_ms)
    lo = bisect_left(activation_times, lower)
    hi = bisect_right(activation_times, event.timestamp_ms)
    return any(
        activation.skill_id in rule.activation_hit_skill_ids
        and (
            not rule.activation_hit_requires_effect_owner
            or activation.source_id == event.source_id
        )
        for _timestamp, _index, activation in activation_rows[lo:hi]
    )


def _prepare_event_candidates(
    events: Sequence[CombatEvent],
    jobs_by_source: Mapping[str, str],
    activation_index: _ActivationIndex,
    effect_interval_index: _EffectIntervalIndex,
    direct_rows_by_target: Mapping[tuple[str, str], list[tuple[int, int, CombatEvent]]],
    direct_times_by_target: Mapping[tuple[str, str], list[int]],
    direct_rows_by_source: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_by_source: Mapping[str, list[int]],
    direct_rows_for_target: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_for_target: Mapping[str, list[int]],
    status_index: _TargetStatusIndex,
) -> dict[int, _PreparedEvent]:
    """Resolve expensive cast/status/window lookups once per status row."""

    prepared: dict[int, _PreparedEvent] = {}
    for event_index, event in enumerate(events):
        if not _is_additional_candidate(event) or _is_periodic(event):
            continue
        rules = _candidate_rules(
            event, jobs_by_source, activation_index, effect_interval_index
        )
        explicit_rules = tuple(
            rule
            for rule in rules
            if str(event.skill_id or "").strip() == rule.effect_skill_id
        )
        eligible: list[
            tuple[
                ShadowEffectRule,
                list[tuple[float, int, int, CombatEvent]],
                str,
                bool,
            ]
        ] = []
        for rule in rules:
            condition_ok, condition_note = _target_status_condition(
                rule, event, status_index
            )
            if not condition_ok:
                continue
            matches = _candidate_rows(
                rule, event,
                direct_rows_by_target, direct_times_by_target,
                direct_rows_by_source, direct_times_by_source,
                direct_rows_for_target, direct_times_for_target,
            )
            activation_hit_ok = _target_activation_hit_matches(
                rule, event, direct_rows_for_target, direct_times_for_target
            )
            activated_effect_signature = bool(
                rule.effect_source_when_activated
                and activation_hit_ok
                and (
                    not rule.fixed_amounts
                    or event.amount in rule.fixed_amounts
                )
            )
            standalone = bool(
                activated_effect_signature
                or (
                    rule.standalone_wire_signature
                    and rule.wire_layout_contains
                    and rule.wire_layout_contains in str(event.wire_damage_layout or "")
                )
            )
            eligible.append((rule, matches, condition_note, standalone))
        prepared[event_index] = _PreparedEvent(
            rules=rules,
            explicit_rules=explicit_rules,
            eligible=tuple(eligible),
        )
    return prepared


def _parallel_effect_allocations(
    events: Sequence[CombatEvent],
    prepared: Mapping[int, _PreparedEvent],
) -> dict[
    int,
    tuple[
        ShadowEffectRule,
        list[tuple[float, int, int, CombatEvent]],
        tuple[str, ...],
        bool,
    ],
]:
    """Globally allocate physical extra rows to effect/trigger slots.

    One factual Hit may trigger several independent effects, while every
    physical extra row has capacity one and every ``(effect, trigger)`` slot
    has capacity one.  Grouping and matching the whole local candidate set
    avoids the old first-row-wins behaviour.  Explicit effect IDs are locked
    first; the remaining rows are matched only when all surviving effect slots
    can be covered.  An incomplete set keeps ambiguity, except for the general
    guaranteed-effect-over-chance rule.
    """

    groups: dict[
        tuple[str, str, int],
        list[
            tuple[
                int,
                dict[
                    str,
                    tuple[
                        ShadowEffectRule,
                        list[tuple[float, int, int, CombatEvent]],
                    ],
                ],
            ]
        ],
    ] = {}

    for event_index, item in prepared.items():
        event = events[event_index]
        matched: list[
            tuple[ShadowEffectRule, list[tuple[float, int, int, CombatEvent]]]
        ] = []
        for rule, matches, _condition_note, _standalone in item.eligible:
            if matches:
                matched.append((rule, matches))
        if not matched:
            continue

        # Build one candidate view for *every* factual Hit rather than first
        # binding the physical extra row to the nearest Hit independently for
        # each effect.  In a dense packet the same 3,000 row can look like
        # ``30% of Hit A`` and ``10% of Hit B``.  The old nearest-first gate
        # rejected that row before its 1,000 sibling could prove that Hit A
        # actually emitted two independent effects.  A factual Hit is a
        # reusable trigger anchor; only the physical extra row and the
        # ``(effect, trigger)`` result slot have capacity one.
        resolved_by_trigger: dict[
            int,
            dict[
                str,
                tuple[
                    ShadowEffectRule,
                    list[tuple[float, int, int, CombatEvent]],
                ],
            ],
        ] = {}
        for rule, matches in matched:
            identities = {
                (trigger.skill_id, trigger.skill_name)
                for *_rest, trigger in matches
            }
            if len(identities) != 1:
                # This rule cannot choose between different skills.  Do not
                # discard another independently coherent effect candidate on
                # the same physical row.
                continue
            ordered = sorted(matches, key=lambda item: (item[0], item[1], item[2]))
            for trigger_index in {row[2] for row in ordered}:
                trigger_matches = [
                    row for row in ordered if row[2] == trigger_index
                ]
                resolved_by_trigger.setdefault(trigger_index, {})[
                    rule.effect_skill_id
                ] = (rule, trigger_matches)
        for trigger_index, resolved in resolved_by_trigger.items():
            if not resolved:
                continue
            key = (event.source_id, event.target_id, trigger_index)
            groups.setdefault(key, []).append((event_index, resolved))

    allocations: dict[
        int,
        tuple[
            ShadowEffectRule,
            list[tuple[float, int, int, CombatEvent]],
            tuple[str, ...],
            bool,
        ],
    ] = {}
    # Prefer a coherent multi-effect generation before a coincidental
    # single-effect interpretation on another nearby Hit.  This ordering is
    # generic: it uses only graph capacity and formula/time error, never a
    # profession or skill-id special case.
    def group_priority(item: tuple[tuple[str, str, int], list[tuple[int, dict]]]):
        (_source_id, _target_id, trigger_index), rows = item
        rule_ids = {
            rule_id for _event_index, resolved in rows for rule_id in resolved
        }
        capacity = min(len({index for index, _resolved in rows}), len(rule_ids))
        best_edges = [
            min(matches, key=lambda row: (row[0], row[1], row[2]))
            for _event_index, resolved in rows
            for _rule, matches in resolved.values()
            if matches
        ]
        total_error = sum(edge[0] for edge in best_edges)
        total_distance = sum(edge[1] for edge in best_edges)
        return (
            -(capacity >= 2),
            -capacity,
            -len(rule_ids),
            -len({index for index, _resolved in rows}),
            total_error,
            total_distance,
            trigger_index,
        )

    globally_used_events: set[int] = set()
    globally_used_slots: set[tuple[str, int]] = set()
    for (
        (_source_id, _target_id, trigger_index), rows
    ) in sorted(groups.items(), key=group_priority):
        rows = [
            (event_index, resolved)
            for event_index, resolved in rows
            if event_index not in globally_used_events
        ]
        if not rows:
            continue
        rows = sorted(rows, key=lambda item: item[0])
        rule_ids = tuple(sorted({
            rule_id
            for _index, resolved in rows
            for rule_id in resolved
            if (rule_id, trigger_index) not in globally_used_slots
        }))
        if not rule_ids:
            continue
        candidate_names = tuple(
            f"{next(resolved[rule_id][0] for _index, resolved in rows if rule_id in resolved).effect_skill_id}:"
            f"{next(resolved[rule_id][0] for _index, resolved in rows if rule_id in resolved).effect_name}"
            for rule_id in rule_ids
        )

        assigned_event: dict[int, str] = {}
        assigned_rule: dict[str, int] = {}
        locked_events: set[int] = set()
        locked_rules: set[str] = set()

        # Packet-named effects are authoritative reservations.  They also
        # constrain ambiguous sibling rows in the same trigger generation.
        for event_index, resolved in rows:
            explicit = [
                rule_id
                for rule_id in resolved
                if rule_id in rule_ids
                if str(events[event_index].skill_id or "").strip() == rule_id
            ]
            if len(explicit) != 1 or explicit[0] in locked_rules:
                continue
            rule_id = explicit[0]
            assigned_event[event_index] = rule_id
            assigned_rule[rule_id] = event_index
            locked_events.add(event_index)
            locked_rules.add(rule_id)

        remaining_rows = [
            (event_index, resolved)
            for event_index, resolved in rows
            if event_index not in locked_events
        ]
        remaining_rules = set(rule_ids) - locked_rules

        # Deterministic augmenting-path maximum matching.  Low-degree rows are
        # processed first so a flexible row does not steal the only slot of a
        # constrained sibling.  Locked explicit assignments are immutable.
        trial_event = dict(assigned_event)
        trial_rule = dict(assigned_rule)

        def assign(event_index: int, seen: set[str]) -> bool:
            resolved = next(
                mapping for index, mapping in remaining_rows if index == event_index
            )
            ordered_rules = sorted(
                (rule_id for rule_id in resolved if rule_id in remaining_rules),
                key=lambda rule_id: (
                    not resolved[rule_id][0].guaranteed_when_eligible,
                    resolved[rule_id][1][0][0],
                    resolved[rule_id][1][0][1],
                    rule_id,
                ),
            )
            for rule_id in ordered_rules:
                if rule_id in seen:
                    continue
                seen.add(rule_id)
                owner = trial_rule.get(rule_id)
                if owner is None or (
                    owner not in locked_events and assign(owner, seen)
                ):
                    trial_rule[rule_id] = event_index
                    trial_event[event_index] = rule_id
                    return True
            return False

        for event_index, resolved in sorted(
            remaining_rows,
            key=lambda item: (
                len([rule_id for rule_id in item[1] if rule_id in remaining_rules]),
                item[0],
            ),
        ):
            assign(event_index, set())

        full_cover = all(rule_id in trial_rule for rule_id in remaining_rules)
        guaranteed_fallback_events: set[int] = set()
        if full_cover:
            assigned_event = trial_event
        else:
            # Do not publish an arbitrary partial matching.  Preserve explicit
            # reservations, then apply only the generic guaranteed-over-chance
            # rule where exactly one guaranteed candidate survives.
            assigned_event = {
                event_index: rule_id
                for event_index, rule_id in assigned_event.items()
                if event_index in locked_events
            }
            used_rules = set(locked_rules)
            for event_index, resolved in remaining_rows:
                guaranteed = [
                    rule_id
                    for rule_id, (rule, _matches) in resolved.items()
                    if rule_id not in used_rules and rule.guaranteed_when_eligible
                ]
                if len(guaranteed) == 1:
                    assigned_event[event_index] = guaranteed[0]
                    used_rules.add(guaranteed[0])
                    guaranteed_fallback_events.add(event_index)

        for event_index, rule_id in assigned_event.items():
            resolved = next(mapping for index, mapping in rows if index == event_index)
            rule, matches = resolved[rule_id]
            allocations[event_index] = (
                rule,
                matches,
                candidate_names,
                event_index in guaranteed_fallback_events,
            )
            globally_used_events.add(event_index)
            globally_used_slots.add((rule_id, trigger_index))
    return allocations


def _same_effect_batch_allocations(
    events: Sequence[CombatEvent],
    prepared: Mapping[int, _PreparedEvent],
    existing_allocations: Mapping[
        int,
        tuple[
            ShadowEffectRule,
            list[tuple[float, int, int, CombatEvent]],
            tuple[str, ...],
            bool,
        ],
    ],
) -> dict[
    int,
    tuple[
        ShadowEffectRule,
        list[tuple[float, int, int, CombatEvent]],
        tuple[str, ...],
        bool,
    ],
]:
    """Allocate dense rows of one proven effect to distinct factual Hits.

    A multi-Hit skill can emit several equal damage values inside one 120ms
    window. The former row-by-row pass always selected the nearest factual
    Hit first; after that slot was consumed, later physical rows became mere
    candidates even though the same batch still contained other valid Hits.

    This pass builds the event/trigger bipartite graph for every uniquely
    identified effect and performs maximum one-to-one matching per connected
    batch. It deliberately refuses rows whose candidates contain more than
    one trigger skill identity: matching capacity may resolve duplicate Hits,
    but must never be used to guess between different skills.
    """

    occupied_slots: set[tuple[str, int]] = set()
    for rule, matches, _candidate_names, _fallback in existing_allocations.values():
        if not matches:
            continue
        ordered = sorted(matches, key=lambda item: (item[0], item[1], item[2]))
        occupied_slots.add((rule.effect_skill_id, ordered[0][2]))

    groups: dict[
        tuple[str, str, str],
        dict[
            int,
            tuple[
                ShadowEffectRule,
                dict[int, tuple[float, int, int, CombatEvent]],
            ],
        ],
    ] = {}
    for event_index, item in prepared.items():
        if event_index in existing_allocations:
            continue
        matched = [
            (rule, matches)
            for rule, matches, _condition_note, _standalone in item.eligible
            if matches
        ]
        if len(matched) != 1:
            continue
        rule, matches = matched[0]
        identities = {
            (trigger.skill_id, trigger.skill_name)
            for *_rest, trigger in matches
        }
        if len(identities) != 1:
            continue

        # Overlapping decoder evidence can expose the same factual event more
        # than once. Keep only its best edge.
        edges: dict[int, tuple[float, int, int, CombatEvent]] = {}
        for match in sorted(matches, key=lambda row: (row[0], row[1], row[2])):
            trigger_index = match[2]
            if (rule.effect_skill_id, trigger_index) in occupied_slots:
                continue
            edges.setdefault(trigger_index, match)
        if not edges:
            continue
        event = events[event_index]
        groups.setdefault(
            (event.source_id, event.target_id, rule.effect_skill_id), {}
        )[event_index] = (rule, edges)

    allocations: dict[
        int,
        tuple[
            ShadowEffectRule,
            list[tuple[float, int, int, CombatEvent]],
            tuple[str, ...],
            bool,
        ],
    ] = {}
    for rows in groups.values():
        slot_rows: dict[int, set[int]] = {}
        for event_index, (_rule, edges) in rows.items():
            for trigger_index in edges:
                slot_rows.setdefault(trigger_index, set()).add(event_index)

        # Connected components keep the augmenting paths local to one actual
        # packet/Hit generation and avoid long recursive chains in a fight.
        unseen = set(rows)
        while unseen:
            seed = min(unseen)
            component_events: set[int] = set()
            component_slots: set[int] = set()
            pending_events = [seed]
            while pending_events:
                event_index = pending_events.pop()
                if event_index in component_events:
                    continue
                component_events.add(event_index)
                unseen.discard(event_index)
                for trigger_index in rows[event_index][1]:
                    if trigger_index in component_slots:
                        continue
                    component_slots.add(trigger_index)
                    pending_events.extend(
                        row_index
                        for row_index in slot_rows.get(trigger_index, ())
                        if row_index not in component_events
                    )

            owner_by_slot: dict[int, int] = {}
            slot_by_event: dict[int, int] = {}

            def assign(event_index: int, seen_slots: set[int]) -> bool:
                _rule, edges = rows[event_index]
                for trigger_index, match in sorted(
                    edges.items(),
                    key=lambda item: (
                        item[1][0], item[1][1], item[1][2]
                    ),
                ):
                    if trigger_index in seen_slots:
                        continue
                    seen_slots.add(trigger_index)
                    owner = owner_by_slot.get(trigger_index)
                    if owner is None or assign(owner, seen_slots):
                        owner_by_slot[trigger_index] = event_index
                        slot_by_event[event_index] = trigger_index
                        return True
                return False

            for event_index in sorted(
                component_events,
                key=lambda index: (
                    len(rows[index][1]),
                    events[index].timestamp_ms,
                    index,
                ),
            ):
                assign(event_index, set())

            for event_index, trigger_index in slot_by_event.items():
                rule, edges = rows[event_index]
                allocations[event_index] = (
                    rule,
                    [edges[trigger_index]],
                    (f"{rule.effect_skill_id}:{rule.effect_name}",),
                    False,
                )
                occupied_slots.add((rule.effect_skill_id, trigger_index))
    return allocations


def _equipment_suffix_damage_allocations(
    events: Sequence[CombatEvent],
    skill_parameter_events: Sequence[object],
) -> dict[int, _EquipmentSuffixDamageDefinition]:
    """Identify suffix damage without offering it to profession rules.

    The resource contains nine damage-producing suffix families and 57 level /
    equipment variants per family (513 status IDs).  A direct tail ID is
    authoritative.  Generic FFFFFFFF status-damage rows are accepted only when
    one deduplicated suffix response from the same actor has one uniquely
    nearest extra-damage row in the same packet-time window.
    """

    catalog = _equipment_suffix_damage_catalog()
    if not catalog:
        return {}
    allocations: dict[int, _EquipmentSuffixDamageDefinition] = {}
    for index, event in enumerate(events):
        definition = catalog.get(str(event.wire_status_skill_id or ""))
        if definition is None:
            definition = catalog.get(str(event.skill_id or ""))
        if definition is not None and _is_additional_candidate(event):
            allocations[index] = definition

    # A suffix response does not emit through the generic status-damage lane.
    # The actual result is the following factual direct41 record.  Keep this
    # candidate pool private to suffix matching so ordinary direct Hits are not
    # globally reclassified as extra-damage candidates.
    rows_by_source: dict[str, list[tuple[int, int]]] = {}
    for index, event in enumerate(events):
        if (
            index in allocations
            or event.healing
            or event.amount <= 0
            or event.has_real_hit_evidence is not True
            or "direct41" not in str(event.wire_damage_layout or "").casefold()
        ):
            continue
        rows_by_source.setdefault(event.source_id, []).append(
            (event.timestamp_ms, index)
        )
    times_by_source: dict[str, list[int]] = {}
    for source_id, rows in rows_by_source.items():
        rows.sort()
        times_by_source[source_id] = [timestamp for timestamp, _index in rows]

    response_rows: dict[
        tuple[str, str, int], list[tuple[int, int, _EquipmentSuffixDamageDefinition]]
    ] = {}
    for row in skill_parameter_events:
        raw_skill_id = str(
            getattr(row, "raw_skill_id", "")
            or getattr(row, "skill_id", "")
            or ""
        ).strip()
        definition = catalog.get(raw_skill_id)
        source_id = str(getattr(row, "source_id", "") or "").strip()
        if definition is None or not source_id:
            continue
        timestamp_ms = int(getattr(row, "timestamp_ms", 0) or 0)
        effect_class_id = int(getattr(row, "effect_class_id", 0) or 0)
        effect_instance_id = int(getattr(row, "effect_instance_id", 0) or 0)
        response_rows.setdefault(
            (source_id, raw_skill_id, effect_instance_id), []
        ).append(
            (timestamp_ms, effect_class_id, definition)
        )

    # One activation commonly has complementary class34/class182 responses.
    # They may arrive one millisecond apart, so timestamp equality is not a
    # valid generation boundary.  Cluster the same actor/status within 5ms.
    generations: list[
        tuple[int, str, str, _EquipmentSuffixDamageDefinition]
    ] = []
    for (source_id, raw_skill_id, _effect_instance_id), rows in response_rows.items():
        current_start = -1
        current_end = -1
        current_definition: _EquipmentSuffixDamageDefinition | None = None
        for timestamp_ms, _effect_class_id, definition in sorted(rows):
            if current_start < 0 or timestamp_ms - current_end > 5:
                if current_definition is not None:
                    generations.append(
                        (current_start, source_id, raw_skill_id, current_definition)
                    )
                current_start = timestamp_ms
                current_definition = definition
            current_end = max(current_end, timestamp_ms)
        if current_definition is not None:
            generations.append(
                (current_start, source_id, raw_skill_id, current_definition)
            )

    used_events = set(allocations)
    for timestamp_ms, source_id, _raw_skill_id, definition in sorted(generations):
        rows = rows_by_source.get(source_id, ())
        stamps = times_by_source.get(source_id, ())
        if not rows:
            continue
        lo = bisect_left(stamps, timestamp_ms)
        hi = bisect_right(stamps, timestamp_ms + 150)
        candidates = [
            (event_ms - timestamp_ms, event_index)
            for event_ms, event_index in rows[lo:hi]
            if event_index not in used_events
        ]
        if not candidates:
            continue
        candidates.sort()
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            # The response proves that a suffix generation exists, but two
            # physical direct41 rows are equally near.  Do not steal one native
            # Hit merely because its event index happens to sort first.
            continue
        _distance, event_index = candidates[0]
        allocations[event_index] = definition
        used_events.add(event_index)
    return allocations


def _confirmed_effect_chain_attributions(
    events: Sequence[CombatEvent],
    audited: Sequence[ShadowAttribution],
    jobs_by_source: Mapping[str, str],
    direct_rows_for_target: Mapping[str, list[tuple[int, int, CombatEvent]]],
    direct_times_for_target: Mapping[str, list[int]],
) -> tuple[ShadowAttribution, ...]:
    """Resolve one generation of owner-credited effects from proven extras.

    A status-result can itself be the numerical base of another status-result.
    Only an independently confirmed *other actor's* extra is eligible: this
    avoids recursively using an unknown row, claiming the same owner's own
    result as its trigger, or treating an equipment suffix as a profession
    passive.  The existing owner-side activation and layout constraints still
    apply, and ambiguous matching remains unassigned.
    """

    rules = tuple(
        rule for rule in SHADOW_EFFECT_RULES
        if rule.allow_confirmed_cross_source_effect_trigger
    )
    if not rules:
        return tuple(audited)
    applied = {"当前已正确识别", "影子归因确定", "效果来源已识别"}
    confirmed_by_target: dict[
        str, list[tuple[int, int, CombatEvent, ShadowAttribution]]
    ] = {}
    for audit in audited:
        if audit.status not in applied or not audit.effect_skill_id:
            continue
        if audit.effect_type == "装备后缀额外伤害":
            continue
        event = events[audit.event_index]
        if event.has_real_hit_evidence or event.amount <= 0:
            continue
        confirmed_by_target.setdefault(event.target_id, []).append(
            (event.timestamp_ms, audit.event_index, event, audit)
        )
    times_by_target: dict[str, list[int]] = {}
    for target_id, rows in confirmed_by_target.items():
        rows.sort(key=lambda item: (item[0], item[1]))
        times_by_target[target_id] = [row[0] for row in rows]

    occupied = {
        (audit.effect_skill_id, audit.trigger_event_index)
        for audit in audited
        if audit.effect_skill_id and audit.trigger_event_index >= 0
    }
    repaired: list[ShadowAttribution] = []
    for audit in audited:
        if audit.status != "保留未归因":
            repaired.append(audit)
            continue
        event = events[audit.event_index]
        rows = confirmed_by_target.get(event.target_id, ())
        stamps = times_by_target.get(event.target_id, ())
        candidates = []
        for rule in rules:
            if rule.job and _normalized_job(event, jobs_by_source) != rule.job:
                continue
            if (
                rule.wire_layout_contains
                and rule.wire_layout_contains not in str(event.wire_damage_layout or "")
            ):
                continue
            if not _target_activation_hit_matches(
                rule, event, direct_rows_for_target, direct_times_for_target
            ):
                continue
            lo = bisect_left(stamps, event.timestamp_ms - _BEFORE_MS)
            hi = bisect_right(stamps, event.timestamp_ms + _AFTER_MS)
            for stamp, trigger_index, trigger, trigger_audit in rows[lo:hi]:
                if (
                    trigger.source_id == event.source_id
                    or trigger_audit.effect_skill_id == rule.effect_skill_id
                    or (rule.effect_skill_id, trigger_index) in occupied
                ):
                    continue
                error = _ratio_error(event.amount, trigger.amount, rule)
                if error is not None:
                    candidates.append(
                        (rule, trigger_index, trigger, trigger_audit, error, stamp)
                    )
        if len(candidates) != 1:
            repaired.append(audit)
            continue
        rule, trigger_index, trigger, trigger_audit, error, stamp = candidates[0]
        occupied.add((rule.effect_skill_id, trigger_index))
        repaired.append(replace(
            audit,
            status="影子归因确定",
            confidence=max(0.95, 1.0 - min(0.05, error)),
            effect_type=rule.effect_type,
            effect_skill_id=rule.effect_skill_id,
            effect_name=rule.effect_name,
            trigger_event_index=trigger_index,
            trigger_skill_id=trigger_audit.effect_skill_id,
            trigger_skill_name=trigger_audit.effect_name,
            trigger_damage=trigger.amount,
            delta_ms=event.timestamp_ms - stamp,
            observed_ratio=event.amount / trigger.amount,
            evidence="同目标施放者重力Hit+另一角色已确认追加伤害+30%唯一匹配",
            reason="只追踪一层已确认效果，不递归使用未归因状态记录",
            effect_candidates=(f"{rule.effect_skill_id}:{rule.effect_name}",),
        ))
    return tuple(repaired)


def audit_additional_damage_shadow(
    events: Sequence[CombatEvent],
    jobs_by_source: Mapping[str, str] | None = None,
    cast_events: Sequence[SkillCastEvent] = (),
    boss_effects: Sequence[object] | None = None,
    player_effects: Sequence[object] | None = None,
    skill_parameter_events: Sequence[object] = (),
) -> tuple[ShadowAttribution, ...]:
    """Return a parallel Buff/passive/Debuff attribution audit.

    Status/HP-delta rows and every row folded into the production
    ``额外伤害`` composition bucket are audited. A result is definite only
    when one effect rule and one trigger identity survive packet, activation,
    critical state and ratio checks, or when parallel independent effects
    have enough distinct wire rows to be allocated one-to-one. Unobservable
    conditions remain marked candidates.
    """

    jobs_by_source = jobs_by_source or {}
    suffix_allocations = _equipment_suffix_damage_allocations(
        events, skill_parameter_events
    )
    direct_rows_by_target: dict[tuple[str, str], list[tuple[int, int, CombatEvent]]] = {}
    direct_rows_by_source: dict[str, list[tuple[int, int, CombatEvent]]] = {}
    direct_rows_for_target: dict[str, list[tuple[int, int, CombatEvent]]] = {}
    for index, event in enumerate(events):
        # A direct41 row already proven to be an equipment-suffix result is an
        # effect result, not a factual attack that can trigger another
        # profession effect.  Excluding it here also prevents suffix damage
        # from recursively generating a second attribution.
        if index in suffix_allocations:
            continue
        if not _is_proc_trigger(event):
            continue
        native_skill_id, native_skill_name = native_supplemental_skill_identity(event)
        trigger_event = (
            replace(
                event,
                skill_id=native_skill_id,
                skill_name=native_skill_name,
            )
            if native_skill_id
            and native_skill_name
            and (
                event.skill_id != native_skill_id
                or event.skill_name != native_skill_name
            )
            else event
        )
        row = (event.timestamp_ms, index, trigger_event)
        direct_rows_by_target.setdefault((event.source_id, event.target_id), []).append(row)
        direct_rows_by_source.setdefault(event.source_id, []).append(row)
        direct_rows_for_target.setdefault(event.target_id, []).append(row)
    direct_times_by_target: dict[tuple[str, str], list[int]] = {}
    for key, rows in direct_rows_by_target.items():
        rows.sort(key=lambda item: item[0])
        direct_times_by_target[key] = [item[0] for item in rows]
    direct_times_by_source: dict[str, list[int]] = {}
    for key, rows in direct_rows_by_source.items():
        rows.sort(key=lambda item: item[0])
        direct_times_by_source[key] = [item[0] for item in rows]
    direct_times_for_target: dict[str, list[int]] = {}
    for key, rows in direct_rows_for_target.items():
        rows.sort(key=lambda item: item[0])
        direct_times_for_target[key] = [item[0] for item in rows]

    activation_index = _build_activation_index(cast_events)
    effect_interval_index = _build_effect_interval_index(player_effects)
    status_index = _build_target_status_index(boss_effects, events)
    prepared = _prepare_event_candidates(
        events, jobs_by_source, activation_index, effect_interval_index,
        direct_rows_by_target, direct_times_by_target,
        direct_rows_by_source, direct_times_by_source,
        direct_rows_for_target, direct_times_for_target,
        status_index,
    )

    parallel_allocations = _parallel_effect_allocations(
        events, prepared,
    )
    same_effect_allocations = _same_effect_batch_allocations(
        events, prepared, parallel_allocations,
    )
    parallel_allocations = {
        **same_effect_allocations,
        **parallel_allocations,
    }

    combo_allocations = infer_combo_damage(events, jobs_by_source)
    audited: list[ShadowAttribution] = []
    consumed: set[tuple[str, int]] = set()
    reserved_slots: dict[tuple[str, int], int] = {}
    for event_index, (
        rule, matches, _candidate_names, _guaranteed_fallback
    ) in parallel_allocations.items():
        if matches:
            ordered = sorted(matches, key=lambda item: (item[0], item[1], item[2]))
            reserved_slots[(rule.effect_skill_id, ordered[0][2])] = event_index
    last_rule_proc_ms: dict[tuple[str, str, str], int] = {}
    for event_index, event in enumerate(events):
        suffix_definition = suffix_allocations.get(event_index)
        if suffix_definition is not None:
            audited.append(
                ShadowAttribution(
                    event_index, "效果来源已识别", 0.98,
                    "装备后缀额外伤害", suffix_definition.skill_id,
                    suffix_definition.display_name,
                    evidence=(
                        "装备后缀513状态码目录+同源唯一窄时间窗"
                    ),
                    reason=(
                        "已进入独立装备后缀通道，不再参与职业Buff/被动/"
                        "Debuff效果竞争"
                    ),
                    effect_candidates=(
                        f"{suffix_definition.skill_id}:"
                        f"{suffix_definition.display_name}",
                    ),
                )
            )
            continue
        if event_index in combo_allocations:
            trigger_index, sample_count = combo_allocations[event_index]
            trigger = events[trigger_index] if trigger_index >= 0 else None
            audited.append(ShadowAttribution(
                event_index, "影子归因确定", 0.90, "被动", "501", "连击",
                trigger_event_index=trigger_index,
                trigger_skill_id=trigger.skill_id if trigger else "",
                trigger_skill_name=trigger.skill_name if trigger else "",
                trigger_damage=trigger.amount if trigger else 0,
                delta_ms=event.timestamp_ms - trigger.timestamp_ms if trigger else 0,
                evidence=("狂战士subtype5 B1匿名对外伤害；按已确认统计规则归入连击"
                          if sample_count == 0 else
                          f"狂战士独立B1结果；{sample_count}个时刻同值、主伤害变化；排除10%追加"),
                reason="连击独立归属；不增加触发技能Hit",
            ))
            continue
        if not _is_additional_candidate(event):
            continue
        if _is_periodic(event):
            audited.append(
                ShadowAttribution(
                    event_index, "当前仅识别Debuff类型", 0.0,
                    "Debuff周期伤害", "", event.skill_name,
                    evidence="周期状态伤害记录",
                    reason="没有独立效果技能号，不用最近施法猜测Buff/被动来源",
                )
            )
            continue

        item = prepared.get(event_index)
        if item is None:
            continue
        explicit_rules = item.explicit_rules
        if (
            len(explicit_rules) == 1
            and (event.trigger_skill_id or event.trigger_skill_name)
            and event.trigger_attribution_confidence >= 0.95
        ):
            rule = explicit_rules[0]
            audited.append(
                ShadowAttribution(
                    event_index, "当前已正确识别", 1.0,
                    rule.effect_type, rule.effect_skill_id, rule.effect_name,
                    trigger_skill_id=event.trigger_skill_id,
                    trigger_skill_name=event.trigger_skill_name,
                    evidence="包内效果技能号+已验证的同包/同目标触发技能字段",
                    reason=(
                        "正式触发字段优先于影子窗口重算；不会因附近存在多个Hit、"
                        "阶梯倍率整数舍入或原始Hit缺包而丢失已确认关联"
                    ),
                    effect_candidates=(
                        f"{rule.effect_skill_id}:{rule.effect_name}",
                    ),
                )
            )
            continue
        matched_rules: list[tuple[ShadowEffectRule, list[tuple[float, int, int, CombatEvent]]]] = []
        condition_notes_by_rule: dict[str, str] = {}
        standalone_rules: list[ShadowEffectRule] = []
        activated_effect_rules: list[
            tuple[ShadowEffectRule, list[tuple[float, int, int, CombatEvent]]]
        ] = []
        for rule, matches, condition_note, standalone in item.eligible:
            condition_notes_by_rule[rule.effect_skill_id] = condition_note
            interval_key = (rule.effect_skill_id, event.source_id, event.target_id)
            previous_proc = last_rule_proc_ms.get(interval_key)
            if (
                rule.minimum_interval_ms > 0
                and previous_proc is not None
                and event.timestamp_ms - previous_proc < rule.minimum_interval_ms
            ):
                continue
            if matches:
                matched_rules.append((rule, matches))
                if rule.minimum_interval_ms > 0:
                    last_rule_proc_ms[interval_key] = event.timestamp_ms
            elif standalone:
                # Prefer an independently matched originating Hit whenever it
                # exists. Some independently identified status rows are
                # nevertheless emitted without that Hit in the same narrow
                # record window; their full signature may identify the effect
                # source, but must not invent the triggering skill. Subtype0
                # alone is deliberately excluded because ordinary skill Hits
                # and Burning Hand share that layout.
                standalone_rules.append(rule)
                if rule.minimum_interval_ms > 0:
                    last_rule_proc_ms[interval_key] = event.timestamp_ms
            if standalone and rule.effect_source_when_activated:
                activated_effect_rules.append((rule, matches))

        # A target-specific Debuff signature is stronger than a coincidental
        # percentage match from another generic proc.  It proves the effect
        # owner even when several party Hits share the same packet timestamp
        # and the exact triggering skill cannot be selected safely.
        if len(activated_effect_rules) == 1:
            activated_rule, activated_matches = activated_effect_rules[0]
            matched_rules = (
                [(activated_rule, activated_matches)]
                if activated_matches else []
            )
            standalone_rules = [activated_rule] if not activated_matches else []

        effect_candidates = tuple(
            f"{rule.effect_skill_id}:{rule.effect_name}" for rule, _ in matched_rules
        )
        parallel_candidate_names: tuple[str, ...] = ()
        guaranteed_priority_applied = False
        parallel = parallel_allocations.get(event_index)
        if parallel is not None:
            (
                rule,
                matches,
                parallel_candidate_names,
                guaranteed_priority_applied,
            ) = parallel
            matched_rules = [(rule, matches)]
        elif len(matched_rules) > 1:
            if parallel is None:
                guaranteed = [
                    (rule, matches)
                    for rule, matches in matched_rules
                    if rule.guaranteed_when_eligible
                ]
                if len(guaranteed) == 1:
                    # Example: Raid is guaranteed on a critical Hit while
                    # Vengeance Power is only a chance proc.  With one wire
                    # row, assigning it to the guaranteed effect is the only
                    # result consistent with both client rules.  If two
                    # guaranteed effects survive, ambiguity is retained.
                    matched_rules = guaranteed
                    parallel_candidate_names = effect_candidates
                    guaranteed_priority_applied = True
                else:
                    audited.append(
                        ShadowAttribution(
                            event_index, "效果来源冲突", 0.50,
                            "Buff/被动候选", "", "",
                            evidence=f"{len(matched_rules)}个效果规则同时通过倍率与触发条件",
                            reason=(
                                "同一Hit允许并行触发多个效果，但实际状态伤害记录数不足，"
                                "且没有唯一的必定触发效果，不能把同一条伤害重复归给多个效果"
                            ),
                            effect_candidates=effect_candidates,
                            condition_notes=tuple(
                                condition_notes_by_rule.get(rule.effect_skill_id, "")
                                for rule, _ in matched_rules
                                if condition_notes_by_rule.get(rule.effect_skill_id, "")
                            ),
                        )
                    )
                    continue

        if not matched_rules:
            if len(standalone_rules) == 1:
                rule = standalone_rules[0]
                evidence_parts = ["职业"]
                if rule.effect_source_when_activated:
                    evidence_parts.extend(
                        [
                            f"同目标{rule.activation_duration_ms}ms Debuff窗口",
                            "固定附加伤害签名",
                        ]
                    )
                elif rule.wire_layout_contains:
                    evidence_parts.append(
                        f"{rule.wire_layout_contains}独立状态伤害签名"
                    )
                if rule.activation_skill_ids:
                    evidence_parts.append(
                        f"{rule.activation_duration_ms}ms施法激活窗口"
                    )
                if rule.minimum_interval_ms > 0:
                    evidence_parts.append(
                        f"{rule.minimum_interval_ms}ms最短触发间隔"
                    )
                reason = (
                    "目标Debuff窗口与固定伤害签名同时成立；确认伤害属于Debuff施放者，"
                    "但附近缺少唯一事实Hit，因此不猜触发技能"
                    if rule.effect_source_when_activated else
                    "独立状态伤害布局与施法激活窗口同时成立；只确认效果来源，"
                    "不把面板结算伤害错误关联到最近Hit"
                )
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 0.98,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence="+".join(evidence_parts),
                        reason=reason,
                        effect_candidates=(f"{rule.effect_skill_id}:{rule.effect_name}",),
                    )
                )
                continue
            if explicit_rules:
                rule = explicit_rules[0]
                if (
                    event.trigger_skill_id or event.trigger_skill_name
                ) and event.trigger_attribution_confidence >= 0.95:
                    audited.append(
                        ShadowAttribution(
                            event_index, "当前已正确识别", 1.0,
                            rule.effect_type, rule.effect_skill_id, rule.effect_name,
                            trigger_skill_id=event.trigger_skill_id,
                            trigger_skill_name=event.trigger_skill_name,
                            observed_ratio=0.0,
                            evidence=(
                                "包内效果技能号+已验证的同包/同目标触发技能字段"
                            ),
                            reason=(
                                "效果来源和触发技能已有独立字段；不因阶梯倍率整数舍入"
                                "或原始Hit缺包而丢失技能构成合并"
                            ),
                            effect_candidates=(
                                f"{rule.effect_skill_id}:{rule.effect_name}",
                            ),
                        )
                    )
                    continue
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 1.0,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence="包内状态伤害技能号直接命中规则",
                        reason="触发Hit没有通过独立倍率公式，保持未关联",
                        effect_candidates=(f"{rule.effect_skill_id}:{rule.effect_name}",),
                        condition_notes=(
                            (condition_notes_by_rule.get(rule.effect_skill_id, ""),)
                            if condition_notes_by_rule.get(rule.effect_skill_id, "")
                            else ()
                        ),
                    )
                )
            else:
                audited.append(
                    ShadowAttribution(
                        event_index, "保留未归因", 0.0,
                        "未知Buff/被动/Debuff额外伤害", "", "",
                        evidence="未具名状态伤害包",
                        reason="没有规则同时通过职业/激活窗口/暴击/倍率校验",
                    )
                )
            continue

        rule, matches = matched_rules[0]
        identities = {(trigger.skill_id, trigger.skill_name) for *_rest, trigger in matches}
        if len(identities) != 1:
            if str(event.skill_id or "").strip() == rule.effect_skill_id:
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 1.0,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence=(
                            "包内/同包伤害技能身份直接命中效果规则；"
                            f"附近{len(matches)}条事实Hit"
                        ),
                        reason=(
                            "效果来源确定，但触发Hit身份不唯一；保留效果技能，"
                            "不使用最近技能强猜"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            if rule.effect_source_when_activated:
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 0.98,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence=(
                            f"同目标{rule.activation_duration_ms}ms Debuff窗口+"
                            f"固定伤害签名；附近{len(matches)}条全队事实Hit"
                        ),
                        reason=(
                            "伤害确定归属Debuff施放者；多个技能身份同时邻近，"
                            "不强行指定触发技能"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            if rule.required_target_status_labels:
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 0.98,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        observed_ratio=(
                            event.amount / matches[0][3].amount
                            if matches and matches[0][3].amount > 0 else 0.0
                        ),
                        evidence=(
                            "同目标状态生命周期+客户端固定倍率；"
                            f"附近{len(matches)}条事实Hit"
                        ),
                        reason=(
                            "效果来源唯一，但触发Hit身份不唯一；保留效果技能，"
                            "不使用最近技能强猜"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            audited.append(
                ShadowAttribution(
                    event_index, "仅候选", 0.55,
                    rule.effect_type, rule.effect_skill_id, rule.effect_name,
                    evidence=f"{len(matches)}条公式匹配Hit，{len(identities)}个技能身份",
                    reason="触发技能不唯一，影子审计不强行选择",
                    effect_candidates=(f"{rule.effect_skill_id}:{rule.effect_name}",),
                    condition_notes=(
                        (condition_notes_by_rule.get(rule.effect_skill_id, ""),)
                        if condition_notes_by_rule.get(rule.effect_skill_id, "")
                        else ()
                    ),
                )
            )
            continue

        matches.sort(key=lambda item: (item[0], item[1], item[2]))
        error, _distance, trigger_index, trigger = matches[0]
        one_to_one_key = (rule.effect_skill_id, trigger_index)
        if (
            one_to_one_key in consumed
            or (
                one_to_one_key in reserved_slots
                and reserved_slots[one_to_one_key] != event_index
            )
        ):
            # Some effects carry their authoritative effect skill id in every
            # physical result and are displayed in the effect skill itself.
            # They do not need a unique nearby trigger merely to prove their
            # effect identity.  In particular, a multi-result skill-local
            # effect may legitimately emit more than one row beside the same
            # native Hit.  Keeping later rows as ordinary skill Hits inflated
            # the native Hit/average-Hit metrics (for example Punishing
            # Attack).  We still avoid guessing a trigger here; only the
            # packet-proven effect source is accepted.
            if (
                rule.explicit_only
                and rule.composition_destination == "effect_skill"
                and str(event.skill_id or "").strip() == rule.effect_skill_id
            ):
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 1.0,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence="包内状态伤害技能号直接命中效果规则",
                        reason=(
                            "效果来源由包内技能号确定；附近事实Hit已被同效果占用，"
                            "因此不重复指定触发Hit"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            if rule.effect_source_when_activated:
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 0.98,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        evidence=(
                            f"同目标{rule.activation_duration_ms}ms Debuff窗口+"
                            "固定伤害签名"
                        ),
                        reason=(
                            "效果来源确定，但最近事实Hit已关联另一条状态记录；"
                            "为避免一条Hit重复充当触发源，不记录具体触发技能"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            if rule.required_target_status_labels:
                audited.append(
                    ShadowAttribution(
                        event_index, "效果来源已识别", 0.98,
                        rule.effect_type, rule.effect_skill_id, rule.effect_name,
                        observed_ratio=(
                            event.amount / trigger.amount if trigger.amount > 0 else 0.0
                        ),
                        evidence="同目标状态生命周期+客户端固定倍率",
                        reason=(
                            "效果来源确定，但最近事实Hit已关联另一条状态记录；"
                            "为避免一条Hit重复充当触发源，不记录具体触发技能"
                        ),
                        effect_candidates=(
                            f"{rule.effect_skill_id}:{rule.effect_name}",
                        ),
                    )
                )
                continue
            audited.append(
                ShadowAttribution(
                    event_index, "仅候选", 0.55,
                    rule.effect_type, rule.effect_skill_id, rule.effect_name,
                    trigger_index, trigger.skill_id, trigger.skill_name,
                    trigger.amount, event.timestamp_ms - trigger.timestamp_ms,
                    event.amount / trigger.amount,
                    "倍率与时间匹配，但同一事实Hit已被同效果占用",
                    "防止复合路由或重复状态记录被重复归因",
                    (f"{rule.effect_skill_id}:{rule.effect_name}",),
                )
            )
            continue
        consumed.add(one_to_one_key)

        current_exact = bool(
            str(event.skill_id or "").strip() == rule.effect_skill_id
            and event.trigger_skill_id == trigger.skill_id
            and event.trigger_skill_name == trigger.skill_name
            and event.trigger_attribution_confidence >= 0.95
        )
        condition_note = condition_notes_by_rule.get(rule.effect_skill_id, "")
        condition_pending = bool(condition_note)
        if parallel_candidate_names:
            parallel_evidence = (
                "同倍率候选只有一条物理记录，已按必定触发优先于概率触发分配"
                if guaranteed_priority_applied
                else
                "同一触发Hit观测到足量的独立同倍率状态伤害记录，"
                "已按职业被动与团队Buff一对一分配"
            )
        else:
            parallel_evidence = ""
        audited.append(
            ShadowAttribution(
                event_index,
                "当前已正确识别" if current_exact else "条件候选" if condition_pending else "影子归因确定",
                max(0.95, 1.0 - min(0.05, error)) if not condition_pending else min(0.85, 1.0 - min(0.15, error)),
                rule.effect_type, rule.effect_skill_id, rule.effect_name,
                trigger_index, trigger.skill_id, trigger.skill_name,
                trigger.amount, event.timestamp_ms - trigger.timestamp_ms,
                event.amount / trigger.amount,
                parallel_evidence + (
                    "；本效果的附加条件尚无包内证据"
                    if parallel_evidence and condition_pending else ""
                )
                if parallel_evidence else
                "当前效果技能号+触发技能字段已完整一致"
                if current_exact else
                "同目标、全队触发窗口、客户端数值及额外条件唯一匹配"
                if rule.allow_cross_source_same_target and not condition_pending else
                "同角色、限定窗口、客户端倍率及额外条件唯一匹配"
                if not condition_pending else
                "倍率和触发技能唯一，但客户端条件尚无包内证据",
                condition_note,
                (f"{rule.effect_skill_id}:{rule.effect_name}",),
                ((condition_note,) if condition_note else ()),
            )
        )
    return _confirmed_effect_chain_attributions(
        events, audited, jobs_by_source,
        direct_rows_for_target, direct_times_for_target,
    )
