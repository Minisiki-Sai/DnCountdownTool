from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from functools import lru_cache
from itertools import combinations, product
from math import ceil, gcd, hypot, trunc
from pathlib import Path
from typing import Any, Iterable
from unicodedata import east_asian_width

from .effect_constants import TEAM_FREEZE_SKILL_GROUPS
from .acceleration_resistance import (
    BURST_RESISTANCE_GROUP,
    TIME_ACCEL_RESISTANCE_GROUP,
    acceleration_resistance_group,
    acceleration_resistance_label,
    is_acceleration_resistance_skill,
    resistance_windows_from_schedule,
    schedule_acceleration_resistance,
)
from .live.buff_clear import (
    clip_player_buff_intervals_at_clears,
    player_buff_clear_affects_timeline_skill,
    player_buff_is_clear_immune,
    player_buff_survives_at,
)

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QButtonGroup,
    QRadioButton,
    QScrollArea,
    QSplashScreen,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:  # pragma: no cover - PySide6 always supplies shiboken6.
    _shiboken_is_valid = None


def qt_object_is_valid(value: Any) -> bool:
    if not isinstance(value, QObject):
        return False
    if _shiboken_is_valid is None:
        return True
    try:
        return bool(_shiboken_is_valid(value))
    except RuntimeError:
        return False

from .calculator import BuildCalculator, DISPLAY_TO_TARGET, ResultRow, SOURCE_ATTRS
from .effect_stacking import (
    DEFENSE_REDUCTION_CAP_PERCENT,
    EFFECT_STACKING_ENGINE,
    EffectInput,
    amplification_channels,
    amplification_for_channel,
    attack_increase_channels,
    attack_increase_for_channel,
    canonical_defense_reduction_key,
    canonical_effect_key,
    defense_reduction_channels,
    defense_reduction_for_attack,
    effect_lane_marker,
    effect_rule,
    fixed_attack_for_channel,
    skill_percent_amplified_flat_attack,
)
from .cooldown import SkillCooldownEngine
from .timeline_engine import (
    PureTimelineRuntime,
    TimelineCalculationResult,
    TimelineSimulationEngine,
    TimelineSimulationSnapshot,
)
from .data_loader import (
    BUFF_EXCLUSIVE_SKILL_GROUPS,
    TALISMAN_SLOTS,
    TALISMAN_SLOT_PERCENT,
    DataStore,
    MODULAR_DIR,
    as_number,
    buff_aliases,
    buff_display_label,
    buff_exclusive_group,
    buff_label,
    norm,
    unique,
)
from .job_mechanics import (
    JOB_MECHANICS,
    is_ring_strike,
    ring_strike_reference_start_seconds,
    ring_strike_default_hit_count,
    ring_strike_output_multiplier,
    ring_strike_total_multiplier,
    adjust_fixed_duration_action_speed_cast_time,
    fixed_duration_action_speed_output_skills,
    stance_damage_fraction_for_utilization,
)
from .team_support_rules import (
    TEAM_EFFECT_LEVEL_CAPS,
    resolved_team_effect_duration_for_effect,
    team_effect_application_window_for_attrs,
    team_effect_application_window_for_effect,
    team_effect_duration_from_application,
    team_effect_duration_override,
    team_effect_proc_chance,
    team_effect_refresh_tail,
    team_effect_refresh_tail_for_attrs,
)

try:
    with contextlib.redirect_stdout(io.StringIO()):
        from qfluentwidgets import (
            PrimaryPushButton,
            PushButton,
            TableWidget as FluentTableWidget,
            Theme,
            setTheme,
            setThemeColor,
        )
    FLUENT_AVAILABLE = True
except Exception:
    PrimaryPushButton = PushButton = None
    FluentTableWidget = None
    Theme = None
    setTheme = setThemeColor = None
    FLUENT_AVAILABLE = False


