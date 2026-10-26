"""An independent, bounded pool for Hit evidence, never the score-axis pool."""
from collections import defaultdict, Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import atexit, multiprocessing, os, threading
_POOL=None
_LOCK=threading.Lock()

def worker_limit():
    try: requested=int(os.environ.get('DN_HIT_WORKERS','8'))
    except ValueError: requested=8
    return max(1,min(8,requested,os.cpu_count() or 1))

def _initialize():
    from .score_hit_evidence import addon_root
    addon_root()
    from engine.timeline_parallel import _set_worker_process_low_priority
    _set_worker_process_low_priority()

def _compare(rows, speeds, context=None):
    from .score_hit_evidence import repository
    from skill_hit_tool.completion_report import compare_all_skills
    return compare_all_skills(rows,repository(),speeds,context_observations=context)

def shutdown():
    global _POOL
    with _LOCK: pool,_POOL=_POOL,None
    if pool is not None:pool.shutdown(wait=True,cancel_futures=True)
atexit.register(shutdown)

def compare_parallel(observations, repository, speeds, progress=None, *, group_by_skill=True):
    global _POOL
    rows=list(observations); groups=defaultdict(list)
    for row in rows:groups[str(row.source_id)].append(row)
    if worker_limit()==1 or not rows:
        from skill_hit_tool.completion_report import compare_all_skills
        return compare_all_skills(rows,repository,speeds,progress=progress)
    with _LOCK:
        if _POOL is None:
            _POOL=ProcessPoolExecutor(max_workers=worker_limit(),mp_context=multiprocessing.get_context('spawn'),initializer=_initialize)
        pool=_POOL
    from skill_hit_tool.route_components import annotate_route_components
    from skill_hit_tool.completion_report import _skill_id
    tasks=[]
    for actor, group in groups.items():
        if not group_by_skill:
            tasks.append((group,{k:v for k,v in speeds.items() if k[0]==actor},None))
            continue
        context=annotate_route_components(group)
        by_skill=defaultdict(list)
        for row in context:
            # Keep aliases of one native skill together: effective-rate
            # summaries intentionally inspect every cast with that skill ID.
            by_skill[_skill_id(row.skill_id)].append(row)
        actor_speeds={k:v for k,v in speeds.items() if k[0]==actor}
        for subset in by_skill.values():tasks.append((subset,actor_speeds,context))
    # Put the largest fitting workloads first to reduce the long final tail.
    tasks.sort(key=lambda task:sum(len(o.hit_times)**2 for o in task[0]),reverse=True)
    futures={pool.submit(_compare,*task):len(task[0]) for task in tasks}
    reports=[];done=0
    for future in as_completed(futures):
        reports.append(future.result());done+=futures[future]
        if progress:progress(done,len(rows))
    first=reports[0]
    result={k:v for k,v in first.items() if k not in {'cast_rows','skill_rows','observed_damage_casts','eligible_casts','excluded_non_player_jobs','issue_casts'}}
    casts=[r for report in reports for r in report['cast_rows']]
    order={(str(o.source_id),int(o.cast_timestamp_ms),str(o.skill_id)):i for i,o in enumerate(rows)}
    casts.sort(key=lambda r:order.get((r['source_id'],r['cast_timestamp_ms'],r['skill_id']),len(rows)))
    skills=[r for report in reports for r in report['skill_rows']]
    skill_order={}
    for o in rows:
        key=(str(o.source_id),str(o.source_name),str(o.job),_skill_id(o.skill_id),str(o.skill_name))
        skill_order.setdefault(key,len(skill_order))
    skills.sort(key=lambda r:(r['source_name'],r['job'],-r['actual_hits_all'],r['skill_name'],
        skill_order.get((r['source_id'],r['source_name'],r['job'],r['skill_id'],r['skill_name']),len(rows))))
    result.update(cast_rows=casts,skill_rows=skills,observed_damage_casts=len(casts),eligible_casts=sum(r['eligible_casts'] for r in skills))
    for key in ('excluded_non_player_jobs','issue_casts'):
        counts=Counter()
        for report in reports:counts.update(report[key])
        result[key]=dict(counts.most_common())
    return result
