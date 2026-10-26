from __future__ import annotations

import copy
import ctypes
import dataclasses
import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import socket
import threading
import time
import traceback
import uuid
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from ctypes import wintypes
from typing import Any, Callable

os.environ.setdefault("DRAGON_NEST_NATIVE_NO_QT", "1")

from .calculator import BuildCalculator
from .effect_stacking import (
    EFFECT_STACKING_ENGINE,
    EffectInput,
    amplification_for_channel,
    attack_increase_for_channel,
    canonical_effect_key,
    defense_reduction_for_attack,
    effect_rule,
    fixed_attack_for_channel,
    skill_percent_amplified_flat_attack,
)
from .data_loader import (
    DATA_DIR,
    MODULAR_DIR,
    TALISMAN_SLOTS,
    TALISMAN_SLOT_PERCENT,
    DataStore,
    as_number,
    buff_display_label,
    buff_exclusive_group,
    norm,
    unique,
)
from .live.aggregator import CombatAggregator, _player_job_stages
from .live.auto_capture_stop import V576AutoCaptureStopDetector
from .live.boss_recognizer import BossEncounter, BossRecognizer
from .live.boss_hp import normalize_boss_hp_name, resolve_fixed_boss_hp_profile
from .live.collector import LiveDpsCollector
from .live.models import CombatSnapshot, PlayerInspectEvent
from .live.event_platform import event_platform_for
from .live.cast_timing import LiveCastTimingResolver
from .live.effects import boss_effect_rules
from .live.history import default_history_path
from .live.profile import ClientProfile
from .live.scoring import (
    PlayerScore,
    TeamScoreEngine,
    _snapshot_recording_end_seconds,
)
from .live.theory_planner import TeamScoreTheoryPlanner
from .live.sources import (
    JsonlReplaySource,
    V576AutoCaptureProbe,
    V576DiagnosticReplaySource,
    scan_v576_diagnostic_map_history,
    V576PcapReplaySource,
)
from .live.npcap import find_npcap_dll, set_current_thread_low_priority
from .live.replay_cache import ReplayDiskCache, replay_cache_identity

V410_USER_CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "DragonNestBuildSimulator"
APP_VERSION_COMPACT = re.sub(r"\D", "", os.getenv("DRAGON_NEST_APP_VERSION", "4.10")) or "410"
from .native_timeline import NativeTimelinePlanner
from .profile_panel import ProfilePanelCalculator
from .skill_glyph_policy import (
    apply_damage_cooldown_glyph_policy,
    approximate_team_objective_gain_ratio,
    glyph_marginal_score_multiplier,
    linked_cooldown_glyph_skills,
    select_skill_glyphs,
    select_skill_glyphs_two_stage,
)
from .mechanism_timeline import (
    active_seconds as mechanism_active_seconds,
    blocked_seconds as mechanism_blocked_seconds,
    lightweight_cdr_buff_schedule,
    lightweight_team_buff_schedule,
    load_mechanism_templates,
    normalize_mechanism_intervals,
)
from .timeline_engine import PureTimelineRuntime, TimelineSimulationSnapshot, TimelineValue
from .timeline_parallel import reset_timeline_process_pool
from .legacy_timeline import LegacyTimelineService


NONE_TEXT = "无"
EQUIPMENT_SLOTS = ("头盔", "上装", "下装", "手套", "鞋子", "主手", "副手")
JEWELRY_SLOTS = ("项链", "耳环", "戒指")
FASHION_SLOTS = ("主手", "副手", "翅膀", "尾巴", "印花", "精灵", "项链", "耳环", "戒指1", "戒指2")
ENGRAVING_SLOTS = (
    "头盔", "上装", "下装", "手套", "鞋子", "主手", "副手",
    "项链", "耳环", "戒指1", "戒指2", "翅膀", "尾巴", "印花",
)
MASTERY_NAMES = ("最大生命值", "最大魔法值", "物理攻击力", "魔法攻击力", "物理防御", "魔法防御", "致命一击", "最终伤害")
CARD_PREVIEW_ATTRS = ("HP", "MP", "体质", "力量", "敏捷", "智力", "最终", "物攻", "致命", "致命伤害", "防御", "魔攻", "魔防")
CARD_MASTERY_ATTR_MAP = {
    "破坏": "物攻", "魔力": "魔攻", "巨熊": "力量", "疾风": "敏捷",
    "智慧": "智力", "健康": "体质", "致命": "致命", "最终": "最终",
    "铁壁": "防御", "帷幕": "魔防", "生命": "HP", "法力": "MP",
}
AUTO_EQUIP_TARGETS = {
    "50": (("霸龙L装备", "equipment", "50L"), ("霸龙L首饰", "jewelry", "50L")),
    "60": (("赞龙L装备", "equipment", "60L"), ("赞龙L首饰", "jewelry", "60L"), ("赞龙S首饰", "jewelry", "60S")),
    "70": (
        ("黑龙L装备+首饰", "all", "70L"),
        ("黑龙L装备", "equipment", "70L"),
        ("黑龙L首饰", "jewelry", "70L"),
        ("黑龙S装备", "equipment", "70S"),
        ("黑龙S首饰", "jewelry", "70S"),
    ),
}
BOSS_PRESETS = {
    "梦魇·审判者天启": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},
    "梦魇·狮蝎领主克雷姆": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},

    "梦魇·海龙萨芬特拉": {"crit_resist": 12000, "element_resist": 20.0, "monster_defense": 11000, "freeze_coefficient": 0.3},
    "红龙佩达": {"crit_resist": 19000, "element_resist": 20.0, "monster_defense": 4950, "freeze_coefficient": 0.3},
    "天堂龙灾·佩达": {"crit_resist": 18000, "element_resist": 10.0, "monster_defense": 9000, "freeze_coefficient": 0.3},
}

# The overlay's key-team-Buff reminder uses the common high-grade cooldown
# glyphs for these two skills. They remain immune to external cooldown effects;
# this only shortens their own reference cooldown shown by the reminder.
_OVERLAY_DEFAULT_COOLDOWN_GLYPH_PERCENT = {
    "精神爆发": 12.0,
    "时间加速": 6.0,
}

