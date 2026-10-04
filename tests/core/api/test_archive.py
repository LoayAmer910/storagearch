"""core.api - archiving files and folders: results and reasons, duplicates, splitting, lookups."""
import concurrent.futures
import contextlib
import ctypes
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

import pytest

from core import api, api as main, config, store, strategies
from core.paths import ExtractionRefused, InvalidPathError, SourceInUseError
from support import corpus
from support.api import item_id_by_name, leftovers, read, tree
from support.files import code_folder, locked, write_lines_file


def _archives():
    return {row[2]: row for row in api.list_archives()}


def test_row_shapes_and_summary(app):
    src = app / "report.txt"
    src.write_bytes(corpus.lines(3000, "{i} {w} {n}\n"))
    assert api.archive_file_result(str(src), "Docs")["success"]
    item_id = item_id_by_name("report.txt")
    item = api.get_item(item_id)
    assert len(item) == 16 and item[2] == "file" and item[3] == "report.txt" and item[10] == "ARCHIVED"
    assert len(api.search("report")[0]) == 11 and len(api.list_archives()[0]) == 8
    assert len(api.get_archive(item[1])) == 12
    summary = api.dashboard_summary()
    assert summary["item_count"] == 1 and summary["total_original_size"] == os.path.getsize(src)
    assert summary["integrity_counts"]["VERIFIED"] == 1


def test_file_and_folder_hashes_match_what_is_archived(app):
    folder = app / "proj"
    (folder / "sub" / "empty").mkdir(parents=True)
    (folder / "sub" / "a.txt").write_bytes(corpus.lines(900, "{i} {w}\n"))
    (folder / "b.bin").write_bytes(os.urandom(5000))
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    assert api.find_duplicate(api.calculate_folder_hash(str(folder)))["status"] == "VERIFIED"
    src = app / "one.txt"
    src.write_bytes(corpus.lines(900, "{w} {n}\n"))
    assert api.archive_file_result(str(src), "Docs")["success"]
    assert api.find_duplicate(api.calculate_file_hash(str(src)))["item_name"] == "one.txt"
    assert api.find_duplicate("0" * 64) is None
    assert api.calculate_file_hash(str(app / "missing")) is None and api.calculate_folder_hash(str(app / "missing")) is None


def test_the_folder_hash_depends_on_content_structure_and_empty_folders(app):
    a = app / "a"
    (a / "x").mkdir(parents=True)
    (a / "f.txt").write_text("same")
    base = api.calculate_folder_hash(str(a))
    (a / "x" / "new").mkdir()
    assert api.calculate_folder_hash(str(a)) != base  # an empty folder is part of the content
    shutil.rmtree(a / "x" / "new")
    assert api.calculate_folder_hash(str(a)) == base
    (a / "f.txt").write_text("changed")
    assert api.calculate_folder_hash(str(a)) != base


def test_duplicates_of_files_and_compressible_folders_are_recognized(app):
    src = write_lines_file(app, "dup.txt")
    assert api.archive_file_result(src, "Docs")["reason"] == "OK"
    again = api.archive_file_result(src, "Other")
    assert again["success"] and again["reason"] == "DUPLICATE" and again["duplicate"]["status"] == "VERIFIED"
    folder = app / "f"
    write_lines_file(folder, "a.txt", seed=5)
    assert api.archive_folder_result(str(folder), "Docs")["reason"] == "OK"
    assert api.archive_folder_result(str(folder), "Docs")["reason"] == "DUPLICATE"  # failed in the old version


