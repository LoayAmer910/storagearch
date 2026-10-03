"""core.api - initialize, the data and workspace locations, protected and invalid paths."""
import json
import os
import zipfile

import pytest

from core import api, api as main, config
from core.catalog import LegacyDatabaseError
from support import corpus
from support.api import item_id_by_name, read
from support.files import write_lines_file


def test_initialize_creates_the_data_layout(app):
    for folder in ("archives", "workspace", "temp", "logs"):
        assert os.path.isdir(app / "SmartArchiveData" / folder)
    assert os.path.isfile(app / "SmartArchiveData" / "smartarchive.db")


def test_storage_locations(app):
    locations = api.get_storage_locations()
    assert locations["data_root"] == str(app / "SmartArchiveData") and locations["workspace_root"] is None
    assert not api.set_storage_location(workspace_root=os.environ.get("SystemRoot", "C:\\Windows"))
    assert api.set_storage_location(workspace_root=str(app / "ws"))
    assert config.workspace_dir() == str(app / "ws") and os.path.isdir(app / "ws")
    assert api.reset_workspace_location() and config.workspace_dir() == str(app / "SmartArchiveData" / "workspace")
    assert api.set_storage_location(data_root=str(app / "Other"))
    assert api.list_categories() == [] and os.path.isfile(app / "Other" / "smartarchive.db")


def test_switching_to_a_folder_with_an_old_version_database_is_refused_safely(app):
    import sqlite3
    old = app / "OldData"
    old.mkdir()
    with sqlite3.connect(old / "smartarchive.db") as db:
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, file_name TEXT)")
    before = read(old / "smartarchive.db")
    api.create_category("Keep")
    assert not api.set_storage_location(data_root=str(old))
    assert read(old / "smartarchive.db") == before
    assert api.get_storage_locations()["data_root"] == str(app / "SmartArchiveData")
    assert [c[1] for c in api.list_categories()] == ["Keep"]


def test_the_workspace_cannot_move_while_items_are_checked_out(app):
    src = app / "a.txt"
    src.write_bytes(corpus.lines(900, "{w}\n"))
    api.archive_file_result(str(src), "Docs")
    assert api.checkout(item_id_by_name("a.txt"))
    assert not api.set_storage_location(workspace_root=str(app / "ws2"))
    assert not api.reset_workspace_location()


def test_protected_locations(app):
    assert api.is_protected_path(os.environ.get("SystemRoot", "C:\\Windows"))
    assert not api.is_protected_path(str(app))
    result = api.archive_folder_result(str(app / "SmartArchiveData"), "Docs")  # the archive into itself
    assert result["reason"] == "PROTECTED_PATH"


def test_validate_source_path_is_exposed(app):
    assert api.validate_source_path("relative")[0] is False
    with pytest.raises(LegacyDatabaseError):
        raise LegacyDatabaseError("the class is exported for the GUI")


def test_initialize_repairs_an_interrupted_append(app):
    src = write_lines_file(app, "a.txt")
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


def test_fresh_configured_data_root_is_used(sandbox):
    new_root = os.path.join(str(sandbox), "custom_data")
    assert main.set_storage_location(data_root=new_root) is True

    locations = main.get_storage_locations()
    assert locations["data_root"] == new_root
    assert os.path.isdir(os.path.join(new_root, "archives"))
    assert os.path.isfile(os.path.join(new_root, "smartarchive.db"))


def test_fresh_configured_workspace_root_is_used(sandbox, make_file):
    main.create_category("Docs")
    new_workspace = os.path.join(str(sandbox), "custom_workspace")
    assert main.set_storage_location(workspace_root=new_workspace) is True

    path = make_file("doc.txt", "content")
    assert main.archive_file(path, "Docs") is True
    item_id = main.search("doc.txt")[0][0]
    workspace_path = main.checkout(item_id)
    assert workspace_path is not None
    assert os.path.dirname(os.path.dirname(workspace_path)) == new_workspace  # <workspace>/<item>/<name>
    assert os.path.isfile(workspace_path)


def test_archive_checkout_checkin_after_switching_locations(sandbox):
    new_root = os.path.join(str(sandbox), "switched_data")
    new_workspace = os.path.join(str(sandbox), "switched_workspace")
    assert main.set_storage_location(data_root=new_root, workspace_root=new_workspace) is True

    main.create_category("Switched")
    file_path = os.path.join(str(sandbox), "switched_file.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("switched content")
    assert main.archive_file(file_path, "Switched") is True

    item_id = main.search("switched_file.txt")[0][0]
    workspace_path = main.checkout(item_id)
    assert os.path.dirname(os.path.dirname(workspace_path)) == new_workspace

    with open(workspace_path, "a", encoding="utf-8") as f:
        f.write(" - edited")
    assert main.checkin(item_id) is True

    status = main.verify_archive_by_id(main.get_item(item_id)[1])
    assert status == "VERIFIED"


@pytest.mark.parametrize("which, protected", [
    ("data_root", r"C:\Windows\System32"),
    ("workspace_root", r"C:\Program Files\SomeApp"),
])
def test_a_protected_path_is_refused_and_nothing_changes(app, which, protected):
    before = main.get_storage_locations()
    assert main.set_storage_location(**{which: protected}) is False
    assert main.get_storage_locations() == before


def test_workspace_change_refused_while_items_checked_out(sandbox, make_file):
    main.create_category("Docs")
    path = make_file("checked_out.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("checked_out.txt")[0][0]
    main.checkout(item_id)

    new_workspace = os.path.join(str(sandbox), "should_not_apply")
    result = main.set_storage_location(workspace_root=new_workspace)
    assert result is False
    assert main.get_storage_locations()["workspace_root"] != new_workspace


def test_switching_data_location_does_not_delete_or_move_existing_data(sandbox, make_file):
    old_root = main.get_storage_locations()["data_root"]

    main.create_category("Original")
    path = make_file("keep.txt", "must survive")
    main.archive_file(path, "Original")
    items = main.search("keep.txt")
    original_zip_path = items[0][10]
    assert os.path.isfile(original_zip_path)

    new_root = os.path.join(str(sandbox), "new_empty_location")
    assert main.set_storage_location(data_root=new_root) is True

    # the OLD location's files are untouched on disk even though the app now points elsewhere
    assert os.path.isfile(original_zip_path)
    # the app now sees an empty (new) location - no silent merge/loss, just a different view
    assert main.dashboard_summary()["category_count"] == 0

    # switching back restores full access to the original data
    assert main.set_storage_location(data_root=old_root) is True
    assert any(c[1] == "Original" for c in main.list_categories())
    assert os.path.isfile(original_zip_path)
