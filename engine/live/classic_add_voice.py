"""Classic phase-three add exit actions; never infer removal from a spawn timer."""
from .protocol import decode_v576_boss_skill_starts


def publish_classic_add_exit(host, packet, timestamp_ms):
    feed = getattr(host, 'voice_signal_feed', None)
    if feed is None or packet.transport.upper() != 'UDP' or packet.inferred_direction() != 'server_to_client':
        return
    actors = {actor for actor, template in host.monster_entities.items()
              if template.template_id == 503693}
    if not actors:
        return
    for start in decode_v576_boss_skill_starts(packet.payload, 'server_to_client', actors, {34926, 34928}):
        feed.publish({'kind': 'classic_add_exit', 'skill': start.skill_index,
                      'source': f'0x{start.source_actor_id:08X}@503693',
                      'templateId': 503693, 'timestamp': timestamp_ms})
