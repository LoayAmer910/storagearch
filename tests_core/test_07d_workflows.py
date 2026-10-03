"""Step 7d - whole workflows through the API (what the old suite covered, plus every defect fixed on the
way): checkout / edit / checkin of files and folders, duplicates, archive splitting and sealing, moving
out of a sealed archive, compaction, source actions, links, bad input, crash recovery, concurrency."""
import concurrent.futures
import json
import os
import shutil
import subprocess
import zipfile

import pytest

import corpus
from api_support import item_id_by_name, leftovers, read, tree
from core import api, config


def _file(folder, name, lines=3000, seed=0):
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(corpus.lines(lines, f"line {{i}} {seed} {{w}} {{n}}\n", seed=seed))
    return path


def _archives():
    return {row[2]: row for row in api.list_archives()}


def test_checkout_edit_checkin_of_a_file(app):
    src = _file(app, "notes.txt")
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    ws = api.checkout(item_id)
    assert read(ws) == read(src) and api.get_item(item_id)[10] == "CHECKED_OUT"
    assert api.checkout(item_id) == ws  # already checked out: same copy
    with open(ws, "ab") as f:
        f.write(b"edited line\n")
    edited = read(ws)
    assert api.checkin(item_id) and not os.path.exists(ws)
    assert api.get_item(item_id)[10] == "ARCHIVED"
    assert api.get_item(item_id)[8] != api.calculate_file_hash(src)  # the item now holds the edited content
    assert read(api.checkout(item_id, destination=str(app / "back"))) == edited
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_an_unchanged_checkin_does_not_touch_the_archive(app):
    src = _file(app, "same.txt")
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    before = read(zip_path)
    item_id = item_id_by_name("same.txt")
    ws = api.checkout(item_id)
    assert api.checkin(item_id) and not os.path.exists(ws)
    assert read(zip_path) == before


def test_checkout_edit_checkin_of_a_folder_with_added_removed_and_empty_entries(app):
    folder = app / "proj"
    _file(folder, "keep.txt", seed=1)
    _file(folder, "remove.txt", seed=2)
    _file(folder, "sub/change.txt", seed=3)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    ws = api.checkout(item_id)
    assert tree(ws) == tree(folder)
    os.remove(os.path.join(ws, "remove.txt"))
    _file(ws, "added/new.txt", seed=4)
    os.makedirs(os.path.join(ws, "added", "empty"))
    with open(os.path.join(ws, "sub", "change.txt"), "ab") as f:
        f.write(b"changed\n")
    expected = tree(ws)
    assert api.checkin(item_id)
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def _written_files(monkeypatch):
    """Records the relative path of every file member any write puts into an archive."""
    from core import store
    written, real = [], store.write_members

    def spy(zip_path, planned):
        written.extend(m.relative_path for m in planned if m.kind == "file")
        return real(zip_path, planned)
    monkeypatch.setattr(store, "write_members", spy)
    return written


def _folder_of(app, name, count=8):
    folder = app / name
    for i in range(count):
        _file(folder, f"sub{i % 3}/f{i}.txt", 2000, seed=i)
    os.makedirs(folder / "empty")
    return folder


