"""Runs one candidate under its limits and turns the result into a CandidateOutcome.

In-process (on the calling thread, behind a CPU gate) only when the strategy is in-process-safe and the
file is at most its max_in_process_bytes - a bound measured so the work finishes well inside the
timeout. Everything else runs in a killable worker. Both paths run candidate.run_candidate, so both
restore and verify (I1) and both are bounded (I5).
"""
import os
import shutil
import threading
from dataclasses import dataclass

from core import candidate
from core.workers import WorkerPool

CPU_GATE = threading.BoundedSemaphore(max(1, os.cpu_count() or 1))
# Heavy strategies (JPEG XL, LZMA, Brotli) additionally share half the cores: their measured speed feeds the
# policy, and it must reflect the work, not a pile-up of every heavy candidate of a folder at once.
HEAVY_GATE = threading.BoundedSemaphore(max(1, (os.cpu_count() or 2) // 2))


class _Gates:
    def __init__(self, heavy):
        self.heavy = heavy

    def __enter__(self):
        if self.heavy:
            HEAVY_GATE.acquire()
        CPU_GATE.acquire()

    def __exit__(self, *exc):
        CPU_GATE.release()
        if self.heavy:
            HEAVY_GATE.release()


@dataclass(frozen=True)
class CandidateOutcome:
    name: str
    status: str
    stored_path: str | None = None
    stored_size: int | None = None
    compress_s: float = 0.0
    restore_s: float = 0.0
    detail: str = ""
    where: str = ""                 # "thread" or "worker"

    @property
    def verified(self):
        return self.status == candidate.VERIFIED


class Executor:
    """Belongs to one OperationScope; its worker pool is created on first need and closed with the scope."""

    def __init__(self, scope, pool_size=None, allow_in_process=True):
        self.scope = scope
        self.pool_size = pool_size
        self.allow_in_process = allow_in_process
        self._pool = None
        self._pool_lock = threading.Lock()

    def pool(self):
        with self._pool_lock:
            if self._pool is None:
                self._pool = WorkerPool(self.pool_size)
                self.scope.on_close(self._pool.close)
            return self._pool

    def runs_in_process(self, strategy, size):
        spec = strategy.spec
        return self.allow_in_process and spec.in_process_safe and size <= spec.max_in_process_bytes

    def run(self, strategy, src_path, info):
        """`info` is the file's ContentInfo (size and SHA-256 of the original)."""
        name = strategy.name
        if self.scope.cancelled:
            return CandidateOutcome(name, candidate.CANCELLED, detail="operation cancelled")
        try:
            ok, reason = strategy.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            return CandidateOutcome(name, candidate.UNAVAILABLE, detail=reason)

        timeout_s = strategy.spec.timeout_s(info.size)
        work_dir = self.scope.work_dir(name)
        if self.runs_in_process(strategy, info.size):
            with _Gates(strategy.spec.weight == "heavy"):
                result = candidate.run_candidate(strategy, src_path, work_dir, info.sha256, timeout_s)
            where = "thread"
        else:
            result = self.pool().run(strategy, src_path, work_dir, info.sha256, timeout_s, self.scope._cancel)
            where = "worker"

        outcome = CandidateOutcome(
            name=name, status=result.get("status", candidate.ERROR), stored_path=result.get("stored_path"),
            stored_size=result.get("stored_size"), compress_s=result.get("compress_s", 0.0),
            restore_s=result.get("restore_s", 0.0), detail=result.get("detail", ""), where=where,
        )
        if not outcome.verified:
            shutil.rmtree(work_dir, ignore_errors=True)  # nothing of a failed candidate is kept
        return outcome

    def restore(self, strategy, stored_path, out_dir, original_size):
        """Restores a stored file under the same rules as a candidate: on a thread only within the strategy's
        in-process bound, otherwise in a killable worker. Returns the restored path; raises RestoreFailed."""
        if self.scope.cancelled:
            raise RestoreFailed("operation cancelled")
        timeout_s = strategy.spec.timeout_s(original_size)
        if self.runs_in_process(strategy, original_size):
            with _Gates(strategy.spec.weight == "heavy"):
                result = candidate.run_restore(strategy, stored_path, out_dir, timeout_s)
        else:
            result = self.pool().run_restore(strategy, stored_path, out_dir, timeout_s, self.scope._cancel)
        if result.get("status") != candidate.VERIFIED:
            raise RestoreFailed(f"{strategy.name}: {result.get('status')} {result.get('detail', '')}".strip())
        return result["restored_path"]


class RestoreFailed(Exception):
    pass
