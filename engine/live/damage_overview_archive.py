"""Post-score factual damage export. Never participates in score calculation."""
from dataclasses import asdict
from .additional_damage_composition import attach_attributed_skill_views
from .event_platform import event_platform_for
from .death_report import build_death_reports


def build_damage_overview_archives(snapshot, *, realization_rows=(), encounters=(), hp_curves=(), realization_message=""):
    platform = event_platform_for(snapshot)
    payload = {"players": [asdict(p) for p in platform.players],
               "combat_events": [{} for _ in snapshot.combat_events]}
    attach_attributed_skill_views(payload, snapshot)
    # Names/profile identifiers are deliberately not archived: history privacy
    # must not be bypassed by a nested copy of the live roster.
    fields = ("job", "damage", "healing", "dps", "hits", "critical_rate", "critical_samples", "incoming_damage")
    team = [{**{k: p.get(k, 0) for k in fields}, "ordinal": i + 1}
            for i, p in enumerate(payload["players"])]
    targets = {str(t.target_id): t for t in platform.damage_targets}
    per_source = {}
    for event in snapshot.combat_events:
        if event.healing:
            continue
        key = str(event.source_id).upper()
        target = str(event.target_id)
        entry = per_source.setdefault(key, {}).setdefault(target, {"damage": 0, "hits": 0})
        entry["damage"] += event.amount
        entry["hits"] += 1
    result = {}
    aliases = {str(p["source_id"]).upper(): str(i + 1) for i, p in enumerate(payload["players"])}
    deaths = [{"actor_id": aliases[str(r.get("actor_id", "")).upper()],
               "death_timestamp_ms": r["death_timestamp_ms"], "death_seconds": r["death_seconds"]}
              for r in build_death_reports(snapshot)
              if str(r.get("actor_id", "")).upper() in aliases]
    # Reuse the exact full-capture realization ledger, not the scoring-window
    # completion rate. Unknown remains null; percentage points stay unchanged.
    members = []
    for i, p in enumerate(payload["players"]):
        member = {k: v for k, v in p.items()
                  if k in fields or k == "damage_series" or k.startswith("skills")}
        member.update(ordinal=i + 1, source_id=str(i + 1), name=f"{i + 1} · {p.get('job') or '未知职业'}")
        member["realizations"] = [{k: v for k, v in r.items() if k != "source_id"}
                                  for r in realization_rows
                                  if str(r.get("source_id", "")).upper() == str(p["source_id"]).upper()]
        source = per_source.get(str(p["source_id"]).upper(), {})
        member["boss_rows"] = [dict(id=str(j), name=e.name, damage=e.damage,
            personalDamage=sum(source.get(str(t), {}).get("damage", 0) for t in e.target_ids),
            ranges=[dict(start=a, end=b) for a, b in (e.phase_windows or ((e.first_ms / 1000, e.last_ms / 1000),))])
            for j, e in enumerate(encounters)]
        members.append(member)
    for i, player in enumerate(payload["players"]):
        key = str(player["source_id"]).upper()
        boss_rows = []
        for target_id, values in per_source.get(key, {}).items():
            target = targets.get(target_id)
            boss_rows.append({**values, "name": target.target_name if target else "未知目标",
                              "max_hp": target.target_max_hp if target else None})
        result[key] = {"v": 1, "elapsed_seconds": platform.elapsed_seconds,
                       "total_damage": platform.total_damage, "team": team, "selected": i + 1,
                       "player": {k: v for k, v in player.items()
                                  if k in fields or k == "damage_series" or k.startswith("skills")},
                       "targets": sorted(boss_rows, key=lambda r: r["damage"], reverse=True),
                       "members": members, "damage_series": platform.damage_series,
                       "death_reports": deaths,
                       "boss_hp_curves": list(hp_curves),
                       "realization_message": realization_message,
                       "realizations": members[i]["realizations"]}
    return result
