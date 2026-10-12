"""gui.main_window - closing, restarting, and every button while an operation runs."""
import os

from PySide6.QtCore import QEvent

from core import api, api as main
from gui.dialogs import PendingCheckoutsDialog
from support import corpus
from support.gui import (
    app_is_healthy,
    archive_file_through_gui,
    item_status,
    no_crash_reports,
    select_row_by_name,
    set_fake_source_action,
    type_category_name,
    wait_idle,
    FakeSourceActionDialog,
)


class FakePendingDialogLeave:
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return self.Accepted

    def result_action(self):
        return PendingCheckoutsDialog.LEAVE_CHECKED_OUT

    def selected_item_ids(self):
        return []


class FakePendingDialogReturnAll:
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, pending_items=(), **k):
        self._ids = [item[0] for item in pending_items]

    def exec(self):
        return self.Accepted

    def result_action(self):
        return PendingCheckoutsDialog.RETURN_SELECTED

    def selected_item_ids(self):
        return self._ids


class FakePendingDialogCancel:
    Rejected = 0
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return self.Rejected

    def result_action(self):
        return PendingCheckoutsDialog.CANCEL

    def selected_item_ids(self):
        return []


def _close_event():
    return QEvent(QEvent.Close)


def test_close_with_no_checked_out_items_closes_immediately(gui_window):
    gui_window._closing_in_progress = False
    event = _close_event()
    gui_window.closeEvent(event)
    assert event.isAccepted()


