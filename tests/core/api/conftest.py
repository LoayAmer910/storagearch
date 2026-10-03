"""Fixtures for the API tests that create their own files: `sandbox` is the fresh installation's folder."""
import pytest


@pytest.fixture
def sandbox(app):
    return app


@pytest.fixture
def make_file(sandbox):
    def _make_file(relative_path, content="hello storagearch"):
        full_path = sandbox / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return str(full_path)
    return _make_file
