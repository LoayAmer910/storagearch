"""Deciding how to store one file.

One read of the file gives its kind, its SHA-256 and the size of the baseline (a plain ZIP member:
Deflate, or stored uncompressed when Deflate would not shrink it). Then the router's candidates run -
in parallel, each through the executor, each restored and verified - the policy judges the verified
ones against the baseline, and the smallest accepted one wins. Failing candidates are data in the
Decision, never an exception (I4); nothing that was not verified can win (I1); a winner is always
strictly smaller than the baseline (I3).
"""
import concurrent.futures
import os
import shutil
import zlib
from dataclasses import dataclass, field, replace

from core import content
from core import policy as policy_module
from core.content import ContentInfo, ContentKind
from core.executor import CandidateOutcome
from core.scope import OperationCancelled

DEFLATE, STORED = "deflate", "stored"
BASELINE_METHODS = (DEFLATE, STORED)


@dataclass(frozen=True)
class Decision:
    info: ContentInfo
    method: str                         # "deflate", "stored" or the winning strategy's name
    strategy_version: int               # 0 for the baseline methods
    stored_path: str | None             # the winner's stored file; None for a baseline method
    stored_size: int
    baseline_method: str
    baseline_size: int
    candidates: tuple[CandidateOutcome, ...] = ()
    verdicts: dict = field(default_factory=dict)
    stored_bytes: bytes | None = None   # the winner's stored bytes, instead of stored_path, for a small file

    @property
    def smart(self):
        return self.method not in BASELINE_METHODS


# zipfile feeds its compressor 8 KiB at a time, and with zlib-ng (the zlib this Python ships) the Deflate
# output depends on those input boundaries. The counter and the archive writer use the same size, so the
# baseline measured here is exactly the member zipfile writes (measured: 1 MiB chunks gave 2 bytes less).
ZIP_WRITE_CHUNK = 8 * 1024


class _DeflateCounter:
    """Counts what zipfile's Deflate (zlib default level, raw stream) would produce, without keeping it."""

    def __init__(self):
        self._c = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
        self.size = 0

    def feed(self, chunk):
        view = memoryview(chunk)
        for start in range(0, len(view), ZIP_WRITE_CHUNK):
            self.size += len(self._c.compress(view[start:start + ZIP_WRITE_CHUNK]))

    def finish(self):
        self.size += len(self._c.flush())
        return self.size


def scan(path):
    """(ContentInfo, baseline method, baseline size) from a single read of the file."""
    counter = _DeflateCounter()
    info = content.classify(path, on_chunk=counter.feed)
    deflate_size = counter.finish()
    if deflate_size < info.size:
        return info, DEFLATE, deflate_size
    return info, STORED, info.size


class Pipeline:
    def __init__(self, executor, route, policy=policy_module.DEFAULT):
        """`route(info)` returns the strategies to try for a ContentInfo, in preference order."""
        self.executor = executor
        self.route = route
        self.policy = policy

    def decide(self, path, scanned=None):
        """`scanned`: the result of scan(path) when the caller already read the file (e.g. to check for a
        duplicate first), so the file is not read twice."""
        scope = self.executor.scope
        scope.raise_if_cancelled()
        info, baseline_method, baseline_size = scanned or scan(path)
        baseline = Decision(info, baseline_method, 0, None, baseline_size, baseline_method, baseline_size)
        if info.kind is ContentKind.EMPTY or not self.policy.worth_trying(info.size):
            return baseline

        chosen = list(self.route(info))
        if not chosen:
            return baseline
        data = _read_small(path, info.size) if any(self.executor.runs_in_memory(s, info.size) for s in chosen) else None
        if len(chosen) == 1:
            outcomes = [self.executor.run(chosen[0], path, info, data)]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="candidate") as pool:
                outcomes = list(pool.map(lambda s: self.executor.run(s, path, info, data), chosen))
        scope.raise_if_cancelled()

        verdicts = {}
        best = None
        for strategy, outcome in zip(chosen, outcomes):
            if not outcome.verified:
                continue
            verdict = self.policy.judge(info.size, baseline_size, outcome.stored_size, outcome.compress_s, outcome.restore_s)
            verdicts[outcome.name] = verdict
            if verdict.accepted and (best is None or outcome.stored_size < best[1].stored_size):
                best = (strategy, outcome)

        for outcome in outcomes:  # free the disk: only the winner's stored file is kept
            if outcome.stored_path and (best is None or outcome is not best[1]):
                _remove_candidate_dir(outcome.stored_path, scope.root)

        kept = tuple(replace(o, stored_bytes=None) if o.stored_bytes is not None else o for o in outcomes)
        if best is None:
            return Decision(info, baseline_method, 0, None, baseline_size, baseline_method, baseline_size, kept, verdicts)
        strategy, outcome = best
        stored_path, stored_bytes = outcome.stored_path, outcome.stored_bytes
        if stored_bytes is not None and not self.executor.keep_in_memory(len(stored_bytes)):
            stored_path, stored_bytes = _spill(stored_bytes, scope.work_dir(strategy.name)), None
        return Decision(info, strategy.name, strategy.spec.version, stored_path, outcome.stored_size,
                        baseline_method, baseline_size, kept, verdicts, stored_bytes=stored_bytes)


def _read_small(path, size):
    """The bytes of a small file, or None if it no longer has the size it was scanned with."""
    try:
        with open(path, "rb") as f:
            data = f.read(size + 1)
    except OSError:
        return None
    return data if len(data) == size else None


def _spill(stored_bytes, work_dir):
    """Writes stored bytes that do not fit in the operation's memory budget to a file."""
    path = os.path.join(work_dir, "stored")
    with open(path, "wb") as f:
        f.write(stored_bytes)
    return path


def _remove_candidate_dir(stored_path, scope_root):
    work_dir = os.path.dirname(os.path.dirname(os.path.abspath(stored_path)))
    root = os.path.abspath(scope_root)
    if os.path.commonpath([work_dir, root]) == root and work_dir != root:
        shutil.rmtree(work_dir, ignore_errors=True)


__all__ = ["BASELINE_METHODS", "DEFLATE", "STORED", "Decision", "OperationCancelled", "Pipeline", "scan"]
