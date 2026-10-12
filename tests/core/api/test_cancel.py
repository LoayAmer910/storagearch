"""core.api - cancelling an operation and following its progress.

A cancel that arrives before the item is recorded leaves nothing: no item, no archive row, no temporary
file, and the source untouched. From the moment the item is recorded the save stands; a cancel then only
keeps the source from being removed."""
import os
import threading

import pytest

from core import api, config, pipeline, store
from support.api import item_id_by_name, read, tree
from support.files import write_lines_file


def _folder(app, name="work", count=8, seed=0):
    folder = app / name
    for i in range(count):
        write_lines_file(folder, f"sub{i % 3}/f{i}.txt", 3000, seed=seed + i)
    return folder


def _nothing_was_saved(app):
    assert api.search("") == [] and api.list_archives() == []
    archives = config.archives_dir()
    assert [f for _d, _s, fs in os.walk(archives) for f in fs] == []
    assert os.listdir(config.temp_dir()) == []
    assert set(api.verify_all().get("Docs", {}).values()) <= {"VERIFIED"}


def _cancel_at(monkeypatch, owner, name, call_number):
    """Cancels the running operation just before the `call_number`-th call of owner.name, then lets it run."""
    real, calls, lock = getattr(owner, name), [], threading.Lock()

    def wrapper(*args, **kwargs):
        with lock:
            calls.append(1)
            if len(calls) == call_number:
                assert api.cancel_current_operation() is True
        return real(*args, **kwargs)
    monkeypatch.setattr(owner, name, wrapper)
    return calls


def test_a_cancel_with_nothing_running_does_nothing_and_does_not_reach_the_next_operation(app):
    assert api.cancel_current_operation() is False
    assert api.operation_progress() is None
    src = write_lines_file(app, "notes.txt")
    assert api.archive_file_result(src, "Docs")["success"]


@pytest.mark.parametrize("source_action", ["keep", "delete"])
def test_a_folder_cancelled_while_it_is_read_is_not_archived(app, monkeypatch, source_action):
    folder = _folder(app)
    before = tree(str(folder))
    _cancel_at(monkeypatch, api, "scan_file", 3)
    result = api.archive_folder_result(str(folder), "Docs", source_action)
    assert (result["success"], result["reason"]) == (False, "CANCELLED")
    _nothing_was_saved(app)
    assert tree(str(folder)) == before


@pytest.mark.parametrize("source_action", ["keep", "delete"])
def test_a_folder_cancelled_while_it_is_compressed_is_not_archived(app, monkeypatch, source_action):
    folder = _folder(app)
    before = tree(str(folder))
    _cancel_at(monkeypatch, pipeline.Pipeline, "decide", 2)
    result = api.archive_folder_result(str(folder), "Docs", source_action)
    assert (result["success"], result["reason"]) == (False, "CANCELLED")
    assert "cancel" in result["message"].lower()
    _nothing_was_saved(app)
    assert tree(str(folder)) == before


def test_a_file_cancelled_while_it_is_compressed_is_not_archived(app, monkeypatch):
    src = write_lines_file(app, "notes.txt", 5000)
    _cancel_at(monkeypatch, pipeline.Pipeline, "decide", 1)
    result = api.archive_file_result(src, "Docs", "delete")
    assert (result["success"], result["reason"]) == (False, "CANCELLED")
    _nothing_was_saved(app)
    assert os.path.exists(src)


def test_a_cancel_just_before_the_write_still_leaves_nothing(app, monkeypatch):
    folder = _folder(app)
    _cancel_at(monkeypatch, api, "_choose_archive", 1)
    result = api.archive_folder_result(str(folder), "Docs")
    assert (result["success"], result["reason"]) == (False, "CANCELLED")
    _nothing_was_saved(app)


@pytest.mark.parametrize("source_action", ["delete", "recycle"])
def test_a_cancel_after_the_item_is_recorded_keeps_the_save_and_keeps_the_source(app, monkeypatch, source_action):
    folder = _folder(app)
    before = tree(str(folder))
    _cancel_at(monkeypatch, api, "_after_recorded", 1)
    result = api.archive_folder_result(str(folder), "Docs", source_action)
    assert result["success"] and "cancel" in result["message"].lower()
    assert tree(str(folder)) == before, "the source was removed although the operation was cancelled"
    item_id = item_id_by_name("work")
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == before


def test_the_operation_after_a_cancelled_one_runs_normally(app, monkeypatch):
    folder = _folder(app)
    with monkeypatch.context() as cancelling:
        _cancel_at(cancelling, pipeline.Pipeline, "decide", 2)
        assert api.archive_folder_result(str(folder), "Docs")["reason"] == "CANCELLED"
    result = api.archive_folder_result(str(folder), "Docs", "delete")
    assert result["success"] and not os.path.exists(folder)
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}


