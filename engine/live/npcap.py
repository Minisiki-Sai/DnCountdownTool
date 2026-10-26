from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

from .bounded_queue import AsyncSpillQueue
from .trace_writer import AsyncTraceWriter
from .. import npcap_config
from . import capture_health


_PCAP_ERRBUF_SIZE = 256
_PACKET_QUEUE_MEMORY_ITEMS = 16_384
_PACKET_QUEUE_MEMORY_BYTES = 64 * 1024 * 1024
_PCAP_IF_LOOPBACK = 0x00000001
_ACCELERATOR_ADAPTER_MARKERS = (
    "loopback",
    "wintun",
    "wireguard",
    "tap-windows",
    "tap adapter",
    "tun adapter",
    "vpn",
    "accelerator",
    "netfilter",
    "socks",
    "proxy",
    "uu",
    "qiyou",
    "leigod",
    "xunyou",
)
# Combat events use UDP, so the default accelerator probe remains UDP-only.
# Character profiles use an independent, explicitly bounded TCP probe below;
# separating them prevents a proven UDP flow from hiding a rewritten TCP one.
_ACCELERATOR_DISCOVERY_FILTER = "udp"
_SIMULATOR_BACKEND_PORT = 48710
# TCP profile/inspect responses can be moved to an unrelated local port by a
# game accelerator.  The process socket table then still describes the
# pre-tunnel connection, so a port-only BPF filter never sees the response.
# Keep this broad probe bounded and exclude our own local HTTP backend; the
# decoder must still prove a v576 message before the flow is retained.
_ACCELERATOR_TCP_DISCOVERY_FILTER = (
    f"tcp and not port {_SIMULATOR_BACKEND_PORT}"
)

# The client changes from the character/login connection to a game-server
# connection before Windows' process socket table necessarily exposes the new
# local endpoint.  The first server response contains the cached character
# profile, so waiting for the next periodic port refresh loses the one packet
# that profile sync needs most.  These server-side ports are proven by the
# local v576 capture corpus; packet decoding still validates every payload.
V576_TCP_SERVICE_PORTS = (
    14301,
    14401,
    14403,
    14501,
    14502,
    14503,
    15100,
)

# ``DNDP1`` stores the originating adapter in one unsigned byte. Real Npcap
# adapters are allocated from zero upward; 0xFF is reserved for raw identity
# evidence, 0xFE for its verified compact projection, 0xFD for verified
# pre-boundary 21 0C session evidence and 0xFC for the formal-boundary state
# snapshot, and 0xFB for stable profile/runtime identity rows. Replay isolates
# all five so their old timestamps can never move the
# formal-map battle origin.
IDENTITY_TRACE_ADAPTER_INDEX = 0xFF
# Raw TCP identity fragments can depend on handshake/key state that predates
# the combat boundary.  0xFE stores the already verified actor-name bindings
# alongside those fragments, making a saved trace self-contained on replay.
IDENTITY_METADATA_ADAPTER_INDEX = 0xFE
IDENTITY_METADATA_MAGIC = b"DNID1\x00"
# Verified 21 0C resource frames are retained separately from identity TCP
# evidence because they have a narrower contract: maximum HP, local identity
# and reconnect matching only. They are not combat timeline records.
SESSION_EVIDENCE_ADAPTER_INDEX = 0xFD
# Active summons and player Buffs may have been created before dungeon entry.
# The memory-only probe validates them, then stores one compact snapshot before
# the first combat frame so both live handoff and future replay start identically.
PREBATTLE_STATE_ADAPTER_INDEX = 0xFC
PREBATTLE_STATE_MAGIC = b"DNPB1\x00"
PROFILE_IDENTITY_ADAPTER_INDEX = 0xFB
PROFILE_IDENTITY_MAGIC = b"DNPI1\x00"


def encode_prebattle_state_snapshot(snapshot: object) -> bytes:
    if not isinstance(snapshot, dict):
        return b""
    if not isinstance(snapshot.get("prebattle_state"), dict):
        return b""
    payload = dict(snapshot)
    payload["version"] = 1
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return b""
    # This is state metadata, never an unbounded packet archive.
    if len(encoded) > 2 * 1024 * 1024:
        return b""
    return PREBATTLE_STATE_MAGIC + encoded


def decode_prebattle_state_snapshot(payload: bytes) -> dict[str, object]:
    data = bytes(payload or b"")
    if not data.startswith(PREBATTLE_STATE_MAGIC):
        return {}
    try:
        decoded = json.loads(
            data[len(PREBATTLE_STATE_MAGIC) :].decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict) or int(decoded.get("version") or 0) != 1:
        return {}
    return decoded


