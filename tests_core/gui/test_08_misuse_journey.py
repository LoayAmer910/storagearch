"""End to end through the real window: at every step, the wrong things a user does.

Only the modal dialogs are simulated (the picked path, the typed text, the confirmation); every handler,
background operation and message box is the application's own. After every step the same checks run: the
window is not stuck busy, no message is an unexpected crash report, and every archive still verifies - a
user's mistake may be refused, but it must never cost data.
"""
import ctypes
import os
import stat
from contextlib import contextmanager
from ctypes import wintypes

import pytest
from PySide6.QtWidgets import QDialog

import corpus
from gui_support import wait_idle, workspace_of
from test_08_items_workflow import (FakeSourceActionDialog, _archive_file_through_gui, _archive_folder_through_gui,
                                    _select_row_by_name)
from core import api

# --- helpers ------------------------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]


@contextmanager
def locked(path):
    """Opens the file the way another program holding it exclusively would (no sharing at all)."""
    handle = _kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share 0
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    try:
        yield
    finally:
        _kernel32.CloseHandle(handle)


def long_path(path):
    return "\\\\?\\" + os.path.abspath(path)


def _last(recorder):
    return recorder.calls[-1] if recorder.calls else None


def _no_crash_reports(recorder):
    crashes = [c for c in recorder.calls if c[0] == "critical"]
    assert crashes == [], crashes


def _all_verified():
    statuses = [s for c in api.verify_all().values() for s in c.values()]
    assert set(statuses) <= {"VERIFIED"}, statuses


def _healthy(window, recorder):
    assert not window.is_busy()
    _no_crash_reports(recorder)
    _all_verified()


def _type_category(monkeypatch, name):
    import gui.categories_tab as tab

    def fake_exec(dialog):
        dialog.name_edit.setText(name)
        return QDialog.Accepted
    monkeypatch.setattr(tab.CreateCategoryDialog, "exec", fake_exec)


def _checkout_through_gui(window, qtbot, name):
    window.items_tab.refresh()
    _select_row_by_name(window.items_tab.table, name)
    window.items_tab._checkout()
    wait_idle(qtbot, window)


def _checkin_through_gui(window, qtbot, name):
    window.items_tab.refresh()
    _select_row_by_name(window.items_tab.table, name)
    window.items_tab._checkin()
    wait_idle(qtbot, window)


def _status(name):
    return api.get_item(api.search(name)[0][0])[10]


def _content_back(name, sandbox, sub="back"):
    item_id = api.search(name)[0][0]
    return api.checkout(item_id, destination=str(sandbox / sub / str(item_id)))


# --- categories ---------------------------------------------------------------------------------------

# Hebrew on purpose: non-ASCII category names must work
@pytest.mark.parametrize("name", ["CON", "nul", "com1", "abc.", "trailing ", "..", ".", "a\\b/c", "📁 תמונות",
                                  "tab\there", "line\nbreak", "  Docs  "])
def test_category_names_windows_would_choke_on(gui_window, qtbot, monkeypatch, message_boxes, sandbox, name):
    _type_category(monkeypatch, name)
    gui_window.categories_tab._create_category()
    _no_crash_reports(message_boxes)
    created = api.list_categories()
    if not created:
        assert _last(message_boxes)[0] == "warning"
        return
    folder = os.path.join(api.data_root(), "archives", created[0][6])
    assert os.path.isdir(folder)
    # and the category is actually usable
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(500, "{i} {w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), created[0][1])
    assert "archived and verified" in _last(message_boxes)[2], message_boxes.calls
    _healthy(gui_window, message_boxes)


def test_two_categories_differing_only_in_case_do_not_share_a_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    for name in ("Docs", "docs"):
        _type_category(monkeypatch, name)
        gui_window.categories_tab._create_category()
        gui_window.categories_tab.refresh()
    names = [c[1] for c in api.list_categories()]
    if len(names) == 2:
        folders = {c[6].lower() for c in api.list_categories()}
        assert len(folders) == 2, "two categories would write into the same folder on Windows"
        for i, name in enumerate(names):
            doc = sandbox / f"f{i}.txt"
            doc.write_bytes(corpus.lines(400 + i, "{i} {w}\n"))
            _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), name)
    _healthy(gui_window, message_boxes)


