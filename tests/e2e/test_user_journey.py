"""Whole journeys through the real window, from archiving to restarting the app."""
import json
import os
import zipfile

from core import api, api as main
from support import corpus
from support.gui import (
    archive_file_through_gui,
    archive_folder_through_gui,
    last_message,
    select_row_by_name,
    wait_idle,
    workspace_of,
)


def _select_row_by_name_and_original_path(table, name, original_path_suffix):
    # for two items sharing the same filename, disambiguate via the
    # "Original location" column (index 7) - see spec 11.6 same-filename fix.
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name and table.item(row, 7).text().replace("\\", "/").endswith(
            original_path_suffix.replace("\\", "/")
        ):
            table.selectRow(row)
            return row
    raise AssertionError(f"row '{name}' with original path ending '{original_path_suffix}' not found")


def _rows(table):
    return [table.item(r, 0).text() for r in range(table.rowCount())]


def test_full_file_e2e_through_gui(gui_window, qtbot, monkeypatch, sandbox):
    from gui.main_window import MainWindow

    # create category -> add file -> archive -> verify -> search -> duplicate -> checkout -> edit -> checkin -> verify
    main.create_category("E2EDocs", max_archive_size=2 * 1024 ** 3)
    gui_window.categories_tab.refresh()

    source_path = os.path.join(str(sandbox), "e2e_file.txt")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write("end to end content")

    archive_file_through_gui(gui_window, qtbot, monkeypatch, source_path, "E2EDocs")

    items = main.search("e2e_file.txt")
    assert len(items) == 1
    zip_path = items[0][10]
    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    assert os.path.isfile(zip_path)
    assert os.path.isfile(manifest_path)

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "e2e_file.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    # duplicate attempt
    duplicate_path = os.path.join(str(sandbox), "e2e_file_copy.txt")
    with open(duplicate_path, "w", encoding="utf-8") as f:
        f.write("end to end content")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, duplicate_path, "E2EDocs")
    assert len(main.search("")) == 1  # duplicate correctly rejected, no second item

    # checkout -> modify -> checkin
    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "e2e_file.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    workspace_path = workspace_of("e2e_file.txt")
    with open(workspace_path, "a", encoding="utf-8") as f:
        f.write(" - edited")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "e2e_file.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    path_in_archive = main.search("e2e_file.txt")[0][3]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(path_in_archive).decode("utf-8") == "end to end content - edited"

    # close and "restart" - fresh MainWindow instance against the same on-disk data
    gui_window.close()
    main.initialize()
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2._closing_in_progress = True

    window2.items_tab.search_edit.setText("e2e_file")
    window2.items_tab.refresh()
    assert window2.items_tab.table.rowCount() == 1

    select_row_by_name(window2.items_tab.table, "e2e_file.txt")
    window2.items_tab._checkout()
    wait_idle(qtbot, window2)
    workspace_path2 = workspace_of("e2e_file.txt")
    with open(workspace_path2, "r", encoding="utf-8") as f:
        assert f.read() == "end to end content - edited"

    window2.items_tab.refresh()
    select_row_by_name(window2.items_tab.table, "e2e_file.txt")
    window2.items_tab._checkin()
    wait_idle(qtbot, window2)

    select_row_by_name(window2.items_tab.table, "e2e_file.txt")
    window2.items_tab._verify_selected()
    wait_idle(qtbot, window2)

    final_status = main.verify_archive_by_id(main.get_item(main.search("e2e_file.txt")[0][0])[1])
    assert final_status == "VERIFIED"
    window2.close()


