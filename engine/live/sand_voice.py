"""Explicit sand hardcore starts; no damage-hit inference."""
from .protocol import decode_v576_boss_skill_starts
from .voice_signals import SAND_ACTIONS, SAND_TEMPLATES


def publish_sand_start(host, packet, stamp):
    feed = getattr(host, 'voice_signal_feed', None)
    if feed is None or packet.transport.upper() != 'UDP' or packet.inferred_direction() != 'server_to_client':
        return
    actors = {actor for actor, template in host.monster_entities.items()
              if template.template_id in SAND_TEMPLATES}
    if not actors:
        return
    for start in decode_v576_boss_skill_starts(packet.payload, 'server_to_client', actors, SAND_ACTIONS):
        template = host.monster_entities[start.source_actor_id].template_id
        feed.publish(dict(skill=start.skill_index, source=f'0x{start.source_actor_id:08X}@{template}',
                          timestamp=stamp, templateId=template, evidenceKind='structured_action', recipients=[]))
