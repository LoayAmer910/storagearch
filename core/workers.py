"""A pool of spawned worker processes for candidates that must be killable.

Each worker has its own pipe. The parent waits in short slices, so a worker that dies is noticed within
a fraction of a second (not after the full timeout); one that runs past its timeout is killed together
with every process it started (e.g. a cjxl it launched) and is never reused, so a late result cannot be
taken for the next task's. The timeout starts when the task is handed to a ready worker - spawn time is
not charged to the candidate.
"""
import itertools
import multiprocessing
import os
import threading
import time

from core import candidate, procs

_CTX = multiprocessing.get_context("spawn")
READY_TIMEOUT_S = 120.0
_POLL_S = 0.05


def _worker_main(conn):
    conn.send(("ready", os.getpid()))
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        task_id, kind, args = message
        if kind == "restore":
            result = candidate.run_restore(*args)
        else:
            result = candidate.run_candidate(*args)
        try:
            conn.send((task_id, result))
        except (BrokenPipeError, OSError):
            return


class _Worker:
    def __init__(self):
        self.conn, child = _CTX.Pipe()
        self.process = _CTX.Process(target=_worker_main, args=(child,), daemon=True)
        self.process.start()
        child.close()
        self.pid = self.process.pid
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not self.conn.poll(_POLL_S):
            if not self.process.is_alive() or time.monotonic() > deadline:
                self.kill()
                raise RuntimeError("worker process did not start")
        try:
            message = self.conn.recv()
        except (EOFError, OSError):  # the process ended before it said it was ready (its pipe closed)
            self.kill()
            raise RuntimeError("worker process ended before it was ready") from None
        if message[0] != "ready":
            self.kill()
            raise RuntimeError("worker process sent an unexpected greeting")

    def kill(self):
        procs.kill_tree(self.pid)
        self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        self.conn.close()

    def stop(self):
        try:
            self.conn.send(None)
        except (BrokenPipeError, OSError):
            pass
        self.process.join(2)
        if self.process.is_alive():
            self.kill()
        else:
            self.conn.close()


class WorkerPool:
    def __init__(self, size=None):
        self.size = max(1, min(size or os.cpu_count() or 2, 8))
        self._slots = threading.BoundedSemaphore(self.size)
        self._idle = []
        self._live = set()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._closed = False

    def run(self, strategy, src_path, work_dir, expected_sha256, timeout_s, cancel_event=None):
        """Runs one candidate in a worker. Returns candidate.run_candidate's result dict, or one with status
        timeout / crashed / cancelled. Never raises for the candidate's own failures."""
        return self._run("candidate", (strategy, src_path, work_dir, expected_sha256, timeout_s), timeout_s, cancel_event)

    def run_restore(self, strategy, stored_path, out_dir, timeout_s, cancel_event=None):
        """Restores one stored file in a worker, under the same timeout and kill rules as a candidate."""
        return self._run("restore", (strategy, stored_path, out_dir, timeout_s), timeout_s, cancel_event)

    def _run(self, kind, args, timeout_s, cancel_event):
        with self._slots:
            worker = self._take()
            if worker is None:
                return {"status": candidate.CRASHED, "detail": "no worker process could be started"}
            task_id = next(self._ids)
            try:
                worker.conn.send((task_id, kind, args))
            except Exception as exc:  # noqa: BLE001 - e.g. an unpicklable strategy or a dead pipe
                self._discard(worker)
                return {"status": candidate.ERROR, "detail": f"could not hand the task to a worker: {exc}"[:500]}
            deadline = time.monotonic() + timeout_s
            while True:
                if worker.conn.poll(_POLL_S):
                    try:
                        got_id, result = worker.conn.recv()
                    except (EOFError, OSError):
                        self._discard(worker)
                        return {"status": candidate.CRASHED, "detail": "worker process died"}
                    if got_id == task_id:
                        self._give_back(worker)
                        return result
                    continue  # cannot happen for a worker that is never reused after a timeout; ignored
                if not worker.process.is_alive():
                    self._discard(worker)
                    return {"status": candidate.CRASHED, "detail": f"worker process exited with {worker.process.exitcode}"}
                if cancel_event is not None and cancel_event.is_set():
                    self._discard(worker)
                    return {"status": candidate.CANCELLED, "detail": "operation cancelled"}
                if time.monotonic() > deadline:
                    self._discard(worker)
                    return {"status": candidate.TIMEOUT, "detail": f"exceeded {timeout_s:.1f}s - worker killed"}

    def close(self):
        with self._lock:
            self._closed = True
            workers, self._idle = list(self._live), []
            self._live.clear()
        for worker in workers:
            worker.stop()

    def live_pids(self):
        with self._lock:
            return {w.pid for w in self._live}

    def _take(self):
        with self._lock:
            if self._closed:
                return None
            if self._idle:
                return self._idle.pop()
        try:
            worker = _Worker()
        except RuntimeError:
            return None
        with self._lock:
            if self._closed:
                closed = True
            else:
                closed = False
                self._live.add(worker)
        if closed:
            worker.stop()
            return None
        return worker

    def _give_back(self, worker):
        with self._lock:
            if not self._closed:
                self._idle.append(worker)
                return
        worker.stop()

    def _discard(self, worker):
        with self._lock:
            self._live.discard(worker)
        worker.kill()
