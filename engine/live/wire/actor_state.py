from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from ..damage_attribution import is_deterministic_attribution
from ..entity_owner_evidence import (
    SINGLE_FAMILY_ENTITY_SKILLS,
    entity_owner_evidence_id,
)
from ..summon_attack_runtime import resolve_existing_summon_hit, repair_proven_entity_target
from ..entity_damage_owner import (
    EntityOwnerHitContext, annotate_existing_entity_damage, resolve_current_entity_owner,
)
from ..autonomous_attack_families import (
    AUTONOMOUS_ATTACK_FAMILIES, annotate_autonomous_attack,
    annotate_exact_current_attack, confirmed_pve_target,
    confirmed_registered_monster_from_player_hit,
)
from ..native_damage_skill_keys import (
    annotate_native_entity_attack,
    clear_foreign_internal_attack_bucket,
)
from ..entity_damage_families import annotate_entity_damage_family

# Entity-kind evidence only, not a template-to-one-attack mapping. Magic Grass
# has separate fire/poison attacks (30810/30812), so its absence from the
# single-skill object map must not classify it as an ordinary hostile monster.
_REVIEWED_OWNER_ONLY_SUMMON_TEMPLATES = frozenset({32002, 32003, 32012, 32015, 32017, 32018})


def _clear_stale_entity_skill_after_owner_correction(
    event,
    owner_actor_id: int,
    job_conflicts,
):
    """Drop a heuristic skill label that contradicts the proven owner job.

    Entity IDs are recycled heavily in long raids.  The exact birth/owner
    ledger can therefore correct the player who owns an already emitted Hit
    after an older recent-summon guess supplied its skill name.  Keeping that
    old name produces impossible combinations such as a Heavy Gunner Hit named
    after an Alchemist or Saint summon.  The owner proof does not itself prove
    the entity's current child attack, so fail closed and let the native
    self-skill/projectile selectors below rename it when they have evidence.

    Fixed packet/object/status identities are deliberately untouched here;
    contradictory deterministic evidence must be handled by its own proof
    validator rather than silently erased by this hygiene guard.
    """
    if is_deterministic_attribution(getattr(event, "attribution_method", "")):
        return event
    try:
        skill_id = int(getattr(event, "skill_id", 0) or 0)
    except (TypeError, ValueError):
        return event
    if skill_id <= 0 or not job_conflicts(owner_actor_id, skill_id):
        return event
    return replace(
        event,
        skill_id="-1",
        skill_name="待识别伤害",
        # This marker is intentionally locked against later timing/profile
        # backfill.  All deterministic entity/object readers have already run
        # before this guard; allowing the cast ledger to rename it again would
        # merely replace one disproven label with another nearby guess.
        attribution_method="entity_owner_skill_conflict",
        cast_instance_id="",
        attribution_confidence=0.0,
    )

