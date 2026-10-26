"""Combat-only effective skill levels, isolated by actor and capture.

These are effective cast levels, not a reconstruction of SP versus mastery or
equipment bonuses. Missing usage excludes an active skill from scoring theory
(level 0), not proof that the player never learned it. Suspected-only skills are disabled (level 0), not
declared unlearned. Confirmed usage without a level keeps a labelled baseline.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from functools import lru_cache
from typing import Any

from ..data_loader import DATA_DIR, as_number, norm
from .event_platform import event_platform_for

COMBAT_SKILL_LEVEL_VERSION = 'combat-skill-levels-v6-exclude-unused'

# Protocol provenance, not a per-profession/skill blacklist. Unknown or derived
# cast kinds cannot turn a candidate skill name into evidence of learning.
_USAGE_ACTION_KINDS = frozenset({
    'skill_action', 'status_skill_action', 'skill_cast', 'compact_skill_input',
    'structured_multi_action', '2404', '1224',
})


def _damage_confirms_skill(event: Any) -> bool:
    from .damage_attribution import attribution_rule, AttributionLayer
    if event.healing or event.amount <= 0 or event.has_real_hit_evidence is not True:
        return False
    if str(event.skill_id).startswith(('supplemental:', 'periodic:')):
        return False
    rule = attribution_rule(event.attribution_method)
    if not rule.deterministic or rule.layer not in {
        AttributionLayer.DIRECT, AttributionLayer.OBJECT, AttributionLayer.ENTITY,
    }:
        return False
    if rule.layer is AttributionLayer.ENTITY:
        # A verified owned entity can establish the producing skill family,
        # but does NOT create a new player cast or infer a cast timestamp.
        return bool(event.entity_owner_evidence_id and event.entity_attack_evidence_id)
    return True


@lru_cache(maxsize=1)
def level_row_catalog() -> dict[str, list[list[int]]]:
    """Legacy DNT fallback; current profession PvE rows take precedence below."""
    path = DATA_DIR / 'profiles/combat_skill_level_rows.json'
    # A missing packaged catalogue is an error, not permission to guess levels.
    return json.loads(path.read_text(encoding='utf-8'))['rows']


def _pve_level_rows(data: dict[str, Any]) -> dict[str, tuple[tuple[int, int], ...]]:
    """Index only explicit row/skill/level triples, never list positions.

    Older compact job files omit RowId/_SkillIndex and must keep using the
    legacy catalogue. PvP rows cannot establish a PvE combat level.
    """
    found: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for mode in ('normal', 'master'):
        for skill_id, entries in data.get('skill_levels', {}).get(mode, {}).get('pve', {}).items():
            for entry in entries:
                try:
                    row_id = int(entry['RowId'])
                    sid = int(entry['_SkillIndex'])
                    level = int(entry['_SkillLevel'])
                    container_sid = int(skill_id)
                except (KeyError, TypeError, ValueError):
                    continue
                if row_id > 0 and sid == container_sid and sid > 0 and 0 < level < 200:
                    found[str(row_id)].add((sid, level))
    return {row: tuple(sorted(pairs)) for row, pairs in found.items()}


def _current_level_row_catalog(store: Any, job: str) -> dict[str, tuple[tuple[int, int], ...]]:
    # Reuse the profession payload already loaded for skill_build_tree. No
    # all-profession file scan, and no full catalogue copy for each player.
    loader = getattr(store, '_full_skill_job_data', None)
    data = loader(job) if callable(loader) else None
    if not data:
        return {}
    cache = getattr(store, '_combat_current_level_row_cache', None)
    if cache is None:
        cache = store._combat_current_level_row_cache = {}
    cached = cache.get(job)
    if cached is not None and cached[0] is data:
        return cached[1]
    rows = _pve_level_rows(data)
    # Identity validation also handles replacing a loaded job payload. The
    # cache belongs to the DataStore, not a global retaining large stores.
    cache[job] = (data, rows)
    return rows


def infer_combat_skill_levels(snapshot: Any, store: Any) -> dict[str, dict[str, Any]]:
    from .scoring import canonical_skill, data_job_for, _builtin_skill_builds

    platform = event_platform_for(snapshot)
    parameters = tuple(platform.stream('skill_parameter'))
    casts = tuple(platform.skill_cast_events)
    combat = tuple(platform.combat_events)
    if not (parameters or casts or combat or platform.player_buff_series):
        return {}
    by_source: dict[str, list[Any]] = defaultdict(list)
    for event in parameters:
        by_source[str(event.source_id)].append(event)
    buffs_by_source: dict[str, list[Any]] = defaultdict(list)
    used: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    suspected: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for event in casts:
        kind = str(event.packet_kind).strip().casefold()
        evidence = used if kind in _USAGE_ACTION_KINDS else suspected
        evidence[str(event.source_id)][canonical_skill(event.skill_name)].add('cast:' + kind)
    for event in combat:
        if not event.healing and event.amount > 0 and event.skill_name:
            evidence = used if _damage_confirms_skill(event) else suspected
            evidence[str(event.source_id)][canonical_skill(event.skill_name)].add(
                'damage:' + str(event.attribution_method))
    for event in platform.player_buff_series:
        # These intervals come from the received-status ledger, not the
        # theoretical axis. Validate their provider/skill ID against the tree
        # below; a display name alone must not establish skill usage.
        buffs_by_source[str(event.source_id)].append(event)
        suspected[str(event.source_id)][canonical_skill(event.skill_name)].add('buff_series')
    catalog = level_row_catalog()
    result: dict[str, dict[str, Any]] = {}
    for player in platform.players:
        source, job = str(player.source_id), data_job_for(player.job)
        tree = store.skill_build_tree(job, 70).get('skills', {})
        if not tree:
            continue
        current_rows = _current_level_row_catalog(store, job)
        defaults = _builtin_skill_builds().get(job, {})
        normal = defaults.get('skill_build', {}).get('levels', {})
        mastery = defaults.get('skill_mastery_build', {}).get('levels', {})
        # Never let an EX passive's level 1 overwrite its active base level.
        damage_ids = {str(row['id']) for row in store._skill_damage_entries(job)}
        active = {str(sid): item for sid, item in tree.items()
                  if int(item.get('skill_type', 0)) == 0
                  or (int(item.get('skill_type', 0)) != 3 and str(sid) in damage_ids)}
        active_by_key = {canonical_skill(item['name']): sid for sid, item in active.items()}
        for event in buffs_by_source.get(source, ()):
            sid = str(event.skill_id)
            key = canonical_skill(event.skill_name)
            if (sid in active and canonical_skill(active[sid]['name']) == key
                    and any(end > start for start, end in event.intervals)):
                # Preserve confirmed received effects even when their level
                # row/action is absent (e.g. older recordings). This proves
                # effect usage by the recorded provider, not an exact level
                # or a new cast time, and never credits the receiver.
                used[source][key].add('buff_effect_provider')
        exact: dict[str, set[int]] = defaultdict(set)
        evidence: dict[str, set[tuple[int, int]]] = defaultdict(set)
        level_sources: dict[str, set[str]] = defaultdict(set)
        for event in by_source.get(source, ()):
            raw_sid = str(event.raw_skill_id or event.skill_id)
            key = canonical_skill(event.skill_name)
            # Restrict evidence to this profession's active skill IDs. This
            # rejects proc/suffix/monster IDs even when a display alias matches.
            if raw_sid not in active:
                continue
            key = canonical_skill(active[raw_sid]['name'])
            if event.packet_kind != 'skill_parameter_status':
                suspected[source][key].add('parameter:' + str(event.packet_kind))
                continue
            used[source][key].add('skill_parameter_status')
            row_id = int(event.skill_level_row_id or 0)
            row_key = str(row_id)
            # A current row supersedes the old row for THIS profession. Do
            # not rewrite a row ID globally or retry a mismatching current
            # row against the old table: both skill ID and row must agree.
            current = row_key in current_rows
            candidates = current_rows[row_key] if current else catalog.get(row_key, ())
            matches = {int(level) for sid, level in candidates
                       if str(sid) == raw_sid}
            if matches:
                exact[key].update(matches)
                evidence[key].add((row_id, int(event.timestamp_ms)))
                level_sources[key].add('current_job_pve' if current else 'legacy_dnt')
        levels: dict[str, int] = {}
        skills: list[dict[str, Any]] = []
        for key, sid in sorted(active_by_key.items()):
            baseline = int(as_number(normal.get(sid))) + int(as_number(mastery.get(sid)))
            observed = key in used[source]
            values = sorted(exact.get(key, ()))
            if len(values) == 1:
                level, status = values[0], 'confirmed_combat_row'
            elif observed:
                # A used skill with conflicting cast levels cannot be reduced
                # to one factual level. Keep baseline and expose the conflict.
                level = baseline or 1
                status = 'conflicting_combat_levels' if values else 'used_level_unknown'
            elif key in suspected[source]:
                level, status = 0, 'unconfirmed_usage_level_0'
            elif baseline > 0:
                level, status = 0, 'unused_theory_excluded'
            else:
                # Do not learn a whole new tree of mutually exclusive skills.
                continue
            levels[key] = level
            skills.append({'skill_id': sid, 'skill': active[sid]['name'],
                           'level': level, 'baseline_level': baseline,
                           'status': status, 'observed': observed,
                           'usage_status': ('confirmed' if observed else
                                            'suspected' if key in suspected[source] else 'not_observed'),
                           'usage_evidence': sorted(used[source].get(key, ())),
                           'suspected_evidence': sorted(suspected[source].get(key, ())),
                           'candidates': values, 'evidence_count': len(evidence.get(key, ())),
                           'level_data_sources': sorted(level_sources.get(key, ())),
                           'level_row_ids': sorted({r for r, _ in evidence.get(key, ())})})
        parent = {'十字军':'神圣之锤', '烈':'致残惩罚者'}.get(job)
        if parent and parent in levels:
            levels[parent + '攻击'] = levels[parent]
        result[source] = {'version': COMBAT_SKILL_LEVEL_VERSION, 'source_id': source,
                          'job': job, 'levels': levels, 'skills': skills}
    return result


def level_signature(reports: dict[str, dict[str, Any]]) -> tuple:
    return (COMBAT_SKILL_LEVEL_VERSION, tuple(
        (source, tuple((r['skill_id'], r['level'], r['status'], tuple(r['candidates']))
                       for r in report['skills'])) for source, report in sorted(reports.items())))


def skill_level_display_rows(report: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Expose evidence, never present a scoring fallback as a factual level."""
    from .scoring import canonical_skill

    result = {}
    for row in (report or {}).get('skills', ()):
        key = canonical_skill(row.get('skill'))
        if not key:
            continue
        status = str(row.get('status') or 'unknown')
        value = row.get('level')
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        result[key] = {
            'actual_level': value if valid and value > 0 and status == 'confirmed_combat_row' else None,
            'scoring_level': value if valid else None,
            'level_status': status,
            'level_candidates': tuple(sorted({level for level in row.get('candidates', ())
                if isinstance(level, int) and not isinstance(level, bool) and level > 0})),
        }
    parent_name = {'十字军':'神圣之锤', '烈':'致残惩罚者'}.get((report or {}).get('job'))
    if parent_name and parent_name in result:
        parent = dict(result[parent_name])
        if parent['level_status'] == 'confirmed_combat_row':
            parent['level_status'] = 'confirmed_parent_skill'
        result[parent_name + '攻击'] = parent
    return result


def apply_combat_levels_to_build(build: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    from .scoring import canonical_skill
    result = copy.deepcopy(build)
    if not report:
        return result
    normal = result.setdefault('skill_build', {}).setdefault('levels', {})
    mastery = result.setdefault('skill_mastery_build', {}).setdefault('levels', {})
    for row in report['skills']:
        if row['status'] in {'confirmed_combat_row', 'unused_theory_level_1', 'unused_theory_excluded', 'unconfirmed_usage_level_0'}:
            normal[row['skill_id']] = row['level']
            mastery[row['skill_id']] = 0  # response already includes bonuses
        elif row['observed'] and int(as_number(normal.get(row['skill_id']))) <= 0:
            normal[row['skill_id']] = row['level']
    for buff in result.get('buffs', ()):
        if isinstance(buff, dict):
            key = canonical_skill(norm(buff.get('skill')).split('｜')[-1])
            if key in report['levels']:
                buff['level'] = str(report['levels'][key])
    if isinstance(result.get('_score_standard_profile'), dict):
        result['_score_standard_profile']['skill_build'] = '战斗等级；仅疑似及未观察使用的主动技能不排入理论轴；确认使用但等级未知保留基准'
    result['_score_combat_skill_levels'] = copy.deepcopy(report)
    return result
