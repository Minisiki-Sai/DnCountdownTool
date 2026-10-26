from __future__ import annotations

import re
import unicodedata
import json
import os
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable

from ..data_loader import DataStore
from .additional_damage_triggers import link_additional_damage_triggers
from .action_hit_timing_fallback import ActionHitTimingFallback, _normal_name
from .damage_attribution import (
    is_deterministic_attribution,
    is_locked_attribution,
    is_persistent_attribution,
)
from .damage_object_origins import DAMAGE_OBJECT_SKILL_IDS
from .ex_skill_resolution import ExSkillResolver
from .healing_attribution import load_healing_skill_models
from .models import CombatEvent, SkillCastEvent
from .skill_attribution import SkillAttributionEngineV2
from .timeline_evidence import (
    TimelineEvidenceResolver,
    is_timeline_recovered_cast,
)


_UNRESOLVED_SKILL_IDS = frozenset({"", "-1", "unknown", "unresolved"})
_UNRESOLVED_SKILL_NAMES = frozenset({"", "待识别伤害", "未学习伤害"})
_DIRECT_CAST_EVIDENCE_METHODS = frozenset(
    {
        "packet_skill_id",
        "appended_status_first_hit",
        "supplemental_cast_owner",
    }
)

# These skills either keep dealing damage after the original action or have
# non-damaging/form-changing behavior. Their damage records are valid damage
# evidence, but cannot prove that another cast happened.
_ACTION_PACKET_ONLY_CAST_SKILL_IDS = frozenset(
    {"2", "10", "1503", "2410", "3306", "4204", "4306"}
)
_ACTION_PACKET_ONLY_CAST_SKILL_NAMES = frozenset(
    {
        "飓风舞",
        "能量反射",
        "神圣惩戒",
        "生化导弹",
        "神圣之锤攻击",
        "致残惩罚者攻击",
        # This is a non-damaging form-changing/buff action.  A delayed
        # damage record can never prove that the action itself was cast.
        "致残惩罚者",
    }
)
_WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS = frozenset(
    {
        "冲击波",
        "侧踢",
        "起身斩",
        "攻击欲望",
        "重斩",
        "重勾拳",
        "升龙斩",
        "飞腿",
        "无限之刃",
        "冲锋踢",
        "横扫千军",
        "落肘",
    }
)
_MERCENARY_ACTION_PACKET_ONLY_CAST_SKILLS = frozenset({"冲击拳"})
_ELEMENTALIST_ACTION_PACKET_ONLY_CAST_SKILLS = frozenset({"火径"})
_ACTION_PACKET_ONLY_CAST_SKILLS_BY_JOB = {
    "战士": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS,
    "剑圣": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS,
    "战神": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS
    | _MERCENARY_ACTION_PACKET_ONLY_CAST_SKILLS,
    "复仇者": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS,
    "剑皇": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS,
    "月之领主": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS
    | frozenset({"觉醒黑暗之刃", "月光碎片"}),
    "狂战士": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS
    | _MERCENARY_ACTION_PACKET_ONLY_CAST_SKILLS,
    "毁灭者": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS
    | _MERCENARY_ACTION_PACKET_ONLY_CAST_SKILLS,
    "黑暗复仇者": _WARRIOR_ACTION_PACKET_ONLY_CAST_SKILLS
    | frozenset({"觉醒黑暗之刃"}),
    "时空领主": frozenset({"能量爆炸"}),
    "狙翎": frozenset({"包围", "偷袭点"}),
    "元素师": _ELEMENTALIST_ACTION_PACKET_ONLY_CAST_SKILLS,
    "火舞": frozenset({"地狱火海", "漫长熔浆"})
    | _ELEMENTALIST_ACTION_PACKET_ONLY_CAST_SKILLS,
    "冰灵": _ELEMENTALIST_ACTION_PACKET_ONLY_CAST_SKILLS
    | frozenset({"冰柱喷射", "冰刺"}),
    "圣骑士": frozenset({"神圣之锤"}),
    "十字军": frozenset({"神圣之锤"}),
    "魔羽": frozenset({"爆炸箭矢"}),
    # These Silver Hunter hits can continue/arrive independently of the
    # action packet. Keep their damage, but never invent timeline casts from
    # damage clusters.
    "银色猎人": frozenset({"银色箭雨", "飓风狂射"}),
    "曜": frozenset({"日光焰", "暗影召唤术"}),
    "暗": frozenset({"暗影召唤术"}),
    "影": frozenset({"天昏地暗"}),
}
_DAMAGE_DERIVED_CAST_KINDS = frozenset(
    {"damage_inferred", "shared_timeline_damage"}
)
_DARK_AVENGER_AMBIGUOUS_BASE_SKILL_IDS = frozenset(
    {"2", "5", "6", "7", "8", "9", "10", "15", "20", "28", "29"}
)


@lru_cache(maxsize=8192)
def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _text(value: object) -> str:
    return _normalized_text(str(value or ""))