def test_a_checkin_cancelled_before_it_is_saved_changes_nothing(app, monkeypatch):
    folder = _folder(app)
    original = tree(str(folder))
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("work")
    ws = api.checkout(item_id)
    write_lines_file(ws, "sub0/new.txt", 4000, seed=99)
    edited = tree(ws)
    _cancel_at(monkeypatch, pipeline.Pipeline, "decide", 1)
    assert api.checkin_result(item_id) == {"success": False, "reason": "CANCELLED"}
    assert tree(ws) == edited and api.get_item(item_id).status == "CHECKED_OUT"
    assert os.listdir(config.temp_dir()) == [] and set(api.verify_all()["Docs"].values()) == {"VERIFIED"}
    # the archived version is still the one from before the edit
    members = api._cat().members_of_item(item_id)
    assert not any(m["relative_path"].endswith("new.txt") for m in members)
    assert len([m for m in members if m["kind"] == "file"]) == len([v for v in original.values() if v is not None])


def test_a_checkin_cancelled_too_late_is_simply_finished(app, monkeypatch):
    src = write_lines_file(app, "notes.txt")
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    ws = api.checkout(item_id)
    with open(ws, "ab") as f:
        f.write(b"edited\n")
    edited = read(ws)
    _cancel_at(monkeypatch, api, "_after_recorded", 1)
    assert api.checkin_result(item_id) == {"success": True, "reason": None}
    assert api.get_item(item_id).status == "ARCHIVED" and not os.path.exists(ws)
    assert read(api.checkout(item_id, destination=str(app / "back"))) == edited


@pytest.mark.parametrize("to_destination", [False, True])
def test_a_checkout_cancelled_half_way_leaves_nothing_behind(app, monkeypatch, to_destination):
    folder = _folder(app, count=12)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("work")
    destination = str(app / "out") if to_destination else None
    if destination:
        os.makedirs(destination)
    _cancel_at(monkeypatch, store, "_extract_file", 3)
    assert api.checkout_result(item_id, destination) == {"success": False, "path": None, "reason": "CANCELLED"}
    assert api.get_item(item_id).status == "ARCHIVED"
    left = os.listdir(destination) if destination else [d for d in os.listdir(config.workspace_dir())]
    assert left == [], left
    assert tree(api.checkout(item_id, destination)) == tree(str(folder))  # and it works the next time


def test_hashing_a_folder_for_the_duplicate_check_can_be_cancelled(app, monkeypatch):
    folder = _folder(app)
    _cancel_at(monkeypatch, store, "sha256_file", 3)
    with pytest.raises(api.OperationCancelled):
        api.calculate_folder_hash(str(folder))
    assert api.calculate_folder_hash(str(folder))  # the next call is not affected


def test_a_real_cancel_from_another_thread_never_leaves_a_half_saved_item(app):
    """No hooks: a cancel at an arbitrary moment. Whatever it interrupted, the result is one of the two whole states."""
    for round_number, delay in enumerate((0.0, 0.01, 0.05, 0.15, 0.4)):
        folder = _folder(app, name=f"work{round_number}", count=24, seed=round_number * 100)
        before = tree(str(folder))
        result = {}
        worker = threading.Thread(target=lambda: result.update(api.archive_folder_result(str(folder), "Docs", "delete")))
        worker.start()
        threading.Event().wait(delay)
        api.cancel_current_operation()
        worker.join(120)
        assert not worker.is_alive()
        found = api.search(f"work{round_number}")
        if result["success"]:
            assert len(found) == 1
            if "cancel" in result["message"].lower():
                assert tree(str(folder)) == before
        else:
            assert result["reason"] == "CANCELLED" and found == [] and tree(str(folder)) == before
        assert os.listdir(config.temp_dir()) == []
        assert set(api.verify_all().get("Docs", {}).values()) <= {"VERIFIED"}
        assert api.operation_progress() is None


# --- progress ----------------------------------------------------------------------------------------------

def test_the_progress_of_a_folder_counts_its_files(app, monkeypatch):
    folder = _folder(app, count=10)
    seen, real = [], pipeline.Pipeline.decide

    def watching(self, path, scanned=None):
        seen.append(api.operation_progress())
        return real(self, path, scanned)
    monkeypatch.setattr(pipeline.Pipeline, "decide", watching)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    assert seen and all(p["total"] == 10 and 0 <= p["done"] <= 10 and p["phase"] == "Compressing" for p in seen)
    assert api.operation_progress() is None


def test_the_progress_goes_through_reading_compressing_and_restoring(app, monkeypatch):
    folder = _folder(app, count=6)
    phases = []
    for owner, name in ((api, "scan_file"), (pipeline.Pipeline, "decide"), (store, "_extract_file")):
        real = getattr(owner, name)

        def watching(*args, _real=real, **kwargs):
            progress = api.operation_progress()
            phases.append((progress["phase"], progress["total"]))
            return _real(*args, **kwargs)
        monkeypatch.setattr(owner, name, watching)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    assert api.checkout(item_id_by_name("work"))
    assert {p for p, _t in phases} == {"Reading", "Compressing", "Restoring"} and {t for _p, t in phases} == {6}
