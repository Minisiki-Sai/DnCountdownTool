"""Recipient-scoped danger-HP rescue scoring; never a second healing ledger.

Read canonical HP tracks and already reconciled healing amounts. This module
does not emit/attribute damage or healing, nor edit its inputs. Amounts used for
scoring are normalized to each recipient's best available maximum HP. Estimated
canonical tracks remain usable, but are explicitly distinguished from exact HP.
"""
from collections import defaultdict
from dataclasses import dataclass, field, replace
from math import isfinite

from .healing_attribution import HealingAttributionLedger
from .healing_exclusions import contained_in


RESCUE_METHOD = "danger-hp-recovery-v5-heal-block-fatal-burst"
RESCUE_SAFE_RATIO = 0.60
RESCUE_EPISODE_PENALTY = 2.0
RESCUE_PENALTY_CAP = 40.0
RESCUE_MIN_FATAL_WINDOW_SECONDS = 1.0


def danger_rescue_weight(delay: float) -> float:
    """100% through 3s, linearly to 50% at 8s, then stay at 50%."""
    return max(0.5, min(1.0, 1.0 - max(0.0, delay - 3.0) * 0.1))


@dataclass(frozen=True)
class RescueEpisode:
    target_id: str
    start_seconds: float
    end_seconds: float
    end_reason: str
    onset_known: bool
    needed_percent: float
    recovered: dict[str, float]
    weighted: dict[str, float]
    estimated_hp: bool = False
    # Other observed recovery resolves team need without inventing healer credit.
    other_weighted_percent: float = 0.0
    # Fraction of this episode alive. None is only for legacy/test callers.
    opportunity_weights: dict[str, float] | None = None


@dataclass(frozen=True)
class DangerRescueAudit:
    episodes: tuple[RescueEpisode, ...] = ()
    evaluated_targets: tuple[str, ...] = ()
    skipped_targets: tuple[str, ...] = ()
    needed_percent: float = 0.0
    recovered: dict[str, float] = field(default_factory=dict)
    weighted: dict[str, float] = field(default_factory=dict)
    estimated_targets: tuple[str, ...] = ()
    healer_ids: tuple[str, ...] = ()
    excluded_heal_block_count: int = 0
    excluded_fatal_burst_count: int = 0


