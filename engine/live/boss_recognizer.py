from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Iterable

from .boss_hp import (
    load_fixed_boss_hp_profiles,
    normalize_boss_hp_name,
    resolve_fixed_boss_hp_profile,
)
from .critical_stats import is_critical_rate_sample
from .event_platform import event_platform_for
from .models import (
    BossEffectSnapshot,
    CombatEvent,
    CombatSnapshot,
    DamageTargetSnapshot,
    StatusEventSnapshot,
)


_ENCOUNTER_REUSE_GAP_MS = 120_000
_ANONYMOUS_NAME_RE = re.compile(r"^(?:角色\s*)?0x[0-9a-f]+$", re.IGNORECASE)

# These Black Dragon weapon entities are complete encounter phases even though
# the monster table marks them as non-Boss mechanics.  The ordered sequence is
# also the fallback identity for hard-mode captures started after their spawn
# packets, where the runtime actors are only visible as 0x2E through 0x31.
_BLACK_DRAGON_WEAPON_TEMPLATE_SEQUENCE = (4180002, 4180003, 4180004, 4180005)
_BLACK_DRAGON_WEAPON_TEMPLATE_IDS = frozenset(
    _BLACK_DRAGON_WEAPON_TEMPLATE_SEQUENCE
)
_BLACK_DRAGON_PEDA_TEMPLATE_IDS = frozenset({4181001, 4181009})


@lru_cache(maxsize=1)
def _fixed_profile_boss_names() -> frozenset[str]:
    return frozenset(
        normalized
        for profile in load_fixed_boss_hp_profiles()
        for name, aliases, _values in profile.bosses
        for raw_name in (name, *aliases)
        if (normalized := normalize_boss_hp_name(raw_name))
    )


@dataclass(frozen=True, slots=True)
class BossEncounter:
    identity: tuple[str, str]
    name: str
    max_hp: int
    target_ids: frozenset[str]
    damage: int
    hits: int
    critical_hits: int
    first_ms: int
    last_ms: int
    damage_series: tuple[tuple[int, int], ...]
    phase_windows: tuple[tuple[float, float], ...]
    confidence: str = "inferred"
    critical_samples: int | None = None

    @property
    def active_seconds(self) -> float:
        return max(0.001, (self.last_ms - self.first_ms) / 1000.0)


@dataclass(slots=True)
class _ActorEvidence:
    target_id: str
    names: Counter[str] = field(default_factory=Counter)
    jobs: Counter[str] = field(default_factory=Counter)
    hp_values: Counter[int] = field(default_factory=Counter)
    damage: int = 0
    hits: int = 0
    critical_hits: int = 0
    critical_samples: int = 0
    first_ms: int = 0
    last_ms: int = 0
    damage_series: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    has_combat: bool = False
    status_only: bool = False
    template_id: int = 0
    template_boss: bool | None = None
    catalog_name: str = ""
    catalog_hp: int = 0
    implausible_template_identity: bool = False

    def touch(self, timestamp_ms: int) -> None:
        timestamp_ms = max(0, int(timestamp_ms))
        if self.first_ms <= 0:
            self.first_ms = timestamp_ms
        elif timestamp_ms > 0:
            self.first_ms = min(self.first_ms, timestamp_ms)
        self.last_ms = max(self.last_ms, timestamp_ms)

    def add_context(self, name: str, job: str, max_hp: int) -> None:
        cleaned = clean_target_name(name, self.target_id)
        if cleaned:
            self.names[cleaned] += 1
        job = str(job or "").strip()
        if job:
            self.jobs[job] += 1
        max_hp = max(0, int(max_hp or 0))
        if max_hp:
            self.hp_values[max_hp] += 1

    @property
    def name(self) -> str:
        if self.names:
            return self.names.most_common(1)[0][0]
        return self.catalog_name

    @property
    def max_hp(self) -> int:
        if self.hp_values:
            return int(self.hp_values.most_common(1)[0][0])
        return self.catalog_hp

    @property
    def confirmed_boss(self) -> bool:
        if any(job.partition(" ")[0].casefold() == "boss" for job in self.jobs):
            return True
        return self.template_boss is True


