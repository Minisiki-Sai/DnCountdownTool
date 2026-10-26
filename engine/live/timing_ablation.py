"""Exact leave-one-provider-out timing experiments, with bounded live results.

Keep A/T provider schedules fixed; replan the recipients after removing one
provider's CDR/action-speed effect. Cocktail's non-timing stats stay on both
sides. These marginal gains overlap: their sum is NOT the joint A-N/T-N gain.

The linked-support diagnostic adds a third axis.  It keeps every other support
cast's captured *intent*, but delays a cast when removal of the timing effect
makes that timestamp cooldown-illegal.  The resulting B-L delta measures the
extra team-Buff/Debuff coverage enabled by the accelerator without assigning
the same interval to both the accelerator and the downstream provider.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import pickle
import time
from typing import Any, Callable

Unit = tuple[str, str]
_EXACT_ROUND2 = os.environ.get('DN_SCORE_EXACT_ROUND2', '1') == '1'

_ACCELERATION_RESISTANCE_GROUP_SKILLS = {
    "group1_haste_resistance": {
        "精神爆发", "变异精神爆发", "灼热火焰", "变异猎人之眼",
    },
    "group2_cooldown_resistance": {"时间加速", "变异时间加速"},
}


class TimingAblationError(ValueError):
    """Incomplete experiments must not silently become approximate scores."""


def timing_attribute(key: object) -> bool:
    name = str(key or "").strip()
    return "动作速度" in name or "攻速" in name or (
        "冷却" in name and any(word in name for word in ("缩减", "减少", "恢复"))
    )


def timing_unit_explicitly_inactive(
    spec: dict[str, Any], unit: Unit, *, skill_name: Callable, skill_group: Callable,
) -> bool:
    """Only skip an A/B replay when this captured timing channel never ran.

    An absent interval field is *not* evidence of inactivity: older or
    partial schedules may reconstruct the effect from coverage/default data.
    """
    if not spec.get("same_environment_theory") or spec.get("partial_cast_schedule"):
        return False

    def matches(provider: object, skill: object) -> bool:
        return (str(provider or "").strip(), skill_group(skill_name(skill))) == unit

    timing_effects = [
        effect for effect in spec.get("effects", ())
        if matches(effect.get("provider"), effect.get("skill"))
        and any(timing_attribute(key) for key in (
            *effect.get("attrs", {}), *effect.get("effective_attrs", {}),
        ))
    ]
    if not timing_effects or any(
        "_score_recipient_active_intervals" not in effect
        or effect["_score_recipient_active_intervals"]
        for effect in timing_effects
    ):
        return False
    matching_schedules = [
        casts for key, casts in spec.get("cast_schedule", {}).items()
        if isinstance(key, (tuple, list)) and len(key) >= 2
        and matches(key[0], key[1])
    ]
    return bool(matching_schedules) and all(not casts for casts in matching_schedules)


def without_timing_unit(
    spec: dict[str, Any], unit: Unit, *, skill_name: Callable, skill_group: Callable,
) -> dict[str, Any] | None:
    """Clone only changed inputs; None means this recipient has no such effect."""
    def effect_unit(provider: object, skill: object) -> Unit:
        return str(provider or "").strip(), skill_group(skill_name(skill))

    effects = []
    removed = False
    for effect in spec.get("effects", ()):
        attrs = effect.get("attrs", {})
        effective = effect.get("effective_attrs", {})
        if effect_unit(effect.get("provider"), effect.get("skill")) != unit or not any(
            timing_attribute(key) for key in (*attrs, *effective)
        ):
            effects.append(effect)
            continue
        removed = True
        residual = copy.deepcopy(effect)
        residual["attrs"] = {key: value for key, value in attrs.items() if not timing_attribute(key)}
        residual["effective_attrs"] = {
            key: value for key, value in effective.items() if not timing_attribute(key)
        } or copy.deepcopy(residual["attrs"])
        if residual["attrs"] or residual["effective_attrs"]:
            skill = skill_name(effect.get("skill"))
            residual["skill"] = f"团队buff｜{skill}（仅非减CD属性）"
            residual["_score_residual_for"] = skill
            effects.append(residual)
    if not removed:
        return None
    # Retain the slot even when its only team effect was removed. Otherwise the
    # timeline engine can reconstruct the missing provider's default effects.
    if not any(str(effect.get("provider") or "").strip() == unit[0] for effect in effects):
        effects.append({
            "row": None, "provider": unit[0], "skill": "__反事实占位__", "level": "",
            "attrs": {}, "effective_attrs": {}, "cooldown": 0.0,
            "duration": 0.0, "coverage": 0.0,
        })
    result = dict(spec)
    result["effects"] = effects
    result["cast_schedule"] = {
        key: value for key, value in spec.get("cast_schedule", {}).items()
        if effect_unit(key[0], key[1]) != unit
    }
    # No repeated glyph selection, exclusion scoring, or large capture IPC.
    result.pop("score_exclusion_context", None)
    result.pop("score_actual_casts_by_source", None)
    result["include_display_windows"] = False
    return result


def _frozen_dict_items(pairs):
    if _EXACT_ROUND2:
        # The legacy order is repr((key, value)). Its key prefix normally
        # determines the order without formatting the (potentially huge)
        # value, again, at every nesting level. When prefixes can overlap,
        # retain the complete legacy comparison, including stable ties.
        decorated = [(repr(key) + ', ', index) for index, (key, _) in enumerate(pairs)]
        decorated.sort(key=lambda row: row[0])
        ambiguous = any(right[0].startswith(left[0])
                        for left, right in zip(decorated, decorated[1:]))
        ordered = (sorted(pairs, key=repr) if ambiguous
                   else [pairs[index] for _, index in decorated])
        return tuple(ordered)
    return tuple(sorted(pairs, key=repr))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return ('dict', _frozen_dict_items([(_freeze(k), _freeze(v)) for k, v in value.items()]))
    if isinstance(value, (tuple, list)):
        return ("sequence", tuple(_freeze(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_freeze(item) for item in value), key=repr)))
    return value


def retimed_unit(
    actual: dict[str, Any], candidate: dict[str, Any], unit: Unit,
    *, skill_name: Callable, skill_group: Callable,
) -> dict[str, Any]:
    """Change only this provider's timing channel, not other Buffs or panels.

    This is a conditional intervention, not a fixed-order timeline replay.
    The recipient engine is free to replan every damage action afterward.
    Cocktail's stat track remains exactly as observed when its CDR is retimed.
    """
    def matches(effect):
        return (str(effect.get('provider') or '').strip(),
                skill_group(skill_name(effect.get('skill')))) == unit

    before = [e for e in actual.get('effects', ()) if matches(e)]
    after = [e for e in candidate.get('effects', ()) if matches(e)]
    def schedules(spec):
        return {k: v for k, v in spec.get('cast_schedule', {}).items()
                if (str(k[0]).strip(), skill_group(skill_name(k[1]))) == unit}
    if before == after and schedules(actual) == schedules(candidate):
        return actual
    changed = without_timing_unit(actual, unit, skill_name=skill_name, skill_group=skill_group)
    if changed is None:
        return actual
    timing_effects = []
    for effect in after:
        timing = copy.deepcopy(effect)
        timing['attrs'] = {k: v for k, v in effect.get('attrs', {}).items() if timing_attribute(k)}
        timing['effective_attrs'] = {
            k: v for k, v in effect.get('effective_attrs', {}).items() if timing_attribute(k)
        } or copy.deepcopy(timing['attrs'])
        if timing['attrs'] or timing['effective_attrs']:
            timing_effects.append(timing)
    if not timing_effects:
        return actual
    remaining_effects = [e for e in changed['effects'] if not (
        str(e.get('provider') or '').strip() == unit[0]
        and e.get('skill') == '__反事实占位__'
    )]
    # Insertion is stable: effect ordering can participate in priority and
    # mutually exclusive Buff handling. Do not move the retimed provider
    # behind unrelated providers merely because its timing channel changed.
    original_index = next(i for i, e in enumerate(actual.get('effects', ())) if matches(e))
    preceding = actual['effects'][:original_index]
    insertion = sum(any(e is original for original in preceding) for e in remaining_effects)
    changed['effects'] = remaining_effects[:insertion] + timing_effects + remaining_effects[insertion:]
    changed['cast_schedule'].update(schedules(candidate))
    return changed


def _merged_intervals(rows: Any, *, horizon: float | None = None) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            start = float(row[0])
            end = float(row[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start = max(0.0, start)
        if horizon is not None:
            end = min(max(0.0, float(horizon)), end)
        if end > start + 1e-9:
            intervals.append((start, end))
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _cooldown_ready_from_work(
    start: float,
    remaining_work: float,
    cdr_windows: list[tuple[float, float, float, str]],
) -> float:
    """Mirror the timeline engine's piecewise cooldown-work integration."""

    current = max(0.0, float(start))
    remaining = max(0.0, float(remaining_work))
    if remaining <= 1e-12:
        return current
    boundaries = sorted({
        point
        for left, right, _value, _source in cdr_windows
        for point in (max(current, left), right)
        if point > current + 1e-9
    })

    def speed_at(second: float) -> float:
        by_source: dict[str, float] = {}
        for left, right, value, source in cdr_windows:
            if left <= second < right:
                by_source[source] = max(by_source.get(source, 0.0), value)
        cdr = min(70.0, max(0.0, sum(by_source.values())))
        return 1.0 / max(0.3, 1.0 - cdr / 100.0)

    for boundary in boundaries:
        speed = speed_at(current + max(1e-6, (boundary - current) / 2.0))
        progressed = max(0.0, boundary - current) * speed
        if progressed >= remaining - 1e-9:
            return current + remaining / speed
        remaining -= progressed
        current = boundary
    return current + remaining / speed_at(current + 1e-6)


