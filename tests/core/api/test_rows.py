"""core.api - what the API hands out are rows with named fields that still behave as the tuples they were."""
import sqlite3

from core import api, config
from support.files import write_lines_file


def _raw(sql, args=()):
    with sqlite3.connect(config.db_path()) as db:
        return db.execute(sql, args).fetchall()


def test_every_row_the_api_returns_has_named_fields_in_the_order_of_its_columns(app):
    src = write_lines_file(app, "notes.txt")
    assert api.archive_file_result(src, "Docs")["success"]
    (found,) = api.search("notes.txt")
    item, category = api.get_item(found.id), api.list_categories()[0]
    archive, (listed,) = api.get_archive(item.archive_id), api.list_archives()
    api.checkout(item.id)
    (pending,) = api.items_pending_return()
    item = api.get_item(item.id)

    # by name
    assert (found.file_name, found.status, found.category_name) == ("notes.txt", "ARCHIVED", "Docs")
    assert (item.file_type, item.status, item.workspace_path) == ("file", "CHECKED_OUT", pending.workspace_path)
    assert (category.name, category.folder_name) == ("Docs", "Docs")
    assert archive.file_path == found.archive_path == listed.file_path and listed.category_name == "Docs"
    assert api.get_category(category.id) == category

    # and still the same tuples, column by column
    assert item == _raw("SELECT id, archive_id, file_type, file_name, original_path, path_in_archive, original_size, "
                        "compressed_size, original_hash, archive_hash, status, workspace_path, archived_at, "
                        "checked_out_at, checked_in_at, strategy FROM items")[0]
    assert category == _raw("SELECT id, name, description, color, max_archive_size, created_at, folder_name FROM categories")[0]
    assert archive[:5] == _raw("SELECT id, category_id, file_name, file_path, current_size FROM archives")[0]
    for row in (found, item, category, archive, listed, pending):
        assert isinstance(row, tuple) and list(row) == [row[i] for i in range(len(row))]
        assert tuple(getattr(row, name) for name in row._fields) == tuple(row)
    item_id, name, kind, *_rest = found
    assert (item_id, name, kind) == (item.id, "notes.txt", "file") and found[7] == "ARCHIVED" and item[10] == item.status


def test_a_row_that_is_not_there_is_still_none(app):
    assert api.get_item(12345) is None and api.get_category(12345) is None and api.get_archive(12345) is None
    assert api.search("nothing") == [] and api.items_pending_return() == []


def test_the_core_and_the_window_do_not_read_catalog_rows_by_position():
    import os
    import re

    import core
    repo = os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))
    by_position = re.compile(r"\b(item|row|category|archive|pending)\[\d+\]")
    found = []
    for folder, names in (("core", ["api.py"]), ("gui", sorted(os.listdir(os.path.join(repo, "gui"))))):
        for name in names:
            if name.endswith(".py"):
                with open(os.path.join(repo, folder, name), encoding="utf-8") as f:
                    found += [f"{folder}/{name}:{n}: {line.strip()}" for n, line in enumerate(f, 1) if by_position.search(line)]
    assert found == [], found
