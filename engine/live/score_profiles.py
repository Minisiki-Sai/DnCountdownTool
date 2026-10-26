from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for

class ScoreProfilesMixin:
    def _combat_level_report(self, snapshot, source_id):
        from .combat_skill_levels import infer_combat_skill_levels
        if getattr(self, '_combat_levels_snapshot', None) is not snapshot:
            self._combat_levels_by_source = infer_combat_skill_levels(snapshot, self.store)
            self._combat_levels_snapshot = snapshot
        return self._combat_levels_by_source.get(str(source_id), {})

    def _combat_learned_skills(self, snapshot, source_id, job):
        report = self._combat_level_report(snapshot, source_id)
        if not report:
            return None
        return {**self._learned_skills(data_job_for(job)), **report['levels']}

    def _combat_support_expectations(self, snapshot, source_id, job):
        learned = self._combat_learned_skills(snapshot, source_id, job)
        if learned is None:
            return self._support_expectations(job)
        return self._support_expectations(
            job, learned_override=learned
        )

    def _learned_skills(self, job: str) -> dict[str, int]:
        job_name = norm(job)
        cached = self._learned_cache.get(job_name)
        if cached is not None:
            return cached
        payload = _builtin_skill_builds().get(job_name, {})
        normal = (
            payload.get("skill_build", {}).get("levels", {})
            if isinstance(payload, dict)
            else {}
        )
        mastery = (
            payload.get("skill_mastery_build", {}).get("levels", {})
            if isinstance(payload, dict)
            else {}
        )
        tree = self.store.skill_build_tree(job_name, 70)
        result: dict[str, int] = {}
        for skill_id, item in tree.get("skills", {}).items():
            level = int(as_number(normal.get(str(skill_id))))
            if level <= 0:
                continue
            level += int(as_number(mastery.get(str(skill_id))))
            name = norm(item.get("name"))
            if name:
                result[canonical_skill(name)] = max(result.get(canonical_skill(name), 0), level)
        self._learned_cache[job_name] = result
        return result
    def _job_profiles(self, job: str) -> tuple[SkillProfile, ...]:
        job_name = norm(job)
        cached = self._profile_cache.get(job_name)
        if cached is not None:
            return cached
        rows = self._apply_best_glyphs(
            job_name, list(self._job_base_profiles(job_name))
        )
        self._profile_cache[job_name] = tuple(rows)
        return self._profile_cache[job_name]
    def _profiles_with_observed_cast_skills(
        self,
        profiles: tuple[SkillProfile, ...],
        *,
        score_snapshot: CombatSnapshot,
        source_id: str,
        job: str,
        output_windows: tuple[tuple[float, float], ...],
        learned_override: dict[str, int] | None = None,
    ) -> tuple[SkillProfile, ...]:
        """Restore valid job skills observed on the captured cast timeline.

        The standardized default build remains the theory baseline.  An
        observed skill can nevertheless be absent from it because the skill
        was not learned by that build, or because an inherited base skill was
        omitted from the curated damage allowlist.  Such skills still need an
        actual-side value and an output-detail row.  Learned skills use their
        configured level; otherwise the highest level available at level 70
        is used, while the theory planner is intentionally left unchanged.
        """

        existing_keys = {profile.key for profile in profiles}
        # An explicit combat-evidence zero is a prohibition, not a missing
        # default. Never restore it at highest level from the same weak Hit.
        disabled = {key for key, level in (learned_override or {}).items() if level <= 0}
        level_aliases = {
            canonical_skill(skill): canonical_skill(source_skill)
            for skill, source_skill in JOB_MECHANICS.job_value(job, "level_aliases", {}).items()
        }
        disabled.update(key for key, base in level_aliases.items() if base in disabled)
        disabled.update(canonical_skill(key) for key, base in _TRANSFORMED_BASIC_ATTACK_BASE_SKILLS.items()
                        if canonical_skill(base) in disabled)
        profiles = tuple(profile for profile in profiles if profile.key not in disabled)
        event_index = event_platform_for(score_snapshot).event_index
        observed_cast_keys = {
            key
            for event in event_index.casts_by_source.get(source_id, ())
            if _in_intervals(event.timestamp_ms / 1000.0, output_windows)
            if (key := canonical_skill(event.skill_name))
            and key not in existing_keys and key not in disabled
        }
        observed_damage_keys = {
            key
            for event in event_index.combat_by_source.get(source_id, ())
            if not event.healing
            and event.amount > 0
            and _in_intervals(event.timestamp_ms / 1000.0, output_windows)
            if (key := canonical_skill(event.skill_name))
            and key not in existing_keys and key not in disabled
        }
        observed_keys = observed_cast_keys | observed_damage_keys
        if not observed_keys:
            return profiles

        job_name = norm(job)
        raw_data = self.store.skill_damage_job_data(job_name)
        if not isinstance(raw_data, dict):
            return profiles
        learned = learned_override if learned_override is not None else self._learned_skills(job_name)
        level_aliases = {
            canonical_skill(skill): canonical_skill(source_skill)
            for skill, source_skill in JOB_MECHANICS.job_value(
                job_name, "level_aliases", {}
            ).items()
            if canonical_skill(skill) and canonical_skill(source_skill)
        }
        additions: list[SkillProfile] = []

        # These names are created by the live parser only after a verified
        # form-changing buff is active.  They are not independent entries in
        # the game's normal skill table, so derive an actual-side profile from
        # the base skill description and the profession-specific multiplier.
        # They are intentionally not inserted into the fallback theory axis;
        # the real action evidence is what establishes their actual count.
        for attack_name, base_name in _TRANSFORMED_BASIC_ATTACK_BASE_SKILLS.items():
            attack_key = canonical_skill(attack_name)
            if attack_key not in observed_keys:
                continue
            base_profile = next(
                (profile for profile in profiles if profile.key == canonical_skill(base_name)),
                None,
            )
            if base_profile is None:
                # A transformed attack can be observed even when the default
                # build did not learn the underlying form skill.  In that
                # case use the same highest-available-level fallback as the
                # ordinary observed-skill path.
                for group in ("n", "m"):
                    for item in raw_data.get(group, []):
                        if not isinstance(item, list) or len(item) < 3:
                            continue
                        if canonical_skill(item[1]) != canonical_skill(base_name):
                            continue
                        base_profile = self._skill_profile_from_damage_entry(
                            job_name,
                            {
                                "id": item[0],
                                "name": norm(item[1]),
                                "levels": item[2],
                            },
                            level_cap=None,
                            multiplier_level=None,
                        )
                        break
                    if base_profile is not None:
                        break
            if base_profile is None:
                continue
            base_multiplier = max(
                1e-9,
                self.store.skill_damage_multiplier(job_name, base_name),
            )
            attack_multiplier = self.store.skill_damage_multiplier(
                job_name, attack_name,
            )
            timing = self.store.skill_cast_time(attack_name, [job_name])
            cast_time = max(
                0.12,
                as_number(
                    timing.get("corrected_cast_time"),
                    as_number(timing.get("cast_time"), base_profile.cast_time),
                ),
            )
            additions.append(
                SkillProfile(
                    name=attack_name,
                    key=attack_key,
                    power=(base_profile.power / base_multiplier) * attack_multiplier,
                    cooldown=0.0,
                    cast_time=cast_time,
                )
            )
            existing_keys.add(attack_key)
            observed_keys.remove(attack_key)

        for group in ("n", "m"):
            for item in raw_data.get(group, []):
                if not isinstance(item, list) or len(item) < 3:
                    continue
                name = norm(item[1])
                key = canonical_skill(name)
                if key not in observed_keys:
                    continue
                learned_key = level_aliases.get(key, key)
                learned_level = learned.get(learned_key, 0)
                profile = self._skill_profile_from_damage_entry(
                    job_name,
                    {
                        "id": item[0],
                        "name": name,
                        "levels": item[2],
                    },
                    level_cap=(learned_level or None),
                    multiplier_level=(learned_level or None),
                )
                if profile is None:
                    continue
                additions.append(profile)
                existing_keys.add(key)
                observed_keys.remove(key)
                if not observed_keys:
                    break
            if not observed_keys:
                break
        return profiles + tuple(additions)
    @staticmethod
    def _profiles_with_standardized_skill_powers(
        profiles: tuple[SkillProfile, ...],
        theory_plan: dict[str, Any] | None,
    ) -> tuple[SkillProfile, ...]:
        """Use the planner's default-build expected damage per skill.

        Older cached plans only contain percentage-equivalent powers. They
        remain supported, while current plans provide real expected damage
        with physical/magical channels, element attack and boss parameters
        already resolved on one common scale.
        """

        if not isinstance(theory_plan, dict):
            return profiles
        raw_damages = theory_plan.get("_score_skill_damages", {})
        uses_actual_damage = isinstance(raw_damages, dict) and bool(raw_damages)
        raw_values = (
            raw_damages
            if uses_actual_damage
            else theory_plan.get("_score_skill_powers", {})
        )
        if not isinstance(raw_values, dict) or not raw_values:
            return profiles
        values = {
            canonical_skill(skill): max(0.0, as_number(value))
            for skill, value in raw_values.items()
            if canonical_skill(skill) and as_number(value) > 0
        }
        if not values:
            return profiles
        fallback_damage_per_percent = max(
            0.0,
            as_number(theory_plan.get("_score_default_damage_per_percent")),
        )
        return tuple(
            SkillProfile(
                profile.name,
                profile.key,
                values.get(
                    profile.key,
                    (
                        profile.power * fallback_damage_per_percent
                        if uses_actual_damage and fallback_damage_per_percent > 0
                        else profile.power
                    ),
                ),
                profile.cooldown,
                profile.cast_time,
                profile.glyph_effect,
                profile.glyph_value,
            )
            for profile in profiles
        )
    def _job_base_profiles(self, job: str, learned_override: dict[str, int] | None = None) -> tuple[SkillProfile, ...]:
        job_name = norm(job)
        cache_key = (job_name, tuple(sorted(learned_override.items()))) if learned_override is not None else job_name
        cached = self._base_profile_cache.get(cache_key)
        if cached is not None:
            return cached
        learned = learned_override if learned_override is not None else self._learned_skills(job_name)
        level_aliases = {
            canonical_skill(skill): canonical_skill(source_skill)
            for skill, source_skill in JOB_MECHANICS.job_value(
                job_name, "level_aliases", {}
            ).items()
            if canonical_skill(skill) and canonical_skill(source_skill)
        }
        rows: list[SkillProfile] = []
        for entry in self.store._skill_damage_entries(job_name):
            name = norm(entry.get("name"))
            key = canonical_skill(name)
            learned_key = level_aliases.get(key, key)
            if entry.get("required_form"):
                learned_key = canonical_skill(entry["required_form"])
            learned_level = learned.get(learned_key, 0)
            if learned_level <= 0:
                continue
            profile = self._skill_profile_from_damage_entry(
                job_name,
                entry,
                level_cap=learned_level,
                multiplier_level=learned_level,
            )
            if profile is not None:
                rows.append(profile)
        self._base_profile_cache[cache_key] = tuple(rows)
        while len(self._base_profile_cache) > 128:
            self._base_profile_cache.pop(next(iter(self._base_profile_cache)))
        return self._base_profile_cache[cache_key]
    def _skill_profile_from_damage_entry(
        self,
        job: str,
        entry: dict[str, Any],
        *,
        level_cap: int | None,
        multiplier_level: int | None,
    ) -> SkillProfile | None:
        name = norm(entry.get("name"))
        key = canonical_skill(name)
        level_rows = [
            row for row in entry.get("levels", []) if isinstance(row, list)
        ]
        selected = max(
            (
                row
                for row in level_rows
                if (level_cap is None or int(as_number(row[0])) <= level_cap)
                and (as_number(row[1], 9999) <= 70 or int(as_number(row[0])) == level_cap)
            ),
            key=lambda row: as_number(row[0]),
            default=None,
        )
        if selected is None:
            return None
        selected_level = int(as_number(selected[0]))
        power = self._level_power(selected)
        power *= self.store.skill_damage_multiplier(
            job,
            name,
            multiplier_level or selected_level,
        )
        if power <= 0:
            return None
        cooldown = self.store.skill_reference_cooldown(
            job,
            name,
            as_number(selected[2]) if len(selected) > 2 else 0.0,
        )
        cooldown = _scoring_fixed_cooldowns().get(key, cooldown)
        cast_row = self.store.skill_cast_time(name, [job])
        cast_time = as_number(
            cast_row.get("corrected_cast_time"),
            as_number(cast_row.get("cast_time"), 0.8),
        )
        return SkillProfile(
            name=name,
            key=key,
            power=power,
            cooldown=max(0.0, cooldown),
            cast_time=max(0.12, cast_time),
        )
    @staticmethod
    def _level_power(level: list[Any]) -> float:
        if len(level) >= 4 and isinstance(level[3], list):
            damage_rows = level[3]
        elif len(level) >= 6 and isinstance(level[3], str):
            damage_rows = [[level[3], level[4], level[5]]]
        elif len(level) >= 5 and isinstance(level[2], str):
            damage_rows = [[level[2], level[3], level[4]]]
        else:
            return 0.0
        return sum(
            max(0.0, as_number(row[1]))
            + max(0.0, as_number(row[2])) / _STANDARD_ATTACK * 100.0
            for row in damage_rows
            if isinstance(row, list) and len(row) >= 3
        )


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreProfilesMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
