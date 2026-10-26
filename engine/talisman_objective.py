"""Compile invariant rules for the exact talisman-only panel evaluator.

Every original rounding boundary is retained. Percentage-input changes use
the reference evaluator; the search itself adds individually truncated flats.
This is not an alternative scoring formula or a relaxed/approximate search.
"""
from __future__ import annotations

from math import trunc

from .calculator import (
    DISPLAY_TO_TARGET, ELEMENT_ATTRS, FLAT_TARGETS, INDEPENDENT_SOURCE_PERCENT_ATTRS,
    PERCENT_ATTR, SOURCE_ATTRS, as_number, get_attr, get_display_attr, norm,
)
from .effect_stacking import attack_increase_for_channel, skill_percent_amplified_flat_attack


def compile_objective(calculator, build, pre_base, mastery, mastery_exempt, buffs,
                      needed_attrs, conversion_targets, buff_attrs, core_parameters, reference):
    fixed_attrs = tuple(attr for attr in needed_attrs if not attr.endswith("%"))
    percent_attrs = tuple(attr for attr in needed_attrs if attr.endswith("%"))
    actual_rules = tuple((attr, 1 + get_display_attr(pre_base, PERCENT_ATTR.get(attr, "")) / 100)
                         for attr in fixed_attrs)
    job = calculator.store.job_family(norm(build.get("second_job")), norm(build.get("first_job")))
    conversions = tuple((tuple(display_attrs),
        tuple(calculator.conversion.get((job, source, target), 0.0) for source in SOURCE_ATTRS),
        tuple(1 + get_display_attr(pre_base, PERCENT_ATTR.get(attr, "")) / 100 for attr in display_attrs))
        for target, display_attrs in conversion_targets.items())
    source_buffs = []
    for attr in SOURCE_ATTRS:
        fixed = get_attr(buffs, attr)
        percent = get_attr(buffs, f"{attr}%")
        independent = sum(get_attr(buffs, key) for key in INDEPENDENT_SOURCE_PERCENT_ATTRS.get(attr, ()))
        if abs(fixed) >= 1e-9 or abs(percent) >= 1e-9 or abs(independent) >= 1e-9:
            source_buffs.append((attr, fixed * (1 + get_display_attr(pre_base, PERCENT_ATTR.get(attr, "")) / 100),
                                 1 + percent / 100, 1 + independent / 100))
    apply_buffs = []
    for attr in buff_attrs:
        if attr in SOURCE_ATTRS or attr.endswith("%"):
            continue
        base = DISPLAY_TO_TARGET.get(attr, attr)
        fixed = get_attr(buffs, attr) + (get_attr(buffs, base) if base != attr else 0.0)
        percent = attack_increase_for_channel(buffs, base) if base in {"物攻", "魔攻"} else get_attr(buffs, f"{base}%")
        amplified = skill_percent_amplified_flat_attack(buffs, base)
        amplified = min(max(0.0, amplified), fixed) if fixed >= 0 else max(min(0.0, amplified), fixed)
        apply_buffs.append((attr, base, amplified, fixed - amplified, 1 + percent / 100))
    elemental_buffs = tuple((attr, get_attr(buffs, attr)) for attr in buff_attrs if attr in ELEMENT_ATTRS)
    source, target, ratio, coeff, cap = core_parameters
    source_keys, target_keys = tuple(FLAT_TARGETS.get(source, [source])), tuple(FLAT_TARGETS.get(target, [target]))
    core_enabled = bool(source and target and ratio > 0 and coeff > 0)

    def actual(pre, special):
        values = {attr: trunc(pre.get(attr, 0.0) * factor + special.get(attr, 0.0)) for attr, factor in actual_rules}
        values.update((attr, pre.get(attr, 0.0) + special.get(attr, 0.0)) for attr in percent_attrs)
        return values

    def convert(values):
        out = dict(values)
        sources = tuple(values.get(source, 0.0) for source in SOURCE_ATTRS)
        for targets, ratios, factors in conversions:
            delta = 0.0
            for value, conversion_ratio in zip(sources, ratios):
                delta += value * conversion_ratio
            for attr, factor in zip(targets, factors):
                out[attr] = out.get(attr, 0.0) + delta * factor
        for attr in fixed_attrs:
            out[attr] = trunc(out.get(attr, 0.0))
        return out

    def prepare_pre(totals):
        pre = dict(pre_base)
        for attr, value in totals.items():
            pre[attr] = trunc(pre.get(attr, 0.0) + value)
        return pre

    effective_job = norm(build.get("effective_second_job")) or norm(build.get("second_job"))
    effective = compile_effective_panel(calculator, effective_job, build, buffs)

    def objective(totals, *, relaxed_core=False, lower_totals=None):
        if any(key.endswith("%") for key in totals) or any(key.endswith("%") for key in (lower_totals or {})):
            return reference(totals, relaxed_core=relaxed_core, lower_totals=lower_totals)
        pre = prepare_pre(totals)
        special, exempt = dict(mastery), dict(mastery_exempt)
        if core_enabled:
            converted = convert(actual(pre, special))
            source_value = calculator._core_source_value(converted, source)
            deducted = (min(source_value, cap) if cap > 0 else source_value) * ratio / 100
            gained = deducted * coeff
            if deducted:
                for key in source_keys:
                    special[key] = special.get(key, 0.0) - deducted
                for key in target_keys:
                    special[key] = special.get(key, 0.0) + gained
                    if target in {"物攻", "魔攻"}:
                        exempt[key] = exempt.get(key, 0.0) + gained
        if relaxed_core:
            lower = convert(actual(prepare_pre(lower_totals or {}), mastery))
            source_value = calculator._core_source_value(lower, source)
            deduction = (min(source_value, cap) if cap > 0 else source_value) * ratio / 100 if coeff > 0 else 0.0
            for key in source_keys:
                special[key] = max(special.get(key, 0.0), mastery.get(key, 0.0) - deduction)
        for key in special:
            if not key.endswith("%"):
                special[key] = trunc(special[key])
        for key in exempt:
            if not key.endswith("%"):
                exempt[key] = trunc(exempt[key])
        values = actual(pre, special)
        for attr, fixed, factor, independent in source_buffs:
            values[attr] = trunc((values.get(attr, 0.0) + fixed) * factor * independent)
        converted = convert(values)
        buffed = dict(converted)
        for attr, fixed in elemental_buffs:
            buffed[attr] = converted.get(attr, 0.0) + fixed
        for attr, base, amplified, fixed, factor in apply_buffs:
            exempt_fixed = exempt.get(attr, 0.0)
            value = (converted.get(attr, 0.0) - exempt_fixed + amplified) * factor + exempt_fixed + fixed
            if base == "魔攻":
                value += converted.get("力量", 0.0) * get_attr(buffs, "力量转魔攻%") / 100
                value += converted.get("智力", 0.0) * get_attr(buffs, "智力转魔攻%") / 100
            elif base == "物攻":
                value += converted.get("智力", 0.0) * get_attr(buffs, "智力转物攻%") / 100
            buffed[attr] = trunc(value)
        return effective(buffed)

    return objective


