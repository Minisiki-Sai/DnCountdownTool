from __future__ import annotations

import hashlib
import pickle
import zlib
from pathlib import Path
from typing import Any

from .team_score_sync import capture_content_hash
from .version_fingerprint import team_score_rules_fingerprint


TEAM_SCORE_CACHE_SCHEMA = "dragon-nest-team-score-cache-v9-defense-lanes"


def default_team_score_cache_directory() -> Path:
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "team_score_cache"
    )


def team_score_cache_key(
    capture_path: Path | None,
    context: object,
    *,
    capture_hash: str = "",
    data_root: Path | None = None,
) -> str:
    """Build a rename-independent, automatically versioned score key."""

    digest = hashlib.sha256()
    digest.update(TEAM_SCORE_CACHE_SCHEMA.encode("ascii"))
    content_hash = capture_hash or capture_content_hash(Path(capture_path))
    root = Path(data_root or Path(__file__).resolve().parents[1] / "data")
    digest.update(content_hash.encode("ascii"))
    digest.update(
        team_score_rules_fingerprint(str(root.resolve())).encode("ascii")
    )
    digest.update(pickle.dumps(context, protocol=5))
    return digest.hexdigest()


class TeamScoreDiskCache:
    """Small local cache for completed, exact team-score results."""

    def __init__(self, directory: Path | None = None, max_entries: int = 64) -> None:
        self.directory = Path(directory or default_team_score_cache_directory())
        self.max_entries = max(1, int(max_entries))

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.scorecache"

    def load(
        self, key: str
    ) -> tuple[tuple[Any, ...], dict[str, list[dict[str, Any]]]] | None:
        path = self._path(key)
        try:
            payload = pickle.loads(zlib.decompress(path.read_bytes()))
        except (OSError, ValueError, TypeError, pickle.PickleError, zlib.error):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != TEAM_SCORE_CACHE_SCHEMA:
            return None
        if payload.get("key") != key:
            return None
        rows = payload.get("rows")
        glyphs = payload.get("glyphs")
        if not isinstance(rows, tuple) or not isinstance(glyphs, dict):
            return None
        try:
            path.touch()
        except OSError:
            pass
        return rows, glyphs

    def save(
        self,
        key: str,
        rows: tuple[Any, ...],
        glyphs: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": TEAM_SCORE_CACHE_SCHEMA,
            "key": key,
            "rows": tuple(rows),
            "glyphs": glyphs,
        }
        encoded = zlib.compress(pickle.dumps(payload, protocol=5), level=3)
        destination = self._path(key)
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(destination)
        self._prune()

    def discard(self, key: str) -> None:
        """Remove one completed score so an explicit re-score cannot reuse it."""

        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def _prune(self) -> None:
        try:
            entries = sorted(
                self.directory.glob("*.scorecache"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        for path in entries[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass
