"""Feedback queue (plan §17.3): max 1 MB, user-initiated, no automatic
expiration, no automatic oldest-deletion - if full, a new submission is
REJECTED (the caller must tell the user to send/wait rather than silently
losing older unsent feedback, which - unlike telemetry/crash - was an
explicit user action)."""
from .. import local_state
from .base_queue import PersistentQueue

MAX_BYTES = 1 * 1024 * 1024


def open_queue():
    return PersistentQueue(local_state.feedback_queue_path(), max_bytes=MAX_BYTES, max_age_days=None, drop_oldest_when_full=False)
