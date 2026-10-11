"""core.api - the log file: what goes wrong is written down in the data folder, and the file is never held open."""
import logging
import os

from core import api, config, store


def _log_path():
    return os.path.join(config.log_dir(), "storagearch.log")


def _log_text():
    with open(_log_path(), encoding="utf-8") as f:
        return f.read()


def test_a_failure_the_user_only_sees_as_a_reason_is_written_to_the_log_file(app, monkeypatch, make_file):
    def broken(*args, **kwargs):
        raise store.StoreError("the internal detail of the failure")
    monkeypatch.setattr(store, "write_members", broken)
    assert api.archive_file_result(make_file("a.txt"), "Docs")["reason"] == "WRITE_FAILED"
    text = _log_text()
    assert "ERROR" in text and "the internal detail of the failure" in text


def test_an_unexpected_error_is_logged_with_where_it_happened(app, monkeypatch, make_file):
    def broken(*args, **kwargs):
        raise RuntimeError("something nobody expected")
    monkeypatch.setattr(store, "write_members", broken)
    assert api.archive_file_result(make_file("a.txt"), "Docs")["reason"] == "UNEXPECTED_ERROR"
    text = _log_text()
    assert "something nobody expected" in text and "Traceback" in text


def test_ordinary_work_writes_no_log_file(app, make_file):
    assert api.archive_file_result(make_file("a.txt"), "Docs")["success"]
    assert not os.path.exists(_log_path())


def test_the_log_file_is_not_held_open_and_follows_the_data_folder(app, tmp_path):
    api.log.error("first")
    os.rename(config.log_dir(), config.log_dir() + "_moved")  # nothing holds the file: the folder can be moved
    elsewhere = tmp_path / "elsewhere"
    assert api.set_storage_location(data_root=str(elsewhere))
    api.log.error("second")
    assert os.path.dirname(_log_path()).startswith(str(elsewhere)) and "second" in _log_text() and "first" not in _log_text()
    assert len([h for h in api.log.handlers if isinstance(h, api._LogFile)]) == 1  # starting again adds no second writer


def test_a_full_log_file_starts_again_and_keeps_one_old_copy(app, monkeypatch):
    monkeypatch.setattr(api, "_LOG_LIMIT", 2000)
    for i in range(100):
        api.log.warning("line %03d %s", i, "x" * 100)
    assert os.path.getsize(_log_path()) <= 2000 + 200
    assert sorted(os.listdir(config.log_dir())) == ["storagearch.log", "storagearch.log.1"]
    assert "line 099" in _log_text()


def test_a_log_that_cannot_be_written_never_breaks_the_operation(app, monkeypatch, make_file):
    os.rmdir(config.log_dir())
    with open(config.log_dir(), "w") as f:  # a file where the folder should be: every write to the log fails
        f.write("in the way")
    api.log.error("lost, and that is fine")
    assert api.archive_file_result(make_file("a.txt"), "Docs")["success"]
    assert logging.getLogger("storagearch").handlers


def test_an_unhandled_error_of_the_app_is_written_to_the_log_file(app, monkeypatch):
    import sys

    import app as app_module
    monkeypatch.setattr(sys, "excepthook", lambda *info: None)
    app_module._install_crash_hook(api)
    try:
        raise ZeroDivisionError("the app fell over")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())
    text = _log_text()
    assert "CRITICAL unhandled error" in text and "ZeroDivisionError: the app fell over" in text
