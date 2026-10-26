from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .effect_stacking import (
    amplification_for_channel,
    attack_increase_for_channel,
)


_DAMAGE_EFFECT = "伤害增加"
_COOLDOWN_EFFECT = "冷却缩减"
_ACTION_SPEED_EFFECTS = frozenset({"动作速度增加", "动作速度"})


def required_timing_skill_glyphs(job: Any, first_job: Any = "") -> tuple[tuple[str, str], ...]:
    """Required scoring assumptions, not evidence of equipped plates."""
    job, first_job = str(job or "").strip(), str(first_job or "").strip()
    if first_job == "游侠" or job in {"游侠", "影舞者", "风行者"}:
        return (("精神爆发", _COOLDOWN_EFFECT),)
    if first_job == "魔导师" or job in {"魔导师", "时空领主", "黑暗女王"}:
        return (("时间加速", _COOLDOWN_EFFECT),)
    return ()


def _glyph_policy_skill_key(value: Any) -> str:
    """Return the small canonical key needed by glyph-only policies."""

    text = "".join(str(value or "").split()).upper()
    return text[:-2] if text.endswith("EX") else text


_LINKED_COOLDOWN_GLYPH_SKILLS = {
    # 神圣大地 is the ten-second enable window for both Thunderer's large
    # follow-up attacks.  Its cooldown plate therefore owns the marginal
    # casts of the complete package, not only Holy Ground's own damage.
    ("雷神", "神圣大地"): ("圣光十字", "神圣暴风", "神圣风暴"),
}


def linked_cooldown_glyph_skills(job: Any, skill: Any) -> tuple[str, ...]:
    """Return attacks whose usable casts are gated by one cooldown skill."""

    return _LINKED_COOLDOWN_GLYPH_SKILLS.get(
        (str(job or "").strip(), _glyph_policy_skill_key(skill)),
        (),
    )


def glyph_marginal_score_multiplier(
    skill: Any,
    effect: Any = "",
    job: Any = "",
) -> float:
    """Return a practical confidence weight for one measured glyph gain.

    Chain Lightning is highly range/target-shape dependent and real captures
    use it much less often than a cooldown-only estimate assumes.  Keep it as
    a normal candidate, but count only half of its measured plate benefit in
    every automatic selector.
    """

    del effect, job
    return 0.5 if _glyph_policy_skill_key(skill) == "连锁闪电" else 1.0


