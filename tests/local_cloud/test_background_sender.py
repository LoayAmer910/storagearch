"""local_cloud.background_sender - one request at a time, crash before feedback before telemetry; a 401 pauses until
reconnect; temporary failures are retried later; a payload the server will never accept is dropped, not retried forever."""
import json

from local_cloud import cloud_client, installation_service, telemetry_service
from local_cloud.background_sender import BackgroundSender
from local_cloud.cloud_client import CloudResult
from local_cloud.queues import crash_queue, feedback_queue, telemetry_queue
from support.cloud import wait_for


def _sender():
    return BackgroundSender(installation_service.get_credentials)


def test_queued_items_are_sent_crash_first_then_feedback_then_telemetry(registered):
    telemetry_service.set_consent(True)
    telemetry_queue.open_queue().enqueue("t", json.dumps({"event_type": "app_started"}))
    feedback_queue.open_queue().enqueue("f", json.dumps({"auth_mode": "installation_token"}))
    crash_queue.open_queue().enqueue("c", json.dumps({"crash_event_id": "c"}))
    _sender()._process_all()
    assert [r["path"] for r in registered.requests if r["method"] == "POST"][-3:] == [
        "/api/v1/crashes", "/api/v1/feedback", "/api/v1/telemetry"]
    assert sum(m.open_queue().count() for m in (crash_queue, feedback_queue, telemetry_queue)) == 0


def test_beta_code_feedback_is_sent_unsigned_even_without_an_installation(server):
    feedback_queue.open_queue().enqueue("f", json.dumps({"auth_mode": "beta_code", "beta_code": "B"}))
    crash_queue.open_queue().enqueue("c", json.dumps({"crash_event_id": "c"}))  # needs an installation: waits
    _sender()._process_all()
    (sent,) = server.posts("/api/v1/feedback")
    assert not any(h.startswith("X-StorageArch") for h in sent["headers"])
    assert crash_queue.open_queue().count() == 1


def test_an_unreadable_queued_row_is_dropped_without_a_request(registered):
    crash_queue.open_queue().enqueue("bad", "{ not json")
    _sender()._process_all()
    assert crash_queue.open_queue().count() == 0 and registered.posts("/api/v1/crashes") == []


def test_an_unauthorized_answer_pauses_sending_until_reconnect(registered):
    crash_queue.open_queue().enqueue("c", json.dumps({"crash_event_id": "c"}))
    sender = _sender()
    registered.mode = "unauthorized"
    sender._process_all()
    assert sender.is_paused() and crash_queue.open_queue().count() == 1
    registered.mode = "ok"
    sender._process_all()  # still paused: nothing is sent
    assert len(registered.posts("/api/v1/crashes")) == 1
    sender.reconnect()
    sender._process_all()
    assert not sender.is_paused() and crash_queue.open_queue().count() == 0


def test_a_temporary_failure_keeps_the_item_for_later(registered):
    crash_queue.open_queue().enqueue("c", json.dumps({"crash_event_id": "c"}))
    registered.mode = "error500"
    _sender()._process_all()
    assert crash_queue.open_queue().count() == 1


def test_a_payload_the_server_refuses_for_good_is_dropped(registered, monkeypatch):
    crash_queue.open_queue().enqueue("c", json.dumps({"crash_event_id": "c"}))
    monkeypatch.setattr(cloud_client, "post_signed", lambda *a, **k: (CloudResult(400, {"error": "bad_payload"}), "r"))
    _sender()._process_all()
    assert crash_queue.open_queue().count() == 0


def test_telemetry_queued_before_an_opt_out_is_never_sent(registered):
    telemetry_queue.open_queue().enqueue("t", json.dumps({"event_type": "app_started"}))  # consent is off
    _sender()._process_all()
    assert telemetry_queue.open_queue().count() == 0 and registered.posts("/api/v1/telemetry") == []


def test_the_thread_survives_a_failing_round_and_stops_on_shutdown(server, monkeypatch):
    sender = _sender()
    rounds = []

    def failing_round():
        rounds.append(1)
        raise RuntimeError("one bad round")

    monkeypatch.setattr(sender, "_process_all", failing_round)
    sender.start()
    first_thread = sender._thread
    sender.start()  # already running: no second thread
    assert sender._thread is first_thread
    assert wait_for(lambda: len(rounds) >= 1)
    sender.wake()
    assert wait_for(lambda: len(rounds) >= 2), "the loop must keep going after a failing round"
    sender.shutdown(timeout=2)
    assert not first_thread.is_alive()
