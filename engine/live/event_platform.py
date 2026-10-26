from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from dataclasses import MISSING, dataclass, fields
from types import MappingProxyType
from threading import RLock
from typing import Any, Iterable, Mapping, TypeVar

from .event_index import CombatEventIndex, event_index_for
from .models import CombatSnapshot


_T = TypeVar("_T")
_EVENT_PLATFORM_CACHE_LIMIT = 2
_EVENT_PLATFORM_CACHE: OrderedDict[
    int, tuple[object, "CombatEventPlatform"]
] = OrderedDict()
_EVENT_PLATFORM_LOCK = RLock()
_LAST_EVENT_PLATFORM: tuple[object, "CombatEventPlatform"] | None = None
_SNAPSHOT_FIELDS_BY_NAME = MappingProxyType(
    {field.name: field for field in fields(CombatSnapshot)}
)
_SNAPSHOT_FIELD_NAMES = frozenset(_SNAPSHOT_FIELDS_BY_NAME)
_REQUIRED_FIELD_DEFAULTS: Mapping[str, Any] = MappingProxyType(
    {
        "elapsed_seconds": 0.0,
        "total_damage": 0,
        "total_healing": 0,
        "players": (),
        "damage_series": (),
    }
)


def _snapshot_default(name: str) -> Any:
    if name in _REQUIRED_FIELD_DEFAULTS:
        return _REQUIRED_FIELD_DEFAULTS[name]
    model_field = _SNAPSHOT_FIELDS_BY_NAME[name]
    if model_field.default is not MISSING:
        return model_field.default
    if model_field.default_factory is not MISSING:
        return model_field.default_factory()
    raise AttributeError(name)


def _snapshot_value(snapshot: object, name: str) -> Any:
    if name not in _SNAPSHOT_FIELD_NAMES:
        raise AttributeError(name)
    try:
        return getattr(snapshot, name)
    except AttributeError:
        return _snapshot_default(name)


def _event_timestamp_ms(row: object) -> int:
    timestamp_ms = getattr(row, "timestamp_ms", None)
    if timestamp_ms is not None:
        return max(0, int(timestamp_ms))
    return max(0, round(float(getattr(row, "timestamp_seconds", 0.0)) * 1000.0))


def _group_rows(
    rows: Iterable[_T],
    attribute: str,
) -> Mapping[str, tuple[_T, ...]]:
    grouped: dict[str, list[_T]] = defaultdict(list)
    for row in rows:
        grouped[str(getattr(row, attribute, ""))].append(row)
    return MappingProxyType(
        {key: tuple(values) for key, values in grouped.items()}
    )


def _group_multi_rows(
    rows: Iterable[_T],
    attribute: str,
) -> Mapping[str, tuple[_T, ...]]:
    grouped: dict[str, list[_T]] = defaultdict(list)
    for row in rows:
        raw_keys = getattr(row, attribute, ())
        keys = raw_keys if isinstance(raw_keys, (list, tuple, set)) else (raw_keys,)
        for raw_key in keys:
            key = str(raw_key)
            if key:
                grouped[key].append(row)
    return MappingProxyType(
        {key: tuple(values) for key, values in grouped.items()}
    )


def _ordered_rows(rows: Iterable[_T]) -> tuple[_T, ...]:
    resolved = tuple(rows)
    timestamps = tuple(_event_timestamp_ms(row) for row in resolved)
    if all(
        timestamps[index - 1] <= timestamps[index]
        for index in range(1, len(timestamps))
    ):
        return resolved
    return tuple(sorted(resolved, key=_event_timestamp_ms))


