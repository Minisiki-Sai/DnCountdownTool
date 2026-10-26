"""空中的海龙：冰眼/龙息 必须在「正在打地狱犬」时也能播报。

梦魇海龙这一场有个特殊情况：海龙本体在天上飞，**玩家打不到它**，所以
`lastAttackTargets` / 血量行指的都是地面的地狱犬卡卡里（4237012）。
如果语音链路里加任何「技能来源必须是当前 Boss / 当前攻击目标」的过滤，
冰眼和龙息就会在整段地狱犬阶段消失。

所以这条链路的约定是：
- 解码阶段只按**技能号**放行（`boss_skills.json` 白名单）；
- `publish_evidence` 只看技能号 + 证据类型，不看模板是不是「当前 Boss」；
- 前端 `voiceRule` / `SEA_MECHANICS` 只按技能号 + 副本白名单匹配。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.live import voice_signals  # noqa: E402
from engine.live.voice_signals import (  # noqa: E402
    MECHANICS,
    VoiceSignalFeed,
    publish_evidence,
)

CATALOG = (
    ROOT / "engine" / "data" / "profiles" / "boss_skills.json"
)

# 空中海龙的两个机制 + 同组会一起出现的拉冰/冰捆。
SKY_SKILLS = {
    325447: "海龙 · 冰眼",
    325450: "萨芬特拉之威",
    325430: "右向冰弹",
    325431: "左向冰弹",
    325456: "海龙 · 冰捆",
}

# 海龙本体（含飞天阶段用的几种 AI 模板）与地面的地狱犬。
SEA_DRAGON_TEMPLATE = 4237023
CERBERUS_TEMPLATE = 4237012


class SkySkillCatalogTest(unittest.TestCase):
    def test_sky_skills_are_decodable(self) -> None:
        skills = json.loads(CATALOG.read_text(encoding="utf-8")).get("skills") or {}
        missing = sorted(s for s in SKY_SKILLS if str(s) not in skills)
        self.assertEqual(missing, [], f"这些号会被解码白名单丢掉：{missing}")

    def test_sky_skills_stay_in_the_mechanic_set(self) -> None:
        # MECHANICS 是 publish_evidence 里唯一不看模板的放行分支。
        self.assertTrue({325447, 325450} <= MECHANICS)


class SkyBossPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.feed = VoiceSignalFeed()
        self.host = types.SimpleNamespace(
            voice_signal_feed=self.feed, actor_jobs={}, probe=None
        )

    def item(self, skill: int, template: int, source: str) -> object:
        return types.SimpleNamespace(
            skill_index=skill,
            evidence_kind="protocol_action",
            template_id=template,
            source_id=source,
            timestamp_ms=1_000,
            affected_target_id="",
        )

    def published(self) -> list[int]:
        return [int(row["skill"]) for row in self.feed.snapshot() if row.get("skill")]

    def test_ice_eye_and_breath_publish_while_ground_boss_is_the_target(self) -> None:
        # 场景：队伍在打地狱犬，海龙在天上放冰眼 / 龙息。
        # 两个事件来自不同 actor，发布侧不能因为「不是正在打的那只」而丢弃。
        publish_evidence(
            self.host,
            [
                self.item(325447, SEA_DRAGON_TEMPLATE, "0x0000002A@4237023"),
                self.item(325450, SEA_DRAGON_TEMPLATE, "0x0000002A@4237023"),
            ],
        )
        self.assertEqual(sorted(self.published()), [325447, 325450])

    def test_sky_skills_publish_before_any_damage_to_that_actor(self) -> None:
        # 空中的海龙不会被攻击，所以它可能一条 HP 包都没有；
        # 发布链路不允许依赖「先见过这只 Boss 的血」。
        publish_evidence(
            self.host, [self.item(325447, SEA_DRAGON_TEMPLATE, "0x0000002E@4237023")]
        )
        self.assertEqual(self.published(), [325447])

    def test_unlisted_skill_is_still_dropped(self) -> None:
        # 反向保护：只有白名单/ACTIONS 里的号能过，避免把杂兵动作也播出去。
        publish_evidence(
            self.host, [self.item(34799, CERBERUS_TEMPLATE, "0x00000036@4237012")]
        )
        self.assertEqual(self.published(), [])


if __name__ == "__main__":
    unittest.main()
