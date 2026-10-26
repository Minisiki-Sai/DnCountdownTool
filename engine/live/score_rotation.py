from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for
from .score_burst import original_buff_duration
from .mechanic_damage import is_mechanic_damage_skill

class ScoreRotationMixin:
    @staticmethod
    def _alchemist_used_sacrifice(
        snapshot: CombatSnapshot, source_id: str
    ) -> bool:
        """Accept an action or applied buff, never a damage-derived pseudo-cast."""
        from .combat_skill_levels import _USAGE_ACTION_KINDS

        platform = event_platform_for(snapshot)
        skill_key = canonical_skill(_ALCHEMIST_SACRIFICE_SKILL)
        if any(
            event.packet_kind in _USAGE_ACTION_KINDS
            and (
                canonical_skill(event.skill_name) == skill_key
                or str(event.skill_id) == "4404"
            )
            for event in platform.event_index.casts_by_source.get(source_id, ())
        ):
            return True
        return any(
            row.source_id == source_id
            and canonical_skill(row.skill_name) == skill_key
            for row in (*platform.player_buff_series, *platform.player_buffs)
        )

    @staticmethod
    def _is_dot_damage_event(event: CombatEvent) -> bool:
        """Return whether a hit is a periodic status tick rather than an action hit."""
        skill_id = norm(event.skill_id).casefold()
        attribution = norm(event.attribution_method).casefold()
        return skill_id.startswith("periodic:") or attribution.startswith("periodic_")
    @staticmethod
    def _is_output_window_mechanic_skill(skill_id: str) -> bool:
        """Encounter weapons are real damage, not player-output evidence."""
        key = norm(skill_id).casefold()
        return is_mechanic_damage_skill(key)

    @staticmethod
    def _is_output_window_anchor(event: CombatEvent) -> bool:
        """Return whether a hit proves that a player can actively attack the Boss.

        Residual status hits, ordinary/special-attack fallbacks, summons and
        persistent skill objects still contribute damage inside a confirmed
        output window. They must not independently open or extend one because
        those hits can continue while every player is inside a Boss mechanic.
        Encounter-owned transformed attacks likewise remain in the damage
        ledger, but cannot prove that ordinary player skills are available.
        """
        if TeamScoreEngine._is_dot_damage_event(event):
            return False
        skill_id = norm(event.skill_id).casefold()
        attribution = norm(event.attribution_method).casefold()
        return (
            skill_id not in _OUTPUT_WINDOW_RESIDUAL_SKILL_IDS
            and not TeamScoreEngine._is_output_window_mechanic_skill(skill_id)
            and not skill_id.startswith("supplemental:")
            and attribution not in _OUTPUT_WINDOW_RESIDUAL_ATTRIBUTIONS
        )
    @staticmethod
    def _output_windows(
        snapshot: CombatSnapshot,
        boss_target_ids: frozenset[str] | None = None,
    ) -> tuple[tuple[float, float], ...]:
        platform = event_platform_for(snapshot)
        primary_target_ids = boss_target_ids or frozenset()
        has_target_scope = boss_target_ids is not None or bool(platform.damage_targets)
        if boss_target_ids is None and platform.damage_targets:
            # Use the same encounter list as replay scoring/Boss statistics,
            # including all runtime entities belonging to each recognized Boss.
            from .boss_recognizer import BossRecognizer

            primary_target_ids = frozenset(
                target_id
                for encounter in BossRecognizer().recognize_snapshot(snapshot)
                for target_id in encounter.target_ids
            )
        index = platform.event_index
        boss_damage_events = tuple(
            event
            for event in index.damage_events
            if (
                not has_target_scope
                or event.target_id in primary_target_ids
            )
        )
        # A mandatory objective can take real damage while the Boss is blocked.
        # Keep that damage in the factual ledger, but never let an unselected
        # target open, sustain or bridge the Boss output denominator. Only old
        # snapshots without any target catalog retain unscoped compatibility.
        return TeamScoreEngine._output_windows_from_events(
            boss_damage_events,
            index.skill_cast_events,
            (
                player.source_id
                for player in platform.players
            ),
            selected_target=has_target_scope,
            fallback_end_seconds=max(
                platform.dps_elapsed_seconds,
                platform.elapsed_seconds,
            ),
        )

    @staticmethod
    def _output_windows_from_events(
        matching_damage_events: Iterable[CombatEvent],
        skill_cast_events: Iterable[SkillCastEvent],
        roster_source_ids: Iterable[str] = (),
        *,
        selected_target: bool = True,
        fallback_end_seconds: float = 0.0,
    ) -> tuple[tuple[float, float], ...]:
        """Resolve output windows from event rows shared by replay and live.

        The live page deliberately omits raw events from its JSON payload, but
        its aggregator still owns the factual event ledger.  Keeping the
        window algorithm here lets that lightweight path use exactly the same
        mechanic-gap, residual-hit and party-consensus rules as final scoring.
        """
        damage_events = tuple(matching_damage_events)
        anchors = sorted(
            [
                (
                    event.timestamp_ms / 1000.0,
                    event,
                )
                for event in damage_events
                if TeamScoreEngine._is_output_window_anchor(event)
            ],
            key=lambda row: row[0],
        )
        if not anchors:
            # Captured damage without an active action anchor is residual
            # damage, not evidence that this Boss phase is output-capable.
            if damage_events or selected_target:
                return ()
            end = max(0.0, float(fallback_end_seconds))
            return ((0.0, end),) if end > 0 else ()

        active_sources = {
            norm(event.source_id)
            for _seconds, event in anchors
            if norm(event.source_id)
        }
        roster_sources = {
            norm(source_id)
            for source_id in roster_source_ids
            if norm(source_id)
        }
        multi_player_capture = len(active_sources | roster_sources) > 1

        # A single-player capture has no team consensus signal, so preserve
        # the original direct-hit behavior. In a party, opening a window needs
        # either two players attacking close together or a cast-backed dense
        # sequence from one player. This prevents one old projectile or an
        # unpaired fallback hit from reopening a Boss mechanic phase.
        strong_ranges: list[tuple[float, float]] = []
        cast_times_by_source: dict[str, list[float]] = defaultdict(list)
        cast_times_by_skill: dict[tuple[str, str], list[float]] = defaultdict(list)
        for cast in skill_cast_events:
            # Even when an encounter weapon has a factual action row, it must
            # not provide either the cast-backed tail or the two-cast evidence
            # used to open a normal output phase.
            if TeamScoreEngine._is_output_window_mechanic_skill(cast.skill_id):
                continue
            source_id = norm(cast.source_id)
            if source_id:
                cast_seconds = cast.timestamp_ms / 1000.0
                cast_times_by_source[source_id].append(cast_seconds)
                skill_name = norm(cast.skill_name).casefold()
                if skill_name:
                    cast_times_by_skill[(source_id, skill_name)].append(cast_seconds)
        for values in cast_times_by_source.values():
            values.sort()
        for values in cast_times_by_skill.values():
            values.sort()

        recent: deque[tuple[float, str]] = deque()
        recent_counts: Counter[str] = Counter()
        recent_by_source: dict[str, deque[float]] = defaultdict(deque)
        medium_seconds: set[float] = set()

        for seconds, event in anchors:
            source_id = norm(event.source_id)
            cutoff = seconds - _OUTPUT_WINDOW_TEAM_CONFIRM_SECONDS
            while recent and recent[0][0] < cutoff:
                _old_seconds, old_source = recent.popleft()
                recent_counts[old_source] -= 1
                if recent_counts[old_source] <= 0:
                    del recent_counts[old_source]
            recent.append((seconds, source_id))
            recent_counts[source_id] += 1

            source_recent = recent_by_source[source_id]
            while source_recent and source_recent[0] < cutoff:
                source_recent.popleft()
            source_recent.append(seconds)

            cast_confirmed = False
            cast_times = cast_times_by_source.get(source_id, ())
            if cast_times:
                position = bisect_right(
                    cast_times, seconds + _OUTPUT_WINDOW_CAST_LEAD_SECONDS
                ) - 1
                cast_confirmed = (
                    position >= 0
                    and cast_times[position]
                    >= seconds - _OUTPUT_WINDOW_CAST_LOOKBACK_SECONDS
                )
            if cast_confirmed:
                medium_seconds.add(seconds)

            # A multi-hit action from one player is still one piece of
            # evidence. During Boss mechanics, low-value attacks can produce
            # several direct packets (for example Side Kick) even though the
            # party cannot resume normal output. Count independent casts in
            # the confirmation window instead of treating hit count alone as
            # team activity.
            recent_cast_count = 0
            if cast_times:
                cast_left = bisect_left(cast_times, cutoff)
                cast_right = bisect_right(
                    cast_times, seconds + _OUTPUT_WINDOW_CAST_LEAD_SECONDS
                )
                recent_cast_count = max(0, cast_right - cast_left)

            exact_cast_confirmed = False
            exact_cast_times = cast_times_by_skill.get(
                (source_id, norm(event.skill_name).casefold()), ()
            )
            if exact_cast_times:
                position = bisect_right(
                    exact_cast_times,
                    seconds + _OUTPUT_WINDOW_CAST_LEAD_SECONDS,
                ) - 1
                exact_cast_confirmed = (
                    position >= 0
                    and exact_cast_times[position]
                    >= seconds - _OUTPUT_WINDOW_CAST_LOOKBACK_SECONDS
                )

            if not multi_player_capture:
                strong_ranges.append((seconds, seconds))
            elif (
                len(active_sources) <= 1
                and
                exact_cast_confirmed
                and norm(event.attribution_method).casefold()
                in _OUTPUT_WINDOW_DIRECT_ATTRIBUTIONS
            ):
                strong_ranges.append((seconds, seconds))
            elif len(recent_counts) >= 2:
                strong_ranges.append((recent[0][0], seconds))
            elif (
                cast_confirmed
                and len(source_recent) >= _OUTPUT_WINDOW_SINGLE_SOURCE_HITS
                and recent_cast_count >= 2
            ):
                strong_ranges.append((source_recent[0], seconds))

        merged_strong_ranges = _merge_intervals(
            strong_ranges,
            join_gap=0.001,
        )
        if not merged_strong_ranges:
            # Some old or partial captures contain only one attacking source
            # even though the roster was decoded. They still have an
            # unambiguous single-player timeline and should remain usable.
            if len(active_sources) <= 1:
                merged_strong_ranges = tuple(
                    (seconds, seconds) for seconds, _event in anchors
                )
            else:
                return ()

        strong_seconds = {
            seconds
            for seconds, _event in anchors
            if _in_intervals(seconds, merged_strong_ranges)
        }
        first_strong = next(
            (
                seconds
                for seconds, _event in anchors
                if seconds in strong_seconds
            ),
            None,
        )
        if first_strong is None:
            return ()

        windows: list[tuple[float, float]] = []
        start = max(0.0, first_strong)
        previous = first_strong
        for current, _event in anchors:
            if current <= first_strong:
                continue
            is_strong = current in strong_seconds
            is_cast_confirmed = current in medium_seconds
            if is_strong:
                if current - previous > _OUTPUT_WINDOW_SPLIT_GAP_SECONDS:
                    windows.append(
                        (start, previous + _OUTPUT_WINDOW_END_GRACE_SECONDS)
                    )
                    start = max(0.0, current)
                previous = current
            elif (
                is_cast_confirmed
                and current - previous <= _OUTPUT_WINDOW_SPLIT_GAP_SECONDS
            ):
                # Once team activity has opened a phase, a confirmed cast from
                # one player may keep it alive. It cannot open a new phase by
                # itself after the configured confirmed-silence gap.
                previous = current
        windows.append((start, previous + _OUTPUT_WINDOW_END_GRACE_SECONDS))
        return _merge_intervals(windows, join_gap=0.1)
    def _actual_casts(
        self,
        snapshot: CombatSnapshot,
        source_id: str,
        profiles: dict[str, SkillProfile],
        output_windows: tuple[tuple[float, float], ...],
        *,
        job: str = "",
        infer_missing_from_damage: bool = True,
    ) -> tuple[list[_ObservedCast], float, int, int, int, int]:
        rows: list[_ObservedCast] = []
        last_direct: dict[str, float] = {}
        direct_row_keys: set[str] = set()
        direct_ranges: dict[str, list[tuple[float, float]]] = defaultdict(list)
        damage_times_by_key: dict[str, list[float]] = defaultdict(list)
        periodic_damage_keys = set(_scoring_periodic_damage_durations())
        summon_durations = {
            key: duration
            for key, (_maximum, duration) in _scoring_summon_limits(job).items()
        }
        event_index = event_platform_for(snapshot).event_index
        source_events = event_index.casts_by_source.get(source_id, ())
        explicit_action_times: list[float] = []
        explicit_action_keys: list[str] = []
        last_explicit_action: dict[str, float] = {}
        for event in source_events:
            packet_kind = norm(event.packet_kind).casefold() or "skill_cast"
            if packet_kind not in _EXPLICIT_INTERRUPT_PACKET_KINDS:
                continue
            seconds = event.timestamp_ms / 1000.0
            key = canonical_skill(event.skill_name)
            if seconds - last_explicit_action.get(key, -999.0) < 0.12:
                continue
            last_explicit_action[key] = seconds
            explicit_action_times.append(seconds)
            explicit_action_keys.append(key)
        for damage_event in event_index.combat_by_source.get(source_id, ()):
            if (
                damage_event.source_id != source_id
                or damage_event.healing
                or damage_event.amount <= 0
            ):
                continue
            damage_key = canonical_skill(damage_event.skill_name)
            if damage_key in profiles:
                damage_times_by_key[damage_key].append(
                    damage_event.timestamp_ms / 1000.0
                )
        cast_timings = self._observed_cast_timings(snapshot)
        for event in source_events:
            seconds = event.timestamp_ms / 1000.0
            key = canonical_skill(event.skill_name)
            if key not in profiles:
                continue
            # The form activation is a support action with no direct damage.
            # Only its separately identified transformed attacks belong to the
            # output ledger.
            if key in _TRANSFORMED_ATTACK_BUFF_ONLY_SKILLS:
                continue
            if seconds - last_direct.get(key, -999.0) < 0.12:
                continue
            last_direct[key] = seconds
            cast_time, action_speed = cast_timings.get(
                (event.source_id, event.timestamp_ms, key),
                (0.0, 0.0),
            )
            active_end = seconds + max(
                0.12,
                cast_time,
                profiles[key].cast_time,
            )
            periodic_duration = (
                _scoring_periodic_damage_duration(key, profiles[key])
                if key in periodic_damage_keys
                else summon_durations.get(key, 0.0)
            )
            damage_end = active_end + periodic_duration
            direct_ranges[key].append(
                (
                    seconds - 0.25,
                    max(active_end + 0.75, damage_end + 0.10),
                )
            )
            has_output_hit = any(
                seconds - 0.25
                <= hit_seconds
                <= max(active_end + 0.75, damage_end + 0.10)
                and _in_intervals(hit_seconds, output_windows)
                for hit_seconds in damage_times_by_key.get(key, ())
            )
            periodic_output_overlap = (
                periodic_duration > 0
                and _interval_duration(
                    _intersect_intervals(
                        ((active_end, damage_end),),
                        output_windows,
                    )
                )
                > 1e-9
            )
            if (
                _in_intervals(seconds, output_windows)
                or has_output_hit
                or periodic_output_overlap
            ):
                interrupt_seconds = 0.0
                if key in _proportional_channel_skills(job):
                    action_index = bisect_right(
                        explicit_action_times,
                        seconds + 0.05,
                    )
                    while action_index < len(explicit_action_times):
                        if explicit_action_keys[action_index] != key:
                            interrupt_seconds = explicit_action_times[action_index]
                            break
                        action_index += 1
                rows.append(
                    _ObservedCast(
                        seconds,
                        key,
                        "直接施法",
                        cast_time,
                        action_speed,
                        interrupt_seconds,
                    )
                )
                direct_row_keys.add(key)

        # A legacy caller may omit the shared team axis. Reuse the same
        # evidence resolver instead of maintaining a third damage-clustering
        # algorithm with another per-skill denylist.
        if infer_missing_from_damage:
            evidence_result = TimelineEvidenceResolver(self.store).resolve(
                tuple(source_events),
                tuple(event_index.combat_by_source.get(source_id, ())),
            )
            known_row_times = {
                (row.key, round(row.seconds, 3)) for row in rows
            }
            for event in evidence_result.recovered_casts:
                key = canonical_skill(event.skill_name)
                seconds = event.timestamp_ms / 1000.0
                row_key = (key, round(seconds, 3))
                if (
                    key not in profiles
                    or row_key in known_row_times
                    or not _in_intervals(seconds, output_windows)
                ):
                    continue
                rows.append(_ObservedCast(seconds, key, "证据重建"))
                known_row_times.add(row_key)
        rows.sort(key=lambda row: (row.seconds, row.key))
        observed_keys = {row.key for row in rows}
        direct_ratio = (
            len(direct_row_keys) / len(observed_keys) if observed_keys else 0.0
        )
        direct_cast_count = sum(row.source == "直接施法" for row in rows)
        inferred_cast_count = len(rows) - direct_cast_count
        return (
            rows,
            direct_ratio,
            len(direct_row_keys),
            len(observed_keys),
            direct_cast_count,
            inferred_cast_count,
        )
    def _actual_prep_context_timeline(
        self,
        snapshot: CombatSnapshot,
        source_id: str,
        profiles: dict[str, SkillProfile],
        output_windows: tuple[tuple[float, float], ...],
        burst_windows: tuple[tuple[float, float], ...],
        top_keys: set[str],
    ) -> tuple[ScoreTimelineCast, ...]:
        """Keep setup and captured support casts visible without scoring them."""
        if not output_windows:
            return ()
        context_windows = tuple(
            (max(0.0, start - _OUTPUT_PREP_CONTEXT_SECONDS), start)
            for start, _end in output_windows
            if start > 0
        )
        player = next(
            (
                row
                for row in event_platform_for(snapshot).players
                if row.source_id == source_id
            ),
            None,
        )
        player_job = norm(player.job) if player is not None else ""
        job_names = {
            value
            for value in (player_job, data_job_for(player_job))
            if value
        }
        learned_skill_keys = {
            canonical_skill(skill)
            for job_name in job_names
            for skill in self.store.full_skill_names(job_name)
            if canonical_skill(skill)
        }
        support_skill_keys = {
            skill_key
            for row in self.store.tables.get("buff", [])
            if norm(row.get("类别")) in {"个人buff", "团队buff"}
            if (skill_key := canonical_skill(row.get("技能")))
            if (
                skill_key in learned_skill_keys
                or norm(row.get("来源")) in job_names
            )
        }
        cast_timings = self._observed_cast_timings(snapshot)
        rows: list[ScoreTimelineCast] = []
        seen: dict[str, float] = {}
        for event in event_platform_for(
            snapshot
        ).event_index.casts_by_source.get(source_id, ()):
            seconds = event.timestamp_ms / 1000.0
            key = canonical_skill(event.skill_name)
            in_prep_context = _in_intervals(seconds, context_windows)
            in_output_support = (
                key in support_skill_keys
                and _in_intervals(seconds, output_windows)
            )
            if not in_prep_context and not in_output_support:
                continue
            profile = profiles.get(key)
            if seconds - seen.get(key, -999.0) < 0.12:
                continue
            seen[key] = seconds
            fallback_time = profile.cast_time if profile is not None else 0.0
            cast_time, action_speed = cast_timings.get(
                (event.source_id, event.timestamp_ms, key),
                (fallback_time, 0.0),
            )
            duration = max(0.0, cast_time or fallback_time)
            rows.append(
                ScoreTimelineCast(
                    seconds=seconds,
                    end_seconds=seconds + duration,
                    skill_name=(profile.name if profile is not None else event.skill_name),
                    power=0.0,
                    source=(
                        "实战增益施放（不计直接输出）"
                        if in_output_support
                        else "Boss起手准备（不计输出评分）"
                    ),
                    high_value=key in top_keys,
                    in_burst_window=_in_intervals(seconds, burst_windows),
                    expected_duration=duration,
                    utilized_duration=duration,
                    action_speed_percent=action_speed,
                )
            )
        return tuple(rows)
    @staticmethod
    def _value_actual_casts(
        casts: list[_ObservedCast],
        profiles: dict[str, SkillProfile],
        output_windows: tuple[tuple[float, float], ...],
        job: str = "",
        modifier_windows: tuple[tuple[float, float, float, float], ...] = (),
        theory_plan: dict[str, Any] | None = None,
        source_id: str = "",
        summon_removal_events: tuple[SummonRemovalEvent, ...] = (),
    ) -> list[_ValuedCast]:
        """Value channels and limited summons by their effective active time."""
        summon_limits = _scoring_summon_limits(job)
        periodic_durations = _scoring_periodic_damage_durations()
        refreshable_periodic_keys = set(
            _scoring_refreshable_periodic_durations()
        )
        summon_end_by_index: dict[int, float] = {}
        periodic_end_by_index: dict[int, float] = {}
        damage_start_by_index: dict[int, float] = {}
        indices_by_summon: dict[str, list[int]] = defaultdict(list)
        indices_by_periodic: dict[str, list[int]] = defaultdict(list)
        exact_periodic_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
        used_exact_periodic_rows: dict[str, set[int]] = defaultdict(set)
        exact_cast_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
        used_exact_cast_rows: dict[str, set[int]] = defaultdict(set)
        if isinstance(theory_plan, dict) and source_id:
            replay_by_source = theory_plan.get("_score_actual_periodic_segments", {})
            source_rows = (
                replay_by_source.get(str(source_id), ())
                if isinstance(replay_by_source, dict)
                else ()
            )
            time_offset = max(
                0.0, as_number(theory_plan.get("_score_time_offset"))
            )
            for row in source_rows if isinstance(source_rows, (list, tuple)) else ():
                if not isinstance(row, dict):
                    continue
                key = canonical_skill(row.get("skill"))
                damage = max(0.0, as_number(row.get("_score_actual_damage")))
                # Every periodic row in this replay map has already been valued
                # by the long-simulation engine with its job multiplier, buffs,
                # debuffs and Boss output windows. Restricting this path to the
                # handful of summons that dynamically inherit attack buffs made
                # Lightning Relic fall back to its one-cast DPS-page value.
                if key not in periodic_durations or damage <= 0:
                    continue
                exact_periodic_rows[key].append(
                    (time_offset + max(0.0, as_number(row.get("start"))), damage)
                )
            for rows in exact_periodic_rows.values():
                rows.sort()
            cast_replay_by_source = theory_plan.get("_score_actual_cast_segments", {})
            cast_source_rows = (
                cast_replay_by_source.get(str(source_id), ())
                if isinstance(cast_replay_by_source, dict)
                else ()
            )
            for row in (
                cast_source_rows
                if isinstance(cast_source_rows, (list, tuple))
                else ()
            ):
                if not isinstance(row, dict):
                    continue
                key = canonical_skill(row.get("skill"))
                damage = max(0.0, as_number(row.get("_score_actual_damage")))
                if not key or damage <= 0:
                    continue
                exact_cast_rows[key].append(
                    (time_offset + max(0.0, as_number(row.get("start"))), damage)
                )
            for rows in exact_cast_rows.values():
                rows.sort()
        for index, cast in enumerate(casts):
            profile = profiles[cast.key]
            source = norm(cast.source).casefold()
            direct_cast = source in {"直接施法", "direct"}
            damage_start_by_index[index] = cast.seconds + (
                max(0.12, cast.expected_duration, profile.cast_time)
                if direct_cast
                else 0.0
            )
            if cast.key in summon_limits:
                indices_by_summon[cast.key].append(index)
            if cast.key in periodic_durations:
                indices_by_periodic[cast.key].append(index)

        for key, indices in indices_by_summon.items():
            maximum, duration = summon_limits[key]
            active: list[int] = []
            for index in indices:
                start = damage_start_by_index[index]
                active = [
                    active_index
                    for active_index in active
                    if summon_end_by_index[active_index] > start + 1e-9
                ]
                if len(active) >= maximum:
                    oldest = min(active, key=damage_start_by_index.__getitem__)
                    summon_end_by_index[oldest] = start
                    active.remove(oldest)
                summon_end_by_index[index] = start + duration
                active.append(index)

        # Preserve the no-removal lifetime because the exact periodic replay
        # has already applied Boss output windows and ordinary replacement.
        # Only the additional factual early-death ratio may be applied to that
        # exact value later.
        summon_baseline_end_by_index = dict(summon_end_by_index)
        if summon_removal_events and indices_by_summon:
            removals_by_skill: dict[str, list[float]] = defaultdict(list)
            for event in summon_removal_events:
                if source_id and str(getattr(event, "owner_id", "")) != str(source_id):
                    continue
                skill_key = canonical_skill(getattr(event, "skill_name", ""))
                if skill_key not in indices_by_summon:
                    continue
                removals_by_skill[skill_key].append(
                    max(0.0, as_number(getattr(event, "timestamp_ms", 0)) / 1000.0)
                )
            for key, removal_times in removals_by_skill.items():
                maximum, duration = summon_limits[key]
                resolved_ends: dict[int, float] = {}
                active: list[int] = []
                lifecycle_rows = [
                    (damage_start_by_index[index], 1, order, index)
                    for order, index in enumerate(indices_by_summon[key])
                ]
                lifecycle_rows.extend(
                    (seconds, 0, order, None)
                    for order, seconds in enumerate(removal_times)
                )
                lifecycle_rows.sort(key=lambda row: (row[0], row[1], row[2]))
                for seconds, _priority, _order, index in lifecycle_rows:
                    active = [
                        active_index
                        for active_index in active
                        if resolved_ends[active_index] > seconds + 1e-9
                    ]
                    if index is None:
                        if active:
                            oldest = min(
                                active,
                                key=damage_start_by_index.__getitem__,
                            )
                            resolved_ends[oldest] = seconds
                            active.remove(oldest)
                        continue
                    if len(active) >= maximum:
                        oldest = min(
                            active,
                            key=damage_start_by_index.__getitem__,
                        )
                        resolved_ends[oldest] = seconds
                        active.remove(oldest)
                    resolved_ends[index] = seconds + duration
                    active.append(index)
                summon_end_by_index.update(resolved_ends)

        for key, indices in indices_by_periodic.items():
            for offset, index in enumerate(indices):
                profile = profiles[casts[index].key]
                duration = _scoring_periodic_damage_duration(
                    key,
                    profile,
                )
                natural_end = damage_start_by_index[index] + duration
                if key in refreshable_periodic_keys:
                    next_cast = (
                        damage_start_by_index[indices[offset + 1]]
                        if offset + 1 < len(indices)
                        else float("inf")
                    )
                    natural_end = min(natural_end, next_cast)
                periodic_end_by_index[index] = natural_end

        result: list[_ValuedCast] = []
        for index, cast in enumerate(casts):
            profile = profiles[cast.key]
            stance_rule = _scoring_stance_action_rule(job, cast.key)
            summon_limit = summon_limits.get(cast.key)
            periodic_duration = (
                _scoring_periodic_damage_duration(cast.key, profile)
                if cast.key in periodic_durations
                else None
            )
            expected_duration = max(
                0.12,
                (
                    summon_limit[1]
                    if summon_limit
                    else (
                        periodic_duration
                        if periodic_duration is not None
                        else (
                            max(0.0, as_number(stance_rule.get("duration")))
                            if stance_rule
                            else cast.expected_duration or profile.cast_time
                        )
                    )
                ),
            )
            proportional = (
                cast.key in _proportional_channel_skills(job)
                or summon_limit is not None
                or periodic_duration is not None
            )
            maintained_duration = expected_duration
            utilized_duration = expected_duration
            baseline_utilized_duration = expected_duration
            if summon_limit is not None:
                active_end = summon_end_by_index.get(
                    index, damage_start_by_index[index] + expected_duration
                )
                utilized_duration = _interval_duration(
                    _intersect_intervals(
                        ((damage_start_by_index[index], active_end),),
                        output_windows,
                    )
                )
                baseline_active_end = summon_baseline_end_by_index.get(
                    index,
                    damage_start_by_index[index] + expected_duration,
                )
                baseline_utilized_duration = _interval_duration(
                    _intersect_intervals(
                        ((damage_start_by_index[index], baseline_active_end),),
                        output_windows,
                    )
                )
            elif periodic_duration is not None:
                active_end = periodic_end_by_index.get(
                    index, damage_start_by_index[index] + expected_duration
                )
                utilized_duration = _interval_duration(
                    _intersect_intervals(
                        ((damage_start_by_index[index], active_end),),
                        output_windows,
                    )
                )
            elif proportional:
                next_seconds = next(
                    (
                        row.seconds
                        for row in casts[index + 1 :]
                        if row.seconds > cast.seconds + 0.05
                    ),
                    float("inf"),
                )
                explicit_interrupt = (
                    cast.interrupt_seconds
                    if cast.interrupt_seconds > cast.seconds + 0.05
                    else float("inf")
                )
                cutoff = min(
                    cast.seconds + expected_duration,
                    explicit_interrupt
                    if math.isfinite(explicit_interrupt)
                    else next_seconds,
                )
                maintained_duration = max(
                    0.0,
                    min(expected_duration, cutoff - cast.seconds),
                )
                utilized_duration = _interval_duration(
                    _intersect_intervals(
                        ((cast.seconds, cast.seconds + maintained_duration),),
                        output_windows,
                    )
                )
            utilization_ratio = max(
                0.0, min(1.0, utilized_duration / expected_duration)
            )
            _cooldown_factor, action_factor = TeamScoreEngine._combat_modifiers_at(
                cast.seconds,
                modifier_windows,
            )
            action_speed = max(
                0.0,
                cast.action_speed_percent,
                (action_factor - 1.0) * 100.0,
            )
            action_output_multiplier = _action_speed_output_multiplier(
                job,
                cast.key,
                action_speed,
            )
            from .score import speed_output_factor
            speed_rule=(theory_plan or {}).get('_score_v2',{}).get('profiles',{}).get(cast.key)
            action_output_multiplier=speed_output_factor(speed_rule,action_speed,action_output_multiplier)
            if cast.key in _proportional_channel_skills(job):
                maintained_ratio = max(
                    0.0,
                    min(1.0, maintained_duration / expected_duration),
                )
                interruption_fraction = _proportional_damage_fraction(
                    job,
                    cast.key,
                    maintained_ratio,
                    action_output_multiplier,
                )
                output_overlap = (
                    min(1.0, utilized_duration / maintained_duration)
                    if maintained_duration > 1e-9
                    else 0.0
                )
                damage_fraction = interruption_fraction * output_overlap
            else:
                damage_fraction = utilization_ratio
            factual_removal_ratio = (
                max(
                    0.0,
                    min(
                        1.0,
                        utilized_duration / baseline_utilized_duration,
                    ),
                )
                if summon_limit is not None
                and baseline_utilized_duration > 1e-9
                else 1.0
            )
            exact_periodic_power = 0.0
            candidates = exact_periodic_rows.get(cast.key, ())
            if candidates:
                available = [
                    (abs(seconds - cast.seconds), row_index, damage)
                    for row_index, (seconds, damage) in enumerate(candidates)
                    if row_index not in used_exact_periodic_rows[cast.key]
                ]
                if available:
                    distance, row_index, damage = min(available)
                    match_tolerance = max(
                        0.75,
                        profile.cast_time + 0.35,
                        cast.expected_duration + 0.35,
                    )
                    if distance <= match_tolerance:
                        used_exact_periodic_rows[cast.key].add(row_index)
                        exact_periodic_power = damage
            exact_cast_power = 0.0
            ordinary_candidates = exact_cast_rows.get(cast.key, ())
            if ordinary_candidates:
                available = [
                    (abs(seconds - cast.seconds), row_index, damage)
                    for row_index, (seconds, damage) in enumerate(ordinary_candidates)
                    if row_index not in used_exact_cast_rows[cast.key]
                ]
                if available:
                    distance, row_index, damage = min(available)
                    match_tolerance = max(
                        0.75,
                        profile.cast_time + 0.35,
                        cast.expected_duration + 0.35,
                    )
                    if distance <= match_tolerance:
                        used_exact_cast_rows[cast.key].add(row_index)
                        exact_cast_power = damage
            from .score import factor_at
            v2_rule=(theory_plan or {}).get('_score_v2',{}).get('profiles',{}).get(cast.key)
            continuous_boss_factor=factor_at(v2_rule,cast.seconds,action_output_multiplier) if v2_rule and ('boss_windows' in v2_rule or 'state_calibrations' in v2_rule) else 1.
            if v2_rule and not v2_rule.get('actual_only') and not v2_rule.get('burst_only'):
                damage_fraction=1.
            result.append(
                _ValuedCast(
                    seconds=cast.seconds,
                    key=cast.key,
                    source=cast.source,
                    power=(
                        exact_periodic_power * factual_removal_ratio
                        if exact_periodic_power > 0
                        else exact_cast_power * damage_fraction
                        if exact_cast_power > 0
                        else profile.power
                        * continuous_boss_factor
                        * action_output_multiplier
                        * damage_fraction
                    ),
                    expected_duration=expected_duration,
                    utilized_duration=utilized_duration,
                    utilization_ratio=utilization_ratio,
                    proportional_output=proportional,
                    action_speed_percent=action_speed,
                    action_speed_output_multiplier=action_output_multiplier,
                )
            )
        return result
    @staticmethod
    def _combat_modifier_effect_windows(
        snapshot: CombatSnapshot, target_id: str
    ) -> tuple[tuple[str, float, float, float, float], ...]:
        grouped: dict[
            tuple[str, float, float], list[tuple[float, float]]
        ] = defaultdict(list)
        clear_times = player_buff_clear_times_by_target(
            event_platform_for(snapshot).player_buff_clear_events
        ).get(target_id, ())
        for buff in event_platform_for(snapshot).player_buff_series:
            if buff.target_id != target_id:
                continue
            skill = canonical_skill(buff.skill_name)
            rule = _COMBAT_MODIFIER_RULES.get(skill)
            if rule is None:
                continue
            cooldown_factor, action_factor, normal_duration = rule
            for start, end in clip_player_buff_intervals_at_clears(
                _recorded_timing_buff_intervals(buff, skill),
                (
                    clear_times
                    if player_buff_clear_affects_timeline_skill(skill)
                    else ()
                ),
                skill_id=getattr(buff, "skill_id", ""),
                skill_name=getattr(buff, "skill_name", ""),
            ):
                interval_duration = max(0.0, end - start)
                # Several packet statuses share the buff's skill id. Resource
                # counters can remain active from the first cast until combat
                # ends; they are not the finite action-speed/CDR effect.
                if normal_duration is not None and interval_duration > max(
                    normal_duration + 2.0, normal_duration * 2.0
                ):
                    continue
                if interval_duration > 1e-9:
                    grouped[(skill, cooldown_factor, action_factor)].append(
                        (start, end)
                    )

        rows: list[tuple[str, float, float, float, float]] = []
        for (skill, cooldown_factor, action_factor), intervals in grouped.items():
            rows.extend(
                (skill, start, end, cooldown_factor, action_factor)
                for start, end in _merge_intervals(intervals)
            )
        return tuple(sorted(rows, key=lambda row: (row[1], row[2], row[0])))
    @staticmethod
    def _combat_modifier_windows(
        snapshot: CombatSnapshot, target_id: str
    ) -> tuple[tuple[float, float, float, float], ...]:
        return tuple(
            (
                start,
                end,
                cooldown_factor,
                # Phantom Gaze is already applied per skill by the cast-timing
                # resolver/theory planner. Treating it as a generic modifier
                # here would give its 15% speed to every Blade Dancer skill.
                1.0 if skill == "幻影凝视" else action_factor,
            )
            for skill, start, end, cooldown_factor, action_factor in (
                TeamScoreEngine._combat_modifier_effect_windows(snapshot, target_id)
            )
            if cooldown_factor < 1.0 - 1e-9
            or action_factor > 1.0 + 1e-9
        )
    @staticmethod
    def _combat_modifiers_at(
        seconds: float,
        windows: tuple[tuple[float, float, float, float], ...],
    ) -> tuple[float, float]:
        cooldown_reduction = 0.0
        action_speed_bonus = 0.0
        for start, end, cooldown, action in windows:
            if start - 1e-6 <= seconds <= end + 1e-6:
                cooldown_reduction += max(0.0, 1.0 - cooldown)
                action_speed_bonus += max(0.0, action - 1.0)
        return (
            max(0.30, 1.0 - cooldown_reduction),
            max(1.0, 1.0 + action_speed_bonus),
        )
    @staticmethod
    def _planned_theoretical_casts(
        theory_plan: dict[str, Any] | None,
        profiles: dict[str, SkillProfile],
        output_windows: tuple[tuple[float, float], ...],
        job: str = "",
        modifier_windows: tuple[tuple[float, float, float, float], ...] = (),
    ) -> list[_TheoreticalCast]:
        if not theory_plan or not profiles or not output_windows:
            return []
        time_offset = max(0.0, as_number(theory_plan.get("_score_time_offset")))
        prepared: list[
            tuple[dict[str, Any], str, SkillProfile, float, float, float]
        ] = []
        for segment in theory_plan.get("segments", ()):
            if not isinstance(segment, dict):
                continue
            key = canonical_skill(segment.get("skill"))
            profile = profiles.get(key)
            if profile is None:
                continue
            if key in _TRANSFORMED_ATTACK_BUFF_ONLY_SKILLS:
                continue
            start = time_offset + max(0.0, as_number(segment.get("start")))
            end = time_offset + max(
                0.0,
                as_number(segment.get("end"), as_number(segment.get("start"))),
            )
            raw_score = max(0.0, as_number(segment.get("raw_effective_score")))
            effective_score = max(0.0, as_number(segment.get("effective_score")))
            if raw_score <= 1e-9 and effective_score <= 1e-9:
                continue
            prepared.append(
                (segment, key, profile, start, max(start, end), raw_score)
            )

        next_periodic_start: dict[int, float] = {}
        next_start_by_key: dict[str, float] = {}
        periodic_keys = _scoring_periodic_damage_durations()
        refreshable_periodic_keys = set(
            _scoring_refreshable_periodic_durations()
        )
        for index in range(len(prepared) - 1, -1, -1):
            _segment, key, _profile, start, _end, _raw_score = prepared[index]
            if key not in refreshable_periodic_keys:
                continue
            next_periodic_start[index] = next_start_by_key.get(key, float("inf"))
            next_start_by_key[key] = max(start, _end)

        rows: list[_TheoreticalCast] = []
        for index, (segment, key, profile, start, end, raw_score) in enumerate(
            prepared
        ):
            periodic_duration = (
                _scoring_periodic_damage_duration(key, profile)
                if key in periodic_keys
                else 0.0
            )
            utilization = 1.0
            utilized_duration = 0.0
            proportional_output = periodic_duration > 0
            if periodic_duration > 0:
                damage_start = max(start, end)
                active_end = min(
                    damage_start + periodic_duration,
                    next_periodic_start.get(index, float("inf")),
                )
                utilized_duration = _interval_duration(
                    _intersect_intervals(
                        ((damage_start, active_end),),
                        output_windows,
                    )
                )
                if utilized_duration <= 1e-9:
                    continue
                utilization = max(
                    0.0,
                    min(1.0, utilized_duration / periodic_duration),
                )
            else:
                if not _in_intervals(start, output_windows):
                    # Persistent/summoned damage can survive a gap even when its cast
                    # marker sits just outside the next active boss interval.
                    active_overlap = _intersect_intervals(
                        ((start, end),), output_windows
                    )
                    if not active_overlap:
                        continue
                metric = segment.get("metric")
                if isinstance(metric, dict):
                    full_raw = max(
                        0.0,
                        as_number(
                            metric.get("timeline_raw_score"),
                            as_number(metric.get("raw_effective_score")),
                        ),
                    )
                    if full_raw > 1e-9 and raw_score > 0:
                        utilization = max(
                            0.0, min(1.0, raw_score / full_raw)
                        )
            _cooldown_factor, action_factor = TeamScoreEngine._combat_modifiers_at(
                start,
                modifier_windows,
            )
            planned_action_speed = segment.get("action_speed_percent")
            action_speed = max(
                0.0,
                (
                    as_number(planned_action_speed)
                    if planned_action_speed is not None
                    else (action_factor - 1.0) * 100.0
                ),
            )
            action_output_multiplier = max(
                0.0,
                as_number(segment.get("action_speed_output_multiplier")),
            )
            if action_output_multiplier <= 0:
                action_output_multiplier = _action_speed_output_multiplier(
                    job,
                    key,
                    action_speed,
                )
            integrated_standard_damage = max(
                0.0, as_number(segment.get("_score_actual_damage"))
            )
            from .score import factor_at, speed_output_factor, uses_native_speed_reference
            v2_rule=theory_plan.get('_score_v2',{}).get('profiles',{}).get(key)
            # Integrated segments were already priced by the shared model.
            # Only the non-integrated fallback needs an explicit conversion.
            if integrated_standard_damage <= 0 and uses_native_speed_reference(v2_rule):
                action_output_multiplier=speed_output_factor(v2_rule,action_speed,action_output_multiplier)
            continuous_boss_factor=factor_at(v2_rule,start,action_output_multiplier) if v2_rule and ('boss_windows' in v2_rule or 'state_calibrations' in v2_rule) else 1.
            rows.append(
                _TheoreticalCast(
                    seconds=start,
                    key=key,
                    power=(
                        integrated_standard_damage
                        if integrated_standard_damage > 0
                        else profile.power
                        * continuous_boss_factor
                        * utilization
                        * action_output_multiplier
                    ),
                    end_seconds=end,
                    expected_duration=periodic_duration,
                    utilized_duration=utilized_duration,
                    proportional_output=proportional_output,
                    action_speed_percent=action_speed,
                    action_speed_output_multiplier=action_output_multiplier,
                )
            )
        rows.sort(key=lambda row: (row.seconds, row.end_seconds, row.key))
        return rows
    @staticmethod
    def _planned_theory_context_timeline(
        theory_plan: dict[str, Any] | None,
        output_windows: tuple[tuple[float, float], ...],
        burst_windows: tuple[tuple[float, float], ...],
        scored_timeline: tuple[ScoreTimelineCast, ...] = (),
    ) -> tuple[ScoreTimelineCast, ...]:
        """Keep real support casts visible without treating them as damage."""

        if not theory_plan or not output_windows:
            return ()
        time_offset = max(0.0, as_number(theory_plan.get("_score_time_offset")))
        existing = {
            (round(row.seconds, 3), canonical_skill(row.skill_name))
            for row in scored_timeline
        }
        rows: list[ScoreTimelineCast] = []
        for segment in theory_plan.get("segments", ()):
            if not isinstance(segment, dict):
                continue
            preparation = norm(segment.get("kind")) == "prep"
            if norm(segment.get("kind")) not in {"support", "prep"} or segment.get("timeline_hidden"):
                continue
            skill_name = norm(segment.get("display_name")) or norm(segment.get("skill"))
            skill_key = canonical_skill(skill_name)
            if not skill_key:
                continue
            start = time_offset + max(0.0, as_number(segment.get("start")))
            end = time_offset + max(
                0.0,
                as_number(segment.get("end"), as_number(segment.get("start"))),
            )
            end = max(start, end)
            if not preparation and not (
                _in_intervals(start, output_windows)
                or _intersect_intervals(((start, end),), output_windows)
            ):
                continue
            identity = (round(start, 3), skill_key)
            if identity in existing:
                continue
            existing.add(identity)
            action_speed = max(0.0, as_number(segment.get("action_speed_percent")))
            duration = max(0.0, end - start)
            rows.append(
                ScoreTimelineCast(
                    seconds=start,
                    end_seconds=end,
                    skill_name=skill_name,
                    power=0.0,
                    source=("停手期攒怒准备（不计伤害）" if preparation else "长时间实战模拟轴（辅助动作）"),
                    display_name=f"准备·{skill_name}" if preparation else "",
                    high_value=False,
                    in_burst_window=_in_intervals(start, burst_windows),
                    expected_duration=duration,
                    utilized_duration=duration,
                    utilization_ratio=1.0,
                    action_speed_percent=action_speed,
                    action_speed_output_multiplier=1.0,
                )
            )
        return tuple(sorted(rows, key=lambda row: (row.seconds, row.end_seconds, row.skill_name)))
    @staticmethod
    def _theoretical_casts(
        profiles: tuple[SkillProfile, ...],
        output_windows: tuple[tuple[float, float], ...],
        modifier_windows: tuple[tuple[float, float, float, float], ...] = (),
        job: str = "",
    ) -> list[_TheoreticalCast]:
        if not profiles or not output_windows:
            return []
        ready = {row.key: 0.0 for row in profiles}
        result: list[_TheoreticalCast] = []
        for window_start, window_end in output_windows:
            current = window_start
            while current < window_end - 0.05:
                cooldown_factor, action_factor = TeamScoreEngine._combat_modifiers_at(
                    current, modifier_windows
                )
                available = [
                    row
                    for row in profiles
                    if not row.key.endswith(":basic_attack")
                    if ready[row.key] <= current + 1e-6
                    and current
                    + _action_speed_adjusted_cast_time(
                        row.key, row.cast_time, action_factor, job
                    )
                    <= window_end + 1e-6
                ]
                if not available:
                    future = [
                        ready[row.key]
                        for row in profiles
                        if current < ready[row.key] < window_end
                    ]
                    if not future:
                        break
                    current = min(future)
                    continue
                action_speed = max(0.0, (action_factor - 1.0) * 100.0)
                skill = max(
                    available,
                    key=lambda row: (
                        (
                            row.dpa + row.dps * 0.20
                        )
                        * _action_speed_output_multiplier(
                            job,
                            row.key,
                            action_speed,
                        ),
                        row.power,
                    ),
                )
                adjusted_cast = _action_speed_adjusted_cast_time(
                    skill.key, skill.cast_time, action_factor, job
                )
                action_output_multiplier = _action_speed_output_multiplier(
                    job,
                    skill.key,
                    action_speed,
                )
                result.append(
                    _TheoreticalCast(
                        seconds=current,
                        key=skill.key,
                        power=skill.power * action_output_multiplier,
                        end_seconds=current + adjusted_cast,
                        action_speed_percent=action_speed,
                        action_speed_output_multiplier=action_output_multiplier,
                    )
                )
                ready[skill.key] = current + max(
                    skill.cooldown * cooldown_factor, adjusted_cast, 0.20
                )
                current += adjusted_cast
        return result
    @staticmethod
    def _burst_windows(
        snapshot: CombatSnapshot, target_id: str
    ) -> tuple[tuple[float, float], ...]:
        return _merge_intervals(
            (row.start_seconds, row.end_seconds)
            for row in TeamScoreEngine._burst_window_details(snapshot, target_id)
        )
    @staticmethod
    def _burst_window_details(
        snapshot: CombatSnapshot, target_id: str
    ) -> tuple[ScoreWindowDetail, ...]:
        rows: dict[tuple[str, float, float], ScoreWindowDetail] = {}
        for row in event_platform_for(snapshot).player_buff_series:
            if row.target_id != target_id:
                continue
            if canonical_skill(row.skill_name) in _BURST_SKILLS:
                label = norm(row.skill_name) or norm(row.label) or "攻击类Buff"
                for start, end in _recorded_timing_buff_intervals(
                    row, row.skill_name
                ):
                    identity = (label, round(start, 3), round(end, 3))
                    rows[identity] = ScoreWindowDetail(
                        label=label,
                        start_seconds=start,
                        end_seconds=end,
                        original_duration=original_buff_duration(label,end-start),
                        actual_power=0.0,
                        theory_power=0.0,
                        completion=100.0,
                    )
        return tuple(
            sorted(rows.values(), key=lambda row: (row.start_seconds, row.end_seconds, row.label))
        )
    @staticmethod
    def _planned_burst_window_details(
        theory_plan: dict[str, Any] | None,
        output_windows: tuple[tuple[float, float], ...],
    ) -> tuple[ScoreWindowDetail, ...]:
        """Read the planner's own burst schedule on the score time axis.

        Actual and theoretical rotations may choose different cast times for
        Spirit Boost and Time Acceleration. Comparing both damage streams in
        the actual wall-clock window makes an unboosted part of the theory axis
        the denominator and can greatly inflate timing completion.
        """

        if not isinstance(theory_plan, dict) or not output_windows:
            return ()
        time_offset = max(0.0, as_number(theory_plan.get("_score_time_offset")))
        clear_times = tuple(
            time_offset + max(0.0, as_number(seconds))
            for seconds in theory_plan.get("player_buff_clear_times", ())
        )
        rows: dict[tuple[str, float, float], ScoreWindowDetail] = {}
        for collection_name in ("effect_windows", "external_windows"):
            for effect_row in theory_plan.get(collection_name, ()):
                if not isinstance(effect_row, dict):
                    continue
                team_effect = effect_row.get("team_effect")
                candidate_names = [
                    effect_row.get("source"),
                    effect_row.get("skill"),
                    team_effect.get("skill")
                    if isinstance(team_effect, dict)
                    else "",
                ]
                display_name = norm(effect_row.get("name"))
                burst_skill = next(
                    (
                        key
                        for key in _BURST_SKILLS
                        if any(
                            canonical_skill(candidate) == key
                            for candidate in candidate_names
                        )
                        or key in display_name
                    ),
                    "",
                )
                if not burst_skill:
                    continue
                start = time_offset + max(
                    0.0,
                    as_number(effect_row.get("start")),
                )
                end = time_offset + max(
                    as_number(effect_row.get("start")),
                    as_number(
                        effect_row.get("end"),
                        effect_row.get("start"),
                    ),
                )
                clipped = clip_player_buff_intervals_at_clears(
                    ((start, end),),
                    clear_times,
                    skill_name=burst_skill,
                )
                for visible_start, visible_end in _intersect_intervals(
                    clipped, output_windows
                ):
                    identity = (
                        burst_skill,
                        round(visible_start, 3),
                        round(visible_end, 3),
                    )
                    rows[identity] = ScoreWindowDetail(
                        label=burst_skill,
                        start_seconds=visible_start,
                        end_seconds=visible_end,
                        original_duration=original_buff_duration(burst_skill,end-start),
                        actual_power=0.0,
                        theory_power=0.0,
                        completion=100.0,
                    )
        return tuple(
            sorted(
                rows.values(),
                key=lambda row: (
                    row.start_seconds,
                    row.end_seconds,
                    row.label,
                ),
            )
        )
    @staticmethod
    def _yao_meditation_coverage(
        snapshot: CombatSnapshot,
        source_id: str,
        output_windows: tuple[tuple[float, float], ...],
        boss_target_ids: frozenset[str] | None = None,
    ) -> float:
        """Damage-weighted received Chakra Cure coverage, not union uptime."""
        skill_key = canonical_skill(_YAO_MEDITATION_SKILL)
        platform = event_platform_for(snapshot)
        received = {}
        for row in platform.player_buff_series:
            if row.source_id == source_id and canonical_skill(row.skill_name) == skill_key:
                received.setdefault(row.target_id, []).extend(row.intervals)
        if not received:
            for row in platform.player_buffs:
                if row.source_id == source_id and canonical_skill(row.skill_name) == skill_key:
                    received.setdefault(row.target_id, []).extend(
                        row.intervals or ((row.start_seconds, row.end_seconds),))
        received = {actor: _merge_intervals(rows) for actor, rows in received.items()}
        party = {row.source_id for row in platform.players}
        total = covered = 0.0
        for event in platform.event_index.damage_events:
            at = event.timestamp_ms / 1000.0
            if (party and event.source_id not in party) or not _in_intervals(at, output_windows):
                continue
            if boss_target_ids and event.target_id not in boss_target_ids:
                continue
            amount = max(0.0, event.amount)
            total += amount
            if _in_intervals(at, received.get(event.source_id, ())):
                covered += amount
        return covered / total if total > 0 else 0.0


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreRotationMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
