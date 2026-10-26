from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..data_loader import DATA_DIR, DataStore, as_number, norm
from .models import CombatEvent, SkillCastEvent


TIMELINE_EVIDENCE_SCHEMA_VERSION = "timeline-evidence-v1"
TIMELINE_EVIDENCE_PROFILE = (
    DATA_DIR / "profiles" / "timeline_cast_evidence.json"
)

# These names are protocol provenance, not skill rules.  Old serialized
# captures remain readable, while newly recovered casts use one explicit
# evidence provenance instead of the overloaded ``damage_inferred`` label.
LEGACY_DAMAGE_DERIVED_CAST_KINDS = frozenset(
    {"damage_inferred", "shared_timeline_damage"}
)
EVIDENCE_RECOVERED_CAST_KINDS = frozenset(
    {"evidence_recovered", "timeline_combo_recovered"}
)
TIMELINE_DERIVED_CAST_KINDS = (
    LEGACY_DAMAGE_DERIVED_CAST_KINDS | EVIDENCE_RECOVERED_CAST_KINDS
)

_UNRESOLVED_SKILL_IDS = frozenset({"", "-1", "unknown", "unresolved"})
_UNRESOLVED_SKILL_NAMES = frozenset(
    {"", "待识别伤害", "未学习伤害", "额外伤害", "unknown"}
)
_STRONG_ACTION_KINDS = frozenset(
    {"skill_action", "status_skill_action", "transformed_basic_attack"}
)
_SUPPORTED_ACTION_KINDS = frozenset(
    {
        "skill_cast",
        "compact_skill_input",
        "structured_multi_action",
        "direct",
        "legacy",
        "inferred_combo",
    }
)
_EXPLICIT_HIT_METHODS = frozenset(
    {
        "packet_skill_id",
        "direct_result_group_link",
        "direct_result_group_run_link",
    }
)
_LEGACY_DIRECT_HIT_METHODS = frozenset({"recorded", "direct"})
_CIRCULAR_HIT_METHODS = frozenset(
    {
        "cast_instance_match",
        "latest_profiled_direct_cast",
        "learned_cast_profile",
    }
)
_NON_CAST_METHODS = frozenset(
    {
        "action_free_special_attack",
        "appended_status_first_hit",
        "continuation_window",
        "healing_cast",
        "periodic_cast_fallback",
        "periodic_status",
        "periodic_status_packet",
        "refreshable_pulsing_aura",
        "skill_object_owner",
        "status_damage_packet",
        "supplemental_continuation",
        "supplemental_same_packet",
        "supplemental_summon_owner",
        "summon_owner",
        "packet_entity_template_owner",
        "packet_entity_status_damage",
        "packet_entity_damage_family",
        "packet_entity_self_attack",
        "packet_entity_projectile_selector",
        "packet_entity_skill_id",
        "sustained_cast_window",
        "unique_active_cast",
    }
)
_NON_CAST_METHOD_MARKERS = ("periodic", "status_damage", "summon", "skill_object")
_DEFAULT_CLUSTER_GAP_MS = 750
_MAX_CLUSTER_GAP_MS = 1_250
_CAST_EARLY_TOLERANCE_MS = 250
_CAST_LATE_TOLERANCE_MS = 750
_MIN_DAMAGE_WINDOW_MS = 3_000


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


@lru_cache(maxsize=8192)
def _compact_skill_name(value: str) -> str:
    return re.sub(r"\s+", "", _text(value)).replace("ＥＸ", "EX")


@lru_cache(maxsize=8192)
def _skill_family_name(value: str) -> str:
    compact = _compact_skill_name(value)
    compact = re.sub(r"(?:EX|ex)(?:等级\d+)?$", "", compact)
    compact = re.sub(r"等级\d+$", "", compact)
    return compact.casefold()


def timeline_skill_family(skill_id: object, skill_name: object) -> str:
    family = _skill_family_name(_text(skill_name))
    return family or _text(skill_id).casefold()


def is_legacy_damage_derived_cast(event: object) -> bool:
    return (
        _text(getattr(event, "packet_kind", "")).casefold()
        in LEGACY_DAMAGE_DERIVED_CAST_KINDS
    )


def is_timeline_recovered_cast(event: object) -> bool:
    return (
        _text(getattr(event, "packet_kind", "")).casefold()
        in TIMELINE_DERIVED_CAST_KINDS
    )


