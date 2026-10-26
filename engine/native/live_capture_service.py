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

from ..analytics import record_usage_event
from .nest_inference import known_nest_keys
from ..background_priority import (
    ForegroundLease, foreground_pending, score_checkpoint, score_execution,
)
from ..live.buff_clear import player_buff_clear_times_by_target
from ..live.additional_damage_composition import attach_attributed_skill_views
from ..live.death_report import build_death_reports
from ..live.event_platform import event_platform_for
from ..live.replay_normalization import (
    _merge_rejoined_replay_players,
    _replay_rejoined_player_aliases,
)
from ..live.identity_cache import (
    CachedPlayerIdentity,
    PersistentPlayerIdentityCache,
    PlayerIdentityCloudBridge,
)
from ..live.dpscap_segments import post_capture_splitter
from ..live.catch_up import LiveCatchUpPolicy


def _recover_decode_spool_capture(collector, source_file, data_root):
    """Post-Stop only: replace partial statistics only after complete replay."""
    source = V576DiagnosticReplaySource(
        Path(source_file), data_root, collector.profile.known_players,
    )
    try:
        aggregator = CombatAggregator()
        for batch in source.iter_batches():
            for event in batch:
                aggregator.ingest(event)
        return aggregator, source
    finally:
        source.close()


def _live_refresh_intervals(
    latency: dict[str, Any] | None,
) -> tuple[float, float, float]:
    """Return timeline, critical-overlay and status refresh intervals.

    The compact overlay countdown advances locally with an adaptive 200 ms / 1 s
    clock and stops completely when no countdown is visible, but lifecycle
    edges still need a fresh projection quickly. Keep the compact projection at
    0.5 s normally and cap overload backoff at 1 s; non-essential timeline and
    full status work may back off further while packet decoding is behind.
    """

    sample = latency if isinstance(latency, dict) else {}

    def number(key: str) -> float:
        try:
            value = float(sample.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    delay_ms = max(number("captureToDecodeMs"), number("decoderQueueDelayMs"))
    raw_frames = number("pendingRawFrames")
    decoded_events = number("pendingDecodedEvents")
    severe = (
        number("initialFramesRemaining") > 0
        or delay_ms >= 2_000 or raw_frames >= 8_192 or decoded_events >= 16_384
    )
    degraded = severe or delay_ms >= 500 or raw_frames >= 2_048 or decoded_events >= 4_096
    if severe:
        return 1.50, 1.00, 2.50
    if degraded:
        return 0.75, 0.75, 1.50
    return 0.25, 0.50, 0.75

class NativeLiveCapture:
    """Own ordered live statistics; SQLite history has a dedicated writer."""

    _TIMELINE_WINDOW_MS = 120_000
    _TIMELINE_MAX_EVENTS = 1_200

    def __init__(
        self,
        profile_path: Path,
        payload_enricher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        *,
        score_ready_callback: Callable[
            [CombatSnapshot, str, str, dict[str, Any]], object
        ] | None = None,
        replay_cache_ready_callback: Callable[
            [CombatAggregator, str, str], object
        ] | None = None,
        auto_settings_path: Path | None = None,
        auto_default_enabled: bool = False,
        auto_start_monitor: bool = False,
        timeline_store: DataStore | None = None,
        identity_cache_path: Path | None = None,
    ) -> None:
        self.profile_path = profile_path
        self.payload_enricher = payload_enricher
        self._score_ready_callback = score_ready_callback
        self._replay_cache_ready_callback = replay_cache_ready_callback
        self._auto_settings_path = auto_settings_path
        self._lock = threading.RLock()
        # Capture is the only collector writer. Lightweight projection readers
        # copy under this lock; full current-capture scoring is deferred to Stop.
        self._collector_lock = threading.RLock()
        self._active_collector: LiveDpsCollector | None = None
        self._active_effect_elapsed_seconds = 0.0
        self._active_effect_clock_monotonic = 0.0
        self._voice_clock_running = False
        # 观测流水里 voice_state 的抽样节流（每秒最多一条）。
        self._trace_voice_state_at = 0.0
        self._history_threads: set[threading.Thread] = set()
        # Raw capture is already closed, but the old worker may still freeze
        # statistics. Let the next-map probe capture that transition meanwhile.
        self._ingestion_released = threading.Event()
        # Self identity must remain stable after it has been proven for one
        # collector. Supplemental action decoders can temporarily discover an
        # additional actor candidate; treating that ambiguity as "no self"
        # makes the frontend's strict target filter blank every Buff row.
        self._stable_self_actor_collector: LiveDpsCollector | None = None
        self._stable_self_actor_id = 0
        # Final browser projections are detached from capture shutdown.  A
        # generation token makes them latest-only so an old long projection
        # can never overwrite a newly started capture.
        self._final_projection_generation = 0
        self._final_projection_thread: threading.Thread | None = None
        # Overlay projections are produced by one persistent latest-only
        # worker.  Replacing ``_overlay_projection_pending`` drops obsolete
        # refreshes without ever blocking packet ingest on JSON/UI work.
        self._overlay_projection_generation = 0
        self._overlay_projection_revision = 0
        self._overlay_projection_pending: tuple[
            int, LiveDpsCollector, float, int
        ] | None = None
        self._overlay_projection_wake = threading.Event()
        self._overlay_projection_stop = threading.Event()
        # Wake the main WebView only for player-Buff lifecycle edges. Ordinary
        # countdown ageing remains on the shared 1-second maintenance cadence;
        # the WebView animates the visible countdown independently every 100 ms.
        self._overlay_buff_revision_condition = threading.Condition()
        self._overlay_buff_published_revision = 0
        self._overlay_latency_last_logged = 0.0
        self._overlay_latency_last_raw_revision = 0
        self._overlay_latency_log_path = (
            Path.home()
            / "Documents"
            / "DragonNestDpsCapture"
            / "live_diagnostics"
            / "overlay_latency.jsonl"
        )
        self._overlay_projection_thread = threading.Thread(
            target=self._overlay_projection_loop,
            name="V410OverlayProjection",
            daemon=True,
        )
        self._overlay_projection_thread.start()
        # Full live-page projections are also latest-only, but intentionally
        # use a different worker from the overlay.  They may be expensive on
        # long fights and must never run on the packet-ingest thread.
        self._page_projection_generation = 0
        self._page_projection_pending: tuple[
            int, LiveDpsCollector, str, float
        ] | None = None
        self._page_projection_wake = threading.Event()
        self._page_projection_stop = threading.Event()
        self._page_projection_thread = threading.Thread(
            target=self._page_projection_loop,
            name="V410LivePageProjection",
            daemon=True,
        )
        self._page_projection_thread.start()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._auto_enabled = self._load_auto_enabled(auto_default_enabled)
        self._toggles = self._load_capture_toggles()
        self._auto_probe_lock = threading.RLock()
        self._auto_probe: V576AutoCaptureProbe | None = None
        self._auto_probe_pid = 0
        # ``03 03`` player-spawn identities usually arrive before the dungeon
        # recording boundary. Keep them in memory for the lifetime of the
        # current game process, then seed the combat decoder at hand-off.
        # Runtime IDs are deliberately discarded when the game PID changes so
        # an ID reused by a later client session can never inherit a stale name.
        self._identity_cache_pid = 0
        self._identity_profile_names: dict[int, str] = {}
        self._identity_ambiguous_profile_ids: set[int] = set()
        # A CloudBase/local-cache row may fill an identity gap, but it must
        # never replace a name observed in the current game session.  Keep
        # that provenance separately because the compact name map is also
        # populated by cache lookups.
        self._identity_packet_name_profiles: set[int] = set()
        self._identity_runtime_names: dict[int, str] = {}
        self._identity_runtime_profiles: dict[int, int] = {}
        self._identity_profile_runtime_ids: dict[int, set[int]] = defaultdict(set)
        self._identity_profile_last_seen: dict[int, float] = {}
        self._identity_persisted_jobs: dict[int, str] = {}
        self._identity_capture_frame_groups: dict[
            tuple[int, int],
            tuple[tuple[float, bytes, int, int], ...],
        ] = {}
        self._identity_capture_frame_bytes = 0
        self._session_evidence_frames: dict[
            tuple[int, bytes], tuple[float, bytes, int, int]
        ] = {}
        self._session_evidence_frame_bytes = 0
        self._identity_cache_revision = 0
        self._identity_persistent_sync_at = 0.0
        # Layer 1 is a local retry outbox used only until upload succeeds.
        # Layer 2 is the shared CloudBase cache; its worker never runs on
        # packet-ingest or UI threads. Tests/embedded callers may omit
        # identity_cache_path, while the desktop core supplies the real path.
        self._persistent_identity_cache = (
            PersistentPlayerIdentityCache(identity_cache_path)
            if identity_cache_path is not None
            else None
        )
        self._identity_cloud_bridge = (
            PlayerIdentityCloudBridge(
                self._persistent_identity_cache,
                self._accept_cloud_identity_bindings,
            )
            if self._persistent_identity_cache is not None
            else None
        )
        self._identity_import_thread: threading.Thread | None = None
        # Keep the latest packet-confirmed local-character profile in memory
        # for the lifetime of exactly one game process.  The always-on TCP
        # probe normally sees this login archive before the user opens the
        # build page, so "从游戏识别" can reuse it without restarting Npcap.
        # It is intentionally not persisted to disk: a profile from a prior
        # game process must never be applied to a newly logged-in character.
        self._self_profile_cache_pid = 0
        self._self_profile_cache_revision = 0
        self._self_profile_cache_profile: PlayerInspectEvent | None = None
        self._self_profile_cache_observed_monotonic = 0.0
        self._self_profile_cache_ready = False
        self._self_profile_cache_seen_signatures: set[tuple[object, ...]] = set()
        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._auto_paused_until = 0.0
        # A separate disk-free TCP listener owns identity/profile discovery.
        # It remains active for the whole game session, including while the
        # auto-start probe is running.  The auto probe decodes combat/map data
        # at background priority and can accumulate seconds of work; it must
        # never be the interactive 查看信息 path.
        self._teammate_info_revision = 0
        self._teammate_info_profile: dict[str, Any] | None = None
        self._teammate_info_signature: tuple[object, ...] | None = None
        self._teammate_info_seen_signatures: set[tuple[object, ...]] = set()
        self._teammate_info_profiles: dict[str, dict[str, Any]] = {}
        self._inspect_stop = threading.Event()
        self._inspect_thread: threading.Thread | None = None
        self._identity_probe_lock = threading.RLock()
        self._identity_probe: V576AutoCaptureProbe | None = None
        self._identity_probe_pid = 0
        self._identity_pending_discovery: dict[int, set[str]] = defaultdict(set)
        # The live page consumes casts through a small cursor-based stream.
        # Keeping it outside ``_state`` prevents capture_status() from copying
        # an ever-growing event array together with the aggregate snapshot.
        self._timeline_events: deque[dict[str, Any]] = deque()
        self._timeline_source_count = 0
        self._timeline_latest_sequence = 0
        self._timeline_elapsed_seconds = 0.0
        self._timeline_session_id = ""
        # The incremental timeline is also used by a few lightweight capture
        # entry points which historically did not pass the application's
        # already-loaded store.  A resolver without a store returns ``0`` for
        # every cast; the web timeline then rendered its 0.3s visual fallback
        # until the stopped capture was rebuilt by the replay path.  Always
        # give the live resolver the same catalog used by replay loading.
        self._timeline_store = timeline_store or DataStore.load()
        self._timeline_timing_resolver = LiveCastTimingResolver(
            self._timeline_store
        )
        self._timeline_projection_pending = None
        self._timeline_projection_wake = threading.Event()
        self._timeline_projection_stop = threading.Event()
        self._timeline_projection_thread = threading.Thread(
            target=self._timeline_projection_loop,
            name="V410TimelineProjection", daemon=True,
        )
        self._timeline_projection_thread.start()
        self._state: dict[str, Any] = {
            "state": "idle", "status": "未连接", "error": "",
            "sourceFile": "", "process": "", "pid": 0,
            "udpPorts": [], "tcpPorts": [], "snapshot": None,
            "adapterCount": 0, "acceleratorAdapterCount": 0,
            "acceleratorDiscovery": False,
            "snapshotPublishedMonotonic": 0.0,
            "overlaySnapshot": None,
            "overlaySnapshotPublishedMonotonic": 0.0,
            "overlaySnapshotRevision": 0,
            "overlayLatency": {},
            "startDiagnostics": {},
            # Full scoring waits for Stop to freeze this capture. Keep the
            # handshake so stale live requests cannot build heavy snapshots.
            "scoreReady": False,
            "scoreSourceHash": "",
            "autoScoreJobId": "",
            "autoScoreStatus": "",
            "autoScoreError": "",
            "pageReady": False,
            "autoEnabled": self._auto_enabled,
            "autoStage": "waiting_game" if self._auto_enabled else "off",
            "autoStatus": "等待游戏登录" if self._auto_enabled else "自动统计已关闭",
            "nestKey": "",
            "nestLabel": "",
            "nestName": "",
            "nestConfidence": "",
            "nestTemplateId": 0,
            "nestObservedAt": 0.0,
            "nestMapId": 0,
            "nestMapName": "",
            "nestMapLayout": "",
            "nestMapActive": False,
            "unconfiguredMapIds": [],
        }
        self.snapshot_object: CombatSnapshot | None = None
        if self._persistent_identity_cache is not None:
            self._identity_import_thread = threading.Thread(
                target=self._import_legacy_identity_cache,
                name="PlayerIdentityLegacyImport",
                daemon=True,
            )
            self._identity_import_thread.start()
        if self._auto_enabled and auto_start_monitor:
            self._ensure_auto_monitor_started()
        # 待机零抓包：默认不启动任何常驻探针。
        #
        # 2026-10 起「进本后才点开始统计」不再依赖待机时的实体档案，而是靠
        # 服务端技能起始包里的技能号反查模板（`skill_identity` + 
        # `_observe_skill_identity`）。技能号来自怪物技能表、全局唯一，进本
        # 之后随时都能读到，所以待机抓包没有任何收益，只会白占 CPU/内存。
        #
        # 需要复现历史行为（诊断用）时设环境变量 DNACT_IDLE_PROBE=1。
        self._idle_probe_enabled = str(
            os.environ.get("DNACT_IDLE_PROBE", "")
        ).strip().casefold() in {"1", "true", "yes", "on"}
        if self._idle_probe_enabled:
            self._inspect_thread = threading.Thread(
                target=self._inspect_loop,
                name="V410EntityProbe",
                daemon=True,
            )
            self._inspect_thread.start()

    def _import_legacy_identity_cache(self) -> None:
        cache = self._persistent_identity_cache
        if cache is None:
            return
        try:
            cache.import_legacy_trace_bindings(
                cache.path.parent / "live_diagnostics"
            )
        except Exception:
            # Upgrade bootstrap is best-effort and never blocks capture/UI.
            return

    @staticmethod
    def _is_teammate_inspect_profile(event: PlayerInspectEvent) -> bool:
        fields = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in event.fields
        }
        response_type = fields.get("响应类型", "")
        return bool(
            response_type == "查看信息"
            and str(event.inspection_key or "").strip()
            and not str(event.request_fingerprint or "").startswith(
                "passive-profile:"
            )
            and not str(event.target_id or "").startswith("self-login:")
        )

    @staticmethod
    def _teammate_profile_signature(
        event: PlayerInspectEvent,
    ) -> tuple[object, ...]:
        """Identify one decoded projection, not merely the latest profile.

        The collector retains several inspected teammates and projects all of
        them on every maintenance tick.  A single global "last signature"
        therefore alternated A/B/A/B forever and repeatedly reopened both
        overlays.  Keep a bounded seen set instead.  Include the decoded body
        so an identity-only response can still be upgraded when its equipment
        body becomes available later.
        """

        # Keep only a compact content fingerprint. Retaining the complete
        # equipment/card tuples in the dedupe set kept dozens of old inspect
        # bodies alive after the UI had moved to another character.
        body_fingerprint = hashlib.blake2s(
            repr((
                event.fields,
                event.equipment,
                event.cards,
                event.stats,
            )).encode("utf-8", errors="replace"),
            digest_size=12,
        ).hexdigest()
        return (
            event.target_id,
            event.target_name,
            event.job,
            event.timestamp_ms,
            event.inspection_key,
            event.request_fingerprint,
            event.response_layout,
            body_fingerprint,
        )

    @staticmethod
    def _teammate_inspection_sequence(value: object) -> int:
        """Return the packet timestamp embedded in an inspection key.

        The combat decoder and the independent identity listener observe the
        same TCP exchange on different queues.  Their event-relative clocks
        are not comparable, but both derive ``inspection_key`` from the
        absolute timestamp of the 17 01 request.
        """

        match = re.search(r":(\d{11,})$", str(value or "").strip())
        return int(match.group(1)) if match else 0

    @staticmethod
    def _teammate_profile_body_quality(value: object) -> tuple[int, int, int, int]:
        """Rank a decoded body above the lightweight request shell."""

        if isinstance(value, PlayerInspectEvent):
            equipment = value.equipment
            cards = value.cards
            stats = value.stats
            layout = str(value.response_layout or "")
        elif isinstance(value, dict):
            equipment = value.get("equipment") or ()
            cards = value.get("cards") or ()
            stats = value.get("stats") or ()
            layout = str(value.get("response_layout") or "")
        else:
            return (0, 0, 0, 0)
        return (
            int(bool(equipment) and layout != "TCP:inspect_request:pending"),
            len(equipment),
            len(cards),
            len(stats),
        )

    @classmethod
    def _teammate_profile_supersedes(
        cls,
        incoming: PlayerInspectEvent,
        existing: dict[str, Any] | None,
    ) -> bool:
        """Keep one inspection monotonic across the two live TCP decoders."""

        if not isinstance(existing, dict):
            return True
        incoming_key = str(incoming.inspection_key or "").strip()
        existing_key = str(existing.get("inspection_key") or "").strip()
        incoming_sequence = cls._teammate_inspection_sequence(incoming_key)
        existing_sequence = cls._teammate_inspection_sequence(existing_key)
        if incoming_key and incoming_key == existing_key:
            return cls._teammate_profile_body_quality(incoming) >= (
                cls._teammate_profile_body_quality(existing)
            )
        if incoming_sequence > 0 and existing_sequence > 0:
            return incoming_sequence >= existing_sequence
        return True

    def _publish_teammate_info(self, event: PlayerInspectEvent) -> None:
        if not self._is_teammate_inspect_profile(event):
            return
        event = self._resolve_teammate_profile_identity(event)
        signature = self._teammate_profile_signature(event)
        with self._lock:
            seen = getattr(self, "_teammate_info_seen_signatures", None)
            if seen is None:
                seen = set()
                self._teammate_info_seen_signatures = seen
            if signature in seen:
                return
            seen.add(signature)
            if len(seen) > 128:
                # The retained profile dictionary is capped at sixteen rows;
                # resetting the dedupe set at a much larger bound is safe and
                # prevents one long-running client from growing indefinitely.
                self._teammate_info_seen_signatures = {signature}
            self._teammate_info_signature = signature
            profile = _json_safe(event)
            # Wall-clock publication time is diagnostic metadata only. It
            # lets a field report distinguish packet/decode latency from the
            # later HTTP, panel-projection and native-window stages without
            # retaining raw equipment traffic or relying on UI impressions.
            profile["decoded_published_epoch_ms"] = round(time.time() * 1000)
            # The pending 03 15 event is keyed by ``profile:0x...`` while the
            # completed response may already resolve to a runtime-looking
            # ``0x...`` target.  They are still the same inspection and must
            # replace one row instead of accumulating duplicate profiles and
            # triggering two expensive panel projections.
            profile_id = self._profile_event_profile_id(event)
            profile_key = (
                f"PROFILE:{profile_id:08X}"
                if profile_id > 0
                else norm(event.target_id).upper() or str(signature)
            )
            retained_profile = self._teammate_info_profiles.get(profile_key)
            if not self._teammate_profile_supersedes(event, retained_profile):
                return

            current_profile = self._teammate_info_profile
            current_key = (
                str(current_profile.get("inspection_key") or "").strip()
                if isinstance(current_profile, dict)
                else ""
            )
            incoming_key = str(event.inspection_key or "").strip()
            incoming_sequence = self._teammate_inspection_sequence(incoming_key)
            current_sequence = self._teammate_inspection_sequence(current_key)
            same_current_inspection = bool(
                incoming_key and incoming_key == current_key
            )
            current_can_advance = (
                not isinstance(current_profile, dict)
                or same_current_inspection
                and self._teammate_profile_body_quality(event)
                >= self._teammate_profile_body_quality(current_profile)
                or incoming_sequence > 0
                and current_sequence > 0
                and incoming_sequence >= current_sequence
                or not same_current_inspection
                and (incoming_sequence <= 0 or current_sequence <= 0)
            )
            if current_can_advance:
                # This is a transient current-inspection buffer, not an
                # equipment archive. Combat captures retain their own profile
                # events in the active aggregator/.dpscap; the always-on UI
                # keeps only the character the user most recently opened.
                self._teammate_info_profiles.clear()
                self._teammate_info_profiles[profile_key] = profile
                self._teammate_info_profile = profile
                self._teammate_info_revision += 1
        self._remember_teammate_profile_identity(event)

    def _remember_teammate_profile_identity(
        self,
        event: PlayerInspectEvent,
    ) -> None:
        """Persist a recent profile/name alias without treating it as permanent."""

        profile_id = self._profile_event_profile_id(event)
        name = self._verified_character_name(event.target_name)
        job = str(event.job or "").strip()
        # Inspect projections can temporarily use the profession label as a
        # display fallback. It is not identity evidence and must never become
        # a permanent profile/name binding.
        if (
            profile_id <= 0
            or not name
            or name == job
            or re.fullmatch(r"职业\d+", name)
        ):
            return
        target_id = str(event.target_id or "").strip()
        runtime_id = 0
        if re.fullmatch(r"0x[0-9a-f]+", target_id, re.I):
            try:
                candidate = int(target_id, 16)
            except ValueError:
                candidate = 0
            if candidate > 0 and candidate != profile_id:
                runtime_id = candidate
        with self._lock:
            previous_name = self._identity_profile_names.get(profile_id, "")
            if previous_name and previous_name != name:
                self._identity_ambiguous_profile_ids.add(profile_id)
            self._identity_profile_names[profile_id] = name
            if runtime_id > 0:
                self._identity_runtime_names[runtime_id] = name
                self._identity_runtime_profiles[runtime_id] = profile_id
                self._identity_profile_runtime_ids[profile_id].add(runtime_id)
            self._identity_profile_last_seen[profile_id] = time.monotonic()
        persistent = self._persistent_identity_cache
        if persistent is None:
            return
        persistent.observe(CachedPlayerIdentity(
            profile_id=profile_id,
            runtime_actor_id=runtime_id,
            character_name=name,
            job=job,
            source="inspect_profile_name",
            observed_at=time.time(),
        ), queue_cloud=True)
        if self._identity_cloud_bridge is not None:
            self._identity_cloud_bridge.notify_outbox()

    @staticmethod
    def _profile_event_fields(event: PlayerInspectEvent) -> dict[str, str]:
        return {
            str(key or "").strip(): str(value or "").strip()
            for key, value in event.fields
        }

    @classmethod
    def _profile_event_profile_id(cls, event: PlayerInspectEvent) -> int:
        fields = cls._profile_event_fields(event)
        candidates = (
            fields.get("角色档案标识", ""),
            fields.get("稳定档案ID", ""),
            fields.get("profile_id", ""),
        )
        target_id = str(event.target_id or "").strip()
        if target_id.casefold().startswith("profile:"):
            candidates += (target_id.split(":", 1)[1],)
        for value in candidates:
            text = str(value or "").strip()
            try:
                parsed = int(text, 16 if text.casefold().startswith("0x") else 10)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0

    @staticmethod
    def _verified_character_name(value: object) -> str:
        name = str(value or "").strip()
        if not name or re.fullmatch(r"(?:角色\s*)?0x[0-9a-f]+", name, re.I):
            return ""
        if name in {"未知角色", "未知职业"}:
            return ""
        return name

    def _resolve_teammate_profile_identity(
        self, event: PlayerInspectEvent
    ) -> PlayerInspectEvent:
        """Resolve an inspect response by a recent, unambiguous profile alias.

        The explicit 03 15 request proves the current alias, but the numeric
        value may be reused after reconnect/line/character switches. Cached
        fallback is therefore allowed only when no conflicting current-session
        name exists and the bounded local/cloud cache also reports one name.
        """

        profile_id = self._profile_event_profile_id(event)
        if profile_id <= 0:
            return event
        current_name = self._verified_character_name(event.target_name)
        job = str(event.job or "").strip()
        # Some inspect projections temporarily put the profession label in
        # target_name.  It is presentation fallback, not packet identity.
        packet_name = (
            current_name
            if current_name
            and current_name != job
            and not re.fullmatch(r"职业\d+", current_name)
            else ""
        )
        resolved_name = ""
        resolved_job = ""
        with self._lock:
            if packet_name:
                previous_name = self._identity_profile_names.get(profile_id, "")
                if previous_name and previous_name != packet_name:
                    self._identity_ambiguous_profile_ids.add(profile_id)
                self._identity_profile_names[profile_id] = packet_name
                self._identity_packet_name_profiles.add(profile_id)
                self._identity_profile_last_seen[profile_id] = time.monotonic()
                resolved_name = packet_name
            else:
                if profile_id not in self._identity_ambiguous_profile_ids:
                    resolved_name = self._verified_character_name(
                        self._identity_profile_names.get(profile_id, "")
                    )
            profile_ambiguous = profile_id in self._identity_ambiguous_profile_ids
        persistent = self._persistent_identity_cache
        if not profile_ambiguous and persistent is not None and (
            not resolved_name or not str(event.job or "").strip()
        ):
            cached = persistent.resolve_profile(profile_id)
            if cached is not None:
                resolved_name = resolved_name or self._verified_character_name(
                    cached.character_name
                )
                resolved_job = str(cached.job or "").strip()
        if (
            not profile_ambiguous
            and not resolved_name
            and self._identity_cloud_bridge is not None
        ):
            self._identity_cloud_bridge.request_profile_lookup(profile_id)
        if not resolved_name and not resolved_job:
            return event
        return replace(
            event,
            target_name=packet_name or resolved_name or current_name or event.target_name,
            job=str(event.job or "").strip() or resolved_job,
        )

    @staticmethod
    def _json_profile_id(profile: dict[str, Any]) -> int:
        fields = profile.get("fields", ())
        values: dict[str, object] = {}
        if isinstance(fields, (list, tuple)):
            for row in fields:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    values[str(row[0] or "").strip()] = row[1]
        for key in ("角色档案标识", "稳定档案ID", "profile_id"):
            text = str(values.get(key, "") or "").strip()
            try:
                parsed = int(text, 16 if text.casefold().startswith("0x") else 10)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        target_id = str(profile.get("target_id", "") or "").strip()
        if target_id.casefold().startswith("profile:"):
            target_id = target_id.split(":", 1)[1].strip()
        try:
            parsed = int(
                target_id,
                16 if target_id.casefold().startswith("0x") else 10,
            )
        except (TypeError, ValueError):
            return 0
        if parsed > 0:
            return parsed
        return 0

    def _refresh_teammate_profile_names(
        self, bindings: Iterable[dict[str, object]]
    ) -> None:
        identities = {
            int(row.get("profile_id", 0) or 0): (
                self._verified_character_name(row.get("character_name", "")),
                str(row.get("job", "") or "").strip(),
            )
            for row in bindings
            if isinstance(row, dict)
        }
        identities = {
            profile_id: row
            for profile_id, row in identities.items()
            if profile_id > 0 and (row[0] or row[1])
        }
        if not identities:
            return
        with self._lock:
            changed = False
            for profile in self._teammate_info_profiles.values():
                profile_id = self._json_profile_id(profile)
                if profile_id in self._identity_ambiguous_profile_ids:
                    continue
                name, job = identities.get(profile_id, ("", ""))
                current_name = self._verified_character_name(
                    profile.get("target_name")
                )
                current_job = str(profile.get("job", "") or "").strip()
                unresolved_name = (
                    not current_name
                    or current_name == current_job
                    or bool(re.fullmatch(r"职业\d+", current_name))
                )
                if name and unresolved_name and current_name != name:
                    profile["target_name"] = name
                    changed = True
                if job and not current_job:
                    profile["job"] = job
                    changed = True
            current = self._teammate_info_profile
            if isinstance(current, dict):
                profile_id = self._json_profile_id(current)
                name, job = identities.get(profile_id, ("", ""))
                current_name = self._verified_character_name(
                    current.get("target_name")
                )
                current_job = str(current.get("job", "") or "").strip()
                unresolved_name = (
                    not current_name
                    or current_name == current_job
                    or bool(re.fullmatch(r"职业\d+", current_name))
                )
                if name and unresolved_name and current_name != name:
                    current["target_name"] = name
                    changed = True
                if job and not current_job:
                    current["job"] = job
                    changed = True
            if changed:
                self._teammate_info_revision += 1

    @classmethod
    def _is_direct_self_profile(cls, event: PlayerInspectEvent) -> bool:
        """Return true only for packet-proven local profile archives."""

        fields = cls._profile_event_fields(event)
        response_type = fields.get("响应类型", "")
        fingerprint = str(event.request_fingerprint or "")
        return bool(
            event.equipment
            and (
                (
                    response_type == "被动角色档案"
                    and fingerprint.startswith("passive-profile:")
                )
                or response_type == "自身状态栏"
            )
        )

    @staticmethod
    def _is_self_profile_supplement(event: PlayerInspectEvent) -> bool:
        """Recognize archives emitted only for the local character."""

        layout = str(event.response_layout or "")
        return layout.startswith((
            "TCP:cards_archive:",
            "TCP:engravings_archive:",
            "TCP:card_mastery_archive:",
        ))

    @staticmethod
    def _self_profile_event_signature(
        event: PlayerInspectEvent,
    ) -> tuple[object, ...]:
        return (
            event.target_id,
            event.timestamp_ms,
            event.request_fingerprint,
            event.response_layout,
            event.fields,
            event.equipment,
            event.cards,
            event.stats,
        )

    def _clear_self_profile_cache(self) -> None:
        with self._lock:
            changed = bool(
                self._self_profile_cache_pid
                or self._self_profile_cache_profile is not None
                or self._self_profile_cache_seen_signatures
            )
            self._self_profile_cache_pid = 0
            self._self_profile_cache_profile = None
            self._self_profile_cache_observed_monotonic = 0.0
            self._self_profile_cache_ready = False
            self._self_profile_cache_seen_signatures.clear()
            if changed:
                self._self_profile_cache_revision += 1

    def _publish_self_profile(
        self, pid: int, event: PlayerInspectEvent
    ) -> None:
        """Cache a reliable own profile without accepting teammate inspect data."""

        pid = int(pid or 0)
        if pid <= 0:
            return
        direct = self._is_direct_self_profile(event)
        supplement = self._is_self_profile_supplement(event)
        if not direct and not supplement:
            return
        self._activate_identity_cache_pid(pid)
        signature = self._self_profile_event_signature(event)
        with self._lock:
            if pid != self._self_profile_cache_pid:
                return
            if signature in self._self_profile_cache_seen_signatures:
                return
            self._self_profile_cache_seen_signatures.add(signature)
            if len(self._self_profile_cache_seen_signatures) > 64:
                # These signatures only suppress repeated collector
                # projections; clearing is safe and bounds retained payloads.
                self._self_profile_cache_seen_signatures = {signature}

            if direct:
                # Every new passive login/self-status archive is a complete
                # current-character base. Replace rather than merge so a
                # character switch inside the same game process cannot retain
                # equipment from the previously selected character.
                self._self_profile_cache_profile = event
                self._self_profile_cache_ready = True
            else:
                current = self._self_profile_cache_profile
                if current is None or not self._self_profile_cache_ready:
                    # A card/engraving supplement cannot identify a character
                    # by itself. The decoder will publish a merged login
                    # profile later when a reliable base arrives.
                    return
                self._self_profile_cache_profile = (
                    CombatAggregator._merge_player_profiles(current, event)
                )
            self._self_profile_cache_observed_monotonic = time.monotonic()
            self._self_profile_cache_revision += 1

    def _publish_profile_info(
        self, pid: int, event: PlayerInspectEvent
    ) -> None:
        """Route one decoded profile to the local cache and teammate panel."""

        self._publish_self_profile(pid, event)
        self._publish_teammate_info(event)

    def _drain_probe_profiles(
        self, pid: int, probe: V576AutoCaptureProbe
    ) -> None:
        for event in probe.poll_profiles():
            self._publish_profile_info(pid, event)

    def _publish_collector_teammate_info(
        self, collector: LiveDpsCollector
    ) -> None:
        profiles = getattr(collector.aggregator, "_player_profiles", {})
        with self._lock:
            pid = int(self._state.get("pid") or self._identity_cache_pid or 0)
        for event in tuple(profiles.values()):
            if isinstance(event, PlayerInspectEvent):
                self._publish_profile_info(pid, event)

    def _capture_maintenance_loop(self, pid, collector, source, stop) -> None:
        """Session-owned metadata work; never perform persistence in the ingest loop.

        Inspect events are immutable. Copy references under the statistics lock,
        then publish only changed objects outside it. Stop joins this worker before
        draining/closing the source, so an old session cannot update a new one.
        """
        published = {}
        while not stop.is_set():
            with self._collector_lock:
                if self._active_collector is not collector:
                    return
            started = time.monotonic()
            error = ""
            with self._lock:
                self._state["identityMaintenance"] = {
                    "worker": "session_metadata", "running": True,
                    "startedMonotonic": started,
                    "publishedProfiles": len(published),
                }
            try:
                self._ingest_source_identities(pid, source)
                with self._collector_lock:
                    if self._active_collector is not collector:
                        return
                    profiles = tuple(
                        getattr(collector.aggregator, "_player_profiles", {}).items()
                    )
                for key, event in profiles:
                    if stop.is_set():
                        break
                    if (
                        isinstance(event, PlayerInspectEvent)
                        and published.get(key) is not event
                    ):
                        self._publish_profile_info(pid, event)
                        published[key] = event
            except Exception as exc:
                # A metadata failure must not terminate packet ingestion.
                error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._state["identityMaintenance"] = {
                    "worker": "session_metadata",
                    "running": False,
                    "lastPassMs": round((time.monotonic() - started) * 1000, 3),
                    "publishedProfiles": len(published),
                    "error": error,
                }
            if stop.wait(0.25):
                return

    def self_profile_cache_status(self) -> dict[str, Any]:
        """Return the current-process local profile cache for build sync."""

        with self._lock:
            profile = self._self_profile_cache_profile
            ready = bool(
                self._self_profile_cache_ready
                and profile is not None
                and profile.equipment
                and self._self_profile_cache_pid > 0
            )
            observed = self._self_profile_cache_observed_monotonic
            return {
                "state": "ready" if ready else "empty",
                "pid": self._self_profile_cache_pid,
                "revision": self._self_profile_cache_revision,
                "ageSeconds": (
                    round(max(0.0, time.monotonic() - observed), 3)
                    if ready and observed > 0
                    else None
                ),
                "profile": (
                    copy.deepcopy(_json_safe(profile)) if ready else None
                ),
            }

    def self_identity_status(self) -> dict[str, Any]:
        """Return only the packet-confirmed current character identity."""

        with self._lock:
            profile = self._self_profile_cache_profile
            pid = self._self_profile_cache_pid
            revision = self._self_profile_cache_revision
            observed = self._self_profile_cache_observed_monotonic
            profile_id = (
                self._profile_event_profile_id(profile)
                if profile is not None else 0
            )
            name = self._verified_character_name(
                profile.target_name if profile is not None else ""
            )
            job = str(profile.job or "").strip() if profile is not None else ""
            if name == job or re.fullmatch(r"职业\d+", name):
                name = ""
        if profile_id > 0 and (not name or not job):
            persistent = self._persistent_identity_cache
            cached = persistent.resolve_profile(profile_id) if persistent is not None else None
            if cached is not None:
                name = name or self._verified_character_name(cached.character_name)
                job = job or str(cached.job or "").strip()
        return {
            "state": "ready" if profile_id > 0 and pid > 0 else "empty",
            "pid": pid,
            "revision": revision,
            "profileId": profile_id,
            "characterName": name,
            "job": job,
            "ageSeconds": (
                round(max(0.0, time.monotonic() - observed), 3)
                if profile_id > 0 and observed > 0 else None
            ),
        }

    def teammate_info_status(
        self, since_revision: int | None = None
    ) -> dict[str, Any]:
        with self._lock:
            revision = self._teammate_info_revision
            if since_revision is not None and int(since_revision) == revision:
                return {
                    "revision": revision,
                    "profile": None,
                    "unchanged": True,
                }
            return {
                "revision": revision,
                "profile": copy.deepcopy(self._teammate_info_profile),
            }

    def teammate_profiles_status(
        self, since_revision: int | None = None
    ) -> dict[str, Any]:
        """Return retained inspect profiles for the normal simulator page."""

        with self._lock:
            revision = self._teammate_info_revision
            if since_revision is not None and int(since_revision) == revision:
                return {
                    "revision": revision,
                    "profiles": None,
                    "unchanged": True,
                }
            return {
                "revision": revision,
                "profiles": copy.deepcopy(list(self._teammate_info_profiles.values())),
            }

    def _activate_identity_cache_pid(self, pid: int) -> None:
        """Scope runtime identity aliases to exactly one game process."""

        pid = int(pid or 0)
        if pid <= 0:
            return
        with self._lock:
            if self._identity_cache_pid == pid:
                if self._self_profile_cache_pid != pid:
                    self._self_profile_cache_pid = pid
                return
            self._identity_cache_pid = pid
            self._identity_profile_names.clear()
            self._identity_ambiguous_profile_ids.clear()
            self._identity_packet_name_profiles.clear()
            self._identity_runtime_names.clear()
            self._identity_runtime_profiles.clear()
            self._identity_profile_runtime_ids.clear()
            self._identity_profile_last_seen.clear()
            self._identity_persisted_jobs.clear()
            self._identity_capture_frame_groups.clear()
            self._identity_capture_frame_bytes = 0
            self._session_evidence_frames.clear()
            self._session_evidence_frame_bytes = 0
            self._identity_cache_revision += 1
            self._identity_persistent_sync_at = 0.0
            self._self_profile_cache_pid = pid
            self._self_profile_cache_profile = None
            self._self_profile_cache_observed_monotonic = 0.0
            self._self_profile_cache_ready = False
            self._self_profile_cache_seen_signatures.clear()
            self._self_profile_cache_revision += 1

    def _ingest_player_identities(
        self,
        pid: int,
        identities: Iterable[Any],
        source: Any | None = None,
    ) -> None:
        """Retain exact profile/runtime/name/job bindings from the TCP listener."""

        rows = tuple(identities)
        if not rows:
            return
        self._activate_identity_cache_pid(pid)
        decoder_jobs = self._decoder_actor_jobs(self._source_decoder(source))
        profile_bindings: list[dict[str, object]] = []
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return
            changed = False
            observed_at = time.monotonic()
            for identity in rows:
                try:
                    profile_id = int(identity.profile_id)
                    runtime_id = int(identity.runtime_actor_id)
                except (AttributeError, TypeError, ValueError):
                    continue
                name = str(getattr(identity, "character_name", "") or "").strip()
                if profile_id <= 0 or runtime_id <= 0 or not name:
                    continue
                job = (
                    decoder_jobs.get(runtime_id, "")
                    or decoder_jobs.get(profile_id, "")
                    or self._identity_persisted_jobs.get(runtime_id, "")
                )
                previous_job = self._identity_persisted_jobs.get(
                    runtime_id, ""
                )
                profile_bindings.append({
                    "profile_id": profile_id,
                    "runtime_actor_id": runtime_id,
                    "character_name": name,
                    "job": job,
                    "source": "packet",
                })
                old_profile = self._identity_runtime_profiles.get(runtime_id)
                if old_profile is not None and old_profile != profile_id:
                    self._identity_profile_runtime_ids[old_profile].discard(
                        runtime_id
                    )
                previous_profile_name = self._identity_profile_names.get(
                    profile_id, ""
                )
                if previous_profile_name and previous_profile_name != name:
                    self._identity_ambiguous_profile_ids.add(profile_id)
                if (
                    previous_profile_name != name
                    or self._identity_runtime_names.get(runtime_id) != name
                    or old_profile != profile_id
                    or bool(job and previous_job != job)
                ):
                    changed = True
                self._identity_profile_names[profile_id] = name
                self._identity_packet_name_profiles.add(profile_id)
                self._identity_runtime_names[runtime_id] = name
                self._identity_runtime_profiles[runtime_id] = profile_id
                self._identity_profile_runtime_ids[profile_id].add(runtime_id)
                self._identity_profile_last_seen[profile_id] = observed_at
                if job:
                    self._identity_persisted_jobs[runtime_id] = job
                # Never rewrite older runtime aliases merely because they used
                # the same profile number; profile IDs can be reused.
            # Busy towns can publish hundreds of surrounding players. Keep a
            # bounded recent cache; party/dungeon identities are refreshed on
            # scene entry, while an all-day lobby session cannot grow forever.
            overflow = len(self._identity_profile_names) - 512
            if overflow > 0:
                expired_profiles = sorted(
                    self._identity_profile_names,
                    key=lambda value: self._identity_profile_last_seen.get(
                        value, 0.0
                    ),
                )[:overflow]
                for expired_profile in expired_profiles:
                    self._identity_profile_names.pop(expired_profile, None)
                    self._identity_ambiguous_profile_ids.discard(expired_profile)
                    self._identity_profile_last_seen.pop(expired_profile, None)
                    for alias_id in self._identity_profile_runtime_ids.pop(
                        expired_profile, set()
                    ):
                        self._identity_runtime_names.pop(alias_id, None)
                        self._identity_runtime_profiles.pop(alias_id, None)
                changed = True
            if changed:
                self._identity_cache_revision += 1
        persistent = self._persistent_identity_cache
        bridge = self._identity_cloud_bridge
        if persistent is not None:
            for identity in rows:
                try:
                    profile_id = int(identity.profile_id)
                    runtime_id = int(identity.runtime_actor_id)
                    binding = CachedPlayerIdentity(
                        profile_id=profile_id,
                        runtime_actor_id=runtime_id,
                        character_name=str(identity.character_name or "").strip(),
                        job=(
                            decoder_jobs.get(runtime_id, "")
                            or decoder_jobs.get(profile_id, "")
                        ),
                        source="packet",
                        observed_at=time.time(),
                    )
                except (AttributeError, TypeError, ValueError):
                    continue
                persistent.observe(binding, queue_cloud=True)
            if bridge is not None:
                bridge.notify_outbox()
        # The independent identity listener has no diagnostic trace of its
        # own. Merge its already verified compact rows into the active combat
        # stream, where they are written as the adapter-251 appendix on close.
        if profile_bindings:
            self._refresh_teammate_profile_names(profile_bindings)
            with self._collector_lock:
                collector = self._active_collector
                source = getattr(collector, "source", None) if collector else None
            stage = getattr(source, "stage_profile_identity_bindings", None)
            if callable(stage):
                stage(profile_bindings)

    def _apply_cached_identity_binding(
        self,
        pid: int,
        binding: CachedPlayerIdentity,
    ) -> bool:
        """Apply one already-validated local/cloud binding to this process."""

        try:
            profile_id = int(binding.profile_id)
            runtime_id = int(binding.runtime_actor_id)
        except (TypeError, ValueError):
            return False
        name = str(binding.character_name or "").strip()
        if profile_id <= 0 or runtime_id < 0 or not name:
            return False
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return False
            if profile_id in self._identity_ambiguous_profile_ids:
                return False
            packet_name = self._identity_profile_names.get(profile_id, "")
            if (
                profile_id in self._identity_packet_name_profiles
                and packet_name
                and packet_name != name
            ):
                return False
            # Current-session exact packet evidence always wins. A cached
            # result may fill a blank actor but can never rewrite a different
            # packet-proven profile already bound to this runtime ID.
            current_profile = (
                self._identity_runtime_profiles.get(runtime_id)
                if runtime_id > 0 else None
            )
            if runtime_id > 0 and current_profile not in {None, profile_id}:
                return False
            changed = (
                self._identity_profile_names.get(profile_id) != name
                or runtime_id > 0 and self._identity_runtime_names.get(runtime_id) != name
                or runtime_id > 0 and current_profile != profile_id
            )
            self._identity_profile_names[profile_id] = name
            if runtime_id > 0:
                self._identity_runtime_names[runtime_id] = name
                self._identity_runtime_profiles[runtime_id] = profile_id
                self._identity_profile_runtime_ids[profile_id].add(runtime_id)
            self._identity_profile_last_seen[profile_id] = time.monotonic()
            if changed:
                self._identity_cache_revision += 1
            return changed

    def _accept_cloud_identity_bindings(
        self,
        bindings: tuple[CachedPlayerIdentity, ...],
    ) -> None:
        with self._lock:
            pid = self._identity_cache_pid
        refreshed: list[dict[str, object]] = []
        for binding in bindings:
            applied = self._apply_cached_identity_binding(pid, binding)
            with self._lock:
                packet_conflict = (
                    int(binding.profile_id) in self._identity_packet_name_profiles
                    and self._identity_profile_names.get(int(binding.profile_id), "")
                    != str(binding.character_name or "").strip()
                )
            if not applied and packet_conflict:
                continue
            refreshed.append({
                "profile_id": int(binding.profile_id),
                "character_name": str(binding.character_name or ""),
                "job": str(binding.job or ""),
            })
        self._refresh_teammate_profile_names(refreshed)

    @staticmethod
    def _source_decoder(source: Any) -> Any | None:
        return getattr(source, "_decoder", None)

    @staticmethod
    def _decoder_actor_jobs(decoder: Any) -> dict[int, str]:
        jobs: dict[int, str] = {}
        actor_jobs = getattr(decoder, "actor_jobs", {})
        if not isinstance(actor_jobs, dict):
            return jobs
        for actor_id, value in actor_jobs.items():
            try:
                runtime_id = int(actor_id)
            except (TypeError, ValueError):
                continue
            if isinstance(value, (tuple, list)):
                job = str(value[0] if value else "").strip()
            else:
                job = str(value or "").strip()
            if runtime_id > 0:
                jobs[runtime_id] = job
        return jobs

    def _sync_persistent_source_identities(self, pid: int, source: Any) -> None:
        """Resolve offline/cloud identities and inherit reconnect aliases."""

        persistent = self._persistent_identity_cache
        if persistent is None:
            return
        now_monotonic = time.monotonic()
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return
            if now_monotonic < self._identity_persistent_sync_at:
                return
            # SQLite/cloud coordination is intentionally outside the 40 ms
            # decoder cadence. Reconnect and name display can tolerate this
            # sub-second delay; packet capture cannot tolerate repeated disk
            # reads for every actor on every loop.
            self._identity_persistent_sync_at = now_monotonic + 0.75
        decoder = self._source_decoder(source)
        if decoder is None:
            return
        jobs = self._decoder_actor_jobs(decoder)
        # Current 03 03 spawn rows carry the exact profession at plaintext
        # byte 76. The decoder publishes it through actor_jobs immediately;
        # combat evidence remains a fallback for old/unknown row layouts.
        # Enrich the durable/cloud row exactly once per runtime/job pair
        # instead of writing on every 40 ms poll.
        for runtime_id, job in jobs.items():
            if not job:
                continue
            with self._lock:
                profile_id = self._identity_runtime_profiles.get(runtime_id, 0)
                name = self._identity_runtime_names.get(runtime_id, "")
                persisted_job = self._identity_persisted_jobs.get(runtime_id, "")
            if profile_id and name and persisted_job != job:
                enriched = CachedPlayerIdentity(
                    profile_id=profile_id,
                    runtime_actor_id=runtime_id,
                    character_name=name,
                    job=job,
                    source="packet_job",
                    observed_at=time.time(),
                )
                persistent.observe(enriched, queue_cloud=True)
                with self._lock:
                    self._identity_persisted_jobs[runtime_id] = job
                if self._identity_cloud_bridge is not None:
                    self._identity_cloud_bridge.notify_outbox()
        runtime_aliases = getattr(decoder, "player_runtime_aliases", {})
        if isinstance(runtime_aliases, dict):
            for new_id_value, old_id_value in tuple(runtime_aliases.items()):
                try:
                    new_id = int(new_id_value)
                    old_id = int(old_id_value)
                except (TypeError, ValueError):
                    continue
                with self._lock:
                    profile_id = self._identity_runtime_profiles.get(old_id, 0)
                    name = self._identity_runtime_names.get(old_id, "")
                if not profile_id or not name:
                    inherited = persistent.resolve_runtime(old_id, job=jobs.get(old_id, ""))
                    if inherited is not None:
                        profile_id = inherited.profile_id
                        name = inherited.character_name
                if profile_id and name:
                    binding = CachedPlayerIdentity(
                        profile_id=profile_id,
                        runtime_actor_id=new_id,
                        character_name=name,
                        job=jobs.get(new_id, "") or jobs.get(old_id, ""),
                        source="reconnect_alias",
                        observed_at=time.time(),
                    )
                    if self._apply_cached_identity_binding(pid, binding):
                        persistent.observe(binding, queue_cloud=True)
                        if self._identity_cloud_bridge is not None:
                            self._identity_cloud_bridge.notify_outbox()

        unresolved: list[tuple[int, str]] = []
        candidate_ids = set(jobs)
        observations = getattr(decoder, "player_resource_observations", {})
        if isinstance(observations, dict):
            for value in observations:
                try:
                    candidate = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate > 0:
                    candidate_ids.add(candidate)
        with self._lock:
            already_bound = set(self._identity_runtime_profiles)
        wanted = tuple(
            (runtime_id, jobs.get(runtime_id, ""))
            for runtime_id in candidate_ids - already_bound
        )
        locally_resolved = persistent.resolve_runtimes(wanted)
        for runtime_id, job in wanted:
            binding = locally_resolved.get(runtime_id)
            if binding is not None:
                self._apply_cached_identity_binding(pid, binding)
            else:
                unresolved.append((runtime_id, job))
        known_actor_names = getattr(decoder, "known_actor_names", None)
        if isinstance(known_actor_names, dict):
            known_actor_names.update(self._cached_actor_names(pid))
        stage_profiles = getattr(source, "stage_profile_identity_bindings", None)
        if callable(stage_profiles):
            stage_profiles(self._cached_profile_identity_bindings(pid))
        if unresolved and self._identity_cloud_bridge is not None:
            self._identity_cloud_bridge.request_lookup(unresolved)

    def _cached_actor_names(self, pid: int) -> dict[int, str]:
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return {}
            return {
                **{
                    profile_id: name
                    for profile_id, name in self._identity_profile_names.items()
                    if profile_id not in self._identity_ambiguous_profile_ids
                },
                **self._identity_runtime_names,
            }

    def _cached_profile_identity_bindings(
        self,
        pid: int,
    ) -> tuple[CachedPlayerIdentity, ...]:
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return ()
            return tuple(
                CachedPlayerIdentity(
                    profile_id=profile_id,
                    runtime_actor_id=runtime_id,
                    character_name=self._identity_runtime_names.get(runtime_id, ""),
                    job=self._identity_persisted_jobs.get(runtime_id, ""),
                    source="session_cache",
                    observed_at=time.time(),
                )
                for runtime_id, profile_id in self._identity_runtime_profiles.items()
                if profile_id > 0 and self._identity_runtime_names.get(runtime_id, "")
            )

    def _ingest_identity_capture_groups(
        self,
        pid: int,
        groups: Iterable[Any],
    ) -> None:
        """Drain legacy raw identity groups without retaining packet bodies.

        Verified profile/runtime/name/job rows are already staged through the
        compact adapter-251 appendix. Equipment and other TCP bodies are saved
        only when they are observed by an active combat recording.
        """

        del pid
        for _row in groups:
            pass
        with self._lock:
            self._identity_capture_frame_groups.clear()
            self._identity_capture_frame_bytes = 0

    def _cached_identity_capture_frames(
        self,
        pid: int,
    ) -> tuple[tuple[float, bytes, int, int], ...]:
        """Raw pre-capture identity frames are no longer retained."""

        del pid
        with self._lock:
            self._identity_capture_frame_groups.clear()
            self._identity_capture_frame_bytes = 0
        return ()

    def _ingest_session_evidence_frames(
        self,
        pid: int,
        frames: Iterable[Any],
    ) -> None:
        """Cache verified 21 0C raw frames for the current game process."""

        rows = tuple(frames)
        if not rows:
            return
        self._activate_identity_cache_pid(pid)
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return
            for row in rows:
                try:
                    timestamp, frame, original_length, adapter_index = row
                    payload = bytes(frame)
                except (TypeError, ValueError):
                    continue
                if not payload:
                    continue
                key = (
                    int(original_length),
                    hashlib.blake2s(payload, digest_size=12).digest(),
                )
                if key in self._session_evidence_frames:
                    continue
                frozen = (
                    float(timestamp),
                    payload,
                    int(original_length),
                    int(adapter_index),
                )
                self._session_evidence_frames[key] = frozen
                self._session_evidence_frame_bytes += len(payload)
            while self._session_evidence_frames and (
                len(self._session_evidence_frames) > 4096
                or self._session_evidence_frame_bytes > 16 * 1024 * 1024
            ):
                oldest_key = next(iter(self._session_evidence_frames))
                expired = self._session_evidence_frames.pop(oldest_key)
                self._session_evidence_frame_bytes -= len(expired[1])

    def _cached_session_evidence_frames(
        self,
        pid: int,
    ) -> tuple[tuple[float, bytes, int, int], ...]:
        with self._lock:
            if int(pid or 0) != self._identity_cache_pid:
                return ()
            frames = tuple(self._session_evidence_frames.values())
        return tuple(sorted(frames, key=lambda row: row[0]))

    def _ingest_probe_identities(
        self,
        pid: int,
        probe: V576AutoCaptureProbe,
    ) -> None:
        poll = getattr(probe, "poll_identities", None)
        if callable(poll):
            self._ingest_player_identities(pid, poll(), source=probe)
        poll_frames = getattr(probe, "poll_identity_capture_frames", None)
        if callable(poll_frames):
            self._ingest_identity_capture_groups(pid, poll_frames())
        poll_session_evidence = getattr(
            probe, "poll_session_evidence_frames", None
        )
        if callable(poll_session_evidence):
            self._ingest_session_evidence_frames(
                pid, poll_session_evidence()
            )
        # The probe has already acted on these triggers itself; draining keeps
        # the diagnostic queue bounded during an all-day client session.
        poll_triggers = getattr(
            probe, "poll_identity_discovery_triggers", None
        )
        if callable(poll_triggers):
            poll_triggers()
        self._sync_persistent_source_identities(pid, probe)

    def _request_identity_tcp_discovery(self, pid: int, reason: str) -> bool:
        """Open a 45-second window on the disk-free identity listener."""

        pid = int(pid or 0)
        if pid <= 0:
            return False
        with self._identity_probe_lock:
            probe = (
                self._identity_probe
                if self._identity_probe_pid == pid
                else None
            )
        if probe is None:
            # Before automatic hand-off the auto probe is already the only
            # disk-free listener. Reuse it instead of opening a third Npcap
            # handle for the same process.
            with self._auto_probe_lock:
                probe = (
                    self._auto_probe
                    if self._auto_probe_pid == pid
                    else None
                )
        request = getattr(probe, "request_identity_discovery", None)
        if not callable(request):
            with self._identity_probe_lock:
                self._identity_pending_discovery[pid].add(
                    str(reason or "identity_refresh")
                )
            return False
        request(str(reason or "identity_refresh"), 45.0)
        return True

    def _ingest_source_identities(self, pid: int, source: Any) -> None:
        poll = getattr(source, "poll_identities", None)
        if callable(poll):
            self._ingest_player_identities(pid, poll(), source=source)
        poll_triggers = getattr(
            source, "poll_identity_discovery_triggers", None
        )
        if callable(poll_triggers):
            reasons = tuple(poll_triggers())
            if reasons:
                self._request_identity_tcp_discovery(
                    pid, "+".join(dict.fromkeys(map(str, reasons)))
                )
        self._sync_persistent_source_identities(pid, source)

    def identity_cache_status(self) -> dict[str, Any]:
        """Small diagnostic view used to verify pre-capture identity coverage."""

        with self._lock:
            result = {
                "pid": self._identity_cache_pid,
                "revision": self._identity_cache_revision,
                "profiles": len(self._identity_profile_names),
                "ambiguousProfiles": len(self._identity_ambiguous_profile_ids),
                "runtimeActors": len(self._identity_runtime_names),
                "identityTraceFrames": sum(
                    len(group)
                    for group in self._identity_capture_frame_groups.values()
                ),
                "sessionEvidenceFrames": len(
                    self._session_evidence_frames
                ),
                "names": sorted(set(self._identity_profile_names.values())),
            }
        with self._identity_probe_lock:
            identity_probe = self._identity_probe
            result["identityTcpListener"] = {
                "active": identity_probe is not None,
                "pid": self._identity_probe_pid,
                "discoveryActive": bool(
                    getattr(
                        getattr(identity_probe, "_stream", None),
                        "accelerator_discovery_active",
                        False,
                    )
                ),
                "pendingReasons": sorted(
                    self._identity_pending_discovery.get(
                        self._identity_probe_pid, set()
                    )
                ),
            }
        if self._persistent_identity_cache is not None:
            result["local"] = self._persistent_identity_cache.status()
        if self._identity_cloud_bridge is not None:
            result["cloud"] = self._identity_cloud_bridge.status()
        return result

    def _inspect_loop(self) -> None:
        """Run the independent disk-free identity/profile TCP listener."""

        # Keep this small publisher at normal priority.  It used to be lowered
        # together with background diagnostics, so under sustained capture it
        # could take seconds to drain an already-decoded 03 15 response.  The
        # decoder now has a bounded identity-only fast path, making normal
        # priority both cheap and necessary for interactive inspection.
        probe: V576AutoCaptureProbe | None = None
        probe_pid = 0
        next_process_check = 0.0
        cached_pid = 0
        try:
            while not self._inspect_stop.wait(0.20):
                now = time.monotonic()
                if now >= next_process_check:
                    next_process_check = now + 2.0
                    cached_pid, _process = _windows_process_id(
                        ("dragonnest_reborn.exe", "dragonnest.exe")
                    )
                    self._set_timeline_game_runtime_state(cached_pid > 0, False)
                    if cached_pid > 0:
                        self._activate_identity_cache_pid(cached_pid)
                    else:
                        self._clear_self_profile_cache()
                    if probe is not None and cached_pid != probe_pid:
                        self._ingest_probe_identities(probe_pid, probe)
                        self._drain_probe_profiles(probe_pid, probe)
                        probe.close()
                        self._ingest_probe_identities(probe_pid, probe)
                        self._drain_probe_profiles(probe_pid, probe)
                        with self._identity_probe_lock:
                            if self._identity_probe is probe:
                                self._identity_probe = None
                                self._identity_probe_pid = 0
                        probe = None
                        probe_pid = 0
                if cached_pid <= 0:
                    continue
                if probe is None:
                    tcp_ports = _windows_transport_ports(cached_pid, "tcp")
                    udp_ports = _windows_transport_ports(cached_pid, "udp")

                    def current_ports(
                        pid: int = cached_pid,
                    ) -> tuple[list[int], list[int]]:
                        return _windows_transport_ports(pid, "udp"), _windows_transport_ports(pid, "tcp")

                    try:
                        probe = V576AutoCaptureProbe(
                            udp_ports,
                            self.profile_path.parents[1],
                            ClientProfile.load(self.profile_path).known_players,
                            tcp_ports=tcp_ports,
                            port_provider=current_ports,
                            prebuffer_seconds=3.0,
                            prebuffer_bytes=1024 * 1024,
                            discover_udp=True,
                            discover_tcp=True,
                            known_actor_names=self._cached_actor_names(cached_pid),
                            # 完整解码（否则 Boss 实体表建不起来），但不保留
                            # 原始帧、不聚合、不落盘 —— 只为了「进本后才开统计」
                            # 也能认出 Boss。
                            identity_only=False,
                            entity_only=True,
                            prebattle_udp=True,
                            # Known process ports are sufficient on a direct
                            # connection.  The explicit 45-second rediscovery
                            # triggers still reopen broad TCP when a map,
                            # reconnect or party change actually requires it.
                            arm_tcp_discovery_on_start=not bool(tcp_ports),
                        )
                        probe_pid = cached_pid
                        with self._identity_probe_lock:
                            self._identity_probe = probe
                            self._identity_probe_pid = probe_pid
                            pending_reasons = tuple(
                                self._identity_pending_discovery.pop(
                                    probe_pid, set()
                                )
                            )
                        if pending_reasons:
                            probe.request_identity_discovery(
                                "+".join(sorted(pending_reasons)), 45.0
                            )
                    except Exception:
                        probe = None
                        probe_pid = 0
                        self._inspect_stop.wait(2.0)
                        continue
                # No second combat decoder while formal recording owns UDP.
                # Keep the independent TCP equipment/identity path responsive.
                with self._lock:
                    state_name = self._state.get("state")
                    capture_busy = state_name in {"running", "stopping"} or (
                        state_name == "starting" and getattr(self, "_prebattle_formal_handoff", False)
                    )
                set_prebattle = getattr(probe, "set_prebattle_udp_enabled", None)
                if callable(set_prebattle):
                    set_prebattle(not capture_busy)
                self._ingest_probe_identities(probe_pid, probe)
                self._drain_probe_profiles(probe_pid, probe)
                # 诊断：待机实体探针是否真的建起了 Boss 档案。
                try:
                    decoder = getattr(probe, "_decoder", None)
                    entities = getattr(decoder, "monster_entities", {}) or {}
                    bosses = [
                        template
                        for template in entities.values()
                        if getattr(template, "boss", False)
                    ]
                    self._state["entityProbe"] = {
                        "active": True,
                        "pid": probe_pid,
                        "entities": len(entities),
                        "bosses": len(bosses),
                        "error": str(getattr(probe, "error", "") or "")[:200],
                    }
                except Exception:
                    self._state["entityProbe"] = {"active": True, "error": "diagnostic-failed"}
                if probe.error:
                    self._drain_probe_profiles(probe_pid, probe)
                    probe.close()
                    self._drain_probe_profiles(probe_pid, probe)
                    with self._identity_probe_lock:
                        if self._identity_probe is probe:
                            self._identity_probe = None
                            self._identity_probe_pid = 0
                    probe = None
                    probe_pid = 0
        finally:
            if probe is not None:
                self._ingest_probe_identities(probe_pid, probe)
                self._drain_probe_profiles(probe_pid, probe)
                probe.close()
                self._ingest_probe_identities(probe_pid, probe)
                self._drain_probe_profiles(probe_pid, probe)
            with self._identity_probe_lock:
                if self._identity_probe is probe:
                    self._identity_probe = None
                    self._identity_probe_pid = 0

    def _idle_monster_entity_seeds(self, pid: int) -> dict[int, int]:
        """待机探针收集到的 Boss 实体档案（actor -> 模板号）。"""

        with self._identity_probe_lock:
            probe = self._identity_probe if self._identity_probe_pid == pid else None
        export = getattr(probe, "export_monster_entities", None)
        if not callable(export):
            return {}
        try:
            seeds = export()
        except (RuntimeError, ValueError):
            # 探针线程正在写字典时重试一次即可，不能因此阻塞开始统计。
            try:
                seeds = export()
            except Exception:
                return {}
        return seeds if isinstance(seeds, dict) else {}

    def _idle_prebattle_snapshot(self, pid: int, boundary: float) -> dict[str, object] | None:
        with self._identity_probe_lock:
            probe = self._identity_probe if self._identity_probe_pid == pid else None
        export = getattr(probe, "prebattle_snapshot", None)
        disable = getattr(probe, "set_prebattle_udp_enabled", None)
        if callable(disable):
            disable(False)
        return export(boundary) if callable(export) else None

    def _idle_prebattle_tracker(self, pid: int) -> object:
        with self._identity_probe_lock:
            probe = self._identity_probe if self._identity_probe_pid == pid else None
        return getattr(probe, "_prebattle_state", None)

    def _read_auto_settings(self) -> dict[str, Any]:
        path = self._auto_settings_path
        if path is None or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_auto_settings(self, payload: dict[str, Any]) -> None:
        path = self._auto_settings_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            # Capture availability must not depend on a writable config path.
            pass

    def _load_auto_enabled(self, default: bool) -> bool:
        # 待机零抓包：自动统计（进图自动开始）已移除。老版本可能把
        # auto_capture_enabled=true 写进了设置文件，这里必须无视它，否则
        # 一启动又会拉起抓包探针。
        del default
        return False

    def _save_auto_enabled(self) -> None:
        payload = self._read_auto_settings()
        payload["auto_capture_enabled"] = self._auto_enabled
        self._write_auto_settings(payload)

    def _load_capture_toggles(self):
        from ..live.capture_toggles import CaptureToggles
        payload = self._read_auto_settings()
        return CaptureToggles.from_mapping(payload.get("capture_toggles"))

    def _save_capture_toggles(self) -> None:
        payload = self._read_auto_settings()
        payload["capture_toggles"] = self._toggles.to_mapping()
        self._write_auto_settings(payload)

    def get_toggles(self) -> dict[str, bool]:
        return self._toggles.to_mapping()

    def set_toggles(self, mapping) -> dict[str, bool]:
        from ..live.capture_toggles import CaptureToggles
        self._toggles = CaptureToggles.from_mapping(mapping)
        self._save_capture_toggles()
        with self._collector_lock:
            collector = self._active_collector
        if collector is not None:
            collector.set_toggles(self._toggles)
        return self._toggles.to_mapping()

    def _ensure_auto_monitor_started(self) -> None:
        self._auto_stop.clear()
        if self._auto_thread is None or not self._auto_thread.is_alive():
            self._auto_thread = threading.Thread(
                target=self._auto_loop,
                name="V410AutoCaptureMonitor",
                daemon=True,
            )
            self._auto_thread.start()

    def status(self, *, include_snapshot: bool = True) -> dict[str, Any]:
        with self._lock:
            # The desktop overlay has its own compact endpoint. Returning its
            # snapshot together with the live-page snapshot duplicated the
            # entire stopped replay in JSON (often well over 100 MB) and made
            # the Stop request appear frozen after long encounters.
            state_name = str(self._state.get("state") or "")
            compact_live_snapshot = (
                self._state.get("overlaySnapshot")
                if state_name in {"starting", "running", "stopping"}
                and isinstance(self._state.get("overlaySnapshot"), dict)
                else None
            )
            result: dict[str, Any] = {}
            for key, value in self._state.items():
                if key == "overlaySnapshot":
                    continue
                if key == "snapshot":
                    if not include_snapshot:
                        continue
                    value = (
                        compact_live_snapshot
                        if compact_live_snapshot is not None
                        else value
                    )
                result[key] = copy.deepcopy(value)
        result["npcapInstalled"] = find_npcap_dll() is not None
        source_file = str(result.get("sourceFile") or "").strip()
        result["captureSegments"] = (
            post_capture_splitter.status(Path(source_file))
            if source_file
            else {"status": "none"}
        )
        return result

    def overlay_status(self) -> dict[str, Any]:
        """Return status without copying the progressively growing live page."""

        with self._lock:
            overlay_snapshot = self._state.get("overlaySnapshot")
            snapshot = (
                overlay_snapshot
                if overlay_snapshot is not None
                else self._state.get("snapshot")
            )
            collector = self._active_collector
            live_bosses = self._live_bosses(collector) if collector is not None else []
            return {
                "state": self._state.get("state", "idle"),
                "status": self._state.get("status", ""),
                "nestKey": self._state.get("nestKey", ""),
                "nestLabel": self._state.get("nestLabel", ""),
                "nestName": self._state.get("nestName", ""),
                "nestConfidence": self._state.get("nestConfidence", ""),
                "nestTemplateId": self._state.get("nestTemplateId", 0),
                "nestObservedAt": self._state.get("nestObservedAt", 0.0),
                "nestMapId": self._state.get("nestMapId", 0),
                "nestMapName": self._state.get("nestMapName", ""),
                "nestMapLayout": self._state.get("nestMapLayout", ""),
                "nestMapActive": self._state.get("nestMapActive", False),
                "unconfiguredMapIds": self._state.get(
                    "unconfiguredMapIds", []
                ),
                # 「进本即绑定」的 Boss：出场包一到就有，不等它掉血。
                # focus 白名单与悬浮窗 Boss 行的 A 链路都读这一份。
                "liveBosses": live_bosses,
                "overlaySnapshot": copy.deepcopy(snapshot),
                "overlaySnapshotPublishedMonotonic": (
                    self._state.get("overlaySnapshotPublishedMonotonic")
                    or self._state.get("snapshotPublishedMonotonic")
                    or 0.0
                ),
                "overlaySnapshotRevision": self._state.get(
                    "overlaySnapshotRevision", 0
                ),
                "overlayLatency": dict(
                    self._state.get("overlayLatency") or {}
                ),
            }

    def wait_overlay_buff_revision(
        self,
        after_revision: int,
        timeout_seconds: float = 15.0,
    ) -> dict[str, object]:
        """Wait for Buff apply/refresh/remove without polling full snapshots."""

        expected = max(0, int(after_revision))
        timeout = max(0.0, min(25.0, float(timeout_seconds)))
        with self._overlay_buff_revision_condition:
            if self._overlay_buff_published_revision == expected and timeout:
                self._overlay_buff_revision_condition.wait_for(
                    lambda: (
                        self._overlay_buff_published_revision != expected
                        or self._overlay_projection_stop.is_set()
                    ),
                    timeout=timeout,
                )
            current = self._overlay_buff_published_revision
        return {
            "changed": current != expected,
            "buffStateRevision": current,
        }

    def _set_overlay_buff_published_revision(self, revision: int) -> None:
        with self._overlay_buff_revision_condition:
            resolved = max(0, int(revision))
            changed = resolved != self._overlay_buff_published_revision
            self._overlay_buff_published_revision = resolved
            if changed:
                self._overlay_buff_revision_condition.notify_all()

    def timeline_status(self, after_sequence: int = 0) -> dict[str, Any]:
        """Return only new live casts while retaining a bounded catch-up window."""

        requested = max(0, int(after_sequence or 0))
        with self._lock:
            events = list(self._timeline_events)
            latest = self._timeline_latest_sequence
            oldest = int(events[0].get("sequence", latest + 1)) if events else latest + 1
            reset = requested > latest or (requested > 0 and requested < oldest - 1)
            selected = events if reset else [
                row for row in events if int(row.get("sequence", 0)) > requested
            ]
            return {
                "state": self._state.get("state", "idle"),
                "sessionId": self._timeline_session_id,
                "elapsedSeconds": self._timeline_elapsed_seconds,
                "oldestSequence": oldest,
                "latestSequence": latest,
                "reset": reset,
                "windowSeconds": self._TIMELINE_WINDOW_MS / 1000.0,
                "maxEvents": self._TIMELINE_MAX_EVENTS,
                "events": copy.deepcopy(selected),
            }

    def voice_status(self) -> dict[str, Any]:
        """Bounded event-only feed: never serialize a combat snapshot or casts."""
        with self._lock:
            collector = self._active_collector
            decoder = getattr(getattr(collector, "source", None), "_decoder", None)
            feed = getattr(decoder, "voice_signal_feed", None)
            elapsed = self._active_effect_elapsed_seconds
            anchor = getattr(self, "_active_effect_clock_monotonic", 0.0)
            if anchor and self._state.get("state") == "running":
                elapsed += max(0.0, time.monotonic() - anchor)
            payload = {
                "state": self._state.get("state", "idle"),
                "sessionId": self._timeline_session_id,
                "elapsedSeconds": elapsed,
                "clockRunning": getattr(self, "_voice_clock_running", True),
                "voiceEvents": feed.snapshot() if feed is not None else [],
                # 血量贴地后不再收到 0 包的 Boss：前端据此清掉它的倒计时/已好。
                # 场景 Boss（P2 空中海龙）豁免：它选不中、血量长期 0.9%，
                # 只靠技能行维持活跃，不该被静默阈值判成离场。
                "retiredBossIds": (
                    feed.retired_sources(
                        int(elapsed * 1000),
                        self._scene_templates_for(decoder),
                    )
                    if feed is not None
                    else []
                ),
            }
        self._trace_voice_state(payload, decoder)
        return payload

    def _trace_voice_state(self, payload: dict[str, Any], decoder: Any) -> None:
        """每秒把「前端做判断时看到的输入」写进观测流水。

        抓包能重算事件，但 ``state`` / ``clockRunning`` / ``elapsedMs`` / ``focus``
        只存在于运行时。缺了它们，回放只能证明「规则该播」，证明不了「播出去了」。
        锁外调用，且无论如何都不抛出。
        """

        probe = getattr(decoder, "probe", None) if decoder is not None else None
        if probe is None or not getattr(probe, "enabled", False):
            return
        now = time.monotonic()
        if now - self._trace_voice_state_at < 1.0:
            return
        self._trace_voice_state_at = now
        events = payload.get("voiceEvents") or []
        last = events[-1] if events else {}
        try:
            probe.record(
                "voice_state",
                state=payload.get("state"),
                clock=payload.get("clockRunning"),
                elapsedMs=round(float(payload.get("elapsedSeconds") or 0.0) * 1000),
                rows=len(events),
                lastSeq=int(last.get("sequence") or 0),
                lastTs=int(last.get("timestamp") or 0),
                retired=len(payload.get("retiredBossIds") or ()),
            )
        except Exception:
            pass

    def record_probe_event(self, kind: str, fields: dict[str, Any]) -> bool:
        """前端把「这一批事件怎么处理的」写进同一条观测流水。

        这样一场副本只产生两份东西：``.dpscap``（原始帧）和 ``probe_*.jsonl``
        （解码事实 + 运行时状态 + 前端处置）。回放时用它们就能逐条对账，
        不需要再让用户复现。
        """

        with self._lock:
            collector = self._active_collector
            decoder = getattr(getattr(collector, "source", None), "_decoder", None)
        probe = getattr(decoder, "probe", None) if decoder is not None else None
        if probe is None or not getattr(probe, "enabled", False):
            return False
        clean = {
            str(key): value
            for key, value in dict(fields or {}).items()
            if str(key) not in {"kind", "t"}
        }
        try:
            probe.record(str(kind or "frontend_event"), **clean)
            return True
        except Exception:
            return False

    @staticmethod
    def _scene_templates_for(decoder: Any) -> tuple[int, ...]:
        """当前副本声明的场景 Boss 模板（识别不到副本时为空）。

        场景 Boss（梦魇海龙 P2 的空中海龙）选不中、血量长期贴地，只靠技能行
        维持活跃。它们不参与离场判定，否则 180 秒静默阈值会把倒计时整行清掉
        再重建，悬浮窗上就是一次闪烁。
        """

        try:
            from .nest_inference import scene_boss_templates

            key = str(getattr(decoder, "detected_nest_key", "") or "")
            return tuple(sorted(scene_boss_templates(key)))
        except Exception:
            return ()

    def _reset_timeline_locked(self) -> None:
        self._timeline_events.clear()
        self._timeline_source_count = 0
        self._timeline_latest_sequence = 0
        self._timeline_elapsed_seconds = 0.0
        self._timeline_session_id = uuid.uuid4().hex
        # An old projection may still be finishing outside the capture locks.
        # Replace its resolver instead of mutating it concurrently.
        self._timeline_timing_resolver = LiveCastTimingResolver(self._timeline_store)
        self._timeline_projection_pending = None

    def live_source_hash(self) -> str:
        """Return the stable in-memory key for the current capture session."""

        with self._lock:
            session_id = self._timeline_session_id
        return f"live:{session_id}" if session_id else ""

    def score_snapshot(
        self,
        expected_source_hash: str = "",
    ) -> tuple[CombatSnapshot, int, str]:
        """Resolve a full score snapshot only outside active capture/finalization."""

        with self._lock:
            capture_state = str(self._state.get("state") or "")
        if capture_state == "stopping":
            raise KeyError("live_capture_finalizing")

        current_source_hash = self.live_source_hash()
        if expected_source_hash and expected_source_hash != current_source_hash:
            raise KeyError("live_capture_session_changed")
        if capture_state in {"starting", "running"}:
            # The UI already defers full scoring until Stop. Enforce the same
            # rule for stale/manual HTTP requests before acquiring the collector.
            raise KeyError("live_capture_scoring_deferred_until_stop")
        with self._collector_lock:
            with self._lock:
                if self._state.get("state") in {"starting", "running"}:
                    raise KeyError("live_capture_scoring_deferred_until_stop")
                if self._state.get("state") == "stopping":
                    raise KeyError("live_capture_finalizing")
            collector = self._active_collector
            if collector is None:
                snapshot = self.snapshot_object
                if snapshot is None:
                    raise KeyError("live_capture_not_ready")
                return snapshot, 0, ""
            revision = int(collector.aggregator.revision)
            snapshot = collector.snapshot(
                self._active_effect_elapsed_seconds,
                include_events=True,
                live_clock=True,
            )
        with self._lock:
            source_file = str(self._state.get("sourceFile") or "")
        return snapshot, revision, source_file

    def resolve_score_source_hash(self, expected_source_hash: str) -> str:
        """Promote a stale live key to the immutable stopped-capture key."""

        expected = str(expected_source_hash or "").strip()
        if not expected.startswith("live:"):
            return expected
        with self._lock:
            if (
                expected == self.live_source_hash()
                and self._state.get("state") in {"stopping", "stopped"}
                and bool(self._state.get("scoreReady"))
            ):
                return str(self._state.get("scoreSourceHash") or expected).strip()
        return expected

    def _publish_frozen_score_ready(
        self,
        snapshot: CombatSnapshot,
        source_file: str,
        source_hash: str,
        final_status: str = "实时统计已停止，页面明细正在后台整理",
        aggregator: CombatAggregator | None = None,
    ) -> None:
        """Expose the exact frozen snapshot before page projection finishes.

        Team scoring consumes ``snapshot_object`` directly.  It must not wait
        for the much larger browser payload (Boss views, curves and event JSON)
        to be projected and copied.
        """

        platform = event_platform_for(snapshot)
        self.snapshot_object = snapshot
        with self._lock:
            current_payload = self._state.get("snapshot")
            # A Stop can win the race before the first full-page projection is
            # published.  In that case ``snapshot`` is still a small live-key
            # placeholder (or an overlay-shaped payload) and does not contain
            # the numeric fields consumed by the page header.  Always overlay
            # the exact frozen summary so the live -> final hand-off is a valid
            # ReplayPayload even while the expensive detail projection runs.
            interim_payload = {
                **(current_payload if isinstance(current_payload, dict) else {}),
                "elapsed_seconds": platform.elapsed_seconds,
                "dps_elapsed_seconds": platform.dps_elapsed_seconds,
                "instance_completion": _json_safe(platform.instance_completion),
                "total_damage": platform.total_damage,
                "total_healing": platform.total_healing,
                "players": _json_safe(platform.players),
                "damage_targets": _json_safe(platform.damage_targets),
                "sourceFile": source_file,
                "sourceHash": source_hash,
            }
            self._state.update(
                state="stopped",
                status=final_status,
                snapshot=interim_payload,
                snapshotPublishedMonotonic=time.monotonic(),
                acceleratorDiscovery=False,
                scoreReady=bool(source_hash),
                scoreSourceHash=source_hash,
                pageReady=False,
            )
            callback_state = {
                key: copy.deepcopy(value)
                for key, value in self._state.items()
                if key != "snapshot"
            }

        # Scoring is intentionally started only after the exact event snapshot
        # and immutable capture hash are both published.  The callback may only
        # enqueue work; the capture thread must never execute the expensive
        # theory/score calculation itself.
        callback = self._score_ready_callback
        if callback is not None and source_hash:
            try:
                job_id = str(
                    callback(snapshot, source_file, source_hash, callback_state)
                    or ""
                ).strip()
            except Exception as exc:
                with self._lock:
                    self._state.update(
                        autoScoreJobId="",
                        autoScoreStatus="后台评分启动失败",
                        autoScoreError=f"{type(exc).__name__}: {exc}",
                    )
            else:
                if job_id:
                    with self._lock:
                        self._state.update(
                            autoScoreJobId=job_id,
                            autoScoreStatus="已加入后台评分与上传队列",
                            autoScoreError="",
                        )

        # The complete live aggregator already contains the expensive packet
        # semantics.  Hand it to a low-priority post-capture cache worker so a
        # future first reopen does not decode the same .dpscap from scratch.
        # The callback must only enqueue work and return immediately.
        cache_callback = self._replay_cache_ready_callback
        if (
            cache_callback is not None
            and aggregator is not None
            and source_file
            and source_hash
        ):
            try:
                cache_callback(aggregator, source_file, source_hash)
            except Exception:
                # Replay caching is an optional acceleration.  It must never
                # turn a successfully stopped capture into an error state.
                pass

    @staticmethod
    def _final_page_snapshot_payload(snapshot: CombatSnapshot) -> dict[str, Any]:
        """Build the same bounded browser projection used by replay loading."""

        platform = event_platform_for(snapshot)

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
        # The page does not consume attribution-debug fields.  Keeping only
        # the factual columns avoids turning a long capture into a huge Stop
        # response while the complete immutable event stays in the backend.
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
        return payload

    def _resolved_skill_cast_payloads(
        self,
        snapshot: CombatSnapshot,
    ) -> list[dict[str, Any]]:
        """Serialize final live casts with the same timings as replay loading.

        ``SkillCastEvent`` intentionally stores only protocol facts, so a raw
        dataclass-to-JSON conversion has no ``cast_time`` field.  The browser
        uses 0.3 seconds only as a last-resort visual fallback; publishing raw
        casts after Stop therefore made every block on both the shared and
        score timelines appear to last 0.3 seconds until the capture was
        reloaded.  Resolve the immutable final axis from the beginning so
        action-speed windows are reconstructed in deterministic order too.
        """

        platform = event_platform_for(snapshot)
        resolver = LiveCastTimingResolver(
            self._timeline_store,
            player_buff_clear_times_by_target(
                platform.player_buff_clear_events
            ),
        )
        rows: list[dict[str, Any]] = []
        for event in platform.skill_cast_events:
            timing = resolver.resolve(event, event.timestamp_ms / 1000.0)
            rows.append(
                {
                    **_json_safe(event),
                    "cast_time": timing.cast_time,
                    "action_speed_percent": timing.action_speed_percent,
                    "end_timestamp_ms": round(timing.end_seconds * 1000),
                }
            )
        return rows

    def _start_final_page_projection(
        self,
        snapshot: CombatSnapshot,
        source_file: str,
        source_hash: str,
        self_source_id: str,
        aggregator: CombatAggregator,
        final_status: str,
    ) -> None:
        """Build the large browser payload without delaying capture Stop.

        Scoring already owns the exact immutable ``snapshot_object``. Boss
        curves, effect views and JSON conversion are page conveniences and
        may finish later. Only the newest capture generation may publish.
        """

        with self._lock:
            self._final_projection_generation += 1
            generation = self._final_projection_generation

        def project() -> None:
            set_current_thread_low_priority()
            try:
                payload = self._final_page_snapshot_payload(snapshot)
                # Replay loading enriches its cast rows before publishing.
                # The stopped-live path must do the same because the frontend
                # discards the bounded incremental stream when Stop is pressed
                # and switches to this final snapshot for both timeline views.
                payload["skill_cast_events"] = (
                    self._resolved_skill_cast_payloads(snapshot)
                )
                encounters = tuple(BossRecognizer().recognize_snapshot(snapshot))
                payload["boss_encounters"] = _boss_encounter_payload(
                    snapshot, encounters, source_file
                )
                payload["boss_hp_curves"] = _boss_hp_curve_payload(
                    snapshot, source_file, encounters
                )
                payload["boss_effect_views"] = _boss_effect_views_payload(
                    snapshot, aggregator, encounters
                )
                payload["sourceFile"] = source_file
                payload["selfSourceId"] = self_source_id
                payload["sourceHash"] = source_hash
                if self.payload_enricher is not None:
                    payload = self.payload_enricher(payload)
            except Exception as exc:
                with self._lock:
                    if (
                        generation == self._final_projection_generation
                        and self._state.get("scoreSourceHash") == source_hash
                    ):
                        self._state.update(
                            status=(
                                f"{final_status}；页面明细整理失败，"
                                "可重新载入本次抓包"
                            ),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                return
            with self._lock:
                if (
                    generation != self._final_projection_generation
                    or self._state.get("state") != "stopped"
                    or self._state.get("scoreSourceHash") != source_hash
                ):
                    return
                self._state.update(
                    status=final_status,
                    snapshot=payload,
                    snapshotPublishedMonotonic=time.monotonic(),
                    overlaySnapshotPublishedMonotonic=time.monotonic(),
                    error="",
                    pageReady=True,
                )

        thread = threading.Thread(
            target=project,
            name="V410FinalPageProjection",
            daemon=True,
        )
        self._final_projection_thread = thread
        thread.start()

    @staticmethod
    def _runtime_alias_count(capture_source: object | None) -> int:
        """Return the number of proven reconnect runtime-ID aliases."""

        decoder = getattr(capture_source, "_decoder", None)
        aliases = getattr(decoder, "player_runtime_aliases", None)
        return len(aliases) if isinstance(aliases, dict) else 0

    @staticmethod
    def _stopped_visible_runtime_aliases(
        collector: LiveDpsCollector,
        capture_source: object | None,
        snapshot: CombatSnapshot,
    ) -> dict[str, str]:
        """Return aliases that actually collapse two visible player rows."""

        decoder = getattr(capture_source, "_decoder", None)
        aliases: dict[str, str] = {}
        for raw_alias, raw_canonical in (
            getattr(decoder, "player_runtime_aliases", {}) or {}
        ).items():
            try:
                alias_id = f"0x{int(raw_alias):08X}"
                canonical_id = f"0x{int(raw_canonical):08X}"
            except (TypeError, ValueError):
                continue
            if alias_id != canonical_id:
                aliases[alias_id] = canonical_id

        # A remote reconnect may not resend the exact 21 0C or 03 03 row.
        # At Stop the complete cast/profile evidence is finally available, so
        # run the same conservative non-overlap matcher used by replay.  This
        # is an in-memory evidence scan, not a second packet decode.
        aggregator = collector.aggregator
        evidence_events = [
            *getattr(aggregator, "_skill_cast_events", ()),
            *getattr(aggregator, "_party_members", {}).values(),
            *getattr(aggregator, "_player_profiles", {}).values(),
            *getattr(aggregator, "_player_health_events", ()),
            *getattr(aggregator, "_player_session_evidence", ()),
        ]
        inferred = _replay_rejoined_player_aliases(
            evidence_events,
            getattr(decoder, "profile_lineages", None),
        )
        aliases.update(inferred)

        def canonical(actor_id: str) -> str:
            current = str(actor_id or "")
            seen: set[str] = set()
            while current in aliases and current not in seen:
                seen.add(current)
                current = aliases[current]
            return current

        visible_ids = {
            str(player.source_id or "")
            for player in event_platform_for(snapshot).players
            if str(player.source_id or "")
        }
        if len({canonical(actor_id) for actor_id in visible_ids}) == len(
            visible_ids
        ):
            return {}
        return {
            actor_id: canonical(actor_id)
            for actor_id in visible_ids
            if canonical(actor_id) != actor_id
        }

    def _normalize_stopped_reconnect_snapshot(
        self,
        collector: LiveDpsCollector,
        capture_source: object | None,
        source_file: str,
        snapshot: CombatSnapshot,
    ) -> tuple[CombatSnapshot, CombatAggregator, dict[str, object]]:
        """Rebuild the final view when live runtime IDs may be split.

        The live decoder can prove a reconnect only after events from the old
        runtime ID have already entered ``CombatAggregator``.  Future batches
        are canonicalized, but mutating every historical damage, HP, Buff,
        summon and cast ledger in place is both fragile and easy to make
        scoring-dependent.  The saved DNDP1 trace already owns the complete
        evidence and its replay path performs the fixed-point actor merge.

        Replaying every ordinary Stop would be wasteful.  Limit this exact
        finalization pass to captures where conservative identity evidence
        proves that two visible rows are one player. Total damage is an
        invariant: if replay cannot reproduce it byte-for-byte, fail closed
        and retain the live snapshot.
        """

        original_aggregator = collector.aggregator
        original_platform = event_platform_for(snapshot)
        original_player_count = len(original_platform.players)
        alias_count = self._runtime_alias_count(capture_source)
        visible_aliases = self._stopped_visible_runtime_aliases(
            collector,
            capture_source,
            snapshot,
        )
        diagnostics: dict[str, object] = {
            "attempted": False,
            "aliasCount": alias_count,
            "visibleAliasCount": len(visible_aliases),
            "visibleAliases": dict(visible_aliases),
            "playersBefore": original_player_count,
            "playersAfter": original_player_count,
            "applied": False,
        }
        if not visible_aliases:
            return snapshot, original_aggregator, diagnostics

        source_events = list(
            getattr(original_aggregator, "_source_events", ()) or ()
        )
        if source_events:
            diagnostics["attempted"] = True
            diagnostics["method"] = "retained_events"
            decoder = getattr(capture_source, "_decoder", None)
            canonicalize = getattr(
                decoder,
                "_canonicalize_runtime_alias_events",
                None,
            )
            if callable(canonicalize):
                source_events = canonicalize(source_events)
            source_events = _merge_rejoined_replay_players(
                source_events,
                decoder,
            )
            rebuilt_aggregator = CombatAggregator(
                authoritative_boss_max_hp=bool(
                    getattr(
                        original_aggregator,
                        "_authoritative_boss_max_hp",
                        False,
                    )
                )
            )
            for event in source_events:
                rebuilt_aggregator.ingest(event)
            rebuilt_snapshot = rebuilt_aggregator.snapshot(
                include_events=True
            )
            rebuilt_platform = event_platform_for(rebuilt_snapshot)
            rebuilt_player_count = len(rebuilt_platform.players)
            diagnostics["playersAfter"] = rebuilt_player_count
            if (
                rebuilt_platform.total_damage
                == original_platform.total_damage
                and rebuilt_player_count < original_player_count
            ):
                diagnostics["applied"] = True
                return (
                    rebuilt_snapshot,
                    rebuilt_aggregator,
                    diagnostics,
                )
            diagnostics["retainedEventsFallback"] = (
                "damage_invariant_failed"
                if rebuilt_platform.total_damage
                != original_platform.total_damage
                else "visible_alias_not_merged"
            )

        path = Path(str(source_file or "")).expanduser()
        if not path.is_file() or path.suffix.casefold() != ".dpscap":
            diagnostics["error"] = "final_capture_file_unavailable"
            return snapshot, original_aggregator, diagnostics

        diagnostics["attempted"] = True
        diagnostics["method"] = "capture_replay"
        source: object | None = None
        try:
            source = V576DiagnosticReplaySource(
                path,
                self.profile_path.parents[1],
                collector.profile.known_players,
            )
            rebuilt_aggregator = CombatAggregator()
            iter_batches = getattr(source, "iter_batches", None)
            if callable(iter_batches):
                for batch in iter_batches():
                    for event in batch:
                        rebuilt_aggregator.ingest(event)
            else:
                for event in source.poll():
                    rebuilt_aggregator.ingest(event)
            rebuilt_snapshot = rebuilt_aggregator.snapshot(include_events=True)
        except Exception as exc:
            diagnostics["error"] = f"{type(exc).__name__}: {exc}"
            return snapshot, original_aggregator, diagnostics
        finally:
            close_source = getattr(source, "close", None)
            if callable(close_source):
                try:
                    close_source()
                except Exception:
                    pass

        rebuilt_platform = event_platform_for(rebuilt_snapshot)
        rebuilt_player_count = len(rebuilt_platform.players)
        diagnostics["playersAfter"] = rebuilt_player_count
        if rebuilt_platform.total_damage != original_platform.total_damage:
            diagnostics["error"] = (
                "damage_invariant_failed:"
                f"{original_platform.total_damage}->{rebuilt_platform.total_damage}"
            )
            return snapshot, original_aggregator, diagnostics
        if rebuilt_player_count > original_player_count:
            diagnostics["error"] = (
                "player_count_increased:"
                f"{original_player_count}->{rebuilt_player_count}"
            )
            return snapshot, original_aggregator, diagnostics

        if rebuilt_player_count >= original_player_count:
            diagnostics["error"] = "visible_alias_not_merged_by_replay"
            return snapshot, original_aggregator, diagnostics

        diagnostics["applied"] = True
        return rebuilt_snapshot, rebuilt_aggregator, diagnostics

    def _queue_timeline_projection(self, collector, elapsed):
        with self._lock:
            self._timeline_projection_pending = (self._timeline_session_id, collector, elapsed)
        self._timeline_projection_wake.set()

    def _timeline_projection_loop(self):
        set_current_thread_low_priority()
        while not self._timeline_projection_stop.is_set():
            self._timeline_projection_wake.wait(0.5)
            self._timeline_projection_wake.clear()
            with self._lock:
                pending = self._timeline_projection_pending
                self._timeline_projection_pending = None
            if pending is None:
                continue
            session, collector, elapsed = pending
            try:
                self._publish_timeline_events(collector, elapsed, expected_session=session)
            except Exception as exc:
                with self._lock:
                    if self._timeline_session_id == session:
                        self._state["timelineProjectionError"] = f"{type(exc).__name__}: {exc}"

    def _publish_timeline_events(
        self,
        collector: LiveDpsCollector,
        effect_elapsed_seconds: float,
        expected_session: str | None = None,
    ) -> None:
        """Copy references briefly; do timing/JSON work outside capture locks."""
        with self._collector_lock:
            with self._lock:
                if expected_session is not None and expected_session != self._timeline_session_id:
                    return
                source = getattr(collector.aggregator, "_skill_cast_events", ())
                source_count = len(source)
                if source_count < self._timeline_source_count:
                    self._reset_timeline_locked()
                session = self._timeline_session_id
                resolver = self._timeline_timing_resolver
                new_events = tuple(source[self._timeline_source_count:source_count])
                clear_events = tuple(getattr(collector.aggregator, "_player_buff_clear_events", ()))
        resolver.set_player_buff_clear_times(player_buff_clear_times_by_target(clear_events))
        rows = []
        for event in new_events:
            row = _json_safe(event)
            timing = resolver.resolve(event, event.timestamp_ms / 1000.0)
            row.update(
                cast_time=timing.cast_time,
                action_speed_percent=timing.action_speed_percent,
                end_timestamp_ms=round(timing.end_seconds * 1000),
            )
            rows.append(row)
        with self._lock:
            if session != self._timeline_session_id:
                return
            for row in rows:
                self._timeline_latest_sequence += 1
                row["sequence"] = self._timeline_latest_sequence
                self._timeline_events.append(row)
            self._timeline_source_count = source_count
            self._timeline_elapsed_seconds = max(
                self._timeline_elapsed_seconds,
                max(0.0, float(effect_elapsed_seconds)),
            )
            cutoff_ms = max(
                0,
                round(self._timeline_elapsed_seconds * 1000)
                - self._TIMELINE_WINDOW_MS,
            )
            while self._timeline_events and (
                len(self._timeline_events) > self._TIMELINE_MAX_EVENTS
                or int(self._timeline_events[0].get("timestamp_ms", 0)) < cutoff_ms
            ):
                self._timeline_events.popleft()

    def set_auto_enabled(self, enabled: bool) -> dict[str, Any]:
        # 待机零抓包：自动统计已移除。即使外部调用传 True 也保持关闭，
        # 不能在待机时悄悄拉起抓包探针。
        enabled = False
        with self._lock:
            self._auto_enabled = enabled
            self._state.update(
                autoEnabled=enabled,
                autoStage="off",
                autoStatus="自动统计已关闭",
            )
            self._save_auto_enabled()
        if enabled:
            self._ensure_auto_monitor_started()
        else:
            self._auto_stop.set()
            with self._auto_probe_lock:
                probe = self._auto_probe
                probe_pid = self._auto_probe_pid
                self._auto_probe = None
                self._auto_probe_pid = 0
            if probe is not None:
                self._ingest_probe_identities(probe_pid, probe)
                probe.close()
                self._ingest_probe_identities(probe_pid, probe)
        return self.status()

    def _set_auto_status(self, stage: str, message: str) -> None:
        with self._lock:
            if self._auto_enabled:
                self._state.update(autoStage=stage, autoStatus=message)

    def _auto_probe_blocked_by_capture(self) -> bool:
        """Post-capture statistics must not prevent observing the next map."""
        with self._lock:
            return bool(
                self._thread is not None and self._thread.is_alive()
                and not self._ingestion_released.is_set()
            )

    def _auto_loop(self) -> None:
        next_process_check = 0.0
        cached_pid = 0
        cached_process = ""
        while not self._auto_stop.wait(0.25):
            with self._lock:
                enabled = self._auto_enabled
                capture_running = self._auto_probe_blocked_by_capture()
            if not enabled:
                return
            if capture_running:
                self._set_auto_status("capturing", "已检测到副本进图，统计中")
                continue
            now = time.monotonic()
            if now < self._auto_paused_until:
                remaining = max(1, int(self._auto_paused_until - now + 0.999))
                self._set_auto_status("paused", f"手动停止后暂停 {remaining}s")
                continue
            if now >= next_process_check:
                next_process_check = now + 2.0
                cached_pid, cached_process = _windows_process_id(
                    ("dragonnest_reborn.exe", "dragonnest.exe")
                )
                self._set_timeline_game_runtime_state(cached_pid > 0, False)
                if cached_pid > 0:
                    self._activate_identity_cache_pid(cached_pid)
                else:
                    self._clear_self_profile_cache()
                with self._auto_probe_lock:
                    if self._auto_probe is not None and cached_pid != self._auto_probe_pid:
                        self._ingest_probe_identities(
                            self._auto_probe_pid, self._auto_probe
                        )
                        self._drain_probe_profiles(
                            self._auto_probe_pid, self._auto_probe
                        )
                        self._auto_probe.close()
                        self._ingest_probe_identities(
                            self._auto_probe_pid, self._auto_probe
                        )
                        self._drain_probe_profiles(
                            self._auto_probe_pid, self._auto_probe
                        )
                        self._auto_probe = None
                        self._auto_probe_pid = 0
            if cached_pid <= 0:
                self._set_auto_status("waiting_game", "等待游戏登录")
                continue
            with self._auto_probe_lock:
                probe_missing = self._auto_probe is None
            if probe_missing:
                try:
                    udp_ports = _windows_transport_ports(cached_pid, "udp")
                    tcp_ports = _windows_transport_ports(cached_pid, "tcp")

                    def current_ports(pid: int = cached_pid) -> tuple[list[int], list[int]]:
                        return (
                            _windows_transport_ports(pid, "udp"),
                            _windows_transport_ports(pid, "tcp"),
                        )

                    with self._auto_probe_lock:
                        if self._auto_probe is None:
                            self._auto_probe = V576AutoCaptureProbe(
                                udp_ports,
                                self.profile_path.parents[1],
                                ClientProfile.load(self.profile_path).known_players,
                                tcp_ports=tcp_ports,
                                port_provider=current_ports,
                                # Accelerators may own the visible Windows
                                # sockets, leaving the game PID with no ports.
                                # The bounded probes discover the pre-tunnel
                                # v576 UDP/TCP flows directly.
                                discover_udp=True,
                                discover_tcp=True,
                                arm_tcp_discovery_on_start=not bool(tcp_ports),
                                known_actor_names=self._cached_actor_names(
                                    cached_pid
                                ),
                            )
                            self._auto_probe_pid = cached_pid
                    self._set_auto_status("monitoring", "轻量监听中，等待进入副本地图")
                except Exception as exc:
                    self._set_auto_status("error", f"轻量监听重试中：{exc}")
                    self._auto_stop.wait(3.0)
                continue
            with self._auto_probe_lock:
                probe = self._auto_probe
            if probe is None:
                continue
            self._ingest_probe_identities(cached_pid, probe)
            self._drain_probe_profiles(cached_pid, probe)
            if probe.error:
                with self._auto_probe_lock:
                    if self._auto_probe is probe:
                        self._ingest_probe_identities(cached_pid, probe)
                        self._drain_probe_profiles(cached_pid, probe)
                        probe.close()
                        self._ingest_probe_identities(cached_pid, probe)
                        self._drain_probe_profiles(cached_pid, probe)
                        self._auto_probe = None
                        self._auto_probe_pid = 0
                self._set_auto_status("error", f"轻量监听重试中：{probe.error}")
                continue
            if not probe.confirmed:
                probe_stream = getattr(probe, "_stream", None)
                adapter_count = int(getattr(probe_stream, "adapter_count", 0) or 0)
                discovery = bool(
                    getattr(probe_stream, "accelerator_discovery_active", False)
                )
                self._set_auto_status(
                    "monitoring",
                    (
                        f"加速器兼容探测中（{adapter_count}张网卡）"
                        if discovery
                        else f"轻量监听中，等待进入副本地图（{adapter_count}张网卡，逻辑包 {probe.packet_count}）"
                    ),
                )
                continue
            reason = probe.confirmation_reason
            with self._lock:
                finalizing = self._thread is not None and self._thread.is_alive()
            if finalizing:
                # Keep the confirmed probe and its ordered boundary buffer.
                # Passing it to start() now would close it as "already busy".
                self._set_auto_status("confirmed", "已监听到下一次进本，保留进图数据，等待上一把尾部冻结")
                continue
            try:
                with self._auto_probe_lock:
                    if self._auto_probe is not probe:
                        continue
                    self._ingest_probe_identities(cached_pid, probe)
                    self._drain_probe_profiles(cached_pid, probe)
                    handoff = probe.handoff()
                    self._ingest_probe_identities(cached_pid, probe)
                    self._drain_probe_profiles(cached_pid, probe)
                    self._auto_probe = None
                    self._auto_probe_pid = 0
                self._set_auto_status("confirmed", f"已确认：{reason}")
                self.start(0.0, handoff=handoff, automatic=True)
            except Exception as exc:
                self._set_auto_status("error", f"自动启动失败：{exc}")
                self._auto_paused_until = time.monotonic() + 5.0

    def _self_source_id(self, collector: LiveDpsCollector) -> str:
        """Return the packet-confirmed local actor instead of a UI selection."""
        source = getattr(collector, "source", None)
        decoder = getattr(source, "_decoder", None)
        if self._stable_self_actor_collector is not collector:
            self._stable_self_actor_collector = collector
            self._stable_self_actor_id = 0
        actor_id = int(getattr(decoder, "current_self_actor_id", 0) or 0)
        local_actor_ids = {
            int(value)
            for value in (getattr(decoder, "local_actor_ids", set()) or set())
            if int(value) > 0
        }
        if actor_id > 0:
            canonicalizer = getattr(decoder, "_canonical_player_actor_id", None)
            if callable(canonicalizer):
                actor_id = int(canonicalizer(actor_id) or actor_id)
            self._stable_self_actor_id = actor_id
        elif (
            self._stable_self_actor_id > 0
            and self._stable_self_actor_id in local_actor_ids
        ):
            actor_id = self._stable_self_actor_id
        elif len(local_actor_ids) == 1:
            actor_id = next(iter(local_actor_ids))
            self._stable_self_actor_id = actor_id
        try:
            return f"0x{int(actor_id):08X}" if int(actor_id or 0) > 0 else ""
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _set_timeline_game_runtime_state(
        game_present: bool,
        combat_active: bool,
    ) -> None:
        """Separate the relaxed logged-in budget from live-combat limits."""

        try:
            from ..timeline_parallel import set_timeline_game_runtime_state

            set_timeline_game_runtime_state(game_present, combat_active)
        except (ImportError, RuntimeError):
            # Capture must stay available in minimal/legacy embedded builds.
            pass

    @staticmethod
    def _detected_nest_from_decoder(decoder: object | None) -> dict[str, Any]:
        key = str(getattr(decoder, "detected_nest_key", "") or "").strip()
        name = str(getattr(decoder, "detected_nest_name", "") or "").strip()
        if not key or not name:
            return {}
        map_history = [
            dict(row)
            for row in (
                getattr(decoder, "detected_map_history", ()) or ()
            )
            if isinstance(row, dict)
        ]
        # ``detected_map_id`` is the current scene. After leaving a nest it
        # can therefore be Saint Haven/Garden while ``detected_nest_name``
        # intentionally remains the scored dungeon. Bind the nest to its
        # latest battle row instead of combining a town ID with a dungeon
        # name in the final score/upload state.
        battle_row = next(
            (
                row
                for row in reversed(map_history)
                if int(row.get("map_type") or 0) == 3
            ),
            None,
        )
        confidence = str(
            getattr(decoder, "detected_nest_confidence", "") or ""
        ).strip()
        exact_battle = confidence == "exact_map_id" and battle_row is not None
        nest_map_id = (
            int(battle_row.get("map_id") or 0)
            if exact_battle
            else 0
        )
        nest_map_name = (
            str(battle_row.get("map_name") or name).strip()
            if exact_battle
            else ""
        )
        nest_map_layout = (
            str(battle_row.get("layout") or "").strip()
            if exact_battle
            else ""
        )
        return {
            "nestKey": key,
            "nestLabel": str(
                getattr(decoder, "detected_nest_label", "") or name
            ).strip(),
            "nestName": name,
            "nestConfidence": confidence,
            "nestTemplateId": int(
                getattr(decoder, "detected_nest_template_id", 0) or 0
            ),
            "nestObservedAt": round(
                max(
                    0.0,
                    float(
                        getattr(decoder, "detected_nest_observed_at", 0.0)
                        or 0.0
                    ),
                ),
                3,
            ),
            "nestMapId": nest_map_id,
            "nestMapName": nest_map_name,
            "nestMapLayout": nest_map_layout,
            "nestMapActive": bool(
                exact_battle and battle_row.get("left_at") is None
            ),
            "nestMapHistory": map_history,
            "unconfiguredMapIds": sorted(
                int(value)
                for value in getattr(
                    decoder,
                    "detected_unconfigured_map_ids",
                    (),
                )
                if int(value) > 0
            ),
        }

    def _inferred_nest(self, collector: LiveDpsCollector) -> dict[str, Any]:
        """用「本机正在攻击的实体」反推副本。

        只读聚合器里那张极小的 ``lastAttackTargets`` 表（待机零抓包后仍然由
        伤害事件旁路维护），不构建快照，开销是一次 dict 查询。
        """

        from .nest_inference import infer_nest_from_attack_targets

        aggregator = getattr(collector, "aggregator", None)
        attacks = getattr(aggregator, "_overlay_last_attack_targets", None)
        if not attacks:
            self._state["nestInference"] = {"reason": "no-attack-target"}
            return {}
        # 优先用解码器的本机 actor（O(1)）；`_self_source_id` 会遍历本地
        # actor 列表，放在每轮 poll 里没必要。
        decoder = getattr(getattr(collector, "source", None), "_decoder", None)
        self_source = ""
        try:
            actor = int(getattr(decoder, "current_self_actor_id", 0) or 0)
            if actor > 0:
                self_source = f"0x{actor & 0xFFFFFFFF:08X}"
        except (TypeError, ValueError):
            self_source = ""
        if not self_source:
            try:
                self_source = self._self_source_id(collector)
            except Exception:
                self_source = ""
        names = getattr(aggregator, "_overlay_target_names", None)
        inferred = infer_nest_from_attack_targets(attacks, self_source, names)
        if not inferred:
            # 有攻击目标但都不在 Boss 模板表里（打小怪 / 副本未收录）。
            self._state["nestInference"] = {
                "reason": "unknown-target",
                "self": self_source,
                "names": dict(list((names or {}).items())[-6:]),
                "targets": sorted(
                    {
                        str(
                            value.get("targetId") if isinstance(value, dict) else value
                        )
                        for value in attacks.values()
                    }
                )[:8],
            }
            return {}
        self._state["nestInference"] = {
            "reason": "matched",
            "self": self_source,
            "templateId": int(inferred.get("nestTemplateId") or 0),
            "nestKey": inferred.get("nestKey", ""),
        }
        return {
            "nestKey": inferred["nestKey"],
            "nestLabel": inferred["nestLabel"],
            "nestName": inferred["nestName"],
            "nestConfidence": inferred["nestConfidence"],
            "nestTemplateId": int(inferred.get("nestTemplateId") or 0),
            "nestObservedAt": round(time.time(), 3),
            "nestMapId": 0,
            "nestMapName": "",
            "nestMapLayout": "",
            "nestMapActive": False,
            "unconfiguredMapIds": [],
        }

    def _live_bosses(self, collector: LiveDpsCollector) -> list[dict[str, Any]]:
        """「本关」在场的 Boss：出场包绑到的、属于最新阶段的那些。

        这是「进本即绑定」的唯一数据来源。Boss 出场包（88 字节 ``06 27 55 00``）
        一到就进 ``boss_actor_ids``；血量包要等它掉血才发，实测能晚好几分钟，
        所以这里**不看血量**。

        ``boss_actor_ids`` 本身只增不减（Boss 死了不删，只有 actor 被复用时替换），
        直接拿来当「在场」会一路累积，最后九只全在，A 链路永远不成立。所以按
        解码器的**阶段编号**取最新一批：``_monster_birth_phase`` 记录每个 actor
        出生时的阶段序号，换关时新 Boss 名字变化会让序号 +1。

        同一阶段里仍有多个是正常的：红龙佩达 P1/P2 同名同阶段、终局本体 + 分身。
        调用方给的是「这一关的 Boss 组」，不是单只。
        """

        decoder = getattr(getattr(collector, "source", None), "_decoder", None)
        if decoder is None:
            return []
        entities = getattr(decoder, "monster_entities", None) or {}
        actors = getattr(decoder, "boss_actor_ids", None) or ()
        if not entities or not actors:
            return []

        # 悬浮窗 4 次/秒轮询这个状态，而 ``retired_sources`` 要扫一遍事件行。
        # 一秒算一次足够：出场/死亡都是秒级事件，取不到更新晚一拍没有体感。
        now = time.monotonic()
        if now - float(getattr(self, "_live_bosses_at", 0.0) or 0.0) < 1.0:
            return list(getattr(self, "_live_bosses_cache", ()) or ())

        ended: set[str] = set()
        ledger = getattr(
            getattr(collector, "aggregator", None), "_boss_hp_ledger", None
        )
        if ledger is not None:
            try:
                ended = {str(value) for value in ledger.ended_target_ids()}
            except Exception:
                ended = set()

        retired: set[str] = set()
        feed = getattr(decoder, "voice_signal_feed", None)
        if feed is not None:
            elapsed = float(getattr(self, "_active_effect_elapsed_seconds", 0.0) or 0.0)
            anchor = float(getattr(self, "_active_effect_clock_monotonic", 0.0) or 0.0)
            if anchor and self._state.get("state") == "running":
                elapsed += max(0.0, time.monotonic() - anchor)
            try:
                retired = {
                    str(value)
                    for value in feed.retired_sources(
                        int(max(0.0, elapsed) * 1000),
                        self._scene_templates_for(decoder),
                    )
                }
            except Exception:
                retired = set()

        birth = getattr(decoder, "_monster_birth_phase", None) or {}
        latest_phase = max((int(value) for value in birth.values()), default=0)
        rows: list[tuple[float, int, int]] = []
        for raw_actor in actors:
            try:
                actor_id = int(raw_actor)
            except (TypeError, ValueError):
                continue
            template = entities.get(actor_id)
            if template is None:
                continue
            # 只留最新阶段：旧关卡的 Boss 不删，靠阶段号把它挡在外面。
            # 没有阶段记录的 actor（技能号反查补出来的，比如靠技能身份才认出
            # 的梦魇海龙）一律视为本阶段——它们本来就是「现在这场」的线索。
            phase = birth.get(actor_id)
            if birth and phase is not None and int(phase) != latest_phase:
                continue
            try:
                template_id = int(getattr(template, "template_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if template_id <= 0:
                continue
            target_id = f"0x{actor_id:08X}@{template_id}"
            if target_id in ended or target_id in retired:
                continue
            rows.append((float(birth.get(actor_id, 0) or 0), actor_id, template_id))
        rows.sort()

        out: list[dict[str, Any]] = []
        for _phase, actor_id, template_id in rows:
            try:
                name = str(decoder._monster_display_context(actor_id)[0] or "")
            except Exception:
                name = ""
            out.append(
                {
                    "templateId": template_id,
                    "actorId": f"0x{actor_id:08X}",
                    "targetId": f"0x{actor_id:08X}@{template_id}",
                    "name": name,
                }
            )
        self._live_bosses_at = now
        self._live_bosses_cache = out
        return out

    @staticmethod
    def _detected_nest(collector: LiveDpsCollector) -> dict[str, Any]:
        source = getattr(collector, "source", None)
        return NativeLiveCapture._detected_nest_from_decoder(
            getattr(source, "_decoder", None)
        )

    def _refresh_skill_identity_state(self, collector: LiveDpsCollector) -> None:
        """把「技能号反查」的结果写进状态，供诊断接口查看。

        每 2 秒刷一次就够了：学习索引本身按文件 mtime 缓存，这里只是把
        「注入过哪些 actor、索引有多大」抄给 /api/capture/status。
        """

        now = time.monotonic()
        if now - getattr(self, "_skill_identity_state_at", 0.0) < 2.0:
            return
        self._skill_identity_state_at = now
        decoder = getattr(getattr(collector, "source", None), "_decoder", None)
        seeds = getattr(decoder, "late_identity_seeds", None) or {}
        try:
            from .skill_identity import stats as skill_index_stats

            index = skill_index_stats()
        except Exception:
            index = {}
        self._state["skillIdentity"] = {
            "indexSkills": int(index.get("skills", 0) or 0),
            "indexPairs": int(index.get("pairs", 0) or 0),
            "indexUpdatedAt": str(index.get("updatedAt", "") or ""),
            "seedCount": len(seeds),
            "seeds": {
                f"0x{int(actor):08X}": int(template)
                for actor, template in list(seeds.items())[:8]
            },
        }

    def start(
        self,
        duration: float = 0.0,
        *,
        handoff: tuple[Any, tuple[tuple[float, bytes, int, int], ...]] | None = None,
        automatic: bool = False,
        discover_tcp: bool = False,
    ) -> dict[str, Any]:
        requested_at = time.time()
        requested_monotonic = time.monotonic()
        pending_probe: tuple[int, V576AutoCaptureProbe] | None = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if handoff is not None:
                    handoff[0].close()
                return self.status()
        # Manual Start must return immediately.  Probe hand-off can wait up to
        # five seconds for its decoder thread, so detach ownership here and do
        # the blocking transfer inside ``_run`` instead of the HTTP thread.
        if handoff is None:
            with self._auto_probe_lock:
                probe = self._auto_probe
                if probe is not None:
                    pending_probe = (self._auto_probe_pid, probe)
                    self._auto_probe = None
                    self._auto_probe_pid = 0
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if handoff is not None:
                    handoff[0].close()
                if pending_probe is not None:
                    pending_probe[1].close()
                return self.status()
            self._stop.clear()
            self._ingestion_released.clear()
            self._final_projection_generation += 1
            self._overlay_projection_generation += 1
            self._overlay_projection_pending = None
            self._overlay_latency_last_raw_revision = 0
            self._stable_self_actor_collector = None
            self._stable_self_actor_id = 0
            self._page_projection_generation += 1
            self._page_projection_pending = None
            self._reset_timeline_locked()
            self._prebattle_formal_handoff = False
            self._state.update(
                state="starting",
                status="正在查找游戏端口",
                error="",
                sourceFile="",
                process="",
                pid=0,
                udpPorts=[],
                tcpPorts=[],
                snapshot=None,
                snapshotPublishedMonotonic=0.0,
                overlaySnapshot=None,
                overlaySnapshotPublishedMonotonic=0.0,
                overlaySnapshotRevision=0,
                overlayLatency={},
                historyWriter={},
                statisticsPipeline={},
                identityMaintenance={},
                startDiagnostics={
                    "requestedEpochMs": round(requested_at * 1000),
                    "requestedMonotonic": requested_monotonic,
                    "apiReturnedMs": 0.0,
                    "workerStartedMs": 0.0,
                    "handoffCompletedMs": 0.0,
                    "processResolvedMs": 0.0,
                    "portsResolvedMs": 0.0,
                    "collectorConstructedMs": 0.0,
                    "collectorReadyMs": 0.0,
                    "stateRunningMs": 0.0,
                    "usedProbeHandoff": pending_probe is not None,
                },
                stopDiagnostics={},
                scoreReady=False,
                scoreSourceHash="",
                autoScoreJobId="",
                autoScoreStatus="",
                autoScoreError="",
                pageReady=False,
                nestKey="",
                nestLabel="",
                nestName="",
                nestConfidence="",
                nestTemplateId=0,
                nestObservedAt=0.0,
                nestMapId=0,
                nestMapName="",
                nestMapLayout="",
                nestMapActive=False,
                # 地图历史必须和身份字段一起清空。之前只清了 nestName 而保留
                # nestMapHistory，导致上一轮的「黑龙巢穴（一）」会冒充本轮的
                # 识别结果：日志里明明只有神圣天堂，界面上却能看到巢穴名。
                nestMapHistory=[],
                unconfiguredMapIds=[],
            )
            self._set_overlay_buff_published_revision(0)
            # A stopped live session or replay can leave a finalized snapshot
            # available for scoring.  It must never become the starting point
            # of the next capture session.
            self.snapshot_object = None
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    max(0.0, float(duration)),
                    handoff,
                    automatic,
                    discover_tcp,
                    pending_probe,
                    requested_at,
                    requested_monotonic,
                ),
                name="V410NpcapCapture",
                daemon=True,
            )
            # Mark the whole live lifetime so optional post-capture work (most
            # importantly the decoded replay cache) yields to Npcap capture.
            post_capture_splitter.capture_started()
            try:
                self._thread.start()
            except BaseException:
                post_capture_splitter.capture_finished()
                raise
            diagnostics = dict(self._state.get("startDiagnostics") or {})
            diagnostics["apiReturnedMs"] = round(
                max(0.0, time.monotonic() - requested_monotonic) * 1000.0,
                3,
            )
            self._state["startDiagnostics"] = diagnostics
        record_usage_event("capture_attempt")
        return self.status()

    @staticmethod
    def _handoff_probe_for_capture(
        probe: V576AutoCaptureProbe,
        *,
        automatic: bool,
        manual_boundary_timestamp: float | None = None,
    ) -> tuple[Any, tuple[tuple[float, bytes, int, int], ...]]:
        """Transfer the listening stream without replaying pre-click traffic.

        Automatic capture needs the probe's boundary-trimmed prebuffer so the
        opener is not lost.  A manual Start already defines its own recording
        boundary, so decoding and writing the probe's preceding 12-second /
        32-MiB rolling buffer only causes a CPU/disk spike and can put events
        from before the click into the recording.  ``start_trace`` still
        drains packets that arrived during the short handoff, so no traffic
        after the click is lost.
        """

        if not automatic:
            prepare_boundary = getattr(probe, "prepare_manual_boundary", None)
            if callable(prepare_boundary):
                prepare_boundary(float(manual_boundary_timestamp or time.time()))
        stream, preloaded_frames = probe.handoff()
        if not automatic:
            boundary = max(
                0.0,
                float(manual_boundary_timestamp or time.time()),
            )
            set_minimum = getattr(stream, "set_trace_minimum_timestamp", None)
            if callable(set_minimum):
                set_minimum(boundary)
            disable_broad_tcp = getattr(
                stream, "disable_accelerator_tcp_discovery", None
            )
            if callable(disable_broad_tcp):
                disable_broad_tcp()
            preloaded_frames = tuple(
                frame
                for frame in preloaded_frames
                if float(frame[0]) >= boundary
            )
        return stream, tuple(preloaded_frames)

    def _publish_snapshot(
        self,
        collector: LiveDpsCollector,
        source_file: str,
        effect_elapsed_seconds: float,
        generation: int,
    ) -> None:
        # Live refresh only needs aggregate tables and curves. Shipping every
        # raw event once per second grows linearly with battle length and used
        # to make both the UI and capture thread progressively slower.
        snapshot = collector.snapshot(
            effect_elapsed_seconds,
            include_events=False,
            live_clock=True,
        )
        payload = _json_safe(snapshot)
        encounters = tuple(BossRecognizer().recognize_snapshot(snapshot))
        payload["boss_encounters"] = _boss_encounter_payload(
            snapshot, encounters, source_file
        )
        payload["boss_hp_curves"] = _boss_hp_curve_payload(
            snapshot, source_file, encounters
        )
        payload["boss_effect_views"] = _boss_effect_views_payload(
            snapshot, collector.aggregator, encounters
        )
        payload["sourceFile"] = source_file
        # A stable live key lets the rating page request an event-complete
        # in-memory snapshot without hashing a trace file that is still being
        # written.  It is replaced by the immutable file hash when capture
        # stops.
        payload["sourceHash"] = self.live_source_hash()
        payload["selfSourceId"] = self._self_source_id(collector)
        if self.payload_enricher is not None:
            payload = self.payload_enricher(payload)
        with self._lock:
            if (
                generation != self._page_projection_generation
                or collector is not self._active_collector
            ):
                return
            self._state["snapshot"] = payload
            self._state["snapshotPublishedMonotonic"] = time.monotonic()
            self._state["pageProjectionError"] = ""

    def _queue_page_snapshot(
        self,
        collector: LiveDpsCollector,
        source_file: str,
        effect_elapsed_seconds: float,
    ) -> None:
        """Replace any obsolete full-page refresh with the newest request."""

        with self._lock:
            self._page_projection_pending = (
                self._page_projection_generation,
                collector,
                source_file,
                max(0.0, float(effect_elapsed_seconds)),
            )
        self._page_projection_wake.set()

    def _page_projection_loop(self) -> None:
        """Build full live-page snapshots without stalling packet polling."""

        set_current_thread_low_priority()
        while not self._page_projection_stop.is_set():
            self._page_projection_wake.wait(0.5)
            self._page_projection_wake.clear()
            while not self._page_projection_stop.is_set():
                with self._lock:
                    pending = self._page_projection_pending
                    self._page_projection_pending = None
                if pending is None:
                    break
                generation, collector, source_file, effect_elapsed = pending
                try:
                    # The live page is an advisory projection.  CPython's GIL
                    # gives each individual container operation a consistent
                    # view; if a dictionary changes during iteration, discard
                    # this projection and let the already queued latest request
                    # retry.  Final scoring still uses the locked frozen snapshot.
                    self._publish_snapshot(
                        collector,
                        source_file,
                        effect_elapsed,
                        generation,
                    )
                except Exception as exc:
                    with self._lock:
                        if generation == self._page_projection_generation:
                            self._state["pageProjectionError"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                    continue

    def _publish_overlay_snapshot(
        self,
        collector: LiveDpsCollector,
        effect_elapsed_seconds: float,
    ) -> None:
        """Queue one latest-only incremental overlay projection."""

        with self._lock:
            generation = self._overlay_projection_generation
            revision = int(collector.aggregator.revision)
            pending = self._overlay_projection_pending
            if (
                pending is not None
                and pending[0] == generation
                and (
                    pending[3] > revision
                    or (
                        pending[3] == revision
                        and pending[2] >= effect_elapsed_seconds
                    )
                )
            ):
                return
            self._overlay_projection_pending = (
                generation,
                collector,
                max(0.0, float(effect_elapsed_seconds)),
                revision,
            )
        self._overlay_projection_wake.set()

    def _overlay_projection_loop(self) -> None:
        """Build only the newest requested overlay state off the capture loop."""

        # This projection is latest-only and normally takes only a few dozen
        # milliseconds.  Keeping it permanently below-normal made it starve
        # behind the game/DWM when several transparent overlays were open;
        # during that starvation it also held ``_collector_lock``, delaying
        # every Buff/Debuff edge and the live DPS curve together.
        while not self._overlay_projection_stop.is_set():
            self._overlay_projection_wake.wait(0.25)
            self._overlay_projection_wake.clear()
            while not self._overlay_projection_stop.is_set():
                with self._lock:
                    pending = self._overlay_projection_pending
                    self._overlay_projection_pending = None
                if pending is None:
                    break
                generation, collector, effect_elapsed, requested_revision = pending
                started = time.perf_counter()
                try:
                    with self._collector_lock:
                        acquired = time.perf_counter()
                        freeze = getattr(collector, "freeze_live_overlay", None)
                        frozen = freeze(effect_elapsed) if callable(freeze) else None
                        if frozen is None:
                            snapshot_reader = getattr(collector, "live_overlay_snapshot", collector.overlay_snapshot)
                            payload = snapshot_reader(effect_elapsed)
                        self_source_id = self._self_source_id(collector)
                        source = collector.source
                        copied = time.perf_counter()
                    if frozen is not None:
                        payload = frozen.overlay_snapshot(
                            max(0, round(effect_elapsed * 1000)), refresh_composition=False,
                        )
                    payload["selfSourceId"] = self_source_id
                    with self._lock:
                        payload["sourceFile"] = str(
                            self._state.get("sourceFile") or ""
                        )
                    payload["sourceHash"] = self.live_source_hash()
                    latency_reader = getattr(source, "capture_latency_sample", None)
                    latency = (
                        dict(latency_reader())
                        if callable(latency_reader)
                        else {}
                    )
                    with self._lock:
                        latency["statisticsPipeline"] = dict(
                            self._state.get("statisticsPipeline") or {}
                        )
                        latency["historyWriter"] = dict(
                            self._state.get("historyWriter") or {}
                        )
                        latency["identityMaintenance"] = dict(
                            self._state.get("identityMaintenance") or {}
                        )
                except Exception as exc:
                    with self._lock:
                        if generation == self._overlay_projection_generation:
                            self._state["overlayLatency"] = {
                                "error": f"{type(exc).__name__}: {exc}"
                            }
                    continue

                published_monotonic = time.monotonic()
                projection_ms = (time.perf_counter() - started) * 1000.0
                decoded_monotonic = as_number(
                    latency.get("decodedFrameMonotonic")
                )
                raw_timestamp = as_number(latency.get("rawFrameTimestamp"))
                raw_revision = int(latency.get("rawFrameRevision") or 0)
                pipeline_advanced = (
                    raw_revision > 0
                    and raw_revision != self._overlay_latency_last_raw_revision
                )
                if pipeline_advanced:
                    self._overlay_latency_last_raw_revision = raw_revision
                decode_to_publish_ms = (
                    max(0.0, (published_monotonic - decoded_monotonic) * 1000.0)
                    if pipeline_advanced and decoded_monotonic > 0.0
                    else 0.0
                )
                capture_to_publish_ms = (
                    max(0.0, (time.time() - raw_timestamp) * 1000.0)
                    if pipeline_advanced and raw_timestamp > 0.0
                    else 0.0
                )
                diagnostics = {
                    **latency,
                    "snapshotBuildMs": round(projection_ms, 3),
                    "snapshotLockWaitMs": round((acquired - started) * 1000, 3),
                    "snapshotLockHeldMs": round((copied - acquired) * 1000, 3),
                    "snapshotOutsideLockMs": round((time.perf_counter() - copied) * 1000, 3),
                    "decodeToOverlayPublishMs": round(
                        decode_to_publish_ms, 3
                    ),
                    "captureToOverlayPublishMs": round(
                        capture_to_publish_ms, 3
                    ),
                    "pipelineAdvanced": pipeline_advanced,
                    "thresholdExceeded": max(
                        projection_ms,
                        decode_to_publish_ms,
                        capture_to_publish_ms,
                        as_number(latency.get("captureToDecodeMs")),
                        as_number(latency.get("decoderQueueDelayMs")),
                    )
                    > 500.0,
                }
                with self._lock:
                    if (
                        generation != self._overlay_projection_generation
                        or collector is not self._active_collector
                    ):
                        continue
                    self._overlay_projection_revision += 1
                    self._state["overlaySnapshot"] = payload
                    self._state["overlaySnapshotPublishedMonotonic"] = (
                        published_monotonic
                    )
                    self._state["overlaySnapshotRevision"] = (
                        self._overlay_projection_revision
                    )
                    self._state["overlayLatency"] = diagnostics
                self._set_overlay_buff_published_revision(
                    int(payload.get("player_buff_state_revision") or 0)
                )
                if diagnostics["thresholdExceeded"]:
                    self._record_overlay_latency(diagnostics)

    def _record_overlay_latency(self, diagnostics: dict[str, Any]) -> None:
        """Rate-limit actionable >500 ms pipeline diagnostics to JSONL."""

        now = time.monotonic()
        # Avoid opening an append handle every second during the exact busy
        # periods being measured. Keep the first sample, then one per 10 s.
        if now - self._overlay_latency_last_logged < 10.0:
            return
        self._overlay_latency_last_logged = now
        try:
            self._overlay_latency_log_path.parent.mkdir(
                parents=True, exist_ok=True
            )
            row = {
                "recordedAt": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                **diagnostics,
            }
            with self._overlay_latency_log_path.open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
        except OSError:
            pass

    def _record_capture_start_stage(
        self,
        key: str,
        requested_monotonic: float,
    ) -> None:
        """Record one startup milestone without delaying packet capture."""

        elapsed_ms = round(
            max(0.0, time.monotonic() - float(requested_monotonic)) * 1000.0,
            3,
        )
        with self._lock:
            diagnostics = dict(self._state.get("startDiagnostics") or {})
            diagnostics[str(key)] = elapsed_ms
            self._state["startDiagnostics"] = diagnostics

    def _run(
        self,
        duration: float,
        handoff: tuple[Any, tuple[tuple[float, bytes, int, int], ...]] | None,
        automatic: bool,
        discover_tcp: bool,
        pending_probe: tuple[int, V576AutoCaptureProbe] | None = None,
        requested_at: float = 0.0,
        requested_monotonic: float = 0.0,
    ) -> None:
        # Keep startup at the default priority.  Process/port lookup and the
        # first decoder construction are bounded foreground setup: lowering
        # the thread before these steps made the UI remain on “正在查找游戏端口”
        # for 10-20 seconds while the game was busy.  The long-running poll
        # loop is lowered only after capture is fully ready below.
        collector: LiveDpsCollector | None = None
        source_file = ""
        final_source_hash = ""
        handoff_consumed = False
        automatic_stop_reason = ""
        history_snapshot = None
        maintenance_stop = threading.Event()
        maintenance_thread = None
        handoff_pid = pending_probe[0] if pending_probe is not None else 0
        with self._lock:
            self._prebattle_formal_handoff = False
        try:
            self._record_capture_start_stage(
                "workerStartedMs", requested_monotonic
            )
            if pending_probe is not None:
                probe_pid, probe = pending_probe
                self._ingest_probe_identities(probe_pid, probe)
                self._drain_probe_profiles(probe_pid, probe)
                handoff = self._handoff_probe_for_capture(
                    probe,
                    automatic=automatic,
                    manual_boundary_timestamp=requested_at,
                )
                self._ingest_probe_identities(probe_pid, probe)
                self._drain_probe_profiles(probe_pid, probe)
                pending_probe = None
            self._record_capture_start_stage(
                "handoffCompletedMs", requested_monotonic
            )
            pid, process_name = _windows_process_id(("dragonnest_reborn.exe", "dragonnest.exe"))
            if pid <= 0:
                raise RuntimeError("未找到游戏进程 dragonnest_reborn.exe 或 dragonnest.exe")
            self._record_capture_start_stage(
                "processResolvedMs", requested_monotonic
            )
            self._set_timeline_game_runtime_state(True, True)
            self._activate_identity_cache_pid(pid)
            # The transferred probe already owns a live endpoint filter.
            # Do not repeat blocking OS enumeration on the handoff path;
            # the source's dedicated port worker will refresh it immediately.
            transferred_stream = (
                handoff[0] if handoff is not None and handoff_pid == pid else None
            )
            udp_ports = list(getattr(transferred_stream, "udp_ports", ()) or ())
            tcp_ports = list(getattr(transferred_stream, "tcp_ports", ()) or ())
            if not udp_ports and not tcp_ports:
                udp_ports = _windows_transport_ports(pid, "udp")
                tcp_ports = _windows_transport_ports(pid, "tcp")
            if not udp_ports and not tcp_ports and not discover_tcp:
                raise RuntimeError("未找到游戏 TCP/UDP 端口，请确认角色已登录")
            self._record_capture_start_stage(
                "portsResolvedMs", requested_monotonic
            )
            # Publish the proven endpoint immediately instead of making the
            # frontend wait for catalog loading and decoder construction.  A
            # user can now distinguish “port found, decoder starting” from a
            # real endpoint-discovery failure.
            with self._lock:
                self._state.update(
                    status=(
                        "已获取游戏端口，正在初始化实时解码"
                        f"（UDP {len(udp_ports)} / TCP {len(tcp_ports)}）"
                    ),
                    process=process_name,
                    pid=pid,
                    udpPorts=udp_ports,
                    tcpPorts=tcp_ports,
                )
            collector = LiveDpsCollector(self.profile_path)
            self._record_capture_start_stage(
                "collectorConstructedMs", requested_monotonic
            )
            def current_ports() -> tuple[list[int], list[int]]:
                return (
                    _windows_transport_ports(pid, "udp"),
                    _windows_transport_ports(pid, "tcp"),
                )

            with self._lock:
                self._prebattle_formal_handoff = True
            collector.start_npcap(
                udp_ports,
                tcp_ports,
                port_provider=current_ports,
                stream=handoff[0] if handoff is not None else None,
                preloaded_frames=handoff[1] if handoff is not None else (),
                known_actor_names=self._cached_actor_names(pid),
                identity_capture_frames=(),
                session_evidence_frames=(
                    self._cached_session_evidence_frames(pid)
                ),
                prebattle_state=(
                    # With no transferable automatic stream, formal capture
                    # starts now, not when process/catalog setup was requested.
                    # The idle observer stays active through that setup gap.
                    self._idle_prebattle_snapshot(pid, time.time())
                    if handoff is None else None
                ),
                prebattle_tracker=self._idle_prebattle_tracker(pid),
                toggles=self._toggles,
                # 进本后才开统计：用探针的 Boss 档案把实体接上。
                seed_monster_entities=self._idle_monster_entity_seeds(pid),
            )
            self._record_capture_start_stage(
                "collectorReadyMs", requested_monotonic
            )
            with self._collector_lock:
                self._active_collector = collector
                self._active_effect_elapsed_seconds = 0.0
                self._active_effect_clock_monotonic = time.monotonic()
                self._voice_clock_running = True
            maintenance_thread = threading.Thread(
                target=self._capture_maintenance_loop,
                args=(pid, collector, collector.source, maintenance_stop),
                name="DragonNestCaptureMetadata",
                daemon=True,
            )
            maintenance_thread.start()
            handoff_consumed = handoff is not None
            source_file = str(getattr(collector.source, "diagnostic_trace_path", ""))
            capture_stream = getattr(collector.source, "_stream", None)
            if discover_tcp and capture_stream is not None:
                # “从游戏识别” is armed before entering the character.  Open
                # a bounded TCP discovery window immediately so an
                # accelerator-rewritten login profile cannot be filtered out
                # before the one-second process-port refresh learns it.
                enable_tcp_discovery = getattr(
                    capture_stream, "enable_accelerator_tcp_discovery", None
                )
                if callable(enable_tcp_discovery):
                    enable_tcp_discovery(45.0)
            adapter_count = int(getattr(capture_stream, "adapter_count", 0) or 0)
            accelerator_adapter_count = int(
                getattr(capture_stream, "accelerator_adapter_count", 0) or 0
            )
            with self._lock:
                self._state.update(
                    state="running",
                    status=(
                        f"已检测到副本进图，Npcap 自动统计中（{adapter_count}张网卡）"
                        if automatic
                        else f"Npcap 实时统计中（{adapter_count}张网卡）"
                    ),
                    process=process_name,
                    pid=pid, udpPorts=udp_ports, tcpPorts=tcp_ports, sourceFile=source_file,
                    adapterCount=adapter_count,
                    acceleratorAdapterCount=accelerator_adapter_count,
                    acceleratorDiscovery=False,
                    scoreReady=True,
                    scoreSourceHash=self.live_source_hash(),
                )
            self._record_capture_start_stage(
                "stateRunningMs", requested_monotonic
            )
            # Decoder and capture are now initialized.  The controller may run
            # below the game from here on; the tiny Npcap driver-drain threads
            # deliberately remain at normal priority so raw packets stay live.
            set_current_thread_low_priority()
            deadline = time.monotonic() + duration if duration > 0 else 0.0
            last_snapshot = 0.0
            last_overlay_snapshot = 0.0
            last_overlay_buff_state_revision = int(
                getattr(
                    collector.aggregator,
                    "overlay_player_buff_state_revision",
                    0,
                )
                or 0
            )
            last_timeline_snapshot = 0.0
            # Statuses, casts, the live timeline and the overlay all share one
            # capture-relative clock. Automatic capture has already trimmed
            # the decoder input to the formal-dungeon entry boundary, so that
            # boundary is 0s. Wall time advances the clock during quiet
            # periods; decoded events may move it forward but never backward.
            effect_clock_anchor_monotonic = 0.0
            effect_clock_anchor_seconds = 0.0
            effect_clock_last_event_ms = -1
            # Safe dungeon-return detection is independent from the background
            # automatic-start switch. A manually started recording should
            # still stop after a verified return to Heaven/Garden; manual Stop
            # remains available and keeps its normal restart pause.
            auto_stop_detector = V576AutoCaptureStopDetector()
            stage_end_announced = False
            # Catch up decoded events without enlarging the ingest critical
            # section. Idle cadence and projection refresh rules stay unchanged.
            catch_up_policy = LiveCatchUpPolicy(enabled=True)
            poll_wait_seconds = catch_up_policy.normal_seconds
            pipeline_max_ms: dict[str, float] = {}
            while not self._stop.wait(poll_wait_seconds):
                poll_work_started = time.monotonic()
                events = collector.read_events()
                read_finished = time.monotonic()
                with self._collector_lock:
                    apply_started = time.monotonic()
                    collector.apply_events(events)
                apply_finished = time.monotonic()
                collector.archive_events(events)
                archive_finished = time.monotonic()
                # Identity/profile persistence runs on the session worker.
                # Only the inexpensive decoded map observation stays here.
                detected_nest = self._detected_nest(collector)
                self._refresh_skill_identity_state(collector)
                # 待机不抓包 → 进本那一刻的场景包错过了，此时场景识别要么为空、
                # 要么识别成城镇/未配置地图。只要当前这行还不是「受支持的副本」，
                # 就用「我正在打的 Boss」反推。已识别成受支持副本就跳过，别白算。
                current_nest_key = str(self._state.get("nestKey") or "")
                if current_nest_key not in known_nest_keys():
                    detected_inferred = self._inferred_nest(collector)
                    if detected_inferred:
                        detected_nest = detected_inferred
                identity_finished = time.monotonic()
                for name, seconds in (
                    ("sourceReadMs", read_finished - poll_work_started),
                    ("statisticsLockWaitMs", apply_started - read_finished),
                    ("statisticsApplyMs", apply_finished - apply_started),
                    ("historyEnqueueMs", archive_finished - apply_finished),
                    ("nestObservationMs", identity_finished - archive_finished),
                ):
                    pipeline_max_ms[name] = max(
                        pipeline_max_ms.get(name, 0.0), seconds * 1000,
                    )
                if detected_nest:
                    with self._lock:
                        current_nest_key = str(
                            self._state.get("nestKey") or ""
                        )
                        # 地图精确识别优先于「按攻击目标反推」。
                        # 精确值的 key 是 ``map:14514`` 这种地图级号，进不了
                        # ``known_nest_keys()``，于是原来那句「当前 key 不认识
                        # 就允许覆盖」每秒都成立 —— 推断值反复顶掉精确值，
                        # nestLabel 在「黑龙巢穴（三）」和「黑龙经典」之间来回跳，
                        # 前端把每次名字变化都当成换副本并重置所有时钟，
                        # 表现为倒计时闪一下就没、播报丢失。
                        current_exact = (
                            str(self._state.get("nestConfidence") or "")
                            == "exact_map_id"
                            and int(self._state.get("nestMapId", 0) or 0) > 0
                        )
                        incoming_inferred = (
                            str(detected_nest.get("nestConfidence") or "")
                            == "inferred"
                        )
                        if (not (current_exact and incoming_inferred)) and (
                            not current_nest_key
                            # 之前是按目标反推的（confidence=inferred），
                            # 真正的场景识别一到就让它接班，反过来则不允许。
                            or self._state.get("nestConfidence")
                            == "inferred"
                            # 当前是城镇 / 未配置地图：不算识别成功，允许覆盖。
                            or current_nest_key not in known_nest_keys()
                            or (
                                detected_nest.get("nestConfidence")
                                == "exact_map_id"
                                and (
                                    self._state.get("nestConfidence")
                                    != "exact_map_id"
                                    or int(
                                        self._state.get("nestMapId", 0) or 0
                                    )
                                    != int(
                                        detected_nest.get("nestMapId", 0) or 0
                                    )
                                )
                            )
                        ):
                            self._state.update(detected_nest)
                now = time.monotonic()
                capture_clock_sample = getattr(
                    collector.source, "capture_clock_sample", None
                )
                if callable(capture_clock_sample):
                    event_revision, capture_clock_seconds = (
                        capture_clock_sample()
                    )
                    event_clock_ms = max(
                        0, round(float(capture_clock_seconds) * 1000)
                    )
                else:
                    # Compatibility for memory/test sources which do not own
                    # raw Npcap frames.
                    event_clock_ms = int(
                        getattr(collector.aggregator, "_last_ms", 0) or 0
                    )
                    event_revision = int(
                        getattr(collector.aggregator, "revision", 0) or 0
                    )
                (
                    effect_clock_anchor_monotonic,
                    effect_clock_anchor_seconds,
                    effect_clock_last_event_ms,
                    effect_elapsed,
                ) = _advance_live_capture_clock(
                    now=now,
                    event_clock_ms=event_clock_ms,
                    event_revision=event_revision,
                    anchor_monotonic=effect_clock_anchor_monotonic,
                    anchor_seconds=effect_clock_anchor_seconds,
                    last_event_ms=effect_clock_last_event_ms,
                )
                with self._collector_lock:
                    overlay_buff_state_revision = int(
                        getattr(
                            collector.aggregator,
                            "overlay_player_buff_state_revision",
                            0,
                        )
                        or 0
                    )
                    if auto_stop_detector is not None:
                        latency_sample = getattr(
                            collector.source, "capture_latency_sample", None
                        )
                        decode_backlog_seconds = 0.0
                        if callable(latency_sample):
                            capture_lag = latency_sample()
                            decode_backlog_seconds = max(
                                0.0,
                                float(capture_lag.get("decoderQueueDelayMs", 0.0) or 0.0) / 1000.0,
                                float(capture_lag.get("npcapReaderLagMs", 0.0) or 0.0) / 1000.0,
                            )
                        automatic_stop_reason = auto_stop_detector.evaluate(
                            collector.aggregator,
                            effect_elapsed,
                            decoder=getattr(
                                collector.source,
                                "_decoder",
                                None,
                            ),
                            decode_backlog_seconds=decode_backlog_seconds,
                        )
                    stage_end_seconds = (
                        auto_stop_detector.pending_stage_end_seconds
                        if auto_stop_detector is not None else None
                    )
                    # NestEnd is the visible end of the fight. Keep the raw
                    # capture running until formal settlement (or a verified
                    # exit after the result window), without extending the
                    # displayed fight clock through the chest delay.
                    if stage_end_seconds is not None:
                        effect_elapsed = min(effect_elapsed, stage_end_seconds)
                    self._active_effect_elapsed_seconds = effect_elapsed
                    self._active_effect_clock_monotonic = time.monotonic()
                    self._voice_clock_running = stage_end_seconds is None
                if (
                    stage_end_seconds is not None
                    and not stage_end_announced
                    and not automatic_stop_reason
                ):
                    stage_end_announced = True
                    with self._lock:
                        self._state.update(
                            # The fight clock has stopped, but Npcap must
                            # remain active until this encounter's formal
                            # result is decoded. Publishing "stopping" here
                            # made the UI treat a still-live capture as ended.
                            state="running",
                            status="已检测到关卡结束，正在收取正式结算包",
                            scoreReady=False,
                            scoreSourceHash="",
                        )
                if automatic_stop_reason:
                    with self._lock:
                        self._state.update(
                            state="stopping",
                            status=f"{automatic_stop_reason}，正在自动整理记录",
                            scoreReady=False,
                            scoreSourceHash="",
                        )
                    break
                with self._lock:
                    refresh_latency = dict(
                        self._state.get("overlayLatency") or {}
                    )
                # Use the current decoder watermark, not only the last
                # completed overlay (which may itself be several seconds old).
                if auto_stop_detector is not None and callable(latency_sample):
                    refresh_latency.update(capture_lag)
                (
                    timeline_refresh_seconds,
                    overlay_refresh_seconds,
                    status_refresh_seconds,
                ) = _live_refresh_intervals(refresh_latency)
                if now - last_timeline_snapshot >= timeline_refresh_seconds:
                    self._queue_timeline_projection(collector, effect_elapsed)
                    last_timeline_snapshot = now
                if (
                    overlay_buff_state_revision
                    != last_overlay_buff_state_revision
                    and (
                        overlay_refresh_seconds <= 0.5
                        or now - last_overlay_snapshot >= overlay_refresh_seconds
                    )
                ):
                    # Publish lifecycle edges immediately when caught up.
                    # During backlog, coalesce them at the bounded refresh
                    # cadence instead of rebuilding on every historical buff.
                    # Voice signals have their own independent decoder feed.
                    self._publish_overlay_snapshot(collector, effect_elapsed)
                    last_overlay_snapshot = now
                    last_overlay_buff_state_revision = (
                        overlay_buff_state_revision
                    )
                elif now - last_overlay_snapshot >= overlay_refresh_seconds:
                    self._publish_overlay_snapshot(collector, effect_elapsed)
                    last_overlay_snapshot = now
                if now - last_snapshot >= status_refresh_seconds:
                    with self._lock:
                        self._state["statisticsPipeline"] = {
                            "sampleMonotonic": time.monotonic(),
                            "kind": "maximum_since_previous_status_sample",
                            "adaptiveCatchUpEnabled": catch_up_policy.enabled,
                            "pollWaitMs": round(poll_wait_seconds * 1000, 3),
                            **{key: round(value, 3) for key, value in pipeline_max_ms.items()},
                        }
                    pipeline_max_ms.clear()
                    history_diagnostics = getattr(collector.history, "diagnostics", None)
                    if callable(history_diagnostics):
                        with self._lock:
                            self._state["historyWriter"] = history_diagnostics()
                    adapter_count = int(
                        getattr(capture_stream, "adapter_count", 0) or 0
                    )
                    accelerator_adapter_count = int(
                        getattr(capture_stream, "accelerator_adapter_count", 0)
                        or 0
                    )
                    discovery = bool(
                        getattr(capture_stream, "accelerator_discovery_active", False)
                    )
                    with self._lock:
                        self._state.update(
                            adapterCount=adapter_count,
                            acceleratorAdapterCount=accelerator_adapter_count,
                            acceleratorDiscovery=discovery,
                            status=(
                                "实时解码暂存异常，原始抓包继续保存；结束后重建统计"
                                if getattr(capture_stream, "_decoder_replay_required", "")
                                else
                                "已检测到关卡结束，正在等待正式结算包"
                                if stage_end_seconds is not None
                                else f"加速器兼容探测中（{adapter_count}张网卡）"
                                if discovery
                                else (
                                    f"已检测到副本进图，Npcap 自动统计中（{adapter_count}张网卡）"
                                    if automatic
                                    else f"Npcap 实时统计中（{adapter_count}张网卡）"
                                )
                            ),
                        )
                    # The main page consumes the same bounded incremental
                    # projection as the overlay while capture is active.  A
                    # full historical page is built exactly once after Stop;
                    # rebuilding and serializing it every second made Stop
                    # requests sit behind minutes of stale HTTP work.
                    last_snapshot = now
                if deadline and now >= deadline:
                    break
                backlog = getattr(collector.source, "decoded_event_backlog", None)
                poll_wait_seconds = catch_up_policy.next_wait(
                    backlog() if callable(backlog) else 0,
                    time.monotonic() - poll_work_started,
                )
            stop_loop_exited = time.monotonic()
            with self._lock:
                stop_requested = as_number(
                    (self._state.get("stopDiagnostics") or {}).get(
                        "requestedMonotonic"
                    )
                )
                stop_diagnostics = dict(
                    self._state.get("stopDiagnostics") or {}
                )
                stop_diagnostics["captureLoopExitMs"] = round(
                    max(0.0, stop_loop_exited - stop_requested) * 1000.0,
                    3,
                ) if stop_requested > 0.0 else 0.0
                self._state["stopDiagnostics"] = stop_diagnostics
            metadata_stop_started = time.monotonic()
            # Never join while holding the statistics lock: the worker takes
            # it briefly to copy immutable profile references / bind identities.
            maintenance_stop.set()
            collector.request_stop()
            maintenance_thread.join()
            drain_started = time.monotonic()
            with self._collector_lock:
                with self._lock:
                    self._page_projection_generation += 1
                    self._page_projection_pending = None
                self_source_id = self._self_source_id(collector)
                # Drain packets first, then build one exact final snapshot.
                # collector.stop() historically built another complete
                # snapshot for SQLite before this one, doubling Stop time.
                final_capture_source = collector.source
                stage_identity_bindings = getattr(
                    capture_stream, "set_identity_trace_bindings", None
                )
                if callable(stage_identity_bindings):
                    stage_identity_bindings(self._cached_actor_names(pid))
                    source_identity_bindings = getattr(
                        final_capture_source,
                        "identity_bindings_snapshot",
                        None,
                    )
                    if callable(source_identity_bindings):
                        stage_identity_bindings(source_identity_bindings())
                stage_profile_bindings = getattr(
                    capture_stream, "set_profile_identity_bindings", None
                )
                if callable(stage_profile_bindings):
                    stage_profile_bindings(
                        self._cached_profile_identity_bindings(pid)
                    )
                stage_session_evidence = getattr(
                    capture_stream,
                    "set_session_evidence_trace_frames",
                    None,
                )
                if callable(stage_session_evidence):
                    stage_session_evidence(
                        self._cached_session_evidence_frames(pid)
                    )
                collector.stop(finalize_history=False)
                self._ingestion_released.set()
                replay_reason = str(getattr(capture_stream, "_decoder_replay_required", "") or "")
                if replay_reason:
                    # Exceptional decoder spool failure does not cancel RAW.
                    # Never score the partial live view: rebuild after capture,
                    # with no extra work in the overlay's normal update path.
                    collector.aggregator, final_capture_source = _recover_decode_spool_capture(
                        collector, source_file, self.profile_path.parents[1],
                    )
            decoder_drained_at = time.monotonic()
            # Final drain can reveal identity/profile events. Persist them
            # outside the statistics lock after the metadata worker has joined.
            self._ingest_source_identities(pid, final_capture_source)
            self._publish_collector_teammate_info(collector)
            with self._collector_lock:
                # Stop drains the decoder queue and stale TCP tail. The exact
                # map confirmation can be decoded only during that drain, so
                # refresh the immutable nest evidence before automatic score
                # registration. Previously the UI/upload kept the earlier
                # monster-template fallback (for example 黑龙经典) even when
                # the completed trace proved 黑龙一/二.
                final_detected_nest = self._detected_nest_from_decoder(
                    getattr(final_capture_source, "_decoder", None)
                )
                if final_detected_nest:
                    with self._lock:
                        self._state.update(final_detected_nest)
                drained_at = time.monotonic()
                snapshot = collector.snapshot(include_events=True)
                reconnect_normalize_started = time.monotonic()
                (
                    snapshot,
                    final_aggregator,
                    reconnect_normalize_diagnostics,
                ) = self._normalize_stopped_reconnect_snapshot(
                    collector,
                    final_capture_source,
                    source_file,
                    snapshot,
                )
                final_decoder = getattr(final_capture_source, "_decoder", None)
                final_map_history = tuple(
                    dict(row)
                    for row in getattr(
                        final_decoder, "detected_map_history", ()
                    )
                    if isinstance(row, dict)
                )
                if final_map_history:
                    final_aggregator._replay_map_history = final_map_history
                collector.aggregator = final_aggregator
                if reconnect_normalize_diagnostics.get("applied"):
                    aliases = reconnect_normalize_diagnostics.get(
                        "visibleAliases", {}
                    )
                    if isinstance(aliases, dict):
                        seen_source_ids: set[str] = set()
                        while (
                            self_source_id in aliases
                            and self_source_id not in seen_source_ids
                        ):
                            seen_source_ids.add(self_source_id)
                            self_source_id = str(
                                aliases.get(self_source_id) or self_source_id
                            )
                frozen_at = time.monotonic()
                self._active_collector = None
            with self._lock:
                stop_diagnostics = dict(
                    self._state.get("stopDiagnostics") or {}
                )
                stop_diagnostics.update(
                    metadataStopWaitMs=round(
                        max(0.0, drain_started - metadata_stop_started) * 1000.0, 3
                    ),
                    decoderDrainMs=round(
                        max(0.0, decoder_drained_at - drain_started) * 1000.0, 3
                    ),
                    identityFinalizationMs=round(
                        max(0.0, drained_at - decoder_drained_at) * 1000.0, 3
                    ),
                    frozenSnapshotMs=round(
                        max(
                            0.0,
                            reconnect_normalize_started - drained_at,
                        )
                        * 1000.0,
                        3,
                    ),
                    reconnectNormalizeMs=round(
                        max(
                            0.0,
                            frozen_at - reconnect_normalize_started,
                        )
                        * 1000.0,
                        3,
                    ),
                    reconnectNormalize=reconnect_normalize_diagnostics,
                )
                self._state["stopDiagnostics"] = stop_diagnostics
            # Npcap hashes the exact bytes while writing them.  Re-reading a
            # multi-GB trace here used to keep the score button disabled long
            # after all events were already frozen.
            final_source_hash = str(
                getattr(capture_stream, "trace_content_hash", "") or ""
            )
            if (
                not final_source_hash
                and source_file
                and Path(source_file).is_file()
            ):
                final_source_hash = capture_content_hash(Path(source_file))
            # Packet ingestion is over before score readiness is published.
            # Switch budgets here so an immediately queued score does not
            # inherit the two-worker/3-GiB live-combat restriction.
            score_ready_pid, _score_ready_process = _windows_process_id(
                ("dragonnest_reborn.exe", "dragonnest.exe")
            )
            self._set_timeline_game_runtime_state(score_ready_pid > 0, False)
            # Scoring needs only the frozen object and immutable file key.
            # Publish both and report Stop complete before the browser-facing
            # projection starts.
            final_status = (
                f"{automatic_stop_reason}，已自动停止"
                if automatic_stop_reason
                else "实时统计已停止"
            )
            integrity = dict(getattr(capture_stream, "capture_integrity", {}) or {})
            with self._lock:
                self._state["captureIntegrity"] = integrity
            if integrity.get("driverDroppedDelta") or integrity.get("interfaceDroppedDelta") or integrity.get("truncatedFrames"):
                final_status += "；检测到采集缺失，本场伤害与评分可能不完整"
            self._publish_frozen_score_ready(
                snapshot,
                source_file,
                final_source_hash,
                final_status,
                aggregator=collector.aggregator,
            )
            self._start_final_page_projection(
                snapshot,
                source_file,
                final_source_hash,
                self_source_id,
                collector.aggregator,
                final_status,
            )
            record_usage_event("capture_success")
            history_snapshot = snapshot
        except Exception as exc:
            record_usage_event("capture_failure")
            with self._lock:
                self._state.update(
                    state="error",
                    status="实时抓取失败",
                    error=f"{type(exc).__name__}: {exc}",
                    acceleratorDiscovery=False,
                    scoreReady=False,
                    scoreSourceHash="",
                )
        finally:
            maintenance_stop.set()
            if maintenance_thread is not None and maintenance_thread.ident is not None:
                maintenance_thread.join()
            with self._collector_lock:
                if self._active_collector is collector:
                    self._active_collector = None
            if collector is not None and history_snapshot is None:
                try:
                    # Detached above: disk drain/close must not hold the lock
                    # needed by a subsequent capture or an overlay request.
                    collector.close()
                except Exception:
                    pass
            if handoff is not None and not handoff_consumed:
                try:
                    handoff[0].close()
                except Exception:
                    pass
            if pending_probe is not None:
                try:
                    pending_probe[1].close()
                except Exception:
                    pass
            if automatic or automatic_stop_reason:
                # A confirmed dungeon completion should return to the light
                # probe immediately so a fast re-entry into a formal dungeon
                # is not missed. Manual Stop keeps the anti-restart pause.
                self._auto_paused_until = time.monotonic() + (
                    1.0 if automatic_stop_reason else 30.0
                )
            try:
                current_pid, _current_process = _windows_process_id(
                    ("dragonnest_reborn.exe", "dragonnest.exe")
                )
                # The game may still be logged in, but packet ingestion has
                # ended. Release the live-combat 3 GiB reserve before
                # post-capture scoring.
                self._set_timeline_game_runtime_state(current_pid > 0, False)
                # Do not physically split the immutable .dpscap.  The frozen
                # live aggregator is persisted by the replay-cache callback as
                # a compact, directly loadable sidecar.  This keeps capture
                # stop constant-time and avoids duplicating raw packet bytes.
            finally:
                post_capture_splitter.capture_finished()

        if collector is not None and history_snapshot is not None:
            self._finish_released_history(collector, history_snapshot, source_file)

    def _finish_released_history(self, collector, snapshot, source_file: str) -> None:
        """Release capture ownership before waiting for the history writer.

        All stream shutdown and global capture cleanup has already completed.
        This thread now owns only the old collector; a new capture can start.
        Late history diagnostics must not overwrite the new session's state.
        """
        owner = threading.current_thread()
        with self._lock:
            generation = self._timeline_session_id
            self._history_threads.add(owner)
            if self._thread is owner:
                self._thread = None
        started = time.monotonic()
        diagnostics = {}
        try:
            collector.finish_history(snapshot)
            diagnostics["historyFinalizeMs"] = round(
                (time.monotonic() - started) * 1000, 3,
            )
        except Exception as exc:
            diagnostics["historyError"] = f"{type(exc).__name__}: {exc}"
        finally:
            # The dedicated writer closes its own SQLite connection. Wait with
            # no collector/global lock: this is no longer the live collector.
            try:
                collector.close()
            except Exception as exc:
                diagnostics["historyCloseError"] = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if (generation == self._timeline_session_id
                        and str(self._state.get("sourceFile") or "") == source_file):
                    self._state["stopDiagnostics"] = {
                        **(self._state.get("stopDiagnostics") or {}), **diagnostics,
                    }
                self._history_threads.discard(owner)

    def request_stop(self) -> dict[str, Any]:
        """Signal capture shutdown without blocking the HTTP/UI thread."""

        self._auto_paused_until = time.monotonic() + 30.0
        thread = self._thread
        requested_at = time.monotonic()
        with self._lock:
            if thread is not None and thread.is_alive():
                self._state.update(
                    state="stopping",
                    status="正在结束统计并整理尾部数据",
                    acceleratorDiscovery=False,
                    scoreReady=False,
                    scoreSourceHash="",
                    stopDiagnostics={
                        "requestedMonotonic": requested_at,
                        "captureLoopExitMs": 0.0,
                        "decoderDrainMs": 0.0,
                        "frozenSnapshotMs": 0.0,
                        "historyFinalizeMs": 0.0,
                    },
                )
        self._stop.set()
        collector = self._active_collector
        if collector is not None:
            try:
                collector.request_stop()
            except Exception:
                pass
        return self.status(include_snapshot=False)

    def stop(self) -> dict[str, Any]:
        self.request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15.0)
        # 技能号 -> 模板 的学习结果在会话结束时落盘：防抖写回只在流量持续时
        # 触发，短会话必须在这里补一刀，否则下次还得重新学。
        try:
            from .skill_identity import save as save_skill_index

            save_skill_index(force=True)
        except Exception:
            pass
        return self.status()

    def clear(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("请先停止实时统计")
        with self._lock:
            self._final_projection_generation += 1
            self._overlay_projection_generation += 1
            self._overlay_projection_pending = None
            self._page_projection_generation += 1
            self._page_projection_pending = None
            self._reset_timeline_locked()
            self._state.update(
                state="idle",
                status="未连接",
                error="",
                sourceFile="",
                snapshot=None,
                snapshotPublishedMonotonic=0.0,
                overlaySnapshot=None,
                overlaySnapshotPublishedMonotonic=0.0,
                overlaySnapshotRevision=0,
                overlayLatency={},
                historyWriter={},
                statisticsPipeline={},
                identityMaintenance={},
                startDiagnostics={},
                stopDiagnostics={},
                scoreReady=False,
                scoreSourceHash="",
                autoScoreJobId="",
                autoScoreStatus="",
                autoScoreError="",
                pageReady=False,
                nestKey="",
                nestLabel="",
                nestName="",
                nestConfidence="",
                nestTemplateId=0,
                nestObservedAt=0.0,
                nestMapId=0,
                nestMapName="",
                nestMapLayout="",
                nestMapActive=False,
                # 地图历史必须和身份字段一起清空。之前只清了 nestName 而保留
                # nestMapHistory，导致上一轮的「黑龙巢穴（一）」会冒充本轮的
                # 识别结果：日志里明明只有神圣天堂，界面上却能看到巢穴名。
                nestMapHistory=[],
                unconfiguredMapIds=[],
            )
            self.snapshot_object = None
        self._set_overlay_buff_published_revision(0)
        with self._collector_lock:
            self._active_collector = None
            self._active_effect_elapsed_seconds = 0.0
        return self.status()

    def close(self) -> None:
        """Stop capture/probes and join the persistent overlay worker."""

        self._auto_stop.set()
        self._inspect_stop.set()
        self.request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15.0)
        auto_thread = self._auto_thread
        if auto_thread is not None and auto_thread.is_alive():
            auto_thread.join(timeout=2.0)
        inspect_thread = self._inspect_thread
        if inspect_thread is not None and inspect_thread.is_alive():
            inspect_thread.join(timeout=2.0)
        with self._auto_probe_lock:
            probe = self._auto_probe
            self._auto_probe = None
            self._auto_probe_pid = 0
        if probe is not None:
            try:
                self._ingest_probe_identities(self._identity_cache_pid, probe)
                self._drain_probe_profiles(self._identity_cache_pid, probe)
                probe.close()
                self._ingest_probe_identities(self._identity_cache_pid, probe)
                self._drain_probe_profiles(self._identity_cache_pid, probe)
            except Exception:
                pass
        with self._lock:
            self._overlay_projection_generation += 1
            self._overlay_projection_pending = None
        self._set_overlay_buff_published_revision(0)
        self._overlay_projection_stop.set()
        with self._overlay_buff_revision_condition:
            self._overlay_buff_revision_condition.notify_all()
        self._overlay_projection_wake.set()
        if self._overlay_projection_thread.is_alive():
            self._overlay_projection_thread.join(timeout=3.0)
        self._page_projection_stop.set()
        self._page_projection_wake.set()
        if self._page_projection_thread.is_alive():
            self._page_projection_thread.join(timeout=3.0)
        self._timeline_projection_stop.set()
        self._timeline_projection_wake.set()
        if self._timeline_projection_thread.is_alive():
            self._timeline_projection_thread.join(timeout=3.0)
        self._set_timeline_game_runtime_state(False, False)
        identity_import_thread = self._identity_import_thread
        if identity_import_thread is not None and identity_import_thread.is_alive():
            identity_import_thread.join(timeout=1.0)
        if self._identity_cloud_bridge is not None:
            self._identity_cloud_bridge.close()
        with self._lock:
            history_threads = tuple(self._history_threads)
        history_deadline = time.monotonic() + 3.0
        for worker in history_threads:
            worker.join(timeout=max(0.0, history_deadline - time.monotonic()))

class NativeJobCancelled(Exception):
    """Raised inside a background calculation after the user cancels it."""

class NativeJobQueue:
    """Small in-process queue used by the web UI for long calculations."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._pending: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._foreground_pending: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._foreground_leases: dict[str, ForegroundLease] = {}
        self._score_pending: dict[str, Callable[[], None]] = {}
        self._score_sequence = 0
        self._closed = False
        self._worker = threading.Thread(
            target=self._run_pending,
            name="v410-heavy-job-worker",
            daemon=True,
        )
        self._worker.start()
        self._foreground_worker = threading.Thread(
            target=self._run_pending, args=(self._foreground_pending,),
            name="v410-replay-reader", daemon=True,
        )
        self._foreground_worker.start()

    def _run_pending(self, pending=None) -> None:
        pending = self._pending if pending is None else pending
        while True:
            callback = pending.get()
            try:
                if callback is None:
                    return
                callback()
            finally:
                pending.task_done()

    def _run_latest_score(self, parent_id: str = "") -> bool:
        """Enter a newer score only at an explicitly safe phase boundary.

        Keep the parent's Python stack/results, not another concurrently running
        score thread. Callers must have drained their process-pool futures and
        must not subsequently depend on mutable shared planner state.
        """
        with self._lock:
            if self._closed:
                return False
            for job_id in list(self._score_pending):
                if self._jobs[job_id].get("done"):
                    self._score_pending.pop(job_id)
            parent = self._jobs.get(parent_id)
            minimum = parent.get("scoreSequence", 0) if parent else -1
            candidates = [
                job_id for job_id in self._score_pending
                if self._jobs[job_id].get("scoreSequence", 0) > minimum
                and not self._jobs[job_id].get("done")
            ]
            if not candidates:
                return False
            selected = max(candidates, key=lambda key: self._jobs[key]["scoreSequence"])
            callback = self._score_pending.pop(selected)
            if parent:
                parent["state"] = "paused"
                parent["priorityWaitJobId"] = selected
        try:
            callback()
        finally:
            if parent:
                with self._lock:
                    parent.pop("priorityWaitJobId", None)
                    parent["state"] = "running"
        return True

    def start(
        self,
        label: str,
        callback: Callable[[Callable[[int, str], None]], Any],
        *,
        kind: str = "calculation",
        dedupe_key: str = "",
    ) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("程序正在退出，不能启动新任务")
        if dedupe_key:
            with self._lock:
                for existing_id, existing in self._jobs.items():
                    if existing.get("dedupeKey") == dedupe_key and not existing.get("done"):
                        return existing_id
        job_id = uuid.uuid4().hex
        with self._lock:
            # The lookup and insertion must be atomic: prefetch and a visible
            # score request can arrive on separate HTTP handler threads.
            if self._closed:
                raise RuntimeError("程序正在退出，不能启动新任务")
            if dedupe_key:
                for existing_id, existing in self._jobs.items():
                    if existing.get("dedupeKey") == dedupe_key and not existing.get("done"):
                        return existing_id
            self._jobs[job_id] = {
                "done": False,
                "state": "queued",
                "cancelRequested": False,
                "progress": 0,
                "label": label,
                "result": None,
                "error": "",
                "kind": kind,
                "dedupeKey": dedupe_key,
                "createdAt": time.time(),
            }
            if kind == "replay-score":
                self._score_sequence += 1
                self._jobs[job_id]["scoreSequence"] = self._score_sequence

        def progress(value: int, message: str) -> None:
            score_checkpoint(force=True)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.get("cancelRequested"):
                    raise NativeJobCancelled("任务已取消")
                # A background job is one user-visible unit of work.  Nested
                # calculation phases historically reused their own local
                # ranges and could therefore move 88% back to 64%, making the
                # bar appear stalled or broken.  Preserve label updates but
                # never let an in-flight job's completed-work estimate rewind.
                next_progress = max(0, min(100, int(value)))
                job["progress"] = max(
                    int(job.get("progress") or 0),
                    next_progress,
                )
                job["label"] = str(message or label)

        def check_cancel() -> None:
            with self._lock:
                if self._jobs[job_id].get("cancelRequested"):
                    raise NativeJobCancelled("任务已取消")

        def yield_to_newer_scores(preserve=None) -> None:
            # Do not call from arbitrary progress callbacks: the planner and
            # pool can still own in-flight state there.
            score_checkpoint(force=True)
            check_cancel()
            if preserve is not None:
                with self._lock:
                    newer = any(
                        self._jobs[key].get("scoreSequence", 0)
                        > self._jobs[job_id].get("scoreSequence", 0)
                        and not self._jobs[key].get("done")
                        for key in self._score_pending
                    )
                if not newer:
                    return
                # Snapshot only when actually yielding, outside the queue lock.
                preserve()
            while self._run_latest_score(job_id):
                score_checkpoint(force=True)
                check_cancel()

        progress.yield_to_newer_scores = yield_to_newer_scores

        def run_calculation() -> None:
            with self._lock:
                job = self._jobs[job_id]
                if job.get("cancelRequested"):
                    job.update(done=True, state="cancelled", label="任务已取消")
                    return
                job["state"] = "running"
                job["progress"] = max(1, int(job.get("progress") or 0))
            tracks_score = kind == "replay-score"
            if tracks_score:
                record_usage_event("score_attempt")
            try:
                if tracks_score:
                    with score_execution(check_cancel):
                        result = callback(progress)
                else:
                    result = callback(progress)
                score_completed = False
                with self._lock:
                    job = self._jobs[job_id]
                    if job.get("cancelRequested"):
                        job.update(done=True, state="cancelled", label="任务已取消", result=None)
                    else:
                        job.update(done=True, state="completed", progress=100, result=_json_safe(result))
                        score_completed = True
                if tracks_score and score_completed:
                    record_usage_event("score_success")
            except NativeJobCancelled:
                with self._lock:
                    job = self._jobs[job_id]
                    job.update(done=True, state="cancelled", label="任务已取消", result=None, error="")
            except Exception as exc:  # pragma: no cover - surfaced to the UI.
                if tracks_score:
                    record_usage_event("score_failure")
                with self._lock:
                    job = self._jobs[job_id]
                    job.update(
                        done=True,
                        state="failed",
                        error=f"{type(exc).__name__}: {exc}",
                        label="计算失败",
                    )

        def run() -> None:
            try:
                run_calculation()
            finally:
                with self._lock:
                    lease = self._foreground_leases.pop(job_id, None)
                if lease is not None:
                    lease.release()

        with self._lock:
            if self._closed:
                self._jobs[job_id].update(done=True, state="cancelled", cancelRequested=True)
                return job_id
            if kind == "replay-load":
                self._foreground_leases[job_id] = ForegroundLease()
                self._foreground_pending.put(run)
            elif kind == "replay-score":
                self._score_pending[job_id] = run
                # Entries consumed at a safe boundary leave harmless queue
                # wakeups; each score callback is claimed exactly once.
                self._pending.put(self._run_latest_score)
            else:
                self._pending.put(run)
        return job_id

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cooperative cancellation and return the latest public state."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {
                    "done": True,
                    "state": "missing",
                    "progress": 100,
                    "label": "任务不存在",
                    "result": None,
                    "error": "unknown_job",
                }
            if not job.get("done"):
                job["cancelRequested"] = True
                if job.get("state") == "queued":
                    job.update(done=True, state="cancelled", label="任务已取消", result=None, error="")
                    lease = self._foreground_leases.pop(job_id, None)
                    if lease is not None:
                        lease.release()
                else:
                    job["label"] = "正在取消计算…"
            return copy.deepcopy(job)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._public_job(self._jobs.get(job_id, {
                "done": True,
                "state": "missing",
                "progress": 100,
                "label": "任务不存在",
                "result": None,
                "error": "unknown_job",
            }))

    @staticmethod
    def _public_job(job):
        result = copy.deepcopy(job)
        if result.get("priorityWaitJobId") and not result.get("cancelRequested"):
            result["label"] = "已保留评分进度，优先计算新打开的抓包；完成后继续"
        if (result.get("kind") == "replay-score" and not result.get("done")
                and not result.get("cancelRequested") and foreground_pending()):
            if result.get("state") == "running":
                result["state"] = "paused"
                result["label"] = "评分已让出资源，优先读取记录；读取完成后继续"
            else:
                result["label"] = "等待读取记录完成后后台评分"
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for job_id, job in self._jobs.items():
                row = {key: value for key, value in job.items() if key != "result"}
                row["jobId"] = job_id
                rows.append(self._public_job(row))
        return sorted(rows, key=lambda row: float(row.get("createdAt") or 0), reverse=True)

    def shutdown(self, timeout: float = 3.0) -> None:
        """Cancel queued work and terminate the single dispatch thread."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._score_pending.clear()
            for job in self._jobs.values():
                if not job.get("done"):
                    job["cancelRequested"] = True
            leases = list(self._foreground_leases.values())
            self._foreground_leases.clear()
        for lease in leases:
            lease.release()
        # Running calculations receive cooperative cancellation through their
        # next progress callback.  The process pool is reset by the core even
        # if a callback does not return before this bounded join.
        self._pending.put(None)
        self._foreground_pending.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=max(0.0, float(timeout)))
        if self._foreground_worker.is_alive():
            self._foreground_worker.join(timeout=max(0.0, float(timeout)))


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'NativeJobCancelled', 'NativeLiveCapture', 'NativeJobQueue'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from engine/core.py; algorithm bodies are unchanged.
