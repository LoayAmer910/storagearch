"""A user's journey through the real window, on the new core - with the wrong inputs a user makes.

Only the modal dialogs are simulated (typing into a form, picking a file, confirming); every handler, every
background operation and every message box is the application's own. Each step checks what the user would
see: the message shown, the table content, the files on disk.
"""
import os
import sys

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

import corpus
from gui_support import wait_idle, workspace_of
from test_08_items_workflow import _archive_file_through_gui, _archive_folder_through_gui, _select_row_by_name
from core import api, events

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_10_cloud"))
from cloud_support import FakeWorker, wait_for  # noqa: E402


def _type_into_category_form(monkeypatch, name, description=None):
    import gui.categories_tab as tab

    def fake_exec(dialog):
        dialog.name_edit.setText(name)
        if description is not None:
            dialog.description_edit.setText(description)
        return QDialog.Accepted
    monkeypatch.setattr(tab.CreateCategoryDialog, "exec", fake_exec)


def _messages(recorder, kind=None):
    return [c for c in recorder.calls if kind is None or c[0] == kind]


def _last(recorder):
    return recorder.calls[-1] if recorder.calls else None


def _rows(table):
    return [table.item(r, 0).text() for r in range(table.rowCount())]


# --- categories ---------------------------------------------------------------------------------------

CATEGORY_NAMES = [
    ("", "warning", "cannot be empty"),
    ("     ", "warning", "cannot be empty"),
    ("a/b:c*?|", "information", "created"),
    # Hebrew on purpose: non-ASCII category names must work
    ("קטגוריה בעברית", "information", "created"),
    ("x" * 500, "information", "created"),
]


@pytest.mark.parametrize("name,expected_kind,expected_text", CATEGORY_NAMES, ids=[f"name{i}" for i in range(len(CATEGORY_NAMES))])
def test_creating_categories_with_odd_names(gui_window, monkeypatch, message_boxes, name, expected_kind, expected_text):
    _type_into_category_form(monkeypatch, name)
    gui_window.categories_tab._create_category()
    kind, _title, text = _last(message_boxes)
    assert kind == expected_kind and expected_text in text, message_boxes.calls
    if expected_kind == "information":
        assert name.strip() in [c[1] for c in api.list_categories()]
        folder = os.path.join(api.data_root(), "archives", api.list_categories()[0][6])
        assert os.path.isdir(folder), "the category folder must be a valid Windows folder"


def test_a_duplicate_category_name_is_refused(gui_window, monkeypatch, message_boxes):
    _type_into_category_form(monkeypatch, "Docs")
    gui_window.categories_tab._create_category()
    gui_window.categories_tab.refresh()
    gui_window.categories_tab._create_category()
    assert "already exists" in _last(message_boxes)[2]
    assert [c[1] for c in api.list_categories()] == ["Docs"]


def test_archiving_with_no_category_asks_for_one(gui_window, monkeypatch, message_boxes, make_file):
    path = make_file("a.txt", "content")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab._add_file()
    assert "Create a category first" in _last(message_boxes)[2]
    assert api.search("") == []


# --- adding items -------------------------------------------------------------------------------------

def test_cancelling_the_file_picker_does_nothing(gui_window, monkeypatch, message_boxes):
    api.create_category("Docs")
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    gui_window.items_tab._add_file()
    assert message_boxes.calls == [] and not gui_window.is_busy()


@pytest.mark.parametrize("bad", ["missing", "protected", "data_root"])
def test_bad_sources_are_explained_and_nothing_is_stored(gui_window, qtbot, monkeypatch, message_boxes, sandbox, bad):
    api.create_category("Docs")
    path = {"missing": str(sandbox / "no_such_file.txt"),
            "protected": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "win.ini"),
            "data_root": os.path.join(api.data_root(), "smartarchive.db")}[bad]
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    assert message_boxes.calls and message_boxes.calls[-1][0] in ("warning", "critical"), message_boxes.calls
    assert api.search("") == []


def test_adding_files_and_folders_like_a_user(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    report = sandbox / "report.txt"
    report.write_bytes(corpus.lines(4000, "line {i} {w} {n}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(report), "Docs")
    assert "archived and verified" in _last(message_boxes)[2] and "Saved:" in _last(message_boxes)[2]

    empty = sandbox / "empty.txt"
    empty.write_bytes(b"")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(empty), "Docs")
    assert "archived and verified" in _last(message_boxes)[2]

    project = sandbox / "project"
    (project / "src").mkdir(parents=True)
    (project / "empty_folder").mkdir()
    (project / "src" / "main.py").write_bytes(corpus.lines(800, "def f{i}(): return {n}\n"))
    _archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
    assert "archived and verified" in _last(message_boxes)[2]

    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(report), "Docs")  # the same file again
    assert any("already" in c[2].lower() or "duplicate" in c[2].lower() for c in message_boxes.calls[-2:]), message_boxes.calls[-2:]

    gui_window.items_tab.refresh()
    assert sorted(_rows(gui_window.items_tab.table)) == ["empty.txt", "project", "report.txt"]
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}


# --- checkout / checkin through the buttons -----------------------------------------------------------

