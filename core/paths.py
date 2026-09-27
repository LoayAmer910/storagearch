"""Every decision about where a file may be read from or written to.

safe_target() is the only function that turns a name stored in an archive (or in the database) into a
path on disk; everything that extracts goes through it (I7). It refuses - with ExtractionRefused, never
another exception - anything that could land outside the destination: '..', absolute or drive paths,
backslashes, alternate data streams, reserved device names. Protected locations are resolved through
symlinks and junctions and taken from the system, not hard-coded to C:.
"""
import ctypes
import os
import unicodedata

INVALID_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_PATH = 260


class ExtractionRefused(Exception):
    """A stored name would place a file outside its destination, or is not a valid Windows name."""


class InvalidPathError(OSError):
    """A source path cannot be read because of its name or length."""


class CloudPlaceholderError(InvalidPathError):
    """A cloud (Files On-Demand) placeholder that is not downloaded to this device."""


# --- names ---------------------------------------------------------------------------------------------

def name_problem(component):
    """Why a single path component is not a valid Windows file name, or None."""
    if component in ("", ".", ".."):
        return f"'{component}' is not a name"
    stem = component.split(".", 1)[0].rstrip(" ")
    if stem.upper() in RESERVED_NAMES:
        return f"'{component}' is a reserved system name"
    if component != component.rstrip(" ."):
        return f"'{component}' ends with a space or a dot"
    bad = sorted(set(component) & INVALID_NAME_CHARS)
    if bad:
        return f"'{component}' contains invalid characters"
    return None


def bare_name(name, fallback="item"):
    """Reduces any name - possibly a path from a tampered database - to one safe file name component."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    name = "".join("_" if ch in INVALID_NAME_CHARS else ch for ch in name).rstrip(" .").strip()
    if name in ("", ".", "..") or name_problem(name):
        name = "_" + name if name and name not in (".", "..") else fallback
        if name_problem(name):
            name = fallback
    return name


# --- extraction targets --------------------------------------------------------------------------------

def split_member(member_path):
    """Validates a stored, '/'-separated relative path and returns its components."""
    if not isinstance(member_path, str) or not member_path:
        raise ExtractionRefused("empty stored name")
    if "\\" in member_path or member_path.startswith("/") or "\x00" in member_path:
        raise ExtractionRefused(f"stored name is not a plain relative path: {member_path!r}")
    parts = member_path.rstrip("/").split("/")
    for part in parts:
        problem = name_problem(part)
        if problem:
            raise ExtractionRefused(f"stored name {member_path!r}: {problem}")
    return parts


def safe_target(destination, member_path):
    """The absolute path under `destination` for a stored relative path; ExtractionRefused otherwise."""
    parts = split_member(member_path)
    try:
        root = os.path.realpath(os.path.abspath(destination))
        target = os.path.abspath(os.path.join(root, *parts))
        if os.path.splitdrive(target)[0].lower() != os.path.splitdrive(root)[0].lower():
            raise ExtractionRefused(f"stored name {member_path!r} changes the drive")
        if os.path.commonpath([target, root]) != root or target == root:
            raise ExtractionRefused(f"stored name {member_path!r} leaves the destination")
    except (ValueError, OSError) as exc:
        raise ExtractionRefused(f"stored name {member_path!r} cannot be placed: {exc}") from exc
    return target


# --- sources -------------------------------------------------------------------------------------------

def is_cloud_placeholder(path):
    if os.name != "nt":
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except (OSError, AttributeError):
        return False
    if attrs == 0xFFFFFFFF:
        return False
    return bool(attrs & (0x1000 | 0x40000 | 0x400000))  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


def validate_source_path(path):
    """(True, None) if the path can be read as a source, else (False, reason)."""
    if not isinstance(path, str) or not path.strip():
        return False, "Path is empty"
    if not os.path.isabs(path):
        return False, "Path must be absolute"
    if len(path) > MAX_PATH:
        return False, f"Path is too long ({len(path)} > {MAX_PATH} characters)"
    if not os.path.exists(path):
        return False, "Path does not exist"
    _drive, tail = os.path.splitdrive(path)
    for component in tail.replace("/", "\\").split("\\"):
        if component:
            problem = name_problem(component)
            if problem:
                return False, problem
    return True, None


# --- protected locations -------------------------------------------------------------------------------

def _system_roots():
    roots = []
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        value = os.environ.get(var)
        if value:
            roots.append(value)
    drive = os.environ.get("SystemDrive", "C:")
    roots += [drive + "\\Windows", drive + "\\Program Files", drive + "\\Program Files (x86)", drive + "\\ProgramData"]
    return roots


def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path))).rstrip("\\/")


def is_under(path, root):
    try:
        p, r = _norm(path), _norm(root)
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r + os.sep)


def is_protected(path, extra_roots=()):
    """System folders (taken from the environment, followed through links) and any extra roots - such as
    the app's own data folder - may not be archived, recycled or used as a workspace."""
    try:
        _norm(path)
    except (OSError, ValueError):
        return True
    return any(is_under(path, root) for root in (*_system_roots(), *extra_roots))
