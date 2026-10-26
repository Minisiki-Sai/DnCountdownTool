from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, replace

from ..data_loader import DataStore, as_number, norm
from .buff_clear import player_buff_clear_times_by_target
from .boss_scope import _compress_events_to_windows, _events_in_windows, _merge_seconds_windows
from .cast_timing import LiveCastTimingResolver
from .damage_ledger import is_disallowed_damage_derived_cast, requires_action_packet_for_cast
from .critical_stats import is_critical_rate_sample
from .models import BossActionEvent, CombatEvent, SkillCastEvent
from .skill_identity import canonical_skill
from .timeline_evidence import (
    TIMELINE_EVIDENCE_SCHEMA_VERSION,
    TimelineEvidenceResolver,
    classify_timeline_hit,
    is_timeline_recovered_cast,
)


_TIMELINE_HIT_CLUSTER_GAP_MS = 750
_TIMELINE_HIT_TO_LATE_CAST_MS = 750
_TIMELINE_MIN_CAST_DAMAGE_WINDOW_MS = 3000
_BOSS_TIMELINE_CONTEXT_SECONDS = 30.0
_BOSS_TIMELINE_PREP_SECONDS = 3.0
_TIMELINE_NON_ACTION_DAMAGE_METHODS = {
    "cast_instance_match",
    "continuation_window",
    "healing_cast",
    "learned_cast_profile",
    "supplemental_cast_owner",
    "supplemental_same_packet",
    "summon_owner",
    "packet_entity_template_owner",
    "packet_entity_status_damage",
    "packet_entity_self_attack",
    "packet_entity_projectile_selector",
    "packet_entity_skill_id",
    "sustained_cast_window",
    "status_damage_packet",
    "appended_status_first_hit",
    "unique_active_cast",
}

@dataclass(slots=True)
class _TimelineOccurrence:
    event: CombatEvent | SkillCastEvent
    start_seconds: float
    end_seconds: float
    damage: int
    healing: int
    hits: int
    critical_hits: int
    cast_time: float = 0.0
    action_speed_percent: float = 0.0

class _LiveCastTimingResolver:
    def __init__(
        self,
        store: DataStore | None,
        buff_clear_events: tuple[BossActionEvent, ...] = (),
    ) -> None:
        self._resolver = LiveCastTimingResolver(
            store,
            player_buff_clear_times_by_target(buff_clear_events),
        )

    def reset(self) -> None:
        self._resolver.reset()

    def occurrence(
        self, event: SkillCastEvent, seconds: float
    ) -> _TimelineOccurrence:
        display_name = _timeline_skill_display_name(event)
        timing_event = (
            replace(event, skill_name=display_name)
            if display_name and display_name != event.skill_name
            else event
        )
        timing = self._resolver.resolve(timing_event, seconds)
        return _TimelineOccurrence(
            event=event,
            start_seconds=seconds,
            end_seconds=timing.end_seconds,
            damage=0,
            healing=0,
            hits=0,
            critical_hits=0,
            cast_time=timing.cast_time,
            action_speed_percent=timing.action_speed_percent,
        )

def _timeline_skill_key(event: CombatEvent | SkillCastEvent) -> str:
    name = canonical_skill(event.skill_name)
    return name or event.skill_id or event.skill_name or "unknown"

def _timeline_skill_name(event: CombatEvent | SkillCastEvent) -> str:
    """Return the simulator skill name for timeline data lookups."""
    raw = norm(event.skill_name).replace(" EX", "EX")
    if not raw:
        return ""
    key = canonical_skill(raw)
    ex_suffix = "EX" if re.search(r"EX$", raw, flags=re.IGNORECASE) else ""
    return f"{key}{ex_suffix}"

def _timeline_skill_display_name(event: CombatEvent | SkillCastEvent) -> str:
    """Use one readable name for normal and 神圣 Silver Hunter variants."""
    raw = norm(event.skill_name)
    normalized = _timeline_skill_name(event)
    if raw.startswith("神圣") and normalized:
        return normalized
    return event.skill_name or event.skill_id or "unknown"

def _timeline_skill_family(event: CombatEvent | SkillCastEvent) -> str:
    name = canonical_skill(event.skill_name)
    if name:
        return name
    return _timeline_skill_key(event)

def _is_timeline_periodic_damage(event: CombatEvent) -> bool:
    method = event.attribution_method
    return (
        event.skill_id.startswith("periodic:")
        or method.startswith("periodic_")
        or method in {"status_damage_packet", "appended_status_first_hit"}
        or "status_damage" in method
    )

