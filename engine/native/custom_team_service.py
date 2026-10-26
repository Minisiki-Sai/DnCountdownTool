from __future__ import annotations

from functools import lru_cache

from ..acceleration_resistance import is_acceleration_resistance_skill

class NativeCustomTeamMixin:
    @staticmethod
    def _payload_team_members(payload: dict[str, Any]) -> list[str]:
        members: list[str] = []
        for raw in payload.get("members", []):
            value = raw.get("job") if isinstance(raw, dict) else raw
            job = norm(value)
            if job and job != NONE_TEXT:
                members.append(job)
        return members[:8]
    def _custom_timeline_runtime(
        self,
        payload: dict[str, Any],
    ) -> PureTimelineRuntime:
        """Create the exact V4.01 extreme/custom-axis runtime without Qt state."""

        build = self.get_build("build1")
        boss = norm(payload.get("boss")) or norm(build.get("boss")) or "无"
        if boss in BOSS_PRESETS:
            preset = BOSS_PRESETS[boss]
            build["boss"] = boss
            build["crit_resist_value"] = as_number(preset.get("crit_resist"))
            build["monster_defense"] = as_number(preset.get("monster_defense"))
            build["element_resists"] = {
                element: as_number(preset.get("element_resist"))
                for element in ("火", "水", "光", "暗")
            }
            build["freeze_coefficient"] = as_number(
                payload.get("freezeCoefficient"),
                as_number(preset.get("freeze_coefficient"), 0.3),
            )
        snapshot = TimelineSimulationSnapshot.create(
            build,
            [],
            {"_timeline_extreme_mode": True},
        )
        source_type = self.legacy_timeline._source_type()
        runtime = PureTimelineRuntime(
            snapshot,
            store=self.store,
            calculator=self.calculator,
            cooldown_engine=self.legacy_timeline.cooldown_engine,
            method_source_type=source_type,
            level_cap=70,
        )
        # Class constants are not descriptors, so expose the one used by the
        # process-safe eight-character executor explicitly.
        runtime._EXTREME_PARALLEL_RUNTIME_ATTRS = source_type._EXTREME_PARALLEL_RUNTIME_ATTRS
        runtime.skill_long_accel_start = TimelineValue(60.0)
        runtime.team_extreme_aura_choices = copy.deepcopy(
            payload.get("auraChoices", {})
            if isinstance(payload.get("auraChoices"), dict)
            else {}
        )
        runtime._reset_extreme_timeline_context(boss)
        return runtime
    def _custom_selected_effects(
        self,
        runtime: PureTimelineRuntime,
        members: list[str],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply the V4.01 team-effect table choices to extreme-engine rows."""

        raw_effects = runtime._dedup_team_effects(
            runtime._team_effects_for_jobs(members)
        )
        catalog = {
            norm(row.get("id")): row
            for row in self.legacy_timeline.team_effect_catalog(
                self.get_build("build1"), members, payload
            )
            if norm(row.get("id"))
        }
        selected: list[dict[str, Any]] = []
        for effect in raw_effects:
            skill = runtime._team_effect_skill_name(effect.get("skill"))
            effect_id = f"{norm(effect.get('provider'))}|{skill}"
            configured = catalog.get(effect_id)
            if configured is not None and not bool(configured.get("enabled", True)):
                continue
            copied = copy.deepcopy(effect)
            if configured is not None:
                copied["level"] = norm(configured.get("level")) or copied.get("level")
                copied["cooldown"] = max(0.0, as_number(configured.get("cooldown")))
                copied["duration"] = max(0.0, as_number(configured.get("duration")))
                copied["usage_ratio"] = max(
                    0.0, min(1.0, as_number(configured.get("usageRatio"), 1.0))
                )
                attrs = configured.get("attrs")
                if isinstance(attrs, dict):
                    copied["attrs"] = copy.deepcopy(attrs)
                    copied["display_attrs"] = copy.deepcopy(attrs)
            selected.append(copied)
        cdr_applies = payload.get("cdrApplies", {})
        cdr_applies = cdr_applies if isinstance(cdr_applies, dict) else {}
        applied = {
            runtime._team_effect_skill_name(effect.get("skill"))
            for effect in selected
            if bool(cdr_applies.get(runtime._team_effect_skill_name(effect.get("skill")), False))
        }
        return runtime._team_effects_with_cdr_adjusted_cooldowns(selected, applied)
    @staticmethod
    def _schedule_rows_to_casts(
        runtime: PureTimelineRuntime,
        rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[tuple[float, float]]]:
        schedule: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for row in rows:
            provider = norm(row.get("provider"))
            skill = runtime._skill_burst_skill_key(row.get("skill"))
            if provider and skill:
                schedule.setdefault((provider, skill), []).append((
                    as_number(row.get("cast_start")),
                    as_number(row.get("cast_end")),
                ))
        return schedule
    def _custom_team_buff_specs(
        self,
        runtime: PureTimelineRuntime,
        effects: list[dict[str, Any]],
        members: list[str],
        timelines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Mirror V4.01's intermittent team-buff optimizer input."""

        result_by_job = {norm(row.get("job")): row for row in timelines}
        specs: list[dict[str, Any]] = []
        for effect in effects:
            skill = runtime._skill_burst_skill_key(
                runtime._team_effect_skill_name(effect.get("skill"))
            )
            _effect_type, category = runtime._team_practical_effect_type(effect)
            if (
                not skill
                or category != "team_buff"
                or skill in {"鸡尾酒", "查克拉治愈术", "阿兹那之息"}
                or bool(effect.get("full_time_equivalent"))
                or runtime._team_practical_is_continuous_effect(effect)
                or not runtime._team_practical_effect_affects_output(effect)
            ):
                continue
            cooldown = max(0.0, as_number(effect.get("cooldown")))
            duration = max(0.0, as_number(effect.get("duration")))
            if cooldown <= 0.0 or duration <= 0.0:
                continue
            attrs = {
                norm(key): as_number(value)
                for key, value in {
                    **(effect.get("display_attrs", {}) or {}),
                    **(effect.get("attrs", {}) or {}),
                }.items()
                if norm(key) and abs(as_number(value)) > 1e-9
            }
            job_weights: dict[str, float] = {}
            for job in members:
                base_output = max(
                    1.0,
                    as_number(result_by_job.get(norm(job), {}).get("damage_panel"), 1.0),
                )
                baseline = max(
                    1e-9,
                    as_number(runtime._team_extreme_job_breakdown(
                        job, {}, base_output,
                        include_boss=True,
                        include_base_element=False,
                        base_neutral_percent=as_number(result_by_job.get(norm(job), {}).get('summary', {}).get('无属性%')),
                    ).get("total_multiplier"), 1.0),
                )
                enhanced = as_number(runtime._team_extreme_job_breakdown(
                    job, attrs, base_output,
                    include_boss=True,
                    include_base_element=False,
                    base_neutral_percent=as_number(result_by_job.get(norm(job), {}).get('summary', {}).get('无属性%')),
                ).get("total_multiplier"), 1.0)
                gain = max(0.0, enhanced / baseline - 1.0)
                if gain > 1e-9:
                    job_weights[job] = gain
            if any(
                token in attr
                for attr in attrs
                for token in ("冷却缩减", "动作速度", "攻速")
            ):
                cdr = min(70.0, max(
                    [abs(value) for attr, value in attrs.items() if "冷却缩减" in attr]
                    or [0.0]
                )) / 100.0
                speed = max(
                    [abs(value) for attr, value in attrs.items() if "动作速度" in attr or "攻速" in attr]
                    or [0.0]
                ) / 100.0
                usage = max(0.0, min(1.0, as_number(effect.get("usage_ratio"), 1.0)))
                timeline_gain = min(
                    3.0,
                    (1.0 / max(0.3, 1.0 - cdr) - 1.0) * usage
                    + speed * usage * 0.35,
                )
                for job in members:
                    job_weights[job] = max(job_weights.get(job, 0.0), timeline_gain)
            if not job_weights:
                continue
            specs.append({
                "provider": norm(effect.get("provider")),
                "skill": skill,
                "stack_group": runtime._skill_burst_exclusive_effect_group(skill) or skill,
                "cooldown": cooldown,
                "duration": duration,
                "cast_time": runtime._team_practical_effect_cast_time(effect),
                "job_weights": job_weights,
            })
        return specs
    @staticmethod
    def _custom_timeline_payload(row: dict[str, Any]) -> dict[str, Any]:
        window = max(1.0, as_number(row.get("window"), 1.0))
        skill_damages = {
            norm(skill): max(0.0, as_number(value))
            for skill, value in (row.get("_score_skill_damages", {}) or {}).items()
            if norm(skill) and as_number(value) > 0
        }
        counts = Counter(
            norm(segment.get("skill"))
            for segment in row.get("segments", [])
            if norm(segment.get("skill")) and not segment.get("timeline_hidden")
        )
        total_damage = sum(skill_damages.values())
        contribution = [
            {
                "skill": skill,
                "count": counts.get(skill, 0),
                "output": damage,
                "share": damage / total_damage if total_damage else 0.0,
            }
            for skill, damage in sorted(
                skill_damages.items(), key=lambda item: item[1], reverse=True
            )
        ]
        segments = []
        for segment in row.get("segments", []):
            if segment.get("timeline_hidden"):
                continue
            copied = copy.deepcopy(segment)
            copied["effectiveScore"] = as_number(
                copied.get("effectiveScore"),
                as_number(copied.get("effective_score")),
            )
            copied["output"] = max(
                0.0,
                as_number(
                    copied.get("output"),
                    as_number(copied.get("_score_actual_damage")),
                ),
            )
            copied["actionSpeed"] = as_number(
                copied.get("actionSpeed"),
                as_number(copied.get("action_speed_percent")),
            )
            segments.append(copied)
        damage = max(0.0, as_number(row.get("dps"))) * window
        return {
            "job": norm(row.get("job")),
            "window": window,
            "segments": segments,
            "total": damage,
            "dps": max(0.0, as_number(row.get("dps"))),
            "contribution": contribution,
            "effectWindows": copy.deepcopy([
                *(row.get("effect_windows", []) or []),
                *(row.get("external_windows", []) or []),
            ]),
            "planner": "V4.01自选机制轴同源规划器",
            "legacyParity": True,
        }
    def custom_team_preview(
        self,
        payload: dict[str, Any],
        progress: Callable[[int, str], None],
    ) -> dict[str, Any]:
        """Run the complete V4.01 custom-mechanism team planning pipeline."""

        members = self._payload_team_members(payload)
        if len(members) != 8:
            raise ValueError("自选时间轴需要先选择完整的八人队伍")
        window = max(180.0, min(720.0, as_number(payload.get("window"), 600.0)))
        intervals = normalize_mechanism_intervals(
            payload.get("mechanisms") if isinstance(payload.get("mechanisms"), list) else [],
            window,
        )
        cache_payload = copy.deepcopy(payload)
        cache_payload["window"] = window
        cache_payload["mechanisms"] = intervals
        cache_payload.pop("baselineDpsByJob", None)
        cache_key = hashlib.sha256(json.dumps(
            cache_payload, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        cached = self._custom_timeline_cache.get(cache_key)
        if cached is not None:
            progress(100, "已复用相同队伍与机制设置的精确结果")
            result = copy.deepcopy(cached)
            result["comparison"] = self._custom_comparison(
                result.get("members", []), payload.get("baselineDpsByJob", {})
            )
            return result

        progress(3, "正在准备 V4.01 自选机制轴核心")
        runtime = self._custom_timeline_runtime(payload)
        effects = self._custom_selected_effects(runtime, members, payload)
        locks = {
            (norm(row.get("provider")), norm(row.get("skill")), int(as_number(row.get("occurrence")))):
            max(0.0, as_number(row.get("start")))
            for row in payload.get("manualBuffLocks", [])
            if isinstance(row, dict) and norm(row.get("provider")) and norm(row.get("skill"))
        }
        cdr_specs = []
        for effect in effects:
            skill = runtime._skill_burst_skill_key(
                runtime._team_effect_skill_name(effect.get("skill"))
            )
            if is_acceleration_resistance_skill(skill):
                cdr_specs.append({
                    "provider": norm(effect.get("provider")),
                    "skill": skill,
                    "cooldown": max(0.01, as_number(effect.get("cooldown"))),
                    "duration": max(0.0, as_number(effect.get("duration"))),
                    "cast_time": runtime._team_practical_effect_cast_time(effect),
                })
        cdr_rows = lightweight_cdr_buff_schedule(
            cdr_specs, intervals, window,
            {
                "精神爆发": 0.0,
                "变异精神爆发": 0.0,
                "灼热火焰": 0.0,
                "变异猎人之眼": 0.0,
                "时间加速": 0.0,
                "变异时间加速": 0.0,
            },
            locks,
        )
        fallback_schedule = self._schedule_rows_to_casts(runtime, cdr_rows)
        progress(10, f"减CD预排程完成，共 {len(cdr_rows)} 次施放")

        hand_values = payload.get("handMultipliers", {})
        hand_values = hand_values if isinstance(hand_values, dict) else {}
        build_overrides = self._team_payload_build_overrides(payload, members)

        def task_specs(schedule: dict[tuple[str, str], list[tuple[float, float]]], partial: bool) -> list[dict[str, Any]]:
            specs = []
            for index, job in enumerate(members):
                spec: dict[str, Any] = {
                    "job": job,
                    "team": members,
                    "effects": effects,
                    "hand_multiplier": max(0.0, as_number(hand_values.get(job), 1.0)),
                    "window": window,
                    "blocked_intervals": intervals,
                    "cast_schedule": schedule,
                    "partial_cast_schedule": partial,
                    "include_display_windows": False,
                    "fast_long_timeline": True,
                }
                if index < len(build_overrides) and isinstance(build_overrides[index], dict):
                    spec["build_override"] = build_overrides[index]
                specs.append(spec)
            return specs

        runtime._extreme_timeline_process_events = False
        initial = runtime._extreme_timeline_tasks_parallel(
            task_specs(fallback_schedule, True),
            progress_callback=lambda done, _index, job: progress(
                10 + round(done * 28 / len(members)),
                f"正在生成 {job} 的自选机制实战轴（{done}/{len(members)}）",
            ),
        )
        if len(initial) != len(members):
            raise RuntimeError("自选机制轴未能生成完整八职业结果")

        progress(40, "正在按八人伤害密度联合优化团队 Buff")
        buff_specs = self._custom_team_buff_specs(runtime, effects, members, initial)
        optimized_rows = lightweight_team_buff_schedule(
            buff_specs, intervals, window, initial,
            seed_schedule=None,
            preferred_starts={
                "精神爆发": 0.0,
                "变异精神爆发": 0.0,
                "灼热火焰": 0.0,
                "变异猎人之眼": 0.0,
                "时间加速": 0.0,
                "变异时间加速": 0.0,
            },
            locked_starts=locks,
        )
        optimized_schedule = self._schedule_rows_to_casts(runtime, optimized_rows)
        hand_by_job = {
            job: max(0.0, as_number(hand_values.get(job), 1.0)) for job in members
        }
        progress(48, "正在同步八职业团队效果时机")
        optimized = runtime._extreme_synchronized_team_timelines(
            members,
            effects,
            hand_by_job,
            initial,
            window,
            progress_callback=lambda index, job: progress(
                48 + round((index + 1) * 34 / len(members)),
                f"正在同步 {job} 的团队效果时机",
            ),
            build_overrides=build_overrides,
            blocked_intervals=intervals,
            pinned_cast_schedule=optimized_schedule,
            max_passes=1,
            fast_long_timeline=True,
        )
        optimized_total = sum(max(0.0, as_number(row.get("dps"))) for row in optimized)
        initial_total = sum(max(0.0, as_number(row.get("dps"))) for row in initial)
        schedule_rows = optimized_rows
        schedule_mode = "稳定初始轴 + 受限联合优化"
        final_rows = optimized
        if optimized_total < initial_total - 1e-6:
            progress(84, "联合优化未提升输出，正在恢复稳定减CD日程")
            fallback = runtime._extreme_synchronized_team_timelines(
                members, effects, hand_by_job, initial, window,
                build_overrides=build_overrides,
                blocked_intervals=intervals,
                pinned_cast_schedule=fallback_schedule,
                max_passes=1,
                fast_long_timeline=True,
            )
            fallback_total = sum(max(0.0, as_number(row.get("dps"))) for row in fallback)
            if fallback_total >= optimized_total - 1e-6:
                final_rows = fallback
                schedule_rows = cdr_rows
                schedule_mode = "稳定减CD日程（自动回退）"

        progress(92, "正在整理自选时间轴与职业对比")
        members_payload = []
        total = 0.0
        for row in final_rows:
            timeline = self._custom_timeline_payload(row)
            damage = as_number(timeline.get("total"))
            total += damage
            members_payload.append({
                "job": norm(row.get("job")),
                "damage": damage,
                "dps": as_number(timeline.get("dps")),
                "timeline": timeline,
                "handMultiplier": max(0.0, as_number(hand_values.get(norm(row.get("job"))), 1.0)),
            })
        for row in members_payload:
            row["share"] = as_number(row.get("damage")) / total if total else 0.0
        active = mechanism_active_seconds(intervals, window)
        result = {
            "members": members_payload,
            "window": window,
            "total": total,
            "dps": total / window if window else 0.0,
            "activeDps": total / active if active > 1e-9 else 0.0,
            "activeSeconds": active,
            "blockedSeconds": mechanism_blocked_seconds(intervals, window),
            "mechanisms": intervals,
            "buffSchedule": schedule_rows,
            "scheduleMode": schedule_mode,
            "planner": "V4.01自选机制轴完整同源管线",
            "settings": {
                "boss": norm(payload.get("boss")),
                "freezeCoefficient": as_number(payload.get("freezeCoefficient"), 0.3),
            },
        }
        result["comparison"] = self._custom_comparison(
            members_payload, payload.get("baselineDpsByJob", {})
        )
        self._custom_timeline_cache[cache_key] = copy.deepcopy(result)
        while len(self._custom_timeline_cache) > 4:
            self._custom_timeline_cache.pop(next(iter(self._custom_timeline_cache)), None)
        progress(100, "自选机制八人时间轴已完成")
        return result
    @staticmethod
    def _custom_comparison(
        custom_members: list[dict[str, Any]],
        baseline: Any,
    ) -> list[dict[str, Any]]:
        baseline = baseline if isinstance(baseline, dict) else {}
        rows = []
        total_custom = sum(max(0.0, as_number(row.get("dps"))) for row in custom_members)
        for row in custom_members:
            job = norm(row.get("job"))
            standard = max(0.0, as_number(baseline.get(job)))
            custom = max(0.0, as_number(row.get("dps")))
            loss = max(0.0, standard - custom)
            rows.append({
                "job": job,
                "standardDps": standard,
                "customDps": custom,
                "loss": loss,
                "lossRate": loss / standard if standard > 0 else 0.0,
                "share": custom / total_custom if total_custom > 0 else 0.0,
            })
        return rows
    def _cached_personal_timeline(
        self,
        build: dict[str, Any],
        window: float,
    ) -> dict[str, Any]:
        """Return the exact personal long axis without team effects."""

        cache_build = copy.deepcopy(build)
        cache_build.pop("team_sim", None)
        cache_key = hashlib.sha256(
            json.dumps(
                {"build": cache_build, "window": round(float(window), 4)},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cached = self._personal_timeline_cache.get(cache_key)
        if cached is None:
            cached = self.quick_timeline(
                copy.deepcopy(build),
                {"mode": "long", "window": window, "cocktail": True},
            )
            self._personal_timeline_cache[cache_key] = copy.deepcopy(cached)
            if len(self._personal_timeline_cache) > 256:
                oldest = next(iter(self._personal_timeline_cache))
                self._personal_timeline_cache.pop(oldest, None)
        return copy.deepcopy(cached)


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeCustomTeamMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
