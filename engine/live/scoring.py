from __future__ import annotations

import json
import math
import re
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable
from .mechanic_damage import MECHANIC_SKILL_IDS

from ..calculator import BuildCalculator
from ..data_loader import MODULAR_DIR, DataStore, as_number, norm
from .skill_identity import (
    _COMBAT_MODIFIER_RULES,
    canonical_skill,
    data_job_for,
)
from ..effect_stacking import (
    DEFENSE_REDUCTION_CAP_PERCENT,
    EFFECT_STACKING_ENGINE,
    EffectInput,
    effect_rule,
)
from ..hp_conditional import (
    CROW_SKILL,
    DANCE_STAGE_TWO_SKILL,
    boss_hp_percent_at,
    conditional_team_buff_coefficient,
    conditional_team_buff_gain_rate,
)
from ..job_mechanics import (
    JOB_MECHANICS,
    adjust_ring_strike_cast_time,
    is_ring_strike,
    ring_strike_output_multiplier,
    adjust_fixed_duration_action_speed_cast_time,
    is_fixed_duration_action_speed_output_skill,
    stance_damage_fraction_for_utilization,
    stance_guaranteed_damage_fraction,
)
from ..skill_glyph_policy import (
    apply_score_glyph_exclusions,
    glyph_marginal_score_multiplier,
    linked_cooldown_glyph_skills,
    select_skill_glyphs,
    select_skill_glyphs_two_stage,
)
from ..team_support_rules import (
    TEAM_EFFECT_LEVEL_CAPS,
    corrected_team_effect_duration_for_effect,
    effective_team_effect_duration_for_effect,
    effective_team_buff_duration,
    resolved_team_effect_duration_for_effect,
    team_effect_application_window_for_attrs,
    team_effect_application_window_for_effect,
    team_effect_refresh_tail,
    team_effect_refresh_tail_for_attrs,
)
from .cast_timing import LiveCastTimingResolver
from .buff_clear import (
    clip_player_buff_intervals_at_clears,
    merged_player_buff_clear_times,
    player_buff_clear_affects_timeline_skill,
    player_buff_clear_times_by_target,
    player_buff_is_clear_immune,
    player_buff_survives_at,
)
from .combat_core import snapshot_recording_end_ms
from .timeline_evidence import (
    TimelineEvidenceResolver,
    is_legacy_damage_derived_cast,
)
from .effects import boss_effect_rules
from .event_platform import event_platform_for
from .freeze_engine import (
    ALCHEMIST_FREEZE_BREAK_HITS,
    DEFAULT_FREEZE_BREAK_HITS,
    hit_limited_freeze_intervals,
    refreshing_freeze_applications,
)
from .healing_attribution import load_healing_skill_models
from .models import (
    CombatEvent,
    CombatSnapshot,
    PlayerSnapshot,
    SkillCastEvent,
)


HEALER_JOBS = frozenset({"圣徒", "药剂师", "辅助药剂", "曜", "打坐曜"})
TANK_JOBS = frozenset({"毁灭者", "圣骑士"})
_YAO_JOBS = frozenset({"曜", "打坐曜", "输出曜"})
_YAO_MEDITATION_SKILL = "查克拉治愈术"
_YAO_MEDITATION_ROLE_THRESHOLD = 0.40
_ALCHEMIST_JOBS = frozenset({"药剂师", "辅助药剂", "输出药剂"})
_ALCHEMIST_SACRIFICE_SKILL = "牺牲"


def _score_cast_axis(
    events: tuple[SkillCastEvent, ...] | None,
) -> tuple[SkillCastEvent, ...] | None:
    """Reject legacy damage-created casts at the scoring boundary."""

    if events is None:
        return None
    return tuple(
        event
        for event in events
        if not is_legacy_damage_derived_cast(event)
    )
HYBRID_SUPPORT_REASONS = {
    "剑皇": "重锤40%通用增伤与减致命抗覆盖",
    "月之领主": "重锤40%通用增伤与旋风斩EX减致命抗",
    "狂战士": "战吼提供固定双攻与致命增益",
    "狙翎": "鹰怒与瞄准提供常驻致命和通用增伤",
    "魔羽": "鹰怒与瞄准提供常驻致命和通用增伤",
    "影舞者": "精神爆发与30%物理增伤",
    "风行者": "精神爆发与30%物理增伤",
    "火舞": "灼热火焰加速、30%减火抗与30%减致命抗",
    "冰灵": "叠冰、减水抗与通用增伤",
    "十字军": "元素/判定光环与破甲增伤",
    "雷神": "双攻、光攻团队增益与多段减光抗",
    "炼金圣士": "鸡尾酒、减全抗与叠冰",
    "时空领主": "时间加速与30%魔法增伤",
    "黑暗女王": "时间加速与30%魔法增伤",
    "黑暗萨满": "幽灵之魂、怨恨之像、狂虐之乐与减抗",
    "噬魂者": "幽灵之魂、怨恨之像、傀儡增伤与减抗",
    "刀锋舞者": "幽灵之魂与九天之舞",
    "灵魂舞者": "幽灵之魂与九天之舞",
    "烈": "突袭、奉献乌鸦与减火抗",
    "影": "突袭、奉献乌鸦、减防与减暗抗",
    "暗": "黑暗化身、阿兹那之息与通用增伤",
    "黑暗复仇者": "火焰之力、通用增伤与减火抗",
}
HYBRID_SUPPORT_JOBS = frozenset(HYBRID_SUPPORT_REASONS)
SUPPORT_JOBS = HEALER_JOBS | TANK_JOBS | HYBRID_SUPPORT_JOBS
_DEFAULT_BUILD_PATH = (
    MODULAR_DIR / "tables" / "skills" / "skill_build_defaults_builtin.json"
)
_TEAM_BUFF_DATA_PATH = MODULAR_DIR / "tables" / "skill_buff.json"
_PLAYER_BUFF_DISPLAY_PATH = (
    MODULAR_DIR.parent / "profiles" / "player_buff_display_catalog.json"
)
_SUPPORT_SCORE_MIN_COMPLETION = 0.05
_LOW_DAMAGE_SCORE_EXCLUSION_THRESHOLD = 0.10
_MAX_SCORE_HIGH_VALUE_EXCLUSIONS_PER_JOB = 1
_MAX_SCORE_LOW_VALUE_EXCLUSIONS_PER_JOB = 3
_SURVIVAL_INCIDENT_MERGE_SECONDS = 1.0
_SURVIVAL_SHARED_WAVE_SECONDS = 1.0
_SURVIVAL_PARTY_WIDE_DAMAGE_WEIGHT = 0.15
_SURVIVAL_TARGETED_DAMAGE_WEIGHT = 0.40
_SURVIVAL_UNCERTAIN_DAMAGE_WEIGHT = 0.50
_SURVIVAL_DAMAGE_PENALTY_CAP = 20.0
_SURVIVAL_INTERRUPTION_SECONDS = 1.5
_SURVIVAL_OPERATION_BEFORE_SECONDS = 3.0
_SURVIVAL_OPERATION_RECOVERY_SECONDS = 4.5
_SURVIVAL_INTERRUPTION_PENALTY_CAP = 8.0
_SURVIVAL_BASE_NON_DEATH_PENALTY_CAP = 10.0
_SURVIVAL_SCORE_CAP_RATIO = 0.20
_SURVIVAL_POINTS_PER_CONFIRMED_DEATH = 5.0
# Tank/support roles are penalized at one third of the ordinary role for
# non-death survival losses.  Confirmed death penalties use their separate
# multiplier below.
_SURVIVAL_TANK_NON_DEATH_MULTIPLIER = 1.0 / 3.0
_SURVIVAL_TANK_DEATH_MULTIPLIER = 2.0
_SURVIVAL_HP_SHADOW_TANK_NON_DEATH_MULTIPLIER = 1.0 / 3.0
_SURVIVAL_HP_SHADOW_WAVE_SECONDS = 1.25
_SURVIVAL_HP_SHADOW_MAX_SAMPLE_GAP_SECONDS = 30.0
_SURVIVAL_HP_SHADOW_NOISE_RATIO = 0.001
_SURVIVAL_DANGER_HP_RATIO = 0.30
_SURVIVAL_DANGER_HP_RECOVERY_RATIO = 0.40
_SURVIVAL_DANGER_PENALTY_CAP = 6.0
_SURVIVAL_DEATH_EVENT_METHODS = frozenset(
    {"player_death_hp_state_086f"}
)
_OUTPUT_WINDOW_RESIDUAL_ATTRIBUTIONS = frozenset(
    {
        "action_free_special_attack",
        "skill_object_owner",
        "supplemental_cast_owner",
        "supplemental_continuation",
        "supplemental_same_packet",
        "status_damage_packet",
        "summon_owner",
        "unresolved",
    }
)
_OUTPUT_WINDOW_RESIDUAL_SKILL_IDS = frozenset({"-1", "4294967295"})
# Exact native result identity for the encounter-owned railgun. Newer object
# evidence names the two Black Dragon HC weapon branches as ``mechanism:*``.
# Neither route proves that a player's ordinary skill rotation is available.
_OUTPUT_WINDOW_MECHANIC_SKILL_IDS = MECHANIC_SKILL_IDS
_OUTPUT_WINDOW_TEAM_CONFIRM_SECONDS = 1.5
_OUTPUT_WINDOW_CAST_LOOKBACK_SECONDS = 4.0
_OUTPUT_WINDOW_CAST_LEAD_SECONDS = 0.35
_OUTPUT_WINDOW_SINGLE_SOURCE_HITS = 3
_OUTPUT_WINDOW_SPLIT_GAP_SECONDS = 4.0
_OUTPUT_WINDOW_END_GRACE_SECONDS = 1.0
_OUTPUT_PREP_CONTEXT_SECONDS = 3.0
_OUTPUT_WINDOW_DIRECT_ATTRIBUTIONS = frozenset(
    {
        "packet_skill_id",
        # This attribution also covers player-equipped damaging skill plates;
        # exclude encounter weapons by identity, not this shared method.
        "packet_dungeon_special_skill",
        "packet_basic_attack_key",
        "latest_profiled_direct_cast",
        "appended_status_first_hit",
    }
)
_OFFENSIVE_EFFECT_KEYS = frozenset(
    {
        "增伤%",
        "物理增伤%",
        "魔法增伤%",
        "减火抗%",
        "减水抗%",
        "减光抗%",
        "减暗抗%",
        "减全抗%",
        "减防御%",
        "减物理防御%",
        "减魔法防御%",
        "减致命抵抗%",
        "冰冻层",
    }
)
_DAMAGE_AMPLIFICATION_EFFECT_KEYS = frozenset(
    {"增伤%", "物理增伤%", "魔法增伤%"}
)
_ELEMENT_RESISTANCE_REDUCTION_EFFECT_KEYS = frozenset(
    {"减火抗%", "减水抗%", "减光抗%", "减暗抗%"}
)
# Optional utility is not part of a profession's mandatory theoretical support
# duty. If it is actually used, its measured counterfactual gain can still
# raise the support score without increasing the theoretical denominator.
_OPTIONAL_SUPPORT_EFFECT_SKILLS = frozenset({"冰尖柱"})
_DUTY_EFFECT_KEYS = _OFFENSIVE_EFFECT_KEYS | frozenset({"减攻击%"})
_OFFENSIVE_BUFF_ATTRS = (
    "无属性",
    "物攻",
    "魔攻",
    "攻击",
    "最终",
    "火攻",
    "水攻",
    "光攻",
    "暗攻",
    "属性攻",
    "致命",
    "动作速度",
    "冷却",
    "力量",
    "敏捷",
    "智力",
)
_HEALING_DESC_MARKERS = (
    "恢复生命",
    "生命恢复",
    "恢复全部生命",
    "治疗量",
    "治愈周围",
    "恢复队员",
)
_HEALING_SKILL_MODELS = load_healing_skill_models()
_HEALING_SKILL_NAMES = frozenset(
    model.name for model in _HEALING_SKILL_MODELS
)
_HEALING_EFFECT_WINDOWS = {
    model.name: max(
        0.5,
        model.active_window_ms() / 1000.0,
    )
    for model in _HEALING_SKILL_MODELS
}
_HEALING_PRESSURE_BUCKET_SECONDS = 3.0
_HEALING_HIGH_PRESSURE_PERCENTILE = 0.65
# Regular team mitigation is a healer duty, not offensive support gain.  Keep
# emergency reductions such as Miracle Relic/Energy Ring out of this metric.
_HEALER_REGULAR_DAMAGE_REDUCTION_SKILLS = {
    "圣徒": frozenset({"保护之盾"}),
    "药剂师": frozenset({"牺牲"}),
    "曜": frozenset({"查克拉治愈术"}),
}
_HEALER_REGULAR_DAMAGE_REDUCTION_DURATIONS = {
    canonical: duration
    for canonical, duration in (
        ("保护之盾", 180.0),
        ("牺牲", 180.0),
        ("查克拉治愈术", 120.0),
    )
}
# Some persistent team auras can be reported as one permanent status on the
# provider even though the effect applies to the party.  A recipient average
# must therefore project the provider's exact status interval to party members
# instead of treating missing repeated recipient packets as a missing Buff.
_HEALER_SELF_REPORTED_TEAM_AURAS = {
    "药剂师": frozenset({"牺牲"}),
    "曜": frozenset({"阿兹那之息"}),
    "暗": frozenset({"阿兹那之息"}),
}
# A living recipient can carry the only status row seen in one packet window,
# while another recipient carries it after death/rejoin.  For a truly
# persistent common aura, the union is the source aura's lifecycle and must be
# projected back to every party recipient before time/damage coverage is read.
_PERSISTENT_TEAM_AURA_SKILL_KEYS = frozenset(
    {"阿兹那之息"}
)
_BURST_SKILLS = frozenset({"精神爆发", "灼热火焰", "时间加速"})
_TRANSFORMED_BASIC_ATTACK_BASE_SKILLS = {
    "神圣之锤攻击": "神圣之锤",
    "致残惩罚者攻击": "致残惩罚者",
}
_TRANSFORMED_ATTACK_BUFF_ONLY_SKILLS = frozenset({"神圣之锤", "致残惩罚者"})
def _recorded_timing_buff_intervals(
    row: Any, skill_name: str
) -> tuple[tuple[float, float], ...]:
    """Keep the actual timed effect, not another status sharing its skill ID.

    Scorching Flame has a 40% speed track, a 60% remaining-CD track and a
    long-lived resource track.  The speed track identifies the one real
    application window used for both timing modifiers.
    """

    skill = canonical_skill(skill_name)
    rule = _COMBAT_MODIFIER_RULES.get(skill)
    if rule is None:
        return ()
    if skill == canonical_skill("灼热火焰") and abs(
        as_number(getattr(row, "value", None)) - 40.0
    ) > 0.5:
        return ()
    normal_duration = rule[2]
    return tuple(
        (float(start), float(end))
        for start, end in getattr(row, "intervals", ())
        if end > start + 1e-9
        and (
            normal_duration is None
            or end - start <= max(normal_duration + 2.0, normal_duration * 2.0)
        )
    )
