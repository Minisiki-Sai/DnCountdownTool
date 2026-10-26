"""技能号反查模板：学习、投票、目录兜底。

背景：实体表只由 88 字节怪物出场包填充，进本后才开统计时那个包已经过去，
于是血量/技能包全被丢掉。技能号来自怪物技能表、全局唯一，是中途唯一还活着
的身份线索。这里钉住三层行为：自学习、投票自愈、目录兜底不瞎猜。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.native import skill_identity


class SkillIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original_path = skill_identity._INDEX_PATH
        skill_identity._INDEX_PATH = Path(self._tmp.name) / "index.json"
        skill_identity.reset_cache()

    def tearDown(self) -> None:
        skill_identity._INDEX_PATH = self._original_path
        skill_identity.reset_cache()
        self._tmp.cleanup()

    def test_learn_then_resolve(self) -> None:
        self.assertEqual(skill_identity.resolve_skill_template(729067), 0)
        self.assertTrue(skill_identity.learn_skill_template(729067, 4150051))
        self.assertEqual(skill_identity.resolve_skill_template(729067), 4150051)

    def test_learn_ignores_invalid_values(self) -> None:
        self.assertFalse(skill_identity.learn_skill_template(0, 4150051))
        self.assertFalse(skill_identity.learn_skill_template(729067, 0))
        self.assertFalse(skill_identity.learn_skill_template("abc", 4150051))
        self.assertEqual(skill_identity.stats()["skills"], 0)

    def test_conflicting_pairs_vote_by_hits(self) -> None:
        for _ in range(3):
            skill_identity.learn_skill_template(34714, 503611)
        skill_identity.learn_skill_template(34714, 999999)
        self.assertEqual(skill_identity.resolve_skill_template(34714), 503611)
        # 少数派继续命中后会反超，索引能自愈
        for _ in range(3):
            skill_identity.learn_skill_template(34714, 999999)
        self.assertEqual(skill_identity.resolve_skill_template(34714), 999999)

    def test_save_roundtrip(self) -> None:
        skill_identity.learn_skill_template(34734, 503621)
        self.assertTrue(skill_identity.save(force=True))
        payload = json.loads(skill_identity._INDEX_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["skills"]["34734"], {"503621": 1})
        skill_identity.reset_cache()
        self.assertEqual(skill_identity.resolve_skill_template(34734), 503621)
        # 没有新数据时不重复写盘
        self.assertFalse(skill_identity.save(force=True))

    def test_vote_payload_keeps_top_entries(self) -> None:
        for template_id in range(1, 10):
            for _ in range(template_id):
                skill_identity.learn_skill_template(1000, template_id)
        votes = skill_identity.index_snapshot()[1000]
        self.assertLessEqual(len(votes), 8)
        self.assertEqual(max(votes, key=lambda key: votes[key]), 9)


class CatalogFallbackTest(unittest.TestCase):
    """目录兜底：只认唯一候选，宁缺勿猜。"""

    class _Template:
        def __init__(self, name: str, boss: bool) -> None:
            self.name = name
            self.boss = boss

    def _templates(self, *rows):
        return {
            template_id: self._Template(name, boss)
            for template_id, name, boss in rows
        }

    def test_catalog_token_for_boss_skills(self) -> None:
        self.assertEqual(skill_identity.catalog_token(34714), "食人魔")
        self.assertEqual(skill_identity.catalog_token(34848), "红龙")
        # "硬核·xxx" 只表示难度，不指名 Boss
        self.assertEqual(skill_identity.catalog_token(735001), "")
        # 目录里没有的技能号（沙龙那批 729xxx）
        self.assertEqual(skill_identity.catalog_token(729067), "")

    def test_picks_unique_boss(self) -> None:
        templates = self._templates(
            (503611, "百夫长食人魔巴欧帕", True),
            (503617, "兽人", False),
        )
        self.assertEqual(
            skill_identity.catalog_template(34714, templates, {503611: 1}),
            503611,
        )

    def test_prefers_boss_marker_among_parts(self) -> None:
        templates = self._templates(
            (503681, "红龙佩达", False),
            (503682, "红龙佩达", False),
            (503696, "红龙佩达", True),
        )
        nests = {503681: 1, 503682: 1, 503696: 1}
        self.assertEqual(skill_identity.catalog_template(34848, templates, nests), 503696)

    def test_refuses_ambiguous_without_boss_flag(self) -> None:
        templates = self._templates(
            (1, "某某龙甲", False),
            (2, "某某龙乙", False),
        )
        self.assertEqual(skill_identity.catalog_template(34848, templates, {1: 1, 2: 1}), 0)

    def test_ignores_templates_outside_nests(self) -> None:
        templates = self._templates(
            (503611, "百夫长食人魔巴欧帕", True),
            (9001, "食人魔的召唤物", False),
        )
        self.assertEqual(
            skill_identity.catalog_template(34714, templates, {503611: 1}),
            503611,
        )

    def test_real_profiles_cover_black_dragon_stages(self) -> None:
        """真实模板表 + 副本配置下，黑龙经典五关都能落到唯一模板。"""

        import json as _json

        data = Path(__file__).resolve().parents[1] / "engine" / "data"
        raw = _json.loads(
            (data / "profiles" / "monster_templates.json").read_text("utf-8")
        )["templates"]
        from engine.native.nest_inference import template_nest_index

        templates = {
            int(key): self._Template(
                str(value.get("name") or ""), bool(value.get("boss"))
            )
            for key, value in raw.items()
        }
        nests = template_nest_index()
        expected = {
            34714: 503611,  # 食人魔
            34734: 503621,  # 翁巴拉
            34771: 503652,  # 梅鲁卡
            34798: 503661,  # 提塔尼安
            34848: 503696,  # 红龙
        }
        for skill, template_id in expected.items():
            self.assertEqual(
                skill_identity.catalog_template(skill, templates, nests),
                template_id,
                f"技能 {skill} 应落到模板 {template_id}",
            )


if __name__ == "__main__":
    unittest.main()
