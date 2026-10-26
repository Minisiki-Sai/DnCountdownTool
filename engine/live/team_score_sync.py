from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .boss_hp import resolve_fixed_boss_hp_profile
from .compact_score_timeline import build_compact_score_timeline
from .dungeon_map_catalog import (
    is_battle_map,
    is_battle_map_name,
    resolve_dungeon_map_id,
)
from .nest_recognizer import canonical_nest_name, resolve_nest_evidence


TENCENT_DOCS_MCP_ENDPOINT = "https://docs.qq.com/openapi/mcp"
TENCENT_DOCS_TOKEN_URL = "https://docs.qq.com/open/auth/mcp.html"
TEAM_SCORE_FIELD_TITLES = (
    "游戏职业",
    "对应巢穴",
    "总分",
    "输出分",
    "辅助分",
    "特色分/承伤扣分",
    "记录时间",
    "数据指纹",
)
_DPSCAP_MAGIC = b"DNDP1\x00"
_DPSCAP_FRAME_HEADER = struct.Struct("<dBII")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}:[0-9]+$")
CURRENT_SUPABASE_SYNC_SCHEMA_VERSION = 7
# Scoring-rule revisions use the calendar date on which the scoring behavior
# changed. Bump this value whenever any score formula, weight, cap, penalty,
# completion metric, or score-detail interpretation changes.
CURRENT_SCORE_SCHEMA_VERSION = 20261019
_YAO_SCORE_VARIANTS = frozenset({"打坐曜", "输出曜"})
_YAO_SCORE_JOBS = frozenset({"曜", *_YAO_SCORE_VARIANTS})
_ALCHEMIST_SCORE_VARIANTS = frozenset({"辅助药剂", "输出药剂"})
_ALCHEMIST_SCORE_JOBS = frozenset({"药剂师", *_ALCHEMIST_SCORE_VARIANTS})


def _canonical_fingerprint(value: object) -> str:
    text = str(value or "").strip()
    if not _FINGERPRINT_RE.fullmatch(text):
        return text
    capture_hash, sample_ordinal = text.split(":", 1)
    return f"{capture_hash.casefold()}:{int(sample_ordinal)}"


def score_export_job(score: object) -> str:
    """Export an observed rotation duty without rewriting unrelated jobs."""

    job = str(getattr(score, "job", "") or "").strip()
    rotation_job = str(getattr(score, "rotation_job", "") or "").strip()
    return (
        rotation_job
        if (
            (job in _YAO_SCORE_JOBS and rotation_job in _YAO_SCORE_VARIANTS)
            or (job in _ALCHEMIST_SCORE_JOBS and rotation_job in _ALCHEMIST_SCORE_VARIANTS)
        )
        else job
    )


def attach_team_output_time(detail: dict[str, Any], team_windows) -> None:
    """Export the encounter denominator without changing personal scoring lanes."""
    from .scoring import _merge_intervals, _interval_duration

    merged_team_windows = _merge_intervals(team_windows)
    detail["team_output_windows"] = [list(interval) for interval in merged_team_windows]
    detail["team_output_seconds"] = round(_interval_duration(merged_team_windows), 3)
    detail["personal_output_seconds"] = round(
        _interval_duration(_merge_intervals(detail.get("output_windows") or ())), 3)
    detail["output_seconds"] = detail["team_output_seconds"]
    detail["output_seconds_scope"] = "team"


@dataclass(frozen=True, slots=True)
class TeamScoreExportRow:
    job: str
    total_score: float
    output_score: float
    support_score: float
    specialty_score: float | None
    survival_penalty: float
    theory_damage_completion: float | None = None
    actual_dps: float | None = None
    score_detail: dict[str, Any] | None = None
    character_identity: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StoredTeamScoreRow:
    capture_hash: str
    sample_ordinal: int
    job: str
    nest: str
    total_score: float
    output_score: float
    support_score: float
    specialty_score: float | None
    survival_penalty: float
    theory_damage_completion: float | None
    recorded_at: str
    score_detail: dict[str, Any] | None = None
    character_identity: dict[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.capture_hash}:{self.sample_ordinal}"

    @property
    def actual_dps(self) -> float | None:
        try:
            value = float((self.score_detail or {}).get("actual_dps"))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def document_values(self) -> dict[str, str]:
        specialty = (
            f"{self.specialty_score:.1f}"
            if self.specialty_score is not None
            else "-"
        )
        return {
            "游戏职业": self.job,
            "对应巢穴": self.nest,
            "总分": f"{self.total_score:.1f}",
            "输出分": f"{self.output_score:.1f}",
            "辅助分": f"{self.support_score:.1f}",
            "特色分/承伤扣分": (
                f"{specialty} / -{self.survival_penalty:.1f}"
            ),
            "记录时间": self.recorded_at,
            "数据指纹": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class StoredTeamScoreCapture:
    capture_hash: str
    source_name: str
    nest: str
    recorded_at: str
    rows: tuple[StoredTeamScoreRow, ...]


@dataclass(frozen=True, slots=True)
class StoredCaptureFile:
    capture_hash: str
    source_path: Path
    file_size: int


@dataclass(frozen=True, slots=True)
class RemoteRecord:
    record_id: str
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class SyncResult:
    duplicate_capture: bool
    stored_rows: int
    added_rows: int
    deleted_rows: int
    nest: str

    def summary(self) -> str:
        if self.duplicate_capture and not self.added_rows and not self.deleted_rows:
            return "已识别为重复数据，腾讯文档未重复写入"
        return (
            f"{self.nest or '未识别巢穴'}：本地 {self.stored_rows} 条，"
            f"新增 {self.added_rows} 条，清理旧记录 {self.deleted_rows} 条"
        )


class TencentDocsError(RuntimeError):
    pass


def default_team_score_sync_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "team_score_sync.sqlite3"
    )


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


def remember_capture_content_hash(path: Path, content_hash: str) -> bool:
    """Seed the persistent hash index from the live writer's verified digest.

    This records only lightweight file metadata plus the first/last 64 KiB
    signature.  A later replay load can validate the immutable file cheaply
    and find its decoded replay cache without hashing the full capture again.
    """

    normalized_hash = str(content_hash or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
        return False
    capture_path = Path(path).resolve()
    try:
        stat = capture_path.stat()
        size = int(stat.st_size)
        modified_ns = int(stat.st_mtime_ns)
        changed_ns = int(getattr(stat, "st_ctime_ns", 0))
        quick_digest = hashlib.sha256()
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
                    str(capture_path),
                    size,
                    modified_ns,
                    changed_ns,
                    quick_signature,
                    normalized_hash,
                ),
            )
        # Populate the in-process LRU too, so an immediate load in this app
        # session does not even need the SQLite lookup.
        _capture_content_hash_cached.cache_clear()
        return (
            _capture_content_hash_cached(
                str(capture_path), size, modified_ns, changed_ns
            )
            == normalized_hash
        )
    except (OSError, sqlite3.Error):
        return False


def capture_recorded_at(path: Path) -> str:
    capture_path = Path(path)
    timestamp = capture_path.stat().st_mtime
    if capture_path.suffix.casefold() == ".dpscap":
        try:
            with capture_path.open("rb") as handle:
                if handle.read(len(_DPSCAP_MAGIC)) == _DPSCAP_MAGIC:
                    header = handle.read(_DPSCAP_FRAME_HEADER.size)
                    if len(header) == _DPSCAP_FRAME_HEADER.size:
                        packet_time, _adapter, _original, _captured = (
                            _DPSCAP_FRAME_HEADER.unpack(header)
                        )
                        if 946_684_800 <= packet_time <= 4_102_444_800:
                            timestamp = packet_time
        except (OSError, ValueError, struct.error):
            pass
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(
        timespec="seconds"
    )


