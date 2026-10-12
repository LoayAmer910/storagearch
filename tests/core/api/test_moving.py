"""core.api - a data folder that was moved, renamed or copied while StorageArch was closed keeps working:
the catalog's full paths follow the folder it is in, and nothing is ever written to where it used to be."""
import json
import os
import shutil
import subprocess
import sys

from core import api, config, events
from support.api import item_id_by_name, read, tree

REPO = os.path.dirname(os.path.dirname(os.path.abspath(api.__file__)))

# A StorageArch that worked in its own folder (argv[1]) and was closed: two files and a folder archived,
# one file checked out and edited, and one archive sealed.
EARLIER_SESSION = """
import os, sys
sys.path.insert(0, {repo!r})
os.chdir(sys.argv[1])
from core import api

def make(name, word, lines=20000):
    path = os.path.join(sys.argv[1], "src", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write((word + " line\\n") * lines)
    return path

api.initialize()
api.create_category("Small", max_archive_size=1024)
assert api.archive_file_result(make("sealed.txt", "sigma"), "Small")["success"]
assert api.archive_file_result(make("a.txt", "alpha"), "Docs")["success"]
assert api.archive_file_result(make("b.txt", "beta"), "Docs")["success"]
make("folder/one.txt", "one"); make("folder/sub/two.txt", "two")
assert api.archive_folder_result(os.path.join(sys.argv[1], "src", "folder"), "Docs")["success"]
out = api.checkout(next(r[0] for r in api.search("b.txt")))
with open(out, "a") as f:
    f.write("edited while checked out\\n")
print("closed")
"""


def _earlier_session(home):
    os.makedirs(home)
    done = subprocess.run([sys.executable, "-c", EARLIER_SESSION.format(repo=REPO), str(home)],
                          capture_output=True, text=True, timeout=300)
    assert done.stdout.strip() == "closed", done.stderr[-2000:]


def _start_in(home, monkeypatch):
    monkeypatch.chdir(home)
    events.reset()
    api.initialize()


def _all_verified():
    statuses = api.verify_all()
    assert statuses and all(set(s.values()) == {"VERIFIED"} for s in statuses.values()), statuses
    assert set(api.dashboard_summary()["integrity_counts"].items()) >= {("MISSING", 0), ("CHANGED", 0), ("CORRUPTED", 0)}


def _everything_works(home):
    _all_verified()
    assert read(api.checkout(item_id_by_name("a.txt"), destination=str(home / "out"))) == read(home / "src" / "a.txt")
    assert tree(api.checkout(item_id_by_name("folder"), destination=str(home / "out"))) == tree(home / "src" / "folder")
    assert read(api.checkout(item_id_by_name("sealed.txt"), destination=str(home / "out"))) == read(home / "src" / "sealed.txt")
    # the copy that was checked out before the move is where the catalog says, and goes back in with its edit
    item = api.get_item(item_id_by_name("b.txt"))
    assert item[10] == "CHECKED_OUT" and os.path.normcase(item[11]).startswith(os.path.normcase(str(home)))
    assert read(item[11]).rstrip().endswith(b"edited while checked out")
    assert api.checkin(item[0]) and not os.path.exists(item[11])
    assert read(api.checkout(item[0], destination=str(home / "out2"))).rstrip().endswith(b"edited while checked out")
    # and new work goes into the folder it is in now
    new = home / "src" / "new.txt"
    new.write_text("gamma line\n" * 20000)
    assert api.archive_file_result(str(new), "Docs")["success"]
    assert all(os.path.normcase(a[3]).startswith(os.path.normcase(str(home))) for a in api.list_archives())
    _all_verified()


def test_a_moved_data_folder_keeps_every_archive_and_every_checked_out_copy(app, monkeypatch):
    first, second = app / "first place", app / "second place"
    _earlier_session(first)
    shutil.move(str(first), str(second))
    _start_in(second, monkeypatch)
    _everything_works(second)
    assert not first.exists(), "something was written to where the data folder used to be"