def _timeline_damage_hit_clusters(
    combat_events: tuple[CombatEvent, ...],
) -> dict[tuple[str, str], list[tuple[CombatEvent, ...]]]:
    """Group compact multi-hit records into probable individual casts."""
    hits_by_family: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
    for event in combat_events:
        if _is_timeline_periodic_damage(event):
            continue
        hits_by_family[(event.source_id, _timeline_skill_family(event))].append(event)

    result: dict[tuple[str, str], list[tuple[CombatEvent, ...]]] = {}
    for key, family_hits in hits_by_family.items():
        clusters: list[list[CombatEvent]] = []
        for event in sorted(family_hits, key=lambda row: row.timestamp_ms):
            if (
                not clusters
                or event.timestamp_ms - clusters[-1][-1].timestamp_ms
                > _TIMELINE_HIT_CLUSTER_GAP_MS
            ):
                clusters.append([event])
            else:
                clusters[-1].append(event)
        result[key] = [tuple(cluster) for cluster in clusters]
    return result

def _timeline_missing_combo_hits(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
) -> tuple[CombatEvent, ...]:
    """Keep the proven aerial-combo recovery as a compatibility floor."""
    combo_starters = {
        "1402",  # Binding Shot
        "1504",  # Binding Shot EX
        "1409",  # Eagle Dive
        "1554",  # Eagle Dive EX
        "1503",  # Hurricane Dance
    }
    double_somersault_ids = {"1411", "1505"}
    casts_by_source: dict[str, list[SkillCastEvent]] = defaultdict(list)
    hits_by_source: dict[str, list[CombatEvent]] = defaultdict(list)
    for event in cast_events:
        casts_by_source[event.source_id].append(event)
    for event in combat_events:
        if not _is_timeline_periodic_damage(event):
            hits_by_source[event.source_id].append(event)

    recovered: list[CombatEvent] = []
    for source_id, source_casts in casts_by_source.items():
        source_casts.sort(key=lambda row: row.timestamp_ms)
        source_hits = sorted(
            hits_by_source.get(source_id, ()), key=lambda row: row.timestamp_ms
        )
        for cast in source_casts:
            if cast.skill_id not in double_somersault_ids:
                continue
            window_start = cast.timestamp_ms - 2500
            has_starter_cast = any(
                event.skill_id in combo_starters
                and window_start <= event.timestamp_ms < cast.timestamp_ms
                for event in source_casts
            )
            if has_starter_cast:
                continue
            starter_hits = [
                event
                for event in source_hits
                if event.skill_id in combo_starters
                and window_start <= event.timestamp_ms < cast.timestamp_ms
            ]
            if not starter_hits:
                continue
            family = _timeline_skill_family(starter_hits[-1])
            cluster: list[CombatEvent] = []
            for event in reversed(starter_hits):
                if _timeline_skill_family(event) != family:
                    continue
                if cluster and cluster[-1].timestamp_ms - event.timestamp_ms > 850:
                    break
                cluster.append(event)
            recovered.extend(reversed(cluster))
    return tuple(recovered)

