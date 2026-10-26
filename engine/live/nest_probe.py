"""副本 / Boss 观测日志。只记录事实，不参与任何判定。

实地打一次副本后用它回答三个问题：

1. 副本身份是怎么来的 —— 切图包命中了没有？还是靠 Boss 模板反推的？
2. Boss 实体绑上了没有 —— `boss_actor_ids` 到底有没有值？
3. Boss 技能号实际是什么 —— 在不在 `boss_skills.json` 的白名单里？

开关（默认关闭，release 不受影响）：

    DRAGON_NEST_NEST_PROBE=1          # 启动时默认值（可省略，用界面开关即可）
    DRAGON_NEST_NEST_PROBE_PATH=...   # 覆盖输出路径

运行期开关在界面「设置 → 调试记录」里，一个开关同时控制本模块与「保留抓包」
（见 `debug_recording` 模块）。`ProbeSwitch` 是解码器长期持有的稳定句柄，开关
只切换它内部是否建立 recorder，因此随时切换都不需要重启。

输出（默认）：

    <项目根>/.dev/probe/probe_<YYYYmmdd_HHMMSS>.jsonl

设置里改过保存目录后，写到 `<保存目录>/probe/probe_<时间>.jsonl`；环境变量
`DRAGON_NEST_NEST_PROBE_PATH` 的优先级仍然最高（脚本/测试用）。

每条一行 JSON。首行是头部信息，之后按发生顺序写入。另外每 5 秒写一条
`shape_stats`，汇总这段时间内服务端报文的「长度 + 签名」分布 —— 如果副本里
根本没有出生包，从这份分布可以立刻看出来（找不到 88 字节 / 06275500 这一族）。

设计约束：
  * 任何异常都在内部吞掉：观测代码绝不能让采集/解码失败；
  * 热路径只做计数（dict += 1）和一两次时间比较，不做 I/O；
  * 真正的事件行才落盘，落盘是同步 write + 定期 flush；
  * 单文件超过 MAX_BYTES 后停止写入并追加一条 truncated 记录。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

PROBE_ENV = "DRAGON_NEST_NEST_PROBE"
PROBE_PATH_ENV = "DRAGON_NEST_NEST_PROBE_PATH"
MAX_BYTES = 32 * 1024 * 1024
_SHAPE_FLUSH_SECONDS = 5.0
_MAX_SHAPE_KEYS = 512

_ENABLED_TRUE = {"1", "true", "yes", "on"}

# 与前端 frontend/src/utils/classicMechanics.ts 的 isClassic() 保持一致的近似判定。
# 观测日志用它回答「这个副本名会不会被当成黑龙经典」——一旦不一致，规则集就会
# 套错，所以必须把判定结果一起记下来，而不是等事后靠人眼比。
_CLASSIC_PATTERN = re.compile(
    r"^黑龙(?:巢穴)?[（(]?(?:123|[一二三123ⅠⅡⅢ])[）)]?(?:$|[（(])"
)
_WHITESPACE = re.compile(r"\s+")


def classic_nest_hint(name: object) -> bool:
    """前端 `isClassic()` 的等价实现，只用于观测，不参与任何业务判定。"""

    text = str(name or "")
    if "黑龙" not in text or "硬核" in text:
        return False
    if "经典" in text:
        return True
    return bool(_CLASSIC_PATTERN.match(_WHITESPACE.sub("", text)))


_RUNTIME_STATE_LOCK = threading.RLock()
# None = 跟随环境变量；True/False = 运行期开关（UI 或接口设置）说了算。
_RUNTIME_DEFAULT: bool | None = None
# 运行期输出目录（界面「调试记录」里设置的保存路径）。None = 项目内 .dev/probe。
_OUTPUT_DIR: Path | None = None


def set_output_dir(directory: Path | str | None) -> None:
    """设置观测日志的输出目录。None 表示回到 `<root>/.dev/probe`。"""

    global _OUTPUT_DIR
    raw = "" if directory is None else str(directory).strip()
    with _RUNTIME_STATE_LOCK:
        _OUTPUT_DIR = Path(raw).expanduser() if raw else None


def output_dir() -> Path | None:
    with _RUNTIME_STATE_LOCK:
        return _OUTPUT_DIR


def probe_requested() -> bool:
    """当前是否应当记录。运行期开关优先于环境变量。"""

    with _RUNTIME_STATE_LOCK:
        if _RUNTIME_DEFAULT is not None:
            return bool(_RUNTIME_DEFAULT)
    return str(os.environ.get(PROBE_ENV, "")).strip().casefold() in _ENABLED_TRUE


def runtime_source() -> str:
    with _RUNTIME_STATE_LOCK:
        return "runtime" if _RUNTIME_DEFAULT is not None else "env"


def set_runtime_enabled(value: bool | None) -> None:
    """运行期开关。None 表示恢复为跟随环境变量。"""

    global _RUNTIME_DEFAULT
    with _RUNTIME_STATE_LOCK:
        _RUNTIME_DEFAULT = None if value is None else bool(value)
    if _RUNTIME_DEFAULT is False:
        _close_recorders()


def _now() -> float:
    return time.monotonic()


class ProbeSwitch:
    """解码器长期持有的稳定句柄。

    开关是运行时的：解码器在初始化时拿到这个对象就再也不换，之后无论从 UI 还是
    接口切换开关，都只是这个对象内部的 recorder 建立/关闭。未开启时所有方法都
    在入口处返回，不产生文件、不产生 I/O。
    """

    def __init__(self, root: Path, *, header: dict[str, Any] | None = None) -> None:
        self._root = Path(root)
        self._header = dict(header or {})
        self._lock = threading.RLock()
        self._recorder: ProbeRecorder | None = None

    # ---------------------------------------------------------------- 状态
    @property
    def enabled(self) -> bool:
        # 只看开关，不做 I/O：文件在第一条真正要记录的事件时才建立。
        return probe_requested()

    @property
    def path(self) -> Path | None:
        recorder = self._recorder
        return recorder.path if recorder is not None else None

    @property
    def rows(self) -> int:
        recorder = self._recorder
        return recorder.rows if recorder is not None else 0

    @property
    def bytes_written(self) -> int:
        recorder = self._recorder
        return recorder.bytes_written if recorder is not None else 0

    @property
    def start_wall(self) -> str:
        recorder = self._recorder
        return recorder.start_wall if recorder is not None else ""

    def _ensure(self) -> ProbeRecorder | None:
        if not probe_requested():
            return None
        recorder = self._recorder
        if recorder is not None and not recorder.closed:
            return recorder
        with self._lock:
            recorder = self._recorder
            if recorder is not None and not recorder.closed:
                return recorder
            path = _default_path(self._root)
            try:
                recorder = ProbeRecorder(path, header=self._header)
            except Exception:
                return None
            self._recorder = recorder
            try:
                print(f"[probe] 观测日志：{recorder.path}", flush=True)
            except Exception:
                pass
            return recorder

    def set_enabled(self, value: bool) -> bool:
        set_runtime_enabled(value)
        if not value:
            self.close()
        else:
            self._ensure()
        return probe_requested()

    # ---------------------------------------------------------------- 转发
    def record(self, kind: str, **fields: Any) -> None:
        if not probe_requested():
            return
        recorder = self._ensure()
        if recorder is not None:
            recorder.record(kind, **fields)

    def observe_packet(
        self, transport: str, direction: str, payload: bytes
    ) -> None:
        if not probe_requested():
            return
        recorder = self._ensure()
        if recorder is not None:
            recorder.observe_packet(transport, direction, payload)

    def note_spawn_attempt(self, **fields: Any) -> None:
        if not probe_requested():
            return
        recorder = self._ensure()
        if recorder is not None:
            recorder.note_spawn_attempt(**fields)

    def note_map_attempt(self, **fields: Any) -> None:
        if not probe_requested():
            return
        recorder = self._ensure()
        if recorder is not None:
            recorder.note_map_attempt(**fields)

    def close(self) -> None:
        recorder = self._recorder
        if recorder is not None:
            recorder.close()


class ProbeRecorder:
    """把观测事实写成 JSON Lines。"""

    enabled = True

    def __init__(self, path: Path, *, header: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._handle = None
        self._written = 0
        self._bytes = 0
        self._closed = False
        self._truncated = False
        self._shapes: dict[str, int] = {}
        self._shapes_dropped = 0
        self._spawn_attempts = 0
        self._spawn_hits = 0
        self._spawn_miss_logged = 0
        self._map_attempts = 0
        self._map_hits = 0
        self._map_miss_logged = 0
        self._origin = _now()
        self._shape_started = self._origin
        self._last_flush = self._origin
        self.start_wall = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        except OSError:
            self._handle = None
        self._write_raw(
            {
                "t": 0.0,
                "kind": "header",
                "wall": self.start_wall,
                "pid": os.getpid(),
                **(header or {}),
            }
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def rows(self) -> int:
        return self._written

    @property
    def bytes_written(self) -> int:
        return self._bytes

    # ---------------------------------------------------------------- 内部
    def _write_raw(self, row: dict[str, Any]) -> None:
        if self._handle is None or self._truncated:
            return
        try:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return
        payload = line + "\n"
        size = len(payload.encode("utf-8", "ignore"))
        if self._bytes + size > MAX_BYTES:
            self._truncated = True
            try:
                self._handle.write(
                    json.dumps(
                        {"t": round(_now() - self._origin, 3), "kind": "truncated",
                         "bytes": self._bytes, "rows": self._written},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self._handle.flush()
            except OSError:
                pass
            return
        try:
            self._handle.write(payload)
            self._bytes += size
            self._written += 1
        except OSError:
            self._handle = None

    def _flush_if_due(self) -> None:
        now = _now()
        if now - self._last_flush < 1.0:
            return
        self._last_flush = now
        try:
            if self._handle is not None:
                self._handle.flush()
        except OSError:
            self._handle = None

    def _flush_shapes(self, *, force: bool = False) -> None:
        now = _now()
        if not force and now - self._shape_started < _SHAPE_FLUSH_SECONDS:
            return
        if not self._shapes:
            self._shape_started = now
            return
        top = sorted(self._shapes.items(), key=lambda item: item[1], reverse=True)[:40]
        self._write_raw(
            {
                "t": round(now - self._origin, 3),
                "kind": "shape_stats",
                "windowSeconds": round(now - self._shape_started, 3),
                "distinct": len(self._shapes),
                "dropped": self._shapes_dropped,
                "top": [{"shape": key, "count": count} for key, count in top],
            }
        )
        self._shapes.clear()
        self._shapes_dropped = 0
        self._shape_started = now

    # ---------------------------------------------------------------- 对外
    def record(self, kind: str, **fields: Any) -> None:
        if self._closed:
            return
        try:
            with self._lock:
                self._write_raw({"t": round(_now() - self._origin, 3), "kind": kind, **fields})
                self._flush_if_due()
        except Exception:
            # 观测失败必须静默：它不承担任何业务职责。
            pass

    def observe_packet(
        self, transport: str, direction: str, payload: bytes
    ) -> None:
        """按「长度 + 前段签名」给服务端报文分桶，用来确认某类包到底有没有。

        只做内存计数，落盘由 `_flush_shapes` 控制频率，因此可以放在每个包的
        热路径上。
        """

        if self._closed or direction != "server_to_client":
            return
        try:
            size = len(payload)
            if size < 24 or size > 1024:
                return
            signature = payload[5:9].hex() if size >= 9 else ""
            key = f"{str(transport or '').upper()}:{size}:{signature}"
            with self._lock:
                current = self._shapes.get(key)
                if current is None:
                    if len(self._shapes) >= _MAX_SHAPE_KEYS:
                        self._shapes_dropped += 1
                        return
                    self._shapes[key] = 1
                else:
                    self._shapes[key] = current + 1
                self._flush_shapes()
        except Exception:
            pass

    def note_spawn_attempt(self, **fields: Any) -> None:
        """记录一次怪物出生解码。命中全部记录；未命中抽样，避免刷屏。"""

        if self._closed:
            return
        try:
            hit = bool(fields.get("hit"))
            with self._lock:
                self._spawn_attempts += 1
                if hit:
                    self._spawn_hits += 1
                    self.record("spawn_hit", attempts=self._spawn_attempts, **fields)
                    return
                if self._spawn_miss_logged < 20 or self._spawn_attempts % 500 == 0:
                    self._spawn_miss_logged += 1
                    self.record("spawn_miss", attempts=self._spawn_attempts, **fields)
        except Exception:
            pass

    def note_map_attempt(self, **fields: Any) -> None:
        """记录地图身份解码。命中全部记录；未命中只记首次与每 1000 次。"""

        if self._closed:
            return
        try:
            hit = bool(fields.get("hit"))
            with self._lock:
                self._map_attempts += 1
                if hit:
                    self._map_hits += 1
                    self.record("map_hit", attempts=self._map_attempts, **fields)
                    return
                if self._map_miss_logged < 5 or self._map_attempts % 1000 == 0:
                    self._map_miss_logged += 1
                    self.record("map_miss", attempts=self._map_attempts, **fields)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.record(
                "summary",
                spawnAttempts=self._spawn_attempts,
                spawnHits=self._spawn_hits,
                mapAttempts=self._map_attempts,
                mapHits=self._map_hits,
            )
            self._flush_shapes(force=True)
            self._closed = True
            try:
                if self._handle is not None:
                    self._handle.flush()
                    self._handle.close()
            except OSError:
                pass
            finally:
                self._handle = None


_INSTANCES: dict[str, ProbeSwitch] = {}
_INSTANCES_LOCK = threading.Lock()


def _unique_path(path: Path) -> Path:
    """同一秒内轮转多次时避免互相覆盖。"""

    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _default_path(root: Path) -> Path:
    configured = str(os.environ.get(PROBE_PATH_ENV, "")).strip()
    if configured:
        candidate = Path(configured)
        # 显式指定成 .jsonl 视为「固定文件」：脚本/测试要可预测的路径，
        # 轮转后继续写同一个文件；指定成目录则每次轮转产生新文件。
        if candidate.suffix.casefold() == ".jsonl":
            return candidate
        return _unique_path(candidate / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    runtime_dir = output_dir()
    if runtime_dir is not None:
        return _unique_path(
            runtime_dir / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
    return _unique_path(
        Path(root)
        / ".dev"
        / "probe"
        / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )


def get_probe(root: Path, *, header: dict[str, Any] | None = None) -> ProbeSwitch:
    """进程内共享一个开关句柄，避免预战斗/正式解码器各写一份文件。

    注意它总是返回同一个 `ProbeSwitch`：开关是否真的在记录由 `probe_requested()`
    决定，可以在运行期任意切换，解码器不需要重新初始化。
    """

    key = str(Path(root).resolve())
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(key)
        if instance is not None:
            return instance
        instance = ProbeSwitch(Path(root).resolve(), header=header)
        _INSTANCES[key] = instance
        return instance


def _close_recorders() -> None:
    with _INSTANCES_LOCK:
        switches = list(_INSTANCES.values())
    for switch in switches:
        try:
            switch.close()
        except Exception:
            pass


def rotate() -> None:
    """关闭当前文件但保留开关：下一条事件会写进新的带时间戳文件。

    每次「开始统计」调用一次，保证一轮副本对应一个日志文件。
    """

    _close_recorders()


def ensure_started() -> None:
    """立刻建好当前轮次的日志文件（开关刚打开时用，避免界面上「开着却没文件」）。

    解码器尚未初始化时无事可做，第一笔事件仍会照常建文件。
    """

    if not probe_requested():
        return
    with _INSTANCES_LOCK:
        switches = list(_INSTANCES.values())
    for switch in switches:
        try:
            switch._ensure()
        except Exception:
            pass


def describe() -> dict[str, Any]:
    """当前开关状态，供接口/UI 回显。"""

    with _INSTANCES_LOCK:
        switches = list(_INSTANCES.values())
    recorder = next(
        (switch._recorder for switch in switches if switch._recorder is not None),
        None,
    )
    return {
        "enabled": probe_requested(),
        "source": runtime_source(),
        "path": str(recorder.path) if recorder is not None else "",
        "rows": recorder.rows if recorder is not None else 0,
        "bytes": recorder.bytes_written if recorder is not None else 0,
        "startedAt": recorder.start_wall if recorder is not None else "",
        "outputDir": str(output_dir()) if output_dir() is not None else "",
        "envDefault": str(os.environ.get(PROBE_ENV, "")).strip().casefold() in _ENABLED_TRUE,
    }


def take_files() -> list[Path]:
    """关闭当前观测日志并把文件交出来，供随抓包一起归档到同一场目录。

    归档要先释放文件句柄：Windows 下打开着的文件不能被移动。关掉之后开关仍然
    保持原状，下一条事件会重新建一个新文件。
    """

    paths: list[Path] = []
    with _INSTANCES_LOCK:
        switches = list(_INSTANCES.values())
    for switch in switches:
        recorder = getattr(switch, "_recorder", None)
        if recorder is None or recorder.closed:
            continue
        path = getattr(recorder, "path", None)
        try:
            recorder.close()
        except Exception:
            pass
        # 刻意不清空 switch._recorder：describe() 还要用它回显行数/大小。
        # close() 之后 _ensure() 会因为 closed=True 自动另起一个新文件。
        if path is not None:
            paths.append(Path(path))
    return paths


def close_all() -> None:
    global _RUNTIME_DEFAULT
    with _INSTANCES_LOCK:
        instances = list(_INSTANCES.values())
        _INSTANCES.clear()
    with _RUNTIME_STATE_LOCK:
        _RUNTIME_DEFAULT = None
    set_output_dir(None)
    for instance in instances:
        try:
            instance.close()
        except Exception:
            pass


def relaxed_skill_candidates(
    payload: bytes,
    actor_ids: set[int],
    *,
    marker: bytes = b"\x0e\x16\x00",
    offset: int = 13,
) -> tuple[int, ...]:
    """按正式解码器同样的规则扫一遍，但**不**用技能白名单过滤。

    正式路径 `decode_v576_boss_skill_starts` 要求 `skill in boss_skills.json`，
    白名单外的技能号会被直接丢掉，所以从抓包里看不见它们。这里只是为了观测：
    同样的 marker / XOR phase / 唯一性判定，只把白名单那一步去掉，用来回答
    「这只 Boss 到底发了哪些技能号」。不参与任何业务判定。
    """

    try:
        from . import protocol as pv

        if not actor_ids or len(payload) < 38:
            return ()
        if not pv._v576_wire_may_contain_marker(payload, (marker,), start_offset=offset):
            return ()
        by_phase: dict[int, list[int]] = {}
        for phase in range(len(pv._V576_BODY_XOR_KEY)):
            body = pv._decode_v576_body(payload, phase)
            found: list[int] = []
            cursor = body.find(marker, offset)
            while cursor >= 0:
                actor = int.from_bytes(body[cursor - 4:cursor], "little")
                skill = int.from_bytes(body[cursor + 3:cursor + 7], "little")
                if cursor + 25 <= len(body) and actor in actor_ids and body[cursor + 7] == 1:
                    found.append(skill)
                cursor = body.find(marker, cursor + 1)
            if found:
                by_phase[phase] = found
        if len(by_phase) != 1:
            return ()
        return tuple(next(iter(by_phase.values())))
    except Exception:
        return ()


def relaxed_skill_records(
    payload: bytes,
    *,
    marker: bytes = b"\x0e\x16\x00",
    offset: int = 13,
) -> tuple[tuple[int, int], ...]:
    """不预设 actor 白名单，扫出 ``(actor, 技能号)`` 全部配对。

    实体表为空时（进本后才开统计）不知道哪个 actor 是 Boss，正式解码器要求
    ``actor in known_boss_actor_ids`` 会整段丢掉。这里只做结构校验：同样的
    marker、XOR 相位唯一性、``mode == 1`` 尾字节，唯一的额外约束是技能号大于 0。

    返回值只用于身份反查（技能号 -> 模板），不参与伤害/归属判定。
    """

    try:
        from . import protocol as pv

        if len(payload) < 38:
            return ()
        if not pv._v576_wire_may_contain_marker(payload, (marker,), start_offset=offset):
            return ()
        by_phase: dict[int, list[tuple[int, int]]] = {}
        for phase in range(len(pv._V576_BODY_XOR_KEY)):
            body = pv._decode_v576_body(payload, phase)
            found: list[tuple[int, int]] = []
            cursor = body.find(marker, offset)
            while cursor >= 0:
                if cursor + 25 <= len(body) and body[cursor + 7] == 1:
                    actor = int.from_bytes(body[cursor - 4:cursor], "little")
                    skill = int.from_bytes(body[cursor + 3:cursor + 7], "little")
                    if 0 < actor < 0xFFFFFFFF and 0 < skill < 0x7FFFFFFF:
                        found.append((actor, skill))
                cursor = body.find(marker, cursor + 1)
            if found:
                by_phase[phase] = found
        if len(by_phase) != 1:
            return ()
        return tuple(next(iter(by_phase.values())))
    except Exception:
        return ()