@dataclass(frozen=True, slots=True)
class CombatEventPlatform:
    """One immutable read model for every live/replay combat consumer.

    The decoder and :class:`CombatAggregator` remain the only producers of
    facts.  This object is deliberately a zero-transform facade over one
    ``CombatSnapshot`` plus its stable lookup index: raw snapshot fields keep
    their original tuple identity and order, while explicitly named
    ``ordered_*`` fields and query methods use the canonical stable order.

    Keeping those two views separate is the compatibility boundary that lets
    scoring, timelines, Boss statistics and overlays migrate independently
    without silently changing an existing calculation.
    """

    snapshot: object
    event_index: CombatEventIndex
    streams: Mapping[str, tuple[Any, ...]]
    stream_timestamps_ms: Mapping[str, tuple[int, ...]]
    player_by_id: Mapping[str, Any]
    damage_target_by_id: Mapping[str, Any]
    boss_hp_by_instance: Mapping[str, Any]
    health_track_by_target: Mapping[str, Any]
    party_members_by_actor: Mapping[str, tuple[Any, ...]]
    profiles_by_target: Mapping[str, tuple[Any, ...]]
    buff_clears_by_target: Mapping[str, tuple[Any, ...]]
    summon_removals_by_owner: Mapping[str, tuple[Any, ...]]

    @classmethod
    def build(cls, snapshot: object) -> "CombatEventPlatform":
        index = event_index_for(snapshot)
        player_buff_clear_events = _snapshot_value(
            snapshot, "player_buff_clear_events"
        )
        summon_removal_events = _snapshot_value(snapshot, "summon_removal_events")
        party_members = _snapshot_value(snapshot, "party_members")
        player_profiles = _snapshot_value(snapshot, "player_profiles")
        players = _snapshot_value(snapshot, "players")
        damage_targets = _snapshot_value(snapshot, "damage_targets")
        boss_hp_tracks = _snapshot_value(snapshot, "boss_hp_tracks")
        player_health_tracks = _snapshot_value(snapshot, "player_health_tracks")
        streams: dict[str, tuple[Any, ...]] = {
            # Ordered factual event streams.  Equal timestamps retain packet
            # order because CombatEventIndex uses Python's stable sort.
            "combat": index.combat_events,
            "damage": index.damage_events,
            "healing": index.healing_events,
            "incoming_damage": index.incoming_damage_events,
            "skill_cast": index.skill_cast_events,
            "skill_parameter": _ordered_rows(
                _snapshot_value(snapshot, "skill_parameter_events")
            ),
            "boss_action": index.boss_action_events,
            "boss_effect": index.boss_effect_events,
            "player_buff": index.player_buff_events,
            "player_lifecycle": index.player_lifecycle_events,
            # These streams are already emitted in factual packet order by the
            # aggregator.  Do not rewrite, infer, merge or deduplicate them.
            "player_buff_clear": _ordered_rows(player_buff_clear_events),
            "summon_removal": _ordered_rows(summon_removal_events),
            "party_member": _ordered_rows(party_members),
            "player_profile": _ordered_rows(player_profiles),
        }
        stream_timestamps = {
            name: tuple(_event_timestamp_ms(row) for row in rows)
            for name, rows in streams.items()
        }
        return cls(
            snapshot=snapshot,
            event_index=index,
            streams=MappingProxyType(streams),
            stream_timestamps_ms=MappingProxyType(stream_timestamps),
            player_by_id=MappingProxyType(
                {str(row.source_id): row for row in players}
            ),
            damage_target_by_id=MappingProxyType(
                {str(row.target_id): row for row in damage_targets}
            ),
            boss_hp_by_instance=MappingProxyType(
                {str(row.instance_id): row for row in boss_hp_tracks}
            ),
            health_track_by_target=MappingProxyType(
                {
                    str(row.target_id): row
                    for row in player_health_tracks
                }
            ),
            party_members_by_actor=_group_rows(party_members, "actor_id"),
            profiles_by_target=_group_rows(player_profiles, "target_id"),
            buff_clears_by_target=_group_multi_rows(
                player_buff_clear_events, "affected_target_ids"
            ),
            summon_removals_by_owner=_group_rows(
                summon_removal_events, "owner_id"
            ),
        )

    @property
    def current_ms(self) -> int:
        return self.event_index.current_ms

    def __getattr__(self, name: str) -> Any:
        """Expose raw snapshot state through the one platform boundary.

        Consumers migrating from ``snapshot.<field>`` can use
        ``platform.<field>`` without a semantic change.  Unknown attributes
        still fail normally instead of being silently treated as empty.
        """

        if name in _SNAPSHOT_FIELD_NAMES:
            return _snapshot_value(self.snapshot, name)
        raise AttributeError(name)

    @property
    def recording_end_ms(self) -> int:
        return self.event_index.recording_end_ms

    @property
    def ordered_combat_events(self) -> tuple[Any, ...]:
        return self.event_index.combat_events

    @property
    def ordered_incoming_damage_events(self) -> tuple[Any, ...]:
        return self.event_index.incoming_damage_events

    @property
    def ordered_skill_cast_events(self) -> tuple[Any, ...]:
        return self.event_index.skill_cast_events

    @property
    def ordered_boss_action_events(self) -> tuple[Any, ...]:
        return self.event_index.boss_action_events

    @property
    def ordered_boss_effect_events(self) -> tuple[Any, ...]:
        return self.event_index.boss_effect_events

    @property
    def ordered_player_buff_events(self) -> tuple[Any, ...]:
        return self.event_index.player_buff_events

    @property
    def ordered_player_lifecycle_events(self) -> tuple[Any, ...]:
        return self.event_index.player_lifecycle_events

    def field(self, name: str) -> Any:
        """Return an original snapshot field without changing its value/order."""

        if name not in _SNAPSHOT_FIELD_NAMES:
            raise KeyError(f"unknown CombatSnapshot field: {name}")
        return _snapshot_value(self.snapshot, name)

    def stream(self, name: str) -> tuple[Any, ...]:
        try:
            return self.streams[name]
        except KeyError as exc:
            raise KeyError(f"unknown combat event stream: {name}") from exc

    def window(
        self,
        name: str,
        start_ms: int,
        end_ms: int,
        *,
        include_end: bool = True,
    ) -> tuple[Any, ...]:
        """Read an event window in O(log n + result size), preserving order."""

        if end_ms < start_ms:
            return ()
        rows = self.stream(name)
        timestamps = self.stream_timestamps_ms[name]
        left = bisect_left(timestamps, max(0, int(start_ms)))
        right = (
            bisect_right(timestamps, max(0, int(end_ms)))
            if include_end
            else bisect_left(timestamps, max(0, int(end_ms)))
        )
        return rows[left:right]

    def by_source(self, name: str, source_id: object) -> tuple[Any, ...]:
        """Return one source's rows without forcing consumers to rescan history."""

        key = str(source_id)
        indexed = {
            "combat": self.event_index.combat_by_source,
            "damage": self.event_index.combat_by_source,
            "skill_cast": self.event_index.casts_by_source,
        }.get(name)
        if indexed is not None:
            rows = indexed.get(key, ())
            if name == "damage":
                return tuple(
                    row for row in rows if not row.healing and row.amount > 0
                )
            return rows
        return tuple(
            row
            for row in self.stream(name)
            if str(getattr(row, "source_id", "")) == key
        )

    def by_target(self, name: str, target_id: object) -> tuple[Any, ...]:
        """Return one target's rows from the canonical indexed event view."""

        key = str(target_id)
        indexed = {
            "combat": self.event_index.combat_by_target,
            "incoming_damage": self.event_index.incoming_by_target,
            "healing": self.event_index.healing_by_target,
            "boss_effect": self.event_index.boss_effects_by_target,
            "player_buff": self.event_index.player_buffs_by_target,
            "player_buff_clear": self.buff_clears_by_target,
        }.get(name)
        if indexed is not None:
            return indexed.get(key, ())
        return tuple(
            row
            for row in self.stream(name)
            if str(getattr(row, "target_id", "")) == key
        )

    def assert_lossless(self) -> None:
        """Fail fast if the platform ever mutates a raw snapshot projection."""

        for name in _SNAPSHOT_FIELD_NAMES:
            projected = self.field(name)
            original = _snapshot_value(self.snapshot, name)
            if projected is not original and projected != original:
                raise AssertionError(
                    f"event platform changed CombatSnapshot.{name}"
                )


