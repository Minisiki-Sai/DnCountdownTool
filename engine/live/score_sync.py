from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover - V4.10 is distributed for Windows.
    winreg = None

from .supabase_score_sync import (
    TeamScoreSupabaseSyncService,
    normalize_supabase_function_url,
)
from .benchmark_catalog import (
    BENCHMARK_CACHE_TTL_SECONDS,
    VersionedTeamScoreBenchmarkCatalog,
)
from .team_score_sync import (
    CURRENT_SCORE_SCHEMA_VERSION,
    TeamScoreExportRow,
    TeamScoreSyncStore,
    capture_content_hash,
    default_team_score_sync_path,
    protect_secret,
    resolve_score_nest,
    unprotect_secret,
)
from ..analytics import schedule_usage_ping
from .ranking_query_gate import RankingQueryGate


_REGISTRY_PATH = (
    r"Software\DragonNestBuildSimulator\DragonNestBuildSimulator"
    r"\live\team_score_sync"
)
_BUNDLED_CONFIG = (
    Path("engine")
    / "data"
    / "modular"
    / "supabase_score_sync.json"
)
_ADMIN_PASSWORD_HASH_KEY = "admin_password_sha256"
# Privacy is enforced by the cloud on every fresh request.  A shorter local
# cache prevents a newly hidden name from remaining visible for five hours.
_HISTORY_CACHE_TTL_SECONDS = 5 * 60.0
_HISTORY_CACHE_MAX_ENTRIES = 160
_USAGE_STATISTICS_CACHE_TTL_SECONDS = 5 * 60.0
_LEGACY_DEFAULT_SCORE_ENDPOINTS = {
    "https://cbszidcntkwdkqcmnwfg.supabase.co",
    "https://cbszidcntkwdkqcmnwfg.supabase.co/functions/v1/team-score-sync",
}


def _migrated_score_endpoint(stored: object, bundled: object) -> str:
    stored_text = str(stored or "").strip().rstrip("/")
    bundled_text = str(bundled or "").strip().rstrip("/")
    if stored_text.casefold() in _LEGACY_DEFAULT_SCORE_ENDPOINTS and bundled_text:
        return bundled_text
    return stored_text or bundled_text


def _score_sync_admin_password_sha256() -> str:
    value = str(_bundled_defaults().get(_ADMIN_PASSWORD_HASH_KEY, "") or "").strip().casefold()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return ""
    return value


def score_sync_admin_password_configured() -> bool:
    return bool(_score_sync_admin_password_sha256())


def verify_score_sync_admin_password(password: object) -> bool:
    if not isinstance(password, str):
        return False
    configured_digest = _score_sync_admin_password_sha256()
    if not configured_digest:
        return False
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, configured_digest)


def require_score_sync_admin_password(password: object) -> None:
    if not score_sync_admin_password_configured():
        raise PermissionError("当前版本未配置管理密码，管理员设置不可用")
    if not verify_score_sync_admin_password(password):
        raise PermissionError("管理密码错误，无法查看或修改全队评分上传设置")


@dataclass(frozen=True, slots=True)
class V410ScoreSyncConfig:
    enabled: bool
    endpoint: str
    upload_token: str
    installation_id: str
    benchmark_manifest_url: str = ""
    benchmark_cache_ttl_seconds: float = BENCHMARK_CACHE_TTL_SECONDS
    capture_file_upload_enabled: bool = False

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.endpoint.strip()
            and self.upload_token.strip()
            and self.installation_id.strip()
        )


@dataclass(frozen=True, slots=True)
class V410ScoreSyncJob:
    capture_path: Path
    boss_names: tuple[str, ...]
    target_ids: tuple[str, ...]
    map_ids: tuple[int, ...]
    map_names: tuple[str, ...]
    rows: tuple[TeamScoreExportRow, ...]
    force_upload: bool = False
    expected_capture_hash: str = ""


