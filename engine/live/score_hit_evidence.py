"""Shared Hit-tool evidence for v2 scores, cached by capture and target."""
from pathlib import Path
import hashlib,json,os,time,threading

_EVIDENCE_LOCKS = {}
_EVIDENCE_LOCK_GUARD = threading.Lock()

def evidence_lock(key):
    with _EVIDENCE_LOCK_GUARD:
        return _EVIDENCE_LOCKS.setdefault(key, threading.RLock())

from functools import lru_cache

def module_evidence(path):
    """Use shipped bytecode in frozen builds; source in development."""
    path = Path(path)
    if not path.is_file():
        path = path.with_suffix('.pyc')
    return path.read_bytes()

@lru_cache(maxsize=1)
def addon_root():
    import sys
    base = Path(__file__).resolve().parents[2]
    candidates = [base / 'third_party/skill_hit_tool/app', base]
    for candidate in candidates:
        if (candidate / 'skill_hit_tool/completion_report.py').is_file():
            if str(candidate) not in sys.path:
                sys.path.append(str(candidate))
            return candidate
    raise RuntimeError('缺少技能Hit工具组件，请安装技能Hit工具后重试')

def root():
    return Path(os.environ.get('DN_SCORE_V2_HOME', Path.home() / 'Documents/DragonNestDpsCapture/score_hit_evidence'))

@lru_cache(maxsize=1)
def repository():
    base = addon_root()
    from act_hit_timeline_comparator.core import TimelineRepository
    from skill_timeline_viewer.resource_paths import default_timeline_path
    return TimelineRepository(default_timeline_path())

@lru_cache(maxsize=1)
def evidence_version():
    from .version_fingerprint import replay_decoder_fingerprint
    digest = hashlib.sha256()
    base = addon_root()
    for directory in ('skill_hit_tool', 'act_hit_timeline_comparator'):
        for path in sorted((base / directory).rglob('*.py')):
            digest.update(str(path.relative_to(base)).encode())
            digest.update(path.read_bytes())
    from skill_timeline_viewer.resource_paths import default_timeline_path
    digest.update(default_timeline_path().read_bytes())
    digest.update(replay_decoder_fingerprint(str(Path(__file__).resolve().parents[1] / 'data')).encode())
    digest.update(module_evidence(Path(__file__).resolve().parents[1] / 'hit_analyzer/analyzer.py'))
    digest.update(b'completion-evidence-v2.2')
    return digest.hexdigest()

@lru_cache(maxsize=1)
def decode_version():
    """Raw evidence depends on the decoder/collectors, not scoring or fitting."""
    from .version_fingerprint import replay_decoder_fingerprint
    base=addon_root();digest=hashlib.sha256(b'hit-evidence-raw-v1')
    digest.update(module_evidence(Path(__file__).with_name('replay_hit_evidence.py')))
    digest.update(replay_decoder_fingerprint(str(Path(__file__).resolve().parents[1]/'data')).encode())
    for name in ('projectile_completion.py','holy_hammer_lifecycle.py','hit_ledger.py'):
        digest.update((base/'skill_hit_tool'/name).read_bytes())
    return digest.hexdigest()

def decode_signature(path):
    from .team_score_sync import capture_content_hash
    return hashlib.sha256((capture_content_hash(Path(path))+decode_version()).encode()).hexdigest()

def signature(path, targets):
    from .team_score_sync import capture_content_hash
    content = capture_content_hash(Path(path))
    key = hashlib.sha256(json.dumps([content, sorted(targets) if targets is not None else 'all-targets', evidence_version()], ensure_ascii=False).encode()).hexdigest()
    return content, key

def load_report(path, targets, store, progress=lambda *args: None, raw_bundle=None, snapshot=None):
    import uuid
    addon_root()
    began = time.perf_counter()
    content, key = signature(path, targets)
    cache = root() / 'evidence'; cache.mkdir(parents=True, exist_ok=True)
    destination = cache / (key + '.json')
    def read():
        try:
            report=json.loads(destination.read_bytes())
            return report if isinstance(report.get('cast_rows'),list) else None
        except (OSError,ValueError,AttributeError):return None
    # Completed results must never wait behind another Boss's expensive fit.
    report=read();cached=report is not None
    if report is None:
        lock=evidence_lock(key)
        while not lock.acquire(timeout=.5):progress(1,'正在复用同一抓包的逐Hit分析，请稍候')
        try:
            report=read();cached=report is not None
            if report is None:
                report = compute_report(path, targets, store, progress, raw_bundle=raw_bundle,snapshot=snapshot)
                temporary = cache / (key + '.' + uuid.uuid4().hex + '.tmp')
                temporary.write_text(json.dumps(report, ensure_ascii=False), 'utf8')
                os.replace(temporary, destination)
        finally:lock.release()
    return report, key, dict(cached=cached, evidence_seconds=time.perf_counter()-began, casts=len(report['cast_rows']))