def encode_identity_trace_bindings(bindings: object) -> bytes:
    try:
        items = bindings.items()  # type: ignore[union-attr]
    except AttributeError:
        items = ()
    normalized: dict[int, str] = {}
    for actor_id, character_name in items:
        try:
            parsed_actor_id = int(actor_id)
        except (TypeError, ValueError):
            continue
        name = str(character_name or "").strip().replace("\x00", "")
        if not 0 < parsed_actor_id <= 0xFFFFFFFF or not name:
            continue
        normalized[parsed_actor_id] = name[:128]
        if len(normalized) >= 4096:
            break
    if not normalized:
        return b""
    payload = {
        "version": 1,
        "actors": [[actor_id, normalized[actor_id]] for actor_id in sorted(normalized)],
    }
    return IDENTITY_METADATA_MAGIC + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_identity_trace_bindings(payload: bytes) -> dict[int, str]:
    data = bytes(payload or b"")
    if not data.startswith(IDENTITY_METADATA_MAGIC):
        return {}
    try:
        decoded = json.loads(data[len(IDENTITY_METADATA_MAGIC) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    try:
        version = int(decoded.get("version") or 0)
    except (TypeError, ValueError):
        return {}
    if version != 1:
        return {}
    rows = decoded.get("actors")
    if not isinstance(rows, list):
        return {}
    bindings: dict[int, str] = {}
    for row in rows[:4096]:
        if not isinstance(row, list) or len(row) != 2:
            continue
        try:
            actor_id = int(row[0])
        except (TypeError, ValueError):
            continue
        name = str(row[1] or "").strip().replace("\x00", "")
        if 0 < actor_id <= 0xFFFFFFFF and name:
            bindings[actor_id] = name[:128]
    return bindings


def encode_profile_identity_bindings(bindings: object) -> bytes:
    try:
        rows = tuple(bindings)  # type: ignore[arg-type]
    except TypeError:
        return b""
    normalized: dict[tuple[int, int], list[object]] = {}
    for row in rows[:4096]:
        if isinstance(row, dict):
            value = row.get
        else:
            value = lambda key, default=None, row=row: getattr(row, key, default)
        try:
            profile_id = int(value("profile_id", 0) or 0)
            runtime_id = int(value("runtime_actor_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        name = str(value("character_name", "") or "").strip().replace("\x00", "")
        job = str(value("job", "") or "").strip().replace("\x00", "")
        source = str(value("source", "") or "cache").strip().replace("\x00", "")
        if (
            not 0 < profile_id <= 0x7FFFFFFFFFFFFFFF
            or not 0 < runtime_id <= 0x7FFFFFFFFFFFFFFF
            or not name
        ):
            continue
        normalized[(runtime_id, profile_id)] = [
            runtime_id, profile_id, name[:64], job[:64], source[:32]
        ]
    if not normalized:
        return b""
    payload = {
        "version": 1,
        "bindings": [normalized[key] for key in sorted(normalized)],
    }
    return PROFILE_IDENTITY_MAGIC + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def decode_profile_identity_bindings(payload: bytes) -> tuple[dict[str, object], ...]:
    data = bytes(payload or b"")
    if not data.startswith(PROFILE_IDENTITY_MAGIC):
        return ()
    try:
        decoded = json.loads(data[len(PROFILE_IDENTITY_MAGIC) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return ()
    if not isinstance(decoded, dict) or int(decoded.get("version") or 0) != 1:
        return ()
    rows = decoded.get("bindings")
    if not isinstance(rows, list):
        return ()
    result: list[dict[str, object]] = []
    for row in rows[:4096]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        try:
            runtime_id = int(row[0])
            profile_id = int(row[1])
        except (TypeError, ValueError):
            continue
        name = str(row[2] or "").strip().replace("\x00", "")
        if runtime_id <= 0 or profile_id <= 0 or not name:
            continue
        result.append({
            "runtime_actor_id": runtime_id,
            "profile_id": profile_id,
            "character_name": name[:64],
            "job": str(row[3] if len(row) > 3 else "" or "").strip()[:64],
            "source": str(row[4] if len(row) > 4 else "cache" or "cache").strip()[:32],
        })
    return tuple(result)


def v576_tcp_capture_ports(
    tcp_ports: list[int] | tuple[int, ...] = (),
) -> tuple[int, ...]:
    return tuple(
        sorted(
            set(V576_TCP_SERVICE_PORTS)
            | {int(port) for port in tcp_ports if 0 < int(port) <= 65535}
        )
    )


def set_current_thread_low_priority() -> None:
    """Keep capture work responsive without competing with the game client."""

    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)
    except (AttributeError, OSError):
        pass


def set_current_thread_normal_priority() -> None:
    """Keep readers responsive; also restore an ordered decoder catching up."""

    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 0)
    except (AttributeError, OSError):
        pass


class NpcapError(RuntimeError):
    pass


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _PcapPacketHeader(ctypes.Structure):
    _fields_ = [
        ("ts", _Timeval),
        ("caplen", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
    ]


class _PcapStatistics(ctypes.Structure):
    # WinPcap/Npcap's Windows pcap_stat includes ps_capt after the three
    # portable counters.  Allocating only three fields would let pcap_stats
    # write beyond the ctypes buffer.
    _fields_ = [
        ("ps_recv", ctypes.c_uint32),
        ("ps_drop", ctypes.c_uint32),
        ("ps_ifdrop", ctypes.c_uint32),
        ("ps_capt", ctypes.c_uint32),
    ]


class _BpfProgram(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


class _PcapInterface(ctypes.Structure):
    pass


_PcapInterface._fields_ = [
    ("next", ctypes.POINTER(_PcapInterface)),
    ("name", ctypes.c_char_p),
    ("description", ctypes.c_char_p),
    ("addresses", ctypes.c_void_p),
    ("flags", ctypes.c_uint32),
]


def _decode_error(value: bytes | None) -> str:
    if not value:
        return "unknown Npcap error"
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def _is_accelerator_adapter(
    name: bytes | str,
    description: bytes | str,
    flags: int,
) -> bool:
    """Identify interfaces that can carry pre-tunnel accelerator traffic."""

    if int(flags) & _PCAP_IF_LOOPBACK:
        return True
    values: list[str] = []
    for value in (name, description):
        if isinstance(value, bytes):
            values.append(_decode_error(value))
        else:
            values.append(str(value or ""))
    text = " ".join(values).casefold()
    return any(marker in text for marker in _ACCELERATOR_ADAPTER_MARKERS)


def _is_simulator_loopback_tcp(frame: bytes) -> bool:
    """Reject the simulator's own localhost API traffic at the capture edge.

    The BPF discovery filter is intentionally UDP-only, but this software guard
    keeps a future broad filter (or a stale installed filter) from feeding the
    high-volume 48710/TCP response stream into the decoder and trace writer.
    Raw IP, Npcap DLT_NULL and ordinary Ethernet frames are supported.
    """

    offsets: list[int] = [0, 4]
    if len(frame) >= 14:
        ether_type = int.from_bytes(frame[12:14], "big")
        ethernet_offset = 14
        while ether_type in (0x8100, 0x88A8) and ethernet_offset + 4 <= len(frame):
            ether_type = int.from_bytes(
                frame[ethernet_offset + 2 : ethernet_offset + 4], "big"
            )
            ethernet_offset += 4
        if ether_type in (0x0800, 0x86DD):
            offsets.append(ethernet_offset)

    for offset in offsets:
        if offset >= len(frame):
            continue
        version = frame[offset] >> 4
        if version == 4 and offset + 20 <= len(frame):
            ihl = (frame[offset] & 0x0F) * 4
            if ihl < 20 or offset + ihl + 4 > len(frame) or frame[offset + 9] != 6:
                continue
            source_ip = frame[offset + 12 : offset + 16]
            destination_ip = frame[offset + 16 : offset + 20]
            if source_ip[:1] != b"\x7f" and destination_ip[:1] != b"\x7f":
                continue
            source_port, destination_port = struct.unpack_from(
                "!HH", frame, offset + ihl
            )
            if _SIMULATOR_BACKEND_PORT in (source_port, destination_port):
                return True
        elif version == 6 and offset + 44 <= len(frame):
            # Local API traffic has no IPv6 extension headers. If that changes,
            # the UDP-only BPF filter remains the primary protection.
            if frame[offset + 6] != 6:
                continue
            source_ip = frame[offset + 8 : offset + 24]
            destination_ip = frame[offset + 24 : offset + 40]
            loopback = b"\x00" * 15 + b"\x01"
            if source_ip != loopback and destination_ip != loopback:
                continue
            source_port, destination_port = struct.unpack_from(
                "!HH", frame, offset + 40
            )
            if _SIMULATOR_BACKEND_PORT in (source_port, destination_port):
                return True
    return False


def _app_local_npcap_directories() -> tuple[Path, ...]:
    """App-local Npcap locations, checked before the system driver.

    A portable build may ship an OEM-licensed Npcap runtime under
    ``third_party/npcap`` next to the executable (or inside the PyInstaller
    bundle). An ordinary installation keeps using the system driver, so this
    lookup changes nothing when no app-local copy exists.
    """

    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root))
    roots.append(Path(__file__).resolve().parents[2])
    directories: list[Path] = []
    for root in roots:
        for relative in (Path("third_party") / "npcap", Path("runtime") / "npcap", Path("npcap")):
            directory = root / relative
            if directory not in directories:
                directories.append(directory)
    return tuple(directories)


def find_npcap_dll() -> Path | None:
    """Resolve wpcap.dll: configured path -> portable copy -> system install."""
    resolved, _source = npcap_config.resolve_dll()
    return resolved


def build_capture_filter(
    udp_ports: list[int] | tuple[int, ...],
    tcp_ports: list[int] | tuple[int, ...] = (),
) -> str:
    """Build one transport-aware BPF filter for the game process ports."""
    clauses: list[str] = []
    for transport, raw_ports in (("udp", udp_ports), ("tcp", tcp_ports)):
        ports = sorted(
            {int(port) for port in raw_ports if 0 < int(port) <= 65535}
        )
        if ports:
            port_filter = " or ".join(f"port {port}" for port in ports)
            clauses.append(f"({transport} and ({port_filter}))")
    return " or ".join(clauses)


class NpcapPacketStream:
    """Capture filtered packets through Npcap without external processes."""

    def __init__(
        self,
        udp_ports: list[int],
        trace_path: Path | None = None,
        tcp_ports: list[int] | None = None,
        *,
        use_environment_trace: bool = True,
    ) -> None:
        udp = sorted({int(port) for port in udp_ports if 0 < int(port) <= 65535})
        tcp = list(v576_tcp_capture_ports(tcp_ports or []))
        capture_filter = build_capture_filter(udp, tcp)
        if not capture_filter:
            raise NpcapError("未找到游戏 TCP/UDP 端口，请确认游戏已经登录")
        dll_path = find_npcap_dll()
        if dll_path is None:
            raise NpcapError(
                "未安装 Npcap。请先从 https://npcap.com/ 安装，且不要勾选仅管理员访问"
            )

        self._dll_directory = None
        if hasattr(os, "add_dll_directory"):
            self._dll_directory = os.add_dll_directory(str(dll_path.parent))
        self._pcap = ctypes.CDLL(str(dll_path))
        self._configure_api()
        self._packets: AsyncSpillQueue[
            tuple[float, bytes, int, int]
        ] = AsyncSpillQueue(
            memory_items=_PACKET_QUEUE_MEMORY_ITEMS,
            memory_bytes=_PACKET_QUEUE_MEMORY_BYTES,
            size_of=lambda row: len(row[1]) + 64,
            on_error=self._report_capture_queue_error,
        )
        self._driver_stats_lock = threading.Lock()
        self._driver_stats_by_adapter: dict[int, tuple[int, int, int]] = {}
        self._errors: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._capture_failed = False
        # Capture time is recorded by the Npcap reader before the frame enters
        # the decoder queue.  Consumers must not derive the live clock from
        # ``read_packets`` because a busy decoder can be several seconds
        # behind the network thread.
        self._capture_clock_lock = threading.Lock()
        self._capture_clock_revision = 0
        self._latest_reader_lag_ms = 0.0
        self._latest_frame_timestamp = 0.0
        self._latest_frame_enqueued_monotonic = 0.0
        self._stop_event = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._handles: list[ctypes.c_void_p] = []
        self._threads: list[threading.Thread] = []
        self._device_lock = threading.RLock()
        self._opened_device_names: set[bytes] = set()
        self._adapter_descriptions: dict[int, str] = {}
        self._accelerator_adapter_indexes: set[int] = set()
        self._next_adapter_index = 0
        self._filter_lock = threading.Lock()
        self._filter_generation = 0
        self._capture_filter = capture_filter
        self._accelerator_discovery = False
        self._accelerator_discovery_until = 0.0
        # Scene changes can replace an accelerator's relay flow after an
        # otherwise valid lobby packet has already been decoded.  During this
        # bounded grace period a first decoded packet may teach its ports, but
        # must not close broad UDP discovery before the combat flow appears.
        self._accelerator_discovery_hold_until = 0.0
        self._accelerator_tcp_discovery = False
        self._accelerator_tcp_discovery_until = 0.0
        self._accelerator_tcp_discovery_hold_until = 0.0
        self._trace_handle = None
        self._trace_writer = None
        self._trace_lock = threading.Lock()
        self._trace_pending = 0
        self._trace_last_flush = time.monotonic()
        self._trace_digest = None
        self._trace_final_hash = ""
        self._trace_physical_frames = 0
        self._trace_truncated_frames = 0
        self._trace_driver_baseline = self.driver_packet_stats
        # A manual capture may reuse the always-on probe stream.  Its queue can
        # contain old broad-discovery traffic that predates the user's click;
        # start_trace consumes this one-shot boundary before attaching disk I/O.
        self._trace_minimum_timestamp = 0.0
        self._identity_trace_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = ()
        self._identity_trace_bindings: dict[int, str] = {}
        self._profile_identity_bindings: dict[tuple[int, int], dict[str, object]] = {}
        self._session_evidence_trace_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = ()
        # The memory-only auto probe may decode the formal-map boundary from
        # TCP segments that are intentionally discarded before the combat
        # trace starts.  Carry that already-verified fact across the handoff
        # without retaining lobby traffic or moving the combat zero point.
        self._auto_capture_boundary_evidence: dict[str, object] = {}
        configured_trace = trace_path
        if configured_trace is None and use_environment_trace:
            trace_path_text = os.environ.get("DRAGON_NEST_DPS_TRACE", "").strip()
            configured_trace = Path(trace_path_text) if trace_path_text else None
        self.trace_path = configured_trace
        self.udp_ports = tuple(udp)
        self.tcp_ports = tuple(tcp)
        try:
            if configured_trace is not None:
                path = configured_trace
                path.parent.mkdir(parents=True, exist_ok=True)
                self._trace_handle = AsyncTraceWriter(path, on_error=self._report_trace_error)
                self._trace_writer = self._trace_handle
                magic = b"DNDP1\x00"
                self._trace_handle.write(magic)
            self._start_capture(capture_filter)
        except Exception:
            # A failed adapter open must not leak an idle spool/writer on
            # every retry of the always-on auto-capture probe.
            try:
                self.close()
            except Exception:
                pass
            if self._closed:
                try:
                    self._packets.close()
                except Exception:
                    pass
            raise

    def capture_clock_sample(self) -> tuple[int, float, float]:
        """Return raw-frame revision, packet timestamp and enqueue monotonic."""

        with self._capture_clock_lock:
            return (
                self._capture_clock_revision,
                self._latest_frame_timestamp,
                self._latest_frame_enqueued_monotonic,
            )

    @property
    def capture_reader_lag_ms(self) -> float:
        """Packet age when the reader received it, excluding idle time."""
        with self._capture_clock_lock:
            return float(getattr(self, "_latest_reader_lag_ms", 0.0))

    @property
    def driver_packet_stats(self) -> dict[str, int]:
        """Npcap kernel counters; sampled by each handle's owning reader."""

        with self._driver_stats_lock:
            rows = tuple(self._driver_stats_by_adapter.values())
        return {
            "received": sum(row[0] for row in rows),
            "dropped": sum(row[1] for row in rows),
            "interfaceDropped": sum(row[2] for row in rows),
            "adapters": len(rows),
        }

    def set_auto_capture_boundary_evidence(
        self,
        evidence: dict[str, object] | None,
    ) -> None:
        self._auto_capture_boundary_evidence = dict(evidence or {})

    def take_auto_capture_boundary_evidence(self) -> dict[str, object]:
        evidence = dict(self._auto_capture_boundary_evidence)
        self._auto_capture_boundary_evidence.clear()
        return evidence

    def _remember_capture_clock(self, timestamp: float) -> None:
        now = time.monotonic()
        with self._capture_clock_lock:
            if timestamp < self._latest_frame_timestamp:
                return
            self._latest_frame_timestamp = float(timestamp)
            self._latest_frame_enqueued_monotonic = now
            self._latest_reader_lag_ms = max(0.0, (time.time() - timestamp) * 1000.0)
            self._capture_clock_revision += 1

    def _report_trace_error(self, reason: str) -> None:
        self._capture_failed = True
        errors = getattr(self, "_errors", None)
        if errors is not None:
            errors.put(f"原始抓包保存失败，记录不完整：{reason}")
        stop = getattr(self, "_stop_event", None)
        if stop is not None:
            stop.set()

    def _report_capture_queue_error(self, reason: str) -> None:
        # RAW has an independent writer. A failed decode spool must not stop
        # collecting originals; the stopped session MUST replay the file.
        if getattr(self, "_trace_handle", None) is not None:
            self._decoder_replay_required = str(reason)
            return
        self._capture_failed = True
        errors = getattr(self, "_errors", None)
        if errors is not None:
            errors.put(f"抓包队列暂存失败，记录不完整：{reason}")
        stop = getattr(self, "_stop_event", None)
        if stop is not None:
            stop.set()

    def _write_trace_record_locked(self, timestamp: float, frame: bytes, original_length: int, adapter_index: int) -> None:
        header = struct.pack("<dBII", float(timestamp), int(adapter_index), int(original_length), len(frame))
        # One atomic queue item per record; overflow cannot enqueue just a
        # header and leave a malformed partial record in an otherwise valid file.
        self._trace_handle.write_parts((header, frame))
        if adapter_index < capture_health.ADAPTER_INDEX:
            self._trace_physical_frames = getattr(self, "_trace_physical_frames", 0) + 1
            if len(frame) < original_length:
                self._trace_truncated_frames = getattr(self, "_trace_truncated_frames", 0) + 1

    def _append_capture_health_locked(self) -> None:
        from .version_fingerprint import module_code_fingerprint
        final_stats = self.driver_packet_stats
        baseline = getattr(self, "_trace_driver_baseline", {})
        health = {
            "version": 1,
            "implementation": capture_health.CAPTURE_IMPLEMENTATION,
            # EOF work only, never calculated by the live reader/overlay.
            "captureCodeFingerprint": module_code_fingerprint((
                'engine.live.npcap',
                'engine.live.tls_probe',
                'engine.live.protocol',
            )),
            "appVersion": os.environ.get("DRAGON_NEST_APP_VERSION", "unknown"),
            "udpArchivePolicy": "all_udp_during_recording",
            "knownUdpPorts": list(self.udp_ports) if hasattr(self, 'udp_ports') else [],
            "knownTcpPorts": list(self.tcp_ports) if hasattr(self, 'tcp_ports') else [],
            "physicalFrames": getattr(self, "_trace_physical_frames", 0),
            "truncatedFrames": getattr(self, "_trace_truncated_frames", 0),
            "driverBaseline": baseline,
            "driverFinal": final_stats,
            "driverCountersAvailable": final_stats["adapters"] > 0,
            "driverDroppedDelta": max(0, final_stats["dropped"] - baseline.get("dropped", 0)),
            "interfaceDroppedDelta": max(0, final_stats["interfaceDropped"] - baseline.get("interfaceDropped", 0)),
            "captureFailed": bool(getattr(self, "_capture_failed", False)),
            "decoderReplayRequired": str(getattr(self, "_decoder_replay_required", "")),
            "rawWriter": getattr(self._trace_writer, "stats", {}),
            "scope": "Observed local driver/application only; cannot prove upstream delivery",
        }
        frame = capture_health.encode_health(health)
        self.capture_integrity = health
        self._write_trace_record_locked(0.0, frame, len(frame), capture_health.ADAPTER_INDEX)

    def start_trace(
        self,
        trace_path: Path,
        preloaded_frames: list[tuple[float, bytes, int, int]] | tuple[
            tuple[float, bytes, int, int], ...
        ] = (),
    ) -> tuple[tuple[float, bytes, int, int], ...]:
        """Attach diagnostic recording to an already-running capture stream.

        Auto-capture uses this to keep its first stage memory-only. Once a
        dungeon event is confirmed, the rolling prebuffer and every following
        frame are written into one ordinary ``.dpscap`` file.
        """

        path = Path(trace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_lock:
            if self._trace_handle is not None:
                return tuple(preloaded_frames)
            minimum_timestamp = max(
                0.0,
                float(getattr(self, "_trace_minimum_timestamp", 0.0) or 0.0),
            )
            self._trace_minimum_timestamp = 0.0
            flush_pending = getattr(self._packets, "flush_pending", None)
            if callable(flush_pending):
                flush_pending()
            pending_frames: list[tuple[float, bytes, int, int]] = []
            while True:
                try:
                    pending_frames.append(self._packets.get_nowait())
                except queue.Empty:
                    break
            all_frames = tuple(
                packet
                for packet in tuple(preloaded_frames) + tuple(pending_frames)
                if (
                    (minimum_timestamp <= 0.0 or float(packet[0]) >= minimum_timestamp)
                    and not _is_simulator_loopback_tcp(packet[1])
                )
            )
            self.trace_path = path
            self._trace_handle = AsyncTraceWriter(path, on_error=self._report_trace_error)
            self._trace_writer = self._trace_handle
            self._trace_digest = None
            self._trace_final_hash = ""
            self._trace_physical_frames = 0
            self._trace_truncated_frames = 0
            self._trace_driver_baseline = self.driver_packet_stats
            magic = b"DNDP1\x00"
            self._trace_handle.write(magic)
            prebattle_payload = encode_prebattle_state_snapshot(
                getattr(self, "_auto_capture_boundary_evidence", {})
            )
            if prebattle_payload:
                prebattle_timestamp = 0.0
                try:
                    prebattle_timestamp = float(
                        self._auto_capture_boundary_evidence.get(
                            "boundary_timestamp", 0.0
                        )
                    )
                except (AttributeError, TypeError, ValueError):
                    prebattle_timestamp = 0.0
                self._write_trace_record_locked(prebattle_timestamp, prebattle_payload, len(prebattle_payload), PREBATTLE_STATE_ADAPTER_INDEX)
            for timestamp, frame, original_length, adapter_index in all_frames:
                self._write_trace_record_locked(timestamp, frame, original_length, adapter_index)
            self._trace_handle.flush()
            self._trace_pending = 0
            self._trace_last_flush = time.monotonic()
            with self._filter_lock:
                self._filter_generation += 1
            return all_frames

    def set_trace_minimum_timestamp(self, timestamp: float) -> None:
        """Discard queued probe traffic older than one manual Start click."""

        with self._trace_lock:
            self._trace_minimum_timestamp = max(0.0, float(timestamp or 0.0))

    def set_identity_trace_frames(
        self,
        frames: list[tuple[float, bytes, int, int]] | tuple[
            tuple[float, bytes, int, int], ...
        ],
    ) -> None:
        """Merge identity evidence into the end-of-file adapter-255 appendix.

        Frames may arrive before or during combat from the independent TCP
        identity listener. They stay out of the live packet queue and are
        written only when the trace closes. Keeping them at the end preserves
        the formal-map battle origin while still making the saved recording
        self-contained when opened on another run or another machine.
        """

        with self._trace_lock:
            existing = tuple(
                getattr(self, "_identity_trace_frames", ()) or ()
            )
        normalized: list[tuple[float, bytes, int, int]] = []
        seen: set[tuple[int, bytes]] = set()
        retained_bytes = 0
        # One identity response is normally only a few TCP frames. The cap is
        # intentionally generous for towns/reconnects but prevents an all-day
        # listener from inflating a combat recording without bound.
        for timestamp, frame, original_length, adapter_index in sorted(
            (*existing, *tuple(frames)), key=lambda row: float(row[0])
        ):
            payload = bytes(frame)
            if not payload or _is_simulator_loopback_tcp(payload):
                continue
            key = (
                int(original_length),
                hashlib.blake2s(payload, digest_size=12).digest(),
            )
            if key in seen:
                continue
            seen.add(key)
            if len(normalized) >= 4096 or retained_bytes + len(payload) > 16 * 1024 * 1024:
                break
            normalized.append(
                (
                    float(timestamp),
                    payload,
                    int(original_length),
                    int(adapter_index),
                )
            )
            retained_bytes += len(payload)
        with self._trace_lock:
            self._identity_trace_frames = tuple(normalized)

    def set_session_evidence_trace_frames(
        self,
        frames: list[tuple[float, bytes, int, int]] | tuple[
            tuple[float, bytes, int, int], ...
        ],
    ) -> None:
        """Merge verified 21 0C frames into the session-evidence appendix."""

        with self._trace_lock:
            existing = tuple(
                getattr(self, "_session_evidence_trace_frames", ()) or ()
            )
        normalized: list[tuple[float, bytes, int, int]] = []
        seen: set[tuple[int, bytes]] = set()
        retained_bytes = 0
        for timestamp, frame, original_length, adapter_index in sorted(
            (*existing, *tuple(frames)), key=lambda row: float(row[0])
        ):
            payload = bytes(frame)
            if not payload or _is_simulator_loopback_tcp(payload):
                continue
            key = (
                int(original_length),
                hashlib.blake2s(payload, digest_size=12).digest(),
            )
            if key in seen:
                continue
            seen.add(key)
            if (
                len(normalized) >= 4096
                or retained_bytes + len(payload) > 16 * 1024 * 1024
            ):
                break
            normalized.append(
                (
                    float(timestamp),
                    payload,
                    int(original_length),
                    int(adapter_index),
                )
            )
            retained_bytes += len(payload)
        with self._trace_lock:
            self._session_evidence_trace_frames = tuple(normalized)

    def remember_session_evidence_trace_frame(
        self, frame: tuple[float, bytes, int, int]
    ) -> None:
        """Stage one newly verified in-session 21 0C frame."""

        self.set_session_evidence_trace_frames((frame,))

    def set_identity_trace_bindings(self, bindings: object) -> None:
        """Merge verified actor/name bindings for the saved-trace appendix."""

        encoded = encode_identity_trace_bindings(bindings)
        if not encoded:
            return
        normalized = decode_identity_trace_bindings(encoded)
        with self._trace_lock:
            current = getattr(self, "_identity_trace_bindings", None)
            if not isinstance(current, dict):
                current = {}
                self._identity_trace_bindings = current
            current.update(normalized)

    def set_profile_identity_bindings(self, bindings: object) -> None:
        """Merge session profile/runtime/name observations for replay scoring."""

        encoded = encode_profile_identity_bindings(bindings)
        if not encoded:
            return
        normalized = decode_profile_identity_bindings(encoded)
        with self._trace_lock:
            current = getattr(self, "_profile_identity_bindings", None)
            if not isinstance(current, dict):
                current = {}
                self._profile_identity_bindings = current
            for row in normalized:
                key = (int(row["runtime_actor_id"]), int(row["profile_id"]))
                current[key] = row

    def _append_identity_trace_frames_locked(self) -> int:
        """Write staged identity frames while ``_trace_lock`` is held."""

        trace_handle = getattr(self, "_trace_handle", None)
        frames = tuple(getattr(self, "_identity_trace_frames", ()) or ())
        self._identity_trace_frames = ()
        if trace_handle is None or not frames:
            return 0
        trace_digest = getattr(self, "_trace_digest", None)
        written = 0
        for timestamp, frame, original_length, _adapter_index in frames:
            self._write_trace_record_locked(timestamp, frame, original_length, IDENTITY_TRACE_ADAPTER_INDEX)
            written += 1
        return written

    def _append_session_evidence_trace_frames_locked(self) -> int:
        """Write verified 21 0C evidence while ``_trace_lock`` is held."""

        trace_handle = getattr(self, "_trace_handle", None)
        frames = tuple(
            getattr(self, "_session_evidence_trace_frames", ()) or ()
        )
        self._session_evidence_trace_frames = ()
        if trace_handle is None or not frames:
            return 0
        trace_digest = getattr(self, "_trace_digest", None)
        written = 0
        for timestamp, frame, original_length, _adapter_index in frames:
            self._write_trace_record_locked(timestamp, frame, original_length, SESSION_EVIDENCE_ADAPTER_INDEX)
            written += 1
        return written

    def _append_identity_trace_bindings_locked(self) -> int:
        """Append one compact verified identity record while trace lock is held."""

        trace_handle = getattr(self, "_trace_handle", None)
        bindings = dict(getattr(self, "_identity_trace_bindings", {}) or {})
        self._identity_trace_bindings = {}
        payload = encode_identity_trace_bindings(bindings)
        if trace_handle is None or not payload:
            return 0
        self._write_trace_record_locked(0.0, payload, len(payload), IDENTITY_METADATA_ADAPTER_INDEX)
        return len(bindings)

    def _append_profile_identity_bindings_locked(self) -> int:
        trace_handle = getattr(self, "_trace_handle", None)
        bindings = tuple(
            dict(row)
            for row in dict(
                getattr(self, "_profile_identity_bindings", {}) or {}
            ).values()
        )
        self._profile_identity_bindings = {}
        payload = encode_profile_identity_bindings(bindings)
        if trace_handle is None or not payload:
            return 0
        self._write_trace_record_locked(0.0, payload, len(payload), PROFILE_IDENTITY_ADAPTER_INDEX)
        return len(bindings)

    def update_ports(
        self,
        udp_ports: list[int] | tuple[int, ...],
        tcp_ports: list[int] | tuple[int, ...] = (),
    ) -> bool:
        """Add ports discovered after login without restarting the capture."""
        udp = tuple(
            sorted(
                set(self.udp_ports)
                | {int(port) for port in udp_ports if 0 < int(port) <= 65535}
            )
        )
        tcp = tuple(
            sorted(
                set(self.tcp_ports)
                | {int(port) for port in tcp_ports if 0 < int(port) <= 65535}
            )
        )
        capture_filter = build_capture_filter(udp, tcp)
        changed = False
        with self._filter_lock:
            if capture_filter != self._capture_filter:
                self.udp_ports = udp
                self.tcp_ports = tcp
                self._capture_filter = capture_filter
                self._filter_generation += 1
                changed = True
        # Accelerators can create their TUN/TAP adapter after capture starts.
        # Re-enumeration is cheap and only runs with the existing one-second
        # game-port refresh cadence.
        self._refresh_capture_devices(require_one=False)
        return changed

    @property
    def adapter_count(self) -> int:
        with self._device_lock:
            return len(self._handles)

    @property
    def accelerator_adapter_count(self) -> int:
        with self._device_lock:
            return len(self._accelerator_adapter_indexes)

    @property
    def accelerator_discovery_active(self) -> bool:
        with self._filter_lock:
            now = time.monotonic()
            return bool(
                (
                    self._accelerator_discovery
                    and now < self._accelerator_discovery_until
                )
                or (
                    self._accelerator_tcp_discovery
                    and now < self._accelerator_tcp_discovery_until
                )
            )

    def enable_accelerator_discovery(self, duration: float = 8.0) -> bool:
        """Temporarily inspect UDP on every adapter for rewritten game flows.

        Some accelerators expose a virtual adapter with an ordinary vendor
        description, so adapter-name heuristics cannot be the gate here.  The
        normal game-port filter remains active for TCP while discovery adds
        broad UDP briefly; the decoder narrows the flow again after proving a
        v576 packet.
        """

        if self.adapter_count <= 0:
            return False
        with self._filter_lock:
            until = time.monotonic() + max(2.0, min(15.0, float(duration)))
            if self._accelerator_discovery and until <= self._accelerator_discovery_until:
                return False
            self._accelerator_discovery = True
            self._accelerator_discovery_until = until
            self._filter_generation += 1
        return True

    def hold_accelerator_discovery(self, duration: float = 12.0) -> bool:
        """Keep broad UDP discovery alive across a scene/relay transition.

        ``confirm_game_flow`` normally narrows the BPF filter immediately.
        That is correct for a stable tunnel, but an accelerator can expose a
        lobby/session-sync flow first and create the actual combat flow a few
        seconds later.  Holding discovery is bounded and still learns every
        proven port, so it cannot leave the expensive broad filter permanent.
        """

        if self.adapter_count <= 0:
            return False
        with self._filter_lock:
            now = time.monotonic()
            bounded = max(2.0, min(45.0, float(duration)))
            until = now + bounded
            changed = (
                not self._accelerator_discovery
                or until > self._accelerator_discovery_until
                or until > getattr(
                    self, "_accelerator_discovery_hold_until", 0.0
                )
            )
            self._accelerator_discovery = True
            self._accelerator_discovery_until = max(
                self._accelerator_discovery_until,
                until,
            )
            self._accelerator_discovery_hold_until = max(
                getattr(self, "_accelerator_discovery_hold_until", 0.0),
                until,
            )
            if changed:
                self._filter_generation += 1
            return changed

    def enable_accelerator_tcp_discovery(self, duration: float = 8.0) -> bool:
        """Temporarily inspect TCP on every adapter for rewritten game flows.

        UDP combat and TCP character profiles are independent connections.
        Proving the UDP flow must therefore not stop this TCP probe.  The
        caller uses a bounded window, and ``confirm_game_flow`` narrows the
        filter to the proven endpoints as soon as any v576 TCP event decodes.
        """

        if self.adapter_count <= 0:
            return False
        with self._filter_lock:
            until = time.monotonic() + max(2.0, min(120.0, float(duration)))
            if (
                self._accelerator_tcp_discovery
                and until <= self._accelerator_tcp_discovery_until
            ):
                return False
            self._accelerator_tcp_discovery = True
            self._accelerator_tcp_discovery_until = until
            self._filter_generation += 1
        return True

    def hold_accelerator_tcp_discovery(
        self, duration: float = 12.0
    ) -> bool:
        """Keep broad TCP discovery alive across a scene/relay transition.

        A lobby/profile response can prove one TCP relay before an accelerator
        opens a different game-scene flow. Learning that first port must not
        close the broad filter until the bounded transition window expires;
        otherwise a non-leader can lose the authoritative server map-confirm
        packet and never start automatic capture.
        """

        if self.adapter_count <= 0:
            return False
        with self._filter_lock:
            now = time.monotonic()
            bounded = max(2.0, min(120.0, float(duration)))
            until = now + bounded
            changed = (
                not self._accelerator_tcp_discovery
                or until > self._accelerator_tcp_discovery_until
                or until > getattr(
                    self, "_accelerator_tcp_discovery_hold_until", 0.0
                )
            )
            self._accelerator_tcp_discovery = True
            self._accelerator_tcp_discovery_until = max(
                self._accelerator_tcp_discovery_until,
                until,
            )
            self._accelerator_tcp_discovery_hold_until = max(
                getattr(
                    self, "_accelerator_tcp_discovery_hold_until", 0.0
                ),
                until,
            )
            if changed:
                self._filter_generation += 1
            return changed

    def disable_accelerator_tcp_discovery(self) -> bool:
        """Return to the learned/process TCP ports after a manual hand-off."""

        with self._filter_lock:
            changed = bool(
                self._accelerator_tcp_discovery
                or self._accelerator_tcp_discovery_until
                or self._accelerator_tcp_discovery_hold_until
            )
            self._accelerator_tcp_discovery = False
            self._accelerator_tcp_discovery_until = 0.0
            self._accelerator_tcp_discovery_hold_until = 0.0
            if changed:
                self._filter_generation += 1
            return changed

    def confirm_game_flow(
        self,
        transport: str,
        source_port: int,
        destination_port: int,
    ) -> None:
        """Narrow an accelerator probe as soon as the decoder proves a flow."""

        ports = {
            int(port)
            for port in (source_port, destination_port)
            if 0 < int(port) <= 65535
        }
        is_tcp = str(transport).casefold() == "tcp"
        known_ports = set(self.tcp_ports if is_tcp else self.udp_ports)
        with self._filter_lock:
            now = time.monotonic()
            discovery_was_active = (
                self._accelerator_tcp_discovery
                if is_tcp
                else self._accelerator_discovery
            )
            udp_discovery_held = (
                not is_tcp
                and now
                < getattr(self, "_accelerator_discovery_hold_until", 0.0)
            )
            tcp_discovery_held = (
                is_tcp
                and now
                < getattr(
                    self, "_accelerator_tcp_discovery_hold_until", 0.0
                )
            )
        if ports - known_ports:
            if is_tcp:
                self.update_ports((), tuple(ports))
            else:
                self.update_ports(tuple(ports), ())
        with self._filter_lock:
            if is_tcp:
                if (
                    discovery_was_active
                    and self._accelerator_tcp_discovery
                    and not tcp_discovery_held
                ):
                    self._accelerator_tcp_discovery = False
                    self._accelerator_tcp_discovery_until = 0.0
                    self._accelerator_tcp_discovery_hold_until = 0.0
                    self._filter_generation += 1
            elif (
                discovery_was_active
                and self._accelerator_discovery
                and not udp_discovery_held
            ):
                self._accelerator_discovery = False
                self._accelerator_discovery_until = 0.0
                self._accelerator_discovery_hold_until = 0.0
                self._filter_generation += 1

    def _configure_api(self) -> None:
        self._pcap.pcap_findalldevs.argtypes = [
            ctypes.POINTER(ctypes.POINTER(_PcapInterface)),
            ctypes.POINTER(ctypes.c_char),
        ]
        self._pcap.pcap_findalldevs.restype = ctypes.c_int
        self._pcap.pcap_freealldevs.argtypes = [ctypes.POINTER(_PcapInterface)]
        self._pcap.pcap_open_live.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char),
        ]
        self._pcap.pcap_open_live.restype = ctypes.c_void_p
        self._pcap.pcap_compile.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BpfProgram),
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self._pcap.pcap_compile.restype = ctypes.c_int
        self._pcap.pcap_setfilter.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BpfProgram),
        ]
        self._pcap.pcap_setfilter.restype = ctypes.c_int
        self._pcap.pcap_freecode.argtypes = [ctypes.POINTER(_BpfProgram)]
        self._pcap.pcap_next_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_PcapPacketHeader)),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self._pcap.pcap_next_ex.restype = ctypes.c_int
        self._pcap.pcap_geterr.argtypes = [ctypes.c_void_p]
        self._pcap.pcap_geterr.restype = ctypes.c_char_p
        for name in ("pcap_setbuff", "pcap_setmintocopy"):
            optional = getattr(self._pcap, name, None)
            if optional is not None:
                optional.argtypes = [ctypes.c_void_p, ctypes.c_int]
                optional.restype = ctypes.c_int
        pcap_stats = getattr(self._pcap, "pcap_stats", None)
        if pcap_stats is not None:
            pcap_stats.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PcapStatistics),
            ]
            pcap_stats.restype = ctypes.c_int
        self._pcap.pcap_close.argtypes = [ctypes.c_void_p]
        breakloop = getattr(self._pcap, "pcap_breakloop", None)
        if breakloop is not None:
            breakloop.argtypes = [ctypes.c_void_p]
            breakloop.restype = None

    def _handle_error(self, handle: ctypes.c_void_p) -> str:
        return _decode_error(self._pcap.pcap_geterr(handle))

    def _set_capture_filter(
        self, handle: ctypes.c_void_p, capture_filter: str
    ) -> str:
        program = _BpfProgram()
        if self._pcap.pcap_compile(
            handle,
            ctypes.byref(program),
            capture_filter.encode("ascii"),
            1,
            0xFFFFFFFF,
        ) != 0:
            return self._handle_error(handle)
        try:
            if self._pcap.pcap_setfilter(handle, ctypes.byref(program)) != 0:
                return self._handle_error(handle)
        finally:
            self._pcap.pcap_freecode(ctypes.byref(program))
        return ""

    def _enumerate_devices(self) -> list[tuple[bytes, bytes, int]]:
        devices = ctypes.POINTER(_PcapInterface)()
        error_buffer = ctypes.create_string_buffer(_PCAP_ERRBUF_SIZE)
        result = self._pcap.pcap_findalldevs(ctypes.byref(devices), error_buffer)
        if result != 0:
            raise NpcapError(_decode_error(error_buffer.value))

        rows: list[tuple[bytes, bytes, int]] = []
        try:
            current = devices
            while current:
                value = current.contents
                if value.name:
                    rows.append((
                        bytes(value.name),
                        bytes(value.description) if value.description else b"",
                        int(value.flags),
                    ))
                current = value.next
        finally:
            if devices:
                self._pcap.pcap_freealldevs(devices)
        return rows

    def _effective_filter(self, accelerator_candidate: bool) -> str:
        del accelerator_candidate
        with self._filter_lock:
            now = time.monotonic()
            if getattr(self, "_trace_handle", None) is not None:
                tcp_filter = (
                    _ACCELERATOR_TCP_DISCOVERY_FILTER
                    if self._accelerator_tcp_discovery and now < self._accelerator_tcp_discovery_until
                    else build_capture_filter((), getattr(self, 'tcp_ports', ()))
                )
                # Ignore learned UDP port churn during formal recording.
                # The stable broad clause also avoids reinstalling equivalent
                # filters and pausing the driver each time a new port is seen.
                return f"udp or ({tcp_filter})" if tcp_filter else "udp"
            discovery_filters: list[str] = []
            if (
                self._accelerator_discovery
                and now < self._accelerator_discovery_until
            ):
                # Formal combat recordings retain all UDP even after the
                # bounded probe learns a port. Reconnects/scene transfers and
                # new protocol envelopes must not disappear from RAW first.
                discovery_filters.append(_ACCELERATOR_DISCOVERY_FILTER)
            if (
                self._accelerator_tcp_discovery
                and now < self._accelerator_tcp_discovery_until
            ):
                discovery_filters.append(_ACCELERATOR_TCP_DISCOVERY_FILTER)
            if discovery_filters:
                suffix = " or ".join(
                    f"({capture_filter})"
                    for capture_filter in discovery_filters
                )
                return f"({self._capture_filter}) or {suffix}"
            return self._capture_filter

    def _refresh_capture_devices(self, *, require_one: bool) -> None:
        with self._device_lock:
            if self._stop_event.is_set():
                return
            rows = self._enumerate_devices()
        open_errors: list[str] = []
        for device_name, description, flags in rows:
            with self._device_lock:
                if device_name in self._opened_device_names:
                    continue
            accelerator_candidate = _is_accelerator_adapter(
                device_name, description, flags
            )
            error_buffer = ctypes.create_string_buffer(_PCAP_ERRBUF_SIZE)
            handle = self._pcap.pcap_open_live(
                device_name, 65535, 0, 40, error_buffer
            )
            if not handle:
                open_errors.append(_decode_error(error_buffer.value))
                continue
            filter_error = self._set_capture_filter(
                handle, self._effective_filter(accelerator_candidate)
            )
            if filter_error:
                open_errors.append(filter_error)
                self._pcap.pcap_close(handle)
                continue
            self._configure_live_delivery(handle)
            with self._device_lock:
                if self._stop_event.is_set() or device_name in self._opened_device_names:
                    self._pcap.pcap_close(handle)
                    continue
                adapter_index = self._next_adapter_index
                if adapter_index >= capture_health.ADAPTER_INDEX:
                    self._pcap.pcap_close(handle)
                    raise NpcapError("抓包网卡编号超过格式上限，无法安全保存")
                self._next_adapter_index += 1
                self._opened_device_names.add(device_name)
                self._handles.append(handle)
                self._adapter_descriptions[adapter_index] = (
                    _decode_error(description) or _decode_error(device_name)
                )
                if accelerator_candidate:
                    self._accelerator_adapter_indexes.add(adapter_index)
                thread = threading.Thread(
                    target=self._capture_loop,
                    args=(handle, adapter_index, device_name, accelerator_candidate),
                    name=f"NpcapCapture-{adapter_index}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

        if require_one and self.adapter_count <= 0:
            detail = next((item for item in open_errors if item), "没有可用网卡")
            raise NpcapError(
                f"Npcap 无法打开网卡：{detail}。若安装时启用了仅管理员访问，请重新安装并取消该选项"
            )

    def _configure_live_delivery(self, handle: ctypes.c_void_p) -> None:
        # Only configure a NEW handle, before its reader starts. setbuff
        # discards an existing buffer and must never be reapplied mid-capture.
        # A larger ring absorbs short game/GC stalls; a one-byte threshold
        # avoids waiting for the default 16 KB batch on quiet game streams.
        for name, value in (("pcap_setbuff", 32 * 1024 * 1024), ("pcap_setmintocopy", 1)):
            setter = getattr(self._pcap, name, None)
            if setter is not None and setter(handle, value) != 0:
                # Optional tuning failure must not enter the fatal read-error
                # queue. Older drivers can continue with their default ring.
                warnings = list(getattr(self, "_capture_delivery_warnings", ()))
                warnings.append(f"{name}: {self._handle_error(handle)}")
                self._capture_delivery_warnings = tuple(warnings[-16:])

    def _start_capture(self, capture_filter: str) -> None:
        self._refresh_capture_devices(require_one=True)

    def _capture_loop(
        self,
        handle: ctypes.c_void_p,
        adapter_index: int,
        device_name: bytes,
        accelerator_candidate: bool,
    ) -> None:
        # Do not lower the Npcap reader itself.  This thread only drains the
        # driver buffer and queues raw frames; decoding and aggregation happen
        # on separate background threads.  At BELOW_NORMAL priority a busy
        # game can starve the reader for minutes even though the Python raw
        # queue is empty, leaving the driver to deliver old packet timestamps
        # long after the corresponding Buff/Debuff happened.
        set_current_thread_normal_priority()
        applied_filter_generation = 0
        applied_filter_text = None
        last_driver_stats_at = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                pcap_stats = getattr(self._pcap, "pcap_stats", None)
                if pcap_stats is not None and now - last_driver_stats_at >= 5.0:
                    last_driver_stats_at = now
                    counters = _PcapStatistics()
                    try:
                        if pcap_stats(handle, ctypes.byref(counters)) == 0:
                            with self._driver_stats_lock:
                                self._driver_stats_by_adapter[adapter_index] = (
                                    int(counters.ps_recv),
                                    int(counters.ps_drop),
                                    int(counters.ps_ifdrop),
                                )
                    except Exception:
                        # Statistics are diagnostic only and must not stop
                        # the reader or delay the actual combat packets.
                        pass
                with self._filter_lock:
                    discovery_expired = False
                    if (
                        self._accelerator_discovery
                        and time.monotonic() >= self._accelerator_discovery_until
                    ):
                        self._accelerator_discovery = False
                        self._accelerator_discovery_until = 0.0
                        self._accelerator_discovery_hold_until = 0.0
                        discovery_expired = True
                    if (
                        self._accelerator_tcp_discovery
                        and time.monotonic()
                        >= self._accelerator_tcp_discovery_until
                    ):
                        self._accelerator_tcp_discovery = False
                        self._accelerator_tcp_discovery_until = 0.0
                        self._accelerator_tcp_discovery_hold_until = 0.0
                        discovery_expired = True
                    if discovery_expired:
                        self._filter_generation += 1
                    filter_generation = self._filter_generation
                if filter_generation != applied_filter_generation:
                    filter_text = self._effective_filter(accelerator_candidate)
                    filter_error = (
                        self._set_capture_filter(handle, filter_text)
                        if filter_text != applied_filter_text else ""
                    )
                    if filter_error:
                        if not self._stop_event.is_set():
                            self._errors.put(
                                f"Npcap update filter failed: {filter_error}"
                            )
                        return
                    applied_filter_generation = filter_generation
                    applied_filter_text = filter_text
                header = ctypes.POINTER(_PcapPacketHeader)()
                packet_data = ctypes.POINTER(ctypes.c_ubyte)()
                result = self._pcap.pcap_next_ex(
                    handle, ctypes.byref(header), ctypes.byref(packet_data)
                )
                if result == 0:
                    continue
                if result in (-2,):
                    if not self._stop_event.is_set():
                        self._capture_failed = True
                        self._errors.put("Npcap 抓包驱动意外结束，记录可能不完整")
                        self._stop_event.set()
                    return
                if result < 0:
                    if not self._stop_event.is_set():
                        self._capture_failed = True
                        self._errors.put(f"Npcap 驱动读包失败，记录可能不完整：{self._handle_error(handle)}")
                        self._stop_event.set()
                    return
                packet_header = header.contents
                frame = ctypes.string_at(packet_data, packet_header.caplen)
                timestamp = (
                    float(packet_header.ts.tv_sec)
                    + float(packet_header.ts.tv_usec) / 1_000_000.0
                )
                original_length = int(packet_header.length)
                if _is_simulator_loopback_tcp(frame):
                    continue
                # Do not decode/classify on the driver reader. Unknown UDP,
                # compound envelopes and large transfers MUST remain in RAW.
                # Update the authoritative live clock before queueing or trace
                # I/O.  Even if decoding or disk flushing falls behind, the UI
                # still knows when the newest raw frame reached Npcap.
                self._remember_capture_clock(timestamp)
                with self._trace_lock:
                    if self._trace_handle is not None:
                        self._write_trace_record_locked(timestamp, frame, original_length, adapter_index)
                    if not getattr(self, "_decoder_replay_required", ""):
                        try:
                            self._packets.put((timestamp, frame, original_length, adapter_index))
                        except OSError as exc:
                            self._report_capture_queue_error(str(exc))
                            if self._trace_handle is None:
                                raise
        except Exception as exc:
            if not self._stop_event.is_set():
                self._capture_failed = True
                self._errors.put(f"Npcap 读包线程失败，记录可能不完整：{exc}")
                self._stop_event.set()
        finally:
            # The owning reader samples once more before the native handle
            # disappears. The five-second periodic sample can miss tail drops.
            pcap_stats = getattr(self._pcap, "pcap_stats", None)
            if pcap_stats is not None:
                try:
                    counters = _PcapStatistics()
                    if pcap_stats(handle, ctypes.byref(counters)) == 0:
                        with self._driver_stats_lock:
                            self._driver_stats_by_adapter[adapter_index] = (
                                int(counters.ps_recv), int(counters.ps_drop), int(counters.ps_ifdrop))
                except Exception:
                    pass
            with self._device_lock:
                if handle in self._handles:
                    self._handles.remove(handle)
                self._opened_device_names.discard(device_name)
                self._accelerator_adapter_indexes.discard(adapter_index)
                self._adapter_descriptions.pop(adapter_index, None)
                # request_stop calls pcap_breakloop under this same lock.
                # Never free the native handle between its membership check
                # and that cross-thread call (the 4.68 crash was this race).
                self._pcap.pcap_close(handle)

    def read_packets(self, limit: int = 10000) -> list[tuple[float, bytes, int, int]]:
        get_many = getattr(self._packets, "get_many", None)
        if callable(get_many):
            return get_many(limit)
        packets: list[tuple[float, bytes, int, int]] = []
        for _ in range(limit):
            try:
                packets.append(self._packets.get_nowait())
            except queue.Empty:
                break
        return packets

    def read_error(self) -> str:
        try:
            return self._errors.get_nowait()
        except queue.Empty:
            return ""

    def request_stop(self) -> None:
        """Stop accepting frames immediately without joining from the caller."""

        self._stop_event.set()
        # ``pcap_breakloop`` is the only safe cross-thread wake-up. Closing a
        # handle here can race with ``pcap_next_ex`` and the reader's finally
        # block, which was the source of native access violations on shutdown.
        breakloop = getattr(self._pcap, "pcap_breakloop", None)
        if not callable(breakloop):
            return
        with self._device_lock:
            # A reader removes and closes its handle under _device_lock.
            # Snapshotting here and calling breakloop *after* unlocking let
            # the reader close the native pointer in between: an access
            # violation terminated the whole backend during Stop.
            for handle in tuple(self._handles):
                try:
                    breakloop(handle)
                except Exception:
                    # The 40 ms read timeout remains a safe fallback.
                    pass

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.request_stop()
            with self._device_lock:
                threads = tuple(self._threads)
            # Every handle is owned and finally closed by exactly one capture
            # thread. The controller only asks libpcap to break and joins; it
            # must never free a handle while ``pcap_next_ex`` may still use it.
            join_deadline = time.monotonic() + 2.5
            for thread in threads:
                thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
            alive = tuple(thread for thread in threads if thread.is_alive())
            if alive:
                # Retry the safe wake once. This is bounded and still avoids
                # the former use-after-free/double-close path.
                self.request_stop()
                retry_deadline = time.monotonic() + 1.0
                for thread in alive:
                    thread.join(
                        timeout=max(0.0, retry_deadline - time.monotonic())
                    )
                alive = tuple(thread for thread in alive if thread.is_alive())
            if alive:
                raise NpcapError(
                    "Npcap 抓包线程未能安全退出；已保留句柄以避免驱动层闪退"
                )
            with self._device_lock:
                # Reader finally blocks have already removed and closed every
                # registered handle. Clearing these containers is metadata-only.
                self._handles.clear()
                self._threads.clear()
                self._opened_device_names.clear()
                self._accelerator_adapter_indexes.clear()
                self._adapter_descriptions.clear()
            seal_packets = getattr(self._packets, "seal", None)
            seal_error = None
            if callable(seal_packets):
                try:
                    seal_packets()
                except Exception as exc:
                    if not getattr(self, "_decoder_replay_required", ""):
                        self._capture_failed = True
                        seal_error = exc
            trace_error = None
            if self._trace_handle is not None:
                with self._trace_lock:
                    try:
                        self._append_session_evidence_trace_frames_locked()
                        self._append_identity_trace_frames_locked()
                        self._append_identity_trace_bindings_locked()
                        self._append_profile_identity_bindings_locked()
                        self._append_capture_health_locked()
                        self._trace_handle.flush()
                    except Exception as exc:
                        self._capture_failed = True
                        trace_error = exc
                    # Even an appendix/flush failure must seal the writer. Its
                    # worker remains the sole file owner if draining times out.
                    try:
                        self._trace_handle.close()
                    except Exception as exc:
                        self._capture_failed = True
                        trace_error = trace_error or exc
                    self._trace_final_hash = self._trace_handle.content_hash
                    self._trace_handle = None
            if self._dll_directory is not None:
                self._dll_directory.close()
                self._dll_directory = None
            self._closed = True
            if seal_error is not None or trace_error is not None:
                raise NpcapError(str(seal_error or trace_error))

    @property
    def packet_queue_stats(self) -> dict[str, int]:
        stats = getattr(self._packets, "stats", None)
        if stats is None:
            qsize = getattr(self._packets, "qsize", None)
            return {"pending": int(qsize()) if callable(qsize) else 0}
        return {
            "pending": int(stats.pending),
            "memoryItems": int(stats.memory_items),
            "memoryBytes": int(stats.memory_bytes),
            "spillItems": int(stats.spill_items),
            "peakPending": int(stats.peak_pending),
            "totalSpilled": int(stats.total_spilled),
        }

    @property
    def trace_content_hash(self) -> str:
        """Return the incremental hash after the trace has been closed."""

        with self._trace_lock:
            if getattr(self, "_capture_failed", False):
                return ""
            writer = getattr(self, "_trace_writer", None)
            if writer is not None:
                return writer.content_hash
            final_hash = str(getattr(self, "_trace_final_hash", "") or "")
            if final_hash:
                return final_hash
            if self._trace_handle is None:
                trace_digest = getattr(self, "_trace_digest", None)
                if trace_digest is not None:
                    return trace_digest.hexdigest()
        return ""

    @property
    def trace_writer_stats(self) -> dict[str, object]:
        writer = getattr(self, "_trace_writer", None)
        return writer.stats if writer is not None else {}
