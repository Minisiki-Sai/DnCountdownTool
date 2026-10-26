from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import shutil
import sqlite3
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .action_parser import action_parser_mode
from .aggregator import CombatAggregator
from .team_score_sync import capture_content_hash
from .version_fingerprint import replay_decoder_fingerprint


REPLAY_CACHE_SCHEMA = "dragon-nest-replay-cache-v2"
REPLAY_CACHE_KEY_SCHEMA = "dragon-nest-replay-cache-v1"
REPLAY_CACHE_DIRECTORY_SCHEMA = "dragon-nest-decoded-replay-directory-v1"
_REPLAY_CACHE_MAGIC = b"DNRC2\x00"
_REPLAY_CACHE_HEADER = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class ReplayCacheIdentity:
    key: str
    capture_hash: str
    decoder_fingerprint: str
    # Location metadata does not participate in cache validity. Two byte-for-
    # byte identical copies still share the same decoding key, but each copy
    # receives its own lifecycle-bound directory so deleting one source can
    # remove only its own generated files.
    source_path: str = field(default="", compare=False)
    source_size: int = field(default=0, compare=False)
    source_mtime_ns: int = field(default=0, compare=False)


def default_replay_cache_directory() -> Path:
    return Path.home() / "Documents" / "DragonNestDpsCapture" / "replay_cache"


def replay_cache_identity(
    capture_path: Path,
    data_root: Path,
    known_players: dict[str, dict[str, str]] | None = None,
    *,
    parser_mode: str | None = None,
) -> ReplayCacheIdentity:
    return replay_cache_identity_from_hash(
        capture_content_hash(Path(capture_path)),
        data_root,
        known_players,
        parser_mode=parser_mode,
        capture_path=Path(capture_path),
    )


def replay_cache_identity_from_hash(
    capture_hash: str,
    data_root: Path,
    known_players: dict[str, dict[str, str]] | None = None,
    *,
    parser_mode: str | None = None,
    capture_path: Path | None = None,
) -> ReplayCacheIdentity:
    """Build a replay-cache key from an already verified capture digest.

    Live capture computes the SHA-256 while bytes are written.  Reusing that
    digest lets the post-capture cache persist the decoded aggregator without
    rereading a potentially very large ``.dpscap`` merely to name the cache.
    """

    capture_hash = str(capture_hash or "").strip().casefold()
    if len(capture_hash) != 64 or any(
        character not in "0123456789abcdef" for character in capture_hash
    ):
        raise ValueError("invalid capture SHA-256")
    decoder_fingerprint = replay_decoder_fingerprint(str(Path(data_root).resolve()))
    # Keep the key namespace stable so a valid v1 cache can still be read and
    # transparently replaced by the integrity-checked v2 container.
    digest = hashlib.sha256(REPLAY_CACHE_KEY_SCHEMA.encode("ascii"))
    digest.update(capture_hash.encode("ascii"))
    digest.update(decoder_fingerprint.encode("ascii"))
    digest.update((parser_mode or action_parser_mode()).encode("utf-8"))
    digest.update(pickle.dumps(known_players or {}, protocol=5))
    source_path = ""
    source_size = 0
    source_mtime_ns = 0
    if capture_path is not None:
        source = Path(capture_path).expanduser().resolve()
        source_path = str(source)
        try:
            source_stat = source.stat()
            source_size = int(source_stat.st_size)
            source_mtime_ns = int(source_stat.st_mtime_ns)
        except OSError:
            pass
    return ReplayCacheIdentity(
        key=digest.hexdigest(),
        capture_hash=capture_hash,
        decoder_fingerprint=decoder_fingerprint,
        source_path=source_path,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
    )


