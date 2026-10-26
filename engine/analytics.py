from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_STATE_DIR = Path(os.environ.get("APPDATA", Path.home())) / "DragonNestBuildSimulator"
INSTALL_ID_PATH = APP_STATE_DIR / "anonymous_install_id.txt"
# Kept as a compatibility symbol for old tests/config migrations. Anonymous
# daily-use telemetry is a built-in service and no longer has a user toggle.
CONSENT_PATH = APP_STATE_DIR / "analytics_consent.json"
LAST_PING_PATH = APP_STATE_DIR / "analytics_last_ping.txt"
USAGE_STATE_PATH = APP_STATE_DIR / "anonymous_usage_state_v2.json"

_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_ACTIVE_BUCKET_SECONDS = 15 * 60
_EVENT_FLUSH_DEBOUNCE_SECONDS = 20.0
_MAX_PENDING_BUCKETS = 192
_MAX_EVENT_IDS = 64
_USAGE_COUNTERS = (
    "launches",
    "captureAttempts",
    "captureSuccesses",
    "captureFailures",
    "scoreAttempts",
    "scoreSuccesses",
    "scoreFailures",
    "updateAttempts",
    "updateSuccesses",
    "updateFailures",
    "crashes",
)
_EVENT_COUNTERS = {
    "launch": "launches",
    "capture_attempt": "captureAttempts",
    "capture_success": "captureSuccesses",
    "capture_failure": "captureFailures",
    "score_attempt": "scoreAttempts",
    "score_success": "scoreSuccesses",
    "score_failure": "scoreFailures",
    "update_attempt": "updateAttempts",
    "update_success": "updateSuccesses",
    "update_failure": "updateFailures",
    "crash": "crashes",
}

_PING_LOCK = threading.Lock()
_PING_IN_FLIGHT = False
_REPORTER_LOCK = threading.RLock()
_REPORTER: "_AnonymousUsageReporter | None" = None
_PENDING_EVENTS: list[tuple[str, str]] = []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_text(path: Path, value: str) -> None:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _install_id() -> str:
    existing = _read_text(INSTALL_ID_PATH)
    if existing:
        return existing
    value = str(uuid.uuid4())
    _write_text(INSTALL_ID_PATH, value)
    return value


def _telemetry_install_id(preferred: str) -> str:
    candidate = str(preferred or "").strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return _install_id()
    return str(parsed)


def usage_statistics_enabled(default: bool = True) -> bool:
    """Return the effective anonymous-usage policy.

    The argument remains for source compatibility with the legacy Qt shell.
    Daily anonymous use counting is always enabled in current builds and has
    no ordinary user-facing disable switch.
    """

    del default
    return True


def set_usage_statistics_enabled(enabled: bool) -> None:
    """Compatibility no-op for legacy callers.

    Persisting an old ``false`` preference must not disable the current
    mandatory, minimal daily-active counter. Writing ``true`` also migrates
    stale preference files without exposing another setting.
    """

    del enabled
    try:
        _write_text(CONSENT_PATH, json.dumps({"enabled": True}))
    except OSError:
        pass


def _analytics_settings(config: dict[str, Any]) -> dict[str, str]:
    settings = config.get("analytics", {}) if isinstance(config, dict) else {}
    if not isinstance(settings, dict):
        return {}
    return {
        "endpoint": str(settings.get("endpoint", "") or "").strip(),
        "upload_token": str(settings.get("upload_token", "") or "").strip(),
        "installation_id": str(settings.get("installation_id", "") or "").strip(),
    }


