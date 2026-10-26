from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace

from .boss_hp import normalize_boss_hp_name
from .boss_recognizer import BossRecognizer
from .models import (
    BossActionEvent,
    BossHpTrackSnapshot,
    CombatEvent,
    CombatSnapshot,
    SkillCastEvent,
)
from .skill_identity import canonical_skill


_TERMINAL_BOSS_COMPLETION_MOVEMENT_SKILLS = frozenset(
    canonical_skill(skill)
    for skill in ("翻滚", "冲锋", "闪现", "疾闪", "滑步", "三连斩")
)

@dataclass(frozen=True, slots=True)
class _BossStatisticsGroup:
    identity: tuple[str, str]
    name: str
    max_hp: int
    target_ids: frozenset[str]
    damage: int
    hits: int
    critical_hits: int
    first_ms: int
    last_ms: int
    damage_series: tuple[tuple[int, int], ...]
    phase_windows: tuple[tuple[float, float], ...]
    critical_samples: int | None = None

    @property
    def active_seconds(self) -> float:
        return max(0.001, (self.last_ms - self.first_ms) / 1000.0)

def _merge_seconds_windows(
    windows: list[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    merged: list[list[float]] = []
    for start, end in sorted(windows):
        start = max(0.0, float(start))
        end = max(start, float(end))
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)

def _boss_statistics_groups(
    snapshot: CombatSnapshot,
) -> tuple[_BossStatisticsGroup, ...]:
    """Use the shared recognizer for every Boss-oriented live DPS view."""

    return tuple(
        _BossStatisticsGroup(
            identity=row.identity,
            name=row.name,
            max_hp=row.max_hp,
            target_ids=row.target_ids,
            damage=row.damage,
            hits=row.hits,
            critical_hits=row.critical_hits,
            first_ms=row.first_ms,
            last_ms=row.last_ms,
            damage_series=row.damage_series,
            phase_windows=row.phase_windows,
            critical_samples=row.critical_samples,
        )
        for row in BossRecognizer().recognize_snapshot(snapshot)
    )

def _compress_events_to_windows(
    events: tuple[CombatEvent | SkillCastEvent | BossActionEvent, ...],
    windows: tuple[tuple[float, float], ...],
    *,
    preserve_clock: bool = False,
) -> tuple[tuple[CombatEvent | SkillCastEvent | BossActionEvent, ...], float]:
    """Filter selected Boss phases, optionally retaining capture timestamps."""
    merged_windows = _merge_seconds_windows(list(windows))
    if not merged_windows:
        return (), 0.0
    offsets: list[tuple[float, float, float]] = []
    elapsed = 0.0
    for start, end in merged_windows:
        offsets.append((start, end, start if preserve_clock else elapsed))
        elapsed += end - start

    compressed: list[CombatEvent | SkillCastEvent | BossActionEvent] = []
    for event in events:
        seconds = event.timestamp_ms / 1000.0
        for start, end, offset in offsets:
            if start <= seconds <= end:
                mapped_timestamp_ms = max(
                    0,
                    round((offset + seconds - start) * 1000),
                )
                updates: dict[str, object] = {
                    "timestamp_ms": mapped_timestamp_ms
                }
                if (
                    isinstance(event, BossActionEvent)
                    and event.end_timestamp_ms > event.timestamp_ms
                ):
                    updates["end_timestamp_ms"] = mapped_timestamp_ms + (
                        event.end_timestamp_ms - event.timestamp_ms
                    )
                compressed.append(
                    replace(event, **updates)
                )
                break
    compressed.sort(key=lambda event: event.timestamp_ms)
    return tuple(compressed), (
        merged_windows[-1][1] if preserve_clock else elapsed
    )

def _compress_boss_hp_tracks_to_windows(
    tracks: tuple[BossHpTrackSnapshot, ...],
    groups: tuple[_BossStatisticsGroup, ...],
    windows: tuple[tuple[float, float], ...],
    *,
    infer_from_damage: bool = False,
    completed_group_identities: frozenset[tuple[str, str]] = frozenset(),
    fixed_boss_hp_references: dict[str, tuple[int, ...]] | None = None,
    preserve_clock: bool = False,
) -> tuple[
    tuple[
        str,
        str,
        tuple[tuple[tuple[float, float], ...], ...],
    ],
    ...,
]:
    """Clip Boss HP tracks, optionally retaining capture-relative seconds."""
    # Retain the legacy argument for callers, but never fabricate HP from damage.
    infer_from_damage = False
    tracks = tuple(track for track in tracks if track.confidence == "packet_hp")
    from types import SimpleNamespace
    from .nest_hp_reference import select_reference, entity_max, known_template
    reference = select_reference(SimpleNamespace(boss_hp_tracks=tracks),
                                 [t for group in groups for t in group.target_ids])
    def hp_percent(track, point):
        maximum = entity_max(reference, track.target_id) if known_template(track.target_id) else track.max_hp
        if point.current_hp is None or maximum <= 0 or point.current_hp > maximum*1.00001:
            return None
        return max(0., min(100., 100.*point.current_hp/maximum))
    merged_windows = _merge_seconds_windows(list(windows))
    if not merged_windows or not groups:
        return ()
    offsets: list[tuple[float, float, float]] = []
    elapsed = 0.0
    for start, end in merged_windows:
        offsets.append((start, end, start if preserve_clock else elapsed))
        elapsed += end - start

    tracks_by_target: dict[str, list[BossHpTrackSnapshot]] = defaultdict(list)
    for track in tracks:
        if track.target_id and (
            any(hp_percent(track, point) is not None for point in track.points)
            or any(point.damage > 0 or point.healing > 0 for point in track.points)
        ):
            tracks_by_target[track.target_id].append(track)
    for target_tracks in tracks_by_target.values():
        target_tracks.sort(key=lambda row: (row.first_seconds, row.instance_id))

    rows: list[
        tuple[
            str,
            str,
            tuple[tuple[tuple[float, float], ...], ...],
        ]
    ] = []
    for group in groups:
        segments: list[tuple[tuple[float, float], ...]] = []
        confidences: set[str] = set()
        group_windows = _merge_seconds_windows(list(group.phase_windows))
        for target_id in group.target_ids:
            target_tracks = tracks_by_target.get(target_id, [])
            for track_index, track in enumerate(target_tracks):
                points = tuple(
                    (
                        max(0.0, float(point.timestamp_seconds)),
                        hp_percent(track, point),
                    )
                    for point in track.points
                    if hp_percent(track, point) is not None
                )
                if not points:
                    continue
                point_times = tuple(point[0] for point in points)
                next_track_start = (
                    target_tracks[track_index + 1].first_seconds
                    if track_index + 1 < len(target_tracks)
                    else float("inf")
                )
                for phase_start, phase_end in group_windows:
                    for window_start, window_end, offset in offsets:
                        source_start = max(
                            phase_start,
                            window_start,
                            track.first_seconds,
                        )
                        source_end = min(
                            phase_end,
                            window_end,
                            next_track_start,
                        )
                        if source_end < source_start - 1e-9:
                            continue
                        prior_index = bisect_right(point_times, source_start) - 1
                        if prior_index >= 0:
                            cursor = source_start
                            current_percent = points[prior_index][1]
                            next_index = prior_index + 1
                        else:
                            next_index = bisect_left(point_times, source_start)
                            if (
                                next_index >= len(points)
                                or points[next_index][0] > source_end + 1e-9
                            ):
                                continue
                            cursor, current_percent = points[next_index]
                            next_index += 1
                        mapped: list[tuple[float, float]] = [
                            (
                                offset + cursor - window_start,
                                current_percent,
                            )
                        ]
                        while (
                            next_index < len(points)
                            and points[next_index][0] <= source_end + 1e-9
                        ):
                            point_seconds, current_percent = points[next_index]
                            mapped.append(
                                (
                                    offset + point_seconds - window_start,
                                    current_percent,
                                )
                            )
                            next_index += 1
                        mapped_end = offset + source_end - window_start
                        if mapped_end > mapped[-1][0] + 1e-9:
                            mapped.append((mapped_end, current_percent))
                        segments.append(tuple(mapped))
                        confidences.add(track.confidence)
        if segments:
            confidence = "packet_hp"
            rows.append((group.name, confidence, tuple(segments)))
    return tuple(rows)

def _completed_boss_group_identities(
    groups: tuple[_BossStatisticsGroup, ...],
    tracks: tuple[BossHpTrackSnapshot, ...],
    *,
    terminal_skill_casts: tuple[SkillCastEvent, ...] = (),
    fixed_boss_hp_references: dict[str, tuple[int, ...]] | None = None,
) -> frozenset[tuple[str, str]]:
    """Return encounters whose completion is supported by packet chronology."""

    completed_target_ids = {
        track.target_id for track in tracks if track.complete and track.target_id
    }
    completed: set[tuple[str, str]] = {
        group.identity
        for group in groups
        if group.target_ids & completed_target_ids
    }
    ordered = sorted(groups, key=lambda row: (row.first_ms, row.last_ms))
    for index, current in enumerate(ordered):
        later = tuple(
            row for row in ordered[index + 1 :] if row.first_ms > current.last_ms
        )
        if not later:
            continue
        if any(
            row.name.strip().casefold() == current.name.strip().casefold()
            for row in later
        ):
            # The same Boss appearing again means the earlier attempt may have
            # wiped; do not normalize that partial attempt to a false kill.
            continue
        completed.add(current.identity)

    # The final encounter has no following Boss to prove completion.  Old
    # captures also lack an authoritative HP=0 packet, so use a deliberately
    # strict terminal signal: most calibrated HP was observed as damage and,
    # after damage stopped, multiple players kept moving for several seconds.
    # A normal wipe usually has neither the multi-player post-fight movement
    # nor enough observed damage to satisfy both gates.
    if ordered and terminal_skill_casts and fixed_boss_hp_references:
        final = ordered[-1]
        fixed_values = fixed_boss_hp_references.get(
            normalize_boss_hp_name(final.name), ()
        )
        fixed_max_hp = int(fixed_values[0]) if len(fixed_values) == 1 else 0
        observed_ratio = (
            final.damage / fixed_max_hp if fixed_max_hp > 0 else 0.0
        )
        terminal_casts = tuple(
            event
            for event in terminal_skill_casts
            if final.last_ms + 100 <= event.timestamp_ms <= final.last_ms + 15_000
            and canonical_skill(event.skill_name)
            in _TERMINAL_BOSS_COMPLETION_MOVEMENT_SKILLS
        )
        terminal_sources = {
            event.source_id for event in terminal_casts if event.source_id
        }
        terminal_span_ms = max(
            (event.timestamp_ms - final.last_ms for event in terminal_casts),
            default=0,
        )
        if (
            final.identity not in completed
            and observed_ratio >= 0.80
            and len(terminal_casts) >= 4
            and len(terminal_sources) >= 2
            and terminal_span_ms >= 5_000
        ):
            completed.add(final.identity)
    return frozenset(completed)

def _events_in_windows(
    events: tuple[CombatEvent | SkillCastEvent, ...],
    windows: tuple[tuple[float, float], ...],
    *,
    margin_seconds: float = 0.0,
) -> tuple[CombatEvent | SkillCastEvent, ...]:
    """Select chronological events with one linear pass over phase windows."""
    margin = max(0.0, float(margin_seconds))
    expanded = _merge_seconds_windows(
        [
            (max(0.0, start - margin), end + margin)
            for start, end in windows
        ]
    )
    if not expanded or not events:
        return ()

    selected: list[CombatEvent | SkillCastEvent] = []
    window_index = 0
    for event in events:
        seconds = event.timestamp_ms / 1000.0
        while (
            window_index < len(expanded)
            and seconds > expanded[window_index][1]
        ):
            window_index += 1
        if window_index >= len(expanded):
            break
        start, end = expanded[window_index]
        if start <= seconds <= end:
            selected.append(event)
    return tuple(selected)
