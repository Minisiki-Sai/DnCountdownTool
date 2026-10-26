from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DPSCAP_MAGIC = b"DNDP1\x00"
DPSCAP_FRAME_HEADER = struct.Struct("<dBII")
DPSCAP_SEGMENT_SCHEMA = "dragon-nest-dpscap-segments-v1"
_MAX_CAPTURED_FRAME_BYTES = 16 * 1024 * 1024
# A physical sidecar is useful only when it contains at least two reasonably
# sized parts.  Real-capture measurements show that 16 MiB parts keep the
# post-capture split below one second for ordinary runs, while starting at
# 32 MiB avoids copying small captures into a one-part sidecar with no replay
# benefit.
_DEFAULT_SEGMENT_BYTES = 16 * 1024 * 1024
_DEFAULT_MINIMUM_BYTES = 32 * 1024 * 1024
_DEFAULT_FREE_SPACE_RESERVE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class DpscapSegmentSet:
    source_path: Path
    directory: Path
    paths: tuple[Path, ...]
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    frame_count: int
    split_elapsed_ms: float = 0.0


@dataclass(frozen=True)
class _SplitTask:
    path: Path
    expected_sha256: str


def dpscap_segment_directory(path: Path) -> Path:
    source = Path(path)
    return source.with_name(f"{source.name}.parts")


