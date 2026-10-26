"""Evidence-scoped duties for the reviewed Nightmare Sea split-Boss phase.

Attack targets are not a general distance detector. Only the reviewed
Manticore/Apocalypse and Cerberus/summoned-Minotaur encounters enable grouping.
Buff receipt is never evidence.
Unknown members retain their obligations; grouping ends when concurrent combat
ends. No wire events, damage, or provider contributions are changed here.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import floor

from .event_platform import event_platform_for

SPLIT_TEMPLATES = frozenset({'4235447', '4235448', '4237012', '4237036'})
BIN_SECONDS = 10.0


def reviewed_split_pair(left_targets, right_targets):
    """User-confirmed separated Nightmare Sea mechanics, not a 4-person rule.

    A summon roaming between arenas cannot join their support cohorts. Unknown
    entity IDs are deliberately not promoted to Bosses by this rule.
    """
    left = {str(t).rsplit('@',1)[-1] for t in left_targets}
    right = {str(t).rsplit('@',1)[-1] for t in right_targets}
    return (('4235447' in left and '4235448' in right)
            or ('4235448' in left and '4235447' in right)
            or ('4237012' in left and '4237036' in right)
            or ('4237012' in right and '4237036' in left))


def merge(rows):
    result = []
    for start, end in sorted(rows):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start,end))
    return tuple(result)


def subtract(windows, excluded):
    pieces = list(windows)
    for left, right in merge(excluded):
        pieces = [(a,min(b,left)) for a,b in pieces if a < left] + [
                  (max(a,right),b) for a,b in pieces if b > right]
    return merge(pieces)


@dataclass(frozen=True)
class DutyGroups:
    # start, end, independently supported source -> target assignment
    segments: tuple = ()

    def unavailable(self, source, recipient):
        return merge((start,end) for start,end,groups in self.segments
                     if source in groups and recipient in groups and groups[source] != groups[recipient])

    def recipients(self, source, recipient, windows):
        return subtract(windows, self.unavailable(source, recipient))

    def boss_windows(self, source, target, windows):
        if target.rsplit('@',1)[-1] not in SPLIT_TEMPLATES:
            return tuple(windows)
        return subtract(windows, ((a,b) for a,b,g in self.segments
                                  if source in g and g[source] != target))

    def together(self, source, recipient, seconds):
        for start,end,groups in self.segments:
            if start <= seconds < end:
                return source not in groups or recipient not in groups or groups[source] == groups[recipient]
        return True

    def share(self, source, recipient, seconds, providers):
        eligible = {p for p in providers if self.together(p, recipient, seconds)}
        return 1.0 / len(eligible) if source in eligible else 0.0

    def shared_seconds(self, source, recipient, windows, providers):
        cuts = sorted({t for a,b,_g in self.segments for t in (a,b)})
        total = 0.0
        for start,end in windows:
            points = [start, *(t for t in cuts if start < t < end), end]
            total += sum((b-a)*self.share(source, recipient, (a+b)/2, providers)
                         for a,b in zip(points, points[1:]))
        return total


def build_duty_groups(snapshot):
    platform = event_platform_for(snapshot)
    party = {p.source_id for p in platform.players}
    bins = defaultdict(lambda: defaultdict(Counter))
    amounts = defaultdict(lambda: defaultdict(Counter))
    impacts = defaultdict(set)
    for event in platform.combat_events:
        if (event.healing or event.amount <= 0 or event.source_id not in party
                or event.target_id.rsplit('@',1)[-1] not in SPLIT_TEMPLATES):
            continue
        index = floor(event.timestamp_ms / (BIN_SECONDS*1000))
        bins[index][event.source_id][event.target_id] += 1
        amounts[index][event.source_id][event.target_id] += event.amount
        impacts[(index,event.timestamp_ms,event.source_id,event.skill_id)].add(event.target_id)
    # A simultaneous co-hit is positive evidence that separate attack targets
    # do not establish separation in this bin. Keep the full duty in that case.
    cohit_bins = {key[0] for key,targets in impacts.items() if len(targets)>1}
    candidates = []
    for index, actors in sorted(bins.items()):
        if index in cohit_bins:
            continue
        groups = {}
        for source, counts in actors.items():
            target, count = counts.most_common(1)[0]
            damage = amounts[index][source]
            if (count >= 2 and count >= sum(counts.values())*.8
                    and damage[target] >= sum(damage.values())*.8):
                groups[source] = target
        cohort_sizes = Counter(groups.values())
        targets = [target for target,n in cohort_sizes.items() if n>=2]
        if any(reviewed_split_pair({a},{b}) for i,a in enumerate(targets) for b in targets[i+1:]):
            candidates.append((index*BIN_SECONDS,(index+1)*BIN_SECONDS,groups))
    # A single brief overlap/focus swap must not establish a split encounter.
    indexed = {round(a/BIN_SECONDS): groups for a,_,groups in candidates}
    stable = []
    for start,end,groups in candidates:
        index = round(start/BIN_SECONDS)
        assignment = {source: target for source,target in groups.items()
                      if any(indexed.get(neighbor, {}).get(source) == target
                             for neighbor in (index-1,index+1))}
        if len([n for n in Counter(assignment.values()).values() if n >= 2]) >= 2:
            stable.append((start,end,assignment))
    return DutyGroups(tuple(stable))


def groups_for(engine, snapshot):
    cached = getattr(engine, '_duty_groups_cache', None)
    if cached is None or cached[0] is not snapshot:
        cached = (snapshot, build_duty_groups(snapshot))
        engine._duty_groups_cache = cached
    return cached[1]