def event_platform_for(snapshot: object) -> CombatEventPlatform:
    """Return the shared event platform for one immutable snapshot identity."""

    global _LAST_EVENT_PLATFORM
    last = _LAST_EVENT_PLATFORM
    if last is not None and last[0] is snapshot:
        return last[1]

    cache_key = id(snapshot)
    with _EVENT_PLATFORM_LOCK:
        cached = _EVENT_PLATFORM_CACHE.get(cache_key)
        if cached is not None and cached[0] is snapshot:
            _EVENT_PLATFORM_CACHE.move_to_end(cache_key)
            _LAST_EVENT_PLATFORM = cached
            return cached[1]

    built = CombatEventPlatform.build(snapshot)
    with _EVENT_PLATFORM_LOCK:
        cached = _EVENT_PLATFORM_CACHE.get(cache_key)
        if cached is not None and cached[0] is snapshot:
            _EVENT_PLATFORM_CACHE.move_to_end(cache_key)
            _LAST_EVENT_PLATFORM = cached
            return cached[1]
        _EVENT_PLATFORM_CACHE[cache_key] = (snapshot, built)
        _LAST_EVENT_PLATFORM = (snapshot, built)
        _EVENT_PLATFORM_CACHE.move_to_end(cache_key)
        while len(_EVENT_PLATFORM_CACHE) > _EVENT_PLATFORM_CACHE_LIMIT:
            _EVENT_PLATFORM_CACHE.popitem(last=False)
    return built


def clear_event_platform_cache() -> None:
    global _LAST_EVENT_PLATFORM
    with _EVENT_PLATFORM_LOCK:
        _EVENT_PLATFORM_CACHE.clear()
        _LAST_EVENT_PLATFORM = None
