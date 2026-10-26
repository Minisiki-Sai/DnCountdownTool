"""Per-Boss hit-realization scoring. Production scoring uses V2 exclusively."""
from __future__ import annotations
import copy, math, os
from functools import lru_cache
from collections import defaultdict
from dataclasses import replace
from .scoring import canonical_skill, _proportional_damage_fraction, _action_speed_output_multiplier

BASE_RULE_VERSION='v2.6.15-persistent-summon-personal-scope-20261020'
# Set the experiment flag before process startup; cache consumers import this
# token directly, so an experimental run must not reuse normal score caches.
RULE_VERSION=BASE_RULE_VERSION+('-native-speed-experiment' if os.environ.get('DN_SCORE_NATIVE_SPEED_REFERENCE','0')=='1' else '')
TIMELINE_VERSION=20261019
THRESHOLD=1.0

# Opt-in validation only: preserve released behavior until full replanning and
# variable-speed boundary tests have passed. These routes have been compared
# against the same native Hit repository used by make_context, not inferred
# from a player's best observed cast.
NATIVE_SPEED_REFERENCE_SKILLS=frozenset({
    ('黑暗女王','上升重力'),('黑暗女王','重力开关'),
    ('毁灭者','大漩涡怒吼'),
    ('噬魂者','灵魂尖叫'),('黑暗萨满','混沌阵'),('刀锋舞者','烈风痴狂'),
})

def native_speed_reference_enabled():
    return os.environ.get('DN_SCORE_NATIVE_SPEED_REFERENCE','0')=='1'

def active_rule_version():
    return BASE_RULE_VERSION+('-native-speed-experiment' if native_speed_reference_enabled() else '')

def uses_native_speed_reference(rule):
    return bool(native_speed_reference_enabled() and rule
        and (rule.get('job'),rule.get('skill')) in NATIVE_SPEED_REFERENCE_SKILLS
        and not rule.get('actual_only') and not rule.get('burst_only'))

@lru_cache(maxsize=2048)
def _native_speed_ratio(job,sid,name,form,native_weight,speed):
    from .score_hit_evidence import repository
    variants=repository().variants_for(job,sid,name,speed)
    matches=[v for v in variants if v.form==form]
    if len(matches)!=1 or native_weight<=0:return None
    hits=[h for h in matches[0].hits if not h.optional]
    if not hits or any(h.weight is None or not math.isfinite(h.weight) or h.weight<0 for h in hits):return None
    weight=math.fsum(h.weight for h in hits)
    return weight/native_weight if weight>0 else None

def rule_version(payload):
    version=str(payload.get('scoreRuleVersion','v2')).lower()
    if version not in {'v1','v2'}:raise ValueError('未知评分规则版本：'+version)
    # Older callers may still send v1. Migrate their request rather than
    # silently generating legacy scores or breaking automatic scoring.
    return 'v2'

def boss_factor(maximum):
    if not math.isfinite(maximum) or maximum<0:raise ValueError('非法实际倍率')
    # Observed overflow increases actual output only, never the theory ceiling.
    # All misses do not establish that a Boss makes the skill impossible.
    # A zero denominator would remove the missed skill from the theory axis.
    return 1. if maximum<=1e-12 or maximum>=1.0 else maximum

def usable(row):
    return (not row.get('unbound_hits') and row.get('complete') and row.get('v2_tier')in {'A','B'}
        and (row.get('eligible')or row.get('estimated')or (row.get('actual_hits')==0 and row.get('realized_weight')==0)) and float(row.get('expected_weight')or 0)>0
        and row.get('multiplier_rate_percent')is not None)

