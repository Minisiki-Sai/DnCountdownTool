from __future__ import annotations

import pickle
import queue
import struct
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SpillQueueStats:
    """Small diagnostic snapshot for a bounded-memory handoff queue."""

    pending: int
    memory_items: int
    memory_bytes: int
    spill_items: int
    peak_pending: int
    total_spilled: int


class BoundedSpillQueue(Generic[T]):
    """FIFO queue with bounded RAM and a lossless temporary-file overflow.

    Capture cannot safely discard a packet merely because decoding is briefly
    slower than Npcap.  Blocking the Npcap thread is also undesirable because
    it moves the loss to the kernel capture buffer.  This queue keeps the hot
    prefix in memory and writes a sustained backlog to a delete-on-close
    temporary file.  Once spill mode starts, new values continue to use that
    file until it is drained, preserving strict FIFO order across producers.
    """

    _LENGTH = struct.Struct("<I")

    def __init__(
        self,
        *,
        memory_items: int,
        memory_bytes: int,
        size_of: Callable[[T], int] | None = None,
    ) -> None:
        self._memory_item_limit = max(1, int(memory_items))
        self._memory_byte_limit = max(1, int(memory_bytes))
        self._size_of = size_of or (lambda _value: 1)
        self._memory: deque[tuple[T, int]] = deque()
        self._memory_bytes = 0
        self._spill = tempfile.TemporaryFile(mode="w+b")
        self._spill_read_offset = 0
        self._spill_write_offset = 0
        self._spill_items = 0
        self._pending = 0
        self._peak_pending = 0
        self._total_spilled = 0
        self._sealed = False
        self._closed = False
        self._lock = threading.Lock()

    def put(self, value: T) -> None:
        estimated_size = max(1, int(self._size_of(value)))
        with self._lock:
            if self._sealed or self._closed:
                raise RuntimeError("cannot put into a closed capture queue")
            fits_memory = (
                self._spill_items == 0
                and len(self._memory) < self._memory_item_limit
                and self._memory_bytes + estimated_size <= self._memory_byte_limit
            )
            if fits_memory:
                self._memory.append((value, estimated_size))
                self._memory_bytes += estimated_size
            else:
                payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
                self._spill.seek(self._spill_write_offset)
                self._spill.write(self._LENGTH.pack(len(payload)))
                self._spill.write(payload)
                self._spill_write_offset += self._LENGTH.size + len(payload)
                self._spill_items += 1
                self._total_spilled += 1
            self._pending += 1
            self._peak_pending = max(self._peak_pending, self._pending)

    def _read_one_locked(self) -> T:
        if self._memory:
            value, estimated_size = self._memory.popleft()
            self._memory_bytes = max(0, self._memory_bytes - estimated_size)
        elif self._spill_items:
            self._spill.seek(self._spill_read_offset)
            length_bytes = self._spill.read(self._LENGTH.size)
            if len(length_bytes) != self._LENGTH.size:
                raise RuntimeError("capture queue overflow file is truncated")
            payload_size = self._LENGTH.unpack(length_bytes)[0]
            payload = self._spill.read(payload_size)
            if len(payload) != payload_size:
                raise RuntimeError("capture queue overflow record is truncated")
            value = pickle.loads(payload)
            self._spill_read_offset += self._LENGTH.size + payload_size
            self._spill_items -= 1
            if self._spill_items == 0:
                # No producer can append while this lock is held.  Reclaim the
                # temporary file immediately and return to the in-memory path.
                self._spill.seek(0)
                self._spill.truncate(0)
                self._spill_read_offset = 0
                self._spill_write_offset = 0
        else:
            raise queue.Empty
        self._pending -= 1
        return value

    def get_nowait(self) -> T:
        with self._lock:
            return self._read_one_locked()

    def get_many(self, limit: int) -> list[T]:
        result: list[T] = []
        with self._lock:
            for _index in range(max(0, int(limit))):
                if self._pending <= 0:
                    break
                result.append(self._read_one_locked())
        return result

    def qsize(self) -> int:
        with self._lock:
            return self._pending

    def empty(self) -> bool:
        return self.qsize() == 0

    @property
    def stats(self) -> SpillQueueStats:
        with self._lock:
            return SpillQueueStats(
                pending=self._pending,
                memory_items=len(self._memory),
                memory_bytes=self._memory_bytes,
                spill_items=self._spill_items,
                peak_pending=self._peak_pending,
                total_spilled=self._total_spilled,
            )

    def seal(self) -> None:
        """Reject future producers while retaining every item for draining."""

        with self._lock:
            self._sealed = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._sealed = True
            self._memory.clear()
            self._memory_bytes = 0
            self._pending = 0
            self._spill_items = 0
            self._spill.close()
            self._closed = True

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown guard
        try:
            self.close()
        except Exception:
            pass