def _post_ping(
    endpoint: str,
    version: str,
    upload_token: str = "",
    installation_id: str = "",
) -> bool:
    """Send one idempotent daily-active record and return whether it succeeded."""

    global _PING_IN_FLIGHT
    today = date.today().isoformat()
    with _PING_LOCK:
        if _read_text(LAST_PING_PATH) == today:
            _PING_IN_FLIGHT = False
            return True

    payload = json.dumps(
        {
            "action": "usage_ping",
            "install_id": _telemetry_install_id(installation_id),
            "version": str(version or "unknown").strip()[:32],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "DragonNestACT-AnonymousUsage/1",
    }
    if upload_token:
        headers["X-Upload-Token"] = upload_token
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers=headers,
        method="POST",
    )
    succeeded = False
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = int(getattr(response, "status", response.getcode()))
            body = response.read().decode("utf-8", errors="replace")
            result = json.loads(body) if body else {}
            succeeded = (
                200 <= status < 300
                and isinstance(result, dict)
                and result.get("ok") is True
            )
            if succeeded:
                _write_text(LAST_PING_PATH, today)
    except (OSError, ValueError, urllib.error.URLError):
        # Usage telemetry must never block startup or surface an error to the
        # player. A failed ping is retried on the next launch.
        succeeded = False
    finally:
        with _PING_LOCK:
            _PING_IN_FLIGHT = False
    return succeeded


def _china_day() -> str:
    return datetime.now(_CHINA_TIMEZONE).date().isoformat()


