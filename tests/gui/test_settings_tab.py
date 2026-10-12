"""gui.settings_tab - storage locations and the cloud connection through the window."""
import os

import pytest
from PySide6.QtWidgets import QDialog

from core import api, api as main, config as storage_config, events
from support import corpus
from support.cloud import wait_for, FakeWorker
from support.gui import (
    app_is_healthy,
    archive_file_through_gui,
    checkin_through_gui,
    checkout_through_gui,
    content_after_checkout,
    last_message,
    no_crash_reports,
    wait_idle,
    workspace_of,
)


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


def test_storage_settings_persist_and_survive_restart(gui_window, sandbox):
    from gui.main_window import MainWindow

    new_root = os.path.join(str(sandbox), "persisted_data")
    new_workspace = os.path.join(str(sandbox), "persisted_workspace")
    assert main.set_storage_location(data_root=new_root, workspace_root=new_workspace) is True

    main.create_category("Persisted")

    gui_window.close()
    # brand-new process-equivalent reload: storage_config.load() re-reads from disk
    storage_config.load()
    main.initialize()
    window2 = MainWindow()

    locations = main.get_storage_locations()
    assert locations["data_root"] == new_root
    assert locations["workspace_root"] == new_workspace
    assert any(c[1] == "Persisted" for c in main.list_categories())

    window2._closing_in_progress = True
    window2.close()


def test_the_saved_default_archive_size_survives_a_restart(gui_window, message_boxes):
    from gui.main_window import MainWindow

    combo = gui_window.settings_tab.default_size_combo
    combo.setCurrentIndex(combo.findText("5 GB"))
    gui_window.settings_tab._save_default_size()
    assert last_message(message_boxes)[:3] == ("information", "Settings", "Default archive size saved.")

    window2 = MainWindow()
    try:
        assert window2.settings["default_max_archive_size"] == 5 * 1024 ** 3
        assert window2.settings_tab.default_size_combo.currentText() == "5 GB"
    finally:
        window2._closing_in_progress = True
        window2.close()


def test_settings_tab_change_data_location_through_gui(gui_window, monkeypatch, sandbox, message_boxes):
    new_root = os.path.join(str(sandbox), "gui_chosen_data")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: new_root))

    gui_window.settings_tab._change_data_root()

    assert main.get_storage_locations()["data_root"] == new_root
    assert os.path.isdir(os.path.join(new_root, "archives"))


def test_settings_tab_change_workspace_refused_with_pending_checkout(gui_window, monkeypatch, sandbox, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("held.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("held.txt")[0][0]
    main.checkout(item_id)

    new_workspace = os.path.join(str(sandbox), "gui_chosen_workspace")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: new_workspace))

    gui_window.settings_tab._change_workspace_root()

    assert main.get_storage_locations()["workspace_root"] != new_workspace
    assert any(call[0] == "warning" for call in message_boxes.calls)


def test_data_location_where_the_app_cannot_create_its_folders(gui_window, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    blocked = sandbox / "blocked"
    blocked.mkdir()
    (blocked / "archives").write_text("a file where a folder must go")
    message_boxes.warning_confirm = True
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: str(blocked)))
    before = api.get_storage_locations()
    gui_window.settings_tab._change_data_root()
    no_crash_reports(message_boxes)
    assert last_message(message_boxes)[0] == "warning", message_boxes.calls
    assert api.get_storage_locations() == before, "a failed switch must leave the old location in place"
    assert [c[1] for c in api.list_categories()] == ["Docs"]
    api.initialize()  # the next start must work too
    assert [c[1] for c in api.list_categories()] == ["Docs"]