def native_reference(repo,store,job,sid,name):
    from .score_hit_evidence import addon_root
    addon_root()
    from skill_hit_tool.audit_scope import automatic_damage
    from skill_hit_tool.composition_models import kind
    key=canonical_skill(name);sid=str(sid)
    from .score_body_policy import v1_summon
    actual_only = v1_summon(job,key)
    if key in {'阿兹那之约','阿兹纳之约','暗影分身','暗影召唤术'}:return None
    if automatic_damage(sid,name) and not actual_only:return None
    variants=repo.variants_for(job,sid,name,0)
    if not variants:return None
    v=variants[0]
    if key in {'灵魂之门','灵魂之纸'}:
        # A missing brand is execution loss, not a smaller theoretical route.
        branded=[v for v in variants if '无烙印' not in v.form]
        if branded:
            v=max(branded,key=lambda v:sum(h.weight or 0 for h in v.hits if not h.optional))
    if key=='捆绑射击' and job in {'影舞者','风行者'}:
        v=max(variants,key=lambda v:sum(h.weight or 0 for h in v.hits if not h.optional))
    if key=='召唤黑洞':
        v=max(variants,key=lambda v:sum(h.weight or 0 for h in v.hits if not h.optional))
    hits=[h for h in v.hits if not h.optional]
    # Windmill's intro and blades use different native panels. The approved
    # route uses total cast Hits as a completion proxy over the existing ACT
    # panel, never a separate per-blade quota or a raw sum of mixed weights.
    windmill_panels=(sid=='5114' and key=='风车公主' and len(hits)==31
        and all(h.weight is None for h in hits[:3])
        and all(h.weight is not None and abs(h.weight-.1)<1e-9 for h in hits[3:]))
    if not hits or (any(h.weight is None for h in hits) and not windmill_panels):return None
    weight=sum(h.weight for h in hits if h.weight is not None)
    if weight<=0:return None
    old=float(store.skill_damage_multiplier(job,key if job=="狂战士" and key=="旋转冲击" else name)or 0)
    if old<=0:return None
    full=weight;basis='逐Hit完整路线，与ACT同一面板';uncertain=False
    # Unit bridges are separate from Boss realization. Never sum physical and
    # magical channel weights, then multiply an already summed P+M panel.
    if windmill_panels:
        weight=float(len(hits));full=old;basis='风车公主按单次总Hit比例修正ACT完整面板；31Hit参考，不逐片限制';uncertain=True
    elif key in {'神圣之锤','神圣之锤攻击'} or sid=='3224:basic_attack':
        full=old;basis='神圣之锤沿用技能模拟面板倍率（十字军1.3）；Attack5普通攻击权重不是光锤面板倍率'
    elif key in {'雷神之锤','虔诚'}:
        full=old;basis='物魔通道已分别结算：保留面板倍率，禁止将通道权重和再乘合并面板'
    elif key in {'落雷','重击','神圣十字','审判锤','神圣化身','日食','重锤','勇敢'}:
        full=weight/2;basis='物理/魔法双通道合并面板：逐段权重总和÷2'
    elif key=='连锁闪电':
        weight=full=8.;basis='已确认4轮×4Hit×0.5＝8，替换旧ACT5倍'
    elif sid in {'3202','3355','3224:basic_attack'} or key in {'光雨','黑暗冲浪','圆环强击','日光'}:
        full=old;basis='已确认ACT分通道或描述面板换算；逐Hit完成率另算'
        if key=='黑暗冲浪':weight=16.
    elif job in {'炼金圣士','药剂师'} and key in {'岩浆波流','冰柱释放','冰光束','毒池'}:
        full=weight*2;basis='完整5幻影伤害+100%，逐Hit权重×2'
    elif (job,key) in {('毁灭者','滚动攻击'),('剑皇','直线冲击'),('月之领主','月光碎片'),('魔羽','爆炸箭矢'),('冰灵','冰雾'),('时空领主','能量爆炸'),('火舞','地狱火海'),('火舞','火径'),('黑暗萨满','幽灵之爪'),('黑暗萨满','束缚之爪'),('刀锋舞者','烈风痴狂'),('银色猎人','螺旋飓风'),('银色猎人','飓风狂射')}:
        full=weight*1.15;basis='保留首目标15%面板增益'
    elif (job,key) in {('火舞','火球'),('黑暗女王','上升重力')}:
        full=round(weight,5)*1.15;basis='原生完整路线×首目标1.15；增益只计一次，不改变Hit/原生倍率完成率分母'
    elif job=='黑暗女王' and key=='召唤黑洞':
        full=weight;basis='上升重力后10秒强化黑洞2.7；普通黑洞实战按1.5/2.7折算，理论衔接沿用前置规则'
    elif key=='闪电之爪':
        weight=full=1.3;basis='完整5Hit：0.5+0.5+0.1+0.1+0.1；更长衰减段可超过完整参考'
    elif key=='水月神舞':
        full=old;basis='17.16为含暴击后追加伤害的模拟等效倍率；15为主体原生Hit权重，不额外重复乘暴击追加'
    elif key in {'圣光十字','神圣暴风','神圣风暴'}:
        full=old;basis='神圣大地后10秒强化，窗口外半倍率；原生普通路线与强化面板分别标注，不重复乘强化'
        uncertain=abs(weight*2-old)>1e-4
        if uncertain:basis+='；现有普通路线Hit数与强化完整面板仍有残差，保留面板并列为Hit模型核查项'
    elif key in {'水月神舞','捆绑射击','神圣暴风','圣光十字','毒气暴击','爱情病毒','苦痛之灵','闪电之爪','乒乓爆栗','链锯之塔','炮塔','银色箭雨','追风箭','神圣断言','火球','旋转冲击','挥击','鬼魂踢'} or (job=='黑暗复仇者' and key in {'火焰连击','火焰冲刺','复仇风暴','觉醒','新月之刃','黑暗撞击'}):
        # These already contain resource, transformed-state, projectile, or
        # panel conversions not represented by a simple raw-weight sum.
        # Retain the panel reference, apply observed realization, and expose
        # this limitation instead of silently doubling/halving the output.
        full=old;basis='面板/状态换算待独立闭合：保留v1完整面板值，接入逐次完成率';uncertain=abs(old-weight)>1e-4
    from .score_body_policy import theory_body_sensitive
    if kind(job,sid,name) in {'hacking','hacking_ex','siege','siege_ex'}:full=old;basis='姿态完整参考使用ACT零加速面板；动作速度只改变循环次数'
    if actual_only:
        full=old
        basis='召唤物保留v1完整周期与补召规则；实战按释放快照及召唤物可接收的后续增益结算；获准的体型技能另按Boss降低理论上限'
        uncertain=True
    if abs(full-old)<1e-4:full=old
    return dict(native_weight=weight,full_multiplier=full,old_multiplier=old,full_factor=full/old,actual_only=actual_only,
        body_sensitive=theory_body_sensitive(job,key),separate_panel_reference=windmill_panels,reference_hits=5 if key=='闪电之爪' else len(hits),reference_form='5Hit衰减完整参考' if key=='闪电之爪' else v.form,speed_model=kind(job,sid,name),converted_panel_basis=abs(full-weight)>1e-6,
        basis_note=basis,basis_uncertain=uncertain)

