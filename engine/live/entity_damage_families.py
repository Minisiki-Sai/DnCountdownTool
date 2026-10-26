"""Conservative entity-template -> parent summon damage families.

This is a presentation/aggregation identity, not a child-attack guess.  It is
used only after an existing real Hit has an exact current entity birth and
same-generation 34/2c owner proof.  Exact native attack, selector and
Projectile routes run first and therefore retain their more specific skill.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from types import MappingProxyType

from .damage_attribution import AttributionLayer, attribution_layer, is_deterministic_attribution


ENTITY_DAMAGE_FAMILY_SKILLS = MappingProxyType({
    # Cleric relic families.  Strengthened/EX variants retain the same parent
    # player skill.  Commanded sub-attacks are intentionally aggregated under
    # the relic when their own native attack key/object chain is absent.
    **{template: 3409 for template in range(30001, 30021)},
    **{template: 3403 for template in (30101, 30102, 30151, 30152, 30153)},
    **{template: 3422 for template in (30201, 30202, 30203, 30251, 30252, 30253, 30254)},
    **{template: 3423 for template in (*range(30301, 30312), 30399)},
    30401: 3412,
    30402: 3412,
    **{template: 3409 for template in range(30501, 30521)},
    30601: 3409,

    # Academic commandable summons.  Known child attacks (4217/4218/4219,
    # 4212/4214/4303, etc.) are resolved earlier.  Missing child identity is
    # still exact Heavy Duck/Alfredo family damage instead of a nearby cast.
    # Exact player ACT constructor families, independently joined to
    # template AI/ACT. Native child commands remain higher-priority.
    32002: 4207,
    32003: 4208,
    32004: 4209,
    32005: 4216,
    32008: 4015,
    32011: 4351,
    # Strengthened Tower's sole AI skill 291373 is explicitly joined to
    # player 4351 by SummonSkillTable row 10. RuneResonate also explicitly
    # replaces 4351 with 293149; its player ACT sends capsule 11050 whose
    # Hit action's type48 constructs mutated template 32029. Private AI
    # skill 293166 lives in SkillLevelTable_Rune and executes Attack_Fire.
    32026: 4351,
    32029: 4351,
    32012: 4405,
    32013: 4408,
    32017: 4405,
    # The reviewed Magic Grass fire/poison children all belong to 4405.
    # The autonomous Magma Wall resource closure has one canonical family;
    # keep its family even when the exact suffix/child attack is unknown.
    32018: 4405,
    32015: 4410,
    32022: 4410,
    33101: 2410,
    # Pact of Ajna constructs 33601; Spectral Gaze constructs 33604.
    # Heal/Buff companions 33602/33603 have no damage-family admission.
    33601: 6318,
    33604: 6313,

    # Shadow Summon creates one of eight left/right clone templates.  The
    # clone may execute copied Assassin attacks and may create a child damage
    # object, but every real Hit whose wire source is that current clone
    # generation is damage of the player's Shadow Summon family.  Keep this
    # aggregation here (after exact owner admission) rather than mapping the
    # copied attack globally: the same Projectile templates are also emitted
    # by real player casts.
    **{template: 6319 for template in range(33607, 33615)},
})

SHADOW_CLONE_ENTITY_TEMPLATES = frozenset(range(33607, 33615))

# When the owner has already reached a known second job, a shared first-job
# lineage must not make an EX entity legal for the sibling specialization.
# Unknown/base jobs are left untouched so captures that begin mid-fight can
# still benefit from exact owner+entity evidence.
ENTITY_DAMAGE_FAMILY_SECOND_JOBS = MappingProxyType({
    **{template: frozenset({"雷神"}) for template in range(30001, 30021)},
    **{
        template: frozenset({"圣徒"})
        for template in (
            *range(30251, 30255),
            *range(30301, 30312),
            30399,
            *range(30501, 30521),
            30601,
        )
    },
    **{template: frozenset({"重炮手", "机械大师"})
       for template in (32002, 32003, 32004, 32005)},
    32011: frozenset({"机械大师"}),
    32026: frozenset({"机械大师"}),
    32029: frozenset({"机械大师"}),
    **{template: frozenset({"炼金圣士", "药剂师"})
       for template in (32012, 32013, 32015, 32017, 32018, 32022)},
    **{template: frozenset({"曜", "暗"}) for template in (33601, 33604)},
    32008: frozenset({"重炮手", "机械大师"}),
})
_RELEVANT_SECOND_JOBS = frozenset({
    "圣徒", "雷神", "重炮手", "机械大师", "炼金圣士", "药剂师",
    "烈", "影", "曜", "暗",
})


def annotate_entity_damage_family(
    event,
    context,
    owner_proof,
    *,
    pve_confirmed,
    skill_catalog,
    job_conflicts,
):
    """Attach a reviewed parent summon family without inventing a cast."""
    if (
        owner_proof is None
        or pve_confirmed is not True
        or event.healing
        or event.amount <= 0
        or event.has_real_hit_evidence is not True
        or event.attribution_method == "direct_hit_overflow"
        or attribution_layer(event.attribution_method) is AttributionLayer.STATUS
        # A clone's internal copied skill/object identity is deterministic,
        # but it is deliberately a CHILD identity.  Once the current clone
        # generation and owner are proved below, presentation and scoring must
        # aggregate it under Shadow Summon.  Ordinary summon families retain
        # the old rule and never override an exact child attack.
        or (
            is_deterministic_attribution(event.attribution_method)
            and owner_proof.template_id not in SHADOW_CLONE_ENTITY_TEMPLATES
        )
    ):
        return event
    family_skill = ENTITY_DAMAGE_FAMILY_SKILLS.get(owner_proof.template_id)
    info = skill_catalog.get(family_skill) if family_skill is not None else None
    allowed_jobs = ENTITY_DAMAGE_FAMILY_SECOND_JOBS.get(owner_proof.template_id)
    if (
        info is None
        or job_conflicts(owner_proof.owner_id, family_skill)
        or (
            allowed_jobs
            and event.job in _RELEVANT_SECOND_JOBS
            and event.job not in allowed_jobs
        )
    ):
        return event
    wire_hash = sha256(context.wire).hexdigest()
    if (
        event.source_id != f"0x{owner_proof.owner_id:08X}"
        or event.wire_source_actor_id != owner_proof.entity_id
        or event.wire_entity_id != owner_proof.entity_id
        or event.entity_template_id != owner_proof.template_id
        or event.entity_owner_evidence_id != owner_proof.owner_evidence_id
        or context.generation != owner_proof.generation
        or context.order[2] != owner_proof.record_offset
        or event.wire_record_offset != owner_proof.record_offset
        or wire_hash != owner_proof.hit_wire_sha256
    ):
        return event
    token = ":".join(
        (
            "shadow-clone-family-v2"
            if owner_proof.template_id in SHADOW_CLONE_ENTITY_TEMPLATES
            else "entity-family-v1",
            str(owner_proof.template_id),
            str(family_skill),
            str(owner_proof.generation),
            owner_proof.birth_fingerprint,
            owner_proof.owner_fingerprint,
            wire_hash,
            str(context.order[2]),
        )
    )
    return replace(
        event,
        skill_id=str(family_skill),
        skill_name=info[0],
        attribution_method="packet_entity_damage_family",
        cast_instance_id="",
        # Zero means that the concrete child attack remains unknown.  The
        # evidence ID still proves the parent entity family and owner.
        entity_attack_skill_id=0,
        entity_attack_evidence_id=sha256(token.encode("ascii")).hexdigest(),
        attribution_confidence=1.0,
    )
