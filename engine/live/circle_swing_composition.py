"""Presentation-only split. Canonical combat/scoring identities stay intact."""
from bisect import bisect_right
from collections import defaultdict
from .skill_identity import canonical_skill
from .critical_stats import is_critical_rate_sample

LABELS={'normal':'旋转冲击（普通）','true':'真旋转冲击','unknown':'旋转冲击（未确认）'}
def split_composition(payload,snapshot,factual_hit_indices,event_composition,separated_composition):
    from .additional_damage_composition import _composition_identity
    casts=defaultdict(list)
    for e in snapshot.skill_cast_events:
        if e.job=='狂战士' and canonical_skill(e.skill_name)=='旋转冲击':casts[e.source_id].append(e)
    for values in casts.values():values.sort(key=lambda e:e.timestamp_ms)
    times={k:[e.timestamp_ms for e in v]for k,v in casts.items()}
    groups=defaultdict(list)
    for i,e in enumerate(snapshot.combat_events):
        if e.source_id not in casts:continue
        native=_composition_identity(e)
        destinations={'legacy':separated_composition.get(i,native),'raw':separated_composition.get(i,native),'attributed':event_composition.get(i,native)}
        for view,(sid,name) in destinations.items():
            if canonical_skill(name)!='旋转冲击':continue
            index=bisect_right(times[e.source_id],e.timestamp_ms)-1
            cast=casts[e.source_id][index] if index>=0 else None
            form=getattr(cast,'variant_key','unknown') if cast and 0<=e.timestamp_ms-cast.timestamp_ms<=6000 else 'unknown'
            if form not in LABELS:form='unknown'
            groups[e.source_id,view,form].append((i,e,cast.timestamp_ms if cast else None))
            combat=payload.get('combat_events',[])
            if view=='attributed' and i<len(combat):
                combat[i]['composition_skill_id']='505:circle:'+form
                combat[i]['composition_skill_name']=LABELS[form]
    for player in payload.get('players',()):
        actor=player.get('source_id')
        if actor not in casts:continue
        for field in ('skills','skills_unattributed','skills_attributed','skills_unattributed_burning_hand_merged','skills_attributed_burning_hand_merged'):
            rows=player.get(field)
            if not isinstance(rows,list):continue
            view='legacy' if field=='skills' else ('attributed' if field.startswith('skills_attributed') else 'raw')
            selected=[r for r in rows if canonical_skill(r.get('name',''))=='旋转冲击']
            if not selected:continue
            total=sum(r.get('damage',0) for r in selected)
            evidence=sum(e.amount for form in LABELS for _,e,_ in groups[actor,view,form] if not e.healing)
            if evidence>total:continue  # Never reallocate a differently composed view by ratios.
            split=[]
            for form,label in LABELS.items():
                events=groups[actor,view,form];damage=sum(e.amount for _,e,_ in events if not e.healing)
                if form=='unknown':damage+=total-evidence
                if not damage:continue
                hits=[e for i,e,_ in events if i in factual_hit_indices and not e.healing]
                landed={stamp for i,e,stamp in events if i in factual_hit_indices and stamp is not None and not e.healing}
                samples=[e for e in hits if is_critical_rate_sample(e)]
                split.append(dict(selected[0],skill_id='505:circle:'+form,name=label,damage=damage,healing=0,hits=len(hits),damage_records=len(events),
                    critical_samples=len(samples),critical_hits=sum(e.critical for e in samples),share=damage/max(1,player.get('damage',0)),
                    cast_count=len(landed),direct_cast_count=len(landed),inferred_cast_count=0,average_hits=len(hits)/len(landed) if landed else None,
                    additional_damage=[],attribution=[],trigger_skills=[]))
            player[field]=sorted([r for r in rows if r not in selected]+split,key=lambda r:r.get('damage',0),reverse=True)
