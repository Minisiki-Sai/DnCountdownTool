from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AttributionLayer(str, Enum):
    """Evidence route used to bind one factual damage row to a skill."""

    OBJECT = "object"
    ENTITY = "entity"
    STATUS = "status"
    DIRECT = "direct"
    INFERENCE = "inference"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class AttributionRule:
    layer: AttributionLayer
    priority: int
    deterministic: bool = False
    persistent: bool = False
    # Locked evidence must not be moved to another skill merely because a
    # nearby cast has a better timing score. Some legacy inference routes are
    # intentionally locked until their replacements have corpus parity.
    locked: bool = False


def _rule(
    layer: AttributionLayer,
    priority: int,
    *,
    deterministic: bool = False,
    persistent: bool = False,
    locked: bool = False,
) -> AttributionRule:
    return AttributionRule(
        layer=layer,
        priority=priority,
        deterministic=deterministic,
        persistent=persistent,
        locked=locked,
    )


# One registry owns the precedence contract for the decoder, the cast ledger
# and attribution audits. A new protocol route must be classified here before
# it can silently outrank another source of evidence.
ATTRIBUTION_RULES: dict[str, AttributionRule] = {
    # Exact action/response -> Projectile template -> damage_object_id -> hit.
    "damage_object_projectile_link": _rule(
        AttributionLayer.OBJECT,
        500,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # Exact owner+object linkage with a configured client Projectile mapping,
    # but the template->skill edge has not yet passed the independent
    # fixed-slot packet truth gate. Keep the existing skill bucket and lock
    # semantics without reporting it as proven attribution.
    "damage_object_projectile_link_provisional": _rule(
        AttributionLayer.OBJECT,
        # A provisional template bridge may fill a skill-less object hit, but
        # it must never outrank an exact skill ID carried by that damage row.
        # Packet-validated object links keep the higher priority above.
        309,
        # This is a display candidate, not protocol truth. It may be replaced
        # when a later fixed-slot, entity or validated object edge arrives.
        persistent=False,
        locked=False,
    ),
    # Legacy action-window bridge retained only for old diagnostics/cache
    # compatibility.  Time proximity is not deterministic evidence and new
    # decoding never emits this method.
    "player_root_action_object": _rule(
        AttributionLayer.INFERENCE,
        275,
    ),
    # Exact A90370 action/signal coordinates resolved against the pinned
    # merged player ACT manifest.  Admission also requires signal type 15 and
    # signal payload[0:4] == the wire config row; no cast-time window is used.
    "player_root_action_signal_object": _rule(
        AttributionLayer.OBJECT,
        495,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # A complete root A90370 generation carries its concrete client config
    # row.  Jobs admitted by the fixed-corpus zero-conflict gate can resolve a
    # one-to-one config -> Projectile -> canonical-skill edge without using a
    # timing window or the event's previous display name.
    "player_root_config_object": _rule(
        AttributionLayer.OBJECT,
        492,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    "player_child_config_object": _rule(
        AttributionLayer.OBJECT, 492,
        deterministic=True, persistent=True, locked=True,
    ),
    # Runtime entity ownership. The entity can outlive its owner's action.
    "packet_entity_skill_id": _rule(
        AttributionLayer.ENTITY, 446, deterministic=True, persistent=True, locked=True,
    ),
    # The exact per-result key selected by the client's own damage meter,
    # after current-generation entity ownership and a unique static internal
    # attack -> player skill edge have both been proven.
    "packet_entity_native_attack_key": _rule(
        AttributionLayer.ENTITY, 447, deterministic=True, persistent=True, locked=True,
    ),
    "packet_entity_projectile_selector": _rule(
        AttributionLayer.ENTITY, 445, deterministic=True, persistent=True, locked=True,
    ),
    "packet_entity_self_attack": _rule(
        AttributionLayer.ENTITY, 440, deterministic=True, persistent=True, locked=True,
    ),
    "packet_entity_template_owner": _rule(
        AttributionLayer.ENTITY, 430, deterministic=True, persistent=True, locked=True,
    ),
    # Exact current-generation entity ownership plus a reviewed
    # entity-template -> parent summon family edge.  This deliberately says
    # only which summoned object produced the Hit; a concrete child attack
    # (for example Heavy Duck bomb/siren) still needs its own native slot or
    # Projectile chain and has higher priority above.
    "packet_entity_damage_family": _rule(
        AttributionLayer.ENTITY, 425, deterministic=True, persistent=True, locked=True,
    ),
    # Factual status-damage row produced by a current-generation autonomous
    # entity whose exact owner and reviewed single-skill template are known.
    # It is deterministic attribution, but remains excluded from direct-Hit
    # mechanics through has_real_hit_evidence=False.
    "packet_entity_status_damage": _rule(
        AttributionLayer.ENTITY, 424, deterministic=True, persistent=True, locked=True,
    ),
    "skill_object_owner": _rule(
        AttributionLayer.ENTITY,
        420,
        persistent=True,
        locked=True,
    ),
    "summon_owner": _rule(
        AttributionLayer.ENTITY,
        410,
        persistent=True,
        locked=True,
    ),
    "supplemental_summon_owner": _rule(
        AttributionLayer.ENTITY,
        405,
        persistent=True,
        locked=True,
    ),
    "retroactive_permanent_summon_owner": _rule(
        AttributionLayer.ENTITY,
        400,
        persistent=True,
        locked=True,
    ),
    # Status/effect instance ownership.
    "periodic_status_packet": _rule(
        AttributionLayer.STATUS,
        460,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    "status_damage_packet": _rule(
        AttributionLayer.STATUS,
        455,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    "status_damage_skill": _rule(
        AttributionLayer.STATUS,
        455,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # Exact single physical result + reviewed same-source/target EC43 response
    # in one fully validated datagram. No nearby-packet status window.
    "packet_compound_status_response": _rule(
        AttributionLayer.STATUS, 457,
        deterministic=True, persistent=True, locked=True,
    ),
    # Exact skill action -> 135D runtime status generation -> client ACT
    # attack phase -> physical direct37 result.  Unlike the legacy
    # continuation window, the field must have an independently observed
    # server instance and the result must land in template 11046's attack
    # phase.  A contradictory live object candidate is rejected upstream.
    "field_effect_status_phase": _rule(
        # This is direct damage from a status-created field object, not a
        # periodic-status damage row.  Keep it in the object layer so direct
        # Hit audits and skill composition retain the physical Hit.
        AttributionLayer.OBJECT,
        490,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # A named Lightning Charger attack response supplies source, target and
    # lifetime; the physical direct37 pulse must independently have the
    # complete normal/EX object count at the response's 2-second phase.
    "named_attack_response_phase": _rule(
        AttributionLayer.OBJECT,
        488,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # A multi-target datagram can contain more than one physical row for the
    # same field object. Once one row has the exact field generation proof,
    # a complete-datagram hash + full source + damage-object tuple bounds
    # its sibling rows. A legacy random prefix is not a packet identity.  A conflicting accepted skill vetoes this bridge
    # in SkillAttributionEngineV2.
    "field_effect_result_object_link": _rule(
        AttributionLayer.OBJECT,
        489,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    # Exact zero-duration server action + paired filter generation -> the
    # next physical direct41 result for the same source/target.  The response
    # instance is an action token, not a projectile ID, but the full lifecycle
    # is deterministic and must outrank the trigger skill carried by the hit.
    "instant_action_response": _rule(
        AttributionLayer.OBJECT,
        491,
        deterministic=True,
        persistent=True,
        locked=True,
    ),
    "periodic_status": _rule(
        AttributionLayer.STATUS,
        450,
        persistent=True,
        locked=True,
    ),
    # The status and its first damage row are carried by one packet, but the
    # owning skill is still selected from a recent action window. Keep the
    # useful skill bucket while reporting the provenance honestly.
    "appended_status_first_hit": _rule(
        AttributionLayer.INFERENCE,
        175,
        locked=True,
    ),
    # Skill ID or owner is present in the damage record itself.
    "packet_skill_id": _rule(
        AttributionLayer.DIRECT,
        310,
        deterministic=True,
        locked=True,
    ),
    # A reviewed encounter-owned attack key carried by this concrete damage
    # result.  It intentionally bypasses player profession/skill catalogs.
    "packet_dungeon_special_skill": _rule(
        AttributionLayer.DIRECT,
        320,
        deterministic=True,
        locked=True,
    ),
    "packet_native_attack_key": _rule(
        AttributionLayer.DIRECT,
        315,
        deterministic=True,
        locked=True,
    ),
    # Ordinary/special basic attacks are admitted only when the concrete
    # packet supplies their attack key. Stable action+object signatures keep
    # their existing player_root_action_signal_object route.
    "packet_basic_attack_key": _rule(
        AttributionLayer.DIRECT,
        314,
        deterministic=True,
        locked=True,
    ),
    # SkillAttributionEngineV2 removes an ambiguous legacy skill name while
    # preserving the immutable physical damage row.
    "v2_unresolved": _rule(
        AttributionLayer.UNRESOLVED,
        0,
    ),
    # Deprecated result-group inheritance. Corpus leave-one-out validation
    # proved that one actor can reuse the same value for different skills even
    # inside a 5 ms/contiguous result batch. Keep the labels readable for old
    # diagnostic data, but never treat them as deterministic evidence.
    "direct_result_group_link": _rule(
        AttributionLayer.INFERENCE,
        163,
        locked=True,
    ),
    "direct_result_group_run_link": _rule(
        AttributionLayer.INFERENCE,
        162,
        locked=True,
    ),
    "supplemental_same_packet": _rule(
        AttributionLayer.INFERENCE,
        169,
        locked=True,
    ),
    # These IDs were found by scanning a compound payload and then selected
    # by uniqueness, a recent cast, an actor profile, or candidate ranking.
    # They are not fixed protocol fields and must not count as exact evidence.
    "embedded_single_candidate": _rule(
        AttributionLayer.INFERENCE, 168, locked=True
    ),
    "compound_candidate_recent_cast": _rule(
        AttributionLayer.INFERENCE, 167, locked=True
    ),
    "compound_candidate_profile": _rule(
        AttributionLayer.INFERENCE, 166, locked=True
    ),
    "compound_candidate_ranked": _rule(
        AttributionLayer.INFERENCE, 164, locked=True
    ),
    # Heuristic/fallback routes. These retain the old lock/persistence policy
    # where required, but are never reported as deterministic evidence.
    "refreshable_pulsing_aura": _rule(
        AttributionLayer.INFERENCE,
        190,
        persistent=True,
        locked=True,
    ),
    "action_free_special_attack": _rule(
        AttributionLayer.INFERENCE, 185, locked=True
    ),
    # A later continuation action proves one otherwise hidden intermediate
    # button, while the concrete damage row carries the basic/special key.
    # This is stronger than generic timing proximity but remains action-chain
    # inference rather than a packet-embedded Skill ID.
    "derived_combo_basic_attack": _rule(
        AttributionLayer.INFERENCE, 184, locked=True
    ),
    "direct_hit_overflow": _rule(
        AttributionLayer.INFERENCE, 180, locked=True
    ),
    "periodic_cast_fallback": _rule(
        AttributionLayer.INFERENCE, 175, persistent=True
    ),
    "continuation_window": _rule(AttributionLayer.INFERENCE, 170),
    "learned_cast_profile": _rule(AttributionLayer.INFERENCE, 165),
    # Strict V2 left the Hit unresolved; a later resolver found one unique
    # factual action whose unpacked damage frame fits with a sufficient margin.
    # This remains timing inference and must never outrank packet/object proof.
    "action_hit_timing_fallback": _rule(AttributionLayer.INFERENCE, 161),
    "latest_profiled_direct_cast": _rule(AttributionLayer.INFERENCE, 160),
    "unique_active_cast": _rule(AttributionLayer.INFERENCE, 155),
    "sustained_cast_window": _rule(AttributionLayer.INFERENCE, 150),
    "supplemental_cast_owner": _rule(AttributionLayer.INFERENCE, 145),
    "supplemental_continuation": _rule(AttributionLayer.INFERENCE, 140),
    "cast_instance_match": _rule(AttributionLayer.INFERENCE, 130),
    # Legacy/synthetic rows deliberately preserve their previous mutable
    # behaviour; they do not acquire protocol authority by being old data.
    "recorded": _rule(AttributionLayer.INFERENCE, 120),
    "direct": _rule(AttributionLayer.INFERENCE, 120),
    # Explicitly rejected and unresolved candidates.
    "unresolved": _rule(AttributionLayer.UNRESOLVED, 0),
    "unresolved_transformed_base_id": _rule(AttributionLayer.UNRESOLVED, 0),
    "entity_owner_skill_conflict": _rule(
        AttributionLayer.UNRESOLVED, 304, locked=True
    ),
    "bundled_direct_unresolved": _rule(AttributionLayer.UNRESOLVED, 0),
    "appended_status_skill": _rule(AttributionLayer.UNRESOLVED, 0),
    "awaiting_damage_object_projectile_link": _rule(
        AttributionLayer.UNRESOLVED, 0
    ),
    "object_candidate_skill_conflict": _rule(
        AttributionLayer.UNRESOLVED, 0, locked=True
    ),
    "rejected_non_damage_id": _rule(AttributionLayer.UNRESOLVED, 0),
    "rejected_cross_job_id": _rule(AttributionLayer.UNRESOLVED, 0),
    "rejected_uncorroborated_packet_skill": _rule(
        AttributionLayer.UNRESOLVED, 0
    ),
    "rejected_transformed_base_id": _rule(
        AttributionLayer.UNRESOLVED, 0
    ),
}


_UNKNOWN_RULE = _rule(AttributionLayer.INFERENCE, 100)
_UNRESOLVED_RULE = _rule(AttributionLayer.UNRESOLVED, 0)


def attribution_rule(method: object) -> AttributionRule:
    key = str(method or "").strip().casefold()
    if not key or key == "unresolved" or key.startswith(("rejected_", "awaiting_", "unresolved_")):
        return _UNRESOLVED_RULE
    return ATTRIBUTION_RULES.get(key, _UNKNOWN_RULE)


def attribution_layer(method: object) -> AttributionLayer:
    return attribution_rule(method).layer


def attribution_priority(method: object) -> int:
    return attribution_rule(method).priority


def is_deterministic_attribution(method: object) -> bool:
    return attribution_rule(method).deterministic


def is_persistent_attribution(method: object) -> bool:
    return attribution_rule(method).persistent


def is_locked_attribution(method: object) -> bool:
    return attribution_rule(method).locked


def should_prefer_damage_object_skill(
    *,
    packet_skill_exact: bool,
    object_mapping_packet_validated: bool,
) -> bool:
    """Choose the skill edge without discarding stronger wire truth.

    The object chain may still be required to recover the owning player and
    cast instance. Its configured template-to-skill edge must never replace a
    skill ID carried by the exact fixed slot of this damage row. A disagreement
    is evidence that the object generation or mapping needs more work, not a
    reason to discard wire truth.
    """

    del object_mapping_packet_validated
    return not packet_skill_exact


def should_replace_attribution(
    current: object, candidate: object, *,
    native_primary_key: bool = False,
    physical_hit_proven: bool = False,
) -> bool:
    """Return whether candidate evidence may replace current ownership.

    Equal-priority evidence keeps the first decoded owner to make replay
    deterministic. Higher-level deterministic evidence always beats packet or
    timing inference; inference can only fill a missing/rejected owner or beat
    a weaker inference route.
    """

    current_rule = attribution_rule(current)
    candidate_rule = attribution_rule(candidate)
    # A target's active DOT is not the attack identity of a later result.
    # Native fallback fields and legacy packet_skill_id labels are not
    # interchangeable with the client's validated positive primary key.
    # Callers must first prove that exact key or a physical Hit plus its
    # current object/entity certificate. Factual status packets retain the
    # ordinary precedence contract below.
    if str(current or "").strip().casefold() == "periodic_status":
        method = str(candidate or "").strip().casefold()
        if native_primary_key and method in {
            "packet_native_attack_key", "packet_dungeon_special_skill",
        }:
            return True
        if (physical_hit_proven and candidate_rule.deterministic
                and candidate_rule.layer in {AttributionLayer.OBJECT, AttributionLayer.ENTITY}
                and method not in {
                    "packet_entity_status_damage", "player_root_config_object",
                    "player_root_action_object", "damage_object_projectile_link_provisional",
                }):
            return True
    return candidate_rule.priority > current_rule.priority
