"""gui.dashboard_tab - the dashboard shows the real numbers, and Verify all reports what it found."""
from core import api as main
from support.gui import last_message, wait_idle


def test_dashboard_shows_real_zeros_on_fresh_db(gui_window):
    gui_window.dashboard_tab.refresh()
    assert gui_window.dashboard_tab.categories_label.text() == "0"
    assert gui_window.dashboard_tab.items_label.text() == "0"
    assert gui_window.dashboard_tab.checked_out_label.text() == "0"
    assert gui_window.dashboard_tab.warning_label.text() == ""


def test_dashboard_reflects_real_backend_state(gui_window, make_file):
    main.create_category("Docs", max_archive_size=2 * 1024 ** 3)
    file_path = make_file("sample.txt", "hello world")
    assert main.archive_file(file_path, "Docs") is True

    gui_window.dashboard_tab.refresh()
    assert gui_window.dashboard_tab.categories_label.text() == "1"
    assert gui_window.dashboard_tab.items_label.text() == "1"
    # values must come from the real DB, not be hardcoded
    summary = main.dashboard_summary()
    assert gui_window.dashboard_tab.original_size_label.text() != "-"
    assert summary["total_original_size"] == len("hello world")


def test_dashboard_data_that_cannot_be_loaded_is_explained(gui_window, monkeypatch, message_boxes):
    def broken():
        raise OSError("database is locked")

    monkeypatch.setattr(main, "dashboard_summary", broken)
    gui_window.dashboard_tab.refresh()
    assert last_message(message_boxes)[:3] == ("warning", "Dashboard", "Could not load dashboard data:\ndatabase is locked")


def test_verify_all_updates_the_counts_and_warns_about_a_missing_archive(gui_window, qtbot, make_file):
    main.create_category("Docs")
    main.archive_file(make_file("a.txt", "one"), "Docs")
    main.archive_file(make_file("b.txt", "two" * 5000), "Docs")
    zip_path = main.get_archive(main.get_item(main.search("a.txt")[0][0])[1])[3]
    import os
    os.remove(zip_path)
    gui_window.dashboard_tab._verify_all()
    assert gui_window.is_busy()
    wait_idle(qtbot, gui_window)
    assert gui_window.dashboard_tab.missing_label.text() == "1"
    assert gui_window.dashboard_tab.warning_label.text() == "1 archive(s) need attention - run Verify to review."


def test_verify_all_does_nothing_while_another_operation_runs(gui_window, monkeypatch):
    called = []
    monkeypatch.setattr(main, "verify_all", lambda: called.append(1))
    gui_window.set_busy(True, "Archiving...")
    gui_window.dashboard_tab._verify_all()
    assert called == [] and gui_window.statusBar().currentMessage() == "Archiving..."


def test_a_verify_all_that_fails_is_reported_and_the_window_is_usable_again(gui_window, qtbot, monkeypatch, message_boxes):
    def broken():
        raise OSError("drive disconnected")

    monkeypatch.setattr(main, "verify_all", broken)
    gui_window.dashboard_tab._verify_all()
    wait_idle(qtbot, gui_window)
    kind, title, text = last_message(message_boxes)[:3]
    assert (kind, title) == ("critical", "Verify all archives") and "drive disconnected" in text
    assert not gui_window.is_busy()
