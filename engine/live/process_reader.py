from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ProcessReadError(RuntimeError):
    pass


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


@dataclass(frozen=True, slots=True)
class MemoryModule:
    name: str
    path: str
    base_address: int
    size: int


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_size_t),
        ("Reserved3", ctypes.c_void_p),
    ]


@dataclass(slots=True)
class ReadOnlyProcess:
    pid: int
    process_name: str
    _handle: int

    @classmethod
    def attach(cls, process_name: str) -> "ReadOnlyProcess":
        if os.name != "nt":
            raise ProcessReadError("只读进程采集仅支持 Windows")
        kernel32 = _kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            raise ProcessReadError(f"无法枚举进程：{ctypes.get_last_error()}")
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            found_pid = 0
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    if entry.szExeFile.casefold() == process_name.casefold():
                        found_pid = int(entry.th32ProcessID)
                        break
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
        if not found_pid:
            raise ProcessReadError(f"未运行：{process_name}")
        return cls.attach_pid(found_pid, process_name)

    @classmethod
    def attach_pid(cls, pid: int, process_name: str = "") -> "ReadOnlyProcess":
        if os.name != "nt":
            raise ProcessReadError("只读进程采集仅支持 Windows")
        access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            raise ProcessReadError(f"无法以只读权限连接进程：{ctypes.get_last_error()}")
        return cls(int(pid), process_name, int(handle))

    def read(self, address: int, size: int) -> bytes:
        if address <= 0 or not (0 < size <= 16 * 1024 * 1024):
            raise ValueError("无效的只读内存范围")
        kernel32 = _kernel32()
        buffer = ctypes.create_string_buffer(size)
        read_count = ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(
            self._handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(read_count),
        )
        if not ok:
            raise ProcessReadError(f"读取失败：{ctypes.get_last_error()}")
        return buffer.raw[: read_count.value]

    def module(self, module_name: str | None = None) -> MemoryModule:
        kernel32 = _kernel32()
        flags = TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
        snapshot = kernel32.CreateToolhelp32Snapshot(flags, self.pid)
        if snapshot == INVALID_HANDLE_VALUE:
            raise ProcessReadError(f"无法枚举模块：{ctypes.get_last_error()}")
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    if module_name is None or entry.szModule.casefold() == module_name.casefold():
                        return MemoryModule(
                            name=entry.szModule,
                            path=entry.szExePath,
                            base_address=ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0,
                            size=int(entry.modBaseSize),
                        )
                    if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
        raise ProcessReadError(f"未找到模块：{module_name or self.process_name}")

    def image_base(self) -> int:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        ntdll.NtQueryInformationProcess.restype = wintypes.LONG
        info = PROCESS_BASIC_INFORMATION()
        returned = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(
            self._handle,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if status != 0 or not info.PebBaseAddress:
            raise ProcessReadError(f"无法读取进程基本信息：NTSTATUS 0x{status & 0xFFFFFFFF:08X}")
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        raw = self.read(int(info.PebBaseAddress) + pointer_size * 2, pointer_size)
        base = int.from_bytes(raw, "little")
        if base <= 0:
            raise ProcessReadError("主模块基址无效")
        return base

    def close(self) -> None:
        if self._handle:
            _kernel32().CloseHandle(self._handle)
            self._handle = 0

    def __enter__(self) -> "ReadOnlyProcess":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
