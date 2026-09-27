"""Limited, privacy-safe environment info (plan §6.3) for app_started only.
Explicitly excludes CPU model, serial numbers, machine name, username, or
exact anything - `platform.platform()`/`platform.machine()` never include
those on Windows (unlike `platform.node()`, which returns the computer name
and is never called anywhere in this module or package).
"""
import ctypes
import platform


def windows_version():
    try:
        return platform.platform() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def architecture():
    try:
        return platform.machine() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _total_ram_bytes():
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys
    except Exception:  # noqa: BLE001
        pass
    return None


_RAM_BUCKETS = [
    (8 * 1024 ** 3, "0-8gb"),
    (16 * 1024 ** 3, "8-16gb"),
    (32 * 1024 ** 3, "16-32gb"),
    (64 * 1024 ** 3, "32-64gb"),
]


def ram_bucket():
    total = _total_ram_bytes()
    if total is None:
        return "unknown"
    for threshold, label in _RAM_BUCKETS:
        if total <= threshold:
            return label
    return "64gb_plus"
