"""Boss coexistence needs a cast that actually hits both entities."""
from itertools import combinations
from types import SimpleNamespace

def cast_bosses(row,encounters):
    targets=set(row.get('hit_target_counts') or row.get('target_ids') or ())
    return {i for i,e in enumerate(encounters) if targets.intersection(e.target_ids)}

def encounter_windows(encounters,report,snapshot=None):
    from .score_hit_evidence import exclusive_encounter_windows
    from .duty_groups import reviewed_split_pair
    pairs=set()
    for row in report.get('cast_rows',()):
        if row.get('unbound_hits') or not row.get('actual_hits'):continue
        for i,j in combinations(sorted(cast_bosses(row,encounters)),2):
            if reviewed_split_pair(encounters[i].target_ids, encounters[j].target_ids):
                continue
            if max(encounters[i].first_ms,encounters[j].first_ms)<min(encounters[i].last_ms,encounters[j].last_ms):pairs.add((i,j))
    end_evidence={}
    adjusted=[]
    for i,e in enumerate(encounters):
        windows=list(e.phase_windows)
        # Retrospective user-selected cutoff, not protocol-confirmed death.
        first=float(e.first_ms)/1000;last=float(e.last_ms)/1000+.001
        end_evidence[str(i)]="last_damage_death_policy"
        windows=[(max(a,first),min(b,last))for a,b in windows if min(b,last)>max(a,first)]
        adjusted.append(SimpleNamespace(phase_windows=windows,first_ms=e.first_ms,last_ms=e.last_ms,damage_series=getattr(e,'damage_series',())))
    base,diag=exclusive_encounter_windows(adjusted)
    for i,j in pairs:
        for a,b in adjusted[i].phase_windows:
            for c,d in adjusted[j].phase_windows:
                if min(b,d)>max(a,c):
                    base[i].append((max(a,c),min(b,d)));base[j].append((max(a,c),min(b,d)))
    for i,windows in enumerate(base):
        merged=[]
        for a,b in sorted(windows):
            if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:merged.append((a,b))
        base[i]=merged
    diag.update(cohit_boss_pairs=sorted(pairs),end_evidence=end_evidence,policy='same_cast_cohit_proves_coexistence; maximum_ceiling_while_active; single_continuous_cooldown')
    return base,diag


def player_boss_windows(encounters,report,owned,pairs,*,with_membership=False,snapshot=None):
    """Encounter participation with continuous in-group time and explicit switches."""
    parent=list(range(len(encounters)))
    def root(x):
        while parent[x]!=x:x=parent[x]
        return x
    for a,b in pairs:parent[root(b)]=root(a)
    def merged(rows):
        out=[]
        for a,b in sorted(rows):
            if b<=a:continue
            if out and a<=out[-1][1]:out[-1]=(out[-1][0],max(b,out[-1][1]))
            else:out.append((a,b))
        return out
    # Isolated groups can be attacked concurrently by different players.
    # Global exclusive ownership is unsuitable for their personal windows.
    individual=[]
    for i,e in enumerate(encounters):
        ws=list(getattr(e,'phase_windows',owned[i]))
        if hasattr(e,'first_ms') and hasattr(e,'last_ms'):
            ws=[(max(a,e.first_ms/1000),min(b,e.last_ms/1000+.001))for a,b in ws
                if min(b,e.last_ms/1000+.001)>max(a,e.first_ms/1000)]
        individual.append(ws)
    groups={}
    for i,ws in enumerate(individual):groups.setdefault(root(i),[]).extend(ws)
    groups={k:merged(v) for k,v in groups.items()}
    actors={}
    for row in report.get('cast_rows',[]):
        if row.get('unbound_hits') or not row.get('actual_hits'):continue
        bosses=cast_bosses(row,encounters)
        for group in {root(b) for b in bosses}:
            parts=row.get('burst_parts',[])
            first=min((p['impact_seconds'] for p in parts),default=row['cast_timestamp_ms']/1000)
            last=max((p['impact_seconds'] for p in parts),default=first)
            actors.setdefault(str(row['source_id']),[]).append((row['cast_timestamp_ms']/1000,group,first,last))
    if snapshot is not None:
        # Participation requires damage evidence, not a supported multiplier
        # model or a successfully bound cast. Summons/unknown routes count too.
        # Use settlement clocks so an old cast timestamp cannot hide a later
        # target switch. Never infer spatial co-location from separate hits.
        actors={}
        targets={target:i for i,e in enumerate(encounters) for target in e.target_ids}
        for event in snapshot.combat_events:
            if event.healing or event.amount<=0 or event.target_id not in targets:continue
            i=targets[event.target_id];stamp=event.timestamp_ms/1000.
            if not any(a<=stamp<b for a,b in individual[i]):continue
            actors.setdefault(str(event.source_id),[]).append((stamp,root(i),stamp,stamp))
    result={};membership={}
    for actor,rows in actors.items():
        runs=[]
        for cast,group,first,last in sorted(rows):
            if runs and runs[-1]['group']==group:
                runs[-1]['last']=max(runs[-1]['last'],last)
            else:runs.append(dict(group=group,first=first,last=last,cast=cast))
        windows=[]
        for i,run in enumerate(runs):
            ws=groups[run['group']]
            start=run['first'] if i else -float('inf')
            end=run['last']+.001 if i+1<len(runs) else float('inf')
            # Residual damage from an older cast must not extend through a
            # subsequently evidenced switch to another isolated Boss group.
            if i+1<len(runs):end=min(end,runs[i+1]['first'])
            windows.extend((max(a,start),min(b,end))for a,b in ws if min(b,end)>max(a,start))
            for boss,bws in enumerate(individual):
                if root(boss)==run['group']:
                    membership.setdefault(actor,{}).setdefault(boss,[]).extend(
                        (max(a,start),min(b,end))for a,b in bws if min(b,end)>max(a,start))
        result[actor]=merged(windows)
    membership={actor:{boss:merged(ws)for boss,ws in bs.items()}for actor,bs in membership.items()}
    return (result,membership) if with_membership else result