@dataclass(slots=True)
class _EncounterBucket:
    base_identity: tuple[str, str]
    actors: list[_ActorEvidence]
    episode: int = 1

    @property
    def name(self) -> str:
        names = Counter(actor.name for actor in self.actors if actor.name)
        return names.most_common(1)[0][0] if names else ""

    @property
    def max_hp(self) -> int:
        values = Counter(actor.max_hp for actor in self.actors if actor.max_hp > 0)
        return int(values.most_common(1)[0][0]) if values else 0

    @property
    def damage(self) -> int:
        return sum(actor.damage for actor in self.actors)

    @property
    def hits(self) -> int:
        return sum(actor.hits for actor in self.actors)

    @property
    def critical_hits(self) -> int:
        return sum(actor.critical_hits for actor in self.actors)

    @property
    def critical_samples(self) -> int:
        return sum(actor.critical_samples for actor in self.actors)

    @property
    def first_ms(self) -> int:
        return min((actor.first_ms for actor in self.actors if actor.first_ms > 0), default=0)

    @property
    def last_ms(self) -> int:
        return max((actor.last_ms for actor in self.actors), default=self.first_ms)

    @property
    def confirmed_boss(self) -> bool:
        return any(actor.confirmed_boss for actor in self.actors)

    @property
    def explicitly_non_boss(self) -> bool:
        known = [actor.template_boss for actor in self.actors if actor.template_boss is not None]
        return bool(known) and not any(known)

    @property
    def template_ids(self) -> frozenset[int]:
        return frozenset(actor.template_id for actor in self.actors if actor.template_id > 0)

    @property
    def protected_encounter_phase(self) -> bool:
        return bool(self.template_ids & _BLACK_DRAGON_WEAPON_TEMPLATE_IDS)

    @property
    def fixed_profile_boss(self) -> bool:
        return normalize_boss_hp_name(self.name) in _fixed_profile_boss_names()

    @property
    def implausible_template_identity(self) -> bool:
        return any(actor.implausible_template_identity for actor in self.actors)


def clean_target_name(name: object, target_id: str = "") -> str:
    text = str(name or "").strip()
    if not text or text == str(target_id or "").strip():
        return ""
    compact = re.sub(r"\s+", "", text)
    if _ANONYMOUS_NAME_RE.fullmatch(compact):
        return ""
    if compact.startswith("怪物模板") and compact[4:].isdigit():
        return ""
    return text


def template_id_from_target_id(target_id: object) -> int:
    text = str(target_id or "").strip()
    if "@" not in text:
        return 0
    value = text.rsplit("@", 1)[1]
    return int(value) if value.isdigit() else 0


@lru_cache(maxsize=1)
def monster_template_catalog() -> dict[int, dict[str, object]]:
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "monster_templates.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("templates", {}) if isinstance(payload, dict) else {}
    return {
        int(key): value
        for key, value in rows.items()
        if str(key).isdigit() and isinstance(value, dict)
    }


@lru_cache(maxsize=8192)
def target_identity_key(
    target_id: str,
    target_name: str,
    target_max_hp: int = 0,
) -> tuple[str, str]:
    """Canonical species identity; encounter splitting is handled separately."""

    template_id = template_id_from_target_id(target_id)
    from .nightmare_sea_dragon import TEMPLATE_IDS, NAME
    if template_id in TEMPLATE_IDS:
        return ("name", NAME.casefold())
    template = monster_template_catalog().get(template_id, {})
    explicit_name = clean_target_name(target_name, target_id)
    name = explicit_name or str(template.get("name", "")).strip()
    max_hp = max(0, int(target_max_hp or 0))
    if not explicit_name and max_hp <= 0:
        max_hp = max(0, int(template.get("hp", 0) or 0))
    if name:
        if max_hp:
            return ("name_hp", f"{name.casefold()}|{max_hp}")
        return ("name", name.casefold())
    if template_id:
        return ("template", str(template_id))
    return ("actor", str(target_id or "").strip())