def test_renaming_a_category_onto_another_or_to_nothing(gui_window, monkeypatch, message_boxes):
    import gui.categories_tab as tab
    api.create_category("A")
    api.create_category("B")
    gui_window.categories_tab.refresh()
    for new_name in ("B", "", "   "):
        def fake_exec(dialog, new_name=new_name):
            dialog.name_edit.setText(new_name)
            return QDialog.Accepted
        monkeypatch.setattr(tab.CreateCategoryDialog, "exec", fake_exec)
        _select_row_by_name(gui_window.categories_tab.table, "A")
        gui_window.categories_tab._edit_category()
        gui_window.categories_tab.refresh()
        _no_crash_reports(message_boxes)
    assert sorted(c[1] for c in api.list_categories()) == ["A", "B"]


def test_deleting_a_category_then_saying_no_or_having_items(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Keep")
    doc = sandbox / "k.txt"
    doc.write_bytes(corpus.lines(300, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Keep")
    gui_window.categories_tab.refresh()
    _select_row_by_name(gui_window.categories_tab.table, "Keep")
    message_boxes.warning_confirm = False
    gui_window.categories_tab._delete_category()      # says no
    message_boxes.warning_confirm = True
    gui_window.categories_tab._delete_category()      # says yes, but it has items
    assert "cannot be deleted" in _last(message_boxes)[2]
    assert [c[1] for c in api.list_categories()] == ["Keep"]
    _healthy(gui_window, message_boxes)


# --- picking sources ----------------------------------------------------------------------------------

def test_a_file_another_program_holds_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "open_in_word.docx"
    doc.write_bytes(corpus.lines(2000, "{i} {w}\n"))
    with locked(doc):
        _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
        kind, _title, text = _last(message_boxes)
        assert kind == "warning" and "open_in_word.docx" in text and "open in another program" in text, message_boxes.calls
    assert api.search("") == []
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")  # closed it, tries again
    assert "archived and verified" in _last(message_boxes)[2]
    _healthy(gui_window, message_boxes)


def test_a_file_that_gets_locked_after_the_duplicate_check(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "report.xlsx"
    doc.write_bytes(corpus.lines(2000, "{i} {w}\n"))
    handles = []

    class LocksWhileAsked(FakeSourceActionDialog):
        def exec(self):
            handles.append(_kernel32.CreateFileW(str(doc), 0x80000000, 0, None, 3, 0x80, None))
            return self.Accepted
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", LocksWhileAsked)
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (str(doc), "")))
    try:
        gui_window.items_tab._add_file()
        wait_idle(qtbot, gui_window)
    finally:
        for h in handles:
            _kernel32.CloseHandle(h)
    kind, _title, text = _last(message_boxes)
    assert kind == "warning" and "report.xlsx" in text and "open in another program" in text, message_boxes.calls
    assert api.search("") == []
    _healthy(gui_window, message_boxes)


def test_a_folder_with_one_file_held_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    project = sandbox / "project"
    project.mkdir()
    (project / "a.txt").write_bytes(corpus.lines(900, "{w}\n"))
    (project / "busy.log").write_bytes(corpus.lines(900, "{i}\n"))
    with locked(project / "busy.log"):
        _archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
        kind, _title, text = _last(message_boxes)
        assert kind == "warning" and "busy.log" in text and "open in another program" in text, message_boxes.calls
        assert "name is not valid" not in text
    assert api.search("") == [], "a folder must never be stored with a file silently missing"
    _healthy(gui_window, message_boxes)


@pytest.mark.parametrize("which", ["data_root", "archives", "workspace", "temp"])
def test_archiving_the_apps_own_folders_is_refused(gui_window, qtbot, monkeypatch, message_boxes, which):
    api.create_category("Docs")
    target = {"data_root": api.data_root(), "archives": os.path.join(api.data_root(), "archives"),
              "workspace": os.path.join(api.data_root(), "workspace"), "temp": os.path.join(api.data_root(), "temp")}[which]
    _archive_folder_through_gui(gui_window, qtbot, monkeypatch, target, "Docs")
    assert _last(message_boxes)[0] == "warning", message_boxes.calls
    assert api.search("") == []
    _healthy(gui_window, message_boxes)


def test_archiving_the_copy_that_is_checked_out(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "notes.txt"
    doc.write_bytes(corpus.lines(1200, "{i} {w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    _checkout_through_gui(gui_window, qtbot, "notes.txt")
    ws = workspace_of("notes.txt")
    with open(ws, "a", encoding="utf-8") as f:
        f.write("edited\n")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, ws, "Docs")
    assert _last(message_boxes)[0] == "warning", message_boxes.calls
    assert len(api.search("notes.txt")) == 1
    _healthy(gui_window, message_boxes)


@pytest.mark.parametrize("kind", ["too_long", "trailing_dot", "trailing_space", "reserved"])
def test_names_windows_cannot_normally_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox, kind):
    api.create_category("Docs")
    base = sandbox / "odd"
    base.mkdir()
    name = {"too_long": "d" * 120 + "\\" + "e" * 120 + "\\" + "f" * 60 + ".txt", "trailing_dot": "report.",
            "trailing_space": "report ", "reserved": "aux.txt"}[kind]
    path = os.path.join(str(base), name)
    os.makedirs(long_path(os.path.dirname(path)), exist_ok=True)
    with open(long_path(path), "wb") as f:
        f.write(corpus.lines(700, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    kind_shown = _last(message_boxes)[0]
    assert kind_shown in ("warning", "information"), message_boxes.calls
    if kind_shown == "information":
        back = _content_back(os.path.basename(path).rstrip(". "), sandbox) or _content_back(os.path.basename(path), sandbox)
        assert back and open(long_path(back), "rb").read() == open(long_path(path), "rb").read()
    _healthy(gui_window, message_boxes)


def test_the_file_changes_or_disappears_while_the_dialog_is_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "draft.txt"
    doc.write_bytes(corpus.lines(1500, "{i} {w}\n"))

    class EditsWhileAsked(FakeSourceActionDialog):
        def exec(self):
            with open(doc, "a", encoding="utf-8") as f:
                f.write("typed while the dialog was open\n")
            return self.Accepted
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", EditsWhileAsked)
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (str(doc), "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)
    _no_crash_reports(message_boxes)
    back = _content_back("draft.txt", sandbox)
    assert open(back, "rb").read() == doc.read_bytes(), "what was stored must be what is on disk now"

    gone = sandbox / "gone.txt"
    gone.write_bytes(corpus.lines(800, "{w} {n}\n"))

    class DeletesWhileAsked(FakeSourceActionDialog):
        def exec(self):
            os.remove(gone)
            return self.Accepted
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", DeletesWhileAsked)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (str(gone), "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)
    assert _last(message_boxes)[0] == "warning" and api.search("gone.txt") == []
    _healthy(gui_window, message_boxes)


def test_delete_source_on_a_read_only_file(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "readonly.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    os.chmod(doc, stat.S_IREAD)
    try:
        _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs", action="delete")
        assert "archived and verified" in _last(message_boxes)[2]
        assert open(_content_back("readonly.txt", sandbox), "rb").read() == corpus.lines(900, "{w}\n")
    finally:
        if doc.exists():
            os.chmod(doc, stat.S_IWRITE)
    _healthy(gui_window, message_boxes)


# --- editing a checked-out item -----------------------------------------------------------------------

def _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox, name="notes.txt", lines=2000):
    api.create_category("Docs")
    doc = sandbox / name
    original = corpus.lines(lines, "{i} {w} {n}\n")
    doc.write_bytes(original)
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    _checkout_through_gui(gui_window, qtbot, name)
    return original, workspace_of(name)


def test_the_user_deletes_the_checked_out_copy(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    os.remove(ws)
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    kind, _title, text = _last(message_boxes)
    assert kind == "warning" and "not found" in text and "not saved" in text, message_boxes.calls
    assert _status("notes.txt") == "ARCHIVED", "the item is stuck: it cannot be checked in or checked out again"
    assert open(_content_back("notes.txt", sandbox), "rb").read() == original, "the archived copy must survive"
    _healthy(gui_window, message_boxes)


def test_the_user_replaces_the_copy_with_a_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    os.remove(ws)
    os.makedirs(ws)
    (open(os.path.join(ws, "inside.txt"), "w")).close()
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    _no_crash_reports(message_boxes)
    assert _last(message_boxes)[0] == "warning", message_boxes.calls
    assert _status("notes.txt") == "CHECKED_OUT" and os.path.isdir(ws), "the user's folder must be left alone"
    import shutil
    shutil.rmtree(ws)  # the user removes the folder and tries again
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert _status("notes.txt") == "ARCHIVED"
    assert open(_content_back("notes.txt", sandbox), "rb").read() == original
    _healthy(gui_window, message_boxes)


def test_checkin_while_the_copy_is_still_open_in_another_program(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    with open(ws, "ab") as f:
        f.write(b"unsaved? no - saved edit\n")
    with locked(ws):
        _checkin_through_gui(gui_window, qtbot, "notes.txt")
        assert _last(message_boxes)[0] == "warning"
        assert _status("notes.txt") == "CHECKED_OUT" and os.path.exists(ws)
    _checkin_through_gui(gui_window, qtbot, "notes.txt")  # closed the program, tries again
    assert "checked in" in _last(message_boxes)[2]
    assert open(_content_back("notes.txt", sandbox), "rb").read() == original + b"unsaved? no - saved edit\n"
    _healthy(gui_window, message_boxes)


def test_the_user_empties_the_file(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    _original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    open(ws, "wb").close()
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert "checked in" in _last(message_boxes)[2]
    assert open(_content_back("notes.txt", sandbox), "rb").read() == b""
    _healthy(gui_window, message_boxes)


def test_the_user_reorganizes_a_checked_out_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    project = sandbox / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.py").write_bytes(corpus.lines(600, "x{i} = {n}\n"))
    (project / "readme.md").write_bytes(b"# readme\n")
    _archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
    _checkout_through_gui(gui_window, qtbot, "project")
    ws = workspace_of("project")
    os.remove(os.path.join(ws, "readme.md"))
    os.rename(os.path.join(ws, "src"), os.path.join(ws, "source"))
    os.makedirs(os.path.join(ws, "empty", "deeper"))
    # Hebrew on purpose: non-ASCII, right-to-left names must archive and restore exactly
    with open(os.path.join(ws, "הערות.txt"), "w", encoding="utf-8") as f:
        f.write("שלום\n")
    expected = {}
    for root, _dirs, files in os.walk(ws):
        for n in files:
            full = os.path.join(root, n)
            expected[os.path.relpath(full, ws)] = open(full, "rb").read()
    _checkin_through_gui(gui_window, qtbot, "project")
    assert "checked in" in _last(message_boxes)[2]
    back = _content_back("project", sandbox)
    got = {}
    for root, _dirs, files in os.walk(back):
        for n in files:
            full = os.path.join(root, n)
            got[os.path.relpath(full, back)] = open(full, "rb").read()
    assert got == expected
    assert os.path.isdir(os.path.join(back, "empty", "deeper")), "an empty folder the user made was lost"
    _healthy(gui_window, message_boxes)


def test_checking_out_twice_and_checking_in_twice(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    _checkout_through_gui(gui_window, qtbot, "notes.txt")
    assert "Only archived items" in _last(message_boxes)[2]
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    _checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert "Only checked-out items" in _last(message_boxes)[2]
    assert _status("notes.txt") == "ARCHIVED" and not os.path.exists(ws)
    _healthy(gui_window, message_boxes)


# --- the user touches the archive files by hand -------------------------------------------------------

@pytest.mark.parametrize("damage", ["append", "truncate", "flip", "delete"])
def test_hand_damaged_archives_are_reported_not_trusted(gui_window, qtbot, monkeypatch, message_boxes, sandbox, damage):
    api.create_category("Docs")
    api.create_category("Other")
    doc = sandbox / "doc.txt"
    doc.write_bytes(corpus.lines(3000, "{i} {w} {n}\n"))
    other = sandbox / "other.txt"
    other.write_bytes(corpus.lines(900, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(other), "Other")
    zip_path = api.get_archive(api.get_item(api.search("doc.txt")[0][0])[1])[3]
    size = os.path.getsize(zip_path)
    if damage == "append":
        with open(zip_path, "ab") as f:
            f.write(b"garbage")
    elif damage == "truncate":
        with open(zip_path, "r+b") as f:
            f.truncate(size // 2)
    elif damage == "flip":
        with open(zip_path, "r+b") as f:
            f.seek(size // 3)
            b = f.read(1)
            f.seek(size // 3)
            f.write(bytes([b[0] ^ 0xFF]))
    else:
        os.remove(zip_path)

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "doc.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)
    assert _last(message_boxes)[0] == "warning" and "VERIFIED" not in _last(message_boxes)[2], message_boxes.calls

    _checkout_through_gui(gui_window, qtbot, "doc.txt")
    _no_crash_reports(message_boxes)
    for root, _dirs, files in os.walk(os.path.join(api.data_root(), "workspace")):
        for n in files:
            full = os.path.join(root, n)
            assert open(full, "rb").read() == doc.read_bytes(), "a damaged restore reached the workspace"
    assert set(api.verify_category("Other").values()) == {"VERIFIED"}, "damage spread to another category"
    assert not gui_window.is_busy()


def test_the_database_is_deleted_while_the_app_is_closed(sandbox, qtbot, message_boxes):
    from gui.main_window import MainWindow
    api.initialize()
    api.create_category("Docs")
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(500, "{w}\n"))
    assert api.archive_file(str(doc), "Docs")
    os.remove(api.config.db_path())
    api.initialize()
    win = MainWindow()
    qtbot.addWidget(win)
    win._closing_in_progress = True
    win.refresh_all()
    _no_crash_reports(message_boxes)
    assert os.listdir(os.path.join(api.data_root(), "archives")), "the archives themselves must not be touched"
    win.close()


# --- settings -----------------------------------------------------------------------------------------

def test_data_location_where_the_app_cannot_create_its_folders(gui_window, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    blocked = sandbox / "blocked"
    blocked.mkdir()
    (blocked / "archives").write_text("a file where a folder must go")
    message_boxes.warning_confirm = True
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: str(blocked)))
    before = api.get_storage_locations()
    gui_window.settings_tab._change_data_root()
    _no_crash_reports(message_boxes)
    assert _last(message_boxes)[0] == "warning", message_boxes.calls
    assert api.get_storage_locations() == before, "a failed switch must leave the old location in place"
    assert [c[1] for c in api.list_categories()] == ["Docs"]
    api.initialize()  # the next start must work too
    assert [c[1] for c in api.list_categories()] == ["Docs"]


@pytest.mark.parametrize("where", ["temp", "archives", "data_root"])
def test_workspace_inside_the_apps_own_folders(gui_window, qtbot, monkeypatch, message_boxes, sandbox, where):
    api.create_category("Docs")
    target = {"temp": os.path.join(api.data_root(), "temp", "ws"), "archives": os.path.join(api.data_root(), "archives"),
              "data_root": api.data_root()}[where]
    os.makedirs(target, exist_ok=True)
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: target))
    gui_window.settings_tab._change_workspace_root()
    _no_crash_reports(message_boxes)
    doc = sandbox / "work.txt"
    doc.write_bytes(corpus.lines(900, "{i} {w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    _checkout_through_gui(gui_window, qtbot, "work.txt")
    ws = workspace_of("work.txt")
    with open(ws, "ab") as f:
        f.write(b"an hour of work\n")
    api.initialize()  # the app is restarted before checking in
    assert os.path.exists(ws), "restarting the app deleted the user's checked-out edits"
    _checkin_through_gui(gui_window, qtbot, "work.txt")
    assert open(_content_back("work.txt", sandbox), "rb").read().endswith(b"an hour of work\n")
    _healthy(gui_window, message_boxes)


def test_every_button_while_an_operation_runs(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    small = sandbox / "small.txt"
    small.write_bytes(corpus.lines(500, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(small), "Docs")
    big = sandbox / "big.txt"
    big.write_bytes(corpus.lines(200000, "{i} {w} {n} payload\n"))
    FakeSourceActionDialog._next_action = "keep"
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", FakeSourceActionDialog)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (str(big), "")))
    _type_category(monkeypatch, "Sneaky")
    message_boxes.warning_confirm = True
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab._add_file()
    assert gui_window.is_busy()
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "small.txt")
    for click in (gui_window.items_tab._add_file, gui_window.items_tab._add_folder, gui_window.items_tab._checkout,
                  gui_window.items_tab._checkin, gui_window.items_tab._verify_selected,
                  gui_window.categories_tab._delete_category, gui_window.categories_tab._edit_category,
                  gui_window.settings_tab._change_data_root, gui_window.settings_tab._change_workspace_root,
                  gui_window.settings_tab._reset_workspace_root, gui_window.dashboard_tab._verify_all):
        click()
    wait_idle(qtbot, gui_window, timeout=120000)
    assert sorted(r[1] for r in api.search("")) == ["big.txt", "small.txt"]
    assert _status("small.txt") == "ARCHIVED"
    _healthy(gui_window, message_boxes)


@pytest.mark.parametrize("text", ["%", "_", "[a-z]", "'; DROP TABLE items;--", "\\", "*", "\"", "💥", " " * 50])
def test_searching_for_strange_text(gui_window, qtbot, monkeypatch, message_boxes, sandbox, text):
    api.create_category("Docs")
    doc = sandbox / "100% done_[final].txt"
    doc.write_bytes(corpus.lines(300, "{w}\n"))
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    gui_window.items_tab.search_edit.setText(text)
    gui_window.items_tab.refresh()
    _no_crash_reports(message_boxes)
    assert not [c for c in message_boxes.calls if c[1] == "Search"], message_boxes.calls
    shown = [gui_window.items_tab.table.item(r, 0).text() for r in range(gui_window.items_tab.table.rowCount())]
    if text in ("_", "*", "\"", "💥", "[a-z]"):
        assert shown in ([], ["100% done_[final].txt"])
    if text == "%":
        assert shown == ["100% done_[final].txt"]
    assert len(api.search("")) == 1


def test_a_disconnect_that_could_not_finish_is_reported_not_hidden(gui_window, message_boxes, monkeypatch):
    from gui.settings_tab import DISCONNECT_FAILED_MESSAGE
    tab = gui_window.settings_tab
    monkeypatch.setattr(api, "cloud_disconnect", lambda: False)
    message_boxes.warning_confirm = True
    tab._disconnect_cloud()
    assert message_boxes.calls[-1] == ("warning", "Disconnect", DISCONNECT_FAILED_MESSAGE)
    assert "another program" in DISCONNECT_FAILED_MESSAGE and "Try again" in DISCONNECT_FAILED_MESSAGE


def test_a_disconnect_that_finished_shows_no_warning(gui_window, message_boxes, monkeypatch):
    tab = gui_window.settings_tab
    monkeypatch.setattr(api, "cloud_disconnect", lambda: True)
    message_boxes.warning_confirm = True
    tab._disconnect_cloud()
    assert [c for c in message_boxes.calls if c[0] == "warning"] == [
        ("warning", "Disconnect", "This clears the local Cloud connection and turns Telemetry off. Continue?")]
