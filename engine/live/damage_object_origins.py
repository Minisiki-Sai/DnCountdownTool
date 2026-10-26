from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .generated_attack_instance_specs import (
    CLIENT_ACT_GENERATED_DAMAGE_OBJECT_SPECS,
    CLIENT_ACT_GENERATED_PROJECTILE_CHILD_EDGES,
)
from .skill_plate_damage import SKILL_PLATE_PROJECTILE_SKILLS
from .protocol import (
    _decode_v576_body, _v576_candidate_xor_phases,
    _KALI_11076_PARENT_V28_KEY_PREFIX,
)


_DIRECT37_MARKERS = (b"\x08\x73\x00", b"\x08\x75\x00")


def _find_next_direct37_marker(body: bytes, start: int) -> int:
    return min(
        (offset for marker in _DIRECT37_MARKERS
         for offset in (body.find(marker, start),) if offset >= 0),
        default=-1,
    )
_FIELD_RESPONSE_CODES = frozenset({"134a00", "134e00", "134700"})

# These templates previously had static-name mappings, but the full corpus
# provenance audit disproved or contradicted those families.  Keep them out
# of the deterministic object ledger until an independent packet-level link
# establishes the correct skill.
REJECTED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS = frozenset(
    {2206, 5826, 11087, 33111}
)


@dataclass(frozen=True, slots=True)
class DamageObjectTemplateSpec:
    """Static client-table origin for a network damage-object template."""

    key: str
    projectile_template_id: int
    canonical_skill_id: int
    skill_ids: frozenset[int]
    required_response_codes: frozenset[str] = frozenset()
    # Conservative registration lifetime, also used to admit child lineage.
    lifetime_seconds: float = 8.0
    # Audited duration of Hits from the exact object generation. This can be
    # longer than the conservative lifetime used to admit child lineage.
    factual_active_seconds: float | None = None
    require_exact_object: bool = False
    require_action_evidence: bool = False
    # Maximum delay from the real cast action to child-object creation.  This
    # is a cast-lineage lifetime, not a Hit attribution window: once an exact
    # object generation is registered, every later direct37 row joins by its
    # object ID until that generation's audited Hit deadline.
    action_lineage_seconds: float = 2.5
    # Exact direct-Hit identity can survive the active attack phase while the
    # client's Destroy animation keeps the object and its skill key alive.
    # This deadline never admits child births or extends their parent proof.
    identity_retirement_seconds: float | None = None

    @property
    def direct_hit_lifetime_seconds(self) -> float:
        """Deadline for an existing identity, separate from child admission."""
        return max((self.lifetime_seconds, *(value for value in (
            self.factual_active_seconds, self.identity_retirement_seconds,
        ) if value is not None)))


