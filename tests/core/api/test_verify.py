"""core.api - verifying archives: one archive, a category, everything; unknown ones are reported, never raised."""
from core import api
from support.api import item_id_by_name
from support.files import write_lines_file


def test_verifying_an_archive_that_is_not_in_the_catalog_says_missing(app):
    assert api.verify_archive_by_id(999999) == "MISSING"


def test_verifying_an_unknown_category_returns_nothing(app):
    assert api.verify_category("No such category") == {}


def test_a_category_and_everything_verify_by_archive_name(app):
    api.archive_file_result(write_lines_file(app, "a.txt"), "Docs")
    archive = api.get_archive(api.get_item(item_id_by_name("a.txt"))[1])
    assert api.verify_category("Docs") == {archive[2]: "VERIFIED"}
    assert api.verify_all() == {"Docs": {archive[2]: "VERIFIED"}}
