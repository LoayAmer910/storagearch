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


def test_kill_tree_runs_the_system_tool_and_not_a_file_planted_under_its_name(tmp_path, monkeypatch):
    import shutil

    system32 = os.path.join(os.environ["SystemRoot"], "System32")
    shutil.copy(os.path.join(system32, "whoami.exe"), tmp_path / "taskkill.exe")  # a harmless stand-in for a planted file
    monkeypatch.chdir(tmp_path)
    commands, real = [], subprocess.run
    monkeypatch.setattr(procs.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or real(cmd, **kw))
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        procs.kill_tree(victim.pid)
        assert wait_dead(victim.pid), "the planted file ran instead of the system tool"
    finally:
        victim.kill()
    tool = commands[0][0]
    assert os.path.isfile(tool) and os.path.normcase(os.path.dirname(tool)) == os.path.normcase(system32)
