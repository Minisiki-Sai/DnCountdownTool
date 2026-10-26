from __future__ import annotations

import copy
import hashlib
import inspect
import pickle
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .skill_glyph_policy import damage_glyph_multiplier


TIMELINE_SNAPSHOT_SCHEMA = 1
TIMELINE_RESULT_SCHEMA = 1


@lru_cache(maxsize=4096)
def _timeline_static_descriptor(source_type: type, name: str) -> Any:
    return inspect.getattr_static(source_type, name, None)


@dataclass(frozen=True, slots=True)
class TimelineSimulationSnapshot:
    """Immutable, process-safe input for one character timeline calculation."""

    payload: bytes
    fingerprint: str
    schema: int = TIMELINE_SNAPSHOT_SCHEMA

    @classmethod
    def create(
        cls,
        build: dict[str, Any],
        effects: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        runtime_values: dict[str, Any] | None = None,
    ) -> "TimelineSimulationSnapshot":
        source = {
            "schema": TIMELINE_SNAPSHOT_SCHEMA,
            "build": copy.deepcopy(build),
            "effects": copy.deepcopy(list(effects)),
            "runtime_values": copy.deepcopy(runtime_values or {}),
        }
        payload = pickle.dumps(source, protocol=pickle.HIGHEST_PROTOCOL)
        return cls(
            payload=payload,
            fingerprint=hashlib.sha256(payload).hexdigest(),
        )

    def materialize(self) -> dict[str, Any]:
        source = pickle.loads(self.payload)
        if source.get("schema") != self.schema:
            raise ValueError("时间轴快照版本不匹配")
        return source


@dataclass(frozen=True, slots=True)
class TimelineCalculationResult:
    """Immutable output produced by the data-only timeline runtime."""

    payload: bytes
    fingerprint: str
    snapshot_fingerprint: str
    schema: int = TIMELINE_RESULT_SCHEMA

    @classmethod
    def create(
        cls,
        data: dict[str, Any],
        *,
        snapshot_fingerprint: str,
    ) -> "TimelineCalculationResult":
        source = {
            "schema": TIMELINE_RESULT_SCHEMA,
            "data": copy.deepcopy(data),
        }
        payload = pickle.dumps(source, protocol=pickle.HIGHEST_PROTOCOL)
        return cls(
            payload=payload,
            fingerprint=hashlib.sha256(payload).hexdigest(),
            snapshot_fingerprint=snapshot_fingerprint,
        )

    def materialize(self) -> dict[str, Any]:
        source = pickle.loads(self.payload)
        if source.get("schema") != self.schema:
            raise ValueError("时间轴结果版本不匹配")
        return source["data"]


class TimelineTextChoice:
    """Data-only replacement for the currentText part of a combo box."""

    def __init__(self, text: Any = "") -> None:
        self._text = "" if text is None else str(text).strip()

    def currentText(self) -> str:
        return self._text


class TimelineValue:
    """Data-only replacement for the value/isChecked part of Qt controls."""

    def __init__(self, value: float = 0.0, checked: bool = False) -> None:
        self._value = value
        self._checked = checked

    def value(self) -> float:
        return self._value

    def isChecked(self) -> bool:
        return self._checked


