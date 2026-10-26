from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .additional_damage_shadow import (
    SHADOW_EFFECT_RULES,
    ShadowAttribution,
    audit_additional_damage_shadow,
    is_folded_additional_damage_event,
    is_intrinsic_additional_damage_event,
    is_native_supplemental_damage_event,
    native_supplemental_skill_identity,
)
from .critical_stats import is_critical_rate_sample
from .instant_action_damage import (
    BURNING_HAND_TRIGGER_SKILL_IDS,
    BURNING_HAND_WIRE_TRIGGER_CONFIDENCE,
    BURNING_HAND_WIRE_TRIGGER_METHOD,
)
from .models import CombatEvent, CombatSnapshot


_APPLIED_STATUSES = frozenset(
    {"当前已正确识别", "影子归因确定", "效果来源已识别"}
)
_ALGORITHM_VERSION = "additional-damage-attribution-v18"
_RULES_BY_ID = {rule.effect_skill_id: rule for rule in SHADOW_EFFECT_RULES}
_BURNING_HAND_SKILL_ID = "2301"
_BURNING_HAND_METHOD = "instant_action_response"


def _cast_family_name_key(value: object) -> str:
    """Match base damage rows with their EX action-packet identity."""

    return (
        "".join(str(value or "").split())
        .upper()
        .replace("ＥＸ", "EX")
        .removesuffix("EX")
    )


def _snapshot_cast_counts(
    snapshot: CombatSnapshot,
) -> dict[tuple[str, str], Counter[str]]:
    """Recover factual release counts from the retained action-event axis.

    Some saved snapshots and the first implementation of the attributed skill
    view carried a stale per-skill ``cast_count`` (often ``1``) even though the
    complete EX action axis was still present. Rebuilding this tiny index at
    presentation time is deterministic and also handles the common wire shape
    where damage uses the base skill name while actions use the EX name.
    """

    result: dict[tuple[str, str], Counter[str]] = {}
    for cast in tuple(snapshot.skill_cast_events or ()):
        source_id = str(getattr(cast, "source_id", "") or "")
        family = _cast_family_name_key(getattr(cast, "skill_name", ""))
        if not source_id or not family:
            continue
        packet_kind = str(getattr(cast, "packet_kind", "") or "").casefold()
        evidence = (
            "inferred"
            if "inferred" in packet_kind or "recovered" in packet_kind
            else "direct"
        )
        result.setdefault((source_id, family), Counter())[evidence] += 1
    return result


def _snapshot_factual_hit_event_indices(
    snapshot: CombatSnapshot,
    audits: tuple[ShadowAttribution, ...] = (),
) -> set[int]:
    """Identify gameplay Hits instead of every transported damage record.

    ``SkillSnapshot.hits`` is an accounting-record count.  A single gameplay
    Hit can be followed by one or more subtype4 Buff/passive/suffix results;
    those rows legitimately contribute damage but must not inflate the
    skill's Hit count or average Hit per release.  The wire-level
    ``has_real_hit_evidence`` flag is the common discriminator for direct
    Hits and linked projectiles.  Rows independently proven to be additional
    effects are excluded even when their transport also carries hit evidence.
    """

    excluded = {
        row.event_index
        for row in audits
        if row.status in _APPLIED_STATUSES and row.effect_skill_id
    }
    result: set[int] = set()
    for event_index, event in enumerate(tuple(snapshot.combat_events or ())):
        if (
            event_index in excluded
            or event.healing
            or event.amount <= 0
            or event.has_real_hit_evidence is not True
            or is_intrinsic_additional_damage_event(event)
        ):
            continue
        result.add(event_index)
    return result


def _snapshot_factual_hit_counts(
    snapshot: CombatSnapshot,
    factual_hit_indices: set[int],
) -> dict[tuple[str, str], int]:
    result: Counter[tuple[str, str]] = Counter()
    for event_index in factual_hit_indices:
        event = snapshot.combat_events[event_index]
        native_skill_id, native_skill_name = native_supplemental_skill_identity(event)
        family = _cast_family_name_key(native_skill_name or event.skill_name)
        source_id = str(event.source_id or "")
        if source_id and family:
            result[(source_id, family)] += 1
    return dict(result)


