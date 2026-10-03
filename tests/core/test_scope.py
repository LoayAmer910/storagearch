"""core.scope - an operation's scope: cancelling, cleaning up, refusing work once closed."""
import concurrent.futures
import os
import subprocess
import sys
import time

import pytest

from core import candidate, procs
from core.executor import Executor
from core.scope import ROOT_PREFIX, OperationScope, sweep_stale
from support import fakes
from support.execution import live_children, pid_from, wait_dead


def test_cancel_stops_a_running_candidate_quickly(src, info, tmp_path):
    marker = str(tmp_path / "ran")
    with OperationScope(temp_root=str(tmp_path / "t")) as scope:
        ex = Executor(scope)
        slow = fakes.Hanging(name="h", marker=marker, timeout_s=60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ex.run, slow, src, info)
            pid = pid_from(marker, wait_s=60)
            start = time.monotonic()
            scope.cancel()
            outcome = future.result(timeout=30)
        assert outcome.status == candidate.CANCELLED and time.monotonic() - start < 5
        assert wait_dead(pid)
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
    assert pids and all(wait_dead(p) for p in pids)
    assert live_children() == []


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


def test_sweeping_a_temp_folder_that_does_not_exist_removes_nothing(tmp_path):
    assert sweep_stale(str(tmp_path / "missing")) == []


def test_every_cleanup_runs_in_reverse_order_even_when_one_fails(tmp_path):
    ran = []

    def failing():
        ran.append("failing")
        raise OSError("first problem")

    scope = OperationScope(temp_root=str(tmp_path))
    with pytest.raises(OSError, match="first problem"):
        with scope:
            root = scope.root
            scope.on_close(lambda: ran.append("registered first"))
            scope.on_close(failing)
            scope.on_close(lambda: ran.append("registered last"))
    assert ran == ["registered last", "failing", "registered first"]
    assert not os.path.exists(root), "the scope's folder must be removed even when a cleanup failed"
