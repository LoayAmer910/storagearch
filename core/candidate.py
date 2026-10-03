"""The one implementation of "try a candidate": compress, restore, compare the SHA-256 with the original.

The same function runs on a thread and inside a worker process, so the two paths cannot drift apart
(in the old version the fast path skipped the restore check). It never raises: every failure becomes a
result with a status. The stored file stays in the work dir; only small facts travel back. compress_s and
restore_s are CPU seconds (this thread plus the tools it ran), so the policy's speed rule does not depend
on how busy the machine is.
"""
import hashlib
import os
import shutil
import tempfile
import time

from core import tools

VERIFIED, ERROR, MISMATCH, TIMEOUT, CRASHED, CANCELLED, UNAVAILABLE = (
    "verified", "error", "mismatch", "timeout", "crashed", "cancelled", "unavailable")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


def _inside(path, folder):
    path, folder = os.path.abspath(path), os.path.abspath(folder)
    return os.path.commonpath([path, folder]) == folder and path != folder


def _cpu_now():
    """CPU seconds spent so far by this thread plus the tools it ran - the cost of the work itself, not of
    waiting for a busy machine (wall time made the same file win alone and lose in a busy folder)."""
    return time.thread_time() + tools.cpu_seconds()


def run_candidate(strategy, src_path, work_dir, expected_sha256, time_limit_s=None):
    compress_dir = tempfile.mkdtemp(prefix="c_", dir=work_dir)
    restore_dir = tempfile.mkdtemp(prefix="r_", dir=work_dir)
    try:
        t0 = _cpu_now()
        stored = strategy.compress(src_path, compress_dir, time_limit_s)
        compress_s = _cpu_now() - t0
        if not (isinstance(stored, str) and _inside(stored, compress_dir) and os.path.isfile(stored)):
            return {"status": ERROR, "detail": "compress did not return a file inside its work dir"}
        stored_size = os.path.getsize(stored)

        t0 = _cpu_now()
        restored = strategy.restore(stored, restore_dir, time_limit_s)
        restore_s = _cpu_now() - t0
        if not (isinstance(restored, str) and _inside(restored, restore_dir) and os.path.isfile(restored)):
            return {"status": ERROR, "detail": "restore did not return a file inside its work dir"}
        status = VERIFIED if sha256_file(restored) == expected_sha256 else MISMATCH
        return {"status": status, "stored_path": stored if status == VERIFIED else None, "stored_size": stored_size,
                "compress_s": compress_s, "restore_s": restore_s,
                "detail": "" if status == VERIFIED else "restored bytes differ from the original"}
    except Exception as exc:  # noqa: BLE001 - a failing candidate is a result, never an error of the caller
        return {"status": ERROR, "detail": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)


def run_candidate_in_memory(strategy, data, expected_sha256):
    """run_candidate for a small file held in memory, by a StreamingStrategy: the same compress, restore and
    SHA-256 comparison, without a temporary file - nothing is written and read back from disk (a file that
    was just written is scanned when it is opened again, which made small files slow). The stored bytes
    travel back as "stored_bytes". `data` must be a single chunk (at most base.CHUNK bytes): then the stored
    bytes are exactly the ones the file form gives."""
    try:
        t0 = _cpu_now()
        stored = b"".join(strategy.encode([data] if data else [], len(data)))
        compress_s = _cpu_now() - t0
        t0 = _cpu_now()
        h = hashlib.sha256()
        for piece in strategy.decode([stored]):
            h.update(piece)
        restore_s = _cpu_now() - t0
        status = VERIFIED if h.hexdigest() == expected_sha256 else MISMATCH
        return {"status": status, "stored_bytes": stored if status == VERIFIED else None, "stored_size": len(stored),
                "compress_s": compress_s, "restore_s": restore_s,
                "detail": "" if status == VERIFIED else "restored bytes differ from the original"}
    except Exception as exc:  # noqa: BLE001 - a failing candidate is a result, never an error of the caller
        return {"status": ERROR, "detail": f"{type(exc).__name__}: {exc}"[:500]}


def run_restore(strategy, stored_path, out_dir, time_limit_s=None):
    """Restores one stored file into out_dir. Never raises: returns {"status", "restored_path", "detail"}.
    The caller compares the result with the original SHA-256."""
    try:
        restored = strategy.restore(stored_path, out_dir, time_limit_s)
        if not (isinstance(restored, str) and _inside(restored, out_dir) and os.path.isfile(restored)):
            return {"status": ERROR, "detail": "restore did not return a file inside its work dir"}
        return {"status": VERIFIED, "restored_path": restored, "detail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": ERROR, "detail": f"{type(exc).__name__}: {exc}"[:500]}
