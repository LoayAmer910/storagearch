"""core.api - check out, edit and check in files and folders; restoring."""
import os
import random
import shutil
import zipfile

import pytest

from core import api, events, store, strategies
from core.strategies.base import StrategyError
from support.api import item_id_by_name, read, tree
from support.files import code_folder, files_under, sample_text, write_lines_file


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
        write_lines_file(folder, f"sub{i % 3}/f{i}.txt", 2000, seed=i)
    os.makedirs(folder / "empty")
    return folder


def test_checkout_edit_checkin_of_a_file(app):
    src = write_lines_file(app, "notes.txt")
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
    src = write_lines_file(app, "same.txt")
    api.archive_file_result(src, "Docs")
    zip_path = api.list_archives()[0][3]
    before = read(zip_path)
    item_id = item_id_by_name("same.txt")
    ws = api.checkout(item_id)
    assert api.checkin(item_id) and not os.path.exists(ws)
    assert read(zip_path) == before


def test_checkout_edit_checkin_of_a_folder_with_added_removed_and_empty_entries(app):
    folder = app / "proj"
    write_lines_file(folder, "keep.txt", seed=1)
    write_lines_file(folder, "remove.txt", seed=2)
    write_lines_file(folder, "sub/change.txt", seed=3)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    ws = api.checkout(item_id)
    assert tree(ws) == tree(folder)
    os.remove(os.path.join(ws, "remove.txt"))
    write_lines_file(ws, "added/new.txt", seed=4)
    os.makedirs(os.path.join(ws, "added", "empty"))
    with open(os.path.join(ws, "sub", "change.txt"), "ab") as f:
        f.write(b"changed\n")
    expected = tree(ws)
    assert api.checkin(item_id)
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_a_folder_checkin_writes_only_the_files_that_changed(app, monkeypatch):
    folder = _folder_of(app, "proj")
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    archive_id = api.get_item(item_id)[1]
    old = {m["relative_path"]: m for m in api._cat().members_of_item(item_id)}
    ws = api.checkout(item_id)
    with open(os.path.join(ws, "sub1", "f4.txt"), "ab") as f:
        f.write(b"changed\n")
    write_lines_file(ws, "added.txt", seed=99)
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
        api.archive_file_result(write_lines_file(app, f"filler{i}.txt", 3000, seed=50 + i), "Small")
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
    write_lines_file(folder, "big.txt", 30000, seed=77)
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
    a = write_lines_file(app / "a", "report.txt", seed=1)
    b = write_lines_file(app / "b", "report.txt", seed=2)
    api.archive_file_result(a, "Docs")
    api.archive_file_result(b, "Docs")
    ids = [row[0] for row in api.search("report.txt")]
    paths = [api.checkout(i) for i in ids]
    assert len(set(paths)) == 2 and sorted(read(p) for p in paths) == sorted([read(a), read(b)])


def test_checkout_never_overwrites_an_existing_file_at_an_explicit_destination(app):
    src = write_lines_file(app, "x.txt")
    api.archive_file_result(src, "Docs")
    dest = app / "dest"
    dest.mkdir()
    (dest / "x.txt").write_text("the user's own file")
    assert api.checkout(item_id_by_name("x.txt"), destination=str(dest)) is None
    assert (dest / "x.txt").read_text() == "the user's own file"


def test_checkin_refuses_what_it_cannot_do(app):
    src = write_lines_file(app, "x.txt")
    api.archive_file_result(src, "Docs")
    item_id = item_id_by_name("x.txt")
    assert not api.checkin(item_id)  # not checked out
    assert not api.checkin(999999)
    assert api.checkout(999999) is None


def test_a_deleted_checked_out_copy_returns_the_item_to_its_archived_version(app):
    src = write_lines_file(app, "x.txt")
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


def test_an_item_in_a_sealed_archive_moves_on_checkin(app):
    api.create_category("Small", max_archive_size=30_000)
    first = write_lines_file(app, "first.txt", 2500, seed=1)
    api.archive_file_result(first, "Small")
    api.archive_file_result(write_lines_file(app, "second.txt", 2500, seed=2), "Small")
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
    src = write_lines_file(app, "big.txt", 20000, seed=9)
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


def test_restoring_small_files_makes_no_temporary_files(app, monkeypatch):
    folder = code_folder(str(app / "proj"), count=30, seed=4)
    api.archive_folder_result(folder, "Docs")
    item_id = item_id_by_name("proj", "folder")
    made = []
    real = store.tempfile.mkdtemp
    monkeypatch.setattr(store.tempfile, "mkdtemp",  # the module is shared: count only a per-file restore folder
                        lambda *a, **k: made.append(k.get("prefix")) or real(*a, **k))
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(app / "proj")
    assert "restore_" not in made