_PALADIN_AURA_JOBS = frozenset({"圣骑士", "十字军"})
_PALADIN_AURA_SKILLS = frozenset({"元素光环", "判定光环"})
_PALADIN_AURA_GROUP_KEY = "圣骑互斥光环"
_TANK_SHELL_SKILLS = frozenset({"护卫者姿势", "范围炸弹EX", "范围炸弹"})
_TAUNT_SKILLS = frozenset({"挑衅", "惩戒波"})
# These skills keep applying the same freeze layer while their attack remains
# active.  The first value is one application's lifetime; the second is the
# theory engine's refresh sampling interval.  The union lifetime still comes
# from the shared output plan, so action-speed and interruption rules remain
# authoritative.
_REFRESHING_FREEZE_RULES: dict[str, tuple[float, float]] = {
    # Ice River applies once on its first hit and refreshes on the later hit.
    # A zero application duration means "use the selected skill-level value".
    "冰河": (0.0, 1.6),
    "冰柱喷射": (6.0, 0.5),
    "暴风雪": (11.0, 0.5),
    "冰光束": (6.0, 0.5),
    # 游戏内俗称“冰拳”。炼金圣士的 EX 在一秒动作内完成两段
    # 叠冰判定，第二段会刷新同一个炼金冰冻状态。
    "寒冰戳刺": (3.0, 1.0),
}
_CHANNEL_REFRESHING_FREEZE_SKILLS = frozenset(
    {"冰光束", "寒冰戳刺"}
)
_STANDARD_ATTACK = 30000.0
# The live-score benchmark follows the level-70 Peda preset used by the build
# calculator. Scoring assumes the standardized 70L setup is at the critical
# and critical-damage caps, then measures each debuff against Peda's baseline.
_PEDA_DEFENSE = 4950.0
_PEDA_CRIT_RESIST = 19000.0
_PEDA_ELEMENT_RESIST_PERCENT = 20.0
_STANDARD_CRIT_PERCENT = 90.0
_STANDARD_CRIT_DAMAGE_PERCENT = 100.0
_PROPORTIONAL_CHANNEL_SKILLS = frozenset(
    {"包围", "挥砍姿态", "猛砍站姿", "黑暗冲浪", "地狱火海"}
)
_JOB_PROPORTIONAL_CHANNEL_SKILLS = {
    "游侠": frozenset({"螺旋踢", "螺旋风暴"}),
    "风行者": frozenset({"螺旋踢", "螺旋风暴"}),
    "影舞者": frozenset({"螺旋踢", "螺旋风暴", "飓风舞"}),
    "狙翎": frozenset({"箭雨风暴"}),
    "火舞": frozenset({"火径"}),
}
_PROPORTIONAL_DEFAULT_GUARANTEED_DAMAGE_FRACTIONS = {
    # 无 EX 地狱火海的前 0.5 倍必定打出，后 0.5 倍按维持时间结算。
    "地狱火海": 0.5,
}
_PROPORTIONAL_GUARANTEED_DAMAGE_FRACTIONS = {
    # 影舞者的 1.5 倍螺旋风暴中，0.5 倍为起手必中的固定部分。
    ("影舞者", "螺旋风暴"): 0.5 / 1.5,
    # 飓风舞前半段伤害固定打出，后半段才随维持时间结算。
    ("影舞者", "飓风舞"): 0.5,
    # 火舞地狱火海 EX 完整为 1.66 倍：1.06 倍必中，0.60 倍随维持时间结算。
    ("火舞", "地狱火海"): 1.06 / 1.66,
    # 狙翎箭雨风暴完整为 60 倍：18 倍固定，42 倍随维持时间结算。
    ("狙翎", "箭雨风暴"): 18.0 / 60.0,
}
_EXPLICIT_INTERRUPT_PACKET_KINDS = frozenset(
    {
        "skill_action",
        "skill_cast",
        "compact_skill_input",
        "structured_multi_action",
        "direct",
        "legacy",
    }
)
_DARK_SURF_PROJECTILE_LIFETIME = 4.0
_DARK_SURF_SHOT_INTERVAL = 27 / 60
_DARK_SURF_EXPLOSION_MULTIPLIER = 2.0
_DARK_SURF_BASE_MULTIPLIER = 16.0


