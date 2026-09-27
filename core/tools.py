"""Locating and running the optional external tools (JPEG XL's cjxl/djxl).

The tools are not shipped. They are found, in order, in STORAGEARCH_JXL_TOOLS_DIR, next to a frozen
build's bundled data, or in a `tools` folder next to this package. Every call has its own timeout, runs
without flashing a console window, and never inherits a shell.
"""
import os
import subprocess
import sys
import threading

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_TOOL_TIMEOUT_S = 300.0


class ToolError(RuntimeError):
    pass


def tools_dir():
    configured = os.environ.get("STORAGEARCH_JXL_TOOLS_DIR")
    if configured:
        return configured
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "tools")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def tool_path(name):
    exe = name + (".exe" if os.name == "nt" else "")
    return os.path.join(tools_dir(), exe)


def available(*names):
    missing = [n for n in names if not os.path.isfile(tool_path(n))]
    if missing:
        return False, f"{', '.join(missing)} not found in {tools_dir()} (set STORAGEARCH_JXL_TOOLS_DIR)"
    return True, ""


def run(name, args, timeout_s=DEFAULT_TOOL_TIMEOUT_S, paths=()):
    """Runs a tool with arguments (no shell). Every argument listed in `paths` is made absolute first, so
    a file name starting with '-' can never be read as an option. Raises ToolError on a non-zero exit, a
    timeout or a missing tool; the tool process is killed on timeout by subprocess itself."""
    args = [os.path.abspath(a) if i in paths else a for i, a in enumerate(args)]
    path = tool_path(name)
    if not os.path.isfile(path):
        raise ToolError(f"{name} is not available")
    try:
        proc = subprocess.Popen([path, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    except OSError as exc:
        raise ToolError(f"{name} could not start: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise ToolError(f"{name} timed out after {timeout_s}s") from exc
    finally:
        _add_cpu(_process_cpu_seconds(proc))
    if proc.returncode != 0:
        message = (stderr or stdout or b"").decode("utf-8", "replace").strip().splitlines()
        raise ToolError(f"{name} exited with {proc.returncode}: {message[-1] if message else ''}")
    return proc


# --- CPU time of tool runs -----------------------------------------------------------------------------
# The policy judges a candidate by the CPU time its work takes, not by wall-clock time: wall time grows with
# whatever else runs at the moment, so the same file could win alone and lose inside a busy folder archive.
# A tool's CPU time is its own process's; it is added to a per-thread counter the candidate reads.

_counter = threading.local()


def cpu_seconds():
    """CPU seconds of the tools this thread has run so far."""
    return getattr(_counter, "value", 0.0)


def _add_cpu(seconds):
    _counter.value = cpu_seconds() + seconds


def _process_cpu_seconds(proc):
    if os.name != "nt":
        return 0.0  # the app is Windows-only; elsewhere a tool's CPU time is simply not counted
    import ctypes
    from ctypes import wintypes
    handle = getattr(proc, "_handle", None)
    if not handle:
        return 0.0
    creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = ctypes.windll.kernel32.GetProcessTimes(wintypes.HANDLE(int(handle)), ctypes.byref(creation),
                                                ctypes.byref(exit_), ctypes.byref(kernel), ctypes.byref(user))
    if not ok:
        return 0.0
    to_s = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7  # noqa: E731 - 100 ns units
    return to_s(kernel) + to_s(user)
