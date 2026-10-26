from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .models import (
    BossHealthEvent,
    BossHpPointSnapshot,
    BossHpTrackSnapshot,
    BossSpawnEvent,
    CombatEvent,
)


_DEFAULT_POINT_BUCKET_MS = 100
_COMPLETED_TRACK_REUSE_GAP_MS = 3_000


@dataclass(frozen=True, slots=True)
class FixedBossHpProfile:
    key: str
    label: str
    hints: tuple[str, ...]
    template_ids: frozenset[int]
    bosses: tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...]


def _bundled_boss_hp_profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "boss_hp_profiles.json"
    )


def normalize_boss_hp_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[·•・._\-—:：,，()（）\[\]【】]", "", text)


@lru_cache(maxsize=1)
def load_fixed_boss_hp_profiles() -> tuple[FixedBossHpProfile, ...]:
    try:
        payload = json.loads(
            _bundled_boss_hp_profile_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ()
    raw_profiles = payload.get("profiles", ()) if isinstance(payload, dict) else ()
    profiles: list[FixedBossHpProfile] = []
    for row in raw_profiles:
        if not isinstance(row, dict):
            continue
        bosses: list[tuple[str, tuple[str, ...], tuple[int, ...]]] = []
        for boss in row.get("bosses", ()):
            if not isinstance(boss, dict):
                continue
            name = str(boss.get("name", "") or "").strip()
            max_hp_values = boss.get("max_hp", ())
            if not isinstance(max_hp_values, (list, tuple)):
                max_hp_values = (max_hp_values,)
            values = tuple(
                max(0, int(value))
                for value in max_hp_values
                if isinstance(value, (int, float)) and int(value) > 0
            )
            if not name or not values:
                continue
            aliases = tuple(
                str(alias).strip()
                for alias in boss.get("aliases", ())
                if str(alias).strip()
            )
            bosses.append((name, aliases, values))
        if bosses:
            profiles.append(
                FixedBossHpProfile(
                    key=str(row.get("key", "") or "").strip(),
                    label=str(row.get("label", "") or "").strip(),
                    hints=tuple(
                        str(hint).strip()
                        for hint in row.get("hints", ())
                        if str(hint).strip()
                    ),
                    template_ids=frozenset(
                        int(template_id)
                        for template_id in row.get("template_ids", ())
                        if str(template_id).isdigit()
                    ),
                    bosses=tuple(bosses),
                )
            )
    return tuple(profiles)


def resolve_fixed_boss_hp_profile(
    boss_names: Iterable[object],
    *,
    capture_hint: object = "",
    target_ids: Iterable[object] = (),
) -> tuple[str, dict[str, tuple[int, ...]]]:
    """Match one fixed raid/difficulty table without mixing shared Boss names."""

    profiles = load_fixed_boss_hp_profiles()
    observed = {
        normalize_boss_hp_name(name)
        for name in boss_names
        if normalize_boss_hp_name(name)
    }
    hint = normalize_boss_hp_name(capture_hint)
    observed_template_ids = {
        int(template_text)
        for target_id in target_ids
        if (
            template_text := str(target_id or "").strip().rsplit("@", 1)[-1]
        ).isdigit()
    }
    profile_name_sets: dict[str, set[str]] = {}
    name_frequency: dict[str, int] = {}
    for profile in profiles:
        names = {
            normalize_boss_hp_name(alias)
            for name, aliases, _values in profile.bosses
            for alias in (name, *aliases)
        }
        profile_name_sets[profile.key] = names
        for name in names:
            name_frequency[name] = name_frequency.get(name, 0) + 1

    ranked: list[tuple[int, int, int, int, FixedBossHpProfile]] = []
    for profile in profiles:
        names = profile_name_sets.get(profile.key, set())
        matched = observed & names
        hint_match = max(
            (
                len(normalized)
                for raw_hint in profile.hints
                if (normalized := normalize_boss_hp_name(raw_hint))
                and normalized in hint
            ),
            default=0,
        )
        unique_matches = sum(name_frequency.get(name, 0) == 1 for name in matched)
        template_matches = len(observed_template_ids & profile.template_ids)
        ranked.append(
            (template_matches, hint_match, unique_matches, len(matched), profile)
        )
    ranked.sort(key=lambda row: row[:4], reverse=True)
    if not ranked or ranked[0][:4] == (0, 0, 0, 0):
        return "", {}
    if len(ranked) > 1 and ranked[0][:4] == ranked[1][:4]:
        return "", {}

    profile = ranked[0][4]
    references: dict[str, tuple[int, ...]] = {}
    for name, aliases, values in profile.bosses:
        for alias in (name, *aliases):
            normalized = normalize_boss_hp_name(alias)
            if normalized:
                references[normalized] = values
    return profile.label, references


def _is_boss_job(value: str) -> bool:
    return str(value or "").strip().partition(" ")[0].casefold() == "boss"


@dataclass(slots=True)
class _BossHpTrack:
    instance_id: str
    target_id: str
    target_name: str
    target_job: str
    max_hp: int
    current_hp: int | None
    first_ms: int
    last_ms: int
    confidence: str
    authoritative_max_hp: bool
    total_damage: int = 0
    total_healing: int = 0
    overkill_damage: int = 0
    points: list[BossHpPointSnapshot] = field(default_factory=list)
    complete: bool = False
    completion_source: str = ""
    completion_timestamp_ms: int | None = None
    combat_observed: bool = False


class BossHpLedger:
    """Build per-spawn Boss HP tracks from authoritative HP delta events."""

    def __init__(
        self,
        *,
        point_bucket_ms: int = _DEFAULT_POINT_BUCKET_MS,
        authoritative_max_hp: bool = True,
    ) -> None:
        self.point_bucket_ms = max(1, int(point_bucket_ms))
        self.authoritative_max_hp = bool(authoritative_max_hp)
        self.reset()

    def reset(self) -> None:
        self._tracks: list[_BossHpTrack] = []
        self._active_by_target: dict[str, _BossHpTrack] = {}
        self._serials: dict[str, int] = {}
        self._parts: dict[tuple[str, int], BossHealthEvent] = {}

    def _new_track(
        self,
        *,
        timestamp_ms: int,
        target_id: str,
        target_name: str,
        target_job: str,
        max_hp: int,
        confidence: str,
    ) -> _BossHpTrack:
        serial = self._serials.get(target_id, 0) + 1
        self._serials[target_id] = serial
        normalized_max_hp = max(0, int(max_hp or 0))
        authoritative_max_hp = self.authoritative_max_hp
        track = _BossHpTrack(
            instance_id=f"{target_id}#{serial}",
            target_id=target_id,
            target_name=str(target_name or "").strip(),
            target_job=str(target_job or "").strip(),
            max_hp=normalized_max_hp,
            current_hp=(
                normalized_max_hp
                if authoritative_max_hp and normalized_max_hp > 0
                else None
            ),
            first_ms=max(0, int(timestamp_ms)),
            last_ms=max(0, int(timestamp_ms)),
            confidence=(confidence if authoritative_max_hp else "damage_only"),
            authoritative_max_hp=authoritative_max_hp,
        )
        self._tracks.append(track)
        self._active_by_target[target_id] = track
        self._append_point(track, timestamp_ms, delta_hp=0, damage=0, healing=0)
        return track

    def ingest_spawn(self, event: BossSpawnEvent) -> None:
        target_id = str(event.target_id or "").strip()
        if not target_id:
            return
        self._parts = {k: v for k, v in self._parts.items() if k[0] != target_id}
        self._new_track(
            timestamp_ms=event.timestamp_ms,
            target_id=target_id,
            target_name=event.target_name,
            target_job=event.target_job,
            max_hp=event.target_max_hp,
            confidence="spawn_observed",
        )

    def ingest_health(self, event: BossHealthEvent) -> None:
        if event.part_id:
            from .boss_health_wire import SEA_PARTS
            if not event.target_id.endswith("@4237023") or event.part_id not in SEA_PARTS:
                return
            maximum = SEA_PARTS[event.part_id][1]
            if event.target_max_hp != maximum or not 0 <= event.current_hp <= maximum:
                return
            key = (event.target_id, event.part_id)
            previous = self._parts.get(key)
            parent = self._active_by_target.get(event.target_id)
            if parent and (parent.complete or parent.confidence == "packet_hp" and parent.current_hp == 0):
                return
            if previous:
                if event.timestamp_ms < previous.timestamp_ms:
                    return
                if event.current_hp == previous.current_hp:
                    return  # Repeated zero must not postpone the refresh anchor.
                if event.current_hp > previous.current_hp and not (
                    previous.current_hp == 0 and event.current_hp == maximum
                    and event.timestamp_ms - previous.timestamp_ms >= 59000
                ):
                    return  # Reject stale upward HP jumps; allow verified 60s refresh.
            self._parts[key] = event
            return  # Parts never enter damage totals, Boss tracks, or death detection.
        track = self._active_by_target.get(event.target_id)
        if track is None:
            track = self._new_track(
                timestamp_ms=event.timestamp_ms, target_id=event.target_id,
                target_name=event.target_name, target_job=event.target_job,
                max_hp=event.target_max_hp, confidence="packet_hp",
            )
        # A delayed status must not resurrect a confirmed-ended entity. Only
        # ingest_spawn starts a new life (and clears this entity's tombstone).
        if (track.complete and event.current_hp > 0) or (track.confidence == "packet_hp" and track.current_hp == 0):
            return
        if event.timestamp_ms < track.last_ms:
            return
        if track.confidence != "packet_hp":
            # Do not retain an assumed full-health point before the first observation.
            track.points.clear()
        track.confidence = "packet_hp"
        track.authoritative_max_hp = False
        track.max_hp = max(0, event.target_max_hp)
        previous = track.current_hp
        track.current_hp = max(0, event.current_hp)
        if 0 < track.current_hp < track.max_hp:
            track.combat_observed = True
        track.last_ms = event.timestamp_ms
        self._append_point(track, event.timestamp_ms,
                           delta_hp=0 if previous is None else track.current_hp - previous,
                           damage=0, healing=0, source=event.layout)

    def _event_targets_boss(self, event: CombatEvent) -> bool:
        return bool(
            event.target_id
            and (
                event.target_id in self._active_by_target
                or _is_boss_job(event.target_job)
            )
        )

    @staticmethod
    def _update_context(track: _BossHpTrack, event: CombatEvent) -> None:
        if event.target_name:
            track.target_name = event.target_name
        if event.target_job:
            track.target_job = event.target_job
        if track.max_hp <= 0 and event.target_max_hp > 0:
            track.max_hp = max(0, int(event.target_max_hp))
            if track.authoritative_max_hp:
                track.current_hp = max(
                    0,
                    min(
                        track.max_hp,
                        track.max_hp - track.total_damage + track.total_healing,
                    ),
                )
                track.confidence = "assumed_full"

    def ingest_combat(self, event: CombatEvent) -> None:
        # Boss attacks also establish engagement, even before players deal damage.
        attacker = self._active_by_target.get(event.source_id)
        if attacker is not None and event.amount > 0 and not event.healing:
            attacker.combat_observed = True
        if event.amount <= 0 or not self._event_targets_boss(event):
            return
        target_id = str(event.target_id).strip()
        track = self._active_by_target.get(target_id)
        if track is None:
            track = self._new_track(
                timestamp_ms=event.timestamp_ms,
                target_id=target_id,
                target_name=event.target_name,
                target_job=event.target_job,
                max_hp=event.target_max_hp,
                confidence=(
                    "assumed_full" if event.target_max_hp > 0 else "relative"
                ),
            )
        elif (
            not track.complete and track.confidence != "packet_hp"
            and
            track.current_hp == 0
            and event.timestamp_ms - track.last_ms >= _COMPLETED_TRACK_REUSE_GAP_MS
        ):
            # Spawn packets normally split reused actor IDs. This fallback
            # covers captures that begin after the relevant spawn message.
            track = self._new_track(
                timestamp_ms=event.timestamp_ms,
                target_id=target_id,
                target_name=event.target_name or track.target_name,
                target_job=event.target_job or track.target_job,
                max_hp=event.target_max_hp or track.max_hp,
                confidence="assumed_full",
            )
        self._update_context(track, event)
        if not event.healing:
            track.combat_observed = True

        amount = max(0, int(event.amount))
        applied_damage = 0
        applied_healing = 0
        delta_hp = amount if event.healing else -amount
        if track.current_hp is not None and track.confidence != "packet_hp":
            before = track.current_hp
            after = before + delta_hp
            if track.max_hp > 0:
                after = min(track.max_hp, after)
            after = max(0, after)
            track.current_hp = after
            if event.healing:
                applied_healing = max(0, after - before)
            else:
                applied_damage = max(0, before - after)
                track.overkill_damage += max(0, amount - applied_damage)
            delta_hp = after - before
        elif event.healing:
            applied_healing = amount
        else:
            applied_damage = amount

        track.total_damage += applied_damage
        track.total_healing += applied_healing
        track.last_ms = max(track.last_ms, max(0, int(event.timestamp_ms)))
        if track.confidence == "packet_hp":
            return
        self._append_point(
            track,
            event.timestamp_ms,
            delta_hp=delta_hp,
            damage=applied_damage,
            healing=applied_healing,
        )

    def ingest_completion(
        self,
        *,
        target_id: str,
        timestamp_ms: int,
        source: str,
    ) -> None:
        """Close one packet-confirmed Boss life without fabricating damage."""

        target_id = str(target_id or "").strip()
        track = self._active_by_target.get(target_id)
        if track is None or track.complete:
            return
        timestamp_ms = max(track.last_ms, max(0, int(timestamp_ms)))
        track.complete = True
        track.completion_source = str(source or "boss_completion_trigger")
        track.completion_timestamp_ms = timestamp_ms
        track.last_ms = timestamp_ms
        # Only an authoritative runtime maximum permits an absolute HP delta.
        # The desktop path intentionally uses damage-only tracks, so in that
        # path this adds a lifecycle point while leaving current_hp unknown.
        delta_hp = 0
        if track.confidence == "packet_hp":
            # Encounter completion is not proof that HP became zero (phase exits).
            return
        if track.current_hp is not None and track.current_hp > 0:
            delta_hp = -track.current_hp
            track.current_hp = 0
        self._append_point(
            track,
            timestamp_ms,
            delta_hp=delta_hp,
            damage=0,
            healing=0,
        )

    def _append_point(
        self,
        track: _BossHpTrack,
        timestamp_ms: int,
        *,
        delta_hp: int,
        damage: int,
        healing: int,
        source: str = "",
    ) -> None:
        timestamp_ms = max(0, int(timestamp_ms))
        current_percent = (
            track.current_hp / track.max_hp * 100.0
            if track.current_hp is not None and track.max_hp > 0
            and track.current_hp <= track.max_hp * 1.00001
            else None
        )
        point = BossHpPointSnapshot(
            timestamp_seconds=timestamp_ms / 1000.0,
            current_hp=track.current_hp,
            current_percent=current_percent,
            delta_hp=int(delta_hp),
            damage=max(0, int(damage)),
            healing=max(0, int(healing)),
            source=source,
        )
        if track.points:
            previous = track.points[-1]
            previous_bucket = int(
                previous.timestamp_seconds * 1000
            ) // self.point_bucket_ms
            current_bucket = timestamp_ms // self.point_bucket_ms
            if previous_bucket == current_bucket:
                track.points[-1] = replace(
                    point,
                    delta_hp=previous.delta_hp + point.delta_hp,
                    damage=previous.damage + point.damage,
                    healing=previous.healing + point.healing,
                )
                return
        track.points.append(point)

    def ended_target_ids(self) -> list[str]:
        """Packet-confirmed death or encounter exit, never estimated DPS depletion."""
        return [track.target_id for track in self._active_by_target.values()
                if track.complete or (track.confidence == "packet_hp" and track.current_hp == 0)]

    def overlay_rows(self) -> list[dict[str, object]]:
        """Current instances only; never copy the full historical HP curves."""
        rows = []
        for track in self._active_by_target.values():
            if track.complete or track.current_hp == 0 or not track.combat_observed:
                continue
            known = (track.current_hp is not None and track.max_hp > 0
                     and track.current_hp <= track.max_hp)
            rows.append(dict(instanceId=track.instance_id, targetId=track.target_id,
                name=track.target_name or "未知 Boss", currentHp=track.current_hp,
                maxHp=track.max_hp or None,
                percent=track.current_hp / track.max_hp * 100 if known else None,
                confidence=track.confidence, updatedSeconds=track.last_ms / 1000))
            for part_id in (4150061, 4150062):
                part = self._parts.get((track.target_id, part_id))
                if part is not None:
                    rows.append(dict(
                        instanceId=f"{track.instance_id}:part:{part_id}",
                        targetId=track.target_id, partId=part_id,
                        name=f"海龙 · {part.target_name}", currentHp=part.current_hp,
                        maxHp=part.target_max_hp,
                        percent=part.current_hp / part.target_max_hp * 100,
                        confidence="packet_hp", updatedSeconds=part.timestamp_ms / 1000))
        return rows

    def snapshots(self) -> tuple[BossHpTrackSnapshot, ...]:
        rows: list[BossHpTrackSnapshot] = []
        for track in self._tracks:
            current_percent = (
                track.current_hp / track.max_hp * 100.0
                if track.current_hp is not None and track.max_hp > 0
                and track.current_hp <= track.max_hp * 1.00001
                else None
            )
            rows.append(
                BossHpTrackSnapshot(
                    instance_id=track.instance_id,
                    target_id=track.target_id,
                    target_name=track.target_name,
                    target_job=track.target_job,
                    max_hp=track.max_hp,
                    current_hp=track.current_hp,
                    current_percent=current_percent,
                    total_damage=track.total_damage,
                    total_healing=track.total_healing,
                    overkill_damage=track.overkill_damage,
                    first_seconds=track.first_ms / 1000.0,
                    last_seconds=track.last_ms / 1000.0,
                    confidence=track.confidence,
                    complete=track.complete or track.current_hp == 0,
                    points=tuple(track.points),
                    completion_source=track.completion_source,
                    completion_timestamp_seconds=(
                        track.completion_timestamp_ms / 1000.0
                        if track.completion_timestamp_ms is not None
                        else None
                    ),
                )
            )
        return tuple(rows)