@lru_cache(maxsize=4096)
def _capped_additive_shapley(
    gains: tuple[float, ...], cap: float = 1.0
) -> tuple[float, ...]:
    """Allocate a capped additive pool without losing redundant value.

    Leave-one-out marginal losses do not add up once the raw pool exceeds its
    cap: removing one effect may leave the pool capped and therefore credit it
    with zero. Shapley allocation averages every possible addition order, so
    individual credits remain fair and always sum to the capped pool value.
    """
    values = tuple(max(0.0, float(value)) for value in gains)
    count = len(values)
    if count == 0:
        return ()
    target = min(max(0.0, cap), sum(values))
    if target <= 1e-12:
        return tuple(0.0 for _value in values)
    if sum(values) <= cap + 1e-12:
        return values
    # Amplification pools normally contain only a handful of distinct skill
    # groups. Keep an exact solution there; the proportional fallback protects
    # pathological captures from exponential work while preserving the total.
    if count > 12:
        total = sum(values)
        return tuple(target * value / total for value in values)

    shares = [0.0 for _value in values]
    for index, own_gain in enumerate(values):
        others = values[:index] + values[index + 1 :]
        other_count = len(others)
        for mask in range(1 << other_count):
            subset_size = mask.bit_count()
            subset_gain = sum(
                others[other_index]
                for other_index in range(other_count)
                if mask & (1 << other_index)
            )
            probability = 1.0 / (
                count * math.comb(count - 1, subset_size)
            )
            marginal = min(cap, subset_gain + own_gain) - min(
                cap, subset_gain
            )
            shares[index] += probability * max(0.0, marginal)
    allocated = sum(shares)
    if allocated > 1e-12:
        shares = [share * target / allocated for share in shares]
    return tuple(shares)


def _proportional_channel_skills(job: str) -> frozenset[str]:
    return _PROPORTIONAL_CHANNEL_SKILLS | _JOB_PROPORTIONAL_CHANNEL_SKILLS.get(
        job, frozenset()
    )


def _proportional_damage_fraction(
    job: str,
    skill_key: str,
    utilization_ratio: float,
    full_output_multiplier: float = 1.0,
) -> float:
    """Return damage retained after a channel is interrupted early."""
    utilized = max(0.0, min(1.0, as_number(utilization_ratio)))
    if stance_guaranteed_damage_fraction(norm(job), canonical_skill(skill_key)) > 0.0:
        return stance_damage_fraction_for_utilization(
            job,
            skill_key,
            utilized,
            full_output_multiplier,
        )
    guaranteed = max(
        0.0,
        min(
            1.0,
            _PROPORTIONAL_GUARANTEED_DAMAGE_FRACTIONS.get(
                (norm(job), canonical_skill(skill_key)),
                _PROPORTIONAL_DEFAULT_GUARANTEED_DAMAGE_FRACTIONS.get(
                    canonical_skill(skill_key),
                    0.0,
                ),
            ),
        ),
    )
    return guaranteed + (1.0 - guaranteed) * utilized


@lru_cache(maxsize=128)
def _scoring_stance_action_rule(job: str, skill_key: str) -> dict[str, Any]:
    rules = JOB_MECHANICS.job_value(job, "stance_skills", {})
    if not isinstance(rules, dict):
        return {}
    for name, rule in rules.items():
        if canonical_skill(name) == canonical_skill(skill_key) and isinstance(rule, dict):
            return rule
    return {}


def _action_speed_output_multiplier(
    job: str,
    skill_key: str,
    action_speed_percent: float,
) -> float:
    """Convert action speed into the extra hit count of fixed-duration skills."""
    speed = max(0.0, as_number(action_speed_percent))
    key = canonical_skill(skill_key)
    if is_ring_strike(key):
        return ring_strike_output_multiplier(1.0 + speed / 100.0)
    if is_fixed_duration_action_speed_output_skill(key):
        return 1.0 + speed / 100.0
    rule = _scoring_stance_action_rule(norm(job), key)
    if rule:
        base_count = max(0, int(as_number(rule.get("base_count"))))
        base_multiplier = max(0.01, as_number(rule.get("base_multiplier"), 1.0))
        if base_count <= 0:
            return 1.0
        count = (max(8,int(8+5*speed/100+1e-9))
                 if norm(job)=='月之领主' and key=='猛砍站姿'
                 else max(base_count, int(base_count * (1.0 + speed / 100.0) + 1e-9)))
        output = (
            count * max(0.0, as_number(rule.get("per_hit_multiplier"), 1.0))
            + max(0.0, as_number(rule.get("fixed_multiplier")))
        )
        return max(0.0, output / base_multiplier)
    if norm(job) == "黑暗复仇者" and key == canonical_skill("黑暗冲浪"):
        action_units = _DARK_SURF_PROJECTILE_LIFETIME * (1.0 + speed / 100.0)
        count = max(
            1,
            math.floor(action_units / _DARK_SURF_SHOT_INTERVAL + 1e-9),
        )
        return (
            count
            * _DARK_SURF_EXPLOSION_MULTIPLIER
            / _DARK_SURF_BASE_MULTIPLIER
        )
    return 1.0


def _action_speed_adjusted_cast_time(
    skill_key: str,
    cast_time: float,
    action_factor: float,
    job: str = "",
) -> float:
    duration = max(0.0, as_number(cast_time))
    if is_ring_strike(skill_key):
        return adjust_ring_strike_cast_time(duration, action_factor)
    # Stance actions have a fixed occupation window.  Action speed increases
    # the number of attacks that fit inside that window (handled by
    # _action_speed_output_multiplier); shortening the window here would count
    # the same speed bonus a second time.
    if _scoring_stance_action_rule(norm(job), canonical_skill(skill_key)):
        return duration
    if is_fixed_duration_action_speed_output_skill(skill_key):
        return adjust_fixed_duration_action_speed_cast_time(
            skill_key,
            duration,
            action_factor,
        )
    return duration / max(1.0, as_number(action_factor, 1.0))


@dataclass(frozen=True, slots=True)
class SkillProfile:
    name: str
    key: str
    power: float
    cooldown: float
    cast_time: float
    glyph_effect: str = ""
    glyph_value: float = 0.0

    @property
    def dpa(self) -> float:
        return self.power / max(0.12, self.cast_time)

    @property
    def dps(self) -> float:
        return self.power / max(0.12, self.cooldown)


@dataclass(frozen=True, slots=True)
class ScoreTimelineCast:
    seconds: float
    end_seconds: float
    skill_name: str
    power: float
    source: str
    high_value: bool = False
    in_burst_window: bool = False
    expected_duration: float = 0.0
    utilized_duration: float = 0.0
    utilization_ratio: float = 1.0
    proportional_output: bool = False
    action_speed_percent: float = 0.0
    action_speed_output_multiplier: float = 1.0
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class SkillScoreDetail:
    skill_name: str
    actual_count: int
    theory_count: int
    actual_power: float
    theory_power: float
    completion: float
    high_value: bool
    actual_duration: float = 0.0
    expected_duration: float = 0.0
    duration_utilization: float = 1.0
    proportional_output: bool = False
    usage_rate: float | None = None
    excluded_from_score: bool = False
    # Effective level from this actor's combat response; SP/mastery/equipment
    # components are not separately identifiable from that response.
    actual_level: int | None = None
    scoring_level: int | None = None
    level_status: str = "unknown"
    level_candidates: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ScoreWindowDetail:
    label: str
    start_seconds: float
    end_seconds: float
    actual_power: float
    theory_power: float
    completion: float
    original_duration: float = 0.0


