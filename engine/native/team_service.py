from __future__ import annotations

from functools import lru_cache

class NativeTeamMixin:
    @staticmethod
    def _auto_glyph_cache_key(
        build: dict[str, Any],
        window: float,
        cocktail: bool,
        support_team_size: int = 1,
    ) -> str:
        cache_build = copy.deepcopy(build)
        cache_build.pop("skill_glyphs", None)
        cache_build.pop("team_sim", None)
        payload = {
            "optimizer": "two-stage-linked-cooldown-independent-damage-v3",
            "build": cache_build,
            "window": round(float(window), 4),
            "cocktail": bool(cocktail),
            "supportTeamSize": max(1, int(support_team_size)),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    def quick_timeline(self, build: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """Run the V4.01 planner through its window-free data runtime."""
        resolved = copy.deepcopy(settings if isinstance(settings, dict) else {})
        resolved.setdefault("mode", "long")
        try:
            if bool(resolved.get("teamLink")):
                team_sim = build.get("team_sim", {}) if isinstance(build.get("team_sim"), dict) else {}
                jobs = [
                    norm(value) for value in team_sim.get("members", [])
                    if norm(value) and norm(value) != NONE_TEXT
                ][:8]
                current_job = norm(build.get("second_job"))
                if current_job and current_job not in jobs:
                    jobs.insert(0, current_job)
                if jobs:
                    member_index = jobs.index(current_job) if current_job in jobs else 0
                    for source, target in (
                        ("cdr_usage", "cdrUsage"), ("cdr_glyphs", "cdrGlyphs"),
                        ("cdr_applies", "cdrApplies"), ("disabled_effects", "disabledTeamEffects"),
                    ):
                        if target not in resolved and source in team_sim:
                            resolved[target] = copy.deepcopy(team_sim[source])
                    resolved = self.legacy_timeline.team_settings(build, jobs, member_index, resolved)
            return self.legacy_timeline.calculate(copy.deepcopy(build), resolved)
        except Exception as exc:
            # Keep the application usable on an incomplete installation while
            # making the fallback explicit to the frontend and diagnostics.
            catalog = self.skill_rows(build)
            fallback = self.timeline_planner.plan(build, catalog["rows"], resolved)
            fallback["planner"] = "v410-native-fallback"
            fallback["legacyParity"] = False
            fallback["fallbackReason"] = f"{type(exc).__name__}: {exc}"
            fallback["auto"] = {
                "segments": copy.deepcopy(fallback.get("segments", [])),
                "total": as_number(fallback.get("total")),
                "dps": as_number(fallback.get("dps")),
                "contribution": copy.deepcopy(fallback.get("contribution", [])),
            }
            fallback["manual"] = copy.deepcopy(fallback["auto"])
            fallback["candidates"] = []
            fallback["availableSkills"] = [
                norm(row.get("skill"))
                for row in catalog.get("rows", [])
                if norm(row.get("skill"))
            ]
            fallback["manualSequence"] = [
                norm(row.get("skill"))
                for row in fallback.get("segments", [])
                if norm(row.get("skill"))
            ]
            fallback["autoSequence"] = list(fallback["manualSequence"])
            fallback["manualIssues"] = []
            fallback["scenarioComparison"] = []
            return fallback
    def team_setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        members = self._payload_team_members(payload)
        build = self.get_build("build1")
        team_sim = build.setdefault("team_sim", {})
        if not isinstance(team_sim, dict):
            team_sim = {}
            build["team_sim"] = team_sim
        aura_choices = payload.get("auraChoices")
        if isinstance(aura_choices, dict):
            team_sim["aura_choices"] = copy.deepcopy(aura_choices)
        effects = self.legacy_timeline.team_effect_catalog(build, members, payload)
        return {
            "effects": effects,
            "members": members,
            "effectCount": len(effects),
            "cdrSkills": ["鸡尾酒", "精神爆发", "时间加速"],
            "auraJobs": ["圣骑士", "十字军"],
            "auraOptions": ["元素光环", "判定光环"],
        }
    def team_member_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the editable V4.01-equivalent build for one team slot."""

        job = norm(payload.get("job"))
        if not job or job in {NONE_TEXT, "不限制"}:
            raise ValueError("请先为该位置选择职业")
        runtime = self._custom_timeline_runtime(payload)
        build = self._team_standard_build_for_job(runtime, job)
        build["team_sim"] = {"members": [], "effects": []}
        return build
    def _team_payload_build_overrides(
        self,
        payload: dict[str, Any],
        members: list[str],
    ) -> list[dict[str, Any] | None]:
        """Validate slot-scoped build overrides before they enter a planner."""

        raw = payload.get("buildOverrides", [])
        raw = raw if isinstance(raw, list) else []
        resolved: list[dict[str, Any] | None] = []
        for index, member in enumerate(members):
            entry = raw[index] if index < len(raw) else None
            if not isinstance(entry, dict):
                resolved.append(None)
                continue
            wrapped_build = entry.get("build")
            build = wrapped_build if isinstance(wrapped_build, dict) else entry
            declared_job = norm(entry.get("job")) or norm(build.get("second_job"))
            expected_job = "曜" if member in {"打坐曜", "输出曜"} else norm(member)
            declared_real_job = "曜" if declared_job in {"打坐曜", "输出曜"} else declared_job
            if declared_real_job and declared_real_job != expected_job:
                resolved.append(None)
                continue
            copied = copy.deepcopy(build)
            copied["second_job"] = expected_job
            copied["effective_second_job"] = expected_job
            copied["team_sim"] = {"members": [], "effects": []}
            resolved.append(copied)
        return resolved
    def team_preview(
        self,
        payload: dict[str, Any],
        progress: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        notify = progress or (lambda _value, _message: None)
        notify(2, "正在准备八人配队模拟")
        members = self._payload_team_members(payload)
        window = max(30.0, min(720.0, as_number(payload.get("window"), 125.0)))
        timeline_settings = {
            "window": window,
            "cocktail": bool(payload.get("cocktail", True)),
            "blockedIntervals": copy.deepcopy(payload.get("blockedIntervals", [])),
            "burstIntervals": copy.deepcopy(payload.get("burstIntervals", [])),
            "accelIntervals": copy.deepcopy(payload.get("accelIntervals", [])),
            "disabledTeamEffects": copy.deepcopy(payload.get("disabledTeamEffects", [])),
            "cdrUsage": copy.deepcopy(payload.get("cdrUsage", {})),
            "cdrGlyphs": copy.deepcopy(payload.get("cdrGlyphs", {})),
            "cdrApplies": copy.deepcopy(payload.get("cdrApplies", {})),
            "teamEffectOverrides": copy.deepcopy(payload.get("teamEffectOverrides", {})),
            "freezeCoefficient": as_number(payload.get("freezeCoefficient"), 0.3),
        }
        rows: list[dict[str, Any]] = []
        boss = norm(payload.get("boss"))
        conversion_runtime = self._custom_timeline_runtime(payload)
        build_overrides = self._team_payload_build_overrides(payload, members)
        hand_multipliers = payload.get("handMultipliers", {})
        hand_multipliers = hand_multipliers if isinstance(hand_multipliers, dict) else {}
        member_count = max(1, len(members))
        for member_index, job in enumerate(members, 1):
            notify(
                3 + round(member_index * 17 / member_count),
                f"正在准备 {job} 的配装、加点与技能纹章（{member_index}/{len(members)}）",
            )
            real_job = "曜" if job in {"打坐曜", "输出曜"} else job
            build = (
                copy.deepcopy(build_overrides[member_index - 1])
                if build_overrides[member_index - 1] is not None
                else self._team_standard_build_for_job(conversion_runtime, job)
            )
            build_team_sim = build.setdefault("team_sim", {})
            if not isinstance(build_team_sim, dict):
                build_team_sim = {}
                build["team_sim"] = build_team_sim
            if isinstance(payload.get("auraChoices"), dict):
                build_team_sim["aura_choices"] = copy.deepcopy(
                    payload["auraChoices"]
                )
            if bool(payload.get("autoGlyphs")):
                glyph_window = min(125.0, window)
                cache_key = self._auto_glyph_cache_key(
                    build, glyph_window, True, member_count
                )
                selected_glyphs = self._auto_glyph_cache.get(cache_key)
                if selected_glyphs is None:
                    optimized = self.optimize_skill_glyphs({
                        "build": build, "count": 4, "window": glyph_window,
                        "cocktail": True, "supportTeamSize": member_count,
                    }, lambda value, message: notify(
                        3 + round(((member_index - 1) + max(0, min(100, value)) / 100.0) * 17 / member_count),
                        message,
                    ))
                    selected_glyphs = copy.deepcopy(optimized.get("rows", []))
                    self._auto_glyph_cache[cache_key] = copy.deepcopy(selected_glyphs)
                build["skill_glyphs"] = {
                    "second_job": real_job, "count": 4,
                    "rows": copy.deepcopy(selected_glyphs), "auto_selected": True,
                }
            rows.append({
                "job": job,
                "build": build,
                "customBuild": build_overrides[member_index - 1] is not None,
            })
        for index, row in enumerate(rows):
            notify(
                20 + round(index * 74 / max(1, len(rows))),
                f"正在计算 {row['job']} 的配队实战轴（{index + 1}/{len(rows)}）",
            )
            build = row.pop("build")
            baseline = self._cached_personal_timeline(build, window)
            member_settings = self.legacy_timeline.team_settings(
                build,
                members,
                index,
                timeline_settings,
            )
            timeline = self.quick_timeline(build, member_settings)
            hand = max(0.0, min(10.0, as_number(hand_multipliers.get(row["job"]), 1.0)))
            base_damage = as_number(baseline.get("total")) * hand
            base_dps = as_number(baseline.get("dps")) * hand
            if abs(hand - 1.0) > 1e-9:
                timeline = copy.deepcopy(timeline)
                for key in ("total", "dps"):
                    timeline[key] = as_number(timeline.get(key)) * hand
                for axis_name in ("auto", "manual"):
                    axis = timeline.get(axis_name)
                    if isinstance(axis, dict):
                        axis["total"] = as_number(axis.get("total")) * hand
                        axis["dps"] = as_number(axis.get("dps")) * hand
                        for segment in axis.get("segments", []):
                            for key in ("output", "rawOutput", "dpa"):
                                segment[key] = as_number(segment.get(key)) * hand
                        for contribution in axis.get("contribution", []):
                            contribution["output"] = as_number(contribution.get("output")) * hand
                for segment in timeline.get("segments", []):
                    for key in ("output", "rawOutput", "dpa"):
                        segment[key] = as_number(segment.get(key)) * hand
                for contribution in timeline.get("contribution", []):
                    contribution["output"] = as_number(contribution.get("output")) * hand
            team_damage = as_number(timeline.get("total"))
            team_dps = as_number(timeline.get("dps"))
            row.update(
                damage=team_damage,
                dps=team_dps,
                timeline=timeline,
                handMultiplier=hand,
                baseDamage=base_damage,
                baseDps=base_dps,
                teamGain=team_damage - base_damage,
                teamMultiplier=team_damage / max(1e-9, base_damage),
                gainPercent=(team_damage / max(1e-9, base_damage) - 1.0) * 100.0,
            )
        total = sum(as_number(row["damage"]) for row in rows)
        notify(96, "正在汇总团队输出、效果与职业占比")
        for row in rows:
            row["share"] = as_number(row["damage"]) / total if total else 0.0
        result = {
            "members": rows, "window": window, "total": total,
            "dps": total / window if window else 0.0,
            "settings": {
                "boss": boss, "auraChoices": copy.deepcopy(payload.get("auraChoices", {})),
                "freezeCoefficient": as_number(payload.get("freezeCoefficient"), 0.3),
            },
        }
        notify(100, "八人配队模拟已完成")
        return result
    def _team_standard_build_for_job(
        self,
        runtime: PureTimelineRuntime,
        job: str,
    ) -> dict[str, Any]:
        """Return the V4.01-equivalent converted baseline for one team member.

        A team member must not inherit another job's equipment, enchants,
        cards, engravings, skill points or multiplier overrides from 配装1.
        The extreme runtime owns the established job-conversion rules; skill
        choices are then reset to the deterministic built-in defaults used by
        team simulation.
        """

        real_job = "曜" if job in {"打坐曜", "输出曜"} else norm(job)
        build = runtime._extreme_timeline_build_for_job(job)
        defaults = self._skill_defaults_for_job(real_job)
        for key in (
            "skill_build",
            "skill_mastery_build",
            "skill_glyphs",
            "skill_multiplier_overrides",
        ):
            build[key] = copy.deepcopy(defaults[key])
        return build
    @staticmethod
    def _effect_coverage(windows: list[dict[str, Any]], window: float) -> dict[tuple[str, str], float]:
        grouped: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
        for row in windows:
            start = max(0.0, min(window, as_number(row.get("start"))))
            end = max(start, min(window, as_number(row.get("end"))))
            if end <= start + 1e-6:
                continue
            skill = norm(row.get("skillKey") or row.get("skill"))
            if row.get("attrs") and isinstance(row.get("attrs"), dict):
                for key, value in row["attrs"].items():
                    rule = effect_rule(
                        EffectInput(
                            key=norm(key),
                            value=as_number(value),
                            skill_name=skill,
                            stack_group=norm(row.get("group")),
                            stackable=bool(row.get("stackable")),
                        )
                    )
                    grouped.setdefault((rule.stack_group, rule.key), []).append(
                        (start, end, as_number(value))
                    )
            else:
                key = norm(row.get("key"))
                if key:
                    rule = effect_rule(
                        EffectInput(
                            key=key,
                            value=as_number(row.get("value")),
                            skill_name=skill,
                            stack_group=norm(row.get("group")),
                            stackable=bool(row.get("stackable")),
                        )
                    )
                    grouped.setdefault((rule.stack_group, rule.key), []).append(
                        (start, end, as_number(row.get("value")))
                    )
        result: dict[tuple[str, str], float] = {}
        for key, values in grouped.items():
            boundaries = sorted({point for start, end, _value in values for point in (start, end)})
            integral = 0.0
            for left, right in zip(boundaries, boundaries[1:]):
                midpoint = (left + right) / 2.0
                active = [value for start, end, value in values if start <= midpoint < end]
                if active:
                    integral += (right - left) * max(active)
            result[key] = integral / max(1e-9, window)
        return result
    def _candidate_profile(
        self,
        job: str,
        window: float,
        base: dict[str, Any],
        conversion_runtime: PureTimelineRuntime | None = None,
    ) -> dict[str, Any]:
        runtime = conversion_runtime or self._custom_timeline_runtime({
            "boss": norm(base.get("boss")),
            "freezeCoefficient": as_number(base.get("freeze_coefficient"), 0.3),
        })
        build = self._team_standard_build_for_job(runtime, job)
        catalog = self.skill_rows(build)
        timeline = self._cached_personal_timeline(build, window)
        element_weights: Counter[str] = Counter()
        physical = 0.0
        magical = 0.0
        rows_by_skill = {norm(row.get("skill")): row for row in catalog["rows"]}
        for contribution in timeline.get("contribution", []):
            value = max(0.0, as_number(contribution.get("output")))
            row = rows_by_skill.get(norm(contribution.get("skill")), {})
            element = norm(row.get("element"))
            if element:
                element_weights[element] += value
            channels = row.get("channels", {}) if isinstance(row.get("channels"), dict) else {}
            physical += value if "物理" in channels else 0.0
            magical += value if "魔法" in channels else 0.0
        return {
            "job": job,
            "damage": as_number(timeline.get("total")),
            "dps": as_number(timeline.get("dps")),
            "effects": self._effect_coverage(timeline.get("effectWindows", []), window),
            "elements": element_weights,
            "physical": physical,
            "magical": magical,
        }
    @staticmethod
    def _approximate_team_damage(team: list[str], profiles: dict[str, dict[str, Any]]) -> float:
        total = 0.0
        for target_job in team:
            target = profiles[target_job]
            base = max(0.0, as_number(target.get("damage")))
            element_total = sum(target.get("elements", {}).values())
            effect_by_group: dict[tuple[str, str], float] = {}
            for provider_job in team:
                for marker, average in profiles[provider_job].get("effects", {}).items():
                    effect_by_group[marker] = max(effect_by_group.get(marker, 0.0), average)
            multiplier = 1.0
            final = element = resistance = crit = 0.0
            resolved_attrs: dict[str, float] = {}
            for (_group, key), average in effect_by_group.items():
                canonical_key = canonical_effect_key(key)
                if canonical_key in {
                    "攻击%", "物攻%", "魔攻%", "增伤%", "物理增伤%",
                    "魔法增伤%", "减防御%", "减物理防御%", "减魔法防御%",
                    "物攻", "魔攻", "__技能百分比前物攻", "__技能百分比前魔攻",
                }:
                    resolved_attrs[canonical_key] = (
                        resolved_attrs.get(canonical_key, 0.0) + average
                    )
                elif key == "最终面板%":
                    final += average
                elif key.endswith("攻%") and len(key) >= 3:
                    element_name = key[:-2]
                    share = target.get("elements", {}).get(element_name, 0.0) / max(1e-9, element_total)
                    element += average * share
                elif key.startswith("减") and key.endswith("抗%"):
                    attr = key[1:-2]
                    share = 1.0 if attr == "全" else target.get("elements", {}).get(attr, 0.0) / max(1e-9, element_total)
                    resistance += average * share
                elif "致命" in key:
                    crit += average
            physical_weight = max(0.0, as_number(target.get("physical")))
            magical_weight = max(0.0, as_number(target.get("magical")))
            attack = attack_increase_for_channel(
                resolved_attrs,
                "双攻",
                physical_weight=physical_weight,
                magical_weight=magical_weight,
            )
            flat_attack = fixed_attack_for_channel(
                resolved_attrs,
                "双攻",
                physical_weight=physical_weight,
                magical_weight=magical_weight,
            )
            amplified_flat = skill_percent_amplified_flat_attack(
                resolved_attrs,
                "双攻",
                physical_weight=physical_weight,
                magical_weight=magical_weight,
            )
            if flat_attack >= 0:
                amplified_flat = min(max(0.0, amplified_flat), flat_attack)
            else:
                amplified_flat = max(min(0.0, amplified_flat), flat_attack)
            post_percent_flat = flat_attack - amplified_flat
            amp = amplification_for_channel(
                resolved_attrs,
                "双攻",
                physical_weight=physical_weight,
                magical_weight=magical_weight,
            )
            defense = defense_reduction_for_attack(
                resolved_attrs,
                "双攻",
                physical_weight=physical_weight,
                magical_weight=magical_weight,
            )
            base_attack = max(1.0, as_number(target.get("base_attack"), 30000.0))
            attack_layer = (
                (base_attack + amplified_flat) * (1.0 + min(100.0, attack) / 100.0)
                + post_percent_flat
            ) / base_attack
            multiplier *= max(0.0, attack_layer) * (1.0 + final / 100.0)
            multiplier *= 1.0 + element / 100.0
            multiplier *= 1.0 + min(100.0, amp) / 100.0
            multiplier *= 1.0 + resistance / 100.0
            multiplier *= 1.0 + min(100.0, defense) / 200.0
            multiplier *= 1.0 + min(100.0, crit) / 300.0
            total += base * multiplier
        return total
    def optimize_team(
        self,
        payload: dict[str, Any],
        progress: Callable[[int, str], None],
    ) -> dict[str, Any]:
        raw_slots = payload.get("fixedSlots", payload.get("fixed", []))
        fixed_slots = [
            norm(value) if norm(value) and norm(value) not in {NONE_TEXT, "不限制"} else ""
            for value in (raw_slots if isinstance(raw_slots, list) else [])[:8]
        ]
        fixed_slots.extend([""] * (8 - len(fixed_slots)))
        fixed = [value for value in fixed_slots if value]
        if len(fixed) != len(set(fixed)):
            raise ValueError("固定职业里存在重复职业；打坐曜与输出曜可以同时存在")
        window = max(30.0, min(720.0, as_number(payload.get("window"), 125.0)))
        team_mode = norm(payload.get("teamMode")) or "不限制"
        if team_mode == "不限":
            team_mode = "不限制"
        require_tank = bool(payload.get("requireTank"))
        require_healer = bool(payload.get("requireHealer"))
        replacement_mode = norm(payload.get("replacementMode")) or "快速"
        replacement_candidates = max(0, min(12, int(as_number(payload.get("replacementCandidates"), 2))))
        objective = norm(payload.get("objective")) or "team"
        objective_job = norm(payload.get("objectiveJob"))
        if objective == "personal" and objective_job and objective_job not in fixed:
            first_free = next((index for index, value in enumerate(fixed_slots) if not value), 0)
            fixed_slots[first_free] = objective_job
            fixed.append(objective_job)
        hand_multipliers = payload.get("handMultipliers", {})
        hand_multipliers = hand_multipliers if isinstance(hand_multipliers, dict) else {}
        tank_jobs = {"毁灭者", "圣骑士"}
        healer_jobs = {"圣徒", "药剂师", "曜", "打坐曜"}
        jobs = [job for first in self.store.first_jobs() for job in self.store.second_jobs(first)]
        jobs = [job for job in jobs if job != "曜"] + ["打坐曜", "输出曜"]
        candidates: list[dict[str, Any]] = []
        profiles: dict[str, dict[str, Any]] = {}
        base = self.get_build("build1")
        conversion_runtime = self._custom_timeline_runtime(payload)
        total_jobs = max(1, len(jobs))
        for index, job in enumerate(jobs, 1):
            row = self._candidate_profile(
                job,
                window,
                base,
                conversion_runtime,
            )
            hand = max(0.0, min(10.0, as_number(hand_multipliers.get(job), 1.0)))
            row["damage"] = as_number(row.get("damage")) * hand
            row["dps"] = as_number(row.get("dps")) * hand
            profiles[job] = row
            candidates.append(row)
            progress(round(index * 45 / total_jobs), f"第一级：正在生成 {job} 基础画像（{index}/{len(jobs)}）")

        def mode_matches(job: str) -> bool:
            if team_mode == "不限制":
                return True
            profile = profiles.get(job, {})
            if team_mode == "物理队":
                return as_number(profile.get("physical")) >= as_number(profile.get("magical")) or job in tank_jobs | healer_jobs
            if team_mode == "魔法队":
                return as_number(profile.get("magical")) >= as_number(profile.get("physical")) or job in tank_jobs | healer_jobs
            element = team_mode[:1]
            return as_number(profile.get("elements", {}).get(element)) > 0 or job in tank_jobs | healer_jobs

        def valid_complete(team: list[str]) -> bool:
            return (not require_tank or any(job in tank_jobs for job in team)) and (
                not require_healer or any(job in healer_jobs for job in team)
            )

        def can_still_complete(team: list[str]) -> bool:
            """Keep enough open slots for every requested role.

            Correct panel-scaled damage can rank tanks below the old
            percentage-only candidates.  Without this feasibility guard the
            beam may prune every tank before the eighth slot and silently fall
            back to an invalid team.
            """

            slots_left = max(0, 8 - len(team))
            missing_roles = int(
                require_tank and not any(job in tank_jobs for job in team)
            ) + int(
                require_healer and not any(job in healer_jobs for job in team)
            )
            return missing_roles <= slots_left

        def preview_payload(team: list[str]) -> dict[str, Any]:
            remaining = list(team)
            for fixed_job in fixed_slots:
                if fixed_job and fixed_job in remaining:
                    remaining.remove(fixed_job)
            arranged: list[str] = []
            for fixed_job in fixed_slots:
                if fixed_job:
                    arranged.append(fixed_job)
                elif remaining:
                    arranged.append(remaining.pop(0))
            arranged.extend(remaining)
            value = copy.deepcopy(payload)
            value["members"] = arranged[:8]
            value["window"] = window
            raw_overrides = payload.get("buildOverrides", [])
            value["buildOverrides"] = [
                copy.deepcopy(raw_overrides[index])
                if isinstance(raw_overrides, list) and index < len(raw_overrides)
                else None
                for index in range(8)
            ]
            # Skill glyph optimization belongs to the verified final team.  It
            # must not be repeated for every beam/exact/replacement candidate.
            value["autoGlyphs"] = False
            return value

        def objective_score(preview: dict[str, Any]) -> float:
            if objective == "personal" and objective_job:
                return max(
                    (as_number(row.get("damage")) for row in preview.get("members", []) if norm(row.get("job")) == objective_job),
                    default=0.0,
                )
            return as_number(preview.get("total"))

        def cancellable_preview(
            team: list[str],
            stage_start: float,
            stage_span: float,
            stage_label: str,
            *,
            auto_glyphs: bool = False,
        ) -> dict[str, Any]:
            preview_request = preview_payload(team)
            preview_request["autoGlyphs"] = auto_glyphs

            def relay(value: int, message: str) -> None:
                mapped = stage_start + max(0, min(100, int(value))) * stage_span / 100.0
                progress(round(min(99.0, mapped)), f"{stage_label}｜{message}")

            return self.team_preview(preview_request, relay)

        ranked_jobs = [row["job"] for row in sorted(candidates, key=lambda item: as_number(item.get("damage")), reverse=True)]
        mode_jobs = [job for job in ranked_jobs if mode_matches(job)]
        if len(mode_jobs) >= max(8, 8 - len(fixed)):
            ranked_jobs = mode_jobs
        pool_size = min(len(ranked_jobs), max(14, 8 + len(fixed)))
        support_ranked = sorted(
            ranked_jobs,
            key=lambda job: self._approximate_team_damage([job, *ranked_jobs[:7]], profiles),
            reverse=True,
        )
        required_role_pool = [
            job
            for job in support_ranked
            if (
                (require_tank and job in tank_jobs)
                or (require_healer and job in healer_jobs)
            )
        ]
        pool = list(dict.fromkeys([
            *fixed,
            *required_role_pool,
            *ranked_jobs[:pool_size],
            *support_ranked[:pool_size],
        ]))[:20]
        beams: list[list[str]] = [list(fixed)]
        while len(beams[0]) < 8:
            expanded: list[tuple[float, list[str]]] = []
            for team in beams:
                for job in pool:
                    if job in team:
                        continue
                    trial = [*team, job]
                    if not can_still_complete(trial):
                        continue
                    expanded.append((self._approximate_team_damage(trial, profiles), trial))
            if not expanded:
                break
            expanded.sort(key=lambda item: item[0], reverse=True)
            dedup: list[list[str]] = []
            seen: set[tuple[str, ...]] = set()
            for _score, team in expanded:
                key = tuple(sorted(team))
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(team)
                if len(dedup) >= 10:
                    break
            beams = dedup
            progress(45 + round(len(beams[0]) * 3), f"第二级：正在联合筛选第 {len(beams[0])} 个位置")

        complete_beams = [team for team in beams if valid_complete(team)] or beams
        exact_limit = min(len(complete_beams), 10 if replacement_mode == "完整" else 6)
        exact_rows: list[tuple[float, list[str], dict[str, Any]]] = []
        for index, team in enumerate(complete_beams[:exact_limit], 1):
            arranged = preview_payload(team)["members"]
            exact_span = 18.0 / max(1, exact_limit)
            preview = cancellable_preview(
                team,
                70.0 + (index - 1) * exact_span,
                exact_span,
                f"第三级：完整模拟候选队 {index}/{exact_limit}",
            )
            exact_rows.append((objective_score(preview), arranged, preview))
            progress(70 + round(index * 18 / max(1, exact_limit)), f"第三级：完整模拟候选队 {index}/{exact_limit}")
        if exact_rows:
            exact_rows.sort(key=lambda item: item[0], reverse=True)
            selected = list(exact_rows[0][1])
            result = exact_rows[0][2]
        else:
            selected = list(fixed[:8])
            result = cancellable_preview(selected, 70.0, 18.0, "第三级：正在验证固定队伍")

        replaceable_slots = [index for index, fixed_job in enumerate(fixed_slots[:len(selected)]) if not fixed_job]
        alternatives = support_ranked[:max(4, replacement_candidates)]
        replacement_rows: list[dict[str, Any]] = []
        for position, slot in enumerate(replaceable_slots):
            trials = []
            for job in alternatives:
                if job != selected[slot] and job in selected:
                    continue
                trial = list(selected)
                trial[slot] = job
                trials.append((self._approximate_team_damage(trial, profiles), trial))
            if not trials:
                continue
            _approx, trial = max(trials, key=lambda item: item[0])
            if not valid_complete(trial):
                continue
            replacement_span = 6.0 / max(1, len(replaceable_slots))
            preview = cancellable_preview(
                trial,
                89.0 + position * replacement_span,
                replacement_span,
                "第三级：正在进行局部换人验证",
            )
            replacement_rows.append({
                "slot": slot + 1, "from": selected[slot], "to": trial[slot],
                "damage": as_number(preview.get("total")),
                "gain": as_number(preview.get("total")) / max(1e-9, as_number(result.get("total"))) - 1.0,
            })
            if objective_score(preview) > objective_score(result):
                selected, result = trial, preview
            progress(89 + round((position + 1) * 6 / max(1, len(replaceable_slots))), "第三级：正在进行局部换人验证")

        progress(96, "正在按长时间实战输出选择4块技能纹章")
        if bool(payload.get("autoGlyphs", True)):
            glyph_result = cancellable_preview(
                selected,
                96.0,
                3.0,
                "正在按长时间实战输出选择4块技能纹章",
                auto_glyphs=True,
            )
            if as_number(glyph_result.get("total")) >= as_number(result.get("total")):
                result = glyph_result
                glyph_strategy = "auto-4"
            else:
                # Automatic glyphs are an optional improvement. Keep the verified
                # complete simulation whenever a changed rotation would regress.
                glyph_strategy = "verified-baseline"
        else:
            glyph_strategy = "disabled"
        result["ranking"] = [
            {"job": row.get("job"), "damage": row.get("damage"), "dps": row.get("dps")}
            for row in sorted(candidates, key=lambda item: as_number(item.get("damage")), reverse=True)
        ]
        result["optimizer"] = "V4.10 adaptive three-stage optimizer"
        result["replacementAnalysis"] = replacement_rows
        result["optimizationSettings"] = {
            "teamMode": team_mode, "boss": norm(payload.get("boss")),
            "requireTank": require_tank, "requireHealer": require_healer,
            "replacementMode": replacement_mode, "replacementCandidates": replacement_candidates,
            "objective": objective,
        }
        result["stages"] = {
            "profiles": len(profiles), "candidatePool": len(pool),
            "exactTeams": len(exact_rows), "selected": selected,
            "glyphStrategy": glyph_strategy,
        }
        return result


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeTeamMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
