"""Npcap path configuration for the portable countdown build.

The tool keeps exactly one path setting: where ``wpcap.dll`` (or the Npcap
installation directory) lives. Resolution order is

1. the explicitly configured path (this module),
2. the app-local / portable copy (``third_party/npcap``),
3. the system Npcap installation (``%SystemRoot%\\System32\\Npcap``).

The configuration file is stored at the portable application root
(``<app>/data/npcap.json``) so a green copy keeps its settings when the folder
moves and survives engine upgrades (each engine release lives in its own
``runtime/engines/<id>`` directory). Legacy locations - the engine directory
and ``%LOCALAPPDATA%\\DragonNestACT`` - are still read for migration.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

CONFIG_FILE_NAME = "npcap.json"
_ENV_OVERRIDE = "DRAGON_NEST_NPCAP_DLL"
_PORTABLE_ROOT_MARKERS = ("Start-Portable.bat", "third_party", "runtime")
_MAX_PARENT_WALK = 4


def _portable_root(start: Path) -> Path | None:
    """Walk up from *start* looking for the unpacked portable folder root."""
    for candidate in (start, *list(start.parents)[:_MAX_PARENT_WALK]):
        runtime = candidate / "runtime"
        if (candidate / "Start-Portable.bat").is_file():
            return candidate
        if (runtime / "active-engine.json").is_file():
            return candidate
    return None


def _app_root() -> Path:
    """Directory that owns the portable ``data`` folder."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        return _portable_root(executable_dir) or executable_dir
    return Path(__file__).resolve().parents[2]


def _legacy_config_paths() -> tuple[Path, ...]:
    """Configurations written by earlier builds (engine dir / per-user)."""
    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / "data" / CONFIG_FILE_NAME)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    paths.append(local_app_data / "DragonNestACT" / CONFIG_FILE_NAME)
    return tuple(paths)


def _candidate_config_paths() -> tuple[Path, ...]:
    return (_app_root() / "data" / CONFIG_FILE_NAME, *_legacy_config_paths())


def config_path() -> Path:
    """Return the configuration location used for writes.

    A legacy file that already carries a value is migrated to the portable
    root so the setting is not lost on the next engine upgrade. The portable
    root always wins when it is writable.
    """
    primary = _candidate_config_paths()[0]
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        return primary
    except OSError:
        pass
    for path in _candidate_config_paths()[1:]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return primary


def _read_first_existing() -> str:
    """The first readable configuration wins, including an explicit empty one.

    Stopping at the first *parseable* file is what makes "恢复自动检测" work:
    an empty value at the portable root must not fall through to a stale
    legacy file that still points at an old Npcap copy.
    """
    for path in _candidate_config_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return str(payload.get("path") or "").strip()
    return ""


def read_config() -> dict[str, str]:
    return {"path": _read_first_existing()}


def write_config_path(raw: str) -> dict[str, str]:
    path = config_path()
    payload = {"path": str(raw or "").strip().strip('"')}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return payload
    return payload


def _dll_in_directory(directory: Path) -> Path | None:
    for candidate in (
        directory,
        directory / "wpcap.dll",
        directory / "x64" / "wpcap.dll",
        directory / "Npcap" / "wpcap.dll",
    ):
        if candidate.is_file() and candidate.name.casefold() == "wpcap.dll":
            return candidate
    return None


def validate_path(raw: str) -> tuple[Path | None, str]:
    """Resolve a user supplied path.

    Returns ``(dll, error)``. An empty or missing value yields a neutral
    message so the caller can fall back to automatic detection.
    """
    value = str(raw or "").strip().strip('"')
    if not value:
        return None, ""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        if candidate.name.casefold() == "wpcap.dll":
            return candidate, ""
        return None, "该文件不是 wpcap.dll，请选择 Npcap 的 wpcap.dll 或安装目录。"
    if candidate.is_dir():
        found = _dll_in_directory(candidate)
        if found is not None:
            return found, ""
        return None, "该目录下没有 wpcap.dll，请选择 Npcap 安装目录（通常为 C:\\Windows\\System32\\Npcap）。"
    return None, "路径不存在，请检查是否拼写错误，或改用「选择 DLL / 选择目录」重新指定。"


def _portable_directories() -> tuple[Path, ...]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root))
    roots.append(_app_root())
    directories: list[Path] = []
    for root in roots:
        for relative in (Path("third_party") / "npcap", Path("runtime") / "npcap", Path("npcap")):
            directory = root / relative
            if directory not in directories:
                directories.append(directory)
    return tuple(directories)


def _system_candidates() -> tuple[Path, ...]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return (
        system_root / "System32" / "Npcap" / "wpcap.dll",
        system_root / "System32" / "wpcap.dll",
    )


def configured_dll() -> Path | None:
    """The manually configured DLL, if it still resolves."""
    override = os.environ.get(_ENV_OVERRIDE, "").strip().strip('"')
    raw = override or read_config().get("path", "")
    if not raw:
        return None
    resolved, _error = validate_path(raw)
    return resolved


