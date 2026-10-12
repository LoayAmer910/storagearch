"""Ed25519 verification for signed Remote Config / release metadata
 - the client-side mirror of the server's
canonical-string conventions.

`SIGNING_PUBLIC_KEY_SPKI_B64` (build_channel.py) is the PUBLIC half of
this build's channel signing keypair - not a secret, safe to embed here exactly as a
packaged EXE would ("The EXE contains only public keys"). The
matching private key is a Cloudflare Worker secret and never appears
anywhere in this repository, for either channel.

Uses the `cryptography` package (added as a new runtime dependency,
requirements.txt) for the actual Ed25519 primitive - no homemade
cryptography, same rule the backend follows with Web Crypto.
"""
import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .build_channel import SIGNING_PUBLIC_KEY_SPKI_B64

_public_key_cache = None


def _public_key():
    global _public_key_cache
    if _public_key_cache is None:
        der = base64.b64decode(SIGNING_PUBLIC_KEY_SPKI_B64)
        _public_key_cache = load_der_public_key(der)
    return _public_key_cache


def canonical_config_string(config):
    return "\n".join([
        str(config["config_version"]),
        str(bool(config["telemetry_globally_enabled"])),
        json.dumps(sorted(config["disabled_smart_strategies"])),
    ])


def canonical_release_string(release):
    return "\n".join([
        release.get("app_version") or "",
        release.get("build_version") or "",
        release.get("release_channel") or "",
        release.get("sha256") or "",
        release.get("r2_object_key") or "",
        release.get("published_at_utc") or "",
        release.get("status") or "",
        release.get("severity") or "",
    ])


def verify(canonical_string, signature_hex):
    """Returns True only if `signature_hex` is a genuine Ed25519 signature
    (by the embedded public key) over `canonical_string`.
    Never raises - any malformed input (bad hex, wrong length, wrong key)
    is just "not valid", matching the "Invalid signature: ignore
    response; keep LKG/default; do not block local functionality."""
    try:
        signature_bytes = bytes.fromhex(signature_hex)
        _public_key().verify(signature_bytes, canonical_string.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
