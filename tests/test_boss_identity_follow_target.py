"""Boss 行跟随「我正在打的目标」——单 Boss 副本也要更新。

用户反馈：一关一关往前打，Boss 行永远停在第一关。原因是
``damage_targets`` 行没有血量字段，``is_dead()`` 把 ``percent()==0`` 当成
「已死」，把这些目标全部剔掉；同时排序用的 ``last_ms`` 只由伤害事件维护，
轻量版丢弃伤害事件后全是 0，``newest()`` 退化成「取列表第一个」。
"""

from __future__ import annotations

import unittest

from engine.native.overlay_service import (
    attack_target_is_current,
    overlay_boss_identity,
)

FIRE_BULL = "0x000000B2@503699"   # 未收录模板，模拟「火牛」这类新关卡 Boss
OGRE = "0x00000038@503611"        # 百夫长食人魔巴欧帕（第一关）
ZHUWU = "0x00000059@4150058"
KAJIFU = "0x0000005A@4150059"


def damage_row(target_id: str, name: str, last_ms: int = 0) -> dict:
    return {
        "target_id": target_id,
        "target_name": name,
        "damage": 1_000,
        "hits": 10,
        "share": 0.5,
        "target_job": "",
        "target_max_hp": 0,
        "first_ms": 0,
        "last_ms": last_ms,
    }


def snapshot(**overrides) -> dict:
    base = {
        "selfSourceId": "me",
        "elapsed_seconds": 200.0,
        "bossHealth": [],
        "lastAttackTargets": {},
        "attackTargetsByTemplate": {},
        "damage_targets": [],
        "boss_runtime_targets": [],
    }
    base.update(overrides)
    return base


class BossIdentityFollowsTargetTests(unittest.TestCase):
    def test_single_boss_nest_follows_my_current_target(self) -> None:
        # 我打到火牛：攻击目标是它，且 10 秒内还在打。
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 199_000}},
            attackTargetsByTemplate={"503699": ["me"]},
            damage_targets=[
                damage_row(OGRE, "百夫长食人魔巴欧帕", last_ms=520_000),
                damage_row(FIRE_BULL, "火焰牛头怪", last_ms=199_000),
            ],
        )
        identity = overlay_boss_identity(snap, [], split_boss=False)
        self.assertEqual(identity["name"], "火焰牛头怪")
        self.assertEqual(identity["targetId"], FIRE_BULL)

    def test_unlisted_template_still_follows(self) -> None:
        # 模板不在血表里（target_is_boss_template 返回 None）也要跟随，
        # 否则新副本的 Boss 永远顶不上来。
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 199_000}},
            attackTargetsByTemplate={"503699": ["me"]},
            damage_targets=[damage_row(FIRE_BULL, "火焰牛头怪", last_ms=199_000)],
        )
        self.assertEqual(overlay_boss_identity(snap, [], split_boss=False)["name"], "火焰牛头怪")

    def test_stopped_attacking_clears_the_row(self) -> None:
        # 本机停手且目标已经超出显示时效：留空（未识别），不再拿别的 Boss 顶上。
        snap = snapshot(
            recentAttackTargets={},
            displayAttackTargets={},
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 100_000}},
            attackTargetsByTemplate={"503699": []},
            damage_targets=[
                damage_row(OGRE, "百夫长食人魔巴欧帕", last_ms=520_000),
                damage_row(FIRE_BULL, "火焰牛头怪", last_ms=100_000),
            ],
        )
        identity = overlay_boss_identity(snap, [], split_boss=False)
        self.assertEqual(identity["name"], "")
        self.assertEqual(identity["targetId"], "")

    def test_display_target_keeps_the_row_within_thirty_seconds(self) -> None:
        # 30 秒时效表还有本机目标时，即使 15 秒的业务表已经过期，也继续显示
        # 自己打的那只 —— 读条/被控/跑机制不该让这一行闪成未识别。
        snap = snapshot(
            displayAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 199_000}},
            recentAttackTargets={},
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 199_000}},
            damage_targets=[damage_row(FIRE_BULL, "火焰牛头怪", last_ms=199_000)],
        )
        self.assertEqual(
            overlay_boss_identity(snap, [], split_boss=False)["name"], "火焰牛头怪"
        )

    def test_split_nest_keeps_following_even_without_freshness(self) -> None:
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": ZHUWU, "timestampMs": 100_000}},
            attackTargetsByTemplate={},
            damage_targets=[
                damage_row(ZHUWU, "暴风支配者朱武"),
                damage_row(KAJIFU, "忠实的崇拜者卡基夫"),
            ],
        )
        identity = overlay_boss_identity(snap, [], split_boss=True)
        self.assertEqual(identity["name"], "暴风支配者朱武")

    def test_latest_damage_target_wins_when_times_exist(self) -> None:
        # 关键回归：last_ms 全为 0 时 newest() 只能取第一个，Boss 行就卡在第一关。
        # 第 3 级只认血表里确认过的 Boss，所以这里用两只已收录的。
        snap = snapshot(
            lastAttackTargets={},
            damage_targets=[
                damage_row(ZHUWU, "暴风支配者朱武", last_ms=0),
                damage_row(KAJIFU, "忠实的崇拜者卡基夫", last_ms=199_000),
            ],
        )
        identity = overlay_boss_identity(snap, [], split_boss=False)
        self.assertEqual(identity["name"], "忠实的崇拜者卡基夫")

    def test_unlisted_template_keeps_the_row_until_it_expires(self) -> None:
        # 未收录模板（火牛这类）只要本机打过就一直占这一行，直到显示时效过期。
        # 以前停手后会跳回血表里确认过的 Boss——那正是「不打怪时显示别人」的来源。
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 100_000}},
            attackTargetsByTemplate={"503699": []},
            damage_targets=[
                damage_row(ZHUWU, "暴风支配者朱武", last_ms=199_000),
                damage_row(FIRE_BULL, "火焰牛头怪", last_ms=100_000),
            ],
        )
        self.assertEqual(
            overlay_boss_identity(snap, [], split_boss=False)["name"],
            "火焰牛头怪",
        )


