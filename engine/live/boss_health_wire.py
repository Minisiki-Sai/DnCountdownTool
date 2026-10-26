"""Packet HP observations, independent of damage attribution and DPS totals."""
from dataclasses import dataclass
from functools import lru_cache

from .protocol import (
    _decode_v576_body, _v576_candidate_xor_phases,
    _v576_wire_may_contain_marker,
)
from .udp_damage_records import _walk
from .models import BossHealthEvent
from .boss_hp import resolve_fixed_boss_hp_profile, normalize_boss_hp_name

# Offsets relative to the four-byte target prefix, not to the packet header.
_HP_OFFSETS = {(8, 0x6f): 31, (8, 0x73): 31, (8, 0x77): 35,
               (0x21, 0x0c): 7}
_MARKERS = tuple(bytes((kind, size, 0)) for kind, size in _HP_OFFSETS) + (b'\x1c\x1b\x00\x05', b'\x22\x0c\x00')
SEA_PARTS = {4150061: ("头部", 90000000), 4150062: ("尾部", 700000000)}


@dataclass(frozen=True, slots=True)
class WireBossHealth:
    actor_id: int
    current_hp: int
    layout: str
    record_offset: int
    delta: bool = False
    part_id: int = 0


def decode_boss_health(payload: bytes, direction: str | None) -> tuple[WireBossHealth, ...]:
    if direction != "server_to_client" or len(payload) < 21:
        return ()
    if int.from_bytes(payload[7:9], "little") + 3 != len(payload):
        return ()
    if not _v576_wire_may_contain_marker(payload, _MARKERS, start_offset=14):
        return ()
    candidates = []
    for phase in _v576_candidate_xor_phases(payload):
        body = _decode_v576_body(payload, phase)
        boundaries = _walk(body)
        if boundaries is not None:
            candidates.append((body, boundaries))
    if len(candidates) != 1:
        return ()
    body, boundaries = candidates[0]
    result = []
    for start, marker, end, kind, size in boundaries:
        if (kind, size) == (0x22, 12):
            actor = int.from_bytes(body[start:start + 4], "little")
            part = int.from_bytes(body[start + 7:start + 11], "little")
            hp = int.from_bytes(body[start + 11:start + 15], "little")
            if 0 < actor < 0xffffffff and part in SEA_PARTS and hp <= SEA_PARTS[part][1]:
                result.append(WireBossHealth(actor, hp, "udp-220c00", start, part_id=part))
            continue
        # Validated subtype-5 B1 result: signed HP loss, not an absolute zero.
        # Admit only this proven tail, never generic status/resource deltas.
        if (kind, size) == (0x1c, 0x1b) and body[marker + 3] == 5:
            actor = int.from_bytes(body[start:start + 4], 'little')
            source = int.from_bytes(body[marker + 12:marker + 16], 'little')
            delta = int.from_bytes(body[marker + 4:marker + 12], 'little', signed=True)
            if (0 < actor < 0xffffffff and source not in (0, actor, 0xffffffff)
                    and -10**12 <= delta < 0
                    and body[marker + 16:end] == bytes.fromhex('0001000000000001b100ffffffff')):
                result.append(WireBossHealth(actor, delta, 'udp-1c1b0005-lethal', start, True))
            continue
        offset = _HP_OFFSETS.get((kind, size))
        if offset is None or start + offset + 8 > end:
            continue
        actor = int.from_bytes(body[start:start + 4], "little")
        hp = int.from_bytes(body[start + offset:start + offset + 8], "little", signed=True)
        if not 0 < actor < 0xffffffff or not -10**12 <= hp <= 10**12:
            continue
        result.append(WireBossHealth(actor, hp, f"udp-{kind:02x}{size:02x}00", start))
    return tuple(result)


