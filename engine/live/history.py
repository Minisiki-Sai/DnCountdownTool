from __future__ import annotations

import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .event_platform import event_platform_for
from .settlement import SettlementEvent

from .models import (
    BossActionEvent,
    BossSpawnEvent,
    MonsterAppearanceEvent,
    BossStatusEvent,
    CombatEvent,
    CombatSnapshot,
    PartyMemberEvent,
    PlayerHealthEvent,
    PlayerLifecycleEvent,
    PlayerInspectEvent,
    SkillCastEvent,
    SkillParameterEvent,
    SummonRemovalEvent,
)


LiveDpsEvent = (
    CombatEvent
    | SettlementEvent
    | BossStatusEvent
    | BossSpawnEvent
    | MonsterAppearanceEvent
    | BossActionEvent
    | SkillCastEvent
    | SkillParameterEvent
    | PartyMemberEvent
    | PlayerHealthEvent
    | PlayerLifecycleEvent
    | PlayerInspectEvent
    | SummonRemovalEvent
)


HISTORY_SCHEMA_VERSION = 2
HISTORY_WAL_LIMIT_BYTES = 32 * 1024 * 1024


def initialize_history_schema(
    connection: sqlite3.Connection,
    *,
    compact_database: bool,
) -> None:
    """Create the compact history schema without indexing raw event rows.

    ``events`` remains as an empty compatibility table so older diagnostics do
    not fail on a missing relation.  Current builds persist only summaries and
    compact actor evidence; the ``.dpscap`` file is the authoritative event
    archive.
    """

    if compact_database:
        # This pragma must be selected before the first user table is created.
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            elapsed_seconds REAL NOT NULL DEFAULT 0,
            total_damage INTEGER NOT NULL DEFAULT 0,
            total_healing INTEGER NOT NULL DEFAULT 0,
            player_count INTEGER NOT NULL DEFAULT 0,
            event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            source_id TEXT,
            source_name TEXT,
            job TEXT,
            skill_id TEXT,
            skill_name TEXT,
            amount INTEGER NOT NULL DEFAULT 0,
            critical INTEGER NOT NULL DEFAULT 0,
            healing INTEGER NOT NULL DEFAULT 0,
            target_id TEXT,
            attribution TEXT,
            payload_json TEXT,
            PRIMARY KEY (session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS player_summaries (
            session_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_name TEXT,
            job TEXT,
            damage INTEGER NOT NULL,
            healing INTEGER NOT NULL,
            hits INTEGER NOT NULL,
            critical_hits INTEGER NOT NULL,
            active_seconds REAL NOT NULL,
            PRIMARY KEY (session_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS skill_summaries (
            session_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            skill_name TEXT,
            damage INTEGER NOT NULL,
            healing INTEGER NOT NULL,
            hits INTEGER NOT NULL,
            critical_hits INTEGER NOT NULL,
            PRIMARY KEY (session_id, source_id, skill_id)
        );
        CREATE TABLE IF NOT EXISTS actor_job_observations (
            actor_id TEXT NOT NULL,
            job TEXT NOT NULL,
            observations INTEGER NOT NULL DEFAULT 0,
            last_name TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (actor_id, job)
        );
        CREATE INDEX IF NOT EXISTS idx_actor_job_observations_lookup
            ON actor_job_observations(actor_id, observations DESC);
        CREATE TABLE IF NOT EXISTS actor_identities (
            actor_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            job TEXT NOT NULL DEFAULT '',
            level INTEGER NOT NULL DEFAULT 0,
            party_position INTEGER NOT NULL DEFAULT 0,
            source_layout TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            last_timestamp_ms INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL
        );
        """
    )
    if compact_database:
        connection.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
    connection.commit()


def default_history_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "live_history.sqlite3"
    )


class LiveDpsHistoryStore:
    """Bounded local index for sessions, summaries and actor evidence.

    Raw packet-derived events are already retained in ``.dpscap``.  Duplicating
    millions of combat/status rows here made this ancillary database grow by
    gigabytes and slowed capture shutdown.  The compact store intentionally
    records only data consumed without replaying the capture.
    """

    FLUSH_INTERVAL_SECONDS = 3.0
    FLUSH_EVENT_COUNT = 1000

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_history_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_database = not self.path.exists() or self.path.stat().st_size == 0
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(
            f"PRAGMA journal_size_limit={HISTORY_WAL_LIMIT_BYTES}"
        )
        initialize_history_schema(
            self._connection,
            compact_database=new_database,
        )
        if int(self._connection.execute("PRAGMA user_version").fetchone()[0]) >= HISTORY_SCHEMA_VERSION:
            self._discard_abandoned_sessions()
        self._session_id = ""
        self._source_kind = ""
        self._sequence = 0
        self._pending_event_count = 0
        self._pending_actor_jobs: Counter[tuple[str, str, str]] = Counter()
        self._pending_identities: dict[str, tuple[object, ...]] = {}
        self._last_flush_at = time.monotonic()

    @property
    def session_id(self) -> str:
        return self._session_id

    def _discard_abandoned_sessions(self) -> None:
        cutoff = (
            datetime.now().astimezone() - timedelta(hours=24)
        ).isoformat(timespec="seconds")
        self._connection.execute(
            "DELETE FROM sessions WHERE ended_at IS NULL AND started_at < ?",
            (cutoff,),
        )
        self._connection.execute(
            "DELETE FROM player_summaries WHERE session_id NOT IN "
            "(SELECT session_id FROM sessions)"
        )
        self._connection.execute(
            "DELETE FROM skill_summaries WHERE session_id NOT IN "
            "(SELECT session_id FROM sessions)"
        )
        self._connection.commit()

    def start_session(
        self, source_kind: str, *, session_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        if self._session_id:
            raise RuntimeError("A live DPS history session is already active")
        self._session_id = session_id or uuid.uuid4().hex
        self._source_kind = str(source_kind or "unknown")
        self._sequence = 0
        self._pending_event_count = 0
        self._pending_actor_jobs.clear()
        self._pending_identities.clear()
        self._connection.execute(
            "INSERT INTO sessions(session_id, source_kind, started_at) VALUES (?, ?, ?)",
            (
                self._session_id,
                self._source_kind,
                started_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        self._connection.commit()
        self._last_flush_at = time.monotonic()
        return self._session_id

    def append(self, events: Iterable[LiveDpsEvent]) -> None:
        if not self._session_id:
            return
        for event in events:
            self._sequence += 1
            self._pending_event_count += 1
            observation = self._actor_observation(event)
            if observation is not None:
                actor_id, job, name = observation
                self._pending_actor_jobs[(actor_id, job, name)] += 1
            identity = self._identity_observation(event)
            if identity is not None:
                actor_id = str(identity[0])
                previous = self._pending_identities.get(actor_id)
                if previous is None or float(identity[6]) >= float(previous[6]):
                    self._pending_identities[actor_id] = identity
        if (
            self._pending_event_count >= self.FLUSH_EVENT_COUNT
            or time.monotonic() - self._last_flush_at >= self.FLUSH_INTERVAL_SECONDS
        ):
            self.flush()

    @staticmethod
    def _safe_identity_name(value: object) -> str:
        name = str(value or "").strip()
        lowered = name.lower().replace(" ", "")
        if lowered.startswith("角色0x") or lowered.startswith("0x"):
            return ""
        return name

    @classmethod
    def _actor_observation(
        cls, event: LiveDpsEvent
    ) -> tuple[str, str, str] | None:
        if isinstance(event, PartyMemberEvent):
            actor_id, job, name = event.actor_id, event.job, event.name
        elif isinstance(event, PlayerInspectEvent):
            actor_id, job, name = event.target_id, event.job, event.target_name
        elif isinstance(event, (PlayerHealthEvent, PlayerLifecycleEvent)):
            actor_id = event.target_id
            job = event.target_job
            name = event.target_name
        elif isinstance(event, SummonRemovalEvent):
            actor_id = event.owner_id
            job = event.owner_job
            name = event.owner_name
        elif isinstance(event, BossSpawnEvent):
            return None
        else:
            actor_id = getattr(event, "source_id", "")
            job = getattr(event, "job", "")
            name = getattr(event, "source_name", "")
        actor_id = str(actor_id or "").strip()
        job = str(job or "").strip()
        if not actor_id or not job:
            return None
        return actor_id, job, cls._safe_identity_name(name)

    @classmethod
    def _identity_observation(
        cls, event: LiveDpsEvent
    ) -> tuple[object, ...] | None:
        if isinstance(event, PartyMemberEvent):
            actor_id = str(event.actor_id or "").strip()
            if not actor_id:
                return None
            return (
                actor_id,
                cls._safe_identity_name(event.name),
                str(event.job or "").strip(),
                max(0, int(event.level)),
                max(0, int(event.party_position)),
                str(event.source_layout or "").strip(),
                max(0.0, float(event.confidence)),
                max(0, int(event.timestamp_ms)),
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        if isinstance(event, PlayerInspectEvent):
            actor_id = str(event.target_id or "").strip()
            if not actor_id:
                return None
            return (
                actor_id,
                cls._safe_identity_name(event.target_name),
                str(event.job or "").strip(),
                max(0, int(event.level)),
                max(0, int(event.party_position)),
                str(event.response_layout or "").strip(),
                0.75 if event.job or event.target_name else 0.25,
                max(0, int(event.timestamp_ms)),
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        return None

    def flush(self) -> None:
        if not self._pending_event_count:
            return
        observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._connection.executemany(
            """
            INSERT INTO actor_job_observations(
                actor_id, job, observations, last_name,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_id, job) DO UPDATE SET
                observations = actor_job_observations.observations
                    + excluded.observations,
                last_name = CASE
                    WHEN excluded.last_name <> '' THEN excluded.last_name
                    ELSE actor_job_observations.last_name
                END,
                last_seen_at = excluded.last_seen_at
            """,
            (
                (actor_id, job, count, name, observed_at, observed_at)
                for (actor_id, job, name), count
                in self._pending_actor_jobs.items()
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO actor_identities(
                actor_id, name, job, level, party_position, source_layout,
                confidence, last_timestamp_ms, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET
                name = CASE
                    WHEN excluded.name <> ''
                         AND (actor_identities.name = ''
                              OR excluded.confidence >= actor_identities.confidence)
                    THEN excluded.name ELSE actor_identities.name END,
                job = CASE
                    WHEN excluded.job <> ''
                         AND (actor_identities.job = ''
                              OR excluded.confidence >= actor_identities.confidence)
                    THEN excluded.job ELSE actor_identities.job END,
                level = MAX(actor_identities.level, excluded.level),
                party_position = CASE
                    WHEN excluded.party_position > 0
                    THEN excluded.party_position
                    ELSE actor_identities.party_position END,
                source_layout = CASE
                    WHEN excluded.source_layout <> ''
                    THEN excluded.source_layout
                    ELSE actor_identities.source_layout END,
                confidence = MAX(actor_identities.confidence, excluded.confidence),
                last_timestamp_ms = MAX(
                    actor_identities.last_timestamp_ms,
                    excluded.last_timestamp_ms
                ),
                last_seen_at = excluded.last_seen_at
            """,
            self._pending_identities.values(),
        )
        self._connection.commit()
        self._pending_event_count = 0
        self._pending_actor_jobs.clear()
        self._pending_identities.clear()
        self._last_flush_at = time.monotonic()

    def finish_session(self, snapshot: CombatSnapshot | None = None) -> None:
        if not self._session_id:
            return
        self.flush()
        if snapshot is None:
            elapsed_seconds = total_damage = total_healing = player_count = 0
            players = ()
        else:
            platform = event_platform_for(snapshot)
            elapsed_seconds = platform.elapsed_seconds
            total_damage = platform.total_damage
            total_healing = platform.total_healing
            player_count = len(platform.players)
            players = platform.players
        self._connection.execute(
            """
            UPDATE sessions
            SET ended_at = ?, elapsed_seconds = ?, total_damage = ?,
                total_healing = ?, player_count = ?, event_count = ?
            WHERE session_id = ?
            """,
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                elapsed_seconds,
                total_damage,
                total_healing,
                player_count,
                self._sequence,
                self._session_id,
            ),
        )
        for player in players:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO player_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._session_id,
                    player.source_id,
                    player.name,
                    player.job,
                    player.damage,
                    player.healing,
                    player.hits,
                    player.critical_hits,
                    player.active_seconds,
                ),
            )
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO skill_summaries
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        self._session_id,
                        player.source_id,
                        skill.skill_id,
                        skill.name,
                        skill.damage,
                        skill.healing,
                        skill.hits,
                        skill.critical_hits,
                    )
                    for skill in player.skills
                ),
            )
        self._connection.commit()
        self._session_id = ""
        self._source_kind = ""
        self._sequence = 0
        try:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("PRAGMA incremental_vacuum(256)")
        except sqlite3.Error:
            pass

    def close(self, snapshot: CombatSnapshot | None = None) -> None:
        try:
            self.finish_session(snapshot)
        finally:
            self._connection.close()
