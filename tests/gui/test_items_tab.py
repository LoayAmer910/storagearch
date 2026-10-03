"""gui.items_tab - archive, check out, check in, verify and search through the window, wrong actions included."""
import ctypes
import os
import stat
import zipfile

import pytest

from core import api, api as main
from support import corpus
from support.files import locked
from support.gui import (
    app_is_healthy,
    archive_file_through_gui,
    archive_folder_through_gui,
    checkin_through_gui,
    checkout_through_gui,
    content_after_checkout,
    item_status,
    last_message,
    no_crash_reports,
    select_row_by_name,
    set_fake_source_action,
    wait_idle,
    workspace_of,
    FakeSourceActionDialog,
)


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def long_path(path):
    return "\\\\?\\" + os.path.abspath(path)


def _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox, name="notes.txt", lines=2000):
    api.create_category("Docs")
    doc = sandbox / name
    original = corpus.lines(lines, "{i} {w} {n}\n")
    doc.write_bytes(original)
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    checkout_through_gui(gui_window, qtbot, name)
    return original, workspace_of(name)


def test_archive_file_through_gui_creates_real_zip_manifest_and_db_row(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("report.txt", "quarterly report content")

    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("report.txt")
    assert len(items) == 1
    zip_path = items[0][10]
    assert os.path.isfile(zip_path)
    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    assert os.path.isfile(manifest_path)
    # path_in_archive is now a collision-safe SHA-256-prefixed member name, not the
    # plain basename (spec 11.6 fix) - look up the real internal path via the DB.
    with zipfile.ZipFile(zip_path) as zf:
        assert items[0][3] in zf.namelist()

    gui_window.items_tab.refresh()
    row = select_row_by_name(gui_window.items_tab.table, "report.txt")
    assert gui_window.items_tab.table.item(row, 6).text() == "ARCHIVED"


def test_archive_folder_through_gui_preserves_nested_structure(gui_window, qtbot, monkeypatch, make_folder):
    main.create_category("Code")
    folder = make_folder("project", {"root.txt": "root", "sub/inner.txt": "inner"})

    archive_folder_through_gui(gui_window, qtbot, monkeypatch, folder, "Code")

    items = main.search("project")
    assert len(items) == 1
    assert items[0][2] == "folder"
    zip_path = items[0][10]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert any(name.endswith("root.txt") for name in names)
        assert any(name.endswith("sub/inner.txt") for name in names)


def test_source_removed_only_after_successful_archive(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("todelete.txt", "gone soon")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs", action="delete")
    assert not os.path.exists(path)
    items = main.search("todelete.txt")
    assert len(items) == 1


def test_checkout_extracts_item_and_updates_status(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("checkout_me.txt", "content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "checkout_me.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    items = main.search("checkout_me.txt")
    assert items[0][7] == "CHECKED_OUT"
    workspace_path = workspace_of("checkout_me.txt")
    assert os.path.isfile(workspace_path)

    gui_window.items_tab.refresh()
    row = select_row_by_name(gui_window.items_tab.table, "checkout_me.txt")
    assert gui_window.items_tab.table.item(row, 6).text() == "CHECKED_OUT"


def test_checkout_already_checked_out_item_is_handled_gracefully(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("twice.txt", "content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "twice.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "twice.txt")
    gui_window.items_tab._checkout()  # button would be disabled in the real UI; guard must still hold
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


def test_checkin_unchanged_item_returns_to_archived(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("unchanged.txt", "same content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "unchanged.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "unchanged.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("unchanged.txt")
    assert items[0][7] == "ARCHIVED"


def test_checkin_modified_file_updates_zip_manifest_and_hash(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("editme.txt", "before edit")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "editme.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    workspace_path = workspace_of("editme.txt")
    with open(workspace_path, "a", encoding="utf-8") as f:
        f.write(" - after edit")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "editme.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("editme.txt")
    assert items[0][7] == "ARCHIVED"
    zip_path = items[0][10]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(items[0][3]).decode("utf-8") == "before edit - after edit"

    status = main.verify_archive_by_id(main.get_item(items[0][0])[1])
    assert status == "VERIFIED"


def test_checkin_modified_folder_preserves_unrelated_members(gui_window, qtbot, monkeypatch, make_folder):
    main.create_category("Code")
    folder = make_folder("proj", {"a.txt": "a-content", "b.txt": "b-content"})
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, folder, "Code")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "proj")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    workspace_folder = workspace_of("proj")
    with open(os.path.join(workspace_folder, "a.txt"), "a", encoding="utf-8") as f:
        f.write("-edited")
    before = {m["relative_path"]: m["path_in_archive"] for m in main._cat().members_of_item(main.search("proj")[0][0])}

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "proj")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("proj")
    zip_path = items[0][10]
    # the edited file is written again; the unchanged one stays the very member it was before the checkin
    after = {m["relative_path"]: m["path_in_archive"] for m in main._cat().members_of_item(items[0][0])}
    assert after["proj/a.txt"] != before["proj/a.txt"]
    assert after["proj/b.txt"] == before["proj/b.txt"]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(after["proj/a.txt"]).decode("utf-8") == "a-content-edited"
        assert zf.read(after["proj/b.txt"]).decode("utf-8") == "b-content"  # unrelated member untouched


def test_checkin_without_being_checked_out_is_rejected(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("notcheckedout.txt")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "notcheckedout.txt")
    gui_window.items_tab._checkin()  # button is disabled in the real UI; guard must still hold
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


def test_verify_reports_verified_status(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("verifyme.txt")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "verifyme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "VERIFIED" in status_calls[-1][2]


def test_verify_detects_corrupted_archive(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("corruptme.txt")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("corruptme.txt")
    zip_path = items[0][10]
    with open(zip_path, "ab") as f:
        f.write(b"garbage-bytes-to-corrupt-the-zip")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "corruptme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "CORRUPTED" in status_calls[-1][2] or "CHANGED" in status_calls[-1][2]


def test_verify_detects_missing_archive(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("missingme.txt")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("missingme.txt")
    zip_path = items[0][10]
    os.remove(zip_path)

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "missingme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "MISSING" in status_calls[-1][2]


def test_items_tab_shows_specific_failure_message_not_generic_text(gui_window, message_boxes):
    gui_window.items_tab._pending_archive = ("some/path.txt", "Docs", False)
    gui_window.items_tab._on_archive_done(
        {"success": False, "reason": "INSUFFICIENT_SPACE", "message": "Not enough free space to archive file"}
    )
    assert any(
        call[0] == "warning" and call[2] == "Not enough free space to archive file"
        for call in message_boxes.calls
    )


def test_a_file_another_program_holds_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "open_in_word.docx"
    doc.write_bytes(corpus.lines(2000, "{i} {w}\n"))
    with locked(doc):
        archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
        kind, _title, text = last_message(message_boxes)
        assert kind == "warning" and "open_in_word.docx" in text and "open in another program" in text, message_boxes.calls
    assert api.search("") == []
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")  # closed it, tries again
    assert "archived and verified" in last_message(message_boxes)[2]
    app_is_healthy(gui_window, message_boxes)


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
    kind, _title, text = last_message(message_boxes)
    assert kind == "warning" and "report.xlsx" in text and "open in another program" in text, message_boxes.calls
    assert api.search("") == []
    app_is_healthy(gui_window, message_boxes)


def test_a_folder_with_one_file_held_open(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    project = sandbox / "project"
    project.mkdir()
    (project / "a.txt").write_bytes(corpus.lines(900, "{w}\n"))
    (project / "busy.log").write_bytes(corpus.lines(900, "{i}\n"))
    with locked(project / "busy.log"):
        archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
        kind, _title, text = last_message(message_boxes)
        assert kind == "warning" and "busy.log" in text and "open in another program" in text, message_boxes.calls
        assert "name is not valid" not in text
    assert api.search("") == [], "a folder must never be stored with a file silently missing"
    app_is_healthy(gui_window, message_boxes)


@pytest.mark.parametrize("which", ["data_root", "archives", "workspace", "temp"])
def test_archiving_the_apps_own_folders_is_refused(gui_window, qtbot, monkeypatch, message_boxes, which):
    api.create_category("Docs")
    target = {"data_root": api.data_root(), "archives": os.path.join(api.data_root(), "archives"),
              "workspace": os.path.join(api.data_root(), "workspace"), "temp": os.path.join(api.data_root(), "temp")}[which]
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, target, "Docs")
    assert last_message(message_boxes)[0] == "warning", message_boxes.calls
    assert api.search("") == []
    app_is_healthy(gui_window, message_boxes)


def test_archiving_the_copy_that_is_checked_out(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "notes.txt"
    doc.write_bytes(corpus.lines(1200, "{i} {w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    checkout_through_gui(gui_window, qtbot, "notes.txt")
    ws = workspace_of("notes.txt")
    with open(ws, "a", encoding="utf-8") as f:
        f.write("edited\n")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, ws, "Docs")
    assert last_message(message_boxes)[0] == "warning", message_boxes.calls
    assert len(api.search("notes.txt")) == 1
    app_is_healthy(gui_window, message_boxes)


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
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    kind_shown = last_message(message_boxes)[0]
    assert kind_shown in ("warning", "information"), message_boxes.calls
    if kind_shown == "information":
        back = content_after_checkout(os.path.basename(path).rstrip(". "), sandbox) or content_after_checkout(os.path.basename(path), sandbox)
        assert back and open(long_path(back), "rb").read() == open(long_path(path), "rb").read()
    app_is_healthy(gui_window, message_boxes)


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
    no_crash_reports(message_boxes)
    back = content_after_checkout("draft.txt", sandbox)
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
    assert last_message(message_boxes)[0] == "warning" and api.search("gone.txt") == []
    app_is_healthy(gui_window, message_boxes)


def test_delete_source_on_a_read_only_file(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "readonly.txt"
    doc.write_bytes(corpus.lines(900, "{w}\n"))
    os.chmod(doc, stat.S_IREAD)
    try:
        archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs", action="delete")
        assert "archived and verified" in last_message(message_boxes)[2]
        assert open(content_after_checkout("readonly.txt", sandbox), "rb").read() == corpus.lines(900, "{w}\n")
    finally:
        if doc.exists():
            os.chmod(doc, stat.S_IWRITE)
    app_is_healthy(gui_window, message_boxes)


def test_the_user_deletes_the_checked_out_copy(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    os.remove(ws)
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    kind, _title, text = last_message(message_boxes)
    assert kind == "warning" and "not found" in text and "not saved" in text, message_boxes.calls
    assert item_status("notes.txt") == "ARCHIVED", "the item is stuck: it cannot be checked in or checked out again"
    assert open(content_after_checkout("notes.txt", sandbox), "rb").read() == original, "the archived copy must survive"
    app_is_healthy(gui_window, message_boxes)


def test_the_user_replaces_the_copy_with_a_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    os.remove(ws)
    os.makedirs(ws)
    (open(os.path.join(ws, "inside.txt"), "w")).close()
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    no_crash_reports(message_boxes)
    assert last_message(message_boxes)[0] == "warning", message_boxes.calls
    assert item_status("notes.txt") == "CHECKED_OUT" and os.path.isdir(ws), "the user's folder must be left alone"
    import shutil
    shutil.rmtree(ws)  # the user removes the folder and tries again
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert item_status("notes.txt") == "ARCHIVED"
    assert open(content_after_checkout("notes.txt", sandbox), "rb").read() == original
    app_is_healthy(gui_window, message_boxes)


def test_checkin_while_the_copy_is_still_open_in_another_program(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    with open(ws, "ab") as f:
        f.write(b"unsaved? no - saved edit\n")
    with locked(ws):
        checkin_through_gui(gui_window, qtbot, "notes.txt")
        assert last_message(message_boxes)[0] == "warning"
        assert item_status("notes.txt") == "CHECKED_OUT" and os.path.exists(ws)
    checkin_through_gui(gui_window, qtbot, "notes.txt")  # closed the program, tries again
    assert "checked in" in last_message(message_boxes)[2]
    assert open(content_after_checkout("notes.txt", sandbox), "rb").read() == original + b"unsaved? no - saved edit\n"
    app_is_healthy(gui_window, message_boxes)


def test_the_user_empties_the_file(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    _original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    open(ws, "wb").close()
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert "checked in" in last_message(message_boxes)[2]
    assert open(content_after_checkout("notes.txt", sandbox), "rb").read() == b""
    app_is_healthy(gui_window, message_boxes)


def test_the_user_reorganizes_a_checked_out_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    project = sandbox / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.py").write_bytes(corpus.lines(600, "x{i} = {n}\n"))
    (project / "readme.md").write_bytes(b"# readme\n")
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
    checkout_through_gui(gui_window, qtbot, "project")
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
    checkin_through_gui(gui_window, qtbot, "project")
    assert "checked in" in last_message(message_boxes)[2]
    back = content_after_checkout("project", sandbox)
    got = {}
    for root, _dirs, files in os.walk(back):
        for n in files:
            full = os.path.join(root, n)
            got[os.path.relpath(full, back)] = open(full, "rb").read()
    assert got == expected
    assert os.path.isdir(os.path.join(back, "empty", "deeper")), "an empty folder the user made was lost"
    app_is_healthy(gui_window, message_boxes)


def test_checking_out_twice_and_checking_in_twice(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    original, ws = _archived_and_checked_out(gui_window, qtbot, monkeypatch, sandbox)
    checkout_through_gui(gui_window, qtbot, "notes.txt")
    assert "Only archived items" in last_message(message_boxes)[2]
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    checkin_through_gui(gui_window, qtbot, "notes.txt")
    assert "Only checked-out items" in last_message(message_boxes)[2]
    assert item_status("notes.txt") == "ARCHIVED" and not os.path.exists(ws)
    app_is_healthy(gui_window, message_boxes)


@pytest.mark.parametrize("damage", ["append", "truncate", "flip", "delete"])
def test_hand_damaged_archives_are_reported_not_trusted(gui_window, qtbot, monkeypatch, message_boxes, sandbox, damage):
    api.create_category("Docs")
    api.create_category("Other")
    doc = sandbox / "doc.txt"
    doc.write_bytes(corpus.lines(3000, "{i} {w} {n}\n"))
    other = sandbox / "other.txt"
    other.write_bytes(corpus.lines(900, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(other), "Other")
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
    select_row_by_name(gui_window.items_tab.table, "doc.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)
    assert last_message(message_boxes)[0] == "warning" and "VERIFIED" not in last_message(message_boxes)[2], message_boxes.calls

    checkout_through_gui(gui_window, qtbot, "doc.txt")
    no_crash_reports(message_boxes)
    for root, _dirs, files in os.walk(os.path.join(api.data_root(), "workspace")):
        for n in files:
            full = os.path.join(root, n)
            assert open(full, "rb").read() == doc.read_bytes(), "a damaged restore reached the workspace"
    assert set(api.verify_category("Other").values()) == {"VERIFIED"}, "damage spread to another category"
    assert not gui_window.is_busy()


@pytest.mark.parametrize("text", ["%", "_", "[a-z]", "'; DROP TABLE items;--", "\\", "*", "\"", "💥", " " * 50])
def test_searching_for_strange_text(gui_window, qtbot, monkeypatch, message_boxes, sandbox, text):
    api.create_category("Docs")
    doc = sandbox / "100% done_[final].txt"
    doc.write_bytes(corpus.lines(300, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    gui_window.items_tab.search_edit.setText(text)
    gui_window.items_tab.refresh()
    no_crash_reports(message_boxes)
    assert not [c for c in message_boxes.calls if c[1] == "Search"], message_boxes.calls
    shown = [gui_window.items_tab.table.item(r, 0).text() for r in range(gui_window.items_tab.table.rowCount())]
    if text in ("_", "*", "\"", "💥", "[a-z]"):
        assert shown in ([], ["100% done_[final].txt"])
    if text == "%":
        assert shown == ["100% done_[final].txt"]
    assert len(api.search("")) == 1


def test_archiving_with_no_category_asks_for_one(gui_window, monkeypatch, message_boxes, make_file):
    path = make_file("a.txt", "content")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab._add_file()
    kind, _, text = last_message(message_boxes)[:3]
    assert kind == "warning" and "Create a category first" in text
    assert api.search("") == []


def test_cancelling_the_file_picker_does_nothing(gui_window, monkeypatch, message_boxes):
    api.create_category("Docs")
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    gui_window.items_tab._add_file()
    assert message_boxes.calls == [] and not gui_window.is_busy()
    assert api.search("") == []


@pytest.mark.parametrize("bad", ["missing", "protected", "data_root"])
def test_bad_sources_are_explained_and_nothing_is_stored(gui_window, qtbot, monkeypatch, message_boxes, sandbox, bad):
    api.create_category("Docs")
    path = {"missing": str(sandbox / "no_such_file.txt"),
            "protected": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "win.ini"),
            "data_root": os.path.join(api.data_root(), "smartarchive.db")}[bad]
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    assert message_boxes.calls and message_boxes.calls[-1][0] in ("warning", "critical"), message_boxes.calls
    assert api.search("") == []


@pytest.mark.parametrize("action, title", [("_checkout", "Checkout"), ("_checkin", "Checkin"), ("_verify_selected", "Verify")])
def test_actions_without_a_selection_explain_and_do_nothing(gui_window, qtbot, message_boxes, action, title):
    table = gui_window.items_tab.table
    table.clearSelection()
    table.setCurrentCell(-1, -1)
    getattr(gui_window.items_tab, action)()
    wait_idle(qtbot, gui_window)
    assert not gui_window.is_busy()
    kind, shown_title, text = last_message(message_boxes)[:3]
    assert (kind, shown_title) == ("information", title)
    assert text.startswith("Select ") and text.endswith(" first.")


def test_a_second_click_while_busy_is_ignored(gui_window, qtbot, monkeypatch, sandbox):
    api.create_category("Docs")
    big = sandbox / "big.txt"
    big.write_bytes(corpus.lines(60000, "{i} {w} {n} payload\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(big), "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "big.txt")
    gui_window.items_tab._checkout()
    assert gui_window.is_busy()
    gui_window.items_tab._checkout()  # impatient user
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)
    assert api.get_item(api.search("big.txt")[0][0])[10] == "CHECKED_OUT"


def test_checkout_edit_checkin_and_a_second_checkin(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    doc = sandbox / "notes.txt"
    doc.write_bytes(corpus.lines(3000, "{i} {w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)
    ws = workspace_of("notes.txt")
    with open(ws, "a", encoding="utf-8") as f:
        f.write("my edit\n")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)
    assert not os.path.exists(ws)
    before = len(message_boxes.calls)
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "notes.txt")
    gui_window.items_tab._checkin()  # not checked out any more
    wait_idle(qtbot, gui_window)
    assert len(message_boxes.calls) >= before
    back = api.checkout(api.search("notes.txt")[0][0], destination=str(sandbox / "back"))
    assert open(back, encoding="utf-8").read().endswith("my edit\n")


def test_rapid_add_file_clicks_do_not_start_two_archive_operations(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("rapidfile.txt", "content")

    set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))

    gui_window.items_tab._add_file()
    assert gui_window.is_busy()
    gui_window.items_tab._add_file()  # second click while the first is mid-flight must be a no-op
    gui_window.items_tab._add_file()  # third for good measure
    wait_idle(qtbot, gui_window)

    items = main.search("rapidfile.txt")
    assert len(items) == 1  # only one archive write happened, not three


def test_rapid_verify_clicks_do_not_corrupt_state(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("verifyrapid.txt", "content")
    set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "verifyrapid.txt")

    for _ in range(5):
        gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status = main.verify_archive_by_id(main.get_item(main.search("verifyrapid.txt")[0][0])[1])
    assert status == "VERIFIED"


@pytest.mark.parametrize("first, second", [
    ("alpha.txt", "beta.txt"),                                              # different name
    (os.path.join("dir1", "twin.txt"), os.path.join("dir2", "twin.txt")),  # same name, another folder
])
def test_same_content_is_a_duplicate_whatever_the_name(gui_window, qtbot, monkeypatch, sandbox, message_boxes,
                                                        first, second):
    # Duplicates are found by the SHA-256 of the content, never by name or path; the second copy is not stored.
    main.create_category("Docs")
    paths = []
    for relative in (first, second):
        path = sandbox / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("identical payload", encoding="utf-8")
        paths.append(str(path))

    for path in paths:
        archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("", category_name="Docs")
    assert len(items) == 1
    assert items[0][1] == os.path.basename(first)  # the first-archived name is what's kept
    assert any(call[0] == "information" and "Duplicate" in call[1] for call in message_boxes.calls)


# --- what else can go wrong around adding, listing and verifying ----------------------------------------

def _broken(*args, **kwargs):
    raise OSError("database is locked")


def test_a_search_that_fails_is_explained(gui_window, monkeypatch, message_boxes):
    monkeypatch.setattr(main, "search", _broken)
    gui_window.items_tab.refresh()
    assert last_message(message_boxes)[:3] == ("warning", "Search", "Search failed:\ndatabase is locked")


def test_categories_that_cannot_be_loaded_leave_the_lists_empty_not_broken(gui_window, monkeypatch):
    main.create_category("Docs")
    monkeypatch.setattr(main, "list_categories", _broken)
    gui_window.items_tab.refresh_categories()
    assert gui_window.items_tab.destination_category.count() == 0


def test_cancelling_the_folder_picker_does_nothing(gui_window, monkeypatch, message_boxes):
    main.create_category("Docs")
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: ""))
    gui_window.items_tab._add_folder()
    assert message_boxes.calls == [] and not gui_window.is_busy() and main.search("") == []


def test_a_folder_removed_after_it_was_chosen_is_explained(gui_window, message_boxes, sandbox):
    main.create_category("Docs")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab._start_archive_flow(str(sandbox / "gone"), is_folder=True)
    assert last_message(message_boxes)[:3] == ("warning", "Add item", "The selected folder no longer exists.")
    assert not gui_window.is_busy()


@pytest.mark.parametrize("failure, shown", [
    (main.CloudPlaceholderError("not on this PC"), "is in OneDrive and is not downloaded to this PC"),
    (main.InvalidPathError("bad name"), "Cannot read the source:\nbad name"),
    (OSError("device not ready"), "Unable to read source content: device not ready"),
    (None, "Unable to read source content."),
], ids=["cloud", "invalid", "os_error", "unreadable"])
def test_a_source_that_cannot_be_read_for_the_duplicate_check_is_explained(gui_window, qtbot, monkeypatch, make_file,
                                                                             message_boxes, failure, shown):
    def hash_of(_path):
        if failure is None:
            return None
        raise failure

    monkeypatch.setattr(main, "calculate_file_hash", hash_of)
    main.create_category("Docs")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, make_file("a.txt"), "Docs")
    kind, title, text = last_message(message_boxes)[:3]
    assert (kind, title) == ("warning", "Add item") and shown in text
    assert main.search("") == [] and not gui_window.is_busy()


def test_a_duplicate_of_an_archive_that_no_longer_verifies_stops_for_safety(gui_window, qtbot, monkeypatch, make_file,
                                                                          message_boxes):
    main.create_category("Docs")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, make_file("first.txt", "same bytes"), "Docs")
    os.remove(main.get_archive(main.get_item(main.search("first.txt")[0][0])[1])[3])  # its archive is gone
    archive_file_through_gui(gui_window, qtbot, monkeypatch, make_file("second.txt", "same bytes"), "Docs")
    assert last_message(message_boxes)[:2] == ("warning", "Duplicate found but not trustworthy")
    assert main.search("second.txt") == []


def test_cancelling_the_keep_or_delete_question_archives_nothing(gui_window, qtbot, monkeypatch, make_file):
    class Cancelled(FakeSourceActionDialog):
        def exec(self):
            return self.Rejected

    main.create_category("Docs")
    gui_window.items_tab.refresh_categories()
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", Cancelled)
    path = make_file("b.txt", "other content")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)
    assert main.search("b.txt") == [] and os.path.exists(path)


def test_a_folder_with_files_left_in_onedrive_lists_them_in_the_result(gui_window, qtbot, monkeypatch, sandbox,
                                                                       message_boxes):
    folder = sandbox / "project"
    folder.mkdir()
    (folder / "here.txt").write_text("on this PC")
    cloud = folder / "online_only.docx"
    cloud.write_text("only a placeholder")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(cloud), 0x1000)  # FILE_ATTRIBUTE_OFFLINE
    main.create_category("Docs")
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(folder), "Docs")
    text = last_message(message_boxes)[2]
    assert "1 file(s) were skipped because they are in OneDrive" in text and "  - online_only.docx" in text


def test_a_duplicate_found_only_while_writing_is_shown_as_a_duplicate(gui_window, message_boxes):
    duplicate = {"item_name": "a.txt", "category": "Docs", "archive": "Docs_001.zip", "path_in_archive": "u/a.txt",
                 "status": "VERIFIED"}
    gui_window.items_tab._pending_archive = ("C:/x/a.txt", "Docs", False)
    gui_window.items_tab._on_archive_done({"success": True, "reason": "DUPLICATE", "duplicate": duplicate})
    assert last_message(message_boxes)[:2] == ("information", "Duplicate content found")


def test_a_success_without_size_details_still_says_archived(gui_window, message_boxes):
    gui_window.items_tab._pending_archive = ("C:/x/a.txt", "Docs", False)
    gui_window.items_tab._on_archive_done({"success": True, "reason": "OK", "message": "done"})
    assert last_message(message_boxes)[:3] == ("information", "Add item", "'a.txt' archived and verified successfully.")


def test_an_archive_operation_that_crashes_is_reported_and_the_window_is_usable(gui_window, qtbot, monkeypatch,
                                                                                make_file, message_boxes):
    monkeypatch.setattr(main, "archive_file_result", _broken)  # raises instead of returning a result
    main.create_category("Docs")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, make_file("a.txt"), "Docs")
    kind, title, text = last_message(message_boxes)[:3]
    assert (kind, title) == ("critical", "Add item") and text.startswith("Operation failed:")
    assert not gui_window.is_busy() and gui_window.items_tab._pending_archive is None


def test_verifying_an_item_that_was_removed_meanwhile_is_explained(gui_window, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    main.archive_file(make_file("a.txt"), "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "a.txt")
    monkeypatch.setattr(main, "get_item", lambda item_id: None)
    gui_window.items_tab._verify_selected()
    assert last_message(message_boxes)[:3] == ("warning", "Verify", "Item no longer exists.")


def test_a_verify_that_crashes_is_reported(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    main.archive_file(make_file("a.txt"), "Docs")
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "a.txt")
    monkeypatch.setattr(main, "verify_archive_by_id", _broken)
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)
    kind, title, text = last_message(message_boxes)[:3]
    assert (kind, title) == ("critical", "Operation failed") and "database is locked" in text
