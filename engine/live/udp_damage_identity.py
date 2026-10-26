"""Select a complete physical UDP result before owner/presentation routing."""
from dataclasses import replace

from .udp_damage_records import inspect_udp_damage_records


def canonicalize_udp_damage_identity(packet, decoded):
    if (decoded is None or decoded.kind != "damage_hit"
            or str(packet.transport).upper() != "UDP" or decoded.critical is None):
        return decoded
    layout = str(decoded.layout)
    family = "direct37" if "direct37" in layout else "direct41" if "direct41" in layout else ""
    if layout == "s2c-udp-hit-136-v576":
        # This legacy single-record decoder predates physical result metadata.
        # Still require a complete typed 0873 record and exact field agreement.
        family = "direct37"
    if not family or packet.inferred_direction() != "server_to_client":
        return decoded
    if family == "direct41" and (
        decoded.damage_result_group_id is not None or decoded.damage_object_id is not None
    ):
        return decoded
    offset = int(decoded.record_offset or 0)
    # Most existing rows are already unambiguous. No second full decryption on
    # that fast path; inspect only alternate-target / missing-identity cases.
    entity_with_outer_player_target = (
        decoded.source_actor_id is not None
        and 0 < decoded.source_actor_id < 65536
        and decoded.target_actor_id is not None
        and decoded.target_actor_id > 0xFFFFFF
    )
    # A compound can start with a short-entity 09 object update. The old
    # reader then takes that entity as the target of a later 08 result,
    # producing a spurious self-hit. Inspect the exact offset's full typed
    # record in this case too; its prefix, not the outer 09 prefix, is target.
    # This only enables structural validation, never supplies a guessed target.
    entity_with_self_target = (
        decoded.source_actor_id is not None
        and 0 < decoded.source_actor_id < 65536
        and decoded.target_actor_id == decoded.source_actor_id
    )
    if (offset > 0 and decoded.alternate_target_actor_id is None
            and not entity_with_outer_player_target and not entity_with_self_target):
        return decoded
    inventory = inspect_udp_damage_records(packet.payload, "server_to_client")
    if not inventory.complete:
        return decoded
    candidates = [r for r in inventory.damage_records
                  if r.family == family and r.admissible_fields
                  and (not offset or r.record_offset == offset)
                  and r.source_actor_id == decoded.source_actor_id
                  and r.damage == decoded.damage
                  and r.critical == decoded.critical]
    if len(candidates) != 1:
        return decoded
    if not offset and sum(r.record_type == 8 for r in inventory.typed_records) != 1:
        # A legacy missing-offset primary cannot be matched by amount when
        # another known OR unknown damage/HP type-08 record competes with it.
        return decoded
    row = candidates[0]
    if family == "direct37" and (
        decoded.damage_result_group_id is not None and decoded.damage_result_group_id != row.damage_result_group_id
        or decoded.damage_object_id is not None and decoded.damage_object_id != row.damage_object_id
    ):
        return decoded
    # The record prefix names its target. An outer observer/owner is not a
    # second person struck; leaving that alternate caused false incoming hits.
    return replace(decoded, target_actor_id=row.target_actor_id,
                   alternate_target_actor_id=None, record_offset=row.record_offset,
                   damage_result_group_id=(row.damage_result_group_id if family == "direct37" else None),
                   damage_object_id=(row.damage_object_id if family == "direct37" else None))
