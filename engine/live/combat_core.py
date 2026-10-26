from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CombatClock:
    """One zero-based battle clock for live and replay state projections."""

    origin_ms: int
    factual_end_ms: int
    current_ms: int
    elapsed_ms: int

    @property
    def elapsed_seconds(self) -> float:
        return max(0.001, self.elapsed_ms / 1000.0)


def resolve_combat_clock(
    *,
    first_ms: int | None,
    last_ms: int | None,
    requested_current_ms: int | None,
    live_clock: bool,
) -> CombatClock:
    """Resolve time identically for collector snapshots and replay snapshots.

    The decoder has already converted packets to a zero-based battle axis.
    Replay ends at the last factual event. Live mode may advance beyond that
    event while the player is idle or dead so Buff/Debuff expiry still moves.
    """

    origin_ms = max(0, int(first_ms or 0))
    factual_end_ms = max(origin_ms, int(last_ms or origin_ms))
    current_ms = max(
        0,
        int(
            factual_end_ms
            if requested_current_ms is None
            else requested_current_ms
        ),
    )
    elapsed_end_ms = (
        max(factual_end_ms, current_ms) if live_clock else factual_end_ms
    )
    return CombatClock(
        origin_ms=origin_ms,
        factual_end_ms=factual_end_ms,
        current_ms=current_ms,
        elapsed_ms=max(1, elapsed_end_ms - origin_ms),
    )


def snapshot_clock_ms(
    *,
    elapsed_seconds: float,
    dps_elapsed_seconds: float,
    effect_current_seconds: float,
) -> int:
    """Read the canonical current time from a materialized snapshot."""

    return max(
        0,
        round(max(0.0, float(elapsed_seconds)) * 1000.0),
        round(max(0.0, float(dps_elapsed_seconds)) * 1000.0),
        round(max(0.0, float(effect_current_seconds)) * 1000.0),
    )


def snapshot_recording_end_ms(snapshot: object) -> int:
    """Return the factual recording tail without constructing score indexes.

    Aggregator snapshots are already in stable timestamp order. Reading their
    tails keeps live timeline refresh O(number of streams/players), while the
    heavier grouped event index is reserved for scoring.
    """

    end_ms = snapshot_clock_ms(
        elapsed_seconds=getattr(snapshot, "elapsed_seconds", 0.0),
        dps_elapsed_seconds=getattr(snapshot, "dps_elapsed_seconds", 0.0),
        effect_current_seconds=getattr(
            snapshot, "boss_effect_current_seconds", 0.0
        ),
    )
    for field in (
        "combat_events",
        "incoming_damage_events",
        "skill_cast_events",
        "player_lifecycle_events",
    ):
        rows = getattr(snapshot, field, ())
        if rows:
            end_ms = max(end_ms, int(rows[-1].timestamp_ms))
    buff_events = getattr(snapshot, "player_buff_events", ())
    if buff_events:
        end_ms = max(
            end_ms, round(buff_events[-1].timestamp_seconds * 1000.0)
        )
    for track in getattr(snapshot, "player_health_tracks", ()):
        if track.points:
            end_ms = max(
                end_ms, round(track.points[-1].timestamp_seconds * 1000.0)
            )
    return max(0, end_ms)
