"""core.procs - is a process alive, and killing a process together with every process it started."""
import os
import subprocess
import sys

from core import procs
from support.execution import pid_from, wait_dead


def test_alive_is_false_for_no_process_and_true_for_this_one():
    for not_a_pid in (None, 0, -1):
        assert procs.alive(not_a_pid) is False
    assert procs.alive(os.getpid()) is True


def test_kill_tree_kills_a_process_and_the_process_it_started(tmp_path):
    marker = tmp_path / "grandchild.pid"
    # The parent starts a grandchild that writes its pid and sleeps; both must die with one kill_tree call.
    grandchild = f"import os, time; open(r'{marker}', 'w').write(str(os.getpid())); time.sleep(60)"
    parent = subprocess.Popen([sys.executable, "-c",
                               f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
                               "time.sleep(60)"])
    try:
        grandchild_pid = pid_from(str(marker))
        procs.kill_tree(parent.pid)
        assert wait_dead(parent.pid) and wait_dead(grandchild_pid)
    finally:
        parent.kill()


def test_kill_tree_of_a_finished_process_does_nothing():
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    procs.kill_tree(int(done.stdout))  # must not raise
    assert not procs.alive(int(done.stdout))