def _glyph_cdr_for(
    rows: list[dict[str, Any]],
    skill: str,
    *,
    skill_name: Callable,
    skill_group: Callable,
) -> float:
    value = 0.0
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        if skill_group(skill_name(row.get("skill"))) != skill:
            continue
        effect = str(row.get("effect") or "").strip()
        if "冷却缩减" not in effect and "冷却减少" not in effect:
            continue
        try:
            candidate = float(row.get("value") or 0.0)
        except (TypeError, ValueError):
            candidate = 0.0
        if math.isfinite(candidate):
            value = max(value, candidate)
    return min(100.0, max(0.0, value))


def _effect_cdr_windows(
    spec: dict[str, Any],
    *,
    skill_name: Callable,
    skill_group: Callable,
) -> list[tuple[float, float, float, str]]:
    """Return recipient-authoritative CDR windows grouped by effect source."""

    horizon = max(0.0, float(spec.get("window") or 0.0)) or None
    schedule = spec.get("cast_schedule", {})
    windows: list[tuple[float, float, float, str]] = []
    for effect in spec.get("effects", ()):
        attrs = effect.get("attrs", {})
        if not isinstance(attrs, dict):
            continue
        values = []
        for key, raw_value in attrs.items():
            name = str(key or "").strip()
            if "冷却" not in name or not timing_attribute(name):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 1e-9:
                values.append(value)
        if not values:
            continue
        provider = str(effect.get("provider") or "").strip()
        group = skill_group(skill_name(effect.get("skill")))
        if not provider or not group:
            continue
        value = min(70.0, max(values))
        source_key = f"{provider}\x1f{group}"
        if "_score_recipient_active_intervals" in effect:
            active = _merged_intervals(
                effect.get("_score_recipient_active_intervals", ()),
                horizon=horizon,
            )
        else:
            duration = max(0.0, float(effect.get("duration") or 0.0))
            casts = schedule.get((provider, group), ()) if isinstance(schedule, dict) else ()
            active = _merged_intervals(
                [
                    (float(row[1]), float(row[1]) + duration)
                    for row in casts
                    if isinstance(row, (list, tuple)) and len(row) >= 2
                ],
                horizon=horizon,
            )
        windows.extend((start, end, value, source_key) for start, end in active)
    return windows


