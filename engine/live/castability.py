"""Reusable, evidence-based cast-start permissions (NOT output windows).

Computed in replay/score preparation, never in the packet decoder. Times are
capture-relative. A teammate's verified action permits a start throughout the
action and for 0.5 s afterwards. This is a heuristic, not a control-state oracle.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

CASTABILITY_VERSION = 'teammate-action-end-tail-v1'
VERIFIED_CAST_KINDS = frozenset({'skill_action', 'skill_cast', 'status_skill_action'})


def merge_windows(rows):
    result = []
    for a, b in sorted((float(a), float(b)) for a, b in rows):
        if not isfinite(a) or not isfinite(b) or b <= a:
            continue
        if result and a <= result[-1][1] + 1e-9:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return tuple(result)


def subtract_windows(rows, excluded):
    rows = merge_windows(rows)
    for a, b in merge_windows(excluded):
        rows = tuple(piece for c, d in rows
                     for piece in ((c, min(d, a)), (max(c, b), d))
                     if piece[1] > piece[0])
    return rows


def local_windows(rows, offset, duration):
    return merge_windows((max(0., a-offset), min(duration, b-offset))
                         for a, b in rows if b > offset and a < offset+duration)


@dataclass(frozen=True)
class CastabilityEvidence:
    windows: tuple[tuple[float, float], ...]
    unavailable_windows: tuple[tuple[float, float], ...]
    evidence_casts: int
    unresolved_durations: int
    tail_seconds: float = .5
    version: str = CASTABILITY_VERSION


def build_castability_by_source(players, casts, timings, lifecycle, duration,
                                *, canonical_skill=lambda name: name, tail_seconds=.5):
    """Return permissions for each party member, excluding their own casts.

    ``timings[(actor, timestamp_ms, canonical_skill)]`` is the existing actual-axis
    ``(action_duration, action_speed)`` value. Missing durations never invent an
    animation: only the evidenced start plus tail is retained. Inferred hits,
    summons, TCP bystanders and unconfirmed inputs cannot open windows.
    """
    members = {str(p.source_id) for p in players}
    duration = max(0., float(duration))
    tail = max(0., float(tail_seconds))
    by_actor = {actor: [] for actor in members}
    counts = {actor: 0 for actor in members}
    unresolved = {actor: 0 for actor in members}
    for cast in casts:
        actor = str(cast.source_id)
        if actor not in members or cast.packet_kind not in VERIFIED_CAST_KINDS:
            continue
        start = float(cast.timestamp_ms) / 1000
        if start < 0 or start >= duration:
            continue
        key = (actor, cast.timestamp_ms, canonical_skill(cast.skill_name))
        timing = timings.get(key)
        action = float(timing[0]) if timing is not None else 0.
        if not isfinite(action) or action < 0:
            action = 0.
        by_actor[actor].append((start, min(duration, start+action+tail)))
        counts[actor] += 1
        unresolved[actor] += int(timing is None)
    unavailable = {actor: [] for actor in members}
    inactive_since = {}
    for event in sorted(lifecycle, key=lambda e: e.timestamp_ms):
        actor = str(event.target_id)
        if actor not in members:
            continue
        t = max(0., min(duration, event.timestamp_ms / 1000))
        if not event.active:
            inactive_since.setdefault(actor, t)
        elif actor in inactive_since:
            unavailable[actor].append((inactive_since.pop(actor), t))
    for actor, start in inactive_since.items():
        unavailable[actor].append((start, duration))
    by_actor = {actor: merge_windows(rows) for actor, rows in by_actor.items()}
    return {
        actor: CastabilityEvidence(
            windows=subtract_windows(
                (w for other, rows in by_actor.items() if other != actor for w in rows),
                unavailable[actor]),
            unavailable_windows=merge_windows(unavailable[actor]),
            evidence_casts=sum(n for other, n in counts.items() if other != actor),
            unresolved_durations=sum(n for other, n in unresolved.items() if other != actor),
            tail_seconds=tail,
        ) for actor in members
    }
