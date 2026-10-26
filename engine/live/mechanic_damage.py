"""Mechanic damage remains damage, but is not player output-clock evidence."""

MECHANIC_SKILL_IDS = frozenset({"733042"})


def is_mechanic_damage_skill(skill_id: str) -> bool:
    key = str(skill_id or "").strip().casefold()
    return key.startswith("mechanism:") or key in MECHANIC_SKILL_IDS
