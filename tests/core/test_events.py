"""core.events - the only bridge to the optional cloud client: without the package every call is a harmless no-op
with the "unavailable" answer, and nothing on the cloud side (a full queue, a failing sender) can block or break
the caller."""
import sys
import threading

import pytest

from core import events


@pytest.fixture(autouse=True)
def fresh_lane():
    events.reset()
    yield
    events.reset()


@pytest.fixture
def no_cloud(monkeypatch):
    """A build without the cloud package: not connected, and importing it fails."""
    monkeypatch.setattr(events, "CLOUD_AVAILABLE", False)
    monkeypatch.setitem(sys.modules, "local_cloud", None)


def test_without_the_cloud_package_connect_says_unavailable(no_cloud):
    assert events.connect() is False
    assert events.cloud_available() is False


def test_without_the_cloud_every_call_returns_the_unavailable_answer(no_cloud):
    assert events.cloud_is_registered() is False
    assert events.cloud_register("code") == (False, "cloud_unavailable")
    assert events.cloud_submit_feedback("bug") == (False, "cloud_unavailable")
    assert events.cloud_disconnect() is False
    assert events.cloud_telemetry_enabled() is False
    assert events.cloud_check_for_updates() is None
    assert events.disabled_strategies() == frozenset()
    events.report_operation({"operation": "archive"})  # nothing queued, nothing raised
    events.start()
    events.cloud_set_telemetry_enabled(True)
    events.cloud_report_crash(RuntimeError("x"))
    assert events._get_sender() is None


def test_a_full_queue_drops_events_instead_of_blocking_the_caller(monkeypatch):
    monkeypatch.setattr(events, "_QUEUE_LIMIT", 1)
    events.reset()
    release = threading.Event()
    events._submit(release.wait)       # the lane is busy with this one
    events._submit(lambda: None)       # fills the queue
    ran = []
    events._submit(lambda: ran.append(1))  # no room: dropped at once, the caller is not blocked
    events.reset()                     # the old lane is full too; reset must still not block
    release.set()
    assert events.flush(5)
    assert ran == []


class _FailingSender:
    def shutdown(self, timeout=None):
        raise RuntimeError("sender broke")


def test_a_failing_sender_does_not_break_reset_or_shutdown(monkeypatch):
    monkeypatch.setattr(events, "_sender", _FailingSender())
    events.reset()
    assert events._sender is None
    monkeypatch.setattr(events, "_sender", _FailingSender())
    events.shutdown(timeout=0.5)
    assert events._sender is None


def test_feedback_with_diagnostics_sends_only_the_windows_version_and_architecture(monkeypatch):
    from local_cloud import feedback_service, system_info
    sent = {}
    monkeypatch.setattr(system_info, "windows_version", lambda: "10.0.26100")
    monkeypatch.setattr(system_info, "architecture", lambda: "AMD64")
    monkeypatch.setattr(feedback_service, "submit_feedback", lambda *a, **k: sent.update(k) or (True, "ok"))
    assert events.cloud_submit_feedback("bug", rating=4, include_diagnostics=True) == (True, "ok")
    assert sent["diagnostic_snapshot"] == {"windows_version": "10.0.26100", "architecture": "AMD64"}


def test_feedback_is_still_sent_when_the_diagnostics_cannot_be_read(monkeypatch):
    from local_cloud import feedback_service, system_info
    sent = {}

    def broken():
        raise OSError("registry not readable")

    monkeypatch.setattr(system_info, "windows_version", broken)
    monkeypatch.setattr(feedback_service, "submit_feedback", lambda *a, **k: sent.update(k) or (True, "ok"))
    assert events.cloud_submit_feedback("bug", include_diagnostics=True) == (True, "ok")
    assert sent["diagnostic_snapshot"] is None
