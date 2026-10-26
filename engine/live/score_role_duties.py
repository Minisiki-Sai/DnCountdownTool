from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for

class ScoreRoleDutiesMixin:
    @staticmethod
    def _select_theory_support_intervals(
        expectation: _SupportExpectation,
        planned_intervals: tuple[tuple[float, float], ...],
        fallback_intervals: tuple[tuple[float, float], ...],
        planned_coverage: float,
        fallback_coverage: float,
    ) -> tuple[tuple[float, float], ...]:
        del expectation, fallback_intervals, planned_coverage, fallback_coverage
        # Kept as a compatibility shim for older callers and tests. Support
        # theory must be a projection of the shared output theory plan; it must
        # never replace that plan with an independently generated ideal schedule.
        return planned_intervals
    @staticmethod
    def _planned_support_intervals(
        theory_plan: dict[str, Any] | None,
        expectation: _SupportExpectation,
        end_seconds: float,
        modifier_windows: tuple[tuple[float, float, float, float], ...] = (),
    ) -> tuple[tuple[float, float], ...]:
        if not theory_plan:
            return ()
        time_offset = max(0.0, as_number(theory_plan.get("_score_time_offset")))
        clear_times = tuple(
            time_offset + max(0.0, as_number(seconds))
            for seconds in theory_plan.get("player_buff_clear_times", ())
        )

        def finalized(
            rows: Iterable[tuple[float, float]],
            *,
            merge: bool = True,
        ) -> tuple[tuple[float, float], ...]:
            intervals = tuple(rows)
            if (
                expectation.kind == "buff"
                and player_buff_clear_affects_timeline_skill(
                    expectation.skill_name
                )
            ):
                intervals = clip_player_buff_intervals_at_clears(
                    intervals,
                    clear_times,
                    skill_name=expectation.skill_name,
                )
            return _merge_intervals(intervals) if merge else tuple(sorted(intervals))
        matching_segments = sorted(
            (
                segment
                for segment in theory_plan.get("segments", ())
                if isinstance(segment, dict)
                and canonical_skill(segment.get("skill"))
                == expectation.skill_key
            ),
            key=lambda segment: as_number(segment.get("start")),
        )

        # Multi-hit/lingering skills own a short status that is repeatedly
        # refreshed by later hits.  Their displayed resistance lane may have
        # already been collapsed together with another source (for example,
        # Holy Ground + Mind Breaker).  Such a collapsed row has one visual
        # interval but cannot carry the cast bounds of every listed source.
        # Reusing its inherited ``cast_start``/``cast_end`` made the scorer
        # bind Holy Ground to unrelated lightning casts and merge several
        # eleven-second applications into 30–50 second theory windows.
        #
        # The final skill segments are the authoritative post-optimization
        # cast schedule.  Rebuild every packet-confirmed refreshing effect
        # directly from those segments before consulting display windows.
        refresh_tail = team_effect_refresh_tail(
            expectation.skill_name,
            expectation.effect_key,
        )
        # A collapsed resistance lane is a display of the selected total,
        # not a per-skill lifetime. Equal-strength sources can replace Mind
        # Breaker's label, and overlapping effects split it into fragments.
        # Rebuild this 30% effect from the final casts using the planner's
        # application rule. Do not extend weaker ordinary resistance effects:
        # their selected display intervals can legitimately be suppressed.
        collapsed_resistance_lane = (
            expectation.kind == "debuff"
            and expectation.skill_key == canonical_skill("精神爆破")
            and expectation.effect_key == "减光抗%"
            and any(
                isinstance(row, dict)
                and row.get("category") == "resist"
                and row.get("lane_key") == expectation.effect_key
                for row in theory_plan.get("effect_windows", ())
            )
        )
        # Freeze stacks have their own hit-evidence reconstruction further
        # below.  They are not collapsed into the shared resistance/amplify
        # display lanes, so keep that dedicated path intact.
        if (
            (refresh_tail > 1e-9 or collapsed_resistance_lane)
            and expectation.effect_key != "冰冻层"
            and matching_segments
        ):
            refreshed_intervals: list[tuple[float, float]] = []
            for segment in matching_segments:
                cast_start = max(0.0, as_number(segment.get("start")))
                cast_end = max(
                    cast_start,
                    as_number(segment.get("end"), cast_start),
                )
                effect_start, effect_end, _refresh_tail = (
                    team_effect_application_window_for_effect(
                        expectation.skill_name,
                        expectation.effect_key,
                        cast_start,
                        cast_end,
                        expectation.duration,
                    )
                )
                start = time_offset + effect_start
                end = min(end_seconds, time_offset + effect_end)
                if end > start + 1e-9:
                    refreshed_intervals.append((start, end))
            if refreshed_intervals:
                return finalized(refreshed_intervals)
        if expectation.skill_key == canonical_skill(_YAO_MEDITATION_SKILL):
            # Chakra Cure is a maintained meditation state, not a normal cast
            # that waits for a ten-second cooldown. The sitting-Yao planner
            # writes every idle interval as a `kind=sitting` segment. Buffs are
            # refreshed every 0.5s while that segment is active, and the last
            # refresh remains for two seconds after the next skill starts.
            sitting_intervals = []
            for segment in matching_segments:
                if norm(segment.get("kind")) != "sitting":
                    continue
                start = time_offset + max(
                    0.0, as_number(segment.get("start"))
                )
                source_end = time_offset + max(
                    as_number(segment.get("start")),
                    as_number(segment.get("end")),
                )
                end = min(
                    end_seconds,
                    source_end + max(0.0, expectation.duration),
                )
                if end > start + 1e-9:
                    sitting_intervals.append((start, end))
            if sitting_intervals:
                return finalized(sitting_intervals)

        # The output planner has already applied glyphs, runtime cooldown
        # modifiers, charge rules and profession-specific resets. Re-validating
        # its segments with the raw support cooldown can reject valid casts and
        # leave the support page blank. Trust the shared plan here.
        # New score plans carry the exact selected glyph rows. Rebuild direct
        # team-buff lifetimes from each cast so a stale/pre-merged display
        # window cannot silently turn a 15s buff into its 18s glyph variant.
        # Keeping each cast here also preserves overlapping recasts correctly.
        if (
            "_score_glyphs" in theory_plan
            and expectation.kind == "buff"
            and expectation.duration > 0
        ):
            cast_intervals: list[tuple[float, float]] = []
            for segment in matching_segments:
                start = time_offset + max(
                    0.0,
                    as_number(
                        segment.get("end"), as_number(segment.get("start"))
                    ),
                )
                end = min(end_seconds, start + expectation.duration)
                if end > start + 1e-9:
                    cast_intervals.append((start, end))
            if cast_intervals:
                return finalized(cast_intervals)

        result: list[tuple[float, float]] = []
        explicit_effect_starts: list[float] = []
        for row in theory_plan.get("effect_windows", ()):
            if not isinstance(row, dict):
                continue
            effect = row.get("team_effect")
            candidates = {
                canonical_skill(row.get("source")),
                canonical_skill(row.get("skill")),
            }
            if isinstance(effect, dict):
                candidates.add(canonical_skill(effect.get("skill")))
            name_key = canonical_skill(row.get("name"))
            if expectation.skill_key not in candidates and expectation.skill_key not in name_key:
                continue
            if expectation.kind == "debuff":
                effect_attrs: dict[str, Any] = {}
                for attrs in (
                    row.get("attrs"),
                    row.get("display_attrs"),
                    effect.get("attrs") if isinstance(effect, dict) else None,
                    effect.get("display_attrs") if isinstance(effect, dict) else None,
                    effect.get("effective_attrs") if isinstance(effect, dict) else None,
                ):
                    if isinstance(attrs, dict):
                        effect_attrs.update(attrs)
                # The same cast can own multiple effects with different
                # durations (Frost: water resistance and Frost EX freeze).
                # Skill-name-only matching lets the longer effect silently
                # replace the shorter one in the score timeline.
                if expectation.effect_key not in effect_attrs:
                    continue
            raw_start = max(0.0, as_number(row.get("start")))
            start = time_offset + raw_start
            end = min(
                end_seconds,
                max(start, time_offset + max(raw_start, as_number(row.get("end")))),
            )
            effective_duration = effective_team_effect_duration_for_effect(
                expectation.skill_name,
                expectation.effect_key,
                expectation.duration,
            )
            # New plans keep the cast bounds on each effect row. For older
            # cached plans, recover the nearest matching segment. This lets the
            # scorer rebuild first-hit and last-refresh timing instead of
            # stretching a stale display row from the wrong endpoint.
            cast_segment = None
            if (
                effective_duration > expectation.duration + 1e-9
                and ("cast_start" in row or "cast_end" in row)
            ):
                cast_segment = {
                    "start": row.get("cast_start", row.get("start")),
                    "end": row.get("cast_end", row.get("start")),
                }
            elif (
                effective_duration > expectation.duration + 1e-9
                and matching_segments
            ):
                nearby = [
                    segment
                    for segment in matching_segments
                    if as_number(segment.get("start")) - 0.25
                    <= raw_start
                    <= as_number(segment.get("end"), segment.get("start")) + 0.75
                ]
                if nearby:
                    cast_segment = min(
                        nearby,
                        key=lambda segment: abs(
                            raw_start
                            - as_number(segment.get("end"), segment.get("start"))
                        ),
                    )
            if cast_segment is not None:
                rebuilt_start, rebuilt_end, _refresh_tail = (
                    team_effect_application_window_for_effect(
                        expectation.skill_name,
                        expectation.effect_key,
                        as_number(cast_segment.get("start")),
                        as_number(
                            cast_segment.get("end"), cast_segment.get("start")
                        ),
                        expectation.duration,
                    )
                )
                start = time_offset + rebuilt_start
                end = min(end_seconds, time_offset + rebuilt_end)
            elif (
                expectation.effect_key == "冰冻层"
                and expectation.duration > 0
                and expectation.skill_key not in _REFRESHING_FREEZE_RULES
            ):
                end = min(end_seconds, start + effective_duration)
            else:
                if effective_duration > expectation.duration + 1e-9:
                    end = min(end_seconds, max(end, start + effective_duration))
            if end > start + 1e-9:
                result.append((start, end))
                explicit_effect_starts.append(start)

        # Some debuffs have no dedicated display window but are represented by
        # their actual support cast on the shared practical timeline. An older
        # all-or-nothing fallback stopped here as soon as *one* explicit effect
        # window existed, which silently discarded later casts when the planner
        # emitted only a partial effect-window series. Keep explicit windows
        # authoritative for the casts they describe and fill only unmatched
        # casts from the shared output plan.
        duration = effective_team_effect_duration_for_effect(
            expectation.skill_name,
            expectation.effect_key,
            expectation.duration,
        )
        if duration > 0:
            for segment in matching_segments:
                segment_start = time_offset + max(
                    0.0, as_number(segment.get("start"))
                )
                cast_end = time_offset + max(
                    0.0,
                    as_number(segment.get("end"), as_number(segment.get("start"))),
                )
                if any(
                    segment_start - 0.25 <= effect_start <= cast_end + 0.75
                    for effect_start in explicit_effect_starts
                ):
                    continue
                if expectation.skill_key in _CHANNEL_REFRESHING_FREEZE_SKILLS:
                    # These attacks apply/refresh freeze throughout the action.
                    # Their final hit leaves one more full application after
                    # the cast ends. Cold Stab EX is the in-game “ice punch”.
                    result.append(
                        (
                            segment_start,
                            min(end_seconds, cast_end + expectation.duration),
                        )
                    )
                else:
                    if duration > expectation.duration + 1e-9:
                        application_start, application_end, _refresh_tail = (
                            team_effect_application_window_for_effect(
                                expectation.skill_name,
                                expectation.effect_key,
                                segment_start,
                                cast_end,
                                expectation.duration,
                            )
                        )
                    else:
                        # Preserve the legacy fallback convention for ordinary
                        # one-shot effects: the support interval starts when
                        # the cast completes. Packet-confirmed multi-hit rules
                        # above are the only effects allowed to move that start.
                        application_start = cast_end
                        application_end = cast_end + duration
                    result.append(
                        (
                            application_start,
                            min(end_seconds, application_end),
                        )
                    )
        if result:
            return (
                finalized(result, merge=False)
                if expectation.effect_key == "冰冻层"
                else finalized(result)
            )

        # Maintained team buffs can be deliberately absent from the shared cast
        # plan: they may be pre-applied before the selected Boss window or
        # refreshed by a permanent summon. Requiring a cast segment made
        # Cocktail, Raid and Azuna's Breath report zero theory coverage. If the
        # planner did provide a cast/effect window, the branches above remain
        # authoritative so active maintenance such as Cure Relic is not made
        # more ideal than the shared output plan.
        if expectation.kind == "buff" and (
            expectation.duration <= 0
            or expectation.cooldown <= 0
            or expectation.duration >= expectation.cooldown
        ):
            return finalized(
                ((time_offset, end_seconds),)
                if end_seconds > time_offset + 1e-9
                else ()
            )
        return ()
    @staticmethod
    def _dynamic_cooldown_ready_time(
        cast_seconds: float,
        cooldown: float,
        modifier_windows: tuple[tuple[float, float, float, float], ...],
        end_seconds: float,
    ) -> float:
        if cooldown <= 0:
            return cast_seconds
        boundaries = sorted(
            {
                cast_seconds,
                end_seconds,
                *(
                    max(cast_seconds, min(end_seconds, value))
                    for start, end, _cooldown, _action in modifier_windows
                    for value in (start, end)
                    if cast_seconds < value < end_seconds
                ),
            }
        )
        remaining = cooldown
        for start, end in zip(boundaries, boundaries[1:]):
            if end <= start + 1e-9:
                continue
            factor, _action = TeamScoreEngine._combat_modifiers_at(
                (start + end) / 2.0, modifier_windows
            )
            progress = (end - start) / max(0.01, factor)
            if progress >= remaining - 1e-9:
                return start + remaining * factor
            remaining -= progress
        return end_seconds + remaining
    @staticmethod
    def _next_output_time(
        seconds: float,
        output_windows: tuple[tuple[float, float], ...],
    ) -> float | None:
        for start, end in output_windows:
            if start - 1e-9 <= seconds < end - 1e-9:
                return max(start, seconds)
            if seconds < start:
                return start
        return None
    @staticmethod
    def _theory_support_intervals(
        expectation: _SupportExpectation,
        end_seconds: float,
        output_windows: tuple[tuple[float, float], ...] = (),
        modifier_windows: tuple[tuple[float, float, float, float], ...] = (),
    ) -> tuple[tuple[float, float], ...]:
        if end_seconds <= 0 or not output_windows:
            return ()
        duration = effective_team_effect_duration_for_effect(
            expectation.skill_name,
            expectation.effect_key,
            expectation.duration,
        )
        cooldown = expectation.cooldown
        if duration <= 0 or cooldown <= 0 or duration >= cooldown:
            return _merge_intervals(output_windows)
        # Spirit Boost and Time Acceleration can use their own cooldown glyph,
        # already reflected in expectation.cooldown, but never benefit from
        # Cocktail or any other runtime cooldown-reduction window.
        cooldown_modifier_windows = (
            ()
            if expectation.skill_key in _BURST_SKILLS
            else modifier_windows
        )
        intervals: list[tuple[float, float]] = []
        start = TeamScoreEngine._next_output_time(0.0, output_windows)
        while start is not None and start < end_seconds - 1e-6:
            intervals.append((start, min(end_seconds, start + duration)))
            ready = TeamScoreEngine._dynamic_cooldown_ready_time(
                start, cooldown, cooldown_modifier_windows, end_seconds
            )
            start = TeamScoreEngine._next_output_time(
                max(start + 0.05, ready), output_windows
            )
        return _merge_intervals(intervals)
    def _skill_cast_completion(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        skill_names: set[str],
        output_windows: tuple[tuple[float, float], ...],
        *,
        allow_overflow: bool = False,
    ) -> float:
        if not skill_names:
            return 100.0
        actual = Counter(
            canonical_skill(event.skill_name)
            for event in event_platform_for(
                snapshot
            ).event_index.casts_by_source.get(
                player.source_id, ()
            )
            if canonical_skill(event.skill_name) in skill_names
            and _in_intervals(event.timestamp_ms / 1000.0, output_windows)
        )
        end_seconds = max((end for _start, end in output_windows), default=0.0)
        modifier_windows = self._combat_modifier_windows(
            snapshot, player.source_id
        )
        ratios: list[float] = []
        for key in skill_names:
            name = next(
                (skill for skill in self.store.full_skill_names(player.job) if canonical_skill(skill) == key),
                key,
            )
            cooldown = self.store.skill_reference_cooldown(player.job, name, 0.0)
            theoretical = 1
            if cooldown > 0 and end_seconds > 0:
                theoretical = 0
                cast_seconds = self._next_output_time(0.0, output_windows)
                while (
                    cast_seconds is not None
                    and cast_seconds < end_seconds - 1e-6
                ):
                    theoretical += 1
                    ready = self._dynamic_cooldown_ready_time(
                        cast_seconds,
                        cooldown,
                        modifier_windows,
                        end_seconds,
                    )
                    cast_seconds = self._next_output_time(
                        max(cast_seconds + 0.05, ready), output_windows
                    )
                theoretical = max(1, theoretical)
            ratio = actual[key] / theoretical
            ratios.append(ratio if allow_overflow else min(1.0, ratio))
        score = sum(ratios) / len(ratios) * 100.0
        return max(0.0, score) if allow_overflow else _clamp_score(score)
    def _healer_regular_damage_reduction_completion(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        job: str,
        evaluation_windows: tuple[tuple[float, float], ...],
        healer_count: int,
    ) -> float:
        """Return average party coverage of this healer's routine mitigation.

        Recipient-specific status intervals are authoritative.  Older captures
        without player-status rows may fall back to confirmed casts and the
        data-backed duration.  Multiple healers share the theoretical duty in
        the same way as high-pressure healing participation.
        """

        windows = _merge_intervals(evaluation_windows)
        if not windows:
            windows = ((
                0.0,
                max(0.0, event_platform_for(snapshot).elapsed_seconds),
            ),)
        data_job = data_job_for(job)
        skill_keys = {
            canonical_skill(name)
            for name in _HEALER_REGULAR_DAMAGE_REDUCTION_SKILLS.get(
                data_job, frozenset()
            )
        }
        if not skill_keys:
            return 0.0
        self_reported_aura_keys = {
            canonical_skill(name)
            for name in _HEALER_SELF_REPORTED_TEAM_AURAS.get(
                data_job, frozenset()
            )
        }

        party_ids = tuple(
            dict.fromkeys(
                row.source_id
                for row in event_platform_for(snapshot).players
                if row.source_id
            )
        ) or (player.source_id,)
        by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
        series_pairs = set()
        self_reported_aura_intervals: list[tuple[float, float]] = []
        for row in event_platform_for(snapshot).player_buff_series:
            row_skill = canonical_skill(row.skill_name)
            target_id = str(row.target_id)
            if not (
                str(row.source_id) == player.source_id
                and row_skill in skill_keys
                and target_id in party_ids
            ):
                continue
            by_target[target_id].extend(row.intervals)
            series_pairs.add((target_id, row_skill))
            if (
                row_skill in self_reported_aura_keys
                and target_id == player.source_id
            ):
                self_reported_aura_intervals.extend(row.intervals)

        for row in event_platform_for(snapshot).player_buffs:
            row_skill = canonical_skill(row.skill_name)
            target_id = str(row.target_id)
            if not (
                str(row.source_id) == player.source_id
                and row_skill in skill_keys
                and target_id in party_ids
                and (target_id, row_skill) not in series_pairs
            ):
                continue
            intervals = row.intervals or ((row.start_seconds, row.end_seconds),)
            by_target[target_id].extend(intervals)
            if row_skill in self_reported_aura_keys and target_id == player.source_id:
                self_reported_aura_intervals.extend(intervals)

        if self_reported_aura_intervals:
            for target_id in party_ids:
                by_target.setdefault(target_id, []).extend(
                    self_reported_aura_intervals
                )

        if by_target:
            average_coverage = sum(
                _coverage_in_windows(by_target.get(target_id, ()), windows)
                for target_id in party_ids
            ) / max(1, len(party_ids))
        else:
            from .combat_skill_levels import _USAGE_ACTION_KINDS

            inferred: list[tuple[float, float]] = []
            for event in event_platform_for(
                snapshot
            ).event_index.casts_by_source.get(
                player.source_id, ()
            ):
                skill_key = canonical_skill(event.skill_name)
                if (
                    skill_key not in skill_keys
                    or event.packet_kind not in _USAGE_ACTION_KINDS
                ):
                    continue
                duration = _HEALER_REGULAR_DAMAGE_REDUCTION_DURATIONS.get(
                    skill_key, 0.0
                )
                start = event.timestamp_ms / 1000.0
                # A pre-phase cast can still cover this phase. Clip its real
                # lifetime below; do not require its start to be in the phase.
                if duration > 0:
                    inferred.append((start, start + duration))
            average_coverage = _coverage_in_windows(inferred, windows)

        from .duty_groups import groups_for
        groups = groups_for(self, snapshot)
        if groups.segments:
            providers = {p.source_id for p in event_platform_for(snapshot).players
                         if (norm(p.job) in HEALER_JOBS and data_job_for(p.job) != "药剂师")
                         or (data_job_for(p.job) == "药剂师" and self._alchemist_used_sacrifice(snapshot, p.source_id))
                         or data_job_for(p.job) == "曜"}
            expected_seconds = sum(groups.shared_seconds(player.source_id, target, windows, providers)
                                   for target in party_ids)
            covered_seconds = sum(b-a for target in party_ids
                                  for a,b in _intersect_intervals(
                                      _merge_intervals(by_target.get(target, ()) if by_target else inferred),
                                      groups.recipients(player.source_id, target, windows)))
            return _clamp_score(covered_seconds / expected_seconds * 100.0) if expected_seconds > 0 else 100.0

        return _clamp_score(
            average_coverage * max(1, healer_count) * 100.0
        )
    def _healing_completion(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        job: str,
        evaluation_windows: tuple[tuple[float, float], ...],
    ) -> _HealingCompletion:
        """80% useful healing + 20% mitigation, less at most 40 rescue points."""

        from .combat_skill_levels import _USAGE_ACTION_KINDS
        from .healer_demand import audit_healing_demand
        from .healer_rescue import (
            audit_danger_rescue, rescue_episode_penalty, RESCUE_METHOD, RESCUE_SAFE_RATIO,
        )
        from .healing_attribution import (
            HealingAttributionLedger, NON_HEALING_SKILL_KEYS, healing_skill_key,
        )
        from .healing_exclusions import (
            fatal_burst_intervals, healing_block_intervals,
        )

        windows = _merge_intervals(evaluation_windows)
        if not windows:
            windows = ((
                0.0,
                max(0.0, event_platform_for(snapshot).elapsed_seconds),
            ),)
        party_ids = {
            row.source_id
            for row in event_platform_for(snapshot).players
            if row.source_id
        }
        platform = event_platform_for(snapshot)
        from .duty_groups import groups_for
        groups = groups_for(self, snapshot)
        heal_block = healing_block_intervals(snapshot.skill_parameter_events, party_ids)
        fatal_bursts = fatal_burst_intervals(
            platform.player_health_tracks, platform.ordered_player_lifecycle_events,
            platform.event_index.incoming_damage_events,
        )
        excluded_demand = {
            target: _merge_intervals((*heal_block.get(target, ()),
                                      *fatal_bursts.get(target, ())))
            for target in set(heal_block) | set(fatal_bursts)
        }
        healer_ids = {
            row.source_id
            for row in event_platform_for(snapshot).players
            if row.source_id
            and (
                (
                    norm(row.job) in HEALER_JOBS
                    and data_job_for(row.job) != "药剂师"
                )
                or (
                    data_job_for(row.job) == "药剂师"
                    and self._alchemist_used_sacrifice(snapshot, row.source_id)
                )
                or data_job_for(row.job) == "曜"
            )
        }
        from .periodic_health_mechanism import recovery_windows
        healing_windows = recovery_windows(windows, platform.event_index.healing_events)
        all_healing_events = tuple(
            sorted(
                (
                    event
                    for event in event_platform_for(
                        snapshot
                    ).event_index.healing_events
                    if event.target_id in party_ids
                    and _in_intervals(event.timestamp_ms / 1000.0, healing_windows)
                ),
                key=lambda row: row.timestamp_ms,
            )
        )
        team_healing_events = tuple(
            event for event in all_healing_events
            if event.source_id in healer_ids
            and healing_skill_key(event.skill_name) not in NON_HEALING_SKILL_KEYS
        )
        player_healing_events = tuple(
            event
            for event in team_healing_events
            if event.source_id == player.source_id
        )
        # CombatSnapshot.healing_events already come from the independent
        # HealingAttributionLedger.  That ledger performs the only 08 6F HP
        # differencing pass, removes revival/max-HP edges and caps packets to
        # factual positive HP changes.  Re-differencing here used to create a
        # second truth path and could reduce the same heal twice.
        effective_healing = sum(
            event.amount for event in player_healing_events
        )

        raw_loss_events = tuple(
            event for event in event_platform_for(snapshot).event_index.incoming_damage_events
            if event.attribution_method == "incoming_hp_state"
            and event.target_id in party_ids and event.amount > 0
            and event.source_id != event.target_id
            and _in_intervals(event.timestamp_ms / 1000.0, windows)
        )
        demand = audit_healing_demand(
            raw_loss_events, all_healing_events, healing_windows,
            health_tracks=event_platform_for(snapshot).player_health_tracks,
            lifecycle=event_platform_for(snapshot).ordered_player_lifecycle_events,
            excluded_intervals=excluded_demand,
        )
        actual_loss_events = demand.losses

        learned = self._learned_skills(job)
        healing_skills = {canonical_skill(name) for name in _HEALING_SKILL_NAMES}
        for skill_name in self.store.full_skill_names(job):
            key = canonical_skill(skill_name)
            if key not in learned:
                continue
            desc = norm(self.store.skill_effect_timing(job, skill_name).get("desc"))
            if any(marker in desc for marker in _HEALING_DESC_MARKERS):
                healing_skills.add(key)
        healing_skills = {key for key in healing_skills
                          if healing_skill_key(key) not in NON_HEALING_SKILL_KEYS}

        healing_casts = tuple(
            event
            for event in event_platform_for(
                snapshot
            ).event_index.casts_by_source.get(
                player.source_id, ()
            )
            if canonical_skill(event.skill_name) in healing_skills
            and event.packet_kind in _USAGE_ACTION_KINDS
            and _in_intervals(event.timestamp_ms / 1000.0, windows)
        )
        event_times_by_skill: dict[str, list[float]] = defaultdict(list)
        for event in player_healing_events:
            event_times_by_skill[canonical_skill(event.skill_name)].append(
                event.timestamp_ms / 1000.0
            )
        casts_by_skill: dict[str, list[float]] = defaultdict(list)
        for event in healing_casts:
            times = casts_by_skill[canonical_skill(event.skill_name)]
            seconds = event.timestamp_ms / 1000.0
            # Match the healing ledger's duplicate action suppression.
            if not times or seconds - times[-1] > 0.150:
                times.append(seconds)

        effect_windows = {
            canonical_skill(name): duration
            for name, duration in _HEALING_EFFECT_WINDOWS.items()
        }
        # Reuse the treatment model's channel interruption/instance lifetime;
        # a stopped channel cannot be penalized for injuries occurring later.
        instance_ends = {
            (canonical_skill(instance.skill_name), instance.start_ms): instance.end_ms / 1000.0
            for instance in HealingAttributionLedger()._cast_instances(
                (event for event in event_platform_for(snapshot).event_index.casts_by_source.get(
                    player.source_id, ()) if event.packet_kind in _USAGE_ACTION_KINDS),
                {player.source_id: job},
            )
        }
        meaningful_casts = 0
        evaluated_casts = 0
        models = {canonical_skill(model.name): model for model in _HEALING_SKILL_MODELS}
        for key, cast_times in casts_by_skill.items():
            heal_times = event_times_by_skill.get(key, [])
            for index, cast_seconds in enumerate(cast_times):
                next_cast = (
                    cast_times[index + 1]
                    if index + 1 < len(cast_times)
                    else math.inf
                )
                effect_end = min(
                    next_cast,
                    cast_seconds + effect_windows.get(key, 8.0),
                    instance_ends.get((key, round(cast_seconds * 1000)), math.inf),
                )
                heal_index = bisect_left(heal_times, cast_seconds - 0.35)
                if (
                    heal_index < len(heal_times)
                    and heal_times[heal_index] <= effect_end + 0.35
                ):
                    meaningful_casts += 1
                    evaluated_casts += 1
                else:
                    model = models.get(key)
                    targets = ({player.source_id} if model and model.target_scope == "self"
                               else party_ids)
                    if any(demand.needed_during(a, b, {target}) for target in targets
                           for a,b in groups.recipients(player.source_id, target, ((cast_seconds, effect_end),))):
                        evaluated_casts += 1

        total_casts = sum(len(times) for times in casts_by_skill.values())
        if evaluated_casts > 0:
            cast_completion = _clamp_score(
                meaningful_casts / evaluated_casts * 100.0
            )
        elif player_healing_events:
            # A persistent heal may have been cast before the selected phase.
            cast_completion = 100.0
        else:
            # No known need (or missing HP evidence) is not a failed heal.
            known_need = actual_loss_events or any(rows for rows, _ends in demand.need.values())
            cast_completion = 0.0 if known_need else 100.0

        # Build one recipient rescue audit for all healers in this snapshot/
        # phase. Keep the existing effective-heal and mitigation calculations
        # above/below unchanged, including their separate demand accounting.
        rescue_cache = getattr(self, "_danger_rescue_cache", None)
        if not (rescue_cache and rescue_cache[0] is snapshot and rescue_cache[1] == healing_windows):
            rescue_healer_ids = {
                row.source_id for row in event_platform_for(snapshot).players
                if row.source_id and (
                    (yao_rotation_variant(row.job, self._yao_meditation_coverage(
                        snapshot, row.source_id, windows)) == "打坐曜")
                    if data_job_for(row.job) == "曜"
                    else (
                        self._alchemist_used_sacrifice(snapshot, row.source_id)
                        if data_job_for(row.job) == "药剂师"
                        else norm(row.job) in HEALER_JOBS
                    )
                )
            }
            rescue = audit_danger_rescue(
                event_platform_for(snapshot).player_health_tracks, all_healing_events, healing_windows,
                party_ids=party_ids, healer_ids=rescue_healer_ids,
                danger_ratio=_SURVIVAL_DANGER_HP_RATIO,
                lifecycle=event_platform_for(snapshot).ordered_player_lifecycle_events,
                incoming=event_platform_for(snapshot).event_index.incoming_damage_events,
                heal_block_intervals=heal_block,
                fatal_burst_windows=fatal_bursts,
                group_unavailable={(source, target): groups.unavailable(source, target)
                                   for source in rescue_healer_ids for target in party_ids}
                                  if groups.segments else None,
            )
            rescue_cache = (snapshot, healing_windows, rescue, max(1, len(rescue_healer_ids)))
            self._danger_rescue_cache = rescue_cache
        rescue, rescue_healer_count = rescue_cache[2:]
        rescue_penalty = rescue_episode_penalty(rescue, player.source_id, rescue_healer_count)
        rescue_completion = rescue_penalty["completion"]
        rescue_status = (
            "unavailable" if not rescue.evaluated_targets else
            "not_required" if rescue_completion is None and rescue_penalty["not_required_episode_count"] else
            "no_opportunity" if rescue.episodes and rescue_completion is None else
            "no_demand" if rescue_completion is None else
            "estimated" if any(row["estimated_hp"] for row in rescue_penalty["episode_penalties"]) else
            "evaluated"
        )
        rescue_evidence = (
            "没有可用的准确或估算血量轨迹，无法评价，不扣救急分"
            if not rescue.evaluated_targets else
            "队友已及时完成救急或已观察到及时恢复，无需重复施救；不扣分，不记作本人救急满分"
            if rescue_status == "not_required" else
            "没有观察到可救援区间，不扣救急分"
            if rescue_status == "no_opportunity" else
            "已评价的血量区间内无救急需求，不扣救急分"
            if rescue_completion is None else
            "先按全队实际恢复量与响应时间评价救急结果；每次全队共扣2×(1−团队完成率)特色分，"
            "再按未完成责任分摊；队友及时救好则不扣分；超过20次统一缩放，全队合计最多扣40分"
        )
        rescue_evidence += "；其他来源恢复（含结算回满）消减剩余需求但不计本人贡献，复活回血除外，仍保留恢复响应时间；仅依据可靠死亡/复活状态调整施救机会，不猜测距离或技能是否可用；本人责任兑现率仅用于诊断"
        if groups.segments:
            rescue_evidence += "；已核对的双Boss分组阶段，持续攻击目标明确不同的队员不计跨组救援责任；未知分组保留责任"
        if rescue.estimated_targets:
            rescue_evidence += f"；{len(rescue.estimated_targets)}名队员使用有依据的估算血量，不跳过评价"
        if rescue.skipped_targets and rescue.evaluated_targets:
            rescue_evidence += f"；{len(rescue.skipped_targets)}名队员没有可用血量，未纳入评价"
        if rescue_penalty["excluded_short_fatal_count"]:
            rescue_evidence += (
                f"；{rescue_penalty['excluded_short_fatal_count']}次不足"
                f"{rescue_penalty['min_fatal_window_seconds']:g}秒即死亡且无有效救急的短窗口暂不扣分"
            )
        if rescue_penalty["excluded_heal_block_count"]:
            rescue_evidence += f"；{rescue_penalty['excluded_heal_block_count']}次禁疗期间的救急区间未评价"
        if rescue_penalty["excluded_fatal_burst_count"]:
            rescue_evidence += f"；{rescue_penalty['excluded_fatal_burst_count']}次1秒内损失至少50%最大血量并死亡的救急区间未评价"
        effective_weights = {"healing": 0.80, "rescue": 0.0, "mitigation": 0.20}
        rescue_detail = {
            **rescue_penalty,
            "method": RESCUE_METHOD, "danger_hp_threshold": _SURVIVAL_DANGER_HP_RATIO,
            "status": rescue_status, "effective_weights": effective_weights,
            "estimated_players": len(rescue.estimated_targets),
            "exact_players": len(rescue.evaluated_targets) - len(rescue.estimated_targets),
            "safe_hp_threshold": RESCUE_SAFE_RATIO,
            "episode_count": len(rescue.episodes), "evaluated_players": len(rescue.evaluated_targets),
            "party_size": len(party_ids), "healer_count": rescue_healer_count,
            "needed_percent": rescue.needed_percent,
            "recovered_percent": rescue.recovered.get(player.source_id, 0.0),
            "weighted_percent": rescue.weighted.get(player.source_id, 0.0),
            "unknown_onset_episodes": sum(not row.onset_known for row in rescue.episodes),
            "evidence": rescue_evidence,
        }
        healer_count = max(1, len(healer_ids))

        # Actual HP restored is the numerator.  Every factual heal in the
        # shared recipient ledger consumes earlier healable HP loss at most
        # once, including another healer or a potion.  Only the remaining
        # unmet loss is a responsibility to divide among active healers.
        # This avoids treating one tiny heal and one large heal as equal while
        # not penalizing a healer for need already fulfilled by a teammate.
        healable_loss = sum(max(0.0, float(row.amount)) for row in actual_loss_events)
        matched_healing = sum(
            max(0.0, float(amount))
            for sources in (demand.recovered_raw or {}).values()
            for amount in sources.values()
        )
        # Full-HP synchronization can settle a deficit even when a separate
        # heal packet was not attributable.  The demand ledger distinguishes
        # that from genuinely outstanding HP; total loss minus matched heals
        # would incorrectly penalize such completed recoveries.
        unmet_healing = max(0.0, demand.unmet_raw)
        shared_unmet_healing = unmet_healing / rescue_healer_count
        if groups.segments:
            # Read identities from the shared audit, also when it was cached
            # by another healer. Do not discard factual cross-group healing.
            providers = {p.source_id for p in platform.players if p.source_id and (
                (yao_rotation_variant(p.job, self._yao_meditation_coverage(snapshot, p.source_id, windows)) == "打坐曜")
                if data_job_for(p.job) == "曜" else (
                    self._alchemist_used_sacrifice(snapshot, p.source_id)
                    if data_job_for(p.job) == "药剂师" else norm(p.job) in HEALER_JOBS))}
            shared_unmet_healing = sum(amount * groups.share(
                player.source_id, demand.losses[index].target_id,
                demand.losses[index].timestamp_ms / 1000.0, providers)
                for index, amount in (demand.unmet_by_loss or {}).items())
        volume_denominator = effective_healing + shared_unmet_healing
        effective_realization = (
            _clamp_score(effective_healing / volume_denominator * 100.0)
            if volume_denominator > 0 else
            100.0 if healable_loss > 0 and unmet_healing <= 0 else cast_completion
        )
        rescue_detail.update({
            "healing_volume_method": (
                "factual-hp-restored-v1" if volume_denominator > 0
                else "need-settled-by-other-source-v1" if healable_loss > 0 and unmet_healing <= 0
                else "cast-evidence-fallback-v1"
            ),
            "healing_volume_restored": effective_healing,
            "healing_volume_healable_loss": healable_loss,
            "healing_volume_matched_team": matched_healing,
            "healing_volume_unmet_team": unmet_healing,
            "healing_volume_settled_without_attribution": max(
                0.0, healable_loss - matched_healing - unmet_healing
            ),
            "healing_volume_shared_unmet": shared_unmet_healing,
            "healing_cast_completion": cast_completion,
        })

        damage_reduction_completion = (
            self._healer_regular_damage_reduction_completion(
                snapshot,
                player,
                job,
                windows,
                healer_count,
            )
        )

        base_score = _clamp_score(
            effective_realization * effective_weights["healing"]
            + damage_reduction_completion * effective_weights["mitigation"]
        )
        completion = _clamp_score(base_score - rescue_penalty["penalty"])
        rescue_detail["base_score"] = base_score
        rescue_detail["applied_penalty"] = base_score - completion
        return _HealingCompletion(
            score=completion,
            effective_realization=effective_realization,
            rescue_completion=rescue_completion,
            damage_reduction_completion=damage_reduction_completion,
            effective_healing=effective_healing,
            pressure_windows=0,
            meaningful_casts=meaningful_casts,
            total_casts=total_casts,
            evaluated_casts=evaluated_casts,
            cast_completion=cast_completion,
            rescue_detail=rescue_detail,
        )
    def _taunt_coverage_detail(
        self, snapshot, player, job, output_windows, boss_target_ids=None,
    ) -> dict[str, Any]:
        """Target/provider-scoped uptime, not the number of taunt button presses."""
        from .combat_skill_levels import _USAGE_ACTION_KINDS

        platform = event_platform_for(snapshot)
        windows = _merge_intervals(output_windows)
        detail = {"completion": 100.0, "actual": None, "theory": None, "basis": "无评价区间"}
        if not windows:
            return detail
        learned = self._combat_learned_skills(snapshot, player.source_id, job)
        defaults = self._learned_skills(job)
        learned = defaults if learned is None else learned
        tree = self.store.skill_build_tree(job, 70).get("skills", {})
        names = {canonical_skill(r["name"]): r["name"] for r in tree.values()
                 if canonical_skill(r["name"]) in _TAUNT_SKILLS}
        for row in tree.values():
            # EX is a passive switch here; its zero CD must not replace the
            # active base skill's timing after canonicalisation.
            if norm(row["name"]) in names:
                names[norm(row["name"])] = row["name"]
        skill_ids = {str(sid): canonical_skill(r["name"]) for sid, r in tree.items()
                     if canonical_skill(r["name"]) in _TAUNT_SKILLS}
        if not names:
            return {**detail, "basis": "不适用"}

        def taunt_key(row):
            key = canonical_skill(row.skill_name)
            if key not in _TAUNT_SKILLS:
                return ""
            # Do not count the stun/crit reduction of Taunt or the separate
            # 11s status of Punishing Swing as the threat effect itself.
            if getattr(row, "key", "") == "挑衅":
                return key
            expected_code = "134600" if key == "挑衅" else "134900"
            return key if getattr(row, "effect_code", "") == expected_code else ""

        effects = [(row, taunt_key(row)) for row in platform.boss_effects]
        states = [(row, taunt_key(row)) for row in platform.ordered_boss_effect_events]
        parameters = [row for row in platform.stream("skill_parameter")
                      if row.packet_kind == "skill_parameter_status"
                      and row.effect_class_id == 132
                      and canonical_skill(row.skill_name) in _TAUNT_SKILLS]
        selected = set(boss_target_ids or ())
        if not selected and platform.damage_targets:
            from .aggregator import CombatAggregator
            selected = set(CombatAggregator._primary_boss_target_ids(list(platform.damage_targets)))
        if not selected:
            players = {p.source_id for p in platform.players}
            selected = {row.target_id for row, key in (*effects, *states) if key and row.target_id}
            selected.update(row.target_id for row in parameters if row.target_id)
            selected.difference_update(players)
        if not selected:
            return {**detail, "completion": 0.0, "basis": "目标不明确，未估算"}

        # With several Bosses, a leftover state on Boss A must not cover Boss B.
        target_windows = {
            target: (windows if len(selected) == 1 else _intersect_intervals(
                windows, self._output_windows(snapshot, frozenset({target}))))
            for target in selected
        }
        from .duty_groups import groups_for
        groups = groups_for(self, snapshot)
        target_windows = {target: groups.boss_windows(player.source_id, target, rows)
                          for target, rows in target_windows.items()}
        target_windows = {target: rows for target, rows in target_windows.items() if rows}
        denominator = sum(end-start for rows in target_windows.values() for start, end in rows)
        if denominator <= 0:
            return detail
        aggregates = defaultdict(list)
        transitions = defaultdict(list)
        observed_durations = defaultdict(set)
        status_targets = set()
        for row, key in effects:
            if not key or row.target_id not in target_windows:
                continue
            status_targets.add(row.target_id)
            if row.source_id == player.source_id and key in names:
                aggregates[(row.target_id, key)].extend(
                    row.intervals or ((row.start_seconds, row.end_seconds),))
        for row, key in states:
            if not key or row.target_id not in target_windows:
                continue
            status_targets.add(row.target_id)
            if row.source_id == player.source_id and key in names:
                removing = row.operation == "remove"
                transitions[(row.target_id, key)].append(
                    (row.timestamp_seconds, removing, row.duration_seconds))
                if not removing and row.duration_seconds > 0:
                    observed_durations[key].add(round(row.duration_seconds, 6))
        for row in parameters:
            if row.target_id not in target_windows:
                continue
            status_targets.add(row.target_id)
            key = canonical_skill(row.skill_name)
            if (row.source_id == player.source_id and key in names and row.duration_ms > 0
                    and skill_ids.get(str(row.raw_skill_id or row.skill_id)) == key):
                duration = row.duration_ms / 1000.0
                transitions[(row.target_id, key)].append((row.timestamp_ms/1000.0, False, duration))
                observed_durations[key].add(round(duration, 6))

        def status_intervals(events):
            result = []
            opened = None
            expiry = 0.0
            for seconds, removing, duration in sorted(set(events)):
                if opened is not None:
                    if min(seconds, expiry) > opened:
                        result.append((opened, min(seconds, expiry)))
                    opened = None
                if not removing and duration > 0:
                    opened, expiry = seconds, seconds + duration
            if opened is not None:
                result.append((opened, expiry))
            return _merge_intervals(result)

        factual_keys = {key for _target, key in (*aggregates, *transitions)}
        skills = {key for key in names if learned.get(key, 0) > 0 or key in factual_keys}
        if not skills:
            return {**detail, "basis": "不适用"}
        timings = {}
        for key in skills:
            level = max(1, learned.get(key, 0) or defaults.get(key, 1))
            timing = self.store.skill_effect_timing(job, names[key], level)
            duration = as_number(timing.get("duration"))
            # Explicit threat-status lifetimes include observed duration glyphs
            # and EX effects. Never use a merged track length as one cast's life.
            durations = observed_durations.get(key, set())
            if len(durations) == 1:
                duration = next(iter(durations))
            cooldown = as_number(timing.get("cooldown"))
            timings[key] = (cooldown, duration)
        modifiers = self._combat_modifier_windows(snapshot, player.source_id)
        casts = [cast for cast in platform.event_index.casts_by_source.get(player.source_id, ())
                 if cast.packet_kind in _USAGE_ACTION_KINDS
                 and canonical_skill(cast.skill_name) in skills]
        covered = ideal = 0.0
        estimated = False
        unknown_theory = False
        for target, scope in target_windows.items():
            actual_rows, theory_rows = [], []
            for key in skills:
                pair = (target, key)
                if pair in aggregates:
                    actual_rows.extend(aggregates[pair])
                elif pair in transitions:
                    actual_rows.extend(status_intervals(transitions[pair]))
                elif target not in status_targets:
                    # Only legacy recordings without threat-state evidence
                    # may fall back to reliable actions. A missed application
                    # must not fill holes in an existing Boss status ledger.
                    for cast in casts:
                        if canonical_skill(cast.skill_name) != key:
                            continue
                        start = cast.timestamp_ms / 1000.0
                        if len(selected) > 1:
                            candidates = [t for t, w in target_windows.items() if _in_intervals(start, w)]
                            if candidates != [target]:
                                continue
                        duration = timings[key][1]
                        if duration > 0:
                            actual_rows.append((start, start + duration))
                            estimated = True
                cooldown, duration = timings[key]
                if duration <= 0 or cooldown <= 0:
                    unknown_theory = True
                    theory_rows.extend(scope)  # Conservative full-coverage reference.
                else:
                    expectation = _SupportExpectation(key, names[key], "挑衅", 1.0, 0.0,
                                                      "debuff", cooldown, duration)
                    theory_rows.extend(self._theory_support_intervals(
                        expectation, scope[-1][1], scope, modifiers))
            covered += sum(end-start for start, end in _intersect_intervals(
                _merge_intervals(actual_rows), scope))
            ideal += sum(end-start for start, end in _intersect_intervals(
                _merge_intervals(theory_rows), scope))
        basis = "Boss状态"
        if estimated:
            basis = "混合（含施放估算）" if status_targets else "可靠施放估算"
        elif not status_targets:
            basis = "未获得挑衅状态或可靠施放证据"
        if unknown_theory:
            basis += "；理论持续时间未知，按全覆盖参考"
        if groups.segments:
            basis += "；双Boss分组阶段按持续攻击目标限定职责，未知分组不豁免"
        return {"completion": covered / ideal * 100.0 if ideal > 0 else 0.0,
                "actual": min(100.0, covered / denominator * 100.0),
                "theory": None if unknown_theory else min(100.0, ideal / denominator * 100.0),
                "basis": basis}

    def _tank_shell_theory_detail(
        self, snapshot, player, job, skill_key, output_windows, theory_plan,
    ) -> dict[str, Any]:
        """Project shell lifetimes from this provider's final cast schedule.

        Do not use duration / CD, reconstruct another schedule, or borrow a
        same-job teammate's casts. Raw segments include zero-damage supports.
        """
        from math import isfinite

        detail = {"theory": None, "casts": 0, "basis": "缺少施放者理论轴"}
        if (not isinstance(theory_plan, dict)
            or not isinstance(theory_plan.get("segments"), (list, tuple))):
            return detail
        windows = _merge_intervals(output_windows)
        if not windows:
            return {**detail, "basis": "无有效输出区间"}
        casts = []
        offset = max(0.0, as_number(theory_plan.get("_score_time_offset")))
        if not isfinite(offset):
            return {**detail, "basis": "理论轴起点无效"}
        for row in theory_plan.get("segments", ()):
            if not isinstance(row, dict) or canonical_skill(row.get("skill")) != skill_key:
                continue
            # Plans are already provider-scoped. Honour an explicit owner if
            # supplied, without accepting an external same-name effect lane.
            if row.get("source_id") and str(row["source_id"]) != player.source_id:
                continue
            start = as_number(row.get("start"), -1.0)
            end = as_number(row.get("end"), start)
            if not (isfinite(start) and isfinite(end)) or start < 0 or end < start:
                continue
            casts.append((offset + start, offset + end))
        casts = sorted(set(casts))
        if not casts:
            return {**detail, "theory": 0.0, "basis": "施放者理论轴未安排蛋壳"}

        report = theory_plan.get("_score_combat_skill_levels")
        levels = report.get("levels") if isinstance(report, dict) else None
        if not isinstance(levels, dict):
            levels = self._combat_learned_skills(snapshot, player.source_id, job)
        levels = self._learned_skills(job) if levels is None else levels
        level = max(1, int(as_number(levels.get(skill_key), 1)))
        # Range Bomb is the active cast; its EX row owns the shell lifetime.
        # Use this job's matching effect rows, not the base attack's zero duration
        # or the passive EX's zero cooldown as a full-coverage assumption.
        candidates = [
            (name, as_number(self.store.skill_effect_timing(job, name, level).get("duration")))
            for name in self.store.full_skill_names(job)
            if canonical_skill(name) == skill_key
        ]
        skill_name, duration = max(candidates, key=lambda row: row[1], default=(skill_key, 0.0))
        if duration <= 0 or not isfinite(duration):
            return {**detail, "casts": len(casts), "basis": "蛋壳持续时间未确认"}
        expectation = _SupportExpectation(skill_key, skill_name, "蛋壳", 0.0, 0.0,
                                          "buff", 0.0, duration)
        duration = self._support_expectations_with_glyphs((expectation,), theory_plan)[0].duration
        # Consistent with ordinary team-buff application on the shared planner:
        # the effect begins on cast completion. Never confuse cast duration with
        # shield lifetime; cooldown/speed/resources are already in the plan.
        intervals = [(end, end + duration) for _start, end in casts]
        coverage = _coverage_in_windows(intervals, windows) * 100.0
        relevant_casts = sum(bool(_intersect_intervals((interval,), windows))
                             for interval in intervals)
        return {"theory": coverage, "casts": relevant_casts,
                "intervals": tuple(intervals),
                "basis": ("施放者理论轴" if coverage > 0
                          else "理论蛋壳未覆盖有效输出区间")}

    def _tank_completion(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        job: str,
        output_windows: tuple[tuple[float, float], ...],
        boss_target_ids: frozenset[str] | None = None,
        *, attack_down_detail: dict[str, Any] | None = None,
        taunt_detail: dict[str, Any] | None = None,
        shell_detail: dict[str, Any] | None = None,
        theory_plan: dict[str, Any] | None = None,
    ) -> tuple[float, float, float | None, float | None]:
        learned = self._learned_skills(job)
        # Real coverage is wall-clock uptime. Union every attack-down interval
        # owned by this provider before measuring it, so overlapping refreshes
        # count once while complementary skills can fill each other's gaps.
        attack_actual_intervals = _merge_intervals(
            interval
            for effect in event_platform_for(snapshot).boss_effects
            if effect.source_id == player.source_id
            and effect.key == "减攻击%"
            and (
                not boss_target_ids
                or effect.target_id in boss_target_ids
            )
            for interval in (
                effect.intervals
                or ((effect.start_seconds, effect.end_seconds),)
            )
        )
        attack_actual_coverage = _coverage_in_windows(
            attack_actual_intervals,
            output_windows,
        )

        # The numerator contains every learned attack-down skill, therefore its
        # denominator must be the union of those same skills on the shared
        # theory timeline. The old best-single-skill duration/CD denominator
        # could turn 94% real uptime into a misleading 300% completion.
        combat_learned = self._combat_learned_skills(
            snapshot,
            player.source_id,
            job,
        ) or learned
        attack_expectations_list: list[_SupportExpectation] = []
        for skill_name in self.store.full_skill_names(job):
            skill_key = canonical_skill(skill_name)
            level = int(as_number(combat_learned.get(skill_key)))
            if level <= 0:
                continue
            matching_rules = tuple(
                rule
                for rule in boss_effect_rules(skill_name, job)
                if rule.key == "减攻击%"
            )
            if not matching_rules:
                continue
            timing = self.store.skill_effect_timing(job, skill_name, level)
            cooldown = max(0.0, as_number(timing.get("cooldown")))
            for rule in matching_rules:
                duration = max(
                    0.0,
                    as_number(rule.duration_seconds),
                    as_number(timing.get("duration")),
                )
                if duration <= 0:
                    continue
                attack_expectations_list.append(_SupportExpectation(
                    skill_key=skill_key,
                    skill_name=skill_name,
                    effect_key=rule.key,
                    theory=(
                        1.0
                        if cooldown <= 0
                        else min(1.0, duration / cooldown)
                    ),
                    magnitude=abs(as_number(rule.value, 1.0)) or 1.0,
                    kind="debuff",
                    cooldown=cooldown,
                    duration=duration,
                    stackable=rule.stackable,
                    stack_group=rule.stack_group,
                    proc_chance=max(0.0, min(1.0, as_number(rule.proc_chance, 1.0))),
                ))
        attack_expectations = tuple(attack_expectations_list)
        attack_expectations = self._support_expectations_with_glyphs(
            attack_expectations,
            theory_plan,
        )
        attack_theory_plan = (
            theory_plan
            if isinstance(theory_plan, dict)
            and isinstance(theory_plan.get("segments"), (list, tuple))
            else None
        )
        attack_theory_intervals = _merge_intervals(
            interval
            for expectation in attack_expectations
            for interval in self._planned_support_intervals(
                attack_theory_plan,
                expectation,
                max((end for _start, end in output_windows), default=0.0),
            )
        )
        planned_attack_theory = _coverage_in_windows(
            attack_theory_intervals,
            output_windows,
        )
        required_attack_skill_keys = {
            expectation.skill_key for expectation in attack_expectations
        }
        planned_attack_skill_keys = {
            canonical_skill(segment.get("skill"))
            for segment in (
                attack_theory_plan.get("segments", ())
                if attack_theory_plan is not None
                else ()
            )
            if isinstance(segment, dict)
        }
        missing_attack_skill_keys = (
            required_attack_skill_keys - planned_attack_skill_keys
        )
        if planned_attack_theory > 1e-9 and not missing_attack_skill_keys:
            attack_theory_coverage = planned_attack_theory
            attack_basis = "已学习减攻击技能的施放者理论轴联合覆盖"
        else:
            # Missing/stale plans use the documented interim target instead of
            # recreating a small and incompatible duration/CD denominator.
            attack_theory_coverage = 0.90
            missing_names = [
                expectation.skill_name
                for expectation in attack_expectations
                if expectation.skill_key in missing_attack_skill_keys
            ]
            attack_basis = (
                f"理论轴缺少已学减攻击技能（"
                f"{'、'.join(dict.fromkeys(missing_names))}），使用90%标准目标"
                if attack_theory_plan is not None and missing_names
                else "理论轴未确认，使用90%标准目标"
            )
        attack_down_score = _ratio_score(
            attack_actual_coverage,
            attack_theory_coverage,
            neutral=100.0,
            allow_overflow=False,
        )
        if attack_down_detail is not None:
            attack_down_detail.update({
                "actual": attack_actual_coverage * 100.0,
                "theory": attack_theory_coverage * 100.0,
                "basis": attack_basis,
            })
        shell_skills = {
            canonical_skill(name) for name in _TANK_SHELL_SKILLS if canonical_skill(name) in learned
        }
        taunt = self._taunt_coverage_detail(snapshot, player, job, output_windows, boss_target_ids)
        taunt_score = taunt["completion"]
        if taunt_detail is not None:
            taunt_detail.update(taunt)
        platform = event_platform_for(snapshot)
        party_ids = tuple(dict.fromkeys(
            row.source_id for row in platform.players if row.source_id
        )) or (player.source_id,)
        shell_ratios: list[float] = []
        shell_coverages: list[float] = []
        shell_theories: list[dict[str, Any]] = []
        from .duty_groups import groups_for
        groups = groups_for(self, snapshot)
        recipient_windows = {target: groups.recipients(player.source_id, target, output_windows)
                             for target in party_ids}
        recipient_seconds = sum(b-a for rows in recipient_windows.values() for a,b in rows)
        for key in sorted(shell_skills):
            theory_detail = self._tank_shell_theory_detail(
                snapshot, player, job, key, output_windows, theory_plan,
            )
            shell_theories.append(theory_detail)
            theory = theory_detail["theory"]
            by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
            series_targets: set[str] = set()
            for effect in platform.player_buff_series:
                if (effect.source_id == player.source_id
                    and canonical_skill(effect.skill_name) == key
                    and effect.target_id in party_ids):
                    by_target[effect.target_id].extend(effect.intervals)
                    series_targets.add(effect.target_id)
            for effect in platform.player_buffs:
                if (effect.source_id == player.source_id
                    and canonical_skill(effect.skill_name) == key
                    and effect.target_id in party_ids
                    and effect.target_id not in series_targets):
                    by_target[effect.target_id].extend(
                        effect.intervals or ((effect.start_seconds, effect.end_seconds),)
                    )
            # Keep every party member in the denominator, including those who
            # never received this provider's shell. Interval union prevents
            # refreshes, duplicate rows and multiple status keys inflating it.
            actual = sum(
                _coverage_in_windows(by_target.get(target_id, ()), output_windows)
                for target_id in party_ids
            ) / len(party_ids)
            if groups.segments and recipient_seconds > 0:
                actual = sum(b-a for target, scope in recipient_windows.items()
                             for a,b in _intersect_intervals(
                                 _merge_intervals(by_target.get(target, ())), scope)) / recipient_seconds
                if theory is not None and "intervals" in theory_detail:
                    theory = sum(b-a for scope in recipient_windows.values()
                                 for a,b in _intersect_intervals(
                                     _merge_intervals(theory_detail["intervals"]), scope)) / recipient_seconds * 100.0
                    theory_detail["theory"] = theory
                theory_detail["basis"] += "；按同组队员有效人秒评价实战与理论覆盖"
            shell_coverages.append(actual)
            if theory is not None and theory > 1e-9:
                shell_ratios.append(actual * 100.0 / theory)
        if shell_detail is not None:
            shell_detail.update({
                "actual": (sum(shell_coverages) / len(shell_coverages) * 100.0
                           if shell_coverages else None),
                "party_size": len(party_ids) if shell_skills else None,
                "eligible_recipient_seconds": recipient_seconds,
                "theory": (sum(row["theory"] for row in shell_theories) / len(shell_theories)
                           if shell_theories and all(row["theory"] is not None for row in shell_theories)
                           else None),
                "casts": sum(row["casts"] for row in shell_theories),
                "basis": "；".join(dict.fromkeys(row["basis"] for row in shell_theories)),
            })
        shell_evaluated = bool(shell_skills) and len(shell_ratios) == len(shell_skills)
        shell_score = (
            max(0.0, sum(shell_ratios) / len(shell_ratios) * 100.0)
            if shell_evaluated
            else 100.0
        )
        # An absent/zero theoretical denominator is not evidence of failure or
        # perfect coverage. Leave this duty neutral and display it as pending.
        # These are three fixed, equal T duties. Individual completion may
        # exceed 100%, allowing exceptional coverage in one duty to compensate
        # another, while the final 40-point specialty contribution remains
        # capped by the caller.
        completion = _tank_duty_completion(
            attack_down_score,
            shell_score,
            taunt_score,
        )
        return (
            completion,
            attack_down_score,
            taunt_score if taunt["basis"] != "不适用" else None,
            shell_score if shell_evaluated else None,
        )


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreRoleDutiesMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
