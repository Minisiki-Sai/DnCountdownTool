from __future__ import annotations

import codecs
import copy
import ctypes
import dataclasses
import hashlib
import itertools
import json
import math
import os
import queue
import re
import shutil
import socket
import sqlite3
import struct
import threading
import time
import traceback
import uuid
from bisect import bisect_left, bisect_right, insort
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

def _live_target_identity(event: CombatEvent | BossStatusEvent) -> tuple[str, str]:
    name = str(event.target_name or "").strip().casefold()
    if name:
        return (
            "name_hp",
            f"{name}|{max(0, int(event.target_max_hp))}",
        )
    target_id = str(event.target_id or "")
    if "@" in target_id:
        return ("template", target_id.rsplit("@", 1)[1])
    return ("actor", target_id)

def _canonical_live_skill_id(skill_id: int | None) -> int | None:
    if skill_id is None:
        return None
    return _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)

def _canonicalize_live_message(
    decoded: DecodedV576Message,
) -> DecodedV576Message:
    """Normalize alternate wire actions before job and cast matching."""
    skill_id = _canonical_live_skill_id(decoded.skill_id)
    candidates = tuple(
        dict.fromkeys(
            int(_canonical_live_skill_id(candidate) or candidate)
            for candidate in decoded.candidate_skill_ids
        )
    )
    if skill_id == decoded.skill_id and candidates == decoded.candidate_skill_ids:
        return decoded
    # Keep appended_status_skill_id raw. Periodic-state matching uses the
    # internal status ID before resolving its player-facing owner.
    return replace(
        decoded,
        skill_id=skill_id,
        candidate_skill_ids=candidates,
    )