class ReplayDiskCache:
    """Versioned local cache for a completely aggregated replay."""

    _async_lock = threading.RLock()
    _async_io_lock = threading.Lock()
    _async_pending: set[tuple[str, str]] = set()
    _monitor_lock = threading.Lock()
    _monitored_directories: set[str] = set()

    def __init__(self, directory: Path | None = None, max_entries: int = 64) -> None:
        use_default_directory = directory is None
        self.directory = Path(directory or default_replay_cache_directory())
        self.max_entries = max(1, int(max_entries))
        if use_default_directory:
            self.start_orphan_monitor()

    @staticmethod
    def _safe_capture_stem(value: str) -> str:
        stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value).strip("._-")
        return (stem or "capture")[:72]

    def _capture_directory(self, identity: ReplayCacheIdentity) -> Path:
        if identity.source_path:
            source = Path(identity.source_path)
            path_digest = hashlib.sha256(
                os.path.normcase(identity.source_path).encode("utf-8")
            ).hexdigest()[:8]
            stem = self._safe_capture_stem(source.stem)
        else:
            path_digest = "no-source"
            stem = "capture"
        return self.directory / (
            f"{stem}--{identity.capture_hash[:12]}--{path_digest}"
        )

    def _path(self, identity: ReplayCacheIdentity) -> Path:
        return self._capture_directory(identity) / f"{identity.key}.replaycache"

    def _legacy_path(self, identity: ReplayCacheIdentity) -> Path:
        return self.directory / f"{identity.key}.replaycache"

    def _manifest_path(self, identity: ReplayCacheIdentity) -> Path:
        return self._capture_directory(identity) / "manifest.json"

    def _write_manifest(self, identity: ReplayCacheIdentity) -> None:
        directory = self._capture_directory(identity)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": REPLAY_CACHE_DIRECTORY_SCHEMA,
            "sourcePath": identity.source_path,
            "sourceName": (
                Path(identity.source_path).name if identity.source_path else ""
            ),
            "sourceSize": int(identity.source_size),
            "sourceMtimeNs": int(identity.source_mtime_ns),
            "captureHash": identity.capture_hash,
            "updatedAtEpochMs": round(time.time() * 1000),
        }
        destination = directory / "manifest.json"
        temporary = directory / (
            f".manifest.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _safe_remove_cache_directory(self, candidate: Path) -> bool:
        try:
            root = self.directory.expanduser().resolve()
            resolved = Path(candidate).expanduser().resolve()
            if resolved.parent != root or resolved == root or not resolved.is_dir():
                return False
            shutil.rmtree(resolved)
            return True
        except OSError:
            return False

    def _forget_hash_index_source(self, source_path: Path) -> None:
        index_path = self.directory / "capture_hash_index.sqlite3"
        if not index_path.is_file():
            return
        try:
            resolved = str(Path(source_path).expanduser().resolve())
            with sqlite3.connect(index_path, timeout=0.25) as connection:
                connection.execute("PRAGMA busy_timeout=250")
                connection.execute(
                    "DELETE FROM capture_hash_index WHERE resolved_path = ?",
                    (resolved,),
                )
        except (OSError, sqlite3.Error):
            pass

    @staticmethod
    def _v2_cache_header(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("rb") as handle:
                if handle.read(len(_REPLAY_CACHE_MAGIC)) != _REPLAY_CACHE_MAGIC:
                    return None
                raw_size = handle.read(_REPLAY_CACHE_HEADER.size)
                if len(raw_size) != _REPLAY_CACHE_HEADER.size:
                    return None
                header_size = _REPLAY_CACHE_HEADER.unpack(raw_size)[0]
                if header_size <= 0 or header_size > 1024 * 1024:
                    return None
                payload = json.loads(handle.read(header_size))
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            struct.error,
        ):
            return None
        return payload if isinstance(payload, dict) else None

    def migrate_legacy_entries(self) -> int:
        """Attach old flat v2 caches to sources without reading captures."""

        index_path = self.directory / "capture_hash_index.sqlite3"
        if not index_path.is_file():
            return 0
        try:
            with sqlite3.connect(index_path, timeout=0.5) as connection:
                connection.execute("PRAGMA busy_timeout=500")
                rows = connection.execute(
                    "SELECT resolved_path, size, modified_ns, content_hash "
                    "FROM capture_hash_index"
                ).fetchall()
        except sqlite3.Error:
            return 0
        sources_by_hash: dict[str, list[tuple[Path, int, int]]] = {}
        for source_text, size, modified_ns, content_hash in rows:
            source = Path(str(source_text))
            try:
                source_stat = source.stat()
            except OSError:
                continue
            if (
                int(source_stat.st_size) != int(size or 0)
                or int(source_stat.st_mtime_ns) != int(modified_ns or 0)
            ):
                continue
            sources_by_hash.setdefault(str(content_hash or "").casefold(), []).append(
                (source, int(size or 0), int(modified_ns or 0))
            )
        migrated = 0
        for legacy_path in self.directory.glob("*.replaycache"):
            header = self._v2_cache_header(legacy_path)
            if header is None:
                continue
            capture_hash = str(header.get("capture_hash") or "").casefold()
            candidates = sources_by_hash.get(capture_hash, ())
            if not candidates:
                continue
            # The old flat cache had one content key even if the user kept an
            # identical copy. Associate it with the newest existing source;
            # another copy can create its own lifecycle folder on first load.
            source, source_size, source_mtime_ns = max(
                candidates,
                key=lambda row: row[2],
            )
            identity = ReplayCacheIdentity(
                key=legacy_path.stem,
                capture_hash=capture_hash,
                decoder_fingerprint=str(
                    header.get("decoder_fingerprint") or ""
                ),
                source_path=str(source.resolve()),
                source_size=source_size,
                source_mtime_ns=source_mtime_ns,
            )
            destination = self._path(identity)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy_path, destination)
                self._write_manifest(identity)
                migrated += 1
            except OSError:
                continue
        return migrated

    def remove_for_capture(self, capture_path: Path) -> int:
        """Remove generated decoded data belonging to exactly one source."""

        try:
            requested = os.path.normcase(
                str(Path(capture_path).expanduser().resolve())
            )
        except OSError:
            requested = os.path.normcase(str(Path(capture_path).expanduser()))
        removed = 0
        if not self.directory.is_dir():
            return removed
        for manifest_path in self.directory.glob("*/manifest.json"):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            source_path = os.path.normcase(str(payload.get("sourcePath") or ""))
            if source_path and source_path == requested:
                if self._safe_remove_cache_directory(manifest_path.parent):
                    removed += 1
        self._forget_hash_index_source(Path(capture_path))
        # Old raw sidecars are generated data too. They are safe to discard
        # even after the source itself has already been deleted.
        try:
            from .dpscap_segments import remove_dpscap_segments

            remove_dpscap_segments(Path(capture_path))
        except Exception:
            pass
        return removed

    def cleanup_orphans(self) -> int:
        """Delete only cache folders whose source no longer exists."""

        removed = 0
        if not self.directory.is_dir():
            return removed
        for manifest_path in self.directory.glob("*/manifest.json"):
            source: Path | None = None
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                if payload.get("schema") != REPLAY_CACHE_DIRECTORY_SCHEMA:
                    continue
                source_text = str(payload.get("sourcePath") or "").strip()
                if not source_text:
                    continue
                source = Path(source_text)
                source.stat()
                stale = False  # Changed files are rebuilt, not deleted here.
            except FileNotFoundError:
                stale = source is not None
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # Inaccessible disks and malformed metadata are not deletion.
                continue
            if not stale:
                continue
            if source is not None:
                try:
                    from .dpscap_segments import remove_dpscap_segments

                    remove_dpscap_segments(source)
                except Exception:
                    pass
                self._forget_hash_index_source(source)
            if self._safe_remove_cache_directory(manifest_path.parent):
                removed += 1
        return removed

    def start_orphan_monitor(self, interval_seconds: float = 10.0) -> bool:
        """Keep Explorer/manual source deletion coupled to generated data."""

        try:
            token = os.path.normcase(str(self.directory.expanduser().resolve()))
        except OSError:
            token = os.path.normcase(str(self.directory.expanduser()))
        with self._monitor_lock:
            if token in self._monitored_directories:
                return False
            self._monitored_directories.add(token)

        def monitor() -> None:
            try:
                self.migrate_legacy_entries()
                self.cleanup_orphans()
                while True:
                    time.sleep(max(5.0, float(interval_seconds)))
                    self.migrate_legacy_entries()
                    self.cleanup_orphans()
            except Exception:
                # Maintenance is optional; cache validity is still enforced by
                # the content key on every load.
                return

        threading.Thread(
            target=monitor,
            name="ReplayCacheOrphanMonitor",
            daemon=True,
        ).start()
        return True

    def load(self, identity: ReplayCacheIdentity) -> CombatAggregator | None:
        path = self._path(identity)
        try:
            encoded = path.read_bytes()
        except (OSError, EOFError, ValueError, TypeError, pickle.PickleError, zlib.error):
            path = self._legacy_path(identity)
            try:
                encoded = path.read_bytes()
            except (OSError, EOFError, ValueError, TypeError, pickle.PickleError, zlib.error):
                return None
        try:
            if encoded.startswith(_REPLAY_CACHE_MAGIC):
                offset = len(_REPLAY_CACHE_MAGIC)
                header_size = _REPLAY_CACHE_HEADER.unpack_from(encoded, offset)[0]
                offset += _REPLAY_CACHE_HEADER.size
                if header_size <= 0 or header_size > 1024 * 1024:
                    raise ValueError("invalid replay cache header")
                header = json.loads(encoded[offset : offset + header_size])
                if not isinstance(header, dict):
                    raise ValueError("invalid replay cache metadata")
                compressed = encoded[offset + header_size :]
                payload_bytes = zlib.decompress(compressed)
                if hashlib.sha256(payload_bytes).hexdigest() != header.get(
                    "payload_sha256"
                ):
                    raise ValueError("replay cache checksum mismatch")
                if header.get("schema") != REPLAY_CACHE_SCHEMA:
                    return None
                if header.get("key") != identity.key:
                    return None
                if header.get("capture_hash") != identity.capture_hash:
                    return None
                if header.get("decoder_fingerprint") != identity.decoder_fingerprint:
                    return None
                aggregator = pickle.loads(payload_bytes)
            else:
                # Read-only compatibility for caches produced before v4.25.
                payload = pickle.loads(zlib.decompress(encoded))
                if not isinstance(payload, dict):
                    return None
                if payload.get("schema") != REPLAY_CACHE_KEY_SCHEMA:
                    return None
                if payload.get("key") != identity.key:
                    return None
                if payload.get("capture_hash") != identity.capture_hash:
                    return None
                if payload.get("decoder_fingerprint") != identity.decoder_fingerprint:
                    return None
                aggregator = payload.get("aggregator")
        except (
            EOFError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            pickle.PickleError,
            struct.error,
            zlib.error,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        if not isinstance(aggregator, CombatAggregator):
            return None
        primary_path = self._path(identity)
        if path != primary_path and identity.source_path:
            try:
                primary_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, primary_path)
                self._write_manifest(identity)
                path = primary_path
            except OSError:
                pass
        try:
            path.touch()
        except OSError:
            pass
        return aggregator

    def save(
        self,
        identity: ReplayCacheIdentity,
        aggregator: CombatAggregator,
    ) -> None:
        destination = self._path(identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if identity.source_path and not Path(identity.source_path).is_file():
            self.remove_for_capture(Path(identity.source_path))
            return
        # A complete replay projection is materially more expensive than
        # pickle decompression for long captures.  Store it in the same object
        # graph so a cache hit can skip both packet decoding *and* rebuilding
        # the immutable CombatSnapshot.  Pickle memoization avoids duplicating
        # objects shared with the aggregator.
        if getattr(aggregator, "_replay_snapshot", None) is None:
            try:
                aggregator._replay_snapshot = aggregator.snapshot(
                    include_events=True
                )
            except Exception:
                # The aggregator remains a valid acceleration artifact even
                # if an optional projection cannot be prepared.
                pass
        payload = pickle.dumps(aggregator, protocol=5)
        header: dict[str, Any] = {
            "schema": REPLAY_CACHE_SCHEMA,
            "key": identity.key,
            "capture_hash": identity.capture_hash,
            "decoder_fingerprint": identity.decoder_fingerprint,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "created_at": int(time.time()),
        }
        header_bytes = json.dumps(
            header, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        encoded = b"".join(
            (
                _REPLAY_CACHE_MAGIC,
                _REPLAY_CACHE_HEADER.pack(len(header_bytes)),
                header_bytes,
                zlib.compress(payload, level=1),
            )
        )
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_bytes(encoded)
            temporary.replace(destination)
            self._write_manifest(identity)
            # Publish the replacement first; never remove a usable old cache
            # before the new decoder has successfully produced its output.
            for obsolete in destination.parent.glob("*.replaycache"):
                if obsolete != destination:
                    obsolete.unlink(missing_ok=True)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if identity.source_path:
            try:
                from .dpscap_segments import remove_dpscap_segments

                remove_dpscap_segments(Path(identity.source_path))
            except Exception:
                pass
        self._prune()

    def save_async(
        self,
        identity: ReplayCacheIdentity,
        aggregator: CombatAggregator,
    ) -> bool:
        """Persist a decoded replay without delaying the visible load result.

        The cache is an optional acceleration artifact.  Serialising a long
        aggregator, checksumming it, compressing it and writing it used to run
        synchronously at 98-99%, so the page looked frozen after decoding had
        already completed.  Atomic ``save`` remains the implementation; this
        wrapper only moves that optional work to one low-priority daemon and
        coalesces duplicate requests for the same destination/key.
        """

        try:
            directory_key = str(self.directory.expanduser().resolve())
        except OSError:
            directory_key = str(self.directory)
        token = (directory_key.casefold(), identity.key)
        with self._async_lock:
            if token in self._async_pending:
                return False
            self._async_pending.add(token)

        def persist() -> None:
            try:
                try:
                    from .npcap import set_current_thread_low_priority

                    set_current_thread_low_priority()
                except Exception:
                    pass
                # Cache compression is CPU-heavy.  Serialise background jobs
                # globally so opening several records quickly cannot create a
                # burst of competing zlib workers and make the UI/game stutter.
                with self._async_io_lock:
                    self.save(identity, aggregator)
            except Exception:
                # A replay cache miss only costs a later decode; it must never
                # turn an otherwise successful record load into an error.
                pass
            finally:
                with self._async_lock:
                    self._async_pending.discard(token)

        threading.Thread(
            target=persist,
            name=f"ReplayCacheSave-{identity.key[:8]}",
            daemon=True,
        ).start()
        return True

    @classmethod
    def wait_for_async_saves(cls, timeout: float = 30.0) -> bool:
        """Test/shutdown helper; normal page loads never wait for cache I/O."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with cls._async_lock:
                if not cls._async_pending:
                    return True
            time.sleep(0.01)
        with cls._async_lock:
            return not cls._async_pending

    def _prune(self) -> None:
        # Source-linked replay data persists until the source disappears.
        # Decoder revisions replace only that source's obsolete projection.
        return
