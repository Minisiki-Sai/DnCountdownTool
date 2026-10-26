from __future__ import annotations

from .event_platform import event_platform_for


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


def _excluded_from_score_for_low_damage(
    actual_power: float,
    theory_power: float,
) -> bool:
    """Exclude a scarcely realized theory skill from output scoring.

    A zero theoretical value has no meaningful damage ratio and preserves the
    observed-skill fallback used for skills absent from the default rotation.
    """

    return (
        theory_power > 1e-9
        and max(0.0, actual_power) / theory_power
        < _LOW_DAMAGE_SCORE_EXCLUSION_THRESHOLD
    )

def _high_value_skill_keys(
    theoretical_power_by_skill: dict[str, float],
) -> set[str]:
    """Return the skills covering the first 70% of theoretical damage."""

    positive_rows = [
        (key, value)
        for key, value in theoretical_power_by_skill.items()
        if value > 0
    ]
    total = sum(value for _key, value in positive_rows)
    if total <= 1e-12:
        return set()
    result: set[str] = set()
    cumulative = 0.0
    for key, value in sorted(
        positive_rows,
        key=lambda item: (-item[1], item[0]),
    ):
        result.add(key)
        cumulative += value
        if cumulative >= total * 0.70:
            break
    return result

def _confirmed_player_death_timestamps(
    snapshot: CombatSnapshot,
    source_id: str,
    windows: tuple[tuple[float, float], ...],
) -> tuple[float, ...]:
    """Return deduplicated recognized deaths inside the scoring scope."""

    evaluation_windows = _merge_intervals(windows)
    if not evaluation_windows:
        return ()

    # Only an authoritative absolute ``08 6F HP=0`` event confirms death.
    # Buff labels, output gaps and the noisy ``05 11 active=0`` state are not
    # accepted as substitutes.
    timestamps: list[float] = []
    for event in event_platform_for(snapshot).combat_events:
        if event.target_id != source_id and event.source_id != source_id:
            continue
        method = str(event.attribution_method or "").strip().casefold()
        if method not in _SURVIVAL_DEATH_EVENT_METHODS:
            continue
        timestamp = event.timestamp_ms / 1000.0
        if _in_intervals(timestamp, evaluation_windows):
            timestamps.append(timestamp)

    # One server-side death can be echoed by more than one status/event packet.
    deaths: list[float] = []
    previous = -math.inf
    for timestamp in sorted(timestamps):
        if timestamp - previous > 3.0:
            deaths.append(timestamp)
            previous = timestamp
    return tuple(deaths)


def _confirmed_player_death_count(
    snapshot: CombatSnapshot,
    source_id: str,
    windows: tuple[tuple[float, float], ...],
) -> int:
    """Count recognized deaths inside the selected scoring scope."""

    return len(_confirmed_player_death_timestamps(snapshot, source_id, windows))


def _danger_episode_totals(
    states: Iterable[tuple[float, bool]],
) -> tuple[int, float]:
    """Score danger by episode nadir while leaving fatal episodes to death score.

    The first sample at or below 30% opens an episode.  Its penalty is based on
    the lowest HP reached before recovery to 40%, rather than on packet sampling
    at the threshold crossing.  A confirmed fatal state closes and counts the
    episode but contributes no danger points, preventing one failure from being
    charged both danger and death points.
    """

    incident_count = 0
    raw_penalty = 0.0
    armed = True
    active = False
    minimum_ratio = 1.0

    def episode_penalty(ratio: float) -> float:
        depth = (
            _SURVIVAL_DANGER_HP_RATIO - ratio
        ) / _SURVIVAL_DANGER_HP_RATIO
        return 1.0 + 2.0 * max(0.0, min(1.0, depth))

    for raw_ratio, fatal in states:
        hp_ratio = max(0.0, min(1.0, as_number(raw_ratio)))
        if fatal:
            hp_ratio = 0.0

        if active:
            minimum_ratio = min(minimum_ratio, hp_ratio)
            if fatal:
                incident_count += 1
                active = False
                armed = False
                continue
            if hp_ratio >= _SURVIVAL_DANGER_HP_RECOVERY_RATIO:
                incident_count += 1
                raw_penalty += episode_penalty(minimum_ratio)
                active = False
                armed = True
            continue

        if hp_ratio >= _SURVIVAL_DANGER_HP_RECOVERY_RATIO:
            armed = True
            continue
        if not armed:
            continue
        if hp_ratio <= _SURVIVAL_DANGER_HP_RATIO:
            active = True
            armed = False
            minimum_ratio = hp_ratio
            if fatal:
                incident_count += 1
                active = False

    if active:
        incident_count += 1
        raw_penalty += episode_penalty(minimum_ratio)

    return incident_count, raw_penalty

def _snapshot_recording_end_seconds(snapshot: CombatSnapshot) -> float:
    """Return the latest factual timestamp, including post-output deaths.

    ``elapsed_seconds`` and ``dps_elapsed_seconds`` describe the visible output
    axis.  A player can die after their final damaging action, so those values
    are not the end of the recording and previously clipped terminal deaths
    from an all-Boss score.
    """

    return snapshot_recording_end_ms(snapshot) / 1000.0

