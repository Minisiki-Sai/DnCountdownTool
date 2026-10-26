"""首页两个「总开关」的语义：只控显示，不控采集。

回归点是语音播报：它的数据来自解码器内部的 ``voice_signal_feed``，
一旦有人把开关重新接到 ``_publish_events`` 的丢弃逻辑上，关掉悬浮窗就会把
语音一起静音，而 UI 上完全看不出来。这里的测试把这条边界钉死。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from engine.live.capture_toggles import (
    CAPTURE_TOGGLE_LABELS,
    CaptureToggles,
    classify_event,
    should_publish,
)
from engine.live.models import BossActionEvent, BossStatusEvent


def status_event() -> BossStatusEvent:
    return BossStatusEvent(
        timestamp_ms=1_000,
        target_id="0x1",
        source_id="0x2",
        source_name="剑皇",
        job="剑皇",
        skill_id="9001",
        skill_name="勇气",
        effect_key="buff:courage",
        effect_label="勇气",
        value=10.0,
        duration_ms=60_000,
        status_kind="buff",
    )


def action_event() -> BossActionEvent:
    return BossActionEvent(
        timestamp_ms=1_000,
        source_id="0x59",
        source_name="暴风支配者朱武",
        job="Boss",
        skill_id="730022",
        skill_name="狮子·三连冲",
        template_id=4150058,
        action_code=1,
    )


class ShouldPublishTests(unittest.TestCase):
    def test_two_countdown_categories_are_kept(self) -> None:
        self.assertTrue(should_publish(status_event()))
        self.assertTrue(should_publish(action_event()))

    def test_other_event_families_are_dropped(self) -> None:
        self.assertFalse(should_publish(object()))
        self.assertFalse(should_publish({"kind": "damage"}))

    def test_classify_event_labels(self) -> None:
        self.assertEqual(classify_event(status_event()), "buffs")
        self.assertEqual(classify_event(action_event()), "boss_actions")
        self.assertIsNone(classify_event(object()))

    def test_switch_off_stops_publishing_that_category(self) -> None:
        """关闭总开关 = 停止该类事件的聚合与推送。"""

        toggles = CaptureToggles(buffs=False, boss_actions=True)
        self.assertFalse(should_publish(status_event(), toggles))
        self.assertTrue(should_publish(action_event(), toggles))

        toggles = CaptureToggles(buffs=True, boss_actions=False)
        self.assertTrue(should_publish(status_event(), toggles))
        self.assertFalse(should_publish(action_event(), toggles))

    def test_both_switches_off_drop_both_categories(self) -> None:
        toggles = CaptureToggles(buffs=False, boss_actions=False)
        self.assertFalse(should_publish(status_event(), toggles))
        self.assertFalse(should_publish(action_event(), toggles))
        # 但事件类别以外的照旧丢弃，与开关无关。
        self.assertFalse(should_publish(object(), toggles))

    def test_toggles_none_keeps_only_the_category_filter(self) -> None:
        """离线回放不传 toggles，行为等同「全部打开」。"""

        self.assertTrue(should_publish(status_event(), None))
        self.assertTrue(should_publish(action_event(), None))
        self.assertFalse(should_publish(object(), None))

    def test_voice_feed_is_not_gated_by_toggles(self) -> None:
        """语音走解码器内部的 voice_signal_feed，发布过滤碰不到它。

        ``voice_signals`` / ``core_decoder`` 里没有 capture_toggles 的引用；
        一旦有人把开关接进语音链路，这条会失败。
        """

        for relative in (
            "engine/live/voice_signals.py",
            "engine/live/wire/core_decoder.py",
        ):
            source = Path(relative).read_text(encoding="utf-8")
            self.assertNotIn("capture_toggles", source, relative)
            self.assertNotIn("CaptureToggles", source, relative)

    def test_publish_path_passes_toggles(self) -> None:
        """源码级兜底：发布点必须把开关交给 should_publish。"""

        source = Path(
            "engine/live/capture_sources.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _publish_events")
        block = source[start:source.index("def _decode_frames", start)]
        self.assertIn("should_publish(event, toggles)", block)
        self.assertIn('getattr(self, "_toggles", None)', block)
        self.assertNotIn("should_capture", block)


class CaptureTogglesMappingTests(unittest.TestCase):
    def test_defaults_enable_both(self) -> None:
        toggles = CaptureToggles()
        self.assertEqual(toggles.to_mapping(), {"buffs": True, "boss_actions": True})

    def test_from_mapping_round_trip(self) -> None:
        toggles = CaptureToggles.from_mapping({"buffs": False, "boss_actions": True})
        self.assertFalse(toggles.buffs)
        self.assertTrue(toggles.boss_actions)
        self.assertEqual(toggles.to_mapping(), {"buffs": False, "boss_actions": True})

    def test_missing_keys_default_to_enabled(self) -> None:
        toggles = CaptureToggles.from_mapping({"buffs": False})
        self.assertTrue(toggles.boss_actions)

    def test_non_mapping_falls_back_to_defaults(self) -> None:
        self.assertEqual(CaptureToggles.from_mapping(None), CaptureToggles())
        self.assertEqual(CaptureToggles.from_mapping("nope"), CaptureToggles())

    def test_labels_cover_both_switches(self) -> None:
        self.assertEqual(
            set(CAPTURE_TOGGLE_LABELS),
            set(CaptureToggles.category_names()),
        )


if __name__ == "__main__":
    unittest.main()
