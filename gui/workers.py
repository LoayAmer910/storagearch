import queue
import threading

from PySide6.QtCore import QTimer

_POLL_INTERVAL_MS = 30


def run_in_background(owner, func, on_success, on_error, args=(), kwargs=None):
    """
    Runs func(*args, **kwargs) on a background thread.
    on_success(result) / on_error(message) are invoked back on the GUI thread.

    The background thread is a plain Python thread and never touches a Qt object:
    it puts its outcome on a queue, and a QTimer living on the GUI thread picks it up.
    Running the work on a QThread and reporting through signals emitted from that thread
    crashed the whole process (access violation while the GUI thread was collecting
    garbage) under load, on Python 3.12 and 3.14 alike.

    Returns (thread, timer); callers keep them referenced until the operation is over.
    """
    outcomes = queue.Queue()

    def run():
        try:
            outcomes.put(("ok", func(*args, **(kwargs or {}))))
        except Exception as exc:  # noqa: BLE001 - any backend failure must reach the GUI as a dialog, never a crash
            outcomes.put(("error", str(exc)))

    thread = threading.Thread(target=run, daemon=True)
    timer = QTimer(owner)
    timer.setInterval(_POLL_INTERVAL_MS)

    def poll():
        try:
            kind, payload = outcomes.get_nowait()
        except queue.Empty:
            return
        timer.stop()
        timer.deleteLater()
        (on_success if kind == "ok" else on_error)(payload)

    timer.timeout.connect(poll)
    thread.start()
    timer.start()
    return thread, timer