def _hp_survival_shadow_detail(
    snapshot: CombatSnapshot,
    source_id: str,
    windows: tuple[tuple[float, float], ...],
    role: str,
    death_count: int,
) -> HpSurvivalShadowDetail:
    """Build a conservative survival result from authoritative HP snapshots.

    This intentionally remains a shadow calculation.  A loss is admitted only
    when two adjacent server snapshots belong to the same output interval, and
    tiny resource/rounding drift is ignored.  Simultaneous losses are compared
    as normalized HP ratios so a party mechanic contributes only the player's
    excess over the affected-party median.
    """

    evaluation_windows = _merge_intervals(windows)
    evaluated_seconds = sum(
        max(0.0, end - start) for start, end in evaluation_windows
    )
    tracks = {
        str(track.target_id): track
        for track in event_platform_for(snapshot).player_health_tracks
        if str(track.target_id)
    }
    own_track = tracks.get(str(source_id))
    death_factor = _SURVIVAL_TANK_DEATH_MULTIPLIER if role == "T/辅助" else 1.0
    death_penalty = (
        death_count * _SURVIVAL_POINTS_PER_CONFIRMED_DEATH * death_factor
    )
    if own_track is None or evaluated_seconds <= 0:
        return HpSurvivalShadowDetail(
            penalty=death_penalty,
            death_penalty=death_penalty,
        )

    def same_evaluation_window(left: float, right: float) -> bool:
        return any(
            start - 1e-9 <= left <= end + 1e-9
            and start - 1e-9 <= right <= end + 1e-9
            for start, end in evaluation_windows
        )

    damage_by_target: dict[str, list[CombatEvent]] = defaultdict(list)
    for event in event_platform_for(snapshot).incoming_damage_events:
        if event.target_id in tracks and event.amount > 0:
            damage_by_target[event.target_id].append(event)
    healing_by_target: dict[str, list[CombatEvent]] = defaultdict(list)
    for event in event_platform_for(snapshot).combat_events:
        if event.healing and event.target_id in tracks and event.amount > 0:
            healing_by_target[event.target_id].append(event)
    for rows in (*damage_by_target.values(), *healing_by_target.values()):
        rows.sort(key=lambda event: event.timestamp_ms)

    # timestamp, target_id, absolute loss, normalized loss, reference max HP
    losses: list[tuple[float, str, float, float, float]] = []
    fixed_mechanism_losses = []
    continuous_target_ids: set[str] = set()
    absolute_state_target_ids: set[str] = set()
    for target_id, track in tracks.items():
        maximum = max(0.0, as_number(getattr(track, "max_hp", 0.0)))
        points = tuple(
            sorted(
                getattr(track, "points", ()),
                key=lambda point: as_number(point.timestamp_seconds),
            )
        )
        if maximum <= 0:
            continue
        absolute_state_losses = tuple(
            event
            for event in damage_by_target.get(target_id, ())
            if event.attribution_method == "incoming_hp_state"
            and not (
                str(event.source_id)
                and str(event.source_id) == str(event.target_id)
            )
            and _in_intervals(
                event.timestamp_ms / 1000.0,
                evaluation_windows,
            )
        )
        if absolute_state_losses:
            absolute_state_target_ids.add(target_id)
            for event in absolute_state_losses:
                actual_loss = max(0.0, as_number(event.amount))
                if actual_loss <= 0:
                    continue
                if event.skill_id == "nightmare_sea_periodic_hp_loss":
                    fixed_mechanism_losses.append(event)
                    continue
                losses.append(
                    (
                        event.timestamp_ms / 1000.0,
                        target_id,
                        actual_loss,
                        actual_loss / maximum,
                        maximum,
                    )
                )
            # ``08 6F`` differences are already the final effective HP loss.
            # Replaying healing or direct-hit notifications around them would
            # count the same interval twice.
            continue
        usable_pairs = tuple(
            (previous, current)
            for previous, current in zip(points, points[1:])
            if (
                as_number(current.timestamp_seconds)
                > as_number(previous.timestamp_seconds)
                and as_number(current.timestamp_seconds)
                - as_number(previous.timestamp_seconds)
                <= _SURVIVAL_HP_SHADOW_MAX_SAMPLE_GAP_SECONDS
                and same_evaluation_window(
                    as_number(previous.timestamp_seconds),
                    as_number(current.timestamp_seconds),
                )
            )
        )
        if not usable_pairs:
            continue
        continuous_target_ids.add(target_id)
        noise_floor = max(100.0, maximum * _SURVIVAL_HP_SHADOW_NOISE_RATIO)
        target_state_events = sorted(
            (
                event.timestamp_ms / 1000.0,
                (
                    "heal"
                    if event.healing
                    else "self_loss"
                    if str(event.source_id)
                    and str(event.source_id) == str(event.target_id)
                    else "damage"
                ),
                max(0.0, as_number(event.amount)),
            )
            for event in (
                *damage_by_target.get(target_id, []),
                *healing_by_target.get(target_id, []),
            )
        )
        event_cursor = 0
        for previous, current in usable_pairs:
            previous_seconds = as_number(previous.timestamp_seconds)
            current_seconds = as_number(current.timestamp_seconds)
            ledger_hp = max(
                0.0,
                min(maximum, as_number(previous.current_hp)),
            )
            while (
                event_cursor < len(target_state_events)
                and target_state_events[event_cursor][0] <= previous_seconds
            ):
                event_cursor += 1
            interval_cursor = event_cursor
            while (
                interval_cursor < len(target_state_events)
                and target_state_events[interval_cursor][0] <= current_seconds
            ):
                event_seconds, kind, amount = target_state_events[interval_cursor]
                interval_cursor += 1
                if amount <= 0:
                    continue
                if kind == "heal":
                    ledger_hp = min(maximum, ledger_hp + amount)
                    continue
                actual_loss = min(ledger_hp, amount)
                if actual_loss <= 0:
                    continue
                ledger_hp -= actual_loss
                if kind == "self_loss":
                    continue
                losses.append(
                    (
                        event_seconds,
                        target_id,
                        actual_loss,
                        actual_loss / maximum,
                        maximum,
                    )
                )
            event_cursor = interval_cursor

            # The next server snapshot is authoritative.  A remaining negative
            # residual is real HP loss whose individual damage packet was not
            # decoded; a positive residual is healing and does not erase loss
            # already admitted above.
            authoritative_hp = max(
                0.0,
                min(maximum, as_number(current.current_hp)),
            )
            residual_loss = max(0.0, ledger_hp - authoritative_hp)
            if residual_loss >= noise_floor:
                losses.append(
                    (
                        current_seconds,
                        target_id,
                        residual_loss,
                        residual_loss / maximum,
                        maximum,
                    )
                )

    packet_evidence_target_ids: set[str] = set()
    for event in event_platform_for(snapshot).incoming_damage_events:
        target_id = str(event.target_id)
        if (
            not target_id
            or target_id in continuous_target_ids
            or target_id in absolute_state_target_ids
            or event.amount <= 0
            or (str(event.source_id) and str(event.source_id) == target_id)
            or not _in_intervals(
                event.timestamp_ms / 1000.0,
                evaluation_windows,
            )
        ):
            continue
        track = tracks.get(target_id)
        maximum = (
            max(0.0, as_number(getattr(track, "max_hp", 0.0)))
            or max(0.0, as_number(event.target_max_hp))
        )
        if maximum <= 0:
            continue
        actual_loss = min(maximum, max(0.0, as_number(event.amount)))
        if actual_loss <= 0:
            continue
        losses.append(
            (
                event.timestamp_ms / 1000.0,
                target_id,
                actual_loss,
                actual_loss / maximum,
                maximum,
            )
        )
        packet_evidence_target_ids.add(target_id)
    available_tracks = len(
        continuous_target_ids
        | absolute_state_target_ids
        | packet_evidence_target_ids
    )

    own_maximum = max(0.0, as_number(getattr(own_track, "max_hp", 0.0)))
    own_health_points = tuple(
        point
        for point in sorted(
            getattr(own_track, "points", ()),
            key=lambda point: as_number(point.timestamp_seconds),
        )
        if _in_intervals(
            as_number(point.timestamp_seconds),
            evaluation_windows,
        )
    )
    own_points = len(own_health_points)
    own_peak_samples = sum(
        1
        for point in own_health_points
        if own_maximum > 0
        and as_number(getattr(point, "current_hp", 0.0)) >= own_maximum * 0.99
    )
    exact = bool(getattr(own_track, "exact_percent", False))
    confidence = (
        "exact"
        if exact
        else "observed_full_plateau"
        if own_peak_samples >= 2
        else "observed_peak_only"
        if own_points >= 3
        else "limited_samples"
    )
    danger_incident_count = 0
    raw_danger_penalty = 0.0
    # Adjacent 08 6F states can already have been promoted to canonical
    # ``incoming_hp_state`` events above.  That is stronger evidence, not a
    # reason to mark the factual HP curve unavailable and fall back to a
    # capture-dependent damage/healing simulation.
    own_shadow_available = (
        str(source_id)
        in (continuous_target_ids | absolute_state_target_ids)
        and own_points >= 2
        and own_maximum > 0
    )
    for window_start, window_end in evaluation_windows:
        window_points = tuple(
            point
            for point in own_health_points
            if window_start - 1e-9
            <= as_number(point.timestamp_seconds)
            <= window_end + 1e-9
        )
        if not window_points or own_maximum <= 0:
            continue
        window_incidents, window_penalty = _danger_episode_totals(
            (
                (
                    as_number(point.current_hp) / own_maximum,
                    as_number(point.current_hp) <= 0,
                )
                for point in window_points
            )
        )
        danger_incident_count += window_incidents
        raw_danger_penalty += window_penalty
    danger_role_factor = (
        _SURVIVAL_HP_SHADOW_TANK_NON_DEATH_MULTIPLIER
        if role == "T/辅助"
        else 1.0
    )
    danger_penalty = min(
        _SURVIVAL_DANGER_PENALTY_CAP,
        raw_danger_penalty,
    ) * danger_role_factor
    if (not losses and not fixed_mechanism_losses) or own_maximum <= 0:
        return HpSurvivalShadowDetail(
            available=own_shadow_available,
            penalty=danger_penalty + death_penalty,
            death_penalty=death_penalty,
            non_death_penalty=danger_penalty,
            danger_incident_count=danger_incident_count,
            danger_penalty=danger_penalty,
            danger_available=own_shadow_available,
            danger_source=(
                "server_hp_snapshots"
                if own_shadow_available
                else ""
            ),
            danger_point_count=own_points,
            point_count=own_points,
            max_hp=own_maximum,
            max_hp_source=str(getattr(own_track, "max_hp_source", "")),
            exact_percent=exact,
            confidence=confidence,
        )

    waves: list[list[tuple[float, str, float, float, float]]] = []
    for loss in sorted(losses):
        if (
            not waves
            or loss[0] - waves[-1][0][0] > _SURVIVAL_HP_SHADOW_WAVE_SECONDS
        ):
            waves.append([loss])
        else:
            waves[-1].append(loss)

    fixed_own = [e for e in fixed_mechanism_losses if e.target_id == source_id]
    incoming_hp_loss = sum(e.amount for e in fixed_own)
    baseline_hp_loss = incoming_hp_loss
    avoidable_hp_loss = 0.0
    own_incidents = len(fixed_own)
    team_incidents = len(fixed_own)
    targeted_incidents = 0
    avoidable_incidents = 0
    uncertain_incidents = 0
    for wave in waves:
        by_target: dict[str, list[tuple[float, str, float, float, float]]] = (
            defaultdict(list)
        )
        for row in wave:
            by_target[row[1]].append(row)
        if source_id not in by_target:
            continue
        own_incidents += 1
        ratios_by_target = {
            target_id: sum(row[3] for row in rows)
            for target_id, rows in by_target.items()
        }
        losses_by_target = {
            target_id: sum(row[2] for row in rows)
            for target_id, rows in by_target.items()
        }
        target_count = len(by_target)
        if target_count >= 6:
            mechanism_class = "team_mechanism"
            team_incidents += 1
        elif target_count >= 3:
            mechanism_class = "targeted_mechanism"
            targeted_incidents += 1
        elif target_count >= 1:
            mechanism_class = "avoidable"
            avoidable_incidents += 1
        else:
            mechanism_class = "uncertain"
            uncertain_incidents += 1

        own_loss = losses_by_target[source_id]
        own_ratio = ratios_by_target[source_id]
        incoming_hp_loss += own_loss
        if mechanism_class in {"team_mechanism", "targeted_mechanism"}:
            sorted_ratios = sorted(ratios_by_target.values())
            middle = len(sorted_ratios) // 2
            baseline_ratio = (
                sorted_ratios[middle]
                if len(sorted_ratios) % 2
                else (sorted_ratios[middle - 1] + sorted_ratios[middle]) / 2.0
            )
            baseline = min(own_loss, baseline_ratio * own_maximum)
            affected_ratio = target_count / max(1, available_tracks)
            shared_responsibility = max(
                0.25,
                1.0 - 0.75 * affected_ratio,
            )
            avoidable = (
                max(0.0, own_ratio - baseline_ratio)
                * own_maximum
                * shared_responsibility
            )
        elif mechanism_class == "avoidable":
            baseline = 0.0
            avoidable = own_loss
        else:
            baseline = own_loss * (1.0 - _SURVIVAL_UNCERTAIN_DAMAGE_WEIGHT)
            avoidable = own_loss * _SURVIVAL_UNCERTAIN_DAMAGE_WEIGHT
        baseline_hp_loss += baseline
        avoidable_hp_loss += avoidable

    effective_minutes = max(1.0, evaluated_seconds / 60.0)
    avoidable_per_minute = (
        avoidable_hp_loss / own_maximum / effective_minutes
        if own_maximum > 0
        else 0.0
    )
    points = (
        (0.05, 0.0),
        (0.15, 2.5),
        (0.30, 6.0),
        (0.60, 12.0),
        (1.00, 18.0),
        (1.50, 20.0),
    )
    damage_penalty = 0.0
    if avoidable_per_minute > points[0][0]:
        damage_penalty = _SURVIVAL_DAMAGE_PENALTY_CAP
        for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
            if avoidable_per_minute <= right_x:
                ratio = (avoidable_per_minute - left_x) / max(
                    1e-9, right_x - left_x
                )
                damage_penalty = left_y + (right_y - left_y) * ratio
                break
    if role == "T/辅助":
        damage_penalty *= _SURVIVAL_HP_SHADOW_TANK_NON_DEATH_MULTIPLIER

    return HpSurvivalShadowDetail(
        available=own_shadow_available,
        penalty=damage_penalty + danger_penalty + death_penalty,
        damage_penalty=damage_penalty,
        death_penalty=death_penalty,
        non_death_penalty=damage_penalty + danger_penalty,
        incoming_hp_loss=incoming_hp_loss,
        team_mechanic_baseline_hp_loss=baseline_hp_loss,
        avoidable_hp_loss=avoidable_hp_loss,
        avoidable_hp_loss_per_minute=avoidable_per_minute,
        danger_incident_count=danger_incident_count,
        danger_penalty=danger_penalty,
        danger_available=own_shadow_available,
        danger_source=(
            "server_hp_snapshots"
            if own_shadow_available
            else ""
        ),
        danger_point_count=own_points,
        incident_count=own_incidents,
        team_mechanic_incident_count=team_incidents,
        targeted_mechanic_incident_count=targeted_incidents,
        avoidable_incident_count=avoidable_incidents,
        uncertain_incident_count=uncertain_incidents,
        point_count=own_points,
        max_hp=own_maximum,
        max_hp_source=str(getattr(own_track, "max_hp_source", "")),
        exact_percent=exact,
        confidence=confidence,
    )

