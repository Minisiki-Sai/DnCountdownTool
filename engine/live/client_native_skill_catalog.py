"""Reviewed client skill names omitted from the simulator's skill trees.

Evidence: outputs/verification/native_primary_catalog_v475/evidence.json.
Current SkillTable_character row 41 points to UI _NameID 1000082771 and
NeedJob 75. Its SkillLevelTable actions Skill_00_DeathNell / *_Air spawn
projectile 33113. Complete, independently framed direct41 results carry
positive primary90=41 (source kind 1, common_cc 0); the native meter reads
that key first and resolves SkillTable._NameID, without a DamagePanel gate.

The runtime job_75 profile represents the Black Avenger tree. Keep the wire
ID 41, even though ID 40 has the same translated display name. These entries
provide names and profile membership only; they do not extend damage-panel
allowlists or authorize fallback, proximity, or negative-key attribution.
"""
from types import MappingProxyType
from typing import Mapping


CLIENT_NATIVE_SKILL_CATALOG: Mapping[int, tuple[str, str, int]] = MappingProxyType({
    41: ("觉醒•黑暗之刃", "黑暗复仇者", 3),
})

CLIENT_NATIVE_SKILL_PROFILES: Mapping[int, frozenset[str]] = MappingProxyType({
    41: frozenset({"job_75"}),
})
