"""v576 C2S native basic-action requests, independently of damage/status pulses.

Validated with SacredHammering's five-stage / five-round Paladin capture.
Offsets are in the phase-6 decoded 51-byte message. The two-byte input token
wraps; duplicate suppression is bounded, never global across a capture.
"""
from dataclasses import replace
from .models import BossStatusEvent, SkillCastEvent, CombatEvent
from .protocol import (_decode_v576_body, _V576_XOR_PHASE_BY_SIGNATURE_BYTE,
                            _direct_result_fixed_slot_end)

HAMMER = {**{i: i-31 for i in range(32, 37)},
          **{i: i-63 for i in range(64, 69)},
          249: 1, 252: 2, 250: 3, 251: 4, 253: 5}
BASIC = {1, 6, 7, 12, 13, *range(18, 31), *range(37, 46)}
JOBS = {'圣骑士', '圣骑', '十字军', '贤者'}
BODY_ID = '3224:physical_attack'


def split_body_damage(packet, events):
    """Scope the native result signature to SacredHammering's fixed record.

    Native five-stage body signals use 1000/2000; both hammer magic contacts
    use 1500. These are NOT generic physical/magic flags for other skills.
    Validate the owning result, source and amount before reading its tail.
    """
    candidates = [e for e in events if isinstance(e, CombatEvent)
                  and str(e.skill_id).split(':')[0] in {'3224', '3354'}
                  and e.has_real_hit_evidence is True
                  and 'direct41' in e.wire_damage_layout]
    if not candidates or len(packet.payload) < 6:
        return events
    phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(packet.payload[5])
    if phase is None:
        return events
    b = _decode_v576_body(packet.payload, phase)
    body = set()
    for e in candidates:
        m = e.wire_record_offset
        if (m < 4 or m+110 > _direct_result_fixed_slot_end(b, m)
                or b[m:m+3] != bytes.fromhex('087700')
                or int.from_bytes(b[m+8:m+12], 'little') != e.wire_source_actor_id
                or int.from_bytes(b[m+23:m+31], 'little') != e.amount
                or int.from_bytes(b[m+106:m+110], 'little') not in {3224, 3354}):
            continue
        if int.from_bytes(b[m+104:m+106], 'little') in {1000, 2000}:
            body.add(id(e))
    return [replace(e, skill_id=BODY_ID, skill_name='普通攻击（神圣之锤本体）',
                    attribution_method='packet_native_attack_key',
                    raw_skill_id=e.raw_skill_id or e.skill_id,
                    raw_skill_name=e.raw_skill_name or e.skill_name,
                    raw_attribution_method=e.raw_attribution_method or e.attribution_method)
            if id(e) in body else e for e in events]


def state(decoder):
    if not hasattr(decoder, '_holy_hammer_input_state'):
        decoder._holy_hammer_input_state = dict(buffs={}, jobs={}, seen={}, actors=set())
    return decoder._holy_hammer_input_state


def observe_buffs(decoder, events):
    s = state(decoder)
    for e in events:
        if not isinstance(e, BossStatusEvent) or str(e.skill_id) != '3224' or e.target_scope != 'player':
            continue
        # 134400 is the actual mode Buff; attack parameter pulses are not it.
        if e.effect_code != '134400':
            continue
        actor = e.target_id
        if e.operation in {'remove', 'end', 'clear'}:
            s['buffs'].pop(actor, None)
        else:
            s['buffs'][actor] = (e.timestamp_ms, float('inf') if e.permanent else e.timestamp_ms + e.duration_ms)
            if e.target_job or e.job:
                s['jobs'][actor] = e.target_job or e.job


def decode_input(packet):
    if (str(packet.transport).upper() != 'UDP' or packet.inferred_direction() != 'client_to_server'
            or len(packet.payload) != 51 or packet.payload[5:9] != bytes.fromhex('7a5a3000')):
        return None
    b = _decode_v576_body(packet.payload, 6)
    actor = int.from_bytes(b[10:14], 'little')
    action = int.from_bytes(b[14:16], 'little')
    if not actor or action not in HAMMER and action not in BASIC:
        return None
    return actor, action, b[16:18]


def finalize_packet(decoder, packet, events):
    events = split_body_damage(packet, events)
    observe_buffs(decoder, events)
    s = state(decoder)
    entry = decode_input(packet)
    if entry is not None:
        actor, action, token = entry
        sid = f'0x{actor:08X}'
        name, job = decoder._actor_display_context(actor)
        job = job or s['jobs'].get(sid, '')
        now = packet.timestamp_seconds
        origin = decoder.first_timestamp if decoder.first_timestamp is not None else now
        ms = max(0, round((now-origin)*1000))
        lo, hi = s['buffs'].get(sid, (1, 0))
        active = lo <= ms < hi
        # The native transformed action itself proves the mode when capture
        # starts mid-Buff, but its action index is only valid for a cleric job.
        if job in JOBS and (active or action in HAMMER):
            key = (actor, action, token)
            previous = s['seen'].get(key, float('-inf'))
            s['seen'] = {k: t for k, t in s['seen'].items() if now-t <= 2}
            s['actors'].add(sid)
            if now-previous > 2:
                s['seen'][key] = now
                events.append(SkillCastEvent(ms, sid, name, job, '3224:basic_attack',
                                             '神圣之锤攻击', 'transformed_basic_attack'))
    # A status pulse can contain three components or represent a whole combo.
    # Once actual input packets exist it must not manufacture another attack.
    return [e for e in events if not (isinstance(e, SkillCastEvent)
            and e.skill_id == '3224:basic_attack' and e.packet_kind == 'status_skill_action'
            and e.source_id in s['actors'])]
