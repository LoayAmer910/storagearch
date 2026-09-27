"""The real cloud client (local_cloud) wired to the new core, talking to a local stand-in for the
Cloudflare Worker - never the real server. Each test gets its own cloud home (credentials, queues, config)."""
import os
import sys

import pytest

from cloud_support import FakeWorker, wait_for  # noqa: F401 - shared with the GUI tests

from core import api, events  # noqa: E402


@pytest.fixture
def worker():
    w = FakeWorker()
    yield w
    w.stop()


@pytest.fixture
def cloud_app(tmp_path, monkeypatch, worker):
    """A fresh installation with the real cloud client pointed at the local stand-in."""
    assert events.connect(), "local_cloud must be importable"
    from local_cloud import cloud_client, local_state
    monkeypatch.setattr(cloud_client, "BASE_URL", worker.url)
    monkeypatch.setenv("STORAGEARCH_CLOUD_HOME", str(tmp_path / "cloud_home"))
    monkeypatch.setattr(local_state, "_session_id", None, raising=False)
    monkeypatch.chdir(tmp_path)
    events.reset()
    api.initialize()
    yield tmp_path
    events.flush(5)
    events.shutdown(2)
    events.reset()
