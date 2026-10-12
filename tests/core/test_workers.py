"""core.workers - the worker pool: timeouts kill the whole process tree, the pool keeps working, and every way a
worker can fail to start, take a task or finish one is reported as a result, never an exception."""
import multiprocessing
import os
import threading

import pytest

from core import candidate, procs, workers
from core.executor import Executor
from core.scope import OperationScope
from support import fakes
from support.execution import live_children, pid_from, wait_dead


def test_a_timeout_kills_the_worker_and_everything_it_started(src, info, tmp_path):
    marker = str(tmp_path / "grandchild")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        outcome = Executor(scope).run(fakes.HangingWithChild(name="f_child", marker=marker), src, info)
        grandchild = pid_from(marker)
        assert outcome.status == candidate.TIMEOUT
        assert wait_dead(grandchild), f"the process the worker started ({grandchild}) survived the kill"


def test_the_pool_keeps_working_after_timeouts_and_crashes(src, info, tmp_path):
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope, pool_size=1)
        sequence = [fakes.Hanging(name="h"), fakes.Fake(), fakes.Crashing(name="c"), fakes.Fake(), fakes.Slow(fakes.TIMEOUT_S + 1, name="s"), fakes.Fake()]
        statuses = [ex.run(s, src, info).status for s in sequence]
    assert statuses == [candidate.TIMEOUT, candidate.VERIFIED, candidate.CRASHED, candidate.VERIFIED,
                        candidate.TIMEOUT, candidate.VERIFIED]


# --- the pool itself, without the executor around it ----------------------------------------------------

@pytest.fixture
def pool():
    p = workers.WorkerPool(size=1)
    yield p
    p.close()


def test_a_closed_pool_takes_no_more_work(pool, src, tmp_path):
    pool.close()
    result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result == {"status": candidate.CRASHED, "detail": "no worker process could be started"}


def _workers_start_with(monkeypatch, entry_point):
    """Every new worker process runs `entry_point` instead of the real worker loop (it must be importable)."""
    real = workers._CTX.Process
    monkeypatch.setattr(workers._CTX, "Process",
                        lambda target, args, daemon: real(target=entry_point, args=args, daemon=daemon))


def test_a_worker_that_dies_while_starting_is_reported_as_crashed(pool, src, tmp_path, monkeypatch):
    # e.g. its imports fail, or an antivirus stops the new process: its pipe closes before it says it is ready
    _workers_start_with(monkeypatch, fakes.worker_that_dies_at_startup)
    result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result == {"status": candidate.CRASHED, "detail": "no worker process could be started"}
    assert pool.live_pids() == set() and live_children() == []


def test_a_worker_that_does_not_start_in_time_is_killed_and_reported(pool, src, tmp_path, monkeypatch):
    _workers_start_with(monkeypatch, fakes.worker_that_never_says_ready)
    monkeypatch.setattr(workers, "READY_TIMEOUT_S", 1.0)
    result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result["status"] == candidate.CRASHED
    assert pool.live_pids() == set() and live_children() == []


def test_a_task_that_cannot_be_handed_over_is_an_error_and_the_worker_is_dropped(pool, src, tmp_path):
    strategy = fakes.Fake()
    strategy.unpicklable = lambda: None  # a strategy holding a lambda cannot be sent to another process
    result = pool.run(strategy, src, str(tmp_path), "sha", 5)
    assert result["status"] == candidate.ERROR
    assert result["detail"].startswith("could not hand the task to a worker:")
    assert pool.live_pids() == set()


def test_a_worker_that_finishes_starting_after_the_pool_closed_is_stopped(pool, src, tmp_path, monkeypatch):
    started = []

    def close_then_start():
        pool.close()  # the user closes while the worker is still starting
        started.append(real_worker())
        return started[-1]

    real_worker = workers._Worker
    monkeypatch.setattr(workers, "_Worker", close_then_start)
    result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result["status"] == candidate.CRASHED
    assert wait_dead(started[0].pid) and pool.live_pids() == set()