APP_VERSION = "4.10"
APP_NAME = "龙之谷ACT（高级战斗追踪器）工具及模拟工具"
APP_SHORT_NAME = "龙之谷ACT"
APP_PRODUCT_DESCRIPTION = "高级战斗追踪器 · 工具及模拟工具"
APP_TITLE = f"{APP_NAME} V{APP_VERSION}"
AUTHOR_TEXT = "帅气迷人机智幻制作"
APP_ICON_RELATIVE = Path("engine") / "assets" / "app_icon.png"
USER_CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "DragonNestBuildSimulator"
SKILL_BUILD_DEFAULTS_PATH = USER_CONFIG_DIR / "skill_build_defaults.json"
BUILTIN_SKILL_BUILD_DEFAULTS_PATH = MODULAR_DIR / "tables" / "skills" / "skill_build_defaults_builtin.json"
SKILL_BUILD_SCHEMES_KEY = "__schemes__"
SKILL_DAMAGE_CHANNEL_ORDER = ("光", "暗", "水", "火", "物理", "魔法")
SKILL_DAMAGE_CHANNEL_COLORS = {
    "光": "#b77900",
    "暗": "#7651b5",
    "水": "#1677b8",
    "火": "#d5533f",
    "物理": "#526170",
    "魔法": "#07867d",
}
GRADE_OPTIONS = ["无", "C", "B", "A", "S", "L"]
EQUIPMENT_SLOTS = ["头盔", "上装", "下装", "手套", "鞋子", "主手", "副手"]
JEWELRY_SLOTS = ["项链", "耳环", "戒指1", "戒指2"]
AUTO_EQUIP_TARGETS = {
    "50": [
        ("霸龙L装备", "equipment", "50L"),
        ("霸龙L首饰", "jewelry", "50L"),
    ],
    "60": [
        ("赞龙L装备", "equipment", "60L"),
        ("赞龙L首饰", "jewelry", "60L"),
        ("赞龙S首饰", "jewelry", "60S"),
    ],
    "70": [
        ("黑龙L装备+首饰", "all", "70L"),
        ("黑龙L装备", "equipment", "70L"),
        ("黑龙L首饰", "jewelry", "70L"),
        ("黑龙S装备", "equipment", "70S"),
        ("黑龙S首饰", "jewelry", "70S"),
    ],
}
FASHION_SLOTS = ["主手", "副手", "翅膀", "尾巴", "印花", "精灵", "项链", "耳环", "戒指1", "戒指2"]
ENGRAVING_SLOTS = ["头盔", "上装", "下装", "手套", "鞋子", "主手", "副手", "项链", "耳环", "戒指1", "戒指2", "翅膀", "尾巴", "印花"]
MASTERY_NAMES = ["最大生命值", "最大魔法值", "物理攻击力", "魔法攻击力", "物理防御", "魔法防御", "致命一击", "最终伤害"]
CARD_PREVIEW_ATTRS = ["HP", "MP", "体质", "力量", "敏捷", "智力", "最终", "物攻", "致命", "致命伤害", "防御", "魔攻", "魔防"]
DISPLAY_ATTRS = [
    "HP",
    "力量", "敏捷", "智力", "体质",
    "最小物攻", "最大物攻", "最小魔攻", "最大魔攻",
    "最终", "火攻%", "水攻%", "光攻%", "暗攻%", "无属性%",
    "防御", "魔防", "致命", "致命伤害", "防御率%", "魔防率%",
    "HP%", "移速%",
    "力量%", "敏捷%", "智力%", "体质%",
    "物攻%", "魔攻%", "防御%", "魔防%", "致命%", "致命伤害%",
    "最终%",
]
FREE_PERCENT_INPUTS = [("最终%", "最终"), ("致命%", "致命"), ("爆伤%", "致命伤害")]
FREE_STAT_ATTRS = [attr for attr in DISPLAY_ATTRS if not attr.endswith("%") and attr not in {"最终", "致命", "致命伤害", "防御率%", "魔防率%"}]
FREE_STAT_PERCENT_ATTRS = [attr for attr in DISPLAY_ATTRS if attr.endswith("%") and attr not in {"防御率%", "魔防率%"}]
EDIT_ROW_HEIGHT = 26
SKILL_BUILD_LEVEL_CAP = 70
SKILL_SIM_ROWS = 10
SKILL_SIM_COL_CAST_TIME = 9
SKILL_SIM_COL_DPA = 10
SKILL_SIM_COL_COUNT = 11
SKILL_SIM_ALLOWED_EX_SKILLS = {"神圣惩戒EX"}
SKILL_BURST_REFRESHABLE_PERIODIC_DAMAGE = {"神圣惩戒", "神圣惩戒EX"}
SKILL_REFRESHABLE_PERIODIC_DURATIONS = {"神圣惩戒": 20, "神圣惩戒EX": 20}
SKILL_FIXED_COOLDOWNS = {"雷电之手": 5.4, "诅咒状态": 2, "重磅鸭": 8, "阿兹那之约": 20, "神圣惩戒": 35, "神圣惩戒EX": 35}
SKILL_TIMELINE_FIXED_COOLDOWNS_BY_JOB = {
    ("雷神", "雷电之手"): 1.5,
    ("圣徒", "雷电之手"): 1.5,
}
SKILL_PERIODIC_TOTAL_DAMAGE_CYCLES = {
    "阿兹那之约": 20,
    "灵魂之门": 11,
    "灵魂之门 EX": 11,
    "瞒天过海": 13,
    "雷电之手": 16.2,
    "暗影召唤术": 15,
    "炮塔": 30,
    "寒冰喷射塔": 30,
    "加特林机枪塔": 30,
    "链锯之塔": 30,
    "毒池": 11,
}
SKILL_PERIODIC_DYNAMIC_ATTACK_BUFFS = {
    "阿兹那之约",
    "暗影召唤术",
    "灵魂之门",
    "灵魂之门 EX",
    "瞒天过海",
}
SKILL_PERIODIC_DOUBLE_SNAPSHOT_BUFFS = {
    "阿兹那之约",
}
SKILL_PERIODIC_REFRESHING_ATTACK_BUFFS = {
    "查克拉治愈术",
    "怨恨之像",
    "痊愈之手",
}
SKILL_TIMELINE_DAMAGE_MULTIPLIERS_BY_JOB = {
    # The DPS table uses 5x/5.3x for display. Real timeline totals are 9x/9.6x.
    ("雷神", "雷电之手"): 1.8,
    ("圣徒", "雷电之手"): 9.6 / 5.3,
}
SKILL_SUMMON_MIN_COOLDOWNS = {
    "暗影召唤术": 15,
    "炮塔": 30,
    "寒冰喷射塔": 30,
    "加特林机枪塔": 30,
    "链锯之塔": 30,
    "毒池": 11,
    "瞒天过海": 13,
    "灵魂之门": 11,
    "灵魂之门 EX": 11,
}
SKILL_BURST_SIM_PASSIVE_SUMMONS = {
    "阿兹那之约",
    "重磅汽笛",
    "重磅轰击",
    "摄魂怪光束",
}
SKILL_BURST_SIM_DURATIONS = {"精神爆发": 14.0, "时间加速": 15.0}
SKILL_BURST_PRACTICAL_WINDOW = 180.0
SKILL_LONG_PRACTICAL_WINDOW = 125.0
SKILL_CUSTOM_TIMELINE_WINDOW = 600.0
SKILL_BURST_HOLY_GROUND_FOLLOWUPS = {"圣光十字", "神圣暴风", "神圣风暴"}
SKILL_BURST_HOLY_GROUND_WINDOW_SECONDS = 10.0
SKILL_BURST_HOLY_GROUND_OUTSIDE_MULTIPLIER = 0.5
SKILL_BURST_HOLY_GROUND_REQUIREMENT_MARKER = "__10秒内释放神圣大地"
SKILL_SIM_AUTO_EXCLUSIVE_GROUPS = (
    {"飞溅", "脑电波投射", "生化导弹"},
)
SKILL_SIM_AUTO_SKIP_BY_JOB = {
    "影舞者": {"凌波微步"},
    "风行者": {"凌波微步"},
    "重炮手": {"寒冰喷射塔"},
    "机械大师": {"脑电波投射", "生化导弹", "寒冰喷射塔", "重磅轰击"},
    "曜": {"黑暗线条"},
}
SKILL_SIM_AUTO_FORCED_LAST_BY_JOB = {
    "噬魂者": "诅咒状态",
}
SKILL_SIM_AUTO_FORCE_INCLUDE_BY_JOB = {
    "冰灵": {"冰尖柱", "火径"},
    "魔羽": {"爆炸箭矢"},
    "影舞者": {"双飞腿"},
    "风行者": {"双飞腿"},
}
SKILL_SIM_AUTO_DUPLICATE_LIMITS_BY_JOB = {
    "重炮手": ({"炮塔", "加特林机枪塔"}, 2),
    "机械大师": ({"炮塔", "加特林机枪塔"}, 2),
}
SKILL_BUILD_LEVEL_ALIASES_BY_JOB = {
    "机械大师": {
        "机械链锯": "链锯之塔",
        "机械炮塔": "炮塔",
    },
    "圣骑士": {
        "神圣惩戒EX": "神圣惩戒",
    },
}
SKILL_BURST_AMMO_RULES_BY_JOB = {
    "重炮手": {
        "reload": "实弹装填",
        "gain": 2,
        "max": 6,
        "consumers": {
            "飞溅": 1,
            "生化导弹": 2,
            "脑电波投射": 2,
        },
    },
}
SKILL_BURST_FURY_MARKER = "__怒气"
SKILL_BURST_GUARDIAN_PHANTOM_MARKER = "__护卫者幻影"
SKILL_BURST_HEAVY_DUCK_COUNT_MARKER = "__重磅鸭数量"
SKILL_BURST_HOLY_RELIC_COUNT_MARKER = "__圣物数量"
SKILL_BURST_HEAVY_DUCK_TARGET_COUNT = 3
SKILL_BURST_DESTROYER_MASTERY_TRIGGERS = {"战吼", "反击怒吼", "毁灭呐喊"}
TEAM_EXCLUSIVE_PHYSICAL_AMP_SKILLS = {
    "捆绑射击",
    "捆绑射击EX",
    "爆裂踢",
    "爆裂踢EX",
}
TEAM_EXCLUSIVE_ATTACK_BUFF_SKILLS = {"无情打击", "特制强化药剂"}
SKILL_BURST_BRAND_MARKER = "__烙印"
SKILL_BURST_DANCE_STAGE_MARKER = "__九天之舞1阶"
SKILL_BURST_BERSERKER_MASTERY_MARKER = "__致命一击掌握"
SKILL_BURST_DESTROYER_MASTERY_MARKER = "__毁灭力量掌握"
SKILL_BURST_MECHANIC_MODE_MARKER = "__机械模式"
SKILL_BURST_MECHANIC_MOMENT_MARKER = "__机械时刻"
SKILL_BURST_ENGINEERING_FIELD_MARKER = "__工程领域"
SKILL_BURST_FURY_MAX = 8
SKILL_BURST_FURY_GENERATORS = {
    "践踏": 2,
    "屠戮盛宴": 2,
}
SKILL_BURST_FURY_CONSUMERS = {
    "旋转冲击": (6, 5 / 1.5),
    "连骨撞击": (2, 2.4),
}
SKILL_BURST_GUARDIAN_PHANTOM_GENERATORS = {
    "挑衅": 1,
    "虔诚": 1,
    "正义撞击": 1,
}
SKILL_BURST_GUARDIAN_PHANTOM_COSTS = {
    "护卫者姿势": 2,
}
SKILL_BURST_OPENING_SUPPORT_BY_JOB = {
    "狂战士": ("致命一击掌握触发",),
    "毁灭者": ("力量掌握触发",),
    # 挑衅在化身前预铺，避免高爆发窗口里浪费动作时间补仇恨。
    "圣骑士": ("挑衅", "神圣化身"),
    "十字军": ("神圣化身", "审判力量"),
    "炼金圣士": ("C2H5OH",),
    "噬魂者": ("科兹污点", "幽灵之魂", "怨恨之像"),
    "黑暗萨满": ("幽灵之魂", "怨恨之像", "科兹污点"),
    "刀锋舞者": ("弗里兹科之舞", "幽灵之魂", "九天之舞", "九天之舞2阶"),
    "灵魂舞者": ("幻影之舞", "幽灵之魂", "九天之舞", "九天之舞2阶"),
    "暗": ("黑暗化身", "黑暗线条", "黄昏之光"),
}
SKILL_BURST_ENGINEER_SUMMONS = {"炮塔", "加特林机枪塔", "寒冰喷射塔", "链锯之塔"}
SKILL_BURST_MECHANIC_MODE_ATTACKS = {"机械炮塔", "机械链锯"}
SKILL_BURST_BLADE_DANCE_SCOPE = {
    "草原之舞", "小甜甜圆周", "璀璨星光", "螺旋冲击", "傲慢狂风", "热情似火",
    "暴风雪制造机", "烈风痴狂",
}
SKILL_BURST_SPIRIT_DANCE_SCOPE = {
    "追踪者", "黄昏猎人", "废除者", "风车公主", "唐·弗莱德将军", "艾云尼风暴",
}
SKILL_BURST_EFFECT_COLORS = {
    "team_buff": "#4f8fd6",
    "team_amp": "#e4775a",
    "team_resist": "#e4a23c",
    "team_defense": "#b98255",
    "team_crit_resist": "#c7873f",
    "team_freeze": "#35a9c9",
    "team_personal": "#8b75d7",
    "team_continuous": "#2f9e73",
    "team_no_gain": "#94a3b8",
    "resist": "#f59e0b",
    "freeze": "#38bdf8",
    "amp": "#ef7f5a",
    "speed": "#3b82f6",
    "summon": "#06a6a6",
    "personal": "#10b981",
    "state": "#8b75d7",
}
SKILL_BURST_EFFECT_CATEGORY_ORDER = {
    "team_buff": 0,
    "team_amp": 1,
    "team_resist": 2,
    "team_defense": 3,
    "team_crit_resist": 4,
    "team_freeze": 5,
    "team_personal": 6,
    "team_continuous": 7,
    "team_no_gain": 8,
    "resist": 9,
    "freeze": 10,
    "amp": 11,
    "speed": 12,
    "summon": 13,
    "personal": 14,
    "state": 15,
}
SKILL_BURST_TEAM_PANEL_MULTIPLIER = "__队友面板倍率__"
SKILL_BURST_TEAM_PHYSICAL_PANEL_MULTIPLIER = "__队友物理面板倍率__"
SKILL_BURST_TEAM_MAGIC_PANEL_MULTIPLIER = "__队友魔法面板倍率__"
SKILL_BURST_TEAM_PHYSICAL_ATTACK_MULTIPLIER = "__队友物攻倍率__"
SKILL_BURST_TEAM_MAGIC_ATTACK_MULTIPLIER = "__队友魔攻倍率__"
SKILL_BURST_TEAM_DIRECT_PHYSICAL_ATTACK_MULTIPLIER = "__队友直接物攻倍率__"
SKILL_BURST_TEAM_DIRECT_MAGIC_ATTACK_MULTIPLIER = "__队友直接魔攻倍率__"
SKILL_PERIODIC_IGNORED_DYNAMIC_STAT_ATTRS = {
    "力量", "敏捷", "智力", "智慧", "体质",
    "力量%", "敏捷%", "智力%", "智慧%", "体质%",
    "力量独立乘区%", "敏捷独立乘区%", "智力独立乘区%", "体质独立乘区%",
    "力量智力独立乘区%", "力量转魔攻%", "智力转物攻%",
}
SKILL_BURST_COMBO_FOLLOWUPS_BY_JOB = {
    "黑暗复仇者": {
        "新月之刃": {"黑暗撞击"},
    },
    "剑皇": {
        "重锤": {"直线冲击"},
    },
    "月之领主": {
        "重锤": {"旋风斩", "半月斩"},
        "致命劈砍": {"半月斩"},
        "月光碎片": {"致命劈砍", "半月斩"},
        "半月斩": {"旋风斩", "月光碎片"},
        "旋风斩": {"致命劈砍"},
        "猛砍站姿": {"致命劈砍", "月光碎片"},
    },
    "时空领主": {
        "能量反射": {"光雨", "九尾激光"},
        "激光破碎": {"九尾激光"},
        "聚能激光": {"九尾激光"},
        "九尾激光": {"光雨"},
    },
    "黑暗女王": {
        "能量反射": {"光雨"},
        "上升重力": {"召唤黑洞"},
    },
    "炼金圣士": {
        "岩浆戳刺": {"寒冰戳刺"},
    },
    "药剂师": {
        "岩浆戳刺": {"寒冰戳刺"},
    },
    "狙翎": {
        "追踪箭": {"集中射击"},
    },
    "魔羽": {
        "追踪箭": {"集中射击"},
        "集中射击": {"追踪箭"},
    },
    "风行者": {
        "爆裂踢": {"鹰击长空"},
        "鹰击长空": {"双飞腿"},
    },
    "刀锋舞者": {
        "九天之舞": {"九天之舞2阶"},
    },
    "灵魂舞者": {
        "九天之舞": {"九天之舞2阶"},
    },
}
SKILL_BURST_COMBO_PRIORITY_BY_JOB = {
    "时空领主": {
        "能量反射": ("九尾激光", "光雨"),
        "激光破碎": ("九尾激光",),
        "聚能激光": ("九尾激光",),
        "九尾激光": ("光雨",),
    },
    "风行者": {
        "爆裂踢": ("鹰击长空",),
        "鹰击长空": ("双飞腿",),
    },
    "月之领主": {
        "重锤": ("旋风斩", "半月斩"),
        "旋风斩": ("致命劈砍",),
        "致命劈砍": ("半月斩",),
        "半月斩": ("月光碎片", "旋风斩"),
        "月光碎片": ("致命劈砍", "半月斩"),
        "猛砍站姿": ("月光碎片", "致命劈砍"),
    },
}
SKILL_BURST_OPTIONAL_COMBO_FOLLOWUPS_BY_JOB = {
    # 冰戳刺必须由火戳刺衔接，但只有叠冰带来的全轴收益足以覆盖施法占用时才使用。
    "炼金圣士": {"岩浆戳刺": {"寒冰戳刺"}},
    "药剂师": {"岩浆戳刺": {"寒冰戳刺"}},
}
SKILL_BURST_AUTO_EXCLUDES_BY_JOB = {
    # 九尾激光对黑暗女王的实战轴收益过低，仅保留在数据页和手动轴候选中。
    "黑暗女王": {"九尾激光"},
}
SKILL_BURST_REQUIRED_PREDECESSORS_BY_JOB = {
    "黑暗复仇者": {
        "黑暗撞击": {"新月之刃"},
    },
    "剑皇": {
        "直线冲击": {"重锤"},
    },
    "黑暗女王": {
        "召唤黑洞": {"上升重力"},
    },
    "影舞者": {
        "双飞腿": {"捆绑射击", "飓风舞"},
    },
    "风行者": {
        "鹰击长空": {"爆裂踢"},
        "双飞腿": {"捆绑射击", "鹰击长空"},
    },
    "炼金圣士": {
        "寒冰戳刺": {"岩浆戳刺"},
    },
    "药剂师": {
        "寒冰戳刺": {"岩浆戳刺"},
    },
    "刀锋舞者": {
        "九天之舞2阶": {"九天之舞"},
    },
    "灵魂舞者": {
        "九天之舞2阶": {"九天之舞"},
    },
}
SKILL_BURST_REQUIRED_FOLLOWUPS_BY_JOB = {
    "黑暗复仇者": {
        "新月之刃": {"黑暗撞击"},
    },
    "风行者": {
        "爆裂踢": {"鹰击长空"},
        "鹰击长空": {"双飞腿"},
    },
    "刀锋舞者": {
        "九天之舞": {"九天之舞2阶"},
    },
    "灵魂舞者": {
        "九天之舞": {"九天之舞2阶"},
    },
}
SKILL_BURST_QUICK_PREDECESSORS_BY_JOB = {
    "月之领主": {
        "致命劈砍": {"月光碎片", "旋风斩", "猛砍站姿"},
        "月光碎片": {"半月斩", "猛砍站姿"},
        "半月斩": {"重锤", "致命劈砍", "月光碎片"},
        "旋风斩": {"重锤", "半月斩"},
    },
}
SKILL_BURST_STRICT_QUICK_PREDECESSOR_JOBS = {"月之领主"}
SKILL_BURST_SUMMON_LIMITS = {
    "雷电之手": {"max": 3, "duration": 16.2},
    # These relics are limited naturally by their duration and cooldown. A high
    # technical cap preserves every simultaneously active instance under CDR.
    "治愈之手": {"max": 99, "duration": 8.0},
    "痊愈之手": {"max": 99, "duration": 18.0},
    "掌握之手": {"max": 99, "duration": 27.0},
    "奇迹之手": {"max": 99, "duration": 13.0},
    "灵魂之门": {"max": 1, "duration": 11.0},
    "暗影召唤术": {"max": 1, "duration": 15.0},
    "瞒天过海": {"max": 1, "duration": 13.0},
    "链锯之塔": {"max": 1, "duration": 30.0},
}
SKILL_BURST_SUMMON_LIMITS_BY_JOB = {
    "重炮手": {
        "重磅鸭": {"max": 3, "duration": 45.0},
    },
    "机械大师": {
        "重磅鸭": {"max": 3, "duration": 60.0},
    },
}
SKILL_DARK_SUFFER_PROJECTILE_LIFETIME = 4.0
SKILL_DARK_SUFFER_STARTUP_TIME = 0.4
SKILL_DARK_SUFFER_SHOT_INTERVAL = 27 / 60
SKILL_DARK_SUFFER_EXPLOSION_MULTIPLIER = 2.0
SKILL_DARK_SUFFER_BASE_MULTIPLIER = 16.0
SKILL_STANCE_ACTION_RULES_BY_JOB = {
    "狙翎": {
        "包围": {
            "duration": 4.0,
            "startup_time": 0.5,
            "base_count": 4,
            "per_hit_multiplier": 1.5,
            "fixed_multiplier": 0.0,
            "base_multiplier": 6.0,
            "hit_label": "箭矢",
            "fixed_label": "",
        },
    },
    "剑皇": {
        "挥砍姿态": {
            "duration": 6.0,
            "base_count": 13,
            "per_hit_multiplier": 1.4,
            "fixed_multiplier": 2.0,
            "base_multiplier": 20.2,
            "hit_label": "普通斩",
            "fixed_label": "右键200%",
        },
    },
    "月之领主": {
        "挥砍姿态": {
            "duration": 6.0,
            "base_count": 12,
            "per_hit_multiplier": 1.0,
            "fixed_multiplier": 0.0,
            "base_multiplier": 12.0,
            "hit_label": "斩击",
            "fixed_label": "",
        },
        "猛砍站姿": {
            "duration": 4.0,
            "base_count": 8,
            "per_hit_multiplier": 1.0,
            "fixed_multiplier": 4.8,
            "base_multiplier": 12.8,
            "hit_label": "普通剑气",
            "fixed_label": "巨型剑气480%",
        },
    },
}
SKILL_BURST_PASSIVE_COOLDOWN_SOURCES_BY_JOB = {
    "火舞": {
        # 灼烧之手基础CD为2秒；是否变为1.6秒由当前配装中的-20%CD
        # 纹章统一结算。触发依赖实际火系命中，由施法事件结算；不能
        # 在停手机制中按时钟自动触发。
        "灼烧之手": {
            "base_cooldown": 2.0,
            "interval_multiplier": 1.0,
            "cast_driven": True,
        },
    },
}
SKILL_SIM_SPECIAL_COOLDOWN_SOURCES_BY_JOB = {
    "影舞者": {
        "双飞腿": (("捆绑射击", 11), ("飓风舞", 30)),
    },
    "风行者": {
        "双飞腿": (("捆绑射击", 11), ("鹰击长空", 11)),
    },
}
TEAM_MEMBER_ROWS = 8
TEAM_EFFECT_ROWS = 64
TEAM_TANK_JOBS = {"毁灭者", "圣骑士"}
TEAM_HEALER_JOBS = {"圣徒", "药剂师", "曜"}
TEAM_YAO_VARIANTS = {"打坐曜", "输出曜"}
YAO_MEDITATION_LINGER_SECONDS = 3.0
TEAM_AURA_JOBS = {"圣骑士", "十字军"}
TEAM_AURA_OPTIONS = ["元素光环", "判定光环"]
TEAM_AURA_COMBO_WIDTH = 124
TEAM_EXTREME_SPECIAL_COEFFICIENTS = {
    "毁灭者": 0.5,
    "圣骑士": 0.5,
    "圣徒": 0.8,
    "药剂师": 0.8,
    "曜": 0.4,
    "狙翎": 1.2,
    "十字军": 0.75,
    "影舞者": 0.8,
    "风行者": 0.8,
    "冰灵": 1.0,
    "影": 1.4,
    "暗": 1.4,
    "烈": 1.4,
    "炼金圣士": 1.0,
}
TEAM_EXTREME_BEAM_WIDTH = 260
TEAM_CDR_USAGE_SKILLS = ("时间加速", "精神爆发", "鸡尾酒", "灼热火焰")
TEAM_CDR_APPLY_SKILLS = ("鸡尾酒", "精神爆发", "时间加速", "灼热火焰")
TEAM_CDR_GLYPH_REDUCTION = {"精神爆发": 12.0, "时间加速": 6.0}
DEFAULT_FREEZE_COEFFICIENT = 0.3
BOSS_PRESETS = {
    "梦魇·审判者天启": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},
    "梦魇·狮蝎领主克雷姆": {'crit_resist': 12000, 'element_resist': 20.0, 'monster_defense': 10000, 'freeze_coefficient': 0.5},

    "梦魇·海龙萨芬特拉": {"crit_resist": 12000, "element_resist": 20.0, "monster_defense": 11000, "freeze_coefficient": 0.3},
    "红龙佩达": {
        "crit_resist": 19000,
        "element_resist": 20.0,
        "monster_defense": 4950,
        "freeze_coefficient": 0.3,
    },
    "天堂龙灾·佩达": {
        "crit_resist": 18000,
        "element_resist": 10.0,
        "monster_defense": 9000,
        "freeze_coefficient": 0.3,
    },
}
TEAM_MANUAL_DEBUFFS = {
    "霜冻": ({"减水抗%": 30}, [30]),
    "漫长熔浆": ({"减火抗%": 30}, [30]),
    "天堂审判": ({"减光抗%": 30}, [30]),
    "神圣大地": ({"减光抗%": 10}, [10]),
    "冰雪连天": ({"减水抗%": 30}, [30]),
    "爱情病毒": ({"减暗抗%": 30}, [30]),
    "毒池": ({"减暗抗%": 30}, [30]),
    "C2H5OH": ({"减全抗%": 12}, [12]),
    "混沌阵": ({"减暗抗%": 30}, [30]),
    "上升重力": ({"减暗抗%": 15}, [15]),
    "灵魂之门 EX": ({"减暗抗%": 30}, [30]),
    "冰刃": ({"减水抗%": 15}, [15]),
    "冰雾": ({"减水抗%": 15}, [15]),
    "冰刺": ({"减水抗%": 15}, [15]),
    "弗罗斯特风": ({"减水抗%": 15}, [15]),
    "地狱火海EX": ({"减火抗%": 15}, [15]),
    "岩浆墙": ({"减火抗%": 15}, [15]),
    "毒气聚能": ({"减暗抗%": 15}, [15]),
    "疾病": ({"减暗抗%": 15}, [15]),
    "混合感染": ({"减暗抗%": 15}, [15]),
    "狂暴之爪": ({"减暗抗%": 15}, [15]),
    "复仇之手EX": ({"减暗抗%": 15}, [15]),
    "连锁闪电": ({"减光抗%": 15}, [15]),
    "雷电冲击": ({"减光抗%": 15}, [15]),
    "重击": ({"减光抗%": 15}, [15]),
    "电光箭": ({"减光抗%": 15}, [15]),
    "精神爆破EX": ({"减光抗%": 30}, [1]),
    "闪电风暴": ({"减光抗%": 15}, [15]),
    "雷电之手": ({"减光抗%": 15}, [15]),
    "天降雨点": ({"减暗抗%": 15}, [15]),
    "灵巧猎手": ({"减火抗%": 15}, [15]),
    "黑暗线条": ({"减暗抗%": 15}, [15]),
    "灵魂之刃": ({"减火抗%": 15}, [15]),
    "精神爆发": ({"冷却缩减%": 40, "动作速度%": 40}, [40]),
    "鸡尾酒": ({"冷却缩减%": 12}, [12]),
    "时间加速": ({"冷却缩减%": 70}, [70]),
    "重锤": ({"增伤%": 40}, [40]),
    "瞄准": ({"增伤%": 20}, [20]),
    "能量反射": ({"魔法增伤%": 30}, [30]),
    "破甲": ({"增伤%": 20}, [20]),
    "召唤傀儡": ({"增伤%": 20}, [20]),
    "冲击之手": ({"增伤%": 20}, [20]),
    "掌握之手EX": ({"增伤%": 10}, [10]),
    "捆绑射击EX": ({"物理增伤%": 30}, [30]),
    "爆裂踢EX": ({"物理增伤%": 30}, [30]),
    "冰刃EX": ({"增伤%": 20}, [20]),
    "黄昏之光": ({"增伤%": 25}, [25]),
    "重磅鸭": ({"增伤%": 12}, [12]),
    "飞行冲击EX": ({"增伤%": 20}, [20]),
    "疾病EX": ({"增伤%": 20}, [20]),
    "日光焰": ({"增伤%": 20}, [20]),
    "火焰冲刺": ({"增伤%": 20}, [20]),
    "突袭": ({"致命额外伤害%": 10}, [1]),
    "奉献乌鸦": ({"生命高于30额外系数%": 0.9}, [4]),
    "九天之舞2阶": ({"生命低于50额外系数%": 0.9}, [4]),
    "冰河": ({"冰冻层": 1}, [1]),
    "暴风雪": ({"冰冻层": 1}, [1]),
    "冰柱喷射": ({"冰冻层": 1}, [1]),
    "霜冻EX": ({"冰冻层": 1}, [1]),
    "冰光束": ({"冰冻层": 1}, [1]),
    "寒冰戳刺EX": ({"冰冻层": 1}, [1]),
    "切骨之刺EX": ({"减致命抵抗%": 20}, [20]),
    "旋风斩EX": ({"减致命抵抗%": 20}, [20]),
    "火墙EX": ({"减致命抵抗%": 30}, [30]),
    "重磅轰击": ({"减致命抵抗%": 20}, [20]),
    "灵魂尖叫": ({"减致命抵抗%": 20}, [20]),
    "致命狂镰": ({"减防御%": 10}, [10]),
    "惩罚": ({"减防御%": 10}, [10]),
    "致命狂镰EX": ({"减防御%": 20}, [20]),
    "惩罚EX": ({"减防御%": 20}, [20]),
    "螺旋飓风": ({"增伤%": 20}, [20]),
    "螺旋飓风EX": ({"增伤%": 20}, [20]),
    "飓风狂射": ({"减光抗%": 30}, [30]),
    "银色箭雨": ({"减光抗%": 15}, [15]),
}
TEAM_MANUAL_EFFECT_JOB_LIMITS = {
    # 刺客0转也有一个同名“重击”，但只有贤者页的重击会降低光抗。
    "重击": {"圣骑士", "十字军"},
}
TEAM_JOB_BUFFS = {
    "剑皇": ["重锤", "切骨之刺EX"],
    "月之领主": ["重锤", "旋风斩EX"],
    "狂战士": ["战吼"],
    "毁灭者": ["战吼", "飞行冲击EX"],
    "狙翎": ["鹰怒", "瞄准"],
    "魔羽": ["鹰怒", "瞄准"],
    "影舞者": ["鹰怒", "精神爆发", "捆绑射击EX"],
    "风行者": ["鹰怒", "精神爆发", "爆裂踢EX"],
    "火舞": ["霜冻", "冰刃", "冰雾", "冰刺", "弗罗斯特风", "漫长熔浆", "地狱火海EX", "火墙EX"],
    "冰灵": ["霜冻", "冰刃", "冰雾", "冰刺", "弗罗斯特风", "冰刃EX", "冰河", "暴风雪", "冰柱喷射", "霜冻EX"],
    "圣骑士": ["元素光环", "判定光环", "破甲", "连锁闪电", "雷电冲击", "重击"],
    "十字军": ["元素光环", "判定光环", "破甲", "连锁闪电", "雷电冲击", "重击"],
    "圣徒": ["无情打击", "光明之呼吸", "痊愈之手", "连锁闪电", "电光箭", "闪电风暴", "雷电之手", "天堂审判", "冲击之手", "掌握之手EX"],
    "雷神": ["无情打击", "光明之呼吸", "痊愈之手", "连锁闪电", "电光箭", "精神爆破EX", "闪电风暴", "雷电之手", "天堂审判", "神圣大地"],
    "重炮手": ["重磅鸭", "重磅轰击"],
    "机械大师": ["重磅鸭"],
    "炼金圣士": ["鸡尾酒", "冰雪连天", "岩浆墙", "毒气聚能", "疾病", "混合感染", "毒池", "C2H5OH", "冰光束", "寒冰戳刺EX"],
    "药剂师": ["鸡尾酒", "冰雪连天", "岩浆墙", "毒气聚能", "疾病", "混合感染", "爱情病毒", "疾病EX", "特制强化药剂"],
    "时空领主": ["能量反射", "时间加速"],
    "黑暗女王": ["能量反射", "时间加速"],
    "黑暗萨满": ["幽灵之魂", "怨恨之像", "狂虐之乐", "召唤傀儡", "狂暴之爪", "混沌阵", "复仇之手EX"],
    "噬魂者": ["幽灵之魂", "怨恨之像", "召唤傀儡", "狂暴之爪", "灵魂之门 EX", "灵魂尖叫"],
    "刀锋舞者": ["幽灵之魂", "九天之舞2阶"],
    "灵魂舞者": ["幽灵之魂", "九天之舞2阶"],
    "烈": ["突袭", "奉献乌鸦", "天降雨点", "灵巧猎手", "致命狂镰", "惩罚"],
    "影": ["突袭", "奉献乌鸦", "天降雨点", "致命狂镰EX", "惩罚EX"],
    "曜": ["突袭", "查克拉治愈术", "阿兹那之息", "日光焰"],
    "暗": ["突袭", "阿兹那之息", "黑暗化身", "黑暗线条", "黄昏之光"],
    "黑暗复仇者": ["火焰之力", "灵魂之刃", "火焰冲刺"],
}
TEAM_JOB_BUFF_EXCLUDES = {
    "暗": {"查克拉治愈术"},
    "曜": {"黑暗线条"},
}
TEAM_JOB_BUFF_VARIANT_ADDS = {
    "输出曜": ["黑暗线条"],
}
TEAM_JOB_BUFF_VARIANT_EXCLUDES = {
    "打坐曜": {"黑暗线条"},
    "输出曜": {"查克拉治愈术"},
}
# Profession mechanics live in editable JSON.  These assignments intentionally
# preserve the established constants consumed by the timeline and team engines.
def _mechanic_number_map(path: str) -> dict[str, float]:
    values = JOB_MECHANICS.global_value(path, {})
    return {norm(key): as_number(value) for key, value in values.items()} if isinstance(values, dict) else {}