def test_a_member_that_fails_to_decode_fails_the_restore_and_leaves_nothing_behind(app, monkeypatch):
    folder = code_folder(str(app / "proj"), count=40, seed=6)
    api.archive_folder_result(folder, "Docs")
    item_id = item_id_by_name("proj", "folder")
    smart = [m for m in api._cat().members_of_item(item_id) if m["kind"] == "file" and m["method"] not in store.BASELINE_METHODS]
    victim = strategies.get(smart[len(smart) // 2]["method"])

    def broken(chunks):
        yield b"half of a file"
        raise StrategyError("damaged stream")
    monkeypatch.setattr(victim, "decode", broken)
    assert api.checkout(item_id) is None
    workspace = api.get_storage_locations()["workspace_root"] or os.path.join(api.data_root(), "workspace")
    assert not [p for p in files_under(app) if p.endswith(".restoring")]
    assert api.get_item(item_id)[10] == "ARCHIVED"  # not marked as checked out
    assert not os.path.exists(os.path.join(workspace, api._cat().item_uid(item_id), "proj"))


def test_parallel_restore_keeps_every_file_and_folder(app):
    folder = app / "deep"
    for i in range(200):
        path = folder / f"a{i % 5}" / f"b{i % 7}" / f"f{i}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sample_text(random.Random(i).randint(0, 30_000), seed=i))
    (folder / "empty" / "inner").mkdir(parents=True)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("deep", "folder")
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(folder)


@pytest.fixture
def reported(monkeypatch):
    """The reason each operation reports (what the dashboard sees when a checkout fails)."""
    reasons = []
    monkeypatch.setattr(events, "report_operation", lambda event, metrics=None: reasons.append(event["error_category"]))
    return reasons


def _archived(app, name="x.txt"):
    api.archive_file_result(write_lines_file(app, name), "Docs")
    return item_id_by_name(name)


def test_checkout_of_an_item_whose_archive_records_are_gone_is_refused(app, reported):
    item_id = _archived(app)
    with api._cat().transaction() as db:  # a damaged catalog: the item lost its members
        db.execute("DELETE FROM members WHERE item_id = ?", (item_id,))
    assert api.checkout(item_id) is None
    assert reported[-1] == "ARCHIVE_NOT_FOUND" and api.get_item(item_id)[10] == "ARCHIVED"


def test_checkout_of_a_tampered_stored_name_writes_nothing(app, reported):
    item_id = _archived(app)
    with api._cat().transaction() as db:
        db.execute("UPDATE members SET relative_path = '../escape.txt' WHERE item_id = ?", (item_id,))
    assert api.checkout(item_id) is None
    assert reported[-1] == "SMART_RESTORE_ERROR"
    assert not any("escape" in f for _d, _s, fs in os.walk(app) for f in fs)


def test_checkout_and_checkin_wait_for_enough_free_space(app, reported, monkeypatch):
    item_id = _archived(app)
    with monkeypatch.context() as full_disk:  # never monkeypatch.undo(): it also undoes the test's cloud isolation
        full_disk.setattr(api, "_enough_space", lambda needed: False)
        assert api.checkout(item_id) is None and reported[-1] == "INSUFFICIENT_SPACE"
    ws = api.checkout(item_id)
    with open(ws, "a", encoding="utf-8") as f:
        f.write("edited\n")
    monkeypatch.setattr(api, "_enough_space", lambda needed: False)
    assert api.checkin_result(item_id) == {"success": False, "reason": "INSUFFICIENT_SPACE"}
    assert os.path.exists(ws) and api.get_item(item_id)[10] == "CHECKED_OUT"  # nothing lost, try again later


def test_a_copy_checked_out_outside_the_workspace_is_never_deleted_on_checkin(app):
    item_id = _archived(app)
    mine = app / "my_folder"
    mine.mkdir()
    copy = api.checkout(item_id, destination=str(mine))
    assert api.checkin(item_id, remove_workspace_copy=True)
    assert os.path.exists(copy), "only copies inside the workspace folder may be removed"


def test_a_file_the_user_added_next_to_a_checked_out_copy_survives_checkin(app):
    item_id = _archived(app)
    ws = api.checkout(item_id)
    note = os.path.join(os.path.dirname(ws), "my_notes.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write("mine")
    assert api.checkin(item_id)
    assert not os.path.exists(ws) and open(note, encoding="utf-8").read() == "mine"


def test_on_exit_the_items_still_checked_out_are_listed(app):
    item_id = _archived(app)
    assert api.handle_pending_checkouts_on_exit() == []
    ws = api.checkout(item_id)
    assert [(row[0], os.path.normpath(row[4])) for row in api.handle_pending_checkouts_on_exit()] == [(item_id, ws)]
    api.shutdown_cloud(timeout=0.5)  # closing the app afterwards never raises


def test_an_unexpected_failure_during_checkin_keeps_the_edited_copy(app, reported, monkeypatch):
    item_id = _archived(app)
    ws = api.checkout(item_id)
    with open(ws, "a", encoding="utf-8") as f:
        f.write("work that must not be lost\n")

    def broken(*args, **kwargs):
        raise RuntimeError("disk controller reset")

    monkeypatch.setattr(store, "write_members", broken)
    assert api.checkin_result(item_id) == {"success": False, "reason": "RuntimeError"}
    assert open(ws, encoding="utf-8").read().endswith("work that must not be lost\n")
    assert api.get_item(item_id)[10] == "CHECKED_OUT"


# --- a checked-out copy that changes while it is checked in is never removed with unsaved content -------

def _after_every_write(monkeypatch, change):
    """Runs change(n) right after the n-th write to an archive - the moment the copy looks saved."""
    real, count = store.write_members, []

    def write(zip_path, planned):
        records = real(zip_path, planned)
        count.append(1)
        change(len(count))
        return records
    monkeypatch.setattr(store, "write_members", write)
    return count


def _checked_out_folder(app):
    assert api.archive_folder_result(str(_folder_of(app, "proj")), "Docs")["success"]
    item_id = item_id_by_name("proj")
    return item_id, api.checkout(item_id)


def test_a_file_added_while_a_folder_is_checked_in_is_saved_before_the_copy_is_removed(app, monkeypatch):
    item_id, ws = _checked_out_folder(app)
    write_lines_file(ws, "edited.txt", seed=50)
    _after_every_write(monkeypatch, lambda n: n == 1 and write_lines_file(ws, "sub0/late.txt", seed=51))
    assert api.checkin_result(item_id) == {"success": True, "reason": None}
    assert not os.path.exists(ws) and api.get_item(item_id)[10] == "ARCHIVED"
    back = tree(api.checkout(item_id, destination=str(app / "back")))
    assert back["edited.txt"] and back["sub0/late.txt"]
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}


def test_a_file_added_to_an_unchanged_copy_during_checkin_is_saved_too(app, monkeypatch):
    item_id, ws = _checked_out_folder(app)
    real, calls = api._scan_many, []

    def scan_then_add(entries):
        scanned = real(entries)
        if not calls:
            write_lines_file(ws, "late.txt", seed=52)
        calls.append(1)
        return scanned
    monkeypatch.setattr(api, "_scan_many", scan_then_add)
    assert api.checkin(item_id) and not os.path.exists(ws)
    assert tree(api.checkout(item_id, destination=str(app / "back")))["late.txt"]


def test_a_file_edited_right_after_its_checkin_was_written_is_saved_too(app, monkeypatch):
    item_id = _archived(app)
    ws = api.checkout(item_id)
    with open(ws, "a", encoding="utf-8") as f:
        f.write("first edit\n")

    def edit(n):
        if n == 1:
            with open(ws, "ab") as f:
                f.write(b"late edit\n")
    _after_every_write(monkeypatch, edit)
    assert api.checkin(item_id) and not os.path.exists(ws)
    assert read(api.checkout(item_id, destination=str(app / "back"))).endswith(b"late edit\n")


def test_a_copy_that_never_stops_changing_stays_checked_out_with_everything_in_it(app, reported, monkeypatch):
    item_id, ws = _checked_out_folder(app)
    write_lines_file(ws, "edited.txt", seed=50)
    with monkeypatch.context() as busy:
        _after_every_write(busy, lambda n: write_lines_file(ws, f"late{n}.txt", seed=60 + n))
        assert api.checkin_result(item_id) == {"success": False, "reason": "WORKSPACE_STILL_CHANGING"}
    assert api.get_item(item_id)[10] == "CHECKED_OUT" and os.path.normpath(api.get_item(item_id)[11]) == ws
    expected = tree(ws)
    assert {"edited.txt", "late1.txt", "late2.txt", "late3.txt"} <= set(expected)
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}
    assert api.checkin(item_id) and not os.path.exists(ws)  # the other program stopped: this checkin takes it all
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == expected


def test_a_calm_checkin_writes_once_and_leaves_nothing_behind(app, monkeypatch):
    item_id, ws = _checked_out_folder(app)
    write_lines_file(ws, "edited.txt", seed=50)
    writes = _after_every_write(monkeypatch, lambda n: None)
    assert api.checkin(item_id) and len(writes) == 1
    assert not os.path.exists(os.path.dirname(ws))
