from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import os
from math import trunc
from typing import Any, Callable, Iterable

_EXACT_ROUND3 = os.environ.get('DN_SCORE_EXACT_ROUND3', '1') == '1'

from .data_loader import (
    TALISMAN_SLOT_PERCENT,
    DataStore,
    as_number,
    buff_aliases,
    buff_exclusive_group,
    buff_key,
    norm,
)
from .effect_stacking import (
    amplification_for_channel,
    attack_increase_for_channel,
    defense_reduction_for_attack,
    skill_percent_amplified_flat_attack,
)
from .equipment_resonance import neutral_passives, resonance_preview


SOURCE_ATTRS = ["力量", "敏捷", "智力", "体质"]
GLYPH_S_BASE_FIELDS = tuple(
    (f"base_attr{index}", f"rank{index}") for index in range(1, 5)
)
FLAT_TARGETS = {
    "HP": ["HP"],
    "MP": ["MP"],
    "物攻": ["最小物攻", "最大物攻"],
    "魔攻": ["最小魔攻", "最大魔攻"],
    "防御": ["防御"],
    "魔防": ["魔防"],
    "致命": ["致命"],
    "致命伤害": ["致命伤害"],
}
DISPLAY_TO_TARGET = {
    "最小物攻": "物攻",
    "最大物攻": "物攻",
    "最小魔攻": "魔攻",
    "最大魔攻": "魔攻",
}
PERCENT_ATTR = {
    "HP": "HP%",
    "MP": "MP%",
    "力量": "力量%",
    "敏捷": "敏捷%",
    "智力": "智力%",
    "体质": "体质%",
    "最小物攻": "物攻%",
    "最大物攻": "物攻%",
    "最小魔攻": "魔攻%",
    "最大魔攻": "魔攻%",
    "防御": "防御%",
    "魔防": "魔防%",
    "致命": "致命%",
    "致命伤害": "致命伤害%",
    "最终": "最终%",
}
ELEMENT_ATTRS = {"火攻%", "水攻%", "光攻%", "暗攻%", "无属性%"}
INDEPENDENT_SOURCE_PERCENT_ATTRS = {
    "力量": ("力量独立乘区%", "力量智力独立乘区%"),
    "敏捷": ("敏捷独立乘区%",),
    "智力": ("智力独立乘区%", "力量智力独立乘区%"),
    "体质": ("体质独立乘区%",),
}
PERCENT_ALIASES = {
    "物攻%": ("最小物攻%", "最大物攻%"),
    "魔攻%": ("最小魔攻%", "最大魔攻%"),
}
OPTION_ALIASES = {
    "物攻": ("物攻", "最大物攻"),
    "魔攻": ("魔攻", "最大魔攻"),
}
FREE_PERCENT_INPUT_ATTRS = {
    "最终%": "最终",
    "致命%": "致命",
    "爆伤%": "致命伤害",
    "致伤%": "致命伤害",
    "致命伤害%": "致命伤害",
}
ENGRAVING_ARMOR_SLOTS = {"头盔", "上装", "下装", "手套", "鞋子"}
ENGRAVING_WEAPON_SLOTS = {"主手", "副手"}
ENGRAVING_JEWELRY_SLOTS = {"项链", "耳环", "戒指1", "戒指2"}
ENGRAVING_EXTRA_ALIASES = {
    "防御力%": "防御%",
    "物防%": "防御%",
    "魔法防御力%": "魔防%",
    "魔防%": "魔防%",
    "暴击%": "致命%",
    "生命%": "HP%",
    "法力%": "MP%",
    "最终伤害%": "最终%",
    "火攻击%": "火攻%",
    "冰攻击%": "水攻%",
    "水攻击%": "水攻%",
    "光攻击%": "光攻%",
    "暗攻击%": "暗攻%",
    "无属性攻击%": "无属性%",
}
DEFENSE_LEVEL_REDUCTION_BASE = {
    70: 8655.0,
    75: 10339.0,
}
DEFENSE_RATE_CAP = 0.999899983406067


def add_attr(bucket: dict[str, float], attr: Any, amount: Any) -> None:
    name = norm(attr)
    value = as_number(amount)
    if not name or name == "无" or value == 0:
        return
    if name == "物攻":
        bucket["最小物攻"] += value
        bucket["最大物攻"] += value
    elif name == "魔攻":
        bucket["最小魔攻"] += value
        bucket["最大魔攻"] += value
    else:
        bucket[name] += value


def get_attr(bucket: dict[str, float], attr: str) -> float:
    return bucket.get(attr, 0.0)


def get_display_attr(bucket: dict[str, float], attr: str) -> float:
    aliases = PERCENT_ALIASES.get(attr)
    if aliases:
        alias_values = [get_attr(bucket, alias) for alias in aliases if abs(get_attr(bucket, alias)) > 1e-9]
        return get_attr(bucket, attr) + (sum(alias_values) / len(alias_values) if alias_values else 0.0)
    return get_attr(bucket, attr)


def is_percent_attr(attr: Any) -> bool:
    return norm(attr).endswith("%")


def option_keys(value: Any) -> tuple[str, ...]:
    text = norm(value)
    return OPTION_ALIASES.get(text, (text,))


@dataclass
class ResultRow:
    attr: str
    base: float = 0.0
    equipment: float = 0.0
    card: float = 0.0
    talisman: float = 0.0
    glyph: float = 0.0
    normal_rune: float = 0.0
    core_mastery: float = 0.0
    actual: float = 0.0
    converted: float = 0.0
    buffed: float = 0.0
    percent: float | None = None