def rescue_episode_penalty(audit: DangerRescueAudit, source_id: str,
                           healer_count: int) -> dict:
    """One team penalty budget per episode; allocate only unmet responsibility.

    Keep the HP/healing ledger unchanged. Very short fatal windows without
    recovery are conservatively left ungraded, not claimed as proven one-shots.
    A later death alone cannot erase responsibility; a direct lethal hit never
    opens an episode in the first place.
    """
    healer_ids = set(audit.healer_ids) or {
        source for row in audit.episodes for source in row.weighted
    } | {source_id}
    # Preserve count-only callers; production audits always carry the roster.
    while len(healer_ids) < max(1, healer_count):
        healer_ids.add(f"__rescue_unlisted_{len(healer_ids)}")
    healer_ids = tuple(sorted(healer_ids))
    short_fatal = [row for row in audit.episodes
                   if row.end_reason == "death"
                   and row.end_seconds - row.start_seconds < RESCUE_MIN_FATAL_WINDOW_SECONDS
                   and sum(row.recovered.values()) <= 1e-9]
    short_fatal_ids = {id(row) for row in short_fatal}
    candidates = [row for row in audit.episodes if row.needed_percent > 1e-9
                and id(row) not in short_fatal_ids
                and (row.end_seconds > row.start_seconds + 1e-9
                     or sum(row.recovered.values()) > 1e-9)]
    episodes = [row for row in candidates
                if row.opportunity_weights is None
                or any(row.opportunity_weights.values())
                or sum(row.weighted.values()) > 1e-9]
    count = len(episodes)
    scale = min(1.0, RESCUE_PENALTY_CAP / (RESCUE_EPISODE_PENALTY * count)) if count else 1.0
    details = []
    for row in episodes:
        opportunity = ({source: 1.0 for source in healer_ids}
                       if row.opportunity_weights is None else row.opportunity_weights)
        available = sum(max(0.0, opportunity.get(source, 0.0)) for source in healer_ids)
        contributions = {source: max(0.0, row.weighted.get(source, 0.0)) / row.needed_percent
                         for source in healer_ids}
        other = min(1.0, max(0.0, row.other_weighted_percent / row.needed_percent))
        team_completion = min(1.0, sum(contributions.values()) + other)
        expectations = {
            source: (1.0 - other) * max(0.0, opportunity.get(source, 0.0)) / available
            if available > 1e-9 else 0.0 for source in healer_ids
        }
        deficits = {source: max(0.0, expectations[source] - contributions[source])
                    for source in healer_ids}
        deficit_total = sum(deficits.values())
        team_penalty = RESCUE_EPISODE_PENALTY * (1.0 - team_completion) * scale
        # No capable responder: do not invent an obligation for a dead healer.
        if deficit_total <= 1e-9:
            team_penalty = 0.0
        share = (deficits.get(source_id, 0.0) / deficit_total
                 if team_penalty > 0.0 and deficit_total > 1e-9 else 0.0)
        contribution = contributions.get(source_id, 0.0)
        expected = expectations.get(source_id, 0.0)
        status = ("not_required" if team_completion >= 1.0 - 1e-9 and contribution <= 1e-9
                  else "no_opportunity" if expected <= 1e-9 and contribution <= 1e-9
                  else "contributed" if contribution > 1e-9 else "unmet")
        # Diagnostic only. A teammate saving the target is never personal 100%.
        completion = (None if status in ("not_required", "no_opportunity") else
                      min(1.0, contribution / expected) if expected > 1e-9 else 1.0)
        details.append({
            "target_id": row.target_id,
            "start_seconds": row.start_seconds, "end_seconds": row.end_seconds,
            "end_reason": row.end_reason, "estimated_hp": row.estimated_hp,
            "completion": completion * 100.0 if completion is not None else None,
            "status": status, "team_completion": team_completion * 100.0,
            "team_penalty": team_penalty, "penalty": team_penalty * share,
            "responsibility_share": share, "expected_share": expected,
            "contribution_percent": contribution * 100.0,
            "other_recovery_percent": other * 100.0,
            "available_healers": sum(opportunity.get(source, 0.0) > 0 for source in healer_ids),
        })
    personal = [row["completion"] for row in details if row["completion"] is not None]
    return {
        "completion": sum(personal) / len(personal) if personal else None,
        "team_completion": sum(row["team_completion"] for row in details) / count if count else None,
        "team_penalty": sum(row["team_penalty"] for row in details),
        "penalty": min(RESCUE_PENALTY_CAP, sum(row["penalty"] for row in details)),
        "scored_episode_count": count,
        "personal_evaluated_episode_count": len(personal),
        "not_required_episode_count": sum(row["status"] == "not_required" for row in details),
        "no_opportunity_episode_count": sum(row["status"] == "no_opportunity" for row in details),
        "excluded_episode_count": len(audit.episodes) - count,
        "excluded_no_responder_count": len(candidates) - count,
        "excluded_short_fatal_count": len(short_fatal),
        "excluded_heal_block_count": audit.excluded_heal_block_count,
        "excluded_fatal_burst_count": audit.excluded_fatal_burst_count,
        "min_fatal_window_seconds": RESCUE_MIN_FATAL_WINDOW_SECONDS,
        "penalty_scale": scale, "penalty_per_episode": RESCUE_EPISODE_PENALTY,
        "penalty_cap": RESCUE_PENALTY_CAP, "completion_basis": "personal_share_diagnostic",
        "penalty_basis": "shared_team_deficit",
        "episode_penalties": details,
    }


