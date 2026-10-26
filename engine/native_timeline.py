from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .data_loader import DataStore, as_number, norm
from .cooldown import SkillCooldownEngine
from .effect_stacking import (
    EFFECT_STACKING_ENGINE,
    EffectInput,
    attack_increase_for_channel,
)
from .job_mechanics import (
    JOB_MECHANICS,
    adjust_ring_strike_cast_time,
    is_ring_strike,
    ring_strike_output_multiplier,
    adjust_fixed_duration_action_speed_cast_time,
    is_fixed_duration_action_speed_output_skill,
)
from .live.effects import boss_effect_rules
from .team_support_rules import (
    corrected_team_effect_duration,
    effective_team_buff_duration,
    effective_team_effect_duration_for_effect,
    resolved_team_effect_duration_for_effect,
    team_effect_application_window_for_effect,
)


def skill_key(value: Any) -> str:
    text = norm(value).replace(" ", "")
    return text[:-2] if text.upper().endswith("EX") else text


def _merge_intervals(values: Iterable[Iterable[Any]], window: float) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for value in values:
        try:
            start, end = value
        except (TypeError, ValueError):
            continue
        left = max(0.0, min(window, as_number(start)))
        right = max(left, min(window, as_number(end)))
        if right > left + 1e-6:
            rows.append((left, right))
    rows.sort()
    merged: list[tuple[float, float]] = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(slots=True)
class PlannedSkill:
    skill: str
    level: str
    cooldown: float
    cast_time: float
    output: float
    effective_percent: float
    dpa: float
    dps: float
    element: str
    channels: dict[str, float]
    non_damage: bool = False
    duration: float = 0.0
    attrs: dict[str, float] = field(default_factory=dict)
    effect_rows: list[dict[str, Any]] = field(default_factory=list)
    buff_skill_scope: tuple[str, ...] = ()
    full_cast_time: float = 0.0
    quick_cast_time: float = 0.0


