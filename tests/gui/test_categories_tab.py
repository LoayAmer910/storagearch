"""gui.categories_tab - creating, renaming and deleting categories through the window, wrong input included."""
import os
import zipfile

import pytest
from PySide6.QtWidgets import QDialog

from core import api, api as main
from gui.dialogs import CreateCategoryDialog
from support import corpus
from support.gui import (
    app_is_healthy,
    archive_file_through_gui,
    category_id_by_name,
    last_message,
    no_crash_reports,
    select_row_by_name,
    type_category_name,
)


def _create_category_through_gui(window, monkeypatch, name, existing_dialog_values=None):
    values = existing_dialog_values or {"name": name, "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    class FakeDialog:
        Accepted = CreateCategoryDialog.Accepted

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return CreateCategoryDialog.Accepted

        def values(self):
            return values

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeDialog)
    window.categories_tab._create_category()


def _type_into_category_form(monkeypatch, name, description=None):
    import gui.categories_tab as tab

    def fake_exec(dialog):
        dialog.name_edit.setText(name)
        if description is not None:
            dialog.description_edit.setText(description)
        return QDialog.Accepted
    monkeypatch.setattr(tab.CreateCategoryDialog, "exec", fake_exec)


CATEGORY_NAMES = [
    ("", "warning", "cannot be empty"),
    ("     ", "warning", "cannot be empty"),
    ("a/b:c*?|", "information", "created"),
    # Hebrew on purpose: non-ASCII category names must work
    ("קטגוריה בעברית", "information", "created"),
    ("x" * 500, "information", "created"),
]


def test_edit_category_name_persists_and_refreshes_gui(gui_window):
    main.create_category("OldName", description="old desc", max_archive_size=2 * 1024 ** 3)
    category_id = category_id_by_name("OldName")

    updated = main.update_category(category_id, "NewName", "new desc", "#00ff00", 5 * 1024 ** 3)
    assert updated is True

    category = main.get_category(category_id)
    assert category[1] == "NewName"
    assert category[2] == "new desc"
    assert category[3] == "#00ff00"
    assert category[4] == 5 * 1024 ** 3

    gui_window.categories_tab.refresh()
    names = [gui_window.categories_tab.table.item(row, 0).text() for row in range(gui_window.categories_tab.table.rowCount())]
    assert "NewName" in names
    assert "OldName" not in names