def _simulated_danger_hp_result(
    snapshot: CombatSnapshot,
    source_id: str,
    windows: tuple[tuple[float, float], ...],
    maximum_hp: float,
    role: str,
    death_timestamps: tuple[float, ...] = (),
) -> tuple[int, float, int]:
    """Rebuild danger entries when no authoritative current-HP curve exists.

    The score snapshot already owns one canonical effective-loss stream.  Replay
    those losses together with server-confirmed effective healing from a full-HP
    start for each selected Boss interval.  This is deliberately subordinate to
    real HP snapshots and never replaces their result.
    """

    maximum = max(0.0, as_number(maximum_hp))
    evaluation_windows = _merge_intervals(windows)
    if maximum <= 0 or not evaluation_windows:
        return 0, 0.0, 0

    state_events = sorted(
        (
            *(
                (
                    event.timestamp_ms / 1000.0,
                    0,
                    -max(0.0, as_number(event.amount)),
                    False,
                )
                for event in event_platform_for(
                    snapshot
                ).event_index.incoming_by_target.get(
                    str(source_id), ()
                )
                if event.amount > 0
            ),
            *(
                (
                    event.timestamp_ms / 1000.0,
                    1,
                    max(0.0, as_number(event.amount)),
                    False,
                )
                for event in event_platform_for(
                    snapshot
                ).event_index.healing_by_target.get(
                    str(source_id), ()
                )
            ),
            *(
                (timestamp, 2, 0.0, True)
                for timestamp in death_timestamps
            ),
        ),
        key=lambda row: (row[0], row[1]),
    )
    incident_count = 0
    raw_penalty = 0.0
    point_count = 0
    for window_start, window_end in evaluation_windows:
        current_hp = maximum
        states: list[tuple[float, bool]] = [(1.0, False)]
        point_count += 1
        for seconds, _event_order, delta, fatal in state_events:
            if seconds < window_start - 1e-9:
                continue
            if seconds > window_end + 1e-9:
                break
            current_hp = (
                0.0
                if fatal
                else max(0.0, min(maximum, current_hp + delta))
            )
            point_count += 1
            hp_ratio = current_hp / maximum
            states.append((hp_ratio, fatal))
        window_incidents, window_penalty = _danger_episode_totals(states)
        incident_count += window_incidents
        raw_penalty += window_penalty

    role_factor = (
        _SURVIVAL_HP_SHADOW_TANK_NON_DEATH_MULTIPLIER
        if role == "T/辅助"
        else 1.0
    )
    penalty = min(_SURVIVAL_DANGER_PENALTY_CAP, raw_penalty) * role_factor
    return incident_count, penalty, point_count