def resolve_score_nest(
    boss_names: Iterable[object],
    *,
    capture_path: Path | None = None,
    target_ids: Iterable[object] = (),
    map_ids: Iterable[object] = (),
    map_names: Iterable[object] = (),
) -> str:
    # An exact map identity packet is stronger evidence than Boss templates,
    # names or a user-edited filename.  The UI already uses this packet; score
    # upload must consume the same evidence to avoid showing one dungeon while
    # storing another.
    # Resolve catalog-backed map IDs separately from textual names.  A textual
    # name may be a stale UI/cache value from the previous scene, whereas an ID
    # decoded from a complete map-identity frame has protocol provenance.  This
    # is deliberately generic: no dungeon ID is special-cased here.
    exact_id_names: set[str] = set()
    for map_id in map_ids:
        entry = resolve_dungeon_map_id(map_id)
        if is_battle_map(entry) and entry.display_name:
            exact_id_names.add(canonical_nest_name(entry.display_name))
    exact_id_names.discard("")
    if len(exact_id_names) == 1:
        return next(iter(exact_id_names))
    if len(exact_id_names) > 1:
        return "未识别巢穴（多个地图：{}）".format(
            " / ".join(sorted(exact_id_names)[:3])
        )

    exact_name_hints = {
        name
        for value in map_names
        if is_battle_map_name(value)
        if (name := canonical_nest_name(value))
    }
    exact_name_hints.discard("")
    if len(exact_name_hints) == 1:
        return next(iter(exact_name_hints))
    if len(exact_name_hints) > 1:
        return "未识别巢穴（多个地图：{}）".format(
            " / ".join(sorted(exact_name_hints)[:3])
        )

    hint = capture_path.name if capture_path is not None else ""
    names_input = tuple(boss_names)
    targets_input = tuple(target_ids)
    nest_resolution = resolve_nest_evidence(
        names_input,
        capture_hint=hint,
        target_ids=targets_input,
    )
    if nest_resolution is not None:
        return nest_resolution.profile.label
    label, _references = resolve_fixed_boss_hp_profile(
        names_input,
        capture_hint=hint,
        target_ids=targets_input,
    )
    if label:
        return label
    names = [str(name or "").strip() for name in names_input]
    names = [name for name in names if name and "未识别" not in name]
    if names:
        return f"未识别巢穴（{' / '.join(dict.fromkeys(names[:3]))}）"
    return "未识别巢穴"


