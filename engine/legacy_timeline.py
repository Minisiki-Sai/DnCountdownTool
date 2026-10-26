from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
import types
from collections import OrderedDict
from typing import Any

from .acceleration_resistance import (
    BURST_RESISTANCE_GROUP,
    TIME_ACCEL_RESISTANCE_GROUP,
    acceleration_resistance_group,
    is_acceleration_resistance_skill,
    resistance_windows_from_schedule,
    schedule_acceleration_resistance,
)
from .cooldown import SkillCooldownEngine
from .data_loader import as_number, norm
from .timeline_engine import PureTimelineRuntime, TimelineSimulationSnapshot


class _QtPlaceholderMeta(type):
    def __getattr__(cls, _name: str) -> int:
        return 0


class _QtPlaceholder(metaclass=_QtPlaceholderMeta):
    """Import-only Qt stand-in; no instance is used by the pure runtime."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __getattr__(self, _name: str) -> "_QtPlaceholder":
        return _QtPlaceholder()

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_QtPlaceholder":
        return _QtPlaceholder()

    def __bool__(self) -> bool:
        return False


class _QtPlaceholderModule(types.ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        if name == "PySide6":
            self.__path__ = []

    def __getattr__(self, name: str) -> Any:
        if name == "Signal":
            return lambda *_args, **_kwargs: _QtPlaceholder()
        if name == "isValid":
            return lambda _value: True
        if name == "QApplication":
            class _ApplicationPlaceholder(_QtPlaceholder):
                @staticmethod
                def processEvents() -> None:
                    pass

            return _ApplicationPlaceholder
        return _QtPlaceholder


def _legacy_method_source_type() -> type:
    """Load the V4.01 method registry without making V4.10 depend on Qt."""

    from . import editor as loaded_editor

    return loaded_editor.BuildEditor


def _load_legacy_method_source_type() -> type:
    if os.getenv("DRAGON_NEST_NATIVE_NO_QT") != "1":
        try:
            return _legacy_method_source_type()
        except ModuleNotFoundError as exc:
            if not (exc.name or "").startswith("PySide6"):
                raise

    module_names = (
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "shiboken6",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    try:
        for name in module_names:
            sys.modules[name] = _QtPlaceholderModule(name)
        return _legacy_method_source_type()
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class LegacyTimelineService:
    """Expose the exact V4.01 planner as JSON-safe data for the Vue client."""

    def __init__(self, store: Any, calculator: Any) -> None:
        self.store = store
        self.calculator = calculator
        self.cooldown_engine = SkillCooldownEngine(
            store.skill_cooldown_rules(),
            store.skill_reference_cooldown,
        )
        self._method_source_type: type | None = None
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._scenario_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def _source_type(self) -> type:
        if self._method_source_type is None:
            self._method_source_type = _load_legacy_method_source_type()
        return self._method_source_type

    @staticmethod
    def _hash_payload(payload: Any) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _burst_schedule(settings: dict[str, Any]) -> tuple[dict[str, Any] | None, frozenset[str]]:
        practical = bool(settings.get("practical"))
        burst = bool(settings.get("burst"))
        accel = bool(settings.get("accel"))
        cocktail = bool(settings.get("cocktail"))
        if practical or not any((burst, accel, cocktail)):
            return None, frozenset({"practical"})
        modes = {
            name
            for name, enabled in (
                ("burst", burst),
                ("accel", accel),
                ("cocktail", cocktail),
            )
            if enabled
        }
        return None, frozenset(modes)

    @staticmethod
    def _long_schedule(settings: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
        window = max(30.0, min(720.0, as_number(settings.get("window"), 125.0)))
        burst_duration = 14.0
        accel_duration = 15.0
        burst_intervals = settings.get("burstIntervals")
        if isinstance(burst_intervals, list):
            burst_rows = burst_intervals
        elif bool(settings.get("burst")):
            start = max(0.0, as_number(settings.get("burstStart"), 0.0))
            interval = max(1.0, as_number(settings.get("burstInterval"), 50.0))
            burst_rows = []
            while start < window - 1e-6 and len(burst_rows) < 1000:
                burst_rows.append((start, min(window, start + burst_duration)))
                start += interval
        else:
            burst_rows = []

        accel_intervals = settings.get("accelIntervals")
        if isinstance(accel_intervals, list):
            accel_rows = accel_intervals
        elif bool(settings.get("accel")):
            start = max(0.0, as_number(settings.get("accelStart"), 60.0))
            accel_rows = []
            while start < window - 1e-6 and len(accel_rows) < 1000:
                accel_rows.append((start, min(window, start + accel_duration)))
                start += 100.0
        else:
            accel_rows = []

        return {
            "window": window,
            "burst": burst_rows,
            "accel": accel_rows,
            "cocktail": bool(settings.get("cocktail", True)),
        }, frozenset()

    @classmethod
    def _runtime_values(cls, settings: dict[str, Any]) -> dict[str, Any]:
        mode = norm(settings.get("mode")) or "long"
        if mode == "burst":
            schedule, modes = cls._burst_schedule(settings)
        else:
            schedule, modes = cls._long_schedule(settings)
        blocked = settings.get("blockedIntervals")
        duplicate_buffs = settings.get("duplicateBuildBuffs", [])
        support_initial_times = settings.get("supportInitialTimes", {})
        pinned_special = settings.get("pinnedSpecialSkillKeys", [])
        support_cast_schedule = settings.get("supportCastSchedule", {})
        resistance_windows = settings.get("resistanceWindows", [])
        return {
            "_timeline_extreme_mode": False,
            "_skill_burst_schedule_override": copy.deepcopy(schedule),
            "_skill_burst_mode_override": copy.deepcopy(modes),
            "_skill_burst_blocked_intervals": copy.deepcopy(blocked if isinstance(blocked, list) else []),
            "_skill_burst_external_team_effect_windows": copy.deepcopy(
                settings.get("externalEffectWindows", [])
                if isinstance(settings.get("externalEffectWindows"), list)
                else []
            ),
            "_skill_burst_current_provider_override": norm(settings.get("currentProvider")),
            "_team_practical_duplicate_build_buffs": {
                norm(value) for value in duplicate_buffs if norm(value)
            } if isinstance(duplicate_buffs, (list, tuple, set, frozenset)) else set(),
            "_skill_burst_support_initial_times": copy.deepcopy(
                support_initial_times if isinstance(support_initial_times, dict) else {}
            ),
            "_skill_burst_pinned_special_skill_keys": {
                norm(value) for value in pinned_special if norm(value)
            } if isinstance(pinned_special, (list, tuple, set, frozenset)) else set(),
            "_skill_burst_pinned_support_cast_schedule": copy.deepcopy(
                support_cast_schedule if isinstance(support_cast_schedule, dict) else {}
            ),
            "_skill_burst_resistance_windows": copy.deepcopy(
                resistance_windows if isinstance(resistance_windows, list) else []
            ),
            "_skill_burst_standalone_external_resistance": bool(
                settings.get("standaloneAccelerationResistance")
            ),
            "_team_freeze_coefficient_override": max(
                0.0, as_number(settings.get("freezeCoefficient"), 0.3)
            ),
        }

    @classmethod
    def _standalone_acceleration_resistance_settings(
        cls,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach authoritative external acceleration locks to a skill axis."""

        resolved = copy.deepcopy(settings)
        existing = resolved.get("resistanceWindows")
        if isinstance(existing, list) and existing:
            return resolved

        mode = norm(resolved.get("mode")) or "long"
        if mode == "long":
            schedule, _modes = cls._long_schedule(resolved)
            window = max(0.0, as_number(schedule.get("window")))
            burst_rows = list(schedule.get("burst", ()))
            accel_rows = list(schedule.get("accel", ()))
        else:
            if bool(resolved.get("practical")):
                return resolved
            burst_rows = [(0.0, 14.0)] if bool(resolved.get("burst")) else []
            accel_rows = [(0.0, 15.0)] if bool(resolved.get("accel")) else []
            window = max(
                [end for _start, end in (*burst_rows, *accel_rows)] or [0.0]
            )
        if window <= 0 or not (burst_rows or accel_rows):
            return resolved

        specs: list[dict[str, Any]] = []
        explicit: dict[str, list[Any]] = {}
        if burst_rows:
            specs.append({
                "provider": "外部精神爆发",
                "skill": "精神爆发",
                "cooldown": max(
                    0.01,
                    as_number(
                        resolved.get("burstResistanceCooldown"),
                        resolved.get("burstCooldown", 44.0),
                    ),
                ),
                "duration": 14.0,
                "cast_time": 0.0,
            })
            explicit["精神爆发"] = burst_rows
        if accel_rows:
            specs.append({
                "provider": "外部时间加速",
                "skill": "时间加速",
                "cooldown": max(
                    0.01,
                    as_number(
                        resolved.get("accelResistanceCooldown"),
                        resolved.get("accelCooldown", 100.0),
                    ),
                ),
                "duration": 15.0,
                "cast_time": 0.0,
            })
            explicit["时间加速"] = accel_rows
        scheduled = schedule_acceleration_resistance(
            specs,
            window,
            explicit_intervals=explicit,
            blocked_ranges=[
                row
                for row in resolved.get("blockedIntervals", ())
                if isinstance(row, (list, tuple)) and len(row) >= 2
            ],
        )
        resolved["burstIntervals"] = [
            [row["effect_start"], row["effect_end"]]
            for row in scheduled
            if row.get("resistance_group") == BURST_RESISTANCE_GROUP
        ]
        resolved["accelIntervals"] = [
            [row["effect_start"], row["effect_end"]]
            for row in scheduled
            if row.get("resistance_group") == TIME_ACCEL_RESISTANCE_GROUP
        ]
        resolved["resistanceWindows"] = resistance_windows_from_schedule(
            scheduled, window
        )
        resolved["standaloneAccelerationResistance"] = True
        return resolved

    def _runtime(self, build: dict[str, Any], settings: dict[str, Any]) -> PureTimelineRuntime:
        effects = settings.get("effects")
        effect_rows = copy.deepcopy(effects if isinstance(effects, list) else [])
        snapshot = TimelineSimulationSnapshot.create(
            build,
            effect_rows,
            self._runtime_values(settings),
        )
        return PureTimelineRuntime(
            snapshot,
            store=self.store,
            calculator=self.calculator,
            cooldown_engine=self.cooldown_engine,
            method_source_type=self._source_type(),
            level_cap=70,
        )

    def extreme_runtime(self, build: dict[str, Any]) -> PureTimelineRuntime:
        """Create the same data-only extreme/team runtime used by V4.01."""

        snapshot = TimelineSimulationSnapshot.create(
            build,
            [],
            {"_timeline_extreme_mode": True},
        )
        source_type = self._source_type()
        runtime = PureTimelineRuntime(
            snapshot,
            store=self.store,
            calculator=self.calculator,
            cooldown_engine=self.cooldown_engine,
            method_source_type=source_type,
            level_cap=70,
        )
        runtime._EXTREME_PARALLEL_RUNTIME_ATTRS = (
            source_type._EXTREME_PARALLEL_RUNTIME_ATTRS
        )
        return runtime

    @staticmethod
    def _special_intervals(
        effects: list[dict[str, Any]],
        skill: str,
        window: float,
        preferred_start: float,
        explicit: Any,
    ) -> tuple[list[tuple[float, float]], dict[str, list[tuple[float, float]]]]:
        matching: dict[str, dict[str, Any]] = {}
        for effect in effects:
            effect_skill = norm(effect.get("skill")).split("｜")[-1]
            provider = norm(effect.get("provider"))
            if effect_skill == skill and provider and provider not in matching:
                matching[provider] = effect
        providers = sorted(matching)
        if not providers:
            return [], {}
        if isinstance(explicit, list) and explicit:
            rows = [
                (
                    max(0.0, as_number(row[0])),
                    min(window, max(0.0, as_number(row[1]))),
                )
                for row in explicit
                if isinstance(row, (list, tuple)) and len(row) >= 2
            ]
            rows = [(start, end) for start, end in rows if end > start + 1e-6]
            assigned = {provider: [] for provider in providers}
            for index, row in enumerate(rows):
                assigned[providers[index % len(providers)]].append(row)
            return rows, assigned

        cooldown = max(
            1.0,
            min(max(1.0, as_number(effect.get("cooldown"), 50.0)) for effect in matching.values()),
        )
        duration = max(
            0.1,
            max(as_number(effect.get("duration"), 14.0) for effect in matching.values()),
        )
        assigned = {provider: [] for provider in providers}
        if skill == "时间加速" and len(providers) == 2 and window <= 125.0 + 1e-6:
            starts = [30.0, 90.0]
            for index, start in enumerate(starts):
                if start < window - 1e-6:
                    assigned[providers[index]].append((start, min(window, start + duration)))
        else:
            for index, provider in enumerate(providers):
                start = max(0.0, preferred_start) + cooldown * index / len(providers)
                while start < window - 1e-6:
                    assigned[provider].append((start, min(window, start + duration)))
                    start += cooldown
        rows = sorted(
            (row for provider_rows in assigned.values() for row in provider_rows),
            key=lambda row: (row[0], row[1]),
        )
        return rows, assigned

    def team_settings(
        self,
        build: dict[str, Any],
        jobs: list[str],
        member_index: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the V4.01 personal-team context without reading Qt state."""

        resolved = copy.deepcopy(settings)
        resolved["mode"] = "long"
        window = max(30.0, as_number(resolved.get("window"), 125.0))
        runtime = self._runtime(build, resolved)
        _rows, summary = self.calculator.calculate(copy.deepcopy(build))
        metrics = self._selected_metrics(runtime, build, summary)

        team_sim = build.get("team_sim", {}) if isinstance(build.get("team_sim"), dict) else {}
        disabled_effects = {
            norm(value) for value in resolved.get("disabledTeamEffects", team_sim.get("disabled_effects", []))
            if norm(value)
        }
        cdr_usage = resolved.get("cdrUsage", team_sim.get("cdr_usage", {}))
        cdr_usage = cdr_usage if isinstance(cdr_usage, dict) else {}
        cdr_glyphs = resolved.get("cdrGlyphs", team_sim.get("cdr_glyphs", {}))
        cdr_glyphs = cdr_glyphs if isinstance(cdr_glyphs, dict) else {}
        cdr_applies = resolved.get("cdrApplies", team_sim.get("cdr_applies", {}))
        cdr_applies = cdr_applies if isinstance(cdr_applies, dict) else {}
        effect_overrides = resolved.get("teamEffectOverrides", team_sim.get("effect_overrides", {}))
        effect_overrides = effect_overrides if isinstance(effect_overrides, dict) else {}

        effects = runtime._dedup_team_effects(runtime._team_effects_for_jobs(jobs))
        selected_effects: list[dict[str, Any]] = []
        glyph_reduction = {"精神爆发": 12.0, "时间加速": 6.0}
        for effect in effects:
            skill = runtime._team_effect_skill_name(effect.get("skill"))
            effect_id = f"{norm(effect.get('provider'))}|{skill}"
            if effect_id in disabled_effects:
                continue
            copied = copy.deepcopy(effect)
            override = effect_overrides.get(effect_id, {})
            if isinstance(override, dict):
                level = norm(override.get("level"))
                if level and level != norm(copied.get("level")):
                    copied["level"] = level
                    attrs = runtime._team_effect_attrs(skill, level, copied.get("provider"))
                    if attrs:
                        copied["attrs"] = attrs
                for key in ("cooldown", "duration"):
                    if key in override:
                        copied[key] = max(0.0, as_number(override.get(key)))
            copied["usage_ratio"] = max(0.0, min(1.0, as_number(cdr_usage.get(skill), copied.get("usage_ratio", 1.0))))
            if isinstance(override, dict) and "usageRatio" in override:
                copied["usage_ratio"] = max(0.0, min(1.0, as_number(override.get("usageRatio"), copied["usage_ratio"])))
            reduction = glyph_reduction.get(skill, 0.0)
            if reduction > 0 and bool(cdr_glyphs.get(skill, True)):
                copied["cooldown"] = max(0.0, as_number(copied.get("cooldown")) * (1.0 - reduction / 100.0))
                copied["coverage"] = runtime._team_effect_coverage(copied["cooldown"], copied.get("duration", 0))
                copied["effective_attrs"] = runtime._team_effective_attrs(copied.get("attrs", {}), copied["cooldown"], copied.get("duration", 0))
            else:
                copied["coverage"] = runtime._team_effect_coverage(copied.get("cooldown", 0), copied.get("duration", 0))
                copied["effective_attrs"] = runtime._team_effective_attrs(copied.get("attrs", {}), copied.get("cooldown", 0), copied.get("duration", 0))
            selected_effects.append(copied)
        effects = selected_effects
        applied_cdr = {
            runtime._team_effect_skill_name(effect.get("skill"))
            for effect in effects
            if as_number(effect.get("attrs", {}).get("冷却缩减%")) > 0
            and bool(cdr_applies.get(runtime._team_effect_skill_name(effect.get("skill")), False))
        }
        effects = runtime._team_effects_with_cdr_adjusted_cooldowns(effects, applied_cdr)
        provider = f"队员{member_index + 1} {jobs[member_index]}"
        relevant_resists = runtime._skill_burst_relevant_resist_attrs(metrics)
        elemental_resists = {"减火抗%", "减水抗%", "减光抗%", "减暗抗%"}
        attack_attr = norm(runtime._job_profile().get("attack_attr"))
        irrelevant_amp = {"魔法增伤%", "魔法伤害增加%"} if attack_attr == "物攻" else (
            {"物理增伤%", "物理伤害增加%"} if attack_attr == "魔攻" else set()
        )
        prepared: list[dict[str, Any]] = []
        for effect in effects:
            skill = runtime._team_effect_skill_name(effect.get("skill"))
            if (
                norm(effect.get("provider")) == provider
                and not is_acceleration_resistance_skill(skill)
            ):
                continue
            copied = copy.deepcopy(effect)
            display_attrs = {
                key: value
                for key, value in copied.get("attrs", {}).items()
                if norm(key) not in irrelevant_amp
            }
            had_resist = any(norm(key) in elemental_resists for key in display_attrs)
            for key in list(display_attrs):
                if norm(key) in elemental_resists and norm(key) not in relevant_resists:
                    display_attrs.pop(key, None)
            if had_resist and not display_attrs:
                continue
            active_attrs = dict(display_attrs)
            active_attrs.pop("冷却缩减%", None)
            if is_acceleration_resistance_skill(skill):
                active_attrs.pop("动作速度%", None)
                active_attrs.pop("攻速%", None)
            copied["display_attrs"] = display_attrs
            copied["attrs"] = active_attrs
            prepared.append(copied)

        resistance_specs: list[dict[str, Any]] = []
        for effect in prepared:
            skill = runtime._team_effect_skill_name(effect.get("skill"))
            if not is_acceleration_resistance_skill(skill):
                continue
            effect_provider = norm(effect.get("provider"))
            provider_index = next(
                (
                    index
                    for index, job_name in enumerate(jobs)
                    if effect_provider == f"队员{index + 1} {job_name}"
                ),
                9999,
            )
            resistance_specs.append({
                "provider": effect_provider,
                "provider_order": provider_index,
                "skill": skill,
                "cooldown": max(0.01, as_number(effect.get("cooldown"))),
                "duration": max(0.0, as_number(effect.get("duration"))),
                "cast_time": runtime._team_practical_effect_cast_time(effect),
            })
        resistance_schedule = schedule_acceleration_resistance(
            resistance_specs,
            window,
            preferred_starts={
                "精神爆发": as_number(resolved.get("burstStart"), 0.0),
                "变异精神爆发": as_number(resolved.get("burstStart"), 0.0),
                "灼热火焰": as_number(resolved.get("burstStart"), 0.0),
                "变异猎人之眼": as_number(resolved.get("burstStart"), 0.0),
                "时间加速": as_number(resolved.get("accelStart"), 60.0),
                "变异时间加速": as_number(resolved.get("accelStart"), 60.0),
            },
            explicit_intervals={
                "精神爆发": resolved.get("burstIntervals", []),
                "时间加速": resolved.get("accelIntervals", []),
            },
            blocked_ranges=[
                row
                for row in resolved.get("blockedIntervals", [])
                if isinstance(row, (list, tuple)) and len(row) >= 2
            ],
        )
        burst = [
            (row["effect_start"], row["effect_end"])
            for row in resistance_schedule
            if row["resistance_group"] == BURST_RESISTANCE_GROUP
        ]
        accel = [
            (row["effect_start"], row["effect_end"])
            for row in resistance_schedule
            if row["resistance_group"] == TIME_ACCEL_RESISTANCE_GROUP
        ]
        cocktail = bool(resolved.get("cocktail", True)) and any(
            runtime._team_effect_skill_name(effect.get("skill")) == "鸡尾酒"
            for effect in prepared
        )
        schedule = {"window": window, "burst": burst, "accel": accel, "cocktail": cocktail}
        windows = runtime._team_practical_effect_windows(
            [
                effect
                for effect in prepared
                if not is_acceleration_resistance_skill(
                    runtime._team_effect_skill_name(effect.get("skill"))
                )
            ],
            window,
        )
        color_by_category = {
            "team_buff": "#7b61ff",
            "team_amp": "#ef6c57",
            "team_resist": "#30a6a6",
            "team_defense": "#5a8dee",
            "team_crit_resist": "#9a75d6",
            "team_freeze": "#4bb4e6",
        }
        assigned_by_skill: dict[str, dict[str, list[tuple[float, float]]]] = {}
        cast_schedule_by_provider: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for row in resistance_schedule:
            assigned_by_skill.setdefault(row["skill"], {}).setdefault(
                row["provider"], []
            ).append((row["effect_start"], row["effect_end"]))
            cast_schedule_by_provider.setdefault(
                (row["provider"], row["skill"]), []
            ).append((row["cast_start"], row["cast_end"]))
        for skill, assigned in assigned_by_skill.items():
            effect_by_provider = {
                norm(effect.get("provider")): effect
                for effect in prepared
                if runtime._team_effect_skill_name(effect.get("skill")) == skill
            }
            for effect_provider, intervals in assigned.items():
                effect = effect_by_provider.get(effect_provider)
                if not effect:
                    continue
                _effect_type, category = runtime._team_practical_effect_type(effect)
                attrs_text = "、".join(
                    f"{key}{as_number(value):g}"
                    for key, value in effect.get("display_attrs", {}).items()
                    if abs(as_number(value)) > 1e-9
                )
                for start, end in intervals:
                    windows.append({
                        "name": "｜".join(filter(None, [skill, attrs_text])),
                        "start": start,
                        "end": end,
                        "color": color_by_category.get(category, "#7b61ff"),
                        "detail": f"与长时间实战模拟同步；{attrs_text}",
                        "category": category,
                        "source": skill,
                        "team_effect": effect,
                    })

        resistance_windows = resistance_windows_from_schedule(
            resistance_schedule,
            window,
        )
        windows.extend(resistance_windows)
        own_special = {
            skill
            for skill, assigned in assigned_by_skill.items()
            if assigned.get(provider)
        }
        support_initial_times = {
            skill: cast_schedule_by_provider[(provider, skill)][0][0]
            for skill in own_special
            if cast_schedule_by_provider.get((provider, skill))
        }
        support_cast_schedule = {
            skill: casts
            for (cast_provider, skill), casts in cast_schedule_by_provider.items()
            if cast_provider == provider
        }
        duplicate_buffs = runtime._team_duplicate_build_buff_names(prepared, build)
        resolved.update({
            "window": window,
            "effects": prepared,
            "externalEffectWindows": windows,
            "burstIntervals": burst,
            "accelIntervals": accel,
            "cocktail": cocktail,
            "currentProvider": provider,
            "duplicateBuildBuffs": sorted(duplicate_buffs),
            "supportInitialTimes": support_initial_times,
            "supportCastSchedule": support_cast_schedule,
            "pinnedSpecialSkillKeys": sorted(own_special),
            "resistanceWindows": resistance_windows,
        })
        return resolved

    def team_effect_catalog(
        self,
        build: dict[str, Any],
        jobs: list[str],
        settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the old team-effect table as serializable data for V4.10."""

        resolved = copy.deepcopy(settings or {})
        runtime = self._runtime(build, {"mode": "long", "window": resolved.get("window", 125.0)})
        team_sim = build.get("team_sim", {}) if isinstance(build.get("team_sim"), dict) else {}
        disabled_values = resolved.get("disabledTeamEffects", team_sim.get("disabled_effects", []))
        disabled = {norm(value) for value in disabled_values if norm(value)}
        effect_overrides = resolved.get("teamEffectOverrides", team_sim.get("effect_overrides", {}))
        effect_overrides = effect_overrides if isinstance(effect_overrides, dict) else {}
        cdr_usage = resolved.get("cdrUsage", team_sim.get("cdr_usage", {}))
        cdr_usage = cdr_usage if isinstance(cdr_usage, dict) else {}
        cdr_glyphs = resolved.get("cdrGlyphs", team_sim.get("cdr_glyphs", {}))
        cdr_glyphs = cdr_glyphs if isinstance(cdr_glyphs, dict) else {}
        glyph_reduction = {"精神爆发": 12.0, "时间加速": 6.0}

        rows: list[dict[str, Any]] = []
        for effect in runtime._dedup_team_effects(runtime._team_effects_for_jobs(jobs)):
            copied = copy.deepcopy(effect)
            skill = runtime._team_effect_skill_name(copied.get("skill"))
            effect_id = f"{norm(copied.get('provider'))}|{skill}"
            override = effect_overrides.get(effect_id, {})
            if isinstance(override, dict):
                level = norm(override.get("level"))
                if level and level != norm(copied.get("level")):
                    copied["level"] = level
                    attrs = runtime._team_effect_attrs(skill, level, copied.get("provider"))
                    if attrs:
                        copied["attrs"] = attrs
                for key in ("cooldown", "duration"):
                    if key in override:
                        copied[key] = max(0.0, as_number(override.get(key)))
            copied["usage_ratio"] = max(
                0.0,
                min(1.0, as_number(cdr_usage.get(skill), copied.get("usage_ratio", 1.0))),
            )
            if isinstance(override, dict) and "usageRatio" in override:
                copied["usage_ratio"] = max(
                    0.0,
                    min(1.0, as_number(override.get("usageRatio"), copied["usage_ratio"])),
                )
            reduction = glyph_reduction.get(skill, 0.0)
            if reduction > 0 and bool(cdr_glyphs.get(skill, True)):
                copied["cooldown"] = max(
                    0.0,
                    as_number(copied.get("cooldown")) * (1.0 - reduction / 100.0),
                )
            copied["coverage"] = runtime._team_effect_coverage(
                copied.get("cooldown", 0), copied.get("duration", 0),
            )
            copied["effective_attrs"] = runtime._team_effective_attrs(
                copied.get("attrs", {}), copied.get("cooldown", 0), copied.get("duration", 0),
            )
            rows.append(copied)
        full_summary, _full_attrs = runtime._team_summary_with_effects(rows)
        full_panel = as_number(full_summary.get("effective_panel"))
        base_summary, _base_attrs = runtime._team_summary_with_effects([])
        base_panel = as_number(base_summary.get("effective_panel"))
        result: list[dict[str, Any]] = []
        for index, effect in enumerate(rows):
            skill = runtime._team_effect_skill_name(effect.get("skill"))
            effect_id = f"{norm(effect.get('provider'))}|{skill}"
            attrs = {
                norm(key): as_number(value)
                for key, value in (effect.get("attrs", {}) if isinstance(effect.get("attrs"), dict) else {}).items()
                if norm(key) and abs(as_number(value)) > 1e-9
            }
            without_summary, _without_attrs = runtime._team_summary_with_effects(rows[:index] + rows[index + 1:])
            without_panel = as_number(without_summary.get("effective_panel"))
            contribution = max(0.0, full_panel - without_panel)
            loss_percent = contribution / max(1e-9, full_panel) * 100.0
            gain_percent = contribution / max(1e-9, base_panel) * 100.0
            result.append({
                "id": effect_id,
                "enabled": effect_id not in disabled,
                "provider": norm(effect.get("provider")),
                "skill": skill,
                "level": norm(effect.get("level")),
                "cooldown": as_number(effect.get("cooldown")),
                "duration": as_number(effect.get("duration")),
                "usageRatio": as_number(effect.get("usage_ratio"), 1.0),
                "coverage": as_number(effect.get("coverage")),
                "attrs": attrs,
                "effect": "、".join(f"{key}{value:g}" for key, value in attrs.items()),
                "contribution": contribution,
                "contributionPercent": gain_percent,
                "removalLossPercent": loss_percent,
            })
        return result

    @staticmethod
    def _selected_skill_rows(build: dict[str, Any]) -> list[dict[str, Any]]:
        rows = build.get("skill_sim")
        return [copy.deepcopy(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _selected_metrics(
        self,
        runtime: PureTimelineRuntime,
        build: dict[str, Any],
        summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        profile = runtime._job_profile()
        weighted_panel = runtime._skill_weighted_panel_profile()
        preferred = None if weighted_panel else ("M" if profile.get("attack_attr") == "魔攻" else "P")
        metrics: list[dict[str, Any]] = []
        for index, row in enumerate(self._selected_skill_rows(build)):
            skill = norm(row.get("skill"))
            level = norm(row.get("level"))
            if not skill or skill == "无" or not level or level == "无":
                continue
            level_data, _effective_level, _mastery = runtime._skill_sim_level_data_with_mastery(skill, level)
            if not level_data or runtime._skill_periodic_total_damage_cycle(skill) > 0:
                continue
            multiplier = max(
                0.0,
                as_number(row.get("multiplier"), runtime._skill_adjusted_multiplier(skill, level)),
            )
            burning_bonus = runtime._skill_burning_hand_bonus(skill)
            display_multiplier = multiplier + burning_bonus
            element = norm(self.store.skill_damage_element(runtime.second_job.currentText(), skill))
            dps_multiplier = runtime._skill_sim_dps_multiplier(element, profile)
            # Pure damage glyphs must not change which skills the planner
            # selects.  ``PureTimelineRuntime.calculate_timeline`` removes
            # them while planning and applies their independent per-skill
            # multiplier once to the finished fixed axis.  Applying one here
            # made selected skills both influence the rotation and receive the
            # glyph a second time after the axis was built.
            glyph_multiplier = 1.0
            raw_score = runtime._skill_level_effective(
                level_data,
                summary,
                multiplier=display_multiplier,
                preferred_type=preferred,
                weighted_panel=weighted_panel,
            ) * dps_multiplier * glyph_multiplier
            element_multiplier = runtime._skill_element_display_multiplier(element, summary)
            channel_scores = runtime._skill_level_channel_scores(
                level_data,
                summary,
                multiplier=display_multiplier,
                preferred_type=preferred,
                weighted_panel=weighted_panel,
                element=element,
            )
            channel_scores = {
                key: as_number(value) * dps_multiplier * glyph_multiplier
                for key, value in channel_scores.items()
                if as_number(value) > 0
            }
            cast_time = max(0.0, as_number(runtime._skill_effective_cast_timing(skill).get("cast_time")))
            metric = {
                "row": index,
                "skill": skill,
                "level": level,
                "base_cooldown": runtime._skill_burst_original_cooldown(
                    skill,
                    level_data,
                    as_number(row.get("cooldown")),
                ),
                "raw_score": raw_score,
                "effective_score": raw_score * element_multiplier,
                "element": element,
                "cast_time": cast_time,
                "channel_scores": channel_scores,
                "skill_glyph_damage_applied": False,
                "base_multiplier": multiplier,
                "burning_hand_bonus_applied": burning_bonus,
            }
            if norm(runtime.second_job.currentText()) in {"圣骑士", "十字军"}:
                timeline_scores = runtime._skill_timeline_attack_scores(
                    level_data,
                    summary,
                    multiplier=display_multiplier * dps_multiplier * glyph_multiplier,
                    preferred_type="P",
                )
                metric["timeline_attack_scores"] = timeline_scores
                metric["timeline_raw_score"] = sum(timeline_scores.values())
            metrics.append(metric)
        return metrics

    @staticmethod
    def _visible_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if not row.get("timeline_hidden") and norm(row.get("skill"))]

    @staticmethod
    def _segment_output(row: dict[str, Any]) -> float:
        actual = max(0.0, as_number(row.get("actual_damage")))
        return actual if actual > 0 else max(0.0, as_number(row.get("effective_score")))

    @classmethod
    def _serialize_segment(cls, row: dict[str, Any]) -> dict[str, Any]:
        output = cls._segment_output(row)
        start = max(0.0, as_number(row.get("start")))
        end = max(start, as_number(row.get("end"), start))
        dpa = max(0.0, as_number(row.get("actual_dpa"), as_number(row.get("dpa"))))
        return {
            "skill": norm(row.get("skill")),
            "type": norm(row.get("type")),
            "kind": norm(row.get("kind")),
            "start": start,
            "end": end,
            "castTime": max(0.0, end - start),
            "output": output,
            "effectiveScore": max(0.0, as_number(row.get("effective_score"))),
            "rawOutput": max(0.0, as_number(row.get("raw_actual_damage"), as_number(row.get("raw_effective_score")))),
            "dpa": dpa,
            "element": norm(row.get("element")),
            "cooldownReadyAt": max(0.0, as_number(row.get("cooldown_ready_at"))),
            "detail": norm(row.get("detail")),
        }

    @staticmethod
    def _serialize_effect(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": norm(row.get("name") or row.get("skill") or row.get("source")),
            "skill": norm(row.get("name") or row.get("skill") or row.get("source")),
            "source": norm(row.get("source")),
            "provider": norm(row.get("provider")),
            "group": norm(row.get("group")),
            "category": norm(row.get("category")),
            "start": max(0.0, as_number(row.get("start"))),
            "end": max(0.0, as_number(row.get("end"))),
            "detail": norm(row.get("detail")),
            "color": norm(row.get("color")),
        }

    @classmethod
    def _axis_summary(cls, rows: list[dict[str, Any]], window: float) -> dict[str, Any]:
        visible = cls._visible_segments(rows)
        total = sum(cls._segment_output(row) for row in visible if as_number(row.get("start")) < window - 1e-6)
        contributions: dict[str, dict[str, float]] = {}
        for row in visible:
            if as_number(row.get("start")) >= window - 1e-6:
                continue
            skill = norm(row.get("skill")) or "未知技能"
            current = contributions.setdefault(skill, {"count": 0.0, "output": 0.0, "rawOutput": 0.0})
            current["count"] += 1.0
            current["output"] += cls._segment_output(row)
            current["rawOutput"] += max(
                0.0,
                as_number(row.get("raw_actual_damage"), as_number(row.get("raw_effective_score"))),
            )
        contribution = [
            {
                "skill": skill,
                "count": int(values["count"]),
                "output": values["output"],
                "rawOutput": values["rawOutput"],
                "share": values["output"] / total if total > 0 else 0.0,
            }
            for skill, values in sorted(
                contributions.items(),
                key=lambda item: item[1]["output"],
                reverse=True,
            )
        ]
        return {
            "segments": [cls._serialize_segment(row) for row in visible],
            "total": total,
            "dps": total / window if window > 0 else 0.0,
            "contribution": contribution,
        }

    @staticmethod
    def _remaining_cooldown(
        runtime: PureTimelineRuntime,
        metric: dict[str, Any],
        rows: list[dict[str, Any]],
        reference: float,
    ) -> float:
        skill = runtime._skill_burst_skill_key(metric.get("skill"))
        if (
            not skill
            or runtime._skill_burst_ammo_cost(skill) > 0
            or skill == "双飞腿"
        ):
            return 0.0
        previous = [
            row
            for row in rows
            if runtime._skill_burst_skill_key(row.get("skill")) == skill
            and as_number(row.get("start")) <= reference + 1e-6
        ]
        if not previous:
            return 0.0
        last = max(previous, key=lambda row: as_number(row.get("start")))
        cast_start = max(0.0, as_number(last.get("start")))
        ready = as_number(last.get("cooldown_ready_at"), -1.0)
        if ready >= cast_start - 1e-6:
            return max(0.0, ready - reference)
        base_cooldown = max(0.0, as_number(metric.get("base_cooldown")))
        if base_cooldown <= 0:
            return 0.0
        ready = runtime._skill_burst_cooldown_ready_at(
            skill,
            base_cooldown,
            cast_start,
        )
        return max(0.0, ready - reference)

    def _candidates(
        self,
        runtime: PureTimelineRuntime,
        metrics: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        selected_index: int,
    ) -> list[dict[str, Any]]:
        visible = self._visible_segments(rows)
        reference = (
            max(0.0, as_number(visible[selected_index].get("end")))
            if 0 <= selected_index < len(visible)
            else 0.0
        )
        strongest_dpa = max((runtime._skill_burst_candidate_metric_dpa(metric) for metric in metrics), default=0.0)
        strongest_dps = max((runtime._skill_burst_candidate_metric_dps(metric) for metric in metrics), default=0.0)
        result = []
        for metric in metrics:
            dpa = runtime._skill_burst_candidate_metric_dpa(metric)
            dps = runtime._skill_burst_candidate_metric_dps(metric)
            if not metric.get("is_ultimate") and dpa < strongest_dpa * 0.2 - 1e-9 and dps < strongest_dps * 0.2 - 1e-9:
                continue
            skill_key = runtime._skill_burst_skill_key(metric.get("skill"))
            tracks_cooldown = bool(
                skill_key
                and runtime._skill_burst_ammo_cost(skill_key) <= 0
                and skill_key != "双飞腿"
            )
            remaining = self._remaining_cooldown(
                runtime,
                metric,
                visible,
                reference,
            )
            result.append({
                "skill": norm(metric.get("skill")),
                "dpa": dpa,
                "dps": dps,
                "cooldown": max(0.0, as_number(metric.get("base_cooldown"))),
                "castTime": max(0.0, as_number(metric.get("cast_time"))),
                "remainingCooldown": remaining,
                "ready": remaining <= 1e-6,
                "tracksCooldown": tracks_cooldown,
                "ultimate": bool(metric.get("is_ultimate")),
            })
        return sorted(result, key=lambda row: (not row["ready"], row["remainingCooldown"], -row["dpa"]))

    def _calculate_raw(
        self,
        build: dict[str, Any],
        settings: dict[str, Any],
        manual_sequence: list[str] | None,
    ) -> tuple[PureTimelineRuntime, dict[str, Any]]:
        runtime = self._runtime(build, settings)
        _rows, summary = self.calculator.calculate(copy.deepcopy(build))
        duplicate_buffs = set(getattr(runtime, "_team_practical_duplicate_build_buffs", set()))
        if duplicate_buffs:
            runtime._skill_burst_base_summary = copy.deepcopy(summary)
            runtime._skill_burst_team_base_replacement_multiplier = (
                runtime._team_practical_build_buff_replacement_multiplier(duplicate_buffs)
            )
        metrics = self._selected_metrics(runtime, build, summary)
        result = runtime.calculate_timeline(
            metrics,
            summary,
            manual_sequence=copy.deepcopy(manual_sequence),
        ).materialize()
        if (norm(build.get('second_job')) == '狂战士'
                and build.get('_fury_key_node_search', True)
                and manual_sequence is None):
            # Local fury lookahead is not a global-optimum proof. Personal
            # auto simulation keeps its former complete axis as a quality floor.
            # Score workers use their bounded policy directly, not this UI path.
            floor_build = copy.deepcopy(build)
            floor_build['_fury_key_node_search'] = False
            floor_runtime, floor_result = self._calculate_raw(floor_build, settings, None)
            def value(candidate):
                return sum(max(0., as_number(row.get('effective_score')))
                           for row in candidate.get('auto_segments', ()))
            proposed_value, floor_value = value(result), value(floor_result)
            use_floor = floor_value > proposed_value + 1e-6
            if use_floor:
                runtime, result = floor_runtime, floor_result
            result['fury_planner_comparison'] = {
                'selected': 'original_floor' if use_floor else 'key_nodes',
                'original_total': floor_value, 'key_nodes_total': proposed_value,
            }
        return runtime, result

    def _scenario_rows(self, build: dict[str, Any]) -> list[dict[str, Any]]:
        cache_key = self._hash_payload(build)
        cached = self._scenario_cache.get(cache_key)
        if cached is not None:
            self._scenario_cache.move_to_end(cache_key)
            return copy.deepcopy(cached)
        definitions = (
            ("实战模拟", {"mode": "burst", "practical": True}),
            ("鸡尾酒", {"mode": "burst", "cocktail": True}),
            ("精神爆发", {"mode": "burst", "burst": True}),
            ("精神爆发+鸡尾酒", {"mode": "burst", "burst": True, "cocktail": True}),
            ("时间加速", {"mode": "burst", "accel": True}),
            ("时间加速+精神爆发", {"mode": "burst", "accel": True, "burst": True}),
        )
        rows: list[dict[str, Any]] = []
        for label, scenario in definitions:
            _runtime, result = self._calculate_raw(build, scenario, None)
            window = max(0.0, as_number(result.get("window")))
            axis = self._axis_summary(result.get("auto_segments", []), window)
            rows.append({"name": label, "outputPerSecond": axis["dps"]})
        baseline = rows[0]["outputPerSecond"] if rows else 0.0
        for row in rows:
            row["gain"] = row["outputPerSecond"] / baseline - 1.0 if baseline > 0 else 0.0
        self._scenario_cache[cache_key] = copy.deepcopy(rows)
        while len(self._scenario_cache) > 12:
            self._scenario_cache.popitem(last=False)
        return rows

    def calculate(self, build: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        normalized_settings = self._standalone_acceleration_resistance_settings(
            settings
        )
        mode = norm(normalized_settings.get("mode")) or "long"
        normalized_settings["mode"] = mode
        manual_source = normalized_settings.get("manualSequence")
        manual_sequence = [norm(skill) for skill in manual_source if norm(skill)] if isinstance(manual_source, list) else None
        cache_key = self._hash_payload({"build": build, "settings": normalized_settings})
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return copy.deepcopy(cached)

            runtime, result = self._calculate_raw(build, normalized_settings, manual_sequence)
            window = max(0.0, as_number(result.get("window")))
            auto_rows = result.get("auto_segments", [])
            manual_rows = result.get("segments", auto_rows)
            auto_axis = self._axis_summary(auto_rows, window)
            manual_axis = self._axis_summary(manual_rows, window)
            selected_index = int(as_number(normalized_settings.get("selectedIndex"), -1))
            candidate_metrics = result.get("candidate_metrics", [])
            authoritative_resistance = [
                row
                for row in normalized_settings.get("resistanceWindows", [])
                if isinstance(row, dict)
            ]
            runtime_resistance = list(
                getattr(runtime, "_skill_burst_runtime_resistance_windows", [])
            )
            if bool(normalized_settings.get("standaloneAccelerationResistance")):
                authoritative_resistance = [
                    row
                    for row in authoritative_resistance
                    if not runtime._skill_burst_external_interval_suppressed(
                        "burst"
                        if (
                            norm(row.get("group"))
                            == BURST_RESISTANCE_GROUP
                        )
                        else "accel",
                        as_number(row.get("start")),
                    )
                ]
            external_effect_windows = [
                row
                for row in normalized_settings.get("externalEffectWindows", [])
                if not (
                    authoritative_resistance
                    and isinstance(row, dict)
                    and norm(row.get("category")) == "acceleration_resistance"
                )
            ]
            displayed_resistance = (
                [*authoritative_resistance, *runtime_resistance]
                if bool(
                    normalized_settings.get(
                        "standaloneAccelerationResistance"
                    )
                )
                else authoritative_resistance
                if authoritative_resistance
                else runtime_resistance
            )
            payload = {
                "job": norm(build.get("second_job")),
                "mode": mode,
                "window": window,
                "auto": auto_axis,
                "manual": manual_axis,
                "segments": manual_axis["segments"],
                "total": manual_axis["total"],
                "dps": manual_axis["dps"],
                "contribution": manual_axis["contribution"],
                "effectWindows": [
                    self._serialize_effect(row)
                    for row in (
                        list(result.get("effect_windows", []))
                        + external_effect_windows
                        + displayed_resistance
                    )
                ],
                "resistanceWindows": [
                    self._serialize_effect(row)
                    for row in displayed_resistance
                ],
                "candidates": self._candidates(runtime, candidate_metrics, manual_rows, selected_index),
                "availableSkills": [norm(value) for value in result.get("available_skills", []) if norm(value)],
                "manualSequence": [row["skill"] for row in manual_axis["segments"]],
                "autoSequence": [row["skill"] for row in auto_axis["segments"]],
                "manualIssues": [norm(value) for value in result.get("manual_issues", []) if norm(value)],
                "scenarioComparison": self._scenario_rows(build) if bool(normalized_settings.get("includeScenarios")) else [],
                "planner": "v401-pure-timeline",
                "furyPlannerComparison": result.get('fury_planner_comparison'),
                "legacyParity": True,
                "blockedIntervals": copy.deepcopy(normalized_settings.get("blockedIntervals", [])),
            }
            self._cache[cache_key] = copy.deepcopy(payload)
            while len(self._cache) > 24:
                self._cache.popitem(last=False)
            return payload
