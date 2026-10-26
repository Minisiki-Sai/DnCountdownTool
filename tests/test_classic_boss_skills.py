"""黑龙经典的技能号必须能在解码白名单里。

`decode_v576_boss_skill_starts` 的 `known_skill_ids` 就是
`boss_skills.json` 的 key；不在白名单里的技能号会被**丢弃在解码阶段**，
后面的 `publish_evidence` / 前端倒计时规则再好也收不到。

线上事故：白名单 34xxx 段原本只有 `34023、34849`，实测一轮黑龙经典 Boss 发了
69 个技能号、68 个被丢掉，于是「砸地/冲锋/踏击/陨石/旋风/炸弹/地震/追踪/飞行/重力」
全部没有倒计时，只有「爪」可能出。这里把前端 `NEST_COOLDOWN_RULES` 用到的
经典 id 固定下来，以后改白名单不会悄悄退回去。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CATALOG = (
    ROOT
    / "engine"
    / "data"
    / "profiles"
    / "boss_skills.json"
)

# frontend/src/utils/nestSkillCooldowns.ts 中 nest==='classic' 的全部 ids。
CLASSIC_COUNTDOWN_IDS = {
    34714: "食人魔 · 砸地",
    34715: "食人魔 · 冲锋",
    34734: "翁巴拉 · 踏击",
    34737: "翁巴拉 · 陨石",
    34771: "梅鲁卡 · 旋风",
    34798: "提塔尼安 · 炸弹",
    34801: "提塔尼安 · 地震",
    34848: "红龙 · 追击",
    34849: "点名",
    34850: "红龙 · 追踪",
    34852: "红龙 · 飞行",
    34853: "红龙 · 飞行",
    34854: "红龙 · 飞行",
    34856: "红龙 · 飞行",
    34857: "红龙 · 飞行",
    34858: "红龙 · 飞行",
    34859: "红龙 · 飞行",
    34860: "红龙 · 重力",
    34864: "红龙 · 重力",
    34879: "红龙 · 飞行",
    34880: "红龙 · 飞行",
}

# 只用来延长/打断已有计时器的号（不产生新倒计时），同样必须能解码。
CLASSIC_TIMER_HELPERS = {34739, 34773, 34780, 34781, 34876}


def load_skills() -> dict[str, dict[str, object]]:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    skills = payload.get("skills")
    assert isinstance(skills, dict)
    return skills


class ClassicBossSkillCatalogTest(unittest.TestCase):
    def test_every_countdown_rule_id_is_decodable(self) -> None:
        skills = load_skills()
        missing = sorted(
            skill_id for skill_id in CLASSIC_COUNTDOWN_IDS if str(skill_id) not in skills
        )
        self.assertEqual(missing, [], f"这些号会被解码白名单丢掉：{missing}")

    def test_timer_helpers_are_decodable(self) -> None:
        skills = load_skills()
        missing = sorted(
            skill_id for skill_id in CLASSIC_TIMER_HELPERS if str(skill_id) not in skills
        )
        self.assertEqual(missing, [], f"旋风/踏击的延长与打断号缺失：{missing}")

    def test_entries_keep_a_display_name(self) -> None:
        skills = load_skills()
        for skill_id, expected in CLASSIC_COUNTDOWN_IDS.items():
            row = skills.get(str(skill_id)) or {}
            name = str(row.get("name") or "").strip()
            self.assertTrue(name, f"{skill_id} 没有名字")
        # 已经人工核对过 internal_action 的条目不要被冲掉。
        self.assertEqual(skills["34849"]["internal_action"], "Attack07_Claw")
        self.assertEqual(skills["34715"]["name"], "食人魔 · 冲锋")

    def test_minion_and_unrelated_ids_stay_out(self) -> None:
        skills = load_skills()
        # 观测到但不属于任何 Boss 机制的号不要一股脑塞进来。
        for skill_id in (34712, 34713, 34716, 34730):
            self.assertNotIn(str(skill_id), skills)


if __name__ == "__main__":
    unittest.main()
