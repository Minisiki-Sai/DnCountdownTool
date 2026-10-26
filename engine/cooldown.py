from __future__ import annotations

from collections.abc import Callable
from typing import Any


NumberResolver = Callable[[str, str, float], float]


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SkillCooldownEngine:
    """Calculate long-term average cooldowns from maintainable skill relationships."""

    def __init__(self, data: dict[str, Any], base_cooldown_resolver: NumberResolver):
        self.data = data if isinstance(data, dict) else {}
        self.jobs = self.data.get("jobs", {}) if isinstance(self.data.get("jobs"), dict) else {}
        self.external_cdr_cap = max(0.0, min(0.99, as_float(self.data.get("external_cdr_cap"), 0.7)))
        self.base_cooldown_resolver = base_cooldown_resolver

    def rule(self, job: str, skill: str) -> dict[str, Any]:
        job_rules = self.jobs.get(str(job).strip(), {})
        if not isinstance(job_rules, dict):
            return {}
        rule = job_rules.get(str(skill).strip(), {})
        return rule if isinstance(rule, dict) else {}

    def calculate(self, job: str, skill: str, base_cooldown: float, external_cdr_percent: float = 0.0) -> float:
        return self._calculate(
            str(job).strip(),
            str(skill).strip(),
            max(0.0, as_float(base_cooldown)),
            self._external_cdr(external_cdr_percent),
            set(),
        )

    def infer_base_cooldown(
        self,
        job: str,
        skill: str,
        displayed_cooldown: float,
        external_cdr_percent: float = 0.0,
    ) -> float:
        """Invert the model so the editable cooldown cell keeps its existing behavior."""
        target = max(0.0, as_float(displayed_cooldown))
        if target <= 0:
            return 0.0
        low = 0.0
        high = max(1000.0, target * 20.0)
        for _ in range(80):
            middle = (low + high) / 2
            value = self.calculate(job, skill, middle, external_cdr_percent)
            if value < target:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    def _external_cdr(self, percent: float) -> float:
        return min(self.external_cdr_cap, max(0.0, as_float(percent) / 100))

    def _calculate(
        self,
        job: str,
        skill: str,
        base_cooldown: float,
        external_cdr: float,
        stack: set[tuple[str, str]],
    ) -> float:
        if base_cooldown <= 0:
            return 0.0
        rule = self.rule(job, skill)
        if not rule:
            return base_cooldown * (1 - external_cdr)

        key = (job, skill)
        external_factor = 1 - external_cdr
        passive = min(0.99, max(0.0, as_float(rule.get("passive_percent"))))
        internal_work = base_cooldown * (1 - passive)
        internal_work = max(0.0, internal_work - max(0.0, as_float(rule.get("self_refund_seconds"))))
        work = internal_work * external_factor
        if work <= 0:
            return 0.0

        rate = 0.0
        next_stack = {*stack, key}
        sources = rule.get("sources", [])
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                source_name = str(source.get("skill") or "").strip()
                fallback = max(0.0, as_float(source.get("source_cooldown_fallback")))
                source_base = max(0.0, as_float(self.base_cooldown_resolver(job, source_name, fallback), fallback))
                affected = source.get("affected_by_external_cdr", True) is not False
                configured_effective = source.get("use_effective_cooldown")
                use_effective = (
                    bool(self.rule(job, source_name))
                    if configured_effective is None
                    else configured_effective is True
                )
                if source_base <= 0:
                    continue
                if not affected:
                    source_interval = source_base
                elif use_effective and (job, source_name) not in next_stack:
                    source_interval = self._calculate(job, source_name, source_base, external_cdr, next_stack)
                else:
                    source_interval = source_base * (1 - external_cdr)
                if source_interval <= 0:
                    continue

                source_type = str(source.get("type") or "").strip()
                value = max(0.0, as_float(source.get("value")))
                if source_type == "fixed_seconds":
                    # Fixed reductions are measured in skill-cooldown seconds, not wall-clock seconds.
                    rate += value * external_factor / source_interval
                elif source_type == "reset":
                    rate += work / source_interval
                elif source_type == "grant_charge":
                    # Ideal long-run rate; finite planners enforce stock caps.
                    rate += work * value / source_interval
                elif source_type == "target_percent":
                    rate += work * value / source_interval

        extra_resets = max(0.0, as_float(rule.get("extra_resets")))
        return work / (1 + rate) / (1 + extra_resets)
