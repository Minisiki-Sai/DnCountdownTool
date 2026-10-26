"""进本后才开统计：用「本机正在打的 Boss」反推副本。

数据前提（也是这里要钉住的）：``boss_hp_profiles.json`` 里每个 Boss 模板
只归属一个副本，所以模板号能唯一确定副本；同名不同副本用不同模板，不会串。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from engine.native.nest_inference import (
    infer_nest_from_attack_targets,
    infer_nest_from_snapshot,
    infer_nest_from_target,
    template_id_of,
    template_nest_index,
)

ZHUWU = "0x00000059@4150058"        # 沙龙硬核 · 暴风支配者朱武
SAND_DRAGON = "0x0000005B@4150050"  # 沙龙硬核 · 沙漠龙扎卡德
OGRE = "0x00000038@503611"          # 黑龙经典 · 百夫长食人魔巴欧帕
RED_DRAGON_CLASSIC = "0x00000039@503696"   # 黑龙经典 · 红龙佩达
RED_DRAGON_HARDCORE = "0x0000003A@4181001"  # 黑龙硬核 · 天堂龙灾佩达
UNKNOWN = "0x00000075@33101"


class TemplateIndexTests(unittest.TestCase):
    def test_every_boss_template_belongs_to_exactly_one_nest(self) -> None:
        index = template_nest_index()
        self.assertGreaterEqual(len(index), 50, "Boss 模板表读不到或太少")
        keys = {entry["nestKey"] for entry in index.values()}
        self.assertIn("black_dragon_classic", keys)
        self.assertIn("sand_dragon_hardcore", keys)

    def test_same_name_different_nest_does_not_cross(self) -> None:
        classic = infer_nest_from_target(RED_DRAGON_CLASSIC)
        hardcore = infer_nest_from_target(RED_DRAGON_HARDCORE)
        self.assertEqual(classic["nestKey"], "black_dragon_classic")
        self.assertEqual(hardcore["nestKey"], "black_dragon_hardcore")
        self.assertNotEqual(classic["nestKey"], hardcore["nestKey"])

    def test_template_id_parsing(self) -> None:
        self.assertEqual(template_id_of("0x59@4150058"), 4150058)
        # 纯模板号也认（有些快照/测试只给数字）。
        self.assertEqual(template_id_of(4150058), 4150058)
        self.assertEqual(template_id_of(""), 0)
        self.assertEqual(template_id_of("0x59@abc"), 0)


class InferNestTests(unittest.TestCase):
    def test_infers_from_my_target(self) -> None:
        result = infer_nest_from_target(ZHUWU)
        self.assertEqual(result["nestKey"], "sand_dragon_hardcore")
        self.assertEqual(result["nestLabel"], "沙龙硬核")
        self.assertEqual(result["nestConfidence"], "inferred")
        self.assertEqual(result["nestTemplateId"], 4150058)

    def test_unknown_template_returns_none(self) -> None:
        self.assertIsNone(infer_nest_from_target(UNKNOWN))
        self.assertIsNone(infer_nest_from_target(""))

    def test_prefers_my_target_over_party_votes(self) -> None:
        attacks = {
            "me": {"targetId": OGRE, "timestampMs": 1_000},
            "p2": {"targetId": ZHUWU, "timestampMs": 1_000},
            "p3": {"targetId": ZHUWU, "timestampMs": 1_000},
        }
        self.assertEqual(
            infer_nest_from_attack_targets(attacks, "me")["nestKey"],
            "black_dragon_classic",
        )

    def test_falls_back_to_most_common_target(self) -> None:
        attacks = {
            "me": {"targetId": UNKNOWN, "timestampMs": 1_000},
            "p2": {"targetId": ZHUWU, "timestampMs": 1_000},
            "p3": {"targetId": ZHUWU, "timestampMs": 1_000},
            "p4": {"targetId": SAND_DRAGON, "timestampMs": 1_000},
        }
        self.assertEqual(
            infer_nest_from_attack_targets(attacks, "me")["nestKey"],
            "sand_dragon_hardcore",
        )

    def test_accepts_plain_string_targets(self) -> None:
        # 旧快照/测试里 lastAttackTargets 可能直接是字符串。
        self.assertEqual(
            infer_nest_from_attack_targets({"me": ZHUWU}, "me")["nestKey"],
            "sand_dragon_hardcore",
        )

    def test_snapshot_entry_point(self) -> None:
        snapshot = {
            "selfSourceId": "0x4FABBCD3",
            "lastAttackTargets": {"0x4FABBCD3": {"targetId": OGRE}},
        }
        self.assertEqual(
            infer_nest_from_snapshot(snapshot)["nestKey"],
            "black_dragon_classic",
        )
        self.assertIsNone(infer_nest_from_snapshot(None))
        self.assertIsNone(infer_nest_from_snapshot({}))

    def test_no_boss_target_means_no_guess(self) -> None:
        attacks = {"me": {"targetId": UNKNOWN, "timestampMs": 1_000}}
        self.assertIsNone(infer_nest_from_attack_targets(attacks, "me"))


class NameFallbackTests(unittest.TestCase):
    """targetId 没有 @模板 时按 Boss 名字反推（实测线上就是这种 id）。"""

    def test_infers_from_boss_name_without_template(self) -> None:
        # 「百夫长食人魔巴欧帕」只属于黑龙经典，名字唯一。
        result = infer_nest_from_target("0x00000036", "百夫长食人魔巴欧帕")
        self.assertEqual(result["nestKey"], "black_dragon_classic")
        self.assertEqual(result["nestConfidence"], "inferred")

    def test_name_with_spaces(self) -> None:
        self.assertEqual(
            infer_nest_from_target("0x1", "千夫长 提塔尼安")["nestKey"],
            "black_dragon_classic",
        )

    def test_names_shared_by_two_nests_are_not_guessed(self) -> None:
        # 沙龙经典/硬核都有「暴风支配者朱武」：名字兜底必须放弃，让模板号说话。
        self.assertIsNone(infer_nest_from_target("0x00000036", "暴风支配者朱武"))

    def test_unknown_name_returns_none(self) -> None:
        self.assertIsNone(infer_nest_from_target("0x1", "某个杂兵"))

    def test_attack_targets_uses_names_map(self) -> None:
        attacks = {"me": {"targetId": "0x00000036", "timestampMs": 1_000}}
        names = {"0x00000036": "百夫长食人魔巴欧帕"}
        self.assertEqual(
            infer_nest_from_attack_targets(attacks, "me", names)["nestKey"],
            "black_dragon_classic",
        )


class ServiceIntegrationTests(unittest.TestCase):
    """``_inferred_nest`` 的组装：只读聚合器里的攻击目标表。"""

    def _capture(self):
        from types import SimpleNamespace

        from engine.live.aggregator import CombatAggregator
        from engine.native.live_capture_service import NativeLiveCapture

        capture = object.__new__(NativeLiveCapture)
        capture._state = {}
        aggregator = CombatAggregator()
        fake = SimpleNamespace(
            aggregator=aggregator,
            source=SimpleNamespace(
                _decoder=SimpleNamespace(current_self_actor_id=int(ZHUWU.split("@")[0], 16))
            ),
        )
        return capture, aggregator, fake

    def test_builds_nest_state_from_my_target(self) -> None:
        capture, aggregator, fake = self._capture()
        aggregator.observe_attack_target("0x0000005A", ZHUWU, 10_000)
        result = capture._inferred_nest(fake)

        self.assertEqual(result["nestKey"], "sand_dragon_hardcore")
        self.assertEqual(result["nestLabel"], "沙龙硬核")
        self.assertEqual(result["nestConfidence"], "inferred")
        self.assertEqual(capture._state["nestInference"]["reason"], "matched")

    def test_reports_no_target_before_first_hit(self) -> None:
        capture, _aggregator, fake = self._capture()
        self.assertEqual(capture._inferred_nest(fake), {})
        self.assertEqual(capture._state["nestInference"]["reason"], "no-attack-target")

    def test_reports_unknown_target_when_only_minions(self) -> None:
        capture, aggregator, fake = self._capture()
        aggregator.observe_attack_target("0x0000005A", UNKNOWN, 10_000)
        self.assertEqual(capture._inferred_nest(fake), {})
        self.assertEqual(capture._state["nestInference"]["reason"], "unknown-target")


class WiringTests(unittest.TestCase):
    def test_capture_loop_uses_inference_when_scene_is_unknown(self) -> None:
        source = Path(
            "engine/native/live_capture_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self._inferred_nest(collector)", source)
        self.assertIn("def _inferred_nest", source)
        # 场景识别成城镇/未配置地图时也要能反推（否则永远轮不到推断）。
        self.assertIn("current_nest_key not in known_nest_keys()", source)
        # 推断值必须让位给真正的场景识别，否则会一直粘住。
        self.assertIn('self._state.get("nestConfidence")\n                            == "inferred"', source)

    def test_standby_probes_are_disabled(self) -> None:
        core = Path("engine/core.py").read_text(encoding="utf-8")
        self.assertIn("auto_default_enabled=False", core)
        self.assertIn("auto_start_monitor=False", core)

        service = Path(
            "engine/native/live_capture_service.py"
        ).read_text(encoding="utf-8")
        start = service.index("def _load_auto_enabled")
        block = service[start:start + 700]
        self.assertIn("return False", block, "旧设置文件里的 auto_capture_enabled 必须被忽略")


if __name__ == "__main__":
    unittest.main()
