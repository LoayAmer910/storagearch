"""Runtime queue age-based retention (telemetry/crash 30-day
max age; feedback has no age limit by design). `PersistentQueue.prune_expired()`
already encodes the actual rule per queue (it's a no-op when a queue's
`max_age_days` is None, e.g. feedback) - this module's only job is to
actually call it from a real running app, once per launch, off the
startup path.
"""
from .queues import crash_queue, feedback_queue, telemetry_queue

_QUEUE_MODULES = (telemetry_queue, crash_queue, feedback_queue)


def prune_expired_queues():
    """Deletes only rows older than each queue's own `max_age_days` (a
    no-op for feedback, which has none) - never touches size-bounding,
    which `enqueue()` already enforces independently and continues to.
    Best-effort per queue: one queue's failure (e.g. a concurrent
    corruption-recovery cycle) must never stop the others from being
    pruned, and must never raise into the caller."""
    for module in _QUEUE_MODULES:
        try:
            module.open_queue().prune_expired()
        except Exception:  # noqa: BLE001 - retention is best-effort, never fatal
            pass
