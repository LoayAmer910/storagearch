"""gui.gc_guard - with the window open, garbage is collected only on the GUI thread."""
import threading

from PySide6.QtWidgets import QApplication

from gui.workers import run_in_background


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


class _Gone:
    def parent(self):
        raise RuntimeError("Internal C++ object already deleted.")


def test_a_guard_whose_application_is_gone_is_replaced(qtbot, monkeypatch):
    from gui import gc_guard
    monkeypatch.setattr(gc_guard, "_guard", _Gone())
    guard = gc_guard.install(QApplication.instance())
    assert isinstance(guard, gc_guard._GuiThreadCollector) and guard.parent() is QApplication.instance()


def test_when_every_generation_is_over_its_threshold_the_oldest_is_collected(qtbot, monkeypatch):
    from gui import gc_guard
    guard = gc_guard.install(QApplication.instance())
    collected = []
    monkeypatch.setattr(gc_guard.gc, "get_count", lambda: tuple(t + 1 for t in guard.threshold))
    monkeypatch.setattr(gc_guard.gc, "collect", collected.append)
    guard.collect()
    assert collected == [2]
