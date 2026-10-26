"""Recipient-scoped, packet-proven lifecycle of the four toggle auras."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping

from .models import BossStatusEvent
from .protocol import _decode_v576_body, _v576_candidate_xor_phases

AURA_SKILL_IDS = frozenset({2213, 2214, 3211, 3213})
AURA_SKILL_KEYS = frozenset(map(str, AURA_SKILL_IDS))
AURA_SKILL_NAMES = frozenset({"火焰光环", "寒冰光环", "元素光环", "判定光环"})


def _row_value(row: object, name: str, default=None):
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def is_aura_row(row: object) -> bool:
    return (str(_row_value(row, "skill_id", "")) in AURA_SKILL_KEYS
            or str(_row_value(row, "skill_name", "")).removesuffix("EX") in AURA_SKILL_NAMES)


def is_inferred_aura_row(row: object) -> bool:
    """An aura cast is not evidence that this recipient received its Buff.

    This guard is shared by the factual aggregator and detached display/export
    projections, including legacy metadata. Other maintained Buffs are intact.
    """
    if not is_aura_row(row):
        return False
    key = str(_row_value(row, "effect_key", _row_value(row, "key", "")))
    return (key.startswith("永久角色增益:")
            or _row_value(row, "effect_code", "") == "cast_inferred_permanent"
            or _row_value(row, "state_source", "") == "cast_inferred_permanent")


def is_aura_application(status: object) -> bool:
    # Controller/passive components are not the recipient's attack/resist Buff.
    return (
        getattr(status, "skill_id", 0) in AURA_SKILL_IDS
        and bool(getattr(status, "permanent", False))
        and getattr(status, "effect_code", "") in {"134700", "134600"}
        and int(getattr(status, "effect_instance_id", 0) or 0) > 0
    )


def is_aura_damage_series(row: object) -> bool:
    key = str(_row_value(row, "effect_key", _row_value(row, "key", "")))
    # The resistance component can end at a different instant. It must not
    # extend the attack Buff's interval merely because both belong to an aura.
    return not key.startswith("角色增益:") or ":134700:" in key


def decode_aura_instance_ends(payload: bytes, direction: str | None) -> tuple[tuple[int, int], ...]:
    """Only complete top-level 0x15/5 records; never search nested bytes.

    The record has no skill/source ID. The state ledger must match both its
    recipient and instance to an earlier, verified aura application.
    """
    if (direction != "server_to_client" or len(payload) < 26
        or int.from_bytes(payload[3:5], "little") + 10 != len(payload)):
        return ()
    candidates = []
    for phase in _v576_candidate_xor_phases(payload):
        body = _decode_v576_body(payload, phase)
        if b"\x15\x05\x00" not in body:
            continue
        cursor = 14
        ends = []
        while cursor < len(body):
            marker = cursor + 4
            if marker + 3 > len(body):
                break
            length = int.from_bytes(body[marker + 1:marker + 3], "little")
            end = marker + 3 + length
            if end > len(body):
                break
            if body[marker] == 0x15 and length == 5 and body[end - 1] == 0:
                target = int.from_bytes(body[cursor:marker], "little")
                instance = int.from_bytes(body[marker + 3:marker + 7], "little")
                if target not in (0, 0xffffffff) and instance not in (0, 0xffffffff):
                    ends.append((target, instance))
            cursor = end
        if cursor == len(body) and ends:
            candidates.append(tuple(dict.fromkeys(ends)))
    # Unknown/ambiguous phases fail closed rather than ending arbitrary Buffs.
    return candidates[0] if len(candidates) == 1 else ()


class AuraInstanceLedger:
    def __init__(self) -> None:
        self.active: dict[tuple[str, int], BossStatusEvent] = {}
        self.components: dict[tuple[str, str, str, str, int], tuple[str, int]] = {}

    @staticmethod
    def _component(event: BossStatusEvent) -> tuple[str, str, str, str, int]:
        return event.target_id, event.source_id, event.skill_id, event.effect_code, event.effect_class_id

    def observe(self, event: BossStatusEvent) -> list[BossStatusEvent]:
        key = event.target_id, event.effect_instance_id
        component = self._component(event)
        events = []
        previous_key = self.components.get(component)
        if previous_key is not None and previous_key != key:
            old = self.active.pop(previous_key, None)
            if old is not None:
                events.append(replace(old, timestamp_ms=event.timestamp_ms, operation="remove"))
        self.active[key] = event
        self.components[component] = key
        events.append(event)
        return events

    def end(self, target_id: str, instance_id: int, timestamp_ms: int) -> BossStatusEvent | None:
        key = target_id, instance_id
        event = self.active.pop(key, None)
        if event is None:
            return None
        component = self._component(event)
        if self.components.get(component) == key:
            self.components.pop(component, None)
        return replace(event, timestamp_ms=timestamp_ms, operation="remove")