def parse_tencent_smartsheet_reference(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    parsed = urllib.parse.urlparse(text)
    if parsed.netloc.casefold().endswith("docs.qq.com"):
        match = re.search(r"/smartsheet/([A-Za-z0-9]+)", parsed.path)
        file_id = match.group(1) if match else ""
        query = urllib.parse.parse_qs(parsed.query)
        sheet_id = next(
            (
                values[0]
                for key in ("tab", "sheet_id", "sheetId")
                if (values := query.get(key))
            ),
            "",
        )
        return file_id, sheet_id
    if re.fullmatch(r"[A-Za-z0-9]+", text):
        return text, ""
    return "", ""


class TeamScoreSyncStore:
    def __init__(self, path: Path | None = None, *, read_only: bool = False) -> None:
        self.path = path or default_team_score_sync_path()
        if read_only:
            # Status/count queries must not deserialize every saved score or
            # run migrations/pruning. Keep the regular writer initialization
            # unchanged, including its privacy sanitization. URI mode also
            # prevents an accidental write through this connection.
            self._connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS captures (
                capture_hash TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                nest TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'pending',
                sync_error TEXT NOT NULL DEFAULT '',
                supabase_sync_status TEXT NOT NULL DEFAULT 'pending',
                supabase_sync_error TEXT NOT NULL DEFAULT '',
                supabase_sync_schema_version INTEGER NOT NULL DEFAULT 0,
                score_schema_version INTEGER NOT NULL DEFAULT 0,
                supabase_score_schema_version INTEGER NOT NULL DEFAULT 0,
                capture_file_sync_status TEXT NOT NULL DEFAULT 'pending',
                capture_file_sync_error TEXT NOT NULL DEFAULT '',
                capture_file_object_id TEXT NOT NULL DEFAULT '',
                capture_file_cloud_id TEXT NOT NULL DEFAULT '',
                capture_file_size INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        capture_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(captures)")
        }
        if "supabase_sync_status" not in capture_columns:
            self._connection.execute(
                "ALTER TABLE captures ADD COLUMN supabase_sync_status "
                "TEXT NOT NULL DEFAULT 'pending'"
            )
        if "supabase_sync_error" not in capture_columns:
            self._connection.execute(
                "ALTER TABLE captures ADD COLUMN supabase_sync_error "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "supabase_sync_schema_version" not in capture_columns:
            self._connection.execute(
                "ALTER TABLE captures ADD COLUMN supabase_sync_schema_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "score_schema_version" not in capture_columns:
            self._connection.execute(
                "ALTER TABLE captures ADD COLUMN score_schema_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "supabase_score_schema_version" not in capture_columns:
            self._connection.execute(
                "ALTER TABLE captures ADD COLUMN supabase_score_schema_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        for column, declaration in (
            ("capture_file_sync_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("capture_file_sync_error", "TEXT NOT NULL DEFAULT ''"),
            ("capture_file_object_id", "TEXT NOT NULL DEFAULT ''"),
            ("capture_file_cloud_id", "TEXT NOT NULL DEFAULT ''"),
            ("capture_file_size", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in capture_columns:
                self._connection.execute(
                    f"ALTER TABLE captures ADD COLUMN {column} {declaration}"
                )
        self._migrate_anonymous_score_rows()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS score_row_identities (
                capture_hash TEXT NOT NULL,
                sample_ordinal INTEGER NOT NULL,
                character_name TEXT NOT NULL,
                profile_id INTEGER NOT NULL DEFAULT 0,
                identity_source TEXT NOT NULL DEFAULT '',
                identity_confidence REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (capture_hash, sample_ordinal),
                FOREIGN KEY (capture_hash, sample_ordinal)
                    REFERENCES score_rows(capture_hash, sample_ordinal)
                    ON DELETE CASCADE
            );
            """
        )
        identity_columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(score_row_identities)"
            )
        }
        if "profile_id" not in identity_columns:
            self._connection.execute(
                "ALTER TABLE score_row_identities ADD COLUMN "
                "profile_id INTEGER NOT NULL DEFAULT 0"
            )
        self._sanitize_existing_score_details()
        self._prune_anonymous_samples(1000)
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_team_score_job_capture "
            "ON score_rows(job, capture_hash, sample_ordinal)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supabase_sync_status "
            "ON captures(supabase_sync_status, recorded_at)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_capture_file_sync_status "
            "ON captures(capture_file_sync_status, recorded_at)"
        )
        self._connection.commit()

    def _migrate_anonymous_score_rows(self) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'score_rows'"
        ).fetchone()
        if exists is None:
            self._create_anonymous_score_table("score_rows")
            return
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(score_rows)")
        }
        if "source_id" not in columns and "sample_ordinal" in columns:
            if "theory_damage_completion" not in columns:
                self._connection.execute(
                    "ALTER TABLE score_rows ADD COLUMN "
                    "theory_damage_completion REAL"
                )
            if "score_detail_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE score_rows ADD COLUMN score_detail_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
            return

        detail_column = "score_detail_json" if "score_detail_json" in columns else "'{}'"
        order_column = "source_id" if "source_id" in columns else "rowid"
        legacy_rows = self._connection.execute(
            f"""
                SELECT capture_hash, job, total_score, output_score, support_score,
                   specialty_score, survival_penalty, NULL, {detail_column}
            FROM score_rows
            ORDER BY capture_hash, {order_column}
            """
        ).fetchall()
        self._connection.execute("DROP TABLE IF EXISTS score_rows_anonymous")
        self._create_anonymous_score_table("score_rows_anonymous")
        ordinals: dict[str, int] = {}
        for row in legacy_rows:
            capture_hash = str(row[0])
            ordinal = ordinals.get(capture_hash, 0)
            ordinals[capture_hash] = ordinal + 1
            self._connection.execute(
                """
                INSERT INTO score_rows_anonymous(
                    capture_hash, sample_ordinal, job, total_score, output_score,
                    support_score, specialty_score, survival_penalty,
                    theory_damage_completion, score_detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (capture_hash, ordinal, *row[1:]),
            )
        self._connection.execute("DROP TABLE score_rows")
        self._connection.execute(
            "ALTER TABLE score_rows_anonymous RENAME TO score_rows"
        )

    def _create_anonymous_score_table(self, name: str) -> None:
        if name not in {"score_rows", "score_rows_anonymous"}:
            raise ValueError("invalid score table name")
        self._connection.execute(
            f"""
            CREATE TABLE {name} (
                capture_hash TEXT NOT NULL,
                sample_ordinal INTEGER NOT NULL,
                job TEXT NOT NULL,
                total_score REAL NOT NULL,
                output_score REAL NOT NULL,
                support_score REAL NOT NULL,
                specialty_score REAL,
                survival_penalty REAL NOT NULL,
                theory_damage_completion REAL,
                score_detail_json TEXT NOT NULL DEFAULT '{{}}',
                PRIMARY KEY (capture_hash, sample_ordinal),
                FOREIGN KEY (capture_hash) REFERENCES captures(capture_hash)
            )
            """
        )

    def _sanitize_existing_score_details(self) -> None:
        updates: list[tuple[str, str, int]] = []
        for capture_hash, sample_ordinal, raw_detail in self._connection.execute(
            "SELECT capture_hash, sample_ordinal, score_detail_json FROM score_rows"
        ):
            try:
                decoded = json.loads(str(raw_detail or "{}"))
            except (TypeError, ValueError):
                decoded = {}
            sanitized = json.dumps(
                self._score_details(decoded), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            if sanitized != str(raw_detail or "{}"):
                updates.append((sanitized, str(capture_hash), int(sample_ordinal)))
        if updates:
            self._connection.executemany(
                "UPDATE score_rows SET score_detail_json = ? "
                "WHERE capture_hash = ? AND sample_ordinal = ?",
                updates,
            )

    def record_capture(
        self,
        capture_path: Path,
        nest: str,
        rows: Iterable[TeamScoreExportRow],
        *,
        force_supabase_sync: bool = False,
    ) -> tuple[str, bool, int]:
        capture_path = Path(capture_path).resolve()
        source_path = str(capture_path)
        file_size = int(capture_path.stat().st_size)
        capture_hash = capture_content_hash(capture_path)
        valid_rows = tuple(
            TeamScoreExportRow(
                job=row.job.strip(),
                total_score=row.total_score,
                output_score=row.output_score,
                support_score=row.support_score,
                specialty_score=row.specialty_score,
                survival_penalty=row.survival_penalty,
                theory_damage_completion=row.theory_damage_completion,
                actual_dps=row.actual_dps,
                score_detail=self._score_details({
                    **(
                        row.score_detail
                        if isinstance(row.score_detail, dict)
                        else {}
                    ),
                    **(
                        {"actual_dps": row.actual_dps}
                        if row.actual_dps is not None
                        else {}
                    ),
                }),
                character_identity=self._character_identity(
                    row.character_identity
                ),
            )
            for row in rows
            if row.job.strip()
            and row.job.strip() != "未知职业"
        )
        capture_state = self._connection.execute(
            "SELECT score_schema_version, nest FROM captures "
            "WHERE capture_hash = ?",
            (capture_hash,),
        ).fetchone()
        duplicate = capture_state is not None
        score_schema_changed = (
            duplicate
            and int(capture_state[0] or 0) != CURRENT_SCORE_SCHEMA_VERSION
        )
        nest_changed = (
            duplicate
            and str(capture_state[1] or "").strip() != str(nest or "").strip()
        )
        if duplicate:
            row_changed = False
            with self._connection:
                self._connection.execute(
                    "UPDATE captures SET source_path = ?, capture_file_size = ?, "
                    "nest = ? "
                    "WHERE capture_hash = ?",
                    (source_path, file_size, nest, capture_hash),
                )
                for sample_ordinal, row in enumerate(valid_rows):
                    previous = self._connection.execute(
                        "SELECT job, total_score, output_score, support_score, "
                        "specialty_score, survival_penalty, "
                        "theory_damage_completion, score_detail_json "
                        "FROM score_rows WHERE capture_hash = ? "
                        "AND sample_ordinal = ?",
                        (capture_hash, sample_ordinal),
                    ).fetchone()
                    detail_json = json.dumps(
                        row.score_detail or {}, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"),
                    )
                    current = (
                        row.job, row.total_score, row.output_score,
                        row.support_score, row.specialty_score,
                        row.survival_penalty, row.theory_damage_completion,
                        detail_json,
                    )
                    if previous is None:
                        self._connection.execute(
                            "INSERT INTO score_rows(capture_hash, sample_ordinal, "
                            "job, total_score, "
                            "output_score, support_score, specialty_score, "
                            "survival_penalty, theory_damage_completion, "
                            "score_detail_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (capture_hash, sample_ordinal, *current),
                        )
                        row_changed = True
                        continue
                    if current == tuple(previous):
                        continue
                    self._connection.execute(
                        "UPDATE score_rows SET job = ?, total_score = ?, "
                        "output_score = ?, support_score = ?, "
                        "specialty_score = ?, survival_penalty = ?, "
                        "theory_damage_completion = ?, score_detail_json = ? "
                        "WHERE capture_hash = ? AND sample_ordinal = ?",
                        (*current, capture_hash, sample_ordinal),
                    )
                    row_changed = True
                removed = self._connection.execute(
                    "DELETE FROM score_rows WHERE capture_hash = ? "
                    "AND sample_ordinal >= ?",
                    (capture_hash, len(valid_rows)),
                ).rowcount
                row_changed = row_changed or bool(removed)
                identity_changed = self._replace_score_identities(
                    capture_hash, valid_rows
                )
                row_changed = row_changed or identity_changed
                if (
                    row_changed
                    or score_schema_changed
                    or nest_changed
                    or force_supabase_sync
                ):
                    self._connection.execute(
                        "UPDATE captures SET supabase_sync_status = 'pending', "
                        "supabase_sync_error = '', score_schema_version = ? "
                        "WHERE capture_hash = ?",
                        (CURRENT_SCORE_SCHEMA_VERSION, capture_hash),
                    )
                else:
                    self._connection.execute(
                        "UPDATE captures SET score_schema_version = ? "
                        "WHERE capture_hash = ?",
                        (CURRENT_SCORE_SCHEMA_VERSION, capture_hash),
                    )
                self._prune_anonymous_samples(1000)
            return capture_hash, True, 0
        recorded_at = capture_recorded_at(capture_path)
        stored_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO captures(
                    capture_hash, source_path, nest, recorded_at, stored_at,
                    score_schema_version, capture_file_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_hash,
                    source_path,
                    nest,
                    recorded_at,
                    stored_at,
                    CURRENT_SCORE_SCHEMA_VERSION,
                    file_size,
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO score_rows(
                    capture_hash, sample_ordinal, job, total_score, output_score,
                    support_score, specialty_score, survival_penalty,
                    theory_damage_completion, score_detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        capture_hash,
                        sample_ordinal,
                        row.job,
                        row.total_score,
                        row.output_score,
                        row.support_score,
                        row.specialty_score,
                        row.survival_penalty,
                        row.theory_damage_completion,
                        json.dumps(
                            row.score_detail or {}, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"),
                        ),
                    )
                    for sample_ordinal, row in enumerate(valid_rows)
                ],
            )
            self._replace_score_identities(capture_hash, valid_rows)
            self._prune_anonymous_samples(1000)
        return capture_hash, False, len(valid_rows)

    @staticmethod
    def _score_details(value: object) -> dict[str, Any]:
        from .settlement import completion_upload_metadata

        detail = value if isinstance(value, dict) else {}
        compact_timeline = build_compact_score_timeline(detail)
        specialty_keys = (
            "role", "role_reason", "rotation_job", "specialty_label", "specialty_score",
            "specialty_weight", "specialty_contribution", "support_index",
            "coverage_completion", "contribution_completion",
            "healing_completion", "healing_cast_completion",
            "effective_healing", "healing_effective_realization",
            "healing_rescue_completion",
            "healing_damage_reduction_completion",
            "healing_pressure_windows", "healing_meaningful_casts",
            "healing_total_casts", "tank_completion",
            "healing_evaluated_casts",
            "healing_rescue_detail",
            "tank_attack_down_completion", "tank_taunt_completion",
            "tank_attack_down_coverage", "tank_attack_down_theory_coverage",
            "tank_attack_down_evidence",
            "tank_shell_completion",
            "tank_shell_coverage", "tank_shell_party_size",
            "tank_shell_theory_coverage", "tank_shell_theory_casts", "tank_shell_evidence",
            "tank_taunt_coverage", "tank_taunt_theory_coverage", "tank_taunt_evidence",
        )
        stored_specialty_detail = (
            detail.get("specialty_detail", {})
            if isinstance(detail.get("specialty_detail"), dict)
            else {}
        )
        support_keys = (
            "skill_name", "effect_key", "kind", "magnitude",
            "actual_coverage", "theory_coverage", "completion",
            "score_weight", "actual_contribution", "theory_contribution",
            "expected_duration", "included_in_score",
            "actual_covered_damage_share", "theory_covered_damage_share",
            "effective_damage_coverage", "effective_damage_theory_coverage",
            "support_benefit_completion",
            "actual_direct_contribution", "actual_linked_contribution",
            "actual_total_contribution", "theory_direct_contribution",
            "theory_linked_contribution", "theory_total_contribution",
            "linked_contribution_available",
            "linked_contribution_included_in_score",
            "observed_coverage", "effective_state", "effective_reason",
            "application_coverage",
        )
        support_details = [
            {
                key: row.get(key)
                for key in support_keys
                if key in row
            }
            for row in (
                detail.get("support_details", [])
                if isinstance(detail.get("support_details"), (list, tuple))
                else []
            )
            if isinstance(row, dict)
        ]
        result = {
            "skill_details": list(detail.get("skill_details", []))
            if isinstance(detail.get("skill_details"), (list, tuple)) else [],
            # Coverage intervals are now dictionary/delta encoded once in the
            # compact timeline. Keeping them here as well can more than double
            # the CloudBase JSONB size for long runs.
            "support_details": support_details,
            "specialty_detail": {
                key: (
                    detail.get(key)
                    if key in detail
                    else stored_specialty_detail.get(key)
                )
                for key in specialty_keys
                if key in detail or key in stored_specialty_detail
            },
            "survival_detail": (
                dict(detail.get("survival_detail", {}))
                if isinstance(detail.get("survival_detail"), dict)
                else {}
            ),
            "notes": list(detail.get("notes", []))
            if isinstance(detail.get("notes"), (list, tuple)) else [],
        }
        if "instance_completion" in detail:
            result["instance_completion"] = completion_upload_metadata(detail["instance_completion"])
        if isinstance(detail.get("scoring_equipment"), dict):
            result["scoring_equipment"] = detail["scoring_equipment"]
        if isinstance(detail.get("boss_debuffs"), dict):
            result["boss_debuffs"] = detail["boss_debuffs"]
        if isinstance(detail.get("damage_overview"), dict):
            result["damage_overview"] = detail["damage_overview"]
        try:
            actual_dps = float(detail.get("actual_dps"))
        except (TypeError, ValueError):
            actual_dps = -1.0
        if math.isfinite(actual_dps) and actual_dps >= 0:
            result["actual_dps"] = actual_dps
        if compact_timeline:
            result["compact_timeline"] = compact_timeline
            try:
                quantum = max(1.0, float(compact_timeline.get("q") or 100))
                output_seconds = sum(
                    max(0.0, float(row[1])) / quantum
                    for row in (compact_timeline.get("o") or ())
                    if isinstance(row, (list, tuple)) and len(row) == 2
                )
            except (TypeError, ValueError):
                output_seconds = 0.0
            if math.isfinite(output_seconds) and output_seconds > 0:
                # Keep the scalar beside the compact timeline so history lists
                # can show it without downloading and decoding every detail.
                result["output_seconds"] = round(output_seconds, 3)
        # New exports carry an explicit team denominator. Do not overwrite it
        # with the personal compact lane, including on SQLite re-sanitization.
        # Legacy rows keep their old fallback; never guess team time by taking
        # the largest personal duration or the full recording length.
        for key in ("team_output_seconds", "personal_output_seconds"):
            try:
                value = float(detail.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                result[key] = round(value, 3)
        if "team_output_seconds" in result:
            result["output_seconds"] = result["team_output_seconds"]
            result["output_seconds_scope"] = "team"
        if isinstance(detail.get("team_output_windows"), (list, tuple)):
            from .scoring import _merge_intervals
            result["team_output_windows"] = [
                list(interval) for interval in _merge_intervals(detail["team_output_windows"])
            ]
        return result

    @staticmethod
    def _character_identity(value: object) -> dict[str, Any]:
        identity = value if isinstance(value, dict) else {}
        name = str(identity.get("character_name") or identity.get("name") or "").strip()
        if (
            not name
            or len(name) > 64
            or re.fullmatch(r"(?:角色\s*)?0x[0-9A-Fa-f]+", name)
            or name in {"未知角色", "未知职业"}
        ):
            return {}
        try:
            confidence = max(0.0, min(1.0, float(identity.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.90 or identity.get("trusted") is False:
            return {}
        raw_profile_id = str(
            identity.get("profile_id") or identity.get("profileId") or ""
        ).strip()
        try:
            profile_id = int(
                raw_profile_id,
                16 if raw_profile_id.casefold().startswith("0x") else 10,
            )
        except (TypeError, ValueError):
            profile_id = 0
        return {
            "character_name": name,
            "profile_id": profile_id if profile_id > 0 else 0,
            "source": str(identity.get("source") or "verified_identity")[:64],
            "confidence": confidence,
        }

    def _replace_score_identities(
        self,
        capture_hash: str,
        rows: tuple[TeamScoreExportRow, ...],
    ) -> bool:
        previous = {
            int(ordinal): (str(name), int(profile_id or 0), str(source), float(confidence))
            for ordinal, name, profile_id, source, confidence in self._connection.execute(
                "SELECT sample_ordinal, character_name, profile_id, identity_source, "
                "identity_confidence FROM score_row_identities "
                "WHERE capture_hash = ?",
                (capture_hash,),
            )
        }
        current = {
            ordinal: (
                str(identity["character_name"]),
                int(identity.get("profile_id") or 0),
                str(identity.get("source") or ""),
                float(identity.get("confidence") or 0),
            )
            for ordinal, row in enumerate(rows)
            if (identity := self._character_identity(row.character_identity))
        }
        if previous == current:
            return False
        self._connection.execute(
            "DELETE FROM score_row_identities WHERE capture_hash = ?",
            (capture_hash,),
        )
        if current:
            self._connection.executemany(
                "INSERT INTO score_row_identities("
                "capture_hash, sample_ordinal, character_name, profile_id, "
                "identity_source, identity_confidence) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (capture_hash, ordinal, *values)
                    for ordinal, values in sorted(current.items())
                ],
            )
        return True

    def _prune_anonymous_samples(self, keep_count: int = 1000) -> int:
        keep = max(1, min(1000, int(keep_count)))
        rows = self._connection.execute(
            """
            SELECT r.capture_hash, r.sample_ordinal, r.job, c.nest
            FROM score_rows AS r
            JOIN captures AS c ON c.capture_hash = r.capture_hash
            ORDER BY c.recorded_at DESC, r.capture_hash DESC,
                     r.sample_ordinal DESC
            """
        ).fetchall()
        counts: dict[tuple[str, str], int] = {}
        remove: list[tuple[str, int]] = []
        for capture_hash, sample_ordinal, job, nest in rows:
            key = (str(job), str(nest))
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > keep:
                remove.append((str(capture_hash), int(sample_ordinal)))
        if remove:
            self._connection.executemany(
                "DELETE FROM score_rows WHERE capture_hash = ? AND sample_ordinal = ?",
                remove,
            )
        return len(remove)

    def retained_rows(self, limit: int = 1000) -> tuple[StoredTeamScoreRow, ...]:
        records = [
            StoredTeamScoreRow(
                capture_hash=str(row[0]),
                sample_ordinal=int(row[1]),
                job=str(row[2]),
                nest=str(row[3]),
                total_score=float(row[4]),
                output_score=float(row[5]),
                support_score=float(row[6]),
                specialty_score=(float(row[7]) if row[7] is not None else None),
                survival_penalty=float(row[8]),
                theory_damage_completion=(
                    float(row[9]) if row[9] is not None else None
                ),
                recorded_at=str(row[10]),
                score_detail=(json.loads(row[11]) if row[11] else {}),
                character_identity=(
                    {
                        "character_name": str(row[12]),
                        "profile_id": int(row[13] or 0),
                        "source": str(row[14] or ""),
                        "confidence": float(row[15] or 0),
                        "trusted": True,
                    }
                    if row[12]
                    else {}
                ),
            )
            for row in self._connection.execute(
                """
                SELECT r.capture_hash, r.sample_ordinal, r.job, c.nest,
                       r.total_score, r.output_score, r.support_score,
                       r.specialty_score, r.survival_penalty,
                       r.theory_damage_completion, c.recorded_at,
                       r.score_detail_json, i.character_name, i.profile_id,
                       i.identity_source, i.identity_confidence
                FROM score_rows AS r
                JOIN captures AS c ON c.capture_hash = r.capture_hash
                LEFT JOIN score_row_identities AS i
                  ON i.capture_hash = r.capture_hash
                 AND i.sample_ordinal = r.sample_ordinal
                ORDER BY c.recorded_at DESC, r.capture_hash DESC
                """
            )
        ]
        grouped: dict[tuple[str, str], list[StoredTeamScoreRow]] = {}
        for row in records:
            bucket = grouped.setdefault((row.job, row.nest), [])
            if len(bucket) < max(1, int(limit)):
                bucket.append(row)
        return tuple(
            sorted(
                (row for bucket in grouped.values() for row in bucket),
                key=lambda row: (row.recorded_at, row.sample_ordinal),
            )
        )

    def mark_all_synced(self) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE captures SET sync_status = 'synced', sync_error = ''"
            )

    def mark_error(self, message: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE captures
                SET sync_status = 'pending', sync_error = ?
                WHERE sync_status != 'synced'
                """,
                (str(message)[:1000],),
            )

    def pending_supabase_captures(
        self,
        limit: int = 20,
        minimum_schema_version: int = CURRENT_SUPABASE_SYNC_SCHEMA_VERSION,
        score_schema_version: int = CURRENT_SCORE_SCHEMA_VERSION,
        exclude_hashes: Iterable[str] = (),
    ) -> tuple[StoredTeamScoreCapture, ...]:
        excluded = tuple(dict.fromkeys(str(value) for value in exclude_hashes if value))
        exclusion = (" AND capture_hash NOT IN (" + ",".join("?" for _ in excluded) + ")") if excluded else ""
        capture_records = self._connection.execute(
            f"""
            SELECT capture_hash, source_path, nest, recorded_at
            FROM captures
            WHERE score_schema_version = ?
              AND (supabase_sync_status != 'synced'
               OR supabase_sync_schema_version < ?
               OR supabase_score_schema_version < ?)
            {exclusion}
            ORDER BY recorded_at ASC, capture_hash ASC
            LIMIT ?
            """,
            (
                max(0, int(score_schema_version)),
                max(0, int(minimum_schema_version)),
                max(0, int(score_schema_version)),
                *excluded,
                max(1, int(limit)),
            ),
        ).fetchall()
        captures: list[StoredTeamScoreCapture] = []
        for capture_hash, source_path, nest, recorded_at in capture_records:
            rows = tuple(
                StoredTeamScoreRow(
                    capture_hash=str(capture_hash),
                    sample_ordinal=int(row[0]),
                    job=str(row[1]),
                    nest=str(nest),
                    total_score=float(row[2]),
                    output_score=float(row[3]),
                    support_score=float(row[4]),
                    specialty_score=(
                        float(row[5]) if row[5] is not None else None
                    ),
                    survival_penalty=float(row[6]),
                    theory_damage_completion=(
                        float(row[7]) if row[7] is not None else None
                    ),
                    recorded_at=str(recorded_at),
                    score_detail=(json.loads(row[8]) if row[8] else {}),
                    character_identity=(
                        {
                            "character_name": str(row[9]),
                            "profile_id": int(row[10] or 0),
                            "source": str(row[11] or ""),
                            "confidence": float(row[12] or 0),
                            "trusted": True,
                        }
                        if row[9]
                        else {}
                    ),
                )
                for row in self._connection.execute(
                    """
                    SELECT r.sample_ordinal, r.job, r.total_score, r.output_score,
                           r.support_score, r.specialty_score, r.survival_penalty,
                           r.theory_damage_completion, r.score_detail_json,
                           i.character_name, i.profile_id, i.identity_source,
                           i.identity_confidence
                    FROM score_rows AS r
                    LEFT JOIN score_row_identities AS i
                      ON i.capture_hash = r.capture_hash
                     AND i.sample_ordinal = r.sample_ordinal
                    WHERE r.capture_hash = ?
                    ORDER BY r.sample_ordinal ASC
                    """,
                    (capture_hash,),
                )
            )
            captures.append(
                StoredTeamScoreCapture(
                    capture_hash=str(capture_hash),
                    source_name=Path(str(source_path)).name,
                    nest=str(nest),
                    recorded_at=str(recorded_at),
                    rows=rows,
                )
            )
        return tuple(captures)

    def pending_supabase_count(
        self,
        minimum_schema_version: int = CURRENT_SUPABASE_SYNC_SCHEMA_VERSION,
        score_schema_version: int = CURRENT_SCORE_SCHEMA_VERSION,
    ) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM captures
            WHERE score_schema_version = ?
              AND (supabase_sync_status != 'synced'
               OR supabase_sync_schema_version < ?
               OR supabase_score_schema_version < ?)
            """,
            (
                max(0, int(score_schema_version)),
                max(0, int(minimum_schema_version)),
                max(0, int(score_schema_version)),
            ),
        ).fetchone()
        return int(row[0] if row else 0)

    def mark_supabase_synced(
        self,
        capture_hashes: Iterable[str],
        schema_version: int = CURRENT_SUPABASE_SYNC_SCHEMA_VERSION,
        score_schema_version: int = CURRENT_SCORE_SCHEMA_VERSION,
    ) -> None:
        hashes = tuple(dict.fromkeys(str(value) for value in capture_hashes if value))
        if not hashes:
            return
        placeholders = ",".join("?" for _value in hashes)
        with self._connection:
            self._connection.execute(
                f"""
                UPDATE captures
                SET supabase_sync_status = 'synced', supabase_sync_error = '',
                    supabase_sync_schema_version = ?,
                    supabase_score_schema_version = ?
                WHERE capture_hash IN ({placeholders})
                """,
                (
                    max(0, int(schema_version)),
                    max(0, int(score_schema_version)),
                    *hashes,
                ),
            )

    def mark_supabase_error(
        self, message: str, capture_hashes: Iterable[str] = ()
    ) -> None:
        hashes = tuple(dict.fromkeys(str(value) for value in capture_hashes if value))
        with self._connection:
            if hashes:
                placeholders = ",".join("?" for _value in hashes)
                self._connection.execute(
                    f"""
                    UPDATE captures
                    SET supabase_sync_status = 'pending', supabase_sync_error = ?
                    WHERE capture_hash IN ({placeholders})
                    """,
                    (str(message)[:1000], *hashes),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE captures
                    SET supabase_sync_status = 'pending', supabase_sync_error = ?
                    WHERE supabase_sync_status != 'synced'
                    """,
                    (str(message)[:1000],),
                )

    def pending_capture_files(self, limit: int = 5) -> tuple[StoredCaptureFile, ...]:
        records = self._connection.execute(
            """
            SELECT capture_hash, source_path, capture_file_size
            FROM captures
            WHERE supabase_sync_status = 'synced'
              AND capture_file_sync_status != 'synced'
              AND source_path != ''
              AND EXISTS (
                  SELECT 1 FROM score_rows
                  WHERE score_rows.capture_hash = captures.capture_hash
              )
            ORDER BY recorded_at ASC, capture_hash ASC
            LIMIT ?
            """,
            (max(1, min(25, int(limit))),),
        ).fetchall()
        pending: list[StoredCaptureFile] = []
        for capture_hash, source_path, stored_size in records:
            path = Path(str(source_path))
            if path.suffix.casefold() != ".dpscap":
                continue
            try:
                size = int(path.stat().st_size)
            except OSError as exc:
                self.mark_capture_file_error(
                    str(capture_hash), f"本地抓包文件不可用：{exc}"
                )
                continue
            if size <= 0:
                self.mark_capture_file_error(str(capture_hash), "本地抓包文件为空")
                continue
            if int(stored_size or 0) != size:
                with self._connection:
                    self._connection.execute(
                        "UPDATE captures SET capture_file_size = ? "
                        "WHERE capture_hash = ?",
                        (size, str(capture_hash)),
                    )
            pending.append(
                StoredCaptureFile(
                    capture_hash=str(capture_hash),
                    source_path=path,
                    file_size=size,
                )
            )
        return tuple(pending)

    def pending_capture_file_count(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM captures
            WHERE supabase_sync_status = 'synced'
              AND capture_file_sync_status != 'synced'
              AND source_path != ''
              AND EXISTS (
                  SELECT 1 FROM score_rows
                  WHERE score_rows.capture_hash = captures.capture_hash
              )
            """
        ).fetchone()
        return int(row[0] if row else 0)

    def mark_capture_file_synced(
        self,
        capture_hash: str,
        *,
        object_id: str,
        cloud_object_id: str,
        file_size: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE captures
                SET capture_file_sync_status = 'synced',
                    capture_file_sync_error = '',
                    capture_file_object_id = ?,
                    capture_file_cloud_id = ?,
                    capture_file_size = ?
                WHERE capture_hash = ?
                """,
                (
                    str(object_id), str(cloud_object_id), max(0, int(file_size)),
                    str(capture_hash),
                ),
            )

    def mark_capture_file_error(self, capture_hash: str, message: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE captures
                SET capture_file_sync_status = 'pending',
                    capture_file_sync_error = ?
                WHERE capture_hash = ?
                """,
                (str(message)[:1000], str(capture_hash)),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> TeamScoreSyncStore:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def _json_from_mcp_content(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or "text" not in item:
                continue
            text = str(item.get("text", ""))
            try:
                return json.loads(text)
            except ValueError:
                return {"text": text}
    return result


class TencentDocsMcpClient:
    def __init__(
        self,
        token: str,
        *,
        endpoint: str = TENCENT_DOCS_MCP_ENDPOINT,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not token.strip():
            raise TencentDocsError("尚未填写腾讯文档 MCP Token")
        self.token = token.strip()
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._request_id = 0

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": self.token,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TencentDocsError(
                f"腾讯文档接口返回 HTTP {exc.code}：{body[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TencentDocsError(f"连接腾讯文档失败：{exc}") from exc
        try:
            decoded = json.loads(body)
        except ValueError as exc:
            raise TencentDocsError(
                f"腾讯文档返回了无法解析的数据：{body[:300]}"
            ) from exc
        if isinstance(decoded, dict) and decoded.get("error"):
            error = decoded["error"]
            message = (
                error.get("data", {}).get("message")
                if isinstance(error, dict)
                and isinstance(error.get("data"), dict)
                else ""
            )
            if not message and isinstance(error, dict):
                message = error.get("message", "")
            raise TencentDocsError(f"腾讯文档接口错误：{message or error}")
        return decoded.get("result") if isinstance(decoded, dict) else decoded

    def list_tools(self) -> tuple[dict[str, Any], ...]:
        result = self._request("tools/list")
        tools = result.get("tools", ()) if isinstance(result, dict) else ()
        return tuple(tool for tool in tools if isinstance(tool, dict))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content", ())
            message = ""
            if isinstance(content, list):
                message = " ".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                ).strip()
            raise TencentDocsError(
                f"腾讯文档工具 {name} 调用失败：{message or result}"
            )
        return _json_from_mcp_content(result)


def _walk_named_lists(payload: Any, names: set[str]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.casefold() in names and isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in payload.values():
            found = _walk_named_lists(value, names)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _walk_named_lists(value, names)
            if found:
                return found
    return []


def _find_named_value(payload: Any, names: set[str], default: Any = None) -> Any:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.casefold() in names:
                return value
        for value in payload.values():
            found = _find_named_value(value, names, default)
            if found is not default:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_named_value(value, names, default)
            if found is not default:
                return found
    return default


def _decode_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, dict):
                pieces.append(
                    str(
                        item.get("text")
                        or item.get("value")
                        or item.get("name")
                        or ""
                    )
                )
            else:
                pieces.append(str(item))
        return "".join(pieces)
    if isinstance(value, dict):
        for key in (
            "text",
            "value",
            "name",
            "string_value",
            "number_value",
            "bool_value",
            "text_value",
            "option_value",
            "url_value",
            "items",
        ):
            if key in value:
                return _decode_field_value(value[key])
    return str(value)


class TencentSmartSheetGateway:
    def __init__(self, client: TencentDocsMcpClient) -> None:
        self.client = client
        self.tools = client.list_tools()

    def _tool(self, suffix: str, *, required: bool = True) -> dict[str, Any] | None:
        normalized_suffix = suffix.replace(".", "_").casefold()
        for tool in self.tools:
            name = str(tool.get("name", ""))
            normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
            if normalized.endswith(normalized_suffix):
                return tool
        if required:
            raise TencentDocsError(f"腾讯文档当前未提供 {suffix} 工具")
        return None

    @staticmethod
    def _property_names(tool: dict[str, Any]) -> set[str]:
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        return set(properties) if isinstance(properties, dict) else set()

    @classmethod
    def _base_arguments(
        cls, tool: dict[str, Any], file_id: str, sheet_id: str = ""
    ) -> dict[str, Any]:
        properties = cls._property_names(tool)
        args: dict[str, Any] = {}
        file_key = next(
            (key for key in ("file_id", "fileId", "fileID") if key in properties),
            "file_id",
        )
        args[file_key] = file_id
        if sheet_id:
            sheet_key = next(
                (
                    key
                    for key in ("sheet_id", "sheetId", "table_id", "tableId")
                    if key in properties
                ),
                "sheet_id",
            )
            args[sheet_key] = sheet_id
        return args

    def resolve_sheet_id(self, file_id: str, requested: str = "") -> str:
        if requested.strip():
            return requested.strip()
        tool = self._tool("list_tables")
        payload = self.client.call_tool(
            str(tool["name"]), self._base_arguments(tool, file_id)
        )
        tables = _walk_named_lists(payload, {"tables", "sheets", "data"})
        for table in tables:
            value = next(
                (
                    table.get(key)
                    for key in ("sheet_id", "sheetId", "table_id", "tableId", "id")
                    if table.get(key)
                ),
                "",
            )
            if value:
                return str(value)
        raise TencentDocsError("该腾讯智能表格中没有可用工作表")

    def _list_fields(
        self, file_id: str, sheet_id: str
    ) -> tuple[dict[str, Any], ...]:
        tool = self._tool("list_fields")
        payload = self.client.call_tool(
            str(tool["name"]), self._base_arguments(tool, file_id, sheet_id)
        )
        fields = _walk_named_lists(payload, {"fields", "data"})
        return tuple(fields)

    @staticmethod
    def _field_title(field: dict[str, Any]) -> str:
        return str(
            field.get("field_title")
            or field.get("fieldTitle")
            or field.get("title")
            or field.get("name")
            or ""
        ).strip()

    @staticmethod
    def _field_id(field: dict[str, Any]) -> str:
        return str(
            field.get("field_id")
            or field.get("fieldId")
            or field.get("id")
            or ""
        ).strip()

    def ensure_fields(self, file_id: str, sheet_id: str) -> dict[str, str]:
        def read_existing() -> dict[str, str]:
            return {
                self._field_title(field): self._field_id(field)
                for field in self._list_fields(file_id, sheet_id)
                if self._field_title(field)
            }

        existing = read_existing()
        missing = [title for title in TEAM_SCORE_FIELD_TITLES if title not in existing]
        if missing:
            tool = self._tool("add_fields")
            base = self._base_arguments(tool, file_id, sheet_id)
            row_factories = (
                lambda title: {
                    "field_title": title,
                    "field_type": "text",
                    "property_text": {},
                },
                lambda title: {"field_title": title, "field_type": "text"},
                lambda title: {"title": title, "field_type": "text"},
                lambda title: {"name": title, "type": "text"},
            )
            last_error: Exception | None = None
            for make_row in row_factories:
                missing = [
                    title for title in TEAM_SCORE_FIELD_TITLES if title not in existing
                ]
                if not missing:
                    break
                try:
                    self.client.call_tool(
                        str(tool["name"]),
                        {**base, "fields": [make_row(title) for title in missing]},
                    )
                    last_error = None
                except TencentDocsError as exc:
                    last_error = exc
                # Tencent SmartSheet can commit a batch and still report a
                # business error for the same call. Re-read the document after
                # every attempt and trust its persisted state, otherwise a
                # successful initialization is incorrectly left pending.
                existing = read_existing()
                if all(title in existing for title in TEAM_SCORE_FIELD_TITLES):
                    last_error = None
                    break
            if last_error is not None and any(
                title not in existing for title in TEAM_SCORE_FIELD_TITLES
            ):
                raise last_error
        missing = [title for title in TEAM_SCORE_FIELD_TITLES if title not in existing]
        if missing:
            raise TencentDocsError(
                f"智能表格缺少字段：{', '.join(missing)}"
            )
        return existing

    def list_records(
        self,
        file_id: str,
        sheet_id: str,
        field_ids: dict[str, str],
    ) -> tuple[RemoteRecord, ...]:
        tool = self._tool("list_records")
        properties = self._property_names(tool)
        id_to_title = {value: key for key, value in field_ids.items() if value}
        rows: list[RemoteRecord] = []
        cursor = ""
        page = 1
        offset = 0
        for _iteration in range(200):
            args = self._base_arguments(tool, file_id, sheet_id)
            if "page_size" in properties:
                args["page_size"] = 100
            elif "pageSize" in properties:
                args["pageSize"] = 100
            elif "limit" in properties:
                args["limit"] = 100
            if "offset" in properties:
                args["offset"] = offset
            if cursor:
                cursor_key = "cursor" if "cursor" in properties else "page_token"
                args[cursor_key] = cursor
            elif "page" in properties:
                args["page"] = page
            elif "page_num" in properties:
                args["page_num"] = page
            payload = self.client.call_tool(str(tool["name"]), args)
            raw_records = _walk_named_lists(payload, {"records", "items", "data"})
            for record in raw_records:
                record_id = str(
                    record.get("record_id")
                    or record.get("recordId")
                    or record.get("id")
                    or ""
                )
                raw_values = (
                    record.get("values")
                    or record.get("fields")
                    or record.get("field_values")
                    or {}
                )
                values: dict[str, str] = {}
                if isinstance(raw_values, dict):
                    for key, value in raw_values.items():
                        title = id_to_title.get(str(key), str(key))
                        values[title] = _decode_field_value(value)
                elif isinstance(raw_values, list):
                    for field_value in raw_values:
                        if not isinstance(field_value, dict):
                            continue
                        raw_title = (
                            field_value.get("field")
                            or field_value.get("field_title")
                            or field_value.get("fieldTitle")
                            or field_value.get("field_id")
                            or field_value.get("fieldId")
                            or ""
                        )
                        title = id_to_title.get(str(raw_title), str(raw_title))
                        decoded_value = ""
                        for key, value in field_value.items():
                            if key in {
                                "field",
                                "field_title",
                                "fieldTitle",
                                "field_id",
                                "fieldId",
                            }:
                                continue
                            decoded_value = _decode_field_value(value)
                            if decoded_value:
                                break
                        if title:
                            values[title] = decoded_value
                if record_id:
                    rows.append(RemoteRecord(record_id, values))
            has_next = bool(
                _find_named_value(
                    payload, {"has_next", "hasnext", "has_more"}, False
                )
            )
            next_cursor = str(
                _find_named_value(
                    payload,
                    {"next_cursor", "nextcursor", "next_page_token"},
                    "",
                )
                or ""
            )
            next_offset = _find_named_value(payload, {"next"}, None)
            if not has_next or not raw_records:
                break
            if "offset" in properties and next_offset is not None:
                new_offset = int(next_offset)
                if new_offset <= offset:
                    break
                offset = new_offset
            elif next_cursor and next_cursor != cursor:
                cursor = next_cursor
            else:
                page += 1
        return tuple(rows)

    @staticmethod
    def _text_value(value: object) -> list[dict[str, str]]:
        return [{"text": str(value), "type": "text"}]

    def add_records(
        self,
        file_id: str,
        sheet_id: str,
        rows: Iterable[dict[str, str]],
    ) -> int:
        records = tuple(rows)
        if not records:
            return 0
        tool = self._tool("add_records")
        base = self._base_arguments(tool, file_id, sheet_id)
        added = 0
        for start in range(0, len(records), 100):
            chunk = records[start : start + 100]
            current_rows = [
                {
                    "field_values": [
                        {
                            "field": title,
                            "text_value": {"items": self._text_value(value)},
                        }
                        for title, value in row.items()
                    ]
                }
                for row in chunk
            ]
            rich_rows = [
                {
                    title: self._text_value(value)
                    for title, value in row.items()
                }
                for row in chunk
            ]
            variants = (
                current_rows,
                [{"values": row} for row in rich_rows],
                [{"fields": row} for row in rich_rows],
                rich_rows,
            )
            last_error: Exception | None = None
            for record_rows in variants:
                try:
                    self.client.call_tool(
                        str(tool["name"]), {**base, "records": record_rows}
                    )
                    last_error = None
                    break
                except TencentDocsError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            added += len(chunk)
        return added

    def delete_records(
        self, file_id: str, sheet_id: str, record_ids: Iterable[str]
    ) -> int:
        ids = tuple(dict.fromkeys(record_id for record_id in record_ids if record_id))
        if not ids:
            return 0
        tool = self._tool("delete_records", required=False)
        if tool is None:
            raise TencentDocsError(
                "腾讯文档当前没有提供 delete_records，无法执行最近5次清理"
            )
        base = self._base_arguments(tool, file_id, sheet_id)
        deleted = 0
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            variants = (
                {**base, "record_ids": list(chunk)},
                {
                    **base,
                    "records": [{"record_id": value} for value in chunk],
                },
                {**base, "ids": list(chunk)},
            )
            last_error: Exception | None = None
            for args in variants:
                try:
                    self.client.call_tool(str(tool["name"]), args)
                    last_error = None
                    break
                except TencentDocsError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            deleted += len(chunk)
        return deleted


def remote_record_ids_to_delete(
    records: Iterable[RemoteRecord], *, limit: int = 1000
) -> tuple[str, ...]:
    grouped: dict[tuple[str, str], list[RemoteRecord]] = {}
    duplicate_ids: list[str] = []
    seen_fingerprints: dict[str, RemoteRecord] = {}
    for record in records:
        fingerprint = _canonical_fingerprint(
            record.values.get("数据指纹", "")
        )
        job = record.values.get("游戏职业", "").strip()
        nest = record.values.get("对应巢穴", "").strip()
        if not _FINGERPRINT_RE.fullmatch(fingerprint) or not job or not nest:
            continue
        previous = seen_fingerprints.get(fingerprint)
        if previous is not None:
            previous_time = previous.values.get("记录时间", "")
            current_time = record.values.get("记录时间", "")
            if current_time >= previous_time:
                duplicate_ids.append(previous.record_id)
                seen_fingerprints[fingerprint] = record
            else:
                duplicate_ids.append(record.record_id)
            continue
        seen_fingerprints[fingerprint] = record
    for record in seen_fingerprints.values():
        key = (
            record.values.get("游戏职业", "").strip(),
            record.values.get("对应巢穴", "").strip(),
        )
        grouped.setdefault(key, []).append(record)
    remove = list(duplicate_ids)
    for rows in grouped.values():
        rows.sort(
            key=lambda row: (
                row.values.get("记录时间", ""),
                row.values.get("数据指纹", ""),
            ),
            reverse=True,
        )
        remove.extend(row.record_id for row in rows[max(1, int(limit)) :])
    return tuple(dict.fromkeys(remove))


class TeamScoreTencentSyncService:
    def __init__(
        self,
        token: str,
        file_id: str,
        sheet_id: str = "",
        *,
        endpoint: str = TENCENT_DOCS_MCP_ENDPOINT,
    ) -> None:
        self.file_id = file_id.strip()
        self.requested_sheet_id = sheet_id.strip()
        if not self.file_id:
            raise TencentDocsError("尚未填写腾讯智能表格链接或 file_id")
        self.gateway = TencentSmartSheetGateway(
            TencentDocsMcpClient(token, endpoint=endpoint)
        )

    def initialize_document(self) -> str:
        sheet_id = self.gateway.resolve_sheet_id(
            self.file_id, self.requested_sheet_id
        )
        self.gateway.ensure_fields(self.file_id, sheet_id)
        return sheet_id

    def sync_store(self, store: TeamScoreSyncStore) -> tuple[int, int, str]:
        sheet_id = self.initialize_document()
        fields = self.gateway.ensure_fields(self.file_id, sheet_id)
        remote = self.gateway.list_records(self.file_id, sheet_id, fields)
        remote_fingerprints = {
            _canonical_fingerprint(row.values.get("数据指纹", ""))
            for row in remote
        }
        desired = store.retained_rows(limit=1000)
        missing = [
            row.document_values()
            for row in desired
            if _canonical_fingerprint(row.fingerprint) not in remote_fingerprints
        ]
        added = self.gateway.add_records(self.file_id, sheet_id, missing)
        if added:
            remote = self.gateway.list_records(self.file_id, sheet_id, fields)
        delete_ids = remote_record_ids_to_delete(remote, limit=1000)
        deleted = self.gateway.delete_records(
            self.file_id, sheet_id, delete_ids
        )
        store.mark_all_synced()
        return added, deleted, sheet_id


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    try:
        input_blob, _buffer = _blob_from_bytes(raw)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        ):
            raise OSError(ctypes.get_last_error())
        try:
            encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    except (AttributeError, OSError):
        return "plain:" + base64.b64encode(raw).decode("ascii")


def unprotect_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if text.startswith("plain:"):
        return base64.b64decode(text[6:]).decode("utf-8")
    if not text.startswith("dpapi:"):
        return text
    encrypted = base64.b64decode(text[6:])
    try:
        input_blob, _buffer = _blob_from_bytes(encrypted)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        ):
            raise OSError(ctypes.get_last_error())
        try:
            raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
        return raw.decode("utf-8")
    except (AttributeError, OSError, ValueError):
        return ""
