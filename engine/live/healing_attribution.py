from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

from ..data_loader import DATA_DIR
from .models import (
    CombatEvent,
    PlayerHealthEvent,
    PlayerLifecycleEvent,
    SkillCastEvent,
)
from .timeline_evidence import is_timeline_recovered_cast


HEALING_MODEL_SCHEMA = "dragon-nest-healing-skill-models-v1"
UNKNOWN_HEALING_SOURCE_ID = "healing:unknown"
UNKNOWN_HEALING_SKILL_ID = "healing:unattributed"
UNKNOWN_HEALING_SKILL_NAME = "未归因治疗"
# Confirmed protection-only skills must not survive a legacy raw-name fallback.
NON_HEALING_SKILL_KEYS = frozenset({"奇迹之手"})
_ABSOLUTE_HP_PACKET_KIND = "s2c-udp-player-hp-state-086f-v576"
_GENERIC_OR_UNKNOWN_JOBS = frozenset(
    {
        "",
        "未知",
        "未知职业",
        "战士",
        "战神",
        "牧师",
        "祭司",
        "学者",
        "炼金术师",
        "刺客",
        "使者",
        "舞娘",
        "呐喊者",
        "复仇者",
    }
)


def _lifecycle_transitions_by_target(
    lifecycle_events: Iterable[PlayerLifecycleEvent],
    states_by_target: Mapping[str, list[PlayerHealthEvent]],
) -> dict[str, tuple[PlayerLifecycleEvent, ...]]:
    """Collapse retransmitted 05 11 records to real inactive/active edges.

    The protocol deliberately exposes every wire record.  A repeated
    ``active=True`` packet is not a revival boundary and must not invalidate
    healing observed between adjacent 08 6F snapshots.  The most recent HP
    state before the first lifecycle record supplies the initial state when
    available; otherwise visible party members start as active.
    """

    indexed_by_target: dict[
        str, list[tuple[int, PlayerLifecycleEvent]]
    ] = defaultdict(list)
    for index, event in enumerate(lifecycle_events):
        target_id = str(event.target_id or "")
        if target_id:
            indexed_by_target[target_id].append((index, event))
    result: dict[str, tuple[PlayerLifecycleEvent, ...]] = {}
    for target_id, indexed in indexed_by_target.items():
        indexed.sort(key=lambda row: (row[1].timestamp_ms, row[0]))
        first_timestamp = indexed[0][1].timestamp_ms
        prior_states = sorted(
            (
            state
            for state in states_by_target.get(target_id, ())
            if state.timestamp_ms <= first_timestamp
            ),
            key=lambda state: state.timestamp_ms,
        )
        active = (
            prior_states[-1].current_hp > 0
            if prior_states
            else True
        )
        transitions: list[PlayerLifecycleEvent] = []
        for _index, event in indexed:
            next_active = bool(event.active)
            if next_active == active:
                continue
            transitions.append(event)
            active = next_active
        if transitions:
            result[target_id] = tuple(transitions)
    return result


def healing_skill_key(value: object) -> str:
    text = str(value or "").strip().upper().replace("ＥＸ", "EX")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(?:EX|觉醒)$", "", text)
    return text


@dataclass(frozen=True, slots=True)
class HealingSkillModel:
    name: str
    aliases: tuple[str, ...]
    jobs: tuple[str, ...]
    model_type: str
    target_scope: str
    min_delay_ms: int
    max_delay_ms: int
    duration_ms: int
    tick_interval_ms: int
    tick_jitter_ms: int
    max_targets_per_tick: int
    confidence: float

    def keys(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in {
                healing_skill_key(self.name),
                *(healing_skill_key(alias) for alias in self.aliases),
            }
            if key
        )

    def active_window_ms(self) -> int:
        if self.model_type in {"field", "periodic", "summon", "channel"}:
            return max(0, self.max_delay_ms) + max(0, self.duration_ms)
        return max(
            self.max_delay_ms,
            self.duration_ms,
            self.tick_interval_ms,
            500,
        )


@dataclass(frozen=True, slots=True)
class HealingCastInstance:
    instance_id: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    skill_key: str
    start_ms: int
    end_ms: int
    model: HealingSkillModel


