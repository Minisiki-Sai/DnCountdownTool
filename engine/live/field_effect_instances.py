"""Server-backed generations for persistent player field/status attacks.

This ledger records only exact status responses.  It intentionally does not
guess that an arbitrary skill-less direct37 row belongs to an active field:
current captures do not repeat the status instance ID in that damage row.
Keeping lifecycle truth separate makes it possible to add a future decoded
pulse foreign key without teaching the damage path another cast-time rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class FieldEffectRule:
    skill_id: int
    effect_code: str
    effect_class_id: int
    parameters: str
    expected_duration_ms: int
    attack_template_id: int
    pulse_period_seconds: float
    pulse_phase_start_seconds: float
    pulse_phase_end_seconds: float


@dataclass(frozen=True, slots=True)
class FieldEffectGeneration:
    source_actor_id: int
    skill_id: int
    effect_instance_id: int
    applied_at: float
    expires_at: float
    revision: int
    attack_template_id: int

    @property
    def evidence_id(self) -> str:
        return (
            f"field-effect:{self.source_actor_id:08X}:{self.skill_id}:"
            f"{self.effect_instance_id}:{self.attack_template_id}"
        )


FIELD_EFFECT_RULES = MappingProxyType(
    {
        5207: FieldEffectRule(
            skill_id=5207,
            effect_code="135d00",
            effect_class_id=62,
            parameters="Skill_ResentPVEArea_Attack",
            expected_duration_ms=7500,
            # The client resource ``weapon/Kali_ResentPVEArea.act`` is the
            # local attack object selected by this status processor.  Its
            # Hit1 loop has attack phases at frames 20..30 and 80..90 of a
            # 120-frame loop (60 fps): one admissible phase per second.
            attack_template_id=11046,
            pulse_period_seconds=1.0,
            # The physical result packet trails the local ACT phase.  The
            # bounds include the measured transport delay from three
            # independent captures, but exclude the old whole-duration
            # ``recent cast`` window.
            pulse_phase_start_seconds=0.25,
            pulse_phase_end_seconds=0.80,
        ),
    }
)


class FieldEffectInstanceLedger:
    """Track exact START/REFRESH generations, never infer individual Hits."""

    def __init__(self) -> None:
        self._active: dict[tuple[int, int], FieldEffectGeneration] = {}
        self._revision = 0

    def reset(self) -> None:
        self._active.clear()
        self._revision = 0

    def observe_status(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        skill_id: int,
        effect_code: str,
        effect_class_id: int,
        parameters: str,
        duration_ms: int,
        effect_instance_id: int,
        timestamp: float,
    ) -> FieldEffectGeneration | None:
        rule = FIELD_EFFECT_RULES.get(int(skill_id))
        if (
            rule is None
            or source_actor_id <= 0
            or target_actor_id != source_actor_id
            or effect_instance_id <= 0
            or effect_code.casefold() != rule.effect_code
            or int(effect_class_id) != rule.effect_class_id
            or str(parameters or "").strip() != rule.parameters
            or int(duration_ms) != rule.expected_duration_ms
        ):
            return None
        key = (int(source_actor_id), rule.skill_id)
        previous = self._active.get(key)
        if (
            previous is not None
            and previous.effect_instance_id == int(effect_instance_id)
        ):
            return previous
        self._revision += 1
        generation = FieldEffectGeneration(
            source_actor_id=int(source_actor_id),
            skill_id=rule.skill_id,
            effect_instance_id=int(effect_instance_id),
            applied_at=float(timestamp),
            expires_at=float(timestamp) + int(duration_ms) / 1000.0,
            revision=self._revision,
            attack_template_id=rule.attack_template_id,
        )
        self._active[key] = generation
        return generation

    def pulse_generation(
        self,
        source_actor_id: int,
        skill_id: int,
        timestamp: float,
        *,
        expiry_grace_seconds: float = 0.0,
    ) -> FieldEffectGeneration | None:
        """Return the exact generation only inside a client attack phase.

        This is deliberately narrower than ``active_generation``.  A status
        merely being alive cannot name every skill-less Hit produced by the
        same player; the Hit must also land in an attack phase declared by
        the field object's ACT resource.
        """

        generation = self.active_generation(
            source_actor_id,
            skill_id,
            timestamp,
            expiry_grace_seconds=expiry_grace_seconds,
        )
        rule = FIELD_EFFECT_RULES.get(int(skill_id))
        if generation is None or rule is None:
            return None
        elapsed = float(timestamp) - generation.applied_at
        if elapsed < 0:
            return None
        phase = elapsed % rule.pulse_period_seconds
        if not (
            rule.pulse_phase_start_seconds
            <= phase
            <= rule.pulse_phase_end_seconds
        ):
            return None
        return generation

    def active_generation(
        self,
        source_actor_id: int,
        skill_id: int,
        timestamp: float,
        *,
        expiry_grace_seconds: float = 0.0,
    ) -> FieldEffectGeneration | None:
        key = (int(source_actor_id), int(skill_id))
        generation = self._active.get(key)
        if generation is None:
            return None
        if float(timestamp) > generation.expires_at + max(
            0.0, float(expiry_grace_seconds)
        ):
            self._active.pop(key, None)
            return None
        return generation

    def generations(self) -> tuple[FieldEffectGeneration, ...]:
        return tuple(self._active.values())