def _mechanic_duplicate_limits() -> dict[str, tuple[set[str], int]]:
    out: dict[str, tuple[set[str], int]] = {}
    for job, rule in JOB_MECHANICS.per_job("auto_selection.duplicate_limit").items():
        if not isinstance(rule, dict):
            continue
        out[job] = (
            {norm(skill) for skill in rule.get("skills", []) if norm(skill)},
            max(1, int(as_number(rule.get("max"), 1))),
        )
    return out


def _mechanic_special_cooldown_sources() -> dict[str, dict[str, tuple[tuple[str, float], ...]]]:
    out: dict[str, dict[str, tuple[tuple[str, float], ...]]] = {}
    for job, rules in JOB_MECHANICS.per_job("special_cooldown_sources").items():
        if not isinstance(rules, dict):
            continue
        out[job] = {
            norm(skill): tuple(
                (norm(row.get("source")), as_number(row.get("cooldown")))
                for row in rows
                if isinstance(row, dict) and norm(row.get("source"))
            )
            for skill, rows in rules.items()
            if norm(skill) and isinstance(rows, list)
        }
    return out


SKILL_FIXED_COOLDOWNS = _mechanic_number_map("cooldowns.fixed")
SKILL_TIMELINE_FIXED_COOLDOWNS_BY_JOB = JOB_MECHANICS.job_skill_number_map("cooldowns.timeline_fixed_by_job")
SKILL_BURST_REFRESHABLE_PERIODIC_DAMAGE = set(JOB_MECHANICS.global_value("periodic_damage.refreshable_skills", []))
SKILL_REFRESHABLE_PERIODIC_DURATIONS = _mechanic_number_map("periodic_damage.refreshable_durations")
SKILL_PERIODIC_TOTAL_DAMAGE_CYCLES = _mechanic_number_map("periodic_damage.total_damage_cycles")
SKILL_PERIODIC_DYNAMIC_ATTACK_BUFFS = set(JOB_MECHANICS.global_value("periodic_damage.dynamic_attack_buff_summons", []))
SKILL_PERIODIC_DOUBLE_SNAPSHOT_BUFFS = set(JOB_MECHANICS.global_value("periodic_damage.double_snapshot_buff_summons", []))
SKILL_PERIODIC_REFRESHING_ATTACK_BUFFS = set(JOB_MECHANICS.global_value("periodic_damage.refreshing_attack_buffs", []))
SKILL_TIMELINE_DAMAGE_MULTIPLIERS_BY_JOB = JOB_MECHANICS.job_skill_number_map("periodic_damage.timeline_damage_multipliers")
SKILL_SUMMON_MIN_COOLDOWNS = _mechanic_number_map("summons.minimum_cooldowns")
SKILL_BURST_SIM_PASSIVE_SUMMONS = set(JOB_MECHANICS.global_value("summons.passive_timeline_skills", []))
SKILL_BURST_SUMMON_LIMITS = JOB_MECHANICS.global_value("summons.limits", {})
SKILL_BURST_ENGINEER_SUMMONS = set(JOB_MECHANICS.global_value("summons.engineer_skills", []))
SKILL_BURST_MECHANIC_MODE_ATTACKS = set(JOB_MECHANICS.global_value("summons.mechanic_mode_attacks", []))
SKILL_BURST_SHARED_SUMMON_GROUPS = JOB_MECHANICS.global_value("summons.shared_groups", [])
SKILL_BURST_HEAVY_DUCK_TARGET_COUNT = max(
    1, int(as_number(JOB_MECHANICS.global_value("summons.heavy_duck_target_count", 3), 3))
)
SKILL_SIM_AUTO_EXCLUSIVE_GROUPS = tuple(
    set(group) for group in JOB_MECHANICS.global_value("selection.exclusive_groups", [])
)
SKILL_SIM_AUTO_SKIP_BY_JOB = JOB_MECHANICS.per_job_sets("auto_selection.skip")
SKILL_SIM_AUTO_FORCED_LAST_BY_JOB = JOB_MECHANICS.per_job("auto_selection.forced_last", norm)
SKILL_SIM_AUTO_FORCE_INCLUDE_BY_JOB = JOB_MECHANICS.per_job_sets("auto_selection.force_include")
SKILL_SIM_AUTO_DUPLICATE_LIMITS_BY_JOB = _mechanic_duplicate_limits()
SKILL_FIXED_DURATION_ACTION_SPEED_OUTPUT_SKILLS = set(
    fixed_duration_action_speed_output_skills()
)
SKILL_BUILD_LEVEL_ALIASES_BY_JOB = JOB_MECHANICS.per_job("level_aliases")
SKILL_BURST_AMMO_RULES_BY_JOB = JOB_MECHANICS.per_job("resources.ammo")
SKILL_BURST_OPENING_SUPPORT_BY_JOB = JOB_MECHANICS.per_job_tuples("opening")
SKILL_BURST_BLADE_DANCE_SCOPE = set(JOB_MECHANICS.job_value("刀锋舞者", "enhanced_skill_scope", []))
SKILL_BURST_SPIRIT_DANCE_SCOPE = set(JOB_MECHANICS.job_value("灵魂舞者", "enhanced_skill_scope", []))
SKILL_BURST_COMBO_FOLLOWUPS_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.followups")
SKILL_BURST_COMBO_PRIORITY_BY_JOB = JOB_MECHANICS.per_job_nested_tuples("combos.priority")
SKILL_BURST_OPTIONAL_COMBO_FOLLOWUPS_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.optional_followups")
SKILL_BURST_DPA_PACKAGES_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.dpa_packages")
SKILL_BURST_AUTO_EXCLUDES_BY_JOB = JOB_MECHANICS.per_job_sets("auto_selection.exclude_from_timeline")
SKILL_BURST_IDLE_ONLY_BY_JOB = JOB_MECHANICS.per_job_sets("auto_selection.idle_only")
SKILL_BURST_REQUIRED_PREDECESSORS_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.required_predecessors")
SKILL_BURST_REQUIRED_FOLLOWUPS_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.required_followups")
SKILL_BURST_QUICK_PREDECESSORS_BY_JOB = JOB_MECHANICS.per_job_nested_sets("combos.quick_predecessors")
SKILL_BURST_STRICT_QUICK_PREDECESSOR_JOBS = {
    job for job in JOB_MECHANICS.documented_jobs()
    if JOB_MECHANICS.job_value(job, "combos.strict_quick_predecessors", False)
}
SKILL_BURST_SUMMON_LIMITS_BY_JOB = JOB_MECHANICS.per_job("summons")
SKILL_STANCE_ACTION_RULES_BY_JOB = JOB_MECHANICS.per_job("stance_skills")
SKILL_BURST_PASSIVE_COOLDOWN_SOURCES_BY_JOB = JOB_MECHANICS.per_job("passive_cooldown_sources")
SKILL_BURST_READY_DEADLINES_BY_JOB = JOB_MECHANICS.per_job("planning.ready_deadlines")
SKILL_SIM_SPECIAL_COOLDOWN_SOURCES_BY_JOB = _mechanic_special_cooldown_sources()

