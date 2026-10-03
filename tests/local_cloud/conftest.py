"""Fixtures for the cloud client's own modules: a local stand-in server, a test signing key, a registered installation.
Every test already has its own cloud home (the root conftest), so nothing here touches the user's profile."""
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from local_cloud import cloud_client, local_state, metadata_signing
from support.cloud_fixtures import worker  # noqa: F401 - the local stand-in for the Cloudflare Worker


@pytest.fixture
def server(worker, monkeypatch):  # noqa: F811
    """The cloud client talks to the local stand-in."""
    monkeypatch.setattr(cloud_client, "BASE_URL", worker.url)
    monkeypatch.setattr(local_state, "_session_id", None, raising=False)
    return worker


@pytest.fixture
def sign(monkeypatch):
    """Makes the client trust a key made for this test; returns sign(text) -> signature hex."""
    key = Ed25519PrivateKey.generate()
    der = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(metadata_signing, "SIGNING_PUBLIC_KEY_SPKI_B64", base64.b64encode(der).decode())
    monkeypatch.setattr(metadata_signing, "_public_key_cache", None)
    return lambda text: key.sign(text.encode("utf-8")).hex()


@pytest.fixture
def answers(monkeypatch):
    """Replaces the server's answer to a public GET: answers[path] = (status, body) or an exception to raise."""
    table = {}

    def get_public(path):
        answer = table[path]
        if isinstance(answer, Exception):
            raise answer
        return cloud_client.CloudResult(*answer)

    monkeypatch.setattr(cloud_client, "get_public", get_public)
    return table


@pytest.fixture
def registered(server):
    """An installation registered with the stand-in server (credentials stored with DPAPI in the test's cloud home)."""
    from local_cloud import installation_service
    assert installation_service.register("VALID-CODE") == (True, None)
    return server
