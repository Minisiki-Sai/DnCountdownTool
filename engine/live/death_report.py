from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .models import CombatEvent, CombatSnapshot, SkillCastEvent


DEFAULT_BEFORE_SECONDS = 30.0
DEFAULT_AFTER_SECONDS = 20.0
_ANONYMOUS_NAME = re.compile(r"^(?:角色\s*)?0x[0-9a-f]+$", re.IGNORECASE)


def _display_text(value: object, fallback: str = "") -> str:
    text = str(value or "").strip()
    return fallback if not text or _ANONYMOUS_NAME.match(text) else text


def _event_row(event: CombatEvent | SkillCastEvent, death_ms: int) -> dict[str, Any]:
    timestamp_ms = max(0, int(event.timestamp_ms))
    row = {
        "timestamp_ms": timestamp_ms,
        "seconds": timestamp_ms / 1000.0,
        "relative_seconds": (timestamp_ms - death_ms) / 1000.0,
        "source_id": str(getattr(event, "source_id", "") or ""),
        "source_name": str(getattr(event, "source_name", "") or ""),
        "job": str(getattr(event, "job", "") or ""),
        "skill_id": str(getattr(event, "skill_id", "") or ""),
        "skill_name": str(getattr(event, "skill_name", "") or ""),
    }
    if isinstance(event, CombatEvent):
        row.update(
            amount=max(0, int(event.amount)),
            healing=bool(event.healing),
            target_id=str(event.target_id or ""),
            target_name=str(event.target_name or ""),
            target_job=str(event.target_job or ""),
        )
    return row


def _confirmed_deaths(snapshot: CombatSnapshot) -> tuple[CombatEvent, ...]:
    rows: list[CombatEvent] = []
    seen: set[tuple[str, int]] = set()
    for event in snapshot.combat_events:
        if str(event.attribution_method or "").casefold() != "player_death_hp_state_086f":
            continue
        actor_id = str(event.target_id or event.source_id or "")
        key = (actor_id, int(event.timestamp_ms))
        if not actor_id or key in seen:
            continue
        seen.add(key)
        rows.append(event)
    return tuple(sorted(rows, key=lambda row: (row.timestamp_ms, row.target_id)))


def build_death_reports(
    snapshot: CombatSnapshot,
    *,
    before_seconds: float = DEFAULT_BEFORE_SECONDS,
    after_seconds: float = DEFAULT_AFTER_SECONDS,
) -> list[dict[str, Any]]:
    """Build one compact, actionable report per protocol-confirmed death.

    The report intentionally contains only HP/damage, the dead player's casts
    and received healing.  It reuses the immutable snapshot and therefore has
    no independent death inference that could disagree with scoring.
    """

    before_ms = round(max(0.0, float(before_seconds)) * 1000.0)
    after_ms = round(max(0.0, float(after_seconds)) * 1000.0)
    players = {row.source_id: row for row in snapshot.players}
    health_tracks = {row.target_id: row for row in snapshot.player_health_tracks}
    party_members: dict[str, list[Any]] = defaultdict(list)
    for row in snapshot.party_members:
        party_members[row.actor_id].append(row)
    casts_by_actor: dict[str, list[SkillCastEvent]] = defaultdict(list)
    for row in snapshot.skill_cast_events:
        casts_by_actor[row.source_id].append(row)
    for rows in casts_by_actor.values():
        rows.sort(key=lambda row: row.timestamp_ms)

    reports: list[dict[str, Any]] = []
    per_actor_ordinal: dict[str, int] = defaultdict(int)
    for ordinal, death in enumerate(_confirmed_deaths(snapshot), 1):
        death_ms = int(death.timestamp_ms)
        actor_id = str(death.target_id or death.source_id)
        per_actor_ordinal[actor_id] += 1
        track = health_tracks.get(actor_id)
        player = players.get(actor_id)
        member_rows = sorted(
            party_members.get(actor_id, ()),
            key=lambda row: (
                float(getattr(row, "confidence", 0.0) or 0.0),
                bool(getattr(row, "name", "")),
                bool(getattr(row, "job", "")),
            ),
            reverse=True,
        )
        member = member_rows[0] if member_rows else None
        job = str(
            getattr(member, "job", "")
            or getattr(player, "job", "")
            or getattr(track, "target_job", "")
            or death.target_job
            or "未知职业"
        )
        name = _display_text(
            getattr(member, "name", "")
            or getattr(player, "name", "")
            or getattr(track, "target_name", "")
            or death.target_name,
            job,
        )
        maximum = max(0, int(getattr(track, "max_hp", 0) or 0))
        start_ms = death_ms - before_ms
        end_ms = death_ms + after_ms

        health_points: list[dict[str, Any]] = []
        previous_hp: int | None = None
        previous_positive: dict[str, Any] | None = None
        next_positive: dict[str, Any] | None = None
        for point in sorted(
            tuple(getattr(track, "points", ()) or ()),
            key=lambda row: row.timestamp_seconds,
        ):
            timestamp_ms = round(float(point.timestamp_seconds) * 1000.0)
            current_hp = max(0, int(point.current_hp))
            change = current_hp - previous_hp if previous_hp is not None else None
            previous_hp = current_hp
            raw = {
                "timestamp_ms": timestamp_ms,
                "seconds": timestamp_ms / 1000.0,
                "relative_seconds": (timestamp_ms - death_ms) / 1000.0,
                "current_hp": current_hp,
                "hp_change": change,
                "percent": (
                    max(0.0, min(100.0, current_hp / maximum * 100.0))
                    if maximum > 0
                    else None
                ),
            }
            if current_hp > 0 and timestamp_ms < death_ms:
                previous_positive = raw
            elif current_hp > 0 and timestamp_ms > death_ms and next_positive is None:
                next_positive = raw
            if start_ms <= timestamp_ms <= end_ms:
                health_points.append(raw)

        actor_casts = casts_by_actor.get(actor_id, [])
        previous_cast = next(
            (row for row in reversed(actor_casts) if row.timestamp_ms <= death_ms),
            None,
        )
        next_cast = next(
            (row for row in actor_casts if row.timestamp_ms > death_ms),
            None,
        )
        reports.append(
            {
                "ordinal": ordinal,
                "actor_death_ordinal": per_actor_ordinal[actor_id],
                "death_timestamp_ms": death_ms,
                "death_seconds": death_ms / 1000.0,
                "before_seconds": before_ms / 1000.0,
                "after_seconds": after_ms / 1000.0,
                "actor_id": actor_id,
                "name": name,
                "job": job,
                "max_hp": maximum,
                "max_hp_source": str(
                    getattr(track, "max_hp_source", "") or "missing"
                ),
                "health_points": health_points,
                "previous_positive": previous_positive,
                "next_positive": next_positive,
                "incoming_damage": [
                    _event_row(row, death_ms)
                    for row in snapshot.incoming_damage_events
                    if row.target_id == actor_id
                    and start_ms <= row.timestamp_ms <= death_ms
                ],
                "actor_casts": [
                    _event_row(row, death_ms)
                    for row in actor_casts
                    if start_ms <= row.timestamp_ms <= end_ms
                ],
                "previous_cast": (
                    _event_row(previous_cast, death_ms)
                    if previous_cast is not None
                    else None
                ),
                "next_cast": (
                    _event_row(next_cast, death_ms)
                    if next_cast is not None
                    else None
                ),
                "healing": [
                    _event_row(row, death_ms)
                    for row in snapshot.combat_events
                    if row.healing
                    and row.target_id == actor_id
                    and start_ms <= row.timestamp_ms <= end_ms
                ],
            }
        )
    return reports
