from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from .calculator import BuildCalculator
from .data_loader import MODULAR_DIR, TALISMAN_SLOTS, DataStore, as_number, norm


NONE_TEXT = "无"
EQUIPMENT_SLOTS = ("头盔", "上装", "下装", "手套", "鞋子", "主手", "副手")
FASHION_SLOTS = ("主手", "副手", "翅膀", "尾巴", "印花", "精灵", "项链", "耳环", "戒指1", "戒指2")
ENGRAVING_SLOTS = (
    "头盔", "上装", "下装", "手套", "鞋子", "主手", "副手",
    "项链", "耳环", "戒指1", "戒指2", "翅膀", "尾巴", "印花",
)
CARD_PREVIEW_ATTRS = (
    "HP", "MP", "体质", "力量", "敏捷", "智力", "最终", "物攻",
    "致命", "致命伤害", "防御", "魔攻", "魔防",
)
CARD_MASTERY_ATTR_MAP = {
    "破坏": "物攻", "魔力": "魔攻", "巨熊": "力量", "疾风": "敏捷",
    "智慧": "智力", "健康": "体质", "致命": "致命", "最终": "最终",
    "铁壁": "防御", "帷幕": "魔防", "生命": "HP", "法力": "MP",
}
CARD_MASTERY_NAME_BY_ATTR = {
    attr: name for name, attr in CARD_MASTERY_ATTR_MAP.items()
}
CARD_ESTIMATE_MIN_RATIO = 0.40
CARD_ESTIMATE_MAX_RATIO = 0.90
MASTERY_NAMES = (
    "最大生命值", "最大魔法值", "物理攻击力", "魔法攻击力",
    "物理防御", "魔法防御", "致命一击", "最终伤害",
)