def _shift_recipient_intervals(
    intervals: Any,
    old_casts: list[tuple[float, float]],
    new_casts: list[tuple[float, float]],
    duration: float,
    *,
    horizon: float,
    clear_times: Any,
) -> list[tuple[float, float]]:
    """Move only the captured portion attributable to each delayed cast."""

    observed = _merged_intervals(intervals, horizon=horizon)
    if not observed or len(old_casts) != len(new_casts):
        return observed
    shifted: list[tuple[float, float]] = []
    for left, right in observed:
        matched = False
        for old, new in zip(old_casts, new_casts):
            old_end = float(old[1])
            active_end = old_end + max(0.0, duration)
            piece_start = max(left, old_end)
            piece_end = min(right, active_end)
            if piece_end <= piece_start + 1e-9:
                continue
            delay = float(new[1]) - old_end
            shifted.append((piece_start + delay, piece_end + delay))
            matched = True
        if matched:
            continue
        # A status already active at the local origin may have been cast before
        # the selected Boss window.  Preserve it because its cooldown history is
        # outside the counterfactual horizon and cannot be reconstructed.
        preceding = [
            index for index, cast in enumerate(old_casts)
            if float(cast[1]) <= left + 1e-6
        ]
        if preceding:
            index = preceding[-1]
            delay = float(new_casts[index][1]) - float(old_casts[index][1])
            shifted.append((left + delay, right + delay))
        else:
            shifted.append((left, right))

    clears = sorted({
        float(value) for value in clear_times if isinstance(value, (int, float))
        and math.isfinite(float(value)) and 0.0 < float(value) < horizon
    }) if isinstance(clear_times, (list, tuple)) else []
    clipped: list[tuple[float, float]] = []
    for start, end in shifted:
        start = max(0.0, start)
        end = min(horizon, end)
        for clear in clears:
            if start + 1e-9 < clear < end - 1e-9:
                end = clear
                break
        if end > start + 1e-9:
            clipped.append((start, end))
    return _merged_intervals(clipped, horizon=horizon)