def _supplement_boss_statuses_from_casts(
    events: list[LiveDpsEvent],
) -> list[LiveDpsEvent]:
    """Fill missing known Boss debuffs from local cast/hit evidence.

    Some v576 packets expose the cast and damage target but omit the status
    record for the main Boss.  The fallback is deliberately local: it only
    emits a known semantic rule when a non-self target receives damage
    within five seconds of that cast, and it never replaces a packet status.
    """
    casts = [event for event in events if isinstance(event, SkillCastEvent)]
    boss_hits = [
        event
        for event in events
        if isinstance(event, CombatEvent)
        and not event.healing
        and event.amount > 0
        and event.target_id
        and event.target_id != event.source_id
    ]
    if not casts or not boss_hits:
        return events

    hits_by_second: dict[int, list[CombatEvent]] = defaultdict(list)
    for hit in boss_hits:
        hits_by_second[max(0, hit.timestamp_ms // 1000)].append(hit)
    packet_statuses: dict[
        tuple[str, str, str], list[BossStatusEvent]
    ] = defaultdict(list)
    for event in events:
        if isinstance(event, BossStatusEvent) and event.target_scope != "player":
            packet_statuses[
                (
                    event.source_id,
                    _summon_skill_key(event.skill_name),
                    event.effect_key,
                )
            ].append(event)
    packet_backed_status_keys = {
        key
        for key, rows in packet_statuses.items()
        if any(row.effect_code != "cast_inferred" for row in rows)
    }
    # Long captures can contain thousands of refresh records for one effect.
    # The previous per-cast ``any(...)`` scan walked the complete same-effect
    # history every time, making this replay-only supplement approach O(n^2).
    # Index the exact same target/timestamp evidence once and query only the
    # relevant three-second interval.  Keep inferred entries in the index so
    # later casts retain the previous duplicate-suppression behaviour.
    status_times_by_target: dict[
        tuple[tuple[str, str, str], tuple[str, str]], list[int]
    ] = defaultdict(list)
    for status_key, rows in packet_statuses.items():
        for status in rows:
            status_times_by_target[
                (status_key, _live_target_identity(status))
            ].append(status.timestamp_ms)
    for timestamps in status_times_by_target.values():
        timestamps.sort()

    inferred: list[BossStatusEvent] = []
    for cast in casts:
        rules = tuple(
            rule
            for rule in boss_effect_rules(cast.skill_name, cast.job)
            if rule.value is not None and rule.duration_seconds > 0
        )
        if not rules:
            continue
        cast_second = max(0, cast.timestamp_ms // 1000)
        candidates = [
            hit
            for second in range(max(0, cast_second - 1), cast_second + 6)
            for hit in hits_by_second.get(second, ())
            if -250 <= hit.timestamp_ms - cast.timestamp_ms <= 5000
        ]
        if not candidates:
            continue
        same_skill = [
            hit
            for hit in candidates
            if hit.source_id == cast.source_id
            and (
                hit.skill_id == cast.skill_id
                or _summon_skill_key(hit.skill_name)
                == _summon_skill_key(cast.skill_name)
            )
        ]
        target_hit = min(
            same_skill or candidates,
            key=lambda hit: (
                hit.target_job.strip().partition(" ")[0].casefold() != "boss",
                -max(0, hit.target_max_hp),
                not bool(hit.target_name.strip()),
                hit.timestamp_ms < cast.timestamp_ms,
                abs(hit.timestamp_ms - cast.timestamp_ms),
            ),
        )
        start_ms = max(cast.timestamp_ms, target_hit.timestamp_ms)
        target_identity = _live_target_identity(target_hit)
        for rule in rules:
            status_key = (
                cast.source_id,
                _summon_skill_key(cast.skill_name),
                rule.key,
            )
            # Once this capture proves that the server emits a real status for
            # this source/skill/effect, missing applications are meaningful.
            # Do not turn every damaging cast into a successful debuff: a
            # weaker same-type effect may be rejected while a stronger one is
            # active. Cast+hit inference remains a fallback only when the
            # capture contains no protocol-backed status for this effect.
            if status_key in packet_backed_status_keys:
                continue
            matching_times = status_times_by_target.get(
                (status_key, target_identity),
                (),
            )
            first_possible = cast.timestamp_ms - 250
            match_index = bisect_left(matching_times, first_possible)
            already_recorded = (
                match_index < len(matching_times)
                and matching_times[match_index] <= cast.timestamp_ms + 3000
            )
            if already_recorded:
                continue
            status = BossStatusEvent(
                timestamp_ms=start_ms,
                target_id=target_hit.target_id,
                source_id=cast.source_id,
                source_name=cast.source_name,
                job=cast.job,
                skill_id=cast.skill_id,
                skill_name=cast.skill_name,
                effect_key=rule.key,
                effect_label=boss_effect_label(rule.key),
                value=rule.value,
                duration_ms=max(1, round(rule.duration_seconds * 1000)),
                status_kind="Boss减益",
                effect_code="cast_inferred",
                parameters="由技能施放与Boss命中补全",
                stackable=rule.stackable,
                stack_group=rule.stack_group,
                target_name=target_hit.target_name,
                target_job=target_hit.target_job,
                target_scope="boss",
                target_max_hp=target_hit.target_max_hp,
            )
            inferred.append(status)
            packet_statuses[status_key].append(status)
            indexed_times = status_times_by_target[
                (status_key, target_identity)
            ]
            insort(indexed_times, status.timestamp_ms)
    if not inferred:
        return events
    return sorted([*events, *inferred], key=lambda event: event.timestamp_ms)

def _supplement_player_buffs_from_casts(
    events: list[LiveDpsEvent],
    team_buff_skill_names: frozenset[str],
) -> list[LiveDpsEvent]:
    """Recover an isolated missing player-buff packet from nearby applications.

    A diagnostic capture can occasionally lose one complete status fan-out
    even though the accepted cast is present.  Reuse the same skill's observed
    packet shape and its normal cast-to-application delay, but only after the
    previous buff has expired and the cast was not interrupted by another
    action.  Periodically refreshed buffs remain packet-only because a cast
    alone cannot prove how long their channel or summon kept refreshing.
    """
    configured_team_keys = {
        _summon_skill_key(skill_name)
        for skill_name in team_buff_skill_names
    }
    periodic_keys = {
        _summon_skill_key(skill_name)
        for skill_name in (
            "查克拉治愈术",
            "怨恨之像",
            "阿兹那之息",
            "痊愈之手",
            "火焰光环",
            "寒冰光环",
            "元素光环",
            "判定光环",
        )
    }
    all_statuses: list[BossStatusEvent] = []
    all_casts: list[SkillCastEvent] = []
    all_casts_by_source: dict[str, list[SkillCastEvent]] = defaultdict(list)
    # Build the two replay indexes in one pass.  On long captures statuses can
    # account for most of the event stream; walking the full list three times
    # here was visible in every cold load even when no inferred Buff was added.
    for event in events:
        if isinstance(event, SkillCastEvent):
            all_casts.append(event)
            all_casts_by_source[event.source_id].append(event)
        elif (
            isinstance(event, BossStatusEvent)
            and event.target_scope == "player"
            and event.operation != "remove"
        ):
            all_statuses.append(event)
    observed_keys = {
        _summon_skill_key(status.skill_name) for status in all_statuses
    }
    known_keys = configured_team_keys | observed_keys
    casts = [
        event
        for event in all_casts
        if _summon_skill_key(event.skill_name) in known_keys
    ]
    statuses = [
        status
        for status in all_statuses
        if _summon_skill_key(status.skill_name) in known_keys
    ]
    if not casts or not statuses:
        return events

    casts_by_key: dict[str, list[SkillCastEvent]] = defaultdict(list)
    statuses_by_key: dict[str, list[BossStatusEvent]] = defaultdict(list)
    for cast in casts:
        casts_by_key[_summon_skill_key(cast.skill_name)].append(cast)
    for status in statuses:
        statuses_by_key[_summon_skill_key(status.skill_name)].append(status)

    inferred: list[BossStatusEvent] = []
    for skill_key, skill_casts in casts_by_key.items():
        if skill_key in periodic_keys:
            continue
        skill_statuses = sorted(
            statuses_by_key.get(skill_key, ()),
            key=lambda event: event.timestamp_ms,
        )
        if not skill_statuses:
            continue

        clusters: list[list[BossStatusEvent]] = []
        for status in skill_statuses:
            if (
                not clusters
                or status.timestamp_ms - clusters[-1][0].timestamp_ms > 50
            ):
                clusters.append([status])
            else:
                clusters[-1].append(status)

        matched_offsets: list[tuple[str, str, int]] = []
        for cast in skill_casts:
            matching = [
                cluster
                for cluster in clusters
                if any(row.source_id == cast.source_id for row in cluster)
                and -250
                <= cluster[0].timestamp_ms - cast.timestamp_ms
                <= 5000
            ]
            if matching:
                cluster = min(
                    matching,
                    key=lambda rows: abs(
                        rows[0].timestamp_ms - cast.timestamp_ms
                    ),
                )
                matched_offsets.append(
                    (
                        cast.source_id,
                        cast.job,
                        cluster[0].timestamp_ms - cast.timestamp_ms,
                    )
                )

        if not matched_offsets:
            continue
        all_offsets = sorted(offset for _source, _job, offset in matched_offsets)
        for cast in skill_casts:
            matching = [
                cluster
                for cluster in clusters
                if any(row.source_id == cast.source_id for row in cluster)
                and -250
                <= cluster[0].timestamp_ms - cast.timestamp_ms
                <= 5000
            ]
            if matching:
                continue
            if any(
                status.source_id == cast.source_id
                and status.timestamp_ms <= cast.timestamp_ms
                < status.timestamp_ms + max(1, status.duration_ms)
                for status in skill_statuses
            ):
                continue

            source_offsets = sorted(
                offset
                for source_id, _job, offset in matched_offsets
                if source_id == cast.source_id
            )
            job_offsets = sorted(
                offset
                for _source_id, job, offset in matched_offsets
                if job == cast.job
            )
            offsets = source_offsets or job_offsets or all_offsets
            application_offset = offsets[len(offsets) // 2]
            application_ms = cast.timestamp_ms + application_offset
            if any(
                cast.timestamp_ms < other.timestamp_ms < application_ms - 50
                for other in all_casts_by_source.get(cast.source_id, ())
            ):
                continue

            same_source = [
                cluster
                for cluster in clusters
                if any(row.source_id == cast.source_id for row in cluster)
            ]
            same_job = [
                cluster
                for cluster in clusters
                if any(row.job == cast.job for row in cluster)
            ]
            template = min(
                same_source or same_job or clusters,
                key=lambda rows: abs(rows[0].timestamp_ms - cast.timestamp_ms),
            )
            seen: set[tuple[object, ...]] = set()
            for row in template:
                identity = (
                    row.target_id,
                    row.effect_key,
                    row.value,
                    row.duration_ms,
                    row.status_kind,
                    row.effect_code,
                    row.parameters,
                )
                if identity in seen:
                    continue
                seen.add(identity)
                inferred.append(
                    replace(
                        row,
                        timestamp_ms=application_ms,
                        source_id=cast.source_id,
                        source_name=cast.source_name,
                        job=cast.job,
                        skill_id=cast.skill_id,
                        skill_name=cast.skill_name,
                    )
                )

    if not inferred:
        return events
    return sorted([*events, *inferred], key=lambda event: event.timestamp_ms)

def _backfill_preexisting_maintained_player_buffs(
    events: list[LiveDpsEvent],
) -> list[LiveDpsEvent]:
    """Retain only packet-backed prebattle inheritance.

    Older replays guessed that Cocktail/Raid had been active at battle start
    whenever their first ordinary refresh occurred within one full duration.
    That makes practically every first in-combat cast appear at ``T+0`` and
    cannot distinguish a genuine inherited Buff from a new cast.  The live
    probe now preserves authoritative prebattle rows with
    ``prebattle_inherited=True`` and their exact remaining duration, so a
    later refresh must never be projected backwards.

    Keep the compatibility hook for split-module callers, but deliberately do
    not synthesize events here.
    """
    return events

def _canonicalize_replay_actor_context(
    events: list[LiveDpsEvent], decoder: object
) -> list[LiveDpsEvent]:
    """Backfill both sides of every event from capture-local identity proof.

    Session identities (login/local actor, ``03 03`` spawn bindings and party
    identity rows) are deliberately collected before rewriting any event.
    This lets an identity learned late in a long replay repair earlier casts,
    damage, healing, Buff/Debuff and HP events without consulting the legacy
    cross-session ``known_players`` profession/name guesses.
    """

    from .character_identity_sessions import split_replay_character_sessions
    events, character_contexts = split_replay_character_sessions(events, decoder)

    actor_number_cache: dict[str, int | None] = {}

    def actor_number(value: object) -> int | None:
        text = str(value or "").strip()
        if text in actor_number_cache:
            return actor_number_cache[text]
        match = re.fullmatch(r"(?:profile:)?0x([0-9a-fA-F]{8})", text)
        result = int(match.group(1), 16) if match is not None else None
        actor_number_cache[text] = result
        return result

    names: dict[int, tuple[int, str]] = {}
    jobs: dict[int, tuple[int, str]] = {}

    def remember(
        actor_id: int | None,
        name: object,
        job: object,
        priority: int,
    ) -> None:
        if actor_id is None or actor_id <= 0:
            return
        job_text = str(job or "").strip()
        name_text = str(name or "").strip()
        if (
            name_text
            and not _is_generic_replay_actor_name(
                name_text, f"0x{actor_id:08X}", job_text
            )
            and priority >= names.get(actor_id, (-1, ""))[0]
        ):
            names[actor_id] = (priority, name_text)
        if job_text and priority >= jobs.get(actor_id, (-1, ""))[0]:
            jobs[actor_id] = (priority, job_text)

    # Final decoder state contains only session-local names: pre-capture 03 03
    # cache and identities decoded inside this recording. Legacy configured
    # names are no longer inserted into this map.
    for actor_id, name in getattr(decoder, "known_actor_names", {}).items():
        actor_job = getattr(decoder, "actor_jobs", {}).get(actor_id, ("", 99))[0]
        remember(actor_id, name, actor_job, 400)
    for actor_id, actor_job in getattr(decoder, "actor_jobs", {}).items():
        remember(actor_id, "", actor_job[0], 400)
    local_name = str(getattr(decoder, "local_name", "") or "").strip()
    for actor_id in getattr(decoder, "local_actor_ids", set()):
        actor_job = getattr(decoder, "actor_jobs", {}).get(actor_id, ("", 99))[0]
        remember(actor_id, local_name, actor_job, 500)

    # A client-originated party/action row can only belong to the recording
    # client.  Some old captures lose the separate local actor marker, so use
    # the contributor's exact one-row invariant as a second, capture-local
    # bridge from the UDP login name to the runtime actor ID.
    c2s_party_actor_ids = {
        actor_number(event.actor_id)
        for event in events
        if isinstance(event, PartyMemberEvent)
        and str(event.source_layout or "").startswith("c2s-")
    }
    c2s_party_actor_ids.discard(None)
    if local_name and len(c2s_party_actor_ids) == 1:
        actor_id = next(iter(c2s_party_actor_ids))
        actor_job = getattr(decoder, "actor_jobs", {}).get(actor_id, ("", 99))[0]
        remember(actor_id, local_name, actor_job, 510)

    # Final fallback for old recordings: 22 10 contains name + exact second
    # job but no actor ID.  Join only a unique remaining name to a unique
    # remaining actor of that job.  Duplicate jobs stay unresolved unless a
    # direct local/03 03/inspect identity has already removed the ambiguity.
    roster_names_by_job = {
        str(job): {str(name).strip() for name in raw_names if str(name).strip()}
        for job, raw_names in getattr(
            decoder, "tcp_party_roster_names_by_job", {}
        ).items()
    }
    actor_ids_by_job: dict[str, set[int]] = {}
    for actor_id, actor_job in getattr(decoder, "actor_jobs", {}).items():
        job = str(actor_job[0] or "").strip()
        if job and actor_number(f"0x{int(actor_id):08X}") is not None:
            actor_ids_by_job.setdefault(job, set()).add(int(actor_id))
    # Restrict the join to actors that actually produced party rows. Actor-job
    # tables may also contain profile IDs or stale reconnect aliases.
    party_actor_ids = {
        actor_number(event.actor_id)
        for event in events
        if isinstance(event, PartyMemberEvent)
    }
    party_actor_ids.discard(None)
    for job, roster_names in roster_names_by_job.items():
        actors = actor_ids_by_job.get(job, set()) & party_actor_ids
        claimed_names = {
            names[actor_id][1]
            for actor_id in actors
            if actor_id in names and names[actor_id][1] in roster_names
        }
        unresolved_actors = [actor_id for actor_id in actors if actor_id not in names]
        remaining_names = roster_names - claimed_names
        if len(unresolved_actors) == 1 and len(remaining_names) == 1:
            remember(
                unresolved_actors[0],
                next(iter(remaining_names)),
                job,
                470,
            )

    # Capture-local event evidence can be more specific than the decoder's
    # final actor table (notably after reconnect). Prefer it over cached spawn
    # aliases, while rejecting profession labels and anonymous placeholders.
    for event in events:
        if isinstance(event, PartyMemberEvent):
            remember(actor_number(event.actor_id), event.name, event.job, 460)
        elif isinstance(event, PlayerInspectEvent):
            remember(actor_number(event.target_id), event.target_name, event.job, 450)
        elif isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
            remember(
                actor_number(event.target_id),
                event.target_name,
                event.target_job,
                440,
            )
        elif isinstance(event, SummonRemovalEvent):
            remember(
                actor_number(event.owner_id),
                event.owner_name,
                event.owner_job,
                440,
            )

    # The final runtime UID may now belong to another named local character.
    # Each proved character session has its own presentation identity/context.
    for actor_id, (name, job) in character_contexts.items():
        remember(actor_id, name, job, 1000)

    def context(actor_id: str, name: str, job: str) -> tuple[str, str]:
        numeric = actor_number(actor_id)
        if numeric is None:
            return name, job
        resolved_name = names.get(numeric, (0, ""))[1]
        resolved_job = jobs.get(numeric, (0, ""))[1]
        return resolved_name or name, resolved_job or job

    result: list[LiveDpsEvent] = []
    for event in events:
        if isinstance(event, PlayerInspectEvent):
            actor_id = actor_number(event.target_id)
            if actor_id is not None and actor_id not in character_contexts:
                actor_job, actor_job_index = decoder.actor_jobs.get(
                    actor_id, ("", 99)
                )
                current_index = decoder.job_name_indexes.get(event.job, 99)
                if actor_job and actor_job_index < 0 and actor_job_index < current_index:
                    evidence = (
                        "职业识别依据",
                        f"同角色 UDP 二转技能：{actor_job}",
                    )
                    fields = event.fields
                    if evidence not in fields:
                        fields = (*fields, evidence)
                    event = replace(event, job=actor_job, fields=fields)
            target_name, target_job = context(
                event.target_id, event.target_name, event.job
            )
            result.append(
                event
                if (
                    target_name == event.target_name
                    and target_job == event.job
                )
                else replace(event, target_name=target_name, job=target_job)
            )
            continue
        if isinstance(event, BossSpawnEvent):
            result.append(event)
            continue
        if isinstance(event, PartyMemberEvent):
            member_name, member_job = context(
                event.actor_id, event.name, event.job
            )
            numeric = actor_number(event.actor_id)
            confidence = event.confidence
            if (
                numeric is not None
                and numeric in names
                and not _is_generic_replay_actor_name(
                    member_name, event.actor_id, member_job
                )
            ):
                # The final name came from a session-local identity packet or
                # an exact party/health identity row. Preserve that proof for
                # downstream private history linking instead of retaining the
                # early anonymous event's 0.65 confidence.
                confidence = max(confidence, 0.95)
            result.append(
                event
                if (
                    member_name == event.name
                    and member_job == event.job
                    and confidence == event.confidence
                )
                else replace(
                    event,
                    name=member_name,
                    job=member_job,
                    confidence=confidence,
                )
            )
            continue
        if isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
            target_name, target_job = context(
                event.target_id, event.target_name, event.target_job
            )
            result.append(
                event
                if (
                    target_name == event.target_name
                    and target_job == event.target_job
                )
                else replace(
                    event,
                    target_name=target_name,
                    target_job=target_job,
                )
            )
            continue
        if isinstance(event, SummonRemovalEvent):
            owner_name, owner_job = context(
                event.owner_id, event.owner_name, event.owner_job
            )
            result.append(
                event
                if (
                    owner_name == event.owner_name
                    and owner_job == event.owner_job
                )
                else replace(
                    event,
                    owner_name=owner_name,
                    owner_job=owner_job,
                )
            )
            continue
        if isinstance(event, (CombatEvent, BossStatusEvent)):
            source_name, source_job = context(
                event.source_id, event.source_name, event.job
            )
            target_name, target_job = context(
                event.target_id, event.target_name, event.target_job
            )
            result.append(
                event
                if (
                    source_name == event.source_name
                    and source_job == event.job
                    and target_name == event.target_name
                    and target_job == event.target_job
                )
                else replace(
                    event,
                    source_name=source_name,
                    job=source_job,
                    target_name=target_name,
                    target_job=target_job,
                )
            )
            continue
        if isinstance(
            event, (SkillCastEvent, SkillParameterEvent, BossActionEvent)
        ):
            source_name, source_job = context(
                event.source_id, event.source_name, event.job
            )
            result.append(
                event
                if source_name == event.source_name and source_job == event.job
                else replace(event, source_name=source_name, job=source_job)
            )
            continue
        result.append(event)
    return result

@dataclass(slots=True)
class _ReplayPlayerSegment:
    actor_id: str
    job: str = ""
    name: str = ""
    first_cast_ms: int | None = None
    last_cast_ms: int | None = None
    cast_counts: Counter[str] = field(default_factory=Counter)
    profile_fingerprints: set[tuple[object, ...]] = field(default_factory=set)
    resource_fingerprints: set[tuple[int, int]] = field(default_factory=set)

@lru_cache(maxsize=4096)
def _is_generic_replay_actor_name(
    name: str, actor_id: str = "", job: str = ""
) -> bool:
    text = str(name or "").strip()
    folded = text.casefold()
    return (
        not text
        or folded == str(actor_id or "").strip().casefold()
        or folded == str(job or "").strip().casefold()
        or folded.startswith("角色 0x")
        or folded.startswith("actor 0x")
    )

def _replay_skill_signature_similarity(
    left: Counter[str], right: Counter[str]
) -> tuple[float, int, float, float]:
    left = Counter({key: count for key, count in left.items() if count > 0})
    right = Counter({key: count for key, count in right.items() if count > 0})
    shared = set(left).intersection(right)
    if not shared:
        return 0.0, 0, 0.0, 0.0
    left_norm = sum(value * value for value in left.values()) ** 0.5
    right_norm = sum(value * value for value in right.values()) ** 0.5
    cosine = (
        sum(left[key] * right[key] for key in shared)
        / max(1.0, left_norm * right_norm)
    )
    set_overlap = len(shared) / max(1, min(len(left), len(right)))
    left_total = max(1, sum(left.values()))
    right_total = max(1, sum(right.values()))
    distribution_overlap = sum(
        min(left[key] / left_total, right[key] / right_total)
        for key in shared
    )
    score = 0.55 * cosine + 0.25 * distribution_overlap + 0.20 * set_overlap
    return score, len(shared), set_overlap, distribution_overlap

def _replay_rejoined_player_aliases(
    events: list[LiveDpsEvent],
    profile_lineages: dict[str, tuple[tuple[str, int], ...]] | None = None,
) -> dict[str, str]:
    """Match a player whose runtime actor ID changed after leaving the party.

    Runtime IDs are session entities, so a leave/rejoin creates a new ID even
    for the same character. Anonymous captures cannot prove identity from one
    field alone. The fallback therefore requires non-overlapping cast ranges,
    a compatible class lineage and a strong, uniquely best skill-usage
    fingerprint. Exact spawn names, inspect profiles, or matching 21 0C
    HP/MP state provide stronger identity evidence when available.
    """
    segments: dict[str, _ReplayPlayerSegment] = {}

    lineage_keys_by_job: dict[str, set[str]] = defaultdict(set)
    for lineage_key, lineage in (profile_lineages or {}).items():
        for job_name, _job_index in lineage:
            normalized_job = str(job_name or "").strip()
            if normalized_job:
                lineage_keys_by_job[normalized_job].add(str(lineage_key))

    def compatible_jobs(left_job: str, right_job: str) -> bool:
        left_job = str(left_job or "").strip()
        right_job = str(right_job or "").strip()
        if not left_job or not right_job:
            return False
        if left_job == right_job:
            return True
        return bool(
            lineage_keys_by_job.get(left_job, set()).intersection(
                lineage_keys_by_job.get(right_job, set())
            )
        )

    def segment(actor_id: str) -> _ReplayPlayerSegment | None:
        actor_id = str(actor_id or "").strip()
        if re.fullmatch(r"0x[0-9A-Fa-f]{8}", actor_id) is None:
            return None
        return segments.setdefault(actor_id, _ReplayPlayerSegment(actor_id))

    for event in events:
        if isinstance(event, SkillCastEvent):
            row = segment(event.source_id)
            if row is None:
                continue
            row.job = event.job or row.job
            if not _is_generic_replay_actor_name(
                event.source_name, row.actor_id, event.job
            ):
                row.name = event.source_name
            row.first_cast_ms = (
                event.timestamp_ms
                if row.first_cast_ms is None
                else min(row.first_cast_ms, event.timestamp_ms)
            )
            row.last_cast_ms = (
                event.timestamp_ms
                if row.last_cast_ms is None
                else max(row.last_cast_ms, event.timestamp_ms)
            )
            skill_key = str(event.skill_id or event.skill_name or "").strip()
            if skill_key:
                row.cast_counts[skill_key] += 1
        elif isinstance(event, PartyMemberEvent):
            row = segment(event.actor_id)
            if row is not None:
                row.job = event.job or row.job
                if not _is_generic_replay_actor_name(
                    event.name, row.actor_id, event.job
                ):
                    row.name = event.name
        elif isinstance(event, PlayerInspectEvent):
            row = segment(event.target_id)
            if row is None:
                continue
            row.job = event.job or row.job
            if not _is_generic_replay_actor_name(
                event.target_name, row.actor_id, event.job
            ):
                row.name = event.target_name
            fingerprint = (
                tuple(event.equipment),
                tuple(event.cards),
                tuple(event.stats),
            )
            if any(fingerprint):
                row.profile_fingerprints.add(fingerprint)
        elif isinstance(event, PlayerHealthEvent):
            row = segment(event.target_id)
            if row is None:
                continue
            row.job = event.target_job or row.job
            if not _is_generic_replay_actor_name(
                event.target_name, row.actor_id, event.target_job
            ):
                row.name = event.target_name
            # A reconnect issues another 21 0C resource sync.  The current
            # HP/MP pair survives the runtime-actor replacement even when the
            # packet's reported maximum is temporarily equal to current HP.
            # Requiring both non-zero values keeps delta-only placeholders out
            # of this strong identity fingerprint.
            if (
                event.packet_kind
                == "s2c-udp-player-resource-sync-210c-v576"
                and event.current_hp > 0
                and event.current_mp > 0
            ):
                row.resource_fingerprints.add(
                    (int(event.current_hp), int(event.current_mp))
                )

    candidate_rows: list[_ReplayPlayerSegment] = []
    for row in segments.values():
        has_exact_name = bool(
            row.name
            and not _is_generic_replay_actor_name(row.name, row.actor_id, row.job)
        )
        if (
            row.job
            and row.first_cast_ms is not None
            and row.last_cast_ms is not None
            and (
                has_exact_name
                or bool(row.resource_fingerprints)
                or (
                    sum(row.cast_counts.values()) >= 12
                    and len(row.cast_counts) >= 6
                )
            )
        ):
            candidate_rows.append(row)

    aliases: dict[str, str] = {}
    candidate_rows.sort(key=lambda row: (row.first_cast_ms or 0, row.actor_id))
    for right_index, right in enumerate(candidate_rows):
        candidates: list[tuple[float, _ReplayPlayerSegment]] = []
        for left in candidate_rows[:right_index]:
            if not compatible_jobs(left.job, right.job):
                continue
            left_end = left.last_cast_ms or 0
            right_start = right.first_cast_ms or 0
            gap_ms = right_start - left_end
            if gap_ms < 250 or gap_ms > 30 * 60 * 1000:
                continue
            score, shared, set_overlap, distribution_overlap = (
                _replay_skill_signature_similarity(
                    left.cast_counts, right.cast_counts
                )
            )
            same_name = (
                left.name
                and right.name
                and left.name.casefold() == right.name.casefold()
            )
            same_profile = bool(
                left.profile_fingerprints.intersection(
                    right.profile_fingerprints
                )
            )
            same_resource = bool(
                left.resource_fingerprints.intersection(
                    right.resource_fingerprints
                )
            )
            # Character names are unique server-side. Once both runtime
            # segments carry the same non-generic name from an exact 03 03
            # identity packet, a reconnect can be joined even if the new
            # segment is too short to build the old 12-cast fingerprint.
            if same_name:
                candidates.append((max(score, 0.96), left))
                continue
            # This is the authoritative fallback for a disconnect that
            # happens before enough casts exist to resolve the second job.
            # The exact 21 0C HP/MP pair plus at least two shared skills is
            # materially stronger than the old long-segment fingerprint,
            # while still rejecting unrelated same-branch party members.
            if same_resource and shared >= 2:
                candidates.append((max(score, 0.99), left))
                continue
            if same_profile:
                score = max(score, 1.0)
            if (
                shared < 6
                or set_overlap < 0.70
                or distribution_overlap < 0.62
                or score < 0.86
            ):
                continue
            candidates.append((score, left))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score, best_left = candidates[0]
        if len(candidates) > 1 and best_score - candidates[1][0] < 0.06:
            continue
        canonical = aliases.get(best_left.actor_id, best_left.actor_id)
        aliases[right.actor_id] = canonical
    return aliases

def _merge_rejoined_replay_players_once(
    events: list[LiveDpsEvent],
    decoder: object | None = None,
) -> list[LiveDpsEvent]:
    aliases = (
        _replay_rejoined_player_aliases(events)
        if decoder is None
        else _replay_rejoined_player_aliases(
            events,
            getattr(decoder, "profile_lineages", None),
        )
    )
    if not aliases:
        return events

    canonical_context: dict[str, tuple[str, str]] = {}
    alias_actor_ids = set(aliases).union(aliases.values())
    for event in events:
        if isinstance(event, PartyMemberEvent):
            actor_id, name, job = event.actor_id, event.name, event.job
        elif isinstance(event, PlayerInspectEvent):
            actor_id, name, job = event.target_id, event.target_name, event.job
        elif isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
            actor_id, name, job = (
                event.target_id,
                event.target_name,
                event.target_job,
            )
        elif isinstance(event, SummonRemovalEvent):
            actor_id, name, job = (
                event.owner_id,
                event.owner_name,
                event.owner_job,
            )
        elif isinstance(
            event,
            (
                CombatEvent,
                SkillCastEvent,
                SkillParameterEvent,
                BossStatusEvent,
                BossActionEvent,
            ),
        ):
            actor_id, name, job = event.source_id, event.source_name, event.job
        else:
            continue
        canonical = aliases.get(actor_id, actor_id)
        old_name, old_job = canonical_context.get(canonical, ("", ""))
        if _is_generic_replay_actor_name(name, actor_id, job):
            name = old_name
        canonical_context[canonical] = (name or old_name, job or old_job)

    def context(actor_id: str, name: str, job: str) -> tuple[str, str, str]:
        if actor_id not in alias_actor_ids:
            return actor_id, name, job
        canonical = aliases.get(actor_id, actor_id)
        best_name, best_job = canonical_context.get(canonical, ("", ""))
        if _is_generic_replay_actor_name(name, actor_id, job):
            name = best_name or f"角色 {canonical}"
        return canonical, name, best_job or job

    result: list[LiveDpsEvent] = []
    for event in events:
        if isinstance(event, SettlementEvent):
            result.append(replace(event, actor_ids=tuple(
                aliases.get(actor_id, actor_id) for actor_id in event.actor_ids
            )))
        elif isinstance(event, PartyMemberEvent):
            actor_id, name, job = context(event.actor_id, event.name, event.job)
            result.append(replace(event, actor_id=actor_id, name=name, job=job))
        elif isinstance(event, PlayerInspectEvent):
            target_id, target_name, job = context(
                event.target_id, event.target_name, event.job
            )
            result.append(
                replace(
                    event,
                    target_id=target_id,
                    target_name=target_name,
                    job=job,
                )
            )
        elif isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
            target_id, target_name, target_job = context(
                event.target_id, event.target_name, event.target_job
            )
            result.append(
                replace(
                    event,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            )
        elif isinstance(event, SummonRemovalEvent):
            owner_id, owner_name, owner_job = context(
                event.owner_id, event.owner_name, event.owner_job
            )
            result.append(
                replace(
                    event,
                    owner_id=owner_id,
                    owner_name=owner_name,
                    owner_job=owner_job,
                )
            )
        elif isinstance(event, CombatEvent):
            source_id, source_name, job = context(
                event.source_id, event.source_name, event.job
            )
            target_id, target_name, target_job = context(
                event.target_id, event.target_name, event.target_job
            )
            result.append(
                replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            )
        elif isinstance(event, BossStatusEvent):
            source_id, source_name, job = context(
                event.source_id, event.source_name, event.job
            )
            target_id, target_name, target_job = context(
                event.target_id, event.target_name, event.target_job
            )
            result.append(
                replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            )
        elif isinstance(event, SkillCastEvent):
            source_id, source_name, job = context(
                event.source_id, event.source_name, event.job
            )
            result.append(
                replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                )
            )
        elif isinstance(event, SkillParameterEvent):
            source_id, source_name, job = context(
                event.source_id, event.source_name, event.job
            )
            target_id, _target_name, _target_job = context(
                event.target_id, "", ""
            )
            result.append(
                replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                    target_id=target_id,
                )
            )
        elif isinstance(event, BossActionEvent):
            source_id, source_name, job = context(
                event.source_id, event.source_name, event.job
            )
            # Boss actions can carry the party members hit by a mechanic.
            # Reconnect normalization must rewrite that nested roster too;
            # otherwise a merged fifth row could survive in death/mechanic
            # reports even though damage and HP ledgers already use one ID.
            affected_target_ids = tuple(
                dict.fromkeys(
                    context(actor_id, "", "")[0]
                    for actor_id in event.affected_target_ids
                )
            )
            result.append(
                replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                    affected_target_ids=affected_target_ids,
                )
            )
        else:
            result.append(event)
    return result

def _merge_rejoined_replay_players(
    events: list[LiveDpsEvent],
    decoder: object | None = None,
) -> list[LiveDpsEvent]:
    """Collapse every proven runtime-ID rejoin chain to one player.

    One reconnect can expose more than one replacement runtime ID through
    different packet streams.  Merging the strongest pair often gives the
    remaining segment enough canonical cast/profile context to prove the next
    link.  A single pass therefore leaves later HP and death records detached
    from the visible player.  Iterate to a fixed point; every successful pass
    removes at least one runtime ID, so the small party-size bound is only a
    defensive guard against malformed input.
    """

    merged = events
    for _pass in range(16):
        updated = _merge_rejoined_replay_players_once(merged, decoder)
        if updated is merged:
            break
        merged = updated
    return merged

def _backfill_permanent_skill_object_history(
    events: list[LiveDpsEvent], decoder: object
) -> list[LiveDpsEvent]:
    """Compatibility hook: never infer old ownership from a reused low slot.

    A later entity with the same low nine bits proves neither the earlier
    entity's owner nor its skill. Preserve those existing hits verbatim. Only
    an event-time birth/owner proof may resolve a companion in the decoder.
    """
    return events


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'_backfill_permanent_skill_object_history', '_canonical_live_skill_id', '_is_generic_replay_actor_name', '_supplement_boss_statuses_from_casts', '_live_target_identity', '_canonicalize_live_message', '_replay_skill_signature_similarity', '_merge_rejoined_replay_players', '_canonicalize_replay_actor_context', '_backfill_preexisting_maintained_player_buffs', '_replay_rejoined_player_aliases', 'bind_host_globals', '_ReplayPlayerSegment', '_supplement_player_buffs_from_casts', '_merge_rejoined_replay_players_once'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
