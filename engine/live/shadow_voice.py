"""Packet-backed frozen-shadow alerts, independent of Boss classification."""
from .protocol import decode_v576_boss_skill_starts


def publish_shadow_start(host, packet, timestamp_ms):
    feed = getattr(host, 'voice_signal_feed', None)
    if feed is None or packet.transport.upper() != 'UDP' or packet.inferred_direction() != 'server_to_client':
        return
    actors = {actor for actor, template in host.monster_entities.items()
              if template.template_id == 4237025}
    for start in decode_v576_boss_skill_starts(packet.payload, 'server_to_client', actors, {325478}):
        feed.publish({'kind': 'shadow_start', 'skill': 325478,
                      'source': f'0x{start.source_actor_id:08X}@4237025',
                      'templateId': 4237025, 'timestamp': timestamp_ms})
