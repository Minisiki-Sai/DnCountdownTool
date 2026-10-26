from __future__ import annotations

import hashlib
import json

from functools import lru_cache, wraps

from ..analytics import record_usage_event
from ..live.additional_damage_composition import attach_attributed_skill_views
from ..live.buff_clear import player_buff_clear_times_by_target
from ..live.combat_glyph_inference import (
    combat_glyph_signature,
    infer_combat_skill_glyphs,
)
from ..live.death_report import build_death_reports
from ..live.event_platform import event_platform_for
from ..live.damage_overview_archive import build_damage_overview_archives
from ..live.dungeon_map_catalog import (
    is_battle_map,
    is_battle_map_name,
    resolve_dungeon_map_id,
)
from ..live.movement import (
    MOVEMENT_SCAN_VERSION,
    build_movement_payload,
    cached_v576_diagnostic_movement,
)
from ..live.player_identity import resolve_player_identities
from ..live.dpscap_segments import post_capture_splitter
from ..live.replay_cache import replay_cache_identity_from_hash
from ..live.team_score_sync import remember_capture_content_hash
from ..live.boss_debuff_archive import encode_boss_debuff_archive
from ..live.score_timeline_display import score_timeline_display_context
from ..live.scoring import canonical_skill
from ..live.theory_plan_builder import (
    TEAM_SCORE_PROGRESS_EXCLUSION_END,
    TEAM_SCORE_PROGRESS_FINAL_END,
    TEAM_SCORE_PROGRESS_REPLAN_END,
    TEAM_SCORE_PROGRESS_TIMING_END,
)
from ..live.score_glyph_selection import prepare_score_glyphs
from ..live.score_equipment import apply_score_equipment, equipment_overrides
from ..live.score_equipment_uniform import apply_uniform_equipment, uniform_equipment_options, validate_uniform_settings
from ..live.score_glyph_objective import captured_glyph_damage_context, glyph_profile_for_source
from ..timeline_parallel import stable_score_worker_budget


def _retain_active_score_source(method):
    @wraps(method)
    def wrapped(self, payload, progress):
        source_hash = self.resolve_score_source_hash(norm(payload.get("sourceHash")))
        with self._state_lock:
            active = getattr(self, "_active_score_sources", None)
            if active is None:
                active = self._active_score_sources = set()
            already_active = source_hash in active
            active.add(source_hash)
        try:
            return method(self, payload, progress)
        finally:
            with self._state_lock:
                if not already_active:
                    active.discard(source_hash)
    return wrapped

def _yield_newer_scores(progress, preserve=None) -> None:
    """Only invoke between phases with no outstanding futures/shared planner use."""
    yield_callback = getattr(progress, "yield_to_newer_scores", None)
    if callable(yield_callback):
        yield_callback(preserve)

