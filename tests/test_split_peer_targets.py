"""分边副本「我在打哪只 Boss / 另一侧是谁」的数据链路。

轻量版不把伤害事件交给聚合器（不做 DPS），但分边识别必须有攻击目标：
``CombatEvent`` 只走一条记账旁路，不进 DPS。另一侧优先看血量行，没有血量
事件时退回队友的攻击目标，并用「打 A 的人和打 B 的人是否重叠」区分
「左右两队各打一只」与「同一关两只都要打」。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from engine.live.aggregator import CombatAggregator
from engine.native.overlay_service import (
    split_active_boss_templates,
    split_peer_boss_templates,
)

ZHUWU = "0x00000059@4150058"      # 暴风支配者朱武
KAJIFU = "0x0000005A@4150059"     # 忠实的崇拜者卡基夫
BANSHEE = "0x0000005B@4150054"    # 女妖皇后伊泰拉（同关双 Boss）
LORD = "0x0000005C@4150055"       # 消亡领主阿赫玛德（同关双 Boss）
MINION = "0x00000075@33101"       # 杂兵
DOG = "0x00000089@4237012"            # 梦魇·地狱犬卡卡里
SKY_DRAGON = "0x0000008A@4237034"     # 梦魇·海龙萨芬特拉（空中，打不到）
SEA_APOCALYPSE = "0x000000A3@4235447"   # 梦魇·审判者天启
SEA_SCORPION = "0x000000BE@4235448"     # 梦魇·狮蝎领主克雷姆


def snapshot(
    *,
    health: list[dict] | None = None,
    attacks: dict | None = None,
    by_template: dict | None = None,
    recent: dict | None = None,
) -> dict:
    return {
        "bossHealth": health or [],
        "lastAttackTargets": attacks or {},
        "attackTargetsByTemplate": by_template or {},
        # 短窗口版默认与累计版一致，便于老用例只写一份数据。
        "recentAttackers": by_template or {} if recent is None else recent,
    }


class ObserveAttackTargetTests(unittest.TestCase):
    def test_records_last_target_and_player_set(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", ZHUWU, 1_000)

        self.assertEqual(agg._overlay_last_attack_targets["p1"]["targetId"], ZHUWU)
        self.assertEqual(agg._overlay_last_attack_targets["p1"]["timestampMs"], 1_000)
        self.assertIn("p1", agg._overlay_attack_players["4150058"])

    def test_late_packet_does_not_rewind_last_target(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", ZHUWU, 5_000)
        agg.observe_attack_target("p1", KAJIFU, 3_000)

        self.assertEqual(agg._overlay_last_attack_targets["p1"]["targetId"], ZHUWU)

    def test_player_window_drops_stale_attackers(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", ZHUWU, 1_000)
        agg.observe_attack_target("p2", ZHUWU, 30_000)

        self.assertNotIn("p1", agg._overlay_attack_players["4150058"])
        self.assertIn("p2", agg._overlay_attack_players["4150058"])

    def test_snapshot_exposes_template_to_players(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("p1", ZHUWU, 1_000)
        agg.observe_attack_target("p2", KAJIFU, 1_100)
        payload = agg.overlay_snapshot(2_000, refresh_composition=False)

        self.assertEqual(payload["attackTargetsByTemplate"]["4150058"], ["p1"])
        self.assertEqual(payload["attackTargetsByTemplate"]["4150059"], ["p2"])

    def test_blank_ids_are_ignored(self) -> None:
        agg = CombatAggregator()
        agg.observe_attack_target("", ZHUWU, 1_000)
        agg.observe_attack_target("p1", "", 1_000)
        self.assertEqual(agg._overlay_last_attack_targets, {})
        self.assertEqual(agg._overlay_attack_players, {})


class SplitPeerBossTests(unittest.TestCase):
    def test_health_rows_win_when_present(self) -> None:
        health = [
            {"targetId": ZHUWU, "percent": 60, "updatedSeconds": 100},
            {"targetId": KAJIFU, "percent": 70, "updatedSeconds": 101},
        ]
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 100_000},
            "p2": {"targetId": KAJIFU, "timestampMs": 100_500},
        }
        peers = split_peer_boss_templates(
            snapshot(health=health, attacks=attacks), 4150058, "p1"
        )
        self.assertEqual(peers, [4150059])

    def test_companion_boss_without_attackers_is_not_the_other_side(self) -> None:
        # 梦魇海龙 P2 实测：地狱犬卡卡里（地面，全队在打）+ 海龙萨芬特拉
        # （空中，谁也打不到，只在两边同时有血量行）。空中那只不是「另一侧」，
        # 把它当对侧会过滤掉它刚放出来的冰眼/龙息。
        health = [
            {"targetId": DOG, "percent": 80, "updatedSeconds": 100},
            {"targetId": SKY_DRAGON, "percent": 100, "updatedSeconds": 100},
        ]
        attacks = {
            "p1": {"targetId": DOG, "timestampMs": 100_000},
            "p2": {"targetId": DOG, "timestampMs": 100_400},
        }
        peers = split_peer_boss_templates(
            snapshot(health=health, attacks=attacks), 4237012, "p1"
        )
        self.assertEqual(peers, [])

    def test_two_groups_each_on_one_boss_is_a_split(self) -> None:
        # 只有「两组人各打一只」才算分边：即使血量行里还有第三只同伴。
        health = [
            {"targetId": ZHUWU, "percent": 60, "updatedSeconds": 100},
            {"targetId": KAJIFU, "percent": 70, "updatedSeconds": 101},
            {"targetId": SKY_DRAGON, "percent": 100, "updatedSeconds": 100},
        ]
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 100_000},
            "p2": {"targetId": KAJIFU, "timestampMs": 100_500},
        }
        peers = split_peer_boss_templates(
            snapshot(health=health, attacks=attacks), 4150058, "p1"
        )
        self.assertEqual(peers, [4150059])

    def test_falls_back_to_party_attack_targets(self) -> None:
        # 轻量版没有血量事件：另一侧只能从「队友在打谁」推出来。
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 10_000},
            "p2": {"targetId": KAJIFU, "timestampMs": 10_500},
        }
        peers = split_peer_boss_templates(snapshot(attacks=attacks), 4150058, "p1")
        self.assertEqual(peers, [4150059])

    def test_same_players_on_both_bosses_means_not_a_split(self) -> None:
        # 同一关两只都要打（女妖 + 消亡领主）：两队的人两边都打，不能过滤。
        attacks = {
            "p1": {"targetId": BANSHEE, "timestampMs": 10_000},
            "p2": {"targetId": LORD, "timestampMs": 10_100},
        }
        overlaps = {"4150054": ["p1", "p2"], "4150055": ["p1", "p2"]}
        peers = split_peer_boss_templates(
            snapshot(attacks=attacks, by_template=overlaps), 4150054, "p1"
        )
        self.assertEqual(peers, [])

    def test_disjoint_players_keep_the_peer(self) -> None:
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 10_000},
            "p3": {"targetId": KAJIFU, "timestampMs": 10_100},
        }
        disjoint = {"4150058": ["p1", "p2"], "4150059": ["p3", "p4"]}
        peers = split_peer_boss_templates(
            snapshot(attacks=attacks, by_template=disjoint), 4150058, "p1"
        )
        self.assertEqual(peers, [4150059])

    def test_sea_dragon_split_when_aoe_only_grazes_the_peer(self) -> None:
        # 梦魇海龙实测：10 秒累计里我「两边都碰过」（AOE 蹭到天启），
        # 但 4 秒内只在打狮蝎 —— 仍应判定为分边，把天启过滤掉。
        attacks = {
            "p1": {"targetId": SEA_SCORPION, "timestampMs": 10_000},
            "p3": {"targetId": SEA_APOCALYPSE, "timestampMs": 10_100},
        }
        cumulative = {"4235447": ["p1", "p3"], "4235448": ["p1"]}
        recent = {"4235447": ["p3"], "4235448": ["p1"]}
        peers = split_peer_boss_templates(
            snapshot(attacks=attacks, by_template=cumulative, recent=recent),
            4235448,
            "p1",
        )
        self.assertEqual(peers, [4235447])

    def test_self_still_hitting_peer_means_not_a_split(self) -> None:
        # 反过来：4 秒内我自己也还在打对侧，就是同一关双 Boss，不能过滤。
        attacks = {
            "p1": {"targetId": SEA_SCORPION, "timestampMs": 10_000},
            "p3": {"targetId": SEA_APOCALYPSE, "timestampMs": 10_100},
        }
        recent = {"4235447": ["p1"], "4235448": ["p1"]}
        peers = split_peer_boss_templates(
            snapshot(attacks=attacks, by_template=recent, recent=recent),
            4235448,
            "p1",
        )
        self.assertEqual(peers, [])

    def test_minions_do_not_count_as_the_other_side(self) -> None:
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 10_000},
            "p2": {"targetId": MINION, "timestampMs": 10_100},
        }
        peers = split_peer_boss_templates(snapshot(attacks=attacks), 4150058, "p1")
        self.assertEqual(peers, [])

    def test_stale_party_targets_are_ignored(self) -> None:
        attacks = {
            "p1": {"targetId": ZHUWU, "timestampMs": 100_000},
            # 上一关残留：20 秒窗口之外。
            "p2": {"targetId": KAJIFU, "timestampMs": 10_000},
        }
        peers = split_peer_boss_templates(snapshot(attacks=attacks), 4150058, "p1")
        self.assertEqual(peers, [])

    def test_self_is_never_the_peer(self) -> None:
        attacks = {"p1": {"targetId": ZHUWU, "timestampMs": 10_000}}
        peers = split_peer_boss_templates(snapshot(attacks=attacks), 4150058, "p1")
        self.assertEqual(peers, [])

    def test_empty_snapshot_is_safe(self) -> None:
        self.assertEqual(split_peer_boss_templates({}, 4150058, "p1"), [])
        self.assertEqual(split_active_boss_templates([]), set())


class PublishPathTests(unittest.TestCase):
    def test_damage_events_only_feed_the_target_sink(self) -> None:
        source = Path(
            "engine/live/capture_sources.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _publish_events")
        block = source[start:start + 1_800]
        self.assertIn("isinstance(event, CombatEvent)", block)
        self.assertIn("sink(event.source_id, event.target_id, event.timestamp_ms)", block)
        # 命中之后必须 continue：伤害事件绝不能进 _decoded_events（它们不参与统计）。
        branch = block[block.index("isinstance(event, CombatEvent)"):]
        self.assertIn("continue", branch.split("should_publish")[0])

    def test_collector_wires_the_sink(self) -> None:
        source = Path("engine/live/collector.py").read_text(encoding="utf-8")
        self.assertIn("attack_target_sink=self.aggregator.observe_attack_target", source)


if __name__ == "__main__":
    unittest.main()