def compile_effective_panel(calculator, job, build, buffs):
    """Freeze environment/buff terms only for independent physical/magic lanes."""
    row = calculator.effective.get(job, {})
    channels = [i for i in range(1, 4) if as_number(row.get(f"权重{i}")) > 0 and norm(row.get(f"攻击{i}"))]
    if any(norm(row.get(f"攻击{i}")) not in {"物攻", "魔攻"} for i in channels):
        return lambda values: calculator._effective_panel(job, build, values, buffs)["effective_panel"]
    free_final = calculator._free_percent_bonus(build, "最终")
    panel_final = calculator._panel_percent_bonus(buffs, "最终")
    free_crit = calculator._free_percent_bonus(build, "致命")
    panel_crit = calculator._panel_percent_bonus(buffs, "致命")
    free_damage = calculator._free_percent_bonus(build, "致命伤害")
    panel_damage = calculator._panel_percent_bonus(buffs, "致命伤害")
    resistance = as_number(build.get("crit_resist_value", build.get("crit_resist", 0)))
    reduction = calculator._crit_resist_reduction_percent(buffs)
    effective_resistance = max(0.0, resistance * (1 - min(100.0, reduction) / 100))
    crit_resistance = min(round(effective_resistance / 433, 1), 90) / 100
    critical_extra = calculator._critical_extra_damage_percent(buffs)
    hp_multiplier = calculator._hp_conditional_damage_multiplier(buffs)
    independent_multiplier = calculator._independent_damage_multiplier(buffs)
    monster_defense = as_number(build.get("monster_defense"))
    element_resists = calculator._element_resists(build)
    lanes = []
    weight_total = 0.0
    for i in channels:
        attack, element = norm(row.get(f"攻击{i}")), norm(row.get(f"属性{i}"))
        weight, fixed, extra_final = as_number(row.get(f"权重{i}")), as_number(row.get(f"固定{i}")), as_number(row.get(f"额外最终{i}"))
        elem_rate = {"火": "火攻%", "水": "水攻%", "光": "光攻%", "暗": "暗攻%"}.get(element, '无属性%')
        resist = element_resists.get(element, 0.0) - calculator._element_resist_reduction(buffs, element) if element in element_resists else 0.0
        amplification = 1 + calculator._damage_amp_percent(buffs, attack, 1, 1) / 100
        defense_reduction = calculator._defense_reduction_percent(buffs, attack, 1, 1)
        defense = max(0.0, monster_defense * (1 - defense_reduction / 100))
        defense_multiplier = max(0.0, 1 - calculator.defense_percent(defense) / 100)
        lanes.append((attack, weight, fixed, extra_final, elem_rate, resist, amplification, defense_multiplier))
        weight_total += weight

    def effective(values):
        phys = (values.get("最小物攻", 0.0) + values.get("最大物攻", 0.0)) / 2
        magic = (values.get("最小魔攻", 0.0) + values.get("最大魔攻", 0.0)) / 2
        final_percent = calculator.final_percent(values.get("最终", 0.0)) + free_final + panel_final
        crit_percent = min(round(values.get("致命", 0.0) / 433, 1) + free_crit + panel_crit, 90)
        damage_percent = min(round(values.get("致命伤害", 0.0) / 62500 * 100, 3) + free_damage + panel_damage, 100)
        final_rate, crit_rate = final_percent / 100, crit_percent / 100
        crit_damage = 2 + damage_percent / 100
        effective_crit = crit_rate * max(0, 1 - crit_resistance)
        crit_multiplier = 1 + effective_crit * (crit_damage - 1)
        extra_multiplier = 1 + effective_crit * critical_extra / 100
        sniper_multiplier = calculator._sniper_critical_extra_multiplier(buffs, effective_crit)
        total = 0.0
        for attack, weight, fixed, extra_final, elem_rate, resist, amplification, defense_multiplier in lanes:
            panel = phys if attack == "物攻" else magic
            element_multiplier = (1 + values.get(elem_rate, 0.0) / 100) * (1 - resist / 100) if elem_rate else 1.0
            total += (panel * weight / 100 + fixed) * (1 + final_rate + extra_final / 100) * crit_multiplier * extra_multiplier * sniper_multiplier * hp_multiplier * independent_multiplier * element_multiplier * amplification * defense_multiplier
        return total / (weight_total / 100) if weight_total else 0.0

    return effective
