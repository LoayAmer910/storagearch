"""Fixtures for every test. Every test gets its own cloud home and can never reach the real cloud server.
(pyproject.toml puts the repository root and tests/ on sys.path, so `core` and `support` import directly.)"""
import pytest

from core.scope import OperationScope
from support import corpus
from support.api import app  # noqa: F401 - registers the `app` fixture: a fresh installation in tmp_path


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


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    """One file of every kind the core tells apart, plus the traps (built fresh for each test module)."""
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


@pytest.fixture
def scope(tmp_path):
    """An operation scope with its own temporary folder."""
    with OperationScope(temp_root=str(tmp_path / "scopes")) as s:
        yield s