# Client unpack/capture bridge:
#   skill 2404 -> Skill_MicroHole -> projectile 3006 -> MicroHole.act
#   skill 2554 -> Skill_MicroHole_EX -> projectile 3029 -> MicroHole_EX.act
#   skill 2401/2556 -> projectile 3039 -> Rising Gravity family
#   skill 2405/2555 -> projectile 3040 -> Summon Black Hole family
#
# The latter two rows are additionally proved by their exact client object
# registrations in both the controlled Queen capture and a long Black Dragon
# capture: 3039 follows Rising Gravity and produces 7-8 persistent-object
# pulses, while 3040 follows Summon Black Hole and produces 13-14 pulses.
# All EX rows are normalized to the displayed/base score skill family.
_MANUAL_DAMAGE_OBJECT_SPECS = (
    # Native DarkSuffer slow/fast swords. Register each emitted sword for its
    # own lifetime; late contacts must not migrate to the next stance.
    DamageObjectTemplateSpec('dark-suffer-slow',33102,7504,frozenset({7504}),
        lifetime_seconds=10.0,action_lineage_seconds=7.0,require_exact_object=True),
    DamageObjectTemplateSpec('dark-suffer-fast',33112,7504,frozenset({7504}),
        lifetime_seconds=10.0,action_lineage_seconds=7.0,require_exact_object=True),
    # BladeRunner EX PVE Signal15(F17) -> 11087 Shoot Signal4(F40-48).
    # The template alone remains rejected: require an independent same-owner
    # 6210/6506 action and the exact object generation, never a recent label.
    DamageObjectTemplateSpec(
        "blade-runner-ex-pve-action", 11087, 6210, frozenset({6210, 6506}),
        lifetime_seconds=2.5, require_exact_object=False,
        require_action_evidence=True,
    ),
    DamageObjectTemplateSpec(
        key="dark-temple-normal",
        projectile_template_id=3006,
        canonical_skill_id=2404,
        skill_ids=frozenset({2404}),
        required_response_codes=frozenset({"134a00", "134e00"}),
        lifetime_seconds=5.2,
        factual_active_seconds=5.2,
        require_exact_object=True,
    ),
    DamageObjectTemplateSpec(
        key="dark-temple-ex",
        projectile_template_id=3029,
        canonical_skill_id=2404,
        skill_ids=frozenset({2404, 2554}),
        required_response_codes=frozenset({"134a00", "134e00"}),
        lifetime_seconds=5.2,
        factual_active_seconds=5.2,
        require_exact_object=True,
    ),
    DamageObjectTemplateSpec(
        key="rising-gravity",
        projectile_template_id=3039,
        canonical_skill_id=2401,
        skill_ids=frozenset({2401, 2556}),
        required_response_codes=frozenset({"134a00", "134700"}),
        factual_active_seconds=8.0,
        require_exact_object=True,
    ),
    DamageObjectTemplateSpec(
        key="summon-black-hole",
        projectile_template_id=3040,
        canonical_skill_id=2405,
        skill_ids=frozenset({2405, 2555}),
        required_response_codes=frozenset({"134a00", "134700"}),
        factual_active_seconds=8.0,
        require_exact_object=True,
    ),
    # Corpus-confirmed projectile bridges.  Each template was observed across
    # the capture corpus with an exact registration-object-ID -> direct37
    # join, then independently resolved through weapontable_projectile.dnt and
    # filetable.dnt.  Unlike the four overlapping field skills above, these
    # mappings are additive evidence: a missing registration must not suppress
    # the existing direct/status/entity attribution route.
    # Projectile 92 is the crossbow weapon's ordinary-shot object created by
    # Ar_Crossbow_D01.act.  It deliberately maps to the existing generic
    # normal-attack bucket rather than to any Magic Archer skill: six-capture
    # review found the same exact object generation under several mutually
    # exclusive old inferred labels, proving those labels were action-context
    # spillover rather than the projectile's identity.
    DamageObjectTemplateSpec(
        "crossbow-normal-attack", 92, 0xFFFFFFFF, frozenset({0xFFFFFFFF})
    ),
    DamageObjectTemplateSpec("flame-spark", 3004, 2217, frozenset({2217})),
    # Server object broadcasts independently confirmed by packet_skill_id or
    # an exact direct-result-group link in the five-capture hidden-truth
    # audit.  Every listed template has at least 30 anchored records and zero
    # conflicting exact skill IDs; aliases are normalized to the displayed
    # score family here rather than inferred from timing.
    DamageObjectTemplateSpec(
        "moonlight-splinter-ex-1", 1009, 208, frozenset({208})
    ),
    DamageObjectTemplateSpec(
        "moonlight-splinter-ex-2", 1010, 208, frozenset({208})
    ),
    DamageObjectTemplateSpec(
        "smash-stance-left", 1011, 353, frozenset({353})
    ),
    DamageObjectTemplateSpec(
        "smash-stance-right", 1022, 353, frozenset({353})
    ),
    DamageObjectTemplateSpec(
        "crescent-cleave-plus-ex", 1036, 209, frozenset({209})
    ),
    DamageObjectTemplateSpec(
        "crescent-cleave-pro-ex", 1037, 209, frozenset({209})
    ),
    DamageObjectTemplateSpec(
        "crescent-cleave-ex", 1038, 209, frozenset({209, 355})
    ),
    # Exact client-resource joins:
    #   projectile 1004 -> Warrior_PunishingSwing.act
    #   projectile 1039 -> Warrior_PunishingSwing_EX_PVE.act
    # The SkillLevel table names those exact ACT processors for 406/554.
    DamageObjectTemplateSpec(
        "punishing-swing", 1004, 406, frozenset({406})
    ),
    DamageObjectTemplateSpec(
        "punishing-swing-ex-pve", 1039, 406, frozenset({406, 554})
    ),
    DamageObjectTemplateSpec(
        "siege-stance-ex", 2033, 1220, frozenset({1220, 1305})
    ),
    DamageObjectTemplateSpec(
        "siege-stance-multi-ex", 2034, 1220, frozenset({1220, 1305})
    ),
    DamageObjectTemplateSpec(
        "detonating-arrow", 2025, 1353, frozenset({1353})
    ),
    DamageObjectTemplateSpec(
        "chain-lightning", 4004, 3407, frozenset({3407})
    ),
    DamageObjectTemplateSpec(
        "chain-lightning-ex-pve", 4024, 3407, frozenset({3407, 3555})
    ),
    # Full-corpus fixed packet-skill anchors plus exact client ACT names:
    #   projectile 3019 -> SoceressRollingLava_Ball.act -> 漫长熔浆 (2303)
    #   projectile 5043 -> Academic_Splash.act -> 飞溅 (4301)
    # Both mappings have >= 9,000 exact direct37 anchors, multiple files and
    # actors, and no conflicting fixed-slot skill ID.
    DamageObjectTemplateSpec(
        "rolling-lava-ball", 3019, 2303, frozenset({2303})
    ),
    DamageObjectTemplateSpec(
        "academic-splash", 5043, 4301, frozenset({4301})
    ),
    # Unique Projectile ACT -> Skill/SkillLevel processor bridges from the
    # unpacked client tables.  Generic processor labels such as ``Attack`` or
    # ``Jump`` and non-damaging Buff candidates are deliberately excluded.
    DamageObjectTemplateSpec(
        "grand-cross-projectile", 47, 3401, frozenset({3401}),
        # Two long Thunder captures end this exact object generation at
        # 4.390s / 4.446s.  Five seconds retains packet jitter without
        # allowing a recycled damage_object ID to inherit Grand Cross.
        lifetime_seconds=5.0,
        factual_active_seconds=5.0,
    ),
    DamageObjectTemplateSpec(
        "charge-volt-projectile", 63, 3019, frozenset({3019})
    ),
    DamageObjectTemplateSpec(
        "gravity-ball-plate", 403, 80005, frozenset({80005})
    ),
    DamageObjectTemplateSpec(
        "gravity-ball-plate-child", 415, 80005, frozenset({80005})
    ),
    DamageObjectTemplateSpec(
        "black-hole-laser-plate", 417, 80020, frozenset({80020})
    ),
    DamageObjectTemplateSpec(
        "great-wave", 1007, 214, frozenset({214})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-1", 1013, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-2", 1014, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-3", 1015, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-4", 1016, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-5", 1017, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "cyclone-slash-ex", 1018, 203, frozenset({203, 354})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-no-effect-1", 1028, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-no-effect-2", 1029, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-no-effect-3", 1030, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-no-effect-4", 1031, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "moon-blade-dance-no-effect-5", 1032, 351, frozenset({351})
    ),
    DamageObjectTemplateSpec(
        "arrow-shower", 2002, 1207, frozenset({1207, 1306}),
        lifetime_seconds=14.0,
        factual_active_seconds=14.0,
        action_lineage_seconds=14.0,
    ),
    DamageObjectTemplateSpec(
        "swift-shot", 2004, 1209, frozenset({1209, 1355})
    ),
    DamageObjectTemplateSpec(
        "revolutionary-ballista", 2005, 1211, frozenset({1211})
    ),
    DamageObjectTemplateSpec(
        "spirit-firing", 2014, 1416, frozenset({1416})
    ),
    DamageObjectTemplateSpec(
        "aerial-chain-shot", 2020, 1202, frozenset({1202, 1304})
    ),
    DamageObjectTemplateSpec(
        "rapid-shot-ex-shoot", 2031, 1210, frozenset({1210, 1354})
    ),
    DamageObjectTemplateSpec(
        "aerial-chain-shot-ex-combo", 2032, 1202,
        frozenset({1202, 1304}),
    ),
    DamageObjectTemplateSpec(
        "extension-arrow-ex-first", 2022, 1208,
        frozenset({1208, 1352}),
    ),
    DamageObjectTemplateSpec(
        "guided-arrow-ex", 2036, 1203, frozenset({1203, 1356})
    ),
    DamageObjectTemplateSpec(
        "swift-shot-ex", 2035, 1209, frozenset({1209, 1355})
    ),
    DamageObjectTemplateSpec(
        "nine-tail-laser", 3025, 2420, frozenset({2420, 2504})
    ),
    # projectile 3030 -> Soceress_NineTailLaser_EX.act -> the exact
    # SkillLevel processor for NineTailLaser_EX (2504, base family 2420).
    DamageObjectTemplateSpec(
        "nine-tail-laser-ex", 3030, 2420, frozenset({2420, 2504})
    ),
    DamageObjectTemplateSpec(
        "mind-breaker", 4003, 3425, frozenset({3425})
    ),
    DamageObjectTemplateSpec(
        "sacred-hammering", 4006, 3224, frozenset({3224})
    ),
    # Sacred Hammering EX emits two client object templates.  The unpacked
    # ACT/SkillLevel bridge names the same base/EX family and 1,586 fixed-slot
    # direct37 anchors from three actors agree with canonical skill 3224 with
    # zero conflicts.
    DamageObjectTemplateSpec(
        "sacred-hammering-ex", 4012, 3224, frozenset({3224, 3354})
    ),
    DamageObjectTemplateSpec(
        "sacred-hammering-ex-side", 4013, 3224,
        frozenset({3224, 3354}),
    ),
    DamageObjectTemplateSpec(
        "consecration", 4009, 3553, frozenset({3553}),
        # Exact actor + object-generation joins span up to 10.184s / 10.200s
        # in the audited captures.  Eleven seconds covers the final tick and
        # transport jitter; the old 18s lease was wider than the effect.
        lifetime_seconds=11.0,
        factual_active_seconds=11.0,
    ),
    DamageObjectTemplateSpec(
        "mind-breaker-ex", 4011, 3425, frozenset({3425, 3554})
    ),
    DamageObjectTemplateSpec(
        "electric-detonation-ex", 4018, 3408,
        frozenset({3408, 3556}),
    ),
    DamageObjectTemplateSpec(
        "magma-wave-ex-level2", 5073, 4411,
        frozenset({4411, 4506}),
    ),
    DamageObjectTemplateSpec(
        "magma-wave-ex-level3", 5074, 4411,
        frozenset({4411, 4506}),
    ),
    DamageObjectTemplateSpec(
        "piercing-star", 5701, 6003, frozenset({6003})
    ),
    DamageObjectTemplateSpec(
        "fan-of-edge", 5702, 6004, frozenset({6004})
    ),
    DamageObjectTemplateSpec(
        "raindrop-main", 5708, 6204, frozenset({6204})
    ),
    DamageObjectTemplateSpec(
        "raging-blast", 5801, 6307, frozenset({6307})
    ),
    DamageObjectTemplateSpec(
        "illusion-strike-ex-sub", 5823, 6308,
        frozenset({6308, 6705}),
    ),
    DamageObjectTemplateSpec(
        "nightfall-summon", 5824, 6701, frozenset({6701}),
        lifetime_seconds=18.0,
    ),
    DamageObjectTemplateSpec(
        "raging-blast-1", 5830, 6307, frozenset({6307})
    ),
    DamageObjectTemplateSpec(
        "raging-blast-2", 5831, 6307, frozenset({6307})
    ),
    DamageObjectTemplateSpec(
        "mixed-infection", 5058, 4418, frozenset({4418})
    ),
    DamageObjectTemplateSpec(
        "mixed-infection-fast", 11056, 4418, frozenset({4418})
    ),
    DamageObjectTemplateSpec(
        "arrow-shower-a-ball", 11084, 1207, frozenset({1207}),
        lifetime_seconds=14.0,
        factual_active_seconds=14.0,
        action_lineage_seconds=14.0,
    ),
    DamageObjectTemplateSpec(
        "plasma-burst-ex-pve", 11090, 6309,
        frozenset({6309, 6706}),
    ),
    # Shared/base skills can be emitted while the actor is labelled with any
    # compatible advanced job.  The corpus auditor therefore validates these
    # by canonical skill ID across jobs, while the client ACT resource proves
    # the projectile family independently.
    DamageObjectTemplateSpec("multi-shot-level1", 94, 1003, frozenset({1003})),
    DamageObjectTemplateSpec("fire-bird", 3001, 2207, frozenset({2207})),
    DamageObjectTemplateSpec(
        "glacial-spike-lv2", 3012, 2034, frozenset({2034})
    ),
    DamageObjectTemplateSpec(
        "gravity-ball-ex", 3018, 2403, frozenset({2403, 2552})
    ),
    DamageObjectTemplateSpec(
        "fireball-small-ex", 3026, 2220, frozenset({2220, 2302})
    ),
    DamageObjectTemplateSpec(
        "fireball-level2-ex", 3027, 2220, frozenset({2220, 2302})
    ),
    DamageObjectTemplateSpec(
        "freezing-spike-ex", 3032, 2211, frozenset({2211, 2355})
    ),
    # Unique ACT -> SkillLevel processor bridges with corpus-visible object
    # hits. Orbiter Laser covers 2,250 direct37 rows across three independent
    # Time Lord captures; Astral Illusion covers two independent Windwalker
    # captures. Neither template has another character-skill processor.
    DamageObjectTemplateSpec(
        "orbiter-laser", 3022, 2503, frozenset({2503})
    ),
    DamageObjectTemplateSpec(
        "astral-illusion", 2013, 1413, frozenset({1413})
    ),
    DamageObjectTemplateSpec(
        "cannon-tower-bomb", 5020, 48002, frozenset({48002})
    ),
    DamageObjectTemplateSpec(
        "poison-charging-ex", 5071, 4414, frozenset({4414, 4555})
    ),
    DamageObjectTemplateSpec("soul-wind-1", 5513, 5000, frozenset({5000})),
    DamageObjectTemplateSpec("soul-wind-2", 5514, 5000, frozenset({5000})),
    DamageObjectTemplateSpec(
        "specter-of-pain", 5515, 5401, frozenset({5401})
    ),
    DamageObjectTemplateSpec(
        "spirit-paper-ex", 5517, 5214, frozenset({5214, 5402})
    ),
    DamageObjectTemplateSpec(
        "stigma-of-curse", 5518, 5211, frozenset({5211})
    ),
    DamageObjectTemplateSpec("blitz-claw", 5519, 5206, frozenset({5206})),
    DamageObjectTemplateSpec(
        "phantoms-rage", 5520, 5204, frozenset({5204})
    ),
    DamageObjectTemplateSpec(
        "grudge-formation", 5508, 5218, frozenset({5218}),
        # Skill_GrudgeFormation ACT 107, type15 signal 32, payload +80:
        # 10000ms. Shoot repeats two 5% damage signals each 60-frame cycle.
        # Keep child-origin admission at 8s; the same object hits for 10s.
        factual_active_seconds=10.0,
        # Current native AB3EC0/AB6450: Destroy has 200 frames, Projectile
        # action switching fixes the rate at 60fps, and the retirement delay
        # truncates to 3333ms. Keep this separate from the 10s attack phase.
        identity_retirement_seconds=13.333,
    ),
    DamageObjectTemplateSpec("spirit-twig", 5524, 5202, frozenset({5202})),
    DamageObjectTemplateSpec(
        "sunshine-spark",
        5816,
        6603,
        frozenset({6603, 6608}),
        require_action_evidence=True,
    ),
    DamageObjectTemplateSpec(
        "sunshine-spark-destination",
        5834,
        6603,
        frozenset({6603, 6608}),
        require_action_evidence=True,
    ),
    DamageObjectTemplateSpec(
        "wild-shot-ex", 11112, 1605, frozenset({1605, 1950})
    ),
    DamageObjectTemplateSpec(
        "raging-wind-ex", 11120, 1615, frozenset({1615, 1956})
    ),
    # Alternate server lifecycle rows validated over the complete capture
    # corpus.  Transformed Silver Hunter skill IDs are normalized to the base
    # score families while remaining valid same-family action evidence.
    DamageObjectTemplateSpec(
        "tornado-shot-hit", 2214, 1703, frozenset({1703, 1715})
    ),
    DamageObjectTemplateSpec(
        "silver-missile-transformed-ex", 11118, 1601,
        frozenset({1601, 1624, 1954, 1955}),
    ),
    DamageObjectTemplateSpec(
        "arrow-storm-transformed-ex", 11122, 1608,
        frozenset({1608, 1626}),
    ),
    DamageObjectTemplateSpec(
        "arrow-storm-transformed-ex-main", 11123, 1608,
        frozenset({1608, 1626, 1952, 1953}),
    ),
    # The mappings below are not learned from the old damage assignment.
    # They are strict client-resource joins: the Projectile ACT basename is
    # also named by the corresponding character Skill/SkillLevel processor
    # (including the base/EX aliases listed here).  The runtime registration
    # then supplies the exact owner + damage_object_id consumed by direct37.
    # Keeping these in the same ledger turns formerly timing-inferred hits
    # into object-ID attribution without changing the factual hit record.
    DamageObjectTemplateSpec(
        "poison-missile", 81, 2026, frozenset({2026})
    ),
    DamageObjectTemplateSpec(
        "twin-arrow", 93, 1001, frozenset({1001})
    ),
    DamageObjectTemplateSpec(
        "pinpoint-shot-1", 106, 1204, frozenset({1204})
    ),
    DamageObjectTemplateSpec(
        "pinpoint-shot-2", 107, 1204, frozenset({1204})
    ),
    DamageObjectTemplateSpec(
        "magic-arrow", 2000, 1027, frozenset({1027})
    ),
    DamageObjectTemplateSpec(
        "physical-bullseye", 2007, 1215, frozenset({1215})
    ),
    DamageObjectTemplateSpec(
        "triangle-shot", 2009, 1217, frozenset({1217})
    ),
    DamageObjectTemplateSpec(
        "kick-and-shot", 2012, 1405, frozenset({1405, 1502})
    ),
    DamageObjectTemplateSpec(
        "ankle-shot", 2018, 1221, frozenset({1221})
    ),
    DamageObjectTemplateSpec(
        "charge-arrow-ex", 2021, 1201, frozenset({1201, 1302})
    ),
    DamageObjectTemplateSpec(
        "aerial-chain-shot-ex", 2029, 1202, frozenset({1202, 1304})
    ),
    DamageObjectTemplateSpec(
        "spiral-vortex-ex", 2039, 1406, frozenset({1406, 1506})
    ),
    DamageObjectTemplateSpec(
        "kick-and-shot-ex", 2040, 1405, frozenset({1405, 1502})
    ),
    DamageObjectTemplateSpec(
        "tornado-shot-air", 2217, 1703, frozenset({1703})
    ),
    DamageObjectTemplateSpec(
        "flame-road", 3000, 2205, frozenset({2205, 2306}),
        lifetime_seconds=12.0,
    ),
    DamageObjectTemplateSpec(
        "gravity-ball", 3005, 2403, frozenset({2403})
    ),
    DamageObjectTemplateSpec(
        "fireball-level2", 3011, 2220, frozenset({2220, 2302})
    ),
    DamageObjectTemplateSpec(
        "blizzard", 3013, 2216, frozenset({2216}),
        lifetime_seconds=12.0,
    ),
    DamageObjectTemplateSpec(
        "fireball-big-ex", 3017, 2220, frozenset({2220, 2302})
    ),
    DamageObjectTemplateSpec(
        "icicle-injection", 3023, 2353, frozenset({2353})
    ),
    DamageObjectTemplateSpec(
        "nine-tail-laser-ex-main", 3031, 2420,
        frozenset({2420, 2504}),
    ),
    DamageObjectTemplateSpec(
        "fire-wall-ex", 3036, 2202, frozenset({2202, 2305})
    ),
    DamageObjectTemplateSpec(
        "flame-road-ex", 3037, 2205, frozenset({2205, 2306}),
        lifetime_seconds=12.0,
    ),
    DamageObjectTemplateSpec(
        "divine-ascension-ex", 4015, 3218,
        frozenset({3218, 3305}),
    ),
    # Client resource join:
    #   Skill_DivinePunishment_EX -> Cleric_DivinePunishment_EX.act
    #   -> Projectile.RowId 4019 -> direct37 damage_object_id.
    # The fixed captures currently do not expose a 4019 object registration,
    # so this mapping is intentionally dormant until that full object edge is
    # present; status lifetime alone must not activate it.
    DamageObjectTemplateSpec(
        # 3205 is the base buff; 3306 is the EX skill and the only owner of
        # Projectile 4019's continuous damage.
        "divine-punishment-ex", 4019, 3306, frozenset({3306}),
    ),
    DamageObjectTemplateSpec(
        "bubble-bubble", 5002, 4008, frozenset({4008})
    ),
    DamageObjectTemplateSpec(
        "ping-pong-bomb", 5011, 4201, frozenset({4201, 4302})
    ),
    DamageObjectTemplateSpec(
        "napalm", 5017, 4222, frozenset({4222})
    ),
    DamageObjectTemplateSpec(
        "mecha-duck-ex-bomb", 5047, 4216,
        frozenset({4216, 4352}),
    ),
    DamageObjectTemplateSpec(
        "electric-saw-tower", 5052, 48001, frozenset({48001})
    ),
    DamageObjectTemplateSpec(
        "icing-mass", 5056, 4406, frozenset({4406})
    ),
    DamageObjectTemplateSpec(
        "love-virus", 5060, 4551, frozenset({4551})
    ),
    DamageObjectTemplateSpec(
        "disease-ex", 5066, 4417, frozenset({4417, 4554})
    ),
    DamageObjectTemplateSpec(
        "despair-needle", 5504, 5003, frozenset({5003})
    ),
    DamageObjectTemplateSpec(
        "spirit-of-genie", 5521, 5007, frozenset({5007})
    ),
    DamageObjectTemplateSpec(
        "graze-dance", 5612, 5103, frozenset({5103, 5502})
    ),
    DamageObjectTemplateSpec(
        "open-edge", 5706, 6201, frozenset({6201, 6403})
    ),
    DamageObjectTemplateSpec(
        "the-end", 5712, 6206, frozenset({6206})
    ),
    DamageObjectTemplateSpec(
        "rake-ex", 5713, 6203, frozenset({6203, 6405})
    ),
    DamageObjectTemplateSpec(
        "umbra", 5717, 6401, frozenset({6401, 6553}),
        # Skill_Umbra typed15 +80 is 10000ms; Assassin_Umbra Destroy
        # is 200 frames at the native 60fps rate. Existing exact identity
        # retires after 13333ms; child admission remains the original 8s.
        factual_active_seconds=10.0,
        identity_retirement_seconds=13.333,
    ),
    DamageObjectTemplateSpec(
        "crooking-deadfall", 5718, 6402, frozenset({6402})
    ),
    DamageObjectTemplateSpec(
        "the-end-2", 5719, 6206, frozenset({6206})
    ),
    DamageObjectTemplateSpec(
        "outbreak-ex", 5805, 6301, frozenset({6301, 6604})
    ),
    DamageObjectTemplateSpec(
        "illusion-strike", 5806, 6308, frozenset({6308, 6705})
    ),
    DamageObjectTemplateSpec(
        "big-mecha-bomber", 11043, 4353, frozenset({4353})
    ),
    DamageObjectTemplateSpec(
        "line-drive-ex-fast", 11061, 205, frozenset({205})
    ),
    DamageObjectTemplateSpec(
        "open-edge-ex", 11088, 6201, frozenset({6201, 6403})
    ),
    DamageObjectTemplateSpec(
        "wild-shot-ex-combo", 11113, 1605,
        frozenset({1605, 1950}),
    ),
    DamageObjectTemplateSpec(
        "arrow-storm-main-ex", 11116, 1608,
        frozenset({1608}),
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-piercing-crescent", 33101, 7503,
        frozenset({7503}),
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-doom-blade", 33103, 7505,
        frozenset({7505}),
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-death-nell", 33108, 40, frozenset({40})
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-death-nell-2", 33113, 40, frozenset({40})
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-death-nell-3", 33114, 40, frozenset({40})
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-death-nell-4", 33115, 40, frozenset({40})
    ),
    DamageObjectTemplateSpec(
        "dark-avenger-death-nell-6", 33117, 40, frozenset({40})
    ),
)

