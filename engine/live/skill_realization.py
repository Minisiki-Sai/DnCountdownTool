"""Automatic whole-capture realization, with per-cast, state-dependent capped Boss references."""
from __future__ import annotations
from collections import defaultdict
import math,re

def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None

def summarize_report(report, *, include_forms=True):
    from .skill_identity import canonical_skill
    from .score_body_policy import body_sensitive
    from .score import usable
    groups = defaultdict(list)
    for row in report.get('cast_rows', ()):
        groups[str(row['source_id']), canonical_skill(row['skill_name'])].append(row)
    result = []
    for (actor, key), records in groups.items():
        geometry=body_sensitive(records[0]["job"],key)
        # Route/shape and speed-dependent ceilings cannot calibrate each other.
        cohorts = defaultdict(list)
        for row in records:
            if row.get('is_boss_cast') is False or not usable(row) or not number(row.get('actual_hits')):
                continue
            values = [number(row.get(k)) for k in ('expected_hits', 'matched_hits', 'actual_hits', 'expected_weight', 'realized_weight')]
            if any(v is None for v in values) or values[0] <= 0 or values[3] <= 0:
                continue
            cohort = (row.get('form', ''), row.get('baseline_kind', ''), values[0], row.get('boss_key', 'current'))
            cohorts[cohort].append((row, values))
        n = estimated = 0
        hits = matched = expected_hits = weight = expected_weight = boss_hits = boss_weight = 0.
        baselines = []
        hit_rates=[]; multiplier_rates=[]; theory_hit_rates=[]; theory_weight_rates=[]; raw_hit_rates=[]
        per_boss=defaultdict(lambda:dict(hit_rates=[],multiplier_rates=[],valid_casts=0,estimated_casts=0))
        ceilings={}; normalized={}
        def state_group(cohort):
            # Speed is a condition of the cast, not a new geometric target.
            form=re.sub(r'本次\d+Hit','本次状态Hit',str(cohort[0]))
            return form,cohort[1],cohort[3]
        for cohort, rows in cohorts.items():
            key0=state_group(cohort);previous=normalized.get(key0,(0.,0.))
            normalized[key0]=(max(previous[0],max(v[2]/v[0] for _,v in rows)),max(previous[1],max(v[4]/v[3] for _,v in rows)))
        for cohort, rows in cohorts.items():
            hr,wr=normalized[state_group(cohort)]
            ceilings[cohort]=(cohort[2]*min(1.,hr),min(1.,wr)) if geometry else (cohort[2],1.)
        for cohort, rows in cohorts.items():
            max_hits = ceilings[cohort][0]
            # Physical/magical conversion can change the full-route weight on
            # every cast. Compare normalized realization, not exact float values.
            max_ratio = ceilings[cohort][1]
            if not geometry or cohort[1]=="outbreak_ex_left_reference":max_hits,max_ratio=cohort[2],1.
            weights = [values[3] for _, values in rows]
            baselines.append(dict(form=cohort[0], expected_hits=cohort[2],
                boss=rows[0][0].get('boss_name','当前Boss'), boss_key=cohort[3],
                expected_weight=max(weights), expected_weight_min=min(weights),
                max_hits=max_hits, max_weight=max(weights)*max_ratio,
                max_multiplier_rate=100*max_ratio, casts=len(rows)))
            for row, (eh, mh, ah, ew, aw) in rows:
                n += 1; estimated += bool(row.get('estimated'))
                hits += ah; matched += mh; expected_hits += eh
                weight += aw; expected_weight += ew
                active=[ceilings.get((*cohort[:3],key)) for key in row.get("active_boss_keys",(cohort[3],))]
                active=[v for v in active if v is not None]
                cast_hits=max([max_hits]+[v[0] for v in active])
                cast_ratio=max([max_ratio]+[v[1] for v in active])
                if cohort[1]=="outbreak_ex_left_reference":cast_hits,cast_ratio=eh,1.
                boss_hits += cast_hits; boss_weight += ew * cast_ratio
                # Each cast votes once, at its own state/speed ceiling.
                if cast_hits>0:hit_rates.append(ah/cast_hits)
                if cast_ratio>0:multiplier_rates.append(aw/(ew*cast_ratio))
                detail=per_boss[cohort[3]]
                detail['boss']=row.get('boss_name','当前Boss')
                detail['valid_casts']+=1;detail['estimated_casts']+=bool(row.get('estimated'))
                if cast_hits>0:detail['hit_rates'].append(ah/cast_hits)
                if cast_ratio>0:detail['multiplier_rates'].append(aw/(ew*cast_ratio))
                theory_hit_rates.append(mh/eh); theory_weight_rates.append(aw/ew); raw_hit_rates.append(ah/eh)
        def mean(rows): return 100*sum(rows)/len(rows) if rows else None
        boss_records=defaultdict(int)
        for row in records:
            if row.get('is_boss_cast') is not False:
                boss_records[row.get('boss_key','current')]+=1
        boss_rows=[dict(boss_key=boss_key,boss=detail['boss'],valid_casts=detail['valid_casts'],
            total_records=boss_records[boss_key],unknown_records=boss_records[boss_key]-detail['valid_casts'],
            estimated_casts=detail['estimated_casts'],hit_boss_rate=mean(detail['hit_rates']),
            multiplier_boss_rate=mean(detail['multiplier_rates'])) for boss_key,detail in per_boss.items()]
        result.append(dict(source_id=actor, skill=key,boss_rows=boss_rows, skill_id=str(records[0]['skill_id']),
            hit_boss_rate=mean(hit_rates), hit_theory_rate=mean(theory_hit_rates),
            multiplier_boss_rate=mean(multiplier_rates), multiplier_theory_rate=mean(theory_weight_rates),
            raw_hit_theory_rate=mean(raw_hit_rates), valid_casts=n, total_records=len(records),
            unknown_records=sum(r.get('is_boss_cast') is not False for r in records)-n, estimated_casts=estimated, actual_hits=hits, matched_hits=matched,
            boss_records=sum(r.get('is_boss_cast') is not False for r in records),
            non_boss_records=sum(r.get('is_boss_cast') is False for r in records),
            landed_casts=len({r["cast_timestamp_ms"] for r in records if not r.get("unbound_hits") and number(r.get("actual_hits"))}),
            raw_hits=sum(number(r.get("act_hits")) or 0 for r in records),
            logical_hits=sum(number(r.get("actual_hits")) or 0 for r in records),
            expected_hits=expected_hits, actual_weight=weight, expected_weight=expected_weight,
            extra_hits=sum(max(0, number(r.get('extra_hits')) or 0) for rows in cohorts.values() for r,_ in rows),
            baselines=baselines,
            note='整场命中Boss的施放；名单外技能使用逐次状态的理论完整Hit与倍率；仅指定20项体型技能按实际命中目标及已证明同时在场的Boss（取较高上限）、同形态/动作速度分别取可靠单次最大值，分母最高为完整参考100%，实得Hit和倍率可超过100%，先逐次除以自身条件上限，再对单次完成率等权平均；非Boss命中保留在伤害统计，不参与Boss实现率。物魔换算权重先归一化，不使用90%阈值。只纳入至少命中一次的施放；理论Hit只计主体段，倍率保留额外命中。未绑定/截断/未知记录不按0%计算。'))
    if include_forms:
        labels={'normal':'旋转冲击（普通）','true':'真旋转冲击','unknown':'旋转冲击（未确认）'}
        split=[]
        for row in report.get('cast_rows',()):
            if row.get('job')=='狂战士' and canonical_skill(row['skill_name'])=='旋转冲击':
                form=row.get('circle_swing_form',{}).get('form','unknown')
                split.append(dict(row,skill_name=labels.get(form,labels['unknown'])))
        result.extend(summarize_report(dict(report,cast_rows=split),include_forms=False))
    return result

