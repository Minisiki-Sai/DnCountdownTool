from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


class ScoreSupportAllocationMixin:
    def _support_ex_variant_skill_keys(self, job: str) -> frozenset[str]:
        """Return support skill keys whose EX node belongs to ``job``.

        Status packets normally retain the inherited/base skill name even when
        a second-job EX node owns the stronger version.  The profession skill
        table is therefore used to distinguish an EX provider from a provider
        that only owns the inherited base skill.
        """

        job_name = data_job_for(job)
        cache = getattr(self, "_support_ex_variant_skill_cache", None)
        if cache is None:
            cache = {}
            self._support_ex_variant_skill_cache = cache
        cached = cache.get(job_name)
        if cached is not None:
            return cached

        result = frozenset(
            skill_key
            for skill_name in self.store.full_skill_names(job_name)
            if re.search(r"EX\s*$", norm(skill_name), flags=re.IGNORECASE)
            and (skill_key := canonical_skill(skill_name))
        )
        cache[job_name] = result
        return result

    def _support_provider_owns_ex(
        self,
        source_job: str,
        expectation: _SupportExpectation,
    ) -> bool:
        return (
            expectation.skill_key
            in self._support_ex_variant_skill_keys(source_job)
        )

    def _backup_support_skill_keys_for_player(
        self,
        snapshot: CombatSnapshot,
        player: PlayerSnapshot,
        expectations: tuple[_SupportExpectation, ...],
    ) -> frozenset[str]:
        """Return base effects that are optional backups to a party EX owner."""

        own_ex_skills = self._support_ex_variant_skill_keys(player.job)
        result: set[str] = set()
        provider_expectations: dict[str, frozenset[tuple[str, str, str]]] = {}
        for provider in event_platform_for(snapshot).players:
            if str(provider.source_id) == str(player.source_id):
                continue
            provider_ex_skills = self._support_ex_variant_skill_keys(provider.job)
            if not provider_ex_skills:
                continue
            provider_job = data_job_for(provider.job)
            identities = provider_expectations.get(str(provider.source_id))
            if identities is None:
                identities = frozenset(
                    (row.skill_key, row.effect_key, row.kind)
                    for row in self._combat_support_expectations(snapshot, provider.source_id, provider_job)
                )
                provider_expectations[str(provider.source_id)] = identities
            for expectation in expectations:
                if expectation.skill_key in own_ex_skills:
                    continue
                identity = (
                    expectation.skill_key,
                    expectation.effect_key,
                    expectation.kind,
                )
                if (
                    expectation.skill_key in provider_ex_skills
                    and identity in identities
                ):
                    result.add(expectation.skill_key)
        return frozenset(result)

    def _active_support_indexes_with_ex_priority(
        self,
        active_indexes: tuple[int, ...],
        effects: tuple[_SupportEffectInstance, ...],
    ) -> tuple[int, ...]:
        """Prefer an active EX provider over active base copies of one effect."""

        grouped: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for effect_index in active_indexes:
            expectation = effects[effect_index].expectation
            grouped[
                (
                    expectation.skill_key,
                    expectation.effect_key,
                    expectation.kind,
                )
            ].append(effect_index)

        selected: list[int] = []
        for indexes in grouped.values():
            ex_indexes = [
                effect_index
                for effect_index in indexes
                if self._support_provider_owns_ex(
                    effects[effect_index].source_job,
                    effects[effect_index].expectation,
                )
            ]
            selected.extend(ex_indexes or indexes)
        return tuple(sorted(selected))

    @lru_cache(maxsize=4096)
    def _support_damage_skill_element(self, job: str, skill_name: str) -> str:
        """Return the exact element of one damage event when it is known.

        A profession-level damage profile is only an approximation.  Hybrid
        rotations such as output Yao contain both light and dark skills, so a
        dark-resistance reduction must be valued against the dark packets even
        though Yao's default panel profile is light.  The skill table already
        contains this information; use it before falling back to the aggregate
        profession weights.
        """

        skill_key = canonical_skill(skill_name)
        if not skill_key:
            return ""
        return norm(
            self.store.skill_damage_element(data_job_for(job), skill_key)
        )

    @lru_cache(maxsize=4096)
    def _support_effect_relevance(
        self,
        effect_key: str,
        job: str,
        skill_name: str = "",
    ) -> float:
        profile = self._job_damage_profile(job)
        if "物理" in effect_key or effect_key == "物攻%":
            return profile["physical"]
        if "魔法" in effect_key or effect_key == "魔攻%":
            return profile["magical"]
        skill_element = self._support_damage_skill_element(job, skill_name)
        if effect_key == '无属性%':
            if skill_element in {'火','水','光','暗','不适用'}:
                return 0.0
            if skill_name and self.store._skill_damage_entry(data_job_for(job), canonical_skill(skill_name)):
                return 1.0
            return 1.0 - profile.get('elemental', 0.0)
        element_names = {
            "fire": "火",
            "water": "水",
            "light": "光",
            "dark": "暗",
        }
        element_map = {
            "减火抗%": "fire",
            "减水抗%": "water",
            "减光抗%": "light",
            "减暗抗%": "dark",
            "减全抗%": "elemental",
            "火攻%": "fire",
            "水攻%": "water",
            "光攻%": "light",
            "暗攻%": "dark",
            "属性攻%": "elemental",
        }
        if effect_key == "火水攻%":
            if skill_element:
                return 1.0 if skill_element in {"火", "水"} else 0.0
            return min(1.0, profile.get("fire", 0.0) + profile.get("water", 0.0))
        profile_key = element_map.get(effect_key)
        if skill_element and profile_key == "elemental":
            return 1.0
        if skill_element and profile_key in element_names:
            return 1.0 if skill_element == element_names[profile_key] else 0.0
        return profile.get(profile_key, 1.0) if profile_key else 1.0

    @staticmethod
    @lru_cache(maxsize=512)
    def _support_effect_identity(
        source_id: str, expectation: _SupportExpectation
    ) -> tuple[str, str, str, str]:
        return (
            source_id,
            expectation.skill_key,
            expectation.effect_key,
            expectation.kind,
        )

    def _confirmed_freeze_cast_intervals(
        self,
        snapshot: CombatSnapshot,
        source_id: str,
        expectation: _SupportExpectation,
        end_seconds: float,
        raw_events: Iterable[Any] = (),
        boss_target_ids: frozenset[str] | None = None,
    ) -> tuple[tuple[float, float], ...]:
        """Rebuild freeze applications missing from the captured status stream.

        Some projectile and persistent-area skills expose both their cast and
        damage packets while intermittently omitting the corresponding status
        application.  Scoring only reconstructs a layer when the same skill is
        observed casting *and* dealing damage.  Existing status applications
        remain authoritative, and a captured remove event still shortens an
        inferred layer (for example when the ice layer shatters early).
        """
        if expectation.effect_key != "冰冻层" or expectation.duration <= 0:
            return ()

        if self._freeze_interval_snapshot is not snapshot:
            self._freeze_interval_snapshot = snapshot
            self._freeze_interval_cache.clear()
        cache_key = (
            source_id,
            expectation.skill_key,
            round(expectation.duration, 4),
            round(end_seconds, 4),
            tuple(sorted(boss_target_ids or ())),
        )
        cached = self._freeze_interval_cache.get(cache_key)
        if cached is not None:
            return cached

        event_index = event_platform_for(snapshot).event_index
        casts: list[float] = []
        for row in event_index.casts_by_source.get(source_id, ()):
            if canonical_skill(row.skill_name) != expectation.skill_key:
                continue
            seconds = row.timestamp_ms / 1000.0
            if casts and seconds - casts[-1] < 0.12:
                continue
            casts.append(seconds)
        if not casts:
            self._freeze_interval_cache[cache_key] = ()
            return ()

        hits = tuple(
                row
                for row in event_index.combat_by_source.get(source_id, ())
                if not row.healing
                and row.amount > 0
                and row.has_real_hit_evidence
                and canonical_skill(row.skill_name) == expectation.skill_key
                and (
                    not boss_target_ids
                    or row.target_id in boss_target_ids
                )
            )
        if not hits:
            self._freeze_interval_cache[cache_key] = ()
            return ()

        event_rows = sorted(
            (
                row
                for row in raw_events
                if str(getattr(row, "source_id", "")) == source_id
                and canonical_skill(getattr(row, "skill_name", ""))
                == expectation.skill_key
            ),
            key=lambda row: as_number(getattr(row, "timestamp_seconds", 0.0)),
        )
        applies = [
            row
            for row in event_rows
            if norm(getattr(row, "operation", "apply")).casefold() != "remove"
        ]
        removes = [
            row
            for row in event_rows
            if norm(getattr(row, "operation", "apply")).casefold() == "remove"
        ]
        used_applies: set[int] = set()
        inferred: list[tuple[float, float]] = []
        hit_index = 0

        for index, cast_start in enumerate(casts):
            next_cast = casts[index + 1] if index + 1 < len(casts) else math.inf
            association_end = min(
                next_cast - 0.01,
                cast_start + max(4.0, expectation.duration + 1.0),
            )
            while (
                hit_index < len(hits)
                and hits[hit_index].timestamp_ms / 1000.0 < cast_start - 0.10
            ):
                hit_index += 1
            if hit_index >= len(hits):
                break
            hit = hits[hit_index]
            hit_seconds = hit.timestamp_ms / 1000.0
            if hit_seconds > association_end:
                continue
            hit_index += 1

            captured_apply = next(
                (
                    apply_index
                    for apply_index, row in enumerate(applies)
                    if apply_index not in used_applies
                    and cast_start - 0.25
                    <= as_number(getattr(row, "timestamp_seconds", 0.0))
                    <= association_end + 0.25
                ),
                None,
            )
            if captured_apply is not None:
                used_applies.add(captured_apply)
                continue

            inferred_end = min(end_seconds, hit_seconds + expectation.duration)
            captured_remove = next(
                (
                    as_number(getattr(row, "timestamp_seconds", inferred_end))
                    for row in removes
                    if hit_seconds
                    < as_number(getattr(row, "timestamp_seconds", 0.0))
                    < inferred_end
                ),
                None,
            )
            if captured_remove is not None:
                inferred_end = captured_remove
            if inferred_end > hit_seconds + 1e-9:
                inferred.append((hit_seconds, inferred_end))

        result = _merge_intervals(inferred)
        self._freeze_interval_cache[cache_key] = result
        return result

    def _theory_freeze_intervals_from_actual_hits(
        self,
        snapshot: CombatSnapshot,
        source_job: str,
        expectation: _SupportExpectation,
        applications: Iterable[tuple[float, float]],
        output_windows: tuple[tuple[float, float], ...],
        boss_target_ids: frozenset[str] | None,
    ) -> tuple[tuple[float, float], ...]:
        """Apply factual Boss-scoped team-Hit density to theory freeze layers."""

        rows = tuple(applications)
        if expectation.effect_key != "冰冻层" or not rows:
            return _merge_intervals(rows)
        if self._theory_freeze_hit_snapshot is not snapshot:
            self._theory_freeze_hit_snapshot = snapshot
            self._theory_freeze_hit_cache.clear()
        target_key = tuple(sorted(boss_target_ids or ()))
        window_key = tuple(
            (round(start, 4), round(end, 4))
            for start, end in _merge_intervals(output_windows)
        )
        cache_key = (target_key, window_key)
        hit_timestamps = self._theory_freeze_hit_cache.get(cache_key)
        if hit_timestamps is None:
            hit_timestamps = tuple(
                sorted(
                    event.timestamp_ms / 1000.0
                    for event in event_platform_for(
                        snapshot
                    ).event_index.damage_events
                    if (not boss_target_ids or event.target_id in boss_target_ids)
                    and event.has_real_hit_evidence
                    and _in_intervals(
                        event.timestamp_ms / 1000.0,
                        output_windows,
                    )
                )
            )
            self._theory_freeze_hit_cache[cache_key] = hit_timestamps
        data_job = data_job_for(source_job)
        refresh_rule = _REFRESHING_FREEZE_RULES.get(expectation.skill_key)
        if refresh_rule is not None:
            rows = refreshing_freeze_applications(
                rows,
                application_duration=(
                    refresh_rule[0] or expectation.duration
                ),
                refresh_interval=refresh_rule[1],
            )
        hit_limit = (
            ALCHEMIST_FREEZE_BREAK_HITS
            if data_job == "炼金圣士"
            and expectation.skill_key in {"寒冰戳刺", "冰光束"}
            else DEFAULT_FREEZE_BREAK_HITS
        )
        return hit_limited_freeze_intervals(rows, hit_timestamps, hit_limit)

    @staticmethod
    @lru_cache(maxsize=512)
    def _support_effect_group(
        expectation: _SupportExpectation,
    ) -> tuple[str, str]:
        if expectation.effect_key == "冰冻层":
            return ("freeze", "冰冻层")
        if expectation.kind == "buff":
            return (
                "buff",
                expectation.stack_group or expectation.skill_key,
            )
        probe = EffectInput(
            key=expectation.effect_key,
            value=expectation.magnitude,
            skill_name=expectation.skill_name,
            stack_group=expectation.stack_group,
            stackable=expectation.stackable,
        )
        resolved_rule = effect_rule(probe)
        if expectation.stackable:
            # Stackable resistance effects remain independent from the normal
            # highest-value resistance group, while duplicate casts of the
            # same stackable status still share one contribution.
            return (
                "stackable-debuff",
                resolved_rule.stack_group,
            )
        return (
            "debuff",
            resolved_rule.stack_group,
        )

    def _reduction_channel_allocation(
        self,
        active_rows: list[_SupportEffectInstance],
        *,
        gain_for_total: Callable[[float], float],
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        """Resolve client groups once, then allocate one channel's real gain."""

        grouped: dict[
            tuple[str, str], list[tuple[_SupportEffectInstance, float]]
        ] = defaultdict(list)
        additive: list[tuple[_SupportEffectInstance, float]] = []
        for instance in active_rows:
            expectation = instance.expectation
            magnitude = max(0.0, expectation.magnitude)
            if magnitude <= 1e-12:
                continue
            rule = effect_rule(
                EffectInput(
                    key=expectation.effect_key,
                    value=magnitude,
                    skill_name=expectation.skill_name,
                    source_id=instance.source_id,
                    stack_group=expectation.stack_group,
                    stackable=expectation.stackable,
                )
            )
            if rule.group_mode == "additive":
                additive.append((instance, magnitude))
            else:
                grouped[(rule.key, rule.stack_group)].append(
                    (instance, magnitude)
                )

        shares: list[tuple[_SupportEffectInstance, float]] = list(additive)
        for rows in grouped.values():
            group_shares = self._highest_effect_shares(
                [magnitude for _instance, magnitude in rows]
            )
            shares.extend(
                (instance, share)
                for (instance, _magnitude), share in zip(
                    rows, group_shares, strict=True
                )
                if share > 1e-12
            )
        total_reduction = sum(share for _instance, share in shares)
        full_gain = gain_for_total(total_reduction)
        if total_reduction <= 1e-12 or full_gain <= 1e-12:
            return 0.0, []
        return full_gain, [
            (instance, full_gain * share / total_reduction)
            for instance, share in shares
        ]

    def _element_resistance_reduction_allocation(
        self,
        active_rows: list[_SupportEffectInstance],
        recipient_job: str,
        recipient_skill: str,
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        relevant = [
            instance
            for instance in active_rows
            if self._support_effect_relevance(
                instance.expectation.effect_key,
                recipient_job,
                recipient_skill,
            )
            > 1e-12
        ]
        return self._reduction_channel_allocation(
            relevant,
            gain_for_total=self._boss_element_resistance_reduction_gain,
        )

    def _critical_resistance_reduction_allocation(
        self,
        active_rows: list[_SupportEffectInstance],
        recipient_source_id: str,
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        return self._reduction_channel_allocation(
            active_rows,
            gain_for_total=lambda total: self._boss_crit_resist_reduction_gain_for_recipient(
                total, recipient_source_id
            ),
        )

    @staticmethod
    def _maximum_effect_shares(gains: list[float]) -> list[float]:
        """Shapley values for a highest-value-wins effect group."""
        shares = [0.0 for _ in gains]
        previous = 0.0
        for level in sorted({value for value in gains if value > 1e-12}):
            eligible = [index for index, value in enumerate(gains) if value >= level]
            if not eligible:
                continue
            increment = max(0.0, level - previous) / len(eligible)
            for index in eligible:
                shares[index] += increment
            previous = level
        return shares

    @staticmethod
    def _highest_effect_shares(gains: list[float]) -> list[float]:
        """Credit only the currently effective highest tier.

        Lower layers are observable fallbacks, not simultaneous contributors.
        Equal highest providers split the one effective value evenly.
        """

        values = [max(0.0, float(value)) for value in gains]
        maximum = max(values, default=0.0)
        if maximum <= 1e-12:
            return [0.0 for _value in values]
        winners = [
            index
            for index, value in enumerate(values)
            if math.isclose(value, maximum, rel_tol=1e-9, abs_tol=1e-12)
        ]
        share = maximum / len(winners)
        return [share if index in winners else 0.0 for index in range(len(values))]

    @staticmethod
    def _maximum_effect_tier_shares(gains: list[float]) -> list[float]:
        """Allocate a highest-value group as a descending fallback waterfall.

        Each distinct magnitude owns only the increase above the next lower
        tier. Duplicate effects at one tier split that tier evenly. The shares
        therefore sum to the active maximum while retaining credit for a lower
        fallback effect that is currently covered by a stronger one.
        """

        values = [max(0.0, float(value)) for value in gains]
        shares = [0.0 for _value in values]
        previous = 0.0
        for level in sorted({value for value in values if value > 1e-12}):
            owners = [
                index
                for index, value in enumerate(values)
                if math.isclose(value, level, rel_tol=1e-9, abs_tol=1e-12)
            ]
            if not owners:
                continue
            increment = max(0.0, level - previous) / len(owners)
            for index in owners:
                shares[index] += increment
            previous = level
        return shares

    @staticmethod
    def _damage_amplification_subgroup(
        instance: _SupportEffectInstance,
    ) -> tuple[str, str]:
        """Group duplicate or explicitly exclusive amplification effects.

        Different amplification skills add to the same physical/magical cap.
        Repeated copies of one skill do not stack, and an explicit stack group
        (for example the two ranger physical-amplification skills) remains a
        highest-value-wins subgroup.
        """
        expectation = instance.expectation
        if expectation.stackable:
            return (
                "stackable",
                expectation.stack_group
                or f"{instance.source_id}:{expectation.skill_key}",
            )
        return (
            "exclusive",
            expectation.stack_group or expectation.skill_key,
        )

    def _damage_amplification_allocation(
        self,
        active_rows: list[tuple[_SupportEffectInstance, float]],
        recipient_job: str,
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        """Apply the shared 100% physical/magical amplification cap.

        Generic amplification adds to both channels. Physical and magical
        amplification add only to their matching channel. A mixed profession
        is valued using its configured physical/magical damage profile.

        Credits use a budget-balanced Shapley allocation after the cap. This
        keeps the sum of all skill credits equal to the pool's real removal
        loss even when several individually redundant effects exceed 100%.
        Duplicate providers inside one non-stacking subgroup then split that
        subgroup's allocation with the existing highest-value Shapley rule.
        """
        if not active_rows:
            return 0.0, []

        grouped: dict[
            tuple[str, str], list[tuple[_SupportEffectInstance, float]]
        ] = defaultdict(list)
        for instance, gain in active_rows:
            if gain > 1e-12:
                grouped[self._damage_amplification_subgroup(instance)].append(
                    (instance, gain)
                )
        if not grouped:
            return 0.0, []

        subgroup_rows: list[
            tuple[
                str,
                float,
                list[tuple[_SupportEffectInstance, float]],
            ]
        ] = []
        for rows in grouped.values():
            gains = [gain for _instance, gain in rows]
            duplicate_shares = self._highest_effect_shares(gains)
            effective_gain = sum(duplicate_shares)
            if effective_gain <= 1e-12:
                continue
            effect_key = rows[0][0].expectation.effect_key
            subgroup_rows.append(
                (
                    effect_key,
                    effective_gain,
                    [
                        (instance, share)
                        for (instance, _gain), share in zip(
                            rows, duplicate_shares, strict=True
                        )
                        if share > 1e-12
                    ],
                )
            )
        if not subgroup_rows:
            return 0.0, []

        profile = self._job_damage_profile(recipient_job)
        physical_weight = max(0.0, profile.get("physical", 0.0))
        magical_weight = max(0.0, profile.get("magical", 0.0))
        attack_weight = physical_weight + magical_weight
        if attack_weight <= 1e-12:
            physical_weight = magical_weight = 0.5
        else:
            physical_weight /= attack_weight
            magical_weight /= attack_weight

        physical_raw = sum(
            gain
            for effect_key, gain, _rows in subgroup_rows
            if effect_key in {"增伤%", "物理增伤%"}
        )
        magical_raw = sum(
            gain
            for effect_key, gain, _rows in subgroup_rows
            if effect_key in {"增伤%", "魔法增伤%"}
        )
        physical_gain = min(1.0, physical_raw)
        magical_gain = min(1.0, magical_raw)
        full_gain = (
            physical_weight * physical_gain
            + magical_weight * magical_gain
        )
        if full_gain <= 1e-12:
            return 0.0, []

        subgroup_allocations = [0.0 for _row in subgroup_rows]
        for channel_weight, accepted_keys in (
            (physical_weight, {"增伤%", "物理增伤%"}),
            (magical_weight, {"增伤%", "魔法增伤%"}),
        ):
            if channel_weight <= 1e-12:
                continue
            indexes = [
                index
                for index, (effect_key, _gain, _rows) in enumerate(subgroup_rows)
                if effect_key in accepted_keys
            ]
            channel_gains = tuple(
                round(subgroup_rows[index][1], 10) for index in indexes
            )
            channel_shares = _capped_additive_shapley(channel_gains)
            for index, share in zip(indexes, channel_shares, strict=True):
                subgroup_allocations[index] += channel_weight * share

        allocated_total = sum(subgroup_allocations)
        if allocated_total > 1e-12:
            subgroup_allocations = [
                share * full_gain / allocated_total
                for share in subgroup_allocations
            ]

        allocations: list[tuple[_SupportEffectInstance, float]] = []
        for (_effect_key, effective_gain, rows), subgroup_share in zip(
            subgroup_rows, subgroup_allocations, strict=True
        ):
            if subgroup_share <= 1e-12:
                continue
            for instance, duplicate_share in rows:
                allocations.append(
                    (
                        instance,
                        subgroup_share
                        * duplicate_share
                        / max(1e-12, effective_gain),
                    )
                )
        return full_gain, allocations

    @staticmethod
    def _defense_reduction_subgroup(
        instance: _SupportEffectInstance,
    ) -> tuple[str, str]:
        expectation = instance.expectation
        rule = effect_rule(
            EffectInput(
                key=expectation.effect_key,
                value=expectation.magnitude,
                skill_name=expectation.skill_name,
                source_id=instance.source_id,
                stack_group=expectation.stack_group,
                stackable=expectation.stackable,
            )
        )
        return rule.key, rule.stack_group

    def _defense_reduction_allocation(
        self,
        active_rows: list[_SupportEffectInstance],
        recipient_job: str,
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        """Allocate stackable defense reduction up to zero remaining defense."""

        if not active_rows:
            return 0.0, []
        grouped: dict[tuple[str, str], list[_SupportEffectInstance]] = defaultdict(list)
        for instance in active_rows:
            if max(0.0, instance.expectation.magnitude) > 1e-12:
                grouped[self._defense_reduction_subgroup(instance)].append(instance)
        if not grouped:
            return 0.0, []

        subgroup_rows: list[
            tuple[str, float, list[tuple[_SupportEffectInstance, float]]]
        ] = []
        for rows in grouped.values():
            magnitudes = [max(0.0, row.expectation.magnitude) for row in rows]
            duplicate_shares = self._highest_effect_shares(magnitudes)
            effective_magnitude = sum(duplicate_shares)
            if effective_magnitude <= 1e-12:
                continue
            subgroup_rows.append(
                (
                    rows[0].expectation.effect_key,
                    effective_magnitude,
                    [
                        (instance, share)
                        for instance, share in zip(rows, duplicate_shares, strict=True)
                        if share > 1e-12
                    ],
                )
            )
        if not subgroup_rows:
            return 0.0, []

        profile = self._job_damage_profile(recipient_job)
        physical_weight = max(0.0, profile.get("physical", 0.0))
        magical_weight = max(0.0, profile.get("magical", 0.0))
        total_weight = physical_weight + magical_weight
        if total_weight <= 1e-12:
            physical_weight = magical_weight = 0.5
        else:
            physical_weight /= total_weight
            magical_weight /= total_weight

        subgroup_allocations = [0.0 for _row in subgroup_rows]
        full_gain = 0.0
        for channel_weight, accepted_keys in (
            (physical_weight, {"减防御%", "减物理防御%"}),
            (magical_weight, {"减防御%", "减魔法防御%"}),
        ):
            if channel_weight <= 1e-12:
                continue
            indexes = [
                index
                for index, (effect_key, _magnitude, _rows) in enumerate(subgroup_rows)
                if effect_key in accepted_keys
            ]
            magnitudes = tuple(
                round(subgroup_rows[index][1], 10) for index in indexes
            )
            total_reduction = min(
                DEFENSE_REDUCTION_CAP_PERCENT, sum(magnitudes)
            )
            if total_reduction <= 1e-12:
                continue
            channel_gain = self._boss_defense_reduction_gain(total_reduction)
            full_gain += channel_weight * channel_gain
            reduction_shares = _capped_additive_shapley(
                magnitudes, DEFENSE_REDUCTION_CAP_PERCENT
            )
            for index, reduction_share in zip(
                indexes, reduction_shares, strict=True
            ):
                subgroup_allocations[index] += (
                    channel_weight
                    * channel_gain
                    * reduction_share
                    / total_reduction
                )

        allocated_total = sum(subgroup_allocations)
        if full_gain <= 1e-12 or allocated_total <= 1e-12:
            return 0.0, []
        subgroup_allocations = [
            value * full_gain / allocated_total for value in subgroup_allocations
        ]
        allocations: list[tuple[_SupportEffectInstance, float]] = []
        for (_key, effective_magnitude, rows), subgroup_share in zip(
            subgroup_rows, subgroup_allocations, strict=True
        ):
            if subgroup_share <= 1e-12:
                continue
            for instance, duplicate_share in rows:
                allocations.append(
                    (
                        instance,
                        subgroup_share
                        * duplicate_share
                        / max(1e-12, effective_magnitude),
                    )
                )
        return full_gain, allocations

    @staticmethod
    def _freeze_effect_shares(
        active_rows: list[tuple[_SupportEffectInstance, float]],
    ) -> tuple[float, list[float]]:
        """Value Ice Witch freeze as one profession effect, then split by skill.

        Treating every freeze skill as an independent provider lets Alchemist
        or another overlapping layer dilute each Ice Witch skill before the
        profession's two-layer value is measured.  The Ice Witch rows are
        therefore collapsed into one counterfactual participant first.  Its
        Shapley value is the average of adding the profession before and after
        all other freeze providers.  That profession-level value is then
        attributed to the active skills by their effective layer strength.
        """

        def capped_gain(
            rows: list[tuple[_SupportEffectInstance, float]],
        ) -> float:
            if not rows:
                return 0.0
            gains = [gain for _instance, gain in rows]
            capacity = max(
                _freeze_max_layers(instance.source_job)
                for instance, _gain in rows
            )
            return min(
                sum(gains),
                max(gains, default=0.0) * max(1, capacity),
            )

        full_gain = capped_gain(active_rows)
        if full_gain <= 1e-12:
            return 0.0, [0.0 for _row in active_rows]

        ice_indexes = [
            index
            for index, (instance, _gain) in enumerate(active_rows)
            if data_job_for(instance.source_job) == "冰灵"
        ]
        if not ice_indexes:
            gain_total = sum(gain for _instance, gain in active_rows)
            return full_gain, [
                full_gain * gain / max(1e-12, gain_total)
                for _instance, gain in active_rows
            ]

        ice_index_set = set(ice_indexes)
        ice_rows = [active_rows[index] for index in ice_indexes]
        other_rows = [
            row for index, row in enumerate(active_rows) if index not in ice_index_set
        ]
        ice_only_gain = capped_gain(ice_rows)
        other_only_gain = capped_gain(other_rows)
        # Two-participant Shapley allocation: the entire Ice Witch freeze kit
        # is one participant and all external freeze sources are the other.
        ice_group_share = 0.5 * (
            ice_only_gain + max(0.0, full_gain - other_only_gain)
        )
        ice_group_share = max(0.0, min(full_gain, ice_group_share))
        other_group_share = max(0.0, full_gain - ice_group_share)

        shares = [0.0 for _row in active_rows]
        ice_weight = sum(active_rows[index][1] for index in ice_indexes)
        for index in ice_indexes:
            shares[index] = (
                ice_group_share
                * active_rows[index][1]
                / max(1e-12, ice_weight)
            )
        other_indexes = [
            index for index in range(len(active_rows)) if index not in ice_index_set
        ]
        other_weight = sum(active_rows[index][1] for index in other_indexes)
        for index in other_indexes:
            shares[index] = (
                other_group_share
                * active_rows[index][1]
                / max(1e-12, other_weight)
            )
        return full_gain, shares

    @staticmethod
    def _theory_team_damage_events(
        players: tuple[PlayerSnapshot, ...],
        theory_plans: dict[str, dict[str, Any]],
        output_windows: tuple[tuple[float, float], ...],
    ) -> tuple[CombatEvent, ...]:
        """Expand planned team damage into a small time-weighted event stream.

        Theory plans store one total score per cast. Freeze needs to know when
        that score lands, so normal casts are spread across their action span
        and periodic summons across their scored lifetime. A 250ms slice is
        precise enough for short freeze windows without turning a long Boss
        score into a per-frame simulation.
        """

        if not theory_plans or not output_windows:
            return ()
        result: list[CombatEvent] = []
        slice_seconds = 0.25
        for player in players:
            plan = theory_plans.get(player.source_id, {})
            if not isinstance(plan, dict):
                continue
            time_offset = max(0.0, as_number(plan.get("_score_time_offset")))
            damage_scale = (
                max(0.0, as_number(plan.get("damage_panel")))
                * max(0.0, as_number(plan.get("hand_multiplier"), 1.0))
                / 100.0
            )
            if damage_scale <= 1e-12:
                damage_scale = max(
                    1.0,
                    as_number(plan.get("_score_default_damage_per_percent"), 1.0),
                )
            for segment_index, segment in enumerate(plan.get("segments", ())):
                if not isinstance(segment, dict):
                    continue
                score = max(0.0, as_number(segment.get("effective_score")))
                if score <= 1e-12:
                    continue
                cast_start = time_offset + max(
                    0.0, as_number(segment.get("start"))
                )
                cast_end = time_offset + max(
                    as_number(segment.get("start")),
                    as_number(segment.get("end"), segment.get("start")),
                )
                periodic_duration = (
                    max(0.0, as_number(segment.get("dpa_basis_seconds")))
                    if isinstance(segment.get("periodic_metric"), dict)
                    else 0.0
                )
                if periodic_duration > 1e-9:
                    damage_start = cast_end
                    damage_end = cast_end + periodic_duration
                else:
                    damage_start = cast_start
                    damage_end = max(cast_start, cast_end)
                clipped = _intersect_intervals(
                    ((damage_start, damage_end),), output_windows
                )
                skill_name = norm(segment.get("skill")) or "理论伤害"
                total_amount = score * damage_scale
                if damage_end <= damage_start + 1e-9:
                    if _in_intervals(damage_end, output_windows):
                        result.append(
                            CombatEvent(
                                timestamp_ms=max(0, int(round(damage_end * 1000.0))),
                                source_id=player.source_id,
                                source_name=player.name,
                                job=player.job,
                                skill_id=f"theory:{segment_index}",
                                skill_name=skill_name,
                                amount=total_amount,
                                target_id="theory-boss",
                                target_name="理论Boss",
                                attribution_method="theory_timeline",
                            )
                        )
                    continue
                clipped_duration = _interval_duration(clipped)
                if clipped_duration <= 1e-9:
                    continue
                for interval_start, interval_end in clipped:
                    cursor = interval_start
                    while cursor < interval_end - 1e-9:
                        slice_end = min(interval_end, cursor + slice_seconds)
                        amount = total_amount * (
                            (slice_end - cursor) / clipped_duration
                        )
                        result.append(
                            CombatEvent(
                                timestamp_ms=max(
                                    0,
                                    int(round((cursor + slice_end) * 500.0)),
                                ),
                                source_id=player.source_id,
                                source_name=player.name,
                                job=player.job,
                                skill_id=f"theory:{segment_index}",
                                skill_name=skill_name,
                                amount=amount,
                                target_id="theory-boss",
                                target_name="理论Boss",
                                attribution_method="theory_timeline",
                            )
                        )
                        cursor = slice_end
        return tuple(sorted(result, key=lambda row: row.timestamp_ms))

    def _freeze_damage_weighted_contributions(
        self,
        damage_events: tuple[CombatEvent, ...],
        total_damage: float,
        effects: tuple[_SupportEffectInstance, ...],
        *,
        replacement_source_id: str = "",
        replacement_intervals: dict[
            tuple[str, str, str], tuple[tuple[float, float], ...]
        ]
        | None = None,
    ) -> tuple[dict[str, float], dict[tuple[str, str, str, str], float]]:
        """Integrate exact 0/1/2-layer freeze states over team damage events."""

        if total_damage <= 1e-9 or not damage_events or not effects:
            return {}, {}
        replacements = replacement_intervals or {}
        gain_per_layer = _freeze_gain_per_layer() / 100.0
        source_damage: dict[str, float] = defaultdict(float)
        detail_damage: dict[tuple[str, str, str, str], float] = defaultdict(float)
        for event in damage_events:
            seconds = event.timestamp_ms / 1000.0
            active_rows: list[tuple[_SupportEffectInstance, float]] = []
            for instance in effects:
                expectation = instance.expectation
                if expectation.effect_key != "冰冻层":
                    continue
                local_identity = (
                    expectation.skill_key,
                    expectation.effect_key,
                    expectation.kind,
                )
                intervals = (
                    replacements.get(local_identity, ())
                    if instance.source_id == replacement_source_id
                    and local_identity in replacements
                    else instance.intervals
                )
                if _in_intervals(seconds, intervals):
                    active_rows.append((instance, gain_per_layer))
            if not active_rows:
                continue
            group_gain, shares = self._freeze_effect_shares(active_rows)
            if group_gain <= 1e-12:
                continue
            # The event amount is the damage with the current layer state. The
            # leave-freeze-out loss is therefore L/(1+L) of that amount.
            group_bonus = max(0.0, as_number(event.amount)) * (
                1.0 - 1.0 / (1.0 + group_gain)
            )
            for (instance, _gain), share in zip(
                active_rows, shares, strict=True
            ):
                if share <= 1e-12:
                    continue
                credit = group_bonus * share / group_gain
                identity = self._support_effect_identity(
                    instance.source_id, instance.expectation
                )
                source_damage[instance.source_id] += credit
                detail_damage[identity] += credit
        scale = 100.0 / total_damage
        return (
            {source_id: value * scale for source_id, value in source_damage.items()},
            {identity: value * scale for identity, value in detail_damage.items()},
        )
