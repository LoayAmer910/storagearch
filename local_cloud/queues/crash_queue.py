"""Crash queue (plan §17.2): max 1 MB, FIFO, oldest-drop when full,
30-day retention. Sanitized content only - crash_service.py is the only
writer, and it never passes raw exception text/paths/args into a payload
(see schemas.build_crash_report - there is no field for them)."""
from .. import local_state
from .base_queue import PersistentQueue

MAX_BYTES = 1 * 1024 * 1024
MAX_AGE_DAYS = 30


def open_queue():
    return PersistentQueue(local_state.crash_queue_path(), max_bytes=MAX_BYTES, max_age_days=MAX_AGE_DAYS, drop_oldest_when_full=True)
