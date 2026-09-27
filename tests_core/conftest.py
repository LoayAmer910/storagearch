"""Test setup: the repository root on sys.path; every test gets its own cloud home and can never reach the
real cloud server."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from api_support import app  # noqa: E402,F401 - registers the `app` fixture for the API tests


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _never_the_real_cloud(monkeypatch):
    """No test may reach the real Cloudflare Worker: whenever the cloud client is loadable, its server address
    points at a closed local port unless a test installs its own local stand-in."""
    try:
        from local_cloud import cloud_client
    except Exception:  # noqa: BLE001 - the cloud client is not on the path for this test
        return
    monkeypatch.setattr(cloud_client, "BASE_URL", "http://127.0.0.1:9")


@pytest.fixture(autouse=True)
def _own_cloud_home(tmp_path, monkeypatch):
    """The cloud client keeps its state (credentials, queues, consent) in the user's profile; a test never
    touches the real one."""
    monkeypatch.setenv("STORAGEARCH_CLOUD_HOME", str(tmp_path / "cloud_home_default"))
