"""Fixtures for the core tests: one compressible input file and its content info."""
import pytest

from core import content
from support import corpus


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "input.txt"
    path.write_bytes(corpus.lines(3000, "row {i} {w} {n} payload\n"))
    return str(path)


@pytest.fixture
def info(src):
    return content.classify(src)