def apply_practical_action_speed_tiebreak(
    candidates: Iterable[dict[str, Any]],
    *,
    score_key: str = "local_score",
    near_tie_ratio: float = 0.90,
) -> list[dict[str, Any]]:
    """Prefer a 50% action-speed plate over a near-tied 20% damage plate.

    A fixed-axis local estimate can value time freed *inside* the axis, but it
    cannot see the practical value of completing a long cast before the next
    Boss stop boundary.  Treat the two choices as a near tie when the measured
    action-speed gain reaches 90% of the damage gain.  This keeps materially
    stronger damage plates unchanged while preventing a small modelling blind
    spot from selecting damage for long casts such as 灵巧猎手.
    """

    rows = [row for row in candidates if isinstance(row, dict)]
    threshold = min(1.0, max(0.0, float(near_tie_ratio)))
    by_skill: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        skill = str(row.get("skill") or "").strip()
        if skill:
            by_skill.setdefault(skill, []).append(row)

    def score(row: dict[str, Any]) -> float:
        try:
            return max(0.0, float(row.get(score_key) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    preferred_speed_skills: set[str] = set()
    for skill, skill_rows in by_skill.items():
        damage_rows = [
            row
            for row in skill_rows
            if str(row.get("effect") or "").strip() == _DAMAGE_EFFECT
            and abs(float(row.get("value") or 0.0) - 20.0) <= 1e-6
        ]
        speed_rows = [
            row
            for row in skill_rows
            if str(row.get("effect") or "").strip() in _ACTION_SPEED_EFFECTS
            and float(row.get("value") or 0.0) >= 50.0 - 1e-6
        ]
        if not damage_rows or not speed_rows:
            continue
        best_damage = max(score(row) for row in damage_rows)
        best_speed = max(score(row) for row in speed_rows)
        if (
            best_damage > 1e-9
            and best_speed > 1e-9
            and best_speed + 1e-9 >= best_damage * threshold
        ):
            preferred_speed_skills.add(skill)

    if not preferred_speed_skills:
        return rows
    return [
        row
        for row in rows
        if not (
            str(row.get("skill") or "").strip() in preferred_speed_skills
            and str(row.get("effect") or "").strip() == _DAMAGE_EFFECT
            and abs(float(row.get("value") or 0.0) - 20.0) <= 1e-6
        )
    ]


def damage_glyph_multiplier(value: Any, final_percent: Any) -> float:
    """Return the independent multiplier of a skill-damage plate.

    Skill-damage plates scale the affected skill after the character's final
    damage has been applied.  They therefore must not be added to, or diluted
    by, the global final-damage bucket.  ``final_percent`` is retained in the
    public signature because timeline and score callers share this helper.
    """

    bonus = max(0.0, float(value or 0.0))
    del final_percent
    return 1.0 + bonus / 100.0


def select_fixed_axis_damage_glyphs(
    candidates: Iterable[dict[str, Any]],
    count: int,
    contributions: dict[str, Any],
    final_percent: Any,
    *,
    occupied_skills: Iterable[Any] = (),
    job: Any = "",
) -> list[dict[str, Any]]:
    """Choose damage plates from damage already produced on one fixed axis.

    ``skill_key`` may be supplied on a candidate when its display name differs
    from the canonical timeline key.  Cooldown/damage pair preferences are
    resolved before the damage-only ranking, and a skill already occupying a
    plate slot with a timing plate cannot take a second slot.
    """

    limit = max(0, int(count or 0))
    if limit <= 0:
        return []
    contribution_by_skill = {
        str(skill or "").strip(): max(0.0, float(value or 0.0))
        for skill, value in contributions.items()
        if str(skill or "").strip()
    }
    occupied = {
        str(skill or "").strip()
        for skill in occupied_skills
        if str(skill or "").strip()
    }
    scored: list[dict[str, Any]] = []
    for row in apply_damage_cooldown_glyph_policy(candidates):
        if str(row.get("effect") or "").strip() != _DAMAGE_EFFECT:
            continue
        skill = str(row.get("skill") or "").strip()
        skill_key = str(row.get("skill_key") or skill).strip()
        if not skill or not skill_key or skill_key in occupied:
            continue
        contribution = contribution_by_skill.get(skill_key, 0.0)
        value = max(0.0, float(row.get("value") or 0.0))
        gain = contribution * (
            damage_glyph_multiplier(value, final_percent) - 1.0
        )
        gain *= glyph_marginal_score_multiplier(skill, row.get("effect"), job)
        if gain <= 0:
            continue
        copied = dict(row)
        copied["score"] = gain
        scored.append(copied)
    return select_skill_glyphs(scored, limit, score_key="score")


def score_glyph_exclusions(job: Any) -> set[tuple[str, str]]:
    """Return score-only glyph choices that have no measurable axis value.

    These exclusions are shared by the full theory planner and the compact
    scoring fallback.  Keeping them here prevents a fallback or a cached
    recomputation from bringing back a utility/filler glyph that the complete
    score axis has already proved worthless.
    """

    job_name = str(job or "").strip()
    excluded = {("幽灵之魂", "增益持续")}
    if job_name in {"圣骑士", "十字军"}:
        # Stomp is only a learned low-value filler for both Paladin branches.
        # Its standalone cast-time estimate must not consume a score glyph
        # slot when the long rotation does not obtain measurable output from it.
        excluded.update({
            ("践踏", "动作速度增加"),
            ("践踏", "动作速度"),
        })
    if job_name == "银色猎人":
        excluded.update({
            ("猎人翻滚", "冷却缩减"),
            ("猎人翻滚", "冷却时间减少"),
        })
    return excluded


def apply_score_glyph_exclusions(
    candidates: Iterable[dict[str, Any]],
    job: Any,
) -> list[dict[str, Any]]:
    """Filter score candidates with the shared job-specific exclusions."""

    excluded = score_glyph_exclusions(job)
    return [
        row
        for row in candidates
        if isinstance(row, dict)
        and (
            str(row.get("skill") or "").strip(),
            str(row.get("effect") or "").strip(),
        ) not in excluded
    ]


def preferred_damage_cooldown_glyph(
    choices: Iterable[tuple[Any, Any]],
) -> tuple[str, float] | None:
    """Return the fixed user preference for a damage/CD plate pair.

    Only the two explicitly defined comparisons are overridden. Other plate
    values continue through the normal timeline-benefit calculation.
    """

    normalized = {
        (str(effect or "").strip(), round(max(0.0, float(value or 0.0)), 6))
        for effect, value in choices
    }
    damage_20 = (_DAMAGE_EFFECT, 20.0)
    cooldown_20 = (_COOLDOWN_EFFECT, 20.0)
    cooldown_12 = (_COOLDOWN_EFFECT, 12.0)
    if damage_20 in normalized and cooldown_20 in normalized:
        return cooldown_20
    if damage_20 in normalized and cooldown_12 in normalized:
        return damage_20
    return None


def apply_damage_cooldown_glyph_policy(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove the losing side of a defined same-skill damage/CD choice."""

    rows = [row for row in candidates if isinstance(row, dict)]
    by_skill: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        skill = str(row.get("skill") or "").strip()
        if skill:
            by_skill.setdefault(skill, []).append(row)
    preferred = {
        skill: preferred_damage_cooldown_glyph(
            (row.get("effect"), row.get("value")) for row in skill_rows
        )
        for skill, skill_rows in by_skill.items()
    }
    result: list[dict[str, Any]] = []
    for row in rows:
        skill = str(row.get("skill") or "").strip()
        selection = preferred.get(skill)
        effect = str(row.get("effect") or "").strip()
        value = round(max(0.0, float(row.get("value") or 0.0)), 6)
        if selection is not None and effect in {_DAMAGE_EFFECT, _COOLDOWN_EFFECT}:
            if (effect, value) != selection:
                continue
        result.append(row)
    return result


def select_skill_glyphs(
    candidates: Iterable[dict[str, Any]],
    count: int,
    *,
    score_key: str = "score",
    forced: Iterable[tuple[Any, Any]] = (),
    fill_to_count: bool = False,
) -> list[dict[str, Any]]:
    """Select one glyph per skill with one shared, inexpensive policy.

    The selector is deliberately pure and works only on already-computed
    marginal scores.  It never runs a timeline itself, so skill simulation,
    team optimization and score theory can share the same final decision
    without multiplying full-axis calculations.

    The user's two damage/CD rules are applied before forced or score-based
    selection.  Forced utility glyphs therefore remain supported, but cannot
    override an explicitly defined damage/CD pair.
    """

    limit = max(0, int(count or 0))
    if limit <= 0:
        return []
    eligible = apply_damage_cooldown_glyph_policy(candidates)
    indexed = [(index, row) for index, row in enumerate(eligible)]

    def normalized_effect(value: Any) -> str:
        effect = str(value or "").strip()
        return "增益持续" if effect == "增益持续时间增加" else effect

    forced_specs = [
        (str(skill or "").strip(), normalized_effect(effect))
        for skill, effect in forced
        if str(skill or "").strip() and normalized_effect(effect)
    ]
    selected: list[dict[str, Any]] = []
    selected_skills: set[str] = set()

    def row_score(item: tuple[int, dict[str, Any]]) -> float:
        try:
            return float(item[1].get(score_key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    for forced_skill, forced_effect in forced_specs:
        matches = [
            item for item in indexed
            if str(item[1].get("skill") or "").strip() == forced_skill
            and normalized_effect(item[1].get("effect")) == forced_effect
        ]
        if not matches or forced_skill in selected_skills:
            continue
        _index, row = max(matches, key=lambda item: (row_score(item), -item[0]))
        selected.append(dict(row))
        selected_skills.add(forced_skill)
        if len(selected) >= limit:
            return selected

    ordered = sorted(indexed, key=lambda item: (-row_score(item), item[0]))
    for item in ordered:
        _index, row = item
        skill = str(row.get("skill") or "").strip()
        if not skill or skill in selected_skills:
            continue
        if row_score(item) <= 0 and not fill_to_count:
            continue
        selected.append(dict(row))
        selected_skills.add(skill)
        if len(selected) >= limit:
            break
    return selected


def select_skill_glyphs_two_stage(
    candidates: Iterable[dict[str, Any]],
    count: int,
    *,
    score_key: str = "score",
    local_score_key: str = "local_score",
    forced: Iterable[tuple[Any, Any]] = (),
    fill_to_count: bool = False,
    challenger_count: int | None = None,
) -> list[dict[str, Any]]:
    """Select glyphs with a cheap shortlist and a local counterfactual score.

    Stage one ranks every candidate by a cheap marginal estimate.  Stage two
    re-ranks only the initially selected skills, every alternative glyph for
    those skills, and a small number of the nearest challenger skills.  The
    caller may therefore spend more work on ``local_score`` without turning a
    four-glyph decision into an exhaustive full-timeline search.

    The second-stage score is deliberately *local*: cooldown, duration and
    action-speed choices should be valued against the same baseline rotation,
    rather than allowing every candidate to rebuild the entire axis and win or
    lose because unrelated skills moved around a stop window.  A caller that
    has no refined value can omit ``local_score`` and transparently falls back
    to the stage-one score.
    """

    limit = max(0, int(count or 0))
    if limit <= 0:
        return []

    eligible = apply_damage_cooldown_glyph_policy(candidates)
    if not eligible:
        return []

    stage_one = select_skill_glyphs(
        eligible,
        limit,
        score_key=score_key,
        forced=forced,
        fill_to_count=fill_to_count,
    )
    shortlisted_skills = {
        str(row.get("skill") or "").strip()
        for row in stage_one
        if str(row.get("skill") or "").strip()
    }

    # Keep a bounded number of alternative skills around the four-slot cutoff.
    # All effect variants of a shortlisted skill are retained so choices such
    # as cooldown versus action speed are compared in stage two.
    best_by_skill: dict[str, tuple[int, float]] = {}
    for index, row in enumerate(eligible):
        skill = str(row.get("skill") or "").strip()
        if not skill:
            continue
        try:
            score = float(row.get(score_key) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        previous = best_by_skill.get(skill)
        if previous is None or score > previous[1]:
            best_by_skill[skill] = (index, score)

    extra_limit = (
        max(0, int(challenger_count))
        if challenger_count is not None
        else limit
    )
    challengers = sorted(
        (
            (skill, index, score)
            for skill, (index, score) in best_by_skill.items()
            if skill not in shortlisted_skills
        ),
        key=lambda item: (-item[2], item[1]),
    )
    shortlisted_skills.update(skill for skill, _index, _score in challengers[:extra_limit])

    stage_two_rows: list[dict[str, Any]] = []
    for row in eligible:
        skill = str(row.get("skill") or "").strip()
        if skill not in shortlisted_skills:
            continue
        copied = dict(row)
        try:
            local_score = float(copied.get(local_score_key))
        except (TypeError, ValueError):
            try:
                local_score = float(copied.get(score_key) or 0.0)
            except (TypeError, ValueError):
                local_score = 0.0
        copied[local_score_key] = local_score
        stage_two_rows.append(copied)

    stage_two_rows = apply_practical_action_speed_tiebreak(
        stage_two_rows,
        score_key=local_score_key,
    )

    return select_skill_glyphs(
        stage_two_rows,
        limit,
        score_key=local_score_key,
        forced=forced,
        fill_to_count=fill_to_count,
    )


def approximate_team_objective_gain_ratio(attrs: Any) -> float:
    """Return a conservative one-character gain for team-effect attributes.

    Full score runs provide a composition- and damage-weighted objective.  The
    build and team-preview pages do not, so they use this small deterministic
    approximation only for shortlisting.  It intentionally values common
    attack/amp/timing effects and stays conservative for resistance/defence
    effects whose exact value depends on the target and party composition.
    """

    if not isinstance(attrs, dict):
        return 0.0
    values: dict[str, float] = {}
    for raw_key, raw_value in attrs.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            value = float(raw_value or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(value) > 1e-9:
            values[key] = value

    def total(*keys: str) -> float:
        return sum(values.get(key, 0.0) for key in keys)

    attack_percent = max(
        attack_increase_for_channel(values, "物攻"),
        attack_increase_for_channel(values, "魔攻"),
    )
    amp_percent = max(
        amplification_for_channel(values, "物攻"),
        amplification_for_channel(values, "魔攻"),
    )
    main_stat = max(total("力量%"), total("敏捷%"), total("智力%"))

    panel_multiplier = (
        max(0.0, 1.0 + attack_percent / 100.0)
        * max(0.0, 1.0 + main_stat / 100.0)
        * max(0.0, 1.0 + amp_percent / 100.0)
    )
    panel_gain = max(0.0, panel_multiplier - 1.0)

    resist_gain = max(
        0.0,
        total("减全抗%")
        + max(
            total("减火抗%"),
            total("减水抗%"),
            total("减光抗%"),
            total("减暗抗%"),
        ),
    ) / 125.0
    defense_gain = max(0.0, total("减防御%")) / 200.0
    freeze_gain = max(0.0, total("冰冻层")) * 0.15
    timing_gain = (
        max(0.0, total("冷却缩减%")) * 0.55
        + max(0.0, total("动作速度%", "攻速%")) * 0.90
    ) / 100.0
    return max(0.0, panel_gain + resist_gain + defense_gain + freeze_gain + timing_gain)
