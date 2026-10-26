from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import math
from typing import Any, Literal


# The client has no verified 30% aggregate defense-reduction cap. Its natural
# boundary is zero remaining defense, so no more than 100% can be effective.
DEFENSE_REDUCTION_CAP_PERCENT = 100.0
AMPLIFICATION_CAP_PERCENT = 100.0
ATTACK_INCREASE_CAP_PERCENT = 100.0
CRITICAL_RESIST_REDUCTION_CAP_PERCENT = 100.0

# The client applies some fixed attack skill effects before the skill attack
# percentage lane.  Keep the lane as hidden metadata so the visible fixed
# attack is still rendered only once.  The DNT importer emits these markers
# from EffectClass 1/28 with ApplyType=2 (currently BattleHowl/War Cry).
SKILL_PERCENT_AMPLIFIED_FLAT_ATTACK_KEYS = {
    "物攻": "__技能百分比前物攻",
    "魔攻": "__技能百分比前魔攻",
}

GroupMode = Literal["additive", "max", "exclusive_admission", "replace"]

_GENERAL_DEFENSE_REDUCTION_KEYS = frozenset(
    {"减防御%", "减防御力%", "防御降低%"}
)
_PHYSICAL_DEFENSE_REDUCTION_KEYS = frozenset(
    {"减物理防御%", "减物理防御力%", "物理防御降低%"}
)
_MAGICAL_DEFENSE_REDUCTION_KEYS = frozenset(
    {"减魔法防御%", "减魔法防御力%", "魔法防御降低%"}
)
_CRITICAL_RESIST_REDUCTION_KEYS = frozenset(
    {"减致命抵抗%", "减致命一击抵抗%", "减暴击抵抗%"}
)
_UNIVERSAL_AMPLIFICATION_KEYS = frozenset(
    {"增伤%", "伤害增加%", "全增伤%", "全增伤", "全伤害增加%", "通用增伤%"}
)
_PHYSICAL_AMPLIFICATION_KEYS = frozenset({"物理增伤%", "物理伤害增加%"})
_MAGICAL_AMPLIFICATION_KEYS = frozenset({"魔法增伤%", "魔法伤害增加%"})
_UNIVERSAL_ATTACK_KEYS = frozenset(
    {"攻击%", "双攻%", "物魔攻%", "物理魔法攻击%"}
)
_PHYSICAL_ATTACK_KEYS = frozenset({"物攻%", "物理攻击%", "物理攻击力%"})
_MAGICAL_ATTACK_KEYS = frozenset({"魔攻%", "魔法攻击%", "魔法攻击力%"})
_ELEMENT_RESISTANCE_KEYS = frozenset(
    {"减火抗%", "减水抗%", "减光抗%", "减暗抗%"}
)

# SkillDuplicate/BuffGroup/BuffBlow 36 is an admission lane. An application
# attempted while the lane is occupied is rejected and cannot revive later.
_CLIENT_GROUP_36_SKILLS = frozenset(
    {"地狱火海", "漫长熔浆", "岩浆墙", "灵巧猎手", "灵魂之刃"}
)
# Client group 162 only links these two EX effects. Firewall EX, Soul Scream
# and Gale suffix use independent lanes and therefore stack with this group.
_CLIENT_GROUP_162_SKILLS = frozenset({"切骨之刺", "旋风斩"})

# Both Assassin defence debuffs use SkillDuplicate 6202.  Shadow's EX raises
# the lane to 20%, while the inherited Blaze version remains 10%; concurrent
# applications are fallback layers of one max lane rather than additive rows.
_CLIENT_SKILL_DUPLICATE_6202_SKILLS = frozenset({"致命狂镰", "惩罚"})
_CLIENT_SUFFIX_STATE_EFFECT_GROUPS = {
    # Equipment suffix packets from every weapon/job variant converge on the
    # same target-side StateEffect id.  The weapon slot is only the proc
    # source; it is not an independent debuff lane on the target.
    "疾风后缀": ("减致命抵抗%", "client:state-effect:20003"),
    "铁壁后缀": ("减物理防御%", "client:state-effect:20013"),
    "帷幕后缀": ("减魔法防御%", "client:state-effect:20014"),
}


@lru_cache(maxsize=4096)
def _clean_text_value(value: str) -> str:
    """Normalize the small, repeatedly-used effect vocabulary once."""

    return value.strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if type(value) is str:
        return _clean_text_value(value)
    return str(value).strip()


