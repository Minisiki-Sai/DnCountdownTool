from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from .damage_attribution import AttributionLayer, attribution_layer
from .ex_skill_resolution import ExSkillResolver
from .models import CombatEvent, SkillCastEvent


UNKNOWN_SKILL_ID = "-1"
UNKNOWN_SKILL_NAME = "待识别伤害"
NORMAL_ATTACK_SKILL_ID = "4294967295"
NORMAL_ATTACK_SKILL_NAME = "普通/特殊攻击"


class EvidenceTier(str, Enum):
    """Evidence admitted by the second-generation attribution engine."""

    PACKET = "packet"
    OBJECT = "object"
    ENTITY = "entity"
    STATUS = "status"
    ADDITIONAL = "additional"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class AttributionDecision:
    accepted: bool
    tier: EvidenceTier
    reason: str


@dataclass(frozen=True, slots=True)
class V2AttributionResult:
    events: tuple[CombatEvent, ...]
    cast_events: tuple[SkillCastEvent, ...]
    accepted_hits: int
    unresolved_hits: int
    accepted_damage: int
    unresolved_damage: int


_UNKNOWN_IDS = frozenset({"", "-1", "unknown", "unresolved", "4294967295", "0xffffffff"})
_UNKNOWN_NAMES = frozenset({"", UNKNOWN_SKILL_NAME, "未学习伤害"})
_PACKET_METHODS = frozenset(
    {
        "packet_skill_id",
        "packet_native_attack_key",
        "packet_dungeon_special_skill",
        "packet_basic_attack_key",
    }
)
_OBJECT_METHODS = frozenset(
    {
        "damage_object_projectile_link",
        "player_root_action_signal_object",
        "player_root_config_object",
        "player_child_config_object",
        "field_effect_status_phase",
        "field_effect_result_object_link",
        "instant_action_response",
        "named_attack_response_phase",
    }
)
_ENTITY_METHODS = frozenset(
    {
        "packet_entity_skill_id",
        "packet_entity_native_attack_key",
        "packet_entity_projectile_selector",
        "packet_entity_self_attack",
        "packet_entity_template_owner",
        "packet_entity_damage_family",
    }
)
_ENTITY_STATUS_METHODS = frozenset({"packet_entity_status_damage"})
_STATUS_METHODS = frozenset(
    {"periodic_status_packet", "status_damage_packet", "status_damage_skill"}
)
_ADDITIONAL_METHODS = frozenset(
    {
        "direct_hit_overflow",
        # The damage row itself is an anonymous supplemental/periodic row,
        # while ownership is gated by an exact self-applied EX status
        # generation and its lifetime.  Keep it outside direct-Hit counting,
        # but retain the proven EX damage bucket in strict V2.
        "refreshable_pulsing_aura",
        "supplemental_continuation",
        "supplemental_same_packet",
        "supplemental_cast_owner",
    }
)
_DAMAGE_DERIVED_CAST_KINDS = frozenset(
    {"damage_inferred", "shared_timeline_damage"}
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _method(event: CombatEvent) -> str:
    return _text(event.attribution_method).casefold()


def _has_skill_identity(event: CombatEvent) -> bool:
    skill_id = _text(event.skill_id).casefold()
    skill_name = _text(event.skill_name)
    if (
        skill_id in {"4294967295", "0xffffffff"}
        and skill_name == "普通/特殊攻击"
        and _method(event)
        in {
            "packet_basic_attack_key",
            "damage_object_projectile_link",
            "player_root_action_signal_object",
        }
    ):
        return True
    if (
        skill_id in {"4294967295", "0xffffffff"}
        and skill_name == "弩系普通攻击"
        and _method(event) == "damage_object_projectile_link"
    ):
        return True
    return skill_id not in _UNKNOWN_IDS and skill_name not in _UNKNOWN_NAMES


def _has_wire_damage_fact(event: CombatEvent) -> bool:
    return bool(
        event.has_real_hit_evidence is True
        and event.amount > 0
        and event.wire_damage_layout
        and event.wire_source_actor_id > 0
    )


def _is_additional_or_periodic_bucket(event: CombatEvent) -> bool:
    method = _method(event)
    # This route repairs a physical Hit formerly classified as shock. Its
    # retained raw label must not bypass the exact certificate gate below.
    if method == "packet_compound_status_response":
        return False
    skill_id = _text(event.skill_id).casefold()
    raw_skill_id = _text(event.raw_skill_id).casefold()
    # Raw labels are audit history. They cannot bypass the current packet,
    # object or entity certificate, or turn its genuine Hit into a DOT.
    if (method in _PACKET_METHODS | _OBJECT_METHODS | _ENTITY_METHODS
            and not skill_id.startswith(("supplemental:", "periodic:"))):
        return False
    return bool(
        method in _ADDITIONAL_METHODS
        or skill_id.startswith(("supplemental:", "periodic:"))
        or raw_skill_id.startswith(("supplemental:", "periodic:"))
    )


def _is_proven_basic_attack(event: CombatEvent) -> bool:
    """Return whether a deterministic route proves a non-skill attack.

    Different client actions used separate display labels historically.  V2
    intentionally merges them into one audit bucket only after either the
    concrete packet key or a stable action+object signature has been accepted.
    """

    method = _method(event)
    skill_id = _text(event.skill_id).casefold()
    skill_name = _text(event.skill_name)
    if method == "packet_basic_attack_key":
        return skill_id in {"4294967295", "0xffffffff"}
    if method == "damage_object_projectile_link":
        return bool(
            skill_id in {"4294967295", "0xffffffff"}
            and skill_name in {"弩系普通攻击", NORMAL_ATTACK_SKILL_NAME}
        )
    if method == "player_root_action_signal_object":
        return bool(
            skill_id.startswith("basic_attack:")
            or skill_name in {"卡莉扇刃普通攻击", NORMAL_ATTACK_SKILL_NAME}
        )
    return False


class SkillAttributionEngineV2:
    """Resolve skills only from independently decoded wire evidence.

    There is deliberately no candidate score and no recent-cast selector.
    Time may constrain an already proven object/status generation, but cannot
    create a skill identity.
    """

    def __init__(self, ex_skill_resolver: ExSkillResolver | None = None) -> None:
        self.ex_skill_resolver = ex_skill_resolver or ExSkillResolver()

    @staticmethod
    def decide(event: CombatEvent) -> AttributionDecision:
        if event.healing or event.source_id == "__incoming_damage__":
            return AttributionDecision(True, EvidenceTier.ADDITIONAL, "non_outgoing_damage")
        if _is_additional_or_periodic_bucket(event):
            return AttributionDecision(True, EvidenceTier.ADDITIONAL, "separate_damage_bucket")
        if not _has_skill_identity(event):
            return AttributionDecision(False, EvidenceTier.UNRESOLVED, "missing_skill_identity")

        method = _method(event)
        layer = attribution_layer(method)
        if method in _PACKET_METHODS:
            # These routes are emitted only after the decoder has read the
            # fixed result field. Legacy serialized/synthetic events may
            # predate the wire metadata fields and use None for hit evidence;
            # an explicit False is still a veto.
            accepted = bool(event.amount > 0 and event.has_real_hit_evidence is not False)
            return AttributionDecision(
                accepted,
                EvidenceTier.PACKET if accepted else EvidenceTier.UNRESOLVED,
                "explicit_wire_attack_key" if accepted else "packet_method_without_wire_fact",
            )

        if method in _OBJECT_METHODS:
            if not _has_wire_damage_fact(event):
                return AttributionDecision(False, EvidenceTier.UNRESOLVED, "object_method_without_wire_fact")
            evidence_id = _text(event.cast_instance_id).casefold()
            if method == "instant_action_response":
                accepted = bool(
                    evidence_id.startswith("instant-action:2301:")
                    and "direct41" in _text(event.wire_damage_layout).casefold()
                )
                return AttributionDecision(
                    accepted,
                    EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                    "paired_instant_action_generation"
                    if accepted else "missing_instant_action_generation",
                )
            if method == "named_attack_response_phase":
                accepted = bool(
                    evidence_id.startswith("named-attack:3202:")
                    and event.wire_damage_object_id > 0
                    and "direct37" in _text(event.wire_damage_layout).casefold()
                )
                return AttributionDecision(
                    accepted,
                    EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                    "named_attack_complete_pulse"
                    if accepted else "missing_named_attack_pulse",
                )
            # The v25 action/signal ledger is normalized by the decoder to
            # ``damage_object_projectile_link`` before V2 sees the event.
            # Preserve that exact proof across the normalization: the
            # act-signal token binds an independently decoded player action
            # and signal ordinal to this object's full ID.  It is not a time
            # window and must remain distinct from an ordinary object-only
            # label.
            if method == "damage_object_projectile_link" and evidence_id.startswith("act-signal:"):
                accepted = bool(event.wire_damage_object_id > 0)
                return AttributionDecision(
                    accepted,
                    EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                    "exact_action_signal_object" if accepted else "missing_object_generation",
                )
            if method == "field_effect_status_phase":
                accepted = evidence_id.startswith("field-effect:")
                return AttributionDecision(
                    accepted,
                    EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                    "field_generation_and_attack_phase" if accepted else "missing_field_generation",
                )
            if method == "field_effect_result_object_link":
                accepted = bool(
                    evidence_id.startswith("field-effect:")
                    and event.wire_result_group_id > 0
                    and event.wire_damage_object_id > 0
                )
                return AttributionDecision(
                    accepted,
                    EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                    "same_result_group_and_object"
                    if accepted else "missing_result_object_generation",
                )
            accepted = bool(
                event.wire_damage_object_id > 0
                and (
                    evidence_id.startswith("damage-object:")
                    or method.startswith("player_root_")
                    or (method == "player_child_config_object"
                        and evidence_id.startswith("player-child-object:"))
                )
            )
            return AttributionDecision(
                accepted,
                EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                "object_generation_chain" if accepted else "missing_object_generation",
            )

        # The opt-in shaman/illusion ledger emits an exact action/signal
        # object proof before the general decoder can resolve its shared
        # Projectile template.  Keep this narrow prefix separate from the
        # generic provisional object route, which remains rejected.
        if method == "damage_object_projectile_link_provisional":
            evidence_id = _text(event.cast_instance_id).casefold()
            accepted = bool(
                _has_wire_damage_fact(event)
                and event.wire_damage_object_id > 0
                and evidence_id.startswith("act-signal:")
            )
            return AttributionDecision(
                accepted,
                EvidenceTier.OBJECT if accepted else EvidenceTier.UNRESOLVED,
                "exact_action_signal_object" if accepted else "missing_exact_action_signal",
            )

        if method in _ENTITY_METHODS:
            accepted = bool(
                _has_wire_damage_fact(event)
                and event.wire_entity_id > 0
                and event.entity_template_id > 0
                and (
                    _text(event.entity_owner_evidence_id)
                    or _text(event.entity_attack_evidence_id)
                )
            )
            return AttributionDecision(
                accepted,
                EvidenceTier.ENTITY if accepted else EvidenceTier.UNRESOLVED,
                "entity_generation_and_owner_chain" if accepted else "missing_entity_generation_proof",
            )

        if method in _ENTITY_STATUS_METHODS:
            accepted = bool(
                event.amount > 0
                and "status-damage" in _text(event.wire_damage_layout).casefold()
                and event.wire_entity_id > 0
                and event.entity_template_id > 0
                and _text(event.entity_owner_evidence_id)
            )
            return AttributionDecision(
                accepted,
                EvidenceTier.ENTITY if accepted else EvidenceTier.UNRESOLVED,
                "entity_generation_owner_and_status_damage"
                if accepted else "missing_entity_status_generation_proof",
            )

        if method == "packet_compound_status_response":
            accepted = bool(
                _has_wire_damage_fact(event)
                and _text(event.skill_id) == "3407"
                and "direct37" in _text(event.wire_damage_layout)
                and _text(event.cast_instance_id).startswith(
                    f"compound-status:{event.skill_id}:"
                )
            )
            return AttributionDecision(
                accepted, EvidenceTier.STATUS if accepted else EvidenceTier.UNRESOLVED,
                "unique_same_packet_physical_response" if accepted
                else "missing_compound_response_certificate",
            )

        if method in _STATUS_METHODS or layer is AttributionLayer.STATUS:
            accepted = method in _STATUS_METHODS
            return AttributionDecision(
                accepted,
                EvidenceTier.STATUS if accepted else EvidenceTier.UNRESOLVED,
                "explicit_status_damage_record" if accepted else "legacy_status_timing",
            )

        return AttributionDecision(False, EvidenceTier.UNRESOLVED, "legacy_or_ambiguous_route")

    @staticmethod
    def _unresolved(event: CombatEvent, decision: AttributionDecision) -> CombatEvent:
        original_method = _method(event) or "unresolved"
        return replace(
            event,
            skill_id=UNKNOWN_SKILL_ID,
            skill_name=UNKNOWN_SKILL_NAME,
            attribution_method="v2_unresolved",
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=(
                event.raw_attribution_method
                or f"{original_method}:{decision.reason}"
            ),
            # Keep the decoded object/field/entity generation evidence even
            # when the skill identity is withheld.  V2 must clear only the
            # unproven display identity; erasing this fact would prevent a
            # later lifecycle/template decoder from closing the chain.
            cast_instance_id=event.cast_instance_id,
            attribution_confidence=0.0,
        )

    def resolve(
        self,
        events: Iterable[CombatEvent],
        cast_events: Iterable[SkillCastEvent],
    ) -> V2AttributionResult:
        event_rows = tuple(events)
        field_anchor_methods = frozenset({"field_effect_status_phase"})
        # A legacy "result group" is random-state input. Only an independently
        # validated complete datagram may bound this same-object sibling proof.
        result_object_identities: dict[
            tuple[str, int, str, int], set[tuple[str, str]]
        ] = {}
        field_anchors: dict[
            tuple[str, int, str, int], list[CombatEvent]
        ] = {}
        for event in event_rows:
            key = (
                _text(event.source_id),
                int(event.wire_source_actor_id or 0),
                _text(event.wire_packet_sha256),
                int(event.wire_damage_object_id or 0),
            )
            if not all(key) or not self.decide(event).accepted:
                continue
            result_object_identities.setdefault(key, set()).add(
                (_text(event.skill_id), _text(event.skill_name))
            )
            if _method(event) in field_anchor_methods:
                field_anchors.setdefault(key, []).append(event)

        reusable_field_anchors: dict[tuple[str, int, str, int], CombatEvent] = {}
        for key, anchors in field_anchors.items():
            identities = result_object_identities.get(key, set())
            evidence_ids = {_text(anchor.cast_instance_id) for anchor in anchors}
            if len(identities) == 1 and len(evidence_ids) == 1:
                reusable_field_anchors[key] = anchors[0]

        resolved: list[CombatEvent] = []
        accepted_hits = unresolved_hits = 0
        accepted_damage = unresolved_damage = 0
        for event in event_rows:
            decision = self.decide(event)
            if not decision.accepted and not _is_additional_or_periodic_bucket(event):
                key = (
                    _text(event.source_id),
                    int(event.wire_source_actor_id or 0),
                    _text(event.wire_packet_sha256),
                    int(event.wire_damage_object_id or 0),
                )
                anchor = reusable_field_anchors.get(key)
                if anchor is not None and _has_wire_damage_fact(event):
                    event = replace(
                        event,
                        skill_id=anchor.skill_id,
                        skill_name=anchor.skill_name,
                        attribution_method="field_effect_result_object_link",
                        raw_skill_id=event.raw_skill_id or event.skill_id,
                        raw_skill_name=event.raw_skill_name or event.skill_name,
                        raw_attribution_method=(
                            event.raw_attribution_method
                            or f"{_method(event) or 'unresolved'}:same_result_object"
                        ),
                        cast_instance_id=anchor.cast_instance_id,
                        attribution_confidence=anchor.attribution_confidence,
                    )
                    decision = self.decide(event)
            if decision.accepted:
                if _is_proven_basic_attack(event):
                    event = replace(
                        event,
                        skill_id=NORMAL_ATTACK_SKILL_ID,
                        skill_name=NORMAL_ATTACK_SKILL_NAME,
                    )
                resolved.append(event)
                if not event.healing and event.amount > 0:
                    accepted_hits += 1
                    accepted_damage += int(event.amount)
                continue
            unresolved = self._unresolved(event, decision)
            resolved.append(unresolved)
            if not event.healing and event.amount > 0:
                unresolved_hits += 1
                unresolved_damage += int(event.amount)

        factual_casts = tuple(
            self.ex_skill_resolver.upgrade_cast(cast)
            for cast in cast_events
            if _text(cast.packet_kind).casefold()
            not in _DAMAGE_DERIVED_CAST_KINDS
        )
        return V2AttributionResult(
            events=tuple(resolved),
            cast_events=factual_casts,
            accepted_hits=accepted_hits,
            unresolved_hits=unresolved_hits,
            accepted_damage=accepted_damage,
            unresolved_damage=unresolved_damage,
        )