def make_context(report,repo,store):
    groups=defaultdict(list)
    for row in report['cast_rows']:groups[(row['source_id'],row['job'],canonical_skill(row['skill_name']))].append(row)
    actors={};audit=[]
    for (actor,job,key),rows in groups.items():
        first=rows[0];ref=native_reference(repo,store,job,first['skill_id'],first['skill_name'])
        if not ref:
            actors.setdefault(actor,{})[key]=dict(skill=key,job=job,skill_id=first['skill_id'],
                burst_only=True,full_factor=1.,boss_factor=1.,casts={},
                burst_casts={str(int(r['cast_timestamp_ms'])):r.get('burst_parts',[]) for r in rows},
                status='输出倍率保留v1；爆发时刻独立读取UDP和技能时间轴')
            audit.append(dict(source_id=actor,job=job,skill=key,status='完整倍率或面板口径未闭合，保留v1',casts=len(rows)));continue
        native=ref['native_weight'];accepted=[];points={};unknown=0;state_samples=defaultdict(list)
        for row in rows:
            if ref.get('separate_panel_reference') and row.get('baseline_kind')!='windmill_total_cast_hits':unknown+=1;continue
            if not usable(row):unknown+=1;continue
            actual=float(row['realized_weight']);expected=float(row['expected_weight'])
            if not math.isfinite(actual)or actual<0:unknown+=1;continue
            # Fixed-duration speed models have per-cast changing ceilings.
            # Input-route variants keep the common full-route denominator.
            adaptive= 'scythe'in row.get('baseline_kind','') or key in {'捆绑射击','闪电之爪','召唤黑洞'}
            speed_sensitive=abs(_action_speed_output_multiplier(job,key,40)-1)>1e-9 or key=='滚动攻击'
            state_ratio=expected/native if speed_sensitive and not adaptive else 1.
            normalized=actual/(native if adaptive else expected)
            absolute_ratio=normalized*state_ratio
            if job=="狂战士" and key=="旋转冲击":absolute_ratio=actual/native
            accepted.append(normalized)
            if speed_sensitive and not adaptive:
                state_samples[(int(row.get('expected_hits') or 0),round(state_ratio,5))].append(normalized)
            stamp=str(int(row['cast_timestamp_ms']))
            if stamp in points:raise ValueError('重复施放倍率记录：'+actor+'/'+key+'/'+stamp)
            points[stamp]=dict(actual_ratio=absolute_ratio,full_rate=100*normalized,hit_rate=row.get('hit_rate_percent'),
                actual_weight=actual,expected_weight=expected,estimated=bool(row.get('estimated')),
                burst_parts=row.get('burst_parts',[]),external_speed_percent=float(row.get('external_speed_percent')or 0),
                state_ratio=state_ratio,speed_sensitive=speed_sensitive,matched_hits=row.get('matched_hits'),expected_hits=row.get('expected_hits'))
            if ref.get('actual_only'):
                points[stamp]['estimated']=True
                points[stamp]['valuation_note']='按释放快照，允许的召唤物追加后续攻击/属性攻增益；沿用v1周期结算，再按已识别的实际倍率换算'
        calibration=boss_factor(max(accepted))if accepted and ref.get('body_sensitive') else 1.
        # A positive reliable sample is required to reduce the Boss ceiling.
        # All-zero samples remain real zero output against the full ceiling.
        has_reference_sample=bool(accepted and max(accepted)>0)
        entry=dict(ref,normalized_body_ceiling=True,reference_factor=1. if has_reference_sample else ref['old_multiplier']/ref['full_multiplier'],reference_fallback=not has_reference_sample,skill=key,job=job,skill_id=first['skill_id'],boss_factor=calibration,
            max_realization=max(accepted)if accepted else None,casts=points,burst_casts={str(int(r['cast_timestamp_ms'])):r.get('burst_parts',[]) for r in rows},
            calibrated=has_reference_sample,unknown_casts=unknown,status='仅零命中样本，理论参考回退ACT完整倍率；实际零伤害仍计零' if accepted and max(accepted)<=0 else ('倍率口径待闭合；完成率已接入'if ref['basis_uncertain']else'已校准')if accepted else'该Boss无可靠施放，理论参考回退ACT完整倍率')
        if state_samples:
            entry['state_calibrations']=[dict(expected_hits=hits,state_ratio=ratio,
                boss_factor=calibration,casts=len(samples),max_realization=max(samples))
                for (hits,ratio),samples in sorted(state_samples.items())]
            # State ceilings are selected at the actual/theory cast time.
            # Normalize each cast by its own complete speed reference, then
            # share the best normalized geometry ceiling across speed states.
            entry['boss_factor']=calibration
        actors.setdefault(actor,{})[key]=entry
        audit.append(dict(source_id=actor,**{k:v for k,v in entry.items()if k not in {'casts','burst_casts'}},valid_casts=len(points)))
    # No reliable sample on this Boss: preserve the ACT definition locally.
    # Other Bosses with reliable samples keep their own corrected reference.
    actor_jobs={actor:job for actor,job,key in groups}
    if hasattr(store,'_skill_damage_entries'):
        for actor,job in actor_jobs.items():
            for e in store._skill_damage_entries(job):
                key=canonical_skill(e['name'])
                if key in actors.get(actor,{}):continue
                sid={'神圣之锤攻击':'3224:basic_attack','致残惩罚者攻击':'6212:basic_attack'}.get(key,str(e.get('id','')))
                ref=native_reference(repo,store,job,sid,e['name'])
                if ref:
                    entry=dict(ref,normalized_body_ceiling=True,reference_factor=ref['old_multiplier']/ref['full_multiplier'],reference_fallback=True,skill=key,job=job,skill_id=sid,boss_factor=1.,max_realization=None,casts={},calibrated=False,unknown_casts=0,status='该Boss无可靠施放，理论参考回退ACT完整倍率')
                    actors.setdefault(actor,{})[key]=entry
                    audit.append(dict(source_id=actor,**{k:v for k,v in entry.items()if k!='casts'},valid_casts=0))
    return dict(version=active_rule_version(),threshold=THRESHOLD,actors=actors,audit=audit)

