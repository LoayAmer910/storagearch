"""local_cloud.telemetry_service - telemetry is sent only with the user's consent and while the remote safety switch
allows it; turning it off empties the queue; nothing here ever raises into the app."""
import json

from local_cloud import cloud_client, local_state, telemetry_service as ts
from local_cloud.queues import telemetry_queue


class _Sender:
    def __init__(self, fails=False):
        self.woken, self.fails = 0, fails

    def wake(self):
        self.woken += 1
        if self.fails:
            raise RuntimeError("sender is gone")


def _queued():
    return [json.loads(i.payload_json)["event_type"] for i in telemetry_queue.open_queue().list_pending()]


def test_the_remote_safety_switch_can_only_turn_telemetry_off(registered):
    ts.set_consent(True)
    assert ts.is_effectively_enabled()
    with open(local_state.remote_config_lkg_path(), "w", encoding="utf-8") as f:
        json.dump({"config_version": 1, "telemetry_globally_enabled": False, "disabled_smart_strategies": []}, f)
    assert ts.is_consent_on() and not ts.is_globally_enabled() and not ts.is_effectively_enabled()
    ts.record_app_started()
    assert _queued() == []


def test_turning_telemetry_on_wakes_the_sender_and_a_failing_sender_is_ignored(registered):
    sender = _Sender(fails=True)
    ts.set_consent(True, sender=sender)
    assert sender.woken == 1 and ts.is_consent_on()
    ts.set_consent(True, sender=sender)  # already on: nothing happens
    assert sender.woken == 1


def test_turning_telemetry_off_empties_the_queue_and_tells_the_server(registered):
    ts.set_consent(True)
    ts.record_app_started()
    assert _queued() == ["app_started"]
    ts.set_consent(False)
    assert _queued() == []
    opted_out = registered.posts("/api/v1/telemetry")
    assert [r["body"]["event_type"] for r in opted_out] == ["telemetry_opted_out"]


def test_an_opt_out_that_cannot_be_sent_still_turns_telemetry_off(registered, monkeypatch):
    ts.set_consent(True)

    def offline(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(cloud_client, "post_signed", offline)
    ts.set_consent(False)
    assert not ts.is_consent_on()


def test_app_started_is_queued_with_only_coarse_system_facts(registered):
    ts.set_consent(True)
    sender = _Sender(fails=True)
    ts.record_app_started(sender=sender)
    (item,) = telemetry_queue.open_queue().list_pending()
    payload = json.loads(item.payload_json)
    assert payload["event_type"] == "app_started" and set(payload) >= {"windows_version", "architecture", "ram_bucket"}
    assert sender.woken == 1


def test_without_a_registered_installation_nothing_is_queued(monkeypatch):
    monkeypatch.setattr(ts, "is_effectively_enabled", lambda: True)
    ts.record_app_started()
    ts.record_operation_event("archive_completed", "archive", "success")
    assert _queued() == []


def test_a_queue_that_cannot_be_written_never_reaches_the_caller(registered, monkeypatch):
    ts.set_consent(True)

    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(telemetry_queue, "open_queue", broken)
    sender = _Sender()
    ts.record_app_started(sender=sender)  # must not raise
    assert sender.woken == 0
