from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Callable

from ..data_loader import norm
from ..timeline_engine import TimelineValue
from ..legacy_timeline import LegacyTimelineService
from .boss_scope import _boss_statistics_groups
from .cast_timeline import (
    evidence_score_cast_events,
)
from .models import CombatSnapshot
from .event_platform import event_platform_for
from .scoring import TeamScoreEngine
from .theory_plan_builder import (
    BOSS_PRESETS,
    TEAM_SCORE_PROGRESS_EXCLUSION_END,
    TEAM_SCORE_PROGRESS_REPLAN_END,
    TheoryPlanBuilderMixin,
    _TEAM_SCORE_DEFAULT_BOSS_PRESET,
    _team_score_apply_fixed_axis_damage_glyphs,
)


def _load_live_page_type() -> type:
    """Return the data-only planner through the legacy private entry point.

    V4.25 loaded ``LiveDpsPage`` behind temporary Qt placeholder modules so
    the score engine could borrow its theory-planning methods.  Those methods
    now live in :class:`TheoryPlanBuilderMixin`; retaining this thin adapter
    keeps frozen-runtime checks and older internal callers compatible without
    importing the Qt page again.
    """

    return TeamScoreTheoryPlanner


class TeamScoreTheoryPlanner(TheoryPlanBuilderMixin):
    """Run the exact V4.01 team-score theory planner as a data-only service."""

    def __init__(
        self,
        store: Any,
        calculator: Any,
        timeline_service: LegacyTimelineService,
    ) -> None:
        self.store = store
        self.calculator = calculator
        self.timeline_service = timeline_service
        self.score_engine = TeamScoreEngine(store)
        self.collector = SimpleNamespace(status="V4.10 replay")
        self.timeline_editor: Any = None
        self._capture_active = False
        self._team_score_theory_signature = None
        self._team_score_theory_plans: dict[str, dict[str, Any]] = {}
        self._team_score_theory_jobs: tuple[str, ...] = ()
        self._team_score_theory_scope: tuple[str, ...] = ()
        self._team_score_theory_built_at = 0.0
        self._team_score_theory_error = ""
        self._team_score_theory_diagnostics: dict[str, Any] = {}
        self._team_score_theory_plan_cache: dict[Any, Any] = {}
        self._team_score_replan_specs_by_source: dict[str, dict[str, Any]] = {}
        self._team_score_replan_specs_cache: dict[
            Any, dict[str, dict[str, Any]]
        ] = {}
        self._team_score_precomputed_exclusion_plans: dict[
            str, tuple[frozenset[str], dict[str, Any]]
        ] = {}
        self._team_score_build_override_cache: dict[Any, Any] = {}
        self._team_score_glyph_overrides_by_job: dict[str, list[dict[str, Any]]] = {}
        self._team_score_glyph_inference_by_source: dict[
            str, list[dict[str, Any]]
        ] = {}
        self._team_score_equipment_overrides: dict[str, dict[str, Any]] = {}
        self._progress: Callable[[int, str], None] = lambda _value, _message: None

    def _advance_view_progress(self, _view: str, value: int, message: str) -> None:
        self._progress(max(0, min(100, int(value))), str(message or ""))

    def score_cast_events(self, snapshot: CombatSnapshot) -> tuple[Any, ...]:
        """Build the same evidence cast axis shown by the team timeline."""

        platform = event_platform_for(snapshot)
        return evidence_score_cast_events(
            platform.skill_cast_events,
            platform.combat_events,
            self.store,
        )

    def primary_boss_target_ids(
        self,
        snapshot: CombatSnapshot,
    ) -> frozenset[str] | None:
        """Match V4.01's default "all primary Bosses" score scope."""

        groups = _boss_statistics_groups(snapshot)
        target_ids = frozenset(
            target_id
            for group in groups
            for target_id in group.target_ids
        )
        return target_ids or None

    def specialty_windows(
        self,
        snapshot: CombatSnapshot,
        boss_target_ids: frozenset[str] | None,
    ) -> tuple[tuple[float, float], ...]:
        """Return the same full Boss phase windows used by V4.01 duties."""

        rows = sorted(
            (
                max(0.0, float(start)),
                max(float(start), float(end)),
            )
            for group in _boss_statistics_groups(snapshot)
            if not boss_target_ids
            or bool(set(group.target_ids) & set(boss_target_ids))
            for start, end in group.phase_windows
            if float(end) > float(start)
        )
        merged: list[list[float]] = []
        for start, end in rows:
            if not merged or start > merged[-1][1] + 1e-6:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return tuple((start, end) for start, end in merged)

    def build(
        self,
        snapshot: CombatSnapshot,
        build: dict[str, Any],
        *,
        boss_target_ids: frozenset[str] | None,
        boss_preset_name: str,
        capture_hint: str,
        glyph_overrides: dict[str, list[dict[str, Any]]] | None,
        inferred_glyphs_by_source: dict[str, list[dict[str, Any]]] | None,
        progress: Callable[[int, str], None],
        equipment_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self._progress = progress
        self.collector.status = capture_hint
        self._team_score_equipment_overrides = copy.deepcopy(equipment_overrides or {})
        self._team_score_glyph_overrides_by_job = copy.deepcopy(glyph_overrides or {})
        self._team_score_glyph_inference_by_source = copy.deepcopy(
            inferred_glyphs_by_source or {}
        )
        runtime = self.timeline_service.extreme_runtime(copy.deepcopy(build))
        runtime.skill_long_accel_start = TimelineValue(60.0)
        runtime._skill_burst_disable_team_objective_opening_order = bool(
            getattr(
                self,
                "_skill_burst_disable_team_objective_opening_order",
                False,
            )
        )
        runtime._skill_burst_disable_team_objective_pair_order = bool(
            getattr(
                self,
                "_skill_burst_disable_team_objective_pair_order",
                False,
            )
        )
        self.timeline_editor = runtime
        self.score_engine.set_boss_profile(
            boss_preset_name,
            self._boss_preset(boss_preset_name),
        )
        return self._build_team_score_theory_plans(
            snapshot,
            boss_target_ids,
            boss_preset_name,
        )

    def invalidate_score_cache(self) -> None:
        """Drop every reusable theory artifact before an explicit re-score."""

        self._team_score_theory_signature = None
        self._team_score_theory_plans = {}
        self._team_score_theory_jobs = ()
        self._team_score_theory_scope = ()
        self._team_score_replan_specs_by_source = {}
        self._team_score_precomputed_exclusion_plans = {}
        self._team_score_theory_plan_cache.clear()
        self._team_score_replan_specs_cache.clear()
        self._team_score_build_override_cache.clear()

    def rebuild_excluded_axes(
        self,
        plans: dict[str, dict[str, Any]],
        excluded_skills_by_source: dict[str, set[str]],
        progress: Callable[[int, str], None],
    ) -> dict[str, dict[str, Any]]:
        """Replan only players whose score exemption frees rotation time."""

        editor = self.timeline_editor
        run_parallel = getattr(editor, "_extreme_timeline_tasks_parallel", None)
        if not callable(run_parallel):
            return plans
        sources: list[str] = []
        calculated_sources: list[str] = []
        specs: list[dict[str, Any]] = []
        result_by_source: dict[str, dict[str, Any]] = {}
        prefetched = self._team_score_precomputed_exclusion_plans
        for source_id, raw_skills in excluded_skills_by_source.items():
            skill_keys = {
                norm(skill) for skill in raw_skills if norm(skill)
            }
            base_spec = self._team_score_replan_specs_by_source.get(
                str(source_id)
            )
            if not skill_keys or not isinstance(base_spec, dict):
                continue
            normalized_source = str(source_id)
            sources.append(normalized_source)
            precomputed = prefetched.pop(normalized_source, None)
            if (
                isinstance(precomputed, tuple)
                and len(precomputed) == 2
                and precomputed[0] == frozenset(skill_keys)
                and isinstance(precomputed[1], dict)
            ):
                # Ownership transfers to this one rebuild.  The exact-key
                # check means a provisional mismatch automatically falls back
                # to the original full calculation below.
                result_by_source[normalized_source] = precomputed[1]
                continue
            # The parallel task builder already copies every mutable value that
            # it hands to a worker (and the sequential fallback deep-copies the
            # complete spec).  Only this top-level key is changed here, so a
            # shallow copy avoids cloning each player's large cast schedules a
            # second time without sharing anything that can be mutated.
            spec = dict(base_spec)
            spec["excluded_skill_keys"] = sorted(skill_keys)
            calculated_sources.append(normalized_source)
            specs.append(spec)
        if not sources:
            return plans

        if specs:
            progress(
                TEAM_SCORE_PROGRESS_EXCLUSION_END,
                f"正在重排豁免技能后的理论轴（0/{len(specs)}，"
                f"已提前完成{len(result_by_source)}个）",
            )

        def replan_progress(completed: int, _index: int, job: str) -> None:
            progress(
                TEAM_SCORE_PROGRESS_EXCLUSION_END
                + round(
                    completed
                    * (
                        TEAM_SCORE_PROGRESS_REPLAN_END
                        - TEAM_SCORE_PROGRESS_EXCLUSION_END
                    )
                    / max(1, len(specs))
                ),
                f"正在重排豁免技能后的理论轴 "
                f"（{completed}/{len(specs)}，{job}）",
            )

        results = (
            run_parallel(specs, progress_callback=replan_progress)
            if specs
            else []
        )
        result_by_source.update({
            source_id: result
            for source_id, result in zip(calculated_sources, results)
            if isinstance(result, dict)
        })
        # Replanning replaces only the affected players.  Keep unchanged plans
        # by reference and copy-on-write the changed top-level dictionaries;
        # all nested plan values are treated as immutable by the score engine.
        merged_plans = dict(plans)
        for source_id in sources:
            result = result_by_source.get(source_id)
            if not isinstance(result, dict):
                continue
            previous = merged_plans.get(source_id, {})
            _team_score_apply_fixed_axis_damage_glyphs(
                result,
                (
                    previous.get("_score_glyphs", [])
                    if isinstance(previous, dict)
                    else []
                ),
            )
            merged = dict(previous) if isinstance(previous, dict) else {}
            merged.update(result)
            for metadata_key in (
                "_score_time_offset",
                "_score_theory_cdr_schedule",
                "_score_actual_cdr_schedule",
                "_score_glyphs",
                "_score_glyph_source",
                "_score_equipment",
                "_score_combat_skill_levels",
                "_score_boss_hp_segments",
                "_support_counterfactual_replan",
            ):
                if isinstance(previous, dict) and metadata_key in previous:
                    merged[metadata_key] = previous[metadata_key]
            merged["_score_excluded_skill_keys"] = sorted(
                norm(skill)
                for skill in excluded_skills_by_source.get(source_id, set())
                if norm(skill)
            )
            merged_plans[source_id] = merged
        self._team_score_theory_plans = dict(merged_plans)
        return merged_plans

    @staticmethod
    def _boss_preset(name: str) -> dict[str, Any]:
        return copy.deepcopy(
            BOSS_PRESETS.get(
                norm(name), BOSS_PRESETS[_TEAM_SCORE_DEFAULT_BOSS_PRESET]
            )
        )
