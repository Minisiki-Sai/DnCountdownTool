from __future__ import annotations

from functools import lru_cache

class ScoreGlyphsMixin:
    def _apply_best_glyphs(
        self, job: str, profiles: list[SkillProfile]
    ) -> list[SkillProfile]:
        by_key = {row.key: row for row in profiles}

        def linked_cooldown_dps(skill: str) -> float:
            linked_keys = {
                canonical_skill(name)
                for name in linked_cooldown_glyph_skills(job, skill)
                if canonical_skill(name)
            }
            return sum(
                max(0.0, as_number(by_key[key].dps))
                for key in linked_keys
                if key in by_key
            )

        choices: list[dict[str, Any]] = []
        for glyph in self.store.skill_glyphs(job):
            key = canonical_skill(glyph.get("skill"))
            profile = by_key.get(key)
            if profile is None:
                continue
            evaluated: list[tuple[float, str, float]] = []
            for raw_effect_name, values in (glyph.get("effects") or {}).items():
                effect_name = (
                    "增益持续时间增加"
                    if norm(raw_effect_name) in {"增益持续", "增益持续时间增加"}
                    else norm(raw_effect_name)
                )
                if effect_name not in {
                    "伤害增加",
                    "冷却缩减",
                    "增益持续时间增加",
                    "动作速度增加",
                }:
                    continue
                value = max(
                    (as_number(row.get("value")) for row in values if isinstance(row, dict)),
                    default=0.0,
                )
                if value <= 0:
                    continue
                if effect_name == "伤害增加":
                    gain = profile.dps * value / 100.0
                elif effect_name == "冷却缩减" and value < 100:
                    package_dps = profile.dps + linked_cooldown_dps(profile.name)
                    gain = package_dps * (
                        1.0 / (1.0 - value / 100.0) - 1.0
                    )
                elif effect_name == "动作速度增加":
                    cast_ratio = self._action_speed_glyph_cast_ratio(
                        self.store, job, profile.name, value
                    )
                    gain = profile.dps * max(0.0, 1.0 - cast_ratio)
                else:
                    timing = self.store.skill_effect_timing(job, profile.name)
                    has_duration = (
                        as_number(timing.get("duration")) > 0
                        or profile.key in _scoring_refreshable_periodic_durations()
                    )
                    gain = profile.dps * value / 100.0 if has_duration else 0.0
                gain *= glyph_marginal_score_multiplier(
                    profile.name,
                    effect_name,
                    job,
                )
                evaluated.append((gain, effect_name, value))
            choices.extend(
                {
                    "skill": key,
                    "effect": effect,
                    "value": value,
                    "score": gain,
                    "local_score": gain,
                }
                for gain, effect, value in evaluated
            )
        chosen = select_skill_glyphs_two_stage(
            apply_score_glyph_exclusions(choices, job),
            4,
        )
        selected = {
            str(row.get("skill") or ""): (
                str(row.get("effect") or ""),
                max(0.0, as_number(row.get("value"))),
            )
            for row in chosen
        }
        result: list[SkillProfile] = []
        for profile in profiles:
            selection = selected.get(profile.key)
            if selection is None:
                result.append(profile)
                continue
            effect, value = selection
            power = profile.power
            cooldown = profile.cooldown
            cast_time = profile.cast_time
            if effect == "伤害增加":
                power *= 1.0 + value / 100.0
            elif effect == "冷却缩减":
                cooldown *= max(0.0, 1.0 - value / 100.0)
            elif effect == "增益持续时间增加":
                power *= 1.0 + value / 100.0
            elif effect == "动作速度增加":
                cast_time *= self._action_speed_glyph_cast_ratio(
                    self.store, job, profile.name, value
                )
            result.append(
                SkillProfile(
                    profile.name,
                    profile.key,
                    power,
                    cooldown,
                    cast_time,
                    effect,
                    value,
                )
            )
        return result
    @staticmethod
    def _apply_selected_glyphs(
        profiles: list[SkillProfile],
        glyph_rows: Iterable[Any],
        job: str = "",
        store: Any | None = None,
    ) -> list[SkillProfile]:
        selected: dict[str, tuple[str, float]] = {}
        for row in glyph_rows:
            if not isinstance(row, dict):
                continue
            key = canonical_skill(row.get("skill"))
            effect = norm(row.get("effect"))
            if effect in {"增益持续", "增益持续时间增加"}:
                effect = "增益持续时间增加"
            value = max(0.0, as_number(row.get("value")))
            if key and effect and value > 0:
                selected[key] = (effect, value)

        result: list[SkillProfile] = []
        for profile in profiles:
            selection = selected.get(profile.key)
            if selection is None:
                result.append(profile)
                continue
            effect, value = selection
            power = profile.power
            cooldown = profile.cooldown
            cast_time = profile.cast_time
            if effect == "伤害增加":
                power *= 1.0 + value / 100.0
            elif effect == "冷却缩减":
                cooldown *= max(0.0, 1.0 - value / 100.0)
            elif effect == "增益持续时间增加":
                power *= 1.0 + value / 100.0
            elif effect == "动作速度增加":
                cast_time *= TeamScoreEngine._action_speed_glyph_cast_ratio(
                    store, job, profile.name, value
                )
            result.append(
                SkillProfile(
                    profile.name,
                    profile.key,
                    power,
                    cooldown,
                    cast_time,
                    effect,
                    value,
                )
            )
        return result
    @staticmethod
    def _action_speed_glyph_cast_ratio(
        store: Any | None, job: str, skill: str, value: float
    ) -> float:
        if store is not None:
            timing = store.skill_cast_time(skill, [job] if norm(job) else [])
            base_cast = store.effective_skill_cast_time(timing)
            accelerated_cast = store.effective_skill_cast_time(timing, value)
            if base_cast > 0 and accelerated_cast > 0:
                return max(0.0, min(1.0, accelerated_cast / base_cast))
        return 1.0 / (1.0 + max(0.0, as_number(value)) / 100.0)
    @staticmethod
    def _glyph_display_rows(glyph_rows: Iterable[Any]) -> tuple[str, ...]:
        result: list[str] = []
        for row in glyph_rows:
            if not isinstance(row, dict):
                continue
            skill = norm(row.get("skill"))
            effect = norm(row.get("effect"))
            option = norm(row.get("option"))
            value = max(0.0, as_number(row.get("value")))
            if not skill or not effect or value <= 0:
                continue
            rank = f"[{option}] " if option else ""
            result.append(f"{rank}{skill}：{effect}+{value:g}%")
        return tuple(result)


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'ScoreGlyphsMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from scoring.py; algorithm bodies are unchanged.