def _merge_windows(
    windows: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    merged: list[list[float]] = []
    for start, end in sorted(windows):
        start = max(0.0, float(start))
        end = max(start, float(end))
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _clip_disjoint_encounter_padding(
    encounters: list[BossEncounter],
) -> tuple[BossEncounter, ...]:
    """Keep look-ahead padding without overlapping two sequential Bosses."""

    ordered = sorted(encounters, key=lambda row: (row.first_ms, -row.damage, row.name))
    windows = [list(row.phase_windows) for row in ordered]
    for index in range(len(ordered) - 1):
        current = ordered[index]
        following = ordered[index + 1]
        if current.last_ms >= following.first_ms:
            # Both targets genuinely received damage at the same time.
            continue
        if not windows[index] or not windows[index + 1]:
            continue
        if windows[index][-1][1] <= windows[index + 1][0][0]:
            continue
        boundary = (current.last_ms + following.first_ms) / 2000.0
        windows[index] = [
            (start, min(end, boundary))
            for start, end in windows[index]
            if start < boundary
        ]
        windows[index + 1] = [
            (max(start, boundary), end)
            for start, end in windows[index + 1]
            if end > boundary
        ]
    return tuple(
        replace(row, phase_windows=tuple(windows[index]))
        for index, row in enumerate(ordered)
    )


class BossRecognizer:
    """Recognize main Boss encounters from runtime actors and packet evidence."""

    def __init__(self, *, encounter_reuse_gap_ms: int = _ENCOUNTER_REUSE_GAP_MS):
        self.encounter_reuse_gap_ms = max(10_000, int(encounter_reuse_gap_ms))
        self.templates = monster_template_catalog()

    def _actor(self, rows: dict[str, _ActorEvidence], target_id: str) -> _ActorEvidence:
        actor = rows.setdefault(target_id, _ActorEvidence(target_id=target_id))
        if not actor.template_id:
            actor.template_id = template_id_from_target_id(target_id)
            template = self.templates.get(actor.template_id)
            if template:
                actor.template_boss = bool(template.get("boss"))
                actor.catalog_name = str(template.get("name", "")).strip()
                actor.catalog_hp = max(0, int(template.get("hp", 0) or 0))
        return actor

    def _collect_actors(
        self,
        target_rows: Iterable[DamageTargetSnapshot],
        combat_events: Iterable[CombatEvent],
        boss_effects: Iterable[BossEffectSnapshot | StatusEventSnapshot],
        source_ids: frozenset[str],
    ) -> dict[str, _ActorEvidence]:
        actors: dict[str, _ActorEvidence] = {}
        target_snapshots: dict[str, DamageTargetSnapshot] = {}
        for row in target_rows:
            if not row.target_id or row.target_id.startswith("__") or row.target_id in source_ids:
                continue
            target_snapshots[row.target_id] = row
            actor = self._actor(actors, row.target_id)
            actor.add_context(row.target_name, row.target_job, row.target_max_hp)
            if row.first_ms > 0:
                actor.touch(row.first_ms)
                actor.touch(row.last_ms)

        for event in combat_events:
            if (
                event.healing
                or event.amount <= 0
                or not event.target_id
                or event.target_id.startswith("__")
                or event.target_id in source_ids
            ):
                continue
            actor = self._actor(actors, event.target_id)
            actor.add_context(event.target_name, event.target_job, event.target_max_hp)
            actor.damage += int(event.amount)
            actor.hits += 1
            if is_critical_rate_sample(event):
                actor.critical_samples += 1
                actor.critical_hits += int(event.critical)
            actor.damage_series[event.timestamp_ms // 1000] += int(event.amount)
            actor.has_combat = True
            actor.touch(event.timestamp_ms)

        for effect in boss_effects:
            target_id = str(effect.target_id or "").strip()
            if not target_id or target_id.startswith("__") or target_id in source_ids:
                continue
            actor = self._actor(actors, target_id)
            actor.add_context(effect.target_name, effect.target_job, 0)
            timestamp_ms = int(
                round(
                    (
                        effect.start_seconds
                        if isinstance(effect, BossEffectSnapshot)
                        else effect.timestamp_seconds
                    )
                    * 1000
                )
            )
            # Runtime IDs can be reused and old status packets can survive in
            # the capture longer than the actor's damage phase. Combat bounds
            # are authoritative whenever this actor dealt or received damage.
            if not actor.has_combat:
                actor.touch(timestamp_ms)
                actor.status_only = True

        # A compact snapshot can omit combat events. Fall back to the already
        # aggregated target totals without double counting full snapshots.
        for target_id, row in target_snapshots.items():
            actor = actors[target_id]
            if actor.damage <= 0 and row.damage > 0:
                actor.damage = int(row.damage)
                actor.hits = int(row.hits)
                actor.has_combat = True
                if row.first_ms > 0:
                    actor.touch(row.first_ms)
                    actor.touch(row.last_ms)
        for actor in actors.values():
            # A tiny catalog object cannot absorb hundreds of millions of
            # damage. In captures missing a later birth packet, its short
            # runtime ID may have been reused by the next encounter. Do not
            # perpetuate the old name/template as authoritative evidence.
            if (
                actor.template_boss is False
                and 0 < actor.catalog_hp <= 1_000
                and actor.hits >= 20
                and actor.damage >= max(100_000_000, actor.catalog_hp * 10_000)
            ):
                actor.implausible_template_identity = True
                actor.template_id = 0
                actor.template_boss = None
                actor.catalog_name = ""
                actor.catalog_hp = 0
                actor.names.clear()
                actor.jobs.clear()
                actor.hp_values.clear()
        return actors

    @staticmethod
    def _reconcile_missing_hp(actors: dict[str, _ActorEvidence]) -> None:
        hp_by_name: dict[str, set[int]] = defaultdict(set)
        for actor in actors.values():
            if actor.name and actor.max_hp > 0:
                hp_by_name[actor.name.casefold()].add(actor.max_hp)
        for actor in actors.values():
            if actor.name and actor.max_hp <= 0:
                choices = hp_by_name.get(actor.name.casefold(), set())
                if len(choices) == 1:
                    actor.hp_values[next(iter(choices))] += 1

    def _episode_buckets(
        self, actors: dict[str, _ActorEvidence]
    ) -> list[_EncounterBucket]:
        self._reconcile_missing_hp(actors)
        by_identity: dict[tuple[str, str], list[_ActorEvidence]] = defaultdict(list)
        for actor in actors.values():
            if actor.damage <= 0 and not actor.status_only:
                continue
            by_identity[
                target_identity_key(
                    actor.target_id.split("@", 1)[0]
                    if actor.implausible_template_identity else actor.target_id,
                    actor.name,
                    actor.max_hp,
                )
            ].append(actor)

        buckets: list[_EncounterBucket] = []
        for identity, identity_actors in by_identity.items():
            ordered = sorted(identity_actors, key=lambda row: (row.first_ms, row.last_ms))
            episodes: list[list[_ActorEvidence]] = []
            for actor in ordered:
                if (
                    not episodes
                    or actor.first_ms - max(row.last_ms for row in episodes[-1])
                    > self.encounter_reuse_gap_ms
                ):
                    episodes.append([actor])
                else:
                    episodes[-1].append(actor)
            buckets.extend(
                _EncounterBucket(identity, episode, index + 1)
                for index, episode in enumerate(episodes)
            )
        return buckets

    @staticmethod
    def _is_subordinate(
        candidate: _EncounterBucket,
        host: _EncounterBucket,
    ) -> bool:
        # Catalog-confirmed Bosses can overlap their host in time. The Apostle
        # shadows during Peda are selectable phases, not summons to discard.
        if candidate.confirmed_boss or candidate.protected_encounter_phase:
            return False
        if candidate is host or host.damage <= candidate.damage:
            return False
        if candidate.first_ms < host.first_ms - 2_000 or candidate.last_ms > host.last_ms + 2_000:
            return False
        if candidate.max_hp > 0 and host.max_hp > 0:
            if host.max_hp < candidate.max_hp * 2.5:
                return False
        elif host.damage < candidate.damage * 2.5:
            return False
        return host.damage >= candidate.damage * 1.2

    @staticmethod
    def _is_subordinate_to_host_union(
        candidate: _EncounterBucket,
        hosts: list[_EncounterBucket],
    ) -> bool:
        if candidate.confirmed_boss or candidate.protected_encounter_phase:
            return False
        if not candidate.explicitly_non_boss or candidate.last_ms <= candidate.first_ms:
            return False
        stronger = [
            host
            for host in hosts
            if host is not candidate and host.damage >= candidate.damage * 2.5
        ]
        if not stronger:
            return False
        intersections = [
            (
                max(candidate.first_ms, host.first_ms),
                min(candidate.last_ms, host.last_ms),
            )
            for host in stronger
            if host.first_ms <= candidate.last_ms and host.last_ms >= candidate.first_ms
        ]
        if not intersections:
            return False
        merged: list[list[int]] = []
        for start, end in sorted(intersections):
            if end <= start:
                continue
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        covered = sum(end - start for start, end in merged)
        duration = candidate.last_ms - candidate.first_ms
        return covered / max(1, duration) >= 0.60

    def _main_buckets(self, buckets: list[_EncounterBucket]) -> list[_EncounterBucket]:
        damage_buckets = [row for row in buckets if row.damage > 0]
        if not damage_buckets:
            return []
        max_damage = max(row.damage for row in damage_buckets)
        max_hp = max((row.max_hp for row in damage_buckets), default=0)
        confirmed = [row for row in damage_buckets if row.confirmed_boss]
        first_confirmed_ms = min(
            (row.first_ms for row in confirmed if row.first_ms > 0), default=0
        )
        candidates: list[_EncounterBucket] = []
        for row in damage_buckets:
            damage_ratio = row.damage / max(1, max_damage)
            hp_ratio = row.max_hp / max(1, max_hp) if row.max_hp > 0 and max_hp > 0 else 0.0
            inferred_main = (row.hits >= 3 or not confirmed) and (
                damage_ratio >= 0.08
                or (damage_ratio >= 0.035 and hp_ratio >= 0.02)
            )
            if row.confirmed_boss or row.fixed_profile_boss:
                candidates.append(row)
                continue
            if row.protected_encounter_phase and row.hits >= 3:
                candidates.append(row)
                continue
            if row.explicitly_non_boss and confirmed:
                # Damage volume alone is not Boss evidence. Large trash packs
                # and destructible mechanics must not become extra phases when
                # the same recording has catalog-confirmed Boss anchors.
                continue
            preceding_anonymous = bool(
                confirmed
                and not row.name
                and row.hits >= 3
                and damage_ratio >= 0.035
                and first_confirmed_ms > 0
                and row.last_ms + 5_000 < first_confirmed_ms
            )
            if not inferred_main and not preceding_anonymous:
                continue
            if confirmed:
                # Do not promote a high-damage add merely because it outranks a
                # one-hit confirmed Boss. Inferred encounters need their own
                # non-overlapping time range when explicit Boss evidence exists.
                if row.first_ms <= 0 or row.last_ms <= 0:
                    continue
                overlaps_confirmed = any(
                    row.first_ms <= host.last_ms + 2_000
                    and row.last_ms >= host.first_ms - 2_000
                    for host in confirmed
                )
                if overlaps_confirmed:
                    continue
            candidates.append(row)

        selected = [
            row
            for row in candidates
            if not any(self._is_subordinate(row, host) for host in candidates)
        ]
        selected = [
            row
            for row in selected
            if not self._is_subordinate_to_host_union(row, selected)
        ]
        return sorted(selected, key=lambda row: (row.first_ms, -row.damage, row.name))

    def _black_dragon_pre_peda_identities(
        self,
        buckets: list[_EncounterBucket],
    ) -> dict[int, tuple[str, int]]:
        """Recover the four hard-mode weapon names when spawn packets are absent."""

        peda_rows = [
            row
            for row in buckets
            if row.template_ids & _BLACK_DRAGON_PEDA_TEMPLATE_IDS
        ]
        if not peda_rows:
            return {}
        first_peda = min(peda_rows, key=lambda row: row.first_ms)
        anonymous = sorted(
            (
                row
                for row in buckets
                if not row.name
                and row.damage > 0
                and row.hits >= 3
                and row.last_ms + 5_000 < first_peda.first_ms
            ),
            key=lambda row: row.first_ms,
        )
        # A missing mechanic birth (e.g. the curse shield) can otherwise make
        # five anonymous rows and defeat the whole four-weapon recovery. Only
        # accept a unique disjoint phase chain. Extra rows must be demonstrably
        # weaker mechanics spanning that chain, never an unexplained fifth Boss.
        if not 4 <= len(anonymous) <= 12:
            return {}
        chains = []
        for chain in combinations(anonymous, 4):
            if any(left.last_ms + 2_000 >= right.first_ms
                   for left, right in zip(chain, chain[1:])):
                continue
            if min(row.damage for row in chain) < max(row.damage for row in chain) * 0.25:
                continue
            extras = [row for row in anonymous if all(row is not host for host in chain)]
            if all(self._is_cross_phase_mechanic(row, chain) for row in extras):
                chains.append((chain, extras))
                if len(chains) > 1:
                    return {}
        if len(chains) != 1:
            return {}
        chain, extras = chains[0]
        _, references = resolve_fixed_boss_hp_profile(
            (row.name for row in buckets),
            target_ids=(actor.target_id for row in buckets for actor in row.actors),
        )

        identities: dict[int, tuple[str, int]] = {}
        for row, template_id in zip(
            chain, _BLACK_DRAGON_WEAPON_TEMPLATE_SEQUENCE
        ):
            template = self.templates.get(template_id, {})
            name = str(template.get("name", "")).strip()
            if name:
                identities[id(row)] = (
                    name,
                    references.get(normalize_boss_hp_name(name),
                                   (max(0, int(template.get("hp", 0) or 0)),))[0],
                )
        if len(identities) == 4:
            extra_ids = {id(row) for row in extras}
            buckets[:] = [row for row in buckets if id(row) not in extra_ids]
        return identities

    @staticmethod
    def _is_cross_phase_mechanic(
        candidate: _EncounterBucket, hosts: tuple[_EncounterBucket, ...],
    ) -> bool:
        if candidate.name or candidate.confirmed_boss or candidate.last_ms <= candidate.first_ms:
            return False
        intersections = sorted(
            (max(candidate.first_ms, host.first_ms), min(candidate.last_ms, host.last_ms))
            for host in hosts
            if host.damage >= candidate.damage * 2
            and host.first_ms < candidate.last_ms and host.last_ms > candidate.first_ms
        )
        if len(intersections) < 2:
            return False
        merged: list[list[int]] = []
        for start, end in intersections:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        covered = sum(end - start for start, end in merged)
        return covered >= (candidate.last_ms - candidate.first_ms) * 0.60

    @staticmethod
    def _profile_missing_opening_identity(
        buckets: list[_EncounterBucket],
    ) -> dict[int, tuple[str, int]]:
        """Recover one missing opening from a uniquely evidenced raid roster.

        Roster order is NOT necessarily combat order: raids may revisit the
        dragon between simultaneous sub-Bosses. Never pick the numeric nearest
        monster template or its shared base HP to label an anonymous actor.
        """
        label, _references = resolve_fixed_boss_hp_profile(
            (row.name for row in buckets),
            target_ids=(actor.target_id for row in buckets for actor in row.actors),
        )
        profiles = [profile for profile in load_fixed_boss_hp_profiles() if profile.label == label]
        if len(profiles) != 1:
            return {}
        profile = profiles[0]
        names = {
            normalize_boss_hp_name(row.name) for row in buckets if row.name
        }
        observed_templates = {template for row in buckets for template in row.template_ids}
        if len(observed_templates & profile.template_ids) < 2:
            return {}
        matched = [
            index for index, (name, aliases, _hp) in enumerate(profile.bosses)
            if names & {normalize_boss_hp_name(value) for value in (name, *aliases)}
        ]
        missing = [index for index in range(len(profile.bosses)) if index not in matched]
        if len(matched) < 2 or missing != [0]:
            return {}
        known = [row for row in buckets if row.name and row.first_ms > 0]
        if not known:
            return {}
        first_known_ms = min(row.first_ms for row in known)
        preceding = [row for row in buckets if not row.name and row.damage > 0
                     and row.hits >= 20 and row.first_ms > 0
                     and row.last_ms + 5_000 < first_known_ms]
        if len(preceding) != 1:
            return {}
        row = preceding[0]
        name, _aliases, values = profile.bosses[0]
        # A guard against a trivial add or wildly incompatible object, not a
        # substitute for the independently selected profile and unique slot.
        if not min(values) * 0.25 <= row.damage <= max(values) * 1.25:
            return {}
        return {id(row): (name, values[0] if len(values) == 1 else 0)}

    @staticmethod
    def _profile_phase_identities(
        buckets: list[_EncounterBucket],
    ) -> dict[int, tuple[str, int]]:
        """Fill uniquely bracketed missing births from an ordered HP profile.

        This is encounter-order evidence, not a guessed runtime-ID mapping.
        A profile needs at least two named anchors in the correct order; every
        missing slot needs exactly one dominant anonymous encounter between
        those anchors. Ambiguous captures remain unidentified.
        """

        ordered = sorted(buckets, key=lambda row: (row.first_ms, -row.damage))
        choices: list[tuple[int, object, list[tuple[int, _EncounterBucket]]]] = []
        for profile in load_fixed_boss_hp_profiles():
            name_to_index = {
                normalize_boss_hp_name(value): index
                for index, (name, aliases, _hp) in enumerate(profile.bosses)
                for value in (name, *aliases)
            }
            anchors = [
                (name_to_index[normalize_boss_hp_name(row.name)], row)
                for row in ordered
                if normalize_boss_hp_name(row.name) in name_to_index
            ]
            if len(anchors) < 2 or any(
                left[0] >= right[0]
                for left, right in zip(anchors, anchors[1:])
            ):
                continue
            choices.append((len(anchors), profile, anchors))
        if not choices:
            return {}
        choices.sort(key=lambda row: row[0], reverse=True)
        if len(choices) > 1 and choices[0][0] == choices[1][0]:
            return {}
        _count, profile, anchors = choices[0]
        identities: dict[int, tuple[str, int]] = {}
        for (left_index, left), (right_index, right) in zip(anchors, anchors[1:]):
            missing = right_index - left_index - 1
            if missing <= 0:
                continue
            anonymous = [
                row for row in ordered
                if not row.name
                and row.hits >= 10
                and row.damage >= min(left.damage, right.damage) * 0.10
                and left.last_ms + 5_000 < row.first_ms
                and row.last_ms + 5_000 < right.first_ms
            ]
            if len(anonymous) != missing:
                continue
            for row, index in zip(anonymous, range(left_index + 1, right_index)):
                name, _aliases, values = profile.bosses[index]
                identities[id(row)] = (name, values[0] if values else 0)

        # One final Boss may change runtime ID and lose its next birth packet.
        # The decoder either strips an impossible stale tiny-template identity
        # or emits its unknown runtime actor. Keep the merge narrow: one
        # dominant anonymous tail, adjacent to the final profile phase.
        final_index, final_anchor = anchors[-1]
        if final_index == len(profile.bosses) - 1:
            tail = [
                row for row in ordered
                if row is not final_anchor
                and (
                    row.implausible_template_identity
                    or all(actor.template_id == 0 for actor in row.actors)
                )
                and not row.name
                and 0 <= row.first_ms - final_anchor.last_ms <= 90_000
                and row.hits >= 20
                and row.damage >= final_anchor.damage * 0.10
            ]
            if len(tail) == 1 and not any(
                row.name and row.first_ms > final_anchor.last_ms
                for row in ordered if row is not final_anchor
            ):
                final_anchor.actors.extend(tail[0].actors)
                buckets.remove(tail[0])
        return identities

    def recognize(
        self,
        *,
        target_rows: Iterable[DamageTargetSnapshot],
        combat_events: Iterable[CombatEvent] = (),
        boss_effects: Iterable[BossEffectSnapshot | StatusEventSnapshot] = (),
        source_ids: Iterable[str] = (),
    ) -> tuple[BossEncounter, ...]:
        actors = self._collect_actors(
            target_rows,
            combat_events,
            boss_effects,
            frozenset(str(value) for value in source_ids if str(value)),
        )
        buckets = self._main_buckets(self._episode_buckets(actors))
        inferred_preceding = self._black_dragon_pre_peda_identities(buckets)
        inferred_preceding.update(self._profile_missing_opening_identity(buckets))
        profile_identities = self._profile_phase_identities(buckets)
        encounters: list[BossEncounter] = []
        anonymous_index = 0
        identity_counts = Counter(row.base_identity for row in buckets)
        for row in buckets:
            inferred_name, inferred_hp = inferred_preceding.get(id(row), ("", 0))
            if not inferred_name:
                inferred_name, inferred_hp = profile_identities.get(id(row), ("", 0))
            name = row.name or inferred_name
            if not name:
                anonymous_index += 1
                name = f"未识别 Boss（第{anonymous_index}关）"
            identity = row.base_identity
            if identity_counts[identity] > 1:
                identity = (identity[0], f"{identity[1]}#encounter:{row.episode}")
            series: dict[int, int] = defaultdict(int)
            for actor in row.actors:
                for second, damage in actor.damage_series.items():
                    series[second] += damage
            windows = _merge_windows(
                (
                    max(0.0, (actor.first_ms - 10_000) / 1000.0),
                    (actor.last_ms + 2_000) / 1000.0,
                )
                for actor in row.actors
                if actor.first_ms > 0
            )
            encounters.append(
                BossEncounter(
                    identity=identity,
                    name=name,
                    max_hp=row.max_hp or inferred_hp,
                    target_ids=frozenset(actor.target_id for actor in row.actors),
                    damage=row.damage,
                    hits=row.hits,
                    critical_hits=row.critical_hits,
                    first_ms=row.first_ms,
                    last_ms=row.last_ms,
                    damage_series=tuple(sorted(series.items())),
                    phase_windows=windows,
                    confidence=(
                        "confirmed"
                        if row.confirmed_boss
                        else "fixed_profile"
                        if row.fixed_profile_boss
                        else "inferred"
                    ),
                    critical_samples=row.critical_samples,
                )
            )
        # A catalog/template HP is only a fallback.  Reused runtime IDs may
        # carry a tiny object's HP into a later boss phase, and the nest-70
        # monster table can differ from the difficulty-specific Boss table.
        # Correct only unmistakably implausible values, and only after the
        # entire encounter set selects one unambiguous fixed profile.
        _profile_label, hp_references = resolve_fixed_boss_hp_profile(
            (encounter.name for encounter in encounters),
            target_ids=(
                target_id
                for encounter in encounters
                for target_id in encounter.target_ids
            ),
        )
        if hp_references:
            corrected: list[BossEncounter] = []
            for encounter in encounters:
                reference = hp_references.get(normalize_boss_hp_name(encounter.name), ())
                if len(reference) == 1 and (
                    encounter.max_hp <= 0
                    or encounter.max_hp < reference[0] // 8
                    or encounter.max_hp < encounter.damage // 4
                ):
                    encounter = replace(encounter, max_hp=reference[0])
                corrected.append(encounter)
            encounters = corrected
        return _clip_disjoint_encounter_padding(encounters)

    def recognize_snapshot(self, snapshot: CombatSnapshot) -> tuple[BossEncounter, ...]:
        platform = event_platform_for(snapshot)
        return self.recognize(
            target_rows=platform.damage_targets,
            combat_events=platform.combat_events,
            boss_effects=(*platform.boss_effects, *platform.boss_effect_events),
            source_ids=(player.source_id for player in platform.players),
        )

    def primary_target_ids(
        self, target_rows: Iterable[DamageTargetSnapshot]
    ) -> frozenset[str]:
        rows = tuple(target_rows)
        selected_ids = {
            target_id
            for encounter in self.recognize(target_rows=rows)
            for target_id in encounter.target_ids
        }
        if not selected_ids:
            return frozenset()

        # Some protocol variants use one runtime entity for damage and a
        # second entity for the Boss status stream.  Attach zero-damage status
        # entities when their canonical species identity matches an already
        # selected encounter, otherwise the Boss is visible but all of its
        # debuffs disappear from the status view.
        selected_identities = {
            target_identity_key(row.target_id, row.target_name, row.target_max_hp)
            for row in rows
            if row.target_id in selected_ids
        }
        selected_ids.update(
            row.target_id
            for row in rows
            if row.target_id
            and not row.target_id.startswith("__")
            and target_identity_key(
                row.target_id, row.target_name, row.target_max_hp
            )
            in selected_identities
        )
        return frozenset(selected_ids)
