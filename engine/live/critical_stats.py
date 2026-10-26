from __future__ import annotations

from typing import Protocol


class _CriticalEvent(Protocol):
    amount: int
    critical: bool
    healing: bool
    skill_id: str
    attribution_method: str
    has_real_hit_evidence: bool | None


class _SkillHitSummary(Protocol):
    damage: int
    hits: int
    critical_samples: int | None


_LEGACY_NON_REAL_HIT_METHODS = frozenset(
    {
        "action_free_special_attack",
        "periodic_cast_fallback",
        "periodic_status",
        "periodic_status_packet",
        "refreshable_pulsing_aura",
        "status_damage_packet",
        "supplemental_cast_owner",
        "supplemental_continuation",
        "supplemental_same_packet",
    }
)


def is_critical_rate_sample(event: _CriticalEvent) -> bool:
    """Return whether a damage row contains one independent critical roll.

    Additional/status/periodic damage remains factual damage, but it does not
    represent another game-facing hit and therefore must not dilute critical
    rate. ``direct_hit_overflow`` is intentionally retained: the overflow only
    changes the display bucket and still carries direct-hit wire evidence.

    ``None`` is reserved for legacy JSON/test events created before real-hit
    evidence was serialized. Those rows keep the old direct-record behaviour,
    while every newly decoded event uses the authoritative boolean flag.
    """

    if event.healing or int(event.amount) <= 0:
        return False
    evidence = event.has_real_hit_evidence
    if evidence is not None:
        return bool(evidence)

    method = str(event.attribution_method or "").strip().casefold()
    if method in _LEGACY_NON_REAL_HIT_METHODS:
        return False
    skill_id = str(event.skill_id or "").strip().casefold()
    if skill_id.startswith("periodic:"):
        return False
    if skill_id.startswith("supplemental:") and method != "direct_hit_overflow":
        return False
    return True


def gameplay_hit_count(skill: _SkillHitSummary) -> int:
    """Expose packet-backed damage Hits, not all attributed damage records.

    SkillSnapshot.hits remains the accounting count for compatibility. Its
    critical_samples count is accumulated only from independently evidenced
    damage Hits, including non-critical ones, and is therefore the appropriate
    numerator for the UI's Hit and average-Hit columns. Pure healing rows keep
    their existing pulse/record count.
    """

    samples = skill.critical_samples
    if int(skill.damage or 0) > 0 and samples is not None:
        return max(0, int(samples))
    return max(0, int(skill.hits or 0))
