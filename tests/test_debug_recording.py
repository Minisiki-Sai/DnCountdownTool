"""「调试记录」开关的回归测试：一个开关同时控制观测日志与保留抓包。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from engine.live import debug_recording, nest_probe


class DebugRecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dn-debug-rec-"))
        self.config = self.root / "debug_recording.json"
        self.output = self.root / "records"
        os.environ[debug_recording.ENV_CONFIG_PATH] = str(self.config)
        os.environ[debug_recording.ENV_OUTPUT_DIR] = str(self.output)
        os.environ.pop(nest_probe.PROBE_ENV, None)
        os.environ.pop(nest_probe.PROBE_PATH_ENV, None)
        nest_probe.close_all()
        debug_recording._reset_for_tests()

    def tearDown(self) -> None:
        nest_probe.close_all()
        debug_recording._reset_for_tests()
        for name in (debug_recording.ENV_CONFIG_PATH, debug_recording.ENV_OUTPUT_DIR):
            os.environ.pop(name, None)

    def _working_capture(self, name: str = "live_test.dpscap") -> Path:
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        path = work / name
        path.write_bytes(b"DNDP1\x00" + b"\x00" * 32)
        return path

    def test_defaults_are_off_and_write_nothing(self) -> None:
        state = debug_recording.describe()
        self.assertFalse(state["enabled"])
        self.assertFalse(debug_recording.keep_capture_enabled())
        self.assertEqual(state["outputDir"], str(self.output))
        self.assertEqual(list(self.root.glob("records/**/*")), [])

    def test_single_switch_starts_probe_and_keeps_capture(self) -> None:
        state = debug_recording.apply(enabled=True)
        self.assertTrue(state["enabled"])
        self.assertTrue(nest_probe.probe_requested())

        probe = nest_probe.get_probe(self.root)
        probe.record("map_hit", map_id=14511)
        self.assertTrue(probe.path is not None and probe.path.is_file())
        self.assertEqual(probe.path.parent, debug_recording.probe_dir())

        source = self._working_capture()
        self.assertTrue(debug_recording.keep_capture_enabled())
        self.assertTrue(debug_recording.retain_capture(source))
        self.assertFalse(source.exists(), "保留后应已从临时目录移走")

        kept = list(debug_recording.capture_dir().rglob("capture_*.dpscap"))
        self.assertEqual(len(kept), 1)
        # 一场一个以时间命名的目录，抓包和观测日志必须在同一层。
        session = kept[0].parent
        self.assertEqual(session.parent, debug_recording.capture_dir())
        self.assertRegex(session.name, r"^\d{8}_\d{6}$")
        self.assertTrue(
            list(session.glob("probe_*.jsonl")),
            "观测日志应随抓包归档到同一场目录",
        )
        state = debug_recording.describe()
        self.assertEqual(state["lastCapturePath"], str(kept[0]))
        self.assertEqual(state["keptCaptures"], 1)
        self.assertGreater(state["probeRows"], 0)

    def test_disabling_stops_keeping_without_deleting_files(self) -> None:
        debug_recording.apply(enabled=True)
        source = self._working_capture()
        debug_recording.retain_capture(source)

        state = debug_recording.apply(enabled=False)
        self.assertFalse(state["enabled"])
        self.assertFalse(debug_recording.keep_capture_enabled())
        kept = list(debug_recording.capture_dir().rglob("capture_*.dpscap"))
        self.assertEqual(len(kept), 1, "关闭开关不应删除已保存的抓包")

    def test_output_dir_change_redirects_probe(self) -> None:
        debug_recording.apply(enabled=True)
        probe = nest_probe.get_probe(self.root)
        probe.record("first", value=1)
        first_path = probe.path
        self.assertIsNotNone(first_path)

        other = self.root / "elsewhere"
        state = debug_recording.apply(output_dir=str(other))
        self.assertEqual(state["outputDir"], str(other))
        probe.record("second", value=2)

        self.assertTrue(first_path.is_file(), "旧目录里的文件应保留")
        second_path = probe.path
        self.assertIsNotNone(second_path)
        self.assertEqual(second_path.parent, other / "probe")
        self.assertNotEqual(second_path, first_path)

    def test_config_survives_restart(self) -> None:
        debug_recording.apply(enabled=True, output_dir=str(self.root / "custom"))
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"enabled": True, "outputDir": str(self.root / "custom")})

        # 模拟后端重启：清掉内存态后从磁盘恢复。
        nest_probe.close_all()
        debug_recording._reset_for_tests()
        state = debug_recording.describe()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["outputDir"], str(self.root / "custom"))

    def test_unwritable_output_dir_is_rejected(self) -> None:
        target = self.root / "blocked"
        target.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ValueError):
            debug_recording.apply(output_dir=str(target))
        # 校验失败发生在落盘之前：配置保持原样，开关也不会被连带打开。
        self.assertFalse(self.config.exists())
        state = debug_recording.describe()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["configuredDir"], "")

    def test_capture_is_dropped_when_switch_is_off(self) -> None:
        """关闭时采集端走原来的删除分支：这里只验证开关判定。"""

        self.assertFalse(debug_recording.keep_capture_enabled())
        source = self._working_capture("live_off.dpscap")
        self.assertTrue(source.exists())
        # 未开启时调用方不会调用 retain_capture，文件应保持原状由调用方删除。
        self.assertFalse(debug_recording.enabled())


if __name__ == "__main__":
    unittest.main()