@dataclass(frozen=True, slots=True)
class HealingAttributionResult:
    events: tuple[CombatEvent, ...]
    instances: tuple[HealingCastInstance, ...]
    raw_packet_total: int
    factual_healing_total: int
    attributed_healing_total: int
    unattributed_healing_total: int
    suppressed_overheal_total: int
    matched_events: int
    unattributed_events: int
    ambiguous_events: int
    method_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @property
    def balanced(self) -> bool:
        return (
            sum(max(0, event.amount) for event in self.events)
            == self.factual_healing_total
            == self.attributed_healing_total + self.unattributed_healing_total
        )


def _int_pair(value: object, default: tuple[int, int]) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return default
    try:
        start = int(value[0])
        end = int(value[1])
    except (TypeError, ValueError):
        return default
    return min(start, end), max(start, end)


@lru_cache(maxsize=4)
def load_healing_skill_models(
    path: Path | None = None,
) -> tuple[HealingSkillModel, ...]:
    source = Path(
        path
        or DATA_DIR / "profiles" / "healing_skill_models.json"
    )
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("schema") != HEALING_MODEL_SCHEMA:
        return ()
    rows = payload.get("skills", ()) if isinstance(payload, dict) else ()
    result: list[HealingSkillModel] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            continue
        delay = _int_pair(row.get("delay_ms"), (0, 3_000))
        result.append(
            HealingSkillModel(
                name=str(row.get("name") or "").strip(),
                aliases=tuple(
                    str(value).strip()
                    for value in row.get("aliases", ())
                    if str(value).strip()
                ),
                jobs=tuple(
                    str(value).strip()
                    for value in row.get("jobs", ())
                    if str(value).strip()
                ),
                model_type=str(row.get("model_type") or "direct").strip().casefold(),
                target_scope=str(row.get("target_scope") or "party").strip().casefold(),
                min_delay_ms=delay[0],
                max_delay_ms=delay[1],
                duration_ms=max(0, int(row.get("duration_ms") or 0)),
                tick_interval_ms=max(0, int(row.get("tick_interval_ms") or 0)),
                tick_jitter_ms=max(50, int(row.get("tick_jitter_ms") or 350)),
                max_targets_per_tick=max(
                    1, int(row.get("max_targets_per_tick") or 8)
                ),
                confidence=max(
                    0.0, min(1.0, float(row.get("confidence") or 0.85))
                ),
            )
        )
    return tuple(result)