class BuildCalculator:
    def __init__(self, store: DataStore):
        self.store = store
        self._static_calculation_cache: dict[str, tuple[Any, ...]] = {}
        self._exact_summary_cache: dict[tuple[Any, ...], tuple[Any, dict[str, float]]] = {}
        self._talisman_result_cache: dict[str, tuple[Any, ...]] = {}
        self._build_indexes()

    def fork_calculation_state(self) -> BuildCalculator:
        """Share read-only resource indexes, not mutable calculation/search caches."""
        import copy
        calculator = copy.copy(self)
        calculator._static_calculation_cache = {}
        calculator._exact_summary_cache = {}
        calculator._talisman_result_cache = {}
        return calculator

    @staticmethod
    def _static_build_signature(build: dict[str, Any]) -> str:
        """Identify the build portion that is unchanged by timeline buffs."""
        static_build = {
            key: value
            for key, value in build.items()
            if key not in {"buffs", "extra_buff_attrs"}
        }
        return json.dumps(
            static_build,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _build_indexes(self) -> None:
        self.base: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.enhance: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        self.suffix: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.jewelry: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.enchant: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.set_bonus: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.card_lookup: dict[str, dict[str, float]] = {}
        self.talisman: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.talisman_extra: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.glyph_a: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.glyph_a_extra: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.glyph_s: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.glyph_s_extra: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.rune_normal: dict[tuple[str, str, str, str], float] = {}
        self.rune_core: list[dict[str, Any]] = []
        self.collection: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.mastery: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        self.title: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.fashion: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.conversion: dict[tuple[str, str, str], float] = {}
        self.effective: dict[str, dict[str, Any]] = {}
        self.buff_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.buff_by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.buff_label_to_key: dict[str, str] = {}
        self.equipment_set_ids: dict[tuple[str, str], int] = {}
        self.jewelry_set_ids: dict[tuple[str, str], int] = {}

        for row in self.store.tables["base"]:
            key = (norm(row.get("职业")), norm(row.get("装备名称")))
            self.base[key].append(row)
            set_id = int(as_number(row.get("套装ID")))
            if set_id:
                self.equipment_set_ids[key] = set_id
        for row in self.store.tables["enhance"]:
            self.enhance[(norm(row.get("职业")), norm(row.get("装备名称")), int(as_number(row.get("强化等级"))))].append(row)
        for row in self.store.tables["suffix"]:
            self.suffix[(norm(row.get("职业")), norm(row.get("装备名称")), norm(row.get("后缀名称")))].append(row)
        for row in self.store.tables["jewelry"]:
            self.jewelry[(norm(row.get("饰品类别")), norm(row.get("饰品名称")), norm(row.get("变体")))].append(row)
        for row in self.store.tables["enchant"]:
            self.enchant[norm(row.get("附魔名称"))].append(row)
        for row in self.store.tables["set_bonus"]:
            self.set_bonus[int(as_number(row.get("套装ID")))].append(row)
        for row in self.store.tables["card_lookup"]:
            attrs = {k: as_number(v) for k, v in row.items() if k != "key"}
            self.card_lookup[norm(row.get("key"))] = attrs
        for row in self.store.tables.get("talisman", []):
            grade = norm(row.get("品质"))
            if norm(row.get("类型")) == "主属性":
                self.talisman[(grade, norm(row.get("护符")))].append(row)
            elif norm(row.get("类型")) == "额外属性":
                self.talisman_extra[(grade, norm(row.get("额外属性")))].append(row)
        for row in self.store.tables["glyph_a"]:
            self.glyph_a[(norm(row.get("等级")), norm(row.get("纹章名称")))].append(row)
        for row in self.store.tables["glyph_a_extra"]:
            self.glyph_a_extra[(norm(row.get("等级")), norm(row.get("额外属性")))].append(row)
        for row in self.store.tables["glyph_s"]:
            key = (norm(row.get("等级")), norm(row.get("纹章名称")), norm(row.get("选择基础属性")), norm(row.get("数值档位")))
            self.glyph_s[key].append(row)
        for row in self.store.tables["glyph_s_extra"]:
            self.glyph_s_extra[(norm(row.get("等级")), norm(row.get("纹章名称")), norm(row.get("选择额外属性")))].append(row)
        for row in self.store.tables["rune"]:
            if norm(row.get("数据类型")) == "普通":
                self.rune_normal[(norm(row.get("等级")), norm(row.get("石板类型/转换")), norm(row.get("属性")), norm(row.get("档位")))] = as_number(row.get("数值"))
            elif norm(row.get("数据类型")) == "特殊":
                self.rune_core.append(row)
        for row in self.store.tables["collection"]:
            self.collection[int(as_number(row.get("等级")))].append(row)
        for row in self.store.tables["mastery"]:
            self.mastery[(norm(row.get("专精")), int(as_number(row.get("等级"))))].append(row)
        for row in self.store.tables["title"]:
            self.title[norm(row.get("称号"))].append(row)
        for row in self.store.tables["fashion"]:
            self.fashion[norm(row.get("时装"))].append(row)
        for row in self.store.tables["conversion"]:
            self.conversion[(norm(row.get("一转职业")), norm(row.get("来源属性")), norm(row.get("目标属性")))] = as_number(row.get("转换比例"))
        for row in self.store.tables["effective"]:
            self.effective[norm(row.get("职业"))] = row
        for row in self.store.tables["buff"]:
            key = buff_key(row)
            level = norm(row.get("等级"))
            self.buff_by_key[(key, level)].append(row)
            self.buff_by_name[(norm(row.get("技能")), level)].append(row)
            for alias in buff_aliases(row):
                self.buff_label_to_key.setdefault(alias, key)
            self.buff_label_to_key.setdefault(key, key)
        for row in self.store.tables.get("auxiliary", []):
            eq_key = norm(row.get("套装键"))
            if "|" in eq_key:
                job, name = eq_key.split("|", 1)
                set_id = int(as_number(row.get("套装ID")))
                if set_id:
                    self.equipment_set_ids[(job, name)] = set_id
            j_key = norm(row.get("饰品套装键"))
            if "|" in j_key:
                slot, name = j_key.split("|", 1)
                set_id = int(as_number(row.get("饰品套装ID")))
                if set_id:
                    self.jewelry_set_ids[(slot, name)] = set_id

    def calculate(
        self,
        build: dict[str, Any],
        *,
        static_signature: str | None = None,
        summary_only: bool = False,
    ) -> tuple[list[ResultRow], dict[str, float]]:
        memoize_summary = _EXACT_ROUND3 and summary_only and static_signature is not None
        first_job = norm(build.get("first_job"))
        second_job = norm(build.get("second_job"))
        if static_signature is None:
            static_signature = self._static_build_signature(build)
        cached_static = self._static_calculation_cache.get(static_signature)
        if cached_static is None:
            jobs = [second_job, first_job, self.store.job_family(second_job, first_job)]
            categories = {
                "base": defaultdict(float),
                "equipment": defaultdict(float),
                "card": defaultdict(float),
                "talisman": defaultdict(float),
                "glyph": defaultdict(float),
                "normal_rune": defaultdict(float),
                "core_mastery": defaultdict(float),
            }
            set_counts: Counter[int] = Counter()

            self._add_character_and_misc(categories["base"], build, second_job)
            self._add_equipment(categories["equipment"], build, jobs, set_counts)
            self._add_jewelry(categories["equipment"], build, set_counts)
            self._add_set_bonus(categories["equipment"], set_counts)
            for attr, value in resonance_preview(self.store, build)['attrs'].items():
                add_attr(categories["equipment"], attr, value)
            self._add_cards(categories["card"], build)
            self._add_talismans(categories["talisman"], build)
            self._add_glyphs(categories["glyph"], build)
            self._add_normal_runes(categories["normal_rune"], build)
            for bucket in categories.values():
                self._truncate_fixed_attrs(bucket)

            pre_special = self._sum_categories(categories)
            self._truncate_fixed_attrs(pre_special)
            skill_buff_exempt_attack: dict[str, float] = defaultdict(float)
            # Mastery is part of the character's fixed panel before the core
            # rune converts its source attribute.  In particular, defense and
            # magic-defense mastery must increase a 防御/魔防 source rune just
            # like the same attributes from equipment do.
            self._add_mastery(
                categories["core_mastery"],
                build,
                skill_buff_exempt_attack,
            )
            self._truncate_fixed_attrs(categories["core_mastery"])
            actual_before_core = self._actual_attributes(
                pre_special, categories["core_mastery"]
            )
            self._truncate_fixed_attrs(actual_before_core)
            converted_before_core = self._convert_attributes(actual_before_core, pre_special, first_job, second_job)
            self._truncate_fixed_attrs(converted_before_core)
            self._add_core_rune(
                categories["core_mastery"],
                build,
                converted_before_core,
                skill_buff_exempt_attack,
            )
            self._truncate_fixed_attrs(categories["core_mastery"])
            self._truncate_fixed_attrs(skill_buff_exempt_attack)

            actual = self._actual_attributes(pre_special, categories["core_mastery"])
            self._truncate_fixed_attrs(actual)
            converted = self._convert_attributes(actual, pre_special, first_job, second_job)
            self._truncate_fixed_attrs(converted)
            cached_static = (
                categories,
                pre_special,
                actual,
                converted,
                skill_buff_exempt_attack,
            )
            self._static_calculation_cache[static_signature] = cached_static
            while len(self._static_calculation_cache) > 64:
                self._static_calculation_cache.pop(next(iter(self._static_calculation_cache)))
        else:
            (
                categories,
                pre_special,
                actual,
                converted,
                skill_buff_exempt_attack,
            ) = cached_static
        buff_values = self._buff_values(build)
        summary_key = None
        if memoize_summary:
            # The static signature includes every build field except Buffs.
            # Resolve current Buffs first, then key by their exact ordered
            # numeric values. In particular, Judgement's snapshot is NOT a
            # skill-name-only cache. Hold the static state to guard id reuse.
            summary_key = (static_signature, tuple(buff_values.items()))
            cached_summary = self._exact_summary_cache.get(summary_key)
            if cached_summary is not None and cached_summary[0] is cached_static:
                return [], dict(cached_summary[1])
        buff_actual = self._actual_with_source_buffs(actual, pre_special, buff_values)
        self._truncate_fixed_attrs(buff_actual)
        buff_converted = self._convert_attributes(buff_actual, pre_special, first_job, second_job)
        self._truncate_fixed_attrs(buff_converted)
        buffed = self._apply_buffs(
            buff_converted,
            buff_values,
            skip_attrs=set(SOURCE_ATTRS),
            percent_exempt=skill_buff_exempt_attack,
        )
        rows: list[ResultRow] = []
        if not summary_only:
            row_attrs = list(dict.fromkeys(self.store.attribute_order))
            for bucket in [*categories.values(), actual, converted, buffed]:
                for attr in bucket:
                    if attr not in row_attrs:
                        row_attrs.append(attr)
            for attr in row_attrs:
                row = ResultRow(
                    attr=attr,
                    base=get_display_attr(categories["base"], attr),
                    equipment=get_display_attr(categories["equipment"], attr),
                    card=get_display_attr(categories["card"], attr),
                    talisman=get_display_attr(categories["talisman"], attr),
                    glyph=get_display_attr(categories["glyph"], attr),
                    normal_rune=get_display_attr(categories["normal_rune"], attr),
                    core_mastery=get_display_attr(categories["core_mastery"], attr),
                    actual=get_display_attr(actual, attr),
                    converted=get_display_attr(converted, attr),
                    buffed=get_display_attr(buffed, attr),
                )
                if attr == "最终":
                    row.percent = self.final_percent(row.buffed)
                elif attr == "致命":
                    row.percent = min(round(row.buffed / 433, 1), 90)
                elif attr == "致命伤害":
                    row.percent = round(row.buffed / 62500 * 100, 3)
                rows.append(row)
            for attr, source_attr in (("防御率%", "防御"), ("魔防率%", "魔防")):
                rows.append(ResultRow(
                    attr=attr,
                    actual=self.defense_percent(get_attr(actual, source_attr)),
                    converted=self.defense_percent(get_attr(converted, source_attr)),
                    buffed=self.defense_percent(get_attr(buffed, source_attr)),
                ))
        effective_second_job = norm(build.get("effective_second_job")) or second_job
        summary = self._effective_panel(effective_second_job, build, buffed, buff_values)
        if summary_key is not None:
            if len(self._exact_summary_cache) >= 2048:
                self._exact_summary_cache.pop(next(iter(self._exact_summary_cache)))
            self._exact_summary_cache[summary_key] = (cached_static, dict(summary))
        return rows, summary

    def _add_character_and_misc(self, bucket: dict[str, float], build: dict[str, Any], second_job: str) -> None:
        for attr, amount in self.store.character_base(second_job).items():
            add_attr(bucket, attr, amount)
        level = int(as_number(build.get("collection_level")))
        for current_level, rows in self.collection.items():
            if current_level <= level:
                for row in rows:
                    add_attr(bucket, row.get("属性"), row.get("数值"))
        for row in self.title.get(norm(build.get("title")), []):
            add_attr(bucket, row.get("属性"), row.get("数值"))
        for item in build.get("fashion", []):
            for row in self.fashion.get(norm(item.get("name")), []):
                add_attr(bucket, row.get("属性"), row.get("数值"))
        self._add_engravings(bucket, build)
        self._add_free_inputs(bucket, build)

    def prepare_talisman_objective(self, build: dict[str, Any], *, compiled: bool = True) -> Callable[..., float]:
        """Evaluate slot searches without rebuilding equipment or sharing cache state.

        All conversions, per-stage truncations, core-rune deductions and buffs
        below use the same methods as calculate(). The caller supplies totals
        of already individually truncated talisman_attrs() results.
        """
        import copy

        baseline = copy.deepcopy(build)
        baseline["talismans"] = []
        calculator = self.fork_calculation_state()
        # The conversion is fixed throughout a talisman search. Avoid scanning
        # the entire core table on every branch, retaining identical rows/order.
        core = baseline.get("core_rune", {})
        calculator.rune_core = [
            row for row in calculator.rune_core
            if norm(row.get("等级")) == norm(core.get("level"))
            and norm(row.get("石板类型/转换")) == norm(core.get("conversion"))
        ]
        signature = calculator._static_build_signature(baseline)
        calculator.calculate(baseline, static_signature=signature, summary_only=True)
        pre_base = dict(calculator._static_calculation_cache[signature][1])
        mastery = defaultdict(float)
        mastery_exempt = defaultdict(float)
        calculator._add_mastery(mastery, baseline, mastery_exempt)
        calculator._truncate_fixed_attrs(mastery)
        buffs = calculator._buff_values(baseline)
        first, second = norm(baseline.get("first_job")), norm(baseline.get("second_job"))
        effective_job = norm(baseline.get("effective_second_job")) or second
        core_parameters = calculator._core_rune_parameters(baseline)
        needed = set(SOURCE_ATTRS) | ELEMENT_ATTRS | {
            "最小物攻", "最大物攻", "最小魔攻", "最大魔攻", "最终", "致命", "致命伤害",
        }
        for attr in core_parameters[:2]:
            needed.update(FLAT_TARGETS.get(attr, [attr]) if attr else [])
        needed_attrs = tuple(sorted(needed))
        conversion_targets = {key: values for key, values in FLAT_TARGETS.items() if set(values) & needed}
        buff_attrs = tuple(attr for attr in self.store.attribute_order if attr in needed)

        def objective(totals: dict[str, float], *, relaxed_core: bool = False, lower_totals=None) -> float:
            pre = defaultdict(float, pre_base)
            for attr, value in totals.items():
                pre[attr] += value
                if not is_percent_attr(attr):
                    pre[attr] = trunc(pre[attr])
            special = defaultdict(float, mastery)
            exempt = defaultdict(float, mastery_exempt)
            actual = calculator._actual_attributes(pre, special, attributes=needed_attrs)
            calculator._truncate_fixed_attrs(actual)
            converted = calculator._convert_attributes(actual, pre, first, second, targets=conversion_targets)
            calculator._truncate_fixed_attrs(converted)
            calculator._add_core_rune(special, baseline, converted, exempt, parameters=core_parameters)
            if relaxed_core:
                # Interval bound: gain uses the largest possible source, while
                # deduction uses its smallest possible source. Do not discard
                # an already unavoidable baseline deduction from the bound.
                source, _, ratio, coeff, cap = core_parameters
                lower_pre = defaultdict(float, pre_base)
                for attr, value in (lower_totals or {}).items():
                    lower_pre[attr] += value
                calculator._truncate_fixed_attrs(lower_pre)
                lower_actual = calculator._actual_attributes(lower_pre, mastery, attributes=needed_attrs)
                calculator._truncate_fixed_attrs(lower_actual)
                lower_converted = calculator._convert_attributes(lower_actual, lower_pre, first, second, targets=conversion_targets)
                calculator._truncate_fixed_attrs(lower_converted)
                lower_source = calculator._core_source_value(lower_converted, source)
                deduction = (min(lower_source, cap) if cap > 0 else lower_source) * ratio / 100 if coeff > 0 else 0.0
                for key in FLAT_TARGETS.get(source, [source]):
                    special[key] = max(special[key], mastery.get(key, 0.0) - deduction)
            calculator._truncate_fixed_attrs(special)
            calculator._truncate_fixed_attrs(exempt)
            actual = calculator._actual_attributes(pre, special, attributes=needed_attrs)
            calculator._truncate_fixed_attrs(actual)
            buff_actual = calculator._actual_with_source_buffs(actual, pre, buffs)
            calculator._truncate_fixed_attrs(buff_actual)
            converted = calculator._convert_attributes(buff_actual, pre, first, second, targets=conversion_targets)
            calculator._truncate_fixed_attrs(converted)
            buffed = calculator._apply_buffs(
                converted, buffs, skip_attrs=set(SOURCE_ATTRS), percent_exempt=exempt, attributes=buff_attrs,
            )
            return calculator._effective_panel(effective_job, baseline, buffed, buffs)["effective_panel"]

        if not compiled:
            return objective
        from .talisman_objective import compile_objective
        return compile_objective(calculator, baseline, pre_base, mastery, mastery_exempt, buffs,
                                 needed_attrs, conversion_targets, buff_attrs, core_parameters, objective)

    def _add_engravings(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        for item in build.get("engravings", []):
            slot = norm(item.get("slot"))
            main_value = as_number(item.get("main_value"))
            secondary_value = as_number(item.get("secondary_value"))
            if not main_value and "fixed1" in item:
                if slot in ENGRAVING_JEWELRY_SLOTS or slot not in ENGRAVING_ARMOR_SLOTS | ENGRAVING_WEAPON_SLOTS:
                    main_value = as_number(item.get("fixed2")) or as_number(item.get("fixed1")) * 2
                    secondary_value = as_number(item.get("fixed1"))
                else:
                    main_value = as_number(item.get("fixed1"))
                    secondary_value = as_number(item.get("fixed2"))
            if slot in ENGRAVING_ARMOR_SLOTS:
                add_attr(bucket, "防御%", main_value)
                add_attr(bucket, "魔防%", main_value)
            elif slot in ENGRAVING_WEAPON_SLOTS:
                add_attr(bucket, "物攻%", main_value)
                add_attr(bucket, "魔攻%", main_value)
            elif slot in ENGRAVING_JEWELRY_SLOTS:
                add_attr(bucket, "HP%", main_value)
                add_attr(bucket, "物攻%", secondary_value)
                add_attr(bucket, "魔攻%", secondary_value)
            else:
                add_attr(bucket, "致命%", main_value)
                add_attr(bucket, "物攻%", secondary_value)
                add_attr(bucket, "魔攻%", secondary_value)
            extra_attr = self._engraving_extra_attr(item.get("extra"))
            if extra_attr:
                add_attr(bucket, extra_attr, item.get("extra_value"))

    @staticmethod
    def _engraving_extra_attr(attr: Any) -> str:
        text = norm(attr)
        if not text or text == "无":
            return ""
        return ENGRAVING_EXTRA_ALIASES.get(text, text)

    def _add_equipment(self, bucket: dict[str, float], build: dict[str, Any], jobs: list[str], set_counts: Counter[int]) -> None:
        for item in build.get("equipment", []):
            name = norm(item.get("name"))
            if not name or name == "无":
                continue
            matched_job = ""
            for job in jobs:
                if not job:
                    continue
                rows = self.base.get((job, name), [])
                if rows:
                    matched_job = job
                    for row in rows:
                        add_attr(bucket, row.get("属性名称"), row.get("属性数值"))
                    set_id = self.equipment_set_ids.get((job, name))
                    if set_id:
                        set_counts[set_id] += 1
                    break
            enhance_level = int(as_number(item.get("enhance")))
            candidate_jobs = [matched_job] if matched_job else [job for job in jobs if job]
            for job in candidate_jobs:
                rows = self.enhance.get((job, name, enhance_level), [])
                if rows:
                    for row in rows:
                        add_attr(bucket, row.get("属性名称"), row.get("属性数值"))
                    break
            suffix = norm(item.get("suffix"))
            if suffix and suffix != "无":
                for job in candidate_jobs:
                    rows = self.suffix.get((job, name, suffix), [])
                    if rows:
                        for row in rows:
                            add_attr(bucket, row.get("属性名称"), row.get("属性数值"))
                        break
            for enchant in item.get("enchants", []):
                self._add_enchant(bucket, enchant)

    def _add_jewelry(self, bucket: dict[str, float], build: dict[str, Any], set_counts: Counter[int]) -> None:
        for item in build.get("jewelry", []):
            slot, name, variant = norm(item.get("slot")), norm(item.get("name")), norm(item.get("variant"))
            lookup_slot = "戒指" if slot.startswith("戒指") else slot
            if not name or name == "无":
                continue
            if hasattr(self.store, "jewelry_variant_code"):
                variant = self.store.jewelry_variant_code(variant)
            if variant == "无":
                variant = ""
            for row in self.jewelry.get((lookup_slot, name, variant), []):
                add_attr(bucket, row.get("属性名称"), row.get("属性数值"))
            set_id = self.jewelry_set_ids.get((lookup_slot, name))
            if set_id:
                set_counts[set_id] += 1
            for enchant in item.get("enchants", []):
                self._add_enchant(bucket, enchant)

    def _add_enchant(self, bucket: dict[str, float], enchant: Any) -> None:
        if norm(enchant) == "无":
            return
        for row in self.enchant.get(norm(enchant), []):
            add_attr(bucket, row.get("属性名称"), row.get("倍率"))

    def _add_set_bonus(self, bucket: dict[str, float], set_counts: Counter[int]) -> None:
        for set_id, count in set_counts.items():
            for row in self.set_bonus.get(set_id, []):
                if int(as_number(row.get("件数"))) <= count:
                    add_attr(bucket, row.get("属性名称"), row.get("属性数值"))

    def _add_cards(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        raw: dict[str, float] = defaultdict(float)
        initial_override = build.get("card_initial_totals")
        if isinstance(initial_override, dict):
            for attr, amount in initial_override.items():
                add_attr(raw, attr, amount)
        else:
            for item in build.get("card_grades", []):
                name = norm(item.get("name"))
                grade = norm(item.get("grade")) or "无"
                if not name or name == "无":
                    continue
                attrs = self.card_lookup.get(f"{name}|{grade}", {})
                for attr, amount in attrs.items():
                    add_attr(raw, attr, amount)
        bonus_levels = build.get("card_bonus_levels", {})
        for attr, amount in raw.items():
            bonus_key = DISPLAY_TO_TARGET.get(attr, attr)
            level = (
                0.0
                if bonus_key == "致命伤害"
                else as_number(bonus_levels.get(bonus_key, bonus_levels.get(attr, 0)))
            )
            add_attr(bucket, attr, amount * (1 + level * 0.05))

    def _add_talismans(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        for item in build.get("talismans", []):
            if not isinstance(item, dict):
                continue
            for attr, amount in self.talisman_attrs(item).items():
                add_attr(bucket, attr, amount)

    def _add_glyphs(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        for item in build.get("glyph_a", []):
            for row in self.glyph_a.get((norm(item.get("level")), norm(item.get("name"))), []):
                add_attr(bucket, row.get("属性"), row.get("数值"))
            extra = norm(item.get("extra"))
            if extra and extra != "无":
                for key in option_keys(extra):
                    for row in self.glyph_a_extra.get((norm(item.get("level")), key), []):
                        add_attr(bucket, row.get("属性"), row.get("数值"))
        for item in build.get("glyph_s", []):
            for attr_key, rank_key in GLYPH_S_BASE_FIELDS:
                attr, rank = norm(item.get(attr_key)), norm(item.get(rank_key))
                if attr and attr != "无" and rank and rank != "无":
                    for row in self.glyph_s.get((norm(item.get("level")), norm(item.get("name")), attr, rank), []):
                        add_attr(bucket, row.get("属性"), row.get("数值"))
            extra = norm(item.get("extra"))
            if extra and extra != "无":
                for key in option_keys(extra):
                    for row in self.glyph_s_extra.get((norm(item.get("level")), norm(item.get("name")), key), []):
                        add_attr(bucket, row.get("属性"), row.get("数值"))

    def _add_normal_runes(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        boost = 1 + as_number(build.get("core_rune", {}).get("enhance")) / 100
        for rune in build.get("normal_runes", []):
            level, rune_type = norm(rune.get("level")), norm(rune.get("type"))
            for entry in rune.get("entries", []):
                amount = self.rune_normal.get((level, rune_type, norm(entry.get("attr")), norm(entry.get("rank"))), 0)
                add_attr(bucket, entry.get("attr"), amount * boost)

    def _add_core_rune(
        self,
        bucket: dict[str, float],
        build: dict[str, Any],
        source_values: dict[str, float],
        skill_buff_exempt_attack: dict[str, float] | None = None,
        *,
        parameters: tuple[str, str, float, float, float] | None = None,
    ) -> None:
        source_attr, target_attr, ratio, coeff, max_deduct = (
            parameters if parameters is not None else self._core_rune_parameters(build)
        )
        if not source_attr or not target_attr or ratio <= 0 or coeff <= 0:
            return
        source_value = self._core_source_value(source_values, source_attr)
        capped_source = min(source_value, max_deduct) if max_deduct > 0 else source_value
        deducted = capped_source * ratio / 100
        gained = deducted * coeff
        add_attr(bucket, source_attr, -deducted)
        add_attr(bucket, target_attr, gained)
        if norm(target_attr) in {"物攻", "魔攻"} and skill_buff_exempt_attack is not None:
            add_attr(skill_buff_exempt_attack, target_attr, gained)

    def _core_rune_parameters(self, build: dict[str, Any]) -> tuple[str, str, float, float, float]:
        core = build.get("core_rune", {})
        level, conversion = norm(core.get("level")), norm(core.get("conversion"))
        if not level or not conversion:
            return "", "", 0.0, 0.0, 0.0
        source_attr, target_attr = self._split_conversion(conversion)
        if not source_attr or not target_attr:
            return "", "", 0.0, 0.0, 0.0
        candidates = [
            row for row in self.rune_core
            if norm(row.get("等级")) == level and norm(row.get("石板类型/转换")) == conversion
        ]
        ratio = self._core_selected_value(candidates, core.get("ratio_rank"), "转换比例", "比例档位")
        coeff = self._core_selected_value(candidates, core.get("coeff_rank"), "转换系数", "系数档位")
        max_deduct = self._average([as_number(row.get("最大扣除值")) for row in candidates])
        return source_attr, target_attr, ratio, coeff, max_deduct

    def _add_mastery(
        self,
        bucket: dict[str, float],
        build: dict[str, Any],
        skill_buff_exempt_attack: dict[str, float] | None = None,
    ) -> None:
        for item in build.get("mastery", []):
            for row in self.mastery.get((norm(item.get("name")), int(as_number(item.get("level")))), []):
                attr = norm(row.get("属性"))
                value = row.get("数值")
                add_attr(bucket, attr, value)
                if attr in {"物攻", "最小物攻", "最大物攻", "魔攻", "最小魔攻", "最大魔攻"} and skill_buff_exempt_attack is not None:
                    add_attr(skill_buff_exempt_attack, attr, value)

    def _add_free_inputs(self, bucket: dict[str, float], build: dict[str, Any]) -> None:
        free = build.get("free_inputs", {})
        for attr, amount in free.get("stats", {}).items():
            add_attr(bucket, attr, amount)
        for attr, amount in free.get("stat_percent", {}).items():
            add_attr(bucket, attr, amount)

    def _free_percent_bonus(self, build: dict[str, Any], attr: str) -> float:
        percent_inputs = build.get("free_inputs", {}).get("percent", {})
        total = 0.0
        for label, value in percent_inputs.items():
            if FREE_PERCENT_INPUT_ATTRS.get(norm(label)) == attr:
                total += as_number(value)
        return total

    @staticmethod
    def _panel_percent_bonus(buffs: dict[str, float], attr: str) -> float:
        aliases = {
            "最终": {"最终面板%"},
            "致命": {"致命面板%"},
            "致命伤害": {"致命伤害面板%", "爆伤面板%", "致伤面板%"},
        }.get(attr, {f"{attr}面板%"})
        return sum(as_number(value) for key, value in buffs.items() if norm(key) in aliases)

    @staticmethod
    def _element_resist_reduction(buffs: dict[str, float], element: str) -> float:
        aliases = {
            "火": {"减火抗%", "减全抗%"},
            "水": {"减水抗%", "减全抗%"},
            "光": {"减光抗%", "减全抗%"},
            "暗": {"减暗抗%", "减全抗%"},
        }.get(norm(element), set())
        return sum(as_number(value) for key, value in buffs.items() if norm(key) in aliases)

    @staticmethod
    def _crit_resist_reduction_percent(buffs: dict[str, float]) -> float:
        return max(
            [
                max(0.0, as_number(value))
                for key, value in buffs.items()
                if norm(key) in {"减致命抵抗%", "减致命一击抵抗%", "减暴击抵抗%"}
            ]
            or [0.0]
        )

    @staticmethod
    def _defense_reduction_percent(
        buffs: dict[str, float],
        attack: str = "",
        physical_weight: float = 1.0,
        magical_weight: float = 1.0,
    ) -> float:
        return defense_reduction_for_attack(
            buffs,
            attack,
            physical_weight=physical_weight,
            magical_weight=magical_weight,
        )

    @staticmethod
    def _damage_amp_percent(buffs: dict[str, float], attack: str, phys: float, magic: float) -> float:
        return amplification_for_channel(
            buffs,
            attack,
            physical_weight=phys,
            magical_weight=magic,
        )

    @staticmethod
    def _critical_extra_damage_percent(buffs: dict[str, float]) -> float:
        return sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"致命额外伤害%", "暴击额外伤害%"}
        )

    @staticmethod
    def _independent_damage_multiplier(buffs: dict[str, float]) -> float:
        percent = sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"独立伤害乘区%", "独立增伤%"}
        )
        return max(0.0, 1 + percent / 100)

    @staticmethod
    def _sniper_critical_extra_multiplier(buffs: dict[str, float], effective_crit_rate: float) -> float:
        probability = sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"致命暴击概率%", "狙翎致命暴击概率%"}
        )
        extra_damage = sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"致命暴击额外伤害%", "狙翎致命暴击额外伤害%"}
        )
        if probability <= 0 or extra_damage <= 0:
            return 1.0
        return 1 + effective_crit_rate * probability / 100 * extra_damage / 100

    @staticmethod
    def _hp_conditional_damage_multiplier(buffs: dict[str, float]) -> float:
        high_hp_coeff = sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"生命高于30额外系数%", "高血量额外系数%"}
        )
        low_hp_coeff = sum(
            as_number(value)
            for key, value in buffs.items()
            if norm(key) in {"生命低于50额外系数%", "低血量额外系数%"}
        )
        high_hp_average = high_hp_coeff * 24.5
        low_hp_average = low_hp_coeff * 37.5
        return (1 + high_hp_average / 100) * (1 + low_hp_average / 100)

    @staticmethod
    def _element_resists(build: dict[str, Any]) -> dict[str, float]:
        old_value = as_number(build.get("element_resist_percent", build.get("element_resist", 0)))
        values = {"火": old_value, "水": old_value, "光": old_value, "暗": old_value}
        raw = build.get("element_resists")
        if isinstance(raw, dict):
            for element in values:
                if element in raw:
                    values[element] = as_number(raw.get(element))
        return values

    def _sum_categories(self, categories: dict[str, dict[str, float]]) -> dict[str, float]:
        total: dict[str, float] = defaultdict(float)
        for bucket in categories.values():
            for attr, amount in bucket.items():
                total[attr] += amount
        return total

    def _actual_attributes(self, pre_special: dict[str, float], special: dict[str, float], *, attributes: Iterable[str] | None = None) -> dict[str, float]:
        actual: dict[str, float] = defaultdict(float)
        attrs = attributes if attributes is not None else set(self.store.attribute_order) | set(pre_special) | set(special)
        for attr in attrs:
            base = get_attr(pre_special, attr)
            extra = get_attr(special, attr)
            if is_percent_attr(attr):
                actual[attr] = base + extra
                continue
            percent_attr = PERCENT_ATTR.get(attr)
            percent = get_display_attr(pre_special, percent_attr) if percent_attr else 0.0
            actual[attr] = base * (1 + percent / 100) + extra
        return actual

    @staticmethod
    def _truncate_fixed_attrs(bucket: dict[str, float]) -> None:
        for attr, amount in list(bucket.items()):
            if not is_percent_attr(attr):
                bucket[attr] = trunc(amount)

    def _convert_attributes(self, actual: dict[str, float], pre_special: dict[str, float], first_job: str, second_job: str, *, targets: dict[str, list[str]] | None = None) -> dict[str, float]:
        out = defaultdict(float, actual)
        job = self.store.job_family(second_job, first_job)
        for target, display_attrs in (FLAT_TARGETS if targets is None else targets).items():
            delta = 0.0
            for source in SOURCE_ATTRS:
                delta += get_attr(actual, source) * self.conversion.get((job, source, target), 0.0)
            for display_attr in display_attrs:
                pct = get_display_attr(pre_special, PERCENT_ATTR.get(display_attr, ""))
                out[display_attr] += delta * (1 + pct / 100)
        return out

    def _actual_with_source_buffs(self, actual: dict[str, float], pre_special: dict[str, float], buffs: dict[str, float]) -> dict[str, float]:
        out = defaultdict(float, actual)
        for attr in SOURCE_ATTRS:
            fixed = get_attr(buffs, attr)
            percent = get_attr(buffs, f"{attr}%")
            independent_percent = sum(get_attr(buffs, key) for key in INDEPENDENT_SOURCE_PERCENT_ATTRS.get(attr, ()))
            if abs(fixed) < 1e-9 and abs(percent) < 1e-9 and abs(independent_percent) < 1e-9:
                continue
            equipment_percent = get_display_attr(pre_special, PERCENT_ATTR.get(attr, ""))
            base_after_equipment = get_attr(actual, attr)
            fixed_after_equipment = fixed * (1 + equipment_percent / 100)
            out[attr] = (base_after_equipment + fixed_after_equipment) * (1 + percent / 100) * (1 + independent_percent / 100)
        return out

    @staticmethod
    def _core_source_value(values: dict[str, float], attr: str) -> float:
        if attr == "物攻":
            return (get_attr(values, "最小物攻") + get_attr(values, "最大物攻")) / 2
        if attr == "魔攻":
            return (get_attr(values, "最小魔攻") + get_attr(values, "最大魔攻")) / 2
        return get_attr(values, attr)

    def _buff_values(self, build: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        neutral_sources: set[str] = set()
        for item in self._exclusive_filtered_buffs(build.get("buffs", [])):
            rows = self._buff_rows(item.get("skill"), item.get("level"))
            if rows:
                first = rows[0]
                source, skill = norm(first.get('来源')), norm(first.get('技能'))
                passive = neutral_passives().get(source)
                if passive and skill in {passive, f'{passive}（常驻无属性）'}:
                    neutral_sources.add(source)
                rule = self.store.buff_activation_rule(
                    first.get("来源"), first.get("技能"), first.get("类别")
                )
                # A skill-scoped effect is valued by the timeline only while
                # its trigger window is active. Applying it here would turn it
                # into an unconditional whole-build multiplier.
                if rule.get("skill_scope"):
                    continue
            for row in rows:
                if norm(row.get('属性')) == '无属性%' and norm(row.get('来源')) in neutral_sources and '常驻无属性' in norm(row.get('技能')):
                    continue
                add_attr(out, row.get("属性"), row.get("表格数值"))
        if neutral_sources:
            # Different aliases/trigger rows of one mastery must not grant 10%.
            add_attr(out, '无属性%', 5 * len(neutral_sources))
        for attr, value in build.get("extra_buff_attrs", {}).items():
            add_attr(out, attr, value)
        return out

    def _exclusive_filtered_buffs(self, buffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best_exact: dict[str, dict[str, Any]] = {}
        best_group: dict[str, dict[str, Any]] = {}
        group_order: list[str] = []
        plain_order: list[str] = []
        for item in buffs:
            skill = norm(item.get("skill"))
            if not skill or skill == "无":
                continue
            rows = self._buff_rows(skill, item.get("level"))
            group = buff_exclusive_group(rows[0]) if rows else ""
            level = as_number(item.get("level"))
            if group:
                if group not in group_order:
                    group_order.append(group)
                current = best_group.get(group)
                if current is None or level > as_number(current.get("level")):
                    best_group[group] = {"skill": skill, "level": item.get("level")}
                continue
            if skill not in plain_order:
                plain_order.append(skill)
            current = best_exact.get(skill)
            if current is None or level > as_number(current.get("level")):
                best_exact[skill] = {"skill": skill, "level": item.get("level")}
        return [best_group[key] for key in group_order if key in best_group] + [
            best_exact[key] for key in plain_order if key in best_exact
        ]

    def _buff_rows(self, skill: Any, level: Any) -> list[dict[str, Any]]:
        selector = norm(skill)
        level_text = norm(level)
        key = self.buff_label_to_key.get(selector)
        if key:
            return self.buff_by_key.get((key, level_text), [])
        return self.buff_by_name.get((selector, level_text), [])

    def _apply_buffs(
        self,
        converted: dict[str, float],
        buffs: dict[str, float],
        skip_attrs: set[str] | None = None,
        percent_exempt: dict[str, float] | None = None,
        *,
        attributes: Iterable[str] | None = None,
    ) -> dict[str, float]:
        skipped = skip_attrs or set()
        exempt = percent_exempt or {}
        out = defaultdict(float, converted)
        for attr in self.store.attribute_order if attributes is None else attributes:
            if attr in skipped:
                out[attr] = get_attr(converted, attr)
                continue
            if attr in ELEMENT_ATTRS:
                out[attr] = get_attr(converted, attr) + get_attr(buffs, attr)
            elif attr.endswith("%"):
                out[attr] = get_attr(converted, attr)
            else:
                base = DISPLAY_TO_TARGET.get(attr, attr)
                fixed = get_attr(buffs, attr)
                if base != attr:
                    fixed += get_attr(buffs, base)
                percent = (
                    attack_increase_for_channel(buffs, base)
                    if base in {"物攻", "魔攻"}
                    else get_attr(buffs, f"{base}%")
                )
                exempt_fixed = get_attr(exempt, attr)
                amplified_fixed = skill_percent_amplified_flat_attack(buffs, base)
                if fixed >= 0:
                    amplified_fixed = min(max(0.0, amplified_fixed), fixed)
                else:
                    amplified_fixed = max(min(0.0, amplified_fixed), fixed)
                post_percent_fixed = fixed - amplified_fixed
                value = (
                    (get_attr(converted, attr) - exempt_fixed + amplified_fixed)
                    * (1 + percent / 100)
                    + exempt_fixed
                    + post_percent_fixed
                )
                if base == "魔攻":
                    value += get_attr(converted, "力量") * get_attr(buffs, "力量转魔攻%") / 100
                    value += get_attr(converted, "智力") * get_attr(buffs, "智力转魔攻%") / 100
                elif base == "物攻":
                    value += get_attr(converted, "智力") * get_attr(buffs, "智力转物攻%") / 100
                out[attr] = trunc(value)
        return out

    def _effective_panel(self, second_job: str, build: dict[str, Any], buffed: dict[str, float], buff_values: dict[str, float] | None = None) -> dict[str, float]:
        row = self.effective.get(norm(second_job), {})
        physical_min = get_attr(buffed, "最小物攻")
        physical_max = get_attr(buffed, "最大物攻")
        magic_min = get_attr(buffed, "最小魔攻")
        magic_max = get_attr(buffed, "最大魔攻")
        phys = (physical_min + physical_max) / 2
        magic = (magic_min + magic_max) / 2
        buffs = buff_values or {}
        final_percent = self.final_percent(get_attr(buffed, "最终")) + self._free_percent_bonus(build, "最终") + self._panel_percent_bonus(buffs, "最终")
        crit_percent = min(round(get_attr(buffed, "致命") / 433, 1) + self._free_percent_bonus(build, "致命") + self._panel_percent_bonus(buffs, "致命"), 90)
        crit_damage_percent = min(round(get_attr(buffed, "致命伤害") / 62500 * 100, 3) + self._free_percent_bonus(build, "致命伤害") + self._panel_percent_bonus(buffs, "致命伤害"), 100)
        final_rate = final_percent / 100
        crit_rate = crit_percent / 100
        crit_resist_value = as_number(build.get("crit_resist_value", build.get("crit_resist", 0)))
        crit_resist_reduction_percent = self._crit_resist_reduction_percent(buffs)
        effective_crit_resist_value = max(0.0, crit_resist_value * (1 - min(100.0, crit_resist_reduction_percent) / 100))
        crit_resist_percent = min(round(effective_crit_resist_value / 433, 1), 90)
        crit_resist = crit_resist_percent / 100
        crit_damage = 2 + crit_damage_percent / 100
        effective_crit_rate = crit_rate * max(0, 1 - crit_resist)
        crit_multiplier = 1 + effective_crit_rate * (crit_damage - 1)
        crit_extra_multiplier = 1 + effective_crit_rate * self._critical_extra_damage_percent(buffs) / 100
        sniper_crit_extra_multiplier = self._sniper_critical_extra_multiplier(buffs, effective_crit_rate)
        hp_conditional_multiplier = self._hp_conditional_damage_multiplier(buffs)
        independent_damage_multiplier = self._independent_damage_multiplier(buffs)
        monster_defense = as_number(build.get("monster_defense"))
        defense_reduction_percent = self._defense_reduction_percent(
            buffs, physical_weight=phys, magical_weight=magic
        )
        effective_monster_defense = max(0.0, monster_defense * (1 - defense_reduction_percent / 100))
        monster_defense_rate = self.defense_percent(effective_monster_defense)
        element_resists = self._element_resists(build)
        element_effective_resists: dict[str, float] = {}
        for element in ("火", "水", "光", "暗"):
            element_effective_resists[element] = element_resists[element] - self._element_resist_reduction(buffs, element)
        total = 0.0
        weight_total = 0.0
        for i in range(1, 4):
            attack = norm(row.get(f"攻击{i}"))
            element = norm(row.get(f"属性{i}"))
            weight = as_number(row.get(f"权重{i}"))
            fixed = as_number(row.get(f"固定{i}"))
            extra_final = as_number(row.get(f"额外最终{i}"))
            if not attack or weight <= 0:
                continue
            panel = phys if attack == "物攻" else magic if attack == "魔攻" else phys + magic if attack == "双攻" else 0
            elem_rate = {"火": "火攻%", "水": "水攻%", "光": "光攻%", "暗": "暗攻%"}.get(element, "无属性%")
            effective_resist = element_effective_resists.get(element, 0.0)
            element_multiplier = 1.0
            if elem_rate:
                element_multiplier = (1 + get_attr(buffed, elem_rate) / 100) * (1 - effective_resist / 100)
            damage_amp_multiplier = 1 + self._damage_amp_percent(buffs, attack, phys, magic) / 100
            attack_defense_reduction = self._defense_reduction_percent(
                buffs, attack, phys, magic
            )
            attack_defense = max(
                0.0,
                monster_defense * (1 - attack_defense_reduction / 100),
            )
            defense_multiplier = max(
                0.0,
                1 - self.defense_percent(attack_defense) / 100,
            )
            total += (panel * weight / 100 + fixed) * (1 + final_rate + extra_final / 100) * crit_multiplier * crit_extra_multiplier * sniper_crit_extra_multiplier * hp_conditional_multiplier * independent_damage_multiplier * element_multiplier * damage_amp_multiplier * defense_multiplier
            weight_total += weight
        effective = total / (weight_total / 100) if weight_total else 0.0
        return {
            # Team-score survival normalization uses the same standardized
            # build as output scoring.  Keeping HP in the result avoids a
            # second build calculation for every player.
            "HP": get_attr(buffed, "HP"),
            "effective_panel": effective,
            "final_percent": final_percent,
            "crit_percent": crit_percent,
            "crit_damage_percent": crit_damage_percent,
            "crit_resist_percent": crit_resist_percent,
            "effective_crit_resist_value": effective_crit_resist_value,
            "crit_resist_reduction_percent": crit_resist_reduction_percent,
            "monster_defense": monster_defense,
            "effective_monster_defense": effective_monster_defense,
            "defense_reduction_percent": defense_reduction_percent,
            "monster_defense_rate": monster_defense_rate,
            "element_resist_percent": element_resists["火"] if len(set(element_resists.values())) == 1 else 0.0,
            "physical_min": physical_min,
            "physical_max": physical_max,
            "physical_average": phys,
            "magic_min": magic_min,
            "magic_max": magic_max,
            "magic_average": magic,
            "火攻%": get_attr(buffed, "火攻%"),
            "水攻%": get_attr(buffed, "水攻%"),
            "光攻%": get_attr(buffed, "光攻%"),
            "暗攻%": get_attr(buffed, "暗攻%"),
            "无属性%": get_attr(buffed, "无属性%"),
            "火抗%": element_resists["火"],
            "水抗%": element_resists["水"],
            "光抗%": element_resists["光"],
            "暗抗%": element_resists["暗"],
            "火有效抗%": element_effective_resists["火"],
            "水有效抗%": element_effective_resists["水"],
            "光有效抗%": element_effective_resists["光"],
            "暗有效抗%": element_effective_resists["暗"],
            "增伤%": get_attr(buffs, "增伤%"),
            "物理增伤%": get_attr(buffs, "物理增伤%"),
            "魔法增伤%": get_attr(buffs, "魔法增伤%"),
            "致命额外伤害%": self._critical_extra_damage_percent(buffs),
            "致命暴击期望收益%": (sniper_crit_extra_multiplier - 1) * 100,
            "奉献乌鸦平均收益%": get_attr(buffs, "生命高于30额外系数%") * 24.5,
            "九天平均收益%": get_attr(buffs, "生命低于50额外系数%") * 37.5,
            "独立伤害乘区%": (independent_damage_multiplier - 1) * 100,
        }

    def card_attrs(self, name: str, grade: str) -> dict[str, float]:
        return dict(self.card_lookup.get(f"{norm(name)}|{norm(grade) or '无'}", {}))

    def glyph_a_attrs(self, item: dict[str, Any]) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for row in self.glyph_a.get((norm(item.get("level")), norm(item.get("name"))), []):
            add_attr(bucket, row.get("属性"), row.get("数值"))
        extra = norm(item.get("extra"))
        if extra and extra != "无":
            for key in option_keys(extra):
                for row in self.glyph_a_extra.get((norm(item.get("level")), key), []):
                    add_attr(bucket, row.get("属性"), row.get("数值"))
        return dict(bucket)

    def glyph_s_attrs(self, item: dict[str, Any]) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for attr_key, rank_key in GLYPH_S_BASE_FIELDS:
            attr, rank = norm(item.get(attr_key)), norm(item.get(rank_key))
            if attr and attr != "无" and rank and rank != "无":
                for row in self.glyph_s.get((norm(item.get("level")), norm(item.get("name")), attr, rank), []):
                    add_attr(bucket, row.get("属性"), row.get("数值"))
        extra = norm(item.get("extra"))
        if extra and extra != "无":
            for key in option_keys(extra):
                for row in self.glyph_s_extra.get((norm(item.get("level")), norm(item.get("name")), key), []):
                    add_attr(bucket, row.get("属性"), row.get("数值"))
        return dict(bucket)

    def rune_attrs_for_item(self, rune: dict[str, Any], boost_percent: Any = 0) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        level, rune_type = norm(rune.get("level")), norm(rune.get("type"))
        boost = 1 + as_number(boost_percent) / 100
        for entry in rune.get("entries", []):
            amount = self.rune_normal.get((level, rune_type, norm(entry.get("attr")), norm(entry.get("rank"))), 0)
            add_attr(bucket, entry.get("attr"), amount * boost)
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def equipment_attrs(self, item: dict[str, Any], first_job: str, second_job: str) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        first, second = norm(first_job), norm(second_job)
        self._add_equipment(bucket, {"equipment": [item]}, [second, first, self.store.job_family(second, first)], Counter())
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def equipment_set_bonus_attrs(self, equipment: list[dict[str, Any]], first_job: str, second_job: str) -> dict[str, float]:
        set_counts, _members = self._equipment_set_counts(
            equipment, first_job, second_job
        )
        bucket: dict[str, float] = defaultdict(float)
        self._add_set_bonus(bucket, set_counts)
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def _equipment_set_counts(
        self,
        equipment: list[dict[str, Any]],
        first_job: str,
        second_job: str,
    ) -> tuple[Counter[int], dict[int, list[str]]]:
        set_counts: Counter[int] = Counter()
        members: dict[int, list[str]] = defaultdict(list)
        first, second = norm(first_job), norm(second_job)
        jobs = [second, first, self.store.job_family(second, first)]
        for item in equipment:
            name = norm(item.get("name"))
            if not name or name == "无":
                continue
            for job in jobs:
                if not job:
                    continue
                if self.base.get((job, name), []):
                    set_id = self.equipment_set_ids.get((job, name))
                    if set_id:
                        set_counts[set_id] += 1
                        members[set_id].append(name)
                    break
        return set_counts, dict(members)

    def jewelry_attrs(self, item: dict[str, Any]) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        self._add_jewelry(bucket, {"jewelry": [item]}, Counter())
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def jewelry_set_bonus_attrs(self, jewelry: list[dict[str, Any]]) -> dict[str, float]:
        set_counts, _members = self._jewelry_set_counts(jewelry)
        bucket: dict[str, float] = defaultdict(float)
        self._add_set_bonus(bucket, set_counts)
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def _jewelry_set_counts(
        self, jewelry: list[dict[str, Any]]
    ) -> tuple[Counter[int], dict[int, list[str]]]:
        set_counts: Counter[int] = Counter()
        members: dict[int, list[str]] = defaultdict(list)
        for item in jewelry:
            slot, name = norm(item.get("slot")), norm(item.get("name"))
            slot = "戒指" if slot.startswith("戒指") else slot
            if not name or name == "无":
                continue
            set_id = self.jewelry_set_ids.get((slot, name))
            if set_id:
                set_counts[set_id] += 1
                members[set_id].append(name)
        return set_counts, dict(members)

    def set_bonus_breakdown(
        self,
        equipment: list[dict[str, Any]],
        jewelry: list[dict[str, Any]],
        first_job: str,
        second_job: str,
    ) -> list[dict[str, Any]]:
        """Return the exact set tiers applied by the normal build calculator."""
        equipment_counts, equipment_members = self._equipment_set_counts(
            equipment, first_job, second_job
        )
        jewelry_counts, jewelry_members = self._jewelry_set_counts(jewelry)
        result: list[dict[str, Any]] = []
        for scope, counts, members in (
            ("装备", equipment_counts, equipment_members),
            ("首饰", jewelry_counts, jewelry_members),
        ):
            for set_id, count in sorted(counts.items()):
                attrs: dict[str, float] = defaultdict(float)
                tiers: set[int] = set()
                for row in self.set_bonus.get(set_id, []):
                    required = int(as_number(row.get("件数")))
                    if required <= count:
                        tiers.add(required)
                        add_attr(attrs, row.get("属性名称"), row.get("属性数值"))
                self._truncate_fixed_attrs(attrs)
                result.append({
                    "scope": scope,
                    "setId": set_id,
                    "count": count,
                    "members": list(members.get(set_id, [])),
                    "appliedTiers": sorted(tiers),
                    "attributes": dict(attrs),
                })
        return result

    def mastery_attrs(self, name: str, level: Any) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for row in self.mastery.get((norm(name), int(as_number(level))), []):
            add_attr(bucket, row.get("属性"), row.get("数值"))
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def fashion_attrs(self, name: str) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for row in self.fashion.get(norm(name), []):
            add_attr(bucket, row.get("属性"), row.get("数值"))
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def talisman_attrs(self, item: dict[str, Any]) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        grade = norm(item.get("grade"))
        name = norm(item.get("name"))
        slot = norm(item.get("slot"))
        multiplier = TALISMAN_SLOT_PERCENT.get(slot, 0) / 100.0
        if grade in {"", "无"} or name in {"", "无"} or multiplier <= 0:
            return {}
        for row in self.talisman.get((grade, name), []):
            add_attr(bucket, row.get("属性"), as_number(row.get("数值")) * multiplier)
        extra = norm(item.get("extra"))
        if extra in set(self.store.talisman_extra_options(grade, name)) - {"无"}:
            for row in self.talisman_extra.get((grade, extra), []):
                add_attr(bucket, row.get("属性"), as_number(row.get("数值")) * multiplier)
        # The game exposes fixed integer stats.  Truncate each installed
        # talisman independently instead of allowing fractions from two
        # 125% slots to combine into an extra point.
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def buff_attrs(self, skill: str, level: Any) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for row in self._buff_rows(skill, level):
            add_attr(bucket, row.get("属性"), row.get("表格数值"))
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def engraving_attrs(self, item: dict[str, Any]) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        self._add_engravings(bucket, {"engravings": [item]})
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def collection_attrs(self, level: Any) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        selected_level = int(as_number(level))
        for current_level, rows in self.collection.items():
            if current_level <= selected_level:
                for row in rows:
                    add_attr(bucket, row.get("属性"), row.get("数值"))
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def title_attrs(self, title: str) -> dict[str, float]:
        bucket: dict[str, float] = defaultdict(float)
        for row in self.title.get(norm(title), []):
            add_attr(bucket, row.get("属性"), row.get("数值"))
        self._truncate_fixed_attrs(bucket)
        return dict(bucket)

    def core_rune_preview(self, build: dict[str, Any]) -> dict[str, Any]:
        first_job = norm(build.get("first_job"))
        second_job = norm(build.get("second_job"))
        jobs = [second_job, first_job, self.store.job_family(second_job, first_job)]
        categories = {
            "base": defaultdict(float),
            "equipment": defaultdict(float),
            "card": defaultdict(float),
            "talisman": defaultdict(float),
            "glyph": defaultdict(float),
            "normal_rune": defaultdict(float),
            "core_mastery": defaultdict(float),
        }
        set_counts: Counter[int] = Counter()
        self._add_character_and_misc(categories["base"], build, second_job)
        self._add_equipment(categories["equipment"], build, jobs, set_counts)
        self._add_jewelry(categories["equipment"], build, set_counts)
        self._add_set_bonus(categories["equipment"], set_counts)
        self._add_cards(categories["card"], build)
        self._add_talismans(categories["talisman"], build)
        self._add_glyphs(categories["glyph"], build)
        self._add_normal_runes(categories["normal_rune"], build)
        for bucket in categories.values():
            self._truncate_fixed_attrs(bucket)
        pre_special = self._sum_categories(categories)
        self._truncate_fixed_attrs(pre_special)
        self._add_mastery(categories["core_mastery"], build)
        self._truncate_fixed_attrs(categories["core_mastery"])
        actual = self._actual_attributes(
            pre_special, categories["core_mastery"]
        )
        self._truncate_fixed_attrs(actual)
        converted = self._convert_attributes(actual, pre_special, first_job, second_job)
        self._truncate_fixed_attrs(converted)

        core = build.get("core_rune", {})
        level, conversion = norm(core.get("level")), norm(core.get("conversion"))
        source_attr, target_attr = self._split_conversion(conversion)
        if not level or not conversion or not source_attr or not target_attr:
            return {}
        candidates = [
            row for row in self.rune_core
            if norm(row.get("等级")) == level and norm(row.get("石板类型/转换")) == conversion
        ]
        ratio = self._core_selected_value(candidates, core.get("ratio_rank"), "转换比例", "比例档位")
        coeff = self._core_selected_value(candidates, core.get("coeff_rank"), "转换系数", "系数档位")
        max_deduct = self._average([as_number(row.get("最大扣除值")) for row in candidates])
        if ratio <= 0 or coeff <= 0:
            return {
                "source_attr": source_attr,
                "target_attr": target_attr,
                "source_value": self._core_source_value(converted, source_attr),
                "deduct": 0.0,
                "gain": 0.0,
                "max_deduct": max_deduct,
                "normal_boost": as_number(core.get("enhance")),
            }
        source_value = self._core_source_value(converted, source_attr)
        capped_source = min(source_value, max_deduct) if max_deduct > 0 else source_value
        deducted = capped_source * ratio / 100
        gain = deducted * coeff
        return {
            "source_attr": source_attr,
            "target_attr": target_attr,
            "source_value": source_value,
            "deduct": deducted,
            "gain": gain,
            "max_deduct": max_deduct,
            "normal_boost": as_number(core.get("enhance")),
        }

    @staticmethod
    def final_percent(value: float) -> float:
        return round(value / 69.25, 1) if value <= 3116.25 else 45 + round((value - 3116.25) / 161.583333333333, 1)

    @staticmethod
    def defense_percent(value: float, player_level: int = 70) -> float:
        defense = max(0.0, as_number(value))
        if defense <= 0:
            return 0.0
        base = DEFENSE_LEVEL_REDUCTION_BASE.get(player_level)
        if base is None:
            base = DEFENSE_LEVEL_REDUCTION_BASE[70]
        if base < 0:
            reduction = max(50.0, 741.0 - 0.975 * defense)
        else:
            lookup_level = min(player_level + 5, 100)
            unknown = DEFENSE_LEVEL_REDUCTION_BASE.get(lookup_level, base)
            reduction = unknown + 20.0
        defense_rate = min(defense / (defense + reduction), DEFENSE_RATE_CAP)
        return round(defense_rate * 100, 1)

    @staticmethod
    def _average(values: list[float]) -> float:
        values = [v for v in values if v]
        return sum(values) / len(values) if values else 0.0

    def _core_selected_value(self, rows: list[dict[str, Any]], selected: Any, value_key: str, rank_key: str) -> float:
        selected_text = norm(selected)
        if not selected_text:
            return 0.0
        selected_number = as_number(selected, default=float("nan"))
        values = [as_number(row.get(value_key), default=float("nan")) for row in rows]
        values = [value for value in values if value == value]
        by_value = [value for value in values if value == selected_number]
        if by_value:
            return self._average(by_value)
        max_value = max(values, default=0.0)
        is_old_rank = selected_number == selected_number and selected_number.is_integer() and 1 <= selected_number <= 21 and selected_number > max_value
        if not is_old_rank:
            return 0.0
        by_rank = [
            as_number(row.get(value_key))
            for row in rows
            if norm(row.get(rank_key)) == selected_text
        ]
        return self._average(by_rank)

    @staticmethod
    def _split_conversion(text: str) -> tuple[str, str]:
        for sep in ("→", "->", ">"):
            if sep in text:
                left, right = text.split(sep, 1)
                return norm(left), norm(right)
        return "", ""
