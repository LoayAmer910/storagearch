"""Helpers for the API-level tests: a fresh data folder per test, and small inspection helpers."""
import multiprocessing
import os

import pytest

from core import api, config, events


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A clean installation in tmp_path (config file, data root, catalog). Yields tmp_path."""
    monkeypatch.chdir(tmp_path)
    events.reset()
    api.initialize()
    yield tmp_path
    events.flush(5)
    events.reset()


def item_id_by_name(name, kind=None):
    for row in api.search(name):
        if row[1] == name and (kind is None or row[2] == kind):
            return row[0]
    raise AssertionError(f"no item named {name!r}")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def tree(root):
    """{relative path: bytes or None for a directory} of everything under root."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for d in dirs:
            out[os.path.relpath(os.path.join(folder, d), root).replace(os.sep, "/")] = None
        for f in files:
            p = os.path.join(folder, f)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = read(p)
    return out


def leftovers():
    """What the operations left behind: files in the app temp folder, live worker processes."""
    temp = config.temp_dir()
    files = [os.path.join(d, f) for d, _s, fs in os.walk(temp) for f in fs] if os.path.isdir(temp) else []
    dirs = os.listdir(temp) if os.path.isdir(temp) else []
    children = [p for p in multiprocessing.active_children() if p.is_alive()]
    return {"temp_entries": dirs, "temp_files": files, "children": children}
