"""Step 7a - the API the GUI calls: categories, lookups, row shapes, locations, hashes, duplicates."""
import os
import shutil

import pytest

import corpus
from api_support import item_id_by_name, read
from core import api, config
from core.catalog import LegacyDatabaseError


def test_initialize_creates_the_data_layout(app):
    for folder in ("archives", "workspace", "temp", "logs"):
        assert os.path.isdir(app / "SmartArchiveData" / folder)
    assert os.path.isfile(app / "SmartArchiveData" / "smartarchive.db")


def test_categories_create_update_delete(app):
    assert api.create_category("Docs", "desc", "#112233", 1000)
    assert api.create_category("Docs")  # already there: still True, like before
    assert not api.create_category("   ") and not api.create_category(None)
    row = api.list_categories()[0]
    assert row[1:5] == ("Docs", "desc", "#112233", 1000) and os.path.isdir(app / "SmartArchiveData" / "archives" / row[6])
    assert api.update_category(row[0], "Documents", None, None, 2000)
    assert api.get_category(row[0])[1] == "Documents" and api.get_category(row[0])[6] == row[6]
    assert api.delete_category(row[0]) == "DELETED"
    assert not os.path.isdir(app / "SmartArchiveData" / "archives" / row[6])
    assert api.delete_category(row[0]) == "NOT_FOUND"


def test_a_category_with_archives_cannot_be_deleted(app):
    api.create_category("Docs")
    src = app / "a.txt"
    src.write_bytes(corpus.lines(500, "{i} {w}\n"))
    assert api.archive_file_result(str(src), "Docs")["success"]
    assert api.delete_category(api.list_categories()[0][0]) == "NOT_EMPTY"


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
