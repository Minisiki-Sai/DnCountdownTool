"""分边副本「只播/只显示当前分组」的后端事实层。

后端只提供事实：本机在打哪只 Boss（``current``）、同场另一侧还有谁（``others``）。
要不要过滤由前端两个开关各自决定（语音一个、悬浮窗倒计时一个），所以这里只测
「提示算得对」和「过滤不误伤」。

真实数据来自 ``capture_20261024_沙龙硬核``：320~360s 两名队员分别打
4150058（暴风支配者朱武）和 4150059（忠实的崇拜者卡基夫）。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.native.overlay_service import (  # noqa: E402
    NativeOverlayMixin,
    filter_current_group_rows,
    split_active_boss_templates,
)

ZHUWU = "0x00000059@4150058"
KAJIFU = "0x0000005A@4150059"
DRAGON = "0x0000005B@4150050"
SELF = "0x4FABBCD3"


def health(template_id: int, updated: float, *, percent: float = 50.0, part_id=0) -> dict:
    return {
        "targetId": f"0x00000059@{template_id}",
        "name": f"Boss {template_id}",
        "percent": percent,
        "currentHp": 1,
        "maxHp": 2,
        "updatedSeconds": updated,
        "partId": part_id,
    }


class ActiveTemplatesTest(unittest.TestCase):
    def test_only_fresh_rows_count_as_the_other_side(self) -> None:
        rows = [
            health(4150058, 360.0),
            health(4150059, 359.0),
            health(4150050, 318.0),  # 上一关残留，不该算「同时在场」
        ]
        self.assertEqual(split_active_boss_templates(rows), {4150058, 4150059})

    def test_part_rows_are_ignored(self) -> None:
        rows = [health(4150061, 100.0, part_id=4150061), health(4150058, 100.0)]
        self.assertEqual(split_active_boss_templates(rows), {4150058})

    def test_rows_without_template_suffix_are_ignored(self) -> None:
        rows = [{"targetId": "0x00000036", "updatedSeconds": 10.0}]
        self.assertEqual(split_active_boss_templates(rows), set())

    def test_empty_input(self) -> None:
        self.assertEqual(split_active_boss_templates([]), set())
        self.assertEqual(split_active_boss_templates(None), set())


class FilterRowsTest(unittest.TestCase):
    ROWS = [
        {"sequence": 1, "skill": 730021, "templateId": 4150059, "source": KAJIFU},
        {"sequence": 2, "skill": 730022, "templateId": 4150058, "source": ZHUWU},
        {"sequence": 3, "skill": 730025, "templateId": 4150050, "source": DRAGON},
        {"sequence": 4, "skill": 0, "templateId": 0, "source": "0x1"},
    ]

    def test_drops_the_other_side_only(self) -> None:
        kept = filter_current_group_rows(self.ROWS, {"current": 4150058, "others": [4150059]})
        self.assertEqual([row["sequence"] for row in kept], [2, 3, 4])

    def test_no_hint_returns_everything(self) -> None:
        self.assertEqual(len(filter_current_group_rows(self.ROWS, None)), 4)
        self.assertEqual(len(filter_current_group_rows(self.ROWS, {})), 4)
        self.assertEqual(
            len(filter_current_group_rows(self.ROWS, {"current": 0, "others": [4150059]})),
            4,
        )
        self.assertEqual(
            len(filter_current_group_rows(self.ROWS, {"current": 4150058, "others": []})),
            4,
        )

    def test_both_sides_drop_their_opposite(self) -> None:
        kept = filter_current_group_rows(self.ROWS, {"current": 4150059, "others": [4150058]})
        self.assertEqual([row["sequence"] for row in kept], [1, 3, 4])

    def test_source_list_is_not_mutated(self) -> None:
        rows = list(self.ROWS)
        filter_current_group_rows(rows, {"current": 4150058, "others": [4150059]})
        self.assertEqual(len(rows), 4)


class _FakeLiveCapture:
    """只实现 capture_voice_events 依赖的两个方法。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    def voice_status(self) -> dict:
        return {
            "state": "running",
            "sessionId": "s",
            "elapsedSeconds": 343.0,
            "clockRunning": True,
            "voiceEvents": [
                {"sequence": 1, "skill": 730021, "templateId": 4150059, "source": KAJIFU},
                {"sequence": 2, "skill": 730022, "templateId": 4150058, "source": ZHUWU},
            ],
        }

    def overlay_status(self) -> dict:
        return self._state


