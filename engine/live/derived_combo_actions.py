from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DerivedComboPredecessor:
    skill_id: int
    alias_skill_ids: tuple[int, ...]
    max_gap_seconds: float
    damage_owner: str = "predecessor_packet_skill_id"

    @property
    def skill_ids(self) -> frozenset[int]:
        return frozenset((self.skill_id, *self.alias_skill_ids))

    def matches(self, skill_id: int) -> bool:
        return skill_id in self.skill_ids


@dataclass(frozen=True, slots=True)
class DerivedComboActionRule:
    trigger_skill_id: int
    trigger_alias_skill_ids: tuple[int, ...]
    intermediate_skill_id: int
    intermediate_skill_name: str
    anchor_before_trigger_seconds: float
    predecessors: tuple[DerivedComboPredecessor, ...]

    @property
    def trigger_skill_ids(self) -> frozenset[int]:
        return frozenset((self.trigger_skill_id, *self.trigger_alias_skill_ids))

    def predecessor_for(self, skill_id: int) -> DerivedComboPredecessor | None:
        for row in self.predecessors:
            if row.matches(skill_id):
                return row
        return None

    def max_gap_after(self, skill_id: int) -> float | None:
        predecessor = self.predecessor_for(skill_id)
        return predecessor.max_gap_seconds if predecessor is not None else None

    def match_predecessor(
        self,
        history: tuple[tuple[int, float], ...] | list[tuple[int, float]],
        trigger_time: float,
    ) -> tuple[DerivedComboPredecessor, int, float] | None:
        """Match the nearest real predecessor without crossing another action.

        The client can publish both the base and EX/compact IDs for one button
        press.  Those trigger aliases are transparent for 250 ms.  Any other
        action remains a hard barrier, so a much older hurricane/binding action
        cannot manufacture a kick for an unrelated Double Somersault.
        """

        for skill_id, cast_time in reversed(history):
            gap = trigger_time - cast_time
            if gap < 0.0:
                continue
            if skill_id in self.trigger_skill_ids and gap <= 0.25:
                continue
            predecessor = self.predecessor_for(skill_id)
            if predecessor is None:
                return None
            if gap <= predecessor.max_gap_seconds:
                return predecessor, skill_id, cast_time
            return None
        return None


def load_derived_combo_action_rules(
    data_root: Path,
) -> dict[int, tuple[DerivedComboActionRule, ...]]:
    """Load action-chain inference without embedding skill timings in code.

    Some client-side continuation actions do not emit their own ordinary cast
    packet.  A later server action can still prove that the intermediate action
    happened.  The profile records those transitions and, importantly, keeps a
    separate predecessor window for each source action.
    """

    path = Path(data_root) / "profiles" / "derived_combo_actions.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    grouped: dict[int, list[DerivedComboActionRule]] = {}
    for raw_rule in document.get("rules", ()):
        try:
            trigger_skill_id = int(raw_rule["trigger_skill_id"])
            trigger_alias_skill_ids = tuple(
                int(value)
                for value in raw_rule.get("trigger_alias_skill_ids", ())
            )
            intermediate_skill_id = int(raw_rule["intermediate_skill_id"])
            intermediate_skill_name = str(
                raw_rule.get("intermediate_skill_name", "")
            ).strip()
            anchor_seconds = max(
                0.0, float(raw_rule.get("anchor_before_trigger_ms", 0.0)) / 1000.0
            )
            predecessors = tuple(
                DerivedComboPredecessor(
                    skill_id=int(row["skill_id"]),
                    alias_skill_ids=tuple(
                        int(value)
                        for value in row.get("alias_skill_ids", ())
                    ),
                    max_gap_seconds=max(0.0, float(row["max_gap_ms"]) / 1000.0),
                    damage_owner=str(
                        row.get(
                            "damage_owner",
                            "predecessor_packet_skill_id",
                        )
                    ).strip(),
                )
                for row in raw_rule.get("predecessors", ())
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not predecessors:
            continue
        grouped.setdefault(trigger_skill_id, []).append(
            DerivedComboActionRule(
                trigger_skill_id=trigger_skill_id,
                trigger_alias_skill_ids=trigger_alias_skill_ids,
                intermediate_skill_id=intermediate_skill_id,
                intermediate_skill_name=intermediate_skill_name,
                anchor_before_trigger_seconds=anchor_seconds,
                predecessors=predecessors,
            )
        )
    result: dict[int, list[DerivedComboActionRule]] = {}
    for rows in grouped.values():
        for rule in rows:
            for trigger_skill_id in rule.trigger_skill_ids:
                result.setdefault(trigger_skill_id, []).append(rule)
    return {key: tuple(rows) for key, rows in result.items()}
