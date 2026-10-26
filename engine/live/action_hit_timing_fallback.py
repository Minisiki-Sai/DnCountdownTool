from __future__ import annotations

import json
import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

from ..data_loader import DATA_DIR
from .models import CombatEvent, SkillCastEvent


CATALOG_SCHEMA = "action-hit-timing-v1"
ATTRIBUTION_METHOD = "action_hit_timing_fallback"
_UNRESOLVED_METHOD = "v2_unresolved"
_FACTUAL_ACTION_KINDS = frozenset(
    {
        "skill_action",
        # Non-compact ``skill_cast`` rows are decoded server cast records.
        # The cancellable C2S shortcut is emitted separately as
        # ``compact_skill_input`` and remains excluded.
        "skill_cast",
        "transformed_basic_attack",
        # This row is not a nearest-skill guess.  It is synthesized only when
        # two factual actions prove a configured hidden continuation (for
        # example 飓风舞 -> 踢飞 -> 双飞腿).  The continuation still has to
        # win the ordinary timing/ambiguity checks below and may consume only
        # one theoretical Hit slot.
        "inferred_combo",
    }
)


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _normal_name(value: object) -> str:
    return _normal_name_cached(_text(value))


@lru_cache(maxsize=8192)
def _normal_name_cached(value: str) -> str:
    name = re.sub(r"\s+", "", value).casefold()
    name = re.sub(r"(?:ex)(?:等级\d+)?$", "", name, flags=re.IGNORECASE)
    return re.sub(r"等级\d+$", "", name)


def _exact_name(value: object) -> str:
    """Normalize a name without merging the normal and EX identities."""
    return re.sub(r"\s+", "", _text(value)).casefold()


@dataclass(frozen=True, slots=True)
class ActionHitTimingProfile:
    job: str
    skill_id: str
    skill_name: str
    windows_ms: tuple[tuple[float, float], ...]

    @property
    def max_end_ms(self) -> float:
        return max((row[1] for row in self.windows_ms), default=0.0)


@dataclass(frozen=True, slots=True)
class ActionHitTimingResult:
    events: tuple[CombatEvent, ...]
    recovered_hits: int
    unresolved_hits: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_id: str
    job: str
    skill_id: str
    skill_name: str
    start_ms: int
    windows_ms: tuple[tuple[float, float], ...]
    identity_quality: int

    @property
    def max_end_ms(self) -> float:
        return max((row[1] for row in self.windows_ms), default=0.0)


