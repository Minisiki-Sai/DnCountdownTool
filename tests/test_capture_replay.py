"""抓包回放测试：用昨天的实战包代替实机验证机制播报。

三层断言：

1. **守恒**（最快，常开）：``tests/fixtures/capture_replay`` 里每个包的
   ``drops`` 必须为空 —— 解码器解出来的 Boss 技能不许被静默丢掉。
   红龙佩达 P1/P2 那次事故就是「解出 4 条 34848、发布 0 条」而日志一片安静。

2. **对齐实机**（常开）：同一场既有抓包又有观测日志时，把回放出的证据序列和
   当时实机 ``evidence_publish`` 逐条比对。这是唯一的外部锚点 —— 没有它，
   「回放跑通」只能证明「和昨天一样」，证明不了「和实战一样」。

3. **重跑真抓包**（慢，``DN_CAPTURE_REPLAY=1`` 时开）：重新解码原始 .dpscap，
   确认 fixture 没和代码漂移。抓包不常在仓库里，所以默认跳过。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "capture_replay"

# 抓包可能在这些位置；一个都找不到时，慢测试自动跳过。
CAPTURE_ROOTS = [
    Path.home() / "Desktop" / "抓包文件",
    Path.home() / "Documents" / "DragonNestDpsCapture" / "debug_records" / "sessions",
]

# 既有观测日志（实机锚点）。目前只有 135542 那场留下了 evidence_publish ——
# 更早的日志因为 `ProbeRecorder.record(kind, **fields)` 形参重名抛 TypeError
# 被 except 吞掉，证据记录一条都没落盘。
PROBE_ANCHORS = [
    (
        "capture_20261025_135542.json",
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "debug_records"
        / "sessions"
        / "20261025_135542",
    ),
]


def fixtures() -> list[dict]:
    if not FIXTURE_DIR.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def publish_anchor(session_dir: Path) -> list[tuple[int, int]]:
    """从观测日志里取出实机发布的 (skill, templateId) 序列。"""
    rows: list[tuple[float, int, int]] = []
    for path in sorted(session_dir.glob("probe*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") != "evidence_publish":
                continue
            rows.append(
                (
                    float(row.get("t") or 0),
                    int(row.get("skill") or 0),
                    int(row.get("template_id") or 0),
                )
            )
    rows.sort()
    return [(skill, template) for _, skill, template in rows]


class ReplayConservationTest(unittest.TestCase):
    def test_fixtures_exist(self) -> None:
        self.assertTrue(fixtures(), f"没有回放 fixture：{FIXTURE_DIR}")

    def test_no_boss_skill_is_silently_dropped(self) -> None:
        """守恒：解码器解出来的 Boss 技能必须全部有归属。"""
        for fixture in fixtures():
            with self.subTest(capture=fixture["capture"]):
                self.assertEqual(
                    fixture.get("drops") or {},
                    {},
                    f"{fixture['capture']} 有被丢弃的 Boss 技能："
                    f"{fixture.get('drops')} 样本 {fixture.get('dropSamples')}",
                )

    def test_every_capture_has_rows_or_is_explained(self) -> None:
        for fixture in fixtures():
            with self.subTest(capture=fixture["capture"]):
                rows = fixture.get("rows") or []
                if "134741" in fixture["capture"]:
                    # 5 秒空包：录制误触，本来就没有内容。
                    self.assertEqual(rows, [])
                    continue
                self.assertTrue(rows, f"{fixture['capture']} 一行都没解出来")

    def test_skill_rows_carry_template_and_source(self) -> None:
        """每条技能行都要能定位到哪只 Boss 的哪个技能，否则前端规则无从匹配。"""
        for fixture in fixtures():
            for row in fixture.get("rows") or []:
                if int(row.get("skill") or 0) <= 0:
                    continue
                kind = row.get("kind") or row.get("evidenceKind")
                if kind in {"boss_hp_state", "boss_hp_threshold"}:
                    continue
                with self.subTest(capture=fixture["capture"], skill=row["skill"]):
                    self.assertTrue(row.get("templateId"), "技能行缺 templateId")
                    self.assertTrue(row.get("source"), "技能行缺 source")


class ReplayMatchesLiveTest(unittest.TestCase):
    """回放必须复现当时实机发布的同一条证据序列。"""

    def test_published_skills_match_recorded_run(self) -> None:
        by_name = {row["capture"].replace(".dpscap", ".json"): row for row in fixtures()}
        checked = 0
        for fixture_name, session_dir in PROBE_ANCHORS:
            if fixture_name not in by_name or not session_dir.is_dir():
                continue
            live = publish_anchor(session_dir)
            if not live:
                continue
            replayed = [
                (int(row["skill"]), int(row.get("templateId") or 0))
                for row in by_name[fixture_name]["rows"]
                if int(row.get("skill") or 0) > 0
                and (row.get("kind") or row.get("evidenceKind"))
                not in {"boss_hp_state", "boss_hp_threshold"}
            ]
            with self.subTest(capture=fixture_name):
                self.assertEqual(
                    replayed,
                    live,
                    "回放出来的证据序列和当时实机发布的不一致："
                    f"\n回放 {replayed}\n实机 {live}",
                )
            checked += 1
        self.assertGreater(checked, 0, "没有可用的实机锚点，对齐断言没跑起来")

    def test_replay_time_offset_is_a_constant(self) -> None:
        """回放时间只允许和实机差一个固定原点，不允许漂移。

        观测日志的 ``t`` 从「开始录制」起算，抓包行的 ``timestamp`` 从
        「第一个解码包」起算，实测差约 0.5s。差值恒定才说明两边的时钟同源。
        """
        by_name = {row["capture"].replace(".dpscap", ".json"): row for row in fixtures()}
        for fixture_name, session_dir in PROBE_ANCHORS:
            if fixture_name not in by_name or not session_dir.is_dir():
                continue
            live_times: list[float] = []
            for path in sorted(session_dir.glob("probe*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("kind") == "evidence_publish":
                        live_times.append(float(row.get("t") or 0))
            replay_times = [
                float(row["t"])
                for row in by_name[fixture_name]["rows"]
                if int(row.get("skill") or 0) > 0
            ]
            if not live_times or len(live_times) != len(replay_times):
                continue
            live_times.sort()
            offsets = [live - replay for live, replay in zip(live_times, replay_times)]
            with self.subTest(capture=fixture_name):
                self.assertLess(
                    max(offsets) - min(offsets),
                    0.2,
                    f"回放与实机的时间差不是常量：{offsets}",
                )


@unittest.skipUnless(
    os.environ.get("DN_CAPTURE_REPLAY") == "1",
    "慢测试：设 DN_CAPTURE_REPLAY=1 才会重新解码原始 .dpscap",
)
class ReplayFromRealCapturesTest(unittest.TestCase):
    """重新解码原始抓包，确认 fixture 没和代码漂移。"""

    def test_captures_are_available(self) -> None:
        found = [
            path
            for root in CAPTURE_ROOTS
            if root.is_dir()
            for path in root.rglob("*.dpscap")
        ]
        self.assertTrue(found, f"找不到抓包：{CAPTURE_ROOTS}")

    def test_redecode_matches_fixture(self) -> None:
        from engine.live.capture_replay import replay  # noqa: PLC0415

        by_stem = {row["capture"].replace(".dpscap", ""): row for row in fixtures()}
        compared = 0
        for root in CAPTURE_ROOTS:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.dpscap")):
                fixture = by_stem.get(path.stem)
                if fixture is None:
                    continue
                result = replay(path, ROOT / "engine" / "data")
                with self.subTest(capture=path.name):
                    self.assertEqual(dict(result.drops), {}, "重新解码出现丢弃")
                    replayed = [
                        (int(row["skill"]), int(row.get("templateId") or 0))
                        for row in result.rows
                        if int(row.get("skill") or 0) > 0
                    ]
                    expected = [
                        (int(row["skill"]), int(row.get("templateId") or 0))
                        for row in fixture["rows"]
                        if int(row.get("skill") or 0) > 0
                    ]
                    self.assertEqual(
                        replayed,
                        expected,
                        f"{path.name} 的 fixture 已和代码漂移，请重新生成",
                    )
                compared += 1
        self.assertGreater(compared, 0)


if __name__ == "__main__":
    unittest.main()
