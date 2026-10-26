"""One server Attack1_CripplePunisher action is one four-hit attack.

The v576 kind-3 record is 41 bytes, with action 117 in assassin_basic.act.
Only complete top-level records are admitted. Damage and attack-parameter
pulses never manufacture casts, so missed/interrupted rounds remain visible.
"""
from dataclasses import replace
from .models import SkillCastEvent
from .protocol import _decode_v576_body, _V576_XOR_PHASE_BY_SIGNATURE_BYTE

ATTACK_ID = "6212:basic_attack"
ATTACK_NAME = "致残惩罚者攻击"
JOBS = frozenset({"烈", "影", "暗之使徒", "刺客"})


def _records(packet):
    """Require a complete outer message and walk all top-level boundaries."""
    payload = packet.payload
    if (packet.inferred_direction() != 'server_to_client' or len(payload) < 62
            or int.from_bytes(payload[7:9], 'little') + 3 != len(payload)):
        return ()
    phase = _V576_XOR_PHASE_BY_SIGNATURE_BYTE.get(payload[5])
    if phase is None:
        return ()
    body = _decode_v576_body(payload, phase)
    cursor, rows = 14, []
    while cursor < len(body):
        if cursor + 7 > len(body):
            return ()
        end = cursor + 7 + int.from_bytes(body[cursor+5:cursor+7], 'little')
        if end > len(body):
            return ()
        rows.append(body[cursor:end])
        cursor = end
    return rows


def action_records(packet):
    if str(packet.transport).upper() != "UDP":
        return ()
    result = []
    for record in _records(packet):
        if record[4] != 3 or len(record) != 48 or record[5:7] != b"\x29\x00":
            continue
        actor = int.from_bytes(record[:4], "little")
        if actor < 0x01000000 or actor == 0xFFFFFFFF:
            continue
        if int.from_bytes(record[7:9], "little") != 117:
            continue
        result.append((actor, record[9:13]))
    return tuple(result)


def finalize_packet(decoder, packet, events):
    rows = action_records(packet)
    if not rows:
        return events
    now = packet.timestamp_seconds
    seen = getattr(decoder, "_punisher_action_seen", {})
    seen = {key: stamp for key, stamp in seen.items() if now - stamp <= 2.0}
    decoder._punisher_action_seen = seen
    origin = decoder.first_timestamp if decoder.first_timestamp is not None else now
    ms = max(0, round((now - origin) * 1000))
    for actor, token in rows:
        name, job = decoder._actor_display_context(actor)
        if job not in JOBS or (actor, token) in seen:
            continue
        seen[(actor, token)] = now
        events.append(SkillCastEvent(ms, f"0x{actor:08X}", name, job,
                                    ATTACK_ID, ATTACK_NAME, "transformed_basic_attack"))
    return events


def install_decoder_hook(cls):
    """Extend an installed decoder without replacing its unrelated logic."""
    if getattr(cls.feed_events, '_punisher_four_hit', False):
        return
    original = cls.feed_events
    def feed_events(self, packet):
        return finalize_packet(self, packet, original(self, packet))
    feed_events._punisher_four_hit = True
    cls.feed_events = feed_events


def install_aggregator_hook(cls):
    """Keep other installed skill normalizers intact, including Holy Hammer."""
    if getattr(cls._normalize_transformed_basic_attack, '_punisher_four_hit', False):
        return
    original = cls._normalize_transformed_basic_attack
    def normalize(self, event):
        if str(event.skill_id) != '6212':
            return original(self, event)
        if event.healing or not event.source_id or not event.target_id or event.source_id == event.target_id:
            return event, None
        return replace(event, skill_id=ATTACK_ID, skill_name=ATTACK_NAME), None
    normalize._punisher_four_hit = True
    cls._normalize_transformed_basic_attack = normalize
