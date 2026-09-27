"""OperationScope: the one owner of everything temporary an operation creates.

Every work directory lives under one root per operation (`storagearch_<pid>_<id>` in the temp folder),
every worker pool and cleanup action is registered here, and leaving the scope - normally, by an
exception or after a cancel - stops what is running and removes the root. A process that dies without
leaving its scopes is covered by sweep_stale(), which removes roots whose owning process is gone.
"""
import os
import shutil
import tempfile
import threading
import time
import uuid

from core import procs

ROOT_PREFIX = "storagearch_"


class OperationCancelled(Exception):
    pass


class OperationScope:
    def __init__(self, kind="operation", temp_root=None):
        self.kind = kind
        self.temp_root = temp_root or tempfile.gettempdir()
        self.root = None
        self._cleanups = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._closed = False

    def __enter__(self):
        os.makedirs(self.temp_root, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix=f"{ROOT_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:8]}_", dir=self.temp_root)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # --- ownership -----------------------------------------------------------------------------------

    def work_dir(self, label="work"):
        self._check_open()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:40] or "work"
        return tempfile.mkdtemp(prefix=safe + "_", dir=self.root)

    def on_close(self, callback):
        """Registers a cleanup action; actions run in reverse order when the scope closes."""
        with self._lock:
            self._check_open()
            self._cleanups.append(callback)

    # --- cancellation --------------------------------------------------------------------------------

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def raise_if_cancelled(self):
        if self._cancel.is_set():
            raise OperationCancelled(self.kind)

    # --- closing -------------------------------------------------------------------------------------

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cleanups, self._cleanups = self._cleanups[::-1], []
        errors = []
        for callback in cleanups:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - every cleanup must get its turn
                errors.append(exc)
        if self.root:
            remove_tree(self.root)
        if errors:
            raise errors[0]

    def _check_open(self):
        if self._closed:
            raise RuntimeError("the operation scope is already closed")
        if self.root is None:
            raise RuntimeError("the operation scope was not entered")


def remove_tree(path, attempts=5):
    """Removes a directory tree; on Windows a file may stay locked for a moment after its process died,
    so this retries briefly. Returns True when the tree is gone (what is left is for sweep_stale)."""
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(0.1 * (attempt + 1))
    return False


def sweep_stale(temp_root=None):
    """Removes operation roots left behind by processes that no longer run. Returns the removed paths."""
    temp_root = temp_root or tempfile.gettempdir()
    removed = []
    try:
        entries = os.listdir(temp_root)
    except OSError:
        return removed
    for entry in entries:
        if not entry.startswith(ROOT_PREFIX):
            continue
        pid_text = entry[len(ROOT_PREFIX):].split("_", 1)[0]
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == os.getpid() or procs.alive(pid):
            continue
        path = os.path.join(temp_root, entry)
        if remove_tree(path):
            removed.append(path)
    return removed
