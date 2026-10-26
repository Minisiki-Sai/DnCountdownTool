from __future__ import annotations

from functools import lru_cache

from ..damage_object_origins import DAMAGE_OBJECT_SKILL_IDS

class V576CombatDecoderMixin:
    def _healing_source_context(
        self, observer_actor_id: int, timestamp: float
    ) -> tuple[int | None, int, str, str]:
        candidates: list[tuple[float, int, int]] = []
        for skill_id in self.healing_skill_ids:
            skill_name = _skill_display_name(self.skill_catalog, skill_id)
            window = _HEALING_CAST_WINDOWS.get(
                _summon_skill_key(skill_name), 20.0
            )
            for actor_id, cast_time in self.recent_skill_casters.get(skill_id, []):
                if 0.0 <= timestamp - cast_time <= window:
                    candidates.append((cast_time, actor_id, skill_id))
        if candidates:
            _cast_time, actor_id, skill_id = max(candidates)
            return (
                actor_id,
                skill_id,
                _skill_display_name(self.skill_catalog, skill_id),
                "healing_cast",
            )
        # The leading actor in an HP-delta packet is the local observer, not
        # an authoritative healer.  When no nearby healing cast proves the
        # source, keep the amount unattributed instead of crediting whichever
        # player happened to record the capture.
        return None, 0, "待识别治疗", "unresolved_healing"
    def _healing_events(self, packet: PacketRecord) -> list[CombatEvent]:
        decoded_events = decode_v576_healing(
            packet.payload, packet.inferred_direction()
        )
        if not decoded_events:
            return []
        origin = self.first_timestamp or packet.timestamp_seconds
        events: list[CombatEvent] = []
        for decoded in decoded_events:
            if (
                decoded.source_actor_id is None
                or decoded.target_actor_id is None
                or decoded.damage is None
            ):
                continue
            actor_id, skill_id, skill_name, method = self._healing_source_context(
                decoded.source_actor_id, packet.timestamp_seconds
            )
            if actor_id is None:
                source_id = "healing:unknown"
                source_name = "未知治疗来源"
                source_job = "未知职业"
            else:
                source_id = f"0x{actor_id:08X}"
                source_name, source_job = self._actor_display_context(actor_id)
            if decoded.target_actor_id in self.monster_entities:
                target_id = self._monster_event_id(decoded.target_actor_id)
                target_name, target_job = self._monster_display_context(
                    decoded.target_actor_id
                )
                target_max_hp = self._monster_max_hp(decoded.target_actor_id)
            else:
                target_id = f"0x{decoded.target_actor_id:08X}"
                target_name, target_job = self._actor_display_context(
                    decoded.target_actor_id
                )
                target_max_hp = 0
            events.append(
                CombatEvent(
                    timestamp_ms=max(
                        0,
                        round(
                            (packet.timestamp_seconds - origin) * 1000
                        ),
                    ),
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    skill_id=(str(skill_id) if skill_id else "healing:unknown"),
                    skill_name=skill_name,
                    amount=decoded.damage,
                    critical=False,
                    healing=True,
                    target_id=target_id,
                    target_name=target_name,
                    target_job=target_job,
                    target_max_hp=target_max_hp,
                    attribution_method=method,
                    raw_attribution_method=decoded.layout,
                )
            )
        return events
    @staticmethod
    def _damage_signature(packet: PacketRecord, layout: str) -> str:
        return f"{len(packet.payload)}:{packet.payload[5:9].hex()}:{layout}"
    def _load_learned_damage_profiles(self) -> None:
        path = self.learned_profile_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        skills = data.get("skills", {}) if isinstance(data, dict) else {}
        if not isinstance(skills, dict):
            return
        for skill_id_text, value in skills.items():
            if not isinstance(value, dict):
                continue
            try:
                skill_id = int(skill_id_text)
            except ValueError:
                continue
            if skill_id not in self.damage_skill_ids:
                continue
            delays = [
                float(delay)
                for delay in value.get("delays", [])
                if isinstance(delay, (int, float)) and 0 <= float(delay) <= 12
            ]
            signatures = Counter(
                {
                    str(key): int(count)
                    for key, count in value.get("signatures", {}).items()
                    if isinstance(count, int) and count > 0
                }
            )
            if delays:
                self.learned_damage_profiles[skill_id] = (
                    _LearnedDamageTimingProfile(delays[-256:], signatures)
                )
    def save_learned_damage_profiles(self) -> None:
        path = self.learned_profile_path
        if path is None or not self.learned_profiles_dirty:
            return
        data = {
            "version": 1,
            "skills": {
                str(skill_id): {
                    "delays": profile.delays[-256:],
                    "signatures": dict(profile.signatures.most_common(24)),
                }
                for skill_id, profile in sorted(
                    self.learned_damage_profiles.items()
                )
                if profile.delays and skill_id in self.damage_skill_ids
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
            self.learned_profiles_dirty = False
        except OSError:
            return
    def _damage_casts_for_actor(
        self, actor_id: int
    ) -> list[tuple[int, float]]:
        # Tests and older integrations may seed recent_actor_casts directly.
        # Once the decoder has seen a real packet it creates an explicit
        # damage-context list, including an empty list for timeline-only
        # compact inputs.
        if actor_id in self.recent_damage_actor_casts:
            return self.recent_damage_actor_casts[actor_id]
        return self.recent_actor_casts.get(actor_id, [])
    def _latest_cast_time(
        self, actor_id: int, skill_id: int, timestamp: float
    ) -> float | None:
        for cast_skill_id, cast_time in reversed(
            self._damage_casts_for_actor(actor_id)
        ):
            if cast_time > timestamp:
                continue
            if timestamp - cast_time > 12.0:
                break
            if cast_skill_id == skill_id:
                return cast_time
        return None
    def _learn_damage_timing(
        self,
        actor_id: int,
        skill_id: int,
        timestamp: float,
        signature: str,
        attribution_method: str,
    ) -> None:
        # Grudge Formation has an exact 13 5D field generation, while its
        # skill-less pulses do not repeat the generation ID.  Learning from a
        # continuation/unique-cast guess feeds that guess back as future
        # evidence and was observed to absorb neighbouring Soul Eater skills.
        # Keep only independently identified 5207 rows out of this circular
        # timing profile.
        if skill_id == 5207 and attribution_method != "packet_skill_id":
            return
        if attribution_method not in _PROFILE_LEARNING_METHODS:
            return
        if skill_id not in self.damage_skill_ids:
            return
        cast_time = self._latest_cast_time(actor_id, skill_id, timestamp)
        if cast_time is None:
            return
        delay = timestamp - cast_time
        if not 0 <= delay <= 12.0:
            return
        profile = self.learned_damage_profiles.setdefault(
            skill_id, _LearnedDamageTimingProfile()
        )
        profile.add(delay, signature)
        self.learned_profiles_dirty = True
    def _learned_damage_candidate(
        self,
        actor_id: int,
        timestamp: float,
        signature: str,
    ) -> int | None:
        latest_casts: dict[int, float] = {}
        for skill_id, cast_time in reversed(
            self._damage_casts_for_actor(actor_id)
        ):
            age = timestamp - cast_time
            if age < 0:
                continue
            if age > 12.0:
                break
            latest_casts.setdefault(skill_id, cast_time)

        candidates: list[tuple[float, int]] = []
        for skill_id, cast_time in latest_casts.items():
            if skill_id not in self.damage_skill_ids:
                continue
            if skill_id == 5207:
                # 5207 is lifecycle-backed, not a learnable cast-delay
                # family. Old on-disk profiles may contain circular labels.
                continue
            profile = self.learned_damage_profiles.get(skill_id)
            if profile is None or len(profile.delays) < 4:
                continue
            age = timestamp - cast_time
            max_delay = max(profile.delays)
            if age > max_delay + max(0.35, max_delay * 0.12):
                continue
            nearest_delta = min(abs(age - delay) for delay in profile.delays)
            tolerance = max(0.18, min(0.5, 0.12 + age * 0.08))
            if nearest_delta > tolerance:
                continue
            signature_count = profile.signatures.get(signature, 0)
            if profile.signatures and signature_count <= 0:
                continue
            score = nearest_delta / tolerance
            if signature_count:
                score -= min(0.35, signature_count * 0.02)
            candidates.append((score, skill_id))

        if not candidates:
            return None
        candidates.sort()
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 0.35:
            return None
        return candidates[0][1]
    def _periodic_rule_for_status(
        self,
        raw_skill_id: int,
        resolved_skill_id: int,
        parameters: str,
        duration_ms: int,
    ) -> _PeriodicDamageRule | None:
        checked: set[int] = set()
        for skill_id in (raw_skill_id, resolved_skill_id):
            if skill_id in checked:
                continue
            checked.add(skill_id)
            for rule in self.periodic_damage_catalog.get(skill_id, ()):
                if rule.matches(parameters, duration_ms):
                    return rule
        return None
    def _remember_periodic_status(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        raw_skill_id: int,
        resolved_skill_id: int,
        parameters: str,
        duration_ms: int,
        effect_code: str,
        timestamp: float,
    ) -> None:
        rule = self._periodic_rule_for_status(
            raw_skill_id,
            resolved_skill_id,
            parameters,
            duration_ms,
        )
        if rule is None:
            return
        key = (
            source_actor_id,
            target_actor_id,
            rule.kind,
            rule.source_skill_id,
        )
        self.active_periodic_damage[key] = _ActivePeriodicDamage(
            source_actor_id=source_actor_id,
            target_actor_id=target_actor_id,
            raw_skill_id=raw_skill_id,
            source_skill_id=rule.source_skill_id,
            source_skill_name=rule.source_skill_name,
            kind=rule.kind,
            label=rule.label,
            effect_code=effect_code,
            applied_at=timestamp,
            expires_at=timestamp + max(0, duration_ms) / 1000.0,
        )
    def _periodic_damage_candidate(
        self,
        source_actor_id: int,
        target_actor_id: int | None,
        timestamp: float,
        appended_status_skill_id: int | None,
    ) -> _ActivePeriodicDamage | None:
        if target_actor_id is None:
            return None
        expired = [
            key
            for key, state in self.active_periodic_damage.items()
            if state.expires_at + 0.25 < timestamp
        ]
        for key in expired:
            self.active_periodic_damage.pop(key, None)
        candidates = [
            state
            for state in self.active_periodic_damage.values()
            if state.source_actor_id == source_actor_id
            and state.target_actor_id == target_actor_id
            and state.applied_at <= timestamp <= state.expires_at + 0.25
        ]
        actor_profiles = self.actor_skill_profiles.get(source_actor_id)
        if actor_profiles:
            candidates = [
                state
                for state in candidates
                if not self.skill_profiles.get(state.source_skill_id)
                or not actor_profiles.isdisjoint(
                    self.skill_profiles.get(
                        state.source_skill_id, frozenset()
                    )
                )
            ]
        if appended_status_skill_id is not None:
            matching = [
                state
                for state in candidates
                if appended_status_skill_id
                in {state.raw_skill_id, state.source_skill_id}
            ]
            if len(matching) == 1:
                return matching[0]
        if candidates:
            kinds = {(state.kind, state.label) for state in candidates}
            if len(kinds) == 1:
                # Multiple fire skills can keep independent Burn states. The
                # game combines their ticks into one status-damage row.
                return max(candidates, key=lambda state: state.applied_at)
        # With one active status there is no competing owner. When several
        # periodic families overlap, retaining an unresolved hit is safer than
        # fabricating a skill share; a later packet signature can learn it.
        return candidates[0] if len(candidates) == 1 else None
    def _skill_applies_periodic_kind(
        self,
        skill_id: int,
        kind: str,
    ) -> bool:
        resolved_skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)
        return (resolved_skill_id, kind) in self.periodic_source_kinds
    def _same_packet_damage_trigger(
        self,
        decoded: object,
        primary_decoded: object | None,
    ) -> int | None:
        """Return the resolved direct skill that owns a bundled extra hit."""
        if (
            primary_decoded is None
            or getattr(primary_decoded, "kind", None) != "damage_hit"
            or getattr(primary_decoded, "source_actor_id", None)
            != getattr(decoded, "source_actor_id", None)
            or getattr(primary_decoded, "target_actor_id", None)
            != getattr(decoded, "target_actor_id", None)
        ):
            return None
        try:
            skill_id = int(getattr(primary_decoded, "skill_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        skill_id = _LIVE_DAMAGE_SKILL_ALIASES.get(skill_id, skill_id)
        source_actor_id = int(
            getattr(primary_decoded, "source_actor_id", 0) or 0
        )
        if self._action_conflicts_with_confirmed_job(
            source_actor_id, skill_id
        ):
            # Bundled rows do not carry an independent owner skill.  Never
            # propagate an incidental integer from the primary compound row
            # into a profession that cannot own that skill.
            return None
        return skill_id if skill_id in self.damage_skill_ids else None
    def _supplemental_cast_damage_candidate(
        self,
        source_actor_id: int,
        timestamp: float,
        packet: PacketRecord,
        *,
        allow_recent_cast: bool,
    ) -> tuple[int, str] | None:
        """Resolve extra HP records before considering an active DOT owner."""
        continuation_skill_id = self._continuation_damage_candidate(
            source_actor_id,
            timestamp,
            packet,
            profiled_only=True,
        )
        if continuation_skill_id is not None:
            return continuation_skill_id, "supplemental_continuation"

        sustained_skill_id = self._sustained_cast_damage_candidate(
            source_actor_id,
            timestamp,
            packet,
        )
        if sustained_skill_id is not None:
            return sustained_skill_id, "supplemental_continuation"

        if not allow_recent_cast:
            return None
        actor_profiles = self.actor_skill_profiles.get(source_actor_id)
        for cast_skill_id, cast_time in reversed(
            self._damage_casts_for_actor(source_actor_id)
        ):
            age = timestamp - cast_time
            if age < 0:
                continue
            if age > _SUPPLEMENTAL_DIRECT_CAST_WINDOW_SECONDS:
                break
            cast_profiles = self.skill_profiles.get(
                cast_skill_id, frozenset()
            )
            if (
                cast_skill_id in self.damage_skill_ids
                and cast_skill_id not in DAMAGE_OBJECT_SKILL_IDS
                and (
                    not actor_profiles
                    or not cast_profiles
                    or not actor_profiles.isdisjoint(cast_profiles)
                )
            ):
                return cast_skill_id, "supplemental_cast_owner"
        return None
    def _continuation_damage_candidate(
        self,
        source_actor_id: int,
        timestamp: float,
        packet: PacketRecord,
        *,
        profiled_only: bool = False,
    ) -> int | None:
        profiled: list[
            tuple[float, float, int, tuple[int, int, float], int]
        ] = []
        unprofiled: list[tuple[float, int]] = []
        packet_signature = (len(packet.payload), packet.payload[5:9])
        stale_usage = [
            key
            for key in self.continuation_profile_usage
            if timestamp - key[2] > 120.0
        ]
        for key in stale_usage:
            self.continuation_profile_usage.pop(key, None)
        for skill_id, window_seconds in _CONTINUATION_DAMAGE_WINDOWS.items():
            if self._action_conflicts_with_confirmed_job(
                source_actor_id, skill_id
            ):
                continue
            if (
                skill_id == 5207
                and self.field_effect_instances.active_generation(
                    source_actor_id,
                    skill_id,
                    timestamp,
                    # The final server result can trail the declared status
                    # expiry slightly; this is transport grace, not an extra
                    # field lifetime.
                    expiry_grace_seconds=0.5,
                )
                is None
            ):
                continue
            profile = _CONTINUATION_DAMAGE_PROFILES.get(skill_id)
            if profiled_only and profile is None:
                # A broad cast-duration fallback must not pre-empt a
                # persistent effect with an independently known pulse rhythm.
                # Guardian's Divine Punishment EX is the important overlap:
                # its 0.5-second aura runs while Chain Lightning's legacy
                # 4.5-second continuation window may also be open.
                continue
            if profile is not None and packet_signature not in profile["signatures"]:
                continue
            for actor_id, cast_time in self.recent_skill_casters.get(skill_id, []):
                age = timestamp - cast_time
                if actor_id != source_actor_id or not (0.0 <= age <= window_seconds):
                    continue
                if profile is None:
                    unprofiled.append((cast_time, skill_id))
                    continue
                usage_key = (source_actor_id, skill_id, cast_time)
                used_slots = self.continuation_profile_usage.get(
                    usage_key, set()
                )
                available_slots = [
                    (abs(age - delay), slot_index)
                    for slot_index, delay in enumerate(profile["delays"])
                    if slot_index not in used_slots
                ]
                if not available_slots:
                    continue
                distance, slot_index = min(available_slots)
                if distance <= profile["tolerance"]:
                    profiled.append(
                        (
                            distance,
                            -cast_time,
                            skill_id,
                            usage_key,
                            slot_index,
                        )
                    )
        if profiled:
            _distance, _cast_order, skill_id, usage_key, slot_index = min(
                profiled
            )
            self.continuation_profile_usage.setdefault(usage_key, set()).add(
                slot_index
            )
            return skill_id
        if unprofiled:
            return max(unprofiled)[1]
        return None
    def _periodic_cast_damage_fallback(
        self,
        source_actor_id: int,
        target_actor_id: int | None,
        timestamp: float,
        packet: PacketRecord,
    ) -> _ActivePeriodicDamage | None:
        if target_actor_id is None:
            return None
        signature = (len(packet.payload), packet.payload[5:9])
        candidates: list[tuple[float, int, dict[str, object]]] = []
        for cast_skill_id, rule in _PERIODIC_CAST_DAMAGE_FALLBACKS.items():
            if signature not in rule["signatures"]:
                continue
            for actor_id, cast_time in self.recent_skill_casters.get(
                cast_skill_id, []
            ):
                age = timestamp - cast_time
                if (
                    actor_id == source_actor_id
                    and float(rule["min_delay"])
                    <= age
                    <= float(rule["max_delay"])
                ):
                    candidates.append((cast_time, cast_skill_id, rule))
        if not candidates:
            return None
        cast_time, cast_skill_id, rule = max(candidates, key=lambda row: row[0])
        result_skill_id = int(rule["result_skill_id"])
        return _ActivePeriodicDamage(
            source_actor_id=source_actor_id,
            target_actor_id=target_actor_id,
            raw_skill_id=cast_skill_id,
            source_skill_id=result_skill_id,
            source_skill_name=_skill_display_name(
                self.skill_catalog, result_skill_id
            ),
            kind=str(rule["kind"]),
            label=str(rule["label"]),
            effect_code="cast_fallback",
            applied_at=cast_time,
            expires_at=cast_time + float(rule["max_delay"]),
        )
    def _sustained_cast_damage_candidate(
        self,
        source_actor_id: int,
        timestamp: float,
        packet: PacketRecord,
    ) -> int | None:
        signature = (len(packet.payload), packet.payload[5:9])
        candidates: list[tuple[float, int]] = []
        suppression_only_match = False
        for cast_skill_id, rule in _SUSTAINED_CAST_DAMAGE_RULES.items():
            if signature not in rule["signatures"]:
                continue
            for actor_id, cast_time in self.recent_skill_casters.get(
                cast_skill_id, []
            ):
                age = timestamp - cast_time
                if (
                    actor_id == source_actor_id
                    and float(rule["min_delay"])
                    <= age
                    <= float(rule["max_delay"])
                ):
                    if bool(rule.get("suppression_only")):
                        suppression_only_match = True
                        continue
                    candidates.append(
                        (cast_time, int(rule["result_skill_id"]))
                    )
        if suppression_only_match:
            return None
        if not candidates:
            return None
        result_skill_ids = {skill_id for _cast_time, skill_id in candidates}
        if len(result_skill_ids) != 1:
            return None
        return result_skill_ids.pop()
    def _refreshable_pulsing_aura_candidate(
        self,
        source_actor_id: int,
        target_actor_id: int | None,
        timestamp: float,
        layout: str,
    ) -> int | None:
        """Resolve skill-less Holy Punishment pulses without stealing casts.

        Exact status generations are retained separately for diagnostics.
        Until a pulse repeats that generation key, this selector deliberately
        keeps its established cast-backed behaviour.
        """
        if target_actor_id is None or not (
            "direct37" in layout
            or layout in {
                "s2c-udp-hit-136-v576",
                "s2c-udp-hit-140-v576",
                "s2c-udp-hit-148-v576",
                "s2c-udp-periodic-hit-160-v576",
            }
        ):
            return None

        # The status ledger above proves the aura generation boundary, but
        # the skill-less direct37 pulse does not repeat its effect-instance
        # ID. Shock and aura rows can therefore overlap with identical wire
        # shapes. Keep the established cast-backed display selection until a
        # per-hit status/object foreign key is decoded; otherwise merely
        # changing START by a few milliseconds swaps real Shock and aura Hits.
        candidates: list[
            tuple[float, int, dict[str, object], _ActivePulsingAura | None]
        ] = []
        for cast_skill_id, rule in _REFRESHABLE_PULSING_AURA_RULES.items():
            for actor_id, cast_time in self.recent_skill_casters.get(
                cast_skill_id, ()
            ):
                age = timestamp - cast_time
                result_skill_id = int(rule["result_skill_id"])
                aura_state = self.active_refreshable_auras.get(
                    (source_actor_id, result_skill_id)
                )
                if bool(rule.get("requires_exact_ex_status")) and (
                    aura_state is None
                    or abs(aura_state.applied_at - cast_time) > 1.0
                    or timestamp < aura_state.applied_at
                    or timestamp > aura_state.expires_at + 0.25
                ):
                    continue
                if (
                    actor_id == source_actor_id
                    and float(rule["min_delay"])
                    <= age
                    <= float(rule["duration"]) + 0.25
                ):
                    candidates.append(
                        (cast_time, cast_skill_id, rule, aura_state)
                    )
        if not candidates:
            return None
        cast_time, _cast_skill_id, rule, aura_state = max(
            candidates, key=lambda row: row[0]
        )
        result_skill_id = int(rule["result_skill_id"])
        generation_key = (
            (aura_state.effect_instance_id, aura_state.applied_at)
            if aura_state is not None
            else (0, cast_time)
        )

        hit_key = (source_actor_id, target_actor_id, result_skill_id)
        previous = self.refreshable_aura_last_hits.get(hit_key)
        if previous is not None:
            previous_generation, previous_hit = previous
            if previous_generation == generation_key and (
                timestamp - previous_hit < float(rule["min_hit_gap"])
            ):
                return None
        self.refreshable_aura_last_hits[hit_key] = (generation_key, timestamp)
        return result_skill_id

    def _remember_refreshable_pulsing_aura_status(
        self,
        *,
        source_actor_id: int,
        target_actor_id: int,
        raw_skill_id: int,
        resolved_skill_id: int,
        effect_class_id: int,
        parameters: str,
        duration_ms: int,
        effect_instance_id: int,
        timestamp: float,
    ) -> None:
        """Record the exact START/REFRESH boundary of Holy Punishment EX."""
        rule = _REFRESHABLE_PULSING_AURA_RULES.get(raw_skill_id)
        if rule is None:
            rule = _REFRESHABLE_PULSING_AURA_RULES.get(resolved_skill_id)
        if (
            rule is None
            or source_actor_id <= 0
            or target_actor_id != source_actor_id
            or effect_class_id != int(rule["status_effect_class"])
            or str(parameters or "").strip()
            != str(rule["status_parameters"])
            or duration_ms <= 0
        ):
            return
        result_skill_id = int(rule["result_skill_id"])
        self.active_refreshable_auras[(source_actor_id, result_skill_id)] = (
            _ActivePulsingAura(
                source_actor_id=source_actor_id,
                raw_skill_id=raw_skill_id,
                source_skill_id=result_skill_id,
                effect_instance_id=max(0, effect_instance_id),
                applied_at=timestamp,
                expires_at=timestamp + duration_ms / 1000.0,
            )
        )
    def _is_inactive_player_special_attack(
        self,
        source_actor_id: int,
        target_actor_id: int | None,
        timestamp: float,
    ) -> bool:
        """Recognize action-free encounter attacks without masking skill hits."""
        if (
            target_actor_id is None
            or target_actor_id not in self.monster_entities
            or source_actor_id not in self.actor_skill_profiles
        ):
            return False
        actor_casts = self.recent_actor_casts.get(source_actor_id, ())
        if not actor_casts:
            return False
        latest_cast_time = actor_casts[-1][1]
        # Every known delayed, field, summon and periodic family has already
        # been offered ownership above. Requiring twelve action-free seconds
        # keeps this fallback away from ordinary delayed skill damage.
        return timestamp - latest_cast_time > 12.0


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'V576CombatDecoderMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
