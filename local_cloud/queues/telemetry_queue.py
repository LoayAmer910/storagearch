"""Telemetry queue (plan §17.1): max 5 MB, FIFO, oldest-drop when full,
max local age 30 days, deleted immediately when Telemetry is turned OFF."""
from .. import local_state
from .base_queue import PersistentQueue

MAX_BYTES = 5 * 1024 * 1024
MAX_AGE_DAYS = 30


def open_queue():
    return PersistentQueue(local_state.telemetry_queue_path(), max_bytes=MAX_BYTES, max_age_days=MAX_AGE_DAYS, drop_oldest_when_full=True)
