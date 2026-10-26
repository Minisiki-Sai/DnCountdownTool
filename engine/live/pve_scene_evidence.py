"""Independent PvE scene evidence for captures missing the initial map packet.

Never infer a mode from a matched player skill, its damage, a file name, or a
historical boss name. Require a raw current raid-Boss birth, identical UDP
endpoints, unchanged scene boundaries, and a fresh nonplayer target birth.
This does NOT identify a dungeon name or drive recording start/stop.
"""
from dataclasses import dataclass
from hashlib import sha256
import math

from .nest_recognizer import resolve_nest_template
from .protocol import decode_v576_monster_spawn, _decode_v576_body


def connection_key(packet):
    return (str(packet.transport).upper(),packet.source_ip,packet.source_port,
            packet.destination_ip,packet.destination_port)


def scene_stamp(host):
    return (host.entity_owner_evidence.scene_serial,
        getattr(host,'detected_scene_transition_ack_revision',0),
        getattr(host,'detected_lobby_return_revision',0),
        getattr(host,'detected_map_id',0),getattr(host,'detected_map_active',False),
        len(getattr(host,'detected_map_history',())))


@dataclass(frozen=True,slots=True)
class ScenePveWitness:
    actor: int
    template: int
    timestamp: float
    packet_serial: int
    wire_hash: str
    stamp: tuple
    connection: tuple


def observe_scene_boss(host,packet):
    if (packet.transport.upper()!='UDP' or packet.inferred_direction()!='server_to_client'
            or not math.isfinite(packet.timestamp_seconds)):
        return
    born=decode_v576_monster_spawn(packet.payload,'server_to_client')
    if born is None:return
    template=host.monster_entities.get(born.actor_id)
    if (template is None or template.template_id!=born.template_id or not template.boss
            or resolve_nest_template(born.template_id) is None):
        return
    # Ignore envelope retransmission counters when recognizing an identical
    # birth. Keep these bounded tombstones across scene resets, just like the
    # ownership ledger: an old datagram must not reactivate a previous scene.
    # No interpretation of the opaque birth fields is needed here.
    state=host._pve_scene_witnesses
    key=connection_key(packet);digest=sha256(packet.payload).hexdigest()
    canonical=(key,sha256(_decode_v576_body(packet.payload,5)[10:]).hexdigest())
    seen=host._pve_scene_seen_births
    if canonical in seen:return
    seen[canonical]=None
    while len(seen)>1024:seen.pop(next(iter(seen)))
    state[key]=ScenePveWitness(born.actor_id,born.template_id,packet.timestamp_seconds,
        host._damage_object_packet_serial,digest,scene_stamp(host),key)
    while len(state)>4:state.pop(next(iter(state)))


def confirmed_scene_target(host,target_id,packet):
    """Mode-only proof. Ownership/skill/raw physical-hit checks remain separate."""
    if (packet is None or packet.transport.upper()!='UDP'
            or packet.inferred_direction()!='server_to_client'
            or not math.isfinite(packet.timestamp_seconds)
            or getattr(host,'detected_map_id',0)!=0
            or getattr(host,'detected_map_history',())):
        return False  # never override an explicit map, leave, or hub record
    witness=getattr(host,'_pve_scene_witnesses',{}).get(connection_key(packet))
    if (witness is None or witness.stamp!=scene_stamp(host)
            or packet.timestamp_seconds<witness.timestamp
            or host._damage_object_packet_serial<witness.packet_serial):
        return False
    boss=host.monster_entities.get(witness.actor)
    target=host.monster_entities.get(target_id)
    if (boss is None or boss.template_id!=witness.template or not boss.boss
            or target is None or any(target_id in getattr(host,key,())for key in
                ('skill_actor_ids','local_actor_ids','known_actor_names'))):
        return False
    ledger=host.entity_owner_evidence
    birth=ledger.births.get(target_id);evidence=ledger.birth_evidence.get(target_id)
    # Player-owned summons are not a PvE mode witness/target. Check a current
    # raw target birth, not only an old entry left in monster_entities.
    if (birth is None or birth[0]!=target.template_id or evidence is None
            or ledger.negative_state(target_id,timestamp=packet.timestamp_seconds)!='missing_owner'
            or target_id in ledger.bindings
            or birth[2]>packet.timestamp_seconds
            or evidence[0]>(host._damage_object_packet_serial,0,0)
            or len(evidence[1])!=64):
        return False
    return True