def test_a_duplicate_of_a_damaged_archive_is_refused(app):
    src = write_lines_file(app, "dup.txt")
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    data = bytearray(read(zip_path))
    data[len(data) // 3] ^= 0xFF
    with open(zip_path, "wb") as f:
        f.write(bytes(data))
    result = api.archive_file_result(src, "Docs")
    assert not result["success"] and result["reason"] == "DUPLICATE_UNTRUSTED"


def test_archives_split_at_the_size_limit_and_full_ones_are_sealed(app):
    api.create_category("Small", max_archive_size=40_000)
    for i in range(6):
        assert api.archive_file_result(write_lines_file(app, f"f{i}.bin", 2500, seed=i), "Small")["success"]
    archives = _archives()
    assert len(archives) >= 2
    with contextlib.closing(sqlite3.connect(config.db_path())) as db:
        sealed = db.execute("SELECT file_path, zip_hash FROM archives WHERE state = 'sealed'").fetchall()
    assert sealed and all(h and api.calculate_file_hash(p) == h for p, h in sealed)
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}
    for i in range(6):
        assert read(api.checkout(item_id_by_name(f"f{i}.bin"), destination=str(app / f"o{i}"))) == read(app / f"f{i}.bin")
    with open(sealed[0][0], "ab") as f:
        f.write(b"x")
    assert "CHANGED" in api.verify_category("Small").values()


def test_source_actions(app):
    kept = write_lines_file(app, "kept.txt")
    assert api.archive_file_result(kept, "Docs", "keep")["success"] and os.path.exists(kept)
    deleted = write_lines_file(app, "deleted.txt", seed=2)
    assert api.archive_file_result(deleted, "Docs", "delete")["success"] and not os.path.exists(deleted)
    folder = app / "gone"
    write_lines_file(folder, "a.txt", seed=3)
    assert api.archive_folder_result(str(folder), "Docs", "delete")["success"] and not folder.exists()
    assert read(api.checkout(item_id_by_name("deleted.txt"), destination=str(app / "o"))) != b""


def test_a_link_inside_a_folder_is_skipped_and_the_source_is_kept(app):
    outside = app / "outside"
    write_lines_file(outside, "secret.txt", seed=7)
    folder = app / "with_link"
    write_lines_file(folder, "own.txt", seed=8)
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(folder / "link"), str(outside)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    try:
        result = api.archive_folder_result(str(folder), "Docs", "delete")
        assert result["success"] and result["skipped_links"] and folder.exists() and (outside / "secret.txt").exists()
        restored = api.checkout(item_id_by_name("with_link", "folder"), destination=str(app / "o"))
        assert sorted(tree(restored)) == ["own.txt"]
    finally:
        if (folder / "link").exists():
            os.rmdir(folder / "link")


@pytest.mark.parametrize("args,reason", [
    (("", "Docs"), "INVALID_PATH"), (("C:/nope/missing.txt", "Docs"), "SOURCE_MISSING"),
    (("__FILE__", ""), "INVALID_CATEGORY"), (("__FILE__", "Docs", "shred"), "INVALID_SOURCE_ACTION"),
])
def test_bad_requests_get_a_reason(app, args, reason):
    src = write_lines_file(app, "ok.txt")
    args = tuple(src if a == "__FILE__" else a for a in args)
    result = api.archive_file_result(*args)
    assert not result["success"] and result["reason"] == reason and result["message"]
    assert api.archive_folder_result(src, "Docs")["reason"] == "SOURCE_MISSING"  # a file is not a folder


def test_empty_files_empty_folders_and_unicode_names(app):
    # Hebrew on purpose: non-ASCII, right-to-left names must archive and restore exactly
    empty = app / "ריק.txt"
    empty.write_bytes(b"")
    assert api.archive_file_result(str(empty), "מסמכים")["success"]
    folder = app / "תיקייה ריקה"
    (folder / "תת").mkdir(parents=True)
    assert api.archive_folder_result(str(folder), "מסמכים")["success"]
    assert read(api.checkout(item_id_by_name("ריק.txt"), destination=str(app / "o1"))) == b""
    assert tree(api.checkout(item_id_by_name("תיקייה ריקה", "folder"), destination=str(app / "o2"))) == {"תת": None}


def test_many_archives_at_once_stay_consistent(app):
    files = [write_lines_file(app / "many", f"f{i}.txt", 1500, seed=i) for i in range(16)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: api.archive_file_result(p, "Docs"), files))
    assert all(r["success"] and r["reason"] == "OK" for r in results), [r for r in results if not r["success"]]
    assert api.dashboard_summary()["item_count"] == 16
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}
    left = leftovers()
    assert left["temp_entries"] == [] and left["children"] == []


