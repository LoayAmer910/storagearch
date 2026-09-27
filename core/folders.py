"""Reading a folder as an item: its files and directories (empty ones included), what is skipped and why,
and the folder's content hash.

Links (symlinks, junctions) are never followed - a link could pull in files from anywhere on the disk -
and are reported as skipped, like cloud placeholders that are not downloaded. A name Windows cannot
recreate is an error, not a silent skip. The folder hash is a SHA-256 over a length-prefixed, sorted list
of (relative path, kind, content hash), so no two different folders can encode to the same input.
"""
import hashlib
import os
from dataclasses import dataclass

from core import paths
from core.catalog import EMPTY_DIGEST


@dataclass(frozen=True)
class Entry:
    rel_path: str      # '/'-separated, relative to the folder
    kind: str          # "file" or "dir"
    full_path: str


@dataclass(frozen=True)
class Skipped:
    rel_path: str
    full_path: str
    reason: str        # "cloud" or "link"


def _is_link(path):
    return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def scan(root):
    """Returns (entries, skipped). Raises paths.InvalidPathError for a name that cannot be restored."""
    root = os.path.abspath(root)
    entries, skipped = [], []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        rel_folder = os.path.relpath(folder, root).replace(os.sep, "/")
        rel_folder = "" if rel_folder == "." else rel_folder
        keep_dirs = []
        for d in dirs:
            full = os.path.join(folder, d)
            rel = f"{rel_folder}/{d}" if rel_folder else d
            if _is_link(full):
                skipped.append(Skipped(rel, full, "link"))
                continue
            _check_name(d, full)
            entries.append(Entry(rel, "dir", full))
            keep_dirs.append(d)
        dirs[:] = keep_dirs
        for f in files:
            full = os.path.join(folder, f)
            rel = f"{rel_folder}/{f}" if rel_folder else f
            if _is_link(full):
                skipped.append(Skipped(rel, full, "link"))
            elif paths.is_cloud_placeholder(full):
                skipped.append(Skipped(rel, full, "cloud"))
            else:
                _check_name(f, full)
                entries.append(Entry(rel, "file", full))
    return entries, skipped


def _check_name(name, full):
    problem = paths.name_problem(name)
    if problem:
        raise paths.InvalidPathError(f"Cannot archive '{full}': {problem}")


def digest(rows):
    """rows: iterable of (rel_path, kind, sha256). Order does not matter."""
    h = hashlib.sha256()
    for rel, kind, sha in sorted(rows):
        for value in (rel, kind, sha):
            data = value.encode("utf-8")
            h.update(len(data).to_bytes(8, "big"))
            h.update(data)
    return h.hexdigest()


def folder_hash(root, file_hash):
    """The folder's content hash, using file_hash(path) for each file."""
    entries, _skipped = scan(root)
    return digest((e.rel_path, e.kind, file_hash(e.full_path) if e.kind == "file" else EMPTY_DIGEST) for e in entries)
