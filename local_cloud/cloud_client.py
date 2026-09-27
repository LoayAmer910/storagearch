"""Low-level HTTPS client for the Cloud backend (plan §18.2, §20-§21) -
Development or Beta, whichever `build_channel.BASE_URL` was set to at
build time. Stdlib `urllib.request` only - no new HTTP dependency needed
for this. Every call has a short, bounded timeout; nothing here can hang
StorageArch (plan: "Cloud timeout protection" / "no unconditional network-
thread join").

BASE_URL is fixed at build/checkout time (plan §23, see build_channel.py) -
never a user-facing setting, matching "A Development Build cannot
accidentally send to Beta" (and vice versa).
"""
import json
import urllib.error
import urllib.request

from . import local_state, signing
from .build_channel import BASE_URL

REQUEST_TIMEOUT_SECONDS = 5

# An honest, descriptive User-Agent - not evasion of anything: Python's
# urllib default ("Python-urllib/x.y") is Cloudflare's own well-documented
# example of a generic bot signature and gets edge-blocked (error 1010)
# before the request ever reaches this Worker, confirmed live during this
# phase's own end-to-end verification. Every real HTTP client identifies
# itself; this just does the same.
USER_AGENT = f"StorageArch-Cloud-Client/{local_state.APP_VERSION}"


class CloudResult:
    """`status` is an HTTP status code on any real response from the
    server, or one of the synthetic strings below when the request never
    got a response at all - callers (BackgroundSender) treat both
    "network_error" and "timeout" as retryable, matching plan §19's
    Temporary/Retryable category."""

    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"

    def __init__(self, status, body=None):
        self.status = status
        self.body = body

    @property
    def ok(self):
        return isinstance(self.status, int) and 200 <= self.status < 300

    @property
    def retryable(self):
        if self.status in (CloudResult.NETWORK_ERROR, CloudResult.TIMEOUT):
            return True
        if isinstance(self.status, int):
            return self.status == 429 or self.status >= 500
        return False


def _do_request(method, url, headers, body_text):
    data = body_text.encode("utf-8") if body_text else None
    all_headers = {"User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return CloudResult(CloudResult.TIMEOUT)

    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return CloudResult(status, parsed)


def get_public(path):
    """Unauthenticated read (plan §21: config/release metadata) - never
    includes installation_id/session_id/any per-installation data."""
    return _do_request("GET", BASE_URL + path, {"Accept": "application/json"}, None)


def register(beta_code):
    body_text = json.dumps({"beta_code": beta_code})
    return _do_request("POST", BASE_URL + "/api/v1/installations/register", {"Content-Type": "application/json"}, body_text)


def post_signed(path, body_dict, credentials):
    """`credentials` is the dict returned by dpapi_store.load_credentials()."""
    body_text = json.dumps(body_dict)
    headers, request_id = signing.build_signed_headers(
        "POST", path, body_text, credentials["installation_id"], credentials["installation_token"], credentials["signing_secret_hex"]
    )
    result = _do_request("POST", BASE_URL + path, headers, body_text)
    return result, request_id


def post_unsigned(path, body_dict):
    """Unauthenticated POST - the ONLY mutation path that doesn't need
    installation credentials at all: Feedback's Beta-Code fallback mode
    (plan §13.3), matching the backend's own handleBetaCodeFeedback, which
    is gated by the request NOT carrying the C2B signed-header set. Used
    for nothing else - crash reports and telemetry always require a real
    installation and are always sent via post_signed."""
    body_text = json.dumps(body_dict)
    return _do_request("POST", BASE_URL + path, {"Content-Type": "application/json"}, body_text)
