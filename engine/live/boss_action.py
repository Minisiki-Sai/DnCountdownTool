from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from .models import BossActionEvent


_STRONG_EVIDENCE = frozenset({"protocol_action", "structured_action"})


@dataclass(frozen=True, slots=True)
class BossActionEvidence:
    """One independently decoded observation of a Boss action."""

    timestamp_ms: int
    source_actor_id: int
    source_id: str
    source_name: str
    job: str
    template_id: int
    action_code: int
    skill_index: int = 0
    variant_code: int = 0
    action_kind: int = 0
    evidence_kind: str = "protocol_action"
    skill_name: str = ""
    family_key: str = ""
    packet_signature: str = ""
    packet_length: int = 0
    xor_phase: int = 0
    record_offset: int = 0
    mapped: bool = False
    confidence: float = 0.6
    affected_target_id: str = ""

    @property
    def strong(self) -> bool:
        return self.evidence_kind in _STRONG_EVIDENCE


@dataclass(slots=True)
class _PendingBossAction:
    source_actor_id: int
    template_id: int
    start_ms: int
    end_ms: int
    evidence: list[BossActionEvidence] = field(default_factory=list)

    @property
    def strong_evidence(self) -> tuple[BossActionEvidence, ...]:
        return tuple(item for item in self.evidence if item.strong)

    @property
    def family_keys(self) -> frozenset[str]:
        return frozenset(item.family_key for item in self.evidence if item.family_key)

    def add(self, item: BossActionEvidence) -> None:
        self.evidence.append(item)
        self.start_ms = min(self.start_ms, item.timestamp_ms)
        self.end_ms = max(self.end_ms, item.timestamp_ms)