class NativeReplayScoreMixin:
    def _remember_boss_debuff_archive(self, source_hash: str, archive: dict[str, object]) -> None:
        with self._state_lock:
            cache = getattr(self, "_score_boss_debuffs_by_source", None)
            if cache is None:
                cache = self._score_boss_debuffs_by_source = {}
            cache[source_hash] = archive
            while len(cache) > 4:
                cache.pop(next(iter(cache)))

    def _schedule_stopped_capture_replay_cache(
        self,
        aggregator: CombatAggregator,
        source_file: str,
        source_hash: str,
    ) -> bool:
        """Persist the already decoded live aggregator after capture settles.

        The replay cache is the acceleration artifact.  It stores the complete
        decoded/aggregated battle independently from the original ``.dpscap``
        and therefore avoids another packet read and decode on the next load.
        No physical capture splitting is required.  The worker runs below
        normal priority and yields whenever a new capture starts.  The
        original capture remains the authority; a cache failure only loses
        acceleration.
        """

        capture_path = Path(source_file).expanduser().resolve()
        immutable_hash = norm(source_hash).casefold()
        if (
            not isinstance(aggregator, CombatAggregator)
            or not capture_path.is_file()
            or not immutable_hash
            or immutable_hash.startswith("live:")
        ):
            return False
        with self._state_lock:
            pending = getattr(self, "_replay_cache_warm_pending", None)
            if pending is None:
                pending = self._replay_cache_warm_pending = set()
            if immutable_hash in pending:
                return False
            pending.add(immutable_hash)
            ready_events = getattr(self, "_boss_debuff_archive_ready", None)
            if ready_events is None:
                ready_events = self._boss_debuff_archive_ready = {}
            archive_ready = ready_events[immutable_hash] = threading.Event()

        def warm_cache() -> None:
            try:
                try:
                    set_current_thread_low_priority()
                except Exception:
                    pass
                # Wait until the just-finished capture releases its activity
                # lease, then leave a short quiet window for score/page
                # publication.  Older builds waited for physical dpscap
                # splitting here, which delayed the useful decoded cache and
                # wrote a second copy of the raw packet stream.
                deadline = time.monotonic() + 15 * 60.0
                quiet_since = 0.0
                capture_was_released = False
                while time.monotonic() < deadline:
                    capture_active = post_capture_splitter.capture_active()
                    if capture_active:
                        # The first active state is the capture whose frozen
                        # aggregator we just received.  If another capture
                        # starts after that state was released, discard this
                        # optional warm-up instead of retaining a large old
                        # aggregator throughout the new battle.
                        if capture_was_released:
                            return
                        quiet_since = 0.0
                        time.sleep(0.25)
                        continue
                    capture_was_released = True
                    if quiet_since <= 0.0:
                        quiet_since = time.monotonic()
                    if time.monotonic() - quiet_since >= 2.0:
                        break
                    time.sleep(0.1)
                else:
                    return
                if post_capture_splitter.capture_active():
                    return
                try:
                    known_players = ClientProfile.load(
                        DATA_DIR / "profiles" / "client_profile.json"
                    ).known_players
                except (OSError, ValueError, KeyError):
                    known_players = {}
                identity = replay_cache_identity_from_hash(
                    immutable_hash,
                    DATA_DIR,
                    known_players,
                    capture_path=capture_path,
                )
                try:
                    with self._state_lock:
                        frozen = self._replays.get(immutable_hash)
                    if frozen is None:
                        frozen = aggregator.snapshot()
                    self._remember_boss_debuff_archive(immutable_hash, encode_boss_debuff_archive(
                        frozen, _boss_effect_views_payload(frozen, aggregator)))
                except Exception:
                    # Optional history projection must never block the decoded
                    # replay cache or prevent an otherwise valid score upload.
                    self._remember_boss_debuff_archive(immutable_hash, {
                        "v": 1, "unavailable": "Boss 状态账本生成失败；可重新载入原包评分后补传"})
                self._replay_disk_cache.save(identity, aggregator)
                remember_capture_content_hash(capture_path, immutable_hash)
            except Exception:
                pass
            finally:
                with self._state_lock:
                    self._replay_cache_warm_pending.discard(immutable_hash)
                    if immutable_hash not in getattr(self, "_score_boss_debuffs_by_source", {}):
                        self._remember_boss_debuff_archive(immutable_hash, {
                            "v": 1, "unavailable": "状态账本后台任务已让出资源；可重新载入原包评分后补传"})
                    archive_ready.set()
                    self._boss_debuff_archive_ready.pop(immutable_hash, None)

        threading.Thread(
            target=warm_cache,
            name="DpscapReplayCacheWarm",
            daemon=True,
        ).start()
        return True

    def load_replay(self, path_text: str, progress: Callable[[int, str], None]) -> dict[str, Any]:
        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"找不到实战记录：{path}")
        data_root = DATA_DIR
        profile_path = DATA_DIR / "profiles" / "client_profile.json"
        try:
            known_players = ClientProfile.load(profile_path).known_players
        except (OSError, ValueError, KeyError):
            known_players = {}
        suffix = path.suffix.casefold()
        cache_identity = None
        aggregator = None
        replay_cache_hit = False
        replay_cache_needs_save = False
        replay_map_history: tuple[dict[str, object], ...] = ()
        replay_movement_capture = None
        if suffix == ".dpscap":
            progress(1, "正在核对记录与解析器版本指纹...")
            try:
                cache_identity = replay_cache_identity(path, data_root, known_players)
                aggregator = self._replay_disk_cache.load(cache_identity)
            except Exception:
                cache_identity = None
                aggregator = None
            replay_cache_hit = aggregator is not None
            if replay_cache_hit:
                replay_map_history = tuple(
                    dict(row)
                    for row in getattr(
                        aggregator, "_replay_map_history", ()
                    )
                    if isinstance(row, dict)
                )
                progress(88, "版本指纹一致，已恢复完整解析缓存")
        if aggregator is None:
            if suffix == ".dpscap":
                source = V576DiagnosticReplaySource(
                    path,
                    data_root,
                    known_players,
                    progress_callback=lambda done, total: progress(round(done * 88 / max(1, total)), "正在读取并解码实战记录"),
                )
            elif suffix == ".pcapng":
                source = V576PcapReplaySource(path, data_root, known_players)
            else:
                source = JsonlReplaySource(path)
            aggregator = CombatAggregator()
            aggregated_events = 0
            hit_recorder = None
            if suffix == ".dpscap":
                from ..live.replay_hit_evidence import start_recorder
                try:
                    hit_recorder = start_recorder(source, path, data_root)
                except Exception:
                    # An absent optional Hit-tool component/cache must not
                    # prevent normal replay loading.
                    hit_recorder = None
            decode_succeeded = False
            try:
                if suffix == ".dpscap" and hasattr(source, "iter_batches"):
                    for batch in source.iter_batches():
                        if hit_recorder is not None:
                            hit_recorder.append(batch)
                        total_events = max(
                            1,
                            int(getattr(source, "total_event_count", 0)),
                        )
                        for event in batch:
                            aggregator.ingest(event)
                        aggregated_events += len(batch)
                        progress(
                            88 + round(aggregated_events * 10 / total_events),
                            f"正在流式汇总 {aggregated_events:,}/{total_events:,} 条事件",
                        )
                else:
                    events = source.poll()
                    total_events = max(1, len(events))
                    for index, event in enumerate(events, 1):
                        aggregator.ingest(event)
                        if index % 1000 == 0:
                            progress(88 + round(index * 10 / total_events), f"正在汇总 {index:,}/{len(events):,} 条事件")
                decode_succeeded = True
            finally:
                if hit_recorder is not None:
                    try:
                        hit_recorder.finish(decode_succeeded)
                    except Exception:
                        # Optional reuse must not prevent replay/scoring. The
                        # existing independent Hit decode remains available.
                        pass
                source_decoder = getattr(source, "_decoder", None)
                replay_movement_capture = getattr(
                    source, "movement_capture", None
                )
                replay_map_history = tuple(
                    dict(row)
                    for row in getattr(
                        source_decoder,
                        "detected_map_history",
                        (),
                    )
                    if isinstance(row, dict)
                )
                if not replay_map_history and source_decoder is not None:
                    detected_name = str(
                        getattr(source_decoder, "detected_nest_name", "")
                        or ""
                    ).strip()
                    detected_label = str(
                        getattr(source_decoder, "detected_nest_label", "")
                        or detected_name
                    ).strip()
                    confidence = str(
                        getattr(
                            source_decoder,
                            "detected_nest_confidence",
                            "",
                        )
                        or ""
                    ).strip()
                    if detected_name and confidence:
                        try:
                            map_id = max(
                                0,
                                int(
                                    getattr(
                                        source_decoder,
                                        "detected_map_id",
                                        0,
                                    )
                                    or 0
                                ),
                            )
                        except (TypeError, ValueError):
                            map_id = 0
                        replay_map_history = (
                            {
                                "map_id": map_id,
                                "map_name": str(
                                    getattr(
                                        source_decoder,
                                        "detected_map_name",
                                        "",
                                    )
                                    or detected_name
                                ).strip(),
                                "nest_name": detected_label,
                                "official_name": detected_name,
                                "content_kind": "dungeon",
                                "observed_at": max(
                                    0.0,
                                    float(
                                        getattr(
                                            source_decoder,
                                            "detected_nest_observed_at",
                                            0.0,
                                        )
                                        or 0.0
                                    ),
                                ),
                                "layout": (
                                    str(
                                        getattr(
                                            source_decoder,
                                            "detected_map_layout",
                                            "",
                                        )
                                        or ""
                                    ).strip()
                                    or "s2c-udp-formal-map-entity-fallback-v576"
                                ),
                                "confidence": confidence,
                                "template_id": int(
                                    getattr(
                                        source_decoder,
                                        "detected_nest_template_id",
                                        0,
                                    )
                                    or 0
                                ),
                                "inferred": confidence != "exact_map_id",
                            },
                        )
                source.close()
            if replay_movement_capture is not None:
                # Persist the independent movement ledger beside the parsed
                # replay so a later tab open or application restart does not
                # reread the complete capture.
                aggregator._movement_capture = replay_movement_capture
            if replay_map_history:
                aggregator._replay_map_history = replay_map_history
            replay_cache_needs_save = cache_identity is not None
        if suffix == ".dpscap" and not replay_map_history:
            replay_map_history = scan_v576_diagnostic_map_history(path)
        if replay_movement_capture is None:
            replay_movement_capture = getattr(
                aggregator, "_movement_capture", None
            )
        if replay_movement_capture is not None:
            try:
                movement_scan_version = int(
                    replay_movement_capture.diagnostics.get("scan_version") or 0
                )
            except (AttributeError, TypeError, ValueError):
                movement_scan_version = 0
            if movement_scan_version < MOVEMENT_SCAN_VERSION:
                # Combat replay caches are intentionally long-lived. Movement
                # decoding has its own schema because an old, otherwise valid
                # combat cache may still contain a late client map candidate as
                # the only room. Force only the inexpensive movement rescan.
                replay_movement_capture = None
        # Cache persistence and full page projection are separate stages.
        # The latter used to run while the UI still displayed the 98% cache
        # message, making a slow effect-axis build look like slow disk I/O.
        cached_snapshot = getattr(aggregator, "_replay_snapshot", None)
        replay_snapshot_hit = cached_snapshot is not None
        if replay_snapshot_hit:
            progress(99, "已恢复完整统计快照...")
            snapshot = cached_snapshot
        else:
            progress(99, "正在生成完整统计快照...")
            snapshot = aggregator.snapshot(include_events=True)
            aggregator._replay_snapshot = snapshot
            # Upgrade an older aggregator-only cache once.  Subsequent loads
            # can then skip the expensive snapshot projection as well.
            if replay_cache_hit and cache_identity is not None:
                replay_cache_needs_save = True
        if replay_cache_needs_save and cache_identity is not None:
            progress(99, "载入完成，版本缓存将在后台保存")
            self._replay_disk_cache.save_async(cache_identity, aggregator)
        platform = event_platform_for(snapshot)
        source_hash = (
            cache_identity.capture_hash
            if cache_identity is not None
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        with self._state_lock:
            movement_captures = getattr(
                self, "_replay_movement_captures", None
            )
            if movement_captures is None:
                movement_captures = self._replay_movement_captures = {}
            self._replays[source_hash] = snapshot
            self._replay_sources[source_hash] = path
            self._replay_map_history[source_hash] = replay_map_history
            if replay_movement_capture is not None:
                movement_captures[source_hash] = (
                    replay_movement_capture
                )
            # Keep memory bounded while making repeated page switches instant.
            while len(self._replays) > 4:
                protected = getattr(self, "_active_score_sources", set()) | {source_hash}
                oldest = next((key for key in self._replays if key not in protected), None)
                if oldest is None:
                    break
                self._replays.pop(oldest, None)
                self._score_encounters_by_replay.pop(oldest, None)
                self._replay_map_history.pop(oldest, None)
                movement_captures.pop(oldest, None)
                old_source = self._replay_sources.pop(oldest, None)
                if old_source is not None:
                    try:
                        upload_root = (
                            Path(os.environ.get("TEMP", ""))
                            / "DragonNestSimulatorV410"
                            / "replay_uploads"
                        ).resolve()
                        old_source.resolve().relative_to(upload_root)
                        old_source.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass
        # Keep the complete snapshot in the backend for score generation.  The
        # frontend only needs a compact projection; sending raw attribution and
        # incoming-hit fields made long captures spend many seconds serializing
        # data that no V4.10 page reads.
        payload_fields = (
            "elapsed_seconds", "total_damage", "total_healing", "players",
            "damage_series", "damage_targets", "boss_effects",
            "boss_effect_events", "boss_effect_summary", "boss_effect_series",
            "boss_effect_current_seconds", "player_buffs", "player_buff_events",
            "player_buff_series", "incoming_damage_events",
            "skill_cast_events", "boss_action_events", "player_buff_clear_events",
            "dps_elapsed_seconds", "instance_completion",
            "raw_damage_total", "attributed_damage_total",
            "unresolved_damage_total", "inferred_cast_count",
            "reassigned_hit_count", "raw_healing_total",
            "factual_healing_total", "attributed_healing_total",
            "unattributed_healing_total", "suppressed_overheal_total",
            "healing_matched_event_count",
            "healing_unattributed_event_count",
            "healing_ambiguous_event_count", "healing_attribution_methods",
            "boss_hp_tracks", "party_members",
            "player_profiles", "player_health_tracks",
        )
        payload = {
            field: _json_safe(platform.field(field))
            for field in payload_fields
        }
        encounters = tuple(BossRecognizer().recognize_snapshot(snapshot))
        with self._state_lock:
            self._score_encounters_by_replay[source_hash] = encounters
        payload["boss_encounters"] = _boss_encounter_payload(
            snapshot, encounters, str(path)
        )
        payload["boss_hp_curves"] = _boss_hp_curve_payload(
            snapshot, str(path), encounters
        )
        payload["boss_effect_views"] = _boss_effect_views_payload(
            snapshot, aggregator, encounters
        )
        self._remember_boss_debuff_archive(source_hash, encode_boss_debuff_archive(
            snapshot, payload["boss_effect_views"]))
        payload["skill_cast_events"] = self._skill_cast_payloads(snapshot)
        # Post-capture statistics only. Never put this full-event calculation
        # on the decoder/overlay path. Same service is used by score preparation.
        castability = TeamScoreEngine(self.store).castability_by_source(snapshot)
        payload["castability_windows_by_source"] = {
            actor: [list(w) for w in evidence.windows]
            for actor, evidence in castability.items()
        }
        payload["combat_events"] = [
            {
                "timestamp_ms": event.timestamp_ms,
                "source_id": event.source_id,
                "source_name": event.source_name,
                "job": event.job,
                "skill_id": event.skill_id,
                "skill_name": event.skill_name,
                "amount": event.amount,
                "critical": event.critical,
                "healing": event.healing,
                "target_id": event.target_id,
                "target_name": event.target_name,
                "target_job": event.target_job,
                "has_real_hit_evidence": event.has_real_hit_evidence,
            }
            for event in platform.combat_events
        ]
        attach_attributed_skill_views(payload, snapshot)
        payload["death_reports"] = build_death_reports(snapshot)
        payload["sourceFile"] = str(path)
        payload["sourceHash"] = source_hash
        payload["dungeon_map_history"] = [
            dict(row) for row in replay_map_history
        ]
        replay_battle_maps = [
            row
            for row in replay_map_history
            if (
                is_battle_map(resolve_dungeon_map_id(row.get("map_id")))
                or (
                    int(row.get("map_id") or 0) == 0
                    and str(row.get("content_kind") or "") == "dungeon"
                )
            )
        ]
        payload["dungeon_map"] = (
            dict(replay_battle_maps[-1]) if replay_battle_maps else None
        )
        payload["current_map"] = (
            dict(replay_map_history[-1]) if replay_map_history else None
        )
        payload["versionCache"] = {
            "replayHit": replay_cache_hit,
            "snapshotHit": replay_snapshot_hit,
            "rawCaptureDecoded": not replay_cache_hit,
            "storage": "decoded-replay-folder-v3",
            "captureHash": source_hash,
            "decoderFingerprint": (
                cache_identity.decoder_fingerprint if cache_identity else ""
            ),
        }
        return self._with_profile_panels(payload)

    @staticmethod
    def _movement_actor_identities(
        snapshot: CombatSnapshot,
        encounters: tuple[BossEncounter, ...] = (),
    ) -> dict[str, dict[str, object]]:
        """Build display metadata for MovementLedger without guessing names.

        Runtime positions are protocol evidence, but a runtime id by itself is
        not a stable character identity.  Prefer the same reliable party and
        inspect bindings used by the rest of the replay UI; combat labels only
        fill otherwise-empty job/name fields and can never replace stronger
        evidence.
        """

        platform = event_platform_for(snapshot)
        result: dict[str, dict[str, object]] = {}

        def canonical_actor_id(value: object) -> str:
            text = str(value or "").strip()
            if not text.casefold().startswith("0x"):
                return ""
            # Boss tracks use ``runtime-id@template-id`` so one reused runtime
            # slot can still distinguish stage instances. Movement packets
            # carry only the runtime portion.
            text = text.split("@", 1)[0].strip()
            try:
                return f"0x{int(text[2:], 16) & 0xFFFFFFFF:08X}"
            except ValueError:
                return ""

        def usable_name(value: object, actor_id: str) -> str:
            text = str(value or "").strip()
            folded = text.casefold()
            if (
                not text
                or folded == actor_id.casefold()
                or folded.startswith("角色 0x")
                or folded in {"未知角色", "未知职业"}
            ):
                return ""
            return text

        def offer(
            actor_value: object,
            *,
            name: object = "",
            job: object = "",
            profile_id: object = "",
            kind: str = "entity",
            priority: int = 0,
        ) -> None:
            actor_id = canonical_actor_id(actor_value)
            if not actor_id:
                return
            actor_text = str(actor_value or "").strip()
            template_text = actor_text.rsplit("@", 1)[-1] if "@" in actor_text else ""
            template_id = int(template_text) if template_text.isdigit() else 0
            candidate_name = usable_name(name, actor_id)
            candidate_job = str(job or "").strip()
            candidate_profile_id = str(profile_id or "").strip()
            current = result.setdefault(
                actor_id,
                {
                    "name": "",
                    "job": "",
                    "profileId": "",
                    "kind": kind,
                    "priority": -1,
                    "templateIds": set(),
                },
            )
            if template_id > 0:
                template_ids = current.setdefault("templateIds", set())
                if isinstance(template_ids, set):
                    template_ids.add(template_id)
            current_priority = int(current.get("priority", -1))
            if priority >= current_priority:
                if candidate_name:
                    current["name"] = candidate_name
                if candidate_job and candidate_job != "未知职业":
                    current["job"] = candidate_job
                if candidate_profile_id:
                    current["profileId"] = candidate_profile_id
                if kind != "entity" or current.get("kind") == "entity":
                    current["kind"] = kind
                current["priority"] = priority
            else:
                if not current.get("name") and candidate_name:
                    current["name"] = candidate_name
                if not current.get("job") and candidate_job != "未知职业":
                    current["job"] = candidate_job
                if not current.get("profileId") and candidate_profile_id:
                    current["profileId"] = candidate_profile_id

        def offer_template_visibility(
            actor_value: object,
            *,
            first_seconds: object,
            last_seconds: object,
        ) -> None:
            """Keep lifetimes per template because runtime IDs can be reused."""

            actor_id = canonical_actor_id(actor_value)
            actor_text = str(actor_value or "").strip()
            template_text = (
                actor_text.rsplit("@", 1)[-1] if "@" in actor_text else ""
            )
            if not actor_id or not template_text.isdigit():
                return
            try:
                visible_from = max(0.0, float(first_seconds))
                visible_until = max(visible_from, float(last_seconds))
            except (TypeError, ValueError):
                return
            current = result.get(actor_id)
            if current is None:
                return
            ranges = current.setdefault("templateVisibility", {})
            if not isinstance(ranges, dict):
                ranges = current["templateVisibility"] = {}
            existing = ranges.get(template_text)
            if isinstance(existing, dict):
                visible_from = min(
                    visible_from,
                    float(existing.get("visibleFrom") or visible_from),
                )
                visible_until = max(
                    visible_until,
                    float(existing.get("visibleUntil") or visible_until),
                )
            ranges[template_text] = {
                "visibleFrom": visible_from,
                "visibleUntil": visible_until,
            }
            current["visibleFrom"] = min(
                visible_from,
                float(current.get("visibleFrom") or visible_from),
            )
            current["visibleUntil"] = max(
                visible_until,
                float(current.get("visibleUntil") or visible_until),
            )

        def offer_encounter_visibility(
            actor_value: object,
            *,
            first_ms: object,
            last_ms: object,
        ) -> None:
            """Bind a runtime-only Boss target to its recognized encounter.

            Older captures may start after the spawn/template packet.  The
            Boss recognizer can still prove the encounter from damage and the
            fixed nest profile, while a runtime ID may later be reused by an
            ordinary monster.  Keeping the encounter lifetime here prevents
            that later reuse from erasing or extending the Boss track.
            """

            actor_id = canonical_actor_id(actor_value)
            current = result.get(actor_id)
            if current is None:
                return
            try:
                visible_from = max(0.0, float(first_ms) / 1000.0 - 3.0)
                visible_until = max(
                    visible_from,
                    float(last_ms) / 1000.0 + 3.0,
                )
            except (TypeError, ValueError):
                return
            current["visibleFrom"] = min(
                visible_from,
                float(current.get("visibleFrom") or visible_from),
            )
            current["visibleUntil"] = max(
                visible_until,
                float(current.get("visibleUntil") or visible_until),
            )

        # Packet-backed identities are strongest, including duplicate jobs.
        for identity in resolve_player_identities(
            platform.players,
            platform.party_members,
            platform.player_profiles,
        ):
            offer(
                identity.source_id,
                name=identity.character_name,
                job=identity.job,
                profile_id=identity.profile_id,
                kind="player",
                priority=100,
            )
        for row in platform.party_members:
            offer(
                row.actor_id,
                name=row.name,
                job=row.job,
                kind="player",
                priority=95,
            )
        for row in platform.player_profiles:
            offer(
                row.target_id,
                name=row.target_name,
                job=row.job,
                kind="player",
                priority=90,
            )
        for row in platform.players:
            offer(
                row.source_id,
                # PlayerSnapshot names can contain a historical local-profile
                # fallback.  Job is safe for presentation; names come only
                # from the reliable party/inspect identity rows above.
                job=row.job,
                kind="player",
                priority=75,
            )
        for row in platform.player_health_tracks:
            offer(
                row.target_id,
                name=row.target_name,
                job=row.target_job,
                kind="player",
                priority=70,
            )
        for row in platform.boss_hp_tracks:
            offer(
                row.target_id,
                name=row.target_name,
                job=row.target_job,
                kind="boss",
                priority=85,
            )
            # Damage/HP evidence starts a little after spawn and ends a little
            # before the death animation. Keep a short grace on both sides.
            offer_template_visibility(
                row.target_id,
                first_seconds=float(row.first_seconds) - 3.0,
                last_seconds=max(
                    float(row.first_seconds),
                    float(row.last_seconds),
                ) + 3.0,
            )
        # A capture can begin after the first room Boss has already spawned.
        # Such a target has no template-backed BossHpTrack but is still a
        # recognized Boss encounter.  Feed that authoritative recognition into
        # MovementLedger instead of leaving thousands of verified body points
        # in the unconfirmed-entity bucket.
        for encounter in encounters:
            for target_id in encounter.target_ids:
                offer(
                    target_id,
                    name=encounter.name,
                    job="Boss",
                    kind="boss",
                    priority=88,
                )
                offer_encounter_visibility(
                    target_id,
                    first_ms=encounter.first_ms,
                    last_ms=encounter.last_ms,
                )
        for row in platform.damage_targets:
            target_job = str(row.target_job or "")
            actor_id = canonical_actor_id(row.target_id)
            already_boss = bool(
                actor_id and result.get(actor_id, {}).get("kind") == "boss"
            )
            target_kind = (
                "boss"
                if target_job.casefold().startswith("boss") or already_boss
                else "entity"
            )
            offer(
                row.target_id,
                name=row.target_name,
                job=target_job,
                kind=target_kind,
                priority=60,
            )
            if row.first_ms > 0:
                # Some real nest bosses are catalogued as ordinary Lv60
                # entities and therefore never produce a BossHpTrack. Their
                # template and damage lifetime are still authoritative room
                # evidence for MovementLedger.
                offer_template_visibility(
                    row.target_id,
                    first_seconds=float(row.first_ms) / 1000.0 - 3.0,
                    last_seconds=max(row.first_ms, row.last_ms) / 1000.0 + 3.0,
                )
        for row in platform.combat_events:
            source_id = canonical_actor_id(row.source_id)
            source_kind = str(result.get(source_id, {}).get("kind") or "entity")
            if str(row.job or "").casefold().startswith("boss"):
                source_kind = "boss"
            offer(
                row.source_id,
                job=row.job,
                kind=source_kind,
                priority=50,
            )
            offer(
                row.target_id,
                name=row.target_name,
                job=row.target_job,
                kind=(
                    "boss"
                    if str(row.target_job or "").casefold().startswith("boss")
                    else "entity"
                ),
                priority=40,
            )
        for value in result.values():
            value.pop("priority", None)
            template_ids = value.get("templateIds")
            if isinstance(template_ids, set):
                value["templateIds"] = sorted(template_ids)
        return result

    def replay_movement(self, source_hash: object) -> dict[str, object]:
        """Return an independently decoded X/Y replay for a loaded capture."""

        requested = norm(source_hash)
        with self._state_lock:
            snapshot = self._replays.get(requested)
            capture_path = self._replay_sources.get(requested)
            map_history = self._replay_map_history.get(requested, ())
            encounters = self._score_encounters_by_replay.get(requested, ())
            capture = getattr(
                self, "_replay_movement_captures", {}
            ).get(requested)
        if snapshot is None or capture_path is None:
            raise KeyError("replay_not_loaded")
        if capture_path.suffix.casefold() != ".dpscap":
            raise ValueError("MovementLedger 目前只支持原始 .dpscap 记录")
        if capture is None:
            capture, cache_hit = cached_v576_diagnostic_movement(
                capture_path,
                source_hash=requested,
            )
        else:
            cache_hit = True
        return build_movement_payload(
            capture,
            source_hash=requested,
            source_file=str(capture_path),
            identities=self._movement_actor_identities(snapshot, encounters),
            map_history=map_history,
            cache_hit=cache_hit,
        )
    @staticmethod
    def _blocked_complement(
        output_windows: tuple[tuple[float, float], ...],
        end_seconds: float,
    ) -> list[list[float]]:
        blocked: list[list[float]] = []
        cursor = 0.0
        for start, end in sorted(output_windows):
            start = max(0.0, min(end_seconds, float(start)))
            end = max(start, min(end_seconds, float(end)))
            if start > cursor + 1e-6:
                blocked.append([cursor, start])
            cursor = max(cursor, end)
        if cursor < end_seconds - 1e-6:
            blocked.append([cursor, end_seconds])
        return blocked
    @staticmethod
    def _modifier_intervals(
        snapshot: CombatSnapshot,
        names: set[str],
    ) -> list[list[float]]:
        rows: list[list[float]] = []
        for series in event_platform_for(snapshot).player_buff_series:
            if norm(series.skill_name) not in names and norm(series.label) not in names:
                continue
            rows.extend([float(start), float(end)] for start, end in series.intervals)
        rows.sort(key=lambda value: (value[0], value[1]))
        merged: list[list[float]] = []
        for start, end in rows:
            if not merged or start > merged[-1][1] + 1e-6:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return merged
    def _score_theory_plans(
        self,
        snapshot: CombatSnapshot,
        score_engine: TeamScoreEngine,
        boss_target_ids: frozenset[str] | None,
        progress: Callable[[int, str], None],
        *,
        inferred_glyphs_by_source: dict | None = None,
        glyph_overrides: dict | None = None,
    ) -> dict[str, dict[str, Any]]:
        output_windows = score_engine._output_windows(snapshot, boss_target_ids)
        platform = event_platform_for(snapshot)
        end_seconds = max(5.0, float(platform.elapsed_seconds))
        settings = {
            "window": end_seconds,
            "cocktail": True,
            "blockedIntervals": self._blocked_complement(output_windows, end_seconds),
            "burstIntervals": self._modifier_intervals(snapshot, {"精神爆发"}),
            "accelIntervals": self._modifier_intervals(snapshot, {"时间加速"}),
        }
        base = self.get_build("build1")
        plans: dict[str, dict[str, Any]] = {}
        players = tuple(player for player in platform.players if norm(player.job))
        glyph_context = captured_glyph_damage_context(
            snapshot, players, self.store, output_windows, boss_target_ids)
        for index, player in enumerate(players, 1):
            job = norm(player.job)
            build = copy.deepcopy(base)
            build["second_job"] = job
            for first in self.store.first_jobs():
                if job in self.store.second_jobs(first):
                    build["first_job"] = first
                    break
            self.apply_job_defaults(build, job)
            if inferred_glyphs_by_source is not None:
                overrides = glyph_overrides or {}
                # Configure the actual fallback build before simulating it;
                # changing only the returned label would leave its axis wrong.
                editor = self.legacy_timeline.extreme_runtime(copy.deepcopy(build))
                prepare_score_glyphs(editor, build, job,
                    inferred_rows=inferred_glyphs_by_source.get(player.source_id, []),
                    manual_rows=overrides.get(player.source_id, overrides.get(job)),
                    team_size=len(players), team_objective_profile=glyph_profile_for_source(
                        glyph_context, player.source_id))
            catalog = self.skill_rows(build)
            native_plan = self.quick_timeline(build, {**settings, "mode": "long"})
            segments = []
            for row in native_plan.get("segments", []):
                item = copy.deepcopy(row)
                score = max(0.0, as_number(item.get("effectiveScore")))
                item.update(
                    raw_effective_score=score,
                    effective_score=score,
                    action_speed_percent=max(0.0, as_number(item.get("actionSpeed"))),
                    action_speed_output_multiplier=1.0,
                )
                segments.append(item)
            plans[player.source_id] = {
                "segments": segments,
                "effect_windows": copy.deepcopy(native_plan.get("effectWindows", [])),
                "external_windows": [],
                "summary": copy.deepcopy(catalog.get("summary", {})),
                "_score_time_offset": 0.0,
                "_score_glyph_source": (build.get("skill_glyphs") or {}).get("source", "automatic"),
                "_score_glyphs": copy.deepcopy(
                    (build.get("skill_glyphs") or {}).get("rows", [])
                    if isinstance(build.get("skill_glyphs"), dict)
                    else []
                ),
            }
            progress(
                10 + round(index * 42 / max(1, len(players))),
                f"正在生成 {job} 理论轴（{index}/{len(players)}）",
            )
        return plans
    def score_sync_settings(self) -> dict[str, Any]:
        return self._score_sync.status()
    def score_history_benchmark(self, nest: object, job: object) -> dict[str, Any]:
        return self._score_sync.benchmark(str(nest or ""), str(job or ""))
    def database_profession_ranking(
        self,
        *,
        section: object = "professions",
        composition: object = "",
        nest: object = "",
        score_schema_versions: object = (),
        completion_status: object = "",
        recorded_from: object = "",
        recorded_to: object = "",
        profile_id: object = "",
        character_name: object = "",
        job: object = "",
        force_refresh: object = False,
    ) -> dict[str, Any]:
        return self._score_sync.database_profession_ranking(
            section=section,
            composition=composition,
            nest=nest,
            score_schema_versions=score_schema_versions,
            completion_status=completion_status,
            recorded_from=recorded_from,
            recorded_to=recorded_to,
            profile_id=profile_id,
            character_name=character_name,
            job=job,
            force_refresh=force_refresh,
        )
    def unlock_score_sync_settings(self, password: object) -> dict[str, Any]:
        return self._score_sync.unlock(password)
    def save_score_sync_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._score_sync.save(payload, payload.get("adminPassword"))
    def set_capture_file_upload_enabled(
        self, enabled: object, password: object = None
    ) -> dict[str, Any]:
        return self._score_sync.set_capture_file_upload_enabled(enabled, password)
    def test_score_sync(self, password: object) -> dict[str, Any]:
        return self._score_sync.test_and_flush(password)
    def anonymous_usage_statistics(
        self,
        password: object = None,
        days: object = 30,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        return self._score_sync.usage_statistics(
            password,
            days=days,
            force_refresh=force_refresh,
        )
    def record_anonymous_usage_event(
        self,
        event: object,
        event_id: object = "",
    ) -> dict[str, Any]:
        return {
            "accepted": record_usage_event(
                str(event or ""),
                str(event_id or ""),
            )
        }
    def character_score_history(
        self,
        character_name: object,
        profile_id: object = "",
        limit: object = 20,
        nest: object = "",
        completion_status: object = "",
        score_schema_versions: object = (),
        cursor: object = "",
        include_facets: object = True,
        force_refresh: object = False,
        surface: object = "",
    ) -> dict[str, Any]:
        return self._score_sync.character_history(
            character_name,
            profile_id=profile_id,
            limit=limit,
            nest=nest,
            completion_status=completion_status,
            score_schema_versions=score_schema_versions,
            cursor=cursor,
            include_facets=include_facets,
            force_refresh=bool(force_refresh),
            surface=surface,
        )

    def personal_score_overlay_visibility(
        self,
        hidden: object,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        """Sync visibility only for the packet-confirmed current character."""

        identity_provider = getattr(self.live_capture, "self_identity_status", None)
        identity = identity_provider() if callable(identity_provider) else {}
        profile_id = int(identity.get("profileId") or 0)
        hidden_value = bool(hidden)
        if profile_id <= 0 or identity.get("state") != "ready":
            return {
                "state": "pending_identity",
                "message": "尚未识别当前登录角色，识别后将自动同步此设置",
                "profileId": "",
                "characterName": "",
                "hidden": hidden_value,
            }
        try:
            return self._score_sync.set_profile_overlay_visibility(
                profile_id,
                hidden_value,
                character_name=identity.get("characterName", ""),
                force_refresh=force_refresh,
            )
        except Exception as exc:
            message = str(exc)
            if (
                "unsupported_action" in message
                or "set_profile_score_overlay_visibility" in message
            ):
                message = (
                    "云端悬浮窗隐私服务尚未部署，请执行数据库 027 并部署 2.11.0 云函数"
                )
            return {
                "state": "error",
                "message": message,
                "profileId": f"0x{profile_id:08X}",
                "characterName": str(identity.get("characterName") or ""),
                "hidden": hidden_value,
            }

    def personal_score_visibility(
        self,
        overlay_hidden: object,
        software_hidden: object,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        """Sync both privacy scopes for the packet-confirmed character."""

        identity_provider = getattr(self.live_capture, "self_identity_status", None)
        identity = identity_provider() if callable(identity_provider) else {}
        profile_id = int(identity.get("profileId") or 0)
        overlay_value = bool(overlay_hidden)
        software_value = bool(software_hidden)
        if profile_id <= 0 or identity.get("state") != "ready":
            return {
                "state": "pending_identity",
                "message": "尚未识别当前登录角色，识别后将自动同步此设置",
                "profileId": "",
                "characterName": "",
                "overlayHidden": overlay_value,
                "softwareHidden": software_value,
            }
        try:
            return self._score_sync.set_profile_score_visibility(
                profile_id,
                overlay_value,
                software_value,
                character_name=identity.get("characterName", ""),
                force_refresh=force_refresh,
            )
        except Exception as exc:
            message = str(exc)
            if (
                "unsupported_action" in message
                or "set_profile_score_visibility" in message
            ):
                message = (
                    "云端评分隐私服务尚未部署，请执行数据库 028 并部署 2.12.0 云函数"
                )
            return {
                "state": "error",
                "message": message,
                "profileId": f"0x{profile_id:08X}",
                "characterName": str(identity.get("characterName") or ""),
                "overlayHidden": overlay_value,
                "softwareHidden": software_value,
            }

    def character_score_history_detail(
        self,
        character_name: object,
        history_id: object,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        return self._score_sync.character_history_detail(
            character_name,
            history_id,
            force_refresh=bool(force_refresh),
        )

    def profession_score_history(
        self,
        job: object,
        **filters: object,
    ) -> dict[str, Any]:
        return self._score_sync.profession_history(job, **filters)

    def profession_score_history_detail(
        self,
        job: object,
        history_id: object,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        return self._score_sync.profession_history_detail(
            job,
            history_id,
            force_refresh=bool(force_refresh),
        )

    @staticmethod
    def _score_character_identities(
        snapshot: CombatSnapshot,
    ) -> dict[str, dict[str, Any]]:
        """Return only names backed by packet/profile identity evidence.

        Combat rows can inherit a stale friendly name from the local decoder
        profile.  Historical scores must never use that fallback.  Require a
        party identity, an exact inspect binding, or exact OCR/name evidence
        before a character name is allowed into the private history table.
        """

        strong_evidence = {
            "party_actor_name",
            "inspect_direct_name",
            "inspect_exact_name",
            "ocr_exact_name",
        }
        result: dict[str, dict[str, Any]] = {}
        for identity in resolve_player_identities(
            event_platform_for(snapshot).players,
            event_platform_for(snapshot).party_members,
            event_platform_for(snapshot).player_profiles,
        ):
            evidence = set(identity.evidence)
            name = str(identity.character_name or "").strip()
            if (
                not identity.upload_allowed
                or not name
                or identity.confidence < 0.90
                or not evidence.intersection(strong_evidence)
            ):
                continue
            source = next(
                (
                    value
                    for value in (
                        "inspect_direct_name",
                        "inspect_exact_name",
                        "party_actor_name",
                        "ocr_exact_name",
                    )
                    if value in evidence
                ),
                "verified_identity",
            )
            result[str(identity.source_id).upper()] = {
                "character_name": name,
                "profile_id": identity.profile_id,
                "source": source,
                "confidence": float(identity.confidence),
                "trusted": True,
            }
        return result
    def _queue_replay_score_sync(
        self,
        source_hash: str,
        snapshot: CombatSnapshot,
        rows: tuple[PlayerScore, ...],
        boss_target_ids: frozenset[str] | None,
        *,
        force_upload: bool = False,
    ) -> None:
        # Match V4.01: cloud history stores one full-run score set per capture,
        # never a score recalculated for only one selected Boss.
        if boss_target_ids:
            return
        with self._state_lock:
            capture_path = self._replay_sources.get(source_hash)
        if capture_path is None or not capture_path.is_file() or not rows:
            return
        encounters = self._score_encounters(source_hash, snapshot)
        selected = set(boss_target_ids or ())
        selected_encounters = tuple(
            encounter
            for encounter in encounters
            if not selected or selected.intersection(encounter.target_ids)
        ) or encounters
        character_identities = self._score_character_identities(snapshot)
        actual_dps_by_source = {
            str(player.source_id).upper(): max(0.0, float(player.dps))
            for player in event_platform_for(snapshot).players
        }
        map_ids, map_names = self._replay_map_evidence(source_hash)

        def prepare_and_enqueue() -> None:
            # Historical timelines are a post-score projection. Resolve the
            # same cast durations used by the visible full-team timeline, then
            # attach only this player's rows before the sync store compacts
            # them. Nothing here is fed back into scoring.
            resolved_casts = tuple(self._skill_cast_payloads(snapshot))
            with self._state_lock:
                archive_ready = getattr(self, "_boss_debuff_archive_ready", {}).get(source_hash)
            # Only the post-score export worker waits. Packet capture, overlay
            # publication and score/UI completion never wait for this ledger.
            if archive_ready is not None:
                archive_ready.wait(timeout=30.0)
            with self._state_lock:
                boss_debuffs = getattr(self, "_score_boss_debuffs_by_source", {}).get(source_hash)
            if boss_debuffs is None:
                boss_debuffs = {"v": 1, "unavailable": "此记录未生成状态账本；可重新载入原包评分后补传"}
            player_buff_rows = tuple(
                dataclasses.asdict(buff)
                for buff in event_platform_for(snapshot).player_buff_series
            )
            display_hp_curves = _boss_hp_curve_payload(snapshot, str(capture_path), selected_encounters)
            display_engine = TeamScoreEngine(self.store)
            from ..live.team_score_sync import attach_team_output_time
            team_output_windows = display_engine._output_windows(snapshot, frozenset(
                target_id for encounter in selected_encounters for target_id in encounter.target_ids))
            from ..live.skill_realization import replay_skill_realization
            realization_rows = ()
            realization_message = ""
            try:
                # Post-score worker only. Uses the same cached evidence and
                # full-capture projection as the live damage page.
                realization_rows = replay_skill_realization(
                    self, {"sourceHash": source_hash}, lambda *_: None,
                    snapshot=snapshot, path=capture_path, encounters=selected_encounters)["rows"]
            except Exception:
                realization_message = "整场实现率未生成，请重新载入抓包评分后补传；缺失值不按零处理。"
            damage_archives = build_damage_overview_archives(
                snapshot, realization_rows=realization_rows, encounters=selected_encounters,
                hp_curves=display_hp_curves, realization_message=realization_message)

            archive_attached = False

            def score_detail_for(row: PlayerScore) -> dict[str, Any]:
                nonlocal archive_attached
                source_id = str(row.source_id).upper()
                detail = dataclasses.asdict(row)
                attach_team_output_time(detail, team_output_windows)
                detail["damage_overview"] = damage_archives.get(source_id, {})
                detail["_compact_display_context"] = score_timeline_display_context(
                    detail, event_platform_for(snapshot), resolved_casts, display_hp_curves,
                    _score_output_curve_specs(display_engine, row),
                    event_platform_for(snapshot).boss_hp_tracks,
                )
                if boss_debuffs and not archive_attached:
                    # One shared ledger per capture; not eight copies in the
                    # same upload or database. History RPC resolves siblings.
                    detail["boss_debuffs"] = boss_debuffs
                    archive_attached = True
                detail["instance_completion"] = dict(
                    event_platform_for(snapshot).instance_completion
                )
                detail["_compact_duration_seconds"] = float(
                    max(event_platform_for(snapshot).elapsed_seconds,
                        event_platform_for(snapshot).boss_effect_current_seconds)
                )
                detail["_compact_actual_casts"] = [
                    cast
                    for cast in resolved_casts
                    if str(cast.get("source_id") or "").upper() == source_id
                ]
                detail["_compact_player_buffs"] = [
                    buff
                    for buff in player_buff_rows
                    if str(buff.get("target_id") or "").upper() == source_id
                ]
                return detail

            self._score_sync.enqueue(
                capture_path,
                (encounter.name for encounter in selected_encounters),
                (
                    target_id
                    for encounter in selected_encounters
                    for target_id in encounter.target_ids
                ),
                (
                    TeamScoreExportRow(
                        job=score_export_job(row),
                        total_score=row.total_score,
                        output_score=row.output_score,
                        support_score=row.support_score,
                        specialty_score=row.specialty_score,
                        survival_penalty=row.survival_penalty,
                        theory_damage_completion=row.theory_damage_completion,
                        actual_dps=actual_dps_by_source.get(
                            str(row.source_id).upper()
                        ),
                        score_detail=score_detail_for(row),
                        character_identity=character_identities.get(
                            str(row.source_id).upper()
                        ),
                    )
                    for row in rows
                    if row.job and row.job != "未知职业"
                ),
                map_ids=map_ids,
                map_names=map_names,
                force_upload=force_upload,
                expected_capture_hash=source_hash,
            )

        # Dataclass expansion contains the full skill/timeline detail for all
        # eight players. It is local persistence work and must not hold the UI
        # on the final 96% step.
        threading.Thread(
            target=prepare_and_enqueue,
            name="v410-score-export-prepare",
            daemon=True,
        ).start()

    @staticmethod
    def _canonical_score_job_payload(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        result["sourceHash"] = norm(result.get("sourceHash"))
        raw_targets = result.get("bossTargetIds")
        result["bossTargetIds"] = sorted(
            {
                norm(value)
                for value in (
                    raw_targets if isinstance(raw_targets, (list, tuple, set)) else ()
                )
                if norm(value)
            }
        )
        if not isinstance(result.get("glyphOverrides"), dict):
            result["glyphOverrides"] = {}
        result["equipmentOverrides"] = equipment_overrides(result.get("equipmentOverrides"))
        result["forceRecompute"] = bool(result.get("forceRecompute"))
        result["scoreRuleVersion"] = str(result.get("scoreRuleVersion") or "v2").lower()
        result["bossPreset"] = norm(result.get("bossPreset")) or "红龙佩达"
        return result

    def score_equipment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Lazy, read-only projection; never run on the capture/polling path."""
        source_hash = self.resolve_score_source_hash(norm(payload.get("sourceHash")))
        source_id = norm(payload.get("sourceId"))
        with self._state_lock:
            snapshot = self._replays.get(source_hash)
            scores = self._score_results.get(self._score_keys_by_replay.get(source_hash, ""), ())
        if snapshot is None:
            raise KeyError("replay_not_loaded")
        player = next((p for p in snapshot.players if norm(p.source_id) == source_id and norm(p.job)), None)
        if player is None:
            raise ValueError("当前抓包没有该评分队员")
        saved = next((row.scoring_equipment for row in scores if norm(row.source_id) == source_id), None)
        override = equipment_overrides({source_id: payload["build"]}).get(source_id) if isinstance(payload.get("build"), dict) else None
        if saved and payload.get("useScored") and not payload.get("reset") and override is None:
            build = copy.deepcopy(saved["build"])
            source = saved["source"]
        else:
            runtime = self.legacy_timeline.extreme_runtime(copy.deepcopy(self.get_build("build1")))
            base = runtime._extreme_team_score_build_for_job(norm(player.job))
            from ..live.theory_plan_builder import _team_score_apply_boss_preset
            _team_score_apply_boss_preset(base, norm(payload.get("bossPreset")) or "红龙佩达")
            build = apply_score_equipment(base, override)
            source = "manual_override" if override else "standard_default"
        _rows, summary = self.calculator.calculate(build)
        return {"sourceId": source_id, "sourceHash": source_hash, "source": source, "build": build, "summary": summary,
                "options": uniform_equipment_options(self.store), "preview": self.score_build_preview(build)}

    def score_equipment_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_hash = self.resolve_score_source_hash(norm(payload.get("sourceHash")))
        with self._state_lock:
            snapshot = self._replays.get(source_hash)
        if snapshot is None:
            raise KeyError("replay_not_loaded")
        settings = validate_uniform_settings(self.store, payload.get("settings"))
        overrides = equipment_overrides(payload.get("equipmentOverrides"))
        players = [player for player in snapshot.players if norm(player.job) and norm(player.job) != "未知职业"]
        if set(overrides) - {norm(player.source_id) for player in players}:
            raise ValueError("评分装备包含不属于当前抓包的队员")
        rows = {}
        for player in players:
            source_id = norm(player.source_id)
            current = self.score_equipment({"sourceHash": source_hash, "sourceId": source_id,
                "build": overrides.get(source_id), "useScored": bool(payload.get("useScored"))})
            modified = apply_uniform_equipment(self, current["build"], settings)
            # Re-merge equipment only so environment/skills/buffs remain those
            # of scoring, and recalculate card conversion consistently.
            rows[source_id] = self.score_equipment({"sourceHash": source_hash, "sourceId": source_id, "build": modified})
        # Pure projection: an error in any player applies nothing to the UI or
        # global builds. All drafts are committed together only on success.
        return {"sourceHash": source_hash, "players": rows, "settings": settings}

    def score_job_dedupe_key(self, payload: dict[str, Any]) -> str:
        """Use one score job identity for UI, replay prefetch and live Stop.

        The immutable capture hash, selected Boss scope, glyph overrides and
        current build are all score inputs.  Sharing this key means clicking
        the rating tab while the automatic post-capture score is running joins
        that job instead of starting a second expensive calculation.
        """

        canonical_payload = self._canonical_score_job_payload(payload)
        frozen = json.dumps(
            {
                "request": canonical_payload,
                "build": self.get_build("build1"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"replay-score:{hashlib.sha256(frozen.encode('utf-8')).hexdigest()}"

    def start_score_job(
        self,
        payload: dict[str, Any],
        *,
        label: str = "正在生成全队评分",
    ) -> str:
        """Start or join the canonical background score job."""

        request_payload = self._canonical_score_job_payload(payload)
        requested_source_hash = norm(request_payload.get("sourceHash"))
        resolved_source_hash = self.resolve_score_source_hash(
            requested_source_hash
        )
        if resolved_source_hash != requested_source_hash:
            request_payload["sourceHash"] = resolved_source_hash

        # Queued scores retain only the saved path, not another large snapshot.
        # A later load may evict their parse cache; restore that exact file when
        # their turn starts. Running scores are separately pinned until done.
        with self._state_lock:
            queued_source_path = self._replay_sources.get(resolved_source_hash)

        def run_score(progress):
            source_hash = norm(request_payload.get("sourceHash"))
            with self._state_lock:
                still_loaded = source_hash in self._replays
            if not source_hash.startswith("live:") and not still_loaded and queued_source_path:
                restored = self.load_replay(str(queued_source_path), progress)
                if norm(restored.get("sourceHash")) != source_hash:
                    raise ValueError("排队评分的原始抓包已改变，请重新载入")
            if source_hash.startswith("live:"):
                # Keep an in-progress capture responsive. Finalized captures
                # use the normal offline worker budget because packet ingest
                # has already stopped.
                from ..timeline_parallel import timeline_worker_limit

                with timeline_worker_limit(2):
                    return self.score_replay(request_payload, progress)
            return self.score_replay(request_payload, progress)

        return self.jobs.start(
            label,
            run_score,
            kind="replay-score",
            dedupe_key=self.score_job_dedupe_key(request_payload),
        )

    def _schedule_stopped_capture_score(
        self,
        snapshot: CombatSnapshot,
        source_file: str,
        source_hash: str,
        capture_state: dict[str, Any],
    ) -> str:
        """Queue an exact full-run score as soon as live capture is frozen."""

        immutable_hash = norm(source_hash)
        capture_path = Path(source_file).expanduser() if source_file else None
        if (
            not immutable_hash
            or immutable_hash.startswith("live:")
            or capture_path is None
            or not capture_path.is_file()
            or not event_platform_for(snapshot).players
            or float(event_platform_for(snapshot).total_damage) <= 0.0
        ):
            return ""

        state = {
            **capture_state,
            "state": "stopped",
            "scoreReady": True,
            "scoreSourceHash": immutable_hash,
            "sourceFile": str(capture_path),
        }
        # Bind the callback's exact frozen snapshot. Looking it up again via
        # live_capture.snapshot_object is racy when the next automatic capture
        # starts before this background score registration completes.
        self._register_capture_score_source(
            state,
            immutable_hash,
            snapshot=snapshot,
            source_file=capture_path,
        )
        return self.start_score_job(
            {
                "sourceHash": immutable_hash,
                "bossTargetIds": [],
                "glyphOverrides": {},
            },
            label="实时统计已停止，正在后台评分并上传",
        )

    def _score_benchmark_nest(
        self,
        source_hash: str,
        snapshot: CombatSnapshot,
    ) -> str:
        with self._state_lock:
            capture_path = self._replay_sources.get(source_hash)
        map_ids, map_names = self._replay_map_evidence(source_hash)
        encounters = self._score_encounters(source_hash, snapshot)
        return resolve_score_nest(
            (encounter.name for encounter in encounters),
            capture_path=capture_path,
            target_ids=(
                target_id
                for encounter in encounters
                for target_id in encounter.target_ids
            ),
            map_ids=map_ids,
            map_names=map_names,
        )

    def _replay_map_evidence(
        self,
        source_hash: str,
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        """Return the strongest map/dungeon evidence from replay decoding.

        Keep the evidence immutable before it is passed to background score
        export, because loading another replay may evict the source cache while
        the export thread is still preparing the full score detail. Exact map
        IDs win; otherwise one uniquely resolved formal-map entity profile is
        retained with map ID zero.
        """

        replay_key = norm(source_hash)
        with self._state_lock:
            history = tuple(
                dict(row)
                for row in (
                    self._replay_map_history.get(source_hash)
                    or self._replay_map_history.get(replay_key)
                    or ()
                )
                if isinstance(row, dict)
            )
        # The live/replay badge represents the latest strongest identity. A
        # capture can legitimately contain a brief previous-map entry before
        # the real destination (for example 寂夜沼泽 -> 银色新月修炼场), so
        # forwarding every historical map would make upload disagree with the
        # badge. Use the same latest valid identity for both consumers.
        for row in reversed(history):
            try:
                map_type = int(row.get("map_type") or 0)
            except (TypeError, ValueError):
                map_type = 0
            if (
                (map_type and map_type != 3)
                or str(row.get("content_kind") or "").strip().casefold()
                in {"hub_return", "hub", "town", "lobby"}
            ):
                # Scene-return evidence is useful for auto-stop, but describes
                # the destination lobby, not the dungeon being scored.
                continue
            try:
                map_id = int(row.get("map_id") or 0)
            except (TypeError, ValueError):
                map_id = 0
            map_name = str(
                row.get("nest_name") or row.get("map_name") or ""
            ).strip()
            entry = resolve_dungeon_map_id(map_id)
            if entry is not None and not is_battle_map(entry):
                continue
            if map_id <= 0 and map_name and not is_battle_map_name(map_name):
                continue
            if map_id > 0 or map_name:
                return (
                    (map_id,) if map_id > 0 else (),
                    (map_name,) if map_name else (),
                )
        return (), ()
    def _score_encounters(
        self,
        source_hash: str,
        snapshot: CombatSnapshot,
    ) -> tuple[BossEncounter, ...]:
        """Reuse the Boss recognition already performed while loading replay."""

        replay_key = norm(source_hash)
        with self._state_lock:
            cached = self._score_encounters_by_replay.get(replay_key)
        if cached is not None:
            return cached
        resolved = tuple(BossRecognizer().recognize_snapshot(snapshot))
        with self._state_lock:
            if self._replays.get(replay_key) is snapshot:
                self._score_encounters_by_replay[replay_key] = resolved
        return resolved
    @staticmethod
    def _score_specialty_windows_from_encounters(
        encounters: tuple[BossEncounter, ...],
        boss_target_ids: frozenset[str] | None,
    ) -> tuple[tuple[float, float], ...]:
        rows = sorted(
            (max(0.0, float(start)), max(float(start), float(end)))
            for encounter in encounters
            if not boss_target_ids
            or bool(set(encounter.target_ids) & set(boss_target_ids))
            for start, end in encounter.phase_windows
            if float(end) > float(start)
        )
        merged: list[list[float]] = []
        for start, end in rows:
            if not merged or start > merged[-1][1] + 1e-6:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return tuple((start, end) for start, end in merged)
    @stable_score_worker_budget
    @_retain_active_score_source
    def score_replay(
        self,
        payload: dict[str, Any],
        progress: Callable[[int, str], None],
    ) -> dict[str, Any]:
        from ..live.score import rule_version, attach_context
        selected_rule = rule_version(payload)
        local_comparison = bool(payload.get('localComparison'))
        v2_context = None
        requested_source_hash = norm(payload.get("sourceHash"))
        source_hash = self.resolve_score_source_hash(requested_source_hash)
        if source_hash != requested_source_hash:
            # Treat a score submitted on the Stop boundary exactly like a
            # normal immutable replay score: full worker budget, disk-cache
            # lookup and upload eligibility all remain available.
            payload = {**payload, "sourceHash": source_hash}
        is_live_score = source_hash.startswith("live:")
        force_recompute = bool(payload.get("forceRecompute")) and not is_live_score
        live_revision = 0
        if is_live_score:
            snapshot, live_revision, live_source_file = (
                self.live_capture.score_snapshot(source_hash)
            )
            if live_source_file and not norm(payload.get("sourceFile")):
                payload["sourceFile"] = live_source_file
            # The same live key points at progressively newer immutable
            # snapshots.  Replace the in-memory entry and invalidate Boss
            # recognition before every score refresh.
            with self._state_lock:
                self._replays[source_hash] = snapshot
                self._score_encounters_by_replay.pop(source_hash, None)
        else:
            if force_recompute and not payload.get("_fullRunChild"):
                with self._state_lock:
                    capture_path = self._replay_sources.get(source_hash)
                if capture_path is None or not Path(capture_path).is_file():
                    raise KeyError("replay_source_file_not_found")
                progress(1, "正在废弃旧评分并核验当前实战记录")

                def reload_progress(_value: int, message: str) -> None:
                    progress(2, f"正在核验当前记录：{message}")

                reloaded = self.load_replay(str(capture_path), reload_progress)
                reloaded_hash = norm(reloaded.get("sourceHash"))
                if reloaded_hash.casefold() != source_hash.casefold():
                    raise ValueError("replay_source_hash_mismatch")
                if not norm(payload.get("sourceFile")):
                    payload = {**payload, "sourceFile": str(capture_path)}
                with self._state_lock:
                    snapshot = self._replays.get(source_hash)
                    previous_score_key = self._score_keys_by_replay.pop(
                        source_hash, None
                    )
                    if previous_score_key:
                        self._score_results.pop(previous_score_key, None)
                        self._score_benchmark_nests.pop(previous_score_key, None)
                invalidate_theory_cache = getattr(
                    self.score_theory_planner,
                    "invalidate_score_cache",
                    None,
                )
                if callable(invalidate_theory_cache):
                    invalidate_theory_cache()
                progress(3, "当前记录核验完成，正在重新生成理论轴")
            else:
                with self._state_lock:
                    snapshot = self._replays.get(source_hash)
        if snapshot is None:
            raise KeyError("replay_not_loaded")
        raw_targets = payload.get("bossTargetIds", [])
        requested_boss_target_ids = frozenset(
            norm(value) for value in raw_targets if norm(value)
        ) or None
        upload_full_run = requested_boss_target_ids is None
        score_encounters = self._score_encounters(source_hash, snapshot)
        benchmark_nest = self._score_benchmark_nest(source_hash, snapshot)
        boss_target_ids = requested_boss_target_ids
        if boss_target_ids is None:
            boss_target_ids = frozenset(
                target_id
                for encounter in score_encounters
                for target_id in encounter.target_ids
            ) or None
        specialty_windows = self._score_specialty_windows_from_encounters(
            score_encounters,
            boss_target_ids,
        )
        if selected_rule == 'v2':
            matching_groups = [e for e in score_encounters if set(e.target_ids) & set(boss_target_ids or ())]
            from ..live.score_hit_evidence import prepare_context, prepare_continuous_context
            with self._state_lock:
                capture_path = self._replay_sources.get(source_hash)
            if not capture_path or is_live_score:
                raise ValueError('v2需要已保存的抓包文件，请停止抓包后再计算')
            progress(3, '正在读取逐Hit倍率证据并校准Boss上限')
            if requested_boss_target_ids is None:
                v2_context = prepare_continuous_context(Path(capture_path), matching_groups, self.store, progress, snapshot=snapshot)
            elif len(matching_groups)==1:
                v2_context = prepare_context(Path(capture_path), boss_target_ids, self.store, progress, windows=matching_groups[0].phase_windows, snapshot=snapshot)
            else:
                raise ValueError('内部Boss目标分组不唯一，无法校准')
        # Hit evidence is local and complete; no planner mutation has begun.
        _yield_newer_scores(progress)
        rule_signature = (selected_rule, v2_context.get('fingerprint') if v2_context else '')
        previous_rule = getattr(self.score_theory_planner, '_score_rule_signature', None)
        if previous_rule is not None and previous_rule != rule_signature:
            self.score_theory_planner.invalidate_score_cache()
        self.score_theory_planner._score_rule_signature = rule_signature
        self.score_theory_planner._score_v2_context = v2_context
        score_engine = TeamScoreEngine(self.store)
        try:
            score_cast_events = self.score_theory_planner.score_cast_events(snapshot)
        except Exception:
            score_cast_events = event_platform_for(snapshot).skill_cast_events
        score_snapshot = (
            replace(snapshot, skill_cast_events=score_cast_events)
            if score_cast_events
            else snapshot
        )
        progress(3, "正在识别实战技能纹章（分段冷却与储存次数）")
        inferred_glyphs_by_source, glyph_inference_diagnostics = (
            infer_combat_skill_glyphs(score_snapshot, self.store)
        )
        progress(4, "正在确定主要 Boss 可输出区间")
        boss_preset_name = norm(payload.get("bossPreset")) or "红龙佩达"
        glyph_overrides = (
            payload.get("glyphOverrides")
            if isinstance(payload.get("glyphOverrides"), dict)
            else {}
        )
        build_overrides = equipment_overrides(payload.get("equipmentOverrides"))
        valid_sources = {norm(player.source_id) for player in score_snapshot.players if norm(player.job)}
        if set(build_overrides) - valid_sources:
            raise ValueError("评分装备包含不属于当前抓包的队员")
        score_disk_key = ""
        cached_score = None
        if not is_live_score:
            try:
                score_disk_key = team_score_cache_key(
                    None,
                    (
                        rule_signature,
                        tuple(sorted(boss_target_ids or ())),
                        boss_preset_name,
                        glyph_overrides,
                        build_overrides,
                        combat_glyph_signature(inferred_glyphs_by_source),
                        self.get_build("build1"),
                    ),
                    capture_hash=source_hash,
                    data_root=DATA_DIR,
                )
                if force_recompute:
                    self._team_score_disk_cache.discard(score_disk_key)
                else:
                    cached_score = self._team_score_disk_cache.load(score_disk_key)
            except Exception:
                score_disk_key = ""
                cached_score = None
        if cached_score is not None:
            _yield_newer_scores(progress)
            rows, cached_glyphs = cached_score
            score_key = uuid.uuid4().hex
            with self._state_lock:
                self._score_results[score_key] = rows
                self._score_keys_by_replay[source_hash] = score_key
                self._score_benchmark_nests[score_key] = benchmark_nest
                while len(self._score_results) > 8:
                    removed_key = next(iter(self._score_results))
                    self._score_results.pop(removed_key, None)
                    self._score_benchmark_nests.pop(removed_key, None)
            progress(96, "版本指纹一致，已恢复完整评分缓存")
            if not local_comparison:
                self._queue_replay_score_sync(
                source_hash,
                score_snapshot,
                rows,
                None if upload_full_run else boss_target_ids,
            )
            return {
                "scoreKey": score_key,
                "scoreRuleVersion": selected_rule,
                "bossPreset": boss_preset_name,
                "sourceHash": source_hash,
                "bossTargetIds": sorted(boss_target_ids or ()),
                "rows": [self._score_summary(row) for row in rows],
                "glyphsBySource": copy.deepcopy(cached_glyphs or {}),
                "equipmentBySource": {row.source_id: copy.deepcopy(row.scoring_equipment) for row in rows},
                "diagnostics": {"versionCacheHit": True, "scoreRuleVersion": selected_rule, "hitEvidence": v2_context.get("diagnostics", {}) if v2_context else {}},
                "glyphInference": copy.deepcopy(glyph_inference_diagnostics),
                "theoryPlanner": "version-fingerprint-cache",
                "theoryPlannerError": "",
                "theoryDiagnostics": {},
                "scoreSync": self._score_sync.status(),
            }
        # The V4.01 planner has a short live-session reuse window.  Enabling
        # it here avoids rebuilding every profession's theoretical axis for
        # back-to-back live refreshes while the exact actual HP/effect events
        # are still rescored on every pass.
        self.score_theory_planner._capture_active = is_live_score
        try:
            plans = self.score_theory_planner.build(
                score_snapshot,
                self.get_build("build1"),
                boss_target_ids=boss_target_ids,
                boss_preset_name=boss_preset_name,
                capture_hint=norm(payload.get("sourceFile")),
                glyph_overrides=glyph_overrides or None,
                inferred_glyphs_by_source=inferred_glyphs_by_source,
                progress=progress,
                equipment_overrides=build_overrides,
            )
            expected_sources = {
                norm(player.source_id)
                for player in score_snapshot.players
                if norm(player.source_id) and norm(player.job)
            }
            missing_sources = sorted(
                source_id for source_id in expected_sources
                if not isinstance(plans.get(source_id), dict)
                or not plans[source_id].get("segments")
            )
            if self.score_theory_planner._team_score_theory_error or missing_sources:
                raise RuntimeError(
                    self.score_theory_planner._team_score_theory_error
                    or f"理论轴缺少角色：{', '.join(missing_sources)}"
                )
            planner_name = "v401-full-team-score"
            planner_error = ""
        except Exception as exc:
            from ..live.timing_ablation import TimingAblationError
            if isinstance(exc, TimingAblationError):
                raise
            # A simplified fallback has different skill legality, damage units
            # and glyph assumptions. It previously produced plausible-looking
            # scores for all eight players and even uploaded them when the
            # exact planner failed before its first worker (0926 BlueMoon).
            # Fail visibly instead of publishing an incomparable ranking.
            raise RuntimeError(
                f"完整评分理论轴生成失败，未生成或上传简化评分：{exc}"
            ) from exc

        def preliminary_score_progress(done: int, total: int, job: str) -> None:
            progress(
                TEAM_SCORE_PROGRESS_TIMING_END
                + round(
                    done
                    * (
                        TEAM_SCORE_PROGRESS_EXCLUSION_END
                        - TEAM_SCORE_PROGRESS_TIMING_END
                    )
                    / max(1, total)
                ),
                f"正在判定 {job} 的评分豁免（{done}/{total}）",
            )

        if v2_context:
            attach_context(plans, v2_context)
        exclusions = score_engine.score_exclusions(
            score_snapshot,
            theory_plans=plans,
            boss_target_ids=boss_target_ids,
            actual_timeline_events=score_cast_events,
            progress_callback=preliminary_score_progress,
            allow_parallel=True,
        )
        excluded_skills_by_source = {
            source_id: set(skill_keys)
            for source_id, (skill_keys, _high_value_keys) in exclusions.items()
            if skill_keys
        }
        excluded_high_value_by_source = {
            source_id: set(high_value_keys)
            for source_id, (_skill_keys, high_value_keys) in exclusions.items()
            if high_value_keys
        }
        if excluded_skills_by_source:
            replanned = self.score_theory_planner.rebuild_excluded_axes(
                plans,
                excluded_skills_by_source,
                progress,
            )
            replanned_sources = {
                source_id
                for source_id, plan in replanned.items()
                if isinstance(plan, dict)
                and plan.get("_score_excluded_skill_keys")
            }
            if replanned_sources:
                plans = replanned
                for source_id in replanned_sources:
                    plan = plans.get(source_id)
                    if not isinstance(plan, dict):
                        continue
                    plan["_score_excluded_high_value_keys"] = sorted(
                        excluded_high_value_by_source.get(source_id, set())
                    )

        # A nested newer score may replace all shared planner diagnostics and
        # replan specs. Replanning is now finished, so retain only this score's
        # diagnostics and never read the shared planner again below.
        theory_diagnostics = copy.deepcopy(
            self.score_theory_planner._team_score_theory_diagnostics
            if planner_name == "v401-full-team-score" else {}
        )
        def retain_local_plans() -> None:
            nonlocal plans
            # A newer request for the same capture can reuse and annotate the
            # planner's cached dicts. Do not let it mutate the suspended score.
            plans = copy.deepcopy(plans)
        _yield_newer_scores(progress, retain_local_plans)
        progress(
            TEAM_SCORE_PROGRESS_REPLAN_END,
            "豁免技能理论轴重排完成，正在计算最终评分...",
        )

        def final_score_progress(done: int, total: int, job: str) -> None:
            player_phase_end = 78
            if job == "辅助收益索引":
                value = TEAM_SCORE_PROGRESS_REPLAN_END
                label = "正在建立一次性辅助收益索引"
            elif job.startswith("辅助基准："):
                value = player_phase_end + round(
                    done
                    * (TEAM_SCORE_PROGRESS_FINAL_END - player_phase_end)
                    / max(1, total)
                )
                label = f"正在计算全职业辅助基准（{done}/{total}）"
            else:
                value = TEAM_SCORE_PROGRESS_REPLAN_END + round(
                    done
                    * (player_phase_end - TEAM_SCORE_PROGRESS_REPLAN_END)
                    / max(1, total)
                )
                label = f"正在计算 {job} 的输出与辅助评分（{done}/{total}）"
            progress(
                value,
                label,
            )

        if v2_context:
            attach_context(plans, v2_context)
        rows = score_engine.score(
            score_snapshot,
            theory_plans=plans,
            boss_target_ids=boss_target_ids,
            specialty_windows=specialty_windows,
            death_windows=(
                (
                    (
                        0.0,
                        _snapshot_recording_end_seconds(snapshot),
                    ),
                )
                if upload_full_run
                else specialty_windows
            ),
            actual_timeline_events=score_cast_events,
            progress_callback=final_score_progress,
            allow_parallel=True,
        )
        cached_glyphs = {
            source_id: copy.deepcopy(plan.get("_score_glyphs", []))
            for source_id, plan in plans.items()
            if isinstance(plan, dict)
        }
        rows = tuple(replace(row, scoring_equipment=copy.deepcopy(
            plans.get(row.source_id, {}).get("_score_equipment", {})
        )) for row in rows)
        _yield_newer_scores(progress)
        if score_disk_key:
            try:
                self._team_score_disk_cache.save(
                    score_disk_key, rows, cached_glyphs
                )
            except Exception:
                pass
        score_key = uuid.uuid4().hex
        with self._state_lock:
            self._score_results[score_key] = rows
            self._score_keys_by_replay[source_hash] = score_key
            self._score_benchmark_nests[score_key] = benchmark_nest
            while len(self._score_results) > 8:
                removed_key = next(iter(self._score_results))
                self._score_results.pop(removed_key, None)
                self._score_benchmark_nests.pop(removed_key, None)
        progress(
            99,
            "正在覆盖旧评分并提交云端同步"
            if force_recompute and not local_comparison
            else "正在整理评分明细",
        )
        sync_boss_target_ids = None if upload_full_run else boss_target_ids
        if not is_live_score and not local_comparison:
            if force_recompute and upload_full_run:
                self._queue_replay_score_sync(
                    source_hash,
                    score_snapshot,
                    rows,
                    sync_boss_target_ids,
                    force_upload=True,
                )
            else:
                self._queue_replay_score_sync(
                    source_hash,
                    score_snapshot,
                    rows,
                    sync_boss_target_ids,
                )
        return {
            "scoreKey": score_key,
                "scoreRuleVersion": selected_rule,
                "bossPreset": boss_preset_name,
            "sourceHash": source_hash,
            "live": is_live_score,
            "liveRevision": live_revision,
            "liveAnchor": (
                self._live_score_anchor(score_snapshot, rows)
                if is_live_score
                else None
            ),
            "bossTargetIds": sorted(boss_target_ids or ()),
            "rows": [self._score_summary(row) for row in rows],
            "glyphsBySource": copy.deepcopy(cached_glyphs or {}),
            "equipmentBySource": {row.source_id: copy.deepcopy(row.scoring_equipment) for row in rows},
            "glyphInference": copy.deepcopy(glyph_inference_diagnostics),
            "diagnostics": {
                **copy.deepcopy(score_engine._score_runtime_diagnostics),
                "exclusionPrecheck": copy.deepcopy(
                    score_engine._score_exclusion_diagnostics
                ),
                "versionCacheHit": False,
                "forceRecompute": force_recompute,
            },
            "theoryPlanner": planner_name,
            "theoryPlannerError": planner_error,
            "theoryDiagnostics": theory_diagnostics,
            "scoreSync": self._score_sync.status(),
        }
    @staticmethod
    def _live_score_anchor(
        snapshot: CombatSnapshot,
        rows: Iterable[PlayerScore] = (),
    ) -> dict[str, Any]:
        """Expose the exact totals used by a live score calibration.

        The UI can cheaply update a clearly labelled rough score between full
        scoring passes without rebuilding theory axes for every capture poll.
        These values are deliberately returned only for an in-memory live
        session and are never uploaded as a final score.
        """

        score_rows = tuple(rows)
        score_by_source = {
            str(getattr(row, "source_id", "")): row
            for row in score_rows
            if str(getattr(row, "source_id", ""))
        }
        player_payloads: list[dict[str, Any]] = []
        platform = event_platform_for(snapshot)
        for player in platform.players:
            player_payload: dict[str, Any] = {
                "sourceId": player.source_id,
                "damage": max(0, int(player.damage)),
                "healing": max(0, int(player.healing)),
                "incomingDamage": max(0, int(player.incoming_damage)),
            }
            score_row = score_by_source.get(player.source_id)
            if score_row is not None:
                scored_skill_names = sorted({
                    canonical_skill(detail.skill_name)
                    for detail in getattr(score_row, "skill_details", ())
                    if not bool(getattr(detail, "excluded_from_score", False))
                    and canonical_skill(getattr(detail, "skill_name", ""))
                })
                if scored_skill_names:
                    scored_name_set = set(scored_skill_names)
                    scored_skills = tuple(
                        skill
                        for skill in player.skills
                        if canonical_skill(skill.name) in scored_name_set
                    )
                    player_payload["scoredDamage"] = sum(
                        max(0, int(skill.damage)) for skill in scored_skills
                    )
                    player_payload["scoredSkillNames"] = scored_skill_names
                    player_payload["scoredSkillIds"] = sorted({
                        str(skill.skill_id)
                        for skill in scored_skills
                        if str(skill.skill_id)
                    })
            player_payloads.append(player_payload)

        payload = {
            "elapsedSeconds": max(0.0, float(platform.elapsed_seconds)),
            "dpsElapsedSeconds": max(
                0.0,
                float(platform.dps_elapsed_seconds or platform.elapsed_seconds),
            ),
            "players": player_payloads,
        }
        # The exact score engine has already resolved Boss output windows. Keep
        # that authoritative effective-combat clock beside the calibration so
        # opening waits, loading screens and long mechanics cannot dilute the
        # one-second projection in the UI.
        if score_rows:
            ordered = sorted(
                (
                    (max(0.0, float(start)), max(0.0, float(end)))
                    for row in score_rows
                    for start, end in row.output_windows
                    if float(end) > float(start)
                ),
                key=lambda interval: (interval[0], interval[1]),
            )
            merged: list[list[float]] = []
            for start, end in ordered:
                if not merged or start > merged[-1][1] + 1e-6:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            payload["outputWindows"] = [list(interval) for interval in merged]
            payload["activeCombatSeconds"] = sum(
                end - start for start, end in merged
            ) or max(
                0.0,
                float(platform.dps_elapsed_seconds or platform.elapsed_seconds),
            )
        return payload
    @staticmethod
    def _score_summary(row: PlayerScore) -> dict[str, Any]:
        return {
            "sourceId": row.source_id,
            "name": row.name,
            "job": row.rotation_job or row.job,
            "role": row.role,
            "roleReason": row.role_reason,
            "grade": row.grade,
            "totalScore": row.total_score,
            "outputScore": row.output_score,
            "supportScore": row.support_score,
            "specialtyScore": row.specialty_score,
            "specialtyContribution": row.specialty_contribution,
            "survivalPenalty": row.survival_penalty,
            "outputWeight": row.output_weight,
            "supportWeight": row.support_weight,
            "specialtyWeight": row.specialty_weight,
            "specialtyLabel": row.specialty_label,
            "theoryDamageCompletion": row.theory_damage_completion,
            "burstUsage": row.burst_usage,
            "hasBurstWindows": bool(row.burst_windows),
            "coverageCompletion": row.coverage_completion,
            "contributionCompletion": row.contribution_completion,
            "actualStandardPower": row.actual_standard_power,
            "theoryStandardPower": row.theory_standard_power,
            "notes": list(row.notes),
        }
    def score_detail(
        self,
        score_key: str,
        source_id: str,
        source_hash: str = "",
    ) -> dict[str, Any]:
        requested_key = norm(score_key)
        requested_source = norm(source_id)
        requested_replay = norm(source_hash)
        with self._state_lock:
            resolved_key = requested_key
            rows = self._score_results.get(resolved_key, ())
            # A score result may be evicted from the in-memory LRU while the
            # browser still holds its response. Reuse the latest result for
            # the same replay when the caller supplies sourceHash. This keeps
            # a transient cache miss from breaking profession switching.
            if not rows and requested_replay:
                resolved_key = self._score_keys_by_replay.get(requested_replay, "")
                rows = self._score_results.get(resolved_key, ())
            benchmark_nest = self._score_benchmark_nests.get(resolved_key, "")
        row = next(
            (item for item in rows if norm(item.source_id) == requested_source),
            None,
        )
        if row is None and requested_replay:
            # The requested key can still exist while its source roster is
            # stale (for example, a score job finished while the replay was
            # reloaded). Try the latest result for this replay once more.
            with self._state_lock:
                fallback_key = self._score_keys_by_replay.get(requested_replay, "")
                fallback_rows = self._score_results.get(fallback_key, ())
                fallback_row = next(
                    (
                        item
                        for item in fallback_rows
                        if norm(item.source_id) == requested_source
                    ),
                    None,
                )
                if fallback_row is not None:
                    resolved_key = fallback_key
                    benchmark_nest = self._score_benchmark_nests.get(
                        resolved_key, ""
                    )
                    row = fallback_row
        if row is None:
            raise KeyError("score_detail_not_found")
        payload = _json_safe(row)
        # Display the shared Boss availability separately from this player's
        # scoring windows. Never reconstruct team availability from one player.
        with self._state_lock:
            display_source = requested_replay or next(
                (source for source, key in self._score_keys_by_replay.items()
                 if key == resolved_key), "")
            display_snapshot = self._replays.get(display_source)
        if display_snapshot is not None:
            encounters = self._score_encounters(display_source, display_snapshot)
            payload["team_output_windows"] = _json_safe(
                TeamScoreEngine(self.store)._output_windows(
                    display_snapshot, frozenset(
                        target for encounter in encounters for target in encounter.target_ids)))
        payload["output_curve_specs"] = _score_output_curve_specs(
            TeamScoreEngine(self.store), row
        )
        payload["improvement_rows"] = _score_improvement_rows(row)
        payload["improvement_summary"] = _score_improvement_summary(row)
        if row.hit_realization.get("scope") == "full_run":
            payload["improvement_rows"] = [
                *copy.deepcopy(row.hit_realization.get("improvement_rows", [])),
                *(item for item in payload["improvement_rows"] if item.get("direction") == "辅助分"),
            ]
            payload["improvement_summary"]["recoverable_output"] = row.hit_realization.get("recoverable_output", 0.0)
        payload["cloud_benchmark"] = self._score_sync.benchmark(
            benchmark_nest,
            row.job,
        )
        return payload


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeReplayScoreMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