def test_full_folder_e2e_through_gui(gui_window, qtbot, monkeypatch, sandbox):
    from gui.main_window import MainWindow

    main.create_category("E2EFolders")
    gui_window.categories_tab.refresh()

    folder_path = os.path.join(str(sandbox), "e2e_folder")
    os.makedirs(os.path.join(folder_path, "sub"), exist_ok=True)
    with open(os.path.join(folder_path, "root.txt"), "w", encoding="utf-8") as f:
        f.write("root content")
    with open(os.path.join(folder_path, "sub", "nested.txt"), "w", encoding="utf-8") as f:
        f.write("nested content")

    archive_folder_through_gui(gui_window, qtbot, monkeypatch, folder_path, "E2EFolders")

    items = main.search("e2e_folder")
    assert len(items) == 1

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "e2e_folder")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    nested_workspace_path = os.path.join(workspace_of("e2e_folder"), "sub", "nested.txt")
    assert os.path.isfile(nested_workspace_path)
    with open(nested_workspace_path, "a", encoding="utf-8") as f:
        f.write(" - modified nested")

    gui_window.items_tab.refresh()
    select_row_by_name(gui_window.items_tab.table, "e2e_folder")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    # restart
    gui_window.close()
    main.initialize()
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2._closing_in_progress = True

    window2.items_tab.search_edit.setText("e2e_folder")
    window2.items_tab.refresh()
    select_row_by_name(window2.items_tab.table, "e2e_folder")
    window2.items_tab._checkout()
    wait_idle(qtbot, window2)

    root_workspace_path = os.path.join(workspace_of("e2e_folder"), "root.txt")
    nested_workspace_path2 = os.path.join(workspace_of("e2e_folder"), "sub", "nested.txt")
    assert os.path.isfile(root_workspace_path)
    assert os.path.isfile(nested_workspace_path2)
    with open(nested_workspace_path2, "r", encoding="utf-8") as f:
        assert f.read() == "nested content - modified nested"

    window2.items_tab.refresh()
    select_row_by_name(window2.items_tab.table, "e2e_folder")
    window2.items_tab._checkin()
    wait_idle(qtbot, window2)
    window2.close()


def test_multiple_categories_are_isolated(gui_window, qtbot, monkeypatch, sandbox):
    main.create_category("CatA")
    main.create_category("CatB")

    file_a = os.path.join(str(sandbox), "a.txt")
    with open(file_a, "w", encoding="utf-8") as f:
        f.write("content A")
    file_b = os.path.join(str(sandbox), "b.txt")
    with open(file_b, "w", encoding="utf-8") as f:
        f.write("content B")

    archive_file_through_gui(gui_window, qtbot, monkeypatch, file_a, "CatA")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, file_b, "CatB")

    items_a = main.search("", category_name="CatA")
    items_b = main.search("", category_name="CatB")
    assert len(items_a) == 1 and items_a[0][1] == "a.txt"
    assert len(items_b) == 1 and items_b[0][1] == "b.txt"

    zip_a = items_a[0][10]
    zip_b = items_b[0][10]
    assert os.path.dirname(zip_a) != os.path.dirname(zip_b)


def test_archive_splitting_creates_multiple_independent_zips(gui_window, qtbot, monkeypatch, sandbox):
    small_max_size = 300  # bytes - forces a new zip almost every file
    main.create_category("SplitTest", max_archive_size=small_max_size)

    for index in range(4):
        file_path = os.path.join(str(sandbox), f"chunk_{index}.bin")
        with open(file_path, "wb") as f:
            f.write(os.urandom(250))
        archive_file_through_gui(gui_window, qtbot, monkeypatch, file_path, "SplitTest")

    archives = [a for a in main.list_archives() if a[1] == "SplitTest"]
    assert len(archives) >= 2  # size limit forced splitting into independent archives
    for archive in archives:
        assert archive[3].endswith(".zip")
        assert os.path.isfile(archive[3])

    items = main.search("", category_name="SplitTest")
    assert len(items) == 4