class V576ActorStateMixin:
    def _current_entity_owner_id(self, source_actor_id: int, timestamp: float):
        ledger = self.entity_owner_evidence
        birth = ledger.births.get(source_actor_id)
        if birth is None or self._legacy_entity_owner_blocked(source_actor_id, timestamp):
            return None
        binding = resolve_current_entity_owner(
            ledger, source_actor_id, birth[0], timestamp,
            order=(self._damage_object_packet_serial, 0, 18), generation=birth[1],
            entity_templates=self.monster_entities,
        )
        return binding.record.owner_id if binding is not None else None

    def _finalize_wire_damage_owner(self, event, packet):
        event = clear_foreign_internal_attack_bucket(
            event,
            packet.payload,
            packet.inferred_direction(),
        )
        entity = event.wire_source_actor_id
        if not 0 < entity < 65536 or event.healing or event.source_id.startswith("__"):
            return event
        ledger = self.entity_owner_evidence
        birth = ledger.births.get(entity)
        if birth is None or self._legacy_entity_owner_blocked(entity, packet.timestamp_seconds):
            return event
        binding = ledger.resolve(entity, birth[0], packet.timestamp_seconds)
        if binding is None:
            return event
        if ledger.observe_hit(entity, birth[0], packet.timestamp_seconds) is None:
            return event
        owner = binding.record.owner_id
        players = self.skill_actor_ids | self.local_actor_ids | set(self.known_actor_names)
        # A direct player-origin hit is independent target evidence.  Generic
        # hit history is deliberately excluded because an entity hit must not
        # certify its own otherwise unknown target.
        targets = (
            set(self.monster_entities)
            | set(self.direct_player_confirmed_damage_target_ids)
        )
        context = EntityOwnerHitContext(
                packet.payload, packet.inferred_direction(), packet.timestamp_seconds,
                (self._damage_object_packet_serial, 0, event.wire_record_offset), birth[1],
            )
        decision = annotate_existing_entity_damage(
            event, context, admitted_outgoing=True, ledger=ledger, entity_templates=self.monster_entities,
            summon_template_ids=(set(_SKILL_OBJECT_TEMPLATE_SKILL_IDS)
                                 | set(SINGLE_FAMILY_ENTITY_SKILLS) | _REVIEWED_OWNER_ONLY_SUMMON_TEMPLATES),
            confirmed_nonplayer_target_ids=targets, known_player_actor_ids=players,
            owner_display_context={owner: self._actor_display_context(owner)},
        )
        if decision.evidence is None:
            return decision.event
        pve = confirmed_pve_target(self,event.wire_target_actor_id,packet)
        # A capture may miss the active scene row while still retaining both
        # the server monster registration and an independently decoded direct
        # player Hit against that same target.  The exact entity self-attack
        # route below already requires this Hit's current birth/owner
        # generation and its own positive native attack slot, so the narrow
        # registered-monster proof is sufficient here just as it is for the
        # fully-bound compact selector route.  Empty/FFFFFFFF slots still fail
        # closed inside annotate_exact_current_attack.
        exact_attack_pve = pve or confirmed_registered_monster_from_player_hit(
            self, event.wire_target_actor_id
        )
        updated = annotate_exact_current_attack(decision.event,context,decision.evidence,
            pve_confirmed=exact_attack_pve,skill_catalog=self.skill_catalog,
            job_conflicts=self._action_conflicts_with_confirmed_job)
        # Audited one-family autonomous entities have a stricter self-slot
        # contract below.  Do not let the generic native-key fallback bypass a
        # contradictory/missing primary slot and relabel the hit from a nearby
        # fallback field.
        if birth[0] not in AUTONOMOUS_ATTACK_FAMILIES:
            # Template 30401 (奇迹之手) has an independently audited exact
            # self-attack slot: native key 30615 -> player skill 3412.  Some
            # captures omit the scene row that makes ``pve`` true even though
            # the same server-registered target has already received a real
            # direct player Hit.  Broaden only this closed template/key family;
            # arbitrary native fallback keys retain the stricter scene gate.
            # Evidence: outputs/saint_relic_cross_skill_v73_20260910.
            native_pve = pve or (
                birth[0] in {30401, 33601, 33602, 33603}
                and confirmed_registered_monster_from_player_hit(
                    self, event.wire_target_actor_id
                )
            )
            updated = annotate_native_entity_attack(
                updated, context, decision.evidence,
                pve_confirmed=native_pve,
                skill_catalog=self.skill_catalog,
                damage_skill_ids=self.damage_skill_ids,
                live_aliases=_LIVE_DAMAGE_SKILL_ALIASES,
                job_conflicts=self._action_conflicts_with_confirmed_job,
            )
        # The compact selector chain already requires the exact current
        # birth/owner generation and the same entity/object registration.  It
        # may therefore use a server-registered target that an identified
        # player has independently hit, even when the capture missed the
        # scene packet which would make ``confirmed_pve_target`` true.  Keep
        # the broader proof local to this fully bound selector route; native
        # fallback keys and ordinary entity attacks retain their stricter
        # scene gate.
        selector_pve = pve or confirmed_registered_monster_from_player_hit(
            self, event.wire_target_actor_id
        )
        updated = self.compact_entity_objects.annotate(updated, context, decision.evidence,
            pve_confirmed=selector_pve, skill_catalog=self.skill_catalog,
            job_conflicts=self._action_conflicts_with_confirmed_job)
        autonomous_pve = pve or confirmed_registered_monster_from_player_hit(
            self, event.wire_target_actor_id
        )
        spec = AUTONOMOUS_ATTACK_FAMILIES.get(birth[0])
        if spec is not None:
            updated = annotate_autonomous_attack(updated, context, decision.evidence,
                pve_confirmed=autonomous_pve,
                skill_name=_skill_display_name(self.skill_catalog, spec[0]),
                job_conflicts=self._action_conflicts_with_confirmed_job(owner, spec[0]))
        # Last deterministic entity layer: if the exact child attack was not
        # exposed, retain the exact parent summon family instead of borrowing a
        # nearby player cast.  This cannot override any exact attack/object/
        # status route and never creates a timeline cast.
        updated = annotate_entity_damage_family(
            updated,
            context,
            decision.evidence,
            pve_confirmed=autonomous_pve,
            skill_catalog=self.skill_catalog,
            job_conflicts=self._action_conflicts_with_confirmed_job,
        )
        return _clear_stale_entity_skill_after_owner_correction(
            updated,
            owner,
            self._action_conflicts_with_confirmed_job,
        )

    def _legacy_entity_owner_blocked(self, source_actor_id: int, timestamp: float | None = None) -> bool:
        """Negative lifetime/type evidence cannot fall back to a recent caster."""
        if source_actor_id is None or not 0 < source_actor_id < 65536:
            return False
        ledger = getattr(self, "entity_owner_evidence", None)
        negative_state = getattr(ledger, "negative_state", None)
        if callable(negative_state):
            reason = negative_state(source_actor_id, timestamp=timestamp)
            if reason not in (None, "missing_birth", "missing_owner"):
                return True
        template = getattr(self, "monster_entities", {}).get(source_actor_id)
        return bool(template is not None and (
            template.boss or (
                template.template_id not in _SKILL_OBJECT_TEMPLATE_SKILL_IDS
                and template.template_id not in SINGLE_FAMILY_ENTITY_SKILLS
                # Independently audited player-created towers / magma wall;
                # their class is known even where an attack skill is not.
                and template.template_id not in _REVIEWED_OWNER_ONLY_SUMMON_TEMPLATES
            )
        ))

    def _cached_summon_owner(self, source_actor_id: int, timestamp: float | None = None):
        if timestamp is None:
            self.summon_owners.pop(source_actor_id, None)
            return None
        # A bare actor-id cache is never ownership evidence: short IDs are
        # reused repeatedly in long raids. Return only the exact current
        # birth/template/34-2c binding, and refresh the compatibility cache
        # from that proof rather than trusting its previous contents.
        explicit = self._packet_entity_owner(source_actor_id, timestamp)
        if explicit is None:
            self.summon_owners.pop(source_actor_id, None)
            return None
        self.summon_owners[source_actor_id] = explicit
        return explicit

    def _entity_self_attack_evidence(self, packet, decoded):
        return resolve_existing_summon_hit(self, packet, decoded)

    def _repair_proven_entity_target(self, packet, decoded):
        return repair_proven_entity_target(self, packet, decoded)

    def _packet_entity_owner(self, source_actor_id: int, timestamp: float):
        """Require independent birth/template AND an explicit owner record."""
        ledger = getattr(self, "entity_owner_evidence", None)
        if ledger is None:
            return None
        template = self.monster_entities.get(source_actor_id)
        if template is None or template.boss:
            return None
        binding = ledger.resolve(source_actor_id, template.template_id, timestamp)
        if binding is None:
            return None
        # Audited AI + ACT attack closure, NOT the length of the old guess table.
        # Commandable ducks, robots, relic-shock variants and player clones
        # require the entity's own action/object evidence instead.
        skill = SINGLE_FAMILY_ENTITY_SKILLS.get(template.template_id)
        if skill is None:
            return None
        owner = binding.record.owner_id
        if (owner in self.monster_entities
                or self._action_conflicts_with_confirmed_job(owner, skill)):
            return None
        return owner, skill

    def _entity_damage_evidence(self, source_actor_id: int, timestamp: float,
                                owner_actor_id: int, skill_id: int):
        owner = self._packet_entity_owner(source_actor_id, timestamp)
        if owner != (owner_actor_id, skill_id):
            return {}
        template = self.monster_entities[source_actor_id]
        binding = self.entity_owner_evidence.resolve(source_actor_id, template.template_id, timestamp)
        return dict(wire_entity_id=source_actor_id, entity_template_id=template.template_id,
                    entity_owner_evidence_id=entity_owner_evidence_id(binding))

    def _entity_wire_context(self, source_actor_id: int):
        """Observed entity identity only; not a certification of owner/skill."""
        template = self.monster_entities.get(source_actor_id)
        if template is None or template.boss:
            return {}
        return dict(wire_entity_id=source_actor_id, entity_template_id=template.template_id)

    def _reject_extreme_damage_outlier(
        self,
        source_actor_id: int,
        amount: int,
    ) -> bool:
        actor_samples = self.recent_damage_amounts[source_actor_id]
        reference = (
            tuple(actor_samples)
            if len(actor_samples) >= 24
            else tuple(self.global_damage_amounts)
        )
        if _is_extreme_damage_outlier(amount, reference):
            self.filtered_damage_anomalies += 1
            return True
        actor_samples.append(amount)
        self.global_damage_amounts.append(amount)
        return False
    def _is_hostile_damage_target(
        self, source_actor_id: int | None, target_actor_id: int | None
    ) -> bool:
        return bool(
            target_actor_id is not None
            and target_actor_id > 0
            and target_actor_id != source_actor_id
            and target_actor_id not in self.skill_actor_ids
            and target_actor_id not in self.local_actor_ids
            and target_actor_id not in self.known_actor_names
        )
    def _recent_confirmed_damage_target(
        self,
        source_actor_id: int | None,
        timestamp: float,
        *,
        max_age: float = 10.0,
    ) -> int | None:
        """Return a stable recent enemy target without trusting object IDs."""
        cutoff = timestamp - max_age
        self.recent_confirmed_damage_targets = [
            row for row in self.recent_confirmed_damage_targets if row[0] >= cutoff
        ]
        candidates = [
            row
            for row in self.recent_confirmed_damage_targets
            if source_actor_id is not None and row[1] == source_actor_id
        ]
        if not candidates:
            # Skill objects use their own source IDs. Fall back globally only
            # when recent reliable hits agree on one target; this avoids moving
            # damage between simultaneous enemies in multi-target encounters.
            recent_targets = {row[2] for row in self.recent_confirmed_damage_targets}
            if len(recent_targets) == 1:
                return next(iter(recent_targets))
            if (
                self.boss_actor_id is not None
                and self.boss_actor_id in self.confirmed_damage_target_ids
            ):
                return self.boss_actor_id
            return None

        counts = Counter(row[2] for row in candidates)
        latest_seen: dict[int, float] = {}
        for seen_at, _source, target_actor_id in candidates:
            latest_seen[target_actor_id] = max(
                seen_at, latest_seen.get(target_actor_id, float("-inf"))
            )
        return max(counts, key=lambda target: (counts[target], latest_seen[target]))
    def _normalize_transient_damage_target(self, decoded, timestamp: float):
        """Replace one-use projectile object IDs with the struck actor."""
        target_actor_id = decoded.target_actor_id
        if (
            decoded.kind != "damage_hit"
            or decoded.layout not in _TRANSIENT_DAMAGE_TARGET_LAYOUTS
            or target_actor_id is None
            or target_actor_id in self.monster_entities
            or target_actor_id in self.confirmed_damage_target_ids
        ):
            return decoded
        resolved_target = self._recent_confirmed_damage_target(
            decoded.source_actor_id, timestamp
        )
        if resolved_target is None or resolved_target == target_actor_id:
            return decoded
        return replace(
            decoded,
            target_actor_id=resolved_target,
            alternate_target_actor_id=(
                decoded.alternate_target_actor_id or target_actor_id
            ),
        )
    def _observe_damage_target(
        self,
        source_actor_id: int | None,
        target_actor_id: int | None,
        timestamp: float,
        *,
        confirmed: bool,
    ) -> bool:
        direct_players = (
            self.skill_actor_ids | self.local_actor_ids | set(self.known_actor_names)
        )
        source_was_direct_player = source_actor_id in direct_players
        if source_actor_id is not None and 0 < source_actor_id < 65536:
            owner = self._current_entity_owner_id(source_actor_id, timestamp)
            if owner is not None:
                if target_actor_id == owner:
                    return False
                source_actor_id = owner
        if not self._is_hostile_damage_target(source_actor_id, target_actor_id):
            return False
        assert target_actor_id is not None
        if confirmed:
            self.confirmed_damage_target_ids.add(target_actor_id)
            if source_was_direct_player:
                self.direct_player_confirmed_damage_target_ids.add(target_actor_id)
            if source_actor_id is not None:
                self.recent_confirmed_damage_targets.append(
                    (timestamp, source_actor_id, target_actor_id)
                )
        self.damage_target_counts[target_actor_id] += 1
        self.recent_damage_targets.append((timestamp, target_actor_id))
        self.recent_damage_target_counts[target_actor_id] += 1
        recent_cutoff = timestamp - 8.0
        while (
            self.recent_damage_targets
            and self.recent_damage_targets[0][0] < recent_cutoff
        ):
            _seen_at, expired_target = self.recent_damage_targets.popleft()
            remaining = self.recent_damage_target_counts[expired_target] - 1
            if remaining > 0:
                self.recent_damage_target_counts[expired_target] = remaining
            else:
                del self.recent_damage_target_counts[expired_target]
        self.boss_actor_id = self.recent_damage_target_counts.most_common(1)[0][0]
        return True
    def _common_profile_job(
        self, profile_ids: set[str]
    ) -> tuple[str, int] | None:
        """Return the deepest job shared by all still-possible second jobs."""
        lineages = [
            self.profile_lineages.get(profile_id, ())
            for profile_id in profile_ids
        ]
        lineages = [lineage for lineage in lineages if lineage]
        if not lineages:
            return None
        common_names = set(name for name, _index in lineages[0])
        for lineage in lineages[1:]:
            common_names.intersection_update(name for name, _index in lineage)
        if not common_names:
            return None
        candidates = [
            (index, name)
            for name, index in lineages[0]
            if name in common_names
        ]
        if not candidates:
            return None
        index, name = min(candidates)
        return name, index
    def _historical_profile_job(self, profile_id: int) -> str:
        """Return a previously proven second job for a stable profile ID.

        An inspect-only TCP capture often contains only first-job equipment
        restrictions. A combat capture of the same stable actor ID is stronger
        evidence because a second-job-only cast resolves the class tree. Keep
        this lookup local and read-only; ambiguity still fails closed.
        """
        if profile_id <= 0:
            return ""
        if profile_id in self.historical_profile_jobs:
            return self.historical_profile_jobs[profile_id]
        result = ""
        history_path = (
            Path.home()
            / "Documents"
            / "DragonNestDpsCapture"
            / "live_history.sqlite3"
        )
        if history_path.is_file():
            actor_id = f"0x{profile_id:08X}"
            try:
                connection = sqlite3.connect(
                    f"file:{history_path.as_posix()}?mode=ro",
                    uri=True,
                    timeout=0.25,
                )
                try:
                    compact_table = connection.execute(
                        "SELECT 1 FROM sqlite_schema "
                        "WHERE type = 'table' "
                        "AND name = 'actor_job_observations'"
                    ).fetchone()
                    rows = (
                        connection.execute(
                            """
                            SELECT job, observations
                            FROM actor_job_observations
                            WHERE actor_id = ? AND job <> ''
                            ORDER BY observations DESC, job
                            """,
                            (actor_id,),
                        ).fetchall()
                        if compact_table
                        else []
                    )
                    # Databases created before compact history v2 keep their
                    # evidence in the raw event index until the background
                    # migration has completed.
                    if not rows:
                        legacy_index = connection.execute(
                            "SELECT 1 FROM sqlite_schema "
                            "WHERE type = 'index' AND name = 'idx_events_player'"
                        ).fetchone()
                        legacy_table = connection.execute(
                            "SELECT 1 FROM sqlite_schema "
                            "WHERE type = 'table' AND name = 'events'"
                        ).fetchone()
                        if legacy_table:
                            indexed = (
                                " INDEXED BY idx_events_player"
                                if legacy_index
                                else ""
                            )
                            rows = connection.execute(
                                f"""
                                SELECT job, COUNT(*) AS observations
                                FROM events{indexed}
                                WHERE source_id = ? AND job <> ''
                                GROUP BY job
                                ORDER BY observations DESC, job
                                """,
                                (actor_id,),
                            ).fetchall()
                finally:
                    connection.close()
            except sqlite3.Error:
                rows = []
            second_jobs = [
                (int(count), str(job).strip())
                for job, count in rows
                if self.job_name_indexes.get(str(job).strip(), 99) == 1
            ]
            if second_jobs:
                second_jobs.sort(reverse=True)
                top_count, top_job = second_jobs[0]
                runner_up = second_jobs[1][0] if len(second_jobs) > 1 else 0
                if top_count >= 3 and (runner_up == 0 or top_count >= runner_up * 3):
                    result = top_job
        self.historical_profile_jobs[profile_id] = result
        return result
    def _skill_plate_specialization_job(
        self,
        direct_job: str,
        skill_plate_names: list[str],
    ) -> tuple[str, tuple[str, ...]]:
        """Resolve a second job from equipped plates tied to its EX branch."""
        if not direct_job or not skill_plate_names:
            return "", ()
        candidate_profiles = {
            profile_id: self.profile_jobs.get(profile_id, "")
            for profile_id, lineage in self.profile_lineages.items()
            if any(job_name == direct_job for job_name, _index in lineage)
            and self.job_name_indexes.get(self.profile_jobs.get(profile_id, ""), 99)
            == 1
        }
        if len(candidate_profiles) < 2:
            return "", ()
        normalized_plates = {
            _skill_branch_key(name): name for name in skill_plate_names if name
        }
        scores: Counter[str] = Counter()
        evidence: dict[str, list[str]] = defaultdict(list)
        for skill_id, profile_ids in self.skill_profiles.items():
            skill_name = _skill_branch_key(
                self.skill_catalog.get(skill_id, ("", "", 99))[0]
            )
            if not skill_name.endswith("EX"):
                continue
            plate_name = normalized_plates.get(skill_name[:-2])
            matching_profiles = set(profile_ids).intersection(candidate_profiles)
            if plate_name and len(matching_profiles) == 1:
                profile_id = next(iter(matching_profiles))
                scores[profile_id] += 1
                evidence[profile_id].append(plate_name)
        candidate_jobs = set(candidate_profiles.values())
        for normalized_name, plate_name in normalized_plates.items():
            affinity_job = _SKILL_PLATE_SECOND_JOB_AFFINITY.get(normalized_name, "")
            if not affinity_job or affinity_job not in candidate_jobs:
                continue
            profile_id = next(
                profile
                for profile, job_name in candidate_profiles.items()
                if job_name == affinity_job
            )
            scores[profile_id] += 1
            evidence[profile_id].append(plate_name)
        if not scores:
            return "", ()
        ranked = scores.most_common()
        winner_profile, winner_score = ranked[0]
        runner_score = ranked[1][1] if len(ranked) > 1 else 0
        if winner_score <= runner_score:
            return "", ()
        return candidate_profiles[winner_profile], tuple(evidence[winner_profile])
    def _update_actor_job(self, actor_id: int, skill_id: int) -> None:
        """Accumulate class-tree evidence without guessing from shared skills."""
        profile_ids = set(self.skill_profiles.get(skill_id, frozenset()))
        if not profile_ids:
            skill_info = self.skill_catalog.get(skill_id)
            if skill_info is None or not skill_info[1]:
                return
            _name, job_name, job_index = skill_info
            if skill_id in _UNIQUE_MECHANIC_MODE_SKILL_IDS:
                # These synthetic IDs are emitted only by Mechanical Mode.
                # They are not present in the regular tree-profile map, so a
                # previously observed shared Engineer skill must not keep the
                # actor stuck at the first job during replay/scoring.
                self.actor_jobs[actor_id] = (job_name, -1)
                matching_profiles = {
                    profile_id
                    for profile_id, profile_job in self.profile_jobs.items()
                    if profile_job == job_name
                }
                if matching_profiles:
                    self.actor_skill_profiles[actor_id] = matching_profiles
                return
            existing = self.actor_jobs.get(actor_id)
            if existing is None or (existing[1] >= 0 and job_index < existing[1]):
                self.actor_jobs[actor_id] = (job_name, job_index)
            return

        current_profiles = self.actor_skill_profiles.get(actor_id)
        if current_profiles:
            shared_profiles = current_profiles.intersection(profile_ids)
            if shared_profiles:
                resolved_profiles = shared_profiles
            else:
                # A confirmed first/base-job branch is evidence too. Do not
                # replace Mercenary's two possible specializations with a
                # cross-class singleton just because its second job is not
                # known yet. Authoritative session/spawn identities have their
                # own update path and can replace stale runtime identities.
                resolved_profiles = set(current_profiles)
        else:
            resolved_profiles = profile_ids
        self.actor_skill_profiles[actor_id] = resolved_profiles

        if len(resolved_profiles) == 1:
            profile_id = next(iter(resolved_profiles))
            job_name = self.profile_jobs.get(profile_id, "")
            if job_name:
                self.actor_jobs[actor_id] = (job_name, -1)
            return

        common_job = self._common_profile_job(resolved_profiles)
        existing = self.actor_jobs.get(actor_id)
        if common_job is not None and (existing is None or existing[1] >= 0):
            self.actor_jobs[actor_id] = common_job
    def _action_conflicts_with_confirmed_job(
        self, actor_id: int, skill_id: int
    ) -> bool:
        if (
            skill_id in _DARK_AVENGER_AMBIGUOUS_BASE_SKILL_IDS
            and self._actor_is_confirmed_dark_avenger(actor_id)
        ):
            return True
        actor_profiles = self.actor_skill_profiles.get(actor_id, frozenset())
        skill_profiles = self.skill_profiles.get(skill_id, frozenset())
        return bool(
            actor_profiles
            and skill_profiles
            and actor_profiles.isdisjoint(skill_profiles)
        )
    def _actor_is_confirmed_dark_avenger(self, actor_id: int) -> bool:
        job_name, job_depth = self.actor_jobs.get(actor_id, ("", 99))
        if job_name == "黑暗复仇者" and job_depth < 0:
            return True
        profiles = self.actor_skill_profiles.get(actor_id, frozenset())
        return bool(
            len(profiles) == 1
            and self.profile_jobs.get(next(iter(profiles)), "") == "黑暗复仇者"
        )
    def _is_ambiguous_dark_avenger_packet_skill(
        self, actor_id: int, skill_id: int | None
    ) -> bool:
        return bool(
            skill_id in _DARK_AVENGER_AMBIGUOUS_BASE_SKILL_IDS
            and self._actor_is_confirmed_dark_avenger(actor_id)
        )
    def _infer_recent_summon_owner(
        self, source_actor_id: int, timestamp: float
    ) -> tuple[int, int] | None:
        """Resolve a summon only from its current packet-backed generation.

        The former fallback assigned any newly seen short actor to whichever
        summon button was pressed most recently. It is intentionally removed:
        time proximity cannot establish entity identity or ownership.
        """
        explicit = V576ActorStateMixin._packet_entity_owner(self, source_actor_id, timestamp)
        if explicit is not None:
            self.summon_owners[source_actor_id] = explicit
            return explicit
        self.summon_owners.pop(source_actor_id, None)
        return None
    def _summon_owner_from_explicit_skill(
        self,
        source_actor_id: int,
        skill_id: int | None,
        timestamp: float,
    ) -> tuple[int, int] | None:
        """Prefer a summon hit's own skill ID over proximity guessing.

        Several summon classes can act at the same time. A newly seen short
        actor ID was previously assigned to whichever summon button happened
        to be pressed last, even when its damage packet explicitly identified
        Soul Gate or another summon. Short actor IDs are also reused during a
        long encounter, so authoritative packet evidence must be allowed to
        replace an older heuristic owner.
        """
        if skill_id is None:
            return None
        canonical_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)
        if canonical_skill_id not in self.summon_creation_skill_ids:
            return None
        if (
            source_actor_id <= 0
            or source_actor_id > _SUMMON_ACTOR_ID_MAX
            or source_actor_id == self.boss_actor_id
            or source_actor_id in self.known_actor_names
            or source_actor_id in self.skill_actor_ids
            or source_actor_id in self.local_actor_ids
        ):
            return None

        template = self.monster_entities.get(source_actor_id)
        ledger = getattr(self, "entity_owner_evidence", None)
        if template is None or template.boss or ledger is None:
            return None
        binding = ledger.resolve(source_actor_id, template.template_id, timestamp)
        if binding is None:
            return None
        permitted = set(_SKILL_OBJECT_TEMPLATE_SKILL_IDS.get(template.template_id, ()))
        single_family = SINGLE_FAMILY_ENTITY_SKILLS.get(template.template_id)
        if single_family is not None:
            permitted.add(single_family)
        if canonical_skill_id not in permitted:
            return None
        owner = (binding.record.owner_id, canonical_skill_id)
        self.summon_owners[source_actor_id] = owner
        return owner
    def _recent_caster_for_damage_skill(
        self,
        skill_id: int,
        timestamp: float,
        *,
        max_age: float = 15.0,
    ) -> int | None:
        """Return the newest real player that used this damage skill.

        v576 damage objects use short, reusable runtime IDs.  Their hit packet
        often contains the player-facing skill ID even though the source field
        contains only that temporary object ID.  Resolve ownership from the
        exact skill on every hit instead of binding the short ID for the whole
        encounter.
        """
        canonical_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)
        equivalent_skill_ids = {canonical_skill_id, skill_id}
        equivalent_skill_ids.update(
            raw_skill_id
            for raw_skill_id, result_skill_id in _LIVE_DAMAGE_SKILL_ALIASES.items()
            if result_skill_id == canonical_skill_id
        )
        candidates: list[tuple[float, int]] = []
        for equivalent_skill_id in equivalent_skill_ids:
            for actor_id, cast_time in self.recent_skill_casters.get(
                equivalent_skill_id, ()
            ):
                age = timestamp - cast_time
                if (
                    0.0 <= age <= max_age
                    and actor_id > _SUMMON_ACTOR_ID_MAX
                    and actor_id not in self.monster_entities
                ):
                    candidates.append((cast_time, actor_id))
        return max(candidates)[1] if candidates else None
    def _skill_object_owner(
        self,
        source_actor_id: int,
        target_actor_id: int | None,
        skill_id: int | None,
        timestamp: float,
    ) -> tuple[int, int] | None:
        """Resolve one hit emitted by a reusable field/projectile actor.

        The same short source ID can represent a Swordmaster effect, a Sorceress
        field and a summon at different points in one capture.  Explicit skill
        evidence therefore wins over a stale summon mapping.  Skill-less
        continuation hits may inherit only the immediately preceding owner on
        the same source/target pair.
        """
        if self._legacy_entity_owner_blocked(source_actor_id, timestamp):
            return None
        explicit = self._packet_entity_owner(source_actor_id, timestamp)
        if explicit is not None:
            self.summon_owners[source_actor_id] = explicit
            return explicit
        template = self.monster_entities.get(source_actor_id)
        template_skill_ids = (
            _SKILL_OBJECT_TEMPLATE_SKILL_IDS.get(template.template_id, ())
            if template is not None and not template.boss
            else ()
        )
        template_owner_profiles = (
            _SKILL_OBJECT_TEMPLATE_OWNER_PROFILES.get(
                template.template_id,
                frozenset(),
            )
            if template is not None and not template.boss
            else frozenset()
        )

        # A server-created entity is a summon only after this exact runtime
        # generation has independent owner evidence. Never let template/job
        # uniqueness or a nearby cast manufacture the missing owner.
        if template is not None and not template.boss:
            self.summon_owners.pop(source_actor_id, None)
            return None

        def template_owner_match(owner_actor_id: int) -> bool | None:
            """Return True/False for proven profiles, None when still unknown."""

            if not template_owner_profiles:
                return True
            actor_profiles = self.actor_skill_profiles.get(owner_actor_id)
            if not actor_profiles:
                return None
            return not template_owner_profiles.isdisjoint(actor_profiles)
        if (
            source_actor_id <= 0
            or source_actor_id > _SUMMON_ACTOR_ID_MAX
            or (
                source_actor_id == self.boss_actor_id
                and not template_skill_ids
            )
            or source_actor_id in self.known_actor_names
            or source_actor_id in self.skill_actor_ids
            or source_actor_id in self.local_actor_ids
            or (
                not template_skill_ids
                and (
                    target_actor_id is None
                    or target_actor_id > _SUMMON_ACTOR_ID_MAX
                )
            )
        ):
            return None


        existing_summon_owner = self.summon_owners.get(source_actor_id)
        canonical_skill_id = (
            _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)
            if skill_id is not None
            else None
        )
        existing_owner_match = (
            template_owner_match(existing_summon_owner[0])
            if existing_summon_owner is not None
            else None
        )
        existing_owner_compatible = bool(
            existing_summon_owner is not None
            and (
                existing_summon_owner[1] in template_skill_ids
                or canonical_skill_id is None
                or canonical_skill_id == existing_summon_owner[1]
            )
        )
        if (
            existing_summon_owner is not None
            and existing_owner_compatible
            and existing_owner_match is True
        ):
            return existing_summon_owner
        if (
            existing_summon_owner is not None
            and existing_owner_match is False
        ):
            # Runtime short IDs are reused and the old proximity heuristic may
            # already have attached this generation to the other Priest
            # branch.  Do not let that stale binding bypass template evidence.
            self.summon_owners.pop(source_actor_id, None)

        object_key = (source_actor_id, target_actor_id or 0)
        template_candidates: list[tuple[float, int, int]] = []
        for template_skill_id in template_skill_ids:
            canonical_template_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(
                template_skill_id, template_skill_id
            )
            equivalent_skill_ids = {
                template_skill_id,
                canonical_template_skill_id,
            }
            equivalent_skill_ids.update(
                raw_skill_id
                for raw_skill_id, result_skill_id
                in _LIVE_DAMAGE_SKILL_ALIASES.items()
                if result_skill_id == canonical_template_skill_id
            )
            max_age = (
                120.0
                if canonical_template_skill_id in self.summon_creation_skill_ids
                else 30.0
            )
            for equivalent_skill_id in equivalent_skill_ids:
                for owner_actor_id, cast_time in self.recent_skill_casters.get(
                    equivalent_skill_id, ()
                ):
                    age = timestamp - cast_time
                    if (
                        0.0 <= age <= max_age
                        and owner_actor_id > _SUMMON_ACTOR_ID_MAX
                        and owner_actor_id not in self.monster_entities
                        and not self._action_conflicts_with_confirmed_job(
                            owner_actor_id, canonical_template_skill_id
                        )
                    ):
                        template_candidates.append(
                            (
                                cast_time,
                                owner_actor_id,
                                canonical_template_skill_id,
                            )
                        )
        if template_owner_profiles and template_candidates:
            confirmed_template_candidates = [
                candidate
                for candidate in template_candidates
                if template_owner_match(candidate[1]) is True
            ]
            unknown_template_candidates = [
                candidate
                for candidate in template_candidates
                if template_owner_match(candidate[1]) is None
            ]
            # Prefer packet-confirmed second-job evidence.  Unknown actors are
            # retained only until no matching profile has been observed, so a
            # capture that begins mid-fight still attributes damage instead of
            # dropping the summon completely.
            template_candidates = (
                confirmed_template_candidates
                if confirmed_template_candidates
                else unknown_template_candidates
            )
        if template_candidates:
            _cast_time, owner_actor_id, owner_skill_id = max(
                template_candidates
            )
            owner = (owner_actor_id, owner_skill_id)
            self.recent_skill_object_owners[object_key] = (
                owner_actor_id,
                owner_skill_id,
                timestamp,
            )
            if owner_skill_id in self.summon_creation_skill_ids:
                self.summon_owners[source_actor_id] = owner
            return owner

        if template_skill_ids:
            # Permanent companions and objects already alive when capture
            # starts may have no usable cast packet. Resolve them when exactly
            # one confirmed player job can own the template. A template may
            # expose several related skills (for example summon/call); those
            # are not an ownership ambiguity when they all point to one actor.
            profile_candidates: dict[int, list[int]] = {}
            for template_skill_id in template_skill_ids:
                skill_profiles = self.skill_profiles.get(
                    template_skill_id, frozenset()
                )
                if not skill_profiles:
                    continue
                for owner_actor_id, actor_profiles in self.actor_skill_profiles.items():
                    if (
                        owner_actor_id > _SUMMON_ACTOR_ID_MAX
                        and owner_actor_id not in self.monster_entities
                        and template_owner_match(owner_actor_id) is not False
                        and not skill_profiles.isdisjoint(actor_profiles)
                    ):
                        profile_candidates.setdefault(owner_actor_id, []).append(
                            template_skill_id
                        )
            if len(profile_candidates) == 1:
                owner_actor_id, candidate_skill_ids = next(
                    iter(profile_candidates.items())
                )
                owner_skill_id = next(
                    skill_id
                    for skill_id in template_skill_ids
                    if skill_id in candidate_skill_ids
                )
                owner = (owner_actor_id, owner_skill_id)
                self.recent_skill_object_owners[object_key] = (
                    owner_actor_id,
                    owner_skill_id,
                    timestamp,
                )
                self.summon_owners[source_actor_id] = owner
                return owner

        if (
            existing_summon_owner is not None
            and existing_owner_compatible
            and existing_owner_match is None
        ):
            # Captures can begin before the owner's class tree is known. Keep
            # that packet-backed binding only after every confirmed matching
            # caster/profile candidate above has had the chance to replace it.
            return existing_summon_owner

        if skill_id is not None and not (
            template_skill_ids and template_owner_profiles
        ):
            canonical_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(
                skill_id, skill_id
            )
            if canonical_skill_id in self.summon_creation_skill_ids:
                # Missing birth/template/owner evidence is repaired only by
                # the exact owner+native-attack recovery path. A recent summon
                # cast is not sufficient to claim this actor or its damage.
                return None
            owner_actor_id = self._recent_caster_for_damage_skill(
                canonical_skill_id,
                timestamp,
                max_age=(
                    120.0
                    if canonical_skill_id in self.summon_creation_skill_ids
                    else 15.0
                ),
            )
            if owner_actor_id is not None:
                owner = (owner_actor_id, canonical_skill_id)
                self.recent_skill_object_owners[object_key] = (
                    owner_actor_id,
                    canonical_skill_id,
                    timestamp,
                )
                if canonical_skill_id in self.summon_creation_skill_ids:
                    self.summon_owners[source_actor_id] = owner
                else:
                    stale_owner = self.summon_owners.get(source_actor_id)
                    if stale_owner is not None and stale_owner != owner:
                        self.summon_owners.pop(source_actor_id, None)
                return owner

        recent_owner = self.recent_skill_object_owners.get(object_key)
        if recent_owner is not None:
            owner_actor_id, owner_skill_id, seen_at = recent_owner
            if 0.0 <= timestamp - seen_at <= 1.5:
                self.recent_skill_object_owners[object_key] = (
                    owner_actor_id,
                    owner_skill_id,
                    timestamp,
                )
                return owner_actor_id, owner_skill_id
        return None
    def _canonical_player_actor_id(self, actor_id: int) -> int:
        """Resolve a reconnect runtime ID to its first session identity."""
        try:
            current = int(actor_id)
        except (TypeError, ValueError):
            return 0
        path: list[int] = []
        seen: set[int] = set()
        while current in self.player_runtime_aliases and current not in seen:
            seen.add(current)
            path.append(current)
            current = int(self.player_runtime_aliases[current])
        for item in path:
            self.player_runtime_aliases[item] = current
        return current

    def _runtime_actor_name_is_anonymous(
        self,
        name: str,
        actor_id: int | None = None,
    ) -> bool:
        value = str(name or "").strip()
        if not value:
            return True
        if re.fullmatch(r"角色\s*0x[0-9A-Fa-f]{1,8}", value):
            return True
        if actor_id is not None and value.casefold() == f"0x{actor_id:08X}".casefold():
            return True
        return False

    def _runtime_alias_jobs_compatible(
        self,
        left_job: str,
        right_job: str,
    ) -> bool:
        left = str(left_job or "").strip()
        right = str(right_job or "").strip()
        if not left or not right:
            return False
        if left == right:
            return True
        for lineage in self.profile_lineages.values():
            names = {str(job_name or "").strip() for job_name, _index in lineage}
            if left in names and right in names:
                return True
        return False

    def _preferred_runtime_job(self, left_job: str, right_job: str) -> str:
        left = str(left_job or "").strip()
        right = str(right_job or "").strip()
        if not left:
            return right
        if not right:
            return left
        left_rank = int(self.job_name_indexes.get(left, 99))
        right_rank = int(self.job_name_indexes.get(right, 99))
        return right if right_rank < left_rank else left

    def _register_runtime_actor_alias(
        self,
        new_actor_id: int,
        old_actor_id: int,
        *,
        timestamp: float,
        reason: str,
        signature: tuple[int, int] | None = None,
    ) -> bool:
        """Join one reconnected runtime entity using conservative evidence."""
        new_actor_id = int(new_actor_id)
        old_actor_id = self._canonical_player_actor_id(old_actor_id)
        if (
            new_actor_id <= _SUMMON_ACTOR_ID_MAX
            or old_actor_id <= _SUMMON_ACTOR_ID_MAX
            or new_actor_id == old_actor_id
            or new_actor_id in self.monster_entities
            or old_actor_id in self.monster_entities
            or new_actor_id in self.summon_owners
            or old_actor_id in self.summon_owners
        ):
            return False
        existing = self._canonical_player_actor_id(new_actor_id)
        if existing != new_actor_id:
            return existing == old_actor_id

        self.player_runtime_aliases[new_actor_id] = old_actor_id
        for alias_actor_id, canonical_actor_id in tuple(
            self.player_runtime_aliases.items()
        ):
            if canonical_actor_id == new_actor_id:
                self.player_runtime_aliases[alias_actor_id] = old_actor_id

        old_name = self.known_actor_names.get(old_actor_id, "")
        new_name = self.known_actor_names.get(new_actor_id, "")
        if self._runtime_actor_name_is_anonymous(old_name, old_actor_id):
            old_name = ""
        if self._runtime_actor_name_is_anonymous(new_name, new_actor_id):
            new_name = ""
        if not old_name and new_name:
            old_name = new_name
            self.known_actor_names[old_actor_id] = new_name
        if old_name:
            self.known_actor_names[new_actor_id] = old_name

        old_job = self.actor_jobs.get(old_actor_id, ("", 99))
        new_job = self.actor_jobs.get(new_actor_id, ("", 99))
        if new_job[0] and (
            not old_job[0]
            or int(new_job[1]) < int(old_job[1])
            or (
                int(new_job[1]) == int(old_job[1])
                and self.job_name_indexes.get(new_job[0], 99)
                < self.job_name_indexes.get(old_job[0], 99)
            )
        ):
            self.actor_jobs[old_actor_id] = new_job
        if old_actor_id in self.actor_jobs:
            self.actor_jobs[new_actor_id] = self.actor_jobs[old_actor_id]

        old_profiles = set(self.actor_skill_profiles.get(old_actor_id, set()))
        new_profiles = set(self.actor_skill_profiles.get(new_actor_id, set()))
        if old_profiles and new_profiles:
            shared_profiles = old_profiles.intersection(new_profiles)
            if shared_profiles:
                self.actor_skill_profiles[old_actor_id] = shared_profiles
        elif new_profiles:
            self.actor_skill_profiles[old_actor_id] = new_profiles
        if old_actor_id in self.actor_skill_profiles:
            self.actor_skill_profiles[new_actor_id] = set(
                self.actor_skill_profiles[old_actor_id]
            )

        self.player_actor_observed_skills[old_actor_id].update(
            self.player_actor_observed_skills.get(new_actor_id, set())
        )
        old_latest = self.latest_actor_skills.get(old_actor_id)
        new_latest = self.latest_actor_skills.get(new_actor_id)
        if new_latest is not None and (
            old_latest is None or new_latest[1] >= old_latest[1]
        ):
            self.latest_actor_skills[old_actor_id] = new_latest
        self.player_actor_last_observed[old_actor_id] = max(
            float(timestamp),
            self.player_actor_last_observed.get(old_actor_id, float("-inf")),
            self.player_actor_last_observed.get(new_actor_id, float("-inf")),
        )
        if new_actor_id in self.player_actor_zero_hp_at:
            self.player_actor_zero_hp_at[old_actor_id] = max(
                self.player_actor_zero_hp_at.get(old_actor_id, float("-inf")),
                self.player_actor_zero_hp_at[new_actor_id],
            )
        if new_actor_id in self.local_actor_ids:
            self.local_actor_ids.add(old_actor_id)
            if self.current_self_actor_id == new_actor_id:
                self.current_self_actor_id = old_actor_id
        self.pending_runtime_alias_candidates.pop(new_actor_id, None)
        self.party_member_last_emit.pop(new_actor_id, None)
        self.runtime_alias_evidence.append(
            {
                "new_actor_id": f"0x{new_actor_id:08X}",
                "canonical_actor_id": f"0x{old_actor_id:08X}",
                "timestamp": float(timestamp),
                "reason": str(reason),
                "resource_signature": tuple(signature or ()),
            }
        )
        if len(self.runtime_alias_evidence) > 64:
            del self.runtime_alias_evidence[:-64]
        return True

    def _observe_player_health_state(
        self,
        *,
        actor_id: int,
        current_hp: int,
        current_mp: int,
        packet_kind: str,
        timestamp: float,
    ) -> None:
        """Track reconnect evidence before a health event reaches aggregation."""
        raw_actor_id = int(actor_id)
        if (
            raw_actor_id <= _SUMMON_ACTOR_ID_MAX
            or raw_actor_id in self.monster_entities
            or raw_actor_id in self.summon_owners
        ):
            return
        canonical_actor_id = self._canonical_player_actor_id(raw_actor_id)
        resource_kind = "s2c-udp-player-resource-sync-210c-v576"
        signature: tuple[int, int] | None = None
        if (
            packet_kind == resource_kind
            and int(current_hp) > 0
            and int(current_mp) > 0
        ):
            signature = (int(current_hp), int(current_mp))
            observations = self.player_resource_observations[signature]
            if canonical_actor_id == raw_actor_id:
                candidates: dict[int, tuple[float, bool]] = {}
                for observed_actor_id, observed_at in observations.items():
                    old_actor_id = self._canonical_player_actor_id(
                        observed_actor_id
                    )
                    if old_actor_id == raw_actor_id:
                        continue
                    last_seen = self.player_actor_last_observed.get(
                        old_actor_id, observed_at
                    )
                    gap_seconds = float(timestamp) - float(last_seen)
                    zero_at = self.player_actor_zero_hp_at.get(old_actor_id)
                    terminal_zero = bool(
                        zero_at is not None
                        and float(timestamp) - float(zero_at) >= 0.25
                        and float(last_seen) <= float(zero_at) + 0.5
                    )
                    if terminal_zero or gap_seconds >= 30.0:
                        previous = candidates.get(old_actor_id)
                        if previous is None or observed_at > previous[0]:
                            candidates[old_actor_id] = (
                                float(observed_at),
                                terminal_zero,
                            )
                if len(candidates) == 1:
                    old_actor_id, (_observed_at, terminal_zero) = next(
                        iter(candidates.items())
                    )
                    if terminal_zero:
                        self._register_runtime_actor_alias(
                            raw_actor_id,
                            old_actor_id,
                            timestamp=timestamp,
                            reason="matching_21c_after_terminal_zero_hp",
                            signature=signature,
                        )
                    else:
                        self.pending_runtime_alias_candidates[raw_actor_id] = (
                            old_actor_id,
                            signature,
                            float(timestamp),
                        )
                else:
                    self.pending_runtime_alias_candidates.pop(
                        raw_actor_id, None
                    )
            canonical_actor_id = self._canonical_player_actor_id(raw_actor_id)
            observations[canonical_actor_id] = float(timestamp)

        canonical_actor_id = self._canonical_player_actor_id(raw_actor_id)
        if (
            packet_kind == "s2c-udp-player-hp-state-086f-v576"
            and int(current_hp) == 0
        ):
            self.player_actor_zero_hp_at[canonical_actor_id] = float(timestamp)
        self.player_actor_last_observed[canonical_actor_id] = float(timestamp)

    def _observe_player_skill_activity(
        self,
        actor_id: int,
        skill_id: int,
        timestamp: float,
    ) -> None:
        """Confirm a pending resource match with current class/skill evidence."""
        raw_actor_id = int(actor_id)
        if (
            raw_actor_id <= _SUMMON_ACTOR_ID_MAX
            or raw_actor_id in self.monster_entities
            or raw_actor_id in self.summon_owners
        ):
            return
        pending = self.pending_runtime_alias_candidates.get(raw_actor_id)
        if pending is not None:
            old_actor_id, signature, observed_at = pending
            if float(timestamp) - float(observed_at) > 10 * 60.0:
                self.pending_runtime_alias_candidates.pop(raw_actor_id, None)
            else:
                old_actor_id = self._canonical_player_actor_id(old_actor_id)
                old_job = self.actor_jobs.get(old_actor_id, ("", 99))[0]
                new_job = self.actor_jobs.get(raw_actor_id, ("", 99))[0]
                shared_skill = int(skill_id) in self.player_actor_observed_skills.get(
                    old_actor_id, set()
                )
                compatible_job = self._runtime_alias_jobs_compatible(
                    old_job, new_job
                )
                if compatible_job or shared_skill:
                    self._register_runtime_actor_alias(
                        raw_actor_id,
                        old_actor_id,
                        timestamp=timestamp,
                        reason=(
                            "matching_21c_and_job_lineage"
                            if compatible_job
                            else "matching_21c_and_shared_skill"
                        ),
                        signature=signature,
                    )

        canonical_actor_id = self._canonical_player_actor_id(raw_actor_id)
        if canonical_actor_id != raw_actor_id:
            canonical_job = self.actor_jobs.get(canonical_actor_id, ("", 99))
            raw_job = self.actor_jobs.get(raw_actor_id, ("", 99))
            if raw_job[0] and (
                not canonical_job[0] or int(raw_job[1]) < int(canonical_job[1])
            ):
                self.actor_jobs[canonical_actor_id] = raw_job
        self.player_actor_observed_skills[canonical_actor_id].add(int(skill_id))
        self.player_actor_last_observed[canonical_actor_id] = float(timestamp)

    def _runtime_alias_event_context(
        self,
        actor_id: str,
        name: str,
        job: str,
    ) -> tuple[str, str, str]:
        value = str(actor_id or "").strip()
        match = re.fullmatch(r"0x([0-9A-Fa-f]{1,8})", value)
        if match is None:
            return value, name, job
        raw_actor_id = int(match.group(1), 16)
        canonical_actor_id = self._canonical_player_actor_id(raw_actor_id)
        if canonical_actor_id == raw_actor_id:
            return value, name, job
        best_name, best_job = self._actor_display_context(canonical_actor_id)
        incoming_name = str(name or "").strip()
        if not self._runtime_actor_name_is_anonymous(
            incoming_name, raw_actor_id
        ):
            best_name = incoming_name
            self.known_actor_names[canonical_actor_id] = incoming_name
        best_job = self._preferred_runtime_job(best_job, job)
        return f"0x{canonical_actor_id:08X}", best_name, best_job

    def _canonicalize_runtime_alias_events(
        self,
        events: list[LiveDpsEvent],
    ) -> list[LiveDpsEvent]:
        """Rewrite both sides of live events after a reconnect is proven."""
        if not events or not self.player_runtime_aliases:
            return events
        normalized: list[LiveDpsEvent] = []
        for event in events:
            if isinstance(event, CombatEvent):
                source_id, source_name, source_job = (
                    self._runtime_alias_event_context(
                        event.source_id, event.source_name, event.job
                    )
                )
                target_id, target_name, target_job = (
                    self._runtime_alias_event_context(
                        event.target_id, event.target_name, event.target_job
                    )
                )
                event = replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            elif isinstance(event, SkillCastEvent):
                source_id, source_name, source_job = (
                    self._runtime_alias_event_context(
                        event.source_id, event.source_name, event.job
                    )
                )
                event = replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                )
            elif isinstance(event, SkillParameterEvent):
                source_id, source_name, source_job = (
                    self._runtime_alias_event_context(
                        event.source_id, event.source_name, event.job
                    )
                )
                target_id, _target_name, _target_job = (
                    self._runtime_alias_event_context(event.target_id, "", "")
                )
                event = replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    target_id=target_id,
                )
            elif isinstance(event, SummonRemovalEvent):
                owner_id, owner_name, owner_job = (
                    self._runtime_alias_event_context(
                        event.owner_id,
                        event.owner_name,
                        event.owner_job,
                    )
                )
                event = replace(
                    event,
                    owner_id=owner_id,
                    owner_name=owner_name,
                    owner_job=owner_job,
                )
            elif isinstance(event, PartyMemberEvent):
                actor_id, name, job = self._runtime_alias_event_context(
                    event.actor_id, event.name, event.job
                )
                event = replace(
                    event,
                    actor_id=actor_id,
                    name=name,
                    job=job,
                    confidence=(
                        max(0.95, event.confidence)
                        if not self._runtime_actor_name_is_anonymous(name)
                        else event.confidence
                    ),
                )
            elif isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
                target_id, target_name, target_job = (
                    self._runtime_alias_event_context(
                        event.target_id,
                        event.target_name,
                        event.target_job,
                    )
                )
                event = replace(
                    event,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            elif isinstance(event, PlayerInspectEvent):
                target_id, target_name, target_job = (
                    self._runtime_alias_event_context(
                        event.target_id, event.target_name, event.job
                    )
                )
                event = replace(
                    event,
                    target_id=target_id,
                    target_name=target_name,
                    job=target_job,
                )
            elif isinstance(event, BossStatusEvent):
                source_id, source_name, source_job = (
                    self._runtime_alias_event_context(
                        event.source_id, event.source_name, event.job
                    )
                )
                target_id, target_name, target_job = (
                    self._runtime_alias_event_context(
                        event.target_id, event.target_name, event.target_job
                    )
                )
                event = replace(
                    event,
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                )
            elif isinstance(event, BossActionEvent) and event.affected_target_ids:
                affected_target_ids = tuple(
                    dict.fromkeys(
                        self._runtime_alias_event_context(actor_id, "", "")[0]
                        for actor_id in event.affected_target_ids
                    )
                )
                event = replace(event, affected_target_ids=affected_target_ids)
            normalized.append(event)
        return normalized

    def _actor_display_context(self, actor_id: int) -> tuple[str, str]:
        """Resolve a session-specific actor ID to a readable player name."""
        actor_id = self._canonical_player_actor_id(actor_id)
        monster_name, monster_level = self._monster_display_context(actor_id)
        if monster_name:
            return monster_name, monster_level
        actor_job, actor_job_index = self.actor_jobs.get(actor_id, ("", 99))
        if actor_id in self.local_actor_ids and self.local_name:
            name = self.local_name
        else:
            name = self.known_actor_names.get(actor_id, "")
        if not name:
            name = f"角色 0x{actor_id:08X}"
        known_job = self.known_players.get(name, {}).get("job", "")
        # A historical job is only a fallback after this session has already
        # supplied the same real name. Current packet/skill evidence always
        # wins and can never be downgraded by the legacy registry.
        display_job = actor_job or known_job
        return name, display_job
    def _monster_display_context(self, actor_id: int | None) -> tuple[str, str]:
        if actor_id is None:
            return "", ""
        template = self.monster_entities.get(actor_id)
        if template is None:
            return "", ""
        level_label = f"Lv{template.level}" if template.level else ""
        target_kind = "Boss" if self._is_boss_actor(actor_id) else ""
        return template.name, " ".join(
            value for value in (target_kind, level_label) if value
        )
    def _is_boss_actor(self, actor_id: int | None) -> bool:
        """这个 actor 算不算这场战斗的主 Boss。

        客户端怪物表不是权威：阶段体（红龙佩达 503681/503682、沙漠龙扎卡德
        4150050/4160001、沙虫哈扎尔 4150065）在表里都是 ``boss=False``，只有
        终局形态标了 boss。可它们就是这一关的 Boss —— 只按 ``template.boss``
        判定，这些 actor 的技能/动作/状态证据会在生成事件那一步被整段丢掉，
        表现就是「血量行有、机制提示和倒计时一条都没有」。

        ``boss_actor_ids`` 才是权威集合：它在绑定时除了 ``template.boss``，
        还认「固定血量档案登记过的名字」和副本 Boss 模板集合
        （见 ``bootstrap_boss_entities`` / ``_observe_monster_spawn``）。
        """
        if actor_id is None:
            return False
        if actor_id in self.boss_actor_ids:
            return True
        template = self.monster_entities.get(actor_id)
        return bool(template is not None and template.boss)
    def note_skill_drop(self, reason: str, actor_id: int | None, skill: object) -> None:
        """记一条「解出来了但没进事件」的 Boss 技能。

        只累加计数，不做 I/O，所以回放和实机都能常开。抓包回放测试用
        ``skill_drop_counts`` 做守恒断言：一个都不该有。
        """
        counts = getattr(self, "skill_drop_counts", None)
        if counts is None:
            counts = self.skill_drop_counts = {}
        counts[reason] = counts.get(reason, 0) + 1
        samples = getattr(self, "skill_drop_samples", None)
        if samples is None:
            samples = self.skill_drop_samples = []
        if len(samples) < 32:
            try:
                actor_text = f"0x{int(actor_id or 0):08X}"
            except (TypeError, ValueError):
                actor_text = str(actor_id)
            try:
                skill_text = int(skill or 0)
            except (TypeError, ValueError):
                skill_text = 0
            samples.append(
                {"reason": reason, "actor": actor_text, "skill": skill_text}
            )
    def _monster_event_id(self, actor_id: int | None) -> str:
        if actor_id is None:
            return ""
        template = self.monster_entities.get(actor_id)
        if template is None:
            return f"0x{actor_id:08X}"
        return f"0x{actor_id:08X}@{template.template_id}"
    def _observe_login_identity(self, packet: PacketRecord) -> None:
        """Remember the authoritative class path for the character logging in."""

        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_login_identity_payload(payload)
            if decoded is None:
                continue
            if decoded.actor_marker != self.current_login_identity_marker:
                # The active heraldry page is character-session state.  A
                # cached 03 1D from the previous character must never select
                # a page in the next character's 07 03 archive while waiting
                # for its own authoritative acknowledgement.
                self.current_login_identity_marker = decoded.actor_marker
                self.pending_glyph_page_request = None
                self.current_login_glyph_page = None
                self.current_login_glyph_actor_id = None
                self.active_glyph_page_by_actor.clear()
                self.self_login_profile_packet = None
            recognized = [
                (self.inspect_job_codes[code][1], self.inspect_job_codes[code][0])
                for code in decoded.job_class_codes
                if code in self.inspect_job_codes
            ]
            if not recognized:
                continue
            # Job index 1 is the second job, 2 the first job and 3 the base
            # class.  The smallest index is therefore the most specific row.
            _job_index, job = min(recognized, key=lambda row: row[0])
            self.current_login_job = job
            self.current_login_job_codes = decoded.job_class_codes
            return
    def _observe_tcp_player_spawn_identity(self, packet: PacketRecord) -> None:
        """Bind profile/runtime IDs to the spawn row's real name and job."""

        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_player_spawn_identity_payload(payload)
            if decoded is None:
                continue
            name = decoded.character_name.strip()
            if not name:
                continue
            job_row = self.inspect_job_codes.get(decoded.job_class_code)
            if job_row is not None:
                job_name, job_index = job_row
                matching_profiles = {
                    profile_key
                    for profile_key, profile_job in self.profile_jobs.items()
                    if profile_job == job_name
                }
                for actor_id in (
                    decoded.profile_id,
                    decoded.runtime_actor_id,
                ):
                    # 03 03 is authoritative session evidence and arrives
                    # before inspect/combat-derived fallbacks. Bind both IDs so
                    # an immediate 03 15 request already has the profession.
                    self.actor_jobs[actor_id] = (job_name, job_index)
                    if matching_profiles:
                        self.actor_skill_profiles[actor_id] = set(
                            matching_profiles
                        )
            changed = bool(
                self.known_actor_names.get(decoded.profile_id) != name
                or self.known_actor_names.get(decoded.runtime_actor_id) != name
            )
            self.known_actor_names[decoded.profile_id] = name
            self.known_actor_names[decoded.runtime_actor_id] = name
            if changed:
                self.player_spawn_identities.append(decoded)

    def drain_player_spawn_identities(
        self,
    ) -> tuple[DecodedTcpPlayerSpawnIdentity, ...]:
        """Return newly proven profile/runtime/name bindings exactly once."""

        identities = tuple(self.player_spawn_identities)
        self.player_spawn_identities.clear()
        return identities

    def _tcp_party_roster_name_for_actor(
        self,
        actor_id: int,
        timestamp: float,
    ) -> str:
        """Return a 22 10 name only when the job join is one-to-one.

        ``22 10`` has no actor ID.  A single remaining name may therefore be
        assigned only to a single remaining party actor of the same exact job.
        This also lets an already proven local/03 03 name disambiguate a pair
        of duplicate jobs without ever relying on packet or party ordering.
        """

        job = self.actor_jobs.get(actor_id, ("", 99))[0]
        if not job:
            return ""
        cutoff = timestamp - 180.0
        names = {
            name
            for (candidate_job, name), seen_at in self.tcp_party_roster_last_seen.items()
            if candidate_job == job and seen_at >= cutoff
        }
        if not names:
            return ""
        actors = {
            candidate_id
            for candidate_id in self.party_member_last_emit
            if self.actor_jobs.get(candidate_id, ("", 99))[0] == job
        }
        actors.add(actor_id)

        def proven_name(candidate_id: int) -> str:
            if candidate_id in self.local_actor_ids and self.local_name:
                return self.local_name
            return str(self.known_actor_names.get(candidate_id, "") or "").strip()

        claimed = {
            name
            for candidate_id in actors
            if (name := proven_name(candidate_id)) in names
        }
        unresolved = [
            candidate_id for candidate_id in actors if not proven_name(candidate_id)
        ]
        remaining = names - claimed
        if len(unresolved) == 1 and unresolved[0] == actor_id and len(remaining) == 1:
            return next(iter(remaining))
        return ""

    def _observe_tcp_party_roster_identity(
        self,
        packet: PacketRecord,
    ) -> list[PartyMemberEvent]:
        """Collect S2C 22 10 names and upgrade uniquely matched party rows."""

        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return []
        observed = False
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_party_roster_identity_payload(
                payload,
                connection_context=packet,
            )
            if decoded is None:
                continue
            job_row = self.inspect_job_codes.get(decoded.job_class_code)
            if job_row is None:
                continue
            job = job_row[0]
            name = decoded.character_name.strip()
            self.tcp_party_roster_names_by_job.setdefault(job, set()).add(name)
            self.tcp_party_roster_last_seen[(job, name)] = packet.timestamp_seconds
            observed = True
        if not observed:
            return []

        events: list[PartyMemberEvent] = []
        for actor_id in tuple(self.party_member_last_emit):
            name = self._tcp_party_roster_name_for_actor(
                actor_id,
                packet.timestamp_seconds,
            )
            if not name:
                continue
            self.known_actor_names[actor_id] = name
            event = self._observe_party_member(
                actor_id,
                packet.timestamp_seconds,
                "s2c-tcp-party-roster-2210-108-v576",
            )
            if event is not None:
                events.append(event)
        return events

    def _monster_max_hp(self, actor_id: int | None) -> int:
        if actor_id is None:
            return 0
        template = self.monster_entities.get(actor_id)
        return max(0, int(template.hp)) if template is not None else 0
    def _observe_party_member(
        self,
        actor_id: int,
        timestamp: float,
        source_layout: str,
    ) -> PartyMemberEvent | None:
        """Publish passive identity evidence without inventing roster fields."""
        actor_id = self._canonical_player_actor_id(actor_id)
        if (
            actor_id <= 0
            or actor_id in self.monster_entities
            or actor_id in self.summon_owners
            or actor_id <= _SUMMON_ACTOR_ID_MAX
        ):
            return None
        roster_name = self._tcp_party_roster_name_for_actor(actor_id, timestamp)
        if roster_name:
            self.known_actor_names[actor_id] = roster_name
        name, job = self._actor_display_context(actor_id)
        if not job and name.startswith("角色 0x"):
            return None
        previous = self.party_member_last_emit.get(actor_id)
        if previous is not None:
            old_name, old_job, old_timestamp = previous
            if old_name == name and old_job == job:
                return None
        self.party_member_last_emit[actor_id] = (name, job, timestamp)
        confidence = 0.95 if name and not name.startswith("角色 0x") else 0.65
        if not job:
            confidence = min(confidence, 0.35)
        origin = self.first_timestamp or timestamp
        return PartyMemberEvent(
            timestamp_ms=max(0, round((timestamp - origin) * 1000)),
            actor_id=f"0x{actor_id:08X}",
            name=name,
            job=job,
            source_layout=source_layout,
            confidence=confidence,
        )


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'V576ActorStateMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
