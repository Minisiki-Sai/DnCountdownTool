"""Display-only projections. Never alter packet/history/scoring ledgers."""
from __future__ import annotations


ACCELERATION_GAIN_SECONDS = {
    "灼热火焰": 14.0, "精神爆发": 14.0, "变异精神爆发": 14.0,
    "时间加速": 15.0, "变异时间加速": 15.0,
}


def acceleration_overlay_row(row: dict, skill: str, elapsed: float) -> dict | None:
    duration = ACCELERATION_GAIN_SECONDS.get(skill)
    if duration is None:
        return row
    identity = " ".join(str(row.get(k) or "") for k in
                        ("key", "effect_key", "label", "status_kind", "kind"))
    # These are reapplication locks, not the acceleration gain. Preserve them
    # in the raw ledger but never merge their long duration into the Buff bar.
    # Captures label both rows "增益": 136e00 is burst reapplication lock,
    # 135c00 is Time Acceleration lock. Do not depend on translated labels.
    code = str(row.get("effect_code") or "").lower()
    if (code in {"136e00", "135c00"} or ":136e00:" in identity.lower()
            or ":135c00:" in identity.lower()
            or "抵抗" in identity or "debuff" in identity.lower()):
        return None
    intervals = row.get("intervals") or [[row.get("start_seconds", 0), row.get("end_seconds", 0)]]
    clipped = [[float(a), min(float(b), float(a) + duration)] for a, b in intervals]
    end = max((b for a, b in clipped), default=0.0)
    return {**row, "intervals": clipped, "end_seconds": end,
            "active": any(a <= elapsed < b for a, b in clipped), "open_ended": False}


def party_boss_health(snapshot: dict, player_ids: set[str]) -> list[dict]:
    """Latest damage target per known party member, including split-Boss fights.

    A short mechanic/pause does not imply death; switching target or confirmed
    entity end removes the old row. No arbitrary inactivity timeout.
    """
    ended = set(snapshot.get("endedBossIds") or [])
    roster = {str(row.get("actor_id")) for row in snapshot.get("party_members", [])
              if row.get("actor_id") and float(row.get("confidence") or 0) >= 0.65}
    if roster:
        # An explicit roster outranks unrelated players observed earlier.
        player_ids = player_ids & (roster | {str(snapshot.get("selfSourceId") or "")})
    # 这里刻意保留「永久」目标表：队伍血条不该因为整队跑位 15 秒没输出就整排
    # 消失。而「Boss 行跟谁走」「分边按哪一侧过滤」用的是带时效的另一张表
    # （见 overlay_service.current_attack_targets）。
    attacks = snapshot.get("lastAttackTargets") or {}
    targets = {row.get("targetId") for actor, row in attacks.items() if actor in player_ids}
    return [row for row in snapshot.get("bossHealth", [])
            if row.get("targetId") in targets and row.get("targetId") not in ended
            and (row.get("currentHp") != 0 or row.get("partId") in (4150061, 4150062))]
