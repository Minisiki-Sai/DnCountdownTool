"""Keep combat-proven plates and enforce required scoring timing plates.

Selection metadata is not inference evidence. A manual per-player override
remains authoritative, including an explicitly empty override. Required timing
plates may replace the least valuable existing plate at the seven-slot limit.
"""
import copy
import math
from .scoring import canonical_skill
from ..data_loader import as_number, norm
from ..skill_glyph_policy import required_timing_skill_glyphs

MAX_SCORE_GLYPHS = 7
MIN_SCORE_GLYPHS = 4


def clean_score_glyph_rows(rows, source):
    result, seen = [], set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        key = canonical_skill(row.get('skill'))
        effect = norm(row.get('effect'))
        value = as_number(row.get('value'))
        if not key or key == canonical_skill('无') or key in seen or effect in ('', '无') or not math.isfinite(value) or value <= 0:
            continue
        seen.add(key)
        result.append({**copy.deepcopy(row), 'selection_source': source})
        if len(result) == MAX_SCORE_GLYPHS:
            break
    return result


def score_glyph_source(rows, manual=False):
    if manual:
        return 'manual_override'
    return 'combat_inferred_with_auto_fill' if any(r.get('selection_source') == 'auto_fill' for r in rows) else 'combat_inferred'


def _add_required_timing_glyphs(editor, build, job, rows, team_size, team_objective_profile):
    """Keep required timing plates even when combat evidence fills 4-7 slots."""
    replaced = []
    # Glyph catalogues are stored under second jobs; these required first-job
    # skills use the same catalogue/tier in either descendant profession.
    catalogue_job = {'游侠': '影舞者', '魔导师': '时空领主'}.get(job, job)
    for skill, effect in required_timing_skill_glyphs(job):
        key = canonical_skill(skill)
        if any(canonical_skill(r['skill']) == key and r['effect'] == effect for r in rows):
            continue
        options = [
            option
            for entry in editor.store.skill_glyphs(catalogue_job)
            if canonical_skill(entry.get('skill')) == key
            for option in entry.get('effects', {}).get(effect, [])
            if math.isfinite(as_number(option.get('value'))) and as_number(option.get('value')) > 0
        ]
        if not options:
            continue  # Do not invent a glyph tier absent from the catalogue.
        option = max(options, key=lambda r: as_number(r.get('value')))
        required = dict(skill=skill, effect=effect, value=as_number(option['value']),
                        option=norm(option.get('grade')) or '高档',
                        selection_source='auto_fill', selection_reason='required_timing')
        same_skill = [r for r in rows if canonical_skill(r['skill']) == key]
        for row in same_skill:
            rows.remove(row)
            replaced.append({**copy.deepcopy(row), 'replacement_reason': 'required_timing_same_skill'})
        if len(rows) >= MAX_SCORE_GLYPHS:
            ranked = editor._extreme_apply_auto_skill_glyphs(
                copy.deepcopy(build), job, count=MAX_SCORE_GLYPHS,
                support_team_size=max(1, team_size), team_objective_profile=team_objective_profile,
                fill_to_count=True, candidate_glyphs=copy.deepcopy(rows))
            rank_keys = [canonical_skill(r['skill']) for r in ranked]
            # Ineligible/unlearned candidates have no measurable score benefit.
            lowest = next((r for r in reversed(rows) if canonical_skill(r['skill']) not in rank_keys), None)
            if lowest is None:
                lowest = next(r for r in rows if canonical_skill(r['skill']) == rank_keys[-1])
            rows.remove(lowest)
            replaced.append({**copy.deepcopy(lowest), 'replacement_reason': 'required_timing_lowest_gain'})
        rows.append(required)
    return replaced


def prepare_score_glyphs(editor, build, job, *, inferred_rows, manual_rows=None,
                         team_size=1, team_objective_profile=None):
    """Mutate this player's copied build only; return rows and refinement flag."""
    real_job = norm(editor._team_real_job(job))
    replaced = []
    if manual_rows is not None:
        rows = clean_score_glyph_rows(manual_rows, 'manual_override')
        auto = False
    else:
        rows = clean_score_glyph_rows(inferred_rows, 'combat_inferred')
        replaced = _add_required_timing_glyphs(
            editor, build, real_job, rows, team_size, team_objective_profile)
        remaining = max(0, MIN_SCORE_GLYPHS - len(rows))
        auto = remaining > 0 or any(r.get('selection_source') == 'auto_fill' for r in rows)
        if remaining:
            occupied = {canonical_skill(row['skill']) for row in rows}
            excluded = set(editor._extreme_score_excluded_glyphs(job))
            # Exclude every effect of an occupied skill, not just the inferred
            # effect. One skill cannot use two plates and replace its evidence.
            for entry in editor.store.skill_glyphs(real_job):
                if canonical_skill(entry.get('skill')) in occupied:
                    excluded.update((entry['skill'], effect) for effect in entry.get('effects', {}))
            forced = [(skill, effect) for skill, effect in editor._extreme_score_forced_glyphs(job)
                      if canonical_skill(skill) not in occupied]
            ranked = editor._extreme_apply_auto_skill_glyphs(
                copy.deepcopy(build), job, count=remaining,
                support_team_size=max(1, team_size), team_objective_profile=team_objective_profile,
                fill_to_count=True, excluded_glyphs=excluded, forced_glyphs=forced)
            for row in clean_score_glyph_rows(ranked, 'auto_fill'):
                key = canonical_skill(row['skill'])
                if key not in occupied:
                    rows.append(row)
                    occupied.add(key)
                if len(rows) >= MIN_SCORE_GLYPHS:
                    break
    build['skill_glyphs'] = dict(second_job=real_job, count=max(MIN_SCORE_GLYPHS, len(rows)),
        rows=copy.deepcopy(rows), auto_selected=auto,
        source=score_glyph_source(rows, manual_rows is not None))
    if replaced:
        build['skill_glyphs']['replaced_rows'] = replaced
    return rows, auto
