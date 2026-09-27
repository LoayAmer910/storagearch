"""Single background sender (plan §18): one dedicated thread, one HTTP
request at a time, priority Crash -> Feedback -> Telemetry, natural retry
triggers instead of a tight loop, bounded shutdown.

This is the ONLY place in local_cloud that talks to the network on an
ongoing basis - services enqueue locally and call `wake()`; they never
block waiting for a send to complete. Nothing here can hang app shutdown:
`shutdown()` always returns within its bounded timeout regardless of what
the thread is doing.
"""
import threading

from . import cloud_client
from .queues import crash_queue, feedback_queue, telemetry_queue

FALLBACK_POLL_SECONDS = 60
SHUTDOWN_JOIN_SECONDS = 1.0

_PATHS = {
    "crash": "/api/v1/crashes",
    "feedback": "/api/v1/feedback",
    "telemetry": "/api/v1/telemetry",
}


class BackgroundSender:
    def __init__(self, get_credentials):
        """`get_credentials` is a zero-arg callable returning the current
        dpapi_store credentials dict or None - looked up fresh on every send
        attempt (not cached at construction) so a reconnect takes effect
        immediately without recreating this object."""
        self._get_credentials = get_credentials
        self._crash_queue = crash_queue.open_queue()
        self._feedback_queue = feedback_queue.open_queue()
        self._telemetry_queue = telemetry_queue.open_queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="StorageArchCloudSender", daemon=True)
            self._thread.start()
        self.wake()

    def wake(self):
        self._wake.set()

    def reconnect(self):
        """Clears the auth-failure pause (plan §15.6: reconnect is manual)."""
        self._paused.clear()
        self.wake()

    def is_paused(self):
        return self._paused.is_set()

    def shutdown(self, timeout=SHUTDOWN_JOIN_SECONDS):
        """Stop accepting new work and wait AT MOST `timeout` seconds for
        an in-flight request to finish - then return unconditionally
        (plan §18.3). Unsent items simply remain queued for next launch."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- main loop -------------------------------------------------------

    def _run(self):
        while True:
            self._wake.wait(timeout=FALLBACK_POLL_SECONDS)
            if self._stop.is_set():
                return
            self._wake.clear()
            try:
                self._process_all()
            except Exception:  # noqa: BLE001 - the sender must never crash the app
                pass

    def _process_all(self):
        if self._paused.is_set():
            return
        # NOT gated on credentials being present: Feedback's Beta-Code
        # fallback mode (plan §13.3) is sent unauthenticated and needs none
        # at all - only crash, telemetry, and installation_token-mode
        # feedback need a real installation. `_send_one` decides per item.
        credentials = self._get_credentials()

        for name, queue in (("crash", self._crash_queue), ("feedback", self._feedback_queue), ("telemetry", self._telemetry_queue)):
            while not self._stop.is_set():
                items = queue.list_pending(limit=1)
                if not items:
                    break
                item = items[0]
                outcome = self._send_one(name, queue, item, credentials)
                if outcome != "sent_or_dropped":
                    break  # retryable failure or pause - stop this queue, try again next wake

    def _send_one(self, queue_name, queue, item, credentials):
        import json as _json

        try:
            body = _json.loads(item.payload_json)
        except ValueError:
            queue.remove(item.id)  # unparseable local row - drop, nothing to retry
            return "sent_or_dropped"

        # The only unauthenticated mutation path in the whole system:
        # feedback items built via the Beta-Code fallback carry their own
        # auth_mode in the payload (schemas.build_feedback_submission) -
        # trust that, not whether credentials currently happen to exist,
        # since a beta_code-mode item must send even from a NEVER-registered
        # or disconnected installation (that is the whole point of the mode).
        needs_installation_auth = not (queue_name == "feedback" and body.get("auth_mode") == "beta_code")
        if needs_installation_auth and credentials is None:
            return "retry_later"  # no installation yet - not an error, just can't authenticate right now

        if queue_name == "telemetry":
            with telemetry_queue.SEND_LOCK:
                if not telemetry_queue.consent_given():
                    queue.remove(item.id)  # opted out after this was queued - never send it
                    return "sent_or_dropped"
                queue.record_attempt(item.id)
                result, _request_id = cloud_client.post_signed(_PATHS[queue_name], body, credentials)
        else:
            queue.record_attempt(item.id)
            if needs_installation_auth:
                result, _request_id = cloud_client.post_signed(_PATHS[queue_name], body, credentials)
            else:
                result = cloud_client.post_unsigned(_PATHS[queue_name], body)

        if result.ok:
            queue.remove(item.id)
            return "sent_or_dropped"

        if result.status == 401:
            self._paused.set()
            return "paused"

        if result.retryable:
            return "retry_later"

        # Permanent payload failure (400 non-auth / 413 / unknown event) -
        # plan §19: drop, safe local logging only, no infinite retry.
        print(f"StorageArch Cloud: dropping unsendable {queue_name} item (status={result.status})")
        queue.remove(item.id)
        return "sent_or_dropped"