@lru_cache(maxsize=8192)
def _skill_family_cached(skill_id: str, skill_name: str) -> str:
    name = _text(skill_name)
    if name and name not in _UNRESOLVED_SKILL_NAMES:
        compact = re.sub(r"\s+", "", name)
        compact = re.sub(r"(?:EX|ex)(?:等级\d+)?$", "", compact)
        compact = re.sub(r"等级\d+$", "", compact)
        if compact:
            return compact.casefold()
    return _text(skill_id).casefold()


def skill_family(skill_id: object, skill_name: object) -> str:
    """Return the game-facing skill family shared by base and EX actions."""
    return _skill_family_cached(_text(skill_id), _text(skill_name))


@lru_cache(maxsize=4096)
def _action_packet_only_skill_key_cached(skill_name: str) -> str:
    """Normalize display variants used by transformed/awakened actions."""

    name = re.sub(r"^(?:\[[^\]]+\])+", "", _text(skill_name))
    name = re.sub(r"(?:EX|ex)(?:等级\d+)?$", "", name)
    name = re.sub(r"等级\d+$", "", name)
    return re.sub(r"[\s•·・]+", "", name).casefold()


def _action_packet_only_skill_key(skill_name: object) -> str:
    return _action_packet_only_skill_key_cached(_text(skill_name))


@lru_cache(maxsize=8192)
def _requires_action_packet_for_cast_cached(
    skill_id: str,
    skill_name: str,
    job: str,
) -> bool:
    job_only_skills = _ACTION_PACKET_ONLY_CAST_SKILLS_BY_JOB.get(job)
    return (
        skill_id in _ACTION_PACKET_ONLY_CAST_SKILL_IDS
        or skill_family(skill_id, skill_name)
        in _ACTION_PACKET_ONLY_CAST_SKILL_NAMES
        or (
            job_only_skills is not None
            and _action_packet_only_skill_key(skill_name) in job_only_skills
        )
    )


def requires_action_packet_for_cast(
    skill_id: object,
    skill_name: object,
    job: object = "",
) -> bool:
    """Return whether damage is never sufficient proof of another cast."""

    return _requires_action_packet_for_cast_cached(
        _text(skill_id),
        _text(skill_name),
        _text(job),
    )


def is_disallowed_damage_derived_cast(event: SkillCastEvent) -> bool:
    """Keep packet artifacts and action-only skills out of cast streams.

    Dark Avenger's transformed records can reuse a few base-Warrior skill
    integers.  By the time the ledger runs the actor's final second job is
    known, so those misleading action names can be removed even when they
    appeared before the live decoder had enough class evidence.
    """

    if (
        _text(event.job) == "黑暗复仇者"
        and _text(event.skill_id) in _DARK_AVENGER_AMBIGUOUS_BASE_SKILL_IDS
    ):
        return True
    return (
        _text(event.packet_kind).casefold() in _DAMAGE_DERIVED_CAST_KINDS
        and requires_action_packet_for_cast(
            event.skill_id, event.skill_name, event.job
        )
    )


def _is_unresolved(event: CombatEvent) -> bool:
    return (
        _text(event.skill_id).casefold() in _UNRESOLVED_SKILL_IDS
        or _text(event.skill_name) in _UNRESOLVED_SKILL_NAMES
        or _text(event.attribution_method).casefold() == "unresolved"
    )


@dataclass(slots=True)
class RawDamageLedger:
    """Append-only factual damage ledger.

    Packet de-duplication happens before this layer. Once accepted, a hit is
    never deleted or rewritten; attribution is maintained by the second
    ledger so a later cast packet can safely correct a skill share.
    """

    events: list[CombatEvent] = field(default_factory=list)
    total_damage: int = 0
    # Retained for snapshot/cache compatibility. Healing is owned exclusively
    # by HealingAttributionLedger and therefore remains zero here.
    total_healing: int = 0

    def append(self, event: CombatEvent) -> None:
        if event.healing:
            raise ValueError(
                "healing events must be appended to HealingAttributionLedger"
            )
        self.events.append(event)
        self.total_damage += max(0, int(event.amount))

    def clear(self) -> None:
        self.events.clear()
        self.total_damage = 0
        self.total_healing = 0


@dataclass(frozen=True, slots=True)
class CastInstance:
    instance_id: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    family: str
    start_ms: int
    expected_end_ms: int
    packet_kind: str
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class AttributionLedgerResult:
    events: tuple[CombatEvent, ...]
    cast_events: tuple[SkillCastEvent, ...]
    instances: tuple[CastInstance, ...]
    raw_damage: int
    attributed_damage: int
    raw_healing: int
    attributed_healing: int
    matched_hits: int
    unresolved_hits: int
    inferred_casts: int
    reassigned_hits: int

    @property
    def balanced(self) -> bool:
        return (
            self.raw_damage == self.attributed_damage
            and self.raw_healing == self.attributed_healing
            and len(self.events) >= 0
        )


