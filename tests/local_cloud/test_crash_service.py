"""local_cloud.crash_service - a crash is queued only with consent and a registered installation, carries only the
exception's class name and safe labels (never its message), and reporting can never crash the app."""
import json

from local_cloud import crash_service, telemetry_service
from local_cloud.queues import crash_queue


class _Sender:
    def __init__(self, fails=False):
        self.woken, self.fails = 0, fails

    def wake(self):
        self.woken += 1
        if self.fails:
            raise RuntimeError("sender is gone")


def _queued():
    return [json.loads(i.payload_json) for i in crash_queue.open_queue().list_pending()]


def test_without_consent_nothing_is_queued(registered):
    crash_service.report_crash(ValueError("x"), component="items")
    assert _queued() == []


def test_without_a_registered_installation_nothing_is_queued(monkeypatch):
    monkeypatch.setattr(telemetry_service, "is_effectively_enabled", lambda: True)
    crash_service.report_crash(ValueError("x"))
    assert _queued() == []


def test_a_crash_carries_only_its_class_name_and_safe_labels(registered):
    telemetry_service.set_consent(True)
    sender = _Sender()
    crash_service.report_crash(FileNotFoundError(r"C:\Users\someone\secret.txt"), component=None, function_name="x" * 200,
                               sender=sender)
    (payload,) = _queued()
    assert payload["exception_type"] == "FileNotFoundError"
    assert payload["grouping_signature"] == "FileNotFoundError:unknown:" + "x" * crash_service.MAX_SIGNATURE_PART_LENGTH
    assert "secret.txt" not in json.dumps(payload)
    assert sender.woken == 1


def test_reporting_never_raises_even_when_queueing_or_waking_fails(registered, monkeypatch):
    telemetry_service.set_consent(True)
    crash_service.report_crash(RuntimeError("x"), sender=_Sender(fails=True))  # a failing wake is ignored
    assert len(_queued()) == 1

    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(crash_queue, "open_queue", broken)
    crash_service.report_crash(RuntimeError("y"), sender=_Sender())  # a failing queue is ignored
