from __future__ import annotations

import copy
import os
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Any

from ..calculator import BuildCalculator
from ..data_loader import as_number, norm
from ..hp_conditional import compact_boss_hp_segments
from ..skill_build_defaults import load_skill_build_defaults
from ..skill_glyph_policy import (
    apply_damage_cooldown_glyph_policy,
    damage_glyph_multiplier,
    select_fixed_axis_damage_glyphs,
)
from .boss_hp import resolve_fixed_boss_hp_profile
from .buff_clear import (
    clip_player_buff_intervals_at_clears,
    merged_player_buff_clear_times,
    player_buff_clear_affects_timeline_skill,
    player_buff_clear_times_by_target,
)
from .boss_scope import (
    _boss_statistics_groups,
    _completed_boss_group_identities,
    _compress_boss_hp_tracks_to_windows,
)
from .models import CombatEvent, CombatSnapshot
from .event_platform import event_platform_for
from .combat_skill_levels import apply_combat_levels_to_build, infer_combat_skill_levels, level_signature
from .scoring import (
    _HEALER_SELF_REPORTED_TEAM_AURAS,
    _PERSISTENT_TEAM_AURA_SKILL_KEYS,
    _coverage_in_windows,
    _primary_player_buff_effect_rows,
    _recorded_timing_buff_intervals,
    canonical_skill,
    data_job_for,
    yao_rotation_variant,
)
from .score_intervals import _bounded_recorded_team_buff_intervals
from .score_glyph_selection import prepare_score_glyphs, clean_score_glyph_rows, score_glyph_source
from .score_equipment import apply_score_equipment, equipment_signature, scoring_equipment_record
from .score_glyph_objective import captured_glyph_damage_context, glyph_profile_for_source
from .timing_ablation import (
    TimingAblationError,
    calculate_timing_ablations,
    calculate_conditional_timing,
    retimed_unit,
    timing_attribute,
    timing_unit_explicitly_inactive,
    without_timing_unit,
)
from ..timeline_parallel import (
    configured_timeline_workers,
    stable_score_worker_budget,
    timeline_process_pool_status,
)


_TEAM_SCORE_AURA_SKILL_KEYS = frozenset(
    {canonical_skill("元素光环"), canonical_skill("判定光环")}
)


def _order_score_followups_by_cost(
    rows: list[tuple[str, dict[str, Any]]], costs: dict[str, float],
) -> list[tuple[str, dict[str, Any]]]:
    """Queue long independent axes first, without changing tokens or results."""
    return sorted(rows, key=lambda row: -max(0.0, as_number(costs.get(norm(row[1].get("job"))))))


_TEAM_SCORE_AURA_GROUP_KEY = canonical_skill("圣骑互斥光环")
_TEAM_SCORE_DEFAULT_BOSS_PRESET = "红龙佩达"
_TEAM_SCORE_BOSS_PRESETS = {
    "梦魇·审判者天启": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},
    "梦魇·狮蝎领主克雷姆": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},

    "梦魇·海龙萨芬特拉": {"crit_resist": 12000, "element_resist": 20.0, "monster_defense": 11000, "freeze_coefficient": 0.3},
    "红龙佩达": {
        "crit_resist": 19000,
        "element_resist": 20.0,
        "monster_defense": 4950,
        "freeze_coefficient": 0.3,
    },
    "天堂龙灾·佩达": {
        "crit_resist": 18000,
        "element_resist": 10.0,
        "monster_defense": 9000,
        "freeze_coefficient": 0.3,
    },
}
BOSS_PRESETS = _TEAM_SCORE_BOSS_PRESETS

# Team-score progress is weighted by measured wall time instead of by the
# number of Python phases.  Preparing builds is comparatively cheap, while
# baseline/timing axes and the final per-player score consume almost the whole
# run.  Keep these boundaries shared with the replay scorer so a phase change
# never jumps forward by dozens of points or moves the bar backwards.
TEAM_SCORE_PROGRESS_PREP_START = 5
TEAM_SCORE_PROGRESS_PREP_END = 17
TEAM_SCORE_PROGRESS_BASELINE_END = 28
TEAM_SCORE_PROGRESS_CANDIDATE_END = 31
TEAM_SCORE_PROGRESS_TIMING_END = 42
TEAM_SCORE_PROGRESS_EXCLUSION_END = 45
TEAM_SCORE_PROGRESS_REPLAN_END = 47
TEAM_SCORE_PROGRESS_FINAL_END = 98

# Keep team value as a strong near-tie preference without letting the local
# greedy scheduler trade too much personal output for repeated support casts.
# A multi-capture 0.30/1.00 A/B run showed that full weight reduced aggregate
# pure theoretical output and sometimes reduced theoretical support value too.
_TEAM_SCORE_OUTPUT_TEAM_OBJECTIVE_WEIGHT = 0.30
_TEAM_SCORE_DEFAULT_EXCLUDED_GLYPHS = frozenset(
    {(canonical_skill("幽灵之魂"), "增益持续")}
)


class _NoopApplication:
    @staticmethod
    def processEvents() -> None:
        return None


QApplication = _NoopApplication


def bind_ui_application(application_type: Any) -> None:
    global QApplication
    QApplication = application_type


def _team_score_effects_for_alchemist_roles(
    effects: list[dict[str, Any]],
    output_providers: set[str],
) -> list[dict[str, Any]]:
    """Do not schedule Sacrifice for an observed output Apothecary."""

    sacrifice_key = canonical_skill("牺牲")
    return [
        effect for effect in effects
        if not (
            effect.get("provider") in output_providers
            and canonical_skill(effect.get("skill")) == sacrifice_key
        )
    ]


def _team_score_summon_removal_schedules_by_source(
    events: Any,
    player_job_by_source: dict[str, str],
    *,
    score_time_offset: float,
    absolute_end: float,
) -> dict[str, tuple[tuple[float, str], ...]]:
    """Resolve factual summon deaths to one player, never to a whole job.

    Older code grouped these rows by second job.  With two players of the same
    job, one relic/pet death was consequently replayed on both theory axes.
    Exact owner IDs are authoritative.  A job-only fallback is retained solely
    when that job has exactly one player in the current party.
    """

    sources_by_job: dict[str, list[str]] = defaultdict(list)
    for source_id, job in player_job_by_source.items():
        normalized_source = str(source_id or "")
        normalized_job = norm(job)
        if normalized_source and normalized_job:
            sources_by_job[normalized_job].append(normalized_source)
    rows_by_source: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for event in events:
        owner_id = str(getattr(event, "owner_id", "") or "")
        owner_job = norm(
            player_job_by_source.get(
                owner_id,
                getattr(event, "owner_job", ""),
            )
        )
        resolved_source = owner_id if owner_id in player_job_by_source else ""
        if not resolved_source and owner_job:
            candidates = sources_by_job.get(owner_job, ())
            if len(candidates) == 1:
                resolved_source = candidates[0]
        skill_key = canonical_skill(getattr(event, "skill_name", ""))
        seconds = max(
            0.0,
            as_number(getattr(event, "timestamp_ms", 0)) / 1000.0,
        )
        if (
            not resolved_source
            or not skill_key
            or seconds < score_time_offset - 1e-6
            or seconds > absolute_end + 1e-6
        ):
            continue
        rows_by_source[resolved_source].append(
            (max(0.0, seconds - score_time_offset), skill_key)
        )
    return {
        source_id: tuple(sorted(rows, key=lambda row: (row[0], row[1])))
        for source_id, rows in rows_by_source.items()
    }

def _team_score_glyph_effect_key(value: Any) -> str:
    effect = norm(value)
    return "增益持续" if effect in {"增益持续", "增益持续时间增加"} else effect

def _team_score_glyph_row_key(row: Any) -> tuple[str, str, str, float]:
    if not isinstance(row, dict):
        return ("", "", "", 0.0)
    return (
        canonical_skill(row.get("skill")),
        _team_score_glyph_effect_key(row.get("effect")),
        norm(row.get("option")),
        round(max(0.0, as_number(row.get("value"))), 6),
    )


def _team_score_glyph_option_text(option: dict[str, Any]) -> str:
    value = as_number(option.get("value"))
    value_text = str(int(value)) if abs(value - int(value)) < 1e-9 else f"{value:g}"
    return " ".join(filter(None, [norm(option.get("grade")), f"{value_text}%"]))


def _team_score_highest_glyph_candidates(store: Any, job: str) -> list[dict[str, Any]]:
    """Return the highest-grade option for every skill/effect pair."""

    rows: list[dict[str, Any]] = []
    for entry in store.skill_glyphs(job):
        if not isinstance(entry, dict):
            continue
        skill = norm(entry.get("skill"))
        effects = entry.get("effects", {})
        if not skill or not isinstance(effects, dict):
            continue
        for effect, options in effects.items():
            if not isinstance(options, list) or not options:
                continue
            option = max(
                (row for row in options if isinstance(row, dict)),
                key=lambda row: as_number(row.get("value")),
                default=None,
            )
            if option is None:
                continue
            rows.append({
                "skill": skill,
                "skill_key": canonical_skill(skill),
                "effect": _team_score_glyph_effect_key(effect),
                "option": _team_score_glyph_option_text(option),
                "value": max(0.0, as_number(option.get("value"))),
            })
    return apply_damage_cooldown_glyph_policy(rows)


def _team_score_without_damage_glyphs(build: dict[str, Any] | None) -> dict[str, Any] | None:
    """Clone one build while keeping only timing/utility plates on its axis."""

    if not isinstance(build, dict):
        return build
    cloned = copy.deepcopy(build)
    glyphs = cloned.get("skill_glyphs")
    if not isinstance(glyphs, dict):
        return cloned
    glyphs["rows"] = [
        copy.deepcopy(row)
        for row in glyphs.get("rows", ())
        if isinstance(row, dict)
        and _team_score_glyph_effect_key(row.get("effect")) != "伤害增加"
    ]
    return cloned


def _team_score_fixed_axis_skill_contributions(
    plan: dict[str, Any],
) -> dict[str, float]:
    contributions: dict[str, float] = defaultdict(float)
    for segment in plan.get("segments", ()):
        if not isinstance(segment, dict):
            continue
        skill_key = canonical_skill(segment.get("skill"))
        if skill_key:
            contributions[skill_key] += max(
                0.0, as_number(segment.get("effective_score"))
            )
    return dict(contributions)


def _team_score_refine_fixed_axis_damage_glyphs(
    store: Any,
    job: str,
    selected_rows: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    count: int = 4,
) -> list[dict[str, Any]]:
    """Replace rough damage-plate picks with measured fixed-axis gains."""

    timing_rows = [
        copy.deepcopy(row)
        for row in selected_rows
        if isinstance(row, dict)
        and (_team_score_glyph_effect_key(row.get("effect")) != "伤害增加"
             or row.get("selection_source") == "combat_inferred")
    ]
    remaining = max(0, int(count) - len(timing_rows))
    if remaining <= 0:
        return timing_rows[:count]
    occupied = {
        canonical_skill(row.get("skill"))
        for row in timing_rows
        if canonical_skill(row.get("skill"))
    }
    selected_damage = select_fixed_axis_damage_glyphs(
        _team_score_highest_glyph_candidates(store, job),
        remaining,
        _team_score_fixed_axis_skill_contributions(plan),
        plan.get("summary", {}).get("final_percent", 0.0),
        occupied_skills=occupied,
        job=job,
    )
    cleaned_damage = [
        {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key not in {"score", "skill_key"}
        }
        for row in selected_damage
    ]
    if any(row.get("selection_source") in {"combat_inferred", "auto_fill"} for row in selected_rows):
        for row in cleaned_damage:
            row["selection_source"] = "auto_fill"
        # A short/no-output interval can give every remaining damage plate a
        # zero marginal value. Preserve the already ranked fill choices rather
        # than collapsing a four-slot score build back to one or two plates.
        occupied.update(canonical_skill(row.get("skill")) for row in cleaned_damage)
        for row in selected_rows:
            if len(timing_rows) + len(cleaned_damage) >= count:
                break
            key = canonical_skill(row.get("skill"))
            if row.get("selection_source") == "auto_fill" and key not in occupied:
                cleaned_damage.append(copy.deepcopy(row))
                occupied.add(key)
    return [*timing_rows, *cleaned_damage][:count]


_TEAM_SCORE_GLYPH_METRIC_FIELDS = (
    "raw_score",
    "effective_score",
    "timeline_raw_score",
    "raw_effective_score",
    "dpa",
    "dps",
    "snapshot_full_score",
    "actual_damage",
    "raw_actual_damage",
    "_score_actual_damage",
    "_score_raw_actual_damage",
)
_TEAM_SCORE_GLYPH_SCORE_MAP_FIELDS = (
    "channel_scores",
    "timeline_attack_scores",
    "metric_channel_scores",
)


def _team_score_scale_glyph_metric(row: dict[str, Any], ratio: float) -> None:
    for field in _TEAM_SCORE_GLYPH_METRIC_FIELDS:
        if field in row and isinstance(row.get(field), (int, float)):
            row[field] = as_number(row.get(field)) * ratio
    for field in _TEAM_SCORE_GLYPH_SCORE_MAP_FIELDS:
        values = row.get(field)
        if isinstance(values, dict):
            row[field] = {
                key: as_number(value) * ratio
                for key, value in values.items()
            }


def _team_score_damage_glyph_multipliers(
    rows: list[dict[str, Any]], final_percent: float
) -> dict[str, float]:
    multipliers: dict[str, float] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or _team_score_glyph_effect_key(row.get("effect")) != "伤害增加"
        ):
            continue
        skill_key = canonical_skill(row.get("skill"))
        if not skill_key:
            continue
        multipliers[skill_key] = max(
            multipliers.get(skill_key, 1.0),
            damage_glyph_multiplier(row.get("value"), final_percent),
        )
    return multipliers


def _team_score_apply_fixed_axis_damage_glyphs(
    plan: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    previous_rows: list[dict[str, Any]] | None = None,
) -> None:
    """Revalue damage on an unchanged axis; cast order and counts stay intact."""

    summary = plan.get("summary", {}) if isinstance(plan.get("summary"), dict) else {}
    final_percent = max(0.0, as_number(summary.get("final_percent")))
    old = _team_score_damage_glyph_multipliers(previous_rows or [], final_percent)
    new = _team_score_damage_glyph_multipliers(selected_rows, final_percent)
    ratios = {
        skill: new.get(skill, 1.0) / max(1e-9, old.get(skill, 1.0))
        for skill in set(old) | set(new)
        if abs(new.get(skill, 1.0) - old.get(skill, 1.0)) > 1e-12
    }
    if not ratios:
        return

    def scale_segment(segment: dict[str, Any]) -> None:
        ratio = ratios.get(canonical_skill(segment.get("skill")), 1.0)
        if abs(ratio - 1.0) <= 1e-12:
            return
        _team_score_scale_glyph_metric(segment, ratio)
        for piece in segment.get("_score_damage_slices", ()):
            if isinstance(piece, dict):
                _team_score_scale_glyph_metric(piece, ratio)
        scaled_metric_ids: set[int] = set()
        for field in ("metric", "_score_metric", "periodic_metric"):
            metric = segment.get(field)
            if isinstance(metric, dict) and id(metric) not in scaled_metric_ids:
                _team_score_scale_glyph_metric(metric, ratio)
                scaled_metric_ids.add(id(metric))

    for segment in plan.get("segments", ()):
        if isinstance(segment, dict):
            scale_segment(segment)
    for map_name in ("_score_actual_cast_segments", "_score_actual_periodic_segments"):
        by_source = plan.get(map_name)
        if not isinstance(by_source, dict):
            continue
        for segments in by_source.values():
            for segment in (
                segments if isinstance(segments, (list, tuple)) else ()
            ):
                if isinstance(segment, dict):
                    scale_segment(segment)

    for map_name in ("_score_skill_powers", "_score_skill_damages"):
        values = plan.get(map_name)
        if not isinstance(values, dict):
            continue
        for skill, value in list(values.items()):
            values[skill] = as_number(value) * ratios.get(
                canonical_skill(skill), 1.0
            )
    components = plan.get("_score_skill_components")
    if isinstance(components, dict):
        scalable = {
            "physical",
            "magic",
            "power",
            "actual_physical_damage",
            "actual_magic_damage",
            "actual_damage",
        }
        for skill, component in components.items():
            if not isinstance(component, dict):
                continue
            ratio = ratios.get(canonical_skill(skill), 1.0)
            for field in scalable:
                if field in component:
                    component[field] = as_number(component.get(field)) * ratio

    total = sum(
        max(0.0, as_number(segment.get("effective_score")))
        for segment in plan.get("segments", ())
        if isinstance(segment, dict)
    )
    raw_total = sum(
        max(0.0, as_number(segment.get("raw_effective_score")))
        for segment in plan.get("segments", ())
        if isinstance(segment, dict)
    )
    window = max(0.0, as_number(plan.get("window")))
    average = total / window if window > 0 else 0.0
    plan["total"] = total
    plan["raw_total"] = raw_total
    plan["average"] = average
    plan["dps"] = (
        max(0.0, as_number(plan.get("damage_panel")))
        * average
        / 100.0
        * max(0.0, as_number(plan.get("hand_multiplier"), 1.0))
    )

def _team_score_apply_boss_preset(
    build: dict[str, Any], boss_name: str
) -> None:
    preset = BOSS_PRESETS.get(norm(boss_name), BOSS_PRESETS[_TEAM_SCORE_DEFAULT_BOSS_PRESET])
    element_resist = as_number(preset.get("element_resist"))
    monster_defense = int(as_number(preset.get("monster_defense")))
    build["boss"] = norm(boss_name) or _TEAM_SCORE_DEFAULT_BOSS_PRESET
    build["crit_resist_value"] = int(as_number(preset.get("crit_resist")))
    build["element_resist_percent"] = element_resist
    build["element_resists"] = {
        element: element_resist for element in ("火", "水", "光", "暗")
    }
    build["monster_defense"] = monster_defense
    build["monster_defense_rate"] = BuildCalculator.defense_percent(
        monster_defense
    )

def _team_score_support_effect_group(skill_name: Any) -> str:
    """Return the scoring unit removed by one support counterfactual."""

    key = canonical_skill(skill_name)
    return _TEAM_SCORE_AURA_GROUP_KEY if key in _TEAM_SCORE_AURA_SKILL_KEYS else key

def _team_score_team_dps(
    results_by_job: dict[str, dict[str, Any]],
    jobs: tuple[str, ...],
) -> float:
    """Sum one planned DPS value per party slot, including duplicate jobs."""

    return sum(
        max(0.0, as_number(results_by_job.get(job, {}).get("dps")))
        for job in jobs
        if job
    )


def _team_score_team_dps_by_source(
    results_by_source: dict[str, dict[str, Any]],
    players: tuple[object, ...],
) -> float:
    """Sum the exact plan owned by each runtime player ID."""

    return sum(
        max(
            0.0,
            as_number(
                results_by_source.get(
                    str(getattr(player, "source_id", "")), {}
                ).get("dps")
            ),
        )
        for player in players
        if str(getattr(player, "source_id", ""))
    )


def _team_score_recipient_damage_weights(
    players: tuple[object, ...],
    combat_events: tuple[CombatEvent, ...],
    boss_target_ids: frozenset[str] | None,
    output_windows: tuple[tuple[float, float], ...],
) -> dict[str, float]:
    """Weight ideal team-Buff timing by each recipient's captured Boss work."""

    windows = _team_score_merge_intervals(output_windows)
    starts = [row[0] for row in windows]
    source_ids = {
        str(getattr(player, "source_id", ""))
        for player in players
        if str(getattr(player, "source_id", ""))
    }
    selected_targets = set(boss_target_ids or ())
    result: dict[str, float] = defaultdict(float)
    for event in combat_events:
        source_id = str(event.source_id)
        seconds = event.timestamp_ms / 1000.0
        index = bisect_right(starts, seconds) - 1
        if (
            event.healing
            or event.amount <= 0
            or source_id not in source_ids
            or selected_targets
            and event.target_id not in selected_targets
            or index < 0
            or seconds >= windows[index][1] - 1e-9
        ):
            continue
        result[source_id] += float(event.amount)
    if not any(value > 1e-9 for value in result.values()):
        return {
            str(getattr(player, "source_id", "")): max(
                1.0, as_number(getattr(player, "damage", 0.0))
            )
            for player in players
            if str(getattr(player, "source_id", ""))
        }
    return {
        source_id: max(1.0, result.get(source_id, 0.0))
        for source_id in source_ids
    }


def _team_score_recipient_damage_events(
    players: tuple[object, ...],
    combat_events: tuple[CombatEvent, ...],
    boss_target_ids: frozenset[str] | None,
    output_windows: tuple[tuple[float, float], ...],
    *,
    origin_seconds: float = 0.0,
) -> dict[str, tuple[tuple[float, float], ...]]:
    """Build a compact per-recipient damage-value stream for timing search.

    Amounts are normalized to each recipient's standard weight by the planner,
    so gear and raw DPS do not get counted twice.  Timestamps retain the real
    damage shape, allowing legal support casts to target valuable moments
    instead of merely maximizing online seconds.
    """

    windows = _team_score_merge_intervals(output_windows)
    starts = [row[0] for row in windows]
    source_ids = {
        str(getattr(player, "source_id", ""))
        for player in players
        if str(getattr(player, "source_id", ""))
    }
    selected_targets = set(boss_target_ids or ())
    buckets: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    for event in combat_events:
        source_id = str(event.source_id)
        seconds = event.timestamp_ms / 1000.0
        index = bisect_right(starts, seconds) - 1
        if (
            event.healing
            or event.amount <= 0
            or source_id not in source_ids
            or selected_targets
            and event.target_id not in selected_targets
            or index < 0
            or seconds >= windows[index][1] - 1e-9
        ):
            continue
        # A 20 ms bucket removes repeated-hit noise without changing any
        # support decision made on the planner's 0.5-second candidate grid.
        local_seconds = round((seconds - origin_seconds) / 0.02) * 0.02
        buckets[source_id][round(max(0.0, local_seconds), 6)] += float(event.amount)
    return {
        source_id: tuple(sorted(rows.items()))
        for source_id, rows in buckets.items()
        if rows
    }


