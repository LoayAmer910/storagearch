"""local_cloud.metadata_signing - only a genuine Ed25519 signature over the exact canonical text is accepted; anything
malformed is simply "not valid", never an exception."""
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from local_cloud import metadata_signing

CONFIG = {"config_version": 7, "telemetry_globally_enabled": True, "disabled_smart_strategies": ["zstd", "brotli"]}


def test_the_public_key_built_into_the_app_loads(monkeypatch):
    monkeypatch.setattr(metadata_signing, "_public_key_cache", None)
    assert isinstance(metadata_signing._public_key(), Ed25519PublicKey)


def test_the_canonical_config_text_does_not_depend_on_list_order():
    reordered = dict(CONFIG, disabled_smart_strategies=["brotli", "zstd"])
    assert metadata_signing.canonical_config_string(CONFIG) == metadata_signing.canonical_config_string(reordered)
    assert metadata_signing.canonical_config_string(CONFIG) == '7\nTrue\n["brotli", "zstd"]'


def test_the_canonical_release_text_keeps_every_field_in_order_and_empty_ones_empty():
    release = {"app_version": "2.0", "build_version": "41", "severity": "critical"}
    assert metadata_signing.canonical_release_string(release) == "2.0\n41\n\n\n\n\n\ncritical"


def test_a_genuine_signature_is_accepted(sign):
    text = metadata_signing.canonical_config_string(CONFIG)
    assert metadata_signing.verify(text, sign(text)) is True


@pytest.mark.parametrize("tamper", ["changed text", "bad hex", "wrong length", "not a string", "other key"])
def test_anything_but_a_genuine_signature_is_refused_quietly(sign, tamper):
    text = metadata_signing.canonical_config_string(CONFIG)
    signature = sign(text)
    if tamper == "changed text":
        text = text.replace("7", "8", 1)
    elif tamper == "bad hex":
        signature = "zz" + signature[2:]
    elif tamper == "wrong length":
        signature = signature[:-2]
    elif tamper == "not a string":
        signature = None
    else:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        signature = Ed25519PrivateKey.generate().sign(text.encode()).hex()
    assert metadata_signing.verify(text, signature) is False