def test_same_filename_different_content_coexist_and_are_independently_managed(
    gui_window, qtbot, monkeypatch, sandbox
):
    # Spec 11.6 fix: two different files with the same filename, from different
    # original folders, in the same category, must both be stored, kept fully
    # distinct (DB/ZIP/Manifest), and independently checkout/checkin-able.
    from gui.main_window import MainWindow

    main.create_category("NameClash")
    path1 = os.path.join(str(sandbox), "dir1", "same.txt")
    os.makedirs(os.path.dirname(path1), exist_ok=True)
    with open(path1, "w", encoding="utf-8") as f:
        f.write("content one")

    path2 = os.path.join(str(sandbox), "dir2", "same.txt")
    os.makedirs(os.path.dirname(path2), exist_ok=True)
    with open(path2, "w", encoding="utf-8") as f:
        f.write("content two - different")

    archive_file_through_gui(gui_window, qtbot, monkeypatch, path1, "NameClash")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, path2, "NameClash")

    items = main.search("", category_name="NameClash")
    assert len(items) == 2  # both stored - no silent overwrite, no silent merge, no data loss
    item_a = next(i for i in items if i[4] == path1.replace("\\", "/"))
    item_b = next(i for i in items if i[4] == path2.replace("\\", "/"))
    assert item_a[0] != item_b[0]  # distinct DB item ids
    assert item_a[3] != item_b[3]  # distinct path_in_archive / ZIP member names

    zip_path = item_a[10]
    assert item_b[10] == zip_path  # same category/zip in this scenario
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(item_a[3]).decode("utf-8") == "content one"
        assert zf.read(item_b[3]).decode("utf-8") == "content two - different"

    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_paths = {entry["path_in_archive"] for entry in manifest["files"]}
    assert item_a[3] in manifest_paths and item_b[3] in manifest_paths

    # checkout each individually and confirm correct, distinct content
    gui_window.items_tab.refresh()
    _select_row_by_name_and_original_path(gui_window.items_tab.table, "same.txt", "dir1/same.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)
    workspace_a = workspace_of("same.txt", "dir1/same.txt")
    with open(workspace_a, "r", encoding="utf-8") as f:
        assert f.read() == "content one"

    # modify + checkin item A while B remains checked-in/untouched
    with open(workspace_a, "a", encoding="utf-8") as f:
        f.write(" - edited A")
    gui_window.items_tab.refresh()
    _select_row_by_name_and_original_path(gui_window.items_tab.table, "same.txt", "dir1/same.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(main.get_item(item_a[0])[5]).decode("utf-8") == "content one - edited A"  # a checkin stores a new version
        assert zf.read(item_b[3]).decode("utf-8") == "content two - different"  # untouched

    # checkout B independently, confirm it was never affected by A's edit
    gui_window.items_tab.refresh()
    _select_row_by_name_and_original_path(gui_window.items_tab.table, "same.txt", "dir2/same.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)
    workspace_b = workspace_of("same.txt", "dir2/same.txt")
    with open(workspace_b, "r", encoding="utf-8") as f:
        assert f.read() == "content two - different"
    gui_window.items_tab.refresh()
    _select_row_by_name_and_original_path(gui_window.items_tab.table, "same.txt", "dir2/same.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    # restart and repeat verification
    gui_window.close()
    main.initialize()
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2._closing_in_progress = True

    items_after_restart = main.search("", category_name="NameClash")
    assert len(items_after_restart) == 2
    for item in items_after_restart:
        status = main.verify_archive_by_id(main.get_item(item[0])[1])
        assert status == "VERIFIED"
    window2.close()


def test_checkin_recovery_after_interrupted_operation_is_detected_on_restart(gui_window, sandbox):
    from gui.main_window import MainWindow

    main.create_category("Recovery")
    file_path = os.path.join(str(sandbox), "recover.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("recoverable content")
    assert main.archive_file(file_path, "Recovery") is True

    items = main.search("recover.txt")
    zip_path = items[0][10]
    leftover_temp = zip_path + ".partial"  # the new core's unfinished-archive file
    with open(leftover_temp, "wb") as f:
        f.write(b"orphaned partial write")
    assert os.path.isfile(leftover_temp)

    # a brand new app startup must clean this up automatically (11.4) before the window even opens
    main.initialize()
    window2 = MainWindow()
    window2._closing_in_progress = True
    assert not os.path.isfile(leftover_temp)
    assert os.path.isfile(zip_path)  # last verified archive untouched
    window2.close()


def test_adding_files_and_folders_like_a_user(gui_window, qtbot, monkeypatch, message_boxes, sandbox):
    api.create_category("Docs")
    report = sandbox / "report.txt"
    report.write_bytes(corpus.lines(4000, "line {i} {w} {n}\n"))
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(report), "Docs")
    assert "archived and verified" in last_message(message_boxes)[2] and "Saved:" in last_message(message_boxes)[2]

    empty = sandbox / "empty.txt"
    empty.write_bytes(b"")
    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(empty), "Docs")
    assert "archived and verified" in last_message(message_boxes)[2]

    project = sandbox / "project"
    (project / "src").mkdir(parents=True)
    (project / "empty_folder").mkdir()
    (project / "src" / "main.py").write_bytes(corpus.lines(800, "def f{i}(): return {n}\n"))
    archive_folder_through_gui(gui_window, qtbot, monkeypatch, str(project), "Docs")
    assert "archived and verified" in last_message(message_boxes)[2]

    archive_file_through_gui(gui_window, qtbot, monkeypatch, str(report), "Docs")  # the same file again
    assert any("already" in c[2].lower() or "duplicate" in c[2].lower() for c in message_boxes.calls[-2:]), message_boxes.calls[-2:]

    gui_window.items_tab.refresh()
    assert sorted(_rows(gui_window.items_tab.table)) == ["empty.txt", "project", "report.txt"]
    assert set(api.verify_all()["Docs"].values()) == {"VERIFIED"}
