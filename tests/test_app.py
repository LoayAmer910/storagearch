"""app - starting the desktop app: the window opens on a fresh installation, the cloud is shut down on exit, and an
unhandled error is reported (as a class name and a code location only) and then handled as usual."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before Qt is loaded

import app as app_module  # noqa: E402
from core import api  # noqa: E402


class _RecordingApi:
    def __init__(self, fails=False):
        self.reports, self.fails = [], fails

    def cloud_report_crash(self, exception, **where):
        if self.fails:
            raise RuntimeError("the cloud client itself is broken")
        self.reports.append((type(exception).__name__, where))


def _raise_and_get_info():
    try:
        raise KeyError("C:\\Users\\someone\\private.txt")
    except KeyError:
        return sys.exc_info()


def test_an_unhandled_error_is_reported_by_where_it_happened_then_handled_as_usual(monkeypatch):
    handled = []
    monkeypatch.setattr(sys, "excepthook", lambda *info: handled.append(info))
    recorder = _RecordingApi()
    app_module._install_crash_hook(recorder)
    info = _raise_and_get_info()
    sys.excepthook(*info)
    (name, where), = recorder.reports
    assert name == "KeyError"
    assert where["component"] == "test_app" and where["function_name"] == "_raise_and_get_info"
    assert where["safe_code_location"].startswith("test_app.py:")
    assert handled == [info], "the default handler must still run"


def test_an_error_without_a_traceback_is_still_handled(monkeypatch):
    handled = []
    monkeypatch.setattr(sys, "excepthook", lambda *info: handled.append(info))
    recorder = _RecordingApi()
    app_module._install_crash_hook(recorder)
    sys.excepthook(ValueError, ValueError("x"), None)
    assert recorder.reports == [("ValueError", {"component": None, "function_name": None, "safe_code_location": None})]
    assert len(handled) == 1


def test_a_failing_crash_report_never_hides_the_error(monkeypatch):
    handled = []
    monkeypatch.setattr(sys, "excepthook", lambda *info: handled.append(info))
    app_module._install_crash_hook(_RecordingApi(fails=True))
    sys.excepthook(*_raise_and_get_info())
    assert len(handled) == 1


def test_start_opens_the_window_on_a_fresh_installation_and_shuts_the_cloud_down(tmp_path, monkeypatch, qtbot):
    from PySide6.QtWidgets import QApplication

    from gui.main_window import MainWindow

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # start() installs its hook; put the old one back after
    shown, shut_down = [], []
    monkeypatch.setattr(QApplication, "exec", lambda self: shown.extend(
        w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()) or 0)
    monkeypatch.setattr(api, "shutdown_cloud", lambda *a, **k: shut_down.append(True))
    try:
        assert app_module.start() == 0
        assert len(shown) == 1 and shown[0].windowTitle()
        assert os.path.isfile(os.path.join(api.data_root(), "smartarchive.db"))  # the installation was initialized
        assert shut_down == [True]
    finally:
        for window in shown:
            window._closing_in_progress = True
            window.close()