_FURY_RULE = JOB_MECHANICS.job_value("狂战士", "resources.fury", {})
SKILL_BURST_FURY_MARKER = norm(_FURY_RULE.get("marker")) or "__怒气"
SKILL_BURST_FURY_MAX = max(0, int(as_number(_FURY_RULE.get("max"), 0)))
SKILL_BURST_FURY_GENERATORS = dict(_FURY_RULE.get("generators", {}))
SKILL_BURST_FURY_CONSUMERS = {
    norm(skill): tuple(values)
    for skill, values in _FURY_RULE.get("consumers", {}).items()
    if norm(skill) and isinstance(values, list)
}
_GUARDIAN_RULE = JOB_MECHANICS.job_value("圣骑士", "resources.guardian_phantom", {})
SKILL_BURST_GUARDIAN_PHANTOM_MARKER = norm(_GUARDIAN_RULE.get("marker")) or "__护卫者幻影"
SKILL_BURST_GUARDIAN_PHANTOM_GENERATORS = dict(_GUARDIAN_RULE.get("generators", {}))
SKILL_BURST_GUARDIAN_PHANTOM_COSTS = dict(_GUARDIAN_RULE.get("costs", {}))
SKILL_BURST_GUARDIAN_PHANTOM_MAX = max(0, int(as_number(_GUARDIAN_RULE.get("max"), 0)))
_DESTROYER_MASTERY = JOB_MECHANICS.job_value("毁灭者", "mastery", {})
SKILL_BURST_DESTROYER_MASTERY_MARKER = norm(_DESTROYER_MASTERY.get("marker")) or "__毁灭力量掌握"
SKILL_BURST_DESTROYER_MASTERY_TRIGGERS = set(_DESTROYER_MASTERY.get("triggers", []))
SKILL_BURST_BERSERKER_MASTERY_MARKER = norm(
    JOB_MECHANICS.job_value("狂战士", "resources.mastery_marker", "__致命一击掌握")
)
SKILL_BURST_BRAND_MARKER = norm(
    JOB_MECHANICS.job_value("噬魂者", "resources.brand_marker", "__烙印")
)
SKILL_BURST_DANCE_STAGE_MARKER = norm(
    JOB_MECHANICS.job_value("刀锋舞者", "resources.dance_stage_marker", "__九天之舞1阶")
)
_MECHANIC_MARKERS = JOB_MECHANICS.job_value("机械大师", "state_markers", {})
SKILL_BURST_MECHANIC_MODE_MARKER = norm(_MECHANIC_MARKERS.get("mechanic_mode")) or "__机械模式"
SKILL_BURST_MECHANIC_MOMENT_MARKER = norm(_MECHANIC_MARKERS.get("mechanic_moment")) or "__机械时刻"
SKILL_BURST_ENGINEERING_FIELD_MARKER = norm(_MECHANIC_MARKERS.get("engineering_field")) or "__工程领域"

TEAM_TANK_JOBS = set(JOB_MECHANICS.global_value("team.tank_jobs", []))
TEAM_HEALER_JOBS = set(JOB_MECHANICS.global_value("team.healer_jobs", []))
TEAM_YAO_VARIANTS = set(JOB_MECHANICS.global_value("team.yao_variants", []))
TEAM_AURA_JOBS = set(JOB_MECHANICS.global_value("team.aura_jobs", []))
TEAM_AURA_OPTIONS = list(JOB_MECHANICS.global_value("team.aura_options", []))
TEAM_EXCLUSIVE_PHYSICAL_AMP_SKILLS = set(JOB_MECHANICS.global_value("team.exclusive_physical_amp_skills", []))
TEAM_EXCLUSIVE_ATTACK_BUFF_SKILLS = set(JOB_MECHANICS.global_value("team.exclusive_attack_buff_skills", []))
TEAM_JOB_BUFFS = JOB_MECHANICS.per_job("team_effects")
TEAM_JOB_BUFF_EXCLUDES = JOB_MECHANICS.per_job_sets("team_effect_excludes")
TEAM_JOB_BUFF_VARIANT_ADDS = JOB_MECHANICS.global_value("team.variant_adds", {})
TEAM_JOB_BUFF_VARIANT_EXCLUDES = {
    variant: set(skills)
    for variant, skills in JOB_MECHANICS.global_value("team.variant_excludes", {}).items()
}


