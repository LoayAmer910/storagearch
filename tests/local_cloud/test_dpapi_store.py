"""local_cloud.dpapi_store - the installation's secrets are kept encrypted for this Windows user only; a damaged or
foreign file means "not registered", never a crash."""
import json
import os

import pytest

from local_cloud import dpapi_store, local_state


def test_saved_credentials_are_encrypted_on_disk_and_load_back():
    dpapi_store.save_credentials("inst-1", "token-secret", "ab" * 32)
    raw = open(local_state.credentials_path(), "rb").read()
    assert b"token-secret" not in raw, "the token must never be on disk in plain text"
    assert dpapi_store.load_credentials() == {"installation_id": "inst-1", "installation_token": "token-secret",
                                              "signing_secret_hex": "ab" * 32}


@pytest.mark.parametrize("content", [b"not an encrypted blob", b""], ids=["damaged", "empty"])
def test_a_damaged_credentials_file_means_not_registered(content):
    with open(local_state.credentials_path(), "wb") as f:
        f.write(content)
    assert dpapi_store.load_credentials() is None


def test_credentials_missing_a_field_mean_not_registered():
    with open(local_state.credentials_path(), "wb") as f:
        f.write(dpapi_store.protect(json.dumps({"installation_id": "inst-1"}).encode()))
    assert dpapi_store.load_credentials() is None


def test_decrypting_something_that_was_never_encrypted_is_an_error():
    with pytest.raises(OSError, match="CryptUnprotectData failed"):
        dpapi_store.unprotect(b"plain bytes")


def test_deleting_removes_the_file_and_deleting_again_is_harmless():
    dpapi_store.save_credentials("inst-1", "t", "ab" * 32)
    dpapi_store.delete_credentials()
    assert not os.path.exists(local_state.credentials_path())
    dpapi_store.delete_credentials()