def replay_skill_realization(core, payload, progress, *, snapshot=None, path=None, encounters=None):
    from pathlib import Path
    from .skill_identity import canonical_skill
    from .score_hit_evidence import load_report, exclusive_encounter_windows
    source = core.resolve_score_source_hash(str(payload.get('sourceHash') or '').strip())
    if source.startswith('live:'):
        raise ValueError('实现率需要已保存的抓包，请停止抓包后计算')
    if snapshot is None or path is None:
        with core._state_lock:
            snapshot = core._replays.get(source)
            path = core._replay_sources.get(source)
    if snapshot is None or not path:
        raise ValueError('请先载入抓包')
    if encounters is None:
        encounters = core._score_encounters(source, snapshot)
    report,key,diagnostics = load_report(Path(path), None, core.store, progress,snapshot=snapshot)
    scoped=[]
    from .calibration_boss_scope import encounter_windows,cast_bosses
    owned_windows,phase_diagnostics=encounter_windows(encounters,report,snapshot)
    for row in report['cast_rows']:
        stamp=row['cast_timestamp_ms']/1000
        matches=[(index,e) for index,e in enumerate(encounters)
                 if any(a <= stamp < b for a,b in owned_windows[index])]
        hit_bosses=cast_bosses(row,encounters)
        selected=set(row.get('target_ids',()))
        primary=[i for i in hit_bosses if selected.intersection(encounters[i].target_ids)]
        boss=primary[0] if primary else (min(hit_bosses) if hit_bosses else None)
        active={str(i) for i,_ in matches}
        if boss is not None:active.add(str(boss))
        scoped.append(dict(row,boss_key=str(boss) if boss is not None else 'non-boss',
            boss_name=encounters[boss].name if boss is not None else '非Boss阶段',
            is_boss_cast=bool(hit_bosses), active_boss_keys=sorted(active)))
    report=dict(report,cast_rows=scoped)
    targets={target for e in encounters for target in e.target_ids}
    result = dict(sourceHash=source, scope='full_capture', bossTargetIds=sorted(targets), fingerprint=key,
                  rows=summarize_report(report), diagnostics=dict(diagnostics,phase_ownership=phase_diagnostics))
    # Show every encounter, including those without a usable sample. Unknown
    # is distinct from zero; multi-target casts keep the global primary-target
    # attribution and are never counted twice in the whole-run mean.
    for skill in result['rows']:
        available={r['boss_key']:r for r in skill['boss_rows']}
        skill['boss_rows']=[available.get(str(i),dict(boss_key=str(i),boss=e.name,
            valid_casts=0,total_records=sum(r['source_id']==skill['source_id'] and
                canonical_skill(r['skill_name'])==skill['skill'] and r['boss_key']==str(i)
                for r in scoped),hit_boss_rate=None,multiplier_boss_rate=None,estimated_casts=0))
            for i,e in enumerate(encounters)]
    progress(100,'技能Hit与倍率实现率计算完成')
    return result