class TargetNameFallbackTests(unittest.TestCase):
    """线上 targetId 有时没有 ``@模板``（实体模板未识别），名字只能靠 targetNames。"""

    def test_uses_recorded_target_name_without_template(self) -> None:
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": "0x00000036", "timestampMs": 199_000}},
            attackTargetsByTemplate={},
            targetNames={"0x00000036": "火焰牛头怪"},
            elapsed_seconds=200.0,
        )
        identity = overlay_boss_identity(snap, [], split_boss=False)
        self.assertEqual(identity["name"], "火焰牛头怪")
        self.assertEqual(identity["targetId"], "0x00000036")
        self.assertEqual(identity["templateId"], 0)

    def test_no_name_no_identity(self) -> None:
        snap = snapshot(
            lastAttackTargets={"me": {"targetId": "0x00000036", "timestampMs": 199_000}},
            attackTargetsByTemplate={},
            elapsed_seconds=200.0,
        )
        self.assertEqual(overlay_boss_identity(snap, [], split_boss=False)["name"], "")


class AttackTargetFreshnessTests(unittest.TestCase):
    def test_recent_attacker_is_current(self) -> None:
        snap = snapshot(attackTargetsByTemplate={"503699": ["me", "other"]})
        self.assertTrue(attack_target_is_current(snap, "me", FIRE_BULL))

    def test_absent_attacker_is_stale(self) -> None:
        snap = snapshot(attackTargetsByTemplate={"503699": ["other"]})
        self.assertFalse(attack_target_is_current(snap, "me", FIRE_BULL))

    def test_falls_back_to_timestamp_when_map_missing(self) -> None:
        snap = snapshot(
            elapsed_seconds=200.0,
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 195_000}},
        )
        self.assertTrue(attack_target_is_current(snap, "me", FIRE_BULL))
        snap["lastAttackTargets"]["me"]["timestampMs"] = 150_000
        self.assertFalse(attack_target_is_current(snap, "me", FIRE_BULL))

    def test_missing_snapshot_clock_is_not_current(self) -> None:
        snap = snapshot(
            elapsed_seconds=0,
            lastAttackTargets={"me": {"targetId": FIRE_BULL, "timestampMs": 1_000}},
        )
        self.assertFalse(attack_target_is_current(snap, "me", FIRE_BULL))


class TargetBoundsTests(unittest.TestCase):
    def test_attack_sink_updates_target_bounds(self) -> None:
        from engine.live.aggregator import CombatAggregator

        agg = CombatAggregator()
        agg.observe_attack_target("me", FIRE_BULL, 5_000)
        agg.observe_attack_target("me", FIRE_BULL, 9_000)
        self.assertEqual(agg._overlay_target_bounds[FIRE_BULL], [5_000, 9_000])
        agg.observe_attack_target("me", FIRE_BULL, 7_000)
        self.assertEqual(agg._overlay_target_bounds[FIRE_BULL], [5_000, 9_000])


if __name__ == "__main__":
    unittest.main()
