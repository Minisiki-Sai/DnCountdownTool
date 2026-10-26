"""Capture-validated Boss completion triggers that do not carry damage.

The 15-byte TCP trigger family synchronises one named ``trigger.ini`` node.
It is not a damage packet and must never manufacture player damage.  A small
number of encounter-local fingerprints are nevertheless useful lifecycle
evidence: the server emits them only after ``AllDieMonsterArea`` has observed
the corresponding Boss area as empty.

Rules remain scoped by the exact monster template.  Unknown fingerprints and
templates fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BossCompletionTriggerRule:
    prefix: bytes
    template_id: int
    grid: str
    trigger_name: str


# Source:
# Resource00/mapdata/grid/tn_desertdragonnest_2a/0_0/trigger.ini
#
# Cross-capture chronology:
# - 0821 hard: both fingerprints immediately follow the two final hit streams.
# - 0920 hard: Scorpion follows the last 4150057 hit by 147 ms; Spittler is in
#   the same millisecond as the final 4150056 hit.
# Both trigger nodes use AllDieMonsterArea before the subsequent actions.
RULES = (
    BossCompletionTriggerRule(
        bytes.fromhex("9871844cd6bebecc"),
        4_150_057,
        "tn_desertdragonnest_2a",
        "Kill Scorpion_Practice Mode & Hardcore",
    ),
    BossCompletionTriggerRule(
        bytes.fromhex("98c9234b16449721"),
        4_150_056,
        "tn_desertdragonnest_2a",
        "Kill Spittler_Practice Mode & Hardcore",
    ),
)

_BY_PREFIX = {rule.prefix: rule for rule in RULES}


def match_boss_completion_trigger(
    packet: object,
    active_template_ids: set[int] | frozenset[int],
) -> BossCompletionTriggerRule | None:
    """Return an exact, template-scoped completion rule for ``packet``."""

    payload = packet.payload
    if (
        packet.transport.upper() != "TCP"
        or packet.inferred_direction() != "server_to_client"
        or len(payload) != 15
        or payload[:2] != b"\x0f\0"
    ):
        return None
    rule = _BY_PREFIX.get(payload[3:11])
    if rule is None or rule.template_id not in active_template_ids:
        return None
    return rule