class NativeTimelinePlanner:
    """Window-free shared planner for skill, team, and mechanism timelines.

    The planner deliberately reads the same editable mechanic registry and
    effect catalog as V4.01.  It does not import BuildEditor or any Qt type.
    """

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self.cooldown_engine = SkillCooldownEngine(
            store.skill_cooldown_rules(), store.skill_reference_cooldown
        )

    @staticmethod
    def _contains(intervals: list[tuple[float, float]], seconds: float) -> bool:
        return any(start <= seconds < end for start, end in intervals)

    @staticmethod
    def _blocked_end(intervals: list[tuple[float, float]], seconds: float) -> float | None:
        for start, end in intervals:
            if start <= seconds < end:
                return end
            if start > seconds:
                break
        return None

    @staticmethod
    def _next_blocked_start(intervals: list[tuple[float, float]], seconds: float) -> float | None:
        for start, _end in intervals:
            if start > seconds + 1e-6:
                return start
        return None

    def _learned_names(self, build: dict[str, Any]) -> set[str]:
        job = norm(build.get("second_job"))
        selected = build.get("skill_build", {})
        levels = selected.get("levels", {}) if isinstance(selected, dict) else {}
        tree = self.store.skill_build_tree(job, 70)
        learned: set[str] = set()
        for group in tree.get("groups", []):
            for item in group.get("skills", []):
                if as_number(levels.get(norm(item.get("id")))) > 0:
                    learned.add(norm(item.get("name")))
        return learned

    def _effect_rows(
        self,
        job: str,
        skill: str,
        level: Any = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        timing = self.store.skill_effect_timing(job, skill, level, 70)
        described_duration = as_number(timing.get("duration"))
        for rule in boss_effect_rules(skill, job):
            base_duration = resolved_team_effect_duration_for_effect(
                skill,
                rule.key,
                rule.duration_seconds,
                described_duration,
            )
            duration = effective_team_effect_duration_for_effect(
                skill, rule.key, base_duration
            )
            rows.append({
                "kind": "debuff",
                "key": rule.key,
                # The native planner is deterministic, so probabilistic
                # effects use their expected magnitude over repeated casts.
                "value": rule.value * rule.proc_chance,
                "raw_value": rule.value,
                "proc_chance": rule.proc_chance,
                "duration": duration,
                "base_duration": base_duration,
                "stackable": rule.stackable,
                "group": rule.stack_group or rule.key,
            })
        return rows

    def _buff_attrs(self, job: str, skill: str) -> dict[str, float]:
        allowed_sources = {"通用", *self.store.job_buff_sources(job)}
        rows = [
            row for row in self.store.tables.get("buff", [])
            if norm(row.get("技能")) == skill
            and norm(row.get("来源")) in allowed_sources
            and norm(row.get("类别")) in {"个人buff", "团队buff"}
        ]
        if not rows:
            return {}
        max_level = max(int(as_number(row.get("等级"))) for row in rows)
        return {
            norm(row.get("属性")): as_number(row.get("表格数值"))
            for row in rows
            if int(as_number(row.get("等级"))) == max_level and norm(row.get("属性"))
        }

    def _support_skill(self, job: str, skill: str, damage_skill: PlannedSkill | None) -> PlannedSkill | None:
        selected_level = damage_skill.level if damage_skill is not None else None
        timing = self.store.skill_effect_timing(
            job,
            skill,
            selected_level,
            max_required_level=70,
        )
        cooldown = max(0.0, as_number(timing.get("cooldown")))
        duration = max(0.0, as_number(timing.get("duration")))
        attrs = self._buff_attrs(job, skill)
        activation_rule = self.store.buff_activation_rule_for_job(
            job, skill, "个人buff"
        ) or self.store.buff_activation_rule_for_job(job, skill, "团队buff")
        skill_scope = tuple(
            norm(value)
            for value in activation_rule.get("skill_scope", [])
            if norm(value)
        )
        effects = self._effect_rows(
            job,
            skill,
            selected_level or timing.get("level"),
        )
        if not attrs and not effects and damage_skill is None:
            return None
        duration = corrected_team_effect_duration(skill, duration)
        if attrs:
            duration = effective_team_buff_duration(skill, duration)
        rule_duration = max(0.0, as_number(activation_rule.get("duration")))
        rule_cooldown = max(0.0, as_number(activation_rule.get("cooldown")))
        if rule_duration > 0:
            duration = rule_duration
        if rule_cooldown > 0:
            cooldown = rule_cooldown
        cooldown_multipliers = JOB_MECHANICS.job_value(
            job, "team_effect_cooldown_multipliers", {}
        )
        if isinstance(cooldown_multipliers, dict):
            multiplier = as_number(cooldown_multipliers.get(skill), 1.0)
            if multiplier > 0:
                cooldown *= multiplier
        cast_time = as_number(self.store.skill_cast_time(skill, (job,)).get("cast_time"), 0.35)
        if damage_skill is not None:
            copied = copy.deepcopy(damage_skill)
            copied.duration = duration
            copied.attrs = attrs
            copied.effect_rows = effects
            copied.buff_skill_scope = skill_scope
            return copied
        # A support-only row with a zero table cooldown is normally a maintained
        # effect, not a command that should be spammed every animation frame.
        # Keep it refreshable as the effect expires while preserving explicit
        # cooldowns whenever the data provides one.
        if cooldown <= 0.0 and duration > 0.0:
            cooldown = duration
        return PlannedSkill(
            skill=skill,
            level=norm(timing.get("level")) or "1",
            cooldown=cooldown,
            cast_time=max(0.08, cast_time),
            output=0.0,
            effective_percent=0.0,
            dpa=0.0,
            dps=0.0,
            element="",
            channels={},
            non_damage=True,
            duration=duration,
            attrs=attrs,
            effect_rows=effects,
            buff_skill_scope=skill_scope,
        )

    def _utility_skill(self, job: str, skill: str) -> PlannedSkill | None:
        timing = self.store.skill_effect_timing(job, skill, max_required_level=70)
        cast = self.store.skill_cast_time(skill, (job,))
        cast_time = max(
            0.0,
            as_number(cast.get("cast_time"), as_number(timing.get("cast_time"))),
        )
        if cast_time <= 0:
            return None
        return PlannedSkill(
            skill=skill,
            level=norm(timing.get("level")) or "1",
            cooldown=max(0.0, as_number(timing.get("cooldown"))),
            cast_time=max(0.08, cast_time),
            output=0.0,
            effective_percent=0.0,
            dpa=0.0,
            dps=0.0,
            element="",
            channels={},
            non_damage=True,
        )

    def _skills(self, build: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, PlannedSkill]:
        job = norm(build.get("second_job"))
        learned = self._learned_names(build)
        result: dict[str, PlannedSkill] = {}
        for row in rows:
            skill = norm(row.get("skill"))
            if not skill:
                continue
            result[skill_key(skill)] = PlannedSkill(
                skill=skill,
                level=norm(row.get("level")),
                cooldown=max(0.0, as_number(row.get("cooldown"))),
                cast_time=max(0.08, as_number(row.get("castTime"), 0.35)),
                output=max(0.0, as_number(row.get("actualDamage"))),
                effective_percent=max(0.0, as_number(row.get("effectivePercent"))),
                dpa=max(0.0, as_number(row.get("dpa"))),
                dps=max(0.0, as_number(row.get("dps"))),
                element=norm(row.get("element")),
                channels=copy.deepcopy(row.get("channels", {})),
                effect_rows=self._effect_rows(job, skill, row.get("level")),
                full_cast_time=max(
                    0.08,
                    as_number(row.get("fullCastTime"), row.get("castTime", 0.35)),
                ),
                quick_cast_time=max(0.0, as_number(row.get("quickCastTime"))),
            )

        profile = JOB_MECHANICS.job_profile(job)
        support_names = {
            norm(value) for value in profile.get("team_effects", []) if norm(value)
        }
        support_names.update(
            norm(value) for value in profile.get("opening", []) if norm(value)
        )
        # Periodic personal buffs are part of the executable rotation even when
        # they have no damage table row.
        rules = self.store.buff_activation_rules().get("personal_buffs", {})
        job_sources = set(self.store.job_buff_sources(job))
        for key, rule in rules.items():
            provider, _, skill = norm(key).partition("|")
            if provider in job_sources and isinstance(rule, dict) and rule.get("mode") in {"periodic", "triggered"}:
                support_names.add(skill)
        for name in support_names:
            if name not in learned and name not in {norm(value) for value in profile.get("opening", [])}:
                continue
            base = result.get(skill_key(name))
            support = self._support_skill(job, name, base)
            if support is not None:
                activation_rule = self.store.buff_activation_rule_for_job(
                    job, name, "个人buff"
                )
                if isinstance(activation_rule, dict):
                    rule_duration = max(0.0, as_number(activation_rule.get("duration")))
                    rule_cooldown = max(0.0, as_number(activation_rule.get("cooldown")))
                    if rule_duration > 0:
                        support.duration = rule_duration
                    if rule_cooldown > 0:
                        support.cooldown = rule_cooldown
                result[skill_key(name)] = support
        glyph_payload = build.get("skill_glyphs", {})
        glyph_rows = glyph_payload.get("rows", []) if isinstance(glyph_payload, dict) else []
        glyphs: dict[str, dict[str, float]] = defaultdict(dict)
        for row in glyph_rows if isinstance(glyph_rows, list) else []:
            if not isinstance(row, dict):
                continue
            key = skill_key(row.get("skill"))
            effect = norm(row.get("effect"))
            if key and effect:
                glyphs[key][effect] = max(
                    glyphs[key].get(effect, 0.0), max(0.0, as_number(row.get("value")))
                )
        for key, item in result.items():
            values = glyphs.get(key, {})
            duration_bonus = max(
                as_number(values.get("增益持续")),
                as_number(values.get("增益持续时间增加")),
            )
            if duration_bonus > 0 and item.duration > 0:
                item.duration *= 1.0 + duration_bonus / 100.0
        ammo_rule = profile.get("resources", {}).get("ammo", {})
        if isinstance(ammo_rule, dict):
            reload_skill = norm(ammo_rule.get("reload"))
            if reload_skill and skill_key(reload_skill) not in result:
                utility = self._utility_skill(job, reload_skill)
                if utility is not None:
                    result[skill_key(reload_skill)] = utility

        fixed = JOB_MECHANICS.global_value("cooldowns.fixed", {})
        timeline_fixed = {
            (norm(row.get("job")), skill_key(row.get("skill"))): as_number(row.get("value"))
            for row in JOB_MECHANICS.global_value("cooldowns.timeline_fixed_by_job", [])
            if isinstance(row, dict)
        }
        minimums = JOB_MECHANICS.global_value("summons.minimum_cooldowns", {})
        ammo_consumers = (
            ammo_rule.get("consumers", {}) if isinstance(ammo_rule, dict) else {}
        )
        for key, item in result.items():
            if key in {skill_key(value) for value in ammo_consumers}:
                continue
            override = timeline_fixed.get((job, key))
            if override is None:
                override = as_number(fixed.get(key)) if key in fixed else None
            if key in minimums:
                override = max(as_number(override), as_number(minimums.get(key)))
            if override is not None and override > 0:
                item.cooldown = override
        return result

    @staticmethod
    def _attrs_multiplier(attrs: dict[str, float], skill: PlannedSkill) -> float:
        physical_weight = max(0.0, as_number(skill.channels.get("物理")))
        magical_weight = max(0.0, as_number(skill.channels.get("魔法")))
        if physical_weight > 0.0 and magical_weight <= 0.0:
            attack_channel = "物攻"
        elif magical_weight > 0.0 and physical_weight <= 0.0:
            attack_channel = "魔攻"
        else:
            attack_channel = "双攻"
        attack = attack_increase_for_channel(
            attrs,
            attack_channel,
            physical_weight=physical_weight,
            magical_weight=magical_weight,
        )
        final = as_number(attrs.get("最终面板%"))
        element = as_number(attrs.get(f"{skill.element}攻%")) if skill.element else 0.0
        skill_attack = as_number(attrs.get("技能攻击力%"))
        first_target = as_number(attrs.get("首目标增伤%"))
        return (
            (1.0 + attack / 100.0)
            * (1.0 + final / 100.0)
            * (1.0 + element / 100.0)
            * (1.0 + (skill_attack + first_target) / 100.0)
        )

    @staticmethod
    def _active_attrs(
        windows: list[dict[str, Any]],
        seconds: float,
        target_skill: PlannedSkill | None = None,
    ) -> dict[str, float]:
        grouped: dict[tuple[str, str], float] = {}
        for window in windows:
            if window["start"] <= seconds < window["end"]:
                scope = {
                    skill_key(value)
                    for value in window.get("skillScope", ())
                    if norm(value)
                }
                if (
                    scope
                    and target_skill is not None
                    and skill_key(target_skill.skill) not in scope
                ):
                    continue
                for key, value in window.get("attrs", {}).items():
                    group = norm(window.get("skillKey") or window.get("skill"))
                    marker = (group, key)
                    grouped[marker] = max(grouped.get(marker, 0.0), as_number(value))
        attrs: dict[str, float] = defaultdict(float)
        for (_group, key), value in grouped.items():
            attrs[key] += value
        return dict(attrs)

    @staticmethod
    def _debuff_multiplier(windows: list[dict[str, Any]], skill: PlannedSkill, seconds: float) -> float:
        effects: list[EffectInput] = []
        for index, row in enumerate(windows):
            key = norm(row.get("key"))
            value = max(0.0, as_number(row.get("value")))
            if not key or value <= 0.0:
                continue
            effects.append(
                EffectInput(
                    key=key,
                    value=value,
                    skill_name=norm(row.get("skill") or row.get("skillKey")),
                    source_id=norm(row.get("source_id")),
                    target_id="boss",
                    instance_id=str(index),
                    start=as_number(row.get("start")),
                    end=as_number(row.get("end")),
                    stack_group=norm(row.get("group")),
                    stackable=bool(row.get("stackable")),
                )
            )
        resolved = EFFECT_STACKING_ENGINE.resolve_active(effects, seconds).values
        channel_amps: list[float] = []
        if "物理" in skill.channels:
            channel_amps.append(as_number(resolved.get("物理增伤合计%")))
        if "魔法" in skill.channels:
            channel_amps.append(as_number(resolved.get("魔法增伤合计%")))
        amp = sum(channel_amps) / len(channel_amps) if channel_amps else 0.0
        resist = as_number(resolved.get(f"减{skill.element}抗%"))
        crit = as_number(resolved.get("减致命抵抗%"))
        defense_channels: list[float] = []
        if "物理" in skill.channels:
            defense_channels.append(as_number(resolved.get("减物理防御%")))
        if "魔法" in skill.channels:
            defense_channels.append(as_number(resolved.get("减魔法防御%")))
        defense = (
            sum(defense_channels) / len(defense_channels)
            if defense_channels
            else 0.0
        )
        # Defense and critical resistance use a conservative marginal model in
        # this planner. Exact boss parameters are applied by live scoring.
        return (1.0 + amp / 100.0) * (1.0 + resist / 100.0) * (1.0 + defense / 200.0) * (1.0 + crit / 300.0)

    def plan(
        self,
        build: dict[str, Any],
        skill_rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
        *,
        external_effect_windows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        settings = settings or {}
        job = norm(build.get("second_job"))
        window = max(5.0, min(7200.0, as_number(settings.get("window"), 125.0)))
        blocked = _merge_intervals(settings.get("blockedIntervals", []), window)
        burst_intervals = _merge_intervals(settings.get("burstIntervals", []), window)
        accel_intervals = _merge_intervals(settings.get("accelIntervals", []), window)
        if settings.get("burst") and not burst_intervals:
            burst_intervals = [(0.0, min(14.0, window))]
        if settings.get("accel") and not accel_intervals:
            accel_intervals = [(0.0, min(15.0, window))]
        cocktail = bool(settings.get("cocktail", True))
        skills = self._skills(build, skill_rows)
        if not skills:
            return self._empty(job, window)

        profile = JOB_MECHANICS.job_profile(job)
        stance_skills = {
            skill_key(name): rule
            for name, rule in profile.get("stance_skills", {}).items()
            if isinstance(rule, dict)
        }
        cooldown_rules = self.store.skill_cooldown_rules().get("jobs", {}).get(job, {})
        if not isinstance(cooldown_rules, dict):
            cooldown_rules = {}
        excludes = {skill_key(value) for value in profile.get("auto_selection", {}).get("exclude_from_timeline", [])}
        # Zero-cooldown rows without an animation are passive/on-hit damage or
        # permanent states. They belong in damage valuation, never in the cast
        # sequence itself.
        candidates = [
            skill for key, skill in skills.items()
            if key not in excludes
            and not (skill.cooldown <= 0 and skill.cast_time <= 0.081 and not skill.non_damage)
        ]
        followups = {
            skill_key(key): tuple(skill_key(value) for value in values)
            for key, values in profile.get("combos", {}).get("followups", {}).items()
        }
        required = {
            skill_key(key): {skill_key(value) for value in values}
            for key, values in profile.get("combos", {}).get("required_predecessors", {}).items()
        }
        required_followups = {
            skill_key(key): tuple(skill_key(value) for value in values)
            for key, values in profile.get("combos", {}).get("required_followups", {}).items()
        }
        quick_predecessors = {
            skill_key(key): {skill_key(value) for value in values}
            for key, values in profile.get("combos", {}).get("quick_predecessors", {}).items()
        }
        dpa_packages = {
            skill_key(key): tuple(skill_key(value) for value in values)
            for key, values in profile.get("combos", {}).get("dpa_packages", {}).items()
        }
        opening = [skill_key(value) for value in profile.get("opening", [])]
        opening = [value for value in opening if value in skills]
        personal_buff_rules = self.store.buff_activation_rules().get("personal_buffs", {})
        continuous_buff_durations = {
            skill_key(skill): max(0.0, as_number(rule.get("duration")))
            for rule_key, rule in personal_buff_rules.items()
            for provider, _separator, skill in [norm(rule_key).partition("|")]
            if provider in set(self.store.job_buff_sources(job))
            and isinstance(rule, dict)
            and bool(rule.get("continuous_uptime"))
            and skill_key(skill) in skills
        }
        continuous_buff_keys = set(continuous_buff_durations)
        ammo_rule = profile.get("resources", {}).get("ammo", {})
        ammo_max = max(0, int(as_number(ammo_rule.get("max")))) if isinstance(ammo_rule, dict) else 0
        ammo = ammo_max
        ammo_gain = max(0, int(as_number(ammo_rule.get("gain")))) if isinstance(ammo_rule, dict) else 0
        ammo_reload = skill_key(ammo_rule.get("reload")) if isinstance(ammo_rule, dict) else ""
        ammo_costs = {
            skill_key(name): max(0, int(as_number(value)))
            for name, value in (
                ammo_rule.get("consumers", {}).items()
                if isinstance(ammo_rule, dict) and isinstance(ammo_rule.get("consumers"), dict)
                else ()
            )
        }
        summon_config = copy.deepcopy(JOB_MECHANICS.global_value("summons.limits", {}))
        summon_config.update(profile.get("summons", {}) if isinstance(profile.get("summons"), dict) else {})
        summon_active: dict[str, list[float]] = defaultdict(list)
        ready = {key: 0.0 for key in skills}
        # The rule table describes refunds; storage capacity belongs to the
        # learned skill level. In particular Nine Tail has two/three charges.
        stored_maxes: dict[str, int] = {}
        tree = self.store.skill_build_tree(job, 70)
        selected_levels = (build.get("skill_build") or {}).get("levels", {})
        for item in tree.get("skills", {}).values():
            key = skill_key(item.get("name"))
            selected = int(as_number(selected_levels.get(norm(item.get("id"))),
                                         skills[key].level if key in skills else 0))
            levels = [row for row in item.get("levels", []) if as_number(row.get("level")) <= selected]
            if selected > 0 and levels:
                desc = norm(max(levels, key=lambda row: as_number(row.get("level"))).get("desc"))
                matches = re.findall(r"冷却时间(?:可以|可)储存\s*(\d+)\s*次", desc)
                if matches:
                    stored_maxes[key] = max(int(value) for value in matches)
        charge_ready: dict[str, list[float]] = {}
        for key, skill in skills.items():
            charge_ready[key] = [0.0]
            rule = self.store.skill_cooldown_rules().get("jobs", {}).get(job, {}).get(skill.skill, {})
            charges = max(1, int(as_number(rule.get("charges"), 1))) if isinstance(rule, dict) else 1
            charges = max(charges, stored_maxes.get(key, 1))
            charge_ready[key] = [0.0] * charges

        external = copy.deepcopy(external_effect_windows or [])
        buff_windows: list[dict[str, Any]] = [
            row for row in external if row.get("kind") == "buff" or row.get("attrs")
        ]
        continuous_buff_until = {
            key: max(
                (
                    as_number(row.get("end"))
                    for row in buff_windows
                    if row.get("skillKey") == key
                ),
                default=0.0,
            )
            for key in continuous_buff_keys
        }
        debuff_windows: list[dict[str, Any]] = [
            row for row in external if row.get("kind") != "buff" and not row.get("attrs")
        ]
        segments: list[dict[str, Any]] = []
        queue = list(opening)
        now = 0.0
        previous = ""
        sequence_id = 0
        while now < window - 1e-6:
            blocked_end = self._blocked_end(blocked, now)
            if blocked_end is not None:
                now = blocked_end
                previous = ""
                continue
            for values in charge_ready.values():
                values.sort()
            for summon_key in list(summon_active):
                summon_active[summon_key] = [end for end in summon_active[summon_key] if end > now + 1e-6]

            def scheduled_cast_time(
                item: PlannedSkill,
                at: float,
                predecessor: str | None = None,
            ) -> float:
                attrs = self._active_attrs(buff_windows, at, item)
                action_speed = 40.0 if self._contains(burst_intervals, at) else 0.0
                action_speed += as_number(attrs.get("动作速度%")) + as_number(attrs.get("攻速%"))
                item_key = skill_key(item.skill)
                predecessor_key = previous if predecessor is None else skill_key(predecessor)
                base_cast_time = item.cast_time
                allowed_quick_predecessors = quick_predecessors.get(item_key, set())
                if allowed_quick_predecessors:
                    if (
                        predecessor_key in allowed_quick_predecessors
                        and item.quick_cast_time > 0.0
                    ):
                        base_cast_time = item.quick_cast_time
                    elif item.full_cast_time > 0.0:
                        base_cast_time = item.full_cast_time
                if skill_key(item.skill) in stance_skills:
                    return base_cast_time
                if is_ring_strike(item.skill):
                    return adjust_ring_strike_cast_time(base_cast_time, 1.0 + action_speed / 100.0)
                if is_fixed_duration_action_speed_output_skill(item.skill):
                    return adjust_fixed_duration_action_speed_cast_time(
                        item.skill,
                        base_cast_time,
                        1.0 + action_speed / 100.0,
                    )
                return base_cast_time / max(0.25, 1.0 + action_speed / 100.0)

            def effective_cooldown(item: PlannedSkill, at: float) -> float:
                attrs = self._active_attrs(buff_windows, at, item)
                cdr = 12.0 if cocktail else 0.0
                if self._contains(burst_intervals, at):
                    cdr += 40.0
                if self._contains(accel_intervals, at):
                    cdr += 70.0
                cdr += as_number(attrs.get("冷却缩减%"))
                base = self.cooldown_engine.calculate(job, item.skill, item.cooldown, 0.0)
                return max(scheduled_cast_time(item, at), base * (1.0 - min(70.0, cdr) / 100.0))

            def required_chain_ready(item: PlannedSkill) -> bool:
                root_key = skill_key(item.skill)
                targets = required_followups.get(root_key, ())
                if not targets:
                    return True
                root_end = now + scheduled_cast_time(item, now)
                for target in targets:
                    followup = skills.get(target)
                    if followup is None or min(charge_ready.get(target, [float("inf")])) > root_end + 1e-6:
                        continue
                    if root_end + scheduled_cast_time(followup, root_end, root_key) <= window + 1e-6:
                        return True
                return False

            # A DPA package opener remains usable by itself while the linked
            # skill is far away. Reserve it only at a finite-window boundary
            # where spending it now would reduce the linked skill's cast count.
            for item in candidates:
                root_key = skill_key(item.skill)
                if not dpa_packages.get(root_key):
                    continue
                root_values = charge_ready.get(root_key, [])
                if not root_values or min(root_values) > now + 1e-6:
                    continue
                root_cast = scheduled_cast_time(item, now)
                next_root_ready = now + effective_cooldown(item, now)
                reservations: list[float] = []
                for target in dpa_packages[root_key]:
                    followup = skills.get(target)
                    target_values = charge_ready.get(target, [])
                    if followup is None or not target_values:
                        continue
                    target_ready = min(target_values)
                    if target_ready <= now + root_cast + 1e-6 or target_ready >= next_root_ready - 1e-6:
                        continue
                    reserve_at = max(now, target_ready - root_cast)
                    followup_cast = scheduled_cast_time(followup, target_ready, root_key)
                    followup_cooldown = effective_cooldown(followup, target_ready)

                    def remaining_uses(first_start: float) -> int:
                        if first_start + followup_cast > window + 1e-6:
                            return 0
                        if followup_cooldown <= 1e-6:
                            return 1
                        return 1 + int(
                            max(0.0, window - first_start - followup_cast)
                            // followup_cooldown
                        )

                    if remaining_uses(target_ready) > remaining_uses(next_root_ready + root_cast):
                        reservations.append(reserve_at)
                if reservations:
                    slot = min(range(len(root_values)), key=root_values.__getitem__)
                    root_values[slot] = min(reservations)

            available = [
                item for item in candidates
                if min(charge_ready[skill_key(item.skill)]) <= now + 1e-6
                and (skill_key(item.skill) not in required or previous in required[skill_key(item.skill)])
                and required_chain_ready(item)
                and ammo_costs.get(skill_key(item.skill), 0) <= ammo
                and not (skill_key(item.skill) == ammo_reload and (ammo_max <= 0 or ammo >= ammo_max))
            ]
            selected: PlannedSkill | None = None
            if queue:
                target = queue.pop(0)
                candidate = skills.get(target)
                if candidate is not None and min(charge_ready[target]) <= now + 1e-6:
                    selected = candidate
            if selected is None and available and continuous_buff_keys:
                due_buffs = [
                    item for item in available
                    if skill_key(item.skill) in continuous_buff_keys
                    and continuous_buff_until.get(skill_key(item.skill), 0.0) <= now + 1e-6
                ]
                if due_buffs:
                    selected = min(due_buffs, key=lambda item: item.cast_time)
            if selected is None and available:
                def priority(item: PlannedSkill) -> tuple[float, float, float]:
                    key = skill_key(item.skill)
                    root_cast_time = scheduled_cast_time(item, now)
                    setup = 1.0 if (item.non_damage or item.attrs or item.effect_rows) else 0.0
                    active = any(row["start"] <= now < row["end"] and row.get("skillKey") == key for row in buff_windows + debuff_windows)
                    setup_bonus = setup * (2.0 if now < 5.0 and not active else 0.35 if not active else -1.0)
                    item_attrs = self._active_attrs(buff_windows, now, item)
                    score = item.output * self._attrs_multiplier(item_attrs, item) * self._debuff_multiplier(debuff_windows, item, now)
                    dpa = score / max(0.08, root_cast_time)
                    package_dpas: list[float] = []
                    root_end = now + root_cast_time
                    package_targets = {
                        *required_followups.get(key, ()),
                        *dpa_packages.get(key, ()),
                    }
                    for target in package_targets:
                        followup = skills.get(target)
                        if followup is None or min(charge_ready.get(target, [float("inf")])) > root_end + 1e-6:
                            continue
                        followup_cast_time = scheduled_cast_time(followup, root_end, key)
                        if root_end + followup_cast_time > window + 1e-6:
                            continue
                        followup_attrs = self._active_attrs(
                            buff_windows, root_end, followup
                        )
                        followup_score = (
                            followup.output
                            * self._attrs_multiplier(followup_attrs, followup)
                            * self._debuff_multiplier(debuff_windows, followup, root_end)
                        )
                        package_dpas.append(
                            (score + followup_score)
                            / max(0.08, root_cast_time + followup_cast_time)
                        )
                    if package_dpas:
                        dpa = max(package_dpas)
                    config = summon_config.get(key, {})
                    if isinstance(config, dict):
                        maximum = max(0, int(as_number(config.get("max"))))
                        if maximum and len(summon_active.get(key, [])) >= maximum:
                            # Early replacement remains legal, but only as an
                            # idle filler after productive casts have priority.
                            dpa *= 0.02
                            setup_bonus -= 2.0
                    return (dpa + setup_bonus * max(1.0, dpa), item.dps, -item.cast_time)
                selected = max(available, key=priority)
            if selected is None:
                next_ready = [value for values in charge_ready.values() for value in values if value > now + 1e-6]
                if not next_ready:
                    break
                now = min(window, min(next_ready))
                previous = ""
                continue

            key = skill_key(selected.skill)
            action_speed = 40.0 if self._contains(burst_intervals, now) else 0.0
            active_attrs = self._active_attrs(buff_windows, now, selected)
            action_speed += as_number(active_attrs.get("动作速度%")) + as_number(active_attrs.get("攻速%"))
            if is_ring_strike(selected.skill):
                cast_time = scheduled_cast_time(selected, now)
                speed_output = ring_strike_output_multiplier(1.0 + action_speed / 100.0)
            elif is_fixed_duration_action_speed_output_skill(selected.skill):
                cast_time = scheduled_cast_time(selected, now)
                speed_output = 1.0 + action_speed / 100.0
            elif key in stance_skills:
                cast_time = scheduled_cast_time(selected, now)
                stance_rule = stance_skills[key]
                base_count = max(0, int(as_number(stance_rule.get("base_count"))))
                base_multiplier = max(
                    0.01, as_number(stance_rule.get("base_multiplier"), 1.0)
                )
                count = max(
                    base_count,
                    int(base_count * (1.0 + action_speed / 100.0) + 1e-9),
                )
                accelerated_output = (
                    count
                    * max(0.0, as_number(stance_rule.get("per_hit_multiplier"), 1.0))
                    + max(0.0, as_number(stance_rule.get("fixed_multiplier")))
                )
                speed_output = accelerated_output / base_multiplier
            else:
                cast_time = scheduled_cast_time(selected, now)
                speed_output = 1.0
            if key not in continuous_buff_keys and continuous_buff_keys:
                reserved_key = ""
                reserved_at = 0.0
                for buff_key in continuous_buff_keys:
                    expiry = continuous_buff_until.get(buff_key, 0.0)
                    buff_ready = min(charge_ready.get(buff_key, [float("inf")]))
                    if (
                        expiry > now + 1e-6
                        and buff_ready <= expiry + 1e-6
                        and now + cast_time > expiry + 1e-6
                        and (not reserved_key or expiry < reserved_at)
                    ):
                        reserved_key = buff_key
                        reserved_at = max(now, buff_ready)
                if reserved_key:
                    queue.insert(0, reserved_key)
                    now = reserved_at
                    previous = ""
                    continue
            next_block = self._next_blocked_start(blocked, now)
            end = min(window, now + cast_time)
            if next_block is not None and now < next_block < end:
                # A cast may start before a mechanism. Damage after the boundary
                # is not counted, but the cast still completes and consumes CD.
                output_ratio = max(0.0, (next_block - now) / max(0.08, cast_time))
            else:
                output_ratio = 1.0
            output = 0.0
            if not selected.non_damage:
                output = selected.output * speed_output * output_ratio
                output *= self._attrs_multiplier(active_attrs, selected)
                output *= self._debuff_multiplier(debuff_windows, selected, now)
            sequence_id += 1
            segment = {
                "id": sequence_id,
                "skill": selected.skill,
                "start": now,
                "end": end,
                "output": output,
                "effectiveScore": selected.effective_percent * speed_output * output_ratio,
                "element": selected.element,
                "actionSpeed": action_speed,
                "kind": "support" if selected.non_damage else "damage",
                "buffs": copy.deepcopy(selected.attrs),
                "effects": copy.deepcopy(selected.effect_rows),
            }
            segments.append(segment)
            if key in continuous_buff_keys:
                duration = max(
                    selected.duration,
                    continuous_buff_durations.get(key, 0.0),
                )
                continuous_buff_until[key] = now + duration
            ammo_cost = ammo_costs.get(key, 0)
            if ammo_cost:
                ammo = max(0, ammo - ammo_cost)
            if key == ammo_reload:
                ammo = min(ammo_max, ammo + ammo_gain)
            summon_rule = summon_config.get(key, {})
            if isinstance(summon_rule, dict):
                maximum = max(0, int(as_number(summon_rule.get("max"))))
                duration = max(0.0, as_number(summon_rule.get("duration")))
                if maximum > 0 and duration > 0:
                    active = summon_active[key]
                    if len(active) >= maximum:
                        active.remove(min(active))
                    active.append(end + duration)
            if selected.attrs and selected.duration > 0:
                buff_windows.append({
                    "skill": selected.skill,
                    "skillKey": key,
                    "provider": job,
                    "start": now,
                    "end": min(window, now + selected.duration),
                    "attrs": copy.deepcopy(selected.attrs),
                    "skillScope": list(selected.buff_skill_scope),
                    "kind": "buff",
                })
            for effect in selected.effect_rows:
                duration = max(
                    0.0,
                    as_number(effect.get("base_duration"), effect.get("duration")),
                )
                if duration <= 0:
                    continue
                effect_start, effect_end, _refresh_tail = (
                    team_effect_application_window_for_effect(
                        selected.skill,
                        norm(effect.get("key")),
                        now,
                        end,
                        duration,
                    )
                )
                debuff_windows.append({
                    "skill": selected.skill,
                    "skillKey": key,
                    "provider": job,
                    **copy.deepcopy(effect),
                    "start": effect_start,
                    "end": min(window, effect_end),
                })

            values = charge_ready[key]
            slot = min(range(len(values)), key=values.__getitem__)
            cdr = 12.0 if cocktail else 0.0
            if self._contains(burst_intervals, now):
                cdr += 40.0
            if self._contains(accel_intervals, now):
                cdr += 70.0
            cdr += as_number(active_attrs.get("冷却缩减%"))
            if selected.skill in {"精神爆发", "时间加速", "灼热火焰"}:
                cdr = 0.0
            selected_cooldown_rule = cooldown_rules.get(selected.skill, {})
            selected_sources = (
                selected_cooldown_rule.get("sources", [])
                if isinstance(selected_cooldown_rule, dict)
                else []
            )
            if isinstance(selected_sources, list) and selected_sources:
                passive = min(
                    0.99,
                    max(0.0, as_number(selected_cooldown_rule.get("passive_percent"))),
                )
                internal_cooldown = max(
                    0.0,
                    selected.cooldown * (1.0 - passive)
                    - max(0.0, as_number(selected_cooldown_rule.get("self_refund_seconds"))),
                )
                internal_cooldown /= 1.0 + max(
                    0.0,
                    as_number(selected_cooldown_rule.get("extra_resets")),
                )
            else:
                internal_cooldown = self.cooldown_engine.calculate(
                    job, selected.skill, selected.cooldown, 0.0
                )
            values[slot] = now + max(
                cast_time,
                internal_cooldown * (1.0 - min(70.0, cdr) / 100.0),
            )
            source_key = skill_key(selected.skill)
            external_factor = 1.0 - min(70.0, cdr) / 100.0
            for target_skill, target_rule in cooldown_rules.items():
                if not isinstance(target_rule, dict):
                    continue
                target_key = skill_key(target_skill)
                target_values = charge_ready.get(target_key)
                if not target_values:
                    continue
                for source in target_rule.get("sources", []):
                    if (
                        not isinstance(source, dict)
                        or skill_key(source.get("skill")) != source_key
                    ):
                        continue
                    source_type = norm(source.get("type"))
                    if source_type == "grant_charge":
                        # One stored use at completion, never reset all slots.
                        # Refund the furthest slot to retain natural progress.
                        missing = sorted(
                            (i for i, ready_at in enumerate(target_values) if ready_at > end + 1e-6),
                            key=target_values.__getitem__, reverse=True,
                        )
                        for index in missing[:max(0, int(as_number(source.get("value"))))]:
                            target_values[index] = end
                        continue
                    if source_type == "reset":
                        for index, ready_at in enumerate(target_values):
                            if ready_at > now + 1e-6:
                                target_values[index] = now
                        continue
                    if source_type == "fixed_seconds":
                        reduction = max(0.0, as_number(source.get("value"))) * external_factor
                    elif source_type == "target_percent":
                        target_base = max(
                            0.0,
                            as_number(cooldown_rules.get(target_skill, {}).get("base_cooldown_fallback")),
                        )
                        reduction = (
                            target_base
                            * max(0.0, as_number(source.get("value")))
                            * external_factor
                        )
                    else:
                        continue
                    for index, ready_at in enumerate(target_values):
                        if ready_at > now + 1e-6:
                            target_values[index] = max(now, ready_at - reduction)
            follow = [value for value in followups.get(key, ()) if value in skills]
            if follow:
                queue = [follow[0]] + queue
            previous = key
            now = max(end, now + 0.01)

        return self._result(job, window, segments, buff_windows, debuff_windows, blocked)

    @staticmethod
    def _empty(job: str, window: float) -> dict[str, Any]:
        return {
            "job": job, "window": window, "segments": [], "total": 0.0,
            "dps": 0.0, "contribution": [], "effectWindows": [],
            "blockedIntervals": [], "planner": "V4.10 unified native planner",
        }

    @staticmethod
    def _result(
        job: str,
        window: float,
        segments: list[dict[str, Any]],
        buffs: list[dict[str, Any]],
        debuffs: list[dict[str, Any]],
        blocked: list[tuple[float, float]],
    ) -> dict[str, Any]:
        total = sum(as_number(row.get("output")) for row in segments)
        contribution: Counter[str] = Counter()
        counts: Counter[str] = Counter()
        for row in segments:
            if as_number(row.get("output")) > 0:
                contribution[norm(row.get("skill"))] += as_number(row.get("output"))
                counts[norm(row.get("skill"))] += 1
        return {
            "job": job,
            "window": window,
            "segments": segments,
            "total": total,
            "dps": total / window if window else 0.0,
            "contribution": [
                {"skill": skill, "count": counts[skill], "output": output, "share": output / total if total else 0.0}
                for skill, output in contribution.most_common()
            ],
            "effectWindows": [*buffs, *debuffs],
            "blockedIntervals": [list(value) for value in blocked],
            "planner": "V4.10 unified native planner",
        }