def _timeline_unmatched_damage_hits(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore | None = None,
) -> tuple[CombatEvent, ...]:
    """Match each hit cluster to nearby casts and conservatively recover misses.

    Existing damage-only families remain the quality baseline. For a family
    that also has authoritative casts, only direct, non-duration damage outside
    every local cast window is allowed to add another occurrence.
    """
    casts_by_family: dict[tuple[str, str], list[SkillCastEvent]] = defaultdict(list)
    for event in cast_events:
        casts_by_family[(event.source_id, _timeline_skill_family(event))].append(event)
    for family_casts in casts_by_family.values():
        family_casts.sort(key=lambda row: row.timestamp_ms)

    timing_cache: dict[tuple[str, str], tuple[int, bool]] = {}
    damage_families_cache: dict[str, frozenset[str]] = {}

    def is_catalogued_for_job(event: CombatEvent | SkillCastEvent) -> bool:
        loader = getattr(store, "skill_damage_entry_names", None)
        if not callable(loader):
            return True
        job = norm(event.job)
        families = damage_families_cache.get(job)
        if families is None:
            families = frozenset(
                norm(name).replace(" ", "").upper().removesuffix("EX")
                for name in loader(job)
                if norm(name)
            )
            damage_families_cache[job] = families
        return _timeline_skill_family(event) in families

    def local_timing(event: CombatEvent | SkillCastEvent) -> tuple[int, bool]:
        cache_key = (norm(event.job), _timeline_skill_family(event))
        cached = timing_cache.get(cache_key)
        if cached is not None:
            return cached
        if store is None:
            result = (_TIMELINE_MIN_CAST_DAMAGE_WINDOW_MS, True)
            timing_cache[cache_key] = result
            return result

        skill = _timeline_skill_name(event)
        base_skill = re.sub(r"EX(?:LV\d+)?$", "", skill, flags=re.IGNORECASE)
        cast_row = store.skill_cast_time(skill, [event.job])
        cast_time = max(
            as_number(cast_row.get("cast_time")),
            as_number(cast_row.get("quick_time")),
        )
        effect_row = store.skill_effect_timing(
            event.job, base_skill or skill, max_required_level=70
        )
        duration = max(0.0, as_number(effect_row.get("duration")))
        description = norm(effect_row.get("desc"))
        has_skill_data = bool(cast_row) or bool(description)
        deals_damage = "攻击力" in description or "伤害" in description
        window_ms = max(
            _TIMELINE_MIN_CAST_DAMAGE_WINDOW_MS,
            int(round((cast_time + 0.75) * 1000)),
        )
        # Duration alone does not make a hit non-action damage: stance/channel
        # skills such as Surround and Slashing Stance deal direct packet-tagged
        # hits throughout their action. Summons, DoTs and continuations are
        # rejected below by attribution method, which is the reliable boundary.
        result = (
            window_ms,
            has_skill_data and deals_damage and is_catalogued_for_job(event),
        )
        timing_cache[cache_key] = result
        return result

    def recoverable_cluster(cluster: tuple[CombatEvent, ...]) -> bool:
        """Require both a real damage skill and direct action evidence."""
        _window_ms, recoverable = local_timing(cluster[0])
        methods = {event.attribution_method for event in cluster}
        direct_damage = not any(
            method in _TIMELINE_NON_ACTION_DAMAGE_METHODS
            or "status_damage" in method
            or method.startswith("periodic_")
            for method in methods
        )
        return recoverable and direct_damage

    fallback: list[CombatEvent] = []
    for key, clusters in _timeline_damage_hit_clusters(combat_events).items():
        family_casts = casts_by_family.get(key, ())
        for cluster in clusters:
            if not family_casts:
                if recoverable_cluster(cluster):
                    fallback.extend(cluster)
                continue
            first_hit_ms = cluster[0].timestamp_ms
            covered = False
            for cast in family_casts:
                cast_window_ms, _recoverable = local_timing(cast)
                if first_hit_ms < cast.timestamp_ms - _TIMELINE_HIT_TO_LATE_CAST_MS:
                    break
                if cast.timestamp_ms - _TIMELINE_HIT_TO_LATE_CAST_MS <= first_hit_ms <= (
                    cast.timestamp_ms + cast_window_ms
                ):
                    covered = True
                    break
            if covered:
                continue
            if recoverable_cluster(cluster):
                fallback.extend(cluster)
    return tuple(fallback)