def prepare_context(path, targets, store, progress=lambda *args: None, raw_bundle=None, windows=None, snapshot=None, include_end=True, positive_windows=None):
    from .score import make_context, RULE_VERSION
    began = time.perf_counter()
    report, key, diagnostics = load_report(path, targets, store, progress, raw_bundle, snapshot=snapshot)
    if windows is not None:
        ordinary=scope_report(report, windows, include_end=include_end)['cast_rows']
        if positive_windows is not None:
            # Keep verified cross-boundary casts against this target, even
            # when their cast begins in the preceding phase's padding.
            extra=scope_report(report,positive_windows,include_end=False)['cast_rows']
            ids={id(row)for row in ordinary}
            ordinary=ordinary+[row for row in extra if id(row)not in ids and float(row.get('realized_weight') or 0)>0]
        report=dict(report,cast_rows=ordinary)
    context = make_context(report, repository(), store)
    context['realization_cast_rows'] = report['cast_rows']
    context['fingerprint'] = hashlib.sha256((key + RULE_VERSION + repr((windows,include_end,positive_windows))).encode() + module_evidence(Path(__file__).with_name('score.py')) + module_evidence(Path(__file__).with_name('score_body_policy.py'))).hexdigest()
    context['diagnostics'] = dict(diagnostics, total_seconds=time.perf_counter()-began, skills=len(context['audit']))
    return context


def scope_report(report, windows, include_end=True):
    """Off-Boss casts are not misses against this Boss, nor baseline evidence."""
    return dict(report, cast_rows=[row for row in report['cast_rows']
        if any(start <= row['cast_timestamp_ms']/1000 and (row['cast_timestamp_ms']/1000 <= end if include_end else row['cast_timestamp_ms']/1000 < end) for start,end in windows)])


