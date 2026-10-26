"""双链路识别：A 进本绑定（主） + B 目标切换。

A 链路的数据是 Boss 出场包绑到的「本关在场 Boss」，不依赖本机出手，也不等
血量包 —— 实测里 Boss 出场到第一条血量行能差好几分钟。B 链路才是「我换目标了」
时的收窄手段，优先级更高。

这套测试锁四件事：
1. 进本还没出手时，focus 和悬浮窗 Boss 行都要有名字；
2. 分边副本两只同时在场时不许瞎聚焦（宁可多播）；
3. 一旦本机开打，B 立刻接管，只播他那一侧；
4. 换关以新 Boss 为准，旧 Boss 靠死亡/退场标记剔除，不残留。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from engine.native.overlay_service import (
    focus_boss_hint,
    overlay_boss_identity,
)

NEST = "black_dragon_classic"
# 黑龙经典（一关一关推进）与黑龙硬核（左右分边）这两类副本行为不同。
SPLIT_NEST = "black_dragon_hardcore"
# 黑龙经典登记在册的模板里挑两只：第 1 关食人魔、第 4 关惩罚者。
OGRE = 503611
PUNISHER = 503652
# 黑龙硬核的两侧。
SIDE_A = 4181001
SIDE_B = 4181007


def live(template_id: int, name: str, actor: int = 0x38) -> dict:
    return {
        "templateId": template_id,
        "actorId": f"0x{actor:08X}",
        "targetId": f"0x{actor:08X}@{template_id}",
        "name": name,
    }


def idle_snapshot(**overrides) -> dict:
    base = {
        "selfSourceId": "me",
        "elapsed_seconds": 30.0,
        "recentAttackTargets": {},
        "displayAttackTargets": {},
        "lastAttackTargets": {},
        "bossHealth": [],
        "damage_targets": [],
        "boss_runtime_targets": [],
        "endedBossIds": [],
    }
    base.update(overrides)
    return base


class EnterNestBindingTests(unittest.TestCase):
    def test_idle_on_entry_uses_the_bound_boss(self) -> None:
        # 刚进本、一次都没出手：靠出场绑定直接给出这一关的 Boss。
        hint = focus_boss_hint(
            idle_snapshot(),
            nest_key=NEST,
            live_bosses=[live(OGRE, "百夫长食人魔巴欧帕")],
        )
        self.assertIsNotNone(hint)
        self.assertEqual(hint["current"], OGRE)
        self.assertIn(OGRE, hint["focus"])
        self.assertNotIn(OGRE, hint["others"], "本关 Boss 不能被当成要过滤的那一侧")

    def test_idle_on_entry_fills_the_boss_row(self) -> None:
        # 同一份输入，悬浮窗第二行也要有名字，而不是「未识别」。
        identity = overlay_boss_identity(
            idle_snapshot(),
            [],
            live_bosses=[live(OGRE, "百夫长食人魔巴欧帕")],
        )
        self.assertEqual(identity["name"], "百夫长食人魔巴欧帕")
        self.assertEqual(identity["templateId"], OGRE)

    def test_no_bound_boss_keeps_the_old_no_filter_behaviour(self) -> None:
        # 还没绑上（没收到出场包）→ 不过滤，宁可多播。
        self.assertIsNone(
            focus_boss_hint(idle_snapshot(), nest_key=NEST, live_bosses=[])
        )
        self.assertEqual(
            overlay_boss_identity(idle_snapshot(), [], live_bosses=[])["name"], ""
        )


class MapKeyFallbackTests(unittest.TestCase):
    """场景识别给的是地图级 key（``map:14451``），进不了副本登记表。

    A 链路必须能从**已经绑定的 Boss 模板**反推副本，否则「进本即绑定」这条
    根本走不到——这是实测发现的第一个断点。
    """

    def test_map_level_key_is_resolved_from_the_bound_boss(self) -> None:
        hint = focus_boss_hint(
            idle_snapshot(),
            nest_key="map:14451",
            nest_label="黑龙经典",
            live_bosses=[live(OGRE, "百夫长食人魔巴欧帕")],
        )
        self.assertIsNotNone(hint)
        self.assertEqual(hint["nestKey"], NEST, "应当反推出副本 key，而不是停在地图级 key")
        self.assertEqual(hint["current"], OGRE)

    def test_unknown_template_keeps_falling_through(self) -> None:
        # 绑到的模板不在任何副本登记表里 → 仍然不过滤。
        self.assertIsNone(
            focus_boss_hint(
                idle_snapshot(),
                nest_key="map:14451",
                live_bosses=[live(999999, "未收录的怪")],
            )
        )


class SplitNestTests(unittest.TestCase):
    def test_split_nest_idle_does_not_focus_anything(self) -> None:
        # 分边副本两只同时在场、本机还没打 → 多播，不许猜一侧。
        both = [
            live(SIDE_A, "红龙佩达", actor=0x38),
            live(SIDE_B, "红龙佩达（幻影）", actor=0x75),
        ]
        self.assertIsNone(
            focus_boss_hint(idle_snapshot(), nest_key=SPLIT_NEST, live_bosses=both)
        )

    def test_split_nest_does_not_guess_the_boss_row_either(self) -> None:
        both = [
            live(SIDE_A, "红龙佩达", actor=0x38),
            live(SIDE_B, "红龙佩达（幻影）", actor=0x75),
        ]
        identity = overlay_boss_identity(
            idle_snapshot(), [], split_boss=True, live_bosses=both
        )
        self.assertEqual(identity["name"], "", "分边副本没出手时不该替玩家挑一侧")

    def test_single_boss_nest_keeps_the_whole_stage(self) -> None:
        # 一关一关的副本里，同一阶段可能是两只（红龙佩达 P1/P2 同名、
        # 终局本体 + 分身）。整组都要播，不能只挑一只把另一只过滤掉。
        both = [
            live(OGRE, "百夫长食人魔巴欧帕", actor=0x38),
            live(PUNISHER, "黑色戒指惩罚者梅鲁卡", actor=0x75),
        ]
        hint = focus_boss_hint(idle_snapshot(), nest_key=NEST, live_bosses=both)
        self.assertIsNotNone(hint)
        self.assertEqual(sorted(hint["focus"]), sorted([OGRE, PUNISHER]))
        self.assertNotIn(OGRE, hint["others"])
        self.assertNotIn(PUNISHER, hint["others"])

    def test_attacking_narrows_to_my_side(self) -> None:
        # 开打之后 B 接管：15 秒表指向哪只就只播哪只。
        snap = idle_snapshot(
            recentAttackTargets={
                "me": {"targetId": f"0x00000075@{PUNISHER}", "timestampMs": 29_000},
            },
            attackScores={"me": {f"0x00000075@{PUNISHER}": 9_000.0}},
        )
        hint = focus_boss_hint(
            snap,
            nest_key=NEST,
            live_bosses=[
                live(OGRE, "百夫长食人魔巴欧帕", actor=0x38),
                live(PUNISHER, "黑色戒指惩罚者梅鲁卡", actor=0x75),
            ],
        )
        self.assertIsNotNone(hint)
        self.assertEqual(hint["current"], PUNISHER)
        self.assertIn(OGRE, hint["others"], "没在打的那一侧应当被过滤")

    def test_attacking_beats_binding_even_when_only_one_is_bound(self) -> None:
        # 单 Boss 场景下 B 与 A 结果一致，但顺序必须是 B 先（不然换目标跟不上）。
        snap = idle_snapshot(
            recentAttackTargets={
                "me": {"targetId": f"0x00000075@{PUNISHER}", "timestampMs": 29_000},
            },
        )
        hint = focus_boss_hint(
            snap,
            nest_key=NEST,
            live_bosses=[live(OGRE, "百夫长食人魔巴欧帕")],
        )
        self.assertEqual(hint["current"], PUNISHER)


class StageSwitchTests(unittest.TestCase):
    def test_new_stage_wins_over_the_previous_one(self) -> None:
        # 换关：上一关已经判结束，在场的只有新 Boss → 直接切过去。
        identity = overlay_boss_identity(
            idle_snapshot(endedBossIds=[f"0x00000038@{OGRE}"]),
            [],
            live_bosses=[live(PUNISHER, "黑色戒指惩罚者梅鲁卡", actor=0x75)],
        )
        self.assertEqual(identity["templateId"], PUNISHER)
        self.assertEqual(identity["name"], "黑色戒指惩罚者梅鲁卡")

    def test_row_keeps_my_target_within_thirty_seconds(self) -> None:
        # 跑机制停手 20 秒：30 秒表还有值，这一行不能闪成未识别。
        snap = idle_snapshot(
            recentAttackTargets={},
            displayAttackTargets={
                "me": {"targetId": f"0x00000075@{PUNISHER}", "timestampMs": 20_000},
            },
            lastAttackTargets={
                "me": {"targetId": f"0x00000075@{PUNISHER}", "timestampMs": 20_000},
            },
            bossHealth=[
                {
                    "targetId": f"0x00000075@{PUNISHER}",
                    "name": "黑色戒指惩罚者梅鲁卡",
                    "percent": 62.0,
                    "partId": 0,
                }
            ],
        )
        identity = overlay_boss_identity(snap, [], live_bosses=[])
        self.assertEqual(identity["name"], "黑色戒指惩罚者梅鲁卡")


class LiveBossFilterTests(unittest.TestCase):
    """``_live_bosses`` 只认「出场过 + 没被判结束」的 Boss。"""

    def _capture(self, *, boss_actors, ended=(), retired=(), birth=None):
        from engine.native.live_capture_service import NativeLiveCapture

        entities = {
            actor: SimpleNamespace(template_id=template)
            for actor, template in boss_actors.items()
        }
        decoder = SimpleNamespace(
            monster_entities=entities,
            boss_actor_ids=set(boss_actors),
            _monster_birth_phase=birth or {actor: 1 for actor in boss_actors},
            voice_signal_feed=SimpleNamespace(
                retired_sources=lambda *_a, **_k: list(retired)
            ),
            _monster_display_context=lambda actor: (
                f"Boss-{entities[actor].template_id}",
                "",
            ),
        )
        collector = SimpleNamespace(
            source=SimpleNamespace(_decoder=decoder),
            aggregator=SimpleNamespace(
                _boss_hp_ledger=SimpleNamespace(ended_target_ids=lambda: list(ended))
            ),
        )
        host = NativeLiveCapture.__new__(NativeLiveCapture)
        host._state = {"state": "running"}
        host._active_effect_elapsed_seconds = 60.0
        host._active_effect_clock_monotonic = 0.0
        host._scene_templates_for = lambda _decoder: ()  # type: ignore[assignment]
        return host._live_bosses(collector)

    def test_returns_bound_boss_in_entry_order(self) -> None:
        rows = self._capture(boss_actors={0x38: OGRE, 0x75: PUNISHER})
        self.assertEqual([row["templateId"] for row in rows], [OGRE, PUNISHER])
        self.assertEqual(rows[0]["targetId"], f"0x00000038@{OGRE}")

    def test_keeps_only_the_latest_stage(self) -> None:
        # 旧关卡的 Boss 不会被从 boss_actor_ids 里删掉，靠阶段号挡住。
        rows = self._capture(
            boss_actors={0x38: OGRE, 0x75: PUNISHER},
            birth={0x38: 1, 0x75: 2},
        )
        self.assertEqual([row["templateId"] for row in rows], [PUNISHER])

    def test_ended_and_retired_are_dropped(self) -> None:
        rows = self._capture(
            boss_actors={0x38: OGRE, 0x75: PUNISHER},
            ended=[f"0x00000038@{OGRE}"],
            retired=[f"0x00000075@{PUNISHER}"],
        )
        self.assertEqual(rows, [])

    def test_empty_when_nothing_bound(self) -> None:
        self.assertEqual(self._capture(boss_actors={}), [])


if __name__ == "__main__":
    unittest.main()
