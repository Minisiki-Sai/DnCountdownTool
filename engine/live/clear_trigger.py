"""Narrow, capture-validated NestEnd trigger evidence.

The 15-byte TCP family synchronizes map trigger execution. Its trigger-index
projection was cross-checked against complete binary trigger.ini records (not
string-search order). The full short-round key/opcode is NOT uniquely recovered:
do not register the diagnostic key or treat its other fields as plaintext.
Instead, use only observed encrypted first-block fingerprints, scoped by map AND
terminal monster template. All supported rows invoke NestEnd without a preceding
Delay action. NestEnd starts BeginCountDownEx before showing the later 0C05 table.
Unknown fingerprints, maps and templates fail closed; elapsed time alone never
creates a clear. The packet's rolling byte and final clock bytes are not IDs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClearTriggerRule:
    prefix: bytes
    index: int
    grid: str
    name: str
    map_ids: frozenset[int]
    boss_template_id: int
    clear_table_id: int


# Source: Resource00/Resource00/mapdata/grid/<grid>/0_0/trigger.ini.
# Packet validation: Guardian x2, Black 1/2, Black 3, Classic, HC x2, Sand x2,
# Mist and Trial. The latter two repeat their encrypted trigger fingerprint in
# earlier rooms, so the terminal Boss template is mandatory.
# Root map IDs are allowed for multi-grid nests, but never without the terminal
# Boss template and a recent hit on the same runtime Boss in SettlementLedger.
RULES = (
    ClearTriggerRule(bytes.fromhex("98b1dd49d6b09233"), 32, "aa_guardiannestb",
                     "NestClear_Default(4)", frozenset({14300, 14301}), 503151, 14300),
    ClearTriggerRule(bytes.fromhex("9869d24316618659"), 123, "aa_blackdragon_nest_memoria_1_2",
                     "Memoria_Clear", frozenset({14511, 14512}), 503621, 14511),
    ClearTriggerRule(bytes.fromhex("98d1d34ed64d8ae4"), 108, "aa_blackdragon_nest_memoria_2",
                     "Memoria_Clear", frozenset({14513}), 503661, 14513),
    ClearTriggerRule(bytes.fromhex("98295a4e16679eec"), 115, "aa_blackdragon_nest_memoria_3_boss",
                     "All_Clear_Easy", frozenset({14514}), 503696, 14514),
    ClearTriggerRule(bytes.fromhex("98116b4ed647a0e9"), 116, "aa_blackdragon_nestc_boss",
                     "All_Clear_Abyss", frozenset({14501, 14502, 14503, 14504}), 503696, 14515),
    ClearTriggerRule(bytes.fromhex("98a98a4d1663b3f2"), 99, "aa_blackdragon_nest_hc",
                     "BossClear 2-3weaponbroken", frozenset({17612}), 4181009, 17612),
    ClearTriggerRule(bytes.fromhex("98195b50964fe48d"), 85, "tn_desertdragonnest_bossa_classical",
                     "Clear_Classical", frozenset({14221, 14222, 14223}), 503064, 14061),
    ClearTriggerRule(bytes.fromhex("9869104a1691a237"), 59, "aa_mistnest_bossa",
                     "Clear_Kill Segyesu Boss(4=Abyss)", frozenset({14400, 14401}), 503496, 14400),
    ClearTriggerRule(bytes.fromhex("9899c94b967b95d0"), 5, "aa_nesthell_bossa",
                     "NestClear_Default(4)", frozenset({14410, 14411, 14412}), 503561, 14410),
)
_BY_PREFIX = {rule.prefix: rule for rule in RULES}


def match_clear_trigger(packet: object, map_id: int, boss_template_id: int) -> ClearTriggerRule | None:
    payload = packet.payload
    if (packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
            or len(payload) != 15 or payload[:2] != b"\x0f\0"):
        return None
    rule = _BY_PREFIX.get(payload[3:11])
    if rule is None or boss_template_id != rule.boss_template_id:
        return None
    if map_id and map_id not in rule.map_ids:
        return None
    return rule