def _repair_cast_metrics(
    rows: list[dict[str, Any]],
    source_id: str,
    cast_counts: dict[tuple[str, str], Counter[str]],
) -> None:
    """Apply factual action counts without changing damage or Hit evidence."""

    for row in rows:
        family = _cast_family_name_key(row.get("name"))
        counts = cast_counts.get((source_id, family)) if family else None
        if not counts:
            continue
        direct = int(counts.get("direct", 0))
        inferred = int(counts.get("inferred", 0))
        total = direct + inferred
        row["cast_count"] = total
        row["direct_cast_count"] = direct
        row["inferred_cast_count"] = inferred
        row["average_hits"] = (
            int(row.get("hits", 0) or 0) / total if total > 0 else None
        )


def _composition_identity(event: CombatEvent) -> tuple[str, str]:
    display_as_unresolved = not event.healing and event.skill_id in {"", "-1"}
    display_as_extra = (
        not event.healing
        and is_folded_additional_damage_event(event)
    )
    skill_id = (
        "unresolved:damage"
        if display_as_unresolved
        else "supplemental:status_damage"
        if display_as_extra
        else event.skill_id
    )
    skill_name = (
        "待识别伤害"
        if display_as_unresolved
        else "额外伤害"
        if display_as_extra
        else event.skill_name
    )
    return skill_id or skill_name or "unknown", skill_name or skill_id or "unknown"


def _skill_payload(skill: object) -> dict[str, Any]:
    return {
        "skill_id": str(getattr(skill, "skill_id", "") or ""),
        "name": str(getattr(skill, "name", "") or ""),
        "damage": int(getattr(skill, "damage", 0) or 0),
        "healing": int(getattr(skill, "healing", 0) or 0),
        "hits": int(getattr(skill, "hits", 0) or 0),
        "critical_hits": int(getattr(skill, "critical_hits", 0) or 0),
        "critical_samples": int(getattr(skill, "critical_samples", 0) or 0),
        "cast_count": int(getattr(skill, "cast_count", 0) or 0),
        "direct_cast_count": int(getattr(skill, "direct_cast_count", 0) or 0),
        "inferred_cast_count": int(getattr(skill, "inferred_cast_count", 0) or 0),
        "average_hits": getattr(skill, "average_hits", None),
        "share": float(getattr(skill, "share", 0.0) or 0.0),
        "attribution": [list(row) for row in getattr(skill, "attribution", ())],
        "trigger_skills": [list(row) for row in getattr(skill, "trigger_skills", ())],
        "additional_damage": [],
    }


def _display_effect_name(effect_name: str) -> str:
    name = str(effect_name or "").strip()
    if name.endswith("附加伤害"):
        name = name[: -len("附加伤害")].strip()
    return name or "未确定"


def _empty_bucket(skill_id: str, name: str) -> dict[str, Any]:
    return {
        "skill_id": skill_id,
        "name": name,
        "damage": 0,
        "healing": 0,
        "hits": 0,
        "critical_hits": 0,
        "critical_samples": 0,
        "cast_count": 0,
        "direct_cast_count": 0,
        "inferred_cast_count": 0,
        "average_hits": None,
        "share": 0.0,
        "attribution": [],
        "trigger_skills": [],
        "additional_damage": [],
    }


def _extra_bucket_identity(audit: ShadowAttribution) -> tuple[str, str]:
    if audit.status in _APPLIED_STATUSES and audit.effect_skill_id == "501":
        return "501", "连击"
    if audit.status in _APPLIED_STATUSES and audit.effect_skill_id:
        effect_name = _display_effect_name(audit.effect_name)
        return f"additional:{audit.effect_skill_id}", f"额外伤害（{effect_name}）"
    return "additional:unknown", "额外伤害（未确定）"


