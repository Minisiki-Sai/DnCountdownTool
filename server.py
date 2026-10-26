from __future__ import annotations

import argparse
import ctypes
import faulthandler
import http.client
import json
import multiprocessing
import os
import platform
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


_BACKEND_DIAGNOSTIC_HANDLE: Any | None = None
_BACKEND_DIAGNOSTIC_PATH: Path | None = None
_BACKEND_STATE_STOP = threading.Event()
_BACKEND_STATE_THREAD: threading.Thread | None = None
_BACKEND_STARTED_MONOTONIC = time.monotonic()
_WINDOWS_EXCEPTION_HANDLER: Any | None = None


def _crash_diagnostics_directory() -> Path:
    """Return a stable, user-shareable crash diagnostics directory."""

    path = (
        Path.home()
        / "Documents"
        / "DragonNestDpsCapture"
        / "crash_diagnostics"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_backend_diagnostic(
    context: str,
    error: BaseException | None = None,
) -> None:
    handle = _BACKEND_DIAGNOSTIC_HANDLE
    if handle is None:
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"\n[{stamp}] {context}\n")
        if error is not None:
            traceback.print_exception(
                type(error), error, error.__traceback__, file=handle
            )
        handle.flush()
    except Exception:
        # Diagnostics must never turn a recoverable application error into a
        # second failure.
        pass


def _install_windows_backend_minidump() -> None:
    """Write a minimal native dump when the frozen backend terminates fatally."""

    global _WINDOWS_EXCEPTION_HANDLER
    if os.name != "nt":
        return
    try:
        dbghelp = ctypes.WinDLL("DbgHelp.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        dbghelp.MiniDumpWriteDump.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL

        class MiniDumpExceptionInformation(ctypes.Structure):
            _fields_ = [
                ("ThreadId", wintypes.DWORD),
                ("ExceptionPointers", ctypes.c_void_p),
                ("ClientPointers", wintypes.BOOL),
            ]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

        def exception_handler(exception_pointers: int) -> int:
            invalid_handle = ctypes.c_void_p(-1).value
            handle = invalid_handle
            try:
                stamp = int(time.time() * 1000)
                desktop_pid = os.getenv("DRAGON_NEST_DESKTOP_PID", "") or "unknown"
                path = _crash_diagnostics_directory() / (
                    f"backend-native-crash_{desktop_pid}_{stamp}_{os.getpid()}.dmp"
                )
                handle = kernel32.CreateFileW(
                    str(path), 0x40000000, 0, None, 2, 0x80, None
                )
                if handle == invalid_handle:
                    return 0
                info = MiniDumpExceptionInformation(
                    kernel32.GetCurrentThreadId(),
                    ctypes.c_void_p(exception_pointers),
                    False,
                )
                dbghelp.MiniDumpWriteDump(
                    kernel32.GetCurrentProcess(),
                    os.getpid(),
                    handle,
                    0x1000,  # MiniDumpWithThreadInfo | MiniDumpNormal
                    ctypes.byref(info) if exception_pointers else None,
                    None,
                    None,
                )
            except Exception:
                pass
            finally:
                try:
                    if handle not in {None, invalid_handle}:
                        kernel32.CloseHandle(handle)
                except Exception:
                    pass
            return 0

        _WINDOWS_EXCEPTION_HANDLER = callback_type(exception_handler)
        kernel32.SetUnhandledExceptionFilter(_WINDOWS_EXCEPTION_HANDLER)
    except Exception:
        _WINDOWS_EXCEPTION_HANDLER = None


def _initialize_crash_diagnostics() -> None:
    """Capture Python, worker-thread and native-extension failures."""

    global _BACKEND_DIAGNOSTIC_HANDLE, _BACKEND_DIAGNOSTIC_PATH
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = _crash_diagnostics_directory() / (
            f"backend_{stamp}_{os.getpid()}.log"
        )
        handle = path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return
    _BACKEND_DIAGNOSTIC_HANDLE = handle
    _BACKEND_DIAGNOSTIC_PATH = path
    handle.write(
        "Dragon Nest Battle Platform backend diagnostics\n"
        f"started={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"pid={os.getpid()}\n"
        f"parent_pid={os.getppid()}\n"
        f"desktop_pid={os.getenv('DRAGON_NEST_DESKTOP_PID', '')}\n"
        f"process_name={multiprocessing.current_process().name}\n"
        f"python={sys.version.replace(chr(10), ' ')}\n"
        f"platform={platform.platform()}\n"
        f"executable={sys.executable}\n"
    )
    handle.flush()
    try:
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, RuntimeError):
        pass
    _install_windows_backend_minidump()

    previous_sys_hook = sys.excepthook

    def sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
    ) -> None:
        _write_backend_diagnostic("Uncaught main-thread exception")
        try:
            traceback.print_exception(
                exc_type, exc_value, exc_traceback, file=handle
            )
            handle.flush()
        finally:
            previous_sys_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = sys_hook

    previous_thread_hook = threading.excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        _write_backend_diagnostic(
            f"Uncaught worker-thread exception: {args.thread.name if args.thread else 'unknown'}"
        )
        try:
            traceback.print_exception(
                args.exc_type, args.exc_value, args.exc_traceback, file=handle
            )
            handle.flush()
        finally:
            previous_thread_hook(args)

    threading.excepthook = thread_hook

    previous_unraisable_hook = sys.unraisablehook

    def unraisable_hook(args: sys.UnraisableHookArgs) -> None:
        _write_backend_diagnostic(
            f"Unraisable exception: {getattr(args, 'err_msg', '') or 'unknown'}",
            args.exc_value,
        )
        previous_unraisable_hook(args)

    sys.unraisablehook = unraisable_hook


