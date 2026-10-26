from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .identity_cache import CachedPlayerIdentity

from .team_score_sync import (
    CURRENT_SCORE_SCHEMA_VERSION,
    CURRENT_SUPABASE_SYNC_SCHEMA_VERSION,
    StoredCaptureFile,
    TeamScoreSyncStore,
    capture_content_hash,
)


SUPABASE_FUNCTION_NAME = "team-score-sync"
SUPABASE_SYNC_SCHEMA_VERSION = CURRENT_SUPABASE_SYNC_SCHEMA_VERSION
SCORE_SCHEMA_VERSION = CURRENT_SCORE_SCHEMA_VERSION


class SupabaseScoreSyncError(RuntimeError):
    pass


def _is_statement_timeout(error: BaseException) -> bool:
    message = str(error or "").casefold()
    return any(fragment in message for fragment in (
        "canceling statement due to statement timeout",
        "statement timeout",
        "query canceled",
    ))


def _http_error_message(code: int, detail: str, endpoint: str) -> str:
    text = str(detail or "").strip()
    lowered = text.casefold()
    if code == 404 and (
        "requested function was not found" in lowered
        or "not_found" in lowered
    ):
        return (
            f'未找到评分同步服务 "{SUPABASE_FUNCTION_NAME}"。'
            f"当前请求地址：{endpoint}。"
            "请确认 CloudBase HTTP 网关路由已启用，并且指向同名 HTTP 云函数。"
        )
    if code in {401, 403}:
        return (
            f"评分同步服务拒绝访问（HTTP {code}）：{text[:300]}。"
            "模拟器这里需要填写部署脚本生成的 TEAM_SCORE_UPLOAD_TOKEN，"
            "不是数据库 API Key。"
        )
    return f"评分同步服务返回 HTTP {code}：{text[:500]}"


@dataclass(frozen=True, slots=True)
class SupabaseSyncResult:
    uploaded_captures: int
    added_rows: int
    skipped_rows: int
    deleted_rows: int
    remaining_captures: int
    updated_rows: int = 0
    skill_detail_rows: int = 0
    support_detail_rows: int = 0
    character_identity_rows: int = 0
    extended_detail_rows: int = 0
    uploaded_capture_files: int = 0
    remaining_capture_files: int = 0
    capture_file_error: str = ""
    failed_captures: int = 0
    score_upload_error: str = ""

    def summary(self) -> str:
        file_summary = (
            f"；原始抓包已上传 {self.uploaded_capture_files} 份，"
            f"待上传 {self.remaining_capture_files} 份"
        )
        if self.capture_file_error:
            file_summary += f"（最近错误：{self.capture_file_error[:120]}）"
        return (
            f"云端已处理 {self.uploaded_captures} 份记录，"
            f"新增 {self.added_rows} 条，更新 {self.updated_rows} 条，"
            f"技能明细 {self.skill_detail_rows} 条，辅助明细 {self.support_detail_rows} 条，"
            f"角色身份 {self.character_identity_rows} 条，扩展明细 {self.extended_detail_rows} 条，"
            f"清理旧记录 {self.deleted_rows} 条；"
            f"本地待上传 {self.remaining_captures} 份{file_summary}"
            + (f"；{self.failed_captures} 份评分失败并保留待重试：{self.score_upload_error[:160]}" if self.failed_captures else "")
        )


def normalize_supabase_function_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SupabaseScoreSyncError("评分同步地址必须是完整的 http(s) URL")
    if "/functions/v1/" in parsed.path:
        return text
    if parsed.path.rstrip("/") in {"", "/"} and parsed.netloc.casefold().endswith(
        ".supabase.co"
    ):
        return f"{text}/functions/v1/{SUPABASE_FUNCTION_NAME}"
    if parsed.path.rstrip("/") in {"", "/"} and ".app.tcloudbase.com" in parsed.netloc.casefold():
        return f"{text}/{SUPABASE_FUNCTION_NAME}"
    if parsed.path.rstrip("/") not in {"", "/"}:
        return text
    raise SupabaseScoreSyncError("请填写 CloudBase 网关地址或完整的评分同步服务地址")