def _rescue_opportunities(episodes, tracks, lifecycle, healer_ids, group_unavailable=None):
    """Reuse reliable life states, never guess range or skill availability.

    Missing life evidence is not death. Estimated max HP remains usable, but a
    reconstructed HP zero alone cannot prove that the healer was unavailable.
    """
    states = defaultdict(dict)

    def put(source, seconds, alive):
        # Conflicting same-timestamp states conservatively retain death.
        states[source][seconds] = states[source].get(seconds, True) and alive

    for event in lifecycle:
        if event.target_id in healer_ids:
            put(event.target_id, event.timestamp_ms / 1000.0, bool(event.active))
    selected = {}
    for track in tracks:
        if track.target_id not in selected or track.exact_percent:
            selected[track.target_id] = track
    for track in selected.values():
        if track.target_id in healer_ids and track.state_source in ("", "absolute_086f"):
            for point in track.points:
                if isfinite(point.timestamp_seconds) and isfinite(point.current_hp):
                    put(track.target_id, point.timestamp_seconds, point.current_hp > 0)
    dead_windows = {}
    for source in healer_ids:
        opened = None
        windows = []
        for seconds, alive in sorted(states[source].items()):
            if not alive and opened is None:
                opened = seconds
            elif alive and opened is not None:
                windows.append((opened, seconds))
                opened = None
        if opened is not None:
            windows.append((opened, float("inf")))
        dead_windows[source] = windows
    result = []
    from .duty_groups import merge
    group_unavailable = group_unavailable or {}
    for row in episodes:
        weights = {}
        for source in healer_ids:
            excluded = merge((*dead_windows[source],
                              *group_unavailable.get((source, row.target_id), ())))
            duration = row.end_seconds - row.start_seconds
            unavailable = sum(max(0.0, min(row.end_seconds, end) - max(row.start_seconds, start))
                              for start, end in excluded)
            weights[source] = (max(0.0, 1.0 - unavailable / duration) if duration > 1e-9 else
                               float(not any(start <= row.start_seconds < end
                                             for start, end in excluded)))
        result.append(replace(row, opportunity_weights=weights))
    return result


