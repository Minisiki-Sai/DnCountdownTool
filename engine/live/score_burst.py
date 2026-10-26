"""Skill burst valuation with impact segments for fixed-duration stances."""
from functools import lru_cache
from collections import defaultdict
from collections.abc import Iterator
from bisect import bisect_right

FIXED_DURATION = frozenset({'包围','猛砍站姿','猛砍姿态','挥砍姿态'})

def original_buff_duration(skill, observed):
    from .scoring import canonical_skill, _COMBAT_MODIFIER_RULES
    nominal=_COMBAT_MODIFIER_RULES.get(canonical_skill(skill),(None,None,None))[2]
    return max(float(observed),float(nominal or 0.))

def score_weight(diagnostics, available=True):
    return .3 if available else 0.

def output_score(completion, burst, weight):
    return max(0., completion*(1-weight)+min(100.,max(0.,burst))*weight)

def window_power(parts, windows):
    # Legacy callers may supply a consumable window iterator. Preserve its
    # original consumption semantics; production scoring uses tuple/list.
    if isinstance(windows, Iterator):
        return sum(power for seconds, power in parts if seconds is not None
                   and any(start <= seconds < end for start, end in windows))
    # The interval union avoids scanning every Buff window for every Hit.
    # Keep the original part order (and sum order): this is not a rounded or
    # bucketed scoring approximation. Intervals remain left-closed/right-open.
    ranges = sorted((start, end) for start, end in windows if start < end)
    if not ranges:
        return sum(power for seconds, power in parts if seconds is not None
                   and any(start <= seconds < end for start, end in windows))
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    starts = tuple(start for start, _ in merged)
    ends = tuple(end for _, end in merged)
    return sum(power for seconds, power in parts
               if seconds is not None and seconds == seconds
               and (index := bisect_right(starts, seconds) - 1) >= 0
               and seconds < ends[index])

def support_window_fallback(snapshot, personal, membership, owned_windows):
    """No damaging evidence: use the team's maximum available window union."""
    from .support_boss_scope import merge
    boss_windows={i:list(ws) for i,ws in enumerate(owned_windows)}
    for bosses in membership.values():
        for i,ws in bosses.items():boss_windows.setdefault(i,[]).extend(ws)
    maximum=merge([w for ws in boss_windows.values() for w in ws]
                  +[w for ws in personal.values() for w in ws])
    fallback=[]
    for player in getattr(snapshot,'players',()):
        actor=str(player.source_id)
        if not personal.get(actor) and getattr(player,'damage',0)<=0:
            personal[actor]=list(maximum)
            membership[actor]={i:list(merge(ws)) for i,ws in boss_windows.items()}
            fallback.append(actor)
    return fallback

@lru_cache(maxsize=4096)
def native_parts(job,sid,key,speed):
    # Retained for standalone Project timing diagnostics.
    from .score_hit_evidence import repository
    from skill_hit_tool.burst_timing import relative_parts
    return relative_parts(repository(),job,sid,key,speed)

@lru_cache(maxsize=4096)
def impact_parts(job,sid,key,speed):
    from .score_hit_evidence import repository
    from skill_hit_tool.burst_timing import route_hits
    hits=route_hits(repository(),job,sid,key,speed)
    # Impact clocks, not object creation or status-refresh clocks.
    if not hits or any(h.start is None or h.weight is None for h in hits):return ()
    return tuple((max(0.,h.start),h.weight,'impact') for h in hits if h.weight>0)

def split_cast(cast,rule,job,actual):
    if cast.key not in FIXED_DURATION:return [(cast.seconds,cast.power)],'cast_start'
    if actual:
        point=rule.get('casts',{}).get(str(round(cast.seconds*1000)),{})
        parts=point.get('burst_parts',[])
        if parts and all(p.get('impact_seconds') is not None for p in parts):
            total=sum(p['weight'] for p in parts)
            if total>0:return [(p['impact_seconds'],cast.power*p['weight']/total) for p in parts],'observed_hit'
    template=impact_parts(job,str(rule.get('skill_id','')),cast.key,round(float(getattr(cast,'action_speed_percent',0)),5))
    duration=float(getattr(cast,'utilized_duration',0) or getattr(cast,'expected_duration',0) or 0)
    if duration>0:template=tuple(p for p in template if p[0]<=duration+1e-6)
    total=sum(p[1] for p in template)
    if total>0:
        return [(cast.seconds+t,cast.power*w/total) for t,w,_ in template],'timeline_impact_estimate' if actual else 'timeline_impact'
    return [(cast.seconds,cast.power)],'unresolved_cast_start'

def burst_contributions(actual,theory,plan,job):
    v2=isinstance(plan,dict) and bool(plan.get('_score_v2'))
    if not v2:return ([(c.seconds,c.power) for c in actual],[(c.seconds,c.power) for c in theory],dict(mode='v1_cast_start'))
    rules=plan['_score_v2'].get('profiles',{});counts=defaultdict(int);out=[]
    for side,casts in [('actual',actual),('theory',theory)]:
        result=[]
        for cast in casts:
            parts,method=split_cast(cast,rules.get(cast.key,{}),job,side=='actual')
            counts[side+':'+method]+=1;result.extend(parts)
        out.append(result)
    return *out,dict(mode='v2_skill_impact_segments',methods=dict(counts),
        actual_power_coverage=1.,theory_power_coverage=1.,excluded_skills={},score_cap=100.)

def matching_window(row,peers):
    """Prefer the same time interval; duplicate status tracks cannot shift peers."""
    if not peers:return row
    return min(peers,key=lambda p:(
        -max(0.,min(row.end_seconds,p.end_seconds)-max(row.start_seconds,p.start_seconds)),
        abs(row.start_seconds-p.start_seconds),abs(row.end_seconds-p.end_seconds)))

def filter_burst_windows(actual_rows,theory_rows,theory_parts):
    """Compare same-skill windows by overlap, then nearest planned time."""
    from .scoring import canonical_skill
    removed=[]
    def eligible(rows,side):
        kept=[]
        for row in rows:
            duration=row.end_seconds-row.start_seconds
            original=getattr(row,'original_duration',0.) or duration
            if duration <= original/3.+1e-9:
                removed.append(dict(side=side,label=row.label,start=row.start_seconds,end=row.end_seconds,
                    original_duration=original,reason='有效片段不超过原始Buff时长的1/3'))
            else:kept.append(row)
        return kept
    actual_rows=eligible(actual_rows,'actual');theory_rows=eligible(theory_rows,'theory')
    by_label=defaultdict(list)
    for row in theory_rows:by_label[canonical_skill(row.label)].append(row)
    kept_actual=[]
    # Keep every positive planned window, including windows with no factual
    # counterpart. Otherwise missing burst casts would shrink the denominator.
    kept_theory=[r for r in theory_rows if window_power(theory_parts,((r.start_seconds,r.end_seconds),))>1e-9]
    for row in actual_rows:
        peers=by_label[canonical_skill(row.label)]
        reference=matching_window(row,peers)
        power=window_power(theory_parts,((reference.start_seconds,reference.end_seconds),))
        if power<=1e-9:
            removed.append(dict(label=row.label,start=row.start_seconds,end=row.end_seconds,reason='对应理论窗口伤害为零'))
        else:
            kept_actual.append(row)
            if not theory_rows:kept_theory.append(reference)
    return tuple(kept_actual),tuple(kept_theory),removed
