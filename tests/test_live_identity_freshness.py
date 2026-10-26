"""「当前目标」时效与 Boss 离场判定的回归用例。

对应三个实测问题（梦魇海龙抓包 capture_20261024/25_梦魇海龙*.dpscap）：

1. P2 里空中海龙刚放出来的冰眼/龙息被分边过滤吃掉 —— 因为已离场的上一关海龙
   还停在 ``lastAttackTargets`` / 血量行里，被当成「另一侧」。
2. 终局阶段悬浮窗仍显示天启/狮蝎「已好」—— 天启最后一条血量停在 0.017%，
   服务器不再发 0，死亡行永远等不到。
3. 换边后旧侧仍在提示 —— 倒计时条目没有随分组变化清理。
"""

from __future__ import annotations

import unittest

from engine.live.aggregator import CombatAggregator
from engine.live.voice_signals import VoiceSignalFeed
from engine.native.overlay_lifecycle import party_boss_health
from engine.native.overlay_service import (
    current_attack_targets,
    overlay_boss_identity,
    split_peer_boss_templates,
)


APOCALYPSE = "0x000000A2@4235447"   # 梦魇·审判者天启
SCORPION = "0x000000B7@4235448"     # 梦魇·狮蝎领主克雷姆
DOG = "0x00000089@4237012"          # 梦魇·地狱犬卡卡里
SKY_DRAGON = "0x0000008A@4237034"   # 空中海龙（打不到）
OLD_SEA_DRAGON = "0x0000002C@4237003"  # 上一阶段的海龙（725s 后离场）


