"""Reborn 2026-10-17 warrior native actions; never infer form from damage."""
from dataclasses import replace
from .models import SkillCastEvent

ACTION_FORMS = {366: 'normal', 367: 'normal', 376: 'true', 377: 'true'}
LABELS = {'normal': '旋转冲击（普通）', 'true': '真旋转冲击'}

def apply_records(state, records, now, events):
    output = list(events)
    for e in events:
        if isinstance(e, SkillCastEvent) and e.job in {'狂战士','战神','战士',''} and str(e.skill_id) in {'407','505'}:
            state[e.source_id] = (e, '')
    for kind, offset, record in records:
        if kind != 3 or len(record) not in {44,48}:continue
        action = int.from_bytes(record[7:9], 'little')
        if action not in ACTION_FORMS:continue
        actor = f"0x{int.from_bytes(record[:4], 'little'):08X}"
        if actor not in state:continue
        cast, previous = state[actor]
        if not 0 <= now-cast.timestamp_ms <= 6000:continue
        form = ACTION_FORMS[action]
        if previous == form or previous == 'conflict':continue
        if previous and previous != form:form = 'conflict'
        evidence = f'服务器原生动作 {action}；Reborn warrior ACT 2026-10-17'
        output.append(replace(cast, packet_kind='skill_form_confirmation',
            display_name=LABELS.get(form,'旋转冲击（形态证据冲突）'),
            variant_action_id=action, variant_evidence=evidence, variant_key=form))
        state[actor] = (cast, form)
    for actor,(cast,_) in list(state.items()):
        if now-cast.timestamp_ms > 6000:state.pop(actor,None)
    return output

def finalize_packet(decoder, packet, events):
    from .player_root_action_objects import _typed_records
    state = getattr(decoder, '_circle_swing_forms', None)
    if state is None:state={};decoder._circle_swing_forms=state
    if not state and not any(isinstance(e,SkillCastEvent) and e.job in {'狂战士','战神','战士',''} and str(e.skill_id) in {'407','505'} for e in events):return events
    now=round((packet.timestamp_seconds-decoder.first_timestamp)*1000)
    return apply_records(state, _typed_records(packet.payload,packet.inferred_direction()),now,events)

def label_score_axes(actual, theory, casts, actor, plan, job=None):
    known={e.timestamp_ms:e for e in casts if e.source_id==actor and e.job in {'狂战士','战神','战士',''} and str(e.skill_id) in {'407','505'}}
    if job is not None and job != '狂战士':return actual,theory
    def circle(name):return name.replace('EX','').strip()=='旋转冲击'
    actual=tuple(replace(r,display_name=getattr(known.get(round(r.seconds*1000)),'display_name','') or '旋转冲击（形态未确认）') if circle(r.skill_name) else r for r in actual)
    plan=plan or {};offset=float(plan.get('_score_time_offset',0))
    labels={round((float(s.get('start',0))+offset)*1000):s['display_name'] for s in plan.get('segments',()) if s.get('display_name')}
    theory=tuple(replace(r,display_name=labels.get(round(r.seconds*1000),'旋转冲击（形态未确认）')) if circle(r.skill_name) else r for r in theory)
    return actual,theory
