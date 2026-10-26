"""观测日志（nest_probe）的最小回归测试。

这些用例只验证「只记录、不判定」的契约：
  * 未开启时是空操作，不产生任何文件；
  * 开启后按 JSON Lines 落盘，事件可被回读；
  * 未命中的 spawn / map 抽样写，不会把每个包都记下来；
  * classic_nest_hint 与前端 isClassic() 的判定保持一致。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from engine.live import nest_probe


def _read(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class NestProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in (nest_probe.PROBE_ENV, nest_probe.PROBE_PATH_ENV)
        }
        nest_probe.close_all()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        nest_probe.close_all()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _enable(self) -> Path:
        target = self.root / "probe.jsonl"
        os.environ[nest_probe.PROBE_ENV] = "1"
        os.environ[nest_probe.PROBE_PATH_ENV] = str(target)
        return target

    def test_disabled_by_default(self) -> None:
        os.environ.pop(nest_probe.PROBE_ENV, None)
        probe = nest_probe.get_probe(self.root)
        probe.record("should_not_exist")
        probe.observe_packet("UDP", "server_to_client", b"\x00" * 88)
        probe.close()
        self.assertFalse(probe.enabled)
        self.assertEqual(list(self.root.glob("*.jsonl")), [])

    def test_runtime_toggle_creates_and_stops_file(self) -> None:
        os.environ.pop(nest_probe.PROBE_ENV, None)
        target = self.root / "probe.jsonl"
        os.environ[nest_probe.PROBE_PATH_ENV] = str(target)
        probe = nest_probe.get_probe(self.root)

        self.assertTrue(probe.set_enabled(True))
        probe.record("map_hit", map_id=14511)
        self.assertEqual(nest_probe.describe()["source"], "runtime")
        self.assertTrue(nest_probe.describe()["enabled"])

        self.assertFalse(probe.set_enabled(False))
        probe.record("after_off")
        rows = _read(target)
        kinds = [row["kind"] for row in rows]
        self.assertIn("map_hit", kinds)
        self.assertNotIn("after_off", kinds)

        # 关闭不删文件；显式指定 .jsonl 时再次开启继续写同一个文件。
        before = target.stat().st_size
        self.assertTrue(probe.set_enabled(True))
        probe.record("second_round")
        probe.close()
        self.assertGreater(target.stat().st_size, before)
        self.assertIn("second_round", [row["kind"] for row in _read(target)])

    def test_rotate_keeps_switch_but_starts_new_file(self) -> None:
        os.environ[nest_probe.PROBE_ENV] = "1"
        log_dir = self.root / "logs"
        os.environ[nest_probe.PROBE_PATH_ENV] = str(log_dir)
        probe = nest_probe.get_probe(self.root)
        probe.record("first")
        first_path = probe.path

        nest_probe.rotate()
        self.assertTrue(probe.enabled)
        probe.record("second")
        second_path = probe.path
        probe.close()

        self.assertIsNotNone(first_path)
        self.assertIsNotNone(second_path)
        self.assertNotEqual(first_path, second_path)
        self.assertIn("first", [row["kind"] for row in _read(first_path)])
        self.assertIn("second", [row["kind"] for row in _read(second_path)])

    def test_records_events_and_survives_unserializable_fields(self) -> None:
        target = self._enable()
        probe = nest_probe.get_probe(self.root, header={"dataRoot": "x"})
        probe.record("map_hit", map_id=14511, name="黑龙巢穴（一）")
        probe.record("weird", payload=object())
        probe.close()

        rows = _read(target)
        self.assertEqual(rows[0]["kind"], "header")
        self.assertEqual(rows[0]["dataRoot"], "x")
        kinds = [row["kind"] for row in rows]
        self.assertIn("map_hit", kinds)
        # object() 不可序列化时必须退化成字符串而不是抛异常。
        weird = next(row for row in rows if row["kind"] == "weird")
        self.assertIn("payload", weird)

    def test_spawn_misses_are_sampled(self) -> None:
        target = self._enable()
        probe = nest_probe.get_probe(self.root)
        for _ in range(1200):
            probe.note_spawn_attempt(hit=False, length=88)
        probe.note_spawn_attempt(hit=True, template_id=503611, boss=True)
        probe.close()

        rows = _read(target)
        misses = [row for row in rows if row["kind"] == "spawn_miss"]
        hits = [row for row in rows if row["kind"] == "spawn_hit"]
        self.assertEqual(len(hits), 1)
        # 前 20 条 + 每 500 次一条 → 远小于 1200，避免刷屏。
        self.assertLess(len(misses), 30)
        self.assertGreaterEqual(len(misses), 20)

    def test_packet_shapes_are_aggregated(self) -> None:
        target = self._enable()
        probe = nest_probe.get_probe(self.root)
        payload = b"\x00" * 5 + b"\x06\x27\x55\x00" + b"\x00" * 79
        for _ in range(5):
            probe.observe_packet("UDP", "server_to_client", payload)
        probe.observe_packet("UDP", "client_to_server", payload)
        probe.close()

        rows = _read(target)
        shape_rows = [row for row in rows if row["kind"] == "shape_stats"]
        self.assertTrue(shape_rows)
        shapes = {item["shape"]: item["count"] for row in shape_rows for item in row["top"]}
        self.assertEqual(shapes.get(f"UDP:{len(payload)}:06275500"), 5)
        # 客户端方向的包不计入。
        self.assertFalse(any(key.startswith("UDP:" ) and False for key in shapes))

    def test_relaxed_scan_rejects_garbage(self) -> None:
        self.assertEqual(
            nest_probe.relaxed_skill_candidates(b"\x00" * 64, {0x36}),
            (),
        )

    def test_classic_hint_matches_frontend(self) -> None:
        cases = {
            "黑龙巢穴（经典模式）": True,
            "黑龙经典": True,
            "黑龙巢穴（一）": True,
            "黑龙1": True,
            "黑龙巢穴（一）硬核": False,
            "黑龙硬核": False,
            "梦魇海龙": False,
            "": False,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(nest_probe.classic_nest_hint(name), expected)


if __name__ == "__main__":
    unittest.main()
