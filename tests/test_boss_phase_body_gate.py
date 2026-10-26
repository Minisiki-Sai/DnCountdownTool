"""阶段体（boss=False）的技能证据不能被 `template.boss` 丢掉。

线上事故（V4.88）：黑龙巢穴（三）打红龙佩达 P1，悬浮窗有血量、但「小追」等机制
一条提示和倒计时都没有。抓包回放实测：`decode_v576_boss_skill_starts` 对 actor
`0x09@503681` 解出了 4 条 `34848`，全部在 `_boss_action_events` 里被
`if template is None or not template.boss: continue` 丢掉 —— 因为客户端怪物表里
`503681 红龙佩达` / `503682 红龙佩达` 都是 `boss=False`（只有终局 503696 是 True）。

同一批抓包里 `503652 黑色戒指惩罚者梅鲁卡` 是 `boss=True`，技能行正常产出，
所以「有的副本有提示、有的副本什么都没有」并不是前端规则的问题。

修法：Boss 判定统一走 `_is_boss_actor()`，以 `boss_actor_ids` 为准（它在绑定时
已经认了「固定血量档案登记过的名字」和副本 Boss 模板集合）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.live.profile import ClientProfile  # noqa: E402
from engine.live.sources import (  # noqa: E402
    _V576PacketEventDecoder,
)

DATA = ROOT / "engine" / "data"
PROFILE = DATA / "profiles" / "client_profile.json"
TEMPLATES = DATA / "profiles" / "monster_templates.json"

# 客户端怪物表里没标 boss、但确实是这场战斗主 Boss 的阶段体。
PHASE_BODIES = {503681: "红龙佩达 P1", 503682: "红龙佩达 P2"}
# 表里正常标了 boss 的对照。
REAL_BOSS = {503652: "黑色戒指惩罚者梅鲁卡"}


def make_decoder() -> _V576PacketEventDecoder:
    prof = ClientProfile.load(PROFILE)
    return _V576PacketEventDecoder(DATA, prof.known_players)


class BossPhaseBodyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = make_decoder()

    def test_client_table_really_leaves_phase_bodies_unflagged(self) -> None:
        """确认前提：这个坑是数据造成的，不是测试写错。"""
        rows = json.loads(TEMPLATES.read_text(encoding="utf-8-sig"))["templates"]
        for template_id in PHASE_BODIES:
            self.assertFalse(
                bool(rows[str(template_id)].get("boss", False)),
                f"{template_id} 已经被标成 boss 了，本测试的前提失效，可考虑简化修复",
            )
        for template_id in REAL_BOSS:
            self.assertTrue(bool(rows[str(template_id)].get("boss", False)))

    def test_bound_actor_counts_as_boss_without_template_flag(self) -> None:
        for template_id in PHASE_BODIES:
            template = self.decoder.monster_templates[template_id]
            self.assertFalse(template.boss)
            actor_id = 0x09
            self.decoder.monster_entities[actor_id] = template
            self.decoder.boss_actor_ids = {actor_id}
            self.assertTrue(
                self.decoder._is_boss_actor(actor_id),
                f"{template_id} 绑上之后仍然被判成非 Boss，技能证据会整段丢失",
            )
            self.decoder.boss_actor_ids = set()

    def test_unbound_non_boss_actor_is_not_a_boss(self) -> None:
        """没有绑定证据的杂兵不能因为放宽判定被当成 Boss。"""
        template = self.decoder.monster_templates[503686]  # 埃尔德龙之追随者堕落贤者
        self.decoder.monster_entities[0x0A] = template
        self.decoder.boss_actor_ids = set()
        self.assertFalse(self.decoder._is_boss_actor(0x0A))
        self.assertFalse(self.decoder._is_boss_actor(None))

    def test_display_context_labels_phase_body_as_boss(self) -> None:
        template = self.decoder.monster_templates[503681]
        self.decoder.monster_entities[0x09] = template
        self.decoder.boss_actor_ids = {0x09}
        name, job = self.decoder._monster_display_context(0x09)
        self.assertEqual(name, "红龙佩达")
        self.assertIn("Boss", job)

    def test_seed_path_binds_phase_body(self) -> None:
        """进本后才点开始统计时走的就是这条注入路径。"""
        self.decoder.seed_monster_entities({0x09: 503681})
        self.assertIn(0x09, self.decoder.boss_actor_ids)


class EventGateSourceTest(unittest.TestCase):
    """事件生成循环不许再用裸 `template.boss` 判 Boss。"""

    def test_status_decoder_uses_boss_actor_helper(self) -> None:
        source = (
            ROOT
            / "engine"
            / "live"
            / "wire"
            / "status_decoder.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("not template.boss", source)
        self.assertIn("_is_boss_actor", source)


if __name__ == "__main__":
    unittest.main()