# Passive/mastery rows remain available to the build calculator and packet
# decoder, but they are panel statistics rather than useful timed overlay
# statuses. Keep them out of both the selectable list and rendered Buff rows.
_OVERLAY_HIDDEN_BUFF_SKILL_NAMES = frozenset(
    {
        "力量掌握",
        "精神掌握",
        "精神训练",
        "荆棘",
        "长弓掌握",
        "十字弓掌握",
        "物理攻击掌握",
        "致命暴击",
        "魔法暴雪",
        "魔法攻击掌握",
        "短弓掌握",
        "地狱之火",
        "火焰大师",
        "寒冰大师",
        "魔杖掌握",
        "刀锋之力",
        "灵魂之力",
        "力量提升",
        "敏捷提升",
        "智力提升",
        "体质提升",
    }
)

# These toggled auras have no finite duration/cooldown in the skill table.
# Other full-time team Buffs are derived from their level-70 duration versus
# cooldown below, so short burst skills cannot become missing-Buff alerts just
# because one packet component happens to look open-ended.
_OVERLAY_PERMANENT_TEAM_AURA_SKILL_NAMES = frozenset(
    {"元素光环", "判定光环", "火焰光环", "寒冰光环"}
)

# Long-duration emergency skills are selectable team Buffs, but they are not
# maintainable throughout a fight.  Keep them out of the automatic
# "missing full-time Buff" reminder even when their one-shot duration reaches
# the generic 60-second maintenance threshold.
_OVERLAY_NON_FULL_DURATION_TEAM_BUFF_SKILL_NAMES = frozenset({"高地人"})


def _overlay_full_duration_team_buff(
    store: DataStore,
    skill: object,
    providers: list[str],
) -> bool:
    skill_name = norm(skill)
    if not skill_name:
        return False
    if skill_name in _OVERLAY_NON_FULL_DURATION_TEAM_BUFF_SKILL_NAMES:
        return False
    if skill_name in _OVERLAY_PERMANENT_TEAM_AURA_SKILL_NAMES:
        return True
    activation = store.buff_activation_rule("通用", skill_name, "团队buff")
    if norm(activation.get("mode")).casefold() in {
        "permanent", "mode", "intermittent_refresh"
    }:
        return True
    for provider in providers:
        timing = store.skill_effect_timing(provider, skill_name, max_required_level=70)
        duration = max(0.0, as_number(timing.get("duration")))
        cooldown = max(0.0, as_number(timing.get("cooldown")))
        # Long maintenance Buffs (typically 150-300s) and Buffs whose duration
        # covers their own cooldown are functionally full-time. Short burst,
        # heal-zone and defensive windows stay out of automatic monitoring.
        if duration >= 60.0 or (cooldown > 0.0 and duration + 0.05 >= cooldown):
            return True
    return False

_SCORE_OUTPUT_EXTRA_ELEMENTS: dict[str, frozenset[str]] = {
    "烈": frozenset({"火", "暗"}),
    "冰灵": frozenset({"水", "火"}),
    "炼金圣士": frozenset({"水", "火"}),
    "药剂师": frozenset({"水", "火"}),
    "暗": frozenset({"光", "暗"}),
    "曜": frozenset({"光", "暗"}),
    "输出曜": frozenset({"光", "暗"}),
    "打坐曜": frozenset({"光", "暗"}),
}
_SCORE_OUTPUT_MIXED_ATTACK_JOBS = frozenset({"圣骑士", "十字军"})


def _advance_live_capture_clock(
    *,
    now: float,
    event_clock_ms: int,
    event_revision: int,
    anchor_monotonic: float,
    anchor_seconds: float,
    last_event_ms: int,
) -> tuple[float, float, int, float]:
    """Advance the one capture-relative clock shared by live UI consumers.

    ``event_clock_ms`` is the newest raw capture-frame time relative to the
    accepted session boundary (21 0C 00 for automatic capture).  Every new raw
    sample is authoritative; only a genuinely quiet network interval is
    extrapolated with the monotonic clock.  This prevents decoder backlog and
    loop scheduling jitter from accumulating into a multi-second overlay
    offset during long encounters.
    """

    initialized = anchor_monotonic > 0.0
    projected_seconds = (
        max(0.0, anchor_seconds + now - anchor_monotonic)
        if initialized
        else 0.0
    )
    has_new_event = (
        (not initialized and event_revision > 0)
        or event_clock_ms > last_event_ms
    )
    if has_new_event:
        anchor_monotonic = now
        anchor_seconds = max(0.0, event_clock_ms / 1000.0)
        last_event_ms = max(last_event_ms, event_clock_ms)
        projected_seconds = anchor_seconds
    return (
        anchor_monotonic,
        anchor_seconds,
        last_event_ms,
        projected_seconds,
    )


def _score_output_curve_specs(
    engine: TeamScoreEngine, score: PlayerScore,
) -> list[dict[str, str]]:
    """Expose the same job-relevant state-curve order used by V4.01."""

    job = norm(score.rotation_job or score.job)
    profile = engine._job_damage_profile(job)
    attacks = {
        label
        for label, key in (("物理", "physical"), ("魔法", "magical"))
        if as_number(profile.get(key)) > 1e-6
    }
    if job in _SCORE_OUTPUT_MIXED_ATTACK_JOBS:
        attacks.update({"物理", "魔法"})
    if not attacks:
        attacks.add("物理")
    elements = {
        label
        for label, key in (
            ("火", "fire"), ("水", "water"), ("光", "light"), ("暗", "dark")
        )
        if as_number(profile.get(key)) > 1e-6
    }
    elements.update(_SCORE_OUTPUT_EXTRA_ELEMENTS.get(job, ()))

    rows: list[dict[str, str]] = []
    if "物理" in attacks:
        rows.append({"key": "物理增伤合计%", "label": "物理增伤", "color": "#e56b61"})
    if "魔法" in attacks:
        rows.append({"key": "魔法增伤合计%", "label": "魔法增伤", "color": "#b06cc7"})
    for element, color in (
        ("火", "#e66a42"), ("水", "#3196c7"),
        ("光", "#d4a72c"), ("暗", "#7656ad"),
    ):
        if element in elements:
            rows.append({"key": f"减{element}抗%", "label": f"减{element}抗", "color": color})
    rows.append({"key": "减致命抵抗%", "label": "减致命抗", "color": "#c7873f"})
    if "物理" in attacks:
        rows.append({"key": "减物理防御%", "label": "减物理防御", "color": "#9b6b43"})
    if "魔法" in attacks:
        rows.append({"key": "减魔法防御%", "label": "减魔法防御", "color": "#6f789e"})
    return rows


