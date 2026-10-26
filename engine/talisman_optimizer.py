"""Exact, slot-aware offensive talisman search for an existing build.

No timeline or capture work is performed. Only an explicit build preset calls
this module. Equal-output defensive fillers are collapsed into capacity groups;
branch-and-bound removes layouts whose optimistic panel cannot beat the best.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from math import ceil, floor
from time import perf_counter
from threading import RLock
from typing import Any

from .calculator import BuildCalculator, FLAT_TARGETS, get_display_attr
from .data_loader import TALISMAN_SLOTS, TALISMAN_SLOT_PERCENT, as_number, norm
from .effect_stacking import attack_increase_for_channel

_RESULT_CACHE_LOCK = RLock()
_RESULT_CACHE_SIZE = 32


def _attack_weights(calculator: BuildCalculator, build: dict[str, Any], *, fixed_core: bool = False):
    """Exact channel slopes with conservative bounds on integer rounding.

    This enables output-dominance comparisons between physical/magical extras
    without treating the two independent channel maxima as simultaneously
    attainable. Mixed damage formulas/offensive source runes use the generic
    bound instead.
    """
    row = calculator.effective.get(norm(build.get("effective_second_job")) or norm(build.get("second_job")), {})
    channels = [i for i in range(1, 4) if as_number(row.get(f"权重{i}")) > 0]
    source, _ = calculator._split_conversion(norm(build.get("core_rune", {}).get("conversion")))
    if ((not fixed_core and source in {"力量", "敏捷", "智力", "物攻", "魔攻", "致命", "最终", "致命伤害"})
            or any(norm(row.get(f"攻击{i}")) not in {"物攻", "魔攻"} for i in channels)
            or len({as_number(row.get(f"额外最终{i}")) for i in channels}) > 1):
        return None
    baseline = copy.deepcopy(build)
    baseline["talismans"] = []
    local = calculator.fork_calculation_state()
    result, _ = local.calculate(baseline)
    pre = local._static_calculation_cache[local._static_build_signature(baseline)][1]
    buffs = local._buff_values(baseline)
    panel = {item.attr: item.buffed for item in result}
    for attr in ("最小物攻", "最大物攻", "最小魔攻", "最大魔攻", "最终", "致命", "致命伤害"):
        panel[attr] = 0.0
    job = norm(build.get("effective_second_job")) or norm(build.get("second_job"))
    zero = local._effective_panel(job, baseline, panel, buffs)["effective_panel"]
    output = []
    for attack in ("物攻", "魔攻"):
        increased = dict(panel)
        increased[f"最小{attack}"] = increased[f"最大{attack}"] = 1000.0
        slope = (local._effective_panel(job, baseline, increased, buffs)["effective_panel"] - zero) / 1000
        equipment = 1 + get_display_attr(pre, f"{attack}%") / 100
        buff = 1 + attack_increase_for_channel(buffs, attack) / 100
        if slope < 0 or equipment < 0 or buff < 0:
            return None
        output.append((equipment, slope * buff))
    return output


@dataclass
class _Group:
    families: list[list[dict[str, str]]]
    vectors: tuple[tuple[tuple[float, ...], ...], ...]
    priority: int


def optimize_talismans(
    calculator: BuildCalculator, build: dict[str, Any], profile: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    started = perf_counter()
    store = calculator.store
    main = norm(profile.get("main_stat"))
    attacks = {norm(value) for value in profile.get("attack_attrs", [profile.get("attack_attr")]) if norm(value) in {"物攻", "魔攻"}}
    # The current talismans are the output being replaced, not a search input.
    # Buffs, environment, free inputs, equipment and runes ARE search inputs.
    cache_key = json.dumps({"build": {key: value for key, value in build.items() if key != "talismans"},
                            "main": main, "attacks": sorted(attacks)},
                           ensure_ascii=False, sort_keys=True, default=str)
    with _RESULT_CACHE_LOCK:
        cached = calculator._talisman_result_cache.pop(cache_key, None)
        if cached is not None:
            calculator._talisman_result_cache[cache_key] = cached
    if cached is not None:
        result, info = copy.deepcopy(cached)
        checked = {**build, "talismans": result}
        actual_score = calculator.calculate(checked, summary_only=True)[1]["effective_panel"]
        if actual_score == info["after"]:
            info.update(before=calculator.calculate(build, summary_only=True)[1]["effective_panel"],
                        evaluations=0, nodes=0, cached=True,
                        elapsed_ms=round((perf_counter() - started) * 1000, 3))
            return result, info
    preferred = {main, *attacks, "最终", "致命伤害"}
    extras = {main, *attacks}
    source, _ = calculator._split_conversion(norm(build.get("core_rune", {}).get("conversion")))
    offensive_sources = {
        "力量", "敏捷", "智力", "物攻", "魔攻", "最终", "致命", "致命伤害",
        "最小物攻", "最大物攻", "最小魔攻", "最大魔攻",
    }
    baseline = copy.deepcopy(build)
    baseline["talismans"] = []
    core_preview = calculator.core_rune_preview(baseline)
    fixed_core = bool(core_preview.get("max_deduct", 0) > 0 and core_preview.get("source_value", 0) >= core_preview["max_deduct"])
    # Attack-source runes retain a positive part of that attack (ratio <=100)
    # and add a nonnegative gain; crit/final conversions can trade two multipliers.
    ratio = calculator._core_rune_parameters(build)[2]
    relaxed_core = not fixed_core and (source in {"力量", "敏捷", "智力", "最终", "致命", "致命伤害"} or ratio > 100)
    # Other attributes cannot affect equivalent output except as core sources.
    relevant = set(offensive_sources) - {"物攻", "魔攻"}
    relevant.update(FLAT_TARGETS.get(source, [source]) if source else [])
    if source in {"HP", "防御", "眩晕抵抗"}:
        relevant.add("体质")
    attrs = tuple(sorted(relevant))
    channel_weights = _attack_weights(calculator, build, fixed_core=fixed_core)
    attack_indices = [attrs.index(f"最小{attack}") for attack in ("物攻", "魔攻")]
    attack_keys = {"最小物攻", "最大物攻", "最小魔攻", "最大魔攻"}

    def dominates(left, right):
        if all(a >= b for va, vb in zip(left, right) for a, b in zip(va, vb)):
            return True
        if channel_weights is None:
            return False
        for va, vb in zip(left, right):
            if any(a < b for key, a, b in zip(attrs, va, vb) if key not in attack_keys):
                return False
            advantage = 0.0
            for index, (equipment, slope) in zip(attack_indices, channel_weights):
                delta = va[index] - vb[index]
                advantage += (floor(delta * equipment) if delta >= 0 else -ceil(-delta * equipment)) * slope
            if advantage < 0:
                return False
        return True
    families: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for grade in store.talisman_grades():
        for name in store.talisman_names(grade):
            main_rows = calculator.talisman.get((grade, name), [])
            main_attrs = tuple(sorted({norm(row.get("属性")) for row in main_rows}))
            if not main_attrs:
                continue
            for extra in store.talisman_extra_options(grade, name):
                extra_rows = calculator.talisman_extra.get((grade, extra), [])
                allowed_extra = bool(extra_rows) and all(norm(row.get("属性")) in extras for row in extra_rows)
                if extra != "无" and not allowed_extra:
                    continue
                if not (set(main_attrs) & preferred) and not allowed_extra:
                    continue
                families.setdefault(main_attrs, []).append({"grade": grade, "name": name, "extra": extra})

    groups: list[_Group] = []
    signatures: dict[Any, int] = {}
    for family, items in families.items():
        vector_items = []
        for item in items:
            slot_attrs = [calculator.talisman_attrs({**item, "slot": slot}) for slot in TALISMAN_SLOTS]
            vectors = tuple(tuple(values.get(attr, 0.0) for attr in attrs) for values in slot_attrs)
            vector_items.append((item, vectors))
        # Same family may be equipped only once, irrespective of grade/tier.
        kept = []
        for item_index, (item, vector) in enumerate(vector_items):
            if any(vector == old for _, old in kept):
                continue
            if not relaxed_core and any(
                other != vector and dominates(other, vector)
                and (not dominates(vector, other) or item_index > other_index)
                for other_index, (_, other) in enumerate(vector_items)
            ):
                continue
            kept.append((item, vector))
        kept.sort(key=lambda row: row[1], reverse=True)
        signature = tuple(vector for _, vector in kept)
        priority = 0 if set(family) & preferred else 1 if set(family) & relevant else 2
        key = (signature, priority)
        if key in signatures:
            groups[signatures[key]].families.append([item for item, _ in kept])
        else:
            signatures[key] = len(groups)
            groups.append(_Group([[item for item, _ in kept]], signature, priority))
    groups.sort(key=lambda group: group.priority)
    if sum(len(group.families) for group in groups) < len(TALISMAN_SLOTS):
        raise ValueError("符合职业属性的护符种类不足八种，无法自动配置")

    # Exchange proof, not a hardcoded list: when all fillers have identical
    # output vectors, a distinct offensive family that dominates every filler
    # can replace one without decreasing output. Include these families in an
    # optimum instead of enumerating redundant omissions/defensive duplicates.
    fillers = [group for group in groups if len(group.families) > 1]
    singletons = [i for i, group in enumerate(groups) if len(group.families) == 1]
    required: set[int] = set()
    if not relaxed_core and len(singletons) <= len(TALISMAN_SLOTS) and fillers:
        for index in singletons:
            if all(
                any(dominates(candidate, filler)
                    for candidate in groups[index].vectors)
                for group in fillers for filler in group.vectors
            ):
                required.add(index)

    # Independent calculator state: no eviction/contention in live panel caches.
    objective = calculator.prepare_talisman_objective(build)
    evaluations = 0

    @lru_cache(maxsize=32768)
    def score(values: tuple[float, ...], upper: bool = False, lower: tuple[float, ...] = ()) -> float:
        nonlocal evaluations
        evaluations += 1
        return objective(dict(zip(attrs, values)), relaxed_core=upper and relaxed_core, lower_totals=dict(zip(attrs, lower)))

    @lru_cache(maxsize=8192)
    def optimistic_remaining(depth: int, counts: tuple[int, ...]) -> tuple[float, ...]:
        multipliers = [TALISMAN_SLOT_PERCENT[slot] / 100 for slot in TALISMAN_SLOTS[depth:]]
        upper = []
        for attr_index in range(len(attrs)):
            available = []
            for index, group in enumerate(groups):
                # 100% values before slot scaling; rearrangement upper bound
                # ignores competition between attributes/extras, not capacities.
                amount = max(vector[-1][attr_index] for vector in group.vectors)
                available.extend([amount] * (len(group.families) - counts[index]))
            available.sort(reverse=True)
            upper.append(ceil(sum(a * b for a, b in zip(available, multipliers))))
        if channel_weights is not None:
            weights = [equipment * slope for equipment, slope in channel_weights]
            available = []
            for index, group in enumerate(groups):
                amount = max(sum(vector[-1][key] * weight for key, weight in zip(attack_indices, weights)) for vector in group.vectors)
                available.extend([amount] * (len(group.families) - counts[index]))
            available.sort(reverse=True)
            # Two possible rounding errors per channel (the feasible and
            # synthetic bound panels). Gains from sources/core stay unchanged.
            cap = sum(a * b for a, b in zip(available, multipliers)) + 2 * sum(slope for _, slope in channel_weights)
            excess = sum(upper[key] * weight for key, weight in zip(attack_indices, weights)) - cap
            for attack, key, weight in zip(("物攻", "魔攻"), attack_indices, weights):
                if excess <= 0 or weight <= 0:
                    continue
                deduction = min(upper[key], excess / weight)
                adjusted = ceil(upper[key] - deduction)
                removed = upper[key] - adjusted
                upper[key] = adjusted
                upper[attrs.index(f"最大{attack}")] -= removed
                excess -= removed * weight
        return tuple(upper)

    def choices(depth: int, counts: tuple[int, ...], last: tuple[int, int]):
        same_multiplier = depth > 0 and TALISMAN_SLOT_PERCENT[TALISMAN_SLOTS[depth]] == TALISMAN_SLOT_PERCENT[TALISMAN_SLOTS[depth - 1]]
        for index, group in enumerate(groups):
            if counts[index] >= len(group.families):
                continue
            for option, vector in enumerate(group.vectors):
                if same_multiplier and (index, option) < last:
                    continue
                yield index, option, vector[depth]

    zeros = (0.0,) * len(attrs)
    empty_counts = (0,) * len(groups)
    # Establish an incumbent immediately; exhaustive search then proves it.
    counts, totals, selected = empty_counts, zeros, []
    for depth in range(len(TALISMAN_SLOTS)):
        options = list(choices(depth, counts, (-1, -1)))
        missing = required - {i for i, count in enumerate(counts) if count}
        if len(missing) == len(TALISMAN_SLOTS) - depth:
            options = [row for row in options if row[0] in missing]
        index, option, vector = max(options, key=lambda row: (score(tuple(a + b for a, b in zip(totals, row[2]))), -groups[row[0]].priority, -row[0], -row[1]))
        selected.append((index, option))
        counts = tuple(count + (i == index) for i, count in enumerate(counts))
        totals = tuple(a + b for a, b in zip(totals, vector))
    best_score, best_layout = score(totals), selected
    nodes = 0
    seen: set[Any] = set()

    def search(depth: int, counts: tuple[int, ...], totals: tuple[float, ...], layout: list[tuple[int, int]]):
        nonlocal best_score, best_layout, nodes
        nodes += 1
        if depth == len(TALISMAN_SLOTS):
            value = score(totals)
            if value > best_score:
                best_score, best_layout = value, list(layout)
            return
        if sum(counts[index] == 0 for index in required) > len(TALISMAN_SLOTS) - depth:
            return
        last = layout[-1] if layout else (-1, -1)
        state = (depth, counts, totals, last)
        if state in seen:
            return
        # Bounded memoization does not alter exhaustiveness.
        if len(seen) < 100000:
            seen.add(state)
        bound = tuple(a + b for a, b in zip(totals, optimistic_remaining(depth, counts)))
        if score(bound, True, totals if relaxed_core else ()) <= best_score:
            return
        options = list(choices(depth, counts, last))
        options.sort(key=lambda row: score(tuple(a + b for a, b in zip(totals, row[2]))), reverse=True)
        for index, option, vector in options:
            following = tuple(count + (i == index) for i, count in enumerate(counts))
            search(depth + 1, following, tuple(a + b for a, b in zip(totals, vector)), [*layout, (index, option)])

    search(0, empty_counts, zeros, [])
    counts = [0] * len(groups)
    result = []
    for slot, (index, option) in zip(TALISMAN_SLOTS, best_layout):
        item = groups[index].families[counts[index]][option]
        result.append({"slot": slot, **item})
        counts[index] += 1
    checked = copy.deepcopy(build)
    checked["talismans"] = result
    actual_score = calculator.calculate(checked, summary_only=True)[1]["effective_panel"]
    if abs(actual_score - best_score) > max(1e-8, abs(best_score) * 1e-10):
        raise RuntimeError("护符优化结果与装备面板计算不一致，未应用配置")
    info = {
        "before": calculator.calculate(build, summary_only=True)[1]["effective_panel"],
        "after": actual_score, "evaluations": evaluations, "nodes": nodes,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "main_stat": main, "attack_attrs": sorted(attacks), "optimal": True, "cached": False,
    }
    with _RESULT_CACHE_LOCK:
        calculator._talisman_result_cache[cache_key] = (copy.deepcopy(result), copy.deepcopy(info))
        while len(calculator._talisman_result_cache) > _RESULT_CACHE_SIZE:
            calculator._talisman_result_cache.pop(next(iter(calculator._talisman_result_cache)))
    return result, info
