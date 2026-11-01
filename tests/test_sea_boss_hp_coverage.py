"""梦魇海龙的血量行覆盖范围 + 离场判定。

两个已复现的实战缺陷：

1. `health()` 原先只给 ``{4235447, 4237023, 4237012}`` 算血量百分比，狮蝎
   （4235448）虽然 audit 表里有 full_hp，却一条血量行都不发 —— 前端拿不到它
   的血量，死亡只能等服务端补 0 包。
2. `retired_sources()` 只看「血量贴地」。客户端在 Boss 死前就停止刷血时，最后
   一条可能停在中间血量（实测天启停在 19.96% 后再无任何包），两档都等不到，
   它放过的技能会一直挂在悬浮窗里。
"""
import unittest
from types import SimpleNamespace

from engine.live.voice_signals import VoiceSignalFeed

APOCALYPSE = 4235447  # 梦魇·审判者天启
MANTICORE = 4235448  # 梦魇·狮蝎领主克雷姆
SEA_DRAGON = 4237023  # 梦魇·海龙萨芬特拉
APOCALYPSE_HP = 900_000_000
MANTICORE_HP = 1_080_000_000


def hp_event(template: int, percent: float, at_ms: int, actor: str = "0x000000B0"):
    total = {APOCALYPSE: APOCALYPSE_HP, MANTICORE: MANTICORE_HP, SEA_DRAGON: 9_820_000_000}[template]
    return SimpleNamespace(
        part_id=0,
        target_max_hp=total,
        current_hp=int(total * percent / 100.0),
        target_id=f"{actor}@{template}",
        timestamp_ms=at_ms,
    )


def kinds(feed: VoiceSignalFeed) -> list[tuple[str, int]]:
    return [
        (row["kind"], row.get("threshold") or 0)
        for row in feed.snapshot()
        if row["kind"] in {"boss_hp_state", "boss_hp_threshold"}
    ]


class BossHpCoverageTest(unittest.TestCase):
    def test_scorpion_now_reports_hp_state(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(hp_event(MANTICORE, 99.9, 1_000))
        self.assertIn(("boss_hp_state", 0), kinds(feed))

    def test_scorpion_has_no_threshold_rows(self) -> None:
        """狮蝎没有阈值播报规则，不该套用海龙那张 13 档表。"""
        feed = VoiceSignalFeed()
        feed.health(hp_event(MANTICORE, 99.9, 1_000))
        feed.health(hp_event(MANTICORE, 60.0, 2_000))
        feed.health(hp_event(MANTICORE, 40.0, 3_000))
        self.assertEqual([row for row in kinds(feed) if row[0] == "boss_hp_threshold"], [])

    def test_apocalypse_keeps_its_thresholds(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(hp_event(APOCALYPSE, 99.9, 1_000))
        feed.health(hp_event(APOCALYPSE, 59.0, 2_000))
        self.assertIn(("boss_hp_threshold", 60), kinds(feed))

    def test_sea_dragon_keeps_its_thresholds(self) -> None:
        feed = VoiceSignalFeed()
        feed.health(hp_event(SEA_DRAGON, 99.9, 1_000))
        feed.health(hp_event(SEA_DRAGON, 65.0, 2_000))
        self.assertIn(("boss_hp_threshold", 66), kinds(feed))

    def test_unknown_template_still_ignored(self) -> None:
        """不在 audit 表里的模板仍然不发血量行（避免拿错分母）。"""
        feed = VoiceSignalFeed()
        feed.health(SimpleNamespace(
            part_id=0, target_max_hp=1_000_000, current_hp=500_000,
            target_id="0x000000CC@999999", timestamp_ms=1_000,
        ))
        self.assertEqual(kinds(feed), [])


class MiddleHealthRetireTest(unittest.TestCase):
    def feed(self, percent: float, at_ms: int = 1_000) -> VoiceSignalFeed:
        feed = VoiceSignalFeed()
        feed._hp_seen[f"0x0000009D@{APOCALYPSE}"] = (
            int(APOCALYPSE_HP * percent / 100.0), APOCALYPSE_HP, at_ms
        )
        return feed

    def test_middle_health_needs_the_full_window(self) -> None:
        feed = self.feed(19.96)
        # 100 秒还不够：打完一关后捡东西/走位不会超过 3 分钟。
        self.assertEqual(feed.retired_sources(1_000 + 100_000), [])

    def test_middle_health_retires_after_three_minutes(self) -> None:
        feed = self.feed(19.96)
        self.assertEqual(
            feed.retired_sources(1_000 + 181_000), [f"0x0000009D@{APOCALYPSE}"]
        )

    def test_skills_keep_the_boss_alive(self) -> None:
        """还在放技能的 Boss 不能被判离场（血量可以长时间不刷新）。"""
        feed = self.feed(19.96)
        feed.publish(dict(source=f"0x0000009D@{APOCALYPSE}", skill=50456,
                          timestamp=1_000 + 170_000, templateId=APOCALYPSE))
        self.assertEqual(feed.retired_sources(1_000 + 181_000), [])

    def test_near_death_still_retires_in_twenty_seconds(self) -> None:
        feed = self.feed(0.05)
        self.assertEqual(
            feed.retired_sources(1_000 + 21_000), [f"0x0000009D@{APOCALYPSE}"]
        )

    def test_untouched_side_boss_is_never_retired(self) -> None:
        """另一侧还没开打的 Boss 血量停在 100%：既不刷血也不放技能，不能判离场。"""
        feed = self.feed(100.0)
        self.assertEqual(feed.retired_sources(1_000 + 900_000), [])

    def test_over_half_health_is_never_retired(self) -> None:
        """门槛是「掉过半」：60% 停更 15 分钟也保持原语义（保守）。"""
        feed = self.feed(60.0)
        self.assertEqual(feed.retired_sources(1_000 + 900_000), [])


if __name__ == "__main__":
    unittest.main()