class HealingAttributionLedger:
    """Attribute factual positive HP changes without touching damage ledgers.

    The factual layer is built from adjacent absolute ``08 6F`` states. Raw
    positive HP-delta packets identify candidate moments and cap observable
    healing, while revival boundaries are excluded. The skill layer then
    matches each factual event against explicit per-skill timing models.
    Ambiguous events remain in an unassigned bucket instead of being credited
    to the capture observer or simply to the most recent skill.
    """

    def __init__(
        self,
        models: Iterable[HealingSkillModel] | None = None,
    ) -> None:
        self.models = tuple(models) if models is not None else load_healing_skill_models()
        self._models_by_key: dict[str, HealingSkillModel] = {}
        for model in self.models:
            for key in model.keys():
                self._models_by_key[key] = model

    def model_for(self, skill_name: object) -> HealingSkillModel | None:
        key = healing_skill_key(skill_name)
        return None if key in NON_HEALING_SKILL_KEYS else self._models_by_key.get(key)

    @staticmethod
    def _model_accepts_job(model: HealingSkillModel, job: object) -> bool:
        job_text = re.sub(r"\s+", "", str(job or "").strip())
        if not model.jobs or job_text in _GENERIC_OR_UNKNOWN_JOBS:
            return True
        return any(
            job_text == allowed
            or job_text.endswith(allowed)
            for allowed in model.jobs
        )

    def _cast_instances(
        self,
        cast_events: Iterable[SkillCastEvent],
        actor_jobs_by_source: Mapping[str, str],
    ) -> tuple[HealingCastInstance, ...]:
        instances: list[HealingCastInstance] = []
        last_cast_by_key: dict[tuple[str, str], int] = {}
        ordinals: dict[tuple[str, str], int] = defaultdict(int)
        ordered_casts = tuple(
            sorted(
                cast_events,
                key=lambda row: (
                    row.timestamp_ms,
                    row.source_id,
                    row.skill_id,
                ),
            )
        )
        action_times_by_source: dict[str, list[int]] = defaultdict(list)
        for cast in ordered_casts:
            if is_timeline_recovered_cast(cast):
                continue
            action_times_by_source[cast.source_id].append(cast.timestamp_ms)
        for cast in ordered_casts:
            model = self.model_for(cast.skill_name)
            if model is None or not cast.source_id:
                continue
            effective_job = str(
                actor_jobs_by_source.get(cast.source_id) or cast.job or ""
            ).strip()
            if not self._model_accepts_job(model, effective_job):
                continue
            skill_key = healing_skill_key(model.name)
            key = (cast.source_id, skill_key)
            previous_ms = last_cast_by_key.get(key)
            if previous_ms is not None and cast.timestamp_ms - previous_ms <= 150:
                continue
            last_cast_by_key[key] = cast.timestamp_ms
            ordinals[key] += 1
            active_ms = model.active_window_ms()
            end_ms = cast.timestamp_ms + active_ms
            if model.model_type == "channel":
                source_action_times = action_times_by_source.get(
                    cast.source_id, ()
                )
                next_index = bisect_right(
                    source_action_times,
                    cast.timestamp_ms + 150,
                )
                if next_index < len(source_action_times):
                    # A real following action ends an interrupted channel.
                    # Damage-inferred pseudo casts are excluded above so a
                    # channel's own healing/damage ticks cannot cancel it.
                    end_ms = min(
                        end_ms,
                        max(
                            cast.timestamp_ms + 250,
                            source_action_times[next_index],
                        ),
                    )
            instances.append(
                HealingCastInstance(
                    instance_id=(
                        f"heal:{cast.source_id}:{skill_key}:"
                        f"{cast.timestamp_ms}:{ordinals[key]}"
                    ),
                    source_id=cast.source_id,
                    source_name=cast.source_name,
                    job=effective_job,
                    skill_id=cast.skill_id,
                    skill_name=model.name,
                    skill_key=skill_key,
                    start_ms=cast.timestamp_ms,
                    end_ms=end_ms,
                    model=model,
                )
            )
        return tuple(instances)

    @staticmethod
    def _allocate_budget(
        events: list[CombatEvent], budget: int
    ) -> tuple[list[int], int]:
        raw_total = sum(max(0, event.amount) for event in events)
        budget = max(0, min(int(budget), raw_total))
        if raw_total <= 0 or budget <= 0:
            return [0] * len(events), raw_total
        floors: list[int] = []
        fractions: list[tuple[float, int]] = []
        for index, event in enumerate(events):
            exact = budget * max(0, event.amount) / raw_total
            floor_value = int(exact)
            floors.append(floor_value)
            fractions.append((exact - floor_value, index))
        remainder = budget - sum(floors)
        for _fraction, index in sorted(
            fractions,
            key=lambda row: (row[0], -row[1]),
            reverse=True,
        )[:remainder]:
            floors[index] += 1
        return floors, raw_total - budget

    @staticmethod
    def _unknown_event(
        timestamp_ms: int,
        amount: int,
        target: PlayerHealthEvent | CombatEvent,
        method: str,
    ) -> CombatEvent:
        return CombatEvent(
            timestamp_ms=max(0, int(timestamp_ms)),
            source_id=UNKNOWN_HEALING_SOURCE_ID,
            source_name="未知治疗来源",
            job="未知职业",
            skill_id=UNKNOWN_HEALING_SKILL_ID,
            skill_name=UNKNOWN_HEALING_SKILL_NAME,
            amount=max(0, int(amount)),
            healing=True,
            target_id=str(target.target_id),
            target_name=str(target.target_name),
            target_job=str(target.target_job),
            target_max_hp=max(0, int(getattr(target, "max_hp", 0) or 0)),
            attribution_method=method,
            raw_attribution_method=str(getattr(target, "packet_kind", "") or ""),
            attribution_confidence=0.0,
        )

    def _factual_events(
        self,
        raw_events: tuple[CombatEvent, ...],
        health_events: tuple[PlayerHealthEvent, ...],
        lifecycle_events: tuple[PlayerLifecycleEvent, ...],
        party_ids: frozenset[str] | None,
        excluded_hp_rise_intervals_by_target: Mapping[
            str, tuple[tuple[int, int], ...]
        ],
    ) -> tuple[tuple[CombatEvent, ...], int]:
        raw_by_target: dict[str, list[CombatEvent]] = defaultdict(list)
        targetless_raw: list[CombatEvent] = []
        for event in raw_events:
            if (
                event.healing
                and event.amount > 0
                and not event.target_id
                and (party_ids is None or event.source_id in party_ids)
            ):
                targetless_raw.append(event)
                continue
            if (
                event.healing
                and event.amount > 0
                and event.target_id
                and (party_ids is None or event.target_id in party_ids)
            ):
                raw_by_target[event.target_id].append(event)
        states_by_target: dict[str, list[PlayerHealthEvent]] = defaultdict(list)
        for event in health_events:
            if (
                event.current_hp >= 0
                and event.target_id
                and event.packet_kind == _ABSOLUTE_HP_PACKET_KIND
                and (party_ids is None or event.target_id in party_ids)
            ):
                states_by_target[event.target_id].append(event)
        reliable_targets = {
            target_id
            for target_id, states in states_by_target.items()
            if len(states) >= 2
        }
        lifecycle_by_target = _lifecycle_transitions_by_target(
            lifecycle_events,
            states_by_target,
        )

        used_raw_ids: set[int] = set()
        # Old imported sessions may not carry a target id. They cannot be
        # reconciled against 08 6F, so preserve the server amount as legacy
        # raw evidence instead of silently deleting it.
        effective: list[CombatEvent] = list(targetless_raw)
        suppressed = 0
        for target_id in reliable_targets:
            indexed = sorted(
                enumerate(states_by_target[target_id]),
                key=lambda row: (
                    row[1].timestamp_ms,
                    row[0],
                    row[1].record_offset,
                ),
            )
            states = [row for _index, row in indexed]
            target_raw = sorted(
                raw_by_target.get(target_id, ()),
                key=lambda row: row.timestamp_ms,
            )
            for previous, current in zip(states, states[1:]):
                interval_raw = [
                    event
                    for event in target_raw
                    if id(event) not in used_raw_ids
                    and (
                        previous.timestamp_ms
                        < event.timestamp_ms
                        <= current.timestamp_ms
                        or (
                            previous.timestamp_ms == current.timestamp_ms
                            and abs(event.timestamp_ms - current.timestamp_ms) <= 1
                        )
                    )
                ]
                if previous.current_hp <= 0 or any(
                    previous.timestamp_ms < edge.timestamp_ms <= current.timestamp_ms
                    for edge in lifecycle_by_target.get(target_id, ())
                ):
                    suppressed += sum(event.amount for event in interval_raw)
                    used_raw_ids.update(id(event) for event in interval_raw)
                    continue
                rise = max(0, current.current_hp - previous.current_hp)
                # Reviewed 05 records report effective restored HP, even if
                # intervening damage makes the net snapshot change negative.
                exact_restores = [e for e in interval_raw
                                  if (e.raw_attribution_method == "s2c-udp-healing-hp-delta-05-v576"
                                      or e.attribution_method in {"mechanism_recovery_unattributed", "completion_recovery_unattributed"})]
                if exact_restores:
                    effective.extend(exact_restores)
                    used_raw_ids.update(id(e) for e in exact_restores)
                    interval_raw = [e for e in interval_raw if id(e) not in used_raw_ids]
                    rise = max(0, rise - sum(e.amount for e in exact_restores))
                if interval_raw:
                    allocations, clipped = self._allocate_budget(interval_raw, rise)
                    suppressed += clipped
                    for event, amount in zip(interval_raw, allocations):
                        used_raw_ids.add(id(event))
                        if amount <= 0:
                            continue
                        effective.append(
                            replace(
                                event,
                                amount=amount,
                                raw_skill_id=event.raw_skill_id or event.skill_id,
                                raw_skill_name=event.raw_skill_name or event.skill_name,
                                raw_attribution_method=(
                                    event.raw_attribution_method
                                    or event.attribution_method
                                ),
                            )
                        )
                    attributed_rise = sum(allocations)
                else:
                    attributed_rise = 0
                unmatched_rise = rise - attributed_rise
                # A transient maximum-HP status can raise current HP at the
                # application edge without any heal.  Explicit positive-delta
                # packets inside the interval remain usable; only the residual
                # rise is suppressed when the interval crosses such an edge.
                crosses_max_hp_edge = any(
                    previous.timestamp_ms < edge_ms <= current.timestamp_ms
                    for start_ms, end_ms in excluded_hp_rise_intervals_by_target.get(
                        target_id, ()
                    )
                    for edge_ms in (start_ms, end_ms)
                )
                if unmatched_rise > 0 and not crosses_max_hp_edge:
                    effective.append(
                        self._unknown_event(
                            current.timestamp_ms,
                            unmatched_rise,
                            current,
                            "healing_hp_rise_unattributed",
                        )
                    )

        # Without two absolute states there is no trustworthy cap. Retain the
        # server's positive HP-delta amount and let the model layer decide only
        # its source/skill identity.
        for target_id, events in raw_by_target.items():
            if target_id in reliable_targets:
                # Post-Boss resets can arrive after the final combat HP state.
                # Preserve factual recovery, but never carry it across death.
                states = sorted(states_by_target[target_id], key=lambda e:e.timestamp_ms)
                last = states[-1]
                for event in events:
                    if (id(event) not in used_raw_ids and event.timestamp_ms > last.timestamp_ms
                            and (event.raw_attribution_method == "s2c-udp-healing-hp-delta-05-v576"
                                 or event.attribution_method in {"mechanism_recovery_unattributed", "completion_recovery_unattributed"})
                            and last.current_hp > 0
                            and not any(last.timestamp_ms < edge.timestamp_ms <= event.timestamp_ms
                                        for edge in lifecycle_by_target.get(target_id, ()))):
                        effective.append(event)
                        used_raw_ids.add(id(event))
                suppressed += sum(
                    event.amount for event in events if id(event) not in used_raw_ids
                )
                continue
            for event in events:
                prior_states = [s for s in states_by_target.get(target_id, ())
                                if s.timestamp_ms <= event.timestamp_ms]
                anchor = max(prior_states, key=lambda s: s.timestamp_ms, default=None)
                edges = [edge for edge in lifecycle_by_target.get(target_id, ())
                         if (anchor is None or edge.timestamp_ms > anchor.timestamp_ms)
                         and edge.timestamp_ms <= event.timestamp_ms]
                # A known dead/just-revived actor needs a new positive HP
                # anchor; its first full-life reset is not ordinary healing.
                if (anchor is not None and anchor.current_hp <= 0) or edges:
                    suppressed += event.amount
                else:
                    effective.append(event)
        return (
            tuple(
                sorted(
                    effective,
                    key=lambda row: (
                        row.timestamp_ms,
                        row.target_id,
                        row.source_id,
                    ),
                )
            ),
            max(0, suppressed),
        )

    @staticmethod
    def _candidate_score(
        event: CombatEvent,
        instance: HealingCastInstance,
    ) -> tuple[float, int] | None:
        model = instance.model
        if model.target_scope == "self" and event.target_id != instance.source_id:
            return None
        age_ms = event.timestamp_ms - instance.start_ms
        if age_ms < model.min_delay_ms:
            return None
        active_end = max(0, instance.end_ms - instance.start_ms)
        if event.timestamp_ms > instance.end_ms + model.tick_jitter_ms:
            return None

        if model.model_type == "direct":
            span = max(250, model.max_delay_ms - model.min_delay_ms)
            preferred = model.min_delay_ms + min(500, span // 3)
            timing = max(0.0, 1.0 - abs(age_ms - preferred) / max(500, span))
            tick_bucket = 0
        elif model.tick_interval_ms > 0:
            tick_index = max(
                0,
                round(
                    max(0, age_ms - model.min_delay_ms)
                    / model.tick_interval_ms
                ),
            )
            expected = model.min_delay_ms + tick_index * model.tick_interval_ms
            timing = max(
                0.0,
                1.0
                - abs(age_ms - expected)
                / max(model.tick_jitter_ms, model.tick_interval_ms),
            )
            tick_bucket = tick_index
        else:
            timing = max(
                0.0,
                1.0 - age_ms / max(1_000, active_end + model.tick_jitter_ms),
            )
            tick_bucket = max(0, age_ms // 1_000)

        score = model.confidence * 0.72 + timing * 0.28
        raw_key = healing_skill_key(event.raw_skill_name or event.skill_name)
        if raw_key and raw_key == instance.skill_key:
            score += 0.10
        if (
            event.source_id
            and event.source_id != UNKNOWN_HEALING_SOURCE_ID
            and event.source_id == instance.source_id
        ):
            score += 0.06
        return min(1.0, score), int(tick_bucket)

    @staticmethod
    def _raw_fallback(event: CombatEvent) -> CombatEvent:
        model_key = healing_skill_key(event.skill_name)
        if (
            event.source_id
            and event.source_id != UNKNOWN_HEALING_SOURCE_ID
            and model_key
            and model_key not in NON_HEALING_SKILL_KEYS
            and event.skill_id not in {"", "healing:unknown"}
        ):
            return replace(
                event,
                attribution_method="healing_raw_cast_evidence",
                raw_skill_id=event.raw_skill_id or event.skill_id,
                raw_skill_name=event.raw_skill_name or event.skill_name,
                raw_attribution_method=(
                    event.raw_attribution_method or event.attribution_method
                ),
                attribution_confidence=max(event.attribution_confidence, 0.58),
            )
        return replace(
            event,
            source_id=UNKNOWN_HEALING_SOURCE_ID,
            source_name="未知治疗来源",
            job="未知职业",
            skill_id=UNKNOWN_HEALING_SKILL_ID,
            skill_name=UNKNOWN_HEALING_SKILL_NAME,
            attribution_method=(
                event.attribution_method
                if event.attribution_method == "healing_hp_rise_unattributed"
                else "healing_model_unattributed"
            ),
            raw_skill_id=event.raw_skill_id or event.skill_id,
            raw_skill_name=event.raw_skill_name or event.skill_name,
            raw_attribution_method=(
                event.raw_attribution_method or event.attribution_method
            ),
            attribution_confidence=0.0,
        )

    def reconcile(
        self,
        raw_events: Iterable[CombatEvent],
        cast_events: Iterable[SkillCastEvent],
        health_events: Iterable[PlayerHealthEvent] = (),
        lifecycle_events: Iterable[PlayerLifecycleEvent] = (),
        *,
        party_ids: Iterable[str] | None = None,
        excluded_hp_rise_intervals_by_target: Mapping[
            str, Iterable[tuple[int, int]]
        ]
        | None = None,
        actor_jobs_by_source: Mapping[str, str] | None = None,
    ) -> HealingAttributionResult:
        raw = tuple(
            event
            for event in raw_events
            if event.healing and event.amount > 0
        )
        casts = tuple(cast_events)
        health = tuple(health_events)
        lifecycle = tuple(lifecycle_events)
        party = (
            frozenset(str(value) for value in party_ids if str(value))
            if party_ids is not None
            else None
        )
        if party is not None:
            raw = tuple(
                event
                for event in raw
                if (
                    event.target_id in party
                    if event.target_id
                    else event.source_id in party
                )
            )
        excluded_intervals = {
            str(target_id): tuple(
                (min(int(start_ms), int(end_ms)), max(int(start_ms), int(end_ms)))
                for start_ms, end_ms in intervals
            )
            for target_id, intervals in (
                excluded_hp_rise_intervals_by_target or {}
            ).items()
        }
        if party is not None:
            casts = tuple(cast for cast in casts if cast.source_id in party)
        factual, suppressed = self._factual_events(
            raw,
            health,
            lifecycle,
            party,
            excluded_intervals,
        )
        instances = self._cast_instances(
            casts,
            actor_jobs_by_source or {},
        )
        usage: dict[tuple[str, int], set[str]] = defaultdict(set)
        attributed: list[CombatEvent] = []
        ambiguous_events = 0
        for event in factual:
            if event.attribution_method in {"mechanism_recovery_unattributed", "completion_recovery_unattributed"}:
                attributed.append(replace(event, source_id=UNKNOWN_HEALING_SOURCE_ID,
                    source_name="其他来源恢复", job="未知职业",
                    skill_id=UNKNOWN_HEALING_SKILL_ID, skill_name=UNKNOWN_HEALING_SKILL_NAME,
                    attribution_confidence=0.0))
                continue
            candidates: list[tuple[float, HealingCastInstance, int]] = []
            for instance in instances:
                scored = self._candidate_score(event, instance)
                if scored is None:
                    continue
                score, tick_bucket = scored
                used_targets = usage[(instance.instance_id, tick_bucket)]
                if (
                    event.target_id not in used_targets
                    and len(used_targets) >= instance.model.max_targets_per_tick
                ):
                    continue
                candidates.append((score, instance, tick_bucket))
            candidates.sort(
                key=lambda row: (
                    row[0],
                    row[1].start_ms,
                    row[1].source_id,
                ),
                reverse=True,
            )
            if not candidates or candidates[0][0] < 0.52:
                attributed.append(self._raw_fallback(event))
                continue
            best_score, best, tick_bucket = candidates[0]
            if (
                len(candidates) > 1
                and candidates[1][1].source_id != best.source_id
                and best_score - candidates[1][0] < 0.055
                and event.source_id in {"", UNKNOWN_HEALING_SOURCE_ID}
            ):
                ambiguous_events += 1
                attributed.append(
                    replace(
                        self._raw_fallback(event),
                        attribution_method="healing_model_ambiguous",
                    )
                )
                continue
            usage[(best.instance_id, tick_bucket)].add(event.target_id)
            method = f"healing_model_{best.model.model_type}"
            attributed.append(
                replace(
                    event,
                    source_id=best.source_id,
                    source_name=best.source_name or best.source_id,
                    job=best.job,
                    skill_id=best.skill_id,
                    skill_name=best.skill_name,
                    attribution_method=method,
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method or event.attribution_method
                    ),
                    cast_instance_id=best.instance_id,
                    attribution_confidence=max(0.0, min(1.0, best_score)),
                )
            )

        attributed.sort(
            key=lambda row: (
                row.timestamp_ms,
                row.source_id,
                row.target_id,
                row.skill_id,
            )
        )
        attributed_total = sum(
            event.amount
            for event in attributed
            if event.source_id != UNKNOWN_HEALING_SOURCE_ID
        )
        factual_total = sum(event.amount for event in attributed)
        unattributed_total = factual_total - attributed_total
        method_counts: dict[str, int] = defaultdict(int)
        for event in attributed:
            method_counts[event.attribution_method] += 1
        result = HealingAttributionResult(
            events=tuple(attributed),
            instances=instances,
            raw_packet_total=sum(event.amount for event in raw),
            factual_healing_total=factual_total,
            attributed_healing_total=attributed_total,
            unattributed_healing_total=unattributed_total,
            suppressed_overheal_total=suppressed,
            matched_events=sum(
                1
                for event in attributed
                if event.attribution_method.startswith("healing_model_")
                and event.source_id != UNKNOWN_HEALING_SOURCE_ID
            ),
            unattributed_events=sum(
                1
                for event in attributed
                if event.source_id == UNKNOWN_HEALING_SOURCE_ID
            ),
            ambiguous_events=ambiguous_events,
            method_counts=tuple(sorted(method_counts.items())),
        )
        if not result.balanced:
            raise RuntimeError("healing attribution ledger is not balanced")
        return result
