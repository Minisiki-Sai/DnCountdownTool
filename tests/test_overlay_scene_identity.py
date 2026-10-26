"""悬浮窗第一行「副本 / 场景」身份回退的回归测试。

背景：第一行过去只读 ``state["nestName"]``，而它仅在战斗地图上赋值，
于是「在城里点开始统计」或「抓包晚于切图」时，即使包里已经读到
``神圣天堂``，界面仍然只能显示「未识别」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.core  # noqa: F401  (绑定原生解码依赖)
from engine.native.overlay_service import overlay_scene_identity


TOWN_ROW = {
    "map_id": 11,
    "map_name": "神圣天堂",
    "content_kind": "town",
    "map_type": 2,
    "observed_at": 25.7,
    "layout": "s2c-tcp-town-map-init-40-v576",
}
NEST_ROW = {
    "map_id": 14511,
    "map_name": "黑龙巢穴（一）",
    "nest_name": "黑龙巢穴（一）",
    "content_kind": "nest",
    "map_type": 3,
    "observed_at": 20.1,
    "layout": "s2c-tcp-map-load-28-v576",
}
HUB_RETURN_ROW = {
    "map_id": 0,
    "map_name": "庭院/天堂（场景返回）",
    "content_kind": "hub_return",
    "map_type": 2,
    "observed_at": 76.8,
    "layout": "battle+tcp-world-transfer+town-scene-load-v576",
}


class OverlaySceneIdentityTest(unittest.TestCase):
    def test_empty_state_stays_unknown(self) -> None:
        self.assertEqual(
            overlay_scene_identity({}),
            {"sceneName": "", "sceneKind": "unknown"},
        )

    def test_town_is_recognised_not_unknown(self) -> None:
        # 这是用户报的核心问题：抓到了「神圣天堂」，就不该显示未识别。
        self.assertEqual(
            overlay_scene_identity({"nestMapHistory": [TOWN_ROW]}),
            {"sceneName": "神圣天堂", "sceneKind": "hub"},
        )

    def test_battle_map_is_a_nest(self) -> None:
        self.assertEqual(
            overlay_scene_identity({"nestMapHistory": [NEST_ROW]}),
            {"sceneName": "黑龙巢穴（一）", "sceneKind": "nest"},
        )

    def test_latest_row_wins_after_returning_to_hub(self) -> None:
        # 打完回城后不能继续把上一场副本当成「当前所在」。
        state = {
            "nestName": "黑龙巢穴（一）",
            "nestMapHistory": [NEST_ROW, HUB_RETURN_ROW],
        }
        self.assertEqual(
            overlay_scene_identity(state),
            {"sceneName": "庭院/天堂（场景返回）", "sceneKind": "hub"},
        )

    def test_template_only_identity_is_used_when_history_is_empty(self) -> None:
        # 没有切图包、只靠 Boss 模板反推时仍然要能报出副本名。
        self.assertEqual(
            overlay_scene_identity(
                {"nestName": "黑龙巢穴（一）", "nestConfidence": "template"}
            ),
            {"sceneName": "黑龙巢穴（一）", "sceneKind": "nest"},
        )

    def test_history_without_observed_at_does_not_crash(self) -> None:
        row = {"map_name": "神圣天堂", "content_kind": "town"}
        self.assertEqual(
            overlay_scene_identity({"nestMapHistory": [row, {}]}),
            {"sceneName": "神圣天堂", "sceneKind": "hub"},
        )


if __name__ == "__main__":
    unittest.main()
