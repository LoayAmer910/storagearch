"""Helpers for the cloud tests (a module name no other test folder uses)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class FakeWorker:
    """Records every request. `mode`: ok | error500 | unauthorized | slow | garbage | reject_code."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.lock = threading.Lock()
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with worker.lock:
                    worker.requests.append({"method": method, "path": self.path, "headers": dict(self.headers),
                                            "body": json.loads(body) if body else None})
                mode = worker.mode
                if mode == "slow":
                    time.sleep(7)  # longer than the client's 5 s timeout
                if mode == "error500":
                    return self._reply(500, {"error": "internal"})
                if mode == "unauthorized":
                    return self._reply(401, {"error": "unauthorized"})
                if mode == "garbage":
                    return self._reply(200, b"\x00\xff not json at all")
                if self.path == "/api/v1/installations/register":
                    code = (json.loads(body) if body else {}).get("beta_code")
                    if mode == "reject_code" or code != "VALID-CODE":
                        return self._reply(400, {"error": "invalid_beta_code"})
                    return self._reply(201, {"installation_id": "11111111-2222-4333-8444-555555555555",
                                             "installation_token": "t" * 40, "request_signing_secret": "ab" * 32})
                if self.path in ("/api/v1/config", "/api/v1/releases/latest"):
                    return self._reply(200, {"config": {"config_version": 99}, "signature": "not-a-signature"})
                return self._reply(200, {"accepted": True})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self, path):
        with self.lock:
            return [r for r in self.requests if r["path"] == path]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