class ProfilePanelCalculator:
    """Build a teammate panel through the same calculator used by the build page."""

    def __init__(self, store: DataStore, calculator: BuildCalculator) -> None:
        self.store = store
        self.calculator = calculator
        self._cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def profile_dict(profile: Any) -> dict[str, Any]:
        if isinstance(profile, dict):
            return copy.deepcopy(profile)
        if is_dataclass(profile):
            return asdict(profile)
        return {
            key: copy.deepcopy(getattr(profile, key))
            for key in (
                "timestamp_ms", "target_id", "target_name", "job", "level",
                "party_position", "status", "request_fingerprint",
                "response_layout", "fields", "equipment", "cards", "stats",
                "raw_payload_hex",
            )
            if hasattr(profile, key)
        }

    @staticmethod
    def _skill_defaults() -> dict[str, Any]:
        path = MODULAR_DIR / "tables" / "skills" / "skill_build_defaults_builtin.json"
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _default_account_mastery(self) -> list[dict[str, Any]]:
        """Return the highest available account-mastery level per stat."""
        maximums: dict[str, int] = {}
        for row in self.store.tables.get("mastery", []):
            name = norm(row.get("专精"))
            if not name:
                continue
            maximums[name] = max(
                maximums.get(name, 0), int(as_number(row.get("等级")))
            )
        return [
            {"name": name, "level": maximums.get(name, 0)}
            for name in MASTERY_NAMES
        ]

    def _job_pair(self, job: str) -> tuple[str, str]:
        selected = norm(job)
        for first_job in self.store.first_jobs():
            if selected in self.store.second_jobs(first_job):
                return first_job, selected
        return "", ""

    def _passive_buffs(self, job: str) -> list[dict[str, Any]]:
        defaults = self._skill_defaults().get(norm(job), {})
        if not isinstance(defaults, dict):
            return []
        normal = defaults.get("skill_build", {})
        mastery = defaults.get("skill_mastery_build", {})
        levels: dict[str, int] = {}
        for source in (normal, mastery):
            if not isinstance(source, dict):
                continue
            for skill_id, level in source.get("levels", {}).items():
                levels[str(skill_id)] = max(
                    levels.get(str(skill_id), 0), int(as_number(level))
                )
        skill_names: dict[str, str] = {}
        for mode in ("normal", "master"):
            tree = self.store.skill_build_tree(job, 70, mode=mode)
            for skill_id, row in tree.get("skills", {}).items():
                if isinstance(row, dict) and norm(row.get("name")):
                    skill_names[str(skill_id)] = norm(row.get("name"))
        learned = {
            skill_names[skill_id]: level
            for skill_id, level in levels.items()
            if level > 0 and skill_id in skill_names
        }
        result: list[dict[str, Any]] = []
        for skill, learned_level in learned.items():
            rows = [
                row for row in self.store.tables.get("buff", [])
                if norm(row.get("类别")) == "被动"
                and norm(row.get("来源")) in {norm(job), "通用"}
                and norm(row.get("技能")) == skill
            ]
            available = sorted({int(as_number(row.get("等级"))) for row in rows})
            selected = max(
                (level for level in available if level <= learned_level),
                default=learned_level,
            )
            if rows:
                result.append({"skill": skill, "level": selected})
        return result[:10]

    def blank_build(self, context_build: dict[str, Any], job: str) -> dict[str, Any]:
        build = copy.deepcopy(context_build)
        first_job, second_job = self._job_pair(job)
        if second_job:
            build["first_job"] = first_job
            build["second_job"] = second_job
        build["equipment"] = [
            {"slot": slot, "name": NONE_TEXT, "suffix": NONE_TEXT,
             "enhance": 0, "enchants": [NONE_TEXT] * 5}
            for slot in EQUIPMENT_SLOTS
        ]
        build["jewelry"] = [
            {"slot": slot, "name": NONE_TEXT, "variant": NONE_TEXT,
             "enchants": [NONE_TEXT] * 5}
            for slot in ("项链", "耳环", "戒指1", "戒指2")
        ]
        build["glyph_a"] = [
            {"level": NONE_TEXT, "name": NONE_TEXT, "extra": NONE_TEXT}
            for _ in range(11)
        ]
        build["glyph_s"] = [
            {"level": NONE_TEXT, "name": NONE_TEXT, "base_attr1": NONE_TEXT,
             "rank1": None, "base_attr2": NONE_TEXT, "rank2": None,
             "base_attr3": NONE_TEXT, "rank3": None,
             "base_attr4": NONE_TEXT, "rank4": None,
             "extra": NONE_TEXT}
            for _ in range(4)
        ]
        build["normal_runes"] = [
            {
                "type": "攻击" if index < 2 else "防御",
                "level": NONE_TEXT,
                "entries": [{"attr": NONE_TEXT, "rank": None} for _ in range(6)],
            }
            for index in range(4)
        ]
        build["core_rune"] = {
            "level": NONE_TEXT, "conversion": NONE_TEXT, "ratio_rank": None,
            "coeff_rank": None, "enhance": 0,
        }
        build["fashion"] = [
            {"slot": slot, "name": NONE_TEXT} for slot in FASHION_SLOTS
        ]
        build["talismans"] = [
            {
                "slot": slot,
                "grade": NONE_TEXT,
                "name": NONE_TEXT,
                "extra": NONE_TEXT,
            }
            for slot in TALISMAN_SLOTS
        ]
        build["engravings"] = [
            {
                "slot": slot, "grade": NONE_TEXT, "value_rank": NONE_TEXT,
                "main_value": 0.0, "secondary_value": 0.0,
                "extra": NONE_TEXT, "extra_tier": NONE_TEXT,
                "extra_value": 0.0,
            }
            for slot in ENGRAVING_SLOTS
        ]
        build["card_grades"] = [
            {"name": name, "grade": NONE_TEXT} for name in self.store.card_names()
        ]
        build["card_bonus_levels"] = {attr: 0 for attr in CARD_PREVIEW_ATTRS}
        build.pop("card_initial_totals", None)
        build["collection_level"] = 33
        build["mastery"] = self._default_account_mastery()
        build["title"] = NONE_TEXT
        build["free_inputs"] = {"percent": {}, "stats": {}, "stat_percent": {}}
        build["extra_buff_attrs"] = {}
        build["buffs"] = self._passive_buffs(second_job)
        return build

    def _job_profile(self, build: dict[str, Any]) -> dict[str, str]:
        first = norm(build.get("first_job"))
        second = norm(build.get("second_job"))
        family = self.store.job_family(second, first)
        effective = next(
            (row for row in self.store.tables.get("effective", [])
             if norm(row.get("职业")) == second),
            {},
        )
        attack = norm(effective.get("攻击1"))
        if attack == "双攻":
            attack = "物攻"
        if attack not in {"物攻", "魔攻"}:
            attack = "物攻"
        main_stat = "智力" if attack == "魔攻" else "力量"
        best_ratio = -1.0
        for row in self.store.tables.get("conversion", []):
            if norm(row.get("一转职业")) != family or norm(row.get("目标属性")) != attack:
                continue
            source = norm(row.get("来源属性"))
            ratio = as_number(row.get("转换比例"))
            if source in {"力量", "敏捷", "智力", "体质"} and ratio > best_ratio:
                main_stat, best_ratio = source, ratio
        return {"attack": attack, "main_stat": main_stat}

    def _fill_estimated_jewelry_variants(
        self, confirmed: dict[str, Any], estimated: dict[str, Any]
    ) -> list[str]:
        profile = self._job_profile(estimated)
        preferred = {
            "力量": "力敏", "敏捷": "力敏", "智力": "敏智", "体质": "智体",
        }.get(profile["main_stat"], "力敏")
        inferred: list[str] = []
        for confirmed_row, estimated_row in zip(
            confirmed.get("jewelry", []), estimated.get("jewelry", [])
        ):
            name = norm(estimated_row.get("name"))
            if not name or name == NONE_TEXT or norm(estimated_row.get("variant")) != NONE_TEXT:
                continue
            slot = "戒指" if norm(estimated_row.get("slot")).startswith("戒指") else norm(estimated_row.get("slot"))
            variants = [
                value for value in self.store.jewelry_variants(slot, name)
                if value != NONE_TEXT
            ]
            if len(variants) == 1:
                estimated_row["variant"] = variants[0]
                confirmed_row["variant"] = variants[0]
                inferred.append(f"{estimated_row.get('slot')}变体唯一：{variants[0]}")
            elif preferred in variants:
                estimated_row["variant"] = preferred
                inferred.append(f"{estimated_row.get('slot')}变体估算：{preferred}")
        return inferred

    @staticmethod
    def _profile_level(detail: Any) -> int:
        import re
        match = re.search(r"需求\s*Lv\.(\d+)", str(detail or ""), re.IGNORECASE)
        return int(match.group(1)) if match else 0

    def _card_estimate(
        self, profile: dict[str, Any], preview: dict[str, Any]
    ) -> tuple[str, int, float]:
        levels = [
            self._profile_level(detail)
            for key, detail in profile.get("equipment", [])
            if norm(key).startswith("装备|") and self._profile_level(detail) > 0
        ]
        level_score = min(1.0, sum(levels) / max(1, len(levels)) / 70) if levels else 0.5
        # Keep the estimate reproducible in both the native team-info page and
        # the lightweight teammate overlay. Mapping coverage depends on which
        # page built the preview and previously made the two displays disagree.
        coverage = (
            min(1.0, len(profile.get("equipment", [])) / 48)
            if profile.get("equipment")
            else 0.5
        )
        quality = min(1.0, max(1 / 3, level_score * 0.65 + coverage * 0.35))
        grade = "L" if quality >= 0.88 else "S" if quality >= 0.72 else "A" if quality >= 0.55 else "B" if quality >= 0.4 else "C"
        # This is only the fallback used when the card mastery archive was not
        # received. Real decoded values can legitimately be 0 or below 15;
        # estimates follow the observed end-game range supplied by the user.
        return grade, max(15, min(25, round(25 * quality))), quality

    def _observed_card_grades(self, profile: dict[str, Any]) -> dict[str, str]:
        names = self.store.card_names()
        by_folded = {norm(name).casefold(): name for name in names}
        result: dict[str, str] = {}
        for row in profile.get("cards", []):
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            name = by_folded.get(norm(row[1]).casefold(), "")
            grade = norm(row[2])
            if name and grade in {"L", "S", "A", "B", "C"}:
                result[name] = grade
        return result

    @staticmethod
    def _observed_card_bonus_levels(profile: dict[str, Any]) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in profile.get("fields", []):
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            key = norm(row[0])
            if not key.startswith("卡片掌握|"):
                continue
            mastery_name = key.split("|", 1)[1]
            attr = CARD_MASTERY_ATTR_MAP.get(mastery_name)
            if not attr:
                continue
            try:
                level = int(float(row[1]))
            except (TypeError, ValueError):
                continue
            result[attr] = max(0, min(25, level))
        if result:
            result["致命伤害"] = 0
        return result

    def card_estimate_summary(
        self,
        profile_value: Any,
        preview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return visible card totals used by the teammate-panel estimate.

        Packet values remain authoritative. Missing card archives are filled
        with one quality estimate, while missing mastery rows use a 15-25
        estimate. Every row is labelled as observed or estimated so inferred
        values are never presented as decrypted data.
        """

        profile = self.profile_dict(profile_value)
        observed_cards = self._observed_card_grades(profile)
        observed_levels = self._observed_card_bonus_levels(profile)
        grade, estimated_level, quality = self._card_estimate(
            profile, preview if isinstance(preview, dict) else {}
        )
        # Missing card archives only give us a range, not one universal
        # account value. Position each character inside the user-calibrated
        # 40%-90% range with that profile's equipment quality. ``quality`` is
        # already derived from the character's item requirements and archive
        # coverage and is clamped to 1/3..1, so map that interval linearly to
        # the complete card range. This keeps the estimate deterministic while
        # preventing every inspected teammate from receiving the old 65% sum.
        quality_position = min(
            1.0, max(0.0, (quality - 1 / 3) / (1 - 1 / 3))
        )
        initial_estimate_ratio = (
            CARD_ESTIMATE_MIN_RATIO
            + (CARD_ESTIMATE_MAX_RATIO - CARD_ESTIMATE_MIN_RATIO)
            * quality_position
        )
        card_names = self.store.card_names()
        card_grades = [
            {
                "name": name,
                "grade": observed_cards.get(name, NONE_TEXT),
            }
            for name in card_names
        ] if observed_cards else [
            {"name": name, "grade": NONE_TEXT} for name in card_names
        ]

        maximum_totals = {attr: 0.0 for attr in CARD_PREVIEW_ATTRS}
        for name in card_names:
            attrs = self.calculator.card_attrs(name, "L")
            for attr in CARD_PREVIEW_ATTRS:
                maximum_totals[attr] += as_number(attrs.get(attr))

        observed_totals = {attr: 0.0 for attr in CARD_PREVIEW_ATTRS}
        for item in card_grades:
            attrs = self.calculator.card_attrs(
                norm(item.get("name")), norm(item.get("grade"))
            )
            for attr in CARD_PREVIEW_ATTRS:
                observed_totals[attr] += as_number(attrs.get(attr))

        initial_totals = {
            attr: (
                observed_totals[attr]
                if observed_cards
                else maximum_totals[attr] * initial_estimate_ratio
            )
            for attr in CARD_PREVIEW_ATTRS
        }

        mastery_levels = {
            attr: (
                0
                if attr == "致命伤害"
                else int(observed_levels[attr])
                if attr in observed_levels
                else estimated_level
            )
            for attr in CARD_PREVIEW_ATTRS
        }
        rows = []
        for attr in CARD_PREVIEW_ATTRS:
            level = mastery_levels[attr]
            initial = initial_totals[attr]
            initial_minimum = (
                initial
                if observed_cards
                else maximum_totals[attr] * CARD_ESTIMATE_MIN_RATIO
            )
            initial_maximum = (
                initial
                if observed_cards
                else maximum_totals[attr] * CARD_ESTIMATE_MAX_RATIO
            )
            rows.append({
                "name": CARD_MASTERY_NAME_BY_ATTR.get(attr, "爆伤"),
                "attr": attr,
                "initial": initial,
                "initialMin": initial_minimum,
                "initialMax": initial_maximum,
                "level": level,
                "boosted": initial * (1 + level * 0.05),
                "levelSource": (
                    "fixed"
                    if attr == "致命伤害"
                    else "observed"
                    if attr in observed_levels
                    else "estimated"
                ),
            })
        return {
            "cardsSource": "observed" if observed_cards else "estimated",
            "observedCards": len(observed_cards),
            "totalCards": len(card_names),
            "estimatedGrade": grade,
            "estimatedMasteryLevel": estimated_level,
            "quality": quality,
            "initialEstimateMinRatio": CARD_ESTIMATE_MIN_RATIO,
            "initialEstimateMaxRatio": CARD_ESTIMATE_MAX_RATIO,
            "initialEstimateExpectedRatio": initial_estimate_ratio,
            "initialTotals": initial_totals,
            "cardGrades": card_grades,
            "masteryLevels": mastery_levels,
            "rows": rows,
        }

    @staticmethod
    def _panel_metrics(
        rows: list[Any], summary: dict[str, float]
    ) -> dict[str, float]:
        row_map = {norm(row.attr): as_number(row.buffed) for row in rows}
        return {
            "effective_panel": as_number(summary.get("effective_panel")),
            "physical_attack_min": as_number(summary.get("physical_min")),
            "physical_attack_max": as_number(summary.get("physical_max")),
            "physical_attack": as_number(summary.get("physical_average")),
            "magic_attack_min": as_number(summary.get("magic_min")),
            "magic_attack_max": as_number(summary.get("magic_max")),
            "magic_attack": as_number(summary.get("magic_average")),
            "final_percent": as_number(summary.get("final_percent")),
            "crit_percent": as_number(summary.get("crit_percent")),
            "crit_damage_percent": as_number(summary.get("crit_damage_percent")),
            "HP": as_number(summary.get("HP", row_map.get("HP"))),
            "strength": row_map.get("力量", 0.0),
            "agility": row_map.get("敏捷", 0.0),
            "intelligence": row_map.get("智力", 0.0),
            "vitality": row_map.get("体质", 0.0),
            "physical_defense": row_map.get("防御", 0.0),
            "magic_defense": row_map.get("魔防", 0.0),
            "fire_attack": as_number(summary.get("火攻%")),
            "water_attack": as_number(summary.get("水攻%")),
            "light_attack": as_number(summary.get("光攻%")),
            "dark_attack": as_number(summary.get("暗攻%")),
        }

    @staticmethod
    def _profile_section(detail: Any, label: str) -> str:
        match = re.search(
            rf"(?:^|；){re.escape(label)}\s*[:：]\s*([^；]+)",
            str(detail or ""),
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _profile_item_name(detail: Any) -> str:
        return str(detail or "").split("；", 1)[0].strip()

    @staticmethod
    def _exact_profile_attr(label: str, percent: bool) -> str:
        text = norm(label).replace("属性", "")
        aliases = {
            "最小物理攻击力": "最小物攻",
            "最大物理攻击力": "最大物攻",
            "物理攻击力": "物攻",
            "最小魔法攻击力": "最小魔攻",
            "最大魔法攻击力": "最大魔攻",
            "魔法攻击力": "魔攻",
            "最大生命值": "HP",
            "生命值": "HP",
            "最大魔法值": "MP",
            "魔法值": "MP",
            "火攻击": "火攻",
            "水攻击": "水攻",
            "冰攻击": "水攻",
            "光攻击": "光攻",
            "暗攻击": "暗攻",
            "火属性攻击": "火攻",
            "水属性攻击": "水攻",
            "冰属性攻击": "水攻",
            "光属性攻击": "光攻",
            "暗属性攻击": "暗攻",
            "火抵抗": "火防",
            "水抵抗": "水防",
            "光抵抗": "光防",
            "暗抵抗": "暗防",
            "火属性抵抗": "火防",
            "水属性抵抗": "水防",
            "光属性抵抗": "光防",
            "暗属性抵抗": "暗防",
            "物理防御": "防御",
            "魔法防御": "魔防",
            "致命一击": "致命",
            "致命一击抵抗": "致命抵抗",
            "最终伤害": "最终",
            "魔法恢复": "MP恢复",
        }
        attr = aliases.get(text, text)
        if percent and not attr.endswith("%"):
            attr = f"{attr}%"
        return attr

    @classmethod
    def _exact_profile_attributes(
        cls, text: Any, source: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cleaned = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", str(text or ""))
        for part in re.split(r"[，,]", cleaned):
            match = re.match(r"\s*(.+?)\s+\+?(-?\d+(?:\.\d+)?)(%)?\s*$", part)
            if not match:
                continue
            percent = bool(match.group(3))
            rows.append({
                "attr": cls._exact_profile_attr(match.group(1), percent),
                "value": float(match.group(2)),
                "percent": percent,
                "source": source,
            })
        return rows

    @classmethod
    def _server_jewelry_option(
        cls, detail: str
    ) -> tuple[str, list[dict[str, Any]]]:
        match = re.search(
            r"首饰选项\s*[:：]\s*([^（(；;]+)(?:[（(]([^）)]*)[）)])?",
            detail,
        )
        if not match:
            return NONE_TEXT, []
        return (
            norm(match.group(1)) or NONE_TEXT,
            cls._exact_profile_attributes(match.group(2) or "", "首饰选项"),
        )

    @classmethod
    def _server_suffix_attributes(cls, detail: str) -> list[dict[str, Any]]:
        match = re.search(
            r"(?:^|；)后缀\s*[:：]\s*([^（(；;]+)(?:[（(]([^）)]*)[）)])?",
            detail,
        )
        if not match or not norm(match.group(2)):
            return []
        suffix = norm(match.group(1)) or "后缀"
        return cls._exact_profile_attributes(match.group(2), f"后缀·{suffix}")

    @staticmethod
    def _jewelry_attr_text(rows: list[dict[str, Any]]) -> str:
        grouped: dict[str, list[str]] = {}
        for row in rows:
            source = norm(row.get("source")) or "属性"
            value = as_number(row.get("value"))
            number = f"{value:g}"
            suffix = "%" if row.get("percent") else ""
            grouped.setdefault(source, []).append(
                f"{norm(row.get('attr')).rstrip('%')} +{number}{suffix}"
            )
        return "；".join(
            f"{source}：{'，'.join(values)}" for source, values in grouped.items()
        ) or "服务器正文未返回可拆分数值"

    @classmethod
    def _server_attribute_values(
        cls, rows: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Collapse packet min/max attack percentages to calculator semantics."""
        values: dict[str, float] = {}
        attack_percent: dict[str, list[float]] = {
            "物攻%": [],
            "魔攻%": [],
        }
        for row in rows:
            attr = norm(row.get("attr"))
            value = as_number(row.get("value"))
            family = cls._attribute_family(attr)
            if attr == "物攻":
                values["最小物攻"] = as_number(values.get("最小物攻")) + value
                values["最大物攻"] = as_number(values.get("最大物攻")) + value
                continue
            if attr == "魔攻":
                values["最小魔攻"] = as_number(values.get("最小魔攻")) + value
                values["最大魔攻"] = as_number(values.get("最大魔攻")) + value
                continue
            if attr in {"最小物攻%", "最大物攻%"}:
                attack_percent["物攻%"].append(value)
                continue
            if attr in {"最小魔攻%", "最大魔攻%"}:
                attack_percent["魔攻%"].append(value)
                continue
            values[family] = as_number(values.get(family)) + value
        for attr, entries in attack_percent.items():
            if entries:
                values[attr] = as_number(values.get(attr)) + sum(entries) / len(entries)
        return values

    @staticmethod
    def _attribute_family(attr: str) -> str:
        name = norm(attr)
        aliases = {
            "最小物攻%": "物攻%", "最大物攻%": "物攻%",
            "最小魔攻%": "魔攻%", "最大魔攻%": "魔攻%",
            "最小物攻": "最小物攻", "最大物攻": "最大物攻",
            "最小魔攻": "最小魔攻", "最大魔攻": "最大魔攻",
        }
        return aliases.get(name, name)

    def _mapped_enchant_families(self, item: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        for enchant in item.get("enchants", []):
            for row in self.calculator.enchant.get(norm(enchant), []):
                result.add(self._attribute_family(norm(row.get("属性名称"))))
        return result

    def _equipment_details(
        self,
        profile: dict[str, Any],
        build: dict[str, Any],
        preview: dict[str, Any],
    ) -> list[dict[str, Any]]:
        build_by_slot = {
            norm(row.get("slot")): row for row in build.get("equipment", [])
        }
        mapping_by_slot = {
            re.sub(r"\s+", "", norm(row.get("item"))): row
            for row in preview.get("items", [])
            if row.get("category") == "装备"
        }
        aliases = {"主武器": "主手", "副武器": "副手"}
        details: list[dict[str, Any]] = []
        for raw_key, raw_detail in profile.get("equipment", []):
            category, separator, raw_slot = str(raw_key or "").partition("|")
            slot_key = re.sub(r"\s+", "", norm(raw_slot))
            slot = aliases.get(slot_key, slot_key)
            if not separator or norm(category) != "装备" or slot not in EQUIPMENT_SLOTS:
                continue
            detail = norm(raw_detail)
            attributes: list[dict[str, Any]] = []
            for label in ("基础属性", "强化属性", "当前附加"):
                attributes.extend(self._exact_profile_attributes(
                    self._profile_section(detail, label), label
                ))
            attributes.extend(self._server_suffix_attributes(detail))
            attributes.extend(self._exact_profile_attributes(
                self._profile_section(detail, "附魔"), "附魔"
            ))

            mapped_item = build_by_slot.get(slot, {})
            mapping = mapping_by_slot.get(slot, {})
            mapped_name = (
                mapping.get("status") == "matched"
                and norm(mapped_item.get("name")) not in {"", NONE_TEXT}
            )
            simulator_attrs = self.calculator.equipment_attrs(
                mapped_item,
                norm(build.get("first_job")),
                norm(build.get("second_job")),
            ) if mapped_item else {}
            server_values = self._server_attribute_values(attributes)
            simulator_values = self._server_attribute_values([
                {"attr": attr, "value": value}
                for attr, value in simulator_attrs.items()
            ])
            fallback_attributes: list[dict[str, Any]] = []
            # Once the packet supplied concrete item attributes, replace the
            # mapped item's complete contribution. Set bonuses remain separate.
            compared_attrs = set(server_values) | (
                set(simulator_values) if server_values else set()
            )
            for attr in sorted(compared_attrs):
                server_value = as_number(server_values.get(attr))
                simulator_value = as_number(simulator_values.get(attr))
                delta = server_value - simulator_value
                if abs(delta) > 1e-6:
                    fallback_attributes.append({
                        "attr": attr,
                        "value": delta,
                        "serverValue": server_value,
                        "simulatorValue": simulator_value,
                        "source": "装备实际属性差额",
                    })
            status = (
                "装备库已映射，服务器实际数值优先"
                if mapped_name
                else "装备库缺项，直接采用服务器实际数值"
            )
            if not attributes:
                status = "服务器正文未返回可计算属性"
            elif fallback_attributes:
                status = f"{status}，补正 {len(fallback_attributes)} 项"
            details.append({
                "slot": slot,
                "name": self._profile_item_name(detail),
                "mappedName": norm(mapped_item.get("name")),
                "serverAttributes": attributes,
                "serverText": self._jewelry_attr_text(attributes),
                "simulatorText": "，".join(
                    f"{attr} +{as_number(value):g}"
                    for attr, value in simulator_attrs.items()
                ) or "配装库没有可用的同名装备属性",
                "status": status,
                "fallbackAttributes": fallback_attributes,
            })
        return details

    @staticmethod
    def _simulator_attr_value(attrs: dict[str, float], attr: str) -> float:
        name = norm(attr)
        if name == "物攻":
            return (
                as_number(attrs.get("最小物攻"))
                + as_number(attrs.get("最大物攻"))
            ) / 2
        if name == "魔攻":
            return (
                as_number(attrs.get("最小魔攻"))
                + as_number(attrs.get("最大魔攻"))
            ) / 2
        if name == "物攻%":
            aliases = [
                as_number(attrs.get(key))
                for key in ("最小物攻%", "最大物攻%")
                if key in attrs
            ]
            return as_number(attrs.get(name)) + (
                sum(aliases) / len(aliases) if aliases else 0.0
            )
        if name == "魔攻%":
            aliases = [
                as_number(attrs.get(key))
                for key in ("最小魔攻%", "最大魔攻%")
                if key in attrs
            ]
            return as_number(attrs.get(name)) + (
                sum(aliases) / len(aliases) if aliases else 0.0
            )
        return as_number(attrs.get(name))

    def _jewelry_details(
        self,
        profile: dict[str, Any],
        build: dict[str, Any],
        preview: dict[str, Any],
    ) -> list[dict[str, Any]]:
        build_by_slot = {
            norm(row.get("slot")): row for row in build.get("jewelry", [])
        }
        mapping_by_slot = {
            re.sub(r"\s+", "", norm(row.get("item"))): row
            for row in preview.get("items", [])
            if row.get("category") == "首饰"
        }
        ring_index = 0
        details: list[dict[str, Any]] = []
        for raw_key, raw_detail in profile.get("equipment", []):
            category, separator, raw_slot = str(raw_key or "").partition("|")
            slot_key = re.sub(r"\s+", "", norm(raw_slot))
            if not separator or norm(category) != "装备":
                continue
            if slot_key not in {"项链", "耳环", "戒指", "戒指1", "戒指2"}:
                continue
            if slot_key == "戒指":
                ring_index += 1
                slot = f"戒指{min(ring_index, 2)}"
            else:
                slot = slot_key
                if slot.startswith("戒指"):
                    ring_index = max(ring_index, int(slot[-1]))
            detail = norm(raw_detail)
            name = self._profile_item_name(detail)
            variant, variant_attrs = self._server_jewelry_option(detail)
            attributes: list[dict[str, Any]] = []
            for label in (
                "基础属性", "强化属性", "当前附加", "首饰固有属性"
            ):
                attributes.extend(self._exact_profile_attributes(
                    self._profile_section(detail, label), label
                ))
            attributes.extend(variant_attrs)
            enchant_attrs = self._exact_profile_attributes(
                self._profile_section(detail, "附魔"), "附魔"
            )
            attributes.extend(enchant_attrs)

            mapped_item = build_by_slot.get(slot, {})
            mapping = mapping_by_slot.get(slot, {})
            mapped_name = (
                mapping.get("status") == "matched"
                and norm(mapped_item.get("name")) not in {"", NONE_TEXT}
            )
            mapped_variant = norm(mapped_item.get("variant")) not in {"", NONE_TEXT}
            simulator_item = copy.deepcopy(mapped_item)
            if simulator_item:
                simulator_item["enchants"] = [NONE_TEXT] * 5
            simulator_attrs = self.calculator.jewelry_attrs(simulator_item) if simulator_item else {}

            server_base: dict[str, float] = {}
            for row in attributes:
                if row["source"] == "附魔":
                    continue
                attr = norm(row["attr"])
                server_base[attr] = as_number(server_base.get(attr)) + as_number(row["value"])
            fallback_attributes: list[dict[str, Any]] = []
            for attr, server_value in server_base.items():
                delta = server_value - self._simulator_attr_value(simulator_attrs, attr)
                if abs(delta) > 1e-6:
                    fallback_attributes.append({
                        "attr": attr,
                        "value": delta,
                        "source": "首饰属性差额",
                    })

            represented_enchants = self._mapped_enchant_families(mapped_item)
            unmatched_enchants = [
                row for row in enchant_attrs
                if self._attribute_family(row["attr"]) not in represented_enchants
            ]
            fallback_attributes.extend(
                {
                    "attr": norm(row["attr"]),
                    "value": as_number(row["value"]),
                    "source": "未映射附魔",
                }
                for row in unmatched_enchants
            )
            if not mapped_name:
                status = "首饰库缺项，按服务器数值回退"
            elif variant != NONE_TEXT and not mapped_variant:
                status = "名称已映射，变体按服务器数值回退"
            else:
                status = "名称与变体已映射"
            if fallback_attributes:
                status = f"{status}，补正 {len(fallback_attributes)} 项"
            details.append({
                "slot": slot,
                "name": name,
                "variant": variant,
                "mappedName": norm(mapped_item.get("name")),
                "mappedVariant": norm(mapped_item.get("variant")),
                "serverAttributes": attributes,
                "serverText": self._jewelry_attr_text(attributes),
                "simulatorText": "，".join(
                    f"{attr} +{as_number(value):g}" for attr, value in simulator_attrs.items()
                ) or "配装库没有可用的同名变体属性",
                "status": status,
                "fallbackAttributes": fallback_attributes,
            })
        return details

    def _fashion_details(
        self,
        profile: dict[str, Any],
        build: dict[str, Any],
        preview: dict[str, Any],
    ) -> list[dict[str, Any]]:
        aliases = {
            "主武器时装": "主手", "副武器时装": "副手",
            "翅膀": "翅膀", "尾巴": "尾巴", "花纹": "印花", "印花": "印花",
            "精灵": "精灵", "精灵时装": "精灵",
            "时装槽13": "精灵",
            "项链时装": "项链", "耳环时装": "耳环",
            "戒指时装1": "戒指1", "戒指时装2": "戒指2",
        }
        build_by_slot = {
            norm(row.get("slot")): row for row in build.get("fashion", [])
        }
        mapping_by_slot = {
            re.sub(r"\s+", "", norm(row.get("item"))): row
            for row in preview.get("items", [])
            if row.get("category") == "时装"
        }
        details: list[dict[str, Any]] = []
        for raw_key, raw_detail in profile.get("equipment", []):
            category, separator, raw_slot = str(raw_key or "").partition("|")
            if not separator or norm(category) != "装备":
                continue
            slot_key = re.sub(r"\s+", "", norm(raw_slot))
            slot = aliases.get(slot_key)
            if not slot:
                continue
            detail = norm(raw_detail)
            attributes = self._exact_profile_attributes(
                self._profile_section(detail, "基础属性"), "时装基础属性"
            )
            mapped_item = build_by_slot.get(slot, {})
            mapping = mapping_by_slot.get(slot, mapping_by_slot.get(slot_key, {}))
            mapped_name = (
                mapping.get("status") == "matched"
                and norm(mapped_item.get("name")) not in {"", NONE_TEXT}
            )
            simulator_attrs = (
                self.calculator.fashion_attrs(norm(mapped_item.get("name")))
                if mapped_item else {}
            )
            server_values = self._server_attribute_values(attributes)
            simulator_values = self._server_attribute_values([
                {"attr": attr, "value": value}
                for attr, value in simulator_attrs.items()
            ])
            fallback_attributes: list[dict[str, Any]] = []
            if server_values:
                for attr in sorted(set(server_values) | set(simulator_values)):
                    if re.fullmatch(r"ID\s*\d+", norm(attr), flags=re.IGNORECASE):
                        continue
                    server_value = as_number(server_values.get(attr))
                    simulator_value = as_number(simulator_values.get(attr))
                    delta = server_value - simulator_value
                    if abs(delta) > 1e-6:
                        fallback_attributes.append({
                            "attr": attr,
                            "value": delta,
                            "serverValue": server_value,
                            "simulatorValue": simulator_value,
                            "source": "时装实际属性差额",
                        })
            status = (
                "时装库已映射，服务器实际数值优先"
                if mapped_name else "时装库缺项，直接采用服务器实际数值"
            )
            if not attributes:
                status = (
                    "客户端精灵品质与强化等级已映射，属性已计入面板"
                    if slot == "精灵" and mapped_name
                    else "服务器正文未返回可计算属性"
                )
            elif fallback_attributes:
                status = f"{status}，补正 {len(fallback_attributes)} 项"
            details.append({
                "slot": f"时装·{slot}",
                "name": self._profile_item_name(detail),
                "mappedName": norm(mapped_item.get("name")),
                "serverAttributes": attributes,
                "serverText": self._jewelry_attr_text(attributes),
                "simulatorText": "，".join(
                    f"{attr} +{as_number(value):g}"
                    for attr, value in simulator_attrs.items()
                ) or "配装库没有可用的同名时装属性",
                "status": status,
                "fallbackAttributes": fallback_attributes,
            })
        return details

    def _glyph_details(
        self,
        profile: dict[str, Any],
        build: dict[str, Any],
    ) -> list[dict[str, Any]]:
        glyph_a = list(build.get("glyph_a", []))
        glyph_s = list(build.get("glyph_s", []))
        a_index = 0
        s_index = 0
        details: list[dict[str, Any]] = []
        for raw_key, raw_detail in profile.get("equipment", []):
            category, separator, raw_slot = str(raw_key or "").partition("|")
            if not separator or norm(category) != "纹章":
                continue
            detail = norm(raw_detail)
            name = self._profile_item_name(detail)
            is_a = "强化纹章" in name
            is_s = "远征队纹章" in name
            if not (is_a or is_s):
                continue
            mapped: dict[str, Any]
            if is_a:
                mapped = glyph_a[a_index] if a_index < len(glyph_a) else {}
                a_index += 1
            else:
                mapped = glyph_s[s_index] if s_index < len(glyph_s) else {}
                s_index += 1
            attribute_text = self._profile_section(detail, "纹章实际属性")
            attributes = self._exact_profile_attributes(
                attribute_text, "纹章实际属性"
            )
            server_values = self._server_attribute_values(attributes)
            if is_a:
                simulator_attrs = self.calculator.glyph_a_attrs(mapped)
                base_attrs = self.calculator.glyph_a_attrs({**mapped, "extra": NONE_TEXT})
                simulator_attrs = {
                    attr: as_number(value) - as_number(base_attrs.get(attr))
                    for attr, value in simulator_attrs.items()
                    if abs(as_number(value) - as_number(base_attrs.get(attr))) > 1e-9
                }
            else:
                simulator_attrs = self.calculator.glyph_s_attrs(mapped)
            simulator_values = self._server_attribute_values([
                {"attr": attr, "value": value}
                for attr, value in simulator_attrs.items()
            ])
            fallback_attributes: list[dict[str, Any]] = []
            for attr in sorted(set(server_values) | set(simulator_values)):
                server_value = as_number(server_values.get(attr))
                simulator_value = as_number(simulator_values.get(attr))
                delta = server_value - simulator_value
                if abs(delta) > 1e-6:
                    fallback_attributes.append({
                        "attr": attr, "value": delta,
                        "serverValue": server_value, "simulatorValue": simulator_value,
                        "source": "纹章实际属性差额",
                    })
            if attribute_text.startswith("未解码"):
                status = "服务器选择码尚未进入当前DNT；未猜测额外属性"
            elif attribute_text == NONE_TEXT:
                status = "服务器选择码已确认：无额外属性"
            else:
                status = "当前使用页已识别；服务器实际属性已计入"
            if fallback_attributes and not attribute_text.startswith("未解码"):
                status += f"，补正 {len(fallback_attributes)} 项"
            details.append({
                "slot": f"纹章·{norm(raw_slot)}", "name": name,
                "mappedName": norm(mapped.get("name")),
                "serverAttributes": attributes,
                "serverText": (
                    attribute_text
                    if attribute_text.startswith("未解码") or attribute_text == NONE_TEXT
                    else self._jewelry_attr_text(attributes)
                ),
                "simulatorText": "，".join(
                    f"{attr} +{as_number(value):g}" for attr, value in simulator_values.items()
                ) or "模拟器旧纹章结构无法表达该选择",
                "status": status, "fallbackAttributes": fallback_attributes,
            })
        return details

    def _engraving_details(
        self,
        profile: dict[str, Any],
        build: dict[str, Any],
    ) -> list[dict[str, Any]]:
        build_rows = list(build.get("engravings", []))
        positions: dict[str, int] = {}
        aliases = {"辅助武器": "副手", "主武器": "主手", "花纹": "印花"}
        details: list[dict[str, Any]] = []
        for raw_key, raw_detail in profile.get("equipment", []):
            category, separator, raw_slot = str(raw_key or "").partition("|")
            if not separator or norm(category) != "刻印":
                continue
            base_slot = aliases.get(
                re.sub(r"\s+", "", norm(raw_slot)),
                re.sub(r"\s+", "", norm(raw_slot)),
            )
            candidates = [
                row for row in build_rows
                if norm(row.get("slot")).startswith(base_slot)
            ]
            index = positions.get(base_slot, 0)
            positions[base_slot] = index + 1
            mapped = candidates[min(index, len(candidates) - 1)] if candidates else {}
            attribute_text = self._profile_section(raw_detail, "额外属性")
            attributes = self._exact_profile_attributes(
                attribute_text, "刻印额外属性"
            )
            details.append({
                "slot": norm(mapped.get("slot")) or norm(raw_slot),
                "name": self._profile_item_name(raw_detail),
                "serverAttributes": attributes,
                "serverText": (
                    attribute_text
                    if attribute_text.startswith("未解码") or attribute_text == NONE_TEXT
                    else self._jewelry_attr_text(attributes)
                ),
                "mappedExtra": norm(mapped.get("extra")) or NONE_TEXT,
                "mappedRank": norm(mapped.get("extra_rank")) or NONE_TEXT,
                "mappedValue": as_number(mapped.get("extra_value")),
                "status": (
                    "服务器选择码尚未完成模板级校准；未按部位或邻档猜测"
                    if attribute_text.startswith("未解码")
                    else "服务器选择码已确认：无额外属性"
                    if attribute_text == NONE_TEXT
                    else
                    "额外属性已精确映射并计入总面板"
                    if attributes and norm(mapped.get("extra")) != NONE_TEXT
                    else "额外属性已显示；该属性暂不属于配装面板计算项"
                    if attributes else "服务器正文未返回额外属性"
                ),
            })
        return details

    @staticmethod
    def _add_free_profile_attr(build: dict[str, Any], attr: str, value: float) -> None:
        free = build.setdefault(
            "free_inputs", {"percent": {}, "stats": {}, "stat_percent": {}}
        )
        group = "stat_percent" if norm(attr).endswith("%") else "stats"
        target = free.setdefault(group, {})
        target[attr] = as_number(target.get(attr)) + value

    def _apply_jewelry_fallbacks(
        self, build: dict[str, Any], details: list[dict[str, Any]]
    ) -> list[str]:
        notes: list[str] = []
        for detail in details:
            fallback_attributes = detail.get("fallbackAttributes", [])
            if not fallback_attributes:
                continue
            for row in fallback_attributes:
                self._add_free_profile_attr(
                    build, norm(row.get("attr")), as_number(row.get("value"))
                )
            notes.append(
                f"{detail.get('slot')}有 {len(fallback_attributes)} 项服务器确认属性差额已补入；"
                "库外首饰套装效果仍不会臆测。"
            )
        return notes

    def _apply_equipment_fallbacks(
        self, build: dict[str, Any], details: list[dict[str, Any]]
    ) -> list[str]:
        notes: list[str] = []
        for detail in details:
            fallback_attributes = detail.get("fallbackAttributes", [])
            if not fallback_attributes:
                continue
            for row in fallback_attributes:
                self._add_free_profile_attr(
                    build, norm(row.get("attr")), as_number(row.get("value"))
                )
            notes.append(
                f"{detail.get('slot')}有 {len(fallback_attributes)} 项实际装备属性差额已补入；"
                "库外装备不臆测套装效果。"
            )
        return notes

    def _apply_fashion_fallbacks(
        self, build: dict[str, Any], details: list[dict[str, Any]]
    ) -> list[str]:
        notes: list[str] = []
        for detail in details:
            fallback_attributes = detail.get("fallbackAttributes", [])
            if not fallback_attributes:
                continue
            for row in fallback_attributes:
                self._add_free_profile_attr(
                    build, norm(row.get("attr")), as_number(row.get("value"))
                )
            notes.append(
                f"{detail.get('slot')}有 {len(fallback_attributes)} 项服务器确认时装属性差额已补入。"
            )
        return notes

    def _apply_glyph_fallbacks(
        self, build: dict[str, Any], details: list[dict[str, Any]]
    ) -> list[str]:
        notes: list[str] = []
        for detail in details:
            fallback_attributes = detail.get("fallbackAttributes", [])
            if not fallback_attributes:
                continue
            for row in fallback_attributes:
                self._add_free_profile_attr(
                    build, norm(row.get("attr")), as_number(row.get("value"))
                )
            notes.append(
                f"{detail.get('slot')}有 {len(fallback_attributes)} 项服务器确认纹章差额已补入。"
            )
        return notes

    @staticmethod
    def _set_bonus_attr_text(attributes: dict[str, Any]) -> str:
        return "，".join(
            f"{norm(attr)} +{as_number(value):g}"
            for attr, value in attributes.items()
            if abs(as_number(value)) > 1e-9
        ) or "当前件数尚未达到套装属性档位"

    def _set_bonus_details(self, build: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.calculator.set_bonus_breakdown(
            build.get("equipment", []),
            build.get("jewelry", []),
            norm(build.get("first_job")),
            norm(build.get("second_job")),
        )
        details: list[dict[str, Any]] = []
        for row in rows:
            members = list(dict.fromkeys(norm(value) for value in row.get("members", []) if norm(value)))
            tiers = [int(as_number(value)) for value in row.get("appliedTiers", [])]
            details.append({
                **row,
                "slot": f"{norm(row.get('scope'))}套装",
                "name": "、".join(members) or f"套装 ID {int(as_number(row.get('setId')))}",
                "mappedName": "、".join(members),
                "serverText": self._set_bonus_attr_text(row.get("attributes", {})),
                "simulatorText": self._set_bonus_attr_text(row.get("attributes", {})),
                "status": (
                    f"按 {int(as_number(row.get('count')))} 件计算；"
                    f"已触发 {'/'.join(str(value) for value in tiers)} 件档"
                    if tiers else f"按 {int(as_number(row.get('count')))} 件计算；尚未触发套装档位"
                ),
            })
        return details

    @staticmethod
    def _cache_key(profile: dict[str, Any], context_build: dict[str, Any]) -> str:
        key = {
            "profile": {
                "timestamp_ms": profile.get("timestamp_ms"),
                "target_id": profile.get("target_id"),
                "job": profile.get("job"),
                "equipment": profile.get("equipment"),
                "cards": profile.get("cards"),
                "fields": profile.get("fields"),
                "stats": profile.get("stats"),
            },
            "boss": {
                key: context_build.get(key)
                for key in (
                    "crit_resist_value", "monster_defense", "element_resist_percent",
                    "fire_resist_percent", "water_resist_percent",
                    "light_resist_percent", "dark_resist_percent",
                )
            },
            "selfContext": {
                key: context_build.get(key)
                for key in (
                    "title", "collection_level", "mastery", "buffs",
                    "extra_buff_attrs", "free_inputs",
                )
            },
        }
        return json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)

    def calculate(
        self,
        profile_value: Any,
        context_build: dict[str, Any],
        previewer: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        profile = self.profile_dict(profile_value)
        job = norm(profile.get("job"))
        if not job:
            equipment_count = len(profile.get("equipment", []))
            if equipment_count:
                message = (
                    f"已读取 {equipment_count} 条查看信息，但尚未识别职业，"
                    "无法套用职业配装公式。"
                )
            else:
                message = (
                    "只捕获到状态栏/会话标记，未捕获服务器返回的装备档案正文和职业，"
                    "因此无法计算总面板。"
                )
            return {"state": "unavailable", "message": message}
        if not profile.get("equipment"):
            return {
                "state": "unavailable",
                "message": (
                    f"已识别职业为 {job}，但本次记录没有装备档案正文；"
                    "仅凭职业不能反算总面板。"
                ),
            }
        cache_key = self._cache_key(profile, context_build)
        if cache_key in self._cache:
            return copy.deepcopy(self._cache[cache_key])

        profile_fields = {
            norm(row[0]): norm(row[1])
            for row in profile.get("fields", [])
            if isinstance(row, (list, tuple)) and len(row) >= 2
        }
        is_self_profile = profile_fields.get("响应类型") == "自身状态栏"
        base_build = (
            copy.deepcopy(context_build)
            if is_self_profile else self.blank_build(context_build, job)
        )
        preview = previewer(base_build, profile)
        confirmed_build = copy.deepcopy(preview.get("build", base_build))
        confirmed_build.pop("card_initial_totals", None)
        # Teammate profiles must not inherit the local character's title or
        # account systems. The v576 title carrier is still being decoded, so
        # an absent parsed title means "unknown", not "the server omitted it".
        # A self profile is also the build-sync source, so preserving its
        # unresolved account fields keeps this panel and Build 1 consistent.
        if not is_self_profile:
            confirmed_build["title"] = norm(profile.get("title")) or NONE_TEXT
            confirmed_build["collection_level"] = 33
            confirmed_build["mastery"] = self._default_account_mastery()
        confirmed_build["card_grades"] = [
            {"name": name, "grade": NONE_TEXT} for name in self.store.card_names()
        ]
        observed_cards = self._observed_card_grades(profile)
        observed_card_bonus_levels = self._observed_card_bonus_levels(profile)
        existing_card_bonus_levels = (
            copy.deepcopy(confirmed_build.get("card_bonus_levels"))
            if isinstance(confirmed_build.get("card_bonus_levels"), dict)
            else {}
        )
        card_mastery_complete = all(
            attr in observed_card_bonus_levels
            for attr in CARD_MASTERY_ATTR_MAP.values()
        )
        card_mastery_details = [
            {
                "name": CARD_MASTERY_NAME_BY_ATTR.get(attr, attr),
                "attr": attr,
                "level": int(observed_card_bonus_levels[attr]),
            }
            for attr in CARD_PREVIEW_ATTRS
            if attr != "致命伤害" and attr in observed_card_bonus_levels
        ]
        if observed_cards:
            confirmed_build["card_grades"] = [
                {"name": name, "grade": observed_cards.get(name, NONE_TEXT)}
                for name in self.store.card_names()
            ]
        confirmed_build["card_bonus_levels"] = {
            attr: (
                0
                if attr == "致命伤害"
                else observed_card_bonus_levels[attr]
                if attr in observed_card_bonus_levels
                else int(as_number(existing_card_bonus_levels.get(attr)))
                if is_self_profile
                else 0
            )
            for attr in CARD_PREVIEW_ATTRS
        }

        equipment_details = self._equipment_details(profile, confirmed_build, preview)
        equipment_fallback_notes = self._apply_equipment_fallbacks(
            confirmed_build, equipment_details
        )
        jewelry_details = self._jewelry_details(
            profile, confirmed_build, preview
        )
        jewelry_fallback_notes = self._apply_jewelry_fallbacks(
            confirmed_build, jewelry_details
        )
        fashion_details = self._fashion_details(
            profile, confirmed_build, preview
        )
        fashion_fallback_notes = self._apply_fashion_fallbacks(
            confirmed_build, fashion_details
        )
        glyph_details = self._glyph_details(profile, confirmed_build)
        glyph_fallback_notes = self._apply_glyph_fallbacks(
            confirmed_build, glyph_details
        )
        engraving_details = self._engraving_details(
            profile, confirmed_build
        )

        estimated_build = copy.deepcopy(confirmed_build)
        jewelry_notes = self._fill_estimated_jewelry_variants(
            confirmed_build, estimated_build
        )
        card_estimate = self.card_estimate_summary(profile, preview)
        card_level = int(as_number(card_estimate.get("estimatedMasteryLevel")))
        card_quality = as_number(card_estimate.get("quality"))
        estimated_build["card_grades"] = copy.deepcopy(
            card_estimate["cardGrades"]
        )
        estimated_build["card_bonus_levels"] = copy.deepcopy(
            card_estimate["masteryLevels"]
        )
        if card_estimate.get("cardsSource") == "estimated":
            estimated_build["card_initial_totals"] = copy.deepcopy(
                card_estimate["initialTotals"]
            )
        else:
            estimated_build.pop("card_initial_totals", None)

        set_bonus_details = self._set_bonus_details(confirmed_build)

        confirmed_rows, confirmed_summary = self.calculator.calculate(confirmed_build)
        estimated_rows, estimated_summary = self.calculator.calculate(estimated_build)
        confirmed_metrics = self._panel_metrics(confirmed_rows, confirmed_summary)
        estimated_metrics = self._panel_metrics(estimated_rows, estimated_summary)
        labels = (
            ("effective_panel", "等效面板", "number"),
            ("physical_attack_min", "最小物理攻击", "number"),
            ("physical_attack_max", "最大物理攻击", "number"),
            ("physical_attack", "平均物理攻击", "number"),
            ("magic_attack_min", "最小魔法攻击", "number"),
            ("magic_attack_max", "最大魔法攻击", "number"),
            ("magic_attack", "平均魔法攻击", "number"),
            ("final_percent", "最终", "percent"),
            ("crit_percent", "致命", "percent"),
            ("crit_damage_percent", "爆伤加成", "percent"),
            ("HP", "HP", "number"),
            ("strength", "力量", "number"),
            ("agility", "敏捷", "number"),
            ("intelligence", "智力", "number"),
            ("vitality", "体质", "number"),
            ("physical_defense", "物理防御", "number"),
            ("magic_defense", "魔法防御", "number"),
            ("fire_attack", "火攻", "percent"),
            ("water_attack", "水攻", "percent"),
            ("light_attack", "光攻", "percent"),
            ("dark_attack", "暗攻", "percent"),
        )
        mapped = int(preview.get("profile", {}).get("mappedItems", 0))
        total = int(preview.get("profile", {}).get("totalItems", 0))
        result = {
            "state": "ready",
            "targetId": norm(profile.get("target_id")),
            "job": job,
            "rows": [
                {
                    "key": key, "label": label, "format": value_format,
                    "confirmed": confirmed_metrics[key],
                    "estimated": estimated_metrics[key],
                }
                for key, label, value_format in labels
            ],
            "coverage": {
                "mapped": mapped, "total": total,
                "percent": (mapped / total * 100) if total else 0.0,
            },
            "actualItems": {
                "records": (
                    len(equipment_details) + len(jewelry_details)
                    + len(fashion_details) + len(glyph_details)
                ),
                "mapped": sum(
                    1 for row in equipment_details
                    if norm(row.get("mappedName")) not in {"", NONE_TEXT}
                ) + sum(
                    1 for row in jewelry_details
                    if norm(row.get("mappedName")) not in {"", NONE_TEXT}
                ) + sum(
                    1 for row in fashion_details
                    if norm(row.get("mappedName")) not in {"", NONE_TEXT}
                ) + sum(
                    1 for row in glyph_details
                    if norm(row.get("mappedName")) not in {"", NONE_TEXT}
                ),
                "details": [
                    *equipment_details, *jewelry_details, *fashion_details,
                    *glyph_details, *set_bonus_details,
                ],
            },
            "glyphs": {
                "records": len(glyph_details),
                "mapped": sum(
                    1 for row in glyph_details
                    if norm(row.get("mappedName")) not in {"", NONE_TEXT}
                ),
                "details": glyph_details,
            },
            "setBonuses": {
                "sets": len(set_bonus_details),
                "details": set_bonus_details,
            },
            "accountDefaults": {
                "collectionLevel": int(
                    as_number(confirmed_build.get("collection_level"))
                ),
                "mastery": copy.deepcopy(confirmed_build.get("mastery", [])),
                "source": "build" if is_self_profile else "team-default",
            },
            "calculation": {
                "engine": "BuildCalculator",
                "sameAsBuildPage": True,
                "order": [
                    "职业基础", "逐件装备与首饰", "按件数触发套装属性",
                    "纹章与属性石板", "固定值百分比", "四维转攻击",
                    "源铸与专精", "被动Buff",
                ],
                "excluded": ["龙渊石板", "技能石板"],
            },
            "jewelry": {
                "records": len({
                    norm(key).split("|", 1)[-1].replace(" ", "")
                    for key, _detail in profile.get("equipment", [])
                    if norm(key).startswith("装备|")
                    and norm(key).split("|", 1)[-1].replace(" ", "")
                    in {"项链", "耳环", "戒指1", "戒指2"}
                }),
                "mapped": sum(
                    1 for row in preview.get("items", [])
                    if row.get("category") == "首饰" and row.get("status") == "matched"
                ),
                "variantNotes": jewelry_notes,
                "details": jewelry_details,
            },
            "engravings": {
                "records": len(engraving_details),
                "mapped": sum(
                    1 for row in engraving_details
                    if row.get("mappedExtra") != NONE_TEXT
                ),
                "details": engraving_details,
            },
            "cardMastery": card_mastery_details,
            "cardEstimate": {
                key: copy.deepcopy(value)
                for key, value in card_estimate.items()
                if key not in {"cardGrades", "masteryLevels"}
            },
            "notes": [
                "已确认列优先使用服务器查看信息逐部位返回的基础、强化、后缀、附魔和首饰变体数值；模拟器库仅补充正文未返回的可靠项目。",
                "已映射装备和首饰按实际件数触发套装属性；库外项目仅计算服务器明确返回的数值，不臆测未知套装。",
                "龙渊石板和其他技能石板只改变对应技能，不进入最终面板；普通属性石板与源铸石板仍按配装公式计算。",
                (
                    "卡片品质与掌握之力均使用服务器档案实测值；爆伤不受掌握等级加成。"
                    if observed_cards and card_mastery_complete
                    else f"卡片品质使用服务器档案实测值；掌握之力已读取部分项目，估算列对未读取项目按 {card_level} 级补齐并逐项标记。"
                    if observed_cards and observed_card_bonus_levels
                    else f"估算列根据该角色装备质量在全L卡片最大初始合计的40%～90%区间内取值；本档案取 {as_number(card_estimate.get('initialEstimateExpectedRatio')) * 100:.1f}%，卡片掌握等级按 {card_level} 级估算（装备质量系数 {card_quality * 100:.0f}%），爆伤不受掌握等级加成。"
                ),
                (
                    "卡片记录中的重新封印次数不参与面板计算；本次已独立识别掌握之力等级。"
                    if card_mastery_complete
                    else "卡片记录中的重新封印次数不参与面板计算；本次掌握档案已发送，但仍有项目缺少页面校准。"
                    if observed_card_bonus_levels
                    else "卡片记录中的重新封印次数不参与面板计算；真正的卡片总属性掌握等级未读取时，确认值按0级处理。"
                ),
                (
                    "本人档案中的称号协议字段尚未完成解码；收藏等级和精通沿用配装1对应值，导入后与本页使用同一构筑计算。"
                    if is_self_profile else
                    "称号协议字段尚未完成解码；队友收藏默认按33套、各项专精按当前数据表最高等级计算。"
                ),
                *equipment_fallback_notes,
                *jewelry_fallback_notes,
                *fashion_fallback_notes,
                *glyph_fallback_notes,
                *jewelry_notes,
            ],
            "confirmedBuild": confirmed_build,
            "estimatedBuild": estimated_build,
            "mapping": preview,
        }
        self._cache[cache_key] = copy.deepcopy(result)
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return result
