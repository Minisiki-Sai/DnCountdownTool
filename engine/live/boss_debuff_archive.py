"""Post-score, privacy-safe projection of the SAME Boss status UI ledger.

No packet processing, inference or scoring arithmetic lives in this module.
Curves are passed from the existing target-scoped live/replay projection.
"""
from __future__ import annotations
import base64
import dataclasses
import json
import zlib
from typing import Any
from .event_platform import event_platform_for

EFFECT_KEYS = frozenset({"减火抗%", "减水抗%", "减光抗%", "减暗抗%", "减全抗%",
    "增伤%", "物理增伤合计%", "魔法增伤合计%", "减防御%", "减物理防御%",
    "减魔法防御%", "减致命抵抗%", "减攻击%", "冰冻层"})
ALIASES = {"物理增伤%": "物理增伤合计%", "魔法增伤%": "魔法增伤合计%"}
ROW_KEYS = frozenset({"key", "label", "job", "skill_id", "skill_name", "value",
    "status_kind", "effect_code", "parameters", "start_seconds", "end_seconds",
    "last_refresh_seconds", "refresh_count", "coverage", "target_id", "target_name",
    "intervals", "effective_intervals", "effective_coverage", "effective_state", "effective_reason"})


def encode_boss_debuff_archive(snapshot: object, views: list[dict[str, Any]]) -> dict[str, object]:
    platform = event_platform_for(snapshot)
    boss_ids = {str(target) for view in views for target in view.get("target_ids", ())}
    effects = []
    for value in platform.boss_effects:
        row = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(value)
        key = str(row.get("key", ""))
        if ALIASES.get(key, key) not in EFFECT_KEYS or str(row.get("target_id", "")) not in boss_ids:
            continue
        # Source actor IDs and character names are intentionally never uploaded.
        effects.append({key: value for key, value in row.items() if key in ROW_KEYS})
    clean_views = [{key: value for key, value in view.items() if key in {
        "key", "name", "target_ids", "series", "freeze_coverage", "freeze_covered_seconds",
        "freeze_layers", "output_windows", "output_seconds"}} for view in views]
    for view in clean_views:
        view["series"] = [row for row in view.get("series", ()) if row[0] in EFFECT_KEYS]
    archive = {"v": 1, "extent": float(platform.elapsed_seconds), "effects": effects,
        "views": clean_views, "series": [row for row in platform.boss_effect_series if row[0] in EFFECT_KEYS],
        "phases": []}
    encoded = json.dumps(archive, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    # Compression is done once after capture/scoring, never in the receive loop.
    if len(encoded) > 8 * 1024 * 1024:
        return {"v": 1, "unavailable": "Boss 状态账本超过上传容量限制，未截断或伪造曲线"}
    compressed = base64.b64encode(zlib.compress(encoded, 6)).decode("ascii")
    if len(compressed) > 2 * 1024 * 1024:
        return {"v": 1, "unavailable": "压缩后的 Boss 状态账本超过上传容量限制，未截断或伪造曲线"}
    return {"v": 1, "encoding": "deflate-base64", "data": compressed}