def _score_improvement_rows(score: PlayerScore) -> list[dict[str, object]]:
    """Expose the same Boss-scoped improvement analysis used by V4.01."""

    def priority(value: float) -> str:
        if value >= 5.0:
            return "高"
        if value >= 2.0:
            return "中"
        return "低"

    output_rows: list[dict[str, object]] = []
    theory_total = max(1e-9, score.theory_standard_power)
    completion_weight = 70.0 if score.burst_windows else 100.0
    for detail in score.skill_details:
        if detail.excluded_from_score or detail.theory_power <= 0:
            continue
        missing_power = max(0.0, detail.theory_power - detail.actual_power)
        if missing_power <= max(1.0, detail.theory_power * 0.02):
            continue
        estimated_gain = missing_power / theory_total * completion_weight
        missing_casts = max(0, detail.theory_count - detail.actual_count)
        if missing_casts > 0:
            advice = (
                f"在可输出区间内争取多释放 {missing_casts} 次；"
                "技能转好时优先避免被低收益动作挤占。"
            )
        elif detail.proportional_output and detail.duration_utilization < 0.98:
            advice = (
                f"减少提前中断，当前持续段只兑现 "
                f"{detail.duration_utilization * 100:.1f}%。"
            )
        else:
            advice = (
                "次数接近理论但标准伤害偏低；优先放进增伤、减抗、"
                "属性攻或动作加速窗口，并保证多段伤害完整命中。"
            )
        output_rows.append({
            "direction": "输出分",
            "priority": priority(estimated_gain),
            "item": detail.skill_name,
            "current": (
                f"{detail.actual_count}/{detail.theory_count}次；"
                f"完成 {detail.completion:.1f}%"
            ),
            "advice": advice,
            "gain": f"输出分理论可追回约 +{estimated_gain:.2f}",
            "sort_value": estimated_gain,
        })
    output_rows.sort(key=lambda row: -as_number(row.get("sort_value")))
    output_rows = output_rows[:6]

    burst_gap = max(0.0, 100.0 - score.burst_usage)
    if burst_gap > 0.5:
        weakest_windows = sorted(
            (row for row in score.burst_windows if row.completion < 98.0),
            key=lambda row: (row.completion, -row.theory_power),
        )[:3]
        window_text = "、".join(
            f"{row.label}({row.completion:.1f}%)" for row in weakest_windows
        ) or "精神爆发/时间加速窗口"
        estimated_gain = burst_gap * 0.30
        output_rows.append({
            "direction": "输出分",
            "priority": priority(estimated_gain),
            "item": "爆发/加速窗口",
            "current": f"兑现率 {score.burst_usage:.1f}%",
            "advice": (
                f"优先改善 {window_text}；把高标准伤害技能和可受益的"
                "长CD技能移入窗口，减少窗口内空挡。"
            ),
            "gain": f"输出分理论可追回约 +{estimated_gain:.2f}",
            "sort_value": estimated_gain,
        })
    output_rows.sort(key=lambda row: -as_number(row.get("sort_value")))

    support_rows: list[dict[str, object]] = []
    for detail in score.support_details:
        if not detail.included_in_score:
            continue
        contribution_completion = (
            detail.actual_contribution / detail.theory_contribution * 100.0
            if detail.theory_contribution > 1e-9
            else detail.completion
        )
        effective_completion = min(detail.completion, contribution_completion)
        weighted_gap = max(
            0.0,
            detail.score_weight * (100.0 - effective_completion) / 100.0,
        )
        if weighted_gap <= 0.2:
            continue
        actual_seconds = sum(
            max(0.0, end - start)
            for start, end in (
                detail.actual_display_intervals or detail.actual_intervals
            )
        )
        theory_seconds = sum(
            max(0.0, end - start)
            for start, end in (
                detail.theory_display_intervals or detail.theory_intervals
            )
        )
        if actual_seconds <= 1e-6:
            advice = (
                "所选 Boss 阶段没有形成有效覆盖；确认技能已学习并在"
                "Boss可输出后尽早施放。"
            )
        elif "冷却" in detail.effect_key or detail.skill_name in {
            "精神爆发", "时间加速", "鸡尾酒",
        }:
            advice = (
                "避开 Boss 即将不可输出的时段，并把窗口放在团队高伤技能"
                "集中转好的位置；同名效果不要重叠浪费。"
            )
        else:
            advice = (
                "按CD补齐断档，优先覆盖Boss实际受击区间；存在同名来源时"
                "错开施放，避免重复覆盖。"
            )
        support_rows.append({
            "direction": "辅助分",
            "priority": priority(weighted_gap),
            "item": f"{detail.skill_name} · {detail.effect_key}",
            "current": (
                f"覆盖 {detail.actual_coverage * 100:.1f}%/"
                f"{detail.theory_coverage * 100:.1f}%；"
                f"{actual_seconds:.1f}/{theory_seconds:.1f}s"
            ),
            "advice": advice,
            "gain": f"加权辅助缺口约 {weighted_gap:.2f}",
            "sort_value": weighted_gap,
        })
    support_rows.sort(key=lambda row: -as_number(row.get("sort_value")))
    support_rows = support_rows[:8]

    rows = [*output_rows, *support_rows]
    if not rows:
        rows.append({
            "direction": "综合",
            "priority": "低",
            "item": "当前未发现明显短板",
            "current": f"输出 {score.output_score:.1f} / 辅助 {score.support_score:.1f}",
            "advice": "当前已接近本职业在所选Boss范围内的理论轴表现。",
            "gain": "保持现有节奏",
            "sort_value": 0.0,
        })
    return rows


