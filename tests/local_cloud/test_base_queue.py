"""local_cloud.queues.base_queue - the on-disk queue: first in, first out; bounded in size; old rows expire; a damaged
file is set aside, never a reason to fail."""
import sqlite3

from local_cloud.queues.base_queue import PersistentQueue


def _queue(tmp_path, **kwargs):
    return PersistentQueue(str(tmp_path / "q.db"), **{"max_bytes": 1024 * 1024, **kwargs})


def test_items_come_out_in_the_order_they_went_in_and_can_be_removed(tmp_path):
    q = _queue(tmp_path)
    for key in ("a", "b", "c"):
        assert q.enqueue(key, f'{{"k": "{key}"}}') is True
    assert [i.item_key for i in q.list_pending()] == ["a", "b", "c"] and q.count() == 3
    first = q.list_pending(limit=1)[0]
    q.record_attempt(first.id)
    q.record_attempt(first.id)
    assert q.list_pending(limit=1)[0].attempts == 2
    q.remove(first.id)
    assert [i.item_key for i in q.list_pending()] == ["b", "c"]


def test_a_full_queue_that_drops_oldest_keeps_the_newest(tmp_path):
    q = _queue(tmp_path, max_bytes=40_000, drop_oldest_when_full=True)
    for n in range(60):
        assert q.enqueue(f"k{n}", "x" * 2000)
    keys = [i.item_key for i in q.list_pending(limit=1000)]
    assert keys[-1] == "k59" and "k0" not in keys
    assert q._file_size() <= 40_000


def test_a_full_queue_that_keeps_what_it_has_refuses_the_new_item(tmp_path):
    q = _queue(tmp_path, max_bytes=1, drop_oldest_when_full=False)  # the empty file is already over the limit
    assert q.enqueue("new", "{}") is False and q.count() == 0


def test_only_rows_older_than_the_age_limit_expire(tmp_path):
    q = _queue(tmp_path, max_age_days=30)
    q.enqueue("fresh", "{}")
    conn = sqlite3.connect(q.db_path)
    conn.execute("INSERT INTO queue_items (item_key, payload_json, enqueued_at_utc) VALUES ('old', '{}', '2020-01-01T00:00:00.000Z')")
    conn.commit()
    conn.close()
    assert q.prune_expired() == 1
    assert [i.item_key for i in q.list_pending()] == ["fresh"]


def test_a_queue_without_an_age_limit_never_expires_anything(tmp_path):
    q = _queue(tmp_path, max_age_days=None)
    q.enqueue("kept", "{}")
    assert q.prune_expired() == 0 and q.count() == 1


def test_a_damaged_queue_file_is_set_aside_and_a_new_one_works(tmp_path):
    path = tmp_path / "q.db"
    path.write_bytes(b"this is not a database at all" * 100)
    q = PersistentQueue(str(path), max_bytes=1024 * 1024)
    assert q.enqueue("after", "{}") and q.count() == 1
    assert len(list(tmp_path.glob("q.db.corrupt.*"))) == 1


def test_clearing_deletes_everything_and_leaves_a_usable_queue(tmp_path):
    q = _queue(tmp_path)
    q.enqueue("a", "{}")
    q.clear()
    assert q.count() == 0 and q.enqueue("b", "{}") and q.count() == 1


def test_the_size_of_a_missing_file_is_zero(tmp_path):
    q = _queue(tmp_path)
    q.db_path = str(tmp_path / "gone.db")
    assert q._file_size() == 0


def test_a_queue_too_small_for_even_one_item_ends_empty_instead_of_looping(tmp_path):
    q = _queue(tmp_path, max_bytes=1, drop_oldest_when_full=True)
    assert q.enqueue("only", "{}") is True  # accepted, then dropped: the size bound always wins
    assert q.count() == 0


def test_clearing_a_queue_file_held_by_another_connection_does_not_fail(tmp_path):
    q = _queue(tmp_path)
    q.enqueue("a", "{}")
    holder = open(q.db_path, "rb")  # Windows refuses to delete a file that is open
    try:
        q.clear()  # must not raise
    finally:
        holder.close()
    assert q.count() == 1, "a file that could not be deleted keeps its rows"


def test_a_queue_nothing_was_put_into_leaves_no_file(tmp_path):
    q = _queue(tmp_path, max_age_days=30)
    assert q.count() == 0 and q.list_pending() == [] and q.prune_expired() == 0
    q.remove(1)
    q.record_attempt(1)
    q.clear()
    assert list(tmp_path.iterdir()) == []
    assert q.enqueue("first", "{}") and q.count() == 1 and (tmp_path / "q.db").is_file()
    q.clear()
    assert list(tmp_path.iterdir()) == [] and q.count() == 0  # emptied: the file goes too
