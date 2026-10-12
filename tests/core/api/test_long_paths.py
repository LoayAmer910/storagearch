"""core.api - paths longer than Windows' 260 characters: never a partial archive, and the user is told why."""
import os

import pytest

from core import api, folders, paths
from support.api import item_id_by_name
from support.files import write_lines_file

LONG = "\\\\?\\"  # the prefix that lets a test build such a path whatever the system setting is


def _deep_file(folder, text="deep line\n"):
    """A file inside `folder` whose full path is longer than 260 characters. Returns its path."""
    path = os.path.join(str(folder), "d" * 120, "e" * 120, "deep.txt")
    os.makedirs(LONG + os.path.dirname(path), exist_ok=True)
    with open(LONG + path, "w", encoding="utf-8") as f:
        f.write(text * 500)
    assert len(path) > paths.MAX_PATH
    return path


@pytest.fixture
def long_tree(app):
    """A folder with one ordinary file and one file too deep for Windows' ordinary paths."""
    folder = app / "proj"
    write_lines_file(folder, "ok.txt", 2000)
    deep = _deep_file(folder)
    yield folder, deep
    # pytest's own cleanup of tmp_path cannot remove such a path
    import shutil
    shutil.rmtree(LONG + str(folder), ignore_errors=True)




def test_a_folder_scan_never_leaves_out_what_it_could_not_read(long_tree):
    folder, deep = long_tree
    if os.path.exists(deep):
        pytest.skip("this system reads long paths without help")
    with pytest.raises(paths.InvalidPathError) as refused:
        folders.scan(str(folder))
    assert "too long" in str(refused.value)


@pytest.mark.parametrize("source_action", ["keep", "delete", "recycle"])
def test_a_folder_with_a_file_too_deep_to_read_is_not_archived_in_part(long_tree, source_action):
    folder, deep = long_tree
    if os.path.exists(deep):
        pytest.skip("this system reads long paths without help")
    result = api.archive_folder_result(str(folder), "Docs", source_action)
    assert (result["success"], result["reason"]) == (False, "INVALID_PATH")
    assert "too long" in result["message"]
    assert api.search("") == []
    assert os.path.exists(LONG + deep) and os.path.exists(folder / "ok.txt")  # the source is whole


def test_a_file_whose_path_is_too_long_is_not_reported_as_missing(app):
    deep = _deep_file(app / "holder")
    try:
        if os.path.exists(deep):
            pytest.skip("this system reads long paths without help")
        result = api.archive_file_result(deep, "Docs")
        assert (result["success"], result["reason"]) == (False, "INVALID_PATH")
        assert "too long" in result["message"] and str(paths.MAX_PATH) in result["message"]
    finally:
        import shutil
        shutil.rmtree(LONG + str(app / "holder"), ignore_errors=True)


def test_a_checkout_that_would_make_a_path_too_long_says_so_and_leaves_nothing(app):
    src = write_lines_file(app, "notes.txt", 2000)
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    destination = os.path.join(str(app), "out", "x" * 120, "y" * 120)
    os.makedirs(LONG + destination)
    try:
        if os.path.exists(destination):
            pytest.skip("this system reads long paths without help")
        assert api._checkout(item_id, destination) == (None, "PATH_TOO_LONG")
        assert os.listdir(LONG + destination) == [] and api.get_item(item_id)[10] == "ARCHIVED"
    finally:
        import shutil
        shutil.rmtree(LONG + os.path.join(str(app), "out"), ignore_errors=True)


def test_a_checkout_into_the_workspace_that_would_be_too_long_says_so(app):
    """An archived folder whose inner names are long: the workspace path in front of them makes them too long."""
    folder = app / "p"
    room = 240 - len(str(folder)) - len(os.sep * 3 + "f.txt")  # the source stays under the limit: 240 characters
    assert room > 20, "the temporary folder is itself too deep for this test"
    write_lines_file(folder, os.path.join("a" * (room // 2), "b" * (room - room // 2), "f.txt"), 500)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("p")
    path, reason = api._checkout(item_id, None)
    if path is not None:
        pytest.skip("this system reads long paths without help")
    assert reason == "PATH_TOO_LONG" and api.get_item(item_id)[10] == "ARCHIVED"
