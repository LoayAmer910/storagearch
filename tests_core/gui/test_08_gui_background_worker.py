"""Contract of gui.workers.run_in_background.

Background archives used to run on a QThread and report through signals emitted from that
thread. Under memory pressure that crashed the whole process (an access violation on the GUI
thread while it was collecting garbage), on Python 3.12 and 3.14 alike. The crash only shows up
in long sessions and depends on timing, so it cannot be turned into a small deterministic test;
what these tests pin down is the design that removed it: the work runs on a plain Python thread,
never on a Qt thread object, and the outcome is delivered on the GUI thread.
"""
import threading

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QWidget

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


def test_with_the_gui_open_garbage_is_collected_only_on_the_gui_thread(qtbot):
    """The collector freed Qt objects (left by closed windows) on a worker thread, which killed the
    process with an access violation in the middle of a checkout. With a MainWindow open, automatic
    collection is off and the GUI thread collects - so a worker that makes plenty of garbage never does."""
    import gc

    from gui import gc_guard
    from gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window._closing_in_progress = True
    assert not gc.isenabled()
    collected_on = set()

    def watch(phase, _info):
        if phase == "start":
            collected_on.add(threading.get_ident())
    gc.callbacks.append(watch)
    try:
        def make_garbage():
            for _ in range(200_000):
                a = []
                a.append(a)  # a cycle only the collector frees
            return threading.get_ident()
        delivered = []
        run_in_background(window, make_garbage, delivered.append, delivered.append)
        qtbot.waitUntil(lambda: bool(delivered), timeout=15000)
        qtbot.waitUntil(lambda: bool(collected_on), timeout=5000)  # the GUI thread's timer does collect
    finally:
        gc.callbacks.remove(watch)
    worker = delivered[0]
    assert worker not in collected_on
    assert collected_on == {threading.get_ident()}
    installed = gc_guard._guard
    assert gc_guard.install(QApplication.instance()) is installed  # a second window adds no second collector


def test_many_operations_in_a_row_complete_and_leave_nothing_behind(qtbot):
    owner = _owner(qtbot)
    results = []
    for number in range(25):
        run_in_background(owner, lambda n=number: n * 2, results.append, results.append)
    qtbot.waitUntil(lambda: len(results) == 25, timeout=10000)

    assert sorted(results) == [n * 2 for n in range(25)]
    qtbot.wait(100)  # let the finished timers be deleted
    assert owner.findChildren(QTimer) == []
