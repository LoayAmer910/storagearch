"""Wire payload builders - the client-side mirror of cloud/schemas/src/
{envelope,telemetry,feedback,crash}.ts.

Every function here takes only explicit, named, already-sanitized
parameters and returns a plain dict containing ONLY the fields the backend
schema allows (plan §8's "constructed from approved fields... never
serialize a large internal object and then try to remove unsafe fields").
Optional fields are OMITTED from the dict entirely when not provided
(never sent as an explicit `null`) - the backend's zod `.optional()`
schemas accept a missing key, not a null value, so this must match.

schema_version is frozen at 1 for the whole of this Beta era, exactly as
the backend documents (API_CONTRACT.md referenced by cloud/schemas).
"""
import uuid

from . import local_state

SCHEMA_VERSION = 1


def new_uuid():
    return str(uuid.uuid4())


def _envelope(event_id, event_type, installation_id, occurred_at_utc):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "installation_id": installation_id,
        "session_id": local_state.session_id(),
        "occurred_at_utc": occurred_at_utc,
        "app_version": local_state.APP_VERSION,
        "build_version": local_state.BUILD_VERSION,
        "release_channel": local_state.RELEASE_CHANNEL,
    }


def build_app_started_event(installation_id, occurred_at_utc, windows_version, architecture, ram_bucket, event_id=None):
    payload = _envelope(event_id or new_uuid(), "app_started", installation_id, occurred_at_utc)
    payload["windows_version"] = windows_version
    payload["architecture"] = architecture
    payload["ram_bucket"] = ram_bucket
    return payload


def build_telemetry_opted_out_event(installation_id, occurred_at_utc, event_id=None):
    return _envelope(event_id or new_uuid(), "telemetry_opted_out", installation_id, occurred_at_utc)


def build_smart_aggregate_row(
    file_type,
    strategy,
    file_count,
    original_bytes,
    stored_bytes,
    saved_bytes,
    avg_saving_percent=None,
    min_saving_percent=None,
    max_saving_percent=None,
    avg_processing_ms=None,
    min_processing_ms=None,
    max_processing_ms=None,
    timeout_count=0,
    fallback_count=0,
    failure_count=0,
    safe_fallback_category=None,
):
    row = {
        "file_type": file_type,
        "strategy": strategy,
        "file_count": file_count,
        "original_bytes": original_bytes,
        "stored_bytes": stored_bytes,
        "saved_bytes": saved_bytes,
        "timeout_count": timeout_count,
        "fallback_count": fallback_count,
        "failure_count": failure_count,
    }
    optional = {
        "avg_saving_percent": avg_saving_percent,
        "min_saving_percent": min_saving_percent,
        "max_saving_percent": max_saving_percent,
        "avg_processing_ms": avg_processing_ms,
        "min_processing_ms": min_processing_ms,
        "max_processing_ms": max_processing_ms,
        "safe_fallback_category": safe_fallback_category,
    }
    for key, value in optional.items():
        if value is not None:
            row[key] = value
    return row


def build_operation_event(
    event_type,
    operation_type,
    result,
    installation_id,
    operation_id,
    occurred_at_utc,
    duration_ms=None,
    error_category=None,
    error_code=None,
    file_count=None,
    original_bytes_total=None,
    stored_bytes_total=None,
    saved_bytes_total=None,
    saving_percent=None,
    restored_bytes_total=None,
    timeout_count=None,
    fallback_count=None,
    strategy_failure_count=None,
    storage_location_type=None,
    smart_metrics=None,
    event_id=None,
):
    payload = _envelope(event_id or new_uuid(), event_type, installation_id, occurred_at_utc)
    payload["operation_id"] = operation_id
    payload["operation_type"] = operation_type
    payload["result"] = result
    optional = {
        "duration_ms": duration_ms,
        "error_category": error_category,
        "error_code": error_code,
        "file_count": file_count,
        "original_bytes_total": original_bytes_total,
        "stored_bytes_total": stored_bytes_total,
        "saved_bytes_total": saved_bytes_total,
        "saving_percent": saving_percent,
        "restored_bytes_total": restored_bytes_total,
        "timeout_count": timeout_count,
        "fallback_count": fallback_count,
        "strategy_failure_count": strategy_failure_count,
        "storage_location_type": storage_location_type,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    if smart_metrics:
        payload["smart_metrics"] = smart_metrics
    return payload


def build_feedback_submission(
    category,
    auth_mode,
    app_version_override=None,
    rating=None,
    feedback_text=None,
    diagnostic_context=None,
    installation_id=None,
    feedback_id=None,
    submitted_at_utc=None,
):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "feedback_id": feedback_id or new_uuid(),
        "auth_mode": auth_mode,
        "category": category,
        "app_version": app_version_override or local_state.APP_VERSION,
        "build_version": local_state.BUILD_VERSION,
        "release_channel": local_state.RELEASE_CHANNEL,
        "submitted_at_utc": submitted_at_utc,
    }
    if installation_id is not None:
        payload["installation_id"] = installation_id
    if rating is not None:
        payload["rating"] = rating
    if feedback_text:
        payload["feedback_text"] = feedback_text
    if diagnostic_context:
        payload["diagnostic_context"] = diagnostic_context
    return payload


def build_crash_report(
    installation_id,
    grouping_signature,
    exception_type,
    occurred_at_utc,
    component=None,
    function_name=None,
    safe_code_location=None,
    safe_error_code=None,
    crash_event_id=None,
):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "crash_event_id": crash_event_id or new_uuid(),
        "installation_id": installation_id,
        "session_id": local_state.session_id(),
        "grouping_signature": grouping_signature,
        "exception_type": exception_type,
        "app_version": local_state.APP_VERSION,
        "build_version": local_state.BUILD_VERSION,
        "release_channel": local_state.RELEASE_CHANNEL,
        "occurred_at_utc": occurred_at_utc,
    }
    optional = {
        "component": component,
        "function_name": function_name,
        "safe_code_location": safe_code_location,
        "safe_error_code": safe_error_code,
    }
    for key, value in optional.items():
        if value:
            payload[key] = value
    return payload