if multiprocessing.current_process().name == "MainProcess":
    _initialize_crash_diagnostics()


def _stable_runtime_root() -> Path:
    """Resolve the backend root independently of the caller's working dir.

    Explorer shortcuts commonly start a process with the Desktop (or
    ``System32``) as its current directory.  The frozen backend must instead
    use the directory containing its bundled Python files.
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    try:
        candidates.append(Path(__file__).resolve().parent)
    except OSError:
        pass
    for candidate in candidates:
        if (candidate / "engine").is_dir():
            return candidate
        internal = candidate / "_internal"
        if (internal / "engine").is_dir():
            return internal
    return candidates[0] if candidates else Path.cwd()


# Normalize the backend process before importing modules whose resource paths
# are initialized at import time. This makes shortcut launches equivalent to
# launches from the installation directory.
_RUNTIME_ROOT = _stable_runtime_root()
try:
    os.chdir(_RUNTIME_ROOT)
except OSError:
    pass

_FALLBACK_APP_VERSION = "5.0"


def _version_from_file() -> str:
    """Read the single version source shipped next to the engine.

    ``version.json`` at the project/installation root is the one file a human
    edits when cutting a release; ``tools/sync_version.py`` propagates it into
    the four packaging manifests.  Reading it here as well means a bare
    ``python server.py`` launch (no desktop shell injecting the version) still
    reports the release it was built from instead of a stale literal.

    NOTE for anyone restoring the PyInstaller build: ``version.json`` lives
    outside the ``engine`` package, so the spec's ``datas`` must include
    ``('version.json', '.')``.  Without that line a frozen engine silently
    falls back to ``_FALLBACK_APP_VERSION``.
    """

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "version.json")
    candidates.append(_RUNTIME_ROOT / "version.json")
    candidates.append(Path(__file__).resolve().parent / "version.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（例如被误写成数组）时跳过，
            # 否则这里会在 import 阶段抛 AttributeError，把整个后端起不来。
            continue
        for key in ("engine_protocol", "product"):
            value = str(payload.get(key) or "").strip()
            if value:
                # 只在三段式 x.y.0 时去掉末尾 .0（4.88.0 -> 4.88）。
                # 不能无条件 removesuffix(".0")：那样 5.0 会被削成 5。
                return re.sub(r"^(\d+\.\d+)\.0$", r"\1", value)
    return ""


def _runtime_app_version() -> str:
    """Use the desktop shell's package version as the update authority.

    Tauri injects ``DRAGON_NEST_APP_VERSION`` whenever it starts either the
    development backend or the frozen engine. Keeping a second hard-coded
    release number here previously made a newer desktop report itself as an
    older release, so the updater offered the already-installed version forever.
    The fallback only serves direct or standalone backend launches, and it now
    reads ``version.json`` instead of a literal so the two cannot drift.
    """

    injected = str(os.environ.get("DRAGON_NEST_APP_VERSION") or "").strip()
    return injected or _version_from_file() or _FALLBACK_APP_VERSION


VERSION = _runtime_app_version()
os.environ["DRAGON_NEST_APP_VERSION"] = VERSION

# The desktop and Python engine can be rebuilt at different times while the
# public app version remains unchanged.  Do not treat an older engine as
# compatible merely because both report the same public version; otherwise a
# newly built frontend can silently reuse a backend which lacks newer routes.
# Bump this value whenever the local HTTP contract changes.
BACKEND_PRODUCT = "dragon-nest-battle-platform"
BACKEND_API_REVISION = "20261023-countdown-only"
# Advertised so a freshly built frontend never silently pairs with an older
# engine.  The countdown build exposes capture control, the two overlay
# snapshots, voice events and the Npcap path - nothing else.
BACKEND_API_CAPABILITIES = frozenset(
    {
        "capture-control-v1",
        "capture-toggles-v1",
        "overlay-buff-debuff-v1",
        "overlay-boss-skill-v1",
        "overlay-wait-v1",
        "voice-events-v1",
        "npcap-path-v2",
    }
)

from engine import npcap_config

# 开发者模式「回放实战录像」的后台作业。解码一次几十秒，放在后台线程里跑，
# 前端轮询进度，避免请求线程被卡住看起来像死机。
_REPLAY_JOBS: Any | None = None
_REPLAY_JOBS_LOCK = threading.Lock()


def _replay_jobs() -> Any:
    global _REPLAY_JOBS
    with _REPLAY_JOBS_LOCK:
        if _REPLAY_JOBS is None:
            from engine.live.replay_jobs import ReplayJobManager

            _REPLAY_JOBS = ReplayJobManager(Path(__file__).resolve().parent / "engine" / "data")
        return _REPLAY_JOBS


def _replay_capture_roots() -> list[Path]:
    """能从哪里挑录像：工具自己的录制目录优先，再兜一层用户目录。"""

    from engine.live import debug_recording

    roots = [debug_recording.effective_output_dir()]
    roots.append(Path.home() / "Documents" / "DragonNestDpsCapture" / "debug_records")
    return [path for path in roots if path.is_dir()]


# The capture core pulls in the protocol catalogs and the whole ``live``
# package (a couple of seconds of imports). Import it inside the warm-up
# thread instead of at module scope so the HTTP socket is listening - and the
# desktop window usable - within a few hundred milliseconds of launch.


def _engine_classes():
    from engine.calculator import BuildCalculator
    from engine.data_loader import DataStore
    from engine.core import NativeSimulatorCore

    return BuildCalculator, DataStore, NativeSimulatorCore


def as_number(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


HOST = "127.0.0.1"
DEFAULT_PORT = 48710
BACKEND_PROBE_TIMEOUT_SECONDS = 0.35
BACKEND_TAKEOVER_POLL_SECONDS = 0.5


def _backend_state_path() -> Path:
    desktop_pid = os.getenv("DRAGON_NEST_DESKTOP_PID", "") or "standalone"
    return _crash_diagnostics_directory() / (
        f"backend-state-{desktop_pid}-{os.getpid()}.json"
    )


def _compact_capture_diagnostic(engine: "Engine") -> dict[str, Any]:
    try:
        raw = engine.native.capture_status(False)
    except Exception as exc:
        return {"state": "unavailable", "error": type(exc).__name__}
    allowed = (
        "state", "status", "progress", "elapsedSeconds", "scoreReady",
        "capturePackets", "decodedPackets", "droppedPackets", "queueDepth",
        "autoEnabled", "npcapInstalled", "overlayLatency", "stopDiagnostics",
    )
    result = {key: raw.get(key) for key in allowed if key in raw}
    source_file = str(raw.get("sourceFile") or raw.get("file") or "").strip()
    if source_file:
        result["sourceFileName"] = Path(source_file).name
    return result


def _write_backend_state(engine: "Engine", *, phase: str) -> None:
    try:
        payload = {
            "schema": "dragon-nest-backend-state-v1",
            "version": VERSION,
            "phase": phase,
            "pid": os.getpid(),
            "desktopPid": os.getenv("DRAGON_NEST_DESKTOP_PID", ""),
            "recordedEpochMs": int(time.time() * 1000),
            "uptimeSeconds": round(time.monotonic() - _BACKEND_STARTED_MONOTONIC, 3),
            "threads": sorted(
                thread.name for thread in threading.enumerate() if thread.is_alive()
            ),
            "capture": _compact_capture_diagnostic(engine),
        }
        destination = _backend_state_path()
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        pass


def _backend_state_loop(engine: "Engine") -> None:
    while not _BACKEND_STATE_STOP.wait(5.0):
        _write_backend_state(engine, phase="running")


def _start_backend_state_heartbeat(engine: "Engine") -> None:
    global _BACKEND_STATE_THREAD
    _BACKEND_STATE_STOP.clear()
    _write_backend_state(engine, phase="ready")
    _BACKEND_STATE_THREAD = threading.Thread(
        target=_backend_state_loop,
        args=(engine,),
        name="CrashDiagnosticHeartbeat",
        daemon=True,
    )
    _BACKEND_STATE_THREAD.start()


def _stop_backend_state_heartbeat(engine: "Engine" | None) -> None:
    _BACKEND_STATE_STOP.set()
    thread = _BACKEND_STATE_THREAD
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=0.75)
    if engine is not None:
        _write_backend_state(engine, phase="stopped-normally")


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can allow an old and a new development backend
    # to share the port. Requests then land on different code versions.
    allow_reuse_address = False


def _probe_backend(host: str, port: int) -> dict[str, Any] | None:
    """Return the health payload exposed by a local backend, if available."""

    connection = http.client.HTTPConnection(
        host,
        port,
        timeout=BACKEND_PROBE_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if response.status != HTTPStatus.OK or not isinstance(payload, dict):
            return None
        return payload
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


def _compatible_backend_is_running(host: str, port: int) -> bool:
    """Return whether the occupied endpoint has the exact local API contract."""

    payload = _probe_backend(host, port)
    return bool(
        payload
        and payload.get("ok") is True
        and str(payload.get("product") or "") == BACKEND_PRODUCT
        and str(payload.get("version") or "") == VERSION
        and str(payload.get("apiRevision") or "") == BACKEND_API_REVISION
    )


def _replaceable_stale_backend(payload: dict[str, Any] | None) -> bool:
    """Recognize an older build of this backend without trusting unrelated apps."""

    if not payload or payload.get("ok") is not True:
        return False
    product = str(payload.get("product") or "")
    if product:
        return product == BACKEND_PRODUCT
    # V4.24-V4.434 health payloads predate the product/API revision fields but
    # already exposed this unique marker.  Restrict the fallback to the same
    # public version so an unrelated or separately installed release is not
    # stopped by accident.
    return (
        payload.get("windowFree") is True
        and str(payload.get("version") or "") == VERSION
    )


def _request_stale_backend_shutdown(host: str, port: int) -> bool:
    """Ask a recognized stale backend to release the shared local API port."""

    connection = http.client.HTTPConnection(
        host,
        port,
        timeout=max(0.5, BACKEND_PROBE_TIMEOUT_SECONDS),
    )
    try:
        connection.request("POST", "/api/shutdown", body=b"")
        response = connection.getresponse()
        response.read()
        return response.status == HTTPStatus.OK
    except OSError:
        # The process may close the listener before the response is read.  The
        # bind retry below is the final authority, so this is still progress.
        return True
    finally:
        connection.close()


def _address_is_in_use(error: OSError) -> bool:
    return error.errno in {48, 98, 10048} or getattr(error, "winerror", None) == 10048


def _bind_server_or_wait_for_takeover(
    host: str,
    port: int,
) -> ExclusiveThreadingHTTPServer:
    reused_existing = False
    stale_shutdown_requested = False
    while True:
        try:
            server = ExclusiveThreadingHTTPServer((host, port), ApiHandler)
            if reused_existing:
                print(
                    f"The previous V{VERSION} backend stopped; this process now owns "
                    f"http://{host}:{port}",
                    flush=True,
                )
            return server
        except OSError as error:
            if not _address_is_in_use(error):
                raise
            payload = _probe_backend(host, port)
            compatible = bool(
                payload
                and payload.get("ok") is True
                and str(payload.get("product") or "") == BACKEND_PRODUCT
                and str(payload.get("version") or "") == VERSION
                and str(payload.get("apiRevision") or "")
                == BACKEND_API_REVISION
            )
            if not compatible and _replaceable_stale_backend(payload):
                if not stale_shutdown_requested:
                    if not _request_stale_backend_shutdown(host, port):
                        raise RuntimeError(
                            f"V{VERSION} stale local API could not be stopped."
                        ) from error
                    print(
                        f"A stale V{VERSION} backend is using port {port}; "
                        "requesting a graceful API-contract takeover.",
                        flush=True,
                    )
                    stale_shutdown_requested = True
                time.sleep(BACKEND_TAKEOVER_POLL_SECONDS)
                continue
            if not compatible:
                raise RuntimeError(
                    f"V{VERSION} local API port {port} is occupied by another program."
                ) from error
            if not reused_existing:
                print(
                    f"A compatible V{VERSION} backend is already listening on "
                    f"http://{host}:{port}; reusing it and waiting for takeover.",
                    flush=True,
                )
                reused_existing = True
            time.sleep(BACKEND_TAKEOVER_POLL_SECONDS)


class Engine:
    """Window-free application service: capture core only."""

    def __init__(self) -> None:
        started = time.perf_counter()
        # The capture core needs the data store and a calculator instance for
        # protocol catalogs and profile labels; no HTTP route exposes build
        # calculation any more.
        BuildCalculator, DataStore, NativeSimulatorCore = _engine_classes()
        self.store = DataStore.load()
        self.calculator = BuildCalculator(self.store)
        self.native = NativeSimulatorCore(self.store, self.calculator)
        # The parallel score pool is created lazily by the first calculation.
        # Starting all workers here made an idle application occupy up to 16
        # extra processes before the user opened team scoring.
        self.loaded_ms = round((time.perf_counter() - started) * 1000, 1)
        self._lock = threading.RLock()

    def shutdown(self) -> None:
        self.native.shutdown()

ENGINE: Engine | None = None


_ENGINE_LOCK = threading.Lock()
_ENGINE_READY = threading.Event()


def get_engine() -> Engine:
    """Build the capture engine at most once, even under concurrent requests."""
    global ENGINE
    if ENGINE is not None:
        return ENGINE
    with _ENGINE_LOCK:
        if ENGINE is None:
            t0 = time.perf_counter()
            ENGINE = Engine()
            _ENGINE_READY.set()
            print(
                f"Engine ready in {time.perf_counter() - t0:.2f}s",
                flush=True,
            )
    return ENGINE


def engine_ready() -> bool:
    return _ENGINE_READY.is_set() and ENGINE is not None


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DragonNestV420/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[v{VERSION}-api] {self.address_string()} {fmt % args}")

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Capture-Name")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _write_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json_bytes(
        self, data: bytes, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Windows PowerShell 5 sends string request bodies with the
            # active ANSI code page unless a byte array is supplied.
            text = raw.decode("gb18030")
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}

    def _handle_error(self, error: Exception) -> None:
        try:
            self._write_json(
                {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
        except Exception:  # noqa: BLE001
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """Only the countdown overlays talk to this engine.

        The surface is deliberately tiny: health, capture state, the two
        overlay snapshots, voice events and the Npcap path setting.  Anything
        else the old combat tracker exposed (builds, teams, scoring, ranking,
        history, telemetry) has been removed from the product.
        """
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            # Health answers immediately: the engine still needs a few seconds
            # to load its protocol catalogs, and the window must not look dead
            # while that happens.
            if parsed.path == "/api/health":
                self._write_json(
                    {
                        "ok": True,
                        "ready": engine_ready(),
                        "product": BACKEND_PRODUCT,
                        "version": VERSION,
                        "apiRevision": BACKEND_API_REVISION,
                        "apiCapabilities": sorted(BACKEND_API_CAPABILITIES),
                        "windowFree": True,
                    }
                )
                return
            core = get_engine().native
            if parsed.path == "/api/capture/status":
                if not engine_ready():
                    # Never park the request behind the engine warm-up: the
                    # window polls this every second and must stay responsive.
                    self._write_json(
                        {
                            "state": "starting",
                            "status": "正在初始化解析核心…",
                            "autoEnabled": False,
                        }
                    )
                    return
                include_snapshot = query.get("includeSnapshot", ["1"])[0] != "0"
                self._write_json(core.capture_status(include_snapshot))
                return
            if parsed.path == "/api/capture/voice-events":
                self._write_json(core.capture_voice_events())
                return
            if parsed.path == "/api/replay/captures":
                self._write_json(_replay_jobs().captures(*_replay_capture_roots()))
                return
            if parsed.path == "/api/replay/status":
                job = (query.get("job") or [""])[0]
                self._write_json(_replay_jobs().status(job))
                return
            if parsed.path == "/api/capture/timeline":
                try:
                    after_sequence = int(query.get("afterSequence", ["0"])[0] or 0)
                except (TypeError, ValueError):
                    after_sequence = 0
                self._write_json(core.capture_timeline(after_sequence))
                return
            if parsed.path == "/api/capture/buff-overlay/wait":
                try:
                    after_revision = int(
                        query.get("afterRevision", ["0"])[0] or 0
                    )
                except (TypeError, ValueError):
                    after_revision = 0
                try:
                    timeout_ms = int(query.get("timeoutMs", ["15000"])[0] or 15000)
                except (TypeError, ValueError):
                    timeout_ms = 15000
                self._write_json(
                    core.wait_buff_overlay_change(after_revision, timeout_ms)
                )
                return
            if parsed.path == "/api/capture/buff-overlay":
                include_catalog = query.get("includeCatalog", ["1"])[0] in {
                    "1", "true", "yes"
                }
                self._write_json_bytes(
                    core.buff_overlay_status_json(include_catalog)
                )
                return
            if parsed.path == "/api/capture/toggles":
                if not engine_ready():
                    self._write_json(
                        {
                            "toggles": {"buffs": True, "boss_actions": True},
                            "labels": {
                                "buffs": "增益 / 减益倒计时",
                                "boss_actions": "Boss 技能 / 动作倒计时",
                            },
                        }
                    )
                    return
                self._write_json(core.capture_toggles())
                return
            if parsed.path == "/api/probe":
                # 观测日志开关：只记录事实，不影响任何统计/播报判定。
                from engine.live import nest_probe

                self._write_json(nest_probe.describe())
                return
            if parsed.path == "/api/debug-recording":
                # 设置页「调试记录」：一个开关同时控制观测日志与保留抓包。
                from engine.live import debug_recording

                self._write_json(debug_recording.describe())
                return
            if parsed.path == "/api/npcap/config":
                self._write_json(npcap_config.status_payload())
                return
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except Exception as error:  # noqa: BLE001
            self._handle_error(error)

    def do_POST(self) -> None:  # noqa: N802
        """Capture control and the Npcap path setting only."""
        try:
            parsed_path = urlparse(self.path)
            if parsed_path.path == "/api/shutdown":
                query = parse_qs(parsed_path.query)
                requested_pid = int(query.get("pid", [str(os.getpid())])[0])
                if requested_pid != os.getpid():
                    self._write_json(
                        {"ok": False, "error": "backend_pid_mismatch"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                self._write_json({"ok": True})
                # BaseServer.shutdown() must run outside the request thread.
                threading.Thread(
                    target=self.server.shutdown,
                    name="v424-api-shutdown",
                    daemon=True,
                ).start()
                return
            payload = self._read_json()
            core = get_engine().native
            if parsed_path.path == "/api/replay/open":
                self._write_json(_replay_jobs().open(str(payload.get("path", "") or "")))
                return
            if parsed_path.path == "/api/debug/probe-event":
                # 前端把「这一批事件怎么处理的」写进观测流水，回放时才能对账。
                self._write_json(core.debug_probe_event(payload))
                return
            if self.path == "/api/npcap/config":
                raw = str(payload.get("path", "") or "")
                npcap_config.write_config_path(raw)
                status = npcap_config.status_payload()
                # Echo what was actually stored so the UI never shows a value
                # that only lived in the request body.
                status["saved"] = True
                self._write_json(status)
                return
            if self.path == "/api/npcap/detect":
                candidates = npcap_config.detect_candidates()
                resolved, source = npcap_config.resolve_dll()
                self._write_json(
                    {
                        "candidates": candidates,
                        "resolvedPath": str(resolved) if resolved else "",
                        "installed": resolved is not None,
                        "message": "" if resolved else "未找到 Npcap。",
                        "source": source,
                    }
                )
                return
            if self.path == "/api/npcap/test":
                self._write_json(npcap_config.test_connection())
                return
            if self.path == "/api/capture/start":
                from engine.live import nest_probe

                # 每次「开始统计」换一个新文件：一轮副本一个日志，便于事后对齐。
                nest_probe.rotate()
                self._write_json(core.start_capture(as_number(payload.get("duration"))))
                return
            if self.path == "/api/debug-recording":
                from engine.live import debug_recording

                enabled = payload.get("enabled")
                output_dir = payload.get("outputDir")
                try:
                    self._write_json(
                        debug_recording.apply(
                            enabled=None if enabled is None else bool(enabled),
                            output_dir=None if output_dir is None else str(output_dir),
                        )
                    )
                except ValueError as error:
                    state = debug_recording.describe()
                    state["error"] = str(error)
                    state["message"] = str(error)
                    self._write_json(state, HTTPStatus.BAD_REQUEST)
                return
            if self.path == "/api/probe":
                from engine.live import nest_probe

                if payload.get("enabled") is None:
                    self._write_json(nest_probe.describe())
                    return
                nest_probe.set_runtime_enabled(bool(payload.get("enabled")))
                self._write_json(nest_probe.describe())
                return
            if self.path == "/api/capture/stop":
                self._write_json(core.stop_capture())
                return
            if self.path == "/api/capture/auto":
                self._write_json(core.set_capture_auto(bool(payload.get("enabled"))))
                return
            if self.path == "/api/capture/toggles":
                self._write_json(core.set_capture_toggles(payload))
                return
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except Exception as error:  # noqa: BLE001
            self._handle_error(error)

def serve(host: str = HOST, port: int = DEFAULT_PORT) -> None:
    # Native extensions imported during startup may replace the process-wide
    # exception filter. Install ours once more after all backend modules loaded.
    _install_windows_backend_minidump()
    # 「调试记录」开关要在第一次抓包之前就生效，否则上一轮保存的开关得等界面
    # 拉一次状态才起作用（中途开关会让头一份文件落在旧目录）。
    try:
        from engine.live import debug_recording

        debug_recording.ensure_applied()
    except Exception:
        pass
    server = _bind_server_or_wait_for_takeover(host, port)
    # Load the engine off the request path so /api/health answers in
    # milliseconds and the window becomes usable immediately.
    threading.Thread(
        target=get_engine, name="engine-warmup", daemon=True
    ).start()

    def heartbeat_when_ready() -> None:
        if _ENGINE_READY.wait(120.0):
            engine = ENGINE
            if engine is not None:
                _start_backend_state_heartbeat(engine)

    threading.Thread(
        target=heartbeat_when_ready, name="heartbeat-start", daemon=True
    ).start()
    pid_path = Path(__file__).resolve().parent / ".server_pid"
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    _write_backend_diagnostic(
        f"Backend ready: version={VERSION}, endpoint=http://{host}:{port}"
    )
    print(f"Dragon Nest V{VERSION} API listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        global ENGINE
        engine = ENGINE
        ENGINE = None
        _stop_backend_state_heartbeat(engine)
        if engine is not None:
            try:
                engine.shutdown()
            except Exception as exc:
                print(f"V{VERSION} backend shutdown warning: {exc}", flush=True)
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        _write_backend_diagnostic("Backend stopped normally")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Dragon Nest simulator V{VERSION} local API")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    args = parser.parse_args()
    serve(args.host, args.port)
