from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for
from .combat_skill_levels import skill_level_display_rows
from ..background_priority import score_checkpoint

class ScoreEngineMixin:
    def castability_by_source(self, snapshot):
        """Shared capture-relative permissions; reusable by resource mechanics."""
        if getattr(self, '_castability_snapshot', None) is snapshot:
            return self._castability_cache
        from .castability import build_castability_by_source
        platform = event_platform_for(snapshot)
        result = build_castability_by_source(
            platform.players, platform.event_index.skill_cast_events,
            self._observed_cast_timings(snapshot),
            platform.player_lifecycle_events, platform.elapsed_seconds,
            canonical_skill=canonical_skill,
        )
        self._castability_snapshot = snapshot
        self._castability_cache = result
        return result

    def _observed_cast_timings(
        self, snapshot: CombatSnapshot
    ) -> dict[tuple[str, int, str], tuple[float, float]]:
        """Resolve action-speed-aware duration and speed for every observed cast."""
        if self._cast_timing_snapshot is snapshot:
            return self._cast_timing_cache
        platform = event_platform_for(snapshot)
        recipient_speed_windows: dict[
            tuple[str, str], list[tuple[float, float]]
        ] = {}
        for row in platform.player_buff_series:
            skill_key = canonical_skill(getattr(row, "skill_name", ""))
            if skill_key not in {
                canonical_skill("精神爆发"),
                canonical_skill("灼热火焰"),
            }:
                continue
            target_id = str(getattr(row, "target_id", ""))
            if not target_id:
                continue
            intervals = _recorded_timing_buff_intervals(row, skill_key)
            if not intervals:
                continue
            recipient_speed_windows.setdefault(
                (target_id, skill_key), []
            ).extend(intervals)
        resolver = LiveCastTimingResolver(
            self.store,
            player_buff_clear_times_by_target(
                platform.player_buff_clear_events
            ),
            recipient_speed_windows=recipient_speed_windows,
            authoritative_team_speed=hasattr(snapshot, "player_buff_series"),
        )
        timings: dict[tuple[str, int, str], tuple[float, float]] = {}
        for event in platform.event_index.skill_cast_events:
            timing = resolver.resolve(event, event.timestamp_ms / 1000.0)
            timings[
                (
                    event.source_id,
                    event.timestamp_ms,
                    canonical_skill(event.skill_name),
                )
            ] = (timing.cast_time, timing.action_speed_percent)
        self._cast_timing_snapshot = snapshot
        self._cast_timing_cache = timings
        return timings
    def score_exclusions(
        self,
        snapshot: CombatSnapshot,
        theory_plans: dict[str, dict[str, Any]] | None = None,
        *,
        boss_target_ids: frozenset[str] | None = None,
        actual_timeline_events: tuple[SkillCastEvent, ...] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        allow_parallel: bool = True,
    ) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """Find low-realization theory skills without calculating full scores.

        The exclusion decision is made at the same point in ``_score_player``
        as before. Support counterfactuals, survival discipline, benchmark
        normalization and final score composition happen only after the
        affected theory axes have been rebuilt.
        """

        started = time.perf_counter()
        actual_timeline_events = _score_cast_axis(actual_timeline_events)
        authoritative_cast_axis = actual_timeline_events is not None
        score_snapshot = (
            replace(snapshot, skill_cast_events=actual_timeline_events)
            if authoritative_cast_axis
            else snapshot
        )
        plans = theory_plans or {}
        if plans and all(p.get('_score_v2') for p in plans.values()):
            self._score_exclusion_diagnostics={'disabled_for_v2':True,'excluded_player_count':0}
            return {}
        output_windows = self._output_windows(score_snapshot, boss_target_ids)
        players = tuple(event_platform_for(score_snapshot).players)
        total_players = len(players)
        resolved: tuple[Any, ...] | None = None
        parallel_progress_reported = False
        if allow_parallel:
            try:
                from ..timeline_parallel import calculate_team_score_players_parallel

                resolved = calculate_team_score_players_parallel(
                    score_snapshot,
                    players,
                    output_windows,
                    plans,
                    boss_target_ids,
                    None,
                    (),
                    authoritative_cast_axis,
                    self._score_boss_name,
                    {
                        "monster_defense": self._score_boss_defense,
                        "crit_resist": self._score_boss_crit_resist,
                        "element_resist": self._score_boss_element_resist,
                    },
                    {},
                    death_windows=(),
                    exclusion_only=True,
                    progress_callback=progress_callback,
                )
                parallel_progress_reported = resolved is not None
            except Exception:
                resolved = None
        if resolved is None:
            resolved = tuple(
                self._score_player(
                    score_snapshot,
                    player,
                    output_windows,
                    plans.get(player.source_id),
                    boss_target_ids,
                    None,
                    (),
                    (),
                    authoritative_cast_axis,
                    exclusion_only=True,
                )
                for player in players
            )
        result: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        for index, (player, row) in enumerate(
            zip(players, resolved, strict=True),
            start=1,
        ):
            if (
                isinstance(row, tuple)
                and len(row) == 2
                and isinstance(row[0], frozenset)
                and isinstance(row[1], frozenset)
            ):
                result[str(player.source_id)] = (row[0], row[1])
            if progress_callback is not None and not parallel_progress_reported:
                progress_callback(index, total_players, player.job)
        self._score_exclusion_diagnostics = {
            "player_count": total_players,
            "excluded_player_count": sum(bool(row[0]) for row in result.values()),
            "total_seconds": time.perf_counter() - started,
        }
        return result
    def _score_player(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        output_windows: tuple[tuple[float, float], ...],
        theory_plan: dict[str, Any] | None = None,
        boss_target_ids: frozenset[str] | None = None,
        support_context: _SupportCounterfactualContext | None = None,
        specialty_windows: tuple[tuple[float, float], ...] = (),
        death_windows: tuple[tuple[float, float], ...] = (),
        authoritative_cast_axis: bool = False,
        *,
        exclusion_only: bool = False,
    ) -> PlayerScore | tuple[frozenset[str], frozenset[str]]:
        job = norm(player.job)
        role_team_windows = output_windows
        personal=(theory_plan or {}).get('_score_v2',{}).get('player_output_windows')
        if personal is not None:
            output_windows=_intersect_intervals(tuple(tuple(w)for w in personal),output_windows)
        data_job = data_job_for(job)
        yao_meditation_coverage = (
            self._yao_meditation_coverage(
                snapshot,
                player.source_id,
                role_team_windows,
                boss_target_ids,
            )
            if job in _YAO_JOBS or data_job == "曜"
            else 0.0
        )
        is_yao = job in _YAO_JOBS or data_job == "曜"
        yao_variant = yao_rotation_variant(job, yao_meditation_coverage)
        is_seated_yao = yao_variant == "打坐曜"
        is_alchemist = job in _ALCHEMIST_JOBS
        alchemist_variant = alchemist_rotation_variant(
            job,
            self._alchemist_used_sacrifice(snapshot, player.source_id)
            if is_alchemist else False,
        )
        role = (
            "奶/辅助"
            if is_seated_yao or alchemist_variant == "辅助药剂"
            else ("输出" if is_yao or is_alchemist else role_for_job(job))
        )
        score_glyph_rows = (
            theory_plan.get("_score_glyphs", ())
            if isinstance(theory_plan, dict)
            else ()
        )
        score_glyph_source = (
            str(theory_plan.get("_score_glyph_source", "") or "")
            if isinstance(theory_plan, dict)
            else ""
        )
        combat_learned = self._combat_learned_skills(snapshot, player.source_id, data_job)
        profiles = (
            tuple(
                self._apply_selected_glyphs(
                    list(self._job_base_profiles(data_job, combat_learned)),
                    score_glyph_rows,
                    data_job,
                    self.store,
                )
            )
            if isinstance(score_glyph_rows, (list, tuple))
            and (score_glyph_rows or score_glyph_source)
            else (tuple(self._apply_best_glyphs(data_job, list(self._job_base_profiles(data_job, combat_learned))))
                  if combat_learned is not None else self._job_profiles(data_job))
        )
        profiles = self._profiles_with_observed_cast_skills(
            profiles,
            score_snapshot=snapshot,
            source_id=player.source_id,
            job=data_job,
            output_windows=output_windows,
            learned_override=combat_learned,
        )
        profiles = self._profiles_with_standardized_skill_powers(
            profiles,
            theory_plan,
        )
        profile_by_key = {row.key: row for row in profiles}
        (
            actual_casts,
            direct_cast_ratio,
            direct_skill_count,
            observed_skill_count,
            direct_cast_count,
            inferred_cast_count,
        ) = self._actual_casts(
            snapshot,
            player.source_id,
            profile_by_key,
            output_windows,
            job=job,
            infer_missing_from_damage=not authoritative_cast_axis,
        )
        # Preserve the factual release denominator before channel/summon
        # valuation reshapes rows for damage scoring.  Presentation fields
        # such as ``actual_count`` must describe how many times the player
        # pressed/cast the skill, not how many integrated periodic-damage rows
        # the scoring model produced afterward.
        factual_actual_counts = Counter(row.key for row in actual_casts)
        modifier_windows = self._combat_modifier_windows(snapshot, player.source_id)
        actual_casts = self._value_actual_casts(
            actual_casts,
            profile_by_key,
            output_windows,
            job,
            modifier_windows,
            theory_plan,
            player.source_id,
            tuple(event_platform_for(snapshot).summon_removal_events),
        )
        summon_limits = _scoring_summon_limits(job)
        persistent_summon_keys = set(_scoring_persistent_summon_cycles()) & set(
            profile_by_key
        )
        periodic_damage_keys = set(
            _scoring_periodic_damage_durations()
        ) & set(profile_by_key)
        proportional_keys = set(_proportional_channel_skills(job)) | set(
            summon_limits
        ) | persistent_summon_keys | periodic_damage_keys
        theory_casts = self._planned_theoretical_casts(
            theory_plan,
            profile_by_key,
            output_windows,
            job,
            modifier_windows,
        )
        if not theory_casts:
            theory_casts = self._theoretical_casts(
                profiles,
                output_windows,
                modifier_windows,
                job,
            )
        if persistent_summon_keys:
            persistent_actual, persistent_theory = _persistent_summon_score_casts(
                profile_by_key,
                output_windows,
                theory_plan,
                player.source_id,
            )
            actual_casts = [
                row for row in actual_casts if row.key not in persistent_summon_keys
            ]
            actual_casts.extend(persistent_actual)
            actual_casts.sort(key=lambda row: (row.seconds, row.key))
            theory_casts = [
                row for row in theory_casts if row.key not in persistent_summon_keys
            ]
            theory_casts.extend(persistent_theory)
            theory_casts.sort(key=lambda row: (row.seconds, row.key))

        v2_detail = {}
        if isinstance(theory_plan, dict) and theory_plan.get('_score_v2'):
            from .score import value_actual, detail_for
            v2_detail = detail_for(theory_plan, actual_casts)
            actual_casts = value_actual(actual_casts, profile_by_key, theory_plan, job)
        actual_counts = Counter(row.key for row in actual_casts)
        theory_counts = Counter(row.key for row in theory_casts)
        for key, factual_count in factual_actual_counts.items():
            if key not in persistent_summon_keys:
                actual_counts[key] = factual_count
        for key in persistent_summon_keys:
            actual_counts[key] = 1
            theory_counts[key] = 1
        actual_power_by_skill: dict[str, float] = defaultdict(float)
        actual_duration_by_skill: dict[str, float] = defaultdict(float)
        expected_duration_by_skill: dict[str, float] = defaultdict(float)
        for row in actual_casts:
            score_checkpoint()
            actual_power_by_skill[row.key] += row.power
            if row.proportional_output:
                actual_duration_by_skill[row.key] += row.utilized_duration
                expected_duration_by_skill[row.key] += row.expected_duration
        theory_power_for_exclusion: dict[str, float] = defaultdict(float)
        for row in theory_casts:
            score_checkpoint()
            theory_power_for_exclusion[row.key] += row.power
        high_value_keys = _high_value_skill_keys(theory_power_for_exclusion)
        preplanned_excluded_score_keys = {
            canonical_skill(skill)
            for skill in (
                theory_plan.get("_score_excluded_skill_keys", ())
                if isinstance(theory_plan, dict)
                else ()
            )
            if canonical_skill(skill)
        }
        preplanned_excluded_high_value_keys = {
            canonical_skill(skill)
            for skill in (
                theory_plan.get("_score_excluded_high_value_keys", ())
                if isinstance(theory_plan, dict)
                else ()
            )
            if canonical_skill(skill)
        }
        low_damage_candidates = [
            key
            for key, theory_power in theory_power_for_exclusion.items()
            if _excluded_from_score_for_low_damage(
                actual_power_by_skill[key], theory_power
            )
        ]
        # High- and low-value skills use independent omission budgets so filler
        # skills do not consume the single high-value exception. A high-value
        # skill with a real cast is demonstrably learned and required by this
        # character's rotation; poor realization must remain scoreable instead
        # of becoming a free build exemption. The caps also prevent a zero-
        # output player from erasing the whole rotation.
        def exclusion_priority(key: str) -> tuple[float, int, str]:
            return (
                -theory_power_for_exclusion[key],
                -theory_counts[key],
                key,
            )

        high_value_candidates = sorted(
            (
                key
                for key in low_damage_candidates
                if key in high_value_keys
            ),
            key=exclusion_priority,
        )
        low_value_candidates = sorted(
            (key for key in low_damage_candidates if key not in high_value_keys),
            key=exclusion_priority,
        )
        if preplanned_excluded_score_keys:
            # A V4.10 second-pass plan has already removed these skills and
            # filled the released action time. Keep that exact exemption set;
            # selecting fresh candidates here would exclude rows that were not
            # replanned and would recreate an artificially sparse denominator.
            excluded_score_keys = set(preplanned_excluded_score_keys)
        else:
            excluded_score_keys = set(
                high_value_candidates[
                    :_MAX_SCORE_HIGH_VALUE_EXCLUSIONS_PER_JOB
                ]
            )
            excluded_score_keys.update(
                low_value_candidates[:_MAX_SCORE_LOW_VALUE_EXCLUSIONS_PER_JOB]
            )
        if isinstance(theory_plan, dict) and theory_plan.get('_score_v2'):
            # V2 uses observed levels and hit weights, with no omission exemption.
            excluded_score_keys=set()
            preplanned_excluded_high_value_keys=set()
        if exclusion_only:
            return (
                frozenset(excluded_score_keys),
                frozenset(excluded_score_keys & high_value_keys),
            )
        scored_actual_casts = [
            row for row in actual_casts if row.key not in excluded_score_keys
        ]
        scored_theory_casts = [
            row for row in theory_casts if row.key not in excluded_score_keys
        ]

        # Exemption removes one skill from both sides of the score ledger. Its
        # observed casts remain auditable on the factual timeline, but cannot
        # become denominator-free bonus damage and inflate a weak rotation.
        actual_standard_power = sum(row.power for row in scored_actual_casts)
        theory_standard_power = sum(row.power for row in scored_theory_casts)
        theory_damage_completion = _ratio_score(
            actual_standard_power,
            theory_standard_power,
            neutral=0.0,
            allow_overflow=True,
        )

        theoretical_by_skill: dict[str, float] = defaultdict(float)
        for row in theory_casts:
            if row.key in profile_by_key:
                theoretical_by_skill[row.key] += row.power
        scored_theoretical_by_skill = {
            key: value
            for key, value in theoretical_by_skill.items()
            if key not in excluded_score_keys
        }
        top_keys = high_value_keys - excluded_score_keys
        high_actual = sum(
            min(actual_power_by_skill[key], scored_theoretical_by_skill[key])
            for key in top_keys
        )
        high_theory = sum(scored_theoretical_by_skill[key] for key in top_keys)
        high_value_usage = _ratio_score(high_actual, high_theory, neutral=0.0)

        burst_window_rows = tuple(
            ScoreWindowDetail(
                label=row.label,
                start_seconds=start,
                end_seconds=end,
                original_duration=getattr(row,"original_duration",0.) or row.end_seconds-row.start_seconds,
                actual_power=0.0,
                theory_power=0.0,
                completion=100.0,
            )
            for row in self._burst_window_details(snapshot, player.source_id)
            for start, end in _intersect_intervals(
                ((row.start_seconds, row.end_seconds),), output_windows
            )
        )
        theory_burst_window_rows = self._planned_burst_window_details(
            theory_plan,
            output_windows,
        )
        burst_windows = _merge_intervals(
            (row.start_seconds, row.end_seconds) for row in burst_window_rows
        )
        theory_burst_windows = _merge_intervals(
            (row.start_seconds, row.end_seconds)
            for row in theory_burst_window_rows
        ) or burst_windows
        from .score_burst import burst_contributions, window_power, score_weight, output_score as combine_burst_score
        burst_actual_parts, burst_theory_parts, burst_diagnostics = burst_contributions(
            scored_actual_casts, scored_theory_casts, theory_plan, job)
        if isinstance(theory_plan,dict) and theory_plan.get('_score_v2'):
            from .score_burst import filter_burst_windows
            burst_window_rows,theory_burst_window_rows,removed = filter_burst_windows(
                burst_window_rows,theory_burst_window_rows,burst_theory_parts)
            burst_windows=_merge_intervals((r.start_seconds,r.end_seconds) for r in burst_window_rows)
            theory_burst_windows=_merge_intervals((r.start_seconds,r.end_seconds) for r in theory_burst_window_rows)
            burst_diagnostics['discarded_windows']=removed
            burst_diagnostics['actual_windows']=[dict(label=r.label,start=r.start_seconds,end=r.end_seconds,original_duration=r.original_duration) for r in burst_window_rows]
            burst_diagnostics['theory_windows']=[dict(label=r.label,start=r.start_seconds,end=r.end_seconds,original_duration=r.original_duration) for r in theory_burst_window_rows]
            if not burst_windows:burst_diagnostics['unavailable_reason']='没有有效爆发窗口，输出分完全按整场输出完成率计算'
        if v2_detail:
            v2_detail['burst_diagnostics'] = burst_diagnostics
        burst_scored = bool(burst_windows)
        if burst_windows:
            actual_burst = window_power(burst_actual_parts, burst_windows)
            theory_burst = window_power(burst_theory_parts, theory_burst_windows)
            burst_usage = _ratio_score(
                actual_burst,
                theory_burst,
                neutral=100.0,
                allow_overflow=True,
            )
            if burst_diagnostics.get('mode') != 'v1_cast_start' and theory_burst <= 1e-9:
                burst_scored = False
                burst_diagnostics['unavailable_reason'] = '窗口内没有可比较的理论Project/直伤，爆发项不参与加权'
            burst_diagnostics.update(actual_window_power=actual_burst,theory_window_power=theory_burst,
                                     scored=burst_scored)
            if burst_diagnostics.get('mode') in {'project_creation_and_direct_hit','v2_skill_impact_segments'}:
                # The timeline must use the same source clocks as the final
                # burst score, rather than reconstructing cast-start buckets.
                burst_diagnostics['actual_window_parts'] = [
                    [t,p] for t,p in burst_actual_parts
                    if any(a <= t < b for a,b in burst_windows)]
                burst_diagnostics['theory_window_parts'] = [
                    [t,p] for t,p in burst_theory_parts
                    if any(a <= t < b for a,b in theory_burst_windows)]
        else:
            burst_usage = 100.0
        scored_burst_usage = min(100.0, max(0.0, burst_usage))
        burst_weight = score_weight(burst_diagnostics, burst_scored)
        burst_diagnostics['score_weight'] = burst_weight
        burst_diagnostics['scored'] = burst_scored
        timing_efficiency = _timing_efficiency(
            scored_actual_casts,
            scored_theory_casts,
            top_keys,
            burst_windows,
        )
        output_score = combine_burst_score(theory_damage_completion, scored_burst_usage, burst_weight)

        support_details: tuple[SupportScoreDetail, ...]
        coverage, contribution, support_details = self._support_scores(
            snapshot,
            player,
            data_job,
            output_windows,
            theory_plan,
            boss_target_ids,
            support_context,
            optional_support_skill_keys=(
                frozenset({_YAO_MEDITATION_SKILL}) if is_yao and not is_seated_yao
                else frozenset({_ALCHEMIST_SACRIFICE_SKILL})
                if alchemist_variant == "输出药剂" else frozenset()
            ),
        )
        healing: float | None = None
        tank: float | None = None
        healing_cast_completion: float | None = None
        effective_healing = 0
        healing_effective_realization: float | None = None
        healing_rescue_completion: float | None = None
        healing_damage_reduction_completion: float | None = None
        healing_pressure_windows = 0
        healing_meaningful_casts = 0
        healing_total_casts = 0
        healing_evaluated_casts: int | None = None
        healing_rescue_detail: dict[str, Any] = {}
        tank_attack_down: float | None = None
        tank_taunt: float | None = None
        tank_shell: float | None = None
        attack_down_detail: dict[str, Any] = {}
        taunt_detail: dict[str, Any] = {}
        shell_detail: dict[str, Any] = {}
        specialty_score: float | None = None
        specialty_weight = 0.0
        specialty_label = ""
        # Only recorded healing rotations reserve the 40-point healer duty
        # pool. Output Yao and output Apothecary use ordinary DPS scoring.
        if role == "奶/辅助":
            healing_detail = self._healing_completion(
                snapshot,
                player,
                data_job,
                specialty_windows or output_windows,
            )
            healing = healing_detail.score
            healing_cast_completion = healing_detail.cast_completion
            effective_healing = healing_detail.effective_healing
            healing_effective_realization = healing_detail.effective_realization
            healing_rescue_completion = healing_detail.rescue_completion
            healing_rescue_detail = healing_detail.rescue_detail
            healing_damage_reduction_completion = (
                healing_detail.damage_reduction_completion
            )
            healing_pressure_windows = healing_detail.pressure_windows
            healing_meaningful_casts = healing_detail.meaningful_casts
            healing_total_casts = healing_detail.total_casts
            healing_evaluated_casts = healing_detail.evaluated_casts
            specialty_score = healing
            specialty_weight = 0.40
            specialty_label = "治疗完成率"
        elif role == "T/辅助":
            shell_theory_plan = theory_plan
            if not isinstance(shell_theory_plan, dict):
                # Reuse the already calculated fallback output axis too. Do not
                # run another planner solely to obtain a shell denominator.
                shell_theory_plan = {
                    "segments": [
                        {"skill": profile_by_key[row.key].name,
                         "start": row.seconds,
                         "end": (row.end_seconds if row.end_seconds > row.seconds
                                 else row.seconds + profile_by_key[row.key].cast_time)}
                        for row in theory_casts if row.key in profile_by_key
                    ],
                    "_score_glyphs": score_glyph_rows or (),
                }
            tank, tank_attack_down, tank_taunt, tank_shell = self._tank_completion(
                snapshot,
                player,
                data_job,
                output_windows,
                boss_target_ids,
                attack_down_detail=attack_down_detail,
                taunt_detail=taunt_detail,
                shell_detail=shell_detail,
                theory_plan=shell_theory_plan,
            )
            specialty_score = tank
            specialty_weight = 0.40
            specialty_label = "T职责完成率"

        support_index = sum(
            row.theory_contribution
            for row in support_details
            if row.included_in_score
        )
        # Output professions are normalized only after every party member has
        # completed the same Boss-scoped counterfactual calculation.  Do not use
        # the static skill-panel index here: values such as 20% armor break or a
        # 26% aura describe different damage pools and are not comparable role
        # weights by themselves.
        support_weight = 0.0
        if role in {"奶/辅助", "T/辅助"}:
            # The final support/output split is assigned after every profession
            # has been evaluated on the same counterfactual benchmark. Until
            # then the full non-duty pool belongs to output.
            output_weight = 1.0 - specialty_weight
        else:
            output_weight = 1.0 - specialty_weight - support_weight
            role = "输出/辅助" if support_weight >= 0.20 else "输出"
        # A score of 100 is the theoretical baseline, not a hard ceiling. Better
        # execution or a stronger measured team contribution remains visible.
        support_score = max(0.0, contribution)
        if is_yao:
            yao_threshold_percent = _YAO_MEDITATION_ROLE_THRESHOLD * 100.0
            role_reason = (
                f"查克拉治愈术在所选Boss全队可输出区间的伤害覆盖率 "
                f"{yao_meditation_coverage * 100.0:.1f}%；"
                + (
                    f"达到{yao_threshold_percent:.0f}%，按40%治疗职责、动态辅助占比和剩余输出占比评分"
                    if is_seated_yao
                    else f"低于{yao_threshold_percent:.0f}%，按普通输出职业规则评分；只由输出分和动态辅助分组成"
                )
            )
        elif is_alchemist:
            role_reason = (
                "本场识别到本人施放牺牲，按40%治疗职责、动态辅助占比和剩余输出占比评分"
                if alchemist_variant == "辅助药剂"
                else "本场未识别到本人施放牺牲，按普通输出职业规则评分；只由输出分和动态辅助分组成"
            )
        else:
            role_reason = role_reason_for_job(job)
        if (
            not is_yao and not is_alchemist
            and job not in HEALER_JOBS | TANK_JOBS | HYBRID_SUPPORT_JOBS
        ):
            role_reason = (
                f"等待本场真实反事实收益归一化；"
                f"当前队伍反事实理论收益 {support_index:.1f}%"
            )
        output_contribution, support_contribution = (
            _overflow_balanced_score_contributions(
                output_score,
                support_score,
                output_weight,
                support_weight,
            )
        )
        specialty_contribution = (
            _clamp_score(specialty_score or 0.0) * specialty_weight
        )
        gross_total = (
            output_contribution
            + support_contribution
            + specialty_contribution
        )
        survival_detail = _survival_discipline_detail(
            snapshot,
            player.source_id,
            specialty_windows or output_windows,
            role,
            standard_hp=as_number(
                (
                    theory_plan.get("summary", {})
                    if isinstance(theory_plan, dict)
                    and isinstance(theory_plan.get("summary"), dict)
                    else {}
                ).get("HP")
            ),
            actual_casts=scored_actual_casts,
            theory_casts=scored_theory_casts,
            death_windows=(
                death_windows or specialty_windows or output_windows
            ),
        )
        survival_detail = _apply_survival_penalty_cap(
            survival_detail,
            gross_total,
            role,
            canonical_incoming_damage=(
                survival_detail.incoming_damage
                if not survival_detail.hp_shadow.available
                else None
            ),
        )
        total = max(
            0.0,
            gross_total - survival_detail.penalty,
        )

        recognized_skills = {
            canonical_skill(skill.name)
            for skill in player.skills
            if canonical_skill(skill.name) in profile_by_key
        }
        damaging_skills = {
            canonical_skill(skill.name) for skill in player.skills if skill.damage > 0
        }
        damage_name_ratio = (
            len(recognized_skills) / len(damaging_skills) if damaging_skills else 1.0
        )
        confidence = _clamp_score(
            (direct_cast_ratio * 0.65 + damage_name_ratio * 0.35) * 100.0
        )
        notes: list[str] = []
        if healing_rescue_detail and (not healing_rescue_detail.get("episode_count")
                                     or healing_rescue_detail.get("evaluated_players", 0)
                                     < healing_rescue_detail.get("party_size", 0)):
            notes.append(f"救急评价：{healing_rescue_detail['evidence']}")
        if role == "T/辅助" and tank_shell is None:
            notes.append(f"蛋壳完成率暂不评价：{shell_detail.get('basis', '理论轴缺失')}；该分项按中性处理")
        level_report = self._combat_level_report(snapshot, player.source_id)
        level_display = skill_level_display_rows(level_report)
        if level_report:
            rows = level_report['skills']
            confirmed = sum(row['status'] == 'confirmed_combat_row' for row in rows)
            unseen = sum(row['status'] == 'unused_theory_excluded' for row in rows)
            unknown = sum(row['status'] in {'used_level_unknown', 'conflicting_combat_levels'} for row in rows)
            notes.append(f'战斗等级：已确认{confirmed}个，未使用不排轴{unseen}个，等级未确认沿用基准{unknown}个')
        if confidence < 70:
            notes.append("部分施法由伤害命中反推，评分可信度偏低")
        if not profiles:
            notes.append("未找到该职业的默认加点伤害技能")
        if not burst_windows:
            notes.append("本场未识别爆发/加速窗口，输出分直接按标准化输出完成率计算")
        unmatched_skills = tuple(
            sorted(
                {
                    norm(skill.name)
                    for skill in player.skills
                    if skill.damage > 0
                    and canonical_skill(skill.name) not in profile_by_key
                }
            )
        )
        confidence_detail = ConfidenceDetail(
            direct_skill_count=direct_skill_count,
            observed_skill_count=observed_skill_count,
            direct_cast_count=direct_cast_count,
            inferred_cast_count=inferred_cast_count,
            recognized_skill_count=len(recognized_skills),
            damaging_skill_count=len(damaging_skills),
            direct_ratio=direct_cast_ratio,
            skill_name_ratio=damage_name_ratio,
            unmatched_skills=unmatched_skills,
        )

        skill_details = tuple(
            sorted(
                (
                    SkillScoreDetail(
                        skill_name=profile_by_key[key].name,
                        actual_count=actual_counts[key],
                        theory_count=theory_counts[key],
                        actual_power=(
                            actual_power_by_skill[key]
                        ),
                        theory_power=(
                            theoretical_by_skill[key]
                        ),
                        completion=_ratio_score(
                            actual_power_by_skill[key],
                            theoretical_by_skill[key],
                            neutral=100.0,
                        ),
                        high_value=(
                            key in high_value_keys
                            or key in preplanned_excluded_high_value_keys
                        ),
                        actual_duration=actual_duration_by_skill[key],
                        expected_duration=expected_duration_by_skill[key],
                        duration_utilization=(
                            actual_duration_by_skill[key]
                            / expected_duration_by_skill[key]
                            if expected_duration_by_skill[key] > 0
                            else 1.0
                        ),
                        proportional_output=(key in proportional_keys),
                        usage_rate=(
                            actual_counts[key] / theory_counts[key]
                            if theory_counts[key] > 0
                            else None
                        ),
                        excluded_from_score=(key in excluded_score_keys),
                        **level_display.get(key, {}),
                    )
                    for key in set(actual_counts) | set(theory_counts)
                    if key in profile_by_key
                ),
                key=lambda row: (
                    not row.high_value,
                    -row.theory_power,
                    row.skill_name,
                ),
            )
        )
        actual_timeline = tuple(
            ScoreTimelineCast(
                seconds=row.seconds,
                end_seconds=row.seconds + row.utilized_duration,
                skill_name=profile_by_key[row.key].name,
                power=(0.0 if row.key in excluded_score_keys else row.power),
                source=(
                    f"{row.source}·评分豁免不计分"
                    if row.key in excluded_score_keys
                    else row.source
                ),
                high_value=row.key in high_value_keys,
                in_burst_window=_in_intervals(row.seconds, burst_windows),
                expected_duration=row.expected_duration,
                utilized_duration=row.utilized_duration,
                utilization_ratio=row.utilization_ratio,
                proportional_output=row.proportional_output,
                action_speed_percent=row.action_speed_percent,
                action_speed_output_multiplier=row.action_speed_output_multiplier,
            )
            for row in actual_casts
        )
        actual_context_timeline = self._actual_prep_context_timeline(
            snapshot,
            player.source_id,
            profile_by_key,
            output_windows,
            burst_windows,
            high_value_keys,
        )
        scored_timeline_keys = {
            (round(row.seconds, 3), canonical_skill(row.skill_name))
            for row in actual_timeline
        }
        actual_context_timeline = tuple(
            row
            for row in actual_context_timeline
            if (round(row.seconds, 3), canonical_skill(row.skill_name))
            not in scored_timeline_keys
        )
        actual_timeline = tuple(
            sorted(
                (*actual_context_timeline, *actual_timeline),
                key=lambda row: (row.seconds, row.skill_name),
            )
        )
        theory_timeline = tuple(
            ScoreTimelineCast(
                seconds=row.seconds,
                end_seconds=(
                    row.end_seconds
                    if row.end_seconds > row.seconds
                    else row.seconds + profile_by_key[row.key].cast_time
                ),
                skill_name=profile_by_key[row.key].name,
                power=row.power,
                source="长时间实战模拟轴" if theory_plan else "理论最优轴",
                high_value=row.key in high_value_keys,
                in_burst_window=_in_intervals(row.seconds, theory_burst_windows),
                expected_duration=(
                    row.expected_duration
                    if row.expected_duration > 0
                    else (
                        max(0.0, row.end_seconds - row.seconds)
                        if row.key in persistent_summon_keys
                        else summon_limits.get(
                            row.key, (0, profile_by_key[row.key].cast_time)
                        )[1]
                    )
                ),
                utilized_duration=(
                    row.utilized_duration
                    if row.utilized_duration > 0
                    else max(
                        0.0,
                        (
                            row.end_seconds
                            if row.end_seconds > row.seconds
                            else row.seconds + profile_by_key[row.key].cast_time
                        )
                        - row.seconds,
                    )
                ),
                utilization_ratio=(
                    min(1.0, row.utilized_duration / row.expected_duration)
                    if row.expected_duration > 0
                    else 1.0
                ),
                proportional_output=(
                    row.proportional_output or row.key in proportional_keys
                ),
                action_speed_percent=row.action_speed_percent,
                action_speed_output_multiplier=row.action_speed_output_multiplier,
            )
            for row in scored_theory_casts
            if row.key in profile_by_key
        )
        from .circle_swing_forms import label_score_axes
        actual_timeline,theory_timeline=label_score_axes(actual_timeline,theory_timeline,snapshot.skill_cast_events,player.source_id,theory_plan,player.job)

        theory_context_timeline = self._planned_theory_context_timeline(
            theory_plan,
            output_windows,
            theory_burst_windows,
            theory_timeline,
        )
        theory_timeline = tuple(
            sorted(
                (*theory_timeline, *theory_context_timeline),
                key=lambda row: (row.seconds, row.end_seconds, row.skill_name),
            )
        )
        theory_rows_by_label: dict[str, list[ScoreWindowDetail]] = defaultdict(list)
        for row in theory_burst_window_rows:
            theory_rows_by_label[canonical_skill(row.label)].append(row)
        burst_window_details_list: list[ScoreWindowDetail] = []
        for row in burst_window_rows:
            matching_theory_rows = theory_rows_by_label.get(
                canonical_skill(row.label),
                [],
            )
            if isinstance(theory_plan,dict) and theory_plan.get('_score_v2'):
                from .score_burst import matching_window
                theory_row=matching_window(row,matching_theory_rows)
            else:
                theory_row = matching_theory_rows.pop(0) if matching_theory_rows else row
            actual_power = window_power(burst_actual_parts, ((row.start_seconds, row.end_seconds),))
            theory_power = window_power(burst_theory_parts, ((theory_row.start_seconds, theory_row.end_seconds),))
            burst_window_details_list.append(ScoreWindowDetail(
                label=row.label,
                start_seconds=row.start_seconds,
                end_seconds=row.end_seconds,
                original_duration=getattr(row,'original_duration',0.),
                actual_power=actual_power,
                theory_power=theory_power,
                completion=_ratio_score(
                    actual_power,
                    theory_power,
                    neutral=100.0,
                    allow_overflow=bool(isinstance(theory_plan,dict) and theory_plan.get('_score_v2')),
                ),
            ))
        burst_window_details = tuple(burst_window_details_list)
        glyphs = (
            self._glyph_display_rows(score_glyph_rows)
            if isinstance(score_glyph_rows, (list, tuple)) and score_glyph_rows
            else tuple(
                f"{row.name}·{row.glyph_effect}+{row.glyph_value:g}%"
                for row in profiles
                if row.glyph_effect
            )
        )
        return PlayerScore(
            source_id=player.source_id,
            name=player.name,
            job=job,
            rotation_job=(yao_variant if is_yao else alchemist_variant if is_alchemist else ""),
            role=role,
            role_reason=role_reason,
            grade=grade_for_score(total),
            total_score=total,
            hit_realization=v2_detail,
            output_score=output_score,
            support_score=support_score,
            specialty_score=specialty_score,
            output_weight=output_weight,
            support_weight=support_weight,
            specialty_weight=specialty_weight,
            specialty_label=specialty_label,
            support_index=support_index,
            theory_damage_completion=theory_damage_completion,
            timing_efficiency=timing_efficiency,
            high_value_usage=high_value_usage,
            burst_usage=burst_usage,
            coverage_completion=coverage,
            contribution_completion=contribution,
            healing_completion=healing,
            tank_completion=tank,
            healing_cast_completion=healing_cast_completion,
            effective_healing=effective_healing,
            healing_effective_realization=healing_effective_realization,
            healing_rescue_completion=healing_rescue_completion,
            healing_damage_reduction_completion=(
                healing_damage_reduction_completion
            ),
            healing_pressure_windows=healing_pressure_windows,
            healing_meaningful_casts=healing_meaningful_casts,
            healing_total_casts=healing_total_casts,
            healing_evaluated_casts=healing_evaluated_casts,
            healing_rescue_detail=healing_rescue_detail,
            tank_attack_down_completion=tank_attack_down,
            tank_attack_down_coverage=attack_down_detail.get("actual"),
            tank_attack_down_theory_coverage=attack_down_detail.get("theory"),
            tank_attack_down_evidence=attack_down_detail.get("basis", ""),
            tank_taunt_completion=tank_taunt,
            tank_shell_completion=tank_shell,
            tank_shell_coverage=shell_detail.get("actual"),
            tank_shell_party_size=shell_detail.get("party_size"),
            tank_shell_theory_coverage=shell_detail.get("theory"),
            tank_shell_theory_casts=shell_detail.get("casts"),
            tank_shell_evidence=shell_detail.get("basis", ""),
            tank_taunt_coverage=taunt_detail.get("actual"),
            tank_taunt_theory_coverage=taunt_detail.get("theory"),
            tank_taunt_evidence=taunt_detail.get("basis", ""),
            confidence=confidence,
            actual_standard_power=actual_standard_power,
            theory_standard_power=theory_standard_power,
            glyphs=glyphs,
            notes=tuple(notes),
            output_windows=output_windows,
            actual_timeline=actual_timeline,
            castability_windows=(
                self.castability_by_source(snapshot)[player.source_id].windows
                if player.source_id in self.castability_by_source(snapshot) else ()
            ),
            theory_timeline=theory_timeline,
            skill_details=skill_details,
            burst_windows=burst_window_details,
            support_details=support_details,
            confidence_detail=confidence_detail,
            output_contribution=output_contribution,
            support_contribution=support_contribution,
            specialty_contribution=specialty_contribution,
            survival_penalty=survival_detail.penalty,
            survival_detail=survival_detail,
        )


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreEngineMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
