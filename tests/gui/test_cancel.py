"""The window while a long operation runs: a Cancel button that really stops it, and a count of how far it is."""
import os
import threading

import pytest

from core import api, pipeline, store
from support.gui import (
    archive_folder_through_gui,
    select_destination_category,
    select_row_by_name,
    set_fake_source_action,
    wait_idle,
)


def _folder(sandbox, name="work", count=6):
    folder = sandbox / name
    for i in range(count):
        path = folder / f"sub{i % 2}" / f"f{i}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"line {n} of file {i}\n" for n in range(3000)), encoding="utf-8")
    return folder


class _Held:
    """Holds the operation at owner.name until the test lets it go, so the test can act while it runs."""

    def __init__(self, monkeypatch, owner, name):
        self.reached, self.go = threading.Event(), threading.Event()
        real = getattr(owner, name)

        def held(*args, **kwargs):
            self.reached.set()
            self.go.wait(30)
            return real(*args, **kwargs)
        monkeypatch.setattr(owner, name, held)


@pytest.fixture
def hold(monkeypatch, gui_window, qtbot):
    """hold(owner, name) -> _Held. Whatever happens in the test - a failed assertion too - the held operation is
    let go and has finished before the test's isolation ends: a background operation that outlives its test
    would report into the real cloud folder."""
    made = []

    def make(owner, name):
        made.append(_Held(monkeypatch, owner, name))
        return made[-1]
    yield make
    for held in made:
        held.go.set()
    qtbot.waitUntil(lambda: not gui_window.is_busy(), timeout=60000)


def _start_folder_archive(window, monkeypatch, folder, action="keep"):
    api.create_category("Docs")
    set_fake_source_action(monkeypatch, action)
    select_destination_category(window, "Docs")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: str(folder)))
    window.items_tab._add_folder()


def test_the_cancel_button_is_there_only_for_what_can_be_cancelled(gui_window):
    assert gui_window.cancel_button.isHidden()
    gui_window.set_busy(True, "Verifying archive...")
    assert gui_window.cancel_button.isHidden()
    gui_window.set_busy(True, "Archiving...", cancellable=True)
    assert not gui_window.cancel_button.isHidden() and gui_window.cancel_button.isEnabled()
    gui_window.set_busy(False)
    assert gui_window.cancel_button.isHidden() and gui_window.statusBar().currentMessage() == "Ready"


@pytest.mark.parametrize("action", ["keep", "delete"])
def test_cancel_while_a_folder_is_compressed_saves_nothing_and_says_so(gui_window, qtbot, monkeypatch, sandbox,
                                                                       message_boxes, hold, action):
    folder = _folder(sandbox)
    held = hold(pipeline.Pipeline, "decide")
    _start_folder_archive(gui_window, monkeypatch, folder, action)
    qtbot.waitUntil(held.reached.is_set, timeout=15000)

    assert gui_window.is_busy() and not gui_window.cancel_button.isHidden()
    gui_window._show_progress()
    assert "Compressing" in gui_window.statusBar().currentMessage() and "of 6" in gui_window.statusBar().currentMessage()
    gui_window.cancel_button.click()
    assert gui_window.statusBar().currentMessage() == "Cancelling..." and not gui_window.cancel_button.isEnabled()
    held.go.set()
    wait_idle(qtbot, gui_window)

    kind, title, text = message_boxes.calls[-1][:3]
    assert (kind, title) == ("information", "Add item") and text.startswith("Cancelled")
    assert api.search("") == [] and api.list_archives() == []
    assert len([f for _d, _s, fs in os.walk(folder) for f in fs]) == 6
    assert gui_window.cancel_button.isHidden() and gui_window.statusBar().currentMessage() == "Ready"
    # and the window is usable: the same folder is archived normally right after
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(folder), "Docs")
    assert [row.file_name for row in api.search("")] == ["work"]


def test_cancel_while_the_source_is_read_for_the_duplicate_check(gui_window, qtbot, monkeypatch, sandbox, message_boxes,
                                                                 hold):
    folder = _folder(sandbox)
    held = hold(store, "sha256_file")
    asked = []
    _start_folder_archive(gui_window, monkeypatch, folder)
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", lambda *a, **k: asked.append(1))
    qtbot.waitUntil(held.reached.is_set, timeout=15000)
    assert not gui_window.cancel_button.isHidden()
    gui_window.cancel_button.click()
    held.go.set()
    wait_idle(qtbot, gui_window)
    kind, title, text = message_boxes.calls[-1][:3]
    assert (kind, title) == ("information", "Add item") and text.startswith("Cancelled")
    assert asked == [] and api.search("") == []


def test_cancel_while_an_item_is_checked_out(gui_window, qtbot, monkeypatch, sandbox, message_boxes, hold):
    folder = _folder(sandbox)
    api.create_category("Docs")
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(folder), "Docs")
    item_id = api.search("work")[0].id
    held = hold(store, "_extract_file")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "work")
    gui_window.items_tab._checkout()
    qtbot.waitUntil(held.reached.is_set, timeout=15000)
    gui_window.cancel_button.click()
    held.go.set()
    wait_idle(qtbot, gui_window)
    kind, title, text = message_boxes.calls[-1][:3]
    assert (kind, title) == ("information", "Checkout") and "still in its archive" in text
    assert api.get_item(item_id).status == "ARCHIVED"


def test_a_cancel_click_that_comes_too_late_changes_nothing(gui_window, qtbot, monkeypatch, sandbox, message_boxes):
    folder = _folder(sandbox)
    api.create_category("Docs")
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(folder), "Docs")
    gui_window.set_busy(True, "Archiving...", cancellable=True)  # the button is still up, the operation is over
    gui_window.cancel_button.click()
    assert gui_window.cancel_button.isEnabled() and gui_window.statusBar().currentMessage() == "Archiving..."
    gui_window.set_busy(False)
    assert [row.status for row in api.search("")] == ["ARCHIVED"]