class AttackTargetFreshnessTests(unittest.TestCase):
    def test_stale_target_is_dropped_from_current_view_only(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", OLD_SEA_DRAGON, 700_000)
        agg.observe_attack_target("p2", DOG, 1_010_000)
        snapshot = agg.overlay_snapshot(1_011_000, refresh_composition=False)

        # 永久表保住历史（名字反查要用），当前视图只留最近的。
        self.assertIn("p1", snapshot["lastAttackTargets"])
        self.assertNotIn("p1", snapshot["recentAttackTargets"])
        self.assertIn("p2", snapshot["recentAttackTargets"])

    def test_current_attack_targets_falls_back_for_old_snapshots(self) -> None:
        legacy = {"lastAttackTargets": {"p1": {"targetId": DOG, "timestampMs": 1}}}
        self.assertEqual(current_attack_targets(legacy), legacy["lastAttackTargets"])

    def test_stale_target_does_not_become_the_boss_line(self) -> None:
        # 上一阶段的海龙早就离场，但永久目标表还写着它；Boss 行不能因此停在它。
        agg = CombatAggregator()
        agg.observe_attack_target("p1", OLD_SEA_DRAGON, 600_000)
        snapshot = agg.overlay_snapshot(1_100_000, refresh_composition=False)
        self_source = "p1"
        snapshot["selfSourceId"] = self_source
        health = [
            {"targetId": DOG, "percent": 60.0, "updatedSeconds": 1099.0},
            {"targetId": SKY_DRAGON, "percent": 100.0, "updatedSeconds": 1099.0},
        ]
        snapshot["bossHealth"] = health
        identity = overlay_boss_identity(snapshot, health, split_boss=True)
        self.assertNotEqual(identity.get("templateId"), 4237003)

    def test_party_rows_keep_persisting_targets(self) -> None:
        # 队伍血条不能因为整队跑位 15 秒没输出就消失（保持永久表语义）。
        snapshot = {
            "lastAttackTargets": {"p1": {"targetId": DOG, "timestampMs": 1_000}},
            "recentAttackTargets": {},
            "bossHealth": [{"targetId": DOG, "percent": 50.0, "currentHp": 5, "maxHp": 10}],
        }
        self.assertEqual(len(party_boss_health(snapshot, {"p1"})), 1)

    def test_airborne_companion_is_not_a_split_side(self) -> None:
        # 地狱犬和空中海龙同场，但只有一组人（全队打狗）——不是分边。
        agg = CombatAggregator()
        agg.observe_attack_target("p1", DOG, 1_000_000)
        agg.observe_attack_target("p2", DOG, 1_000_400)
        snapshot = agg.overlay_snapshot(1_001_000, refresh_composition=False)
        snapshot["bossHealth"] = [
            {"targetId": DOG, "percent": 80.0, "updatedSeconds": 1000.5},
            {"targetId": SKY_DRAGON, "percent": 100.0, "updatedSeconds": 1000.6},
        ]
        self.assertEqual(split_peer_boss_templates(snapshot, 4237012, "p1"), [])

    def test_two_real_sides_still_split(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", APOCALYPSE, 1_000_000)
        agg.observe_attack_target("p2", SCORPION, 1_000_500)
        snapshot = agg.overlay_snapshot(1_001_000, refresh_composition=False)
        snapshot["bossHealth"] = [
            {"targetId": APOCALYPSE, "percent": 60.0, "updatedSeconds": 1000.5},
            {"targetId": SCORPION, "percent": 70.0, "updatedSeconds": 1000.6},
        ]
        self.assertEqual(split_peer_boss_templates(snapshot, 4235447, "p1"), [4235448])


class SplitRegistrationTests(unittest.TestCase):
    def test_add_or_summon_never_becomes_the_split_side(self) -> None:
        # 梦魇海龙终局：本机去打黑影 4237025（未登记 Boss），真正的 4237023
        # 还在被别人打。此时不能把 4237023 当「对侧」过滤掉——那会把冰捆/拉冰
        # 一起吞掉。
        agg = CombatAggregator()
        agg.observe_attack_target("p1", "0x000000E1@4237025", 1_000_000)
        agg.observe_attack_target("p2", "0x000000DB@4237023", 1_000_400)
        snapshot = agg.overlay_snapshot(1_001_000, refresh_composition=False)
        snapshot["bossHealth"] = [
            {"targetId": "0x000000DB@4237023", "percent": 40.0, "updatedSeconds": 1000.8},
        ]
        self.assertEqual(
            split_peer_boss_templates(
                snapshot, 4237025, "p1", nest_key="nightmare_sea_dragon"
            ),
            [],
        )
        # 但真正的两侧仍然要能分出来。
        self.assertEqual(
            split_peer_boss_templates(
                snapshot, 4237023, "p2", nest_key="nightmare_sea_dragon"
            ),
            [],
        )

    def test_registered_pair_splits_inside_the_same_nest(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", APOCALYPSE, 1_000_000)
        agg.observe_attack_target("p2", SCORPION, 1_000_500)
        snapshot = agg.overlay_snapshot(1_001_000, refresh_composition=False)
        snapshot["bossHealth"] = [
            {"targetId": APOCALYPSE, "percent": 60.0, "updatedSeconds": 1000.5},
            {"targetId": SCORPION, "percent": 70.0, "updatedSeconds": 1000.6},
        ]
        self.assertEqual(
            split_peer_boss_templates(
                snapshot, 4235447, "p1", nest_key="nightmare_sea_dragon"
            ),
            [4235448],
        )


class _Health:
    def __init__(self, target_id: str, current_hp: int, maximum: int, stamp_ms: int) -> None:
        self.target_id = target_id
        self.target_name = ""
        self.part_id = 0
        self.current_hp = current_hp
        self.target_max_hp = maximum
        self.timestamp_ms = stamp_ms


class RetiredBossTests(unittest.TestCase):
    def test_boss_stuck_at_near_zero_is_retired(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(_Health(APOCALYPSE, 156_428, 900_000_000, 560_000))
        self.assertEqual(feed.retired_sources(570_000), [])
        self.assertEqual(feed.retired_sources(600_000), [APOCALYPSE])

    def test_alive_boss_is_not_retired(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(_Health(DOG, 700_000_000, 1_000_000_000, 10_000))
        self.assertEqual(feed.retired_sources(600_000), [])

    def test_phase_end_boss_near_one_percent_is_retired_later(self) -> None:
        # P2 的空中海龙停在 0.9% 后再没有任何血量包：20 秒不够（0.5% 才算），
        # 三分钟静默后才判离场。
        feed = VoiceSignalFeed()
        feed.health(_Health(SKY_DRAGON, 6_400_000, 712_000_000, 1_000_000))
        self.assertEqual(feed.retired_sources(1_060_000), [])
        self.assertEqual(feed.retired_sources(1_190_000), [SKY_DRAGON])

    def test_near_zero_boss_still_casting_is_not_retired(self) -> None:
        # P2 的空中海龙血量停在 0.9%，但每 30 秒还在放冰眼/龙息：不能因为血量
        # 不刷新就判它离场，否则整段冰眼/龙息会被清掉。
        feed = VoiceSignalFeed()
        feed.health(_Health(SKY_DRAGON, 6_400_000, 712_000_000, 1_000_000))
        feed.publish({"skill": 325447, "source": SKY_DRAGON, "timestamp": 1_180_000,
                      "templateId": 4237034, "recipients": []})
        self.assertEqual(feed.retired_sources(1_240_000), [])
        # 技能也停下后再过 180 秒才判离场。
        self.assertEqual(feed.retired_sources(1_365_000), [SKY_DRAGON])

    def test_explicit_zero_is_retired_too(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(_Health(SCORPION, 0, 1_080_000_000, 10_000))
        self.assertEqual(feed.retired_sources(40_000), [SCORPION])


if __name__ == "__main__":
    unittest.main()
