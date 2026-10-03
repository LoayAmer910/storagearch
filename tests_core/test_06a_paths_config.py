"""Step 6a - paths and configuration (I7). Every stored name that could escape its destination is refused
with ExtractionRefused and nothing else - including other-drive names that made the old checkout crash;
a fuzz of random names never lands outside; protection follows junctions and does not assume C:; the
config does not move when the working directory changes."""
import os
import random
import subprocess

import pytest

from core import config, paths
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


def test_config_defaults_relative_values_and_bad_files(tmp_path):
    assert config.load(str(tmp_path))["data_root"] == str(tmp_path / "SmartArchiveData")
    (tmp_path / config.CONFIG_FILE).write_text('{"data_root": "MyData", "workspace_root": "   "}', encoding="utf-8")
    state = config.load(str(tmp_path))
    assert state == {"data_root": str(tmp_path / "MyData"), "workspace_root": None}
    for garbage in ("{not json", "[1, 2]", '{"data_root": 5}', "\xff\xfe"):
        (tmp_path / config.CONFIG_FILE).write_bytes(garbage.encode("latin-1"))
        assert config.load(str(tmp_path))["data_root"] == str(tmp_path / "SmartArchiveData"), garbage


def test_config_save_round_trips_and_survives_a_change_of_working_directory(tmp_path, monkeypatch):
    config.load(str(tmp_path))
    config.save(data_root=str(tmp_path / "D"), workspace_root=str(tmp_path / "W"))
    assert not os.path.exists(str(tmp_path / config.CONFIG_FILE) + ".tmp")
    monkeypatch.chdir(tmp_path.parent)
    assert config.data_root() == str(tmp_path / "D") and config.workspace_dir() == str(tmp_path / "W")
    config.save(workspace_root="")
    assert config.workspace_dir() == str(tmp_path / "D" / "workspace")
    assert config.load(str(tmp_path)) == {"data_root": str(tmp_path / "D"), "workspace_root": None}
