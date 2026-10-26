from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from statistics import median
from typing import Any, Iterable

from ..data_loader import DATA_DIR, as_number, load_json, norm
from .event_platform import event_platform_for
from .scoring import TeamScoreEngine, _COMBAT_MODIFIER_RULES, canonical_skill, data_job_for
from .timeline_evidence import is_authoritative_timeline_cast
from .cooldown_glyph_evidence import (
    CooldownClock, cooldown_plate_evidence, skill_cooldown_mechanics,
)


COMBAT_GLYPH_INFERENCE_VERSION = "combat-glyph-inference-v6-segmented-stock"
_MAX_EQUIPPED_GLYPHS = 7
_MIN_APPLIED_CONFIDENCE = 0.9
_MIN_MEASURABLE_COOLDOWN_REDUCTION_SECONDS = 0.25
# Basic evade/recovery skills are controlled by player input, hit reactions,
# animation cancellation and movement state.  Their cast gaps are not a
# server-declared cooldown measurement, even when several gaps happen to land
# on a glyph-adjusted value.  Keep them unknown unless a future packet source
# exposes the equipped cooldown glyph directly.
_TIMING_COOLDOWN_EXCLUDED_SKILL_IDS = frozenset(
    {
        "11",    # Warrior: Roll
        "1013",  # Archer: Roll
        "1023",  # Archer: Urgency Trick
        "1024",  # Archer: Aerial Evasion
        "2012",  # Sorceress: Blink
        "2023",  # Sorceress: Escape
        "2024",  # Sorceress: Aerial Evasion
        "3020",  # Cleric: Sliding Step
        "3024",  # Cleric: Aerial Evasion
        "4003",  # Academic: Dodge
        "5010",  # Kali: Evasive Step
        "5015",  # Kali: Aerial Evasion
        "6012",  # Assassin: Roll
        "6017",  # Assassin: Aerial Evasion
        "1616",  # Hunter: Hunter Roll
        "1702",  # Silver Hunter: Instant Dodge
    }
)
_TIMING_COOLDOWN_EXCLUDED_SKILL_NAMES = frozenset(
    {
        "翻滚",
        "疾闪",
        "浮空调整",
        "闪现",
        "逃脱",
        "侧滑",
        "闪避",
        "回旋脚步",
        "猎人翻滚",
        "瞬间回避",
    }
)
_WIRE_EFFECT_CLASSES = {
    "伤害增加": 50,
    "动作速度增加": 25,
}
# Chakra Cure already owns effect class 25 in its base SkillTable row, so a
# class-25 response cannot independently prove its action-speed glyph.
_WIRE_CLASS_COLLISIONS = {("动作速度增加", "6302")}
_APPLICATION_RATIO_BASELINES_PATH = (
    DATA_DIR
    / "profiles"
    / "skill_application_ratio_baselines.json"
)
_ROTATION_CDR_BUFF_MARKERS = (
    "精神爆发",
    "灼热火焰",
    "时间加速",
    "鸡尾酒",
    "奇迹之轮",
    "冷却缩减",
)


def _effect_name(value: object) -> str:
    name = norm(value)
    if name in {"动作速度", "动作速度增加"}:
        return "动作速度增加"
    if name in {"增益持续", "增益持续时间增加"}:
        return "增益持续时间增加"
    if name in {
        "攻击力应用比率增加",
        "攻击力应用比例增加",
        "攻击力应用比率",
        "攻击力应用比例",
    }:
        return "攻击力应用比例增加"
    return name


def _numbers(value: object) -> tuple[float, ...]:
    result: list[float] = []
    for token in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value or "")):
        try:
            number = float(token)
        except ValueError:
            continue
        if math.isfinite(number):
            result.append(number)
    return tuple(result)


