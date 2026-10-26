"""Explicit HC skill starts, including Rosalinde (not classified as a Boss)."""
from .protocol import decode_v576_boss_skill_starts
from .voice_signals import HARD_ACTIONS, HARD_TEMPLATES


def publish_hard_start(host, packet, stamp):
    feed = getattr(host, 'voice_signal_feed', None)
    if feed is None or packet.transport.upper() != 'UDP' or packet.inferred_direction() != 'server_to_client':
        return
    actors = {actor for actor, template in host.monster_entities.items()
              if template.template_id in HARD_TEMPLATES}
    if not actors:
        return
    for start in decode_v576_boss_skill_starts(packet.payload, 'server_to_client', actors, HARD_ACTIONS):
        template = host.monster_entities[start.source_actor_id].template_id
        feed.publish(dict(skill=start.skill_index, source=f'0x{start.source_actor_id:08X}@{template}',
                          timestamp=stamp, templateId=template, evidenceKind='structured_action', recipients=[]))