# Common NeedJob=0 skills are absent from profession-tree ACT catalogs. The
# reviewed plate catalog uses the same exact Projectile/FileTable/ACT joins;
# adding a template still requires its owner + object + generation at runtime.
_MANUAL_DAMAGE_OBJECT_SPECS += tuple(
    DamageObjectTemplateSpec(
        f"common-skill-plate-{projectile_id}", projectile_id, skill_id,
        frozenset({skill_id}),
    )
    for projectile_id, skill_id in SKILL_PLATE_PROJECTILE_SKILLS.items()
    if not any(spec.projectile_template_id == projectile_id
               for spec in _MANUAL_DAMAGE_OBJECT_SPECS)
)

# Complete deterministic client-ACT supplement.  These rows are generated
# from exact SkillLevel action keys and typed ACT signal-15/Projectile-child
# edges.  They do not make a hit by themselves: runtime attribution still
# requires the exact owner + full damage_object_id + object generation join.
# Manual rows above retain audited lifetimes and special action requirements.
_MANUAL_PROJECTILE_TEMPLATE_IDS = frozenset(
    spec.projectile_template_id for spec in _MANUAL_DAMAGE_OBJECT_SPECS
)
_GENERATED_SPEC_BY_TEMPLATE = {
    template_id: (canonical_id, skill_ids)
    for template_id, canonical_id, skill_ids
    in CLIENT_ACT_GENERATED_DAMAGE_OBJECT_SPECS
}
_DAMAGE_OBJECT_SPECS = tuple(
    replace(spec, skill_ids=spec.skill_ids | generated[1])
    if (generated := _GENERATED_SPEC_BY_TEMPLATE.get(spec.projectile_template_id))
    and generated[0] == spec.canonical_skill_id else spec
    for spec in _MANUAL_DAMAGE_OBJECT_SPECS
) + tuple(
    DamageObjectTemplateSpec(
        key=f"client-act-generated-{projectile_template_id}",
        projectile_template_id=projectile_template_id,
        canonical_skill_id=canonical_skill_id,
        skill_ids=skill_ids,
    )
    for projectile_template_id, canonical_skill_id, skill_ids
    in CLIENT_ACT_GENERATED_DAMAGE_OBJECT_SPECS
    if projectile_template_id not in _MANUAL_PROJECTILE_TEMPLATE_IDS
    and projectile_template_id
    not in REJECTED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
)

_SPEC_BY_PROJECTILE_TEMPLATE = {
    spec.projectile_template_id: spec for spec in _DAMAGE_OBJECT_SPECS
}

_PARENT_TEMPLATES_BY_CHILD: dict[int, frozenset[int]] = {}
_mutable_parent_templates_by_child: dict[int, set[int]] = {}
for _parent_template_id, _child_template_ids in (
    CLIENT_ACT_GENERATED_PROJECTILE_CHILD_EDGES
):
    for _child_template_id in _child_template_ids:
        _mutable_parent_templates_by_child.setdefault(
            _child_template_id, set()
        ).add(_parent_template_id)
_PARENT_TEMPLATES_BY_CHILD = {
    child_template_id: frozenset(parent_template_ids)
    for child_template_id, parent_template_ids
    in _mutable_parent_templates_by_child.items()
}
del _mutable_parent_templates_by_child

# These child Projectile templates have demonstrated packet-skill conflicts
# when treated as global template -> skill mappings.  They become reliable
# only when an independently typed own action proves their family or an
# exact live parent Projectile generation supplies the family.
PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS = frozenset(
    {2023, 3021, 5520, 5709, 5711, 5737}
)

# Native EC168 creates an independent Shocktransition Skill_ShockInfection
# component whose ACT281/signal3 spawns Projectile4004. Ordinary ChainLightning
# ACT159/signals8..10 uses the same row. Config, recent skill actions, and a
# previously validated numeric template therefore cannot disambiguate it.
# Complete player-root ACT coordinates are handled by the separate root ledger.
FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS = frozenset({4004})

# Legacy DISPLAY alternatives from fixed-skill context + recent action votes.
# These are NOT typed skill -> Projectile edges (e.g. FireBird -> 3001, not
# 3021). Keep their display fallback, but neither these votes nor an actual
# nearby cast prove the child belongs to that cast. They cannot qualify a
# deterministic origin/root or defeat later full-ID typed-parent evidence.
_SHARED_CHILD_DIRECT_SKILL_IDS: dict[int, frozenset[int]] = {
    2023: frozenset({1353}),
    3021: frozenset({2205, 2207}),
    5520: frozenset({5204}),
    5709: frozenset({6212}),
    5711: frozenset({6212}),
    5737: frozenset({6212}),
}
_SHARED_CHILD_DIRECT_ACTION_SKILL_IDS = frozenset().union(
    *_SHARED_CHILD_DIRECT_SKILL_IDS.values()
)

# Fully decoded, same-owner, same-generation parent -> child Projectile
# chains whose concrete pair identifies the player skill even when the
# capture starts after the root action packet.  Keep this table explicit:
# nearby actions, a child template by itself, and packet skill context are
# not substitutes for the full parent object ID.
EXACT_PARENT_CHILD_SKILL_CHAINS = MappingProxyType({
    (3023, 3021): 2353,  # 冰柱喷射 root -> damage child
})

# Full-corpus packet validation gate (205 captures, fixed direct37 skill
# slots only). Every template in this set has at least 30 exact anchors and
# zero conflicting skill IDs. Other configured templates may still provide
# the best available display bucket, but remain explicitly provisional until
# they meet the same independent packet-evidence threshold.
PACKET_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS = frozenset(
    {
        1009,
        1010,
        1011,
        1022,
        1036,
        1037,
        1038,
        2025,
        2033,
        2034,
        3000,
        3001,
        3019,
        3017,
        3026,
        3027,
        3037,
        4012,
        4013,
        4024,
        5011,
        5043,
        11043,
        11061,
        11112,
        11120,
    }
)

# Deterministic client action bridge generated from the V576 resources:
#
#   SkillTable.RowId
#     -> SkillLevel._SkillIndex
#     -> exact SkillLevel._ProcessParam action key
#     -> typed ACT signal 15 payload[0]
#     -> weapontable_projectile.RowId
#
# Only templates already configured in ``_DAMAGE_OBJECT_SPECS`` whose ACT
# candidates collapse to the same canonical skill family are admitted.  This
# is deliberately separate from the independent fixed-slot packet anchors
# above.  Runtime attribution still requires the full owner/object/generation
# join, so a static table row alone can never manufacture a damage event.
CLIENT_ACT_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS = frozenset(
    {
        # Common skill plates have NeedJob=0, not a profession-tree entry.
        # Projectile/FileTable ACT proof and native 80020 anchors distinguish
        # them from the Force User gravity ball's 3005/3018 objects.
        403,
        415,
        417,
        92,
        47,
        63,
        81,
        93,
        94,
        106,
        107,
        1004,
        1009,
        1010,
        1013,
        1014,
        1015,
        1016,
        1017,
        1018,
        1028,
        1029,
        1030,
        1031,
        1032,
        1036,
        1037,
        1039,
        2000,
        2002,
        2004,
        2005,
        2007,
        2009,
        2012,
        2013,
        2014,
        2018,
        2020,
        2021,
        2022,
        2025,
        2029,
        2031,
        2036,
        2040,
        3000,
        3001,
        3005,
        3006,
        3012,
        3013,
        3018,
        3019,
        3022,
        3023,
        3025,
        3029,
        3030,
        3031,
        3032,
        3037,
        3039,
        # These two were previously held provisional because the catalog
        # mixed NPC/Bot ACTs and cross-skill generic actions into player
        # candidates. The scoped player-only catalog proves their typed chain.
        3040,
        4003,
        4009,
        4011,
        4018,
        4019,
        4024,
        5002,
        5011,
        5017,
        5043,
        5047,
        5056,
        5058,
        5060,
        5066,
        5073,
        5074,
        5504,
        5508,
        5513,
        5514,
        5515,
        5517,
        5518,
        5519,
        5521,
        5612,
        5701,
        5702,
        5706,
        5708,
        5712,
        5713,
        5717,
        5718,
        5719,
        5806,
        5816,
        5823,
        5824,
        5834,
        11088,
        33101,
    }
) | frozenset(
    projectile_template_id
    for projectile_template_id, _canonical_skill_id, _skill_ids
    in CLIENT_ACT_GENERATED_DAMAGE_OBJECT_SPECS
) | frozenset(SKILL_PLATE_PROJECTILE_SKILLS)

# Generated ACT name bridges also contain shared rows.  Those rows require the
# full player action/signal coordinates before a concrete generation is named.
CLIENT_ACT_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS -= (
    FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS
)

# Runtime-action-qualified semantic bridges.  The Projectile row -> ACT file
# edge is numeric, while the ACT/SkillLevel resource-name edge alone is not a
# foreign key.  These high-volume templates therefore become deterministic
# only for the concrete object generation that is also backed by a same-owner
# action from the configured canonical skill family.  A registration without
# that action remains provisional.  Full-corpus validation (205 captures)
# measured 99.86%+ action backing for every row admitted here.
ACTION_QUALIFIED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS = frozenset(
    {
        11087,  # EX wave: independently typed same-owner action is mandatory.
        3004,  # 火焰闪耀
        2032,  # 飞空连射 EX combo
        2035,  # 迅捷箭 EX
        2039,  # 螺旋风暴 EX
        3036,  # 火墙 EX
        4015,  # 神圣断言/神圣断言 EX; 神圣惩戒 EX is Projectile 4019.
        5020,  # 机械炮塔炮弹
        5052,  # 机械链锯
        5071,  # 毒气聚能 EX
        5524,  # 灵魂之枝
        5805,  # 爆发 EX
        11056,  # 混合感染快速体
        11084,  # 箭雨风暴：引导期分批创建的子箭对象
        11090,  # 血浆迸射 EX PVE
        11113,  # 狂野射击 EX combo
        11116,  # 箭矢风暴主段 EX
        33103,  # 刀锋之灭
    }
)