@pytest.mark.parametrize("where", ["temp", "archives", "data_root"])
def test_workspace_inside_the_apps_own_folders(gui_window, qtbot, monkeypatch, message_boxes, sandbox, where):
    api.create_category("Docs")
    target = {"temp": os.path.join(api.data_root(), "temp", "ws"), "archives": os.path.join(api.data_root(), "archives"),
              "data_root": api.data_root()}[where]
    os.makedirs(target, exist_ok=True)
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: target))
    gui_window.settings_tab._change_workspace_root()
    no_crash_reports(message_boxes)
    doc = sandbox / "work.txt"
    doc.write_bytes(corpus.lines(900, "{i} {w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    checkout_through_gui(gui_window, qtbot, "work.txt")
    ws = workspace_of("work.txt")
    with open(ws, "ab") as f:
        f.write(b"an hour of work\n")
    api.initialize()  # the app is restarted before checking in
    assert os.path.exists(ws), "restarting the app deleted the user's checked-out edits"
    checkin_through_gui(gui_window, qtbot, "work.txt")
    assert open(content_after_checkout("work.txt", sandbox), "rb").read().endswith(b"an hour of work\n")
    app_is_healthy(gui_window, message_boxes)


def test_a_disconnect_that_could_not_finish_is_reported_not_hidden(gui_window, message_boxes, monkeypatch):
    from gui.settings_tab import DISCONNECT_FAILED_MESSAGE
    tab = gui_window.settings_tab
    monkeypatch.setattr(api, "cloud_disconnect", lambda: False)
    message_boxes.warning_confirm = True
    tab._disconnect_cloud()
    assert message_boxes.calls[-1] == ("warning", "Disconnect", DISCONNECT_FAILED_MESSAGE)
    assert "another program" in DISCONNECT_FAILED_MESSAGE and "Try again" in DISCONNECT_FAILED_MESSAGE


def test_a_disconnect_that_finished_shows_no_warning(gui_window, message_boxes, monkeypatch):
    tab = gui_window.settings_tab
    monkeypatch.setattr(api, "cloud_disconnect", lambda: True)
    message_boxes.warning_confirm = True
    tab._disconnect_cloud()
    assert [c for c in message_boxes.calls if c[0] == "warning"] == [
        ("warning", "Disconnect", "This clears the local Cloud connection and turns Telemetry off. Continue?")]


def test_protected_or_busy_locations_are_refused_in_settings(gui_window, monkeypatch, message_boxes, sandbox, qtbot):
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: windows))
    gui_window.settings_tab._change_workspace_root()
    assert "protected" in last_message(message_boxes)[2].lower()
    gui_window.settings_tab._change_data_root()
    assert "protected" in last_message(message_boxes)[2].lower()
    api.create_category("Docs")
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    api.checkout(api.search("a.txt")[0][0])
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: str(sandbox / "ws2")))
    gui_window.settings_tab._change_workspace_root()
    assert "checked out" in last_message(message_boxes)[2]
    assert api.get_storage_locations()["workspace_root"] is None


def test_the_cloud_from_the_settings_screen(cloud_server, gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    tab = gui_window.settings_tab
    _type_beta_code(monkeypatch, "")
    tab._connect_cloud()
    assert message_boxes.calls == []  # an empty code is simply not sent
    _type_beta_code(monkeypatch, "not-the-code")
    tab._connect_cloud()
    assert "Could not connect" in last_message(message_boxes)[2] and not api.cloud_is_registered()
    _type_beta_code(monkeypatch, "VALID-CODE")
    tab._connect_cloud()
    assert "Connected" in last_message(message_boxes)[2] and api.cloud_is_registered()

    tab.telemetry_checkbox.setChecked(True)
    assert api.cloud_telemetry_enabled()
    api.create_category("Docs")
    doc = sandbox / "private name.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    events.flush(5)
    assert wait_for(lambda: cloud_server.posts("/api/v1/telemetry"))
    assert not any("private name" in str(p["body"]) for p in cloud_server.posts("/api/v1/telemetry"))

    _fill_feedback(monkeypatch, "x" * 2001)
    tab._send_feedback()
    assert "too long" in message_boxes.calls[-1][2]
    _fill_feedback(monkeypatch, "Works well", rating=5)
    tab._send_feedback()
    assert "Thank you" in last_message(message_boxes)[2]

    tab._check_for_updates()
    wait_idle(qtbot, gui_window)
    assert "No release information" in last_message(message_boxes)[2]  # an unsigned answer is not trusted

    cloud_server.mode = "slow"
    import time
    start = time.monotonic()
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(sandbox / "private name.txt"), "Docs")  # duplicate
    assert time.monotonic() - start < 4, "a hanging server slowed the window down"

    message_boxes.warning_confirm = True
    cloud_server.mode = "ok"
    tab._disconnect_cloud()
    assert not api.cloud_is_registered() and not api.cloud_telemetry_enabled()


# --- the cloud part when there is no cloud, or the user changes their mind ------------------------------

@pytest.fixture
def no_cloud(monkeypatch):
    monkeypatch.setattr(main, "cloud_available", lambda: False)