def build_override(build,context,source_id,time_offset=0.):
    result=copy.deepcopy(build)
    result['_score_v2_profiles']=copy.deepcopy(context.get('actors',{}).get(str(source_id),{}))
    result['_score_rule_version']=active_rule_version()
    result['_score_v2_time_offset']=time_offset
    if 'player_output_windows' in context:
        result['_score_player_output_windows']=context['player_output_windows'].get(str(source_id),[])
    return result

def override_multiplier(owner,skill,job):
    rows=owner.build.get('_score_v2_profiles',{})
    row=rows.get(canonical_skill(skill))
    if not row or row.get('actual_only') or row.get('burst_only'):return None
    # Replaces the old hand-entered multiplier, before DPA/CD prioritization.
    return row['full_multiplier']*(1. if 'boss_windows' in row or 'state_calibrations' in row else state_factor(row))

def attach_context(plans,context):
    for actor,plan in plans.items():
        plan['_score_v2']=dict(version=active_rule_version(),profiles=copy.deepcopy(context.get('actors',{}).get(actor,{})))
        if 'player_output_windows' in context:plan['_score_v2']['player_output_windows']=context['player_output_windows'].get(str(actor),[])
        if 'support_boss_scope' in context:plan['_score_v2']['support_boss_scope']=context['support_boss_scope']

