"""悬浮窗第二行「Boss:」的四级回退。

本次 bug：进黑龙巢穴（一）后 `bossHealth` 为空（这一轮没有任何 HP 事件），
但 Boss 确实吃了伤害、也有 `target_name`。只看血量行就会显示「Boss: 未识别」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.core  # noqa: F401  (绑定原生解码依赖)
from engine.native.overlay_service import (
    nest_uses_split_boss,
    overlay_boss_identity,
    target_is_boss_template,
)

OGRE = "百夫长食人魔巴欧帕"


class OverlayBossIdentityTest(unittest.TestCase):
    def test_health_row_wins(self) -> None:
        rows = [{"targetId": "0x36@503611", "name": OGRE, "partId": 0}]
        self.assertEqual(
            overlay_boss_identity({}, rows),
            {"name": OGRE, "targetId": "0x36@503611", "templateId": 503611},
        )

    def test_split_part_falls_back_to_parent_name(self) -> None:
        # 分部位行的 targetId 是父模板（4237023 梦魇·海龙萨芬特拉），
        # partId 才是 4150061/4150062。
        rows = [
            {"targetId": "0x8E@4237023", "name": "海龙 · 冰晶", "partId": 4150061}
        ]
        self.assertEqual(
            overlay_boss_identity({}, rows),
            {"name": "海龙", "targetId": "0x8E@4237023", "templateId": 4237023},
        )

    def test_no_hp_events_still_identifies_from_damage_targets(self) -> None:
        # 本次线上数据：bossHealth 空，但 boss_runtime_targets 有名字。
        snapshot = {
            "bossHealth": [],
            "boss_runtime_targets": [
                {"target_id": "0x36@503611", "target_name": OGRE, "damage": 95_000_000},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, []),
            {"name": OGRE, "targetId": "0x36@503611", "templateId": 503611},
        )

    def test_damage_targets_are_used_when_runtime_targets_are_empty(self) -> None:
        snapshot = {
            "boss_runtime_targets": [],
            "damage_targets": [
                {"target_id": "0x64@503064", "target_name": "沙漠龙扎卡德", "damage": 1},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, [])["name"], "沙漠龙扎卡德"
        )

    def test_unknown_template_is_not_used_as_a_cumulative_fallback(self) -> None:
        # 线上实测：换关时 33632（模板未收录的怪）成了最新的伤害目标，
        # 「Boss:」被写成「未识别实体 33632」。累计列表只认确认过的 Boss。
        snapshot = {
            "boss_runtime_targets": [
                {"target_id": "0x0000008F@33632", "target_name": "未识别实体 33632", "last_ms": 1_600_000},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, []),
            {"name": "", "targetId": "", "templateId": 0},
        )

    def test_action_events_are_the_last_resort(self) -> None:
        snapshot = {
            "boss_action_events": [
                {"source_id": "0x36@503611", "source_name": OGRE, "action_code": 53},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, []),
            {"name": OGRE, "targetId": "0x36@503611", "templateId": 503611},
        )

    def test_nothing_known_stays_empty(self) -> None:
        self.assertEqual(
            overlay_boss_identity({}, []),
            {"name": "", "targetId": "", "templateId": 0},
        )

    def test_target_without_template_suffix(self) -> None:
        # 分部位 / 非模板目标不能让解析抛异常。
        rows = [{"targetId": "0x36", "name": OGRE, "partId": 0}]
        self.assertEqual(
            overlay_boss_identity({}, rows),
            {"name": OGRE, "targetId": "0x36", "templateId": 0},
        )


MINION = {"targetId": "0x00000020@503614", "name": "红色军队小妖精战士", "partId": None}


class MinionIsNotBossTest(unittest.TestCase):
    """杂兵只要发过 HP 包也会进 bossHealth，第二行不能显示成杂兵名字。"""

    def test_minion_template_is_rejected(self) -> None:
        self.assertFalse(target_is_boss_template("0x00000020@503614"))
        self.assertTrue(target_is_boss_template("0x00000036@503611"))
        # 未收录的模板不排除。
        self.assertIsNone(target_is_boss_template("0x1@999999"))

    def test_real_boss_wins_even_when_minion_row_comes_first(self) -> None:
        snapshot = {
            "bossHealth": [
                MINION,
                {"targetId": "0x00000036@503611", "name": OGRE, "partId": None},
            ],
        }
        self.assertEqual(overlay_boss_identity(snapshot, [])["name"], OGRE)


    def test_only_minions_means_no_boss_yet(self) -> None:
        # 宁可显示「未识别」，也不能把杂兵当成 Boss 报出来。
        snapshot = {
            "bossHealth": [MINION],
            "boss_runtime_targets": [
                {"target_id": "0x00000020@503614", "target_name": "红色军队小妖精战士"},
            ],
            "damage_targets": [
                {"target_id": "0x00000002@579071", "target_name": "70级巢穴木桩"},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, []),
            {"name": "", "targetId": "", "templateId": 0},
        )

    def test_party_boss_rows_are_filtered_too(self) -> None:
        # party_boss_health 取的是「最后攻击目标」，打小怪时它返回的就是杂兵。
        rows = [MINION]
        snapshot = {
            "bossHealth": [MINION],
            "boss_runtime_targets": [
                {"target_id": "0x00000036@503611", "target_name": OGRE},
            ],
        }
        self.assertEqual(overlay_boss_identity(snapshot, rows)["name"], OGRE)

    def test_damage_targets_skip_minions(self) -> None:
        snapshot = {
            "boss_runtime_targets": [
                {"target_id": "0x00000020@503614", "target_name": "红色军队小妖精战士"},
            ],
            "damage_targets": [
                {"target_id": "0x00000036@503611", "target_name": OGRE},
            ],
        }
        self.assertEqual(overlay_boss_identity(snapshot, [])["name"], OGRE)


DRAGON_PHASE = {"targetId": "0x00000009@503681", "name": "红龙佩达", "partId": None}


class MultiStageBossTest(unittest.TestCase):
    """黑龙经典五关：模板表把红龙三阶段标成 boss=false，累计列表又永不过期。

    线上实测：t=1649~2015s 队伍在打红龙佩达，悬浮窗仍然是第一关的食人魔。
    """

    def test_dragon_phase_templates_count_as_boss(self) -> None:
        # 503681/503682/503683 在 monster_templates 里 boss=false，但它们是
        # boss_hp_profiles.json 里黑龙经典的正式阶段模板。
        for template in (503681, 503682, 503683, 503696):
            self.assertTrue(
                target_is_boss_template(f"0x00000009@{template}"), template
            )
        self.assertFalse(target_is_boss_template("0x00000020@503614"))

    def test_current_dragon_wins_over_first_stage_party_row(self) -> None:
        # party_boss_health 给的正是当前在打的红龙，旧逻辑因为模板标记把它丢了。
        self.assertEqual(
            overlay_boss_identity({}, [DRAGON_PHASE])["name"], "红龙佩达"
        )

    def test_ended_boss_does_not_win_the_fallback(self) -> None:
        snapshot = {
            "endedBossIds": ["0x00000038@503611"],
            "boss_runtime_targets": [
                {"target_id": "0x00000038@503611", "target_name": OGRE, "last_ms": 900_000},
                {"target_id": "0x00000009@503681", "target_name": "红龙佩达", "last_ms": 1_700_000},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, []),
            {"name": "红龙佩达", "targetId": "0x00000009@503681", "templateId": 503681},
        )

    def test_newest_row_wins_when_nothing_is_marked_ended(self) -> None:
        # 没收到死亡包时也不能按 id 顺序取第一个：按时间取最新。
        snapshot = {
            "damage_targets": [
                {"target_id": "0x00000038@503611", "target_name": OGRE, "last_ms": 700_000},
                {"target_id": "0x000000B2@503621", "target_name": "安加塔军团长翁巴拉", "last_ms": 850_000},
            ],
        }
        self.assertEqual(
            overlay_boss_identity(snapshot, [])["name"], "安加塔军团长翁巴拉"
        )

    def test_health_rows_prefer_the_freshest(self) -> None:
        rows = [
            {"targetId": "0x00000038@503611", "name": OGRE, "partId": None, "updatedSeconds": 700.0},
            DRAGON_PHASE | {"updatedSeconds": 1_700.0},
        ]
        self.assertEqual(overlay_boss_identity({}, rows)["name"], "红龙佩达")


class TwoSidedBossTest(unittest.TestCase):
    """沙龙硬核这类「分边」副本：左右各一只 Boss，队伍两边同时在打。

    旧逻辑按「谁的血量行更新」挑，两个 Boss 都在掉血就会 1~3 秒来回跳。
    分边副本改成以本机角色最后一次攻击的目标为准（``split_boss=True``）；
    单 Boss 副本不启用这一级，避免自己打小怪 / 停手时换掉正在推进的那只。
    """

    ZHUWU = {"targetId": "0x00000059@4150058", "name": "暴风支配者朱武", "partId": ""}
    KAJIFU = {"targetId": "0x0000005A@4150059", "name": "忠实的崇拜者卡基夫", "partId": ""}

    def snapshot(self, *, self_target: str | None, self_id: str = "0x4FABBCD3") -> dict:
        # aggregator 里 lastAttackTargets 的值是 {"targetId", "timestampMs"}，
        # 这里保持同样的形状，避免测试和线上结构不一致。
        return {
            "selfSourceId": self_id,
            "lastAttackTargets": {
                self_id: {"targetId": self_target or "", "timestampMs": 900_000},
                "0x11111111": {"targetId": "0x0000005A@4150059", "timestampMs": 899_000},
            },
            "bossHealth": [],
        }

    def identity(self, snapshot: dict, rows: list) -> dict:
        return overlay_boss_identity(snapshot, rows, split_boss=True)

    def test_self_attack_target_wins_even_if_the_other_boss_is_fresher(self) -> None:
        rows = [
            dict(self.KAJIFU, updatedSeconds=900.0, percent=80.0),
            dict(self.ZHUWU, updatedSeconds=800.0, percent=60.0),
        ]
        self.assertEqual(
            self.identity(self.snapshot(self_target="0x00000059@4150058"), rows),
            {"name": "暴风支配者朱武", "targetId": "0x00000059@4150058", "templateId": 4150058},
        )

    def test_boss_line_does_not_flip_between_two_frames(self) -> None:
        # 两帧里「更新」的那一只换成对面，本机打的没变 -> 结果必须一致。
        first = [
            dict(self.KAJIFU, updatedSeconds=900.0),
            dict(self.ZHUWU, updatedSeconds=800.0),
        ]
        second = [
            dict(self.KAJIFU, updatedSeconds=903.0),
            dict(self.ZHUWU, updatedSeconds=902.5),
        ]
        snapshot = self.snapshot(self_target="0x00000059@4150058")
        self.assertEqual(
            self.identity(snapshot, first)["targetId"],
            self.identity(snapshot, second)["targetId"],
        )

    def test_without_self_actor_falls_back_to_the_most_attacked_boss(self) -> None:
        snapshot = {
            "lastAttackTargets": {
                "0x11111111": {"targetId": "0x0000005A@4150059", "timestampMs": 1},
                "0x22222222": {"targetId": "0x0000005A@4150059", "timestampMs": 1},
                "0x33333333": {"targetId": "0x00000059@4150058", "timestampMs": 1},
            },
            "bossHealth": [],
        }
        rows = [dict(self.KAJIFU, updatedSeconds=900.0), dict(self.ZHUWU, updatedSeconds=800.0)]
        self.assertEqual(self.identity(snapshot, rows)["name"], "忠实的崇拜者卡基夫")

    def test_dead_self_target_leaves_the_row_empty(self) -> None:
        # 本机打的那只已经死了 —— 不再拿别的 Boss 顶上，按「未识别」留空。
        snapshot = self.snapshot(self_target="0x00000059@4150058")
        rows = [
            dict(self.ZHUWU, updatedSeconds=800.0, percent=0.0, currentHp=0),
            dict(self.KAJIFU, updatedSeconds=900.0, percent=70.0),
        ]
        self.assertEqual(self.identity(snapshot, rows)["name"], "")
        self.assertEqual(self.identity(snapshot, rows)["targetId"], "")

    def test_minion_self_target_leaves_the_row_empty(self) -> None:
        # 本机在打杂兵：这一行不显示队伍正在打的 Boss，直接留空。
        snapshot = self.snapshot(self_target="0x00000020@503614")
        rows = [dict(self.KAJIFU, updatedSeconds=900.0)]
        self.assertEqual(self.identity(snapshot, rows)["name"], "")


class SplitBossScopeTest(unittest.TestCase):
    """分边规则只对分边副本生效，别把单 Boss 副本也带偏。"""

    ZHUWU = {"targetId": "0x00000059@4150058", "name": "暴风支配者朱武", "partId": ""}
    KAJIFU = {"targetId": "0x0000005A@4150059", "name": "忠实的崇拜者卡基夫", "partId": ""}

    def test_only_split_nests_use_the_self_target(self) -> None:
        self.assertTrue(nest_uses_split_boss("black_dragon_hardcore", ""))
        self.assertTrue(nest_uses_split_boss("sand_dragon_hardcore", ""))
        self.assertTrue(nest_uses_split_boss("", "黑龙硬核"))
        self.assertTrue(nest_uses_split_boss("", "沙龙硬核"))
        # 梦魇海龙前半段是「审判者天启 / 狮蝎领主克雷姆」两队各打一只，
        # 抓包实测 24 个 20s 采样点人数互不重叠，按本机目标定位当前组。
        self.assertTrue(nest_uses_split_boss("nightmare_sea_dragon", ""))
        self.assertTrue(nest_uses_split_boss("", "梦魇海龙"))
        for key, label in (
            ("black_dragon_classic", "黑龙经典"),
            ("sand_dragon_classic", "沙龙经典"),
            ("", ""),
        ):
            self.assertFalse(nest_uses_split_boss(key, label), f"{key}/{label}")

    def test_self_target_wins_in_every_nest(self) -> None:
        # 播报与显示同源：不管是不是分边副本，「Boss:」都跟着本机打的那只，
        # 否则停手后这一行会跳到队友正在打的目标。
        snapshot = {
            "selfSourceId": "0x4FABBCD3",
            "lastAttackTargets": {
                "0x4FABBCD3": {"targetId": "0x00000059@4150058", "timestampMs": 900_000},
            },
            "bossHealth": [],
        }
        rows = [
            dict(self.KAJIFU, updatedSeconds=900.0, percent=80.0),
            dict(self.ZHUWU, updatedSeconds=800.0, percent=60.0),
        ]
        self.assertEqual(
            overlay_boss_identity(snapshot, rows, split_boss=True)["name"], "暴风支配者朱武"
        )
        self.assertEqual(
            overlay_boss_identity(snapshot, rows)["name"], "暴风支配者朱武"
        )


if __name__ == "__main__":
    unittest.main()
