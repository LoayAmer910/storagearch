"""Helpers shared by several test files."""
import multiprocessing
import os
import time

from core import procs


def pid_from(marker, wait_s=5):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if os.path.isfile(marker) and open(marker).read().strip():
            return int(open(marker).read())
        time.sleep(0.05)
    raise AssertionError("the fault never ran - the test would prove nothing")


def wait_dead(pid, wait_s=5):
    deadline = time.monotonic() + wait_s
    while procs.alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not procs.alive(pid)


def live_children():
    return [p for p in multiprocessing.active_children() if p.is_alive()]
