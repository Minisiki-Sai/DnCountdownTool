from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


_CATALOG_FILENAME = "inspect_item_catalog.json.gz"
_CATEGORY_NAMES = {
    "e": "装备",
    "g": "纹章",
    "r": "石板",
    "i": "刻印",
    "t": "护符",
}
_PERCENT_STATE_IDS = {
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    58,
    59,
    60,
    61,
    62,
    63,
    64,
    65,
    75,
    76,
    77,
    79,
    101,
    102,
    104,
}
_STATE_NAMES = {
    0: "力量",
    1: "敏捷",
    2: "智力",
    3: "体质",
    4: "最小物理攻击力",
    5: "最大物理攻击力",
    6: "最小魔法攻击力",
    7: "最大魔法攻击力",
    8: "物理防御",
    9: "魔法防御",
    10: "眩晕",
    11: "硬直",
    12: "致命一击",
    13: "眩晕抵抗",
    14: "致命一击抵抗",
    15: "硬直抵抗",
    16: "火属性攻击",
    17: "水属性攻击",
    18: "光属性攻击",
    19: "暗属性攻击",
    20: "火属性抵抗",
    21: "水属性抵抗",
    22: "光属性抵抗",
    23: "暗属性抵抗",
    25: "最大生命值",
    26: "最大魔法值",
    27: "魔法恢复",
    29: "最终伤害",
    32: "物理攻击力",
    33: "魔法攻击力",
    50: "力量",
    51: "敏捷",
    52: "智力",
    53: "体质",
    54: "最小物理攻击力",
    55: "最大物理攻击力",
    56: "最小魔法攻击力",
    57: "最大魔法攻击力",
    58: "物理防御",
    59: "魔法防御",
    60: "眩晕",
    61: "硬直",
    62: "致命一击",
    63: "眩晕抵抗",
    64: "致命一击抵抗",
    65: "硬直抵抗",
    75: "最大生命值",
    76: "最大魔法值",
    # These IDs are the percentage variants used by the current client.
    # The labels are verified from the client's state renderer/UI strings;
    # 79 was previously mislabeled as critical damage.
    77: "魔法值恢复",
    79: "最终伤害",
    101: "物理攻击力",
    102: "魔法攻击力",
    103: "致命伤害",
    104: "致命伤害",
}
_EQUIPMENT_SLOTS = {
    0: "角色外观",
    1: "发型",
    2: "头盔",
    3: "上装",
    4: "下装",
    5: "手套",
    6: "鞋子",
    7: "项链",
    8: "耳环",
    9: "戒指 1",
    10: "戒指 2",
    11: "主武器",
    12: "副武器",
}
_COSTUME_SLOTS = {
    0: "头部时装",
    1: "上装时装",
    2: "下装时装",
    3: "手套时装",
    4: "鞋子时装",
    5: "项链时装",
    6: "耳环时装",
    7: "戒指时装 1",
    8: "戒指时装 2",
    9: "翅膀",
    10: "尾巴",
    11: "印花",
    12: "精灵",
    13: "主武器时装",
    14: "副武器时装",
}


def inspect_spirit_grade(item: InspectCatalogItem | None) -> str:
    """Resolve the fairy grade from the client item template.

    ``itemtable_cashfairy`` assigns rank 3 to enchant groups 12031-12035
    (A grade) and rank 4 to 12041-12045 (S grade).  The compact catalog does
    not retain ``_Rank``, so use the equivalent, stable enchant-group field
    that is already part of every catalog item.
    """
    if item is None or "精灵" not in item.name:
        return ""
    if 12031 <= item.enchant_id <= 12035:
        return "A"
    if 12041 <= item.enchant_id <= 12045:
        return "S"
    return ""
_TALISMAN_SLOTS = (
    "200%槽位1",
    "175%槽位1",
    "125%槽位1",
    "100%槽位1",
    "200%槽位2",
    "175%槽位2",
    "125%槽位2",
    "100%槽位2",
)
_TALISMAN_SLOT_MULTIPLIERS = (2.0, 1.75, 1.25, 1.0, 2.0, 1.75, 1.25, 1.0)
_TALISMAN_ATTRIBUTE_STATE_IDS: dict[str, tuple[int, ...]] = {
    "力量": (0,),
    "敏捷": (1,),
    "智力": (2,),
    "体质": (3,),
    "物攻": (4, 5),
    "魔攻": (6, 7),
    "防御": (8,),
    "魔防": (9,),
    "眩晕": (10,),
    "硬直": (11,),
    "致命": (12,),
    "眩晕抵抗": (13,),
    "致命抵抗": (14,),
    "硬直抵抗": (15,),
    "HP": (25,),
    "MP": (26,),
    "最终": (29,),
    "致命伤害": (103,),
    "致命一击": (12,),
    "物理防御力": (8,),
    "魔法防御力": (9,),
    "物理攻击力": (4, 5),
    "魔法攻击力": (6, 7),
    "最大生命值": (25,),
    "最大魔法值": (26,),
}