def _positive_environment_integer(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def _nonnegative_environment_float(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, value)


def _has_split_disk_space(source: Path, source_size: int) -> bool:
    """Keep one source-sized copy plus a reserve before creating a sidecar."""

    reserve = _positive_environment_integer(
        "DRAGON_NEST_DPSCAP_SPLIT_RESERVE_BYTES",
        _DEFAULT_FREE_SPACE_RESERVE_BYTES,
    )
    try:
        free_bytes = int(shutil.disk_usage(source.parent).free)
    except OSError:
        # A failed advisory query must not make an otherwise valid capture
        # unreadable.  The atomic temporary directory still protects it from
        # partial publication if the actual write later fails.
        return True
    return free_bytes >= max(0, int(source_size)) + reserve


def _safe_remove_directory(path: Path, source: Path) -> None:
    """Remove exactly one generated sibling directory, never a broad path."""

    candidate = Path(path).resolve()
    expected = dpscap_segment_directory(source).resolve()
    if candidate != expected or candidate.parent != Path(source).resolve().parent:
        raise ValueError("拒绝删除非 .dpscap 分片目录")
    if candidate.is_dir():
        shutil.rmtree(candidate)


def remove_dpscap_segments(path: Path) -> None:
    source = Path(path)
    directory = dpscap_segment_directory(source)
    try:
        _safe_remove_directory(directory, source)
    except (OSError, ValueError):
        pass


def load_dpscap_segments(path: Path) -> DpscapSegmentSet | None:
    """Return a validated sidecar index without scanning the source capture."""

    source = Path(path)
    directory = dpscap_segment_directory(source)
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_stat = source.stat()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("schema") != DPSCAP_SEGMENT_SCHEMA:
        return None
    try:
        source_size = int(manifest.get("sourceSize", 0) or 0)
        source_mtime_ns = int(manifest.get("sourceMtimeNs", 0) or 0)
        frame_count = int(manifest.get("frameCount", 0) or 0)
        split_elapsed_ms = max(
            0.0, float(manifest.get("splitElapsedMs", 0.0) or 0.0)
        )
    except (TypeError, ValueError):
        return None
    if (
        str(manifest.get("sourceName") or "") != source.name
        or source_size != int(source_stat.st_size)
        or source_mtime_ns != int(source_stat.st_mtime_ns)
        or source_size < len(DPSCAP_MAGIC)
        or frame_count < 0
    ):
        return None
    rows = manifest.get("segments")
    if not isinstance(rows, list) or not rows:
        return None
    paths: list[Path] = []
    counted_frames = 0
    counted_source_bytes = len(DPSCAP_MAGIC)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return None
        name = str(row.get("file") or "")
        # Names are generated locally and must never escape the sidecar folder.
        if not name or Path(name).name != name:
            return None
        segment_path = directory / name
        try:
            segment_size = int(row.get("fileSize", 0) or 0)
            payload_bytes = int(row.get("recordBytes", 0) or 0)
            segment_frames = int(row.get("frameCount", 0) or 0)
            actual_size = int(segment_path.stat().st_size)
        except (OSError, TypeError, ValueError):
            return None
        if (
            segment_size != actual_size
            or segment_size != len(DPSCAP_MAGIC) + payload_bytes
            or payload_bytes < 0
            or segment_frames < 0
            or name != f"segment-{index + 1:05d}.dpscap"
        ):
            return None
        paths.append(segment_path)
        counted_frames += segment_frames
        counted_source_bytes += payload_bytes
    if counted_frames != frame_count or counted_source_bytes != source_size:
        return None
    return DpscapSegmentSet(
        source_path=source,
        directory=directory,
        paths=tuple(paths),
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        source_sha256=str(manifest.get("sourceSha256") or "").lower(),
        frame_count=frame_count,
        split_elapsed_ms=split_elapsed_ms,
    )


def _wait_while_paused(
    pause_check: Callable[[], bool] | None,
    cancel_check: Callable[[], bool] | None,
) -> None:
    while pause_check is not None and pause_check():
        if cancel_check is not None and cancel_check():
            raise InterruptedError("dpscap split cancelled")
        time.sleep(0.1)


def split_dpscap(
    path: Path,
    *,
    segment_bytes: int | None = None,
    minimum_bytes: int | None = None,
    expected_sha256: str = "",
    pause_check: Callable[[], bool] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> DpscapSegmentSet | None:
    """Create an atomic, replay-compatible sidecar bundle after capture.

    Each segment is itself a valid DNDP1 file.  The original file is immutable
    and remains the compatibility/upload authority.  No work from this
    function belongs on the live packet-ingest path.
    """

    source = Path(path)
    source_stat = source.stat()
    requested_segment_bytes = max(
        1024,
        int(
            segment_bytes
            if segment_bytes is not None
            else _positive_environment_integer(
                "DRAGON_NEST_DPSCAP_SEGMENT_BYTES", _DEFAULT_SEGMENT_BYTES
            )
        ),
    )
    requested_minimum_bytes = max(
        len(DPSCAP_MAGIC),
        int(
            minimum_bytes
            if minimum_bytes is not None
            else _positive_environment_integer(
                "DRAGON_NEST_DPSCAP_SPLIT_MIN_BYTES", _DEFAULT_MINIMUM_BYTES
            )
        ),
    )
    if source_stat.st_size < requested_minimum_bytes:
        return None
    # An environment override may lower the minimum below the segment target.
    # Never duplicate a capture into one physical part: it costs the full file
    # size and cannot improve replay scheduling.
    if segment_bytes is None and source_stat.st_size <= requested_segment_bytes:
        return None
    existing = load_dpscap_segments(source)
    normalized_expected_hash = str(expected_sha256 or "").strip().lower()
    if existing is not None and (
        not normalized_expected_hash
        or existing.source_sha256 == normalized_expected_hash
    ):
        return existing

    split_started = time.perf_counter()
    destination = dpscap_segment_directory(source)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{uuid.uuid4().hex}"
    )
    temporary.mkdir(parents=False, exist_ok=False)
    source_digest = hashlib.sha256()
    segments: list[dict[str, object]] = []
    frame_count = 0
    source_record_bytes = 0
    current_handle = None
    current_digest = None
    current_path: Path | None = None
    current_record_bytes = 0
    current_frames = 0
    current_first_timestamp: float | None = None
    current_last_timestamp: float | None = None
    current_adapters: dict[str, int] = {}

    def close_segment() -> None:
        nonlocal current_handle, current_digest, current_path
        nonlocal current_record_bytes, current_frames
        nonlocal current_first_timestamp, current_last_timestamp, current_adapters
        if current_handle is None or current_path is None or current_digest is None:
            return
        current_handle.flush()
        current_handle.close()
        current_handle = None
        segments.append(
            {
                "file": current_path.name,
                "fileSize": len(DPSCAP_MAGIC) + current_record_bytes,
                "recordBytes": current_record_bytes,
                "frameCount": current_frames,
                "firstTimestamp": current_first_timestamp,
                "lastTimestamp": current_last_timestamp,
                "adapterCounts": dict(sorted(current_adapters.items())),
                "sha256": current_digest.hexdigest(),
            }
        )
        current_path = None
        current_digest = None
        current_record_bytes = 0
        current_frames = 0
        current_first_timestamp = None
        current_last_timestamp = None
        current_adapters = {}

    def open_segment() -> None:
        nonlocal current_handle, current_digest, current_path
        index = len(segments) + 1
        current_path = temporary / f"segment-{index:05d}.dpscap"
        current_handle = current_path.open("wb", buffering=1024 * 1024)
        current_handle.write(DPSCAP_MAGIC)
        current_digest = hashlib.sha256()
        current_digest.update(DPSCAP_MAGIC)

    try:
        with source.open("rb", buffering=1024 * 1024) as handle:
            magic = handle.read(len(DPSCAP_MAGIC))
            if magic != DPSCAP_MAGIC:
                raise ValueError("不是有效的 Dragon Nest .dpscap 统计记录")
            source_digest.update(magic)
            while True:
                _wait_while_paused(pause_check, cancel_check)
                if cancel_check is not None and cancel_check():
                    raise InterruptedError("dpscap split cancelled")
                header = handle.read(DPSCAP_FRAME_HEADER.size)
                if not header:
                    break
                if len(header) != DPSCAP_FRAME_HEADER.size:
                    raise ValueError(".dpscap 尾部记录不完整")
                timestamp, adapter, _original_length, captured_length = (
                    DPSCAP_FRAME_HEADER.unpack(header)
                )
                if captured_length > _MAX_CAPTURED_FRAME_BYTES:
                    raise ValueError(".dpscap 包长度异常，文件可能已损坏")
                frame = handle.read(captured_length)
                if len(frame) != captured_length:
                    raise ValueError(".dpscap 尾部数据不完整")
                record_bytes = len(header) + len(frame)
                if (
                    current_handle is not None
                    and current_frames > 0
                    and current_record_bytes + record_bytes
                    > requested_segment_bytes
                ):
                    close_segment()
                if current_handle is None:
                    open_segment()
                assert current_handle is not None
                assert current_digest is not None
                current_handle.write(header)
                current_handle.write(frame)
                current_digest.update(header)
                current_digest.update(frame)
                source_digest.update(header)
                source_digest.update(frame)
                current_record_bytes += record_bytes
                source_record_bytes += record_bytes
                current_frames += 1
                frame_count += 1
                timestamp_value = float(timestamp)
                current_first_timestamp = (
                    timestamp_value
                    if current_first_timestamp is None
                    else min(current_first_timestamp, timestamp_value)
                )
                current_last_timestamp = (
                    timestamp_value
                    if current_last_timestamp is None
                    else max(current_last_timestamp, timestamp_value)
                )
                adapter_key = str(int(adapter))
                current_adapters[adapter_key] = (
                    current_adapters.get(adapter_key, 0) + 1
                )
        close_segment()
        if not segments:
            # Preserve a valid empty capture as one independently readable part.
            open_segment()
            close_segment()
        final_stat = source.stat()
        if (
            int(final_stat.st_size) != int(source_stat.st_size)
            or int(final_stat.st_mtime_ns) != int(source_stat.st_mtime_ns)
            or len(DPSCAP_MAGIC) + source_record_bytes != int(source_stat.st_size)
        ):
            raise RuntimeError("拆分期间原始 .dpscap 发生变化")
        source_hash = source_digest.hexdigest()
        if normalized_expected_hash and source_hash != normalized_expected_hash:
            raise ValueError(".dpscap 内容哈希与抓取结束时不一致")
        manifest = {
            "schema": DPSCAP_SEGMENT_SCHEMA,
            "version": 1,
            "sourceName": source.name,
            "sourceSize": int(source_stat.st_size),
            "sourceMtimeNs": int(source_stat.st_mtime_ns),
            "sourceSha256": source_hash,
            "segmentTargetBytes": requested_segment_bytes,
            "splitElapsedMs": round(
                max(0.0, time.perf_counter() - split_started) * 1000.0,
                3,
            ),
            "frameCount": frame_count,
            "segments": segments,
            "createdAtEpochMs": round(time.time() * 1000),
        }
        manifest_temporary = temporary / "manifest.json.tmp"
        manifest_temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temporary, temporary / "manifest.json")
        if destination.exists():
            _safe_remove_directory(destination, source)
        os.replace(temporary, destination)
        result = load_dpscap_segments(source)
        if result is None:
            raise RuntimeError(".dpscap 分片完成后校验失败")
        return result
    except BaseException:
        if current_handle is not None:
            current_handle.close()
        if temporary.is_dir():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