class PureTimelineRuntime:
    """Execute timeline methods without retaining an editor or any Qt widget."""

    def __init__(
        self,
        snapshot: TimelineSimulationSnapshot,
        *,
        store: Any,
        calculator: Any,
        cooldown_engine: Any,
        method_source_type: type,
        level_cap: int,
    ) -> None:
        source = snapshot.materialize()
        build = source["build"]
        self.snapshot_fingerprint = snapshot.fingerprint
        self._timeline_method_source_type = method_source_type
        self._timeline_missing_attributes: set[str] = set()
        self.store = store
        self.calculator = calculator
        self.skill_cooldown_engine = cooldown_engine
        self.build = build
        self.first_job = TimelineTextChoice(build.get("first_job", ""))
        self.second_job = TimelineTextChoice(build.get("second_job", ""))
        self.skill_build_data = store.skill_build_tree(
            self.second_job.currentText(), level_cap
        )
        self.skill_mastery_data = store.skill_build_tree(
            self.second_job.currentText(), level_cap, mode="master"
        )
        self.skill_build_level_spins = {}
        self.skill_mastery_spins = {}
        self.buff_table = None
        if bool(source["runtime_values"].get("_timeline_extreme_mode", True)):
            self._extreme_selected_effects = source["effects"]
        else:
            self._timeline_selected_effects = source["effects"]
        self.skill_burst_check = TimelineValue()
        self.skill_cocktail_check = TimelineValue()
        self.skill_accel_check = TimelineValue()
        self.skill_custom_cdr = TimelineValue()
        self.skill_burst_timeline = True
        self._skill_burst_periodic_buffs = []
        self._skill_burst_candidate_metrics = []
        self._team_practical_panel_cache = {}
        saved_glyphs = build.get("skill_glyphs", {})
        saved_glyph_rows = (
            saved_glyphs.get("rows", [])
            if isinstance(saved_glyphs, dict)
            else []
        )
        self._skill_glyph_applied_rows = [
            dict(row) for row in saved_glyph_rows if isinstance(row, dict)
        ]
        for name, value in source["runtime_values"].items():
            object.__setattr__(self, name, value)
        self._skill_burst_active_attrs_cache = {}
        self._skill_burst_cooldown_ready_cache = {}
        self._skill_burst_new_scheme_1_state_attrs_cache = {}
        self._timeline_is_pure_runtime = True

    def __getattr__(self, name: str) -> Any:
        source_type = object.__getattribute__(self, "_timeline_method_source_type")
        missing = object.__getattribute__(self, "_timeline_missing_attributes")
        if name in missing:
            raise AttributeError(name)
        descriptor = _timeline_static_descriptor(source_type, name)
        if isinstance(descriptor, staticmethod):
            resolved = descriptor.__func__
        elif isinstance(descriptor, classmethod):
            resolved = descriptor.__get__(None, source_type)
        elif callable(descriptor):
            resolved = descriptor.__get__(self, type(self))
        else:
            missing.add(name)
            raise AttributeError(name)
        object.__setattr__(self, name, resolved)
        return resolved

    def collect_build(self) -> dict[str, Any]:
        return copy.deepcopy(self.build)

    def _selected_team_effects(self, *args, **kwargs) -> list[dict[str, Any]]:
        return copy.deepcopy(
            getattr(
                self,
                "_extreme_selected_effects",
                getattr(self, "_timeline_selected_effects", []),
            )
        )

    def _selected_skill_mastery_ids(self) -> set[str]:
        saved = self.build.get("skill_mastery_build", {})
        levels = saved.get("levels", {}) if isinstance(saved, dict) else {}
        selected: set[str] = set()
        for skill_id, level in levels.items():
            key = str(skill_id).strip()
            if not key:
                continue
            try:
                learned = float(level or 0) > 0
            except (TypeError, ValueError):
                learned = False
            if learned:
                selected.add(key)
        return selected

    @staticmethod
    def _fixed_axis_scale_metric(row: dict[str, Any], ratio: float) -> None:
        for field in (
            "raw_score",
            "effective_score",
            "timeline_raw_score",
            "raw_effective_score",
            "dpa",
            "dps",
            "snapshot_full_score",
            "actual_damage",
            "raw_actual_damage",
        ):
            if field in row and isinstance(row.get(field), (int, float)):
                row[field] = float(row[field]) * ratio
        for field in (
            "channel_scores",
            "timeline_attack_scores",
            "metric_channel_scores",
        ):
            values = row.get(field)
            if isinstance(values, dict):
                row[field] = {
                    key: float(value or 0.0) * ratio
                    for key, value in values.items()
                }

    def _apply_fixed_axis_damage_glyphs(
        self,
        rows: list[dict[str, Any]],
        damage_rows: list[dict[str, Any]],
        final_percent: float,
    ) -> None:
        multipliers: dict[str, float] = {}
        for row in damage_rows:
            skill_key = self._skill_burst_skill_key(row.get("skill"))
            if not skill_key:
                continue
            multipliers[skill_key] = max(
                multipliers.get(skill_key, 1.0),
                damage_glyph_multiplier(row.get("value"), final_percent),
            )
        for row in rows:
            if not isinstance(row, dict):
                continue
            ratio = multipliers.get(
                self._skill_burst_skill_key(row.get("skill")), 1.0
            )
            if abs(ratio - 1.0) <= 1e-12:
                continue
            self._fixed_axis_scale_metric(row, ratio)
            scaled_metric_ids: set[int] = set()
            for field in ("metric", "_score_metric", "periodic_metric"):
                metric = row.get(field)
                if isinstance(metric, dict) and id(metric) not in scaled_metric_ids:
                    self._fixed_axis_scale_metric(metric, ratio)
                    scaled_metric_ids.add(id(metric))

    def calculate_timeline(
        self,
        metrics: list[dict[str, Any]],
        base_summary: dict[str, Any],
        *,
        manual_sequence: list[str] | None = None,
    ) -> TimelineCalculationResult:
        """Calculate one complete axis without reading or mutating UI state."""
        self._skill_burst_base_summary = copy.deepcopy(base_summary)
        selected_rows = [
            copy.deepcopy(row)
            for row in getattr(self, "_skill_glyph_applied_rows", ())
            if isinstance(row, dict)
        ]
        damage_rows = [
            row
            for row in selected_rows
            if str(row.get("effect") or "").strip() == "伤害增加"
        ]
        if damage_rows:
            timing_rows = [
                row
                for row in selected_rows
                if str(row.get("effect") or "").strip() != "伤害增加"
            ]
            # A pure damage plate cannot make one action outrank another.  Plan
            # with timing plates only, then revalue the unchanged cast axis.
            self._skill_glyph_applied_rows = copy.deepcopy(timing_rows)
            glyphs = self.build.get("skill_glyphs")
            if isinstance(glyphs, dict):
                glyphs["rows"] = copy.deepcopy(timing_rows)
            self.__dict__.pop("_skill_glyph_effect_index_cache", None)
        auto_segments, window = self._calculate_skill_burst_sim_preview_data(
            copy.deepcopy(metrics),
            render_ui=False,
        )
        all_metrics = self._skill_burst_all_learned_metrics(
            self._skill_burst_base_summary,
            copy.deepcopy(metrics),
        )
        metrics_by_skill = {
            self._skill_burst_skill_key(metric.get("skill")): metric
            for metric in all_metrics
        }
        support_actions = self._skill_burst_support_actions(metrics_by_skill)
        candidate_metrics = copy.deepcopy(
            getattr(
                self,
                "_skill_burst_calculated_candidate_metrics",
                self._skill_burst_candidate_pool(all_metrics),
            )
        )
        auto_sequence = [
            str(segment.get("skill") or "").strip()
            for segment in auto_segments
            if str(segment.get("skill") or "").strip()
            and not segment.get("timeline_hidden")
        ]
        requested_sequence = [
            str(skill or "").strip()
            for skill in (manual_sequence or [])
            if str(skill or "").strip()
        ]
        manual_issues: list[str] = []
        if not requested_sequence or requested_sequence == auto_sequence:
            segments = copy.deepcopy(auto_segments)
        else:
            segments, manual_issues = self._skill_burst_replay_manual_sequence(
                requested_sequence,
                all_metrics,
                support_actions,
                window,
            )
            segments.extend(
                copy.deepcopy(segment)
                for segment in auto_segments
                if segment.get("timeline_hidden")
            )
            self._skill_burst_apply_periodic_total_damage_scores(
                segments,
                support_actions,
                window,
            )
            self._skill_burst_attach_mixed_attack_damage(segments)
        effect_windows = self._skill_burst_effect_windows(
            segments,
            support_actions,
            window,
        )
        if damage_rows:
            final_percent = max(
                0.0, float(base_summary.get("final_percent") or 0.0)
            )
            self._apply_fixed_axis_damage_glyphs(
                auto_segments, damage_rows, final_percent
            )
            self._apply_fixed_axis_damage_glyphs(
                segments, damage_rows, final_percent
            )
            self._apply_fixed_axis_damage_glyphs(
                all_metrics, damage_rows, final_percent
            )
            self._apply_fixed_axis_damage_glyphs(
                candidate_metrics, damage_rows, final_percent
            )
        available_skills = copy.deepcopy(
            getattr(self, "_skill_burst_calculated_available_skills", [])
        )
        data = {
            "auto_segments": auto_segments,
            "segments": segments,
            "window": window,
            "all_metrics": all_metrics,
            "support_actions": support_actions,
            "candidate_metrics": candidate_metrics,
            "available_skills": available_skills,
            "effect_windows": effect_windows,
            "generated_effect_windows": copy.deepcopy(
                getattr(self, "_skill_burst_generated_effect_windows", [])
            ),
            "periodic_buffs": copy.deepcopy(
                getattr(self, "_skill_burst_periodic_buffs", [])
            ),
            "note": str(getattr(self, "_skill_burst_generated_note", "")),
            "manual_issues": manual_issues,
            "planner_state": {
                name: copy.deepcopy(getattr(self, name))
                for name in (
                    "_skill_burst_local_optimizer_last_stats",
                    "_skill_burst_new_scheme_1_planning_stats",
                    "_skill_burst_new_scheme_1_last_multistart",
                    "_skill_burst_new_scheme_1_last_quality",
                    "_skill_burst_short_personal_burst_last_quality",
                )
                if hasattr(self, name)
            },
        }
        return TimelineCalculationResult.create(
            data,
            snapshot_fingerprint=self.snapshot_fingerprint,
        )


class TimelineSimulationEngine:
    """Factory for data-only timeline runtimes."""

    def __init__(
        self,
        *,
        store: Any,
        calculator: Any,
        cooldown_engine: Any,
        method_source_type: type,
        level_cap: int,
    ) -> None:
        self.store = store
        self.calculator = calculator
        self.cooldown_engine = cooldown_engine
        self.method_source_type = method_source_type
        self.level_cap = level_cap

    def runtime(self, snapshot: TimelineSimulationSnapshot) -> PureTimelineRuntime:
        return PureTimelineRuntime(
            snapshot,
            store=self.store,
            calculator=self.calculator,
            cooldown_engine=self.cooldown_engine,
            method_source_type=self.method_source_type,
            level_cap=self.level_cap,
        )

    def calculate(
        self,
        snapshot: TimelineSimulationSnapshot,
        metrics: list[dict[str, Any]],
        base_summary: dict[str, Any],
        *,
        manual_sequence: list[str] | None = None,
    ) -> TimelineCalculationResult:
        return self.runtime(snapshot).calculate_timeline(
            metrics,
            base_summary,
            manual_sequence=manual_sequence,
        )
