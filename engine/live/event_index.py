from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterable, TypeVar

from .combat_core import snapshot_clock_ms
from .models import (
    BossActionEvent,
    CombatEvent,
    CombatSnapshot,
    PlayerHealthTrackSnapshot,
    PlayerLifecycleEvent,
    SkillCastEvent,
    StatusEventSnapshot,
)


_T = TypeVar("_T")
_EVENT_INDEX_CACHE_LIMIT = 2
_EVENT_INDEX_CACHE: OrderedDict[
    int, tuple[CombatSnapshot, "CombatEventIndex"]
] = OrderedDict()
_EVENT_INDEX_LOCK = RLock()
_LAST_EVENT_INDEX: tuple[CombatSnapshot, "CombatEventIndex"] | None = None


def _stable_time_order(
    rows: Iterable[_T],
    timestamp: Callable[[_T], int | float],
) -> tuple[_T, ...]:
    """Return rows in factual packet order without disturbing equal timestamps."""

    resolved = tuple(rows)
    if all(
        timestamp(resolved[index - 1]) <= timestamp(resolved[index])
        for index in range(1, len(resolved))
    ):
        return resolved
    return tuple(sorted(resolved, key=timestamp))


def _group(
    rows: Iterable[_T],
    key: Callable[[_T], str],
) -> dict[str, tuple[_T, ...]]:
    grouped: dict[str, list[_T]] = defaultdict(list)
    for row in rows:
        grouped[str(key(row))].append(row)
    return {group_key: tuple(values) for group_key, values in grouped.items()}


@dataclass(frozen=True, slots=True)
class CombatEventIndex:
    """Canonical, immutable projection shared by replay and live consumers.

    The decoder/aggregator remains the single producer of combat state.  This
    projection only gives every downstream consumer the same zero-based clock,
    stable event order and pre-grouped lookup tables.  It deliberately does
    not infer, drop or rename events, so building an index cannot change score
    inputs.
    """

    snapshot: CombatSnapshot
    combat_events: tuple[CombatEvent, ...]
    damage_events: tuple[CombatEvent, ...]
    healing_events: tuple[CombatEvent, ...]
    incoming_damage_events: tuple[CombatEvent, ...]
    skill_cast_events: tuple[SkillCastEvent, ...]
    boss_action_events: tuple[BossActionEvent, ...]
    boss_effect_events: tuple[StatusEventSnapshot, ...]
    player_buff_events: tuple[StatusEventSnapshot, ...]
    player_lifecycle_events: tuple[PlayerLifecycleEvent, ...]
    combat_by_source: dict[str, tuple[CombatEvent, ...]]
    combat_by_target: dict[str, tuple[CombatEvent, ...]]
    incoming_by_target: dict[str, tuple[CombatEvent, ...]]
    healing_by_target: dict[str, tuple[CombatEvent, ...]]
    casts_by_source: dict[str, tuple[SkillCastEvent, ...]]
    boss_effects_by_target: dict[str, tuple[StatusEventSnapshot, ...]]
    player_buffs_by_target: dict[str, tuple[StatusEventSnapshot, ...]]
    health_tracks_by_target: dict[str, PlayerHealthTrackSnapshot]
    current_ms: int
    recording_end_ms: int

    @classmethod
    def build(cls, snapshot: CombatSnapshot) -> "CombatEventIndex":
        combat_events = _stable_time_order(
            getattr(snapshot, "combat_events", ()), lambda row: row.timestamp_ms
        )
        incoming_events = _stable_time_order(
            getattr(snapshot, "incoming_damage_events", ()),
            lambda row: row.timestamp_ms,
        )
        cast_events = _stable_time_order(
            getattr(snapshot, "skill_cast_events", ()),
            lambda row: row.timestamp_ms,
        )
        boss_actions = _stable_time_order(
            getattr(snapshot, "boss_action_events", ()),
            lambda row: row.timestamp_ms,
        )
        boss_effect_events = _stable_time_order(
            getattr(snapshot, "boss_effect_events", ()),
            lambda row: row.timestamp_seconds,
        )
        player_buff_events = _stable_time_order(
            getattr(snapshot, "player_buff_events", ()),
            lambda row: row.timestamp_seconds,
        )
        lifecycle_events = _stable_time_order(
            getattr(snapshot, "player_lifecycle_events", ()),
            lambda row: row.timestamp_ms,
        )
        damage_events = tuple(
            row for row in combat_events if not row.healing and row.amount > 0
        )
        healing_events = tuple(
            row for row in combat_events if row.healing and row.amount > 0
        )

        # This is the one battle clock used by live and replay projections.
        # Live snapshots advance ``elapsed_seconds`` and the effect clock while
        # idle; replay snapshots end at the latest factual packet.
        current_ms = snapshot_clock_ms(
            elapsed_seconds=getattr(snapshot, "elapsed_seconds", 0.0),
            dps_elapsed_seconds=getattr(snapshot, "dps_elapsed_seconds", 0.0),
            effect_current_seconds=getattr(
                snapshot, "boss_effect_current_seconds", 0.0
            ),
        )

        # Keep the historical recording-end definition byte-for-byte in
        # spirit: it includes post-output deaths and HP states, but does not
        # extend scoring merely because an inspect/profile packet arrived.
        recording_end_ms = current_ms
        for rows in (
            combat_events,
            incoming_events,
            cast_events,
            lifecycle_events,
        ):
            if rows:
                recording_end_ms = max(
                    recording_end_ms, int(getattr(rows[-1], "timestamp_ms"))
                )
        if player_buff_events:
            recording_end_ms = max(
                recording_end_ms,
                round(player_buff_events[-1].timestamp_seconds * 1000.0),
            )
        for track in getattr(snapshot, "player_health_tracks", ()):
            if track.points:
                recording_end_ms = max(
                    recording_end_ms,
                    round(
                        max(point.timestamp_seconds for point in track.points)
                        * 1000.0
                    ),
                )

        return cls(
            snapshot=snapshot,
            combat_events=combat_events,
            damage_events=damage_events,
            healing_events=healing_events,
            incoming_damage_events=incoming_events,
            skill_cast_events=cast_events,
            boss_action_events=boss_actions,
            boss_effect_events=boss_effect_events,
            player_buff_events=player_buff_events,
            player_lifecycle_events=lifecycle_events,
            combat_by_source=_group(combat_events, lambda row: row.source_id),
            combat_by_target=_group(combat_events, lambda row: row.target_id),
            incoming_by_target=_group(incoming_events, lambda row: row.target_id),
            healing_by_target=_group(healing_events, lambda row: row.target_id),
            casts_by_source=_group(cast_events, lambda row: row.source_id),
            boss_effects_by_target=_group(
                boss_effect_events, lambda row: row.target_id
            ),
            player_buffs_by_target=_group(
                player_buff_events, lambda row: row.target_id
            ),
            health_tracks_by_target={
                str(track.target_id): track
                for track in getattr(snapshot, "player_health_tracks", ())
            },
            current_ms=current_ms,
            recording_end_ms=max(0, recording_end_ms),
        )