def test_rename_category_does_not_break_existing_archive_paths(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("report.txt", "content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items_before = main.search("report.txt")
    zip_path_before = items_before[0][10]
    assert os.path.isfile(zip_path_before)

    category_id = category_id_by_name("Docs")
    assert main.update_category(category_id, "Documents") is True

    # existing archive/manifest/db paths remain valid and readable after rename
    items_after = main.search("report.txt")
    assert len(items_after) == 1
    zip_path_after = items_after[0][10]
    assert zip_path_after == zip_path_before  # physical folder is stable, not renamed
    assert os.path.isfile(zip_path_after)
    with zipfile.ZipFile(zip_path_after) as zf:
        assert items_after[0][3] in zf.namelist()

    status = main.verify_archive_by_id(main.get_item(items_after[0][0])[1])
    assert status == "VERIFIED"

    # a NEW archive added after rename goes to the SAME physical folder as the old one
    path2 = os.path.join(os.path.dirname(path), "second.txt")
    with open(path2, "w", encoding="utf-8") as f:
        f.write("second content")
    assert main.archive_file(path2, "Documents") is True
    items2 = main.search("second.txt")
    assert os.path.dirname(items2[0][10]) == os.path.dirname(zip_path_after)


def test_rename_category_through_gui_dialog(gui_window, monkeypatch):
    main.create_category("Renamed1", max_archive_size=2 * 1024 ** 3)
    gui_window.categories_tab.refresh()
    row = None
    for r in range(gui_window.categories_tab.table.rowCount()):
        if gui_window.categories_tab.table.item(r, 0).text() == "Renamed1":
            row = r
    assert row is not None
    gui_window.categories_tab.table.selectRow(row)

    class FakeEditDialog:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return self.Accepted

        def values(self):
            return {"name": "Renamed2", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeEditDialog)
    gui_window.categories_tab._edit_category()

    categories = main.list_categories()
    assert any(c[1] == "Renamed2" for c in categories)
    assert not any(c[1] == "Renamed1" for c in categories)


def test_delete_nonempty_category_is_refused_and_data_survives(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("HasData")
    path = make_file("keepme.txt", "important content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "HasData")

    category_id = category_id_by_name("HasData")
    items_before = main.search("keepme.txt")
    zip_path = items_before[0][10]

    result = main.delete_category(category_id)
    assert result == "NOT_EMPTY"

    # nothing was silently deleted
    assert main.get_category(category_id) is not None
    items_after = main.search("keepme.txt")
    assert len(items_after) == 1
    assert os.path.isfile(zip_path)
    status = main.verify_archive_by_id(main.get_item(items_after[0][0])[1])
    assert status == "VERIFIED"


def test_delete_category_through_gui_shows_correct_messages(gui_window, monkeypatch, message_boxes):
    main.create_category("GuiDeleteEmpty")
    gui_window.categories_tab.refresh()
    for r in range(gui_window.categories_tab.table.rowCount()):
        if gui_window.categories_tab.table.item(r, 0).text() == "GuiDeleteEmpty":
            gui_window.categories_tab.table.selectRow(r)

    message_boxes.warning_confirm = True  # user confirms the "delete category?" prompt
    gui_window.categories_tab._delete_category()
    assert not any(c[1] == "GuiDeleteEmpty" for c in main.list_categories())
    assert last_message(message_boxes)[:3] == ("information", "Delete category", "Category 'GuiDeleteEmpty' deleted.")


@pytest.mark.parametrize("action, title", [
    ("_edit_category", "Edit category"), ("_delete_category", "Delete category"), ("_open_items", "Open items")])
def test_actions_without_a_selection_explain_and_do_nothing(gui_window, message_boxes, action, title):
    main.create_category("Docs")
    gui_window.categories_tab.refresh()
    gui_window.categories_tab.table.clearSelection()
    gui_window.categories_tab.table.setCurrentCell(-1, -1)
    getattr(gui_window.categories_tab, action)()
    assert last_message(message_boxes)[:3] == ("information", title, "Select a category first.")
    assert [c[1] for c in main.list_categories()] == ["Docs"] and gui_window.tabs.currentWidget() is not gui_window.items_tab


def test_category_edit_delete_persist_after_restart(gui_window, sandbox):
    from gui.main_window import MainWindow

    main.create_category("PersistEdit", max_archive_size=2 * 1024 ** 3)
    category_id = category_id_by_name("PersistEdit")
    main.update_category(category_id, "PersistEditRenamed", "desc", "#123456", 3 * 1024 ** 3)
    main.create_category("PersistDeleteMe")
    delete_id = category_id_by_name("PersistDeleteMe")
    assert main.delete_category(delete_id) == "DELETED"

    gui_window.close()
    main.initialize()
    window2 = MainWindow()

    categories = main.list_categories()
    names = [c[1] for c in categories]
    assert "PersistEditRenamed" in names
    assert "PersistDeleteMe" not in names
    renamed = next(c for c in categories if c[1] == "PersistEditRenamed")
    assert renamed[2] == "desc"
    assert renamed[3] == "#123456"
    assert renamed[4] == 3 * 1024 ** 3

    window2._closing_in_progress = True
    window2.close()


def test_create_category_through_gui_persists_to_backend(gui_window, monkeypatch):
    gui_window.categories_tab.refresh()
    _create_category_through_gui(gui_window, monkeypatch, "Projects")

    categories = main.list_categories()
    assert len(categories) == 1
    assert categories[0][1] == "Projects"

    # no empty ZIP should be created just from creating the category
    import os
    archives_dir = os.path.join("SmartArchiveData", "archives", "Projects")
    assert not os.path.isdir(archives_dir) or not any(
        name.endswith(".zip") for name in os.listdir(archives_dir)
    )


def test_create_category_empty_name_is_rejected(gui_window, monkeypatch):
    _create_category_through_gui(
        gui_window, monkeypatch, "", existing_dialog_values={"name": "", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}
    )
    assert main.list_categories() == []


def test_open_items_for_category_switches_tab_and_filters(gui_window, make_file):
    main.create_category("Docs")
    main.create_category("Code")
    main.archive_file(make_file("a.txt"), "Docs")
    main.archive_file(make_file("b.txt", "different content"), "Code")

    gui_window.categories_tab.refresh()
    gui_window.categories_tab.table.selectRow(
        [gui_window.categories_tab.table.item(r, 0).text() for r in range(gui_window.categories_tab.table.rowCount())].index("Docs")
    )
    gui_window.categories_tab._open_items()

    assert gui_window.tabs.currentWidget() is gui_window.items_tab
    assert gui_window.items_tab.table.rowCount() == 1
    assert gui_window.items_tab.table.item(0, 0).text() == "a.txt"


# Hebrew on purpose: non-ASCII category names must work
@pytest.mark.parametrize("name", ["CON", "nul", "com1", "abc.", "trailing ", "..", ".", "a\\b/c", "📁 תמונות",
                                  "tab\there", "line\nbreak", "  Docs  "])
def test_category_names_windows_would_choke_on(gui_window, qtbot, monkeypatch, message_boxes, sandbox, name):
    type_category_name(monkeypatch, name)
    gui_window.categories_tab._create_category()
    no_crash_reports(message_boxes)
    created = api.list_categories()
    if not created:
        assert last_message(message_boxes)[0] == "warning"
        return
    folder = os.path.join(api.data_root(), "archives", created[0][6])
    assert os.path.isdir(folder)
    # and the category is actually usable
    doc = sandbox / "a.txt"
    doc.write_bytes(corpus.lines(500, "{i} {w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), created[0][1])
    assert "archived and verified" in last_message(message_boxes)[2], message_boxes.calls
    app_is_healthy(gui_window, message_boxes)


def test_two_categories_differing_only_in_case_do_not_share_a_folder(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    for name in ("Docs", "docs"):
        type_category_name(monkeypatch, name)
        gui_window.categories_tab._create_category()
        gui_window.categories_tab.refresh()
    names = [c[1] for c in api.list_categories()]
    if len(names) == 2:
        folders = {c[6].lower() for c in api.list_categories()}
        assert len(folders) == 2, "two categories would write into the same folder on Windows"
        for i, name in enumerate(names):
            doc = sandbox / f"f{i}.txt"
            doc.write_bytes(corpus.lines(400 + i, "{i} {w}\n"))
            archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), name)
    app_is_healthy(gui_window, message_boxes)


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
        select_row_by_name(gui_window.categories_tab.table, "A")
        gui_window.categories_tab._edit_category()
        gui_window.categories_tab.refresh()
        no_crash_reports(message_boxes)
    assert sorted(c[1] for c in api.list_categories()) == ["A", "B"]


def test_deleting_a_category_then_saying_no_or_having_items(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Keep")
    doc = sandbox / "k.txt"
    doc.write_bytes(corpus.lines(300, "{w}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(doc), "Keep")
    gui_window.categories_tab.refresh()
    select_row_by_name(gui_window.categories_tab.table, "Keep")
    message_boxes.warning_confirm = False
    gui_window.categories_tab._delete_category()      # says no
    message_boxes.warning_confirm = True
    gui_window.categories_tab._delete_category()      # says yes, but it has items
    assert "cannot be deleted" in last_message(message_boxes)[2]
    assert [c[1] for c in api.list_categories()] == ["Keep"]
    app_is_healthy(gui_window, message_boxes)


@pytest.mark.parametrize("name,expected_kind,expected_text", CATEGORY_NAMES, ids=[f"name{i}" for i in range(len(CATEGORY_NAMES))])
def test_creating_categories_with_odd_names(gui_window, monkeypatch, message_boxes, name, expected_kind, expected_text):
    _type_into_category_form(monkeypatch, name)
    gui_window.categories_tab._create_category()
    kind, _title, text = last_message(message_boxes)
    assert kind == expected_kind and expected_text in text, message_boxes.calls
    if expected_kind == "information":
        assert name.strip() in [c[1] for c in api.list_categories()]
        folder = os.path.join(api.data_root(), "archives", api.list_categories()[0][6])
        assert os.path.isdir(folder), "the category folder must be a valid Windows folder"


def test_a_duplicate_category_name_is_refused(gui_window, monkeypatch, message_boxes):
    _type_into_category_form(monkeypatch, "Docs")
    gui_window.categories_tab._create_category()
    gui_window.categories_tab.refresh()
    gui_window.categories_tab._create_category()
    kind, _, text = last_message(message_boxes)[:3]
    assert kind == "warning" and "already exists" in text
    assert [c[1] for c in api.list_categories()] == ["Docs"]


def test_rapid_category_creation_clicks_do_not_create_duplicates(gui_window, monkeypatch):
    class FakeDialog:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return self.Accepted

        def values(self):
            return {"name": "RapidCat", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeDialog)

    for _ in range(5):
        gui_window.categories_tab._create_category()

    matches = [c for c in main.list_categories() if c[1] == "RapidCat"]
    assert len(matches) == 1


# --- what can go wrong around a category action -------------------------------------------------------

def _select(window, name):
    window.categories_tab.refresh()
    select_row_by_name(window.categories_tab.table, name)


@pytest.mark.parametrize("action", ["_create_category", "_edit_category", "_delete_category"])
def test_category_actions_do_nothing_while_the_window_is_busy(gui_window, monkeypatch, message_boxes, action):
    main.create_category("Docs")
    _select(gui_window, "Docs")
    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", None)  # opening a dialog now would fail the test
    gui_window.set_busy(True, "Archiving...")
    getattr(gui_window.categories_tab, action)()
    assert message_boxes.calls == [] and [c[1] for c in main.list_categories()] == ["Docs"]


@pytest.mark.parametrize("action", ["_create_category", "_edit_category"])
def test_cancelling_the_category_form_changes_nothing(gui_window, monkeypatch, message_boxes, action):
    main.create_category("Docs")
    _select(gui_window, "Docs")
    monkeypatch.setattr(CreateCategoryDialog, "exec", lambda dialog: QDialog.Rejected)
    getattr(gui_window.categories_tab, action)()
    assert message_boxes.calls == [] and [c[1] for c in main.list_categories()] == ["Docs"]


def _fail(*args, **kwargs):
    raise OSError("database is locked")


@pytest.mark.parametrize("outcome, kind, text", [
    (_fail, "critical", "Could not create category:\ndatabase is locked"),
    (lambda *a, **k: False, "warning", "Category could not be created."),
])
def test_a_category_the_core_cannot_create_is_explained(gui_window, monkeypatch, message_boxes, outcome, kind, text):
    monkeypatch.setattr(main, "create_category", outcome)
    _type_into_category_form(monkeypatch, "Docs")
    gui_window.categories_tab._create_category()
    assert last_message(message_boxes)[:3] == (kind, "New category", text)


@pytest.mark.parametrize("outcome, kind, text", [
    (_fail, "critical", "Could not update category:\ndatabase is locked"),
    (lambda *a, **k: False, "warning", "Category could not be updated."),
])
def test_a_category_the_core_cannot_update_is_explained(gui_window, monkeypatch, message_boxes, outcome, kind, text):
    main.create_category("Docs")
    _select(gui_window, "Docs")
    monkeypatch.setattr(main, "update_category", outcome)
    _type_into_category_form(monkeypatch, "Documents")
    gui_window.categories_tab._edit_category()
    assert last_message(message_boxes)[:3] == (kind, "Edit category", text)
    assert [c[1] for c in api.list_categories()] == ["Docs"]


def test_a_category_the_core_cannot_delete_is_explained(gui_window, monkeypatch, message_boxes):
    main.create_category("Docs")
    _select(gui_window, "Docs")
    monkeypatch.setattr(main, "delete_category", _fail)
    message_boxes.warning_confirm = True
    gui_window.categories_tab._delete_category()
    assert last_message(message_boxes)[:3] == ("critical", "Delete category", "Could not delete category:\ndatabase is locked")


@pytest.mark.parametrize("action", ["_edit_category", "_delete_category"])
def test_a_category_removed_after_it_was_selected_is_reported_gone(gui_window, monkeypatch, message_boxes, action):
    main.create_category("Docs")
    _select(gui_window, "Docs")
    main.delete_category(category_id_by_name("Docs"))  # gone before the click (e.g. a second window)
    message_boxes.warning_confirm = True
    getattr(gui_window.categories_tab, action)()
    assert last_message(message_boxes)[:2] == ("warning", "Edit category" if action == "_edit_category" else "Delete category")
    assert last_message(message_boxes)[2] == "Category no longer exists."
    assert gui_window.categories_tab.table.rowCount() == 0  # the list was refreshed


def test_categories_that_cannot_be_loaded_are_explained(gui_window, monkeypatch, message_boxes):
    monkeypatch.setattr(main, "list_categories", _fail)
    gui_window.categories_tab.refresh()
    assert last_message(message_boxes)[:3] == ("warning", "Categories", "Could not load categories:\ndatabase is locked")
