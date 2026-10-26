"""播报白名单（focus）：本机主目标 + 场景 Boss。

核心变更：原来的「分边黑名单」要求先算出「另一侧是谁」，再剔除；现在只定
「本机在打谁」，其余自动不播。这样 Boss 死亡但服务器没补血量归零包时，它
只是「不在名单里」，不需要知道它死了。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.live.voice_signals import VoiceSignalFeed  # noqa: E402
from engine.native.overlay_service import (  # noqa: E402
    focus_boss_hint,
    focus_target_of,
)
from engine.native.nest_inference import (  # noqa: E402
    nest_boss_templates,
    scene_boss_templates,
)

SEA = "nightmare_sea_dragon"
SAND = "sand_dragon_hardcore"
DOG = "0x000000DB@4237023"        # P2 地狱犬
AIR_SEA = "0x000000AA@4237034"    # P2 空中海龙（场景 Boss，选不中）
FINAL_SEA = "0x000000E1@4237035"  # 终局海龙
APOCALYPSE = "0x000000C1@4237011"  # 天启
SCORPION = "0x000000C2@4237023"
SHADOW = "0x000000E9@4237025"     # 终局暗影/召唤物，不是 Boss
ZHUWU = "0x00000059@4150058"
KAJIFU = "0x0000005A@4150059"
SELF = "0x00000001"


def snapshot(
    *,
    mine: str = "",
    scores: dict | None = None,
    others: dict | None = None,
    ended: tuple = (),
    nest_attacks: dict | None = None,
) -> dict:
    attacks: dict = {}
    if mine:
        attacks[SELF] = {"targetId": mine, "timestampMs": 1_000_000}
    if others:
        attacks.update(others)
    data = {
        "selfSourceId": SELF,
        "recentAttackTargets": attacks,
        "lastAttackTargets": attacks,
        "endedBossIds": list(ended),
        "bossHealth": [],
    }
    if scores is not None:
        data["attackScores"] = {SELF: scores}
    if nest_attacks is not None:
        data["recentAttackTargets"] = nest_attacks
        data["lastAttackTargets"] = nest_attacks
    return data


class FocusTargetTest(unittest.TestCase):
    def test_dominant_beats_last_hit(self) -> None:
        """AOE 溅射到对侧一下，不能把主目标改掉。"""

        template, target = focus_target_of(
            snapshot(mine=KAJIFU, scores={ZHUWU: 900_000, KAJIFU: 1_000}),
            SELF,
            nest_boss_templates(SAND),
        )
        self.assertEqual(template, 4150058)
        self.assertEqual(target, ZHUWU)

    def test_tie_prefers_the_last_hit(self) -> None:
        template, _ = focus_target_of(
            snapshot(mine=KAJIFU, scores={ZHUWU: 500, KAJIFU: 500}),
            SELF,
            nest_boss_templates(SAND),
        )
        self.assertEqual(template, 4150059)

    def test_falls_back_to_last_hit_without_damage(self) -> None:
        template, target = focus_target_of(
            snapshot(mine=ZHUWU), SELF, nest_boss_templates(SAND)
        )
        self.assertEqual((template, target), (4150058, ZHUWU))

    def test_minion_is_never_the_focus(self) -> None:
        template, _ = focus_target_of(
            snapshot(mine=SHADOW), SELF, nest_boss_templates(SEA)
        )
        self.assertEqual(template, 0)

    def test_ended_target_is_ignored(self) -> None:
        template, _ = focus_target_of(
            snapshot(mine=ZHUWU, ended=(ZHUWU,)),
            SELF,
            nest_boss_templates(SAND),
        )
        self.assertEqual(template, 0)

    def test_peer_targets_are_not_used_as_fallback(self) -> None:
        """本机没目标时不许猜队友在打谁——猜错一侧会整段吞掉技能。"""

        template, _ = focus_target_of(
            snapshot(others={"0xP2": {"targetId": KAJIFU, "timestampMs": 999_000}}),
            SELF,
            nest_boss_templates(SAND),
        )
        self.assertEqual(template, 0)

    def test_empty_registered_set(self) -> None:
        self.assertEqual(focus_target_of(snapshot(mine=ZHUWU), SELF, ()), (0, ""))


class FocusHintTest(unittest.TestCase):
    def test_scene_boss_stays_in_whitelist(self) -> None:
        """P2：本机在地面打地狱犬，空中海龙照样要播冰眼/龙息。"""

        hint = focus_boss_hint(snapshot(mine=DOG), nest_key=SEA, self_source=SELF)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["current"], 4237023)
        self.assertEqual(hint["scene"], [4237034])
        self.assertIn(4237034, hint["focus"])
        self.assertNotIn(4237034, hint["others"])
        self.assertIn(4237011, hint["others"])  # 天启这一关不在场

    def test_final_phase_drops_apocalypse_and_scorpion(self) -> None:
        """终局：本机打海龙，天启/狮蝎不在 focus 里 → 不再显示「已好」。"""

        hint = focus_boss_hint(snapshot(mine=FINAL_SEA), nest_key=SEA, self_source=SELF)
        self.assertEqual(hint["current"], 4237035)
        self.assertIn(4237011, hint["others"])
        self.assertNotIn(4237035, hint["others"])

    def test_reports_nothing_when_nest_is_unknown(self) -> None:
        self.assertIsNone(focus_boss_hint(snapshot(mine="0x1@999999")))

    def test_reports_nothing_without_self_target(self) -> None:
        self.assertIsNone(focus_boss_hint(snapshot(), nest_key=SEA, self_source=SELF))

    def test_nest_is_inferred_from_attack_targets(self) -> None:
        """中途开统计：副本 key 还没定，先用攻击目标反推。"""

        hint = focus_boss_hint(snapshot(mine=ZHUWU), self_source=SELF)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["nestKey"], SAND)

    def test_hint_is_stable_across_repeated_calls(self) -> None:
        data = snapshot(mine=DOG)
        first = focus_boss_hint(data, nest_key=SEA, self_source=SELF)
        second = focus_boss_hint(data, nest_key=SEA, self_source=SELF)
        self.assertEqual(first, second)


class SceneTemplateConfigTest(unittest.TestCase):
    def test_sea_declares_air_dragon(self) -> None:
        self.assertEqual(set(scene_boss_templates(SEA)), {4237034})
        # 场景 Boss 同时也是登记 Boss（副本本身认得它）。
        self.assertIn(4237034, nest_boss_templates(SEA))

    def test_other_nests_have_no_scene_boss(self) -> None:
        for key in (
            "black_dragon_classic",
            "black_dragon_hardcore",
            "sand_dragon_classic",
            "sand_dragon_hardcore",
        ):
            self.assertEqual(set(scene_boss_templates(key)), set(), key)


class SceneBossRetireExemptionTest(unittest.TestCase):
    def feed(self) -> VoiceSignalFeed:
        feed = VoiceSignalFeed()
        # 空中海龙血量停在 0.9%，之后只有技能行维持活跃。
        feed._hp_seen[FINAL_SEA] = (900_000, 100_000_000, 1_000)
        feed._hp_seen[AIR_SEA] = (900_000, 100_000_000, 1_000)
        return feed

    def test_scene_boss_is_not_retired(self) -> None:
        feed = self.feed()
        later = 1_000 + 300_000
        retired = feed.retired_sources(later)
        self.assertIn(FINAL_SEA, retired)
        self.assertIn(AIR_SEA, retired)

    def test_exempt_scene_boss_is_kept(self) -> None:
        feed = self.feed()
        later = 1_000 + 300_000
        retired = feed.retired_sources(later, (4237034,))
        self.assertNotIn(AIR_SEA, retired)
        self.assertIn(FINAL_SEA, retired)


if __name__ == "__main__":
    unittest.main()