def test_close_with_checked_out_items_offers_leave_checked_out(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("pending.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("pending.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogLeave)
    event = _close_event()
    gui_window.closeEvent(event)

    assert event.isAccepted()
    items = main.search("pending.txt")
    assert items[0][7] == "CHECKED_OUT"  # left exactly as the user chose, not auto-returned


def test_close_with_checked_out_items_can_return_selected(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("returnme.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("returnme.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogReturnAll)
    event = _close_event()
    gui_window.closeEvent(event)

    assert event.isAccepted()
    items = main.search("returnme.txt")
    assert items[0][7] == "ARCHIVED"


def test_close_can_be_cancelled_by_user(gui_window, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("cancelclose.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("cancelclose.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogCancel)
    event = _close_event()
    gui_window.closeEvent(event)

    assert not event.isAccepted()
    items = main.search("cancelclose.txt")
    assert items[0][7] == "CHECKED_OUT"  # nothing changed


def test_close_while_busy_is_refused(gui_window, message_boxes):
    gui_window._closing_in_progress = False
    gui_window.set_busy(True, "working...")
    event = _close_event()
    gui_window.closeEvent(event)
    assert not event.isAccepted()
    gui_window.set_busy(False)


def test_restart_reloads_persistent_state_from_disk(sandbox, qtbot):
    from gui.main_window import MainWindow

    main.initialize()
    main.create_category("Docs")
    path = os.path.join(str(sandbox), "persisted.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("persisted content")
    assert main.archive_file(path, "Docs") is True

    window1 = MainWindow()
    qtbot.addWidget(window1)
    window1.refresh_all()
    assert window1.dashboard_tab.items_label.text() == "1"
    window1.close()

    # simulate a brand new process: fresh MainWindow against the same on-disk DB
    main.initialize()
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2.refresh_all()
    assert window2.dashboard_tab.items_label.text() == "1"
    assert window2.dashboard_tab.categories_label.text() == "1"

    window2.items_tab.search_edit.setText("persisted")
    window2.items_tab.refresh()
    assert window2.items_tab.table.rowCount() == 1
    window2.close()


def test_the_database_is_deleted_while_the_app_is_closed(sandbox, qtbot, message_boxes):
    from gui.main_window import MainWindow
    api.initialize()
    api.create_category("Docs")
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(500, "{w}\n"))
    assert api.archive_file(str(doc), "Docs")
    os.remove(api.config.db_path())
    api.initialize()
    win = MainWindow()
    qtbot.addWidget(win)
    win._closing_in_progress = True
    win.refresh_all()
    no_crash_reports(message_boxes)
    assert os.listdir(os.path.join(api.data_root(), "archives")), "the archives themselves must not be touched"
    win.close()


def test_every_button_while_an_operation_runs(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    small = sandbox / "small.txt"
    small.write_bytes(corpus.lines(500, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(small), "Docs")
    big = sandbox / "big.txt"
    big.write_bytes(corpus.lines(200000, "{i} {w} {n} payload\n"))
    FakeSourceActionDialog._next_action = "keep"
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", FakeSourceActionDialog)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (str(big), "")))
    type_category_name(monkeypatch, "Sneaky")
    message_boxes.warning_confirm = True
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab._add_file()
    assert gui_window.is_busy()
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "small.txt")
    for click in (gui_window.items_tab._add_file, gui_window.items_tab._add_folder, gui_window.items_tab._checkout,
                  gui_window.items_tab._checkin, gui_window.items_tab._verify_selected,
                  gui_window.categories_tab._delete_category, gui_window.categories_tab._edit_category,
                  gui_window.settings_tab._change_data_root, gui_window.settings_tab._change_workspace_root,
                  gui_window.settings_tab._reset_workspace_root, gui_window.dashboard_tab._verify_all):
        click()
    wait_idle(qtbot, gui_window, timeout=120000)
    assert sorted(r[1] for r in api.search("")) == ["big.txt", "small.txt"]
    assert item_status("small.txt") == "ARCHIVED"
    app_is_healthy(gui_window, message_boxes)


def test_repeated_refresh_calls_are_harmless(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("refreshme.txt", "content")
    set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)

    for _ in range(10):
        gui_window.refresh_all()

    assert len(main.search("refreshme.txt")) == 1


def test_close_still_works_when_the_checked_out_list_cannot_be_read(gui_window, monkeypatch):
    def broken():
        raise OSError("database is locked")

    monkeypatch.setattr(main, "items_pending_return", broken)
    gui_window._closing_in_progress = False
    event = _close_event()
    gui_window.closeEvent(event)
    assert event.isAccepted()


def test_items_that_cannot_be_returned_on_close_are_reported(gui_window, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    for name in ("refused.txt", "broken.txt"):
        main.archive_file(make_file(name, f"content of {name}"), "Docs")
        main.checkout(main.search(name)[0][0])
    refused, broken = (main.search(n)[0][0] for n in ("refused.txt", "broken.txt"))

    def checkin(item_id):
        if item_id == broken:
            raise OSError("disk removed")
        return False

    monkeypatch.setattr(main, "checkin", checkin)
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogReturnAll)
    gui_window._closing_in_progress = False
    event = _close_event()
    gui_window.closeEvent(event)
    assert event.isAccepted()
    assert message_boxes.calls[-1] == ("warning", "StorageArch", "2 item(s) could not be returned and remain checked out.")
    assert {main.get_item(i)[10] for i in (refused, broken)} == {"CHECKED_OUT"}


def test_returning_items_on_close_does_not_freeze_the_window(gui_window, monkeypatch, make_file, message_boxes):
    """The check-ins run on another thread while the window's own thread keeps handling events."""
    import threading
    import time

    from PySide6.QtCore import QTimer

    main.create_category("Docs")
    main.archive_file(make_file("slow.txt"), "Docs")
    main.checkout(main.search("slow.txt")[0][0])
    seen = {"ticks": 0, "closes_refused": 0}
    real_checkin = main.checkin

    def slow_checkin(item_id):
        seen["thread"] = threading.current_thread()
        seen["busy"] = gui_window.is_busy()
        time.sleep(0.4)
        return real_checkin(item_id)

    def tick():
        seen["ticks"] += 1
        if seen["ticks"] == 2:  # the user clicks the X again while the items are being returned
            again = _close_event()
            gui_window.closeEvent(again)
            seen["closes_refused"] += not again.isAccepted()

    monkeypatch.setattr(main, "checkin", slow_checkin)
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogReturnAll)
    timer = QTimer(gui_window)
    timer.timeout.connect(tick)
    timer.start(20)
    gui_window._closing_in_progress = False
    event = _close_event()
    gui_window.closeEvent(event)
    timer.stop()
    assert event.isAccepted() and main.search("slow.txt")[0][7] == "ARCHIVED"
    assert seen["thread"] is not threading.main_thread(), "the check-in ran on the window's thread"
    assert seen["ticks"] >= 5, f"the window handled only {seen['ticks']} events while the items were returned"
    assert seen["busy"] and seen["closes_refused"] == 1 and not gui_window.is_busy()
    assert message_boxes.calls == []  # a second click on the X is ignored quietly