def test_a_renamed_data_folder_alone_keeps_working(app, monkeypatch):
    home = app / "home"
    _earlier_session(home)
    os.rename(home / "SmartArchiveData", home / "My Archive")
    (home / config.CONFIG_FILE).write_text(json.dumps({"data_root": "My Archive", "workspace_root": None}))
    _start_in(home, monkeypatch)
    _everything_works(home)
    assert not (home / "SmartArchiveData").exists()


def test_a_copy_of_the_data_folder_never_writes_into_the_original(app, monkeypatch):
    original, copy = app / "original", app / "copy"
    _earlier_session(original)
    shutil.copytree(original, copy)
    untouched = tree(original)
    _start_in(copy, monkeypatch)
    _everything_works(copy)
    assert tree(original) == untouched


def test_a_data_folder_that_did_not_move_is_left_exactly_as_it_is(app, monkeypatch):
    home = app / "home"
    _earlier_session(home)
    def paths():
        import sqlite3
        with sqlite3.connect(home / "SmartArchiveData" / "smartarchive.db") as db:
            return (db.execute("SELECT file_path, manifest_path FROM archives ORDER BY id").fetchall(),
                    db.execute("SELECT workspace_path FROM items ORDER BY id").fetchall())
    before = paths()
    _start_in(home, monkeypatch)
    assert paths() == before and api._cat().follow_data_folder(config.archives_dir(), config.workspace_dir()) == 0
    assert not os.path.exists(os.path.join(config.log_dir(), "storagearch.log"))  # nothing to report


def test_a_copy_checked_out_to_a_workspace_outside_the_data_folder_stays_where_it_is(app, monkeypatch, tmp_path):
    first, second, workspace = app / "first", app / "second", tmp_path / "my workspace"
    os.makedirs(first)
    workspace.mkdir()
    done = subprocess.run([sys.executable, "-c", (
        "import os, sys; sys.path.insert(0, {repo!r}); os.chdir(sys.argv[1]);"
        "from core import api; api.initialize(); api.set_storage_location(workspace_root=sys.argv[2]);"
        "p = os.path.join(sys.argv[1], 'a.txt'); open(p, 'w').write('alpha line\\n' * 20000);"
        "assert api.archive_file_result(p, 'Docs')['success']; print(api.checkout(api.search('a.txt')[0][0]))"
    ).format(repo=REPO), str(first), str(workspace)], capture_output=True, text=True, timeout=300)
    checked_out = done.stdout.strip()
    assert os.path.isfile(checked_out), done.stderr[-2000:]
    shutil.move(str(first), str(second))
    _start_in(second, monkeypatch)
    item = api.get_item(item_id_by_name("a.txt"))
    assert os.path.normcase(item[11]) == os.path.normcase(checked_out) and api.checkin(item[0])
    _all_verified()


# --- the config file ------------------------------------------------------------------------------------

def _config(folder):
    with open(os.path.join(folder, config.CONFIG_FILE), encoding="utf-8") as f:
        return json.load(f)


def test_a_location_inside_the_app_folder_is_saved_relative_to_it_so_the_whole_folder_can_move(app, monkeypatch, tmp_path):
    inside = app / "data here"
    assert api.set_storage_location(data_root=str(inside), workspace_root=str(app / "work here"))
    assert _config(app) == {"data_root": "data here", "workspace_root": "work here"}
    assert os.path.normcase(api.data_root()) == os.path.normcase(str(inside))
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    try:
        assert api.set_storage_location(data_root=str(outside))
        assert os.path.isabs(_config(app)["data_root"]) and _config(app)["workspace_root"] == "work here"
        assert os.path.normcase(api.data_root()) == os.path.normcase(str(outside))
    finally:
        api.set_storage_location(data_root=str(inside))
        shutil.rmtree(outside, ignore_errors=True)