def is_authoritative_timeline_cast(event: SkillCastEvent) -> bool:
    return not is_timeline_recovered_cast(event)


def is_strong_action_cast(event: SkillCastEvent) -> bool:
    return _text(event.packet_kind).casefold() in _STRONG_ACTION_KINDS


def is_supported_action_cast(event: SkillCastEvent) -> bool:
    kind = _text(event.packet_kind).casefold()
    return kind in _STRONG_ACTION_KINDS or kind in _SUPPORTED_ACTION_KINDS


@dataclass(frozen=True, slots=True)
class TimelineHitEvidence:
    accepted: bool
    tier: str
    reason: str


def classify_timeline_hit(event: CombatEvent) -> TimelineHitEvidence:
    """Return whether one damage row can independently seed a cast.

    A factual Hit and a trustworthy skill link are separate requirements.
    Periodic, summon, additional-damage and template-derived ownership may
    remain valid damage, but they can never create a new timeline action.
    """

    if event.healing or int(event.amount) <= 0:
        return TimelineHitEvidence(False, "U", "not_positive_damage")
    skill_id = _text(event.skill_id).casefold()
    skill_name = _text(event.skill_name)
    if skill_id in _UNRESOLVED_SKILL_IDS or skill_name in _UNRESOLVED_SKILL_NAMES:
        return TimelineHitEvidence(False, "U", "unresolved_skill")
    if event.has_real_hit_evidence is not True:
        allow_legacy = os.environ.get(
            "DRAGON_NEST_TIMELINE_ALLOW_LEGACY_HITS", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        if not (allow_legacy and event.has_real_hit_evidence is None):
            return TimelineHitEvidence(False, "U", "no_real_hit_evidence")

    method = _text(event.attribution_method).casefold()
    raw_method = _text(event.raw_attribution_method).casefold()
    # The attribution ledger may attach a factual packet Hit to a cast
    # instance. Preserve the original packet provenance for timeline evidence;
    # a learned/circular raw method remains rejected below.
    if method in _CIRCULAR_HIT_METHODS and raw_method:
        method = raw_method
    if (
        method in _NON_CAST_METHODS
        or method in _CIRCULAR_HIT_METHODS
        or any(marker in method for marker in _NON_CAST_METHOD_MARKERS)
    ):
        return TimelineHitEvidence(False, "U", f"non_cast_method:{method}")
    if method == "direct_hit_overflow":
        if (
            raw_method in _CIRCULAR_HIT_METHODS
            or raw_method in _NON_CAST_METHODS
            or any(marker in raw_method for marker in _NON_CAST_METHOD_MARKERS)
        ):
            return TimelineHitEvidence(False, "D", "circular_direct_hit_overflow")
        return TimelineHitEvidence(True, "C", "independent_direct_hit_overflow")
    if method in _EXPLICIT_HIT_METHODS:
        return TimelineHitEvidence(True, "A", method)
    if method in _LEGACY_DIRECT_HIT_METHODS:
        return TimelineHitEvidence(True, "B", "recorded_real_hit")
    # Unknown methods are useful for discovery, not for inventing an action.
    return TimelineHitEvidence(False, "D", f"unsupported_method:{method or 'empty'}")


@dataclass(frozen=True, slots=True)
class TimelineCastDecision:
    source_id: str
    job: str
    skill_id: str
    skill_name: str
    first_hit_ms: int
    estimated_cast_ms: int
    hit_count: int
    accepted: bool
    confidence: float
    reason_codes: tuple[str, ...]
    evidence_tier: str
    profile_source: str


@dataclass(frozen=True, slots=True)
class TimelineEvidenceResult:
    authoritative_casts: tuple[SkillCastEvent, ...]
    recovered_casts: tuple[SkillCastEvent, ...]
    decisions: tuple[TimelineCastDecision, ...]

    @property
    def cast_events(self) -> tuple[SkillCastEvent, ...]:
        return tuple(
            sorted(
                (*self.authoritative_casts, *self.recovered_casts),
                key=lambda row: (row.timestamp_ms, row.source_id, row.skill_id),
            )
        )


@dataclass(frozen=True, slots=True)
class _RuntimeProfile:
    source: str
    corpus_recovery: bool
    persistent_only: bool
    training_sample_count: int
    file_count: int
    actor_count: int
    reliable_cast_ratio: float
    hit_count_p10: float
    hit_count_median: float
    hit_count_p90: float
    first_hit_seconds: float
    span_seconds: float


@lru_cache(maxsize=4)
def _load_profile_payload(path_text: str) -> Mapping[str, object]:
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("timeline_schema_version") != TIMELINE_EVIDENCE_SCHEMA_VERSION:
        return {}
    return payload


class TimelineEvidenceResolver:
    """Resolve one factual action axis from casts plus independent Hit evidence.

    There are deliberately no profession or skill allow/deny lists here.
    Decisions are made from protocol provenance, the current capture's own
    action/Hit relationship and a versioned corpus profile generated by the
    Hit Analyzer. Ambiguous damage stays damage and is never displayed as a
    cast.
    """

    def __init__(
        self,
        store: DataStore | None = None,
        profile_path: Path | None = None,
    ) -> None:
        self.store = store or DataStore.load()
        payload = _load_profile_payload(
            str((profile_path or TIMELINE_EVIDENCE_PROFILE).resolve())
        )
        self.profile_source = str(
            ((payload.get("source") or {}).get("analysis_signature") or "none")
            if isinstance(payload.get("source"), dict)
            else "none"
        )
        self._profiles: dict[tuple[str, str, str], _RuntimeProfile] = {}
        self._profiles_by_variant: dict[
            tuple[str, str], list[_RuntimeProfile]
        ] = defaultdict(list)
        rows = payload.get("profiles", {})
        if isinstance(rows, dict):
            for row in rows.values():
                if not isinstance(row, dict):
                    continue
                fixed_rows = row.get("fixed") or ()
                fixed = (
                    fixed_rows[0]
                    if isinstance(fixed_rows, list)
                    and fixed_rows
                    and isinstance(fixed_rows[0], dict)
                    else {}
                )
                key = self._profile_key(
                    row.get("job"), row.get("skill_name"), row.get("skill_id")
                )
                if not any(key):
                    continue
                runtime_profile = _RuntimeProfile(
                    source=(
                        "corpus"
                        if bool(row.get("corpus_recovery"))
                        else "corpus_observation"
                    ),
                    corpus_recovery=bool(row.get("corpus_recovery")),
                    persistent_only=bool(row.get("persistent_only")),
                    training_sample_count=int(
                        as_number(fixed.get("training_sample_count"))
                    ),
                    file_count=int(as_number(fixed.get("file_count"))),
                    actor_count=int(as_number(fixed.get("actor_count"))),
                    reliable_cast_ratio=as_number(
                        fixed.get("reliable_cast_ratio")
                    ),
                    hit_count_p10=as_number(fixed.get("hit_count_p10")),
                    hit_count_median=as_number(fixed.get("hit_count_median")),
                    hit_count_p90=as_number(fixed.get("hit_count_p90")),
                    first_hit_seconds=as_number(fixed.get("first_hit_seconds")),
                    span_seconds=as_number(fixed.get("span_seconds")),
                )
                self._profiles[key] = runtime_profile
                self._profiles_by_variant[key[:2]].append(runtime_profile)
        self._damage_families: dict[str, frozenset[str]] = {}
        self._learned_variants: dict[str, frozenset[str]] = {}
        self._timing_cache: dict[tuple[str, str], tuple[int, int, int]] = {}

    @staticmethod
    def _profile_key(
        job: object, skill_name: object, skill_id: object
    ) -> tuple[str, str, str]:
        return (
            norm(job).casefold(),
            _compact_skill_name(_text(skill_name)).casefold(),
            _text(skill_id).casefold(),
        )

    def _profile_for(self, event: CombatEvent | SkillCastEvent) -> _RuntimeProfile:
        exact = self._profile_key(event.job, event.skill_name, event.skill_id)
        profile = self._profiles.get(exact)
        if profile is not None:
            return profile
        # Some packet paths preserve the exact variant name but use a mapped
        # player-facing skill ID. Only fall back when job and variant remain
        # exact; never share evidence between professions.
        prefix = exact[:2]
        matches = self._profiles_by_variant.get(prefix, ())
        if len(matches) == 1:
            return matches[0]
        return _RuntimeProfile("none", False, False, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def _catalogued(self, event: CombatEvent | SkillCastEvent) -> bool:
        job = norm(event.job)
        families = self._damage_families.get(job)
        if families is None:
            loader = getattr(self.store, "skill_damage_entry_names", None)
            if not callable(loader):
                return True
            try:
                families = frozenset(
                    _skill_family_name(norm(name))
                    for name in loader(job)
                    if norm(name)
                )
            except Exception:
                families = frozenset()
            self._damage_families[job] = families
        return timeline_skill_family(event.skill_id, event.skill_name) in families

    def _learned(self, event: CombatEvent | SkillCastEvent) -> bool:
        job = norm(event.job)
        variants = self._learned_variants.get(job)
        if variants is None:
            loader = getattr(self.store, "full_skill_names", None)
            if not callable(loader):
                return True
            try:
                variants = frozenset(
                    _skill_family_name(norm(name))
                    for name in loader(job)
                    if norm(name)
                )
            except Exception:
                variants = frozenset()
            self._learned_variants[job] = variants
        # Damage tables are the stronger source for old/base skills not copied
        # into a second-job tree. Requiring either avoids rejecting valid base
        # actions while still excluding unrelated-profession packet aliases.
        family = timeline_skill_family(event.skill_id, event.skill_name)
        return family in variants or self._catalogued(event)

    def _timing(self, event: CombatEvent | SkillCastEvent) -> tuple[int, int, int]:
        key = (norm(event.job), timeline_skill_family(event.skill_id, event.skill_name))
        cached = self._timing_cache.get(key)
        if cached is not None:
            return cached
        skill_name = _compact_skill_name(_text(event.skill_name))
        base_name = re.sub(r"(?:EX|ex)(?:等级\d+)?$", "", skill_name)
        cast_seconds = 0.0
        cooldown_seconds = 0.0
        duration_seconds = 0.0
        try:
            row = self.store.skill_cast_time(skill_name, [norm(event.job)])
            cast_seconds = max(
                as_number(row.get("cast_time")), as_number(row.get("quick_time"))
            )
        except (AttributeError, TypeError, ValueError):
            pass
        for candidate in dict.fromkeys((skill_name, base_name)):
            if not candidate:
                continue
            try:
                row = self.store.skill_effect_timing(
                    norm(event.job), candidate, max_required_level=70
                )
            except (AttributeError, TypeError, ValueError):
                continue
            cooldown_seconds = max(cooldown_seconds, as_number(row.get("cooldown")))
            duration_seconds = max(
                duration_seconds,
                as_number(row.get("duration")),
                as_number(row.get("total_time")),
            )
        cast_ms = max(120, int(round(cast_seconds * 1000.0)))
        active_ms = max(_MIN_DAMAGE_WINDOW_MS, cast_ms + _CAST_LATE_TOLERANCE_MS)
        # Duration is intentionally not used to turn a status/summon lifetime
        # into one enormous action window. Proven direct channels are bounded
        # by their cast time; persistent methods were already rejected.
        active_ms = min(20_000, max(active_ms, min(12_000, int(duration_seconds * 1000))))
        cooldown_floor_ms = (
            max(1, int(round(cooldown_seconds * 0.30 * 1000.0)))
            if cooldown_seconds > 0
            else 0
        )
        result = (cast_ms, active_ms, cooldown_floor_ms)
        self._timing_cache[key] = result
        return result

    @staticmethod
    def _cluster_hit_count(cluster: Sequence[CombatEvent]) -> int:
        by_target: dict[str, int] = defaultdict(int)
        without_target = 0
        for event in cluster:
            if event.target_id:
                by_target[event.target_id] += 1
            else:
                without_target += 1
        return max([without_target, *by_target.values()], default=len(cluster))

    def _clusters(
        self, events: Sequence[CombatEvent]
    ) -> tuple[tuple[CombatEvent, ...], ...]:
        if not events:
            return ()
        sample = events[0]
        cast_ms, _active_ms, _cooldown_floor_ms = self._timing(sample)
        gap_ms = max(
            _DEFAULT_CLUSTER_GAP_MS,
            min(_MAX_CLUSTER_GAP_MS, int(round(cast_ms * 0.35))),
        )
        max_span_ms = max(1_250, cast_ms + _CAST_LATE_TOLERANCE_MS)
        result: list[list[CombatEvent]] = []
        for event in sorted(events, key=lambda row: row.timestamp_ms):
            if (
                not result
                or event.timestamp_ms - result[-1][-1].timestamp_ms > gap_ms
                or event.timestamp_ms - result[-1][0].timestamp_ms > max_span_ms
            ):
                result.append([event])
            else:
                result[-1].append(event)
        return tuple(tuple(cluster) for cluster in result)

    @staticmethod
    def _nearest_distance(timestamps: Sequence[int], value: int) -> int | None:
        if not timestamps:
            return None
        index = bisect_left(timestamps, value)
        distances = [
            abs(timestamps[candidate] - value)
            for candidate in (index - 1, index)
            if 0 <= candidate < len(timestamps)
        ]
        return min(distances) if distances else None

    def resolve(
        self,
        cast_events: Iterable[SkillCastEvent],
        combat_events: Iterable[CombatEvent],
    ) -> TimelineEvidenceResult:
        input_casts = tuple(cast_events)
        pre_recovered = tuple(
            event
            for event in input_casts
            if _text(event.packet_kind).casefold() in EVIDENCE_RECOVERED_CAST_KINDS
        )
        authoritative = tuple(
            sorted(
                (
                    event
                    for event in input_casts
                    if is_authoritative_timeline_cast(event)
                ),
                key=lambda row: (row.timestamp_ms, row.source_id, row.skill_id),
            )
        )
        casts_by_key: dict[tuple[str, str], list[SkillCastEvent]] = defaultdict(list)
        action_times_by_source: dict[str, list[int]] = defaultdict(list)
        for cast in authoritative:
            key = (cast.source_id, timeline_skill_family(cast.skill_id, cast.skill_name))
            casts_by_key[key].append(cast)
            if is_supported_action_cast(cast):
                action_times_by_source[cast.source_id].append(cast.timestamp_ms)
        recovered_times_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
        for cast in pre_recovered:
            key = (cast.source_id, timeline_skill_family(cast.skill_id, cast.skill_name))
            recovered_times_by_key[key].append(cast.timestamp_ms)
        for rows in casts_by_key.values():
            rows.sort(key=lambda row: row.timestamp_ms)
        for rows in action_times_by_source.values():
            rows.sort()

        evidence_by_key: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
        rejected: list[TimelineCastDecision] = []
        for event in combat_events:
            evidence = classify_timeline_hit(event)
            if not evidence.accepted:
                continue
            if not self._catalogued(event) or not self._learned(event):
                continue
            key = (event.source_id, timeline_skill_family(event.skill_id, event.skill_name))
            evidence_by_key[key].append(event)

        recovered: list[SkillCastEvent] = list(pre_recovered)
        decisions: list[TimelineCastDecision] = rejected
        for key, hits in evidence_by_key.items():
            known = casts_by_key.get(key, [])
            sample = hits[0]
            profile = self._profile_for(sample)
            cast_ms, active_ms, cooldown_floor_ms = self._timing(sample)
            clusters = self._clusters(hits)

            # Learn from this capture without feeding prior inferred casts back
            # into the model. Strong action packets and independent real Hits
            # are paired once in timestamp order.
            paired_counts: list[int] = []
            paired_delays: list[int] = []
            for cast in known:
                local = tuple(
                    hit
                    for hit in hits
                    if cast.timestamp_ms - _CAST_EARLY_TOLERANCE_MS
                    <= hit.timestamp_ms
                    <= cast.timestamp_ms + active_ms
                )
                if local:
                    paired_counts.append(self._cluster_hit_count(local))
                    paired_delays.append(
                        max(0, min(cast_ms, local[0].timestamp_ms - cast.timestamp_ms))
                    )
            local_recovery = (
                len(paired_counts) >= 2
                and len(paired_counts) >= max(2, math.ceil(len(known) * 0.40))
            )
            if paired_delays:
                ordered_delays = sorted(paired_delays)
                first_hit_delay_ms = ordered_delays[len(ordered_delays) // 2]
            elif profile.corpus_recovery:
                first_hit_delay_ms = max(
                    0,
                    min(cast_ms, int(round(profile.first_hit_seconds * 1000.0))),
                )
            else:
                first_hit_delay_ms = 0

            accepted_times = [
                *(cast.timestamp_ms for cast in known),
                *recovered_times_by_key.get(key, ()),
            ]
            accepted_times.sort()
            for cluster in clusters:
                first_hit_ms = cluster[0].timestamp_ms
                hit_count = self._cluster_hit_count(cluster)
                evidence_tiers = [classify_timeline_hit(row).tier for row in cluster]
                evidence_tier = min(
                    evidence_tiers,
                    key=lambda value: {"A": 3, "B": 2, "C": 1}.get(value, 0),
                )
                if any(
                    cast.timestamp_ms - _CAST_EARLY_TOLERANCE_MS
                    <= first_hit_ms
                    <= cast.timestamp_ms + active_ms
                    for cast in known
                ):
                    continue

                reason_codes: list[str] = []
                allowed = profile.corpus_recovery or local_recovery
                if profile.persistent_only:
                    allowed = False
                    reason_codes.append("persistent_profile")
                if profile.corpus_recovery:
                    reason_codes.append("corpus_profile")
                if local_recovery:
                    reason_codes.append("capture_local_profile")
                if not allowed:
                    reason_codes.append("insufficient_repeatable_evidence")

                if allowed and profile.corpus_recovery and profile.hit_count_median > 0:
                    lower = max(1, int(math.floor(profile.hit_count_p10 * 0.60)))
                    upper = max(
                        lower,
                        int(math.ceil(max(1.0, profile.hit_count_p90) * 1.40)),
                    )
                    if not lower <= hit_count <= upper:
                        allowed = False
                        reason_codes.append("corpus_hit_shape_mismatch")
                if allowed and local_recovery and paired_counts:
                    lower = max(1, int(math.floor(min(paired_counts) * 0.50)))
                    upper = max(lower, int(math.ceil(max(paired_counts) * 1.50)))
                    if not lower <= hit_count <= upper:
                        allowed = False
                        reason_codes.append("local_hit_shape_mismatch")

                estimated_cast_ms = max(0, first_hit_ms - first_hit_delay_ms)
                if allowed and cooldown_floor_ms > 0:
                    nearest = self._nearest_distance(accepted_times, estimated_cast_ms)
                    if nearest is not None and nearest < cooldown_floor_ms:
                        allowed = False
                        reason_codes.append("cooldown_conflict")

                # A Hit landing while a different action starts is not by
                # itself negative evidence (projectiles are delayed). It only
                # lowers confidence; profile/CD/shape still decide acceptance.
                nearby_action = self._nearest_distance(
                    action_times_by_source.get(sample.source_id, ()),
                    estimated_cast_ms,
                )
                confidence = 0.0
                if profile.corpus_recovery:
                    confidence += 0.55
                if local_recovery:
                    confidence += 0.30
                if evidence_tier == "A":
                    confidence += 0.15
                elif evidence_tier == "B":
                    confidence += 0.10
                else:
                    confidence += 0.05
                if nearby_action is not None and nearby_action <= 150:
                    confidence -= 0.05
                    reason_codes.append("concurrent_action_observed")
                confidence = max(0.0, min(1.0, confidence))

                if allowed:
                    recovered.append(
                        SkillCastEvent(
                            timestamp_ms=estimated_cast_ms,
                            source_id=cluster[0].source_id,
                            source_name=cluster[0].source_name,
                            job=cluster[0].job,
                            skill_id=cluster[0].skill_id,
                            skill_name=cluster[0].skill_name,
                            packet_kind="evidence_recovered",
                        )
                    )
                    accepted_times.append(estimated_cast_ms)
                    accepted_times.sort()
                    recovered_times_by_key[key].append(estimated_cast_ms)
                    reason_codes.append("accepted")
                decisions.append(
                    TimelineCastDecision(
                        source_id=cluster[0].source_id,
                        job=cluster[0].job,
                        skill_id=cluster[0].skill_id,
                        skill_name=cluster[0].skill_name,
                        first_hit_ms=first_hit_ms,
                        estimated_cast_ms=estimated_cast_ms,
                        hit_count=hit_count,
                        accepted=allowed,
                        confidence=confidence,
                        reason_codes=tuple(dict.fromkeys(reason_codes)),
                        evidence_tier=evidence_tier,
                        profile_source=(
                            "corpus+capture"
                            if profile.corpus_recovery and local_recovery
                            else "corpus"
                            if profile.corpus_recovery
                            else "capture"
                            if local_recovery
                            else profile.source
                        ),
                    )
                )

        return TimelineEvidenceResult(
            authoritative_casts=authoritative,
            recovered_casts=tuple(
                sorted(
                    recovered,
                    key=lambda row: (row.timestamp_ms, row.source_id, row.skill_id),
                )
            ),
            decisions=tuple(
                sorted(
                    decisions,
                    key=lambda row: (row.first_hit_ms, row.source_id, row.skill_id),
                )
            ),
        )


def timeline_evidence_resolver(
    store: DataStore | None = None,
) -> TimelineEvidenceResolver:
    return TimelineEvidenceResolver(store)