def _timeline_inferred_casts_from_damage(
    fallback_hits: tuple[CombatEvent, ...],
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore,
) -> tuple[SkillCastEvent, ...]:
    """Turn authoritative unmatched hit bursts into one readable cast each.

    Some v576 actions have no standalone cast packet even though every damage
    packet carries the exact skill. Existing casts of the same skill teach the
    normal first-hit delay, so missing occurrences can be placed near their
    real action start instead of displaying dozens of hit fragments.
    """
    if not fallback_hits:
        return ()

    timing_cache: dict[tuple[str, str], float] = {}

    def cast_time_seconds(event: CombatEvent | SkillCastEvent) -> float:
        cache_key = (norm(event.job), _timeline_skill_family(event))
        cached = timing_cache.get(cache_key)
        if cached is not None:
            return cached
        skill = _timeline_skill_name(event)
        row = store.skill_cast_time(skill, [event.job])
        result = max(
            0.0,
            as_number(row.get("cast_time")),
            as_number(row.get("quick_time")),
        )
        timing_cache[cache_key] = result
        return result

    all_hits_by_family: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
    fallback_by_family: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
    casts_by_family: dict[tuple[str, str], list[SkillCastEvent]] = defaultdict(list)
    for event in combat_events:
        if not _is_timeline_periodic_damage(event):
            all_hits_by_family[
                (event.source_id, _timeline_skill_family(event))
            ].append(event)
    for event in fallback_hits:
        fallback_by_family[
            (event.source_id, _timeline_skill_family(event))
        ].append(event)
    for event in cast_events:
        casts_by_family[(event.source_id, _timeline_skill_family(event))].append(event)

    inferred: list[SkillCastEvent] = []
    for key, family_hits in fallback_by_family.items():
        family_hits.sort(key=lambda row: row.timestamp_ms)
        sample = family_hits[0]
        if requires_action_packet_for_cast(
            sample.skill_id, sample.skill_name, sample.job
        ):
            continue
        cast_seconds = cast_time_seconds(sample)
        max_gap_ms = round(
            max(0.75, min(1.25, cast_seconds * 0.35 or 0.75)) * 1000
        )
        max_span_ms = round(max(1.25, cast_seconds + 0.75) * 1000)
        clusters: list[list[CombatEvent]] = []
        for event in family_hits:
            if (
                not clusters
                or event.timestamp_ms - clusters[-1][-1].timestamp_ms > max_gap_ms
                or event.timestamp_ms - clusters[-1][0].timestamp_ms > max_span_ms
            ):
                clusters.append([event])
            else:
                clusters[-1].append(event)

        first_hit_delays: list[int] = []
        all_family_hits = sorted(
            all_hits_by_family.get(key, ()), key=lambda row: row.timestamp_ms
        )
        search_window_ms = round(max(3.0, cast_seconds + 0.75) * 1000)
        for cast in casts_by_family.get(key, ()):
            first_hit = next(
                (
                    hit
                    for hit in all_family_hits
                    if cast.timestamp_ms <= hit.timestamp_ms
                    <= cast.timestamp_ms + search_window_ms
                ),
                None,
            )
            if first_hit is not None:
                first_hit_delays.append(first_hit.timestamp_ms - cast.timestamp_ms)
        if first_hit_delays:
            ordered_delays = sorted(first_hit_delays)
            first_hit_delay_ms = ordered_delays[len(ordered_delays) // 2]
            first_hit_delay_ms = min(
                first_hit_delay_ms, round(max(0.0, cast_seconds) * 1000)
            )
        else:
            first_hit_delay_ms = 0

        for cluster in clusters:
            first_hit = cluster[0]
            inferred.append(
                SkillCastEvent(
                    timestamp_ms=max(0, first_hit.timestamp_ms - first_hit_delay_ms),
                    source_id=first_hit.source_id,
                    source_name=first_hit.source_name,
                    job=first_hit.job,
                    skill_id=first_hit.skill_id,
                    skill_name=first_hit.skill_name,
                    packet_kind="damage_inferred",
                )
            )
    return tuple(sorted(inferred, key=lambda row: row.timestamp_ms))

def _legacy_timeline_events_with_damage_fallback(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore | None = None,
) -> tuple[tuple[CombatEvent | SkillCastEvent, ...], int]:
    """Keep authoritative casts and locally recover individually missed casts."""
    cast_events = tuple(
        event
        for event in cast_events
        if not is_disallowed_damage_derived_cast(event)
    )
    timeline_hits = tuple(
        event
        for event in combat_events
        if not _is_timeline_periodic_damage(event)
    )
    fallback_hits = tuple(
        _timeline_unmatched_damage_hits(cast_events, timeline_hits, store)
    )
    fallback: list[CombatEvent | SkillCastEvent] = list(
        _timeline_inferred_casts_from_damage(
            fallback_hits, cast_events, timeline_hits, store
        )
        if store is not None
        else fallback_hits
    )
    fallback_ids = {id(event) for event in fallback_hits}
    for event in _timeline_missing_combo_hits(cast_events, timeline_hits):
        if id(event) not in fallback_ids:
            fallback.append(event)
            fallback_ids.add(id(event))
    combined: tuple[CombatEvent | SkillCastEvent, ...] = tuple(
        sorted((*cast_events, *fallback), key=lambda event: event.timestamp_ms)
    )
    return combined, len(fallback)


def _evidence_timeline_events(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore | None,
) -> tuple[
    tuple[CombatEvent | SkillCastEvent, ...],
    int,
    tuple[object, ...],
]:
    """Build one cast axis from protocol and corpus evidence.

    Unlike the legacy path, damage attribution and cast reconstruction are not
    repeated here. Every accepted fallback is already a ``SkillCastEvent``
    with explicit evidence provenance; ambiguous damage remains damage only.
    """

    resolver = TimelineEvidenceResolver(store)
    result = resolver.resolve(cast_events, combat_events)
    return result.cast_events, len(result.recovered_casts), result.decisions


def _timeline_mode(value: object = None) -> str:
    configured = norm(
        value
        or os.environ.get("DRAGON_NEST_TIMELINE_MODE")
        or "evidence"
    ).casefold()
    if configured in {"legacy", "shadow", "evidence"}:
        return configured
    return "evidence"


def _timeline_events_with_damage_fallback(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore | None = None,
    *,
    mode: str | None = None,
    diagnostics: dict[str, object] | None = None,
) -> tuple[tuple[CombatEvent | SkillCastEvent, ...], int]:
    """Return the selected timeline projection.

    ``legacy`` freezes the V4.523 behaviour for regression and emergency
    rollback. ``shadow`` calculates both paths but returns the legacy axis.
    ``evidence`` is the normal path and contains no profession/skill denylist.
    """

    selected_mode = _timeline_mode(mode)
    if selected_mode == "legacy":
        events, fallback_count = _legacy_timeline_events_with_damage_fallback(
            cast_events, combat_events, store
        )
        if diagnostics is not None:
            diagnostics.update(
                {
                    "mode": "legacy",
                    "schema": "legacy-v4523",
                    "event_count": len(events),
                    "fallback_count": fallback_count,
                }
            )
        return events, fallback_count

    evidence_events, evidence_count, decisions = _evidence_timeline_events(
        cast_events, combat_events, store
    )
    if selected_mode == "shadow":
        legacy_events, legacy_count = _legacy_timeline_events_with_damage_fallback(
            cast_events, combat_events, store
        )
        if diagnostics is not None:
            diagnostics.update(
                {
                    "mode": "shadow",
                    "schema": TIMELINE_EVIDENCE_SCHEMA_VERSION,
                    "legacy_event_count": len(legacy_events),
                    "legacy_fallback_count": legacy_count,
                    "evidence_event_count": len(evidence_events),
                    "evidence_fallback_count": evidence_count,
                    "accepted_candidates": sum(
                        bool(getattr(row, "accepted", False)) for row in decisions
                    ),
                    "rejected_candidates": sum(
                        not bool(getattr(row, "accepted", False)) for row in decisions
                    ),
                    "decisions": decisions,
                }
            )
        return legacy_events, legacy_count

    if diagnostics is not None:
        diagnostics.update(
            {
                "mode": "evidence",
                "schema": TIMELINE_EVIDENCE_SCHEMA_VERSION,
                "event_count": len(evidence_events),
                "fallback_count": evidence_count,
                "accepted_candidates": sum(
                    bool(getattr(row, "accepted", False)) for row in decisions
                ),
                "rejected_candidates": sum(
                    not bool(getattr(row, "accepted", False)) for row in decisions
                ),
                "decisions": decisions,
            }
        )
    return evidence_events, evidence_count

def _boss_timeline_events_for_windows(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    target_ids: frozenset[str],
    windows: tuple[tuple[float, float], ...],
    store: DataStore | None = None,
) -> tuple[tuple[CombatEvent | SkillCastEvent, ...], float]:
    """Build a Boss-only axis without resolving unrelated replay phases."""
    display_windows = _merge_seconds_windows(
        (max(0.0, start - _BOSS_TIMELINE_PREP_SECONDS), end)
        for start, end in windows
    )
    # Keep the complete cast index: long-lived summons can be created well
    # before a Boss phase and still deal damage inside it. Cast rows are much
    # smaller than damage rows, so this preserves attribution at little cost.
    context_casts = cast_events
    context_combat = tuple(
        event
        for event in _events_in_windows(
            combat_events,
            windows,
            margin_seconds=_BOSS_TIMELINE_CONTEXT_SECONDS,
        )
        if isinstance(event, CombatEvent)
    )
    local_events, _fallback_count = _timeline_events_with_damage_fallback(
        context_casts,
        context_combat,
        store,
    )
    selected_events = tuple(
        event
        for event in _events_in_windows(local_events, display_windows)
        if (
            isinstance(event, SkillCastEvent)
            or (
                event.target_id in target_ids
                and any(
                    start <= event.timestamp_ms / 1000.0 <= end
                    for start, end in windows
                )
            )
        )
    )
    return _compress_events_to_windows(
        selected_events, display_windows, preserve_clock=True
    )

def _build_timeline_occurrences(
    events: tuple[CombatEvent | SkillCastEvent, ...],
    origin_ms: int,
    timing_resolver: _LiveCastTimingResolver | None = None,
) -> list[_TimelineOccurrence]:
    """Collapse the nearby hit records of one cast into a readable block."""
    occurrences: list[_TimelineOccurrence] = []
    latest_by_skill: dict[tuple[str, str], _TimelineOccurrence] = {}
    if timing_resolver is not None:
        timing_resolver.reset()
    for event in sorted(events, key=lambda row: row.timestamp_ms):
        seconds = max(0.0, (event.timestamp_ms - origin_ms) / 1000.0)
        if isinstance(event, SkillCastEvent):
            occurrences.append(
                timing_resolver.occurrence(event, seconds)
                if timing_resolver is not None
                else _TimelineOccurrence(event, seconds, seconds, 0, 0, 0, 0)
            )
            continue
        key = (event.source_id, _timeline_skill_key(event))
        current = latest_by_skill.get(key)
        if current is not None and seconds - current.end_seconds <= 0.75:
            current.end_seconds = max(current.end_seconds, seconds)
            current.damage += 0 if event.healing else event.amount
            current.healing += event.amount if event.healing else 0
            current.hits += 1
            current.critical_hits += int(
                is_critical_rate_sample(event) and event.critical
            )
            continue
        current = _TimelineOccurrence(
            event=event,
            start_seconds=seconds,
            end_seconds=seconds,
            damage=0 if event.healing else event.amount,
            healing=event.amount if event.healing else 0,
            hits=1,
            critical_hits=int(
                is_critical_rate_sample(event) and event.critical
            ),
        )
        latest_by_skill[key] = current
        occurrences.append(current)
    occurrences.sort(key=lambda row: (row.start_seconds, row.event.source_id))
    return occurrences

def _timeline_score_cast_events(
    events: tuple[CombatEvent | SkillCastEvent, ...],
    store: DataStore,
    *,
    mode: str | None = None,
) -> tuple[SkillCastEvent, ...]:
    """Use exactly one scoring cast for every block on the shared skill axis."""

    selected_mode = _timeline_mode(mode)
    if selected_mode != "legacy" and any(
        isinstance(event, CombatEvent) for event in events
    ):
        # Callers normally pass the already resolved evidence axis. This
        # defensive projection keeps direct callers from turning an arbitrary
        # damage row into a scored cast.
        result = TimelineEvidenceResolver(store).resolve(
            tuple(event for event in events if isinstance(event, SkillCastEvent)),
            tuple(event for event in events if isinstance(event, CombatEvent)),
        )
        events = result.cast_events

    rows: list[SkillCastEvent] = []
    for occurrence in _build_timeline_occurrences(
        events,
        0,
        _LiveCastTimingResolver(store),
    ):
        event = occurrence.event
        if isinstance(event, SkillCastEvent):
            if selected_mode == "legacy" and is_disallowed_damage_derived_cast(event):
                continue
            rows.append(event)
            continue
        if selected_mode == "legacy":
            if requires_action_packet_for_cast(
                event.skill_id, event.skill_name, event.job
            ):
                continue
            packet_kind = "shared_timeline_damage"
        else:
            if not classify_timeline_hit(event).accepted:
                continue
            packet_kind = "timeline_combo_recovered"
        rows.append(
            SkillCastEvent(
                timestamp_ms=max(
                    0, int(round(occurrence.start_seconds * 1000.0))
                ),
                source_id=event.source_id,
                source_name=event.source_name,
                job=event.job,
                skill_id=event.skill_id,
                skill_name=event.skill_name,
                packet_kind=packet_kind,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.timestamp_ms))


def evidence_score_cast_events(
    cast_events: tuple[SkillCastEvent, ...],
    combat_events: tuple[CombatEvent, ...],
    store: DataStore,
) -> tuple[SkillCastEvent, ...]:
    """Return the single evidence axis shared by display and scoring."""

    events, _fallback_count = _timeline_events_with_damage_fallback(
        cast_events,
        combat_events,
        store,
        mode="evidence",
    )
    return _timeline_score_cast_events(events, store, mode="evidence")
