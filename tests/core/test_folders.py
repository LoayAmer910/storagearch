"""core.folders - reading a folder as an item: what is listed, what is skipped and why, names that cannot be
restored, and the folder's content hash."""
import ctypes
import os
import subprocess

import pytest

from core import folders, paths
from core.catalog import EMPTY_DIGEST

FILE_ATTRIBUTE_OFFLINE = 0x1000


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_files_and_empty_folders_are_listed_with_relative_paths(tmp_path):
    _write(tmp_path / "b.txt")
    _write(tmp_path / "sub" / "a.txt")
    (tmp_path / "empty").mkdir()
    entries, skipped = folders.scan(str(tmp_path))
    assert [(e.rel_path, e.kind) for e in entries] == [
        ("empty", "dir"), ("sub", "dir"), ("b.txt", "file"), ("sub/a.txt", "file")]
    assert skipped == []


def test_a_cloud_file_that_is_not_downloaded_is_skipped_not_read(tmp_path):
    placeholder = tmp_path / "online_only.docx"
    _write(placeholder)
    assert ctypes.windll.kernel32.SetFileAttributesW(str(placeholder), FILE_ATTRIBUTE_OFFLINE)
    entries, skipped = folders.scan(str(tmp_path))
    assert entries == []
    assert [(s.rel_path, s.reason) for s in skipped] == [("online_only.docx", "cloud")]


def test_a_linked_folder_is_skipped_and_never_followed(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "secret.txt")
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jump"), str(outside)], check=True, capture_output=True)
    entries, skipped = folders.scan(str(root))
    assert entries == []
    assert [(s.rel_path, s.reason) for s in skipped] == [("jump", "link")]


def test_a_linked_file_is_skipped(tmp_path):
    _write(tmp_path / "real.txt")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(tmp_path / "real.txt", root / "link.txt")
    except OSError:
        pytest.skip("creating a file link needs Developer Mode or administrator rights")
    entries, skipped = folders.scan(str(root))
    assert entries == []
    assert [(s.rel_path, s.reason) for s in skipped] == [("link.txt", "link")]


def test_a_name_windows_cannot_recreate_is_an_error_not_a_silent_skip(tmp_path):
    # The \\?\ prefix lets a program create "report." - a name Explorer and a normal restore cannot make again.
    with open("\\\\?\\" + str(tmp_path / "report."), "wb") as f:
        f.write(b"x")
    with pytest.raises(paths.InvalidPathError, match="ends with a space or a dot"):
        folders.scan(str(tmp_path))


def test_the_folder_hash_depends_on_content_and_names_not_on_order():
    rows = [("a.txt", "file", "1" * 64), ("d", "dir", EMPTY_DIGEST)]
    assert folders.digest(rows) == folders.digest(list(reversed(rows)))
    assert folders.digest(rows) != folders.digest([("a.txt", "file", "2" * 64), ("d", "dir", EMPTY_DIGEST)])
    assert folders.digest(rows) != folders.digest([("b.txt", "file", "1" * 64), ("d", "dir", EMPTY_DIGEST)])
    # length-prefixed: moving characters between fields can never give the same hash
    assert folders.digest([("ab", "file", "c")]) != folders.digest([("a", "bfile", "c")])


def test_folder_hash_uses_the_given_file_hash_for_every_file(tmp_path):
    _write(tmp_path / "a.txt", b"one")
    (tmp_path / "empty").mkdir()
    seen = []

    def file_hash(path):
        seen.append(os.path.basename(path))
        return "f" * 64

    assert folders.folder_hash(str(tmp_path), file_hash) == folders.digest(
        [("a.txt", "file", "f" * 64), ("empty", "dir", EMPTY_DIGEST)])
    assert seen == ["a.txt"]
