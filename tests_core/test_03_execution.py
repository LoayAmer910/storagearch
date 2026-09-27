"""Step 3 - running candidates (I1, I4, I5, I6). A candidate that raises, crashes its process, hangs,
restores wrong bytes, returns nothing or points outside its work dir becomes a status - never an error of
the caller, never a stored result. A hang is killed at its timeout together with any process it started;
a crash is noticed at once; the pool keeps working; nothing outlives the scope."""
import concurrent.futures
import multiprocessing
import os
import subprocess
import sys
import time

import pytest

import corpus
import fakes
from core import candidate, content, procs, strategies
from core.executor import Executor
from core.scope import OperationScope, ROOT_PREFIX, sweep_stale


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "input.txt"
    path.write_bytes(corpus.lines(3000, "row {i} {w} {n} payload\n"))
    return str(path)


@pytest.fixture
def info(src):
    return content.classify(src)


def _pid_from(marker, wait_s=5):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if os.path.isfile(marker) and open(marker).read().strip():
            return int(open(marker).read())
        time.sleep(0.05)
    raise AssertionError("the fault never ran - the test would prove nothing")


def _wait_dead(pid, wait_s=5):
    deadline = time.monotonic() + wait_s
    while procs.alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not procs.alive(pid)


def _live_children():
    return [p for p in multiprocessing.active_children() if p.is_alive()]


@pytest.mark.parametrize("in_process", [True, False], ids=["thread", "worker"])
def test_a_good_candidate_is_verified_and_kept_inside_the_scope(in_process, src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Fake(in_process=in_process), src, info)
        assert outcome.verified and outcome.where == ("thread" if in_process else "worker"), outcome
        assert os.path.commonpath([outcome.stored_path, scope.root]) == scope.root
        assert open(outcome.stored_path, "rb").read() == open(src, "rb").read()
        root = scope.root
    assert not os.path.exists(root)


FAULTS = {
    "raises": (fakes.Raising, candidate.ERROR),
    "wrong_restore": (fakes.WrongRestore, candidate.MISMATCH),
    "missing_output": (fakes.MissingOutput, candidate.ERROR),
    "outside_work_dir": (fakes.OutsideWorkDir, candidate.ERROR),
    "crashes": (fakes.Crashing, candidate.CRASHED),
    "hangs": (fakes.Hanging, candidate.TIMEOUT),
}


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("in_process", [True, False], ids=["thread", "worker"])
def test_a_failing_candidate_becomes_a_status_and_leaves_nothing(fault, in_process, src, info, tmp_path):
    if in_process and fault in ("crashes", "hangs"):
        pytest.skip("only a worker can survive a crash or be killed; the executor never runs these in-process")
    cls, expected = FAULTS[fault]
    marker = str(tmp_path / "ran")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        start = time.monotonic()
        outcome = Executor(scope).run(cls(name=f"f_{fault}", in_process=in_process, marker=marker), src, info)
        elapsed = time.monotonic() - start
        _pid_from(marker)
        assert outcome.status == expected and outcome.stored_path is None, outcome
        assert os.listdir(scope.root) == [], f"a failed candidate left {os.listdir(scope.root)}"
        if fault == "crashes":
            assert elapsed < fakes.TIMEOUT_S, f"a crash took {elapsed:.1f}s to notice - it waited for the timeout"
        if fault == "hangs":
            assert elapsed < fakes.TIMEOUT_S + 3, f"took {elapsed:.1f}s with a {fakes.TIMEOUT_S}s timeout"
    assert _live_children() == []


def test_a_timeout_kills_the_worker_and_everything_it_started(src, info, tmp_path):
    marker = str(tmp_path / "grandchild")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.HangingWithChild(name="f_child", marker=marker), src, info)
        grandchild = _pid_from(marker)
        assert outcome.status == candidate.TIMEOUT
        assert _wait_dead(grandchild), f"the process the worker started ({grandchild}) survived the kill"


def test_the_pool_keeps_working_after_timeouts_and_crashes(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, pool_size=1)
        sequence = [fakes.Hanging(name="h"), fakes.Fake(), fakes.Crashing(name="c"), fakes.Fake(), fakes.Slow(fakes.TIMEOUT_S + 1, name="s"), fakes.Fake()]
        statuses = [ex.run(s, src, info).status for s in sequence]
    assert statuses == [candidate.TIMEOUT, candidate.VERIFIED, candidate.CRASHED, candidate.VERIFIED,
                        candidate.TIMEOUT, candidate.VERIFIED]


