from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections import deque
from math import isfinite
from threading import RLock
from typing import Iterable

from ..team_support_rules import team_effect_duration_override
from .buff_clear import player_buff_is_clear_immune
from .aura_state import AURA_SKILL_KEYS
from .models import (
    BossActionEvent,
    BossStatusEvent,
    PlayerHealthEvent,
    PlayerLifecycleEvent,
)


@dataclass(slots=True)
class _TrackedPlayerBuff:
    event: BossStatusEvent
    observed_at: float
    expires_at: float | None


def _effect_value_signature(event: BossStatusEvent) -> tuple[object, str]:
    value = event.value
    if value is not None:
        try:
            value = round(float(value), 8)
        except (TypeError, ValueError):
            value = None
    return value, str(event.parameters or "")


class PrebattleStateTracker:
    """Small packet-backed state ledger used before the combat boundary.

    The automatic probe already decodes town/Garden traffic, but the combat
    decoder is intentionally recreated at the formal dungeon boundary.  This
    tracker retains only state that can legally cross that boundary: active
    player buffs.  Damage, casts and Boss debuffs remain probe-only.
    """

    def __init__(self) -> None:
        self._player_buffs: dict[tuple[object, ...], _TrackedPlayerBuff] = {}
        self._lock = RLock()
        # A handoff runs slightly after the click/map boundary. Retain a small
        # undo journal so a refresh/removal in that gap cannot erase the state
        # that really existed at the boundary. Never retain raw UDP traffic.
        self._changes: deque = deque(maxlen=8192)
        self._next_prune = 0.0

    def _store(self, key: tuple[object, ...], value: _TrackedPlayerBuff | None, now: float) -> None:
        self._changes.append((now, key, self._player_buffs.get(key)))
        if value is None:
            self._player_buffs.pop(key, None)
        else:
            self._player_buffs.pop(key, None)
            self._player_buffs[key] = value
            while len(self._player_buffs) > 4096:
                self._player_buffs.pop(next(iter(self._player_buffs)))

    @staticmethod
    def _key(event: BossStatusEvent) -> tuple[object, ...]:
        return (
            str(event.target_id or ""),
            str(event.source_id or ""),
            str(event.skill_id or ""),
            str(event.effect_key or ""),
            _effect_value_signature(event),
            event.effect_instance_id if event.skill_id in AURA_SKILL_KEYS else 0,
        )

    @staticmethod
    def _expires_at(event: BossStatusEvent, observed_at: float) -> float | None:
        if event.permanent:
            reviewed_duration = max(
                0.0, float(team_effect_duration_override(event.skill_name) or 0.0)
            )
            if reviewed_duration <= 0.0:
                return None
            return observed_at + reviewed_duration
        return observed_at + max(0.001, int(event.duration_ms or 0) / 1000.0)

    def _clear_target(self, target_id: object, now: float, *, positive_clear: bool = False, visibility_only: bool = False) -> None:
        if visibility_only:
            # A hidden party HP bar does not end the recipient's Buffs. This
            # must match live aggregation or the capture handoff loses Buffs
            # applied before the map/capture boundary.
            return
        target = str(target_id or "").strip()
        if not target:
            return
        for key, row in tuple(self._player_buffs.items()):
            if str(row.event.target_id or "") == target:
                if positive_clear and player_buff_is_clear_immune(
                    skill_id=row.event.skill_id, skill_name=row.event.skill_name, permanent=row.event.permanent,
                ):
                    continue
                self._store(key, None, now)

    def observe(self, events: Iterable[object], observed_at: float) -> None:
        with self._lock:
            self._observe(events, observed_at)

    def _observe(self, events: Iterable[object], observed_at: float) -> None:
        try:
            now = float(observed_at)
        except (TypeError, ValueError):
            return
        if not isfinite(now) or now <= 0.0:
            return
        for event in events:
            if isinstance(event, BossStatusEvent) and event.target_scope == "player":
                key = self._key(event)
                if event.operation == "remove":
                    self._store(key, None, now)
                    continue
                if not event.target_id or not event.skill_id:
                    continue
                self._store(key, _TrackedPlayerBuff(
                    event=event,
                    observed_at=now,
                    expires_at=self._expires_at(event, now),
                ), now)
                continue
            if isinstance(event, PlayerLifecycleEvent) and not event.active:
                self._clear_target(event.target_id, now, visibility_only=True)
                continue
            if (
                isinstance(event, PlayerHealthEvent)
                and not bool(getattr(event, "session_evidence", False))
                and int(event.current_hp or 0) <= 0
                and (
                    str(event.packet_kind or "")
                    == "s2c-udp-player-hp-state-086f-v576"
                    or int(event.max_hp or 0) > 0
                )
            ):
                self._clear_target(event.target_id, now)
                continue
            if (
                isinstance(event, BossActionEvent)
                and str(event.packet_kind or "") == "boss_buff_clear_status"
            ):
                for target_id in tuple(event.affected_target_ids or ()):
                    # Use the same reviewed immunity rule as live aggregation;
                    # Confirmed death above still clears every effect.
                    self._clear_target(target_id, now, positive_clear=True)
        if now >= self._next_prune:
            self._prune(now)
            self._next_prune = now + 1.0

    def _prune(self, now: float) -> None:
        self._player_buffs = {
            key: row
            for key, row in self._player_buffs.items()
            if row.expires_at is None or row.expires_at > now - 30.0
        }
        while self._changes and self._changes[0][0] < now - 30.0:
            self._changes.popleft()

    def snapshot(self, boundary_timestamp: float) -> dict[str, object]:
        with self._lock:
            return self._snapshot(boundary_timestamp)

    def _snapshot(self, boundary_timestamp: float) -> dict[str, object]:
        try:
            boundary = float(boundary_timestamp)
        except (TypeError, ValueError):
            return {"version": 1, "boundary_timestamp": 0.0, "player_buffs": []}
        if not isfinite(boundary) or boundary <= 0.0:
            return {"version": 1, "boundary_timestamp": 0.0, "player_buffs": []}
        state = dict(self._player_buffs)
        for timestamp, key, previous in reversed(self._changes):
            if timestamp > boundary:
                if previous is None:
                    state.pop(key, None)
                else:
                    state[key] = previous
        rows: list[dict[str, object]] = []
        for tracked in sorted(
            (
                row
                for row in state.values()
                # A client map-entry candidate is deliberately stabilized for
                # a short interval before recording starts.  Effects observed
                # during that interval are already present in the retained
                # combat frames and must not also be inherited at battle 0s.
                if row.observed_at <= boundary
                and (row.expires_at is None or row.expires_at > boundary)
            ),
            key=lambda row: (
                str(row.event.target_id or ""),
                str(row.event.source_id or ""),
                str(row.event.skill_id or ""),
                str(row.event.effect_key or ""),
            ),
        ):
            event = tracked.event
            if tracked.expires_at is None:
                inherited = replace(
                    event,
                    timestamp_ms=0,
                    duration_ms=0,
                    permanent=True,
                    prebattle_inherited=True,
                )
            else:
                remaining_ms = max(
                    1, round((tracked.expires_at - boundary) * 1000.0)
                )
                inherited = replace(
                    event,
                    timestamp_ms=0,
                    duration_ms=remaining_ms,
                    # A protocol "permanent" marker may have a reviewed finite
                    # lifetime.  Seed only the actual remainder at the boundary.
                    permanent=False,
                    prebattle_inherited=True,
                )
            rows.append(asdict(inherited))
        return {
            "version": 1,
            "boundary_timestamp": boundary,
            "player_buffs": rows,
        }


