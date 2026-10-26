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
import tempfile
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

from .bounded_queue import BoundedSpillQueue
from . import capture_health
from .dpscap_segments import (
    load_dpscap_segments,
    remove_dpscap_segments,
)
from .prebattle_state import PrebattleStateTracker
from .replay_duplicate_hit_evidence import ReplayDuplicateHitEvidence
from .dungeon_map_catalog import is_battle_map, resolve_dungeon_map_id
from .nest_recognizer import (
    NestResolution,
    canonical_nest_name,
    resolve_nest_evidence,
    resolve_nest_template,
)
from .protocol import (
    decode_v576_gateway_route_catalog,
    decode_v576_lobby_return,
    decode_v576_map_identity,
    decode_v576_monster_spawn,
    decode_v576_player_health,
    decode_v576_scene_teardown,
)
from .scene_transition import (
    V576SceneReturnTracker,
    V576TcpWorldReturnTracker,
    VerifiedV576HubReturn,
    inferred_hub_history_row,
    promote_hub_row_to_time_space_garden,
)

class CombatEventSource(Protocol):
    def poll(self) -> list[LiveDpsEvent]: ...

    def close(self) -> None: ...

class JsonlReplaySource:
    def __init__(self, path: Path) -> None:
        self._events = [
            CombatEvent.from_mapping(json.loads(line))
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
        self._done = False

    def poll(self) -> list[CombatEvent]:
        if self._done:
            return []
        self._done = True
        return self._events

    def close(self) -> None:
        self._done = True

class V576PcapReplaySource:
    def __init__(
        self,
        path: Path,
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
        action_parser_mode: str | None = None,
    ) -> None:
        packets = read_pcapng_packets(path, deduplicate=False)
        decoder = _V576PacketEventDecoder(
            data_root,
            known_players,
            action_parser_mode=action_parser_mode,
        )
        # Keep the replay decoder available for parity/overlay diagnostics.
        # Live Npcap sources already expose the same attribute; without it,
        # long-capture validation could not verify the local-player Buff
        # filter and silently tested only the unfiltered aggregate rows.
        self._decoder = decoder
        ledger = V576EventLedger()
        transport = V576TransportReassembler()
        events: list[LiveDpsEvent] = []
        for packet in packets:
            for logical_packet in transport.feed(packet):
                if not ledger.accept(logical_packet):
                    continue
                events.extend(decoder.feed_events(logical_packet))
        for logical_packet in transport.flush():
            if ledger.accept(logical_packet):
                events.extend(decoder.feed_events(logical_packet))
        events.extend(decoder.flush_boss_actions())
        permanent_complete = _backfill_permanent_skill_object_history(
            events, decoder
        )
        canonical_events = _canonicalize_replay_actor_context(
            permanent_complete, decoder
        )
        player_joined_events = _merge_rejoined_replay_players(
            canonical_events, decoder
        )
        boss_complete = _supplement_boss_statuses_from_casts(player_joined_events)
        player_complete = _supplement_player_buffs_from_casts(
            boss_complete, decoder.team_buff_skill_names
        )
        self._events = _backfill_preexisting_maintained_player_buffs(
            player_complete
        )
        evidence_registry = decoder.debuff_evidence_registry
        self.debuff_evidence_stats = {
            "enabled": evidence_registry.enabled,
            "keys": evidence_registry.key_count,
            "cache_hits": evidence_registry.hits,
            "conflicts": evidence_registry.conflicts,
        }
        self._done = False

    def poll(self) -> list[LiveDpsEvent]:
        if self._done:
            return []
        self._done = True
        return self._events

    def close(self) -> None:
        self._done = True

class V576DiagnosticReplaySource:
    """Replay the compact DNDP1 traces written by ``V576NpcapSource``."""

    _MAGIC = b"DNDP1\x00"
    _FRAME_HEADER = struct.Struct("<dBII")
    _PIPELINE_QUEUE_CAPACITY = 512

    def __init__(
        self,
        path: Path,
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        action_parser_mode: str | None = None,
        duplicate_hit_evidence: bool = True,
        voice_feed: bool = False,
    ) -> None:
        # MovementLedger stays independent from combat attribution, but the
        # replay reader has already paid for frame I/O and transport
        # reassembly. Collect verified positions on that same ordered packet
        # stream so opening the trajectory tab does not rescan a large file.
        from .movement import MovementLedger

        self._path = Path(path)
        self._segment_set = load_dpscap_segments(self._path)
        self._capture_paths = (
            self._segment_set.paths
            if self._segment_set is not None
            else (self._path,)
        )
        self._capture_total_bytes = (
            int(self._segment_set.source_size)
            if self._segment_set is not None
            else int(self._path.stat().st_size)
        )
        self._progress_callback = progress_callback
        self._cancel_check = cancel_check
        self._duplicate_hit_evidence_enabled = bool(duplicate_hit_evidence)
        self.duplicate_hit_evidence_stats: dict[str, int] = {}
        pipeline_setting = os.environ.get(
            "DRAGON_NEST_REPLAY_PIPELINE", "serial"
        ).strip().lower()
        self._pipeline_mode = (
            "serial"
            if pipeline_setting in {"0", "false", "off", "serial"}
            else "hybrid"
        )
        self._pipeline_cancel = threading.Event()
        self._pipeline_queue: queue.Queue[tuple[str, object]] | None = None
        self._pipeline_thread: threading.Thread | None = None
        self.replay_pipeline_stats: dict[str, int | float | str] = {
            "mode": self._pipeline_mode,
            "storage": (
                "segmented-v1" if self._segment_set is not None else "single-file"
            ),
            "segmentCount": len(self._capture_paths),
        }

        # Frame I/O and Ethernet/IP/TCP header parsing are independent from
        # the large skill/profile catalog initialization.  Start that bounded
        # producer first so both stages overlap, but leave transport assembly
        # and all stateful event decoding on the caller thread in exact capture
        # order.  This is deliberately a hybrid pipeline, not unsafe parallel
        # mutation of decoder state.
        if self._pipeline_mode == "hybrid":
            self._pipeline_queue = queue.Queue(
                maxsize=self._PIPELINE_QUEUE_CAPACITY
            )
            self._pipeline_thread = threading.Thread(
                target=self._run_frame_preprocessor,
                name="dndp-replay-frame-preprocessor",
                daemon=True,
            )
            self._pipeline_thread.start()

        decoder_started = time.perf_counter()
        try:
            decoder = _V576PacketEventDecoder(
                data_root,
                known_players,
                action_parser_mode=action_parser_mode,
            )
        except BaseException:
            self._stop_pipeline()
            raise
        self.replay_pipeline_stats["decoderInitMs"] = round(
            (time.perf_counter() - decoder_started) * 1000.0,
            3,
        )
        self._decoder = decoder
        # 语音证据：实时源（V576NpcapSource）在建立解码器后必接这一句，回放源
        # 以前没接，于是「回放录像」永远产不出语音证据行。默认仍然不接
        # （DPS 回放不需要，接了只是白算），需要的人显式要。
        self.voice_signal_feed = None
        if voice_feed:
            from .voice_signals import VoiceSignalFeed

            self.voice_signal_feed = VoiceSignalFeed()
            decoder.voice_signal_feed = self.voice_signal_feed
        self._events: list[LiveDpsEvent] | None = None
        self.total_event_count = 0
        self.debuff_evidence_stats: dict[str, int | bool] = {}
        self.capture_origin_timestamp_seconds: float | None = None
        self.capture_end_timestamp_seconds: float | None = None
        self.capture_end_timestamp_ms: int = 0
        self.capture_integrity: dict[str, object] = {"status": "unknown", "reason": "legacy_capture_no_health_metadata"}
        self._observed_physical_frames = 0
        self._movement_ledger = MovementLedger()
        self.movement_capture = None
        self._done = False

    def _pipeline_put(self, kind: str, value: object) -> bool:
        output = self._pipeline_queue
        if output is None:
            return False
        while not self._pipeline_cancel.is_set():
            try:
                output.put((kind, value), timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    @staticmethod
    def _decode_preprocessed_frame(
        timestamp: float,
        adapter: int,
        frame: bytes,
        original_length: int,
    ) -> object:
        if adapter == capture_health.ADAPTER_INDEX:
            return capture_health.decode_health(frame)
        if adapter == IDENTITY_METADATA_ADAPTER_INDEX:
            return decode_identity_trace_bindings(frame)
        if adapter == PROFILE_IDENTITY_ADAPTER_INDEX:
            return decode_profile_identity_bindings(frame)
        if adapter == PREBATTLE_STATE_ADAPTER_INDEX:
            return decode_prebattle_state_snapshot(frame)
        return decode_captured_frame(timestamp, frame, original_length)

    def _read_predecoded_segment_frames(self, total_bytes: int):
        """Pre-read two immutable parts while preserving physical order.

        Stateless Ethernet/IP decoding may run ahead by at most two bounded
        queues. Stateful TCP reassembly and game-event decoding remain on the
        single ordered consumer in ``_load_events``.
        """

        paths = tuple(self._capture_paths)
        try:
            configured_workers = int(
                os.environ.get("DRAGON_NEST_DPSCAP_READ_WORKERS", "1") or 1
            )
        except (TypeError, ValueError):
            configured_workers = 1
        worker_limit = max(
            1,
            min(4, configured_workers),
        )
        if worker_limit <= 1 or len(paths) <= 1:
            yield from self._read_predecoded_frames_serial(total_bytes)
            return
        virtual_bases: list[int] = []
        virtual_base = len(self._MAGIC)
        for capture_path in paths:
            virtual_bases.append(virtual_base)
            virtual_base += max(
                0, int(capture_path.stat().st_size) - len(self._MAGIC)
            )
        local_cancel = threading.Event()
        outputs = [
            queue.Queue(maxsize=max(2, self._PIPELINE_QUEUE_CAPACITY // 64))
            for _ in paths
        ]
        workers: list[threading.Thread | None] = [None] * len(paths)

        def put(index: int, kind: str, value: object) -> bool:
            output = outputs[index]
            while not local_cancel.is_set() and not self._pipeline_cancel.is_set():
                try:
                    output.put((kind, value), timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def produce(index: int) -> None:
            capture_path = paths[index]
            base = virtual_bases[index]
            batch: list[tuple[float, int, object, int, int]] = []
            try:
                with capture_path.open("rb", buffering=1024 * 1024) as handle:
                    if handle.read(len(self._MAGIC)) != self._MAGIC:
                        raise ValueError(
                            "不是有效的 Dragon Nest .dpscap 统计记录"
                        )
                    while not local_cancel.is_set():
                        if self._pipeline_cancel.is_set() or (
                            self._cancel_check is not None
                            and self._cancel_check()
                        ):
                            raise InterruptedError(
                                "Replay loading was cancelled"
                            )
                        header = handle.read(self._FRAME_HEADER.size)
                        if not header:
                            break
                        if len(header) != self._FRAME_HEADER.size:
                            raise ValueError(".dpscap 分片尾部记录不完整")
                        timestamp, adapter, original_length, captured_length = (
                            self._FRAME_HEADER.unpack(header)
                        )
                        if captured_length > 16 * 1024 * 1024:
                            raise ValueError(
                                ".dpscap 包长度异常，文件可能已损坏"
                            )
                        frame = handle.read(captured_length)
                        if len(frame) != captured_length:
                            raise ValueError(".dpscap 分片尾部数据不完整")
                        decoded = self._decode_preprocessed_frame(
                            timestamp, adapter, frame, original_length
                        )
                        position = base + handle.tell() - len(self._MAGIC)
                        batch.append(
                            (
                                float(timestamp),
                                int(adapter),
                                decoded,
                                position,
                                total_bytes,
                            )
                        )
                        if len(batch) >= 256:
                            if not put(index, "batch", tuple(batch)):
                                return
                            batch.clear()
                if batch and not put(index, "batch", tuple(batch)):
                    return
            except BaseException as exc:
                put(index, "error", exc)
            finally:
                put(index, "done", None)

        def launch(index: int) -> None:
            worker = threading.Thread(
                target=produce,
                args=(index,),
                name=f"dndp-segment-prefetch-{index + 1}",
                daemon=True,
            )
            workers[index] = worker
            worker.start()

        next_to_launch = 0
        for _ in range(min(worker_limit, len(paths))):
            launch(next_to_launch)
            next_to_launch += 1
        frame_count = 0
        try:
            for index, output in enumerate(outputs):
                while True:
                    if self._cancel_check is not None and self._cancel_check():
                        raise InterruptedError("Replay loading was cancelled")
                    try:
                        kind, value = output.get(timeout=0.1)
                    except queue.Empty:
                        worker = workers[index]
                        if worker is not None and not worker.is_alive():
                            raise RuntimeError(
                                ".dpscap 分片预取线程提前结束"
                            )
                        continue
                    if kind == "batch":
                        if not isinstance(value, tuple):
                            raise RuntimeError(".dpscap 分片预取批次无效")
                        frame_count += len(value)
                        yield from value
                    elif kind == "error":
                        if isinstance(value, BaseException):
                            raise value
                        raise RuntimeError(str(value))
                    elif kind == "done":
                        break
                worker = workers[index]
                if worker is not None:
                    worker.join(timeout=1.0)
                if next_to_launch < len(paths):
                    launch(next_to_launch)
                    next_to_launch += 1
        finally:
            local_cancel.set()
            for worker in workers:
                if (
                    worker is not None
                    and worker is not threading.current_thread()
                    and worker.is_alive()
                ):
                    worker.join(timeout=1.0)
        self.replay_pipeline_stats["physicalFrames"] = frame_count
        self.replay_pipeline_stats["captureBytes"] = total_bytes
        self.replay_pipeline_stats["segmentReadWorkers"] = worker_limit

    def _read_predecoded_frames_serial(self, total_bytes: int):
        frame_count = 0
        virtual_base = len(self._MAGIC)
        for capture_path in self._capture_paths:
            with capture_path.open("rb", buffering=1024 * 1024) as handle:
                if handle.read(len(self._MAGIC)) != self._MAGIC:
                    raise ValueError("不是有效的 Dragon Nest .dpscap 统计记录")
                while True:
                    if self._pipeline_cancel.is_set() or (
                        self._cancel_check is not None and self._cancel_check()
                    ):
                        raise InterruptedError("Replay loading was cancelled")
                    header = handle.read(self._FRAME_HEADER.size)
                    if not header:
                        break
                    if len(header) != self._FRAME_HEADER.size:
                        raise ValueError(".dpscap 记录头被截断，不能视为完整抓包")
                    timestamp, adapter, original_length, captured_length = (
                        self._FRAME_HEADER.unpack(header)
                    )
                    if captured_length > 16 * 1024 * 1024:
                        raise ValueError(".dpscap 包长度异常，文件可能已损坏")
                    frame = handle.read(captured_length)
                    if len(frame) != captured_length:
                        raise ValueError(".dpscap 原始包被截断，不能视为完整抓包")
                    decoded = self._decode_preprocessed_frame(
                        timestamp, adapter, frame, original_length
                    )
                    frame_count += 1
                    position = virtual_base + handle.tell() - len(self._MAGIC)
                    yield (
                        float(timestamp),
                        int(adapter),
                        decoded,
                        position,
                        total_bytes,
                    )
                virtual_base += max(0, handle.tell() - len(self._MAGIC))
        self.replay_pipeline_stats["physicalFrames"] = frame_count
        self.replay_pipeline_stats["captureBytes"] = total_bytes

    def _read_predecoded_frames(self):
        """Yield physical capture rows after stateless frame preprocessing."""

        total_bytes = max(1, self._capture_total_bytes)
        if self._segment_set is not None and len(self._capture_paths) > 1:
            yield from self._read_predecoded_segment_frames(total_bytes)
            return
        yield from self._read_predecoded_frames_serial(total_bytes)

    def _run_frame_preprocessor(self) -> None:
        started = time.perf_counter()
        try:
            for record in self._read_predecoded_frames():
                if not self._pipeline_put("record", record):
                    return
        except BaseException as exc:
            self._pipeline_put("error", exc)
        finally:
            self.replay_pipeline_stats["preprocessMs"] = round(
                (time.perf_counter() - started) * 1000.0,
                3,
            )
            self._pipeline_put("done", None)

    def _iter_predecoded_frames(self):
        if self._pipeline_mode == "serial":
            yield from self._read_predecoded_frames()
            return
        output = self._pipeline_queue
        if output is None:
            raise RuntimeError("Replay preprocessing pipeline was not initialized")
        while True:
            if self._cancel_check is not None and self._cancel_check():
                self._pipeline_cancel.set()
                raise InterruptedError("Replay loading was cancelled")
            try:
                kind, value = output.get(timeout=0.1)
            except queue.Empty:
                worker = self._pipeline_thread
                if worker is not None and not worker.is_alive():
                    raise RuntimeError("Replay preprocessing pipeline stopped early")
                continue
            if kind == "record":
                yield value
            elif kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError(str(value))
            elif kind == "done":
                break

    def _stop_pipeline(self) -> None:
        self._pipeline_cancel.set()
        output = self._pipeline_queue
        if output is not None:
            while True:
                try:
                    output.get_nowait()
                except queue.Empty:
                    break
        worker = self._pipeline_thread
        if (
            worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=1.0)
        self._pipeline_thread = None

    def _load_events(self) -> list[LiveDpsEvent]:
        if self._events is not None:
            return self._events
        load_started = time.perf_counter()
        decoder = self._decoder
        ledger = V576EventLedger()
        transport = V576TransportReassembler()
        identity_transport = V576TransportReassembler()
        events: list[LiveDpsEvent] = []
        duplicate_evidence = (
            ReplayDuplicateHitEvidence()
            if self._duplicate_hit_evidence_enabled
            and hasattr(getattr(decoder, "damage_object_origins", None), "evidence_epoch")
            else None
        )

        def consume(logical_packet: PacketRecord) -> None:
            if ledger.accept(logical_packet):
                start = len(events)
                events.extend(decoder.feed_events(logical_packet))
                if duplicate_evidence is not None:
                    duplicate_evidence.remember(logical_packet, events, start, decoder)
            elif duplicate_evidence is not None:
                # A duplicate is never decoded/emitted twice. Its exact bytes
                # may supplement the original replay row with forward object
                # evidence that was not yet available on the first arrival.
                duplicate_evidence.supplement(logical_packet, events, decoder)
            movement_origin = (
                getattr(decoder, "first_timestamp", None)
                or self.capture_origin_timestamp_seconds
            )
            if movement_origin is not None:
                self._movement_ledger.observe(logical_packet, movement_origin)

        def observe_session_evidence_packet(packet: PacketRecord) -> None:
            events.extend(_verified_session_evidence_events(decoder, packet))

        def observe_identity_packet(logical_packet: PacketRecord) -> None:
            observer = getattr(
                decoder, "_observe_tcp_player_spawn_identity", None
            )
            if callable(observer):
                # Calling the narrow observer is deliberate. ``feed_events``
                # would set first_timestamp and could shift battle zero when
                # the regular TCP stream is not emitted until EOF flush.
                observer(logical_packet)
                return
            original_origin = getattr(decoder, "first_timestamp", None)
            decoder.feed_events(logical_packet)
            decoder.first_timestamp = original_origin
        total_bytes = max(1, self._capture_total_bytes)
        progress_step = max(128 * 1024, total_bytes // 400)
        next_progress = 0
        if self._progress_callback is not None:
            self._progress_callback(len(self._MAGIC), total_bytes)
        try:
            for (
                timestamp,
                adapter,
                decoded,
                position,
                _capture_bytes,
            ) in self._iter_predecoded_frames():
                if adapter < capture_health.ADAPTER_INDEX:
                    self._observed_physical_frames += 1
                if adapter not in {
                    IDENTITY_TRACE_ADAPTER_INDEX,
                    IDENTITY_METADATA_ADAPTER_INDEX,
                    PROFILE_IDENTITY_ADAPTER_INDEX,
                    SESSION_EVIDENCE_ADAPTER_INDEX,
                    PREBATTLE_STATE_ADAPTER_INDEX,
                    capture_health.ADAPTER_INDEX,
                }:
                    if self.capture_origin_timestamp_seconds is None:
                        self.capture_origin_timestamp_seconds = float(timestamp)
                    self.capture_end_timestamp_seconds = max(
                        float(timestamp),
                        self.capture_end_timestamp_seconds
                        if self.capture_end_timestamp_seconds is not None
                        else float(timestamp),
                    )
                if adapter == capture_health.ADAPTER_INDEX:
                    self.capture_integrity = dict(decoded)
                    self.capture_integrity["status"] = (
                        "incomplete" if any(decoded.get(field) for field in (
                            "captureFailed", "truncatedFrames", "driverDroppedDelta", "interfaceDroppedDelta"))
                        else "no_local_loss_detected" if decoded.get("driverCountersAvailable")
                        else "driver_counters_unavailable")
                    continue
                if adapter == IDENTITY_METADATA_ADAPTER_INDEX:
                    if isinstance(decoded, dict):
                        decoder.known_actor_names.update(decoded)
                    continue
                if adapter == PROFILE_IDENTITY_ADAPTER_INDEX:
                    if isinstance(decoded, tuple):
                        observe_profile_rows = getattr(
                            self._movement_ledger,
                            "observe_profile_identity_rows",
                            None,
                        )
                        if callable(observe_profile_rows):
                            observe_profile_rows(decoded)
                        for row in decoded:
                            try:
                                runtime_id = int(row.get("runtime_actor_id", 0) or 0)
                                profile_id = int(row.get("profile_id", 0) or 0)
                            except (AttributeError, TypeError, ValueError):
                                continue
                            name = str(row.get("character_name", "") or "").strip()
                            job = str(row.get("job", "") or "").strip()
                            source = str(row.get("source", "cache") or "cache").strip()
                            if runtime_id <= 0 or profile_id <= 0 or not name:
                                continue
                            decoder.known_actor_names[runtime_id] = name
                            decoder.known_actor_names[profile_id] = name
                            events.append(PlayerInspectEvent(
                                timestamp_ms=0,
                                target_id=f"0x{runtime_id:08X}",
                                target_name=name,
                                job=job,
                                status="已验证身份缓存",
                                request_fingerprint=(
                                    f"identity-cache:{runtime_id:08X}:{profile_id:08X}"
                                ),
                                response_layout="DNPI1",
                                fields=(
                                    ("响应类型", "共享身份缓存"),
                                    ("角色档案标识", f"0x{profile_id:08X}"),
                                    ("实时ID", f"0x{runtime_id:08X}"),
                                    ("身份来源", source),
                                ),
                            ))
                    continue
                if adapter == PREBATTLE_STATE_ADAPTER_INDEX:
                    if isinstance(decoded, dict):
                        inherited = decoder.seed_capture_boundary_evidence(decoded)
                        if inherited:
                            events.extend(inherited)
                    continue
                packet = decoded
                if packet is None:
                    continue
                if adapter == SESSION_EVIDENCE_ADAPTER_INDEX:
                    # Session evidence is intentionally decoded through the
                    # narrow HP observer only. It can seed local identity,
                    # max HP and reconnect fingerprints, but cannot enter the
                    # normal transport, event ledger or battle timeline.
                    observe_session_evidence_packet(packet)
                    movement_origin = (
                        getattr(decoder, "first_timestamp", None)
                        or self.capture_origin_timestamp_seconds
                        or float(packet.timestamp_seconds)
                    )
                    observe_movement_session = getattr(
                        self._movement_ledger,
                        "observe_session_evidence",
                        None,
                    )
                    if callable(observe_movement_session):
                        observe_movement_session(packet, movement_origin)
                    continue
                if adapter == IDENTITY_TRACE_ADAPTER_INDEX:
                    # These original TCP frames predate the accepted battle
                    # boundary and are appended at EOF. Decode them through a
                    # separate stream solely for actor/profile/name bindings:
                    # old sequence numbers must never poison combat TCP state,
                    # and identity packets must not create timeline events.
                    for logical_packet in identity_transport.feed(packet):
                        observe_identity_packet(logical_packet)
                    continue
                for logical_packet in transport.feed(packet):
                    consume(logical_packet)
                if (
                    self._progress_callback is not None
                    and position >= next_progress
                ):
                    self._progress_callback(position, total_bytes)
                    next_progress = position + progress_step
        finally:
            worker = self._pipeline_thread
            if worker is not None and worker.is_alive():
                worker.join(timeout=1.0)
            self._pipeline_thread = None
        for logical_packet in transport.flush():
            consume(logical_packet)
        if duplicate_evidence is not None:
            self.duplicate_hit_evidence_stats = dict(duplicate_evidence.stats)
        for logical_packet in identity_transport.flush():
            observe_identity_packet(logical_packet)
        drain_identities = getattr(
            decoder, "drain_player_spawn_identities", None
        )
        if callable(drain_identities):
            drain_identities()
        events.extend(decoder.flush_boss_actions())
        decoder_origin = getattr(decoder, "first_timestamp", None)
        if decoder_origin is not None:
            self.capture_origin_timestamp_seconds = float(decoder_origin)
        if (
            self.capture_origin_timestamp_seconds is not None
            and self.capture_end_timestamp_seconds is not None
        ):
            self.capture_end_timestamp_ms = max(
                0,
                round(
                    (
                        self.capture_end_timestamp_seconds
                        - self.capture_origin_timestamp_seconds
                    )
                    * 1000.0
                ),
            )
            self.movement_capture = self._movement_ledger.snapshot(
                duration_seconds=self.capture_end_timestamp_ms / 1000.0,
                map_history=tuple(
                    dict(row)
                    for row in getattr(decoder, "detected_map_history", ())
                    if isinstance(row, dict)
                ),
                diagnostics={
                    "source": "primary_replay_packet_stream",
                    "physical_frames": int(
                        self.replay_pipeline_stats.get("physicalFrames", 0)
                        or 0
                    ),
                    "runtime_aliases": {
                        f"0x{int(new_id) & 0xFFFFFFFF:08X}": (
                            f"0x{int(old_id) & 0xFFFFFFFF:08X}"
                        )
                        for new_id, old_id in getattr(
                            decoder,
                            "player_runtime_aliases",
                            {},
                        ).items()
                    },
                    "runtime_alias_evidence": tuple(
                        dict(row)
                        for row in getattr(decoder, "runtime_alias_evidence", ())
                        if isinstance(row, dict)
                    ),
                },
            )
        if self._progress_callback is not None:
            self._progress_callback(total_bytes, total_bytes)
        events = _backfill_permanent_skill_object_history(
            events,
            decoder,
        )
        events = _canonicalize_replay_actor_context(events, decoder)
        events = _merge_rejoined_replay_players(events, decoder)
        events = _supplement_boss_statuses_from_casts(events)
        events = _supplement_player_buffs_from_casts(
            events,
            decoder.team_buff_skill_names,
        )
        events = _backfill_preexisting_maintained_player_buffs(events)
        evidence_registry = decoder.debuff_evidence_registry
        self.debuff_evidence_stats = {
            "enabled": evidence_registry.enabled,
            "keys": evidence_registry.key_count,
            "cache_hits": evidence_registry.hits,
            "conflicts": evidence_registry.conflicts,
        }
        self._events = events
        self.total_event_count = len(events)
        self.replay_pipeline_stats["loadMs"] = round(
            (time.perf_counter() - load_started) * 1000.0,
            3,
        )
        self.replay_pipeline_stats["eventCount"] = len(events)
        self.replay_pipeline_stats["tlsConnections"] = transport.tls_probe.confirmed_connections
        self.replay_pipeline_stats["tlsDecodeSkippedPackets"] = transport.tls_probe.ignored_packets
        if "physicalFrames" in self.capture_integrity and self.capture_integrity["physicalFrames"] != self._observed_physical_frames:
            raise ValueError(".dpscap 实际包数量与保存的完整性元数据不一致")
        return events

    def iter_batches(self, batch_size: int = 4096):
        """Yield normalized replay events in bounded aggregation batches.

        DNDP1 frames are decoded incrementally. Global actor/status
        normalization remains byte-for-byte compatible, while callers no
        longer retain a second full event list during aggregation.
        """
        if self._done:
            return
        events = self._load_events()
        self._done = True
        size = max(1, int(batch_size))
        try:
            for offset in range(0, len(events), size):
                if self._cancel_check is not None and self._cancel_check():
                    raise InterruptedError("Replay loading was cancelled")
                yield events[offset : offset + size]
        finally:
            self._events = []

    def poll(self) -> list[LiveDpsEvent]:
        if self._done:
            return []
        events = self._load_events()
        self._done = True
        self._events = []
        return events

    def close(self) -> None:
        self._done = True
        self._events = []
        self._stop_pipeline()


def scan_v576_diagnostic_map_history(path: Path) -> tuple[dict[str, object], ...]:
    """Read verified map packets, with a conservative entity fallback.

    This is used when the full combat replay comes from the version cache.
    It avoids rebuilding damage/status events while preserving exact map-ID
    recognition on every load.  A large share of valid captures start after
    the one-shot map transition packet (or receive no TCP confirmation as a
    non-leader).  In that case the formal map still emits structurally
    verified ``06 27 55 00`` entity rows.  If, and only if, all unique entity
    evidence resolves to one configured dungeon profile, return a synthetic
    map-history row with ``map_id == 0``.  This lets replay, movement and score
    pages show the proven dungeon without pretending that an exact room/map
    ID was observed.
    """

    capture_path = Path(path)
    transport = V576TransportReassembler()
    history: list[dict[str, object]] = []
    template_observed_at: dict[int, float] = {}
    origin: float | None = None
    scene_return_tracker = V576SceneReturnTracker()
    tcp_world_return_tracker = V576TcpWorldReturnTracker()
    active_battle_map_id = 0
    latest_inferred_hub_row: dict[str, object] | None = None

    def append_hub_observation(
        observation: VerifiedV576HubReturn,
    ) -> None:
        nonlocal latest_inferred_hub_row
        hub_row = inferred_hub_history_row(observation)
        if observation.next_battle_map_id is not None:
            promote_hub_row_to_time_space_garden(hub_row)
            latest_inferred_hub_row = None
        elif observation.destination_map_id is not None:
            latest_inferred_hub_row = None
        else:
            latest_inferred_hub_row = hub_row
        history.append(hub_row)
        history.sort(key=lambda item: float(item.get("observed_at") or 0.0))

    def observe_logical_packet(logical_packet: PacketRecord) -> None:
        nonlocal latest_inferred_hub_row, active_battle_map_id
        observed_at = max(
            0.0,
            float(logical_packet.timestamp_seconds)
            - float(origin or logical_packet.timestamp_seconds),
        )
        world_return = tcp_world_return_tracker.observe(
            logical_packet,
            observed_at=observed_at,
            active_battle_map_id=active_battle_map_id,
        )
        if world_return is not None:
            append_hub_observation(world_return)
            active_battle_map_id = 0
        monster_spawn = decode_v576_monster_spawn(
            logical_packet.payload,
            logical_packet.inferred_direction(),
        )
        if monster_spawn is not None:
            template_observed_at.setdefault(
                int(monster_spawn.template_id),
                observed_at,
            )
            nest_resolution = resolve_nest_template(
                monster_spawn.template_id
            )
            if nest_resolution is not None:
                scene_return_tracker.observe_battle_context(
                    observed_at=observed_at,
                    context_key=nest_resolution.profile.key,
                )

        decoded = decode_v576_map_identity(
            logical_packet.payload,
            logical_packet.inferred_direction(),
            connection_context=logical_packet,
        )
        if decoded is not None and not bool(
            getattr(decoded, "authoritative", True)
        ):
            selected_entry = resolve_dungeon_map_id(decoded.map_id)
            if is_battle_map(selected_entry):
                scene_return_tracker.observe_map_candidate(
                    int(decoded.map_id),
                    observed_at=observed_at,
                )
                if latest_inferred_hub_row is not None:
                    promote_hub_row_to_time_space_garden(
                        latest_inferred_hub_row
                    )
                    latest_inferred_hub_row = None
            decoded = None
        if decoded is not None and bool(
            getattr(decoded, "authoritative", True)
        ):
            scene_return = scene_return_tracker.observe_map(
                int(decoded.map_id),
                active=bool(decoded.active),
                observed_at=observed_at,
            )
            if (
                not decoded.active
                and decoded.layout
                == "s2c-tcp-scene-map-release-322-v576"
            ):
                scene_return_tracker.observe_scene_teardown(
                    observed_at=observed_at,
                )
            if scene_return is not None:
                append_hub_observation(scene_return)
            entry = resolve_dungeon_map_id(decoded.map_id)
            if decoded.active:
                active_battle_map_id = (
                    int(decoded.map_id) if is_battle_map(entry) else 0
                )
            elif int(decoded.map_id) == active_battle_map_id:
                active_battle_map_id = 0
            if entry is not None and entry.display_name:
                if (
                    decoded.active
                    and is_battle_map(entry)
                    and latest_inferred_hub_row is not None
                ):
                    promote_hub_row_to_time_space_garden(
                        latest_inferred_hub_row
                    )
                    latest_inferred_hub_row = None
                map_row = {
                    "map_id": decoded.map_id,
                    "map_name": entry.display_name,
                    "nest_name": (
                        canonical_nest_name(entry.display_name)
                        if is_battle_map(entry)
                        else ""
                    ),
                    "official_name": entry.official_name,
                    "map_type": entry.map_type,
                    "content_kind": entry.content_kind,
                    "observed_at": observed_at,
                    "layout": decoded.layout,
                    "confidence": "exact_map_id",
                    "inferred": False,
                }
                if decoded.active:
                    if not (
                        history
                        and int(history[-1].get("map_id") or 0)
                        == decoded.map_id
                        and history[-1].get("left_at") is None
                    ):
                        history.append(map_row)
                else:
                    matching_row = next(
                        (
                            row
                            for row in reversed(history)
                            if int(row.get("map_id") or 0)
                            == decoded.map_id
                            and row.get("left_at") is None
                        ),
                        None,
                    )
                    if matching_row is None:
                        matching_row = next(
                            (
                                row
                                for row in reversed(history)
                                if int(row.get("map_id") or 0)
                                == decoded.map_id
                                and row.get("left_at") is not None
                                and 0.0
                                <= observed_at
                                - float(row.get("left_at") or 0.0)
                                <= 2.0
                            ),
                            None,
                        )
                    if matching_row is None:
                        map_row["observed_at"] = (
                            0.0 if not history else observed_at
                        )
                        map_row["entry_inferred_from_exit"] = True
                        history.append(map_row)
                        matching_row = map_row
                    matching_row["left_at"] = observed_at
                    matching_row["exit_layout"] = decoded.layout

        # A 322-byte scene-release packet can carry both the authoritative
        # map-leave record and the independent scene-teardown record.  Match
        # the realtime decoder order: record the map leave first, then attach
        # teardown evidence to that same exit.  Reversing this order loses the
        # teardown flag and creates a duplicate synthetic map row.
        scene_teardown = decode_v576_scene_teardown(
            logical_packet.payload,
            logical_packet.inferred_direction(),
            connection_context=logical_packet,
        )
        if scene_teardown is not None:
            scene_return_tracker.observe_scene_teardown(
                observed_at=observed_at,
            )
            for row in reversed(history):
                if int(row.get("map_type") or 0) != 3:
                    continue
                if row.get("left_at") is None:
                    row["left_at"] = observed_at
                    row["exit_layout"] = scene_teardown.layout
                break

        transition = decode_v576_lobby_return(
            logical_packet.payload,
            logical_packet.inferred_direction(),
        )
        if transition is not None:
            scene_return_tracker.observe_transition_ack(
                observed_at=observed_at,
                route_code=transition.route_code,
            )
            terminal_return = (
                scene_return_tracker.consume_configured_terminal_return()
            )
            if terminal_return is not None:
                append_hub_observation(terminal_return)

        gateway_route = decode_v576_gateway_route_catalog(
            logical_packet.payload,
            logical_packet.inferred_direction(),
            connection_context=logical_packet,
        )
        if gateway_route is not None:
            gateway_return = (
                scene_return_tracker.observe_gateway_route_catalog(
                    observed_at=observed_at,
                )
            )
            if gateway_return is not None:
                append_hub_observation(gateway_return)

        health_rows = decode_v576_player_health(
            logical_packet.payload,
            logical_packet.inferred_direction(),
        )
        if any(
            row.layout == "s2c-udp-player-resource-sync-210c-v576"
            for row in health_rows
        ):
            observation = scene_return_tracker.observe_resource_sync(
                observed_at=observed_at,
            )
            if observation is not None:
                append_hub_observation(observation)

    try:
        handle = capture_path.open("rb")
    except OSError:
        return ()
    with handle:
        if handle.read(len(V576DiagnosticReplaySource._MAGIC)) != (
            V576DiagnosticReplaySource._MAGIC
        ):
            return ()
        while True:
            header = handle.read(V576DiagnosticReplaySource._FRAME_HEADER.size)
            if not header:
                break
            if len(header) != V576DiagnosticReplaySource._FRAME_HEADER.size:
                break
            timestamp, adapter, original_length, captured_length = (
                V576DiagnosticReplaySource._FRAME_HEADER.unpack(header)
            )
            if captured_length > 16 * 1024 * 1024:
                break
            frame = handle.read(captured_length)
            if len(frame) != captured_length:
                break
            if adapter in {
                IDENTITY_TRACE_ADAPTER_INDEX,
                IDENTITY_METADATA_ADAPTER_INDEX,
                PROFILE_IDENTITY_ADAPTER_INDEX,
                SESSION_EVIDENCE_ADAPTER_INDEX,
                PREBATTLE_STATE_ADAPTER_INDEX,
                capture_health.ADAPTER_INDEX,
            }:
                continue
            if origin is None:
                origin = float(timestamp)
            packet = decode_captured_frame(timestamp, frame, original_length)
            if packet is None or str(packet.transport).upper() not in {"UDP", "TCP"}:
                continue
            for logical_packet in transport.feed(packet):
                observe_logical_packet(logical_packet)
        # A TCP map confirmation can be the last message in a capture and
        # remain buffered until EOF.  Omitting this flush made those records
        # intermittently appear as “未识别地图”.
        for logical_packet in transport.flush():
            observe_logical_packet(logical_packet)
    if any(int(row.get("map_type") or 3) == 3 for row in history):
        history.sort(key=lambda item: float(item.get("observed_at") or 0.0))
        return tuple(history)

    resolution = resolve_nest_evidence(
        template_ids=tuple(template_observed_at)
    )
    if resolution is None:
        # Some hardcore rooms intentionally reuse one ordinary monster from
        # the classic profile.  Treat a lone foreign template as noise only
        # when another profile has at least two independent template IDs and
        # at least twice as much evidence.  A 1-vs-1 or otherwise close split
        # remains unrecognized, preserving the multi-run/ambiguous guard.
        profile_evidence: Counter[str] = Counter()
        profiles_by_key = {}
        first_template_by_key: dict[str, int] = {}
        for template_id in template_observed_at:
            candidate = resolve_nest_template(template_id)
            if candidate is None:
                continue
            key = candidate.profile.key
            profile_evidence[key] += 1
            profiles_by_key[key] = candidate.profile
            first_template_by_key.setdefault(key, int(template_id))
        ranked = profile_evidence.most_common(2)
        if ranked:
            best_key, best_count = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0
            if best_count >= 2 and best_count >= runner_up * 2:
                resolution = NestResolution(
                    profiles_by_key[best_key],
                    "dominant_template_set",
                    first_template_by_key[best_key],
                )
    if resolution is None:
        return tuple(history)
    profile = resolution.profile
    observed_at = template_observed_at.get(
        int(resolution.template_id),
        min(template_observed_at.values(), default=0.0),
    )
    history.append(
        {
            "map_id": 0,
            "map_name": profile.display_name,
            "nest_name": profile.label,
            "official_name": profile.display_name,
            "content_kind": "dungeon",
            "observed_at": max(0.0, float(observed_at)),
            "layout": "s2c-udp-formal-map-entity-fallback-v576",
            "confidence": resolution.confidence,
            "template_id": int(resolution.template_id),
            "inferred": True,
        }
    )
    history.sort(key=lambda item: float(item.get("observed_at") or 0.0))
    return tuple(history)

class V576PktmonTextStreamSource:
    """Incrementally decode PktMon real-time text output while it is written."""

    _HEADER_RE = re.compile(
        r"^(\d{2}):(\d{2}):(\d{2})\.(\d+).*?OriginalSize\s+(\d+).*?LoggedSize\s+(\d+)",
        re.I,
    )
    _HEX_RE = re.compile(r"^\s*0x[0-9a-f]+:\s*([0-9a-f][0-9a-f\s]*)$", re.I)

    def __init__(
        self,
        path: Path,
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.path = path
        self._decoder = _V576PacketEventDecoder(data_root, known_players)
        self._handle = None
        self._text_decoder = None
        self._buffer = ""
        self._current_timestamp = 0.0
        self._current_original_size = 0
        self._current_logged_size = 0
        self._current_frame = bytearray()
        self._last_clock_seconds: float | None = None
        self._day_offset = 0.0
        self._ledger = V576EventLedger()
        self._transport = V576TransportReassembler()
        self.packet_count = 0
        self.decoded_event_count = 0

    def _open_if_ready(self) -> bool:
        if self._handle is not None:
            return True
        if not self.path.exists():
            return False
        self._handle = self.path.open("rb")
        prefix = self._handle.read(3)
        if prefix.startswith(codecs.BOM_UTF16_LE):
            self._handle.seek(2)
            encoding = "utf-16-le"
        elif prefix.startswith(codecs.BOM_UTF8):
            self._handle.seek(3)
            encoding = "utf-8"
        else:
            self._handle.seek(0)
            encoding = "utf-16-le" if len(prefix) >= 2 and prefix[1] == 0 else "utf-8"
        self._text_decoder = codecs.getincrementaldecoder(encoding)(errors="ignore")
        return True

    def _timestamp(self, match: re.Match[str]) -> float:
        hour, minute, second = (int(match.group(index)) for index in range(1, 4))
        fraction = float(f"0.{match.group(4)}")
        clock = hour * 3600 + minute * 60 + second + fraction
        if self._last_clock_seconds is not None and clock + 43200 < self._last_clock_seconds:
            self._day_offset += 86400.0
        self._last_clock_seconds = clock
        return self._day_offset + clock

    def _finish_frame(self) -> list[LiveDpsEvent]:
        if not self._current_frame:
            return []
        frame = bytes(self._current_frame[: self._current_logged_size or None])
        timestamp = self._current_timestamp
        original_size = self._current_original_size or len(frame)
        self._current_frame.clear()
        packet = decode_captured_frame(timestamp, frame, original_size)
        if packet is None:
            return []
        events: list[LiveDpsEvent] = []
        for logical_packet in self._transport.feed(packet):
            if not self._ledger.accept(logical_packet):
                continue
            self.packet_count += 1
            events.extend(self._decoder.feed_events(logical_packet))
        self.decoded_event_count += len(events)
        return events

    def _consume_line(self, line: str) -> list[LiveDpsEvent]:
        events: list[LiveDpsEvent] = []
        header = self._HEADER_RE.search(line)
        if header is not None:
            events.extend(self._finish_frame())
            self._current_timestamp = self._timestamp(header)
            self._current_original_size = int(header.group(5))
            self._current_logged_size = int(header.group(6))
            return events
        hex_line = self._HEX_RE.search(line)
        if hex_line is None or self._current_logged_size <= 0:
            return events
        compact = re.sub(r"\s+", "", hex_line.group(1))
        if len(compact) % 2:
            return events
        self._current_frame.extend(bytes.fromhex(compact))
        if len(self._current_frame) >= self._current_logged_size:
            events.extend(self._finish_frame())
        return events

    def poll(self) -> list[LiveDpsEvent]:
        if not self._open_if_ready():
            return []
        chunk = self._handle.read()
        if not chunk:
            return []
        self._buffer += self._text_decoder.decode(chunk)
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        events: list[LiveDpsEvent] = []
        for line in lines:
            events.extend(self._consume_line(line.rstrip("\r")))
        return events

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

def auto_capture_confirmation_reason(
    events: list[LiveDpsEvent],
    *,
    detected_nest_name: str = "",
    detected_nest_confidence: str = "",
    detected_map_active: bool = False,
    detected_map_layout: str = "",
    detected_map_id: int = 0,
    detected_map_candidate_id: int = 0,
    detected_map_candidate_name: str = "",
    detected_map_candidate_layout: str = "",
    map_candidate_stable: bool = False,
    map_transition_server_confirmed: bool = False,
) -> str:
    """Return a confirmed tier-3 dungeon boundary for automatic capture.

    ``21 0C`` is generic resource synchronization, while client 07 E5 and
    34-byte transition records describe requests/selections. Neither proves
    that a map load began: a player may browse multiple choices without
    entering any. Only an active, configured server map instruction (including
    the 06 0B load acknowledgement) can open automatic recording. Monster/Boss
    instances are not required, so guests start at the same map-load boundary.
    """

    # Keep ``events`` in the compatibility signature: callers already have the
    # decoded batch available, while the accepted boundary is intentionally
    # determined solely by the decoder's formal-map state.
    _ = events
    # A hub load preserves the previous nest name for score/upload. Validate
    # the current destination instead of treating that retained name as entry.
    if detected_map_id:
        from .dungeon_map_catalog import resolve_dungeon_map_id, is_battle_map
        if not is_battle_map(resolve_dungeon_map_id(detected_map_id)):
            return ""
    resolved_name = str(detected_nest_name or "").strip()
    confidence = str(detected_nest_confidence or "").strip()
    exact_active_map = (
        confidence == "exact_map_id"
        and bool(detected_map_active)
        and str(detected_map_layout or "").startswith("s2c-")
    )
    if resolved_name and exact_active_map:
        return f"正式地图：{resolved_name}"

    return ""


# A supplied guest/accelerator trace did not receive the authoritative Saint
# Haven map initialization until 45.14 seconds after the scene acknowledgement.
# Keeping the transition TCP discovery window at exactly 45 seconds made that
# valid server packet intermittently disappear at the boundary.  This window
# is opened only around a verified map/scene transition, not during ordinary
# combat, so the wider grace period does not add steady-state capture load.
_SCENE_TRANSITION_TCP_DISCOVERY_SECONDS = 90.0
_SESSION_SYNC_TCP_DISCOVERY_SECONDS = 45.0


class _IdentityRediscoveryTracker:
    """Detect the small set of events that can replace player identities.

    The tracker deliberately looks only at party/lifecycle evidence and map
    state.  Combat actors include monsters, summons and temporary objects, so
    treating every newly observed actor id as a player would keep broad TCP
    discovery enabled for an entire fight.
    """

    def __init__(self) -> None:
        self.known_runtime_ids: set[int] = set()
        self.active_runtime_ids: set[int] = set()
        self.last_map_id = 0

    @staticmethod
    def _runtime_id(value: object) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except (TypeError, ValueError):
            return 0

    def observe(
        self,
        events: Iterable[object],
        *,
        previous_map_id: int = 0,
        current_map_id: int = 0,
        scene_transition: bool = False,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if scene_transition:
            reasons.append("scene_transition")
        previous_map = int(previous_map_id or self.last_map_id or 0)
        current_map = int(current_map_id or 0)
        if current_map > 0 and current_map != previous_map:
            reasons.append(
                "enter_dungeon" if previous_map <= 0 else "map_changed"
            )
            self.last_map_id = current_map

        size_before = len(self.active_runtime_ids)
        for event in events:
            event_type = type(event).__name__
            if event_type == "PartyMemberEvent":
                runtime_id = self._runtime_id(getattr(event, "actor_id", ""))
                if runtime_id <= 0:
                    continue
                if runtime_id not in self.known_runtime_ids:
                    self.known_runtime_ids.add(runtime_id)
                    reasons.append("new_runtime_actor")
                self.active_runtime_ids.add(runtime_id)
            elif event_type == "PlayerLifecycleEvent":
                runtime_id = self._runtime_id(getattr(event, "target_id", ""))
                if runtime_id <= 0:
                    continue
                active = bool(getattr(event, "active", False))
                if active:
                    if (
                        runtime_id in self.known_runtime_ids
                        and runtime_id not in self.active_runtime_ids
                    ):
                        reasons.append("teammate_reconnected")
                    elif runtime_id not in self.known_runtime_ids:
                        self.known_runtime_ids.add(runtime_id)
                        reasons.append("new_runtime_actor")
                    self.active_runtime_ids.add(runtime_id)
                else:
                    self.active_runtime_ids.discard(runtime_id)
        if len(self.active_runtime_ids) != size_before:
            reasons.append("party_size_changed")
        return tuple(dict.fromkeys(reasons))


def _is_verified_session_evidence(event: object) -> bool:
    return bool(
        isinstance(event, PlayerHealthEvent)
        and str(event.packet_kind or "")
        == "s2c-udp-player-resource-sync-210c-v576"
        and str(event.target_id or "").startswith("0x")
        and int(event.current_hp or 0) > 0
        and int(event.max_hp or 0) > 0
    )


def _v576_protocol_flow_evidence(
    packet: PacketRecord,
) -> tuple[bool, bool]:
    """Return ``(proven_flow, hold_tcp_for_transition)`` for one packet.

    Map/scene records update decoder state but intentionally emit no combat
    event. They must still teach an accelerator-rewritten port. A client map
    request, authoritative map leave, or scene acknowledgement/teardown also
    opens a bounded TCP discovery window so the next server map confirmation
    or town initialization cannot disappear with a relay switch.
    """

    payload = getattr(packet, "payload", None)
    infer_direction = getattr(packet, "inferred_direction", None)
    if not isinstance(payload, (bytes, bytearray, memoryview)) or not callable(
        infer_direction
    ):
        return False, False
    packet_payload = bytes(payload)
    direction = infer_direction()
    map_identity = decode_v576_map_identity(
        packet_payload,
        direction,
        connection_context=packet,
    )
    transition = decode_v576_lobby_return(packet_payload, direction)
    teardown = decode_v576_scene_teardown(
        packet_payload,
        direction,
        connection_context=packet,
    )
    world_transfer = V576TcpWorldReturnTracker.is_world_transfer_start(packet)
    proven = bool(
        world_transfer
        or map_identity is not None
        or transition is not None
        or teardown is not None
    )
    hold_tcp = bool(
        transition is not None
        or teardown is not None
        or world_transfer
        or (
            map_identity is not None
            and (
                not bool(getattr(map_identity, "authoritative", True))
                or not bool(map_identity.active)
            )
        )
    )
    return proven, hold_tcp


def _may_confirm_game_flow(
    packet: PacketRecord,
    events: Iterable[LiveDpsEvent],
    protocol_flow_proven: bool,
) -> bool:
    """Never learn an arbitrary UDP port from a loose decoded event alone.

    The temporary all-UDP accelerator probe may see unrelated encrypted
    streams. A coincidental event candidate in one such packet must not add
    its port to the permanent Npcap filter. Real UDP damage/status packets
    carry the closed v576 outer length; exact map/scene evidence is separately
    admitted by ``protocol_flow_proven``.
    """

    if protocol_flow_proven:
        return True
    if not events:
        return False
    if str(getattr(packet, "transport", "") or "").upper() != "UDP":
        # Loose event candidates in HTTPS must not permanently teach port
        # 443 (and capture every other program's upload). Rewritten TCP ports
        # remain learnable through verified map/profile/session evidence.
        from .npcap import V576_TCP_SERVICE_PORTS
        return packet.source_port in V576_TCP_SERVICE_PORTS or packet.destination_port in V576_TCP_SERVICE_PORTS
    payload = bytes(packet.payload)
    return len(payload) >= 9 and int.from_bytes(payload[7:9], "little") + 3 == len(payload)


def _verified_session_evidence_events(
    decoder: object,
    packet: PacketRecord,
) -> list[PlayerHealthEvent]:
    """Decode only reliable 21 0C facts without moving decoder battle zero."""

    observer = getattr(decoder, "_player_health_events", None)
    if not callable(observer):
        return []
    original_origin = getattr(decoder, "first_timestamp", None)
    try:
        decoded = observer(packet)
    finally:
        decoder.first_timestamp = original_origin
    return [
        replace(event, timestamp_ms=0, session_evidence=True)
        for event in decoded
        if _is_verified_session_evidence(event)
    ]

class V576AutoCaptureProbe:
    """Memory-only first-stage capture used to confirm dungeon activity.

    It deliberately omits aggregation, SQLite history, UI refresh and disk
    recording. The most recent raw frames are retained so the confirmed fight
    can be handed to ``V576NpcapSource`` without losing the opener.
    """

    def __init__(
        self,
        udp_ports: list[int],
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
        tcp_ports: list[int] | None = None,
        port_provider: Callable[[], tuple[list[int], list[int]]] | None = None,
        *,
        prebuffer_seconds: float = 12.0,
        prebuffer_bytes: int = 32 * 1024 * 1024,
        discover_udp: bool | None = None,
        discover_tcp: bool | None = None,
        known_actor_names: dict[int, str] | None = None,
        identity_only: bool = False,
        entity_only: bool = False,
        prebattle_udp: bool = False,
        arm_tcp_discovery_on_start: bool = True,
    ) -> None:
        self._identity_only = bool(identity_only)
        # 只建实体档案：完整解码（否则 monster_entities 建不起来），但不保留
        # 原始帧、不聚合、不落盘。正式采集会用导出的实体表接上。
        self._entity_only = bool(entity_only)
        self._prebattle_udp = bool(prebattle_udp)
        self._stream = NpcapPacketStream(
            udp_ports,
            trace_path=None,
            tcp_ports=tcp_ports,
            use_environment_trace=False,
        )
        self._decoder = _V576PacketEventDecoder(
            data_root,
            known_players,
            known_actor_names=known_actor_names,
        )
        self._prebattle_state = PrebattleStateTracker()
        self._formal_boundary_timestamp = 0.0
        self._last_session_sync_observed_at = -1.0
        # The always-on identity listener sees hours of lobby TCP traffic but
        # needs deduplication only around the current retransmission window.
        # Keeping the combat-sized 262k ledger caused avoidable long-session
        # memory pressure; transport reassembly already handles TCP retries.
        self._ledger = V576EventLedger(
            max_events=8_192 if (self._identity_only or self._entity_only) else 262_144
        )
        self._transport = V576TransportReassembler(
            # Identity/profile records are only a few KiB.  The combat-sized
            # 4 MiB recovery window made one unrecoverable lobby TCP gap grow
            # for hours and then scan that whole prefix on every 250 ms stale
            # flush.  That backlog is why teammate inspection started fast and
            # gradually reached 10--20 seconds.  Keep a generous 512 KiB
            # identity window and retire sockets that have been silent for a
            # minute; normal combat/replay transports remain unchanged.
            max_buffer_bytes=(
                512 * 1024 if (self._identity_only or self._entity_only)
                else 4 * 1024 * 1024
            ),
            flow_retention_seconds=(
                60.0 if (self._identity_only or self._entity_only) else None
            ),
        )
        self._port_provider = port_provider
        self._discover_udp = (
            bool(udp_ports) if discover_udp is None else bool(discover_udp)
        )
        self._discover_tcp = (
            bool(tcp_ports) if discover_tcp is None else bool(discover_tcp)
        )
        # Direct clients expose their real game sockets in the process table;
        # periodically reopening an all-TCP BPF in that case only captures
        # browser/localhost traffic.  Portless or explicitly armed probes keep
        # the old accelerator discovery loop.
        self._periodic_tcp_discovery = bool(
            self._discover_tcp
            and (bool(arm_tcp_discovery_on_start) or not tuple(tcp_ports or ()))
        )
        # Enumerating the Windows TCP/UDP owner tables is synchronous.  Doing
        # it once per second on this decoder thread can stall packet draining,
        # especially after the tables grow during a long client session.
        # Retry quickly only until at least one game socket is known; stable
        # sessions use a ten-second cache and still follow a reconnect.
        self._next_port_refresh = time.monotonic() + 1.0
        self._prebuffer_seconds = max(3.0, float(prebuffer_seconds))
        self._prebuffer_bytes_limit = max(1024 * 1024, int(prebuffer_bytes))
        self._frames: deque[tuple[float, bytes, int, int]] = deque()
        self._prebuffer_bytes = 0
        self.packet_count = 0
        self.decoded_event_count = 0
        self.decoded_udp_event_count = 0
        self.decoded_tcp_event_count = 0
        self.confirmation_reason = ""
        self.error = ""
        self._profile_events: queue.SimpleQueue[PlayerInspectEvent] = (
            queue.SimpleQueue()
        )
        self._identity_events: queue.SimpleQueue[
            DecodedTcpPlayerSpawnIdentity
        ] = queue.SimpleQueue()
        self._identity_rediscovery = _IdentityRediscoveryTracker()
        self._identity_discovery_triggers: queue.SimpleQueue[str] = (
            queue.SimpleQueue()
        )
        self._identity_capture_groups: queue.SimpleQueue[
            tuple[
                DecodedTcpPlayerSpawnIdentity,
                tuple[tuple[float, bytes, int, int], ...],
            ]
        ] = queue.SimpleQueue()
        self._session_evidence_frames: queue.SimpleQueue[
            tuple[float, bytes, int, int]
        ] = queue.SimpleQueue()
        self._identity_flow_frames: dict[
            tuple[str, str, int, int],
            deque[tuple[float, bytes, int, int]],
        ] = {}
        self._probe_started_at = time.monotonic()
        self._next_accelerator_probe_at = self._probe_started_at + 3.0
        if self._discover_udp:
            hold_discovery = getattr(
                self._stream, "hold_accelerator_discovery", None
            )
            if callable(hold_discovery):
                hold_discovery(12.0)
        if self._periodic_tcp_discovery:
            self._hold_accelerator_tcp_discovery(
                _SESSION_SYNC_TCP_DISCOVERY_SECONDS
                if self._identity_only
                else 12.0
            )
        self._stop = threading.Event()
        self._transferred = False
        self._thread = threading.Thread(
            target=self._run,
            name="DragonNestAutoCaptureProbe",
            daemon=True,
        )
        self._thread.start()

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmation_reason)

    def _hold_accelerator_tcp_discovery(self, duration: float) -> None:
        if not self._discover_tcp:
            return
        if (
            bool(getattr(self, "_identity_only", False))
            or not bool(getattr(self, "_periodic_tcp_discovery", True))
        ):
            # Identity discovery is allowed to stay broad for *up to* the
            # requested window. Direct clients with process-owned ports use
            # the same bounded behavior. Once a real game TCP flow is proven,
            # confirm_game_flow must be able to narrow it immediately; using
            # the combat transition hold here kept every unrelated TCP flow in
            # the queue for 45 seconds and delayed interactive inspections.
            enable_discovery = getattr(
                self._stream, "enable_accelerator_tcp_discovery", None
            )
            if callable(enable_discovery):
                enable_discovery(duration)
                return
        hold_discovery = getattr(
            self._stream, "hold_accelerator_tcp_discovery", None
        )
        if callable(hold_discovery):
            hold_discovery(duration)
            return
        enable_discovery = getattr(
            self._stream, "enable_accelerator_tcp_discovery", None
        )
        if callable(enable_discovery):
            enable_discovery(duration)

    def request_identity_discovery(
        self,
        reason: str,
        duration: float = _SESSION_SYNC_TCP_DISCOVERY_SECONDS,
    ) -> None:
        """Re-open only the memory-only TCP identity discovery window."""

        if not bool(getattr(self, "_discover_tcp", False)):
            return
        normalized = str(reason or "identity_refresh").strip() or "identity_refresh"
        triggers = getattr(self, "_identity_discovery_triggers", None)
        if triggers is None:
            triggers = queue.SimpleQueue()
            self._identity_discovery_triggers = triggers
        triggers.put(normalized)
        self._hold_accelerator_tcp_discovery(duration)

    def export_monster_entities(self) -> dict[int, int]:
        """actor_id -> 模板号（只导 Boss）。

        正式采集新建 decoder 时用它把实体验回来，这样「进本后才开统计」也能
        立刻认出 Boss、出技能事件。
        """

        entities = getattr(self._decoder, "monster_entities", None)
        if not isinstance(entities, dict):
            return {}
        seeds: dict[int, int] = {}
        for actor_id, template in list(entities.items())[-512:]:
            if not getattr(template, "boss", False):
                continue
            template_id = int(getattr(template, "template_id", 0) or 0)
            if template_id > 0:
                seeds[int(actor_id)] = template_id
        return seeds

    def poll_identity_discovery_triggers(self) -> list[str]:
        triggers: list[str] = []
        while True:
            try:
                triggers.append(self._identity_discovery_triggers.get_nowait())
            except queue.Empty:
                return triggers

    def _observe_identity_rediscovery(
        self,
        events: Iterable[object],
        *,
        previous_map_id: int,
        current_map_id: int,
        scene_transition: bool,
    ) -> None:
        tracker = getattr(self, "_identity_rediscovery", None)
        if tracker is None:
            tracker = _IdentityRediscoveryTracker()
            self._identity_rediscovery = tracker
        reasons = tracker.observe(
            events,
            previous_map_id=previous_map_id,
            current_map_id=current_map_id,
            scene_transition=scene_transition,
        )
        if reasons:
            self.request_identity_discovery("+".join(reasons))

    def _remember(self, frame: tuple[float, bytes, int, int]) -> None:
        self._frames.append(frame)
        self._prebuffer_bytes += len(frame[1])
        newest = float(frame[0])
        while self._frames and (
            newest - float(self._frames[0][0]) > self._prebuffer_seconds
            or self._prebuffer_bytes > self._prebuffer_bytes_limit
        ):
            expired = self._frames.popleft()
            self._prebuffer_bytes -= len(expired[1])

    @staticmethod
    def _identity_flow_key(packet: PacketRecord) -> tuple[str, str, int, int]:
        return (
            packet.source_ip,
            packet.destination_ip,
            int(packet.source_port),
            int(packet.destination_port),
        )

    def _remember_identity_flow_frame(
        self,
        packet: PacketRecord,
        raw_frame: tuple[float, bytes, int, int],
    ) -> None:
        if str(packet.transport or "").upper() != "TCP":
            return
        key = self._identity_flow_key(packet)
        recent = self._identity_flow_frames.setdefault(key, deque())
        recent.append(raw_frame)
        newest = float(raw_frame[0])
        while recent and (
            len(recent) > 24
            or newest - float(recent[0][0]) > 1.0
        ):
            recent.popleft()
        if len(self._identity_flow_frames) > 64:
            oldest_key = min(
                self._identity_flow_frames,
                key=lambda candidate: (
                    float(self._identity_flow_frames[candidate][-1][0])
                    if self._identity_flow_frames[candidate]
                    else 0.0
                ),
            )
            if oldest_key != key:
                self._identity_flow_frames.pop(oldest_key, None)

    def _queue_identity_capture_groups(
        self,
        packet: PacketRecord,
        identities: tuple[DecodedTcpPlayerSpawnIdentity, ...],
    ) -> None:
        if not identities:
            return
        frames = tuple(
            self._identity_flow_frames.get(
                self._identity_flow_key(packet), ()
            )
        )
        if not frames:
            return
        for identity in identities:
            self._identity_capture_groups.put((identity, frames))

    def _decode(self, frames: list[tuple[float, bytes, int, int]]) -> str:
        self._reset_paused_prebattle_sources()
        for raw_frame in frames:
            if not bool(getattr(self, "_identity_only", False)) and not bool(
                getattr(self, "_entity_only", False)
            ):
                self._remember(raw_frame)
            timestamp, frame, original_length, _adapter_index = raw_frame
            packet = decode_captured_frame(timestamp, frame, original_length)
            if packet is None:
                continue
            if (getattr(self, "_identity_only", False)
                and not getattr(self, "_prebattle_udp", False)
                and packet.transport.upper() == "UDP"):
                # Suspend before transport decryption/reassembly, not merely
                # before event projection. Formal capture owns this UDP flow.
                continue
            packet_events: list[LiveDpsEvent] = []
            for logical_packet in self._transport.feed(packet):
                if not self._ledger.accept(logical_packet):
                    continue
                self.packet_count += 1
                protocol_flow_proven, hold_tcp = (
                    _v576_protocol_flow_evidence(logical_packet)
                )
                previous_map_id = int(
                    getattr(self._decoder, "detected_map_id", 0) or 0
                )
                logical_events = (
                    (self._decoder.feed_prebattle_events(logical_packet)
                     if getattr(self, "_prebattle_udp", False)
                     else self._decoder.feed_identity_profile_events(logical_packet))
                    if bool(getattr(self, "_identity_only", False))
                    else self._decoder.feed_events(logical_packet)
                )
                current_map_id = int(
                    getattr(self._decoder, "detected_map_id", 0) or 0
                )
                session_evidence_seen = any(
                    _is_verified_session_evidence(event)
                    for event in logical_events
                )
                if hold_tcp:
                    self._hold_accelerator_tcp_discovery(
                        _SESSION_SYNC_TCP_DISCOVERY_SECONDS
                        if bool(getattr(self, "_identity_only", False))
                        else _SCENE_TRANSITION_TCP_DISCOVERY_SECONDS
                    )
                elif session_evidence_seen and not bool(
                    getattr(self, "_identity_only", False)
                ):
                    self._hold_accelerator_tcp_discovery(
                        _SESSION_SYNC_TCP_DISCOVERY_SECONDS
                    )
                self._observe_identity_rediscovery(
                    logical_events,
                    previous_map_id=previous_map_id,
                    current_map_id=current_map_id,
                    scene_transition=hold_tcp,
                )
                if _may_confirm_game_flow(
                    logical_packet, logical_events, protocol_flow_proven
                ):
                    confirm_flow = getattr(
                        self._stream, "confirm_game_flow", None
                    )
                    if callable(confirm_flow):
                        confirm_flow(
                            logical_packet.transport,
                            logical_packet.source_port,
                            logical_packet.destination_port,
                        )
                packet_events.extend(logical_events)
                identities = self._drain_decoder_identities()
                # Compact identity rows are sufficient for CloudBase and the
                # adapter-251 replay appendix. Do not retain surrounding raw
                # TCP frames while no combat recording is active.
            if packet_events:
                self._note_scene_evidence(packet_events, packet.timestamp_seconds)
                self._prebattle_state.observe(
                    packet_events, packet.timestamp_seconds
                )
                self.decoded_event_count += len(packet_events)
                if packet.transport.upper() == "TCP":
                    self.decoded_tcp_event_count += len(packet_events)
                elif packet.transport.upper() == "UDP":
                    self.decoded_udp_event_count += len(packet_events)
                for event in packet_events:
                    if isinstance(event, PlayerInspectEvent):
                        self._profile_events.put(event)
            if any(
                _is_verified_session_evidence(event)
                for event in packet_events
            ):
                self._session_evidence_frames.put(raw_frame)
            if bool(getattr(self, "_identity_only", False)):
                continue
            reason = auto_capture_confirmation_reason(
                packet_events,
                detected_map_id=getattr(self._decoder, "detected_map_id", 0),
                detected_nest_name=getattr(
                    self._decoder, "detected_nest_name", ""
                ),
                detected_nest_confidence=getattr(
                    self._decoder, "detected_nest_confidence", ""
                ),
                detected_map_active=bool(
                    getattr(self._decoder, "detected_map_active", False)
                ),
                detected_map_layout=str(
                    getattr(self._decoder, "detected_map_layout", "") or ""
                ),
            )
            if reason:
                # The formal-map packet/entity is the recording boundary.  All
                # town/Garden traffic remains probe-only, so the handed-off
                # decoder and every live/score timeline start at dungeon 0s.
                self._trim_to_formal_map_boundary(
                    float(raw_frame[0]), confirmation_reason=reason
                )
                return reason
        return ""

    def _map_candidate_stable(self, now_seconds: float) -> bool:
        """Wait briefly so a base-map request can resolve to its mode map."""

        try:
            candidate_id = int(
                getattr(self._decoder, "detected_map_candidate_id", 0) or 0
            )
            observed_at = float(
                getattr(
                    self._decoder,
                    "detected_map_candidate_observed_at",
                    0.0,
                )
                or 0.0
            )
            origin = float(getattr(self._decoder, "first_timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        entry = resolve_dungeon_map_id(candidate_id)
        layout = str(
            getattr(self._decoder, "detected_map_candidate_layout", "") or ""
        )
        if (
            candidate_id <= 0
            or not is_battle_map(entry)
            or not (
                layout.startswith("c2s-udp-map-entry-")
                or layout.startswith("c2s-udp-map-transition-")
            )
            or origin <= 0.0
        ):
            return False
        return float(now_seconds) - (origin + max(0.0, observed_at)) >= 1.25

    def _note_scene_evidence(
        self, events: Iterable[LiveDpsEvent], timestamp_seconds: float
    ) -> None:
        if not any(_is_verified_session_evidence(event) for event in events):
            return
        origin = float(getattr(self._decoder, "first_timestamp", 0.0) or 0.0)
        if origin > 0.0:
            self._last_session_sync_observed_at = max(
                0.0, float(timestamp_seconds) - origin
            )

    def _map_transition_candidate_server_confirmed(
        self, now_seconds: float
    ) -> bool:
        """Accept a 34-byte destination only after the matching server scene.

        This route also occurs on dungeon exit. In particular, an ACK followed
        by the *old* battle map ID is not evidence of entering that map again.
        """

        decoder = self._decoder
        if not self._map_candidate_stable(now_seconds):
            return False
        if not str(
            getattr(decoder, "detected_map_candidate_layout", "") or ""
        ).startswith("c2s-udp-map-transition-"):
            return False
        if int(getattr(decoder, "detected_scene_transition_ack_revision", 0) or 0) <= 0:
            return False
        candidate_id = int(getattr(decoder, "detected_map_candidate_id", 0) or 0)
        candidate_at = float(
            getattr(decoder, "detected_map_candidate_observed_at", 0.0) or 0.0
        )
        ack_at = float(
            getattr(decoder, "detected_scene_transition_ack_observed_at", 0.0)
            or 0.0
        )
        sync_at = float(getattr(self, "_last_session_sync_observed_at", -1.0))
        origin = float(getattr(decoder, "first_timestamp", 0.0) or 0.0)
        elapsed = float(now_seconds) - origin
        if not (
            0.0 <= candidate_at - ack_at <= 3.0
            and candidate_at <= sync_at <= candidate_at + 45.0
            and candidate_at <= elapsed <= candidate_at + 45.0
        ):
            return False
        # The same short ACK and resource sync can occur while *leaving* a
        # dungeon. A verified gateway return defeats a stale source-map ID,
        # unless that return explicitly names this candidate as the next
        # battle map (a valid cross-dungeon entry can include an inferred hub).
        if any(
            int(row.get("source_map_id") or 0) > 0
            and float(row.get("transition_ack_at") or -1.0) >= ack_at
            and int(row.get("next_battle_map_id") or 0) != candidate_id
            for row in (getattr(decoder, "detected_map_history", ()) or ())
        ):
            return False
        # A previously confirmed instance of the same map means this can be
        # the source-map ID sent while leaving it, not a new entry destination.
        if any(
            int(row.get("map_id") or 0) == candidate_id
            and int(row.get("map_type") or 0) == 3
            and float(row.get("observed_at") or 0.0) <= ack_at
            for row in (getattr(decoder, "detected_map_history", ()) or ())
        ):
            return False
        # Never override a newer authoritative town/world-zone observation.
        if any(
            int(row.get("map_type") or 0) in (1, 2)
            and str(row.get("layout") or "").startswith("s2c-")
            and row.get("left_at") is None
            and float(row.get("observed_at") or 0.0) >= candidate_at
            for row in (getattr(decoder, "detected_map_history", ()) or ())
        ):
            return False
        return True

    def _trim_to_formal_map_boundary(
        self, fallback: float, *, confirmation_reason: str = ""
    ) -> None:
        """Discard probe-only frames preceding the resolved dungeon entry."""

        boundary = float(fallback)
        origin = getattr(self._decoder, "first_timestamp", None)
        observed_at = getattr(self._decoder, "detected_nest_observed_at", None)
        try:
            resolved_boundary = float(origin) + max(0.0, float(observed_at))
        except (TypeError, ValueError):
            resolved_boundary = 0.0
        if resolved_boundary > 0.0:
            boundary = resolved_boundary
        self._formal_boundary_timestamp = boundary
        self._frames = deque(
            frame for frame in self._frames if float(frame[0]) >= boundary
        )
        self._prebuffer_bytes = sum(len(frame[1]) for frame in self._frames)

    def _flush_stale_profile_events(self, now_seconds: float) -> str:
        """Release completed TCP inspect messages even when traffic pauses.

        Viewing a teammate can be the final TCP action for several seconds.
        Without this flush the reassembler keeps that response buffered until
        combat capture starts or another packet arrives, which made the
        disk-free teammate panel appear dependent on 开始实时统计.
        """

        self._reset_paused_prebattle_sources()
        packet_events: list[LiveDpsEvent] = []
        for logical_packet in self._transport.flush_stale(now_seconds):
            if not self._ledger.accept(logical_packet):
                continue
            self.packet_count += 1
            protocol_flow_proven, hold_tcp = (
                _v576_protocol_flow_evidence(logical_packet)
            )
            previous_map_id = int(
                getattr(self._decoder, "detected_map_id", 0) or 0
            )
            logical_events = (
                (self._decoder.feed_prebattle_events(logical_packet)
                 if getattr(self, "_prebattle_udp", False)
                 else self._decoder.feed_identity_profile_events(logical_packet))
                if bool(getattr(self, "_identity_only", False))
                else self._decoder.feed_events(logical_packet)
            )
            current_map_id = int(
                getattr(self._decoder, "detected_map_id", 0) or 0
            )
            prebattle_state = getattr(self, "_prebattle_state", None)
            if prebattle_state is not None:
                prebattle_state.observe(
                    logical_events,
                    getattr(logical_packet, "timestamp_seconds", now_seconds),
                )
            session_evidence_seen = any(
                _is_verified_session_evidence(event)
                for event in logical_events
            )
            if hold_tcp:
                self._hold_accelerator_tcp_discovery(
                    _SESSION_SYNC_TCP_DISCOVERY_SECONDS
                    if bool(getattr(self, "_identity_only", False))
                    else _SCENE_TRANSITION_TCP_DISCOVERY_SECONDS
                )
            elif session_evidence_seen and not bool(
                getattr(self, "_identity_only", False)
            ):
                self._hold_accelerator_tcp_discovery(
                    _SESSION_SYNC_TCP_DISCOVERY_SECONDS
                )
            self._observe_identity_rediscovery(
                logical_events,
                previous_map_id=previous_map_id,
                current_map_id=current_map_id,
                scene_transition=hold_tcp,
            )
            identities = self._drain_decoder_identities()
            self._queue_identity_capture_groups(logical_packet, identities)
            packet_events.extend(logical_events)
            self._note_scene_evidence(
                logical_events,
                getattr(logical_packet, "timestamp_seconds", now_seconds),
            )
            if _may_confirm_game_flow(
                logical_packet, logical_events, protocol_flow_proven
            ):
                transport = str(
                    getattr(logical_packet, "transport", "TCP") or "TCP"
                ).upper()
                if logical_events and transport == "TCP":
                    self.decoded_tcp_event_count = (
                        int(getattr(self, "decoded_tcp_event_count", 0))
                        + len(logical_events)
                    )
                elif logical_events and transport == "UDP":
                    self.decoded_udp_event_count = (
                        int(getattr(self, "decoded_udp_event_count", 0))
                        + len(logical_events)
                    )
                stream = getattr(self, "_stream", None)
                confirm_flow = getattr(stream, "confirm_game_flow", None)
                if callable(confirm_flow):
                    confirm_flow(
                        transport,
                        getattr(logical_packet, "source_port", 0),
                        getattr(logical_packet, "destination_port", 0),
                    )
        if packet_events:
            self.decoded_event_count += len(packet_events)
            for event in packet_events:
                if isinstance(event, PlayerInspectEvent):
                    self._profile_events.put(event)
        if bool(getattr(self, "_identity_only", False)):
            return ""
        reason = auto_capture_confirmation_reason(
            packet_events,
            detected_map_id=getattr(self._decoder, "detected_map_id", 0),
            detected_nest_name=getattr(
                self._decoder, "detected_nest_name", ""
            ),
            detected_nest_confidence=getattr(
                self._decoder, "detected_nest_confidence", ""
            ),
            detected_map_active=bool(
                getattr(self._decoder, "detected_map_active", False)
            ),
            detected_map_layout=str(
                getattr(self._decoder, "detected_map_layout", "") or ""
            ),
        )
        if reason:
            # A complete TCP map record can be released by stale-flush rather
            # than the raw-frame loop. Use the decoder's original observation
            # time so delayed reassembly cannot move the recording zero later.
            self._trim_to_formal_map_boundary(
                now_seconds, confirmation_reason=reason
            )
        return reason

    def poll_profiles(self) -> list[PlayerInspectEvent]:
        """Return newly inspected profiles without retaining combat events.

        Both dungeon auto-detection and the standalone teammate-panel listener
        use this memory-only probe.  Exposing only profile events keeps the
        latter independent from combat aggregation and diagnostic recording.
        """

        profiles: list[PlayerInspectEvent] = []
        while True:
            try:
                profiles.append(self._profile_events.get_nowait())
            except queue.Empty:
                return profiles

    def _drain_decoder_identities(
        self,
    ) -> tuple[DecodedTcpPlayerSpawnIdentity, ...]:
        drain = getattr(
            self._decoder, "drain_player_spawn_identities", None
        )
        if not callable(drain):
            return ()
        identity_events = getattr(self, "_identity_events", None)
        if identity_events is None:
            identity_events = queue.SimpleQueue()
            self._identity_events = identity_events
        identities = tuple(drain())
        for identity in identities:
            identity_events.put(identity)
        return identities

    def poll_identities(self) -> list[DecodedTcpPlayerSpawnIdentity]:
        """Return exact profile/runtime/name bindings seen by this probe."""

        identities: list[DecodedTcpPlayerSpawnIdentity] = []
        identity_events = getattr(self, "_identity_events", None)
        if identity_events is None:
            return identities
        while True:
            try:
                identities.append(identity_events.get_nowait())
            except queue.Empty:
                return identities

    def poll_identity_capture_frames(
        self,
    ) -> list[
        tuple[
            DecodedTcpPlayerSpawnIdentity,
            tuple[tuple[float, bytes, int, int], ...],
        ]
    ]:
        """Return original TCP frame groups proving each identity binding."""

        groups: list[
            tuple[
                DecodedTcpPlayerSpawnIdentity,
                tuple[tuple[float, bytes, int, int], ...],
            ]
        ] = []
        capture_groups = getattr(self, "_identity_capture_groups", None)
        if capture_groups is None:
            return groups
        while True:
            try:
                groups.append(capture_groups.get_nowait())
            except queue.Empty:
                return groups

    def poll_session_evidence_frames(
        self,
    ) -> list[tuple[float, bytes, int, int]]:
        """Return newly verified pre-boundary 21 0C raw frames."""

        frames: list[tuple[float, bytes, int, int]] = []
        evidence_frames = getattr(self, "_session_evidence_frames", None)
        if evidence_frames is None:
            return frames
        while True:
            try:
                frames.append(evidence_frames.get_nowait())
            except queue.Empty:
                return frames

    def _run(self) -> None:
        # This decoder owns the explicit 查看装备 interaction when running in
        # identity-only mode.  Leaving it below normal priority made a busy
        # client drain already-captured requests seconds late.
        if not bool(getattr(self, "_identity_only", False)):
            set_current_thread_low_priority()
        last_stale_flush = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self._port_provider is not None and now >= self._next_port_refresh:
                    try:
                        udp_ports, tcp_ports = self._port_provider()
                        self._stream.update_ports(udp_ports, tcp_ports)
                        self._next_port_refresh = now + (
                            10.0 if (udp_ports or tcp_ports) else 1.0
                        )
                    except Exception:
                        self._next_port_refresh = now + 1.0
                stream_error = self._stream.read_error()
                if stream_error:
                    self.error = stream_error
                    return
                frames = self._stream.read_packets(limit=256)
                if frames:
                    reason = self._decode(frames)
                    if reason:
                        self.confirmation_reason = reason
                        return
                else:
                    self._stop.wait(0.02)
                now = time.monotonic()
                if now - last_stale_flush >= 0.25:
                    reason = self._flush_stale_profile_events(time.time())
                    last_stale_flush = now
                    if reason:
                        self.confirmation_reason = reason
                        return
                if now >= self._next_accelerator_probe_at:
                    # Keep UDP discovery alive until a trusted dungeon
                    # boundary is found.  A lobby/buff packet can be valid yet
                    # belong to the relay flow that an accelerator replaces
                    # on map entry; treating that first event as permanent
                    # proof made later combat traffic intermittently vanish.
                    if self._discover_udp:
                        hold_discovery = getattr(
                            self._stream,
                            "hold_accelerator_discovery",
                            None,
                        )
                        if callable(hold_discovery):
                            hold_discovery(12.0)
                        else:
                            self._stream.enable_accelerator_discovery(12.0)
                    if self._periodic_tcp_discovery and not bool(
                        getattr(self, "_identity_only", False)
                    ):
                        # A decoded lobby/profile packet proves only that TCP
                        # flow.  Accelerators may open a different scene flow
                        # afterwards.  Guests do not send the leader's map
                        # selection packet that used to reopen discovery, so
                        # keep the bounded scene probe alive until either
                        # server evidence confirms the formal map or this
                        # probe is handed off.
                        self._hold_accelerator_tcp_discovery(12.0)
                    self._next_accelerator_probe_at = now + 10.0
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def set_prebattle_udp_enabled(self, enabled: bool) -> None:
        if getattr(self, "_prebattle_udp", False) and not enabled:
            # The formal decoder now owns refresh/death/clear observations.
            # Do not replay the idle decoder's old synthetic aura sources when
            # returning to town. Its packet-backed shared ledger stays intact.
            self._prebattle_decoder_reset = True
        self._prebattle_udp = bool(enabled)

    def _reset_paused_prebattle_sources(self) -> None:
        if not getattr(self, "_prebattle_decoder_reset", False):
            return
        self._prebattle_decoder_reset = False
        for name in (
            "active_permanent_player_buffs", "prebattle_permanent_player_buffs",
            "permanent_player_buff_targets", "permanent_player_buff_known_actors",
            "active_azuna_breath_refresh_sources",
        ):
            ledger = getattr(self._decoder, name, None)
            if ledger is not None:
                ledger.clear()

    def prebattle_snapshot(self, boundary_timestamp: float) -> dict[str, object]:
        return self._prebattle_state.snapshot(boundary_timestamp)

    def prepare_manual_boundary(self, boundary_timestamp: float) -> None:
        self._manual_boundary_timestamp = float(boundary_timestamp)

    def handoff(
        self,
    ) -> tuple[NpcapPacketStream, tuple[tuple[float, bytes, int, int], ...]]:
        self._stop.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise NpcapError("自动监听探针未能及时移交")
        if self.error:
            raise NpcapError(self.error)
        set_boundary_evidence = getattr(
            self._stream,
            "set_auto_capture_boundary_evidence",
            None,
        )
        export_boundary_evidence = getattr(
            self._decoder,
            "capture_boundary_evidence",
            None,
        )
        boundary_evidence: dict[str, object] = {}
        if callable(export_boundary_evidence):
            boundary_evidence = dict(export_boundary_evidence() or {})
        boundary_timestamp = float(
            getattr(self, "_manual_boundary_timestamp", 0.0)
            or getattr(self, "_formal_boundary_timestamp", 0.0)
            or (
                min((float(frame[0]) for frame in self._frames), default=0.0)
            )
        )
        boundary_evidence["boundary_timestamp"] = boundary_timestamp
        tracker = getattr(self, "_prebattle_state", None)
        prebattle_state = (
            tracker.snapshot(boundary_timestamp)
            if tracker is not None
            else {
                "version": 1,
                "boundary_timestamp": boundary_timestamp,
                "player_buffs": [],
            }
        )
        export_summons = getattr(
            self._decoder, "capture_prebattle_summon_state", None
        )
        if callable(export_summons):
            prebattle_state["summons"] = list(
                export_summons(boundary_timestamp) or ()
            )
        boundary_evidence["prebattle_state"] = prebattle_state
        if callable(set_boundary_evidence):
            set_boundary_evidence(boundary_evidence)
        # When a guest starts from the server's formal-entity fallback there
        # may be no 82 45 map confirmation yet.  Keep TCP discovery open long
        # enough to learn the accelerator's scene flow; later map-release and
        # exact hub packets can then stop the recording without a leader-only
        # client selection packet.
        if (
            self._discover_tcp
            and (
                int(boundary_evidence.get("map_id") or 0) <= 0
                or not str(
                    boundary_evidence.get("map_layout") or ""
                ).startswith("s2c-")
            )
        ):
            self._hold_accelerator_tcp_discovery(
                _SCENE_TRANSITION_TCP_DISCOVERY_SECONDS
            )
        self._transferred = True
        return self._stream, tuple(self._frames)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if not self._transferred:
            self._stream.close()

def _decoder_requires_normal_priority(
    pending_packets: int,
    queue_lag_ms: float,
    *,
    currently_normal: bool,
) -> bool:
    """Use hysteresis so brief packet bursts do not thrash thread priority."""

    pending = max(0, int(pending_packets))
    lag_ms = max(0.0, float(queue_lag_ms))
    if currently_normal:
        # Stay responsive until both the raw queue and its time watermark are
        # effectively caught up.  A low packet count alone is insufficient:
        # one large TCP frame can still contain expensive nested records.
        return pending > 64 or lag_ms > 100.0
    return pending >= 256 or lag_ms >= 250.0


class V576NpcapSource:
    """Decode Dragon Nest combat events directly from an Npcap stream."""

    def __init__(
        self,
        udp_ports: list[int],
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
        tcp_ports: list[int] | None = None,
        port_provider: Callable[[], tuple[list[int], list[int]]] | None = None,
        stream: NpcapPacketStream | None = None,
        preloaded_frames: tuple[tuple[float, bytes, int, int], ...] = (),
        known_actor_names: dict[int, str] | None = None,
        identity_capture_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = (),
        session_evidence_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = (),
        prebattle_state: dict[str, object] | None = None,
        prebattle_tracker: PrebattleStateTracker | None = None,
        toggles: Any | None = None,
        attack_target_sink: Callable[[str, str, int], None] | None = None,
        seed_monster_entities: dict[int, int] | None = None,
    ) -> None:
        # The countdown build keeps no capture history: the raw trace is a
        # short-lived working file under %TEMP% and is deleted when the source
        # closes. Nothing is written to Documents or any user capture folder.
        trace_dir = Path(tempfile.gettempdir()) / "DragonNestACT" / "working"
        trace_dir.mkdir(parents=True, exist_ok=True)
        # A previous session that was killed cannot delete its own working
        # trace; purge only our own pattern here so nothing accumulates.
        for stale in trace_dir.glob("live_*.dpscap"):
            try:
                stale.unlink()
            except OSError:
                pass
        trace_path = trace_dir / (
            f"live_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.dpscap"
        )
        self._stream = stream or (
            NpcapPacketStream(udp_ports, trace_path=None, tcp_ports=tcp_ports, use_environment_trace=False)
            if prebattle_state else
            NpcapPacketStream(udp_ports, trace_path=trace_path, tcp_ports=tcp_ports)
        )
        if stream is None and prebattle_state:
            self._stream.set_auto_capture_boundary_evidence({
                "boundary_timestamp": prebattle_state.get("boundary_timestamp", 0.0),
                "prebattle_state": prebattle_state,
            })
        if stream is not None or prebattle_state:
            preloaded_frames = self._stream.start_trace(
                trace_path, preloaded_frames
            )
        set_identity_trace_frames = getattr(
            self._stream, "set_identity_trace_frames", None
        )
        if callable(set_identity_trace_frames):
            set_identity_trace_frames(identity_capture_frames)
        set_identity_trace_bindings = getattr(
            self._stream, "set_identity_trace_bindings", None
        )
        if callable(set_identity_trace_bindings):
            set_identity_trace_bindings(known_actor_names or {})
        set_session_evidence_trace_frames = getattr(
            self._stream, "set_session_evidence_trace_frames", None
        )
        if callable(set_session_evidence_trace_frames):
            set_session_evidence_trace_frames(session_evidence_frames)
        self._port_provider = port_provider
        self._next_port_refresh = time.monotonic() + 1.0
        self.diagnostic_trace_path = trace_path
        self._decoder = _V576PacketEventDecoder(
            data_root,
            known_players,
            learned_profile_path=(
                trace_dir.parent / "skill_damage_profiles.json"
            ),
            known_actor_names=known_actor_names,
        )
        self._prebattle_tracker = prebattle_tracker
        self._toggles = toggles
        if seed_monster_entities:
            # 待机探针收集的 Boss 实体档案：进本后才开统计时靠它把实体接上。
            self._decoder.seed_monster_entities(seed_monster_entities)
        # 分边副本靠「本机最后一次攻击的目标」判断我在打哪只 Boss，而这个
        # 信息只存在于伤害事件里。轻量版不把伤害事件交给聚合器（不做 DPS），
        # 所以单独留一个只记账的旁路，见 _publish_events。
        self._attack_target_sink = attack_target_sink
        from .voice_signals import VoiceSignalFeed
        self._decoder.voice_signal_feed = VoiceSignalFeed()
        prebattle_events: list[LiveDpsEvent] = []
        take_boundary_evidence = getattr(
            self._stream,
            "take_auto_capture_boundary_evidence",
            None,
        )
        seed_boundary_evidence = getattr(
            self._decoder,
            "seed_capture_boundary_evidence",
            None,
        )
        if callable(take_boundary_evidence) and callable(seed_boundary_evidence):
            prebattle_events.extend(
                seed_boundary_evidence(take_boundary_evidence()) or ()
            )
        self._ledger = V576EventLedger()
        self._transport = V576TransportReassembler()
        self.packet_count = 0
        self.udp_packet_count = 0
        self.tcp_packet_count = 0
        self.decoded_event_count = 0
        self.decoded_udp_event_count = 0
        self.decoded_tcp_event_count = 0
        # A source starts before the first outgoing hit is proven.  Scene
        # boundaries reset this flag so an accelerator may expose a new UDP
        # combat flow even when its previous lobby flow decoded successfully.
        self._awaiting_udp_combat_flow = True
        # TCP discovery is deliberately broad because an accelerator can
        # rewrite the scene port. A 21 0C resource sync used to extend it on
        # every refresh, which kept unrelated localhost TCP traffic enabled
        # for an entire encounter. Only a verified scene transition may arm
        # that extension now; an authoritative server map clears it.
        self._awaiting_tcp_scene_flow = False
        self._tcp_session_extension_used = False
        self.accelerator_flow_recovery_count = 0
        self._capture_started_at = time.monotonic()
        self._next_accelerator_probe_at = self._capture_started_at + 3.0
        # Keep normal bursts in RAM, but never let a slow UI/full snapshot grow
        # this handoff without bound. Overflow is losslessly spooled to a
        # temporary file, so scoring and saved captures keep every event.
        self._decoded_events: BoundedSpillQueue[
            LiveDpsEvent
        ] = BoundedSpillQueue(
            memory_items=32_768,
            memory_bytes=24 * 1024 * 1024,
            size_of=lambda _event: 768,
        )
        self._identity_events: queue.SimpleQueue[
            DecodedTcpPlayerSpawnIdentity
        ] = queue.SimpleQueue()
        self._identity_rediscovery = _IdentityRediscoveryTracker()
        self._identity_discovery_triggers: queue.SimpleQueue[str] = (
            queue.SimpleQueue()
        )
        self._profile_identity_events_seen: set[
            tuple[int, int, str, str]
        ] = set()
        self._decode_errors: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._decode_stop = threading.Event()
        self._port_results = queue.SimpleQueue()
        self._port_refresh_ms = 0.0
        self._port_refresh_thread = None
        self._decode_finalized = False
        self._stream_closed = False
        # Stop must not wait for the decoder to finish an entire large batch.
        # When shutdown is requested the worker hands the unvisited suffix of
        # its current batch back to ``flush_pending``.  The caller then drains
        # that suffix together with the stream queue exactly once, preserving
        # every tail frame without leaving the capture in ``stopping``.
        self._shutdown_pending_lock = threading.Lock()
        self._shutdown_pending_frames: list[
            tuple[float, bytes, int, int]
        ] = []
        # The overlay clock follows the newest raw capture frame, not the
        # newest event that the decoder happened to finish processing.  Keep
        # this sample independent from the event queue so a busy decoder
        # cannot make a long encounter drift progressively behind/ahead.
        self._capture_clock_lock = threading.Lock()
        self._capture_clock_revision = 0
        self._latest_frame_timestamp = 0.0
        self._latest_decoded_frame_timestamp = 0.0
        self._latest_decoded_monotonic = 0.0
        self._latest_capture_to_decode_ms = 0.0
        self._decoder_priority_mode = "normal"
        # Probe hand-off frames are already ordered before the stream queue.
        # Decode them at the head of the worker instead of synchronously in
        # this constructor.  A delayed Npcap reader can otherwise make
        # ``start_npcap`` spend many seconds replaying its catch-up buffer
        # while the UI still says that it is looking for a port.
        self._initial_frames = list(preloaded_frames)
        self._initial_frames_remaining = len(self._initial_frames)
        self._initial_decode_ms = 0.0
        self._decode_thread = threading.Thread(
            target=self._decode_loop,
            name="DragonNestDpsDecode",
            daemon=True,
        )
        if prebattle_events:
            self._publish_events(prebattle_events)
        if session_evidence_frames:
            self._publish_events(
                self._decode_session_evidence_frames(
                    session_evidence_frames
                )
            )
        self._decode_thread.start()
        if self._port_provider is not None:
            self._port_refresh_thread = threading.Thread(
                target=self._port_refresh_loop, name="DragonNestPortRefresh", daemon=True,
            )
            self._port_refresh_thread.start()

    def _port_refresh_loop(self) -> None:
        """OS endpoint enumeration must never block ordered packet decoding."""
        while not self._decode_stop.is_set():
            started = time.monotonic()
            delay = 1.0
            try:
                udp_ports, tcp_ports = self._port_provider()
                if self._decode_stop.is_set():
                    return
                self._port_results.put((udp_ports, tcp_ports))
                delay = 10.0 if (udp_ports or tcp_ports) else 1.0
            except Exception:
                pass  # retain the last known ports; RAW capture stays intact
            self._port_refresh_ms = (time.monotonic() - started) * 1000.0
            if self._decode_stop.wait(delay):
                return

    def _apply_port_refresh(self) -> None:
        latest = None
        while True:
            try:
                latest = self._port_results.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._stream.update_ports(*latest)

    def _decode_session_evidence_frames(
        self,
        frames: tuple[tuple[float, bytes, int, int], ...],
    ) -> list[PlayerHealthEvent]:
        events: list[PlayerHealthEvent] = []
        for timestamp, frame, original_length, _adapter_index in frames:
            packet = decode_captured_frame(timestamp, frame, original_length)
            if packet is None:
                continue
            events.extend(
                _verified_session_evidence_events(self._decoder, packet)
            )
        return events

    def _hold_accelerator_udp_discovery(self, duration: float) -> None:
        hold_discovery = getattr(
            self._stream, "hold_accelerator_discovery", None
        )
        if callable(hold_discovery):
            if hold_discovery(duration):
                self.accelerator_flow_recovery_count += 1
            return
        enable_discovery = getattr(
            self._stream, "enable_accelerator_discovery", None
        )
        if callable(enable_discovery) and enable_discovery(duration):
            self.accelerator_flow_recovery_count += 1

    def _hold_accelerator_tcp_discovery(self, duration: float) -> None:
        hold_discovery = getattr(
            self._stream, "hold_accelerator_tcp_discovery", None
        )
        if callable(hold_discovery):
            if hold_discovery(duration):
                self.accelerator_flow_recovery_count += 1
            return
        enable_discovery = getattr(
            self._stream, "enable_accelerator_tcp_discovery", None
        )
        if callable(enable_discovery) and enable_discovery(duration):
            self.accelerator_flow_recovery_count += 1

    def _observe_udp_flow_transition(
        self,
        packet_events: list[LiveDpsEvent],
        *,
        map_changed: bool,
    ) -> None:
        boundary_seen = any(
            isinstance(event, PlayerHealthEvent)
            and str(getattr(event, "packet_kind", "") or "")
            == "s2c-udp-player-resource-sync-210c-v576"
            for event in packet_events
        )
        cast_seen = any(
            isinstance(event, SkillCastEvent) for event in packet_events
        )
        damage_seen = any(
            isinstance(event, CombatEvent)
            and int(getattr(event, "amount", 0) or 0) > 0
            for event in packet_events
        )
        if boundary_seen or map_changed:
            self._awaiting_udp_combat_flow = True
            # Slow loads in the supplied accelerator traces reached the combat
            # socket more than twenty seconds after 21 0C.  The upper bound is
            # still short and automatically narrows after the grace period.
            self._hold_accelerator_udp_discovery(45.0)
        elif self._awaiting_udp_combat_flow and cast_seen:
            # Manual capture may begin after 21 0C/map-spawn was missed.  A
            # real skill input is a cheap last chance to expose the parallel
            # server damage flow before its response arrives.
            self._hold_accelerator_udp_discovery(12.0)
        if damage_seen:
            self._awaiting_udp_combat_flow = False

    def _observe_tcp_flow_transition(
        self,
        *,
        protocol_flow_proven: bool,
        hold_tcp: bool,
        session_evidence_seen: bool,
        transport: str,
    ) -> None:
        """Bound broad TCP discovery to one verified scene transition."""

        if hold_tcp:
            self._awaiting_tcp_scene_flow = True
            self._tcp_session_extension_used = False
            self._hold_accelerator_tcp_discovery(
                _SCENE_TRANSITION_TCP_DISCOVERY_SECONDS
            )
        elif protocol_flow_proven and str(transport or "").upper() == "TCP":
            # In ``_v576_protocol_flow_evidence`` the only proven TCP record
            # with ``hold_tcp == False`` is an authoritative active map.
            self._awaiting_tcp_scene_flow = False
            self._tcp_session_extension_used = False
        elif session_evidence_seen and bool(
            getattr(self, "_awaiting_tcp_scene_flow", False)
        ) and not bool(getattr(self, "_tcp_session_extension_used", False)):
            self._tcp_session_extension_used = True
            self._hold_accelerator_tcp_discovery(
                _SESSION_SYNC_TCP_DISCOVERY_SECONDS
            )

    def _decode_logical_packets(
        self, logical_packets: tuple[PacketRecord, ...]
    ) -> list[LiveDpsEvent]:
        events: list[LiveDpsEvent] = []
        for logical_packet in logical_packets:
            if not self._ledger.accept(logical_packet):
                continue
            self.packet_count += 1
            protocol_flow_proven, hold_tcp = (
                _v576_protocol_flow_evidence(logical_packet)
            )
            previous_map_id = int(
                getattr(self._decoder, "detected_map_id", 0) or 0
            )
            packet_events = self._decoder.feed_events(logical_packet)
            session_evidence_seen = any(
                _is_verified_session_evidence(event)
                for event in packet_events
            )
            self._observe_tcp_flow_transition(
                protocol_flow_proven=protocol_flow_proven,
                hold_tcp=hold_tcp,
                session_evidence_seen=session_evidence_seen,
                transport=str(logical_packet.transport or ""),
            )
            current_map_id = int(
                getattr(self._decoder, "detected_map_id", 0) or 0
            )
            identity_reasons = self._identity_rediscovery.observe(
                packet_events,
                previous_map_id=previous_map_id,
                current_map_id=current_map_id,
                scene_transition=hold_tcp,
            )
            for reason in identity_reasons:
                self._identity_discovery_triggers.put(reason)
            drain_identities = getattr(
                self._decoder, "drain_player_spawn_identities", None
            )
            if callable(drain_identities):
                identities = tuple(drain_identities())
                for identity in identities:
                    self._identity_events.put(identity)
                if identities:
                    bindings: dict[int, str] = {}
                    profile_bindings: list[dict[str, object]] = []
                    for identity in identities:
                        name = str(identity.character_name or "").strip()
                        if not name:
                            continue
                        bindings[int(identity.profile_id)] = name
                        bindings[int(identity.runtime_actor_id)] = name
                        actor_job = getattr(self._decoder, "actor_jobs", {}).get(
                            int(identity.runtime_actor_id), ("", -1)
                        )
                        job = str(
                            actor_job[0]
                            if isinstance(actor_job, (tuple, list)) and actor_job
                            else actor_job or ""
                        ).strip()
                        profile_bindings.append({
                            "profile_id": int(identity.profile_id),
                            "runtime_actor_id": int(identity.runtime_actor_id),
                            "character_name": name,
                            "job": job,
                            "source": "packet",
                        })
                    stage_bindings = getattr(
                        self._stream, "set_identity_trace_bindings", None
                    )
                    if callable(stage_bindings):
                        stage_bindings(bindings)
                    self.stage_profile_identity_bindings(profile_bindings)
            self.decoded_event_count += len(packet_events)
            transport = str(logical_packet.transport or "").upper()
            if transport == "UDP":
                self._observe_udp_flow_transition(
                    packet_events,
                    map_changed=(
                        current_map_id > 0
                        and current_map_id != previous_map_id
                    ),
                )
            if _may_confirm_game_flow(
                logical_packet, packet_events, protocol_flow_proven
            ):
                if transport == "TCP":
                    self.decoded_tcp_event_count += len(packet_events)
                elif transport == "UDP":
                    self.decoded_udp_event_count += len(packet_events)
                confirm_flow = getattr(self._stream, "confirm_game_flow", None)
                if callable(confirm_flow):
                    confirm_flow(
                        logical_packet.transport,
                        logical_packet.source_port,
                        logical_packet.destination_port,
                    )
            events.extend(packet_events)
        return events

    def _publish_events(self, events: list[LiveDpsEvent]) -> None:
        tracker = getattr(self, "_prebattle_tracker", None)
        origin = getattr(self._decoder, "first_timestamp", None) if tracker is not None else None
        toggles = getattr(self, "_toggles", None)
        sink = getattr(self, "_attack_target_sink", None)
        from .capture_toggles import should_publish
        for event in events:
            # 伤害事件不进聚合器（轻量版不做 DPS/曲线），但「某玩家最后一次打的是谁」
            # 是分边副本识别「我在打朱武还是卡基夫」的唯一依据。这里只记一条目标，
            # 不累计伤害、不建曲线，成本是一次 dict 写。
            if isinstance(event, CombatEvent):
                if sink is not None and getattr(event, "target_id", "") and getattr(event, "amount", 0) > 0:
                    # 名字也要带出去：轻量版没有血量行，「Boss:」这一行的名字
                    # 只能靠伤害事件里的 target_name。
                    try:
                        sink(
                            event.source_id,
                            event.target_id,
                            event.timestamp_ms,
                            getattr(event, "target_name", ""),
                            # 伤重量：白名单要选「15 秒内主要打谁」，只靠
                            # 「最后命中」会被 AOE 的末位单位带偏。
                            float(getattr(event, "amount", 0) or 0),
                        )
                    except TypeError:
                        sink(event.source_id, event.target_id, event.timestamp_ms)
                continue
            # 只按事件类别裁剪（本轻量版只保留 buff / Boss 技能两类），并尊重首页
            # 两个总开关：关掉就停止该类事件的聚合与推送。解码本身停不了——语音
            # 走解码器里独立的 voice_signal_feed，关掉悬浮窗后仍要照常播报。
            if not should_publish(event, toggles):
                continue
            if origin is not None and isinstance(event, (BossStatusEvent, PlayerLifecycleEvent, PlayerHealthEvent, BossActionEvent)):
                tracker.observe((event,), float(origin) + event.timestamp_ms / 1000.0)
            self._decoded_events.put(event)

    def _decode_frames(
        self,
        frames: list[tuple[float, bytes, int, int]],
        *,
        interruptible: bool = False,
    ) -> None:
        wall, cpu = time.perf_counter(), time.thread_time()
        try:
            self._decode_frames_ordered(frames, interruptible=interruptible)
        finally:
            self._decode_batch_timing = {
                "decodeBatchInputFrames": len(frames),
                "decodeBatchWallMs": round((time.perf_counter() - wall) * 1000, 3),
                "decodeBatchCpuMs": round((time.thread_time() - cpu) * 1000, 3),
            }

    def _decode_frames_ordered(
        self,
        frames: list[tuple[float, bytes, int, int]],
        *,
        interruptible: bool = False,
    ) -> None:
        if frames:
            newest = max(float(row[0]) for row in frames)
            with self._capture_clock_lock:
                if newest > self._latest_frame_timestamp:
                    self._latest_frame_timestamp = newest
                    self._capture_clock_revision += 1
        for index, (
            timestamp,
            frame,
            original_length,
            _adapter_index,
        ) in enumerate(frames):
            if interruptible and self._decode_stop.is_set():
                with self._shutdown_pending_lock:
                    self._shutdown_pending_frames.extend(frames[index:])
                return
            # Mark every dequeued frame before protocol decoding.  Invalid or
            # unrelated Ethernet frames must still advance the decode-watermark;
            # otherwise diagnostics report a fictitious decoder backlog whenever
            # the newest captured frame is not a Dragon Nest packet.
            decoded_monotonic = time.monotonic()
            with self._capture_clock_lock:
                if timestamp >= self._latest_decoded_frame_timestamp:
                    self._latest_decoded_frame_timestamp = float(timestamp)
                    self._latest_decoded_monotonic = decoded_monotonic
                    self._latest_capture_to_decode_ms = max(
                        0.0, (time.time() - float(timestamp)) * 1000.0
                    )
            packet = decode_captured_frame(timestamp, frame, original_length)
            if packet is None:
                continue
            if packet.transport.upper() == "TCP":
                self.tcp_packet_count += 1
            elif packet.transport.upper() == "UDP":
                self.udp_packet_count += 1
            packet_events = self._decode_logical_packets(
                self._transport.feed(packet)
            )
            if any(
                _is_verified_session_evidence(event)
                for event in packet_events
            ):
                remember_evidence = getattr(
                    self._stream,
                    "remember_session_evidence_trace_frame",
                    None,
                )
                if callable(remember_evidence):
                    remember_evidence(
                        (
                            float(timestamp),
                            bytes(frame),
                            int(original_length),
                            int(_adapter_index),
                        )
                    )
            if packet_events:
                self._publish_events(packet_events)

    def _decode_loop(self) -> None:
        """Decode capture batches away from Qt's UI thread."""

        # Catch up the short, ordered probe hand-off at normal priority.  Once
        # it is drained, ordinary continuous decoding yields to the game.
        initial_frames = self._initial_frames
        self._initial_frames = []
        try:
            initial_started = time.monotonic()
            # Bound handoff work just like live work. Keep FIFO ordering, expose
            # progress after each batch, and preserve every unvisited frame on
            # Stop. Do not lower priority between handoff and live catch-up.
            for offset in range(0, len(initial_frames), 128):
                end = min(offset + 128, len(initial_frames))
                self._decode_frames(initial_frames[offset:end], interruptible=True)
                self._initial_frames_remaining = len(initial_frames) - end
                self._initial_decode_ms = (time.monotonic() - initial_started) * 1000
                if self._decode_stop.is_set():
                    with self._shutdown_pending_lock:
                        self._shutdown_pending_frames.extend(initial_frames[end:])
                    self._initial_frames_remaining = 0
                    return
                time.sleep(0)
            self._initial_frames_remaining = 0
            initial_frames.clear()
            last_stale_flush = time.monotonic()
            while not self._decode_stop.is_set():
                now = time.monotonic()
                self._apply_port_refresh()
                error = self._stream.read_error()
                if error:
                    self._decode_errors.put(error)
                    return
                # Keep one live batch small so Stop can transfer its unvisited
                # suffix promptly.  Throughput is unchanged: the loop runs
                # again immediately while packets remain queued.
                packet_queue_stats = getattr(
                    self._stream, "packet_queue_stats", {}
                )
                pending_packets = (
                    int(packet_queue_stats.get("pending", 0) or 0)
                    if isinstance(packet_queue_stats, dict)
                    else 0
                )
                stream_clock = getattr(
                    self._stream, "capture_clock_sample", None
                )
                if callable(stream_clock):
                    _raw_revision, raw_timestamp, _raw_enqueued = (
                        stream_clock()
                    )
                else:
                    raw_timestamp = 0.0
                with self._capture_clock_lock:
                    decoded_timestamp = self._latest_decoded_frame_timestamp
                queue_lag_ms = max(
                    0.0,
                    (float(raw_timestamp) - float(decoded_timestamp)) * 1000.0,
                )
                needs_normal_priority = _decoder_requires_normal_priority(
                    pending_packets,
                    max(queue_lag_ms, float(getattr(self._stream, "capture_reader_lag_ms", 0.0))),
                    currently_normal=(
                        self._decoder_priority_mode == "normal"
                    ),
                )
                if (
                    needs_normal_priority
                    and self._decoder_priority_mode != "normal"
                ):
                    set_current_thread_normal_priority()
                    self._decoder_priority_mode = "normal"
                elif (
                    not needs_normal_priority
                    and self._decoder_priority_mode != "low"
                ):
                    set_current_thread_low_priority()
                    self._decoder_priority_mode = "low"
                # Preserve single-threaded protocol order, but amortize Python
                # handoff overhead aggressively when capture gets ahead.
                frame_limit = (
                    2_048
                    if pending_packets >= 8_192
                    else 1_024
                    if pending_packets >= 2_048
                    else 512
                    if pending_packets >= 512
                    else 128
                )
                frames = self._stream.read_packets(limit=frame_limit)
                if frames:
                    self._decode_frames(frames, interruptible=True)
                if now >= self._next_accelerator_probe_at:
                    if (
                        self._awaiting_udp_combat_flow
                        and getattr(self._stream, "udp_ports", ())
                    ):
                        self._hold_accelerator_udp_discovery(12.0)
                    self._next_accelerator_probe_at = now + 10.0
                now = time.monotonic()
                if now - last_stale_flush >= 0.25:
                    self._publish_events(
                        self._decode_logical_packets(
                            self._transport.flush_stale(time.time())
                        )
                    )
                    last_stale_flush = now
                if not frames:
                    self._decode_stop.wait(0.005)
                else:
                    time.sleep(0)
        except Exception as exc:
            self._decode_errors.put(str(exc))

    def _drain_decoded_events(self, limit: int | None) -> list[LiveDpsEvent]:
        get_many = getattr(self._decoded_events, "get_many", None)
        if callable(get_many) and limit is not None:
            return get_many(limit)
        events: list[LiveDpsEvent] = []
        while limit is None or len(events) < limit:
            try:
                events.append(self._decoded_events.get_nowait())
            except queue.Empty:
                break
        return events

    def decoded_event_backlog(self) -> int:
        """Event queue only: this is not raw-packet decoder lag."""
        qsize = getattr(self._decoded_events, "qsize", None)
        return max(0, int(qsize())) if callable(qsize) else 0

    def poll(self) -> list[LiveDpsEvent]:
        try:
            error = self._decode_errors.get_nowait()
        except queue.Empty:
            error = ""
        if error:
            raise NpcapError(f"Npcap 实时抓取失败：{error}")
        # Keep each live ingest lock short.  The overlay snapshot worker uses
        # the same collector lock; draining thousands of decoded events in one
        # pass made a current raw frame wait seconds for the compact overlay
        # projection even after Npcap and decoding had caught up.  At the
        # controller's 40 ms cadence, 512 events/pass still sustains 12,800
        # events/s.  Stop continues to drain the complete suffix separately.
        pending = self.decoded_event_backlog()
        return self._drain_decoded_events(512 if pending >= 512 else 256)

    def poll_identities(self) -> list[DecodedTcpPlayerSpawnIdentity]:
        """Drain exact identities independently from combat event polling."""

        identities: list[DecodedTcpPlayerSpawnIdentity] = []
        while True:
            try:
                identities.append(self._identity_events.get_nowait())
            except queue.Empty:
                return identities

    def poll_identity_discovery_triggers(self) -> list[str]:
        """Drain events that require the independent identity TCP probe."""

        triggers: list[str] = []
        while True:
            try:
                triggers.append(self._identity_discovery_triggers.get_nowait())
            except queue.Empty:
                return triggers

    def stage_identity_capture_frames(
        self,
        frames: Iterable[tuple[float, bytes, int, int]],
    ) -> None:
        """Append pre-boundary identity proof to adapter 255 at trace close."""

        stage = getattr(self._stream, "set_identity_trace_frames", None)
        if callable(stage):
            stage(tuple(frames))

    def identity_bindings_snapshot(self) -> dict[int, str]:
        """Return session-local verified identities for trace finalization."""

        return dict(getattr(self._decoder, "known_actor_names", {}) or {})

    def stage_profile_identity_bindings(self, bindings: object) -> int:
        """Persist and publish verified runtime -> stable-profile bindings."""

        encoded = encode_profile_identity_bindings(bindings)
        rows = decode_profile_identity_bindings(encoded)
        if not rows:
            return 0
        stage = getattr(self._stream, "set_profile_identity_bindings", None)
        if callable(stage):
            stage(rows)
        published = 0
        for row in rows:
            runtime_id = int(row["runtime_actor_id"])
            profile_id = int(row["profile_id"])
            name = str(row["character_name"])
            job = str(row.get("job", "") or "").strip()
            source = str(row.get("source", "cache") or "cache").strip()
            self._decoder.known_actor_names[runtime_id] = name
            self._decoder.known_actor_names[profile_id] = name
            signature = (runtime_id, profile_id, name, job)
            if signature in self._profile_identity_events_seen:
                continue
            self._profile_identity_events_seen.add(signature)
            self._decoded_events.put(PlayerInspectEvent(
                timestamp_ms=max(
                    0,
                    round(
                        max(
                            0.0,
                            float(getattr(self._decoder, "latest_timestamp", 0.0) or 0.0)
                            - float(getattr(self._decoder, "first_timestamp", 0.0) or 0.0),
                        ) * 1000
                    ),
                ),
                target_id=f"0x{runtime_id:08X}",
                target_name=name,
                job=job,
                status="已验证身份缓存",
                request_fingerprint=(
                    f"identity-cache:{runtime_id:08X}:{profile_id:08X}"
                ),
                response_layout="DNPI1",
                fields=(
                    ("响应类型", "共享身份缓存"),
                    ("角色档案标识", f"0x{profile_id:08X}"),
                    ("实时ID", f"0x{runtime_id:08X}"),
                    ("身份来源", source),
                ),
            ))
            published += 1
        return published

    def flush_pending(self) -> list[LiveDpsEvent]:
        if self._decode_finalized:
            return self._drain_decoded_events(None)
        # Signal the decoder before closing Npcap.  Otherwise it may begin a
        # new 1024-frame batch while the capture workers are being joined,
        # making an ordinary Stop hit the old fixed ten-second timeout.
        self._decode_stop.set()
        stage_bindings = getattr(
            self._stream, "set_identity_trace_bindings", None
        )
        if callable(stage_bindings):
            stage_bindings(self.identity_bindings_snapshot())
        if not self._stream_closed:
            self._stream.close()
            self._stream_closed = True
        self._decode_thread.join(timeout=10.0)
        if self._decode_thread.is_alive():
            raise NpcapError("实时解码线程未能及时停止")
        with self._shutdown_pending_lock:
            interrupted_frames = self._shutdown_pending_frames
            self._shutdown_pending_frames = []
        if interrupted_frames:
            self._decode_frames(interrupted_frames)
        # The capture stream may have queued frames between the worker's final
        # batch and close(). Decode every remaining frame before final flush.
        while True:
            frames = self._stream.read_packets(limit=4096)
            if not frames:
                break
            self._decode_frames(frames)
        self._publish_events(self._decode_logical_packets(self._transport.flush()))
        flush_boss_actions = getattr(self._decoder, "flush_boss_actions", None)
        if callable(flush_boss_actions):
            self._publish_events(flush_boss_actions())
        self._decode_finalized = True
        return self._drain_decoded_events(None)

    def request_stop(self) -> None:
        """Stop Npcap producers now; decoder draining remains asynchronous."""

        request_stream_stop = getattr(self._stream, "request_stop", None)
        if callable(request_stream_stop):
            request_stream_stop()

    def close(self) -> None:
        if not self._decode_finalized:
            self.flush_pending()
        self._decoder.save_learned_damage_profiles()
        if not self._stream_closed:
            self._stream.close()
            self._stream_closed = True
        self._discard_working_trace()

    def _discard_working_trace(self) -> None:
        """本轮抓包的收尾。

        默认（「调试记录」开关关闭）行为与以前一致：临时抓包直接从 %TEMP% 删除，
        不留任何历史。开关打开时改为移动到保存目录，便于事后按同一份数据反复
        离线复算，而不必让玩家重打一遍副本。
        """

        path = getattr(self, "diagnostic_trace_path", None)
        if not path:
            return
        if self._retain_working_trace(Path(path)):
            self.diagnostic_trace_path = None
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            remove_dpscap_segments(Path(path))
        except OSError:
            pass
        self.diagnostic_trace_path = None

    @staticmethod
    def _retain_working_trace(path: Path) -> bool:
        """按「调试记录」开关决定是否保留抓包；任何异常都回退到删除。"""

        try:
            from . import debug_recording
        except Exception:
            return False
        try:
            if not debug_recording.keep_capture_enabled():
                return False
            return debug_recording.retain_capture(path)
        except Exception:
            return False

    def capture_clock_sample(self) -> tuple[int, float]:
        """Return raw-frame revision and seconds from the session boundary.

        Automatic capture trims its probe buffer to the first confirmed formal
        dungeon map/entity. That frame is also the decoder's first timestamp.
        Read the latest timestamp
        directly from the Npcap reader; updating this value after
        ``read_packets`` would make a busy decoder move the overlay clock late.
        """

        stream_sample = getattr(self._stream, "capture_clock_sample", None)
        if callable(stream_sample):
            revision, latest, _enqueued = stream_sample()
        else:
            with self._capture_clock_lock:
                revision = self._capture_clock_revision
                latest = self._latest_frame_timestamp
        origin_value = getattr(self._decoder, "first_timestamp", None)
        if revision <= 0 or latest <= 0.0 or origin_value is None:
            return revision, 0.0
        origin = float(origin_value)
        return revision, max(0.0, latest - origin)

    def capture_latency_sample(self) -> dict[str, float | int | str]:
        """Expose capture/decode queue timing without touching event history."""

        stream_sample = getattr(self._stream, "capture_clock_sample", None)
        if callable(stream_sample):
            raw_revision, raw_timestamp, raw_enqueued = stream_sample()
        else:
            with self._capture_clock_lock:
                raw_revision = self._capture_clock_revision
                raw_timestamp = self._latest_frame_timestamp
            raw_enqueued = 0.0
        with self._capture_clock_lock:
            decoded_timestamp = self._latest_decoded_frame_timestamp
            decoded_monotonic = self._latest_decoded_monotonic
            capture_to_decode_ms = self._latest_capture_to_decode_ms
        queue_delay_ms = max(
            0.0, (float(raw_timestamp) - float(decoded_timestamp)) * 1000.0
        )
        packet_queue = getattr(self._stream, "packet_queue_stats", {})
        event_stats = getattr(self._decoded_events, "stats", None)
        result = {
            **getattr(self, "_decode_batch_timing", {}),
            "rawFrameRevision": int(raw_revision),
            "rawFrameTimestamp": float(raw_timestamp),
            "rawFrameEnqueuedMonotonic": float(raw_enqueued),
            "decodedFrameTimestamp": float(decoded_timestamp),
            "decodedFrameMonotonic": float(decoded_monotonic),
            "captureToDecodeMs": float(capture_to_decode_ms),
            "decoderQueueDelayMs": float(queue_delay_ms),
            "npcapReaderLagMs": float(getattr(self._stream, "capture_reader_lag_ms", 0.0)),
            "npcapDeliveryWarnings": "; ".join(getattr(self._stream, "_capture_delivery_warnings", ())),
            "decoderPriority": self._decoder_priority_mode,
            "initialFramesRemaining": self._initial_frames_remaining,
            "initialDecodeMs": round(self._initial_decode_ms, 3),
            "portRefreshMs": self._port_refresh_ms,
        }
        if isinstance(packet_queue, dict):
            result.update({
                "pendingRawFrames": int(packet_queue.get("pending", 0) or 0),
                "spilledRawFrames": int(
                    packet_queue.get("spillItems", 0) or 0
                ),
                "totalSpilledRawFrames": int(
                    packet_queue.get("totalSpilled", 0) or 0
                ),
            })
        driver_stats = getattr(self._stream, "driver_packet_stats", {})
        result["decoderReplayRequired"] = int(bool(getattr(self._stream, "_decoder_replay_required", "")))
        result["tlsDecodeSkippedPackets"] = self._transport.tls_probe.ignored_packets
        if isinstance(driver_stats, dict):
            result.update({
                "npcapReceivedPackets": int(driver_stats.get("received", 0) or 0),
                "npcapDroppedPackets": int(driver_stats.get("dropped", 0) or 0),
                "npcapInterfaceDroppedPackets": int(
                    driver_stats.get("interfaceDropped", 0) or 0
                ),
                "npcapSampledAdapters": int(driver_stats.get("adapters", 0) or 0),
            })
        # Retain the active capture scope with the drop counters.  A future
        # sparse timeline can then distinguish a driver loss from a scene
        # server/port change that never entered the saved .dpscap file.
        result["knownUdpPorts"] = ",".join(
            str(port) for port in getattr(self._stream, "udp_ports", ())
        )
        result["udpDiscoveryActive"] = int(bool(
            getattr(self._stream, "accelerator_discovery_active", False)
        ))
        write_stats = getattr(self._stream, "trace_writer_stats", {})
        if isinstance(write_stats, dict):
            result.update({
                "rawWritePendingBytes": int(write_stats.get("pendingBytes", 0)),
                "rawWritePendingItems": int(write_stats.get("pendingItems", 0)),
                "rawWriteBacklogMs": float(write_stats.get("oldestPendingMs", 0.0)),
                "rawWriteLastBatchMs": float(write_stats.get("lastWriteMs", 0.0)),
                "rawWriteFailed": int(bool(write_stats.get("failed", False))),
            })
        if event_stats is not None:
            result.update({
                "pendingDecodedEvents": int(event_stats.pending),
                "spilledDecodedEvents": int(event_stats.spill_items),
                "totalSpilledDecodedEvents": int(event_stats.total_spilled),
            })
        return result

    @property
    def event_ledger_stats(self):
        return self._ledger.stats

    @property
    def event_ledger_mode(self) -> str:
        return self._ledger.mode

class V576MemorySource:
    """Version-gated read-only source. Event locators are added after live calibration."""

    def __init__(self, profile: ClientProfile) -> None:
        try:
            from .process_reader import ProcessReadError, ReadOnlyProcess
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "当前发布版不包含只读内存采集，请使用 Npcap 实时统计"
            ) from exc
        valid, message = profile.validate_executable()
        if not valid:
            raise ProcessReadError(message)
        if not profile.calibrated:
            raise ProcessReadError("v576 已验证，但战斗事件定位尚未校准")
        self._profile = profile
        self._process = ReadOnlyProcess.attach(profile.process_name)

    def poll(self) -> list[CombatEvent]:
        # The profile intentionally fails closed until the authorized v576
        # combat-event layout has been calibrated and validated.
        return []

    def close(self) -> None:
        self._process.close()


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'V576PktmonTextStreamSource', 'JsonlReplaySource', 'auto_capture_confirmation_reason', 'V576AutoCaptureProbe', 'V576MemorySource', 'V576NpcapSource', 'V576PcapReplaySource', 'bind_host_globals', 'CombatEventSource', 'V576DiagnosticReplaySource'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
