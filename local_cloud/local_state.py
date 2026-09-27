"""Local Cloud state paths and small non-secret config (plan §14).

Everything Cloud-related lives under %LOCALAPPDATA%\\StorageArch\\ -
completely separate from the StorageArch business database
(storage_config.db_path(), inside the user's chosen data_root). Cloud
credentials are NEVER stored in that business database (task requirement) -
this module owns Cloud's own small footprint instead.

`STORAGEARCH_CLOUD_HOME` env var override exists only for tests (mirrors
storage_config.py's own sandbox-friendly design, which uses the process cwd
for the same reason) - production code never sets it, so it always resolves
to the real per-user LOCALAPPDATA path.
"""
import json
import os
import uuid

from .build_channel import APP_VERSION, BUILD_VERSION, RELEASE_CHANNEL

_ENV_OVERRIDE = "STORAGEARCH_CLOUD_HOME"
_APP_DIR_NAME = "StorageArch"

_PRIVACY_POLICY_VERSION = 1

__all__ = ["APP_VERSION", "BUILD_VERSION", "RELEASE_CHANNEL"]

_session_id = None


def base_dir():
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        path = override
    else:
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(local_app_data, _APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(base_dir(), "cloud_config.json")


def credentials_path():
    return os.path.join(base_dir(), "credentials.dat")


def telemetry_queue_path():
    return os.path.join(base_dir(), "telemetry_queue.db")


def feedback_queue_path():
    return os.path.join(base_dir(), "feedback_queue.db")


def crash_queue_path():
    return os.path.join(base_dir(), "crash_queue.db")


def remote_config_lkg_path():
    return os.path.join(base_dir(), "remote_config_lkg.json")


def session_id():
    """One random UUID per process lifetime (plan §6.2), cached after first
    call so every telemetry event this run emits shares the same value."""
    global _session_id
    if _session_id is None:
        _session_id = str(uuid.uuid4())
    return _session_id


_DEFAULT_CONFIG = {
    "installation_id": None,
    "telemetry_enabled": False,  # OFF by default, plan §5.1
    "telemetry_changed_at_utc": None,
    "privacy_policy_version": _PRIVACY_POLICY_VERSION,
    "registered_at_utc": None,
}


def load_config():
    """Never raises - a missing/corrupt config file is treated as a fresh
    install (all defaults), matching the fail-safe philosophy applied
    everywhere else in this package."""
    path = config_path()
    if not os.path.isfile(path):
        return dict(_DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(_DEFAULT_CONFIG)
        merged = dict(_DEFAULT_CONFIG)
        merged.update({k: v for k, v in data.items() if k in _DEFAULT_CONFIG})
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_CONFIG)


def save_config(config):
    path = config_path()
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(temp_path, path)


def reset_local_state():
    """Deletes all Cloud-local state (config, credentials, queues) - the
    local half of plan §5.4's Uninstall requirement, and useful for test
    teardown. Never touches the StorageArch business database, archives,
    or workspace - those are explicitly out of scope for this function."""
    for path_fn in (config_path, credentials_path, telemetry_queue_path, feedback_queue_path, crash_queue_path, remote_config_lkg_path):
        path = path_fn()
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    global _session_id
    _session_id = None
