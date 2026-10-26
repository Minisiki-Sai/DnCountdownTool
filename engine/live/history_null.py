"""No-op history store for the lightweight countdown build.

The original store maintained a SQLite session archive (``live_history``)
plus per-session summaries. Both existed only for the removed history list,
replay loading, CSV export and score upload features. This build keeps the
overlay countdowns and writes nothing to disk, so the collector talks to an
API-compatible object that drops every call.

``live/history.py`` still documents the retired SQLite schema, but nothing
wires it into the capture pipeline any more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


class NullHistoryStore:
    """API-compatible stand-in that never persists anything."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.path = Path()
        self._session_id = ""
        self.diagnostics = lambda: {}

    def start_session(self, source: str = "") -> None:
        self._session_id = ""

    def append(self, events: Iterable[Any]) -> None:
        for _ in events:
            pass

    def finish_session(self, snapshot: Any = None) -> None:
        self._session_id = ""

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None