def _snapshot_residual_hp_loss_events(
    snapshot: CombatSnapshot,
    windows: tuple[tuple[float, float], ...],
) -> tuple[CombatEvent, ...]:
    """Expose unexplained authoritative HP drops at their server timestamps."""

    evaluation_windows = _merge_intervals(windows)
    if not evaluation_windows:
        return ()

    def same_window(left: float, right: float) -> bool:
        return any(
            start - 1e-9 <= left <= end + 1e-9
            and start - 1e-9 <= right <= end + 1e-9
            for start, end in evaluation_windows
        )

    state_by_target: dict[str, list[tuple[float, int, float]]] = defaultdict(list)
    absolute_state_targets = {
        str(event.target_id)
        for event in event_platform_for(snapshot).incoming_damage_events
        if event.target_id
        and event.attribution_method == "incoming_hp_state"
        and _in_intervals(
            event.timestamp_ms / 1000.0,
            evaluation_windows,
        )
    }
    for event in event_platform_for(snapshot).incoming_damage_events:
        if event.target_id and event.amount > 0:
            state_by_target[str(event.target_id)].append(
                (
                    event.timestamp_ms / 1000.0,
                    0,
                    -max(0.0, as_number(event.amount)),
                )
            )
    for event in event_platform_for(snapshot).combat_events:
        if event.healing and event.target_id and event.amount > 0:
            state_by_target[str(event.target_id)].append(
                (
                    event.timestamp_ms / 1000.0,
                    1,
                    max(0.0, as_number(event.amount)),
                )
            )
    for rows in state_by_target.values():
        rows.sort(key=lambda row: (row[0], row[1]))

    residual_events: list[CombatEvent] = []
    for track in event_platform_for(snapshot).player_health_tracks:
        target_id = str(track.target_id)
        if target_id in absolute_state_targets:
            # The canonical incoming stream is already the exact difference
            # between adjacent ``08 6F`` states. Residual replay is legacy-only.
            continue
        maximum = max(0.0, as_number(track.max_hp))
        points = tuple(
            sorted(track.points, key=lambda point: point.timestamp_seconds)
        )
        if maximum <= 0 or len(points) < 2:
            continue
        noise_floor = max(100.0, maximum * _SURVIVAL_HP_SHADOW_NOISE_RATIO)
        target_events = state_by_target.get(target_id, ())
        event_cursor = 0
        for previous, current in zip(points, points[1:]):
            previous_seconds = as_number(previous.timestamp_seconds)
            current_seconds = as_number(current.timestamp_seconds)
            if (
                current_seconds <= previous_seconds
                or current_seconds - previous_seconds
                > _SURVIVAL_HP_SHADOW_MAX_SAMPLE_GAP_SECONDS
                or not same_window(previous_seconds, current_seconds)
            ):
                continue
            ledger_hp = max(
                0.0,
                min(maximum, as_number(previous.current_hp)),
            )
            while (
                event_cursor < len(target_events)
                and target_events[event_cursor][0] <= previous_seconds
            ):
                event_cursor += 1
            interval_cursor = event_cursor
            while (
                interval_cursor < len(target_events)
                and target_events[interval_cursor][0] <= current_seconds
            ):
                _seconds, _order, delta = target_events[interval_cursor]
                interval_cursor += 1
                ledger_hp = max(0.0, min(maximum, ledger_hp + delta))
            event_cursor = interval_cursor
            authoritative_hp = max(
                0.0,
                min(maximum, as_number(current.current_hp)),
            )
            residual_loss = max(0.0, ledger_hp - authoritative_hp)
            if residual_loss < noise_floor:
                continue
            residual_events.append(
                CombatEvent(
                    timestamp_ms=max(0, round(current_seconds * 1000.0)),
                    source_id="__hp_snapshot_residual__",
                    source_name="服务器HP快照",
                    job="",
                    skill_id="hp_snapshot_residual",
                    skill_name="快照确认生命损失",
                    amount=max(1, round(residual_loss)),
                    target_id=target_id,
                    target_name=track.target_name,
                    target_job=track.target_job,
                    target_max_hp=max(0, round(maximum)),
                    attribution_method="hp_snapshot_residual",
                )
            )
    residual_events.sort(key=lambda event: (event.timestamp_ms, event.target_id))
    return tuple(residual_events)