def _team_score_theory_damage_objective(
    results_by_source: dict[str, dict[str, Any]],
    players: tuple[object, ...],
    output_windows: tuple[tuple[float, float], ...],
) -> tuple[
    dict[str, float],
    dict[str, tuple[tuple[float, float], ...]],
]:
    """Build the timing-search objective from the neutral theory axes.

    A legal support ceiling must not inherit a poor captured rotation.  The
    neutral (no CDR/action-speed) plans provide one standard-damage stream per
    real recipient.  Timestamps describe where that job's legal rotation puts
    damage, while ``effective_score`` supplies its standard value.  The
    optimizer can then place one shared support cast schedule against the whole
    theoretical party rather than chasing the capture's accidental burst gaps.
    """

    windows = _team_score_merge_intervals(output_windows)
    starts = [row[0] for row in windows]

    def in_output_window(second: float) -> bool:
        index = bisect_right(starts, second) - 1
        return index >= 0 and second < windows[index][1] - 1e-9

    weights: dict[str, float] = {}
    events: dict[str, tuple[tuple[float, float], ...]] = {}
    for player in players:
        source_id = str(getattr(player, "source_id", ""))
        plan = results_by_source.get(source_id, {})
        if not source_id or not isinstance(plan, dict):
            continue
        buckets: dict[float, float] = defaultdict(float)
        objective_segments = plan.get('segments', ())
        if '_timing_damage_events' in plan:
            objective_segments = ({'kind':'damage', 'start':start, 'effective_score':amount}
                                  for start, amount in plan['_timing_damage_events'])
        for segment in objective_segments:
            if not isinstance(segment, dict) or segment.get("kind") != "damage":
                continue
            amount = max(0.0, as_number(segment.get("effective_score")))
            second = max(0.0, as_number(segment.get("start")))
            if amount <= 1e-12 or not in_output_window(second):
                continue
            # Match the support search's 20 ms evidence buckets.  A theory
            # segment is deliberately anchored at its damage action start;
            # the exact T-axis worker remains authoritative for the resulting
            # cooldown/action cascade and final team DPS.
            bucket = round(second / 0.02) * 0.02
            buckets[round(bucket, 6)] += amount
        if not buckets:
            continue
        rows = tuple(sorted(buckets.items()))
        total = sum(amount for _second, amount in rows)
        events[source_id] = rows
        weights[source_id] = max(
            1e-9,
            as_number(plan.get("dps"), total),
        )
    return weights, events


def _team_score_damage_value_coverage(
    intervals_by_source: dict[str, list[tuple[float, float]]],
    damage_events_by_source: dict[str, tuple[tuple[float, float], ...]],
    recipient_weights: dict[str, float],
) -> float | None:
    """Return normalized relevant-damage coverage, or None without evidence."""

    denominator = 0.0
    numerator = 0.0
    for source_id, events in damage_events_by_source.items():
        raw_total = sum(max(0.0, as_number(amount)) for _second, amount in events)
        if raw_total <= 1e-9:
            continue
        standard_weight = max(
            0.0, as_number(recipient_weights.get(source_id), 1.0)
        )
        if standard_weight <= 1e-9:
            standard_weight = 1.0
        denominator += standard_weight
        active = _team_score_merge_intervals(intervals_by_source.get(source_id, ()))
        if not active:
            continue
        starts = [row[0] for row in active]
        covered_raw = 0.0
        for second, amount in events:
            index = bisect_right(starts, second) - 1
            if index >= 0 and second < active[index][1] - 1e-9:
                covered_raw += max(0.0, as_number(amount))
        numerator += standard_weight * covered_raw / raw_total
    if denominator <= 1e-9:
        return None
    return max(0.0, min(1.0, numerator / denominator))