def _setting_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _setting_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _bundled_defaults() -> dict[str, object]:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    path = base / _BUNDLED_CONFIG
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _registry_values() -> dict[str, object]:
    if winreg is None:
        return {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            values: dict[str, object] = {}
            for name in (
                "enabled",
                "capture_file_upload_enabled",
                "capture_file_upload_user_choice",
                "supabase_endpoint",
                "supabase_upload_token",
                "installation_id",
            ):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    pass
            return values
    except FileNotFoundError:
        return {}


def load_v410_score_sync_config() -> V410ScoreSyncConfig:
    bundled = _bundled_defaults()
    values = _registry_values()
    installation_id = str(values.get("installation_id", "") or "").strip()
    if not installation_id:
        installation_id = str(uuid.uuid4())
        _write_registry_values({"installation_id": installation_id})
    encrypted = str(values.get("supabase_upload_token", "") or "")
    capture_upload_explicitly_chosen = _setting_bool(
        values.get("capture_file_upload_user_choice"), False
    )
    return V410ScoreSyncConfig(
        enabled=_setting_bool(
            values.get("enabled", bundled.get("enabled", True)),
            _setting_bool(bundled.get("enabled"), True),
        ),
        capture_file_upload_enabled=_setting_bool(
            (
                values.get("capture_file_upload_enabled", False)
                if capture_upload_explicitly_chosen
                else bundled.get("capture_file_upload_enabled", False)
            ),
            False,
        ),
        endpoint=_migrated_score_endpoint(
            values.get("supabase_endpoint", ""), bundled.get("endpoint", "")
        ),
        upload_token=(
            unprotect_secret(encrypted)
            if encrypted
            else str(bundled.get("upload_token", "") or "").strip()
        ),
        installation_id=installation_id,
        benchmark_manifest_url=str(
            bundled.get("benchmark_manifest_url", "") or ""
        ).strip(),
        benchmark_cache_ttl_seconds=max(
            60.0,
            _setting_float(
                bundled.get("benchmark_cache_ttl_seconds"),
                BENCHMARK_CACHE_TTL_SECONDS,
            ),
        ),
    )


def _write_registry_values(values: dict[str, object]) -> None:
    if winreg is None:  # pragma: no cover
        return
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        _REGISTRY_PATH,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def save_v410_score_sync_config(
    *,
    enabled: bool,
    endpoint: str,
    upload_token: str | None,
    capture_file_upload_enabled: bool | None = None,
) -> V410ScoreSyncConfig:
    current = load_v410_score_sync_config()
    token = current.upload_token if upload_token is None else str(upload_token).strip()
    endpoint_text = str(endpoint or "").strip()
    if endpoint_text:
        # Validate early while preserving the project URL in the settings UI.
        normalize_supabase_function_url(endpoint_text)
    registry_values: dict[str, object] = {
        "enabled": "true" if enabled else "false",
        "capture_file_upload_enabled": (
            "true"
            if (
                current.capture_file_upload_enabled
                if capture_file_upload_enabled is None
                else capture_file_upload_enabled
            )
            else "false"
        ),
        "supabase_endpoint": endpoint_text,
        "supabase_upload_token": protect_secret(token),
        "installation_id": current.installation_id,
    }
    if capture_file_upload_enabled is not None:
        # V4.41 and earlier shipped with this option silently enabled. Only a
        # choice made in the now-visible control may opt a user back in; an
        # inherited legacy ``true`` must not survive the privacy-default
        # migration.
        registry_values["capture_file_upload_user_choice"] = "true"
    _write_registry_values(registry_values)
    return load_v410_score_sync_config()


class V410ScoreSyncManager:
    """Window-free bridge for the SQLite/cloud score synchronization pipeline."""

    def __init__(self) -> None:
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._history_request_lock = threading.Lock()
        self._ranking_query_gate = RankingQueryGate()
        self._overlay_visibility_lock = threading.Lock()
        self._overlay_visibility_synced: tuple[str, bool] | None = None
        self._score_visibility_synced: tuple[str, bool, bool] | None = None
        self._history_cache: dict[
            tuple[object, ...], tuple[float, dict[str, Any]]
        ] = {}
        self._history_cache_ttl_seconds = _HISTORY_CACHE_TTL_SECONDS
        self._history_cache_max_entries = _HISTORY_CACHE_MAX_ENTRIES
        self._usage_statistics_request_lock = threading.Lock()
        self._usage_statistics_cache: dict[
            int, tuple[float, dict[str, Any]]
        ] = {}
        self._jobs: queue.Queue[V410ScoreSyncJob | None] = queue.Queue()
        self._running = False
        self._last_ok: bool | None = None
        self._message = ""
        config = load_v410_score_sync_config()
        self._benchmark_catalog = VersionedTeamScoreBenchmarkCatalog(
            config.benchmark_manifest_url,
            cache_ttl_seconds=config.benchmark_cache_ttl_seconds,
        )
        self._benchmark_catalog.prime()
        self._benchmark_prefetch_running = False
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            name="v410-team-score-sync",
            daemon=True,
        )
        self._worker.start()
        # One sleeping process-wide batcher records the launch, low-frequency
        # outcomes and quarter-hour activity. This remains independent of the
        # score-upload preference: disabling score uploads must not disable
        # the built-in anonymous aggregate counters.
        schedule_usage_ping(
            None,
            {
                "analytics": {
                    "endpoint": config.endpoint,
                    "upload_token": config.upload_token,
                    "installation_id": config.installation_id,
                }
            },
            os.getenv("DRAGON_NEST_APP_VERSION", "unknown"),
        )
        self._start_benchmark_prefetch()
        if config.ready and (config.enabled or config.capture_file_upload_enabled):
            self._jobs.put(None)

    def _start_benchmark_prefetch(self) -> None:
        with self._state_lock:
            if self._benchmark_prefetch_running:
                return
            self._benchmark_prefetch_running = True
        threading.Thread(
            target=self._prefetch_benchmarks,
            name="v410-team-score-benchmark-prefetch",
            daemon=True,
        ).start()

    def _prefetch_benchmarks(self) -> None:
        try:
            self._benchmark_catalog.refresh()
        except Exception:
            # Static benchmark refresh is optional; bundled or stale local data
            # remains usable without touching PostgreSQL.
            return
        finally:
            with self._state_lock:
                self._benchmark_prefetch_running = False

    def _queue_count(self, method: str) -> int:
        if not default_team_score_sync_path().is_file():
            return 0
        try:
            try:
                with TeamScoreSyncStore(read_only=True) as store:
                    return getattr(store, method)()
            except sqlite3.OperationalError as exc:
                # An older installed database may not have the count columns
                # yet. Preserve the existing migration path only in that case;
                # current-schema reads never reopen a writable store.
                if not str(exc).lower().startswith(("no such table:", "no such column:")):
                    raise
                with TeamScoreSyncStore() as store:
                    return getattr(store, method)()
        except Exception:
            return 0

    def _pending_count(self) -> int:
        return self._queue_count("pending_supabase_count")

    def _pending_capture_file_count(self) -> int:
        return self._queue_count("pending_capture_file_count")

    def status(self, *, reveal_sensitive: bool = False) -> dict[str, Any]:
        config = load_v410_score_sync_config()
        try:
            function_url = normalize_supabase_function_url(config.endpoint)
        except Exception:
            function_url = ""
        with self._state_lock:
            running = self._running
            last_ok = self._last_ok
            message = self._message
        benchmark_status = (
            self._benchmark_catalog.status()
            if hasattr(self, "_benchmark_catalog")
            else {}
        )
        return {
            "adminPasswordConfigured": score_sync_admin_password_configured(),
            "enabled": config.enabled,
            # This is an ordinary user preference, not an administrator
            # secret. Keep it visible while endpoint/token details stay
            # redacted behind the management password.
            "captureFileUploadEnabled": config.capture_file_upload_enabled,
            "endpoint": config.endpoint if reveal_sensitive else "",
            "functionUrl": function_url if reveal_sensitive else "",
            "tokenConfigured": bool(config.upload_token),
            "installationId": config.installation_id if reveal_sensitive else "",
            "ready": config.ready,
            "pendingCaptures": self._pending_count(),
            "pendingCaptureFiles": self._pending_capture_file_count(),
            "queuePath": str(default_team_score_sync_path()),
            "running": running,
            "lastOk": last_ok,
            "message": message,
            "locked": not reveal_sensitive,
            "benchmarkVersion": benchmark_status.get("version", ""),
            "benchmarkSource": benchmark_status.get("source", "unavailable"),
            "benchmarkRows": benchmark_status.get("rowCount", 0),
            "benchmarkCacheTtlSeconds": benchmark_status.get(
                "cacheTtlSeconds", BENCHMARK_CACHE_TTL_SECONDS
            ),
        }

    def unlock(self, password: object) -> dict[str, Any]:
        require_score_sync_admin_password(password)
        return self.status(reveal_sensitive=True)

    def save(self, payload: dict[str, Any], password: object) -> dict[str, Any]:
        require_score_sync_admin_password(password)
        token_value = payload.get("uploadToken")
        preserve_token = bool(payload.get("preserveToken", token_value in {None, ""}))
        save_v410_score_sync_config(
            enabled=bool(payload.get("enabled", True)),
            endpoint=str(payload.get("endpoint", "")),
            upload_token=None if preserve_token else str(token_value or ""),
        )
        with self._state_lock:
            self._message = "设置已保存"
        self._start_benchmark_prefetch()
        return self.status(reveal_sensitive=True)

    def set_capture_file_upload_enabled(
        self, enabled: object, password: object = None
    ) -> dict[str, Any]:
        del password  # Kept only for compatibility with older frontends.
        current = load_v410_score_sync_config()
        updated = save_v410_score_sync_config(
            enabled=current.enabled,
            endpoint=current.endpoint,
            upload_token=None,
            capture_file_upload_enabled=bool(enabled),
        )
        with self._state_lock:
            self._message = (
                ".dpscap 私有云存储上传已开启"
                if updated.capture_file_upload_enabled
                else ".dpscap 私有云存储上传已关闭"
            )
        if updated.ready and updated.capture_file_upload_enabled:
            self._jobs.put(None)
        return self.status(reveal_sensitive=False)

    def test_and_flush(self, password: object) -> dict[str, Any]:
        require_score_sync_admin_password(password)
        config = load_v410_score_sync_config()
        if not config.endpoint.strip():
            raise ValueError("请先填写云端评分服务地址")
        if not config.upload_token.strip():
            raise ValueError("请先填写 TEAM_SCORE_UPLOAD_TOKEN 专用上传密钥")
        with self._operation_lock:
            service = TeamScoreSupabaseSyncService(
                config.endpoint,
                config.upload_token,
                config.installation_id,
            )
            version = service.health_check()
            with TeamScoreSyncStore() as store:
                result = service.sync_store(
                    store,
                    upload_capture_files=config.capture_file_upload_enabled,
                )
        if result.uploaded_captures:
            self._invalidate_history_cache()
        with self._state_lock:
            self._last_ok = not result.failed_captures and not result.capture_file_error
            self._message = f"连接成功（服务版本 {version}）。{result.summary()}"
        self._start_benchmark_prefetch()
        return self.status(reveal_sensitive=True)

    def usage_statistics(
        self,
        password: object = None,
        *,
        days: object = 30,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        """Return the public aggregate daily-use dashboard.

        ``password`` remains accepted so older frontends can keep calling the
        same local route.  Only aggregate counts are returned; endpoint,
        upload token and installation identifiers stay behind the existing
        administrator gate.
        """

        del password
        config = load_v410_score_sync_config()
        if not config.endpoint.strip() or not config.upload_token.strip():
            raise ValueError("匿名使用统计尚未连接 CloudBase 服务")
        try:
            requested_days = int(days)
        except (TypeError, ValueError):
            requested_days = 30
        requested_days = max(7, min(180, requested_days))
        refresh = _setting_bool(force_refresh, False)
        now = time.monotonic()
        with self._state_lock:
            cache = getattr(self, "_usage_statistics_cache", {})
            cached = cache.get(requested_days)
            if (
                not refresh
                and cached is not None
                and now - cached[0] < _USAGE_STATISTICS_CACHE_TTL_SECONDS
            ):
                return dict(cached[1])

        request_lock = getattr(self, "_usage_statistics_request_lock", None)
        if request_lock is None:
            request_lock = threading.Lock()
            self._usage_statistics_request_lock = request_lock
        with request_lock:
            now = time.monotonic()
            with self._state_lock:
                cache = getattr(self, "_usage_statistics_cache", {})
                cached = cache.get(requested_days)
                if (
                    not refresh
                    and cached is not None
                    and now - cached[0] < _USAGE_STATISTICS_CACHE_TTL_SECONDS
                ):
                    return dict(cached[1])
            service = TeamScoreSupabaseSyncService(
                config.endpoint,
                config.upload_token,
                config.installation_id,
            )
            statistics = service.fetch_usage_statistics(requested_days)
            with self._state_lock:
                cache = getattr(self, "_usage_statistics_cache", None)
                if cache is None:
                    cache = {}
                    self._usage_statistics_cache = cache
                cache[requested_days] = (time.monotonic(), dict(statistics))
            return statistics

    def benchmark(self, nest: str, job: str) -> dict[str, Any]:
        nest_name = str(nest or "").strip()
        job_name = str(job or "").strip()
        if not nest_name or not job_name:
            return {"state": "unavailable", "message": "当前记录缺少巢穴或职业信息"}
        if self._benchmark_catalog.refresh_due():
            self._start_benchmark_prefetch()
        status = self._benchmark_catalog.status()
        catalog_schema_version = int(status.get("scoreSchemaVersion") or 0)
        if catalog_schema_version != CURRENT_SCORE_SCHEMA_VERSION:
            return {
                "state": "unavailable",
                "nest": nest_name,
                "job": job_name,
                "message": (
                    "职业均值仍使用旧评分口径，等待生成当前算法版本的静态数据"
                ),
            }
        benchmark = self._benchmark_catalog.lookup(nest_name, job_name)
        if benchmark is not None:
            return {"state": "ready", "message": "", **benchmark}
        if not int(status.get("rowCount") or 0):
            return {
                "state": "error",
                "nest": nest_name,
                "job": job_name,
                "message": "职业均值静态数据暂不可用",
            }
        return {
            "state": "empty",
            "nest": nest_name,
            "job": job_name,
            "message": "该巢穴与职业尚无静态样本",
        }

    def database_profession_ranking(
        self,
        *,
        section: object = "professions",
        composition: object = "",
        nest: object = "",
        score_schema_versions: object = (),
        completion_status: object = "",
        recorded_from: object = "",
        recorded_to: object = "",
        profile_id: object = "",
        character_name: object = "",
        job: object = "",
        force_refresh: object = False,
    ) -> dict[str, Any]:
        section_name = str(section or "").strip().casefold()
        if section_name not in {"professions", "skills", "support", "teams", "distribution", "team-captures", "roles"}:
            section_name = "professions"
        nest_name = str(nest or "").strip()[:2048]
        completion = str(completion_status or "").strip().casefold()
        if completion not in {"cleared", "not_cleared", "unknown"}:
            completion = ""
        from_value = str(recorded_from or "").strip()[:64]
        to_value = str(recorded_to or "").strip()[:64]
        # Ranking identity is character-name based. Dragon Nest profile IDs are
        # session aliases and may be reused by another character, so accepting
        # one here can merge unrelated jobs into the "my position" result.
        # Keep the argument for wire compatibility with released clients, but
        # deliberately never use it as a ranking key.
        profile_value = ""
        character_value = str(character_name or "").strip()[:64]
        if any(ord(char) < 32 for char in character_value):
            character_value = ""
        job_name = str(job or "").strip()[:64]
        composition_name = str(composition or "").strip()[:1024]
        raw_versions = (
            score_schema_versions
            if isinstance(score_schema_versions, (list, tuple, set))
            else ()
        )
        normalized_versions: list[int] = []
        for value in raw_versions:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if 0 < parsed <= 29991231 and parsed not in normalized_versions:
                normalized_versions.append(parsed)
        versions: tuple[int, ...] = tuple(normalized_versions[:64])
        refresh = _setting_bool(force_refresh, False)
        cache_key = (
            "database-ranking-center-analytics-v2",
            section_name,
            nest_name.casefold(),
            versions,
            completion,
            from_value,
            to_value,
            profile_value,
            character_value.casefold(),
            job_name.casefold(),
            composition_name,
        )
        if refresh:
            with self._state_lock:
                self._history_cache.pop(cache_key, None)
        else:
            cached = self._history_cached(cache_key)
            if cached is not None:
                return cached

        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置",
                "generatedAt": "",
                "section": section_name,
                "filters": {
                    "nest": nest_name, "scoreSchemaVersions": list(versions),
                    "completionStatus": completion, "recordedFrom": from_value,
                    "recordedTo": to_value, "profileId": profile_value,
                    "characterName": character_value,
                    "job": job_name,
                },
                "facets": {"nests": [], "scoreSchemaVersions": []},
                "summary": {},
                "rows": [],
                "myPositions": [],
            }
        try:
            # Separate from history/detail requests; deduplicate identical
            # rankings while permitting at most three distinct queries.
            with self._state_lock:
                if not hasattr(self, "_ranking_query_gate"):
                    self._ranking_query_gate = RankingQueryGate()
                gate = self._ranking_query_gate
            with gate.hold(cache_key):
                payload = self._history_cached(cache_key)
                if payload is None:
                    service = TeamScoreSupabaseSyncService(
                        config.endpoint, config.upload_token, config.installation_id
                    )
                    ranking = service.fetch_database_profession_ranking(
                        section=section_name,
                        composition=composition_name,
                        nest=nest_name,
                        score_schema_versions=versions,
                        completion_status=completion,
                        recorded_from=from_value,
                        recorded_to=to_value,
                        profile_id=profile_value,
                        character_name=character_value,
                        job=job_name,
                    )
                    rows = [
                        row for row in (ranking.get("rows") or ())
                        if isinstance(row, dict)
                    ]
                    payload = {
                        **ranking,
                        "state": "ready" if rows or ranking.get("myPositions") else "empty",
                        "message": "" if rows or ranking.get("myPositions") else "当前筛选范围暂无评分样本",
                        "rows": rows,
                    }
                    self._remember_history(cache_key, payload)
            return payload
        except Exception as exc:
            message = str(exc)
            if re.search(
                r"canceling statement due to statement timeout|statement timeout|query canceled",
                message,
                flags=re.IGNORECASE,
            ):
                message = (
                    f"云端数据库排行查询超时（{section_name}）。"
                    "请重试，或暂时缩小巢穴、日期、评分版本范围。"
                    "超时不代表没有数据，也不能据此判断数据库迁移缺失。"
                )
            elif (
                "unsupported_action" in message
                or "get_database_" in message
            ):
                message = "数据库排行功能尚未部署完整，请执行 CloudBase 数据库 039 迁移并部署 v4.76 评分同步云函数"
            return {
                "state": "error",
                "message": message,
                "generatedAt": "",
                "section": section_name,
                "filters": {
                    "nest": nest_name, "scoreSchemaVersions": list(versions),
                    "completionStatus": completion, "recordedFrom": from_value,
                    "recordedTo": to_value, "profileId": profile_value,
                    "characterName": character_value,
                    "job": job_name,
                },
                "facets": {"nests": [], "scoreSchemaVersions": []},
                "summary": {},
                "rows": [],
                "myPositions": [],
            }

    @staticmethod
    def _history_character_name(value: object) -> str:
        name = str(value or "").strip()
        if (
            not name
            or len(name) > 64
            or name in {"未知角色", "未知职业"}
            or re.fullmatch(r"(?:角色\s*)?0x[0-9A-Fa-f]+", name)
        ):
            raise ValueError("当前队友没有可验证的真实角色名")
        return name

    @staticmethod
    def _history_job_name(value: object) -> str:
        job = str(value or "").strip()
        if not job or len(job) > 64 or job in {"未知职业", "无"}:
            raise ValueError("请选择需要查询的二转职业")
        return job

    def _history_cached(self, key: tuple[object, ...]) -> dict[str, Any] | None:
        with self._state_lock:
            cached = self._history_cache.get(key)
            if cached is None:
                return None
            expires_at, payload = cached
            if expires_at <= time.monotonic():
                self._history_cache.pop(key, None)
                return None
            self._history_cache.pop(key, None)
            self._history_cache[key] = (expires_at, payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

    def _remember_history(
        self, key: tuple[object, ...], payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._state_lock:
            now = time.monotonic()
            for cached_key, (expires_at, _) in tuple(self._history_cache.items()):
                if expires_at <= now:
                    self._history_cache.pop(cached_key, None)
            self._history_cache.pop(key, None)
            self._history_cache[key] = (
                now + self._history_cache_ttl_seconds,
                json.loads(json.dumps(payload, ensure_ascii=False)),
            )
            limit = max(
                1,
                int(
                    getattr(
                        self,
                        "_history_cache_max_entries",
                        _HISTORY_CACHE_MAX_ENTRIES,
                    )
                ),
            )
            while len(self._history_cache) > limit:
                oldest_key = next(iter(self._history_cache))
                self._history_cache.pop(oldest_key, None)
        return payload

    def _invalidate_history_cache(self) -> None:
        with self._state_lock:
            self._history_cache.clear()

    @staticmethod
    def _history_nest_key(value: object) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _history_completion_filter(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"", "cleared", "not_cleared", "unknown"}:
            raise ValueError("通关状态筛选值无效")
        return normalized

    @staticmethod
    def _history_schema_versions(value: object) -> tuple[int, ...]:
        values = value if isinstance(value, (list, tuple, set)) else ()
        result: list[int] = []
        for item in values:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if 0 < parsed <= 29991231 and parsed not in result:
                result.append(parsed)
        return tuple(result[:64])

    @classmethod
    def _complete_character_history(
        cls,
        service: TeamScoreSupabaseSyncService,
        name: str,
    ) -> dict[str, Any]:
        """Fetch one character once, then let every nest/page query stay local.

        CloudBase 1.7 can filter a nest in PostgreSQL, but installations still
        running the 1.6 function silently ignore that argument.  Keeping one
        complete, short-lived character snapshot locally makes both service
        versions behave identically and avoids a cloud request for every
        dropdown change or page turn.
        """

        remote_limit = 50
        remote_offset = 0
        expected_total: int | None = None
        items: list[dict[str, Any]] = []
        item_keys: set[str] = set()
        nests: list[str] = []
        nest_keys: set[str] = set()

        def remember_nest(value: object) -> None:
            nest_name = str(value or "").strip()
            nest_key = cls._history_nest_key(nest_name)
            if nest_name and nest_key not in nest_keys:
                nest_keys.add(nest_key)
                nests.append(nest_name)

        # The score table is pruned to 1,000 rows, so 25 pages leaves headroom
        # while still placing a hard bound on a malformed legacy endpoint.
        for _page_index in range(25):
            page = service.fetch_character_history(
                name,
                offset=remote_offset,
                limit=remote_limit,
                nest="",
            )
            if not isinstance(page, dict):
                raise RuntimeError("既往评分服务返回了无效分页数据")
            for value in page.get("nests") or ():
                remember_nest(value)

            page_items = [
                row for row in (page.get("items") or ()) if isinstance(row, dict)
            ]
            new_count = 0
            for row in page_items:
                identifier = str(row.get("historyId") or "").strip().casefold()
                if not identifier:
                    identifier = json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                if identifier in item_keys:
                    continue
                item_keys.add(identifier)
                items.append(row)
                remember_nest(row.get("nest"))
                new_count += 1

            try:
                page_total = max(0, int(page.get("total", len(items)) or 0))
            except (TypeError, ValueError):
                page_total = len(items)
            expected_total = max(expected_total or 0, page_total)

            if not page_items or len(items) >= expected_total:
                break
            if new_count == 0:
                raise RuntimeError("既往评分服务分页游标没有前进")
            remote_offset += len(page_items)
        else:
            if expected_total is not None and len(items) < expected_total:
                raise RuntimeError("既往评分记录超过本地分页安全上限")

        if expected_total is not None and len(items) < expected_total:
            raise RuntimeError("既往评分服务未返回完整记录")
        return {
            "total": len(items),
            "offset": 0,
            "limit": len(items),
            "nest": "",
            "nests": nests,
            "items": items,
        }

    @classmethod
    def _character_history_page(
        cls,
        complete: dict[str, Any],
        *,
        name: str,
        nest: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        all_items = [
            row for row in (complete.get("items") or ()) if isinstance(row, dict)
        ]
        nest_key = cls._history_nest_key(nest)
        filtered_items = (
            [
                row
                for row in all_items
                if cls._history_nest_key(row.get("nest")) == nest_key
            ]
            if nest_key
            else all_items
        )
        page_items = filtered_items[offset : offset + limit]
        return {
            "state": "ready",
            "message": "",
            "characterName": name,
            "total": len(filtered_items),
            "offset": offset,
            "limit": limit,
            "nest": nest,
            "nests": list(complete.get("nests") or ()),
            "items": json.loads(json.dumps(page_items, ensure_ascii=False)),
        }

    @classmethod
    def _complete_profession_history(
        cls,
        service: TeamScoreSupabaseSyncService,
        job: str,
    ) -> dict[str, Any]:
        remote_limit = 50
        remote_offset = 0
        expected_total: int | None = None
        items: list[dict[str, Any]] = []
        item_keys: set[str] = set()
        nests: list[str] = []
        nest_keys: set[str] = set()

        def remember_nest(value: object) -> None:
            nest_name = str(value or "").strip()
            nest_key = cls._history_nest_key(nest_name)
            if nest_name and nest_key not in nest_keys:
                nest_keys.add(nest_key)
                nests.append(nest_name)

        for _page_index in range(25):
            page = service.fetch_profession_history(
                job,
                offset=remote_offset,
                limit=remote_limit,
            )
            if not isinstance(page, dict):
                raise RuntimeError("职业评分服务返回了无效分页数据")
            for value in page.get("nests") or ():
                remember_nest(value)
            page_items = [
                row for row in (page.get("items") or ()) if isinstance(row, dict)
            ]
            new_count = 0
            for row in page_items:
                identifier = str(row.get("historyId") or "").strip().casefold()
                if not identifier:
                    identifier = json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                if identifier in item_keys:
                    continue
                item_keys.add(identifier)
                items.append(row)
                remember_nest(row.get("nest"))
                new_count += 1
            try:
                page_total = max(0, int(page.get("total", len(items)) or 0))
            except (TypeError, ValueError):
                page_total = len(items)
            expected_total = max(expected_total or 0, page_total)
            if not page_items or len(items) >= expected_total:
                break
            if new_count == 0:
                raise RuntimeError("职业评分服务分页游标没有前进")
            remote_offset += len(page_items)
        else:
            if expected_total is not None and len(items) < expected_total:
                raise RuntimeError("职业评分记录超过本地分页安全上限")
        if expected_total is not None and len(items) < expected_total:
            raise RuntimeError("职业评分服务未返回完整记录")
        return {
            "job": job,
            "total": len(items),
            "offset": 0,
            "limit": len(items),
            "nests": nests,
            "items": items,
        }

    def character_history(
        self,
        character_name: object,
        *,
        profile_id: object = "",
        limit: object = 20,
        nest: object = "",
        completion_status: object = "",
        score_schema_versions: object = (),
        cursor: object = "",
        include_facets: object = True,
        force_refresh: object = False,
        surface: object = "",
    ) -> dict[str, Any]:
        page_limit = max(1, min(50, int(limit or 20)))
        nest_name = str(nest or "").strip()[:2048]
        completion_filter = self._history_completion_filter(completion_status)
        schema_versions = self._history_schema_versions(score_schema_versions)
        page_cursor = str(cursor or "").strip()[:1024]
        facets = bool(include_facets)
        refresh = _setting_bool(force_refresh, False)
        query_surface = str(surface or "").strip().casefold()
        if query_surface not in {"", "overlay"}:
            raise ValueError("既往评分查询来源无效")
        overlay_query = query_surface == "overlay"
        raw_name = str(character_name or "").strip()
        parsed_profile_id = self._history_profile_id(profile_id)
        try:
            name = self._history_character_name(raw_name)
        except ValueError:
            if parsed_profile_id <= 0:
                raise
            name = self._resolve_profile_character_name(
                parsed_profile_id,
                force_refresh=refresh,
            )
        if overlay_query and not name and parsed_profile_id <= 0:
            return {
                "state": "unavailable",
                "message": "尚未取得角色名或近期会话身份，无法确认其战绩可见性",
                "characterName": name,
                "profileId": "",
                "total": 0 if facets else None,
                "limit": page_limit,
                "cursor": page_cursor,
                "nextCursor": "",
                "hasMore": False,
                "nest": nest_name,
                "nests": [],
                "scoreSchemaVersions": [],
                "items": [],
            }
        profile_cursor_match = re.fullmatch(r"profile-offset:(\d+)", page_cursor)
        profile_query = (
            parsed_profile_id > 0
            and not name
            and not completion_filter
            and not schema_versions
            and (not page_cursor or profile_cursor_match is not None)
        )
        cache_key = (
            "character-page-v3",
            parsed_profile_id,
            name.casefold(),
            nest_name.casefold(),
            completion_filter,
            schema_versions,
            page_cursor,
            page_limit,
            facets,
            query_surface,
        )
        # Overlay privacy is mutable. Never reuse the five-hour history cache
        # here, otherwise a newly hidden profile could remain visible to
        # another running client until that cache expires.
        if not overlay_query:
            if refresh:
                with self._state_lock:
                    self._history_cache.pop(cache_key, None)
            else:
                cached = self._history_cached(cache_key)
                if cached is not None:
                    return cached
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置",
                "characterName": name,
                "profileId": self._format_history_profile_id(parsed_profile_id),
                "total": 0 if facets else None,
                "limit": page_limit,
                "cursor": page_cursor,
                "nextCursor": "",
                "hasMore": False,
                "nest": nest_name,
                "nests": [],
                "scoreSchemaVersions": [],
                "items": [],
            }
        try:
            with self._history_request_lock:
                # Recheck after entering the single-flight lock. Concurrent
                # identical refreshes can share the result fetched first.
                payload = (
                    None if overlay_query else self._history_cached(cache_key)
                )
                if payload is None:
                    service = TeamScoreSupabaseSyncService(
                        config.endpoint, config.upload_token, config.installation_id
                    )
                    if profile_query:
                        profile_offset = (
                            int(profile_cursor_match.group(1))
                            if profile_cursor_match is not None else 0
                        )
                        try:
                            fetch_profile = (
                                service.fetch_profile_history_overlay
                                if overlay_query else service.fetch_profile_history
                            )
                            page = fetch_profile(
                                parsed_profile_id,
                                offset=profile_offset,
                                limit=page_limit,
                                nest=nest_name,
                            )
                        except Exception as profile_exc:
                            if overlay_query:
                                raise profile_exc
                            # A 2.9/older cloud function has no profile-history
                            # action.  If the stable ID has a verified name,
                            # continue through the legacy name-keyed index.
                            if not name:
                                raise profile_exc
                        else:
                            if overlay_query and page.get("hidden") is True:
                                payload = {
                                    "state": "hidden",
                                    "message": "该玩家已隐藏个人战绩",
                                    "characterName": name,
                                    "profileId": self._format_history_profile_id(parsed_profile_id),
                                    "total": 0 if facets else None,
                                    "limit": page_limit,
                                    "cursor": page_cursor,
                                    "nextCursor": "",
                                    "hasMore": False,
                                    "nest": nest_name,
                                    "nests": [],
                                    "scoreSchemaVersions": [],
                                    "items": [],
                                }
                                if not overlay_query:
                                    self._remember_history(cache_key, payload)
                                return payload
                            items = [
                                row for row in (page.get("items") or ())
                                if isinstance(row, dict)
                            ]
                            try:
                                profile_total = max(0, int(page.get("total") or 0))
                            except (TypeError, ValueError):
                                profile_total = len(items)
                            resolved_name = name or next((
                                str(row.get("characterName") or "").strip()
                                for row in items
                                if str(row.get("characterName") or "").strip()
                            ), "")
                            if resolved_name:
                                try:
                                    resolved_name = self._history_character_name(resolved_name)
                                except ValueError:
                                    resolved_name = ""
                            if resolved_name and parsed_profile_id > 0:
                                self._remember_history(
                                    ("profile-character-name", parsed_profile_id),
                                    {"characterName": resolved_name},
                                )
                            # This compatibility query resolves a recent
                            # profile alias to the name-keyed history index.
                            if profile_total > 0 or not name:
                                next_offset = profile_offset + len(items)
                                has_more = next_offset < profile_total
                                versions = sorted({
                                    int(
                                        row.get("score_schema_version")
                                        or row.get("scoreSchemaVersion")
                                    )
                                    for row in items
                                    if str(
                                        row.get("score_schema_version")
                                        or row.get("scoreSchemaVersion")
                                        or ""
                                    ).isdigit()
                                })
                                payload = {
                                    "state": "ready" if items else "empty",
                                    "message": "" if items else "该档案暂无符合条件的既往评分",
                                    "characterName": resolved_name,
                                    "profileId": self._format_history_profile_id(parsed_profile_id),
                                    "total": profile_total if facets else None,
                                    "limit": page_limit,
                                    "cursor": (
                                        f"profile-offset:{profile_offset}"
                                        if profile_offset else ""
                                    ),
                                    "nextCursor": (
                                        f"profile-offset:{next_offset}" if has_more else ""
                                    ),
                                    "hasMore": has_more,
                                    "nest": nest_name,
                                    "nests": list(page.get("nests") or ()) if facets else [],
                                    "scoreSchemaVersions": (
                                        list(page.get("scoreSchemaVersions") or versions)
                                        if facets else []
                                    ),
                                    "items": items,
                                }
                                if not overlay_query:
                                    self._remember_history(cache_key, payload)
                                return payload
                    if not name:
                        return {
                            "state": "unavailable",
                            "message": (
                                "已取得会话档案 ID，但它已过期或对应多个角色，"
                                "当前没有可安全使用的角色名"
                            ),
                            "characterName": "",
                            "profileId": self._format_history_profile_id(parsed_profile_id),
                            "total": 0 if facets else None,
                            "limit": page_limit,
                            "cursor": page_cursor,
                            "nextCursor": "",
                            "hasMore": False,
                            "nest": nest_name,
                            "nests": [],
                            "scoreSchemaVersions": [],
                            "items": [],
                        }
                    if overlay_query:
                        overlay_offset = (
                            int(profile_cursor_match.group(1))
                            if profile_cursor_match is not None else 0
                        )
                        page = service.fetch_character_history_overlay(
                            name,
                            offset=overlay_offset,
                            limit=page_limit,
                            nest=nest_name,
                        )
                        if page.get("hidden") is True:
                            return {
                                "state": "hidden",
                                "message": "该玩家已隐藏个人战绩",
                                "characterName": name,
                                "profileId": self._format_history_profile_id(parsed_profile_id),
                                "total": 0 if facets else None,
                                "limit": page_limit,
                                "cursor": page_cursor,
                                "nextCursor": "",
                                "hasMore": False,
                                "nest": nest_name,
                                "nests": [],
                                "scoreSchemaVersions": [],
                                "items": [],
                            }
                        items = [
                            row for row in (page.get("items") or ())
                            if isinstance(row, dict)
                        ]
                        total = max(0, int(page.get("total") or 0))
                        next_offset = overlay_offset + len(items)
                        has_more = next_offset < total
                        return {
                            "state": "ready" if items else "empty",
                            "message": "" if items else "该角色暂无符合条件的既往评分",
                            "characterName": name,
                            "profileId": self._format_history_profile_id(parsed_profile_id),
                            "total": total if facets else None,
                            "limit": page_limit,
                            "cursor": page_cursor,
                            "nextCursor": (
                                f"profile-offset:{next_offset}" if has_more else ""
                            ),
                            "hasMore": has_more,
                            "nest": nest_name,
                            "nests": list(page.get("nests") or ()) if facets else [],
                            "scoreSchemaVersions": (
                                list(page.get("scoreSchemaVersions") or ()) if facets else []
                            ),
                            "items": items,
                        }
                    page = service.fetch_score_history_page(
                        character_name=name,
                        nest=nest_name,
                        completion_status=completion_filter,
                        score_schema_versions=schema_versions,
                        cursor=page_cursor,
                        limit=page_limit,
                        include_facets=facets,
                    )
                    items = [
                        row for row in (page.get("items") or ())
                        if isinstance(row, dict)
                    ]
                    payload = {
                        "state": "ready" if items else "empty",
                        "message": "" if items else "该角色暂无符合条件的既往评分",
                        "characterName": name,
                        "profileId": self._format_history_profile_id(parsed_profile_id),
                        "total": page.get("total"),
                        "limit": page_limit,
                        "cursor": str(page.get("cursor") or page_cursor),
                        "nextCursor": str(page.get("nextCursor") or ""),
                        "hasMore": bool(page.get("hasMore")),
                        "nest": nest_name,
                        "nests": list(page.get("nests") or ()),
                        "scoreSchemaVersions": list(page.get("scoreSchemaVersions") or ()),
                        "facetsDeferred": bool(page.get("facetsDeferred")),
                        "items": items,
                    }
                    if not overlay_query:
                        self._remember_history(cache_key, payload)
            return payload
        except Exception as exc:
            message = str(exc)
            if overlay_query and (
                "unsupported_action" in message
                or "character_history_overlay" in message
                or "get_character_score_history_overlay" in message
                or "profile_history_overlay" in message
                or "get_profile_score_history_overlay" in message
            ):
                message = (
                    "云端角色隐私服务尚未部署，请执行数据库 033 并部署新版云函数"
                )
            elif (
                profile_query
                and not name
                and (
                    "unsupported_action" in message
                    or "get_profile_score_history" in message
                )
            ):
                message = "云端档案评分服务尚未部署，请执行数据库 026 并部署 2.10.0 云函数"
            elif "unsupported_action" in message or "get_score_history_page" in message:
                message = "云端既往评分服务版本过旧，请执行数据库 012 及后续迁移并部署新版云函数"
            payload = {
                "state": "error",
                "message": message,
                "characterName": name,
                "profileId": self._format_history_profile_id(parsed_profile_id),
                "total": 0 if facets else None,
                "limit": page_limit,
                "cursor": page_cursor,
                "nextCursor": "",
                "hasMore": False,
                "nest": nest_name,
                "nests": [],
                "scoreSchemaVersions": [],
                "items": [],
            }
        return payload

    def set_profile_overlay_visibility(
        self,
        profile_id: object,
        hidden: object,
        *,
        character_name: object = "",
        force_refresh: object = False,
    ) -> dict[str, Any]:
        parsed_profile_id = self._history_profile_id(profile_id)
        if parsed_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        hidden_value = _setting_bool(hidden, False)
        name = str(character_name or "").strip()[:64]
        if not name:
            name = self._resolve_profile_character_name(
                parsed_profile_id,
                force_refresh=_setting_bool(force_refresh, False),
            )
        if not name:
            raise ValueError(
                "当前 profileId 已过期或对应多个角色，无法安全保存隐私设置"
            )
        sync_key = (name.casefold(), hidden_value)
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置，个人战绩可见性暂未同步",
                "profileId": self._format_history_profile_id(parsed_profile_id),
                "characterName": name,
                "hidden": hidden_value,
            }
        with self._overlay_visibility_lock:
            if not _setting_bool(force_refresh, False):
                with self._state_lock:
                    if self._overlay_visibility_synced == sync_key:
                        return {
                            "state": "synced",
                            "message": "个人战绩可见性已同步",
                            "profileId": self._format_history_profile_id(parsed_profile_id),
                            "characterName": name,
                            "hidden": hidden_value,
                        }
            service = TeamScoreSupabaseSyncService(
                config.endpoint, config.upload_token, config.installation_id
            )
            visibility = service.set_profile_score_overlay_visibility(
                parsed_profile_id,
                hidden_value,
                character_name=name,
            )
            with self._state_lock:
                self._overlay_visibility_synced = sync_key
            self._invalidate_history_cache()
        return {
            "state": "synced",
            "message": "个人战绩可见性已同步",
            "profileId": self._format_history_profile_id(parsed_profile_id),
            "characterName": name,
            "hidden": bool(visibility.get("hidden", hidden_value)),
        }

    def set_profile_score_visibility(
        self,
        profile_id: object,
        overlay_hidden: object,
        software_hidden: object,
        *,
        character_name: object = "",
        force_refresh: object = False,
    ) -> dict[str, Any]:
        parsed_profile_id = self._history_profile_id(profile_id)
        if parsed_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        overlay_value = _setting_bool(overlay_hidden, False)
        software_value = _setting_bool(software_hidden, False)
        name = str(character_name or "").strip()[:64]
        if not name:
            name = self._resolve_profile_character_name(
                parsed_profile_id,
                force_refresh=_setting_bool(force_refresh, False),
            )
        if not name:
            raise ValueError(
                "当前 profileId 已过期或对应多个角色，无法安全保存隐私设置"
            )
        sync_key = (
            name.casefold(),
            overlay_value,
            software_value,
        )
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置，个人战绩隐私设置暂未同步",
                "profileId": self._format_history_profile_id(parsed_profile_id),
                "characterName": name,
                "overlayHidden": overlay_value,
                "softwareHidden": software_value,
            }
        with self._overlay_visibility_lock:
            if not _setting_bool(force_refresh, False):
                with self._state_lock:
                    if self._score_visibility_synced == sync_key:
                        return {
                            "state": "synced",
                            "message": "个人战绩隐私设置已同步",
                            "profileId": self._format_history_profile_id(parsed_profile_id),
                            "characterName": name,
                            "overlayHidden": overlay_value,
                            "softwareHidden": software_value,
                        }
            service = TeamScoreSupabaseSyncService(
                config.endpoint, config.upload_token, config.installation_id
            )
            visibility = service.set_character_score_visibility(
                name,
                overlay_value,
                software_value,
            )
            with self._state_lock:
                self._score_visibility_synced = sync_key
            self._invalidate_history_cache()
        return {
            "state": "synced",
            "message": "个人战绩隐私设置已同步",
            "profileId": self._format_history_profile_id(parsed_profile_id),
            "characterName": name,
            "overlayHidden": bool(
                visibility.get("overlayHidden", overlay_value)
            ),
            "softwareHidden": bool(
                visibility.get("softwareHidden", software_value)
            ),
        }

    @staticmethod
    def _history_profile_id(value: object) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if text.casefold().startswith("profile:"):
            text = text.split(":", 1)[1].strip()
        try:
            parsed = int(text, 16 if text.casefold().startswith("0x") else 10)
        except (TypeError, ValueError):
            return 0
        return parsed if 0 < parsed <= 0x7FFFFFFFFFFFFFFF else 0

    @staticmethod
    def _format_history_profile_id(profile_id: int) -> str:
        return f"0x{profile_id:08X}" if profile_id > 0 else ""

    def _resolve_profile_character_name(
        self,
        profile_id: int,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Resolve history identity without requiring the encrypted body.

        Profile-ID bindings are deliberately stored by the independent TCP
        identity channel.  Query that shared cache only when the inspect event
        did not already carry a verified name, then reuse the ordinary
        name-keyed score-history API so existing history rows remain valid.
        """

        if profile_id <= 0:
            return ""
        config = load_v410_score_sync_config()
        if not config.ready:
            return ""
        try:
            with self._history_request_lock:
                service = TeamScoreSupabaseSyncService(
                    config.endpoint, config.upload_token, config.installation_id
                )
                bindings = service.fetch_player_identity_profiles((profile_id,))
                names = {
                    str(binding.character_name or "").strip()
                    for binding in bindings
                    if int(binding.profile_id or 0) == profile_id
                    and str(binding.character_name or "").strip()
                }
                if len(names) != 1:
                    return ""
                name = self._history_character_name(next(iter(names)))
                return name
        except Exception:
            # The history panel remains open with the stable ID and retries on
            # refresh or when the asynchronous identity cache publishes a name.
            return ""

    def character_history_detail(
        self,
        character_name: object,
        history_id: object,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        name = self._history_character_name(character_name)
        identifier = str(history_id or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}:[0-9]+", identifier):
            raise ValueError("既往评分标识无效")
        cache_key = ("detail", name.casefold(), identifier.casefold())
        if force_refresh:
            with self._state_lock:
                self._history_cache.pop(cache_key, None)
        else:
            cached = self._history_cached(cache_key)
            if cached is not None:
                return cached
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置",
                "characterName": name,
                "detail": None,
            }
        try:
            with self._history_request_lock:
                payload = self._history_cached(cache_key)
                if payload is None:
                    service = TeamScoreSupabaseSyncService(
                        config.endpoint, config.upload_token, config.installation_id
                    )
                    detail = service.fetch_character_history_detail(name, identifier)
                    payload = {
                        "state": "ready" if detail is not None else "empty",
                        "message": "" if detail is not None else "该评分明细不存在",
                        "characterName": name,
                        "detail": detail,
                    }
                    self._remember_history(cache_key, payload)
            return payload
        except Exception as exc:
            return {
                "state": "error",
                "message": str(exc),
                "characterName": name,
                "detail": None,
            }

    def profession_history(
        self,
        job: object,
        *,
        jobs: object = (),
        nest: object = "",
        minimum_score: object = None,
        maximum_score: object = None,
        recorded_from: object = "",
        recorded_to: object = "",
        character_search: object = "",
        completion_status: object = "",
        score_schema_versions: object = (),
        sort_key: object = "recordedAt",
        sort_direction: object = "desc",
        cursor: object = "",
        limit: object = 20,
        include_facets: object = True,
        force_refresh: object = False,
    ) -> dict[str, Any]:
        job_name = self._history_job_name(job)
        requested_jobs = jobs if isinstance(jobs, (list, tuple, set)) else ()
        job_names = tuple(dict.fromkeys(
            self._history_job_name(value) for value in requested_jobs
        )) or (job_name,)
        if len(job_names) > 128:
            raise ValueError("职业查询范围过大")
        nest_name = str(nest or "").strip()[:2048]
        character_filter = str(character_search or "").strip()[:64]
        completion_filter = self._history_completion_filter(completion_status)
        schema_versions = self._history_schema_versions(score_schema_versions)
        order_key = str(sort_key or "recordedAt").strip()
        order_direction = str(sort_direction or "desc").strip().lower()
        page_cursor = str(cursor or "").strip()[:1024]
        page_limit = max(1, min(50, int(limit or 20)))
        facets = bool(include_facets)
        refresh = _setting_bool(force_refresh, False)

        def optional_number(value: object) -> float | None:
            if value is None or str(value).strip() == "":
                return None
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("评分筛选值无效")
            return parsed

        min_score = optional_number(minimum_score)
        max_score = optional_number(maximum_score)
        if min_score is not None and max_score is not None and min_score > max_score:
            raise ValueError("总分下限不能高于上限")
        from_time = str(recorded_from or "").strip()[:64]
        to_time = str(recorded_to or "").strip()[:64]
        cache_key = (
            "profession-page-v2",
            job_name.casefold(),
            tuple(value.casefold() for value in job_names),
            nest_name.casefold(),
            min_score,
            max_score,
            from_time,
            to_time,
            character_filter.casefold(),
            completion_filter,
            schema_versions,
            order_key,
            order_direction,
            page_cursor,
            page_limit,
            facets,
        )
        if refresh:
            with self._state_lock:
                self._history_cache.pop(cache_key, None)
        else:
            cached = self._history_cached(cache_key)
            if cached is not None:
                return cached
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置",
                "job": job_name,
                "total": 0 if facets else None,
                "limit": page_limit,
                "cursor": page_cursor,
                "nextCursor": "",
                "hasMore": False,
                "nests": [],
                "scoreSchemaVersions": [],
                "items": [],
            }
        try:
            with self._history_request_lock:
                # A second check closes the race between the first cache miss
                # and another request completing its CloudBase fetch.
                payload = self._history_cached(cache_key)
                if payload is None:
                    service = TeamScoreSupabaseSyncService(
                        config.endpoint, config.upload_token, config.installation_id
                    )
                    page = service.fetch_score_history_page(
                        jobs=job_names,
                        nest=nest_name,
                        minimum_score=min_score,
                        maximum_score=max_score,
                        recorded_from=from_time,
                        recorded_to=to_time,
                        character_search=character_filter,
                        completion_status=completion_filter,
                        score_schema_versions=schema_versions,
                        sort_key=order_key,
                        sort_direction=order_direction,
                        cursor=page_cursor,
                        limit=page_limit,
                        include_facets=facets,
                    )
                    items = [
                        row for row in (page.get("items") or ())
                        if isinstance(row, dict)
                    ]
                    payload = {
                        "state": "ready" if items else "empty",
                        "message": "" if items else f"{job_name}暂无符合条件的既往评分",
                        "job": job_name,
                        "total": page.get("total"),
                        "limit": page_limit,
                        "cursor": str(page.get("cursor") or page_cursor),
                        "nextCursor": str(page.get("nextCursor") or ""),
                        "hasMore": bool(page.get("hasMore")),
                        "nests": list(page.get("nests") or ()),
                        "scoreSchemaVersions": list(page.get("scoreSchemaVersions") or ()),
                        "facetsDeferred": bool(page.get("facetsDeferred")),
                        "items": items,
                    }
                    self._remember_history(cache_key, payload)
            return payload
        except Exception as exc:
            message = str(exc)
            if (
                order_key == "totalScoreWithoutSurvival"
                and "invalid history sort key" in message.lower()
            ):
                message = "云端数据库尚未部署既往评分双总分排序迁移 030"
            elif "unsupported_action" in message or "get_score_history_page" in message:
                message = "云端既往评分服务版本过旧，请执行数据库 012 及后续迁移并部署新版云函数"
            return {
                "state": "error",
                "message": message,
                "job": job_name,
                "total": 0 if facets else None,
                "limit": page_limit,
                "cursor": page_cursor,
                "nextCursor": "",
                "hasMore": False,
                "nests": [],
                "scoreSchemaVersions": [],
                "items": [],
            }

    def profession_history_detail(
        self,
        job: object,
        history_id: object,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        job_name = self._history_job_name(job)
        identifier = str(history_id or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}:[0-9]+", identifier):
            raise ValueError("既往评分标识无效")
        cache_key = ("profession-detail", job_name.casefold(), identifier.casefold())
        if force_refresh:
            with self._state_lock:
                self._history_cache.pop(cache_key, None)
        else:
            cached = self._history_cached(cache_key)
            if cached is not None:
                return cached
        config = load_v410_score_sync_config()
        if not config.ready:
            return {
                "state": "unavailable",
                "message": "云端评分服务尚未配置",
                "job": job_name,
                "detail": None,
            }
        try:
            with self._history_request_lock:
                payload = self._history_cached(cache_key)
                if payload is None:
                    service = TeamScoreSupabaseSyncService(
                        config.endpoint, config.upload_token, config.installation_id
                    )
                    detail = service.fetch_profession_history_detail(
                        job_name, identifier
                    )
                    payload = {
                        "state": "ready" if detail is not None else "empty",
                        "message": "" if detail is not None else "该评分明细不存在",
                        "job": job_name,
                        "detail": detail,
                    }
                    self._remember_history(cache_key, payload)
            return payload
        except Exception as exc:
            message = str(exc)
            if "unsupported_action" in message:
                message = "云端评分服务版本过旧，请部署职业历史查询更新包"
            return {
                "state": "error",
                "message": message,
                "job": job_name,
                "detail": None,
            }

    def enqueue(
        self,
        capture_path: Path,
        boss_names: Iterable[str],
        target_ids: Iterable[str],
        rows: Iterable[TeamScoreExportRow],
        *,
        map_ids: Iterable[object] = (),
        map_names: Iterable[object] = (),
        force_upload: bool = False,
        expected_capture_hash: str = "",
    ) -> None:
        path = Path(capture_path)
        score_rows = tuple(rows)
        if not path.is_file() or not score_rows:
            return
        self._jobs.put(
            V410ScoreSyncJob(
                capture_path=path,
                boss_names=tuple(str(value) for value in boss_names if str(value)),
                target_ids=tuple(str(value) for value in target_ids if str(value)),
                map_ids=tuple(
                    parsed
                    for value in map_ids
                    if (parsed := _positive_int(value)) > 0
                ),
                map_names=tuple(
                    str(value).strip()
                    for value in map_names
                    if str(value).strip()
                ),
                rows=score_rows,
                force_upload=bool(force_upload),
                expected_capture_hash=str(expected_capture_hash or "").strip(),
            )
        )
        with self._state_lock:
            self._message = (
                "重新评分结果已进入覆盖上传队列"
                if force_upload
                else "评分已进入本地上传队列"
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._state_lock:
                self._running = True
                self._message = "正在保存并上传全队评分..."
            try:
                if job is None:
                    self._flush_pending()
                else:
                    self._process(job)
            except Exception as exc:
                with self._state_lock:
                    self._last_ok = False
                    self._message = f"上传失败：{exc}"
            finally:
                with self._state_lock:
                    self._running = False
                self._jobs.task_done()

    def _flush_pending(self) -> None:
        config = load_v410_score_sync_config()
        if not config.ready:
            return
        with self._operation_lock:
            service = TeamScoreSupabaseSyncService(
                config.endpoint,
                config.upload_token,
                config.installation_id,
            )
            with TeamScoreSyncStore() as store:
                result = service.sync_store(
                    store,
                    upload_capture_files=config.capture_file_upload_enabled,
                )
        if result.uploaded_captures:
            self._invalidate_history_cache()
        with self._state_lock:
            self._last_ok = not result.failed_captures and not result.capture_file_error
            self._message = result.summary()

    def close(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)

    def _process(self, job: V410ScoreSyncJob) -> None:
        expected_hash = str(job.expected_capture_hash or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            actual_hash = capture_content_hash(job.capture_path).casefold()
            if actual_hash != expected_hash:
                # Never persist score rows against bytes from another run. The
                # saved trace may change if late evidence is appended or a path
                # is unexpectedly reused; rescoring the final file is safe.
                raise ValueError(
                    "抓包文件在评分后发生变化，本次旧评分未上传；请重新评分"
                )
        config = load_v410_score_sync_config()
        nest = resolve_score_nest(
            job.boss_names,
            capture_path=job.capture_path,
            target_ids=job.target_ids,
            map_ids=job.map_ids,
            map_names=job.map_names,
        )
        with self._operation_lock:
            with TeamScoreSyncStore() as store:
                _capture_hash, duplicate, stored_rows = store.record_capture(
                    job.capture_path,
                    nest,
                    job.rows,
                    force_supabase_sync=job.force_upload,
                )
                if not config.ready:
                    suffix = "该记录已存在" if duplicate else f"已保存 {stored_rows} 名队员"
                    with self._state_lock:
                        self._last_ok = True
                        self._message = f"评分已保存到本地离线队列；{suffix}"
                    return
                service = TeamScoreSupabaseSyncService(
                    config.endpoint,
                    config.upload_token,
                    config.installation_id,
                )
                result = service.sync_store(
                    store,
                    upload_capture_files=config.capture_file_upload_enabled,
                )
        if result.uploaded_captures:
            self._invalidate_history_cache()
        with self._state_lock:
            self._last_ok = not result.failed_captures and not result.capture_file_error
            self._message = result.summary()
