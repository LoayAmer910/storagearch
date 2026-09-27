"""Privacy allowlist (plan §7-§8) - the client-side half of the same rule
worker-public's cloud/schemas/src/privacy.ts enforces server-side.

The PRIMARY defense here is structural, exactly like the backend: every
payload in schemas.py is built by explicitly naming each allowed field one
at a time (never `**vars(some_internal_object)` or similar blind
passthrough) - plan §8's "never serialize a large internal object and then
try to remove unsafe fields". FORBIDDEN_FIELD_NAMES below is kept in sync
with cloud/schemas/src/privacy.ts's own list, used only by tests to assert
none of these names is ever a key schemas.py could produce, and as a
secondary defensive check in cloud_client.py right before a request leaves
the machine (belt-and-suspenders, not the primary mechanism).
"""

APPROVED_APP_EVENT_TYPES = frozenset({"app_started", "telemetry_opted_out"})

APPROVED_OPERATION_EVENT_TYPES = frozenset({
    "archive_completed",
    "archive_failed",
    "verify_completed",
    "verify_failed",
    "checkout_completed",
    "checkout_failed",
    "checkin_completed",
    "checkin_failed",
})

APPROVED_EVENT_TYPES = APPROVED_APP_EVENT_TYPES | APPROVED_OPERATION_EVENT_TYPES

# Mirrors cloud/schemas/src/privacy.ts's FORBIDDEN_FIELD_NAMES exactly.
FORBIDDEN_FIELD_NAMES = frozenset({
    "file_contents",
    "archive_contents",
    "zip_contents",
    "restored_contents",
    "workspace_contents",
    "filename",
    "file_name",
    "folder_name",
    "path",
    "full_path",
    "file_path",
    "folder_path",
    "windows_username",
    "username",
    "computer_name",
    "device_name",
    "hostname",
    "machine_name",
    "manifest",
    "archive_manifest",
    "sqlite_contents",
    "local_db_contents",
    "original_file_sha256",
    "file_sha256",
    "full_file_hash",
    "app_secret",
    "cloud_credential",
    "beta_code",
    "installation_signing_secret",
    "raw_request_body",
    "raw_exception_dump",
    "stack_trace_raw",
})


def assert_no_forbidden_keys(payload):
    """Defensive check, not the primary mechanism (see module docstring).
    Raises ValueError if somehow triggered - a caller producing a payload
    with a forbidden key is a programming error that must be caught loudly
    in development, never silently "fixed" by stripping the field and
    sending the rest."""
    bad = set(payload.keys()) & FORBIDDEN_FIELD_NAMES
    if bad:
        raise ValueError(f"payload contains forbidden field name(s): {sorted(bad)}")