def test_a_folder_checkin_writes_only_the_files_that_changed(app, monkeypatch):
    folder = _folder_of(app, "proj")
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    archive_id = api.get_item(item_id)[1]
    old = {m["relative_path"]: m for m in api._cat().members_of_item(item_id)}
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "sub1", "f4.txt"), "ab") as f:
        f.write(b"changed\n")
    _file(ws, "added.txt", seed=99)
    os.remove(os.path.join(ws, "sub2", "f5.txt"))
    expected = tree(ws)
    sizes = sum(os.path.getsize(os.path.join(d, n)) for d, _s, files in os.walk(ws) for n in files)
    written = _written_files(monkeypatch)
    assert api.checkin(item_id)
    assert sorted(written) == ["proj/added.txt", "proj/sub1/f4.txt"]
    assert api._cat().archive_state(archive_id)["dead_bytes"] == (
        old["proj/sub1/f4.txt"]["stored_size"] + old["proj/sub2/f5.txt"]["stored_size"])  # only these became dead
    item, members = api.get_item(item_id), api._cat().members_of_item(item_id)
    assert item[6] == sizes == sum(m["original_size"] for m in members)
    assert item[7] == sum(m["stored_size"] for m in members)
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_a_file_changed_and_changed_back_is_written_again(app, monkeypatch):
    folder = _folder_of(app, "proj")
    api.archive_folder_result(str(folder), "Docs")
    item_id = item_id_by_name("proj", "folder")
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "sub0", "f0.txt"), "ab") as f:
        f.write(b"first edit\n")
    assert api.checkin(item_id)
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "sub0", "f0.txt"), "wb") as f:
        f.write(read(folder / "sub0" / "f0.txt"))  # back to the original - which differs from the checked-in edit
    with open(os.path.join(ws, "sub1", "f1.txt"), "ab") as f:
        f.write(b"other edit\n")
    expected = tree(ws)
    written = _written_files(monkeypatch)
    assert api.checkin(item_id)
    assert sorted(written) == ["proj/sub0/f0.txt", "proj/sub1/f1.txt"]
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_a_folder_checkin_that_only_removes_entries_writes_nothing(app, monkeypatch):
    folder = _folder_of(app, "proj")
    api.archive_folder_result(str(folder), "Docs")
    item_id = item_id_by_name("proj", "folder")
    ws = api.checkout(item_id)
    os.remove(os.path.join(ws, "sub0", "f3.txt"))
    shutil.rmtree(os.path.join(ws, "empty"))
    expected = tree(ws)
    written = _written_files(monkeypatch)
    assert api.checkin(item_id)
    assert written == []
    back = api.checkout(item_id, destination=str(app / "back"))
    assert tree(back) == expected and not os.path.exists(os.path.join(back, "empty"))
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_a_folder_moving_out_of_a_sealed_archive_takes_every_file_along(app, monkeypatch):
    api.create_category("Small", max_archive_size=60_000)
    folder = _folder_of(app, "proj", count=4)
    assert api.archive_folder_result(str(folder), "Small")["success"]
    item_id = item_id_by_name("proj", "folder")
    for i in range(20):  # fill the archive until it is sealed
        if api._cat().archive_state(api.get_item(item_id)[1])["state"] == "sealed":
            break
        api.archive_file_result(_file(app, f"filler{i}.txt", 3000, seed=50 + i), "Small")
    assert api._cat().archive_state(api.get_item(item_id)[1])["state"] == "sealed"
    sealed_path = api.get_archive(api.get_item(item_id)[1])[3]
    sealed_bytes = read(sealed_path)
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "sub0", "f0.txt"), "ab") as f:
        f.write(b"edit\n")
    expected = tree(ws)
    written = _written_files(monkeypatch)
    assert api.checkin(item_id)
    assert len(written) == 4 and read(sealed_path) == sealed_bytes
    assert api.get_archive(api.get_item(item_id)[1])[3] != sealed_path
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}


def test_a_folder_that_outgrows_its_open_archive_takes_every_file_along(app, monkeypatch):
    api.create_category("Small", max_archive_size=200_000)
    folder = _folder_of(app, "proj", count=4)
    assert api.archive_folder_result(str(folder), "Small")["success"]
    item_id = item_id_by_name("proj", "folder")
    old_archive = api.get_item(item_id)[1]
    assert api._cat().archive_state(old_archive)["state"] == "open"
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "big.bin"), "wb") as f:
        f.write(os.urandom(190_000))  # does not fit in what is left of the open archive
    expected = tree(ws)
    written = _written_files(monkeypatch)
    assert api.checkin(item_id)
    assert api.get_item(item_id)[1] != old_archive
    assert len(written) == 5  # the unchanged files moved along with the new one
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}


