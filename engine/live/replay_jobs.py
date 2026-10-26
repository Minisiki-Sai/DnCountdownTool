"""开发者模式「回放实战录像」的后台作业。

一次回放的解码是 CPU 密集的（黑龙经典那种 40MB / 4 万帧要几十秒），所以
不能放在请求线程里同步跑，否则界面看起来像卡死。这里用后台线程 + 轮询进度。

同一份文件解码一次就缓存，重复点同一个录像不会重新算。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .capture_replay import discover_captures, replay

# 只保留最近几个作业，避免长时间开着工具时内存不断长。
_MAX_JOBS = 4


class ReplayJobManager:
    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cache: dict[tuple[str, float], dict[str, Any]] = {}

    def open(self, path_text: str) -> dict[str, Any]:
        path = Path(str(path_text or "")).expanduser()
        if not path.is_file() or path.suffix.casefold() != ".dpscap":
            return {"ok": False, "error": "找不到这个抓包文件"}
        try:
            stat = path.stat()
        except OSError as exc:
            return {"ok": False, "error": f"读不到文件：{exc}"}

        key = (str(path.resolve()), float(stat.st_mtime))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                job_id = uuid.uuid4().hex[:12]
                self._jobs[job_id] = {**cached, "job": job_id, "state": "ready", "progress": 1.0,
                                      "message": "已从缓存载入"}
                self._trim()
                return {"ok": True, "job": job_id, "cached": True}

            job_id = uuid.uuid4().hex[:12]
            self._jobs[job_id] = {
                "job": job_id,
                "state": "loading",
                "progress": 0.0,
                "message": "准备中...",
                "capture": path.name,
                "path": str(path.resolve()),
            }
            self._trim()

        thread = threading.Thread(
            target=self._run,
            args=(job_id, path, key),
            name=f"dn-replay-{job_id}",
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job": job_id, "cached": False}

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job is None:
                return {"ok": False, "error": "作业不存在或已过期"}
            return {"ok": True, **job}

    def captures(self, *roots: Path) -> dict[str, Any]:
        rows = discover_captures(*roots)
        return {"ok": True, "captures": rows}

    # ── 内部 ────────────────────────────────────────────────────────────

    def _run(self, job_id: str, path: Path, key: tuple[str, float]) -> None:
        try:
            def progress(ratio: float, message: str) -> None:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job["progress"] = max(0.0, min(1.0, float(ratio)))
                        job["message"] = str(message)

            started = time.monotonic()
            result = replay(path, self._data_root, progress=progress)
            payload = {
                "capture": result.capture.name,
                "path": str(result.capture),
                "nestKey": result.nest_key,
                "nestName": result.nest_name,
                "frames": result.frames,
                "durationMs": result.duration_ms,
                "bossActorCount": result.boss_actor_count,
                # 证据行只有模板号，悬浮窗的 Boss 行要名字，所以整场名单一起给。
                "bosses": result.bosses,
                "drops": dict(result.drops),
                "dropSamples": result.drop_samples,
                "kindCounts": dict(result.kind_counts),
                "rows": result.rows,
                "elapsedMs": int((time.monotonic() - started) * 1000),
            }
            with self._lock:
                self._cache[key] = payload
                job = self._jobs.get(job_id)
                if job is not None:
                    job.update(payload)
                    job["state"] = "ready"
                    job["progress"] = 1.0
                    job["message"] = f"解析完成，{len(result.rows)} 行"
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["state"] = "error"
                    job["progress"] = 1.0
                    job["message"] = "解析失败"
                    job["error"] = f"{type(exc).__name__}: {exc}"

    def _trim(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        # 丢掉最旧的、且已经结束的作业。
        for job_id in list(self._jobs)[: len(self._jobs) - _MAX_JOBS]:
            job = self._jobs.get(job_id)
            if job is not None and job.get("state") != "loading":
                self._jobs.pop(job_id, None)