@dataclass(frozen=True, slots=True)
class SupportScoreDetail:
    skill_name: str
    effect_key: str
    kind: str
    magnitude: float
    actual_coverage: float
    theory_coverage: float
    completion: float
    score_weight: float
    actual_contribution: float
    theory_contribution: float
    actual_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    theory_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    actual_display_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    theory_display_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    expected_duration: float = 0.0
    included_in_score: bool = True
    actual_covered_damage_share: float = 0.0
    theory_covered_damage_share: float = 0.0
    # Four deliberately independent diagnostics:
    # - actual_coverage is the client-effective effect lifetime within output
    #   windows; hit-assumed resistance application is separately exposed in
    #   application_coverage before same-key strongest-value arbitration;
    # - effective_damage_coverage is the share of relevant damage that really
    #   landed while the captured effect was active;
    # - effective_damage_theory_coverage is the share of the same observed
    #   damage stream that would land inside the local theoretical effect axis;
    # - support_benefit_completion is actual counterfactual gain divided by
    #   the legal-optimal gain and is intentionally allowed to exceed 100%.
    effective_damage_coverage: float = 0.0
    support_benefit_completion: float = 100.0
    effective_damage_theory_coverage: float = 0.0
    # Accelerator diagnostics are separate from the official contribution.
    # ``actual_contribution``/``theory_contribution`` remain the direct score
    # axes; linked values are the incremental downstream Buff/Debuff coverage
    # obtained by keeping captured cast intent and enforcing cooldown legality.
    actual_direct_contribution: float = 0.0
    actual_linked_contribution: float = 0.0
    actual_total_contribution: float = 0.0
    theory_direct_contribution: float = 0.0
    theory_linked_contribution: float = 0.0
    theory_total_contribution: float = 0.0
    linked_contribution_available: bool = False
    linked_contribution_included_in_score: bool = False
    # Packet evidence is retained independently from the client-resolved
    # lifetime. A lower same-group status can be observed on the wire while it
    # contributes no active value or score.
    observed_coverage: float = 0.0
    observed_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    observed_display_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    effective_state: str = ""
    effective_reason: str = ""
    # For resistance effects inferred from real Hits, application is distinct
    # from the strongest same-key layer that contributes to damage/score.
    # None means that this diagnostic does not apply to this effect.
    application_coverage: float | None = None
    application_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    application_display_intervals: tuple[tuple[float, float], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ConfidenceDetail:
    direct_skill_count: int
    observed_skill_count: int
    direct_cast_count: int
    inferred_cast_count: int
    recognized_skill_count: int
    damaging_skill_count: int
    direct_ratio: float
    skill_name_ratio: float
    unmatched_skills: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class HpSurvivalShadowDetail:
    """HP-snapshot survival result kept beside, not inside, the live score."""

    available: bool = False
    penalty: float = 0.0
    damage_penalty: float = 0.0
    death_penalty: float = 0.0
    non_death_penalty: float = 0.0
    non_death_penalty_cap: float = _SURVIVAL_BASE_NON_DEATH_PENALTY_CAP
    incoming_hp_loss: float = 0.0
    self_sourced_hp_loss: float = 0.0
    team_mechanic_baseline_hp_loss: float = 0.0
    avoidable_hp_loss: float = 0.0
    avoidable_hp_loss_per_minute: float = 0.0
    danger_hp_threshold: float = _SURVIVAL_DANGER_HP_RATIO
    danger_incident_count: int = 0
    danger_penalty: float = 0.0
    danger_available: bool = False
    danger_source: str = ""
    danger_point_count: int = 0
    incident_count: int = 0
    team_mechanic_incident_count: int = 0
    targeted_mechanic_incident_count: int = 0
    avoidable_incident_count: int = 0
    uncertain_incident_count: int = 0
    point_count: int = 0
    max_hp: float = 0.0
    max_hp_source: str = ""
    exact_percent: bool = False
    confidence: str = "unavailable"


@dataclass(frozen=True, slots=True)
class SurvivalDisciplineDetail:
    penalty: float = 0.0
    incoming_damage: int = 0
    hit_count: int = 0
    incident_count: int = 0
    shared_incident_count: int = 0
    team_mechanic_incident_count: int = 0
    targeted_mechanic_incident_count: int = 0
    avoidable_incident_count: int = 0
    uncertain_incident_count: int = 0
    weighted_incidents: float = 0.0
    free_incidents: float = 0.0
    evaluated_seconds: float = 0.0
    role_factor: float = 1.0
    death_count: int = 0
    reference_hp: float = 0.0
    team_mechanic_baseline_damage: float = 0.0
    team_mechanic_baseline_hp_loss_per_minute: float = 0.0
    avoidable_damage: float = 0.0
    avoidable_hp_loss_per_minute: float = 0.0
    damage_penalty: float = 0.0
    danger_incident_count: int = 0
    danger_penalty: float = 0.0
    interruption_loss: float = 0.0
    interruption_loss_ratio: float = 0.0
    interruption_penalty: float = 0.0
    verified_interruption_count: int = 0
    death_penalty: float = 0.0
    non_death_penalty_cap: float = _SURVIVAL_BASE_NON_DEATH_PENALTY_CAP
    uncapped_non_death_penalty: float = 0.0
    hp_shadow: HpSurvivalShadowDetail = field(
        default_factory=HpSurvivalShadowDetail
    )


@dataclass(frozen=True, slots=True)
class PlayerScore:
    source_id: str
    name: str
    job: str
    role: str
    role_reason: str
    grade: str
    total_score: float
    output_score: float
    support_score: float
    specialty_score: float | None
    output_weight: float
    support_weight: float
    specialty_weight: float
    specialty_label: str
    support_index: float
    theory_damage_completion: float
    timing_efficiency: float
    high_value_usage: float
    burst_usage: float
    coverage_completion: float
    contribution_completion: float
    healing_completion: float | None
    tank_completion: float | None
    healing_cast_completion: float | None
    effective_healing: int
    healing_effective_realization: float | None
    healing_rescue_completion: float | None
    healing_damage_reduction_completion: float | None
    healing_pressure_windows: int
    healing_meaningful_casts: int
    healing_total_casts: int
    tank_attack_down_completion: float | None
    tank_taunt_completion: float | None
    tank_shell_completion: float | None
    confidence: float
    actual_standard_power: float
    theory_standard_power: float
    glyphs: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    hit_realization: dict[str, Any] = field(default_factory=dict)
    output_windows: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    castability_windows: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    actual_timeline: tuple[ScoreTimelineCast, ...] = field(default_factory=tuple)
    theory_timeline: tuple[ScoreTimelineCast, ...] = field(default_factory=tuple)
    skill_details: tuple[SkillScoreDetail, ...] = field(default_factory=tuple)
    burst_windows: tuple[ScoreWindowDetail, ...] = field(default_factory=tuple)
    support_details: tuple[SupportScoreDetail, ...] = field(default_factory=tuple)
    confidence_detail: ConfidenceDetail | None = None
    rotation_job: str = ""
    output_contribution: float = 0.0
    support_contribution: float = 0.0
    specialty_contribution: float = 0.0
    survival_penalty: float = 0.0
    survival_detail: SurvivalDisciplineDetail = field(
        default_factory=SurvivalDisciplineDetail
    )
    tank_taunt_coverage: float | None = None
    tank_taunt_theory_coverage: float | None = None
    tank_taunt_evidence: str = ""
    healing_evaluated_casts: int | None = None
    tank_shell_coverage: float | None = None
    tank_shell_party_size: int | None = None
    tank_shell_theory_coverage: float | None = None
    tank_shell_theory_casts: int | None = None
    tank_shell_evidence: str = ""
    tank_attack_down_coverage: float | None = None
    tank_attack_down_theory_coverage: float | None = None
    tank_attack_down_evidence: str = ""
    healing_rescue_detail: dict[str, Any] = field(default_factory=dict)
    scoring_equipment: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _HealingCompletion:
    score: float
    effective_realization: float
    rescue_completion: float | None
    damage_reduction_completion: float
    effective_healing: int
    pressure_windows: int
    meaningful_casts: int
    total_casts: int
    evaluated_casts: int = 0
    cast_completion: float = 100.0
    rescue_detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _TheoreticalCast:
    seconds: float
    key: str
    power: float
    end_seconds: float = 0.0
    expected_duration: float = 0.0
    utilized_duration: float = 0.0
    proportional_output: bool = False
    action_speed_percent: float = 0.0
    action_speed_output_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class _ObservedCast:
    seconds: float
    key: str
    source: str
    expected_duration: float = 0.0
    action_speed_percent: float = 0.0
    interrupt_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _ValuedCast:
    seconds: float
    key: str
    source: str
    power: float
    expected_duration: float
    utilized_duration: float
    utilization_ratio: float
    proportional_output: bool
    action_speed_percent: float = 0.0
    action_speed_output_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class _SupportExpectation:
    skill_key: str
    skill_name: str
    effect_key: str
    theory: float
    magnitude: float
    kind: str
    cooldown: float
    duration: float
    stackable: bool = False
    stack_group: str = ""
    proc_chance: float = 1.0


@dataclass(frozen=True, slots=True)
class _SupportEffectInstance:
    source_id: str
    source_job: str
    expectation: _SupportExpectation
    intervals: tuple[tuple[float, float], ...]
    # Boss Debuffs are global once they are active on the selected target.
    # Player Buffs are not: the protocol reports one status track per
    # recipient, and a party member who missed the application must not gain
    # damage or support credit from somebody else's track.
    recipient_scoped: bool = False
    recipient_intervals: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ] = ()


@dataclass(frozen=True, slots=True)
class _PersonalAttackEffectInstance:
    target_id: str
    skill_key: str
    physical_percent: float
    magical_percent: float
    intervals: tuple[tuple[float, float], ...]


@dataclass(slots=True)
class _SupportCounterfactualContext:
    team_damage_events: tuple[CombatEvent, ...]
    total_team_damage: float
    effects: tuple[_SupportEffectInstance, ...]
    intervals_by_identity: dict[
        tuple[str, str, str, str], tuple[tuple[float, float], ...]
    ]
    effective_intervals_by_identity: dict[
        tuple[str, str, str, str], tuple[tuple[float, float], ...]
    ] = field(default_factory=dict)
    effective_state_by_identity: dict[
        tuple[str, str, str, str], str
    ] = field(default_factory=dict)
    effective_reason_by_identity: dict[
        tuple[str, str, str, str], str
    ] = field(default_factory=dict)
    hit_assumed_identities: frozenset[tuple[str, str, str, str]] = frozenset()
    personal_attack_effects: tuple[_PersonalAttackEffectInstance, ...] = ()
    boss_hp_segments: tuple[tuple[tuple[float, float], ...], ...] = ()
    support_boss_scope: dict = field(default_factory=dict)
    support_boss_event_eligibility: dict = field(default_factory=dict, repr=False)
    actual_source_contributions: dict[str, float] = field(default_factory=dict)
    actual_detail_contributions: dict[tuple[str, str, str, str], float] = field(
        default_factory=dict
    )
    baseline_damage_by_event: tuple[float, ...] = field(default_factory=tuple)
    allocation_state_cache: dict[
        tuple[object, ...], tuple[float, tuple[object, ...]]
    ] = field(default_factory=dict, repr=False)


_TEAM_ATTACK_BUFF_EXCLUSIVE_GROUP = "团队攻击力增益"


@lru_cache(maxsize=128)
def _team_attack_buff_exclusive_group(skill: Any) -> str:
    """Return the shared non-stacking group for team attack buffs."""

    skill_key = canonical_skill(skill)
    configured = JOB_MECHANICS.global_value(
        "team.exclusive_attack_buff_skills", ()
    )
    if skill_key and any(
        canonical_skill(candidate) == skill_key for candidate in configured
    ):
        return _TEAM_ATTACK_BUFF_EXCLUSIVE_GROUP
    return ""


@lru_cache(maxsize=128)
def _team_attack_buff_priority(skill: Any) -> int:
    """Return explicit game priority inside the team-attack exclusive group.

    The configured order is strongest first. Unknown skills intentionally have
    the lowest priority so adding a new member to the exclusive group cannot
    silently override a game-declared winner.
    """

    skill_key = canonical_skill(skill)
    configured = JOB_MECHANICS.global_value(
        "team.exclusive_attack_buff_priority", ()
    )
    ordered = tuple(canonical_skill(candidate) for candidate in configured)
    try:
        return len(ordered) - ordered.index(skill_key)
    except ValueError:
        return 0