def test_parallel_candidates_never_get_each_others_results(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, pool_size=3)
        tags = [f"t{i}" for i in range(15)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda t: ex.run(fakes.Tagged(t), src, info), tags))
        for tag, outcome in zip(tags, outcomes):
            assert outcome.verified, outcome
            assert open(outcome.stored_path, "rb").read().endswith(f"|{tag}".encode()), f"{tag} got another result"


def test_cancel_stops_a_running_candidate_quickly(src, info, tmp_path):
    marker = str(tmp_path / "ran")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope)
        slow = fakes.Hanging(name="h", marker=marker, timeout_s=60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ex.run, slow, src, info)
            pid = _pid_from(marker, wait_s=60)
            start = time.monotonic()
            scope.cancel()
            outcome = future.result(timeout=30)
        assert outcome.status == candidate.CANCELLED and time.monotonic() - start < 5
        assert _wait_dead(pid)
        assert ex.run(fakes.Fake(), src, info).status == candidate.CANCELLED  # nothing new starts after a cancel


def test_leaving_the_scope_by_an_exception_still_cleans_up(src, info, tmp_path):
    with pytest.raises(RuntimeError, match="boom"):
        with OperationScope(temp_root=str(tmp_path / "t")) as scope:
            ex = Executor(scope)
            assert ex.run(fakes.Fake(), src, info).verified
            pids = ex.pool().live_pids()
            root = scope.root
            raise RuntimeError("boom")
    assert not os.path.exists(root)
    assert pids and all(_wait_dead(p) for p in pids)
    assert _live_children() == []


def test_unavailable_strategies_never_run(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Unavailable(), src, info)
    assert outcome.status == candidate.UNAVAILABLE and "tool missing" in outcome.detail


def test_large_inputs_go_to_a_worker_even_for_in_process_safe_strategies(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.Fake(in_process=True, max_bytes=info.size - 1), src, info)
    assert outcome.verified and outcome.where == "worker"


@pytest.mark.parametrize("in_process", [True, False], ids=["thread", "worker"])
def test_real_strategies_verify_through_the_executor(in_process, tmp_path):
    samples = corpus.build(str(tmp_path / "corpus"))
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, allow_in_process=in_process)
        for sample_name, name in (("text.log", "zstd"), ("program.exe", "lzma_x86"), ("text.csv", "brotli"),
                                  ("large_text.txt", "zstd_ldm"), ("image.jpg", "jpeg_xl"), ("image.bmp", "bmp_jxl")):
            strategy = strategies.get(name)
            if not strategy.available()[0]:
                continue
            outcome = ex.run(strategy, samples[sample_name].path, content.classify(samples[sample_name].path))
            assert outcome.verified, (sample_name, name, outcome)


def test_sweep_removes_only_roots_of_dead_processes(tmp_path):
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(dead.stdout)
    assert not procs.alive(dead_pid)
    stale = tmp_path / f"{ROOT_PREFIX}{dead_pid}_abcd1234_x"
    mine = tmp_path / f"{ROOT_PREFIX}{os.getpid()}_abcd1234_y"
    odd = tmp_path / f"{ROOT_PREFIX}notapid_z"
    unrelated = tmp_path / "other_folder"
    for folder in (stale, mine, odd, unrelated):
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "f.bin").write_bytes(b"x")
    removed = sweep_stale(str(tmp_path))
    assert removed == [str(stale)] and mine.exists() and odd.exists() and unrelated.exists()


def test_a_closed_scope_refuses_new_work(tmp_path):
    scope = OperationScope(temp_root=str(tmp_path))
    with pytest.raises(RuntimeError):
        scope.work_dir()
    with scope:
        pass
    with pytest.raises(RuntimeError):
        scope.work_dir()
    scope.close()  # idempotent


def test_a_file_held_open_does_not_make_closing_the_scope_raise(tmp_path):
    with OperationScope(temp_root=str(tmp_path)) as scope:
        path = os.path.join(scope.work_dir("held"), "locked.bin")
        handle = open(path, "wb")
    handle.close()
    sweep_stale(str(tmp_path))  # our own pid is alive: the root is not swept, but nothing raises
