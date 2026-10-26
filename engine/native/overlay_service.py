from __future__ import annotations

import time
from functools import lru_cache

from ..effect_stacking import EFFECT_STACKING_ENGINE, EffectInput
from ..job_mechanics import JOB_MECHANICS
from ..live.aggregator import _is_recognized_player_job
from ..live.buff_clear import player_buff_clear_times_by_target
from ..live.event_platform import event_platform_for
from ..live.summon_presence import summon_overlay_options
from ..live.aura_state import is_aura_row, is_aura_damage_series, is_inferred_aura_row
from .overlay_lifecycle import acceleration_overlay_row, party_boss_health

class NativeOverlayMixin:
    def _register_capture_score_source(
        self,
        state: dict[str, Any],
        source_hash: str,
        *,
        snapshot: CombatSnapshot | None = None,
        source_file: str | Path | None = None,
    ) -> None:
        """Bind a frozen live snapshot to both its final hash and trace path.

        Replay loading registers ``_replays`` and ``_replay_sources`` together.
        Live capture used to register only the frozen snapshot after Stop.  The
        score page could therefore calculate a final score, but score sync had
        no capture path and silently skipped both the score row and .dpscap
        upload.  Keep active ``live:`` sessions memory-only, then register the
        immutable file as soon as Stop publishes its final hash.
        """

        replay_key = norm(source_hash)
        # Stop hands the exact frozen snapshot to the callback. Prefer that
        # object over the mutable ``live_capture.snapshot_object`` reference:
        # the next run may already have started while the previous score is
        # being registered.
        explicit_snapshot = snapshot is not None
        capture_snapshot = (
            snapshot
            if explicit_snapshot
            else getattr(self.live_capture, "snapshot_object", None)
        )
        if not replay_key or capture_snapshot is None:
            return

        final_key = norm(state.get("scoreSourceHash"))
        # A stopped capture remains available on ``live_capture`` while the
        # user is free to load and score older files.  Never bind that frozen
        # live snapshot to an unrelated offline replay hash.  Doing so made
        # the requested Boss ids come from (for example) 黑龙1 while players,
        # effects and damage came from the previously stopped 黑龙3 capture.
        # Only the active ``live:`` key or this capture's own immutable final
        # key may be registered from live-capture state.
        is_live_key = replay_key.startswith("live:")
        live_key_getter = getattr(self.live_capture, "live_source_hash", None)
        current_live_key = (
            norm(live_key_getter()) if callable(live_key_getter) else ""
        )
        # A ``live:`` key names one concrete recording generation. Never bind
        # an old browser request to the current generation's mutable snapshot.
        is_own_live_key = bool(current_live_key) and replay_key == current_live_key
        is_own_final_key = bool(final_key) and replay_key == final_key
        if (is_live_key and not is_own_live_key) or (
            not is_live_key and not is_own_final_key
        ):
            return

        source_file_text = norm(source_file) or norm(state.get("sourceFile"))
        capture_path: Path | None = None
        if (
            replay_key == final_key
            and not replay_key.startswith("live:")
            and str(state.get("state") or "") == "stopped"
            and bool(state.get("scoreReady"))
            and source_file_text
        ):
            candidate = Path(source_file_text).expanduser()
            if candidate.is_file():
                capture_path = candidate.resolve()

        with self._state_lock:
            # Immutable bindings are write-once for status/resolve callers.
            # Only the Stop callback, which owns the exact snapshot argument,
            # may establish or repair one.
            if (
                not is_live_key
                and not explicit_snapshot
                and replay_key in self._replays
            ):
                if capture_path is not None:
                    self._replay_sources.setdefault(replay_key, capture_path)
                return
            self._replays[replay_key] = capture_snapshot
            if capture_path is not None:
                self._replay_sources[replay_key] = capture_path
            final_map_history = tuple(
                dict(row)
                for row in (state.get("nestMapHistory") or ())
                if isinstance(row, dict)
            )
            if final_map_history:
                # The live decoder already owns the complete, ordered scene
                # evidence. Persist it as one small immutable tuple so upload
                # and the badge select the same latest battle map even when a
                # town/Garden row follows it.
                self._replay_map_history[replay_key] = final_map_history
            # Replay loading records exact map history itself.  A capture that
            # was just stopped previously registered only its frozen snapshot
            # and file path, so direct scoring lost the map ID and fell back to
            # Boss-name inference.  Persist the same exact identity already
            # shown by the live badge before the score/export thread starts.
            if (
                not final_map_history
                and str(state.get("nestConfidence") or "") == "exact_map_id"
            ):
                try:
                    map_id = int(state.get("nestMapId") or 0)
                except (TypeError, ValueError):
                    map_id = 0
                nest_name = str(state.get("nestName") or "").strip()
                if map_id > 0 or nest_name:
                    self._replay_map_history[replay_key] = (
                        {
                            "map_id": map_id,
                            "map_name": nest_name,
                            "nest_name": nest_name,
                            "official_name": nest_name,
                            "content_kind": "dungeon",
                            "observed_at": float(
                                state.get("nestObservedAt") or 0.0
                            ),
                            "layout": "live-final-exact-map-id",
                        },
                    )
            elif not final_map_history:
                # Non-leaders and captures started after the one-shot map
                # transition often have no exact room ID.  The live decoder
                # can still prove one dungeon from a unique formal-map entity
                # template.  Preserve that evidence for the just-stopped
                # replay/movement page instead of reverting to 未识别地图 until
                # the user manually reloads and rescans the file.
                confidence = str(state.get("nestConfidence") or "").strip()
                nest_name = str(state.get("nestName") or "").strip()
                if confidence in {
                    "entry_template",
                    "template",
                    "template_set",
                    "dominant_template_set",
                } and nest_name:
                    self._replay_map_history[replay_key] = (
                        {
                            "map_id": 0,
                            "map_name": nest_name,
                            "nest_name": str(
                                state.get("nestLabel") or nest_name
                            ).strip(),
                            "official_name": nest_name,
                            "content_kind": "dungeon",
                            "observed_at": float(
                                state.get("nestObservedAt") or 0.0
                            ),
                            "layout": (
                                str(state.get("nestMapLayout") or "").strip()
                                or "live-final-formal-map-entity-fallback"
                            ),
                            "confidence": confidence,
                            "template_id": int(
                                state.get("nestTemplateId") or 0
                            ),
                            "inferred": True,
                        },
                    )

    def capture_status(self, include_snapshot: bool = True) -> dict[str, Any]:
        # The main live page needs the progressively updated full snapshot.
        # Returning overlay_status() here leaves the page pinned to a replay
        # loaded before capture because that compact payload deliberately omits
        # the ``snapshot`` field.
        try:
            state = self.live_capture.status(include_snapshot=include_snapshot)
        except TypeError:
            # Lightweight test/embedded capture adapters may still implement
            # the older parameterless status contract.
            state = self.live_capture.status()
            if not include_snapshot:
                state.pop("snapshot", None)
        source_hash = norm(state.get("scoreSourceHash"))
        if not source_hash and isinstance(state.get("snapshot"), dict):
            source_hash = norm((state.get("snapshot") or {}).get("sourceHash"))
        self._register_capture_score_source(state, source_hash)
        auto_score_job_id = norm(state.get("autoScoreJobId"))
        if auto_score_job_id:
            job = self.jobs.status(auto_score_job_id)
            job_state = str(job.get("state") or "")
            state["autoScoreState"] = job_state
            state["autoScoreProgress"] = int(job.get("progress") or 0)
            if job_state == "completed":
                state["autoScoreStatus"] = "后台评分完成，已加入评分上传队列"
            elif job_state == "failed":
                state["autoScoreStatus"] = "后台评分失败，可在全队评分中手动重试"
                state["autoScoreError"] = str(job.get("error") or "")
            elif job_state == "cancelled":
                state["autoScoreStatus"] = "后台评分已取消，可在全队评分中手动重试"
            else:
                state["autoScoreStatus"] = str(
                    job.get("label") or state.get("autoScoreStatus") or "后台评分中"
                )
        teammate_payload = self._teammate_profiles_payload()
        state["teammateInfoRevision"] = teammate_payload["revision"]
        # The normal one-second status poll can wake the inspection overlay
        # when a click arrives or its background panel projection completes.
        # This keeps /teammate-info on a five-second fallback cadence instead
        # of polling it continuously for those transitions.
        inspection_revision = int(as_number(teammate_payload["revision"]))
        presentation_revision = max(0, inspection_revision) * 2
        projection_lock = getattr(self, "_teammate_projection_worker_lock", None)
        if projection_lock is not None:
            with projection_lock:
                cached_inspection = getattr(self, "_teammate_info_cache", None)
                cached_revision = int(as_number(getattr(
                    self, "_teammate_info_cache_revision", -1
                )))
                if cached_revision == inspection_revision and isinstance(cached_inspection, dict):
                    presentation_revision = max(
                        presentation_revision,
                        int(as_number(cached_inspection.get("presentationRevision"))),
                    )
        state["teammatePresentationRevision"] = presentation_revision
        state["teammateProfiles"] = teammate_payload["player_profiles"]
        state["teammateProfilePanels"] = teammate_payload["profile_panels"]
        return state
    def resolve_score_source_hash(self, source_hash: str) -> str:
        """Return the immutable key once a live capture has been finalized.

        A Stop response is intentionally asynchronous, so a browser request
        already in flight can still carry the old ``live:`` key.  Resolve it
        here before job deduplication and worker-limit selection, and register
        the frozen in-memory snapshot so scoring never falls back to replay
        decoding.
        """

        requested = norm(source_hash)
        resolver = getattr(self.live_capture, "resolve_score_source_hash", None)
        resolved = norm(resolver(requested)) if callable(resolver) else requested
        # A final score can be requested either with the stale ``live:`` key
        # (an in-flight browser request) or with the already promoted hash from
        # capture_status().  Register the source path in both cases so the
        # normal replay upload pipeline can persist the score and its capture.
        try:
            state = self.live_capture.status(include_snapshot=False)
        except (AttributeError, TypeError):
            state = {}
        self._register_capture_score_source(state, resolved)
        return resolved or requested
    def capture_timeline(self, after_sequence: int = 0) -> dict[str, Any]:
        return self.live_capture.timeline_status(after_sequence)

    def capture_voice_events(self) -> dict[str, Any]:
        """实时语音/倒计时事件流，附一份「播报白名单」提示。

        ``focusGroup`` 只描述事实（本机在打哪只 Boss、同场还有哪几只、哪些是
        场景 Boss），要不要过滤由前端的两个开关各自决定：语音一个开关、悬浮窗
        倒计时一个开关。

        ``splitGroup`` 保留为同义字段：旧前端与旧采集端按这个名字读，语义已经
        从「分边黑名单」变成「focus 白名单的 others」。
        """

        payload = self.live_capture.voice_status()
        hint = self._focus_hint()
        payload["focusGroup"] = hint
        payload["splitGroup"] = hint
        # 用 getattr 取：测试里的 host 是 SimpleNamespace，不会绑定私有方法。
        trace = getattr(self, "_trace_voice_runtime", None)
        if callable(trace):
            trace(payload, hint)
        return payload

    def _trace_voice_runtime(self, payload: dict[str, Any], hint: Any) -> None:
        """每秒把 focus 白名单也记进观测流水（voice_state 由采集侧写）。

        ``focus`` 直接决定前端那个「只播本机在打的 Boss」开关会不会把整批行
        过滤掉，所以它必须和事件在同一份日志里、同一时间基准上。
        """

        now = time.monotonic()
        if now - float(getattr(self, "_trace_focus_at", 0.0) or 0.0) < 1.0:
            return
        self._trace_focus_at = now
        current = hint.get("current") if isinstance(hint, dict) else None
        others = hint.get("others") if isinstance(hint, dict) else None
        self.live_capture.record_probe_event(
            "voice_focus",
            {
                "state": payload.get("state"),
                "current": current,
                "others": others,
                "focus": hint.get("focus") if isinstance(hint, dict) else None,
            },
        )

    def debug_probe_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """前端上报「这一批事件怎么处理的」，写进同一条观测流水。"""

        kind = str(payload.get("kind") or "frontend_event")
        fields = payload.get("fields")
        record = getattr(self.live_capture, "record_probe_event", None)
        if not callable(record):
            return {"ok": False}
        ok = record(kind, fields if isinstance(fields, dict) else {})
        return {"ok": ok}

    def _focus_hint(self) -> dict[str, Any] | None:
        """播报白名单提示；副本没识别、或选不出本机主目标时返回 None。

        一秒最多算一次：身份与攻击目标在 150ms 的轮询里几乎不变，重复计算只会
        在采集线程上徒增开销。
        """

        now = time.monotonic()
        cached_at = float(getattr(self, "_focus_hint_at", 0.0) or 0.0)
        if now - cached_at < 1.0:
            return getattr(self, "_focus_hint_value", None)
        hint: dict[str, Any] | None = None
        try:
            overlay_status = getattr(self.live_capture, "overlay_status", None)
            state = overlay_status() if callable(overlay_status) else {}
            if isinstance(state, dict):
                snapshot = state.get("overlaySnapshot") or state.get("snapshot")
                if isinstance(snapshot, dict):
                    hint = focus_boss_hint(
                        snapshot,
                        nest_key=state.get("nestKey"),
                        nest_label=state.get("nestLabel"),
                        self_source=snapshot.get("selfSourceId"),
                        # A 链路：本关在场的 Boss（出场绑定得到，不依赖出手）。
                        live_bosses=state.get("liveBosses") or (),
                    )
        except Exception:
            hint = None
        self._focus_hint_at = now
        self._focus_hint_value = hint
        return hint

    def _split_group_hint(self) -> dict[str, Any] | None:
        """旧名兼容：分边黑名单已并入 focus 白名单。"""

        return self._focus_hint()

    def wait_buff_overlay_change(
        self,
        after_revision: int = 0,
        timeout_ms: int = 15_000,
    ) -> dict[str, object]:
        """Long-poll one tiny Buff lifecycle signal for the desktop publisher."""

        waiter = getattr(self.live_capture, "wait_overlay_buff_revision", None)
        if not callable(waiter):
            return {
                "changed": False,
                "buffStateRevision": max(0, int(after_revision)),
            }
        return waiter(
            max(0, int(after_revision)),
            max(0, min(25_000, int(timeout_ms))) / 1000.0,
        )

    def buff_overlay_status(self) -> dict[str, Any]:
        """Return canonical live effects for the compact desktop overlay.

        This endpoint intentionally uses packet-defined intervals.  The only
        exception is an explicitly reviewed action-marker status whose real
        lifetime is reconstructed from its factual cast plus the shared
        activation rule; generic simulator duration corrections never replace
        the server clock shown by a live overlay.
        """
        # The overlay polls four times per second.  Use its compact state so a
        # long live session does not deep-copy the complete page snapshot on
        # every poll.  Older/test capture implementations can fall back to the
        # full status contract.
        self._ensure_overlay_response_cache()
        overlay_status = getattr(self.live_capture, "overlay_status", None)
        state = (
            overlay_status()
            if callable(overlay_status)
            else self.live_capture.status()
        )
        snapshot = state.get("overlaySnapshot") or state.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        players = snapshot.get("players")
        buffs = snapshot.get("player_buffs")
        summons = snapshot.get("summons")
        player_health = snapshot.get("player_health")
        self_source_id = norm(snapshot.get("selfSourceId"))
        # Buff intervals use the capture-relative status clock. Combat elapsed
        # excludes idle sections and therefore cannot be used as "now" here;
        # mixing the two clocks made durations look longer or shorter in the
        # overlay and kept expired effects alive.
        authoritative_elapsed = max(
            0.0,
            as_number(snapshot.get("boss_effect_current_seconds"))
            or as_number(snapshot.get("elapsed_seconds")),
        )
        elapsed = authoritative_elapsed
        # Correct the packet clock to response time. Snapshots are published
        # every 0.75s, so using the unaged value starts every countdown late.
        published_at = as_number(
            state.get("overlaySnapshotPublishedMonotonic")
            or state.get("snapshotPublishedMonotonic")
        )
        # The capture clock belongs to the capture session, not to packet
        # arrival. Keep extrapolating while the session is starting as well
        # as while it is running; a quiet UDP stream must not freeze the
        # overlay's countdown.
        if state.get("state") in {"starting", "running"} and published_at > 0.0:
            elapsed += max(0.0, time.monotonic() - published_at)

        overlay_revision = int(state.get("overlaySnapshotRevision") or 0)
        cache_key = (overlay_revision, self_source_id)
        with self._overlay_response_cache_lock:
            cached = (
                self._overlay_response_cache
                if self._overlay_response_cache_key == cache_key
                else None
            )
        if cached is not None:
            return self._age_buff_overlay_response(
                cached,
                elapsed,
                state=norm(state.get("state")) or "idle",
                status=norm(state.get("status")),
                latency=dict(state.get("overlayLatency") or {}),
            )

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for source in buffs if isinstance(buffs, list) else []:
            if not isinstance(source, dict):
                continue
            if is_inferred_aura_row(source) or (is_aura_row(source) and not is_aura_damage_series(source)):
                continue
            skill_key = _display_skill_key(
                source.get("skill_name") or source.get("skill_id")
            )
            catalog = self._live_buff_catalog.get(skill_key)
            # The full live page can retain diagnostic statuses. The overlay is
            # deliberately limited to the reviewed display catalog so hit-side
            # packet variants cannot create unrelated rows.
            if not catalog or catalog.get("overlay") != "1":
                continue
            source = acceleration_overlay_row(source, skill_key, elapsed)
            if source is None:
                continue
            target_id = norm(source.get("target_id"))
            grouped.setdefault((target_id, skill_key), []).append(source)

        raw_overlay_buff_casts = snapshot.get("buff_casts")
        if not isinstance(raw_overlay_buff_casts, list):
            raw_overlay_buff_casts = snapshot.get("team_buff_casts")
        if not isinstance(raw_overlay_buff_casts, list):
            raw_overlay_buff_casts = []
        store = getattr(self, "store", None)
        activation_loader = getattr(
            store, "buff_activation_rule_for_job", None
        )
        # A few server states are only short action markers or permanent-looking
        # carrier states. Silver Hunter's observed 1 s 134800 state and
        # Scorching Flame's zero-duration carrier are examples. For explicitly
        # reviewed catalog rows, combine the factual cast timestamp with the
        # authoritative current effect duration.
        for cast in raw_overlay_buff_casts:
            if not isinstance(cast, dict):
                continue
            if is_aura_row(cast):
                continue  # A cast cannot prove the recipient entered its range.
            skill_key = _display_skill_key(
                cast.get("skill_name") or cast.get("skill_id")
            )
            catalog = self._live_buff_catalog.get(skill_key, {})
            duration_from_activation = bool(
                catalog.get("cast_duration_from_activation_rule")
            )
            duration_from_skill_table = bool(
                catalog.get("cast_duration_from_skill_table")
            )
            if not (duration_from_activation or duration_from_skill_table):
                continue
            source_id = norm(cast.get("source_id"))
            source_job = norm(cast.get("source_job")) or norm(cast.get("job"))
            skill_name = norm(cast.get("skill_name")) or norm(
                catalog.get("skill")
            )
            cast_seconds = as_number(cast.get("cast_seconds"))
            activation = (
                activation_loader(
                    source_job,
                    skill_name,
                    "团队buff"
                    if norm(catalog.get("scope")) == "team"
                    else "个人buff",
                )
                if callable(activation_loader)
                else {}
            )
            timing_source = "cast_and_activation_rule"
            duration = max(0.0, as_number(activation.get("duration")))
            if duration_from_skill_table:
                timing_loader = getattr(store, "skill_effect_timing", None)
                fallback_level = int(
                    as_number(catalog.get("skill_table_fallback_level"))
                )
                fallback_max_required_level = max(
                    70,
                    int(
                        as_number(
                            catalog.get("skill_table_fallback_max_required_level"),
                            70,
                        )
                    ),
                )
                timing = (
                    timing_loader(
                        source_job,
                        skill_name,
                        level=fallback_level or None,
                        max_required_level=fallback_max_required_level,
                    )
                    if callable(timing_loader)
                    else {}
                )
                duration = max(0.0, as_number(timing.get("duration")))
                timing_source = "current_skill_table_fallback"
            if not source_id or cast_seconds <= 0.0 or duration <= 0.0:
                continue
            # Skill-table reconstruction exists for newly added/marker-only
            # effects such as Scorching Flame. Once a real finite status packet
            # for this actor is present, it owns the visible lifetime. A short
            # action marker remains insufficient and keeps the reviewed table
            # duration as the fallback.
            if duration_from_skill_table:
                minimum_packet_duration = max(1.0, duration * 0.25)
                packet_rows = grouped.get((source_id, skill_key), [])
                has_trustworthy_packet_interval = any(
                    (
                        norm(row.get("timing_source")) in {"", "server_status", "packet"}
                        and any(
                            isinstance(interval, (list, tuple))
                            and len(interval) >= 2
                            and abs(as_number(interval[0]) - cast_seconds) <= 2.0
                            and as_number(interval[1]) - as_number(interval[0])
                            >= minimum_packet_duration
                            for interval in row.get("intervals", [])
                        )
                    )
                    for row in packet_rows
                )
                if has_trustworthy_packet_interval:
                    continue
            end_seconds = cast_seconds + duration
            grouped.setdefault((source_id, skill_key), []).append(
                {
                    "target_id": source_id,
                    "target_job": source_job,
                    "source_id": source_id,
                    "job": source_job,
                    "skill_name": skill_name,
                    "active": cast_seconds <= elapsed < end_seconds,
                    "last_refresh_seconds": cast_seconds,
                    "start_seconds": cast_seconds,
                    "end_seconds": end_seconds,
                    "intervals": [[cast_seconds, end_seconds]],
                    "open_ended": False,
                    "timing_source": timing_source,
                }
            )

        overlay_buffs: list[dict[str, Any]] = []
        buff_observations: list[dict[str, Any]] = []
        for (target_id, skill_key), rows in grouped.items():
            # One skill application can carry both its real finite Buff and
            # permanent-looking action/resource components under the same
            # skill id. The visible lifetime belongs to the finite status.
            if not is_aura_row(rows[0]):
                rows = self._prefer_finite_live_buff_rows(rows)
            intervals = _merge_display_intervals(
                [
                    [as_number(interval[0]), as_number(interval[1])]
                    for row in rows
                    for interval in row.get("intervals", [])
                    if isinstance(interval, (list, tuple)) and len(interval) >= 2
                ]
            )
            # Retain one tiny latest-receipt observation even after expiry.
            # Missing-Buff alerts need a proven recipient lifetime; absence
            # from the active display list alone is not evidence of removal.
            if intervals:
                buff_observations.append({
                    "target_id": target_id,
                    "catalog_key": skill_key,
                    "end_seconds": max(interval[1] for interval in intervals),
                    "permanent": any(bool(row.get("open_ended")) for row in rows),
                })
            active_intervals = [
                interval
                for interval in intervals
                if interval[0] <= elapsed + 0.05 and interval[1] > elapsed
            ]
            active_rows = [row for row in rows if bool(row.get("active"))]
            if not active_intervals and not active_rows:
                continue

            representative = max(
                rows,
                key=lambda row: as_number(row.get("last_refresh_seconds")),
            )
            catalog = self._live_buff_catalog[skill_key]
            skill_name = norm(representative.get("skill_name")) or norm(
                catalog.get("skill")
            )
            raw_start = (
                max(interval[0] for interval in active_intervals)
                if active_intervals
                else as_number(representative.get("start_seconds"))
            )
            raw_end = (
                max(interval[1] for interval in active_intervals)
                if active_intervals
                else as_number(representative.get("end_seconds"))
            )
            # ``active`` is an event-side hint. It can remain true when the
            # packet that closes the effect is absent (for example while no
            # UDP packets are arriving). A finite interval must therefore
            # remain finite even if that stale flag is still present; only an
            # open-ended row or a deliberately very long interval is
            # permanent.
            source_intervals = [
                [as_number(interval[0]), as_number(interval[1])]
                for row in rows
                for interval in row.get("intervals", [])
                if isinstance(interval, (list, tuple))
                and len(interval) >= 2
                and as_number(interval[1]) > as_number(interval[0]) + 1e-6
            ]
            source_intervals.extend(
                [
                    [
                        as_number(row.get("start_seconds")),
                        as_number(row.get("end_seconds")),
                    ]
                    for row in rows
                    if as_number(row.get("end_seconds"))
                    > as_number(row.get("start_seconds")) + 1e-6
                ]
            )
            has_finite_application = bool(source_intervals)
            # The aggregator clips an open-ended server status to
            # ``snapshot_now + 1ms`` before serializing it.  The overlay clock
            # is deliberately extrapolated by response age, so comparing that
            # clipped end directly with the extrapolated clock makes every
            # still-live permanent status disappear between polls.  Use the
            # explicit marker when available; keep the clock-proximity fallback
            # for older cached snapshots/builds.
            open_ended = any(bool(row.get("open_ended")) for row in rows)
            if not open_ended and active_rows:
                open_ended = (
                    authoritative_elapsed - 0.05
                    <= raw_end
                    <= authoritative_elapsed + 0.25
                )
            last_refresh = max(
                (as_number(row.get("last_refresh_seconds")) for row in rows),
                default=0.0,
            )
            permanent = (
                open_ended
            ) or raw_end - elapsed > 3600
            if permanent:
                start = raw_start
                end = 0.0
                duration = 0.0
            else:
                start = raw_start
                end = raw_end
                # A refreshable status can be one long merged interval.  The
                # visible bar represents the current server application, so it
                # starts at the final refresh without changing the real expiry.
                if (
                    not (
                        catalog.get("cast_duration_from_activation_rule")
                        or catalog.get("cast_duration_from_skill_table")
                    )
                    and raw_start <= last_refresh < raw_end
                ):
                    start = last_refresh
                duration = max(0.1, end - start)
                if end <= elapsed:
                    continue

            timing_representative = max(
                rows,
                key=lambda row: (
                    any(
                        isinstance(interval, (list, tuple))
                        and len(interval) >= 2
                        and as_number(interval[0]) <= elapsed < as_number(interval[1])
                        for interval in row.get("intervals", [])
                    ),
                    max(
                        (
                            as_number(interval[1])
                            for interval in row.get("intervals", [])
                            if isinstance(interval, (list, tuple)) and len(interval) >= 2
                        ),
                        default=as_number(row.get("end_seconds")),
                    ),
                    as_number(row.get("last_refresh_seconds")),
                ),
            )
            timing_source = (
                norm(timing_representative.get("timing_source"))
                or "server_status"
            )

            overlay_buffs.append(
                {
                    "key": f"{target_id}|{skill_key}",
                    "catalog_key": skill_key,
                    "skill_name": skill_name,
                    "label": norm(catalog.get("display")),
                    "status_kind": norm(catalog.get("category")),
                    "display_order": int(catalog.get("display_order") or 0),
                    "provider_order": int(catalog.get("provider_order") or 0),
                    "icon_index": int(catalog.get("icon_index", -1)),
                    "target_id": target_id,
                    "target_job": norm(representative.get("target_job")),
                    # Same-name effects are collapsed to one visible row.  Its
                    # source must still be the actor that supplied the active
                    # application, not a static list of every profession that
                    # can own the skill or every source seen earlier.
                    "source_id": norm(representative.get("source_id"))
                    or self._joined_live_buff_field(rows, "source_id"),
                    "job": norm(representative.get("job"))
                    or self._joined_live_buff_field(rows, "job"),
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": duration,
                    "remaining_seconds": 0.0 if permanent else max(0.0, end - elapsed),
                    "permanent": permanent,
                    "timing_source": timing_source,
                    "data_source": (
                        "抓包数据"
                        if timing_source in {"server_status", "packet"}
                        else "预设数据兜底"
                    ),
                }
            )
        overlay_buffs.sort(
            key=lambda row: (
                norm(row.get("target_id")),
                bool(row.get("permanent")),
                as_number(row.get("end_seconds")),
                norm(row.get("skill_name")),
            )
        )
        player_rows: dict[str, dict[str, Any]] = {}
        for row in (players if isinstance(players, list) else []):
            if not isinstance(row, dict):
                continue
            source_id = norm(row.get("source_id"))
            if not source_id or (
                source_id != self_source_id
                and not _is_recognized_player_job(norm(row.get("job")))
            ):
                continue
            player_rows[source_id] = dict(row)
        # Full snapshots omit teammates with no combat totals. Only independent
        # party/action evidence may restore those rows; cached Buff identities
        # alone must never create missing-Buff alert recipients.
        for row in snapshot.get("party_members", []):
            if not isinstance(row, dict):
                continue
            source_id = norm(row.get("actor_id"))
            if (
                not source_id
                or as_number(row.get("confidence")) < 0.65
                or not _is_recognized_player_job(norm(row.get("job")))
            ):
                continue
            existing = player_rows.setdefault(source_id, {"source_id": source_id})
            for field in ("job", "name"):
                if not norm(existing.get(field)):
                    existing[field] = norm(row.get(field))
        if self_source_id:
            player_rows.setdefault(self_source_id, {"source_id": self_source_id})
        for row in (buffs if isinstance(buffs, list) else []):
            if not isinstance(row, dict):
                continue
            target_id = norm(row.get("target_id"))
            if target_id not in player_rows:
                continue
            existing = player_rows[target_id]
            if not norm(existing.get("job")):
                existing["job"] = norm(row.get("target_job"))
            if not norm(existing.get("name")):
                existing["name"] = norm(row.get("target_name"))

        # Keep unrelated cached targets out of the frontend's observed team
        # Buff catalog too, not just its recipient list. The raw ledger stays
        # intact for replay diagnostics and later identity resolution.
        overlay_buffs = [
            row for row in overlay_buffs
            if norm(row.get("target_id")) in player_rows
        ]
        buff_observations = [
            row for row in buff_observations
            if norm(row.get("target_id")) in player_rows
        ]

        overlay_players: list[dict[str, Any]] = []
        for source_id, row in player_rows.items():
            job = norm(row.get("job"))
            profile = self._overlay_job_profile(job)
            overlay_players.append(
                {
                    "source_id": source_id,
                    "job": job,
                    "name": norm(row.get("name")),
                    "damage": int(as_number(row.get("damage"))),
                    "dps": as_number(row.get("dps")),
                    "damage_series": row.get("damage_series", []),
                    **profile,
                }
            )
        overlay_players.sort(
            key=lambda row: (
                0 if norm(row.get("source_id")) == self_source_id else 1,
                norm(row.get("job")),
                norm(row.get("source_id")),
            )
        )

        overlay_summons: list[dict[str, Any]] = []
        for source in summons if isinstance(summons, list) else []:
            if not isinstance(source, dict):
                continue
            catalog_key = norm(source.get("catalog_key"))
            source_id = norm(source.get("source_id"))
            end_seconds = as_number(source.get("end_seconds"))
            if not catalog_key or not source_id or end_seconds <= elapsed:
                continue
            player = player_rows.get(source_id, {})
            overlay_summons.append(
                {
                    "key": norm(source.get("key"))
                    or f"{source_id}|{catalog_key}|{end_seconds:.3f}",
                    "catalog_key": catalog_key,
                    "skill_id": norm(source.get("skill_id")),
                    "skill_name": norm(source.get("skill_name")) or catalog_key,
                    "source_id": source_id,
                    "source_name": norm(source.get("source_name"))
                    or norm(player.get("name")),
                    "source_job": norm(source.get("source_job"))
                    or norm(player.get("job")),
                    "start_seconds": as_number(source.get("start_seconds")),
                    "end_seconds": end_seconds,
                    "duration_seconds": max(
                        0.1, as_number(source.get("duration_seconds"))
                    ),
                    "remaining_seconds": max(0.0, end_seconds - elapsed),
                    "detection_source": norm(source.get("detection_source"))
                    or "skill_cast",
                }
            )
        overlay_summons.sort(
            key=lambda row: (
                norm(row.get("source_job")),
                norm(row.get("source_id")),
                norm(row.get("skill_name")),
                as_number(row.get("end_seconds")),
            )
        )

        boss_effects = snapshot.get("boss_runtime_effects")
        if not isinstance(boss_effects, list) or not boss_effects:
            boss_effects = snapshot.get("boss_effects")
        boss_targets = snapshot.get("boss_runtime_targets")
        # An empty runtime list is not the same statement as a missing one: the
        # runtime list only carries *recognized* Boss entities, so it stays empty
        # for training dummies and unrecognized targets.  Treating it as
        # authoritative silently produced zero debuff rows there, even though the
        # same snapshot had a confirmed damage target and live Boss effects.
        if not isinstance(boss_targets, list) or not boss_targets:
            boss_targets = snapshot.get("damage_targets")
        ended_boss_ids = set(snapshot.get("endedBossIds") or [])
        if isinstance(boss_effects, list):
            boss_effects = [row for row in boss_effects
                            if norm(row.get("target_id")) not in ended_boss_ids]
        if isinstance(boss_targets, list):
            boss_targets = [row for row in boss_targets
                            if norm(row.get("target_id")) not in ended_boss_ids]
        # A target list without any confirmed hit is not evidence that the Boss
        # is unknown to us: the lightweight capture profile drops damage events,
        # so ``damage_targets`` rows stay zeroed (damage=0, last_ms=0) while the
        # Boss status effects are decoded normally.  Passing such a list makes
        # ``_overlay_debuff_rows`` fail closed and publish zero debuff rows, so
        # hand it ``None`` instead and let the live effects pick their target.
        debuff_targets = boss_targets if isinstance(boss_targets, list) else None
        if debuff_targets is not None and not any(
            as_number(row.get("last_ms")) > 0.0
            for row in debuff_targets
            if isinstance(row, dict)
        ):
            debuff_targets = None
        overlay_debuffs = self._overlay_debuff_rows(
            boss_effects if isinstance(boss_effects, list) else [],
            elapsed,
            debuff_targets,
        )
        icon_indices = getattr(self, "_live_skill_icon_indices", {})
        for debuff in overlay_debuffs:
            for contributor in debuff.get("contributors", []):
                if not isinstance(contributor, dict):
                    continue
                contributor["icon_index"] = int(
                    icon_indices.get(
                        norm(contributor.get("skill_id")),
                        icon_indices.get(
                            _display_skill_key(contributor.get("skill_name")),
                            -1,
                        ),
                    )
                )
        buff_options = sorted(
            (
                {
                    "key": key,
                    "skill": norm(row.get("skill")),
                    "label": norm(row.get("display")),
                    "category": norm(row.get("category")),
                    "scope": norm(row.get("scope")) or "personal",
                    "fullDurationTeam": bool(row.get("full_duration_team")),
                    "provider": norm(row.get("provider")),
                    "displayOrder": int(row.get("display_order") or 0),
                    "providerOrder": int(row.get("provider_order") or 0),
                    "iconIndex": int(row.get("icon_index", -1)),
                    "cooldownTrackable": bool(
                        row.get("cooldown_trackable")
                    ),
                }
                for key, row in self._live_buff_catalog.items()
                if row.get("overlay") == "1"
            ),
            key=lambda row: (
                row["providerOrder"],
                row["displayOrder"],
                row["skill"],
            ),
        )
        buff_clear_revision = int(
            as_number(snapshot.get("player_buff_clear_revision"))
        )
        overlay_buff_clear_events: list[dict[str, Any]] = []
        raw_buff_clear_events = snapshot.get("player_buff_clear_events")
        for source in (
            raw_buff_clear_events
            if isinstance(raw_buff_clear_events, list)
            else []
        ):
            if not isinstance(source, dict):
                continue
            targets: dict[str, dict[str, str]] = {}
            for closed in source.get("closed_buffs", []):
                if not isinstance(closed, dict):
                    continue
                skill_key = _display_skill_key(
                    closed.get("skill_name") or closed.get("skill_id")
                )
                catalog = self._live_buff_catalog.get(skill_key)
                # Match the visible overlay contract: an explicit Boss clear
                # only raises a notification when it actually removed at
                # least one reviewed, selectable Buff from that target.
                if not catalog or catalog.get("overlay") != "1":
                    continue
                target_id = norm(closed.get("target_id"))
                if not target_id:
                    continue
                player = player_rows.get(target_id, {})
                targets[target_id] = {
                    "target_id": target_id,
                    "target_name": norm(closed.get("target_name"))
                    or norm(player.get("name")),
                    "target_job": norm(closed.get("target_job"))
                    or norm(player.get("job")),
                }
            if not targets:
                continue
            revision = int(as_number(source.get("revision")))
            timestamp_seconds = as_number(source.get("timestamp_seconds"))
            overlay_buff_clear_events.append(
                {
                    "key": f"{revision}:{timestamp_seconds:.3f}",
                    "revision": revision,
                    "timestamp_seconds": timestamp_seconds,
                    "source_id": norm(source.get("source_id")),
                    "source_name": norm(source.get("source_name")),
                    "skill_id": norm(source.get("skill_id")),
                    "skill_name": norm(source.get("skill_name")),
                    "targets": list(targets.values()),
                }
            )
        cooldown_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        cooldown_refunds: dict[tuple[str, str], dict[str, Any]] = {}
        raw_cooldown_refunds = snapshot.get("cooldown_refunds")
        for raw_refund in (
            raw_cooldown_refunds
            if isinstance(raw_cooldown_refunds, list)
            else []
        ):
            if not isinstance(raw_refund, dict):
                continue
            refund_source_id = norm(raw_refund.get("source_id"))
            refund_skill_key = _display_skill_key(
                raw_refund.get("target_skill_name")
                or raw_refund.get("target_skill_id")
            )
            if refund_source_id and refund_skill_key:
                cooldown_refunds[(refund_source_id, refund_skill_key)] = dict(
                    raw_refund
                )

        def remember_cooldown(
            *,
            source_id: str,
            source_job: str,
            skill_key: str,
            skill_name: str,
            cast_seconds: float,
        ) -> bool:
            effect_providers = self._live_buff_catalog.get(skill_key, {}).get(
                "effect_providers", []
            )
            if effect_providers and source_job not in effect_providers:
                # This is effect/CD ownership, not the recipient's profession.
                # Keep factual received shield statuses visible to every job.
                return False
            store = getattr(self, "store", None)
            details_loader = getattr(
                store, "skill_reference_cooldown_details", None
            )
            if callable(details_loader):
                cooldown_details = details_loader(
                    source_job, skill_name, 0.0
                )
            else:
                fallback_cooldown = (
                    store.skill_reference_cooldown(
                        source_job, skill_name, 0.0
                    )
                    if store is not None
                    else 0.0
                )
                cooldown_details = {
                    "cooldown": fallback_cooldown,
                    "source": "reference",
                    "source_label": "技能CD数据",
                }
            base_cooldown = max(
                0.0, as_number(cooldown_details.get("cooldown"))
            )
            cooldown_multiplier = 1.0
            mechanic_multipliers = JOB_MECHANICS.job_value(
                source_job, "team_effect_cooldown_multipliers", {}
            )
            if isinstance(mechanic_multipliers, dict):
                cooldown_multiplier = max(
                    0.0,
                    as_number(
                        mechanic_multipliers.get(skill_name), 1.0
                    ),
                )
            cooldown = base_cooldown * cooldown_multiplier
            glyph_percent = _OVERLAY_DEFAULT_COOLDOWN_GLYPH_PERCENT.get(
                skill_name, 0.0
            )
            if glyph_percent > 0.0:
                cooldown *= 1.0 - glyph_percent / 100.0
            if not source_id or cooldown <= 0.0 or cast_seconds <= 0.0:
                return False
            cooldown_key = (source_id, skill_key)
            refund = cooldown_refunds.get(cooldown_key, {})
            refund_cycle = as_number(
                refund.get("cycle_cast_seconds"), -1.0
            )
            refund_seconds = (
                max(0.0, as_number(refund.get("refund_seconds")))
                if abs(refund_cycle - cast_seconds) <= 0.001
                else 0.0
            )
            refund_seconds = min(cooldown, refund_seconds)
            ready_seconds = cast_seconds + cooldown - refund_seconds
            candidate = {
                "key": f"{source_id}|{skill_key}",
                "catalog_key": skill_key,
                "skill_name": skill_name,
                "source_id": source_id,
                "source_job": source_job,
                "cast_seconds": cast_seconds,
                "base_cooldown_seconds": base_cooldown,
                "cooldown_seconds": cooldown,
                "cooldown_source": norm(cooldown_details.get("source")),
                "cooldown_source_label": norm(
                    cooldown_details.get("source_label")
                ),
                "intrinsic_cooldown_multiplier": cooldown_multiplier,
                "cooldown_glyph_percent": glyph_percent,
                "cooldown_refund_seconds": refund_seconds,
                "cooldown_refund_events": int(
                    as_number(refund.get("event_count"))
                ),
                "cooldown_refund_evidence": norm(refund.get("evidence")),
                "ready_seconds": ready_seconds,
                "remaining_seconds": max(
                    0.0, ready_seconds - elapsed
                ),
            }
            previous = cooldown_by_key.get(cooldown_key)
            if previous is None or cast_seconds > as_number(
                previous.get("cast_seconds")
            ):
                cooldown_by_key[cooldown_key] = candidate
            return True

        # A real cast is authoritative for Key Buff CD ownership. Several team
        # buffs fan out one status per recipient and some packet layouts put the
        # recipient in the status source field; deriving cooldown ownership
        # from those mirrors produced one row for every teammate. The compact
        # aggregator keeps only the latest cast per actor/skill, so this remains
        # bounded and does not scan the battle timeline.
        cast_backed_actor_skill_keys: set[tuple[str, str]] = set()
        cast_backed_team_skill_keys: set[str] = set()
        for cast in raw_overlay_buff_casts:
            if not isinstance(cast, dict):
                continue
            skill_key = _display_skill_key(
                cast.get("skill_name") or cast.get("skill_id")
            )
            catalog = self._live_buff_catalog.get(skill_key, {})
            if catalog.get("overlay") != "1":
                continue
            source_id = norm(cast.get("source_id"))
            source_job = norm(cast.get("source_job")) or norm(cast.get("job"))
            skill_name = norm(cast.get("skill_name")) or norm(
                catalog.get("skill")
            )
            if remember_cooldown(
                source_id=source_id,
                source_job=source_job,
                skill_key=skill_key,
                skill_name=skill_name,
                cast_seconds=as_number(cast.get("cast_seconds")),
            ):
                cast_backed_actor_skill_keys.add((source_id, skill_key))
                if norm(catalog.get("scope")) == "team":
                    cast_backed_team_skill_keys.add(skill_key)

        for (target_id, skill_key), rows in grouped.items():
            catalog = self._live_buff_catalog.get(skill_key, {})
            if catalog.get("overlay") != "1":
                continue
            if (
                norm(catalog.get("scope")) == "team"
                and skill_key in cast_backed_team_skill_keys
            ):
                continue
            representative = max(
                rows,
                key=lambda row: as_number(row.get("last_refresh_seconds")),
            )
            source_id = norm(representative.get("source_id"))
            if (source_id, skill_key) in cast_backed_actor_skill_keys:
                continue
            source_job = norm(representative.get("job"))
            skill_name = norm(representative.get("skill_name")) or norm(
                catalog.get("skill")
            )
            cast_seconds = max(
                [
                    as_number(interval[0])
                    for row in rows
                    for interval in row.get("intervals", [])
                    if isinstance(interval, (list, tuple)) and len(interval) >= 2
                ]
                + [as_number(row.get("last_refresh_seconds")) for row in rows],
                default=as_number(representative.get("start_seconds")),
            )
            remember_cooldown(
                source_id=source_id,
                source_job=source_job,
                skill_key=skill_key,
                skill_name=skill_name,
                cast_seconds=cast_seconds,
            )
        cooldown_rows = list(cooldown_by_key.values())
        cooldown_rows.sort(
            key=lambda row: (
                as_number(row.get("remaining_seconds")),
                norm(row.get("source_job")),
                norm(row.get("skill_name")),
            )
        )
        # 悬浮窗第二行要显示「当前 Boss 是谁」。优先用队伍正在打的目标（也就是
        # 界面上的那条血量行），没有时退回快照里任一未结束的 Boss 实例，
        # 这样刚进本、还没开打时也能立刻显示名字。
        # 分边副本（黑龙硬核 / 沙龙硬核）另外按「本机角色正在打的那一只」定，
        # 否则左右两边同时掉血会让这一行来回跳。
        overlay_boss_health = party_boss_health(snapshot, set(player_rows))
        boss_identity = overlay_boss_identity(
            snapshot,
            overlay_boss_health,
            split_boss=nest_uses_split_boss(
                state.get("nestKey"), state.get("nestLabel")
            ),
            # A 链路：本关在场的 Boss 唯一时，进场第一秒就显示它。
            live_bosses=state.get("liveBosses") or (),
        )
        boss_display_name = boss_identity["name"]
        boss_target_id = boss_identity["targetId"]
        boss_template_id = boss_identity["templateId"]
        response = {
            "state": state.get("state", "idle"),
            "status": state.get("status", ""),
            "elapsedSeconds": elapsed,
            "overlayRevision": overlay_revision,
            "buffStateRevision": int(
                snapshot.get("player_buff_state_revision") or 0
            ),
            "latency": dict(state.get("overlayLatency") or {}),
            "selfSourceId": self_source_id,
            # 悬浮窗前两行的身份信息：副本与当前 Boss。只用于展示，不参与统计或播报判定。
            # sceneName 会包含城镇/庭院，保证「抓到了包就不会显示未识别」。
            **overlay_scene_identity(state),
            "nestName": norm(state.get("nestName")),
            "nestLabel": norm(state.get("nestLabel")),
            "nestKey": norm(state.get("nestKey")),
            "nestConfidence": norm(state.get("nestConfidence")),
            "bossName": boss_display_name,
            "bossTemplateId": boss_template_id,
            "bossTargetId": boss_target_id,
            "bossHealth": overlay_boss_health,
            "endedBossIds": sorted(ended_boss_ids),
            "selfAttackTarget": (snapshot.get("lastAttackTargets") or {}).get(self_source_id),
            "players": overlay_players,
            "buffs": overlay_buffs,
            "buffObservations": buff_observations,
            "debuffs": overlay_debuffs,
            "cooldowns": cooldown_rows,
            "summons": overlay_summons,
            "playerHealth": [
                row
                for row in (player_health if isinstance(player_health, list) else [])
                if isinstance(row, dict)
            ],
            "buffClearRevision": buff_clear_revision,
            "buffClearEvents": overlay_buff_clear_events,
            "buffOptions": buff_options,
            "summonOptions": summon_overlay_options(),
        }
        with self._overlay_response_cache_lock:
            self._overlay_response_cache_key = cache_key
            self._overlay_response_cache = response
            self._overlay_json_cache_key = None
            self._overlay_json_cache = b""
        return copy.deepcopy(response)
    @staticmethod
    def _age_buff_overlay_response(
        cached: dict[str, Any],
        elapsed: float,
        *,
        state: str,
        status: str,
        latency: dict[str, Any],
    ) -> dict[str, Any]:
        """Age a cached compact response without rebuilding effect groups."""

        result = copy.deepcopy(cached)
        result["state"] = state
        result["status"] = status
        result["elapsedSeconds"] = elapsed
        result["latency"] = latency
        aged_buffs: list[dict[str, Any]] = []
        for row in result.get("buffs", []):
            end = as_number(row.get("end_seconds"))
            if not row.get("permanent") and end > 0.0 and end <= elapsed:
                continue
            row["remaining_seconds"] = (
                0.0 if row.get("permanent") else max(0.0, end - elapsed)
            )
            aged_buffs.append(row)
        result["buffs"] = aged_buffs
        aged_debuffs: list[dict[str, Any]] = []
        for row in result.get("debuffs", []):
            end = as_number(row.get("end_seconds"))
            if end > 0.0 and end <= elapsed:
                continue
            row["remaining_seconds"] = max(0.0, end - elapsed) if end else 0.0
            aged_contributors: list[dict[str, Any]] = []
            for contributor in row.get("contributors", []):
                contributor_end = as_number(contributor.get("end_seconds"))
                if contributor_end > 0.0 and contributor_end <= elapsed:
                    continue
                contributor["remaining_seconds"] = (
                    max(0.0, contributor_end - elapsed)
                    if contributor_end
                    else 0.0
                )
                aged_contributors.append(contributor)
            row["contributors"] = aged_contributors
            aged_debuffs.append(row)
        result["debuffs"] = aged_debuffs
        for row in result.get("cooldowns", []):
            row["remaining_seconds"] = max(
                0.0, as_number(row.get("ready_seconds")) - elapsed
            )
        aged_summons: list[dict[str, Any]] = []
        for row in result.get("summons", []):
            end = as_number(row.get("end_seconds"))
            if end <= elapsed:
                continue
            row["remaining_seconds"] = max(0.0, end - elapsed)
            aged_summons.append(row)
        result["summons"] = aged_summons
        return result
    def buff_overlay_status_json(self, include_catalog: bool = True) -> bytes:
        """Return compact JSON, optionally omitting immutable option lists.

        The main publisher requests the catalogue once and stores it for every
        on-demand WebView. Subsequent 500 ms snapshots no longer serialize or
        transfer the same Buff/summon option tables.
        """

        self._ensure_overlay_response_cache()
        payload = self.buff_overlay_status()
        if not include_catalog:
            payload.pop("buffOptions", None)
            payload.pop("summonOptions", None)
        cache_key = (
            int(payload.get("overlayRevision") or 0),
            int(as_number(payload.get("elapsedSeconds")) * 4.0),
            norm(payload.get("state")),
            bool(include_catalog),
        )
        with self._overlay_response_cache_lock:
            if self._overlay_json_cache_key == cache_key:
                return self._overlay_json_cache
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        with self._overlay_response_cache_lock:
            self._overlay_json_cache_key = cache_key
            self._overlay_json_cache = encoded
        return encoded
    def _ensure_overlay_response_cache(self) -> None:
        """Initialize cache fields for legacy embedded/test core instances."""

        if hasattr(self, "_overlay_response_cache_lock"):
            return
        self._overlay_response_cache_lock = threading.RLock()
        self._overlay_response_cache_key = None
        self._overlay_response_cache = None
        self._overlay_json_cache_key = None
        self._overlay_json_cache = b""
    @staticmethod
    def _teammate_profile_projection_identity(
        profile: dict[str, Any],
    ) -> str:
        fields = profile.get("fields", ())
        values = {
            norm(row[0]): norm(row[1])
            for row in fields
            if isinstance(row, (list, tuple))
            and len(row) >= 2
            and norm(row[0])
        }
        profile_id = next((
            values.get(key, "")
            for key in ("角色档案标识", "稳定档案ID", "profile_id")
            if values.get(key, "")
        ), "")
        return (
            profile_id.upper()
            or norm(profile.get("target_id")).upper()
            or norm(profile.get("inspection_key"))
        )

    def _teammate_profile_projection_key(
        self, profile: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        try:
            build_context = self.get_build("build1")
        except (AttributeError, KeyError):
            # Lightweight embedded/test hosts may expose profile projection
            # without the persisted build store used by the packaged app.
            build_context = {}
        signature_payload = {
            "identity": self._teammate_profile_projection_identity(profile),
            "target_name": profile.get("target_name"),
            "job": profile.get("job"),
            "timestamp_ms": profile.get("timestamp_ms"),
            "inspection_key": profile.get("inspection_key"),
            "request_fingerprint": profile.get("request_fingerprint"),
            "response_layout": profile.get("response_layout"),
            "fields": profile.get("fields"),
            "equipment": profile.get("equipment"),
            "cards": profile.get("cards"),
            "stats": profile.get("stats"),
            "build": build_context,
        }
        cache_key = hashlib.sha256(json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        return cache_key, build_context

    def _teammate_profile_projection_cached(
        self, profile: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Read one completed panel without waiting for its calculator."""

        cache_key, _build_context = self._teammate_profile_projection_key(profile)
        lock = getattr(self, "_teammate_profile_projection_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._teammate_profile_projection_lock = lock
        with lock:
            cache = getattr(self, "_teammate_profile_projection_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._teammate_profile_projection_cache = cache
            cached = cache.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached[0]), copy.deepcopy(cached[1])
        return None

    def _teammate_profile_projection(
        self, profile: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Calculate one profile outside the latency-sensitive cache lock."""

        cache_key, _build_context = self._teammate_profile_projection_key(profile)
        cached = self._teammate_profile_projection_cached(profile)
        if cached is not None:
            return cached

        # Full equipment mapping can take more than a second. Never hold the
        # projection cache lock while it runs: the 200 ms overlay endpoint and
        # the normal live-page status request must remain non-blocking.
        enriched = self._with_profile_panels({
            "player_profiles": [profile],
            "party_members": [],
        })
        profiles = enriched.get("player_profiles", [])
        resolved = (
            profiles[0]
            if isinstance(profiles, list) and profiles
            else copy.deepcopy(profile)
        )
        target_id = norm(resolved.get("target_id"))
        panel = enriched.get("profile_panels", {}).get(target_id, {})
        if not isinstance(panel, dict):
            panel = {}
        lock = getattr(self, "_teammate_profile_projection_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._teammate_profile_projection_lock = lock
        with lock:
            cache = getattr(self, "_teammate_profile_projection_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._teammate_profile_projection_cache = cache
            # Viewing equipment is transient. Keep only the current projected
            # panel so long-running clients cannot accumulate complete decoded
            # equipment bodies in this secondary cache.
            cache.clear()
            cache[cache_key] = (copy.deepcopy(resolved), copy.deepcopy(panel))
            return copy.deepcopy(resolved), copy.deepcopy(panel)

    @staticmethod
    def _teammate_identity_result(
        profile: dict[str, Any],
        revision: int,
        *,
        body_decoded: bool,
        message: str,
        panel_state: str,
    ) -> dict[str, Any]:
        target_id = norm(profile.get("target_id"))
        profile_fields = {
            norm(row[0]): norm(row[1])
            for row in profile.get("fields", [])
            if isinstance(row, (list, tuple))
            and len(row) >= 2
            and norm(row[0])
        }
        profile_id = next((
            profile_fields.get(key, "")
            for key in ("角色档案标识", "稳定档案ID", "profile_id")
            if profile_fields.get(key, "")
        ), "")
        if not profile_id and target_id.casefold().startswith("profile:"):
            profile_id = target_id.split(":", 1)[1]
        display_name = norm(profile.get("target_name"))
        if re.fullmatch(r"(?:角色\s*)?0x[0-9A-Fa-f]+", display_name):
            display_name = ""
        inspection_key = (
            norm(profile.get("inspection_key")) or f"revision:{revision}"
        )
        packet_epoch_match = re.search(r":(\d{11,})$", inspection_key)
        return {
            "state": "unavailable",
            "revision": revision,
            "presentationRevision": max(0, revision) * 2,
            "inspectionKey": inspection_key,
            "name": display_name,
            "job": norm(profile.get("job")),
            "gameId": profile_id or target_id,
            "profileId": profile_id,
            "identityResolved": bool(display_name),
            "historyReady": bool(display_name or profile_id),
            "bodyDecoded": bool(body_decoded),
            "panelState": panel_state,
            "message": message,
            "packetRequestEpochMs": (
                int(packet_epoch_match.group(1)) if packet_epoch_match else 0
            ),
            "decodedPublishedEpochMs": int(
                as_number(profile.get("decoded_published_epoch_ms"))
            ),
            "statusPublishedEpochMs": round(time.time() * 1000),
        }

    def _calculate_teammate_info_result(
        self,
        profile: dict[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        resolved, panel = self._teammate_profile_projection(profile)
        fallback = self._teammate_identity_result(
            resolved,
            revision,
            body_decoded=True,
            message=(
                norm(panel.get("message"))
                if isinstance(panel, dict)
                else ""
            ) or "装备正文已解密，但总面板暂时无法计算",
            panel_state="error",
        )
        if not isinstance(panel, dict) or panel.get("state") != "ready":
            fallback["presentationRevision"] = max(0, revision) * 2 + 1
            return fallback

        panel_rows = {
            norm(row.get("key")): row
            for row in panel.get("rows", [])
            if isinstance(row, dict) and norm(row.get("key"))
        }

        def metric(key: str) -> float:
            row = panel_rows.get(key, {})
            return as_number(row.get("estimated", row.get("confirmed")))

        job = norm(resolved.get("job"))
        effective = next(
            (
                row
                for row in self.store.tables.get("effective", [])
                if norm(row.get("职业")) == job
            ),
            {},
        )
        element_candidates: list[tuple[float, int, str]] = []
        for index in range(1, 4):
            element = norm(effective.get(f"属性{index}"))
            if element and element != NONE_TEXT:
                element_candidates.append(
                    (as_number(effective.get(f"权重{index}")), -index, element)
                )
        primary_element = max(element_candidates)[2] if element_candidates else "无"
        element_key = {
            "火": "fire_attack",
            "水": "water_attack",
            "光": "light_attack",
            "暗": "dark_attack",
        }.get(primary_element, "")
        result = {
            **fallback,
            "state": "ready",
            "presentationRevision": max(0, revision) * 2 + 1,
            "panelState": "ready",
            "physicalAttackMin": metric("physical_attack_min"),
            "physicalAttackMax": metric("physical_attack_max"),
            "magicAttackMin": metric("magic_attack_min"),
            "magicAttackMax": metric("magic_attack_max"),
            "critPercent": metric("crit_percent"),
            "critDamagePercent": metric("crit_damage_percent"),
            "finalPercent": metric("final_percent"),
            "element": primary_element,
            "elementAttackPercent": metric(element_key) if element_key else 0.0,
        }
        return result

    def _ensure_teammate_projection_worker(self) -> threading.RLock:
        lock = getattr(self, "_teammate_projection_worker_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._teammate_projection_worker_lock = lock
        with lock:
            thread = getattr(self, "_teammate_projection_worker", None)
            if thread is None or not thread.is_alive():
                self._teammate_projection_pending = None
                self._teammate_projection_active_token = ""
                self._teammate_projection_wake = threading.Event()
                self._teammate_projection_stop = threading.Event()
                thread = threading.Thread(
                    target=self._teammate_projection_loop,
                    name="V410TeammatePanelProjection",
                    daemon=True,
                )
                self._teammate_projection_worker = thread
                thread.start()
        return lock

    def _schedule_teammate_projection(
        self,
        profile: dict[str, Any],
        revision: int,
    ) -> None:
        lock = self._ensure_teammate_projection_worker()
        inspection_key = norm(profile.get("inspection_key"))
        token = f"{int(revision)}:{inspection_key}:{norm(profile.get('response_layout'))}"
        with lock:
            pending = getattr(self, "_teammate_projection_pending", None)
            if (
                token == getattr(self, "_teammate_projection_active_token", "")
                or isinstance(pending, tuple) and pending[0] == token
            ):
                return
            # Only the latest click matters to the two on-demand windows. A
            # single replacement slot prevents ten rapid inspections from
            # becoming ten seconds of obsolete panel work.
            self._teammate_projection_pending = (
                token,
                int(revision),
                copy.deepcopy(profile),
            )
            self._teammate_projection_wake.set()

    def _teammate_projection_loop(self) -> None:
        set_current_thread_low_priority()
        while not self._teammate_projection_stop.is_set():
            self._teammate_projection_wake.wait(0.5)
            self._teammate_projection_wake.clear()
            while not self._teammate_projection_stop.is_set():
                lock = self._teammate_projection_worker_lock
                with lock:
                    pending = self._teammate_projection_pending
                    self._teammate_projection_pending = None
                    if pending is None:
                        self._teammate_projection_active_token = ""
                        break
                    token, revision, profile = pending
                    self._teammate_projection_active_token = token
                started = time.perf_counter()
                try:
                    result = self._calculate_teammate_info_result(
                        profile, revision
                    )
                except Exception as exc:
                    result = self._teammate_identity_result(
                        profile,
                        revision,
                        body_decoded=True,
                        message=f"总面板后台计算失败：{type(exc).__name__}: {exc}",
                        panel_state="error",
                    )
                    result["presentationRevision"] = max(0, revision) * 2 + 1
                result["projectionMs"] = round(
                    (time.perf_counter() - started) * 1000.0, 3
                )
                with lock:
                    self._teammate_projection_active_token = ""
                    current_revision = int(as_number(getattr(
                        self, "_teammate_info_cache_revision", -1
                    )))
                    current = getattr(self, "_teammate_info_cache", None)
                    current_key = (
                        norm(current.get("inspectionKey"))
                        if isinstance(current, dict)
                        else ""
                    )
                    result_key = norm(result.get("inspectionKey"))
                    if current_revision == revision and current_key == result_key:
                        self._teammate_info_cache = copy.deepcopy(result)
                    # Force the normal live page to pick up the now-cached
                    # compact panel without rerunning the calculator.
                    self._teammate_profiles_cache_revision = -1
                if self._teammate_projection_pending is not None:
                    self._teammate_projection_wake.set()

    def _shutdown_teammate_projection_worker(self) -> None:
        stop = getattr(self, "_teammate_projection_stop", None)
        wake = getattr(self, "_teammate_projection_wake", None)
        thread = getattr(self, "_teammate_projection_worker", None)
        if stop is not None:
            stop.set()
        if wake is not None:
            wake.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def teammate_info_status(self) -> dict[str, Any]:
        """Return the latest inspected teammate as one compact panel.

        The raw profile remains in the Python process.  The webview receives
        only the six user-requested panel values, which also keeps polling
        inexpensive and avoids exposing the equipment archive to this overlay.
        """

        projection_lock = self._ensure_teammate_projection_worker()
        with projection_lock:
            cached_revision = int(
                as_number(getattr(self, "_teammate_info_cache_revision", -1))
            )
            cached = copy.deepcopy(getattr(self, "_teammate_info_cache", None))
        provider = self.live_capture.teammate_info_status
        try:
            status = provider(
                cached_revision if isinstance(cached, dict) else None
            )
        except TypeError:
            status = provider()
        revision = int(as_number(status.get("revision")))
        if revision == cached_revision and isinstance(cached, dict):
            return cached
        profile = status.get("profile")
        if revision <= 0 or not isinstance(profile, dict):
            result = {
                "state": "listening",
                "revision": revision,
                "presentationRevision": max(0, revision) * 2,
            }
            with projection_lock:
                self._teammate_info_cache_revision = revision
                self._teammate_info_cache = copy.deepcopy(result)
            return result

        inspection_key = norm(profile.get("inspection_key")) or f"revision:{revision}"

        # A verified 03 15 request is published before the large equipment
        # response is decoded.  Do not run the full build calculator for that
        # identity-only shell: history can use the packet name directly or a
        # recent, unique profile alias. This makes the two on-demand WebViews appear immediately,
        # while a later revision fills in equipment-derived panel metrics.
        raw_equipment = profile.get("equipment")
        response_layout = norm(profile.get("response_layout"))
        if (
            response_layout == "TCP:inspect_request:pending"
            or not isinstance(raw_equipment, (list, tuple))
            or not raw_equipment
        ):
            result = self._teammate_identity_result(
                profile,
                revision,
                body_decoded=False,
                panel_state="waiting_body",
                message=(
                    "已收到查看信息请求，装备正文解析中"
                    if response_layout == "TCP:inspect_request:pending"
                    else "查看信息正文暂时无法解密，仍可按档案 ID 查询既往评分"
                ),
            )
            with projection_lock:
                self._teammate_info_cache_revision = revision
                self._teammate_info_cache = copy.deepcopy(result)
            return result

        # The encrypted body is already available. Publish that fact now and
        # calculate the simulator panel on one coalescing background worker.
        result = self._teammate_identity_result(
            profile,
            revision,
            body_decoded=True,
            panel_state="calculating",
            message="装备正文已收到，正在后台计算模拟面板",
        )
        with projection_lock:
            self._teammate_info_cache_revision = revision
            self._teammate_info_cache = copy.deepcopy(result)
        self._schedule_teammate_projection(profile, revision)
        return result
    def _teammate_profiles_payload(self) -> dict[str, Any]:
        """Project disk-free teammate inspections into the normal UI model."""

        provider = getattr(self.live_capture, "teammate_profiles_status", None)
        if not callable(provider):
            return {
                "revision": 0,
                "player_profiles": [],
                "profile_panels": {},
            }
        cached_revision = int(
            as_number(getattr(self, "_teammate_profiles_cache_revision", -1))
        )
        cached = getattr(self, "_teammate_profiles_cache", None)
        try:
            status = provider(
                cached_revision if isinstance(cached, dict) else None
            )
        except TypeError:
            status = provider()
        revision = int(as_number(status.get("revision")))
        if revision == cached_revision and isinstance(cached, dict):
            return copy.deepcopy(cached)
        profiles = status.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
        resolved_profiles: list[dict[str, Any]] = []
        panels: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            raw_equipment = profile.get("equipment")
            if not isinstance(raw_equipment, (list, tuple)) or not raw_equipment:
                resolved_profiles.append(copy.deepcopy(profile))
                continue
            cached_projection = self._teammate_profile_projection_cached(profile)
            if cached_projection is None:
                resolved_profiles.append(copy.deepcopy(profile))
                self._schedule_teammate_projection(profile, revision)
                continue
            resolved, panel = cached_projection
            resolved_profiles.append(resolved)
            target_id = norm(resolved.get("target_id"))
            if target_id and panel:
                panels[target_id] = panel
        result = {
            "revision": revision,
            "player_profiles": copy.deepcopy(resolved_profiles),
            "profile_panels": copy.deepcopy(panels),
        }
        self._teammate_profiles_cache_revision = revision
        self._teammate_profiles_cache = copy.deepcopy(result)
        return result
    def _overlay_job_profile(self, job: str) -> dict[str, list[str]]:
        tables = getattr(getattr(self, "store", None), "tables", {})
        row = next(
            (
                item
                for item in tables.get("effective", [])
                if norm(item.get("职业")) == norm(job)
            ),
            {},
        )
        attack_text = norm(row.get("攻击1"))
        attack_types = (
            ["physical", "magic"]
            if attack_text == "双攻"
            else (["magic"] if attack_text == "魔攻" else ["physical"])
        )
        element_text = " ".join(
            norm(row.get(key)) for key in ("属性1", "属性2") if norm(row.get(key))
        )
        element_names = (("火", "fire"), ("水", "water"), ("光", "light"), ("暗", "dark"))
        elements = [value for label, value in element_names if label in element_text]
        mixed_elements = {
            "冰灵": ["water", "fire"],
            "烈": ["dark", "fire"],
            "暗": ["dark", "light"],
            "曜": ["light", "dark"],
        }
        mixed_attacks = {"圣骑士", "十字军"}
        if job in mixed_elements:
            elements = mixed_elements[job]
        if job in mixed_attacks:
            attack_types = ["physical", "magic"]
        return {"attack_types": attack_types, "elements": elements}
    @staticmethod
    def _overlay_row_active(row: dict[str, Any], second: float) -> bool:
        return any(
            isinstance(interval, (list, tuple))
            and len(interval) >= 2
            and as_number(interval[0]) <= second < as_number(interval[1])
            for interval in row.get("intervals", [])
        )
    @classmethod
    def _overlay_effect_summary(
        cls, rows: list[dict[str, Any]], second: float
    ) -> dict[str, float]:
        return dict(cls._overlay_effect_resolution(rows, second).values)

    @staticmethod
    def _overlay_effect_resolution(
        rows: list[dict[str, Any]], second: float
    ) -> Any:
        inputs: list[EffectInput] = []
        for row_index, row in enumerate(rows):
            key = norm(row.get("key"))
            value = max(0.0, as_number(row.get("value")))
            if not key or value <= 0.0:
                continue
            intervals = row.get("intervals", [])
            for interval_index, interval in enumerate(intervals):
                if not isinstance(interval, (list, tuple)) or len(interval) < 2:
                    continue
                start = as_number(interval[0])
                end = as_number(interval[1])
                inputs.append(
                    EffectInput(
                        key=key,
                        value=value,
                        skill_id=norm(row.get("skill_id")),
                        skill_name=norm(row.get("skill_name")),
                        job=norm(row.get("job")),
                        source_id=norm(row.get("source_id")),
                        target_id=norm(row.get("target_id")),
                        instance_id=f"{row_index}:{interval_index}",
                        start=start,
                        end=end,
                        stack_group=norm(row.get("stack_group")),
                        stackable=bool(row.get("stackable")),
                        data_source=norm(row.get("data_source"))
                        or norm(row.get("value_source")),
                        metadata={"row_index": row_index},
                    )
                )
        return EFFECT_STACKING_ENGINE.resolve_active(inputs, second)

    @classmethod
    def _overlay_effect_contributors(
        cls,
        rows: list[dict[str, Any]],
        second: float,
        *,
        category: str,
        mode: str,
    ) -> list[dict[str, Any]]:
        """Return the active skills that actually contribute to one total.

        The compact overlay used to publish only the post-stacking total.  Keep
        the exact same replacement groups here so an expanded row never lists
        a weaker, currently suppressed skill as part of that total. Aggregate
        caps belong only to the summary row: each concrete skill keeps its
        packet/catalog effect value instead of being proportionally reduced.
        """

        if category == "damage":
            desired_keys = {
                "增伤%",
                "物理增伤%" if mode == "physical" else "魔法增伤%",
            }
        elif category == "resistance":
            element = {
                "fire": "火",
                "water": "水",
                "light": "光",
                "dark": "暗",
            }.get(mode, "")
            if not element:
                return []
            desired_keys = {"减全抗%", f"减{element}抗%"}
        elif category == "critical":
            desired_keys = {"减致命抵抗%"}
        elif category == "defense":
            desired_keys = {
                "减防御%",
                "减物理防御%" if mode == "physical" else "减魔法防御%",
            }
        elif category == "freeze":
            desired_keys = {"冰冻层"}
        else:
            return []

        resolution = cls._overlay_effect_resolution(rows, second)
        contributors: list[dict[str, Any]] = []
        for contribution in resolution.effective:
            if contribution.rule.key not in desired_keys:
                continue
            effect = contribution.effect
            row_index = int(effect.metadata.get("row_index", -1))
            if not 0 <= row_index < len(rows):
                continue
            row = rows[row_index]
            raw_value = max(0.0, effect.value)
            start, end = effect.start, effect.end
            skill_name = norm(row.get("skill_name")) or norm(row.get("label"))
            contributors.append(
                {
                    "key": "|".join(
                        filter(
                            None,
                            (
                                norm(row.get("target_id")),
                                category,
                                mode,
                                norm(row.get("source_id")),
                                _display_skill_key(skill_name or row.get("skill_id")),
                                norm(row.get("key")),
                            ),
                        )
                    ),
                    "skill_id": norm(row.get("skill_id")),
                    "skill_name": skill_name or "待识别技能",
                    "source_id": norm(row.get("source_id")),
                    "source_name": norm(row.get("source_name")),
                    "source_job": norm(row.get("job")),
                    "effect_key": norm(row.get("key")),
                    "raw_value": raw_value,
                    "current_value": raw_value,
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": max(0.1, end - start),
                    "remaining_seconds": max(0.0, end - second),
                    "value_source": norm(row.get("value_source")),
                    "duration_source": norm(row.get("duration_source")),
                    "data_source": norm(row.get("data_source")) or (
                        "抓包数据"
                        if norm(row.get("value_source")) == "packet"
                        and norm(row.get("duration_source")) == "packet"
                        else "抓包优先 / 预设兜底"
                    ),
                }
            )
        contributors.sort(
            key=lambda row: (
                -as_number(row.get("current_value")),
                as_number(row.get("end_seconds")),
                norm(row.get("skill_name")),
                norm(row.get("source_id")),
            )
        )
        return contributors

    @classmethod
    def _overlay_debuff_rows(
        cls,
        sources: list[dict[str, Any]],
        elapsed: float,
        damage_targets: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rows = [row for row in sources if isinstance(row, dict)]
        active = [row for row in rows if cls._overlay_row_active(row, elapsed)]

        # During transitions two entities can briefly retain statuses. Status
        # refreshes are not target-selection evidence: a long/open status on
        # an old entity must not steal the overlay from the Boss the party is
        # actually hitting.
        def target_score(target_id: str) -> tuple[float, int]:
            target_rows = [row for row in active if norm(row.get("target_id")) == target_id]
            latest = max(
                (
                    as_number(interval[0])
                    for row in target_rows
                    for interval in row.get("intervals", [])
                    if isinstance(interval, (list, tuple)) and len(interval) >= 2
                    and as_number(interval[0]) <= elapsed < as_number(interval[1])
                ),
                default=0.0,
            )
            return latest, len(target_rows)

        # Live snapshots pass all recognized runtime Boss targets. Select only
        # by their latest confirmed damage timestamp and keep that target
        # sticky through a stop/mechanism interval. Buffs are built from the
        # independent player-buff ledger above and are deliberately unaffected
        # by this Boss choice.
        damaged_targets = [
            row
            for row in (damage_targets or [])
            if isinstance(row, dict)
            and norm(row.get("target_id"))
            and as_number(row.get("last_ms")) > 0.0
        ]
        if damaged_targets:
            selected_target = max(
                damaged_targets,
                key=lambda row: (
                    as_number(row.get("last_ms")),
                    as_number(row.get("first_ms")),
                    target_score(norm(row.get("target_id"))),
                ),
            )
            target_id = norm(selected_target.get("target_id"))
        elif damage_targets is not None:
            # A Boss status arriving before the first confirmed hit is not
            # enough to know which target the player is fighting.
            target_id = ""
        else:
            # Backward-compatible fallback for old cached snapshots which did
            # not publish runtime target rows.
            target_id = max(
                {
                    norm(row.get("target_id"))
                    for row in active
                    if norm(row.get("target_id"))
                },
                key=target_score,
                default="",
            )
        if not target_id:
            return []
        target_rows = [row for row in rows if norm(row.get("target_id")) == target_id]
        boundaries = sorted(
            {
                as_number(interval[1])
                for row in target_rows
                for interval in row.get("intervals", [])
                if isinstance(interval, (list, tuple)) and len(interval) >= 2
                and as_number(interval[0]) <= elapsed < as_number(interval[1])
                and as_number(interval[1]) > elapsed
            }
        )
        current = cls._overlay_effect_summary(target_rows, elapsed)
        definitions = (
            ("damage", "physical", "物理增伤", "物理增伤合计%"),
            ("damage", "magic", "魔法增伤", "魔法增伤合计%"),
            ("resistance", "fire", "减火抗", "减火抗%"),
            ("resistance", "water", "减水抗", "减水抗%"),
            ("resistance", "light", "减光抗", "减光抗%"),
            ("resistance", "dark", "减暗抗", "减暗抗%"),
            ("critical", "critical", "减致命抗", "减致命抵抗%"),
            ("defense", "physical", "减物防", "减物理防御%"),
            ("defense", "magic", "减魔防", "减魔法防御%"),
            ("freeze", "freeze", "叠冰", "冰冻层"),
        )
        result: list[dict[str, Any]] = []
        for category, mode, label, summary_key in definitions:
            value = current.get(summary_key, 0.0)
            if value <= 0:
                continue
            change_at = 0.0
            next_value = value
            for boundary in boundaries:
                candidate = cls._overlay_effect_summary(target_rows, boundary + 0.001).get(
                    summary_key, 0.0
                )
                if abs(candidate - value) > 1e-9:
                    change_at = boundary
                    next_value = candidate
                    break
            applicable_keys = {
                summary_key,
                "增伤%" if category == "damage" else "",
                f"{mode == 'physical' and '物理' or '魔法'}增伤%" if category == "damage" else "",
                "减全抗%" if category == "resistance" else "",
                "减防御%" if category == "defense" else "",
                f"减{mode == 'physical' and '物理' or '魔法'}防御%" if category == "defense" else "",
            }
            active_source_rows = [
                row
                for row in target_rows
                if cls._overlay_row_active(row, elapsed)
                and norm(row.get("key")) in applicable_keys
            ]
            active_starts = [
                as_number(interval[0])
                for row in active_source_rows
                for interval in row.get("intervals", [])
                if isinstance(interval, (list, tuple)) and len(interval) >= 2
                and as_number(interval[0]) <= elapsed < as_number(interval[1])
            ]
            start = max(active_starts, default=elapsed)
            contributors = cls._overlay_effect_contributors(
                target_rows,
                elapsed,
                category=category,
                mode=mode,
            )
            value_sources = {
                norm(row.get("value_source"))
                for row in active_source_rows
                if norm(row.get("value_source"))
            }
            duration_sources = {
                norm(row.get("duration_source"))
                for row in active_source_rows
                if norm(row.get("duration_source"))
            }
            value_source = (
                "packet" if value_sources == {"packet"}
                else "preset_fallback" if value_sources
                else "unavailable"
            )
            duration_source = (
                "packet" if duration_sources == {"packet"}
                else "preset_fallback" if duration_sources
                else "unavailable"
            )
            result.append(
                {
                    "key": f"{target_id}|{category}|{mode}",
                    "category": category,
                    "mode": mode,
                    "label": label,
                    "target_id": target_id,
                    "current_value": value,
                    "next_value": next_value,
                    "start_seconds": start,
                    "end_seconds": change_at,
                    "duration_seconds": max(0.1, change_at - start) if change_at else 0.0,
                    "remaining_seconds": max(0.0, change_at - elapsed) if change_at else 0.0,
                    "contributors": contributors,
                    "value_source": value_source,
                    "duration_source": duration_source,
                    "data_source": (
                        "抓包数据"
                        if value_source == "packet" and duration_source == "packet"
                        else "抓包优先 / 预设兜底"
                    ),
                }
            )
        return result
    def _skill_cast_payloads(
        self,
        snapshot: CombatSnapshot,
    ) -> list[dict[str, Any]]:
        platform = event_platform_for(snapshot)
        resolver = LiveCastTimingResolver(
            self.store,
            player_buff_clear_times_by_target(
                platform.player_buff_clear_events
            ),
        )
        rows: list[dict[str, Any]] = []
        for event in platform.skill_cast_events:
            timing = resolver.resolve(event, event.timestamp_ms / 1000.0)
            rows.append({
                **_json_safe(event),
                "cast_time": timing.cast_time,
                "action_speed_percent": timing.action_speed_percent,
                "end_timestamp_ms": round(timing.end_seconds * 1000),
            })
        return rows
    def start_capture(self, duration: float = 0.0) -> dict[str, Any]:
        return self.live_capture.start(duration)
    def set_capture_auto(self, enabled: bool) -> dict[str, Any]:
        return self.live_capture.set_auto_enabled(enabled)
    def capture_toggles(self) -> dict[str, Any]:
        from ..live.capture_toggles import CAPTURE_TOGGLE_LABELS
        return {
            "toggles": self.live_capture.get_toggles(),
            "labels": CAPTURE_TOGGLE_LABELS,
        }
    def set_capture_toggles(self, payload: dict[str, Any]) -> dict[str, Any]:
        from ..live.capture_toggles import CAPTURE_TOGGLE_LABELS
        raw = payload.get("toggles", {}) if isinstance(payload, dict) else {}
        return {
            "toggles": self.live_capture.set_toggles(raw),
            "labels": CAPTURE_TOGGLE_LABELS,
        }
    def stop_capture(self) -> dict[str, Any]:
        # Manual Stop must acknowledge immediately. Npcap closure, decoder
        # draining and the exact final snapshot continue on the capture thread;
        # the page's existing one-second status poll receives the completed
        # snapshot as soon as it is ready.
        return self.live_capture.request_stop()
_SCENE_NEST_KINDS = {"nest", "dungeon", "raid"}
_SCENE_HUB_KINDS = {"hub", "hub_return", "town", "lobby", "garden", "village"}


def overlay_scene_identity(state: dict[str, Any]) -> dict[str, str]:
    """悬浮窗第一行的场景身份，城镇也算「识别到了」。

    之前这一行只读 ``state["nestName"]``，而它仅在**战斗地图**上才会赋值。
    于是「在城里点开始统计」或「抓包晚于切图」时，即使我们明明已经从包里
    读到了 ``神圣天堂``，界面也只能写「未识别」——这正是用户看到的现象。

    现在退回地图历史里最新的一条，并用 ``sceneKind`` 区分副本/城镇：
    调用方据此决定显示 ``(已识别)``、``(不播报)`` 还是 ``(非副本)``。
    """

    nest_name = str(state.get("nestName") or "").strip()
    history = [
        row
        for row in (state.get("nestMapHistory") or ())
        if isinstance(row, dict)
    ]
    latest = max(
        history,
        key=lambda row: float(row.get("observed_at") or 0.0),
        default=None,
    )
    if latest is None:
        # 没有地图证据时只能相信精确副本身份（模板反推等）。
        return {
            "sceneName": nest_name,
            "sceneKind": "nest" if nest_name else "unknown",
        }
    content_kind = str(latest.get("content_kind") or "").strip().casefold()
    try:
        map_type = int(latest.get("map_type") or 0)
    except (TypeError, ValueError):
        map_type = 0
    if content_kind in _SCENE_NEST_KINDS or map_type == 3:
        kind = "nest"
    elif content_kind in _SCENE_HUB_KINDS or map_type == 2:
        kind = "hub"
    else:
        kind = "map"
    scene_name = str(
        latest.get("nest_name") or latest.get("map_name") or ""
    ).strip()
    if kind == "nest" and nest_name:
        # 同一张副本可能有多条历史行，精确副本身份更可靠。
        scene_name = nest_name
    return {"sceneName": scene_name or nest_name, "sceneKind": kind}


_PART_SUFFIX = " · "


def _scene_text(value: object) -> str:
    """本模块的 norm 是运行时由宿主注入的；独立函数要能脱离宿主单测。"""

    return str(value or "").strip()


def _template_id_from_target_id(value: str) -> int:
    suffix = str(value or "").rsplit("@", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


@lru_cache(maxsize=1)
def curated_boss_template_ids() -> frozenset[int]:
    """档位血表里人工核对过的 Boss 模板 id（`boss_hp_profiles.json`）。

    模板表自身并不完整：红龙佩达的三个阶段 ``503681/503682/503683`` 标的是
    ``boss=false``（只有 ``503696`` 是 true），沙虫/沙扎卡德的非末阶段同理。
    只信模板标记，就会把「队伍正在集火的红龙」当成杂兵丢掉，Boss 行随即退回
    上一关的名字——这正是「Boss 一直显示第一关」的直接原因之一。
    """

    ids: set[int] = set()
    try:
        from ..live.boss_hp import load_fixed_boss_hp_profiles

        for profile in load_fixed_boss_hp_profiles():
            ids.update(int(value) for value in profile.template_ids)
    except Exception:
        return frozenset()
    return frozenset(ids)


@lru_cache(maxsize=4096)
def target_is_boss_template(target_id: str) -> bool | None:
    """用怪物模板表 + 档位血表判断一个目标是不是 Boss。

    ``True`` = Boss（档位血表收录或模板标记 boss）；``False`` = 明确是杂兵；
    ``None`` = 两边都没收录（此时不能排除，按 Boss 处理更安全）。

    背景：``BossHpLedger.ingest_health`` 对**任何**带 HP 的事件都会新建轨迹，
    所以杂兵只要发了 HP 包也会进 ``bossHealth``。悬浮窗第二行必须再筛一次，
    否则「Boss:」会显示成小妖精/木桩这种名字。
    """

    template_id = _template_id_from_target_id(target_id)
    if template_id <= 0:
        return None
    if template_id in curated_boss_template_ids():
        return True
    try:
        from ..live.boss_recognizer import monster_template_catalog

        template = monster_template_catalog().get(template_id)
    except Exception:
        return None
    if not isinstance(template, dict):
        return None
    return bool(template.get("boss"))


def _identity_clock(row: dict[str, Any]) -> float:
    """把一行里的时间戳统一成秒；取不到就返回 0。

    血量行用 ``updatedSeconds``，伤害目标行用毫秒 ``last_ms``，动作证据用
    ``timestamp_ms``。回退时必须按时间挑**最新**的那个，否则永远命中累计列表
    里排第一的第一关 Boss。
    """

    for key, scale in (
        ("updatedSeconds", 1.0),
        ("lastSeconds", 1.0),
        ("last_ms", 0.001),
        ("timestamp_ms", 0.001),
        ("timestamp", 1.0),
    ):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw) * scale)
        except (TypeError, ValueError):
            continue
    return 0.0


# 分边副本：左右两边各一只 Boss，两个队伍各打各的。
SPLIT_BOSS_NEST_KEYS = frozenset(
    {
        "black_dragon_hardcore",
        "sand_dragon_hardcore",
        # 梦魇海龙：前半段是「审判者天启 / 狮蝎领主克雷姆」左右各一队。
        # 实测 24 个 20s 采样点全部互不重叠（4 人打天启、7-8 人打狮蝎），
        # 所以按本机攻击目标定当前组是安全的。
        "nightmare_sea_dragon",
    }
)
SPLIT_BOSS_NEST_NAMES = ("黑龙硬核", "沙龙硬核", "沙漠龙硬核", "梦魇海龙")
# 「同时在场」的判定窗口：血量行 updatedSeconds 与最新一行相差在窗口内，才算这一场
# 里另一侧的 Boss；超出窗口的是上一关残留，不能拿来当分组依据。
SPLIT_ACTIVE_WINDOW_SECONDS = 20.0


def split_active_boss_templates(
    health_rows: Any, window: float = SPLIT_ACTIVE_WINDOW_SECONDS
) -> set[int]:
    """血量行里「最近还在被打」的 Boss 模板集合。

    分边副本里两只 Boss 的血量行会同时刷新；上一关打完的 Boss 只是残留在
    ``bossHealth`` 里不再更新，靠这个窗口把它们排除掉。
    """

    pairs: list[tuple[int, float]] = []
    for row in health_rows or ():
        if not isinstance(row, dict) or row.get("partId"):
            continue
        target_id = _scene_text(row.get("targetId") or row.get("target_id"))
        template = _template_id_from_target_id(target_id)
        if template <= 0:
            continue
        pairs.append((template, _identity_clock(row)))
    if not pairs:
        return set()
    newest = max(clock for _, clock in pairs)
    return {template for template, clock in pairs if clock >= newest - window}


def split_peer_boss_templates(
    snapshot: dict[str, Any],
    current_template: int,
    self_source: str = "",
    *,
    window: float = SPLIT_ACTIVE_WINDOW_SECONDS,
    nest_key: str = "",
) -> list[int]:
    """同一时间场上「另一侧」的 Boss 模板。

    优先用血量行（只有窗口内还在更新的才算同场）。轻量版（倒计时工具）不把
    Boss 血量事件交给聚合器，``bossHealth`` 常常为空，此时退化为
    「队友最近还在打、且不是我这一只的 Boss」——两边各打各的，队友的目标
    自然指向另一侧，用同一个时间窗口把上一关的残留排掉。
    """

    # 只认「这个副本登记在册的 Boss」。本机去打暗影/召唤物时（梦魇海龙终局
    # 4237025 就是这种），它们既不该当「我在打的那一侧」，也不该成为对侧，
    # 否则真正的 Boss 的冰捆/拉冰会被整段过滤掉。
    from .nest_inference import nest_boss_templates, template_nest_index

    registered = nest_boss_templates(nest_key) if nest_key else frozenset()
    if not registered:
        index = template_nest_index()
        entry = index.get(int(current_template)) or {}
        sibling_nest = str(entry.get("nestKey") or "")
        if sibling_nest:
            registered = nest_boss_templates(sibling_nest)
    if not registered:
        # 拿不到副本身份时至少要求「是档位血表登记过的 Boss 模板」，
        # 这样暗影/召唤物（4237025）这类既不是 Boss 也不会被当成对侧。
        registered = curated_boss_template_ids()
    if registered and int(current_template) not in registered:
        return []

    active = split_active_boss_templates(snapshot.get("bossHealth"), window)
    if registered:
        active = {template for template in active if template in registered}
    peers: set[int] = set()
    if active:
        peers = set(active - {current_template})
    else:
        attacks = current_attack_targets(snapshot)
        if not isinstance(attacks, dict):
            return []
        me = _scene_text(self_source)
        entries: list[tuple[str, str, float]] = []
        for source, value in attacks.items():
            if isinstance(value, dict):
                target_id = _scene_text(value.get("targetId") or value.get("target_id"))
                clock = float(value.get("timestampMs") or 0)
            else:
                target_id = _scene_text(value)
                clock = 0.0
            if target_id:
                entries.append((_scene_text(source), target_id, clock))
        if not entries:
            return []
        # 「现在」要按所有人的最新时间算（包括自己），否则把自己剔掉之后窗口会失真。
        newest = max(clock for _, _, clock in entries)
        for source, target_id, clock in entries:
            if source == me:
                continue
            if clock < newest - window * 1000:
                continue
            # 队友打杂兵/召唤物时不能算成「另一侧」。
            if target_is_boss_template(target_id) is not True:
                continue
            template = _template_id_from_target_id(target_id)
            if template > 0 and template != current_template:
                peers.add(template)
    if not peers:
        return []
    # 闸门 1：**本机自己**此刻还在打对侧 —— 这是同一关的双 Boss（沙龙的
    # 女妖皇后 + 消亡领主），过滤掉另一只就是漏自己吃到的技能，宁可不过滤。
    #
    # 用 4 秒的「还在打」表，不用 10 秒的累计表：梦魇海龙里 AOE 每隔几秒蹭到
    # 对侧一下，10 秒表必然命中，会把真正分开的两组一起否掉（实测 peers 恒为空）。
    me = _scene_text(self_source)
    recent = snapshot.get("recentAttackers")
    by_template = snapshot.get("attackTargetsByTemplate")
    recent = recent if isinstance(recent, dict) else by_template
    if me and isinstance(recent, dict):
        for template in peers:
            if me in set(recent.get(str(template)) or ()):
                return []
    # 闸门 2：场上必须真的存在**两组人各打一只**。一个玩家只有一个当前目标，
    # 所以同一人不会同时落在两组 —— 「此刻重叠」就等于「同一批人两边打」。
    #
    # 这条同时排掉了「同一关的同伴」：梦魇海龙 P2 里地狱犬卡卡里和空中的海龙
    # 同时在场，但空中那只谁也打不到，除了我这一组没有第二个攻击者。之前只
    # 要求「血量行里有另一只」，于是空中海龙被当成「另一侧」，它刚放出来的
    # 冰眼/龙息被整段过滤掉，表现就是「要放过一次才出来」。
    groups = _current_target_groups(snapshot, window)
    if len(groups) <= 1:
        return []
    mine_players = groups.get(int(current_template), set())
    peers = {template for template in peers if groups.get(int(template))}
    if mine_players:
        peers = {
            template
            for template in peers
            if not (groups.get(int(template), set()) & mine_players)
        }
    return sorted(peers)


def _current_target_groups(
    snapshot: Any, window: float = SPLIT_ACTIVE_WINDOW_SECONDS
) -> dict[int, set[str]]:
    """此刻每个 Boss 模板下「各自正在打它的玩家」。

    每个玩家只保留最后一次攻击目标，所以同一人不会同时出现在两组里 ——
    这正是分边与「同一批人两边打」的区别。
    """

    if not isinstance(snapshot, dict):
        return {}
    attacks = current_attack_targets(snapshot)
    if not isinstance(attacks, dict):
        return {}
    entries: list[tuple[str, str, float]] = []
    for source, value in attacks.items():
        if isinstance(value, dict):
            target_id = _scene_text(value.get("targetId") or value.get("target_id"))
            clock = float(value.get("timestampMs") or 0)
        else:
            target_id = _scene_text(value)
            clock = 0.0
        if target_id:
            entries.append((_scene_text(source), target_id, clock))
    if not entries:
        return {}
    newest = max(clock for _, _, clock in entries)
    groups: dict[int, set[str]] = {}
    for source, target_id, clock in entries:
        if clock < newest - window * 1000:
            continue
        template = _template_id_from_target_id(target_id)
        if template > 0:
            groups.setdefault(template, set()).add(source)
    return groups


def filter_current_group_rows(
    rows: Any, hint: Any
) -> list[dict[str, Any]]:
    """只保留「当前分组」的事件：剔除同时在场、但属于另一侧 Boss 的技能。

    ``hint`` 为 ``None``／没有 current／没有别的同时在场 Boss 时**原样返回**——
    识别不出来时宁可都播，也不能把技能吞掉。
    """

    items = [row for row in (rows or ()) if isinstance(row, dict)]
    if not isinstance(hint, dict):
        return items
    current = int(hint.get("current") or 0)
    others = {int(value) for value in (hint.get("others") or ()) if int(value or 0) > 0}
    if current <= 0 or not others:
        return items
    return [row for row in items if int(row.get("templateId") or 0) not in others]


def nest_uses_split_boss(nest_key: Any = "", nest_label: Any = "") -> bool:
    """这个副本是不是「左右分边、两只 Boss 同时在场」的打法。

    只有这类副本（黑龙硬核、沙龙硬核）的「Boss:」一行要按**本机角色正在攻击
    的目标**决定。其余副本仍然按「最近活跃」取，避免自己打小怪、或者自己停手
    时把队伍正在推进的那只 Boss 换掉。
    """

    key = _scene_text(nest_key).strip().lower()
    if key in SPLIT_BOSS_NEST_KEYS:
        return True
    label = _scene_text(nest_label).replace(" ", "")
    return any(name in label for name in SPLIT_BOSS_NEST_NAMES)


def current_attack_targets(snapshot: dict[str, Any]) -> dict[str, Any]:
    """「此刻谁在打谁」——带时效的攻击目标表。

    聚合器同时给出两张表：``lastAttackTargets`` 是永久表（名字反查、历史诊断
    要用），``recentAttackTargets`` 只保留最近 15 秒的目标。所有业务判定
    （Boss 行身份、分边分组、队友目标血条）都必须用后者：Boss 离场后没人再碰
    它，永久表会永远停在那一只，导致 Boss 行卡在上一关、分边把真正在打的
    Boss 当成「另一侧」过滤掉。
    """

    if not isinstance(snapshot, dict):
        return {}
    fresh = snapshot.get("recentAttackTargets")
    if isinstance(fresh, dict):
        return fresh
    return snapshot.get("lastAttackTargets") or {}


# 「本机此刻主要在打谁」的加权窗口。与聚合器的 _ATTACK_SCORE_WINDOW_MS 对齐：
# 窗口内伤害占比最高的那只才算主目标，一次 AOE 溅射改不掉它。
FOCUS_TARGET_WINDOW_MS = 15_000.0


def _entry_target_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return _scene_text(entry.get("targetId") or entry.get("target_id"))
    return _scene_text(entry)


def focus_target_of(
    snapshot: Any,
    self_source: Any,
    registered: Any,
) -> tuple[int, str]:
    """本机「此刻主要在打」的登记 Boss：模板号 + 目标 ID。

    只用**本机自己**的证据，两级：

    0. ``attackScores`` —— 15 秒窗口内伤害占比最高者。AOE 扫过一群怪时最后
       命中的常是杂兵或对侧 Boss，按占比取最大才不会被一次溅射改掉主目标。
    1. 本机最后命中且属于登记 Boss（轻量版没有伤害事件，只剩这一级）。

    刻意**不做**「队友在打谁」「血量行最活跃」这类回落：分边副本里我在打 A、
    队友在打 B，按票数回落有一半概率把「我在打谁」猜成对侧，从而把真正该播
    的那一侧整段吞掉。选不出来就返回 ``(0, "")``，让调用方放弃过滤 ——
    宁可多播，不可漏播。
    """

    if not isinstance(snapshot, dict):
        return 0, ""
    allowed = {int(value) for value in (registered or ()) if int(value or 0) > 0}
    if not allowed:
        return 0, ""
    me = _scene_text(self_source)
    ended = {
        _scene_text(value)
        for value in (snapshot.get("endedBossIds") or ())
        if _scene_text(value)
    }

    def accept(target_id: Any) -> int:
        target = _scene_text(target_id)
        if not target or target in ended:
            return 0
        template = _template_id_from_target_id(target)
        return template if template in allowed else 0

    attacks = current_attack_targets(snapshot)
    attacks = attacks if isinstance(attacks, dict) else {}

    # 0. 窗口内伤害占比
    scores = snapshot.get("attackScores")
    if isinstance(scores, dict) and me:
        mine = scores.get(me)
        if isinstance(mine, dict):
            best_target = ""
            best_weight = 0.0
            last_hit = _entry_target_id(attacks.get(me))
            for target_id, amount in mine.items():
                template = accept(target_id)
                if not template:
                    continue
                weight = float(amount or 0)
                if weight <= 0:
                    continue
                target = _scene_text(target_id)
                # 同权重时让「最后命中的那个」胜出：占比接近说明两边都在打，
                # 此时按「刚刚打的那只」更贴近玩家此刻的意图。
                if weight > best_weight or (
                    weight == best_weight and target == last_hit
                ):
                    best_target = target
                    best_weight = weight
            if best_target:
                return _template_id_from_target_id(best_target), best_target

    # 1. 本机最后命中
    template = accept(_entry_target_id(attacks.get(me)))
    if template:
        return template, _entry_target_id(attacks.get(me))
    return 0, ""


def focus_boss_hint(
    snapshot: Any,
    *,
    nest_key: Any = "",
    nest_label: Any = "",
    self_source: Any = "",
    live_bosses: Any = (),
) -> dict[str, Any] | None:
    """播报白名单：只播「本机正在打的 Boss」+「副本声明的场景 Boss」。

    两条链路，顺序不能反：

    **B · 目标切换（优先）**：本机 15 秒伤害占比指向哪只登记 Boss，就聚焦哪只。
    分边副本换边、单 Boss 副本改打另一关，都靠它立刻跟上。

    **A · 进本绑定（兜底，进本即生效）**：B 选不出来（还没出手 / 刚进本 / 停手）
    时，用出场包绑到的「本关在场 Boss」。非分边副本整组一起聚焦（P1/P2 同名、
    本体 + 分身都算）；分边副本两只同时在场，返回 ``None`` 也就是不过滤
    （宁可多播，不可漏播），等玩家真的开打再由 B 收窄到他那一侧。

    返回 ``None`` 表示**不要过滤**：副本没识别、或进场 Boss 不唯一且本机没出手。
    """

    if not isinstance(snapshot, dict):
        return None
    from .nest_inference import (
        infer_nest_from_snapshot,
        nest_boss_templates,
        scene_boss_templates,
        template_nest_index,
    )

    # 调用方漏传 self_source 时从快照兜底：拿不到「我是谁」就永远选不出主目标，
    # 表现是 focus 恒为 None（不过滤），分边副本两侧都会播。
    self_source = _scene_text(self_source) or _scene_text(snapshot.get("selfSourceId"))

    key = _scene_text(nest_key)
    registered = nest_boss_templates(key)
    if not registered:
        # 场景识别给的常常是地图级 key（``map:14451``），进不了副本登记表。
        # 这时用**已经绑定到手的 Boss 模板**反推副本——比「攻击目标反推」更硬：
        # 出场包绑定的模板号是确定的，不需要本机出手。
        for row in live_bosses or ():
            if not isinstance(row, dict):
                continue
            try:
                template_id = int(row.get("templateId") or 0)
            except (TypeError, ValueError):
                continue
            entry = template_nest_index().get(template_id)
            if not entry:
                continue
            candidate = _scene_text(entry.get("nestKey"))
            candidate_registered = nest_boss_templates(candidate)
            if candidate_registered:
                key = candidate
                registered = candidate_registered
                break
    if not registered:
        inferred = infer_nest_from_snapshot(snapshot) or {}
        inferred_key = _scene_text(inferred.get("nestKey"))
        if inferred_key:
            key = inferred_key
            registered = nest_boss_templates(key)
    if not registered:
        return None

    current, target_id = focus_target_of(snapshot, self_source, registered)
    scene = {int(value) for value in scene_boss_templates(key)}
    focus = {int(current)} | scene if current else set(scene)
    if not current and not nest_uses_split_boss(key):
        # A 链路：进场就把这一关的 Boss 绑好了，不用等本机出手。
        # 「这一关」= 解码器最新阶段里的全部 Boss（P1/P2 同名、本体+分身都算），
        # 所以 focus 是一组而不是一只；current 取最近出场的那个当代表。
        ordered = [
            int(row.get("templateId") or 0)
            for row in (live_bosses or ())
            if isinstance(row, dict) and int(row.get("templateId") or 0) in registered
        ]
        if ordered:
            current = ordered[-1]
            target_id = ""
            focus = set(ordered) | scene
    if not current:
        return None

    others = sorted(int(value) for value in registered - focus)
    return {
        "current": int(current),
        "targetId": target_id,
        "focus": sorted(focus),
        "scene": sorted(scene),
        "others": others,
        "nestKey": key,
        "nestLabel": _scene_text(nest_label),
    }


def attack_target_is_current(
    snapshot: dict[str, Any],
    source_id: str,
    target_id: str,
    *,
    window: float = SPLIT_ACTIVE_WINDOW_SECONDS,
) -> bool:
    """本机最近是否还在打这个目标。

    优先看 ``attackTargetsByTemplate``（聚合器按 10 秒窗口维护的「谁在打谁」）；
    旧快照没有这个字段时退回 ``timestampMs`` 与当前战斗时钟比较。只有确认
    「还在打」才让单 Boss 副本的 Boss 行跟着目标走，否则停手后那行会一直挂着
    最后碰过的那只。
    """

    template = _scene_text(target_id).rsplit("@", 1)[-1].strip()
    by_template = snapshot.get("attackTargetsByTemplate")
    if isinstance(by_template, dict) and template:
        players = by_template.get(template)
        if players is not None:
            return _scene_text(source_id) in {_scene_text(item) for item in players}
    entry = (snapshot.get("lastAttackTargets") or {}).get(source_id)
    if not isinstance(entry, dict):
        return False
    stamp = float(entry.get("timestampMs") or 0)
    clock = float(snapshot.get("elapsed_seconds") or 0) * 1000.0
    if not stamp or not clock:
        return False
    return clock - stamp <= window * 1000.0


def overlay_boss_identity(
    snapshot: dict[str, Any],
    boss_rows: list[Any],
    *,
    split_boss: bool = False,
    live_bosses: Any = (),
) -> dict[str, Any]:
    """悬浮窗第二行的 Boss：名字 / 目标 ID / 模板 ID。

    每一级都按时间取**最新**的一行，并剔除已经结束的实例。之前按列表顺序取
    第一个，而 ``boss_runtime_targets`` / ``damage_targets`` 是累计列表
    （``overlay_snapshot`` 按 target_id 排序），第一关的食人魔永远排第一，
    于是「Boss:」从第二关开始就再也不会更新——这就是用户看到的「一直显示
    第一关的 Boss」。

    取值顺序（前两级是**本机自己的目标**，命中不了就到此为止）：

    0. ``recentAttackTargets``（15 秒）里本机的目标 —— 播报用的同一张表，
       换边立刻跟上。
    1. ``displayAttackTargets``（30 秒）里本机的目标 —— 读条、被控、跑机制
       期间仍然显示自己最后打的那只，不闪。
    2. 本机**出手过**但已经超过 30 秒：直接返回空（前端显示「未识别」）。
       这是明确的产品口径：不打怪时这一行只认自己打过的目标，**不跳到队友
       正在打的那只**。只有「本机从进本到现在一次都没出手」才会继续往下，
       覆盖刚进本、中途开统计还没动手这两种情况。

    以下四级只在「本机从未出手」时生效：

    3. 血量行主行（队伍正在打的那个目标）—— 还能区分分部位。
    4. 血量行的分部位行，按 ``" · "`` 去掉部位后缀取父名（海龙 4150061/2）。
    5. ``boss_runtime_targets`` / ``damage_targets`` —— 食人魔这一轮完全没有
       HP 事件（``bossHealth`` 为空），但它确实吃了伤害、有 ``target_name``；
       只看血量行就会误判成「未识别」。
    6. ``boss_action_events`` 的 ``source_name`` —— 最终快照里保留的动作证据。

    ``split_boss`` 保留在签名里只为兼容既有调用方：分边副本不再走单独的
    「最多人在打的那只」投票，那条路径正是「显示成队友目标」的来源。
    """

    ended = {
        str(value).strip()
        for value in (snapshot.get("endedBossIds") or ())
        if str(value or "").strip()
    }

    def build(name: str, target_id: str = "") -> dict[str, Any]:
        return {
            "name": name,
            "targetId": target_id,
            "templateId": _template_id_from_target_id(target_id),
        }

    def is_addable(target_id: str, *, strict: bool = False) -> bool:
        # 杂兵（模板 boss=False 且不在档位血表里）不能占据「Boss:」这一行。
        # 模板未收录时返回 None：血量行（队伍真的在打它）可以接受，但累计列表
        # 里必须要求「确认是 Boss」——否则某个没收录的杂兵/召唤物一旦变成最新
        # 的伤害目标，就会顶着「未识别实体 33632」这种名字霸占这一行。
        known = target_is_boss_template(target_id)
        return known is True if strict else known is not False

    def rows_of(rows: Any) -> list[dict[str, Any]]:
        return [row for row in (rows or ()) if isinstance(row, dict)]

    def newest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        picked: dict[str, Any] | None = None
        picked_clock = -1.0
        for row in rows:
            clock = _identity_clock(row)
            if clock > picked_clock:
                picked, picked_clock = row, clock
        return picked

    def candidates(
        rows: list[dict[str, Any]],
        *,
        parts: bool | None = None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in rows:
            if parts is not None and bool(row.get("partId")) != parts:
                continue
            target_id = _scene_text(row.get("targetId") or row.get("target_id"))
            if not target_id or target_id in ended:
                continue
            if not is_addable(target_id, strict=strict):
                continue
            if not _scene_text(row.get("name") or row.get("target_name")):
                continue
            selected.append(row)
        return selected

    health_rows = rows_of(snapshot.get("bossHealth"))
    evidence_rows = (
        rows_of(boss_rows)
        + health_rows
        + rows_of(snapshot.get("boss_runtime_targets"))
        + rows_of(snapshot.get("damage_targets"))
    )

    def row_target_id(row: dict[str, Any]) -> str:
        return _scene_text(row.get("targetId") or row.get("target_id"))

    def bare_id(value: Any) -> str:
        # 伤害事件里的 target_id 是 ``0x00000036``（没有 @模板），血量行是
        # ``0x00000036@503611``。两者必须按前半段对齐，否则永远匹配不上。
        return _scene_text(value).split("@", 1)[0].strip()

    def percent(row: dict[str, Any]) -> float:
        # 血量行（boss_hp.overlay_rows）用的是 percent / currentHp，别的来源可能给
        # hpPercent；这个函数在模块级，host globals（as_number）不一定绑定过，
        # 所以这里自带解析，避免依赖调用方。
        for key in ("hpPercent", "healthPercent", "percent"):
            value = row.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        try:
            current = float(row.get("currentHp"))
            maximum = float(row.get("maxHp") or 0)
        except (TypeError, ValueError):
            return -1.0
        return current / maximum * 100.0 if maximum > 0 else -1.0

    # 轻量版没有血量行，目标名字只能来自伤害事件旁路记录的 targetNames。
    target_names = (
        snapshot.get("targetNames") if isinstance(snapshot.get("targetNames"), dict) else {}
    )

    def name_for(target_id: str) -> str:
        wanted = bare_id(target_id)
        for row in evidence_rows:
            if bare_id(row_target_id(row)) != wanted:
                continue
            name = _scene_text(row.get("name") or row.get("target_name"))
            if name:
                return name
        mapped = _scene_text(
            target_names.get(target_id) or target_names.get(wanted)
        )
        if mapped:
            return mapped
        return ""

    def is_dead(target_id: str) -> bool:
        # 血量行读到 0 就是这一只已经打完；否则自己停手后「Boss:」会一直挂在
        # 那只已经死掉的怪上。
        # 只看真正的血量行：damage_targets / boss_runtime_targets 没有血量字段，
        # percent() 对它们返回 0，以前会被误判成「已死」——Boss 行就永远换不过去。
        wanted = bare_id(target_id)
        for row in rows_of(boss_rows) + health_rows:
            if bare_id(row_target_id(row)) != wanted:
                continue
            if row.get("currentHp") == 0 or percent(row) == 0:
                return True
        return False

    def ranked(target_id: str) -> dict[str, Any] | None:
        target_id = _scene_text(target_id)
        # 伤害事件给的是不带模板的 id，血量行带模板。优先换成带模板的那个，
        # 这样「Boss:」的 templateId 与分边判断都能用。
        if target_id and "@" not in target_id:
            wanted = bare_id(target_id)
            for row in evidence_rows:
                full = row_target_id(row)
                if bare_id(full) == wanted and "@" in full:
                    target_id = full
                    break
        if not target_id or target_id in ended:
            return None
        if not is_addable(target_id) or is_dead(target_id):
            return None
        name = name_for(target_id)
        if not name:
            return None
        return build(name.split(_PART_SUFFIX)[0].strip(), target_id)

    # 0) 分边副本：只显示「我自己正在打的那一只」。
    #    ``lastAttackTargets`` 是每个玩家最后一次攻击的目标；之前一律按
    #    「谁的血量行更新」挑，左右两边的人同时在打，名字就会来回跳。
    #    本机角色认不出来（或自己打的是杂兵/已经死了）时，退成「队伍里最多
    #    人在打的那一只」，同票取剩余血量高的，保证同一帧内结果确定。
    def attack_target_id(value: Any) -> str:
        # aggregator 存的是 {"targetId": ..., "timestampMs": ...}，早期/测试里
        # 也可能直接给字符串，两种都要认。
        if isinstance(value, dict):
            return _scene_text(value.get("targetId") or value.get("target_id"))
        return _scene_text(value)

    def self_source_has_attacked(snapshot_obj: Any, source: str) -> bool:
        """本机从进本到现在是否已经对任何目标出手过。

        三张表任一命中即可：``displayAttackTargets``（30s）、
        ``recentAttackTargets``（15s）、``lastAttackTargets``（永久）。
        永久表是关键——「本机打过」这件事整局都不能被遗忘，这样停手后才按
        用户预期留空，而不是跳去显示队友正在打的那只。
        """

        if not source or not isinstance(snapshot_obj, dict):
            return False
        for key in ("displayAttackTargets", "recentAttackTargets", "lastAttackTargets"):
            table = snapshot_obj.get(key)
            if isinstance(table, dict) and attack_target_id(table.get(source)):
                return True
        return False

    attack_targets = current_attack_targets(snapshot)
    self_source_id = _scene_text(snapshot.get("selfSourceId"))
    mine_id = (
        attack_target_id(attack_targets.get(self_source_id))
        if isinstance(attack_targets, dict) and self_source_id
        else ""
    )
    if mine_id:
        mine = ranked(mine_id)
        if mine is not None:
            return mine

    # 显示层专属的 30 秒目标表：停手 15~30 秒内仍然显示自己最后打的那只。
    # 播报用 15 秒（要立刻跟上换边），这一行用 30 秒（跟着换会闪）。
    display_targets = snapshot.get("displayAttackTargets")
    display_id = (
        attack_target_id(display_targets.get(self_source_id))
        if isinstance(display_targets, dict) and self_source_id
        else ""
    )
    if display_id:
        mine = ranked(display_id)
        if mine is not None:
            return mine

    # ── A 链路：本关在场的登记 Boss ─────────────────────────────────────
    # 出场包一到就知道这一关是谁，不用等本机出手、也不用等血量包（Boss 不掉血
    # 时服务器根本不下发血量行，实测能晚好几分钟）。取最近出场的那个：
    # 换关时新 Boss 一定比旧的后出场，天然满足「以新 Boss 为准」。
    # 分边副本不用这条：两只同时在场，谁后出场本来就不代表玩家在打哪一侧，
    # 交给下面的本机目标接着判。
    live_rows = [row for row in (live_bosses or ()) if isinstance(row, dict)]
    if live_rows and not split_boss:
        only = live_rows[-1]
        live_name = _scene_text(only.get("name"))
        live_target = _scene_text(only.get("targetId") or only.get("target_id"))
        if live_name and live_target and live_target not in ended:
            return build(live_name, live_target)

    # 本机已经出手过、但目标已经超过 30 秒不可用 —— 按「未识别」留空。
    # 用户明确要求：不打怪时这一行只认自己打过的，不跳到队友正在打的目标。
    # 只有「本机从进本到现在一次都没出手」时才允许往下走，用来覆盖刚进本、
    # 中途开统计还没动手这两种情况。
    if self_source_has_attacked(snapshot, self_source_id):
        return build("")

    # 1) 队伍正在打的主行（party_boss_health）优先，其次快照里任一未结束主行。
    for rows in (rows_of(boss_rows), health_rows):
        primary = newest(candidates(rows, parts=False))
        if primary is not None:
            name = _scene_text(primary.get("name") or primary.get("target_name"))
            return build(name, _scene_text(primary.get("targetId") or primary.get("target_id")))
    # 2) 分部位：没有主行时用父名，避免「没选主部位」就退回未识别。
    primary = newest(candidates(rows_of(boss_rows) + health_rows, parts=True))
    if primary is not None:
        name = _scene_text(primary.get("name") or primary.get("target_name"))
        return build(
            name.split(_PART_SUFFIX)[0].strip(),
            _scene_text(primary.get("targetId") or primary.get("target_id")),
        )
    # 3) 没有 HP 证据时，仍然可以从「谁吃了伤害」确定 Boss 身份。
    #    累计列表没有「在不在打」这层证据，所以只接受确认过的 Boss 模板。
    for key in ("boss_runtime_targets", "damage_targets"):
        picked = newest(candidates(rows_of(snapshot.get(key)), strict=True))
        if picked is not None:
            name = _scene_text(picked.get("target_name") or picked.get("name"))
            return build(
                name.split(_PART_SUFFIX)[0].strip(),
                _scene_text(picked.get("target_id") or picked.get("targetId")),
            )
    # 4) 最终快照里保留的动作证据。
    actions: list[dict[str, Any]] = []
    for row in rows_of(snapshot.get("boss_action_events")):
        target_id = _scene_text(row.get("source_id"))
        if not target_id or target_id in ended or not is_addable(target_id, strict=True):
            continue
        if _scene_text(row.get("source_name")):
            actions.append(row)
    picked = newest(actions)
    if picked is not None:
        return build(_scene_text(picked.get("source_name")), _scene_text(picked.get("source_id")))
    return build("")


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeOverlayMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
