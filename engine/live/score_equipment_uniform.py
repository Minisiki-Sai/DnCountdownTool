"""Explicit whole-party grade settings, applied only when requested by the UI."""
from __future__ import annotations

import copy
from typing import Any

from ..data_loader import norm, as_number
from ..equipment_constants import AUTO_EQUIP_TARGETS, CARD_PREVIEW_ATTRS, ENGRAVING_SLOTS


def uniform_equipment_options(store) -> dict[str, Any]:
    def targets(kind):
        return [{"value": tier, "label": label, "level": level}
            for level, rows in AUTO_EQUIP_TARGETS.items()
            for label, entry_kind, tier in rows if entry_kind == kind]
    return {
        "equipmentTiers": targets("equipment"), "jewelryTiers": targets("jewelry"),
        "glyphALevels": store.glyph_a_levels(), "glyphSLevels": store.glyph_s_levels(),
        "talismanGrades": store.talisman_grades(), "engravingGrades": store.engraving_grades(),
        "cardGrades": ["无", *dict.fromkeys(norm(row.get("等级")) for row in store.tables.get("card_data", []) if norm(row.get("等级")))],
    }


def _integer(value, key, maximum=25):
    try:
        number = float(value)
        if not number.is_integer() or not 0 <= number <= maximum:
            raise ValueError
        return int(number)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{key}必须是 0–{maximum} 的整数") from exc


def validate_uniform_settings(store, settings: object) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("全队统一设置格式不正确")
    catalog = uniform_equipment_options(store)
    result = {}
    choices = {
        "equipmentTier": [row["value"] for row in catalog["equipmentTiers"]],
        "jewelryTier": [row["value"] for row in catalog["jewelryTiers"]],
        "glyphALevel": catalog["glyphALevels"], "glyphSLevel": catalog["glyphSLevels"],
        "talismanGrade": catalog["talismanGrades"], "engravingGrade": catalog["engravingGrades"],
        "cardGrade": catalog["cardGrades"],
    }
    for key, allowed in choices.items():
        value = norm(settings.get(key))
        if not value:
            continue
        if value not in allowed:
            raise ValueError(f"{key}不是当前数据支持的档位")
        result[key] = value
    for key in ("weaponEnhance", "armorEnhance", "cardBonusLevel"):
        value = settings.get(key)
        if value not in (None, ""):
            result[key] = _integer(value, key)
    if not result:
        raise ValueError("请至少选择一项全队统一设置")
    return result


def _talisman_rows(host, build, grade):
    """Preserve attribute families; fill empty slots with role-appropriate ones."""
    profile = host._job_profile(build)
    main = norm(profile.get("main_stat"))
    attacks = profile.get("attack_attrs") or [profile.get("attack_attr")]
    wanted = list(dict.fromkeys([main, *attacks, "最终", "致命伤害", "致命", "HP", "体质", "敏捷", "力量", "智力"]))
    families = {}
    old_families = {}
    for row in host.store.tables.get("talisman", []):
        if norm(row.get("类型")) != "主属性":
            continue
        name, attr = norm(row.get("护符")), norm(row.get("属性"))
        old_families[name] = attr
        if norm(row.get("品质")) == grade:
            families.setdefault(attr, []).append((name, as_number(row.get("数值"))))
    used = set()
    rows = []
    for item in build.get("talismans", []):
        row = copy.deepcopy(item)
        attr = old_families.get(norm(row.get("name")))
        if attr not in families or attr in used:
            attr = next((candidate for candidate in wanted if candidate in families and candidate not in used), None)
        if attr is None or grade == "无":
            row.update(grade="无", name="无", extra="无")
        else:
            used.add(attr)
            names = families[attr]
            name = norm(row.get("name"))
            row["grade"] = grade
            row["name"] = name if any(entry[0] == name for entry in names) else max(names, key=lambda entry: entry[1])[0]
            allowed = host.store.talisman_extra_options(grade, row["name"])
            extra = norm(row.get("extra"))
            row["extra"] = extra if extra in allowed else "无"
        rows.append(row)
    return rows


def apply_uniform_equipment(host, build: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(build)
    for key, kind in (("equipmentTier", "equipment"), ("jewelryTier", "jewelry")):
        tier = settings.get(key)
        if tier:
            level, label = next((level, label) for level, entries in AUTO_EQUIP_TARGETS.items()
                for label, entry_kind, value in entries if entry_kind == kind and value == tier)
            result = host.apply_build_preset(result, "equipment", {"level": level, "target": label})
            # A tier change alone must not silently reset selected enhancement.
            if kind == "equipment":
                old = {norm(item.get("slot")): item for item in build.get("equipment", [])}
                for row in result["equipment"]:
                    if row["slot"] in old and "enhance" in old[row["slot"]]:
                        row["enhance"] = old[row["slot"]]["enhance"]
    for row in result.get("equipment", []):
        key = "weaponEnhance" if norm(row.get("slot")) in {"主手", "副手"} else "armorEnhance"
        if key in settings:
            row["enhance"] = settings[key]
    for key, rows in (("glyphALevel", "glyph_a"), ("glyphSLevel", "glyph_s")):
        if settings.get(key):
            for row in result.get(rows, []):
                row["level"] = settings[key]
    if "talismanGrade" in settings:
        result["talismans"] = _talisman_rows(host, result, settings["talismanGrade"])
    if "cardGrade" in settings:
        # Existing card names and mastery levels are independent choices.
        for row in result.get("card_grades", []):
            row["grade"] = settings["cardGrade"]
    if "cardBonusLevel" in settings:
        keys = set(result.get("card_bonus_levels", {})) | set(CARD_PREVIEW_ATTRS)
        result["card_bonus_levels"] = {attr: 0 if norm(attr) in {"致命伤害", "爆伤"} else settings["cardBonusLevel"] for attr in keys}
    if "engravingGrade" in settings:
        if not result.get("engravings"):
            result["engravings"] = [{"slot": slot} for slot in ENGRAVING_SLOTS]
        previous = {row["slot"]: row for row in result["engravings"]}
        grade = settings["engravingGrade"]
        result = host.apply_build_preset(result, "engraving", {"grade": grade})
        for row in result["engravings"]:
            old = previous.get(row["slot"], {})
            extra = norm(old.get("extra"))
            tier = "高档" if "高档" in f"{old.get('extra_tier', '')} {old.get('extra_rank', '')}" else "低档"
            value = host.store.engraving_extra_value(extra, grade, tier)
            if grade != "无" and extra not in {"", "无"} and value > 0:
                row.update(extra=extra, extra_tier=tier, extra_value=value,
                    extra_rank=next((option for option in host.store.engraving_extra_rank_options(extra)
                        if option.startswith(f"{grade}{tier}")), "无"))
    return result
