"""Release/update check. Notification only - nothing here ever
downloads or installs anything; the only output is verified metadata for a
caller (GUI) to display. A failure of any kind returns None silently and
must never affect normal StorageArch operation.
"""
from . import cloud_client, metadata_signing


def check_latest_release():
    """Returns verified release metadata (app_version, build_version,
    release_channel, sha256, r2_object_key, published_at_utc, status,
    severity) or None on any failure - network error, non-200, malformed
    body, or invalid signature all collapse to the same "nothing to show"
    outcome; callers must not distinguish or surface these as errors."""
    try:
        result = cloud_client.get_public("/api/v1/releases/latest")
    except Exception:  # noqa: BLE001
        return None

    if not result.ok or not isinstance(result.body, dict):
        return None

    release = result.body.get("release")
    signature = result.body.get("signature")
    if not isinstance(release, dict) or not isinstance(signature, str):
        return None

    canonical = metadata_signing.canonical_release_string(release)
    if not metadata_signing.verify(canonical, signature):
        return None

    return release