def _survival_discipline_detail(
    snapshot: CombatSnapshot,
    source_id: str,
    windows: tuple[tuple[float, float], ...],
    role: str,
    *,
    standard_hp: float = 0.0,
    actual_casts: Iterable[_ValuedCast] = (),
    theory_casts: Iterable[_TheoreticalCast] = (),
    death_windows: tuple[tuple[float, float], ...] | None = None,
) -> SurvivalDisciplineDetail:
    """Score avoidable damage severity and its observable rotation impact."""

    evaluation_windows = _merge_intervals(windows)
    evaluated_seconds = sum(
        max(0.0, end - start) for start, end in evaluation_windows
    )
    party_ids = {
        row.source_id for row in event_platform_for(snapshot).players
    }
    death_timestamps = _confirmed_player_death_timestamps(
        snapshot,
        source_id,
        _merge_intervals(death_windows or evaluation_windows),
    )
    death_count = len(death_timestamps)
    hp_shadow = _hp_survival_shadow_detail(
        snapshot,
        source_id,
        evaluation_windows,
        role,
        death_count,
    )
    life_loss_events = [
        row
        for row in (
            *event_platform_for(snapshot).incoming_damage_events,
            *_snapshot_residual_hp_loss_events(snapshot, evaluation_windows),
        )
        if row.target_id in party_ids
        and row.amount > 0
        and _in_intervals(row.timestamp_ms / 1000.0, evaluation_windows)
    ]
    self_sourced_hp_loss = sum(
        max(0.0, as_number(row.amount))
        for row in life_loss_events
        if str(row.target_id) == str(source_id)
        and str(row.source_id)
        and str(row.source_id) == str(row.target_id)
    )
    if self_sourced_hp_loss > 0:
        hp_shadow = replace(
            hp_shadow,
            incoming_hp_loss=(
                max(0.0, as_number(hp_shadow.incoming_hp_loss))
                + self_sourced_hp_loss
            ),
            self_sourced_hp_loss=self_sourced_hp_loss,
        )
    own_losses = [row for row in life_loss_events if str(row.target_id) == str(source_id)]
    if own_losses and all(
        str(row.source_id) == str(row.target_id)
        and str(row.skill_id) == "412"
        and str(row.job) in {"狂战士", "狂战"}
        for row in own_losses
    ):
        # Counter Roar can lower HP below the danger threshold in a harmless
        # training encounter. With no external loss at all, that is verified
        # skill expenditure, not a survivability failure. Keep HP and death
        # facts; other self losses and encounters with external damage retain
        # the existing danger policy.
        previous_danger_penalty = hp_shadow.danger_penalty
        hp_shadow = replace(
            hp_shadow,
            danger_incident_count=0,
            danger_penalty=0.0,
            danger_available=True,
            danger_source="verified_self_cost_only",
            penalty=max(0.0, hp_shadow.penalty - previous_danger_penalty),
            non_death_penalty=max(0.0, hp_shadow.non_death_penalty - previous_danger_penalty),
        )
    events = [
        row
        for row in life_loss_events
        if not (
            str(row.source_id)
            and str(row.source_id) == str(row.target_id)
        )
    ]
    events.sort(key=lambda row: (row.timestamp_ms, row.target_id))
    captured_reference_hp = max(
        (
            max(0.0, as_number(event.target_max_hp))
            for event in life_loss_events
            if str(event.target_id) == str(source_id)
        ),
        default=0.0,
    )
    snapshot_hp_track = next(
        (
            track
            for track in event_platform_for(snapshot).player_health_tracks
            if str(track.target_id) == str(source_id)
            and max(0.0, as_number(track.max_hp)) > 0
        ),
        None,
    )
    snapshot_reference_hp = (
        max(0.0, as_number(snapshot_hp_track.max_hp))
        if snapshot_hp_track is not None
        else 0.0
    )
    simulated_reference_hp = (
        max(0.0, as_number(hp_shadow.max_hp))
        or snapshot_reference_hp
        or captured_reference_hp
        or max(0.0, as_number(standard_hp))
    )
    if not hp_shadow.danger_available and simulated_reference_hp > 0:
        danger_count, danger_penalty, danger_points = (
            _simulated_danger_hp_result(
                snapshot,
                source_id,
                evaluation_windows,
                simulated_reference_hp,
                role,
                death_timestamps,
            )
        )
        hp_shadow = replace(
            hp_shadow,
            danger_incident_count=danger_count,
            danger_penalty=danger_penalty,
            danger_available=True,
            danger_source="simulated_damage_healing",
            danger_point_count=danger_points,
            max_hp=(
                max(0.0, as_number(hp_shadow.max_hp))
                or simulated_reference_hp
            ),
            max_hp_source=(
                (
                    snapshot_hp_track.max_hp_source
                    if snapshot_hp_track is not None
                    else ""
                )
                or hp_shadow.max_hp_source
                or (
                    "captured_max_hp"
                    if captured_reference_hp > 0
                    else "theory_plan"
                )
            ),
        )
    tank_role = role == "T/辅助"
    role_factor = _SURVIVAL_TANK_NON_DEATH_MULTIPLIER if tank_role else 1.0
    death_factor = _SURVIVAL_TANK_DEATH_MULTIPLIER if tank_role else 1.0
    death_penalty = (
        death_count * _SURVIVAL_POINTS_PER_CONFIRMED_DEATH * death_factor
    )
    if not events or source_id not in party_ids or evaluated_seconds <= 0:
        reference_hp = (
            max(0.0, as_number(hp_shadow.max_hp))
            or max(0.0, as_number(standard_hp))
        )
        return SurvivalDisciplineDetail(
            penalty=hp_shadow.danger_penalty + death_penalty,
            evaluated_seconds=evaluated_seconds,
            role_factor=role_factor,
            death_count=death_count,
            reference_hp=reference_hp,
            death_penalty=death_penalty,
            danger_incident_count=hp_shadow.danger_incident_count,
            danger_penalty=hp_shadow.danger_penalty,
            uncapped_non_death_penalty=hp_shadow.danger_penalty,
            hp_shadow=hp_shadow,
        )

    # Classify simultaneous HP-loss waves before assigning responsibility.
    # Shared and targeted mechanics use the affected players' median normalized
    # HP loss as their unavoidable baseline.  Only excess loss is penalized.
    reference_hp_by_target: dict[str, float] = {}
    for event in events:
        if event.target_max_hp > 0:
            reference_hp_by_target[event.target_id] = max(
                reference_hp_by_target.get(event.target_id, 0.0),
                float(event.target_max_hp),
            )
    if (
        reference_hp_by_target.get(source_id, 0.0) <= 0
        and as_number(standard_hp) > 0
    ):
        reference_hp_by_target[source_id] = as_number(standard_hp)

    known_party_ids = tuple(
        target_id
        for target_id in party_ids
        if reference_hp_by_target.get(target_id, 0.0) > 0
    )
    minimum_normalized_party = max(2, math.ceil(len(party_ids) * 0.5))
    can_use_normalized_team_baseline = (
        len(known_party_ids) >= minimum_normalized_party
    )

    event_avoidability: dict[int, float] = {}
    event_team_baseline: dict[int, float] = {}
    event_mechanism_class: dict[int, str] = {}
    wave: list[CombatEvent] = []

    def finish_wave() -> None:
        damage_by_target: dict[str, float] = defaultdict(float)
        events_by_target: dict[str, list[CombatEvent]] = defaultdict(list)
        for row in wave:
            damage_by_target[row.target_id] += max(0, row.amount)
            events_by_target[row.target_id].append(row)

        target_count = len(damage_by_target)
        target_ratio = target_count / max(1, len(party_ids))
        baseline_hp_ratio = 0.0
        if target_count >= 6:
            mechanism_class = "team_mechanism"
        elif target_count >= 3:
            mechanism_class = "targeted_mechanism"
        elif target_count >= 1:
            mechanism_class = "avoidable"
        else:
            mechanism_class = "uncertain"

        use_normalized_baseline = (
            can_use_normalized_team_baseline
            and target_count >= 2
            and all(
                reference_hp_by_target.get(target_id, 0.0) > 0
                for target_id in damage_by_target
            )
        )
        if use_normalized_baseline:
            normalized_losses = sorted(
                (
                    damage_by_target.get(target_id, 0.0)
                    / reference_hp_by_target[target_id]
                    if reference_hp_by_target.get(target_id, 0.0) > 0
                    else 0.0
                )
                for target_id in damage_by_target
            )
            middle = len(normalized_losses) // 2
            if len(normalized_losses) % 2:
                baseline_hp_ratio = normalized_losses[middle]
            else:
                baseline_hp_ratio = (
                    normalized_losses[middle - 1] + normalized_losses[middle]
                ) / 2.0

        if mechanism_class == "avoidable":
            fallback_weight = 1.0
        elif mechanism_class == "team_mechanism":
            fallback_weight = _SURVIVAL_PARTY_WIDE_DAMAGE_WEIGHT
        elif mechanism_class == "targeted_mechanism":
            fallback_weight = _SURVIVAL_TARGETED_DAMAGE_WEIGHT
        else:
            fallback_weight = _SURVIVAL_UNCERTAIN_DAMAGE_WEIGHT

        for target_id, target_events in events_by_target.items():
            raw_damage = damage_by_target[target_id]
            target_hp = reference_hp_by_target.get(target_id, 0.0)
            if use_normalized_baseline and target_hp > 0 and raw_damage > 0:
                baseline_damage = min(raw_damage, baseline_hp_ratio * target_hp)
                baseline_fraction = baseline_damage / raw_damage
                shared_responsibility = max(
                    0.25,
                    1.0 - 0.75 * target_ratio,
                )
                avoidability = (
                    max(0.0, raw_damage - baseline_damage)
                    / raw_damage
                    * shared_responsibility
                )
            else:
                avoidability = fallback_weight
                baseline_fraction = 1.0 - avoidability
            for row in target_events:
                event_avoidability[id(row)] = avoidability
                event_mechanism_class[id(row)] = mechanism_class
                event_team_baseline[id(row)] = max(
                    0.0,
                    max(0, row.amount) * baseline_fraction,
                )

    for event in events:
        seconds = event.timestamp_ms / 1000.0
        if (
            wave
            and seconds - wave[0].timestamp_ms / 1000.0
            > _SURVIVAL_SHARED_WAVE_SECONDS
        ):
            finish_wave()
            wave = []
        wave.append(event)
    if wave:
        finish_wave()

    player_events = [row for row in events if row.target_id == source_id]
    incidents: list[list[CombatEvent]] = []
    for event in player_events:
        seconds = event.timestamp_ms / 1000.0
        if (
            not incidents
            or seconds - incidents[-1][-1].timestamp_ms / 1000.0
            > _SURVIVAL_INCIDENT_MERGE_SECONDS
        ):
            incidents.append([event])
        else:
            incidents[-1].append(event)

    incident_rows: list[tuple[float, float, float]] = []
    avoidable_damage = 0.0
    team_mechanic_baseline_damage = 0.0
    weighted_incidents = 0.0
    shared_incidents = 0
    team_mechanic_incidents = 0
    targeted_mechanic_incidents = 0
    avoidable_incidents = 0
    uncertain_incidents = 0
    free_incidents = 0.0
    for incident in incidents:
        raw_damage = sum(max(0, event.amount) for event in incident)
        weighted_damage = sum(
            max(0, event.amount) * event_avoidability.get(id(event), 1.0)
            for event in incident
        )
        weight = weighted_damage / raw_damage if raw_damage > 0 else 0.0
        avoidable_damage += weighted_damage
        incident_baseline_damage = sum(
            event_team_baseline.get(id(event), 0.0) for event in incident
        )
        team_mechanic_baseline_damage += incident_baseline_damage
        weighted_incidents += weight
        class_damage: dict[str, float] = defaultdict(float)
        for event in incident:
            class_damage[event_mechanism_class.get(id(event), "uncertain")] += max(
                0, event.amount
            )
        incident_class = max(class_damage, key=class_damage.get, default="uncertain")
        if (
            incident_class in {"team_mechanism", "targeted_mechanism"}
            and incident_baseline_damage > 1e-9
        ):
            shared_incidents += 1
        if incident_class == "team_mechanism":
            team_mechanic_incidents += 1
        elif incident_class == "targeted_mechanism":
            targeted_mechanic_incidents += 1
        elif incident_class == "avoidable":
            avoidable_incidents += 1
        else:
            uncertain_incidents += 1
        if weight <= 1e-9:
            free_incidents += 1.0
        incident_rows.append(
            (
                incident[-1].timestamp_ms / 1000.0,
                incident[-1].timestamp_ms / 1000.0
                + _SURVIVAL_INTERRUPTION_SECONDS,
                weight,
            )
        )

    observed_hp = max(
        (max(0, int(event.target_max_hp)) for event in player_events),
        default=0,
    )
    # Captured/proven maximum HP is authoritative. The theory build is only
    # the fallback for recordings that missed the 21 0C absolute sync.
    reference_hp = (
        snapshot_reference_hp
        or float(observed_hp)
        or max(0.0, as_number(standard_hp))
    )
    if not hp_shadow.available and reference_hp > 0:
        hp_shadow = replace(
            hp_shadow,
            max_hp=reference_hp,
            max_hp_source=(
                str(snapshot_hp_track.max_hp_source or "protocol_210c")
                if snapshot_hp_track is not None
                else "captured_max_hp"
                if observed_hp > 0
                else "theory_plan"
            ),
            confidence=(
                "snapshot_max_without_curve"
                if snapshot_hp_track is not None
                else "captured_max_without_curve"
                if observed_hp > 0
                else "theory_plan_fallback"
            ),
        )
    effective_minutes = max(1.0, evaluated_seconds / 60.0)
    avoidable_hp_loss_per_minute = (
        avoidable_damage / reference_hp / effective_minutes
        if reference_hp > 0
        else 0.0
    )
    team_mechanic_baseline_hp_loss_per_minute = (
        team_mechanic_baseline_damage / reference_hp / effective_minutes
        if reference_hp > 0
        else 0.0
    )

    def damage_penalty_for(loss_per_minute: float) -> float:
        points = (
            (0.05, 0.0),
            (0.15, 2.5),
            (0.30, 6.0),
            (0.60, 12.0),
            (1.00, 18.0),
            (1.50, 20.0),
        )
        value = max(0.0, loss_per_minute)
        if value <= points[0][0]:
            return 0.0
        for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
            if value <= right_x:
                ratio = (value - left_x) / max(1e-9, right_x - left_x)
                return left_y + (right_y - left_y) * ratio
        return _SURVIVAL_DAMAGE_PENALTY_CAP

    def merge_weighted_windows(
        rows: Iterable[tuple[float, float, float]],
    ) -> tuple[tuple[float, float, float], ...]:
        clipped: list[tuple[float, float, float]] = []
        for start, end, weight in rows:
            for clipped_start, clipped_end in _intersect_intervals(
                ((start, end),), evaluation_windows
            ):
                clipped.append((clipped_start, clipped_end, weight))
        merged: list[list[float]] = []
        for start, end, weight in sorted(clipped):
            if merged and start <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], end)
                merged[-1][2] = max(merged[-1][2], weight)
            else:
                merged.append([start, end, weight])
        return tuple((row[0], row[1], row[2]) for row in merged)

    actual_cast_rows = tuple(actual_casts)
    theory_cast_rows = tuple(theory_casts)
    actual_cast_seconds = tuple(
        as_number(getattr(cast, "seconds", 0.0))
        for cast in actual_cast_rows
        if max(0.0, as_number(getattr(cast, "power", 0.0))) > 0
    )
    theory_cast_seconds = tuple(
        as_number(getattr(cast, "seconds", 0.0))
        for cast in theory_cast_rows
        if max(0.0, as_number(getattr(cast, "power", 0.0))) > 0
    )

    def has_verified_operation_impact(start: float, end: float, weight: float) -> bool:
        if weight <= 1e-9:
            return False
        active_before = any(
            start - _SURVIVAL_OPERATION_BEFORE_SECONDS <= second < start
            for second in actual_cast_seconds
        )
        resumed_output = any(
            end < second <= start + _SURVIVAL_OPERATION_RECOVERY_SECONDS
            for second in actual_cast_seconds
        )
        theoretical_action = any(start <= second <= end for second in theory_cast_seconds)
        output_during_interruption = any(
            start <= second <= end for second in actual_cast_seconds
        )
        return (
            active_before
            and resumed_output
            and theoretical_action
            and not output_during_interruption
        )

    verified_incident_rows = tuple(
        row for row in incident_rows if has_verified_operation_impact(*row)
    )
    interruption_windows = merge_weighted_windows(verified_incident_rows)

    def cast_power_in_windows(casts: Iterable[Any]) -> float:
        total = 0.0
        for cast in casts:
            power = max(0.0, as_number(getattr(cast, "power", 0.0)))
            if power <= 0:
                continue
            seconds = as_number(getattr(cast, "seconds", 0.0))
            duration = max(
                0.0,
                as_number(getattr(cast, "utilized_duration", 0.0)),
                as_number(getattr(cast, "end_seconds", 0.0)) - seconds,
            )
            proportional = bool(getattr(cast, "proportional_output", False))
            for start, end, weight in interruption_windows:
                if proportional and duration > 1e-9:
                    overlap = max(0.0, min(seconds + duration, end) - max(seconds, start))
                    total += power * min(1.0, overlap / duration) * weight
                elif start - 1e-9 <= seconds <= end + 1e-9:
                    total += power * weight
        return total

    total_actual_power = sum(max(0.0, as_number(row.power)) for row in actual_cast_rows)
    total_theory_power = sum(max(0.0, as_number(row.power)) for row in theory_cast_rows)
    baseline_completion = min(
        1.0,
        total_actual_power / total_theory_power if total_theory_power > 1e-9 else 0.0,
    )
    local_actual_power = cast_power_in_windows(actual_cast_rows)
    local_theory_power = cast_power_in_windows(theory_cast_rows)
    interruption_loss = max(
        0.0,
        local_theory_power * baseline_completion - local_actual_power,
    )
    interruption_loss_ratio = (
        interruption_loss / total_theory_power if total_theory_power > 1e-9 else 0.0
    )
    damage_penalty = damage_penalty_for(avoidable_hp_loss_per_minute)
    interruption_penalty = min(
        _SURVIVAL_INTERRUPTION_PENALTY_CAP,
        interruption_loss_ratio / 0.08 * _SURVIVAL_INTERRUPTION_PENALTY_CAP,
    )
    damage_penalty *= role_factor
    interruption_penalty *= role_factor
    penalty = (
        damage_penalty
        + hp_shadow.danger_penalty
        + interruption_penalty
        + death_penalty
    )
    hp_shadow = replace(
        hp_shadow,
        penalty=(
            hp_shadow.damage_penalty
            + hp_shadow.danger_penalty
            + interruption_penalty
            + death_penalty
        ),
        death_penalty=death_penalty,
        non_death_penalty=(
            hp_shadow.damage_penalty
            + hp_shadow.danger_penalty
            + interruption_penalty
        ),
    )
    return SurvivalDisciplineDetail(
        penalty=penalty,
        incoming_damage=sum(row.amount for row in player_events),
        hit_count=len(player_events),
        incident_count=len(incidents),
        shared_incident_count=shared_incidents,
        team_mechanic_incident_count=team_mechanic_incidents,
        targeted_mechanic_incident_count=targeted_mechanic_incidents,
        avoidable_incident_count=avoidable_incidents,
        uncertain_incident_count=uncertain_incidents,
        weighted_incidents=weighted_incidents,
        free_incidents=free_incidents,
        evaluated_seconds=evaluated_seconds,
        role_factor=role_factor,
        death_count=death_count,
        reference_hp=reference_hp,
        team_mechanic_baseline_damage=team_mechanic_baseline_damage,
        team_mechanic_baseline_hp_loss_per_minute=(
            team_mechanic_baseline_hp_loss_per_minute
        ),
        avoidable_damage=avoidable_damage,
        avoidable_hp_loss_per_minute=avoidable_hp_loss_per_minute,
        damage_penalty=damage_penalty,
        danger_incident_count=hp_shadow.danger_incident_count,
        danger_penalty=hp_shadow.danger_penalty,
        interruption_loss=interruption_loss,
        interruption_loss_ratio=interruption_loss_ratio,
        interruption_penalty=interruption_penalty,
        verified_interruption_count=len(verified_incident_rows),
        death_penalty=death_penalty,
        uncapped_non_death_penalty=(
            damage_penalty
            + hp_shadow.danger_penalty
            + interruption_penalty
        ),
        hp_shadow=hp_shadow,
    )

