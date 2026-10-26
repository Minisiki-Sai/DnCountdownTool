from __future__ import annotations
from ..background_priority import score_checkpoint

from .boss_recognizer import BossRecognizer
from .effects import resistance_application_mode
from .event_platform import event_platform_for
from .aura_state import is_aura_damage_series, is_inferred_aura_row


def _hit_assumed_resistance_intervals(
    snapshot: CombatSnapshot,
    source_id: str,
    skill_key: str,
    duration: float,
    end_seconds: float,
    boss_target_ids: frozenset[str] | None,
    effect_key: str = "",
) -> tuple[tuple[float, float], ...]:
    """Score a resistance application only after its own real Boss hit.

    This is a scoring assumption, not a rewrite of packet-observed statuses:
    each matching hit refreshes the full per-application duration even when
    the target omitted/rejected a status packet. Non-damaging debuffs continue
    to use their recorded status events.
    """
    if duration <= 0 or resistance_application_mode(skill_key, effect_key) == "cast":
        return ()
    intervals = []
    for hit in event_platform_for(snapshot).event_index.combat_by_source.get(source_id, ()):
        if (
            hit.healing
            or hit.amount <= 0
            or hit.has_real_hit_evidence is not True
            or canonical_skill(hit.skill_name) != skill_key
        ):
            continue
        if boss_target_ids:
            if hit.target_id not in boss_target_ids:
                continue
        elif hit.target_job.strip().partition(" ")[0].casefold() != "boss":
            continue
        at = hit.timestamp_ms / 1000.0
        if 0 <= at < end_seconds:
            intervals.append((at, min(end_seconds, at + duration)))
    return _merge_intervals(intervals)


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


