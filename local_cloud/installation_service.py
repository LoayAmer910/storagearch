"""Installation registration + credential lifecycle (plan §15).

Registration uses the existing Development Beta Code flow (C2B) unchanged -
this module is a thin, honest client of that endpoint, never reimplementing
any of its security logic. On success, the installation token and request-
signing secret are handed straight to dpapi_store - they are never written
anywhere else, never logged, and never touch the StorageArch business
database.
"""
from datetime import datetime, timezone

from . import cloud_client, dpapi_store, local_state


def is_registered():
    return dpapi_store.load_credentials() is not None


def get_credentials():
    """Returns the DPAPI-protected credentials dict, or None if not
    registered (or if the stored blob can't be read - see dpapi_store's own
    fail-safe handling)."""
    return dpapi_store.load_credentials()


def get_installation_id():
    credentials = get_credentials()
    return credentials["installation_id"] if credentials else None


def register(beta_code):
    """Registers a new installation using the shared Development Beta Code.
    Returns (success: bool, error_code: str | None). Never raises - a
    registration failure is always reported through the return value, so a
    caller (GUI or test) never has to guard this with a try/except of its
    own."""
    if not isinstance(beta_code, str) or not beta_code.strip():
        return False, "invalid_beta_code"

    try:
        result = cloud_client.register(beta_code.strip())
    except Exception:  # noqa: BLE001 - registration must never crash the caller
        return False, "network_error"

    if not result.ok:
        error = (result.body or {}).get("error") if isinstance(result.body, dict) else None
        return False, error or str(result.status)

    body = result.body if isinstance(result.body, dict) else {}
    installation_id = body.get("installation_id")
    installation_token = body.get("installation_token")
    signing_secret_hex = body.get("request_signing_secret")
    if not (installation_id and installation_token and signing_secret_hex):
        return False, "malformed_response"

    try:
        dpapi_store.save_credentials(installation_id, installation_token, signing_secret_hex)
    except OSError:
        return False, "credential_storage_failed"

    config = local_state.load_config()
    config["installation_id"] = installation_id
    config["registered_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    local_state.save_config(config)
    return True, None


def disconnect():
    """Local-only: clears stored credentials and the local installation_id
    mirror. Turning Telemetry off (and clearing its queue) is TelemetryService's
    job, called separately by whatever UI action triggers this - kept
    separate so this function does exactly one thing. Does not attempt to
    reach the Cloud diagnostic-deletion endpoint (that requires the
    installation's own still-valid signature to authorize deleting its own
    data server-side - a distinct, larger feature not in this phase's scope;
    disconnecting locally is always safe and always possible even fully
    offline). Returns False - and changes nothing - if the stored credentials cannot be removed."""
    try:
        dpapi_store.delete_credentials()
    except OSError:
        return False
    config = local_state.load_config()
    config["installation_id"] = None
    local_state.save_config(config)
    return True
