"""core.api - categories: create, rename, delete."""
import os

from core import api, api as main
from support import corpus
from support.gui import category_id_by_name


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


def test_edit_category_duplicate_name_is_rejected(app):
    main.create_category("Alpha")
    main.create_category("Beta")
    alpha_id = category_id_by_name("Alpha")

    updated = main.update_category(alpha_id, "Beta")
    assert updated is False
    assert main.get_category(alpha_id)[1] == "Alpha"  # unchanged


def test_edit_category_empty_name_is_rejected(app):
    main.create_category("KeepMe")
    category_id = category_id_by_name("KeepMe")
    assert main.update_category(category_id, "") is False
    assert main.get_category(category_id)[1] == "KeepMe"


def test_delete_empty_category_succeeds(app):
    main.create_category("EmptyCat")
    category_id = category_id_by_name("EmptyCat")

    result = main.delete_category(category_id)
    assert result == "DELETED"
    assert main.get_category(category_id) is None
    assert not any(c[1] == "EmptyCat" for c in main.list_categories())


def test_delete_nonexistent_category_is_safe(app):
    result = main.delete_category(999999)
    assert result == "NOT_FOUND"


def test_deleting_a_category_keeps_a_file_the_user_put_in_its_folder(app):
    api.create_category("Docs")
    row = api.list_categories()[0]
    folder = app / "SmartArchiveData" / "archives" / row[6]
    (folder / "readme.txt").write_text("the user's note")
    assert api.delete_category(row[0]) == "DELETED"
    assert (folder / "readme.txt").read_text() == "the user's note"
