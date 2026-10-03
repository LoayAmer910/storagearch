"""local_cloud.feedback_service - feedback is checked locally first, queued with the right kind of authentication, and
only carries diagnostics when the user ticked the box."""
import json

import pytest

from local_cloud import feedback_service as fb, privacy
from local_cloud.queues import feedback_queue


class _Sender:
    def __init__(self, fails=False):
        self.woken, self.fails = 0, fails

    def wake(self):
        self.woken += 1
        if self.fails:
            raise RuntimeError("sender is gone")


def _queued():
    return [json.loads(i.payload_json) for i in feedback_queue.open_queue().list_pending()]


@pytest.mark.parametrize("kwargs, reason", [
    ({"category": "praise"}, "invalid_category"),
    ({"category": "bug", "rating": 6}, "invalid_rating"),
    ({"category": "bug", "rating": "5"}, "invalid_rating"),
    ({"category": "bug", "feedback_text": "x" * (fb.MAX_TEXT_LENGTH + 1)}, "text_too_long"),
    ({"category": "bug"}, "not_registered_and_no_beta_code"),
])
def test_bad_or_unauthenticated_feedback_is_refused_locally(kwargs, reason):
    assert fb.submit_feedback(**kwargs) == (False, reason)
    assert _queued() == []


def test_a_registered_installation_sends_with_its_token_and_diagnostics_only_when_ticked(registered):
    snapshot = {"windows_version": "10", "architecture": "AMD64"}
    sender = _Sender(fails=True)
    assert fb.submit_feedback("bug", rating=4, feedback_text="slow", diagnostic_snapshot=snapshot, sender=sender) == (True, None)
    assert fb.submit_feedback("ui_ux", include_diagnostics=True, diagnostic_snapshot=snapshot) == (True, None)
    first, second = _queued()
    assert first["auth_mode"] == "installation_token" and "diagnostic_context" not in first
    assert second["diagnostic_context"] == snapshot
    assert sender.woken == 1


def test_without_an_installation_the_beta_code_is_attached_for_this_one_request():
    assert fb.submit_feedback("other", feedback_text="hello", beta_code="BETA-1") == (True, None)
    (payload,) = _queued()
    assert payload["auth_mode"] == "beta_code" and payload["beta_code"] == "BETA-1"
    assert "installation_id" not in payload


def test_a_full_feedback_queue_refuses_new_feedback(registered, monkeypatch):
    monkeypatch.setattr(feedback_queue, "MAX_BYTES", 1)
    assert fb.submit_feedback("bug", feedback_text="one more") == (False, "queue_full")


def test_a_payload_that_breaks_the_privacy_rule_is_never_queued(registered, monkeypatch):
    def refuse(payload):
        raise ValueError("forbidden field")

    monkeypatch.setattr(privacy, "assert_no_forbidden_keys", refuse)
    assert fb.submit_feedback("bug") == (False, "internal_privacy_violation")
    assert _queued() == []
