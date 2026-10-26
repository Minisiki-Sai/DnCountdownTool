from __future__ import annotations

import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


# Both IDs are transport/session aliases.  They may be reused after a line
# switch, reconnect or character switch, so neither is a durable person key.
# Six hours covers a long raid session while keeping stale aliases out of the
# next session.  Conflicting names inside the window are rejected below.
RUNTIME_ALIAS_MAX_AGE_SECONDS = 6 * 60 * 60
PROFILE_ALIAS_MAX_AGE_SECONDS = 6 * 60 * 60
LOCAL_ALIAS_RETENTION_SECONDS = 48 * 60 * 60


@dataclass(frozen=True, slots=True)
class CachedPlayerIdentity:
    profile_id: int
    runtime_actor_id: int
    character_name: str
    job: str = ""
    source: str = "local"
    observed_at: float = 0.0


def default_player_identity_cache_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "player_identity_cache.sqlite3"
    )


class PersistentPlayerIdentityCache:
    """Retryable CloudBase outbox plus a bounded two-layer identity cache.

    Character names are the durable identity used by history/privacy.  Both
    profile and runtime IDs are recent aliases only.  Uploaded aliases remain
    locally for a bounded time so CloudBase outages do not make an already
    observed teammate anonymous.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or default_player_identity_cache_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=NORMAL")
        connection.execute("pragma busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                create table if not exists player_identity_profiles (
                    profile_id integer primary key check(profile_id > 0),
                    character_name text not null,
                    job text not null default '',
                    first_seen real not null,
                    last_seen real not null,
                    observation_count integer not null default 1
                );
                create table if not exists player_runtime_identity_aliases (
                    runtime_actor_id integer not null check(runtime_actor_id > 0),
                    profile_id integer not null check(profile_id > 0),
                    character_name text not null,
                    job text not null default '',
                    source text not null default 'packet',
                    first_seen real not null,
                    last_seen real not null,
                    observation_count integer not null default 1,
                    primary key(runtime_actor_id, profile_id)
                );
                create table if not exists player_identity_name_history (
                    profile_id integer not null check(profile_id > 0),
                    character_name text not null,
                    first_seen real not null,
                    last_seen real not null,
                    observation_count integer not null default 1,
                    primary key(profile_id, character_name)
                );
                create index if not exists player_runtime_identity_recent_idx
                    on player_runtime_identity_aliases(runtime_actor_id, last_seen desc);
                create table if not exists player_identity_cloud_outbox (
                    runtime_actor_id integer not null,
                    profile_id integer not null,
                    character_name text not null,
                    job text not null default '',
                    source text not null default 'packet',
                    observed_at real not null,
                    attempts integer not null default 0,
                    next_attempt_at real not null default 0,
                    last_error text not null default '',
                    primary key(runtime_actor_id, profile_id)
                );
                create table if not exists player_identity_cache_meta (
                    key text primary key,
                    value text not null
                );
                create table if not exists player_character_identities_v2 (
                    character_key text primary key,
                    character_name text not null,
                    job text not null default '',
                    first_seen real not null,
                    last_seen real not null,
                    observation_count integer not null default 1
                );
                create table if not exists player_identity_alias_history_v2 (
                    character_key text not null,
                    profile_id integer not null check(profile_id > 0),
                    runtime_actor_id integer not null default 0
                        check(runtime_actor_id >= 0),
                    character_name text not null,
                    job text not null default '',
                    source text not null default 'packet',
                    first_seen real not null,
                    last_seen real not null,
                    observation_count integer not null default 1,
                    primary key(character_key, profile_id, runtime_actor_id)
                );
                create index if not exists player_identity_alias_profile_v2_idx
                    on player_identity_alias_history_v2(profile_id, last_seen desc);
                create index if not exists player_identity_alias_runtime_v2_idx
                    on player_identity_alias_history_v2(runtime_actor_id, last_seen desc)
                    where runtime_actor_id > 0;
                create table if not exists player_identity_cloud_outbox_v2 (
                    character_key text not null,
                    runtime_actor_id integer not null,
                    profile_id integer not null,
                    character_name text not null,
                    job text not null default '',
                    source text not null default 'packet',
                    observed_at real not null,
                    attempts integer not null default 0,
                    next_attempt_at real not null default 0,
                    last_error text not null default '',
                    primary key(character_key, runtime_actor_id, profile_id)
                );
                """
            )
            # Import legacy observations once. They keep their timestamps, so
            # old profile/name guesses cannot become current merely because
            # the application was upgraded.
            connection.execute(
                """
                insert into player_character_identities_v2(
                    character_key, character_name, job, first_seen, last_seen,
                    observation_count
                )
                select lower(trim(character_name)), character_name, job,
                       min(first_seen), max(last_seen), sum(observation_count)
                  from player_identity_profiles
                 where trim(character_name) <> ''
                 group by lower(trim(character_name)), character_name, job
                on conflict(character_key) do update set
                    character_name=case
                        when excluded.last_seen >= player_character_identities_v2.last_seen
                        then excluded.character_name
                        else player_character_identities_v2.character_name end,
                    job=case when excluded.job <> '' then excluded.job
                             else player_character_identities_v2.job end,
                    first_seen=min(player_character_identities_v2.first_seen,
                                   excluded.first_seen),
                    last_seen=max(player_character_identities_v2.last_seen,
                                  excluded.last_seen)
                """
            )
            connection.execute(
                """
                insert into player_identity_alias_history_v2(
                    character_key, profile_id, runtime_actor_id,
                    character_name, job, source, first_seen, last_seen,
                    observation_count
                )
                select lower(trim(character_name)), profile_id, 0,
                       character_name, job, 'legacy_profile', first_seen,
                       last_seen, observation_count
                  from player_identity_profiles
                 where trim(character_name) <> ''
                on conflict(character_key, profile_id, runtime_actor_id)
                do nothing
                """
            )
            connection.execute(
                """
                insert into player_identity_cloud_outbox_v2(
                    character_key, runtime_actor_id, profile_id,
                    character_name, job, source, observed_at,
                    attempts, next_attempt_at, last_error
                )
                select lower(trim(character_name)), runtime_actor_id,
                       profile_id, character_name, job, source, observed_at,
                       attempts, next_attempt_at, last_error
                  from player_identity_cloud_outbox
                 where trim(character_name) <> ''
                on conflict(character_key, runtime_actor_id, profile_id)
                do update set
                    observed_at=max(player_identity_cloud_outbox_v2.observed_at,
                                    excluded.observed_at),
                    next_attempt_at=min(player_identity_cloud_outbox_v2.next_attempt_at,
                                        excluded.next_attempt_at)
                """
            )

    @staticmethod
    def _normalized(binding: CachedPlayerIdentity) -> CachedPlayerIdentity | None:
        try:
            profile_id = int(binding.profile_id)
            runtime_id = int(binding.runtime_actor_id)
        except (TypeError, ValueError):
            return None
        name = str(binding.character_name or "").strip()[:64]
        # A recent profile/name observation is useful by itself. The runtime
        # actor ID may legitimately be unknown until the character spawns.
        if profile_id <= 0 or runtime_id < 0 or not name:
            return None
        return CachedPlayerIdentity(
            profile_id=profile_id,
            runtime_actor_id=runtime_id,
            character_name=name,
            job=str(binding.job or "").strip()[:64],
            source=str(binding.source or "local").strip()[:32] or "local",
            observed_at=float(binding.observed_at or time.time()),
        )

    def observe(self, binding: CachedPlayerIdentity, *, queue_cloud: bool = True) -> bool:
        row = self._normalized(binding)
        if row is None:
            return False
        character_key = row.character_name.casefold()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                insert into player_character_identities_v2(
                    character_key, character_name, job, first_seen, last_seen,
                    observation_count
                ) values (?, ?, ?, ?, ?, 1)
                on conflict(character_key) do update set
                    character_name=case
                        when excluded.last_seen >= player_character_identities_v2.last_seen
                        then excluded.character_name
                        else player_character_identities_v2.character_name end,
                    job=case when excluded.job <> '' then excluded.job
                             else player_character_identities_v2.job end,
                    last_seen=max(player_character_identities_v2.last_seen,
                                  excluded.last_seen),
                    observation_count=player_character_identities_v2.observation_count + 1
                """,
                (character_key, row.character_name, row.job,
                 row.observed_at, row.observed_at),
            )
            connection.execute(
                """
                insert into player_identity_alias_history_v2(
                    character_key, profile_id, runtime_actor_id,
                    character_name, job, source, first_seen, last_seen,
                    observation_count
                ) values (?, ?, ?, ?, ?, ?, ?, ?, 1)
                on conflict(character_key, profile_id, runtime_actor_id)
                do update set
                    character_name=excluded.character_name,
                    job=case when excluded.job <> '' then excluded.job
                             else player_identity_alias_history_v2.job end,
                    source=excluded.source,
                    last_seen=max(player_identity_alias_history_v2.last_seen,
                                  excluded.last_seen),
                    observation_count=player_identity_alias_history_v2.observation_count + 1
                """,
                (character_key, row.profile_id, row.runtime_actor_id,
                 row.character_name, row.job, row.source,
                 row.observed_at, row.observed_at),
            )
            connection.execute(
                """
                insert into player_identity_profiles(
                    profile_id, character_name, job, first_seen, last_seen,
                    observation_count
                ) values (?, ?, ?, ?, ?, 1)
                on conflict(profile_id) do update set
                    character_name=case
                        when excluded.last_seen >= player_identity_profiles.last_seen
                        then excluded.character_name
                        else player_identity_profiles.character_name end,
                    job=case when excluded.job <> '' then excluded.job
                             else player_identity_profiles.job end,
                    last_seen=max(player_identity_profiles.last_seen, excluded.last_seen),
                    observation_count=player_identity_profiles.observation_count + 1
                """,
                (
                    row.profile_id, row.character_name, row.job,
                    row.observed_at, row.observed_at,
                ),
            )
            connection.execute(
                """
                insert into player_identity_name_history(
                    profile_id, character_name, first_seen, last_seen,
                    observation_count
                ) values (?, ?, ?, ?, 1)
                on conflict(profile_id, character_name) do update set
                    last_seen=max(player_identity_name_history.last_seen,
                                  excluded.last_seen),
                    observation_count=player_identity_name_history.observation_count + 1
                """,
                (
                    row.profile_id, row.character_name,
                    row.observed_at, row.observed_at,
                ),
            )
            if row.runtime_actor_id > 0:
                connection.execute(
                    """
                    insert into player_runtime_identity_aliases(
                        runtime_actor_id, profile_id, character_name, job, source,
                        first_seen, last_seen, observation_count
                    ) values (?, ?, ?, ?, ?, ?, ?, 1)
                    on conflict(runtime_actor_id, profile_id) do update set
                        character_name=case
                            when excluded.last_seen >= player_runtime_identity_aliases.last_seen
                            then excluded.character_name
                            else player_runtime_identity_aliases.character_name end,
                        job=case when excluded.job <> '' then excluded.job
                                 else player_runtime_identity_aliases.job end,
                        source=excluded.source,
                        last_seen=max(player_runtime_identity_aliases.last_seen, excluded.last_seen),
                        observation_count=player_runtime_identity_aliases.observation_count + 1
                    """,
                    (
                        row.runtime_actor_id, row.profile_id, row.character_name,
                        row.job, row.source, row.observed_at, row.observed_at,
                    ),
                )
            if queue_cloud:
                connection.execute(
                    """
                    insert into player_identity_cloud_outbox_v2(
                        character_key, runtime_actor_id, profile_id,
                        character_name, job,
                        source, observed_at, attempts, next_attempt_at, last_error
                    ) values (?, ?, ?, ?, ?, ?, ?, 0, 0, '')
                    on conflict(character_key, runtime_actor_id, profile_id)
                    do update set
                        character_name=excluded.character_name,
                        job=case when excluded.job <> '' then excluded.job
                                 else player_identity_cloud_outbox_v2.job end,
                        source=excluded.source,
                        observed_at=max(player_identity_cloud_outbox_v2.observed_at,
                                        excluded.observed_at),
                        next_attempt_at=0,
                        last_error=''
                    """,
                    (
                        character_key, row.runtime_actor_id, row.profile_id,
                        row.character_name, row.job, row.source, row.observed_at,
                    ),
                )
            cutoff = time.time() - LOCAL_ALIAS_RETENTION_SECONDS
            connection.execute(
                "delete from player_identity_alias_history_v2 where last_seen < ?",
                (cutoff,),
            )
        return True

    def observe_profile(
        self,
        profile_id: int,
        character_name: str,
        *,
        job: str = "",
        source: str = "profile_packet",
        observed_at: float = 0.0,
        queue_cloud: bool = True,
    ) -> bool:
        """Persist a recent profile/name alias without a runtime alias."""

        return self.observe(CachedPlayerIdentity(
            profile_id=int(profile_id or 0),
            runtime_actor_id=0,
            character_name=str(character_name or ""),
            job=str(job or ""),
            source=str(source or "profile_packet"),
            observed_at=float(observed_at or time.time()),
        ), queue_cloud=queue_cloud)

    def resolve_runtime(
        self,
        runtime_actor_id: int,
        *,
        job: str = "",
        max_age_seconds: float = RUNTIME_ALIAS_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> CachedPlayerIdentity | None:
        try:
            runtime_id = int(runtime_actor_id)
        except (TypeError, ValueError):
            return None
        if runtime_id <= 0:
            return None
        threshold = float(now or time.time()) - max(60.0, float(max_age_seconds))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                select runtime_actor_id, profile_id, character_key,
                       character_name, job, source, last_seen
                  from player_identity_alias_history_v2
                 where runtime_actor_id = ? and last_seen >= ?
                 order by last_seen desc
                """,
                (runtime_id, threshold),
            ).fetchall()
        character_keys = {str(row["character_key"] or "") for row in rows}
        if len(character_keys) != 1:
            return None
        wanted_job = str(job or "").strip()
        compatible = [
            row for row in rows
            if not wanted_job or not str(row["job"] or "").strip()
            or str(row["job"] or "").strip() == wanted_job
        ]
        if not compatible:
            return None
        row = compatible[0]
        return CachedPlayerIdentity(
            profile_id=int(row["profile_id"]),
            runtime_actor_id=runtime_id,
            character_name=str(row["character_name"] or "").strip(),
            job=str(row["job"] or "").strip(),
            source="local_cache",
            observed_at=float(row["last_seen"] or 0.0),
        )

    def resolve_profile(
        self,
        profile_id: int,
        *,
        job: str = "",
        max_age_seconds: float = PROFILE_ALIAS_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> CachedPlayerIdentity | None:
        """Resolve a recent, unambiguous profile alias.

        Profile IDs are not permanent character IDs. If the same value points
        at multiple character names inside the active window, returning no
        match is safer than leaking another character's name/history.
        """

        try:
            parsed_profile_id = int(profile_id)
        except (TypeError, ValueError):
            return None
        if parsed_profile_id <= 0:
            return None
        threshold = float(now or time.time()) - max(60.0, float(max_age_seconds))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                select profile_id, runtime_actor_id, character_key,
                       character_name, job, source, last_seen
                  from player_identity_alias_history_v2
                 where profile_id = ? and last_seen >= ?
                 order by last_seen desc
                """,
                (parsed_profile_id, threshold),
            ).fetchall()
        if len({str(row["character_key"] or "") for row in rows}) != 1:
            return None
        wanted_job = str(job or "").strip()
        compatible = [
            row for row in rows
            if not wanted_job or not str(row["job"] or "").strip()
            or str(row["job"] or "").strip() == wanted_job
        ]
        if not compatible:
            return None
        row = compatible[0]
        name = str(row["character_name"] or "").strip()
        if not name:
            return None
        return CachedPlayerIdentity(
            profile_id=parsed_profile_id,
            runtime_actor_id=0,
            character_name=name,
            job=str(row["job"] or "").strip(),
                source="local_recent_profile_alias",
            observed_at=float(row["last_seen"] or 0.0),
        )

    def import_legacy_trace_bindings(
        self,
        directory: Path,
        *,
        limit: int = 60,
    ) -> int:
        """Seed the durable cache from packet-proven legacy trace appendices.

        Releases before the SQLite cache already stored the verified name map
        in adapter 254. Real v576 spawn records expose a profile alias and its
        scene/runtime ID as one consecutive pair. Import only that exact
        same-name pair, keep it local, and never upload inferred legacy rows.
        """

        marker = "legacy_trace_identity_import_v1"
        with self._lock, self._connect() as connection:
            done = connection.execute(
                "select value from player_identity_cache_meta where key=?",
                (marker,),
            ).fetchone()
        if done is not None:
            return 0
        root = Path(directory)
        candidates: list[Path] = []
        try:
            candidates = sorted(
                (path for path in root.glob("*.dpscap") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[: max(1, int(limit))]
        except OSError:
            candidates = []

        from .npcap import (
            IDENTITY_METADATA_ADAPTER_INDEX,
            decode_identity_trace_bindings,
        )

        imported = 0
        header = struct.Struct("<dBII")
        for path in candidates:
            names: dict[int, str] = {}
            try:
                with path.open("rb") as stream:
                    if stream.read(6) != b"DNDP1\x00":
                        continue
                    while True:
                        raw_header = stream.read(header.size)
                        if not raw_header:
                            break
                        if len(raw_header) != header.size:
                            break
                        _timestamp, adapter_index, _original, captured = header.unpack(raw_header)
                        if captured < 0 or captured > 128 * 1024 * 1024:
                            break
                        if adapter_index == IDENTITY_METADATA_ADAPTER_INDEX:
                            payload = stream.read(captured)
                            if len(payload) != captured:
                                break
                            names.update(decode_identity_trace_bindings(payload))
                        else:
                            stream.seek(captured, 1)
            except OSError:
                continue
            for profile_id, name in sorted(names.items()):
                runtime_id = int(profile_id) + 1
                if names.get(runtime_id) != name:
                    continue
                if self.resolve_profile(profile_id) is not None:
                    continue
                observed_at = path.stat().st_mtime
                if self.observe(CachedPlayerIdentity(
                    profile_id=int(profile_id),
                    runtime_actor_id=runtime_id,
                    character_name=str(name or "").strip(),
                    source="legacy_trace",
                    observed_at=observed_at,
                ), queue_cloud=False):
                    imported += 1
        with self._lock, self._connect() as connection:
            connection.execute(
                "insert or replace into player_identity_cache_meta(key, value) values (?, ?)",
                (marker, str(imported)),
            )
        return imported

    def resolve_runtimes(
        self,
        runtime_jobs: Iterable[tuple[int, str]],
        *,
        max_age_seconds: float = RUNTIME_ALIAS_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> dict[int, CachedPlayerIdentity]:
        requested: dict[int, str] = {}
        for runtime_id, job in runtime_jobs:
            try:
                parsed = int(runtime_id)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                requested[parsed] = str(job or "").strip()
        if not requested:
            return {}
        threshold = float(now or time.time()) - max(60.0, float(max_age_seconds))
        placeholders = ",".join("?" for _value in requested)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                select runtime_actor_id, profile_id, character_key,
                       character_name, job, source, last_seen
                  from player_identity_alias_history_v2
                 where runtime_actor_id in ({placeholders}) and last_seen >= ?
                 order by runtime_actor_id, last_seen desc
                """,
                (*requested, threshold),
            ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["runtime_actor_id"]), []).append(row)
        resolved: dict[int, CachedPlayerIdentity] = {}
        for runtime_id, candidates in grouped.items():
            if len({str(row["character_key"] or "") for row in candidates}) != 1:
                continue
            wanted_job = requested.get(runtime_id, "")
            compatible = [
                row for row in candidates
                if not wanted_job or not str(row["job"] or "").strip()
                or str(row["job"] or "").strip() == wanted_job
            ]
            if not compatible:
                continue
            row = compatible[0]
            resolved[runtime_id] = CachedPlayerIdentity(
                profile_id=int(row["profile_id"]),
                runtime_actor_id=runtime_id,
                character_name=str(row["character_name"] or "").strip(),
                job=str(row["job"] or "").strip(),
                source="local_cache",
                observed_at=float(row["last_seen"] or 0.0),
            )
        return resolved

    def pending_cloud_rows(self, limit: int = 100) -> tuple[CachedPlayerIdentity, ...]:
        now = time.time()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                select runtime_actor_id, profile_id, character_name, job,
                       source, observed_at
                  from player_identity_cloud_outbox_v2
                 where next_attempt_at <= ?
                 order by observed_at asc
                 limit ?
                """,
                (now, max(1, min(500, int(limit)))),
            ).fetchall()
        return tuple(
            CachedPlayerIdentity(
                profile_id=int(row["profile_id"]),
                runtime_actor_id=int(row["runtime_actor_id"]),
                character_name=str(row["character_name"] or ""),
                job=str(row["job"] or ""),
                source=str(row["source"] or "local"),
                observed_at=float(row["observed_at"] or now),
            )
            for row in rows
        )

    def mark_cloud_uploaded(self, rows: Iterable[CachedPlayerIdentity]) -> None:
        keys = [
            (str(row.character_name or "").strip().casefold(),
             int(row.runtime_actor_id), int(row.profile_id))
            for row in rows
        ]
        if not keys:
            return
        with self._lock, self._connect() as connection:
            connection.executemany(
                """delete from player_identity_cloud_outbox_v2
                    where character_key=? and runtime_actor_id=? and profile_id=?""",
                keys,
            )
            connection.executemany(
                """delete from player_identity_cloud_outbox
                    where runtime_actor_id=? and profile_id=?""",
                [(runtime_id, profile_id)
                 for _character_key, runtime_id, profile_id in keys],
            )
            # Do not delete the recent local alias after upload. This is the
            # offline half of the two-layer cache and expires independently.

    def mark_cloud_failed(self, rows: Iterable[CachedPlayerIdentity], error: str) -> None:
        keys = [
            (str(row.character_name or "").strip().casefold(),
             int(row.runtime_actor_id), int(row.profile_id))
            for row in rows
        ]
        if not keys:
            return
        with self._lock, self._connect() as connection:
            for character_key, runtime_id, profile_id in keys:
                attempt_row = connection.execute(
                    """select attempts from player_identity_cloud_outbox_v2
                        where character_key=? and runtime_actor_id=? and profile_id=?""",
                    (character_key, runtime_id, profile_id),
                ).fetchone()
                attempts = int(attempt_row[0] if attempt_row else 0) + 1
                delay = min(30 * 60, 15 * (2 ** min(7, attempts - 1)))
                connection.execute(
                    """
                    update player_identity_cloud_outbox_v2
                       set attempts=?, next_attempt_at=?, last_error=?
                     where character_key=? and runtime_actor_id=? and profile_id=?
                    """,
                    (
                        attempts, time.time() + delay, str(error or "")[:500],
                        character_key, runtime_id, profile_id,
                    ),
                )

    def status(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            profiles = int(connection.execute(
                "select count(*) from player_character_identities_v2"
            ).fetchone()[0])
            aliases = int(connection.execute(
                "select count(*) from player_identity_alias_history_v2"
            ).fetchone()[0])
            pending = int(connection.execute(
                "select count(*) from player_identity_cloud_outbox_v2"
            ).fetchone()[0])
            last_error_row = connection.execute(
                """
                select last_error from player_identity_cloud_outbox_v2
                 where last_error <> '' order by next_attempt_at desc limit 1
                """
            ).fetchone()
        return {
            "profiles": profiles,
            "runtimeAliases": aliases,
            "cloudPending": pending,
            "cloudLastError": str(last_error_row[0]) if last_error_row else "",
            "path": str(self.path),
        }


class PlayerIdentityCloudBridge:
    """Low-priority, non-blocking CloudBase lookup/outbox worker."""

    def __init__(
        self,
        cache: PersistentPlayerIdentityCache,
        on_bindings: Callable[[tuple[CachedPlayerIdentity, ...]], None],
    ) -> None:
        self.cache = cache
        self._on_bindings = on_bindings
        self._lock = threading.RLock()
        self._pending_lookup: dict[int, str] = {}
        self._pending_profile_lookup: set[int] = set()
        self._recent_lookup: dict[int, float] = {}
        self._recent_profile_lookup: dict[int, float] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._state = "idle"
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="PlayerIdentityCloudCache",
            daemon=True,
        )
        self._thread.start()
        # Flush both new observations and legacy compact-cache migration rows
        # immediately instead of waiting for the first 30-second timer tick.
        self._wake.set()

    def request_lookup(self, rows: Iterable[tuple[int, str]]) -> None:
        now = time.monotonic()
        with self._lock:
            for runtime_id, job in rows:
                runtime_id = int(runtime_id or 0)
                if runtime_id <= 0 or now - self._recent_lookup.get(runtime_id, 0.0) < 60.0:
                    continue
                self._pending_lookup[runtime_id] = str(job or "").strip()[:64]
                self._recent_lookup[runtime_id] = now
        self._wake.set()

    def request_profile_lookup(self, profile_id: int) -> None:
        try:
            parsed = int(profile_id)
        except (TypeError, ValueError):
            return
        if parsed <= 0:
            return
        now = time.monotonic()
        with self._lock:
            # One lookup is enough for a burst of duplicate decoder events,
            # but a line switch/reconnect must be allowed to retry quickly if
            # the first request raced an upload or a transient network error.
            if (
                parsed in self._pending_profile_lookup
                or now - self._recent_profile_lookup.get(parsed, 0.0) < 5.0
            ):
                return
            self._pending_profile_lookup.add(parsed)
            self._recent_profile_lookup[parsed] = now
        self._wake.set()

    def notify_outbox(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state,
                "lastError": self._last_error,
                "pendingLookups": len(self._pending_lookup) + len(self._pending_profile_lookup),
            }

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _service(self):
        from .supabase_score_sync import TeamScoreSupabaseSyncService
        from .score_sync import load_v410_score_sync_config

        config = load_v410_score_sync_config()
        if not config.ready:
            return None
        return TeamScoreSupabaseSyncService(
            config.endpoint,
            config.upload_token,
            config.installation_id,
            timeout_seconds=4.0,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(30.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                lookups = tuple(self._pending_lookup.items())[:128]
                for runtime_id, _job in lookups:
                    self._pending_lookup.pop(runtime_id, None)
                profile_lookups = tuple(self._pending_profile_lookup)[:128]
                for profile_id in profile_lookups:
                    self._pending_profile_lookup.discard(profile_id)
                self._state = "connecting"
            outbox: tuple[CachedPlayerIdentity, ...] = ()
            try:
                service = self._service()
                if service is None:
                    with self._lock:
                        self._state = "offline"
                        self._last_error = "评分同步服务未配置，本地缓存继续生效"
                    continue
                outbox = self.cache.pending_cloud_rows(100)
                if outbox:
                    accepted = service.publish_player_identity_bindings(outbox)
                    if accepted < len(outbox):
                        raise RuntimeError(
                            f"CloudBase 仅确认 {accepted}/{len(outbox)} 条身份记录"
                        )
                    self.cache.mark_cloud_uploaded(outbox)
                if lookups:
                    found = service.fetch_player_identity_bindings(
                        (runtime_id for runtime_id, _job in lookups),
                        jobs={runtime_id: job for runtime_id, job in lookups},
                    )
                    accepted: list[CachedPlayerIdentity] = []
                    for row in found:
                        binding = self.cache._normalized(row)
                        if binding is None:
                            continue
                        accepted.append(binding)
                    if accepted:
                        self._on_bindings(tuple(accepted))
                if profile_lookups:
                    found = service.fetch_player_identity_profiles(profile_lookups)
                    accepted_profiles: list[CachedPlayerIdentity] = []
                    for row in found:
                        binding = self.cache._normalized(row)
                        if binding is None:
                            continue
                        accepted_profiles.append(binding)
                    if accepted_profiles:
                        self._on_bindings(tuple(accepted_profiles))
                with self._lock:
                    self._state = "ready"
                    self._last_error = ""
                # Drain a large legacy cache in bounded batches without adding
                # a fixed 30-second pause between successful uploads.
                if self.cache.pending_cloud_rows(1):
                    self._wake.set()
            except Exception as exc:
                if outbox:
                    self.cache.mark_cloud_failed(outbox, str(exc))
                with self._lock:
                    # Lookups are read-only and may have been lost solely
                    # because CloudBase was temporarily unavailable. Keep
                    # them pending for the worker's next bounded retry rather
                    # than requiring the user to inspect the character again.
                    for runtime_id, job in lookups:
                        self._pending_lookup.setdefault(runtime_id, job)
                    self._pending_profile_lookup.update(profile_lookups)
                    self._state = "offline"
                    self._last_error = str(exc)[:500]
