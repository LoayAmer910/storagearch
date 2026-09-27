"""Shared persistent-queue mechanics (plan §17) - one SQLite file per queue
type (telemetry_queue.db / feedback_queue.db / crash_queue.db), completely
separate from the StorageArch business database.

Every payload is stored as JSON text exactly as it will be sent - already
built by schemas.py's allowlist-only functions before it ever reaches here,
so this module never inspects payload content, only manages bytes/rows.

Corruption handling: if the queue DB file can't be opened as valid SQLite,
it is isolated (renamed aside) and a fresh one created - never blocks
startup, never raises into a caller performing a real local operation
(plan §17.1's corruption-handling requirement, applied uniformly to all
three queue types since the plan asks for it on telemetry/crash and the
same fail-safe logic is equally correct for feedback).
"""
import os
import sqlite3
import time
from datetime import datetime, timezone


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


class QueueItem:
    __slots__ = ("id", "item_key", "payload_json", "enqueued_at_utc", "attempts")

    def __init__(self, id, item_key, payload_json, enqueued_at_utc, attempts):
        self.id = id
        self.item_key = item_key
        self.payload_json = payload_json
        self.enqueued_at_utc = enqueued_at_utc
        self.attempts = attempts


class PersistentQueue:
    def __init__(self, db_path, max_bytes, max_age_days=None, drop_oldest_when_full=True):
        self.db_path = db_path
        self.max_bytes = max_bytes
        self.max_age_days = max_age_days
        self.drop_oldest_when_full = drop_oldest_when_full
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def _ensure_schema(self):
        try:
            conn = self._connect()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS queue_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        enqueued_at_utc TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at_utc TEXT
                    )"""
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            self._recover_from_corruption()

    def _recover_from_corruption(self):
        # Isolate the bad file rather than deleting it outright - keeps a
        # forensic trace without risking losing something recoverable, and
        # never delays startup on cleanup. A safe local-only marker, no
        # Cloud call, no user-facing content.
        if os.path.isfile(self.db_path):
            corrupt_path = f"{self.db_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.db_path, corrupt_path)
            except OSError:
                try:
                    os.remove(self.db_path)
                except OSError:
                    pass
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS queue_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    enqueued_at_utc TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at_utc TEXT
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def _file_size(self):
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    def enqueue(self, item_key, payload_json):
        """Returns True if the item was queued, False if rejected (feedback-
        queue-full case, plan §17.3: "reject a new queued Feedback entry").
        For drop_oldest_when_full queues (telemetry/crash), the new item is
        always accepted - old items are dropped instead, never the new one,
        matching plan §17.1/§17.2's FIFO oldest-drop rule."""
        try:
            conn = self._connect()
        except sqlite3.DatabaseError:
            self._recover_from_corruption()
            conn = self._connect()

        try:
            if not self.drop_oldest_when_full and self._file_size() >= self.max_bytes:
                return False

            conn.execute(
                "INSERT INTO queue_items (item_key, payload_json, enqueued_at_utc, attempts) VALUES (?, ?, ?, 0)",
                (item_key, payload_json, _utc_now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

        if self.drop_oldest_when_full:
            self._enforce_size_bound()
        return True

    def _enforce_size_bound(self):
        if self._file_size() <= self.max_bytes:
            return
        conn = self._connect()
        try:
            # Oldest-first drop, one row at a time, until back under budget
            # or the queue is empty (bounded loop either way - never spins).
            for _ in range(100_000):
                if self._file_size() <= self.max_bytes:
                    break
                row = conn.execute("SELECT id FROM queue_items ORDER BY id ASC LIMIT 1").fetchone()
                if row is None:
                    break
                conn.execute("DELETE FROM queue_items WHERE id = ?", (row[0],))
                conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()

    def prune_expired(self):
        if self.max_age_days is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - _timedelta_days(self.max_age_days)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM queue_items WHERE enqueued_at_utc < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def list_pending(self, limit=50):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, item_key, payload_json, enqueued_at_utc, attempts FROM queue_items ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [QueueItem(*row) for row in rows]
        finally:
            conn.close()

    def count(self):
        conn = self._connect()
        try:
            return conn.execute("SELECT count(*) FROM queue_items").fetchone()[0]
        finally:
            conn.close()

    def remove(self, item_id):
        conn = self._connect()
        try:
            conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))
            conn.commit()
        finally:
            conn.close()

    def record_attempt(self, item_id):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE queue_items SET attempts = attempts + 1, last_attempt_at_utc = ? WHERE id = ?",
                (_utc_now_iso(), item_id),
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self):
        """Deletes the queue file entirely (plan §17.1: "delete queue
        immediately when Telemetry is turned OFF")."""
        if os.path.isfile(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass
        self._ensure_schema()


def _timedelta_days(days):
    from datetime import timedelta
    return timedelta(days=days)
