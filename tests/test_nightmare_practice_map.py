"""试炼梦境·海龙萨芬特拉（活动副本）与梦魇龙巢共用一套播报/倒计时。

2026-10-29 客户端更新新增两张地图（maptable 762→764）：

* 17861 ``SeaDragonNest_Utmost_1_Practice``    第一关（试炼）
* 17862 ``SeaDragonNest_Utmost_Boss_Practice`` Boss 房（试炼）

打的内容和 17008/17009 完全一样，只是入口独立（stageentertable 里
``_PartyOneNumMin`` 从 5 改成 -1，可单人进）。所以这里不做第二条播报链，
而是让地图精确识别后退化成同一个副本名 ``梦魇海龙``，前端沿用原有那套
VOICE_RULES / SEA_MECHANICS / 悬浮窗倒计时，设置面板也还是「梦魇海龙」一组。

要钉住的四件事：
1. 新地图进 catalog，且是战斗地图；
2. ``enter_condition_table_id`` 指向 17008 —— 这是 ``detected_nest_key``
   的 ``map:<root>``，保持一致才能让换边、评分、场景链完全复用；
3. 官方名归一化成 ``梦魇海龙``（前端所有规则靠 ``含梦魇 + 含海龙`` 命中）；
4. 第一关独有的 4235453（福根亡灵）能反推出梦魇龙巢，避免「进本后才开
   统计」时认不出来。
"""

from __future__ import annotations

import unittest

from engine.live.dungeon_map_catalog import is_battle_map, resolve_dungeon_map_id
from engine.live.nest_recognizer import canonical_nest_name, resolve_nest_template

PRACTICE_MAPS = (17861, 17862)
PRACTICE_NAME = "试炼梦境·海龙萨芬特拉"
LIVE_MAPS = (17008, 17009)
LIVE_NAME = "梦魇·海龙萨芬特拉"
NEST_KEY = "nightmare_sea_dragon"
NEST_LABEL = "梦魇海龙"


class PracticeMapCatalogTests(unittest.TestCase):
    def test_practice_maps_are_battle_maps(self) -> None:
        for map_id in PRACTICE_MAPS:
            entry = resolve_dungeon_map_id(map_id)
            self.assertIsNotNone(entry, "%s 未收录进 dungeon_map_catalog" % map_id)
            assert entry is not None
            self.assertTrue(is_battle_map(entry), "%s 必须是战斗地图" % map_id)
            self.assertEqual(entry.content_kind, "nest")
            self.assertEqual(entry.official_name, PRACTICE_NAME)
            self.assertEqual(entry.display_name, PRACTICE_NAME)

    def test_practice_shares_the_live_dungeon_root(self) -> None:
        """root 决定 ``detected_nest_key = map:<root>``，必须和正式版一致。"""

        for map_id in (*LIVE_MAPS, *PRACTICE_MAPS):
            entry = resolve_dungeon_map_id(map_id)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(
                entry.enter_condition_table_id,
                17008,
                "%s 的 enter_condition_table_id 必须指回 17008" % map_id,
            )

    def test_practice_tool_names_and_grids(self) -> None:
        expected = {
            17861: ("SeaDragonNest_Utmost_1_Practice", "seadragonnest_utmost_1_practice"),
            17862: ("SeaDragonNest_Utmost_Boss_Practice", "seadragonnest_utmost_boss_practice"),
        }
        for map_id, (tool, grid) in expected.items():
            entry = resolve_dungeon_map_id(map_id)
            assert entry is not None
            self.assertEqual(entry.tool_names, (tool,))
            self.assertEqual(entry.grid_directories, (grid,))
            self.assertTrue(entry.grid_resource_found)

    def test_live_maps_unchanged(self) -> None:
        """老梦魇龙巢不能被动到。"""

        for map_id in LIVE_MAPS:
            entry = resolve_dungeon_map_id(map_id)
            assert entry is not None
            self.assertEqual(entry.official_name, LIVE_NAME)
            self.assertEqual(entry.enter_condition_table_id, 17008)


class PracticeNestNameTests(unittest.TestCase):
    def test_practice_name_collapses_to_the_shared_label(self) -> None:
        """前端靠「名字含梦魇 + 含海龙」选规则，必须归一化成同一个 label。"""

        self.assertEqual(canonical_nest_name(PRACTICE_NAME), NEST_LABEL)
        self.assertEqual(canonical_nest_name(LIVE_NAME), NEST_LABEL)
        # 归一化不能把别的副本卷进来
        self.assertNotEqual(canonical_nest_name("海龙巢穴外围"), NEST_LABEL)
        self.assertNotEqual(canonical_nest_name("海龙巢穴核心"), NEST_LABEL)

    def test_first_room_boss_infers_the_nest(self) -> None:
        """试炼/正式第一关只出福根亡灵时也要认得出（4235453 原本缺登记）。"""

        for template_id in (4235447, 4235448, 4235453):
            resolution = resolve_nest_template(template_id)
            self.assertIsNotNone(resolution, "模板 %s 未登记副本" % template_id)
            assert resolution is not None
            self.assertEqual(resolution.profile.key, NEST_KEY)
            self.assertEqual(resolution.profile.label, NEST_LABEL)


if __name__ == "__main__":
    unittest.main()