class DpscapPostCaptureSplitter:
    """One lazy, low-priority worker for post-capture sidecar generation."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[_SplitTask | None] = queue.Queue()
        self._lock = threading.RLock()
        self._capture_count = 0
        self._pending: set[Path] = set()
        self._results: dict[Path, dict[str, object]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def capture_started(self) -> None:
        with self._lock:
            self._capture_count += 1

    def capture_finished(self) -> None:
        with self._lock:
            self._capture_count = max(0, self._capture_count - 1)

    def capture_active(self) -> bool:
        with self._lock:
            return self._capture_count > 0

    def submit(self, path: Path, expected_sha256: str = "") -> bool:
        source = Path(path)
        key = source.resolve()
        try:
            minimum = _positive_environment_integer(
                "DRAGON_NEST_DPSCAP_SPLIT_MIN_BYTES", _DEFAULT_MINIMUM_BYTES
            )
            segment_bytes = _positive_environment_integer(
                "DRAGON_NEST_DPSCAP_SEGMENT_BYTES", _DEFAULT_SEGMENT_BYTES
            )
            if not source.is_file():
                return False
            source_size = int(source.stat().st_size)
            if source_size < minimum or source_size <= segment_bytes:
                with self._lock:
                    self._results[key] = {
                        "status": "skipped",
                        "reason": "below-threshold",
                        "sourceBytes": source_size,
                        "minimumBytes": max(minimum, segment_bytes + 1),
                    }
                return False
            if not _has_split_disk_space(source, source_size):
                with self._lock:
                    self._results[key] = {
                        "status": "skipped",
                        "reason": "low-disk-space",
                        "sourceBytes": source_size,
                    }
                return False
        except OSError:
            return False
        with self._lock:
            if key in self._pending:
                return False
            existing = load_dpscap_segments(source)
            normalized_hash = str(expected_sha256 or "").strip().lower()
            if existing is not None and (
                not normalized_hash or existing.source_sha256 == normalized_hash
            ):
                self._results[key] = {
                    "status": "ready",
                    "segments": len(existing.paths),
                    "sourceBytes": existing.source_size,
                    "elapsedMs": existing.split_elapsed_ms,
                }
                return False
            self._pending.add(key)
            self._results[key] = {"status": "queued"}
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name="DpscapPostCaptureSplitter",
                    daemon=True,
                )
                self._thread.start()
        self._tasks.put(_SplitTask(source, normalized_hash))
        return True

    def _run(self) -> None:
        try:
            # Import lazily to keep the file-format module dependency-free.
            from .npcap import set_current_thread_low_priority

            set_current_thread_low_priority()
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=0.25)
            except queue.Empty:
                continue
            if task is None:
                break
            key = task.path.resolve()
            with self._lock:
                self._results[key] = {"status": "waiting"}
            # Leave the immediate Stop/score/page projection path quiet.
            quiet_until = time.monotonic() + _nonnegative_environment_float(
                "DRAGON_NEST_DPSCAP_SPLIT_QUIET_SECONDS", 3.0
            )
            while not self._stop.is_set() and (
                time.monotonic() < quiet_until or self.capture_active()
            ):
                time.sleep(0.1)
            try:
                with self._lock:
                    self._results[key] = {"status": "splitting"}
                result = split_dpscap(
                    task.path,
                    expected_sha256=task.expected_sha256,
                    pause_check=self.capture_active,
                    cancel_check=self._stop.is_set,
                )
                with self._lock:
                    self._results[key] = (
                        {
                            "status": "ready",
                            "segments": len(result.paths),
                            "sourceBytes": result.source_size,
                            "elapsedMs": result.split_elapsed_ms,
                        }
                        if result is not None
                        else {"status": "skipped"}
                    )
            except InterruptedError:
                with self._lock:
                    self._results[key] = {"status": "cancelled"}
            except Exception as exc:
                with self._lock:
                    self._results[key] = {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            finally:
                with self._lock:
                    self._pending.discard(key)

    def status(self, path: Path) -> dict[str, object]:
        key = Path(path).resolve()
        with self._lock:
            return dict(self._results.get(key) or {"status": "none"})

    def wait(self, path: Path, timeout: float = 10.0) -> dict[str, object]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            state = self.status(path)
            if state.get("status") in {
                "ready", "skipped", "cancelled", "error", "none"
            }:
                return state
            time.sleep(0.02)
        return self.status(path)

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._tasks.put(None)
        worker = self._thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout)))


post_capture_splitter = DpscapPostCaptureSplitter()