def inherited_player_buff_events(state: object) -> tuple[BossStatusEvent, ...]:
    """Validate and rebuild boundary-relative Buff events from metadata."""

    if not isinstance(state, dict) or int(state.get("version") or 0) != 1:
        return ()
    values = state.get("player_buffs")
    if not isinstance(values, list):
        return ()
    result: list[BossStatusEvent] = []
    field_names = set(BossStatusEvent.__dataclass_fields__)
    for value in values[:4096]:
        if not isinstance(value, dict):
            continue
        payload = {key: value[key] for key in field_names if key in value}
        try:
            event = BossStatusEvent(**payload)
        except (TypeError, ValueError):
            continue
        if event.skill_id in AURA_SKILL_KEYS:
            try:
                if min(int(event.source_id, 0), int(event.target_id, 0), int(event.effect_instance_id)) <= 0:
                    continue
                event = replace(event, effect_instance_id=int(event.effect_instance_id))
            except (TypeError, ValueError):
                continue
        if (
            event.target_scope != "player"
            or event.operation != "apply"
            or not event.target_id
            or not event.skill_id
            or event.timestamp_ms != 0
            or (not event.permanent and event.duration_ms <= 0)
            or (event.skill_id in AURA_SKILL_KEYS and (event.effect_instance_id <= 0 or event.effect_code == "cast_inferred_permanent"))
        ):
            continue
        result.append(replace(event, prebattle_inherited=True))
    return tuple(result)