class AsyncSpillQueue(Generic[T]):
    """Move overflow-file I/O off producers, with bounded memory staging.

    Only this worker writes the spill queue. Consumers may read its files on
    their decode thread. Producers never acquire the spill queue's I/O lock.
    """

    def __init__(self, *, memory_items: int, memory_bytes: int,
                 size_of: Callable[[T], int], on_error: Callable[[str], None] | None = None,
                 staging_bytes: int = 64 * 1024 * 1024,
                 staging_items: int = 65_536) -> None:
        self._inner = BoundedSpillQueue(memory_items=memory_items, memory_bytes=memory_bytes, size_of=size_of)
        self._size_of = size_of
        self._on_error = on_error
        self._condition = threading.Condition()
        self._staging: deque[tuple[T, int]] = deque()
        self._staging_bytes = 0
        self._staging_items = 0
        self._byte_limit = max(1, staging_bytes)
        self._item_limit = max(1, staging_items)
        self._pending = 0
        self._peak = 0
        self._sealed = False
        self._error = ""
        self._inner_stats = self._inner.stats
        self._thread = threading.Thread(target=self._run, name="dn-capture-queue-spool", daemon=True)
        self._thread.start()

    def _fail(self, reason: str) -> None:
        with self._condition:
            first = not self._error
            if first:
                self._error = reason
            self._sealed = True
            self._condition.notify_all()
        if first and self._on_error is not None:
            self._on_error(reason)

    def put(self, value: T) -> None:
        size = max(1, int(self._size_of(value)))
        with self._condition:
            if self._error or self._sealed:
                raise OSError(self._error or "Capture queue is sealed")
            full = self._staging_bytes + size > self._byte_limit or self._staging_items >= self._item_limit
            if not full:
                self._staging.append((value, size))
                self._staging_bytes += size
                self._staging_items += 1
                self._pending += 1
                self._peak = max(self._peak, self._pending)
                self._condition.notify()
        if full:
            reason = "Capture queue spool backlog exceeded its memory limit; recording is incomplete"
            self._fail(reason)
            raise OSError(reason)

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._staging and not self._sealed:
                        self._condition.wait()
                    if not self._staging:
                        return
                    batch = [self._staging.popleft() for _ in range(min(512, len(self._staging)))]
                for value, size in batch:
                    self._inner.put(value)
                    stats = self._inner.stats
                    with self._condition:
                        self._staging_bytes -= size
                        self._staging_items -= 1
                        self._inner_stats = stats
                        self._condition.notify_all()
        except Exception as exc:
            self._fail(f"Capture queue spill failed; recording is incomplete: {exc}")

    def get_many(self, limit: int) -> list[T]:
        values = self._inner.get_many(limit)
        stats = self._inner.stats
        with self._condition:
            self._pending -= len(values)
            self._inner_stats = stats
        return values

    def get_nowait(self) -> T:
        rows = self.get_many(1)
        if not rows:
            raise queue.Empty
        return rows[0]

    def qsize(self) -> int:
        with self._condition:
            return self._pending

    def flush_pending(self, timeout: float = 10.0) -> None:
        """Publish the entire pre-trace handoff before its boundary is frozen."""
        deadline = time.monotonic() + timeout
        timed_out = False
        with self._condition:
            while self._staging_items and not self._error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self._condition.wait(remaining)
            if self._error:
                raise OSError(self._error)
        if timed_out:
            reason = "Capture queue handoff did not finish; recording is incomplete"
            self._fail(reason)
            raise TimeoutError(reason)

    def seal(self) -> None:
        with self._condition:
            self._sealed = True
            self._condition.notify_all()
        self._thread.join(10.0)
        if self._thread.is_alive():
            self._fail("Capture queue spool did not finish draining; recording is incomplete")
            raise TimeoutError(self._error)
        if self._error:
            raise OSError(self._error)
        self._inner.seal()

    def close(self) -> None:
        self.seal()
        self._inner.close()

    @property
    def stats(self) -> SpillQueueStats:
        # This diagnostic path also avoids a lock held during disk I/O.
        with self._condition:
            stats = self._inner_stats
            return SpillQueueStats(self._pending, stats.memory_items + self._staging_items,
                                   stats.memory_bytes + self._staging_bytes, stats.spill_items,
                                   self._peak, stats.total_spilled)
