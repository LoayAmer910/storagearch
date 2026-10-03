"""Garbage collection only on the GUI thread.

Python's automatic garbage collector runs on whatever thread happens to allocate when a threshold is
crossed - often a background worker (archive, check out, check in). If the garbage it frees includes Qt
objects (a closed window, a timer, a dialog), their C++ side is destroyed on that worker thread, which Qt
does not allow: the process dies with an access violation, at an unrelated line of the worker's code.

So, once the GUI starts, automatic collection is switched off and a timer on the GUI thread collects
instead - by the same generation thresholds, a few times a second. Background threads never collect.
"""
import gc

from PySide6.QtCore import QObject, QTimer

INTERVAL_MS = 250

_guard = None


class _GuiThreadCollector(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.threshold = gc.get_threshold()
        self.timer = QTimer(self)
        self.timer.setInterval(INTERVAL_MS)
        self.timer.timeout.connect(self.collect)
        gc.disable()
        self.timer.start()

    def collect(self):
        counts = gc.get_count()
        if counts[0] > self.threshold[0]:
            generation = 0
            if counts[1] > self.threshold[1]:
                generation = 1
                if counts[2] > self.threshold[2]:
                    generation = 2
            gc.collect(generation)


def install(app):
    """Switches collection to the GUI thread for the life of `app` (the QApplication). Idempotent."""
    global _guard
    try:
        current = _guard is not None and _guard.parent() is app
    except RuntimeError:  # the QApplication it belonged to is gone
        current = False
    if not current:
        _guard = _GuiThreadCollector(app)
    return _guard
