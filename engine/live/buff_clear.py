from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable, Mapping


# The four client-table protected effects plus Scorching Flame, which also
# survives Boss clear-positive-effects in the current game rules.
PLAYER_BUFF_CLEAR_IMMUNE_SKILL_IDS = frozenset(
    {"1551", "2208", "3351", "7512", "7612"}
)
PLAYER_BUFF_CLEAR_IMMUNE_SKILL_NAMES = frozenset(
    {"演出时间", "灼热火焰", "审判力量", "觉醒", "[噩梦]觉醒"}
)

# The score/timeline model deliberately keeps Buff-clear handling small and
# deterministic.  Only the two global timing windows are interrupted; ordinary
# personal/team Buffs continue to use their existing observed/planned intervals.
TIMELINE_PLAYER_BUFF_CLEAR_SKILLS = frozenset({"精神爆发", "时间加速"})


@lru_cache(maxsize=512)
def _player_buff_clear_affects_timeline_skill_text(text: str) -> bool:
    text = text.strip().replace(" EX", "EX")
    if not text:
        return False
    # Timeline rows may carry a provider prefix or a human-readable suffix.
    for separator in ("｜", "|"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1].strip()
    for prefix in ("团队·", "个人·"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    for skill in TIMELINE_PLAYER_BUFF_CLEAR_SKILLS:
        if text in {skill, f"{skill}EX"}:
            return True
        if text.startswith(skill):
            suffix = text[len(skill) :]
            if suffix.startswith((" ", "·", "（", "(", "-", "：", ":")):
                return True
    return False


def player_buff_clear_affects_timeline_skill(skill_name: object) -> bool:
    """Return whether a clear should interrupt this timeline timing window."""

    return _player_buff_clear_affects_timeline_skill_text(
        str(skill_name or "")
    )


def player_buff_is_clear_immune(
    *,
    skill_id: object = "",
    skill_name: object = "",
    permanent: bool = False,
) -> bool:
    """Return whether one positive player effect survives a Boss clear."""

    return bool(permanent) or (
        str(skill_id or "").strip() in PLAYER_BUFF_CLEAR_IMMUNE_SKILL_IDS
        or str(skill_name or "").strip()
        in PLAYER_BUFF_CLEAR_IMMUNE_SKILL_NAMES
    )


def player_buff_clear_times_by_target(
    events: Iterable[Any],
    *,
    origin_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> dict[str, tuple[float, ...]]:
    """Index explicit clear events by affected runtime actor.

    Times are returned on the requested local axis.  The function deliberately
    consumes only ``affected_target_ids``: a Boss action without a proven
    recipient must never erase somebody's Buffs by inference.
    """

    grouped: dict[str, set[float]] = defaultdict(set)
    origin = max(0.0, float(origin_seconds or 0.0))
    horizon = None if end_seconds is None else max(0.0, float(end_seconds))
    for event in events:
        timestamp_ms = max(0, int(getattr(event, "timestamp_ms", 0) or 0))
        seconds = timestamp_ms / 1000.0 - origin
        if seconds < -1e-6 or (horizon is not None and seconds > horizon + 1e-6):
            continue
        seconds = max(0.0, seconds)
        for target_id in getattr(event, "affected_target_ids", ()) or ():
            target = str(target_id or "").strip()
            if target:
                grouped[target].add(seconds)
    return {
        target: tuple(sorted(times))
        for target, times in grouped.items()
        if times
    }


def merged_player_buff_clear_times(
    clear_times_by_target: Mapping[str, Iterable[float]],
    target_ids: Iterable[object] = (),
) -> tuple[float, ...]:
    """Return the ordered clear points for selected recipients.

    An empty ``target_ids`` iterable intentionally means all indexed party
    members.  Team-score theory currently builds one axis per profession, so a
    party-wide Boss clear uses this union; the factual/actual replay continues
    to use recipient-specific status intervals.
    """

    selected = {
        str(value or "").strip() for value in target_ids if str(value or "").strip()
    }
    return tuple(
        sorted(
            {
                max(0.0, float(seconds))
                for target, rows in clear_times_by_target.items()
                if not selected or str(target) in selected
                for seconds in rows
            }
        )
    )


def latest_player_buff_clear(
    clear_times: Iterable[float],
    seconds: float,
) -> float | None:
    """Return the latest clear at or before ``seconds``."""

    rows = tuple(clear_times)
    if not rows:
        return None
    index = bisect_right(rows, float(seconds) + 1e-9) - 1
    return rows[index] if index >= 0 else None


def player_buff_survives_at(
    *,
    applied_at: float,
    seconds: float,
    clear_times: Iterable[float],
    skill_id: object = "",
    skill_name: object = "",
    permanent: bool = False,
) -> bool:
    """Return whether the current application is still active after clears."""

    if player_buff_is_clear_immune(
        skill_id=skill_id,
        skill_name=skill_name,
        permanent=permanent,
    ):
        return True
    latest = latest_player_buff_clear(clear_times, seconds)
    return latest is None or latest < float(applied_at) - 1e-6


def clip_player_buff_intervals_at_clears(
    intervals: Iterable[tuple[float, float]],
    clear_times: Iterable[float],
    *,
    skill_id: object = "",
    skill_name: object = "",
    permanent: bool = False,
) -> tuple[tuple[float, float], ...]:
    """End each finite application at its first later clear point.

    A later reapplication is represented by a later source interval and is
    therefore retained.  This is the key distinction between a clear and a
    permanent disable.
    """

    rows = tuple(sorted(max(0.0, float(value)) for value in clear_times))
    source = tuple(
        sorted(
            (
                max(0.0, float(start)),
                max(max(0.0, float(start)), float(end)),
            )
            for start, end in intervals
            if float(end) > float(start) + 1e-9
        )
    )
    if not rows or player_buff_is_clear_immune(
        skill_id=skill_id,
        skill_name=skill_name,
        permanent=permanent,
    ):
        return source

    clipped: list[tuple[float, float]] = []
    for start, end in source:
        stop = end
        index = bisect_right(rows, start + 1e-6)
        if index < len(rows) and rows[index] < end - 1e-9:
            stop = rows[index]
        if stop > start + 1e-9:
            clipped.append((start, stop))
    return tuple(clipped)