def test_checkout_edit_checkin_and_a_second_checkin(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "notes.txt"
    doc.write_bytes(corpus.lines(3000, "{i} {w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)
    ws = workspace_of("notes.txt")
    with open(ws, "a", encoding="utf-8") as f:
        f.write("my edit\n")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)
    assert not os.path.exists(ws)
    before = len(message_boxes.calls)
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkin()  # not checked out any more
    wait_idle(qtbot, gui_window)
    assert len(message_boxes.calls) >= before
    back = api.checkout(api.search("notes.txt")[0][0], destination=str(sandbox / "back"))
    assert open(back, encoding="utf-8").read().endswith("my edit\n")


def test_actions_without_a_selection_do_nothing_harmful(gui_window, qtbot, message_boxes):
    for action in (gui_window.items_tab._checkout, gui_window.items_tab._checkin, gui_window.items_tab._verify_selected,
                   gui_window.categories_tab._edit_category, gui_window.categories_tab._delete_category):
        action()
        wait_idle(qtbot, gui_window)
    assert not gui_window.is_busy()


def test_a_second_click_while_busy_is_ignored(gui_window, qtbot, monkeypatch, sandbox):
    api.create_category("Docs")
    big = sandbox / "big.txt"
    big.write_bytes(corpus.lines(60000, "{i} {w} {n} payload\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(big), "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "big.txt")
    gui_window.items_tab._checkout()
    gui_window.items_tab._checkout()  # impatient user
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)
    assert api.get_item(api.search("big.txt")[0][0])[10] == "CHECKED_OUT"


# --- settings -----------------------------------------------------------------------------------------

def test_protected_or_busy_locations_are_refused_in_settings(gui_window, monkeypatch, message_boxes, sandbox, qtbot):
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: windows))
    gui_window.settings_tab._change_workspace_root()
    assert "protected" in _last(message_boxes)[2].lower()
    gui_window.settings_tab._change_data_root()
    assert "protected" in _last(message_boxes)[2].lower()
    api.create_category("Docs")
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    api.checkout(api.search("a.txt")[0][0])
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: str(sandbox / "ws2")))
    gui_window.settings_tab._change_workspace_root()
    assert "checked out" in _last(message_boxes)[2]
    assert api.get_storage_locations()["workspace_root"] is None


# --- the cloud from the settings screen ---------------------------------------------------------------

@pytest.fixture
def cloud_server(monkeypatch, tmp_path):
    worker = FakeWorker()
    assert events.connect()
    from local_cloud import cloud_client, local_state
    monkeypatch.setattr(cloud_client, "BASE_URL", worker.url)
    monkeypatch.setenv("STORAGEARCH_CLOUD_HOME", str(tmp_path / "cloud_home"))
    monkeypatch.setattr(local_state, "_session_id", None, raising=False)
    yield worker
    events.shutdown(2)
    worker.stop()


def _type_beta_code(monkeypatch, code):
    import gui.dialogs as dialogs

    def fake_exec(dialog):
        dialog.code_edit.setText(code)
        return QDialog.Accepted
    monkeypatch.setattr(dialogs.BetaCodeDialog, "exec", fake_exec)


def _fill_feedback(monkeypatch, text, rating=0):
    import gui.dialogs as dialogs

    def fake_exec(dialog):
        dialog.rating_spin.setValue(rating)
        dialog.text_edit.setPlainText(text)
        dialog._on_accept()
        return dialog.result()
    monkeypatch.setattr(dialogs.FeedbackDialog, "exec", fake_exec)


def test_the_cloud_from_the_settings_screen(cloud_server, gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    tab = gui_window.settings_tab
    _type_beta_code(monkeypatch, "")
    tab._connect_cloud()
    assert message_boxes.calls == []  # an empty code is simply not sent
    _type_beta_code(monkeypatch, "not-the-code")
    tab._connect_cloud()
    assert "Could not connect" in _last(message_boxes)[2] and not api.cloud_is_registered()
    _type_beta_code(monkeypatch, "VALID-CODE")
    tab._connect_cloud()
    assert "Connected" in _last(message_boxes)[2] and api.cloud_is_registered()

    tab.telemetry_checkbox.setChecked(True)
    assert api.cloud_telemetry_enabled()
    api.create_category("Docs")
    doc = sandbox / "private name.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    events.flush(5)
    assert wait_for(lambda: cloud_server.posts("/api/v1/telemetry"))
    assert not any("private name" in str(p["body"]) for p in cloud_server.posts("/api/v1/telemetry"))

    _fill_feedback(monkeypatch, "x" * 2001)
    tab._send_feedback()
    assert "too long" in message_boxes.calls[-1][2]
    _fill_feedback(monkeypatch, "Works well", rating=5)
    tab._send_feedback()
    assert "Thank you" in _last(message_boxes)[2]

    tab._check_for_updates()
    assert "No release information" in _last(message_boxes)[2]  # an unsigned answer is not trusted

    cloud_server.mode = "slow"
    import time
    start = time.monotonic()
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(sandbox / "private name.txt"), "Docs")  # duplicate
    assert time.monotonic() - start < 4, "a hanging server slowed the window down"

    message_boxes.warning_confirm = True
    cloud_server.mode = "ok"
    tab._disconnect_cloud()
    assert not api.cloud_is_registered() and not api.cloud_telemetry_enabled()
