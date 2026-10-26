from __future__ import annotations

from functools import lru_cache

from ..damage_attribution import should_prefer_damage_object_skill, should_replace_attribution
from ..damage_wire_evidence import damage_wire_metadata
from ..protocol import decode_v576_embedded_direct41_damage_events
from ..aura_state import AURA_SKILL_IDS
from ..nest_probe import relaxed_skill_candidates

class V576StatusDecoderMixin:
    @staticmethod
    def _physical_direct_record_key(decoded):
        """Identity of this exact record, not just equal source/target/amount."""
        offset = int(getattr(decoded, "record_offset", 0) or 0)
        group = getattr(decoded, "damage_result_group_id", None)
        obj = getattr(decoded, "damage_object_id", None)
        if offset > 0 and "direct41" in str(getattr(decoded, "layout", "")):
            return (offset, decoded.source_actor_id, decoded.target_actor_id,
                    decoded.damage, bool(decoded.critical), None, None)
        if offset <= 0 or group is None or obj is None:
            return None
        return (offset, decoded.source_actor_id, decoded.target_actor_id,
                decoded.damage, bool(decoded.critical), int(group), int(obj))

    @classmethod
    def _deduplicate_supplemental_physical_records(cls, rows, primary=None):
        """Return one decoded row per physical record in this one message."""
        seen: set[tuple] = set()
        primary_key = cls._physical_direct_record_key(primary) if primary else None
        if primary_key is not None:
            seen.add(primary_key)
        unique = []
        for row in rows:
            key = cls._physical_direct_record_key(row)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            unique.append(row)
        return tuple(unique)

    @staticmethod
    @lru_cache(maxsize=4096)
    def _packet_percent(parameters: str) -> float | None:
        values: list[tuple[str, float]] = []
        for token in re.split(r"[;,]", parameters):
            token = token.strip()
            try:
                value = float(token)
            except ValueError:
                continue
            values.append((token, value))
        if not values:
            return None
        negative = [value for _token, value in values if -1 <= value < 0]
        if negative:
            return round(abs(negative[-1]) * 100, 6)
        # A lone fractional ratio is an unambiguous positive percentage. More
        # complex parameter vectors (for example damage formulas containing
        # 1.04/1.14) remain raw instead of being guessed as a Boss amplification.
        fractional = [
            value
            for token, value in values
            if "." in token and 0 < value < 1
        ]
        ratio_terms = [value for _token, value in values if 1 < abs(value) < 10]
        if len(fractional) == 1 and not ratio_terms:
            return round(fractional[0] * 100, 6)
        return None
    def _status_source_context(
        self, source_actor_id: int, skill_id: int, timestamp: float
    ) -> tuple[int, int]:
        if skill_id in _AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS:
            resolved_skill_id = 6316
        else:
            resolved_skill_id = (
                skill_id
                if skill_id in self.status_effect_catalog
                else _STATUS_SKILL_ALIASES.get(skill_id, skill_id)
            )
        periodic_rules = self.periodic_damage_catalog.get(skill_id, ())
        if periodic_rules:
            resolved_skill_id = periodic_rules[0].source_skill_id
        resolved_skill_id = int(
            _canonical_live_skill_id(resolved_skill_id) or resolved_skill_id
        )
        if self._legacy_entity_owner_blocked(source_actor_id, timestamp):
            return source_actor_id, resolved_skill_id
        explicit_owner = self._current_entity_owner_id(source_actor_id, timestamp)
        if explicit_owner is not None:
            # A status presentation candidate must never steal an entity whose
            # actual owner is already proven. This does not certify a skill.
            return explicit_owner, resolved_skill_id
        if skill_id in _TEAM_BUFF_INTERNAL_STATUS_SKILL_IDS:
            owner_actor_id = self._recent_caster_for_damage_skill(
                resolved_skill_id,
                timestamp,
                max_age=15.0,
            )
            if owner_actor_id is not None:
                return owner_actor_id, resolved_skill_id
        if source_actor_id in self.skill_actor_ids:
            return source_actor_id, resolved_skill_id
        owner_actor_id = self._recent_caster_for_damage_skill(
            resolved_skill_id,
            timestamp,
            max_age=(
                120.0
                if resolved_skill_id in self.summon_creation_skill_ids
                else 15.0
            ),
        )
        if owner_actor_id is not None:
            if resolved_skill_id in self.summon_creation_skill_ids:
                self.summon_owners[source_actor_id] = (
                    owner_actor_id,
                    resolved_skill_id,
                )
            return owner_actor_id, resolved_skill_id
        summon_owner = self.summon_owners.get(source_actor_id)
        if (
            summon_owner is not None
            and summon_owner[1] == resolved_skill_id
        ):
            return summon_owner
        candidates = [
            item
            for item in self.recent_skill_casters.get(resolved_skill_id, [])
            if 0 <= timestamp - item[1] <= 15.0
        ]
        owner_actor_id = (
            max(candidates, key=lambda item: item[1])[0]
            if candidates
            else source_actor_id
        )
        if (
            owner_actor_id != source_actor_id
            and resolved_skill_id in self.summon_creation_skill_ids
        ):
            self.summon_owners[source_actor_id] = (
                owner_actor_id,
                resolved_skill_id,
            )
        return owner_actor_id, resolved_skill_id
    def _player_buff_source_context(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        resolved_skill_id: int,
        effect_code: str,
        duration_ms: int,
        packet_value: float | None,
        timestamp: float,
    ) -> tuple[int, int]:
        """Repair transport-specific player-buff source attribution.

        Some team buffs (currently confirmed for Ambush and Sacrificial Crow)
        emit recipient-local mirrors where source equals target. Rebind those
        rows to the recent real caster so one cast cannot masquerade as a cast
        by every party member.

        The v576 client reuses short summon actor IDs.  A Cure Relic refresh
        can therefore inherit an older Lightning Relic or Grudge Formation
        owner mapping even though its packet signature is unambiguous.  Bind
        that refresh to the newest Cure Relic cast while the relic can still
        be producing its periodic team buff.
        """
        mirror_signature = _TEAM_BUFF_RECIPIENT_MIRROR_SIGNATURES.get(
            resolved_skill_id
        )
        mirror_values: tuple[float, ...] = ()
        mirror_matches = False
        if mirror_signature is not None:
            mirror_effect_code, mirror_duration_ms, mirror_values = mirror_signature
            mirror_matches = (
                source_actor_id == target_actor_id
                and effect_code == mirror_effect_code
                and duration_ms == mirror_duration_ms
                and (
                    not mirror_values
                    or (
                        packet_value is not None
                        and any(
                            abs(packet_value - value) <= 1e-6
                            for value in mirror_values
                        )
                    )
                )
            )
        if mirror_matches:
            # Prefer the action adjacent to this fan-out.  When an action
            # packet is dropped, retain the most recent packet-proven caster
            # for the actual Buff lifetime instead of falling back to the
            # recipient.  That fallback was the reason Raid/Assault appeared
            # under every party member's profession in the live Buff table.
            lifetime_seconds = max(
                _TEAM_BUFF_RECIPIENT_MIRROR_SOURCE_WINDOW_SECONDS,
                max(0, duration_ms) / 1000.0,
            )
            candidates = [
                item
                for item in self.recent_skill_casters.get(
                    resolved_skill_id, ()
                )
                if 0.0 <= timestamp - item[1] <= lifetime_seconds
            ]
            remembered = self.active_recipient_mirror_buff_sources.get(
                resolved_skill_id
            )
            if remembered is not None and timestamp > remembered[2]:
                self.active_recipient_mirror_buff_sources.pop(
                    resolved_skill_id, None
                )
                remembered = None

            newest_candidate = (
                max(candidates, key=lambda item: item[1])
                if candidates
                else None
            )
            if newest_candidate is not None and (
                remembered is None
                or newest_candidate[1] >= remembered[1]
            ):
                owner_actor_id, evidence_at = newest_candidate
                self.active_recipient_mirror_buff_sources[
                    resolved_skill_id
                ] = (
                    owner_actor_id,
                    evidence_at,
                    timestamp + lifetime_seconds,
                )
                return owner_actor_id, resolved_skill_id
            if remembered is not None:
                owner_actor_id, evidence_at, _expires_at = remembered
                self.active_recipient_mirror_buff_sources[
                    resolved_skill_id
                ] = (
                    owner_actor_id,
                    evidence_at,
                    timestamp + lifetime_seconds,
                )
                return owner_actor_id, resolved_skill_id

        if (
            effect_code != "134700"
            or duration_ms != 18_000
            or packet_value is None
            or not any(
                abs(packet_value - value) <= 1e-6
                for value in _CURE_RELIC_LIGHT_BUFF_VALUES
            )
        ):
            return source_actor_id, resolved_skill_id
        candidates = [
            item
            for item in self.recent_skill_casters.get(_CURE_RELIC_SKILL_ID, ())
            if 0.0
            <= timestamp - item[1]
            <= _CURE_RELIC_REFRESH_SOURCE_WINDOW_SECONDS
        ]
        if not candidates:
            return source_actor_id, resolved_skill_id
        owner_actor_id, _cast_time = max(candidates, key=lambda item: item[1])
        return owner_actor_id, _CURE_RELIC_SKILL_ID

    def _azuna_breath_source_entity(
        self,
        raw_source_actor_id: int,
        owner_actor_id: int,
        timestamp: float,
    ) -> int | None:
        """Resolve the live 33603 generation behind one 2619xx refresh."""

        candidates: list[int] = []
        for entity_id, template in self.monster_entities.items():
            if int(getattr(template, "template_id", 0) or 0) != 33603:
                continue
            if entity_id in self.summon_lethal_actor_ids:
                continue
            owner_skill = self._cached_summon_owner(entity_id, timestamp)
            if owner_skill == (owner_actor_id, 6316):
                candidates.append(int(entity_id))
        if raw_source_actor_id in candidates:
            return raw_source_actor_id
        return candidates[0] if len(candidates) == 1 else None

    def _azuna_breath_refresh_events(
        self,
        *,
        owner_actor_id: int,
        timestamp: float,
        value: float | None,
        effect_code: str,
        parameters: str,
        prebattle_inherited: bool = False,
    ) -> list[BossStatusEvent]:
        origin = self.first_timestamp or timestamp
        timestamp_ms = max(0, round((timestamp - origin) * 1000.0))
        source_name, source_job = self._actor_display_context(owner_actor_id)
        targets = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        ) - set(self.monster_entities)
        events: list[BossStatusEvent] = []
        for target_actor_id in targets:
            target_name, target_job = self._actor_display_context(
                target_actor_id
            )
            events.append(
                BossStatusEvent(
                    timestamp_ms=timestamp_ms,
                    target_id=f"0x{target_actor_id:08X}",
                    source_id=f"0x{owner_actor_id:08X}",
                    source_name=source_name,
                    job=source_job,
                    skill_id="6316",
                    skill_name="阿兹那之息",
                    effect_key=(
                        f"角色增益:6316:{effect_code}:{parameters}"
                    ),
                    effect_label="阿兹那之息增益",
                    value=value,
                    duration_ms=_AZUNA_BREATH_DURATION_MS,
                    status_kind="团队增益",
                    effect_code=effect_code,
                    parameters=parameters,
                    target_name=target_name,
                    target_job=target_job,
                    target_scope="player",
                    permanent=False,
                    prebattle_inherited=prebattle_inherited,
                    value_source=("packet" if value is not None else "unavailable"),
                    duration_source="client_ai+dnt",
                )
            )
        return events

    def _observe_azuna_breath_status(
        self,
        *,
        raw_source_actor_id: int,
        owner_actor_id: int,
        raw_skill_id: int,
        effect_code: str,
        parameters: str,
        value: float | None,
        timestamp: float,
    ) -> list[BossStatusEvent]:
        """Start the reviewed 5s refresh cycle from a real status response."""

        entity_id = self._azuna_breath_source_entity(
            raw_source_actor_id,
            owner_actor_id,
            timestamp,
        )
        if entity_id is None:
            return []
        self.active_azuna_breath_refresh_sources[owner_actor_id] = {
            "entity_id": entity_id,
            "owner_id": owner_actor_id,
            "raw_skill_id": raw_skill_id,
            "effect_code": effect_code,
            "parameters": parameters,
            "value": value,
            "next_refresh": (
                timestamp + _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            ),
        }
        # The packet may expose only the local recipient even though the AI
        # uses target=party and range=10000. Fan out this exact application to
        # the currently known party; duplicate packet rows are removed later
        # by the common physical-status deduplicator.
        return self._azuna_breath_refresh_events(
            owner_actor_id=owner_actor_id,
            timestamp=timestamp,
            value=value,
            effect_code=effect_code,
            parameters=parameters,
        )

    def _restore_inherited_azuna_breath_refresh(
        self,
        events: list[BossStatusEvent],
        boundary_timestamp: float,
    ) -> int:
        """Continue a packet-proven prebattle Breath refresh cycle.

        The 33603 companion is persistent, but the player Buff is not: its
        actual state is a 30-second application refreshed by the companion
        every five seconds. A capture that starts after the companion was
        created therefore needs both pieces of evidence. The inherited Buff
        alone cannot manufacture a companion, and the companion alone cannot
        manufacture an already-active Buff.
        """

        restored = 0
        try:
            boundary = float(boundary_timestamp)
        except (TypeError, ValueError):
            return 0
        if boundary <= 0.0:
            return 0
        for event in events:
            try:
                skill_id = int(event.skill_id)
                owner_actor_id = int(event.source_id, 0)
            except (TypeError, ValueError):
                continue
            if (
                skill_id != 6316
                or event.operation != "apply"
                or event.permanent
                or int(event.duration_ms or 0) <= 0
                or owner_actor_id <= _SUMMON_ACTOR_ID_MAX
                or owner_actor_id in self.active_azuna_breath_refresh_sources
            ):
                continue
            entity_id = self._azuna_breath_source_entity(
                0,
                owner_actor_id,
                boundary,
            )
            if entity_id is None:
                continue
            remaining_seconds = min(
                _AZUNA_BREATH_DURATION_MS / 1000.0,
                max(0.001, int(event.duration_ms) / 1000.0),
            )
            age_seconds = max(
                0.0,
                _AZUNA_BREATH_DURATION_MS / 1000.0 - remaining_seconds,
            )
            phase = age_seconds % _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            delay = _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS - phase
            if delay <= 1e-6:
                delay = _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            self.active_azuna_breath_refresh_sources[owner_actor_id] = {
                "entity_id": entity_id,
                "owner_id": owner_actor_id,
                "raw_skill_id": 261901,
                "effect_code": str(event.effect_code or "134700"),
                "parameters": str(event.parameters or "0.07"),
                "value": event.value,
                "next_refresh": boundary + delay,
                "prebattle_inherited": True,
            }
            restored += 1
        return restored

    def _seed_azuna_breath_companion_refresh(
        self,
        boundary_timestamp: float,
    ) -> list[BossStatusEvent]:
        """Start Breath from a proven persistent 33603 companion at entry.

        Unlike the attacking 33601 Pact companion, template 33603 has one
        fixed reviewed AI: every five seconds it applies the 30-second +7%
        light/dark party Buff.  Its exact current generation plus owner is
        therefore sufficient at a capture boundary even when the preceding
        2619xx refresh packet predates ACT startup.
        """

        try:
            boundary = float(boundary_timestamp)
        except (TypeError, ValueError):
            return []
        if boundary <= 0.0:
            return []
        events: list[BossStatusEvent] = []
        for entity_id, template in tuple(self.monster_entities.items()):
            if int(getattr(template, "template_id", 0) or 0) != 33603:
                continue
            owner_skill = self._cached_summon_owner(entity_id, boundary)
            if owner_skill is None or int(owner_skill[1]) != 6316:
                continue
            owner_actor_id = self._canonical_player_actor_id(owner_skill[0])
            if owner_actor_id in self.active_azuna_breath_refresh_sources:
                continue
            self.active_azuna_breath_refresh_sources[owner_actor_id] = {
                "entity_id": int(entity_id),
                "owner_id": owner_actor_id,
                "raw_skill_id": 261901,
                "effect_code": "134700",
                "parameters": "0.07",
                "value": 7.0,
                "next_refresh": boundary + _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS,
                "prebattle_inherited": True,
            }
            events.extend(
                self._azuna_breath_refresh_events(
                    owner_actor_id=owner_actor_id,
                    timestamp=boundary,
                    value=7.0,
                    effect_code="134700",
                    parameters="0.07",
                    prebattle_inherited=True,
                )
            )
        return events

    def _azuna_breath_periodic_refresh_events(
        self, timestamp: float
    ) -> list[BossStatusEvent]:
        """Emit finite 30s applications while a proven 33603 life persists."""

        events: list[BossStatusEvent] = []
        for owner_actor_id, state in tuple(
            self.active_azuna_breath_refresh_sources.items()
        ):
            entity_id = int(state.get("entity_id") or 0)
            template = self.monster_entities.get(entity_id)
            valid = bool(
                template is not None
                and int(getattr(template, "template_id", 0) or 0) == 33603
                and entity_id not in self.summon_lethal_actor_ids
                and self._cached_summon_owner(entity_id, timestamp)
                == (owner_actor_id, 6316)
            )
            if not valid:
                self.active_azuna_breath_refresh_sources.pop(
                    owner_actor_id, None
                )
                continue
            next_refresh = float(state.get("next_refresh") or 0.0)
            if timestamp + 1e-9 < next_refresh:
                continue
            elapsed = max(0.0, timestamp - next_refresh)
            skipped = int(
                elapsed // _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            )
            refresh_at = (
                next_refresh
                + skipped * _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            )
            state["next_refresh"] = (
                refresh_at + _AZUNA_BREATH_REFRESH_INTERVAL_SECONDS
            )
            events.extend(
                self._azuna_breath_refresh_events(
                    owner_actor_id=owner_actor_id,
                    timestamp=refresh_at,
                    value=state.get("value"),
                    effect_code=str(state.get("effect_code") or "134700"),
                    parameters=str(state.get("parameters") or ""),
                )
            )
        return events
    def _status_backed_mechanic_cast_event(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        skill_id: int,
        timestamp: float,
        timestamp_ms: int,
    ) -> SkillCastEvent | None:
        """Promote the two useful Mechanical Mode self states to casts."""
        definition = _STATUS_BACKED_MECHANIC_SKILLS.get(skill_id)
        if definition is None or source_actor_id != target_actor_id:
            return None
        canonical_skill_id, skill_name, minimum_interval = definition
        cast_key = (source_actor_id, canonical_skill_id)
        previous = self.recent_status_backed_casts.get(
            cast_key, float("-inf")
        )
        if timestamp - previous <= minimum_interval:
            return None

        self.recent_status_backed_casts[cast_key] = timestamp
        previous_emitted = self.recent_emitted_casts.get(
            cast_key, float("-inf")
        )
        self._update_actor_job(source_actor_id, canonical_skill_id)
        self._observe_player_skill_activity(
            source_actor_id,
            canonical_skill_id,
            timestamp,
        )
        self.skill_actor_ids.add(source_actor_id)
        self.latest_actor_skills[source_actor_id] = (canonical_skill_id, timestamp)
        actor_casts = self.recent_actor_casts.setdefault(source_actor_id, [])
        if not actor_casts or not (
            actor_casts[-1][0] == canonical_skill_id
            and abs(timestamp - actor_casts[-1][1]) <= 0.12
        ):
            actor_casts.append((canonical_skill_id, timestamp))
        if len(actor_casts) > 48:
            cutoff = timestamp - 15.0
            self.recent_actor_casts[source_actor_id] = [
                row for row in actor_casts if row[1] >= cutoff
            ]
        casters = self.recent_skill_casters.setdefault(canonical_skill_id, [])
        if not casters or not (
            casters[-1][0] == source_actor_id
            and abs(timestamp - casters[-1][1]) <= 0.12
        ):
            casters.append((source_actor_id, timestamp))
        if len(casters) > 32:
            cutoff = timestamp - 120.0
            self.recent_skill_casters[canonical_skill_id] = [
                row for row in casters if row[1] >= cutoff
            ]
        damage_casts = self.recent_damage_actor_casts.setdefault(
            source_actor_id, []
        )
        if not damage_casts or not (
            damage_casts[-1][0] == canonical_skill_id
            and abs(timestamp - damage_casts[-1][1]) <= 0.12
        ):
            damage_casts.append((canonical_skill_id, timestamp))
        if len(damage_casts) > 48:
            cutoff = timestamp - 15.0
            self.recent_damage_actor_casts[source_actor_id] = [
                row for row in damage_casts if row[1] >= cutoff
            ]

        # A compact/direct action and its self-status marker describe the same
        # button press. Keep the status as damage context, but show one block
        # on the timeline.
        if abs(timestamp - previous_emitted) <= 0.2:
            return None
        self.recent_emitted_casts[cast_key] = timestamp

        source_name, job = self._actor_display_context(source_actor_id)
        return SkillCastEvent(
            timestamp_ms=timestamp_ms,
            source_id=f"0x{source_actor_id:08X}",
            source_name=source_name,
            job=job,
            skill_id=str(canonical_skill_id),
            skill_name=skill_name,
            packet_kind="status_skill_action",
        )
    def _permanent_player_buff_event(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        skill_id: int,
        timestamp_ms: int,
        operation: str = "apply",
        prebattle_inherited: bool = False,
    ) -> BossStatusEvent:
        skill_name, team_buff = _PERMANENT_PLAYER_BUFF_SKILLS[skill_id]
        source_name, source_job = self._actor_display_context(source_actor_id)
        target_name, target_job = self._actor_display_context(target_actor_id)
        return BossStatusEvent(
            timestamp_ms=timestamp_ms,
            target_id=f"0x{target_actor_id:08X}",
            source_id=f"0x{source_actor_id:08X}",
            source_name=source_name,
            job=source_job,
            skill_id=str(skill_id),
            skill_name=skill_name,
            effect_key=f"永久角色增益:{skill_id}",
            effect_label=f"{skill_name}增益",
            value=None,
            duration_ms=0,
            status_kind="团队增益" if team_buff else "个人增益",
            effect_code="cast_inferred_permanent",
            parameters="永久",
            operation=operation,
            target_name=target_name,
            target_job=target_job,
            target_scope="player",
            permanent=operation != "remove",
            prebattle_inherited=prebattle_inherited,
        )
    def _activate_permanent_player_buff(
        self,
        *,
        source_actor_id: int,
        skill_id: int,
        timestamp_ms: int,
        prebattle_inherited: bool = False,
    ) -> list[BossStatusEvent]:
        # Some permanent team effects are maintained by a persistent
        # companion rather than by the button that created the companion.
        # Translate only an observed, accepted cast; job presence alone must
        # never manufacture an active Buff.
        skill_id = _PERMANENT_PLAYER_BUFF_TRIGGER_SKILLS.get(
            skill_id,
            skill_id,
        )
        if skill_id not in _PERMANENT_PLAYER_BUFF_SKILLS:
            return []
        if skill_id in AURA_SKILL_IDS:
            # A cast proves activation, not who is within the aura radius.
            # Recipient applications and exact instance ends are authoritative.
            return []

        events: list[BossStatusEvent] = []
        if skill_id in _MUTUALLY_EXCLUSIVE_AURA_SKILL_IDS:
            replaced_keys = [
                key
                for key in self.active_permanent_player_buffs
                if key[0] == source_actor_id
                and key[1] in _MUTUALLY_EXCLUSIVE_AURA_SKILL_IDS
                and key[1] != skill_id
            ]
            for old_source_id, old_skill_id in replaced_keys:
                old_targets = [
                    target_id
                    for source_id, active_skill_id, target_id
                    in self.permanent_player_buff_targets
                    if source_id == old_source_id
                    and active_skill_id == old_skill_id
                ]
                for target_id in old_targets:
                    events.append(
                        self._permanent_player_buff_event(
                            source_actor_id=old_source_id,
                            target_actor_id=target_id,
                            skill_id=old_skill_id,
                            timestamp_ms=timestamp_ms,
                            operation="remove",
                        )
                    )
                    self.permanent_player_buff_targets.discard(
                        (old_source_id, old_skill_id, target_id)
                    )
                self.active_permanent_player_buffs.pop(
                    (old_source_id, old_skill_id), None
                )
                self.prebattle_permanent_player_buffs.discard(
                    (old_source_id, old_skill_id)
                )

        source_name, source_job = self._actor_display_context(source_actor_id)
        self.active_permanent_player_buffs[(source_actor_id, skill_id)] = (
            timestamp_ms,
            source_name,
            source_job,
        )
        if prebattle_inherited:
            self.prebattle_permanent_player_buffs.add(
                (source_actor_id, skill_id)
            )
        else:
            self.prebattle_permanent_player_buffs.discard(
                (source_actor_id, skill_id)
            )
        events.extend(
            self._backfill_permanent_player_buffs(
                force=True,
            )
        )
        return events
    def _backfill_permanent_player_buffs(
        self,
        *,
        force: bool = False,
    ) -> list[BossStatusEvent]:
        if not self.active_permanent_player_buffs:
            return []
        player_actors = (
            self.skill_actor_ids | self.local_actor_ids
        ) - self.monster_entities.keys()
        if not force and player_actors == self.permanent_player_buff_known_actors:
            return []
        self.permanent_player_buff_known_actors = set(player_actors)

        events: list[BossStatusEvent] = []
        for (
            source_actor_id,
            skill_id,
        ), (
            activated_ms,
            _source_name,
            _source_job,
        ) in self.active_permanent_player_buffs.items():
            if skill_id in AURA_SKILL_IDS:
                continue
            _skill_name, team_buff = _PERMANENT_PLAYER_BUFF_SKILLS[skill_id]
            targets = player_actors if team_buff else {source_actor_id}
            for target_actor_id in targets:
                target_key = (source_actor_id, skill_id, target_actor_id)
                if target_key in self.permanent_player_buff_targets:
                    continue
                self.permanent_player_buff_targets.add(target_key)
                events.append(
                    self._permanent_player_buff_event(
                        source_actor_id=source_actor_id,
                        target_actor_id=target_actor_id,
                        skill_id=skill_id,
                        timestamp_ms=activated_ms,
                        prebattle_inherited=(
                            (source_actor_id, skill_id)
                            in self.prebattle_permanent_player_buffs
                        ),
                    )
                )
        return events
    def _supplemental_status_damage_events(
        self,
        packet: PacketRecord,
        primary_decoded: object | None = None,
    ) -> list[CombatEvent]:
        decoded_events = decode_v576_status_damage_events(
            packet.payload,
            packet.inferred_direction(),
            include_swordmaster_result_variants=True,
        )
        if not decoded_events:
            return []

        primary_fingerprint = None
        if (
            primary_decoded is not None
            and getattr(primary_decoded, "kind", None) == "damage_hit"
        ):
            primary_fingerprint = (
                getattr(primary_decoded, "source_actor_id", None),
                getattr(primary_decoded, "target_actor_id", None),
                getattr(primary_decoded, "damage", None),
            )

        player_actors = (
            self.skill_actor_ids | self.local_actor_ids
        ) - self.monster_entities.keys()
        origin = self.first_timestamp or packet.timestamp_seconds
        events: list[CombatEvent] = []
        primary_skipped = False
        for decoded in decoded_events:
            if (
                decoded.source_actor_id is None
                or decoded.target_actor_id is None
                or decoded.damage is None
            ):
                continue
            fingerprint = (
                decoded.source_actor_id,
                decoded.target_actor_id,
                decoded.damage,
            )
            if not primary_skipped and fingerprint == primary_fingerprint:
                primary_skipped = True
                continue
            if (
                decoded.target_actor_id == decoded.source_actor_id
                or decoded.target_actor_id in player_actors
            ):
                continue
            if "swordmaster-result-variant" in decoded.layout:
                target_template = self.monster_entities.get(
                    decoded.target_actor_id
                )
                # Peda emits Punishing Attack's second result with the 0100
                # and 0104 wire variants. Earlier Black Dragon targets also
                # use those bytes for non-ledger snapshots, so only the
                # confirmed Peda phase templates may enter the factual total.
                if (
                    target_template is None
                    or target_template.template_id
                    not in {4_181_001, 4_181_009}
                ):
                    continue

            summon_owner = self._skill_object_owner(
                decoded.source_actor_id,
                decoded.target_actor_id,
                None,
                packet.timestamp_seconds,
            ) or self._cached_summon_owner(
                decoded.source_actor_id, packet.timestamp_seconds
            )
            source_actor_id = (
                summon_owner[0]
                if summon_owner is not None
                else decoded.source_actor_id
            )
            trigger: tuple[int, str] | None = None
            confirmed_periodic_kind = ""
            confirmed_periodic_label = ""
            if (
                decoded.skill_id is not None
                and (
                    decoded.skill_id in self.damage_skill_ids
                    # These two team procs are carried by authoritative
                    # HP-delta rows under their buff skill ids rather than an
                    # ordinary entry in skill_damage.  The packet tail is the
                    # exact damage identity; do not replace it with the most
                    # recent cast or periodic subtype.
                    or decoded.skill_id in {5118, 6219}
                )
            ):
                trigger = (decoded.skill_id, "status_damage_skill")
            elif summon_owner is not None:
                trigger = (summon_owner[1], "summon_owner")
            else:
                same_packet_skill_id = self._same_packet_damage_trigger(
                    decoded,
                    primary_decoded,
                )
                if (
                    same_packet_skill_id is not None
                    and decoded.periodic_kind
                    and self._skill_applies_periodic_kind(
                        same_packet_skill_id,
                        decoded.periodic_kind,
                    )
                ):
                    confirmed_periodic_kind = decoded.periodic_kind
                    confirmed_periodic_label = (
                        decoded.periodic_label or decoded.periodic_kind
                    )
                elif same_packet_skill_id is not None:
                    trigger = (
                        same_packet_skill_id,
                        "supplemental_same_packet",
                    )
                else:
                    trigger = self._supplemental_cast_damage_candidate(
                        source_actor_id,
                        packet.timestamp_seconds,
                        packet,
                        allow_recent_cast=not bool(decoded.periodic_kind),
                    )

            if trigger is not None:
                trigger_skill_id, attribution_method = trigger
                trigger_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(
                    trigger_skill_id, trigger_skill_id
                )
                skill_id = str(trigger_skill_id)
                skill_name = _skill_display_name(
                    self.skill_catalog, trigger_skill_id
                )
            elif confirmed_periodic_kind:
                skill_id = f"periodic:{confirmed_periodic_kind}"
                skill_name = confirmed_periodic_label
                attribution_method = "periodic_status_packet"
            else:
                state = self._periodic_damage_candidate(
                    source_actor_id,
                    decoded.target_actor_id,
                    packet.timestamp_seconds,
                    None,
                )
                # Subtype zero is shared by ordinary skill hits. Only an
                # observed, matching status application proves a periodic hit.
                is_confirmed_periodic = (
                    bool(decoded.periodic_kind)
                    and state is not None
                    and state.kind == decoded.periodic_kind
                )
                if is_confirmed_periodic:
                    skill_id = f"periodic:{state.kind}"
                    skill_name = state.label or state.kind
                    attribution_method = "periodic_status_packet"
                else:
                    skill_id = "supplemental:status_damage"
                    skill_name = "额外伤害"
                    attribution_method = "status_damage_packet"

            entity_evidence = {}
            if summon_owner is not None:
                entity_evidence = self._entity_damage_evidence(
                    decoded.source_actor_id,
                    packet.timestamp_seconds,
                    summon_owner[0],
                    summon_owner[1],
                )
                if entity_evidence and attribution_method == "summon_owner":
                    # Some autonomous summon attacks are transported in the
                    # status-damage family rather than a direct Hit record.
                    # The row is factual damage but not a direct-Hit sample.
                    # Exact current-generation owner + reviewed single-family
                    # template still proves its skill without a cast window.
                    attribution_method = "packet_entity_status_damage"

            if (
                summon_owner is None
                and 0 < decoded.source_actor_id <= _SUMMON_ACTOR_ID_MAX
                and decoded.source_actor_id != self.boss_actor_id
                and decoded.source_actor_id not in player_actors
            ):
                source_id = (
                    f"__unowned_entity__:0x{decoded.source_actor_id:08X}"
                )
                source_name, source_job = (
                    f"未归属召唤/实体 0x{decoded.source_actor_id:08X}",
                    "",
                )
            else:
                source_id = f"0x{source_actor_id:08X}"
                source_name, source_job = self._actor_display_context(source_actor_id)
            target_name, target_job = self._monster_display_context(
                decoded.target_actor_id
            )
            events.append(
                CombatEvent(
                    timestamp_ms=max(
                        0,
                        round((packet.timestamp_seconds - origin) * 1000),
                    ),
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    skill_id=skill_id,
                    skill_name=skill_name,
                    amount=decoded.damage,
                    critical=bool(decoded.critical),
                    target_id=self._monster_event_id(decoded.target_actor_id),
                    target_name=target_name,
                    attribution_method=attribution_method,
                    target_job=target_job,
                    target_max_hp=self._monster_max_hp(decoded.target_actor_id),
                    has_real_hit_evidence=_has_real_hit_evidence(
                        decoded.layout,
                        attribution_method,
                    ),
                    **damage_wire_metadata(decoded, packet_payload=packet.payload if str(packet.transport).upper() == "UDP" else None, direction=packet.inferred_direction()),
                    **entity_evidence,
                )
            )
        return events
    def _supplemental_embedded_direct_damage_events(
        self,
        packet: PacketRecord,
        primary_decoded: object | None = None,
        *,
        retry_corrected_primary: bool = False,
    ) -> list[CombatEvent]:
        """Emit bundled player and owned-object hits missing from the primary."""
        decoded_events = tuple(decode_v576_embedded_direct_damage_events(
            packet.payload,
            packet.inferred_direction(),
            self.damage_skill_ids,
        )) + tuple(decode_v576_embedded_direct41_damage_events(
            packet.payload, packet.inferred_direction(), self.damage_skill_ids,
        ))
        if not decoded_events:
            return []

        # The primary decoder and the signed-HP status decoder already own
        # their records. Consume exact physical identities before considering
        # the legacy value fingerprint, and de-duplicate the two supplemental
        # readers at that same layer. Two legitimate equal-damage records at
        # different offsets therefore remain distinct, while one 0075/0079 row
        # cannot be emitted once by the primary and again by a bundled reader.
        already_emitted: Counter[
            tuple[int | None, int | None, int | None]
        ] = Counter()
        if (
            primary_decoded is not None
            and getattr(primary_decoded, "kind", None) == "damage_hit"
            and not retry_corrected_primary
        ):
            primary_record_key = self._physical_direct_record_key(primary_decoded)
            if primary_record_key is None:
                # Legacy families lacking physical-record metadata retain
                # their old policy; no whole-message amount-only dedup change.
                already_emitted[
                    (getattr(primary_decoded, "source_actor_id", None),
                     getattr(primary_decoded, "target_actor_id", None),
                     getattr(primary_decoded, "damage", None))
                ] += 1
        decoded_events = self._deduplicate_supplemental_physical_records(
            decoded_events,
            None if retry_corrected_primary else primary_decoded,
        )
        for status_damage in decode_v576_status_damage_events(
            packet.payload,
            packet.inferred_direction(),
        ):
            already_emitted[
                (
                    status_damage.source_actor_id,
                    status_damage.target_actor_id,
                    status_damage.damage,
                )
            ] += 1

        player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
            | set(self.actor_jobs)
        ) - self.monster_entities.keys()
        origin = self.first_timestamp or packet.timestamp_seconds
        events: list[CombatEvent] = []
        for decoded in decoded_events:
            fingerprint = (
                decoded.source_actor_id,
                decoded.target_actor_id,
                decoded.damage,
            )
            if already_emitted[fingerprint] > 0:
                already_emitted[fingerprint] -= 1
                continue
            if (
                decoded.source_actor_id is None
                or decoded.target_actor_id is None
                or decoded.damage is None
                or decoded.target_actor_id == decoded.source_actor_id
                or (
                    decoded.target_actor_id not in self.monster_entities
                    and decoded.target_actor_id
                    not in self.confirmed_damage_target_ids
                    and decoded.target_actor_id
                    not in self.damage_target_counts
                )
            ):
                continue

            # Preserve original physical-hit admission. Recovering previously
            # skipped wire rows is a separate audit, not an attribution gain.
            summon_owner = self._skill_object_owner(
                decoded.source_actor_id,
                decoded.target_actor_id,
                None,
                packet.timestamp_seconds,
            ) or self._cached_summon_owner(decoded.source_actor_id, packet.timestamp_seconds)
            exact_entity_owner_id = self._current_entity_owner_id(
                decoded.source_actor_id,
                packet.timestamp_seconds,
            )
            known_unowned_entity = bool(
                decoded.source_actor_id in self.monster_entities
                and not self.monster_entities[decoded.source_actor_id].boss
            )
            unknown_short_source = bool(
                0 < decoded.source_actor_id <= _SUMMON_ACTOR_ID_MAX
                and decoded.source_actor_id != self.boss_actor_id
                and decoded.source_actor_id not in player_actors
            )
            direct_player_source = bool(
                decoded.source_actor_id > _SUMMON_ACTOR_ID_MAX
                and decoded.source_actor_id in player_actors
            )
            object_packet_context_skill_id = None
            damage_object_attribution = (
                self.damage_object_origins.attribute_direct37(
                    source_actor_id=decoded.source_actor_id,
                    timestamp=packet.timestamp_seconds,
                    layout=getattr(
                        decoded,
                        "layout",
                        "s2c-udp-hit-effect-direct37-bundled-v576",
                    ),
                    payload=packet.payload,
                    direction=packet.inferred_direction(),
                    damage=decoded.damage,
                    record_offset=getattr(decoded, "record_offset", 0),
                    packet_serial=getattr(
                        self, "_damage_object_packet_serial", 0
                    ),
                )
            )
            if (
                not direct_player_source
                and summon_owner is None
                and exact_entity_owner_id is None
                and not known_unowned_entity
                and not unknown_short_source
                and damage_object_attribution is None
            ):
                continue
            attribution_actor_id = (
                damage_object_attribution.owner_actor_id
                if damage_object_attribution is not None
                else summon_owner[0]
                if summon_owner is not None
                else exact_entity_owner_id
                if exact_entity_owner_id is not None
                else decoded.source_actor_id
            )

            if (
                damage_object_attribution is not None
                and self._action_conflicts_with_confirmed_job(
                    damage_object_attribution.owner_actor_id,
                    damage_object_attribution.skill_id,
                )
            ):
                self.damage_object_skill_conflicts += 1
                self.damage_object_origins.invalidate_origin(
                    source_actor_id=damage_object_attribution.owner_actor_id,
                    damage_object_id=damage_object_attribution.damage_object_id,
                    timestamp=packet.timestamp_seconds,
                    packet_serial=getattr(
                        self, "_damage_object_packet_serial", 0
                    ),
                    record_offset=max(
                        1, getattr(decoded, "record_offset", 0) + 1
                    ),
                )
                damage_object_attribution = None

            if (
                damage_object_attribution is not None
                and decoded.skill_id_exact
                and decoded.skill_id is not None
                and _canonical_live_skill_id(decoded.skill_id)
                != _canonical_live_skill_id(
                    damage_object_attribution.skill_id
                )
            ):
                self.damage_object_skill_conflicts += 1
                # Match the primary entrance: only a proven full-parent chain
                # with its root action may override a concurrent fixed-slot
                # context. Static templates and child-local actions cannot.
                if (
                    damage_object_attribution.parent_action_lineage_validated
                    or damage_object_attribution.exact_parent_child_skill_validated
                ):
                    object_packet_context_skill_id = int(decoded.skill_id)
                else:
                    self.damage_object_origins.invalidate_origin(
                        source_actor_id=damage_object_attribution.owner_actor_id,
                        damage_object_id=damage_object_attribution.damage_object_id,
                        timestamp=packet.timestamp_seconds,
                        packet_serial=getattr(
                            self, "_damage_object_packet_serial", 0
                        ),
                        record_offset=max(
                            1, getattr(decoded, "record_offset", 0) + 1
                        ),
                    )
                    damage_object_attribution = None
            if (
                damage_object_attribution is not None
                and decoded.skill_id_exact
                and decoded.skill_id is not None
                and damage_object_attribution.packet_skill_is_shared_child_context_only(
                    _canonical_live_skill_id(decoded.skill_id)
                )
            ):
                object_packet_context_skill_id = int(decoded.skill_id)
            if damage_object_attribution is not None and (
                object_packet_context_skill_id is not None
                or should_prefer_damage_object_skill(
                    packet_skill_exact=(
                        decoded.skill_id_exact and decoded.skill_id is not None
                    ),
                    object_mapping_packet_validated=(
                        damage_object_attribution.deterministically_validated
                    ),
                )
            ):
                trigger_skill_id = damage_object_attribution.skill_id
                attribution_method = (
                    "damage_object_projectile_link"
                    if damage_object_attribution.deterministically_validated
                    else "damage_object_projectile_link_provisional"
                )
            elif summon_owner is not None:
                trigger_skill_id = summon_owner[1]
                attribution_method = "supplemental_summon_owner"
            elif decoded.skill_id_exact and decoded.skill_id is not None:
                # Without the independently action-backed full-parent proof
                # above, retain the fixed field conservatively. Its presence
                # alone does not prove the UI skill that created this hit.
                trigger_skill_id = decoded.skill_id
                attribution_method = "packet_skill_id"
            else:
                trigger_skill_id = self._same_packet_damage_trigger(
                    decoded,
                    primary_decoded,
                )
                if trigger_skill_id is not None:
                    attribution_method = "supplemental_same_packet"
                elif (
                    exact_entity_owner_id is not None
                    or known_unowned_entity
                    or unknown_short_source
                ):
                    # Exact owner evidence admits the factual entity Hit but
                    # proves no player skill by itself. A known current entity
                    # without owner evidence is retained under its own wire
                    # identity, never dropped and never charged to a player.
                    # The generation finalizer may still identify a summon
                    # attack deterministically when complete evidence exists.
                    trigger_skill_id = _UNRESOLVED_SKILL_ID
                    attribution_method = "entity_owner_attack_unresolved"
                else:
                    trigger = self._supplemental_cast_damage_candidate(
                        attribution_actor_id,
                        packet.timestamp_seconds,
                        packet,
                        allow_recent_cast=True,
                    )
                    if trigger is not None:
                        trigger_skill_id, attribution_method = trigger
                    else:
                        trigger_skill_id = _UNRESOLVED_SKILL_ID
                        attribution_method = "bundled_direct_unresolved"
            trigger_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(
                trigger_skill_id,
                trigger_skill_id,
            )
            original_real_hit_evidence = _has_real_hit_evidence(decoded.layout, attribution_method)
            entity_evidence = {}
            if summon_owner is not None and attribution_method == "supplemental_summon_owner":
                entity_evidence = self._entity_damage_evidence(
                    decoded.source_actor_id, packet.timestamp_seconds, summon_owner[0], trigger_skill_id
                )
                if entity_evidence:
                    attribution_method = "packet_entity_template_owner"
            if summon_owner is not None and not entity_evidence:
                entity_evidence = self._entity_wire_context(decoded.source_actor_id)
            if (original_real_hit_evidence
                    and should_replace_attribution(attribution_method, "packet_entity_self_attack")):
                self_attack = self._entity_self_attack_evidence(packet, decoded)
                if self_attack is not None:
                    attribution_actor_id, trigger_skill_id, entity_evidence = self_attack
                    summon_owner = (attribution_actor_id, trigger_skill_id)
                    attribution_method = "packet_entity_self_attack"
                    damage_object_attribution = None
                    object_packet_context_skill_id = None
            if (
                unknown_short_source
                and exact_entity_owner_id is None
                and summon_owner is None
                and damage_object_attribution is None
            ):
                source_id = (
                    f"__unowned_entity__:0x{decoded.source_actor_id:08X}"
                )
                source_name, source_job = (
                    f"未归属召唤/实体 0x{decoded.source_actor_id:08X}",
                    "",
                )
            else:
                source_id = f"0x{attribution_actor_id:08X}"
                source_name, source_job = self._actor_display_context(
                    attribution_actor_id
                )
            target_name, target_job = self._monster_display_context(
                decoded.target_actor_id
            )
            event = self._finalize_wire_damage_owner(CombatEvent(
                    timestamp_ms=max(
                        0,
                        round(
                            (packet.timestamp_seconds - origin) * 1000
                        ),
                    ),
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    skill_id=str(trigger_skill_id),
                    skill_name=(
                        _UNRESOLVED_SKILL_NAME
                        if trigger_skill_id == _UNRESOLVED_SKILL_ID
                        else _skill_display_name(
                            self.skill_catalog,
                            trigger_skill_id,
                        )
                    ),
                    amount=decoded.damage,
                    critical=bool(decoded.critical),
                    target_id=self._monster_event_id(
                        decoded.target_actor_id
                    ),
                    target_name=target_name,
                    attribution_method=attribution_method,
                    target_job=target_job,
                    target_max_hp=self._monster_max_hp(
                        decoded.target_actor_id
                    ),
                    # Keep the concurrent wire context out of raw_skill_id,
                    # which is also consumed by proc/glyph inference.
                    raw_attribution_method=(
                        f"packet_skill_context:{object_packet_context_skill_id}"
                        if object_packet_context_skill_id is not None else ""
                    ),
                    cast_instance_id=(
                        (
                            f"damage-object:"
                            f"{damage_object_attribution.owner_actor_id:08X}:"
                            f"{damage_object_attribution.damage_object_id}:"
                            f"{damage_object_attribution.generation}"
                        )
                        if damage_object_attribution is not None
                        else ""
                    ),
                    attribution_confidence=(
                        damage_object_attribution.confidence
                        if damage_object_attribution is not None
                        else 0.0
                    ),
                    has_real_hit_evidence=original_real_hit_evidence,
                    **damage_wire_metadata(decoded, packet_payload=packet.payload if str(packet.transport).upper() == "UDP" else None, direction=packet.inferred_direction()),
                    **entity_evidence,
                ), packet)
            # Store only the final owner; the prior heuristic may be replaced
            # by a full owner record even when the exact attack is unknown.
            self._observe_damage_target(
                # Feed the factual wire actor back into the target ledger.
                # _observe_damage_target resolves it through the current
                # entity generation when exact ownership exists.  Parsing the
                # display source is unsafe because deliberately unowned
                # entities use an audit-only ``__unowned_entity__`` key.
                decoded.source_actor_id, decoded.target_actor_id,
                packet.timestamp_seconds, confirmed=True,
            )
            events.append(event)
        return events
    def _structured_action_events(
        self,
        decoded: DecodedV576Message,
        packet: PacketRecord,
    ) -> list[LiveDpsEvent]:
        decoded = _canonicalize_live_message(decoded)
        actor_id = decoded.source_actor_id
        skill_id = decoded.skill_id
        if actor_id is None or skill_id is None:
            self.structured_action_rejected += 1
            return []
        if (
            skill_id not in self.skill_catalog
            or skill_id in _STRUCTURED_ACTION_EXCLUDED_SKILL_IDS
            or actor_id in self.summon_owners
            or actor_id in self.monster_entities
            or self._action_conflicts_with_confirmed_job(actor_id, skill_id)
        ):
            self.structured_action_rejected += 1
            return []

        if decoded.kind == "skill_cast":
            self.local_actor_ids.add(actor_id)
        self._update_actor_job(actor_id, skill_id)
        self._observe_player_skill_activity(
            actor_id,
            skill_id,
            packet.timestamp_seconds,
        )
        self.skill_actor_ids.add(actor_id)
        self.latest_actor_skills[actor_id] = (skill_id, packet.timestamp_seconds)
        actor_casts = self.recent_actor_casts.setdefault(actor_id, [])
        if not actor_casts or not (
            actor_casts[-1][0] == skill_id
            and packet.timestamp_seconds - actor_casts[-1][1] <= 0.12
        ):
            actor_casts.append((skill_id, packet.timestamp_seconds))
        if len(actor_casts) > 48:
            cutoff = packet.timestamp_seconds - 15.0
            self.recent_actor_casts[actor_id] = [
                row for row in actor_casts if row[1] >= cutoff
            ]
        casters = self.recent_skill_casters.setdefault(skill_id, [])
        casters.append((actor_id, packet.timestamp_seconds))
        if len(casters) > 32:
            cutoff = packet.timestamp_seconds - 120.0
            self.recent_skill_casters[skill_id] = [
                row for row in casters if row[1] >= cutoff
            ]
        damage_casts = self.recent_damage_actor_casts.setdefault(actor_id, [])
        if not damage_casts or not (
            damage_casts[-1][0] == skill_id
            and packet.timestamp_seconds - damage_casts[-1][1] <= 0.12
        ):
            damage_casts.append((skill_id, packet.timestamp_seconds))
        if len(damage_casts) > 48:
            cutoff = packet.timestamp_seconds - 15.0
            self.recent_damage_actor_casts[actor_id] = [
                row for row in damage_casts if row[1] >= cutoff
            ]

        cast_key = (actor_id, skill_id)
        previous_cast = self.recent_emitted_casts.get(cast_key, float("-inf"))
        if packet.timestamp_seconds - previous_cast <= 0.2:
            return []
        origin = self.first_timestamp or packet.timestamp_seconds
        cast_event = SkillCastEvent(
            timestamp_ms=max(
                0,
                round((packet.timestamp_seconds - origin) * 1000),
            ),
            source_id=f"0x{actor_id:08X}",
            source_name=self._actor_display_context(actor_id)[0],
            job=self._actor_display_context(actor_id)[1],
            skill_id=str(skill_id),
            skill_name=_skill_display_name(self.skill_catalog, skill_id),
            packet_kind="structured_multi_action",
        )
        result: list[LiveDpsEvent] = [cast_event]
        result.extend(
            self._activate_permanent_player_buff(
                source_actor_id=actor_id,
                skill_id=skill_id,
                timestamp_ms=cast_event.timestamp_ms,
            )
        )
        self.recent_emitted_casts[cast_key] = packet.timestamp_seconds
        self.structured_action_emitted += 1
        return result
    def _observe_skill_identity(self, packet: PacketRecord, direction: str | None = None) -> None:
        """用技能号反查 Boss 模板，补齐中途开抓时缺失的实体表。

        怪物出场包（88 字节 ``06 27 55 00``）只在 Boss 出场那一刻出现，进本之后
        才开统计就永远看不到它，于是实体表为空、血量包和技能包全被丢掉。技能号
        来自服务端怪物技能表、全局唯一，是本包里唯一还能读到的身份线索：

        * 实体表健康时 -> ``(技能号, 模板)`` 是确定配对，写进自学习索引；
        * 实体表为空时 -> 用技能号查出模板，注入 ``monster_entities``，并顺带
          补上 ``boss_actor_ids``；后续血量/技能/语音全部回到老逻辑。
        """

        if packet.transport.upper() != "UDP":
            return
        payload = packet.payload
        if len(payload) < 38:
            return
        direction = direction or packet.inferred_direction()
        if direction != "server_to_client":
            return

        # 身份已经确认时，这条链路只剩「学新技能号」的价值，抽样跑即可：
        # relaxed_skill_records 要对整包做 13 个 XOR 相位的试解，每包都跑
        # 等于把热路径的成本翻倍。Boss 每次放技能都会连发多条记录，1/4 采样
        # 足够学到号；而身份未知时必须每包都扫，这是唯一的身份线索。
        if self.boss_actor_ids:
            serial = getattr(self, "_skill_learn_serial", 0) + 1
            self._skill_learn_serial = serial
            if serial % 4:
                return

        from ..nest_probe import relaxed_skill_records

        records = relaxed_skill_records(payload)
        if not records:
            return
        from engine.native.skill_identity import (
            catalog_template,
            learn_skill_template,
            resolve_skill_template,
            save as save_skill_index,
        )

        from engine.native.nest_inference import (
            known_nest_keys,
            template_nest_index,
        )

        entities = self.monster_entities
        players = self.local_actor_ids | set(self.known_actor_names) | self.skill_actor_ids
        nest_templates = template_nest_index()
        seeds: dict[int, int] = {}
        for actor_id, skill_id in records:
            template = entities.get(actor_id)
            if template is not None:
                # 实体表健康：技能号与模板的配对是确定的，直接进学习索引。
                if learn_skill_template(skill_id, int(template.template_id)):
                    save_skill_index()
                continue
            if actor_id in players:
                continue
            template_id = resolve_skill_template(skill_id)
            if not template_id:
                # 还没学过的技能号：退回技能目录的 Boss 词干唯一匹配。
                template_id = catalog_template(
                    skill_id, self.monster_templates, nest_templates
                )
            if not template_id:
                continue
            resolved = self.monster_templates.get(template_id)
            if resolved is None:
                continue
            # 只认 Boss / 副本 Boss 模板。30xxx 这类技能号是场景通用物体
            # （偷袭点、自动贩卖机），跨副本复用，不能当身份证据。
            if not (getattr(resolved, "boss", False) or template_id in nest_templates):
                continue
            seeds[actor_id] = template_id
        if not seeds:
            return
        applied = self.seed_monster_entities(seeds)
        if applied <= 0:
            return

        late = getattr(self, "late_identity_seeds", None)
        if late is None:
            late = self.late_identity_seeds = {}
        for actor_id, template_id in seeds.items():
            late[actor_id] = template_id
            self.probe.record(
                "skill_identity",
                actor=f"0x{actor_id:08X}",
                template_id=int(template_id),
                bossActorCount=len(self.boss_actor_ids),
            )

        from engine.native.nest_inference import known_nest_keys

        if str(self.detected_nest_key or "") in known_nest_keys():
            return
        from ..nest_recognizer import resolve_nest_template

        for template_id in seeds.values():
            resolution = resolve_nest_template(template_id)
            if resolution is None:
                continue
            profile = resolution.profile
            origin = self.first_timestamp or packet.timestamp_seconds
            self.detected_nest_key = profile.key
            self.detected_nest_label = profile.label
            self.detected_nest_name = profile.display_name
            self.detected_nest_confidence = resolution.confidence
            self.detected_nest_template_id = template_id
            self.detected_nest_observed_at = max(
                0.0, packet.timestamp_seconds - origin
            )
            self.probe.record(
                "nest_identity",
                route="skill_identity",
                template_id=int(template_id),
                nest_key=self.detected_nest_key,
                nest_name=self.detected_nest_name,
            )
            break

    def _legacy_boss_action_events(self, packet: PacketRecord) -> list[BossActionEvent]:
        if (
            packet.transport.upper() != "UDP"
            or packet.inferred_direction() != "server_to_client"
            or not self.boss_actor_ids
        ):
            return []
        decoded_actions = decode_v576_boss_actions(
            packet.payload,
            "server_to_client",
            self.boss_actor_ids,
        )
        if not decoded_actions:
            return []
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(
            0, round((packet.timestamp_seconds - origin) * 1000)
        )
        events: list[BossActionEvent] = []
        for decoded in decoded_actions:
            template = self.monster_entities.get(decoded.source_actor_id)
            if template is None or not self._is_boss_actor(decoded.source_actor_id):
                self.note_skill_drop(
                    "legacy_boss_action:trivial_actor"
                    if template is not None
                    else "legacy_boss_action:unknown_actor",
                    decoded.source_actor_id,
                    getattr(decoded, "action_code", 0),
                )
                continue
            catalog_name, mapped = self.boss_action_catalog.get(
                (template.template_id, decoded.action_code),
                ("", False),
            )
            action_name = catalog_name or f"动作 #{decoded.action_code}（待命名）"
            source_name, source_job = self._monster_display_context(
                decoded.source_actor_id
            )
            events.append(
                BossActionEvent(
                    timestamp_ms=timestamp_ms,
                    source_id=self._monster_event_id(decoded.source_actor_id),
                    source_name=source_name,
                    job=source_job or "Boss",
                    skill_id=(
                        f"boss:{template.template_id}:{decoded.action_code}"
                    ),
                    skill_name=action_name,
                    template_id=template.template_id,
                    action_code=decoded.action_code,
                    variant_code=decoded.variant_code,
                    action_kind=decoded.action_kind,
                    packet_signature=decoded.packet_signature,
                    packet_length=decoded.payload_length,
                    xor_phase=decoded.xor_phase,
                    record_offset=decoded.record_offset,
                    mapped=mapped,
                    confidence=1.0,
                )
            )
        return events
    def _boss_action_events(self, packet: PacketRecord) -> list[BossActionEvent]:
        from engine.live.protocol import decode_v576_boss_skill_starts
        from ..shadow_voice import publish_shadow_start
        from ..classic_add_voice import publish_classic_add_exit
        from ..hard_voice import publish_hard_start
        from ..sand_voice import publish_sand_start
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(
            0, round((packet.timestamp_seconds - origin) * 1000)
        )
        evidence: list[BossActionEvidence] = []
        publish_shadow_start(self, packet, timestamp_ms)
        publish_classic_add_exit(self, packet, timestamp_ms)
        publish_hard_start(self, packet, timestamp_ms)
        publish_sand_start(self, packet, timestamp_ms)
        is_server_udp = (
            packet.transport.upper() == "UDP"
            and packet.inferred_direction() == "server_to_client"
        )
        direct_actions = (
            decode_v576_boss_actions(
                packet.payload,
                "server_to_client",
                self.boss_actor_ids,
            )
            if is_server_udp and self.boss_actor_ids
            else ()
        )
        structured_actions = (
            decode_v576_boss_structured_actions(
                packet.payload,
                "server_to_client",
                self.boss_actor_ids,
            )
            if is_server_udp and self.boss_actor_ids
            else ()
        )
        skill_starts = (
            decode_v576_boss_skill_starts(packet.payload, "server_to_client",
                self.boss_actor_ids, self.boss_skill_catalog)
            if is_server_udp and self.boss_actor_ids else ()
        )
        probe = getattr(self, "probe", None)
        if probe is not None and getattr(probe, "enabled", False):
            # 观测：三段通道各自解出了什么。skill_starts 只会有白名单内的号，
            # 所以额外做一次「去掉白名单」的扫描，看这只 Boss 还发了哪些未知技能号。
            known = set(self.boss_skill_catalog)
            if direct_actions or structured_actions or skill_starts:
                probe.record(
                    "boss_channels",
                    direct=[(int(d.action_code), int(d.skill_index)) for d in direct_actions],
                    structured=[(int(d.action_code), int(d.skill_index)) for d in structured_actions],
                    starts=[int(d.skill_index) for d in skill_starts],
                    actors=[f"0x{int(a):08X}" for a in self.boss_actor_ids],
                )
            seen = getattr(self, "_probe_skill_seen", None)
            if seen is None:
                seen = self._probe_skill_seen = set()
            for skill in relaxed_skill_candidates(packet.payload, set(self.boss_actor_ids)):
                if skill in seen:
                    continue
                seen.add(skill)
                probe.record(
                    "skill_candidate",
                    skill=int(skill),
                    in_catalog=skill in known,
                    name=str(self.boss_skill_catalog.get(skill, ""))[:40],
                )
        for decoded in (*direct_actions, *structured_actions, *skill_starts):
            template = self.monster_entities.get(decoded.source_actor_id)
            if template is None or not self._is_boss_actor(decoded.source_actor_id):
                self.note_skill_drop(
                    "boss_action:trivial_actor"
                    if template is not None
                    else "boss_action:unknown_actor",
                    decoded.source_actor_id,
                    getattr(decoded, "skill_index", getattr(decoded, "action_code", 0)),
                )
                continue
            catalog_name, mapped = self.boss_action_catalog.get(
                (template.template_id, decoded.action_code),
                ("", False),
            )
            source_name, source_job = self._monster_display_context(
                decoded.source_actor_id
            )
            structured = decoded.layout.endswith("structured-multi-v576") or decoded.skill_index > 0
            skill_index = decoded.skill_index
            # Verified against Reborn Manticore_Black.act and the 1018 replay:
            # AI can invoke HeavySwoop by action_name instead of skill_index.
            # Never apply ordinal 58 globally (Apocalypse also sends action 58).
            if (not skill_index and template.template_id == 4235448
                    and decoded.action_code == 58 and decoded.action_kind == 8
                    and not structured):
                skill_index = 325423
            evidence.append(
                BossActionEvidence(
                    timestamp_ms=timestamp_ms,
                    source_actor_id=decoded.source_actor_id,
                    source_id=self._monster_event_id(decoded.source_actor_id),
                    source_name=source_name,
                    job=source_job or "Boss",
                    template_id=template.template_id,
                    action_code=decoded.action_code,
                    skill_index=skill_index,
                    variant_code=decoded.variant_code,
                    action_kind=decoded.action_kind,
                    evidence_kind=(
                        "structured_action" if structured else "protocol_action"
                    ),
                    skill_name=(
                        self.boss_skill_catalog.get(skill_index, "") or catalog_name
                        or f"\u52a8\u4f5c #{decoded.action_code}\uff08\u5f85\u547d\u540d\uff09"
                    ),
                    family_key=(
                        f"skill-start:{skill_index}" if skill_index else
                        f"structured:{decoded.action_code}:{decoded.variant_code}"
                        if structured
                        else (
                            f"action:{decoded.action_code}:"
                            f"{decoded.variant_code}:{decoded.action_kind}"
                        )
                    ),
                    packet_signature=decoded.packet_signature,
                    packet_length=decoded.payload_length,
                    xor_phase=decoded.xor_phase,
                    record_offset=decoded.record_offset,
                    mapped=mapped or skill_index > 0,
                    confidence=0.94 if structured else 1.0,
                )
            )

        decoded_hit = self.last_decoded_message
        player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        ) - self.monster_entities.keys()
        if (
            is_server_udp
            and decoded_hit is not None
            and decoded_hit.kind == "damage_hit"
            and decoded_hit.source_actor_id in self.boss_actor_ids
            and decoded_hit.target_actor_id in player_actors
            and decoded_hit.damage is not None
            and decoded_hit.damage > 0
        ):
            template = self.monster_entities.get(decoded_hit.source_actor_id)
            if template is not None and self._is_boss_actor(decoded_hit.source_actor_id):
                signature = (
                    packet.payload[5:9].hex() if len(packet.payload) >= 9 else ""
                )
                raw_code = int(decoded_hit.skill_id or 0)
                boss_skill_name = self.boss_skill_catalog.get(raw_code, "")
                family_key = (
                    f"hit:skill:{raw_code}"
                    if raw_code > 0
                    else f"hit:{decoded_hit.layout}"
                )
                action_code = (
                    raw_code
                    if raw_code > 0
                    else self.boss_action_parser.stable_action_code(
                        template.template_id, family_key
                    )
                )
                source_name, source_job = self._monster_display_context(
                    decoded_hit.source_actor_id
                )
                evidence.append(
                    BossActionEvidence(
                        timestamp_ms=timestamp_ms,
                        source_actor_id=decoded_hit.source_actor_id,
                        source_id=self._monster_event_id(
                            decoded_hit.source_actor_id
                        ),
                        source_name=source_name,
                        job=source_job or "Boss",
                        template_id=template.template_id,
                        action_code=action_code,
                        skill_index=raw_code if raw_code > 0 else 0,
                        evidence_kind="hit",
                        skill_name=boss_skill_name,
                        family_key=family_key,
                        packet_signature=signature,
                        packet_length=len(packet.payload),
                        mapped=bool(boss_skill_name),
                        confidence=0.64 if raw_code > 0 else 0.55,
                        affected_target_id=(
                            f"0x{decoded_hit.target_actor_id:08X}"
                        ),
                    )
                )
        from engine.live.voice_signals import publish_evidence
        publish_evidence(self, evidence)
        return self.boss_action_parser.observe(evidence, timestamp_ms)
    def _boss_status_action_events(
        self,
        statuses: tuple[object, ...],
        packet: PacketRecord,
    ) -> list[BossActionEvent]:
        # Most packets have no Boss status and no unsettled action. Avoid
        # rebuilding player actor sets hundreds of thousands of times in a
        # long replay; when an action is pending we still tick it on every
        # packet, preserving the exact historical settle/order semantics.
        if not statuses and not self.boss_action_parser.has_pending:
            return []
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(
            0, round((packet.timestamp_seconds - origin) * 1000)
        )
        player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        ) - self.monster_entities.keys()
        evidence: list[BossActionEvidence] = []
        signature = packet.payload[5:9].hex() if len(packet.payload) >= 9 else ""
        for status in statuses:
            if (
                status.source_actor_id not in self.boss_actor_ids
                or status.target_actor_id not in player_actors
            ):
                continue
            template = self.monster_entities.get(status.source_actor_id)
            if template is None or not self._is_boss_actor(status.source_actor_id):
                self.note_skill_drop(
                    "boss_status:trivial_actor"
                    if template is not None
                    else "boss_status:unknown_actor",
                    status.source_actor_id,
                    status.skill_id,
                )
                continue
            raw_code = int(status.skill_id or 0)
            family_key = (
                f"status:{raw_code}:{status.effect_code}"
                if raw_code > 0
                else f"status:{status.effect_code}:{status.parameters}"
            )
            action_code = (
                raw_code
                if raw_code > 0
                else self.boss_action_parser.stable_action_code(
                    template.template_id, family_key
                )
            )
            source_name, source_job = self._monster_display_context(
                status.source_actor_id
            )
            status_definition = self.status_effect_catalog.get(raw_code)
            boss_skill_name = self.boss_skill_catalog.get(raw_code, "")
            status_name = boss_skill_name or (
                status_definition.display_name
                if status_definition is not None
                else self.status_skill_catalog.get(raw_code, "")
            )
            evidence.append(
                BossActionEvidence(
                    timestamp_ms=timestamp_ms,
                    source_actor_id=status.source_actor_id,
                    source_id=self._monster_event_id(status.source_actor_id),
                    source_name=source_name,
                    job=source_job or "Boss",
                    template_id=template.template_id,
                    action_code=action_code,
                    skill_index=raw_code if raw_code > 0 else 0,
                    evidence_kind="status",
                    skill_name=(
                        f"{status_name}\uff08\u72b6\u6001\u52a8\u4f5c\uff09"
                        if status_name
                        else ""
                    ),
                    family_key=family_key,
                    packet_signature=signature,
                    packet_length=len(packet.payload),
                    xor_phase=int(status.xor_phase),
                    record_offset=int(status.record_offset),
                    mapped=bool(boss_skill_name),
                    confidence=0.72 if raw_code > 0 else 0.6,
                    affected_target_id=(
                        f"0x{status.target_actor_id:08X}"
                    ),
                )
            )
        from engine.live.voice_signals import publish_evidence
        publish_evidence(self, evidence)
        return self.boss_action_parser.observe(evidence, timestamp_ms)
    def _direct_player_buff_clear_events(
        self,
        statuses: tuple[object, ...],
        packet: PacketRecord,
    ) -> list[BossActionEvent]:
        """Emit clear hits immediately from their server status records.

        Monsters do not send one remove record per existing buff. Skills with
        monster EffectClass 83 instead apply a status to every player they
        clear. Those target-specific records are the authoritative instant and
        must not wait for the delayed action grouping pass. Mini-bosses and
        summoned guardians are valid sources even when their template is not
        flagged as the dungeon's primary Boss.
        """
        if not statuses or not self.player_buff_clear_boss_skill_ids:
            return []
        player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        ) - self.monster_entities.keys()
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(
            0, round((packet.timestamp_seconds - origin) * 1000)
        )
        events: list[BossActionEvent] = []
        for status in statuses:
            skill_index = int(status.skill_id or 0)
            if (
                skill_index not in self.player_buff_clear_boss_skill_ids
                or status.source_actor_id not in self.monster_entities
                or status.target_actor_id not in player_actors
            ):
                continue
            key = (
                status.source_actor_id,
                status.target_actor_id,
                skill_index,
            )
            previous = self.recent_direct_player_buff_clears.get(key)
            if (
                previous is not None
                and 0.0 <= packet.timestamp_seconds - previous <= 0.05
            ):
                continue
            self.recent_direct_player_buff_clears[key] = packet.timestamp_seconds
            template = self.monster_entities.get(status.source_actor_id)
            if template is None:
                continue
            source_name, source_job = self._monster_display_context(
                status.source_actor_id
            )
            skill_name = self.boss_skill_catalog.get(
                skill_index, f"清除增益 #{skill_index}"
            )
            events.append(
                BossActionEvent(
                    timestamp_ms=timestamp_ms,
                    end_timestamp_ms=timestamp_ms,
                    source_id=self._monster_event_id(status.source_actor_id),
                    source_name=source_name,
                    job=source_job or ("Boss" if template.boss else "怪物"),
                    skill_id=(
                        f"boss:{template.template_id}:skill:{skill_index}"
                    ),
                    skill_name=skill_name,
                    template_id=template.template_id,
                    action_code=0,
                    skill_index=skill_index,
                    packet_kind="boss_buff_clear_status",
                    packet_signature=(
                        packet.payload[5:9].hex()
                        if len(packet.payload) >= 9
                        else ""
                    ),
                    packet_length=len(packet.payload),
                    xor_phase=int(status.xor_phase),
                    record_offset=int(status.record_offset),
                    mapped=True,
                    confidence=1.0,
                    evidence_types=("status_clear",),
                    evidence_count=1,
                    status_count=1,
                    affected_target_ids=(
                        f"0x{status.target_actor_id:08X}",
                    ),
                )
            )
        if len(self.recent_direct_player_buff_clears) > 256:
            cutoff = packet.timestamp_seconds - 30.0
            self.recent_direct_player_buff_clears = {
                key: seen_at
                for key, seen_at in self.recent_direct_player_buff_clears.items()
                if seen_at >= cutoff
            }
        return events
    def flush_boss_actions(self) -> list[BossActionEvent]:
        return self.boss_action_parser.flush()
    def _player_lifecycle_events(
        self, packet: PacketRecord
    ) -> list[PlayerLifecycleEvent]:
        known_player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
            | set(self.actor_jobs)
        ) - set(self.monster_entities) - set(self.summon_owners)
        records = decode_v576_player_lifecycle(
            packet.payload,
            packet.inferred_direction(),
            known_player_actors,
        )
        if not records:
            return []
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(
            0, round((packet.timestamp_seconds - origin) * 1000)
        )
        signature = (
            packet.payload[5:9].hex() if len(packet.payload) >= 9 else ""
        )
        events: list[PlayerLifecycleEvent] = []
        for record in records:
            target_name, target_job = self._actor_display_context(record.actor_id)
            events.append(
                PlayerLifecycleEvent(
                    timestamp_ms=timestamp_ms,
                    target_id=f"0x{record.actor_id:08X}",
                    target_name=target_name,
                    target_job=target_job,
                    active=record.active,
                    packet_signature=signature,
                    packet_length=len(packet.payload),
                    xor_phase=record.xor_phase,
                    record_offset=record.record_offset,
                )
            )
        return events
    def _player_health_events(
        self, packet: PacketRecord
    ) -> list[LiveDpsEvent]:
        known_player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
            | set(self.actor_jobs)
        ) - set(self.monster_entities) - set(self.summon_owners)
        # Summons publish the same absolute ``08 6F`` state as party HP, but
        # are deliberately excluded from PlayerHealthEvent.  Include only
        # already owner-bound summon actors in the low-level decoder so their
        # factual lethal state can close the matching overlay timer.
        known_health_actors = known_player_actors | set(self.summon_owners)
        records = decode_v576_player_health(
            packet.payload,
            packet.inferred_direction(),
            known_health_actors,
            self.local_actor_ids,
        )
        if not records:
            return []
        # 21 0C is an authoritative resource boundary, but its outer observer
        # is not unconditionally a player actor.  Accelerator relay variants
        # can place a connection/context token in that slot (for example
        # 0x02307298), while the nested actor still contains the real player.
        # Promote only an observer or nested actor that was independently
        # confirmed as local by an outbound cast/profile.  This preserves the
        # HP/MP evidence and reconnect matching without letting a relay token
        # retarget the Buff overlay to a non-existent player.
        resource_actor_ids = {
            int(record.actor_id)
            for record in records
            if (
                record.layout == "s2c-udp-player-resource-sync-210c-v576"
                and int(record.actor_id) > _SUMMON_ACTOR_ID_MAX
                and int(record.actor_id) not in self.monster_entities
                and int(record.actor_id) not in self.summon_owners
            )
        }
        resource_observers = {
            int(record.observer_actor_id)
            for record in records
            if (
                record.layout == "s2c-udp-player-resource-sync-210c-v576"
                and int(record.observer_actor_id) > _SUMMON_ACTOR_ID_MAX
                and int(record.observer_actor_id) not in self.monster_entities
                and int(record.observer_actor_id) not in self.summon_owners
            )
        }
        canonical_local_ids = {
            self._canonical_player_actor_id(actor_id)
            for actor_id in self.local_actor_ids
            if int(actor_id) > _SUMMON_ACTOR_ID_MAX
        }
        canonical_resource_actor_ids = {
            self._canonical_player_actor_id(actor_id)
            for actor_id in resource_actor_ids
        }
        confirmed_resource_self_ids = {
            self._canonical_player_actor_id(actor_id)
            for actor_id in resource_actor_ids | resource_observers
            if self._canonical_player_actor_id(actor_id) in canonical_local_ids
        }
        # A 21 0C frame can also bootstrap identity without an outbound packet
        # when its outer observer is present as one of the frame's own player
        # resource records.  The accelerator context tokens observed in real
        # captures never satisfy that self-consistency check.
        confirmed_resource_self_ids.update(
            self._canonical_player_actor_id(observer_actor_id)
            for observer_actor_id in resource_observers
            if self._canonical_player_actor_id(observer_actor_id)
            in canonical_resource_actor_ids
        )
        if len(confirmed_resource_self_ids) == 1:
            confirmed_self_actor_id = next(iter(confirmed_resource_self_ids))
            self.current_self_actor_id = confirmed_self_actor_id
            self.local_actor_ids.add(confirmed_self_actor_id)
        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(0, round((packet.timestamp_seconds - origin) * 1000))
        signature = packet.payload[5:9].hex() if len(packet.payload) >= 9 else ""
        events: list[LiveDpsEvent] = []
        for record in records:
            summon_owner = self.summon_owners.get(record.actor_id)
            if summon_owner is not None:
                # Delta notifications carry ``current_hp = 0`` only because
                # the absolute value is unknown.  A summon is removed solely
                # by the signed, authoritative 08 6F post-hit state.
                if (
                    record.layout
                    != "s2c-udp-player-hp-state-086f-v576"
                    or record.raw_current_hp is None
                ):
                    continue
                raw_current_hp = int(record.raw_current_hp)
                previous_hp = max(
                    0,
                    int(self.summon_current_hp.get(record.actor_id, 0)),
                )
                self.summon_current_hp[record.actor_id] = max(
                    0, raw_current_hp
                )
                if raw_current_hp > 0:
                    self.summon_lethal_actor_ids.discard(record.actor_id)
                    continue
                if record.actor_id in self.summon_lethal_actor_ids:
                    continue
                self.summon_lethal_actor_ids.add(record.actor_id)
                self._forget_cross_scene_prebattle_summon(record.actor_id)
                self.entity_owner_evidence.retire(
                    record.actor_id,
                    reason="lethal_hp_state",
                    timestamp=packet.timestamp_seconds,
                )
                owner_actor_id, summon_skill_id = summon_owner
                owner_actor_id = self._canonical_player_actor_id(
                    owner_actor_id
                )
                if int(summon_skill_id) == 6316:
                    self.active_azuna_breath_refresh_sources.pop(
                        owner_actor_id, None
                    )
                owner_name, owner_job = self._actor_display_context(
                    owner_actor_id
                )
                events.append(
                    SummonRemovalEvent(
                        timestamp_ms=timestamp_ms,
                        summon_id=f"0x{record.actor_id:08X}",
                        owner_id=f"0x{owner_actor_id:08X}",
                        owner_name=owner_name,
                        owner_job=owner_job,
                        skill_id=str(summon_skill_id),
                        skill_name=_skill_display_name(
                            self.skill_catalog, summon_skill_id
                        ),
                        previous_hp=previous_hp,
                        raw_current_hp=raw_current_hp,
                        observed_lethal_damage=max(
                            0, previous_hp - raw_current_hp
                        ),
                        packet_signature=signature,
                        packet_length=len(packet.payload),
                        xor_phase=record.xor_phase,
                        record_offset=record.record_offset,
                    )
                )
                continue
            if (
                int(record.actor_id) <= _SUMMON_ACTOR_ID_MAX
                or int(record.actor_id) in self.monster_entities
            ):
                # ``21 0C`` also occurs in non-player resource/property rows.
                # In 0916迷雾冰, object 0x0000009C published the Lv70
                # ``危险值`` sequence 206000..2060000. Treating its smallest
                # value as current HP and its largest value as maximum HP made
                # the overlay report an anonymous teammate at exactly 10% on
                # three separate crossings. Runtime player actor IDs live above
                # the reserved summon/object range; owner-bound summons were
                # handled explicitly above. Reject every other low/object or
                # confirmed monster actor before it can become player HP.
                continue
            if (
                record.layout == "s2c-udp-player-hp-state-086f-v576"
                and record.raw_current_hp is not None
                and int(record.raw_current_hp) <= 0
            ):
                self._forget_cross_scene_prebattle_summons_for_owner(
                    record.actor_id
                )
            self._observe_player_health_state(
                actor_id=record.actor_id,
                current_hp=record.current_hp,
                current_mp=record.current_mp,
                packet_kind=record.layout,
                timestamp=packet.timestamp_seconds,
            )
            target_name, target_job = self._actor_display_context(record.actor_id)
            target_id = f"0x{record.actor_id:08X}"
            events.append(
                PlayerHealthEvent(
                    timestamp_ms=timestamp_ms,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                    current_hp=record.current_hp,
                    max_hp=record.max_hp,
                    current_mp=record.current_mp,
                    max_mp=record.max_mp,
                    hp_delta=record.hp_delta,
                    packet_kind=record.layout,
                    packet_signature=signature,
                    packet_length=len(packet.payload),
                    xor_phase=record.xor_phase,
                    record_offset=record.record_offset,
                )
            )
            if record.hp_delta < 0:
                events.append(
                    CombatEvent(
                        timestamp_ms=timestamp_ms,
                        source_id="__incoming_hp_delta__",
                        source_name="服务器生命变化",
                        job="",
                        skill_id="incoming_hp_delta",
                        skill_name="实际生命损失",
                        amount=-record.hp_delta,
                        target_id=target_id,
                        target_name=target_name,
                        target_job=target_job,
                        target_max_hp=max(0, record.max_hp),
                        attribution_method="incoming_hp_delta",
                        raw_attribution_method=record.layout,
                    )
                )
        return events


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'V576StatusDecoderMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