class FocusHintTest(unittest.TestCase):
    """黑名单（分边）已改成白名单（focus）：只播本机在打的那只 + 场景 Boss。"""

    def harness(self, state: dict):
        host = types.SimpleNamespace(live_capture=_FakeLiveCapture(state))
        host.capture_voice_events = NativeOverlayMixin.capture_voice_events.__get__(host)
        host._focus_hint = NativeOverlayMixin._focus_hint.__get__(host)
        return host

    def state(self, nest_key: str) -> dict:
        return {
            "nestKey": nest_key,
            "nestLabel": "",
            "overlaySnapshot": {
                "selfSourceId": SELF,
                "lastAttackTargets": {
                    SELF: {"targetId": ZHUWU, "timestampMs": 343000},
                    "0xOTHER": {"targetId": KAJIFU, "timestampMs": 342_800},
                },
                "recentAttackTargets": {
                    SELF: {"targetId": ZHUWU, "timestampMs": 343000},
                    "0xOTHER": {"targetId": KAJIFU, "timestampMs": 342_800},
                },
                "bossHealth": [
                    health(4150058, 342.9, percent=12.0),
                    health(4150059, 343.0, percent=18.0),
                    health(4150050, 318.0, percent=63.9),
                ],
            },
        }

    def test_reports_current_and_keeps_the_rest_out(self) -> None:
        payload = self.harness(self.state("sand_dragon_hardcore")).capture_voice_events()
        hint = payload["focusGroup"]
        self.assertEqual(hint["current"], 4150058)
        self.assertEqual(hint["focus"], [4150058])
        # 对侧进 others：白名单只列「我在打谁」，其余全在名单外。
        self.assertIn(4150059, hint["others"])
        # 事件流本身不过滤：过滤是前端的两个开关各自决定的。
        self.assertEqual(len(payload["voiceEvents"]), 2)
        # 旧字段名保留，值一致。
        self.assertEqual(payload["splitGroup"], hint)

    def test_dead_boss_is_excluded_without_needing_a_death_packet(self) -> None:
        """只剩一只血量行时仍然给提示：死掉的那只「不在 focus 里」就够了。"""

        state = self.state("sand_dragon_hardcore")
        state["overlaySnapshot"]["bossHealth"] = [health(4150058, 342.9)]
        hint = self.harness(state).capture_voice_events()["focusGroup"]
        self.assertEqual(hint["current"], 4150058)
        self.assertIn(4150059, hint["others"])

    def test_target_from_another_nest_reports_nothing(self) -> None:
        # 本机目标是沙龙模板，却报黑龙经典：登记集合对不上 → 放弃过滤。
        payload = self.harness(self.state("black_dragon_classic")).capture_voice_events()
        self.assertIsNone(payload["focusGroup"])

    def test_unknown_self_target_reports_nothing(self) -> None:
        state = self.state("sand_dragon_hardcore")
        state["overlaySnapshot"]["lastAttackTargets"] = {}
        state["overlaySnapshot"]["recentAttackTargets"] = {}
        payload = self.harness(state).capture_voice_events()
        self.assertIsNone(payload["focusGroup"])

    def test_scene_boss_is_always_in_focus(self) -> None:
        """P2 空中海龙：没人打得中它，也必须常驻白名单。"""

        state = self.state("nightmare_sea_dragon")
        dog = "0x000000DB@4237023"
        state["overlaySnapshot"]["lastAttackTargets"] = {
            SELF: {"targetId": dog, "timestampMs": 343000}
        }
        state["overlaySnapshot"]["recentAttackTargets"] = {
            SELF: {"targetId": dog, "timestampMs": 343000}
        }
        hint = self.harness(state).capture_voice_events()["focusGroup"]
        self.assertEqual(hint["current"], 4237023)
        self.assertEqual(hint["scene"], [4237034])
        self.assertIn(4237034, hint["focus"])
        self.assertNotIn(4237034, hint["others"])

    def test_hint_is_cached_for_one_second(self) -> None:
        host = self.harness(self.state("sand_dragon_hardcore"))
        first = host.capture_voice_events()["focusGroup"]
        host.live_capture._state = self.state("black_dragon_classic")
        # 1 秒内复用缓存，避免 150ms 轮询反复算身份。
        self.assertEqual(host.capture_voice_events()["focusGroup"], first)


if __name__ == "__main__":
    unittest.main()
