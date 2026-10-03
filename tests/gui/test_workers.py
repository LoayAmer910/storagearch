"""gui.workers - work runs on a plain background thread and its result reaches the GUI thread."""
import threading

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QWidget

from gui.workers import run_in_background


def _owner(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    return widget


def test_result_is_delivered_on_the_gui_thread(qtbot):
    owner = _owner(qtbot)
    seen, delivered = {}, []

    def work():
        seen["worker"] = threading.get_ident()
        return 42

    run_in_background(owner, work, lambda r: delivered.append((r, threading.get_ident())), delivered.append)
    qtbot.waitUntil(lambda: bool(delivered), timeout=5000)

    assert delivered[0][0] == 42
    assert delivered[0][1] == threading.get_ident(), "on_success must run on the GUI thread"
    assert seen["worker"] != threading.get_ident(), "the work must not run on the GUI thread"


def test_a_failure_reaches_on_error_as_a_message(qtbot):
    owner = _owner(qtbot)
    delivered = []

    def work():
        raise RuntimeError("disk full")

    run_in_background(owner, work, lambda r: delivered.append(("ok", r)), lambda m: delivered.append(("error", m)))
    qtbot.waitUntil(lambda: bool(delivered), timeout=5000)

    assert delivered == [("error", "disk full")]


def test_the_background_thread_is_not_a_qt_thread(qtbot):
    owner = _owner(qtbot)
    delivered = []

    thread, _handle = run_in_background(owner, lambda: None, delivered.append, delivered.append)
    qtbot.waitUntil(lambda: bool(delivered), timeout=5000)

    assert isinstance(thread, threading.Thread)
    assert not isinstance(thread, QThread)


def test_many_operations_in_a_row_complete_and_leave_nothing_behind(qtbot):
    owner = _owner(qtbot)
    results = []
    for number in range(25):
        run_in_background(owner, lambda n=number: n * 2, results.append, results.append)
    qtbot.waitUntil(lambda: len(results) == 25, timeout=10000)

    assert sorted(results) == [n * 2 for n in range(25)]
    qtbot.wait(100)  # let the finished timers be deleted
    assert owner.findChildren(QTimer) == []


def test_worker_exception_is_shown_as_dialog_not_raised_to_gui(gui_window, qtbot, message_boxes):
    def _boom():
        raise ValueError("worker exploded")

    captured = {}

    def on_success(_result):
        captured["success"] = True

    def on_error(message):
        captured["error"] = message

    thread, worker = run_in_background(gui_window, _boom, on_success, on_error)
    qtbot.waitUntil(lambda: "error" in captured, timeout=5000)
    assert "worker exploded" in captured["error"]
    assert "success" not in captured
