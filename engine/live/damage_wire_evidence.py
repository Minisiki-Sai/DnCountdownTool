"""Lossless decoded-record context, independent of display/skill attribution.

These fields describe the selected protocol interpretation, not a claim that
every layout is independently verified. They MUST NOT be used as a cross-packet
deduplication key: object/group IDs may recur, and equal hits are legitimate.
"""
from __future__ import annotations
from functools import lru_cache

from .udp_damage_records import inspect_udp_damage_records

_inventory = lru_cache(maxsize=64)(inspect_udp_damage_records)


def has_inferred_status_object_hit(event: object, payload: bytes, direction: str | None) -> bool:
    """Admit a legacy status-labelled result to exact object verification.

    This does not emit a Hit or identify a skill. A successful object/entity
    certificate must still win before the temporary real-Hit flag is kept.
    It never treats direct41's opaque weapon context as a projectile UID.
    """
    if (getattr(event, "attribution_method", "") != "periodic_status"
            or getattr(event, "has_real_hit_evidence", None) is not False
            or getattr(event, "healing", False)
            or getattr(event, "amount", 0) <= 0
            or "direct37" not in str(getattr(event, "wire_damage_layout", ""))
            or getattr(event, "wire_damage_object_id", 0) <= 0):
        return False
    inventory = _inventory(bytes(payload), direction)
    if not inventory.complete:
        return False
    expected = (
        event.wire_record_offset, event.wire_source_actor_id,
        event.wire_target_actor_id, event.amount, event.critical,
        event.wire_result_group_id, event.wire_damage_object_id,
    )
    records = [r for r in inventory.damage_records if r.physical_key == expected]
    if len(records) != 1:
        return False
    record = records[0]
    return bool(record.admissible_fields and record.family == "direct37"
                and record.record_bytes[11] == 1 and record.record_bytes[100] == 0)


def damage_wire_metadata(
    decoded: object, *, packet_payload: bytes | None = None, direction: str | None = None,
) -> dict:
    inventory = _inventory(bytes(packet_payload), direction) if packet_payload is not None else None
    return {
        "wire_packet_sha256": inventory.wire_sha256 if inventory is not None and inventory.complete else "",
        "wire_source_actor_id": int(getattr(decoded, "source_actor_id", 0) or 0),
        "wire_target_actor_id": int(getattr(decoded, "target_actor_id", 0) or 0),
        # Some compound direct-result layouts carry a second actor immediately
        # beside the selected target. Preserve it as raw protocol evidence so
        # later audits can distinguish an object/puppet target from a damage
        # target without reconstructing the encrypted packet.
        "wire_alternate_target_actor_id": int(
            getattr(decoded, "alternate_target_actor_id", 0) or 0
        ),
        "wire_record_offset": int(getattr(decoded, "record_offset", 0) or 0),
        "wire_damage_layout": str(getattr(decoded, "layout", "") or ""),
        "wire_critical": getattr(decoded, "critical", None),
        "wire_result_group_id": int(getattr(decoded, "damage_result_group_id", 0) or 0),
        "wire_damage_object_id": int(getattr(decoded, "damage_object_id", 0) or 0),
        "wire_status_skill_id": int(
            getattr(decoded, "appended_status_skill_id", 0) or 0
        ),
        "wire_status_tail_hex": str(
            getattr(decoded, "status_damage_tail_hex", "") or ""
        ),
    }