def audit_danger_rescue(health_tracks, heals, windows, *, party_ids,
                        healer_ids, danger_ratio, lifecycle=(), incoming=(),
                        heal_block_intervals=None, fatal_burst_windows=None,
                        group_unavailable=None):
    """One danger episode per recipient, until safe/dead/phase end.

    HP <= danger_ratio enters; HP >= 60% leaves. Repeated hits do not restart
    the response clock. Additional non-self losses add recovery need. Potions,
    unknown healing and personal-only recovery consume need but earn no healer
    credit. Simultaneous heals share a safety-line cap proportionally.
    """
    if not 0 < danger_ratio < RESCUE_SAFE_RATIO:
        raise ValueError("danger threshold must be below the rescue safety line")
    health_tracks = tuple(health_tracks)
    lifecycle = tuple(lifecycle)
    heal_block_intervals = heal_block_intervals or {}
    fatal_burst_windows = fatal_burst_windows or {}
    scope = []
    for start, end in sorted(windows):
        if not (isfinite(start) and isfinite(end)) or end <= start:
            continue
        if scope and start <= scope[-1][1]:
            scope[-1] = (scope[-1][0], max(end, scope[-1][1]))
        else:
            scope.append((start, end))
    party = frozenset(party_ids)
    healers = frozenset(healer_ids)
    model_ledger = HealingAttributionLedger()
    by_target = defaultdict(list)
    for event in heals:
        if event.target_id in party and event.healing and event.amount > 0:
            by_target[event.target_id].append(event)
    lifecycle_by_target = defaultdict(list)
    for event in lifecycle:
        if event.target_id in party:
            lifecycle_by_target[event.target_id].append(event)
    self_losses = defaultdict(list)
    for event in incoming:
        if event.target_id in party and event.source_id == event.target_id and event.amount > 0:
            self_losses[event.target_id].append(event)
    tracks = {}
    for row in health_tracks:
        if (row.target_id not in party or not isfinite(row.max_hp)
                or row.max_hp <= 0 or not any(
                    isfinite(p.timestamp_seconds) and isfinite(p.current_hp)
                    and any(start <= p.timestamp_seconds <= end for start, end in scope)
                    for p in row.points)):
            continue
        previous = tracks.get(row.target_id)
        # Canonical snapshots normally have one track per actor. If callers
        # provide a fallback too, never let it replace usable exact evidence.
        if previous is None or (row.exact_percent and not previous.exact_percent):
            tracks[row.target_id] = row
    episodes = []
    suppressed_blocked_onsets = 0
    evaluated = set()
    estimated = set()
    for target, track in tracks.items():
        maximum = float(track.max_hp)
        estimated_hp = (not track.exact_percent
                        or track.state_source not in ("", "absolute_086f"))
        points = sorted((p for p in track.points if isfinite(p.timestamp_seconds)
                         and isfinite(p.current_hp)), key=lambda p: p.timestamp_seconds)
        if not points:
            continue
        safe_hp = maximum * RESCUE_SAFE_RATIO
        danger_hp = maximum * danger_ratio
        # Build one time index per target, then visit each event once per selected
        # phase. No synthetic healing is produced from an HP synchronization.
        timeline = defaultdict(lambda: {"points": [], "heals": [], "life": [],
                                       "self": 0.0, "block_start": False,
                                       "block_end": False})
        for point in points:
            timeline[round(point.timestamp_seconds * 1000)]["points"].append(point)
        for event in by_target[target]:
            timeline[event.timestamp_ms]["heals"].append(event)
        for event in lifecycle_by_target[target]:
            timeline[event.timestamp_ms]["life"].append(event)
        for event in self_losses[target]:
            timeline[event.timestamp_ms]["self"] += event.amount
        for block_start, block_end in heal_block_intervals.get(target, ()):
            timeline[round(block_start * 1000)]["block_start"] = True
            timeline[round(block_end * 1000)]["block_end"] = True
        rows = sorted(timeline.items())
        cursor = 0
        for start, end in scope:
            # No previous phase's danger episode or unresolved debt is carried
            # into another phase. A fresh low-HP observation can open one there.
            while cursor < len(rows) and rows[cursor][0] / 1000 < start:
                cursor += 1
            hp = None
            active = None
            dead = False
            awaiting_revive_anchor = False
            self_since_state = 0.0
            suppressed_self_danger = False
            blocked = any(left <= start < right
                          for left, right in heal_block_intervals.get(target, ()))
            blocked_onset_seen = False

            def open_episode(seconds, current, known):
                return {"start": seconds, "known": known,
                        "need": safe_hp - current, "remaining": safe_hp - current,
                        "excluded_self": 0.0, "other_weighted": 0.0,
                        "recovered": defaultdict(float), "weighted": defaultdict(float)}

            def close(seconds, reason):
                nonlocal active
                if active is not None:
                    episodes.append(RescueEpisode(
                        target, active["start"], seconds, reason, active["known"],
                        active["need"] / maximum * 100,
                        {k: v / maximum * 100 for k, v in active["recovered"].items()},
                        {k: v / maximum * 100 for k, v in active["weighted"].items()},
                        estimated_hp=estimated_hp,
                        other_weighted_percent=active["other_weighted"] / maximum * 100,
                    ))
                    active = None

            while cursor < len(rows) and rows[cursor][0] / 1000 <= end:
                timestamp, batch = rows[cursor]
                cursor += 1
                seconds = timestamp / 1000.0
                if batch["block_start"]:
                    close(seconds, "heal_block")
                    blocked = True
                    blocked_onset_seen = False
                if batch["block_end"]:
                    blocked = False
                if any(not event.active for event in batch["life"]):
                    close(seconds, "death")
                    dead = True
                    awaiting_revive_anchor = True
                    hp = None
                elif dead and any(event.active for event in batch["life"]):
                    dead = False
                    awaiting_revive_anchor = True
                self_since_state += batch["self"]
                # Reconciled heal deltas can have the same timestamp as their
                # confirming positive HP state. Consume them before that state,
                # otherwise the safety-line update would erase valid credit.
                if hp is not None and not dead:
                    total = sum(event.amount for event in batch["heals"])
                    if total > 0:
                        eligible = (min(total, max(0.0, safe_hp - hp), active["remaining"])
                                    if active is not None else 0.0)
                        if eligible > 0:
                            weight = (danger_rescue_weight(seconds - active["start"])
                                      if active["known"] else 0.5)
                            for event in batch["heals"]:
                                model = model_ledger.model_for(event.skill_name)
                                amount = eligible * event.amount / total
                                if event.source_id not in healers or model is None or model.target_scope == "self":
                                    active["other_weighted"] += amount * weight
                                    continue
                                active["recovered"][event.source_id] += amount
                                active["weighted"][event.source_id] += amount * weight
                            active["remaining"] -= eligible
                        if active is not None:
                            self_recovery = max(0.0, min(total, max(0.0, safe_hp - hp)) - eligible)
                            active["excluded_self"] = max(0.0, active["excluded_self"] - self_recovery)
                        hp = min(maximum, hp + total)
                        if hp >= safe_hp - 1e-9:
                            close(seconds, "safe")
                            suppressed_self_danger = False
                for point in batch["points"]:
                    evaluated.add(target)
                    if estimated_hp:
                        estimated.add(target)
                    current = max(0.0, min(maximum, float(point.current_hp)))
                    if current <= 0:
                        close(seconds, "death")
                        dead = True
                        awaiting_revive_anchor = True
                        hp = None
                        self_since_state = 0.0
                        continue
                    if dead or awaiting_revive_anchor:
                        # Positive HP following death is a new life, not a heal
                        # of the previous episode. Establish a new anchor first.
                        hp = current
                        dead = False
                        awaiting_revive_anchor = False
                        self_since_state = 0.0
                        continue
                    previous = hp
                    hp = current
                    if active is not None and previous is not None and current > previous:
                        # A factual HP recovery can remove the need to act, but
                        # cannot be attributed to any particular healer here.
                        recovered = min(active["remaining"], max(0.0, min(current, safe_hp) - previous))
                        weight = (danger_rescue_weight(seconds - active["start"])
                                  if active["known"] else 0.5)
                        active["other_weighted"] += recovered * weight
                        active["remaining"] -= recovered
                    if current >= safe_hp - 1e-9:
                        close(seconds, "safe")
                        suppressed_self_danger = False
                    elif active is not None:
                        active["excluded_self"] += min(self_since_state,
                                                       max(0.0, (previous or current) - current))
                        eligible_gap = max(0.0, safe_hp - current - active["excluded_self"])
                        difference = eligible_gap - active["remaining"]
                        if difference > 0:
                            active["remaining"] += difference
                            active["need"] += difference
                        else:
                            # Unattributed state recovery removes outstanding
                            # need, but cannot give an invented healer credit.
                            active["remaining"] = eligible_gap
                    elif blocked and current <= danger_hp + 1e-9:
                        if not blocked_onset_seen:
                            suppressed_blocked_onsets += 1
                            blocked_onset_seen = True
                    elif current <= danger_hp + 1e-9:
                        if self_since_state > 0:
                            suppressed_self_danger = True
                        if not suppressed_self_danger:
                            active = open_episode(
                                seconds, current,
                                previous is not None and previous > danger_hp)
                    self_since_state = 0.0
                if (batch["block_end"] and not dead and hp is not None
                        and hp <= danger_hp + 1e-9 and active is None
                        and not suppressed_self_danger):
                    active = open_episode(seconds, hp, False)
            close(end, "phase_end")
    # A target cannot receive treatment while healing is forbidden. A
    # confirmed >50%-HP one-second fatal collapse likewise offers no
    # actionable rescue window if the danger episode starts in that burst.
    fatal = [row for row in episodes if row.end_reason == "death"
             and contained_in(row.target_id, row.start_seconds,
                              fatal_burst_windows, include_start=True)
             and contained_in(row.target_id, row.end_seconds,
                              fatal_burst_windows)]
    fatal_ids = {id(row) for row in fatal}
    episodes = [row for row in episodes if id(row) not in fatal_ids]
    recovered = defaultdict(float)
    weighted = defaultdict(float)
    for episode in episodes:
        for source, amount in episode.recovered.items():
            recovered[source] += amount
        for source, amount in episode.weighted.items():
            weighted[source] += amount
    episodes = _rescue_opportunities(episodes, health_tracks, lifecycle, healers, group_unavailable)
    return DangerRescueAudit(tuple(episodes), tuple(sorted(evaluated)),
                             tuple(sorted(party - evaluated)),
                             sum(row.needed_percent for row in episodes),
                             dict(recovered), dict(weighted), tuple(sorted(estimated)), tuple(sorted(healers)),
                             suppressed_blocked_onsets, len(fatal))