# Public protocol filter used by the stateful decoder.  Server object
# broadcasts expose the Projectile RowId directly, but production must only
# turn a broadcast into a skill owner when that RowId has an audited static
# mapping below.  Keeping this list derived from the specs prevents the wire
# decoder and attribution ledger from drifting apart.
DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS = frozenset(
    _SPEC_BY_PROJECTILE_TEMPLATE
)

# Every skill for which a client projectile template can provide deterministic
# attribution when its registration is available.
DAMAGE_OBJECT_MAPPED_SKILL_IDS = frozenset(
    skill_id
    for spec in _DAMAGE_OBJECT_SPECS
    for skill_id in spec.skill_ids
)

# Compatibility name used by the decoders to suppress unsafe fallback
# attribution.  Keep this intentionally narrower than the mapped set: only
# overlapping persistent-field skills require the exact object-ID join.
DAMAGE_OBJECT_SKILL_IDS = frozenset(
    skill_id
    for spec in _DAMAGE_OBJECT_SPECS
    if spec.require_exact_object
    for skill_id in spec.skill_ids
)


def damage_object_active_seconds(skill_id: object) -> float | None:
    """Return the factual registered-object lifetime for one skill family."""

    try:
        normalized = int(str(skill_id or "").strip(), 0)
    except (TypeError, ValueError):
        return None
    lifetimes = [
        spec.factual_active_seconds
        for spec in _DAMAGE_OBJECT_SPECS
        if spec.factual_active_seconds is not None
        and (
            normalized == spec.canonical_skill_id
            or normalized in spec.skill_ids
        )
    ]
    return max(lifetimes) if lifetimes else None


@dataclass(frozen=True, slots=True)
class DamageObjectActionEvidence:
    owner_actor_id: int
    canonical_skill_id: int
    raw_skill_id: int
    observed_at: float
    action_epoch_id: int = 0
    natural_end_at: float = 0.0


@dataclass(slots=True)
class DamageObjectResponseEvidence:
    owner_actor_id: int
    canonical_skill_id: int
    raw_skill_id: int
    observed_at: float
    components: dict[str, str] = field(default_factory=dict)
    effect_instance_ids: dict[str, int] = field(default_factory=dict)
    skill_level_row_ids: set[int] = field(default_factory=set)
    effect_class_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Kali11076ParentWitness:
    parent_generation: int
    parent_registered_at: float
    parent_expires_at: float
    parent_packet_serial: int
    parent_record_offset: int
    root_epoch: int
    root_observed_at: float
    root_raw_skill_id: int


@dataclass(slots=True)
class DamageObjectOrigin:
    owner_actor_id: int
    damage_object_id: int
    projectile_template_id: int
    effect_resource_id: int
    child_sequence: int
    canonical_skill_id: int
    generation: int
    registered_at: float
    expires_at: float
    action_backed: bool
    response_backed: bool
    # Concrete cast generation that created this object.  Keeping the epoch
    # prevents a later cast of the same skill from retroactively claiming an
    # older object generation.  Zero means the registration has not been
    # bound to a reliable action.
    action_epoch_id: int = 0
    lifecycle_backed: bool = False
    packet_validated_subtype: bool = False
    action_qualified_subtype: bool = False
    parent_qualified_subtype: bool = False
    parent_backed: bool = False
    parent_projectile_template_id: int = 0
    parent_generation: int = 0
    parent_damage_object_id: int = 0
    parent_lineage_exact: bool = False
    # Frozen when a full-ID parent edge is established. A child's own later
    # action_epoch_id is not evidence for the action that created its root.
    parent_root_action_epoch_id: int = 0
    # Newly admitted FFFF parent tails need an independently proved root.
    # Sticky within this generation, including prefix/state observations.
    requires_root_action_proof: bool = False
    parent_proof_dependencies: frozenset[tuple[int, int, int]] = frozenset()
    registration_key: str = ""
    registered_packet_serial: int = 0
    registered_record_offset: int = 0
    hit_count: int = 0
    last_hit_at: float | None = None
    # Only the exact v28 152-byte admission sets this immutable witness.
    kali_11076_parent_witness: _Kali11076ParentWitness | None = None


@dataclass(frozen=True, slots=True)
class DamageObjectAttribution:
    owner_actor_id: int
    damage_object_id: int
    projectile_template_id: int
    generation: int
    skill_id: int
    confidence: float
    action_backed: bool
    response_backed: bool
    packet_validated: bool
    client_act_validated: bool
    action_qualified_validated: bool = False
    parent_lineage_validated: bool = False
    parent_projectile_template_id: int = 0
    packet_validated_subtype: bool = False
    parent_action_lineage_validated: bool = False
    parent_root_action_epoch_id: int = 0
    requires_root_action_proof: bool = False

    @property
    def exact_parent_child_skill_validated(self) -> bool:
        return bool(
            self.parent_lineage_validated
            and EXACT_PARENT_CHILD_SKILL_CHAINS.get((
                self.parent_projectile_template_id,
                self.projectile_template_id,
            )) == self.skill_id
        )

    def packet_skill_is_shared_child_context_only(self, skill_id: int) -> bool:
        """Return whether a fixed skill is only concurrent packet context.

        Shared child Projectile rows can carry the currently executing skill
        even while damage from an older object is being reported.  Without a
        complete parent chain, 2205/2207 on template 3021 is therefore useful
        provenance but not deterministic final skill identity.
        """
        return bool(
            not self.deterministically_validated
            and skill_id in _SHARED_CHILD_DIRECT_SKILL_IDS.get(
                self.projectile_template_id, frozenset()
            )
        )

    @property
    def deterministically_validated(self) -> bool:
        if self.projectile_template_id in FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS:
            return False
        if self.requires_root_action_proof and not (
            self.parent_action_lineage_validated and self.parent_root_action_epoch_id > 0
        ):
            return False
        return (
            self.packet_validated
            or self.client_act_validated
            or self.action_qualified_validated
            or self.parent_lineage_validated
            or self.packet_validated_subtype
        )


@dataclass(slots=True)
class _PacketOriginSnapshot:
    """One superseded generation, usable only before its same-packet boundary."""

    origin: DamageObjectOrigin
    superseded_at: float
    superseded_record_offset: int
    invalidation: tuple[float, int, int] | None = None


def direct37_damage_object_id(
    payload: bytes,
    direction: str | None,
    *,
    source_actor_id: int,
    damage: int,
    record_offset: int = 0,
) -> int | None:
    """Return the persistent damage object referenced by one direct37 row.

    This is the 24-bit ID beginning at marker ``+13``. It is distinct from
    the short-lived result-group ID at ``+4`` and is repeated by every pulse
    emitted by the same projectile/field object.
    """

    if direction not in (None, "server_to_client"):
        return None
    for xor_phase in _v576_candidate_xor_phases(payload):
        decoded = _decode_v576_body(payload, xor_phase)
        if record_offset >= 9:
            if (
                decoded[record_offset : record_offset + 3]
                not in _DIRECT37_MARKERS
            ):
                continue
            marker_offset = record_offset
        else:
            marker_offset = _find_next_direct37_marker(decoded, 9)
        while marker_offset >= 0:
            if marker_offset + 27 <= len(decoded):
                row_source = int.from_bytes(
                    decoded[marker_offset + 8 : marker_offset + 12],
                    "little",
                )
                row_damage = int.from_bytes(
                    decoded[marker_offset + 19 : marker_offset + 27],
                    "little",
                )
                if row_source == source_actor_id and row_damage == damage:
                    damage_object_id = int.from_bytes(
                        decoded[marker_offset + 13 : marker_offset + 17],
                        "little",
                    )
                    if 0 < damage_object_id <= 0x00FFFFFF:
                        return damage_object_id
            if record_offset >= 9:
                break
            marker_offset = _find_next_direct37_marker(
                decoded, marker_offset + 1
            )
    return None


