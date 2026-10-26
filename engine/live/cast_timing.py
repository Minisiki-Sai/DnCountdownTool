from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..data_loader import DataStore, as_number, norm
from ..job_mechanics import JOB_MECHANICS
from ..team_support_rules import corrected_team_effect_duration
from .buff_clear import (
    latest_player_buff_clear,
    player_buff_clear_affects_timeline_skill,
    player_buff_is_clear_immune,
)
from .models import SkillCastEvent


@dataclass(frozen=True, slots=True)
class ResolvedCastTiming:
    cast_time: float
    action_speed_percent: float
    end_seconds: float


@dataclass(slots=True)
class _ActionSpeedWindow:
    skill: str
    source_id: str
    team_wide: bool
    start_seconds: float
    end_seconds: float
    speed_percent: float


class LiveCastTimingResolver:
    """Resolve observed cast lengths from the shared action-speed timeline."""

    _SPEED_EFFECTS: dict[str, tuple[float, float, bool]] = {
        "精神爆发": (40.0, 14.0, True),
        "灼热火焰": (40.0, 14.0, True),
        "精确瞄准": (30.0, 18.0, False),
        "致残惩罚者": (30.0, 10.0, False),
        "幽灵复仇者": (30.0, 18.0, False),
        "幻影凝视": (15.0, 10.0, False),
    }
    _SPEED_PATTERN = re.compile(
        r"(?:动作|行动|攻击)速度(?:增加|提高|提升)?\s*([0-9]+(?:\.[0-9]+)?)%"
    )
    _SPEED_SKILL_SCOPES: dict[str, frozenset[str]] = {
        "幻影凝视": frozenset(
            {
                "追踪者",
                "黄昏猎人",
                "废除者",
                "风车公主",
                "唐·弗莱德将军",
                "艾云尼风暴",
            }
        ),
    }

    def __init__(
        self,
        store: DataStore | None,
        buff_clear_times_by_target: Mapping[str, Iterable[float]] | None = None,
        *,
        recipient_speed_windows: Mapping[
            tuple[str, str], Iterable[tuple[float, float]]
        ] | None = None,
        authoritative_team_speed: bool = False,
    ) -> None:
        self.store = store
        self.windows: list[_ActionSpeedWindow] = []
        self._last_cast_by_source: dict[str, tuple[str, float]] = {}
        self._authoritative_team_speed = bool(authoritative_team_speed)
        self._buff_clear_times_by_target: dict[str, tuple[float, ...]] = {}
        self.set_player_buff_clear_times(buff_clear_times_by_target or {})
        self.set_recipient_speed_windows(recipient_speed_windows or {})

    def reset(self) -> None:
        self.windows.clear()
        self._last_cast_by_source.clear()
        self._buff_clear_times_by_target.clear()

    def set_player_buff_clear_times(
        self,
        clear_times_by_target: Mapping[str, Iterable[float]],
    ) -> None:
        """Replace the recipient-specific explicit Buff-clear index."""

        self._buff_clear_times_by_target = {
            str(target_id): tuple(
                sorted({max(0.0, float(seconds)) for seconds in rows})
            )
            for target_id, rows in clear_times_by_target.items()
            if str(target_id) and rows
        }

    def set_recipient_speed_windows(
        self,
        recipient_speed_windows: Mapping[
            tuple[str, str], Iterable[tuple[float, float]]
        ],
    ) -> None:
        """Seed authoritative team-speed windows for their real recipients."""

        for (target_id, raw_skill), intervals in recipient_speed_windows.items():
            skill = norm(raw_skill).replace(" EX", "EX")
            effect = self._SPEED_EFFECTS.get(skill) or self._SPEED_EFFECTS.get(
                skill.removesuffix("EX")
            )
            if effect is None or not effect[2] or not target_id:
                continue
            speed_percent = effect[0]
            for start, end in intervals:
                start_seconds = max(0.0, float(start))
                end_seconds = max(start_seconds, float(end))
                if end_seconds <= start_seconds + 1e-9:
                    continue
                self.windows.append(
                    _ActionSpeedWindow(
                        skill=skill,
                        source_id=str(target_id),
                        team_wide=False,
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        speed_percent=speed_percent,
                    )
                )

    def active_speed(self, event: SkillCastEvent, seconds: float) -> float:
        grouped: dict[str, float] = {}
        clear_times = self._buff_clear_times_by_target.get(event.source_id, ())
        latest_clear = latest_player_buff_clear(clear_times, seconds)
        for window in self.windows:
            if not (window.start_seconds <= seconds < window.end_seconds):
                continue
            if not window.team_wide and window.source_id != event.source_id:
                continue
            if (
                latest_clear is not None
                and latest_clear >= window.start_seconds - 1e-6
                and player_buff_clear_affects_timeline_skill(window.skill)
                and not player_buff_is_clear_immune(skill_name=window.skill)
            ):
                # A later application creates a new window whose start is after
                # the clear, so only the stale pre-clear application is removed.
                continue
            scope = self._SPEED_SKILL_SCOPES.get(window.skill.removesuffix("EX"))
            event_skill = norm(event.skill_name).replace(" EX", "EX").removesuffix("EX")
            if scope and event_skill not in scope:
                continue
            grouped[window.skill] = max(
                grouped.get(window.skill, 0.0), window.speed_percent
            )
        return sum(grouped.values())

    def _timing_row(self, event: SkillCastEvent) -> dict[str, object]:
        if self.store is None:
            return {}
        return self.store.skill_cast_time(event.skill_name, [event.job])

    def cast_time(self, event: SkillCastEvent, seconds: float) -> tuple[float, float]:
        timing = self._timing_row(event)
        cast_time = as_number(timing.get("cast_time"))
        quick_time = as_number(timing.get("quick_time"))
        skill_key = norm(event.skill_name).replace(" ", "").upper().removesuffix("EX")
        combo_rules = JOB_MECHANICS.job_value(
            norm(event.job), "combos.quick_predecessors", {}
        )
        allowed = set()
        if isinstance(combo_rules, dict):
            for target, predecessors in combo_rules.items():
                target_key = norm(target).replace(" ", "").upper().removesuffix("EX")
                if target_key != skill_key:
                    continue
                allowed = {
                    norm(value).replace(" ", "").upper().removesuffix("EX")
                    for value in predecessors
                    if norm(value)
                }
                break
        using_quick = quick_time > 0
        if allowed:
            previous_skill, previous_end = self._last_cast_by_source.get(
                event.source_id, ("", float("-inf"))
            )
            using_quick = (
                previous_skill in allowed
                and previous_end - 0.25 <= seconds <= previous_end + 0.25
            )
        base_time = quick_time if using_quick else cast_time
        action_speed = self.active_speed(event, seconds)
        if base_time <= 0:
            return 0.0, action_speed
        effective = self.store.effective_skill_cast_time(
            timing, action_speed, quick=using_quick
        )
        return max(0.0, effective), action_speed

    def register_speed_effect(
        self, event: SkillCastEvent, cast_end_seconds: float
    ) -> None:
        skill = norm(event.skill_name).replace(" EX", "EX")
        effect = self._SPEED_EFFECTS.get(skill) or self._SPEED_EFFECTS.get(
            skill.removesuffix("EX")
        )
        duration_from_data = 0.0
        description = ""
        if self.store is not None:
            timing = self.store.skill_effect_timing(
                event.job, event.skill_name, max_required_level=70
            )
            duration_from_data = corrected_team_effect_duration(
                skill,
                max(0.0, as_number(timing.get("duration"))),
            )
            description = norm(timing.get("desc"))
        if effect is None:
            match = self._SPEED_PATTERN.search(description)
            if match is None or duration_from_data <= 0:
                return
            effect = (as_number(match.group(1)), duration_from_data, False)
        speed_percent, fallback_duration, team_wide = effect
        if team_wide and self._authoritative_team_speed:
            # Team Buff casts prove that the provider acted, but not that every
            # party member was in range. Recipient status intervals seeded at
            # construction time are the sole authority during scoring.
            return
        duration = duration_from_data or fallback_duration
        if speed_percent <= 0 or duration <= 0:
            return
        self.windows.append(
            _ActionSpeedWindow(
                skill=skill,
                source_id=event.source_id,
                team_wide=team_wide,
                start_seconds=cast_end_seconds,
                end_seconds=cast_end_seconds + duration,
                speed_percent=speed_percent,
            )
        )

    def resolve(self, event: SkillCastEvent, seconds: float) -> ResolvedCastTiming:
        cast_time, action_speed = self.cast_time(event, seconds)
        if event.job == '狂战士' and str(event.skill_id) in {'407','505'} and getattr(event,'variant_key','') in {'normal','true'}:
            cast_time = (135.0 if event.variant_key == 'normal' else 196.0) / 60 / max(.1,1+action_speed/100)
        end_seconds = seconds + cast_time
        self.register_speed_effect(event, end_seconds)
        skill_key = norm(event.skill_name).replace(" ", "").upper().removesuffix("EX")
        self._last_cast_by_source[event.source_id] = (skill_key, end_seconds)
        return ResolvedCastTiming(cast_time, action_speed, end_seconds)
