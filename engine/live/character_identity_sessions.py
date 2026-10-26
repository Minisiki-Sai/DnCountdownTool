"""Capture-local character switches proved by the named C2S 114 identity row.

Wire actor IDs are unchanged. Replays assign independent numeric presentation
IDs only after one runtime UID is proved to carry multiple named characters.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import fields, is_dataclass, replace
from hashlib import sha256
import re

_ACTOR = re.compile(r'^(?:profile:)?0x([0-9a-fA-F]{8})$')
_ID_FIELDS = ('source_id', 'target_id', 'actor_id', 'owner_id', 'requester_id',
              'caster_id', 'player_id')
_ID_LIST_FIELDS = ('actor_ids', 'affected_target_ids')


def observe_local_character_identity(host, name: str, timestamp: float, *,
                                     identity_evidence: str = '') -> None:
    """Clear only the recording client's stale class at a verified name switch."""
    name = str(name or '').strip()
    if not name: return
    sessions = getattr(host, 'local_character_identity_sessions', None)
    if sessions is None:
        sessions = []
        host.local_character_identity_sessions = sessions
    actor_ids = set(getattr(host, 'local_actor_ids', ()))
    if sessions and sessions[-1]['name'] == name:
        sessions[-1]['last_seen_at'] = float(timestamp)
        sessions[-1]['last_identity_evidence'] = identity_evidence
        sessions[-1]['actor_ids'].update(actor_ids)
        return
    if not sessions:
        sessions.append({'name': name, 'start_ms': 0, 'last_seen_at': float(timestamp),
                         'actor_ids': actor_ids, 'jobs': {},
                         'identity_evidence': identity_evidence,
                         'last_identity_evidence': identity_evidence})
        return
    previous = sessions[-1]
    previous['actor_ids'].update(actor_ids)
    for actor in previous['actor_ids']:
        job = getattr(host, 'actor_jobs', {}).get(actor, ('', 99))[0]
        if job: previous['jobs'][actor] = job
    # A fresh entity birth can precede the first movement/identity packet by
    # seconds. Retire only a proved old character's existing generation, not
    # a just-born summon belonging to the newly observed character.
    ledger = getattr(host, 'entity_owner_evidence', None)
    if ledger is not None:
        for entity, binding in tuple(ledger.bindings.items()):
            if (binding.record.owner_id in previous['actor_ids']
                    and binding.born_at <= previous['last_seen_at']):
                ledger.retire(entity, reason='local_character_changed', timestamp=float(timestamp))
                getattr(host, 'summon_owners', {}).pop(entity, None)
    for actor in previous['actor_ids']:
        getattr(host, 'actor_skill_profiles', {}).pop(actor, None)
        getattr(host, 'actor_jobs', {}).pop(actor, None)
        getattr(host, 'known_actor_names', {}).pop(actor, None)
    getattr(host, 'local_actor_ids', set()).clear()
    origin = getattr(host, 'first_timestamp', None)
    start_ms = max(0, round((float(timestamp) - float(origin or timestamp)) * 1000))
    sessions.append({'name': name, 'start_ms': start_ms, 'last_seen_at': float(timestamp),
                     # The name packet has no actor UID. A later local cast
                     # or another exact local-actor marker must prove which
                     # runtime actor belongs to this newly named character.
                     'actor_ids': set(), 'jobs': {},
                     'identity_evidence': identity_evidence,
                     'last_identity_evidence': identity_evidence})


def split_replay_character_sessions(events, decoder):
    """Rewrite presentation references consistently; preserve every wire field."""
    sessions = getattr(decoder, 'local_character_identity_sessions', ())
    if len(sessions) < 2:
        return events, {}
    last = sessions[-1]
    last['actor_ids'].update(getattr(decoder, 'local_actor_ids', ()))
    for actor in last['actor_ids']:
        job = getattr(decoder, 'actor_jobs', {}).get(actor, ('', 99))[0]
        if job: last['jobs'][actor] = job
    occupied = set(getattr(decoder, 'known_actor_names', {}))
    for event in events:
        if not is_dataclass(event): continue
        for field in fields(event):
            value = getattr(event, field.name)
            if field.name.startswith('wire_') and isinstance(value, int): occupied.add(value)
            elif isinstance(value, str):
                match = _ACTOR.fullmatch(value)
                if match: occupied.add(int(match[1], 16))
            elif field.name in _ID_LIST_FIELDS:
                for item in value:
                    match = _ACTOR.fullmatch(str(item))
                    if match: occupied.add(int(match[1], 16))
    actor_sessions = {}
    contexts = {}
    aliases = []
    for index, session in enumerate(sessions):
        for actor in sorted(session['actor_ids']):
            prior = actor_sessions.setdefault(actor, [])
            alias = actor
            if prior:
                seed = sha256(f"local-character:{actor}:{index}:{session['name']}".encode('utf8')).digest()
                alias = 0xE0000000 | (int.from_bytes(seed[:4], 'little') & 0x0FFFFFFF)
                while alias in occupied or alias == 0xFFFFFFFF:
                    alias = 0xE0000000 | ((alias + 1) & 0x0FFFFFFF)
                occupied.add(alias)
            prior.append((session['start_ms'], alias))
            contexts[alias] = (session['name'], session['jobs'].get(actor, ''))
            aliases.append({'wire_actor_id': actor, 'presentation_actor_id': alias,
                            'name': session['name'], 'job': session['jobs'].get(actor, ''),
                            'start_ms': session['start_ms'],
                            'identity_evidence': session.get('identity_evidence', '')})
    decoder.replay_character_identity_aliases = aliases

    def actor_at(text, timestamp):
        match = _ACTOR.fullmatch(str(text or ''))
        if not match: return text
        actor = int(match[1], 16)
        spans = actor_sessions.get(actor)
        if not spans: return text
        offset = bisect_right([p[0] for p in spans], timestamp) - 1
        if offset < 0: return text
        alias = spans[offset][1]
        return f"{'profile:' if str(text).startswith('profile:') else ''}0x{alias:08X}"

    result = []
    for event in events:
        if not is_dataclass(event):
            result.append(event)
            continue
        available = {f.name for f in fields(event)}
        timestamp = int(getattr(event, 'timestamp_ms', 0))
        changes = {}
        for key in available & set(_ID_FIELDS):
            value = getattr(event, key)
            mapped = actor_at(value, timestamp)
            if value != mapped: changes[key] = mapped
        for key in available & set(_ID_LIST_FIELDS):
            value = getattr(event, key)
            mapped = tuple(actor_at(item, timestamp) for item in value)
            if value != mapped: changes[key] = mapped
        result.append(replace(event, **changes) if changes else event)
    return result, contexts