def _boss_hp_segments_from_theory_plans(
    theory_plans: dict[str, dict[str, Any]] | None,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    for plan in (theory_plans or {}).values():
        if not isinstance(plan, dict):
            continue
        segments = plan.get("_score_boss_hp_segments", ())
        if not isinstance(segments, (list, tuple)) or not segments:
            continue
        offset = as_number(plan.get("_score_time_offset"))
        return tuple(
            tuple(
                (offset + as_number(point[0]), as_number(point[1]))
                for point in segment
                if isinstance(point, (list, tuple)) and len(point) >= 2
            )
            for segment in segments
            if isinstance(segment, (list, tuple)) and segment
        )
    return ()


@lru_cache(maxsize=64)
def _scoring_summon_limits(job: str) -> dict[str, tuple[int, float]]:
    """Return canonical summon concurrency limits used by score replay."""
    combined: dict[str, Any] = {}
    global_limits = JOB_MECHANICS.global_value("summons.limits", {})
    job_limits = JOB_MECHANICS.job_value(job, "summons", {})
    if isinstance(global_limits, dict):
        combined.update(global_limits)
    if isinstance(job_limits, dict):
        combined.update(job_limits)

    result: dict[str, tuple[int, float]] = {}
    for skill_name, config in combined.items():
        if not isinstance(config, dict):
            continue
        maximum = max(0, int(as_number(config.get("max"))))
        duration = max(0.0, as_number(config.get("duration")))
        skill_key = canonical_skill(skill_name)
        if skill_key and maximum > 0 and duration > 0:
            result[skill_key] = (maximum, duration)
    return result


@lru_cache(maxsize=1)
def _scoring_refreshable_periodic_durations() -> dict[str, float]:
    rows = JOB_MECHANICS.global_value(
        "periodic_damage.refreshable_durations", {}
    )
    if not isinstance(rows, dict):
        return {}
    return {
        canonical_skill(skill_name): max(0.0, as_number(duration))
        for skill_name, duration in rows.items()
        if canonical_skill(skill_name) and as_number(duration) > 0
    }


@lru_cache(maxsize=1)
def _scoring_periodic_damage_cycles() -> dict[str, float]:
    """Return every skill whose listed power is spread over a full cycle."""
    rows = JOB_MECHANICS.global_value(
        "periodic_damage.total_damage_cycles", {}
    )
    if not isinstance(rows, dict):
        return {}
    return {
        canonical_skill(skill_name): max(0.0, as_number(duration))
        for skill_name, duration in rows.items()
        if canonical_skill(skill_name) and as_number(duration) > 0
    }


@lru_cache(maxsize=1)
def _scoring_periodic_damage_durations() -> dict[str, float]:
    """Return full-damage durations for summons and refreshable periodic skills."""
    durations = dict(_scoring_periodic_damage_cycles())
    durations.update(_scoring_refreshable_periodic_durations())
    return durations


def _scoring_periodic_damage_duration(
    key: str,
    profile: SkillProfile,
) -> float:
    duration = _scoring_periodic_damage_durations().get(key, 0.0)
    if duration <= 0:
        return 0.0
    if (
        key in _scoring_refreshable_periodic_durations()
        and profile.glyph_effect == "增益持续时间增加"
    ):
        duration *= 1.0 + max(0.0, profile.glyph_value) / 100.0
    return duration


@lru_cache(maxsize=1)
def _scoring_dynamic_periodic_skills() -> frozenset[str]:
    rows = JOB_MECHANICS.global_value(
        "periodic_damage.dynamic_attack_buff_summons", ()
    )
    if not isinstance(rows, (list, tuple, set)):
        return frozenset()
    return frozenset(
        canonical_skill(skill_name)
        for skill_name in rows
        if canonical_skill(skill_name)
    )


def _scoring_refreshable_periodic_duration(
    key: str,
    profile: SkillProfile,
) -> float:
    duration = _scoring_refreshable_periodic_durations().get(key, 0.0)
    if duration <= 0:
        return 0.0
    if profile.glyph_effect == "增益持续时间增加":
        duration *= 1.0 + max(0.0, profile.glyph_value) / 100.0
    return duration


@lru_cache(maxsize=1)
def _scoring_persistent_summon_cycles() -> dict[str, float]:
    """Return permanent summons whose stored power represents one damage cycle."""
    passive_rows = JOB_MECHANICS.global_value(
        "summons.passive_timeline_skills", ()
    )
    cycle_rows = JOB_MECHANICS.global_value(
        "periodic_damage.total_damage_cycles", {}
    )
    if not isinstance(passive_rows, (list, tuple, set)) or not isinstance(
        cycle_rows, dict
    ):
        return {}
    passive_keys = {
        canonical_skill(skill_name)
        for skill_name in passive_rows
        if canonical_skill(skill_name)
    }
    return {
        canonical_skill(skill_name): max(0.0, as_number(cycle_seconds))
        for skill_name, cycle_seconds in cycle_rows.items()
        if canonical_skill(skill_name) in passive_keys
        and as_number(cycle_seconds) > 0
    }


def _persistent_summon_score_casts(
    profiles: dict[str, SkillProfile],
    output_windows: tuple[tuple[float, float], ...],
    theory_plan: dict[str, Any] | None = None,
    source_id: str = "",
) -> tuple[list[_ValuedCast], list[_TheoreticalCast]]:
    """Spread permanent summons across output windows using integrated damage.

    Current long-simulation plans already integrate summon snapshots and every
    later Buff/Debuff boundary.  Prefer those exact results; the simple
    ``profile.power * time / cycle`` path remains only for old cached plans.
    """
    actual_rows: list[_ValuedCast] = []
    theory_rows: list[_TheoreticalCast] = []

    def integrated_rows(
        rows: Any,
        key: str,
    ) -> list[tuple[float, float, float, float]]:
        if not isinstance(rows, (list, tuple)):
            return []
        time_offset = max(
            0.0,
            as_number(theory_plan.get("_score_time_offset"))
            if isinstance(theory_plan, dict)
            else 0.0,
        )
        result: list[tuple[float, float, float, float]] = []
        for row in rows:
            if not isinstance(row, dict) or canonical_skill(row.get("skill")) != key:
                continue
            damage = max(0.0, as_number(row.get("_score_actual_damage")))
            if damage <= 0:
                continue
            # Modern plans carry constant-environment slices, so clip damage
            # at exactly the same boundaries as the personal output scope.
            # Never assign the full integrated damage to a shorter duration.
            pieces = row.get("_score_damage_slices")
            if not isinstance(pieces, (list, tuple)):
                pieces = [row]  # Legacy plan: uniform-rate approximation only.
            for piece in pieces:
                start = time_offset + max(0.0, as_number(piece.get("start")))
                end = time_offset + max(start - time_offset, as_number(piece.get("end")))
                overlaps = _intersect_intervals(((start, end),), output_windows)
                utilized = _interval_duration(overlaps)
                if utilized <= 1e-9:
                    continue
                power = max(0.0, as_number(piece.get("_score_actual_damage")))
                # Legacy integrations already exclude blocked time; do not
                # penalize the same gap again when all eligible time remains.
                denominator = max(utilized, as_number(
                    piece.get("periodic_damage_seconds"), end - start))
                for a, b in overlaps:
                    result.append((a, b, power * (b - a) / denominator, b - a))
        return result

    planned_segments = (
        theory_plan.get("segments", ()) if isinstance(theory_plan, dict) else ()
    )
    actual_segments: Any = ()
    if isinstance(theory_plan, dict) and source_id:
        by_source = theory_plan.get("_score_actual_periodic_segments", {})
        if isinstance(by_source, dict):
            actual_segments = by_source.get(str(source_id), ())

    for key, cycle_seconds in _scoring_persistent_summon_cycles().items():
        profile = profiles.get(key)
        if profile is None:
            continue
        exact_theory = integrated_rows(planned_segments, key)
        exact_actual = integrated_rows(actual_segments, key)
        # A permanent passive is always present.  Old captures may not have a
        # source-specific replay row, so use the same environment-integrated
        # theory row rather than dropping back to an unbuffed cycle.
        has_theory = any(
            isinstance(row, dict) and canonical_skill(row.get("skill")) == key
            for row in planned_segments
        )
        has_actual = any(
            isinstance(row, dict) and canonical_skill(row.get("skill")) == key
            for row in actual_segments
        ) if isinstance(actual_segments, (list, tuple)) else False
        if has_theory:
            if not has_actual:
                exact_actual = list(exact_theory)
            for start, end, power, utilized in exact_actual:
                actual_rows.append(
                    _ValuedCast(
                        seconds=start,
                        key=key,
                        source="唯一常驻召唤物·逐时刻环境结算",
                        power=power,
                        expected_duration=utilized,
                        utilized_duration=utilized,
                        utilization_ratio=1.0,
                        proportional_output=True,
                    )
                )
            for start, end, power, utilized in exact_theory:
                theory_rows.append(
                    _TheoreticalCast(
                        seconds=start,
                        key=key,
                        power=power,
                        end_seconds=end,
                        expected_duration=utilized,
                        utilized_duration=utilized,
                        proportional_output=True,
                    )
                )
            continue
        for start, end in output_windows:
            duration = max(0.0, end - start)
            if duration <= 1e-9:
                continue
            power = profile.power * duration / cycle_seconds
            actual_rows.append(
                _ValuedCast(
                    seconds=start,
                    key=key,
                    source="唯一常驻召唤物·Boss可输出区间持续伤害",
                    power=power,
                    expected_duration=duration,
                    utilized_duration=duration,
                    utilization_ratio=1.0,
                    proportional_output=True,
                )
            )
            theory_rows.append(
                _TheoreticalCast(
                    seconds=start,
                    key=key,
                    power=power,
                    end_seconds=end,
                )
            )
    return actual_rows, theory_rows


@lru_cache(maxsize=1)
def _scoring_fixed_cooldowns() -> dict[str, float]:
    rows = JOB_MECHANICS.global_value("cooldowns.fixed", {})
    if not isinstance(rows, dict):
        return {}
    refreshable_periodic = frozenset(
        _scoring_refreshable_periodic_durations()
    )
    return {
        canonical_skill(skill_name): max(0.0, as_number(cooldown))
        for skill_name, cooldown in rows.items()
        if canonical_skill(skill_name) in refreshable_periodic
        and as_number(cooldown) > 0
    }


@lru_cache(maxsize=1)
def _team_buff_data() -> dict[str, Any]:
    if not _TEAM_BUFF_DATA_PATH.exists():
        return {}
    try:
        value = json.loads(_TEAM_BUFF_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=1)
def _personal_attack_display_data() -> dict[str, tuple[float, float]]:
    """Return personal physical/magical attack buffs from the live catalog."""
    if not _PLAYER_BUFF_DISPLAY_PATH.exists():
        return {}
    try:
        payload = json.loads(_PLAYER_BUFF_DISPLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    result: dict[str, tuple[float, float]] = {}
    rows = payload.get("buffs", ()) if isinstance(payload, dict) else ()
    for row in rows:
        if not isinstance(row, dict) or norm(row.get("category")) != "攻击增益":
            continue
        skill_key = canonical_skill(row.get("skill"))
        display = norm(row.get("display"))
        if not skill_key or not display:
            continue
        match = re.search(r"\+\s*(\d+(?:\.\d+)?)\s*%", display)
        if match is None:
            continue
        magnitude = max(0.0, as_number(match.group(1)))
        physical = magnitude if "物攻" in display else 0.0
        magical = magnitude if "魔攻" in display else 0.0
        if physical > 0 or magical > 0:
            result[skill_key] = (physical, magical)
    return result


@lru_cache(maxsize=256)
def _team_buff_max_offensive_magnitude(skill_name: str) -> float:
    skill_key = canonical_skill(skill_name)
    values: list[float] = []
    for row in _team_buff_data().values():
        if not isinstance(row, dict) or canonical_skill(row.get("技能")) != skill_key:
            continue
        levels = row.get("等级", {})
        if not isinstance(levels, dict) or not levels:
            continue
        try:
            max_level = str(int(row.get("最大等级") or max(map(int, levels))))
        except (TypeError, ValueError):
            max_level = max(levels, key=lambda value: as_number(value))
        attrs = levels.get(max_level, {})
        if not isinstance(attrs, dict):
            continue
        for attribute, raw_value in attrs.items():
            attr = norm(attribute)
            if not (
                any(marker in attr for marker in _OFFENSIVE_BUFF_ATTRS)
                or attr.endswith("攻%")
                or attr == "双攻%"
            ):
                continue
            value = abs(as_number(raw_value))
            if value <= 0:
                continue
            if "%" in attr:
                values.append(value)
                continue
            if attr in {"物攻", "魔攻", "攻击"}:
                values.append(value / _STANDARD_ATTACK * 100.0)
            # Flat critical/stat points depend on the recipient's current
            # panel and cap. They are not stable percentages and must not be
            # compared directly with percentage attack or amplification.
    return max(values, default=0.0)


@lru_cache(maxsize=512)
def _team_buff_offensive_magnitude_at_level(
    skill_name: str,
    skill_level: int,
) -> float:
    """Return offensive magnitude at the learned/classic-server level."""
    if skill_level <= 0:
        skill_level = int(TEAM_EFFECT_LEVEL_CAPS.get(norm(skill_name), 0))
    if skill_level <= 0:
        return _team_buff_max_offensive_magnitude(skill_name)
    skill_key = canonical_skill(skill_name)
    values: list[float] = []
    for row in _team_buff_data().values():
        if not isinstance(row, dict) or canonical_skill(row.get("技能")) != skill_key:
            continue
        levels = row.get("等级", {})
        if not isinstance(levels, dict) or not levels:
            continue
        available_levels = sorted(
            int(as_number(level))
            for level in levels
            if int(as_number(level)) > 0
        )
        if not available_levels:
            continue
        resolved_level = max(
            (level for level in available_levels if level <= skill_level),
            default=available_levels[0],
        )
        attrs = levels.get(str(resolved_level), {})
        if not isinstance(attrs, dict):
            continue
        for attribute, raw_value in attrs.items():
            attr = norm(attribute)
            if not (
                any(marker in attr for marker in _OFFENSIVE_BUFF_ATTRS)
                or attr.endswith("攻%")
                or attr == "双攻%"
            ):
                continue
            value = abs(as_number(raw_value))
            if value <= 0:
                continue
            if "%" in attr:
                values.append(value)
            elif attr in {"物攻", "魔攻", "攻击"}:
                values.append(value / _STANDARD_ATTACK * 100.0)
    return max(values, default=0.0)


def _team_buff_effect_key(rows: Iterable[dict[str, Any]]) -> str:
    """Classify a team buff by the offensive stat granted at max level."""
    rows = tuple(rows)
    if not rows:
        return "团队Buff"
    max_level = max(as_number(row.get("等级")) for row in rows)
    attributes = {
        norm(row.get("属性"))
        for row in rows
        if abs(as_number(row.get("等级")) - max_level) < 1e-9
        and as_number(row.get("表格数值")) != 0
    }
    if any("最终" in attribute for attribute in attributes):
        return "最终%"
    elements = tuple(
        element
        for element in ("火", "水", "光", "暗")
        if any(f"{element}攻" in attribute for attribute in attributes)
    )
    if len(elements) == 1:
        return f"{elements[0]}攻%"
    if len(elements) > 1 or any("属性攻" in attribute for attribute in attributes):
        return "属性攻%"
    physical = any("物攻" in attribute or "物理攻击" in attribute for attribute in attributes)
    magical = any("魔攻" in attribute or "魔法攻击" in attribute for attribute in attributes)
    if physical and magical:
        return "双攻%"
    if physical:
        return "物攻%"
    if magical:
        return "魔攻%"
    if any(
        marker in attribute
        for attribute in attributes
        for marker in ("力量", "敏捷", "智力", "体质")
    ):
        return "四维%"
    if any("致命" in attribute for attribute in attributes):
        return "致命%"
    if any("动作速度" in attribute for attribute in attributes):
        return "动作速度%"
    if any("冷却" in attribute for attribute in attributes):
        return "冷却恢复%"
    return "团队Buff"


def _effective_team_buff_duration(skill_name: str, duration: float) -> float:
    return effective_team_buff_duration(skill_name, duration)


def _primary_player_buff_effect_rows(
    rows: Iterable[Any], expected_magnitude: float
) -> tuple[Any, ...]:
    """Choose the visible stat track, not every internal state of a buff."""
    by_target: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_target[str(getattr(row, "target_id", ""))].append(row)
    selected: list[Any] = []
    for target_rows in by_target.values():
        numeric_rows = [
            row for row in target_rows if getattr(row, "value", None) is not None
        ]
        if expected_magnitude > 0 and numeric_rows:
            selected.append(
                min(
                    numeric_rows,
                    key=lambda row: (
                        abs(as_number(row.value) - expected_magnitude)
                        / max(1.0, expected_magnitude),
                        -as_number(getattr(row, "coverage", 0.0)),
                    ),
                )
            )
        else:
            selected.append(
                max(
                    target_rows,
                    key=lambda row: (
                        as_number(getattr(row, "coverage", 0.0)),
                        as_number(getattr(row, "uptime_seconds", 0.0)),
                    ),
                )
            )
    return tuple(selected)


def _weighted_player_buff_coverage(rows: Iterable[Any]) -> float:
    weighted_coverage = 0.0
    total_weight = 0.0
    for row in rows:
        coverage = max(0.0, as_number(getattr(row, "coverage", 0.0)))
        uptime = max(0.0, as_number(getattr(row, "uptime_seconds", 0.0)))
        weight = uptime / coverage if coverage > 1e-9 and uptime > 0 else 1.0
        weighted_coverage += coverage * weight
        total_weight += weight
    return weighted_coverage / total_weight if total_weight > 0 else 0.0


def _source_boss_effect_intervals(
    events: Iterable[Any],
    source_id: str,
    end_seconds: float,
) -> tuple[tuple[float, float], ...]:
    """Return the coverage created by one provider's own applications.

    The live Boss overview resolves non-stacking effects to one currently
    effective value.  That ownership view is unsuitable for player scoring:
    when another player refreshes the same effect, it must not erase the first
    player's correctly performed cast.  Build each provider's duty coverage
    independently here; team-wide damage attribution can still de-duplicate
    overlapping effects separately.
    """
    by_target: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        if str(getattr(event, "source_id", "")) != source_id:
            continue
        by_target[str(getattr(event, "target_id", ""))].append(event)

    covered: list[tuple[float, float]] = []
    for target_events in by_target.values():
        intervals: list[list[float]] = []
        for event in sorted(
            target_events,
            key=lambda row: (
                as_number(getattr(row, "timestamp_seconds", 0.0)),
                0 if norm(getattr(row, "operation", "apply")) == "remove" else 1,
            ),
        ):
            timestamp = max(
                0.0,
                min(end_seconds, as_number(getattr(event, "timestamp_seconds", 0.0))),
            )
            operation = norm(getattr(event, "operation", "apply")).casefold()
            if operation == "remove":
                for interval in intervals:
                    if interval[0] <= timestamp < interval[1]:
                        interval[1] = timestamp
                continue

            event_end = as_number(getattr(event, "end_seconds", timestamp))
            if event_end <= timestamp:
                event_end = timestamp + max(
                    0.0, as_number(getattr(event, "duration_seconds", 0.0))
                )
            event_end = min(end_seconds, max(timestamp, event_end))
            if event_end > timestamp + 1e-9:
                intervals.append([timestamp, event_end])

        covered.extend(
            (start, stop)
            for start, stop in intervals
            if stop > start + 1e-9
        )
    return _merge_intervals(covered)


def grade_for_score(score: float) -> str:
    if score >= 90:
        return "SSS"
    if score >= 85:
        return "SS"
    if score >= 80:
        return "S"
    if score >= 75:
        return "A"
    if score >= 70:
        return "B"
    if score >= 65:
        return "C"
    if score >= 60:
        return "D"
    if score >= 50:
        return "E"
    return "F"


def role_for_job(job: str) -> str:
    job_name = norm(job)
    if job_name in HEALER_JOBS:
        return "奶/辅助"
    if job_name in TANK_JOBS:
        return "T/辅助"
    return "输出"


def role_reason_for_job(job: str) -> str:
    job_name = norm(job)
    if job_name in HEALER_JOBS:
        return "团队恢复能力与核心增益职责"
    if job_name in TANK_JOBS:
        return "挑衅、减攻击、保护能力与团队增益职责"
    return HYBRID_SUPPORT_REASONS.get(
        job_name, "团队效果强度未达输出/辅助定位阈值"
    )


@lru_cache(maxsize=128)
def _optional_support_effect_skills_for_job(job: str) -> frozenset[str]:
    configured = JOB_MECHANICS.job_value(
        data_job_for(job), "optional_support_effects", ()
    )
    job_specific = {
        skill_key
        for skill in configured
        if (skill_key := canonical_skill(skill))
    }
    return _OPTIONAL_SUPPORT_EFFECT_SKILLS | frozenset(job_specific)


def yao_rotation_variant(job: str, meditation_coverage: float) -> str:
    """Resolve Yao's scoring rotation from recorded Chakra Cure uptime."""

    job_name = norm(job)
    if job_name not in _YAO_JOBS and data_job_for(job_name) != "曜":
        return job_name
    return (
        "打坐曜"
        if max(0.0, meditation_coverage) >= _YAO_MEDITATION_ROLE_THRESHOLD
        else "输出曜"
    )


def alchemist_rotation_variant(job: str, used_sacrifice: bool) -> str:
    """Resolve the Apothecary's scoring duty from an observed Sacrifice."""

    job_name = norm(job)
    if job_name not in _ALCHEMIST_JOBS:
        return job_name
    return "辅助药剂" if used_sacrifice else "输出药剂"


@lru_cache(maxsize=1)
def _freeze_gain_per_layer() -> float:
    return max(
        0.0,
        as_number(JOB_MECHANICS.global_value("freeze_support.gain_per_layer", 15.0)),
    )


@lru_cache(maxsize=128)
def _freeze_max_layers(job: str) -> int:
    default_layers = max(
        1,
        int(
            as_number(
                JOB_MECHANICS.global_value(
                    "freeze_support.default_max_layers", 2
                )
            )
        ),
    )
    by_job = JOB_MECHANICS.global_value("freeze_support.max_layers_by_job", {})
    job_name = data_job_for(job)
    if not isinstance(by_job, dict):
        return default_layers
    return max(1, int(as_number(by_job.get(job_name), default_layers)))


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, value))


