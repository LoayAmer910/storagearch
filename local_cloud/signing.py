"""Request signing - the client-side mirror of the server's signing format.

ONE canonical format, matching the backend exactly (frozen there, mirrored
here - never a client-specific variant):

    {METHOD}\\n{PATH}\\n{TIMESTAMP}\\n{REQUEST_ID}\\n{SHA256_HEX(BODY)}

signed as HMAC-SHA256(signing_secret_bytes, canonical_string), lowercase hex.
Only Python stdlib (`hashlib`, `hmac`, `uuid`) - no homemade primitive, same rule the backend follows.
"""
import hashlib
import hmac as hmac_module
import uuid
from datetime import datetime, timezone


def utc_timestamp():
    """ISO-8601 UTC with millisecond precision and a literal 'Z', e.g.
    2026-08-14T12:00:00.000Z - matches the server's documented format
    exactly (byte-for-byte, since the timestamp is signed as-is, never
    reformatted)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def fresh_request_id():
    """A fresh UUID for every network attempt (never reused across retries -
    that's what makes it different from event_id)."""
    return str(uuid.uuid4())


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def build_canonical_string(method, path, timestamp, request_id, body_text):
    return "\n".join([method.upper(), path, timestamp, request_id, sha256_hex(body_text)])


def sign(signing_secret_bytes, canonical_string):
    return hmac_module.new(signing_secret_bytes, canonical_string.encode("utf-8"), hashlib.sha256).hexdigest()


def build_signed_headers(method, path, body_text, installation_id, installation_token, signing_secret_hex):
    """Returns the full header dict for an authenticated request, plus the
    request_id used (callers that need it for logging/dedup bookkeeping)."""
    timestamp = utc_timestamp()
    request_id = fresh_request_id()
    canonical = build_canonical_string(method, path, timestamp, request_id, body_text)
    signature = sign(bytes.fromhex(signing_secret_hex), canonical)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {installation_token}",
        "X-StorageArch-Installation-Id": installation_id,
        "X-StorageArch-Request-Id": request_id,
        "X-StorageArch-Timestamp": timestamp,
        "X-StorageArch-Signature": signature,
    }
    return headers, request_id
