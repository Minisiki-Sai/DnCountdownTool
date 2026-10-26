"""Regression tests for the overlay debuff rows.

The reported bug: Boss effects were decoded and a damage target was known, but
``boss_runtime_targets`` was an empty list (training dummy / no resolved boss
identity), so ``/api/capture/buff-overlay`` published zero debuff rows and the
overlay looked like it tracked nothing.

``NativeOverlayMixin.buff_overlay_status`` only needs ``live_capture`` and
``_live_buff_catalog``, so the whole projection can be exercised without the
capture engine, the game or a packaged build.  Import cost is ~0.6 s.
"""

from __future__ import annotations

import unittest

# Must be imported before overlay_service: it injects the host helpers
# (norm / as_number / EFFECT_STACKING_ENGINE ...) that overlay_service uses.
import engine.core  # noqa: F401
from engine.native.overlay_service import NativeOverlayMixin

TARGET_ID = "boss-1"
TARGET_ROW = {
    "target_id": TARGET_ID,
    "damage": 1,
    "hits": 1,
    "share": 1.0,
    "target_name": "修炼场假人",
    "target_job": "",
    "target_max_hp": 0,
    "first_ms": 1_000,
    "last_ms": 1_500,
}


def effect_row(key: str, value: float, *, start: float = 0.0, end: float = 60.0,
               skill_name: str = "技能1") -> dict:
    """One runtime Boss effect row as produced by the aggregator.

    ``key`` must be the canonical name (``减火抗%``), because the projection
    resolves rows through ``canonical_effect_key``; anything else is dropped
    silently, which is the same failure mode as the original bug.
    """

    return {
        "target_id": TARGET_ID,
        "key": key,
        "value": value,
        "intervals": [[start, end]],
        "skill_id": "skill-1",
        "skill_name": skill_name,
        "source_id": "player-1",
        "source_name": "测试角色",
        "job": "元素师",
        "stack_group": "",
        "value_source": "packet",
        "duration_source": "packet",
        "data_source": "boss_status",
    }


def snapshot(*, effects: list[dict], runtime_targets: list[dict] | None = None,
             damage_targets: list[dict] | None = None) -> dict:
    if damage_targets is None:
        damage_targets = [dict(TARGET_ROW)]
    return {
        "elapsed_seconds": 2.0,
        "boss_effect_current_seconds": 2.0,
        "selfSourceId": "player-1",
        "players": [],
        "player_buffs": [],
        "boss_effects": effects,
        # The aggregation layer mirrors the same rows under the runtime key;
        # keep both so the test reflects a real snapshot.
        "boss_runtime_effects": effects,
        "boss_runtime_targets": [] if runtime_targets is None else runtime_targets,
        "damage_targets": damage_targets,
        "endedBossIds": [],
    }


class _FakeCapture:
    def __init__(self, snap: dict) -> None:
        self._snap = snap

    def overlay_status(self) -> dict:
        return {
            "overlaySnapshot": self._snap,
            "overlaySnapshotRevision": 1,
            "state": "running",
            "status": "采集中",
        }


class _Harness(NativeOverlayMixin):
    def __init__(self, snap: dict) -> None:
        self.live_capture = _FakeCapture(snap)
        self._live_buff_catalog: dict = {}


class OverlayDebuffRowsTest(unittest.TestCase):
    def test_rows_survive_empty_runtime_targets(self) -> None:
        """Empty ``boss_runtime_targets`` must fall back to damage targets."""

        snap = snapshot(effects=[effect_row("减火抗%", 30.0)])
        self.assertEqual(snap["boss_runtime_targets"], [])

        payload = _Harness(snap).buff_overlay_status()
        rows = payload["debuffs"]

        self.assertEqual(len(rows), 1, "unrecognised boss target dropped every debuff row")
        row = rows[0]
        self.assertEqual(row["category"], "resistance")
        self.assertEqual(row["mode"], "fire")
        self.assertEqual(row["label"], "减火抗")
        self.assertEqual(row["current_value"], 30.0)
        self.assertEqual(row["target_id"], TARGET_ID)
        self.assertGreater(row["end_seconds"], payload["elapsedSeconds"])
        self.assertEqual([c["skill_name"] for c in row["contributors"]], ["技能1"])

    def test_all_resistance_expands_to_four_elements(self) -> None:
        rows = _Harness(snapshot(effects=[effect_row("减全抗%", 15.0)])).buff_overlay_status()["debuffs"]

        self.assertEqual([row["mode"] for row in rows], ["fire", "water", "light", "dark"])
        self.assertEqual([row["current_value"] for row in rows], [15.0] * 4)

    def test_no_rows_without_evidence(self) -> None:
        """Guard against noisy false positives."""

        expired = _Harness(
            snapshot(effects=[effect_row("减火抗%", 30.0, start=0.0, end=1.0)])
        ).buff_overlay_status()["debuffs"]
        self.assertEqual(expired, [])

        no_effects = _Harness(snapshot(effects=[])).buff_overlay_status()["debuffs"]
        self.assertEqual(no_effects, [])

        # Canonical keys end with %, the display label does not.
        wrong_key = _Harness(
            snapshot(effects=[effect_row("减火抗", 30.0)])
        ).buff_overlay_status()["debuffs"]
        self.assertEqual(wrong_key, [])

    def test_same_stack_group_keeps_the_strongest_value(self) -> None:
        rows = _Harness(snapshot(effects=[
            effect_row("减火抗%", 30.0, skill_name="强"),
            effect_row("减火抗%", 15.0, skill_name="弱"),
        ])).buff_overlay_status()["debuffs"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_value"], 30.0)
        self.assertEqual([c["skill_name"] for c in rows[0]["contributors"]], ["强"])

    def test_zeroed_damage_targets_still_publish_rows(self) -> None:
        """The lightweight capture profile drops damage events.

        ``damage_targets`` then carries rows with ``damage=0, last_ms=0`` while
        Boss effects are decoded normally.  That is not evidence of "no target":
        the effects themselves must pick one, otherwise the overlay publishes
        zero debuff rows in every real fight.
        """

        zeroed = [dict(TARGET_ROW, damage=0, hits=0, last_ms=0, first_ms=0)]
        rows = _Harness(snapshot(
            effects=[effect_row("减火抗%", 30.0)],
            damage_targets=zeroed,
        )).buff_overlay_status()["debuffs"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "resistance")
        self.assertEqual(rows[0]["mode"], "fire")
        self.assertEqual(rows[0]["current_value"], 30.0)

    def test_runtime_targets_still_work(self) -> None:
        """Non-empty runtime targets keep the previous behaviour."""

        rows = _Harness(snapshot(
            effects=[effect_row("减火抗%", 30.0)],
            runtime_targets=[dict(TARGET_ROW)],
        )).buff_overlay_status()["debuffs"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_value"], 30.0)


if __name__ == "__main__":
    unittest.main()
