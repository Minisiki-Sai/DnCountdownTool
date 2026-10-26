from __future__ import annotations

import copy
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .data_loader import MODULAR_DIR, as_number, load_json, norm


JOB_MECHANICS_PATH = MODULAR_DIR / "tables" / "skills" / "job_mechanics.json"


class JobMechanicsRegistry:
    """Read-only access to editable profession mechanics data."""

    def __init__(self, payload: dict[str, Any], source: Path = JOB_MECHANICS_PATH):
        self.source = source
        self.schema_version = int(payload.get("schema_version", 0))
        self._global = payload.get("global", {}) if isinstance(payload.get("global"), dict) else {}
        self._jobs = payload.get("jobs", {}) if isinstance(payload.get("jobs"), dict) else {}
        self._validate()

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "JobMechanicsRegistry":
        if not JOB_MECHANICS_PATH.exists():
            raise FileNotFoundError(f"职业机制数据不存在：{JOB_MECHANICS_PATH}")
        payload = load_json(JOB_MECHANICS_PATH)
        if not isinstance(payload, dict):
            raise ValueError("职业机制数据根节点必须是对象")
        return cls(payload)

    def _validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"不支持的职业机制数据版本：{self.schema_version}")
        handler_catalog = self._global.get("handler_catalog", {})
        if not isinstance(handler_catalog, dict):
            raise ValueError("global.handler_catalog 必须是对象")
        for job, profile in self._jobs.items():
            if not norm(job) or not isinstance(profile, dict):
                raise ValueError(f"职业机制条目无效：{job!r}")
            handlers = profile.get("runtime_handlers", [])
            if not isinstance(handlers, list) or any(not norm(item) for item in handlers):
                raise ValueError(f"{job} 的 runtime_handlers 必须是非空字符串数组")
            unknown_handlers = {norm(item) for item in handlers} - set(handler_catalog)
            if unknown_handlers:
                raise ValueError(f"{job} 使用了未声明的执行器：{sorted(unknown_handlers)}")
            for field in ("opening", "team_effects", "enhanced_skill_scope", "holy_relic_skills"):
                values = profile.get(field)
                if values is None:
                    continue
                if not isinstance(values, list) or any(not norm(item) for item in values):
                    raise ValueError(f"{job}.{field} 必须是非空字符串数组")
                if len(values) != len(set(values)):
                    raise ValueError(f"{job}.{field} 存在重复技能")

    @staticmethod
    def _path_value(root: dict[str, Any], path: str, default: Any = None) -> Any:
        value: Any = root
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return copy.deepcopy(default)
            value = value[key]
        return copy.deepcopy(value)

    def global_value(self, path: str, default: Any = None) -> Any:
        return self._path_value(self._global, path, default)

    def job_profile(self, job: Any) -> dict[str, Any]:
        profile = self._jobs.get(norm(job), {})
        return copy.deepcopy(profile) if isinstance(profile, dict) else {}

    def job_value(self, job: Any, path: str, default: Any = None) -> Any:
        profile = self._jobs.get(norm(job), {})
        return self._path_value(profile if isinstance(profile, dict) else {}, path, default)

    def has_handler(self, job: Any, handler: Any) -> bool:
        return norm(handler) in {
            norm(value) for value in self.job_value(job, "runtime_handlers", []) if norm(value)
        }

    def per_job(
        self,
        path: str,
        converter: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for job, profile in self._jobs.items():
            value = self._path_value(profile, path, None)
            if value is None:
                continue
            out[job] = converter(value) if converter else value
        return out

    def per_job_sets(self, path: str) -> dict[str, set[str]]:
        return self.per_job(path, lambda values: {norm(value) for value in values if norm(value)})

    def per_job_tuples(self, path: str) -> dict[str, tuple[str, ...]]:
        return self.per_job(path, lambda values: tuple(norm(value) for value in values if norm(value)))

    def per_job_nested_sets(self, path: str) -> dict[str, dict[str, set[str]]]:
        def convert(mapping: Any) -> dict[str, set[str]]:
            if not isinstance(mapping, dict):
                return {}
            return {
                norm(key): {norm(value) for value in values if norm(value)}
                for key, values in mapping.items()
                if norm(key) and isinstance(values, list)
            }

        return self.per_job(path, convert)

    def per_job_nested_tuples(self, path: str) -> dict[str, dict[str, tuple[str, ...]]]:
        def convert(mapping: Any) -> dict[str, tuple[str, ...]]:
            if not isinstance(mapping, dict):
                return {}
            return {
                norm(key): tuple(norm(value) for value in values if norm(value))
                for key, values in mapping.items()
                if norm(key) and isinstance(values, list)
            }

        return self.per_job(path, convert)

    def job_skill_number_map(self, path: str) -> dict[tuple[str, str], float]:
        rows = self.global_value(path, [])
        out: dict[tuple[str, str], float] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            job = norm(row.get("job"))
            skill = norm(row.get("skill"))
            if job and skill:
                out[(job, skill)] = float(row.get("value", 0.0))
        return out

    def documented_jobs(self) -> tuple[str, ...]:
        return tuple(self._jobs)


JOB_MECHANICS = JobMechanicsRegistry.load()


def action_speed_skill_key(value: Any) -> str:
    """Normalize one action-speed skill while preserving its EX suffix."""

    return re.sub(r"\s*EX$", "EX", norm(value), flags=re.IGNORECASE).strip()


def mechanic_skill_key(value: Any) -> str:
    """Return the shared base-skill key used by mechanic rules."""

    return re.sub(
        r"EX$", "", action_speed_skill_key(value), flags=re.IGNORECASE
    ).strip()


def is_ring_strike(value: Any) -> bool:
    return mechanic_skill_key(value) == "圆环强击"


@lru_cache(maxsize=1)
def ring_strike_reference() -> tuple[int, float]:
    """Shared scoring reference, NOT an in-game duration or Hit ceiling.

    Height-dependent flight is deliberately not reconstructed from damage
    packets. Actual and theoretical rotations use the same reference instead.
    """
    rule = JOB_MECHANICS.global_value("action_speed.ring_strike_reference", {})
    return (
        max(1, int(as_number(rule.get("hits"), 18))),
        max(0.0, as_number(rule.get("start_seconds"), 53.0 / 60.0)),
    )


def ring_strike_total_multiplier(hit_count: int, *, ex: bool = False) -> float:
    """10%, +5 percentage points per Hit, capped at 100%; EX is x1.4.

    Use integer percentage units so the 19th Hit reaches the cap exactly.
    There is intentionally no upper bound on the number of Hits.
    """
    count = max(0, int(hit_count))
    ramp = min(count, 19)
    percent = ramp * 10 + 5 * ramp * (ramp - 1) // 2 + max(0, count - 19) * 100
    return percent / 100.0 * (1.4 if ex else 1.0)


def ring_strike_default_hit_count(action_factor: float = 1.0) -> int:
    reference_hits, _ = ring_strike_reference()
    return max(0, math.floor(reference_hits * max(0.0, action_factor) + 1e-9))


def ring_strike_output_multiplier(action_factor: float = 1.0) -> float:
    reference_hits, _ = ring_strike_reference()
    return ring_strike_total_multiplier(ring_strike_default_hit_count(action_factor)) / ring_strike_total_multiplier(reference_hits)


def ring_strike_reference_start_seconds(cast_time: float) -> float:
    return min(max(0.0, as_number(cast_time)), ring_strike_reference()[1])


def adjust_ring_strike_cast_time(cast_time: float, action_factor: float) -> float:
    """Keep the reference airborne window; speed up only the launch.

    This is a comparison convention for the default ground-height model,
    not a claim that flight always has a fixed duration in the client.
    """
    duration = max(0.0, as_number(cast_time))
    start = ring_strike_reference_start_seconds(duration)
    return duration - start + start / max(1.0, as_number(action_factor, 1.0))


@lru_cache(maxsize=1)
def fixed_duration_action_speed_output_skills() -> frozenset[str]:
    values = JOB_MECHANICS.global_value(
        "action_speed.fixed_duration_output_skills", []
    )
    return frozenset(
        key
        for key in (mechanic_skill_key(value) for value in values)
        if key
    )


def is_fixed_duration_action_speed_output_skill(value: Any) -> bool:
    return mechanic_skill_key(value) in fixed_duration_action_speed_output_skills()


@lru_cache(maxsize=256)
def stance_guaranteed_damage_fraction(job: str, skill: str) -> float:
    """Return the fixed, always-dealt share of a stance skill's base panel."""

    rules = JOB_MECHANICS.job_value(job, "stance_skills", {})
    if not isinstance(rules, dict):
        return 0.0
    skill_key = mechanic_skill_key(skill)
    for name, rule in rules.items():
        if mechanic_skill_key(name) != skill_key or not isinstance(rule, dict):
            continue
        base_multiplier = max(0.0, as_number(rule.get("base_multiplier")))
        fixed_multiplier = max(0.0, as_number(rule.get("fixed_multiplier")))
        if base_multiplier <= 1e-12 or fixed_multiplier <= 0.0:
            return 0.0
        return min(1.0, fixed_multiplier / base_multiplier)
    return 0.0


def stance_damage_fraction_for_utilization(
    job: str,
    skill: str,
    utilization_ratio: float,
    full_output_multiplier: float = 1.0,
) -> float:
    """Prorate a stance while retaining its fixed hit as a damage floor.

    ``full_output_multiplier`` is the complete stance output after action-speed
    hit-count changes, relative to the no-speed panel.  The returned value is a
    fraction of that already-adjusted full output, so callers can multiply it
    directly without action speed incorrectly amplifying the fixed hit.
    """

    utilized = max(0.0, min(1.0, as_number(utilization_ratio)))
    guaranteed = stance_guaranteed_damage_fraction(norm(job), mechanic_skill_key(skill))
    if guaranteed <= 0.0:
        return utilized
    full_output = max(
        guaranteed,
        max(0.0, as_number(full_output_multiplier, 1.0)),
    )
    if full_output <= 1e-12:
        return 0.0
    retained_output = guaranteed + (full_output - guaranteed) * utilized
    return max(0.0, min(1.0, retained_output / full_output))


@lru_cache(maxsize=1)
def fixed_duration_action_speed_cast_segments() -> dict[str, dict[str, float]]:
    """Return the accelerable phases of fixed Start+Loop+End skills.

    Only the Start phase is accelerated.  ``ex_base_duration_seconds`` marks
    the normal version's total duration; any extra duration in the EX version
    is accelerated together with Start.
    """

    payload = JOB_MECHANICS.global_value(
        "action_speed.fixed_duration_cast_segments", {}
    )
    result: dict[str, dict[str, float]] = {}
    if not isinstance(payload, dict):
        return result
    for skill, rule in payload.items():
        key = mechanic_skill_key(skill)
        if not key or not isinstance(rule, dict):
            continue
        start_seconds = max(0.0, as_number(rule.get("start_seconds")))
        ex_base_duration = max(
            0.0, as_number(rule.get("ex_base_duration_seconds"))
        )
        if start_seconds <= 0:
            continue
        result[key] = {
            "start_seconds": start_seconds,
            "ex_base_duration_seconds": ex_base_duration,
        }
    return result


def fixed_duration_action_speed_accelerated_seconds(
    value: Any,
    cast_time: float,
) -> float:
    """Return the part of a fixed cast that is affected by action speed."""

    duration = max(0.0, as_number(cast_time))
    rule = fixed_duration_action_speed_cast_segments().get(
        mechanic_skill_key(value), {}
    )
    start_seconds = max(0.0, as_number(rule.get("start_seconds")))
    ex_base_duration = max(
        0.0, as_number(rule.get("ex_base_duration_seconds"))
    )
    ex_extension = (
        max(0.0, duration - ex_base_duration)
        if ex_base_duration > 0
        else 0.0
    )
    return min(duration, start_seconds + ex_extension)


def adjust_fixed_duration_action_speed_cast_time(
    value: Any,
    cast_time: float,
    action_factor: float,
) -> float:
    """Accelerate Start/EX extension while keeping Loop+End fixed."""

    duration = max(0.0, as_number(cast_time))
    if duration <= 0 or not is_fixed_duration_action_speed_output_skill(value):
        return duration
    accelerated = fixed_duration_action_speed_accelerated_seconds(value, duration)
    if accelerated <= 0:
        return duration
    factor = max(1.0, as_number(action_factor, 1.0))
    return duration - accelerated + accelerated / factor