def _tank_duty_completion(
    attack_down_score: float,
    shell_score: float,
    taunt_score: float,
) -> float:
    """Combine the three equal T duties without hiding above-theory credit."""

    return max(
        0.0,
        (
            as_number(attack_down_score)
            + as_number(shell_score)
            + as_number(taunt_score)
        )
        / 3.0,
    )


def _overflow_balanced_score_contributions(
    output_score: float,
    support_score: float,
    output_weight: float,
    support_weight: float,
) -> tuple[float, float]:
    """Apply support overflow to the output coefficient before scoring it.

    Output and support share one fixed point budget. A raw score above 100 may
    consume the other category's nominal allocation, but can never make their
    combined contribution exceed that shared budget. Opposing overflows retain
    their existing netting rule. If support reduces the output allocation, an
    incomplete output score earns only that fraction of the reduced allocation,
    rather than keeping the original contribution until it reaches a ceiling.
    The caller's displayed nominal weights and specialty reserve stay unchanged.
    """

    output_weight = max(0.0, as_number(output_weight))
    support_weight = max(0.0, as_number(support_weight))
    output_points = max(0.0, as_number(output_score)) * output_weight
    support_points = max(0.0, as_number(support_score)) * support_weight
    nominal_output = 100.0 * output_weight
    nominal_support = 100.0 * support_weight
    shared_budget = nominal_output + nominal_support
    if shared_budget <= 1e-12:
        return 0.0, 0.0

    output_overflow = max(0.0, output_points - nominal_output)
    support_overflow = max(0.0, support_points - nominal_support)
    support_budget = max(
        0.0,
        min(
            shared_budget,
            nominal_support + support_overflow - output_overflow,
        ),
    )
    output_budget = shared_budget - support_budget
    if support_overflow > 0.0 and output_budget < nominal_output:
        # e.g. output=80, support=150, weights=80%/20%:
        # support receives 30 points; output earns 80% of the remaining 70.
        output_points = max(0.0, as_number(output_score)) * (output_budget / 100.0)
    return (
        min(output_points, output_budget),
        min(support_points, support_budget),
    )