def test_closing_the_pool_kills_a_worker_that_is_still_busy(pool, src, tmp_path):
    # The app closes the pool only after every task has returned; a worker busy at that point (a task nobody
    # waits for any more) does not answer the polite stop and is killed with everything it started.
    worker = pool._take()
    marker = str(tmp_path / "running")
    worker.conn.send((1, "candidate", (fakes.Hanging(name="h", marker=marker), src, str(tmp_path), "sha", 60)))
    busy_pid = pid_from(marker)
    pool.close()
    assert wait_dead(busy_pid) and wait_dead(worker.pid)
    assert pool.live_pids() == set()


def test_closing_after_a_worker_was_killed_from_outside_does_not_fail(pool, src, tmp_path):
    assert pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)["status"] != candidate.CRASHED
    (pid,) = pool.live_pids()
    procs.kill_tree(pid)  # e.g. ended in Task Manager while idle
    assert wait_dead(pid)
    pool.close()
    assert pool.live_pids() == set()


# --- the loop inside a worker process (run here in a thread, with a real pipe) ---------------------------

def test_a_worker_whose_parent_is_gone_exits():
    parent, child = multiprocessing.Pipe()
    thread = threading.Thread(target=workers._worker_main, args=(child,))
    thread.start()
    assert parent.recv() == ("ready", os.getpid())
    parent.close()
    thread.join(5)
    assert not thread.is_alive()


PARENT_GONE = threading.Event()


class _FinishesAfterTheParentLeft(fakes.Fake):
    def restore(self, stored_path, work_dir, time_limit_s=None):
        PARENT_GONE.wait(5)
        return super().restore(stored_path, work_dir, time_limit_s)


def test_a_worker_whose_parent_leaves_mid_task_exits_without_an_error(src, tmp_path):
    PARENT_GONE.clear()
    parent, child = multiprocessing.Pipe()
    thread = threading.Thread(target=workers._worker_main, args=(child,))
    thread.start()
    parent.recv()
    parent.send((1, "restore", (_FinishesAfterTheParentLeft(), src, str(tmp_path), 5)))
    parent.close()
    PARENT_GONE.set()
    thread.join(5)
    assert not thread.is_alive()


class _PipeOfADeadProcess:
    """A worker's pipe as Windows shows it when the process died just before it was asked for an answer:
    the question itself fails (WinError 109), it is not answered with 'nothing yet'."""

    def __init__(self, conn):
        self._conn = conn

    def poll(self, timeout=0.0):
        raise BrokenPipeError(109, "The pipe has been ended")

    def send(self, message):
        self._conn.send(message)

    def recv(self):
        raise EOFError  # reading a pipe whose process is gone

    def close(self):
        self._conn.close()


def test_a_worker_that_died_just_before_it_was_asked_is_reported_as_crashed(pool, src, tmp_path, monkeypatch):
    take = pool._take

    def take_one_that_dies():
        worker = take()
        worker.conn = _PipeOfADeadProcess(worker.conn)
        return worker
    with monkeypatch.context() as dying:
        dying.setattr(pool, "_take", take_one_that_dies)
        result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result == {"status": candidate.CRASHED, "detail": "worker process died"}
    assert pool.live_pids() == set() and live_children() == []
    os.makedirs(tmp_path / "next")
    assert pool.run(fakes.Fake(), src, str(tmp_path / "next"), "sha", 5)["status"] != candidate.CRASHED  # the pool goes on


def test_a_worker_whose_pipe_breaks_while_it_starts_is_reported_as_crashed(pool, src, tmp_path, monkeypatch):
    real = workers._CTX.Pipe

    def broken_pipe():
        parent, child = real()
        return _PipeOfADeadProcess(parent), child
    monkeypatch.setattr(workers._CTX, "Pipe", broken_pipe)
    result = pool.run(fakes.Fake(), src, str(tmp_path), "sha", 5)
    assert result == {"status": candidate.CRASHED, "detail": "no worker process could be started"}
    assert pool.live_pids() == set() and live_children() == []