@dataclass(frozen=True, slots=True)
class InspectCatalogItem:
    template_id: int
    name: str
    category: str
    level: int = 0
    description: str = ""
    job_class_codes: tuple[int, ...] = ()
    attributes: tuple[tuple[int, float, float], ...] = ()
    enchant_id: int = 0
    potential_id: int = 0
    glyph_potential_id: int = 0
    type_param3_id: int = 0
    jewelry_variants: tuple[
        tuple[int, tuple[tuple[str, float], ...]], ...
    ] = ()
    suffixes: tuple[
        tuple[str, tuple[tuple[str, float], ...]], ...
    ] = ()

    @property
    def inherent_potential_id(self) -> int:
        """Generic equipment option table referenced by item TypeParam1.

        Glyph templates use the same item field for their glyph potential, so
        keep ``glyph_potential_id`` for compatibility and expose the equipment
        meaning through this alias.
        """
        return self.glyph_potential_id

    @property
    def engraving_potence_id(self) -> int:
        """Client ``potencetable`` group selected by imprint TypeParam1."""
        return self.glyph_potential_id if self.category == "刻印" else 0


_CORE_RUNE_CONVERSIONS = (
    "物攻→魔攻",
    "物攻→魔防",
    "魔攻→物攻",
    "魔攻→防御",
    "致命→魔防",
    "致命→防御",
    "致命→物攻",
    "致命→魔攻",
    "防御→物攻",
    "魔防→魔攻",
    "防御→致命",
    "魔防→致命",
)
# The coefficient selector is conversion-specific.  The wire value is a
# 1-based rank, not the coefficient itself.  In particular, critical ->
# attack uses 0.10..0.30; treating every conversion as 0.25..0.50 made rank
# 21 appear as 0.5 in profile details even though the game data says 0.3.
_CORE_RUNE_COEFFICIENT_RANGES = {
    "防御→致命": (0.5, 1.0),
    "魔防→致命": (0.5, 1.0),
    "致命→物攻": (0.1, 0.3),
    "致命→魔攻": (0.1, 0.3),
}
_JEWELRY_VARIANT_NAMES = {1: "力敏", 2: "敏智", 3: "智体"}


@lru_cache(maxsize=4)
def _load_inspect_catalog_payload(data_root: str) -> dict:
    path = Path(data_root) / "profiles" / _CATALOG_FILENAME
    if not path.is_file():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_named_attributes(value: object) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, dict):
        return ()
    result: list[tuple[str, float]] = []
    for raw_name, raw_value in value.items():
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            continue
        if abs(number) < 1e-7:
            continue
        name = str(raw_name or "").strip()
        if name:
            result.append((name, number))
    return tuple(result)


