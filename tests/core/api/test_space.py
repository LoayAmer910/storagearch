"""core.api - free space: asked before the work starts, and on the drive that is really written to."""
import os

from core import api, config
from support.api import item_id_by_name, read, tree
from support.files import write_lines_file

PLENTY = 10 ** 12


def _drives(monkeypatch, free_under, elsewhere=PLENTY):
    """Makes every folder under a key of `free_under` look like a drive of its own with that many free bytes.
    Like Windows, the answer for a path that does not exist is an error."""
    real = api.shutil.disk_usage
    asked = []

    def fake(path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        asked.append(str(path))
        where = os.path.normcase(os.path.abspath(path))
        for folder, free in free_under.items():
            if (where + os.sep).startswith(os.path.normcase(os.path.abspath(folder)) + os.sep):
                return real(path)._replace(free=free)
        return real(path)._replace(free=elsewhere)
    monkeypatch.setattr(api.shutil, "disk_usage", fake)
    return asked


def _folder(app, name="work", count=6):
    folder = app / name
    for i in range(count):
        write_lines_file(folder, f"sub{i % 2}/f{i}.txt", 3000, seed=i)
    return folder


def _never(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"{name} started although there is no room for the result")
    return refuse


# --- check out: the drive of the destination ---------------------------------------------------------------

def test_a_checkout_to_a_full_drive_is_refused_before_anything_is_written(app, monkeypatch):
    src = write_lines_file(app, "notes.txt", 5000)
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    other_drive = app / "other_drive"
    other_drive.mkdir()
    _drives(monkeypatch, {str(other_drive): 1024})
    destination = other_drive / "restored" / "here"  # does not exist yet: the drive is the one of its nearest parent
    assert api._checkout(item_id, str(destination)) == (None, "INSUFFICIENT_SPACE")
    assert os.listdir(other_drive) == [] and api.get_item(item_id)[10] == "ARCHIVED"


def test_a_checkout_to_another_drive_does_not_need_its_whole_size_on_the_data_drive(app, monkeypatch):
    folder = _folder(app)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("work")
    sizes = [os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(folder) for f in fs]
    other_drive = app / "other_drive"
    other_drive.mkdir()
    # the data drive has room for the largest file (what a restore may need in the temporary folder), not for all
    _drives(monkeypatch, {config.data_root(): api._SPACE_MARGIN + max(sizes) + 1})
    assert sum(sizes) > max(sizes) + 1
    path, reason = api._checkout(item_id, str(other_drive))
    assert reason is None and tree(path) == tree(str(folder))


def test_a_checkout_into_the_workspace_still_needs_its_size_there(app, monkeypatch):
    src = write_lines_file(app, "notes.txt", 5000)
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    _drives(monkeypatch, {config.workspace_dir(): api._SPACE_MARGIN + os.path.getsize(src) - 1})
    assert api._checkout(item_id, None) == (None, "INSUFFICIENT_SPACE")
    _drives(monkeypatch, {config.workspace_dir(): api._SPACE_MARGIN + os.path.getsize(src)})
    path, reason = api._checkout(item_id, None)
    assert reason is None and read(path) == read(src)


# --- archive and check in: asked before the compression starts -----------------------------------------------

def test_a_folder_is_not_compressed_when_there_is_no_room_for_it(app, monkeypatch):
    folder = _folder(app)
    _drives(monkeypatch, {config.data_root(): api._SPACE_MARGIN + 1024})
    monkeypatch.setattr(api, "_decide_many", _never("compressing the folder"))
    result = api.archive_folder_result(str(folder), "Docs")
    assert (result["success"], result["reason"]) == (False, "INSUFFICIENT_SPACE")
    assert api.search("") == [] and os.listdir(config.temp_dir()) == []


def test_a_file_is_not_compressed_when_there_is_no_room_for_it(app, monkeypatch):
    src = write_lines_file(app, "notes.txt", 5000)
    _drives(monkeypatch, {config.data_root(): api._SPACE_MARGIN + 1024})
    monkeypatch.setattr(api, "_new_pipeline", _never("compressing the file"))
    result = api.archive_file_result(src, "Docs")
    assert (result["success"], result["reason"]) == (False, "INSUFFICIENT_SPACE")
    assert api.search("") == []


def test_a_checkin_is_not_compressed_when_there_is_no_room_for_it(app, monkeypatch):
    folder = _folder(app)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("work")
    ws = api.checkout(item_id)
    write_lines_file(ws, "sub0/new.txt", 4000, seed=99)
    _drives(monkeypatch, {config.data_root(): api._SPACE_MARGIN + 1024})
    monkeypatch.setattr(api, "_decide_many", _never("compressing the changed files"))
    assert api.checkin_result(item_id) == {"success": False, "reason": "INSUFFICIENT_SPACE"}
    assert os.path.exists(os.path.join(ws, "sub0", "new.txt")) and api.get_item(item_id)[10] == "CHECKED_OUT"


def test_the_estimate_is_the_plain_zip_size_so_compressible_content_is_not_turned_away(app, monkeypatch):
    src = write_lines_file(app, "notes.txt", 20000)
    size = os.path.getsize(src)
    _drives(monkeypatch, {config.data_root(): api._SPACE_MARGIN + size // 2})  # half its size is free; it compresses well
    assert api.archive_file_result(src, "Docs")["success"]
