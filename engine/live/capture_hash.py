"""回放抓包的内容哈希（用于 replay 缓存键）。

从原 `team_score_sync` 中抽出：只有它俩被回放缓存使用，
其余（匿名评分上报 / 云端同步）已整体移除。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=32)
def _capture_content_hash_cached(
    resolved_path: str,
    size: int,
    modified_ns: int,
    changed_ns: int = 0,
) -> str:
    capture_path = Path(resolved_path)
    quick_digest = hashlib.sha256()
    try:
        with capture_path.open("rb") as handle:
            quick_digest.update(handle.read(64 * 1024))
            if size > 64 * 1024:
                handle.seek(max(0, size - 64 * 1024))
                quick_digest.update(handle.read(64 * 1024))
        quick_signature = quick_digest.hexdigest()
        index_path = Path(
            os.environ.get(
                "DN_CAPTURE_HASH_INDEX_PATH",
                str(
                    Path.home()
                    / "Documents"
                    / "DragonNestDpsCapture"
                    / "replay_cache"
                    / "capture_hash_index.sqlite3"
                ),
            )
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(index_path, timeout=2.0) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_hash_index (
                    resolved_path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    changed_ns INTEGER NOT NULL,
                    quick_signature TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                """
                SELECT content_hash
                FROM capture_hash_index
                WHERE resolved_path = ? AND size = ? AND modified_ns = ?
                  AND changed_ns = ? AND quick_signature = ?
                """,
                (
                    resolved_path,
                    int(size),
                    int(modified_ns),
                    int(changed_ns),
                    quick_signature,
                ),
            ).fetchone()
            if row and re.fullmatch(r"[0-9a-f]{64}", str(row[0] or "")):
                return str(row[0])
    except (OSError, sqlite3.Error):
        index_path = None
        quick_signature = ""

    digest = hashlib.sha256()
    with capture_path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    content_hash = digest.hexdigest()
    if index_path is not None and quick_signature:
        try:
            with sqlite3.connect(index_path, timeout=2.0) as connection:
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute(
                    """
                    INSERT INTO capture_hash_index (
                        resolved_path, size, modified_ns, changed_ns,
                        quick_signature, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resolved_path) DO UPDATE SET
                        size = excluded.size,
                        modified_ns = excluded.modified_ns,
                        changed_ns = excluded.changed_ns,
                        quick_signature = excluded.quick_signature,
                        content_hash = excluded.content_hash
                    """,
                    (
                        resolved_path,
                        int(size),
                        int(modified_ns),
                        int(changed_ns),
                        quick_signature,
                        content_hash,
                    ),
                )
        except (OSError, sqlite3.Error):
            pass
    return content_hash


def capture_content_hash(path: Path) -> str:
    capture_path = Path(path).resolve()
    stat = capture_path.stat()
    return _capture_content_hash_cached(
        str(capture_path),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ctime_ns", 0)),
    )