class ScoreSupportCounterfactualMixin:
    def _build_support_counterfactual_context(
        self,
        snapshot: CombatSnapshot,
        output_windows: tuple[tuple[float, float], ...],
        boss_target_ids: frozenset[str] | None = None,
        theory_plans: dict[str, dict[str, Any]] | None = None,
    ) -> _SupportCounterfactualContext:
        from .support_boss_scope import from_plans
        platform = event_platform_for(snapshot)
        end_seconds = max(
            platform.elapsed_seconds,
            platform.dps_elapsed_seconds,
            platform.boss_effect_current_seconds,
        )
        player_ids = {row.source_id for row in platform.players}
        event_index = platform.event_index
        # Weapon-type Bosses can have an ordinary "Lv70" packet job despite
        # being confirmed encounters. Reuse the recognizer's runtime IDs when
        # the score covers all Bosses instead of relying on that text label.
        resistance_target_ids = boss_target_ids
        if resistance_target_ids is None:
            recognized = frozenset(
                target_id
                for encounter in BossRecognizer().recognize_snapshot(snapshot)
                for target_id in encounter.target_ids
            )
            resistance_target_ids = recognized or None
        team_damage_events = tuple(
            event
            for event in event_index.damage_events
            if (not player_ids or event.source_id in player_ids)
            and (not boss_target_ids or event.target_id in boss_target_ids)
            and _in_intervals(event.timestamp_ms / 1000.0, output_windows)
        )

        raw_debuff: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for event in platform.boss_effect_events:
            if boss_target_ids and event.target_id not in boss_target_ids:
                continue
            raw_debuff[
                (
                    str(event.source_id),
                    canonical_skill(event.skill_name),
                    event.key,
                )
            ].append(event)
        summary_debuff: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for effect in platform.boss_effects:
            if boss_target_ids and effect.target_id not in boss_target_ids:
                continue
            summary_debuff[
                (
                    str(effect.source_id),
                    canonical_skill(effect.skill_name),
                    effect.key,
                )
            ].append(effect)
        buff_series: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in platform.player_buff_series:
            if is_inferred_aura_row(row):
                continue
            buff_series[
                (str(row.source_id), canonical_skill(row.skill_name))
            ].append(row)
        buff_summary: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in platform.player_buffs:
            if is_inferred_aura_row(row):
                continue
            buff_summary[
                (str(row.source_id), canonical_skill(row.skill_name))
            ].append(row)

        intervals_by_identity: dict[
            tuple[str, str, str, str], tuple[tuple[float, float], ...]
        ] = {}
        hit_assumed_by_identity: set[tuple[str, str, str, str]] = set()
        effects: list[_SupportEffectInstance] = []
        party_ids = tuple(
            dict.fromkeys(str(row.source_id) for row in platform.players if row.source_id)
        )

        def recipient_interval_rows(
            rows: Iterable[Any],
            *,
            source_id: str,
            skill_key: str,
            duration: float,
            expected_magnitude: float,
            summary: bool = False,
        ) -> dict[str, tuple[tuple[float, float], ...]]:
            selected = _primary_player_buff_effect_rows(
                rows, expected_magnitude
            )
            by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for row in selected:
                target_id = str(getattr(row, "target_id", ""))
                # A reliable recipient status row remains usable even when the
                # roster packet for that player is absent. This occurs in
                # partial/reconnected captures; discarding it would turn a
                # known recipient into an invented all-party Buff.
                if not target_id:
                    continue
                raw_intervals = getattr(row, "intervals", ())
                if summary and not raw_intervals:
                    raw_intervals = ((row.start_seconds, row.end_seconds),)
                by_target[target_id].extend(raw_intervals)

            bounded: dict[str, tuple[tuple[float, float], ...]] = {}
            for target_id, target_intervals in by_target.items():
                bounded_intervals = _bounded_recorded_team_buff_intervals(
                    snapshot,
                    source_id,
                    skill_key,
                    duration,
                    target_intervals,
                    end_seconds,
                    target_id=target_id,
                )
                if bounded_intervals:
                    bounded[target_id] = bounded_intervals
            return bounded

        for provider in platform.players:
            source_id = str(provider.source_id)
            for base_expectation in self._combat_support_expectations(snapshot, source_id, provider.job):
                expectation = self._resolved_paladin_aura_expectation(
                    snapshot,
                    source_id,
                    provider.job,
                    base_expectation,
                )
                identity = self._support_effect_identity(source_id, expectation)
                recipient_scoped = expectation.kind != "debuff"
                recipient_intervals: dict[
                    str, tuple[tuple[float, float], ...]
                ] = {}
                if expectation.kind == "debuff":
                    raw_rows = raw_debuff.get(
                        (source_id, expectation.skill_key, expectation.effect_key),
                        (),
                    )
                    if raw_rows:
                        intervals = _clip_intervals(
                            _source_boss_effect_intervals(
                                raw_rows, source_id, end_seconds
                            ),
                            end_seconds,
                        )
                    else:
                        summary_rows = summary_debuff.get(
                            (
                                source_id,
                                expectation.skill_key,
                                expectation.effect_key,
                            ),
                            (),
                        )
                        intervals = _clip_intervals(
                            (
                                interval
                                for row in summary_rows
                                for interval in (
                                    row.intervals
                                    or ((row.start_seconds, row.end_seconds),)
                                )
                            ),
                            end_seconds,
                        )
                    if expectation.effect_key == "冰冻层":
                        intervals = _merge_intervals(
                            (
                                *intervals,
                                *self._confirmed_freeze_cast_intervals(
                                    snapshot,
                                    source_id,
                                    expectation,
                                    end_seconds,
                                    raw_rows,
                                    boss_target_ids,
                                ),
                            )
                        )
                    elif expectation.effect_key in _ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS:
                        hit_intervals = _hit_assumed_resistance_intervals(
                            snapshot, source_id, expectation.skill_key,
                            expectation.duration, end_seconds,
                            resistance_target_ids, expectation.effect_key,
                        )
                        if hit_intervals:
                            hit_assumed_by_identity.add(identity)
                        intervals = _merge_intervals((
                            *intervals,
                            *hit_intervals,
                        ))
                elif expectation.skill_key == _PALADIN_AURA_GROUP_KEY:
                    aura_rows = [
                        row
                        for skill_key in _PALADIN_AURA_SKILLS
                        for row in buff_series.get((source_id, skill_key), ())
                        if is_aura_damage_series(row)
                    ]
                    by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
                    for row in aura_rows:
                        target_id = str(getattr(row, "target_id", ""))
                        if target_id and (not party_ids or target_id in party_ids):
                            by_target[target_id].extend(row.intervals)
                    recipient_intervals = {
                        target_id: clipped
                        for target_id, rows in by_target.items()
                        if (clipped := _clip_intervals(rows, end_seconds))
                    }
                    intervals = _merge_intervals(
                        interval
                        for rows in recipient_intervals.values()
                        for interval in rows
                    )
                else:
                    source_series = buff_series.get(
                        (source_id, expectation.skill_key), ()
                    )
                    if expectation.skill_key in {"火焰光环", "寒冰光环"}:
                        source_series = tuple(row for row in source_series if is_aura_damage_series(row))
                    if source_series:
                        recipient_intervals = recipient_interval_rows(
                            source_series,
                            source_id=source_id,
                            skill_key=expectation.skill_key,
                            duration=expectation.duration,
                            expected_magnitude=expectation.magnitude,
                        )
                    else:
                        recipient_intervals = recipient_interval_rows(
                            tuple(
                                row for row in buff_summary.get((source_id, expectation.skill_key), ())
                                if expectation.skill_key not in {"火焰光环", "寒冰光环"} or is_aura_damage_series(row)
                            ),
                            source_id=source_id,
                            skill_key=expectation.skill_key,
                            duration=expectation.duration,
                            expected_magnitude=expectation.magnitude,
                            summary=True,
                        )

                    self_reported_aura_keys = {
                        canonical_skill(name)
                        for name in _HEALER_SELF_REPORTED_TEAM_AURAS.get(
                            data_job_for(provider.job), frozenset()
                        )
                    }
                    if expectation.skill_key in self_reported_aura_keys:
                        carrier_intervals = recipient_intervals.get(source_id, ())
                        if (
                            expectation.skill_key
                            in _PERSISTENT_TEAM_AURA_SKILL_KEYS
                        ):
                            carrier_intervals = _merge_intervals(
                                interval
                                for rows in recipient_intervals.values()
                                for interval in rows
                            )
                        if carrier_intervals:
                            recipient_intervals = {
                                target_id: carrier_intervals
                                for target_id in (party_ids or (source_id,))
                            }
                    intervals = _merge_intervals(
                        interval
                        for rows in recipient_intervals.values()
                        for interval in rows
                    )
                intervals_by_identity[identity] = intervals
                effects.append(
                    _SupportEffectInstance(
                        source_id=source_id,
                        source_job=provider.job,
                        expectation=expectation,
                        intervals=intervals,
                        recipient_scoped=recipient_scoped,
                        recipient_intervals=tuple(
                            sorted(recipient_intervals.items())
                        ),
                    )
                )

        team_attack_skill_keys = {
            instance.expectation.skill_key
            for instance in effects
            if instance.expectation.effect_key in {"双攻%", "物攻%", "魔攻%"}
        }
        personal_attack_intervals: dict[
            tuple[str, str, float, float], list[tuple[float, float]]
        ] = defaultdict(list)
        personal_attack_catalog = _personal_attack_display_data()
        for row in platform.player_buff_series:
            target_id = str(row.target_id)
            if not target_id or str(row.source_id) != target_id:
                continue
            skill_key = canonical_skill(row.skill_name)
            values = personal_attack_catalog.get(skill_key)
            if values is None or skill_key in team_attack_skill_keys:
                continue
            physical, magical = values
            personal_attack_intervals[
                (target_id, skill_key, physical, magical)
            ].extend(row.intervals)
        personal_attack_effects = tuple(
            _PersonalAttackEffectInstance(
                target_id=target_id,
                skill_key=skill_key,
                physical_percent=physical,
                magical_percent=magical,
                intervals=_clip_intervals(intervals, end_seconds),
            )
            for (
                target_id,
                skill_key,
                physical,
                magical,
            ), intervals in personal_attack_intervals.items()
        )

        # Resolve packet-observed Debuffs into client-effective axes while
        # retaining the raw intervals for evidence and counterfactual fallback.
        effective_intervals_by_identity = dict(intervals_by_identity)
        effective_state_by_identity: dict[
            tuple[str, str, str, str], str
        ] = {}
        effective_reason_by_identity: dict[
            tuple[str, str, str, str], str
        ] = {}
        interval_inputs: list[EffectInput] = []
        interval_owner: dict[str, tuple[str, str, str, str]] = {}
        for effect_index, instance in enumerate(effects):
            expectation = instance.expectation
            if expectation.kind != "debuff" or expectation.effect_key == "冰冻层":
                continue
            identity = self._support_effect_identity(
                instance.source_id, expectation
            )
            # Real-hit evidence changes admission, not stacking. Only the
            # client's exclusive-admission lane needs the scoring override.
            # Applying this marker to Holy Ground erased its independent
            # stack group and suppressed its displayed coverage, while the
            # contribution allocator correctly continued counting its gain.
            override_admission = (
                identity in hit_assumed_by_identity
                and effect_rule(EffectInput(
                    key=expectation.effect_key,
                    value=expectation.magnitude,
                    skill_name=expectation.skill_name,
                    stack_group=expectation.stack_group,
                    stackable=expectation.stackable,
                )).group_mode == "exclusive_admission"
            )
            for interval_index, (start, end) in enumerate(instance.intervals):
                instance_id = f"{effect_index}:{interval_index}"
                interval_owner[instance_id] = identity
                interval_inputs.append(
                    EffectInput(
                        key=expectation.effect_key,
                        value=expectation.magnitude,
                        skill_name=expectation.skill_name,
                        source_id=instance.source_id,
                        target_id="boss",
                        instance_id=instance_id,
                        start=float(start),
                        end=float(end),
                        stack_group=(
                            "theory:hit-assumed-resist"
                            if override_admission
                            else expectation.stack_group
                        ),
                        stackable=expectation.stackable,
                        group_mode=(
                            "max" if override_admission else ""
                        ),
                    )
                )

        interval_resolution = EFFECT_STACKING_ENGINE.resolve_intervals(
            interval_inputs,
            end_seconds=end_seconds,
            include_tied_max=True,
        )
        effective_rows: dict[
            tuple[str, str, str, str], list[tuple[float, float]]
        ] = defaultdict(list)
        rejected_by_identity: dict[
            tuple[str, str, str, str], list[str]
        ] = defaultdict(list)
        suppressed_by_identity: dict[
            tuple[str, str, str, str], list[str]
        ] = defaultdict(list)
        for instance_id, intervals in interval_resolution.effective_intervals.items():
            identity = interval_owner.get(instance_id)
            if identity is not None:
                effective_rows[identity].extend(intervals)
        for instance_id, reason in interval_resolution.rejected_reasons.items():
            identity = interval_owner.get(instance_id)
            if identity is not None:
                rejected_by_identity[identity].append(reason)
        for instance_id, reason in interval_resolution.suppressed_reasons.items():
            identity = interval_owner.get(instance_id)
            if identity is not None:
                suppressed_by_identity[identity].append(reason)

        for instance in effects:
            expectation = instance.expectation
            if expectation.kind != "debuff" or expectation.effect_key == "冰冻层":
                continue
            identity = self._support_effect_identity(
                instance.source_id, expectation
            )
            observed = intervals_by_identity.get(identity, ())
            effective = _merge_intervals(effective_rows.get(identity, ()))
            effective_intervals_by_identity[identity] = effective
            observed_seconds = sum(max(0.0, end - start) for start, end in observed)
            effective_seconds = sum(max(0.0, end - start) for start, end in effective)
            if not observed:
                state = "未观察到"
                reason = "抓包中没有该状态申请"
            elif effective_seconds <= 1e-9:
                if rejected_by_identity.get(identity):
                    state = "申请被拒绝"
                    reason = rejected_by_identity[identity][0]
                else:
                    state = "同组受压制"
                    reason = suppressed_by_identity.get(
                        identity, ["同组存在更高有效值"]
                    )[0]
            elif effective_seconds + 1e-9 < observed_seconds:
                state = "部分生效"
                reason = "仅未被同组更高值压制的区间计入覆盖率和评分"
            else:
                state = "有效"
                reason = "包内状态在对应区间实际参与结算"
            if identity in hit_assumed_by_identity and effective_seconds > 1e-9:
                state = "命中假定"
                reason = (
                    "按同技能真实Boss Hit施加并逐Hit刷新减抗；"
                    "不将包内同组申请失败作为理论/评分扣减"
                )
            effective_state_by_identity[identity] = state
            effective_reason_by_identity[identity] = reason

        context = _SupportCounterfactualContext(
            team_damage_events=team_damage_events,
            total_team_damage=float(sum(row.amount for row in team_damage_events)),
            effects=tuple(effects),
            intervals_by_identity=intervals_by_identity,
            effective_intervals_by_identity=effective_intervals_by_identity,
            effective_state_by_identity=effective_state_by_identity,
            effective_reason_by_identity=effective_reason_by_identity,
            hit_assumed_identities=frozenset(hit_assumed_by_identity),
            personal_attack_effects=personal_attack_effects,
            boss_hp_segments=_boss_hp_segments_from_theory_plans(theory_plans),
            support_boss_scope=from_plans(theory_plans),
        )
        (
            context.actual_source_contributions,
            context.actual_detail_contributions,
        ) = self._counterfactual_support_contributions(context)
        return context

    def _counterfactual_support_contributions(
        self,
        context: _SupportCounterfactualContext,
        *,
        replacement_source_id: str = "",
        replacement_intervals: dict[
            tuple[str, str, str], tuple[tuple[float, float], ...]
        ]
        | None = None,
        modeled_damage_out: list[float] | None = None,
    ) -> tuple[dict[str, float], dict[tuple[str, str, str, str], float]]:
        # Optional research diagnostic: marginal support credits from different
        # multiplicative groups cannot be summed as total modeled damage.
        # Expose the existing calculation without changing score attribution.
        if modeled_damage_out is not None:
            modeled_damage_out[:] = [0.0]
        if context.total_team_damage <= 1e-9 or not context.team_damage_events:
            return {}, {}
        replacements = replacement_intervals or {}
        from .support_boss_scope import eligible_event
        scope = context.support_boss_scope
        # Built once with the shared context and reused in player workers and
        # replacement passes; do not rescan all Boss windows per provider pass.
        eligible_by_provider = context.support_boss_event_eligibility
        if scope:
            for source in {instance.source_id for instance in context.effects}:
                if source not in eligible_by_provider:
                    eligible_by_provider[source] = tuple(
                        eligible_event(scope, source, event) for event in context.team_damage_events)
        source_damage: dict[str, float] = defaultdict(float)
        detail_damage: dict[tuple[str, str, str, str], float] = defaultdict(float)
        has_baseline = bool(context.baseline_damage_by_event)
        baseline_rows = (
            list(context.baseline_damage_by_event)
            if has_baseline
            else [0.0] * len(context.team_damage_events)
        )

        # Damage packets greatly outnumber support-state changes. Build one
        # interval sweep, then reuse cap/Shapley allocations for packets with
        # the same active effects and recipient profile. Every event and its
        # exact timestamp remain in the calculation; only identical state work
        # is memoized.
        starts: list[tuple[float, int]] = []
        ends: list[tuple[float, int]] = []
        replaced_effect_indexes: set[int] = set()
        recipient_intervals_by_effect = tuple(
            dict(instance.recipient_intervals) for instance in context.effects
        )
        resolved_intervals: dict[int, tuple[tuple[float, float], ...]] = {}
        admission_inputs: list[EffectInput] = []
        for effect_index, instance in enumerate(context.effects):
            expectation = instance.expectation
            local_identity = (
                expectation.skill_key,
                expectation.effect_key,
                expectation.kind,
            )
            replaced = (
                instance.source_id == replacement_source_id
                and local_identity in replacements
            )
            hit_assumed_resistance = (
                self._support_effect_identity(instance.source_id, expectation)
                in context.hit_assumed_identities
                or (
                    replaced
                    and expectation.effect_key in _ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS
                    and bool(replacements[local_identity])
                )
            )
            override_admission = (
                hit_assumed_resistance
                and effect_rule(EffectInput(
                    key=expectation.effect_key,
                    value=expectation.magnitude,
                    skill_name=expectation.skill_name,
                    stack_group=expectation.stack_group,
                    stackable=expectation.stackable,
                )).group_mode == "exclusive_admission"
            )
            if replaced:
                replaced_effect_indexes.add(effect_index)
            intervals = tuple(
                replacements.get(local_identity, ())
                if replaced
                else instance.intervals
            )
            resolved_intervals[effect_index] = intervals
            for interval_index, (start, end) in enumerate(intervals):
                if end < start - 1e-9:
                    continue
                admission_inputs.append(
                    EffectInput(
                        key=expectation.effect_key,
                        value=expectation.magnitude,
                        skill_name=expectation.skill_name,
                        source_id=instance.source_id,
                        target_id="boss",
                        instance_id=f"{effect_index}:{interval_index}",
                        start=float(start),
                        end=float(end),
                        stack_group=(
                            "theory:hit-assumed-resist"
                            if override_admission else expectation.stack_group
                        ),
                        stackable=expectation.stackable,
                        group_mode="max" if override_admission else "",
                        metadata={
                            "effect_index": effect_index,
                            "interval_index": interval_index,
                        },
                    )
                )
        admission_resolution = EFFECT_STACKING_ENGINE.resolve_active(
            admission_inputs, 0.0
        )
        accepted_intervals = {
            (
                int(row.effect.metadata.get("effect_index", -1)),
                int(row.effect.metadata.get("interval_index", -1)),
            )
            for row in admission_resolution.contributions
            if row.accepted
        }
        for effect_index, intervals in resolved_intervals.items():
            for interval_index, (start, end) in enumerate(intervals):
                if (effect_index, interval_index) not in accepted_intervals:
                    continue
                starts.append((float(start), effect_index))
                ends.append((float(end), effect_index))
        starts.sort()
        ends.sort()

        personal_effects_by_target: dict[
            str, list[_PersonalAttackEffectInstance]
        ] = defaultdict(list)
        for personal_effect in context.personal_attack_effects:
            personal_effects_by_target[personal_effect.target_id].append(
                personal_effect
            )

        active_counts: dict[int, int] = defaultdict(int)
        start_index = 0
        end_index = 0
        tolerance = 1e-6
        # Replacements for one profession change only the intervals owned by
        # that profession. The allocation for an identical active-effect and
        # recipient state is therefore exactly reusable across all profession
        # counterfactual passes in this Boss scope.
        state_cache = context.allocation_state_cache
        event_order = sorted(
            range(len(context.team_damage_events)),
            key=lambda index: (
                context.team_damage_events[index].timestamp_ms,
                index,
            ),
        )

        for event_index in event_order:
            score_checkpoint()
            event = context.team_damage_events[event_index]
            seconds = event.timestamp_ms / 1000.0
            while (
                start_index < len(starts)
                and starts[start_index][0] <= seconds + tolerance
            ):
                _start, effect_index = starts[start_index]
                active_counts[effect_index] += 1
                start_index += 1
            while (
                end_index < len(ends)
                and ends[end_index][0] < seconds - tolerance
            ):
                _end, effect_index = ends[end_index]
                remaining = active_counts.get(effect_index, 0) - 1
                if remaining > 0:
                    active_counts[effect_index] = remaining
                else:
                    active_counts.pop(effect_index, None)
                end_index += 1

            # A player Buff is active only for the damage-event owner whose
            # status track contains this timestamp.  Theory replacements are
            # ideal within the provider's Boss cohort. They bypass factual
            # receipt evidence, but never bypass Boss participation.
            recipient_active_indexes = tuple(
                effect_index
                for effect_index in sorted(active_counts)
                if (
                    not context.effects[effect_index].recipient_scoped
                    or (effect_index in replaced_effect_indexes and (
                        not scope or eligible_by_provider[context.effects[effect_index].source_id][event_index]
                    ))
                    or _in_intervals(
                        seconds,
                        recipient_intervals_by_effect[effect_index].get(
                            event.source_id, ()
                        ),
                    )
                )
                and (effect_index not in replaced_effect_indexes or not scope
                     or eligible_by_provider[context.effects[effect_index].source_id][event_index])
            )
            active_indexes = self._active_support_indexes_with_ex_priority(
                recipient_active_indexes,
                context.effects,
            )
            personal_physical, personal_magical = (
                self._static_personal_attack_bonuses(event.job)
            )
            for personal_effect in personal_effects_by_target.get(
                event.source_id, ()
            ):
                if _in_intervals(seconds, personal_effect.intervals):
                    personal_physical += personal_effect.physical_percent
                    personal_magical += personal_effect.magical_percent
            boss_hp_percent = (
                boss_hp_percent_at(context.boss_hp_segments, seconds)
                if context.boss_hp_segments
                else None
            )
            damage_skill_element = self._support_damage_skill_element(
                event.job,
                event.skill_name,
            )
            state_key = (
                active_indexes,
                event.source_id,
                event.job,
                damage_skill_element,
                round(personal_physical, 8),
                round(personal_magical, 8),
                round(boss_hp_percent, 1) if boss_hp_percent is not None else -1.0,
            )
            cached_state = state_cache.get(state_key)
            if cached_state is not None:
                combined_multiplier = cached_state[0]
                allocations = [
                    (group_gain, list(rows))
                    for group_gain, rows in cached_state[1]
                ]
            else:
                grouped: dict[
                    tuple[str, str], list[tuple[_SupportEffectInstance, float]]
                ] = defaultdict(list)
                amplification_rows: list[
                    tuple[_SupportEffectInstance, float]
                ] = []
                defense_reduction_rows: list[_SupportEffectInstance] = []
                element_resistance_rows: list[_SupportEffectInstance] = []
                critical_resistance_rows: list[_SupportEffectInstance] = []
                attack_buff_rows: list[_SupportEffectInstance] = []
                for effect_index in active_indexes:
                    instance = context.effects[effect_index]
                    expectation = instance.expectation
                    if expectation.effect_key in {"双攻%", "物攻%", "魔攻%"}:
                        attack_buff_rows.append(instance)
                        continue
                    if expectation.effect_key in {
                        "减防御%",
                        "减物理防御%",
                        "减魔法防御%",
                    }:
                        defense_reduction_rows.append(instance)
                        continue
                    if expectation.effect_key in _ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS:
                        element_resistance_rows.append(instance)
                        continue
                    if expectation.effect_key in {
                        "减致命抵抗%",
                        "减致命一击抵抗%",
                        "减暴击抵抗%",
                    }:
                        critical_resistance_rows.append(instance)
                        continue
                    gain = self._configured_support_effect_gain_rate_for_recipient(
                        expectation,
                        event.source_id,
                        seconds,
                        context.boss_hp_segments,
                    )
                    if expectation.effect_key in _DAMAGE_AMPLIFICATION_EFFECT_KEYS:
                        if gain > 1e-12:
                            amplification_rows.append((instance, gain))
                        continue
                    gain *= self._support_effect_relevance(
                        expectation.effect_key,
                        event.job,
                        event.skill_name,
                    )
                    if gain <= 1e-12:
                        continue
                    grouped[self._support_effect_group(expectation)].append(
                        (instance, gain)
                    )

                allocations: list[
                    tuple[float, list[tuple[_SupportEffectInstance, float]]]
                ] = []
                for group_key, active_rows in grouped.items():
                    gains = [gain for _instance, gain in active_rows]
                    if group_key[0] == "freeze":
                        group_gain, shares = self._freeze_effect_shares(active_rows)
                    else:
                        # One max/exclusive client group has one effective
                        # layer. A lower packet-visible fallback receives no
                        # contribution until the stronger layer ends. Equal
                        # highest providers split that one gain.
                        shares = self._highest_effect_shares(gains)
                        group_gain = sum(shares)
                    if group_gain <= 1e-12:
                        continue
                    allocations.append(
                        (
                            group_gain,
                            [
                                (instance, share)
                                for (instance, _gain), share in zip(
                                    active_rows, shares, strict=True
                                )
                                if share > 1e-12
                            ],
                        )
                    )

                attack_gain, attack_allocations = (
                    self._team_attack_buff_allocation(
                        attack_buff_rows,
                        context,
                        event.source_id,
                        event.job,
                        seconds,
                    )
                )
                if attack_gain > 1e-12:
                    allocations.append((attack_gain, attack_allocations))

                amplification_gain, amplification_allocations = (
                    self._damage_amplification_allocation(
                        amplification_rows, event.job
                    )
                )
                if amplification_gain > 1e-12:
                    allocations.append(
                        (amplification_gain, amplification_allocations)
                    )

                defense_gain, defense_allocations = (
                    self._defense_reduction_allocation(
                        defense_reduction_rows, event.job
                    )
                )
                if defense_gain > 1e-12:
                    allocations.append((defense_gain, defense_allocations))

                resistance_gain, resistance_allocations = (
                    self._element_resistance_reduction_allocation(
                        element_resistance_rows,
                        event.job,
                        event.skill_name,
                    )
                )
                if resistance_gain > 1e-12:
                    allocations.append(
                        (resistance_gain, resistance_allocations)
                    )

                critical_gain, critical_allocations = (
                    self._critical_resistance_reduction_allocation(
                        critical_resistance_rows,
                        event.source_id,
                    )
                )
                if critical_gain > 1e-12:
                    allocations.append((critical_gain, critical_allocations))

                combined_multiplier = math.prod(
                    1.0 + group_gain for group_gain, _rows in allocations
                )
                state_cache[state_key] = (
                    combined_multiplier,
                    tuple(
                        (group_gain, tuple(rows))
                        for group_gain, rows in allocations
                    ),
                )

            if has_baseline:
                baseline_damage = context.baseline_damage_by_event[event_index]
            else:
                baseline_damage = event.amount / max(1.0, combined_multiplier)
                baseline_rows[event_index] = baseline_damage
            modeled_damage = baseline_damage * combined_multiplier
            if modeled_damage_out is not None:
                modeled_damage_out[0] += modeled_damage
            for group_gain, rows in allocations:
                # Use the real leave-one-group-out loss. Logarithmically
                # splitting the complete support bonus diluted elemental
                # resistance effects whenever unrelated buffs were present.
                group_bonus = modeled_damage * (
                    1.0 - 1.0 / (1.0 + group_gain)
                )
                if group_bonus <= 1e-12:
                    continue
                for instance, share in rows:
                    # Preserve factual effects when recovering baseline damage;
                    # only support credit is limited to the provider's duty.
                    if scope and not eligible_by_provider[instance.source_id][event_index]:
                        continue
                    credit = group_bonus * share / max(1e-12, group_gain)
                    identity = self._support_effect_identity(
                        instance.source_id, instance.expectation
                    )
                    source_damage[instance.source_id] += credit
                    detail_damage[identity] += credit

        if not has_baseline:
            context.baseline_damage_by_event = tuple(baseline_rows)
        scale = 100.0 / context.total_team_damage
        return (
            {source_id: value * scale for source_id, value in source_damage.items()},
            {identity: value * scale for identity, value in detail_damage.items()},
        )

    def _support_scores(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        job: str,
        output_windows: tuple[tuple[float, float], ...],
        theory_plan: dict[str, Any] | None = None,
        boss_target_ids: frozenset[str] | None = None,
        counterfactual_context: _SupportCounterfactualContext | None = None,
        optional_support_skill_keys: frozenset[str] = frozenset(),
    ) -> tuple[float, float, tuple[SupportScoreDetail, ...]]:
        platform = event_platform_for(snapshot)
        from .support_boss_scope import opportunities, coverage as cohort_coverage, eligible_event, intersect as scope_intersect
        personal = (theory_plan or {}).get('_score_v2', {}).get('player_output_windows')
        if personal is not None:
            output_windows = scope_intersect(output_windows, personal)
        optional_support_skill_keys = (
            frozenset(optional_support_skill_keys)
            | _optional_support_effect_skills_for_job(job)
        )
        expected = self._support_expectations_with_glyphs(
            tuple(
                self._resolved_paladin_aura_expectation(
                    snapshot,
                    player.source_id,
                    job,
                    row,
                )
                for row in self._combat_support_expectations(snapshot, player.source_id, job)
            ),
            theory_plan,
        )
        optional_support_skill_keys = (
            optional_support_skill_keys
            | self._backup_support_skill_keys_for_player(
                snapshot,
                player,
                expected,
            )
        )
        if not expected:
            return 100.0, 100.0, ()
        if counterfactual_context is None:
            counterfactual_context = self._build_support_counterfactual_context(
                snapshot,
                output_windows,
                boss_target_ids,
                {player.source_id: theory_plan} if theory_plan else None,
            )
        end_seconds = max(
            platform.elapsed_seconds,
            platform.dps_elapsed_seconds,
            platform.boss_effect_current_seconds,
        )
        support_scope = counterfactual_context.support_boss_scope
        recipient_opportunities = {
            actor: scope_intersect(ws, output_windows)
            for actor, ws in opportunities(support_scope, player.source_id).items()
        } if support_scope else None
        from .support_boss_scope import living_opportunities
        living_windows = living_opportunities(
            {row.source_id: output_windows for row in platform.players},
            platform.player_health_tracks,
        )
        if recipient_opportunities is not None:
            recipient_opportunities = {
                actor: scope_intersect(ws, living_windows.get(actor, output_windows))
                for actor, ws in recipient_opportunities.items()
            }
        team_damage_events = tuple(event for event in counterfactual_context.team_damage_events
            if eligible_event(support_scope, player.source_id, event))
        total_team_damage = sum(event.amount for event in team_damage_events)
        event_times = tuple(
            event.timestamp_ms / 1000.0 for event in team_damage_events
        )
        damage_prefix_cache: dict[str, tuple[float, ...]] = {}
        crit_prefix_cache: dict[_SupportExpectation, tuple[float, ...]] = {}

        def prefix_interval_sum(
            prefix: tuple[float, ...],
            intervals: tuple[tuple[float, float], ...],
        ) -> float:
            value = 0.0
            for start, end in _merge_intervals(intervals):
                left = bisect_left(event_times, start - 1e-6)
                right = bisect_right(event_times, end + 1e-6)
                value += prefix[right] - prefix[left]
            return value

        interval_damage_cache: dict[
            tuple[tuple[tuple[float, float], ...], str], float
        ] = {}
        support_instance_by_identity = {
            self._support_effect_identity(
                instance.source_id, instance.expectation
            ): instance
            for instance in counterfactual_context.effects
        }

        def damage_in_intervals(
            intervals: tuple[tuple[float, float], ...],
            expectation: _SupportExpectation,
        ) -> float:
            if not intervals:
                return 0.0
            cache_key = (intervals, expectation.effect_key)
            cached = interval_damage_cache.get(cache_key)
            if cached is not None:
                return cached
            prefix = damage_prefix_cache.get(expectation.effect_key)
            if prefix is None:
                running = 0.0
                rows = [0.0]
                for event in team_damage_events:
                    running += event.amount * self._support_effect_relevance(
                        expectation.effect_key,
                        event.job,
                        event.skill_name,
                    )
                    rows.append(running)
                prefix = tuple(rows)
                damage_prefix_cache[expectation.effect_key] = prefix
            value = prefix_interval_sum(prefix, intervals)
            interval_damage_cache[cache_key] = value
            return value

        def recipient_time_actual_coverage(
            expectation: _SupportExpectation,
            fallback: float,
        ) -> float:
            """Return captured uptime without consulting either scoring axis.

            Team Buffs may be received by only part of the party.  The visible
            real-combat coverage uses eligible living recipient-seconds.
            Keeping damage weights out of this
            metric makes it stable when the planner or score formula changes.
            """

            instance = support_instance_by_identity.get(
                self._support_effect_identity(player.source_id, expectation)
            )
            if instance is None or not instance.recipient_scoped:
                return fallback
            intervals_by_recipient = dict(instance.recipient_intervals)
            if recipient_opportunities is not None:
                return cohort_coverage(recipient_opportunities, intervals_by_recipient)
            party_ids = tuple(intervals_by_recipient)
            if expectation.skill_key in {_PALADIN_AURA_GROUP_KEY, "火焰光环", "寒冰光环"}:
                # Missing receipt evidence must not remove a roster member
                # from the mean and turn partial-party coverage into 100%.
                party_ids = tuple(dict.fromkeys(row.source_id for row in platform.players)) or party_ids
            if not party_ids:
                return fallback
            return cohort_coverage(
                {actor: living_windows.get(actor, output_windows) for actor in party_ids},
                intervals_by_recipient,
            )

        def recipient_weighted_actual_coverage(
            expectation: _SupportExpectation,
            fallback: float,
        ) -> float:
            """Return relevant damage actually covered by a recipient Buff."""

            instance = support_instance_by_identity.get(
                self._support_effect_identity(player.source_id, expectation)
            )
            if instance is None or not instance.recipient_scoped:
                return fallback
            intervals_by_recipient = dict(instance.recipient_intervals)
            relevant_total = 0.0
            relevant_covered = 0.0
            for event in team_damage_events:
                relevance = self._support_effect_relevance(
                    expectation.effect_key,
                    event.job,
                    event.skill_name,
                )
                weight = max(0.0, event.amount * relevance)
                if weight <= 1e-12:
                    continue
                relevant_total += weight
                if _in_intervals(
                    event.timestamp_ms / 1000.0,
                    intervals_by_recipient.get(event.source_id, ()),
                ):
                    relevant_covered += weight
            if relevant_total > 1e-9:
                return max(0.0, min(1.0, relevant_covered / relevant_total))
            return recipient_time_actual_coverage(expectation, fallback)

        def effective_damage_coverage(
            expectation: _SupportExpectation,
            intervals: tuple[tuple[float, float], ...],
            fallback: float,
        ) -> float:
            """Share of effect-relevant team damage that truly received it."""

            if expectation.kind != "debuff":
                return recipient_weighted_actual_coverage(expectation, fallback)
            relevant_total = damage_in_intervals(output_windows, expectation)
            if relevant_total <= 1e-9:
                return fallback
            return max(
                0.0,
                min(
                    1.0,
                    damage_in_intervals(intervals, expectation) / relevant_total,
                ),
            )

        def effective_damage_theory_coverage(
            expectation: _SupportExpectation,
            intervals: tuple[tuple[float, float], ...],
            fallback: float,
        ) -> float:
            """Share of relevant observed damage covered by the theory axis.

            The actual Team Buff metric remains recipient-scoped because the
            capture tells us exactly who received it.  The local theoretical
            axis represents a legal ideal cast and therefore applies to all
            eligible recipients while its interval is active.  Both actual
            and theory are measured against the same observed damage stream.
            """

            relevant_total = damage_in_intervals(output_windows, expectation)
            if relevant_total <= 1e-9:
                return fallback
            return max(
                0.0,
                min(
                    1.0,
                    damage_in_intervals(intervals, expectation) / relevant_total,
                ),
            )

        def theory_damage_in_intervals(
            intervals: tuple[tuple[float, float], ...],
            expectation: _SupportExpectation,
        ) -> float:
            # Actual and theoretical schedules must be compared against the
            # same observed damage stream. Only this profession's effect
            # intervals change; the other seven providers stay untouched.
            return damage_in_intervals(intervals, expectation)

        theory_total_team_damage = total_team_damage

        crit_gain_damage_cache: dict[
            tuple[tuple[tuple[float, float], ...], _SupportExpectation], float
        ] = {}

        def gain_weighted_damage_in_intervals(
            intervals: tuple[tuple[float, float], ...],
            expectation: _SupportExpectation,
        ) -> float:
            if not intervals:
                return 0.0
            dynamic_hp_effect = expectation.skill_key in {
                canonical_skill(CROW_SKILL),
                canonical_skill(DANCE_STAGE_TWO_SKILL),
            }
            if expectation.effect_key != "致命%" and not dynamic_hp_effect:
                return (
                    damage_in_intervals(intervals, expectation)
                    * self._configured_support_effect_gain_rate(expectation)
                )
            cache_key = (intervals, expectation)
            cached = crit_gain_damage_cache.get(cache_key)
            if cached is not None:
                return cached
            prefix = crit_prefix_cache.get(expectation)
            if prefix is None:
                running = 0.0
                rows = [0.0]
                for event in team_damage_events:
                    seconds = event.timestamp_ms / 1000.0
                    running += (
                        event.amount
                        * self._support_effect_relevance(
                            expectation.effect_key,
                            event.job,
                            event.skill_name,
                        )
                        * self._configured_support_effect_gain_rate_for_recipient(
                            expectation,
                            event.source_id,
                            seconds,
                            counterfactual_context.boss_hp_segments,
                        )
                    )
                    rows.append(running)
                prefix = tuple(rows)
                crit_prefix_cache[expectation] = prefix
            value = prefix_interval_sum(prefix, intervals)
            crit_gain_damage_cache[cache_key] = value
            return value

        actual_debuff: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for effect in platform.boss_effects:
            if effect.source_id != player.source_id:
                continue
            if boss_target_ids and effect.target_id not in boss_target_ids:
                continue
            key = (canonical_skill(effect.skill_name), effect.key)
            actual_debuff[key].append(effect)
        raw_debuff_events: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for event in platform.boss_effect_events:
            if boss_target_ids and event.target_id not in boss_target_ids:
                continue
            raw_debuff_events[
                (canonical_skill(event.skill_name), event.key)
            ].append(event)
        actual_buff: dict[str, list[Any]] = defaultdict(list)
        for effect in platform.player_buffs:
            if effect.source_id == player.source_id and not is_inferred_aura_row(effect):
                actual_buff[canonical_skill(effect.skill_name)].append(effect)

        detail_parts: list[
            tuple[
                _SupportExpectation,
                float,
                float,
                float,
                float,
                float,
                float,
                tuple[tuple[float, float], ...],
                tuple[tuple[float, float], ...],
                tuple[tuple[float, float], ...],
                tuple[tuple[float, float], ...],
            ]
        ] = []
        application_metrics: dict[
            tuple[str, str, str, str],
            tuple[float, tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]],
        ] = {}
        observed_metrics: dict[
            tuple[str, str, str, str],
            tuple[
                float,
                tuple[tuple[float, float], ...],
                tuple[tuple[float, float], ...],
            ],
        ] = {}
        coverage_weight_total = 0.0
        coverage_weighted_actual = 0.0
        contribution_actual = 0.0
        contribution_theory = 0.0
        claimed_actual: dict[
            tuple[str, str], tuple[tuple[float, float], ...]
        ] = defaultdict(tuple)
        claimed_theory: dict[
            tuple[str, str], tuple[tuple[float, float], ...]
        ] = defaultdict(tuple)
        freeze_layer_count = _freeze_max_layers(job)
        claimed_actual_freeze: list[tuple[tuple[float, float], ...]] = [
            () for _ in range(freeze_layer_count)
        ]
        claimed_theory_freeze: list[tuple[tuple[float, float], ...]] = [
            () for _ in range(freeze_layer_count)
        ]
        modifier_windows = self._combat_modifier_windows(snapshot, player.source_id)
        display_windows = (
            ((output_windows[0][0], output_windows[-1][1]),)
            if output_windows
            else ()
        )
        responsibility_observation_windows = _merge_intervals(
            (max(0.0, start - 5.0), end) for start, end in output_windows
        )
        # A support duty belongs to the selected Boss scope only when the
        # player actually used it there. Captured effect intervals are the
        # strongest evidence. Explicit casts can recover missing status packets
        # for buffs and debuffs, while damage events are only a safe fallback
        # for debuffs: packet skill-id collisions can otherwise make ordinary
        # damage look like an unused team buff.
        observed_support_cast_skills = {
            canonical_skill(event.skill_name)
            for event in event_platform_for(
                snapshot
            ).event_index.casts_by_source.get(
                player.source_id, ()
            )
            if _in_intervals(
                event.timestamp_ms / 1000.0,
                responsibility_observation_windows,
            )
        }
        observed_support_damage_skills = {
            canonical_skill(event.skill_name)
            for event in event_platform_for(
                snapshot
            ).event_index.combat_by_source.get(
                player.source_id, ()
            )
            if not event.healing
            and event.amount > 0
            and (not boss_target_ids or event.target_id in boss_target_ids)
            and _in_intervals(
                event.timestamp_ms / 1000.0,
                responsibility_observation_windows,
            )
        }
        responsibility_filtered = False
        for expectation in sorted(
            expected,
            key=self._configured_support_effect_gain_rate,
            reverse=True,
        ):
            if expectation.kind == "debuff":
                effect_identity = (
                    expectation.skill_key, expectation.effect_key
                )
                raw_events = raw_debuff_events.get(effect_identity, [])
                source_events = [
                    row
                    for row in raw_events
                    if str(getattr(row, "source_id", "")) == player.source_id
                ]
                if source_events:
                    actual_intervals = _clip_intervals(
                        _source_boss_effect_intervals(
                            source_events, player.source_id, end_seconds
                        ),
                        end_seconds,
                    )
                else:
                    effect_rows = actual_debuff.get(effect_identity, [])
                    actual_intervals = _clip_intervals(
                        (
                            interval
                            for row in effect_rows
                            for interval in (
                                row.intervals
                                or ((row.start_seconds, row.end_seconds),)
                            )
                        ),
                        end_seconds,
                    )
                if expectation.effect_key == "冰冻层":
                    actual_intervals = _merge_intervals(
                        (
                            *actual_intervals,
                            *self._confirmed_freeze_cast_intervals(
                                snapshot,
                                player.source_id,
                                expectation,
                                end_seconds,
                                raw_events,
                                boss_target_ids,
                            ),
                        )
                    )
            elif expectation.skill_key == _PALADIN_AURA_GROUP_KEY:
                effect_rows = []
                actual_intervals = _clip_intervals(
                    (
                        interval
                        for row in platform.player_buff_series
                        if row.source_id == player.source_id
                        and canonical_skill(row.skill_name) in _PALADIN_AURA_SKILLS
                        and not is_inferred_aura_row(row)
                        and is_aura_damage_series(row)
                        for interval in row.intervals
                    ),
                    end_seconds,
                )
            else:
                source_series = [
                    row
                    for row in platform.player_buff_series
                    if row.source_id == player.source_id
                    and not is_inferred_aura_row(row)
                    and canonical_skill(row.skill_name)
                    == expectation.skill_key
                    and (expectation.skill_key not in {"火焰光环", "寒冰光环"} or is_aura_damage_series(row))
                ]
                if source_series:
                    effect_rows = list(
                        _primary_player_buff_effect_rows(
                            source_series,
                            expectation.magnitude,
                        )
                    )
                    actual_intervals = _clip_intervals(
                        (
                            interval
                            for row in effect_rows
                            for interval in row.intervals
                        ),
                        end_seconds,
                    )
                else:
                    effect_rows = list(
                        _primary_player_buff_effect_rows(
                            [row for row in actual_buff.get(expectation.skill_key, [])
                             if expectation.skill_key not in {"火焰光环", "寒冰光环"} or is_aura_damage_series(row)],
                            expectation.magnitude,
                        )
                    )
                    actual_intervals = _clip_intervals(
                        (
                            interval
                            for row in effect_rows
                            for interval in (
                                row.intervals
                                or ((row.start_seconds, row.end_seconds),)
                            )
                        ),
                        end_seconds,
                    )
                actual_intervals = _bounded_recorded_team_buff_intervals(
                    snapshot,
                    player.source_id,
                    expectation.skill_key,
                    expectation.duration,
                    actual_intervals,
                    end_seconds,
                )
            packet_observed_intervals = actual_intervals
            identity = self._support_effect_identity(
                player.source_id, expectation
            )
            if (
                expectation.kind == "debuff"
                and expectation.effect_key in _ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS
            ):
                # The context already indexed the same source/skill Boss hits.
                # Reuse its intervals instead of rescanning every player's
                # combat events during the scoring pass.
                actual_intervals = counterfactual_context.intervals_by_identity.get(
                    identity, actual_intervals
                )
            support_display_windows = (
                output_windows
                if expectation.skill_key == canonical_skill(_YAO_MEDITATION_SKILL)
                else display_windows
            )
            actual_display_intervals = _intersect_intervals(
                actual_intervals, support_display_windows
            )
            actual_intervals = _intersect_intervals(actual_intervals, output_windows)
            if (
                expectation.effect_key in _ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS
                and resistance_application_mode(
                    expectation.skill_key, expectation.effect_key
                ) == "hit"
            ):
                # Preserve the Hit/packet application axis before same-key
                # strongest-value arbitration. The latter is still used for
                # damage, score, and actual_coverage.
                application_metrics[identity] = (
                    _coverage_in_windows(actual_intervals, output_windows),
                    actual_intervals,
                    actual_display_intervals,
                )
            observed_metrics[identity] = (
                _coverage_in_windows(packet_observed_intervals, output_windows),
                _intersect_intervals(packet_observed_intervals, output_windows),
                _intersect_intervals(packet_observed_intervals, support_display_windows),
            )
            if expectation.kind == "debuff" and expectation.effect_key != "冰冻层":
                effective_intervals = (
                    counterfactual_context.effective_intervals_by_identity.get(
                        identity, actual_intervals
                    )
                )
                actual_display_intervals = _intersect_intervals(
                    effective_intervals, support_display_windows
                )
                actual_intervals = _intersect_intervals(
                    effective_intervals, output_windows
                )
            observed_skill_keys = (
                _PALADIN_AURA_SKILLS
                if expectation.skill_key == _PALADIN_AURA_GROUP_KEY
                else (expectation.skill_key,)
            )
            observed_by_cast = any(
                skill_key in observed_support_cast_skills
                for skill_key in observed_skill_keys
            )
            observed_by_damage = expectation.kind == "debuff" and any(
                skill_key in observed_support_damage_skills
                for skill_key in observed_skill_keys
            )
            if (
                not observed_metrics.get(
                    identity, (0.0, (), actual_display_intervals)
                )[2]
                and not observed_by_cast
                and not observed_by_damage
            ):
                responsibility_filtered = True
                continue
            actual = _coverage_in_windows(actual_intervals, output_windows)
            if expectation.kind != "debuff":
                actual = recipient_time_actual_coverage(
                    expectation, actual
                )
            optional_support = (
                expectation.skill_key in optional_support_skill_keys
            )
            if optional_support:
                # Optional utility is a real-performance bonus, not a required
                # theoretical duty. The actual interval remains in the shared
                # counterfactual context, while the theory replacement removes
                # it so it cannot inflate the profession's denominator.
                theory_intervals = ()
            elif expectation.skill_key == _PALADIN_AURA_GROUP_KEY:
                theory_intervals = output_windows
            else:
                planned_intervals = self._planned_support_intervals(
                    theory_plan,
                    expectation,
                    end_seconds,
                    modifier_windows,
                )
                theory_intervals = (
                    planned_intervals
                    if isinstance(theory_plan, dict)
                    else self._theory_support_intervals(
                        expectation,
                        end_seconds,
                        output_windows,
                        modifier_windows,
                    )
                )
            if expectation.effect_key == "冰冻层":
                theory_intervals = self._theory_freeze_intervals_from_actual_hits(
                    snapshot,
                    job,
                    expectation,
                    theory_intervals,
                    output_windows,
                    boss_target_ids,
                )
            theory_display_intervals = _intersect_intervals(
                theory_intervals, support_display_windows
            )
            theory_intervals = _intersect_intervals(theory_intervals, output_windows)
            theory = _coverage_in_windows(theory_intervals, output_windows)
            if expectation.kind != 'debuff' and recipient_opportunities is not None:
                theory = cohort_coverage(recipient_opportunities,
                    {actor: theory_intervals for actor in recipient_opportunities})
            elif expectation.kind != 'debuff':
                instance = support_instance_by_identity.get(
                    self._support_effect_identity(player.source_id, expectation))
                if instance is not None and instance.recipient_scoped:
                    actors = tuple(dict(instance.recipient_intervals))
                    if expectation.skill_key in {_PALADIN_AURA_GROUP_KEY, "火焰光环", "寒冰光环"}:
                        actors = tuple(living_windows) or actors
                    if actors:
                        theory = cohort_coverage(
                            {actor: living_windows.get(actor, output_windows) for actor in actors},
                            {actor: theory_intervals for actor in actors})
            # This provisional ratio is replaced with counterfactual benefit
            # completion once actual/theoretical gains are available.  It is
            # retained here only for legacy no-damage fallbacks.
            completion_ratio = actual / theory if theory > 0 else 1.0
            relevant_damage = damage_in_intervals(output_windows, expectation)
            if expectation.effect_key == "致命%":
                gain_rate = (
                    gain_weighted_damage_in_intervals(
                        output_windows, expectation
                    )
                    / relevant_damage
                    if relevant_damage > 1e-9
                    else 0.0
                )
            else:
                gain_rate = self._configured_support_effect_gain_rate(expectation)
            relevance = (
                relevant_damage / total_team_damage
                if total_team_damage > 1e-9
                else 1.0
            )
            coverage_weight = (
                gain_rate
                * 100.0
                * relevance
                * math.sqrt(max(0.01, theory))
            )
            coverage_weight_total += coverage_weight
            coverage_weighted_actual += completion_ratio * coverage_weight
            if expectation.effect_key == "冰冻层":
                effective_actual_layers = _allocate_capped_interval_layers(
                    actual_intervals, claimed_actual_freeze
                )
                effective_theory_layers = _allocate_capped_interval_layers(
                    theory_intervals, claimed_theory_freeze
                )
            else:
                contribution_group = (
                    (
                        "stackable-debuff",
                        expectation.skill_key,
                    )
                    if expectation.kind == "debuff" and expectation.stackable
                    else ("debuff", expectation.effect_key)
                    if expectation.kind == "debuff"
                    else ("buff", expectation.skill_key)
                )
                effective_actual_intervals = _subtract_intervals(
                    actual_intervals, claimed_actual[contribution_group]
                )
                effective_theory_intervals = _subtract_intervals(
                    theory_intervals, claimed_theory[contribution_group]
                )
                claimed_actual[contribution_group] = _merge_intervals(
                    (*claimed_actual[contribution_group], *actual_intervals)
                )
                claimed_theory[contribution_group] = _merge_intervals(
                    (*claimed_theory[contribution_group], *theory_intervals)
                )
                effective_actual_layers = (effective_actual_intervals,)
                effective_theory_layers = (effective_theory_intervals,)
            if total_team_damage > 1e-9:
                actual_contribution = (
                    sum(
                        gain_weighted_damage_in_intervals(layer, expectation)
                        for layer in effective_actual_layers
                    )
                    / total_team_damage
                    * 100.0
                )
                theory_contribution = (
                    sum(
                        gain_weighted_damage_in_intervals(layer, expectation)
                        for layer in effective_theory_layers
                    )
                    / total_team_damage
                    * 100.0
                )
            else:
                actual_contribution = (
                    sum(
                        _coverage_in_windows(layer, output_windows)
                        for layer in effective_actual_layers
                    )
                    * gain_rate
                    * 100.0
                )
                theory_contribution = (
                    sum(
                        _coverage_in_windows(layer, output_windows)
                        for layer in effective_theory_layers
                    )
                    * gain_rate
                    * 100.0
                )
            if gain_rate > 0:
                contribution_actual += actual_contribution
                contribution_theory += theory_contribution
            detail_parts.append(
                (
                    expectation,
                    actual,
                    completion_ratio,
                    coverage_weight,
                    actual_contribution,
                    theory_contribution,
                    theory,
                    actual_intervals,
                    theory_intervals,
                    actual_display_intervals,
                    theory_display_intervals,
                )
            )
        if total_team_damage > 1e-9:
            theory_replacements = {
                (
                    expectation.skill_key,
                    expectation.effect_key,
                    expectation.kind,
                ): theory_intervals
                for (
                    expectation,
                    _actual,
                    _completion_ratio,
                    _coverage_weight,
                    _actual_contribution,
                    _theory_contribution,
                    _theory,
                    _actual_intervals,
                    theory_intervals,
                    _actual_display_intervals,
                    _theory_display_intervals,
                ) in detail_parts
            }
            theory_source_contributions, theory_detail_contributions = (
                self._counterfactual_support_contributions(
                    counterfactual_context,
                    replacement_source_id=player.source_id,
                    replacement_intervals=theory_replacements,
                )
            )
            contribution_actual = counterfactual_context.actual_source_contributions.get(
                player.source_id, 0.0
            )
            contribution_theory = theory_source_contributions.get(
                player.source_id, 0.0
            )
            counterfactual_parts = []
            for part in detail_parts:
                (
                    expectation,
                    actual,
                    completion_ratio,
                    coverage_weight,
                    _actual_contribution,
                    _theory_contribution,
                    theory,
                    actual_intervals,
                    theory_intervals,
                    actual_display_intervals,
                    theory_display_intervals,
                ) = part
                identity = self._support_effect_identity(
                    player.source_id, expectation
                )
                actual_contribution = (
                    counterfactual_context.actual_detail_contributions.get(
                        identity, 0.0
                    )
                )
                theory_contribution = theory_detail_contributions.get(
                    identity, 0.0
                )
                counterfactual_parts.append(
                    (
                        expectation,
                        actual,
                        completion_ratio,
                        coverage_weight,
                        actual_contribution,
                        theory_contribution,
                        theory,
                        actual_intervals,
                        theory_intervals,
                        actual_display_intervals,
                        theory_display_intervals,
                    )
                )
            detail_parts = counterfactual_parts
            contribution_theory = sum(
                max(0.0, as_number(part[5])) for part in detail_parts
            )
            if contribution_theory > 1e-9:
                coverage_weight_total = contribution_theory
                coverage_weighted_actual = sum(
                    completion_ratio * max(0.0, theory_contribution)
                    for (
                        _expectation,
                        _actual,
                        completion_ratio,
                        _coverage_weight,
                        _actual_contribution,
                        theory_contribution,
                        _theory,
                        _actual_intervals,
                        _theory_intervals,
                        _actual_display_intervals,
                        _theory_display_intervals,
                    ) in detail_parts
                )
        replan = (
            theory_plan.get("_support_counterfactual_replan", {})
            if isinstance(theory_plan, dict)
            else {}
        )
        if (
            isinstance(replan, dict)
            and replan.get("method") in {
                "remove_effect_replan",
                "standalone_timing_effect_replan",
                "shared_timing_gain_allocation",
                "leave_one_out_timing_replan",
                "conditional_timing_replan",
            }
            and isinstance(replan.get("skill_loss_percent"), dict)
        ):
            # The fixed-event counterfactual above remains useful for measuring
            # how well the captured casts used each window. Absolute theoretical
            # value comes from a fully replanned team axis. New plans independently
            # add each timing effect to a neutral axis; old remove-effect plans are
            # still accepted for cached captures. Both capture cooldown, action-
            # speed and rotation changes instead of treating effects as flat gains.
            exact_skill_losses = {
                canonical_skill(key): max(0.0, as_number(value))
                for key, value in replan["skill_loss_percent"].items()
                if canonical_skill(key)
            }
            raw_actual_skill_gains = replan.get(
                "actual_skill_gain_percent", {}
            )
            if not isinstance(raw_actual_skill_gains, dict):
                raw_actual_skill_gains = {}
            raw_theory_skill_gains = replan.get(
                "theory_skill_gain_percent", {}
            )
            if not isinstance(raw_theory_skill_gains, dict):
                raw_theory_skill_gains = {}
            exact_actual_skill_gains = {
                canonical_skill(key): max(0.0, as_number(value))
                for key, value in raw_actual_skill_gains.items()
                if canonical_skill(key)
            }
            exact_theory_skill_gains = {
                canonical_skill(key): max(0.0, as_number(value))
                for key, value in raw_theory_skill_gains.items()
                if canonical_skill(key)
            }
            has_dual_timing_axis = bool(
                exact_actual_skill_gains and exact_theory_skill_gains
            )
            if has_dual_timing_axis:
                exact_skill_losses = exact_theory_skill_gains
            indexes_by_skill: dict[str, list[int]] = defaultdict(list)
            for index, part in enumerate(detail_parts):
                indexes_by_skill[part[0].skill_key].append(index)

            replace_only = bool(replan.get("replace_only"))
            preserve_base_skills = {
                canonical_skill(skill)
                for skill in replan.get("preserve_base_contribution_skills", ())
                if canonical_skill(skill)
            }
            exact_parts = (
                list(detail_parts)
                if replace_only
                else [
                    (
                        part[0],
                        part[1],
                        part[2],
                        part[3],
                        0.0,
                        0.0,
                        part[6],
                        part[7],
                        part[8],
                        part[9],
                        part[10],
                    )
                    for part in detail_parts
                ]
            )
            for skill_key, indexes in indexes_by_skill.items():
                if skill_key not in exact_skill_losses:
                    continue
                exact_total = exact_skill_losses[skill_key]
                exact_actual_total = exact_actual_skill_gains.get(
                    skill_key, exact_total
                )
                old_theory_weights = [
                    max(0.0, as_number(detail_parts[index][5]))
                    for index in indexes
                ]
                if sum(old_theory_weights) <= 1e-9:
                    old_theory_weights = [
                        max(0.0, as_number(detail_parts[index][3]))
                        for index in indexes
                    ]
                if sum(old_theory_weights) <= 1e-9:
                    old_theory_weights = [1.0 for _index in indexes]
                weight_total = sum(old_theory_weights)
                for index, weight in zip(indexes, old_theory_weights):
                    part = detail_parts[index]
                    old_actual = max(0.0, as_number(part[4]))
                    old_theory = max(0.0, as_number(part[5]))
                    preserve_base = skill_key in preserve_base_skills
                    if preserve_base:
                        # The row already measures the non-timing panel/stat
                        # contribution. Add the replanned CDR/action-speed gain
                        # instead of replacing that contribution with it.
                        base_actual = old_actual
                        base_theory = old_theory
                        realized_ratio = max(0.0, as_number(part[2]))
                    else:
                        base_actual = 0.0
                        base_theory = 0.0
                        realized_ratio = max(
                            0.0,
                            old_actual / old_theory
                            if old_theory > 1e-9
                            else as_number(part[2]),
                        )
                    exact_timing = exact_total * weight / max(1e-9, weight_total)
                    exact_theory = base_theory + exact_timing
                    if has_dual_timing_axis:
                        exact_actual_timing = (
                            exact_actual_total * weight / max(1e-9, weight_total)
                        )
                    else:
                        exact_actual_timing = exact_timing * realized_ratio
                    # Do not hide an observed-over-theory result.  The legal
                    # planner now includes the recorded schedule as a candidate;
                    # any remaining ratio above 100% is valuable diagnostics,
                    # not a value to silently clamp away.
                    exact_actual = base_actual + exact_actual_timing
                    display_actual = max(0.0, as_number(part[1]))
                    display_theory = max(0.0, as_number(part[6]))
                    display_completion = max(0.0, as_number(part[2]))
                    exact_parts[index] = (
                        part[0],
                        display_actual,
                        display_completion,
                        part[3],
                        exact_actual,
                        exact_theory,
                        display_theory,
                        part[7],
                        part[8],
                        part[9],
                        part[10],
                    )
            detail_parts = exact_parts
            contribution_actual = sum(
                max(0.0, as_number(part[4])) for part in detail_parts
            )
            contribution_theory = (
                sum(max(0.0, as_number(part[5])) for part in detail_parts)
                if replace_only
                or responsibility_filtered
                else max(
                    0.0,
                    as_number(
                        replan.get("source_loss_percent"),
                        sum(max(0.0, as_number(part[5])) for part in detail_parts),
                    ),
                )
            )
            exact_detail_total = sum(
                max(0.0, as_number(part[5])) for part in detail_parts
            )
            if (
                not replace_only
                and contribution_theory > 1e-9
                and exact_detail_total > 1e-9
            ):
                scale = contribution_theory / exact_detail_total
                detail_parts = [
                    (
                        part[0],
                        part[1],
                        part[2],
                        part[3],
                        max(0.0, as_number(part[4])) * scale,
                        max(0.0, as_number(part[5])) * scale,
                        part[6],
                        part[7],
                        part[8],
                        part[9],
                        part[10],
                    )
                    for part in detail_parts
                ]
                contribution_actual *= scale
            coverage_weight_total = contribution_theory
            coverage_weighted_actual = sum(
                max(0.0, as_number(part[2]))
                * max(0.0, as_number(part[5]))
                for part in detail_parts
            )

        def benefit_completion_ratio(
            expectation: _SupportExpectation,
            actual_contribution: float,
            theory_contribution: float,
        ) -> float:
            """Counterfactual actual gain / legal-optimal gain, without a cap."""

            actual_gain = max(0.0, as_number(actual_contribution))
            theory_gain = max(0.0, as_number(theory_contribution))
            if expectation.skill_key in optional_support_skill_keys:
                return 1.0 if actual_gain > 1e-9 else 0.0
            if theory_gain > 1e-9:
                return actual_gain / theory_gain
            # Required numerator-only rows are invalid and remain visible but
            # excluded below.  A true zero/zero row is neutral rather than a
            # false failure.
            return 0.0 if actual_gain > 1e-9 else 1.0

        # ``part[2]`` is the compatibility completion slot.  From this point on
        # it has exactly one meaning: auxiliary benefit completion.  It must not
        # be derived from either time coverage or damage coverage.
        detail_parts = [
            (
                part[0],
                part[1],
                benefit_completion_ratio(part[0], part[4], part[5]),
                part[3],
                part[4],
                part[5],
                part[6],
                part[7],
                part[8],
                part[9],
                part[10],
            )
            for part in detail_parts
        ]

        def detail_is_scored(
            expectation: _SupportExpectation,
            completion_ratio: float,
            actual_contribution: float,
            theory_contribution: float,
        ) -> bool:
            # A measured required timing duty does not disappear when the
            # provider misses everyone or the Buff is cleared immediately.
            # Keep its attainable denominator so 0% is penalized, not exempted.
            if (
                isinstance(replan, dict)
                and replan.get('method') == 'conditional_timing_replan'
                and expectation.skill_key not in optional_support_skill_keys
                and expectation.skill_key in replan.get('timing_ablation', {})
                and max(0.0, as_number(theory_contribution)) > 1e-9
            ):
                return True
            if not _support_completion_is_scored(completion_ratio):
                return False
            # A required effect with an actual numerator but no theoretical
            # denominator is an invalid ratio, not exceptional support play.
            # This can happen if an external duplicate made the shared planner
            # omit the provider's own cast. Keep the row visible for diagnosis,
            # but never let it turn a tiny unrelated denominator into a support
            # score of several hundred or several thousand percent. Optional
            # effects intentionally use actual-only bonus semantics.
            return not (
                expectation.skill_key not in optional_support_skill_keys
                and max(0.0, as_number(actual_contribution)) > 1e-9
                and max(0.0, as_number(theory_contribution)) <= 1e-9
            )

        # Keep historical trace-level exclusions for unmeasured effects.
        # Measured required timing duties are never exempted merely because
        # they failed: detail_is_scored retains their attainable denominator.
        scored_detail_parts = [
            part
            for part in detail_parts
            if detail_is_scored(part[0], part[2], part[4], part[5])
        ]
        contribution_actual = sum(
            max(0.0, as_number(part[4])) for part in scored_detail_parts
        )
        contribution_theory = sum(
            max(0.0, as_number(part[5])) for part in scored_detail_parts
        )
        support_theory_total = contribution_theory
        optional_actual_total = sum(
            max(0.0, as_number(part[4]))
            for part in scored_detail_parts
            if part[0].skill_key in optional_support_skill_keys
        )
        support_display_weight_total = (
            support_theory_total + optional_actual_total
        )
        if support_theory_total > 1e-9:
            coverage_weight_total = support_theory_total
            coverage_weighted_actual = sum(
                max(0.0, as_number(part[2]))
                * max(0.0, as_number(part[5]))
                for part in scored_detail_parts
            )
        else:
            coverage_weight_total = sum(
                max(0.0, as_number(part[3])) for part in scored_detail_parts
            )
            coverage_weighted_actual = sum(
                max(0.0, as_number(part[2]))
                * max(0.0, as_number(part[3]))
                for part in scored_detail_parts
            )
        coverage = (
            max(
                0.0,
                coverage_weighted_actual
                / max(1e-9, coverage_weight_total)
                * 100.0,
            )
            if scored_detail_parts
            else 100.0
        )
        contribution = _ratio_score(
            contribution_actual,
            contribution_theory,
            neutral=100.0,
            allow_overflow=True,
        )

        def timing_diagnostic_map(name: str) -> dict[str, float]:
            raw = replan.get(name, {}) if isinstance(replan, dict) else {}
            if not isinstance(raw, dict):
                return {}
            return {
                canonical_skill(skill): as_number(value)
                for skill, value in raw.items()
                if canonical_skill(skill)
            }

        actual_linked_map = timing_diagnostic_map(
            "actual_linked_skill_gain_percent"
        )
        actual_total_map = timing_diagnostic_map(
            "actual_total_skill_gain_percent"
        )
        theory_linked_map = timing_diagnostic_map(
            "theory_linked_skill_gain_percent"
        )
        theory_total_map = timing_diagnostic_map(
            "theory_total_skill_gain_percent"
        )

        def allocated_timing_diagnostic(
            values: dict[str, float],
            expectation: _SupportExpectation,
            actual_gain: float,
            theory_gain: float,
            fallback: float,
        ) -> float:
            if expectation.skill_key not in values:
                return fallback
            matching = [
                part for part in detail_parts
                if part[0].skill_key == expectation.skill_key
            ]
            weights = [max(0.0, as_number(part[5])) for part in matching]
            current_weight = max(0.0, as_number(theory_gain))
            if sum(weights) <= 1e-9:
                weights = [max(0.0, as_number(part[4])) for part in matching]
                current_weight = max(0.0, as_number(actual_gain))
            denominator = sum(weights)
            share = current_weight / denominator if denominator > 1e-9 else (
                1.0 / max(1, len(matching))
            )
            return as_number(values[expectation.skill_key]) * share

        details = tuple(
            sorted(
                (
                    SupportScoreDetail(
                        skill_name=expectation.skill_name,
                        effect_key=expectation.effect_key,
                        kind=("团队Buff" if expectation.kind == "buff" else "Debuff"),
                        magnitude=(
                            _freeze_gain_per_layer()
                            if expectation.effect_key == "冰冻层"
                            else expectation.magnitude
                        ),
                        actual_coverage=actual,
                        theory_coverage=theory,
                        completion=completion_ratio * 100.0,
                        score_weight=(
                            0.0
                            if not detail_is_scored(
                                expectation,
                                completion_ratio,
                                actual_contribution,
                                theory_contribution,
                            )
                            else (
                                (
                                    (
                                        actual_contribution
                                        if expectation.skill_key
                                        in optional_support_skill_keys
                                        else theory_contribution
                                    )
                                    if support_theory_total > 1e-9
                                    else coverage_weight
                                )
                                / max(
                                    1e-9,
                                    support_display_weight_total
                                    if support_theory_total > 1e-9
                                    else coverage_weight_total,
                                )
                                * 100.0
                            )
                        ),
                        actual_contribution=actual_contribution,
                        theory_contribution=theory_contribution,
                        actual_intervals=actual_intervals,
                        theory_intervals=theory_intervals,
                        actual_display_intervals=actual_display_intervals,
                        theory_display_intervals=theory_display_intervals,
                        expected_duration=expectation.duration,
                        included_in_score=(
                            detail_is_scored(
                                expectation,
                                completion_ratio,
                                actual_contribution,
                                theory_contribution,
                            )
                        ),
                        actual_covered_damage_share=(
                            damage_in_intervals(actual_intervals, expectation)
                            / total_team_damage
                            if total_team_damage > 1e-9
                            else 0.0
                        ),
                        theory_covered_damage_share=(
                            theory_damage_in_intervals(
                                theory_intervals, expectation
                            )
                            / theory_total_team_damage
                            if theory_total_team_damage > 1e-9
                            else 0.0
                        ),
                        effective_damage_coverage=effective_damage_coverage(
                            expectation,
                            actual_intervals,
                            actual,
                        ),
                        support_benefit_completion=completion_ratio * 100.0,
                        effective_damage_theory_coverage=(
                            effective_damage_theory_coverage(
                                expectation,
                                theory_intervals,
                                theory,
                            )
                        ),
                        actual_direct_contribution=actual_contribution,
                        actual_linked_contribution=allocated_timing_diagnostic(
                            actual_linked_map,
                            expectation,
                            actual_contribution,
                            theory_contribution,
                            0.0,
                        ),
                        # The formal row can contain non-timing value (for
                        # example Cocktail's panel-stat contribution), while
                        # the linked diagnostic maps contain timing-only
                        # counterfactuals.  Always add the newly measured
                        # linked increment to the complete formal direct row so
                        # the displayed split is algebraically consistent.
                        actual_total_contribution=(
                            actual_contribution
                            + allocated_timing_diagnostic(
                                actual_linked_map,
                                expectation,
                                actual_contribution,
                                theory_contribution,
                                0.0,
                            )
                        ),
                        theory_direct_contribution=theory_contribution,
                        theory_linked_contribution=allocated_timing_diagnostic(
                            theory_linked_map,
                            expectation,
                            actual_contribution,
                            theory_contribution,
                            0.0,
                        ),
                        theory_total_contribution=(
                            theory_contribution
                            + allocated_timing_diagnostic(
                                theory_linked_map,
                                expectation,
                                actual_contribution,
                                theory_contribution,
                                0.0,
                            )
                        ),
                        linked_contribution_available=(
                            expectation.skill_key in actual_total_map
                            or expectation.skill_key in theory_total_map
                        ),
                        linked_contribution_included_in_score=bool(
                            replan.get("linked_contribution_included_in_score")
                        ) if isinstance(replan, dict) else False,
                        observed_coverage=observed_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (actual, actual_intervals, actual_display_intervals),
                        )[0],
                        observed_intervals=observed_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (actual, actual_intervals, actual_display_intervals),
                        )[1],
                        observed_display_intervals=observed_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (actual, actual_intervals, actual_display_intervals),
                        )[2],
                        effective_state=(
                            counterfactual_context.effective_state_by_identity.get(
                                self._support_effect_identity(
                                    player.source_id, expectation
                                ),
                                "有效" if actual_intervals else "未观察到",
                            )
                        ),
                        effective_reason=(
                            counterfactual_context.effective_reason_by_identity.get(
                                self._support_effect_identity(
                                    player.source_id, expectation
                                ),
                                "",
                            )
                        ),
                        application_coverage=application_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (None, (), ()),
                        )[0],
                        application_intervals=application_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (None, (), ()),
                        )[1],
                        application_display_intervals=application_metrics.get(
                            self._support_effect_identity(
                                player.source_id, expectation
                            ),
                            (None, (), ()),
                        )[2],
                    )
                    for (
                        expectation,
                        actual,
                        completion_ratio,
                        coverage_weight,
                        actual_contribution,
                        theory_contribution,
                        theory,
                        actual_intervals,
                        theory_intervals,
                        actual_display_intervals,
                        theory_display_intervals,
                    ) in detail_parts
                ),
                key=lambda row: (-row.score_weight, row.skill_name, row.effect_key),
            )
        )
        return coverage, contribution, details
