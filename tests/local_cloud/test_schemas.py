"""local_cloud.schemas - every payload is built field by field from an allowlist, so nothing private can slip in."""
from local_cloud import local_state, privacy, schemas


def test_app_started_has_the_envelope_and_only_three_system_facts():
    payload = schemas.build_app_started_event("inst-1", "2026-10-01T00:00:00.000Z", "Windows-11", "AMD64", "16-32gb",
                                              event_id="e-1")
    assert payload == {
        "schema_version": schemas.SCHEMA_VERSION, "event_id": "e-1", "event_type": "app_started",
        "installation_id": "inst-1", "session_id": local_state.session_id(), "occurred_at_utc": "2026-10-01T00:00:00.000Z",
        "app_version": local_state.APP_VERSION, "build_version": local_state.BUILD_VERSION,
        "release_channel": local_state.RELEASE_CHANNEL,
        "windows_version": "Windows-11", "architecture": "AMD64", "ram_bucket": "16-32gb"}
    privacy.assert_no_forbidden_keys(payload)


def test_feedback_carries_optional_fields_only_when_given():
    bare = schemas.build_feedback_submission("bug", "beta_code")
    assert not {"installation_id", "rating", "feedback_text", "diagnostic_context"} & set(bare)
    full = schemas.build_feedback_submission("bug", "installation_token", rating=3, feedback_text="t",
                                             diagnostic_context={"architecture": "AMD64"}, installation_id="inst-1")
    assert (full["rating"], full["feedback_text"], full["diagnostic_context"], full["installation_id"]) == (
        3, "t", {"architecture": "AMD64"}, "inst-1")
    privacy.assert_no_forbidden_keys(full)
