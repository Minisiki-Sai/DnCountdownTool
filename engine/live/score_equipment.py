"""Per-recipient scoring equipment, separate from skill/environment inputs."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

EQUIPMENT_FIELDS = (
    "equipment", "jewelry", "glyph_a", "glyph_s", "normal_runes",
    "core_rune", "mastery", "fashion", "title", "collection_level",
    "card_grades", "card_bonus_levels", "talismans", "engravings",
    "free_inputs", "free_percent", "free_stats", "free_stat_percent",
)


def equipment_overrides(value: object) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("评分装备必须按队员 ID 提交")
    result = {}
    for source, build in value.items():
        if not isinstance(build, dict):
            raise ValueError("评分装备格式不正确")
        fields = {key: copy.deepcopy(build[key]) for key in EQUIPMENT_FIELDS if key in build}
        list_fields = {"equipment", "jewelry", "glyph_a", "glyph_s", "normal_runes", "mastery", "fashion", "card_grades", "talismans", "engravings"}
        dict_fields = {"core_rune", "card_bonus_levels", "free_inputs", "free_percent", "free_stats", "free_stat_percent"}
        for key, entries in fields.items():
            if key in list_fields and (not isinstance(entries, list) or len(entries) > 256 or any(not isinstance(row, dict) for row in entries)):
                raise ValueError(f"评分装备 {key} 格式不正确")
            if key in dict_fields and not isinstance(entries, dict):
                raise ValueError(f"评分装备 {key} 格式不正确")
        try:
            encoded = json.dumps(fields, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("评分装备包含无效数值") from exc
        if len(encoded.encode("utf-8")) > 262144:
            raise ValueError("单个队员的评分装备数据过大")
        if fields:
            result[str(source).strip()] = fields
    return result


def equipment_signature(value: object) -> str:
    return hashlib.sha256(json.dumps(
        equipment_overrides(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def apply_score_equipment(build: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(build)
    if override:
        result.update(equipment_overrides({"player": override}).get("player", {}))
        # Never apply the standard card conversion subsidy to a different
        # hand-picked card set. Unchanged cards retain the exact old baseline.
        if any(result.get(key) != build.get(key) for key in ("card_grades", "card_bonus_levels")):
            result.pop("extra_buff_attrs", None)
        result["_score_standard_profile"] = {"equipment": "当前记录手动评分装备"}
    return result


def scoring_equipment_record(build: dict[str, Any], *, custom: bool) -> dict[str, Any]:
    return {
        "schema": "score-equipment-v1",
        "source": "manual_override" if custom else "standard_default",
        "build": {key: copy.deepcopy(build[key]) for key in (
            *EQUIPMENT_FIELDS, "first_job", "second_job", "boss", "crit_resist_value",
            "element_resists", "monster_defense", "freeze_coefficient",
            "skill_build", "skill_mastery_build", "skill_glyphs", "buffs", "extra_buff_attrs",
        ) if key in build},
    }
