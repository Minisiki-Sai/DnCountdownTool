"""Nest-scoped entity denominators, separate from phase damage budgets.

UDP supplies current HP, not maximum HP. Only near-full observations may
disambiguate shared templates; damage totals and decoder-derived maxima must
never be used as independent evidence.
"""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def profiles():
    path = Path(__file__).resolve().parents[1]/'data/profiles/nest_hp_audit.json'
    return tuple(json.loads(path.read_text(encoding='utf-8'))['profiles'])


def template_id(target):
    return str(target).rsplit('@', 1)[-1]


def select_reference(snapshot, target_ids, hint=''):
    ids = {template_id(t) for t in target_ids}
    candidates = [p for p in profiles() if ids.intersection(p['entities'])]
    if not candidates:
        return None
    # Same template can appear in both classic and four-player dragon nests.
    current = {}
    for track in getattr(snapshot, 'boss_hp_tracks', ()):
        if track.confidence != 'packet_hp':
            continue
        tid = template_id(track.target_id)
        for point in getattr(track, 'points', ()):
            if point.current_hp is not None:
                current[tid] = max(current.get(tid, 0), point.current_hp)
    def score(profile):
        evidence = sum(0 <= r['full_hp']-current[tid] <= max(32, r['full_hp']*.005)
                       for tid, r in profile['entities'].items() if tid in current)
        named = max((len(h) for h in profile['hints'] if h in str(hint)), default=0)
        return evidence, named, len(ids.intersection(profile['entities']))
    candidates.sort(key=score, reverse=True)
    if len(candidates) > 1 and score(candidates[0]) == score(candidates[1]):
        return None
    return candidates[0]


@lru_cache(maxsize=1)
def known_templates():
    return frozenset(t for p in profiles() for t in p['entities'])


def known_template(target):
    return template_id(target) in known_templates()


def entity_max(profile, target):
    return int(profile['entities'].get(template_id(target), {}).get('full_hp', 0)) if profile else 0


def damage_budget(profile, target_ids):
    """Return None for unresolved phase budgets, never substitute full HP."""
    if not profile:
        return None
    ids = {template_id(t) for t in target_ids}
    if not ids.issubset(profile['entities']):
        return None
    label = profile['label']
    if label == '黑龙硬核':
        if ids.issubset({'4181001','4181009'}):
            return sum(entity_max(profile,t)//2 for t in target_ids)
        if ids & {str(i) for i in range(4182001,4182009)}:
            return None  # Mechanic weapons share names with fightable weapons.
    if label == '沙龙硬核' and ids & {'4150050','4160001'}:
        # Each phase uses half of its own (different) full-HP denominator.
        return sum(entity_max(profile,t)//2 for t in target_ids)
    if label == '梦魇海龙':
        from .nightmare_sea_dragon import PHASES
        sea = {str(t) for p in PHASES for t in p['template_ids']}
        if ids & sea:
            return sum(p['required_damage'] for p in PHASES if ids.intersection(map(str,p['template_ids'])))
        return sum(entity_max(profile,t) for t in target_ids)
    if label.startswith('黑龙'):
        if ids & {'503681','503682','503683','503696'}:
            # 0821黑龙3: 100%->77%, new entity 80%->50%, new entity
            # 50%->0%. The 77% action threshold is also in the phase-1 AI.
            # Resetting from 77% to 80% means the budget is 103%, not 100%.
            portions = {'503681':23, '503682':30, '503696':50}
            if label == '黑龙123' and ids.issubset(portions):
                return sum(entity_max(profile,t)*portions[template_id(t)]//100 for t in target_ids)
            return None  # Arms and other phase exits require separate evidence.
        if ids & {'503639','503652'}:
            # 0821黑龙2: 211749856 -> -14942; 75% spawn to zero.
            if ids == {'503652'} and label == '黑龙123':
                return sum(entity_max(profile,t)*3//4 for t in target_ids)
            return None
    if label.startswith('沙龙') and ids & {'503001','503002','503051','503054','503055','503061','503062','503063','503064'}:
        return None
    if (label == '迷雾巢穴' and '503496' in ids) or ids & {'236018','236048','237056','237057'}:
        return None
    # Count runtime entities, not names (four bishops/two spiders in green).
    return sum(entity_max(profile,t) for t in target_ids)