def _team_score_joint_objective_profiles(
    players: tuple[object, ...],
    jobs: tuple[str, ...],
    combat_events: tuple[CombatEvent, ...],
    boss_target_ids: frozenset[str] | None,
    output_windows: tuple[tuple[float, float], ...],
    profile_for_job: Any,
    boss_preset: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build one lightweight team-damage objective for each planned job.

    The timeline planner scores one character at a time.  These profiles turn
    the other seven members' captured Boss damage into owner-relative weights,
    so a team Buff/Debuff can be compared on the same scale as the owner's
    standard damage without simulating seven extra rotations per candidate.
    """

    if not players or not jobs:
        return {}
    windows = _team_score_merge_intervals(output_windows)
    window_starts = [row[0] for row in windows]
    selected_targets = set(boss_target_ids or ())
    damage_by_source: dict[str, float] = defaultdict(float)

    def in_output_window(seconds: float) -> bool:
        index = bisect_right(window_starts, seconds) - 1
        return index >= 0 and seconds < windows[index][1] - 1e-9

    player_sources = {str(getattr(player, "source_id", "")) for player in players}
    for event in combat_events:
        source_id = str(event.source_id)
        if (
            event.healing
            or event.amount <= 0
            or source_id not in player_sources
            or selected_targets
            and event.target_id not in selected_targets
            or not in_output_window(event.timestamp_ms / 1000.0)
        ):
            continue
        damage_by_source[source_id] += float(event.amount)

    rows: list[dict[str, Any]] = []
    for player, job in zip(players, jobs):
        try:
            profile = profile_for_job(job) if callable(profile_for_job) else {}
        except Exception:
            profile = {}
        attack_attrs = [
            norm(value)
            for value in profile.get("attack_attrs", ())
            if norm(value) in {"物攻", "魔攻"}
        ]
        if not attack_attrs:
            attack = norm(profile.get("attack_attr"))
            attack_attrs = [attack if attack in {"物攻", "魔攻"} else "物攻"]
        physical_share = attack_attrs.count("物攻") / len(attack_attrs)
        magic_share = attack_attrs.count("魔攻") / len(attack_attrs)
        element = norm(profile.get("element"))
        rows.append({
            "source_id": str(getattr(player, "source_id", "")),
            "job": norm(job),
            "damage": max(
                0.0,
                damage_by_source.get(str(getattr(player, "source_id", "")), 0.0),
            ),
            "physical_share": physical_share,
            "magic_share": magic_share,
            "main_stat": norm(profile.get("main_stat")),
            "element": element if element in {"火", "水", "光", "暗"} else "",
        })

    total_damage = sum(row["damage"] for row in rows)
    if total_damage <= 1e-9:
        for row in rows:
            row["damage"] = 1.0
        total_damage = float(len(rows))

    fallback_owner_damage = total_damage / max(1, len(rows))
    common = {
        "element_resist": as_number(boss_preset.get("element_resist")),
        "monster_defense": max(0.0, as_number(boss_preset.get("monster_defense"))),
        "crit_resist": max(0.0, as_number(boss_preset.get("crit_resist"))),
        "freeze_coefficient": max(
            0.0,
            as_number(boss_preset.get("freeze_coefficient"), 0.30),
        ),
        "base_attack": 30000.0,
        "base_element_attack": 42.0,
    }
    profiles: dict[str, dict[str, Any]] = {}
    for owner_job in dict.fromkeys(norm(job) for job in jobs if norm(job)):
        owner_rows = [row for row in rows if row["job"] == owner_job]
        owner_damage = (
            sum(row["damage"] for row in owner_rows) / len(owner_rows)
            if owner_rows
            else fallback_owner_damage
        )
        owner_damage = max(1.0, owner_damage or fallback_owner_damage)
        same_job_total = sum(row["damage"] for row in owner_rows)
        excluded_ratio = (
            min(1.0, owner_damage / same_job_total)
            if same_job_total > 1e-9
            else 0.0
        )
        members: list[dict[str, Any]] = []
        for row in rows:
            remaining_damage = row["damage"]
            if row["job"] == owner_job:
                remaining_damage *= 1.0 - excluded_ratio
            if remaining_damage <= 1e-9:
                continue
            members.append({
                "job": row["job"],
                "weight": remaining_damage / owner_damage,
                "physical_share": row["physical_share"],
                "magic_share": row["magic_share"],
                "main_stat": row["main_stat"],
                "element": row["element"],
            })
        profiles[owner_job] = {
            **common,
            "owner_job": owner_job,
            "owner_damage": owner_damage,
            "other_damage": sum(
                member["weight"] * owner_damage for member in members
            ),
            "members": members,
        }
    return profiles

def _team_score_scale_objective_profiles(
    profiles: dict[str, dict[str, Any]],
    scale: float,
) -> dict[str, dict[str, Any]]:
    """Scale only the scheduling value of teammates, not scored contribution."""

    factor = max(0.0, float(scale))
    scaled = copy.deepcopy(profiles)
    for profile in scaled.values():
        if not isinstance(profile, dict):
            continue
        for member in profile.get("members", ()):
            if isinstance(member, dict):
                member["weight"] = max(
                    0.0, as_number(member.get("weight")) * factor
                )
        profile["other_damage"] = max(
            0.0, as_number(profile.get("other_damage")) * factor
        )
    return scaled

def _team_score_standalone_gain_percent(
    neutral_dps: float, improved_dps: float
) -> float:
    """Return a timing effect's gain over the same team without timing buffs."""

    if neutral_dps <= 1e-9:
        return 0.0
    return max(0.0, (improved_dps - neutral_dps) / neutral_dps * 100.0)

def _team_score_effect_changes_rotation(effect: dict[str, Any]) -> bool:
    """Whether removing this effect can change cooldowns or cast throughput."""

    for raw_key, raw_value in effect.get("attrs", {}).items():
        key = norm(raw_key)
        if abs(as_number(raw_value)) <= 1e-9:
            continue
        if "动作速度" in key or "攻速" in key:
            return True
        if "冷却" in key and any(marker in key for marker in ("缩减", "减少", "恢复")):
            return True
    return False


def _team_score_weighted_interval_coverage(
    intervals_by_source: dict[str, list[tuple[float, float]]],
    output_windows: tuple[tuple[float, float], ...],
    recipient_weights: dict[str, float],
) -> float:
    """Return recipient-damage-weighted uptime inside output windows."""

    weighted = 0.0
    weight_total = 0.0
    for source_id, weight in recipient_weights.items():
        safe_weight = max(0.0, as_number(weight))
        if safe_weight <= 1e-9:
            safe_weight = 1.0
        weighted += safe_weight * _coverage_in_windows(
            intervals_by_source.get(source_id, ()), output_windows
        )
        weight_total += safe_weight
    return weighted / weight_total if weight_total > 1e-9 else 0.0


def _team_score_optimal_shared_timing_schedule(
    effects: list[dict[str, Any]],
    timing_units: set[tuple[str, str]],
    expectations_by_unit: dict[tuple[str, str], tuple[object, ...]],
    output_windows: tuple[tuple[float, float], ...],
    window: float,
    recipient_source_ids: tuple[str, ...],
    clear_times_by_source: dict[str, tuple[float, ...]],
    recipient_weights: dict[str, float],
    *,
    effect_skill_name: Any = norm,
    effect_cast_time: Any = None,
    actual_cast_schedule: dict[
        tuple[str, str], list[tuple[float, float]]
    ] | None = None,
    recipient_damage_events: dict[
        str, tuple[tuple[float, float], ...]
    ] | None = None,
) -> tuple[
    dict[tuple[str, str], list[tuple[float, float]]],
    dict[tuple[str, str], float],
]:
    """Plan one legal shared timing schedule for support-only counterfactuals.

    The selected provider cast times are shared by every recipient. Buff clear
    points remain recipient-specific, so one player can lose Spirit Boost or
    Time Acceleration while another keeps it. Candidate value follows the
    normalized team-damage stream at each timestamp rather than raw uptime.
    The complete recorded cast scheme is also a candidate, which guarantees
    that the legal ceiling cannot be worse than the observed scheme under the
    same objective. The full timeline engine still performs the exact
    cooldown/action cascade.
    """

    horizon = max(0.0, as_number(window))
    if horizon <= 1e-9 or not timing_units or not recipient_source_ids:
        return {}, {}

    rows_by_unit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for effect in effects:
        unit = (
            norm(effect.get("provider")),
            _team_score_support_effect_group(
                effect_skill_name(effect.get("skill"))
            ),
        )
        if unit in timing_units:
            rows_by_unit[unit].append(effect)

    weights = {
        source_id: max(0.0, as_number(recipient_weights.get(source_id)))
        for source_id in recipient_source_ids
    }
    if not any(value > 1e-9 for value in weights.values()):
        weights = {source_id: 1.0 for source_id in recipient_source_ids}
    else:
        weights = {
            source_id: (value if value > 1e-9 else 1.0)
            for source_id, value in weights.items()
        }
    output_seconds = sum(
        max(0.0, min(horizon, end) - max(0.0, start))
        for start, end in output_windows
        if end > 0.0 and start < horizon
    )
    coverage_denominator = output_seconds * sum(weights.values())

    recorded_starts_by_unit: dict[tuple[str, str], set[float]] = defaultdict(set)
    for key, intervals in (actual_cast_schedule or {}).items():
        if not isinstance(key, tuple) or len(key) < 2:
            continue
        unit = (
            norm(key[0]),
            _team_score_support_effect_group(effect_skill_name(key[1])),
        )
        if unit not in timing_units:
            continue
        for interval in intervals:
            if not isinstance(interval, (list, tuple)) or len(interval) < 2:
                continue
            active_start = as_number(interval[1])
            if -1e-9 <= active_start <= horizon + 1e-9:
                recorded_starts_by_unit[unit].add(
                    round(max(0.0, min(horizon, active_start)), 6)
                )

    # Prefix sums make timestamp-valued candidate evaluation effectively O(1)
    # per recipient/interval even for long captures with many damage events.
    damage_times: dict[str, tuple[float, ...]] = {}
    damage_prefixes: dict[str, tuple[float, ...]] = {}
    damage_denominator = 0.0
    for source_id in recipient_source_ids:
        rows = sorted((recipient_damage_events or {}).get(source_id, ()))
        raw_total = sum(max(0.0, as_number(amount)) for _second, amount in rows)
        if raw_total <= 1e-9:
            continue
        standard_weight = weights[source_id]
        scale = standard_weight / raw_total
        running = 0.0
        prefix = [0.0]
        times: list[float] = []
        for second, amount in rows:
            times.append(as_number(second))
            running += max(0.0, as_number(amount)) * scale
            prefix.append(running)
        damage_times[source_id] = tuple(times)
        damage_prefixes[source_id] = tuple(prefix)
        damage_denominator += standard_weight

    def recipient_interval_value(
        source_id: str,
        intervals: tuple[tuple[float, float], ...],
    ) -> float:
        if damage_denominator <= 1e-9:
            return weights[source_id] * sum(
                max(0.0, min(end, output_end) - max(start, output_start))
                for start, end in intervals
                for output_start, output_end in output_windows
            )
        times = damage_times.get(source_id, ())
        prefix = damage_prefixes.get(source_id, ())
        if not times or not prefix:
            return 0.0
        value = 0.0
        for start, end in _team_score_merge_intervals(intervals):
            left = bisect_left(times, start - 1e-9)
            right = bisect_left(times, end - 1e-9)
            value += prefix[right] - prefix[left]
        return value

    objective_denominator = (
        damage_denominator if damage_denominator > 1e-9 else coverage_denominator
    )

    schedules: dict[tuple[str, str], list[tuple[float, float]]] = {}
    coverage_by_unit: dict[tuple[str, str], float] = {}
    for unit in sorted(timing_units):
        effect_rows = rows_by_unit.get(unit, ())
        expectation_rows = expectations_by_unit.get(unit, ())
        duration = max(
            [max(0.0, as_number(row.get("duration"))) for row in effect_rows]
            + [
                max(0.0, as_number(getattr(row, "duration", 0.0)))
                for row in expectation_rows
            ]
            + [0.0]
        )
        positive_cooldowns = [
            max(0.0, as_number(row.get("cooldown")))
            for row in effect_rows
            if as_number(row.get("cooldown")) > 1e-9
        ] + [
            max(0.0, as_number(getattr(row, "cooldown", 0.0)))
            for row in expectation_rows
            if as_number(getattr(row, "cooldown", 0.0)) > 1e-9
        ]
        cooldown = min(positive_cooldowns, default=duration)
        if duration <= 1e-9:
            continue
        spacing = max(0.5, duration, cooldown)
        representative = effect_rows[0] if effect_rows else {}
        cast_time = 0.5
        if callable(effect_cast_time) and representative:
            cast_time = max(
                0.0, as_number(effect_cast_time(representative), 0.5)
            )
        elif representative:
            cast_time = max(
                0.0, as_number(representative.get("cast_time"), 0.5)
            )
        if cast_time <= 1e-9:
            cast_time = 0.5

        candidates = {
            round(index * 0.5, 6)
            for index in range(int(horizon / 0.5) + 1)
        }
        candidates.update({0.0, round(horizon, 6)})
        candidates.update(recorded_starts_by_unit.get(unit, ()))
        for start, end in output_windows:
            for value in (start, end - duration):
                if -1e-9 <= value <= horizon + 1e-9:
                    candidates.add(round(max(0.0, min(horizon, value)), 6))
        if player_buff_clear_affects_timeline_skill(unit[1]):
            for clear_rows in clear_times_by_source.values():
                for clear_time in clear_rows:
                    for value in (clear_time, clear_time - duration):
                        if -1e-9 <= value <= horizon + 1e-9:
                            candidates.add(
                                round(max(0.0, min(horizon, value)), 6)
                            )
        starts = sorted(candidates)

        values: list[float] = []
        for active_start in starts:
            active_end = min(horizon, active_start + duration)
            value = 0.0
            for source_id in recipient_source_ids:
                intervals: tuple[tuple[float, float], ...] = (
                    ((active_start, active_end),)
                    if active_end > active_start + 1e-9
                    else ()
                )
                if player_buff_clear_affects_timeline_skill(unit[1]):
                    intervals = clip_player_buff_intervals_at_clears(
                        intervals,
                        clear_times_by_source.get(source_id, ()),
                        skill_name=unit[1],
                    )
                value += recipient_interval_value(source_id, intervals)
            values.append(value)

        # Weighted interval scheduling over possible Buff start times.  Using
        # max(duration, cooldown) prevents overlapping refreshes from claiming
        # the same output second twice.
        best = [0.0] * (len(starts) + 1)
        take = [False] * (len(starts) + 1)
        previous = [-1] * len(starts)
        for index, active_start in enumerate(starts):
            previous[index] = bisect_right(
                starts, active_start - spacing + 1e-9, 0, index
            ) - 1
            take_value = values[index] + best[previous[index] + 1]
            skip_value = best[index]
            if take_value > skip_value + 1e-9:
                best[index + 1] = take_value
                take[index + 1] = True
            else:
                best[index + 1] = skip_value

        chosen: list[float] = []
        cursor = len(starts)
        while cursor > 0:
            if take[cursor]:
                selected_index = cursor - 1
                chosen.append(starts[selected_index])
                cursor = previous[selected_index] + 1
            else:
                cursor -= 1
        chosen.reverse()

        def schedule_value(active_starts: list[float]) -> float:
            value = 0.0
            for source_id in recipient_source_ids:
                intervals: tuple[tuple[float, float], ...] = tuple(
                    (start, min(horizon, start + duration))
                    for start in active_starts
                    if start < horizon - 1e-9
                )
                if player_buff_clear_affects_timeline_skill(unit[1]):
                    intervals = clip_player_buff_intervals_at_clears(
                        intervals,
                        clear_times_by_source.get(source_id, ()),
                        skill_name=unit[1],
                    )
                value += recipient_interval_value(source_id, intervals)
            return value

        selected_value = best[-1]
        recorded_starts = sorted(recorded_starts_by_unit.get(unit, ()))
        if recorded_starts:
            recorded_value = schedule_value(recorded_starts)
            if recorded_value >= selected_value - 1e-9:
                chosen = recorded_starts
                selected_value = recorded_value
        casts = [
            (
                (-cast_time if start <= 1e-9 else max(0.0, start - cast_time)),
                start,
            )
            for start in chosen
        ]
        for effect in effect_rows:
            key = (
                unit[0],
                canonical_skill(effect_skill_name(effect.get("skill"))),
            )
            schedules[key] = list(casts)
        coverage_by_unit[unit] = (
            min(1.0, max(0.0, selected_value / objective_denominator))
            if objective_denominator > 1e-9
            else 0.0
        )
    return schedules, coverage_by_unit

def _team_score_effects_with_required_cdr_glyphs(
    editor: object,
    effects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the mandatory Ranger/Mystic CDR glyphs to scoring effects."""

    apply_glyphs = getattr(editor, "_team_effects_with_auto_cdr_glyphs", None)
    if not callable(apply_glyphs):
        return list(effects)
    adjusted = apply_glyphs(effects)
    return adjusted if isinstance(adjusted, list) else list(effects)

def _team_score_merge_intervals(
    intervals: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> list[tuple[float, float]]:
    rows = sorted(
        (max(0.0, float(start)), max(0.0, float(end)))
        for start, end in intervals
        if end > start + 1e-9
    )
    merged: list[tuple[float, float]] = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

def _team_score_recorded_avatar_cast_windows(
    buff_series: Any, cast_events: Any,
) -> dict[tuple[str, int, str], tuple[float, float]]:
    """Match finite server Avatar windows to their owner's preceding cast.

    Only packet-backed self Buffs are authoritative here. Missing evidence
    leaves actual replay on its existing action-duration fallback.
    """
    casts_by_source: dict[str, list[int]] = defaultdict(list)
    avatar_key = canonical_skill("神圣化身")
    for event in cast_events:
        if canonical_skill(event.skill_name) == avatar_key:
            casts_by_source[str(event.source_id)].append(int(event.timestamp_ms))
    for times in casts_by_source.values():
        times.sort()
    result: dict[tuple[str, int, str], tuple[float, float]] = {}
    for row in buff_series:
        source = str(row.source_id)
        if (
            canonical_skill(row.skill_name) != avatar_key
            or str(row.target_id) != source
            or getattr(row, "duration_source", "") != "packet"
            or getattr(row, "open_ended", False)
        ):
            continue
        times = casts_by_source.get(source, [])
        for start, end in row.intervals:
            if end <= start:
                continue
            index = bisect_right(times, round(start * 1000)) - 1
            if index < 0:
                continue
            key = (source, times[index], avatar_key)
            previous = result.get(key)
            # Attribute packets for the same Buff can arrive a few ms apart.
            # Use the first authoritative interval, never extend its expiry.
            if previous is None or start < previous[0]:
                result[key] = (float(start), float(end))
    return result


def _team_score_recorded_cdr_cast_schedule(
    snapshot: CombatSnapshot,
    players: tuple[object, ...],
    effects: list[dict[str, Any]],
    *,
    effect_skill_name: Any = norm,
    provider_jobs: tuple[str, ...] | None = None,
    target_id: str | None = None,
    fallback_to_casts: bool = True,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Recover real CDR windows, falling back to casts when status packets are absent."""

    cdr_skills = {
        canonical_skill("精神爆发"),
        canonical_skill("灼热火焰"),
        canonical_skill("时间加速"),
    }
    duration_by_effect: dict[tuple[str, str], float] = {}
    duration_by_skill: dict[str, float] = {}
    for effect in effects:
        skill_key = canonical_skill(effect_skill_name(effect.get("skill")))
        if skill_key not in cdr_skills:
            continue
        duration = max(0.0, as_number(effect.get("duration")))
        if duration <= 0:
            continue
        provider = norm(effect.get("provider"))
        duration_by_effect[(provider, skill_key)] = max(
            duration_by_effect.get((provider, skill_key), 0.0), duration
        )
        duration_by_skill[skill_key] = max(
            duration_by_skill.get(skill_key, 0.0), duration
        )

    fallback_durations = {
        canonical_skill("精神爆发"): 14.0,
        canonical_skill("灼热火焰"): 14.0,
        canonical_skill("时间加速"): 15.0,
    }
    schedule: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for member_index, player in enumerate(players):
        source_id = norm(getattr(player, "source_id", ""))
        job = (
            norm(provider_jobs[member_index])
            if provider_jobs is not None and member_index < len(provider_jobs)
            else norm(getattr(player, "job", ""))
        )
        provider = f"队员{member_index + 1} {job}"
        by_skill: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for row in event_platform_for(snapshot).player_buff_series:
            if norm(row.source_id) != source_id:
                continue
            if target_id is not None and str(row.target_id) != target_id:
                continue
            skill_key = canonical_skill(row.skill_name)
            if skill_key in cdr_skills:
                by_skill[skill_key].extend(
                    _recorded_timing_buff_intervals(row, skill_key)
                )

        recorded_skills = {skill for skill, rows in by_skill.items() if rows}
        if fallback_to_casts:
            for event in event_platform_for(snapshot).skill_cast_events:
                if norm(event.source_id) != source_id:
                    continue
                skill_key = canonical_skill(event.skill_name)
                if skill_key not in cdr_skills or skill_key in recorded_skills:
                    continue
                duration = duration_by_effect.get(
                    (provider, skill_key),
                    duration_by_skill.get(skill_key, fallback_durations[skill_key]),
                )
                start = max(0.0, event.timestamp_ms / 1000.0)
                by_skill[skill_key].append((start, start + duration))

        for skill_key, intervals in by_skill.items():
            merged = _team_score_merge_intervals(intervals)
            if merged:
                schedule[(provider, skill_key)] = merged
    return schedule


def _team_score_recipient_buff_active_schedule(
    snapshot: CombatSnapshot,
    players: tuple[object, ...],
    effects: list[dict[str, Any]],
    target_id: str,
    *,
    effect_skill_name: Any = norm,
    effect_type: Any = None,
    provider_jobs: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Return authoritative player-Buff intervals for one party recipient."""

    source_by_provider: dict[str, tuple[str, str]] = {}
    for index, player in enumerate(players):
        job = (
            norm(provider_jobs[index])
            if provider_jobs is not None and index < len(provider_jobs)
            else norm(getattr(player, "job", ""))
        )
        source_by_provider[f"队员{index + 1} {job}"] = (
            str(getattr(player, "source_id", "")),
            job,
        )

    series_by_key: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in getattr(snapshot, "player_buff_series", ()):
        series_by_key[
            (
                str(getattr(row, "source_id", "")),
                str(getattr(row, "target_id", "")),
                canonical_skill(getattr(row, "skill_name", "")),
            )
        ].append(row)
    summary_by_key: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in event_platform_for(snapshot).player_buffs:
        summary_by_key[
            (
                str(getattr(row, "source_id", "")),
                str(getattr(row, "target_id", "")),
                canonical_skill(getattr(row, "skill_name", "")),
            )
        ].append(row)

    end_seconds = max(
        as_number(getattr(snapshot, "elapsed_seconds", 0.0)),
        as_number(getattr(snapshot, "dps_elapsed_seconds", 0.0)),
        as_number(getattr(snapshot, "boss_effect_current_seconds", 0.0)),
    )
    schedule: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for effect in effects:
        if callable(effect_type):
            _label, category = effect_type(effect)
            if category != "team_buff":
                continue
        provider = norm(effect.get("provider"))
        skill_key = canonical_skill(effect_skill_name(effect.get("skill")))
        if not provider or not skill_key:
            continue
        source_id, provider_job = source_by_provider.get(provider, ("", ""))
        if not source_id:
            continue
        key = (provider, skill_key)
        attrs = effect.get("display_attrs", effect.get("attrs", {}))
        expected_magnitude = max(
            (abs(as_number(value)) for value in attrs.values()),
            default=0.0,
        ) if isinstance(attrs, dict) else 0.0
        rows = series_by_key.get((source_id, target_id, skill_key), ())
        summary_rows = ()
        if not rows:
            summary_rows = summary_by_key.get(
                (source_id, target_id, skill_key), ()
            )
            rows = summary_rows

        # Some common auras are represented by one carrier status at a time.
        # For permanent companion auras, join all captured carrier rows before
        # projecting them to this recipient so death/rejoin gaps do not become
        # invented missing-Buff time.
        self_reported = {
            canonical_skill(name)
            for name in _HEALER_SELF_REPORTED_TEAM_AURAS.get(
                data_job_for(provider_job), frozenset()
            )
        }
        if skill_key in _PERSISTENT_TEAM_AURA_SKILL_KEYS:
            rows = tuple(
                row
                for (row_source, _row_target, row_skill), grouped
                in series_by_key.items()
                if row_source == source_id and row_skill == skill_key
                for row in grouped
            )
            summary_rows = ()
            if not rows:
                summary_rows = tuple(
                    row
                    for (row_source, _row_target, row_skill), grouped
                    in summary_by_key.items()
                    if row_source == source_id and row_skill == skill_key
                    for row in grouped
                )
                rows = summary_rows
        elif not rows and skill_key in self_reported:
            rows = series_by_key.get((source_id, source_id, skill_key), ())
            if not rows:
                summary_rows = summary_by_key.get(
                    (source_id, source_id, skill_key), ()
                )
                rows = summary_rows

        if skill_key == canonical_skill("灼热火焰"):
            rows = tuple(
                row for row in rows
                if abs(as_number(getattr(row, "value", None)) - 40.0) <= 0.5
                and (
                    bool(_recorded_timing_buff_intervals(row, skill_key))
                    if getattr(row, "intervals", None)
                    else 0 < as_number(getattr(row, "end_seconds", 0.0))
                    - as_number(getattr(row, "start_seconds", 0.0)) <= 28.0
                )
            )

        selected_rows = _primary_player_buff_effect_rows(
            rows, expected_magnitude
        )
        intervals: list[tuple[float, float]] = []
        for row in selected_rows:
            row_intervals = getattr(row, "intervals", ())
            if not row_intervals and row in summary_rows:
                row_intervals = ((row.start_seconds, row.end_seconds),)
            intervals.extend(row_intervals)
        bounded = _bounded_recorded_team_buff_intervals(
            snapshot,
            source_id,
            skill_key,
            max(0.0, as_number(effect.get("duration"))),
            intervals,
            end_seconds,
            target_id=(
                ""
                if skill_key in _PERSISTENT_TEAM_AURA_SKILL_KEYS
                else source_id
                if skill_key in self_reported
                else target_id
            ),
        )
        schedule[key] = list(bounded)
    return schedule


def _team_score_effects_for_recipient(
    effects: list[dict[str, Any]],
    active_schedule: dict[tuple[str, str], list[tuple[float, float]]],
    *,
    effect_skill_name: Any = norm,
    effect_type: Any = None,
) -> list[dict[str, Any]]:
    """Attach one recipient's factual Buff intervals to copied team effects."""

    prepared: list[dict[str, Any]] = []
    for effect in effects:
        copied = copy.deepcopy(effect)
        if callable(effect_type):
            _label, category = effect_type(effect)
            if category == "team_buff":
                key = (
                    norm(effect.get("provider")),
                    canonical_skill(effect_skill_name(effect.get("skill"))),
                )
                copied["_score_recipient_active_intervals"] = list(
                    active_schedule.get(key, ())
                )
        prepared.append(copied)
    return prepared

def _team_score_recorded_rotation_schedule(
    effects: list[dict[str, Any]],
    recorded_schedule: dict[tuple[str, str], list[tuple[float, float]]],
    *,
    effect_skill_name: Any = norm,
    pin_all_effects: bool = False,
    is_continuous_effect: Any = None,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Pin scoring rotations to effect casts recorded in this fight.

    Empty entries are deliberate: they stop the practical timeline engine
    from inventing a Spirit Boost, Scorching Flame or Time Acceleration cast,
    while unrelated team effects can still use their normal scheduling rules.
    ``pin_all_effects`` is used by the same-environment score axis: every
    intermittent Buff/Debuff is then fixed to the captured fight and an absent
    skill remains absent instead of being scheduled by the theoretical planner.
    """
    cdr_skills = {
        canonical_skill("精神爆发"),
        canonical_skill("灼热火焰"),
        canonical_skill("时间加速"),
    }
    schedule = {
        (norm(provider), canonical_skill(skill)): list(intervals)
        for (provider, skill), intervals in recorded_schedule.items()
        if pin_all_effects or canonical_skill(skill) in cdr_skills
    }
    for effect in effects:
        skill_key = canonical_skill(effect_skill_name(effect.get("skill")))
        provider = norm(effect.get("provider"))
        continuous = bool(
            callable(is_continuous_effect) and is_continuous_effect(effect)
        )
        if (
            provider
            and (
                skill_key in cdr_skills
                and _team_score_effect_changes_rotation(effect)
                or pin_all_effects and not continuous
            )
        ):
            schedule.setdefault((provider, skill_key), [])
    return schedule

def _team_score_recorded_effect_cast_schedule(
    snapshot: CombatSnapshot,
    players: tuple[object, ...],
    effects: list[dict[str, Any]],
    *,
    observed_cast_timings: dict[tuple[str, int, str], tuple[float, float]] | None = None,
    effect_skill_name: Any = norm,
    effect_cast_time: Any = None,
    is_continuous_effect: Any = None,
    provider_jobs: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Return real cast intervals for every intermittent team effect.

    Status packets remain the authority for the active CDR windows handled by
    ``_team_score_recorded_cdr_cast_schedule``.  This companion schedule uses
    captured action events for ordinary Buffs and Debuffs so the score theory
    axis cannot silently move them to a more convenient timestamp.
    """

    provider_by_source = {
        norm(getattr(player, "source_id", "")): (
            f"队员{index + 1} "
            f"{norm(provider_jobs[index]) if provider_jobs is not None and index < len(provider_jobs) else norm(getattr(player, 'job', ''))}"
        )
        for index, player in enumerate(players)
        if norm(getattr(player, "source_id", ""))
    }
    effects_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    schedule: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for effect in effects:
        provider = norm(effect.get("provider"))
        skill_key = canonical_skill(effect_skill_name(effect.get("skill")))
        if not provider or not skill_key:
            continue
        if callable(is_continuous_effect) and is_continuous_effect(effect):
            continue
        key = (provider, skill_key)
        effects_by_key[key].append(effect)
        schedule.setdefault(key, [])

    timings = observed_cast_timings or {}
    for event in event_platform_for(snapshot).skill_cast_events:
        source_id = norm(event.source_id)
        provider = provider_by_source.get(source_id, "")
        skill_key = canonical_skill(event.skill_name)
        key = (provider, skill_key)
        candidates = effects_by_key.get(key)
        if not candidates:
            continue
        start = max(0.0, event.timestamp_ms / 1000.0)
        cast_time = max(
            0.0,
            as_number(
                timings.get((source_id, event.timestamp_ms, skill_key), (0.0, 0.0))[0]
            ),
        )
        if cast_time <= 0:
            effect = candidates[0]
            if callable(effect_cast_time):
                cast_time = max(0.0, as_number(effect_cast_time(effect)))
            else:
                cast_time = max(0.0, as_number(effect.get("cast_time")))
        # A zero-duration action still needs a real interval so the practical
        # effect builder can start its duration at the captured timestamp.
        cast_time = cast_time if cast_time > 1e-6 else 0.001
        schedule[key].append((start, start + cast_time))

    for key, rows in schedule.items():
        unique_rows = sorted({(round(start, 6), round(end, 6)) for start, end in rows})
        schedule[key] = [(start, end) for start, end in unique_rows]
    return schedule

def _team_score_active_windows_to_cast_schedule(
    effects: list[dict[str, Any]],
    active_schedule: dict[tuple[str, str], list[tuple[float, float]]],
    *,
    effect_skill_name: Any = norm,
    effect_cast_time: Any = None,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Convert recorded Buff-active windows into timeline cast windows.

    Combat status packets describe when a Buff is active. The practical
    timeline API instead expects each tuple to be the start and end of the
    skill animation, then applies the effect duration after ``cast_end``.
    Passing active windows through unchanged doubles Spirit Boost, Scorching
    Flame and Time Acceleration: a 14-second active window becomes a 14-second
    cast followed by another 14-second effect.
    """

    effect_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for effect in effects:
        provider = norm(effect.get("provider"))
        skill_key = canonical_skill(effect_skill_name(effect.get("skill")))
        if provider and skill_key:
            effect_by_key.setdefault((provider, skill_key), effect)

    converted: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for (raw_provider, raw_skill), intervals in active_schedule.items():
        provider = norm(raw_provider)
        skill_key = canonical_skill(raw_skill)
        key = (provider, skill_key)
        effect = effect_by_key.get(key, {})
        cast_time = 0.5
        if callable(effect_cast_time) and effect:
            cast_time = max(0.0, as_number(effect_cast_time(effect), 0.5))
        elif effect:
            cast_time = max(0.0, as_number(effect.get("cast_time"), 0.5))
        if cast_time <= 0:
            cast_time = 0.5

        casts: list[tuple[float, float]] = []
        for active_start, active_end in intervals:
            if active_end <= active_start + 1e-9:
                continue
            cast_end = max(0.0, as_number(active_start))
            # A Buff that was already active when the selected Boss/output
            # window began is localized to active_start == 0. Keep its cast
            # as ending at the local origin instead of collapsing to (0, 0),
            # so pinned CDR and action speed both take effect from t=0.
            cast_start = (
                -cast_time
                if cast_end <= 1e-9
                else max(0.0, cast_end - cast_time)
            )
            casts.append((cast_start, cast_end))
        converted[key] = casts
    return converted

class TheoryPlanBuilderMixin:
    @staticmethod
    def _merge_team_score_intervals(
        intervals: list[tuple[float, float]],
    ) -> tuple[tuple[float, float], ...]:
        rows = sorted(
            (max(0.0, float(start)), max(float(start), float(end)))
            for start, end in intervals
            if float(end) > float(start) + 1e-9
        )
        merged: list[list[float]] = []
        for start, end in rows:
            if not merged or start > merged[-1][1] + 1e-6:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return tuple((start, end) for start, end in merged)

    @classmethod
    def _team_score_blocked_intervals(
        cls,
        output_windows: tuple[tuple[float, float], ...],
        window: float,
    ) -> list[tuple[float, float]]:
        active = cls._merge_team_score_intervals(
            [
                (max(0.0, start), min(window, end))
                for start, end in output_windows
                if end > 0 and start < window
            ]
        )
        blocked: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in active:
            if start > cursor + 1e-6:
                blocked.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < window - 1e-6:
            blocked.append((cursor, window))
        return blocked

    @classmethod
    def _team_score_localized_timing(
        cls,
        output_windows: tuple[tuple[float, float], ...],
        cast_schedule: dict[tuple[str, str], list[tuple[float, float]]],
    ) -> tuple[
        float,
        tuple[tuple[float, float], ...],
        dict[tuple[str, str], list[tuple[float, float]]],
        float,
    ]:
        active = cls._merge_team_score_intervals(list(output_windows))
        if not active:
            return 0.0, (), {}, 0.0
        origin = active[0][0]
        absolute_end = active[-1][1]
        local_windows = tuple(
            (max(0.0, start - origin), max(0.0, end - origin))
            for start, end in active
        )
        local_schedule: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for key, intervals in cast_schedule.items():
            clipped = cls._merge_team_score_intervals(
                [
                    (
                        max(origin, start) - origin,
                        min(absolute_end, end) - origin,
                    )
                    for start, end in intervals
                    if end > origin + 1e-9 and start < absolute_end - 1e-9
                ]
            )
            if clipped:
                local_schedule[key] = list(clipped)
        return origin, local_windows, local_schedule, max(0.0, absolute_end - origin)

    def _team_score_cdr_cast_schedule(
        self,
        snapshot: CombatSnapshot,
        players: tuple[object, ...],
        effects: list[dict[str, Any]],
        effect_skill_name: Any = norm,
        provider_jobs: tuple[str, ...] | None = None,
    ) -> dict[tuple[str, str], list[tuple[float, float]]]:
        return _team_score_recorded_cdr_cast_schedule(
            snapshot,
            players,
            effects,
            effect_skill_name=effect_skill_name,
            provider_jobs=provider_jobs,
        )

    def _team_score_real_job_name(self, job: Any) -> str:
        requested = norm(job)
        resolver = getattr(self.timeline_editor, "_team_real_job", None)
        if callable(resolver):
            return norm(resolver(requested)) or requested
        if requested in {"输出曜", "打坐曜"}:
            return "曜"
        return requested

    def _team_score_support_replan_metadata(
        self,
        *,
        editor: object,
        players: tuple[object, ...],
        jobs: tuple[str, ...],
        unique_jobs: list[str],
        effects: list[dict[str, Any]],
        cast_schedule: dict[tuple[str, str], list[tuple[float, float]]],
        output_windows: tuple[tuple[float, float], ...],
        window: float,
        base_specs_by_job: dict[str, dict[str, Any]],
        baseline_by_job: dict[str, dict[str, Any]],
        glyph_rows_by_job: dict[str, list[dict[str, Any]]],
        glyph_rows_by_source: dict[str, list[dict[str, Any]]] | None = None,
        run_parallel: Any,
        neutral_task_specs_out: list[dict[str, Any]] | None = None,
        ideal_task_specs_out: list[dict[str, Any]] | None = None,
        ideal_task_keys_out: list[str] | None = None,
        precomputed_neutral_by_job: dict[str, dict[str, Any]] | None = None,
        base_specs_by_source: dict[str, dict[str, Any]] | None = None,
        baseline_by_source: dict[str, dict[str, Any]] | None = None,
        precomputed_neutral_by_source: dict[str, dict[str, Any]] | None = None,
        precomputed_ideal_by_source: dict[str, dict[str, Any]] | None = None,
        recipient_damage_weights: dict[str, float] | None = None,
        recipient_damage_events: dict[
            str, tuple[tuple[float, float], ...]
        ] | None = None,
        actual_recipient_damage_weights: dict[str, float] | None = None,
        actual_recipient_damage_events: dict[
            str, tuple[tuple[float, float], ...]
        ] | None = None,
        timing_objective_name: str = "neutral_theory_damage_stream",
        precomputed_ideal_candidate_name: str = "neutral_theory_optimized",
        precomputed_timing_ablations: dict[tuple[str, str], dict[str, float]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Value timing Buffs with neutral, observed, and legal-ideal axes.

        The visible/output theory plan remains the observed recipient
        environment.  Support actual value compares that plan with the neutral
        timing axis; support theory value compares a shared legal-ideal timing
        schedule with the same neutral axis. When supplied, exact per-provider
        leave-one-out measurements replace the legacy throughput allocation.
        Marginal skill gains may overlap; joint A-N/T-N totals stay separate.
        """

        full_window = max(0.0, window)
        source_plan_mode = bool(base_specs_by_source)
        full_team_dps = (
            _team_score_team_dps_by_source(baseline_by_source or {}, players)
            if source_plan_mode
            else _team_score_team_dps(baseline_by_job, jobs)
        )
        if neutral_task_specs_out is None and full_team_dps <= 1e-9:
            return {}
        effect_skill_name = getattr(
            editor, "_team_effect_skill_name", lambda value: norm(value)
        )
        effect_type = getattr(editor, "_team_practical_effect_type", None)

        provider_rows: list[tuple[str, str, tuple[str, ...]]] = []
        expectations_by_unit: dict[
            tuple[str, str], tuple[object, ...]
        ] = {}
        for member_index, (player, job) in enumerate(zip(players, jobs)):
            source_id = norm(getattr(player, "source_id", ""))
            if not source_id or not job:
                continue
            provider = f"队员{member_index + 1} {job}"
            glyph_plan = {
                "_score_glyphs": (glyph_rows_by_source or {}).get(
                    source_id, glyph_rows_by_job.get(job, [])
                )
            }
            level_report = getattr(self, '_team_score_skill_levels_by_source', {}).get(source_id)
            if level_report:
                learned = {
                    **self.score_engine._learned_skills(data_job_for(job)),
                    **level_report['levels'],
                }
                base_expectations = self.score_engine._support_expectations(job, learned_override=learned)
            else:
                base_expectations = self.score_engine._support_expectations(job)
            expectations = self.score_engine._support_expectations_with_glyphs(
                base_expectations, glyph_plan
            )
            expected_groups = {
                row.skill_key
                for row in expectations
                if self.score_engine._support_effect_gain_rate(row) > 1e-12
            }
            groups = sorted({
                _team_score_support_effect_group(
                    effect_skill_name(effect.get("skill"))
                )
                for effect in effects
                if norm(effect.get("provider")) == provider
                and _team_score_support_effect_group(
                    effect_skill_name(effect.get("skill"))
                )
                in expected_groups
                and _team_score_effect_changes_rotation(effect)
            })
            if groups:
                provider_rows.append((source_id, provider, tuple(groups)))
                for group in groups:
                    expectations_by_unit[(provider, group)] = tuple(
                        row for row in expectations if row.skill_key == group
                    )
        if not provider_rows:
            return {}

        variants: list[tuple[str, str, str]] = [
            (source_id, provider, group)
            for source_id, provider, groups in provider_rows
            for group in groups
        ]
        all_providers = {
            norm(effect.get("provider"))
            for effect in effects
            if norm(effect.get("provider"))
        }
        timing_units = {
            (provider, group)
            for _source_id, provider, group in variants
        }
        recipient_source_ids = tuple(
            str(getattr(player, "source_id", ""))
            for player in players
            if str(getattr(player, "source_id", ""))
        )
        resolved_recipient_weights = {
            source_id: max(
                0.0,
                as_number((recipient_damage_weights or {}).get(source_id)),
            )
            for source_id in recipient_source_ids
        }
        if not any(value > 1e-9 for value in resolved_recipient_weights.values()):
            resolved_recipient_weights = {
                str(getattr(player, "source_id", "")): max(
                    1.0,
                    as_number(
                        (baseline_by_source or {}).get(
                            str(getattr(player, "source_id", "")), {}
                        ).get("dps"),
                        as_number(getattr(player, "damage", 0.0)),
                    ),
                )
                for player in players
                if str(getattr(player, "source_id", ""))
            }
        resolved_actual_recipient_weights = {
            source_id: max(
                0.0,
                as_number(
                    (actual_recipient_damage_weights or {}).get(source_id)
                ),
            )
            for source_id in recipient_source_ids
        }
        if not any(
            value > 1e-9
            for value in resolved_actual_recipient_weights.values()
        ):
            resolved_actual_recipient_weights = dict(
                resolved_recipient_weights
            )
        resolved_actual_recipient_events = (
            actual_recipient_damage_events
            if actual_recipient_damage_events is not None
            else recipient_damage_events
        ) or {}
        clear_times_by_source = {
            source_id: tuple(
                max(0.0, as_number(value))
                for value in (
                    (base_specs_by_source or {}).get(source_id, {}).get(
                        "player_buff_clear_times", ()
                    )
                )
            )
            for source_id in recipient_source_ids
        }
        ideal_schedule, ideal_coverage_by_unit = (
            _team_score_optimal_shared_timing_schedule(
                effects,
                timing_units,
                expectations_by_unit,
                output_windows,
                full_window,
                recipient_source_ids,
                clear_times_by_source,
                resolved_recipient_weights,
                effect_skill_name=effect_skill_name,
                effect_cast_time=getattr(
                    editor, "_team_practical_effect_cast_time", None
                ),
                actual_cast_schedule=cast_schedule,
                recipient_damage_events=recipient_damage_events,
            )
            if source_plan_mode and not (
                precomputed_timing_ablations
                and all(
                    precomputed_timing_ablations.get(unit, {}).get('method') == 'conditional_timing_replan'
                    for unit in timing_units
                )
            )
            else ({}, {})
        )

        def observed_unit_coverage(unit: tuple[str, str]) -> float:
            intervals_by_source: dict[str, list[tuple[float, float]]] = {}
            status_found = False
            for source_id in recipient_source_ids:
                recipient_intervals: list[tuple[float, float]] = []
                recipient_spec = (base_specs_by_source or {}).get(source_id, {})
                for recipient_effect in recipient_spec.get("effects", ()):
                    recipient_unit = (
                        norm(recipient_effect.get("provider")),
                        _team_score_support_effect_group(
                            effect_skill_name(recipient_effect.get("skill"))
                        ),
                    )
                    if recipient_unit != unit:
                        continue
                    if "_score_recipient_active_intervals" not in recipient_effect:
                        continue
                    status_found = True
                    recipient_intervals.extend(
                        recipient_effect.get("_score_recipient_active_intervals", ())
                    )
                intervals_by_source[source_id] = recipient_intervals
            if not status_found:
                return 0.0
            damage_coverage = _team_score_damage_value_coverage(
                intervals_by_source,
                resolved_actual_recipient_events,
                resolved_actual_recipient_weights,
            )
            if damage_coverage is not None:
                return damage_coverage
            return _team_score_weighted_interval_coverage(
                intervals_by_source,
                output_windows,
                resolved_actual_recipient_weights,
            )

        actual_coverage_by_unit = {
            unit: observed_unit_coverage(unit) for unit in timing_units
        }
        # Conditional scoring must also evaluate wrong timing with equal uptime.
        # The legacy audit path retains its historical half-point uptime gate.
        idealized_units = {
            unit
            for unit in timing_units
            if getattr(self, '_team_score_timing_ablation_mode', 'conditional') == 'conditional'
            or ideal_coverage_by_unit.get(unit, 0.0)
            > actual_coverage_by_unit.get(unit, 0.0) + 0.005
        }
        total_tasks = max(1, len(unique_jobs))
        last_progress = -1
        metadata: dict[str, dict[str, Any]] = {}

        def is_timing_attr(raw_key: Any) -> bool:
            key = norm(raw_key)
            return (
                "动作速度" in key
                or "攻速" in key
                or (
                    "冷却" in key
                    and any(marker in key for marker in ("缩减", "减少", "恢复"))
                )
            )

        def residual_non_timing_effect(
            effect: dict[str, Any],
        ) -> dict[str, Any] | None:
            """Keep panel/stat attrs while removing a timing buff's CDR work."""

            residual = copy.deepcopy(effect)
            attrs = {
                key: value
                for key, value in effect.get("attrs", {}).items()
                if not is_timing_attr(key)
            }
            effective_attrs = {
                key: value
                for key, value in effect.get("effective_attrs", {}).items()
                if not is_timing_attr(key)
            }
            if not attrs and not effective_attrs:
                return None
            residual["attrs"] = attrs
            residual["effective_attrs"] = effective_attrs or copy.deepcopy(attrs)
            skill = effect_skill_name(effect.get("skill"))
            # The neutral timing pass must retain Cocktail's main-stat panel
            # without re-enabling Cocktail merely because its skill name exists.
            residual["skill"] = f"团队buff｜{skill}（仅非减CD属性）"
            residual["_score_residual_for"] = skill
            return residual

        def standalone_inputs(
            included_unit: tuple[str, str] | None,
            source_effects: list[dict[str, Any]] | None = None,
            source_schedule: dict[
                tuple[str, str], list[tuple[float, float]]
            ] | None = None,
        ) -> tuple[
            list[dict[str, Any]],
            dict[tuple[str, str], list[tuple[float, float]]],
        ]:
            source_effects = effects if source_effects is None else source_effects
            source_schedule = cast_schedule if source_schedule is None else source_schedule
            selected_effects: list[dict[str, Any]] = []
            for effect in source_effects:
                unit = (
                    norm(effect.get("provider")),
                    _team_score_support_effect_group(
                        effect_skill_name(effect.get("skill"))
                    ),
                )
                if unit not in timing_units or unit == included_unit:
                    selected_effects.append(effect)
                    continue
                residual = residual_non_timing_effect(effect)
                if residual is not None:
                    selected_effects.append(residual)
            selected_providers = {
                norm(row.get("provider")) for row in selected_effects
            }
            for provider in sorted(all_providers - selected_providers):
                selected_effects.append(
                    {
                        "row": None,
                        "provider": provider,
                        "skill": "__反事实占位__",
                        "level": "",
                        "attrs": {},
                        "cooldown": 0.0,
                        "duration": 0.0,
                        "coverage": 0.0,
                        "effective_attrs": {},
                    }
                )
            selected_schedule = {
                key: list(intervals)
                for key, intervals in source_schedule.items()
                if (unit := (
                    norm(key[0]),
                    _team_score_support_effect_group(
                        effect_skill_name(key[1])
                    ),
                )) not in timing_units
                or unit == included_unit
            }
            return selected_effects, selected_schedule

        task_specs: list[dict[str, Any]] = []
        neutral_task_keys: list[str] = []
        if source_plan_mode:
            for player in players:
                source_id = str(getattr(player, "source_id", ""))
                source_spec = (base_specs_by_source or {}).get(source_id)
                if not source_id or not isinstance(source_spec, dict):
                    continue
                neutral_effects, neutral_schedule = standalone_inputs(
                    None,
                    source_spec.get("effects", effects),
                    source_spec.get("cast_schedule", cast_schedule),
                )
                spec = dict(source_spec)
                spec["effects"] = neutral_effects
                spec["cast_schedule"] = neutral_schedule
                spec["window"] = window
                spec["include_display_windows"] = False
                spec["score_refinement"] = True
                task_specs.append(spec)
                neutral_task_keys.append(source_id)
        else:
            neutral_effects, neutral_schedule = standalone_inputs(None)
            for job in unique_jobs:
                spec = dict(base_specs_by_job[job])
                spec["effects"] = neutral_effects
                spec["cast_schedule"] = neutral_schedule
                spec["window"] = window
                spec["include_display_windows"] = False
                spec["score_refinement"] = True
                task_specs.append(spec)
                neutral_task_keys.append(job)

        ideal_specs: list[dict[str, Any]] = []
        ideal_task_keys: list[str] = []
        if source_plan_mode and idealized_units:
            for player in players:
                source_id = str(getattr(player, "source_id", ""))
                source_spec = (base_specs_by_source or {}).get(source_id)
                if not source_id or not isinstance(source_spec, dict):
                    continue
                ideal_effects: list[dict[str, Any]] = []
                for effect in source_spec.get("effects", effects):
                    copied = copy.deepcopy(effect)
                    unit = (
                        norm(copied.get("provider")),
                        _team_score_support_effect_group(
                            effect_skill_name(copied.get("skill"))
                        ),
                    )
                    if unit in idealized_units:
                        copied.pop("_score_recipient_active_intervals", None)
                    ideal_effects.append(copied)
                ideal_casts = {
                    key: copy.deepcopy(rows)
                    for key, rows in source_spec.get(
                        "cast_schedule", cast_schedule
                    ).items()
                    if (
                        norm(key[0]),
                        _team_score_support_effect_group(
                            effect_skill_name(key[1])
                        ),
                    ) not in idealized_units
                }
                ideal_casts.update({
                    key: copy.deepcopy(rows)
                    for key, rows in ideal_schedule.items()
                    if (
                        norm(key[0]),
                        _team_score_support_effect_group(
                            effect_skill_name(key[1])
                        ),
                    ) in idealized_units
                })
                spec = dict(source_spec)
                spec["effects"] = ideal_effects
                spec["cast_schedule"] = ideal_casts
                spec["window"] = window
                spec["include_display_windows"] = False
                spec["score_refinement"] = True
                # This pass is a support-value ceiling only.  Its result is
                # never exposed as the player's output theory timeline.
                spec["support_only_ideal_timing"] = True
                ideal_specs.append(spec)
                ideal_task_keys.append(source_id)

        if neutral_task_specs_out is not None:
            neutral_task_specs_out.extend(task_specs)
            if ideal_task_specs_out is not None:
                ideal_task_specs_out.extend(ideal_specs)
            if ideal_task_keys_out is not None:
                ideal_task_keys_out.extend(ideal_task_keys)
            return {}

        def progress(completed: int, _index: int, job: str) -> None:
            nonlocal last_progress
            value = TEAM_SCORE_PROGRESS_BASELINE_END + int(
                (TEAM_SCORE_PROGRESS_TIMING_END - TEAM_SCORE_PROGRESS_BASELINE_END)
                * completed
                / total_tasks
            )
            if value == last_progress and completed != len(task_specs):
                return
            last_progress = value
            self._advance_view_progress(
                "score",
                value,
                f"正在移除减CD效果并重排理论轴 {completed}/{total_tasks}（{job}）...",
            )

        if precomputed_neutral_by_source is not None and source_plan_mode:
            neutral_by_source = {
                source_id: result
                for source_id, result in precomputed_neutral_by_source.items()
                if source_id in neutral_task_keys and isinstance(result, dict)
            }
            neutral_team_dps = _team_score_team_dps_by_source(
                neutral_by_source, players
            )
        elif precomputed_neutral_by_job is None:
            results = run_parallel(task_specs, progress_callback=progress)
            if len(results) != len(task_specs):
                return {}
            if source_plan_mode:
                neutral_by_source = {
                    source_id: result
                    for source_id, result in zip(neutral_task_keys, results)
                    if isinstance(result, dict)
                }
                neutral_team_dps = _team_score_team_dps_by_source(
                    neutral_by_source, players
                )
            else:
                neutral_by_job = {
                    job: result
                    for job, result in zip(unique_jobs, results[:len(unique_jobs)])
                    if isinstance(result, dict)
                }
                neutral_team_dps = _team_score_team_dps(neutral_by_job, jobs)
        else:
            neutral_by_job = {
                job: result
                for job, result in precomputed_neutral_by_job.items()
                if job in unique_jobs and isinstance(result, dict)
            }
            neutral_team_dps = _team_score_team_dps(neutral_by_job, jobs)
        if neutral_team_dps <= 1e-9:
            return {}
        actual_joint_gain = _team_score_standalone_gain_percent(
            neutral_team_dps, full_team_dps
        )
        optimized_candidate_team_dps = full_team_dps
        if source_plan_mode and precomputed_ideal_by_source:
            ideal_by_source = {
                source_id: result
                for source_id, result in precomputed_ideal_by_source.items()
                if source_id in recipient_source_ids and isinstance(result, dict)
            }
            if len(ideal_by_source) == len(recipient_source_ids):
                optimized_candidate_team_dps = (
                    _team_score_team_dps_by_source(ideal_by_source, players)
                )
        ideal_team_dps = max(full_team_dps, optimized_candidate_team_dps)
        selected_legal_candidate = (
            norm(precomputed_ideal_candidate_name)
            or "neutral_theory_optimized"
            if optimized_candidate_team_dps > full_team_dps + 1e-9
            else "recorded_actual"
        )
        # ``ideal_team_dps`` already selects the better exact full-axis result
        # between the complete recorded scheme and the optimized legal
        # candidate.  Do not apply a second numeric max to the gains here: the
        # legal ceiling must come from selecting a real schedule, not from
        # clipping a bad denominator after the fact.
        theory_joint_gain = _team_score_standalone_gain_percent(
            neutral_team_dps, ideal_team_dps
        )
        if theory_joint_gain <= 1e-9 and not precomputed_timing_ablations:
            return {}

        effects_by_unit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for effect in effects:
            unit = (
                norm(effect.get("provider")),
                _team_score_support_effect_group(
                    effect_skill_name(effect.get("skill"))
                ),
            )
            if unit in timing_units:
                effects_by_unit[unit].append(effect)

        preserve_base_groups = {
            unit
            for unit, effect_rows in effects_by_unit.items()
            if any(
                any(
                    not is_timing_attr(key) and abs(as_number(value)) > 1e-9
                    for key, value in effect.get("attrs", {}).items()
                )
                for effect in effect_rows
            )
        }

        actual_unit_weights: dict[tuple[str, str], float] = {}
        theory_unit_weights: dict[tuple[str, str], float] = {}
        for unit in timing_units:
            expectation_rows = expectations_by_unit.get(unit, ())
            coverage = max(
                (
                    _coverage_in_windows(
                        self.score_engine._theory_support_intervals(
                            row,
                            full_window,
                            output_windows,
                        ),
                        output_windows,
                    )
                    for row in expectation_rows
                ),
                default=0.0,
            )
            if source_plan_mode:
                coverage = actual_coverage_by_unit.get(unit, coverage)
            theory_coverage = (
                ideal_coverage_by_unit.get(unit, coverage)
                if unit in idealized_units
                and precomputed_ideal_by_source
                else coverage
            )
            effect_rows = effects_by_unit.get(unit, ())
            cdr = min(
                70.0,
                max(
                    (
                        max(0.0, as_number(effect.get("attrs", {}).get("冷却缩减%")))
                        for effect in effect_rows
                    ),
                    default=0.0,
                ),
            )
            action_speed = max(
                (
                    max(
                        0.0,
                        as_number(effect.get("attrs", {}).get("动作速度%")),
                        as_number(effect.get("attrs", {}).get("攻速%")),
                    )
                    for effect in effect_rows
                ),
                default=0.0,
            )
            cooldown_throughput = cdr / max(1.0, 100.0 - cdr)
            action_throughput = action_speed / 100.0
            # The joint full-vs-neutral simulation is the hard total cap. Adding
            # the two channels here changes only how that measured total is
            # attributed; it does not add a second damage multiplier.
            throughput = cooldown_throughput + action_throughput
            actual_weight = coverage * throughput
            theory_weight = theory_coverage * throughput
            actual_unit_weights[unit] = actual_weight
            theory_unit_weights[unit] = theory_weight

        actual_weight_total = sum(actual_unit_weights.values())
        theory_weight_total = sum(theory_unit_weights.values())
        if theory_weight_total <= 1e-9 and not precomputed_timing_ablations:
            return {}
        if actual_joint_gain > 1e-9 and actual_weight_total <= 1e-9:
            # Do not discard a measured actual timing gain merely because a
            # capture lacks recipient-status detail.  The legal opportunity is
            # the least-assumptive fallback allocation in that case.
            actual_unit_weights = dict(theory_unit_weights)
            actual_weight_total = theory_weight_total
        actual_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        theory_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        actual_linked_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        actual_total_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        theory_linked_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        theory_total_gains_by_source: dict[str, dict[str, float]] = defaultdict(dict)
        # Decompose the exact joint result monotonically.  The complete
        # recorded schedule is a legal candidate, so the legal plan consists
        # of the measured actual gain plus a non-negative improvement.  The
        # old implementation normalized actual and legal weights separately;
        # that could assign one skill 117% completion even while the legal
        # joint plan beat the actual joint plan.  Starting both decompositions
        # from the same actual baseline makes every per-skill denominator a
        # real share of an attainable legal candidate instead of a post-hoc
        # cap.
        actual_allocations: dict[tuple[str, str], float] = {}
        for unit in timing_units:
            actual_allocations[unit] = (
                actual_joint_gain
                * actual_unit_weights.get(unit, 0.0)
                / actual_weight_total
                if actual_weight_total > 1e-9
                else 0.0
            )

        legal_improvement = max(0.0, theory_joint_gain - actual_joint_gain)
        improvement_weights = {
            unit: max(
                0.0,
                theory_unit_weights.get(unit, 0.0)
                - actual_unit_weights.get(unit, 0.0),
            )
            for unit in timing_units
        }
        improvement_weight_total = sum(improvement_weights.values())
        if legal_improvement > 1e-9 and improvement_weight_total <= 1e-9:
            # Exact replanning can expose a nonlinear rotation improvement
            # even when the lightweight timestamp objective is tied.  Retain
            # that measured improvement and distribute it by legal opportunity
            # rather than losing it or fabricating an actual-over-legal row.
            improvement_weights = dict(theory_unit_weights)
            improvement_weight_total = sum(improvement_weights.values())

        for source_id, provider, group in variants:
            unit = (provider, group)
            actual_allocated = actual_allocations.get(unit, 0.0)
            improvement_allocated = (
                legal_improvement
                * improvement_weights.get(unit, 0.0)
                / improvement_weight_total
                if improvement_weight_total > 1e-9
                else 0.0
            )
            theory_allocated = actual_allocated + improvement_allocated
            actual_gains_by_source[source_id][group] = actual_allocated
            theory_gains_by_source[source_id][group] = theory_allocated

        # Measured leave-one-out deltas supersede coverage/throughput shares.
        # The supplied common DPS is only a percentage scale (A in conditional
        # mode, N in the historical audit path). Each conditional numerator
        # subtracts the same B_i. Never redistribute the measured gains.
        for source_id, provider, group in variants:
            measurement = (precomputed_timing_ablations or {}).get((provider, group))
            if measurement is None:
                continue
            actual_gains_by_source[source_id][group] = (
                max(0.0, measurement["actual_marginal_dps"]) / neutral_team_dps * 100.0
            )
            theory_gains_by_source[source_id][group] = (
                max(0.0, measurement["theory_marginal_dps"]) / neutral_team_dps * 100.0
            )
            if measurement.get("linked_diagnostics_complete", True) is False:
                # The formal score is allowed to finish after the B/H removal
                # and retimed-provider axes. Do not synthesize a zero linked
                # result while the L/HL diagnostics are still queued: the
                # detail layer uses map presence to decide whether the split is
                # available.
                continue
            actual_linked_gains_by_source[source_id][group] = (
                as_number(measurement.get("actual_linked_marginal_dps"))
                / neutral_team_dps * 100.0
            )
            actual_total_gains_by_source[source_id][group] = (
                as_number(
                    measurement.get(
                        "actual_total_marginal_dps",
                        measurement.get("actual_marginal_dps"),
                    )
                )
                / neutral_team_dps * 100.0
            )
            theory_linked_gains_by_source[source_id][group] = (
                as_number(measurement.get("theory_linked_marginal_dps"))
                / neutral_team_dps * 100.0
            )
            theory_total_gains_by_source[source_id][group] = (
                as_number(
                    measurement.get(
                        "theory_total_marginal_dps",
                        measurement.get("theory_marginal_dps"),
                    )
                )
                / neutral_team_dps * 100.0
            )

        for source_id, _provider, _groups in provider_rows:
            actual_skill_gains = actual_gains_by_source.get(source_id, {})
            theory_skill_gains = theory_gains_by_source.get(source_id, {})
            if not theory_skill_gains:
                continue
            metadata[source_id] = {
                "method": "shared_timing_gain_allocation",
                # Damage-only effects deliberately keep the fixed-axis
                # counterfactual: stable team rotations do not reorder for those
                # multipliers, so replanning them would repeat the same sequence.
                # Only timing effects replace their old estimated contribution.
                "replace_only": True,
                "baseline_team_dps": full_team_dps,
                "ideal_team_dps": ideal_team_dps,
                "optimized_candidate_team_dps": optimized_candidate_team_dps,
                "neutral_team_dps": neutral_team_dps,
                "legal_timing_objective": norm(timing_objective_name)
                or "neutral_theory_damage_stream",
                "selected_legal_candidate": selected_legal_candidate,
                "actual_joint_timing_gain_percent": actual_joint_gain,
                "theory_joint_timing_gain_percent": theory_joint_gain,
                "joint_timing_gain_percent": theory_joint_gain,
                "counterfactual_window": window,
                "full_window": full_window,
                "source_gain_percent": sum(theory_skill_gains.values()),
                "actual_source_gain_percent": sum(actual_skill_gains.values()),
                "theory_source_gain_percent": sum(theory_skill_gains.values()),
                # Compatibility names are retained for the score-detail layer.
                "source_loss_percent": sum(theory_skill_gains.values()),
                "skill_loss_percent": theory_skill_gains,
                "raw_skill_loss_percent": theory_skill_gains,
                "skill_gain_percent": theory_skill_gains,
                "actual_skill_gain_percent": actual_skill_gains,
                "theory_skill_gain_percent": theory_skill_gains,
                # Diagnostic-only decomposition.  The official score continues
                # to use the direct maps above until cross-capture validation is
                # accepted; downstream linked coverage is never double-counted.
                "actual_direct_skill_gain_percent": actual_skill_gains,
                "actual_linked_skill_gain_percent": actual_linked_gains_by_source.get(source_id, {}),
                "actual_total_skill_gain_percent": actual_total_gains_by_source.get(source_id, actual_skill_gains),
                "theory_direct_skill_gain_percent": theory_skill_gains,
                "theory_linked_skill_gain_percent": theory_linked_gains_by_source.get(source_id, {}),
                "theory_total_skill_gain_percent": theory_total_gains_by_source.get(source_id, theory_skill_gains),
                "linked_contribution_included_in_score": False,
                "legal_improvement_percent": legal_improvement,
                "legal_improvement_allocation_weight": {
                    group: improvement_weights.get((_provider, group), 0.0)
                    for group in _groups
                },
                "skill_allocation_weight": {
                    group: theory_unit_weights.get((_provider, group), 0.0)
                    for group in _groups
                },
                "actual_skill_allocation_weight": {
                    group: actual_unit_weights.get((_provider, group), 0.0)
                    for group in _groups
                },
                "allocation_invariant": (
                    "legal_gain=actual_gain+nonnegative_joint_improvement"
                ),
                "actual_weighted_coverage": {
                    group: actual_coverage_by_unit.get((_provider, group), 0.0)
                    for group in _groups
                },
                "theory_weighted_coverage": {
                    group: (
                        ideal_coverage_by_unit.get(
                            (_provider, group),
                            actual_coverage_by_unit.get((_provider, group), 0.0),
                        )
                        if (_provider, group) in idealized_units
                        and precomputed_ideal_by_source
                        else actual_coverage_by_unit.get((_provider, group), 0.0)
                    )
                    for group in _groups
                },
                # Cocktail and any future mixed panel/timing buff retain their
                # fixed-axis panel contribution; the exact replacement below
                # adds only the independently simulated timing gain.
                "preserve_base_contribution_skills": [
                    group
                    for group in _groups
                    if (_provider, group) in preserve_base_groups
                ],
            }
            measured = {
                group: (precomputed_timing_ablations or {})[(_provider, group)]
                for group in _groups
                if (_provider, group) in (precomputed_timing_ablations or {})
            }
            if measured:
                conditional_measurement = all(
                    row.get('method') == 'conditional_timing_replan' for row in measured.values()
                )
                metadata[source_id].update(
                    method='conditional_timing_replan' if conditional_measurement else "leave_one_out_timing_replan",
                    timing_ablation=copy.deepcopy(measured),
                    allocation_invariant="non_additive_leave_one_out_marginals",
                    counterfactual_schedule_policy=(
                        "preserve_other_support_cast_intent_delay_cooldown_illegal_casts"
                        if conditional_measurement
                        else "hold_other_provider_casts_fixed_replan_recipient_rotations"
                    ),
                )
                if conditional_measurement:
                    metadata[source_id]['allocation_invariant'] = 'same_absent_buff_baseline_per_provider'
                    metadata[source_id]['theory_scope'] = 'best_evaluated_conditional_candidate_not_global_optimum'
                    metadata[source_id]['gain_reference'] = 'observed_team_dps'
                    metadata[source_id]['reference_team_dps'] = full_team_dps
                    metadata[source_id]['legal_timing_objective'] = 'own_absent_buff_damage_stream'
                    for key in ('neutral_team_dps', 'ideal_team_dps', 'optimized_candidate_team_dps',
                                'actual_joint_timing_gain_percent', 'theory_joint_timing_gain_percent',
                                'joint_timing_gain_percent', 'legal_improvement_percent', 'selected_legal_candidate',
                                'theory_weighted_coverage'):
                        metadata[source_id].pop(key, None)
                # These legacy diagnostics describe the superseded heuristic.
                for key in ("legal_improvement_allocation_weight", "skill_allocation_weight", "actual_skill_allocation_weight"):
                    metadata[source_id].pop(key, None)
        return metadata

    @stable_score_worker_budget
    def _build_team_score_theory_plans(
        self,
        snapshot: CombatSnapshot,
        boss_target_ids: frozenset[str] | None = None,
        boss_preset_name: str = _TEAM_SCORE_DEFAULT_BOSS_PRESET,
    ) -> dict[str, dict[str, Any]]:
        self._team_score_theory_error = ""
        # A prefetched exclusion axis belongs only to the exact baseline plan
        # being built in this call.  Never let a previous replay/Boss scope
        # leak into a later score request.
        self._team_score_precomputed_exclusion_plans = {}
        editor = self.timeline_editor
        if editor is None:
            return {}
        boss_preset_name = norm(boss_preset_name)
        if boss_preset_name not in BOSS_PRESETS:
            boss_preset_name = _TEAM_SCORE_DEFAULT_BOSS_PRESET
        boss_preset = BOSS_PRESETS[boss_preset_name]
        build_effects = getattr(editor, "_extreme_team_effects_for_jobs", None)
        run_parallel = getattr(editor, "_extreme_timeline_tasks_parallel", None)
        if not callable(build_effects) or not callable(run_parallel):
            return {}

        platform = event_platform_for(snapshot)
        players = tuple(platform.players[:8])
        raw_jobs = tuple(norm(player.job) for player in players)
        if not players or not any(raw_jobs):
            return {}
        output_windows = self.score_engine._output_windows(
            snapshot, boss_target_ids
        )
        jobs = tuple(
            yao_rotation_variant(
                raw_job,
                self.score_engine._yao_meditation_coverage(
                    snapshot,
                    player.source_id,
                    output_windows,
                    boss_target_ids,
                ),
            )
            for player, raw_job in zip(players, raw_jobs)
        )
        team_jobs = list(jobs)
        output_alchemist_providers = {
            f"队员{index + 1} {job}"
            for index, (player, job) in enumerate(zip(players, jobs))
            if data_job_for(job) == "药剂师"
            and not self.score_engine._alchemist_used_sacrifice(
                snapshot, player.source_id
            )
        }
        skill_levels_by_source = infer_combat_skill_levels(snapshot, self.store)
        self._team_score_skill_levels_by_source = skill_levels_by_source
        levels_by_provider = {
            f'队员{index + 1} {job}': skill_levels_by_source.get(str(player.source_id), {}).get('levels', {})
            for index, (player, job) in enumerate(zip(players, jobs))
        }
        if isinstance(getattr(editor, 'build', None), dict):
            editor.build['_score_combat_levels_by_provider'] = levels_by_provider
        effects = _team_score_effects_with_required_cdr_glyphs(
            editor,
            _team_score_effects_for_alchemist_roles(
                build_effects(team_jobs), output_alchemist_providers
            ),
        )
        effect_skill_name = getattr(
            editor, "_team_effect_skill_name", lambda value: norm(value)
        )
        effect_type = getattr(editor, "_team_practical_effect_type", None)
        observed_cast_timings = self.score_engine._observed_cast_timings(snapshot)
        castability_by_source = self.score_engine.castability_by_source(snapshot)
        absolute_cdr_active_schedule = self._team_score_cdr_cast_schedule(
            snapshot,
            players,
            effects,
            effect_skill_name,
            provider_jobs=jobs,
        )
        absolute_effect_cast_schedule = _team_score_recorded_effect_cast_schedule(
            snapshot,
            players,
            effects,
            observed_cast_timings=observed_cast_timings,
            effect_skill_name=effect_skill_name,
            effect_cast_time=getattr(
                editor, "_team_practical_effect_cast_time", None
            ),
            is_continuous_effect=getattr(
                editor, "_team_practical_is_continuous_effect", None
            ),
            provider_jobs=jobs,
        )
        (
            score_time_offset,
            local_output_windows,
            actual_effect_cast_schedule,
            window,
        ) = self._team_score_localized_timing(
            output_windows,
            absolute_effect_cast_schedule,
        )
        localized_effect_cast_schedule = copy.deepcopy(
            actual_effect_cast_schedule
        )
        if window <= 0:
            return {}
        clear_times_by_target = player_buff_clear_times_by_target(
            platform.player_buff_clear_events,
            origin_seconds=score_time_offset,
            end_seconds=window,
        )
        player_buff_clear_times = merged_player_buff_clear_times(
            clear_times_by_target,
            (player.source_id for player in players),
        )
        hp_groups = tuple(
            group
            for group in _boss_statistics_groups(snapshot)
            if not boss_target_ids
            or bool(set(group.target_ids) & set(boss_target_ids))
        )
        _fixed_profile_label, fixed_hp_references = resolve_fixed_boss_hp_profile(
            (group.name for group in hp_groups),
            capture_hint=self.collector.status,
            target_ids=(
                target_id
                for group in hp_groups
                for target_id in group.target_ids
            ),
        )
        compressed_hp_rows = _compress_boss_hp_tracks_to_windows(
            platform.boss_hp_tracks,
            hp_groups,
            ((score_time_offset, score_time_offset + window),),
            infer_from_damage=True,
            completed_group_identities=_completed_boss_group_identities(
                hp_groups,
                platform.boss_hp_tracks,
                terminal_skill_casts=platform.skill_cast_events,
                fixed_boss_hp_references=fixed_hp_references,
            ),
            fixed_boss_hp_references=fixed_hp_references,
        )
        boss_hp_segments = compact_boss_hp_segments(tuple(
            tuple(
                (max(0.0, as_number(second)), max(0.0, min(100.0, as_number(hp))))
                for second, hp in segment
            )
            for _boss_name, _confidence, segments in compressed_hp_rows
            for segment in segments
            if segment
        ))
        (
            _cdr_offset,
            _cdr_output_windows,
            actual_cast_schedule,
            _cdr_window,
        ) = self._team_score_localized_timing(
            output_windows,
            absolute_cdr_active_schedule,
        )
        recorded_cdr_cast_schedule = _team_score_active_windows_to_cast_schedule(
            effects,
            actual_cast_schedule,
            effect_skill_name=effect_skill_name,
            effect_cast_time=getattr(
                editor, "_team_practical_effect_cast_time", None
            ),
        )
        actual_effect_cast_schedule.update(recorded_cdr_cast_schedule)
        cast_schedule = _team_score_recorded_rotation_schedule(
            effects,
            actual_effect_cast_schedule,
            effect_skill_name=effect_skill_name,
            pin_all_effects=True,
            is_continuous_effect=getattr(
                editor, "_team_practical_is_continuous_effect", None
            ),
        )
        recipient_effects_by_source: dict[str, list[dict[str, Any]]] = {}
        recipient_cast_schedule_by_source: dict[
            str, dict[tuple[str, str], list[tuple[float, float]]]
        ] = {}
        recipient_active_schedule_by_source: dict[
            str, dict[tuple[str, str], list[tuple[float, float]]]
        ] = {}
        provider_by_source = {
            str(player.source_id): f"队员{index + 1} {jobs[index]}"
            for index, player in enumerate(players)
        }
        cdr_skill_keys = {
            canonical_skill("精神爆发"),
            canonical_skill("灼热火焰"),
            canonical_skill("时间加速"),
        }
        for player in players:
            source_id = str(player.source_id)
            absolute_recipient_buffs = _team_score_recipient_buff_active_schedule(
                snapshot,
                players,
                effects,
                source_id,
                effect_skill_name=effect_skill_name,
                effect_type=effect_type,
                provider_jobs=jobs,
            )
            (
                _recipient_offset,
                _recipient_windows,
                local_recipient_buffs,
                _recipient_window,
            ) = self._team_score_localized_timing(
                output_windows,
                absolute_recipient_buffs,
            )
            recipient_active_schedule_by_source[source_id] = (
                local_recipient_buffs
            )
            recipient_effects = _team_score_effects_for_recipient(
                effects,
                local_recipient_buffs,
                effect_skill_name=effect_skill_name,
                effect_type=effect_type,
            )
            recipient_effects_by_source[source_id] = recipient_effects
            recipient_cdr_active = {
                key: rows
                for key, rows in local_recipient_buffs.items()
                if canonical_skill(key[1]) in cdr_skill_keys
            }
            recipient_cdr_casts = _team_score_active_windows_to_cast_schedule(
                recipient_effects,
                recipient_cdr_active,
                effect_skill_name=effect_skill_name,
                effect_cast_time=getattr(
                    editor, "_team_practical_effect_cast_time", None
                ),
            )
            recipient_effect_casts = {
                key: copy.deepcopy(rows)
                for key, rows in localized_effect_cast_schedule.items()
                if canonical_skill(key[1]) not in cdr_skill_keys
            }
            recipient_effect_casts.update(recipient_cdr_casts)
            recipient_cast_schedule_by_source[source_id] = (
                _team_score_recorded_rotation_schedule(
                    recipient_effects,
                    recipient_effect_casts,
                    effect_skill_name=effect_skill_name,
                    pin_all_effects=True,
                    is_continuous_effect=getattr(
                        editor, "_team_practical_is_continuous_effect", None
                    ),
                )
            )
        player_job_by_source = {
            str(player.source_id): job for player, job in zip(players, jobs)
        }
        absolute_end = score_time_offset + window
        summon_removal_schedule_by_source = (
            _team_score_summon_removal_schedules_by_source(
                platform.summon_removal_events,
                player_job_by_source,
                score_time_offset=score_time_offset,
                absolute_end=absolute_end,
            )
        )
        score_actual_casts_by_job: dict[
            str, dict[str, list[dict[str, Any]]]
        ] = defaultdict(lambda: defaultdict(list))
        carry_in_casts: dict[tuple[str, str], dict[str, Any]] = {}
        recorded_avatar_windows = _team_score_recorded_avatar_cast_windows(
            platform.player_buff_series, platform.skill_cast_events
        )
        for event in platform.skill_cast_events:
            source_id = str(event.source_id)
            job = player_job_by_source.get(source_id, "")
            seconds = event.timestamp_ms / 1000.0
            if not job or seconds >= absolute_end:
                continue
            skill_key = canonical_skill(event.skill_name)
            recorded_window = recorded_avatar_windows.get(
                (source_id, int(event.timestamp_ms), skill_key)
            )
            recorded_buff_fields = (
                {
                    "_score_buff_start": recorded_window[0] - score_time_offset,
                    "_score_buff_end": recorded_window[1] - score_time_offset,
                }
                if recorded_window is not None else {}
            )
            cast_time, action_speed = observed_cast_timings.get(
                (source_id, event.timestamp_ms, skill_key),
                (0.0, 0.0),
            )
            if seconds < score_time_offset - 1e-6:
                # Keep only the latest pre-window cast for each skill. The
                # replay engine will retain it only when it is a support
                # action whose effect is still active at the local origin.
                carry_in_casts[(source_id, skill_key)] = {
                    **recorded_buff_fields,
                    "skill": event.skill_name,
                    "start": 0.0,
                    "end": 0.0,
                    "action_speed_percent": max(0.0, action_speed),
                    "_score_carry_in_local_start": seconds - score_time_offset,
                    "_score_carry_in_local_end": (
                        seconds + max(0.0, cast_time) - score_time_offset
                    ),
                }
                continue
            local_start = max(0.0, seconds - score_time_offset)
            score_actual_casts_by_job[job][source_id].append({
                **recorded_buff_fields,
                "skill": event.skill_name,
                "start": local_start,
                "end": min(window, local_start + max(0.0, cast_time)),
                "action_speed_percent": max(0.0, action_speed),
            })
        for (source_id, _skill_key), row in carry_in_casts.items():
            job = player_job_by_source.get(source_id, "")
            if job:
                score_actual_casts_by_job[job][source_id].append(row)
        score_actual_casts_by_job = {
            job: {
                source_id: sorted(
                    rows,
                    key=lambda row: (
                        as_number(row.get("start")), canonical_skill(row.get("skill"))
                    ),
                )
                for source_id, rows in by_source.items()
            }
            for job, by_source in score_actual_casts_by_job.items()
        }
        profile_for_job = getattr(editor, "_extreme_profile_for_job", None)
        full_team_objective_profiles = _team_score_joint_objective_profiles(
            players,
            jobs,
            tuple(platform.combat_events),
            boss_target_ids,
            output_windows,
            profile_for_job,
            boss_preset,
        )
        glyph_damage_context = captured_glyph_damage_context(
            snapshot, players, editor.store, output_windows, boss_target_ids)
        recipient_damage_weights = _team_score_recipient_damage_weights(
            players,
            tuple(platform.combat_events),
            boss_target_ids,
            output_windows,
        )
        recipient_damage_events = _team_score_recipient_damage_events(
            players,
            tuple(platform.combat_events),
            boss_target_ids,
            output_windows,
            origin_seconds=score_time_offset,
        )
        team_objective_profiles = _team_score_scale_objective_profiles(
            full_team_objective_profiles,
            _TEAM_SCORE_OUTPUT_TEAM_OBJECTIVE_WEIGHT,
        )
        glyph_profiles_by_source = {
            str(player.source_id): glyph_profile_for_source(
                glyph_damage_context, player.source_id,
                team_objective_profiles.get(job, {}))
            for player, job in zip(players, jobs)
        }
        glyph_override_signature = tuple(
            sorted(
                (
                    job,
                    tuple(_team_score_glyph_row_key(row) for row in rows),
                )
                for job, rows in self._team_score_glyph_overrides_by_job.items()
            )
        )
        glyph_inference_signature = tuple(
            (
                source_id,
                tuple(
                    _team_score_glyph_row_key(row)
                    for row in rows
                    if isinstance(row, dict)
                ),
            )
            for source_id, rows in sorted(
                getattr(
                    self,
                    "_team_score_glyph_inference_by_source",
                    {},
                ).items()
            )
        )
        scope = tuple(sorted(boss_target_ids or ()))
        signature = (
            "team-score-long-sim-planner-v24-stored-charge-grants",
            ("bounded_mechanism_v1", bool(getattr(self, "_team_score_bounded_search", True))),
            _TEAM_SCORE_OUTPUT_TEAM_OBJECTIVE_WEIGHT,
            jobs,
            scope,
            boss_preset_name,
            glyph_override_signature,
            glyph_inference_signature,
            equipment_signature(getattr(self, "_team_score_equipment_overrides", {})),
            level_signature(skill_levels_by_source),
            tuple(
                (key, as_number(boss_preset.get(key)))
                for key in (
                    "crit_resist",
                    "element_resist",
                    "monster_defense",
                    "freeze_coefficient",
                )
            ),
            round(window, 2),
            round(score_time_offset, 2),
            tuple(round(seconds, 3) for seconds in player_buff_clear_times),
            tuple(
                (
                    source_id,
                    tuple(
                        (round(seconds, 3), skill_key)
                        for seconds, skill_key in rows
                    ),
                )
                for source_id, rows in sorted(
                    summon_removal_schedule_by_source.items()
                )
            ),
            tuple(
                (round(start, 2), round(end, 2))
                for start, end in local_output_windows
            ),
            tuple(
                tuple((round(second, 2), round(hp, 3)) for second, hp in segment)
                for segment in boss_hp_segments
            ),
            tuple(
                sorted(
                    (
                        provider,
                        skill,
                        tuple((round(start, 2), round(end, 2)) for start, end in casts),
                    )
                    for (provider, skill), casts in cast_schedule.items()
                )
            ),
            tuple(
                sorted(
                    (
                        provider,
                        skill,
                        tuple((round(start, 2), round(end, 2)) for start, end in casts),
                    )
                    for (provider, skill), casts in actual_cast_schedule.items()
                )
            ),
            tuple(
                (
                    source_id,
                    tuple(
                        round(seconds, 3)
                        for seconds in clear_times_by_target.get(source_id, ())
                    ),
                    tuple(
                        sorted(
                            (
                                provider,
                                skill,
                                tuple(
                                    (round(start, 2), round(end, 2))
                                    for start, end in rows
                                ),
                            )
                            for (provider, skill), rows in (
                                recipient_active_schedule_by_source.get(
                                    source_id, {}
                                ).items()
                            )
                        )
                    ),
                )
                for source_id in (
                    str(player.source_id) for player in players
                )
            ),
            tuple(
                (
                    job,
                    source_id,
                    tuple(
                        (
                            canonical_skill(row.get("skill")),
                            round(as_number(row.get("start")), 3),
                            round(as_number(row.get("end")), 3),
                            round(as_number(row.get("action_speed_percent")), 2),
                            row.get("_score_buff_start"),
                            row.get("_score_buff_end"),
                            round(
                                as_number(row.get("_score_carry_in_local_start")),
                                3,
                            ),
                            round(
                                as_number(row.get("_score_carry_in_local_end")),
                                3,
                            ),
                        )
                        for row in rows
                    ),
                )
                for job, by_source in sorted(score_actual_casts_by_job.items())
                for source_id, rows in sorted(by_source.items())
            ),
        )
        if signature == self._team_score_theory_signature:
            self._advance_view_progress(
                "score",
                TEAM_SCORE_PROGRESS_TIMING_END,
                "理论轴参数未变化，正在复用本次缓存...",
            )
            return self._team_score_theory_plans
        if signature in self._team_score_theory_plan_cache:
            plans = self._team_score_theory_plan_cache[signature]
            self._team_score_replan_specs_by_source = copy.deepcopy(
                getattr(self, "_team_score_replan_specs_cache", {}).get(
                    signature, {}
                )
            )
            self._team_score_theory_signature = signature
            self._team_score_theory_plans = plans
            self._team_score_theory_jobs = jobs
            self._team_score_theory_scope = scope
            self._advance_view_progress(
                "score",
                TEAM_SCORE_PROGRESS_TIMING_END,
                "已命中理论轴缓存，正在整理评分结果...",
            )
            return plans
        now = time.monotonic()
        if (
            self._capture_active
            and self._team_score_theory_plans
            and jobs == self._team_score_theory_jobs
            and scope == self._team_score_theory_scope
            and now - self._team_score_theory_built_at < 12.0
        ):
            return self._team_score_theory_plans

        blocked = self._team_score_blocked_intervals(local_output_windows, window)
        unique_jobs: list[str] = []
        for job in jobs:
            if job and job not in unique_jobs:
                unique_jobs.append(job)
        default_builds = load_skill_build_defaults()
        specs: list[dict[str, Any]] = []
        baseline_source_ids: list[str] = []
        base_specs_by_source: dict[str, dict[str, Any]] = {}
        base_specs_by_job: dict[str, dict[str, Any]] = {}
        glyph_rows_by_job: dict[str, list[dict[str, Any]]] = {}
        glyph_auto_by_job: dict[str, bool] = {}
        glyph_rows_by_source: dict[str, list[dict[str, Any]]] = {}
        builds_by_source: dict[str, dict[str, Any]] = {}
        glyph_auto_by_source: dict[str, bool] = {}
        real_job_by_job: dict[str, str] = {}
        build_for_job = getattr(editor, "_extreme_timeline_build_for_job", None)
        score_build_for_job = getattr(
            editor, "_extreme_team_score_build_for_job", build_for_job
        )
        apply_glyphs = getattr(editor, "_extreme_apply_auto_skill_glyphs", None)
        score_excluded_glyphs = getattr(
            editor, "_extreme_score_excluded_glyphs", None
        )
        score_forced_glyphs = getattr(
            editor, "_extreme_score_forced_glyphs", None
        )
        real_job_for = getattr(editor, "_team_real_job", lambda value: value)
        cache_value = getattr(editor, "_team_practical_cache_value", None)
        build_context_signature = (
            cache_value(getattr(editor, "build", {}))
            if callable(cache_value)
            else repr(getattr(editor, "build", {}))
        )
        total_unique_jobs = max(1, len(unique_jobs))
        for job_index, job in enumerate(unique_jobs, start=1):
            self._advance_view_progress(
                "score",
                TEAM_SCORE_PROGRESS_PREP_START
                + int(
                    (TEAM_SCORE_PROGRESS_PREP_END - TEAM_SCORE_PROGRESS_PREP_START)
                    * job_index
                    / total_unique_jobs
                ),
                f"正在准备默认装备、全L卡片、加点、一键Buff与实战纹章 "
                f"{job_index}/{total_unique_jobs}（{job}）...",
            )
            QApplication.processEvents()
            # Team-wide duration glyphs are valued by the number of members who
            # benefit, so a build cached for a smaller party is not interchangeable
            # with the same job's eight-player scoring build.
            build_cache_key = (
                build_context_signature,
                boss_preset_name,
                job,
                len(team_jobs),
                (
                    cache_value(team_objective_profiles.get(job))
                    if callable(cache_value)
                    else repr(team_objective_profiles.get(job))
                ),
                tuple(
                    _team_score_glyph_row_key(row)
                    for row in self._team_score_glyph_overrides_by_job.get(
                        self._team_score_real_job_name(job), ()
                    )
                ),
            )
            cached_build = self._team_score_build_override_cache.get(build_cache_key)
            build_override = (
                copy.deepcopy(cached_build)
                if isinstance(cached_build, dict)
                else (
                    score_build_for_job(job)
                    if callable(score_build_for_job)
                    else None
                )
            )
            if isinstance(build_override, dict):
                _team_score_apply_boss_preset(
                    build_override, boss_preset_name
                )
            real_job = norm(real_job_for(job))
            defaults = default_builds.get(real_job, {})
            if (
                cached_build is None
                and isinstance(build_override, dict)
                and isinstance(defaults, dict)
            ):
                build_override["skill_build"] = copy.deepcopy(
                    defaults.get("skill_build", {})
                )
                build_override["skill_mastery_build"] = copy.deepcopy(
                    defaults.get("skill_mastery_build", {})
                )
                if callable(apply_glyphs):
                    excluded_glyphs = (
                        score_excluded_glyphs(job)
                        if callable(score_excluded_glyphs)
                        else set(_TEAM_SCORE_DEFAULT_EXCLUDED_GLYPHS)
                    )
                    apply_glyphs(
                        build_override,
                        job,
                        count=4,
                        support_team_size=max(1, len(team_jobs)),
                        team_objective_profile=team_objective_profiles.get(job),
                        fill_to_count=False,
                        excluded_glyphs=excluded_glyphs,
                        forced_glyphs=(
                            score_forced_glyphs(job)
                            if callable(score_forced_glyphs)
                            else None
                        ),
                    )
                self._team_score_build_override_cache[build_cache_key] = copy.deepcopy(
                    build_override
                )
                while len(self._team_score_build_override_cache) > 32:
                    self._team_score_build_override_cache.pop(
                        next(iter(self._team_score_build_override_cache))
                    )
            manual_glyph_rows = self._team_score_glyph_overrides_by_job.get(real_job)
            if isinstance(build_override, dict) and manual_glyph_rows is not None:
                build_override["skill_glyphs"] = {
                    "second_job": real_job,
                    "count": max(4, min(7, len(manual_glyph_rows))),
                    "rows": clean_score_glyph_rows(manual_glyph_rows, "manual_override"),
                    "auto_selected": False,
                }
            glyph_config = (
                build_override.get("skill_glyphs", {})
                if isinstance(build_override, dict)
                and isinstance(build_override.get("skill_glyphs"), dict)
                else {}
            )
            glyph_rows_by_job[job] = [
                copy.deepcopy(row)
                for row in glyph_config.get("rows", ())
                if isinstance(row, dict)
            ]
            glyph_auto_by_job[job] = bool(glyph_config.get("auto_selected", True))
            real_job_by_job[job] = real_job
            # Damage-only plates must not alter cast priority.  The long axis
            # runs with timing/utility plates only; after the fixed sequence is
            # known, damage plates are selected and applied as pure damage
            # multipliers without rebuilding or perturbing unrelated skills.
            for player, player_job in zip(players, jobs):
                if player_job != job:
                    continue
                source_id = str(player.source_id)
                source_build_override = apply_score_equipment(
                    build_override,
                    getattr(self, "_team_score_equipment_overrides", {}).get(source_id),
                ) if isinstance(build_override, dict) else None
                if isinstance(source_build_override, dict):
                    source_build_override = apply_combat_levels_to_build(
                        source_build_override, skill_levels_by_source.get(source_id, {})
                    )
                    source_build_override['_score_combat_levels_by_provider'] = copy.deepcopy(levels_by_provider)
                    if getattr(self, '_score_v2_context', None):
                        from .score import build_override as apply_v2_build_override
                        source_build_override = apply_v2_build_override(source_build_override, self._score_v2_context, source_id, score_time_offset)
                source_glyph_rows = copy.deepcopy(glyph_rows_by_job[job])
                source_glyph_auto = glyph_auto_by_job[job]
                inferred_rows = getattr(
                    self,
                    "_team_score_glyph_inference_by_source",
                    {},
                ).get(source_id)
                # Per-player overrides take priority; the legacy job-key API
                # remains supported. Keep inferred rows, fill unknown slots,
                # and apply the explicit required-timing/7-slot replacement policy.
                source_manual_rows = self._team_score_glyph_overrides_by_job.get(source_id, manual_glyph_rows)
                if isinstance(source_build_override, dict):
                    source_glyph_rows, source_glyph_auto = prepare_score_glyphs(
                        editor, source_build_override, job, inferred_rows=inferred_rows,
                        manual_rows=source_manual_rows, team_size=len(team_jobs),
                        team_objective_profile=glyph_profiles_by_source.get(source_id))
                glyph_rows_by_source[source_id] = copy.deepcopy(
                    source_glyph_rows
                )
                glyph_auto_by_source[source_id] = source_glyph_auto
                if isinstance(source_build_override, dict):
                    builds_by_source[source_id] = copy.deepcopy(source_build_override)
                timeline_build_override = _team_score_without_damage_glyphs(
                    source_build_override
                )
                provider = provider_by_source.get(source_id, "")
                scoped_effects = copy.deepcopy(recipient_effects_by_source.get(source_id, effects))
                boss_support_scope = (getattr(self, '_score_v2_context', None) or {}).get('support_boss_scope')
                if boss_support_scope is not None:
                    from .support_boss_scope import opportunities
                    source_by_provider = {name: actor for actor, name in provider_by_source.items()}
                    for effect in scoped_effects:
                        owner = source_by_provider.get(norm(effect.get('provider')))
                        if owner is not None:
                            shared = opportunities(boss_support_scope, owner).get(source_id, ())
                            effect['_score_recipient_eligible_intervals'] = [
                                (max(0., a-score_time_offset), min(window, b-score_time_offset))
                                for a,b in shared if b>score_time_offset and a<score_time_offset+window]
                personal_windows=(timeline_build_override or {}).get('_score_player_output_windows')
                # Separate start permissions from damage eligibility. Retain on
                # every baseline/counterfactual build and in its cache identity.
                from .castability import local_windows
                castability = castability_by_source.get(source_id)
                if castability is not None:
                    timeline_build_override = dict(timeline_build_override or {})
                    timeline_build_override['_score_castability_windows'] = local_windows(
                        castability.windows, score_time_offset, window)
                    timeline_build_override['_score_uncastable_windows'] = local_windows(
                        castability.unavailable_windows, score_time_offset, window)
                personal_blocked=blocked
                if personal_windows is not None:
                    local_personal=[(max(0.,a-score_time_offset),min(window,b-score_time_offset))
                        for a,b in personal_windows if b>score_time_offset and a<score_time_offset+window]
                    personal_blocked=_team_score_merge_intervals([*blocked,*self._team_score_blocked_intervals(local_personal,window)])
                spec = {
                    "job": job,
                    "team": team_jobs,
                    "effects": scoped_effects,
                    "hand_multiplier": 1.0,
                    "window": window,
                    "cast_schedule": recipient_cast_schedule_by_source.get(
                        source_id, cast_schedule
                    ),
                    "include_display_windows": True,
                    "build_override": copy.deepcopy(timeline_build_override),
                    "blocked_intervals": personal_blocked,
                    # Every visible theory axis now owns one real recipient.
                    # Provider casts remain shared, while player Buff windows
                    # and clear events are taken only from this recipient.
                    "partial_cast_schedule": False,
                    "same_environment_theory": True,
                    "provider_override": provider,
                    # Reuse the personal simulator's state/legality rules.
                    # Boss windows, recorded team schedules and the bounded
                    # search policy below remain scoring-specific inputs.
                    "long_sim_planner": True,
                    # The same bounded, mechanism-aware search applies to A,
                    # every B_i and H_i; only search breadth changes, never
                    # recipient evidence, clear times or cooldown legality.
                    "fast_long_timeline": window >= 120.0 and bool(
                        getattr(self, "_team_score_bounded_search", True)
                    ),
                    # Keep exact refinement for timing-neutral counterfactuals,
                    # where it can change CDR/action-speed attribution. The visible
                    # baseline already is the deterministic long-simulation plan;
                    # replaying a second full summon/periodic axis there only adds
                    # latency and did not improve any tested baseline.
                    "score_refinement": False,
                    "score_actual_casts_by_source": {
                        source_id: copy.deepcopy(
                            score_actual_casts_by_job.get(job, {}).get(
                                source_id, []
                            )
                        )
                    },
                    # Owner damage remains the dominant objective. A deliberately
                    # reduced team-value weight acts as a near-tie preference so
                    # support upkeep remains realistic without displacing the
                    # long-simulation damage rotation. Exact support contribution
                    # is still calculated by the separate counterfactual pass.
                    "team_objective_profile": team_objective_profiles.get(job),
                    "boss_hp_segments": boss_hp_segments,
                    "player_buff_clear_times": tuple(
                        clear_times_by_target.get(source_id, ())
                    ),
                    "summon_removal_schedule": tuple(
                        summon_removal_schedule_by_source.get(source_id, ())
                    ),
                }
                specs.append(spec)
                baseline_source_ids.append(source_id)
                base_specs_by_source[source_id] = spec
                base_specs_by_job.setdefault(job, spec)

        neutral_specs: list[dict[str, Any]] = []
        timing_comparison_mode = getattr(self, '_team_score_timing_ablation_mode', 'conditional')
        target_groups = {
            canonical_skill(name)
            for name in ("鸡尾酒", "精神爆发", "灼热火焰", "时间加速")
        }
        effect_skill_name = getattr(
            editor,
            "_team_effect_skill_name",
            lambda value: norm(value),
        )
        timing_units = tuple(sorted({
            (
                norm(effect.get("provider")),
                _team_score_support_effect_group(
                    effect_skill_name(effect.get("skill"))
                ),
            )
            for effect in effects
            if _team_score_support_effect_group(
                effect_skill_name(effect.get("skill"))
            ) in target_groups
            and any(timing_attribute(key) for key in effect.get("attrs", {}))
        }))
        # Declared before the first-wave progress closure; populated with all
        # exact legal candidates after the neutral axes are available.
        ideal_specs: list[dict[str, Any]] = []
        captured_ideal_specs: list[dict[str, Any]] = []
        captured_ideal_source_ids: list[str] = []
        self._advance_view_progress(
            "score",
            TEAM_SCORE_PROGRESS_PREP_END,
            "正在准备减CD/动作速度反事实任务...",
        )
        if timing_comparison_mode != 'conditional':
            self._team_score_support_replan_metadata(
                editor=editor,
                players=players,
                jobs=jobs,
                unique_jobs=unique_jobs,
                effects=effects,
                cast_schedule=cast_schedule,
                output_windows=local_output_windows,
                window=window,
                base_specs_by_job=base_specs_by_job,
                baseline_by_job={},
                glyph_rows_by_job=glyph_rows_by_job,
                glyph_rows_by_source=glyph_rows_by_source,
                run_parallel=run_parallel,
                neutral_task_specs_out=neutral_specs,
                ideal_task_specs_out=captured_ideal_specs,
                ideal_task_keys_out=captured_ideal_source_ids,
                base_specs_by_source=base_specs_by_source,
                baseline_by_source={},
                recipient_damage_weights=recipient_damage_weights,
                recipient_damage_events=recipient_damage_events,
                actual_recipient_damage_weights=recipient_damage_weights,
                actual_recipient_damage_events=recipient_damage_events,
            )
        # The former implementation embedded a serialized CombatSnapshot in
        # every baseline-axis task and then performed the score-exclusion scan
        # plus a possible second full simulation inside that same worker. On
        # long captures this produced eight large task payloads and left the
        # most expensive job running alone after the other workers went idle.
        #
        # The existing explicit score_exclusions -> rebuild_excluded_axes
        # phases use the same formula and exact replan spec after all baseline
        # axes finish. Keep the fused path only as an opt-in A/B diagnostic;
        # production submits compact baselines and queues independent exact
        # replans as follow-ups (without copying the capture to each worker).
        fused_exclusion_prefetch = (
            os.getenv("DRAGON_NEST_SCORE_FUSED_EXCLUSION_PREFETCH", "0")
            .strip()
            .lower()
            in {"1", "true", "on", "yes"}
        )
        fused_exclusion_prefetch = fused_exclusion_prefetch and not bool(getattr(self,'_score_v2_context',None))
        if fused_exclusion_prefetch:
            from ..timeline_parallel import serialized_score_snapshot

            score_snapshot_reference = serialized_score_snapshot(snapshot)
            for source_id, spec in base_specs_by_source.items():
                job = player_job_by_source.get(
                    source_id, norm(spec.get("job"))
                )
                exclusion_plan_metadata = {
                    "_score_time_offset": score_time_offset,
                    "_score_theory_cdr_schedule": copy.deepcopy(
                        recipient_cast_schedule_by_source.get(
                            source_id, cast_schedule
                        )
                    ),
                    "_score_actual_cdr_schedule": copy.deepcopy(
                        recipient_active_schedule_by_source.get(
                            source_id, actual_cast_schedule
                        )
                    ),
                    "_score_boss_hp_segments": boss_hp_segments,
                }
                spec["score_exclusion_context"] = {
                    "snapshot": score_snapshot_reference,
                    "source_ids": (source_id,),
                    "output_windows": output_windows,
                    "boss_target_ids": boss_target_ids,
                    "boss_name": boss_preset_name,
                    "boss_profile": copy.deepcopy(boss_preset),
                    "glyph_rows": copy.deepcopy(
                        glyph_rows_by_source.get(
                            source_id, glyph_rows_by_job.get(job, [])
                        )
                    ),
                    "glyph_auto": glyph_auto_by_source.get(
                        source_id, glyph_auto_by_job.get(job, True)
                    ),
                    "real_job": real_job_by_job.get(
                        job, norm(real_job_for(job))
                    ),
                    "plan_metadata": exclusion_plan_metadata,
                }
        previous_process_events = bool(
            getattr(editor, "_extreme_timeline_process_events", False)
        )
        runtime_boss_values = {
            "_extreme_element_resists": {
                element: as_number(boss_preset.get("element_resist"))
                for element in ("火", "水", "光", "暗")
            },
            "_extreme_monster_defense": as_number(
                boss_preset.get("monster_defense")
            ),
            "_extreme_crit_resist_value": as_number(
                boss_preset.get("crit_resist")
            ),
        }
        missing_runtime_value = object()
        previous_runtime_values = {
            key: getattr(editor, key, missing_runtime_value)
            for key in runtime_boss_values
        }
        try:
            setattr(editor, "_extreme_timeline_process_events", True)
            for key, value in runtime_boss_values.items():
                setattr(editor, key, copy.deepcopy(value))
            # Baselines and their independent follow-ups share the same bounded
            # pool. Large capture data stays in the coordinator.
            reversed_neutral_specs = list(reversed(neutral_specs))
            reversed_neutral_source_ids = list(reversed(baseline_source_ids))[
                :len(reversed_neutral_specs)
            ]
            reversed_neutral_jobs = [
                player_job_by_source.get(source_id, "")
                for source_id in reversed_neutral_source_ids
            ]
            combined_specs = [*specs, *reversed_neutral_specs]
            selected_glyph_rows_by_source: dict[
                str, list[dict[str, Any]]
            ] = {}
            baseline_timing_results_by_source: dict[str, dict[str, Any]] = {}
            inactive_timing_ablation_refs: set[tuple[tuple[str, str], str]] = set()

            def prepare_baseline_glyphs(
                source_id: str,
                result: dict[str, Any],
            ) -> list[dict[str, Any]]:
                """Select A-axis glyphs once, before any pipelined B axis.

                B/H comparison axes must use exactly the glyph set selected on
                A.  Preparing it in the completion callback lets a finished
                recipient immediately feed its B axes to idle workers without
                changing glyph selection or any scoring formula.
                """

                prepared = result.get("_score_baseline_glyphs_prepared")
                if prepared:
                    return [
                        copy.deepcopy(row)
                        for row in result.get("_score_glyphs", ())
                        if isinstance(row, dict)
                    ]
                job = player_job_by_source.get(source_id, "")
                if result.get("_score_pipeline_glyphs_prepared"):
                    selected_rows = [
                        copy.deepcopy(row)
                        for row in result.get("_score_glyphs", ())
                        if isinstance(row, dict)
                    ]
                    glyph_rows_by_source[source_id] = copy.deepcopy(
                        selected_rows
                    )
                    result["_score_baseline_glyphs_prepared"] = True
                    selected_glyph_rows_by_source[source_id] = copy.deepcopy(
                        selected_rows
                    )
                    return selected_rows
                selected_rows = glyph_rows_by_source.get(
                    source_id,
                    glyph_rows_by_job.get(job, []),
                )
                if glyph_auto_by_source.get(
                    source_id,
                    glyph_auto_by_job.get(job, True),
                ):
                    selected_rows = _team_score_refine_fixed_axis_damage_glyphs(
                        editor.store,
                        real_job_by_job.get(job, norm(real_job_for(job))),
                        selected_rows,
                        result,
                        count=max(4, min(7, len(selected_rows))),
                    )
                    glyph_rows_by_source[source_id] = copy.deepcopy(
                        selected_rows
                    )
                _team_score_apply_fixed_axis_damage_glyphs(
                    result,
                    selected_rows,
                )
                result["_score_glyphs"] = copy.deepcopy(selected_rows)
                result["_score_baseline_glyphs_prepared"] = True
                selected_glyph_rows_by_source[source_id] = copy.deepcopy(
                    selected_rows
                )
                return [copy.deepcopy(row) for row in selected_rows]

            prefetch_token_by_ref: dict[tuple[tuple[str, str], str], str] = {}
            prefetch_ref_by_token: dict[
                str, tuple[tuple[str, str], str]
            ] = {}
            retimed_token_by_ref: dict[
                tuple[tuple[str, str], str], str
            ] = {}
            retimed_reused_results: dict[
                tuple[tuple[str, str], str], dict[str, Any]
            ] = {}
            retimed_scheduled_units: set[tuple[str, str]] = set()
            retimed_future_completed = 0
            pipelined_exclusions = (
                not bool(getattr(self,'_score_v2_context',None))
                and not fused_exclusion_prefetch
                and configured_timeline_workers(len(combined_specs)) > 1
                and os.getenv("DRAGON_NEST_SCORE_PIPELINED_EXCLUSIONS", "1")
                .strip().lower() not in {"0", "false", "off", "no"}
            )
            exclusion_refs: dict[str, tuple[str, frozenset[str]]] = {}
            exclusion_scan_seconds: dict[str, float] = {}
            exclusion_scan_errors: dict[str, str] = {}
            players_by_source = {str(player.source_id): player for player in players}

            def exclusion_followups(
                index: int, result: dict[str, Any],
            ) -> list[tuple[str, dict[str, Any]]]:
                # Keep the large capture in the coordinator. Only a compact,
                # normal replan spec enters the existing bounded worker pool.
                # The official exclusion pass remains authoritative; the
                # rebuild consumes this result only for an identical key set.
                if not pipelined_exclusions or index >= len(specs):
                    return []
                source_id = baseline_source_ids[index]
                started = time.perf_counter()
                try:
                    prepare_baseline_glyphs(source_id, result)
                    plan = dict(result)
                    plan.update({
                        "_score_time_offset": score_time_offset,
                        "_score_theory_cdr_schedule": recipient_cast_schedule_by_source.get(source_id, cast_schedule),
                        "_score_actual_cdr_schedule": recipient_active_schedule_by_source.get(source_id, actual_cast_schedule),
                        "_score_boss_hp_segments": boss_hp_segments,
                        "_score_glyph_source": score_glyph_source(
                            selected_glyph_rows_by_source.get(source_id, []),
                            source_id in self._team_score_glyph_overrides_by_job
                            or self._team_score_real_job_name(player_job_by_source.get(source_id, ""))
                            in self._team_score_glyph_overrides_by_job,
                        ),
                    })
                    exclusion = self.score_engine._score_player(
                        snapshot, players_by_source[source_id], output_windows,
                        plan, boss_target_ids, None, (), (), True,
                        exclusion_only=True,
                    )
                    if not isinstance(exclusion, tuple) or len(exclusion) != 2:
                        return []
                    excluded = exclusion[0]
                    if not isinstance(excluded, frozenset) or not excluded:
                        return []
                    token = f"exclusion:{index}"
                    spec = dict(base_specs_by_source[source_id])
                    spec.pop("score_exclusion_context", None)
                    spec["excluded_skill_keys"] = sorted(excluded)
                    exclusion_refs[token] = (source_id, excluded)
                    return [(token, spec)]
                except Exception as exc:
                    # An optional prefetch failure must not replace the
                    # official pass with an incomplete or approximate score.
                    exclusion_scan_errors[source_id] = repr(exc)
                    return []
                finally:
                    exclusion_scan_seconds[source_id] = time.perf_counter() - started

            def baseline_followups(
                index: int, result: dict[str, Any],
            ) -> list[tuple[str, dict[str, Any]]]:
                return [
                    *exclusion_followups(index, result),
                    *conditional_followups(index, result),
                ]

            def conditional_followups(
                index: int,
                result: dict[str, Any],
            ) -> list[tuple[str, dict[str, Any]]]:
                if (
                    timing_comparison_mode != "conditional"
                    or index >= len(specs)
                    or not timing_units
                ):
                    return []
                source_id = baseline_source_ids[index]
                selected_rows = prepare_baseline_glyphs(source_id, result)
                baseline_timing_results_by_source[source_id] = result
                rows: list[tuple[str, dict[str, Any]]] = []
                for unit_index, unit in enumerate(timing_units):
                    changed = without_timing_unit(
                        specs[index],
                        unit,
                        skill_name=effect_skill_name,
                        skill_group=_team_score_support_effect_group,
                    )
                    resolved = changed if changed is not None else specs[index]
                    token = f"timing-b:{index}:{unit_index}"
                    followup_spec = dict(
                        resolved,
                        timing_ablation_glyphs=copy.deepcopy(selected_rows),
                        _timing_ablation_source=source_id,
                    )
                    if timing_unit_explicitly_inactive(
                        specs[index], unit,
                        skill_name=effect_skill_name,
                        skill_group=_team_score_support_effect_group,
                    ):
                        # The captured effect has explicit empty recipient
                        # intervals and no casts. Removing its timing channel
                        # cannot change this recipient's A-axis. Preserve the
                        # B task/token so conditional H replans still run.
                        from ..timeline_parallel import summarize_timing_axis

                        followup_spec["_timing_precomputed_result"] = (
                            summarize_timing_axis(result, ())
                        )
                        inactive_timing_ablation_refs.add((unit, source_id))
                    prefetch_token_by_ref[(unit, source_id)] = token
                    prefetch_ref_by_token[token] = (unit, source_id)
                    rows.append((token, followup_spec))
                return rows

            conditional_candidate_cache = {}
            conditional_candidate_diagnostics = []

            def conditional_candidate_specs(
                unit: tuple[str, str],
                absent_results: dict[str, dict[str, Any]],
            ) -> dict[str, dict[str, Any]]:
                weights, damage_events = _team_score_theory_damage_objective(
                    absent_results,
                    players,
                    local_output_windows,
                )
                # Prefetch and final accounting ask for the same candidate.
                # This cache belongs only to this score, includes the exact
                # objective (no rounded times/attributes), and returns private
                # specs. It never caches per-cast Buff snapshots or damage.
                candidate_key = None
                import os
                if os.environ.get('DN_SCORE_EXACT_CANDIDATE_REUSE', '1') == '1':
                    import hashlib, pickle
                    candidate_inputs = (
                        unit, weights, damage_events, players, jobs, unique_jobs,
                        effects, cast_schedule, local_output_windows, window,
                        base_specs_by_job, base_specs_by_source,
                        glyph_rows_by_job, glyph_rows_by_source,
                        recipient_damage_weights, recipient_damage_events,
                        editor.collect_build(),
                    )
                    candidate_key = hashlib.sha256(pickle.dumps(candidate_inputs, protocol=5)).digest()
                    if os.environ.get('DN_SCORE_CANDIDATE_DIAGNOSTICS') == '1':
                        conditional_candidate_diagnostics.append({
                            'unit': unit, 'hit': candidate_key in conditional_candidate_cache,
                            'parts': [hashlib.sha256(pickle.dumps(v, protocol=5)).hexdigest()
                                      for v in candidate_inputs],
                        })
                    if candidate_key in conditional_candidate_cache:
                        return copy.deepcopy(conditional_candidate_cache[candidate_key])
                proposed: list[dict[str, Any]] = []
                proposed_ids: list[str] = []
                self._team_score_support_replan_metadata(
                    editor=editor,
                    players=players,
                    jobs=jobs,
                    unique_jobs=unique_jobs,
                    effects=effects,
                    cast_schedule=cast_schedule,
                    output_windows=local_output_windows,
                    window=window,
                    base_specs_by_job=base_specs_by_job,
                    baseline_by_job={},
                    glyph_rows_by_job=glyph_rows_by_job,
                    glyph_rows_by_source=glyph_rows_by_source,
                    run_parallel=run_parallel,
                    neutral_task_specs_out=[],
                    ideal_task_specs_out=proposed,
                    ideal_task_keys_out=proposed_ids,
                    base_specs_by_source=base_specs_by_source,
                    baseline_by_source={},
                    recipient_damage_weights=weights,
                    recipient_damage_events=damage_events,
                    actual_recipient_damage_weights=recipient_damage_weights,
                    actual_recipient_damage_events=recipient_damage_events,
                )
                result = dict(zip(proposed_ids, proposed))
                if candidate_key is not None:
                    conditional_candidate_cache[candidate_key] = copy.deepcopy(result)
                return result

            def conditional_retimed_followups(
                token: str,
                _result: dict[str, Any],
                completed_results: dict[str, dict[str, Any]],
            ) -> list[tuple[str, dict[str, Any]]]:
                ref = prefetch_ref_by_token.get(token)
                if ref is None:
                    return []
                unit = ref[0]
                if unit in retimed_scheduled_units:
                    return []
                source_tokens = {
                    source_id: prefetch_token_by_ref.get((unit, source_id), "")
                    for source_id in baseline_source_ids
                }
                if any(
                    not source_token
                    or source_token not in completed_results
                    for source_token in source_tokens.values()
                ):
                    return []
                absent_results = {
                    source_id: completed_results[source_token]
                    for source_id, source_token in source_tokens.items()
                }
                candidates = conditional_candidate_specs(unit, absent_results)
                if set(candidates) != set(base_specs_by_source):
                    return []
                retimed_scheduled_units.add(unit)
                rows: list[tuple[str, dict[str, Any]]] = []
                unit_index = timing_units.index(unit)
                for source_index, source_id in enumerate(baseline_source_ids):
                    changed = retimed_unit(
                        base_specs_by_source[source_id],
                        candidates[source_id],
                        unit,
                        skill_name=effect_skill_name,
                        skill_group=_team_score_support_effect_group,
                    )
                    nested_token = f"timing-h:{unit_index}:{source_index}"
                    if changed is base_specs_by_source[source_id]:
                        # The exact H-axis inputs equal A. A already owns the
                        # selected damage glyphs, so reuse its timing summary
                        # instead of replaying the entire long encounter.
                        prepared = baseline_timing_results_by_source.get(
                            source_id
                        )
                        if prepared is not None:
                            from ..timeline_parallel import summarize_timing_axis

                            retimed_reused_results[(unit, source_id)] = (
                                summarize_timing_axis(prepared, ())
                            )
                            retimed_prefetch_progress(
                                retimed_future_completed,
                                expected_retimed_count,
                                nested_token,
                                player_job_by_source.get(source_id, ""),
                            )
                            continue
                    retimed_token_by_ref[(unit, source_id)] = nested_token
                    rows.append((
                        nested_token,
                        dict(
                            changed,
                            timing_ablation_glyphs=copy.deepcopy(
                                selected_glyph_rows_by_source.get(
                                    source_id,
                                    glyph_rows_by_source.get(source_id, []),
                                )
                            ),
                            _timing_ablation_source=source_id,
                        ),
                    ))
                # Otherwise the final heavy recipients sit behind cheap axes
                # in every H wave and run alone after most workers go idle.
                # Tokens/reference maps remain in their original order; only
                # executor submission order changes.
                return _order_score_followups_by_cost(
                    rows, getattr(editor, "_extreme_timeline_job_costs", {}),
                )
            provisional_exclusions: dict[str, frozenset[str]] = {}
            precomputed_exclusion_plans: dict[
                str, tuple[frozenset[str], dict[str, Any]]
            ] = {}
            baseline_done = 0
            neutral_done = 0
            ideal_done = 0
            prefetched_timing_done = 0
            prefetched_retimed_done = 0
            prefetched_exclusion_done = 0
            expected_exclusion_count = len(specs) if pipelined_exclusions else 0
            expected_prefetch_count = (
                len(specs) * len(timing_units)
                if timing_comparison_mode == "conditional"
                else 0
            )
            expected_retimed_count = expected_prefetch_count
            theory_task_count = max(
                1,
                len(combined_specs)
                + expected_prefetch_count
                + expected_exclusion_count
                + expected_retimed_count,
            )
            # Conditional scoring pipelines its B/H timing axes directly
            # behind each completed baseline.  Those follow-ups are the same
            # expensive work that the later formal pass consumes, so their
            # progress must span the whole theory/timing range instead of
            # being squeezed into the old baseline-only slice.
            first_wave_progress_end = (
                TEAM_SCORE_PROGRESS_TIMING_END
                if timing_comparison_mode == "conditional"
                else TEAM_SCORE_PROGRESS_BASELINE_END
            )
            theory_parallel_width = configured_timeline_workers(
                theory_task_count
            )
            parallel_started = time.perf_counter()
            theory_heartbeat_anchor_completed = 0
            theory_heartbeat_anchor_at = parallel_started

            def theory_eta_text(completed: int) -> str:
                """Return a stable ETA without letting a growing queue rewind it."""

                elapsed = max(0.0, time.perf_counter() - parallel_started)
                if completed < min(4, theory_task_count):
                    return "正在收集首批任务耗时"
                remaining = (
                    elapsed / max(1, completed)
                    * max(0, theory_task_count - completed)
                )
                return (
                    f"预计剩余{remaining / 60:.1f}分钟"
                    if remaining >= 90.0
                    else f"预计剩余{remaining:.0f}s"
                )

            def theory_progress_text(
                completed: int,
                *,
                suffix: str,
            ) -> str:
                elapsed = max(0.0, time.perf_counter() - parallel_started)
                removal_done = (
                    prefetched_timing_done
                    if expected_prefetch_count
                    else neutral_done
                )
                removal_total = (
                    expected_prefetch_count
                    if expected_prefetch_count
                    else len(neutral_specs)
                )
                return (
                    f"正在并行生成计分理论轴 {completed}/{theory_task_count}｜"
                    f"基础轴 {baseline_done}/{len(specs)}｜"
                    f"移除轴 {removal_done}/{removal_total}｜"
                    f"改时机轴 {prefetched_retimed_done}/{expected_retimed_count}｜"
                    f"豁免轴 {prefetched_exclusion_done}/{len(exclusion_refs)}｜"
                    f"辅助候选 {ideal_done}/{len(ideal_specs)}｜"
                    f"已用{elapsed:.0f}s，{theory_eta_text(completed)}｜{suffix}"
                )

            def theory_progress(completed: int, index: int, job: str) -> None:
                nonlocal baseline_done, neutral_done, ideal_done
                nonlocal theory_heartbeat_anchor_completed
                nonlocal theory_heartbeat_anchor_at
                if index < len(specs):
                    baseline_done += 1
                    phase = "平衡/豁免轴"
                elif index < len(specs) + len(reversed_neutral_specs):
                    neutral_done += 1
                    phase = "减CD反事实轴"
                else:
                    ideal_done += 1
                    phase = "辅助最优时机轴"
                overall_completed = (
                    baseline_done
                    + neutral_done
                    + ideal_done
                    + prefetched_timing_done
                    + prefetched_retimed_done
                    + prefetched_exclusion_done
                )
                theory_heartbeat_anchor_completed = overall_completed
                theory_heartbeat_anchor_at = time.perf_counter()
                self._advance_view_progress(
                    "score",
                    TEAM_SCORE_PROGRESS_PREP_END
                    + (
                        first_wave_progress_end
                        - TEAM_SCORE_PROGRESS_PREP_END
                    )
                    * overall_completed
                    / theory_task_count,
                    theory_progress_text(
                        overall_completed,
                        suffix=f"刚完成{phase}（{job}）",
                    ),
                )

            def prefetch_progress(
                completed: int,
                scheduled: int,
                _token: str,
                job: str,
            ) -> None:
                nonlocal prefetched_timing_done
                nonlocal prefetched_exclusion_done
                nonlocal theory_heartbeat_anchor_completed
                nonlocal theory_heartbeat_anchor_at
                if _token in exclusion_refs:
                    prefetched_exclusion_done += 1
                else:
                    prefetched_timing_done += 1
                overall_completed = (
                    baseline_done
                    + neutral_done
                    + ideal_done
                    + prefetched_timing_done
                    + prefetched_retimed_done
                    + prefetched_exclusion_done
                )
                theory_heartbeat_anchor_completed = overall_completed
                theory_heartbeat_anchor_at = time.perf_counter()
                self._advance_view_progress(
                    "score",
                    TEAM_SCORE_PROGRESS_PREP_END
                    + (
                        first_wave_progress_end
                        - TEAM_SCORE_PROGRESS_PREP_END
                    )
                    * overall_completed
                    / theory_task_count,
                    theory_progress_text(
                        overall_completed,
                        suffix=(
                            f"刚完成{'豁免轴' if _token in exclusion_refs else '移除轴'}（{job}）；"
                            f"当前已提交{scheduled}/{expected_prefetch_count + expected_exclusion_count}"
                        ),
                    ),
                )

            def retimed_prefetch_progress(
                completed: int,
                scheduled: int,
                _token: str,
                job: str,
            ) -> None:
                nonlocal prefetched_retimed_done
                nonlocal retimed_future_completed
                nonlocal theory_heartbeat_anchor_completed
                nonlocal theory_heartbeat_anchor_at
                retimed_future_completed = completed
                prefetched_retimed_done = completed + len(
                    retimed_reused_results
                )
                overall_completed = (
                    baseline_done
                    + neutral_done
                    + ideal_done
                    + prefetched_timing_done
                    + prefetched_retimed_done
                    + prefetched_exclusion_done
                )
                theory_heartbeat_anchor_completed = overall_completed
                theory_heartbeat_anchor_at = time.perf_counter()
                self._advance_view_progress(
                    "score",
                    TEAM_SCORE_PROGRESS_PREP_END
                    + (
                        first_wave_progress_end
                        - TEAM_SCORE_PROGRESS_PREP_END
                    )
                    * overall_completed
                    / theory_task_count,
                    theory_progress_text(
                        overall_completed,
                        suffix=(
                            f"刚完成改时机轴（{job}）；"
                            f"当前已提交{scheduled}/{expected_retimed_count}"
                        ),
                    ),
                )

            def theory_heartbeat(completed: int, total: int, jobs: str) -> None:
                """Keep the UI visibly alive while one expensive axis runs."""

                nonlocal theory_heartbeat_anchor_completed
                nonlocal theory_heartbeat_anchor_at

                # ``total`` is supplied by the executor for compatibility,
                # but the UI denominator is fixed before the first task is
                # submitted. Dynamic follow-ups must never make the bar move
                # backwards.
                visible_completed = max(
                    completed,
                    baseline_done
                    + neutral_done
                    + ideal_done
                    + prefetched_timing_done
                    + prefetched_retimed_done
                    + prefetched_exclusion_done,
                )
                now = time.perf_counter()
                if visible_completed != theory_heartbeat_anchor_completed:
                    theory_heartbeat_anchor_completed = visible_completed
                    theory_heartbeat_anchor_at = now
                elapsed = max(0.0, now - parallel_started)
                observed_axis_seconds = (
                    elapsed / visible_completed
                    if visible_completed > 0
                    else 35.0
                )
                expected_axis_seconds = max(
                    5.0,
                    min(60.0, observed_axis_seconds),
                )
                active_elapsed = max(
                    0.0, now - theory_heartbeat_anchor_at
                )
                # Never claim that a task has completed, but also never pin
                # the bar at the former 90% heartbeat ceiling during a very
                # heavy profession axis.  This asymptotic interpolation keeps
                # making honest, progressively smaller advances until an
                # actual worker result increments the exact counter.
                soft_fraction = min(
                    0.99,
                    active_elapsed
                    / (active_elapsed + expected_axis_seconds),
                )
                # Several axes are running simultaneously.  Treat each live
                # worker as at most one partially completed unit; using only
                # one fractional unit left the integer bar unchanged for tens
                # of seconds before the first heavy profession finished.
                active_slots = min(
                    theory_parallel_width,
                    max(0, theory_task_count - visible_completed),
                )
                display_completed = min(
                    float(theory_task_count),
                    visible_completed + active_slots * soft_fraction,
                )
                progress_value = (
                    TEAM_SCORE_PROGRESS_PREP_END
                    + (
                        first_wave_progress_end
                        - TEAM_SCORE_PROGRESS_PREP_END
                    )
                    * display_completed
                    / theory_task_count
                )
                self._advance_view_progress(
                    "score",
                    progress_value,
                    theory_progress_text(
                        visible_completed,
                        suffix=f"当前仍在计算：{jobs or '职业轴'}",
                    ),
                )

            results = run_parallel(
                combined_specs,
                progress_callback=theory_progress,
                followup_factory=(
                    baseline_followups
                    if pipelined_exclusions or (timing_comparison_mode == "conditional" and timing_units)
                    else None
                ),
                followup_progress_callback=prefetch_progress,
                expected_followup_count=(
                    expected_prefetch_count + expected_exclusion_count
                ),
                followup_result_factory=(
                    conditional_retimed_followups
                    if timing_comparison_mode == "conditional" and timing_units
                    else None
                ),
                followup_result_progress_callback=retimed_prefetch_progress,
                expected_followup_result_count=expected_retimed_count,
                heartbeat_callback=theory_heartbeat,
            )
            parallel_elapsed = time.perf_counter() - parallel_started
            baseline_results = results[:len(specs)]
            neutral_start = len(specs)
            ideal_start = neutral_start + len(reversed_neutral_specs)
            neutral_results = results[neutral_start:ideal_start]
            ideal_results: list[dict[str, Any]] = []
            by_source = {
                source_id: result
                for source_id, result in zip(
                    baseline_source_ids, baseline_results
                )
                if isinstance(result, dict)
            }
            neutral_by_source = {
                source_id: result
                for source_id, result in zip(
                    reversed_neutral_source_ids, neutral_results
                )
                if isinstance(result, dict)
            }
            ideal_by_source: dict[str, dict[str, Any]] = {}
            neutral_by_job: dict[str, dict[str, Any]] = {}
            for source_id in reversed_neutral_source_ids:
                job = player_job_by_source.get(source_id, "")
                result = neutral_by_source.get(source_id)
                if job and isinstance(result, dict):
                    neutral_by_job.setdefault(job, result)
            prefetch_seconds_by_job: dict[str, float] = {}
            prefetch_errors_by_job: dict[str, str] = {}
            followup_results = getattr(editor, "_extreme_parallel_followup_results", {})
            for token, (source_id, excluded) in exclusion_refs.items():
                prefetched = followup_results.get(token)
                if isinstance(prefetched, dict):
                    provisional_exclusions[source_id] = excluded
                    precomputed_exclusion_plans[source_id] = (excluded, prefetched)
            for source_id, result in by_source.items():
                job = player_job_by_source.get(source_id, "")
                diagnostic_key = f"{job}:{source_id}"
                worker_prepared = bool(
                    result.pop("_score_pipeline_glyphs_prepared", False)
                )
                worker_prefetched = result.pop(
                    "_score_pipeline_exclusion_results", {}
                )
                prefetch_seconds_by_job[diagnostic_key] = max(
                    0.0,
                    as_number(
                        result.pop("_score_pipeline_prefetch_seconds", 0.0)
                    ),
                )
                prefetch_error = norm(
                    result.pop("_score_pipeline_exclusion_error", "")
                )
                if prefetch_error:
                    prefetch_errors_by_job[diagnostic_key] = prefetch_error
                if worker_prepared:
                    selected_rows = [
                        copy.deepcopy(row)
                        for row in result.get("_score_glyphs", ())
                        if isinstance(row, dict)
                    ]
                    glyph_rows_by_source[source_id] = copy.deepcopy(
                        selected_rows
                    )
                else:
                    selected_rows = prepare_baseline_glyphs(source_id, result)
                    result["_score_time_offset"] = score_time_offset
                    result["_score_theory_cdr_schedule"] = copy.deepcopy(
                        recipient_cast_schedule_by_source.get(
                            source_id, cast_schedule
                        )
                    )
                    result["_score_actual_cdr_schedule"] = copy.deepcopy(
                        recipient_active_schedule_by_source.get(
                            source_id, actual_cast_schedule
                        )
                    )
                    result["_score_glyphs"] = copy.deepcopy(selected_rows)
                    result["_score_boss_hp_segments"] = boss_hp_segments
                selected_glyph_rows_by_source[source_id] = copy.deepcopy(
                    result.get("_score_glyphs", selected_rows)
                )
                result.pop("_score_baseline_glyphs_prepared", None)
                if isinstance(worker_prefetched, dict):
                    for prefetched_source_id, entry in worker_prefetched.items():
                        if not isinstance(entry, dict):
                            continue
                        excluded = frozenset(
                            norm(skill)
                            for skill in entry.get("excluded_skill_keys", ())
                            if norm(skill)
                        )
                        replan_result = entry.get("result")
                        if not excluded or not isinstance(replan_result, dict):
                            continue
                        normalized_source = str(prefetched_source_id)
                        provisional_exclusions[normalized_source] = excluded
                        precomputed_exclusion_plans[normalized_source] = (
                            excluded,
                            replan_result,
                        )
            for source_id, neutral_result in neutral_by_source.items():
                if not isinstance(neutral_result, dict):
                    continue
                job = player_job_by_source.get(source_id, "")
                _team_score_apply_fixed_axis_damage_glyphs(
                    neutral_result,
                    selected_glyph_rows_by_source.get(
                        source_id, glyph_rows_by_job.get(job, [])
                    ),
                )
            if timing_comparison_mode == 'conditional':
                # Percentages share A as a scale, NOT as a counterfactual.
                # Each measured gain below subtracts its own exact B_i axis.
                neutral_by_source = by_source
            prefetched_absent_results: dict[
                tuple[tuple[str, str], str], dict[str, Any]
            ] = {}
            prefetched_retimed_results: dict[
                tuple[tuple[str, str], str], dict[str, Any]
            ] = {}
            if timing_comparison_mode == "conditional":
                prefetched_retimed_results.update(retimed_reused_results)
                followup_results = getattr(
                    editor,
                    "_extreme_parallel_followup_results",
                    {},
                )
                if isinstance(followup_results, dict):
                    for ref, token in prefetch_token_by_ref.items():
                        result = followup_results.get(token)
                        if isinstance(result, dict):
                            prefetched_absent_results[ref] = result
                    for ref, token in retimed_token_by_ref.items():
                        result = followup_results.get(token)
                        if isinstance(result, dict):
                            prefetched_retimed_results[ref] = result
            timing_objective_mode = norm(
                getattr(
                    self,
                    "_team_score_support_timing_objective_mode",
                    "neutral_theory",
                )
            ).lower()
            if timing_objective_mode == "captured":
                # Audit-only compatibility mode.  It deliberately keeps A and
                # N on the current implementation and changes only the damage
                # stream used to place T.  This makes score regressions
                # attributable to the timing objective instead of to an old
                # packaged scorer with unrelated formula differences.
                theory_recipient_weights = copy.deepcopy(
                    recipient_damage_weights
                )
                theory_recipient_damage_events = copy.deepcopy(
                    recipient_damage_events
                )
                timing_objective_name = "captured_damage_stream"
            else:
                theory_recipient_weights, theory_recipient_damage_events = (
                    _team_score_theory_damage_objective(
                        neutral_by_source,
                        players,
                        local_output_windows,
                    )
                )
                timing_objective_name = "neutral_theory_damage_stream"
            # Missing/zero-damage theory plans are rare, but preserving the
            # captured stream for only those recipients is safer than silently
            # dropping them from the shared timing objective.
            for source_id in recipient_damage_events:
                theory_recipient_damage_events.setdefault(
                    source_id, recipient_damage_events[source_id]
                )
                theory_recipient_weights.setdefault(
                    source_id,
                    max(
                        1e-9,
                        as_number(recipient_damage_weights.get(source_id), 1.0),
                    ),
                )

            # T is selected from complete legal schedules, not from one
            # surrogate search result.  The neutral-theory stream prevents a
            # poor captured rotation from lowering the ceiling, while the
            # captured-stream candidate remains legal and can occasionally
            # produce a better exact cooldown/action cascade than the static
            # neutral objective predicts.  The recorded schedule is compared
            # later as ``full_team_dps`` by the metadata pass.
            theory_ideal_specs: list[dict[str, Any]] = []
            theory_ideal_source_ids: list[str] = []
            if timing_objective_mode != "captured" and timing_comparison_mode != 'conditional':
                discarded_neutral_specs: list[dict[str, Any]] = []
                self._team_score_support_replan_metadata(
                    editor=editor,
                    players=players,
                    jobs=jobs,
                    unique_jobs=unique_jobs,
                    effects=effects,
                    cast_schedule=cast_schedule,
                    output_windows=local_output_windows,
                    window=window,
                    base_specs_by_job=base_specs_by_job,
                    baseline_by_job={},
                    glyph_rows_by_job=glyph_rows_by_job,
                    glyph_rows_by_source=glyph_rows_by_source,
                    run_parallel=run_parallel,
                    neutral_task_specs_out=discarded_neutral_specs,
                    ideal_task_specs_out=theory_ideal_specs,
                    ideal_task_keys_out=theory_ideal_source_ids,
                    base_specs_by_source=base_specs_by_source,
                    baseline_by_source={},
                    recipient_damage_weights=theory_recipient_weights,
                    recipient_damage_events=theory_recipient_damage_events,
                    actual_recipient_damage_weights=recipient_damage_weights,
                    actual_recipient_damage_events=recipient_damage_events,
                    timing_objective_name=timing_objective_name,
                )

            candidate_groups: list[
                tuple[str, list[dict[str, Any]], list[str]]
            ] = []
            if theory_ideal_specs:
                candidate_groups.append((
                    "neutral_theory_optimized",
                    theory_ideal_specs,
                    theory_ideal_source_ids,
                ))
            if captured_ideal_specs:
                candidate_groups.append((
                    "captured_stream_optimized",
                    captured_ideal_specs,
                    captured_ideal_source_ids,
                ))

            ideal_specs = [
                spec
                for _label, group_specs, _source_ids in candidate_groups
                for spec in group_specs
            ]
            ideal_source_ids = [
                source_id
                for _label, _group_specs, group_source_ids in candidate_groups
                for source_id in group_source_ids
            ]
            ideal_jobs = [
                player_job_by_source.get(source_id, "")
                for source_id in ideal_source_ids
            ]
            ideal_results: list[dict[str, Any]] = []
            ideal_by_source: dict[str, dict[str, Any]] = {}
            selected_ideal_candidate = "recorded_actual"
            selected_ideal_specs_by_source: dict[str, dict[str, Any]] = {}
            ideal_candidate_team_dps: dict[str, float] = {}
            if timing_comparison_mode == 'conditional':
                selected_ideal_specs_by_source = dict(zip(theory_ideal_source_ids, theory_ideal_specs))
            if ideal_specs and timing_comparison_mode != 'conditional':
                ideal_started = time.perf_counter()

                def ideal_progress(completed: int, _index: int, job: str) -> None:
                    self._advance_view_progress(
                        "score",
                        TEAM_SCORE_PROGRESS_BASELINE_END
                        + int(
                            (
                                TEAM_SCORE_PROGRESS_CANDIDATE_END
                                - TEAM_SCORE_PROGRESS_BASELINE_END
                            )
                            * completed
                            / max(1, len(ideal_specs))
                        ),
                        f"正在精确比较公共辅助时机候选 "
                        f"{completed}/{len(ideal_specs)}（{job}）...",
                    )

                ideal_results = list(run_parallel(
                    ideal_specs,
                    progress_callback=ideal_progress,
                ))
                parallel_elapsed += time.perf_counter() - ideal_started
                cursor = 0
                best_candidate_dps = -1.0
                for label, group_specs, group_source_ids in candidate_groups:
                    group_results = ideal_results[
                        cursor:cursor + len(group_specs)
                    ]
                    cursor += len(group_specs)
                    by_candidate_source = {
                        source_id: result
                        for source_id, result in zip(
                            group_source_ids, group_results
                        )
                        if isinstance(result, dict)
                    }
                    for source_id, ideal_result in by_candidate_source.items():
                        job = player_job_by_source.get(source_id, "")
                        _team_score_apply_fixed_axis_damage_glyphs(
                            ideal_result,
                            selected_glyph_rows_by_source.get(
                                source_id, glyph_rows_by_job.get(job, [])
                            ),
                        )
                    if len(by_candidate_source) != len(baseline_source_ids):
                        continue
                    candidate_dps = _team_score_team_dps_by_source(
                        by_candidate_source,
                        players,
                    )
                    ideal_candidate_team_dps[label] = candidate_dps
                    if candidate_dps > best_candidate_dps + 1e-9:
                        best_candidate_dps = candidate_dps
                        selected_ideal_candidate = label
                        ideal_by_source = by_candidate_source
                        selected_ideal_specs_by_source = dict(zip(group_source_ids, group_specs))
            if timing_objective_mode != "captured":
                timing_objective_name = (
                    "best_exact_of_neutral_theory_and_captured_candidates"
                )
            by_job: dict[str, dict[str, Any]] = {}
            for source_id in baseline_source_ids:
                result = by_source.get(source_id)
                job = player_job_by_source.get(source_id, "")
                if job and isinstance(result, dict):
                    by_job.setdefault(job, result)
            self._team_score_precomputed_exclusion_plans = (
                precomputed_exclusion_plans
            )
            ablations: dict[tuple[str, str], dict[str, float]] = {}
            ablation_diagnostics: dict[str, Any] = {"task_count": 0}
            # "legacy" is an offline A/B audit hook, not an end-user setting.
            if timing_comparison_mode != "legacy":
                units = timing_units
                if units and neutral_by_source:
                    optimized_wins = bool(ideal_by_source) and (
                        _team_score_team_dps_by_source(ideal_by_source, players)
                        > _team_score_team_dps_by_source(by_source, players) + 1e-9
                    )

                    scoring_axes_started = time.perf_counter()
                    formal_axis_task_count = max(
                        1,
                        2 * len(units) * len(base_specs_by_source),
                    )
                    conditional_axis_kind_count = (
                        len(units) * len(base_specs_by_source)
                        if timing_comparison_mode == "conditional"
                        else 0
                    )
                    # Conditional timing reuses the removal axes calculated in
                    # the first wave.  Keep them in the overall progress count,
                    # but do not include them in the ETA denominator because
                    # this timer starts only when the remaining timing axes do.
                    prefetched_formal_count = (
                        len(prefetched_absent_results)
                        + len(prefetched_retimed_results)
                        if timing_comparison_mode == "conditional"
                        else 0
                    )
                    ablation_heartbeat_phase_completed = 0
                    ablation_heartbeat_anchor_at = scoring_axes_started
                    formal_axis_capacity = min(12, formal_axis_task_count)
                    formal_axis_workers = configured_timeline_workers(
                        formal_axis_task_count
                    )
                    planned_worker_text = (
                        f"计划并行进程：{formal_axis_workers}/"
                        f"{formal_axis_capacity}"
                    )

                    def worker_progress_text() -> str:
                        status = timeline_process_pool_status()
                        alive = max(0, int(status.get("aliveWorkers", 0)))
                        if alive > 0:
                            return (
                                f"当前并行进程：{alive}/"
                                f"{formal_axis_capacity}"
                            )
                        if bool(status.get("active")):
                            return planned_worker_text
                        return (
                            f"当前并行进程：1/{formal_axis_capacity}"
                            "（并行回退）"
                        )

                    def ablation_progress(completed: int, total: int, job: str) -> None:
                        nonlocal ablation_heartbeat_phase_completed
                        nonlocal ablation_heartbeat_anchor_at
                        elapsed = max(
                            0.0, time.perf_counter() - scoring_axes_started
                        )
                        phase_completed = max(
                            0, int(completed) - prefetched_formal_count
                        )
                        phase_total = max(
                            0, int(total) - prefetched_formal_count
                        )
                        now = time.perf_counter()
                        if phase_completed != ablation_heartbeat_phase_completed:
                            ablation_heartbeat_phase_completed = phase_completed
                            ablation_heartbeat_anchor_at = now
                        observed_axis_seconds = (
                            elapsed / phase_completed
                            if phase_completed > 0
                            else 35.0
                        )
                        expected_axis_seconds = max(
                            5.0,
                            min(60.0, observed_axis_seconds),
                        )
                        active_elapsed = max(
                            0.0, now - ablation_heartbeat_anchor_at
                        )
                        soft_fraction = min(
                            0.99,
                            active_elapsed
                            / (active_elapsed + expected_axis_seconds),
                        )
                        display_completed = min(
                            float(total),
                            completed + soft_fraction,
                        )
                        remaining = (
                            elapsed
                            / phase_completed
                            * max(0, phase_total - phase_completed)
                            if phase_completed > 0
                            else 0.0
                        )
                        if phase_completed >= min(4, max(1, phase_total)):
                            eta_text = (
                                f"预计剩余{remaining / 60:.1f}分钟"
                                if remaining >= 90.0
                                else f"预计剩余{remaining:.0f}秒"
                            )
                        else:
                            eta_text = "正在计算首批改时机轴"
                        if conditional_axis_kind_count > 0:
                            removal_completed = min(
                                conditional_axis_kind_count,
                                max(0, int(completed)),
                            )
                            retimed_completed = min(
                                conditional_axis_kind_count,
                                max(
                                    0,
                                    int(completed)
                                    - conditional_axis_kind_count,
                                ),
                            )
                            phase_label = (
                                f"移除轴 {removal_completed}/"
                                f"{conditional_axis_kind_count}｜"
                                f"改时机轴 {retimed_completed}/"
                                f"{conditional_axis_kind_count}"
                            )
                            if prefetched_formal_count > 0:
                                phase_label += (
                                    f"（预计算 {min(int(completed), prefetched_formal_count)}/"
                                    f"{prefetched_formal_count}）"
                                )
                        else:
                            phase_label = (
                                f"计分轴 {phase_completed}/{phase_total}"
                            )
                        if timing_comparison_mode == "conditional":
                            combined_total = max(
                                1,
                                len(combined_specs) + int(total),
                            )
                            combined_completed = min(
                                float(combined_total),
                                len(combined_specs) + display_completed,
                            )
                            formal_progress_value = (
                                TEAM_SCORE_PROGRESS_PREP_END
                                + (
                                    TEAM_SCORE_PROGRESS_TIMING_END
                                    - TEAM_SCORE_PROGRESS_PREP_END
                                )
                                * combined_completed
                                / combined_total
                            )
                        else:
                            formal_progress_value = (
                                TEAM_SCORE_PROGRESS_CANDIDATE_END
                                + (
                                    TEAM_SCORE_PROGRESS_TIMING_END
                                    - TEAM_SCORE_PROGRESS_CANDIDATE_END
                                )
                                * display_completed
                                / max(1, total)
                            )
                        self._advance_view_progress(
                            "score",
                            formal_progress_value,
                            f"正在计算计分所需的移除轴与改时机轴 "
                            f"{completed}/{total}（{phase_label}，{job}）｜"
                            f"{worker_progress_text()}｜"
                            f"已用{elapsed:.0f}秒，{eta_text}",
                        )

                    if timing_comparison_mode == "conditional":
                        combined_total = max(
                            1,
                            len(combined_specs) + formal_axis_task_count,
                        )
                        formal_progress_start = (
                            TEAM_SCORE_PROGRESS_PREP_END
                            + (
                                TEAM_SCORE_PROGRESS_TIMING_END
                                - TEAM_SCORE_PROGRESS_PREP_END
                            )
                            * (
                                len(combined_specs)
                                + min(
                                    prefetched_formal_count,
                                    formal_axis_task_count,
                                )
                            )
                            / combined_total
                        )
                    else:
                        formal_progress_start = TEAM_SCORE_PROGRESS_CANDIDATE_END
                    self._advance_view_progress(
                        "score",
                        formal_progress_start,
                        f"正在计算计分所需的移除轴与改时机轴 "
                        f"0/{formal_axis_task_count}｜{planned_worker_text}｜"
                        "正在准备首批任务",
                    )
                    if timing_comparison_mode == 'conditional':
                        ablations, ablation_diagnostics = calculate_conditional_timing(
                            actual_specs=base_specs_by_source, actual_results=by_source,
                            units=units, candidate_factory=conditional_candidate_specs,
                            skill_name=effect_skill_name, skill_group=_team_score_support_effect_group,
                            glyphs_by_source=selected_glyph_rows_by_source,
                            apply_glyphs=_team_score_apply_fixed_axis_damage_glyphs,
                            run_parallel=run_parallel,
                            batch_size=formal_axis_workers,
                            progress_callback=ablation_progress,
                            include_linked_diagnostics=False,
                            include_resistance_diagnostics=False,
                            prefetched_absent_results=prefetched_absent_results,
                            prefetched_retimed_results=prefetched_retimed_results,
                        )
                    else:
                        ablations, ablation_diagnostics = calculate_timing_ablations(
                            actual_specs=base_specs_by_source,
                            theory_specs=selected_ideal_specs_by_source if optimized_wins else base_specs_by_source,
                            actual_results=by_source,
                            theory_results=ideal_by_source if optimized_wins else by_source,
                            units=units, skill_name=effect_skill_name,
                            skill_group=_team_score_support_effect_group,
                            glyphs_by_source=selected_glyph_rows_by_source,
                            apply_glyphs=_team_score_apply_fixed_axis_damage_glyphs,
                            run_parallel=run_parallel,
                            batch_size=formal_axis_workers,
                            progress_callback=ablation_progress,
                            seed_specs=dict(zip(baseline_source_ids, neutral_specs)),
                            seed_results=neutral_by_source,
                            group_by_recipient=True,
                        )
                    parallel_elapsed += ablation_diagnostics["seconds"]
                    self._advance_view_progress(
                        "score",
                        TEAM_SCORE_PROGRESS_TIMING_END,
                        "计分所需的移除轴与改时机轴已完成｜"
                        f"{worker_progress_text()}｜正在汇总正式评分...",
                    )
            support_replans = self._team_score_support_replan_metadata(
                editor=editor,
                players=players,
                jobs=jobs,
                unique_jobs=unique_jobs,
                effects=effects,
                cast_schedule=cast_schedule,
                output_windows=local_output_windows,
                window=window,
                base_specs_by_job=base_specs_by_job,
                baseline_by_job=by_job,
                glyph_rows_by_job=glyph_rows_by_job,
                glyph_rows_by_source=glyph_rows_by_source,
                run_parallel=run_parallel,
                precomputed_neutral_by_job=neutral_by_job,
                base_specs_by_source=base_specs_by_source,
                baseline_by_source=by_source,
                precomputed_neutral_by_source=neutral_by_source,
                precomputed_ideal_by_source=ideal_by_source,
                recipient_damage_weights=theory_recipient_weights,
                recipient_damage_events=theory_recipient_damage_events,
                actual_recipient_damage_weights=recipient_damage_weights,
                actual_recipient_damage_events=recipient_damage_events,
                timing_objective_name=timing_objective_name,
                precomputed_ideal_candidate_name=selected_ideal_candidate,
                precomputed_timing_ablations=ablations,
            )
            self._team_score_theory_diagnostics = {
                'exact_candidate_cache': conditional_candidate_diagnostics,
                "search_policy": "bounded_mechanism_v1" if any(
                    spec.get("fast_long_timeline") for spec in specs
                ) else "full_long_simulation",
                "parallel_seconds": parallel_elapsed,
                "timing_ablation": ablation_diagnostics,
                "support_timing_candidate_team_dps": copy.deepcopy(
                    ideal_candidate_team_dps
                ),
                "selected_support_timing_candidate": (
                    'independent_per_provider' if timing_comparison_mode == 'conditional'
                    else selected_ideal_candidate
                ),
                "task_count": (
                    len(combined_specs)
                    + (0 if timing_comparison_mode == 'conditional' else len(ideal_specs))
                    + len(precomputed_exclusion_plans)
                    + ablation_diagnostics["task_count"]
                ),
                "prefetched_exclusion_count": len(
                    precomputed_exclusion_plans
                ),
                "retimed_reused_baseline_count": len(retimed_reused_results),
                "inactive_timing_ablation_reused_count": len(inactive_timing_ablation_refs),
                "prefetched_exclusions": {
                    source_id: sorted(skill_keys)
                    for source_id, skill_keys in provisional_exclusions.items()
                },
                "prefetch_seconds_by_job": prefetch_seconds_by_job,
                "prefetch_errors_by_job": prefetch_errors_by_job,
                "pipelined_exclusion_scan_seconds": exclusion_scan_seconds,
                "pipelined_exclusion_scan_errors": exclusion_scan_errors,
                "task_profiles": [
                    {
                        "phase": (
                            "balanced"
                            if index < len(specs)
                            else (
                                "neutral"
                                if index < len(specs) + len(reversed_neutral_specs)
                                else "support_ideal"
                            )
                        ),
                        "job": job,
                        "segments": len(result.get("segments", ())),
                        "refinement": copy.deepcopy(
                            result.get("planner_refinement", {})
                        ),
                        **copy.deepcopy(result.get("_parallel_profile", {})),
                    }
                    for index, (job, result) in enumerate(
                        zip(
                            [
                                *(
                                    player_job_by_source.get(source_id, "")
                                    for source_id in baseline_source_ids
                                ),
                                *reversed_neutral_jobs,
                                *ideal_jobs,
                            ],
                            [*results, *ideal_results],
                        )
                    )
                    if isinstance(result, dict)
                ],
            }
            plans = {
                norm(player.source_id): {
                    **by_source[norm(player.source_id)],
                    "_score_equipment": scoring_equipment_record(
                        {**builds_by_source.get(norm(player.source_id), {}), "skill_glyphs": {
                            "second_job": self._team_score_real_job_name(job),
                            "count": 7,
                            "rows": selected_glyph_rows_by_source.get(norm(player.source_id), glyph_rows_by_source.get(norm(player.source_id), [])),
                        }},
                        custom=norm(player.source_id) in getattr(self, "_team_score_equipment_overrides", {}),
                    ),
                    "_score_glyphs": copy.deepcopy(
                        selected_glyph_rows_by_source.get(
                            norm(player.source_id),
                            glyph_rows_by_source.get(
                                norm(player.source_id),
                                glyph_rows_by_job.get(job, []),
                            ),
                        )
                    ),
                    "_score_glyph_source": (
                        score_glyph_source(selected_glyph_rows_by_source.get(norm(player.source_id), []),
                            norm(player.source_id) in self._team_score_glyph_overrides_by_job
                            or self._team_score_real_job_name(job) in self._team_score_glyph_overrides_by_job)
                    ),
                    "_score_combat_skill_levels": copy.deepcopy(skill_levels_by_source.get(norm(player.source_id), {})),
                    "_support_counterfactual_replan": copy.deepcopy(
                        support_replans.get(norm(player.source_id), {})
                    ),
                }
                for player, job in zip(players, jobs)
                if norm(player.source_id) in by_source
            }
            if isinstance(ablation_diagnostics, dict):
                # The formal score only needs the removal/retiming axes.
                # Linked-coverage and resistance-group decomposition are
                # optional diagnostics and are not scheduled after scoring.
                if ablation_diagnostics.pop("diagnostics_deferred", False):
                    ablation_diagnostics["diagnostics_skipped"] = True
            replan_specs_by_source = {
                source_id: {
                    key: copy.deepcopy(value)
                    for key, value in base_specs_by_source[source_id].items()
                    if key != "score_exclusion_context"
                }
                for source_id in baseline_source_ids
                if source_id in base_specs_by_source
            }
            self._team_score_replan_specs_by_source = replan_specs_by_source
            self._team_score_theory_signature = signature
            self._team_score_theory_plans = plans
            self._team_score_theory_jobs = jobs
            self._team_score_theory_scope = scope
            self._team_score_theory_built_at = now
            self._team_score_theory_error = ""
            self._team_score_theory_plan_cache[signature] = plans
            replan_cache = getattr(
                self, "_team_score_replan_specs_cache", None
            )
            if not isinstance(replan_cache, dict):
                replan_cache = {}
                self._team_score_replan_specs_cache = replan_cache
            replan_cache[signature] = copy.deepcopy(replan_specs_by_source)
            while len(self._team_score_theory_plan_cache) > 8:
                oldest = next(iter(self._team_score_theory_plan_cache))
                self._team_score_theory_plan_cache.pop(oldest)
                replan_cache.pop(oldest, None)
            return plans
        except Exception as exc:
            self._team_score_theory_error = repr(exc)
            # Never score a newly selected Boss with the previous Boss's plan.
            # The score engine can build a conservative plan for the current
            # target scope when the exact timeline planner fails.
            self._team_score_theory_signature = None
            self._team_score_theory_plans = {}
            self._team_score_theory_jobs = ()
            self._team_score_theory_scope = ()
            self._team_score_replan_specs_by_source = {}
            if isinstance(exc, TimingAblationError):
                raise
            return {}
        finally:
            setattr(
                editor,
                "_extreme_timeline_process_events",
                previous_process_events,
            )
            for key, previous_value in previous_runtime_values.items():
                if previous_value is missing_runtime_value:
                    try:
                        delattr(editor, key)
                    except AttributeError:
                        pass
                else:
                    setattr(editor, key, previous_value)
