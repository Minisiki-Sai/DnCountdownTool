"""开发者模式「回放实战录像」的后台作业。

这条链路是：界面选文件 → 后台线程解码 → 轮询进度 → 拿到证据行交给前端。
这里直接测后端这一段，避免「界面上点不动」只能靠手点。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.live.capture_replay import discover_captures  # noqa: E402
from engine.live.replay_jobs import ReplayJobManager  # noqa: E402

DATA_ROOT = ROOT / "engine" / "data"
CAPTURE_ROOTS = [
    Path.home() / "Documents" / "DragonNestDpsCapture" / "debug_records",
    Path.home() / "Desktop" / "抓包文件",
]


def smallest_capture() -> Path | None:
    """挑一个最小的包，让测试跑得动。"""

    candidates: list[Path] = []
    for root in CAPTURE_ROOTS:
        if root.is_dir():
            candidates.extend(root.rglob("*.dpscap"))
    # 太小的多半是误触录制（几 KB、零事件），解码快但拿不到行，不适合当样本。
    usable = [path for path in candidates if path.stat().st_size > 100_000]
    if not usable:
        return None
    return min(usable, key=lambda item: item.stat().st_size)


class ReplayJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ReplayJobManager(DATA_ROOT)

    def test_rejects_missing_file(self) -> None:
        result = self.manager.open(str(Path.home() / "definitely-not-here.dpscap"))
        self.assertFalse(result["ok"])
        self.assertIn("找不到", str(result.get("error", "")))

    def test_rejects_wrong_extension(self) -> None:
        target = ROOT / "README.md"
        result = self.manager.open(str(target))
        self.assertFalse(result["ok"])

    def test_status_of_unknown_job(self) -> None:
        self.assertFalse(self.manager.status("nope")["ok"])

    def test_captures_listing_is_shaped_for_the_ui(self) -> None:
        payload = self.manager.captures(*[path for path in CAPTURE_ROOTS if path.is_dir()])
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["captures"], list)
        for row in payload["captures"][:5]:
            self.assertIn("path", row)
            self.assertIn("name", row)
            self.assertIn("sizeBytes", row)
            self.assertIn("modifiedAt", row)
            self.assertTrue(str(row["name"]).endswith(".dpscap"))

    def test_open_real_capture_reaches_ready(self) -> None:
        capture = smallest_capture()
        if capture is None:
            self.skipTest("本机没有抓包样本")
        opened = self.manager.open(str(capture))
        self.assertTrue(opened["ok"], opened)
        job = str(opened["job"])
        deadline = time.monotonic() + 120
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.manager.status(job)
            if status.get("state") in {"ready", "error"}:
                break
            time.sleep(0.05)
        self.assertEqual(status.get("state"), "ready", status)
        self.assertEqual(status.get("drops"), {}, "回放里不该有被丢弃的技能")
        self.assertIsInstance(status.get("rows"), list)
        self.assertGreater(len(status["rows"]), 0)
        for row in status["rows"]:
            self.assertIn("timestamp", row)
            self.assertIn("skill", row)
        # 第二次打开同一个文件要命中缓存，不再重新解码。
        again = self.manager.open(str(capture))
        self.assertTrue(again.get("cached"))


class DiscoverCapturesTest(unittest.TestCase):
    def test_discovers_and_sorts_newest_first(self) -> None:
        roots = [path for path in CAPTURE_ROOTS if path.is_dir()]
        if not roots:
            self.skipTest("本机没有抓包目录")
        rows = discover_captures(*roots)
        self.assertGreater(len(rows), 0)
        stamps = [int(row["modifiedAt"]) for row in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "最新的录像要排在前面")


if __name__ == "__main__":
    unittest.main()