class TeamScoreSupabaseSyncService:
    def __init__(
        self,
        endpoint: str,
        upload_token: str,
        installation_id: str,
        *,
        timeout_seconds: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = normalize_supabase_function_url(endpoint)
        self.upload_token = str(upload_token or "").strip()
        self.installation_id = str(installation_id or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._opener = opener or urllib.request.urlopen
        if not self.endpoint:
            raise SupabaseScoreSyncError("尚未填写评分同步服务地址")
        if not self.upload_token:
            raise SupabaseScoreSyncError("尚未填写评分上传密钥")
        if not self.installation_id:
            raise SupabaseScoreSyncError("本机安装标识尚未生成")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body_bytes) > 4 * 1024 * 1024 and isinstance(payload.get("captures"), list):
            import base64
            import zlib
            if len(body_bytes) > 32 * 1024 * 1024:
                raise SupabaseScoreSyncError("评分上传内容超过 32 MiB；本地评分已保留")
            body_bytes = json.dumps({"encoding": "score-upload-deflate-v1", "data":
                base64.b64encode(zlib.compress(body_bytes, 6)).decode("ascii")}, separators=(",", ":")).encode("utf-8")
            if len(body_bytes) > 8 * 1024 * 1024:
                raise SupabaseScoreSyncError("评分压缩上传内容超过 8 MiB；本地评分已保留")
        request = urllib.request.Request(
            self.endpoint,
            data=body_bytes,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "DragonNestSimulator-TeamScoreSync/1",
                "X-Upload-Token": self.upload_token,
                "X-Client-Install-Id": self.installation_id,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", response.getcode()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = body.strip() or str(exc.reason)
            try:
                parsed = json.loads(body)
                detail = str(parsed.get("error") or parsed.get("message") or detail)
            except (ValueError, AttributeError):
                pass
            raise SupabaseScoreSyncError(
                _http_error_message(exc.code, detail, self.endpoint)
            ) from exc
        except urllib.error.URLError as exc:
            raise SupabaseScoreSyncError(
                f"无法连接评分同步服务：{exc.reason}"
            ) from exc
        except OSError as exc:
            raise SupabaseScoreSyncError(f"评分同步请求失败：{exc}") from exc
        if status < 200 or status >= 300:
            raise SupabaseScoreSyncError(
                f"评分同步服务返回 HTTP {status}：{body[:500]}"
            )
        try:
            result = json.loads(body) if body else {}
        except ValueError as exc:
            raise SupabaseScoreSyncError("评分同步服务返回了无效 JSON") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            message = result.get("error", "评分同步服务未确认写入成功")
            raise SupabaseScoreSyncError(str(message))
        return result

    def health_check(self) -> str:
        result = self._post(
            {
                "schema_version": SUPABASE_SYNC_SCHEMA_VERSION,
                "score_schema_version": SCORE_SCHEMA_VERSION,
                "client_install_id": self.installation_id,
                "captures": [],
            }
        )
        return str(result.get("service_version") or "unknown")

    def record_anonymous_usage(self, version: str) -> dict[str, Any]:
        return self._post(
            {
                "action": "usage_ping",
                "install_id": self.installation_id,
                "version": str(version or "unknown").strip()[:32],
            }
        )

    def fetch_usage_statistics(self, days: int = 30) -> dict[str, Any]:
        result = self._post(
            {
                "action": "usage_stats",
                "days": max(7, min(180, int(days))),
            }
        )
        statistics = result.get("statistics")
        if not isinstance(statistics, dict):
            raise SupabaseScoreSyncError("匿名使用统计服务返回了无效数据")
        return statistics

    def publish_player_identity_bindings(
        self,
        bindings: Iterable[CachedPlayerIdentity],
    ) -> int:
        rows = []
        for binding in bindings:
            try:
                profile_id = int(binding.profile_id)
                runtime_id = int(binding.runtime_actor_id)
            except (TypeError, ValueError):
                continue
            name = str(binding.character_name or "").strip()[:64]
            if profile_id <= 0 or runtime_id < 0 or not name:
                continue
            rows.append({
                # Decimal strings avoid JavaScript losing precision if a
                # future protocol expands either ID beyond 2**53.
                "profile_id": str(profile_id),
                "runtime_actor_id": str(runtime_id),
                "character_name": name,
                "job": str(binding.job or "").strip()[:64],
                "source": str(binding.source or "packet").strip()[:32],
                "observed_at": float(binding.observed_at or 0.0),
            })
        if not rows:
            return 0
        result = self._post({
            "action": "player_identity_publish",
            "bindings": rows[:200],
        })
        return max(0, int(result.get("accepted", 0) or 0))

    def fetch_player_identity_bindings(
        self,
        runtime_actor_ids: Iterable[int],
        *,
        jobs: dict[int, str] | None = None,
    ) -> tuple[CachedPlayerIdentity, ...]:
        runtime_ids: list[int] = []
        for value in runtime_actor_ids:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0 and parsed not in runtime_ids:
                runtime_ids.append(parsed)
        if not runtime_ids:
            return ()
        job_map = jobs or {}
        result = self._post({
            "action": "player_identity_lookup",
            "runtime_actor_ids": [str(value) for value in runtime_ids[:128]],
            "jobs": {
                str(value): str(job_map.get(value, "") or "").strip()[:64]
                for value in runtime_ids[:128]
                if str(job_map.get(value, "") or "").strip()
            },
        })
        rows = result.get("bindings")
        if not isinstance(rows, list):
            raise SupabaseScoreSyncError("角色身份缓存服务返回了无效数据")
        bindings: list[CachedPlayerIdentity] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                runtime_id = int(row.get("runtime_actor_id", 0) or 0)
                profile_id = int(row.get("profile_id", 0) or 0)
                observed_at = float(row.get("observed_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            name = str(row.get("character_name", "") or "").strip()
            returned_job = str(row.get("job", "") or "").strip()
            expected_job = str(job_map.get(runtime_id, "") or "").strip()
            if (
                runtime_id <= 0
                or profile_id <= 0
                or not name
                or expected_job and returned_job and expected_job != returned_job
            ):
                continue
            bindings.append(CachedPlayerIdentity(
                profile_id=profile_id,
                runtime_actor_id=runtime_id,
                character_name=name,
                job=returned_job,
                source="cloud_cache",
                observed_at=observed_at,
            ))
        return tuple(bindings)

    def fetch_player_identity_profiles(
        self,
        profile_ids: Iterable[int],
    ) -> tuple[CachedPlayerIdentity, ...]:
        requested: list[int] = []
        for value in profile_ids:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0 and parsed not in requested:
                requested.append(parsed)
        if not requested:
            return ()
        result = self._post({
            "action": "player_identity_profile_lookup",
            "profile_ids": [str(value) for value in requested[:128]],
        })
        rows = result.get("bindings")
        if not isinstance(rows, list):
            raise SupabaseScoreSyncError("角色档案身份缓存服务返回了无效数据")
        bindings: list[CachedPlayerIdentity] = []
        requested_set = set(requested)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                runtime_id = int(row.get("runtime_actor_id", 0) or 0)
                profile_id = int(row.get("profile_id", 0) or 0)
                observed_at = float(row.get("observed_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            name = str(row.get("character_name", "") or "").strip()
            if profile_id not in requested_set or runtime_id < 0 or not name:
                continue
            bindings.append(CachedPlayerIdentity(
                profile_id=profile_id,
                runtime_actor_id=runtime_id,
                character_name=name,
                job=str(row.get("job", "") or "").strip(),
                source="cloud_profile_cache",
                observed_at=observed_at,
            ))
        return tuple(bindings)

    def fetch_benchmark(self, nest: str, job: str) -> dict[str, Any] | None:
        result = self._post(
            {
                "action": "benchmark",
                "nest": str(nest or "").strip(),
                "job": str(job or "").strip(),
            }
        )
        benchmark = result.get("benchmark")
        return benchmark if isinstance(benchmark, dict) else None

    def fetch_benchmarks(self) -> tuple[dict[str, Any], ...]:
        """Fetch all benchmarks, following CloudBase's bounded response pages."""
        collected: list[dict[str, Any]] = []
        offset = 0
        for _page_index in range(100):
            result = self._post(
                {"action": "benchmarks", "offset": offset, "limit": 25}
            )
            rows = result.get("benchmarks")
            if not isinstance(rows, list):
                break
            collected.extend(row for row in rows if isinstance(row, dict))
            next_offset = result.get("next_offset")
            # Supabase's legacy endpoint returns the complete catalog and does
            # not include pagination metadata.
            if next_offset is None:
                break
            next_value = int(next_offset)
            if next_value <= offset:
                raise SupabaseScoreSyncError("职业均值分页游标没有前进")
            offset = next_value
        return tuple(collected)

    def fetch_character_history(
        self,
        character_name: str,
        *,
        offset: int = 0,
        limit: int = 20,
        nest: str = "",
    ) -> dict[str, Any]:
        result = self._post({
            "action": "character_history",
            "character_name": str(character_name or "").strip(),
            "offset": max(0, int(offset)),
            "limit": max(1, min(50, int(limit))),
            "nest": str(nest or "").strip()[:128],
        })
        history = result.get("history")
        return history if isinstance(history, dict) else {
            "total": 0, "offset": 0, "limit": max(1, min(50, int(limit))),
            "nest": str(nest or "").strip()[:128], "nests": [],
            "items": [],
        }

    def fetch_character_history_overlay(
        self,
        character_name: str,
        *,
        offset: int = 0,
        limit: int = 20,
        nest: str = "",
    ) -> dict[str, Any]:
        """Fetch name-keyed history through server-enforced overlay privacy."""

        page_limit = max(1, min(50, int(limit)))
        result = self._post({
            "action": "character_history_overlay",
            "character_name": str(character_name or "").strip(),
            "offset": max(0, int(offset)),
            "limit": page_limit,
            "nest": str(nest or "").strip()[:128],
        })
        history = result.get("history")
        if isinstance(history, dict):
            return history
        raise SupabaseScoreSyncError("云端悬浮窗隐私服务返回了无效数据")

    def fetch_profile_history(
        self,
        profile_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
        nest: str = "",
    ) -> dict[str, Any]:
        normalized_profile_id = int(profile_id)
        if normalized_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        page_limit = max(1, min(50, int(limit)))
        result = self._post({
            "action": "profile_history",
            "profile_id": str(normalized_profile_id),
            "offset": max(0, int(offset)),
            "limit": page_limit,
            "nest": str(nest or "").strip()[:128],
        })
        history = result.get("history")
        return history if isinstance(history, dict) else {
            "profileId": str(normalized_profile_id),
            "total": 0,
            "offset": max(0, int(offset)),
            "limit": page_limit,
            "nest": str(nest or "").strip()[:128],
            "nests": [],
            "items": [],
        }

    def fetch_profile_history_overlay(
        self,
        profile_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
        nest: str = "",
    ) -> dict[str, Any]:
        """Fetch profile history through the server-enforced overlay policy."""

        normalized_profile_id = int(profile_id)
        if normalized_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        page_limit = max(1, min(50, int(limit)))
        result = self._post({
            "action": "profile_history_overlay",
            "profile_id": str(normalized_profile_id),
            "offset": max(0, int(offset)),
            "limit": page_limit,
            "nest": str(nest or "").strip()[:128],
        })
        history = result.get("history")
        if isinstance(history, dict):
            return history
        raise SupabaseScoreSyncError("云端悬浮窗隐私服务返回了无效数据")

    def set_profile_score_overlay_visibility(
        self,
        profile_id: int,
        hidden: bool,
        *,
        character_name: str = "",
    ) -> dict[str, Any]:
        """Persist the current character's teammate-overlay visibility."""

        normalized_profile_id = int(profile_id)
        if normalized_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        result = self._post({
            "action": "profile_score_overlay_visibility_set",
            "profile_id": str(normalized_profile_id),
            "hidden": bool(hidden),
            "character_name": str(character_name or "").strip()[:64],
        })
        visibility = result.get("visibility")
        if isinstance(visibility, dict):
            return visibility
        raise SupabaseScoreSyncError("云端悬浮窗隐私设置返回了无效数据")

    def set_profile_score_visibility(
        self,
        profile_id: int,
        overlay_hidden: bool,
        software_hidden: bool,
        *,
        character_name: str = "",
    ) -> dict[str, Any]:
        """Persist independent overlay and in-software privacy scopes."""

        normalized_profile_id = int(profile_id)
        if normalized_profile_id <= 0:
            raise ValueError("档案 ID 无效")
        result = self._post({
            "action": "profile_score_visibility_set",
            "profile_id": str(normalized_profile_id),
            "overlay_hidden": bool(overlay_hidden),
            "software_hidden": bool(software_hidden),
            "character_name": str(character_name or "").strip()[:64],
        })
        visibility = result.get("visibility")
        if isinstance(visibility, dict):
            return visibility
        raise SupabaseScoreSyncError("云端评分隐私设置返回了无效数据")

    def set_character_score_visibility(
        self,
        character_name: str,
        overlay_hidden: bool,
        software_hidden: bool,
    ) -> dict[str, Any]:
        """Persist privacy by durable character key rather than a session ID."""

        name = str(character_name or "").strip()[:64]
        if not name:
            raise ValueError("角色名无效")
        result = self._post({
            "action": "character_score_visibility_set",
            "character_name": name,
            "overlay_hidden": bool(overlay_hidden),
            "software_hidden": bool(software_hidden),
        })
        visibility = result.get("visibility")
        if isinstance(visibility, dict):
            return visibility
        raise SupabaseScoreSyncError("云端评分隐私设置返回了无效数据")

    def fetch_character_history_detail(
        self,
        character_name: str,
        history_id: str,
    ) -> dict[str, Any] | None:
        result = self._post({
            "action": "character_history_detail",
            "character_name": str(character_name or "").strip(),
            "history_id": str(history_id or "").strip(),
        })
        detail = result.get("detail")
        return detail if isinstance(detail, dict) else None

    def fetch_profession_history(
        self,
        job: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        result = self._post({
            "action": "profession_history",
            "job": str(job or "").strip(),
            "offset": max(0, int(offset)),
            "limit": max(1, min(50, int(limit))),
        })
        history = result.get("history")
        return history if isinstance(history, dict) else {
            "total": 0,
            "offset": 0,
            "limit": max(1, min(50, int(limit))),
            "job": str(job or "").strip(),
            "nests": [],
            "items": [],
        }

    def fetch_profession_history_detail(
        self,
        job: str,
        history_id: str,
    ) -> dict[str, Any] | None:
        result = self._post({
            "action": "profession_history_detail",
            "job": str(job or "").strip(),
            "history_id": str(history_id or "").strip(),
        })
        detail = result.get("detail")
        return detail if isinstance(detail, dict) else None

    def fetch_score_history_page(
        self,
        *,
        character_name: str = "",
        jobs: Iterable[str] = (),
        nest: str = "",
        minimum_score: float | None = None,
        maximum_score: float | None = None,
        recorded_from: str = "",
        recorded_to: str = "",
        character_search: str = "",
        completion_status: str = "",
        score_schema_versions: Iterable[int] = (),
        sort_key: str = "recordedAt",
        sort_direction: str = "desc",
        cursor: str = "",
        limit: int = 20,
        include_facets: bool = False,
    ) -> dict[str, Any]:
        normalized_jobs = tuple(dict.fromkeys(
            str(value or "").strip() for value in jobs if str(value or "").strip()
        ))
        page_limit = max(1, min(50, int(limit)))
        normalized_completion = str(completion_status or "").strip().lower()
        if normalized_completion not in {"", "cleared", "not_cleared", "unknown"}:
            raise ValueError("通关状态筛选值无效")
        normalized_versions: list[int] = []
        for value in score_schema_versions:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if 0 < parsed <= 29991231 and parsed not in normalized_versions:
                normalized_versions.append(parsed)
        request_payload = {
            "action": "score_history_query",
            "character_name": str(character_name or "").strip(),
            "jobs": list(normalized_jobs),
            "nest": str(nest or "").strip()[:2048],
            "minimum_score": minimum_score,
            "maximum_score": maximum_score,
            "recorded_from": str(recorded_from or "").strip(),
            "recorded_to": str(recorded_to or "").strip(),
            "character_search": str(character_search or "").strip()[:64],
            "completion_status": normalized_completion,
            "score_schema_versions": normalized_versions[:64],
            "sort_key": str(sort_key or "recordedAt").strip(),
            "sort_direction": str(sort_direction or "desc").strip().lower(),
            "cursor": str(cursor or "").strip(),
            "limit": page_limit,
            "include_facets": bool(include_facets),
        }
        facets_deferred = False
        try:
            result = self._post(request_payload)
        except SupabaseScoreSyncError as exc:
            if not include_facets or not _is_statement_timeout(exc):
                raise
            # Older deployed cloud functions may still fail the combined
            # count/facet/page RPC. Retry only the required cursor page so the
            # history list remains available.
            request_payload["include_facets"] = False
            result = self._post(request_payload)
            facets_deferred = True
        history = result.get("history")
        if isinstance(history, dict):
            page = dict(history)
            if facets_deferred:
                page["facetsDeferred"] = True
            return page
        return {
            "total": None,
            "limit": page_limit,
            "cursor": str(cursor or "").strip(),
            "nextCursor": "",
            "hasMore": False,
            "nests": [],
            "scoreSchemaVersions": [],
            "items": [],
        }

    def fetch_database_profession_ranking(
        self,
        *,
        section: str = "professions",
        composition: str = "",
        nest: str = "",
        score_schema_versions: Iterable[int] = (),
        completion_status: str = "",
        recorded_from: str = "",
        recorded_to: str = "",
        profile_id: str = "",
        character_name: str = "",
        job: str = "",
    ) -> dict[str, Any]:
        normalized_versions: list[int] = []
        for value in score_schema_versions:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if 0 < parsed <= 29991231 and parsed not in normalized_versions:
                normalized_versions.append(parsed)
        versions = tuple(normalized_versions[:64])
        result = self._post({
            "action": "database_profession_ranking",
            "section": section if section in {"professions", "skills", "support", "teams", "distribution", "team-captures", "roles"} else "professions",
            "composition": str(composition or "").strip()[:1024],
            "nest": str(nest or "").strip()[:2048],
            "score_schema_versions": list(versions),
            "completion_status": completion_status
            if completion_status in {"cleared", "not_cleared", "unknown"} else "",
            "recorded_from": str(recorded_from or "").strip()[:64],
            "recorded_to": str(recorded_to or "").strip()[:64],
            "profile_id": str(profile_id or "").strip()[:20],
            "character_name": str(character_name or "").strip()[:64],
            "job": str(job or "").strip()[:64],
        })
        ranking = result.get("ranking")
        if not isinstance(ranking, dict):
            raise SupabaseScoreSyncError("数据库职业排行服务返回了无效数据")
        if section == "roles" and (
            ranking.get("section") != "roles"
            or not isinstance(ranking.get("rows"), list)
            or any(
                not isinstance(row, dict) or not isinstance(row.get("metric"), str)
                for row in ranking["rows"]
            )
        ):
            raise SupabaseScoreSyncError(
                "职责查询返回了其他排行数据，不是没有样本；请重启或更新 ACT 核心，"
                "并确认云端支持职责统计。"
            )
        return ranking

    def upload_crash_diagnostic(
        self,
        package_path: Path,
        *,
        package_hash: str,
        file_size: int,
        app_version: str,
        trigger: str,
        created_epoch_ms: int,
    ) -> dict[str, Any]:
        """Stream one generated crash ZIP to CloudBase private storage.

        The caller validates the ZIP report and calculates the content hash.
        The cloud function creates the object path, signs the upload and then
        verifies the stored size before acknowledging it.  This intentionally
        stays independent of the optional raw ``.dpscap`` upload preference.
        """

        path = Path(package_path)
        normalized_hash = str(package_hash or "").strip().casefold()
        if not path.is_file() or path.suffix.casefold() != ".zip":
            raise SupabaseScoreSyncError("本地闪退诊断包已不存在")
        if len(normalized_hash) != 64 or any(
            value not in "0123456789abcdef" for value in normalized_hash
        ):
            raise SupabaseScoreSyncError("闪退诊断包哈希无效")
        actual_size = int(path.stat().st_size)
        if actual_size != int(file_size) or actual_size <= 0:
            raise SupabaseScoreSyncError("闪退诊断包大小在上传前发生变化")

        metadata = {
            "package_hash": normalized_hash,
            "file_size": actual_size,
            "app_version": str(app_version or "unknown").strip()[:32],
            "trigger": str(trigger or "unknown").strip()[:64],
            "created_epoch_ms": int(created_epoch_ms),
            "install_id": self.installation_id,
        }
        prepared = self._post({
            "action": "crash_diagnostic_prepare",
            **metadata,
        })
        object_id = str(prepared.get("object_id") or "").strip()
        cloud_object_id = str(prepared.get("cloud_object_id") or "").strip()
        diagnostic_id = str(prepared.get("diagnostic_id") or "").strip()
        if bool(prepared.get("already_uploaded")):
            if not object_id or not cloud_object_id or not diagnostic_id:
                raise SupabaseScoreSyncError("云端闪退诊断记录缺少对象标识")
            return {
                **prepared,
                "package_hash": normalized_hash,
                "object_id": object_id,
                "cloud_object_id": cloud_object_id,
                "diagnostic_id": diagnostic_id,
            }

        upload_url = str(prepared.get("upload_url") or "").strip()
        upload_headers = prepared.get("upload_headers")
        receipt = str(prepared.get("upload_receipt") or "").strip()
        if (
            not upload_url
            or not isinstance(upload_headers, dict)
            or not receipt
            or not object_id
            or not cloud_object_id
            or not diagnostic_id
        ):
            raise SupabaseScoreSyncError("云端未返回完整的闪退诊断包上传信息")
        headers = {
            str(key): str(value)
            for key, value in upload_headers.items()
            if str(key).strip() and str(value).strip()
        }
        headers.update({
            "Content-Type": "application/octet-stream",
            "Content-Length": str(actual_size),
            "User-Agent": "DragonNestACT-CrashDiagnosticUpload/1",
        })
        timeout = max(
            120.0,
            min(1800.0, 60.0 + actual_size / float(256 * 1024)),
        )
        try:
            with path.open("rb") as stream:
                request = urllib.request.Request(
                    upload_url,
                    data=stream,
                    headers=headers,
                    method="PUT",
                )
                with self._opener(request, timeout=timeout) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    if status < 200 or status >= 300:
                        raise SupabaseScoreSyncError(
                            f"CloudBase 闪退诊断包上传返回 HTTP {status}"
                        )
                    response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SupabaseScoreSyncError(
                f"CloudBase 闪退诊断包上传返回 HTTP {exc.code}：{detail[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SupabaseScoreSyncError(
                f"无法上传闪退诊断包到 CloudBase：{exc.reason}"
            ) from exc
        except OSError as exc:
            raise SupabaseScoreSyncError(f"读取或上传闪退诊断包失败：{exc}") from exc

        finalized = self._post({
            "action": "crash_diagnostic_finalize",
            **metadata,
            "object_id": object_id,
            "cloud_object_id": cloud_object_id,
            "upload_receipt": receipt,
        })
        if not bool(finalized.get("diagnostic_file_registered")):
            raise SupabaseScoreSyncError("云端未确认闪退诊断包上传完成")
        return {
            **prepared,
            **finalized,
            "package_hash": normalized_hash,
            "object_id": object_id,
            "cloud_object_id": cloud_object_id,
            "diagnostic_id": diagnostic_id,
        }

    def _put_capture_file(
        self,
        capture: StoredCaptureFile,
    ) -> tuple[str, str]:
        path = capture.source_path
        if not path.is_file() or path.suffix.casefold() != ".dpscap":
            raise SupabaseScoreSyncError("本地 .dpscap 文件已不存在")
        actual_hash = capture_content_hash(path)
        if actual_hash != capture.capture_hash:
            raise SupabaseScoreSyncError("本地 .dpscap 内容已变化，拒绝关联到旧评分")
        file_size = int(path.stat().st_size)
        prepared = self._post({
            "action": "capture_file_prepare",
            "capture_hash": capture.capture_hash,
            "file_size": file_size,
        })
        object_id = str(prepared.get("object_id") or "")
        cloud_object_id = str(prepared.get("cloud_object_id") or "")
        if bool(prepared.get("already_uploaded")):
            if not object_id or not cloud_object_id:
                raise SupabaseScoreSyncError("云端抓包记录缺少私有对象标识")
            return object_id, cloud_object_id

        upload_url = str(prepared.get("upload_url") or "").strip()
        upload_headers = prepared.get("upload_headers")
        receipt = str(prepared.get("upload_receipt") or "").strip()
        if (
            not upload_url
            or not isinstance(upload_headers, dict)
            or not receipt
            or not object_id
            or not cloud_object_id
        ):
            raise SupabaseScoreSyncError("云端未返回完整的私有文件上传信息")
        headers = {
            str(key): str(value)
            for key, value in upload_headers.items()
            if str(key).strip() and str(value).strip()
        }
        headers.update({
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
            "User-Agent": "DragonNestBattlePlatform-CaptureUpload/1",
        })
        timeout = max(
            120.0,
            min(7200.0, 60.0 + file_size / float(256 * 1024)),
        )
        request = None
        try:
            with path.open("rb") as stream:
                request = urllib.request.Request(
                    upload_url,
                    data=stream,
                    headers=headers,
                    method="PUT",
                )
                with self._opener(request, timeout=timeout) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    if status < 200 or status >= 300:
                        raise SupabaseScoreSyncError(
                            f"CloudBase 私有文件上传返回 HTTP {status}"
                        )
                    response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SupabaseScoreSyncError(
                f"CloudBase 私有文件上传返回 HTTP {exc.code}：{detail[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SupabaseScoreSyncError(
                f"无法上传 .dpscap 到 CloudBase：{exc.reason}"
            ) from exc
        except OSError as exc:
            raise SupabaseScoreSyncError(f"读取或上传 .dpscap 失败：{exc}") from exc

        finalized = self._post({
            "action": "capture_file_finalize",
            "capture_hash": capture.capture_hash,
            "file_size": file_size,
            "object_id": object_id,
            "cloud_object_id": cloud_object_id,
            "upload_receipt": receipt,
        })
        if not bool(finalized.get("capture_file_registered")):
            raise SupabaseScoreSyncError("云端未确认 .dpscap 与评分记录的关联")
        return object_id, cloud_object_id

    def _sync_capture_files(
        self,
        store: TeamScoreSyncStore,
        *,
        limit: int = 5,
    ) -> tuple[int, str]:
        uploaded = 0
        last_error = ""
        for capture in store.pending_capture_files(limit=limit):
            try:
                object_id, cloud_object_id = self._put_capture_file(capture)
                store.mark_capture_file_synced(
                    capture.capture_hash,
                    object_id=object_id,
                    cloud_object_id=cloud_object_id,
                    file_size=capture.file_size,
                )
                uploaded += 1
            except Exception as exc:
                last_error = str(exc)
                store.mark_capture_file_error(capture.capture_hash, last_error)
                # A network or storage configuration failure normally affects
                # all remaining files. Keep them queued instead of repeatedly
                # issuing doomed requests in the same background pass.
                break
        return uploaded, last_error

    @staticmethod
    def _capture_payload(capture) -> dict[str, Any]:
        return {
            "capture_hash": capture.capture_hash,
            "nest": capture.nest,
            "recorded_at": capture.recorded_at,
            "rows": [
                {
                    "row_fingerprint": row.fingerprint,
                    "sample_ordinal": row.sample_ordinal,
                    "job": row.job,
                    "total_score": row.total_score,
                    "output_score": row.output_score,
                    "support_score": row.support_score,
                    "specialty_score": row.specialty_score,
                    "survival_penalty": row.survival_penalty,
                    "theory_damage_completion": row.theory_damage_completion,
                    "actual_dps": row.actual_dps,
                    "score_detail": row.score_detail or {},
                    "character_identity": row.character_identity or {},
                }
                for row in capture.rows
            ],
        }

    def sync_store(
        self,
        store: TeamScoreSyncStore,
        *,
        batch_size: int = 1,
        max_batches: int = 50,
        upload_capture_files: bool = False,
    ) -> SupabaseSyncResult:
        uploaded_captures = 0
        added_rows = 0
        skipped_rows = 0
        deleted_rows = 0
        updated_rows = 0
        skill_detail_rows = 0
        support_detail_rows = 0
        character_identity_rows = 0
        extended_detail_rows = 0
        uploaded_capture_files = 0
        capture_file_error = ""
        failed_hashes: set[str] = set()
        score_upload_error = ""
        for _batch_index in range(max(1, int(max_batches))):
            captures = store.pending_supabase_captures(
                limit=max(1, min(25, int(batch_size))),
                minimum_schema_version=SUPABASE_SYNC_SCHEMA_VERSION,
                score_schema_version=SCORE_SCHEMA_VERSION,
                exclude_hashes=failed_hashes,
            )
            if not captures:
                break
            hashes = tuple(capture.capture_hash for capture in captures)
            try:
                result = self._post(
                    {
                        "schema_version": SUPABASE_SYNC_SCHEMA_VERSION,
                        "score_schema_version": SCORE_SCHEMA_VERSION,
                        "client_install_id": self.installation_id,
                        "captures": [
                            self._capture_payload(capture) for capture in captures
                        ],
                    }
                )
                processed = {
                    str(value)
                    for value in result.get("processed_capture_hashes", ())
                }
                if processed != set(hashes):
                    raise SupabaseScoreSyncError(
                        "Edge Function 未确认本批次全部记录，已保留本地队列等待重试"
                    )
            except Exception as exc:
                store.mark_supabase_error(str(exc), hashes)
                # A record-specific size/validation error must not starve
                # every later capture. Never acknowledge or delete it.
                message = str(exc)
                if any(fragment in message for fragment in (
                    "request_body_too_large", "评分上传内容超过",
                    "评分压缩上传内容超过", "时间轴含非显示字段",
                )):
                    failed_hashes.update(hashes)
                    score_upload_error = message
                    continue
                raise
            store.mark_supabase_synced(
                hashes,
                schema_version=SUPABASE_SYNC_SCHEMA_VERSION,
                score_schema_version=SCORE_SCHEMA_VERSION,
            )
            uploaded_captures += len(hashes)
            added_rows += int(result.get("added_rows", 0) or 0)
            skipped_rows += int(result.get("skipped_rows", 0) or 0)
            deleted_rows += int(result.get("deleted_rows", 0) or 0)
            updated_rows += int(result.get("updated_rows", 0) or 0)
            skill_detail_rows += int(result.get("skill_detail_rows", 0) or 0)
            support_detail_rows += int(result.get("support_detail_rows", 0) or 0)
            character_identity_rows += int(
                result.get("character_identity_rows", 0) or 0
            )
            extended_detail_rows += int(
                result.get("extended_detail_rows", 0) or 0
            )
        if upload_capture_files:
            uploaded_capture_files, capture_file_error = self._sync_capture_files(
                store
            )
        return SupabaseSyncResult(
            uploaded_captures=uploaded_captures,
            added_rows=added_rows,
            skipped_rows=skipped_rows,
            deleted_rows=deleted_rows,
            remaining_captures=store.pending_supabase_count(
                SUPABASE_SYNC_SCHEMA_VERSION,
                SCORE_SCHEMA_VERSION,
            ),
            updated_rows=updated_rows,
            skill_detail_rows=skill_detail_rows,
            support_detail_rows=support_detail_rows,
            character_identity_rows=character_identity_rows,
            extended_detail_rows=extended_detail_rows,
            uploaded_capture_files=uploaded_capture_files,
            remaining_capture_files=store.pending_capture_file_count(),
            capture_file_error=capture_file_error,
            failed_captures=len(failed_hashes),
            score_upload_error=score_upload_error,
        )