def test_without_the_cloud_its_controls_are_off_and_its_actions_explain_why(gui_window, no_cloud, message_boxes):
    tab = gui_window.settings_tab
    tab.refresh()
    assert tab.cloud_status_label.text() == "Cloud is not available in this build."
    assert not any(w.isEnabled() for w in (tab.telemetry_checkbox, tab.cloud_connect_button, tab.cloud_disconnect_button))
    tab._send_feedback()
    assert last_message(message_boxes)[:3] == ("information", "Send Feedback", "Cloud is not available in this build.")
    tab._check_for_updates()
    assert last_message(message_boxes)[:3] == ("information", "Check for Updates", "Cloud is not available in this build.")


@pytest.mark.parametrize("answer", ["cancel", "empty"])
def test_connecting_without_a_code_does_nothing(gui_window, monkeypatch, message_boxes, answer):
    import gui.dialogs as dialogs
    registered = []
    monkeypatch.setattr(main, "cloud_register", lambda code: registered.append(code) or (True, None))
    if answer == "cancel":
        monkeypatch.setattr(dialogs.BetaCodeDialog, "exec", lambda dialog: QDialog.Rejected)
    else:
        _type_beta_code(monkeypatch, "   ")
    gui_window.settings_tab._connect_cloud()
    assert registered == [] and message_boxes.calls == []


@pytest.mark.parametrize("answer", ["cancel code", "empty code", "cancel feedback"])
def test_feedback_without_a_connection_can_be_abandoned_at_every_step(gui_window, monkeypatch, message_boxes, answer):
    import gui.dialogs as dialogs
    sent = []
    monkeypatch.setattr(main, "cloud_available", lambda: True)
    monkeypatch.setattr(main, "cloud_is_registered", lambda: False)
    monkeypatch.setattr(main, "cloud_submit_feedback", lambda *a, **k: sent.append(1) or (True, None))
    if answer == "cancel code":
        monkeypatch.setattr(dialogs.BetaCodeDialog, "exec", lambda dialog: QDialog.Rejected)
    else:
        _type_beta_code(monkeypatch, "" if answer == "empty code" else "BETA-CODE")
        monkeypatch.setattr(dialogs.FeedbackDialog, "exec", lambda dialog: QDialog.Rejected)
    gui_window.settings_tab._send_feedback()
    assert sent == [] and message_boxes.calls == []


def test_feedback_that_cannot_be_queued_is_explained(gui_window, monkeypatch, message_boxes):
    monkeypatch.setattr(main, "cloud_available", lambda: True)
    monkeypatch.setattr(main, "cloud_is_registered", lambda: True)
    monkeypatch.setattr(main, "cloud_submit_feedback", lambda *a, **k: (False, "queue_full"))
    _fill_feedback(monkeypatch, "it crashed", rating=2)
    gui_window.settings_tab._send_feedback()
    assert last_message(message_boxes)[:3] == ("warning", "Send Feedback", "Could not queue feedback: queue_full")


def test_a_critical_release_is_marked_critical_and_never_installed(gui_window, qtbot, monkeypatch, message_boxes):
    monkeypatch.setattr(main, "cloud_available", lambda: True)
    monkeypatch.setattr(main, "cloud_check_for_updates",
                        lambda: {"app_version": "2.1.0", "build_version": "42", "severity": "critical"})
    gui_window.settings_tab._check_for_updates()
    wait_idle(qtbot, gui_window)
    text = last_message(message_boxes)[2]
    assert text.startswith("Latest release: 2.1.0 (build 42) (CRITICAL)")
    assert "never downloads or installs updates automatically" in text


def test_a_slow_update_check_does_not_freeze_the_window(gui_window, qtbot, monkeypatch, message_boxes):
    import threading
    import time
    answer = threading.Event()
    monkeypatch.setattr(main, "cloud_available", lambda: True)
    monkeypatch.setattr(main, "cloud_check_for_updates", lambda: answer.wait(20) and None)
    before = len(message_boxes.calls)
    start = time.monotonic()
    gui_window.settings_tab._check_for_updates()
    assert time.monotonic() - start < 1, "the window waited for the server"
    assert gui_window.is_busy() and len(message_boxes.calls) == before
    gui_window.settings_tab._check_for_updates()  # a second click while it is checking starts nothing
    answer.set()
    wait_idle(qtbot, gui_window)
    assert len(message_boxes.calls) == before + 1 and "No release information" in last_message(message_boxes)[2]