def _score_improvement_summary(score: PlayerScore) -> dict[str, float]:
    has_burst_windows = bool(score.burst_windows)
    return {
        "output_score": score.output_score,
        "recoverable_output": (
            (
                max(0.0, 100.0 - score.theory_damage_completion) * 0.70
                + max(0.0, 100.0 - score.burst_usage) * 0.30
            )
            if has_burst_windows
            else max(0.0, 100.0 - score.theory_damage_completion)
        ),
        "support_score": score.support_score,
        "support_gap": max(0.0, 100.0 - score.support_score),
    }


def _json_safe(value: Any) -> Any:
    # Replay payloads contain millions of scalar leaves. Checking those before
    # the comparatively expensive dataclass/container probes keeps the exact
    # projection while avoiding several Python calls per number/string.
    if value is None or type(value) in (str, int, float, bool):
        return value
    if dataclasses.is_dataclass(value):
        return {field.name: _json_safe(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


@lru_cache(maxsize=1)
def _monster_template_catalog() -> dict[str, dict[str, Any]]:
    path = DATA_DIR / "profiles" / "monster_templates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    templates = payload.get("templates", {}) if isinstance(payload, dict) else {}
    return templates if isinstance(templates, dict) else {}


def _boss_base_stats(
    target_ids: tuple[str, ...] | list[str],
    encounter_name: str,
) -> dict[str, Any] | None:
    candidates: list[tuple[str, dict[str, Any]]] = []
    templates = _monster_template_catalog()
    for target_id in target_ids:
        match = re.search(r"@(\d+)$", str(target_id))
        if not match:
            continue
        template_id = match.group(1)
        row = templates.get(template_id)
        if isinstance(row, dict):
            candidates.append((template_id, row))
    if not candidates:
        return None

    selected_id, selected = next(
        (
            (template_id, row)
            for template_id, row in candidates
            if norm(row.get("name")) == norm(encounter_name)
        ),
        candidates[0],
    )
    return {
        "template_id": selected_id,
        "level": int(as_number(selected.get("level"))),
        "physical_defense": as_number(selected.get("physical_defense")),
        "magic_defense": as_number(selected.get("magic_defense")),
        "critical_resistance": as_number(selected.get("critical_resistance")),
        "fire_resistance": as_number(selected.get("fire_resistance")),
        "ice_resistance": as_number(selected.get("ice_resistance")),
        "light_resistance": as_number(selected.get("light_resistance")),
        "dark_resistance": as_number(selected.get("dark_resistance")),
        "source": norm(selected.get("source")) or "monster_templates_v576",
    }


def _boss_encounter_payload(
    snapshot: CombatSnapshot,
    encounters: tuple[BossEncounter, ...] | None = None,
    capture_hint: str = "",
) -> list[dict[str, Any]]:
    """Project the exact V4.01 Boss recognition result for the V4.10 UI."""

    resolved_encounters = tuple(
        encounters or BossRecognizer().recognize_snapshot(snapshot)
    )
    profile_label, fixed_references = resolve_fixed_boss_hp_profile(
        (encounter.name for encounter in resolved_encounters),
        capture_hint=capture_hint,
        target_ids=(
            target_id
            for encounter in resolved_encounters
            for target_id in encounter.target_ids
        ),
    )
    rows: list[dict[str, Any]] = []
    from .live.nest_hp_reference import select_reference, known_template, damage_budget
    hp_reference = select_reference(snapshot,
        [t for e in resolved_encounters for t in e.target_ids], capture_hint)
    for encounter in resolved_encounters:
        row = _json_safe(encounter)
        identity_kind, identity_value = encounter.identity
        row["key"] = (
            f"{identity_kind}:{identity_value}:{encounter.first_ms}"
        )
        fixed_values = fixed_references.get(
            normalize_boss_hp_name(encounter.name), ()
        )
        fixed_denominators = tuple(
            max(0, int(value)) for value in fixed_values
        )
        # A multi-phase profile stores one full-bar denominator per phase.
        # Each phase only occupies 1/N of the combined UI bar, so the actual
        # encounter total is sum(denominators) / N.  For the two Sand Dragon
        # phases this restores the two拆包 HP rows instead of doubling them.
        fixed_max_hp = (
            sum(fixed_denominators) // len(fixed_denominators)
            if fixed_denominators
            else 0
        )
        observed_damage = max(0, int(encounter.damage))
        if fixed_max_hp > 0:
            # Keep the拆包 denominator independent from this capture's
            # observed packets.  A replay can miss a small amount of UDP
            # damage without changing the Boss' actual maximum HP.
            row["max_hp"] = fixed_max_hp
            row["max_hp_source"] = "fixed_reference"
            row["boss_hp_profile"] = profile_label
            row["observed_damage"] = observed_damage
            row["observed_damage_ratio"] = observed_damage / fixed_max_hp
            row["unobserved_damage"] = max(
                0, fixed_max_hp - observed_damage
            )
            row["overkill_damage"] = max(
                0, observed_damage - fixed_max_hp
            )
        from .live.nightmare_sea_dragon import matches as sea_matches, PHASES, HP_NOTE
        if sea_matches(encounter.target_ids):
            row["hp_phases"] = [dict(phase) for phase in PHASES]
            row["max_hp_note"] = HP_NOTE
            row["max_hp_source"] = "phase_damage_budget_reference"
            row["damage_budget_source"] = "Reborn_20261017_client_hp_user_confirmed_phase_ratios"
            row["damage_budget_confirmed"] = False
        base_stats = _boss_base_stats(encounter.target_ids, encounter.name)
        if base_stats:
            row["base_stats"] = base_stats
        from .live.boss_entity_details import entity_details
        row["entities"] = entity_details(snapshot, encounter, _monster_template_catalog(), hp_reference)
        row["required_damage"] = row.get("max_hp", 0) if fixed_max_hp > 0 else None
        if any(known_template(t) for t in encounter.target_ids):
            budget = damage_budget(hp_reference, encounter.target_ids)
            row['required_damage'] = budget
            row['boss_hp_profile'] = hp_reference['label'] if hp_reference else ''
            row['max_hp_source'] = 'nest_template_reference' if hp_reference else 'unresolved_nest'
            row['damage_budget_confirmed'] = bool(budget is not None and hp_reference and hp_reference['capture_checked'])
            row['observed_damage'] = observed_damage
            row['max_hp'] = budget or 0
            for field in ('observed_damage_ratio', 'unobserved_damage', 'overkill_damage'):
                row.pop(field, None)
            if budget:
                row.update(observed_damage_ratio=observed_damage/budget,
                           unobserved_damage=max(0,budget-observed_damage),
                           overkill_damage=max(0,observed_damage-budget))
            else:
                row['max_hp_note'] = '实体满血与阶段需扣血量不同；当前阶段预算尚未核实，不套用同名经典血量。'
        rows.append(row)
    return rows


def _merge_hp_windows(
    windows: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    merged: list[list[float]] = []
    for start, end in sorted(windows):
        start = max(0.0, float(start))
        end = max(start, float(end))
        if not merged or start > merged[-1][1] + 1e-9:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _boss_hp_curve_payload(
    snapshot: CombatSnapshot,
    capture_hint: str = "",
    encounters: tuple[BossEncounter, ...] | None = None,
) -> list[dict[str, Any]]:
    """Project packet-observed Boss HP onto the absolute replay clock."""
    platform = event_platform_for(snapshot)
    groups = tuple(encounters or BossRecognizer().recognize_snapshot(snapshot))
    if not groups:
        return []
    profile_label, _ = resolve_fixed_boss_hp_profile(
        (group.name for group in groups),
        capture_hint=capture_hint,
        target_ids=(target_id for group in groups for target_id in group.target_ids),
    )
    tracks_by_target: dict[str, list[Any]] = {}
    from .live.nest_hp_reference import select_reference, entity_max, known_template
    hp_reference = select_reference(snapshot, [t for g in groups for t in g.target_ids], capture_hint)
    if hp_reference:
        profile_label = hp_reference['label']
    def denominator(track):
        return entity_max(hp_reference, track.target_id) if known_template(track.target_id) else track.max_hp
    for track in platform.boss_hp_tracks:
        tracks_by_target.setdefault(track.target_id, []).append(track)
    for tracks in tracks_by_target.values():
        tracks.sort(key=lambda row: (row.first_seconds, row.instance_id))

    result: list[dict[str, Any]] = []
    for group in groups:
        phase_windows = _merge_hp_windows(tuple(group.phase_windows))
        if not phase_windows:
            continue
        group_tracks = [
            track for target_id in group.target_ids
            for track in tracks_by_target.get(target_id, ())
        ]
        authoritative = any(
            point.current_hp is not None and denominator(track) > 0
            for track in group_tracks if track.confidence == "packet_hp" for point in track.points
        )
        if not authoritative:
            # Missing wire HP is unknown, never a damage-normalized/full-health curve.
            continue
        segments: list[list[list[float]]] = []
        confidence = ""
        max_hp = 0
        if authoritative:
            for target_id in group.target_ids:
                target_tracks = tracks_by_target.get(target_id, ())
                for track_index, track in enumerate(target_tracks):
                    if track.confidence != "packet_hp":
                        continue
                    points = sorted(
                        (float(point.timestamp_seconds), max(0.0, min(100.0, 100.0*point.current_hp/denominator(track))))
                        for point in track.points if point.current_hp is not None and denominator(track) > 0
                        and point.current_hp <= denominator(track)*1.00001
                    )
                    if not points:
                        continue
                    next_start = (
                        target_tracks[track_index + 1].first_seconds
                        if track_index + 1 < len(target_tracks) else float("inf")
                    )
                    for phase_start, phase_end in phase_windows:
                        start = max(phase_start, track.first_seconds)
                        end = min(phase_end, next_start)
                        visible = [[second, percent] for second, percent in points if start <= second <= end]
                        if not visible:
                            prior = [point for point in points if point[0] <= start]
                            if prior and end >= start:
                                visible = [[start, prior[-1][1]], [end, prior[-1][1]]]
                        elif visible[-1][0] < end:
                            visible.append([end, visible[-1][1]])
                        if visible:
                            segments.append(visible)
                    max_hp = max(max_hp, int(denominator(track) or 0))
            confidence = "server"
        if segments:
            result.append({
                "name": group.name,
                "target_ids": sorted(group.target_ids),
                "confidence": confidence,
                "current_hp_source": "packet",
                "max_hp_source": "reference",
                "profile": profile_label,
                "max_hp": max_hp,
                "segments": segments,
            })
    return result


def _boss_effect_views_payload(
    snapshot: CombatSnapshot,
    aggregator: CombatAggregator,
    encounters: tuple[BossEncounter, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build the same target-scoped status curves used by the V4.01 page."""

    # V4.01 projects the status curve to the later of combat time and the
    # status ledger time.  Using only the latter truncated a still-active
    # freeze layer when no later status packet followed it.
    platform = event_platform_for(snapshot)
    current_seconds = max(
        0.0,
        float(platform.elapsed_seconds),
        float(platform.boss_effect_current_seconds),
    )
    current_ms = round(current_seconds * 1000)
    views: list[dict[str, Any]] = []
    entity_encounters = []
    first_seen = {row.target_id: row.first_ms for row in platform.damage_targets}
    seen_entities = set()
    for encounter in encounters or BossRecognizer().recognize_snapshot(snapshot):
        for actor in sorted(encounter.target_ids, key=lambda actor: (first_seen.get(actor, encounter.first_ms), actor)):
            if actor in seen_entities:
                continue
            seen_entities.add(actor)
            entity_encounters.append(replace(encounter, target_ids=frozenset({actor}), first_ms=first_seen.get(actor, encounter.first_ms)))
    entity_encounters.sort(key=lambda row: (row.first_ms, next(iter(row.target_ids))))
    name_counts = {}
    name_indices = {}
    for encounter in entity_encounters:
        name_counts[encounter.name] = name_counts.get(encounter.name, 0) + 1
    for encounter in entity_encounters:
        key = f"actor:{next(iter(encounter.target_ids))}:{encounter.first_ms}"
        name_indices[encounter.name] = name_indices.get(encounter.name, 0) + 1
        index = name_indices[encounter.name]
        suffix = chr(0x2460 + index - 1) if index <= 20 else f"（{index}）"
        display_name = encounter.name + (suffix if name_counts[encounter.name] > 1 else "")
        view = aggregator.boss_effect_view(
            frozenset(encounter.target_ids), current_ms
        )
        target_ids = frozenset(encounter.target_ids)
        identities = frozenset(
            aggregator._boss_target_group_key(row)
            for row in platform.damage_targets
            if row.target_id in target_ids
        )
        if platform.combat_events:
            # V4.01 uses the scoring engine's Boss output windows for status
            # coverage. Reuse that exact denominator when replay events are
            # available; live snapshots omit raw events to stay lightweight.
            output_windows = list(
                TeamScoreEngine._output_windows(snapshot, target_ids)
            )
        else:
            # The lightweight snapshot omits raw events from JSON, but the
            # live aggregator still owns them.  Prefer its score-accurate
            # resolver so resuming after a mechanic does not bridge the whole
            # idle gap into the real-time score denominator.  The fallback is
            # retained for small test doubles and old restored caches.
            output_window_resolver = getattr(
                aggregator,
                "_boss_output_windows",
                aggregator._boss_phase_windows,
            )
            output_windows_by_identity = output_window_resolver(
                identities,
                current_ms,
                target_ids=target_ids,
            )
            output_windows = sorted(
                {
                    (max(0.0, start / 1000.0), max(0.0, end / 1000.0))
                    for windows in output_windows_by_identity.values()
                    for start, end in windows
                    if end > start
                }
            )
        output_windows = _merge_display_intervals(
            [[float(start), float(end)] for start, end in output_windows]
        )
        freeze_points = dict(view.series).get("冰冻层", ())
        ordered_freeze = sorted(
            {
                max(0.0, float(second)): float(value)
                for second, value in freeze_points
                if float(second) <= current_ms / 1000.0
            }.items()
        )
        output_seconds = sum(end - start for start, end in output_windows)
        freeze_layers: list[dict[str, Any]] = []
        for layer in (1, 2):
            raw_intervals: list[list[float]] = []
            for index, (start, value) in enumerate(ordered_freeze):
                end = (
                    ordered_freeze[index + 1][0]
                    if index + 1 < len(ordered_freeze)
                    else max(start, current_ms / 1000.0)
                )
                if value >= float(layer) and end > start:
                    raw_intervals.append([start, end])
            intervals = _merge_display_intervals(raw_intervals)
            covered_seconds = sum(
                max(
                    0.0,
                    min(freeze_end, window_end)
                    - max(freeze_start, window_start),
                )
                for freeze_start, freeze_end in intervals
                for window_start, window_end in output_windows
            )
            freeze_layers.append(
                {
                    "layer": layer,
                    "label": f"{layer}层冰覆盖率",
                    "parameters": (
                        "至少1层（含2层）" if layer == 1 else "达到2层"
                    ),
                    "intervals": intervals,
                    "refresh_count": len(intervals),
                    "covered_seconds": covered_seconds,
                    "coverage": (
                        covered_seconds / output_seconds
                        if output_seconds > 1e-9
                        else 0.0
                    ),
                }
            )
        one_layer = freeze_layers[0]
        views.append(
            {
                "key": key,
                "name": display_name,
                "target_ids": list(encounter.target_ids),
                "series": _json_safe(view.series),
                # Keep the former fields as one-layer aliases for cached V4.10
                # consumers while exposing V4.01's complete two-row summary.
                "freeze_coverage": one_layer["coverage"],
                "freeze_covered_seconds": one_layer["covered_seconds"],
                "freeze_layers": freeze_layers,
                "output_windows": output_windows,
                "output_seconds": output_seconds,
            }
        )
    return views


@lru_cache(maxsize=4096)
def _display_skill_key_text(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"EX(?:Lv\d+)?$", "", text, flags=re.IGNORECASE)
    return norm(text)


def _display_skill_key(value: Any) -> str:
    return _display_skill_key_text(str(value or ""))


def _merge_display_intervals(values: list[list[float]]) -> list[list[float]]:
    ordered = sorted(
        (max(0.0, float(start)), max(0.0, float(end)))
        for start, end in values
        if float(end) > float(start)
    )
    merged: list[list[float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1e-6:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _windows_process_id(names: tuple[str, ...]) -> tuple[int, str]:
    if os.name != "nt":
        return 0, ""

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return 0, ""
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            wanted = {name.casefold() for name in names}
            while True:
                if entry.szExeFile.casefold() in wanted:
                    return int(entry.th32ProcessID), entry.szExeFile
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return 0, ""


def _windows_transport_ports(pid: int, transport: str) -> list[int]:
    if os.name != "nt" or pid <= 0:
        return []
    is_tcp = transport.casefold() == "tcp"
    fields = [
        ("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD), ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
    ] if is_tcp else [
        ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]
    row_type = type("TransportOwnerPidRow", (ctypes.Structure,), {"_fields_": fields})
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = iphlpapi.GetExtendedTcpTable if is_tcp else iphlpapi.GetExtendedUdpTable
    get_table.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL,
        wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
    ]
    get_table.restype = wintypes.DWORD
    size = wintypes.ULONG(0)
    table_class = 5 if is_tcp else 1
    result = get_table(None, ctypes.byref(size), False, 2, table_class, 0)
    if result not in (0, 122) or size.value < 4:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, 2, table_class, 0) != 0:
        return []
    count = wintypes.DWORD.from_buffer_copy(buffer.raw[:4]).value
    row_size = ctypes.sizeof(row_type)
    ports: set[int] = set()
    for index in range(count):
        offset = 4 + index * row_size
        if offset + row_size > size.value:
            break
        row = row_type.from_buffer_copy(buffer.raw[offset:offset + row_size])
        if int(row.dwOwningPid) == pid:
            port = socket.ntohs(int(row.dwLocalPort) & 0xFFFF)
            if port:
                ports.add(port)
    return sorted(ports)


from .native.live_capture_service import bind_host_globals as _bind_native_live_capture_service
from .native.live_capture_service import (
    NativeLiveCapture,
    NativeJobCancelled,
    NativeJobQueue,
)







from .native.custom_team_service import NativeCustomTeamMixin, bind_host_globals as _bind_NativeCustomTeamMixin
from .native.overlay_service import NativeOverlayMixin, bind_host_globals as _bind_NativeOverlayMixin
from .native.profile_sync_service import NativeProfileSyncMixin, bind_host_globals as _bind_NativeProfileSyncMixin
from .native.build_service import NativeBuildMixin, bind_host_globals as _bind_NativeBuildMixin
from .native.team_service import NativeTeamMixin, bind_host_globals as _bind_NativeTeamMixin

class NativeSimulatorCore(NativeCustomTeamMixin, NativeOverlayMixin, NativeProfileSyncMixin, NativeBuildMixin, NativeTeamMixin):
    """Window-free application service for the V4.10 Vue frontend."""

    def __init__(self, store: DataStore, calculator: BuildCalculator) -> None:
        self.store = store
        self.calculator = calculator
        self.timeline_planner = NativeTimelinePlanner(store)
        self.legacy_timeline = LegacyTimelineService(store, calculator)
        self.score_theory_planner = TeamScoreTheoryPlanner(
            store, calculator, self.legacy_timeline
        )
        self.jobs = NativeJobQueue()
        self._state_lock = threading.RLock()
        self._overlay_response_cache_lock = threading.RLock()
        self._overlay_response_cache_key: tuple[int, str] | None = None
        self._overlay_response_cache: dict[str, Any] | None = None
        self._overlay_json_cache_key: tuple[int, int, str] | None = None
        self._overlay_json_cache = b""
        self._replays: dict[str, CombatSnapshot] = {}
        self._replay_sources: dict[str, Path] = {}
        self._replay_map_history: dict[
            str, tuple[dict[str, object], ...]
        ] = {}
        self._replay_movement_captures: dict[str, object] = {}
        self._score_encounters_by_replay: dict[
            str, tuple[BossEncounter, ...]
        ] = {}
        self._score_results: dict[str, tuple[PlayerScore, ...]] = {}
        self._score_keys_by_replay: dict[str, str] = {}
        self._score_benchmark_nests: dict[str, str] = {}
        self._replay_disk_cache = ReplayDiskCache()
        self._auto_glyph_cache: dict[str, list[dict[str, Any]]] = {}
        self._personal_timeline_cache: dict[str, dict[str, Any]] = {}
        self._custom_timeline_cache: dict[str, dict[str, Any]] = {}
        self._profile_sync: dict[str, Any] = {}
        self._live_buff_catalog: dict[str, dict[str, Any]] = {}
        for row in self.store.tables.get("buff", []):
            skill = norm(row.get("技能"))
            category = norm(row.get("类别"))
            if skill and category.casefold() != "debuff":
                scope = "team" if category == "团队buff" else "personal"
                catalog = self._live_buff_catalog.setdefault(
                    _display_skill_key(skill),
                    {
                        "skill": skill,
                        "display": "",
                        "category": "团队 Buff" if scope == "team" else category,
                        "scope": scope,
                        "provider": norm(row.get("来源")),
                    },
                )
                # Reviewed personal/team Buffs are selectable before a packet
                # is seen. Passive panel rows still belong to the calculator,
                # but a short internal status pulse (for example Hunter
                # Instinct's cooldown refund) must not become a visible Buff.
                catalog["scope"] = (
                    "team" if scope == "team" or catalog.get("scope") == "team"
                    else "personal"
                )
                catalog["overlay"] = "0" if category == "被动" else "1"
        catalog_path = (
            DATA_DIR / "profiles" / "player_buff_display_catalog.json"
        )
        try:
            catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog_payload = {}
        for row in catalog_payload.get("buffs", []):
            if not isinstance(row, dict):
                continue
            skill = norm(row.get("skill"))
            category = norm(row.get("category"))
            if not skill or category.casefold() == "debuff":
                continue
            skill_key = _display_skill_key(skill)
            existing = self._live_buff_catalog.get(skill_key, {})
            raw_overlay = row.get("overlay", existing.get("overlay", "1"))
            overlay_enabled = str(raw_overlay).strip().casefold() not in {
                "0", "false", "no", "off"
            }
            self._live_buff_catalog[skill_key] = {
                "skill": skill,
                "display": norm(row.get("display")),
                "category": norm(row.get("category")),
                "scope": norm(row.get("scope")) or existing.get("scope", "personal"),
                "provider": norm(row.get("provider")) or existing.get("provider", ""),
                # Owning a shared base skill does not imply owning its EX effect.
                "effect_providers": [
                    norm(job) for job in row.get("effect_providers", [])
                    if norm(job)
                ],
                "overlay": "1" if overlay_enabled else "0",
                "cast_duration_from_activation_rule": bool(
                    row.get("cast_duration_from_activation_rule", False)
                ),
                "cast_duration_from_skill_table": bool(
                    row.get("cast_duration_from_skill_table", False)
                ),
                "skill_table_fallback_level": int(
                    as_number(row.get("skill_table_fallback_level"))
                ),
                "skill_table_fallback_max_required_level": int(
                    as_number(row.get("skill_table_fallback_max_required_level"), 70)
                ),
            }
        for row in self._live_buff_catalog.values():
            if norm(row.get("skill")) in _OVERLAY_HIDDEN_BUFF_SKILL_NAMES:
                row["overlay"] = "0"
        overlay_jobs = [
            second_job
            for first_job in self.store.first_jobs()
            for second_job in self.store.second_jobs(first_job)
            if norm(second_job) and norm(second_job) != NONE_TEXT
        ]
        # The compact icon layout uses the same client skill sheets as the
        # skill-tree page. Build one small name/id lookup while the job files
        # are already being visited for provider discovery.
        self._live_skill_icon_indices: dict[str, int] = {}
        for job in overlay_jobs:
            for skill in self.store._full_skill_items(job):
                icon_index = int(as_number(skill.get("_IconImageIndex"), -1))
                if icon_index < 0:
                    continue
                skill_name = norm(skill.get("name"))
                if skill_name:
                    self._live_skill_icon_indices.setdefault(
                        _display_skill_key(skill_name), icon_index
                    )
                for id_key in ("_SkillTableID", "_SkillID", "RowId"):
                    skill_id = int(as_number(skill.get(id_key), 0))
                    if skill_id > 0:
                        self._live_skill_icon_indices.setdefault(
                            str(skill_id), icon_index
                        )
        skill_jobs: dict[str, list[str]] = {}
        for job in overlay_jobs:
            for skill_name in self.store.full_skill_names(job):
                skill_jobs.setdefault(_display_skill_key(skill_name), []).append(job)
        job_order = {job: index for index, job in enumerate(overlay_jobs)}
        for display_order, (skill_key, row) in enumerate(
            self._live_buff_catalog.items()
        ):
            providers = list(dict.fromkeys(
                row.get("effect_providers") or skill_jobs.get(skill_key, [])
            ))
            existing_provider = norm(row.get("provider"))
            if providers:
                row["provider"] = " / ".join(providers)
                row["provider_order"] = min(
                    (job_order.get(job, len(job_order)) for job in providers),
                    default=len(job_order),
                )
            else:
                row["provider"] = existing_provider or "职业待确认"
                row["provider_order"] = job_order.get(
                    existing_provider, len(job_order)
                )
            cooldown_providers = providers or (
                [existing_provider] if existing_provider else []
            )
            row["cooldown_trackable"] = any(
                self.store.skill_reference_cooldown(provider, row.get("skill"), 0.0)
                > 0.0
                for provider in cooldown_providers
            )
            row["full_duration_team"] = (
                norm(row.get("scope")) == "team"
                and _overlay_full_duration_team_buff(
                    self.store,
                    row.get("skill"),
                    cooldown_providers,
                )
            )
            row["display_order"] = display_order
            row["icon_index"] = self._live_skill_icon_indices.get(
                skill_key, -1
            )
        self._build_state_path = V410_USER_CONFIG_DIR / f"v{APP_VERSION_COMPACT}_builds.json"
        self._skill_schemes_path = V410_USER_CONFIG_DIR / f"v{APP_VERSION_COMPACT}_skill_build_schemes.json"
        if APP_VERSION_COMPACT == "420":
            for previous, current in (
                (V410_USER_CONFIG_DIR / "v410_builds.json", self._build_state_path),
                (V410_USER_CONFIG_DIR / "v410_skill_build_schemes.json", self._skill_schemes_path),
            ):
                if not current.exists() and previous.is_file():
                    try:
                        current.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(previous, current)
                    except OSError:
                        pass
        if APP_VERSION_COMPACT in {
            "424", "425", "430", "440", "441", "442", "443", "450", "460", "461", "476"
        }:
            previous_version = {
                "424": "423",
                "425": "424",
                "430": "425",
                "440": "430",
                "441": "440",
                "442": "441",
                "443": "442",
                "450": "4434",
                "460": "4523",
                "461": "460",
                "476": "475",
            }[APP_VERSION_COMPACT]
            for previous, current in (
                (
                    V410_USER_CONFIG_DIR / f"v{previous_version}_builds.json",
                    self._build_state_path,
                ),
                (
                    V410_USER_CONFIG_DIR
                    / f"v{previous_version}_skill_build_schemes.json",
                    self._skill_schemes_path,
                ),
            ):
                if not current.exists() and previous.is_file():
                    try:
                        current.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(previous, current)
                    except OSError:
                        pass
        default_builds = {
            "build1": self._default_build("build1"),
            "build2": self._default_build("build2"),
        }
        self._builds = self._load_builds(default_builds)
        self.profile_panel_calculator = ProfilePanelCalculator(
            self.store, self.calculator
        )
        live_capture_settings_path = (
            V410_USER_CONFIG_DIR / f"v{APP_VERSION_COMPACT}_live_capture.json"
        )
        if (
            APP_VERSION_COMPACT in {
                "424", "425", "430", "440", "441", "442", "443", "450", "460", "461", "476"
            }
            and not live_capture_settings_path.exists()
        ):
            previous_version = {
                "424": "423",
                "425": "424",
                "430": "425",
                "440": "430",
                "441": "440",
                "442": "441",
                "443": "442",
                "450": "4434",
                "460": "4523",
                "461": "460",
                "476": "475",
            }[APP_VERSION_COMPACT]
            previous_live_capture_settings = (
                V410_USER_CONFIG_DIR
                / f"v{previous_version}_live_capture.json"
            )
            if previous_live_capture_settings.is_file():
                try:
                    live_capture_settings_path.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    shutil.copy2(
                        previous_live_capture_settings,
                        live_capture_settings_path,
                    )
                except OSError:
                    pass
        self.live_capture = NativeLiveCapture(
            DATA_DIR / "profiles" / "client_profile.json",
            self._with_profile_panels,
            # The lightweight build keeps only the countdown overlays: no
            # post-capture scoring, cloud upload or replay cache is started.
            score_ready_callback=None,
            replay_cache_ready_callback=None,
            auto_settings_path=live_capture_settings_path,
            # 待机零抓包：不再启动自动统计探针和身份监听探针。
            # 只有点「开始统计」那一刻才创建 Npcap 流；进本后才开统计时，
            # 副本与 Boss 靠「本机正在攻击的目标」反推（见 nest_inference）。
            auto_default_enabled=False,
            auto_start_monitor=False,
            timeline_store=self.store,
            identity_cache_path=(
                Path.home()
                / "Documents"
                / "DragonNestDpsCapture"
                / "player_identity_cache.sqlite3"
            ),
        )

    def shutdown(self) -> None:
        """Release capture, background jobs and multiprocessing workers."""

        try:
            self.live_capture.close()
        finally:
            self._shutdown_teammate_projection_worker()
            self.jobs.shutdown()
            reset_timeline_process_pool()

    def mechanism_templates(self) -> dict[str, list[dict[str, Any]]]:
        return load_mechanism_templates()








































































































































_bind_native_live_capture_service(globals())

# V430_SPLIT_MODULES_APPLIED
_bind_NativeCustomTeamMixin(globals())
_bind_NativeOverlayMixin(globals())
_bind_NativeProfileSyncMixin(globals())
_bind_NativeBuildMixin(globals())
_bind_NativeTeamMixin(globals())