def retime_linked_support_specs(
    specs: dict[str, dict[str, Any]],
    unit: Unit,
    *,
    skill_name: Callable,
    skill_group: Callable,
    glyphs_by_source: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Delay downstream support casts made illegal by one timing intervention.

    ``specs`` may represent the accelerator-absent B axis or its legally retimed
    H candidate.  Provider intent comes from the captured cast schedule; only
    cooldown-illegal starts move.  The exact recipient damage rotation is still
    recalculated later by the normal timeline workers.
    """

    if not specs:
        return {}, {"changed_skill_count": 0, "delayed_cast_count": 0, "skills": []}

    def normalized_unit(provider: object, skill: object) -> Unit:
        return str(provider or "").strip(), skill_group(skill_name(skill))

    owner_source_by_provider = {
        str(spec.get("provider_override") or "").strip(): source
        for source, spec in specs.items()
        if str(spec.get("provider_override") or "").strip()
    }
    plans: dict[Unit, dict[str, Any]] = {}
    for provider, owner_source in owner_source_by_provider.items():
        owner = specs.get(owner_source, {})
        schedule = owner.get("cast_schedule", {})
        if not isinstance(schedule, dict):
            continue
        effects_by_unit: dict[Unit, list[dict[str, Any]]] = {}
        for effect in owner.get("effects", ()):
            effect_unit = normalized_unit(effect.get("provider"), effect.get("skill"))
            effects_by_unit.setdefault(effect_unit, []).append(effect)
        cdr_windows = _effect_cdr_windows(
            owner, skill_name=skill_name, skill_group=skill_group
        )
        for raw_key, raw_casts in schedule.items():
            if not isinstance(raw_key, tuple) or len(raw_key) < 2:
                continue
            key = normalized_unit(raw_key[0], raw_key[1])
            if key[0] != provider or key == unit:
                continue
            effect_rows = effects_by_unit.get(key, ())
            if not effect_rows or any(
                any(timing_attribute(attr) for attr in effect.get("attrs", {}))
                for effect in effect_rows
            ):
                # Acceleration-resistance skills keep their own captured intent;
                # the game explicitly prevents these skills accelerating one
                # another or themselves.
                continue
            cooldown = max(
                (max(0.0, float(effect.get("cooldown") or 0.0)) for effect in effect_rows),
                default=0.0,
            )
            duration = max(
                (max(0.0, float(effect.get("duration") or 0.0)) for effect in effect_rows),
                default=0.0,
            )
            casts = [
                (float(row[0]), float(row[1]))
                for row in raw_casts if isinstance(row, (list, tuple)) and len(row) >= 2
                and float(row[1]) > float(row[0]) + 1e-9
            ]
            casts.sort()
            if cooldown <= 1e-9 or duration <= 1e-9 or len(casts) < 2:
                continue
            glyph_cdr = _glyph_cdr_for(
                glyphs_by_source.get(owner_source, []), key[1],
                skill_name=skill_name, skill_group=skill_group,
            )
            cooldown_work = cooldown * max(0.0, 1.0 - glyph_cdr / 100.0)
            retimed = [casts[0]]
            delays = [0.0]
            for intent_start, intent_end in casts[1:]:
                ready = _cooldown_ready_from_work(
                    retimed[-1][0], cooldown_work, cdr_windows
                )
                start = max(intent_start, ready)
                cast_duration = max(0.001, intent_end - intent_start)
                retimed.append((start, start + cast_duration))
                delays.append(max(0.0, start - intent_start))
            if not any(delay > 1e-6 for delay in delays):
                continue
            plans[key] = {
                "provider": provider,
                "skill": key[1],
                "cooldown": cooldown,
                "glyph_cdr": glyph_cdr,
                "duration": duration,
                "old_casts": casts,
                "new_casts": retimed,
                "delays": delays,
            }

    if not plans:
        return specs, {"changed_skill_count": 0, "delayed_cast_count": 0, "skills": []}

    result: dict[str, dict[str, Any]] = {}
    for source, spec in specs.items():
        changed = dict(spec)
        schedule = dict(spec.get("cast_schedule", {}))
        for raw_key in list(schedule):
            key = normalized_unit(raw_key[0], raw_key[1])
            if key in plans:
                schedule[raw_key] = list(plans[key]["new_casts"])
        changed["cast_schedule"] = schedule
        horizon = max(0.0, float(spec.get("window") or 0.0))
        changed_effects: list[dict[str, Any]] = []
        for effect in spec.get("effects", ()):
            key = normalized_unit(effect.get("provider"), effect.get("skill"))
            plan = plans.get(key)
            if plan is None or "_score_recipient_active_intervals" not in effect:
                changed_effects.append(effect)
                continue
            copied = copy.deepcopy(effect)
            copied["_score_recipient_active_intervals"] = _shift_recipient_intervals(
                effect.get("_score_recipient_active_intervals", ()),
                plan["old_casts"], plan["new_casts"], plan["duration"],
                horizon=horizon,
                clear_times=spec.get("player_buff_clear_times", ()),
            )
            changed_effects.append(copied)
        changed["effects"] = changed_effects
        result[source] = changed

    diagnostics = {
        "changed_skill_count": len(plans),
        "delayed_cast_count": sum(
            sum(delay > 1e-6 for delay in plan["delays"])
            for plan in plans.values()
        ),
        "skills": [
            {
                "provider": plan["provider"],
                "skill": plan["skill"],
                "base_cooldown": plan["cooldown"],
                "glyph_cdr_percent": plan["glyph_cdr"],
                "cast_count": len(plan["old_casts"]),
                "delayed_cast_count": sum(delay > 1e-6 for delay in plan["delays"]),
                "total_delay_seconds": sum(plan["delays"]),
                "max_delay_seconds": max(plan["delays"], default=0.0),
                "original_starts": [row[0] for row in plan["old_casts"]],
                "retimed_starts": [row[0] for row in plan["new_casts"]],
            }
            for _key, plan in sorted(plans.items())
        ],
    }
    return result, diagnostics


def _spec_key(spec: dict[str, Any], glyphs: list[dict[str, Any]], *, _build_cache=None) -> bytes:
    # Exclusion metadata is not an input to the axis. Keep all other flags,
    # especially score_refinement: a different optimizer is NOT a cache hit.
    inputs = {key: value for key, value in spec.items() if key not in {
        "score_exclusion_context", "support_only_ideal_timing",
        # Actual-cast replay is post-processing, after the simulated DPS has
        # already been calculated. It is not an input to a timing-only result.
        "score_actual_casts_by_source", "_timing_ablation_source",
    }}
    inputs["include_display_windows"] = False
    # Canonicalize only recipient intervals for which the engine explicitly
    # consumes authoritative status windows. Preserve ordering, duplicates,
    # provider identity, attrs, and every other input: overlaps can stack and
    # equal total uptime alone is NOT evidence of equivalent rotations.
    window = spec.get("window")
    if isinstance(window, (int, float)) and math.isfinite(window) and window >= 30:
        effects = []
        for effect in inputs.get("effects", ()):
            rows = effect.get("_score_recipient_active_intervals")
            if isinstance(rows, (list, tuple)) and all(
                isinstance(row, (list, tuple)) and len(row) == 2
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in row)
                for row in rows
            ):
                clipped = []
                for start, end in rows:
                    start = max(0.0, start)
                    end = min(window, max(start, end))
                    if end > start + 1e-6:
                        clipped.append((start, end))
                effect = dict(effect, _score_recipient_active_intervals=clipped)
            effects.append(effect)
        inputs["effects"] = effects
    if _EXACT_ROUND2 and _build_cache is not None and isinstance(inputs.get('build_override'), dict):
        build = inputs['build_override']
        remembered = _build_cache.get(id(build))
        if remembered is None or remembered[0] is not build:
            remembered = (build, _freeze(build))
            _build_cache[id(build)] = remembered
        # Only the immutable build subtree is shared by these counterfactual
        # specs. Effects, schedules, clear times, glyphs and all other fields
        # are still frozen afresh. Produce the same tree/bytes as _freeze.
        frozen_inputs = ('dict', _frozen_dict_items([
            (_freeze(k), remembered[1] if k == 'build_override' else _freeze(v))
            for k, v in inputs.items()
        ]))
        frozen = ('sequence', (frozen_inputs, _freeze(glyphs)))
    else:
        frozen = _freeze((inputs, glyphs))
    return hashlib.sha256(pickle.dumps(frozen, protocol=5)).digest()


def _dps(result: object) -> float:
    value = result.get("dps") if isinstance(result, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise TimingAblationError("辅助移除对照轴缺少有效 DPS，不能生成该次评分")
    return float(value)


def calculate_timing_ablations(
    *, actual_specs: dict[str, dict[str, Any]], theory_specs: dict[str, dict[str, Any]],
    actual_results: dict[str, dict[str, Any]], theory_results: dict[str, dict[str, Any]],
    units: tuple[Unit, ...], skill_name: Callable, skill_group: Callable,
    glyphs_by_source: dict[str, list[dict[str, Any]]], apply_glyphs: Callable,
    run_parallel: Callable, batch_size: int, progress_callback: Callable | None = None,
    seed_specs: dict[str, dict[str, Any]] | None = None,
    seed_results: dict[str, dict[str, Any]] | None = None,
    group_by_recipient: bool = False,
) -> tuple[dict[Unit, dict[str, float]], dict[str, Any]]:
    """Return raw A-A(-i)/T-T(-i) DPS, without coverage weighting or capping.

    Reuse exact A/T/N inputs and identical ablations, but never cache an entire
    capture globally. At most one bounded batch of detailed results is retained.
    Results and all source-keyed inputs are checked before calculating a loss.
    """
    started = time.perf_counter()
    if not units:
        return {}, {"method": "leave_one_out_timing_replan", "task_count": 0, "seconds": 0.0}
    sources = tuple(actual_specs)
    if not sources or any(set(rows) != set(sources) for rows in (
        theory_specs, actual_results, theory_results,
    )):
        raise TimingAblationError("辅助移除对照轴的角色与 A/T 轴不一致")
    cache: dict[bytes, float] = {}
    # Scoped to one calculation; no caller can edit a build between calls.
    frozen_builds = {} if _EXACT_ROUND2 else None
    phase_inputs = {
        "A": (actual_specs, actual_results), "T": (theory_specs, theory_results),
    }
    for specs, results in (*phase_inputs.values(), (seed_specs or {}, seed_results or {})):
        for source, spec in specs.items():
            if source in results:
                cache[_spec_key(spec, glyphs_by_source.get(source, []), _build_cache=frozen_builds)] = _dps(results[source])
    pending: dict[bytes, tuple[dict[str, Any], str]] = {}
    refs: dict[tuple[Unit, str, str], bytes | float] = {}
    reused = 0
    for unit in sorted(set(units)):
        for phase, (specs, results) in phase_inputs.items():
            for source in sources:
                changed = without_timing_unit(specs[source], unit, skill_name=skill_name, skill_group=skill_group)
                if changed is None:
                    refs[(unit, phase, source)] = _dps(results[source])
                    reused += 1
                    continue
                key = _spec_key(changed, glyphs_by_source.get(source, []), _build_cache=frozen_builds)
                refs[(unit, phase, source)] = key
                if key in cache or key in pending:
                    reused += 1
                else:
                    pending[key] = changed, source
    tasks = list(pending.items())
    batch_size = max(1, min(16, int(batch_size)))
    # Submit all variants of one recipient together. The timeline executor
    # runs that group in one worker; only immutable prepared inputs/caches are
    # reused, never a previous axis's mutable cooldown/resource/summon state.
    if group_by_recipient:
        by_source: dict[str, list[Any]] = {}
        for task in tasks:
            by_source.setdefault(task[1][1], []).append(task)
        groups = list(by_source.values())
        # Submit every recipient to the shared queue at once.  The process pool
        # already enforces ``batch_size``/the negotiated memory limit.  Splitting
        # recipients into synchronous waves made all workers wait for the
        # slowest profession in the current wave before any work from the next
        # wave could start.  Keeping recipient order here still preserves warm
        # input locality; the lower-level scheduler may distribute individual
        # axes whenever a worker becomes idle.
        batches = [[task for group in groups for task in group]] if tasks else []
    else:
        batches = [tasks[start:start + batch_size] for start in range(0, len(tasks), batch_size)]
    completed = 0
    for batch in batches:

        def progress(count: int, _index: int, job: str) -> None:
            if progress_callback is not None:
                progress_callback(completed + count, len(tasks), job)

        def heartbeat(count: int, _total: int, job: str) -> None:
            if progress_callback is not None:
                progress_callback(completed + count, len(tasks), job)

        parallel_kwargs = {"progress_callback": progress}
        if progress_callback is not None:
            parallel_kwargs["heartbeat_callback"] = heartbeat
        results = run_parallel([
            dict(row[1][0], timing_ablation_glyphs=glyphs_by_source.get(row[1][1], []),
                 **({"_timing_ablation_source": row[1][1]} if group_by_recipient else {}))
            for row in batch
        ], **parallel_kwargs)
        if len(results) != len(batch):
            raise TimingAblationError("辅助移除对照轴结果不完整，不能使用部分结果评分")
        for (key, (_spec, source)), result in zip(batch, results):
            _dps(result)
            # Use exactly the glyphs chosen on A, never re-select after removal.
            if not result.get("_timing_glyphs_applied"):
                apply_glyphs(result, glyphs_by_source.get(source, []))
            cache[key] = _dps(result)
        completed += len(batch)
        del results
    baseline_totals = {phase: math.fsum(_dps(results[source]) for source in sources)
                       for phase, (_specs, results) in phase_inputs.items()}
    measurements = {}
    for unit in sorted(set(units)):
        row = {}
        for phase, prefix in (("A", "actual"), ("T", "theory")):
            values = [refs[(unit, phase, source)] for source in sources]
            without = math.fsum(cache[value] if isinstance(value, bytes) else value for value in values)
            row[prefix + "_without_team_dps"] = without
            row[prefix + "_marginal_dps"] = baseline_totals[phase] - without
        measurements[unit] = row
    return measurements, {
        "method": "leave_one_out_timing_replan", "axis_groups": 2 * len(set(units)),
        "recipient_comparisons": len(refs), "task_count": len(tasks), "reused_count": reused,
        "batch_size": batch_size, "seconds": time.perf_counter() - started,
        "group_by_recipient": group_by_recipient,
        "schedule_policy": "hold_other_provider_casts_fixed_replan_recipient_rotations",
        "gain_policy": "non_additive_leave_one_out_marginals",
    }


def calculate_conditional_timing(
    *, actual_specs, actual_results, units, candidate_factory, skill_name, skill_group,
    glyphs_by_source, apply_glyphs, run_parallel, batch_size, progress_callback=None,
    include_linked_diagnostics=True, include_resistance_diagnostics=True,
    diagnostic_runner_out=None, prefetched_absent_results=None,
    prefetched_retimed_results=None,
):
    """Evaluate direct B/H axes plus downstream-support linked L/HL axes.

    Reuse B_i as the neutral timing-search objective for this provider instead
    of computing another whole-party N axis. Other providers stay observed.
    A is a whole-team candidate, and A/B_i/H_i share the same search budget.
    L preserves those other providers' captured cast intent, delaying only casts
    that become cooldown-illegal without i. HL performs the same legality pass
    under H_i, so an optimized accelerator cannot receive free impossible Buff
    coverage either.
    """
    started = time.perf_counter()
    sources = tuple(actual_specs)
    if not sources or set(actual_results) != set(sources):
        raise TimingAblationError('辅助对照轴角色不一致')
    frozen_builds = {} if _EXACT_ROUND2 else None
    cache = {_spec_key(s, glyphs_by_source.get(k, []), _build_cache=frozen_builds): actual_results[k]
             for k, s in actual_specs.items()}
    actual_dps = math.fsum(_dps(actual_results[s]) for s in sources)
    task_count = reused = 0
    prefetched_task_count = 0
    unit_set = sorted(set(units))
    resistance_units = {
        group: [unit for unit in unit_set if unit[1] in names]
        for group, names in _ACCELERATION_RESISTANCE_GROUP_SKILLS.items()
    }
    group1_units = resistance_units["group1_haste_resistance"]
    group2_units = resistance_units["group2_cooldown_resistance"]
    # Formal scoring needs only B/H per accelerator. L/HL linked-support axes
    # and resistance-group coalitions are display diagnostics, so callers may
    # defer those until after the score is visible. The upper bound below must
    # describe only the work performed by this invocation; otherwise the UI
    # appears to stall below completion even after every scoring axis finished.
    coalition_axis_upper = (
        (2 if len(group1_units) == len(group2_units) == 1 else 6) * len(sources)
        if include_resistance_diagnostics and group1_units and group2_units else 0
    )
    total_upper = (
        2 * len(unit_set) * len(sources)
        + (2 * len(unit_set) * len(sources) if include_linked_diagnostics else 0)
        + coalition_axis_upper
    )

    def execute(requests):
        nonlocal task_count, reused
        refs, pending = {}, {}
        for ref, (source, spec) in requests.items():
            key = _spec_key(spec, glyphs_by_source.get(source, []), _build_cache=frozen_builds)
            refs[ref] = key
            if key in cache or key in pending:
                reused += 1
            else:
                pending[key] = source, spec
        grouped = {}
        for key, (source, spec) in pending.items():
            grouped.setdefault(source, []).append((key, spec))
        # All recipients in this phase are independent. The executor already
        # bounds workers and memory; imposing worker-sized synchronous waves
        # here leaves idle workers waiting for the slowest recipient in each
        # wave. Keep each recipient's variants adjacent for prepared-input
        # reuse, but let the shared queue continuously feed available workers.
        tasks = [(key, source, spec) for source, rows in grouped.items()
                 for key, spec in rows]
        if tasks:
            def progress(done, _index, job):
                if progress_callback:
                    progress_callback(
                        prefetched_task_count + task_count + done,
                        total_upper,
                        job,
                    )

            def heartbeat(done, _total, job):
                if progress_callback:
                    progress_callback(
                        prefetched_task_count + task_count + done,
                        total_upper,
                        job,
                    )

            parallel_kwargs = {"progress_callback": progress}
            if progress_callback is not None:
                parallel_kwargs["heartbeat_callback"] = heartbeat
            results = run_parallel([
                dict(spec, timing_ablation_glyphs=glyphs_by_source.get(source, []),
                     _timing_ablation_source=source) for _, source, spec in tasks
            ], **parallel_kwargs)
            if len(results) != len(tasks):
                raise TimingAblationError('辅助对照轴不完整')
            for (key, source, _), result in zip(tasks, results):
                _dps(result)
                if not result.get('_timing_glyphs_applied'):
                    apply_glyphs(result, glyphs_by_source.get(source, []))
                _dps(result)
                cache[key] = result
            task_count += len(tasks)
        return {ref: cache[key] for ref, key in refs.items()}

    absent_requests = {}
    absent_specs_by_unit = {}
    for unit in unit_set:
        absent_specs_by_unit[unit] = {}
        for source in sources:
            spec = without_timing_unit(actual_specs[source], unit, skill_name=skill_name, skill_group=skill_group)
            resolved = spec if spec is not None else actual_specs[source]
            absent_specs_by_unit[unit][source] = resolved
            absent_requests[(unit, source)] = source, resolved
    # B axes can be generated as soon as their corresponding A baseline
    # finishes.  The caller pipelines those exact specs through otherwise idle
    # workers while the long-tail baseline professions are still running.
    # Seed by the same full spec+glyph key used by ``execute``; a stale or
    # mismatched prefetch therefore cannot alter a score and is simply ignored.
    if isinstance(prefetched_absent_results, dict):
        for ref, result in prefetched_absent_results.items():
            if (
                not isinstance(ref, tuple)
                or len(ref) != 2
                or ref[0] not in absent_specs_by_unit
                or ref[1] not in actual_specs
                or not isinstance(result, dict)
            ):
                continue
            unit, source = ref
            spec = absent_specs_by_unit[unit][source]
            key = _spec_key(spec, glyphs_by_source.get(source, []), _build_cache=frozen_builds)
            if key in cache:
                continue
            _dps(result)
            if not result.get('_timing_glyphs_applied'):
                apply_glyphs(result, glyphs_by_source.get(source, []))
            cache[key] = result
            prefetched_task_count += 1
        if prefetched_task_count and progress_callback:
            progress_callback(
                prefetched_task_count,
                total_upper,
                "已预计算移除轴",
            )
    absent = execute(absent_requests)
    linked_absent_requests = {}
    linked_absent_diagnostics = {}
    if include_linked_diagnostics:
        for unit in unit_set:
            linked_specs, linked_diagnostics = retime_linked_support_specs(
                absent_specs_by_unit[unit], unit,
                skill_name=skill_name, skill_group=skill_group,
                glyphs_by_source=glyphs_by_source,
            )
            linked_absent_diagnostics[unit] = linked_diagnostics
            for source in sources:
                linked_absent_requests[(unit, source)] = source, linked_specs[source]
        linked_absent = execute(linked_absent_requests)
    else:
        linked_absent = {}
    retimed_requests = {}
    retimed_specs_by_unit = {}
    for unit in unit_set:
        without_results = {source: absent[(unit, source)] for source in sources}
        candidates = candidate_factory(unit, without_results)
        if set(candidates) != set(sources):
            raise TimingAblationError('辅助改时机候选角色不完整')
        retimed_specs_by_unit[unit] = {}
        for source in sources:
            changed = retimed_unit(actual_specs[source], candidates[source], unit,
                                   skill_name=skill_name, skill_group=skill_group)
            retimed_specs_by_unit[unit][source] = changed
            retimed_requests[(unit, source)] = source, changed
    if isinstance(prefetched_retimed_results, dict):
        for ref, result in prefetched_retimed_results.items():
            if (
                not isinstance(ref, tuple)
                or len(ref) != 2
                or ref[0] not in retimed_specs_by_unit
                or ref[1] not in actual_specs
                or not isinstance(result, dict)
            ):
                continue
            unit, source = ref
            spec = retimed_specs_by_unit[unit][source]
            key = _spec_key(spec, glyphs_by_source.get(source, []), _build_cache=frozen_builds)
            if key in cache:
                continue
            _dps(result)
            if not result.get('_timing_glyphs_applied'):
                apply_glyphs(result, glyphs_by_source.get(source, []))
            cache[key] = result
            prefetched_task_count += 1
        if prefetched_task_count and progress_callback:
            progress_callback(
                prefetched_task_count,
                total_upper,
                "已预计算移除轴与改时机轴",
            )
    retimed = execute(retimed_requests)
    linked_retimed_requests = {}
    linked_retimed_diagnostics = {}
    if include_linked_diagnostics:
        for unit in unit_set:
            linked_specs, linked_diagnostics = retime_linked_support_specs(
                retimed_specs_by_unit[unit], unit,
                skill_name=skill_name, skill_group=skill_group,
                glyphs_by_source=glyphs_by_source,
            )
            linked_retimed_diagnostics[unit] = linked_diagnostics
            for source in sources:
                linked_retimed_requests[(unit, source)] = source, linked_specs[source]
        linked_retimed = execute(linked_retimed_requests)
    else:
        linked_retimed = {}

    # The game has two mutually independent acceleration-resistance groups.
    # A leave-one-out marginal is order-dependent when both groups coexist, so
    # also evaluate all four coalitions and expose an order-neutral Shapley
    # split.  This remains a diagnostic and does not change formal scores.
    resistance_group_shapley = None
    if include_resistance_diagnostics and group1_units and group2_units:
        def without_units(remove_units):
            result = {}
            for source in sources:
                current = actual_specs[source]
                for remove_unit in remove_units:
                    changed = without_timing_unit(
                        current,
                        remove_unit,
                        skill_name=skill_name,
                        skill_group=skill_group,
                    )
                    if changed is not None:
                        current = changed
                result[source] = current
            return result

        coalition_specs = {
            "group1_only": without_units(group2_units),
            "group2_only": without_units(group1_units),
            "neither": without_units((*group1_units, *group2_units)),
        }
        coalition_requests = {
            (coalition, source): (source, specs[source])
            for coalition, specs in coalition_specs.items()
            for source in sources
        }
        coalition_fixed = execute(coalition_requests)
        coalition_linked_specs = {}
        coalition_schedule_diagnostics = {}
        for coalition, specs in coalition_specs.items():
            linked_specs, schedule_diagnostics = retime_linked_support_specs(
                specs,
                ("__acceleration_resistance_group__", "__removed__"),
                skill_name=skill_name,
                skill_group=skill_group,
                glyphs_by_source=glyphs_by_source,
            )
            coalition_linked_specs[coalition] = linked_specs
            coalition_schedule_diagnostics[coalition] = schedule_diagnostics
        coalition_linked = execute({
            (coalition, source): (source, specs[source])
            for coalition, specs in coalition_linked_specs.items()
            for source in sources
        })

        def team_value(rows, coalition):
            return math.fsum(_dps(rows[(coalition, source)]) for source in sources)

        fixed_values = {"both": actual_dps, **{
            coalition: team_value(coalition_fixed, coalition)
            for coalition in coalition_specs
        }}
        linked_values = {"both": actual_dps, **{
            coalition: team_value(coalition_linked, coalition)
            for coalition in coalition_specs
        }}

        def two_group_shapley(values):
            group1 = 0.5 * (
                (values["group1_only"] - values["neither"])
                + (values["both"] - values["group2_only"])
            )
            group2 = 0.5 * (
                (values["group2_only"] - values["neither"])
                + (values["both"] - values["group1_only"])
            )
            return {
                "group1_haste_resistance": group1,
                "group2_cooldown_resistance": group2,
            }

        fixed_shapley = two_group_shapley(fixed_values)
        total_shapley = two_group_shapley(linked_values)
        compact_schedule_diagnostics = {
            coalition: {
                "changed_skill_count": rows.get("changed_skill_count", 0),
                "delayed_cast_count": rows.get("delayed_cast_count", 0),
                "skills": [
                    {
                        key: skill_row.get(key)
                        for key in (
                            "provider", "skill", "base_cooldown",
                            "glyph_cdr_percent", "cast_count",
                            "delayed_cast_count", "total_delay_seconds",
                            "max_delay_seconds",
                        )
                    }
                    for skill_row in rows.get("skills", ())
                    if isinstance(skill_row, dict)
                ],
            }
            for coalition, rows in coalition_schedule_diagnostics.items()
        }
        resistance_group_shapley = {
            "method": "two_group_four_coalition_shapley",
            "included_in_score": False,
            "groups": {
                group: {
                    "units": [list(unit) for unit in resistance_units[group]],
                    "direct_dps": fixed_shapley[group],
                    "linked_dps": total_shapley[group] - fixed_shapley[group],
                    "total_dps": total_shapley[group],
                    "direct_percent": (
                        fixed_shapley[group] / actual_dps * 100.0
                        if actual_dps > 1e-9 else 0.0
                    ),
                    "linked_percent": (
                        (total_shapley[group] - fixed_shapley[group])
                        / actual_dps * 100.0
                        if actual_dps > 1e-9 else 0.0
                    ),
                    "total_percent": (
                        total_shapley[group] / actual_dps * 100.0
                        if actual_dps > 1e-9 else 0.0
                    ),
                }
                for group in resistance_units
            },
            "fixed_schedule_team_dps": fixed_values,
            "linked_schedule_team_dps": linked_values,
            # Keep score payloads small. Full before/after cast timestamps stay
            # in the per-skill offline diagnostic, not the live response.
            "linked_schedule_diagnostics": compact_schedule_diagnostics,
        }
    measurements = {}
    for unit in unit_set:
        b = math.fsum(_dps(absent[(unit, source)]) for source in sources)
        h = math.fsum(_dps(retimed[(unit, source)]) for source in sources)
        linked_b = (
            math.fsum(_dps(linked_absent[(unit, source)]) for source in sources)
            if include_linked_diagnostics else b
        )
        linked_h = (
            math.fsum(_dps(linked_retimed[(unit, source)]) for source in sources)
            if include_linked_diagnostics else h
        )
        observed_wins = actual_dps >= h
        chosen = actual_dps if observed_wins else h
        linked_observed_wins = actual_dps >= linked_h
        linked_chosen = actual_dps if linked_observed_wins else linked_h
        actual_direct = actual_dps - b
        actual_linked = b - linked_b
        actual_total = actual_dps - linked_b
        theory_direct = chosen - b
        theory_total = linked_chosen - linked_b
        measurements[unit] = dict(
            method='conditional_timing_replan', actual_without_team_dps=b, theory_without_team_dps=b,
            actual_marginal_dps=actual_direct, theory_marginal_dps=theory_direct,
            actual_direct_marginal_dps=actual_direct,
            actual_linked_marginal_dps=actual_linked,
            actual_total_marginal_dps=actual_total,
            theory_direct_marginal_dps=theory_direct,
            theory_linked_marginal_dps=theory_total - theory_direct,
            theory_total_marginal_dps=theory_total,
            linked_without_team_dps=linked_b,
            linked_retimed_candidate_team_dps=linked_h,
            linked_selected_candidate=(
                'observed' if linked_observed_wins else 'retimed_provider'
            ),
            linked_absent_schedule=linked_absent_diagnostics.get(unit, {}),
            linked_candidate_schedule=linked_retimed_diagnostics.get(unit, {}),
            linked_diagnostics_complete=bool(include_linked_diagnostics),
            linked_contribution_included_in_score=False,
            observed_team_dps=actual_dps, retimed_candidate_team_dps=h,
            selected_candidate='observed' if observed_wins else 'retimed_provider',
            lost_opportunity_dps=chosen-actual_dps,
        )
    if (
        not include_linked_diagnostics or not include_resistance_diagnostics
    ) and isinstance(diagnostic_runner_out, list):
        # The runner intentionally stays in the parent process. It retains the
        # immutable prepared score inputs and is consumed once by the
        # post-score low-priority job; it is never serialized into a score plan
        # or uploaded with a capture.
        def finish_diagnostics(deferred_progress_callback=None):
            return calculate_conditional_timing(
                actual_specs=actual_specs,
                actual_results=actual_results,
                units=units,
                candidate_factory=candidate_factory,
                skill_name=skill_name,
                skill_group=skill_group,
                glyphs_by_source=glyphs_by_source,
                apply_glyphs=apply_glyphs,
                run_parallel=run_parallel,
                batch_size=batch_size,
                progress_callback=deferred_progress_callback,
                include_linked_diagnostics=True,
                include_resistance_diagnostics=True,
            )

        diagnostic_runner_out.append(finish_diagnostics)

    return measurements, dict(method='conditional_timing_replan',
        task_count=task_count + prefetched_task_count,
        prefetched_task_count=prefetched_task_count,
        reused_count=reused, seconds=time.perf_counter()-started,
        axis_groups=(2 + (2 if include_linked_diagnostics else 0))*len(unit_set),
        search_objective='own_absent_buff_axis', group_by_recipient=True,
        gain_policy='same_absent_buff_baseline_per_provider',
        score_schedule_policy='hold_other_provider_casts_fixed_replan_recipient_rotations',
        schedule_policy='preserve_other_support_cast_intent_delay_cooldown_illegal_casts',
        linked_contribution_included_in_score=False,
        linked_diagnostics_complete=bool(include_linked_diagnostics),
        resistance_diagnostics_complete=bool(include_resistance_diagnostics),
        diagnostics_deferred=bool(
            not include_linked_diagnostics or not include_resistance_diagnostics
        ),
        resistance_group_shapley=resistance_group_shapley)
