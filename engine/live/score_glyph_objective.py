"""Fixed captured-damage reference for scoring glyphs, not a new rotation.

Keep this separate from the scheduler's team objective. Elemental debuffs only
benefit matching skill damage; existing effects on the SAME target reduce the
marginal benefit. Unknown damage elements are never guessed from party size.
"""
from bisect import bisect_right
from collections import defaultdict

from ..data_loader import as_number, norm
from ..effect_constants import TEAM_FREEZE_SKILL_GROUPS
from .scoring import canonical_skill, data_job_for


DEBUFF_KEYS = frozenset(("冰冻层", "减火抗%", "减水抗%", "减光抗%", "减暗抗%", "减全抗%"))
_FREEZE_GROUPS = {canonical_skill(k): v for k, v in TEAM_FREEZE_SKILL_GROUPS.items()}


def captured_glyph_damage_context(snapshot, players, store, output_windows, boss_target_ids=None):
    """Sweep once; retain aggregated damage states, not another event timeline."""
    jobs = {str(p.source_id): data_job_for(norm(p.job)) for p in players}
    windows = sorted((float(a), float(b)) for a, b in output_windows if b > a)
    starts = [a for a, _ in windows]
    if boss_target_ids is None and getattr(snapshot, "damage_targets", ()):
        from .aggregator import CombatAggregator
        boss_target_ids = CombatAggregator._primary_boss_target_ids(list(snapshot.damage_targets))
    targets = set(boss_target_ids or ())
    effects, boundaries = [], []
    for row in getattr(snapshot, "boss_effects", ()):
        if row.key not in DEBUFF_KEYS or not row.target_id or (targets and row.target_id not in targets):
            continue
        value = as_number(row.value)
        if value <= 0:
            continue
        index = len(effects)
        effects.append(dict(source_id=str(row.source_id), skill=row.skill_name,
                            job=row.job, attrs={row.key: value}, cooldown=0, duration=1))
        for a, b in row.intervals:
            if b > a:
                boundaries.extend(((float(a), 1, row.target_id, index),
                                   (float(b), -1, row.target_id, index)))
    boundaries.sort()
    active, counts, buckets = defaultdict(set), defaultdict(int), defaultdict(float)
    element_cache = {}
    boundary_index = 0
    for event in sorted(getattr(snapshot, "combat_events", ()), key=lambda e: e.timestamp_ms):
        if event.healing or event.amount <= 0 or event.source_id not in jobs:
            continue
        if targets and event.target_id not in targets:
            continue
        seconds = event.timestamp_ms / 1000.0
        window_index = bisect_right(starts, seconds) - 1
        if window_index < 0 or seconds >= windows[window_index][1]:
            continue
        while boundary_index < len(boundaries) and boundaries[boundary_index][0] <= seconds:
            _, delta, target, index = boundaries[boundary_index]
            counts[target, index] += delta
            if counts[target, index] > 0:
                active[target].add(index)
            else:
                active[target].discard(index)
            boundary_index += 1
        key = jobs[event.source_id], event.skill_name
        if key not in element_cache:
            element_cache[key] = store.skill_damage_element(*key)
        buckets[event.source_id, element_cache[key], tuple(sorted(active[event.target_id]))] += event.amount
    return dict(effects=effects, buckets=[dict(source_id=s, element=e, active=ids, damage=d)
                                        for (s, e, ids), d in buckets.items()])


def glyph_profile_for_source(context, source_id, base_profile=None):
    """Shallow shared evidence; no changes to the scheduler's profile."""
    return {**(base_profile or {}), "glyph_damage_context": context,
            "glyph_owner_source_id": str(source_id)}


def _merged_debuff_attrs(proxy, effects):
    attrs = proxy._merged_team_attrs(effects)
    # All alchemist ice refreshes ONE layer, including another alchemist's ice.
    # Ice Spirit's different freeze skills can stack, up to two total layers.
    groups = {}
    for row in effects:
        value = max(0.0, as_number(row.get("attrs", {}).get("冰冻层")))
        if not value:
            continue
        skill = canonical_skill(row.get("skill"))
        group = _FREEZE_GROUPS.get(skill)
        if group == "炼金" or norm(row.get("job")) in {"炼金圣士", "药剂师", "炼金术士"}:
            key = ("炼金",)
            value = min(1.0, value)
        else:
            key = (row.get("source_id", ""), skill)
        groups[key] = max(value, groups.get(key, 0.0))
    attrs["冰冻层"] = min(2.0, sum(groups.values()))
    return attrs


def captured_debuff_gain_ratio(proxy, action, profile):
    """Return None without evidence; zero is an authoritative zero benefit.

    All candidates share one captured reference. Remove their own baseline
    status when valuing additional uptime, retain other skills/providers, and
    strip captured debuff amplification from damage weights before comparing.
    The caller still applies the candidate's incremental duration/CD coverage.
    """
    attrs = {norm(k): as_number(v) for k, v in action.get("team_objective_attrs", {}).items()
             if as_number(v)}
    if not attrs or not set(attrs).issubset(DEBUFF_KEYS):
        return None
    context = (profile or {}).get("glyph_damage_context")
    if not isinstance(context, dict) or not context.get("buckets"):
        return None
    owner = str(profile.get("glyph_owner_source_id", ""))
    effects = context["effects"]
    skill = canonical_skill(action.get("skill"))
    candidate = dict(source_id=owner, skill=action.get("skill"), attrs=attrs,
                     cooldown=0, duration=1)
    multiplier_cache, merged_cache = {}, {}

    def multiplier(ids, element, add=False):
        key = ids, add
        if key not in merged_cache:
            rows = [effects[i] for i in ids]
            merged_cache[key] = _merged_debuff_attrs(proxy, rows + ([candidate] if add else []))
        key = ids, element, add
        if key not in multiplier_cache:
            member = dict(element=element, physical_share=1.0, magic_share=0.0)
            empty = proxy._skill_burst_team_objective_member_multiplier({}, member, profile)
            full = proxy._skill_burst_team_objective_member_multiplier(merged_cache[ids, add], member, profile)
            multiplier_cache[key] = full / max(1e-9, empty)
        return multiplier_cache[key]

    owner_damage, gain = 0.0, 0.0
    for bucket in context["buckets"]:
        element = bucket["element"]
        can_benefit = "冰冻层" in attrs or (
            element in {"火", "水", "光", "暗"}
            and ("减全抗%" in attrs or f"减{element}抗%" in attrs))
        if not can_benefit and bucket["source_id"] != owner:
            continue
        relevant_keys = {"冰冻层", "减全抗%", f"减{element}抗%"}
        ids = tuple(i for i in bucket["active"] if relevant_keys.intersection(effects[i]["attrs"]))
        neutral_damage = bucket["damage"] / max(1e-9, multiplier(ids, element))
        if bucket["source_id"] == owner:
            owner_damage += neutral_damage
        if not can_benefit:
            continue
        baseline = tuple(i for i in ids if not (
            effects[i]["source_id"] == owner and canonical_skill(effects[i]["skill"]) == skill
            and set(effects[i]["attrs"]).intersection(attrs)))
        gain += neutral_damage * max(0.0, multiplier(baseline, element, True) - multiplier(baseline, element))
    # No recorded owner damage: do not fabricate a huge team/owner ratio.
    return gain / owner_damage if owner_damage > 1e-9 else 0.0