def test_nothing_the_user_reads_in_the_window_calls_this_release_a_beta_or_a_development_build(gui_window, monkeypatch):
    from PySide6.QtWidgets import QAbstractButton, QGroupBox, QLabel

    import gui.dialogs as dialogs
    monkeypatch.setattr(main, "cloud_available", lambda: True)
    texts = []
    for registered in (False, True):
        monkeypatch.setattr(main, "cloud_is_registered", lambda registered=registered: registered)
        gui_window.settings_tab.refresh()
        for window in (gui_window, dialogs.BetaCodeDialog(gui_window), dialogs.FeedbackDialog(gui_window)):
            texts.append(window.windowTitle())
            texts += [w.text() for w in window.findChildren(QLabel)] + [w.text() for w in window.findChildren(QAbstractButton)]
            texts += [w.title() for w in window.findChildren(QGroupBox)]
    assert len(texts) > 40
    wrong = [t for t in texts if "beta" in t.lower() or "development" in t.lower()]
    assert wrong == [], wrong


# --- storage locations: changing your mind, and the workspace reset --------------------------------------

def test_cancelling_the_folder_choice_changes_no_location(gui_window, monkeypatch, message_boxes):
    before = main.get_storage_locations()
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: ""))
    gui_window.settings_tab._change_data_root()
    gui_window.settings_tab._change_workspace_root()
    assert main.get_storage_locations() == before and message_boxes.calls == []


def test_saying_no_to_leaving_a_location_with_data_keeps_it(gui_window, monkeypatch, message_boxes, sandbox):
    main.create_category("Docs")
    before = main.get_storage_locations()
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory",
                        staticmethod(lambda *a, **k: str(sandbox / "elsewhere")))
    message_boxes.warning_confirm = False
    gui_window.settings_tab._change_data_root()
    assert main.get_storage_locations() == before
    assert "already has categories/items in it" in last_message(message_boxes)[2]


def test_a_workspace_the_core_refuses_is_explained(gui_window, monkeypatch, message_boxes, sandbox):
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory",
                        staticmethod(lambda *a, **k: str(sandbox / "ws")))
    monkeypatch.setattr(main, "set_storage_location", lambda **k: False)
    gui_window.settings_tab._change_workspace_root()
    assert last_message(message_boxes)[:3] == (
        "warning", "Change workspace location", "Could not switch to the selected location.")


def test_resetting_the_workspace_returns_it_to_the_default(gui_window, monkeypatch, message_boxes, sandbox):
    default = main.get_storage_locations()["workspace_root"]
    assert main.set_storage_location(workspace_root=str(sandbox / "custom_ws"))
    gui_window.settings_tab._reset_workspace_root()
    assert last_message(message_boxes)[:3] == (
        "information", "Reset workspace location", "Workspace location reset to the default.")
    assert main.get_storage_locations()["workspace_root"] == default
    assert gui_window.settings_tab.workspace_root_label.text() == os.path.abspath(storage_config.workspace_dir())


def test_the_workspace_is_not_reset_while_items_are_checked_out(gui_window, make_file, message_boxes):
    main.create_category("Docs")
    main.archive_file(make_file("a.txt"), "Docs")
    main.checkout(main.search("a.txt")[0][0])
    before = main.get_storage_locations()["workspace_root"]
    gui_window.settings_tab._reset_workspace_root()
    assert "1 item(s) are still checked out" in last_message(message_boxes)[2]
    assert main.get_storage_locations()["workspace_root"] == before


def test_a_workspace_reset_the_core_refuses_is_explained(gui_window, monkeypatch, message_boxes):
    monkeypatch.setattr(main, "reset_workspace_location", lambda: False)
    gui_window.settings_tab._reset_workspace_root()
    assert last_message(message_boxes)[:3] == (
        "warning", "Reset workspace location", "Could not reset the workspace location.")


def test_location_buttons_do_nothing_while_an_operation_runs(gui_window, monkeypatch, message_boxes):
    before = main.get_storage_locations()
    monkeypatch.setattr("gui.settings_tab.QFileDialog", None)  # opening a folder picker now would fail the test
    gui_window.set_busy(True, "Archiving...")
    for action in (gui_window.settings_tab._change_data_root, gui_window.settings_tab._change_workspace_root,
                   gui_window.settings_tab._reset_workspace_root):
        action()
    assert main.get_storage_locations() == before and message_boxes.calls == []
