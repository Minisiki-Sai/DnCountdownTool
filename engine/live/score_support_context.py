from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for

class ScoreSupportContextMixin:
    @staticmethod
    def _counterfactual_support_weight(
        support_index: float,
        lower: float,
        upper: float,
    ) -> float:
        """Map team gain proportionally to the strongest profession's 50%."""
        value = max(0.0, as_number(support_index))
        # ``lower`` remains in the signature for callers that also display the
        # all-profession range. It must not be subtracted from ``value``: doing
        # so makes the weakest non-zero profession worth 0% and distorts every
        # ratio between professions.
        _ = lower
        ceiling = max(0.0, as_number(upper))
        if ceiling <= 1e-9:
            return 0.0
        return max(0.0, min(0.50, value / ceiling * 0.50))
    @staticmethod
    def _support_benchmark_context(
        context: _SupportCounterfactualContext,
        bucket_ms: int = 2000,
    ) -> _SupportCounterfactualContext:
        """Compress only all-job calibration packets into damage-weighted bins."""
        if bucket_ms <= 1 or len(context.team_damage_events) < 1000:
            return context
        has_baseline = (
            len(context.baseline_damage_by_event)
            == len(context.team_damage_events)
        )
        grouped: dict[tuple[int, str, str], list[Any]] = {}
        for index, event in enumerate(context.team_damage_events):
            amount = max(0.0, as_number(event.amount))
            if amount <= 1e-12:
                continue
            key = (
                int(event.timestamp_ms) // bucket_ms,
                str(event.source_id),
                norm(event.job),
            )
            row = grouped.get(key)
            if row is None:
                row = [event, 0.0, 0.0, 0.0]
                grouped[key] = row
            row[1] += amount
            row[2] += float(event.timestamp_ms) * amount
            row[3] += (
                max(0.0, as_number(context.baseline_damage_by_event[index]))
                if has_baseline
                else amount
            )

        events: list[CombatEvent] = []
        baselines: list[float] = []
        for _key, (template, amount, weighted_time, baseline) in sorted(
            grouped.items(),
            key=lambda item: item[0],
        ):
            events.append(replace(
                template,
                timestamp_ms=int(round(weighted_time / max(1e-12, amount))),
                amount=amount,
            ))
            baselines.append(baseline)
        return _SupportCounterfactualContext(
            team_damage_events=tuple(events),
            total_team_damage=context.total_team_damage,
            effects=context.effects,
            intervals_by_identity=context.intervals_by_identity,
            effective_intervals_by_identity=context.effective_intervals_by_identity,
            effective_state_by_identity=context.effective_state_by_identity,
            effective_reason_by_identity=context.effective_reason_by_identity,
            personal_attack_effects=context.personal_attack_effects,
            boss_hp_segments=context.boss_hp_segments,
            baseline_damage_by_event=tuple(baselines),
        )
    def _all_job_counterfactual_support_indexes(
        self,
        snapshot: CombatSnapshot,
        output_windows: tuple[tuple[float, float], ...],
        context: _SupportCounterfactualContext,
        *,
        allow_parallel: bool = True,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, float]:
        """Evaluate every profession as one ideal support provider in this team.

        This deliberately reuses the event-level counterfactual engine instead
        of generating another 29 complete damage rotations. Current team
        effects, recipients, damage composition, caps and overlap rules remain
        unchanged; only the candidate profession's effects are replaced by its
        ideal Boss-scoped schedule.
        """
        jobs = tuple(dict.fromkeys((
            *(
                data_job_for(job)
                for first_job in self.store.first_jobs()
                for job in self.store.second_jobs(first_job)
                if norm(job)
            ),
            "输出曜",
        )))
        if not jobs:
            return {}
        if (
            context.total_team_damage <= 1e-9
            or not context.team_damage_events
            or not output_windows
        ):
            return {job: 0.0 for job in jobs}

        benchmark_context = self._support_benchmark_context(context)
        end_seconds = max((end for _start, end in output_windows), default=0.0)
        modifier_options = tuple(
            self._combat_modifier_windows(snapshot, player.source_id)
            for player in event_platform_for(snapshot).players
        )
        modifier_windows = max(
            modifier_options,
            key=lambda rows: sum(
                max(0.0, end - start)
                * (max(0.0, 1.0 / max(0.01, cooldown) - 1.0)
                   + max(0.0, action - 1.0))
                for start, end, cooldown, action in rows
            ),
            default=(),
        )

        if allow_parallel:
            try:
                from ..timeline_parallel import calculate_support_benchmarks_parallel

                parallel_result = calculate_support_benchmarks_parallel(
                    jobs,
                    benchmark_context,
                    output_windows,
                    tuple(modifier_windows),
                    end_seconds,
                    self._score_boss_name,
                    {
                        "monster_defense": self._score_boss_defense,
                        "crit_resist": self._score_boss_crit_resist,
                        "element_resist": self._score_boss_element_resist,
                    },
                    self._score_crit_profiles_by_source,
                    progress_callback=progress_callback,
                    neutral_profiles_by_source=self._score_neutral_profiles_by_source,
                )
                if parallel_result is not None:
                    return parallel_result
            except Exception:
                # Exact sequential fallback below remains authoritative.
                pass

        result: dict[str, float] = {}
        for index, job in enumerate(jobs, start=1):
            result[job] = self._counterfactual_support_index_for_job(
                job,
                benchmark_context,
                output_windows,
                tuple(modifier_windows),
                end_seconds,
            )
            if progress_callback is not None:
                progress_callback(index, len(jobs), f"辅助基准：{job}")
        return result
    def _counterfactual_support_index_for_job(
        self,
        job: str,
        benchmark_context: _SupportCounterfactualContext,
        output_windows: tuple[tuple[float, float], ...],
        modifier_windows: tuple[tuple[float, float, float, float], ...],
        end_seconds: float,
    ) -> float:
        """Evaluate one profession with the exact all-job calibration rules."""

        benchmark_job = data_job_for(job)
        optional_support_skills = (
            _optional_support_effect_skills_for_job(benchmark_job)
        )
        # ``_support_expectations`` intentionally contains every team effect
        # that the profession *can* learn.  The wide catalog is required when
        # replay packets prove that a real player used a non-default skill, but
        # it must not turn those merely learnable skills into free value in the
        # all-profession benchmark.  Calibrate absent professions with the same
        # default build used by the theory planner; present players are finally
        # weighted from their confirmed scoring details below.
        default_learned_skills = frozenset(
            self._learned_skills(benchmark_job)
        )
        expectations = tuple(
            row
            for row in self._support_expectations(benchmark_job)
            if row.skill_key not in optional_support_skills
            and (
                row.kind != "buff"
                or not default_learned_skills
                or row.skill_key in default_learned_skills
            )
            and not (
                job == "输出曜"
                and row.skill_key == canonical_skill(_YAO_MEDITATION_SKILL)
            )
        )
        if not expectations:
            return 0.0

        candidate_source_id = f"__support_benchmark__:{job}"
        candidate_effects: list[_SupportEffectInstance] = []
        for expectation in expectations:
            intervals = (
                _merge_intervals(output_windows)
                if expectation.skill_key == _PALADIN_AURA_GROUP_KEY
                else self._theory_support_intervals(
                    expectation,
                    end_seconds,
                    output_windows,
                    modifier_windows,
                )
            )
            if not intervals:
                continue
            candidate_effects.append(_SupportEffectInstance(
                source_id=candidate_source_id,
                source_job=job,
                expectation=expectation,
                intervals=intervals,
            ))
        # A profession already present in the party is replaced, not added as
        # a duplicate ninth provider. Duplicate non-stackable effects therefore
        # keep the same score semantics in sequential and parallel execution.
        retained_effects = tuple(
            effect
            for effect in benchmark_context.effects
            if data_job_for(effect.source_job) != benchmark_job
        )
        retained_attack_priority = max(
            (
                _team_attack_buff_priority(effect.expectation.skill_key)
                for effect in retained_effects
                if (
                    effect.expectation.stack_group
                    or _team_attack_buff_exclusive_group(
                        effect.expectation.skill_key
                    )
                )
                == _TEAM_ATTACK_BUFF_EXCLUSIVE_GROUP
            ),
            default=0,
        )
        if retained_attack_priority > 0:
            # All-job calibration evaluates an ideal candidate against the
            # professions already in this party. A lower-priority member of a
            # permanent exclusive group has no theoretical value when the
            # party already owns its higher-priority replacement. Event-level
            # actual scoring still keeps the lower buff as a fallback during a
            # genuinely uncovered interval.
            candidate_effects = [
                effect
                for effect in candidate_effects
                if (
                    effect.expectation.stack_group
                    or _team_attack_buff_exclusive_group(
                        effect.expectation.skill_key
                    )
                )
                != _TEAM_ATTACK_BUFF_EXCLUSIVE_GROUP
                or _team_attack_buff_priority(effect.expectation.skill_key)
                >= retained_attack_priority
            ]
        if not candidate_effects:
            return 0.0

        candidate_context = _SupportCounterfactualContext(
            team_damage_events=benchmark_context.team_damage_events,
            total_team_damage=benchmark_context.total_team_damage,
            effects=retained_effects + tuple(candidate_effects),
            intervals_by_identity={},
            personal_attack_effects=benchmark_context.personal_attack_effects,
            boss_hp_segments=benchmark_context.boss_hp_segments,
            baseline_damage_by_event=benchmark_context.baseline_damage_by_event,
        )
        source_contributions, _detail_contributions = (
            self._counterfactual_support_contributions(candidate_context)
        )
        return max(
            0.0,
            as_number(source_contributions.get(candidate_source_id)),
        )
    def _apply_counterfactual_support_weights(
        self,
        scores: Iterable[PlayerScore],
        benchmark_indexes: dict[str, float],
    ) -> tuple[PlayerScore, ...]:
        """Recompose output-role scores against the all-profession benchmark.

        Every scored player is compared with all professions evaluated against
        the same Boss scope, team damage events and support caps.  The player's
        numerator is the sum of the theoretical contributions that are actually
        present in ``support_details`` and marked ``included_in_score``.  This
        keeps EX/base fallback effects, optional skills and merely learnable
        skills from influencing the role split while being absent from the
        visible scoring detail.  The strongest all-profession candidate maps to
        50%; every player receives the same direct proportion of 50%. Healers
        and tanks reserve 40% for duty, then use that absolute support
        percentage and assign the rest of their 60% pool to output.
        """
        rows = tuple(scores)
        normalized_benchmarks = {
            norm(job): max(0.0, as_number(value))
            for job, value in benchmark_indexes.items()
        }
        if not normalized_benchmarks:
            return rows

        scored_indexes = tuple(
            max(0.0, as_number(row.support_index)) for row in rows
        )
        indexes = tuple(normalized_benchmarks.values()) + scored_indexes
        lower = min(indexes)
        upper = max(indexes)
        normalized: list[PlayerScore] = []
        for row in rows:
            benchmark_job = (
                "输出曜"
                if row.rotation_job == "输出曜"
                else data_job_for(row.job)
            )
            profession_template_index = normalized_benchmarks.get(
                benchmark_job,
                0.0,
            )
            if row.rotation_job == "输出药剂":
                # The generic Apothecary benchmark assumes Sacrifice. It is
                # not the output variant's default support template and must
                # not be presented as such in this player's explanation.
                profession_template_index = max(0.0, as_number(row.support_index))
            # The weight numerator and the visible support detail must share
            # one source of truth.  Previously this used the profession's full
            # learnable template (for example 雷神 also received 无情打击 and
            # 光明之呼吸) even when those rows were absent from the player's
            # score.  That produced a 23% role share from less than 10% of
            # displayed theoretical benefit.
            profession_index = max(0.0, as_number(row.support_index))
            base_support_weight = self._counterfactual_support_weight(
                profession_index,
                lower,
                upper,
            )
            available_weight = max(0.0, 1.0 - row.specialty_weight)
            if row.role in {"奶/辅助", "T/辅助"}:
                support_weight = min(available_weight, base_support_weight)
                output_weight = available_weight - support_weight
                role = row.role
            else:
                support_weight = available_weight * base_support_weight
                output_weight = available_weight * (1.0 - base_support_weight)
                role = (
                    "输出/辅助"
                    if support_weight >= 0.20 and row.support_details
                    else "输出"
                )
            output_contribution, support_contribution = (
                _overflow_balanced_score_contributions(
                    row.output_score,
                    row.support_score,
                    output_weight,
                    support_weight,
                )
            )
            specialty_contribution = (
                _clamp_score(row.specialty_score or 0.0) * row.specialty_weight
            )
            gross_total = (
                output_contribution
                + support_contribution
                + specialty_contribution
            )
            survival_detail = _apply_survival_penalty_cap(
                row.survival_detail,
                gross_total,
                role,
                canonical_incoming_damage=row.survival_detail.incoming_damage,
            )
            total = max(0.0, gross_total - survival_detail.penalty)
            base_reason = row.role_reason if row.role in {"奶/辅助", "T/辅助"} or data_job_for(row.job) == "曜" else (
                HYBRID_SUPPORT_REASONS.get(row.job, "")
            )
            result_reason = (
                f"本角色计分明细理论收益 {profession_index:.2f}%；"
                f"全职业最高理论收益 {upper:.2f}%，"
                f"同比例辅助权重 {support_weight * 100:.1f}%"
            )
            if abs(profession_template_index - profession_index) > 0.01:
                result_reason += (
                    f"；职业默认模板 {profession_template_index:.2f}%"
                    "仅用于全职业上限校准"
                )
            normalized.append(replace(
                row,
                role=role,
                role_reason=(f"{base_reason}；{result_reason}" if base_reason else result_reason),
                grade=grade_for_score(total),
                total_score=total,
                output_weight=output_weight,
                support_weight=support_weight,
                output_contribution=output_contribution,
                support_contribution=support_contribution,
                specialty_contribution=specialty_contribution,
                survival_penalty=survival_detail.penalty,
                survival_detail=survival_detail,
            ))
        return tuple(normalized)


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreSupportContextMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