class DamageObjectOriginLedger:
    """Resolve skill-ID-free direct37 rows through registered client objects.

    The ledger is intentionally independent from damage magnitude. Its hard
    evidence is the exact object ID shared by the client object-registration
    message and the server direct37 result, plus the projectile template from
    the unpacked client tables. Server 13 4A/4E responses are retained as
    corroborating skill-level/effect evidence, not treated as a damage
    formula fingerprint.
    """

    _RESPONSE_ATTACH_SECONDS = 2.5
    _RETENTION_SECONDS = 16.0
    _PARENT_STATE_IDENTITY_LIMIT = 4096
    _PARENT_STATE_TOMBSTONE_SECONDS = 60.0

    def __init__(self) -> None:
        self.evidence_epoch = 0
        self._origins: dict[tuple[int, int], DamageObjectOrigin] = {}
        self._generation_counters: dict[tuple[int, int], int] = {}
        self._unknown_registration_invalidations: dict[
            tuple[int, int], tuple[float, int, int]
        ] = {}
        self._registration_replicas: dict[tuple[int, int, str], float] = {}
        # Bounded late-echo protection, not a permanent object-ID archive.
        # Values: template, full parent, generation, expiry + retention window.
        self._last_parent_state_identities: dict[tuple[int, int], tuple[int, int, int, float]] = {}
        self._parent_state_history_gap_until = 0.0
        self._revoked_origin_generations: dict[tuple[int, int, int], tuple[float, int, int]] = {}
        self._actions: list[DamageObjectActionEvidence] = []
        # Do not let the legacy 120ms canonical-action dedup hide a second
        # raw root for v28. Exact repeated observations are idempotent.
        self._kali_raw_roots: set[tuple[int, int, float]] = set()
        self._action_epoch_counter = 0
        # Scalar capture-time high-water mark. A recovered action keeps its
        # original timestamp, which must not roll back lifecycle checks.
        self._latest_observed_timestamp = 0.0
        self._responses: list[DamageObjectResponseEvidence] = []
        self._history_packet_serial = 0
        self._packet_origin_history: dict[
            tuple[int, int], list[_PacketOriginSnapshot]
        ] = {}

    def reset(self) -> None:
        self.evidence_epoch += 1
        self._origins.clear()
        self._generation_counters.clear()
        self._unknown_registration_invalidations.clear()
        self._registration_replicas.clear()
        self._last_parent_state_identities.clear()
        self._parent_state_history_gap_until = 0.0
        self._revoked_origin_generations.clear()
        self._actions.clear()
        self._kali_raw_roots.clear()
        self._action_epoch_counter = 0
        self._latest_observed_timestamp = 0.0
        self._responses.clear()
        self._history_packet_serial = 0
        self._packet_origin_history.clear()

    def _advance_packet_history(self, packet_serial: int) -> None:
        # A decoder preprocesses registrations before emitting the Hits in
        # that packet. Retain only that packet's displaced generations, never
        # a cross-packet fallback that could resurrect a reused object ID.
        if packet_serial > self._history_packet_serial:
            self._history_packet_serial = packet_serial
            self._packet_origin_history.clear()

    @staticmethod
    def _boundary_reached(
        boundary: tuple[float, int, int], timestamp: float,
        packet_serial: int, record_offset: int,
    ) -> bool:
        at, serial, offset = boundary
        if packet_serial > 0 and serial > 0:
            return packet_serial > serial or (
                packet_serial == serial and (
                    record_offset <= 0 or offset <= 0 or record_offset >= offset
                )
            )
        return timestamp >= at

    @classmethod
    def _first_boundary(
        cls, previous: tuple[float, int, int] | None,
        current: tuple[float, int, int],
    ) -> tuple[float, int, int]:
        # Repeated invalidations must not move an earlier tombstone past a
        # Hit which it already invalidated.
        return previous if previous is not None and cls._boundary_reached(
            previous, *current
        ) else current

    @staticmethod
    def _registered_by(
        origin: DamageObjectOrigin, timestamp: float,
        packet_serial: int, record_offset: int,
    ) -> bool:
        if timestamp < origin.registered_at:
            return False
        if packet_serial > 0 and origin.registered_packet_serial > 0:
            if packet_serial < origin.registered_packet_serial:
                return False
            if packet_serial == origin.registered_packet_serial:
                if origin.registered_record_offset > 0 and (
                    record_offset <= 0 or record_offset < origin.registered_record_offset
                ):
                    return False
        return True

    def _origin_at(
        self, key: tuple[int, int], timestamp: float,
        packet_serial: int, record_offset: int,
    ) -> tuple[DamageObjectOrigin | None, _PacketOriginSnapshot | None]:
        if packet_serial > 0 and packet_serial == self._history_packet_serial:
            history = self._packet_origin_history.get(key, ())
            if history and record_offset <= 0:
                # No byte position means no safe choice between generations.
                return None, None
            for snapshot in sorted(history, key=lambda row: row.superseded_record_offset):
                if record_offset < snapshot.superseded_record_offset:
                    origin = snapshot.origin
                    return (origin, snapshot) if self._registered_by(
                        origin, timestamp, packet_serial, record_offset
                    ) else (None, None)
        origin = self._origins.get(key)
        return (origin, None) if origin is not None and self._registered_by(
            origin, timestamp, packet_serial, record_offset
        ) else (None, None)

    def _snapshot_before_replacement(
        self, origin: DamageObjectOrigin, timestamp: float,
        packet_serial: int, record_offset: int,
        invalidation: tuple[float, int, int] | None,
    ) -> None:
        if packet_serial <= 0 or record_offset <= 0:
            return
        self._packet_origin_history.setdefault(
            (origin.owner_actor_id, origin.damage_object_id), []
        ).append(_PacketOriginSnapshot(
            replace(origin), timestamp, record_offset, invalidation,
        ))

    def _remember_parent_state_identity(self, origin: DamageObjectOrigin) -> None:
        key = (origin.owner_actor_id, origin.damage_object_id)
        self._last_parent_state_identities.pop(key, None)
        while len(self._last_parent_state_identities) >= self._PARENT_STATE_IDENTITY_LIMIT:
            oldest_key = next(iter(self._last_parent_state_identities))
            dropped = self._last_parent_state_identities.pop(oldest_key)
            # Losing an unexpired tombstone must reduce evidence, not permit
            # a replay to be mistaken for a first creation. One scalar bounds
            # this conservative gap; no second unbounded eviction archive.
            self._parent_state_history_gap_until = max(
                self._parent_state_history_gap_until, dropped[3],
            )
        self._last_parent_state_identities[key] = (
            origin.projectile_template_id, origin.parent_damage_object_id,
            origin.generation, origin.expires_at + self._PARENT_STATE_TOMBSTONE_SECONDS,
        )

    def _prune(self, timestamp: float) -> None:
        self._latest_observed_timestamp = max(self._latest_observed_timestamp, timestamp)
        cutoff = timestamp - self._RETENTION_SECONDS
        self._last_parent_state_identities = {
            key: identity for key, identity in self._last_parent_state_identities.items()
            if identity[3] >= timestamp
        }
        if timestamp > self._parent_state_history_gap_until:
            self._parent_state_history_gap_until = 0.0
        self._registration_replicas = {
            key: first_at for key, first_at in self._registration_replicas.items()
            if abs(timestamp - first_at) <= 0.25
        }
        self._origins = {
            key: origin
            for key, origin in self._origins.items()
            if max(origin.expires_at, origin.last_hit_at or origin.registered_at)
            >= cutoff
        }
        self._unknown_registration_invalidations = {
            key: row
            for key, row in self._unknown_registration_invalidations.items()
            if row[0] >= cutoff
        }
        if self._revoked_origin_generations:
            retained = {dependency for origin in self._origins.values()
                        for dependency in origin.parent_proof_dependencies}
            retained.update((origin.owner_actor_id, origin.damage_object_id, origin.generation)
                            for origin in self._origins.values())
            self._revoked_origin_generations = {
                key: value for key, value in self._revoked_origin_generations.items()
                if key in retained or value[0] >= cutoff
            }
        self._responses = [
            response
            for response in self._responses
            if response.observed_at >= cutoff
        ]
        self._actions = [
            action for action in self._actions if action.observed_at >= cutoff
        ]
        self._kali_raw_roots = {row for row in self._kali_raw_roots if row[2] >= cutoff}
        self._packet_origin_history = {
            key: retained for key, rows in self._packet_origin_history.items()
            if (retained := [row for row in rows if row.superseded_at >= cutoff])
        }

    def observe_action(
        self,
        source_actor_id: int,
        skill_id: int,
        timestamp: float,
    ) -> None:
        """Retain action evidence without creating a synthetic field instance."""

        if source_actor_id <= 0 or skill_id <= 0:
            return
        specs = [
            spec
            for spec in _SPEC_BY_PROJECTILE_TEMPLATE.values()
            if skill_id in spec.skill_ids
        ]
        if specs:
            canonical_skill_id = specs[0].canonical_skill_id
            lineage_seconds = max(
                spec.action_lineage_seconds for spec in specs
            )
        elif skill_id in _SHARED_CHILD_DIRECT_ACTION_SKILL_IDS:
            # Some direct parent alternatives do not own a unique Projectile
            # template of their own.  Retain only their short action epoch so
            # a later shared child registration can select that explicit
            # family; the action does not create an object by itself.
            canonical_skill_id = skill_id
            lineage_seconds = 2.5
        else:
            return
        self._prune(timestamp)
        if skill_id in (5217, 5406):
            self._kali_raw_roots.add((source_actor_id, skill_id, timestamp))
        duplicate = next(
            (
                previous
                for previous in reversed(self._actions)
                if previous.owner_actor_id == source_actor_id
                and previous.canonical_skill_id == canonical_skill_id
                and abs(previous.observed_at - timestamp) <= 0.12
            ),
            None,
        )
        if duplicate is not None:
            action = duplicate
        else:
            self._action_epoch_counter += 1
            action = DamageObjectActionEvidence(
                owner_actor_id=source_actor_id,
                canonical_skill_id=canonical_skill_id,
                raw_skill_id=skill_id,
                observed_at=timestamp,
                action_epoch_id=self._action_epoch_counter,
                natural_end_at=timestamp + lineage_seconds,
            )
            self._actions.append(action)
        # A real action can be emitted after its registrations while waiting
        # for an exact UDP mirror. Only an action whose ORIGINAL timestamp
        # precedes a current root can recover that root's proof. Child-local
        # action diagnostics remain separate and never seed root propagation.
        recovered_roots: list[DamageObjectOrigin] = []
        observed_now = self._latest_observed_timestamp
        for origin in self._origins.values():
            if (
                origin.owner_actor_id == source_actor_id
                and origin.canonical_skill_id == canonical_skill_id
                and origin.action_epoch_id == 0
                and abs(timestamp - origin.registered_at) <= 0.12
                and self._current_origin_accepts_root_recovery(origin, observed_now)
            ):
                root_candidate = (
                    not origin.parent_backed and not origin.parent_damage_object_id
                    and not origin.parent_qualified_subtype
                    and origin.projectile_template_id not in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
                )
                if root_candidate and not (
                    0.0 <= origin.registered_at - action.observed_at <= 0.12
                ):
                    continue
                origin.action_backed = True
                origin.action_epoch_id = action.action_epoch_id
                # Preserve the existing own-action diagnostic (including
                # destination-owned projectiles). Only descendant backfill
                # needs the stricter 120ms recovery-arrival window.
                if (root_candidate and 0.0 <= observed_now - action.observed_at <= 0.12
                        and self._has_independent_action(origin)
                        and self._origin_has_deterministic_lineage(origin)):
                    recovered_roots.append(origin)
        if recovered_roots:
            self._recover_current_descendant_roots(recovered_roots, action, observed_now)

    def _current_origin_accepts_root_recovery(
        self, origin: DamageObjectOrigin, timestamp: float,
    ) -> bool:
        key = (origin.owner_actor_id, origin.damage_object_id)
        return (
            self._origins.get(key) is origin
            and origin.registered_at <= timestamp <= origin.expires_at
            and key not in self._unknown_registration_invalidations
            and (*key, origin.generation) not in self._revoked_origin_generations
            and not self._parent_proof_revoked(origin)
        )

    def _recover_current_descendant_roots(
        self, roots: list[DamageObjectOrigin], action: DamageObjectActionEvidence,
        timestamp: float,
    ) -> None:
        # This index is transient and bounded by currently retained origins;
        # no additional per-object history survives the call. Never touch
        # packet-history snapshots or generations superseded by current IDs.
        children: dict[tuple[int, int, int], list[DamageObjectOrigin]] = {}
        for child in self._origins.values():
            if (
                child.owner_actor_id == action.owner_actor_id
                and child.canonical_skill_id == action.canonical_skill_id
                and child.parent_lineage_exact and child.parent_backed
                and child.parent_damage_object_id > 0 and child.parent_generation > 0
                and child.parent_root_action_epoch_id == 0
                and action.observed_at <= child.registered_at
                and self._current_origin_accepts_root_recovery(child, timestamp)
            ):
                children.setdefault((child.owner_actor_id, child.parent_damage_object_id,
                                     child.parent_generation), []).append(child)
        pending = list(roots)
        for parent in pending:
            parent_key = (parent.owner_actor_id, parent.damage_object_id, parent.generation)
            for child in children.get(parent_key, ()):
                if (
                    child.parent_root_action_epoch_id
                    or child.parent_projectile_template_id != parent.projectile_template_id
                    or parent.projectile_template_id not in _PARENT_TEMPLATES_BY_CHILD.get(
                        child.projectile_template_id, frozenset(),
                    )
                    or parent_key not in child.parent_proof_dependencies
                    or not parent.parent_proof_dependencies.issubset(child.parent_proof_dependencies)
                    or not self._registered_by(parent, child.registered_at,
                                               child.registered_packet_serial, child.registered_record_offset)
                ):
                    continue
                child.parent_root_action_epoch_id = action.action_epoch_id
                if child.action_epoch_id == 0:
                    child.action_epoch_id = action.action_epoch_id
                # No birth/expiry/generation, dependency or registration-byte
                # boundary changes. A later local child epoch cannot replace
                # this independently recovered root on this or deeper edges.
                pending.append(child)

    def observe_response(
        self,
        *,
        source_actor_id: int,
        skill_id: int,
        effect_code: str,
        parameters: str,
        timestamp: float,
        effect_instance_id: int | None = None,
        skill_level_row_id: int | None = None,
        effect_class_id: int | None = None,
    ) -> None:
        normalized_code = str(effect_code or "").strip().casefold()
        if (
            source_actor_id <= 0
            or skill_id <= 0
            or normalized_code not in _FIELD_RESPONSE_CODES
        ):
            return
        specs = [
            spec
            for spec in _SPEC_BY_PROJECTILE_TEMPLATE.values()
            if skill_id in spec.skill_ids
        ]
        if not specs:
            return
        canonical_skill_id = specs[0].canonical_skill_id
        self._prune(timestamp)
        candidates = [
            response
            for response in self._responses
            if response.owner_actor_id == source_actor_id
            and response.canonical_skill_id == canonical_skill_id
            and abs(timestamp - response.observed_at) <= 0.25
        ]
        if candidates:
            response = max(candidates, key=lambda row: row.observed_at)
        else:
            response = DamageObjectResponseEvidence(
                owner_actor_id=source_actor_id,
                canonical_skill_id=canonical_skill_id,
                raw_skill_id=skill_id,
                observed_at=timestamp,
            )
            self._responses.append(response)
        response.components[normalized_code] = str(parameters or "")
        if effect_instance_id is not None and effect_instance_id > 0:
            response.effect_instance_ids[normalized_code] = effect_instance_id
        if skill_level_row_id is not None and skill_level_row_id > 0:
            response.skill_level_row_ids.add(skill_level_row_id)
        if effect_class_id is not None and effect_class_id > 0:
            response.effect_class_ids.add(effect_class_id)
        if any(
            spec.required_response_codes.issubset(response.components)
            for spec in specs
        ):
            # Registration is decoded before status responses when both are
            # bundled in one frame. Preserve the exact object mapping and only
            # upgrade its corroborating evidence; never create a field owner.
            for origin in self._origins.values():
                if (
                    origin.owner_actor_id == source_actor_id
                    and origin.canonical_skill_id == canonical_skill_id
                    and abs(timestamp - origin.registered_at)
                    <= self._RESPONSE_ATTACH_SECONDS
                ):
                    origin.response_backed = True

    def _origin_has_deterministic_lineage(
        self, origin: DamageObjectOrigin,
        timestamp: float | None = None, packet_serial: int = 0, record_offset: int = 0,
    ) -> bool:
        if origin.projectile_template_id in FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS:
            return False
        if origin.kali_11076_parent_witness is not None and not self._kali_11076_parent_current(
            origin, self._latest_observed_timestamp if timestamp is None else timestamp,
            packet_serial, record_offset,
        ):
            return False
        if origin.requires_root_action_proof and not (
            origin.parent_lineage_exact and origin.parent_root_action_epoch_id > 0
            and not self._parent_proof_revoked(origin, timestamp, packet_serial, record_offset)
        ):
            return False
        template_id = origin.projectile_template_id
        return (
            (origin.parent_lineage_exact and not self._parent_proof_revoked(
                origin, timestamp, packet_serial, record_offset
            ))
            or origin.packet_validated_subtype
            or template_id
            in PACKET_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
            or (
                template_id
                in CLIENT_ACT_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
                and template_id
                not in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
                and not origin.action_qualified_subtype
                and not origin.parent_qualified_subtype
            )
            or (
                self._has_independent_action(origin)
                and (
                    origin.action_qualified_subtype
                    or template_id
                    in ACTION_QUALIFIED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
                    or template_id
                    in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
                )
            )
        )

    @staticmethod
    def _has_independent_action(origin: DamageObjectOrigin) -> bool:
        return (
            origin.action_backed and not origin.parent_backed
            and origin.canonical_skill_id not in _SHARED_CHILD_DIRECT_SKILL_IDS.get(
                origin.projectile_template_id, frozenset(),
            )
        )

    @staticmethod
    def _parent_root_action_epoch(parent: DamageObjectOrigin | None) -> int:
        if parent is None:
            return 0
        if parent.parent_backed:
            # Every intermediate edge must already carry an exact inherited
            # root proof. A local action on an intermediate child is no root.
            return parent.parent_root_action_epoch_id if parent.parent_lineage_exact else 0
        return parent.action_epoch_id if DamageObjectOriginLedger._has_independent_action(parent) else 0

    def _parent_proof_revoked(
        self, origin: DamageObjectOrigin, timestamp: float | None = None,
        packet_serial: int = 0, record_offset: int = 0,
    ) -> bool:
        for dependency in origin.parent_proof_dependencies:
            revoked = self._revoked_origin_generations.get(dependency)
            if revoked is None:
                continue
            at, serial, offset = revoked
            if timestamp is None:
                return True
            if packet_serial > 0 and serial > 0:
                if packet_serial > serial or (packet_serial == serial and (
                    record_offset <= 0 or offset <= 0 or record_offset >= offset
                )):
                    return True
            elif timestamp >= at:
                return True
        return False

    def _live_parent_origin(
        self,
        *,
        source_actor_id: int,
        child_projectile_template_id: int,
        timestamp: float,
        parent_damage_object_id: int = 0,
        packet_serial: int = 0,
        record_offset: int = 0,
    ) -> DamageObjectOrigin | None:
        parent_template_ids = _PARENT_TEMPLATES_BY_CHILD.get(
            child_projectile_template_id, frozenset()
        )
        if not parent_template_ids:
            return None
        candidates: list[DamageObjectOrigin] = []
        keys = ((source_actor_id, parent_damage_object_id),) if parent_damage_object_id else (
            self._origins.keys() | self._packet_origin_history.keys()
        )
        for key in keys:
            candidate, snapshot = self._origin_at(key, timestamp, packet_serial, record_offset)
            if candidate is None:
                continue
            if (
                candidate.owner_actor_id != source_actor_id
                or candidate.projectile_template_id not in parent_template_ids
                or not candidate.registered_at <= timestamp <= candidate.expires_at
                or not self._origin_has_deterministic_lineage(candidate, timestamp, packet_serial, record_offset)
            ):
                continue
            invalidation = snapshot.invalidation if snapshot is not None else self._unknown_registration_invalidations.get(
                (candidate.owner_actor_id, candidate.damage_object_id)
            )
            if invalidation is not None and self._boundary_reached(invalidation, timestamp, packet_serial, record_offset):
                continue
            candidates.append(candidate)
        if not candidates:
            return None
        # A child edge may be shared by several parent effects.  It is safe at
        # skill level only when all simultaneously live parents collapse to
        # one canonical family; otherwise keep the child unclaimed.
        if len({row.canonical_skill_id for row in candidates}) != 1:
            return None
        return max(candidates, key=lambda row: row.registered_at)

    def _kali_11076_parent_witness(
        self, owner: int, parent_id: int, timestamp: float,
        packet_serial: int, record_offset: int,
    ) -> _Kali11076ParentWitness | None:
        """A full parent identity and one original root, never a nearest cast."""
        key = (owner, parent_id)
        parent, snapshot = self._origin_at(key, timestamp, packet_serial, record_offset)
        if (parent is None or parent.owner_actor_id != owner
                or parent.projectile_template_id != 11075
                or (parent.effect_resource_id, parent.child_sequence) != (224, 16)
                or parent.parent_backed or parent.parent_damage_object_id
                or not self._has_independent_action(parent)
                or not parent.registered_at <= timestamp <= parent.expires_at):
            return None
        invalidation = (snapshot.invalidation if snapshot is not None
                        else self._unknown_registration_invalidations.get(key))
        revoked = self._revoked_origin_generations.get((*key, parent.generation))
        if any(boundary is not None and self._boundary_reached(
            boundary, timestamp, packet_serial, record_offset,
        ) for boundary in (invalidation, revoked)):
            return None
        roots = [row for row in self._kali_raw_roots if row[0] == owner
                 and 0 <= parent.registered_at - row[2] <= 2.0]
        if len(roots) != 1:
            return None
        _, raw_skill, root_at = roots[0]
        epochs = [a for a in self._actions if a.owner_actor_id == owner
                  and a.raw_skill_id == raw_skill and a.observed_at == root_at
                  and a.action_epoch_id == parent.action_epoch_id]
        if len(epochs) != 1 or epochs[0].action_epoch_id <= 0:
            return None
        return _Kali11076ParentWitness(
            parent.generation, parent.registered_at, parent.expires_at,
            parent.registered_packet_serial, parent.registered_record_offset,
            epochs[0].action_epoch_id, root_at, raw_skill,
        )

    def _kali_11076_parent_current(
        self, origin: DamageObjectOrigin, timestamp: float,
        packet_serial: int = 0, record_offset: int = 0,
    ) -> bool:
        frozen = origin.kali_11076_parent_witness
        if (frozen is None or not origin.registered_at <= timestamp <= origin.expires_at
                or timestamp > frozen.parent_expires_at
                or not origin.parent_lineage_exact or not origin.requires_root_action_proof
                or origin.parent_generation != frozen.parent_generation
                or origin.parent_root_action_epoch_id != frozen.root_epoch):
            return False
        key = (origin.owner_actor_id, origin.damage_object_id)
        child, snapshot = self._origin_at(key, timestamp, packet_serial, record_offset)
        if child is None or child.generation != origin.generation:
            return False
        invalidation = (snapshot.invalidation if snapshot is not None
                        else self._unknown_registration_invalidations.get(key))
        revoked = self._revoked_origin_generations.get((*key, origin.generation))
        if any(boundary is not None and self._boundary_reached(
            boundary, timestamp, packet_serial, record_offset,
        ) for boundary in (invalidation, revoked)):
            return False
        current = self._kali_11076_parent_witness(
            origin.owner_actor_id, origin.parent_damage_object_id, timestamp,
            packet_serial, record_offset,
        )
        # A legacy parent echo may extend its lease, but cannot extend ours.
        return current is not None and replace(
            current, parent_expires_at=frozen.parent_expires_at,
        ) == frozen

    @staticmethod
    def _kali_11076_hit_slots(
        payload: bytes, owner: int, obj: int, damage: int, marker: int,
    ) -> tuple[int, int] | None:
        # The eight raw candidates have complete 0873/115 result records.
        # Do not inherit other skills' fixed-context override policy. Unknown
        # result layouts stay unproved; unknown nonempty slots revoke v28.
        if marker < 14 or marker + 118 > len(payload):
            return None
        # A byte match inside another record is not the selected physical Hit.
        # Require full top-level framing, one phase and this exact result, just
        # as the entity self-attack proof does. Companion truncation fails closed.
        from .udp_damage_records import inspect_udp_damage_records
        inventory = inspect_udp_damage_records(payload, "server_to_client")
        if not inventory.complete:
            return None
        candidates = [r for r in inventory.damage_records
                      if r.record_offset == marker and r.family == "direct37"
                      and r.admissible_fields and r.source_actor_id == owner
                      and r.damage_object_id == obj and r.damage == damage]
        if len(candidates) != 1:
            return None
        body = _decode_v576_body(payload, inventory.xor_phase)
        if body[marker:marker + 3] not in _DIRECT37_MARKERS:
            return None
        return tuple(int.from_bytes(body[marker + at:marker + at + 4], "little")
                     for at in (102, 114))

    def observe_registration(
        self,
        *,
        source_actor_id: int,
        damage_object_id: int,
        projectile_template_id: int,
        effect_resource_id: int,
        child_sequence: int,
        timestamp: float,
        packet_serial: int = 0,
        record_offset: int = 0,
        packet_validated_subtype: bool = False,
        action_qualified_subtype: bool = False,
        parent_qualified_subtype: bool = False,
        parent_damage_object_id: int = 0,
        registration_key: str = "",
        state_observation: bool = False,
        requires_root_action_proof: bool = False,
    ) -> DamageObjectOrigin | None:
        self._prune(timestamp)
        self._advance_packet_history(packet_serial)
        key = (source_actor_id, damage_object_id)
        if projectile_template_id in FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS:
            # Preserve a negative generation boundary even if an older,
            # unrelated object reused this owner's numeric object ID.
            self.invalidate_origin(
                source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                timestamp=timestamp, packet_serial=packet_serial,
                record_offset=record_offset,
            )
            return None
        # Newly audited shared-tail formats may request the same independent
        # root proof as FFFF tails. Keep this on the generation; a later short
        # state prefix must never turn the restriction off.
        requires_root_action_proof = requires_root_action_proof or (
            parent_damage_object_id > 0 and effect_resource_id == 0xFFFF
        )
        spec = _SPEC_BY_PROJECTILE_TEMPLATE.get(projectile_template_id)
        if source_actor_id <= 0 or damage_object_id <= 0:
            return None
        current = self._origins.get(key)
        out_of_order = bool(
            current is not None and packet_serial > 0
            and current.registered_packet_serial == packet_serial
            and 0 < record_offset < current.registered_record_offset
        )
        previous, previous_snapshot = self._origin_at(
            key, timestamp, packet_serial, record_offset
        ) if out_of_order else (current, None)
        previous_invalidation = (
            previous_snapshot.invalidation if previous_snapshot is not None
            else None if out_of_order else self._unknown_registration_invalidations.get(key)
        )
        kali_witness = None
        kali_format = registration_key.startswith(_KALI_11076_PARENT_V28_KEY_PREFIX)
        if previous is not None and previous.kali_11076_parent_witness is not None:
            if projectile_template_id == 11076:
                # Full/short state echoes and client mirrors may retain, never
                # renew or replace, the first v28 proof. Conflicting edges revoke.
                same_edge = (
                    parent_damage_object_id == previous.parent_damage_object_id
                    and (effect_resource_id, child_sequence) == (previous.effect_resource_id, 66)
                ) or (
                    state_observation and not registration_key and parent_damage_object_id == 0
                    and effect_resource_id == 0 and child_sequence == 0
                )
                if (same_edge
                        and not out_of_order and previous_invalidation is None
                        and (*key, previous.generation) not in self._revoked_origin_generations
                        and self._registered_by(previous, timestamp, packet_serial, record_offset)
                        and self._kali_11076_parent_current(previous, timestamp, packet_serial, record_offset)):
                    return previous
                self.invalidate_origin(source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                                       timestamp=timestamp, packet_serial=packet_serial, record_offset=record_offset)
                return None
            # A genuinely different registration still follows its old logic;
            # the displaced v28 generation must not remain usable by descendants.
            self.invalidate_origin(source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                                   timestamp=timestamp, packet_serial=packet_serial, record_offset=record_offset)
            previous_invalidation = self._unknown_registration_invalidations.get(key)
        if kali_format:
            if (spec is not None and projectile_template_id == 11076
                    and state_observation and parent_qualified_subtype and requires_root_action_proof
                    and child_sequence == 66 and 0 < effect_resource_id <= 0xFFFF
                    and 0 < parent_damage_object_id <= 0xFFFFFF
                    and parent_damage_object_id != damage_object_id
                    and previous is None and not out_of_order
                    and previous_invalidation is None and key not in self._generation_counters
                    and key not in self._last_parent_state_identities
                    and timestamp >= self._latest_observed_timestamp
                    and timestamp > self._parent_state_history_gap_until):
                kali_witness = self._kali_11076_parent_witness(
                    source_actor_id, parent_damage_object_id, timestamp, packet_serial, record_offset,
                )
            if kali_witness is None:
                # A failed state observation is not a creation that a later
                # action/parent may backfill. Existing unrelated origins stay intact.
                if previous is None:
                    self.invalidate_origin(source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                                           timestamp=timestamp, packet_serial=packet_serial, record_offset=record_offset)
                return None
        if state_observation and registration_key and parent_damage_object_id:
            identity = (projectile_template_id, parent_damage_object_id)
            retired = self._last_parent_state_identities.get(key)
            if (retired is None and self._parent_state_history_gap_until > 0
                    and timestamp <= self._parent_state_history_gap_until
                    and not (previous is not None and previous.parent_lineage_exact
                             and (previous.projectile_template_id, previous.parent_damage_object_id) == identity)):
                # Capacity eviction cannot launder a prefix/new observation
                # into a full chain. Existing exact generations are checked
                # below and can be retained unchanged; client creations use
                # their separate non-state path. Finite retention deliberately
                # makes no promise about arbitrarily late packets.
                return None
            if (not out_of_order and retired is not None and retired[:2] == identity
                    and (previous is None or previous.generation != retired[2])):
                # A prefix-only observation after expiry is still not a new
                # creation. It must not launder this retired full-parent tail.
                return None
            if previous is None:
                # A full tail can establish a FIRST bounded observation, but
                # cannot resurrect the same identity after retention pruning.
                if previous_invalidation is not None and retired is None:
                    return None
            elif (
                previous.projectile_template_id == projectile_template_id
                and previous.parent_damage_object_id in (0, parent_damage_object_id)
            ):
                revoked = self._revoked_origin_generations.get((
                    source_actor_id, damage_object_id, previous.generation,
                ))
                if (
                    not self._registered_by(previous, timestamp, packet_serial, record_offset)
                    or timestamp > previous.expires_at
                    or (previous_invalidation is not None and self._boundary_reached(
                        previous_invalidation, timestamp, packet_serial, record_offset,
                    ))
                    or (revoked is not None and self._boundary_reached(
                        revoked, timestamp, packet_serial, record_offset,
                    ))
                    or self._parent_proof_revoked(previous, timestamp, packet_serial, record_offset)
                ):
                    return None
                if previous.parent_lineage_exact:
                    # Freeze birth, expiry, parent GENERATION and root epoch.
                    # Neither >250 ms nor changed coordinates prove creation;
                    # reusing the parent's numeric ID cannot rebind this child.
                    return previous
                parent = self._live_parent_origin(
                    source_actor_id=source_actor_id,
                    child_projectile_template_id=projectile_template_id,
                    timestamp=timestamp, parent_damage_object_id=parent_damage_object_id,
                    packet_serial=packet_serial, record_offset=record_offset,
                )
                if (
                    parent is None or parent.registered_at > previous.registered_at
                    or (previous.parent_backed and parent.generation != previous.parent_generation)
                ):
                    return None
                family_changed = parent.canonical_skill_id != previous.canonical_skill_id
                if family_changed and (
                    previous.canonical_skill_id not in _SHARED_CHILD_DIRECT_SKILL_IDS.get(
                        projectile_template_id, frozenset(),
                    )
                    or self._origin_has_deterministic_lineage(
                        previous, timestamp, packet_serial, record_offset,
                    )
                ):
                    return None
                root_epoch = self._parent_root_action_epoch(parent)
                # Enrichment is not permission to import an action observed
                # after this object's birth. In particular, never upgrade a
                # rootless child using its own later compatible action.
                if root_epoch and not any(
                    action.action_epoch_id == root_epoch
                    and action.owner_actor_id == source_actor_id
                    and action.observed_at <= previous.registered_at
                    for action in self._actions
                ):
                    root_epoch = 0
                upgraded = replace(
                    previous, parent_backed=True, parent_qualified_subtype=True,
                    canonical_skill_id=parent.canonical_skill_id,
                    # Discard the voted cast epoch (possibly FireBird) even
                    # when it is nonzero. Only the independently established
                    # root may supply the corrected family's action epoch.
                    action_epoch_id=root_epoch, action_backed=False,
                    response_backed=previous.response_backed and not family_changed,
                    action_qualified_subtype=previous.action_qualified_subtype and not family_changed,
                    parent_projectile_template_id=parent.projectile_template_id,
                    parent_generation=parent.generation,
                    parent_damage_object_id=parent_damage_object_id,
                    parent_lineage_exact=True, parent_root_action_epoch_id=root_epoch,
                    requires_root_action_proof=(
                        previous.requires_root_action_proof or requires_root_action_proof
                    ),
                    effect_resource_id=effect_resource_id,
                    parent_proof_dependencies=parent.parent_proof_dependencies | frozenset({(
                        parent.owner_actor_id, parent.damage_object_id, parent.generation,
                    )}),
                    registration_key=previous.registration_key or registration_key,
                    registered_packet_serial=int(packet_serial),
                    registered_record_offset=int(record_offset),
                )
                # Keep a pre-proof view for earlier Hits in this same packet.
                # This enriches evidence, not generation/birth/lifetime.
                if previous_snapshot is not None:
                    end_offset = previous_snapshot.superseded_record_offset
                    previous_snapshot.superseded_record_offset = record_offset
                    self._packet_origin_history.setdefault(key, []).append(
                        _PacketOriginSnapshot(upgraded, timestamp, end_offset, previous_invalidation)
                    )
                else:
                    self._snapshot_before_replacement(
                        previous, timestamp, packet_serial, record_offset, previous_invalidation,
                    )
                    self._origins[key] = upgraded
                    self._remember_parent_state_identity(upgraded)
                return upgraded
        if spec is not None and state_observation and not registration_key and (
            previous_invalidation is not None
            or (previous is not None and (
                (source_actor_id, damage_object_id, previous.generation) in self._revoked_origin_generations
                or self._parent_proof_revoked(previous)
            ))
        ):
            # State-only evidence cannot clear a lifecycle tombstone or
            # resurrect a revoked creation. Do not fall through to the legacy
            # <=0.5 s merge, which would retain its token and full-parent proof.
            return None
        if (
            state_observation
            and not registration_key
            and previous is not None
            and previous.registration_key
            and previous.projectile_template_id == projectile_template_id
            and previous.registered_at <= timestamp <= previous.expires_at
            and key not in self._unknown_registration_invalidations
            and not self._parent_proof_revoked(previous)
        ):
            # A server broadcast of the same live object is not another
            # creation. In particular it lacks the full parent field carried
            # by the client creation message. Never downgrade that verified
            # generation/parent/action proof to a nearby-parent guess merely
            # because a later state observation arrives after 0.5 seconds.
            # This does NOT extend its lifetime, revive invalidated slots, or
            # merge a new creation token. Server-only origins keep the legacy
            # conservative generation policy until creation can be decoded.
            return previous
        if registration_key:
            replica_key = (source_actor_id, damage_object_id, registration_key)
            first_at = self._registration_replicas.get(replica_key)
            if first_at is not None and abs(timestamp - first_at) <= 0.25:
                # A delayed relay copy must not revive a superseded generation.
                # Retain the FIRST time so repeated copies cannot extend life.
                return current if (
                    current is not None
                    and current.registration_key == registration_key
                    and key not in self._unknown_registration_invalidations
                ) else None
            self._registration_replicas[replica_key] = timestamp
        if spec is None:
            # An unknown birth is also a generation boundary for descendants
            # that froze this object's earlier parent proof. A slot tombstone
            # alone blocks the parent Hit but leaves those cached dependencies
            # live. Revoke the exact generation selected at this byte position;
            # a bundled Hit before the boundary retains its previous proof.
            # If preprocessing already installed a later full birth in this
            # packet and no earlier generation exists, there is nothing to
            # revoke. Do not put a global tombstone before that later birth.
            if not out_of_order or previous is not None or not current.registration_key:
                self.invalidate_origin(
                    source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                    timestamp=timestamp, packet_serial=packet_serial,
                    record_offset=record_offset,
                )
            return None
        shared_direct_actions = [
            action
            for action in self._actions
            if action.owner_actor_id == source_actor_id
            and action.canonical_skill_id
            in _SHARED_CHILD_DIRECT_SKILL_IDS.get(
                projectile_template_id, frozenset()
            )
            and action.observed_at <= timestamp <= action.natural_end_at
        ]
        shared_direct_action_epoch = (
            max(shared_direct_actions, key=lambda row: row.observed_at)
            if shared_direct_actions
            else None
        )
        parent_origin = (
            self._live_parent_origin(
                source_actor_id=source_actor_id,
                child_projectile_template_id=projectile_template_id,
                timestamp=timestamp,
                parent_damage_object_id=parent_damage_object_id,
                packet_serial=packet_serial,
                record_offset=record_offset,
            )
            if (
                parent_qualified_subtype
                or parent_damage_object_id
                or projectile_template_id
                in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
            )
            else None
        )
        # Retain the legacy display vote for an unqualified prefix. A recent
        # alternative action does not prove a typed direct-family edge; this
        # candidate stays provisional and loses to a later exact parent link.
        if (
            not parent_damage_object_id
            and
            shared_direct_action_epoch is not None
            and (
                parent_origin is None
                or shared_direct_action_epoch.observed_at
                >= parent_origin.registered_at
            )
        ):
            parent_origin = None
        else:
            shared_direct_action_epoch = None
        if (
            (parent_qualified_subtype or parent_damage_object_id)
            and parent_origin is None
            and shared_direct_action_epoch is None
        ):
            boundary = (
                timestamp,
                int(packet_serial),
                int(record_offset),
            )
            if previous_snapshot is not None:
                previous_snapshot.invalidation = self._first_boundary(previous_invalidation, boundary)
            if not out_of_order or not current.registration_key:
                self._unknown_registration_invalidations[key] = self._first_boundary(
                    self._unknown_registration_invalidations.get(key), boundary,
                )
            return None
        if not out_of_order:
            self._unknown_registration_invalidations.pop(key, None)
        resolved_canonical_skill_id = (
            shared_direct_action_epoch.canonical_skill_id
            if shared_direct_action_epoch is not None
            else parent_origin.canonical_skill_id
            if parent_origin is not None
            else spec.canonical_skill_id
        )
        response_backed = any(
            response.owner_actor_id == source_actor_id
            and response.canonical_skill_id == resolved_canonical_skill_id
            and 0.0 <= timestamp - response.observed_at
            <= self._RESPONSE_ATTACH_SECONDS
            and spec.required_response_codes.issubset(response.components)
            for response in self._responses
        )
        compatible_actions = [
            action
            for action in self._actions
            if action.owner_actor_id == source_actor_id
            and action.canonical_skill_id == resolved_canonical_skill_id
            and action.observed_at <= timestamp <= action.natural_end_at
        ]
        action_epoch = (
            max(compatible_actions, key=lambda row: row.observed_at)
            if compatible_actions
            else None
        )
        action_backed = action_epoch is not None
        lifecycle_backed = effect_resource_id == 0 and child_sequence == 0
        origin = previous
        if (
            origin is None
            or previous_invalidation is not None
            or timestamp > origin.expires_at
            or (source_actor_id, damage_object_id, origin.generation) in self._revoked_origin_generations
            or origin.projectile_template_id != projectile_template_id
            or origin.canonical_skill_id != resolved_canonical_skill_id
            or (registration_key and (
                origin.registration_key != registration_key
                or abs(timestamp - origin.registered_at) > 0.25
            ))
            or (parent_damage_object_id and origin.parent_damage_object_id != parent_damage_object_id)
            or timestamp - origin.registered_at > 0.5
        ):
            if origin is not None and not out_of_order:
                self._snapshot_before_replacement(
                    origin, timestamp, packet_serial, record_offset, previous_invalidation,
                )
            generation = self._generation_counters.get(key, 0) + 1
            self._generation_counters[key] = generation
            origin = DamageObjectOrigin(
                owner_actor_id=source_actor_id,
                damage_object_id=damage_object_id,
                projectile_template_id=projectile_template_id,
                effect_resource_id=effect_resource_id,
                child_sequence=child_sequence,
                canonical_skill_id=resolved_canonical_skill_id,
                generation=generation,
                registered_at=timestamp,
                # A server object-state registration proves that the object
                # exists at this instant; it is not an immortal ownership
                # lease. Runtime object numbers are reused after counter wrap
                # and scene changes, and not every reuse is preceded by an
                # already-decoded destroy packet. Bound both local and server
                # registrations by the audited effect lifetime. Long-lived
                # summons use the separate entity/summon ownership ledger.
                expires_at=timestamp + spec.lifetime_seconds,
                action_backed=action_backed,
                response_backed=response_backed,
                action_epoch_id=(
                    action_epoch.action_epoch_id
                    if action_epoch is not None
                    else parent_origin.action_epoch_id
                    if parent_origin is not None
                    else 0
                ),
                lifecycle_backed=lifecycle_backed,
                packet_validated_subtype=bool(
                    packet_validated_subtype
                ),
                action_qualified_subtype=bool(
                    action_qualified_subtype
                ),
                parent_qualified_subtype=bool(
                    parent_qualified_subtype
                ),
                parent_backed=parent_origin is not None,
                parent_projectile_template_id=(
                    parent_origin.projectile_template_id
                    if parent_origin is not None
                    else 0
                ),
                parent_generation=(
                    parent_origin.generation
                    if parent_origin is not None
                    else 0
                ),
                parent_damage_object_id=(parent_origin.damage_object_id if parent_origin else parent_damage_object_id),
                parent_lineage_exact=parent_origin is not None and parent_damage_object_id > 0,
                parent_root_action_epoch_id=(
                    self._parent_root_action_epoch(parent_origin) if parent_damage_object_id > 0 else 0
                ),
                requires_root_action_proof=requires_root_action_proof,
                kali_11076_parent_witness=kali_witness,
                parent_proof_dependencies=(
                    parent_origin.parent_proof_dependencies | frozenset({(
                        parent_origin.owner_actor_id, parent_origin.damage_object_id,
                        parent_origin.generation,
                    )}) if parent_origin is not None else frozenset()
                ),
                registration_key=registration_key,
                registered_packet_serial=int(packet_serial),
                registered_record_offset=int(record_offset),
            )
            if out_of_order:
                # Server marker families are scanned separately. Inserting a
                # physically earlier registration must split the old interval,
                # not replace the already-seen later generation/current slot.
                end_offset = (
                    previous_snapshot.superseded_record_offset
                    if previous_snapshot is not None else current.registered_record_offset
                )
                if previous_snapshot is not None:
                    previous_snapshot.superseded_record_offset = record_offset
                self._packet_origin_history.setdefault(key, []).append(
                    _PacketOriginSnapshot(origin, timestamp, end_offset)
                )
            else:
                self._origins[key] = origin
                if parent_damage_object_id:
                    self._remember_parent_state_identity(origin)
                elif not state_observation:
                    self._last_parent_state_identities.pop(key, None)
        else:
            # An already complete non-FFFF chain is not downgraded by a
            # later FFFF state echo. First full-parent evidence and existing
            # FFFF generations must retain the stricter root requirement.
            origin.requires_root_action_proof = origin.requires_root_action_proof or (
                requires_root_action_proof and not origin.parent_lineage_exact
            )
            origin.expires_at = max(
                origin.expires_at,
                timestamp + spec.lifetime_seconds,
            )
            origin.action_backed = origin.action_backed or action_backed
            if origin.action_epoch_id == 0 and action_epoch is not None:
                origin.action_epoch_id = action_epoch.action_epoch_id
            origin.response_backed = origin.response_backed or response_backed
            origin.lifecycle_backed = (
                origin.lifecycle_backed or lifecycle_backed
            )
            origin.packet_validated_subtype = (
                origin.packet_validated_subtype
                or bool(packet_validated_subtype)
            )
            origin.action_qualified_subtype = (
                origin.action_qualified_subtype
                or bool(action_qualified_subtype)
            )
            origin.parent_qualified_subtype = (
                origin.parent_qualified_subtype
                or bool(parent_qualified_subtype)
            )
            if not origin.parent_backed and parent_origin is not None:
                origin.parent_backed = True
                origin.parent_projectile_template_id = (
                    parent_origin.projectile_template_id
                )
                origin.parent_generation = parent_origin.generation
                origin.parent_damage_object_id = parent_origin.damage_object_id
                origin.parent_lineage_exact = parent_damage_object_id > 0
                origin.parent_root_action_epoch_id = (
                    self._parent_root_action_epoch(parent_origin) if parent_damage_object_id > 0 else 0
                )
                origin.parent_proof_dependencies = parent_origin.parent_proof_dependencies | frozenset({(
                    parent_origin.owner_actor_id, parent_origin.damage_object_id, parent_origin.generation,
                )})
        return origin

    def invalidate_origin(
        self,
        *,
        source_actor_id: int,
        damage_object_id: int,
        timestamp: float,
        packet_serial: int = 0,
        record_offset: int = 0,
    ) -> None:
        """Stop one object generation after contradictory fixed-slot truth."""

        if source_actor_id <= 0 or damage_object_id <= 0:
            return
        self._latest_observed_timestamp = max(self._latest_observed_timestamp, timestamp)
        self._advance_packet_history(packet_serial)
        key = (source_actor_id, damage_object_id)
        boundary = (timestamp, int(packet_serial), int(record_offset))
        origin, snapshot = self._origin_at(key, timestamp, packet_serial, record_offset)
        if origin is not None:
            generation_key = (*key, origin.generation)
            self._revoked_origin_generations[generation_key] = self._first_boundary(
                self._revoked_origin_generations.get(generation_key), boundary,
            )
        if snapshot is not None:
            # A contradictory pre-switch Hit revokes its old generation, not
            # the newer registration which preprocessing already installed.
            snapshot.invalidation = self._first_boundary(snapshot.invalidation, boundary)
        else:
            self._unknown_registration_invalidations[key] = self._first_boundary(
                self._unknown_registration_invalidations.get(key), boundary,
            )

    @staticmethod
    def _direct_hit_expires_at(origin: DamageObjectOrigin) -> float:
        spec = _SPEC_BY_PROJECTILE_TEMPLATE.get(origin.projectile_template_id)
        if spec is None:
            return origin.expires_at
        # This is a deadline for an already registered exact object. It never
        # refreshes its generation or expands parent/child creation windows.
        return max(origin.expires_at,
                   origin.registered_at + spec.direct_hit_lifetime_seconds)

    def attribute_direct37(
        self,
        *,
        source_actor_id: int,
        timestamp: float,
        layout: str,
        payload: bytes,
        direction: str | None,
        damage: int,
        record_offset: int = 0,
        packet_serial: int = 0,
        _post_admitted_late: bool = False,
        _evidence_only: bool = False,
    ) -> DamageObjectAttribution | None:
        if source_actor_id <= 0 or damage <= 0 or "direct37" not in layout:
            return None
        if not _post_admitted_late and not _evidence_only:
            self._prune(timestamp)
            self._advance_packet_history(packet_serial)
        damage_object_id = direct37_damage_object_id(
            payload,
            direction,
            source_actor_id=source_actor_id,
            damage=damage,
            record_offset=record_offset,
        )
        if damage_object_id is None:
            return None
        key = (source_actor_id, damage_object_id)
        origin, snapshot = self._origin_at(key, timestamp, packet_serial, record_offset)
        if origin is None:
            return None
        if origin.projectile_template_id in FULL_PLAYER_ROOT_ACTION_REQUIRED_PROJECTILE_TEMPLATE_IDS:
            return None
        if _post_admitted_late:
            # Metadata-only lookup: typed15 +80 declares 10000ms for these
            # two creators, corroborated by complete same-object late Hits.
            # This lookup never changes admission/lineage or refreshes a lifetime.
            if (origin.projectile_template_id not in (5508, 5717)
                    or not 8.0 < timestamp - origin.registered_at <= 10.0):
                return None
        elif timestamp > self._direct_hit_expires_at(origin):
            return None
        invalidation = snapshot.invalidation if snapshot is not None else self._unknown_registration_invalidations.get(key)
        if invalidation is not None and self._boundary_reached(invalidation, timestamp, packet_serial, record_offset):
            return None
        revoked = self._revoked_origin_generations.get((*key, origin.generation))
        if revoked is not None and self._boundary_reached(revoked, timestamp, packet_serial, record_offset):
            return None
        if origin.kali_11076_parent_witness is not None and not self._kali_11076_parent_current(
            origin, timestamp, packet_serial, record_offset,
        ):
            return None
        if origin.kali_11076_parent_witness is not None:
            slots = self._kali_11076_hit_slots(payload, source_actor_id, damage_object_id, damage, record_offset)
            if slots is None:
                return None
            if any(value not in (0, 0xFFFFFFFF, 5217, 5406) for value in slots):
                if not _evidence_only:
                    self.invalidate_origin(source_actor_id=source_actor_id, damage_object_id=damage_object_id,
                                           timestamp=timestamp, packet_serial=packet_serial,
                                           record_offset=record_offset + 1)
                return None
        spec = _SPEC_BY_PROJECTILE_TEMPLATE.get(
            origin.projectile_template_id
        )
        parent_proof_revoked = self._parent_proof_revoked(origin, timestamp, packet_serial, record_offset)
        if parent_proof_revoked and not self._has_independent_action(origin) and not origin.packet_validated_subtype:
            return None
        if (
            spec is not None
            and spec.require_action_evidence
            and not origin.action_backed
        ):
            # Some destination-side projectile registrations (observed for
            # Sunshine Spark) are emitted under a recipient's actor ID.  The
            # ACT/template identifies the effect, but not necessarily its
            # caster.  Do not move damage between professions unless a
            # reliable same-owner action proves the owner semantics.
            return None
        if origin.action_qualified_subtype and not origin.action_backed:
            # Alternate lifecycle families are not globally skill-stable.
            # The concrete row is therefore unusable until a same-owner,
            # same-family action epoch proves this object generation.
            return None
        if (
            origin.parent_qualified_subtype
            and not origin.parent_backed
            and not (
                origin.action_backed
                and origin.canonical_skill_id
                in _SHARED_CHILD_DIRECT_SKILL_IDS.get(
                    origin.projectile_template_id, frozenset()
                )
            )
        ):
            return None
        if (
            origin.projectile_template_id
            in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
            and not origin.parent_backed
            and not origin.action_backed
            and not origin.packet_validated_subtype
        ):
            # These child templates have packet-level counterexamples for a
            # global mapping.  A live parent generation or matching own action
            # is mandatory even when the structural marker is otherwise valid.
            return None
        if not _post_admitted_late and not _evidence_only:
            origin.hit_count += 1
            origin.last_hit_at = timestamp
        return DamageObjectAttribution(
            owner_actor_id=origin.owner_actor_id,
            damage_object_id=origin.damage_object_id,
            projectile_template_id=origin.projectile_template_id,
            generation=origin.generation,
            skill_id=origin.canonical_skill_id,
            confidence=(
                1.0
                if (
                    origin.action_backed
                    or origin.response_backed
                    or origin.parent_backed
                )
                else 0.995
            ),
            action_backed=origin.action_backed,
            response_backed=origin.response_backed,
            packet_validated=(
                origin.projectile_template_id
                in PACKET_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
            ),
            client_act_validated=(
                origin.projectile_template_id
                in CLIENT_ACT_VALIDATED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
                and not origin.action_qualified_subtype
                and not origin.parent_qualified_subtype
                and origin.projectile_template_id
                not in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
            ),
            action_qualified_validated=(
                self._has_independent_action(origin)
                and (
                    origin.action_qualified_subtype
                    or origin.projectile_template_id
                    in ACTION_QUALIFIED_DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS
                    or origin.projectile_template_id
                    in PARENT_LINEAGE_REQUIRED_PROJECTILE_TEMPLATE_IDS
                )
            ),
            parent_lineage_validated=origin.parent_lineage_exact and not parent_proof_revoked,
            parent_projectile_template_id=origin.parent_projectile_template_id,
            packet_validated_subtype=origin.packet_validated_subtype,
            parent_action_lineage_validated=(
                origin.parent_lineage_exact
                and origin.parent_root_action_epoch_id > 0
                and not parent_proof_revoked
            ),
            parent_root_action_epoch_id=origin.parent_root_action_epoch_id,
            requires_root_action_proof=origin.requires_root_action_proof,
        )

    def direct37_candidate_skill_ids(
        self,
        *,
        source_actor_id: int,
        timestamp: float,
        layout: str,
        payload: bytes,
        direction: str | None,
        damage: int,
        record_offset: int = 0,
        packet_serial: int = 0,
    ) -> frozenset[int]:
        """Expose a live object's possible family only as negative evidence.

        Shared child objects can require a complete parent chain before they
        may become deterministic attribution. Even without that proof, a
        current configured generation must stop an unrelated timing fallback
        from stealing the same physical damage row. This lookup never assigns
        a skill and never increments object hit counters.
        """

        if source_actor_id <= 0 or damage <= 0 or "direct37" not in layout:
            return frozenset()
        damage_object_id = direct37_damage_object_id(
            payload,
            direction,
            source_actor_id=source_actor_id,
            damage=damage,
            record_offset=record_offset,
        )
        if damage_object_id is None:
            return frozenset()
        key = (source_actor_id, damage_object_id)
        origin, snapshot = self._origin_at(
            key, timestamp, packet_serial, record_offset
        )
        if origin is None or timestamp > self._direct_hit_expires_at(origin):
            return frozenset()
        invalidation = (
            snapshot.invalidation
            if snapshot is not None
            else self._unknown_registration_invalidations.get(key)
        )
        if invalidation is not None and self._boundary_reached(
            invalidation, timestamp, packet_serial, record_offset
        ):
            return frozenset()
        revoked = self._revoked_origin_generations.get((*key, origin.generation))
        if revoked is not None and self._boundary_reached(
            revoked, timestamp, packet_serial, record_offset
        ):
            return frozenset()
        spec = _SPEC_BY_PROJECTILE_TEMPLATE.get(origin.projectile_template_id)
        if spec is None:
            return frozenset()
        return frozenset(
            {int(spec.canonical_skill_id), *(int(value) for value in spec.skill_ids)}
        )

    def origins(self) -> tuple[DamageObjectOrigin, ...]:
        return tuple(self._origins.values())