def event_index_for(snapshot: CombatSnapshot) -> CombatEventIndex:
    """Reuse one event projection for every player scored from a snapshot."""

    global _LAST_EVENT_INDEX
    # Scoring repeatedly asks for the same immutable snapshot from a single
    # worker thread. Avoid lock/dictionary overhead on that overwhelmingly
    # common path; the locked identity cache remains the cross-thread path.
    last = _LAST_EVENT_INDEX
    if last is not None and last[0] is snapshot:
        return last[1]
    cache_key = id(snapshot)
    with _EVENT_INDEX_LOCK:
        cached = _EVENT_INDEX_CACHE.get(cache_key)
        if cached is not None and cached[0] is snapshot:
            _EVENT_INDEX_CACHE.move_to_end(cache_key)
            _LAST_EVENT_INDEX = cached
            return cached[1]

    built = CombatEventIndex.build(snapshot)
    with _EVENT_INDEX_LOCK:
        cached = _EVENT_INDEX_CACHE.get(cache_key)
        if cached is not None and cached[0] is snapshot:
            _EVENT_INDEX_CACHE.move_to_end(cache_key)
            _LAST_EVENT_INDEX = cached
            return cached[1]
        _EVENT_INDEX_CACHE[cache_key] = (snapshot, built)
        _LAST_EVENT_INDEX = (snapshot, built)
        _EVENT_INDEX_CACHE.move_to_end(cache_key)
        while len(_EVENT_INDEX_CACHE) > _EVENT_INDEX_CACHE_LIMIT:
            _EVENT_INDEX_CACHE.popitem(last=False)
    return built


def clear_event_index_cache() -> None:
    global _LAST_EVENT_INDEX
    with _EVENT_INDEX_LOCK:
        _EVENT_INDEX_CACHE.clear()
        _LAST_EVENT_INDEX = None
