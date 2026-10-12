"""Feedback. Explicit user action only - never gated by
Telemetry consent ("If Telemetry is OFF, Feedback is still
allowed because the send action is explicit"), and sending Feedback never
turns Telemetry on or starts automatic tracking (this module never touches
telemetry_service's consent state at all).
"""
import json
from datetime import datetime, timezone

from . import installation_service, privacy, schemas
from .queues import feedback_queue

APPROVED_CATEGORIES = frozenset({"bug", "performance", "compression", "ui_ux", "other"})
MAX_TEXT_LENGTH = 2000


def _utc_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def submit_feedback(category, rating=None, feedback_text=None, include_diagnostics=False, diagnostic_snapshot=None, beta_code=None, sender=None):
    """Returns (success: bool, error_code: str | None).

    `include_diagnostics` defaults False (the checkbox is OFF
    by default) - `diagnostic_snapshot`, when provided, must already be the
    approved narrow shape (app/build/channel, Windows version/arch, last
    operation type/result, safe error category/code, storage location type)
    - this function does not build it, matching "only ever attached if the
    user explicitly checks... " being the CALLER's decision, not a default
    this service invents on its own.
    """
    if category not in APPROVED_CATEGORIES:
        return False, "invalid_category"
    if rating is not None and (not isinstance(rating, int) or not (1 <= rating <= 5)):
        return False, "invalid_rating"
    if feedback_text and len(feedback_text) > MAX_TEXT_LENGTH:
        return False, "text_too_long"

    installation_id = installation_service.get_installation_id()
    if installation_id:
        auth_mode = "installation_token"
    elif beta_code:
        auth_mode = "beta_code"
    else:
        return False, "not_registered_and_no_beta_code"

    diagnostic_context = diagnostic_snapshot if include_diagnostics else None
    payload = schemas.build_feedback_submission(
        category,
        auth_mode,
        rating=rating,
        feedback_text=feedback_text,
        diagnostic_context=diagnostic_context,
        installation_id=installation_id,
        submitted_at_utc=_utc_now_iso(),
    )

    # `beta_code` is intentionally NOT part of schemas.build_feedback_submission's
    # allowlisted payload (it's in privacy.py's FORBIDDEN_FIELD_NAMES for
    # telemetry/crash automatic-diagnostic payloads and free-text heuristics)
    # - it is a wire-transport concern specific to this one endpoint's
    # unauthenticated fallback path, added only after the schema-validated
    # portion is built and privacy-checked, mirroring exactly how the
    # backend strips it out again before validation.
    try:
        privacy.assert_no_forbidden_keys(payload)
    except ValueError:
        return False, "internal_privacy_violation"
    if auth_mode == "beta_code":
        payload["beta_code"] = beta_code

    queue = feedback_queue.open_queue()
    accepted = queue.enqueue(payload["feedback_id"], json.dumps(payload))
    if not accepted:
        return False, "queue_full"

    if sender is not None:
        try:
            sender.wake()
        except Exception:  # noqa: BLE001
            pass
    return True, None