def _prepare_shared_evidence(path,store,progress,raw_bundle=None,snapshot=None):
    from types import SimpleNamespace
    from .sources import V576DiagnosticReplaySource
    from .aggregator import CombatAggregator
    from engine.hit_analyzer.analyzer import SkillHitAnalyzer
    from act_hit_timeline_comparator.app import _capture_action_speed_by_cast,merge_followup_damage_observations,rebalance_rapid_recast_observations
    from skill_hit_tool.projectile_completion import ProjectileEvidenceCollector,annotate_observations
    from skill_hit_tool.hit_ledger import entity_births_from_source,reconcile
    from skill_hit_tool.composition_models import rebuild_observations
    from skill_hit_tool.field_pulse_evidence import reconcile_field_pulses
    from skill_hit_tool.lifecycle_completion import annotate_lifecycles
    from skill_hit_tool.completion_report import compare_all_skills
    # Reuse one verified decode across Boss selections; target-specific analysis stays separate.
    import pickle, zlib, uuid
    progress(2,'正在恢复逐Hit原始证据缓存')
    decode_key = decode_signature(path)
    decode_dir = root() / 'decoded'; decode_dir.mkdir(parents=True, exist_ok=True)
    decode_path = decode_dir / (decode_key + '.bin')
    # One shared raw decode; independent target fits may proceed afterwards.
    with evidence_lock('decode:'+decode_key):
        if raw_bundle is None and decode_path.is_file():
            try:
                raw_bundle = pickle.loads(zlib.decompress(decode_path.read_bytes()))
            except (OSError, ValueError, TypeError, pickle.PickleError, zlib.error, EOFError, AttributeError):
                raw_bundle = None
        if raw_bundle:
            decoded=raw_bundle['decoded'];births=raw_bundle['births'];end=raw_bundle['end']
            proofs=raw_bundle['proofs'];registrations=raw_bundle['registrations'];life=raw_bundle['life']
        else:
            source=V576DiagnosticReplaySource(Path(path),Path(__file__).resolve().parents[1]/'data', progress_callback=lambda done,total:progress(2,f'逐Hit归因：读取原始抓包 {done}/{total}'))
            collector=ProjectileEvidenceCollector(source)
            try:
                decoded=source.poll();births=entity_births_from_source(source);end=int(source.capture_end_timestamp_ms or 0)
            finally:
                proofs=collector.finish();registrations=collector.finish_registrations();life=collector.hammer.export();source.close()
        if not raw_bundle:
            bundle = dict(decoded=decoded, births=births, end=end, proofs=proofs, registrations=registrations, life=life)
            temporary = decode_dir / (decode_key + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(zlib.compress(pickle.dumps(bundle, protocol=5), level=1))
            os.replace(temporary, decode_path)
    progress(15,'正在复用已载入的战斗快照' if snapshot is not None else '正在汇总逐Hit战斗事件')
    if snapshot is None:
        aggregator=CombatAggregator()
        for event in decoded:aggregator.ingest(event)
        snapshot=aggregator.snapshot(include_events=True)
    casts=snapshot.skill_cast_events
    events,_=reconcile_field_pulses(snapshot.combat_events,decoded,casts,proofs)
    progress(25,'正在核对攻速与技能施放')
    analyzer=SkillHitAnalyzer(store);repo=repository();speed=_capture_action_speed_by_cast(decoded,casts,store)
    progress(32,'正在绑定技能与实际命中')
    obs=analyzer.analyze_events(casts,events,Path(path).name,capture_end_ms=end)
    obs=merge_followup_damage_observations(obs,repo);obs=rebalance_rapid_recast_observations(obs,repo,speed)
    obs=rebuild_observations(obs,events,casts,end)
    from skill_hit_tool.circle_swing_completion import annotate
    obs=annotate(obs,casts)
    return dict(obs=obs,events=events,decoded=decoded,casts=casts,end=end,births=births,proofs=proofs,registrations=registrations,life=life,speed=speed)

_PREPARED = {}  # One immutable-by-convention capture; fits receive private observations.
_PREPARED_GUARD = threading.RLock()

@lru_cache(maxsize=16)
def _prepared_code_signature(code):
    # Code objects are immutable. marshal includes reference/interning flags,
    # so dumping the same object after execution can produce different bytes.
    # Freeze its signature once; replacement code still gets a distinct key.
    import marshal
    return marshal.dumps(code)


def prepared_evidence(path,store,progress,raw_bundle=None,snapshot=None):
    import pickle,zlib,uuid,marshal
    code = _prepare_shared_evidence.__code__
    code_bytes = (_prepared_code_signature(code) if os.environ.get('DN_SCORE_EXACT_ROUND2', '1') == '1'
                  else marshal.dumps(code))
    key=hashlib.sha256(signature(path,None)[1].encode()+code_bytes).hexdigest()
    with _PREPARED_GUARD:
        if key in _PREPARED:return _PREPARED[key]
    lock=evidence_lock('prepared:'+key)
    while not lock.acquire(timeout=.5):progress(2,'正在复用同一抓包的公共命中证据')
    try:
        with _PREPARED_GUARD:
            if key in _PREPARED:return _PREPARED[key]
        directory=root()/'prepared';directory.mkdir(parents=True,exist_ok=True)
        path_cache=directory/(key+'.bin');data=None
        if path_cache.is_file():
            try:data=pickle.loads(zlib.decompress(path_cache.read_bytes()))
            except (OSError,ValueError,TypeError,pickle.PickleError,zlib.error,EOFError,AttributeError):pass
        if not isinstance(data,dict) or 'obs' not in data:
            data=_prepare_shared_evidence(path,store,progress,raw_bundle,snapshot)
            temp=directory/(key+'.'+uuid.uuid4().hex+'.tmp')
            temp.write_bytes(zlib.compress(pickle.dumps(data,protocol=5),1));os.replace(temp,path_cache)
        with _PREPARED_GUARD:
            _PREPARED.clear();_PREPARED[key]=data
        return data
    finally:lock.release()

def compute_report(path,targets,store,progress,raw_bundle=None,snapshot=None):
    import copy
    from engine.hit_analyzer.analyzer import SkillHitAnalyzer
    from skill_hit_tool.hit_ledger import reconcile
    from skill_hit_tool.projectile_completion import annotate_observations
    from skill_hit_tool.lifecycle_completion import annotate_lifecycles
    data=prepared_evidence(path,store,progress,raw_bundle,snapshot)
    # Mutable analysis_metadata is copied; source events and raw packet evidence
    # are read-only inputs. Target fits must not overwrite another Boss's work.
    obs=copy.deepcopy(data['obs'])
    events=data['events'];decoded=data['decoded'];casts=data['casts'];end=data['end']
    births=data['births'];proofs=data['proofs'];registrations=data['registrations'];life=data['life'];speed=data['speed']
    analyzer=SkillHitAnalyzer(store);repo=repository()
    progress(45,'正在核对弹体与多段命中归属')
    obs=reconcile(obs,events,analyzer,repo,end,target_filter=lambda e:targets is None or e.target_id in targets,entity_births=births,object_proofs=proofs,hammer_lifecycle=life)
    obs=annotate_observations(obs,proofs,events,decoded,registrations);obs=annotate_lifecycles(obs,decoded,casts,end)
    progress(55,'逐Hit归因完成，正在匹配各段倍率')
    from .hit_parallel import compare_parallel
    report=compare_parallel(obs,repo,speed,progress=lambda done,total:progress(55+round(43*done/max(1,total)),f'正在并行匹配各技能倍率 {done}/{total}'))
    return report


def prepare_continuous_context(path, encounters, store, progress=lambda *args:None, snapshot=None):
    from .score import merge_contexts, make_context, RULE_VERSION
    contexts=[];fingerprints=[]
    all_targets=frozenset(t for e in encounters for t in e.target_ids)
    report,actual_key,actual_diagnostics=load_report(path,all_targets,store,lambda p,m:progress(3,m),snapshot=snapshot)
    from .calibration_boss_scope import encounter_windows,cast_bosses,player_boss_windows
    owned_windows,phase_diagnostics=encounter_windows(encounters,report,snapshot)
    for index,encounter in enumerate(encounters):
        progress(3,f'正在校准 Boss {index+1}/{len(encounters)}：{encounter.name}')
        context=prepare_context(path,frozenset(encounter.target_ids),store,
            lambda p,m:progress(3,m),windows=None,snapshot=snapshot,include_end=False)
        contexts.append((str(index),encounter.name,owned_windows[index],context))
        fingerprints.append(context['fingerprint'])
    result=merge_contexts(contexts)
    # Reuse fitted target-specific hits; compute shared simultaneous-Boss
    # ceilings before projecting per-Boss means. Never average these Boss
    # means into a whole-run score (a co-hit cast may appear in both rows).
    from .skill_realization import summarize_report
    realization_casts=[]
    for boss_key,boss_name,_,context in contexts:
        for row in context.get('realization_cast_rows',()):
            stamp=row['cast_timestamp_ms']/1000
            active={str(i) for i,windows in enumerate(owned_windows) if any(a<=stamp<b for a,b in windows)}
            active.add(boss_key)
            realization_casts.append(dict(row,boss_key=boss_key,boss_name=boss_name,is_boss_cast=True,active_boss_keys=sorted(active)))
    summaries={(r['source_id'],r['skill']):r for r in summarize_report(dict(cast_rows=realization_casts))}
    for actor,profiles in result['actors'].items():
        for skill,profile in profiles.items():
            available={r['boss_key']:r for r in summaries.get((actor,skill),{}).get('boss_rows',())}
            profile['boss_realizations']=[available.get(boss_key,dict(boss_key=boss_key,boss=boss_name,
                hit_boss_rate=None,multiplier_boss_rate=None,valid_casts=0,total_records=0,estimated_casts=0))
                for boss_key,boss_name,_,context in contexts]
    personal,membership=player_boss_windows(encounters,report,owned_windows,phase_diagnostics['cohit_boss_pairs'],with_membership=True,snapshot=snapshot)
    from .score_burst import support_window_fallback
    fallback_actors=support_window_fallback(snapshot, personal, membership, owned_windows)
    result['support_window_fallback_actors']=fallback_actors
    result['player_output_windows']=personal
    from .support_boss_scope import make_scope
    result['support_boss_scope']=make_scope(encounters,membership,phase_diagnostics['cohit_boss_pairs'])
    # Boss coefficients follow each player's target group, including 4+4
    # simultaneous isolated encounters. Same-field groups retain overlapping
    # ceilings and factor_at selects the higher live Boss ceiling.
    for actor,profiles in result['actors'].items():
        for key,profile in profiles.items():
            profile['boss_windows']=[]
            for index,(boss_key,boss_name,_,context) in enumerate(contexts):
                ref=context['actors'].get(actor,{}).get(key,{})
                for start,end in membership.get(actor,{}).get(index,[]):
                    profile['boss_windows'].append(dict(start=start,end=end,boss_key=boss_key,boss_name=boss_name,
                        boss_factor=ref.get('boss_factor',1.),reference_factor=ref.get('reference_factor',profile.get('old_multiplier',1.)/profile.get('full_multiplier',1.)),reference_fallback=ref.get('reference_fallback',True),normalized_body_ceiling=ref.get('normalized_body_ceiling',False),
                        state_calibrations=ref.get('state_calibrations',[]),valid_casts=len(ref.get('casts',{}))))
    for player in getattr(snapshot,'players',()):result['player_output_windows'].setdefault(str(player.source_id),[])
    # One actual cast must be valued once against all scoring targets. Phase
    # padding and casts flying into the next Boss cannot choose its target.
    report=scope_report(report,[w for e in encounters for w in e.phase_windows],include_end=False)
    actual_context=make_context(report,repository(),store)
    for actor,profiles in result['actors'].items():
        for skill,profile in profiles.items():
            profile['casts']=actual_context['actors'].get(actor,{}).get(skill,{}).get('casts',{})
            profile['burst_casts']=actual_context['actors'].get(actor,{}).get(skill,{}).get('burst_casts',{})
            # A cast hitting simultaneous bosses uses the strongest evidenced
            # single-target weight. It is never counted once per boss.
            for row in report['cast_rows']:
                if row['source_id']!=actor:continue
                from .scoring import canonical_skill
                if canonical_skill(row['skill_name'])!=skill or len(cast_bosses(row,encounters))<2:continue
                stamp=str(int(row['cast_timestamp_ms']))
                points=[c[-1]['actors'].get(actor,{}).get(skill,{}).get('casts',{}).get(stamp) for c in contexts]
                points=[p for p in points if p is not None]
                if points:
                    profile['casts'][stamp]=max(points,key=lambda p:p['actual_ratio'])
                    profile['burst_casts'][stamp]=profile['casts'][stamp].get('burst_parts',[])
    fingerprints.append(actual_key)
    result['fingerprint']=hashlib.sha256(
        json.dumps([RULE_VERSION,fingerprints],ensure_ascii=False).encode()
        + b''.join(module_evidence(Path(__file__).with_name(name)) for name in
                   ('calibration_boss_scope.py','support_boss_scope.py','duty_groups.py'))).hexdigest()
    result['diagnostics']=dict(scope='continuous_full_run',boss_count=len(contexts),
        cooldown_policy='single_v1_timeline',phase_ownership=phase_diagnostics,actual_evidence=actual_diagnostics,calibration=[c[-1]['diagnostics'] for c in contexts])
    return result


def exclusive_encounter_windows(encounters):
    """Nested mechanic phases own their interval; genuine overlap stays unknown."""
    intervals=[];concurrent=set()
    for i,e in enumerate(encounters):
        for start,end in e.phase_windows:
            a,b=float(start),float(end)
            first=float(getattr(e,'first_ms',a*1000))/1000
            last=float(getattr(e,'last_ms',b*1000))/1000
            for j,parent in enumerate(encounters):
                if i==j or not any(c<=start and end<=d and (c<start or end<d)for c,d in parent.phase_windows):continue
                activity=[float(t)for t,damage in getattr(parent,'damage_series',())if damage>0]
                before=[t for t in activity if a<=t<first]
                after=[t for t in activity if last<t<b]
                if before:a=max(a,min(first,max(before)+1.))
                if after:b=min(b,max(last,min(after)))
                if any(first<=t<=last for t in activity):concurrent.add((i,j))
            if b>a:intervals.append((a,b,i))
    boundaries=sorted({t for a,b,i in intervals for t in (a,b)})
    result=[[] for _ in encounters];nested=ambiguous=0.
    for left,right in zip(boundaries,boundaries[1:]):
        middle=(left+right)/2
        active=[w for w in intervals if w[0]<=middle<w[1]]
        owners={w[2]for w in active}
        if not owners:continue
        if len(owners)==1:owner=next(iter(owners))
        else:
            candidates={i for a,b,i in active if all(i==j or ((i,j) not in concurrent and a>=c and b<=d and (a>c or b<d)) for c,d,j in active)}
            if len(candidates)!=1:
                ambiguous+=right-left;continue
            owner=next(iter(candidates));nested+=right-left
        rows=result[owner]
        if rows and abs(rows[-1][1]-left)<1e-8:rows[-1]=(rows[-1][0],right)
        else:rows.append((left,right))
    return result,dict(nested_phase_seconds=nested,ambiguous_overlap_seconds=ambiguous,
        policy='nested_phase_priority; true_overlap_keeps_full_reference')