def _candidate_rows(store: Any, job: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for glyph in store.skill_glyphs(data_job_for(job)):
        skill = norm(glyph.get("skill"))
        skill_id = str(glyph.get("skill_id") or "").strip()
        if not skill:
            continue
        for raw_effect, values in (glyph.get("effects") or {}).items():
            effect = _effect_name(raw_effect)
            if effect not in {
                "伤害增加",
                "冷却缩减",
                "增益持续时间增加",
                "动作速度增加",
                "攻击力应用比例增加",
            }:
                continue
            for option in values if isinstance(values, list) else ():
                if not isinstance(option, dict):
                    continue
                value = max(0.0, as_number(option.get("value")))
                if value <= 0:
                    continue
                result.append(
                    {
                        "skill": skill,
                        "skill_key": canonical_skill(skill),
                        "skill_id": skill_id,
                        "effect": effect,
                        "value": value,
                        "option": norm(option.get("grade")),
                    }
                )
    return result


def _event_matches(candidate: dict[str, Any], event: object) -> bool:
    ids = {
        str(getattr(event, "skill_id", "") or "").strip(),
        str(getattr(event, "raw_skill_id", "") or "").strip(),
    }
    if candidate["skill_id"] and candidate["skill_id"] in ids:
        return True
    return canonical_skill(getattr(event, "skill_name", "")) == candidate[
        "skill_key"
    ]


def _timing_cooldown_inference_allowed(candidate: dict[str, Any]) -> bool:
    """Whether cast spacing can be treated as cooldown-glyph evidence."""

    skill_id = str(candidate.get("skill_id") or "").strip()
    if skill_id in _TIMING_COOLDOWN_EXCLUDED_SKILL_IDS:
        return False
    return norm(candidate.get("skill")) not in _TIMING_COOLDOWN_EXCLUDED_SKILL_NAMES


def _row(
    candidate: dict[str, Any],
    *,
    source: str,
    confidence: float,
    evidence_count: int,
    details: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "skill": candidate["skill"],
        "skill_id": candidate["skill_id"],
        "effect": candidate["effect"],
        "value": candidate["value"],
        "option": candidate["option"],
        "inference_source": source,
        "inference_confidence": round(max(0.0, min(1.0, confidence)), 4),
        "evidence_count": max(0, int(evidence_count)),
        "evidence": list(details),
    }


def _wire_rows(
    candidates: list[dict[str, Any]],
    parameter_events: tuple[object, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        effect_class = _WIRE_EFFECT_CLASSES.get(candidate["effect"])
        if effect_class is None or (
            candidate["effect"], candidate["skill_id"]
        ) in _WIRE_CLASS_COLLISIONS:
            continue
        expected = candidate["value"] / 100.0
        matches = [
            event
            for event in parameter_events
            if int(getattr(event, "effect_class_id", 0) or 0) == effect_class
            and _event_matches(candidate, event)
            and any(
                abs(number - expected) <= 0.0005
                for number in _numbers(getattr(event, "parameters", ""))
            )
        ]
        if not matches:
            continue
        result.append(
            _row(
                candidate,
                source="server_effect_class",
                confidence=1.0,
                evidence_count=len(matches),
                details=(
                    f"effect_class={effect_class}",
                    f"parameter={expected:g}",
                ),
            )
        )
    return result


def _duration_rows(
    store: Any,
    job: str,
    candidates: list[dict[str, Any]],
    parameter_events: tuple[object, ...],
) -> list[dict[str, Any]]:
    def base_durations(candidate: dict[str, Any], event: object) -> tuple[float, ...]:
        try:
            levels = store.full_skill_levels(
                data_job_for(job), candidate["skill"], max_required_level=150
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            levels = []
        row_id = int(getattr(event, "skill_level_row_id", 0) or 0)
        exact = [
            row
            for row in levels
            if int(as_number(row.get("RowId"))) == row_id
        ]
        selected_levels = exact or levels
        durations = {
            as_number(row.get(f"_EffectClassValue{slot}Duration")) / 1000.0
            for row in selected_levels
            if isinstance(row, dict)
            for slot in range(1, 11)
            if 0.05
            <= as_number(row.get(f"_EffectClassValue{slot}Duration")) / 1000.0
            <= 3600.0
        }
        # Compatibility fallback for data sources that expose descriptions but
        # not raw effect-duration columns. Preserve *every* seconds-valued
        # duration in the description: Frozen Barrier contains both a 2.5 s
        # freeze and a 25 s self Buff, while ``skill_effect_timing`` can expose
        # only the first one. The observed server duration disambiguates them.
        if not durations:
            timing = store.skill_effect_timing(
                data_job_for(job), candidate["skill"]
            )
            for token in re.findall(
                r"(?<![\d.])(\d+(?:\.\d+)?)\s*秒",
                str(timing.get("desc") or ""),
            ):
                prose_duration = as_number(token)
                if 0.05 <= prose_duration <= 3600.0:
                    durations.add(prose_duration)
            fallback = max(0.0, as_number(timing.get("duration")))
            if fallback > 0:
                durations.add(fallback)
        return tuple(sorted(durations))

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["effect"] != "增益持续时间增加":
            continue
        matches: list[tuple[object, float, float]] = []
        for event in parameter_events:
            if bool(getattr(event, "permanent", False)) or not _event_matches(
                candidate, event
            ):
                continue
            observed = max(
                0, int(getattr(event, "duration_ms", 0) or 0)
            ) / 1000.0
            if observed <= 0:
                continue
            for base_duration in base_durations(candidate, event):
                expected = base_duration * (1.0 + candidate["value"] / 100.0)
                if abs(observed - expected) <= max(0.08, expected * 0.0125):
                    matches.append((event, base_duration, observed))
                    break
        if not matches:
            continue
        representative_base = median(row[1] for row in matches)
        representative_observed = median(row[2] for row in matches)
        result.append(
            _row(
                candidate,
                source="server_declared_duration",
                confidence=(0.97 if len(matches) >= 2 else 0.9),
                evidence_count=len(matches),
                details=(
                    f"base={representative_base:g}s",
                    f"observed={representative_observed:g}s",
                ),
            )
        )
    return result


def _application_ratio_rows(
    store: Any,
    job: str,
    candidates: list[dict[str, Any]],
    parameter_events: tuple[object, ...],
) -> list[dict[str, Any]]:
    """Prove additive attack-application plates from the effective parameter.

    These plates do not emit the class-50 response used by normal damage
    plates.  The server instead reports the effective attack-application
    parameter for the active SkillLevel row.  A glyph is confirmed only when
    the observed value differs from a numeric value in that *exact* row by the
    advertised 0.05/0.10/0.20 amount.  Equality with the DNT value is explicit
    negative evidence and is never treated as a plate.
    """

    payload = (
        load_json(_APPLICATION_RATIO_BASELINES_PATH)
        if _APPLICATION_RATIO_BASELINES_PATH.exists()
        else {}
    )
    baseline_rows = payload.get("rows", {}) if isinstance(payload, dict) else {}

    def base_parameter(
        candidate: dict[str, Any], event: object
    ) -> tuple[int, float] | None:
        row_id = int(getattr(event, "skill_level_row_id", 0) or 0)
        if row_id <= 0:
            return None
        if not isinstance(baseline_rows, dict):
            return None
        catalog_row = baseline_rows.get(str(row_id), {})
        if (
            not isinstance(catalog_row, dict)
            or int(as_number(catalog_row.get("skill_id")))
            != int(as_number(candidate.get("skill_id")))
        ):
            return None
        effect_class_id = int(
            as_number(catalog_row.get("ratio_effect_class_id"))
        )
        value = as_number(catalog_row.get("ratio_value"), math.nan)
        if effect_class_id <= 0 or not math.isfinite(value):
            return None
        return effect_class_id, value

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["effect"] != "攻击力应用比例增加":
            continue
        delta = candidate["value"] / 100.0
        matches: list[tuple[float, float]] = []
        for event in parameter_events:
            if not _event_matches(candidate, event):
                continue
            baseline = base_parameter(candidate, event)
            if baseline is None:
                continue
            expected_effect_class, base = baseline
            if int(getattr(event, "effect_class_id", 0) or 0) != expected_effect_class:
                continue
            observed_values = _numbers(getattr(event, "parameters", ""))
            for observed in observed_values:
                if abs(observed - base - delta) <= 0.0005:
                    matches.append((base, observed))
                    break
        if not matches:
            continue
        representative_base = median(row[0] for row in matches)
        representative_observed = median(row[1] for row in matches)
        result.append(
            _row(
                candidate,
                source="server_attack_application_ratio",
                confidence=(0.99 if len(matches) >= 2 else 0.92),
                evidence_count=len(matches),
                details=(
                    f"base={representative_base:g}",
                    f"observed={representative_observed:g}",
                    f"additive={delta:g}",
                ),
            )
        )
    return result


def _interval_overlaps(
    start_seconds: float,
    end_seconds: float,
    intervals: tuple[tuple[float, float], ...],
) -> bool:
    return any(
        max(start_seconds, left) < min(end_seconds, right)
        for left, right in intervals
    )


def _external_cdr_intervals(snapshot: object, source_id: str, *, only_unmodelled: bool = False) -> tuple[tuple[float, float], ...]:
    rows: list[tuple[float, float]] = []
    for series in getattr(snapshot, "player_buff_series", ()):
        if str(getattr(series, "target_id", "")) != source_id:
            continue
        if only_unmodelled and canonical_skill(getattr(series, "skill_name", "")) in _COMBAT_MODIFIER_RULES:
            # The score resolver already validates finite CDR windows and
            # clips Buff clears. Long resource/status rows of that same skill
            # are not an additional unknown CDR effect lasting all combat.
            continue
        label = " ".join(
            (
                str(getattr(series, "skill_name", "") or ""),
                str(getattr(series, "label", "") or ""),
                str(getattr(series, "key", "") or ""),
            )
        )
        if not any(marker in label for marker in _ROTATION_CDR_BUFF_MARKERS):
            continue
        rows.extend(
            (max(0.0, float(start)), max(float(start), float(end)))
            for start, end in getattr(series, "intervals", ())
            if float(end) > float(start)
        )
    return tuple(sorted(set(rows)))


def _cooldown_rows(
    store: Any,
    snapshot: object,
    source_id: str,
    job: str,
    candidates: list[dict[str, Any]],
    cast_events: tuple[object, ...],
    *,
    audit_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    windows = TeamScoreEngine._combat_modifier_windows(snapshot, source_id)
    clock = CooldownClock(windows)
    unknown_windows = _external_cdr_intervals(snapshot, source_id, only_unmodelled=True)
    reset_times = sorted({max(0, int(getattr(row, "timestamp_ms", 0))) / 1000.0
                          for row in getattr(snapshot, "player_lifecycle_events", ())
                          if str(getattr(row, "target_id", "")) == source_id})
    tree = store.skill_build_tree(data_job_for(job), 70)
    # The user selects only the highest tier; smaller tiers are not separate
    # timing hypotheses and must not steal observations from the same skill.
    highest: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if candidate["effect"] != "冷却缩减":
            continue
        if not _timing_cooldown_inference_allowed(candidate):
            continue
        current = highest.get(candidate["skill_key"])
        if current is None or candidate["value"] > current["value"]:
            highest[candidate["skill_key"]] = candidate
    for candidate in highest.values():
        timestamps = sorted(
            {
                max(0, int(getattr(event, "timestamp_ms", 0) or 0)) / 1000.0
                for event in cast_events
                if is_authoritative_timeline_cast(event)
                and _event_matches(candidate, event)
            }
        )
        if len(timestamps) < 2:
            continue
        mechanics = skill_cooldown_mechanics(store, data_job_for(job), candidate, tree)
        evidence = cooldown_plate_evidence(candidate, timestamps, mechanics, clock,
                                          reset_times, unknown_windows)
        if audit_rows is not None:
            audit_rows.append(evidence)
        if not evidence["inferred"]:
            continue
        result.append(
            _row(
                candidate,
                source=("charge_recharge_cooldown" if mechanics["capacity"] > 1
                        else "authoritative_cast_cooldown"),
                confidence=evidence["confidence"],
                evidence_count=evidence["evidence_count"],
                details=(
                    f"base={mechanics['base_cooldown']:g}s",
                    f"expected={evidence['expected_cooldown']:g}s",
                    f"median_error={evidence['median_error']:.3f}s",
                    "segmented_cooldown_work",
                    f"capacity={mechanics['capacity']}",
                    f"recharge_model={evidence['recharge_model']}",
                    f"recharge_matches={evidence['recharge_match_count']}",
                ),
            )
        )
    return result


def _select_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    # One skill can equip only one glyph option. Prefer exact wire evidence,
    # then declared server duration, then the conservative cooldown cluster.
    priority = {
        "server_effect_class": 4,
        "server_attack_application_ratio": 3,
        "server_declared_duration": 2,
        "authoritative_cast_cooldown": 1,
        "charge_recharge_cooldown": 1,
    }
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Timing clusters are useful but can occur by chance when a player did
        # not press a skill exactly on cooldown.  Only evidence at or above the
        # application threshold is allowed to affect either score axis; lower
        # confidence observations remain unknown instead of becoming a fake
        # equipped glyph.
        if as_number(row.get("inference_confidence")) < _MIN_APPLIED_CONFIDENCE:
            continue
        key = canonical_skill(row.get("skill"))
        current = best.get(key)
        rank = (
            priority.get(str(row.get("inference_source")), 0),
            as_number(row.get("inference_confidence")),
            int(row.get("evidence_count", 0) or 0),
        )
        current_rank = (
            priority.get(str(current.get("inference_source")), 0),
            as_number(current.get("inference_confidence")),
            int(current.get("evidence_count", 0) or 0),
        ) if current is not None else (-1, -1.0, -1)
        if rank > current_rank:
            best[key] = row
    return sorted(
        best.values(),
        key=lambda row: (
            -priority.get(str(row.get("inference_source")), 0),
            -as_number(row.get("inference_confidence")),
            canonical_skill(row.get("skill")),
        ),
    )[:_MAX_EQUIPPED_GLYPHS]


def infer_combat_skill_glyphs(
    snapshot: object,
    store: Any,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Score preparation only: infer from a snapshot, never inside a reader."""

    began = time.perf_counter()
    platform = event_platform_for(snapshot)
    parameter_events = tuple(platform.stream("skill_parameter"))
    cast_events = tuple(platform.stream("skill_cast"))
    rows_by_source: dict[str, list[dict[str, Any]]] = {}
    reports: dict[str, Any] = {}
    for player in getattr(snapshot, "players", ()):
        source_id = str(getattr(player, "source_id", ""))
        job = norm(getattr(player, "job", ""))
        candidates = _candidate_rows(store, job)
        source_parameters = tuple(
            event
            for event in parameter_events
            if str(getattr(event, "source_id", "")) == source_id
        )
        source_casts = tuple(
            event
            for event in cast_events
            if str(getattr(event, "source_id", "")) == source_id
        )
        cooldown_audit: list[dict[str, Any]] = []
        cooldown_began = time.perf_counter()
        cooldown_rows = _cooldown_rows(store, snapshot, source_id, job, candidates,
                                       source_casts, audit_rows=cooldown_audit)
        cooldown_elapsed_ms = (time.perf_counter() - cooldown_began) * 1000
        selected = _select_rows(
            [
                *_wire_rows(candidates, source_parameters),
                *_application_ratio_rows(
                    store, job, candidates, source_parameters
                ),
                *_duration_rows(store, job, candidates, source_parameters),
                *cooldown_rows,
            ]
        )
        rows_by_source[source_id] = selected
        timing_cooldown_exclusions = sorted(
            {
                (candidate["skill"], candidate["skill_id"])
                for candidate in candidates
                if candidate["effect"] == "冷却缩减"
                and not _timing_cooldown_inference_allowed(candidate)
                and any(_event_matches(candidate, event) for event in source_casts)
            }
        )
        reports[source_id] = {
            "name": str(getattr(player, "name", "") or source_id),
            "job": job,
            "glyphs": selected,
            "inferred_count": len(selected),
            "candidate_skill_count": len(
                {candidate["skill_key"] for candidate in candidates}
            ),
            "parameter_event_count": len(source_parameters),
            "authoritative_cast_count": sum(
                is_authoritative_timeline_cast(event)
                for event in source_casts
            ),
            "timing_cooldown_exclusions": [
                {
                    "skill": skill,
                    "skill_id": skill_id,
                    "reason": "basic_evade_cast_spacing_is_not_equipment_evidence",
                }
                for skill, skill_id in timing_cooldown_exclusions
            ],
            "cooldown_evidence": cooldown_audit,
            "cooldown_analysis_ms": round(cooldown_elapsed_ms, 3),
            "unknown_slots": max(0, 4 - len(selected)),
        }
    return rows_by_source, {
        "version": COMBAT_GLYPH_INFERENCE_VERSION,
        "source": "combat_only",
        "execution_stage": "score_preparation",
        "elapsed_ms": round((time.perf_counter() - began) * 1000, 3),
        "cooldown_analysis_ms": round(sum(row["cooldown_analysis_ms"]
                                          for row in reports.values()), 3),
        "players": reports,
        "inferred_total": sum(len(rows) for rows in rows_by_source.values()),
        "parameter_event_total": len(parameter_events),
    }


def combat_glyph_signature(
    rows_by_source: dict[str, list[dict[str, Any]]],
) -> tuple[tuple[str, tuple[tuple[str, str, float], ...]], ...]:
    return tuple(
        (
            source_id,
            tuple(
                (
                    canonical_skill(row.get("skill")),
                    norm(row.get("effect")),
                    round(as_number(row.get("value")), 6),
                )
                for row in rows
                if isinstance(row, dict)
            ),
        )
        for source_id, rows in sorted(rows_by_source.items())
    )
