from __future__ import annotations

from functools import lru_cache
from ..live.aura_state import is_aura_row, is_aura_damage_series, is_inferred_aura_row

class NativeProfileSyncMixin:
    @staticmethod
    def _profile_fields(profile: dict[str, Any]) -> dict[str, str]:
        source = profile.get("fields", [])
        if isinstance(source, dict):
            return {norm(key): norm(value) for key, value in source.items()}
        return {
            norm(row[0]): norm(row[1])
            for row in source
            if isinstance(row, (list, tuple)) and len(row) >= 2
        }
    @staticmethod
    def _profile_signature(profile: dict[str, Any]) -> tuple[str, str, int]:
        return (
            norm(profile.get("target_id")),
            norm(profile.get("response_layout")),
            int(as_number(profile.get("timestamp_ms"))),
        )
    @staticmethod
    def _profile_item(detail: Any) -> tuple[str, list[str]]:
        parts = [part.strip() for part in str(detail or "").split("；") if part.strip()]
        return (parts[0] if parts else "", parts[1:])
    @staticmethod
    def _profile_section(detail: Any, label: str) -> str:
        match = re.search(rf"(?:^|；){re.escape(label)}：([^；]+)", str(detail or ""))
        return match.group(1).strip() if match else ""
    @staticmethod
    def _profile_level(detail: Any) -> int:
        match = re.search(r"需求\s*Lv\.(\d+)", str(detail or ""), re.IGNORECASE)
        return int(match.group(1)) if match else 0
    @staticmethod
    def _profile_enhance(detail: Any) -> int:
        match = re.search(r"强化\s*\+(\d+)", str(detail or ""))
        return int(match.group(1)) if match else 0
    @staticmethod
    def _canonical_profile_attr(label: str) -> str:
        text = norm(label).replace("属性", "")
        aliases = {
            "最小物理攻击力": "物攻", "最大物理攻击力": "物攻", "物理攻击力": "物攻",
            "最小魔法攻击力": "魔攻", "最大魔法攻击力": "魔攻", "魔法攻击力": "魔攻",
            "最大生命值": "HP", "生命值": "HP", "最大魔法值": "MP", "魔法值": "MP",
            "火攻击": "火攻", "水攻击": "水攻", "光攻击": "光攻", "暗攻击": "暗攻",
            "火抵抗": "火防", "水抵抗": "水防", "光抵抗": "光防", "暗抵抗": "暗防",
            "物理防御": "防御", "魔法防御": "魔防", "致命一击": "致命",
            "致命一击抵抗": "致命抵抗", "最终伤害": "最终",
        }
        return aliases.get(text, text)
    @classmethod
    def _profile_attributes(cls, text: Any) -> list[tuple[str, float, bool]]:
        rows: list[tuple[str, float, bool]] = []
        source = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", str(text or ""))
        for part in re.split(r"[，,]", source):
            match = re.match(r"\s*(.+?)\s+\+?(-?\d+(?:\.\d+)?)(%)?\s*$", part)
            if not match:
                continue
            rows.append((
                cls._canonical_profile_attr(match.group(1)),
                float(match.group(2)),
                bool(match.group(3)),
            ))
        return rows
    @staticmethod
    def _number_text(value: float) -> str:
        return str(int(value)) if abs(value - round(value)) < 1e-6 else f"{value:g}"
    @classmethod
    def _profile_calculator_attrs(cls, text: Any) -> dict[str, float]:
        aliases = {
            "最小物理攻击力": "最小物攻", "最大物理攻击力": "最大物攻",
            "物理攻击力": "物攻", "最小魔法攻击力": "最小魔攻",
            "最大魔法攻击力": "最大魔攻", "魔法攻击力": "魔攻",
            "最大生命值": "HP", "生命值": "HP", "最大魔法值": "MP",
            "魔法值": "MP", "物理防御": "防御", "魔法防御": "魔防",
            "致命一击": "致命", "致命一击抵抗": "致命抵抗",
            "最终伤害": "最终", "火攻击": "火攻", "水攻击": "水攻",
            "冰攻击": "水攻", "光攻击": "光攻", "暗攻击": "暗攻",
        }
        result: dict[str, float] = {}
        cleaned = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", str(text or ""))
        for part in re.split(r"[，,]", cleaned):
            match = re.match(r"\s*(.+?)\s+\+?(-?\d+(?:\.\d+)?)(%)?\s*$", part)
            if not match:
                continue
            attr = aliases.get(norm(match.group(1)), norm(match.group(1)))
            if match.group(3) and not attr.endswith("%"):
                attr = f"{attr}%"
            value = float(match.group(2))
            if attr == "物攻":
                result["最小物攻"] = result.get("最小物攻", 0.0) + value
                result["最大物攻"] = result.get("最大物攻", 0.0) + value
            elif attr == "魔攻":
                result["最小魔攻"] = result.get("最小魔攻", 0.0) + value
                result["最大魔攻"] = result.get("最大魔攻", 0.0) + value
            else:
                result[attr] = result.get(attr, 0.0) + value
        return result
    @staticmethod
    def _attribute_value_matches(actual: Any, candidate: Any) -> bool:
        expected = as_number(actual)
        selected = as_number(candidate)
        magnitude = max(abs(expected), abs(selected))
        # Server DNT values are rendered to two decimals while the editable
        # simulator tables intentionally store several glyph ranks as whole
        # numbers.  A sub-one-point difference is formatting/truncation, not a
        # different roll.  Small percentage-like values still require a tight
        # match so 0.3 cannot be confused with 0.4.
        tolerance = 1.05 if magnitude >= 10.0 else 0.011
        return abs(expected - selected) <= tolerance
    @classmethod
    def _attribute_vectors_match(
        cls,
        actual: dict[str, float],
        candidate: dict[str, float],
        *,
        allow_candidate_subset: bool = False,
    ) -> bool:
        actual_keys = set(actual)
        candidate_keys = set(candidate)
        if (
            not candidate_keys.issubset(actual_keys)
            if allow_candidate_subset
            else candidate_keys != actual_keys
        ):
            return False
        return all(
            cls._attribute_value_matches(actual[attr], candidate[attr])
            for attr in candidate
        )
    def _match_glyph_a_extra(self, level: str, name: str, actual: dict[str, float]) -> str:
        if not actual:
            return NONE_TEXT
        base = self.calculator.glyph_a_attrs({"level": level, "name": name, "extra": NONE_TEXT})
        matches: list[str] = []
        for extra in self.store.glyph_a_extra():
            if extra == NONE_TEXT:
                continue
            attrs = self.calculator.glyph_a_attrs({"level": level, "name": name, "extra": extra})
            delta = {
                attr: as_number(value) - as_number(base.get(attr))
                for attr, value in attrs.items()
                if abs(as_number(value) - as_number(base.get(attr))) > 1e-9
            }
            if self._attribute_vectors_match(actual, delta):
                matches.append(extra)
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else NONE_TEXT
    def _match_glyph_s_row(
        self, level: str, name: str, actual: dict[str, float]
    ) -> tuple[dict[str, Any], dict[str, float], bool]:
        empty = {
            "level": level, "name": name,
            "base_attr1": NONE_TEXT, "rank1": None,
            "base_attr2": NONE_TEXT, "rank2": None,
            "base_attr3": NONE_TEXT, "rank3": None,
            "base_attr4": NONE_TEXT, "rank4": None,
            "extra": NONE_TEXT,
        }
        base_options = [
            attr for attr in self.store.glyph_s_base_options(name)
            if attr != NONE_TEXT
        ]
        base_matches: list[tuple[int, str, str, dict[str, float]]] = []
        for option_index, attr in enumerate(base_options):
            matches: list[tuple[str, dict[str, float]]] = []
            if attr == NONE_TEXT:
                continue
            for rank in self.store.glyph_s_ranks():
                attrs = self.calculator.glyph_s_attrs({**empty, "base_attr1": attr, "rank1": rank})
                if attrs and self._attribute_vectors_match(
                    actual, attrs, allow_candidate_subset=True
                ):
                    matches.append((rank, attrs))
            # A server value must identify one and only one rank.  Nearby
            # values are never rounded to the closest rank.
            if len(matches) == 1:
                rank, attrs = matches[0]
                base_matches.append((option_index, attr, rank, attrs))

        extra_matches: list[tuple[str, dict[str, float]]] = []
        for extra in self.store.glyph_s_extra_options(name):
            if extra == NONE_TEXT:
                continue
            attrs = self.calculator.glyph_s_attrs({**empty, "extra": extra})
            if attrs and self._attribute_vectors_match(
                actual, attrs, allow_candidate_subset=True
            ):
                extra_matches.append((extra, attrs))

        row = copy.deepcopy(empty)
        represented: dict[str, float] = {}

        # The editor now exposes every fixed base family: Rise has four,
        # Resistance has three, and Attack/Root have two.  Keep DNT option
        # order stable so a rank-1 baseline and a rolled higher rank can both
        # be represented without falling back to measured-value correction.
        selected_base = sorted(base_matches, key=lambda match: match[0])[:4]
        for index, (_option_index, attr, rank, attrs) in enumerate(
            selected_base, start=1
        ):
            row[f"base_attr{index}"] = attr
            row[f"rank{index}"] = rank
            for key, value in attrs.items():
                represented[key] = represented.get(key, 0.0) + as_number(value)

        # The four S-glyph families use disjoint base and extra attribute
        # families.  Therefore a unique exact extra match is authoritative;
        # zero matches means no extra only when every observed key belongs to
        # that glyph's base family.  Ambiguous/foreign keys remain unresolved
        # instead of being assigned to a nearby option.
        base_keys: set[str] = set()
        for attr in base_options:
            for rank in self.store.glyph_s_ranks():
                attrs = self.calculator.glyph_s_attrs(
                    {**empty, "base_attr1": attr, "rank1": rank}
                )
                base_keys.update(attrs)
        foreign_keys = set(actual) - base_keys
        extra_resolved = False
        if len(extra_matches) == 1:
            extra, extra_attrs = extra_matches[0]
            if foreign_keys.issubset(extra_attrs):
                row["extra"] = extra
                extra_resolved = True
                for key, value in extra_attrs.items():
                    represented[key] = represented.get(key, 0.0) + as_number(value)
        elif not extra_matches and not foreign_keys:
            row["extra"] = NONE_TEXT
            extra_resolved = True

        return row, represented, extra_resolved
    @classmethod
    def _profile_enchants(
        cls,
        detail: Any,
        allowed: set[str],
        ignored_attrs: set[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        matched: list[str] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        ignored = ignored_attrs or set()
        for attr, value, percent in cls._profile_attributes(cls._profile_section(detail, "附魔")):
            if not percent or attr in ignored:
                continue
            candidate = f"{attr}{cls._number_text(value)}%"
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate in allowed:
                matched.append(candidate)
            else:
                unmatched.append(candidate)
        return matched, unmatched
    @staticmethod
    def _ignored_profile_enchant_attrs(job_profile: dict[str, Any]) -> set[str]:
        """Attributes that cannot improve this job's effective output panel."""
        ignored = {"火防", "水防", "光防", "暗防"}
        attack_attrs = set(job_profile.get("attack_attrs") or ())
        if "物攻" not in attack_attrs:
            ignored.add("物攻")
        if "魔攻" not in attack_attrs:
            ignored.add("魔攻")
        element_attrs = set(job_profile.get("element_attrs") or ())
        ignored.update({"火攻", "水攻", "光攻", "暗攻"} - element_attrs)
        return ignored
    @staticmethod
    def _match_option(value: str, options: list[str]) -> str:
        if value in options:
            return value
        key = re.sub(r"[\s（）()]", "", norm(value)).casefold()
        exact = [item for item in options if re.sub(r"[\s（）()]", "", norm(item)).casefold() == key]
        return exact[0] if exact else ""
    def _profile_job_pair(self, job: str) -> tuple[str, str]:
        selected = norm(job)
        for first in self.store.first_jobs():
            if selected in self.store.second_jobs(first):
                return first, selected
        return "", ""
    def _profile_rune_entries(self, detail: str, rune_type: str, level: str) -> list[dict[str, Any]]:
        allowed = set(self.store.rune_attrs(rune_type)) - {NONE_TEXT}
        result: list[dict[str, Any]] = []
        for attr, value, percent in self._profile_attributes(self._profile_section(detail, "当前附加")):
            if percent or attr not in allowed:
                continue
            choices = [
                (abs(as_number(row.get("数值")) - value), int(as_number(row.get("档位"))))
                for row in self.store.tables["rune"]
                if norm(row.get("数据类型")) == "普通"
                and norm(row.get("等级")) == level
                and norm(row.get("石板类型/转换")) == rune_type
                and norm(row.get("属性")) == attr
            ]
            if choices:
                _delta, rank = min(choices)
                result.append({"attr": attr, "rank": rank})
        return result[:6]
    def _profile_fashion_option(self, slot: str, detail: str) -> str:
        options = self.store.fashion_options("戒指" if slot.startswith("戒指") else slot)
        if slot == "精灵":
            grade = self._profile_section(detail, "精灵品质").upper()
            enhance = self._profile_enhance(detail)
            if grade in {"A", "S"} and enhance > 0:
                option = self._match_option(f"{grade}精灵（+{enhance}）", options)
                if option:
                    return option
        level = self._profile_level(detail)
        if level:
            level_matches = [option for option in options if str(level) in option]
            if len(level_matches) == 1:
                return level_matches[0]
        actual = self._profile_attributes(self._profile_section(detail, "基础属性"))
        actual_map = {(attr, percent): value for attr, value, percent in actual}
        best = ""
        best_error = float("inf")
        for option in options:
            if option == NONE_TEXT:
                continue
            expected = self.calculator.fashion_attrs(option)
            comparable = []
            for attr, value in expected.items():
                percent = norm(attr).endswith("%")
                key = (norm(attr).rstrip("%"), percent)
                if key in actual_map:
                    comparable.append(abs(actual_map[key] - as_number(value)))
            if comparable and sum(comparable) < best_error:
                best, best_error = option, sum(comparable)
        return best
    def _profile_engraving_extra(
        self, detail: str
    ) -> tuple[str, str, str, float]:
        attrs = self._profile_attributes(self._profile_section(detail, "额外属性"))
        if not attrs:
            return NONE_TEXT, NONE_TEXT, NONE_TEXT, 0.0
        aliases = {
            "力量": "力量%", "敏捷": "敏捷%", "智力": "智力%", "体质": "体质%",
            "物攻": "物攻%", "魔攻": "魔攻%", "防御": "防御力%", "魔防": "魔防%",
            "致命": "暴击%", "HP": "生命%", "最终": "最终伤害%",
            "火攻": "火攻击%", "水攻": "冰攻击%", "光攻": "光攻击%", "暗攻": "暗攻击%",
        }
        attr_name, value, percent = attrs[0]
        extra = aliases.get(attr_name, "") if percent else ""
        if extra not in self.store.engraving_extra_options():
            return NONE_TEXT, NONE_TEXT, NONE_TEXT, 0.0
        rank = next((
            option for option in self.store.engraving_extra_rank_options(extra)
            if abs(self._option_number(option) - value) < 1e-6
        ), "")
        if not rank:
            return NONE_TEXT, NONE_TEXT, NONE_TEXT, 0.0
        tier = "高档" if "高档" in rank else "低档" if "低档" in rank else NONE_TEXT
        return extra, tier, rank.split()[0], value
    @staticmethod
    def _option_number(text: Any) -> float:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", norm(text))
        return float(match.group(1)) if match else as_number(text)
    def _profile_to_build_preview(
        self,
        profile: dict[str, Any],
        current_build: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = (
            copy.deepcopy(current_build)
            if isinstance(current_build, dict)
            else self.get_build("build1")
        )
        proposed = copy.deepcopy(current)
        # A profile import rebuilds the character from the current server
        # records.  Do not carry residual corrections left by an earlier
        # import into the new proposal: those corrections were already based
        # on the old item/card decode and would be applied a second time.
        # Truly unmapped attributes from this profile are added back later by
        # ProfilePanelCalculator's fallback pass.
        proposed["free_inputs"] = {
            "percent": {},
            "stats": {},
            "stat_percent": {},
        }
        changes: list[dict[str, Any]] = []
        category_counts: Counter[str] = Counter()
        mapped_counts: Counter[str] = Counter()

        def record(
            category: str,
            item: str,
            detected: str,
            mapped: str = "",
            note: str = "",
            status: str | None = None,
        ) -> None:
            category_counts[category] += 1
            item_status = status or ("matched" if mapped else "unmatched")
            if item_status == "matched":
                mapped_counts[category] += 1
            changes.append({
                "category": category, "item": item, "detected": detected,
                "mapped": mapped, "status": item_status,
                "note": note,
            })

        first_job, second_job = self._profile_job_pair(norm(profile.get("job")))
        if second_job:
            proposed["first_job"] = first_job
            proposed["second_job"] = second_job
            self.apply_job_defaults(proposed, second_job)
            record("职业", "职业", second_job, f"{first_job} / {second_job}")
        else:
            record("职业", "职业", norm(profile.get("job")) or "未识别", note="保留配装1职业")

        catalog = self.build_catalog(proposed.get("first_job", ""), proposed.get("second_job", ""))
        allowed_enchants = set(catalog["enchants"]["all"])
        ignored_enchant_attrs = self._ignored_profile_enchant_attrs(
            self._job_profile(proposed)
        )
        equipment_by_slot = {norm(row.get("slot")): row for row in proposed.get("equipment", [])}
        jewelry_rows = list(proposed.get("jewelry", []))
        jewelry_positions = {"项链": 0, "耳环": 0, "戒指": 0}
        fashion_by_slot = {norm(row.get("slot")): row for row in proposed.get("fashion", [])}
        engraving_rows = list(proposed.get("engravings", []))
        engraving_positions: Counter[str] = Counter()
        glyph_a_records: list[tuple[str, str]] = []
        glyph_s_records: list[tuple[str, str]] = []
        skill_glyph_records: list[tuple[str, str]] = []
        rune_records: list[tuple[str, str]] = []
        core_record = ""

        equipment_slot_aliases = {"主武器": "主手", "副武器": "副手"}
        fashion_slot_aliases = {
            "主武器时装": "主手", "副武器时装": "副手", "翅膀": "翅膀",
            "尾巴": "尾巴", "印花": "印花", "精灵": "精灵", "精灵时装": "精灵",
            "时装槽13": "精灵",
            "项链时装": "项链",
            "耳环时装": "耳环", "戒指时装1": "戒指1", "戒指时装2": "戒指2",
            "戒指时装 1": "戒指1", "戒指时装 2": "戒指2",
        }
        unsupported_fashion_slots = {"头部时装", "上装时装", "下装时装", "手套时装", "鞋子时装"}
        engraving_slot_aliases = {
            "辅助武器": "副手", "主武器": "主手", "花纹": "印花",
            "戒指": "戒指", "项链": "项链", "耳环": "耳环",
        }

        for raw_key, raw_detail in profile.get("equipment", []):
            if not isinstance(raw_key, str):
                continue
            category, _, raw_slot = raw_key.partition("|")
            raw_slot = norm(raw_slot)
            detail = norm(raw_detail)
            name, _parts = self._profile_item(detail)
            if category == "石板":
                if "龙渊" in name or "[石板能力]" in detail:
                    record("石板", raw_slot, name, note="技能石板只影响技能，不计入最终面板")
                elif "源铸" in name:
                    core_record = detail
                else:
                    rune_records.append((raw_slot, detail))
                continue
            if category == "纹章":
                if "技能纹章" in name:
                    skill_glyph_records.append((raw_slot, detail))
                elif "远征队纹章" in name:
                    glyph_s_records.append((raw_slot, detail))
                elif "强化纹章" in name:
                    glyph_a_records.append((raw_slot, detail))
                else:
                    # The game profile also returns decorative, legacy and
                    # other glyph categories that have no editable/calculated
                    # slot in the simulator.  They are intentionally ignored:
                    # showing them as red unmatched rows suggests that the
                    # user must repair a setting which cannot affect the
                    # calculated build at all.
                    pass
                continue
            if category == "刻印":
                slot_key = re.sub(r"\s+", "", raw_slot)
                base_slot = engraving_slot_aliases.get(slot_key, slot_key)
                candidates = [row for row in engraving_rows if norm(row.get("slot")).startswith(base_slot)]
                index = engraving_positions[base_slot]
                engraving_positions[base_slot] += 1
                target = candidates[min(index, len(candidates) - 1)] if candidates else None
                attrs = self._profile_attributes(self._profile_section(detail, "基础属性"))
                raw_extra_text = self._profile_section(detail, "额外属性")
                extra_selector_unknown = raw_extra_text.startswith("未解码")
                extra, extra_tier, extra_rank, extra_value = (
                    self._profile_engraving_extra(detail)
                )
                main = 0.0
                secondary = 0.0
                if target:
                    target.update(
                        extra=extra,
                        extra_tier=extra_tier,
                        extra_rank=extra_rank,
                        extra_value=extra_value,
                    )
                    slot = norm(target.get("slot"))
                    values = {(attr, percent): value for attr, value, percent in attrs}
                    if slot in {"头盔", "上装", "下装", "手套", "鞋子"}:
                        main = max(values.get(("防御", True), 0.0), values.get(("魔防", True), 0.0))
                    elif slot in {"主手", "副手"}:
                        main = max(values.get(("物攻", True), 0.0), values.get(("魔攻", True), 0.0))
                    elif slot in {"项链", "耳环", "戒指1", "戒指2"}:
                        main = values.get(("HP", True), 0.0)
                        secondary = max(values.get(("物攻", True), 0.0), values.get(("魔攻", True), 0.0))
                    else:
                        main = values.get(("致命", True), 0.0)
                        secondary = max(values.get(("物攻", True), 0.0), values.get(("魔攻", True), 0.0))
                    # Server-returned values are authoritative even when a
                    # newly added roll is not present in the editable option
                    # table yet.  The calculator only needs these two numeric
                    # fields, so retaining them keeps profile import exact.
                    target.update(main_value=main, secondary_value=secondary)
                    grade = ""
                    option = f"{self._number_text(main)}%"
                    kind = (
                        "防具双防" if slot in {"头盔", "上装", "下装", "手套", "鞋子"}
                        else "武器双攻" if slot in {"主手", "副手"}
                        else "饰品生命" if slot in {"项链", "耳环", "戒指1", "戒指2"}
                        else "三件套致命"
                    )
                    for candidate_grade in self.store.engraving_grades():
                        if option in self.store.engraving_main_value_options(kind, candidate_grade):
                            grade = candidate_grade
                            break
                    if grade:
                        target.update(
                            grade=grade, value_rank=option, main_value=main,
                            secondary_value=secondary,
                        )
                        extra_text = (
                            f"；{extra} {extra_rank}" if extra != NONE_TEXT else ""
                        )
                        record(
                            "刻印", slot, name,
                            f"{grade} {option}{extra_text}",
                            (
                                "刻印额外属性尚未完成模板级校准，未采用部位或邻档推测"
                                if extra_selector_unknown
                                else "服务器选择码确认无额外属性"
                                if raw_extra_text == NONE_TEXT
                                else "服务器额外属性已显示；非面板词条不写入配装"
                                if raw_extra_text and extra == NONE_TEXT
                                else ""
                            ),
                            "unmatched" if extra_selector_unknown else "matched",
                        )
                    else:
                        record("刻印", slot, name, note="数值不在当前刻印档位表中，已保留原值")
                else:
                    record("刻印", raw_slot, name, note="未找到对应刻印槽")
                continue

            slot_key = re.sub(r"\s+", "", raw_slot)
            fashion_attributes = self._profile_attributes(
                self._profile_section(detail, "基础属性")
            )
            if slot_key in unsupported_fashion_slots:
                if fashion_attributes:
                    record(
                        "时装", raw_slot, name,
                        note="该外观部位有属性，但当前模拟器没有独立属性栏",
                    )
                else:
                    record(
                        "时装", raw_slot, name,
                        "无属性外观，不计入面板",
                        "服务器正文确认没有可计算属性",
                    )
                continue
            fashion_slot = fashion_slot_aliases.get(slot_key)
            if fashion_slot:
                target = fashion_by_slot.get(fashion_slot)
                option = self._profile_fashion_option(fashion_slot, detail)
                if not fashion_attributes and not (
                    fashion_slot == "精灵" and option
                ):
                    if target is not None:
                        target["name"] = NONE_TEXT
                    record(
                        "时装", fashion_slot, name,
                        "无属性外观，不计入面板",
                        "服务器正文确认没有可计算属性",
                    )
                    continue
                if target is not None and option:
                    target["name"] = option
                    record(
                        "时装", fashion_slot, name, option,
                        "按客户端精灵品质与强化等级映射"
                        if fashion_slot == "精灵" else "",
                    )
                else:
                    record("时装", fashion_slot, name, note="外观已识别，但属性档位无法唯一匹配")
                continue

            equipment_slot = equipment_slot_aliases.get(slot_key, slot_key)
            if equipment_slot in equipment_by_slot:
                target_slot = equipment_slot
                target = equipment_by_slot[target_slot]
                option = self._match_option(
                    name, catalog["equipment"].get(target_slot, [])
                )
                slot_conversion = ""
                # Dragon Nest's login archive calls a Sorceress staff the
                # main weapon and its orb/book/doll the sub weapon.  The
                # simulator's inherited equipment tables use the opposite
                # slot convention.  Prefer the declared slot, but when the
                # exact item only exists in the other weapon catalog, map it
                # there instead of marking both otherwise valid items red.
                if not option and target_slot in {"主手", "副手"}:
                    alternate_slot = "副手" if target_slot == "主手" else "主手"
                    alternate_option = self._match_option(
                        name, catalog["equipment"].get(alternate_slot, [])
                    )
                    if alternate_option and alternate_slot in equipment_by_slot:
                        target_slot = alternate_slot
                        target = equipment_by_slot[target_slot]
                        option = alternate_option
                        slot_conversion = (
                            f"游戏{equipment_slot}按模拟器{target_slot}槽位映射"
                        )
                if option:
                    target["name"] = option
                    target["enhance"] = self._profile_enhance(detail)
                    suffix_match = re.search(r"后缀：([^（；]+)", detail)
                    suffix = norm(suffix_match.group(1)) if suffix_match else NONE_TEXT
                    suffix_options = self.store.suffix_names(
                        proposed.get("first_job", ""), proposed.get("second_job", ""), option,
                    )
                    mapped_suffix = self._match_option(suffix, suffix_options)
                    if not mapped_suffix and suffix != NONE_TEXT:
                        mapped_suffix = next((row for row in suffix_options if row.startswith(suffix)), "")
                    target["suffix"] = mapped_suffix or NONE_TEXT
                    enchants, unknown = self._profile_enchants(
                        detail, allowed_enchants, ignored_enchant_attrs
                    )
                    target["enchants"] = enchants + [NONE_TEXT] * max(0, 5 - len(enchants))
                    notes = [slot_conversion] if slot_conversion else []
                    if unknown:
                        notes.append(f"未映射附魔：{'、'.join(unknown)}")
                    record("装备", target_slot, name, option, "；".join(notes))
                else:
                    record("装备", equipment_slot, name, note="当前职业装备库没有同名项目，已保留原值")
                continue

            jewelry_kind = "戒指" if slot_key.startswith("戒指") else slot_key
            candidates = [row for row in jewelry_rows if ("戒指" if norm(row.get("slot")).startswith("戒指") else norm(row.get("slot"))) == jewelry_kind]
            index = jewelry_positions.get(jewelry_kind, 0)
            jewelry_positions[jewelry_kind] = index + 1
            target = candidates[min(index, len(candidates) - 1)] if candidates else None
            if target is not None:
                option = self._match_option(name, catalog["jewelry"].get(jewelry_kind, []))
                if option:
                    target["name"] = option
                    variant_match = re.search(
                        r"首饰选项\s*[:：]\s*([^（(；;]+)", detail
                    )
                    variant = norm(variant_match.group(1)) if variant_match else NONE_TEXT
                    target["variant"] = self._match_option(
                        variant, self.store.jewelry_variants(jewelry_kind, option),
                    ) or NONE_TEXT
                    enchants, unknown = self._profile_enchants(
                        detail, allowed_enchants, ignored_enchant_attrs
                    )
                    target["enchants"] = enchants + [NONE_TEXT] * max(0, 5 - len(enchants))
                    note = f"未映射附魔：{'、'.join(unknown)}" if unknown else ""
                    record("首饰", norm(target.get("slot")), name, option, note)
                else:
                    record("首饰", raw_slot, name, note="首饰库没有同名项目，已保留原值")

        if glyph_a_records:
            current_rows = list(proposed.get("glyph_a", []))
            mapped_rows: list[dict[str, Any]] = []
            for slot, detail in glyph_a_records:
                name, _ = self._profile_item(detail)
                level_number = self._profile_level(detail)
                level = f"{level_number}A" if level_number else ""
                mapped_name = self._match_option(name, self.store.glyph_a_names())
                mapped_level = self._match_option(level, self.store.glyph_a_levels())
                if mapped_name and mapped_level:
                    actual_text = self._profile_section(detail, "纹章实际属性")
                    actual = self._profile_calculator_attrs(actual_text)
                    extra = self._match_glyph_a_extra(mapped_level, mapped_name, actual)
                    mapped_rows.append({"level": mapped_level, "name": mapped_name, "extra": extra})
                    selector_unknown = actual_text.startswith("未解码")
                    numeric_unmatched = bool(actual) and extra == NONE_TEXT
                    status = (
                        "unmatched" if selector_unknown or numeric_unmatched
                        else "matched"
                    )
                    note = (
                        "服务器纹章选择码尚未进入当前DNT，未猜测额外属性"
                        if selector_unknown
                        else "服务器实际属性不能唯一对应模拟器选项，未采用最近似值"
                        if numeric_unmatched
                        else "服务器选择码确认无额外属性"
                        if actual_text == NONE_TEXT
                        else "按服务器实际属性与DNT唯一匹配"
                    )
                    record(
                        "强化纹章", slot, name,
                        f"{mapped_level} {mapped_name}；额外属性 {extra}",
                        note,
                        status,
                    )
                else:
                    record("强化纹章", slot, name, note="纹章等级或名称未进入当前数据表")
            proposed["glyph_a"] = mapped_rows + [
                {"level": NONE_TEXT, "name": NONE_TEXT, "extra": NONE_TEXT}
                for _ in range(max(0, len(current_rows) - len(mapped_rows)))
            ]

        if glyph_s_records:
            rows: list[dict[str, Any]] = []
            for slot, detail in glyph_s_records:
                name, _ = self._profile_item(detail)
                level_number = self._profile_level(detail)
                level = f"{level_number}S" if level_number else ""
                mapped_name = self._match_option(name, self.store.glyph_s_names())
                mapped_level = self._match_option(level, self.store.glyph_s_levels())
                if mapped_name and mapped_level:
                    actual_text = self._profile_section(detail, "纹章实际属性")
                    actual = self._profile_calculator_attrs(actual_text)
                    row, represented, extra_resolved = self._match_glyph_s_row(
                        mapped_level, mapped_name, actual
                    )
                    rows.append(row)
                    residual = {
                        attr: as_number(value) - as_number(represented.get(attr))
                        for attr, value in actual.items()
                        if attr not in represented
                        or not self._attribute_value_matches(
                            value, represented.get(attr)
                        )
                    }
                    mapped_parts = [
                        f"{row[f'base_attr{index}']} {row[f'rank{index}']}"
                        for index in range(1, 5)
                        if row.get(f"rank{index}")
                    ]
                    if row.get("extra") != NONE_TEXT:
                        mapped_parts.append(f"额外 {row['extra']}")
                    mapped_text = " / ".join(mapped_parts) or "无可编辑档位"
                    selector_unknown = actual_text.startswith("未解码")
                    numeric_unmatched = bool(actual) and not extra_resolved
                    record(
                        "远征纹章", slot, name,
                        f"{mapped_level} {mapped_name}；{mapped_text}",
                        (
                            "服务器纹章选择码尚未进入当前DNT，未猜测属性"
                            if selector_unknown
                            else "服务器实际属性不能唯一对应模拟器选项，保留实值补正"
                            if numeric_unmatched
                            else "按服务器实际属性与DNT唯一匹配"
                        )
                        + (f"；另有 {len(residual)} 项由实值补正" if residual else ""),
                        "unmatched"
                        if selector_unknown or numeric_unmatched
                        else "matched",
                    )
                else:
                    record("远征纹章", slot, name, note="未进入当前远征纹章表")
            proposed["glyph_s"] = rows

        if skill_glyph_records:
            entries = self.store.skill_glyphs(proposed.get("second_job", ""))
            rows: list[dict[str, Any]] = []
            for slot, detail in skill_glyph_records:
                name, _ = self._profile_item(detail)
                skill_match = re.search(r"[（(]([^）)]+)[）)]", name)
                skill = norm(skill_match.group(1)) if skill_match else ""
                effect = (
                    "冷却缩减" if "冷却时间减少" in detail
                    else "增益持续" if "增益效果持续时间增加" in detail
                    else "动作速度增加" if "动作速度增加" in detail
                    else "伤害增加" if "伤害增加" in detail
                    else ""
                )
                value_match = re.search(r"(?:减少|增加)(\d+(?:\.\d+)?)%", detail)
                value = float(value_match.group(1)) if value_match else 0.0
                entry = next((row for row in entries if norm(row.get("skill")) == skill), None)
                options = (entry or {}).get("effects", {}).get(effect, []) if effect else []
                option = min(
                    (row for row in options if isinstance(row, dict)),
                    key=lambda row: abs(as_number(row.get("value")) - value),
                    default=None,
                )
                if entry and option:
                    selected_value = as_number(option.get("value"))
                    rows.append({
                        "skill": skill, "effect": effect,
                        "option": f"{norm(option.get('grade'))} {selected_value:g}%".strip(),
                        "value": selected_value,
                    })
                    record("技能纹章", slot, name, f"{skill} {effect} {selected_value:g}%")
                else:
                    record("技能纹章", slot, name, note="技能或效果未进入当前职业纹章库")
            proposed["skill_glyphs"] = {
                "second_job": proposed.get("second_job", ""),
                "count": len(rows), "rows": rows, "auto_selected": False,
            }

        if rune_records:
            rows: list[dict[str, Any]] = []
            for slot, detail in rune_records[:4]:
                level_number = self._profile_level(detail)
                level = str(level_number) if level_number else NONE_TEXT
                attack_entries = self._profile_rune_entries(detail, "攻击", level)
                defense_entries = self._profile_rune_entries(detail, "防御", level)
                rune_type = "攻击" if len(attack_entries) >= len(defense_entries) else "防御"
                entries = attack_entries if rune_type == "攻击" else defense_entries
                if entries:
                    rows.append({
                        "type": rune_type, "level": level,
                        "entries": entries + [{"attr": NONE_TEXT, "rank": None}] * (6 - len(entries)),
                    })
                    record("普通石板", slot, self._profile_item(detail)[0], f"{level}级{rune_type}石板（{len(entries)}条属性）")
                else:
                    record("普通石板", slot, self._profile_item(detail)[0], note="当前附加属性无法映射到石板档位")
            if rows:
                proposed["normal_runes"] = rows

        if core_record:
            name, _ = self._profile_item(core_record)
            source = self._profile_section(core_record, "源铸属性")
            conversion_match = re.search(r"([^，]+→[^，]+)", source)
            ratio_match = re.search(r"转换比例\s*(\d+(?:\.\d+)?)%", source)
            coeff_match = re.search(r"转换系数\s*(\d+(?:\.\d+)?)", source)
            ranks = [int(value) for value in re.findall(r"档\s*(\d+)", source)]
            conversion = norm(conversion_match.group(1)) if conversion_match else ""
            if conversion and ratio_match and coeff_match:
                ratio_options = self.store.core_ratio_ranks(conversion)
                coeff_options = self.store.core_coeff_ranks(conversion)
                ratio_value = (
                    as_number(ratio_options[ranks[0] - 1])
                    if ranks and 1 <= ranks[0] <= len(ratio_options)
                    else float(ratio_match.group(1))
                )
                coeff_value = (
                    as_number(coeff_options[ranks[1] - 1])
                    if len(ranks) >= 2 and 1 <= ranks[1] <= len(coeff_options)
                    else float(coeff_match.group(1))
                )
                proposed["core_rune"] = {
                    "level": str(self._profile_level(core_record) or 70),
                    "conversion": conversion,
                    # Build fields store selected values.  Older import code
                    # wrote the rank number, which is ambiguous for ratios
                    # such as rank 17 / value 18.
                    "ratio_rank": ratio_value,
                    "coeff_rank": coeff_value,
                    "enhance": self._profile_enhance(core_record),
                }
                record("源铸石板", "源铸", name, f"{conversion} {ratio_match.group(1)}% / {coeff_match.group(1)}")
            else:
                record("源铸石板", "源铸", name, note="源铸参数不完整")

        record(
            "称号", "称号", "TCP 称号字段尚未解码", norm(current.get("title")),
            "保留配装1原值", "preserved",
        )
        profile_cards = [
            row for row in profile.get("cards", [])
            if isinstance(row, (list, tuple)) and len(row) >= 3
        ]
        if profile_cards:
            card_names = self.store.card_names()
            mapped_cards: dict[str, str] = {}
            for row in profile_cards:
                detected_name = norm(row[1])
                detected_grade = norm(row[2])
                mapped_name = self._match_option(detected_name, card_names)
                if mapped_name and detected_grade in {"L", "S", "A", "B", "C"}:
                    mapped_cards[mapped_name] = detected_grade
                    record(
                        "卡片", norm(row[0]), detected_name,
                        f"{mapped_name} {detected_grade}",
                    )
                else:
                    record(
                        "卡片", norm(row[0]), detected_name,
                        note="卡片名称或品质未进入当前卡片库",
                    )
            proposed["card_grades"] = [
                {
                    "name": name,
                    "grade": mapped_cards.get(name, NONE_TEXT),
                }
                for name in card_names
            ]
        else:
            record(
                "卡片", "卡片页", "本次 TCP 未提供", "保留原卡片配置",
                "查看信息正文没有卡片记录", "preserved",
            )
        observed_card_mastery: dict[str, int] = {}
        card_mastery_details: list[dict[str, Any]] = []
        for raw_field in profile.get("fields", []):
            if not isinstance(raw_field, (list, tuple)) or len(raw_field) < 2:
                continue
            field_name = norm(raw_field[0])
            if not field_name.startswith("卡片掌握|"):
                continue
            mastery_name = field_name.split("|", 1)[1]
            attr = CARD_MASTERY_ATTR_MAP.get(mastery_name)
            if not attr:
                continue
            level = max(0, min(25, int(as_number(raw_field[1]))))
            observed_card_mastery[attr] = level
            card_mastery_details.append({
                "name": mastery_name,
                "attr": attr,
                "level": level,
            })
            record(
                "卡片掌握", mastery_name, f"{level}级", f"{attr} {level}级",
            )
        if observed_card_mastery:
            card_bonus_levels = {
                attr: 0 if attr == "致命伤害" else int(as_number(
                    proposed.get("card_bonus_levels", {}).get(attr)
                    if isinstance(proposed.get("card_bonus_levels"), dict)
                    else 0
                ))
                for attr in CARD_PREVIEW_ATTRS
            }
            card_bonus_levels.update(observed_card_mastery)
            card_bonus_levels["致命伤害"] = 0
            proposed["card_bonus_levels"] = card_bonus_levels
            for mastery_name, attr in CARD_MASTERY_ATTR_MAP.items():
                if attr in observed_card_mastery:
                    continue
                preserved_level = int(as_number(card_bonus_levels.get(attr)))
                record(
                    "卡片掌握", mastery_name,
                    "档案已发送但等级尚未校准",
                    f"暂时保留 {attr} {preserved_level}级",
                    "该角色的加密档案已经定位；当前缺少游戏页面实测值，应用后请手动确认",
                    "unmatched",
                )
        else:
            record(
                "卡片掌握", "掌握之力", "本次登录未下发",
                note="保留当前数值，但必须手动确认；不会把旧角色数据当成本次识别结果",
                status="unmatched",
            )
        summaries = [
            {
                "category": category,
                "matched": mapped_counts.get(category, 0),
                "total": category_counts[category],
            }
            for category in category_counts
        ]
        return {
            "state": "ready",
            "profile": {
                "targetId": norm(profile.get("target_id")),
                "job": norm(profile.get("job")),
                "status": norm(profile.get("status")),
                "mappedItems": sum(mapped_counts.values()),
                "totalItems": sum(category_counts.values()),
                "cardMasteryCount": len(card_mastery_details),
            },
            "summary": summaries,
            "items": changes,
            "cardMastery": card_mastery_details,
            "build": proposed,
        }
    def _project_live_buff_row(self, source: dict[str, Any]) -> dict[str, Any] | None:
        if is_inferred_aura_row(source):
            return None
        skill_key = _display_skill_key(source.get("skill_name") or source.get("skill_id"))
        catalog = self._live_buff_catalog.get(skill_key)
        if catalog is None:
            return None
        # Display labels must not mutate the canonical packet interval ledger.
        row = dict(source)
        if catalog.get("display"):
            row["label"] = catalog["display"]
        row["scope"] = norm(catalog.get("scope")) or (
            "team" if "团队" in norm(source.get("status_kind")) else "personal"
        )
        if catalog.get("category"):
            row["status_kind"] = catalog["category"]
        return row
    @staticmethod
    def _joined_live_buff_field(
        rows: list[dict[str, Any]], field: str
    ) -> str:
        values: list[str] = []
        for row in rows:
            value = norm(row.get(field))
            if value and value not in values:
                values.append(value)
        return "、".join(values)
    @staticmethod
    def _prefer_finite_live_buff_rows(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Let the actual finite effect own one same-skill Buff's display time.

        The acceleration-resistance update emits two finite status records
        under the same skill id: the real valued effect (for example Spirit
        Boost 14 s or Time Acceleration 15 s) and a much longer, valueless
        resistance carrier whose duration follows the caster's cooldown.  The
        latter must not extend the visible skill countdown.  Keep valueless
        components that are no longer than the valued effect because those
        can still be legitimate same-duration animation/status companions.
        """

        finite_rows = [
            row
            for row in rows
            if not bool(row.get("open_ended"))
            and any(
                isinstance(interval, (list, tuple))
                and len(interval) >= 2
                and as_number(interval[1]) > as_number(interval[0]) + 1e-6
                for interval in row.get("intervals", [])
            )
        ]
        if not finite_rows:
            return rows

        valued_rows = [
            row for row in finite_rows if row.get("value") is not None
        ]
        if not valued_rows:
            return finite_rows

        def longest_interval(row: dict[str, Any]) -> float:
            return max(
                (
                    as_number(interval[1]) - as_number(interval[0])
                    for interval in row.get("intervals", [])
                    if isinstance(interval, (list, tuple))
                    and len(interval) >= 2
                ),
                default=0.0,
            )

        valued_duration = max(
            (longest_interval(row) for row in valued_rows),
            default=0.0,
        )
        if valued_duration <= 0.0:
            return finite_rows
        return [
            row
            for row in finite_rows
            if row.get("value") is not None
            or longest_interval(row) <= valued_duration + 0.25
        ]
    def _merged_live_buff_rows(
        self,
        sources: Any,
        *,
        elapsed_seconds: float,
        series: bool,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict):
                continue
            row = self._project_live_buff_row(source)
            if row is None:
                continue
            aura = is_aura_row(row)
            if aura and not is_aura_damage_series(row):
                continue  # The resistance companion cannot extend attack uptime.
            skill_key = _display_skill_key(row.get("skill_name") or row.get("skill_id"))
            grouped.setdefault((norm(row.get("target_id")), skill_key,
                                norm(row.get("source_id")) if aura else ""), []).append(row)

        merged_rows: list[dict[str, Any]] = []
        for rows in grouped.values():
            if not is_aura_row(rows[0]):
                rows = self._prefer_finite_live_buff_rows(rows)
            # Every nested collection replaced below is newly constructed;
            # retaining the untouched nested values does not mutate a source
            # row and avoids another recursive clone per displayed Buff.
            representative = dict(rows[0])
            intervals = _merge_display_intervals(
                [
                    [float(interval[0]), float(interval[1])]
                    for row in rows
                    for interval in row.get("intervals", [])
                    if isinstance(interval, (list, tuple)) and len(interval) >= 2
                ]
            )
            representative["intervals"] = intervals
            for field in ("source_id", "source_name", "job"):
                representative[field] = self._joined_live_buff_field(rows, field)
            values = {
                float(row["value"])
                for row in rows
                if row.get("value") is not None
            }
            representative["value"] = next(iter(values)) if len(values) == 1 else None
            if not series:
                starts = [float(row.get("start_seconds") or 0) for row in rows]
                ends = [float(row.get("end_seconds") or 0) for row in rows]
                representative["start_seconds"] = min(starts, default=0.0)
                representative["end_seconds"] = max(ends, default=0.0)
                representative["last_refresh_seconds"] = max(
                    (float(row.get("last_refresh_seconds") or 0) for row in rows),
                    default=0.0,
                )
                representative["refresh_count"] = max(
                    (int(row.get("refresh_count") or 0) for row in rows),
                    default=0,
                )
                representative["active"] = any(bool(row.get("active")) for row in rows)
                uptime = sum(end - start for start, end in intervals)
                representative["uptime_seconds"] = uptime
                representative["coverage"] = uptime / max(1e-9, elapsed_seconds)
            merged_rows.append(representative)
        return merged_rows
    def _with_live_display_projection(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        elapsed = max(0.0, float(payload.get("elapsed_seconds") or 0.0),
                      float(payload.get("boss_effect_current_seconds") or 0.0))
        payload["player_buff_series"] = self._merged_live_buff_rows(
            payload.get("player_buff_series"), elapsed_seconds=elapsed, series=True
        )
        payload["player_buffs"] = self._merged_live_buff_rows(
            payload.get("player_buffs"), elapsed_seconds=elapsed, series=False
        )
        payload["player_buff_events"] = [
            row
            for source in payload.get("player_buff_events", [])
            if isinstance(source, dict)
            if (row := self._project_live_buff_row(source)) is not None
        ]
        return payload
    def _with_profile_panels(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._with_live_display_projection(payload)
        profiles = payload.get("player_profiles", [])
        if not isinstance(profiles, list):
            payload["profile_panels"] = {}
            return payload
        party_jobs: dict[str, str] = {}
        for member in payload.get("party_members", []):
            if not isinstance(member, dict):
                continue
            actor_id = norm(member.get("actor_id")).upper()
            if actor_id and norm(member.get("job")):
                party_jobs[actor_id] = norm(member.get("job"))

        resolved_by_actor: dict[str, dict[str, Any]] = {}

        def profile_quality(profile: dict[str, Any]) -> tuple[int, int, int, int, int, int, int, int]:
            equipment = profile.get("equipment", [])
            cards = profile.get("cards", [])
            stats = profile.get("stats", [])
            fields = profile.get("fields", [])
            return (
                int(bool(equipment)),
                len(equipment),
                int(bool(cards)),
                len(cards),
                int(bool(norm(profile.get("job")))),
                len(stats),
                len(fields),
                int(as_number(profile.get("timestamp_ms"))),
            )

        for source in profiles:
            if not isinstance(source, dict):
                continue
            profile = copy.deepcopy(source)
            target_id = norm(profile.get("target_id"))
            actor_id = re.sub(
                r"^PROFILE:", "", target_id, flags=re.IGNORECASE
            ).upper()
            party_job = party_jobs.get(actor_id, "")
            profile_job = norm(profile.get("job"))
            if _player_job_stages().get(party_job, 0) > _player_job_stages().get(
                profile_job, 0
            ):
                profile["job"] = party_job
            identity = actor_id or target_id.upper()
            previous = resolved_by_actor.get(identity)
            if previous is not None:
                richer, supplement = (
                    (profile, previous)
                    if profile_quality(profile) >= profile_quality(previous)
                    else (previous, profile)
                )
                profile = copy.deepcopy(richer)
                for key in (
                    "target_name", "job", "level", "party_position",
                    "request_fingerprint", "response_layout", "fields",
                    "equipment", "cards", "stats", "raw_payload_hex",
                ):
                    if not profile.get(key) and supplement.get(key):
                        profile[key] = copy.deepcopy(supplement[key])
                profile["timestamp_ms"] = max(
                    int(as_number(previous.get("timestamp_ms"))),
                    int(as_number(source.get("timestamp_ms"))),
                )
            if re.fullmatch(r"0X[0-9A-F]{8}", actor_id):
                profile["target_id"] = f"0x{actor_id[2:]}"
            resolved_by_actor[identity] = profile
        resolved_profiles = list(resolved_by_actor.values())
        payload["player_profiles"] = resolved_profiles

        context = self.get_build("build1")
        panels: dict[str, dict[str, Any]] = {}
        for profile in resolved_profiles:
            target_id = norm(profile.get("target_id"))
            if not target_id:
                continue
            try:
                panel = self.profile_panel_calculator.calculate(
                    profile,
                    context,
                    lambda base, current_profile: self._profile_to_build_preview(
                        current_profile, current_build=base
                    ),
                )
            except Exception as exc:
                panel = {
                    "state": "error",
                    "targetId": target_id,
                    "job": norm(profile.get("job")),
                    "message": f"总面板计算失败：{type(exc).__name__}: {exc}",
                }
            panel["passiveBuffs"] = self._profile_passive_buffs(
                norm(profile.get("job")), context
            )
            panels[target_id] = {
                key: copy.deepcopy(panel.get(key))
                for key in (
                    "state", "targetId", "job", "message", "rows",
                    "coverage", "actualItems", "jewelry", "engravings",
                    "setBonuses", "cardMastery", "cardEstimate", "passiveBuffs",
                    "accountDefaults", "calculation", "notes",
                )
                if key in panel
            }
        payload["profile_panels"] = panels
        return payload
    def _profile_sync_preview_from_profile(
        self,
        profile: dict[str, Any],
        *,
        source: str = "live_capture",
        cache_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Build one sync proposal from an already trusted own profile."""

        current = self.get_build("build1")
        panel = self.profile_panel_calculator.calculate(
            profile,
            current,
            lambda base, current_profile: self._profile_to_build_preview(
                current_profile, current_build=base
            ),
        )
        preview = copy.deepcopy(panel.get("mapping") or self._profile_to_build_preview(
            profile, current_build=current
        ))
        proposal = panel.get("confirmedBuild")
        if isinstance(proposal, dict):
            manual_adjustments = [
                copy.deepcopy(item)
                for item in preview.get("items", [])
                if isinstance(item, dict)
                and norm(item.get("status")) == "unmatched"
            ]
            if manual_adjustments:
                proposal["profile_sync_review"] = {
                    "source": "game_profile",
                    "timestampMs": int(as_number(profile.get("timestamp_ms"))),
                    "targetId": norm(profile.get("target_id")),
                    "job": norm(profile.get("job")),
                    "items": manual_adjustments,
                }
            else:
                # A successful later sync supersedes any unresolved list left
                # by an older profile import.
                proposal.pop("profile_sync_review", None)
            preview["build"] = copy.deepcopy(proposal)
            preview["panel"] = {
                "state": panel.get("state"),
                "rows": copy.deepcopy(panel.get("rows", [])),
                "notes": copy.deepcopy(panel.get("notes", [])),
            }
        preview["source"] = source
        if cache_age_seconds is not None:
            preview["cacheAgeSeconds"] = round(
                max(0.0, float(cache_age_seconds)), 3
            )
        self._profile_sync["proposal"] = copy.deepcopy(preview["build"])
        self._profile_sync["profileSignature"] = self._profile_signature(profile)
        return preview
    def begin_profile_sync(self) -> dict[str, Any]:
        cache_status_reader = getattr(
            self.live_capture, "self_profile_cache_status", None
        )
        if callable(cache_status_reader):
            cache_status = cache_status_reader()
            cached_profile = cache_status.get("profile")
            if (
                cache_status.get("state") == "ready"
                and isinstance(cached_profile, dict)
                and cached_profile.get("equipment")
            ):
                cache_age = float(as_number(cache_status.get("ageSeconds")))
                self._profile_sync = {
                    "baseline": set(),
                    "startedCapture": False,
                    "proposal": None,
                    "profileSignature": None,
                    "cachedProfile": copy.deepcopy(cached_profile),
                    "cachedProfileRevision": int(
                        as_number(cache_status.get("revision"))
                    ),
                    "cacheAgeSeconds": cache_age,
                }
                return self._profile_sync_preview_from_profile(
                    cached_profile,
                    source="self_profile_cache",
                    cache_age_seconds=cache_age,
                )
        state = self.live_capture.status()
        # A profile sync must own a clean capture session. Reusing an existing
        # real-time session can merge a newly selected character with the
        # previous character because Dragon Nest reuses the same local actor
        # ID after returning to character select.
        if state.get("state") in {"starting", "running"}:
            self.live_capture.stop()
        state = self.live_capture.start(0.0, discover_tcp=True)
        deadline = time.monotonic() + 8.0
        while state.get("state") == "starting" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = self.live_capture.status()
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        baseline = {
            self._profile_signature(row)
            for row in snapshot.get("player_profiles", [])
            if isinstance(row, dict)
        }
        started_capture = True
        self._profile_sync = {
            "baseline": baseline,
            "startedCapture": started_capture,
            "proposal": None,
            "profileSignature": None,
        }
        capture_ready = state.get("state") == "running"
        return {
            "state": "error" if state.get("state") == "error" else "listening",
            "message": (
                state.get("error") or state.get("status") or "网络监听启动失败"
                if state.get("state") == "error"
                else "监听已就绪，请返回游戏并进入要识别的角色"
                if capture_ready
                else "正在建立抓包监听；请停留在角色选择界面，显示监听中后再进入角色"
            ),
            "capture": {key: state.get(key) for key in ("state", "status", "error", "npcapInstalled")},
            "captureReady": capture_ready,
            "startedCapture": started_capture,
        }
    def profile_sync_preview(self) -> dict[str, Any]:
        if not self._profile_sync:
            return {"state": "idle", "message": "请先开始同步"}
        cached_profile = self._profile_sync.get("cachedProfile")
        if isinstance(cached_profile, dict) and cached_profile.get("equipment"):
            return self._profile_sync_preview_from_profile(
                cached_profile,
                source="self_profile_cache",
                cache_age_seconds=float(
                    as_number(self._profile_sync.get("cacheAgeSeconds"))
                ),
            )
        state = self.live_capture.status()
        if state.get("state") == "error":
            return {"state": "error", "message": state.get("error") or state.get("status")}
        if state.get("state") != "running":
            return {
                "state": "listening",
                "message": "正在建立抓包监听；请停留在角色选择界面，显示监听中后再进入角色",
                "captureState": state.get("state"),
                "captureStatus": state.get("status"),
                "captureReady": False,
            }
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        profiles = [row for row in snapshot.get("player_profiles", []) if isinstance(row, dict)]
        baseline = self._profile_sync.get("baseline", set())
        fresh = [row for row in profiles if self._profile_signature(row) not in baseline]
        own_markers = [
            row for row in fresh
            if self._profile_fields(row).get("响应类型") == "自身状态栏"
        ]
        candidates = [row for row in own_markers if row.get("equipment")]
        if not candidates and own_markers:
            marker_time = max(int(as_number(row.get("timestamp_ms"))) for row in own_markers)
            nearby = [
                row for row in fresh
                if row.get("equipment")
                and abs(int(as_number(row.get("timestamp_ms"))) - marker_time) <= 5000
            ]
            if len(nearby) == 1:
                candidates = nearby
        # Entering a character sends the local equipment archive without an
        # inspect request.  It is explicitly tagged as a passive profile by the
        # decoder; teammate profiles are request/response pairs and do not have
        # this tag.  The sync workflow owns a newly started capture session, so
        # a single fresh passive archive is the intended local character even
        # when no self-status request (and therefore no own marker) is present.
        passive_candidates = [
            row for row in fresh
            if row.get("equipment")
            and self._profile_fields(row).get("响应类型") == "被动角色档案"
            and str(row.get("request_fingerprint") or "").startswith(
                "passive-profile:"
            )
        ]
        if not candidates and not own_markers and len(passive_candidates) == 1:
            candidates = passive_candidates
        if not candidates:
            received = max(own_markers, key=lambda row: as_number(row.get("timestamp_ms")), default=None)
            passive_received = max(
                passive_candidates,
                key=lambda row: as_number(row.get("timestamp_ms")),
                default=None,
            )
            return {
                "state": "listening",
                "message": (
                    "已收到角色状态标记，但缺少本次登录的完整档案；请返回角色选择界面，"
                    "保持从游戏识别开启后重新进入角色"
                    if received
                    else "同时收到多个登录角色档案，无法安全确定本人；请返回角色选择界面后重新识别"
                    if len(passive_candidates) > 1
                    else "已收到登录档案，但档案中没有可同步装备；请返回角色选择界面后重新进入角色"
                    if passive_received
                    else "正在等待角色登录；请从角色选择界面进入要识别的角色"
                ),
                "captureState": state.get("state"),
                "captureStatus": state.get("status"),
            }
        profile = max(candidates, key=lambda row: as_number(row.get("timestamp_ms")))
        return self._profile_sync_preview_from_profile(profile)
    def apply_profile_sync(self) -> dict[str, Any]:
        proposal = self._profile_sync.get("proposal") if self._profile_sync else None
        if not isinstance(proposal, dict):
            raise RuntimeError("尚未取得可同步的自身角色档案")
        build = self.set_build("build1", proposal)
        started_capture = bool(self._profile_sync.get("startedCapture"))
        self._profile_sync = {}
        if started_capture:
            self.live_capture.stop()
        return {"state": "applied", "build": build}
    def cancel_profile_sync(self) -> dict[str, Any]:
        started_capture = bool(self._profile_sync.get("startedCapture")) if self._profile_sync else False
        self._profile_sync = {}
        if started_capture:
            self.live_capture.stop()
        return {"state": "cancelled"}


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeProfileSyncMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
