"""local_cloud.retention - at startup, telemetry and crash rows older than 30 days are removed, feedback is never aged
out, and one broken queue never stops the others."""
import sqlite3

from local_cloud import retention
from local_cloud.queues import crash_queue, feedback_queue, telemetry_queue

OLD = "2020-01-01T00:00:00.000Z"


def _add_old_row(module):
    q = module.open_queue()
    conn = sqlite3.connect(q.db_path)
    conn.execute("INSERT INTO queue_items (item_key, payload_json, enqueued_at_utc) VALUES ('old', '{}', ?)", (OLD,))
    conn.commit()
    conn.close()
    return q


def test_old_telemetry_and_crash_rows_go_and_old_feedback_stays():
    queues = {m: _add_old_row(m) for m in (telemetry_queue, crash_queue, feedback_queue)}
    retention.prune_expired_queues()
    assert queues[telemetry_queue].count() == 0
    assert queues[crash_queue].count() == 0
    assert queues[feedback_queue].count() == 1


def test_a_broken_queue_does_not_stop_the_others(monkeypatch):
    crash = _add_old_row(crash_queue)

    def broken():
        raise OSError("locked")

    monkeypatch.setattr(telemetry_queue, "open_queue", broken)
    retention.prune_expired_queues()  # must not raise
    assert crash.count() == 0
