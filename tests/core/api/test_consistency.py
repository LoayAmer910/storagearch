"""core.api - the archive files and the catalog always tell the same story: a write counts in both or in
neither, whatever fails or dies in between; what an older interrupted write left behind is repaired; a sealed
archive is never rewritten; and only one StorageArch works on a data folder."""
import os
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from core import api, config, events, store
from core.catalog import Catalog
from support.api import item_id_by_name, read
from support.files import write_lines_file

REPO = os.path.dirname(os.path.dirname(os.path.abspath(api.__file__)))

# A second StorageArch process working in its own data folder (argv[1]); `{crash}` makes it die at one exact point.
OTHER_PROCESS = """
import os, sys
sys.path.insert(0, {repo!r})
os.chdir(sys.argv[1])
from core import api, store
from core.catalog import Catalog

def make(name, word):
    path = os.path.join(sys.argv[1], name)
    with open(path, "w") as f:
        f.write((word + " line\\n") * 20000)
    return path

api.initialize()
api.create_category("Docs")
{before}
{crash}
api.archive_file_result(make("b.txt", "beta"), "Docs")
print("survived")
"""
FIRST_ITEM = 'assert api.archive_file_result(make("a.txt", "alpha"), "Docs")["success"]'
DIE_BEFORE_THE_CATALOG = "Catalog.add_item = lambda *a, **k: os._exit(7)"
DIE_AFTER_THE_CATALOG = "store.commit = lambda zip_path: os._exit(7)"