def _ratio_score(
    actual: float,
    theoretical: float,
    neutral: float = 100.0,
    *,
    allow_overflow: bool = False,
) -> float:
    if theoretical <= 1e-9:
        return neutral
    value = max(0.0, actual / theoretical * 100.0)
    return value if allow_overflow else _clamp_score(value)


from .score_survival import bind_host_globals as _bind_score_survival
from .score_survival import (
    _excluded_from_score_for_low_damage,
    _high_value_skill_keys,
    _confirmed_player_death_count,
    _snapshot_recording_end_seconds,
    _hp_survival_shadow_detail,
    _simulated_danger_hp_result,
    _snapshot_residual_hp_loss_events,
    _survival_discipline_detail,
    _apply_survival_penalty_cap,
)


from .score_intervals import bind_host_globals as _bind_score_intervals
from .score_intervals import (
    _support_completion_is_scored,
    _in_intervals,
    _timing_efficiency,
    _merge_intervals,
    _clip_intervals,
    _bounded_recorded_team_buff_intervals,
    _intersect_intervals,
    _subtract_intervals,
    _allocate_capped_interval_layers,
    _interval_duration,
    _coverage_in_windows,
    _builtin_skill_builds,
)


from .score_engine import ScoreEngineMixin, bind_host_globals as _bind_ScoreEngineMixin
from .score_support_context import ScoreSupportContextMixin, bind_host_globals as _bind_ScoreSupportContextMixin
from .score_profiles import ScoreProfilesMixin, bind_host_globals as _bind_ScoreProfilesMixin
from .score_glyphs import ScoreGlyphsMixin, bind_host_globals as _bind_ScoreGlyphsMixin
from .score_rotation import ScoreRotationMixin, bind_host_globals as _bind_ScoreRotationMixin
from .score_support import ScoreSupportMixin, bind_host_globals as _bind_ScoreSupportMixin
from .score_role_duties import ScoreRoleDutiesMixin, bind_host_globals as _bind_ScoreRoleDutiesMixin

