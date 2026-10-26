"""Map-scoped, resource-verified clear reward evidence (not a combat clock).

Classic Black Dragon trigger.ini records 119/120/121/123/124/125 require
Clear_Setting variable 1 == 2 and execute NestClearDrop. All_Clear_Base sets
that value only after LogFinish; All_Clear_Abyss exposes the reward props.
These are safe backup completion proofs if capture loss removed 0C05/NestEnd,
NOT generic prop clicks, item receipts or a reason to wait for a chest.
The encrypted trigger index is shared across grids: map AND terminal monster
template are mandatory, and the ledger also binds evidence to recent combat.
"""
from __future__ import annotations

from .clear_trigger import ClearTriggerRule


RULES = tuple(
    ClearTriggerRule(bytes.fromhex(prefix), index, "aa_blackdragon_nestc_boss",
                     name, frozenset({14501, 14502, 14503, 14504}), 503696, 14515)
    for prefix, index, name in (
        ("9809164e16269eeb", 119, "ChestOpen_Sub1"),
        ("9871274ed686a4f4", 120, "ChestOpen_Sub2"),
        ("9879f0439686825b", 121, "ChestOpen_Sub3"),
        ("9869d24316618659", 123, "ChestOpen_Main_Abyss_Hard"),
        ("9851e343d641885a", 124, "ChestOpen_Main_Chaos"),
    )
)
_BY_PREFIX = {rule.prefix: rule for rule in RULES}


def match_clear_reward_trigger(packet: object, map_id: int,
                               boss_template_id: int) -> ClearTriggerRule | None:
    payload = packet.payload
    if (packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
            or len(payload) != 15 or payload[:2] != b"\x0f\0"):
        return None
    rule = _BY_PREFIX.get(payload[3:11])
    if (rule is None or map_id not in rule.map_ids
            or boss_template_id != rule.boss_template_id):
        return None
    return rule
