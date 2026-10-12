"""Remote Config client. Fetches, verifies (Ed25519), and
maintains a Last Known Good copy of exactly the three approved keys.

Cannot weaken local safety by construction: this module is only ever
consulted for two things elsewhere in the app -
`telemetry_service.is_globally_enabled()` (an AND-gate that can only make
effective telemetry MORE off, never on) and `disabled_strategies_set()`
(an ADDITIVE filter the core applies to its own candidate list
BEFORE evaluation - it can only remove a strategy already in the installed
binary, never add one that isn't, never touch SHA-256/Verify/exact-restore/
fallback/timeout logic at all). Nothing else in this package or in the
core reads Remote Config for anything else.
"""
import json
import os
from datetime import datetime, timezone

from . import cloud_client, local_state, metadata_signing

_ALLOWED_KEYS = {"config_version", "telemetry_globally_enabled", "disabled_smart_strategies"}


def _utc_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def fetch_and_verify():
    """Fetches, schema-checks, and signature-verifies the current published
    Remote Config. On ANY failure (network, non-200, wrong shape, invalid/
    missing signature) returns None and leaves any existing Last Known Good
    file completely untouched - never raises, never blocks the caller."""
    try:
        result = cloud_client.get_public("/api/v1/config")
    except Exception:  # noqa: BLE001
        return None

    if not result.ok or not isinstance(result.body, dict):
        return None

    config = result.body.get("config")
    signature = result.body.get("signature")
    if not isinstance(config, dict) or not isinstance(signature, str):
        return None
    if set(config.keys()) != _ALLOWED_KEYS:
        # Never trust a response carrying an unapproved key, even if the
        # signature happens to verify over whatever shape it has - the
        # allowlist is checked before, not instead of, the signature.
        return None
    if not isinstance(config.get("disabled_smart_strategies"), list):
        return None

    canonical = metadata_signing.canonical_config_string(config)
    if not metadata_signing.verify(canonical, signature):
        return None

    # Rollback/downgrade protection: a signature being valid only proves the
    # payload came from the real signer, not that it's the
    # LATEST thing the signer published - a validly-signed but stale
    # response (replay, edge cache, attacker-controlled downgrade) must not
    # be allowed to quietly replace a newer Last Known Good. Equal version
    # is accepted (deterministic no-op-shaped re-save of the same config,
    # not a downgrade); only a STRICTLY lower version is rejected. No
    # existing LKG (fresh install) always accepts, unchanged from before.
    current = get_last_known_good()
    if current is not None and config["config_version"] < current["config_version"]:
        return None

    _save_last_known_good(config)
    return config


def get_last_known_good():
    """Returns the last verified config, or None if none has ever been
    saved (fresh install / never reached the network yet - the own
    "If no LKG: use safe built-in defaults" case, which callers handle by
    treating None the same way they'd handle a disabled/absent key)."""
    path = local_state.remote_config_lkg_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not _ALLOWED_KEYS.issubset(data.keys()):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_last_known_good(config):
    data = dict(config)
    data["saved_at_utc"] = _utc_now_iso()
    path = local_state.remote_config_lkg_path()
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_path, path)


def disabled_strategies_set():
    config = get_last_known_good()
    if config is None:
        return frozenset()
    return frozenset(config.get("disabled_smart_strategies") or [])