def test_kept_files_survive_many_edits_and_compaction(app):
    folder = _folder_of(app, "proj", count=6)
    _file(folder, "big.txt", 30000, seed=77)
    api.archive_folder_result(str(folder), "Docs")
    item_id = item_id_by_name("proj", "folder")
    zip_path = api.list_archives()[0][3]
    for i in range(6):
        ws = api.checkout(item_id)
        with open(os.path.join(ws, "big.txt"), "ab") as f:
            f.write(f"edit {i}\n".encode())
        expected = tree(ws)
        assert api.checkin(item_id)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    assert len([n for n in names if n.endswith("big.txt") or n.endswith("big.txt.sa")]) <= 3, "never reclaimed"
    assert len([n for n in names if n.endswith("f0.txt") or n.endswith("f0.txt.sa")]) == 1  # kept, never rewritten
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_two_items_with_the_same_name_do_not_overwrite_each_other_in_the_workspace(app):
    a = _file(app / "a", "report.txt", seed=1)
    b = _file(app / "b", "report.txt", seed=2)
    api.archive_file_result(a, "Docs")
    api.archive_file_result(b, "Docs")
    ids = [row[0] for row in api.search("report.txt")]
    paths = [api.checkout(i) for i in ids]
    assert len(set(paths)) == 2 and sorted(read(p) for p in paths) == sorted([read(a), read(b)])


def test_checkout_never_overwrites_an_existing_file_at_an_explicit_destination(app):
    src = _file(app, "x.txt")
    api.archive_file_result(src, "Docs")
    dest = app / "dest"
    dest.mkdir()
    (dest / "x.txt").write_text("the user's own file")
    assert api.checkout(item_id_by_name("x.txt"), destination=str(dest)) is None
    assert (dest / "x.txt").read_text() == "the user's own file"


def test_checkin_refuses_what_it_cannot_do(app):
    src = _file(app, "x.txt")
    api.archive_file_result(src, "Docs")
    item_id = item_id_by_name("x.txt")
    assert not api.checkin(item_id)  # not checked out
    assert not api.checkin(999999)
    assert api.checkout(999999) is None


def test_a_deleted_checked_out_copy_returns_the_item_to_its_archived_version(app):
    src = _file(app, "x.txt")
    api.archive_file_result(src, "Docs")
    item_id = item_id_by_name("x.txt")
    archive_before = open(api.get_archive(api.get_item(item_id)[1])[3], "rb").read()
    ws = api.checkout(item_id)
    os.remove(ws)
    assert api.checkin_result(item_id) == {"success": True, "reason": "WORKSPACE_COPY_MISSING"}
    assert api.get_item(item_id)[10] == "ARCHIVED" and api.items_pending_return() == []
    assert open(api.get_archive(api.get_item(item_id)[1])[3], "rb").read() == archive_before, "the archive was touched"
    back = api.checkout(item_id)
    assert open(back, "rb").read() == open(src, "rb").read()


def test_duplicates_of_files_and_compressible_folders_are_recognized(app):
    src = _file(app, "dup.txt")
    assert api.archive_file_result(src, "Docs")["reason"] == "OK"
    again = api.archive_file_result(src, "Other")
    assert again["success"] and again["reason"] == "DUPLICATE" and again["duplicate"]["status"] == "VERIFIED"
    folder = app / "f"
    _file(folder, "a.txt", seed=5)
    assert api.archive_folder_result(str(folder), "Docs")["reason"] == "OK"
    assert api.archive_folder_result(str(folder), "Docs")["reason"] == "DUPLICATE"  # failed in the old version


def test_a_duplicate_of_a_damaged_archive_is_refused(app):
    src = _file(app, "dup.txt")
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    data = bytearray(read(zip_path))
    data[len(data) // 3] ^= 0xFF
    open(zip_path, "wb").write(bytes(data))
    result = api.archive_file_result(src, "Docs")
    assert not result["success"] and result["reason"] == "DUPLICATE_UNTRUSTED"


def test_archives_split_at_the_size_limit_and_full_ones_are_sealed(app):
    api.create_category("Small", max_archive_size=40_000)
    for i in range(6):
        assert api.archive_file_result(_file(app, f"f{i}.bin", 2500, seed=i), "Small")["success"]
    archives = _archives()
    assert len(archives) >= 2
    with __import__("sqlite3").connect(config.db_path()) as db:
        sealed = db.execute("SELECT file_path, zip_hash FROM archives WHERE state = 'sealed'").fetchall()
    assert sealed and all(h and api.calculate_file_hash(p) == h for p, h in sealed)
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}
    for i in range(6):
        assert read(api.checkout(item_id_by_name(f"f{i}.bin"), destination=str(app / f"o{i}"))) == read(app / f"f{i}.bin")
    with open(sealed[0][0], "ab") as f:
        f.write(b"x")
    assert "CHANGED" in api.verify_category("Small").values()