def test_a_folder_of_small_files_round_trips_and_stores_what_the_file_form_gives(app, tmp_path):
    folder = code_folder(str(app / "proj"))
    assert api.archive_folder_result(folder, "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    members = [m for m in api._cat().members_of_item(item_id) if m["kind"] == "file"]
    smart = [m for m in members if m["method"] not in store.BASELINE_METHODS]
    assert len(smart) > len(members) // 2
    zip_path = api.list_archives()[0][3]
    with zipfile.ZipFile(zip_path) as z:
        for m in smart:
            src = os.path.join(app, *m["relative_path"].split("/"))
            work = tempfile.mkdtemp(dir=tmp_path)
            assert z.read(m["path_in_archive"]) == read(strategies.get(m["method"]).compress(src, work)), m["relative_path"]
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(app / "proj")
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}
    assert leftovers()["temp_files"] == []


def test_archive_file_result_invalid_path():
    result = main.archive_file_result("", "Docs")
    assert result["success"] is False
    assert result["reason"] == "INVALID_PATH"
    assert result["message"]


def test_archive_file_result_source_missing(app):
    main.create_category("Docs")
    result = main.archive_file_result("does_not_exist.txt", "Docs")
    assert result["success"] is False
    assert result["reason"] == "SOURCE_MISSING"


def test_archive_file_result_protected_path(app):
    # a fabricated, non-existent path under a protected root - the guard triggers on the
    # path string alone, before any filesystem access, so nothing real is ever touched.
    fake_protected_path = r"C:\Windows\System32\storagearch_test_fixture_does_not_exist.txt"
    result = main.archive_file_result(fake_protected_path, "Docs")
    assert result["success"] is False
    assert result["reason"] == "PROTECTED_PATH"