def _run_and_die(home, before, crash):
    os.makedirs(home, exist_ok=True)
    done = subprocess.run([sys.executable, "-c", OTHER_PROCESS.format(repo=REPO, before=before, crash=crash), str(home)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 7 and "survived" not in done.stdout, done.stderr[-2000:]


def _start_in(home, monkeypatch):
    monkeypatch.chdir(home)
    events.reset()
    api.initialize()


def _names():
    return sorted(row[1] for row in api.search(""))


def _zips():
    return sorted(os.path.join(d, f) for d, _s, fs in os.walk(config.archives_dir()) for f in fs if f.endswith(".zip"))


def _leftovers():
    return sorted(f for _d, _s, fs in os.walk(config.archives_dir()) for f in fs if not f.endswith((".zip", ".json")))


def _healthy(*expected_names):
    assert _names() == sorted(expected_names)
    assert all(set(statuses.values()) <= {"VERIFIED"} for statuses in api.verify_all().values()), api.verify_all()
    assert api.dashboard_summary()["integrity_counts"]["MISSING"] == 0 and _leftovers() == []


def _archive(app, name, seed=1, category="Docs"):
    path = write_lines_file(app, name, seed=seed)
    result = api.archive_file_result(path, category)
    assert result["success"], result
    return path


# --- a failure between the archive file and the catalog -------------------------------------------------

@pytest.mark.parametrize("error", [KeyboardInterrupt("stopped"), sqlite3.OperationalError("disk I/O error")],
                         ids=["interrupted", "database error"])
@pytest.mark.parametrize("first_write", [False, True], ids=["an archive in use", "a new archive"])
def test_a_write_the_catalog_did_not_record_is_taken_back_out_of_the_archive(app, monkeypatch, error, first_write):
    api.create_category("Docs")
    if not first_write:
        _archive(app, "a.txt")
    before = {z: read(z) for z in _zips()}
    path = write_lines_file(app, "b.txt", seed=2)

    def fails(self, *args, **kwargs):
        raise error
    with monkeypatch.context() as broken:
        broken.setattr(Catalog, "add_item", fails)
        try:
            assert api.archive_file_result(path, "Docs")["success"] is False
        except KeyboardInterrupt:
            pass
    assert {z: read(z) for z in _zips()} == before  # byte for byte what it was; no archive file was added
    _healthy(*([] if first_write else ["a.txt"]))
    assert api.archive_file_result(path, "Docs")["success"]  # and the same file can simply be archived again
    _healthy(*(["b.txt"] if first_write else ["a.txt", "b.txt"]))


def test_a_checkin_the_catalog_did_not_record_leaves_the_item_and_its_archive_as_they_were(app, monkeypatch):
    src = _archive(app, "a.txt")
    item_id = item_id_by_name("a.txt")
    ws = api.checkout(item_id)
    with open(ws, "ab") as f:
        f.write(b"edited\n")
    before = {z: read(z) for z in _zips()}

    def fails(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    with monkeypatch.context() as broken:
        broken.setattr(Catalog, "replace_item_content", fails)
        assert api.checkin_result(item_id)["success"] is False
    assert {z: read(z) for z in _zips()} == before and os.path.exists(ws) and api.get_item(item_id)[10] == "CHECKED_OUT"
    assert api.checkin(item_id)
    _healthy("a.txt")
    assert read(api.checkout(item_id, destination=str(app / "back"))) == read(src) + b"edited\n"


@pytest.mark.parametrize("before", [FIRST_ITEM, ""], ids=["an archive in use", "a new archive"])
def test_a_process_that_dies_before_the_catalog_is_updated_leaves_no_trace_after_restart(app, monkeypatch, before):
    home = app / "home"
    _run_and_die(home, before, DIE_BEFORE_THE_CATALOG)
    _start_in(home, monkeypatch)
    _healthy(*(["a.txt"] if before else []))
    if not before:
        assert api.list_archives() == [] and _zips() == []
    assert api.archive_file_result(str(home / "b.txt"), "Docs")["success"]
    _healthy(*(["a.txt", "b.txt"] if before else ["b.txt"]))


@pytest.mark.parametrize("before", [FIRST_ITEM, ""], ids=["an archive in use", "a new archive"])
def test_a_process_that_dies_right_after_the_catalog_was_updated_keeps_the_item(app, monkeypatch, before):
    home = app / "home"
    _run_and_die(home, before, DIE_AFTER_THE_CATALOG)
    assert any(f.endswith(".guard") for _d, _s, fs in os.walk(home) for f in fs)  # the state the test is about
    _start_in(home, monkeypatch)
    _healthy(*(["a.txt", "b.txt"] if before else ["b.txt"]))
    restored = api.checkout(item_id_by_name("b.txt"), destination=str(home / "out"))
    assert read(restored) == read(home / "b.txt")
    assert api.archive_file_result(str(home / "b.txt"), "Docs")["reason"] == "DUPLICATE"


# --- an archive row without an archive ------------------------------------------------------------------

def test_a_first_write_that_fails_leaves_no_archive_behind(app, monkeypatch):
    api.create_category("Docs")
    path = write_lines_file(app, "a.txt")

    def fails(*args, **kwargs):
        raise store.StoreError("the disk refused the write")
    with monkeypatch.context() as broken:
        broken.setattr(store, "write_members", fails)
        assert api.archive_file_result(path, "Docs")["reason"] == "WRITE_FAILED"
    assert api.list_archives() == [] and api.dashboard_summary()["integrity_counts"]["MISSING"] == 0
    assert api.archive_file_result(path, "Docs")["success"]
    _healthy("a.txt")


def test_an_archive_row_left_by_an_older_failed_write_is_removed_at_start(app):
    api.create_category("Docs")
    category = api.list_categories()[0]
    ghost = os.path.join(config.archives_dir(), category[6], f"{category[6]}_001.zip")
    api._cat().add_archive(category[0], ghost)
    assert [a[2] for a in api.list_archives()] == [os.path.basename(ghost)]
    api.initialize()
    assert api.list_archives() == []


# --- what an older interrupted write left behind --------------------------------------------------------

def _add_unrecorded_entry(zip_path):
    with zipfile.ZipFile(zip_path, "a") as z:
        z.writestr("orphan/left_by_a_crash.txt", "never recorded in the catalog" * 100)


def _log():
    with open(os.path.join(config.log_dir(), "storagearch.log"), encoding="utf-8") as f:
        return f.read()


def test_an_entry_the_catalog_never_recorded_is_removed_from_an_open_archive(app):
    src = _archive(app, "a.txt")
    zip_path, = _zips()
    _add_unrecorded_entry(zip_path)
    assert api.verify_category("Docs") == {os.path.basename(zip_path): "VERIFIED"}
    assert "orphan/left_by_a_crash.txt" not in zipfile.ZipFile(zip_path).namelist()
    assert "never recorded" in _log() and "orphan/left_by_a_crash.txt" in _log()
    assert read(api.checkout(item_id_by_name("a.txt"), destination=str(app / "out"))) == read(src)
    _archive(app, "b.txt", seed=2)
    _healthy("a.txt", "b.txt")


def test_a_recorded_size_that_fell_behind_the_file_is_corrected(app):
    _archive(app, "a.txt")
    zip_path, = _zips()
    with sqlite3.connect(config.db_path()) as db:
        db.execute("UPDATE archives SET current_size = current_size - 100")
    assert api.verify_category("Docs") == {os.path.basename(zip_path): "VERIFIED"}
    assert api.list_archives()[0][4] == os.path.getsize(zip_path)


def test_an_archive_with_something_added_after_it_or_a_damaged_member_is_not_repaired(app):
    _archive(app, "a.txt")
    zip_path, = _zips()
    original = read(zip_path)
    with open(zip_path, "ab") as f:
        f.write(b"x")                      # data after the end of the ZIP is not what an interrupted write leaves
    tampered = read(zip_path)
    assert api.verify_category("Docs") == {os.path.basename(zip_path): "CHANGED"} and read(zip_path) == tampered
    with open(zip_path, "wb") as f:        # a recorded member no longer intact, and an unrecorded entry next to it
        f.write(original)
    name = api._cat().members_of_archive(api.list_archives()[0][0])[0]["path_in_archive"]
    with zipfile.ZipFile(zip_path) as z:
        offset = z.getinfo(name).header_offset + 30 + len(name.encode()) + 5
    with open(zip_path, "r+b") as f:
        f.seek(offset)
        byte = f.read(1)
        f.seek(offset)
        f.write(bytes([byte[0] ^ 0xFF]))
    _add_unrecorded_entry(zip_path)
    damaged = read(zip_path)
    assert api.verify_category("Docs")[os.path.basename(zip_path)] in ("CHANGED", "CORRUPTED")
    assert read(zip_path) == damaged


def test_a_compaction_whose_catalog_update_never_happened_is_made_good(app, monkeypatch):
    src = _archive(app, "a.txt")
    item_id = item_id_by_name("a.txt")

    def fails(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(api, "COMPACT_DEAD_RATIO", 0.0)
    with monkeypatch.context() as broken:
        broken.setattr(Catalog, "record_compaction", fails)
        ws = api.checkout(item_id)
        with open(ws, "ab") as f:
            f.write(b"edited\n")
        assert api.checkin_result(item_id) == {"success": True, "reason": None}  # the item itself was saved
    _healthy("a.txt")
    assert read(api.checkout(item_id, destination=str(app / "back"))) == read(src) + b"edited\n"


# --- a step after the save fails ------------------------------------------------------------------------

def test_a_failure_after_the_item_was_saved_does_not_report_the_item_as_failed(app, monkeypatch):
    path = write_lines_file(app, "a.txt")

    def fails(*args, **kwargs):
        raise OSError("the listing could not be written")
    with monkeypatch.context() as broken:
        broken.setattr(store, "write_manifest", fails)
        result = api.archive_file_result(path, "Docs")
    assert result["success"] and result["reason"] == "OK"
    assert "the listing could not be written" in _log()
    _healthy("a.txt")
    _archive(app, "b.txt", seed=2)  # the next write brings the listing up to date
    manifest = os.path.splitext(_zips()[0])[0] + ".manifest.json"
    assert os.path.exists(manifest) and read(manifest).count(b'"path_in_archive"') == 2


# --- a sealed archive is final --------------------------------------------------------------------------

def test_a_sealed_archive_is_never_rewritten(app, monkeypatch):
    api.create_category("Small", max_archive_size=1024)
    src = _archive(app, "a.txt", category="Small")  # larger than the limit: its archive is sealed at once
    sealed, = _zips()
    sealed_bytes = read(sealed)
    item_id = item_id_by_name("a.txt")
    monkeypatch.setattr(api, "COMPACT_DEAD_RATIO", 0.0)
    ws = api.checkout(item_id)
    with open(ws, "ab") as f:
        f.write(b"edited\n")
    assert api.checkin(item_id)                    # the new version goes to another archive
    api.initialize()                               # a restart with its recovery
    assert set(api.verify_category("Small").values()) == {"VERIFIED"}
    assert read(sealed) == sealed_bytes and len(_zips()) == 2
    _add_unrecorded_entry(sealed)                  # even what would be repaired in an open archive
    changed = read(sealed)
    assert api.verify_category("Small")[os.path.basename(sealed)] == "CHANGED" and read(sealed) == changed
    assert read(api.checkout(item_id, destination=str(app / "back"))) == read(src) + b"edited\n"


# --- one StorageArch per data folder --------------------------------------------------------------------

SECOND_INSTANCE = """
import os, sys
sys.path.insert(0, {repo!r})
os.chdir(sys.argv[1])
from core import api
try:
    api.initialize()
    print("started")
except api.AlreadyRunningError as exc:
    print("refused:", exc)
"""


def _second_instance(home):
    done = subprocess.run([sys.executable, "-c", SECOND_INSTANCE.format(repo=REPO), str(home)],
                          capture_output=True, text=True, timeout=120)
    return done.stdout.strip()


def test_a_second_storagearch_on_the_same_data_folder_is_refused_and_changes_nothing(app):
    _archive(app, "a.txt")
    zip_path, = _zips()
    # a write of the first instance that is still running: its guard is on the disk
    with open(zip_path + ".guard", "w") as f:
        f.write('{"start_dir": 0, "tail_size": 0}')
    with open(zip_path + ".tail", "wb"):
        pass
    before = read(zip_path)
    assert _second_instance(app).startswith("refused:")
    assert read(zip_path) == before and os.path.exists(zip_path + ".guard")  # it repaired nothing that was not its own
    store.commit(zip_path)
    _healthy("a.txt")


def test_the_data_folder_is_free_again_when_its_storagearch_is_gone(app, monkeypatch):
    home = app / "home"
    _run_and_die(home, FIRST_ITEM, DIE_BEFORE_THE_CATALOG)  # a process that held the folder and died
    assert _second_instance(home) == "started"


def test_the_same_process_can_start_again_and_move_between_data_folders(app, tmp_path):
    import msvcrt
    left = os.path.join(api.data_root(), "storagearch.lock")
    api.initialize()
    assert api.set_storage_location(data_root=str(tmp_path / "elsewhere"))
    with open(left, "a+b") as f:  # the folder it left is free for another StorageArch
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    assert api.set_storage_location(data_root=os.path.dirname(left))  # and it can come back
    assert _second_instance(app).startswith("refused:")


def test_switching_to_a_data_folder_another_storagearch_uses_is_refused_and_keeps_the_current_one(app, tmp_path):
    busy = tmp_path / "busy"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import os, sys, time; sys.path.insert(0, {repo!r}); os.makedirs(sys.argv[1], exist_ok=True); os.chdir(sys.argv[1]);"
        "from core import api, config; config.save(data_root=sys.argv[1]); api.initialize(); print('ready', flush=True); time.sleep(120)"
    ).format(repo=REPO), str(busy)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        current = api.data_root()
        assert api.set_storage_location(data_root=str(busy)) is False
        assert api.data_root() == current
        _archive(app, "a.txt")
        _healthy("a.txt")
    finally:
        holder.kill()
        holder.wait(10)