def _is_periodic_status_event(event: CombatEvent) -> bool:
    return bool(
        str(event.attribution_method or "").strip().casefold()
        in {"periodic_status", "periodic_status_packet", "refreshable_pulsing_aura"}
        or str(event.skill_id or "").strip().casefold().startswith("periodic:")
    )


def _attribution_counters(
    buckets: dict[str, dict[str, Any]],
) -> dict[str, Counter[str]]:
    return {
        key: Counter(
            {str(method): int(count) for method, count in row.get("attribution", ())}
        )
        for key, row in buckets.items()
    }


def _additional_counters(
    buckets: dict[str, dict[str, Any]],
) -> dict[str, dict[tuple[str, str], list[int]]]:
    result: dict[str, dict[tuple[str, str], list[int]]] = {}
    for key, row in buckets.items():
        current: dict[tuple[str, str], list[int]] = {}
        for item in row.get("additional_damage", ()):
            if len(item) < 4:
                continue
            current[(str(item[0]), str(item[1]))] = [int(item[2]), int(item[3])]
        result[key] = current
    return result


def _move_event(
    buckets: dict[str, dict[str, Any]],
    attribution_counts: dict[str, Counter[str]],
    event: CombatEvent,
    source_key: str,
    destination_key: str,
    destination_name: str,
    *,
    effect_id: str = "",
    effect_name: str = "",
    additional_counts: dict[str, dict[tuple[str, str], list[int]]] | None = None,
    move_critical_sample: bool = False,
    move_hit_evidence: bool = True,
) -> bool:
    source = buckets.get(source_key)
    if (
        source is None
        or source["damage"] < event.amount
        or (move_hit_evidence and source["hits"] <= 0)
    ):
        return False
    destination = buckets.setdefault(
        destination_key,
        _empty_bucket(destination_key, destination_name),
    )
    attribution_counts.setdefault(destination_key, Counter())
    method = str(event.attribution_method or "recorded")
    if source_key != destination_key:
        source["damage"] -= event.amount
        destination["damage"] += event.amount
        # Classifying a physical record into an extra-damage lane moves its
        # Hit evidence with the record. Folding that already classified
        # damage back into the triggering/effect skill is presentation-only:
        # it must add damage, but must not manufacture another native skill
        # Hit or inflate that skill's average Hit count.
        if move_hit_evidence:
            source["hits"] -= 1
            attribution_counts.setdefault(source_key, Counter())[method] -= 1
            destination["hits"] += 1
            attribution_counts[destination_key][method] += 1
        if (
            move_hit_evidence
            and move_critical_sample
            and is_critical_rate_sample(event)
        ):
            source["critical_samples"] = max(
                0, int(source.get("critical_samples", 0)) - 1
            )
            destination["critical_samples"] = int(
                destination.get("critical_samples", 0)
            ) + 1
            if event.critical:
                source["critical_hits"] = max(
                    0, int(source.get("critical_hits", 0)) - 1
                )
                destination["critical_hits"] = int(
                    destination.get("critical_hits", 0)
                ) + 1
    if additional_counts is not None and effect_id:
        effect_key = (effect_id, _display_effect_name(effect_name))
        detail = additional_counts.setdefault(destination_key, {}).setdefault(
            effect_key, [0, 0]
        )
        detail[0] += 1
        detail[1] += event.amount
    return True