def _apply_survival_penalty_cap(
    detail: SurvivalDisciplineDetail,
    score_basis: float,
    role: str,
    *,
    canonical_incoming_damage: int | None = None,
) -> SurvivalDisciplineDetail:
    """Promote HP loss evidence to the canonical survival score and cap it."""

    cap = max(
        _SURVIVAL_BASE_NON_DEATH_PENALTY_CAP,
        max(0.0, as_number(score_basis)) * _SURVIVAL_SCORE_CAP_RATIO,
    )
    hp_result = detail.hp_shadow
    if not hp_result.available:
        # No usable absolute/current HP curve: keep the packet-backed loss
        # classification and its theory-build HP normalization. The canonical
        # display amount still comes from the same effective incoming ledger.
        raw_non_death = max(
            0.0,
            detail.damage_penalty
            + detail.danger_penalty
            + detail.interruption_penalty,
        )
        non_death = min(cap, raw_non_death)
        death_penalty = max(0.0, detail.death_penalty)
        canonical_external_damage = (
            max(0, int(canonical_incoming_damage))
            if canonical_incoming_damage is not None
            else max(0, detail.incoming_damage)
        )
        hp_result = replace(
            hp_result,
            penalty=non_death + death_penalty,
            damage_penalty=detail.damage_penalty,
            death_penalty=death_penalty,
            non_death_penalty=non_death,
            non_death_penalty_cap=cap,
            incoming_hp_loss=(
                max(
                    max(0.0, as_number(hp_result.incoming_hp_loss)),
                    canonical_external_damage
                    + max(0.0, as_number(hp_result.self_sourced_hp_loss)),
                )
            ),
            team_mechanic_baseline_hp_loss=(
                detail.team_mechanic_baseline_damage
            ),
            avoidable_hp_loss=detail.avoidable_damage,
            avoidable_hp_loss_per_minute=(
                detail.avoidable_hp_loss_per_minute
            ),
            max_hp=(
                max(0.0, hp_result.max_hp)
                or max(0.0, detail.reference_hp)
            ),
            max_hp_source=(
                hp_result.max_hp_source
                or ("theory_plan" if detail.reference_hp > 0 else "")
            ),
            confidence=(
                hp_result.confidence
                if hp_result.max_hp_source
                else (
                    "theory_plan_fallback"
                    if detail.reference_hp > 0
                    else "unavailable"
                )
            ),
        )
        return replace(
            detail,
            penalty=non_death + death_penalty,
            incoming_damage=canonical_external_damage,
            death_penalty=death_penalty,
            non_death_penalty_cap=cap,
            uncapped_non_death_penalty=raw_non_death,
            hp_shadow=hp_result,
        )

    raw_non_death = max(
        0.0,
        hp_result.damage_penalty
        + hp_result.danger_penalty
        + detail.interruption_penalty,
    )
    non_death = min(cap, raw_non_death)
    death_penalty = max(0.0, detail.death_penalty)
    hp_result = replace(
        hp_result,
        penalty=non_death + death_penalty,
        death_penalty=death_penalty,
        non_death_penalty=non_death,
        non_death_penalty_cap=cap,
    )
    effective_minutes = max(1.0, detail.evaluated_seconds / 60.0)
    baseline_per_minute = (
        hp_result.team_mechanic_baseline_hp_loss
        / hp_result.max_hp
        / effective_minutes
        if hp_result.max_hp > 0
        else 0.0
    )
    return replace(
        detail,
        penalty=non_death + death_penalty,
        incoming_damage=(
            max(0, int(canonical_incoming_damage))
            if canonical_incoming_damage is not None
            else int(
                round(
                    max(
                        0.0,
                        hp_result.incoming_hp_loss
                        - hp_result.self_sourced_hp_loss,
                    )
                )
            )
        ),
        hit_count=hp_result.incident_count,
        incident_count=hp_result.incident_count,
        shared_incident_count=(
            hp_result.team_mechanic_incident_count
            + hp_result.targeted_mechanic_incident_count
        ),
        team_mechanic_incident_count=hp_result.team_mechanic_incident_count,
        targeted_mechanic_incident_count=(
            hp_result.targeted_mechanic_incident_count
        ),
        avoidable_incident_count=hp_result.avoidable_incident_count,
        uncertain_incident_count=hp_result.uncertain_incident_count,
        weighted_incidents=0.0,
        free_incidents=0.0,
        reference_hp=hp_result.max_hp,
        team_mechanic_baseline_damage=(
            hp_result.team_mechanic_baseline_hp_loss
        ),
        team_mechanic_baseline_hp_loss_per_minute=baseline_per_minute,
        avoidable_damage=hp_result.avoidable_hp_loss,
        avoidable_hp_loss_per_minute=hp_result.avoidable_hp_loss_per_minute,
        damage_penalty=hp_result.damage_penalty,
        danger_incident_count=hp_result.danger_incident_count,
        danger_penalty=hp_result.danger_penalty,
        death_penalty=death_penalty,
        non_death_penalty_cap=cap,
        uncapped_non_death_penalty=raw_non_death,
        hp_shadow=hp_result,
    )