class AppTableWidget(QTableWidget):
    def __init__(self, rows: int = 0, columns: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.setRowCount(rows)
        self.setColumnCount(columns)


class TimelineSequenceTable(AppTableWidget):
    rowMoveRequested = Signal(int, int)

    def __init__(self, rows: int = 0, columns: int = 0, parent: QWidget | None = None):
        super().__init__(rows, columns, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event) -> None:
        source_row = self.currentRow()
        target_row = self.rowAt(int(event.position().y()))
        if target_row < 0:
            target_row = self.rowCount() - 1
        if 0 <= source_row < self.rowCount() and 0 <= target_row < self.rowCount():
            if source_row != target_row:
                self.rowMoveRequested.emit(source_row, target_row)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class _TimelineStickyHeaderOverlay(QWidget):
    """Viewport overlay for the fixed ruler and comparison lanes.

    The timeline itself remains the scroll-area widget.  Keeping this header
    outside that widget's paint bounds prevents it from disappearing when the
    vertical scrollbar reaches its maximum value.
    """

    def __init__(self, timeline: "SkillBurstTimelineWidget", parent: QWidget):
        super().__init__(parent)
        self.timeline = timeline
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.timeline._draw_sticky_comparison_rows(painter, overlay=True)


class SkillBurstTimelineWidget(QWidget):
    sequenceMoveRequested = Signal(int, int)
    segmentSelected = Signal(int)
    timeHovered = Signal(float)
    intervalSelectionChanged = Signal(float, float, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        editable: bool = False,
        accent: str = "#2563eb",
        single_track: bool = False,
        pixels_per_second: float = 92,
    ):
        super().__init__(parent)
        self.editable = editable
        self.single_track = single_track
        self.accent = QColor(accent)
        self.window_seconds = 18.0
        self.reference_segments: list[dict[str, Any]] = []
        self.segments: list[dict[str, Any]] = []
        self.effect_windows: list[dict[str, Any]] = []
        self.metric_series: list[dict[str, Any]] = []
        self.blocked_intervals: list[dict[str, Any]] = []
        self.note = "请选择精神爆发或时间加速后查看爆发窗口。"
        self.reference_label = "自动"
        self.segment_label = "自选"
        self._default_pixels_per_second = max(10.0, as_number(pixels_per_second, 92))
        self.pixels_per_second = self._default_pixels_per_second
        self._user_pixels_per_second: float | None = None
        self._segment_rects: list[QRectF] = []
        self._reference_segment_rects: list[QRectF] = []
        self._effect_rects: list[tuple[QRectF, dict[str, Any]]] = []
        self._metric_rects: list[tuple[QRectF, dict[str, Any]]] = []
        self._drag_index = -1
        self._drag_preview_target = -1
        self._selected_index = -1
        self._drag_x = 0.0
        self._drag_start_x = 0.0
        self._drag_offset = 0.0
        self._drag_width = 0.0
        self._drag_slot_centers: list[float] = []
        self._pan_dragging = False
        self._pan_origin_global_x = 0.0
        self._pan_origin_scroll = 0
        self._pan_distance = 0.0
        self._pan_scroll_bar = None
        self._sticky_comparison_header = True
        self._sticky_scroll_area: QScrollArea | None = None
        self._sticky_overlay: _TimelineStickyHeaderOverlay | None = None
        self._sticky_segment_rects: list[QRectF] = []
        self._sticky_reference_segment_rects: list[QRectF] = []
        self._hover_position: QPointF | None = None
        self._range_selection_enabled = False
        self._selecting_range = False
        self._selection_anchor_seconds = 0.0
        self._selection_range: tuple[float, float] | None = None
        self.setMouseTracking(True)
        # The timeline width/height is intentionally controlled by
        # _refresh_geometry.  A fixed policy prevents text elision or a
        # scrollbar's transient visibility from making the whole timeline
        # jump in size while skill names are being painted.
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumHeight(218)
        self.setToolTip(
            "鼠标悬停查看时刻；拖动平移；Shift+拖动选择统计区间"
        )
        if not self.editable:
            self.setCursor(Qt.OpenHandCursor)

    def set_timeline(self, window_seconds: float, segments: list[dict[str, Any]], note: str) -> None:
        self.set_comparison_timeline(window_seconds, [], segments, note)

    def set_comparison_timeline(
        self,
        window_seconds: float,
        reference_segments: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        note: str,
    ) -> None:
        self.window_seconds = max(1.0, as_number(window_seconds, 18.0))
        if self._user_pixels_per_second is None:
            self.pixels_per_second = self._default_pixels_per_second
        self.reference_segments = reference_segments
        self.segments = segments
        self.note = note
        self._refresh_geometry()

    def set_pixels_per_second(self, value: float) -> None:
        self._user_pixels_per_second = max(10.0, as_number(value, self._default_pixels_per_second))
        self.pixels_per_second = self._user_pixels_per_second
        self._refresh_geometry()

    def set_effect_windows(self, effect_windows: list[dict[str, Any]]) -> None:
        self.effect_windows = [dict(effect) for effect in effect_windows]
        self._refresh_geometry()

    def set_metric_series(self, metric_series: list[dict[str, Any]]) -> None:
        """Show optional line metrics below the skill and effect tracks."""
        self.metric_series = [dict(series) for series in metric_series]
        self._refresh_geometry()

    def set_track_labels(self, reference: str, segment: str) -> None:
        self.reference_label = norm(reference) or "自动"
        self.segment_label = norm(segment) or "自选"
        self.update()

    def set_sticky_comparison_header(self, enabled: bool) -> None:
        """Keep the time ruler and comparison lanes visible inside a scroll area."""
        self._sticky_comparison_header = bool(enabled)
        self._bind_sticky_scroll_area()
        if not self._sticky_comparison_header and self._sticky_overlay is not None:
            self._sticky_overlay.deleteLater()
            self._sticky_overlay = None
        elif self._sticky_comparison_header:
            self._ensure_sticky_overlay()
        self.update()

    def set_range_selection_enabled(self, enabled: bool) -> None:
        self._range_selection_enabled = bool(enabled)
        self._selecting_range = False
        self.update()

    def clear_interval_selection(self) -> None:
        self._selection_range = None
        self.intervalSelectionChanged.emit(0.0, 0.0, "区间：未选择")
        self.update()

    def _seconds_at_x(self, x: float) -> float:
        return max(
            0.0,
            min(
                self.window_seconds,
                (float(x) - 72.0) / max(1.0, self.pixels_per_second),
            ),
        )

    def _selection_summary(self) -> str:
        if self._selection_range is None:
            return "区间：未选择"
        start, end = sorted(self._selection_range)
        duration = max(0.0, end - start)
        segments = [
            row
            for row in (*self.reference_segments, *self.segments)
            if max(start, as_number(row.get("start")))
            < min(end, as_number(row.get("end")))
        ]
        effects = [
            row
            for row in self.effect_windows
            if max(start, as_number(row.get("start")))
            < min(end, as_number(row.get("end")))
        ]
        return (
            f"区间 {start:.2f}s - {end:.2f}s（{duration:.2f}s）  "
            f"技能段 {len(segments)}  效果段 {len(effects)}"
        )

    def _emit_interval_selection(self) -> None:
        if self._selection_range is None:
            self.intervalSelectionChanged.emit(0.0, 0.0, "区间：未选择")
            return
        start, end = sorted(self._selection_range)
        self.intervalSelectionChanged.emit(start, end, self._selection_summary())

    def _draw_interval_selection(self, painter: QPainter) -> None:
        if self._selection_range is None:
            return
        start, end = sorted(self._selection_range)
        left = 72.0 + start * self.pixels_per_second
        right = 72.0 + end * self.pixels_per_second
        fill = QColor("#0f8fa6")
        fill.setAlpha(34)
        painter.fillRect(
            QRectF(left, 35.0, max(1.0, right - left), max(1.0, self.height() - 53.0)),
            fill,
        )
        painter.setPen(QPen(QColor("#0f8fa6"), 1, Qt.DashLine))
        painter.drawLine(left, 35.0, left, self.height() - 18.0)
        painter.drawLine(right, 35.0, right, self.height() - 18.0)
        if right - left >= 90.0:
            summary_rect = QRectF(left + 4.0, 39.0, right - left - 8.0, 20.0)
            painter.fillRect(summary_rect, QColor("#e7f5f7"))
            painter.setPen(QColor("#0f6575"))
            painter.drawText(
                summary_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    self._selection_summary(),
                    Qt.ElideRight,
                    max(1, int(summary_rect.width() - 8.0)),
                ),
            )

    def _draw_crosshair(self, painter: QPainter) -> None:
        if self._hover_position is None:
            return
        left = 72.0
        right = left + self.window_seconds * self.pixels_per_second
        x = max(left, min(right, self._hover_position.x()))
        seconds = self._seconds_at_x(x)
        painter.setPen(QPen(QColor("#0f8fa6"), 1, Qt.DashLine))
        painter.drawLine(x, 35.0, x, max(36.0, self.height() - 18.0))
        label_rect = QRectF(max(left, x - 31.0), 7.0, 62.0, 20.0)
        painter.fillRect(label_rect, QColor("#e7f5f7"))
        painter.setPen(QColor("#0f6575"))
        painter.drawText(label_rect, Qt.AlignCenter, f"{seconds:.2f}s")

    def _containing_scroll_area(self) -> QScrollArea | None:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    def _bind_sticky_scroll_area(self) -> None:
        scroll_area = self._containing_scroll_area()
        if scroll_area is None:
            return
        if scroll_area is not self._sticky_scroll_area:
            if self._sticky_overlay is not None:
                self._sticky_overlay.deleteLater()
                self._sticky_overlay = None
            self._sticky_scroll_area = scroll_area
            scroll_area.horizontalScrollBar().valueChanged.connect(self.update)
            scroll_area.verticalScrollBar().valueChanged.connect(self.update)
            scroll_area.horizontalScrollBar().valueChanged.connect(
                self._update_sticky_overlay
            )
            scroll_area.verticalScrollBar().valueChanged.connect(
                self._update_sticky_overlay
            )
            scroll_area.viewport().installEventFilter(self)
        if self._sticky_comparison_header:
            self._ensure_sticky_overlay()

    def _sticky_header_height(self) -> int:
        return int(94 if self.single_track else 140)

    def _ensure_sticky_overlay(self) -> None:
        scroll_area = self._sticky_scroll_area
        if scroll_area is None:
            return
        viewport = scroll_area.viewport()
        if self._sticky_overlay is None or self._sticky_overlay.parentWidget() is not viewport:
            if self._sticky_overlay is not None:
                self._sticky_overlay.deleteLater()
            self._sticky_overlay = _TimelineStickyHeaderOverlay(self, viewport)
        self._update_sticky_overlay()

    def _update_sticky_overlay(self) -> None:
        overlay = self._sticky_overlay
        scroll_area = self._sticky_scroll_area
        if overlay is None or scroll_area is None or not self._sticky_comparison_header:
            return
        viewport = scroll_area.viewport()
        overlay.setGeometry(0, 0, viewport.width(), self._sticky_header_height())
        overlay.raise_()
        overlay.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self._sticky_scroll_area is not None
            and watched is self._sticky_scroll_area.viewport()
            and event.type() in {QEvent.Resize, QEvent.Show, QEvent.LayoutRequest}
        ):
            self._update_sticky_overlay()
        return super().eventFilter(watched, event)

    def set_blocked_intervals(self, intervals: list[dict[str, Any]]) -> None:
        self.blocked_intervals = [dict(row) for row in intervals if isinstance(row, dict)]
        self.update()

    def _effect_lane_assignments(self) -> list[tuple[int, dict[str, Any]]]:
        assignments: list[tuple[int, dict[str, Any]]] = []
        category_groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for effect in self.effect_windows:
            category = norm(effect.get("category")) or "state"
            lane_key = (
                norm(effect.get("lane_key"))
                or norm(effect.get("source"))
                or norm(effect.get("name"))
                or "Buff"
            )
            category_groups.setdefault(category, {}).setdefault(lane_key, []).append(effect)
        base_lane = 0
        for category in sorted(
            category_groups,
            key=lambda key: (SKILL_BURST_EFFECT_CATEGORY_ORDER.get(key, 99), key),
        ):
            lane_intervals: list[list[tuple[float, float]]] = []
            for _source, group_effects in sorted(
                category_groups[category].items(),
                key=lambda item: (
                    min(as_number(effect.get("lane_order"), 99) for effect in item[1]),
                    item[0],
                ),
            ):
                intervals = sorted(
                    (
                        max(0.0, as_number(effect.get("start"))),
                        max(0.0, as_number(effect.get("end"))),
                    )
                    for effect in group_effects
                )
                lane = len(lane_intervals)
                if not any("lane_order" in effect for effect in group_effects):
                    for candidate_lane, occupied in enumerate(lane_intervals):
                        if all(
                            end <= other_start + 1e-6 or start >= other_end - 1e-6
                            for start, end in intervals
                            for other_start, other_end in occupied
                        ):
                            lane = candidate_lane
                            break
                if lane == len(lane_intervals):
                    lane_intervals.append([])
                lane_intervals[lane].extend(intervals)
                assignments.extend((base_lane + lane, effect) for effect in group_effects)
            base_lane += len(lane_intervals)
        return assignments

    @staticmethod
    def _effect_lane_layout(
        assignments: list[tuple[int, dict[str, Any]]],
    ) -> tuple[dict[int, tuple[int, int]], int]:
        """Return each effect lane's pixel offset and height."""
        categories: dict[int, str] = {}
        for lane, effect in assignments:
            categories.setdefault(lane, norm(effect.get("category")) or "state")
        layout: dict[int, tuple[int, int]] = {}
        offset = 0
        for lane in sorted(categories):
            height = 58 if categories[lane] in {"team_continuous", "team_no_gain"} else 24
            layout[lane] = (offset, height)
            offset += height
        return layout, offset

    def _layout_metrics(self) -> tuple[int, int, int, int]:
        assignments = self._effect_lane_assignments()
        _lane_layout, effect_height = self._effect_lane_layout(assignments)
        reference_y = 52
        manual_y = reference_y if self.single_track else reference_y + 48
        effect_top = manual_y + 42
        return effect_height, effect_top, reference_y, manual_y

    def _refresh_geometry(self) -> None:
        metric_seconds = [
            as_number(point[0])
            for series in self.metric_series
            for segment in series.get("segments", ())
            for point in segment.get("points", ())
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        visible_seconds = max(
            self.window_seconds,
            max(
                (
                    as_number(segment.get("end"))
                    for segment in [*self.reference_segments, *self.segments, *self.effect_windows]
                ),
                default=self.window_seconds,
            ),
            max(metric_seconds, default=self.window_seconds),
        )
        width = int(82 + visible_seconds * self.pixels_per_second + 52)
        effect_height, effect_top, _reference_y, manual_y = self._layout_metrics()
        metric_height = len(self.metric_series) * 68
        metric_gap = 10 if metric_height and effect_height else 0
        content_height = (
            effect_top
            + effect_height
            + metric_gap
            + metric_height
            + (18 if effect_height or metric_height else 8)
        )
        height = max(164 if self.single_track else 218, content_height)
        self._selected_index = min(self._selected_index, len(self.segments) - 1)
        self.setMinimumSize(width, height)
        self.resize(width, height)
        self.update()

    def set_selected_index(self, index: int) -> None:
        self._selected_index = index if 0 <= index < len(self.segments) else -1
        self.update()

    @staticmethod
    def skill_color(skill: str) -> QColor:
        palette = [
            "#5b8def", "#36a675", "#9b72cf", "#e56b78", "#2aa9bd", "#83a846",
            "#6f7fd8", "#2fa895", "#df7f5f", "#4e9db5", "#c076a1", "#7f9e5c",
            "#d28b36", "#5478c6", "#9a8b3f", "#ba6f6f", "#528f82", "#8d74b8",
            "#3b98d4", "#c5688c", "#6f9f3e", "#b87845", "#487fb0", "#8f8250",
        ]
        color_key = re.sub(r"\s*EX$", "", norm(skill), flags=re.IGNORECASE).strip()
        color_hash = 0
        for char in color_key:
            color_hash = (color_hash * 131 + ord(char)) % 104729
        return QColor(palette[color_hash % len(palette)])

    @staticmethod
    def skill_cell_color(skill: str) -> QColor:
        return SkillBurstTimelineWidget.skill_color(skill).lighter(138)

    @staticmethod
    def skill_timeline_fill_color(skill: str) -> QColor:
        return SkillBurstTimelineWidget.skill_color(skill).lighter(112)

    @staticmethod
    def apply_skill_item_color(item: QTableWidgetItem | None, skill: str) -> None:
        if item is None:
            return
        color = SkillBurstTimelineWidget.skill_cell_color(skill)
        item.setBackground(color)
        item.setData(Qt.BackgroundRole, color)
        item.setForeground(QColor("#0f172a"))

    def _draw_segment(
        self,
        painter: QPainter,
        segment: dict[str, Any],
        rect: QRectF,
        *,
        reference: bool = False,
        selected: bool = False,
        dragging: bool = False,
    ) -> None:
        skill = norm(segment.get("skill"))
        start = max(0.0, as_number(segment.get("start")))
        end = max(start, as_number(segment.get("end")))
        color = self.skill_color(skill)
        fill = color.lighter(148) if reference else self.skill_timeline_fill_color(skill)
        border = QColor("#94a3b8") if reference else color.darker(130)
        if selected:
            border = self.accent
        painter.setPen(QPen(border, 2 if selected or dragging else 1, Qt.DashLine if reference else Qt.SolidLine))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor("#334155" if reference else "#0f172a"))
        text = skill
        elided = painter.fontMetrics().elidedText(text, Qt.ElideRight, max(8, int(rect.width() - 10)))
        painter.drawText(rect.adjusted(5, 0, -5, 0), Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _draw_sticky_comparison_rows(
        self, painter: QPainter, *, overlay: bool = False
    ) -> None:
        if not self._sticky_comparison_header:
            self._sticky_segment_rects = []
            self._sticky_reference_segment_rects = []
            return
        self._bind_sticky_scroll_area()
        scroll_area = self._sticky_scroll_area
        if scroll_area is None:
            return
        horizontal_offset = float(scroll_area.horizontalScrollBar().value())
        vertical_offset = float(scroll_area.verticalScrollBar().value())
        viewport_width = float(scroll_area.viewport().width())
        left = 72.0
        axis_y = vertical_offset + 35.0
        reference_y = vertical_offset + 52.0
        manual_y = reference_y if self.single_track else vertical_offset + 100.0
        row_h = 34.0
        header_height = 94.0 if self.single_track else 140.0
        viewport_rect = QRectF(
            horizontal_offset,
            vertical_offset,
            max(1.0, viewport_width),
            header_height,
        )
        data_clip = QRectF(
            horizontal_offset + left,
            vertical_offset,
            max(1.0, viewport_width - left),
            header_height,
        )

        painter.save()
        if overlay:
            # The overlay is positioned at viewport (0, 0), while the rows'
            # geometry remains in timeline coordinates for hit testing.
            painter.translate(-horizontal_offset, -vertical_offset)
        painter.setClipRect(viewport_rect)
        painter.fillRect(viewport_rect, QColor("#f8fafc"))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawLine(
            horizontal_offset + left,
            axis_y,
            horizontal_offset + viewport_width,
            axis_y,
        )
        min_tick_seconds = 48.0 / max(1.0, self.pixels_per_second)
        tick_step = next(
            (
                candidate
                for candidate in (1, 2, 5, 10, 20, 30)
                if candidate >= min_tick_seconds
            ),
            30,
        )
        first_visible_seconds = max(
            0,
            int((horizontal_offset - left) / max(1.0, self.pixels_per_second)),
        )
        tick = (first_visible_seconds // tick_step) * tick_step
        while tick <= int(self.window_seconds + 0.001):
            x = left + tick * self.pixels_per_second
            if x > horizontal_offset + viewport_width:
                break
            if x >= horizontal_offset + left:
                painter.setPen(QPen(QColor("#94a3b8"), 1))
                painter.drawLine(x, axis_y - 5, x, axis_y + 5)
                painter.setPen(QColor("#475569"))
                painter.drawText(
                    QRectF(x - 20, vertical_offset + 7, 40, 18),
                    Qt.AlignCenter,
                    f"{tick}s",
                )
                painter.setPen(QPen(QColor("#d7e0ea"), 1))
                painter.drawLine(
                    x,
                    axis_y + 10,
                    x,
                    vertical_offset + header_height - 1.0,
                )
            tick += tick_step

        painter.setClipRect(data_clip)
        visible_reference = (
            []
            if self.single_track
            else [
                segment
                for segment in self.reference_segments
                if not segment.get("timeline_hidden")
            ]
        )
        self._sticky_reference_segment_rects = []
        for segment in visible_reference:
            start = max(0.0, as_number(segment.get("start")))
            end = max(start, as_number(segment.get("end")))
            rect = QRectF(
                left + start * self.pixels_per_second,
                reference_y,
                max(10.0, (end - start) * self.pixels_per_second),
                row_h,
            )
            self._sticky_reference_segment_rects.append(rect)
            self._draw_segment(painter, segment, rect, reference=True)

        self._sticky_segment_rects = [QRectF() for _segment in self.segments]
        for index, segment in enumerate(self.segments):
            if segment.get("timeline_hidden"):
                continue
            start = max(0.0, as_number(segment.get("start")))
            end = max(start, as_number(segment.get("end")))
            rect = QRectF(
                left + start * self.pixels_per_second,
                manual_y,
                max(10.0, (end - start) * self.pixels_per_second),
                row_h,
            )
            self._sticky_segment_rects[index] = rect
            self._draw_segment(
                painter,
                segment,
                rect,
                selected=index == self._selected_index,
            )

        painter.setClipRect(viewport_rect)
        label_rect = QRectF(horizontal_offset, vertical_offset, left, 140.0)
        painter.fillRect(label_rect, QColor("#f8fafc"))
        painter.setPen(QColor("#334155"))
        painter.drawText(
            QRectF(horizontal_offset + 12, axis_y - 8, 54, 22),
            Qt.AlignRight | Qt.AlignVCenter,
            "时间",
        )
        painter.setPen(QColor("#64748b"))
        if self.single_track:
            painter.drawText(
                QRectF(horizontal_offset + 12, manual_y, 50, row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                "技能轴",
            )
        else:
            painter.drawText(
                QRectF(horizontal_offset + 12, reference_y, 50, row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                self.reference_label,
            )
            painter.drawText(
                QRectF(horizontal_offset + 12, manual_y, 50, row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                self.segment_label,
            )
        painter.setPen(QPen(QColor("#b7c7d1"), 1))
        painter.drawLine(
            horizontal_offset,
            vertical_offset + header_height - 1.0,
            horizontal_offset + viewport_width,
            vertical_offset + header_height - 1.0,
        )
        painter.restore()

    def paintEvent(self, _event) -> None:
        self._bind_sticky_scroll_area()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#f8fafc"))

        left = 72
        axis_y = 35
        row_h = 34
        axis_w = self.window_seconds * self.pixels_per_second

        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawLine(left, axis_y, int(left + axis_w), axis_y)
        min_tick_seconds = 48.0 / max(1.0, self.pixels_per_second)
        tick_candidates = (1, 2, 5, 10, 20, 30)
        tick_step = next(
            (candidate for candidate in tick_candidates if candidate >= min_tick_seconds),
            tick_candidates[-1],
        )
        tick = 0
        while tick <= int(self.window_seconds + 0.001):
            x = left + tick * self.pixels_per_second
            painter.drawLine(int(x), axis_y - 5, int(x), axis_y + 5)
            painter.setPen(QColor("#475569"))
            painter.drawText(QRectF(x - 16, 7, 40, 18), Qt.AlignCenter, f"{tick}s")
            painter.setPen(QPen(QColor("#d7e0ea"), 1))
            painter.drawLine(int(x), axis_y + 10, int(x), self.height() - 18)
            painter.setPen(QPen(QColor("#94a3b8"), 1))
            tick += tick_step

        painter.setPen(QColor("#334155"))
        painter.drawText(QRectF(12, axis_y - 8, 54, 22), Qt.AlignRight | Qt.AlignVCenter, "时间")
        for interval in self.blocked_intervals:
            start = max(0.0, min(self.window_seconds, as_number(interval.get("start"))))
            end = max(start, min(self.window_seconds, as_number(interval.get("end"))))
            if end <= start + 1e-6:
                continue
            rect = QRectF(
                left + start * self.pixels_per_second,
                axis_y + 10,
                max(3.0, (end - start) * self.pixels_per_second),
                max(20.0, self.height() - axis_y - 28),
            )
            fill = QColor("#dc7a84")
            fill.setAlpha(48)
            painter.setPen(QPen(QColor("#c45a68"), 1, Qt.DashLine))
            painter.setBrush(fill)
            painter.drawRect(rect)
            painter.setPen(QColor("#8e3340"))
            label = norm(interval.get("name")) or "停止输出"
            text = painter.fontMetrics().elidedText(label, Qt.ElideRight, max(8, int(rect.width() - 8)))
            painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignTop | Qt.AlignLeft, text)
        if (
            not self.segments
            and not self.reference_segments
            and not self.effect_windows
            and not self.metric_series
        ):
            painter.setPen(QColor("#64748b"))
            painter.drawText(QRectF(left, axis_y + 28, max(320, axis_w), 60), Qt.AlignLeft | Qt.AlignTop, self.note)
            self._draw_interval_selection(painter)
            self._draw_crosshair(painter)
            return

        _effect_height, effect_top, reference_y, manual_y = self._layout_metrics()
        effect_assignments = self._effect_lane_assignments()
        effect_lane_layout, _total_effect_height = self._effect_lane_layout(effect_assignments)
        self._effect_rects = []
        for lane, effect in effect_assignments:
            start = max(0.0, as_number(effect.get("start")))
            end = max(start, as_number(effect.get("end")))
            lane_offset, lane_height = effect_lane_layout.get(lane, (lane * 24, 24))
            rect = QRectF(
                left + start * self.pixels_per_second,
                effect_top + lane_offset,
                max(10.0, (end - start) * self.pixels_per_second),
                lane_height - 5,
            )
            color = QColor(norm(effect.get("color")) or "#38bdf8")
            fill = QColor(color)
            fill.setAlpha(92)
            painter.setPen(QPen(color.darker(118), 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor("#0f172a"))
            label = norm(effect.get("name"))
            if norm(effect.get("category")) in {"team_continuous", "team_no_gain"}:
                painter.drawText(
                    rect.adjusted(5, 2, -5, -2),
                    Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
                    label,
                )
            else:
                text = painter.fontMetrics().elidedText(label, Qt.ElideRight, max(8, int(rect.width() - 8)))
                painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
            self._effect_rects.append((rect, effect))
        if effect_assignments:
            painter.setPen(QColor("#475569"))
            resist_lanes = [lane for lane, effect in effect_assignments if norm(effect.get("category")) == "resist"]
            continuous_lanes = [lane for lane, effect in effect_assignments if norm(effect.get("category")) == "team_continuous"]
            teammate_categories = {
                "team_buff": "团队Buff",
                "team_amp": "增伤Debuff",
                "team_defense": "减防Debuff",
                "team_crit_resist": "减致抗Debuff",
                "team_freeze": "叠冰Debuff",
            }
            personal_lanes = [lane for lane, effect in effect_assignments if norm(effect.get("category")) == "team_personal"]
            no_gain_lanes = [lane for lane, effect in effect_assignments if norm(effect.get("category")) == "team_no_gain"]
            buff_lanes = [
                lane
                for lane, effect in effect_assignments
                if norm(effect.get("category")) not in {
                    "resist", "team_resist", "team_personal", "team_continuous", "team_no_gain",
                    *teammate_categories,
                }
            ]
            for category, label in teammate_categories.items():
                lanes = [lane for lane, effect in effect_assignments if norm(effect.get("category")) == category]
                if lanes:
                    lane_offset, lane_height = effect_lane_layout[min(lanes)]
                    painter.drawText(QRectF(4, effect_top + lane_offset, 62, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, label)
            if personal_lanes:
                lane_offset, lane_height = effect_lane_layout[min(personal_lanes)]
                painter.drawText(QRectF(4, effect_top + lane_offset, 62, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, "个人效果")
            if continuous_lanes:
                lane_offset, lane_height = effect_lane_layout[min(continuous_lanes)]
                painter.drawText(QRectF(4, effect_top + lane_offset, 62, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, "常驻Buff")
            if no_gain_lanes:
                lane_offset, lane_height = effect_lane_layout[min(no_gain_lanes)]
                painter.drawText(QRectF(4, effect_top + lane_offset, 62, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, "无提升Buff")
            if resist_lanes:
                for lane in sorted(set(resist_lanes)):
                    effect = next(
                        row for assigned_lane, row in effect_assignments
                        if assigned_lane == lane and norm(row.get("category")) == "resist"
                    )
                    lane_offset, lane_height = effect_lane_layout[lane]
                    label = norm(effect.get("lane_label")) or "减抗"
                    painter.drawText(QRectF(4, effect_top + lane_offset, 58, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, label)
            team_resist_lanes = [
                lane for lane, effect in effect_assignments
                if norm(effect.get("category")) == "team_resist"
            ]
            for lane in sorted(set(team_resist_lanes)):
                effect = next(
                    row for assigned_lane, row in effect_assignments
                    if assigned_lane == lane and norm(row.get("category")) == "team_resist"
                )
                lane_offset, lane_height = effect_lane_layout[lane]
                label = norm(effect.get("lane_label")) or "减抗"
                painter.drawText(QRectF(4, effect_top + lane_offset, 58, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, label)
            if buff_lanes:
                lane_offset, lane_height = effect_lane_layout[min(buff_lanes)]
                painter.drawText(QRectF(12, effect_top + lane_offset, 50, lane_height - 5), Qt.AlignRight | Qt.AlignVCenter, "Buff")

        metric_top = effect_top + _total_effect_height
        if self.metric_series and _total_effect_height:
            metric_top += 10
        self._metric_rects = []
        for metric_index, series in enumerate(self.metric_series):
            row_rect = QRectF(
                left,
                metric_top + metric_index * 68,
                max(1.0, axis_w),
                56,
            )
            painter.setPen(QPen(QColor("#d7e0ea"), 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(row_rect, 4, 4)
            minimum = as_number(series.get("minimum"), 0.0)
            maximum = as_number(series.get("maximum"), 100.0)
            all_values = [
                as_number(point[1])
                for segment in series.get("segments", ())
                for point in segment.get("points", ())
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if series.get("auto_range") and all_values:
                minimum = min(minimum, min(all_values))
                maximum = max(maximum, max(all_values))
            if maximum <= minimum + 1e-9:
                maximum = minimum + 1.0
            for tick_index in range(5):
                tick_ratio = tick_index / 4.0
                tick_y = row_rect.bottom() - 5 - tick_ratio * max(
                    1.0, row_rect.height() - 10
                )
                painter.setPen(QPen(QColor("#e3eaf0"), 1))
                painter.drawLine(row_rect.left(), tick_y, row_rect.right(), tick_y)
            if series.get("zero_line") and minimum < 0.0 < maximum:
                zero_ratio = (0.0 - minimum) / (maximum - minimum)
                zero_y = row_rect.bottom() - 5 - zero_ratio * max(
                    1.0, row_rect.height() - 10
                )
                painter.setPen(QPen(QColor("#94a3b8"), 1, Qt.DashLine))
                painter.drawLine(row_rect.left(), zero_y, row_rect.right(), zero_y)
            base_color = QColor(norm(series.get("color")) or "#17859c")
            positive_color = QColor(norm(series.get("positive_color")) or "#24965e")
            negative_color = QColor(norm(series.get("negative_color")) or "#cf5b65")
            delta_colors = bool(series.get("delta_colors"))
            for segment in series.get("segments", ()):
                points = [
                    (max(0.0, as_number(point[0])), as_number(point[1]))
                    for point in segment.get("points", ())
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                points.sort(key=lambda point: point[0])
                previous: tuple[float, float] | None = None
                for point_index, (seconds, value) in enumerate(points):
                    x = left + seconds * self.pixels_per_second
                    ratio = max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))
                    y = row_rect.bottom() - 5 - ratio * max(1.0, row_rect.height() - 10)
                    if previous is not None:
                        previous_seconds, previous_value = previous
                        previous_x = left + previous_seconds * self.pixels_per_second
                        previous_ratio = max(
                            0.0,
                            min(1.0, (previous_value - minimum) / (maximum - minimum)),
                        )
                        previous_y = row_rect.bottom() - 5 - previous_ratio * max(
                            1.0, row_rect.height() - 10
                        )
                        line_color = base_color
                        if delta_colors:
                            line_color = (
                                positive_color
                                if value >= previous_value
                                else negative_color
                            )
                        painter.setPen(QPen(line_color, 2))
                        painter.drawLine(
                            QPointF(previous_x, previous_y), QPointF(x, y)
                        )
                    if (
                        point_index == len(points) - 1
                        or previous is None
                        or abs(value - previous[1]) >= as_number(
                            series.get("marker_threshold"), 2.0
                        )
                    ):
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(
                            positive_color
                            if delta_colors and previous is not None and value >= previous[1]
                            else negative_color
                            if delta_colors and previous is not None
                            else base_color
                        )
                        painter.drawEllipse(QPointF(x, y), 2.5, 2.5)
                    previous = (seconds, value)
            self._metric_rects.append((row_rect, series))
            scroll_area = self._containing_scroll_area()
            fixed_left = (
                float(scroll_area.horizontalScrollBar().value())
                if scroll_area is not None
                else 0.0
            )
            viewport_width = (
                float(scroll_area.viewport().width())
                if scroll_area is not None
                else float(self.width())
            )
            metric_name = norm(series.get("name")) or "指标"
            suffix = norm(series.get("suffix"))
            painter.fillRect(
                QRectF(fixed_left, row_rect.top(), left, row_rect.height()),
                QColor("#f8fafc"),
            )
            painter.setPen(QColor("#475569"))
            painter.drawText(
                QRectF(fixed_left + 4, row_rect.top() + 18, left - 10, 20),
                Qt.AlignCenter,
                metric_name,
            )
            if metric_name == "Boss血量":
                axis_width = 48.0
                axis_left = fixed_left + viewport_width - axis_width
                painter.fillRect(
                    QRectF(axis_left, row_rect.top(), axis_width, row_rect.height()),
                    QColor("#f8fafc"),
                )
                painter.setPen(QColor("#a84a54"))
                painter.drawText(
                    QRectF(axis_left + 2, row_rect.top(), axis_width - 4, 18),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"{maximum:g}{suffix}",
                )
                painter.drawText(
                    QRectF(axis_left + 2, row_rect.bottom() - 18, axis_width - 4, 18),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"{minimum:g}{suffix}",
                )
            else:
                painter.drawText(
                    QRectF(fixed_left + 4, row_rect.top(), left - 10, 17),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"{maximum:g}{suffix}",
                )
                painter.drawText(
                    QRectF(fixed_left + 4, row_rect.bottom() - 17, left - 10, 17),
                    Qt.AlignRight | Qt.AlignVCenter,
                    f"{minimum:g}{suffix}",
                )

        painter.setPen(QColor("#64748b"))
        if self.single_track:
            painter.drawText(QRectF(12, manual_y, 50, row_h), Qt.AlignRight | Qt.AlignVCenter, "技能轴")
        else:
            painter.drawText(
                QRectF(12, reference_y, 50, row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                self.reference_label,
            )
            painter.drawText(
                QRectF(12, manual_y, 50, row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                self.segment_label,
            )
        self._reference_segment_rects = []
        for segment in (
            []
            if self.single_track
            else [segment for segment in self.reference_segments if not segment.get("timeline_hidden")]
        ):
            start = max(0.0, as_number(segment.get("start")))
            end = max(start, as_number(segment.get("end")))
            rect = QRectF(left + start * self.pixels_per_second, reference_y, max(10.0, (end - start) * self.pixels_per_second), row_h)
            self._reference_segment_rects.append(rect)
            self._draw_segment(painter, segment, rect, reference=True)

        indexed_segments = [
            (index, segment)
            for index, segment in enumerate(self.segments)
            if not segment.get("timeline_hidden")
        ]
        if self._drag_index >= 0 and 0 <= self._drag_preview_target < len(indexed_segments):
            dragged_item = indexed_segments.pop(self._drag_index)
            indexed_segments.insert(self._drag_preview_target, dragged_item)
        slots = [max(0.0, as_number(segment.get("start"))) for _index, segment in indexed_segments]
        self._segment_rects = [QRectF() for _segment in self.segments]
        for display_index, (original_index, segment) in enumerate(indexed_segments):
            start = slots[display_index] if display_index < len(slots) else max(0.0, as_number(segment.get("start")))
            duration = max(0.0, as_number(segment.get("end")) - as_number(segment.get("start")))
            x = left + start * self.pixels_per_second
            if original_index == self._drag_index:
                x = max(left, self._drag_x - self._drag_offset)
            rect = QRectF(x, manual_y, max(10.0, duration * self.pixels_per_second), row_h)
            self._segment_rects[original_index] = rect
            self._draw_segment(
                painter,
                segment,
                rect,
                selected=original_index == self._selected_index,
                dragging=original_index == self._drag_index,
            )
        self._draw_interval_selection(painter)
        if self._sticky_overlay is None:
            self._draw_sticky_comparison_rows(painter)
        self._draw_crosshair(painter)

    def _select_segment_at(self, point: QPointF) -> bool:
        clicked_index = next(
            (index for index, rect in enumerate(self._segment_rects) if rect.contains(point)),
            -1,
        )
        if clicked_index < 0:
            clicked_index = next(
                (
                    index
                    for index, rect in enumerate(self._sticky_segment_rects)
                    if rect.contains(point)
                ),
                -1,
            )
        if clicked_index >= 0:
            self._selected_index = clicked_index
            self.segmentSelected.emit(self._selected_index)
            self.update()
            return True
        reference_index = next(
            (index for index, rect in enumerate(self._reference_segment_rects) if rect.contains(point)),
            -1,
        )
        if reference_index < 0:
            reference_index = next(
                (
                    index
                    for index, rect in enumerate(
                        self._sticky_reference_segment_rects
                    )
                    if rect.contains(point)
                ),
                -1,
            )
        if reference_index >= 0:
            visible_reference = [
                segment for segment in self.reference_segments if not segment.get("timeline_hidden")
            ]
            reference_skill = norm(visible_reference[reference_index].get("skill"))
            occurrence = sum(
                1
                for segment in visible_reference[: reference_index + 1]
                if norm(segment.get("skill")) == reference_skill
            )
            matching_indexes = [
                index
                for index, segment in enumerate(self.segments)
                if not segment.get("timeline_hidden") and norm(segment.get("skill")) == reference_skill
            ]
            if matching_indexes:
                self._selected_index = matching_indexes[min(occurrence - 1, len(matching_indexes) - 1)]
            else:
                visible_manual_indexes = [
                    index for index, segment in enumerate(self.segments) if not segment.get("timeline_hidden")
                ]
                if visible_manual_indexes:
                    self._selected_index = visible_manual_indexes[min(reference_index, len(visible_manual_indexes) - 1)]
            if self._selected_index >= 0:
                self.segmentSelected.emit(self._selected_index)
                self.update()
                return True
        return False

    def _containing_horizontal_scroll_bar(self):
        scroll_area = self._containing_scroll_area()
        return scroll_area.horizontalScrollBar() if scroll_area is not None else None

    def mousePressEvent(self, event) -> None:
        point = event.position()
        selecting_range = self._range_selection_enabled or bool(
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        )
        if (
            event.button() == Qt.MouseButton.LeftButton
            and selecting_range
            and 72.0 <= point.x() <= 72.0 + self.window_seconds * self.pixels_per_second
        ):
            seconds = self._seconds_at_x(point.x())
            self._selecting_range = True
            self._selection_anchor_seconds = seconds
            self._selection_range = (seconds, seconds)
            self.setCursor(Qt.CrossCursor)
            QToolTip.hideText()
            self.update()
            event.accept()
            return
        clicked_index = next(
            (index for index, rect in enumerate(self._segment_rects) if rect.contains(point)),
            -1,
        )
        if clicked_index < 0:
            clicked_index = next(
                (
                    index
                    for index, rect in enumerate(self._sticky_segment_rects)
                    if rect.contains(point)
                ),
                -1,
            )
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.editable
            and clicked_index >= 0
        ):
            self._drag_index = clicked_index
            self._drag_x = point.x()
            self._drag_start_x = point.x()
            self._drag_offset = point.x() - self._segment_rects[self._drag_index].left()
            self._drag_width = self._segment_rects[self._drag_index].width()
            self._drag_slot_centers = [rect.center().x() for rect in self._segment_rects]
            self._drag_preview_target = self._drag_index
            self._selected_index = self._drag_index
            self.segmentSelected.emit(self._selected_index)
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            scroll_bar = self._containing_horizontal_scroll_bar()
            if scroll_bar is not None and scroll_bar.maximum() > 0:
                self._pan_dragging = True
                self._pan_origin_global_x = event.globalPosition().x()
                self._pan_origin_scroll = scroll_bar.value()
                self._pan_distance = 0.0
                self._pan_scroll_bar = scroll_bar
                self.setCursor(Qt.ClosedHandCursor)
                QToolTip.hideText()
                event.accept()
                return
        if event.button() == Qt.MouseButton.LeftButton and self._select_segment_at(point):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._selecting_range:
            seconds = self._seconds_at_x(event.position().x())
            self._selection_range = tuple(
                sorted((self._selection_anchor_seconds, seconds))
            )
            self._hover_position = QPointF(event.position())
            self._emit_interval_selection()
            QToolTip.showText(
                event.globalPosition().toPoint(), self._selection_summary(), self
            )
            self.update()
            event.accept()
            return
        if self._pan_dragging and self._pan_scroll_bar is not None:
            delta_x = event.globalPosition().x() - self._pan_origin_global_x
            self._pan_distance = max(self._pan_distance, abs(delta_x))
            self._pan_scroll_bar.setValue(
                round(self._pan_origin_scroll - delta_x)
            )
            QToolTip.hideText()
            event.accept()
            return
        if self._drag_index >= 0:
            self._drag_x = event.position().x()
            if self._drag_slot_centers:
                dragged_center = self._drag_x - self._drag_offset + self._drag_width / 2
                self._drag_preview_target = min(
                    range(len(self._drag_slot_centers)),
                    key=lambda index: abs(self._drag_slot_centers[index] - dragged_center),
                )
            self.update()
            event.accept()
            return
        if 72.0 <= event.position().x() <= 72.0 + self.window_seconds * self.pixels_per_second:
            self._hover_position = QPointF(event.position())
            self.timeHovered.emit(self._seconds_at_x(event.position().x()))
            self.update()
        else:
            self._hover_position = None
        metric_row = next(
            (series for rect, series in self._metric_rects if rect.contains(event.position())),
            None,
        )
        if metric_row is not None:
            hovered_seconds = max(
                0.0,
                (event.position().x() - 72.0) / max(1.0, self.pixels_per_second),
            )
            candidates = [
                (
                    abs(as_number(point[0]) - hovered_seconds),
                    as_number(point[0]),
                    as_number(point[1]),
                    norm(segment.get("name")),
                )
                for segment in metric_row.get("segments", ())
                for point in segment.get("points", ())
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if candidates:
                _distance, seconds, value, segment_name = min(candidates)
                suffix = norm(metric_row.get("suffix"))
                tooltip = (
                    f"{norm(metric_row.get('name')) or '指标'}"
                    + (f" · {segment_name}" if segment_name else "")
                    + f"\n{seconds:.2f}s：{value:.2f}{suffix}"
                )
                detail = norm(metric_row.get("detail"))
                if detail:
                    tooltip += f"\n{detail}"
                QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
            self.setCursor(Qt.OpenHandCursor if not self.editable else Qt.ArrowCursor)
            event.accept()
            return
        effect_window = next(
            (effect for rect, effect in self._effect_rects if rect.contains(event.position())),
            None,
        )
        if effect_window is not None:
            start = max(0.0, as_number(effect_window.get("start")))
            end = max(start, as_number(effect_window.get("end")))
            detail = norm(effect_window.get("detail"))
            tooltip = f"{norm(effect_window.get('name'))}\n{start:.2f}s - {end:.2f}s  覆盖 {end - start:.2f}s"
            if detail:
                tooltip += f"\n{detail}"
            QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
            self.setCursor(Qt.OpenHandCursor if not self.editable else Qt.ArrowCursor)
            event.accept()
            return
        hovered_index = next(
            (index for index, rect in enumerate(self._segment_rects) if rect.contains(event.position())),
            -1,
        )
        if hovered_index < 0:
            hovered_index = next(
                (
                    index
                    for index, rect in enumerate(self._sticky_segment_rects)
                    if rect.contains(event.position())
                ),
                -1,
            )
        reference_index = next(
            (index for index, rect in enumerate(self._reference_segment_rects) if rect.contains(event.position())),
            -1,
        )
        if reference_index < 0:
            reference_index = next(
                (
                    index
                    for index, rect in enumerate(
                        self._sticky_reference_segment_rects
                    )
                    if rect.contains(event.position())
                ),
                -1,
            )
        segment = (
            self.segments[hovered_index]
            if hovered_index >= 0
            else self.reference_segments[reference_index]
            if reference_index >= 0
            else None
        )
        if segment is not None:
            start = max(0.0, as_number(segment.get("start")))
            end = max(start, as_number(segment.get("end")))
            detail = norm(segment.get("detail"))
            tooltip = (
                f"{norm(segment.get('skill'))}\n"
                f"{start:.2f}s - {end:.2f}s  施法 {end - start:.2f}s"
            )
            if detail:
                tooltip += f"\n{detail}"
            QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
        else:
            QToolTip.hideText()
        self.setCursor(
            Qt.OpenHandCursor
            if not self.editable
            or (hovered_index >= 0 or reference_index >= 0)
            else Qt.ArrowCursor
        )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._selecting_range and event.button() == Qt.MouseButton.LeftButton:
            self._selecting_range = False
            seconds = self._seconds_at_x(event.position().x())
            self._selection_range = tuple(
                sorted((self._selection_anchor_seconds, seconds))
            )
            self._emit_interval_selection()
            QToolTip.showText(
                event.globalPosition().toPoint(), self._selection_summary(), self
            )
            self.setCursor(Qt.CrossCursor if self._range_selection_enabled else (
                Qt.ArrowCursor if self.editable else Qt.OpenHandCursor
            ))
            self.update()
            event.accept()
            return
        if self._pan_dragging and event.button() == Qt.MouseButton.LeftButton:
            was_click = self._pan_distance <= 4.0
            self._pan_dragging = False
            self._pan_scroll_bar = None
            self.setCursor(
                Qt.OpenHandCursor if not self.editable else Qt.ArrowCursor
            )
            if was_click:
                self._select_segment_at(event.position())
            event.accept()
            return
        if self._drag_index >= 0:
            old_index = self._drag_index
            x = event.position().x()
            target_index = self._drag_preview_target
            self._drag_index = -1
            self._drag_preview_target = -1
            self._drag_slot_centers = []
            self._drag_width = 0.0
            self.unsetCursor()
            self.update()
            if abs(x - self._drag_start_x) >= 8 and 0 <= target_index < len(self.segments) and target_index != old_index:
                self._selected_index = target_index
                self.segmentSelected.emit(target_index)
                self.sequenceMoveRequested.emit(old_index, target_index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._selecting_range:
            self._hover_position = None
            QToolTip.hideText()
            self.update()
        super().leaveEvent(event)


class SkillTreeArrowOverlay(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.links: list[tuple[QWidget, QWidget]] = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_links(self, links: list[tuple[QWidget, QWidget]]) -> None:
        self.links = links
        self.raise_()
        self.update()

    def paintEvent(self, _event) -> None:
        if not self.links:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(14, 116, 144, 135)
        painter.setPen(QPen(color, 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(color)
        for parent_widget, child_widget in self.links:
            if not parent_widget.isVisible() or not child_widget.isVisible():
                continue
            start = self.mapFromGlobal(parent_widget.mapToGlobal(parent_widget.rect().center()))
            end = self.mapFromGlobal(child_widget.mapToGlobal(child_widget.rect().center()))
            dx = end.x() - start.x()
            dy = end.y() - start.y()
            length = hypot(dx, dy)
            if length < 24:
                continue
            ux = dx / length
            uy = dy / length
            start = QPointF(start.x() + ux * 34, start.y() + uy * 28)
            end = QPointF(end.x() - ux * 42, end.y() - uy * 34)
            painter.drawLine(start, end)
            arrow = 15
            wing = 7
            base = QPointF(end.x() - ux * arrow, end.y() - uy * arrow)
            left = QPointF(base.x() - uy * wing, base.y() + ux * wing)
            right = QPointF(base.x() + uy * wing, base.y() - ux * wing)
            painter.drawPolygon(QPolygonF([end, left, right]))


class SkillBuildGridHost(QWidget):
    def __init__(self):
        super().__init__()
        self.arrow_overlay = SkillTreeArrowOverlay(self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.arrow_overlay.setGeometry(self.rect())
        self.arrow_overlay.raise_()


class SkillTreeZoomView(QGraphicsView):
    def __init__(self, content: QWidget, owner: Any):
        super().__init__()
        self.content = content
        self.owner = owner
        self._refresh_pending = False
        self._last_content_size = None
        self.scene = QGraphicsScene(self)
        self.proxy = self.scene.addWidget(content)
        self.setScene(self.scene)
        self.setObjectName("skillTreeZoomView")
        self.setFrameShape(QFrame.NoFrame)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.content.installEventFilter(self)
        self.set_zoom(float(getattr(owner, "skill_build_zoom", 1.0)))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.content and event.type() in {QEvent.LayoutRequest, QEvent.Resize, QEvent.Show}:
            self.schedule_scene_rect_refresh()
        return super().eventFilter(watched, event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.owner._adjust_skill_build_zoom(1 if delta > 0 else -1)
                event.accept()
                return
        super().wheelEvent(event)

    def set_zoom(self, zoom: float) -> None:
        self.resetTransform()
        self.scale(zoom, zoom)
        self.schedule_scene_rect_refresh()

    def schedule_scene_rect_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self.refresh_scene_rect)

    def refresh_scene_rect(self) -> None:
        self._refresh_pending = False
        if not all(
            qt_object_is_valid(value)
            for value in (self, self.content, self.scene, self.proxy)
        ):
            return
        size = self.content.sizeHint()
        if not size.isValid():
            size = self.content.size()
        if not size.isValid():
            return
        if self._last_content_size == size and self.scene.sceneRect().size() == self.proxy.size():
            return
        self._last_content_size = size
        if self.content.size() != size:
            self.content.resize(size)
        if self.proxy.size() != size:
            self.proxy.resize(size)
        self.scene.setSceneRect(QRectF(QPointF(0, 0), self.proxy.size()))


def resource_path(relative: Path) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    packaged = base / relative
    if packaged.exists():
        return packaged
    parts = list(relative.parts)
    if "assets" in parts:
        asset_index = parts.index("assets")
        return Path(__file__).resolve().parent / "assets" / Path(*parts[asset_index + 1 :])
    return Path(__file__).resolve().parent / "assets" / relative.name


def read_skill_build_defaults_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_user_skill_build_defaults() -> dict[str, Any]:
    return read_skill_build_defaults_file(SKILL_BUILD_DEFAULTS_PATH)


def load_builtin_skill_build_defaults() -> dict[str, Any]:
    return read_skill_build_defaults_file(BUILTIN_SKILL_BUILD_DEFAULTS_PATH)


_SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE: tuple[tuple[int, int], tuple[int, int]] | None = None
_SKILL_BUILD_DEFAULTS_CACHE: dict[str, Any] = {}
_SKILL_BUILD_DEFAULTS_CHECKED_AT = 0.0
_SKILL_BUILD_DEFAULTS_CHECK_INTERVAL_SECONDS = 1.0


def _skill_build_defaults_file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def load_skill_build_defaults() -> dict[str, Any]:
    global _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE, _SKILL_BUILD_DEFAULTS_CACHE
    global _SKILL_BUILD_DEFAULTS_CHECKED_AT
    checked_at = time.monotonic()
    if (
        _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE is not None
        and checked_at - _SKILL_BUILD_DEFAULTS_CHECKED_AT
        < _SKILL_BUILD_DEFAULTS_CHECK_INTERVAL_SECONDS
    ):
        return _SKILL_BUILD_DEFAULTS_CACHE
    signature = (
        _skill_build_defaults_file_signature(BUILTIN_SKILL_BUILD_DEFAULTS_PATH),
        _skill_build_defaults_file_signature(SKILL_BUILD_DEFAULTS_PATH),
    )
    _SKILL_BUILD_DEFAULTS_CHECKED_AT = checked_at
    if signature == _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE:
        return _SKILL_BUILD_DEFAULTS_CACHE
    defaults = load_builtin_skill_build_defaults()
    defaults.update(load_user_skill_build_defaults())
    _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE = signature
    _SKILL_BUILD_DEFAULTS_CACHE = defaults
    return _SKILL_BUILD_DEFAULTS_CACHE


def save_skill_build_defaults(data: dict[str, Any]) -> None:
    global _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE, _SKILL_BUILD_DEFAULTS_CHECKED_AT
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_BUILD_DEFAULTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE = None
    _SKILL_BUILD_DEFAULTS_CHECKED_AT = 0.0


def skill_build_schemes_for_job(data: dict[str, Any], job: str) -> dict[str, dict[str, Any]]:
    normalized_job = norm(job)
    result: dict[str, dict[str, Any]] = {}
    default_payload = data.get(normalized_job, {})
    if isinstance(default_payload, dict):
        default_scheme_name = norm(default_payload.get("scheme_name"))
        if default_scheme_name:
            result[default_scheme_name] = default_payload
    schemes_root = data.get(SKILL_BUILD_SCHEMES_KEY, {})
    if not isinstance(schemes_root, dict):
        return result
    schemes = schemes_root.get(normalized_job, {})
    if not isinstance(schemes, dict):
        return result
    result.update(
        {
            norm(name): payload
            for name, payload in schemes.items()
            if norm(name) and isinstance(payload, dict)
        }
    )
    return result


def app_button(text: str, primary: bool = False) -> QPushButton:
    if FLUENT_AVAILABLE:
        button_class = PrimaryPushButton if primary else PushButton
        return button_class(text)
    return QPushButton(text)


def is_percent_attr(attr: str) -> bool:
    return attr.endswith("%")


def number_text(value: float, integer: bool = False) -> str:
    if abs(value) < 1e-9:
        return ""
    if integer:
        return f"{trunc(value):,}"
    if abs(value - int(value)) < 1e-9:
        return f"{int(value):,}"
    return f"{value:,.2f}"


def attr_number_text(attr: str, value: float) -> str:
    return number_text(value, integer=not is_percent_attr(attr))


def attr_summary(attrs: dict[str, float], limit: int = 6) -> str:
    parts = []
    used = set()
    hidden_attrs = {"MP恢复", "MP恢复%"}
    for attr in DISPLAY_ATTRS:
        if attr in hidden_attrs:
            continue
        value = display_value(attrs, attr)
        if abs(value) > 1e-9:
            sign = "+" if value > 0 else ""
            parts.append(f"{attr}{sign}{attr_number_text(attr, value)}")
            used.add(attr)
    for attr, value in attrs.items():
        if norm(attr).startswith("__") or attr in used or attr in hidden_attrs or attr in {"最小物攻", "最大物攻", "最小魔攻", "最大魔攻", "最小物攻%", "最大物攻%", "最小魔攻%", "最大魔攻%"}:
            continue
        if abs(value) > 1e-9:
            sign = "+" if value > 0 else ""
            parts.append(f"{attr}{sign}{attr_number_text(attr, value)}")
    return "；".join(parts[:limit]) + ("..." if len(parts) > limit else "")


def display_value(source: dict[str, float], attr: str) -> float:
    if attr == "物攻":
        return (source.get("最小物攻", 0.0) + source.get("最大物攻", 0.0)) / 2
    if attr == "魔攻":
        return (source.get("最小魔攻", 0.0) + source.get("最大魔攻", 0.0)) / 2
    if attr == "物攻%":
        vals = [source.get("最小物攻%", 0.0), source.get("最大物攻%", 0.0)]
        vals = [v for v in vals if abs(v) > 1e-9]
        return source.get(attr, 0.0) + (sum(vals) / len(vals) if vals else 0.0)
    if attr == "魔攻%":
        vals = [source.get("最小魔攻%", 0.0), source.get("最大魔攻%", 0.0)]
        vals = [v for v in vals if abs(v) > 1e-9]
        return source.get(attr, 0.0) + (sum(vals) / len(vals) if vals else 0.0)
    return source.get(attr, 0.0)


def text_units(text: Any) -> int:
    return sum(2 if east_asian_width(ch) in {"W", "F"} else 1 for ch in str(text))


def option_width(values: list[Any], min_width: int = 80, max_width: int = 260) -> int:
    units = max([text_units(v) for v in values if v not in (None, "")] + [0])
    return max(min_width, min(max_width, units * 8 + 42))


class Combo(QComboBox):
    def __init__(
        self,
        values: list[Any],
        current: Any = "",
        on_change=None,
        min_width: int = 170,
        align: Qt.AlignmentFlag = Qt.AlignLeft,
        contents_length: int = 10,
        object_name: str = "",
    ):
        super().__init__()
        if object_name:
            self.setObjectName(object_name)
        self.setEditable(True)
        self.setFrame(False)
        self.setMinimumWidth(min_width)
        self.setFixedHeight(EDIT_ROW_HEIGHT)
        self.setMinimumContentsLength(contents_length)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.addItems([str(v) for v in values if v not in (None, "")])
        text = "" if current is None else str(current)
        if text and self.findText(text) < 0:
            self.insertItem(0, text)
        self.setCurrentText(text)
        popup_width = option_width([*values, text], min_width, 520)
        self.view().setMinimumWidth(popup_width)
        self.setToolTip(text)
        self.currentTextChanged.connect(self.setToolTip)
        if self.lineEdit():
            self.lineEdit().setAlignment(align | Qt.AlignVCenter)
            self._show_text_start()
        self.currentTextChanged.connect(lambda _text: QTimer.singleShot(0, self._show_text_start))
        if on_change:
            self.currentTextChanged.connect(on_change)

    def _show_text_start(self) -> None:
        editor = self.lineEdit()
        if editor:
            editor.setCursorPosition(0)

    def wheelEvent(self, event) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class EngravingValueSlider(QWidget):
    def __init__(self, values: list[Any], current: Any = "", on_change=None):
        super().__init__()
        self.values: list[str] = []
        self.label = QLabel("无")
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.label.setMinimumWidth(42)
        self.slider = ClickableSlider(Qt.Horizontal)
        self.slider.setFixedHeight(30)
        self.slider.setToolTip("左侧为低值，右侧为高值")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)
        self.setFixedHeight(30)
        self.set_values(values, current)
        self.slider.valueChanged.connect(lambda _value: self._sync_label())
        if on_change:
            self.slider.valueChanged.connect(lambda _value: on_change())

    def set_values(self, values: list[Any], current: Any = "") -> None:
        parsed = [str(value) for value in values if norm(value) and norm(value) != "无"]
        parsed.sort(key=self._option_value)
        self.values = parsed
        self.slider.blockSignals(True)
        self.slider.setEnabled(bool(self.values))
        self.slider.setRange(0, max(len(self.values) - 1, 0))
        self.slider.setValue(self._find_index(current))
        self._sync_label()
        self.slider.blockSignals(False)

    def currentText(self) -> str:
        if not self.values:
            return "无"
        return self.values[self.slider.value()]

    def setCurrentText(self, text: Any) -> None:
        self.slider.setValue(self._find_index(text))
        self._sync_label()

    def _find_index(self, text: Any) -> int:
        current = norm(text)
        if current in self.values:
            return self.values.index(current)
        current_value = self._option_value(current)
        for index, option in enumerate(self.values):
            if abs(self._option_value(option) - current_value) < 1e-9:
                return index
        return 0

    def _sync_label(self) -> None:
        self.label.setText(self.currentText())
        self.label.setToolTip(self.currentText())

    @staticmethod
    def _option_value(text: Any) -> float:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", norm(text))
        return float(match.group(1)) if match else as_number(text)


class ClickableSlider(QSlider):
    def mousePressEvent(self, event) -> None:
        if self.orientation() == Qt.Horizontal and self.maximum() > self.minimum():
            pos = event.position().x() if hasattr(event, "position") else event.x()
            ratio = max(0.0, min(1.0, pos / max(1, self.width())))
            value = round(self.minimum() + ratio * (self.maximum() - self.minimum()))
            self.setValue(value)
            event.accept()
            return
        super().mousePressEvent(event)


class TierChoice(QWidget):
    def __init__(self, current: Any = "低档", on_change=None):
        super().__init__()
        self.low = QRadioButton("低")
        self.high = QRadioButton("高")
        self.group = QButtonGroup(self)
        self.group.addButton(self.low)
        self.group.addButton(self.high)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(6)
        layout.addWidget(self.low)
        layout.addWidget(self.high)
        self.setFixedHeight(30)
        self.setCurrentText(current)
        if on_change:
            self.low.toggled.connect(lambda _checked: on_change())
            self.high.toggled.connect(lambda _checked: on_change())

    def currentText(self) -> str:
        return "高档" if self.high.isChecked() else "低档"

    def setCurrentText(self, text: Any) -> None:
        if "高" in norm(text):
            self.high.setChecked(True)
        else:
            self.low.setChecked(True)


class _ExtremeTimelineProxy(PureTimelineRuntime):
    """Compatibility constructor for the immutable data-only timeline runtime."""

    def __init__(
        self,
        owner: "BuildEditor",
        build: dict[str, Any],
        effects: list[dict[str, Any]],
        runtime_values: dict[str, Any] | None = None,
        extreme_mode: bool = True,
    ):
        planner_defaults = {
            "_skill_burst_disable_new_scheme_1": False,
            "_skill_burst_disable_team_objective_opening_order": False,
            "_skill_burst_disable_team_objective_pair_order": False,
            "_skill_burst_disable_joint_support_order": False,
            "_skill_burst_force_new_scheme_1": None,
            "_skill_burst_new_scheme_1_override_ratio": 1.08,
            "_skill_burst_new_scheme_1_ready_debt_weight": 0.60,
            "_skill_burst_new_scheme_1_ready_debt_dpa_floor": 0.20,
            "_skill_burst_new_scheme_1_ready_debt_cycle_floor": 0.50,
            "_skill_burst_new_scheme_1_ambiguity_ratio": 0.90,
            "_skill_burst_new_scheme_1_trace": None,
            "_skill_burst_new_scheme_1_trace_support_skills": set(),
            "_skill_burst_collect_baseline_diagnostics": False,
            "_skill_burst_enable_new_scheme_1_multistart": True,
            "_skill_burst_enable_new_scheme_1_quality_floor": False,
            "_skill_burst_disable_hybrid_optimizer": False,
            "_skill_burst_disable_local_optimizer": False,
            "_skill_burst_disable_conservative_1_6": False,
            "_skill_burst_disable_early_summon_refresh": False,
            "_skill_burst_disable_short_personal_burst_scheduler": False,
            "_skill_burst_disable_opening_periodic_optimizer": False,
            "_skill_burst_force_conservative_1_5": None,
            "_skill_burst_force_conservative_1_6": None,
            "_skill_burst_complete_default_rotation": False,
            "_skill_burst_allowed_skill_keys": None,
            "_skill_burst_blocked_intervals": [],
            "_skill_burst_player_buff_clear_times_override": (),
            "_skill_burst_summon_removal_schedule_override": (),
            "_skill_burst_external_team_effect_windows": [],
            "_skill_burst_current_provider_override": "",
            "_skill_burst_team_base_replacement_multiplier": 1.0,
            "_skill_burst_mode_override": None,
            "_skill_burst_schedule_override": None,
            "_skill_burst_ready_deadlines_by_job": SKILL_BURST_READY_DEADLINES_BY_JOB,
        }
        captured_values = {
            name: copy.deepcopy(getattr(owner, name, default))
            for name, default in planner_defaults.items()
        }
        if runtime_values:
            captured_values.update(copy.deepcopy(runtime_values))
        captured_values["_timeline_extreme_mode"] = bool(extreme_mode)
        snapshot = TimelineSimulationSnapshot.create(build, effects, captured_values)
        super().__init__(
            snapshot,
            store=owner.store,
            calculator=owner.calculator,
            cooldown_engine=owner.skill_cooldown_engine,
            method_source_type=getattr(owner, "_timeline_method_source_type", type(owner)),
            level_cap=SKILL_BUILD_LEVEL_CAP,
        )

    def _selected_skill_mastery_ids(self) -> set[str]:
        saved = self.build.get("skill_mastery_build", {})
        levels = saved.get("levels", {}) if isinstance(saved, dict) else {}
        return {
            norm(skill_id)
            for skill_id, level in levels.items()
            if norm(skill_id) and as_number(level) > 0
        }