class TeamScoreEngine(ScoreEngineMixin, ScoreSupportContextMixin, ScoreProfilesMixin, ScoreGlyphsMixin, ScoreRotationMixin, ScoreSupportMixin, ScoreRoleDutiesMixin):
    """Standardized rotation and support scoring for captured combat records.

    Captured raw damage is intentionally not compared with a default-geared
    simulator character. Instead, observed casts are re-valued with one common
    level-70 skill panel and the skill glyphs proven by this combat capture.
    """

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self._score_boss_name = "红龙佩达"
        self._score_boss_defense = _PEDA_DEFENSE
        self._score_boss_crit_resist = _PEDA_CRIT_RESIST
        self._score_boss_element_resist = _PEDA_ELEMENT_RESIST_PERCENT
        self._score_boss_signature = (
            self._score_boss_name,
            self._score_boss_defense,
            self._score_boss_crit_resist,
            self._score_boss_element_resist,
        )
        self._configured_support_gain_cache: dict[_SupportExpectation, float] = {}
        self._configured_support_recipient_gain_cache: dict[
            tuple[_SupportExpectation, str], float
        ] = {}
        self._score_crit_profiles_by_source: dict[str, tuple[float, float]] = {}
        self._score_neutral_profiles_by_source: dict[str, float] = {}
        self._base_profile_cache: dict[str, tuple[SkillProfile, ...]] = {}
        self._profile_cache: dict[str, tuple[SkillProfile, ...]] = {}
        self._learned_cache: dict[str, dict[str, int]] = {}
        self._damage_profile_cache: dict[str, dict[str, float]] = {}
        self._personal_attack_bonus_cache: dict[str, tuple[float, float]] = {}
        self._support_expectation_cache: dict[
            str, tuple[_SupportExpectation, ...]
        ] = {}
        self._freeze_interval_snapshot: CombatSnapshot | None = None
        self._freeze_interval_cache: dict[
            tuple[str, str, float, float, tuple[str, ...]],
            tuple[tuple[float, float], ...],
        ] = {}
        self._theory_freeze_hit_snapshot: CombatSnapshot | None = None
        self._theory_freeze_hit_cache: dict[
            tuple[tuple[str, ...], tuple[tuple[float, float], ...]],
            tuple[float, ...],
        ] = {}
        self._cast_timing_snapshot: CombatSnapshot | None = None
        self._cast_timing_cache: dict[
            tuple[str, int, str], tuple[float, float]
        ] = {}
        self._score_runtime_support_context: (
            _SupportCounterfactualContext | None
        ) = None
        self._score_runtime_support_benchmarks: dict[str, float] = {}

    @property
    def score_boss_name(self) -> str:
        return self._score_boss_name

    def set_boss_profile(
        self,
        name: str,
        profile: dict[str, Any] | None,
    ) -> None:
        """Select the build-simulator Boss parameters used by live scoring."""

        row = profile if isinstance(profile, dict) else {}
        boss_name = norm(name) or "红龙佩达"
        defense = max(0.0, as_number(row.get("monster_defense"), _PEDA_DEFENSE))
        crit_resist = max(0.0, as_number(row.get("crit_resist"), _PEDA_CRIT_RESIST))
        element_resist = as_number(
            row.get("element_resist"), _PEDA_ELEMENT_RESIST_PERCENT
        )
        signature = (boss_name, defense, crit_resist, element_resist)
        if signature == self._score_boss_signature:
            return
        self._score_boss_name = boss_name
        self._score_boss_defense = defense
        self._score_boss_crit_resist = crit_resist
        self._score_boss_element_resist = element_resist
        self._score_boss_signature = signature
        self._configured_support_gain_cache.clear()
        self._configured_support_recipient_gain_cache.clear()


    def score(
        self,
        snapshot: CombatSnapshot,
        theory_plans: dict[str, dict[str, Any]] | None = None,
        *,
        boss_target_ids: frozenset[str] | None = None,
        specialty_windows: tuple[tuple[float, float], ...] | None = None,
        death_windows: tuple[tuple[float, float], ...] | None = None,
        actual_timeline_events: tuple[SkillCastEvent, ...] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        allow_parallel: bool = True,
        score_source_ids: frozenset[str] | None = None,
        precomputed_support_context: _SupportCounterfactualContext | None = None,
        precomputed_support_benchmarks: dict[str, float] | None = None,
    ) -> tuple[PlayerScore, ...]:
        score_started = time.perf_counter()
        event_index_started = time.perf_counter()
        actual_timeline_events = _score_cast_axis(actual_timeline_events)
        authoritative_cast_axis = actual_timeline_events is not None
        score_snapshot = (
            replace(snapshot, skill_cast_events=actual_timeline_events)
            if authoritative_cast_axis
            else snapshot
        )
        plans = theory_plans or {}
        # Maximum-HP priority is protocol 21 0C, then the highest 08 6F state
        # outside every temporary HP/constitution buff. Theory HP is used only
        # when neither factual source exists. The underlying current-HP points
        # and effective losses remain untouched.
        theory_hp_by_source: dict[str, int] = {}
        for source_id, plan in plans.items():
            summary = plan.get("summary", {}) if isinstance(plan, dict) else {}
            if not isinstance(summary, dict):
                continue
            theory_hp = round(as_number(summary.get("HP")))
            if theory_hp > 0:
                theory_hp_by_source[str(source_id)] = theory_hp
        if theory_hp_by_source:
            score_snapshot = replace(
                score_snapshot,
                incoming_damage_events=tuple(
                    replace(
                        event,
                        target_max_hp=theory_hp_by_source.get(
                            str(event.target_id), 0
                        ),
                    )
                    if event.target_max_hp <= 0
                    and theory_hp_by_source.get(str(event.target_id), 0) > 0
                    else event
                    for event in event_platform_for(
                        score_snapshot
                    ).incoming_damage_events
                ),
                player_health_tracks=tuple(
                    replace(
                        track,
                        max_hp=theory_hp_by_source[str(track.target_id)],
                        current_percent=max(
                            0.0,
                            min(
                                100.0,
                                track.current_hp
                                / theory_hp_by_source[str(track.target_id)]
                                * 100.0,
                            ),
                        ),
                        max_hp_source="theory_plan",
                        exact_percent=False,
                        points=tuple(
                            replace(
                                point,
                                percent=max(
                                    0.0,
                                    min(
                                        100.0,
                                        point.current_hp
                                        / theory_hp_by_source[str(track.target_id)]
                                        * 100.0,
                                    ),
                                ),
                            )
                            for point in track.points
                        ),
                    )
                    if (
                        str(track.target_id) in theory_hp_by_source
                        and (
                            track.max_hp <= 0
                            or track.max_hp_source
                            == "missing_protocol_and_unbuffed_086f"
                        )
                    )
                    else track
                    for track in event_platform_for(
                        score_snapshot
                    ).player_health_tracks
                ),
            )
        # Build once before support/player scoring. Every fresh per-player
        # engine, including process workers, then reuses the same projection.
        score_platform = event_platform_for(score_snapshot)
        event_index_seconds = time.perf_counter() - event_index_started
        output_windows = self._output_windows(score_snapshot, boss_target_ids)
        resolved_specialty_windows = _merge_intervals(
            specialty_windows or output_windows
        )
        resolved_death_windows = _merge_intervals(
            death_windows or resolved_specialty_windows
        )
        self._score_crit_profiles_by_source = {}
        self._score_neutral_profiles_by_source = {}
        self._configured_support_recipient_gain_cache.clear()
        for player in score_platform.players:
            plan = plans.get(player.source_id, {})
            summary = plan.get("summary", {}) if isinstance(plan, dict) else {}
            if not isinstance(summary, dict):
                summary = {}
            self._score_neutral_profiles_by_source[str(player.source_id)] = max(0.0, as_number(summary.get('无属性%')))
            self._score_crit_profiles_by_source[str(player.source_id)] = (
                min(
                    90.0,
                    max(
                        0.0,
                        as_number(
                            summary.get("crit_percent"),
                            _STANDARD_CRIT_PERCENT,
                        ),
                    ),
                ),
                min(
                    100.0,
                    max(
                        0.0,
                        as_number(
                            summary.get("crit_damage_percent"),
                            _STANDARD_CRIT_DAMAGE_PERCENT,
                        ),
                    ),
                ),
            )
        support_context_started = time.perf_counter()
        support_context = precomputed_support_context
        if support_context is None:
            if progress_callback is not None:
                progress_callback(0, 1, "辅助收益索引")
            support_context = self._build_support_counterfactual_context(
                score_snapshot,
                output_windows,
                boss_target_ids,
                plans,
            )
        support_context_seconds = time.perf_counter() - support_context_started
        results: list[PlayerScore] = []
        scoring_players = tuple(
            player
            for player in score_platform.players
            if score_source_ids is None
            or norm(player.source_id) in score_source_ids
        )
        total_players = len(scoring_players)
        parallel_results: tuple[PlayerScore, ...] | None = None
        # Keep the two process-pool phases ordered.  Running profession support
        # benchmarks in a background thread while player scoring submitted to
        # the same persistent ProcessPoolExecutor could leave both callers
        # waiting on futures after an exclusion-axis replan.  The workers were
        # already idle at that point, so the score job remained at 8/8 forever.
        # Reusing the warm pool sequentially preserves the exact score formula
        # without creating another set of long-lived worker processes.
        player_scores_started = time.perf_counter()
        if allow_parallel:
            try:
                from ..timeline_parallel import calculate_team_score_players_parallel

                parallel_results = calculate_team_score_players_parallel(
                    score_snapshot,
                    scoring_players,
                    output_windows,
                    plans,
                    boss_target_ids,
                    support_context,
                    resolved_specialty_windows,
                    authoritative_cast_axis,
                    self._score_boss_name,
                    {
                        "monster_defense": self._score_boss_defense,
                        "crit_resist": self._score_boss_crit_resist,
                        "element_resist": self._score_boss_element_resist,
                    },
                    self._score_crit_profiles_by_source,
                    death_windows=resolved_death_windows,
                    progress_callback=progress_callback,
                    neutral_profiles_by_source=self._score_neutral_profiles_by_source,
                )
            except Exception:
                parallel_results = None
        if parallel_results is not None:
            results.extend(parallel_results)
        else:
            for player_index, player in enumerate(
                scoring_players, start=1
            ):
                results.append(self._score_player(
                    score_snapshot,
                    player,
                    output_windows,
                    plans.get(player.source_id),
                    boss_target_ids,
                    support_context,
                    resolved_specialty_windows,
                    resolved_death_windows,
                    authoritative_cast_axis,
                ))
                if progress_callback is not None:
                    progress_callback(player_index, total_players, player.job)
        player_scores_seconds = time.perf_counter() - player_scores_started
        support_benchmarks_started = time.perf_counter()
        support_benchmarks = precomputed_support_benchmarks
        if support_benchmarks is None:
            support_benchmarks = self._all_job_counterfactual_support_indexes(
                score_snapshot,
                output_windows,
                support_context,
                allow_parallel=allow_parallel,
                progress_callback=progress_callback,
            )
        support_benchmarks_seconds = (
            time.perf_counter() - support_benchmarks_started
        )
        results = list(self._apply_counterfactual_support_weights(
            results,
            support_benchmarks,
        ))
        self._score_runtime_support_context = support_context
        self._score_runtime_support_benchmarks = support_benchmarks
        self._score_runtime_diagnostics = {
            "event_index_seconds": event_index_seconds,
            "support_context_seconds": support_context_seconds,
            "player_scores_seconds": player_scores_seconds,
            "support_benchmarks_seconds": support_benchmarks_seconds,
            "scored_player_count": total_players,
            "reused_support_context": precomputed_support_context is not None,
            "reused_support_benchmarks": precomputed_support_benchmarks is not None,
            "total_seconds": time.perf_counter() - score_started,
        }
        return tuple(sorted(results, key=lambda row: row.total_score, reverse=True))

# V430_SPLIT_MODULES_APPLIED
_bind_ScoreEngineMixin(globals())
_bind_ScoreSupportContextMixin(globals())
_bind_ScoreProfilesMixin(globals())
_bind_ScoreGlyphsMixin(globals())
_bind_ScoreRotationMixin(globals())
_bind_ScoreSupportMixin(globals())
_bind_ScoreRoleDutiesMixin(globals())

# V430_SCORING_HELPER_SPLIT_APPLIED
_bind_score_survival(globals())
_bind_score_intervals(globals())
