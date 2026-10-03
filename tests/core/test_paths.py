"""core.paths - stored names can never escape their destination; protected locations; cloud files that are not downloaded."""
import ctypes
import os
import random
import subprocess

import pytest

from core import paths
from core.paths import ExtractionRefused


ESCAPING = ["../x.txt", "a/../../x.txt", "a/..", "..", ".", "a/./b", "a\\..\\x.txt", "a\\b.txt", "/abs.txt",
            "C:/x.txt", "Y:x.txt", "a/Y:x.txt", "a/x.txt:stream", "CON", "a/nul.txt", "a/COM1.log", "a/lpt9",
            "a/b.", "a/b ", "", "a//b", "a/\x00b", "\\\\server\\share\\x", "a/b<c", "a/|", "a/?"]


# Hebrew on purpose: non-ASCII, right-to-left names must archive and restore exactly
FINE = ["a.txt", "a/b/c.txt", "..leading.txt", "a..b", "trailing...txt.x", "name with spaces.txt", "שם/קובץ.txt",
        "a/b/", "console.txt", "x" * 100]


@pytest.mark.parametrize("name", ESCAPING)
def test_escaping_or_invalid_stored_names_are_refused_cleanly(name, tmp_path):
    with pytest.raises(ExtractionRefused):
        paths.safe_target(str(tmp_path / "dest"), name)


@pytest.mark.parametrize("name", FINE)
def test_ordinary_names_are_placed_inside(name, tmp_path):
    dest = tmp_path / "dest"
    target = paths.safe_target(str(dest), name)
    assert os.path.commonpath([target, os.path.realpath(dest)]) == os.path.realpath(dest)


def test_random_names_never_escape_and_never_raise_anything_else(tmp_path):
    rng = random.Random(4)
    alphabet = list("ab./\\:*?\"<>| \x00\x1fCONUL..") + ["..", "/..", "C:", "\\\\"]
    dest = os.path.realpath(tmp_path / "dest")
    for _ in range(20000):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        try:
            target = paths.safe_target(str(tmp_path / "dest"), name)
        except ExtractionRefused:
            continue
        assert os.path.commonpath([target, dest]) == dest and target != dest, name


@pytest.mark.parametrize("raw,expected", [
    ("report.docx", "report.docx"), ("../../escape.txt", "escape.txt"), ("C:\\Windows\\evil.dll", "evil.dll"),
    ("a/b/", "b"), ("..", "item"), ("", "item"), (None, "item"), ("CON", "_CON"), ("x.txt.", "x.txt"),
    ("a<b>.txt", "a_b_.txt"), ("  ", "item"),
])
def test_bare_name_reduces_anything_to_one_safe_component(raw, expected):
    assert paths.bare_name(raw) == expected
    assert paths.name_problem(paths.bare_name(raw)) is None


def test_source_validation(tmp_path):
    good = tmp_path / "ok.txt"
    good.write_text("x")
    assert paths.validate_source_path(str(good)) == (True, None)
    assert paths.validate_source_path("relative.txt")[0] is False
    assert paths.validate_source_path(str(tmp_path / "missing"))[0] is False
    assert paths.validate_source_path("C:\\" + "a" * 300)[0] is False
    assert paths.validate_source_path("")[0] is False


def test_protection_comes_from_the_system_not_from_a_hard_coded_drive(monkeypatch, tmp_path):
    fake_windows = tmp_path / "OtherDrive" / "Windows"
    (fake_windows / "System32").mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(fake_windows))
    assert paths.is_protected(str(fake_windows / "System32" / "x.dll"))
    assert paths.is_protected(str(fake_windows).upper())
    assert not paths.is_protected(str(tmp_path / "OtherDrive" / "Users"))
    assert paths.is_protected(os.environ.get("ProgramFiles", "C:\\Program Files"))


def test_protection_follows_a_junction(tmp_path):
    guarded = tmp_path / "guarded"
    (guarded / "inner").mkdir(parents=True)
    link = tmp_path / "innocent_looking"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(guarded)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    try:
        assert paths.is_protected(str(link / "inner"), extra_roots=[str(guarded)])
        assert not paths.is_protected(str(tmp_path / "elsewhere"), extra_roots=[str(guarded)])
    finally:
        os.rmdir(link)  # removes the link only


def test_only_a_cloud_file_that_is_not_downloaded_counts_as_a_placeholder(tmp_path):
    normal = tmp_path / "normal.txt"
    normal.write_bytes(b"x")
    online_only = tmp_path / "online_only.txt"
    online_only.write_bytes(b"x")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(online_only), 0x1000)  # FILE_ATTRIBUTE_OFFLINE
    assert paths.is_cloud_placeholder(online_only) is True
    assert paths.is_cloud_placeholder(normal) is False
    # Attributes that cannot be read (no such file) are "unknown", never "cloud"
    assert paths.is_cloud_placeholder(tmp_path / "no_such_file.txt") is False
