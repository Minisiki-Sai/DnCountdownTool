"""调试记录：一个开关同时控制「观测日志」与「保留本轮抓包」。

两者单独使用都不好用：只有观测日志时，事后无法换一种假设重新复算；只有抓包时，
又要人工比对半天才知道哪一环断了。所以它们共用一个开关、一个保存目录。

配置（与 Npcap 配置同级的 ``data`` 目录，便携包整体搬家不丢设置）：

    <app>/data/debug_recording.json
    {"enabled": false, "outputDir": ""}       # outputDir 留空 = 默认目录

输出（``<outputDir>`` 留空时为 ``<文档>/DragonNestDpsCapture/debug_records``，
「文档」按 Windows 已知文件夹解析，重定向到其它盘也能落到正确位置）：

    <outputDir>/sessions/<YYYYmmdd_HHMMSS>/capture_<stamp>.dpscap  # 本轮抓包
    <outputDir>/sessions/<YYYYmmdd_HHMMSS>/probe_<stamp>.jsonl     # 同轮观测日志
    <outputDir>/sessions/<YYYYmmdd_HHMMSS>/<capture>.segments/     # 切片（若有）

一场一个以写入时间命名的目录，抓包和观测日志放在一起，整包拷走就能离线复现。
过程文件仍先写 ``<outputDir>/probe/`` 与 ``%TEMP%``，结束时归档进 session 目录。

关掉开关时行为与以前完全一致：抓包留在 %TEMP% 并在统计结束时删除，观测日志不写。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import nest_probe
from .dpscap_segments import dpscap_segment_directory

CONFIG_FILE_NAME = "debug_recording.json"
ENV_CONFIG_PATH = "DRAGON_NEST_DEBUG_RECORDING_CONFIG"
ENV_OUTPUT_DIR = "DRAGON_NEST_DEBUG_RECORDING_DIR"
_ENABLED_TRUE = {"1", "true", "yes", "on"}
_PORTABLE_ROOT_MARKERS = ("Start-Portable.bat", "third_party", "runtime")
_MAX_PARENT_WALK = 4

_LOCK = threading.RLock()
_STATE: dict[str, Any] | None = None
_LAST_CAPTURE: dict[str, Any] = {"path": "", "at": "", "bytes": 0}
_KEPT_CAPTURES = 0
_ERROR = ""


# --------------------------------------------------------------------- 路径
def _portable_root(start: Path) -> Path | None:
    """向上找到解包后的便携包根目录（与 npcap_config 的判定保持一致）。"""

    for candidate in (start, *list(start.parents)[:_MAX_PARENT_WALK]):
        if (candidate / "Start-Portable.bat").is_file():
            return candidate
        if (candidate / "runtime" / "active-engine.json").is_file():
            return candidate
    return None


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        return _portable_root(executable_dir) or executable_dir
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    configured = str(os.environ.get(ENV_CONFIG_PATH, "")).strip()
    if configured:
        return Path(configured).expanduser()
    return _app_root() / "data" / CONFIG_FILE_NAME


def _documents_dir() -> Path:
    """用户「文档」目录。

    Windows 上不能直接拼 ``~/Documents``：用户手动搬家或 OneDrive 接管都会把
    「文档」重定向到其它盘（注册表 ``User Shell Folders\\Personal``），此时
    ``Path.home()/Documents`` 要么不存在、要么是个空壳目录，抓包就写到没人找
    得到的地方去了。优先按 Shell 已知文件夹解析，取不到才回退默认值。
    """

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                raw, _kind = winreg.QueryValueEx(key, "Personal")
            # 这里通常是 REG_EXPAND_SZ（含 %USERPROFILE%），必须展开。
            resolved = Path(os.path.expandvars(str(raw)))
            if resolved.is_absolute() and resolved.is_dir():
                return resolved
        except (OSError, ValueError, TypeError):
            pass
    return Path.home() / "Documents"


def default_output_dir() -> Path:
    configured = str(os.environ.get(ENV_OUTPUT_DIR, "")).strip()
    if configured:
        return Path(configured).expanduser()
    return _documents_dir() / "DragonNestDpsCapture" / "debug_records"


def _resolve_dir(raw: object) -> Path:
    """配置里的相对路径按应用根目录解析，避免依赖启动时的当前目录。"""

    text = str(raw or "").strip().strip('"')
    if not text:
        return default_output_dir()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = _app_root() / candidate
    return candidate


def probe_dir() -> Path:
    return effective_output_dir() / "probe"


def capture_dir() -> Path:
    """留档根目录：一场一个以写入时间命名的子目录。"""
    return effective_output_dir() / "sessions"


def session_dir(stamp: str | None = None) -> Path:
    """本场留档目录：``<outputDir>/sessions/<YYYYmmdd_HHMMSS>/``。

    抓包和观测日志都落在同一个目录里，整包拷走即可复现，不会出现
    「只有 .dpscap 没有 probe」这种对不上账的情况。
    """

    name = stamp or time.strftime("%Y%m%d_%H%M%S")
    return capture_dir() / name


def effective_output_dir() -> Path:
    return _resolve_dir(_state()["outputDir"])


# --------------------------------------------------------------------- 配置
def _read_config() -> dict[str, Any]:
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "enabled": bool(payload.get("enabled")),
        "outputDir": str(payload.get("outputDir") or "").strip(),
        # 配置文件里没写过 enabled 时，开关仍交给环境变量决定（dev 脚本用）。
        "explicit": "enabled" in payload,
    }


def _write_config(state: dict[str, Any]) -> str:
    path = config_path()
    payload = {
        "enabled": bool(state["enabled"]),
        "outputDir": str(state["outputDir"] or "").strip(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as error:
        return f"配置写入失败：{error}"
    return ""


def _apply_runtime(state: dict[str, Any], *, rotate: bool = False) -> None:
    """把配置反映到观测日志的开关与目录上。绝不让异常冒出去。"""

    directory = _resolve_dir(state["outputDir"])
    try:
        nest_probe.set_output_dir(directory / "probe")
        if state.get("explicit"):
            nest_probe.set_runtime_enabled(bool(state["enabled"]))
        if rotate:
            # 换了目录：关掉旧文件，让后面的记录落到新目录。
            nest_probe.rotate()
        if nest_probe.probe_requested():
            # 立刻建文件，界面上就不会出现「开关是开的，但没有文件」。
            nest_probe.ensure_started()
    except Exception:
        pass


def _state() -> dict[str, Any]:
    global _STATE
    with _LOCK:
        if _STATE is None:
            _STATE = _read_config()
            _apply_runtime(_STATE)
        return _STATE


def ensure_applied() -> None:
    """后端启动时调用一次：让上次保存的开关在第一次抓包前就生效。"""

    _state()


def enabled() -> bool:
    state = _state()
    if state.get("explicit"):
        return bool(state["enabled"])
    # 配置里还没写过开关时，跟随环境变量（dev 脚本 / 命令行启动）。
    try:
        return bool(nest_probe.probe_requested())
    except Exception:
        return False


def keep_capture_enabled() -> bool:
    """采集结束时是否保留原始抓包。这个函数必须足够便宜且绝不抛异常。"""

    try:
        return bool(_state()["enabled"])
    except Exception:
        return False


# --------------------------------------------------------------------- 抓包保留
def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def note_kept_capture(path: Path) -> None:
    global _KEPT_CAPTURES
    size = 0
    try:
        size = int(Path(path).stat().st_size)
    except OSError:
        size = 0
    with _LOCK:
        _KEPT_CAPTURES += 1
        _LAST_CAPTURE.update(
            path=str(path),
            at=time.strftime("%Y-%m-%d %H:%M:%S"),
            bytes=size,
        )


def retain_capture(source: Path) -> bool:
    """把本轮抓包移到保存目录；失败返回 False，由调用方回退到删除。"""

    global _ERROR
    source = Path(source)
    if not source.is_file():
        return False
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # 一场一个目录：抓包和观测日志放一起，方便整包留档/发给别人复现。
        target_dir = _unique_path(session_dir(stamp))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"capture_{stamp}.dpscap"
        shutil.move(str(source), str(target))
        segments = dpscap_segment_directory(source)
        if segments.is_dir():
            try:
                shutil.move(str(segments), str(dpscap_segment_directory(target)))
            except OSError:
                pass
        # 观测日志随抓包归档。必须先关句柄，Windows 下打开的文件移不动。
        for probe_path in nest_probe.take_files():
            try:
                shutil.move(str(probe_path), str(target_dir / probe_path.name))
            except OSError:
                pass
    except OSError as error:
        with _LOCK:
            _ERROR = f"抓包保存失败：{error}"
        return False
    with _LOCK:
        _ERROR = ""
    note_kept_capture(target)
    try:
        print(f"[debug-recording] 已保留抓包：{target}", flush=True)
    except Exception:
        pass
    return True


# --------------------------------------------------------------------- 对外
def apply(enabled: bool | None = None, output_dir: str | None = None) -> dict[str, Any]:
    """修改开关/保存目录并立即生效。目录不可写时抛 ``ValueError``。"""

    global _STATE, _ERROR
    dir_changed = False
    with _LOCK:
        state = dict(_state())
        if output_dir is not None:
            raw = str(output_dir or "").strip()
            candidate = _resolve_dir(raw)
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ValueError(f"无法创建保存目录：{error}") from error
            dir_changed = raw != str(state.get("outputDir") or "")
            state["outputDir"] = raw
        if enabled is not None:
            state["enabled"] = bool(enabled)
        state["explicit"] = True
        _ERROR = _write_config(state)
        _STATE = state
    _apply_runtime(state, rotate=dir_changed)
    return describe()


def describe() -> dict[str, Any]:
    state = _state()
    probe = nest_probe.describe()
    directory = _resolve_dir(state["outputDir"])
    with _LOCK:
        last = dict(_LAST_CAPTURE)
        kept = _KEPT_CAPTURES
        error = _ERROR
    return {
        "enabled": enabled(),
        "configuredDir": str(state["outputDir"] or ""),
        "outputDir": str(directory),
        "defaultDir": str(default_output_dir()),
        "probeDir": str(directory / "probe"),
        "captureDir": str(directory / "capture"),
        "configPath": str(config_path()),
        "probeEnabled": bool(probe.get("enabled")),
        "probePath": str(probe.get("path") or ""),
        "probeRows": int(probe.get("rows") or 0),
        "probeBytes": int(probe.get("bytes") or 0),
        "probeStartedAt": str(probe.get("startedAt") or ""),
        "lastCapturePath": last["path"],
        "lastCaptureAt": last["at"],
        "lastCaptureBytes": int(last["bytes"]),
        "keptCaptures": kept,
        "error": error,
    }


def probe_state() -> dict[str, Any]:
    """兼容旧的 /api/probe：返回观测日志自身的状态。"""

    return nest_probe.describe()


def _reset_for_tests() -> None:
    global _STATE, _KEPT_CAPTURES, _ERROR
    with _LOCK:
        _STATE = None
        _KEPT_CAPTURES = 0
        _ERROR = ""
        _LAST_CAPTURE.update(path="", at="", bytes=0)