class ActionHitTimingFallback:
    """Recover V2-unresolved direct Hits from factual action timing.

    This deliberately runs *after* strict V2.  It cannot overwrite packet,
    object, entity or status evidence, and it refuses unresolved rows that
    still carry an object/entity generation which a future decoder can join.
    The resulting method remains an inference and is excluded from profile
    training so it cannot confirm itself.
    """

    def __init__(
        self,
        catalog_path: Path | None = None,
        *,
        tolerance_ms: float = 120.0,
        known_damage_skill_ids: Iterable[object] = (),
        non_damage_skill_names: Iterable[object] = (),
        canonical_damage_skills: Mapping[
            tuple[object, object], tuple[object, object]
        ] | None = None,
    ) -> None:
        self.catalog_path = Path(
            catalog_path
            or DATA_DIR / "profiles" / "action_hit_timing.json"
        )
        self.tolerance_ms = max(1.0, float(tolerance_ms))
        self.known_damage_skill_ids = frozenset(
            _text(value) for value in known_damage_skill_ids if _text(value)
        )
        # The action timeline also contains effect pulses that are not damage
        # (heals, Buff applications and similar ticks).  Keep their normalized
        # identities separate from real damage capability.
        self.non_damage_skill_names = frozenset(
            _normal_name(value)
            for value in non_damage_skill_names
            if _normal_name(value)
        )
        # Runtime action IDs and damage-table IDs are different namespaces for
        # a small number of skills.  Keep the unpacked action ID for timing,
        # but publish the unique job+exact-name identity from skill_damage.
        # Exact-name matching deliberately preserves the EX suffix so a base
        # skill can never inherit an EX-only damage component.
        self.canonical_damage_skills: dict[
            tuple[str, str], tuple[str, str]
        ] = {}
        for raw_key, raw_value in (canonical_damage_skills or {}).items():
            if len(raw_key) < 2 or len(raw_value) < 2:
                continue
            key = (_text(raw_key[0]).casefold(), _exact_name(raw_key[1]))
            value = (_text(raw_value[0]), _text(raw_value[1]))
            if all(key) and all(value):
                self.canonical_damage_skills[key] = value
        self.schema = "missing"
        self.profiles: tuple[ActionHitTimingProfile, ...] = ()
        self.by_job_id: dict[tuple[str, str], tuple[ActionHitTimingProfile, ...]] = {}
        self.by_job_name: dict[tuple[str, str], tuple[ActionHitTimingProfile, ...]] = {}
        self.by_id_name: dict[tuple[str, str], tuple[ActionHitTimingProfile, ...]] = {}
        self.maximum_window_ms = 1_000
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8-sig"))
        except (OSError, TypeError, ValueError):
            return
        if _text(payload.get("schema")) != CATALOG_SCHEMA:
            return

        profiles: list[ActionHitTimingProfile] = []
        for raw in payload.get("profiles") or ():
            if not isinstance(raw, dict):
                continue
            windows: list[tuple[float, float]] = []
            for window in raw.get("windows_ms") or ():
                if not isinstance(window, list | tuple) or len(window) < 2:
                    continue
                try:
                    low, high = sorted((float(window[0]), float(window[1])))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(low) or not math.isfinite(high) or high < 0:
                    continue
                window = (max(0.0, low), max(0.0, high))
                # Preserve multiplicity when the unpacked action really has
                # simultaneous Hits.  A set silently collapsed those slots
                # and made one-to-one reconciliation impossible.
                windows.append(window)
            job = _text(raw.get("job"))
            skill_id = _text(raw.get("skill_id"))
            skill_name = _text(raw.get("skill_name"))
            if not job or not skill_id or not skill_name or not windows:
                continue
            profile = ActionHitTimingProfile(
                job=job,
                skill_id=skill_id,
                skill_name=skill_name,
                windows_ms=tuple(sorted(windows)),
            )
            profiles.append(profile)
            self.maximum_window_ms = max(
                self.maximum_window_ms, math.ceil(profile.max_end_ms)
            )

        def frozen_index(
            rows: Iterable[tuple[tuple[str, str], ActionHitTimingProfile]],
        ) -> dict[tuple[str, str], tuple[ActionHitTimingProfile, ...]]:
            grouped: dict[tuple[str, str], list[ActionHitTimingProfile]] = {}
            for key, profile in rows:
                grouped.setdefault(key, []).append(profile)
            return {key: tuple(value) for key, value in grouped.items()}

        self.profiles = tuple(profiles)
        self.by_job_id = frozen_index(
            ((_text(row.job).casefold(), row.skill_id), row) for row in profiles
        )
        self.by_job_name = frozen_index(
            ((_text(row.job).casefold(), _normal_name(row.skill_name)), row)
            for row in profiles
        )
        self.by_id_name = frozen_index(
            ((row.skill_id, _normal_name(row.skill_name)), row) for row in profiles
        )
        self.schema = CATALOG_SCHEMA

    def _profiles_for(
        self, cast: SkillCastEvent
    ) -> tuple[tuple[ActionHitTimingProfile, ...], int]:
        job = _text(cast.job).casefold()
        skill_id = _text(cast.skill_id)
        skill_name = _normal_name(cast.skill_name)
        rows = self.by_job_id.get((job, skill_id), ())
        if rows:
            named = tuple(row for row in rows if _normal_name(row.skill_name) == skill_name)
            return (named or rows), (3 if named else 2)
        rows = self.by_job_name.get((job, skill_name), ())
        if rows:
            return rows, 2
        rows = self.by_id_name.get((skill_id, skill_name), ())
        if rows:
            return rows, 1
        return (), 0

    def _candidates(
        self, casts: Iterable[SkillCastEvent]
    ) -> tuple[
        dict[str, list[_Candidate]],
        dict[str, list[int]],
        dict[str, list[int]],
    ]:
        candidates: dict[str, list[_Candidate]] = {}
        unknown_damage_starts: dict[str, list[int]] = {}
        seen: set[tuple[str, int, str, str]] = set()
        for cast in sorted(casts, key=lambda row: row.timestamp_ms):
            if _text(cast.packet_kind).casefold() not in _FACTUAL_ACTION_KINDS:
                continue
            key = (
                _text(cast.source_id),
                int(cast.timestamp_ms),
                _text(cast.skill_id),
                _text(cast.skill_name),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            profiles, quality = self._profiles_for(cast)
            if not profiles:
                if _text(cast.skill_id) in self.known_damage_skill_ids:
                    unknown_damage_starts.setdefault(key[0], []).append(key[1])
                continue
            # Multiple profiles here are alternative forms of one action,
            # not additive damage.  Keep one copy of a shared window while
            # retaining duplicate windows declared inside one concrete
            # profile (simultaneous Hits).
            if len(profiles) == 1:
                windows = profiles[0].windows_ms
            else:
                windows = tuple(
                    sorted(
                        {
                            window
                            for profile in profiles
                            for window in profile.windows_ms
                        }
                    )
                )
            job_name = _text(cast.job) or profiles[0].job
            canonical_id = ""
            canonical_name = ""
            # Prefer the packet/action display name, then the unpacked profile
            # name.  The latter covers action packets whose numeric action ID
            # is factual but whose display label is missing or generic.
            canonical_matches = {
                value
                for candidate_name in (
                    _text(cast.skill_name),
                    *(profile.skill_name for profile in profiles),
                )
                if (
                    value := self.canonical_damage_skills.get(
                        (job_name.casefold(), _exact_name(candidate_name))
                    )
                )
            }
            if len(canonical_matches) == 1:
                canonical_id, canonical_name = next(iter(canonical_matches))
            if not canonical_id:
                damage_ids = {
                    _text(cast.skill_id),
                    *(profile.skill_id for profile in profiles),
                }
                candidate_names = {
                    _normal_name(cast.skill_name),
                    *(_normal_name(profile.skill_name) for profile in profiles),
                }
                if (
                    self.known_damage_skill_ids.isdisjoint(damage_ids)
                    and not self.non_damage_skill_names.isdisjoint(
                        candidate_names
                    )
                ):
                    # A timing row alone proves that an action/effect occurred;
                    # it does not prove that the action can deal damage.  Keep
                    # the factual Hit unresolved instead of attaching it to a
                    # nearby heal/Buff pulse such as Chakra Heal or Energy
                    # Wheel.
                    # It is also an intervening factual action.  Without this
                    # blocker, removing the false heal candidate merely lets
                    # the same Hit fall through to an older damaging action,
                    # which changes one wrong label into another.
                    unknown_damage_starts.setdefault(key[0], []).append(
                        key[1]
                    )
                    continue
            candidates.setdefault(key[0], []).append(
                _Candidate(
                    source_id=key[0],
                    job=job_name,
                    skill_id=(
                        canonical_id
                        or _text(cast.skill_id)
                        or profiles[0].skill_id
                    ),
                    skill_name=(
                        canonical_name
                        or _text(cast.skill_name)
                        or profiles[0].skill_name
                    ),
                    start_ms=int(cast.timestamp_ms),
                    windows_ms=windows,
                    identity_quality=quality,
                )
            )
        starts: dict[str, list[int]] = {}
        for source_id, rows in candidates.items():
            rows.sort(key=lambda row: (row.start_ms, row.skill_id, row.skill_name))
            starts[source_id] = [row.start_ms for row in rows]
        for rows in unknown_damage_starts.values():
            rows.sort()
        return candidates, starts, unknown_damage_starts

    @staticmethod
    def _slot_timing_error(
        candidate: _Candidate,
        slot_index: int,
        timestamp_ms: int,
    ) -> tuple[float, float]:
        age_ms = float(timestamp_ms - candidate.start_ms)
        start_ms, end_ms = candidate.windows_ms[slot_index]
        error = (
            0.0
            if start_ms <= age_ms <= end_ms
            else min(abs(age_ms - start_ms), abs(age_ms - end_ms))
        )
        return error, max(0.0, end_ms - start_ms)

    @staticmethod
    def _same_skill(event: CombatEvent, candidate: _Candidate) -> bool:
        event_id = _text(event.skill_id)
        candidate_id = _text(candidate.skill_id)
        if event_id and event_id == candidate_id:
            return True
        event_name = _normal_name(event.skill_name)
        candidate_name = _normal_name(candidate.skill_name)
        return bool(event_name and event_name == candidate_name)

    def _reserve_proven_slots(
        self,
        events: tuple[CombatEvent, ...],
        candidates: dict[str, list[_Candidate]],
        starts: dict[str, list[int]],
    ) -> dict[_Candidate, set[int]]:
        """Reserve theoretical Hit slots already occupied by proven rows.

        Native ``0xFFFFFFFF`` rows used to be matched independently, so ten
        ambiguous rows could all attach to one one-Hit action.  Reserving
        packet/object/status-proven Hits first lets the fallback recover only
        genuinely missing theoretical rows from each cast.
        """

        used: dict[_Candidate, set[int]] = {}
        # An identity can match by ID OR normalized name. Index both while
        # retaining the original candidate ordinal for stable tie-breaking.
        by_identity: dict[tuple[str, str, str], list[int]] = {}
        for source_id, rows in candidates.items():
            for index, candidate in enumerate(rows):
                for kind, identity in (
                    ("id", _text(candidate.skill_id)),
                    ("name", _normal_name(candidate.skill_name)),
                ):
                    if identity:
                        by_identity.setdefault((source_id, kind, identity), []).append(index)
        for event in sorted(events, key=lambda row: row.timestamp_ms):
            if (
                event.healing
                or event.amount <= 0
                or event.has_real_hit_evidence is not True
                or _text(event.attribution_method).casefold()
                == _UNRESOLVED_METHOD
            ):
                continue
            source_id = _text(event.source_id)
            source_candidates = candidates.get(source_id, ())
            source_starts = starts.get(source_id, ())
            if not source_candidates or not source_starts:
                continue

            # Only actions whose maximum theoretical Hit window can still
            # reach this event are eligible.  The old implementation scanned
            # every cast made by the player for every proven Hit, which made
            # long captures quadratic (0918经典冰灵 contains tens of
            # thousands of both).  The candidate rows and their parallel
            # start-time array are already sorted, so two bisections preserve
            # the exact candidate set while making the lookup local.
            right = bisect_right(source_starts, event.timestamp_ms)
            left = bisect_left(
                source_starts,
                event.timestamp_ms
                - self.maximum_window_ms
                - self.tolerance_ms,
                0,
                right,
            )
            ranked: list[tuple[float, float, int, _Candidate, int]] = []
            eligible: set[int] = set()
            for kind, identity in (
                ("id", _text(event.skill_id)),
                ("name", _normal_name(event.skill_name)),
            ):
                indices = by_identity.get((source_id, kind, identity), ())
                eligible.update(indices[bisect_left(indices, left):bisect_left(indices, right)])
            for candidate_index in sorted(eligible):
                candidate = source_candidates[candidate_index]
                if event.timestamp_ms < candidate.start_ms:
                    continue
                if (
                    event.timestamp_ms - candidate.start_ms
                    > candidate.max_end_ms + self.tolerance_ms
                ):
                    continue
                occupied = used.setdefault(candidate, set())
                for slot_index in range(len(candidate.windows_ms)):
                    if slot_index in occupied:
                        continue
                    error_ms, width_ms = self._slot_timing_error(
                        candidate, slot_index, event.timestamp_ms
                    )
                    if error_ms > self.tolerance_ms:
                        continue
                    ranked.append(
                        (
                            error_ms + min(80.0, width_ms * 0.08),
                            error_ms,
                            -candidate.start_ms,
                            candidate,
                            slot_index,
                        )
                    )
            if not ranked:
                continue
            ranked.sort(
                key=lambda row: (
                    row[0],
                    row[1],
                    row[2],
                    row[3].skill_id,
                    row[4],
                )
            )
            _score, _error, _start, candidate, slot_index = ranked[0]
            used.setdefault(candidate, set()).add(slot_index)
        return used

    def _best(
        self,
        event: CombatEvent,
        candidates: dict[str, list[_Candidate]],
        starts: dict[str, list[int]],
        unknown_damage_starts: dict[str, list[int]],
        used_slots: dict[_Candidate, set[int]],
    ) -> tuple[_Candidate, int, float, float] | None:
        rows = candidates.get(_text(event.source_id), ())
        row_starts = starts.get(_text(event.source_id), ())
        if not rows or not row_starts:
            return None
        right = bisect_right(row_starts, event.timestamp_ms)
        ranked: list[tuple[float, float, int, int, _Candidate, int]] = []
        # Iterate the original sorted list by index. ``rows[:right]`` copied
        # the player's complete cast history once per unresolved Hit even
        # though the reverse loop immediately stopped outside the bounded
        # timing window.
        for candidate_index in range(right - 1, -1, -1):
            candidate = rows[candidate_index]
            age_ms = event.timestamp_ms - candidate.start_ms
            if age_ms > self.maximum_window_ms + self.tolerance_ms:
                break
            if age_ms < 0 or age_ms > candidate.max_end_ms + self.tolerance_ms:
                continue
            occupied = used_slots.get(candidate, set())
            for slot_index in range(len(candidate.windows_ms)):
                if slot_index in occupied:
                    continue
                error_ms, width_ms = self._slot_timing_error(
                    candidate, slot_index, event.timestamp_ms
                )
                if error_ms > self.tolerance_ms:
                    continue
                # Exact frame windows outrank broad collision/conditional
                # windows; an exact job+ID+name catalog identity outranks a
                # name fallback.
                score = error_ms + min(80.0, width_ms * 0.08)
                score += (3 - candidate.identity_quality) * 8.0
                ranked.append(
                    (
                        score,
                        error_ms,
                        -candidate.identity_quality,
                        -candidate.start_ms,
                        candidate,
                        slot_index,
                    )
                )
        if not ranked:
            return None
        ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4].skill_id))
        identity_rows: dict[
            tuple[str, str, int],
            tuple[float, float, int, int, _Candidate, int],
        ] = {}
        for row in ranked:
            identity = (
                row[4].skill_id,
                _normal_name(row[4].skill_name),
                row[4].start_ms,
            )
            identity_rows.setdefault(identity, row)
        leaders = sorted(
            identity_rows.values(),
            key=lambda row: (row[0], row[1], row[2], row[3], row[4].skill_id),
        )
        # EX-aware timelines can expose a still-active earlier action beside
        # the newly pressed skill.  Keep exact ties and close competitions
        # unresolved, but admit a clearly superior frame match.  This is
        # still visibly tagged as timing inference and never overwrites V2
        # packet/object/status evidence.
        identity_margin_ms = self.tolerance_ms
        if len(leaders) > 1:
            identity_margin_ms = leaders[1][0] - leaders[0][0]
            if identity_margin_ms < max(30.0, self.tolerance_ms * 0.25):
                return None
        _best_score, best_error, _quality, _start, best, slot_index = ranked[0]
        unknown = unknown_damage_starts.get(_text(event.source_id), ())
        if unknown:
            left = bisect_right(unknown, best.start_ms - 1)
            right_unknown = bisect_right(unknown, event.timestamp_ms)
            if right_unknown > left:
                return None
        return (
            best,
            slot_index,
            best_error,
            min(self.tolerance_ms, identity_margin_ms),
        )

    @staticmethod
    def _eligible(event: CombatEvent) -> bool:
        if (
            event.healing
            or event.amount <= 0
            or event.has_real_hit_evidence is not True
            or _text(event.attribution_method).casefold() != _UNRESOLVED_METHOD
        ):
            return False
        # Preserve incomplete object/entity generations for a future exact
        # decoder edge instead of hiding them behind a timing guess.
        return not bool(
            _text(event.cast_instance_id)
            or event.wire_damage_object_id
            or event.wire_entity_id
            or event.entity_template_id
        )

    def resolve(
        self,
        events: Iterable[CombatEvent],
        cast_events: Iterable[SkillCastEvent],
    ) -> ActionHitTimingResult:
        rows = tuple(events)
        candidates, starts, unknown_damage_starts = self._candidates(cast_events)
        used_slots = self._reserve_proven_slots(rows, candidates, starts)
        recovered = 0
        unresolved = 0
        output: list[CombatEvent] = []
        for event in rows:
            if not self._eligible(event):
                output.append(event)
                if (
                    not event.healing
                    and event.amount > 0
                    and _text(event.attribution_method).casefold() == _UNRESOLVED_METHOD
                ):
                    unresolved += 1
                continue
            match = self._best(
                event,
                candidates,
                starts,
                unknown_damage_starts,
                used_slots,
            )
            if match is None:
                output.append(event)
                unresolved += 1
                continue
            candidate, slot_index, error_ms, margin_ms = match
            confidence = min(
                0.86,
                0.58
                + 0.18 * max(0.0, 1.0 - error_ms / self.tolerance_ms)
                + 0.10 * min(1.0, margin_ms / self.tolerance_ms),
            )
            output.append(
                replace(
                    event,
                    job=candidate.job or event.job,
                    skill_id=candidate.skill_id,
                    skill_name=candidate.skill_name,
                    attribution_method=ATTRIBUTION_METHOD,
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method
                        or event.attribution_method
                    ),
                    # Do not invent a decoder object ID. Offline Hit analysis
                    # will bind this family back to the factual action cast.
                    cast_instance_id="",
                    attribution_confidence=confidence,
                )
            )
            used_slots.setdefault(candidate, set()).add(slot_index)
            recovered += 1
        return ActionHitTimingResult(tuple(output), recovered, unresolved)