class SkillAttributionLedger:
    """Reconcile immutable hits against concrete cast instances."""

    def __init__(
        self,
        store: DataStore | None = None,
        mode: str | None = None,
    ) -> None:
        self.store = store or DataStore.load()
        configured_mode = mode or os.environ.get(
            "DRAGON_NEST_DPS_ATTRIBUTION_MODE",
            "instances",
        )
        requested_mode = _text(configured_mode).casefold()
        self.mode = (
            "legacy"
            if requested_mode == "legacy"
            else "evidence_v2"
            if requested_mode in {"evidence_v2", "v2", "strict"}
            else "instances"
        )
        self._ex_skill_resolver = ExSkillResolver.from_full_skill_dir(
            self.store.full_skill_dir
        )
        self._v2 = SkillAttributionEngineV2(self._ex_skill_resolver)
        damage_skill_ids: set[int] = set()
        damage_skills_by_job_name: dict[
            tuple[str, str], set[tuple[str, str]]
        ] = defaultdict(set)
        for path in sorted(self.store.skill_damage_dir.glob("job_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            job_meta = payload.get("j") or ()
            job_name = _text(job_meta[1]) if len(job_meta) > 1 else ""
            for row in payload.get("n", ()):
                if not isinstance(row, list) or not row:
                    continue
                try:
                    skill_id = int(row[0])
                except (TypeError, ValueError):
                    continue
                damage_skill_ids.add(skill_id)
                skill_name = _text(row[1]) if len(row) > 1 else ""
                if job_name and skill_name:
                    damage_skills_by_job_name[
                        (job_name.casefold(), re.sub(r"\s+", "", skill_name).casefold())
                    ].add((str(skill_id), skill_name))
        self.damage_skill_ids = frozenset(damage_skill_ids)
        canonical_damage_skills = {
            key: next(iter(values))
            for key, values in damage_skills_by_job_name.items()
            if len(values) == 1
        }
        # An action packet can keep the base ID even though the final-job EX
        # passive is learned.  Match against the EX action profile, then emit
        # the canonical damage bucket used by ACT.  Most EX passives modify
        # their base skill and therefore merge back to the base damage row;
        # independent EX damage sources such as 神圣惩戒EX retain their own
        # row and ID.
        for identity in self._ex_skill_resolver.identities:
            ex_key = (
                identity.job.casefold(),
                re.sub(r"\s+", "", identity.ex_skill_name).casefold(),
            )
            canonical_name = self.store.skill_damage_canonical_name(
                identity.job, identity.ex_skill_name
            )
            canonical_entry = self.store._skill_damage_entry(
                identity.job, canonical_name
            )
            if canonical_entry is None:
                canonical_entry = self.store._skill_damage_entry(
                    identity.job, identity.base_skill_name
                )
            if canonical_entry is not None:
                canonical_damage_skills[ex_key] = (
                    str(canonical_entry["id"]),
                    str(canonical_entry["name"]),
                )
        self._action_hit_timing_fallback = ActionHitTimingFallback(
            known_damage_skill_ids=self.damage_skill_ids,
            non_damage_skill_names={
                name
                for model in load_healing_skill_models()
                for name in (model.name, *model.aliases)
            },
            canonical_damage_skills=canonical_damage_skills,
        )

    def __getstate__(self) -> dict[str, object]:
        """Keep the process-wide immutable data store out of replay caches.

        ``DataStore.load`` is an LRU-backed singleton assembled from the
        installed data files. Serializing that same ~33 MB object into every
        replay cache wastes CPU, memory and disk space; the decoder
        fingerprint already invalidates caches whenever those files change.
        """

        state = dict(self.__dict__)
        state.pop("store", None)
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        # Older cache payloads may still contain a private DataStore copy.
        # Always discard it and reconnect the restored ledger to the current
        # version-fingerprinted process singleton.
        restored = dict(state)
        restored.pop("store", None)
        self.__dict__.update(restored)
        self.store = DataStore.load()
        timing = getattr(self, "_action_hit_timing_fallback", None)
        if timing is not None and not hasattr(timing, "non_damage_skill_names"):
            # Old replay caches predate the healer/Buff veto. Restore the
            # current rule rather than letting a cache rebuild attach direct
            # damage to a nearby non-damaging action.
            timing.non_damage_skill_names = frozenset(
                _normal_name(name)
                for model in load_healing_skill_models()
                for name in (model.name, *model.aliases)
                if name
            )

    def _can_own_direct_damage(self, instance: CastInstance) -> bool:
        try:
            skill_id = int(instance.skill_id)
        except (TypeError, ValueError):
            return False
        # These field/projectile skills are identified by their exact runtime
        # object. A nearby cast is never allowed to absorb an unrelated weak
        # hit into the object-backed skill after that deterministic join has
        # failed.
        return (
            skill_id in self.damage_skill_ids
            and skill_id not in DAMAGE_OBJECT_SKILL_IDS
            # Resent Area is admitted only by its exact 135D field generation
            # and client attack phase in the packet decoder.  A neighbouring
            # action/cast instance must never pull an unknown Hit into it.
            and skill_id != 5207
        )

    @staticmethod
    def _is_action_evidence(packet_kind: str) -> bool:
        return _text(packet_kind).casefold() in {
            "skill_action",
            "compact_skill_input",
            # A direct server skill id inside a verified form-buff window is
            # authoritative evidence for the transformed normal-attack
            # action, but it is intentionally not evidence for re-casting the
            # buff itself.
            "transformed_basic_attack",
        }

    @staticmethod
    def _can_reassign_cross_family(instance: CastInstance) -> bool:
        # Compact input packets establish a timeline action only. A player can
        # cancel these inputs before their damage stage, so they may match an
        # already identified hit from the same skill family but must not take
        # an unresolved/weak hit away from a neighbouring skill.
        return _text(instance.packet_kind).casefold() != "compact_skill_input"

    @lru_cache(maxsize=4096)
    def _inferred_action_dedupe_window_ms(self, job: str, skill_name: str) -> int:
        """Return the shortest legal recast interval under the 70% CDR cap."""

        normalized_name = _text(skill_name).replace(" EX", "EX")
        base_name = re.sub(
            r"(?:EX|ex)(?:等级\d+)?$", "", normalized_name
        )
        cooldown = 0.0
        for candidate in dict.fromkeys((normalized_name, base_name)):
            if not candidate:
                continue
            try:
                timing = self.store.skill_effect_timing(
                    job, candidate, max_required_level=70
                )
                cooldown = max(cooldown, float(timing.get("cooldown") or 0.0))
            except (AttributeError, TypeError, ValueError):
                continue
        if cooldown <= 0:
            return 0
        return max(1, round(cooldown * 0.30 * 1000.0))

    @staticmethod
    def _has_nearby_timestamp(
        timestamps: list[int], timestamp_ms: int, window_ms: int
    ) -> bool:
        if not timestamps or window_ms <= 0:
            return False
        index = bisect_left(timestamps, timestamp_ms)
        return any(
            abs(timestamps[candidate] - timestamp_ms) < window_ms
            for candidate in (index - 1, index)
            if 0 <= candidate < len(timestamps)
        )

    def _deduplicate_inferred_action_casts(
        self, cast_events: tuple[SkillCastEvent, ...]
    ) -> tuple[SkillCastEvent, ...]:
        """Prefer action packets over nearby damage-derived copies.

        The comparison is deliberately limited to damage_inferred/action pairs.
        Real action/action recasts and charge skills therefore remain untouched.
        """

        action_starts: dict[tuple[str, str], list[int]] = defaultdict(list)
        for cast in cast_events:
            if not self._is_action_evidence(cast.packet_kind):
                continue
            action_starts[
                (cast.source_id, skill_family(cast.skill_id, cast.skill_name))
            ].append(cast.timestamp_ms)
        for starts in action_starts.values():
            starts.sort()

        result: list[SkillCastEvent] = []
        for cast in sorted(cast_events, key=lambda row: row.timestamp_ms):
            if is_disallowed_damage_derived_cast(cast):
                continue
            if _text(cast.packet_kind).casefold() == "damage_inferred":
                key = (
                    cast.source_id,
                    skill_family(cast.skill_id, cast.skill_name),
                )
                window_ms = self._inferred_action_dedupe_window_ms(
                    cast.job, cast.skill_name
                )
                if self._has_nearby_timestamp(
                    action_starts.get(key, []), cast.timestamp_ms, window_ms
                ):
                    continue
            result.append(cast)
        return tuple(result)

    @lru_cache(maxsize=4096)
    def _timing(self, job: str, skill_name: str) -> tuple[int, int]:
        cast_seconds = 0.0
        duration_seconds = 0.0
        try:
            cast_row = self.store.skill_cast_time(skill_name, [job])
            cast_seconds = max(
                float(cast_row.get("cast_time") or 0.0),
                float(cast_row.get("quick_time") or 0.0),
            )
        except (AttributeError, TypeError, ValueError):
            cast_seconds = 0.0
        try:
            effect_row = self.store.skill_effect_timing(job, skill_name)
            duration_seconds = max(
                float(effect_row.get("duration") or 0.0),
                float(effect_row.get("total_time") or 0.0),
            )
        except (AttributeError, TypeError, ValueError):
            duration_seconds = 0.0
        cast_ms = max(120, round(max(0.0, cast_seconds) * 1000))
        active_ms = max(
            1_000,
            cast_ms,
            round(max(0.0, duration_seconds) * 1000),
        )
        # Direct action matching must remain local. Long summons and DoTs are
        # preserved by their authoritative owner methods instead.
        return cast_ms, min(20_000, active_ms)

    def _base_instances(
        self, cast_events: Iterable[SkillCastEvent]
    ) -> list[CastInstance]:
        result: list[CastInstance] = []
        last_by_key: dict[tuple[str, str], CastInstance] = {}
        ordinal_by_key: dict[tuple[str, str], int] = defaultdict(int)
        for cast in sorted(cast_events, key=lambda row: row.timestamp_ms):
            family = skill_family(cast.skill_id, cast.skill_name)
            key = (cast.source_id, family)
            previous = last_by_key.get(key)
            if previous is not None and cast.timestamp_ms - previous.start_ms <= 200:
                continue
            _cast_ms, active_ms = self._timing(cast.job, cast.skill_name)
            ordinal_by_key[key] += 1
            instance = CastInstance(
                instance_id=(
                    f"{cast.source_id}:{family}:{cast.timestamp_ms}:"
                    f"{ordinal_by_key[key]}"
                ),
                source_id=cast.source_id,
                source_name=cast.source_name,
                job=cast.job,
                skill_id=cast.skill_id,
                skill_name=cast.skill_name,
                family=family,
                start_ms=cast.timestamp_ms,
                expected_end_ms=cast.timestamp_ms + active_ms,
                packet_kind=cast.packet_kind,
                inferred=is_timeline_recovered_cast(cast),
            )
            result.append(instance)
            last_by_key[key] = instance
        return result

    def _infer_missing_instances(
        self,
        events: tuple[CombatEvent, ...],
        instances: list[CastInstance],
    ) -> list[CastInstance]:
        evidence: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
        for event in events:
            if event.healing or _is_unresolved(event):
                continue
            if requires_action_packet_for_cast(
                event.skill_id, event.skill_name, event.job
            ):
                continue
            method = _text(event.attribution_method).casefold()
            if method not in _DIRECT_CAST_EVIDENCE_METHODS:
                continue
            evidence[(event.source_id, skill_family(event.skill_id, event.skill_name))].append(
                event
            )

        instances_by_key: dict[tuple[str, str], list[CastInstance]] = defaultdict(list)
        for instance in instances:
            instances_by_key[(instance.source_id, instance.family)].append(instance)

        inferred: list[CastInstance] = []
        for key, hits in evidence.items():
            hits.sort(key=lambda row: row.timestamp_ms)
            sample = hits[0]
            cast_ms, active_ms = self._timing(sample.job, sample.skill_name)
            max_gap_ms = max(750, min(1_250, round(active_ms * 0.35)))
            max_span_ms = max(1_250, active_ms + 750)
            clusters: list[list[CombatEvent]] = []
            for hit in hits:
                if (
                    not clusters
                    or hit.timestamp_ms - clusters[-1][-1].timestamp_ms > max_gap_ms
                    or hit.timestamp_ms - clusters[-1][0].timestamp_ms > max_span_ms
                ):
                    clusters.append([hit])
                else:
                    clusters[-1].append(hit)

            known = sorted(instances_by_key.get(key, ()), key=lambda row: row.start_ms)
            observed_delays: list[int] = []
            for instance in known:
                first_hit = next(
                    (
                        hit
                        for hit in hits
                        if instance.start_ms <= hit.timestamp_ms
                        <= instance.start_ms + max(active_ms + 750, 3_000)
                    ),
                    None,
                )
                if first_hit is not None:
                    observed_delays.append(first_hit.timestamp_ms - instance.start_ms)
            if observed_delays:
                ordered = sorted(observed_delays)
                first_hit_delay_ms = min(cast_ms, ordered[len(ordered) // 2])
            else:
                first_hit_delay_ms = min(cast_ms, 400)

            ordinal = len(known)
            for cluster in clusters:
                first_hit_ms = cluster[0].timestamp_ms
                if any(
                    instance.start_ms - 250 <= first_hit_ms
                    <= instance.expected_end_ms + 750
                    for instance in (*known, *inferred)
                    if (instance.source_id, instance.family) == key
                ):
                    continue
                ordinal += 1
                start_ms = max(0, first_hit_ms - first_hit_delay_ms)
                action_starts = sorted(
                    instance.start_ms
                    for instance in known
                    if self._is_action_evidence(instance.packet_kind)
                )
                dedupe_window_ms = self._inferred_action_dedupe_window_ms(
                    cluster[0].job, cluster[0].skill_name
                )
                if self._has_nearby_timestamp(
                    action_starts, start_ms, dedupe_window_ms
                ):
                    continue
                inferred.append(
                    CastInstance(
                        instance_id=f"{key[0]}:{key[1]}:{start_ms}:i{ordinal}",
                        source_id=cluster[0].source_id,
                        source_name=cluster[0].source_name,
                        job=cluster[0].job,
                        skill_id=cluster[0].skill_id,
                        skill_name=cluster[0].skill_name,
                        family=key[1],
                        start_ms=start_ms,
                        expected_end_ms=start_ms + active_ms,
                        packet_kind="damage_inferred",
                        inferred=True,
                    )
                )
        return inferred

    @staticmethod
    def _candidate_score(
        event: CombatEvent,
        instance: CastInstance,
        current_family: str,
    ) -> float | None:
        if event.source_id != instance.source_id:
            return None
        age_ms = event.timestamp_ms - instance.start_ms
        active_ms = max(1_000, instance.expected_end_ms - instance.start_ms)
        # Damage cannot be owned by an action that has not started yet. The
        # old 250 ms look-ahead let a following combo button steal the tail of
        # a field/stance skill (for example Arrow Storm -> Aerial Chain Shot).
        if age_ms < 0 or age_ms > active_ms + 750:
            return None
        score = 95.0 - max(0, age_ms) / 40.0
        if instance.family == current_family:
            score += 8.0
        if not instance.inferred:
            score += 4.0
        return score

    @staticmethod
    def _direct_result_group_links(
        events: tuple[CombatEvent, ...],
    ) -> tuple[CombatEvent, ...]:
        """Backfill skill-less rows from a unique direct37 result group.

        The complete event ledger is intentionally used because a skill-
        bearing anchor can arrive immediately after a continuation row. IDs
        are scoped by source actor and a strict 5 ms server-batch window;
        reused IDs and groups containing more than one skill fail closed.
        """

        anchors: dict[tuple[str, int], list[CombatEvent]] = defaultdict(list)
        for event in events:
            if (
                event.healing
                or event.wire_result_group_id <= 0
                or _text(event.attribution_method).casefold()
                != "packet_skill_id"
            ):
                continue
            anchors[(event.source_id, event.wire_result_group_id)].append(event)

        linked: list[CombatEvent] = []
        for event in events:
            method = _text(event.attribution_method).casefold()
            if (
                event.healing
                or event.wire_result_group_id <= 0
                or is_deterministic_attribution(method)
                or is_persistent_attribution(method)
                or event.has_real_hit_evidence is not True
            ):
                linked.append(event)
                continue
            nearby = tuple(
                anchor
                for anchor in anchors.get(
                    (event.source_id, event.wire_result_group_id), ()
                )
                if abs(anchor.timestamp_ms - event.timestamp_ms) <= 5
            )
            identities = {
                (anchor.skill_id, anchor.skill_name) for anchor in nearby
            }
            if len(identities) != 1:
                linked.append(event)
                continue
            skill_id, skill_name = next(iter(identities))
            linked.append(
                replace(
                    event,
                    skill_id=skill_id,
                    skill_name=skill_name,
                    attribution_method="direct_result_group_link",
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method
                        or event.attribution_method
                    ),
                    attribution_confidence=1.0,
                )
            )
        return tuple(linked)

    @staticmethod
    def _direct_result_group_run_links(
        events: tuple[CombatEvent, ...],
    ) -> tuple[CombatEvent, ...]:
        """Backfill one actor's uninterrupted direct37 result-group run.

        The full result-group value is not capture-global: long captures show
        that it can be reused tens of seconds later.  It is, however, stable
        for one contiguous per-actor server result batch.  A different group
        from the same actor closes the run; packets from other actors do not.
        Runs with no exact anchor or more than one exact skill fail closed.
        """

        actor_indices: dict[str, list[int]] = defaultdict(list)
        for index, event in enumerate(events):
            if (
                not event.healing
                and event.wire_result_group_id > 0
                and event.has_real_hit_evidence is True
            ):
                actor_indices[event.source_id].append(index)

        replacements: dict[int, CombatEvent] = {}
        anchor_methods = {
            "packet_skill_id",
            "direct_result_group_link",
            "direct_result_group_run_link",
        }
        for indices in actor_indices.values():
            runs: list[list[int]] = []
            for index in indices:
                event = events[index]
                if (
                    not runs
                    or events[runs[-1][-1]].wire_result_group_id
                    != event.wire_result_group_id
                ):
                    runs.append([index])
                else:
                    runs[-1].append(index)
            for run in runs:
                identities = {
                    (events[index].skill_id, events[index].skill_name)
                    for index in run
                    if _text(events[index].attribution_method).casefold()
                    in anchor_methods
                }
                if len(identities) != 1:
                    continue
                skill_id, skill_name = next(iter(identities))
                for index in run:
                    event = events[index]
                    method = _text(event.attribution_method).casefold()
                    if (
                        is_deterministic_attribution(method)
                        or is_persistent_attribution(method)
                    ):
                        continue
                    replacements[index] = replace(
                        event,
                        skill_id=skill_id,
                        skill_name=skill_name,
                        attribution_method="direct_result_group_run_link",
                        raw_skill_id=event.raw_skill_id or event.skill_id,
                        raw_skill_name=event.raw_skill_name or event.skill_name,
                        raw_attribution_method=(
                            event.raw_attribution_method
                            or event.attribution_method
                        ),
                        attribution_confidence=1.0,
                    )
        if not replacements:
            return events
        return tuple(
            replacements.get(index, event)
            for index, event in enumerate(events)
        )

    def reconcile(
        self,
        events: Iterable[CombatEvent],
        cast_events: Iterable[SkillCastEvent],
    ) -> AttributionLedgerResult:
        # Architectural guard: even a legacy caller that supplies a mixed
        # stream cannot route healing through damage attribution.
        # ``wire_result_group_id`` is result metadata, not a stable skill or
        # cast foreign key. Four-capture leave-one-out validation found both
        # 5 ms and contiguous-run cross-skill collisions. Do not relabel a
        # skill-less direct hit from this value; retain the helpers above only
        # to make older diagnostics/tests easy to audit.
        raw_events = tuple(event for event in events if not event.healing)
        input_casts = tuple(cast_events)
        if self.mode == "evidence_v2":
            v2 = self._v2.resolve(raw_events, input_casts)
            timing = self._action_hit_timing_fallback.resolve(
                v2.events, v2.cast_events
            )
            linked_events = link_additional_damage_triggers(
                timing.events,
                lambda event: (
                    str(event.skill_id or event.skill_name or "unknown"),
                    str(event.skill_name or event.skill_id or "unknown"),
                ),
            )
            raw_damage = sum(row.amount for row in raw_events)
            attributed_damage = sum(row.amount for row in linked_events)
            if raw_damage != attributed_damage or len(raw_events) != len(linked_events):
                raise AssertionError("V2 attribution changed the immutable damage ledger")
            return AttributionLedgerResult(
                events=linked_events,
                cast_events=v2.cast_events,
                instances=(),
                raw_damage=raw_damage,
                attributed_damage=attributed_damage,
                raw_healing=0,
                attributed_healing=0,
                matched_hits=v2.accepted_hits + timing.recovered_hits,
                unresolved_hits=timing.unresolved_hits,
                inferred_casts=0,
                reassigned_hits=timing.recovered_hits,
            )
        if self.mode == "legacy":
            raw_damage = sum(
                row.amount for row in raw_events if not row.healing
            )
            raw_healing = sum(
                row.amount for row in raw_events if row.healing
            )
            return AttributionLedgerResult(
                events=raw_events,
                cast_events=input_casts,
                instances=(),
                raw_damage=raw_damage,
                attributed_damage=raw_damage,
                raw_healing=raw_healing,
                attributed_healing=raw_healing,
                matched_hits=0,
                unresolved_hits=sum(
                    1 for row in raw_events if _is_unresolved(row)
                ),
                inferred_casts=0,
                reassigned_hits=0,
            )
        # Damage ownership and displayed cast reconstruction are deliberately
        # versioned independently.  The evidence timeline may remove a false
        # display cast without changing historical damage ownership or scores.
        # Evidence-based attribution remains available as an opt-in shadow
        # experiment until its score corpus has been approved separately.
        attribution_mode = _text(
            os.environ.get("DRAGON_NEST_TIMELINE_ATTRIBUTION_MODE", "legacy")
        ).casefold()
        if attribution_mode != "evidence":
            raw_casts = self._deduplicate_inferred_action_casts(input_casts)
            instances = self._base_instances(raw_casts)
            inferred = self._infer_missing_instances(raw_events, instances)
            instances.extend(inferred)
            inferred_cast_events = tuple(
                SkillCastEvent(
                    timestamp_ms=row.start_ms,
                    source_id=row.source_id,
                    source_name=row.source_name,
                    job=row.job,
                    skill_id=row.skill_id,
                    skill_name=row.skill_name,
                    packet_kind="damage_inferred",
                )
                for row in inferred
            )
            all_casts = tuple(
                sorted(
                    (*raw_casts, *inferred_cast_events),
                    key=lambda row: row.timestamp_ms,
                )
            )
        else:
            # One evidence resolver now owns cast reconstruction. Damage
            # attribution consumes its accepted occurrences but never creates
            # a second, differently clustered timeline of its own.
            evidence_result = TimelineEvidenceResolver(self.store).resolve(
                input_casts, raw_events
            )
            all_casts = evidence_result.cast_events
            instances = self._base_instances(all_casts)
            inferred = [
                row
                for row in instances
                if is_timeline_recovered_cast(row)
            ]
        instances.sort(key=lambda row: (row.start_ms, row.source_id, row.family))

        by_source: dict[str, list[CastInstance]] = defaultdict(list)
        by_source_family: dict[tuple[str, str], list[CastInstance]] = defaultdict(list)
        for instance in instances:
            by_source[instance.source_id].append(instance)
            by_source_family[(instance.source_id, instance.family)].append(instance)
        starts_by_source = {
            key: [row.start_ms for row in rows]
            for key, rows in by_source.items()
        }
        starts_by_source_family = {
            key: [row.start_ms for row in rows]
            for key, rows in by_source_family.items()
        }

        def local_instances(
            rows: list[CastInstance] | tuple[CastInstance, ...],
            starts: list[int],
            timestamp_ms: int,
        ) -> tuple[CastInstance, ...]:
            if not rows:
                return ()
            left = bisect_left(starts, timestamp_ms - 20_750)
            right = bisect_right(starts, timestamp_ms + 250)
            return tuple(rows[left:right])

        attributed: list[CombatEvent] = []
        matched_hits = 0
        unresolved_hits = 0
        reassigned_hits = 0
        for raw_index, event in enumerate(raw_events):
            family = skill_family(event.skill_id, event.skill_name)
            method = _text(event.attribution_method).casefold()
            authoritative = is_locked_attribution(method)
            persistent = is_persistent_attribution(method)
            has_damage_object_instance = (
                _text(event.cast_instance_id).casefold().startswith(
                    "damage-object:"
                )
            )
            # An exact runtime object generation is already a stronger
            # transaction boundary than a neighbouring cast window.  A
            # provisional template may still lack enough static evidence to
            # be called deterministic, but its owner + damage_object_id +
            # generation must never be renamed to another skill family by
            # temporal reconciliation.  Same-family cast matching remains
            # available so the hit can share the real cast instance.
            object_generation_bound = (
                method == "damage_object_projectile_link_provisional"
                and has_damage_object_instance
            )
            candidate: CastInstance | None = None

            family_key = (event.source_id, family)
            same_family_rows = by_source_family.get(family_key, ())
            same_family = local_instances(
                same_family_rows,
                starts_by_source_family.get(family_key, []),
                event.timestamp_ms,
            )
            if same_family:
                scored_same = [
                    (self._candidate_score(event, row, family), row)
                    for row in same_family
                ]
                scored_same = [row for row in scored_same if row[0] is not None]
                if scored_same:
                    candidate = max(scored_same, key=lambda row: row[0])[1]

            if (
                not authoritative
                and not persistent
                and not object_generation_bound
            ):
                scored = [
                    (self._candidate_score(event, row, family), row)
                    for row in local_instances(
                        by_source.get(event.source_id, ()),
                        starts_by_source.get(event.source_id, []),
                        event.timestamp_ms,
                    )
                    if self._can_own_direct_damage(row)
                    and (
                        row.family == family
                        or self._can_reassign_cross_family(row)
                    )
                ]
                scored = sorted(
                    (row for row in scored if row[0] is not None),
                    key=lambda row: row[0],
                    reverse=True,
                )
                if scored:
                    best_score, best = scored[0]
                    margin = (
                        best_score - scored[1][0] if len(scored) > 1 else best_score
                    )
                    if best_score >= 25.0 and (
                        best.family == family or margin >= 8.0
                    ):
                        candidate = best

            if candidate is None:
                if _is_unresolved(event):
                    unresolved_hits += 1
                attributed.append(
                    replace(
                        event,
                        raw_skill_id=event.raw_skill_id or event.skill_id,
                        raw_skill_name=event.raw_skill_name or event.skill_name,
                        raw_attribution_method=(
                            event.raw_attribution_method
                            or event.attribution_method
                        ),
                        cast_instance_id="",
                        attribution_confidence=(
                            1.0 if authoritative else event.attribution_confidence
                        ),
                    )
                )
                continue

            matched_hits += 1
            reassign = (
                not authoritative
                and candidate.family != family
                and not persistent
            )
            if reassign:
                reassigned_hits += 1
            attributed.append(
                replace(
                    event,
                    skill_id=candidate.skill_id if reassign else event.skill_id,
                    skill_name=candidate.skill_name if reassign else event.skill_name,
                    attribution_method=(
                        "cast_instance_match" if reassign else event.attribution_method
                    ),
                    raw_skill_id=event.raw_skill_id or event.skill_id,
                    raw_skill_name=event.raw_skill_name or event.skill_name,
                    raw_attribution_method=(
                        event.raw_attribution_method
                        or event.attribution_method
                    ),
                    # Keep the exact runtime object generation as the Hit
                    # grouping key. Replacing it with the nearby action's
                    # synthetic cast ID destroys the Projectile/entity chain
                    # needed by Hit-count and damage-distribution analysis.
                    cast_instance_id=(
                        event.cast_instance_id
                        if has_damage_object_instance
                        else candidate.instance_id
                    ),
                    attribution_confidence=(
                        1.0
                        if authoritative
                        else (0.78 if candidate.family == family else 0.62)
                    ),
                )
            )

        raw_damage = sum(row.amount for row in raw_events if not row.healing)
        attributed_damage = sum(row.amount for row in attributed if not row.healing)
        raw_healing = sum(row.amount for row in raw_events if row.healing)
        attributed_healing = sum(row.amount for row in attributed if row.healing)
        if raw_damage != attributed_damage or raw_healing != attributed_healing:
            # Attribution is never allowed to mutate the factual ledger.
            attributed = list(raw_events)
            matched_hits = reassigned_hits = 0
            unresolved_hits = sum(1 for row in raw_events if _is_unresolved(row))
            attributed_damage = raw_damage
            attributed_healing = raw_healing

        return AttributionLedgerResult(
            events=tuple(attributed),
            cast_events=all_casts,
            instances=tuple(instances),
            raw_damage=raw_damage,
            attributed_damage=attributed_damage,
            raw_healing=raw_healing,
            attributed_healing=attributed_healing,
            matched_hits=matched_hits,
            unresolved_hits=unresolved_hits,
            inferred_casts=len(inferred),
            reassigned_hits=reassigned_hits,
        )
