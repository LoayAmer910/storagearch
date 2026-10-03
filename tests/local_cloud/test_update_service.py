"""local_cloud.update_service - release information is shown only when it is genuinely signed; every failure is simply
"nothing to show", and nothing is ever downloaded."""
import pytest

from local_cloud import metadata_signing, update_service

PATH = "/api/v1/releases/latest"
RELEASE = {"app_version": "2.1.0", "build_version": "42", "release_channel": "development", "sha256": "ab" * 32,
           "r2_object_key": "releases/2.1.0.exe", "published_at_utc": "2026-10-01T10:00:00Z", "status": "published",
           "severity": "normal"}


def test_a_signed_release_is_returned(answers, sign):
    answers[PATH] = (200, {"release": RELEASE, "signature": sign(metadata_signing.canonical_release_string(RELEASE))})
    assert update_service.check_latest_release() == RELEASE


@pytest.mark.parametrize("answer", [
    ConnectionError("offline"),
    (404, {"error": "no release"}),
    (200, "not a dict"),
    (200, {"release": "text", "signature": "ab"}),
    (200, {"release": RELEASE}),
    (200, {"release": dict(RELEASE, severity="critical"), "signature": None}),
], ids=["offline", "not_found", "not_a_dict", "release_not_a_dict", "no_signature", "signature_not_text"])
def test_anything_unsigned_or_unreachable_shows_nothing(answers, answer):
    answers[PATH] = answer
    assert update_service.check_latest_release() is None


def test_a_release_changed_after_signing_shows_nothing(answers, sign):
    signature = sign(metadata_signing.canonical_release_string(RELEASE))
    answers[PATH] = (200, {"release": dict(RELEASE, r2_object_key="elsewhere/evil.exe"), "signature": signature})
    assert update_service.check_latest_release() is None
