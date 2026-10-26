from __future__ import annotations

from functools import lru_cache

from ..job_mechanics import (
    is_ring_strike,
    ring_strike_default_hit_count,
    ring_strike_output_multiplier,
)

NONE_TEXT = '无'

class NativeBuildMixin:
    @staticmethod
    def _skill_defaults() -> dict[str, Any]:
        builtin = MODULAR_DIR / "tables" / "skills" / "skill_build_defaults_builtin.json"
        try:
            payload = json.loads(builtin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}
    def _skill_defaults_for_job(self, job: str) -> dict[str, Any]:
        current = norm(job)
        stored = self._skill_defaults().get(current, {})
        defaults = copy.deepcopy(stored) if isinstance(stored, dict) else {}
        defaults.setdefault("second_job", current)
        defaults.setdefault("level_cap", 70)
        if not isinstance(defaults.get("skill_build"), dict):
            defaults["skill_build"] = {"second_job": current, "levels": {}}
        else:
            defaults["skill_build"].setdefault("second_job", current)
        if not isinstance(defaults.get("skill_mastery_build"), dict):
            defaults["skill_mastery_build"] = {
                "second_job": current,
                "levels": {},
            }
        else:
            defaults["skill_mastery_build"].setdefault("second_job", current)
        if not isinstance(defaults.get("skill_glyphs"), dict):
            defaults["skill_glyphs"] = {
                "second_job": current,
                "count": 4,
                "rows": [],
            }
        if not isinstance(defaults.get("skill_multiplier_overrides"), dict):
            defaults["skill_multiplier_overrides"] = {
                "second_job": current,
                "rows": [],
            }
        return defaults
    def _default_build(self, slot: str) -> dict[str, Any]:
        key = slot if slot in self.store.defaults else "build1"
        build = copy.deepcopy(self.store.defaults[key])
        self.apply_job_defaults(build, norm(build.get("second_job")))
        target = AUTO_EQUIP_TARGETS["70"][0][0]
        build = self.apply_build_preset(
            build,
            "equipment",
            {"level": "70", "target": target},
        )
        build = self.apply_build_preset(build, "glyphs", {"aExtra": "自动"})
        build = self.apply_build_preset(build, "runes")
        build = self.apply_build_preset(build, "cards", {"grade": "L"})
        profile = self._job_profile(build)
        build["title"] = max(
            self.store.titles(),
            key=lambda title: self._score_attrs_for_profile(
                self.calculator.title_attrs(title), profile
            ),
            default=NONE_TEXT,
        )
        return build
    def apply_job_defaults(self, build: dict[str, Any], job: str) -> dict[str, Any]:
        defaults = self._skill_defaults_for_job(job)
        for key in ("skill_build", "skill_mastery_build", "skill_glyphs", "skill_multiplier_overrides"):
            build[key] = copy.deepcopy(defaults[key])
        build.setdefault("skill_sim_settings", {
            "long_window": 125.0,
            "long_cocktail": True,
            "burst": False,
            "accel": False,
            "cocktail": False,
        })
        build.setdefault("skill_sim", [])
        build.setdefault("team_sim", {"members": [{"job": job}, *({"job": NONE_TEXT} for _ in range(7))]})
        build.setdefault("engravings", [
            {
                "slot": slot,
                "grade": NONE_TEXT,
                "value_rank": NONE_TEXT,
                "main_value": 0.0,
                "secondary_value": 0.0,
                "extra": NONE_TEXT,
                "extra_tier": NONE_TEXT,
                "extra_value": 0.0,
            }
            for slot in ENGRAVING_SLOTS
        ])
        return build
    def _normalize_fashion_rows(self, build: dict[str, Any]) -> dict[str, Any]:
        """Add newly introduced fashion slots without discarding saved choices."""
        rows = build.get("fashion", [])
        if not isinstance(rows, list):
            rows = []
        known: dict[str, dict[str, Any]] = {}
        extra: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            slot = norm(item.get("slot"))
            if slot in FASHION_SLOTS and slot not in known:
                known[slot] = copy.deepcopy(item)
            elif slot not in FASHION_SLOTS:
                extra.append(copy.deepcopy(item))
        normalized: list[dict[str, Any]] = []
        for slot in FASHION_SLOTS:
            item = known.get(slot, {"slot": slot, "name": NONE_TEXT})
            item["slot"] = slot
            item["name"] = norm(item.get("name")) or NONE_TEXT
            normalized.append(item)
        build["fashion"] = [*normalized, *extra]
        return build
    def _normalize_talisman_rows(self, build: dict[str, Any]) -> dict[str, Any]:
        """Migrate old builds to the fixed eight-slot talisman layout."""
        rows = build.get("talismans", [])
        if not isinstance(rows, list):
            rows = []
        by_slot = {
            {
                "150%槽位1": "175%槽位1",
                "150%槽位2": "175%槽位2",
            }.get(norm(item.get("slot")), norm(item.get("slot"))): copy.deepcopy(item)
            for item in rows
            if isinstance(item, dict)
            and {
                "150%槽位1": "175%槽位1",
                "150%槽位2": "175%槽位2",
            }.get(norm(item.get("slot")), norm(item.get("slot")))
            in TALISMAN_SLOTS
        }
        normalized: list[dict[str, Any]] = []
        for slot in TALISMAN_SLOTS:
            item = by_slot.get(slot, {})
            grade = norm(item.get("grade")) or NONE_TEXT
            if grade not in self.store.talisman_grades():
                grade = NONE_TEXT
            name = norm(item.get("name")) or NONE_TEXT
            if name not in self.store.talisman_names(grade):
                name = NONE_TEXT
            extra = norm(item.get("extra")) or NONE_TEXT
            if extra not in self.store.talisman_extra_options(grade, name):
                extra = NONE_TEXT
            normalized.append({
                "slot": slot,
                "grade": grade,
                "name": name,
                "extra": extra,
            })
        build["talismans"] = normalized
        return build
    def _normalize_glyph_s_rows(self, build: dict[str, Any]) -> dict[str, Any]:
        """Keep every fixed S-glyph base family in a stable editable column.

        Rise glyphs always expose four base families, Resistance exposes
        three, and Attack/Root expose two.  Older saved builds only had two
        fields, so missing families migrate to their in-game baseline rank 1
        without discarding any rank the user already selected.
        """
        rows = build.get("glyph_s", [])
        if not isinstance(rows, list):
            return build
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = norm(item.get("name"))
            options = [
                value for value in self.store.glyph_s_base_options(name)
                if value != NONE_TEXT
            ][:4]
            if not options:
                continue
            rank_by_attr: dict[str, Any] = {}
            for index in range(1, 5):
                attr = norm(item.get(f"base_attr{index}"))
                rank = item.get(f"rank{index}")
                if attr in options and norm(rank) not in ("", NONE_TEXT):
                    rank_by_attr[attr] = rank
            for index in range(1, 5):
                if index <= len(options):
                    attr = options[index - 1]
                    item[f"base_attr{index}"] = attr
                    item[f"rank{index}"] = rank_by_attr.get(attr, "1")
                else:
                    item[f"base_attr{index}"] = NONE_TEXT
                    item[f"rank{index}"] = None
        return build
    def _normalize_engraving_rows(self, build: dict[str, Any]) -> dict[str, Any]:
        """Canonicalize editable engraving rolls without touching unknown rolls.

        Builds saved before the A-grade three-piece table correction used the
        weapon progression (0.40/0.44/.../0.60).  Migrate those six positions
        to the real critical progression while preserving the parallel attack
        roll.  Packet-derived values that are not present in the editable
        table remain authoritative and are deliberately left unchanged.
        """
        rows = build.get("engravings", [])
        if not isinstance(rows, list):
            return build
        three_piece_slots = {"翅膀", "尾巴", "印花"}
        legacy_a_main = (0.40, 0.44, 0.48, 0.52, 0.56, 0.60)
        corrected_a_main = (0.40, 0.50, 0.60, 0.70, 0.80, 0.90)
        secondary_a = (0.20, 0.22, 0.24, 0.26, 0.28, 0.30)
        for item in rows:
            if not isinstance(item, dict):
                continue
            slot = norm(item.get("slot"))
            grade = norm(item.get("grade"))
            kind = (
                "防具双防" if slot in EQUIPMENT_SLOTS[:5]
                else "武器双攻" if slot in {"主手", "副手"}
                else "饰品生命" if slot in {"项链", "耳环", "戒指1", "戒指2"}
                else "三件套致命"
            )
            main_value = as_number(item.get("main_value"))
            secondary_value = as_number(item.get("secondary_value"))
            if slot in three_piece_slots and grade == "A":
                legacy_index = next((
                    index for index, value in enumerate(legacy_a_main)
                    if abs(main_value - value) <= 1e-9
                    and (
                        value != 0.60
                        or abs(secondary_value - secondary_a[index]) <= 1e-9
                    )
                ), None)
                # Values unique to the old table, plus old 0.60/0.30, retain
                # their original roll position during migration.
                if legacy_index is not None and (
                    main_value in legacy_a_main[1:5]
                    or (legacy_index == 5 and abs(secondary_value - 0.30) <= 1e-9)
                ):
                    main_value = corrected_a_main[legacy_index]
                    item["main_value"] = main_value
                    item["value_rank"] = f"{main_value:g}%"
                    item["secondary_value"] = secondary_a[legacy_index]
            main_options = self.store.engraving_main_value_options(kind, grade)
            rank = norm(item.get("value_rank"))
            if rank not in main_options:
                rank = next((
                    option for option in main_options
                    if abs(self._option_number(option) - as_number(item.get("main_value"))) <= 1e-9
                ), "")
            if not rank or rank == NONE_TEXT:
                continue
            index = main_options.index(rank)
            item["value_rank"] = rank
            item["main_value"] = self._option_number(rank)
            secondary_options = self.store.engraving_secondary_value_options(kind, grade)
            item["secondary_value"] = (
                self._option_number(secondary_options[index])
                if index < len(secondary_options)
                else 0.0
            )
        return build
    def get_build(self, slot: str) -> dict[str, Any]:
        key = "build2" if slot == "build2" else "build1"
        with self._state_lock:
            return copy.deepcopy(self._builds[key])
    def _load_builds(self, defaults: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._build_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        stored = payload if isinstance(payload, dict) else {}
        result: dict[str, dict[str, Any]] = {}
        for key, default in defaults.items():
            saved = stored.get(key)
            merged = copy.deepcopy(default)
            if isinstance(saved, dict):
                merged.update(copy.deepcopy(saved))
            default_cards = {
                norm(item.get("name")): norm(item.get("grade")) or NONE_TEXT
                for item in default.get("card_grades", [])
                if isinstance(item, dict) and norm(item.get("name"))
            }
            saved_cards = {
                norm(item.get("name")): norm(item.get("grade")) or NONE_TEXT
                for item in merged.get("card_grades", [])
                if isinstance(item, dict) and norm(item.get("name"))
            }
            merged["card_grades"] = [
                {
                    "name": name,
                    "grade": saved_cards.get(name, default_cards.get(name, NONE_TEXT)),
                }
                for name in self.store.card_names()
            ]
            result[key] = self._normalize_talisman_rows(
                self._normalize_fashion_rows(
                    self._normalize_engraving_rows(
                        self._normalize_glyph_s_rows(merged)
                    )
                )
            )
        return result
    def _persist_builds(self) -> None:
        self._build_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._build_state_path.with_suffix(self._build_state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._builds, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._build_state_path)
    def set_build(self, slot: str, build: dict[str, Any]) -> dict[str, Any]:
        key = "build2" if slot == "build2" else "build1"
        clean = self._normalize_talisman_rows(
            self._normalize_fashion_rows(
                self._normalize_engraving_rows(
                    self._normalize_glyph_s_rows(copy.deepcopy(build))
                )
            )
        )
        with self._state_lock:
            previous = self._builds[key]
            self._builds[key] = clean
            try:
                self._persist_builds()
            except OSError:
                self._builds[key] = previous
                raise
        return self.get_build(key)
    def reset_build(self, slot: str) -> dict[str, Any]:
        key = "build2" if slot == "build2" else "build1"
        with self._state_lock:
            self._builds[key] = self._default_build(key)
            self._persist_builds()
        return self.get_build(key)
    def sync_builds(self) -> dict[str, Any]:
        with self._state_lock:
            self._builds["build2"] = copy.deepcopy(self._builds["build1"])
            self._persist_builds()
        return self.get_build("build2")
    def _load_skill_build_schemes(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            payload = json.loads(self._skill_schemes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for job, schemes in payload.items():
            if not isinstance(schemes, dict):
                continue
            valid = {
                str(name): copy.deepcopy(value)
                for name, value in schemes.items()
                if str(name).strip() and isinstance(value, dict)
            }
            if valid:
                result[norm(job)] = valid
        return result
    def _persist_skill_build_schemes(
        self,
        schemes: dict[str, dict[str, dict[str, Any]]],
    ) -> None:
        self._skill_schemes_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._skill_schemes_path.with_suffix(
            self._skill_schemes_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(schemes, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._skill_schemes_path)
    def _builtin_skill_build_schemes(self, job: str) -> dict[str, dict[str, Any]]:
        current = norm(job)
        payload = self._skill_defaults().get(current, {})
        if not isinstance(payload, dict):
            return {}
        scheme_name = norm(payload.get("scheme_name"))
        if not scheme_name:
            return {}
        scheme = copy.deepcopy(payload)
        scheme["name"] = scheme_name
        scheme["second_job"] = current
        scheme["builtin"] = True
        return {scheme_name: scheme}
    def skill_build_schemes(self, job: str) -> dict[str, Any]:
        current = norm(job)
        with self._state_lock:
            schemes = self._load_skill_build_schemes().get(current, {})
            schemes = {
                **schemes,
                **self._builtin_skill_build_schemes(current),
            }
            rows = [copy.deepcopy(value) for value in schemes.values()]
        rows.sort(key=lambda value: (
            0 if value.get("builtin") else 1,
            str(value.get("name", "")),
            str(value.get("saved_at", "")),
        ))
        return {"job": current, "schemes": rows}
    def save_skill_build_scheme(
        self,
        job: str,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current = norm(job)
        scheme_name = norm(name)[:80]
        if not current or not scheme_name:
            raise ValueError("技能方案名称不能为空")
        if scheme_name in self._builtin_skill_build_schemes(current):
            raise ValueError("内置技能方案不可覆盖，请使用其他名称")
        scheme = {
            "name": scheme_name,
            "second_job": current,
            "saved_at": int(time.time()),
        }
        for key in (
            "skill_build",
            "skill_mastery_build",
            "skill_glyphs",
            "skill_multiplier_overrides",
        ):
            value = payload.get(key)
            if isinstance(value, dict):
                scheme[key] = copy.deepcopy(value)
        with self._state_lock:
            schemes = self._load_skill_build_schemes()
            schemes.setdefault(current, {})[scheme_name] = scheme
            self._persist_skill_build_schemes(schemes)
        return self.skill_build_schemes(current)
    def delete_skill_build_scheme(self, job: str, name: str) -> dict[str, Any]:
        current = norm(job)
        scheme_name = norm(name)
        if scheme_name in self._builtin_skill_build_schemes(current):
            raise ValueError("内置技能方案不可删除")
        with self._state_lock:
            schemes = self._load_skill_build_schemes()
            job_schemes = schemes.get(current, {})
            job_schemes.pop(scheme_name, None)
            if job_schemes:
                schemes[current] = job_schemes
            else:
                schemes.pop(current, None)
            self._persist_skill_build_schemes(schemes)
        return self.skill_build_schemes(current)
    def build_catalog(self, first_job: str, second_job: str) -> dict[str, Any]:
        first = norm(first_job)
        second = norm(second_job)
        equipment = {
            slot: self.store.equipment_names(first, second, slot)
            for slot in EQUIPMENT_SLOTS
        }
        jewelry = {
            slot: self.store.jewelry_names(slot)
            for slot in JEWELRY_SLOTS
        }
        return {
            "equipment": equipment,
            "jewelry": jewelry,
            "enchants": {
                "attack": self.store.enchant_names(True),
                "defense": self.store.enchant_names(False),
                "all": self.store.enchant_names(None),
            },
            "glyphA": {
                "levels": self.store.glyph_a_levels(),
                "names": self.store.glyph_a_names(),
                "extras": self.store.glyph_a_extra(),
            },
            "glyphS": {
                "levels": self.store.glyph_s_levels(),
                "names": self.store.glyph_s_names(),
                "extras": self.store.glyph_s_extra_options(),
                "ranks": self.store.glyph_s_ranks(),
                "baseOptions": {
                    name: self.store.glyph_s_base_options(name)
                    for name in self.store.glyph_s_names()
                },
                "extraOptions": {
                    name: self.store.glyph_s_extra_options(name)
                    for name in self.store.glyph_s_names()
                },
            },
            "runes": {
                "levels": self.store.rune_levels(),
                "attackAttrs": self.store.rune_attrs("攻击"),
                "defenseAttrs": self.store.rune_attrs("防御"),
                "ranks": self.store.rune_ranks(),
                "coreLevels": self.store.core_levels(),
                "conversions": self.store.core_conversions(),
                "coreRatioRanks": {
                    conversion: self.store.core_ratio_ranks(conversion)
                    for conversion in self.store.core_conversions()
                },
                "coreCoeffRanks": {
                    conversion: self.store.core_coeff_ranks(conversion)
                    for conversion in self.store.core_conversions()
                },
            },
            "cards": self.store.card_names(),
            "cardLevels": self.store.card_levels(),
            "talismans": {
                "slots": [
                    {"name": slot, "percent": TALISMAN_SLOT_PERCENT[slot]}
                    for slot in TALISMAN_SLOTS
                ],
                "grades": self.store.talisman_grades(),
                "names": {
                    grade: self.store.talisman_names(grade)
                    for grade in self.store.talisman_grades()
                },
                "extras": {
                    f"{grade}|{name}": self.store.talisman_extra_options(grade, name)
                    for grade in self.store.talisman_grades()
                    for name in self.store.talisman_names(grade)
                },
            },
            "titles": self.store.titles(),
            "fashion": {slot: self.store.fashion_options(slot) for slot in FASHION_SLOTS},
            "engraving": {
                "slots": list(ENGRAVING_SLOTS),
                "grades": self.store.engraving_grades(),
                "extras": self.store.engraving_extra_options(),
                "tiers": self.store.engraving_extra_tiers(),
                "mainValues": {
                    f"{kind}|{grade}": self.store.engraving_main_value_options(kind, grade)
                    for kind in ("防具双防", "武器双攻", "饰品生命", "三件套致命")
                    for grade in self.store.engraving_grades()
                },
                "secondaryValues": {
                    f"{kind}|{grade}": self.store.engraving_secondary_value_options(kind, grade)
                    for kind in ("防具双防", "武器双攻", "饰品生命", "三件套致命")
                    for grade in self.store.engraving_grades()
                },
                "extraRanks": {
                    attr: self.store.engraving_extra_rank_options(attr)
                    for attr in self.store.engraving_extra_options()
                },
            },
            "buffs": self._build_catalog_buff_names(first, second),
            "bosses": {"无": {}} | copy.deepcopy(BOSS_PRESETS),
            "autoEquipTargets": {
                level: [label for label, _kind, _tier in rows]
                for level, rows in AUTO_EQUIP_TARGETS.items()
            },
            "limits": {
                "critResist": [0, 999999],
                "elementResist": [-100, 100],
                "monsterDefense": [0, 999999999],
                "collectionLevel": [0, 42],
                "masteryLevel": [0, 10],
                "enhance": [0, 25],
                "cardBonusLevel": [0, 25],
            },
        }
    def _build_catalog_buff_names(self, first_job: str, second_job: str) -> list[str]:
        names = self.store.skill_names(first_job, second_job)
        if norm(second_job) != "银色猎人":
            return names
        return [
            name
            for name in names
            if "｜debuff｜" not in name and not name.endswith("｜十字弓链")
        ]
    def item_options(self, kind: str, payload: dict[str, Any]) -> list[str]:
        build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
        first = norm(payload.get("first_job") or build.get("first_job"))
        second = norm(payload.get("second_job") or build.get("second_job"))
        if kind == "suffix":
            return self.store.suffix_names(first, second, norm(payload.get("name")))
        if kind == "jewelry-variant":
            return self.store.jewelry_variants(norm(payload.get("slot")), norm(payload.get("name")))
        if kind == "buff-level":
            return self.store.skill_levels(norm(payload.get("skill")))
        if kind == "glyph-s-base":
            return self.store.glyph_s_base_options(norm(payload.get("name")))
        if kind == "glyph-s-extra":
            return self.store.glyph_s_extra_options(norm(payload.get("name")))
        if kind == "core-ratio":
            return self.store.core_ratio_ranks(norm(payload.get("conversion")))
        if kind == "core-coeff":
            return self.store.core_coeff_ranks(norm(payload.get("conversion")))
        if kind == "engraving-main":
            return self.store.engraving_main_value_options(
                norm(payload.get("main_kind")), norm(payload.get("grade"))
            )
        if kind == "engraving-extra-rank":
            return self.store.engraving_extra_rank_options(norm(payload.get("extra")))
        if kind == "skill-damage-levels":
            return self.store.skill_damage_levels(second, norm(payload.get("skill")))
        return []
    @staticmethod
    def _merge_attrs(target: dict[str, float], attrs: dict[str, Any]) -> None:
        for attr, value in attrs.items():
            target[norm(attr)] = target.get(norm(attr), 0.0) + as_number(value)
    @staticmethod
    def _attrs_text(attrs: dict[str, Any]) -> str:
        parts: list[str] = []
        for attr, raw_value in attrs.items():
            value = as_number(raw_value)
            if abs(value) <= 1e-9:
                continue
            text = str(int(value)) if abs(value - int(value)) <= 1e-9 else f"{value:.3f}".rstrip("0").rstrip(".")
            suffix = "%" if norm(attr).endswith("%") else ""
            parts.append(f"{norm(attr)} {text}{suffix}")
        return "，".join(parts) or "无加成"
    def score_build_preview(self, build: dict[str, Any]) -> dict[str, Any]:
        """Lightweight, shared passive/resonance preview; no score computation."""
        from ..equipment_resonance import resonance_preview
        resonance = resonance_preview(self.store, build)
        displayed = {key: value if key.endswith('%') else int(value)
                     for key, value in resonance['attrs'].items()}
        return {
            'resonance': {**resonance, 'text': self._attrs_text(displayed)},
            'buffs': [
                {'index': index, 'text': self._attrs_text(self.calculator.buff_attrs(norm(item.get('skill')), item.get('level')))}
                for index, item in enumerate(build.get('buffs', [])) if isinstance(item, dict)
            ],
        }

    def build_details(self, build: dict[str, Any]) -> dict[str, Any]:
        score_preview = self.score_build_preview(build)
        first = norm(build.get("first_job"))
        second = norm(build.get("second_job"))
        equipment = [item for item in build.get("equipment", []) if isinstance(item, dict)]
        jewelry = [item for item in build.get("jewelry", []) if isinstance(item, dict)]
        glyph_a = [item for item in build.get("glyph_a", []) if isinstance(item, dict)]
        glyph_s = [item for item in build.get("glyph_s", []) if isinstance(item, dict)]
        runes = [item for item in build.get("normal_runes", []) if isinstance(item, dict)]
        talismans = [item for item in build.get("talismans", []) if isinstance(item, dict)]
        engravings = [item for item in build.get("engravings", []) if isinstance(item, dict)]
        card_totals = {attr: 0.0 for attr in CARD_PREVIEW_ATTRS}
        card_rows: list[dict[str, Any]] = []
        for item in build.get("card_grades", []):
            if not isinstance(item, dict):
                continue
            attrs = self.calculator.card_attrs(norm(item.get("name")), norm(item.get("grade")))
            self._merge_attrs(card_totals, attrs)
            card_rows.append({"name": norm(item.get("name")), "attrs": attrs, "text": self._attrs_text(attrs)})
        bonus_levels = build.get("card_bonus_levels", {}) if isinstance(build.get("card_bonus_levels"), dict) else {}
        card_boosted = {
            attr: value * (
                1
                if attr == "致命伤害"
                else 1 + max(0.0, as_number(bonus_levels.get(attr))) * 0.05
            )
            for attr, value in card_totals.items()
        }
        talisman_totals: dict[str, float] = {}
        talisman_rows: list[dict[str, Any]] = []
        for item in talismans:
            attrs = self.calculator.talisman_attrs(item)
            self._merge_attrs(talisman_totals, attrs)
            talisman_rows.append({
                "slot": norm(item.get("slot")),
                "percent": TALISMAN_SLOT_PERCENT.get(norm(item.get("slot")), 0),
                "attrs": attrs,
                "text": self._attrs_text(attrs),
            })
        core_preview = self.calculator.core_rune_preview(build)
        return {
            "equipment": [
                {"slot": norm(item.get("slot")), "attrs": (attrs := self.calculator.equipment_attrs(item, first, second)), "text": self._attrs_text(attrs)}
                for item in equipment
            ],
            "equipmentSet": self._attrs_text(self.calculator.equipment_set_bonus_attrs(equipment, first, second)),
            "resonance": score_preview['resonance'],
            "jewelry": [
                {"slot": norm(item.get("slot")), "attrs": (attrs := self.calculator.jewelry_attrs(item)), "text": self._attrs_text(attrs)}
                for item in jewelry
            ],
            "jewelrySet": self._attrs_text(self.calculator.jewelry_set_bonus_attrs(jewelry)),
            "glyphA": [
                {"index": index, "attrs": (attrs := self.calculator.glyph_a_attrs(item)), "text": self._attrs_text(attrs)}
                for index, item in enumerate(glyph_a)
            ],
            "glyphS": [
                {"index": index, "attrs": (attrs := self.calculator.glyph_s_attrs(item)), "text": self._attrs_text(attrs)}
                for index, item in enumerate(glyph_s)
            ],
            "runes": [
                {"index": index, "attrs": (attrs := self.calculator.rune_attrs_for_item(item, build.get("core_rune", {}).get("enhance", 0))), "text": self._attrs_text(attrs)}
                for index, item in enumerate(runes)
            ],
            "coreRune": core_preview,
            "coreRuneText": (
                f"扣除{core_preview.get('source_attr')} {self._attrs_text({norm(core_preview.get('source_attr')): core_preview.get('deduct', 0)})}；"
                f"获得{self._attrs_text({norm(core_preview.get('target_attr')): core_preview.get('gain', 0)})}；"
                f"普通石板强化 +{as_number(core_preview.get('normal_boost')):g}%"
                if core_preview else "无加成"
            ),
            "cards": card_rows,
            "cardTotals": card_totals,
            "cardBoosted": card_boosted,
            "talismans": talisman_rows,
            "talismanTotals": talisman_totals,
            "collection": self._attrs_text(self.calculator.collection_attrs(build.get("collection_level"))),
            "title": self._attrs_text(self.calculator.title_attrs(norm(build.get("title")))),
            "mastery": [
                {"name": norm(item.get("name")), "text": self._attrs_text(self.calculator.mastery_attrs(norm(item.get("name")), item.get("level")))}
                for item in build.get("mastery", []) if isinstance(item, dict)
            ],
            "fashion": [
                {"slot": norm(item.get("slot")), "text": self._attrs_text(self.calculator.fashion_attrs(norm(item.get("name"))))}
                for item in build.get("fashion", []) if isinstance(item, dict)
            ],
            "buffs": score_preview['buffs'],
            "engravings": [
                {
                    "slot": norm(item.get("slot")),
                    "attrs": (attrs := self.calculator.engraving_attrs(item)),
                    "text": self._attrs_text(attrs),
                }
                for item in engravings
            ],
        }
    @staticmethod
    def _first_existing(options: list[str], choices: list[str], default: str = NONE_TEXT) -> str:
        option_set = set(options)
        for choice in choices:
            if choice in option_set:
                return choice
        return default if default in option_set else (options[0] if options else default)
    @staticmethod
    def _choose_by_keywords(options: list[str], groups: list[tuple[str, ...]]) -> str:
        candidates = [option for option in options if option and option != NONE_TEXT]
        for group in groups:
            for option in candidates:
                if all(keyword in option for keyword in group):
                    return option
        return candidates[0] if candidates else NONE_TEXT
    @staticmethod
    def _highest_number_option(options: list[str], default: str = NONE_TEXT) -> str:
        ranked = []
        for option in options:
            numbers = re.findall(r"\d+(?:\.\d+)?", norm(option))
            ranked.append((float(numbers[-1]) if numbers else -1.0, norm(option)))
        return max(ranked, default=(-1.0, default))[1]
    def _job_profile(self, build: dict[str, Any]) -> dict[str, Any]:
        first = norm(build.get("first_job"))
        second = norm(build.get("second_job"))
        family = self.store.job_family(second, first)
        effective = next((row for row in self.store.tables["effective"] if norm(row.get("职业")) == second), {})
        raw_attack = norm(effective.get("攻击1")) or "物攻"
        attacks = ["物攻", "魔攻"] if raw_attack == "双攻" else [raw_attack if raw_attack in {"物攻", "魔攻"} else "物攻"]
        attack = attacks[0]
        main_stat = "智力" if attack == "魔攻" else "力量"
        best_ratio = -1.0
        for row in self.store.tables["conversion"]:
            if norm(row.get("一转职业")) != family or norm(row.get("目标属性")) != attack:
                continue
            source = norm(row.get("来源属性"))
            ratio = as_number(row.get("转换比例"))
            if source in {"力量", "敏捷", "智力", "体质"} and ratio > best_ratio:
                main_stat, best_ratio = source, ratio
        element = norm(effective.get("属性1"))
        element_attrs = [
            f"{value}攻"
            for value in (element, norm(effective.get("属性2")))
            if value and value != NONE_TEXT
        ]
        mixed_elements = {
            "冰灵": ["水攻", "火攻"],
            "烈": ["暗攻", "火攻"],
            "暗": ["暗攻", "光攻"],
            "曜": ["光攻", "暗攻"],
        }
        if second in mixed_elements:
            element_attrs = mixed_elements[second]
        if second in {"圣骑士", "十字军"}:
            attacks = ["物攻", "魔攻"]
        return {
            "first_job": first, "second_job": second, "family": family,
            "attack_attr": attack, "attack_attrs": attacks, "main_stat": main_stat,
            "element_attr": f"{element}攻%" if element and element != NONE_TEXT else "",
            "element_attrs": element_attrs,
        }
    def _equipment_choice_for_tier(self, tier: str, job: str, slot: str) -> str:
        return self.store.preferred_equipment_name(tier, job, slot)
    def _jewelry_choice_for_tier(self, tier: str, slot: str) -> str:
        for row in self.store.tables["jewelry"]:
            if norm(row.get("等级")) == tier and norm(row.get("饰品类别")) == slot:
                return norm(row.get("饰品名称")) or NONE_TEXT
        return NONE_TEXT
    def _suffix_choice(self, options: list[str], slot: str, profile: dict[str, Any]) -> str:
        attack = "魔力II" if profile["attack_attr"] == "魔攻" else "破坏II"
        stat = {"力量": "巨熊II", "敏捷": "疾风II", "智力": "智慧II", "体质": "生命II"}.get(profile["main_stat"], "生命II")
        return self._first_existing(options, [attack, stat, "致命II", "勇者II"] if slot in {"主手", "副手"} else [stat, "生命II", "致命II", attack])
    @staticmethod
    def _best_enchant(options: list[str], attr: str, value_text: str = "") -> str:
        candidates = [option for option in options if option.startswith(attr) and (not value_text or value_text in option)]
        if candidates:
            return candidates[0]
        candidates = [option for option in options if option.startswith(attr)]
        return candidates[0] if candidates else NONE_TEXT
    def _enchant_plan(self, options: list[str], profile: dict[str, Any], weapon: bool = False) -> list[str]:
        stats = ["力量", "智力"] if profile["first_job"] == "贤者" else (["敏捷", "智力"] if profile["attack_attr"] == "魔攻" else ["力量", "敏捷"])
        attrs: list[tuple[str, str]] = []
        if weapon:
            element = profile["element_attr"].replace("%", "")
            if profile["first_job"] == "光明之怒" or profile["second_job"] == "烈":
                elements = ["光攻", "暗攻"] if profile["first_job"] == "光明之怒" else ["暗攻", "火攻"]
                attrs = [("物攻", "2.88"), *[(value, "8") for value in elements], ("力量", "4.5"), ("敏捷", "4.5")]
            elif profile["first_job"] == "贤者":
                attrs = [("物攻", "2.88"), ("魔攻", "2.88")]
                if element:
                    attrs.append((element, "8"))
                attrs.extend((value, "") for value in stats)
            else:
                attrs = [(profile["attack_attr"], "2.88")]
                if element:
                    attrs.append((element, "8"))
                attrs.extend((value, "") for value in stats)
        else:
            attrs = [(value, "") for value in stats] + [("HP", "")]
        out: list[str] = []
        for attr, value in attrs:
            choice = self._best_enchant(options, attr, value)
            if choice != NONE_TEXT and choice not in out:
                out.append(choice)
        return (out + [NONE_TEXT] * 5)[:5]
    @staticmethod
    def _glyph_a_forbidden_extra(name: str) -> str:
        for keyword, attr in {"魔力": "魔攻", "破坏": "物攻", "巨熊": "力量", "疾风": "敏捷", "智慧": "智力", "健康": "体质", "生命": "HP", "法力": "MP", "铁壁": "防御", "帷幕": "魔防", "致命": "致命", "最终": "最终"}.items():
            if keyword in norm(name):
                return attr
        return ""
    def _standard_glyphs(
        self,
        profile: dict[str, Any],
        glyph_a_extra: str = "自动",
        glyph_s_extra: str = NONE_TEXT,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        requested_a_extra = norm(glyph_a_extra)
        requested_s_extra = norm(glyph_s_extra)
        if not requested_a_extra or requested_a_extra == "自动":
            requested_a_extra = profile["attack_attr"]
        automatic_s = requested_s_extra == "自动"
        attack_name = "魔力" if profile["attack_attr"] == "魔攻" else "破坏"
        stat_names = ["疾风", "巨熊", "智慧"] if profile["first_job"] == "贤者" else (["疾风", "智慧"] if profile["attack_attr"] == "魔攻" else ["疾风", "巨熊"])
        wanted = unique(["最终", attack_name, *stat_names, "致命", "生命", "健康", "铁壁", "帷幕", "不屈", "极限", "冲击"])
        level = self._first_existing(self.store.glyph_a_levels(), ["70A", "60A", "50A", "40A"])
        names = self.store.glyph_a_names()
        used: set[str] = set()
        glyph_a = []
        for keyword in wanted[:11]:
            name = self._choose_by_keywords([value for value in names if value not in used], [(keyword,)])
            used.add(name)
            forbidden = self._glyph_a_forbidden_extra(name)
            extras = [value for value in self.store.glyph_a_extra() if not forbidden or value != forbidden]
            glyph_a.append({"level": level, "name": name, "extra": self._first_existing(extras, [requested_a_extra])})
        s_level = self._first_existing(self.store.glyph_s_levels(), ["70S", "60S"])
        s_plan = (
            ("攻击", ((profile["attack_attr"], "8"), ("魔攻" if profile["attack_attr"] == "物攻" else "物攻", "3"))),
            ("上升", ((profile["main_stat"], "9"), ("体质" if profile["main_stat"] != "体质" else "敏捷", "3"))),
            ("根源", (("HP", "8"), ("MP", "3"))),
            ("抵抗", (("致命抵抗", "3"), ("硬直抵抗", "3"))),
        )
        glyph_s = []
        for key, preferred in s_plan:
            name = self._choose_by_keywords(self.store.glyph_s_names(), [(key,)])
            options = [
                value for value in self.store.glyph_s_base_options(name)
                if value != NONE_TEXT
            ][:4]
            ranks = {attr: "1" for attr in options}
            for wanted_attr, wanted_rank in preferred:
                selected = self._first_existing(options, [wanted_attr])
                if selected != NONE_TEXT:
                    ranks[selected] = wanted_rank
            extra_options = self.store.glyph_s_extra_options(name)
            if automatic_s:
                from ..live.score_equipment_defaults import automatic_s_extra
                extra = automatic_s_extra(self.store, name, profile)
            else:
                extra = self._first_existing(extra_options, [requested_s_extra])
            row: dict[str, Any] = {"level": s_level, "name": name, "extra": extra}
            for index in range(1, 5):
                attr = options[index - 1] if index <= len(options) else NONE_TEXT
                row[f"base_attr{index}"] = attr
                row[f"rank{index}"] = ranks.get(attr) if attr != NONE_TEXT else None
            glyph_s.append(row)
        return glyph_a, glyph_s
    def _standard_runes(self, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        level = self._first_existing(self.store.rune_levels(), ["70", "60", "50"])
        rank = self._highest_number_option(self.store.rune_ranks())
        plans = ([profile["attack_attr"], profile["attack_attr"], profile["main_stat"], "最终", "最终"], [profile["attack_attr"], profile["attack_attr"], "致命伤害", "最终", "最终"], ["HP", "HP", "体质", "防御", "魔防"], ["HP", "HP", "体质", "防御", "魔防"])
        normal = []
        for index, plan in enumerate(plans):
            rune_type = "攻击" if index < 2 else "防御"
            options = self.store.rune_attrs(rune_type)
            entries = []
            for attr in [*plan, NONE_TEXT]:
                selected = self._first_existing(options, [attr])
                entries.append({"attr": selected, "rank": rank if selected != NONE_TEXT else NONE_TEXT})
            normal.append({"type": rune_type, "level": level, "entries": entries})
        choices = ["魔防→魔攻"] if profile["second_job"] == "十字军" or profile["attack_attr"] == "魔攻" else ["致命→物攻"]
        conversion = self._first_existing([value for value in self.store.core_conversions() if value != NONE_TEXT], choices)
        ratios = self.store.core_ratio_ranks(conversion)
        coeffs = self.store.core_coeff_ranks(conversion)
        core = {"level": self._first_existing(self.store.core_levels(), ["70", "60"]), "conversion": conversion, "ratio_rank": self._first_existing(ratios, ["20"], self._highest_number_option(ratios)), "coeff_rank": self._highest_number_option(coeffs), "enhance": 10}
        return normal, core
    def _score_attrs_for_profile(self, attrs: dict[str, Any], profile: dict[str, Any]) -> float:
        score = 0.0
        for attr, raw in attrs.items():
            value = as_number(raw)
            if attr in {profile["attack_attr"], f"{profile['attack_attr']}%"}:
                score += value * (120 if attr.endswith("%") else 1)
            elif attr in {"最小物攻", "最大物攻"} and profile["attack_attr"] == "物攻": score += value * .5
            elif attr in {"最小魔攻", "最大魔攻"} and profile["attack_attr"] == "魔攻": score += value * .5
            elif attr == profile["main_stat"]: score += value * .8
            elif attr == f"{profile['main_stat']}%": score += value * 80
            elif attr == "最终": score += value * 5
            elif attr in {"致命", "致命伤害"}: score += value * .2
            elif attr in {"致命%", "致命伤害%"}: score += value * 80
            elif profile["element_attr"] and attr == profile["element_attr"]: score += value * 100
        return score
    def _auto_buffs(self, build: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
        source = "打坐曜" if profile["second_job"] == "曜" else profile["second_job"]
        job_sources = set(
            self.store.job_buff_sources(source, norm(build.get("first_job")))
        )
        candidate_rows: list[dict[str, Any]] = []
        for row in self.store.tables["buff"]:
            row_source = norm(row.get("来源"))
            category = norm(row.get("类别"))
            if category.casefold() == "debuff":
                continue
            if not (
                (row_source == "通用" and norm(row.get("技能")) == "力量掌握")
                or row_source in job_sources
            ):
                continue
            if category == "个人buff":
                rule = self.store.buff_activation_rule(
                    row_source, row.get("技能"), category
                )
                if rule and norm(rule.get("mode")) != "permanent":
                    continue
            candidate_rows.append(row)

        # Prefer the specialization's declared member of a mutually exclusive
        # group before the normal first-row-wins de-duplication below.
        candidate_rows.sort(
            key=lambda row: int(
                source
                not in {
                    norm(value)
                    for value in self.store.buff_activation_rule(
                        row.get("来源"), row.get("技能"), row.get("类别")
                    ).get("preferred_jobs", [])
                    if norm(value)
                }
            )
        )
        labels: list[str] = []
        seen_groups: set[str] = set()
        for row in candidate_rows:
            group = buff_exclusive_group(row)
            if group and group in seen_groups:
                continue
            if group:
                seen_groups.add(group)
            label = buff_display_label(row)
            if label not in labels:
                labels.append(label)
        result = []
        for label in labels[:10]:
            levels = [value for value in self.store.skill_levels(label) if value != NONE_TEXT]
            result.append({"skill": label, "level": self._highest_number_option(levels, NONE_TEXT)})
        return result
    def _profile_passive_buffs(
        self, job: str, context_build: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return the passive subset of the build page's one-click Buff preset."""

        selected_job = norm(job)
        if not selected_job:
            return []
        first_job = next(
            (
                first
                for first in self.store.first_jobs()
                if selected_job in self.store.second_jobs(first)
            ),
            "",
        )
        if not first_job:
            return []
        build = copy.deepcopy(context_build)
        build["first_job"] = first_job
        build["second_job"] = selected_job
        preset = self._auto_buffs(build, self._job_profile(build))
        result: list[dict[str, Any]] = []
        for item in preset:
            if not isinstance(item, dict):
                continue
            label = norm(item.get("skill"))
            passive_rows = [
                row
                for row in self.store.tables.get("buff", [])
                if buff_display_label(row) == label
                and norm(row.get("类别")) == "被动"
            ]
            if not passive_rows:
                continue
            level = norm(item.get("level")) or NONE_TEXT
            result.append({
                "skill": norm(passive_rows[0].get("技能")) or label,
                "level": level,
                "effect": self._attrs_text(
                    self.calculator.buff_attrs(label, level)
                ),
            })
        return result
    def apply_build_preset(self, build: dict[str, Any], action: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        proposed = copy.deepcopy(build)
        options = options or {}
        profile = self._job_profile(proposed)
        if action == "boss":
            preset = BOSS_PRESETS.get(norm(options.get("boss")), {})
            proposed["boss"] = norm(options.get("boss")) or NONE_TEXT
            if preset:
                proposed["crit_resist_value"] = preset["crit_resist"]
                proposed["element_resists"] = {element: preset["element_resist"] for element in ("火", "水", "光", "暗")}
                proposed["monster_defense"] = preset["monster_defense"]
                proposed["freeze_coefficient"] = preset["freeze_coefficient"]
        elif action == "equipment":
            level = norm(options.get("level")) or "70"
            target = norm(options.get("target"))
            kind, tier = next(((kind, tier) for label, kind, tier in AUTO_EQUIP_TARGETS.get(level, ()) if label == target), ("", ""))
            if not kind:
                raise ValueError("请选择有效的一键配装目标")
            if kind in {"all", "equipment"}:
                equipment_job = profile["family"] if tier == "70S" else profile["first_job"]
                rows = []
                for slot in EQUIPMENT_SLOTS:
                    names = self.store.equipment_names(profile["first_job"], profile["second_job"], slot)
                    name = self._equipment_choice_for_tier(tier, equipment_job, slot)
                    if name == NONE_TEXT:
                        name = self._choose_by_keywords(names, [("黑龙" if tier.startswith("70") else "赞龙" if tier.startswith("60") else "霸龙",)])
                    suffix = self._suffix_choice(self.store.suffix_names(profile["first_job"], profile["second_job"], name), slot, profile)
                    enchants = self._enchant_plan(self.store.enchant_names(slot in {"主手", "副手"}), profile, slot in {"主手", "副手"})
                    rows.append({"slot": slot, "name": name, "suffix": suffix, "enhance": 13 if slot in {"主手", "副手"} else 10, "enchants": enchants})
                proposed["equipment"] = rows
            if kind in {"all", "jewelry"}:
                variant = {"力量": "力敏", "敏捷": "力敏", "智力": "敏智", "体质": "智体"}.get(profile["main_stat"], "力敏")
                rows = []
                for slot in ("项链", "耳环", "戒指1", "戒指2"):
                    lookup_slot = "戒指" if slot.startswith("戒指") else slot
                    name = self._jewelry_choice_for_tier(tier, lookup_slot)
                    variants = self.store.jewelry_variants(lookup_slot, name)
                    enchants = self._enchant_plan(self.store.enchant_names(lookup_slot == "戒指"), profile, lookup_slot == "戒指")
                    rows.append({"slot": slot, "name": name, "variant": self._first_existing(variants, [variant, "力敏", "敏智", "智体"]), "enchants": enchants})
                proposed["jewelry"] = rows
        elif action == "glyphs":
            proposed["glyph_a"], proposed["glyph_s"] = self._standard_glyphs(
                profile,
                norm(options.get("aExtra")) or "自动",
                norm(options.get("sExtra")) or NONE_TEXT,
            )
        elif action == "runes":
            proposed["normal_runes"], proposed["core_rune"] = self._standard_runes(profile)
        elif action == "talismans":
            from ..talisman_optimizer import optimize_talismans

            proposed["talismans"], _ = optimize_talismans(self.calculator, proposed, profile)
        elif action == "talisman-extras":
            requested = norm(options.get("extra")) or "自动"
            extra_rows = [
                row for row in self.store.tables.get("talisman", [])
                if norm(row.get("类型")) == "额外属性"
            ]
            requested_attr = profile["attack_attr"] if requested == "自动" else next((
                norm(row.get("属性")) for row in extra_rows
                if requested in {norm(row.get("额外属性")), norm(row.get("属性"))}
            ), "")
            if requested not in {"自动", NONE_TEXT} and not requested_attr:
                raise ValueError("请选择有效的护符额外属性")
            for item in proposed.get("talismans", []):
                if not isinstance(item, dict):
                    continue
                allowed = self.store.talisman_extra_options(item.get("grade"), item.get("name"))
                # Options are filtered by actual attributes, not display names:
                # e.g. 魔法攻击力 is the same attribute as 魔攻 on 魔力护符.
                item["extra"] = next((
                    norm(row.get("额外属性")) for row in extra_rows
                    if requested != NONE_TEXT
                    and norm(row.get("属性")) == requested_attr
                    and norm(row.get("品质")) == norm(item.get("grade"))
                    and norm(row.get("额外属性")) in allowed
                ), NONE_TEXT)
        elif action == "cards":
            grade = norm(options.get("grade")) or "L"
            requested_names = {
                norm(name) for name in options.get("names", []) if norm(name)
            } if isinstance(options.get("names"), list) else set()
            requested_level = int(as_number(options.get("level")))
            filtered = bool(requested_names or requested_level)
            current = {
                norm(item.get("name")): norm(item.get("grade")) or NONE_TEXT
                for item in proposed.get("card_grades", [])
                if isinstance(item, dict) and norm(item.get("name"))
            }
            proposed["card_grades"] = [
                {
                    "name": name,
                    "grade": grade if (
                        (not filtered)
                        or name in requested_names
                        or (requested_level and self.store.card_level(name) == requested_level)
                    ) else current.get(name, NONE_TEXT),
                }
                for name in self.store.card_names()
            ]
            if not filtered:
                proposed["card_bonus_levels"] = {attr: 0 if attr == "致命伤害" else 25 for attr in CARD_PREVIEW_ATTRS}
        elif action == "buffs":
            proposed["buffs"] = self._auto_buffs(proposed, profile)
        elif action == "clear-buffs":
            proposed["buffs"] = []
        elif action == "engraving":
            target = norm(options.get("target")) or "全部"
            grade = norm(options.get("grade")) or NONE_TEXT
            extra = norm(options.get("extra")) or NONE_TEXT
            tier = norm(options.get("tier")) or "S低档"
            groups = {"防具": set(EQUIPMENT_SLOTS[:5]), "武器": {"主手", "副手"}, "饰品": {"项链", "耳环", "戒指1", "戒指2"}, "三件套": {"翅膀", "尾巴", "印花"}}
            selected = set(ENGRAVING_SLOTS) if target == "全部" else groups.get(target, set())
            rows = []
            for item in proposed.get("engravings", []):
                row = copy.deepcopy(item)
                if norm(row.get("slot")) in selected:
                    row["grade"] = grade
                    kind = "防具双防" if row["slot"] in EQUIPMENT_SLOTS[:5] else "武器双攻" if row["slot"] in {"主手", "副手"} else "饰品生命" if row["slot"] in {"项链", "耳环", "戒指1", "戒指2"} else "三件套致命"
                    value_rank = self._highest_number_option(self.store.engraving_main_value_options(kind, grade))
                    row["value_rank"] = value_rank
                    row["main_value"] = as_number(re.findall(r"\d+(?:\.\d+)?", value_rank)[-1] if re.findall(r"\d+(?:\.\d+)?", value_rank) else 0)
                    main_options = self.store.engraving_main_value_options(kind, grade)
                    secondary_options = self.store.engraving_secondary_value_options(kind, grade)
                    value_index = main_options.index(value_rank) if value_rank in main_options else 0
                    row["secondary_value"] = (
                        self._option_number(secondary_options[value_index])
                        if value_index < len(secondary_options)
                        else 0.0
                    )
                    row["extra"] = extra
                    row["extra_rank"] = tier if extra != NONE_TEXT else NONE_TEXT
                    row["extra_tier"] = (
                        "高档" if "高档" in tier else "低档"
                        if "低档" in tier else NONE_TEXT
                    )
                    row["extra_value"] = next((
                        self._option_number(option)
                        for option in self.store.engraving_extra_rank_options(extra)
                        if norm(option).startswith(tier)
                    ), 0.0)
                rows.append(row)
            proposed["engravings"] = rows
        elif action == "misc":
            proposed["collection_level"] = 33
            proposed["mastery"] = [{"name": name, "level": 10} for name in MASTERY_NAMES]
            proposed["fashion"] = []
            for slot in FASHION_SLOTS:
                options = self.store.fashion_options(
                    "戒指" if slot.startswith("戒指") else slot
                )
                if slot in {"主手", "副手", "翅膀", "尾巴", "印花"}:
                    choice = self._choose_by_keywords(options, [("70",)])
                elif slot == "精灵":
                    choice = max(
                        options,
                        key=lambda option: self._score_attrs_for_profile(
                            self.calculator.fashion_attrs(option), profile
                        ),
                        default=NONE_TEXT,
                    )
                else:
                    choice = self._choose_by_keywords(options, [("A",), ("阿尔泰亚",)])
                proposed["fashion"].append({"slot": slot, "name": choice})
            titles = self.store.titles()
            proposed["title"] = max(titles, key=lambda title: self._score_attrs_for_profile(self.calculator.title_attrs(title), profile), default=NONE_TEXT)
            proposed["buffs"] = self._auto_buffs(proposed, profile)
        else:
            raise ValueError(f"未知的一键操作：{action}")
        return proposed
    def skill_build(self, job: str) -> dict[str, Any]:
        current = norm(job)
        defaults = self._skill_defaults_for_job(current)
        return {
            "job": current,
            "tree": self.store.skill_build_tree(current, 70),
            "mastery": self.store.skill_build_tree(current, 70, mode="master"),
            "glyphs": self.store.skill_glyphs(current),
            "defaults": defaults,
        }
    @staticmethod
    def _selected_skill_glyph_values(build: dict[str, Any]) -> dict[str, dict[str, float]]:
        payload = build.get("skill_glyphs", {})
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        result: dict[str, dict[str, float]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            skill = norm(row.get("skill"))
            effect = norm(row.get("effect"))
            if not skill or not effect:
                continue
            result.setdefault(skill, {})[effect] = max(
                result.setdefault(skill, {}).get(effect, 0.0),
                max(0.0, as_number(row.get("value"))),
            )
        return result
    @staticmethod
    def _skill_levels_by_name(store: DataStore, build: dict[str, Any]) -> dict[str, int]:
        job = norm(build.get("second_job"))
        selected = build.get("skill_build", {})
        selected_levels = selected.get("levels", {}) if isinstance(selected, dict) else {}
        result: dict[str, int] = {}
        tree = store.skill_build_tree(job, 70)
        for group in tree.get("groups", []):
            for skill in group.get("skills", []):
                skill_id = norm(skill.get("id"))
                name = norm(skill.get("name"))
                if name and skill_id:
                    result[name] = max(0, int(as_number(selected_levels.get(skill_id))))
        return result
    @staticmethod
    def _skill_level_damages(level_data: list[Any]) -> list[tuple[str, float, float]]:
        """Read every historical skill-damage row shape used by the simulator."""
        if len(level_data) >= 6:
            return [(norm(level_data[3]), as_number(level_data[4]), as_number(level_data[5]))]
        if len(level_data) >= 4 and isinstance(level_data[3], list):
            return [
                (norm(item[0]), as_number(item[1]), as_number(item[2]))
                for item in level_data[3]
                if isinstance(item, (list, tuple)) and len(item) >= 3
            ]
        if len(level_data) >= 5:
            return [(norm(level_data[2]), as_number(level_data[3]), as_number(level_data[4]))]
        if len(level_data) >= 3 and isinstance(level_data[2], list):
            return [
                (norm(item[0]), as_number(item[1]), as_number(item[2]))
                for item in level_data[2]
                if isinstance(item, (list, tuple)) and len(item) >= 3
            ]
        return []
    @staticmethod
    def _skill_level_cooldown(level_data: list[Any]) -> float:
        if len(level_data) >= 6 or (len(level_data) >= 4 and isinstance(level_data[3], list)):
            return max(0.0, as_number(level_data[2]))
        return 0.0
    @staticmethod
    def _damage_panel(damage_type: str, summary: dict[str, Any]) -> float:
        physical = max(0.0, as_number(summary.get("physical_average")))
        magic = max(0.0, as_number(summary.get("magic_average")))
        if damage_type == "P":
            return physical
        if damage_type == "M":
            return magic
        if damage_type == "B":
            return (physical + magic) / 2.0
        return max(physical, magic)
    def skill_rows(self, build: dict[str, Any], include_unlearned: bool = False, *, settle_action_speed: bool = False) -> dict[str, Any]:
        build = copy.deepcopy(build)
        rows, summary = self.calculator.calculate(build)
        job = norm(build.get("second_job"))
        settings = build.get("skill_sim_settings", {})
        settings = settings if isinstance(settings, dict) else {}
        cdr_percent = min(
            70.0,
            max(
                0.0,
                (40.0 if settings.get("burst") else 0.0)
                + (12.0 if settings.get("cocktail") else 0.0)
                + (70.0 if settings.get("accel") else 0.0)
                + as_number(settings.get("custom_cdr")),
            ),
        )
        action_speed_percent = 40.0 if settings.get("burst") else 0.0
        burning_mode = (
            "accel"
            if settings.get("accel")
            else "both"
            if settings.get("burst") and settings.get("cocktail")
            else "burst"
            if settings.get("burst")
            else "cocktail"
            if settings.get("cocktail")
            else "base"
        )
        # The Vue page must use the same skill-table rules as V4.01.  The pure
        # runtime exposes those rules without constructing any Qt widgets.
        runtime = self.legacy_timeline._runtime(build, {"mode": "long"})
        # Keep the calculated panel summary available to the runtime's other
        # skill rules. Damage glyphs themselves are independent per-skill
        # multipliers and are not diluted by the final-damage bucket.
        runtime._skill_burst_base_summary = copy.deepcopy(summary)
        job_profile = runtime._job_profile()
        weighted_panel = runtime._skill_weighted_panel_profile()
        preferred_type = "M" if job_profile.get("attack_attr") == "魔攻" else "P"
        learned = self._skill_levels_by_name(self.store, build)
        requested_levels = build.get("_skill_level_overrides", {})
        requested_levels = requested_levels if isinstance(requested_levels, dict) else {}
        requested_levels = {
            self.store.skill_damage_canonical_name(job, skill): value
            for skill, value in requested_levels.items()
        }
        glyph_values = self._selected_skill_glyph_values(build)
        override_payload = build.get("skill_multiplier_overrides", {})
        override_payload = override_payload if isinstance(override_payload, dict) else {}
        overrides: dict[str, float] = {}
        # V4.01 stores overrides as {second_job, rows:[{skill,value}]}.  Accept
        # the short-lived flat V4.10 preview format as well so an early build
        # can still be imported without losing the user's edits.
        if isinstance(override_payload.get("rows"), list):
            if not norm(override_payload.get("second_job")) or norm(override_payload.get("second_job")) == job:
                overrides = {
                    self.store.skill_damage_canonical_name(job, row.get("skill")): max(0.0, as_number(row.get("value")))
                    for row in override_payload["rows"]
                    if isinstance(row, dict) and norm(row.get("skill"))
                }
        else:
            overrides = {
                self.store.skill_damage_canonical_name(job, skill): max(0.0, as_number(value))
                for skill, value in override_payload.items()
                if skill not in {"second_job", "rows"} and norm(skill)
            }
        result: list[dict[str, Any]] = []
        for skill in self.store.skill_damage_names(job):
            weighted_panel = runtime._skill_weighted_panel_profile(skill)
            if not skill or skill == NONE_TEXT:
                continue
            levels = [value for value in self.store.skill_damage_levels(job, skill) if value != NONE_TEXT]
            if not levels:
                continue
            entry = self.store._skill_damage_entry(job, skill)
            inherited_level = learned.get(entry.get("required_form", ""), 0) if entry else 0
            selected_level = int(as_number(requested_levels.get(skill), learned.get(skill, inherited_level)))
            if selected_level <= 0 and not include_unlearned:
                continue
            level_text = str(selected_level) if str(selected_level) in levels else levels[-1]
            level_data, effective_level, mastery_bonus = runtime._skill_sim_level_data_with_mastery(
                skill, level_text
            )
            if not isinstance(level_data, list) or len(level_data) < 4:
                continue
            base_cooldown = runtime._skill_adjusted_cooldown(skill, level_data)
            fixed_cooldown = runtime._skill_fixed_cooldown(skill)
            special_cooldown = runtime._skill_special_effective_cooldown(skill, cdr_percent)
            cooldown = (
                fixed_cooldown
                if fixed_cooldown is not None
                else special_cooldown
                if special_cooldown is not None
                else runtime._skill_effective_cooldown(skill, base_cooldown, cdr_percent)
            )
            multiplier = max(
                0.0,
                as_number(
                    overrides.get(norm(skill)),
                    runtime._skill_adjusted_multiplier(
                        skill, effective_level if mastery_bonus else level_text
                    ),
                ),
            )
            burning_bonus = max(
                0.0, runtime._skill_burning_hand_bonus_for_mode(skill, burning_mode)
            )
            display_multiplier = multiplier + burning_bonus
            action_output_factor = 1.0
            modeled_hits = None
            if settle_action_speed and is_ring_strike(skill):
                # Keep the editable multiplier and planner catalogs at the
                # no-speed reference. Only this table settles the Hit ramp.
                action_factor = 1.0 + (
                    action_speed_percent + runtime._skill_glyph_action_speed(skill)
                ) / 100.0
                action_output_factor = ring_strike_output_multiplier(action_factor)
                modeled_hits = ring_strike_default_hit_count(action_factor)
                display_multiplier *= action_output_factor
            glyph_damage_multiplier = runtime._skill_glyph_damage_multiplier(skill)
            element = norm(self.store.skill_damage_element(job, skill))
            element_multiplier = runtime._skill_element_display_multiplier(element, summary)
            dps_multiplier = runtime._skill_sim_dps_multiplier(element, job_profile)
            element_suffix = '' if element == '不适用' else f'{element}属性' if element else '无属性'
            channels: dict[str, float] = {}
            channel_panels: dict[str, float] = {}
            raw_percent = 0.0
            raw_parts: list[str] = []
            effective_parts: list[str] = []
            dps_parts: list[str] = []
            dpa_parts: list[str] = []
            periodic_cycle = max(0.0, runtime._skill_periodic_total_damage_cycle(skill))
            periodic_note = (
                runtime._skill_periodic_total_damage_dpa_note(skill, periodic_cycle)
                if periodic_cycle > 0
                else ""
            )
            timing = runtime._skill_effective_cast_timing(skill, action_speed_percent)
            cast_time = max(0.0, as_number(timing.get("cast_time")))
            full_cast_time = max(0.0, as_number(timing.get("full_cast_time"), cast_time))
            quick_cast_time = max(0.0, as_number(timing.get("quick_time")))
            cast_time_text = f"{cast_time:.3f}s"
            if as_number(timing.get("base_cast_time")) > 0 and abs(
                cast_time - as_number(timing.get("base_cast_time"))
            ) > 0.0005:
                cast_time_text += f"（基础 {as_number(timing.get('base_cast_time')):.3f}s）"
            if timing.get("fixed"):
                cast_time_text += "（固定）"

            damages = runtime._skill_level_damages(level_data)
            for damage_type, base_percent, base_fixed in damages:
                percent = base_percent * display_multiplier
                fixed = base_fixed * display_multiplier
                label = runtime._damage_type_label(damage_type)
                if element_suffix:
                    label = f"{label}{element_suffix}"
                raw_text = f"{label} {percent:,.1f}%"
                if fixed:
                    raw_text += f" + {fixed:,.0f}"
                if periodic_cycle > 0:
                    raw_text += f"（{periodic_cycle:g}s总计）"
                raw_parts.append(raw_text)
                raw_percent += percent

            if weighted_panel:
                effective_before_element = runtime._skill_level_effective(
                    level_data,
                    summary,
                    multiplier=display_multiplier,
                    weighted_panel=weighted_panel,
                ) * glyph_damage_multiplier
                label = f"{weighted_panel['label']}{element_suffix}"
                channels[label] = effective_before_element
                effective_text = f"{label} {effective_before_element:,.1f}%"
                if element in {'火', '水', '光', '暗'} or abs(element_multiplier - 1.0) > 1e-9:
                    effective_text += (
                        f"（属性后 {effective_before_element * element_multiplier:,.1f}%）"
                    )
                if periodic_cycle > 0:
                    effective_text += f"（{periodic_cycle:g}s总计）"
                effective_parts.append(effective_text)
            else:
                effective_before_element = 0.0
                for damage_type, base_percent, base_fixed in damages:
                    panel = runtime._damage_panel_value(damage_type, summary)
                    label = runtime._damage_type_label(damage_type)
                    if element_suffix:
                        label = f"{label}{element_suffix}"
                    if panel <= 0:
                        effective_parts.append(f"{label} 无法计算")
                        continue
                    value = (
                        base_percent * display_multiplier
                        + base_fixed * display_multiplier / panel * 100.0
                    ) * glyph_damage_multiplier
                    channels[label] = channels.get(label, 0.0) + value
                    channel_panels[label] = panel
                    effective_before_element += value
                    value_text = f"{label} {value:,.1f}%"
                    if element in {'火', '水', '光', '暗'} or abs(element_multiplier - 1.0) > 1e-9:
                        value_text += f"（属性后 {value * element_multiplier:,.1f}%）"
                    if periodic_cycle > 0:
                        value_text += f"（{periodic_cycle:g}s总计）"
                    effective_parts.append(value_text)

            effective = effective_before_element * dps_multiplier * element_multiplier
            dps = effective / cooldown if cooldown > 0 else 0.0
            dpa = 0.0 if periodic_cycle > 0 else (
                effective / cast_time if cast_time > 0 else 0.0
            )
            for label, value in channels.items():
                adjusted = value * dps_multiplier
                dps_text = f"{label} {adjusted / cooldown:,.1f}%/s" if cooldown > 0 else f"{label} 无冷却"
                if (element in {'火', '水', '光', '暗'} or abs(element_multiplier - 1.0) > 1e-9) and cooldown > 0:
                    dps_text += f"（属性后 {adjusted * element_multiplier / cooldown:,.1f}%/s）"
                dps_parts.append(dps_text)
                if periodic_cycle > 0:
                    continue
                dpa_text = f"{label} {adjusted / cast_time:,.1f}%/s" if cast_time > 0 else f"{label} 无施法时间"
                if (element in {'火', '水', '光', '暗'} or abs(element_multiplier - 1.0) > 1e-9) and cast_time > 0:
                    dpa_text += f"（属性后 {adjusted * element_multiplier / cast_time:,.1f}%/s）"
                dpa_parts.append(dpa_text)
            if periodic_note:
                dpa_parts = [periodic_note]

            if weighted_panel:
                expected_panel = runtime._skill_weighted_panel_value(summary, weighted_panel)
                actual_damage = expected_panel * effective / 100.0
            else:
                actual_damage = sum(
                    channel_panels.get(label, 0.0)
                    * value
                    * dps_multiplier
                    * element_multiplier
                    / 100.0
                    for label, value in channels.items()
                )
            required_level = int(as_number(level_data[1])) if len(level_data) > 1 else 0
            required_level_text = str(required_level)
            if mastery_bonus:
                required_level_text += f" / 专精+{mastery_bonus}=>Lv{effective_level}"
            description = self.store.skill_damage_description(
                job, skill, effective_level if mastery_bonus else level_text
            )
            if mastery_bonus:
                description = f"专精+{mastery_bonus}，按Lv{effective_level}计算\n{description}"
            result.append({
                "skill": skill,
                "level": level_text,
                "effectiveLevel": effective_level,
                "masteryBonus": mastery_bonus,
                "requiredLevel": required_level,
                "requiredLevelText": required_level_text,
                "baseCooldown": base_cooldown,
                "cooldown": cooldown,
                "cooldownOverride": False,
                "multiplier": multiplier,
                "channelMultipliers": self.store.skill_damage_adjustment(job, skill).get("channel_multipliers", {}),
                "settledMultiplier": multiplier * action_output_factor,
                "actionSpeedOutputMultiplier": action_output_factor,
                "referenceHitCount": ring_strike_default_hit_count() if is_ring_strike(skill) else None,
                "modeledHitCount": modeled_hits,
                "hitModelNote": "默认Hit参考；不按实际地形落差反推；逐Hit递增至封顶" if modeled_hits is not None else "",
                "burningHandPercent": burning_bonus * 100.0,
                "rawPercent": raw_percent,
                "effectivePercent": effective,
                "actualDamage": actual_damage,
                "dps": dps,
                "dpa": dpa,
                "castTime": cast_time,
                "fullCastTime": full_cast_time,
                "quickCastTime": quick_cast_time,
                "castTimeText": cast_time_text,
                "element": element,
                "elementMultiplier": element_multiplier,
                "dpsMultiplier": dps_multiplier,
                "channels": channels,
                "rawParts": raw_parts,
                "effectiveParts": effective_parts,
                "dpsParts": dps_parts,
                "dpaParts": dpa_parts,
                "periodicCycle": periodic_cycle,
                "periodicNote": periodic_note,
                "description": description,
            })
        result.sort(key=lambda row: (as_number(row["dps"]), as_number(row["dpa"])), reverse=True)
        return {
            "job": job,
            "rows": result,
            "summary": summary,
            "attributes": [dataclasses.asdict(row) for row in rows],
        }
    def skill_sim_rows(self, build: dict[str, Any], selections: list[dict[str, Any]]) -> dict[str, Any]:
        """Calculate the editable V4.01-style ten-row skill table."""
        proposed = copy.deepcopy(build)
        valid_by_skill: dict[str, dict[str, Any]] = {}
        for row in selections:
            if not isinstance(row, dict) or norm(row.get("skill")) in {"", NONE_TEXT}:
                continue
            normalized = copy.deepcopy(row)
            skill = self.store.skill_damage_canonical_name(
                norm(proposed.get("second_job")), normalized.get("skill")
            )
            normalized["skill"] = skill
            valid_by_skill.setdefault(skill, normalized)
        valid = list(valid_by_skill.values())
        proposed["_skill_level_overrides"] = {
            norm(row.get("skill")): max(1, int(as_number(row.get("level"), 1)))
            for row in valid
        }
        proposed["skill_multiplier_overrides"] = {
            "second_job": norm(proposed.get("second_job")),
            "rows": [
                {
                    "skill": norm(row.get("skill")),
                    "value": max(
                        0.0,
                        as_number(
                            row.get("multiplier"),
                            self.store.skill_damage_multiplier(
                                norm(proposed.get("second_job")),
                                row.get("skill"),
                                row.get("level"),
                            ),
                        ),
                    ),
                }
                for row in valid
            ],
        }
        calculated = self.skill_rows(proposed, True, settle_action_speed=True)
        by_skill = {norm(row.get("skill")): row for row in calculated.get("rows", []) if isinstance(row, dict)}
        rows: list[dict[str, Any]] = []
        for source in valid:
            skill = norm(source.get("skill"))
            current = copy.deepcopy(by_skill.get(skill))
            if not isinstance(current, dict):
                continue
            automatic_cooldown = max(0.0, as_number(current.get("cooldown")))
            override_marker = source.get("cooldownOverride")
            # Old API callers did not have an override marker, so keep their
            # explicit cooldown semantics.  The V4.41 frontend now sends
            # ``false`` for automatically calculated rows and ``true`` only
            # after the user edits the cooldown field.
            use_override = (
                "cooldown" in source
                if override_marker is None
                else bool(override_marker)
            )
            cooldown = (
                max(0.0, as_number(source.get("cooldown"), automatic_cooldown))
                if use_override
                else automatic_cooldown
            )
            current["automaticCooldown"] = automatic_cooldown
            current["cooldownOverride"] = use_override
            current["cooldown"] = cooldown
            current["dps"] = as_number(current.get("effectivePercent")) / cooldown if cooldown > 0 else 0.0
            element_multiplier = as_number(current.get("elementMultiplier"), 1.0)
            dps_multiplier = as_number(current.get("dpsMultiplier"), 1.0)
            has_element = bool(norm(current.get("element"))) or abs(element_multiplier - 1.0) > 1e-9
            dps_parts: list[str] = []
            for label, value in current.get("channels", {}).items():
                adjusted = as_number(value) * dps_multiplier
                text = f"{label} {adjusted / cooldown:,.1f}%/s" if cooldown > 0 else f"{label} 无冷却"
                if has_element and cooldown > 0:
                    text += f"（属性后 {adjusted * element_multiplier / cooldown:,.1f}%/s）"
                dps_parts.append(text)
            current["dpsParts"] = dps_parts
            if calculated.get("job") == "狂战士" and skill == "旋转冲击":
                self._add_circle_swing_comparison(proposed, current)
            rows.append(current)
        return {
            "job": calculated.get("job", norm(build.get("second_job"))),
            "rows": rows,
            "summary": calculated.get("summary", {}),
            "attributes": calculated.get("attributes", []),
            "totalDps": sum(as_number(row.get("dps")) for row in rows),
        }
    def _add_circle_swing_comparison(self, build: dict[str, Any], current: dict[str, Any]) -> None:
        """Display both EX forms without creating another skill/cooldown slot.

        Derive the ordinary form using the same fury damage and animation
        ratios as the timeline, including the user's multiplier/CD overrides.
        The child is intentionally excluded from totalDps and saved selections.
        """
        runtime = self.legacy_timeline._runtime(build, {"mode": "long"})
        skill = current["skill"]
        cost, boost = runtime._skill_burst_fury_consumer(skill)
        time_ratio = runtime._skill_burst_fury_cast_time_ratio(skill, {})
        if cost <= 0 or boost <= 1 or time_ratio >= 1:
            return
        ordinary = copy.deepcopy(current)
        ordinary["displayName"] = "旋转冲击（普通）"
        ordinary["variantNote"] = "不消耗怒气；含左键追加。与真旋转共用冷却，仅作对比，不重复计入 DPS 总和。"
        ratio = 1.0 / boost
        for key in ("multiplier", "settledMultiplier", "rawPercent", "effectivePercent", "actualDamage", "dps"):
            ordinary[key] = as_number(current.get(key)) * ratio
        for key in ("castTime", "fullCastTime", "quickCastTime"):
            ordinary[key] = as_number(current.get(key)) * time_ratio
        cast_time = ordinary["castTime"]
        ordinary["castTimeText"] = f"{cast_time:.3f}s"
        ordinary["dpa"] = ordinary["effectivePercent"] / cast_time if cast_time > 0 else 0.0
        ordinary["channels"] = {label: value * ratio for label, value in current["channels"].items()}
        element_multiplier = current["elementMultiplier"]
        dps_multiplier = current["dpsMultiplier"]
        has_element = bool(current["element"]) or abs(element_multiplier - 1.0) > 1e-9
        element_suffix = '' if current['element'] == '不适用' else f"{current['element']}属性" if current['element'] else '无属性'
        level_data, _, _ = runtime._skill_sim_level_data_with_mastery(skill, current["level"])
        ordinary["rawParts"] = []
        for damage_type, base_percent, base_fixed in runtime._skill_level_damages(level_data):
            label = runtime._damage_type_label(damage_type) + element_suffix
            text = f"{label} {base_percent * ordinary['multiplier']:,.1f}%"
            if base_fixed:
                text += f" + {base_fixed * ordinary['multiplier']:,.0f}"
            ordinary["rawParts"].append(text)
        ordinary["effectiveParts"] = []
        for label, value in ordinary["channels"].items():
            text = f"{label} {value:,.1f}%"
            if has_element:
                text += f"（属性后 {value * element_multiplier:,.1f}%）"
            ordinary["effectiveParts"].append(text)
        for key, duration, missing in (("dpsParts", current["cooldown"], "无冷却"), ("dpaParts", cast_time, "无施法时间")):
            ordinary[key] = []
            for label, value in ordinary["channels"].items():
                adjusted = value * dps_multiplier
                text = f"{label} {adjusted / duration:,.1f}%/s" if duration > 0 else f"{label} {missing}"
                if has_element and duration > 0:
                    text += f"（属性后 {adjusted * element_multiplier / duration:,.1f}%/s）"
                ordinary[key].append(text)
        current["displayName"] = f"真旋转冲击（{cost}怒气）"
        current["variantNote"] = "含左键追加；下方普通形态共用此技能等级、冷却和倍率调整。"
        current["comparisonRows"] = [ordinary]
    def _skill_glyph_candidates(self, build: dict[str, Any]) -> list[dict[str, Any]]:
        job = norm(build.get("second_job"))
        learned = self._skill_levels_by_name(self.store, build)
        rows: list[dict[str, Any]] = []
        allowed = {"伤害增加", "冷却缩减", "增益持续", "动作速度增加", "动作速度"}
        for entry in self.store.skill_glyphs(job):
            skill = norm(entry.get("skill"))
            if not skill or learned.get(skill, 0) <= 0:
                continue
            effects = entry.get("effects", {}) if isinstance(entry.get("effects"), dict) else {}
            for effect, options in effects.items():
                effect = norm(effect)
                if effect not in allowed or not isinstance(options, list):
                    continue
                valid = [option for option in options if isinstance(option, dict)]
                if not valid:
                    continue
                option = max(valid, key=lambda item: as_number(item.get("value")))
                value = max(0.0, as_number(option.get("value")))
                if value <= 0:
                    continue
                rows.append({
                    "skill": skill,
                    "effect": effect,
                    "option": f"{norm(option.get('grade'))} {value:g}%".strip(),
                    "value": value,
                })
        return apply_damage_cooldown_glyph_policy(rows)
    def optimize_skill_glyphs(
        self,
        payload: dict[str, Any],
        progress: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Select glyphs by two-stage local counterfactual, then verify once."""
        notify = progress or (lambda _value, _message: None)
        source = payload.get("build")
        build = copy.deepcopy(source if isinstance(source, dict) else self.get_build("build1"))
        job = norm(build.get("second_job"))
        count = max(1, min(7, int(as_number(payload.get("count"), 4))))
        window = max(30.0, min(600.0, as_number(payload.get("window"), 125.0)))
        candidates = self._skill_glyph_candidates(build)
        candidate_count = len(candidates)
        settings = {"window": window, "cocktail": bool(payload.get("cocktail", True))}
        build["skill_glyphs"] = {
            "second_job": norm(build.get("second_job")), "count": count,
            "rows": [], "auto_selected": True,
        }
        baseline = self.quick_timeline(build, settings)
        base_total = as_number(baseline.get("total"))
        contribution_by_skill = {
            norm(row.get("skill")): max(0.0, as_number(row.get("output")))
            for row in baseline.get("contribution", [])
            if isinstance(row, dict) and norm(row.get("skill"))
        }
        support_team_size = max(1, int(as_number(payload.get("supportTeamSize"), 1)))
        segment_counts: dict[str, int] = {}
        segment_cast_seconds: dict[str, float] = {}
        for segment in baseline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            skill = norm(segment.get("skill"))
            if not skill:
                continue
            segment_counts[skill] = segment_counts.get(skill, 0) + 1
            segment_cast_seconds[skill] = segment_cast_seconds.get(skill, 0.0) + max(
                0.0,
                as_number(segment.get("end")) - as_number(segment.get("start")),
            )

        def linked_cooldown_package(skill: str) -> tuple[float, float]:
            """Measure the fixed-axis package unlocked by one root skill."""

            linked = {
                norm(name)
                for name in linked_cooldown_glyph_skills(job, skill)
                if norm(name)
            }
            if not linked:
                return 0.0, 0.0
            linked_output = sum(
                contribution_by_skill.get(name, 0.0) for name in linked
            )
            root_casts = max(1.0, float(segment_counts.get(skill, 0) or 1))
            linked_cast_time = 0.0
            for name in linked:
                casts = max(0.0, float(segment_counts.get(name, 0)))
                casts_per_root = min(1.0, casts / root_casts)
                average_cast = (
                    segment_cast_seconds.get(name, 0.0) / casts
                    if casts > 0
                    else 0.0
                )
                linked_cast_time += average_cast * casts_per_root
            return max(0.0, linked_output), max(0.0, linked_cast_time)

        support_context: dict[str, list[dict[str, float]]] = {}
        try:
            support_runtime = self.legacy_timeline.extreme_runtime(build)
            own_effects = support_runtime._team_effects_for_jobs(
                [norm(build.get("second_job"))]
            )
        except Exception:
            support_runtime = None
            own_effects = []

        def support_skill_key(value: Any) -> str:
            """Match a base-skill glyph to the learned EX team effect."""

            skill_name = norm(value)
            if support_runtime is not None:
                canonicalize = getattr(
                    support_runtime, "_skill_burst_skill_key", None
                )
                if callable(canonicalize):
                    return norm(canonicalize(skill_name))
            return re.sub(r"\s*EX$", "", skill_name, flags=re.IGNORECASE).strip()

        for effect in own_effects:
            if not isinstance(effect, dict):
                continue
            display_skill = (
                norm(support_runtime._team_effect_skill_name(effect.get("skill")))
                if support_runtime is not None
                else norm(effect.get("skill"))
            )
            skill = support_skill_key(display_skill)
            duration = max(0.0, as_number(effect.get("duration")))
            cooldown = max(duration, as_number(effect.get("cooldown")))
            objective_gain = approximate_team_objective_gain_ratio(
                effect.get("attrs", {})
            ) * support_team_size
            if skill and duration > 0 and cooldown > 0 and objective_gain > 0:
                support_context.setdefault(skill, []).append({
                    "duration": duration,
                    "cooldown": cooldown,
                    "objective_gain": objective_gain,
                })

        def support_coverage_gain(row: dict[str, Any]) -> float:
            effect = norm(row.get("effect"))
            if effect not in {"冷却缩减", "增益持续"}:
                return 0.0
            value = max(0.0, as_number(row.get("value")))
            gain = 0.0
            for context in support_context.get(
                support_skill_key(row.get("skill")), []
            ):
                duration = max(0.0, as_number(context.get("duration")))
                cooldown = max(duration, as_number(context.get("cooldown")))
                if duration <= 0 or cooldown <= 0:
                    continue
                base_coverage = min(1.0, duration / cooldown)
                if effect == "冷却缩减":
                    glyph_coverage = min(
                        1.0,
                        duration
                        / (cooldown * max(0.05, 1.0 - value / 100.0)),
                    )
                else:
                    glyph_coverage = min(
                        1.0,
                        duration * (1.0 + value / 100.0) / cooldown,
                    )
                gain += (
                    base_total
                    * max(0.0, as_number(context.get("objective_gain")))
                    * max(0.0, glyph_coverage - base_coverage)
                )
            return max(0.0, gain)

        def fast_score(row: dict[str, Any]) -> float:
            """Estimate marginal output without another full-axis run."""

            skill_output = contribution_by_skill.get(norm(row.get("skill")), 0.0)
            if skill_output <= 0:
                skill_output = max(1.0, base_total * 1e-8)
            value = max(0.0, as_number(row.get("value"))) / 100.0
            effect = norm(row.get("effect"))
            if effect == "伤害增加":
                factor = value
            elif effect == "冷却缩减":
                reduction = min(0.8, value)
                factor = reduction / max(0.2, 1.0 - reduction) * 0.8
            elif effect in {"动作速度增加", "动作速度"}:
                factor = value * 0.35
            elif effect == "增益持续":
                factor = value * 0.25
            else:
                factor = value
            linked_output = (
                linked_cooldown_package(norm(row.get("skill")))[0]
                if effect == "冷却缩减"
                else 0.0
            )
            return (
                (skill_output + linked_output) * factor
                + support_coverage_gain(row)
            )

        def local_counterfactual_score(row: dict[str, Any]) -> float:
            """Keep the baseline axis fixed and change only this glyph."""

            skill = norm(row.get("skill"))
            effect = norm(row.get("effect"))
            skill_output = contribution_by_skill.get(skill, 0.0)
            value = max(0.0, as_number(row.get("value"))) / 100.0
            support_gain = support_coverage_gain(row)
            if effect == "伤害增加":
                return skill_output * value
            if effect == "冷却缩减":
                reduction = min(0.8, value)
                linked_output, linked_cast_time = linked_cooldown_package(skill)
                package_output = skill_output + linked_output
                direct_gain = (
                    package_output
                    * reduction
                    / max(0.2, 1.0 - reduction)
                )
                casts = max(1.0, float(segment_counts.get(skill, 0)))
                extra_casts = casts * reduction / max(0.2, 1.0 - reduction)
                average_cast = (
                    segment_cast_seconds.get(skill, 0.0) / casts
                    if segment_cast_seconds.get(skill, 0.0) > 0
                    else 0.0
                )
                opportunity_cost = (
                    max(0.0, base_total - package_output)
                    / max(1e-9, window)
                    * (average_cast + linked_cast_time)
                    * extra_casts
                )
                return max(0.0, direct_gain + support_gain - opportunity_cost)
            if effect in {"动作速度增加", "动作速度"}:
                casts = max(1.0, float(segment_counts.get(skill, 0)))
                average_cast = (
                    segment_cast_seconds.get(skill, 0.0) / casts
                    if segment_cast_seconds.get(skill, 0.0) > 0
                    else 0.0
                )
                saved_time = average_cast * (1.0 - 1.0 / (1.0 + value)) * casts
                return (
                    max(0.0, base_total - skill_output)
                    / max(1e-9, window)
                    * saved_time
                )
            if effect == "增益持续":
                return skill_output * value + support_gain
            return fast_score(row)

        required_skill = "精神爆发" if job in {"影舞者", "风行者"} else "时间加速" if job in {"黑暗女王", "时空领主"} else ""
        selector_candidates: list[dict[str, Any]] = []
        for row in candidates:
            copied = copy.deepcopy(row)
            confidence_weight = glyph_marginal_score_multiplier(
                copied.get("skill"),
                copied.get("effect"),
                job,
            )
            copied["score"] = fast_score(copied) * confidence_weight
            copied["local_score"] = (
                local_counterfactual_score(copied) * confidence_weight
            )
            selector_candidates.append(copied)
        selected = [
            {
                key: value
                for key, value in row.items()
                if key not in {"score", "local_score"}
            }
            for row in select_skill_glyphs_two_stage(
                selector_candidates,
                count,
                forced=[(required_skill, "冷却缩减")] if required_skill else (),
                fill_to_count=True,
            )
        ]

        build["skill_glyphs"]["rows"] = copy.deepcopy(selected)
        notify(70, "局部反事实选择完成，正在验证最终纹章组合")
        result = self.quick_timeline(build, settings)
        optimized_total = as_number(result.get("total"))
        if selected and optimized_total + 1e-6 < base_total:
            # CDR can occasionally reorder a tightly packed rotation and lose
            # output. Verify one damage-only safety combination instead of
            # returning a regression or re-running every candidate.
            safe_candidates = [
                row for row in selector_candidates
                if norm(row.get("effect")) == "伤害增加"
            ]
            fallback = [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"score", "local_score"}
                }
                for row in select_skill_glyphs(
                    safe_candidates, count, fill_to_count=False
                )
            ]
            fallback_build = copy.deepcopy(build)
            fallback_build["skill_glyphs"]["rows"] = copy.deepcopy(fallback)
            notify(85, "组合收益异常，正在验证伤害纹章安全回退")
            fallback_result = self.quick_timeline(fallback_build, settings)
            fallback_total = as_number(fallback_result.get("total"))
            if fallback_total >= optimized_total:
                selected = fallback
                result = fallback_result
                optimized_total = fallback_total
            if optimized_total + 1e-6 < base_total:
                selected = []
                result = baseline
                optimized_total = base_total
        notify(100, "技能纹章优化完成")
        return {
            "count": count,
            "rows": selected,
            "baseline": base_total,
            "optimized": optimized_total,
            "gain": (
                optimized_total / base_total - 1.0 if base_total > 0 else 0.0
            ),
            "timeline": result,
            "mode": "two-stage-linked-cooldown-independent-damage-v3",
            "candidateCount": candidate_count,
        }


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeBuildMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