def test_archive_file_result_insufficient_space(app, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("big.txt", "content")
    monkeypatch.setattr(main, "_enough_space", lambda *a, **k: False)  # the new core's space check
    result = main.archive_file_result(path, "Docs")
    assert result["success"] is False
    assert result["reason"] == "INSUFFICIENT_SPACE"


def test_archive_file_result_duplicate(app, make_file):
    main.create_category("Docs")
    path1 = make_file("first.txt", "same content")
    path2 = make_file("second.txt", "same content")
    assert main.archive_file(path1, "Docs") is True

    result = main.archive_file_result(path2, "Docs")
    assert result["success"] is True
    assert result["reason"] == "DUPLICATE"
    assert result["duplicate"]["item_name"] == "first.txt"


@pytest.mark.parametrize("error, reason", [
    (store.SourceChanged("changed while it was being archived"), "SOURCE_CHANGED"),
    (store.StoreError("internal detail"), "WRITE_FAILED"),
    (ExtractionRefused("internal detail"), "WRITE_FAILED"),
    (sqlite3.OperationalError("internal detail"), "DATABASE_ERROR"),
    (PermissionError("internal detail"), "PERMISSION_ERROR"),
    (SourceInUseError("C:/busy.txt", "used by another program"), "SOURCE_IN_USE"),
    (InvalidPathError("Cannot read it"), "INVALID_PATH"),
    (OSError("internal detail"), "OS_ERROR"),
    (RuntimeError("internal detail"), "UNEXPECTED_ERROR"),
], ids=lambda v: v if isinstance(v, str) else type(v).__name__)
def test_every_failure_inside_an_archive_becomes_a_reason_never_a_traceback(app, monkeypatch, make_file, error, reason):
    main.create_category("Docs")
    path = make_file("boom.txt", "content")

    def _raise(*args, **kwargs):
        raise error

    monkeypatch.setattr(api.store, "write_members", _raise)  # fails while writing to the archive
    result = main.archive_file_result(path, "Docs")  # must not raise
    assert result["success"] is False and result["reason"] == reason
    assert result["message"] and "internal detail" not in result["message"]  # internals are logged, not shown
    if reason == "SOURCE_IN_USE":
        assert result["path"] == "C:/busy.txt"  # the window names the file to close
    assert main.search("") == []


def test_archive_folder_result_source_missing(app):
    main.create_category("Docs")
    result = main.archive_folder_result("no_such_folder", "Docs")
    assert result["success"] is False
    assert result["reason"] == "SOURCE_MISSING"


def test_a_cloud_file_that_cannot_be_read_is_explained_as_a_cloud_file(app, make_file):
    path = make_file("online_only.docx", "content")
    assert ctypes.windll.kernel32.SetFileAttributesW(path, 0x1000)  # FILE_ATTRIBUTE_OFFLINE: not on this device
    with locked(path):  # the download cannot happen, so reading fails
        result = main.archive_file_result(path, "Docs")
    assert result["success"] is False and result["reason"] == "INVALID_PATH"
    assert "has not been downloaded to this device" in result["message"]


def test_when_free_space_cannot_be_checked_nothing_is_archived(app, monkeypatch, make_file):
    path = make_file("a.txt", "content")

    def no_answer(_path):
        raise OSError("the drive does not answer")

    monkeypatch.setattr(api.shutil, "disk_usage", no_answer)
    assert main.archive_file_result(path, "Docs")["reason"] == "INSUFFICIENT_SPACE"
    assert main.search("") == []


def test_the_recycle_action_sends_the_source_to_the_recycle_bin(app, monkeypatch, make_file):
    path = make_file("a.txt", "content")
    recycled = []
    fake = type(sys)("send2trash")
    fake.send2trash = recycled.append  # never the real Recycle Bin from a test
    monkeypatch.setitem(sys.modules, "send2trash", fake)
    assert main.archive_file_result(path, "Docs", source_action="recycle")["success"]
    assert recycled == [path]


def test_a_folder_with_a_file_held_by_another_program_names_that_file(app):
    folder = app / "project"
    folder.mkdir()
    (folder / "ok.txt").write_text("fine")
    busy = folder / "busy.log"
    busy.write_text("held open")
    with locked(busy):
        result = main.archive_folder_result(str(folder), "Docs")
    assert result["reason"] == "SOURCE_IN_USE" and result["path"] == str(busy)
    assert main.archive_folder(str(folder), "Docs") is True  # once it is released the folder archives


def test_a_folder_is_not_archived_without_enough_free_space(app, monkeypatch):
    folder = app / "project"
    folder.mkdir()
    (folder / "a.txt").write_text("content")
    monkeypatch.setattr(main, "_enough_space", lambda *a, **k: False)
    assert main.archive_folder_result(str(folder), "Docs")["reason"] == "INSUFFICIENT_SPACE"
    assert main.search("") == []


def test_a_folder_with_skipped_entries_is_kept_even_when_deletion_was_asked(app):
    folder = app / "project"
    (folder / "inner").mkdir(parents=True)
    (folder / "inner" / "a.txt").write_text("content")
    outside = app / "outside"
    outside.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(folder / "jump"), str(outside)], check=True, capture_output=True)
    result = main.archive_folder_result(str(folder), "Docs", source_action="delete")
    assert result["success"] and "The source was kept" in result["message"]
    assert (folder / "inner" / "a.txt").exists()


def test_a_source_folder_that_cannot_be_removed_is_reported_in_the_message(app):
    folder = app / "project"
    folder.mkdir()
    protected = folder / "a.txt"
    protected.write_text("content")
    os.chmod(protected, stat.S_IREAD)  # a read-only file: archiving reads it, deleting it is refused
    try:
        result = main.archive_folder_result(str(folder), "Docs", source_action="delete")
        assert result["success"] and result["message"].endswith("(the source could not be removed)")
        assert protected.exists()
    finally:
        os.chmod(protected, stat.S_IREAD | stat.S_IWRITE)


def test_hashing_a_file_held_by_another_program_names_that_file(app, make_file):
    path = make_file("busy.txt", "content")
    with locked(path), pytest.raises(SourceInUseError) as raised:
        main.calculate_file_hash(path)
    assert raised.value.path == path


def test_a_source_file_that_cannot_be_removed_is_reported_in_the_message(app, make_file):
    path = make_file("read_only.txt", "content")
    os.chmod(path, stat.S_IREAD)
    try:
        result = main.archive_file_result(path, "Docs", source_action="delete")
        assert result["success"] and result["message"].endswith("(the source could not be removed)")
        assert os.path.exists(path)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