class BossStructuredEventParser:
    """Merge protocol, status and hit observations into Boss action instances.

    The parser never fabricates a mapped skill name. Unknown but independently
    supported actions remain visible as pending-name instances, while repeated
    hit packets extend the same instance instead of becoming fake casts.
    """

    def __init__(
        self,
        catalog: dict[tuple[int, int], tuple[str, bool]] | None = None,
        *,
        settle_ms: int = 2200,
        hit_gap_ms: int = 1800,
        max_instance_ms: int = 12_000,
    ) -> None:
        self.catalog = catalog or {}
        self.settle_ms = max(250, int(settle_ms))
        self.hit_gap_ms = max(100, int(hit_gap_ms))
        self.max_instance_ms = max(self.hit_gap_ms, int(max_instance_ms))
        self._pending: list[_PendingBossAction] = []
        self._recent_emitted_ms: dict[tuple[object, ...], int] = {}

    @staticmethod
    def stable_action_code(*parts: object) -> int:
        payload = "|".join(str(part) for part in parts).encode(
            "utf-8", errors="replace"
        )
        # Keep inferred codes in a separate, positive namespace.
        return 0x40000000 | (zlib.crc32(payload) & 0x3FFFFFFF)

    @staticmethod
    def _strong_identity(item: BossActionEvidence) -> tuple[int, int, int]:
        return (item.action_code, item.variant_code, item.action_kind)

    def _matching_pending(
        self, item: BossActionEvidence
    ) -> _PendingBossAction | None:
        candidates = [
            pending
            for pending in self._pending
            if pending.source_actor_id == item.source_actor_id
            and pending.template_id == item.template_id
            and 0 <= item.timestamp_ms - pending.end_ms <= self.hit_gap_ms
            and item.timestamp_ms - pending.start_ms <= self.max_instance_ms
        ]
        if not candidates:
            return None

        if item.strong:
            identity = self._strong_identity(item)
            same_strong = [
                pending
                for pending in candidates
                if any(
                    self._strong_identity(existing) == identity
                    for existing in pending.strong_evidence
                )
                and item.timestamp_ms - pending.end_ms <= 350
            ]
            if same_strong:
                return max(same_strong, key=lambda pending: pending.end_ms)
            # A strong record can absorb immediately preceding weak evidence,
            # but never merges two independently identified Boss actions.
            weak_only = [
                pending
                for pending in candidates
                if not pending.strong_evidence
                and item.timestamp_ms - pending.end_ms <= 900
            ]
            return max(weak_only, key=lambda pending: pending.end_ms) if weak_only else None

        same_family = [
            pending
            for pending in candidates
            if item.family_key and item.family_key in pending.family_keys
        ]
        if same_family:
            return max(same_family, key=lambda pending: pending.end_ms)
        same_packet = [
            pending
            for pending in candidates
            if pending.end_ms == item.timestamp_ms
            and item.packet_signature
            and any(
                existing.packet_signature == item.packet_signature
                for existing in pending.evidence
            )
        ]
        if same_packet:
            return max(same_packet, key=lambda pending: pending.end_ms)
        strong = [
            pending
            for pending in candidates
            if pending.strong_evidence
            and item.timestamp_ms - pending.end_ms <= self.hit_gap_ms
        ]
        return max(strong, key=lambda pending: pending.end_ms) if strong else None

    def observe(
        self,
        evidence: tuple[BossActionEvidence, ...] | list[BossActionEvidence],
        now_ms: int,
    ) -> list[BossActionEvent]:
        emitted = self._deduplicate_events(
            self._flush_before(now_ms - self.settle_ms)
        )
        for item in sorted(evidence, key=lambda value: value.timestamp_ms):
            pending = self._matching_pending(item)
            if pending is None:
                pending = _PendingBossAction(
                    source_actor_id=item.source_actor_id,
                    template_id=item.template_id,
                    start_ms=item.timestamp_ms,
                    end_ms=item.timestamp_ms,
                )
                self._pending.append(pending)
            pending.add(item)
        return emitted

    @property
    def has_pending(self) -> bool:
        """Whether a later packet timestamp still needs to settle an action."""

        return bool(self._pending)

    def flush(self) -> list[BossActionEvent]:
        rows = sorted(self._pending, key=lambda pending: pending.start_ms)
        self._pending.clear()
        return self._deduplicate_events(
            [self._build_event(pending) for pending in rows]
        )

    def _deduplicate_events(
        self, events: list[BossActionEvent]
    ) -> list[BossActionEvent]:
        result: list[BossActionEvent] = []
        for event in events:
            key = (
                event.source_id,
                event.template_id,
                event.action_code,
                event.skill_index,
                event.variant_code,
                event.action_kind,
            )
            previous_ms = self._recent_emitted_ms.get(key)
            if previous_ms is not None and abs(event.timestamp_ms - previous_ms) <= 250:
                continue
            self._recent_emitted_ms[key] = event.timestamp_ms
            result.append(event)
        if len(self._recent_emitted_ms) > 512 and result:
            cutoff_ms = max(event.timestamp_ms for event in result) - 60_000
            self._recent_emitted_ms = {
                key: timestamp_ms
                for key, timestamp_ms in self._recent_emitted_ms.items()
                if timestamp_ms >= cutoff_ms
            }
        return result

    def _flush_before(self, cutoff_ms: int) -> list[BossActionEvent]:
        ready = [pending for pending in self._pending if pending.end_ms < cutoff_ms]
        if not ready:
            return []
        ready_ids = {id(pending) for pending in ready}
        self._pending = [
            pending for pending in self._pending if id(pending) not in ready_ids
        ]
        return [
            self._build_event(pending)
            for pending in sorted(ready, key=lambda value: value.start_ms)
        ]

    def _build_event(self, pending: _PendingBossAction) -> BossActionEvent:
        ranked = sorted(
            pending.evidence,
            key=lambda item: (
                item.strong,
                item.mapped,
                item.confidence,
                bool(item.skill_name),
            ),
            reverse=True,
        )
        canonical = ranked[0]
        catalog_name, catalog_mapped = self.catalog.get(
            (canonical.template_id, canonical.action_code), ("", False)
        )
        named = next(
            (
                item
                for item in ranked
                if item.skill_name and (item.mapped or item.skill_index > 0)
            ),
            next((item for item in ranked if item.skill_name), canonical),
        )
        mapped = bool(
            catalog_mapped or any(item.mapped for item in pending.evidence)
        )
        skill_name = catalog_name or named.skill_name
        skill_index = next(
            (
                item.skill_index
                for item in ranked
                if item.skill_index > 0
            ),
            0,
        )
        runtime_action_code = canonical.action_code if canonical.strong else 0
        evidence_types = tuple(
            sorted({item.evidence_kind for item in pending.evidence})
        )
        if not skill_name:
            if "status" in evidence_types:
                skill_name = f"状态动作 #{canonical.action_code}（待命名）"
            elif "hit" in evidence_types:
                skill_name = f"命中动作 #{canonical.action_code}（待命名）"
            else:
                skill_name = f"动作 #{canonical.action_code}（待命名）"
        hit_count = sum(item.evidence_kind == "hit" for item in pending.evidence)
        status_count = sum(
            item.evidence_kind == "status" for item in pending.evidence
        )
        affected_target_ids = tuple(
            sorted(
                {
                    item.affected_target_id
                    for item in pending.evidence
                    if item.affected_target_id
                }
            )
        )
        confidence = min(
            1.0,
            max(item.confidence for item in pending.evidence)
            + min(0.18, 0.03 * (len(pending.evidence) - 1)),
        )
        return BossActionEvent(
            timestamp_ms=pending.start_ms,
            end_timestamp_ms=pending.end_ms,
            source_id=canonical.source_id,
            source_name=canonical.source_name,
            job=canonical.job or "Boss",
            skill_id=(
                f"boss:{canonical.template_id}:skill:{skill_index}"
                if skill_index > 0
                else f"boss:{canonical.template_id}:action:{runtime_action_code}"
            ),
            skill_name=skill_name,
            template_id=canonical.template_id,
            action_code=runtime_action_code,
            skill_index=skill_index,
            variant_code=canonical.variant_code,
            action_kind=canonical.action_kind,
            packet_kind="boss_structured_multi_event",
            packet_signature=canonical.packet_signature,
            packet_length=canonical.packet_length,
            xor_phase=canonical.xor_phase,
            record_offset=canonical.record_offset,
            mapped=mapped,
            confidence=confidence,
            evidence_types=evidence_types,
            evidence_count=len(pending.evidence),
            hit_count=hit_count,
            status_count=status_count,
            affected_target_ids=affected_target_ids,
        )