def test_an_item_in_a_sealed_archive_moves_on_checkin(app):
    api.create_category("Small", max_archive_size=30_000)
    first = _file(app, "first.txt", 2500, seed=1)
    api.archive_file_result(first, "Small")
    api.archive_file_result(_file(app, "second.txt", 2500, seed=2), "Small")
    item_id = item_id_by_name("first.txt")
    sealed_path = api.get_archive(api.get_item(item_id)[1])[3]
    sealed_bytes = read(sealed_path)
    ws = api.checkout(item_id)
    with open(ws, "ab") as f:
        f.write(b"edit\n")
    edited = read(ws)
    assert api.checkin(item_id)
    assert api.get_archive(api.get_item(item_id)[1])[3] != sealed_path and read(sealed_path) == sealed_bytes
    assert read(api.checkout(item_id, destination=str(app / "back"))) == edited
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}


def test_repeated_edits_do_not_grow_the_archive_without_bound(app):
    src = _file(app, "big.txt", 20000, seed=9)
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    one = os.path.getsize(zip_path)
    item_id = item_id_by_name("big.txt")
    for i in range(6):
        ws = api.checkout(item_id)
        with open(ws, "ab") as f:
            f.write(f"edit {i}\n".encode())
        assert api.checkin(item_id)
    assert os.path.getsize(zip_path) < 2.5 * one, "replaced versions were never reclaimed"
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}
    with zipfile.ZipFile(zip_path) as z:
        assert len([n for n in z.namelist() if not n.endswith("/")]) <= 2


def test_source_actions(app):
    kept = _file(app, "kept.txt")
    assert api.archive_file_result(kept, "Docs", "keep")["success"] and os.path.exists(kept)
    deleted = _file(app, "deleted.txt", seed=2)
    assert api.archive_file_result(deleted, "Docs", "delete")["success"] and not os.path.exists(deleted)
    folder = app / "gone"
    _file(folder, "a.txt", seed=3)
    assert api.archive_folder_result(str(folder), "Docs", "delete")["success"] and not folder.exists()
    assert read(api.checkout(item_id_by_name("deleted.txt"), destination=str(app / "o"))) != b""


def test_a_link_inside_a_folder_is_skipped_and_the_source_is_kept(app):
    outside = app / "outside"
    _file(outside, "secret.txt", seed=7)
    folder = app / "with_link"
    _file(folder, "own.txt", seed=8)
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
    src = _file(app, "ok.txt")
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


def test_initialize_repairs_an_interrupted_append(app):
    src = _file(app, "a.txt")
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    before = read(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        start = z.start_dir
    tail = before[start:]
    open(zip_path + ".tail", "wb").write(tail)
    open(zip_path + ".guard", "w").write(json.dumps({"start_dir": start, "tail_size": len(tail)}))
    with open(zip_path, "r+b") as f:
        f.seek(start)
        f.write(b"half a member" * 50)
    api.initialize()
    assert read(zip_path) == before and set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_many_archives_at_once_stay_consistent(app):
    files = [_file(app / "many", f"f{i}.txt", 1500, seed=i) for i in range(16)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: api.archive_file_result(p, "Docs"), files))
    assert all(r["success"] and r["reason"] == "OK" for r in results), [r for r in results if not r["success"]]
    assert api.dashboard_summary()["item_count"] == 16
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}
    left = leftovers()
    assert left["temp_entries"] == [] and left["children"] == []
