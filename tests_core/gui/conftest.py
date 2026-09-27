"""The GUI tests. The fixtures mirror the ones the GUI tests always used, so those tests run as they were."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from core import api, events  # noqa: E402


class MessageBoxRecorder:
    """Replaces the blocking QMessageBox dialogs so tests never wait for a click."""

    def __init__(self):
        self.calls = []
        self.warning_confirm = False

    def information(self, parent, title, text, *args, **kwargs):
        self.calls.append(("information", title, text))
        return QMessageBox.Ok

    def warning(self, parent, title, text, *args, **kwargs):
        self.calls.append(("warning", title, text))
        return QMessageBox.Yes if self.warning_confirm else QMessageBox.No

    def critical(self, parent, title, text, *args, **kwargs):
        self.calls.append(("critical", title, text))
        return QMessageBox.Ok


@pytest.fixture(autouse=True)
def message_boxes(monkeypatch):
    recorder = MessageBoxRecorder()
    monkeypatch.setattr(QMessageBox, "information", recorder.information)
    monkeypatch.setattr(QMessageBox, "warning", recorder.warning)
    monkeypatch.setattr(QMessageBox, "critical", recorder.critical)
    return recorder


@pytest.fixture(autouse=True)
def cloud_home(tmp_path, monkeypatch):
    home = tmp_path / "cloud_home"
    monkeypatch.setenv("STORAGEARCH_CLOUD_HOME", str(home))
    events.reset()
    yield home
    events.flush(5)
    events.reset()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def gui_window(sandbox, qtbot):
    from gui.main_window import MainWindow

    api.initialize()
    win = MainWindow()
    qtbot.addWidget(win)
    win._closing_in_progress = True
    yield win
    win.close()


@pytest.fixture
def make_file(sandbox):
    def _make_file(relative_path, content="hello storagearch"):
        full_path = sandbox / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return str(full_path)
    return _make_file


@pytest.fixture
def make_folder(sandbox):
    def _make_folder(relative_path, files):
        base = sandbox / relative_path
        base.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return str(base)
    return _make_folder
