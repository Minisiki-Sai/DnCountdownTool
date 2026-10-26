"""Cheap, table-driven offensive equipment baseline for every score recipient.

Only default score construction uses this preset. Explicit equipment overrides
are applied afterwards, including an intentional empty talisman/extra choice.
No search, timeline simulation or packet processing is performed here.
"""
from __future__ import annotations

from ..data_loader import TALISMAN_SLOTS, as_number, norm
import re


def automatic_s_extra(store, name, profile):
    """Attack plates forbid attack extras: use the job's source stat there."""
    allowed = store.glyph_s_extra_options(name)
    return next((value for value in (profile.get("attack_attr"), profile.get("main_stat"))
                 if value in allowed and value != "无"), "无")


def standard_score_talismans(store, profile):
    """Fixed B-grade slots, offensive families first, without duplicate families."""
    grade = next((g for g in ("B", "C") if g in store.talisman_grades()), "无")
    main = norm(profile.get("main_stat"))
    attacks = list(profile.get("attack_attrs") or [profile.get("attack_attr")])
    # The effective-panel main lane is physical for Paladins, but their skill
    # damage includes both lanes; don't drop their magical attack talisman.
    if profile.get("second_job") in {"圣骑士", "十字军"}:
        attacks = ["物攻", "魔攻"]
    wanted = list(dict.fromkeys([main, *attacks, "最终", "致命伤害", "致命",
                                "HP", "体质", "敏捷", "力量", "智力"]))
    families = {}
    for row in store.tables.get("talisman", []):
        if norm(row.get("类型")) == "主属性" and norm(row.get("品质")) == grade:
            families.setdefault(norm(row.get("属性")), []).append(row)
    extra_names = {norm(row.get("属性")): norm(row.get("额外属性"))
                   for row in store.tables.get("talisman", [])
                   if norm(row.get("类型")) == "额外属性" and norm(row.get("品质")) == grade}
    priorities = [*attacks, main]
    # Tables store extra display labels separately from the calculated keys.
    extra_priorities = [extra_names[attr] for attr in priorities if attr in extra_names]
    result = []
    selected = [attr for attr in wanted if attr in families][:len(TALISMAN_SLOTS)]
    for index, slot in enumerate(TALISMAN_SLOTS):
        if index >= len(selected):
            result.append(dict(slot=slot, grade="无", name="无", extra="无"))
            continue
        row = max(families[selected[index]], key=lambda r: as_number(r.get("数值")))
        name = norm(row.get("护符"))
        allowed = store.talisman_extra_options(grade, name)
        extra = next((value for value in extra_priorities if value in allowed), "无")
        result.append(dict(slot=slot, grade=grade, name=name, extra=extra))
    return result


def apply_standard_score_offense(store, build, profile):
    from ..shared import ENGRAVING_SLOTS, EQUIPMENT_SLOTS
    for item in build.get('equipment', []):
        item['enhance'] = 13
    fashion = {row['slot']: row for row in build.get('fashion', [])}
    for slot, name in {'精灵':'S精灵（+5）', '戒指1':'梦魇主宰的戒指（+10）', '戒指2':'梦魇主宰的戒指（+10）'}.items():
        if name not in store.fashion_options(slot):
            raise ValueError(f'评分默认装备缺少数据：{slot} / {name}')
        fashion[slot] = dict(slot=slot,name=name)
    build['fashion'] = list(fashion.values())
    extra = '光攻击%' if build.get('second_job') in {'圣骑士','十字军'} else f"{profile.get('attack_attr','物攻')}%"
    def number(text):
        values = re.findall(r'\d+(?:\.\d+)?', str(text))
        return float(values[-1]) if values else 0.0
    engravings = []
    for slot in ENGRAVING_SLOTS:
        kind = ('防具双防' if slot in EQUIPMENT_SLOTS[:5] else '武器双攻' if slot in {'主手','副手'}
                else '饰品生命' if slot in {'项链','耳环','戒指1','戒指2'} else '三件套致命')
        main = store.engraving_main_value_options(kind,'L')
        rank = max(main, key=number)
        secondary = store.engraving_secondary_value_options(kind,'L')
        index = main.index(rank)
        engravings.append(dict(slot=slot,grade='L',value_rank=rank,main_value=number(rank),
            secondary_value=number(secondary[index]) if index<len(secondary) else 0,
            extra=extra,extra_tier='高档',extra_value=store.engraving_extra_value(extra,'L','高档'),
            extra_rank=next((option for option in store.engraving_extra_rank_options(extra) if option.startswith('L高档')),'无')))
    build['engravings'] = engravings
    build["talismans"] = standard_score_talismans(store, profile)
    for row in build.get("glyph_s", []):
        row["extra"] = automatic_s_extra(store, row.get("name"), profile)
