"""Cooperative foreground-I/O priority; never suspend a native process/lock.

Only calculation threads and pool workers opt in. Packet ingestion and the
foreground reader never wait here. Pausing preserves Python stacks and inputs;
simulation time and search budgets are deliberately unaffected.
"""
from __future__ import annotations

import multiprocessing
import threading
import time
from concurrent.futures import FIRST_COMPLETED, TimeoutError, wait
from contextlib import contextmanager

_lock = threading.RLock()
_local = threading.local()
_resume = None
_readers = 0
_pause_started = 0.0
_paused_seconds = 0.0


def worker_priority_signal():
    global _resume
    with _lock:
        if _resume is None:
            _resume = multiprocessing.get_context('spawn').Event()
            if not _readers:
                _resume.set()
        return _resume


def install_worker_priority_signal(signal):
    global _resume
    _resume = signal
    _local.enabled = True


def foreground_pending() -> bool:
    with _lock:
        return _readers > 0


class ForegroundLease:
    def __init__(self):
        global _readers, _pause_started
        self._released = False
        with _lock:
            if not _readers:
                _pause_started = time.monotonic()
                worker_priority_signal().clear()
            _readers += 1

    def release(self):
        global _readers, _paused_seconds
        with _lock:
            if self._released:
                return
            self._released = True
            _readers -= 1
            if not _readers:
                _paused_seconds += time.monotonic() - _pause_started
                worker_priority_signal().set()


def score_work_clock() -> float:
    """Wall clock excluding foreground reads, for stall detection only."""
    with _lock:
        now = time.monotonic()
        return now - _paused_seconds - (now - _pause_started if _readers else 0.0)


def score_checkpoint(*, force=False):
    if not getattr(_local, 'enabled', False):
        return
    now = time.monotonic()
    if not force and now < getattr(_local, 'next_check', 0.0):
        return
    _local.next_check = now + 0.025
    check_cancel = getattr(_local, 'check_cancel', None)
    if check_cancel:
        check_cancel()
    signal = _resume
    if signal is not None:
        while not signal.wait(0.1):
            if check_cancel:
                check_cancel()


@contextmanager
def score_execution(check_cancel=None):
    previous = vars(_local).copy()
    _local.enabled = True
    _local.check_cancel = check_cancel
    worker_priority_signal()
    try:
        score_checkpoint(force=True)
        yield
    finally:
        vars(_local).clear()
        vars(_local).update(previous)


def completed_score_futures(futures, timeout):
    """Poll while allowing cancellation and excluding intentional pause time."""
    pending = set(futures)
    deadline = score_work_clock() + timeout
    while pending:
        score_checkpoint(force=True)
        done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        if not done and score_work_clock() >= deadline:
            raise TimeoutError('score phase produced no result before deadline')
        yield from done