def request_skill_realization(core,payload):
    """Automatic derived data with short status requests, outside user jobs."""
    import threading,time
    source=core.resolve_score_source_hash(str(payload.get('sourceHash') or '').strip())
    if not source or source.startswith('live:'):raise ValueError('请先打开已保存的抓包')
    with core._state_lock:
        if source not in core._replays:raise ValueError('请先载入抓包')
        states=getattr(core,'_skill_realization_states',None)
        if states is None:states=core._skill_realization_states={}
        state=states.get(source)
        if state is None:
            state=dict(sourceHash=source,state='pending',progress=0,message='正在读取整场实现率',started=time.monotonic())
            states[source]=state
            def progress(percent,message):
                with core._state_lock:state.update(progress=percent,message=message)
            def run():
                try:
                    result=replay_skill_realization(core,dict(sourceHash=source),progress)
                    with core._state_lock:state.update(result,state='complete',progress=100)
                except Exception as exc:
                    with core._state_lock:state.update(state='error',message=str(exc))
                finally:
                    with core._state_lock:
                        for old in list(states):
                            if len(states)<=4:break
                            if old!=source and states[old]['state']!='pending':states.pop(old,None)
            threading.Thread(target=run,name='ReplaySkillRealization',daemon=True).start()
        return {**{k:v for k,v in state.items() if k!='started'},'elapsedSeconds':round(time.monotonic()-state['started'],1)}
