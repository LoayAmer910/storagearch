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


def test_a_worker_that_does_not_start_in_time_is_reported_and_not_kept(pool, src, tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "READY_TIMEOUT_S", -1)  # the deadline has passed before the worker is ready
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


def test_closing_the_pool_while_a_task_runs_kills_the_busy_worker(pool, src, tmp_path):
    marker = str(tmp_path / "running")
    outcome = {}

    def run():
        try:
            outcome["result"] = pool.run(fakes.Hanging(name="h", marker=marker), src, str(tmp_path), "sha", 60)
        except Exception as exc:  # noqa: BLE001 - recorded so the test can show it
            outcome["raised"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    busy_pid = pid_from(marker)
    pool.close()
    thread.join(30)
    assert wait_dead(busy_pid)
    assert "raised" not in outcome, f"run() raised instead of reporting: {outcome.get('raised')!r}"
    assert outcome["result"]["status"] == candidate.CRASHED



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