@lru_cache(maxsize=4)
def _load_inspect_jewelry_variants(
    data_root: str,
) -> dict[str, tuple[tuple[int, tuple[tuple[str, float], ...]], ...]]:
    table_root = Path(data_root) / "modular" / "tables" / "jewelry"
    result: dict[str, dict[int, tuple[tuple[str, float], ...]]] = {}
    for path in sorted(table_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for levels in payload.values():
            if not isinstance(levels, dict):
                continue
            for items in levels.values():
                if not isinstance(items, dict):
                    continue
                for raw_name, variants in items.items():
                    if not isinstance(variants, dict):
                        continue
                    name = str(raw_name or "").strip()
                    if not name:
                        continue
                    rows = result.setdefault(name, {})
                    for raw_variant, attributes in variants.items():
                        try:
                            variant = int(raw_variant)
                        except (TypeError, ValueError):
                            continue
                        parsed = _parse_named_attributes(attributes)
                        if parsed:
                            rows[variant] = parsed
    return {
        name: tuple(sorted(variants.items()))
        for name, variants in result.items()
    }


@lru_cache(maxsize=4)
def _load_inspect_suffixes(
    data_root: str,
) -> dict[str, tuple[tuple[str, tuple[tuple[str, float], ...]], ...]]:
    table_root = Path(data_root) / "modular" / "tables" / "equipment" / "suffix"
    result: dict[str, tuple[tuple[str, tuple[tuple[str, float], ...]], ...]] = {}
    for path in sorted(table_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for levels in payload.values():
            if not isinstance(levels, dict):
                continue
            for items in levels.values():
                if not isinstance(items, dict):
                    continue
                for raw_name, suffixes in items.items():
                    if not isinstance(suffixes, dict):
                        continue
                    rows: list[tuple[str, tuple[tuple[str, float], ...]]] = []
                    for raw_suffix, attributes in suffixes.items():
                        suffix_name = str(raw_suffix or "").strip()
                        if suffix_name:
                            rows.append(
                                (suffix_name, _parse_named_attributes(attributes))
                            )
                    if rows:
                        result[str(raw_name or "").strip()] = tuple(rows)
    return result


@lru_cache(maxsize=4)
def _load_verified_talisman_base_attributes(
    data_root: str,
) -> dict[str, tuple[tuple[int, float, float], ...]]:
    """Load audited in-game talisman values instead of encoded DNT scalars.

    Current item-table rows store transformed floating-point coefficients for
    talismans (for example, the C-grade wisdom item carries ``309.77`` while
    its tooltip value is ``59``).  ``talisman.json`` is the verified tooltip
    table used by the simulator.  Only names with one unambiguous value set
    are applied here; duplicate display names with different grades fail
    closed and keep their raw catalog value until an item-ID route is proven.
    """

    path = Path(data_root) / "modular" / "tables" / "talisman.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    candidates: dict[str, set[tuple[tuple[int, float, float], ...]]] = {}
    main = payload.get("主属性", {}) if isinstance(payload, dict) else {}
    if not isinstance(main, dict):
        return {}
    for quality_rows in main.values():
        if not isinstance(quality_rows, dict):
            continue
        for raw_name, raw_row in quality_rows.items():
            if not isinstance(raw_row, dict):
                continue
            raw_attributes = raw_row.get("属性", {})
            if not isinstance(raw_attributes, dict):
                continue
            attributes: list[tuple[int, float, float]] = []
            for raw_attribute, raw_value in raw_attributes.items():
                state_ids = _TALISMAN_ATTRIBUTE_STATE_IDS.get(str(raw_attribute))
                if not state_ids:
                    attributes = []
                    break
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    attributes = []
                    break
                attributes.extend((state_id, value, value) for state_id in state_ids)
            if not attributes:
                continue
            name = str(raw_name or "").strip().replace("之强化护符", "的强化护符")
            candidates.setdefault(name, set()).add(tuple(attributes))
    return {
        name: next(iter(values))
        for name, values in candidates.items()
        if len(values) == 1
    }


@lru_cache(maxsize=4)
def _load_verified_talisman_extra_values(data_root: str) -> dict[int, float]:
    """Return exact unscaled B-talisman option values keyed by state ID."""

    path = Path(data_root) / "modular" / "tables" / "talisman.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    extra_root = payload.get("额外属性", {}) if isinstance(payload, dict) else {}
    rows = extra_root.get("B", {}) if isinstance(extra_root, dict) else {}
    if not isinstance(rows, dict):
        return {}
    result: dict[int, float] = {}
    for raw_row in rows.values():
        if not isinstance(raw_row, dict):
            continue
        raw_attributes = raw_row.get("属性", {})
        if not isinstance(raw_attributes, dict):
            continue
        for raw_attribute, raw_value in raw_attributes.items():
            state_ids = _TALISMAN_ATTRIBUTE_STATE_IDS.get(str(raw_attribute))
            if not state_ids:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            for state_id in state_ids:
                result[state_id] = value
    return result


@lru_cache(maxsize=4)
def load_inspect_item_catalog(data_root: str) -> dict[int, InspectCatalogItem]:
    raw_items = _load_inspect_catalog_payload(data_root).get("items", {})
    jewelry_variants = _load_inspect_jewelry_variants(data_root)
    suffixes = _load_inspect_suffixes(data_root)
    verified_talismans = _load_verified_talisman_base_attributes(data_root)
    result: dict[int, InspectCatalogItem] = {}
    for raw_id, values in raw_items.items():
        if not isinstance(values, list) or len(values) < 2:
            continue
        try:
            template_id = int(raw_id)
            level = max(0, int(values[2])) if len(values) > 2 else 0
        except (TypeError, ValueError):
            continue
        name = str(values[0] or "").strip()
        if not name:
            continue
        category = _CATEGORY_NAMES.get(str(values[1]), "装备")
        attributes = _parse_attribute_triples(values[5] if len(values) > 5 else ())
        if category == "护符":
            attributes = verified_talismans.get(
                name.replace("之强化护符", "的强化护符"),
                attributes,
            )
        result[template_id] = InspectCatalogItem(
            template_id=template_id,
            name=name,
            category=category,
            level=level,
            description=str(values[3] or "").strip() if len(values) > 3 else "",
            job_class_codes=_parse_int_tuple(values[4] if len(values) > 4 else ()),
            attributes=attributes,
            enchant_id=_safe_int(values[6] if len(values) > 6 else 0),
            potential_id=_safe_int(values[7] if len(values) > 7 else 0),
            glyph_potential_id=_safe_int(values[8] if len(values) > 8 else 0),
            type_param3_id=_safe_int(values[9] if len(values) > 9 else 0),
            jewelry_variants=jewelry_variants.get(name, ()),
            suffixes=suffixes.get(name, ()),
        )
    return result


@lru_cache(maxsize=4)
def load_inspect_enchant_catalog(
    data_root: str,
) -> dict[tuple[int, int], tuple[tuple[int, float], ...]]:
    raw_enchants = _load_inspect_catalog_payload(data_root).get("enchants", {})
    if not isinstance(raw_enchants, dict):
        return {}
    result: dict[tuple[int, int], tuple[tuple[int, float], ...]] = {}
    for raw_key, raw_attributes in raw_enchants.items():
        try:
            enchant_id_text, level_text = str(raw_key).split(":", 1)
            key = (int(enchant_id_text), int(level_text))
        except (TypeError, ValueError):
            continue
        attributes = _parse_attribute_pairs(raw_attributes)
        if attributes:
            result[key] = attributes
    return result


@lru_cache(maxsize=4)
def load_inspect_potential_catalog(
    data_root: str,
) -> dict[tuple[int, int], tuple[tuple[int, float], ...]]:
    raw_potentials = _load_inspect_catalog_payload(data_root).get("potentials", {})
    if not isinstance(raw_potentials, dict):
        return {}
    raw_items = _load_inspect_catalog_payload(data_root).get("items", {})
    talisman_potential_ids = {
        _safe_int(values[7] if len(values) > 7 else 0)
        for values in raw_items.values()
        if isinstance(values, list)
        and len(values) > 1
        and str(values[1]) == "t"
    }
    talisman_potential_ids.discard(0)
    verified_talisman_extras = _load_verified_talisman_extra_values(data_root)
    result: dict[tuple[int, int], tuple[tuple[int, float], ...]] = {}
    for raw_key, raw_attributes in raw_potentials.items():
        try:
            potential_id_text, option_code_text = str(raw_key).split(":", 1)
            key = (int(potential_id_text), int(option_code_text))
        except (TypeError, ValueError):
            continue
        attributes = _parse_attribute_pairs(raw_attributes)
        if key[0] in talisman_potential_ids and attributes:
            attributes = tuple(
                (state_id, verified_talisman_extras.get(state_id, value))
                for state_id, value in attributes
            )
        if isinstance(raw_attributes, (list, tuple)):
            result[key] = attributes
    return result


@lru_cache(maxsize=4)
def load_inspect_inherent_potential_catalog(
    data_root: str,
) -> dict[tuple[int, int], tuple[tuple[int, float], ...]]:
    raw_potentials = _load_inspect_catalog_payload(data_root).get(
        "inherent_potentials", {}
    )
    if not isinstance(raw_potentials, dict):
        return {}
    result: dict[tuple[int, int], tuple[tuple[int, float], ...]] = {}
    for raw_key, raw_attributes in raw_potentials.items():
        try:
            potential_id_text, option_code_text = str(raw_key).split(":", 1)
            key = (int(potential_id_text), int(option_code_text))
        except (TypeError, ValueError):
            continue
        if isinstance(raw_attributes, (list, tuple)):
            result[key] = _parse_attribute_pairs(raw_attributes)
    return result


@lru_cache(maxsize=4)
def load_inspect_suffix_potential_catalog(
    data_root: str,
) -> dict[
    tuple[int, int],
    tuple[str, tuple[tuple[int, float], ...]],
]:
    """Load the client TypeParam3 potential vector in physical DNT order."""

    raw_potentials = _load_inspect_catalog_payload(data_root).get(
        "suffix_potentials", {}
    )
    if not isinstance(raw_potentials, dict):
        return {}
    result: dict[
        tuple[int, int],
        tuple[str, tuple[tuple[int, float], ...]],
    ] = {}
    for raw_key, raw_value in raw_potentials.items():
        try:
            potential_id_text, option_code_text = str(raw_key).split(":", 1)
            key = (int(potential_id_text), int(option_code_text))
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 2:
            continue
        raw_attributes = raw_value[1]
        if not isinstance(raw_attributes, (list, tuple)):
            continue
        result[key] = (
            str(raw_value[0] or "").strip(),
            _parse_attribute_pairs(raw_attributes),
        )
    return result


@lru_cache(maxsize=4)
def load_inspect_set_catalog(
    data_root: str,
) -> tuple[
    dict[str, int],
    dict[tuple[str, str], int],
    dict[int, tuple[tuple[int, tuple[tuple[str, float], ...]], ...]],
]:
    """Load unambiguous item-to-set routes and the current activation tiers."""

    table_root = Path(data_root) / "modular" / "tables"
    try:
        indexes = json.loads(
            (table_root / "auxiliary_indexes.json").read_text(encoding="utf-8")
        )
        raw_bonuses = json.loads(
            (table_root / "set_bonus.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}, {}

    equipment_candidates: dict[str, set[int]] = defaultdict(set)
    raw_equipment = indexes.get("装备套装", {}) if isinstance(indexes, dict) else {}
    if isinstance(raw_equipment, dict):
        for raw_key, raw_set_id in raw_equipment.items():
            _job, separator, name = str(raw_key).partition("|")
            try:
                set_id = int(raw_set_id)
            except (TypeError, ValueError):
                continue
            if separator and name and set_id > 0:
                equipment_candidates[name].add(set_id)
    equipment = {
        name: next(iter(set_ids))
        for name, set_ids in equipment_candidates.items()
        if len(set_ids) == 1
    }

    jewelry: dict[tuple[str, str], int] = {}
    raw_jewelry = indexes.get("饰品套装", {}) if isinstance(indexes, dict) else {}
    if isinstance(raw_jewelry, dict):
        for raw_key, raw_set_id in raw_jewelry.items():
            slot, separator, name = str(raw_key).partition("|")
            try:
                set_id = int(raw_set_id)
            except (TypeError, ValueError):
                continue
            if separator and slot and name and set_id > 0:
                jewelry[(slot, name)] = set_id

    bonuses: dict[
        int, tuple[tuple[int, tuple[tuple[str, float], ...]], ...]
    ] = {}
    if isinstance(raw_bonuses, dict):
        for raw_set_id, raw_tiers in raw_bonuses.items():
            try:
                set_id = int(raw_set_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_tiers, dict):
                continue
            tiers: list[tuple[int, tuple[tuple[str, float], ...]]] = []
            for raw_count, raw_attributes in raw_tiers.items():
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    continue
                attributes = _parse_named_attributes(raw_attributes)
                if count > 0 and attributes:
                    tiers.append((count, attributes))
            if tiers:
                bonuses[set_id] = tuple(sorted(tiers))
    return equipment, jewelry, bonuses


@lru_cache(maxsize=4)
def load_inspect_card_mastery_catalog(
    data_root: str,
) -> dict[int, tuple[str, int, int, bool]]:
    """Load the client cardskill row used by S2C 0D 19 entries."""

    raw_rows = _load_inspect_catalog_payload(data_root).get(
        "card_masteries", {}
    )
    if not isinstance(raw_rows, dict):
        return {}
    result: dict[int, tuple[str, int, int, bool]] = {}
    for raw_id, raw_value in raw_rows.items():
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            mastery_id = int(raw_id)
            name = str(raw_value[0] or "").strip()
            tree_slot = int(raw_value[1])
            state_type = int(raw_value[2])
            hidden = bool(raw_value[3])
        except (TypeError, ValueError):
            continue
        if mastery_id > 0 and name:
            result[mastery_id] = (name, tree_slot, state_type, hidden)
    return result


@lru_cache(maxsize=4)
def load_inspect_core_rune_catalog(
    data_root: str,
) -> dict[tuple[int, int, int], tuple[str, float, float, float]]:
    """Load exact core-rune values from skillleveltable_corerune."""

    raw_rows = _load_inspect_catalog_payload(data_root).get("core_runes", {})
    if not isinstance(raw_rows, dict):
        return {}
    result: dict[
        tuple[int, int, int], tuple[str, float, float, float]
    ] = {}
    for raw_key, raw_value in raw_rows.items():
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            level_text, param_text, rank_text = str(raw_key).split(":", 2)
            key = (int(level_text), int(param_text), int(rank_text))
            value = (
                str(raw_value[0] or "").strip(),
                float(raw_value[1]),
                float(raw_value[2]),
                float(raw_value[3]),
            )
        except (TypeError, ValueError):
            continue
        if key[0] > 0 and key[1] > 0 and key[2] > 0 and value[0]:
            result[key] = value
    return result


def inspect_core_rune_text(
    value: tuple[str, float, float, float],
) -> str:
    conversion, ratio, coefficient, maximum = value
    return (
        f"{conversion}，转换比例 {_format_number(ratio)}%"
        f"，转换系数 {_format_number(coefficient)}"
        f"，最大扣除值 {_format_number(maximum)}"
    )


def inspect_active_set_details(
    set_catalog: tuple[
        dict[str, int],
        dict[tuple[str, str], int],
        dict[int, tuple[tuple[int, tuple[tuple[str, float], ...]], ...]],
    ],
    equipped_items: tuple[tuple[str, InspectCatalogItem], ...],
) -> tuple[tuple[str, str], ...]:
    """Return only set tiers activated by the exact decoded equipment list."""

    equipment, jewelry, bonuses = set_catalog
    counts: Counter[tuple[str, int]] = Counter()
    members: dict[tuple[str, int], list[str]] = defaultdict(list)
    for slot, item in equipped_items:
        normalized_slot = "戒指" if slot.startswith("戒指") else slot
        if normalized_slot in {"项链", "耳环", "戒指"}:
            set_id = jewelry.get((normalized_slot, item.name), 0)
            scope = "首饰"
        else:
            set_id = equipment.get(item.name, 0)
            scope = "装备"
        if set_id <= 0:
            continue
        key = (scope, set_id)
        counts[key] += 1
        members[key].append(item.name)

    rows: list[tuple[str, str]] = []
    for (scope, set_id), count in sorted(counts.items()):
        active = [tier for tier in bonuses.get(set_id, ()) if tier[0] <= count]
        if not active:
            continue
        combined: dict[str, float] = defaultdict(float)
        for _required, attributes in active:
            for name, value in attributes:
                combined[name] += value
        tiers_text = "/".join(str(required) for required, _attrs in active)
        attribute_text = _named_attributes_text(tuple(combined.items()))
        value = (
            f"套装ID {set_id}；当前 {count} 件；已激活 {tiers_text} 件档"
            + (f"；{attribute_text}" if attribute_text else "")
        )
        rows.append((f"套装效果|{scope} {set_id}", value))
    return tuple(rows)


@lru_cache(maxsize=4)
def load_inspect_glyph_potential_catalog(
    data_root: str,
) -> dict[tuple[int, int], tuple[tuple[int, float], ...]]:
    raw_potentials = _load_inspect_catalog_payload(data_root).get(
        "glyph_potentials", {}
    )
    if not isinstance(raw_potentials, dict):
        return {}
    result: dict[tuple[int, int], tuple[tuple[int, float], ...]] = {}
    for raw_key, raw_attributes in raw_potentials.items():
        try:
            potential_id_text, option_code_text = str(raw_key).split(":", 1)
            key = (int(potential_id_text), int(option_code_text))
        except (TypeError, ValueError):
            continue
        if isinstance(raw_attributes, (list, tuple)):
            # An empty DNT row is a resolved "no extra attribute" option.
            # Preserve its key so callers can distinguish it from an unknown
            # selector that is absent from the table.
            result[key] = _parse_attribute_pairs(raw_attributes)
    return result


@lru_cache(maxsize=4)
def load_inspect_engraving_potence_catalog(
    data_root: str,
) -> dict[
    tuple[int, int],
    tuple[tuple[int, float, float], int] | None,
]:
    """Load the current client's exact imprint instance-option route.

    The key is ``(itemtable_imprintequip._TypeParam1, record[29])``.  The
    value contains the fixed state/value from ``potencetable`` and the grade
    icon obtained by the client's inclusive ``imprintinggradetable`` lookup.
    ``None`` is a real empty option; an absent key is unresolved data.
    """
    raw_options = _load_inspect_catalog_payload(data_root).get(
        "engraving_potences", {}
    )
    if not isinstance(raw_options, dict):
        return {}
    result: dict[
        tuple[int, int],
        tuple[tuple[int, float, float], int] | None,
    ] = {}
    for raw_key, raw_value in raw_options.items():
        try:
            potence_text, option_text = str(raw_key).split(":", 1)
            key = (int(potence_text), int(option_text))
        except (TypeError, ValueError):
            continue
        if raw_value is None:
            result[key] = None
            continue
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            attribute = (
                int(raw_value[0]), float(raw_value[1]), float(raw_value[2])
            )
            grade_icon = int(raw_value[3])
        except (TypeError, ValueError):
            continue
        result[key] = (attribute, grade_icon)
    return result


@lru_cache(maxsize=4)
def load_inspect_engraving_extra_catalog(
    data_root: str,
) -> dict[tuple[int, int], tuple[tuple[int, float, float], int]]:
    raw_extras = _load_inspect_catalog_payload(data_root).get(
        "engraving_extras", {}
    )
    if not isinstance(raw_extras, dict):
        return {}
    result: dict[tuple[int, int], tuple[tuple[int, float, float], int]] = {}
    for raw_key, raw_value in raw_extras.items():
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            selector_text, option_text = str(raw_key).split(":", 1)
            key = (int(selector_text), int(option_text))
            attribute = (
                int(raw_value[0]), float(raw_value[1]), float(raw_value[2])
            )
            grade_icon = int(raw_value[3])
        except (TypeError, ValueError):
            continue
        result[key] = (attribute, grade_icon)
    return result


@lru_cache(maxsize=4)
def load_inspect_engraving_record_extra_catalog(
    data_root: str,
) -> dict[
    tuple[int, int, int],
    tuple[tuple[int, float, float], int] | None,
]:
    """Load slot-scoped v576 engraving instance selectors.

    The first key component is itemtable_imprintequip ``_TypeParam2`` (the
    engraving slot).  The remaining components are the two record bytes at
    offsets 29 and 30.  Keeping the slot in the key is essential: identical
    record bytes can represent different attributes on shoes and earrings.
    """
    raw_extras = _load_inspect_catalog_payload(data_root).get(
        "engraving_record_extras", {}
    )
    if not isinstance(raw_extras, dict):
        return {}
    result: dict[
        tuple[int, int, int],
        tuple[tuple[int, float, float], int] | None,
    ] = {}
    for raw_key, raw_value in raw_extras.items():
        try:
            slot_text, field_29_text, field_30_text = str(raw_key).split(":", 2)
            key = (int(slot_text), int(field_29_text), int(field_30_text))
        except (TypeError, ValueError):
            continue
        if raw_value is None:
            result[key] = None
            continue
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            attribute = (
                int(raw_value[0]), float(raw_value[1]), float(raw_value[2])
            )
            grade_icon = int(raw_value[3])
        except (TypeError, ValueError):
            continue
        result[key] = (attribute, grade_icon)
    return result


@lru_cache(maxsize=4)
def load_inspect_engraving_template_record_extra_catalog(
    data_root: str,
) -> dict[
    tuple[int, int, int],
    tuple[tuple[int, float, float], int] | None,
]:
    """Load template-scoped engraving selectors.

    A template ID is more specific than the imprint slot: different templates
    can reuse identical selector bytes while granting different attributes.
    """
    raw_extras = _load_inspect_catalog_payload(data_root).get(
        "engraving_template_record_extras", {}
    )
    if not isinstance(raw_extras, dict):
        return {}
    result: dict[
        tuple[int, int, int],
        tuple[tuple[int, float, float], int] | None,
    ] = {}
    for raw_key, raw_value in raw_extras.items():
        try:
            template_text, field_29_text, field_30_text = str(raw_key).split(
                ":", 2
            )
            key = (
                int(template_text), int(field_29_text), int(field_30_text)
            )
        except (TypeError, ValueError):
            continue
        if raw_value is None:
            result[key] = None
            continue
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) < 4:
            continue
        try:
            attribute = (
                int(raw_value[0]), float(raw_value[1]), float(raw_value[2])
            )
            grade_icon = int(raw_value[3])
        except (TypeError, ValueError):
            continue
        result[key] = (attribute, grade_icon)
    return result


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({parsed for item in value if (parsed := _safe_int(item)) > 0}))


def _parse_attribute_triples(
    value: object,
) -> tuple[tuple[int, float, float], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[tuple[int, float, float]] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            result.append((int(row[0]), float(row[1]), float(row[2])))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _parse_attribute_pairs(value: object) -> tuple[tuple[int, float], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[tuple[int, float]] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            result.append((int(row[0]), float(row[1])))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-5:
        return f"{round(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _format_attribute(state_id: int, minimum: float, maximum: float) -> str:
    label = _STATE_NAMES.get(state_id, f"属性ID {state_id}")
    if state_id in _PERCENT_STATE_IDS:
        minimum *= 100.0
        maximum *= 100.0
        suffix = "%"
    else:
        suffix = ""
    if abs(minimum - maximum) < 1e-5:
        value_text = _format_number(maximum)
    else:
        value_text = f"{_format_number(minimum)}~{_format_number(maximum)}"
    prefix = "+" if minimum >= 0.0 and maximum >= 0.0 else ""
    return f"{label} {prefix}{value_text}{suffix}"


def inspect_attributes_text(
    attributes: tuple[tuple[int, float, float], ...]
    | tuple[tuple[int, float], ...]
    | list[tuple[int, float]],
) -> str:
    rows: list[str] = []
    for row in attributes:
        if len(row) >= 3:
            state_id, minimum, maximum = int(row[0]), float(row[1]), float(row[2])
        elif len(row) >= 2:
            state_id, minimum = int(row[0]), float(row[1])
            maximum = minimum
        else:
            continue
        if abs(minimum) < 1e-7 and abs(maximum) < 1e-7:
            continue
        rows.append(_format_attribute(state_id, minimum, maximum))
    return "，".join(rows)


def _glyph_attributes_text(
    item: InspectCatalogItem,
    attributes: tuple[tuple[int, float], ...],
) -> str:
    # potentialtable_glyph uses the three resistance fields in the order
    # described by the resistance glyph, rather than the generic item-state
    # labels used by equipment tables.
    if "抵抗之远征队纹章" not in item.name:
        return inspect_attributes_text(attributes)
    resistance_names = {
        13: "致命一击抵抗",
        11: "硬直抵抗",
        15: "眩晕抵抗",
    }
    rows: list[str] = []
    for state_id, value in attributes:
        name = resistance_names.get(int(state_id))
        if name:
            rows.append(f"{name} +{_format_number(float(value))}")
        else:
            rows.append(_format_attribute(int(state_id), float(value), float(value)))
    return "，".join(rows)


def _named_attributes_text(attributes: tuple[tuple[str, float], ...]) -> str:
    rows: list[str] = []
    for name, value in attributes:
        suffix = "%" if name.endswith("%") else ""
        label = name[:-1] if suffix else name
        prefix = "+" if value >= 0.0 else ""
        rows.append(f"{label} {prefix}{_format_number(value)}{suffix}")
    return "，".join(rows)


def _core_rune_text(param_id: int, coefficient_rank: int) -> str:
    kind = param_id // 100
    ratio_rank = param_id % 100
    if not (1 <= kind <= len(_CORE_RUNE_CONVERSIONS)):
        return ""
    if not (1 <= ratio_rank <= 21 and 1 <= coefficient_rank <= 21):
        return ""
    conversion = _CORE_RUNE_CONVERSIONS[kind - 1]
    ratio = 10.0 + (ratio_rank - 1) * 0.5
    coefficient_min, coefficient_max = _CORE_RUNE_COEFFICIENT_RANGES.get(
        conversion, (0.25, 0.5)
    )
    coefficient = coefficient_min + (
        coefficient_rank - 1
    ) * (coefficient_max - coefficient_min) / 20
    return (
        f"{conversion}，转换比例 {_format_number(ratio)}%"
        f"（档 {ratio_rank}），转换系数 {_format_number(coefficient)}"
        f"（档 {coefficient_rank}）"
    )


def inspect_record_category(group_index: int, item: InspectCatalogItem | None) -> str:
    if item is not None:
        return item.category
    if group_index == 2:
        return "纹章"
    if group_index == 3:
        return "刻印"
    return "装备"


def inspect_record_slot(
    group_index: int,
    record_index: int,
    category: str,
    item: InspectCatalogItem | None,
) -> str:
    if category == "护符":
        return (
            _TALISMAN_SLOTS[record_index]
            if 0 <= record_index < len(_TALISMAN_SLOTS)
            else f"护符槽 {record_index + 1}"
        )
    if category == "石板":
        return f"石板槽 {max(1, record_index - 12)}"
    if category == "纹章":
        return f"纹章槽 {record_index + 1}"
    if category == "刻印":
        if item is not None and "刻印卷轴" in item.name:
            return item.name.replace("刻印卷轴", "").strip() or f"刻印槽 {record_index + 1}"
        return f"刻印槽 {record_index + 1}"
    if group_index == 1:
        return _COSTUME_SLOTS.get(record_index, f"时装槽 {record_index + 1}")
    return _EQUIPMENT_SLOTS.get(record_index, f"装备槽 {record_index + 1}")


def inspect_talisman_slot_multiplier(record_index: int) -> float:
    if 0 <= record_index < len(_TALISMAN_SLOT_MULTIPLIERS):
        return _TALISMAN_SLOT_MULTIPLIERS[record_index]
    return 1.0


def _scale_fixed_attributes(
    attributes: tuple[tuple[int, float, float], ...]
    | tuple[tuple[int, float], ...],
    multiplier: float,
    *,
    truncate: bool = False,
) -> tuple:
    if abs(multiplier - 1.0) < 1e-7 and not truncate:
        return attributes
    scaled: list[tuple] = []
    for row in attributes:
        if len(row) >= 3:
            scaled.append(
                (
                    int(row[0]),
                    float(int(int(float(row[1])) * multiplier)),
                    float(int(int(float(row[2])) * multiplier)),
                )
            )
        elif len(row) >= 2:
            scaled.append(
                (int(row[0]), float(int(int(float(row[1])) * multiplier)))
            )
    return tuple(scaled)


def inspect_record_detail(
    template_id: int,
    enhance_level: int,
    item: InspectCatalogItem | None,
    enchant_attributes: tuple[tuple[int, float], ...] = (),
    dynamic_attributes: tuple[tuple[int, float], ...] = (),
    potential_attributes: tuple[tuple[int, float], ...] = (),
    potential_attributes_resolved: bool | None = None,
    potential_option_code: int = 0,
    inherent_attributes: tuple[tuple[int, float], ...] = (),
    inherent_attributes_resolved: bool | None = None,
    inherent_option_code: int = 0,
    inherent_label: str = "实例固有属性",
    glyph_attributes: tuple[tuple[int, float], ...] = (),
    glyph_attributes_resolved: bool | None = None,
    glyph_option_code: int = 0,
    engraving_extra_attributes: tuple[tuple[int, float, float], ...] = (),
    engraving_extra_resolved: bool | None = None,
    engraving_extra_selector: tuple[int, int] | None = None,
    accessory_variant: int = 0,
    suffix_index: int | None = None,
    suffix_name: str = "",
    suffix_attributes: tuple[tuple[int, float], ...] = (),
    suffix_resolved: bool | None = None,
    suffix_option_code: int = 0,
    core_rune_param_id: int = 0,
    core_rune_coefficient_rank: int = 0,
    core_rune_resolved: bool | None = None,
    core_rune_detail: str = "",
    attribute_multiplier: float = 1.0,
) -> str:
    if item is None:
        return f"未在 v576 物品表中找到（0x{template_id:08X}）"
    parts = [item.name]
    if enhance_level > 0:
        parts.append(f"强化 +{enhance_level}")
    spirit_grade = inspect_spirit_grade(item)
    if spirit_grade:
        parts.append(f"精灵品质：{spirit_grade}")
    if item.level >= 10:
        parts.append(f"需求 Lv.{item.level}")
    is_talisman = item.category == "护符"
    if is_talisman:
        parts.append(f"槽位倍率：{_format_number(attribute_multiplier * 100)}%")
    base_attributes = _scale_fixed_attributes(
        item.attributes,
        attribute_multiplier if is_talisman else 1.0,
        truncate=is_talisman,
    )
    base_text = inspect_attributes_text(base_attributes)
    if base_text:
        parts.append(f"{'实际主属性' if is_talisman else '基础属性'}：{base_text}")
    enchant_text = inspect_attributes_text(enchant_attributes)
    if enchant_text:
        parts.append(f"强化属性：{enchant_text}")
    dynamic_text = inspect_attributes_text(
        _scale_fixed_attributes(
            dynamic_attributes,
            attribute_multiplier if is_talisman else 1.0,
            truncate=is_talisman,
        )
    )
    if dynamic_text:
        parts.append(f"当前附加：{dynamic_text}")
    inherent_text = inspect_attributes_text(inherent_attributes)
    if inherent_text:
        parts.append(f"{inherent_label}：{inherent_text}")
    elif inherent_attributes_resolved is True:
        parts.append(f"{inherent_label}：无")
    elif inherent_attributes_resolved is False:
        selector_text = (
            f"（选择码 {inherent_option_code}）" if inherent_option_code else ""
        )
        parts.append(f"{inherent_label}：未解码{selector_text}")
    potential_text = inspect_attributes_text(
        _scale_fixed_attributes(
            potential_attributes,
            attribute_multiplier if is_talisman else 1.0,
            truncate=is_talisman,
        )
    )
    if potential_text:
        parts.append(f"{'额外属性' if is_talisman else '附魔'}：{potential_text}")
    elif potential_attributes_resolved is True:
        parts.append(f"{'额外属性' if is_talisman else '附魔'}：无")
    elif potential_attributes_resolved is False:
        selector_text = (
            f"（选择码 {potential_option_code}）" if potential_option_code else ""
        )
        parts.append(
            f"{'额外属性' if is_talisman else '附魔'}：未解码{selector_text}"
        )
    glyph_text = _glyph_attributes_text(item, glyph_attributes)
    if glyph_text:
        parts.append(f"纹章实际属性：{glyph_text}")
    elif glyph_attributes_resolved is True:
        parts.append("纹章实际属性：无")
    elif glyph_attributes_resolved is False:
        selector_text = f"（选择码 {glyph_option_code}）" if glyph_option_code else ""
        parts.append(f"纹章实际属性：未解码{selector_text}")
    engraving_extra_text = inspect_attributes_text(engraving_extra_attributes)
    if engraving_extra_text:
        parts.append(f"额外属性：{engraving_extra_text}")
    elif engraving_extra_resolved is True:
        parts.append("额外属性：无")
    elif engraving_extra_resolved is False:
        selector_text = ""
        if engraving_extra_selector is not None:
            selector_text = (
                f"（模板 0x{template_id:08X}，实例选项 "
                f"{engraving_extra_selector[0]}，相邻原始字节 "
                f"{engraving_extra_selector[1]}）"
            )
        parts.append(f"额外属性：未解码{selector_text}")
    suffix_text = inspect_attributes_text(suffix_attributes)
    if suffix_name or suffix_text:
        suffix_detail = suffix_name
        if suffix_text:
            suffix_detail += f"（{suffix_text}）" if suffix_detail else suffix_text
        parts.append(f"后缀：{suffix_detail}")
    elif suffix_resolved is True:
        parts.append("后缀：无")
    elif suffix_resolved is False:
        selector_text = f"（选择码 {suffix_option_code}）" if suffix_option_code else ""
        parts.append(f"后缀：未解码{selector_text}")
    if "源铸" in item.name:
        if core_rune_resolved is False:
            raw = ""
            if core_rune_param_id or core_rune_coefficient_rank:
                raw = (
                    f"（原始字段 {core_rune_param_id}/{core_rune_coefficient_rank}）"
                )
            parts.append(f"源铸属性：未解码{raw}")
        elif core_rune_resolved is True:
            parts.append(
                f"源铸属性：{core_rune_detail}"
                if core_rune_detail else "源铸属性：无"
            )
    if item.description:
        parts.append(item.description)
    return "；".join(parts)
