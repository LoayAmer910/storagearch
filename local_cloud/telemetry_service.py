"""Telemetry consent + event construction.

Local consent is authoritative - `is_consent_on()` reads only
the local config file; nothing in this module or package can be made to
report telemetry the user hasn't explicitly turned on, and Remote Config's
`telemetry_globally_enabled` can only ever make effective telemetry MORE
off, never less (see `is_effectively_enabled` - it's an AND, not an OR).
"""
import json
from datetime import datetime, timezone

from . import installation_service, local_state, privacy, schemas, system_info
from .queues import telemetry_queue


def _utc_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def is_consent_on():
    return bool(local_state.load_config().get("telemetry_enabled", False))


def is_globally_enabled():
    """Consults the last-known-good Remote Config (the "global
    Cloud safety switch"). Defaults to True (enabled) when no config has
    ever been fetched - absence of a kill switch is not itself a reason to
    withhold telemetry the user already separately consented to."""
    from . import remote_config_service  # local import: avoids a cycle (remote_config_service doesn't need telemetry_service, but keeping this import lazy costs nothing and keeps module load order flexible)

    config = remote_config_service.get_last_known_good()
    if config is None:
        return True
    return bool(config.get("telemetry_globally_enabled", True))


def is_effectively_enabled():
    return is_consent_on() and is_globally_enabled()


def set_consent(enabled, sender=None):
    """Flips local consent. Turning ON never happens remotely - this is
    only ever called from a local user action (a GUI checkbox, a test).
    Turning OFF: best-effort SYNCHRONOUS opt-out signal (never queued -
    telemetry must not be kept after consent is withdrawn just to
    guarantee an alert), then the queue is deleted immediately."""
    config = local_state.load_config()
    was_enabled = bool(config.get("telemetry_enabled", False))
    if bool(enabled) == was_enabled:
        return

    config["telemetry_enabled"] = bool(enabled)
    config["telemetry_changed_at_utc"] = _utc_now_iso()

    if was_enabled and not enabled:
        # waits for a telemetry send already in flight; after this no queued event can be sent
        with telemetry_queue.SEND_LOCK:
            local_state.save_config(config)
            telemetry_queue.open_queue().clear()
        _send_opt_out_best_effort()
        return
    local_state.save_config(config)
    if enabled and sender is not None:
        try:
            sender.wake()
        except Exception:  # noqa: BLE001
            pass


def _send_opt_out_best_effort():
    installation_id = installation_service.get_installation_id()
    credentials = installation_service.get_credentials()
    if not installation_id or not credentials:
        return
    try:
        from . import cloud_client

        payload = schemas.build_telemetry_opted_out_event(installation_id, _utc_now_iso())
        cloud_client.post_signed("/api/v1/telemetry", payload, credentials)
    except Exception:  # noqa: BLE001 - best-effort only, offline is a normal outcome here
        pass


def _enqueue(payload, sender):
    try:
        privacy.assert_no_forbidden_keys(payload)
        queue = telemetry_queue.open_queue()
        queue.enqueue(payload["event_id"], json.dumps(payload))
    except Exception:  # noqa: BLE001 - a telemetry bug must never surface to the caller
        return
    if sender is not None:
        try:
            sender.wake()
        except Exception:  # noqa: BLE001
            pass


def record_app_started(sender=None):
    if not is_effectively_enabled():
        return
    installation_id = installation_service.get_installation_id()
    if not installation_id:
        return
    payload = schemas.build_app_started_event(
        installation_id, _utc_now_iso(), system_info.windows_version(), system_info.architecture(), system_info.ram_bucket()
    )
    _enqueue(payload, sender)


def record_operation_event(
    event_type,
    operation_type,
    result,
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
    sender=None,
):
    """The main integration point - called by core/events.py for the core's
    operations strictly AFTER the local operation has already committed to
    its final result. Never affects that result: this function has no
    return value callers act on, and never raises (see `_enqueue`)."""
    if not is_effectively_enabled():
        return
    installation_id = installation_service.get_installation_id()
    if not installation_id:
        return
    payload = schemas.build_operation_event(
        event_type,
        operation_type,
        result,
        installation_id,
        schemas.new_uuid(),
        _utc_now_iso(),
        duration_ms=duration_ms,
        error_category=error_category,
        error_code=error_code,
        file_count=file_count,
        original_bytes_total=original_bytes_total,
        stored_bytes_total=stored_bytes_total,
        saved_bytes_total=saved_bytes_total,
        saving_percent=saving_percent,
        restored_bytes_total=restored_bytes_total,
        timeout_count=timeout_count,
        fallback_count=fallback_count,
        strategy_failure_count=strategy_failure_count,
        storage_location_type=storage_location_type,
        smart_metrics=smart_metrics,
    )
    _enqueue(payload, sender)
