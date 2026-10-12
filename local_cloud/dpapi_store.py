"""Windows DPAPI credential protection ("Secrets stored locally
must be protected using Windows DPAPI").

Uses `crypt32.dll`'s `CryptProtectData`/`CryptUnprotectData` directly via
ctypes - no `pywin32` dependency needed for just this. DPAPI ties the
protected blob to the current Windows user account; only that same user
(not even an admin, without extra work) can unprotect it, and nothing here
adds a hardcoded secondary secret - the OS-managed per-user key is the
entire protection, exactly as DPAPI is designed to be used.

`CRYPTPROTECT_UI_FORBIDDEN` is always passed so this can never block on a
Windows UI prompt - StorageArch must never hang waiting on anything here.
"""
import ctypes
import json
import os
import time
from ctypes import wintypes

from . import local_state

CRYPTPROTECT_UI_FORBIDDEN = 0x1
DELETE_RETRY_SECONDS = 2.0


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    return blob, buf  # caller must keep `buf` alive for the blob's lifetime


def _from_blob(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(data):
    """Encrypts `data` (bytes) for the current Windows user. Raises OSError
    on failure - callers decide how to handle that (this module never
    silently returns plaintext)."""
    in_blob, _in_buf = _to_blob(data)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptProtectData failed: %s" % ctypes.WinError())
    return _from_blob(out_blob)


def unprotect(blob_bytes):
    """Decrypts a blob produced by `protect` for the current Windows user.
    Raises OSError on failure (wrong user, corrupted blob, ...)."""
    in_blob, _in_buf = _to_blob(blob_bytes)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed: %s" % ctypes.WinError())
    return _from_blob(out_blob)


def save_credentials(installation_id, installation_token, signing_secret_hex):
    """Stores the installation token and request-signing secret DPAPI-
    protected on disk. Never stores them in the StorageArch business
    database (task requirement) - this file is the only place they live
    locally, under local_state.base_dir()."""
    payload = json.dumps({
        "installation_id": installation_id,
        "installation_token": installation_token,
        "signing_secret_hex": signing_secret_hex,
    }).encode("utf-8")
    protected = protect(payload)
    path = local_state.credentials_path()
    temp_path = path + ".tmp"
    with open(temp_path, "wb") as f:
        f.write(protected)
    os.replace(temp_path, path)


def load_credentials():
    """Returns {"installation_id", "installation_token", "signing_secret_hex"}
    or None if no credentials are stored, or if the stored blob can't be
    decrypted/parsed (wrong user, corruption, tampering) - never raises,
    matching the fail-safe requirement that a Cloud-state problem must
    never block normal StorageArch use. A load failure here is exactly the
    "invalid/expired/revoked" local state case the app is expected to
    tolerate gracefully."""
    path = local_state.credentials_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            protected = f.read()
        plaintext = unprotect(protected)
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict) or not all(k in data for k in ("installation_id", "installation_token", "signing_secret_hex")):
            return None
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def delete_credentials():
    """Removes the stored credentials. A reader in this app (the background sender) may hold the file for a
    few milliseconds, which Windows reports as a sharing violation - so a held file is retried for up to
    DELETE_RETRY_SECONDS. Raises OSError if it still cannot be removed: a disconnect must never report
    success while the credentials remain."""
    path = local_state.credentials_path()
    deadline = time.monotonic() + DELETE_RETRY_SECONDS
    while True:
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
