from __future__ import annotations

from functools import lru_cache

from .event_platform import event_platform_for


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


class ScoreSupportCatalogMixin:
    def _support_expectations(
        self, job: str, learned_override: dict[str, int] | None = None
    ) -> tuple[_SupportExpectation, ...]:
        job_name = data_job_for(job)
        cache_key = (job_name, tuple(sorted(learned_override.items()))) if learned_override is not None else job_name
        cached = self._support_expectation_cache.get(cache_key)
        if cached is not None:
            return cached
        # Rotation variants (currently 打坐曜/输出曜) share one real skill
        # tree. Always build the cached profession template from that real job;
        # otherwise the first variant queried can cache an empty template under
        # the common "曜" key and erase both variants' support details.
        learned = learned_override if learned_override is not None else self._learned_skills(job_name)
        expected_map: dict[
            tuple[str, str, str], _SupportExpectation
        ] = {}
        cooldown_multipliers = JOB_MECHANICS.job_value(
            job_name, "team_effect_cooldown_multipliers", {}
        )

        def support_cooldown(skill_name: str, raw_cooldown: float) -> float:
            if (
                isinstance(cooldown_multipliers, dict)
                and skill_name in cooldown_multipliers
            ):
                multiplier = max(
                    0.0, as_number(cooldown_multipliers.get(skill_name), 1.0)
                )
                return max(0.0, raw_cooldown * multiplier)
            return self.store.skill_reference_cooldown(
                job_name, skill_name, raw_cooldown
            )

        def add_expected(
            skill_name: str,
            effect_key: str,
            theory: float,
            magnitude: float,
            kind: str,
            cooldown: float,
            duration: float,
            stackable: bool = False,
            stack_group: str = "",
            proc_chance: float = 1.0,
        ) -> None:
            skill_key = canonical_skill(skill_name)
            if learned_override is not None and skill_key in learned and learned[skill_key] <= 0:
                return
            identity = (skill_key, effect_key, kind)
            previous = expected_map.get(identity)
            row = _SupportExpectation(
                skill_key=skill_key,
                skill_name=norm(skill_name) or skill_key,
                effect_key=effect_key,
                theory=theory,
                magnitude=magnitude,
                kind=kind,
                cooldown=max(0.0, cooldown),
                duration=max(0.0, duration),
                stackable=bool(stackable),
                stack_group=norm(stack_group),
                proc_chance=max(0.0, min(1.0, proc_chance)),
            )
            if previous is None:
                expected_map[identity] = row
                return
            positive_cooldowns = [
                value for value in (previous.cooldown, cooldown) if value > 0
            ]
            merged_cooldown = (
                min(positive_cooldowns) if positive_cooldowns else 0.0
            )
            merged_duration = max(previous.duration, duration)
            merged_effective_duration = effective_team_effect_duration_for_effect(
                skill_name, effect_key, merged_duration
            )
            merged_chance = max(previous.proc_chance, row.proc_chance)
            merged_theory = merged_chance * (
                1.0
                if merged_cooldown <= 0 or merged_effective_duration <= 0
                else min(1.0, merged_effective_duration / merged_cooldown)
            )
            expected_map[identity] = _SupportExpectation(
                skill_key=skill_key,
                skill_name=previous.skill_name or norm(skill_name) or skill_key,
                effect_key=effect_key,
                theory=merged_theory,
                magnitude=max(previous.magnitude, magnitude),
                kind=kind,
                cooldown=merged_cooldown,
                duration=merged_duration,
                stackable=previous.stackable or bool(stackable),
                stack_group=previous.stack_group or norm(stack_group),
                proc_chance=merged_chance,
            )

        for skill_name in self.store.full_skill_names(job_name):
            skill_key = canonical_skill(skill_name)
            if skill_key not in learned:
                continue
            learned_level = int(learned.get(skill_key, 0))
            explicit_cap = int(TEAM_EFFECT_LEVEL_CAPS.get(norm(skill_name), 0)) if learned_override is None else 0
            effect_level = (
                min(learned_level, explicit_cap)
                if learned_level > 0 and explicit_cap > 0
                else learned_level or explicit_cap
            )
            timing = self.store.skill_effect_timing(
                job_name,
                skill_name,
                effect_level if effect_level > 0 else None,
                max_required_level=150 if learned_override is not None else 70,
            )
            cooldown = support_cooldown(
                skill_name, as_number(timing.get("cooldown"))
            )
            for rule in boss_effect_rules(skill_name, job_name):
                if rule.key not in _DUTY_EFFECT_KEYS:
                    continue
                duration = resolved_team_effect_duration_for_effect(
                    skill_name,
                    rule.key,
                    rule.duration_seconds,
                    as_number(timing.get("duration")),
                )
                effective_duration = effective_team_effect_duration_for_effect(
                    skill_name, rule.key, duration
                )
                theory = (
                    1.0
                    if cooldown <= 0 or effective_duration <= 0
                    else min(1.0, effective_duration / cooldown)
                )
                theory *= max(0.0, min(1.0, as_number(rule.proc_chance, 1.0)))
                magnitude = abs(as_number(rule.value, 1.0)) or 1.0
                add_expected(
                    skill_name,
                    rule.key,
                    theory,
                    magnitude,
                    "debuff",
                    cooldown,
                    duration,
                    rule.stackable,
                    rule.stack_group,
                    rule.proc_chance,
                )

        team_buff_names = set(self.store.team_buff_names_for_job(job_name))
        team_buff_names.update(
            JOB_MECHANICS.job_value(job_name, "team_effects", [])
        )
        # The build-page selector is a variant of the same cast, not a second
        # independent buff duty. Its neutral component is added below.
        team_buff_names.discard('战吼（力量掌握）')
        ungrouped_team_buffs: set[str] = set()
        exclusive_team_buffs: dict[str, tuple[tuple[int, int], str]] = {}
        for skill_name in sorted(team_buff_names):
            activation_rule = self.store.buff_activation_rule_for_job(
                job_name, skill_name, "团队buff"
            )
            exclusive_group = norm(activation_rule.get("exclusive_group"))
            if not exclusive_group:
                ungrouped_team_buffs.add(skill_name)
                continue
            preferred_jobs = {
                norm(value)
                for value in activation_rule.get("preferred_jobs", [])
                if norm(value)
            }
            priority = (
                int(int(learned.get(canonical_skill(skill_name), 0)) > 0),
                int(job_name in preferred_jobs),
            )
            previous = exclusive_team_buffs.get(exclusive_group)
            if previous is None or priority > previous[0]:
                exclusive_team_buffs[exclusive_group] = (priority, skill_name)
        team_buff_names = ungrouped_team_buffs | {
            skill_name for _priority, skill_name in exclusive_team_buffs.values()
        }
        for skill_name in team_buff_names:
            skill_key = canonical_skill(skill_name)
            # Aura skills are mutually exclusive choices, and unlike old
            # permanent-buff data they always have an explicit learned level.
            # Including an unlearned aura here turns the pair into a synthetic
            # all-damage buff after they are merged below.
            if (
                job_name in _PALADIN_AURA_JOBS
                and skill_key in _PALADIN_AURA_SKILLS
                and skill_key not in learned
            ):
                continue
            # “职业可以学习”不等于本套默认加点已经学习。理论辅助轴与
            # 理论输出轴必须使用同一份加点。这里先约束会主动施加 Debuff
            # 的技能；常驻团队增益仍兼容早期未显式保存等级的默认方案。
            if (
                canonical_skill(skill_name) not in learned
                and boss_effect_rules(skill_name, job_name)
            ):
                continue
            learned_level = int(learned.get(canonical_skill(skill_name), 0))
            explicit_cap = int(TEAM_EFFECT_LEVEL_CAPS.get(norm(skill_name), 0)) if learned_override is None else 0
            if learned_level > 0 and explicit_cap > 0:
                effect_level = min(learned_level, explicit_cap)
            elif learned_level > 0:
                effect_level = learned_level
            elif explicit_cap > 0:
                effect_level = explicit_cap
            else:
                effect_level = max(
                    (
                        int(as_number(row.get("_DisplayLevel")))
                        for row in self.store.full_skill_levels(
                            job_name, skill_name, 70
                        )
                    ),
                    default=0,
                )
            timing = self.store.skill_effect_timing(
                job_name,
                skill_name,
                effect_level if effect_level > 0 else None,
                max_required_level=150 if learned_override is not None else 70,
            )
            cooldown = support_cooldown(
                skill_name, as_number(timing.get("cooldown"))
            )
            rules = boss_effect_rules(skill_name, job_name)
            if rules:
                for rule in rules:
                    if rule.key not in _DUTY_EFFECT_KEYS:
                        continue
                    duration = resolved_team_effect_duration_for_effect(
                        skill_name,
                        rule.key,
                        rule.duration_seconds,
                        as_number(timing.get("duration")),
                    )
                    effective_duration = effective_team_effect_duration_for_effect(
                        skill_name, rule.key, duration
                    )
                    theory = (
                        1.0
                        if cooldown <= 0 or effective_duration <= 0
                        else min(1.0, effective_duration / cooldown)
                    )
                    theory *= max(0.0, min(1.0, as_number(rule.proc_chance, 1.0)))
                    magnitude = abs(as_number(rule.value, 1.0)) or 1.0
                    add_expected(
                        skill_name,
                        rule.key,
                        theory,
                        magnitude,
                        "debuff",
                        cooldown,
                        duration,
                        rule.stackable,
                        rule.stack_group,
                        rule.proc_chance,
                    )
                continue
            activation_rule = self.store.buff_activation_rule_for_job(
                job_name, skill_name, "团队buff"
            )
            rule_cooldown = max(
                0.0, as_number(activation_rule.get("cooldown"))
            )
            rule_duration = max(
                0.0, as_number(activation_rule.get("duration"))
            )
            if rule_cooldown > 0:
                cooldown = rule_cooldown
            duration = _effective_team_buff_duration(
                skill_name,
                rule_duration or as_number(timing.get("duration")),
            )
            theory = 1.0 if cooldown <= 0 or duration <= 0 else min(1.0, duration / cooldown)
            rows = [
                row
                for row in self.store.tables.get("buff", [])
                if norm(row.get("技能")) == skill_name
                and norm(row.get("类别")) == "团队buff"
            ]
            offensive_values: list[float] = []
            for row in rows:
                attribute = norm(row.get("属性"))
                if not any(marker in attribute for marker in _OFFENSIVE_BUFF_ATTRS):
                    continue
                value = abs(as_number(row.get("表格数值")))
                if value <= 0:
                    continue
                # Flat attack/stat values are normalized to the common default
                # panel instead of being mistaken for percentages.
                if "%" not in attribute and value > 100:
                    value = value / _STANDARD_ATTACK * 100.0
                offensive_values.append(value)
            description = re.sub(
                r"(?:物理|魔法)?攻击力\s*\d+(?:\.\d+)?%\s*\+\s*\d+",
                "",
                norm(timing.get("desc")),
            )
            description_values: list[float] = []
            description_markers = _OFFENSIVE_BUFF_ATTRS + (
                "额外伤害",
                "伤害提升",
                "受到伤害",
            )
            for line in description.splitlines():
                if not any(marker in line for marker in description_markers):
                    continue
                description_values.extend(
                    as_number(value)
                    for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)
                )
            catalog_magnitude = _team_buff_offensive_magnitude_at_level(
                skill_name,
                effect_level,
            )
            conditional_magnitude = conditional_team_buff_coefficient(
                canonical_skill(skill_name),
                effect_level,
            )
            magnitude = conditional_magnitude or catalog_magnitude or max(
                offensive_values + description_values,
                default=1.0,
            )
            if skill_key in _BURST_SKILLS:
                # The skill table also contains Spirit Boost's personal
                # physical-attack row (25% at Lv4).  That value must not be
                # used to identify or display the team speed status.  Keep the
                # combat-modifier rule as the source of truth for burst buffs:
                # Spirit Boost and Scorching Flame are CD -40% / action
                # speed +40%, while Time Acceleration is CD -70%.
                cooldown_factor, action_factor, _normal_duration = (
                    _COMBAT_MODIFIER_RULES[skill_key]
                )
                magnitude = max(
                    max(0.0, 1.0 - cooldown_factor),
                    max(0.0, action_factor - 1.0),
                ) * 100.0
            effect_key = _team_buff_effect_key(rows)
            if skill_key == canonical_skill("元素光环"):
                # Element Aura raises fire and water attack, not every element.
                effect_key = "火水攻%"
            add_expected(
                skill_name,
                effect_key,
                theory,
                magnitude,
                "buff",
                cooldown,
                duration,
                stack_group=_team_attack_buff_exclusive_group(skill_name),
            )
            if (job_name == '毁灭者' and skill_key == canonical_skill('战吼')
                    and learned.get(canonical_skill('力量掌握'), 1) > 0):
                neutral = min(18.0, max(0.0, effect_level - 1) * 3)
                if neutral > 0:
                    add_expected(skill_name, '无属性%', theory, neutral, 'buff', cooldown, 180.0,
                                 stack_group='战吼无属性')

        expected_rows = list(expected_map.values())
        if job_name in _PALADIN_AURA_JOBS:
            aura_rows = [
                row
                for row in expected_rows
                if row.kind == "buff" and row.skill_key in _PALADIN_AURA_SKILLS
            ]
            if aura_rows:
                expected_rows = [row for row in expected_rows if row not in aura_rows]
                selected_aura = max(
                    aura_rows,
                    key=lambda row: (row.magnitude, row.skill_key),
                )
                expected_rows.append(
                    _SupportExpectation(
                        skill_key=_PALADIN_AURA_GROUP_KEY,
                        skill_name="元素光环/判定光环（互斥）",
                        effect_key=selected_aura.effect_key,
                        theory=1.0,
                        # Coverage remains one mutually exclusive duty, while
                        # counterfactual damage keeps the selected aura's real
                        # element and learned-level value.
                        magnitude=selected_aura.magnitude,
                        kind="buff",
                        cooldown=0.0,
                        duration=0.0,
                        stack_group=_PALADIN_AURA_GROUP_KEY,
                    )
                )
        expected = tuple(expected_rows)
        self._support_expectation_cache[cache_key] = expected
        while len(self._support_expectation_cache) > 128:
            self._support_expectation_cache.pop(next(iter(self._support_expectation_cache)))
        return expected

    def _resolved_paladin_aura_expectation(
        self,
        snapshot: CombatSnapshot,
        source_id: str,
        job: str,
        expectation: _SupportExpectation,
    ) -> _SupportExpectation:
        """Resolve the mutually exclusive aura to the one seen in combat."""
        if expectation.skill_key != _PALADIN_AURA_GROUP_KEY:
            return expectation

        rows_by_skill: dict[str, list[Any]] = defaultdict(list)
        for row in event_platform_for(snapshot).player_buff_series:
            if str(row.source_id) != str(source_id):
                continue
            skill_key = canonical_skill(row.skill_name)
            if skill_key in _PALADIN_AURA_SKILLS:
                rows_by_skill[skill_key].append(row)
        if not rows_by_skill:
            return expectation

        def active_seconds(rows: list[Any]) -> float:
            return sum(
                max(0.0, end - start)
                for start, end in _merge_intervals(
                    interval for row in rows for interval in row.intervals
                )
            )

        skill_key, observed_rows = max(
            rows_by_skill.items(),
            key=lambda item: (active_seconds(item[1]), item[0]),
        )
        learned = self._combat_learned_skills(snapshot, source_id, job)
        learned_level = (learned if learned is not None else self._learned_skills(job)).get(skill_key, 0)
        # Aura status packets may expose the resistance component instead of
        # the offensive component (Element Aura Lv4 reports 30 resistance,
        # while its fire/water attack bonus is 24).  The live value is useful
        # for identifying the active aura, but it must not replace the learned
        # level's offensive value in counterfactual damage scoring.
        magnitude = (
            _team_buff_offensive_magnitude_at_level(skill_key, learned_level)
            if learned_level > 0
            else 0.0
        )
        if magnitude <= 0:
            magnitude = expectation.magnitude
        effect_key = (
            "火水攻%"
            if skill_key == canonical_skill("元素光环")
            else "光攻%"
        )
        return replace(
            expectation,
            effect_key=effect_key,
            magnitude=magnitude,
        )

    @staticmethod
    def _support_expectations_with_glyphs(
        expected: tuple[_SupportExpectation, ...],
        theory_plan: dict[str, Any] | None,
    ) -> tuple[_SupportExpectation, ...]:
        """Apply only the glyphs selected by this exact theoretical build."""
        if not isinstance(theory_plan, dict):
            return expected
        duration_bonus: dict[str, float] = {}
        cooldown_bonus: dict[str, float] = {}
        for row in theory_plan.get("_score_glyphs", ()):
            if not isinstance(row, dict):
                continue
            effect = norm(row.get("effect"))
            key = canonical_skill(row.get("skill"))
            value = max(0.0, as_number(row.get("value")))
            if effect in {"增益持续", "增益持续时间增加"} and key and value > duration_bonus.get(key, 0.0):
                duration_bonus[key] = value
            elif effect in {"冷却缩减", "冷却时间减少"} and key and value > cooldown_bonus.get(key, 0.0):
                cooldown_bonus[key] = value
        if not duration_bonus and not cooldown_bonus:
            return expected

        result: list[_SupportExpectation] = []
        for row in expected:
            duration_increase = duration_bonus.get(row.skill_key, 0.0)
            cooldown_reduction = cooldown_bonus.get(row.skill_key, 0.0)
            if duration_increase <= 0 and cooldown_reduction <= 0:
                result.append(row)
                continue
            duration = row.duration * (1.0 + duration_increase / 100.0)
            cooldown = row.cooldown * (
                1.0 - min(100.0, cooldown_reduction) / 100.0
            )
            effective_duration = effective_team_effect_duration_for_effect(
                row.skill_name, row.effect_key, duration
            )
            theory = row.proc_chance * (
                1.0
                if cooldown <= 0 or effective_duration <= 0
                else min(1.0, effective_duration / cooldown)
            )
            result.append(
                _SupportExpectation(
                    skill_key=row.skill_key,
                    skill_name=row.skill_name,
                    effect_key=row.effect_key,
                    theory=theory,
                    magnitude=row.magnitude,
                    kind=row.kind,
                    cooldown=cooldown,
                    duration=duration,
                    stackable=row.stackable,
                    stack_group=row.stack_group,
                    proc_chance=row.proc_chance,
                )
            )
        return tuple(result)

    @staticmethod
    def _peda_defense_reduction_gain(reduction_percent: float) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0
        before = max(
            0.0,
            1.0 - BuildCalculator.defense_percent(_PEDA_DEFENSE) / 100.0,
        )
        reduced_defense = _PEDA_DEFENSE * (1.0 - reduction / 100.0)
        after = max(
            0.0,
            1.0 - BuildCalculator.defense_percent(reduced_defense) / 100.0,
        )
        return max(0.0, after / max(0.01, before) - 1.0)

    @staticmethod
    def _peda_crit_resist_reduction_gain(reduction_percent: float) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0

        def crit_multiplier(resist_value: float) -> float:
            resist_percent = min(round(max(0.0, resist_value) / 433.0, 1), 90.0)
            effective_crit_rate = (
                _STANDARD_CRIT_PERCENT / 100.0
                * max(0.0, 1.0 - resist_percent / 100.0)
            )
            crit_damage = 2.0 + _STANDARD_CRIT_DAMAGE_PERCENT / 100.0
            return 1.0 + effective_crit_rate * (crit_damage - 1.0)

        before = crit_multiplier(_PEDA_CRIT_RESIST)
        after = crit_multiplier(
            _PEDA_CRIT_RESIST * (1.0 - reduction / 100.0)
        )
        return max(0.0, after / max(0.01, before) - 1.0)

    @staticmethod
    def _peda_element_resistance_reduction_gain(
        reduction_percent: float,
    ) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0
        before = max(0.01, 1.0 - _PEDA_ELEMENT_RESIST_PERCENT / 100.0)
        after = max(
            0.0,
            1.0
            - (_PEDA_ELEMENT_RESIST_PERCENT - reduction) / 100.0,
        )
        return max(0.0, after / before - 1.0)

    @staticmethod
    @lru_cache(maxsize=512)
    def _support_effect_gain_rate(effect: _SupportExpectation) -> float:
        """Convert a status value to an estimated marginal team-DPS gain."""
        key = effect.effect_key
        if key == "减攻击%":
            return 0.0
        magnitude = (
            _freeze_gain_per_layer()
            if key == "冰冻层"
            else max(0.0, effect.magnitude)
        )
        # Main-stat buffs do not turn into attack at a one-to-one rate on the
        # common level-70 equipment baseline. Half value is the standardized
        # conversion used until the capture can expose each recipient's panel.
        if key == "四维%":
            magnitude *= 0.50
        if key == "致命%":
            # Standard score builds are already at the 90% panel cap.  A
            # panel-critical buff is not a flat final-damage multiplier.
            return 0.0
        if "防御" in key:
            return TeamScoreEngine._peda_defense_reduction_gain(magnitude)
        if "致命抵抗" in key:
            return TeamScoreEngine._peda_crit_resist_reduction_gain(magnitude)
        if key in {
            "减火抗%",
            "减水抗%",
            "减光抗%",
            "减暗抗%",
            "减全抗%",
        }:
            return TeamScoreEngine._peda_element_resistance_reduction_gain(
                magnitude
            )
        return magnitude / 100.0

    def _boss_defense_reduction_gain(self, reduction_percent: float) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0
        before = max(
            0.0,
            1.0
            - BuildCalculator.defense_percent(self._score_boss_defense) / 100.0,
        )
        reduced_defense = self._score_boss_defense * (1.0 - reduction / 100.0)
        after = max(
            0.0,
            1.0 - BuildCalculator.defense_percent(reduced_defense) / 100.0,
        )
        return max(0.0, after / max(0.01, before) - 1.0)

    def _boss_crit_resist_reduction_gain(self, reduction_percent: float) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0

        def crit_multiplier(resist_value: float) -> float:
            resist_percent = min(round(max(0.0, resist_value) / 433.0, 1), 90.0)
            effective_crit_rate = (
                _STANDARD_CRIT_PERCENT / 100.0
                * max(0.0, 1.0 - resist_percent / 100.0)
            )
            crit_damage = 2.0 + _STANDARD_CRIT_DAMAGE_PERCENT / 100.0
            return 1.0 + effective_crit_rate * (crit_damage - 1.0)

        before = crit_multiplier(self._score_boss_crit_resist)
        after = crit_multiplier(
            self._score_boss_crit_resist * (1.0 - reduction / 100.0)
        )
        return max(0.0, after / max(0.01, before) - 1.0)

    def _boss_crit_resist_reduction_gain_for_recipient(
        self,
        reduction_percent: float,
        source_id: str,
    ) -> float:
        """Measure critical-resistance shred with the recipient's score build."""
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0
        crit_percent, crit_damage_percent = self._score_crit_profiles_by_source.get(
            str(source_id),
            (_STANDARD_CRIT_PERCENT, _STANDARD_CRIT_DAMAGE_PERCENT),
        )
        panel_crit = min(90.0, max(0.0, crit_percent)) / 100.0
        crit_damage = 2.0 + min(
            100.0, max(0.0, crit_damage_percent)
        ) / 100.0

        def multiplier(resist_value: float) -> float:
            resist_percent = min(
                round(max(0.0, resist_value) / 433.0, 1),
                90.0,
            )
            effective_rate = panel_crit * max(0.0, 1.0 - resist_percent / 100.0)
            return 1.0 + effective_rate * (crit_damage - 1.0)

        before = multiplier(self._score_boss_crit_resist)
        after = multiplier(
            self._score_boss_crit_resist * (1.0 - reduction / 100.0)
        )
        return max(0.0, after / max(0.01, before) - 1.0)

    def _boss_element_resistance_reduction_gain(
        self, reduction_percent: float
    ) -> float:
        reduction = min(100.0, max(0.0, reduction_percent))
        if reduction <= 0:
            return 0.0
        before = max(0.01, 1.0 - self._score_boss_element_resist / 100.0)
        after = max(
            0.0,
            1.0
            - (self._score_boss_element_resist - reduction) / 100.0,
        )
        return max(0.0, after / before - 1.0)

    def _configured_support_effect_gain_rate(
        self, effect: _SupportExpectation
    ) -> float:
        if effect.skill_key in {
            canonical_skill(CROW_SKILL),
            canonical_skill(DANCE_STAGE_TWO_SKILL),
        }:
            # Live scoring deliberately has no fixed HP-equivalent value for
            # these effects. Resolve their gain at each damage timestamp.
            return 0.0
        cached = self._configured_support_gain_cache.get(effect)
        if cached is not None:
            return cached
        key = effect.effect_key
        if key == "减攻击%":
            gain = 0.0
        else:
            magnitude = (
                _freeze_gain_per_layer()
                if key == "冰冻层"
                else max(0.0, effect.magnitude)
            )
            if key == "四维%":
                magnitude *= 0.50
            if key == "致命%":
                gain = self._critical_panel_gain_rate(magnitude)
            elif "防御" in key:
                gain = self._boss_defense_reduction_gain(magnitude)
            elif "致命抵抗" in key:
                gain = self._boss_crit_resist_reduction_gain(magnitude)
            elif key in {
                "减火抗%",
                "减水抗%",
                "减光抗%",
                "减暗抗%",
                "减全抗%",
            }:
                gain = self._boss_element_resistance_reduction_gain(magnitude)
            else:
                gain = magnitude / 100.0
        self._configured_support_gain_cache[effect] = gain
        return gain

    def _critical_panel_gain_rate(
        self,
        increase_percent: float,
        source_id: str = "",
    ) -> float:
        """Convert panel critical chance into expected damage, respecting cap."""

        crit_percent, crit_damage_percent = self._score_crit_profiles_by_source.get(
            str(source_id),
            (_STANDARD_CRIT_PERCENT, _STANDARD_CRIT_DAMAGE_PERCENT),
        )
        increase = max(0.0, increase_percent)
        before_crit = min(90.0, max(0.0, crit_percent))
        after_crit = min(90.0, before_crit + increase)
        if after_crit <= before_crit + 1e-9:
            return 0.0
        resist_percent = min(
            round(max(0.0, self._score_boss_crit_resist) / 433.0, 1),
            90.0,
        )
        crit_damage = 2.0 + min(
            100.0, max(0.0, crit_damage_percent)
        ) / 100.0

        def multiplier(panel_crit: float) -> float:
            effective_rate = (
                panel_crit / 100.0
                * max(0.0, 1.0 - resist_percent / 100.0)
            )
            return 1.0 + effective_rate * (crit_damage - 1.0)

        before = multiplier(before_crit)
        after = multiplier(after_crit)
        return max(0.0, after / max(0.01, before) - 1.0)

    def _configured_support_effect_gain_rate_for_recipient(
        self,
        effect: _SupportExpectation,
        source_id: str,
        seconds: float | None = None,
        boss_hp_segments: tuple[tuple[tuple[float, float], ...], ...] = (),
    ) -> float:
        if effect.skill_key in {
            canonical_skill(CROW_SKILL),
            canonical_skill(DANCE_STAGE_TWO_SKILL),
        }:
            hp_percent = (
                boss_hp_percent_at(boss_hp_segments, seconds)
                if seconds is not None
                else None
            )
            return conditional_team_buff_gain_rate(
                effect.skill_key,
                effect.magnitude,
                hp_percent,
            )
        cache_key = (effect, str(source_id))
        cached = self._configured_support_recipient_gain_cache.get(cache_key)
        if cached is not None:
            return cached
        if effect.effect_key == '无属性%':
            baseline = getattr(self, '_score_neutral_profiles_by_source', {}).get(str(source_id), 0.0)
            gain = max(0.0, effect.magnitude) / max(1.0, 100.0 + baseline)
        elif effect.effect_key == "致命%":
            gain = self._critical_panel_gain_rate(effect.magnitude, source_id)
        elif "致命抵抗" in effect.effect_key:
            gain = self._boss_crit_resist_reduction_gain_for_recipient(
                effect.magnitude,
                source_id,
            )
        else:
            gain = self._configured_support_effect_gain_rate(effect)
        self._configured_support_recipient_gain_cache[cache_key] = gain
        return gain

    def _job_damage_profile(self, job: str) -> dict[str, float]:
        job_name = data_job_for(job)
        cached = self._damage_profile_cache.get(job_name)
        if cached is not None:
            return cached
        row = next(
            (
                item
                for item in self.store.tables.get("effective", ())
                if norm(item.get("职业")) == job_name
            ),
            {},
        )
        profile = {
            "physical": 0.0,
            "magical": 0.0,
            "fire": 0.0,
            "water": 0.0,
            "light": 0.0,
            "dark": 0.0,
            "elemental": 0.0,
        }
        total_weight = 0.0
        element_keys = {
            "火": "fire",
            "水": "water",
            "光": "light",
            "暗": "dark",
        }
        for index in range(1, 4):
            weight = max(0.0, as_number(row.get(f"权重{index}")))
            if weight <= 0:
                continue
            total_weight += weight
            attack = norm(row.get(f"攻击{index}"))
            if attack == "物攻":
                profile["physical"] += weight
            elif attack == "魔攻":
                profile["magical"] += weight
            elif attack == "双攻":
                profile["physical"] += weight * 0.5
                profile["magical"] += weight * 0.5
            else:
                profile["physical"] += weight * 0.5
                profile["magical"] += weight * 0.5
            element = norm(row.get(f"属性{index}"))
            element_key = element_keys.get(element)
            if element_key:
                profile[element_key] += weight
                profile["elemental"] += weight
        if total_weight <= 0:
            profile.update(
                physical=0.5,
                magical=0.5,
                fire=0.25,
                water=0.25,
                light=0.25,
                dark=0.25,
                elemental=1.0,
            )
            self._damage_profile_cache[job_name] = profile
            return profile
        for key in profile:
            profile[key] = max(0.0, min(1.0, profile[key] / total_weight))
        self._damage_profile_cache[job_name] = profile
        return profile

    def _static_personal_attack_bonuses(self, job: str) -> tuple[float, float]:
        """Return always-on attack bonuses from the standardized skill build."""
        job_name = data_job_for(job)
        cached = self._personal_attack_bonus_cache.get(job_name)
        if cached is not None:
            return cached

        learned = self._learned_skills(job_name)
        physical = 0.0
        magical = 0.0
        for row in _team_buff_data().values():
            if not isinstance(row, dict):
                continue
            if norm(row.get("类别")) != "被动" or norm(row.get("来源")) != job_name:
                continue
            skill_key = canonical_skill(row.get("技能"))
            learned_level = int(learned.get(skill_key, 0))
            levels = row.get("等级", {})
            if learned_level <= 0 or not isinstance(levels, dict) or not levels:
                continue
            available_levels = sorted(
                int(as_number(level))
                for level in levels
                if int(as_number(level)) > 0
            )
            if not available_levels:
                continue
            resolved_level = max(
                (level for level in available_levels if level <= learned_level),
                default=available_levels[0],
            )
            attrs = levels.get(str(resolved_level), {})
            if not isinstance(attrs, dict):
                continue
            physical += max(0.0, as_number(attrs.get("物攻%")))
            magical += max(0.0, as_number(attrs.get("魔攻%")))

        result = (physical, magical)
        self._personal_attack_bonus_cache[job_name] = result
        return result

    @staticmethod
    def _team_attack_effect_values(
        effect: _SupportExpectation,
    ) -> tuple[float, float]:
        magnitude = max(0.0, effect.magnitude)
        if effect.effect_key == "双攻%":
            return magnitude, magnitude
        if effect.effect_key == "物攻%":
            return magnitude, 0.0
        if effect.effect_key == "魔攻%":
            return 0.0, magnitude
        return 0.0, 0.0

    def _personal_attack_bonuses_at(
        self,
        context: _SupportCounterfactualContext,
        target_id: str,
        job: str,
        seconds: float,
    ) -> tuple[float, float]:
        physical, magical = self._static_personal_attack_bonuses(job)
        for effect in context.personal_attack_effects:
            if effect.target_id != target_id or not _in_intervals(seconds, effect.intervals):
                continue
            physical += effect.physical_percent
            magical += effect.magical_percent
        return physical, magical

    def _team_attack_buff_allocation(
        self,
        active_rows: list[_SupportEffectInstance],
        context: _SupportCounterfactualContext,
        recipient_id: str,
        recipient_job: str,
        seconds: float,
    ) -> tuple[float, list[tuple[_SupportEffectInstance, float]]]:
        """Allocate additive team attack buffs above the recipient's own pool."""
        if not active_rows:
            return 0.0, []
        personal_physical, personal_magical = self._personal_attack_bonuses_at(
            context,
            recipient_id,
            recipient_job,
            seconds,
        )
        profile = self._job_damage_profile(recipient_job)
        grouped: dict[tuple[str, str], list[tuple[_SupportEffectInstance, float]]] = (
            defaultdict(list)
        )
        for instance in active_rows:
            physical, magical = self._team_attack_effect_values(instance.expectation)
            effective_gain = (
                profile["physical"] * physical / max(1.0, 100.0 + personal_physical)
                + profile["magical"] * magical / max(1.0, 100.0 + personal_magical)
            )
            if effective_gain <= 1e-12:
                continue
            expectation = instance.expectation
            subgroup = (
                "stackable" if expectation.stackable else "exclusive",
                expectation.stack_group or expectation.skill_key,
            )
            grouped[subgroup].append((instance, effective_gain))

        total_gain = 0.0
        allocations: list[tuple[_SupportEffectInstance, float]] = []
        for subgroup, rows in grouped.items():
            selected_rows = rows
            if subgroup == ("exclusive", _TEAM_ATTACK_BUFF_EXCLUSIVE_GROUP):
                # The game explicitly resolves this group by skill priority,
                # not by Shapley fallback value: Merciless Strike suppresses
                # Special Fortifying Potion for every overlapping instant.
                highest_priority = max(
                    _team_attack_buff_priority(instance.expectation.skill_key)
                    for instance, _gain in rows
                )
                selected_rows = [
                    row
                    for row in rows
                    if _team_attack_buff_priority(row[0].expectation.skill_key)
                    == highest_priority
                ]
            shares = self._highest_effect_shares(
                [gain for _instance, gain in selected_rows]
            )
            total_gain += sum(shares)
            allocations.extend(
                (instance, share)
                for (instance, _gain), share in zip(
                    selected_rows, shares, strict=True
                )
                if share > 1e-12
            )
        return total_gain, allocations