def _finalize_rows(
    buckets: dict[str, dict[str, Any]],
    attribution_counts: dict[str, Counter[str]],
    additional_counts: dict[str, dict[tuple[str, str], list[int]]] | None,
    player_damage: int,
    factual_hit_counts: dict[tuple[str, str], int] | None = None,
    source_id: str = "",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, row in buckets.items():
        if row["damage"] <= 0 and row["healing"] <= 0:
            continue
        row["share"] = row["damage"] / max(1, player_damage)
        # Keep damage-record accounting available for diagnostics, but expose
        # only gameplay Hits in the skill-composition columns.  An additional
        # result is a damage record, never another native skill Hit.
        family = _cast_family_name_key(row.get("name"))
        factual_key = (source_id, family)
        if key.startswith("additional:"):
            row["damage_records"] = int(row.get("hits", 0) or 0)
            row["hits"] = 0
        elif factual_hit_counts is not None and factual_key in factual_hit_counts:
            row["damage_records"] = int(row.get("hits", 0) or 0)
            row["hits"] = int(factual_hit_counts[factual_key])
        cast_count = int(row.get("cast_count", 0) or 0)
        row["average_hits"] = (
            int(row.get("hits", 0) or 0) / cast_count
            if cast_count > 0
            else None
        )
        row["attribution"] = [
            [method, count]
            for method, count in attribution_counts.get(key, {}).most_common()
            if count > 0
        ]
        # Trigger links are an audit detail, not a separate composition row.
        # The attributed view has already folded the damage into its actual
        # destination, so the old small "关联：..." line is intentionally gone.
        row["trigger_skills"] = []
        row["additional_damage"] = [
            [effect_id, effect_name, values[0], values[1]]
            for (effect_id, effect_name), values in sorted(
                (additional_counts or {}).get(key, {}).items(),
                key=lambda item: (item[1][1], item[1][0], item[0][1]),
                reverse=True,
            )
        ]
        rows.append(row)
    rows.sort(key=lambda row: (row["damage"], row["healing"]), reverse=True)
    return rows


def _can_merge_legacy_burning_hand_trigger(event: CombatEvent) -> bool:
    # Compatibility for snapshots predating pre-split provenance. Those
    # records separate proven explosion ownership (0.99) from an inherited
    # fire-skill slot (0.8). Do not upgrade generic recent-cast guesses.
    if event.trigger_attribution_confidence >= 0.95:
        return True
    return bool(
        event.trigger_attribution_confidence >= BURNING_HAND_WIRE_TRIGGER_CONFIDENCE
        and event.trigger_attribution_method == BURNING_HAND_WIRE_TRIGGER_METHOD
        and event.trigger_skill_id in BURNING_HAND_TRIGGER_SKILL_IDS
        and event.raw_skill_id == event.trigger_skill_id
        and event.cast_instance_id.startswith("instant-action:2301:")
        and event.attribution_confidence >= 0.95
        and event.has_real_hit_evidence is True
        and "direct41" in event.wire_damage_layout
    )


def _burning_hand_display_destination(event: CombatEvent) -> tuple[str, str] | None:
    # New records carry the exact pre-split identity and its existing proof.
    # Restore that identity directly: raw candidates and a second independent
    # trigger-confidence gate must not veto an already accepted skill owner.
    if event.burning_hand_origin_confirmed:
        skill_id = event.burning_hand_origin_skill_id
        return (
            (skill_id, event.burning_hand_origin_skill_name or skill_id)
            if skill_id and skill_id != _BURNING_HAND_SKILL_ID
            else None
        )
    if (
        event.burning_hand_origin_skill_id
        or event.burning_hand_origin_skill_name
        or event.burning_hand_origin_attribution_method
    ):
        # The decoder explicitly recorded an unconfirmed pre-split identity.
        # Never upgrade it via legacy raw-slot/trigger hints.
        return None
    # Compatibility only for old snapshots lacking pre-split provenance.
    if (
        (event.trigger_skill_id or event.trigger_skill_name)
        and _can_merge_legacy_burning_hand_trigger(event)
    ):
        return (
            event.trigger_skill_id or event.trigger_skill_name,
            event.trigger_skill_name or event.trigger_skill_id,
        )
    return None


def _merge_burning_hand_display_rows(
    rows: list[dict[str, Any]],
    events: tuple[CombatEvent, ...],
    source_id: str,
    player_damage: int,
    followup_links: dict[int, tuple[int, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Restore Burning Hand's pre-split owner in the optional composition view.

    Restore confirmed pre-split skill ownership directly. Older snapshots can
    use their reviewed legacy trigger links; unconfirmed pre-split identities
    remain independent. This changes skill-composition presentation only, not
    raw damage or the originating skill's Hit count and critical samples.
    """

    buckets = {
        str(row.get("skill_id") or row.get("name") or "unknown"): deepcopy(row)
        for row in rows
    }
    attribution_counts = _attribution_counters(buckets)
    additional_counts = _additional_counters(buckets)
    changed = False
    for event_index, event in enumerate(events):
        trigger = event
        effect_id = effect_name = ""
        if followup_links and event_index in followup_links:
            trigger_index, effect_id, effect_name = followup_links[event_index]
            trigger = events[trigger_index]
        if (
            event.source_id != source_id
            or trigger.source_id != source_id
            or str(trigger.skill_id or "") != _BURNING_HAND_SKILL_ID
            or str(trigger.attribution_method or "").casefold()
            != _BURNING_HAND_METHOD
        ):
            continue
        destination = _burning_hand_display_destination(trigger)
        if destination is None:
            continue
        destination_key, destination_name = destination
        moved = _move_event(
            buckets,
            attribution_counts,
            event,
            _BURNING_HAND_SKILL_ID,
            destination_key,
            destination_name,
            effect_id=effect_id,
            effect_name=effect_name,
            additional_counts=additional_counts,
            move_hit_evidence=False,
        )
        if moved and effect_id:
            # A verified additional result already folded into Burning Hand
            # follows that exact explosion to its fire skill. Move its detail
            # as well; do not distribute aggregate extras by damage ratio.
            detail = additional_counts.get(_BURNING_HAND_SKILL_ID, {}).get(
                (effect_id, _display_effect_name(effect_name))
            )
            if detail is not None:
                detail[0] -= 1
                detail[1] -= event.amount
                if detail[0] <= 0 and detail[1] <= 0:
                    del additional_counts[_BURNING_HAND_SKILL_ID][
                        (effect_id, _display_effect_name(effect_name))
                    ]
        elif moved:
            # Partial folds can leave genuinely unknown explosions visible.
            # Their row must retain only the remaining explosion evidence,
            # without adding any passive Hit/critical sample to the fire skill.
            source = buckets[_BURNING_HAND_SKILL_ID]
            source["hits"] = max(0, int(source.get("hits", 0)) - 1)
            if "damage_records" in source:
                source["damage_records"] = max(0, int(source["damage_records"]) - 1)
            attribution_counts[_BURNING_HAND_SKILL_ID][
                str(event.attribution_method or "recorded")
            ] -= 1
            if is_critical_rate_sample(event):
                source["critical_samples"] = max(
                    0, int(source.get("critical_samples", 0)) - 1
                )
                if event.critical:
                    source["critical_hits"] = max(
                        0, int(source.get("critical_hits", 0)) - 1
                    )
        changed = moved or changed
    if not changed:
        return rows
    return _finalize_rows(
        buckets,
        attribution_counts,
        additional_counts,
        player_damage,
    )


def build_additional_damage_views(
    snapshot: CombatSnapshot,
    *,
    factual_hit_indices_out: set[int] | None = None,
    event_composition_out: dict[int, tuple[str, str]] | None = None,
    separated_composition_out: dict[int, tuple[str, str]] | None = None,
    burning_hand_followups_out: dict[int, tuple[int, str, str]] | None = None,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, Any],
]:
    """Build separated and merged extra-damage composition views.

    Periodic/status damage is deliberately untouched.  Only independent
    additional-damage records are classified.  The separated view uses stable
    ``额外伤害（效果）`` / ``额外伤害（未确定）`` buckets; the attributed
    view then folds confirmed records into the factual trigger skill, except
    owner-side Debuffs (for example Bone Crash), which fold into the effect
    skill owned by the caster.
    """

    events = tuple(snapshot.combat_events or ())
    if not events:
        summary = {
            "available": False,
            "algorithmVersion": _ALGORITHM_VERSION,
            "reason": "完整事件尚未发布",
        }
        return {}, {}, summary
    factual_cast_counts = _snapshot_cast_counts(snapshot)
    jobs_by_source = {
        str(player.source_id): str(player.job or "") for player in snapshot.players
    }
    audits = audit_additional_damage_shadow(
        events,
        jobs_by_source,
        tuple(snapshot.skill_cast_events or ()),
        tuple(snapshot.boss_effects or ()),
        tuple(snapshot.player_buffs or ()),
        tuple(snapshot.skill_parameter_events or ()),
    )
    factual_hit_indices = _snapshot_factual_hit_event_indices(snapshot, audits)
    if factual_hit_indices_out is not None:
        factual_hit_indices_out.update(factual_hit_indices)
    factual_hit_counts = _snapshot_factual_hit_counts(snapshot, factual_hit_indices)
    audits_by_source: dict[str, list[tuple[ShadowAttribution, CombatEvent]]] = {}
    status_counts: Counter[str] = Counter()
    for audit in audits:
        status_counts[audit.status] += 1
        event = events[audit.event_index]
        if _is_periodic_status_event(event):
            continue
        audits_by_source.setdefault(event.source_id, []).append((audit, event))

    separated_views: dict[str, list[dict[str, Any]]] = {}
    attributed_views: dict[str, list[dict[str, Any]]] = {}
    classified_hits = 0
    classified_damage = 0
    merged_hits = 0
    merged_damage = 0
    recovered_native_hits = 0
    recovered_native_damage = 0

    for player in snapshot.players:
        separated = {
            str(skill.skill_id): _skill_payload(skill) for skill in player.skills
        }
        # The action-event axis is the authority for release counts. Repair
        # legacy/stale skill rows before any damage is split or folded so every
        # display mode keeps the same factual count and average-Hit denominator.
        _repair_cast_metrics(
            list(separated.values()),
            str(player.source_id),
            factual_cast_counts,
        )
        separated_attribution = _attribution_counters(separated)
        # Repair both fresh and legacy cached snapshots before classifying
        # effects.  Older versions accumulated every bounded recent-cast,
        # same-packet and direct-overflow row into one generic extra bucket.
        # Move only the exact method counts supported by the retained event
        # ledger; confirmed effect rows can subsequently move back into their
        # own effect bucket below.
        for event in events:
            if (
                event.source_id != player.source_id
                or not is_native_supplemental_damage_event(event)
            ):
                continue
            legacy_key = "supplemental:status_damage"
            method = str(event.attribution_method or "recorded")
            if separated_attribution.get(legacy_key, Counter()).get(method, 0) <= 0:
                continue
            native_skill_id, native_skill_name = native_supplemental_skill_identity(event)
            destination_key = str(native_skill_id or native_skill_name or "unknown")
            destination_name = str(native_skill_name or native_skill_id or "unknown")
            if _move_event(
                separated,
                separated_attribution,
                event,
                legacy_key,
                destination_key,
                destination_name,
                move_critical_sample=True,
            ):
                recovered_native_hits += 1
                recovered_native_damage += event.amount
        classified_events: list[
            tuple[ShadowAttribution, CombatEvent, str]
        ] = []
        for audit, event in audits_by_source.get(player.source_id, ()):
            # Candidate ownership is not damage semantics.  An unmatched
            # recent-cast/same-packet row can be a native skill continuation,
            # projectile or summoned-object result.  Only a wire-level status
            # result or a positively identified effect may enter the extra
            # lane; otherwise keep the event in its existing skill bucket.
            if (
                audit.status not in _APPLIED_STATUSES
                and not is_intrinsic_additional_damage_event(event)
            ):
                continue
            source_key, _source_name = _composition_identity(event)
            destination_key, destination_name = _extra_bucket_identity(audit)
            if _move_event(
                separated,
                separated_attribution,
                event,
                source_key,
                destination_key,
                destination_name,
            ):
                classified_events.append((audit, event, destination_key))
                if event_composition_out is not None:
                    event_composition_out[audit.event_index] = (
                        destination_key, destination_name
                    )
                if separated_composition_out is not None:
                    separated_composition_out[audit.event_index] = (destination_key, destination_name)
                classified_hits += 1
                classified_damage += event.amount

        separated_views[str(player.source_id)] = _finalize_rows(
            deepcopy(separated),
            {key: Counter(value) for key, value in separated_attribution.items()},
            None,
            int(player.damage),
            factual_hit_counts,
            str(player.source_id),
        )

        attributed = deepcopy(separated)
        attributed_attribution = {
            key: Counter(value) for key, value in separated_attribution.items()
        }
        additional_counts = _additional_counters(attributed)
        for audit, event, source_key in classified_events:
            if audit.status not in _APPLIED_STATUSES or not audit.effect_skill_id:
                continue
            # Equipment suffix damage has its own composition lane.  It must
            # remain visible as a suffix source even when an ordinary Hit is
            # nearby; folding it into that skill would erase the distinction
            # from profession Buff/passive/Debuff damage.
            if audit.effect_type == "装备后缀额外伤害":
                continue
            rule = _RULES_BY_ID.get(audit.effect_skill_id)
            destination_key = ""
            destination_name = ""
            cross_owner_trigger = bool(
                0 <= audit.trigger_event_index < len(events)
                and events[audit.trigger_event_index].source_id != event.source_id
            )
            # The server may credit a target-side proc to the effect owner
            # even when another party member supplied the factual trigger.
            # Never invent that teammate's skill in the owner's skill list.
            if cross_owner_trigger or (
                rule is not None and rule.composition_destination == "effect_skill"
            ):
                destination_key = audit.effect_skill_id
                existing = attributed.get(destination_key)
                destination_name = (
                    str(existing.get("name") or "") if existing else ""
                ) or _display_effect_name(audit.effect_name)
            elif audit.trigger_skill_id or audit.trigger_skill_name:
                destination_key = audit.trigger_skill_id or audit.trigger_skill_name
                destination_name = audit.trigger_skill_name or audit.trigger_skill_id
            if not destination_key:
                continue
            if _move_event(
                attributed,
                attributed_attribution,
                event,
                source_key,
                destination_key,
                destination_name,
                effect_id=audit.effect_skill_id,
                effect_name=audit.effect_name,
                additional_counts=additional_counts,
                move_hit_evidence=False,
            ):
                if (
                    burning_hand_followups_out is not None
                    and destination_key == _BURNING_HAND_SKILL_ID
                    and not cross_owner_trigger
                    and 0 <= audit.trigger_event_index < len(events)
                    and events[audit.trigger_event_index].skill_id
                    == _BURNING_HAND_SKILL_ID
                ):
                    burning_hand_followups_out[audit.event_index] = (
                        audit.trigger_event_index,
                        audit.effect_skill_id,
                        audit.effect_name,
                    )
                if event_composition_out is not None:
                    event_composition_out[audit.event_index] = (
                        destination_key, destination_name
                    )
                merged_hits += 1
                merged_damage += event.amount

        attributed_views[str(player.source_id)] = _finalize_rows(
            attributed,
            attributed_attribution,
            additional_counts,
            int(player.damage),
            factual_hit_counts,
            str(player.source_id),
        )

    summary = {
        "available": True,
        "algorithmVersion": _ALGORITHM_VERSION,
        "mode": "merge-confirmed-extra-damage",
        "classifiedHits": classified_hits,
        "classifiedDamage": classified_damage,
        "mergedHits": merged_hits,
        "mergedDamage": merged_damage,
        "recoveredNativeHits": recovered_native_hits,
        "recoveredNativeDamage": recovered_native_damage,
        # Compatibility with existing consumers of the v1 summary.
        "movedHits": merged_hits,
        "movedDamage": merged_damage,
        "statusCounts": dict(status_counts),
        "equipmentSuffixDamageStatusCount": 513,
        "note": (
            "仅归因额外伤害；周期状态伤害和主动技能延续Hit保持原样。"
            "具独立状态包证据但无法确定来源的结果单列未确定，"
            "不借最近施放并入原技能。"
            "最近施法候选、同包续段和直接Hit溢出不再自动视为额外伤害；"
            "同一事实Hit允许触发多个独立效果；装备后缀使用独立通道；"
            "同效果的密集多Hit批次按物理记录一对一分配，避免最近一条Hit"
            "被重复争用后漏算；"
            "确定来源并入触发技能，连骨等施放者Debuff并入效果技能；"
            "归因仅合并伤害额，不增加原技能Hit、平均Hit或暴击样本；"
            "技能命中与平均Hit只统计具备真实命中证据的直伤/投射物记录，"
            "状态与追加伤害仍计入伤害但不伪装成原技能Hit；"
            "总伤害、DPS和评分不变"
        ),
    }
    return separated_views, attributed_views, summary


def build_attributed_skill_views(
    snapshot: CombatSnapshot,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    _separated, attributed, summary = build_additional_damage_views(snapshot)
    return attributed, summary


def attach_attributed_skill_views(
    payload: dict[str, Any], snapshot: CombatSnapshot
) -> dict[str, Any]:
    factual_hit_indices: set[int] = set()
    event_composition: dict[int, tuple[str, str]] = {}
    separated_composition: dict[int, tuple[str, str]] = {}
    burning_hand_followups: dict[int, tuple[int, str, str]] = {}
    separated, attributed, summary = build_additional_damage_views(
        snapshot,
        factual_hit_indices_out=factual_hit_indices,
        event_composition_out=event_composition,
        separated_composition_out=separated_composition,
        burning_hand_followups_out=burning_hand_followups,
    )
    combat_rows = payload.get("combat_events")
    if isinstance(combat_rows, list) and len(combat_rows) == len(snapshot.combat_events):
        for index, row in enumerate(combat_rows):
            if isinstance(row, dict):
                row["is_gameplay_hit"] = index in factual_hit_indices
                if index in event_composition:
                    row["composition_skill_id"], row["composition_skill_name"] = (
                        event_composition[index]
                    )
        factual_hits_by_source = Counter(
            snapshot.combat_events[index].source_id
            for index in factual_hit_indices
        )
        for player in payload.get("players", ()):
            if isinstance(player, dict):
                player["damage_records"] = player.get("hits", 0)
                player["hits"] = factual_hits_by_source.get(
                    str(player.get("source_id") or ""), 0
                )
    factual_cast_counts = _snapshot_cast_counts(snapshot)
    for player in payload.get("players", ()):
        if not isinstance(player, dict):
            continue
        source_id = str(player.get("source_id") or "")
        original_skills = player.get("skills")
        if isinstance(original_skills, list):
            _repair_cast_metrics(
                original_skills,
                source_id,
                factual_cast_counts,
            )
        if source_id in separated:
            player["skills_unattributed"] = separated[source_id]
            player["skills_unattributed_burning_hand_merged"] = (
                _merge_burning_hand_display_rows(
                    separated[source_id],
                    tuple(snapshot.combat_events or ()),
                    source_id,
                    int(player.get("damage", 0) or 0),
                )
            )
        if source_id in attributed:
            player["skills_attributed"] = attributed[source_id]
            player["skills_attributed_burning_hand_merged"] = (
                _merge_burning_hand_display_rows(
                    attributed[source_id],
                    tuple(snapshot.combat_events or ()),
                    source_id,
                    int(player.get("damage", 0) or 0),
                    burning_hand_followups,
                )
            )
    from .circle_swing_composition import split_composition
    split_composition(payload,snapshot,factual_hit_indices,event_composition,separated_composition)
    summary["burningHandDisplaySelectable"] = True
    payload["additional_damage_attribution"] = summary
    return payload
