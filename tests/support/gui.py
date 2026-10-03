"""Shared helpers for the GUI tests on the new core."""
from PySide6.QtWidgets import QDialog

from core import api, api as main


def wait_idle(qtbot, win, timeout=15000):
    qtbot.waitUntil(lambda: not win.is_busy(), timeout=timeout)


def workspace_of(name, original_path_suffix=None):
    """The workspace copy of a checked-out item, read from the catalog. The new core gives every item its
    own workspace folder (the old flat layout let two items with the same name overwrite each other)."""
    from core import api as main
    for row in main.search(name):
        if row[1] == name and (original_path_suffix is None or row[4].replace("\\", "/").endswith(original_path_suffix)):
            return main.get_item(row[0])[11]
    raise AssertionError(f"no checked-out item {name!r}")


def category_id_by_name(name):
    for category in main.list_categories():
        if category[1] == name:
            return category[0]
    raise AssertionError(f"category '{name}' not found")


class FakeSourceActionDialog:
    Accepted = 1
    Rejected = 0
    _next_action = "keep"

    def __init__(self, *args, **kwargs):
        pass

    def exec(self):
        return self.Accepted

    def chosen_action(self):
        return FakeSourceActionDialog._next_action


def set_fake_source_action(monkeypatch, action):
    FakeSourceActionDialog._next_action = action
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", FakeSourceActionDialog)


def select_destination_category(window, category_name):
    window.items_tab.refresh_categories()
    index = window.items_tab.destination_category.findText(category_name)
    window.items_tab.destination_category.setCurrentIndex(index)


def archive_file_through_gui(window, qtbot, monkeypatch, path, category, action="keep"):
    set_fake_source_action(monkeypatch, action)
    select_destination_category(window, category)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    window.items_tab._add_file()
    wait_idle(qtbot, window)


def archive_folder_through_gui(window, qtbot, monkeypatch, path, category, action="keep"):
    set_fake_source_action(monkeypatch, action)
    select_destination_category(window, category)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: path))
    window.items_tab._add_folder()
    wait_idle(qtbot, window)


def select_row_by_name(table, name):
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name:
            table.selectRow(row)
            return row
    raise AssertionError(f"row '{name}' not found")


def last_message(recorder):
    return recorder.calls[-1] if recorder.calls else None


def no_crash_reports(recorder):
    crashes = [c for c in recorder.calls if c[0] == "critical"]
    assert crashes == [], crashes


def all_archives_verified():
    statuses = [s for c in api.verify_all().values() for s in c.values()]
    assert set(statuses) <= {"VERIFIED"}, statuses


def app_is_healthy(window, recorder):
    assert not window.is_busy()
    no_crash_reports(recorder)
    all_archives_verified()


def type_category_name(monkeypatch, name):
    import gui.categories_tab as tab

    def fake_exec(dialog):
        dialog.name_edit.setText(name)
        return QDialog.Accepted
    monkeypatch.setattr(tab.CreateCategoryDialog, "exec", fake_exec)


def checkout_through_gui(window, qtbot, name):
    window.items_tab.refresh()
    select_row_by_name(window.items_tab.table, name)
    window.items_tab._checkout()
    wait_idle(qtbot, window)


def checkin_through_gui(window, qtbot, name):
    window.items_tab.refresh()
    select_row_by_name(window.items_tab.table, name)
    window.items_tab._checkin()
    wait_idle(qtbot, window)


def item_status(name):
    return api.get_item(api.search(name)[0][0])[10]


def content_after_checkout(name, sandbox, sub="back"):
    item_id = api.search(name)[0][0]
    return api.checkout(item_id, destination=str(sandbox / sub / str(item_id)))
