"""Support opportunities from Boss participation, never from Buff receipts.

All timestamps are capture seconds. Pre-pull Buffs retain their real duration:
their overlap with the provider's next group is counted when that group starts.
"""

def merge(rows):
    out = []
    for a, b in sorted(rows):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return tuple(out)


def intersect(left, right):
    return merge((max(a, c), min(b, d)) for a, b in left for c, d in right
                 if min(b, d) > max(a, c))


def contains(rows, seconds):
    return any(a <= seconds < b for a, b in rows)


def living_opportunities(expected, tracks):
    """Remove confirmed HP-zero periods, not silence or scene-removal events.

    Only absolute server HP tracks establish death/recovery. Missing evidence
    preserves the original opportunity; inferred HP must never excuse coverage.
    """
    from math import isfinite
    from .duty_groups import subtract

    states = {}
    for track in tracks:
        if track.state_source != "absolute_086f":
            continue
        actor_states = states.setdefault(track.target_id, {})
        for point in track.points:
            if isfinite(point.timestamp_seconds) and isfinite(point.current_hp):
                at = point.timestamp_seconds
                actor_states[at] = actor_states.get(at, True) and point.current_hp > 0
    result = {}
    for actor, windows in expected.items():
        dead = []
        opened = None
        for at, alive in sorted(states.get(actor, {}).items()):
            if not alive and opened is None:
                opened = at
            elif alive and opened is not None:
                dead.append((opened, at))
                opened = None
        if opened is not None:
            dead.append((opened, float("inf")))
        result[actor] = subtract(windows, dead)
    return result


def make_scope(encounters, membership, pairs):
    parent = list(range(len(encounters)))
    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for a, b in pairs:
        parent[root(b)] = root(a)
    members = {}
    for actor, bosses in membership.items():
        groups = {}
        for boss, windows in bosses.items():
            groups.setdefault(str(root(int(boss))), []).extend(windows)
        members[str(actor)] = {g: merge(ws) for g, ws in groups.items()}
    targets = {str(t): str(root(i)) for i, e in enumerate(encounters) for t in e.target_ids}
    return dict(members=members, target_groups=targets)


def from_plans(plans):
    return next((p.get('_score_v2', {}).get('support_boss_scope')
                 for p in (plans or {}).values()
                 if p.get('_score_v2', {}).get('support_boss_scope') is not None), {})


def opportunities(scope, provider):
    """Recipient-person-time in the provider's current or next Boss group."""
    members = scope.get('members', {})
    own = members.get(str(provider), {})
    return {actor: merge(w for group, windows in own.items()
                         for w in intersect(windows, groups.get(group, ())))
            for actor, groups in members.items()}


def eligible_event(scope, provider, event):
    if not scope or str(provider).startswith('__support_benchmark__:'):
        return True
    seconds = event.timestamp_ms / 1000.
    members = scope.get('members', {})
    own = members.get(str(provider), {})
    other = members.get(str(event.source_id), {})
    target_group = scope.get('target_groups', {}).get(str(event.target_id))
    return any((target_group is None or group == target_group)
               and contains(windows, seconds) and contains(other.get(group, ()), seconds)
               for group, windows in own.items())


def coverage(expected, received):
    denominator = sum(b-a for ws in expected.values() for a,b in ws)
    numerator = sum(b-a for actor, ws in expected.items()
                    for a,b in intersect(ws, received.get(actor, ())))
    return numerator / denominator if denominator > 1e-9 else 0.


def clip_effect_windows(windows):
    """Independent opportunity mask survives factual-mask removal/retiming."""
    result = []
    for row in windows:
        effect = row.get('team_effect') or {}
        mask = effect.get('_score_recipient_eligible_intervals')
        if mask is None:
            result.append(row)
        else:
            for a,b in intersect(((row['start'], row['end']),), mask):
                result.append(dict(row, start=a, end=b))
    return result
