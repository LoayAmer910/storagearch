"""Telemetry queue: max 5 MB, FIFO, oldest-drop when full,
max local age 30 days, deleted immediately when Telemetry is turned OFF."""
import threading

from .. import local_state
from .base_queue import PersistentQueue

MAX_BYTES = 5 * 1024 * 1024
MAX_AGE_DAYS = 30

# Held by the sender around each telemetry send (with a consent check inside), and by the opt-out around
# turning consent off and clearing the queue - so an event already read from the queue can never be sent
# after the user opted out.
SEND_LOCK = threading.Lock()


def consent_given():
    return bool(local_state.load_config().get("telemetry_enabled", False))


def open_queue():
    return PersistentQueue(local_state.telemetry_queue_path(), max_bytes=MAX_BYTES, max_age_days=MAX_AGE_DAYS, drop_oldest_when_full=True)