def resolve_dll() -> tuple[Path | None, str]:
    """Return ``(dll, source)`` where source is configured/portable/system/missing."""
    configured = configured_dll()
    if configured is not None:
        return configured, "configured"
    for directory in _portable_directories():
        found = _dll_in_directory(directory)
        if found is not None:
            return found, "portable"
    for candidate in _system_candidates():
        if candidate.is_file():
            return candidate, "system"
    return None, "missing"


def detect_candidates() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        if any(row["path"].casefold() == str(path).casefold() for row in rows):
            return
        rows.append({"path": str(path), "source": source})

    add(configured_dll(), "configured")
    for directory in _portable_directories():
        add(_dll_in_directory(directory), "portable")
    for candidate in _system_candidates():
        if candidate.is_file():
            add(candidate, "system")
    return rows


class _PcapIf(ctypes.Structure):
    pass


_PcapIf._fields_ = [
    ("next", ctypes.POINTER(_PcapIf)),
    ("name", ctypes.c_char_p),
    ("description", ctypes.c_char_p),
    ("addresses", ctypes.c_void_p),
    ("flags", ctypes.c_uint),
]


def _load_and_probe(dll_path: Path) -> dict[str, object]:
    """Load wpcap.dll, enumerate interfaces and try one live capture handle."""
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(dll_path.parent))
        except OSError:
            pass
    try:
        pcap = ctypes.CDLL(str(dll_path))
    except OSError as error:
        return {"ok": False, "stage": "load", "message": str(error), "adapterCount": 0}

    error_buffer = ctypes.create_string_buffer(256)
    devices = ctypes.POINTER(_PcapIf)()
    pcap.pcap_findalldevs.argtypes = [ctypes.POINTER(ctypes.POINTER(_PcapIf)), ctypes.c_char_p]
    pcap.pcap_findalldevs.restype = ctypes.c_int
    if pcap.pcap_findalldevs(ctypes.byref(devices), error_buffer) != 0:
        message = error_buffer.value.decode("utf-8", "replace") or "pcap_findalldevs 失败"
        stage = "permission" if _looks_like_permission(message) else "adapter"
        return {"ok": False, "stage": stage, "message": message, "adapterCount": 0}

    names: list[str] = []
    try:
        current = devices
        while current:
            entry = current.contents
            if entry.name:
                names.append(entry.name.decode("utf-8", "replace"))
            current = entry.next
    finally:
        pcap.pcap_freealldevs.argtypes = [ctypes.POINTER(_PcapIf)]
        pcap.pcap_freealldevs(devices)

    if not names:
        return {"ok": False, "stage": "adapter", "message": "没有任何可用的抓包接口", "adapterCount": 0}

    pcap.pcap_open_live.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
    ]
    pcap.pcap_open_live.restype = ctypes.c_void_p
    pcap.pcap_close.argtypes = [ctypes.c_void_p]
    open_error = ctypes.create_string_buffer(256)
    handle = None
    for name in names:
        handle = pcap.pcap_open_live(name.encode("utf-8"), 65536, 0, 1000, open_error)
        if handle:
            break
    if not handle:
        message = open_error.value.decode("utf-8", "replace") or "无法打开抓包接口"
        stage = "permission" if _looks_like_permission(message) else "adapter"
        return {"ok": False, "stage": stage, "message": message, "adapterCount": len(names)}
    pcap.pcap_close(handle)
    return {"ok": True, "stage": "", "message": "", "adapterCount": len(names)}


def _looks_like_permission(message: str) -> bool:
    lowered = message.casefold()
    return any(token in lowered for token in ("permission", "access", "denied", "管理员", "权限"))


def test_connection() -> dict[str, object]:
    dll_path, source = resolve_dll()
    if dll_path is None:
        return {
            "ok": False,
            "stage": "load",
            "message": "未找到 wpcap.dll：随包目录与系统目录均不存在",
            "adapterCount": 0,
            "resolvedPath": "",
            "source": source,
        }
    result = _load_and_probe(dll_path)
    result.update({"resolvedPath": str(dll_path), "source": source})
    return result


def status_payload() -> dict[str, object]:
    configured_raw = read_config().get("path", "")
    configured_path, configured_error = validate_path(configured_raw)
    dll_path, source = resolve_dll()
    if dll_path is None:
        return {
            "configuredPath": configured_raw,
            "configuredValid": configured_path is not None,
            "configPath": str(config_path()),
            "resolvedPath": "",
            "source": "missing",
            "installed": False,
            "adapterCount": 0,
            "message": configured_error or "未找到 Npcap：请指定 wpcap.dll，或安装随包 / 官方 Npcap。",
        }
    if configured_error:
        message = f"{configured_error} 当前仍使用 {source_label(source)}。"
    else:
        message = {
            "configured": "已使用手动指定的 Npcap 路径。",
            "portable": "已使用随包 Npcap 组件。",
            "system": "已使用系统安装的 Npcap。",
            "missing": "",
        }[source]
    return {
        "configuredPath": configured_raw,
        "configuredValid": configured_path is not None,
        "configPath": str(config_path()),
        "resolvedPath": str(dll_path),
        "source": source,
        "installed": True,
        "adapterCount": 0,
        "message": message,
    }


def source_label(source: str) -> str:
    return {
        "configured": "手动指定的 Npcap",
        "portable": "随包 Npcap",
        "system": "系统安装的 Npcap",
    }.get(source, "未找到的 Npcap")
