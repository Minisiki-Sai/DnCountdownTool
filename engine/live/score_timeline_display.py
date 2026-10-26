"""Privacy-safe inputs to the common live/history timeline renderer.

Built only in the post-score export worker. Never fed into scoring or capture.
"""
from __future__ import annotations
import base64
import dataclasses
import json
import zlib
from .aura_state import is_aura_row, is_aura_damage_series, is_inferred_aura_row


def rows(values):
    return [dataclasses.asdict(row) if dataclasses.is_dataclass(row) else dict(row)
            for row in values or () if dataclasses.is_dataclass(row) or isinstance(row, dict)]


def keep(row, keys):
    return {key: row[key] for key in keys if key in row}


def score_timeline_display_context(detail, platform, casts, hp_curves, curve_specs, hp_tracks=()):
    source = str(detail.get("source_id") or "").upper()
    actual_casts = [keep(row, ("timestamp_ms", "end_timestamp_ms", "skill_name", "display_name", "variant_key", "skill_id", "cast_time",
                               "action_speed_percent", "packet_kind")) | {"source_id": "self"}
                    for row in casts if str(row.get("source_id") or "").upper() == source]
    buffs = []
    for row in rows(platform.player_buff_series):
        if str(row.get("target_id") or "").upper() != source:
            continue
        if is_inferred_aura_row(row) or (is_aura_row(row) and not is_aura_damage_series(row)):
            continue
        buff = keep(row, ("label", "status_kind", "skill_name", "skill_id", "key", "scope", "job", "intervals",
                          "effect_code", "state_source"))
        buff.update(target_id="self", source_id="self" if str(row.get("source_id") or "").upper() == source else "team")
        buffs.append(buff)
    clear_events = [keep(row, ("timestamp_ms", "skill_name")) | {"affected_target_ids": ["self"]}
                    for row in rows(getattr(platform, "player_buff_clear_events", ()))
                    if source in {str(value).upper() for value in row.get("affected_target_ids", ())}]
    cast_keys = ("seconds", "end_seconds", "display_name", "skill_name", "skill", "power", "action_speed_percent",
                 "high_value", "in_burst_window", "display_only", "expected_duration", "utilized_duration")
    support_keys = ("skill_name", "effect_key", "kind", "magnitude", "actual_intervals", "actual_display_intervals",
                    "actual_coverage", "actual_contribution", "theory_contribution")
    displayed = {
        "actual_timeline": [keep(row, cast_keys) for row in rows(detail.get("actual_timeline"))],
        "theory_timeline": [keep(row, cast_keys) for row in rows(detail.get("theory_timeline"))],
        "support_details": [keep(row, support_keys) for row in rows(detail.get("support_details"))],
        "burst_windows": [keep(row, ("label", "start_seconds", "end_seconds", "actual_power", "theory_power", "completion"))
                          for row in rows(detail.get("burst_windows"))],
        "output_windows": detail.get("output_windows", []),
        **({"team_output_windows": detail["team_output_windows"]}
           if "team_output_windows" in detail else {}),
        "output_curve_specs": curve_specs,
    }
    effects = [keep(row, ("target_id", "skill_name", "label", "key", "status_kind", "job", "intervals", "start_seconds", "end_seconds"))
               for row in rows(platform.boss_effects)
               if any(word in (str(row.get("status_kind")) + str(row.get("label"))) for word in ("叠冰", "冰冻层"))]
    tracks = [keep(row, ("target_id", "target_name")) | {
        "points": [keep(point, ("timestamp_seconds", "current_percent")) for point in rows(row.get("points"))]
    } for row in rows(hp_tracks)]
    return {"score": {"sourceId": "self", "job": detail.get("job", ""), "outputScore": detail.get("output_score", 0)},
            "detail": displayed,
            "snapshot": {"elapsed_seconds": max(platform.elapsed_seconds, getattr(platform, "boss_effect_current_seconds", 0.0)), "damage_series": [], "players": [],
                         "skill_cast_events": actual_casts, "player_buff_series": buffs,
                         "player_buff_clear_events": clear_events, "boss_effects": effects,
                         "boss_effect_series": [], "boss_effect_views": [],
                         "boss_hp_curves": [keep(row, ("name", "target_ids", "segments")) for row in hp_curves],
                         "boss_hp_tracks": tracks}}


def encode_score_timeline_display(context):
    raw = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > 4 * 1024 * 1024:
        return None
    return base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")