def value_actual(rows,profiles,plan,job):
    rules=(plan or {}).get('_score_v2',{}).get('profiles',{})
    if not rules:return rows
    result=[]
    for row in rows:
        rule=rules.get(row.key);point=rule and rule['casts'].get(str(round(row.seconds*1000)))
        if not point:
            # This row was valued with the Boss-adjusted theory profile.
            # Undo that adjustment for unknown evidence; fallback must not
            # inherit a denominator calibrated from a different cast.
            undo=(factor_at(rule,row.seconds,row.action_speed_output_multiplier)*rule['full_factor'])if rule else 1.
            result.append(replace(row,power=row.power/max(undo,1e-12))if undo>0 and abs(undo-1)>1e-12 else row)
            continue
        if point['actual_ratio']==0:
            result.append(replace(row,power=0.));continue
        if rule.get('actual_only'):
            # Replay power already includes early removal/replacement and output
            # overlap. Undo that nominal lifetime fraction before substituting
            # the observed full-cycle Hit fraction, otherwise misses count twice.
            # Buff integration inside the used lifetime remains a conditional
            # estimate; do not turn a zero-lifetime replay into invented damage.
            fraction=max(0.,float(row.utilization_ratio))
            if fraction>1e-9:
                result.append(replace(row,power=row.power/fraction*point['actual_ratio']))
            else:
                result.append(row)
            continue
        # Undo the nominal speed count before using the
        # observed native weight, whose count already includes actual speed.
        fraction=1. # V2 actual valuation bypasses duration loss before this step.
        nominal_speed=row.action_speed_output_multiplier if point['speed_sensitive']else 1.
        denom=factor_at(rule,row.seconds,row.action_speed_output_multiplier)*max(1e-12,nominal_speed)*max(1e-12,fraction)
        base=row.power/denom if row.power>0 else profiles[row.key].power/max(rule['boss_factor'],1e-12)
        result.append(replace(row,power=base*point['actual_ratio']))
    return result

def detail_for(plan,actual_casts):
    rules=(plan or {}).get('_score_v2',{}).get('profiles',{})
    if not rules:return {}
    linked=unknown=estimated=legacy=0;extras=low=0;details=[]
    for cast in actual_casts:
        r=rules.get(cast.key);p=r and r['casts'].get(str(round(cast.seconds*1000)))
        if not r:legacy+=1;continue
        if not p:unknown+=1;continue
        estimated+=bool(p.get('estimated'));linked+=1;extras+=p['full_rate']>100+1e-8;low+=p['full_rate']<90-1e-8
        details.append(dict(skill=cast.key,seconds=cast.seconds,
                            **{k:v for k,v in p.items() if k!='burst_parts'},
                            boss_rate=p['full_rate']/factor_at(r,cast.seconds,p['state_ratio'])))
    return dict(version=active_rule_version(),linked_casts=linked,estimated_casts=estimated,unknown_casts=unknown,legacy_casts=legacy,unknown_policy="保留旧完整参考；不把证据未知按零命中扣分",burst_policy="按技能释放时刻归属爆发窗口，整次技能标准伤害参与比较；显示可超过100%，计分封顶100%",extra_casts=extras,low_casts=low,
        skills=[{k:v for k,v in r.items()if k not in {'casts','burst_casts'}}for r in rules.values()],casts=details)


def state_factor(profile, state_ratio=1.):
    states=profile.get('state_calibrations',())
    reference=float(profile.get('reference_factor',1.))
    if profile.get('normalized_body_ceiling') or not states:return reference*float(profile.get('boss_factor',1.))
    closest=min(states,key=lambda s:abs(s['state_ratio']-state_ratio))
    # Do not treat one observed speed ceiling as evidence for an unseen state.
    return reference*(float(closest['boss_factor']) if abs(closest['state_ratio']-state_ratio)<.025 else 1.)

