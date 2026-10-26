from __future__ import annotations

import hashlib
import gzip
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


BENCHMARK_CATALOG_SCHEMA = "dragon-nest-team-score-benchmarks-v1"
BENCHMARK_MANIFEST_SCHEMA = "dragon-nest-team-score-benchmark-manifest-v1"
BENCHMARK_CACHE_SCHEMA = "dragon-nest-team-score-benchmark-cache-v1"
BENCHMARK_CACHE_TTL_SECONDS = 5 * 60 * 60

_BUNDLED_DIRECTORY = (
    Path("engine")
    / "data"
    / "modular"
    / "team_score_benchmarks"
)


def _application_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def default_benchmark_cache_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "team_score_benchmarks"
        / "catalog-cache.json"
    )


def _json_object(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("职业均值静态文件必须是 JSON 对象")
    return payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _catalog_object(raw: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    if str(manifest.get("compression") or "").strip().casefold() == "gzip":
        raw = gzip.decompress(raw)
    return _json_object(raw)


class VersionedTeamScoreBenchmarkCatalog:
    """Versioned static benchmark catalog with a persistent five-hour cache."""

    def __init__(
        self,
        manifest_url: str = "",
        *,
        cache_ttl_seconds: float = BENCHMARK_CACHE_TTL_SECONDS,
        cache_path: Path | None = None,
        bundled_directory: Path | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.manifest_url = str(manifest_url or "").strip()
        self.cache_ttl_seconds = max(60.0, float(cache_ttl_seconds))
        self.cache_path = Path(cache_path or default_benchmark_cache_path())
        self.bundled_directory = Path(
            bundled_directory or (_application_root() / _BUNDLED_DIRECTORY)
        )
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._catalog: dict[str, Any] = {}
        self._index: dict[tuple[str, str], dict[str, Any]] = {}
        self._checked_at = 0.0
        self._source = "unavailable"
        self._last_error = ""

    def prime(self) -> None:
        """Load local data immediately; network refresh remains background-only."""
        cached = self._read_cache()
        bundled = self._read_bundled()
        if (
            bundled is not None
            and cached is not None
            and self._version_key(bundled) > self._version_key(cached["catalog"])
        ):
            self._apply_catalog(bundled, checked_at=0.0, source="bundled")
            return
        if cached is not None:
            self._apply_catalog(
                cached["catalog"],
                checked_at=float(cached.get("checked_at") or 0.0),
                source=str(cached.get("source") or "disk-cache"),
            )
            return
        if bundled is not None:
            # Zero means the first startup may check the remote static manifest,
            # while every later startup can reuse the five-hour disk cache.
            self._apply_catalog(bundled, checked_at=0.0, source="bundled")

    def refresh_due(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else float(now)
        with self._lock:
            return moment - self._checked_at >= self.cache_ttl_seconds

    def refresh(self, *, force: bool = False) -> bool:
        """Refresh from static HTTP files without calling PostgreSQL."""
        now = time.time()
        if not force and not self.refresh_due(now):
            return False
        try:
            if not self.manifest_url:
                raise ValueError("未配置职业均值静态清单地址")
            manifest_raw = self._fetch(self.manifest_url)
            manifest = _json_object(manifest_raw)
            if manifest.get("schema") != BENCHMARK_MANIFEST_SCHEMA:
                raise ValueError("职业均值静态清单版本不受支持")
            data_url = str(manifest.get("data_url") or "").strip()
            if not data_url:
                data_url = urllib.parse.urljoin(
                    self.manifest_url,
                    str(manifest.get("file") or "").strip(),
                )
            if not data_url:
                raise ValueError("职业均值静态清单缺少数据文件地址")
            catalog_raw = self._fetch(data_url)
            expected_hash = str(manifest.get("sha256") or "").strip().casefold()
            if expected_hash and _sha256(catalog_raw) != expected_hash:
                raise ValueError("职业均值静态文件校验失败")
            catalog = _catalog_object(catalog_raw, manifest)
            self._validate_catalog(catalog, manifest)
            with self._lock:
                current = dict(self._catalog)
            if current and self._version_key(catalog) < self._version_key(current):
                with self._lock:
                    self._checked_at = now
                    source = self._source
                self._write_cache(now, source, current)
                return False
            self._apply_catalog(catalog, checked_at=now, source="remote-static")
            self._write_cache(now, "remote-static", catalog)
            return True
        except Exception as exc:
            with self._lock:
                self._checked_at = now
                self._last_error = str(exc)
                catalog = dict(self._catalog)
                source = self._source
            if catalog:
                self._write_cache(now, source, catalog)
            return False

    def lookup(self, nest: str, job: str) -> dict[str, Any] | None:
        key = (str(nest or "").strip(), str(job or "").strip())
        with self._lock:
            row = self._index.get(key)
            return dict(row) if row is not None else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": str(self._catalog.get("version") or ""),
                "scoreSchemaVersion": int(
                    self._catalog.get("score_schema_version") or 0
                ),
                "source": self._source,
                "rowCount": len(self._index),
                "checkedAt": self._checked_at,
                "cacheTtlSeconds": self.cache_ttl_seconds,
                "lastError": self._last_error,
            }

    def _fetch(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "DragonNestSimulator-BenchmarkCatalog/1",
            },
        )
        with self._opener(request, timeout=8.0) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status < 200 or status >= 300:
                raise OSError(f"职业均值静态文件返回 HTTP {status}")
            return response.read()

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            payload = _json_object(self.cache_path.read_bytes())
            if payload.get("schema") != BENCHMARK_CACHE_SCHEMA:
                return None
            catalog = payload.get("catalog")
            if not isinstance(catalog, dict):
                return None
            self._validate_catalog(catalog)
            return payload
        except (OSError, ValueError, TypeError):
            return None

    def _read_bundled(self) -> dict[str, Any] | None:
        try:
            manifest = _json_object(
                (self.bundled_directory / "manifest.json").read_bytes()
            )
            if manifest.get("schema") != BENCHMARK_MANIFEST_SCHEMA:
                return None
            filename = str(manifest.get("file") or "").strip()
            if not filename:
                return None
            raw = (self.bundled_directory / filename).read_bytes()
            expected_hash = str(manifest.get("sha256") or "").strip().casefold()
            if expected_hash and _sha256(raw) != expected_hash:
                return None
            catalog = _catalog_object(raw, manifest)
            self._validate_catalog(catalog, manifest)
            return catalog
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _validate_catalog(
        catalog: dict[str, Any], manifest: dict[str, Any] | None = None
    ) -> None:
        if catalog.get("schema") != BENCHMARK_CATALOG_SCHEMA:
            raise ValueError("职业均值静态数据版本不受支持")
        if not isinstance(catalog.get("benchmarks"), list):
            raise ValueError("职业均值静态数据缺少 benchmarks")
        if manifest is not None and str(manifest.get("version") or "") != str(
            catalog.get("version") or ""
        ):
            raise ValueError("职业均值静态清单与数据版本不一致")

    @staticmethod
    def _version_key(catalog: dict[str, Any]) -> tuple[int, str, str]:
        return (
            int(catalog.get("score_schema_version") or 0),
            str(catalog.get("generated_at") or ""),
            str(catalog.get("version") or ""),
        )

    def _apply_catalog(
        self,
        catalog: dict[str, Any],
        *,
        checked_at: float,
        source: str,
    ) -> None:
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for row in catalog.get("benchmarks", []):
            if not isinstance(row, dict):
                continue
            nest = str(row.get("nest") or "").strip()
            job = str(row.get("job") or "").strip()
            if nest and job:
                index[(nest, job)] = dict(row)
        with self._lock:
            self._catalog = dict(catalog)
            self._index = index
            self._checked_at = max(0.0, float(checked_at))
            self._source = str(source or "unknown")
            self._last_error = ""

    def _write_cache(
        self, checked_at: float, source: str, catalog: dict[str, Any]
    ) -> None:
        payload = {
            "schema": BENCHMARK_CACHE_SCHEMA,
            "checked_at": float(checked_at),
            "source": str(source),
            "catalog": catalog,
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(
            f"{self.cache_path.suffix}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)
