"""Crash reporting. Local sanitizer FIRST: this module never
looks at `str(exception)`, `exception.args`, or a traceback object anywhere
- only `type(exception).__name__` (a fixed, safe class name like
"FileNotFoundError") and whatever safe, pre-sanitized strings the caller
explicitly passes for component/function_name/safe_code_location. A raw
exception message can legitimately contain a file path (e.g.
FileNotFoundError's default message) - that's exactly why this module
never touches it.

Only ever queues anything when Telemetry is ON - the caller
(the global exception hook) is expected to check
telemetry_service.is_effectively_enabled() before calling this, but this
function re-checks it directly too, so a crash can never be queued by a
caller that forgot to check.
"""
import json
from datetime import datetime, timezone

from . import installation_service, privacy, schemas, telemetry_service
from .queues import crash_queue

MAX_SIGNATURE_PART_LENGTH = 96


def _utc_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _safe(value):
    if not value:
        return "unknown"
    text = str(value)[:MAX_SIGNATURE_PART_LENGTH]
    return text


def report_crash(exception, component=None, function_name=None, safe_code_location=None, safe_error_code=None, sender=None):
    if not telemetry_service.is_effectively_enabled():
        return
    installation_id = installation_service.get_installation_id()
    if not installation_id:
        return

    exception_type = type(exception).__name__
    grouping_signature = f"{exception_type}:{_safe(component)}:{_safe(function_name)}"

    payload = schemas.build_crash_report(
        installation_id,
        grouping_signature,
        exception_type,
        _utc_now_iso(),
        component=component,
        function_name=function_name,
        safe_code_location=safe_code_location,
        safe_error_code=safe_error_code,
    )

    try:
        privacy.assert_no_forbidden_keys(payload)
        queue = crash_queue.open_queue()
        queue.enqueue(payload["crash_event_id"], json.dumps(payload))
    except Exception:  # noqa: BLE001 - crash reporting must never itself crash the app
        return

    if sender is not None:
        try:
            sender.wake()
        except Exception:  # noqa: BLE001
            pass
