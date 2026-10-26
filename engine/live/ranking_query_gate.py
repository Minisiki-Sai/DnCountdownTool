"""Bound ranking I/O without serializing unrelated query keys."""
from contextlib import contextmanager
import threading


class RankingQueryGate:
    def __init__(self, concurrency: int = 3) -> None:
        self._slots = threading.BoundedSemaphore(concurrency)
        self._mutex = threading.Lock()
        self._keys: dict[tuple, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, key: tuple):
        with self._mutex:
            lock, users = self._keys.get(key, (threading.Lock(), 0))
            self._keys[key] = (lock, users + 1)
        try:
            # Same-key waiters must not occupy all network slots.
            with lock, self._slots:
                yield
        finally:
            with self._mutex:
                lock, users = self._keys[key]
                if users == 1:
                    del self._keys[key]
                else:
                    self._keys[key] = (lock, users - 1)