def factor_at(profile, seconds, state_ratio=1.):
    """Time-dependent Boss calibration; never changes cooldown state."""
    if not profile:return 1.
    windows=profile.get('boss_windows')
    if windows is None:return state_factor(profile,state_ratio)
    matches=[w for w in windows if w['start'] <= seconds < w['end']]
    # Windows overlap only after a same-cast multi-Boss witness establishes
    # coexistence. Choose the higher available ceiling, never sum axes.
    return max((state_factor(w,state_ratio) for w in matches),default=1.)

def timeline_factor(owner, skill, seconds, state_ratio=1.):
    profile=owner.build.get('_score_v2_profiles',{}).get(canonical_skill(skill))
    # Single-Boss profiles already include their factor in the base panel.
    return factor_at(profile,seconds+float(owner.build.get('_score_v2_time_offset',0)),state_ratio) if profile and ('boss_windows' in profile or 'state_calibrations' in profile) else 1.

def merge_contexts(contexts):
    """Merge calibration evidence, never independently scheduled axes."""
    actors={};audit=[]
    for boss_key,boss_name,windows,context in contexts:
        for actor,skills in context['actors'].items():
            for key,profile in skills.items():
                target=actors.setdefault(actor,{}).setdefault(key,dict(
                    copy.deepcopy(profile),boss_factor=1.,casts={},boss_windows=[]))
                for start,end in windows:
                    target['boss_windows'].append(dict(start=start,end=end,boss_key=boss_key,
                        boss_name=boss_name,boss_factor=profile['boss_factor'],reference_factor=profile.get('reference_factor',1.),reference_fallback=profile.get('reference_fallback',False),normalized_body_ceiling=profile.get('normalized_body_ceiling',False),state_calibrations=copy.deepcopy(profile.get('state_calibrations',[])),valid_casts=len(profile['casts'])))
                for stamp,point in profile['casts'].items():
                    if stamp in target['casts']:
                        # A multi-boss cast cannot be assigned two independent weights.
                        target.setdefault('ambiguous_casts',[]).append(stamp)
                        target['casts'].pop(stamp,None)
                    elif stamp not in target.get('ambiguous_casts',[]):target['casts'][stamp]=copy.deepcopy(point)
                audit.append(dict(source_id=actor,boss=boss_name,**{k:v for k,v in profile.items() if k not in {'casts','burst_casts'}},valid_casts=len(profile['casts'])))
    return dict(version=active_rule_version(),threshold=THRESHOLD,scope='continuous_full_run',actors=actors,audit=audit)


def speed_output_factor(rule, speed, fallback=1.):
    """Use the same full-route frame model as per-cast Hit evidence."""
    if uses_native_speed_reference(rule):
        ratio=_native_speed_ratio(rule['job'],str(rule['skill_id']),rule['skill'],
            rule['reference_form'],float(rule['native_weight']),max(0.,float(speed)))
        if ratio is not None:return ratio
    model=(rule or {}).get('speed_model','')
    if model not in {'flash','hacking','hacking_ex','siege','siege_ex'}:return fallback
    from .score_hit_evidence import addon_root
    addon_root()
    from skill_hit_tool.composition_models import maximum
    return maximum(model,speed)[1]/maximum(model,0)[1]



def actual_reference_attrs(build, skill, seconds, attrs):
    """Value confirmed Shock Hand hits once: reference panel, then hit ratio.

    Theory keeps its own live-relic state. Unknown casts retain legacy replay.
    """
    if canonical_skill(skill)=='旋转冲击':
        profile=build.get('_score_v2_profiles',{}).get('旋转冲击',{})
        stamp=str(round((seconds+float(build.get('_score_v2_time_offset',0)))*1000))
        if stamp in profile.get('casts',{}):return dict(attrs, **{'__怒气':6.0})
        return attrs
    if canonical_skill(skill) != '冲击之手':return attrs
    profile=build.get('_score_v2_profiles',{}).get('冲击之手',{})
    stamp=str(round((seconds+float(build.get('_score_v2_time_offset',0)))*1000))
    if stamp not in profile.get('casts',{}):return attrs
    return dict(attrs, **{'__圣物数量':3.0})
