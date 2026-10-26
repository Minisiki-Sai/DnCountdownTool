from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO, Callable


class AsyncTraceWriter:
    """One file owner; producers only enqueue immutable record parts.

    The bounded allowance includes the batch currently being written. A stuck
    disk cannot grow RAM indefinitely; exhaustion is an explicit recording
    failure, never silent eviction or blocking a Npcap reader on disk I/O.
    """

    def __init__(
        self,
        path: Path,
        *,
        on_error: Callable[[str], None] | None = None,
        memory_bytes: int = 128 * 1024 * 1024,
        memory_items: int = 262_144,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        self.path = Path(path)
        self._on_error = on_error
        self._opener = opener or (lambda target: target.open("wb", buffering=1024 * 1024))
        self._byte_limit = max(1, int(memory_bytes))
        self._item_limit = max(1, int(memory_items))
        self._condition = threading.Condition()
        self._queue: deque[tuple[tuple[bytes, ...], int, float]] = deque()
        self._pending_bytes = 0
        self._pending_items = 0
        self._accepted = 0
        self._written = 0
        self._peak_bytes = 0
        self._inflight_at = 0.0
        self._last_write_ms = 0.0
        self._closing = False
        self._flush_requested = False
        self._error = ""
        self._final_hash = ""
        self._thread = threading.Thread(target=self._run, name="dn-raw-trace-writer", daemon=True)
        self._thread.start()

    def _fail(self, message: str) -> None:
        with self._condition:
            first = not self._error
            if first:
                self._error = message
            self._closing = True
            self._condition.notify_all()
        if first and self._on_error is not None:
            self._on_error(message)

    def write_parts(self, parts: tuple[bytes, ...]) -> None:
        # bytes(bytes_object) preserves its storage: the payload can be shared
        # with the decoder queue without copying it on the capture thread.
        record = tuple(bytes(part) for part in parts)
        size = sum(map(len, record)) + 64
        overflow = False
        with self._condition:
            if self._error:
                raise OSError(self._error)
            if self._closing:
                raise OSError("Raw trace writer is already closing")
            if self._pending_bytes + size > self._byte_limit or self._pending_items >= self._item_limit:
                overflow = True
            else:
                self._queue.append((record, size, time.monotonic()))
                self._pending_bytes += size
                self._pending_items += 1
                self._accepted += 1
                self._peak_bytes = max(self._peak_bytes, self._pending_bytes)
                self._condition.notify()
        if overflow:
            reason = "Raw trace write backlog exceeded its memory limit; recording is incomplete"
            self._fail(reason)
            raise OSError(reason)

    def write(self, data: bytes) -> int:
        self.write_parts((data,))
        return len(data)

    def flush(self) -> None:
        """Request a worker flush without waiting on the calling thread."""
        with self._condition:
            if self._error:
                raise OSError(self._error)
            self._flush_requested = True
            self._condition.notify()

    def _run(self) -> None:
        digest = hashlib.sha256()
        try:
            # Opening, writing, hashing, flushing and closing all belong here.
            with self._opener(self.path) as handle:
                last_flush = time.monotonic()
                dirty = False
                while True:
                    with self._condition:
                        if not self._queue and not self._closing and not self._flush_requested:
                            self._condition.wait(0.5)
                        batch = []
                        batch_bytes = 0
                        while self._queue and batch_bytes < 1024 * 1024 and len(batch) < 512:
                            item = self._queue.popleft()
                            batch.append(item)
                            batch_bytes += item[1]
                        self._inflight_at = batch[0][2] if batch else 0.0
                        closing = self._closing and not self._queue
                        flush_requested = self._flush_requested
                        self._flush_requested = False
                    if batch:
                        payload = b"".join(part for parts, _size, _at in batch for part in parts)
                        began = time.monotonic()
                        written = handle.write(payload)
                        if written != len(payload):
                            raise OSError(f"Short raw trace write: expected={len(payload)} actual={written}")
                        digest.update(payload)
                        dirty = True
                        with self._condition:
                            self._pending_bytes -= sum(row[1] for row in batch)
                            self._pending_items -= len(batch)
                            self._written += len(batch)
                            self._inflight_at = 0.0
                            self._last_write_ms = (time.monotonic() - began) * 1000
                    if dirty and (closing or flush_requested or time.monotonic() - last_flush >= 0.5):
                        handle.flush()
                        dirty = False
                        last_flush = time.monotonic()
                    if closing:
                        break
            with self._condition:
                if not self._error:
                    self._final_hash = digest.hexdigest()
        except Exception as exc:
            self._fail(f"Raw trace file write failed; recording is incomplete: {exc}")

    def close(self, timeout: float = 10.0) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            # The worker still owns its file. Never close its handle from here.
            self._fail("Raw trace writer did not finish draining; recording is incomplete")
            raise TimeoutError(self._error)
        if self._error:
            raise OSError(self._error)

    @property
    def content_hash(self) -> str:
        with self._condition:
            return self._final_hash if not self._error else ""

    @property
    def stats(self) -> dict[str, object]:
        with self._condition:
            oldest = self._inflight_at or (self._queue[0][2] if self._queue else 0.0)
            return {
                "pendingBytes": self._pending_bytes,
                "pendingItems": self._pending_items,
                "peakPendingBytes": self._peak_bytes,
                "acceptedItems": self._accepted,
                "writtenItems": self._written,
                "oldestPendingMs": (time.monotonic() - oldest) * 1000 if oldest else 0.0,
                "lastWriteMs": self._last_write_ms,
                "failed": bool(self._error),
                "error": self._error,
            }