@lru_cache(maxsize=1024)
def _maximum(target_id: str, name: str, template_hp: int) -> int:
    from .nightmare_sea_dragon import PHASES
    template_id = str(target_id).rsplit('@', 1)[-1]
    for phase in PHASES:
        if template_id in {str(value) for value in phase['template_ids']}:
            return int(phase['max_hp'])
    from .nest_hp_reference import profiles
    scoped = {p['entities'][template_id]['full_hp'] for p in profiles() if template_id in p['entities']}
    if scoped:
        return int(next(iter(scoped))) if len(scoped) == 1 else 0
    _, references = resolve_fixed_boss_hp_profile((name,), target_ids=(target_id,))
    values = references.get(normalize_boss_hp_name(name), ())
    # Multiple phase denominators cannot be guessed from the first value.
    if len(values) == 1 and values[0] > 0:
        return int(values[0])
    if len(values) > 1:
        return 0  # Phase-dependent maxima require an explicit template mapping.
    return max(0, int(template_hp))


def packet_boss_health_events(decoder, packet):
    if packet.transport.upper() != "UDP":
        return []
    events = []
    # One immediately preceding absolute observation, not cumulative DPS. Consume
    # it on the first delta; stale baselines and chains of inferred HP are unsafe.
    baselines = getattr(decoder, '_voice_hp_baselines', None)
    if baselines is None:
        baselines = decoder._voice_hp_baselines = {}
    for row in decode_boss_health(packet.payload, packet.inferred_direction()):
        template = decoder.monster_entities.get(row.actor_id)
        # Never promote a guessed/unknown target or player to a Boss.
        from .nest_hp_reference import known_template
        if template is None or (not template.boss and not known_template(decoder._monster_event_id(row.actor_id))):
            continue
        if row.actor_id in decoder.known_actor_names or row.actor_id in decoder.local_actor_ids:
            continue
        target_id = decoder._monster_event_id(row.actor_id)
        if row.part_id:
            if not target_id.endswith("@4237023"):
                continue
            seen = getattr(decoder, "_part_hp_packets", None)
            if seen is None:
                seen = decoder._part_hp_packets = {}
            fingerprint = (target_id, packet.payload, row.record_offset)
            if fingerprint in seen:
                continue
            seen[fingerprint] = True
            if len(seen) > 16384:
                seen.pop(next(iter(seen)))
            name, maximum = SEA_PARTS[row.part_id]
            origin = decoder.first_timestamp
            events.append(BossHealthEvent(
                timestamp_ms=max(0, round((packet.timestamp_seconds - (origin if origin is not None else packet.timestamp_seconds)) * 1000)),
                target_id=target_id, target_name=name, target_job="Boss部位",
                target_max_hp=maximum, current_hp=row.current_hp,
                layout=row.layout, part_id=row.part_id,
            ))
            continue  # Never use part HP as a main-Boss HP/death baseline.
        current_hp = row.current_hp
        if row.delta:
            seen = getattr(decoder, '_voice_hp_delta_packets', None)
            if seen is None:
                seen = decoder._voice_hp_delta_packets = {}
            fingerprint = (packet.payload, row.record_offset)
            if fingerprint in seen:
                continue
            seen[fingerprint] = True
            if len(seen) > 256:
                seen.pop(next(iter(seen)))
            previous = baselines.pop(target_id, None)
            if previous is None:
                continue
            stamp, absolute_hp = previous
            # The verified capture has adjacent messages only 6 microseconds
            # apart. Do not extrapolate death across a missing/stale HP history.
            if not (0 <= packet.timestamp_seconds - stamp <= 0.01
                    and absolute_hp > 0 and absolute_hp + current_hp <= 0):
                continue
            current_hp += absolute_hp
        else:
            previous = baselines.get(target_id)
            if previous is None or packet.timestamp_seconds >= previous[0]:
                baselines[target_id] = (packet.timestamp_seconds, current_hp)
            if len(baselines) > 128:
                baselines.pop(next(iter(baselines)))
        name, job = decoder._monster_display_context(row.actor_id)
        origin = decoder.first_timestamp
        events.append(BossHealthEvent(
            timestamp_ms=max(0, round((packet.timestamp_seconds - (origin if origin is not None else packet.timestamp_seconds)) * 1000)),
            target_id=target_id, target_name=name, target_job=job,
            target_max_hp=_maximum(target_id, name, template.hp),
            current_hp=current_hp, layout=row.layout,
        ))
    return events