def _active_bucket_epoch(timestamp: float | None = None) -> int:
    value = time.time() if timestamp is None else float(timestamp)
    return int(value // _ACTIVE_BUCKET_SECONDS) * _ACTIVE_BUCKET_SECONDS


def _empty_daily_counters() -> dict[str, int]:
    return {key: 0 for key in _USAGE_COUNTERS}


def _bounded_counter(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(1_000_000, parsed))


class _AnonymousUsageReporter:
    """Persist tiny anonymous counters and batch them off the hot path.

    The worker sleeps on a condition variable.  It wakes only for one
    debounced event upload or the next 15-minute active bucket, so capture,
    decoding and scoring threads never perform network I/O.
    """

    def __init__(
        self,
        endpoint: str,
        version: str,
        upload_token: str,
        installation_id: str,
    ) -> None:
        self.endpoint = str(endpoint or "").strip()
        self.version = str(version or "unknown").strip()[:32] or "unknown"
        self.upload_token = str(upload_token or "").strip()
        self.installation_id = _telemetry_install_id(installation_id)
        self._condition = threading.Condition(threading.RLock())
        self._state = self._load_state()
        self._dirty_deadline = 0.0
        self._next_heartbeat_epoch = self._next_bucket_epoch()
        self._thread = threading.Thread(
            target=self._run,
            name="anonymous-usage-batcher",
            daemon=True,
        )

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "days": {},
            "syncedDays": {},
            "pendingBuckets": [],
            "eventIds": [],
        }

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(USAGE_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = self._default_state()
        if not isinstance(payload, dict) or payload.get("schema") != 2:
            payload = self._default_state()
        days = payload.get("days")
        payload["days"] = days if isinstance(days, dict) else {}
        synced_days = payload.get("syncedDays")
        payload["syncedDays"] = (
            synced_days if isinstance(synced_days, dict) else {}
        )
        buckets = payload.get("pendingBuckets")
        payload["pendingBuckets"] = buckets if isinstance(buckets, list) else []
        event_ids = payload.get("eventIds")
        payload["eventIds"] = event_ids if isinstance(event_ids, list) else []
        self._prune_state(payload)
        return payload

    @staticmethod
    def _prune_state(state: dict[str, Any]) -> None:
        today = datetime.now(_CHINA_TIMEZONE).date()
        kept_days: dict[str, dict[str, int]] = {}
        for raw_day, raw_counters in dict(state.get("days") or {}).items():
            try:
                parsed = date.fromisoformat(str(raw_day))
            except ValueError:
                continue
            if not 0 <= (today - parsed).days <= 7:
                continue
            source = raw_counters if isinstance(raw_counters, dict) else {}
            kept_days[parsed.isoformat()] = {
                key: _bounded_counter(source.get(key))
                for key in _USAGE_COUNTERS
            }
        state["days"] = kept_days
        kept_synced_days: dict[str, dict[str, int]] = {}
        for day, counters in dict(state.get("syncedDays") or {}).items():
            if day not in kept_days or not isinstance(counters, dict):
                continue
            kept_synced_days[day] = {
                key: _bounded_counter(counters.get(key))
                for key in _USAGE_COUNTERS
            }
        state["syncedDays"] = kept_synced_days
        earliest = _active_bucket_epoch() - 48 * 60 * 60
        latest = _active_bucket_epoch() + _ACTIVE_BUCKET_SECONDS
        valid_buckets: set[int] = set()
        for value in state.get("pendingBuckets", []):
            if not isinstance(value, (int, float)):
                continue
            try:
                parsed_bucket = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if earliest <= parsed_bucket <= latest:
                valid_buckets.add(
                    parsed_bucket
                    // _ACTIVE_BUCKET_SECONDS
                    * _ACTIVE_BUCKET_SECONDS
                )
        state["pendingBuckets"] = sorted(valid_buckets)[-_MAX_PENDING_BUCKETS:]
        state["eventIds"] = [
            str(value)[:64]
            for value in state.get("eventIds", [])
            if isinstance(value, str) and value
        ][-_MAX_EVENT_IDS:]

    def _save_locked(self) -> None:
        self._prune_state(self._state)
        try:
            _write_json_atomic(USAGE_STATE_PATH, self._state)
        except OSError:
            # Usage statistics must never interfere with the application.
            pass

    def _next_bucket_epoch(self) -> float:
        current = time.time()
        next_bucket = (int(current) // _ACTIVE_BUCKET_SECONDS + 1) * _ACTIVE_BUCKET_SECONDS
        # A deterministic 0-29 second spread avoids every client hitting the
        # CloudBase function on the exact quarter-hour boundary.
        jitter = int(hashlib.sha256(self.installation_id.encode("ascii")).hexdigest()[:4], 16) % 30
        return float(next_bucket + jitter)

    def _ensure_day_locked(self) -> dict[str, int]:
        day = _china_day()
        source = self._state["days"].get(day)
        if not isinstance(source, dict):
            source = _empty_daily_counters()
            self._state["days"][day] = source
        for key in _USAGE_COUNTERS:
            source[key] = _bounded_counter(source.get(key))
        return source

    def _mark_active_locked(self) -> None:
        bucket = _active_bucket_epoch()
        pending = self._state["pendingBuckets"]
        if bucket not in pending:
            pending.append(bucket)

    def start(self) -> None:
        with self._condition:
            counters = self._ensure_day_locked()
            counters["launches"] += 1
            self._mark_active_locked()
            self._save_locked()
        self._thread.start()

    def record(self, event: str, event_id: str = "") -> bool:
        key = _EVENT_COUNTERS.get(str(event or "").strip().casefold())
        if not key:
            return False
        digest = (
            hashlib.sha256(str(event_id).encode("utf-8", errors="replace")).hexdigest()
            if str(event_id or "").strip()
            else ""
        )
        with self._condition:
            if digest and digest in self._state["eventIds"]:
                return False
            if digest:
                self._state["eventIds"].append(digest)
            counters = self._ensure_day_locked()
            counters[key] = min(1_000_000, counters[key] + 1)
            self._mark_active_locked()
            self._save_locked()
            deadline = time.monotonic() + _EVENT_FLUSH_DEBOUNCE_SECONDS
            self._dirty_deadline = (
                min(self._dirty_deadline, deadline)
                if self._dirty_deadline > 0.0
                else deadline
            )
            self._condition.notify_all()
        return True

    def _payload_locked(
        self,
    ) -> tuple[dict[str, Any], tuple[int, ...], dict[str, dict[str, int]]]:
        self._prune_state(self._state)
        buckets = tuple(int(value) for value in self._state["pendingBuckets"])
        synced_days = self._state["syncedDays"]
        sent_daily = {
            day: dict(counters)
            for day, counters in sorted(self._state["days"].items())
            if dict(counters) != dict(synced_days.get(day) or {})
        }
        daily = [{"day": day, **counters} for day, counters in sent_daily.items()]
        return (
            {
                "action": "usage_batch",
                "install_id": self.installation_id,
                "version": self.version,
                "daily_totals": daily,
                "active_buckets": list(buckets),
            },
            buckets,
            sent_daily,
        )

    def _flush(self) -> bool:
        with self._condition:
            payload, sent_buckets, sent_daily = self._payload_locked()
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "DragonNestACT-AnonymousUsage/2",
        }
        if self.upload_token:
            headers["X-Upload-Token"] = self.upload_token
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = int(getattr(response, "status", response.getcode()))
                raw = response.read().decode("utf-8", errors="replace")
                result = json.loads(raw) if raw else {}
            succeeded = (
                200 <= status < 300
                and isinstance(result, dict)
                and result.get("ok") is True
            )
            metrics_supported = result.get("metrics_supported") is not False
        except (OSError, ValueError, urllib.error.URLError):
            succeeded = False
            metrics_supported = False
        if succeeded and metrics_supported and (sent_buckets or sent_daily):
            sent = set(sent_buckets)
            with self._condition:
                self._state["pendingBuckets"] = [
                    value
                    for value in self._state["pendingBuckets"]
                    if int(value) not in sent
                ]
                synced_days = self._state["syncedDays"]
                for day, counters in sent_daily.items():
                    # If an event arrived while the HTTP request was in
                    # flight, acknowledge only the older snapshot. The newer
                    # counters remain different and are retried next time.
                    synced_days[day] = dict(counters)
                self._save_locked()
        return succeeded

    def _run(self) -> None:
        # Flush the launch and current active bucket once. Subsequent events
        # are coalesced for 20 seconds; otherwise this worker sleeps until the
        # next quarter-hour bucket.
        self._flush()
        while True:
            with self._condition:
                now_epoch = time.time()
                heartbeat_delay = max(0.0, self._next_heartbeat_epoch - now_epoch)
                dirty_delay = (
                    max(0.0, self._dirty_deadline - time.monotonic())
                    if self._dirty_deadline > 0.0
                    else heartbeat_delay
                )
                self._condition.wait(timeout=min(heartbeat_delay, dirty_delay))
                heartbeat_due = time.time() >= self._next_heartbeat_epoch
                dirty_due = (
                    self._dirty_deadline > 0.0
                    and time.monotonic() >= self._dirty_deadline
                )
                if heartbeat_due:
                    self._mark_active_locked()
                    self._save_locked()
                    self._next_heartbeat_epoch = self._next_bucket_epoch()
                if dirty_due:
                    self._dirty_deadline = 0.0
            if heartbeat_due or dirty_due:
                self._flush()


def schedule_usage_ping(
    parent: object | None,
    config: dict[str, Any],
    version: str,
) -> bool:
    """Start the silent, sleeping usage batcher once per application process."""

    del parent
    global _REPORTER
    settings = _analytics_settings(config)
    endpoint = settings.get("endpoint", "")
    if not endpoint:
        return False
    with _REPORTER_LOCK:
        if _REPORTER is not None:
            return False
        try:
            reporter = _AnonymousUsageReporter(
                endpoint,
                version,
                settings.get("upload_token", ""),
                settings.get("installation_id", ""),
            )
        except Exception:
            return False
        _REPORTER = reporter
        pending = tuple(_PENDING_EVENTS)
        _PENDING_EVENTS.clear()
    try:
        reporter.start()
    except Exception:
        with _REPORTER_LOCK:
            if _REPORTER is reporter:
                _REPORTER = None
            _PENDING_EVENTS[:0] = pending
        return False
    for event, event_id in pending:
        reporter.record(event, event_id)
    return True


def record_usage_event(event: str, event_id: str = "") -> bool:
    """Record one low-frequency anonymous product event without blocking.

    Event payloads never contain character, capture or hardware data.  A
    caller-supplied identifier is hashed and retained locally only to prevent
    duplicate crash-package notifications.
    """

    normalized = str(event or "").strip().casefold()
    if normalized not in _EVENT_COUNTERS:
        return False
    with _REPORTER_LOCK:
        reporter = _REPORTER
        if reporter is None:
            if len(_PENDING_EVENTS) < 64:
                _PENDING_EVENTS.append((normalized, str(event_id or "")))
            return True
    return reporter.record(normalized, str(event_id or ""))
