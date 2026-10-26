from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from html import unescape

from .effect_stacking import SKILL_PERCENT_AMPLIFIED_FLAT_ATTACK_KEYS


def app_base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


DATA_DIR = app_base_dir() / "engine" / "data"
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_PATH = DATA_DIR / "dragon_nest_data.json"
MODULAR_DIR = DATA_DIR / "modular"
MODULAR_MANIFEST = MODULAR_DIR / "manifest.json"
TALISMAN_SLOT_SPECS = (
    ("200%槽位1", 200),
    ("200%槽位2", 200),
    ("175%槽位1", 175),
    ("175%槽位2", 175),
    ("125%槽位1", 125),
    ("125%槽位2", 125),
    ("100%槽位1", 100),
    ("100%槽位2", 100),
)
TALISMAN_SLOTS = tuple(slot for slot, _percent in TALISMAN_SLOT_SPECS)
TALISMAN_SLOT_PERCENT = {
    **dict(TALISMAN_SLOT_SPECS),
    # Compatibility for V4.523 builds imported before the slot correction.
    "150%槽位1": 175,
    "150%槽位2": 175,
}


def norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def clean_html_text(value: Any) -> str:
    text = re.sub(r"<br\s*/?>", "\n", norm(value), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def as_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    # Most values inside a generated timeline are already Python floats.  A
    # second ``float`` conversion is semantically identical but sits on the
    # hottest path of every candidate comparison. Test the native numeric
    # cases before the empty-string comparison as well: long scoring axes call
    # this tens of millions of times with floats, where ``value == ''`` can
    # never be true.
    if type(value) is float:
        return value
    if type(value) is int:
        return float(value)
    if value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = norm(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def with_none(values: Iterable[Any]) -> list[str]:
    vals = [value for value in unique(values) if value != "无"]
    return ["无"] + vals


def display_option(value: Any) -> str:
    text = norm(value)
    return {
        "最大物攻": "物攻",
        "最大魔攻": "魔攻",
    }.get(text, text)


def format_option_number(value: Any) -> str:
    number = as_number(value, default=float("nan"))
    if number != number:
        return norm(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def format_percent_option(grade: Any, value: Any) -> str:
    number = as_number(value)
    text = str(int(number)) if number.is_integer() else f"{number:g}"
    label = norm(grade)
    return f"{label} {text}%" if label else f"{text}%"


VARIANT_CODE_TO_LABEL = {"1": "力敏", "2": "敏智", "3": "智体"}
VARIANT_LABEL_TO_CODE = {label: code for code, label in VARIANT_CODE_TO_LABEL.items()}
ELEMENT_CODE_TO_NAME = {-1: "", 0: "火", 1: "水", 2: "光", 3: "暗"}
SKILL_DAMAGE_EX_ALLOWLIST = {
    "神圣惩戒EX",
    "银色箭雨EX",
    "箭矢风暴EX",
    "银色飞弹EX",
    "螺旋飓风EX",
}
SKILL_DAMAGE_EX_MERGE_BY_JOB = {
    "十字军": {"雷电冲击EX": "雷电冲击", "雷电冲击 EX": "雷电冲击"},
    "银色猎人": {
        "银色箭雨EX": "银色箭雨",
        "箭矢风暴EX": "箭矢风暴",
        "银色飞弹EX": "银色飞弹",
        "螺旋飓风EX": "螺旋飓风",
    },
}
PANEL_GLYPH_EXTRA_ATTRS = {
    "HP",
    "MP",
    "体质",
    "力量",
    "敏捷",
    "智力",
    "物攻",
    "魔攻",
    "最终",
    "防御",
    "魔防",
    "致命",
    "致命伤害",
}
# These cards are available again in the current game version.
REMOVED_CARD_NAMES: set[str] = set()


def buff_key(row: dict[str, Any]) -> str:
    return norm(row.get("选择键")) or "|".join(
        part for part in (norm(row.get("类别")), norm(row.get("来源")), norm(row.get("技能"))) if part
    )


def buff_label(row: dict[str, Any]) -> str:
    key = buff_key(row)
    if key:
        return key.replace("|", "｜")
    return norm(row.get("技能"))


def buff_display_label(row: dict[str, Any]) -> str:
    category = norm(row.get("类别"))
    source = norm(row.get("来源"))
    skill = norm(row.get("技能"))
    if source and source != "通用":
        return "｜".join(part for part in (source, category, skill) if part)
    return "｜".join(part for part in (category, skill) if part) or buff_label(row)


def buff_aliases(row: dict[str, Any]) -> set[str]:
    aliases = {buff_key(row), buff_label(row), buff_display_label(row), norm(row.get("技能"))}
    category = norm(row.get("类别"))
    source = norm(row.get("来源"))
    skill = norm(row.get("技能"))
    if source and source != "通用" and skill:
        aliases.add("｜".join(part for part in (source, skill) if part))
    return {alias for alias in aliases if alias}


BUFF_EXCLUSIVE_SKILL_GROUPS = (
    ("弗里兹科之舞", "幻影之舞"),
    ("十字弓掌握", "长弓掌握"),
    ("火焰光环", "寒冰光环"),
)


def buff_exclusive_group(row: dict[str, Any]) -> str:
    source = norm(row.get("来源"))
    skill = norm(row.get("技能"))
    if skill in {'战吼', '战吼（力量掌握）'}:
        return '团队|战吼'
    for group in BUFF_EXCLUSIVE_SKILL_GROUPS:
        if skill in group:
            return "|".join([source, *group])
    return ""


class DataStore:
    def __init__(self, data: dict[str, Any]):
        self.raw = data
        self.tables: dict[str, list[dict[str, Any]]] = data["tables"]
        self.lists: dict[str, Any] = data["lists"]
        self.attribute_order: list[str] = data["attribute_order"]
        self.defaults: dict[str, Any] = data["defaults"]
        self.tables["card_data"] = [
            row
            for row in self.tables.get("card_data", [])
            if norm(row.get("卡片名称")) not in REMOVED_CARD_NAMES
        ]
        self.tables["card_lookup"] = [
            row
            for row in self.tables.get("card_lookup", [])
            if norm(row.get("key")).split("|", 1)[0] not in REMOVED_CARD_NAMES
        ]
        for build in self.defaults.values():
            if not isinstance(build, dict):
                continue
            build["card_grades"] = [
                item
                for item in build.get("card_grades", [])
                if isinstance(item, dict)
                and norm(item.get("name")) not in REMOVED_CARD_NAMES
            ]
        self.skill_damage_dir = MODULAR_DIR / "tables" / "skills" / "skill_damage_tiny_jobs"
        self.skill_adjustments_path = MODULAR_DIR / "tables" / "skills" / "skill_sim_adjustments.json"
        self.skill_cooldown_rules_path = MODULAR_DIR / "tables" / "skills" / "skill_cooldown_rules.json"
        self.skill_cast_times_path = MODULAR_DIR / "tables" / "skills" / "skill_cast_times.json"
        self.skill_glyphs_path = MODULAR_DIR / "tables" / "skills" / "skill_glyphs.json"
        self.buff_activation_rules_path = MODULAR_DIR / "tables" / "skills" / "buff_activation_rules.json"
        self.card_levels_path = MODULAR_DIR / "tables" / "card_levels.json"
        self.team_extreme_coefficients_path = MODULAR_DIR / "tables" / "team_extreme_coefficients_lv70.json"
        self.full_skill_dir = MODULAR_DIR / "tables" / "skills" / "dngamer_skill_sim_reborn_split"
        self._skill_damage_index: dict[str, Any] | None = None
        self._skill_damage_cache: dict[str, dict[str, Any]] = {}
        self._skill_damage_entries_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._skill_damage_entry_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        self._skill_damage_element_cache: dict[tuple[str, str], str] = {}
        self._skill_damage_allowlist: dict[str, set[str]] | None = None
        self._skill_adjustments: dict[str, dict[str, dict[str, Any]]] | None = None
        self._skill_cooldown_rules: dict[str, Any] | None = None
        self._skill_cast_times: dict[str, Any] | None = None
        self._skill_glyphs: list[dict[str, Any]] | None = None
        self._buff_activation_rules: dict[str, Any] | None = None
        self._card_levels: dict[str, int] | None = None
        self._team_extreme_coefficients: dict[str, float] | None = None
        self._full_skill_index: dict[str, Any] | None = None
        self._full_skill_cache: dict[str, dict[str, Any]] = {}
        self._skill_build_tree_cache: dict[tuple[str, float, str], dict[str, Any]] = {}
        self._skill_effect_timing_cache: dict[tuple[str, str, str, float], dict[str, Any]] = {}
        self._suffix_names_cache: dict[tuple[str, str, str], tuple[str, ...]] = {}
        self._suffix_rows_by_equipment: dict[str, tuple[tuple[str, str], ...]] | None = None

    def buff_activation_rules(self) -> dict[str, Any]:
        if self._buff_activation_rules is None:
            payload = load_json(self.buff_activation_rules_path) if self.buff_activation_rules_path.exists() else {}
            self._buff_activation_rules = payload if isinstance(payload, dict) else {}
        return self._buff_activation_rules

    def buff_activation_rule(
        self,
        source: Any,
        skill: Any,
        category: Any = "个人buff",
    ) -> dict[str, Any]:
        section = "team_buffs" if norm(category) == "团队buff" else "personal_buffs"
        rules = self.buff_activation_rules().get(section, {})
        if not isinstance(rules, dict):
            return {}
        key = "|".join(part for part in (norm(source), norm(skill)) if part)
        rule = rules.get(key, {})
        return dict(rule) if isinstance(rule, dict) else {}

    def buff_activation_rule_for_job(
        self,
        second_job: Any,
        skill: Any,
        category: Any = "个人buff",
        first_job: Any = "",
    ) -> dict[str, Any]:
        """Resolve a rule declared by either a second job or its first job."""

        for source in self.job_buff_sources(second_job, first_job):
            rule = self.buff_activation_rule(source, skill, category)
            if rule:
                return rule
        return self.buff_activation_rule("通用", skill, category)

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "DataStore":
        data = load_modular_data()
        if data is None:
            if DATA_PATH.exists():
                data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            else:
                raise FileNotFoundError(f"模块化数据不完整，且不存在旧总表：{MODULAR_DIR}")
        return cls(data)

    def first_jobs(self) -> list[str]:
        jobs = unique(self.lists.get("first_jobs", []))
        family_order: dict[str, int] = {}
        branch_order: dict[str, tuple[int, int]] = {}
        for row in self.tables.get("job_map", []):
            family = norm(row.get("转换大职业"))
            branch = norm(row.get("一转分支") or row.get("一转职业"))
            if not family or not branch:
                continue
            family_index = family_order.setdefault(family, len(family_order))
            branch_order.setdefault(branch, (family_index, len(branch_order)))
        original_order = {norm(job): index for index, job in enumerate(jobs)}
        return sorted(
            jobs,
            key=lambda job: (*branch_order.get(norm(job), (999, 999)), original_order.get(norm(job), 999)),
        )

    def second_jobs(self, first_job: str) -> list[str]:
        rows = self.tables["character"]
        return unique(row.get("二转职业") for row in rows if norm(row.get("一转职业")) == norm(first_job))

    def job_family(self, second_job: str, fallback: str = "") -> str:
        for row in self.tables["job_map"]:
            if norm(row.get("二转职业")) == norm(second_job):
                return norm(row.get("转换大职业")) or fallback
        return fallback

    def job_buff_sources(self, second_job: Any, first_job: Any = "") -> list[str]:
        """Return the ordered job sources that may own this build's effects."""

        second = norm(second_job)
        first = norm(first_job)
        if not first and second:
            first = next(
                (
                    norm(row.get("一转职业"))
                    for row in self.tables.get("character", [])
                    if norm(row.get("二转职业")) == second
                    and norm(row.get("一转职业"))
                ),
                "",
            )
        return unique(source for source in (second, first) if source)

    def character_base(self, second_job: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in self.tables["character"]:
            if norm(row.get("二转职业")) == norm(second_job):
                out[norm(row.get("属性"))] = as_number(row.get("数值"))
        return out

    def team_extreme_coefficients(self) -> dict[str, float]:
        if self._team_extreme_coefficients is not None:
            return self._team_extreme_coefficients
        out: dict[str, float] = {}
        if self.team_extreme_coefficients_path.exists():
            payload = load_json(self.team_extreme_coefficients_path)
            coefficients = payload.get("coefficients", payload) if isinstance(payload, dict) else {}
            if isinstance(coefficients, dict):
                out = {
                    norm(job): as_number(value)
                    for job, value in coefficients.items()
                    if norm(job) and as_number(value) > 0
                }
        self._team_extreme_coefficients = out
        return out

    def equipment_names(self, first_job: str, second_job: str, slot: str) -> list[str]:
        family = self.job_family(second_job, first_job)
        jobs = {norm(first_job), norm(second_job), norm(family)}
        slot_name = norm(slot)
        return ["无"] + unique(
            row.get("装备名称")
            for row in self.tables["base"]
            if norm(row.get("部位")).startswith(slot_name) and norm(row.get("职业")) in jobs
        )

    def preferred_equipment_name(self, level: Any, job: Any, slot: Any) -> str:
        level_name = norm(level)
        job_name = norm(job)
        slot_name = norm(slot)
        names = unique(
            row.get("装备名称")
            for row in self.tables["base"]
            if norm(row.get("等级")) == level_name
            and norm(row.get("职业")) == job_name
            and norm(row.get("部位")).startswith(slot_name)
        )
        if slot_name == "主手":
            mage_jobs = {"法师"}
            for row in self.tables.get("job_map", []):
                if norm(row.get("转换大职业")) != "法师":
                    continue
                mage_jobs.update(
                    {
                        norm(row.get("一转分支") or row.get("一转职业")),
                        norm(row.get("二转职业")),
                    }
                )
            if job_name in mage_jobs:
                orb = next((name for name in names if "宝珠" in name), "")
                if orb:
                    return orb
        return names[0] if names else "无"

    def suffix_names(self, first_job: str, second_job: str, name: str) -> list[str]:
        cache_key = (norm(first_job), norm(second_job), norm(name))
        cached = self._suffix_names_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        if self._suffix_rows_by_equipment is None:
            indexed: dict[str, list[tuple[str, str]]] = {}
            for row in self.tables["suffix"]:
                equipment_name = norm(row.get("装备名称"))
                if not equipment_name:
                    continue
                indexed.setdefault(equipment_name, []).append((
                    norm(row.get("职业")),
                    norm(row.get("后缀名称")),
                ))
            self._suffix_rows_by_equipment = {
                equipment_name: tuple(rows)
                for equipment_name, rows in indexed.items()
            }
        family = self.job_family(second_job, first_job)
        jobs = {norm(first_job), norm(second_job), norm(family)}
        vals = unique(
            suffix
            for job, suffix in self._suffix_rows_by_equipment.get(norm(name), ())
            if job in jobs
        )
        result = tuple(["无"] + vals)
        self._suffix_names_cache[cache_key] = result
        return list(result)

    def enchant_names(self, attack: bool | None = None) -> list[str]:
        rows = self.tables["enchant"]
        if attack is True:
            rows = [row for row in rows if "atk" in norm(row.get("附魔类型")).lower() or "base" in norm(row.get("附魔类型")).lower()]
        elif attack is False:
            rows = [row for row in rows if "def" in norm(row.get("附魔类型")).lower() or "base" in norm(row.get("附魔类型")).lower()]
        return ["无"] + unique(row.get("附魔名称") for row in rows)

    def jewelry_names(self, slot: str) -> list[str]:
        return ["无"] + unique(row.get("饰品名称") for row in self.tables["jewelry"] if norm(row.get("饰品类别")) == norm(slot))

    def jewelry_variants(self, slot: str, name: str) -> list[str]:
        codes = unique(
            row.get("变体")
            for row in self.tables["jewelry"]
            if norm(row.get("饰品类别")) == norm(slot) and norm(row.get("饰品名称")) == norm(name)
        )
        return ["无"] + [VARIANT_CODE_TO_LABEL.get(code, code) for code in codes]

    def jewelry_variant_code(self, value: str) -> str:
        text = norm(value)
        return VARIANT_LABEL_TO_CODE.get(text, text)

    def jewelry_variant_label(self, value: str) -> str:
        text = norm(value)
        return VARIANT_CODE_TO_LABEL.get(text, text)

    def glyph_a_levels(self) -> list[str]:
        return with_none(self.lists.get("glyph_a_levels", []))

    def glyph_a_names(self) -> list[str]:
        return with_none(self.lists.get("glyph_a_names", []))

    def glyph_a_extra(self) -> list[str]:
        return with_none(self._glyph_extra_options("glyph_a_extra", "额外属性"))

    def glyph_s_levels(self) -> list[str]:
        return ["无"] + unique(self.lists.get("glyph_s_levels", []))

    def glyph_s_names(self) -> list[str]:
        return ["无"] + unique(self.lists.get("glyph_s_names", []))

    def glyph_s_base_options(self, name: str) -> list[str]:
        return ["无"] + unique(self.lists.get("s_base_options", {}).get(norm(name), []))

    def glyph_s_extra_options(self, name: str = "") -> list[str]:
        target_name = norm(name)
        rows = self.tables.get("glyph_s_extra", [])
        if target_name:
            rows = [row for row in rows if norm(row.get("纹章名称")) == target_name]
        return with_none(self._glyph_extra_options_from_rows(rows, "选择额外属性"))

    def _glyph_extra_options(self, table_name: str, option_key: str) -> list[str]:
        return self._glyph_extra_options_from_rows(self.tables.get(table_name, []), option_key)

    @staticmethod
    def _glyph_extra_options_from_rows(rows: list[dict[str, Any]], option_key: str) -> list[str]:
        values: list[str] = []
        for row in rows:
            option = display_option(row.get(option_key))
            attr = display_option(row.get("属性"))
            if option in PANEL_GLYPH_EXTRA_ATTRS or attr in PANEL_GLYPH_EXTRA_ATTRS:
                values.append(option)
        return unique(values)

    def glyph_s_ranks(self) -> list[str]:
        return unique(self.lists.get("glyph_s_values", []))

    def rune_levels(self) -> list[str]:
        return ["无"] + unique(self.lists.get("rune_levels", []))

    def rune_attrs(self, rune_type: str) -> list[str]:
        key = "rune_attack_attrs" if norm(rune_type) == "攻击" else "rune_defense_attrs"
        return ["无"] + unique(self.lists.get(key, []))

    def rune_ranks(self) -> list[str]:
        return ["无"] + unique(self.lists.get("rune_ranks", []))

    def core_levels(self) -> list[str]:
        return unique(self.lists.get("core_levels", []))

    def core_conversions(self) -> list[str]:
        return ["无"] + unique(self.lists.get("core_minus_attrs", []))

    def core_ratio_ranks(self, conversion: str = "") -> list[str]:
        conversion_text = norm(conversion)
        if conversion_text == "无":
            return ["无"]
        values = unique(
            row.get("转换比例")
            for row in self.tables["rune"]
            if norm(row.get("数据类型")) == "特殊"
            and (not conversion_text or norm(row.get("石板类型/转换")) == conversion_text)
        )
        return sorted([format_option_number(value) for value in values], key=lambda value: as_number(value))

    def core_coeff_ranks(self, conversion: str = "") -> list[str]:
        conversion_text = norm(conversion)
        if conversion_text == "无":
            return ["无"]
        values = unique(
            row.get("转换系数")
            for row in self.tables["rune"]
            if norm(row.get("数据类型")) == "特殊"
            and (not conversion_text or norm(row.get("石板类型/转换")) == conversion_text)
        )
        return sorted([format_option_number(value) for value in values], key=lambda value: as_number(value))

    def titles(self) -> list[str]:
        return unique(self.lists.get("titles", []))

    def fashion_options(self, slot: str) -> list[str]:
        slot_key = norm(slot)
        if slot_key.startswith("戒指"):
            slot_key = "戒指"
        return unique(["无", *self.lists.get("fashion", {}).get(slot_key, [])])

    def talisman_grades(self) -> list[str]:
        return ["无", *unique(
            row.get("品质")
            for row in self.tables.get("talisman", [])
            if norm(row.get("类型")) == "主属性"
        )]

    def talisman_names(self, grade: Any) -> list[str]:
        grade_name = norm(grade)
        if grade_name in {"", "无"}:
            return ["无"]
        return ["无", *unique(
            row.get("护符")
            for row in self.tables.get("talisman", [])
            if norm(row.get("类型")) == "主属性"
            and norm(row.get("品质")) == grade_name
        )]

    def talisman_extra_options(self, grade: Any, name: Any) -> list[str]:
        grade_name = norm(grade)
        talisman_name = norm(name)
        if grade_name != "B" or talisman_name in {
            "", "无", "次级最终的强化护符", "中级最终的强化护符",
        }:
            return ["无"]
        main_attrs = {
            norm(row.get("属性"))
            for row in self.tables.get("talisman", [])
            if norm(row.get("类型")) == "主属性"
            and norm(row.get("品质")) == grade_name
            and norm(row.get("护符")) == talisman_name
        }
        if not main_attrs:
            return ["无"]
        return ["无", *unique(
            row.get("额外属性")
            for row in self.tables.get("talisman", [])
            if norm(row.get("类型")) == "额外属性"
            and norm(row.get("品质")) == grade_name
            and norm(row.get("属性")) not in main_attrs
        )]

    def engraving_main_options(self, kind: str) -> list[str]:
        engraving = self.lists.get("engraving", {})
        values = engraving.get("main_values", {}).get(norm(kind), [])
        if isinstance(values, str) and values.startswith("$ref:"):
            values = engraving.get("main_values", {}).get(values.split(":", 1)[1], [])
        return ["无"] + [format_percent_option(item.get("grade"), item.get("value")) for item in values if isinstance(item, dict)]

    def engraving_grades(self) -> list[str]:
        engraving = self.lists.get("engraving", {})
        return ["无"] + unique(engraving.get("main_probabilities", {}).keys())

    def engraving_main_value_options(self, kind: str, grade: str) -> list[str]:
        engraving = self.lists.get("engraving", {})
        values = engraving.get("main_values", {}).get(norm(kind), [])
        if isinstance(values, str) and values.startswith("$ref:"):
            values = engraving.get("main_values", {}).get(values.split(":", 1)[1], [])
        selected_grade = norm(grade)
        return ["无"] + [
            format_percent_option("", item.get("value"))
            for item in values
            if isinstance(item, dict) and norm(item.get("grade")) == selected_grade
        ]

    def engraving_secondary_value_options(self, kind: str, grade: str) -> list[str]:
        """Return the secondary rolls aligned with each primary roll.

        Jewelry HP and three-piece critical engravings share the same attack
        roll table, but that table is not always one half of the primary
        value.  In particular, A-grade three-piece critical rolls advance by
        0.10 while their attack rolls advance by 0.02, so callers must match
        by roll position instead of deriving the value arithmetically.
        """
        secondary_kind = {
            "饰品生命": "饰品双攻",
            "三件套致命": "三件套双攻",
        }.get(norm(kind))
        if not secondary_kind:
            return ["无"]
        return self.engraving_main_value_options(secondary_kind, grade)

    def engraving_extra_options(self) -> list[str]:
        engraving = self.lists.get("engraving", {})
        return with_none(engraving.get("extra_values", {}).keys())

    def engraving_extra_rank_options(self, attr: str) -> list[str]:
        engraving = self.lists.get("engraving", {})
        group = engraving.get("extra_values", {}).get(norm(attr))
        rolls = engraving.get("extra_rolls", {}).get(group, {})
        return ["无"] + [format_percent_option(label, value) for label, value in rolls.items()]

    def engraving_extra_tiers(self) -> list[str]:
        return ["无", "低档", "高档"]

    def engraving_extra_value(self, attr: str, grade: str, tier: str) -> float:
        engraving = self.lists.get("engraving", {})
        group = engraving.get("extra_values", {}).get(norm(attr))
        rolls = engraving.get("extra_rolls", {}).get(group, {})
        tier_text = norm(tier)
        if tier_text not in {"低档", "高档"}:
            return 0.0
        return as_number(rolls.get(f"{norm(grade)}{tier_text}"))

    def skill_names(self, first_job: str = "", second_job: str = "") -> list[str]:
        first = norm(first_job)
        second = norm(second_job)
        jobs = {first, second, "通用"}
        rows = [
            row for row in self.tables["buff"]
            if norm(row.get("类别")) in {"团队buff", "公会buff"} or norm(row.get("来源")) in jobs
        ]
        def priority(row: dict[str, Any]) -> tuple[int, str, str]:
            source = norm(row.get("来源"))
            category = norm(row.get("类别"))
            if second and source == second:
                group = 0
            elif first and source == first:
                group = 1
            elif source == "通用":
                group = 2
            elif category in {"团队buff", "公会buff"}:
                group = 3
            else:
                group = 4
            return (group, category, norm(row.get("技能")))

        rows.sort(key=priority)
        return ["无"] + unique(buff_display_label(row) for row in rows)

    def skill_display_name(self, skill: str) -> str:
        key = norm(skill)
        if not key or key == "无":
            return "无"
        for row in self.tables["buff"]:
            if key in buff_aliases(row):
                return buff_display_label(row)
        return key

    def skill_levels(self, skill: str) -> list[str]:
        key = norm(skill)
        matching_keys = {
            buff_key(row)
            for row in self.tables["buff"]
            if key in buff_aliases(row)
        }
        if not matching_keys:
            matching_keys = {buff_key(row) for row in self.tables["buff"] if norm(row.get("技能")) == key}
        levels = unique(row.get("等级") for row in self.tables["buff"] if buff_key(row) in matching_keys)
        return levels or ["无"]

    def _load_full_skill_index(self) -> dict[str, Any]:
        if self._full_skill_index is not None:
            return self._full_skill_index
        path = self.full_skill_dir / "index.json"
        self._full_skill_index = load_json(path) if path.exists() else {"jobs": {}}
        return self._full_skill_index

    def _full_skill_job_meta(self, second_job: str) -> dict[str, Any] | None:
        job_name = norm(second_job)
        jobs = self._load_full_skill_index().get("jobs", {})
        if isinstance(jobs, dict):
            files = jobs.get("files")
            if isinstance(files, list):
                for row in files:
                    if isinstance(row, dict) and norm(row.get("name")) == job_name:
                        return row
            for row in jobs.values():
                if isinstance(row, dict) and norm(row.get("name")) == job_name:
                    return row
                if isinstance(row, list):
                    for item in row:
                        if isinstance(item, dict) and norm(item.get("name")) == job_name:
                            return item
        elif isinstance(jobs, list):
            for row in jobs:
                if isinstance(row, dict) and norm(row.get("name")) == job_name:
                    return row
        return None

    def full_skill_names(self, second_job: str) -> list[str]:
        meta = self._full_skill_job_meta(second_job)
        if not meta:
            return []
        filename = norm(meta.get("file"))
        if not filename:
            row_id = norm(meta.get("RowId")) or norm(meta.get("id"))
            filename = f"jobs/job_{row_id}.json" if row_id else ""
        if not filename:
            return []
        if filename not in self._full_skill_cache:
            path = self.full_skill_dir / filename
            if not path.exists():
                return []
            self._full_skill_cache[filename] = load_json(path)
        skills = self._full_skill_cache[filename].get("skills", {})
        if not isinstance(skills, dict):
            return []
        return unique(norm(item.get("name")) for item in skills.values() if isinstance(item, dict) and norm(item.get("name")))

    def _full_skill_job_data(self, second_job: str) -> dict[str, Any] | None:
        meta = self._full_skill_job_meta(second_job)
        if not meta:
            return None
        filename = norm(meta.get("file"))
        if not filename:
            row_id = norm(meta.get("RowId")) or norm(meta.get("id"))
            filename = f"jobs/job_{row_id}.json" if row_id else ""
        if not filename:
            return None
        if filename not in self._full_skill_cache:
            path = self.full_skill_dir / filename
            if not path.exists():
                return None
            self._full_skill_cache[filename] = load_json(path)
        return self._full_skill_cache[filename]

    def _full_skill_items(self, second_job: str) -> list[dict[str, Any]]:
        data = self._full_skill_job_data(second_job)
        if not data:
            return []
        skills = data.get("skills", {})
        return [item for item in skills.values() if isinstance(item, dict)] if isinstance(skills, dict) else []

    def skill_build_tree(self, second_job: str, max_required_level: float = 70, mode: str = "normal") -> dict[str, Any]:
        mode_key = "master" if norm(mode).lower() == "master" else "normal"
        cache_key = (norm(second_job), float(max_required_level), mode_key)
        cached = self._skill_build_tree_cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._full_skill_job_data(second_job)
        if not data:
            return {"total_sp": 0, "groups": [], "skills": {}}
        tree_key = "master_tree" if mode_key == "master" else "tree"
        tree = data.get(tree_key, {}) if isinstance(data.get(tree_key, {}), dict) else {}
        groups = tree.get("tree", []) if isinstance(tree.get("tree", []), list) else []
        pve = (((data.get("skill_levels", {}) or {}).get(mode_key) or {}).get("pve") or {})
        skill_lookup: dict[int, str] = {}
        for item in (data.get("skills", {}) or {}).values():
            if not isinstance(item, dict):
                continue
            name = norm(item.get("name"))
            for key in ("_SkillTableID", "_SkillID", "RowId"):
                value = int(as_number(item.get(key), 0))
                if value and name:
                    skill_lookup[value] = name
        out_groups: list[dict[str, Any]] = []
        all_skills: dict[str, dict[str, Any]] = {}
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            job = group.get("job", {}) if isinstance(group.get("job", {}), dict) else {}
            skills: list[dict[str, Any]] = []
            for raw_skill in group.get("skill", []) if isinstance(group.get("skill", []), list) else []:
                if not isinstance(raw_skill, dict):
                    continue
                skill_id = int(as_number(raw_skill.get("_SkillTableID") or raw_skill.get("_SkillID") or raw_skill.get("RowId")))
                if skill_id <= 0:
                    continue
                skill_key = str(skill_id)
                levels = pve.get(skill_key) or pve.get(str(raw_skill.get("_SkillID"))) or pve.get(str(raw_skill.get("RowId"))) or []
                level_rows: list[dict[str, Any]] = []
                if isinstance(levels, list):
                    for row in levels:
                        if not isinstance(row, dict):
                            continue
                        level = int(as_number(row.get("_SkillLevel"), len(level_rows) + 1))
                        required = as_number(row.get("_LevelLimit"), 9999)
                        if required > max_required_level:
                            continue
                        need_sp = row.get("_NeedSkillPoint")
                        if need_sp is None:
                            need_sp = row.get("_DecreaseSP")
                        level_rows.append({
                            "level": level,
                            "required_level": int(required),
                            "sp": int(as_number(need_sp)),
                            "cooldown": max(0.0, as_number(row.get("_DelayTime")) / 1000),
                            "desc": clean_html_text(row.get("desc")),
                        })
                    # Skill mastery represents the next skill level beyond the
                    # character-level cap. Keep that one row in the mastery
                    # tree even when its own required level is 71+ (for
                    # example Lv20 at character level 72). The normal tree
                    # remains strictly capped at max_required_level.
                    if mode_key == "master" and level_rows:
                        next_level = max(row["level"] for row in level_rows) + 1
                        extra = next(
                            (
                                row
                                for row in levels
                                if isinstance(row, dict)
                                and int(as_number(row.get("_SkillLevel"), -1)) == next_level
                            ),
                            None,
                        )
                        if isinstance(extra, dict):
                            need_sp = extra.get("_NeedSkillPoint")
                            if need_sp is None:
                                need_sp = extra.get("_DecreaseSP")
                            level_rows.append({
                                "level": next_level,
                                "required_level": int(as_number(extra.get("_LevelLimit"), 9999)),
                                "sp": int(as_number(need_sp)),
                                "cooldown": max(0.0, as_number(extra.get("_DelayTime")) / 1000),
                                "desc": clean_html_text(extra.get("desc")),
                            })
                max_level = min(
                    int(as_number(raw_skill.get("maxlevel"), len(level_rows))),
                    max([row["level"] for row in level_rows] + [0]),
                )
                parents: list[dict[str, Any]] = []
                for index in range(1, 4):
                    parent_id = int(as_number(raw_skill.get(f"_ParentSkillID{index}")))
                    need_level = int(as_number(raw_skill.get(f"_NeedParentSkillLevel{index}")))
                    if parent_id > 0 and need_level > 0:
                        parents.append({
                            "id": str(parent_id),
                            "name": skill_lookup.get(parent_id, str(parent_id)),
                            "level": need_level,
                        })
                item = {
                    "id": skill_key,
                    "name": norm(raw_skill.get("name")) or skill_key,
                    "group_index": group_index,
                    "slot": int(as_number(raw_skill.get("_TreeSlotIndex"), 0)),
                    "sort": int(as_number(raw_skill.get("_TreeSortIndex"), raw_skill.get("_TreeSlotIndex", 0))),
                    "icon": int(as_number(raw_skill.get("_IconImageIndex"))),
                    "skill_type": int(as_number(raw_skill.get("_SkillType"))),
                    "duration_type": int(as_number(raw_skill.get("_DurationType"))),
                    "target_type": int(as_number(raw_skill.get("_TargetType"))),
                    "element": int(as_number(raw_skill.get("_Element"), -1)),
                    "duplicated_type": int(as_number(raw_skill.get("_DuplicatedSkillType"))),
                    "global_group": int(as_number(raw_skill.get("_GlobalSkillGroup"))),
                    "max_level": 1 if mode_key == "master" else max_level,
                    "raw_max_level": max_level,
                    "levels": level_rows,
                    "parents": parents,
                    "need_basic_sp": int(as_number(raw_skill.get("_NeedBasicSP1"))),
                    "need_first_sp": int(as_number(raw_skill.get("_NeedFirstSP1"))),
                    "need_second_sp": int(as_number(raw_skill.get("_NeedSecondSP1"))),
                    "default": bool(raw_skill.get("default")),
                    "weapon1": norm(raw_skill.get("weapon1")),
                    "weapon2": norm(raw_skill.get("weapon2")),
                }
                skills.append(item)
                all_skills[skill_key] = item
            skills.sort(key=lambda item: (item.get("slot", 0), item.get("sort", 0), item.get("name", "")))
            out_groups.append({
                "job": {
                    "id": int(as_number(job.get("id"))),
                    "name": norm(job.get("name")) or f"职业树{group_index + 1}",
                    "sp": int(as_number(job.get("sp"))),
                    "index": int(as_number(job.get("index"), group_index)),
                },
                "skills": skills,
            })
        result = {"total_sp": int(as_number(tree.get("totalsp"))), "groups": out_groups, "skills": all_skills, "mode": mode_key}
        self._skill_build_tree_cache[cache_key] = result
        return result

    def skill_damage_description(self, second_job: str, skill: str, level_text: str) -> str:
        entry = self._skill_damage_entry(second_job, skill)
        adjustment = self.skill_damage_adjustment(second_job, skill)
        adjustment_desc = norm(adjustment.get("description"))
        if adjustment_desc:
            return adjustment_desc
        description_source = adjustment.get("description_source")
        if isinstance(description_source, dict):
            source_job = norm(description_source.get("job")) or norm(second_job)
            source_skill = norm(description_source.get("skill"))
            if source_skill and (source_job, source_skill) != (norm(second_job), norm(skill)):
                source_desc = self.skill_damage_description(source_job, source_skill, level_text)
                if source_desc:
                    return source_desc
        data = self._full_skill_job_data(second_job)
        if not entry or not data:
            return ""
        skill_id = int(as_number(entry.get("id"), -1))
        full_skill = None
        skills = data.get("skills", {})
        if isinstance(skills, dict):
            for item in skills.values():
                if not isinstance(item, dict):
                    continue
                if int(as_number(item.get("_SkillTableID"), -2)) == skill_id or int(as_number(item.get("_SkillID"), -2)) == skill_id:
                    full_skill = item
                    break
        if not full_skill:
            return ""
        level_groups = data.get("skill_levels", {})
        levels = (((level_groups.get("normal") or {}).get("pve") or {}).get(str(full_skill.get("_SkillTableID"))) or
                  ((level_groups.get("normal") or {}).get("pve") or {}).get(str(full_skill.get("_SkillID"))) or
                  ((level_groups.get("normal") or {}).get("pve") or {}).get(str(skill_id)) or [])
        if not isinstance(levels, list):
            return ""
        target = int(as_number(level_text, -1))
        candidates = [row for row in levels if int(as_number(row.get("_SkillLevel"), -2)) == target]
        if not candidates:
            candidates = [row for row in levels if as_number(row.get("_LevelLimit"), 9999) <= 80]
        for row in reversed(candidates):
            desc = clean_html_text(row.get("desc"))
            if desc and desc != "-2146826273":
                return desc
        return ""

    def full_skill_levels(self, second_job: str, skill: str, max_required_level: float = 70) -> list[dict[str, Any]]:
        skill_name = norm(skill)
        if not skill_name or skill_name == "无":
            return []
        data = self._full_skill_job_data(second_job)
        if not data:
            return []
        skills = data.get("skills", {})
        if not isinstance(skills, dict):
            return []
        for item in skills.values():
            if not isinstance(item, dict) or norm(item.get("name")) != skill_name:
                continue
            levels = self._full_skill_levels_for_item(data, item)
            valid = []
            for index, row in enumerate(levels, 1):
                desc = clean_html_text(row.get("desc"))
                if as_number(row.get("_LevelLimit"), 9999) <= max_required_level and desc not in {"", "-2146826273"}:
                    valid.append({**row, "_DisplayLevel": index})
            return valid
        return []

    @staticmethod
    def _full_skill_levels_for_item(data: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
        level_groups = data.get("skill_levels", {})
        pve = ((level_groups.get("normal") or {}).get("pve") or {})
        levels = (
            pve.get(str(item.get("_SkillTableID"))) or
            pve.get(str(item.get("_SkillID"))) or
            pve.get(str(item.get("RowId"))) or
            []
        )
        return levels if isinstance(levels, list) else []

    def skill_effect_timing(self, second_job: str, skill: str, level: Any = None, max_required_level: float = 70) -> dict[str, Any]:
        skill_name = norm(skill)
        if not skill_name or skill_name == "无":
            return {"cooldown": 0.0, "duration": 0.0, "desc": ""}
        cache_key = (norm(second_job), skill_name, norm(level), float(max_required_level))
        cached = self._skill_effect_timing_cache.get(cache_key)
        if cached is not None:
            return cached
        search_jobs = [norm(second_job)] if norm(second_job) else []
        jobs = self._load_full_skill_index().get("jobs", {})
        candidates: list[dict[str, Any]] = []
        if isinstance(jobs, dict):
            files = jobs.get("files")
            if isinstance(files, list):
                candidates.extend(row for row in files if isinstance(row, dict))
            for value in jobs.values():
                if isinstance(value, dict):
                    candidates.append(value)
                elif isinstance(value, list):
                    candidates.extend(row for row in value if isinstance(row, dict))
        elif isinstance(jobs, list):
            candidates.extend(row for row in jobs if isinstance(row, dict))
        for row in candidates:
            job_name = norm(row.get("job_name") or row.get("name"))
            if job_name and job_name not in search_jobs:
                search_jobs.append(job_name)
        for job_name in search_jobs:
            data = self._full_skill_job_data(job_name)
            if not data:
                continue
            skills = data.get("skills", {})
            if not isinstance(skills, dict):
                continue
            for item in skills.values():
                if not isinstance(item, dict) or norm(item.get("name")) != skill_name:
                    continue
                level_row = self._full_skill_level(data, item, level, max_required_level)
                if not level_row:
                    continue
                desc = clean_html_text(level_row.get("desc"))
                result = {
                    "cooldown": as_number(level_row.get("_DelayTime")) / 1000,
                    "duration": self._duration_from_desc(desc),
                    "desc": desc,
                    "level": level_row.get("_DisplayLevel") or level,
                    "required_level": as_number(level_row.get("_LevelLimit")),
                }
                self._skill_effect_timing_cache[cache_key] = result
                return result
        result = {"cooldown": 0.0, "duration": 0.0, "desc": ""}
        self._skill_effect_timing_cache[cache_key] = result
        return result

    def _full_skill_level(self, data: dict[str, Any], item: dict[str, Any], level: Any = None, max_required_level: float = 70) -> dict[str, Any] | None:
        levels = self._full_skill_levels_for_item(data, item)
        if not levels:
            return None
        valid = []
        for index, row in enumerate(levels, 1):
            desc = clean_html_text(row.get("desc"))
            if as_number(row.get("_LevelLimit"), 9999) <= max_required_level and desc not in {"", "-2146826273"}:
                valid.append({**row, "_DisplayLevel": index})
        if level not in (None, "", "无"):
            target = int(as_number(level))
            for row in valid:
                if int(as_number(row.get("_DisplayLevel"))) == target:
                    return row
        return (valid or levels or [None])[-1]

    @staticmethod
    def _best_full_skill_level(data: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
        levels = DataStore._full_skill_levels_for_item(data, item)
        if not levels:
            return None
        valid = [
            row for row in levels
            if as_number(row.get("_LevelLimit"), 9999) <= 80
            and clean_html_text(row.get("desc")) not in {"", "-2146826273"}
        ]
        return (valid or levels or [None])[-1]

    @staticmethod
    def _duration_from_desc(desc: str) -> float:
        text = norm(desc)
        num = r"([0-9]+(?:\.[0-9]+)?)(?:;[0-9]+-[0-9]+)?"
        patterns = [
            rf"持续时间\s*{num}\s*秒",
            rf"持续\s*{num}\s*秒",
            rf"{num}\s*秒钟",
            rf"{num}\s*秒内",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return as_number(match.group(1))
        return 0.0

    def skill_damage_element(self, second_job: str, skill: str) -> str:
        cache_key = (norm(second_job), norm(skill))
        cached = self._skill_damage_element_cache.get(cache_key)
        if cached is not None:
            return cached
        entry = self._skill_damage_entry(second_job, skill)
        if not entry:
            self._skill_damage_element_cache[cache_key] = ""
            return ""
        adjustment = self.skill_damage_adjustment(second_job, skill)
        adjusted_element = norm(adjustment.get("element") or adjustment.get("属性"))
        if adjusted_element in {"火", "水", "光", "暗"}:
            self._skill_damage_element_cache[cache_key] = adjusted_element
            return adjusted_element
        skill_id = int(as_number(entry.get("id"), -1))
        skill_name = norm(entry.get("name"))
        for item in self._full_skill_items(second_job):
            if int(as_number(item.get("_SkillTableID"), -2)) == skill_id or int(as_number(item.get("_SkillID"), -2)) == skill_id:
                result = ELEMENT_CODE_TO_NAME.get(int(as_number(item.get("_Element"), -1)), "")
                if result:
                    self._skill_damage_element_cache[cache_key] = result
                    return result
        for item in self._full_skill_items(second_job):
            if norm(item.get("name")) == skill_name:
                result = ELEMENT_CODE_TO_NAME.get(int(as_number(item.get("_Element"), -1)), "")
                if result:
                    self._skill_damage_element_cache[cache_key] = result
                    return result
        # A summon/cast action can have no element in SkillTable while its
        # damage projectile is explicitly elemental in the level description.
        # Only an exact standalone "火/水/光/暗属性" line is accepted here: a
        # description mentioning an elemental resistance debuff is not proof
        # that the damage itself has that element.
        levels = entry.get("levels") or []
        level = str(levels[-1][0]) if levels else "1"
        description = self.skill_damage_description(second_job, skill, level)
        described = re.search(r"(?m)^[ \t]*(火|水|光|暗)属性[ \t]*$", description)
        if described:
            result = described.group(1)
            self._skill_damage_element_cache[cache_key] = result
            return result
        self._skill_damage_element_cache[cache_key] = ""
        return ""

    def team_buff_names_for_job(self, second_job: str) -> list[str]:
        skill_names = set(self.full_skill_names(second_job))
        if not skill_names:
            return []
        names: list[str] = []
        for row in self.tables["buff"]:
            category = norm(row.get("类别"))
            if category != "团队buff":
                continue
            skill = norm(row.get("技能"))
            if skill and skill in skill_names:
                names.append(skill)
        return unique(names)

    def card_names(self) -> list[str]:
        return unique(row.get("卡片名称") for row in self.tables["card_data"])

    def card_levels(self) -> dict[str, int]:
        if self._card_levels is None:
            payload = load_json(self.card_levels_path) if self.card_levels_path.exists() else {}
            self._card_levels = {
                norm(name): int(level)
                for level, names in payload.items()
                for name in names
                if norm(name) not in REMOVED_CARD_NAMES
            }
        return dict(self._card_levels)

    def card_level(self, name: Any) -> int:
        return self.card_levels().get(norm(name), 0)


    def _load_skill_damage_index(self) -> dict[str, Any]:
        if self._skill_damage_index is not None:
            return self._skill_damage_index
        path = self.skill_damage_dir / "index.json"
        self._skill_damage_index = load_json(path) if path.exists() else {"jobs": []}
        return self._skill_damage_index

    def _load_skill_damage_allowlist(self) -> dict[str, set[str]]:
        if self._skill_damage_allowlist is not None:
            return self._skill_damage_allowlist
        path = self.skill_damage_dir / "allowlist.json"
        if not path.exists():
            self._skill_damage_allowlist = {}
            return self._skill_damage_allowlist
        data = load_json(path)
        jobs = data.get("jobs", data) if isinstance(data, dict) else {}
        self._skill_damage_allowlist = {
            norm(job): {norm(skill) for skill in skills if norm(skill)}
            for job, skills in jobs.items()
            if norm(job) and isinstance(skills, list)
        }
        return self._skill_damage_allowlist

    def _skill_damage_job_meta(self, second_job: str) -> dict[str, Any] | None:
        job_name = norm(second_job)
        for row in self._load_skill_damage_index().get("jobs", []):
            if norm(row.get("job_name")) == job_name:
                return row
        return None

    def skill_damage_job_data(self, second_job: str) -> dict[str, Any] | None:
        meta = self._skill_damage_job_meta(second_job)
        if not meta:
            return None
        filename = norm(meta.get("file"))
        if not filename:
            return None
        if filename not in self._skill_damage_cache:
            path = self.skill_damage_dir / filename
            if not path.exists():
                return None
            data = load_json(path)
            # Apply channel totals once at the common data boundary. Both the
            # simulator and score profiles also consume these raw entry lists.
            adjustments = self._load_skill_adjustments().get(norm(second_job), {})
            for group in ("n", "m"):
                for item in data.get(group, []):
                    if not isinstance(item, list) or len(item) < 3:
                        continue
                    name = self.skill_damage_canonical_name(second_job, item[1])
                    # The compact legacy summon row contains the level-2
                    # damage but was labelled level 1. Preserve that data for
                    # confirmed level-2 casts instead of dropping the skill.
                    if name == "暗影召唤术" and norm(second_job) in {"曜", "暗"}:
                        if not any(int(as_number(row[0])) == 2 for row in item[2] if row):
                            original = next((row for row in item[2] if row and int(as_number(row[0])) == 1), None)
                            if original is not None:
                                item[2].append([2, *original[1:]])
                    weights = adjustments.get(name, {}).get("channel_multipliers", {})
                    if not weights:
                        continue
                    levels = []
                    for level in item[2]:
                        result = list(level)
                        if len(result) >= 4 and isinstance(result[3], list):
                            result[3] = [
                                [damage[0],
                                 as_number(damage[1]) * as_number(weights.get(damage[0]), 1.0),
                                 as_number(damage[2]) * as_number(weights.get(damage[0]), 1.0)]
                                for damage in result[3]
                            ]
                        else:
                            raise ValueError(f"Channel adjustment requires damage rows: {name}")
                        levels.append(result)
                    item[2] = levels
            self._skill_damage_cache[filename] = data
        return self._skill_damage_cache[filename]

    def _skill_damage_entries(self, second_job: str, mode: str = "both") -> list[dict[str, Any]]:
        mode_key = norm(mode).lower()
        cache_key = (norm(second_job), mode_key)
        cached = self._skill_damage_entries_cache.get(cache_key)
        if cached is not None:
            return cached
        data = self.skill_damage_job_data(second_job)
        if not data:
            return []
        job_name = norm(second_job)
        ex_merge = SKILL_DAMAGE_EX_MERGE_BY_JOB.get(job_name, {})
        allowlist = self._load_skill_damage_allowlist()
        allowed_names = allowlist.get(job_name) if allowlist else None
        entries: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        groups = ("n",) if mode_key == "normal" else ("m",) if mode_key == "master" else ("n", "m")
        for group in groups:
            for item in data.get(group, []):
                if len(item) < 3:
                    continue
                skill_name = norm(item[1])
                if "ex" in skill_name.lower() and skill_name not in SKILL_DAMAGE_EX_ALLOWLIST:
                    continue
                if skill_name in ex_merge:
                    continue
                if allowed_names is not None and skill_name not in allowed_names:
                    continue
                skill_id = int(as_number(item[0]))
                if skill_id in seen_ids:
                    continue
                seen_ids.add(skill_id)
                entries.append({"id": skill_id, "name": skill_name, "levels": item[2], "mode": group})
        # A transformed hammer attack inherits the learned hammer level,
        # while its zero cooldown, action time and multiplier are independent.
        transformed = {"十字军": ("神圣之锤", "神圣之锤攻击"),
                       "烈": ("致残惩罚者", "致残惩罚者攻击")}.get(job_name)
        if transformed and mode_key != "master":
            parent, attack = transformed
            base = next((entry for entry in entries if entry["name"] == parent), None)
            if base and not any(entry["name"] == attack for entry in entries):
                levels = [list(row) for row in base["levels"]]
                for row in levels:
                    row[2] = 0
                entries.append({**base, "name": attack, "levels": levels,
                                "required_form": parent})
        name_counts: dict[str, int] = {}
        for entry in entries:
            name_counts[entry["name"]] = name_counts.get(entry["name"], 0) + 1
        for entry in entries:
            entry["label"] = entry["name"] if name_counts.get(entry["name"], 0) <= 1 else f"{entry['name']} [{entry['id']}]"
        self._skill_damage_entries_cache[cache_key] = entries
        return entries

    def skill_damage_names(self, second_job: str, mode: str = "both") -> list[str]:
        return ["无"] + [entry["label"] for entry in self._skill_damage_entries(second_job, mode)]

    def skill_damage_entry_names(self, second_job: str, mode: str = "both") -> list[str]:
        return [entry["name"] for entry in self._skill_damage_entries(second_job, mode)]

    def skill_damage_canonical_name(self, second_job: str, skill: str) -> str:
        selected = norm(skill)
        return SKILL_DAMAGE_EX_MERGE_BY_JOB.get(norm(second_job), {}).get(selected, selected)

    def _skill_damage_entry(self, second_job: str, skill: str, mode: str = "both") -> dict[str, Any] | None:
        selected = norm(skill)
        if not selected or selected == "无":
            return None
        selected = self.skill_damage_canonical_name(second_job, selected)
        cache_key = (norm(second_job), selected, norm(mode).lower())
        if cache_key in self._skill_damage_entry_cache:
            return self._skill_damage_entry_cache[cache_key]
        for entry in self._skill_damage_entries(second_job, mode):
            if selected in {entry["label"], entry["name"], str(entry["id"])}:
                self._skill_damage_entry_cache[cache_key] = entry
                return entry
        self._skill_damage_entry_cache[cache_key] = None
        return None

    def skill_damage_levels(self, second_job: str, skill: str, mode: str = "both") -> list[str]:
        entry = self._skill_damage_entry(second_job, skill, mode)
        if not entry:
            return ["无"]
        return ["无"] + [str(level[0]) for level in entry.get("levels", []) if level]

    def skill_damage_level_data(self, second_job: str, skill: str, level_text: str, mode: str = "both") -> list[Any] | None:
        entry = self._skill_damage_entry(second_job, skill, mode)
        if not entry:
            return None
        target = int(as_number(level_text, -1))
        for level in entry.get("levels", []):
            if level and int(as_number(level[0], -2)) == target:
                return self._filtered_skill_damage_level(second_job, skill, level)
        return None

    def _filtered_skill_damage_level(self, second_job: str, skill: str, level: list[Any]) -> list[Any]:
        adjustment = self.skill_damage_adjustment(second_job, skill)
        allowed_types = {
            norm(value)
            for value in adjustment.get("damage_types", [])
            if norm(value)
        }
        if not allowed_types:
            return level

        result = list(level)
        damage_index = (
            3 if len(result) >= 4 and isinstance(result[3], list)
            else 2 if len(result) >= 3 and isinstance(result[2], list)
            else None
        )
        if damage_index is not None:
            filtered = [
                list(damage)
                for damage in result[damage_index]
                if isinstance(damage, list) and damage and norm(damage[0]) in allowed_types
            ]
            if filtered:
                result[damage_index] = filtered
            return result

        type_index = 3 if len(result) >= 6 else 2 if len(result) >= 5 else None
        if type_index is not None and norm(result[type_index]) not in allowed_types:
            return []
        return result

    def _load_skill_adjustments(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._skill_adjustments is not None:
            return self._skill_adjustments
        if not self.skill_adjustments_path.exists():
            self._skill_adjustments = {}
            return self._skill_adjustments
        data = load_json(self.skill_adjustments_path)
        self._skill_adjustments = data if isinstance(data, dict) else {}
        return self._skill_adjustments

    def skill_damage_adjustment(self, second_job: str, skill: str) -> dict[str, Any]:
        entry = self._skill_damage_entry(second_job, skill)
        skill_name = entry.get("name") if entry else norm(skill)
        job_data = self._load_skill_adjustments().get(norm(second_job), {})
        adjustment = job_data.get(norm(skill_name)) or job_data.get(norm(skill)) or {}
        return adjustment if isinstance(adjustment, dict) else {}

    def skill_damage_panel_profile(self, second_job: str, skill: str) -> dict[str, Any] | None:
        adjustment = self.skill_damage_adjustment(second_job, skill)
        if not adjustment.get("channel_multipliers"):
            return None
        paladin = norm(second_job) == "圣骑士"
        return {"label": "物理+半魔" if paladin else "物理+魔法", "mode": "individual_sum",
                "denominator": "physical" if paladin else "average",
                "weights": {"P": 1.0, "M": 0.5 if paladin else 1.0}}

    def skill_damage_multiplier(self, second_job: str, skill: str, level: Any = None) -> float:
        adjustment = self.skill_damage_adjustment(second_job, skill)
        level_multipliers = adjustment.get("level_multipliers", {})
        level_text = norm(level)
        if isinstance(level_multipliers, dict) and level_text in level_multipliers:
            return max(0.0, as_number(level_multipliers[level_text], 1.0))
        return max(0.0, as_number(adjustment.get("multiplier"), 1.0))

    def skill_burning_hand_bonus(self, second_job: str, skill: str, mode: str = "base") -> float:
        return self.skill_burning_hand_trigger_count(second_job, skill, mode) * 0.35

    def skill_burning_hand_trigger_count(
        self,
        second_job: str,
        skill: str,
        mode: str = "base",
    ) -> float:
        adjustment = self.skill_damage_adjustment(second_job, skill)
        triggers = adjustment.get("burning_hand_triggers", {})
        if isinstance(triggers, dict):
            trigger_count = triggers.get(norm(mode))
            if trigger_count is None:
                trigger_count = triggers.get("base", 0)
        else:
            trigger_count = triggers
        return max(0.0, as_number(trigger_count))

    def skill_cooldown_rules(self) -> dict[str, Any]:
        if self._skill_cooldown_rules is not None:
            return self._skill_cooldown_rules
        if not self.skill_cooldown_rules_path.exists():
            self._skill_cooldown_rules = {}
            return self._skill_cooldown_rules
        data = load_json(self.skill_cooldown_rules_path)
        self._skill_cooldown_rules = data if isinstance(data, dict) else {}
        return self._skill_cooldown_rules

    def skill_cast_times(self) -> dict[str, Any]:
        if self._skill_cast_times is not None:
            return self._skill_cast_times
        payload = load_json(self.skill_cast_times_path) if self.skill_cast_times_path.exists() else {}
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        by_skill: dict[str, list[dict[str, Any]]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                skill = norm(row.get("skill"))
                if not skill:
                    continue
                by_skill.setdefault(skill, []).append(row)
        self._skill_cast_times = {"rows": rows if isinstance(rows, list) else [], "by_skill": by_skill}
        return self._skill_cast_times

    def skill_cast_time(self, skill: str, preferred_jobs: Iterable[Any] = ()) -> dict[str, Any]:
        data = self.skill_cast_times()
        by_skill = data.get("by_skill", {}) if isinstance(data, dict) else {}
        skill_name = norm(skill)
        if not skill_name or not isinstance(by_skill, dict):
            return {}

        preferred = [norm(job) for job in preferred_jobs if norm(job)]
        names = [skill_name]
        if "ex" not in skill_name.lower():
            names.extend([f"{skill_name}EX", f"{skill_name} EX"])

        for job in preferred:
            for name in names:
                for row in by_skill.get(name, []):
                    if norm(row.get("job")) == job:
                        return row
        for name in names:
            rows = by_skill.get(name, [])
            if rows:
                return rows[0]
        return {}

    @staticmethod
    def effective_skill_cast_time(
        timing: dict[str, Any],
        action_speed_percent: float = 0.0,
        *,
        quick: bool = False,
    ) -> float:
        """Apply external action speed to an already accelerated workbook time."""
        time_key = "quick_time" if quick else "cast_time"
        fixed_key = "quick_fixed" if quick else "fixed"
        cast_time = max(0.0, as_number(timing.get(time_key)))
        if cast_time <= 0:
            return cast_time

        intrinsic_speed = max(0.0, as_number(timing.get("speed_percent")))
        external_speed = max(0.0, as_number(action_speed_percent))
        skill = norm(timing.get("skill"))
        if skill:
            # Imported lazily to keep the data-loader/job-mechanics module
            # boundary acyclic during startup.
            from .job_mechanics import (
                adjust_ring_strike_cast_time,
                is_ring_strike,
                adjust_fixed_duration_action_speed_cast_time,
                is_fixed_duration_action_speed_output_skill,
            )

            if is_ring_strike(skill) or is_fixed_duration_action_speed_output_skill(skill):
                relative_action_factor = (
                    1.0 + (intrinsic_speed + external_speed) / 100.0
                ) / (1.0 + intrinsic_speed / 100.0)
                if is_ring_strike(skill):
                    return adjust_ring_strike_cast_time(cast_time, relative_action_factor)
                return adjust_fixed_duration_action_speed_cast_time(
                    skill,
                    cast_time,
                    relative_action_factor,
                )
        if bool(timing.get(fixed_key)):
            return cast_time
        return cast_time * (1.0 + intrinsic_speed / 100.0) / (
            1.0 + (intrinsic_speed + external_speed) / 100.0
        )

    def skill_glyphs(self, second_job: str) -> list[dict[str, Any]]:
        if self._skill_glyphs is None:
            payload = load_json(self.skill_glyphs_path) if self.skill_glyphs_path.exists() else {}
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            self._skill_glyphs = [row for row in entries if isinstance(row, dict)]
        tree = self.skill_build_tree(second_job, 70)
        allowed_jobs = {
            norm(group.get("job", {}).get("name"))
            for group in tree.get("groups", [])
            if isinstance(group, dict) and isinstance(group.get("job"), dict)
        }
        return [
            dict(row)
            for row in self._skill_glyphs
            if norm(row.get("job")) in allowed_jobs
        ]

    def skill_reference_cooldown_details(
        self,
        second_job: str,
        skill: str,
        fallback: float = 0.0,
    ) -> dict[str, Any]:
        """Resolve a reference cooldown together with its authoritative source.

        The overlay, planners and diagnostics must not silently disagree about
        whether a value came from the current add-point table, an explicit
        effect activation rule or a compatibility fallback.  Keep the same
        precedence as :meth:`skill_reference_cooldown`, but expose it for
        callers that need to explain the countdown shown to the user.
        """
        job_name = norm(second_job)
        skill_name = norm(skill)

        def resolved(
            cooldown: Any,
            source: str,
            source_label: str,
        ) -> dict[str, Any]:
            return {
                "cooldown": max(0.0, as_number(cooldown)),
                "source": source,
                "source_label": source_label,
            }

        # Support-skill cooldowns come from the current add-point table.  The
        # compact damage table can retain transformed or legacy combat rows
        # (for example Hunter's Eye 5s and Scorching Flame 18s), so it must not
        # override an explicit current personal/team effect definition.
        support_sources = set(self.job_buff_sources(job_name))
        is_support_skill = any(
            norm(row.get("技能")) == skill_name
            and norm(row.get("类别")) in {"个人buff", "团队buff"}
            and norm(row.get("来源")) in support_sources
            for row in self.tables.get("buff", [])
        )
        if is_support_skill:
            activation = self.buff_activation_rule_for_job(
                job_name, skill_name, "个人buff"
            ) or self.buff_activation_rule_for_job(
                job_name, skill_name, "团队buff"
            )
            cooldown = as_number(activation.get("cooldown"))
            if cooldown > 0:
                return resolved(
                    cooldown,
                    "buff_activation_rule",
                    "效果触发规则",
                )
            timing = self.skill_effect_timing(
                job_name, skill_name, max_required_level=70
            )
            cooldown = as_number(timing.get("cooldown"))
            if cooldown > 0:
                return resolved(
                    cooldown,
                    "current_skill_table",
                    "当前加点技能表",
                )

        adjustment = self._load_skill_adjustments().get(job_name, {}).get(skill_name, {})
        if isinstance(adjustment, dict) and "cooldown" in adjustment:
            return resolved(
                adjustment.get("cooldown"),
                "skill_adjustment",
                "技能修正规则",
            )

        data = self.skill_damage_job_data(job_name)
        if data:
            candidates: list[list[Any]] = []
            for group in ("n", "m"):
                for item in data.get(group, []):
                    if len(item) >= 3 and norm(item[1]) == skill_name and isinstance(item[2], list):
                        candidates.extend(level for level in item[2] if isinstance(level, list) and len(level) >= 3)
            valid = [level for level in candidates if as_number(level[1], 9999) <= 70]
            selected = max(valid or candidates, key=lambda level: (as_number(level[1]), as_number(level[0])), default=None)
            if selected:
                cooldown = as_number(selected[2])
                if cooldown > 0:
                    return resolved(
                        cooldown,
                        "compact_skill_table",
                        "当前伤害技能表",
                    )

        timing = self.skill_effect_timing(job_name, skill_name, max_required_level=70)
        cooldown = as_number(timing.get("cooldown"))
        if cooldown > 0:
            return resolved(
                cooldown,
                "current_skill_table",
                "当前加点技能表",
            )
        return resolved(fallback, "fallback", "兼容回退值")

    def skill_reference_cooldown(
        self,
        second_job: str,
        skill: str,
        fallback: float = 0.0,
    ) -> float:
        """Return the current level-70-or-lower cooldown for a rule source skill."""

        return as_number(
            self.skill_reference_cooldown_details(
                second_job,
                skill,
                fallback,
            ).get("cooldown")
        )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_key_number(value: Any) -> Any:
    number = as_number(value, default=float("nan"))
    if number != number:
        return value
    return int(number) if number.is_integer() else number


def load_modular_data() -> dict[str, Any] | None:
    if not MODULAR_MANIFEST.exists():
        return None
    manifest = load_json(MODULAR_MANIFEST)
    compact = int(manifest.get("format", 1)) >= 2

    tables: dict[str, list[dict[str, Any]]] = {}
    for table_name, files in manifest.get("tables", {}).items():
        rel_paths = files if isinstance(files, list) else [files]
        rows: list[dict[str, Any]] = []
        for rel_path in rel_paths:
            path = MODULAR_DIR / rel_path
            if not path.exists():
                return None
            part = load_json(path)
            if compact:
                rows.extend(expand_compact_table(table_name, part))
            elif isinstance(part, list):
                rows.extend(part)
            else:
                raise ValueError(f"Modular table must be a list: {path}")
        tables[table_name] = rows

    lists: dict[str, Any] = {}
    for rel_path in manifest.get("list_files", []):
        path = MODULAR_DIR / rel_path
        if not path.exists():
            return None
        values = load_json(path)
        if not isinstance(values, dict):
            raise ValueError(f"Modular list file must be an object: {path}")
        lists.update(values)
    for key, rel_path in manifest.get("lists", {}).items():
        path = MODULAR_DIR / rel_path
        if not path.exists():
            return None
        lists[key] = load_json(path)

    defaults_path = MODULAR_DIR / manifest.get("defaults", "defaults.json")
    attributes_path = MODULAR_DIR / manifest.get("attribute_order", "attribute_order.json")
    if not defaults_path.exists() or not attributes_path.exists():
        return None

    return {
        "source": manifest.get("source", str(DATA_PATH)),
        "tables": tables,
        "lists": lists,
        "defaults": load_json(defaults_path),
        "attribute_order": load_json(attributes_path),
    }


def expand_attrs_rows(prefix: dict[str, Any], attrs: dict[str, Any], attr_key: str = "属性", value_key: str = "数值") -> list[dict[str, Any]]:
    return [dict(prefix, **{attr_key: attr, value_key: value}) for attr, value in attrs.items()]


def expand_compact_table(table_name: str, data: Any) -> list[dict[str, Any]]:
    if table_name == "base":
        return expand_equipment_base(data)
    if table_name == "enhance":
        return expand_equipment_enhance(data)
    if table_name == "suffix":
        return expand_equipment_suffix(data)
    if table_name == "enchant":
        return [
            {"附魔类型": enchant_type, "附魔名称": name, "属性名称": attr, "倍率": value}
            for enchant_type, names in data.items()
            for name, attrs in names.items()
            for attr, value in attrs.items()
        ]
    if table_name == "jewelry":
        return expand_jewelry(data)
    if table_name == "set_bonus":
        return [
            {"套装ID": compact_key_number(set_id), "件数": compact_key_number(piece), "属性名称": attr, "属性数值": value}
            for set_id, pieces in data.items()
            for piece, attrs in pieces.items()
            for attr, value in attrs.items()
        ]
    if table_name == "card_lookup":
        return [
            {"key": f"{name}|{grade}", **attrs}
            for name, grades in data.items()
            for grade, attrs in grades.items()
        ]
    if table_name == "card_data":
        return [
            {"卡片名称": name, "等级": grade, **entry}
            for name, grades in data.items()
            for grade, payload in grades.items()
            for entry in payload.get("条目", [])
        ]
    if table_name == "glyph_a":
        return expand_simple_attrs(data, ["等级", "纹章名称"])
    if table_name == "glyph_a_extra":
        return expand_simple_attrs(data, ["等级", "额外属性"])
    if table_name == "glyph_s":
        return [
            {"等级": level, "纹章名称": name, "选择基础属性": selected, "数值档位": compact_key_number(rank), "属性": attr, "数值": value}
            for level, names in data.items()
            for name, selected_attrs in names.items()
            for selected, ranks in selected_attrs.items()
            for rank, attrs in ranks.items()
            for attr, value in attrs.items()
        ]
    if table_name == "glyph_s_extra":
        return expand_simple_attrs(data, ["等级", "纹章名称", "选择额外属性"])
    if table_name == "rune":
        return expand_rune(data)
    if table_name == "character":
        return [
            {"一转职业": first, "二转职业": second, "属性": attr, "数值": value}
            for first, seconds in data.items()
            for second, attrs in seconds.items()
            for attr, value in attrs.items()
        ]
    if table_name == "collection":
        return expand_simple_attrs(data, ["等级"])
    if table_name == "mastery":
        return expand_simple_attrs(data, ["专精", "等级"])
    if table_name == "title":
        return expand_simple_attrs(data, ["称号"])
    if table_name == "fashion":
        return expand_simple_attrs(data, ["部位", "时装"])
    if table_name == "talisman":
        return [
            {
                "类型": "主属性",
                "品质": grade,
                "护符": name,
                "掉落率": payload.get("掉落率", 0),
                "属性": attr,
                "数值": value,
            }
            for grade, names in data.get("主属性", {}).items()
            for name, payload in names.items()
            for attr, value in payload.get("属性", {}).items()
        ] + [
            {
                "类型": "额外属性",
                "品质": grade,
                "额外属性": name,
                "掉落率": payload.get("掉落率", 0),
                "属性": attr,
                "数值": value,
            }
            for grade, names in data.get("额外属性", {}).items()
            for name, payload in names.items()
            for attr, value in payload.get("属性", {}).items()
        ]
    if table_name == "conversion":
        return [
            {"一转职业": job, "来源属性": source, "目标属性": target, "转换比例": ratio}
            for job, sources in data.items()
            for source, targets in sources.items()
            for target, ratio in targets.items()
        ]
    if table_name == "job_map":
        return [{"二转职业": second, **payload} for second, payload in data.items()]
    if table_name == "effective":
        return expand_effective(data)
    if table_name == "buff":
        return expand_buff(data)
    if table_name == "auxiliary":
        return expand_auxiliary(data)
    raise ValueError(f"Unknown compact table: {table_name}")


def expand_equipment_base(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job, levels in data.items():
        for level, items in levels.items():
            for name, payload in items.items():
                prefix = {"职业": job, "等级": level, "装备名称": name, "部位": payload.get("部位"), "套装ID": payload.get("套装ID")}
                rows.extend(expand_attrs_rows(prefix, payload.get("属性", {}), "属性名称", "属性数值"))
    return rows


def expand_equipment_enhance(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"职业": job, "等级": level, "装备名称": name, "强化等级": compact_key_number(enhance), "属性名称": attr, "属性数值": value}
        for job, levels in data.items()
        for level, items in levels.items()
        for name, enhances in items.items()
        for enhance, attrs in enhances.items()
        for attr, value in attrs.items()
    ]


def expand_equipment_suffix(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"职业": job, "等级": level, "装备名称": name, "后缀名称": suffix, "属性名称": attr, "属性数值": value}
        for job, levels in data.items()
        for level, items in levels.items()
        for name, suffixes in items.items()
        for suffix, attrs in suffixes.items()
        for attr, value in attrs.items()
    ]


def expand_jewelry(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"饰品类别": slot, "等级": level, "饰品名称": name, "变体": variant, "属性名称": attr, "属性数值": value}
        for slot, levels in data.items()
        for level, items in levels.items()
        for name, variants in items.items()
        for variant, attrs in variants.items()
        for attr, value in attrs.items()
    ]


def expand_simple_attrs(data: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def walk(node: Any, depth: int, prefix: dict[str, Any]) -> None:
        if depth == len(keys):
            rows.extend(expand_attrs_rows(prefix, node))
            return
        key_name = keys[depth]
        for key, child in node.items():
            walk(child, depth + 1, dict(prefix, **{key_name: key}))

    walk(data, 0, {})
    return rows


def expand_rune(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    is_core = any(isinstance(level_data, dict) and any(isinstance(item, dict) and "转换比例" in item for item in level_data.values()) for level_data in data.values())
    if not is_core:
        for level, types in data.items():
            for rune_type, attrs in types.items():
                for attr, ranks in attrs.items():
                    for rank, value in ranks.items():
                        rows.append({"数据类型": "普通", "等级": level, "石板类型/转换": rune_type, "属性": attr, "档位": compact_key_number(rank), "数值": value})
        return rows

    for level, conversions in data.items():
        for conversion, payload in conversions.items():
            ratios = payload.get("转换比例", {})
            coeffs = payload.get("转换系数", {})
            caps = payload.get("最大扣除值", {})
            for ratio_rank, ratio in ratios.items():
                for coeff_rank, coeff in coeffs.items():
                    rows.append({
                        "数据类型": "特殊",
                        "等级": level,
                        "石板类型/转换": conversion,
                        "比例档位": compact_key_number(ratio_rank),
                        "转换比例": ratio,
                        "系数档位": compact_key_number(coeff_rank),
                        "转换系数": coeff,
                        "最大扣除值": caps.get(ratio_rank, caps.get(str(ratio_rank))),
                        "扣除属性": payload.get("扣除属性"),
                        "获得属性": payload.get("获得属性"),
                    })
    return rows


def expand_effective(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job, attacks in data.items():
        row: dict[str, Any] = {"职业": job}
        for index, attack in enumerate(attacks, start=1):
            row[f"攻击{index}"] = attack.get("攻击")
            row[f"属性{index}"] = attack.get("属性")
            row[f"权重{index}"] = attack.get("权重")
            row[f"固定{index}"] = attack.get("固定")
            row[f"额外最终{index}"] = attack.get("额外最终")
        rows.append(row)
    return rows


def expand_buff(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, payload in data.items():
        amplify_fixed_attack = bool(payload.get("固定攻击受技能百分比"))
        for level, attrs in payload.get("等级", {}).items():
            for attr, value in attrs.items():
                row = {
                    "选择键": key,
                    "类别": payload.get("类别"),
                    "来源": payload.get("来源"),
                    "技能": payload.get("技能"),
                    "属性": attr,
                    "等级": compact_key_number(level),
                    "表格数值": value,
                    "最大等级": payload.get("最大等级"),
                }
                rows.append(row)
                hidden_attr = (
                    SKILL_PERCENT_AMPLIFIED_FLAT_ATTACK_KEYS.get(norm(attr))
                    if amplify_fixed_attack
                    else None
                )
                if hidden_attr:
                    rows.append({**row, "属性": hidden_attr})
    return rows


def expand_auxiliary(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, set_id in data.get("装备套装", {}).items():
        rows.append({"套装键": key, "套装ID": set_id})
    for key, set_id in data.get("饰品套装", {}).items():
        rows.append({"饰品套装键": key, "饰品套装ID": set_id})
    return rows
