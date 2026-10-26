"""Burning Hand ownership from action responses AND physical result fields.

The triggering skill's ordinary Hit can precede OR follow the explosion. Never
consume the first result merely because an action response was observed.
Skill_Explosion_Attack's ACT direct-damage signal contains integers 1500/600;
the reviewed direct41 results repeat them at marker+104/+114. Their protocol
semantics remain unnamed: neither is an independent universal skill ID.

Require a complete physical record and an exact EC62/EC637 response context.
Multi-target response bursts can share a filter, and a context can cover more
than one result. No damage, projectile, or missing result is manufactured.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .damage_attribution import is_deterministic_attribution
from .models import CombatEvent
from .udp_damage_records import inspect_udp_damage_records


BURNING_HAND_SKILL_ID = 2301
BURNING_HAND_SKILL_NAME = "灼烧之手"
BURNING_HAND_ACTION_EFFECT_CODE = "135900"
BURNING_HAND_ACTION_EFFECT_CLASS = 62
BURNING_HAND_ACTION_PARAMETER = "Skill_Explosion_Attack"
BURNING_HAND_FILTER_EFFECT_CODE = "138400"
BURNING_HAND_FILTER_EFFECT_CLASS = 637
BURNING_HAND_FILTER_PARAMETER = (
    "2303,0,0,1000;2205,0,0,2200;"
    "2303,291146,0,1000;2205,291144,0,2200"
)
BURNING_HAND_RESULT_WINDOW_SECONDS = 0.150
BURNING_HAND_SHARED_FILTER_WINDOW_SECONDS = 0.005
BURNING_HAND_RESULT_SIGNATURE = (1500, 600)
BURNING_HAND_WIRE_TRIGGER_METHOD = "instant_action_response_wire_trigger"
BURNING_HAND_WIRE_TRIGGER_CONFIDENCE = 0.8
BURNING_HAND_TRIGGER_SKILL_IDS = frozenset({
    "2002", "2202", "2205", "2206", "2207", "2208", "2213",
    "2215", "2217", "2220", "2302", "2303", "2304", "2305", "2306",
})


def is_burning_hand_instant_response(status: object) -> bool:
    """Return whether *status* is one of the two exact internal responses."""

    if (
        int(getattr(status, "skill_id", 0) or 0) != BURNING_HAND_SKILL_ID
        or getattr(status, "duration_ms", None) != 0
        or bool(getattr(status, "permanent", False))
        or int(getattr(status, "effect_instance_id", 0) or 0) <= 0
    ):
        return False
    effect_code = str(getattr(status, "effect_code", "") or "").casefold()
    effect_class = int(getattr(status, "effect_class_id", 0) or 0)
    parameters = str(getattr(status, "parameters", "") or "").strip()
    return bool(
        (
            effect_code == BURNING_HAND_ACTION_EFFECT_CODE
            and effect_class == BURNING_HAND_ACTION_EFFECT_CLASS
            and parameters == BURNING_HAND_ACTION_PARAMETER
        )
        or (
            effect_code == BURNING_HAND_FILTER_EFFECT_CODE
            and effect_class == BURNING_HAND_FILTER_EFFECT_CLASS
            and parameters == BURNING_HAND_FILTER_PARAMETER
        )
    )


@dataclass(slots=True)
class _InstantGeneration:
    source_actor_id: int
    target_actor_id: int
    response_instance_id: int
    created_at: float
    packet_serial: int
    action_record_offset: int = 0
    armed_at: float | None = None
    armed_packet_serial: int = 0
    armed_record_offset: int = 0
    armed_filter_instance_id: int = 0
    attributed_result_count: int = 0

    @property
    def key(self) -> tuple[int, int, int]:
        return (
            self.source_actor_id,
            self.target_actor_id,
            self.response_instance_id,
        )

    @property
    def evidence_id(self) -> str:
        return (
            f"instant-action:{BURNING_HAND_SKILL_ID}:"
            f"{self.source_actor_id:08X}:{self.target_actor_id:08X}:"
            f"{self.response_instance_id}"
        )


class InstantActionDamageLedger:
    """Relabel proven explosion results, not arbitrary neighbouring Hits."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, int, int], _InstantGeneration] = {}
        self._seen: dict[tuple[int, int, int], float] = {}
        self._filters: dict[tuple[int, int], tuple[float, int, int]] = {}
        self.action_generations = 0
        self.paired_generations = 0
        self.attributed_hits = 0
        self.expired_generations = 0
        self.matched_generations = 0
        self.shared_filter_generations = 0

    def reset(self) -> None:
        self._pending.clear()
        self._seen.clear()
        self._filters.clear()

    def _expire(self, timestamp: float) -> None:
        cutoff = float(timestamp) - BURNING_HAND_RESULT_WINDOW_SECONDS
        expired = [
            key
            for key, generation in self._pending.items()
            if generation.created_at < cutoff
        ]
        for key in expired:
            generation = self._pending.pop(key)
            if not generation.attributed_result_count:
                self.expired_generations += 1
        self._filters = {
            key: row for key, row in self._filters.items() if row[0] >= cutoff
        }
        # Response instance IDs are monotonically allocated, but keeping a
        # short duplicate memory bounds long live sessions without allowing a
        # delayed UDP replica to create a second generation.
        seen_cutoff = float(timestamp) - 30.0
        if len(self._seen) > 4096:
            self._seen = {
                key: observed_at
                for key, observed_at in self._seen.items()
                if observed_at >= seen_cutoff
            }

    def observe_status(
        self,
        status: object,
        *,
        timestamp: float,
        packet_serial: int,
    ) -> bool:
        """Observe one response; return True when it is internal-only."""

        if not is_burning_hand_instant_response(status):
            return False
        self._expire(timestamp)
        source = int(getattr(status, "source_actor_id", 0) or 0)
        target = int(getattr(status, "target_actor_id", 0) or 0)
        instance = int(getattr(status, "effect_instance_id", 0) or 0)
        effect_class = int(getattr(status, "effect_class_id", 0) or 0)
        if effect_class == BURNING_HAND_ACTION_EFFECT_CLASS:
            key = (source, target, instance)
            if key not in self._seen:
                generation = _InstantGeneration(
                    source_actor_id=source,
                    target_actor_id=target,
                    response_instance_id=instance,
                    created_at=float(timestamp),
                    packet_serial=max(0, int(packet_serial)),
                    action_record_offset=max(
                        0, int(getattr(status, "record_offset", 0) or 0)
                    ),
                )
                self._pending[key] = generation
                self._seen[key] = float(timestamp)
                self.action_generations += 1
                self._try_arm(generation)
            return True

        # Most filters use action-instance+1. In the reviewed 0815 burst,
        # actions 316229/316231 target different enemies but share filter
        # 316230. Preserve that exact filter for the short multi-target burst;
        # do not require every action to allocate another filter instance.
        if target != source or instance <= 1:
            return True
        self._filters.setdefault((source, instance), (
            float(timestamp), max(0, int(packet_serial)),
            max(0, int(getattr(status, "record_offset", 0) or 0)),
        ))
        # Arm exact pairs first so a shared pair cannot select an unrelated
        # filter solely from temporal proximity.
        candidates = [g for g in self._pending.values()
                      if g.source_actor_id == source and g.armed_at is None]
        for generation in candidates:
            if generation.response_instance_id + 1 == instance:
                self._try_arm(generation)
        for generation in candidates:
            self._try_arm(generation)
        return True

    def _try_arm(self, generation: _InstantGeneration) -> None:
        if generation.armed_at is not None:
            return
        filter_instance = generation.response_instance_id + 1
        row = self._filters.get((generation.source_actor_id, filter_instance))
        shared = False
        if row is None:
            anchors = [g for g in self._pending.values()
                       if g.armed_at is not None
                       and g.source_actor_id == generation.source_actor_id
                       and g.target_actor_id != generation.target_actor_id
                       and abs(g.created_at - generation.created_at)
                       <= BURNING_HAND_SHARED_FILTER_WINDOW_SECONDS]
            filter_ids = {g.armed_filter_instance_id for g in anchors}
            if len(filter_ids) != 1:
                return
            filter_instance = next(iter(filter_ids))
            row = self._filters.get((generation.source_actor_id, filter_instance))
            shared = True
        if row is None or not (
            -BURNING_HAND_SHARED_FILTER_WINDOW_SECONDS
            <= row[0] - generation.created_at
            <= BURNING_HAND_RESULT_WINDOW_SECONDS
        ):
            return
        generation.armed_at, generation.armed_packet_serial, generation.armed_record_offset = row
        generation.armed_filter_instance_id = filter_instance
        self.paired_generations += 1
        self.shared_filter_generations += int(shared)

    @staticmethod
    def _wire_actor(event: CombatEvent, *, source: bool) -> int:
        value = int(
            event.wire_source_actor_id if source else event.wire_target_actor_id
        )
        if value > 0:
            return value
        raw = event.source_id if source else event.target_id.split("@", 1)[0]
        try:
            return int(str(raw), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _eligible_hit(event: CombatEvent) -> bool:
        return bool(
            not event.healing
            and event.amount > 0
            and "direct41" in str(event.wire_damage_layout or "").casefold()
        )

    def attribute(
        self,
        events: list[object] | tuple[object, ...],
        *,
        timestamp: float,
        packet_serial: int,
        payload: bytes | None = None,
        direction: str | None = None,
    ) -> list[object]:
        """Require a current physical signature; never infer from row order."""

        self._expire(timestamp)
        result = list(events)
        if not self._pending or payload is None:
            return result
        candidates = sorted(
            (
                (int(getattr(event, "wire_record_offset", 0) or 0), index, event)
                for index, event in enumerate(result)
                if isinstance(event, CombatEvent) and self._eligible_hit(event)
            ),
            key=lambda row: (row[0], row[1]),
        )
        if not candidates:
            return result
        inventory = inspect_udp_damage_records(payload, direction)
        if not inventory.complete:
            return result
        physical = {
            row.record_offset: row for row in inventory.damage_records
            if row.family == "direct41" and row.admissible_fields
            and (
                int.from_bytes(row.record_bytes[108:110], "little"),
                int.from_bytes(row.record_bytes[118:122], "little"),
            ) == BURNING_HAND_RESULT_SIGNATURE
        }
        for record_offset, index, event in candidates:
            source = self._wire_actor(event, source=True)
            target = self._wire_actor(event, source=False)
            record = physical.get(record_offset)
            if (record is None or record.source_actor_id != source
                or record.target_actor_id != target or record.damage != event.amount
                or record.critical != (event.wire_critical
                                      if event.wire_critical is not None else event.critical)
                or str(event.skill_id) == str(BURNING_HAND_SKILL_ID)):
                continue
            eligible_generations = [
                generation
                for generation in self._pending.values()
                if generation.armed_at is not None
                and generation.source_actor_id == source
                and generation.target_actor_id == target
                and (
                    int(packet_serial) > generation.packet_serial
                    or (int(packet_serial) == generation.packet_serial
                        and record_offset > generation.action_record_offset)
                )
                and (
                    int(packet_serial) > generation.armed_packet_serial
                    or (
                        int(packet_serial) == generation.armed_packet_serial
                        and float(timestamp) >= float(generation.armed_at)
                        and record_offset > generation.armed_record_offset
                    )
                )
                and 0.0
                <= float(timestamp) - generation.created_at
                <= BURNING_HAND_RESULT_WINDOW_SECONDS
            ]
            if not eligible_generations:
                continue
            generation = min(
                eligible_generations,
                key=lambda item: (
                    item.created_at,
                    item.packet_serial,
                    item.response_instance_id,
                ),
            )
            original_skill_id = str(event.skill_id or "")
            original_skill_name = str(event.skill_name or "")
            original_method = str(event.attribution_method or "recorded")
            raw_skill_id = str(event.raw_skill_id or original_skill_id)
            raw_skill_name = str(event.raw_skill_name or original_skill_name)
            # Snapshot the accepted pre-split identity, not raw_skill_*: an
            # exact object mapping may have already corrected a stale packet
            # slot (including Blink). Decide provenance once here. Optional
            # display merging restores it without a second trigger search or
            # confidence gate; timing/candidate fallbacks remain unconfirmed.
            origin_confirmed = bool(
                original_skill_id in BURNING_HAND_TRIGGER_SKILL_IDS
                and is_deterministic_attribution(original_method)
                and event.has_real_hit_evidence is True
            )
            # The result slot can contain an unrelated last-used action
            # (including Blink) while a persistent fire skill triggers this
            # passive. Preserve that raw slot without claiming it triggered
            # the explosion. Ownership and trigger evidence are separate.
            trigger_known = raw_skill_id in BURNING_HAND_TRIGGER_SKILL_IDS
            result[index] = replace(
                event,
                skill_id=str(BURNING_HAND_SKILL_ID),
                skill_name=BURNING_HAND_SKILL_NAME,
                attribution_method="instant_action_response",
                raw_skill_id=raw_skill_id,
                raw_skill_name=raw_skill_name,
                raw_attribution_method=(
                    event.raw_attribution_method or original_method
                ),
                cast_instance_id=generation.evidence_id,
                attribution_confidence=0.99,
                has_real_hit_evidence=True,
                burning_hand_origin_skill_id=original_skill_id,
                burning_hand_origin_skill_name=original_skill_name,
                burning_hand_origin_attribution_method=original_method,
                burning_hand_origin_confirmed=origin_confirmed,
                trigger_skill_id=raw_skill_id if trigger_known else "",
                trigger_skill_name=raw_skill_name if trigger_known else "",
                trigger_attribution_method=(
                    BURNING_HAND_WIRE_TRIGGER_METHOD if trigger_known else ""
                ),
                trigger_attribution_confidence=(
                    BURNING_HAND_WIRE_TRIGGER_CONFIDENCE if trigger_known else 0.0
                ),
            )
            if not generation.attributed_result_count:
                self.matched_generations += 1
            generation.attributed_result_count += 1
            self.attributed_hits += 1
        return result

    def stats(self) -> dict[str, int]:
        return {
            "action_generations": self.action_generations,
            "paired_generations": self.paired_generations,
            "attributed_hits": self.attributed_hits,
            "expired_generations": self.expired_generations,
            "pending_generations": len(self._pending),
            "matched_generations": self.matched_generations,
            "shared_filter_generations": self.shared_filter_generations,
        }