def _number(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


@lru_cache(maxsize=4096)
def _normalized_skill_text(text: str) -> str:
    text = "".join(text.upper().split()).replace("ＥＸ", "EX")
    if text.endswith("状态"):
        text = text[:-2]
    if text.endswith("EX"):
        text = text[:-2]
    return text


def _skill_key(value: Any) -> str:
    return _normalized_skill_text(_text(value))


@lru_cache(maxsize=256)
def _canonical_defense_reduction_text(text: str) -> str:
    if text in _GENERAL_DEFENSE_REDUCTION_KEYS:
        return "减防御%"
    if text in _PHYSICAL_DEFENSE_REDUCTION_KEYS:
        return "减物理防御%"
    if text in _MAGICAL_DEFENSE_REDUCTION_KEYS:
        return "减魔法防御%"
    return ""


def canonical_defense_reduction_key(key: Any) -> str:
    """Return the canonical defense-reduction channel for an attribute key."""

    return _canonical_defense_reduction_text(_text(key))


@lru_cache(maxsize=256)
def _canonical_effect_text(text: str) -> str:
    defense = _canonical_defense_reduction_text(text)
    if defense:
        return defense
    if text in _CRITICAL_RESIST_REDUCTION_KEYS:
        return "减致命抵抗%"
    if text in _UNIVERSAL_AMPLIFICATION_KEYS:
        return "增伤%"
    if text in _PHYSICAL_AMPLIFICATION_KEYS:
        return "物理增伤%"
    if text in _MAGICAL_AMPLIFICATION_KEYS:
        return "魔法增伤%"
    if text in _UNIVERSAL_ATTACK_KEYS:
        return "攻击%"
    if text in _PHYSICAL_ATTACK_KEYS:
        return "物攻%"
    if text in _MAGICAL_ATTACK_KEYS:
        return "魔攻%"
    return text


def canonical_effect_key(key: Any) -> str:
    """Normalize aliases used by add-point data, captures and UI snapshots."""

    return _canonical_effect_text(_text(key))


@dataclass(frozen=True, slots=True)
class EffectInput:
    """One concrete status application or one static simulated effect."""

    key: str
    value: float
    skill_id: str = ""
    skill_name: str = ""
    job: str = ""
    source_id: str = ""
    target_id: str = ""
    instance_id: str = ""
    start: float = 0.0
    end: float = math.inf
    stack_group: str = ""
    group_mode: GroupMode | str = ""
    stackable: bool = False
    priority: int = 0
    data_source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectRule:
    key: str
    stack_group: str
    group_mode: GroupMode
    priority: int = 0


def effect_lane_marker(rule: EffectRule) -> str:
    """Return the hidden timeline marker for one resolved client lane."""

    return f"__effect_lane__{rule.key}::{rule.stack_group}"


@dataclass(frozen=True, slots=True)
class EffectContribution:
    effect: EffectInput
    rule: EffectRule
    accepted: bool
    active: bool
    selected: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EffectResolution:
    values: Mapping[str, float]
    raw_values: Mapping[str, float]
    contributions: tuple[EffectContribution, ...]

    @property
    def rejected(self) -> tuple[EffectContribution, ...]:
        return tuple(row for row in self.contributions if not row.accepted)

    @property
    def effective(self) -> tuple[EffectContribution, ...]:
        return tuple(
            row
            for row in self.contributions
            if row.accepted and row.active and row.selected
        )


@dataclass(frozen=True, slots=True)
class EffectIntervalResolution:
    """Effective lifetime and suppression evidence for concrete instances."""

    effective_intervals: Mapping[str, tuple[tuple[float, float], ...]]
    rejected_reasons: Mapping[str, str]
    suppressed_reasons: Mapping[str, str]


@lru_cache(maxsize=8192)
def _effect_rule_cached(
    key: str,
    skill: str,
    explicit_group: str,
    explicit_mode: str,
    skill_id: str,
    stackable: bool,
    priority: int,
) -> EffectRule:
    if skill in _CLIENT_GROUP_36_SKILLS and key in _ELEMENT_RESISTANCE_KEYS:
        if explicit_group == "theory:hit-assumed-resist" and explicit_mode == "max":
            # Planning/score completion assumes a real hit applies its
            # resistance reduction. The factual Boss status overlay still
            # uses the client's exclusive-admission lane below.
            return EffectRule(key, f"element:{key}", "max", max(1, priority))
        return EffectRule(
            key, "client:36", "exclusive_admission", max(1, priority)
        )
    if skill in _CLIENT_GROUP_162_SKILLS and key == "减致命抵抗%":
        return EffectRule(key, "client:162", "max", max(1, priority))
    if skill in _CLIENT_SKILL_DUPLICATE_6202_SKILLS and key == "减防御%":
        return EffectRule(key, "client:skill-duplicate:6202", "max", priority)
    suffix_rule = _CLIENT_SUFFIX_STATE_EFFECT_GROUPS.get(skill)
    if suffix_rule is not None and key == suffix_rule[0]:
        return EffectRule(key, suffix_rule[1], "max", priority)

    if explicit_mode in {"additive", "max", "exclusive_admission", "replace"}:
        group = explicit_group or skill or skill_id or key
        return EffectRule(key, group, explicit_mode, priority)  # type: ignore[arg-type]

    if key in _ELEMENT_RESISTANCE_KEYS:
        group = (
            explicit_group or skill or skill_id or key
            if stackable
            else f"element:{key}"
        )
        return EffectRule(key, group, "max", priority)
    if key == "减全抗%":
        group = explicit_group or skill or skill_id or key
        return EffectRule(key, group, "max", priority)
    if key == "减致命抵抗%":
        # Old snapshots used the generic label as one global group. It was an
        # ACT convention, not a client group, so do not preserve it for normal
        # skill debuffs. Equipment suffixes have already been routed through
        # their concrete StateEffect lane above.
        legacy_group = explicit_group in {"减致命抵抗", "减致命抵抗%"}
        group = (
            "" if legacy_group else explicit_group
        ) or f"critical:{skill or skill_id or key}"
        return EffectRule(key, group, "max", priority)
    if key in {"减防御%", "减物理防御%", "减魔法防御%"}:
        group = explicit_group or f"defense:{skill or skill_id or key}"
        return EffectRule(key, group, "max", priority)
    if key in {"增伤%", "物理增伤%", "魔法增伤%"}:
        group = explicit_group or f"amplification:{skill or skill_id or key}"
        return EffectRule(key, group, "max", priority)
    if key in {"攻击%", "物攻%", "魔攻%"}:
        group = explicit_group or f"attack:{skill or skill_id or key}"
        return EffectRule(key, group, "max", priority)
    if key == "减攻击%":
        return EffectRule(
            key, explicit_group or "client:attack-down", "max", priority
        )
    group = explicit_group or skill or skill_id or key
    return EffectRule(key, group, "max", priority)


def effect_rule(effect: EffectInput) -> EffectRule:
    """Resolve one effect's client stacking lane.

    A long score axis asks this question millions of times for a vocabulary of
    only a few dozen structural rules.  Concrete instance timestamps and
    values never participate in the answer, so cache precisely the fields the
    original decision tree reads.  The immutable ``EffectRule`` is safe to
    share and the scoring result is bit-for-bit identical.
    """

    return _effect_rule_cached(
        canonical_effect_key(effect.key),
        _skill_key(effect.skill_name),
        _text(effect.stack_group),
        _text(effect.group_mode),
        _text(effect.skill_id),
        bool(effect.stackable),
        int(effect.priority),
    )


class EffectStackingEngine:
    """Resolve effect admission, replacement, caps and contributors."""

    _EPSILON = 1e-9

    @staticmethod
    def _admission_identity(effect: EffectInput, rule: EffectRule) -> str:
        return _skill_key(effect.skill_name) or _text(effect.skill_id) or rule.stack_group

    def _admission_decisions(
        self,
        rows: tuple[tuple[int, EffectInput, EffectRule], ...],
    ) -> dict[int, tuple[bool, str]]:
        decisions = {index: (True, "") for index, _effect, _rule in rows}
        lanes: dict[
            tuple[str, str], list[tuple[int, EffectInput, EffectRule]]
        ] = defaultdict(list)
        for row in rows:
            _index, effect, rule = row
            if rule.group_mode == "exclusive_admission":
                lanes[(effect.target_id, rule.stack_group)].append(row)

        for candidates in lanes.values():
            candidates.sort(
                key=lambda row: (
                    row[1].start,
                    -row[2].priority,
                    -max(0.0, _number(row[1].value)),
                    row[0],
                )
            )
            holder_end = -math.inf
            holder_identity = ""
            holder_name = ""
            for index, effect, rule in candidates:
                identity = self._admission_identity(effect, rule)
                if effect.end <= effect.start + self._EPSILON:
                    decisions[index] = (False, "无有效持续区间")
                    continue
                if effect.start < holder_end - self._EPSILON:
                    if identity == holder_identity:
                        holder_end = max(holder_end, effect.end)
                    else:
                        decisions[index] = (
                            False,
                            f"{rule.stack_group} 已由 {holder_name or holder_identity} 占用，状态施加被拒绝",
                        )
                    continue
                holder_end = effect.end
                holder_identity = identity
                holder_name = effect.skill_name or effect.skill_id
        return decisions

    def _normalized_rows(
        self, effects: Iterable[EffectInput]
    ) -> tuple[tuple[int, EffectInput, EffectRule], ...]:
        rows: list[tuple[int, EffectInput, EffectRule]] = []
        for index, effect in enumerate(effects):
            if max(0.0, _number(effect.value)) <= self._EPSILON:
                continue
            if not canonical_effect_key(effect.key):
                continue
            rows.append((index, effect, effect_rule(effect)))
        return tuple(rows)

    def _selected_indexes(
        self,
        active_rows: Iterable[tuple[int, EffectInput, EffectRule]],
        *,
        include_tied_max: bool = False,
    ) -> set[int]:
        """Select active instances using the same rules for totals and axes."""

        selected: set[int] = set()
        grouped: dict[
            tuple[str, str, str], list[tuple[int, EffectInput, EffectRule]]
        ] = defaultdict(list)
        for row in active_rows:
            index, effect, rule = row
            if rule.group_mode == "additive":
                selected.add(index)
            else:
                grouped[(effect.target_id, rule.key, rule.stack_group)].append(row)

        for candidates in grouped.values():
            mode = candidates[0][2].group_mode
            if mode == "replace":
                chosen = max(
                    candidates,
                    key=lambda row: (
                        row[1].start,
                        row[2].priority,
                        row[1].value,
                        row[0],
                    ),
                )
                selected.add(chosen[0])
                continue

            maximum = max(max(0.0, _number(row[1].value)) for row in candidates)
            tied = [
                row
                for row in candidates
                if math.isclose(
                    max(0.0, _number(row[1].value)),
                    maximum,
                    rel_tol=1e-9,
                    abs_tol=self._EPSILON,
                )
            ]
            if include_tied_max:
                selected.update(row[0] for row in tied)
                continue
            chosen = max(
                tied,
                key=lambda row: (
                    row[2].priority,
                    row[1].start,
                    row[0],
                ),
            )
            selected.add(chosen[0])
        return selected

    def resolve_active(
        self,
        effects: Iterable[EffectInput],
        at_seconds: float | None = None,
    ) -> EffectResolution:
        rows = self._normalized_rows(effects)
        decisions = self._admission_decisions(rows)
        at = 0.0 if at_seconds is None else _number(at_seconds)

        active_rows = [
            row
            for row in rows
            if decisions[row[0]][0]
            and row[1].start <= at + self._EPSILON < row[1].end
        ]
        selected_indexes = self._selected_indexes(active_rows)

        raw: dict[str, float] = defaultdict(float)
        contributions: list[EffectContribution] = []
        for index, effect, rule in rows:
            accepted, reason = decisions[index]
            active = accepted and effect.start <= at + self._EPSILON < effect.end
            selected = active and index in selected_indexes
            if selected:
                raw[rule.key] += max(0.0, _number(effect.value))
            elif active and not reason:
                reason = f"同组 {rule.stack_group} 仅采用最高有效值"
            contributions.append(
                EffectContribution(effect, rule, accepted, active, selected, reason)
            )

        return EffectResolution(
            self._finalize_values(raw), dict(raw), tuple(contributions)
        )

    def resolve_selected_series(
        self,
        effects: Iterable[EffectInput],
        at_seconds: Iterable[float],
        *,
        preserve_input_order: bool = False,
    ) -> tuple[
        tuple[
            float,
            Mapping[str, float],
            tuple[tuple[EffectInput, EffectRule], ...],
        ],
        ...,
    ]:
        """Resolve values and winning instances with one normalization pass.

        ``resolve_active`` intentionally returns per-instance contribution
        diagnostics, which makes it the right API for one detail view.  A
        replay curve, however, asks for the same immutable effect collection
        at every start/end boundary.  Re-normalizing every row at every
        boundary made that path O(boundaries * effects), including millions
        of repeated string canonicalization calls on long captures.

        This sweep preserves the exact half-open interval and stacking rules
        of ``resolve_active`` while normalizing/admitting each effect once.
        Only the usually small currently-active set is regrouped per point.
        """

        rows = self._normalized_rows(effects)
        decisions = self._admission_decisions(rows)
        accepted_rows = tuple(
            row
            for row in rows
            if decisions[row[0]][0]
            and row[1].end > row[1].start + (0.0 if preserve_input_order else self._EPSILON)
        )
        starts = sorted(
            accepted_rows,
            key=lambda row: (row[1].start, row[0]),
        )
        ends = sorted(
            accepted_rows,
            key=lambda row: (row[1].end, row[0]),
        )
        points = sorted({_number(value) for value in at_seconds})
        active: dict[int, tuple[int, EffectInput, EffectRule]] = {}
        start_index = 0
        end_index = 0
        resolved: list[
            tuple[
                float,
                Mapping[str, float],
                tuple[tuple[EffectInput, EffectRule], ...],
            ]
        ] = []

        for at in points:
            while (
                start_index < len(starts)
                and starts[start_index][1].start <= at + self._EPSILON
            ):
                row = starts[start_index]
                # A very short interval can have both boundaries before the
                # current requested point. Do not resurrect it after the end
                # sweep has already advanced past that timestamp.
                if at + self._EPSILON < row[1].end:
                    active[row[0]] = row
                start_index += 1
            while (
                end_index < len(ends)
                and ends[end_index][1].end <= at + self._EPSILON
            ):
                active.pop(ends[end_index][0], None)
                end_index += 1

            # The pointwise resolver sums and presents winners in input order.
            # Keep that exact order when replacing a pointwise caller, including
            # floating-point tails. Existing sweep callers retain their order.
            active_rows = (
                tuple(active[index] for index in sorted(active))
                if preserve_input_order else tuple(active.values())
            )
            selected_indexes = self._selected_indexes(active_rows)
            raw: dict[str, float] = defaultdict(float)
            for index, effect, rule in active_rows:
                if index in selected_indexes:
                    raw[rule.key] += max(0.0, _number(effect.value))
            selected_rows = tuple(
                (effect, rule)
                for index, effect, rule in active_rows
                if index in selected_indexes
            )
            resolved.append((at, self._finalize_values(raw), selected_rows))

        return tuple(resolved)

    def resolve_value_series(
        self,
        effects: Iterable[EffectInput],
        at_seconds: Iterable[float],
    ) -> tuple[tuple[float, Mapping[str, float]], ...]:
        """Resolve many timeline points with one normalization pass."""

        return tuple(
            (at, values)
            for at, values, _selected in self.resolve_selected_series(
                effects,
                at_seconds,
            )
        )

    @staticmethod
    def _merge_effective_intervals(
        intervals: Iterable[tuple[float, float]],
    ) -> tuple[tuple[float, float], ...]:
        ordered = sorted(
            (float(start), float(end))
            for start, end in intervals
            if end > start
        )
        merged: list[list[float]] = []
        for start, end in ordered:
            if merged and start <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return tuple((start, end) for start, end in merged)

    def resolve_intervals(
        self,
        effects: Iterable[EffectInput],
        *,
        end_seconds: float | None = None,
        include_tied_max: bool = True,
    ) -> EffectIntervalResolution:
        """Resolve the actual lifetime of each packet/static effect instance.

        Observation and effectiveness are intentionally separate. A rejected
        exclusive-admission application remains observable but gets no later
        lifetime. A normal max-group fallback becomes effective after the
        stronger layer ends. Equal highest layers are all marked effective so
        score allocation can split one real gain between their providers.
        """

        rows = self._normalized_rows(effects)
        decisions = self._admission_decisions(rows)
        boundaries: set[float] = set()
        for index, effect, _rule in rows:
            if not decisions[index][0]:
                continue
            start = max(0.0, float(effect.start))
            end = float(effect.end)
            if end_seconds is not None:
                end = min(end, max(0.0, _number(end_seconds)))
            if end <= start + self._EPSILON:
                continue
            boundaries.add(start)
            boundaries.add(end)

        ordered = sorted(boundaries)
        intervals_by_instance: dict[str, list[tuple[float, float]]] = defaultdict(list)
        selected_any: set[int] = set()
        for start, end in zip(ordered, ordered[1:]):
            if end <= start + self._EPSILON:
                continue
            probe = start + min((end - start) / 2.0, 1e-6)
            active_rows = [
                row
                for row in rows
                if decisions[row[0]][0]
                and row[1].start <= probe + self._EPSILON < row[1].end
            ]
            selected = self._selected_indexes(
                active_rows,
                include_tied_max=include_tied_max,
            )
            selected_any.update(selected)
            for index, effect, _rule in active_rows:
                if index not in selected:
                    continue
                identity = effect.instance_id or str(index)
                intervals_by_instance[identity].append((start, end))

        rejected: dict[str, str] = {}
        suppressed: dict[str, str] = {}
        for index, effect, rule in rows:
            identity = effect.instance_id or str(index)
            accepted, reason = decisions[index]
            if not accepted:
                rejected[identity] = reason or "状态施加被拒绝"
            elif index not in selected_any:
                suppressed[identity] = f"同组 {rule.stack_group} 被更高有效值压制"

        return EffectIntervalResolution(
            effective_intervals={
                identity: self._merge_effective_intervals(intervals)
                for identity, intervals in intervals_by_instance.items()
            },
            rejected_reasons=rejected,
            suppressed_reasons=suppressed,
        )

    def effective_intervals(
        self,
        effects: Iterable[EffectInput],
        *,
        end_seconds: float | None = None,
        include_tied_max: bool = True,
    ) -> Mapping[str, tuple[tuple[float, float], ...]]:
        return self.resolve_intervals(
            effects,
            end_seconds=end_seconds,
            include_tied_max=include_tied_max,
        ).effective_intervals

    @staticmethod
    def _finalize_values(raw: Mapping[str, float]) -> dict[str, float]:
        values = {key: max(0.0, _number(value)) for key, value in raw.items()}
        universal_amp = min(
            AMPLIFICATION_CAP_PERCENT, values.get("增伤%", 0.0)
        )
        values["增伤%"] = universal_amp
        values["物理增伤合计%"] = min(
            AMPLIFICATION_CAP_PERCENT,
            universal_amp + values.get("物理增伤%", 0.0),
        )
        values["魔法增伤合计%"] = min(
            AMPLIFICATION_CAP_PERCENT,
            universal_amp + values.get("魔法增伤%", 0.0),
        )

        all_resist = values.get("减全抗%", 0.0)
        for key in _ELEMENT_RESISTANCE_KEYS:
            values[key] = values.get(key, 0.0) + all_resist

        general = min(
            DEFENSE_REDUCTION_CAP_PERCENT, values.get("减防御%", 0.0)
        )
        values["减防御%"] = general
        values["减物理防御%"] = min(
            DEFENSE_REDUCTION_CAP_PERCENT,
            general + values.get("减物理防御%", 0.0),
        )
        values["减魔法防御%"] = min(
            DEFENSE_REDUCTION_CAP_PERCENT,
            general + values.get("减魔法防御%", 0.0),
        )
        values["减致命抵抗%"] = min(
            CRITICAL_RESIST_REDUCTION_CAP_PERCENT,
            values.get("减致命抵抗%", 0.0),
        )
        values["冰冻层"] = min(2.0, values.get("冰冻层", 0.0))

        universal_attack = min(
            ATTACK_INCREASE_CAP_PERCENT, values.get("攻击%", 0.0)
        )
        values["攻击%"] = universal_attack
        values["物攻合计%"] = min(
            ATTACK_INCREASE_CAP_PERCENT,
            universal_attack + values.get("物攻%", 0.0),
        )
        values["魔攻合计%"] = min(
            ATTACK_INCREASE_CAP_PERCENT,
            universal_attack + values.get("魔攻%", 0.0),
        )
        return values

    def resolve_timeline(
        self,
        effects: Iterable[EffectInput],
        *,
        end_seconds: float | None = None,
    ) -> tuple[tuple[float, EffectResolution], ...]:
        rows = tuple(effects)
        boundaries = {0.0}
        for effect in rows:
            if math.isfinite(effect.start):
                boundaries.add(max(0.0, effect.start))
            if math.isfinite(effect.end):
                boundaries.add(max(0.0, effect.end))
        if end_seconds is not None:
            boundaries.add(max(0.0, _number(end_seconds)))
        return tuple(
            (point, self.resolve_active(rows, point + self._EPSILON))
            for point in sorted(boundaries)
            if end_seconds is None or point <= end_seconds + self._EPSILON
        )

    def marginal_values(
        self,
        effects: Iterable[EffectInput],
        added: EffectInput,
        *,
        at_seconds: float | None = None,
    ) -> dict[str, float]:
        rows = tuple(effects)
        before = self.resolve_active(rows, at_seconds).values
        after = self.resolve_active((*rows, added), at_seconds).values
        return {
            key: _number(after.get(key)) - _number(before.get(key))
            for key in set(before) | set(after)
            if abs(_number(after.get(key)) - _number(before.get(key))) > self._EPSILON
        }


EFFECT_STACKING_ENGINE = EffectStackingEngine()


def _resolved_channel_values(
    attrs: Mapping[str, Any] | None,
    supported_keys: frozenset[str],
) -> Mapping[str, float]:
    """Resolve flattened attributes without counting aliases more than once."""

    components: dict[str, float] = {}
    for raw_key, raw_value in (attrs or {}).items():
        key = canonical_effect_key(raw_key)
        if key not in supported_keys:
            continue
        # Flattened snapshots occasionally retain both the old and canonical
        # label for one value.  They are aliases, not independent effects.
        components[key] = max(
            components.get(key, 0.0),
            max(0.0, _number(raw_value)),
        )
    effects = tuple(
        EffectInput(
            key=key,
            value=value,
            stack_group=f"summary:{key}",
            group_mode="additive",
        )
        for key, value in components.items()
        if value > 0.0
    )
    return EFFECT_STACKING_ENGINE.resolve_active(effects, 0.0).values


def attack_increase_channels(
    attrs: Mapping[str, Any] | None,
) -> tuple[float, float]:
    """Return capped physical and magical attack-increase percentages."""

    values = _resolved_channel_values(
        attrs,
        frozenset({"攻击%", "物攻%", "魔攻%"}),
    )
    return (
        max(0.0, _number(values.get("物攻合计%"))),
        max(0.0, _number(values.get("魔攻合计%"))),
    )


def attack_increase_for_channel(
    attrs: Mapping[str, Any] | None,
    attack: Any,
    *,
    physical_weight: float = 1.0,
    magical_weight: float = 1.0,
) -> float:
    """Return the capped attack increase relevant to one damage channel."""

    physical, magical = attack_increase_channels(attrs)
    attack_text = _text(attack)
    if attack_text in {"物攻", "物理"}:
        return physical
    if attack_text in {"魔攻", "魔法"}:
        return magical
    physical_weight = max(0.0, _number(physical_weight))
    magical_weight = max(0.0, _number(magical_weight))
    total = physical_weight + magical_weight
    if total <= 1e-12:
        return max(physical, magical)
    return (physical * physical_weight + magical * magical_weight) / total


def _fixed_attack_channel_value(
    attrs: Mapping[str, Any] | None,
    attack: str,
) -> float:
    """Read one fixed-attack channel from raw or expanded calculator attrs."""

    source = attrs or {}
    direct = _number(source.get(attack))
    if abs(direct) > 1e-12:
        return direct
    ranged = [
        _number(source.get(f"最小{attack}")),
        _number(source.get(f"最大{attack}")),
    ]
    present = [value for value in ranged if abs(value) > 1e-12]
    return sum(present) / len(present) if present else 0.0


def fixed_attack_for_channel(
    attrs: Mapping[str, Any] | None,
    attack: Any,
    *,
    physical_weight: float = 1.0,
    magical_weight: float = 1.0,
) -> float:
    """Return fixed attack for a physical, magical, or mixed channel."""

    physical = _fixed_attack_channel_value(attrs, "物攻")
    magical = _fixed_attack_channel_value(attrs, "魔攻")
    attack_text = _text(attack)
    if attack_text in {"物攻", "物理"}:
        return physical
    if attack_text in {"魔攻", "魔法"}:
        return magical
    physical_weight = max(0.0, _number(physical_weight))
    magical_weight = max(0.0, _number(magical_weight))
    total = physical_weight + magical_weight
    if total <= 1e-12:
        return max(physical, magical)
    return (physical * physical_weight + magical * magical_weight) / total


def skill_percent_amplified_flat_attack(
    attrs: Mapping[str, Any] | None,
    attack: Any,
    *,
    physical_weight: float = 1.0,
    magical_weight: float = 1.0,
) -> float:
    """Return the fixed attack that belongs before skill attack percentage."""

    source = attrs or {}
    physical = _number(
        source.get(SKILL_PERCENT_AMPLIFIED_FLAT_ATTACK_KEYS["物攻"])
    )
    magical = _number(
        source.get(SKILL_PERCENT_AMPLIFIED_FLAT_ATTACK_KEYS["魔攻"])
    )
    attack_text = _text(attack)
    if attack_text in {"物攻", "物理"}:
        return physical
    if attack_text in {"魔攻", "魔法"}:
        return magical
    physical_weight = max(0.0, _number(physical_weight))
    magical_weight = max(0.0, _number(magical_weight))
    total = physical_weight + magical_weight
    if total <= 1e-12:
        return max(physical, magical)
    return (physical * physical_weight + magical * magical_weight) / total


def amplification_channels(
    attrs: Mapping[str, Any] | None,
) -> tuple[float, float]:
    """Return capped physical and magical damage-amplification percentages."""

    values = _resolved_channel_values(
        attrs,
        frozenset({"增伤%", "物理增伤%", "魔法增伤%"}),
    )
    return (
        max(0.0, _number(values.get("物理增伤合计%"))),
        max(0.0, _number(values.get("魔法增伤合计%"))),
    )


def amplification_for_channel(
    attrs: Mapping[str, Any] | None,
    attack: Any,
    *,
    physical_weight: float = 1.0,
    magical_weight: float = 1.0,
) -> float:
    """Return the capped amplification relevant to one damage channel."""

    physical, magical = amplification_channels(attrs)
    attack_text = _text(attack)
    if attack_text in {"物攻", "物理"}:
        return physical
    if attack_text in {"魔攻", "魔法"}:
        return magical
    physical_weight = max(0.0, _number(physical_weight))
    magical_weight = max(0.0, _number(magical_weight))
    total = physical_weight + magical_weight
    if total <= 1e-12:
        return max(physical, magical)
    return (physical * physical_weight + magical * magical_weight) / total


def defense_reduction_components(
    attrs: Mapping[str, Any] | None,
) -> tuple[float, float, float]:
    """Read general, physical-only and magical-only reduction components."""

    values = {"减防御%": 0.0, "减物理防御%": 0.0, "减魔法防御%": 0.0}
    for raw_key, raw_value in (attrs or {}).items():
        key = canonical_defense_reduction_key(raw_key)
        if key:
            values[key] = max(values[key], max(0.0, _number(raw_value)))
    return values["减防御%"], values["减物理防御%"], values["减魔法防御%"]


def defense_reduction_channels(
    attrs: Mapping[str, Any] | None,
    *,
    cap_percent: float = DEFENSE_REDUCTION_CAP_PERCENT,
) -> tuple[float, float]:
    """Return physical/magical reduction with only the natural zero floor."""

    cap = max(0.0, _number(cap_percent))
    general, physical, magical = defense_reduction_components(attrs)
    return min(cap, general + physical), min(cap, general + magical)


def defense_reduction_for_attack(
    attrs: Mapping[str, Any] | None,
    attack: Any,
    *,
    physical_weight: float = 1.0,
    magical_weight: float = 1.0,
    cap_percent: float = DEFENSE_REDUCTION_CAP_PERCENT,
) -> float:
    """Return the reduction relevant to one physical/magical attack."""

    physical, magical = defense_reduction_channels(attrs, cap_percent=cap_percent)
    attack_text = _text(attack)
    if attack_text in {"物攻", "物理"}:
        return physical
    if attack_text in {"魔攻", "魔法"}:
        return magical
    physical_weight = max(0.0, _number(physical_weight))
    magical_weight = max(0.0, _number(magical_weight))
    total = physical_weight + magical_weight
    if total <= 1e-12:
        return max(physical, magical)
    return (physical * physical_weight + magical * magical_weight) / total
