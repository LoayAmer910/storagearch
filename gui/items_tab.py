import os

from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from core import api as archive_manager
from core import api as main
from gui.dialogs import SourceActionDialog, show_duplicate_info, show_untrusted_duplicate_warning
from gui.format_utils import format_bytes
from gui.workers import run_in_background

COLUMNS = ("Name", "Type", "Category", "Archive", "Original size", "Compressed size", "Status", "Original location")
ITEM_ID_ROLE = Qt.UserRole


def _in_use_message(path):
    return (f"'{os.path.basename(path)}' is open in another program, or you do not have permission to read it.\n"
            f"Close it and try again.\n\n{path}")


_CANCELLED = "Cancelled. Nothing was saved, and the source was not touched."
_CHECKOUT_FAILURES = {
    "INSUFFICIENT_SPACE": "Checkout failed: there is not enough free space for this item.",
    "PATH_TOO_LONG": "Checkout failed: the item's names are too long for the workspace folder (Windows allows 260 "
                     "characters for a whole path).\nChoose a workspace location with a shorter path in Settings and try again.",
    "TARGET_EXISTS": "Checkout failed: a file or folder with this name is already in the workspace.",
}


class ItemsTab(QWidget):
    """The Items tab: search, archive, check out, check in and verify items."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self._thread = None
        self._worker = None
        self._pending_archive = None  # (path, category, is_folder) awaiting source-action choice

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name...")
        self.search_edit.returnPressed.connect(self.refresh)
        self.category_filter = QComboBox()
        self.category_filter.addItem("All categories", None)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.refresh)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        search_row.addWidget(self.search_edit)
        search_row.addWidget(QLabel("Category:"))
        search_row.addWidget(self.category_filter)
        search_row.addWidget(self.search_button)

        self.destination_category = QComboBox()
        self.add_file_button = QPushButton("Add file...")
        self.add_file_button.clicked.connect(self._add_file)
        self.add_folder_button = QPushButton("Add folder...")
        self.add_folder_button.clicked.connect(self._add_folder)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Destination category:"))
        add_row.addWidget(self.destination_category)
        add_row.addWidget(self.add_file_button)
        add_row.addWidget(self.add_folder_button)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._update_action_buttons)

        self.checkout_button = QPushButton("Checkout")
        self.checkout_button.clicked.connect(self._checkout)
        self.checkin_button = QPushButton("Checkin")
        self.checkin_button.clicked.connect(self._checkin)
        self.verify_button = QPushButton("Verify archive")
        self.verify_button.clicked.connect(self._verify_selected)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        action_row = QHBoxLayout()
        action_row.addWidget(self.checkout_button)
        action_row.addWidget(self.checkin_button)
        action_row.addWidget(self.verify_button)
        action_row.addWidget(self.refresh_button)
        action_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(search_row)
        layout.addLayout(add_row)
        layout.addWidget(self.table)
        layout.addLayout(action_row)

        self._update_action_buttons()

    # ---------------------------------------------------------- data refresh

    def refresh(self):
        self.refresh_categories()
        try:
            category = self.category_filter.currentData()
            results = main.search(self.search_edit.text(), category_name=category)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Search", f"Search failed:\n{exc}")
            return

        self.table.setRowCount(len(results))
        for row_index, row in enumerate(results):
            item_id, file_name, file_type, _path_in_archive, original_path, original_size, compressed_size, status, category_name, archive_name, _archive_path = row
            # "Original location" lets the user tell apart two items that share a filename
            # but came from different source folders (spec 11.6 same-filename-different-content).
            values = [file_name, file_type, category_name, archive_name, format_bytes(original_size), format_bytes(compressed_size), status, original_path]
            for col_index, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if col_index == 0:
                    cell.setData(ITEM_ID_ROLE, item_id)
                self.table.setItem(row_index, col_index, cell)
        self._update_action_buttons()

    def refresh_categories(self):
        try:
            categories = main.list_categories()
        except Exception:  # noqa: BLE001
            categories = []

        current_filter = self.category_filter.currentData()
        current_dest = self.destination_category.currentText()

        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", None)
        for category in categories:
            self.category_filter.addItem(category.name, category.name)
        restored_index = self.category_filter.findData(current_filter)
        self.category_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self.category_filter.blockSignals(False)

        self.destination_category.blockSignals(True)
        self.destination_category.clear()
        for category in categories:
            self.destination_category.addItem(category.name)
        restored_dest = self.destination_category.findText(current_dest)
        if restored_dest >= 0:
            self.destination_category.setCurrentIndex(restored_dest)
        self.destination_category.blockSignals(False)

    def filter_by_category(self, category_name):
        self.refresh_categories()
        index = self.category_filter.findData(category_name)
        if index >= 0:
            self.category_filter.setCurrentIndex(index)
        self.refresh()

    # -------------------------------------------------------- row selection

    def _selected_item_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(ITEM_ID_ROLE)

    def _selected_status(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 6).text()

    def _update_action_buttons(self):
        status = self._selected_status()
        has_selection = status is not None
        self.checkout_button.setEnabled(has_selection and status == "ARCHIVED")
        self.checkin_button.setEnabled(has_selection and status == "CHECKED_OUT")
        self.verify_button.setEnabled(has_selection)

    # -------------------------------------------------------------- add item

    def _add_file(self):
        if self.main_window.is_busy():
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Select file to archive")
        if not path:
            return
        self._start_archive_flow(path, is_folder=False)

    def _add_folder(self):
        if self.main_window.is_busy():
            return
        path = QFileDialog.getExistingDirectory(self, "Select folder to archive")
        if not path:
            return
        self._start_archive_flow(path, is_folder=True)

    def _start_archive_flow(self, path, is_folder):
        category = self.destination_category.currentText()
        if not category:
            QMessageBox.warning(self, "Add item", "Create a category first.")
            return
        if is_folder and not os.path.isdir(path):
            QMessageBox.warning(self, "Add item", "The selected folder no longer exists.")
            return
        if not is_folder and not os.path.isfile(path):
            QMessageBox.warning(self, "Add item", "The selected file no longer exists.")
            return

        # Pre-flight validation: catches paths Windows would refuse to open
        # (trailing space/dot, reserved device names, path too long, ...)
        # before any background work starts, instead of surfacing a raw
        # OSError deep in the duplicate-check/archive phase.
        is_valid, _reason = archive_manager.validate_source_path(path)
        if not is_valid:
            QMessageBox.warning(self, "Add item", "The name is not valid, or the path does not exist.")
            return

        self.main_window.set_busy(True, "Checking for duplicate content...", cancellable=True)
        self._pending_archive = (path, category, is_folder)

        def compute_and_check():
            try:
                content_hash = (
                    archive_manager.calculate_folder_hash(path) if is_folder else archive_manager.calculate_file_hash(path)
                )
            except archive_manager.CloudPlaceholderError as exc:
                return {
                    "error": "A file is in OneDrive and is not downloaded to this PC.\n"
                             "Download it (right-click it in OneDrive → \"Always keep on this device\") and try again.\n\n"
                             f"{exc}"
                }
            except archive_manager.SourceInUseError as exc:
                return {"error": _in_use_message(exc.path)}
            except archive_manager.InvalidPathError as exc:
                return {"error": f"Cannot read the source:\n{exc}"}
            except OSError as exc:
                return {"error": f"Unable to read source content: {exc}"}
            except archive_manager.OperationCancelled:
                return {"cancelled": True}
            if content_hash is None:
                return {"error": "Unable to read source content."}
            return {"hash": content_hash, "duplicate": main.find_duplicate(content_hash)}

        self._thread, self._worker = run_in_background(
            self, compute_and_check, self._on_duplicate_check_done, self._on_archive_failed
        )

    def _on_duplicate_check_done(self, result):
        # Busy stays True across this hand-off into _run_archive - there must be no
        # window where the GUI looks idle while the duplicate-check thread has only
        # just finished and the archive write hasn't started yet (that gap previously
        # allowed a second click to race a concurrent backend operation).
        if result.get("error"):
            self.main_window.set_busy(False)
            QMessageBox.warning(self, "Add item", result["error"])
            self._pending_archive = None
            return
        if result.get("cancelled"):
            self.main_window.set_busy(False)
            QMessageBox.information(self, "Add item", _CANCELLED)
            self._pending_archive = None
            return

        duplicate = result.get("duplicate")
        if duplicate is not None:
            self.main_window.set_busy(False)
            if duplicate["status"] == "VERIFIED":
                show_duplicate_info(self, duplicate)
            else:
                show_untrusted_duplicate_warning(self, duplicate)
            self._pending_archive = None
            return

        path, _category, is_folder = self._pending_archive
        dialog = SourceActionDialog(self, source_label=path)
        if dialog.exec() != SourceActionDialog.Accepted:
            self.main_window.set_busy(False)
            self._pending_archive = None
            return

        source_action = dialog.chosen_action()
        self._run_archive(source_action)

    def _run_archive(self, source_action):
        path, category, is_folder = self._pending_archive
        # still busy from the duplicate-check phase; message update only
        self.main_window.set_busy(True, "Archiving...", cancellable=True)
        # *_result gives a structured {"success", "reason", "message"} so the user sees a
        # specific reason (space/permission/protected path/...) instead of a generic failure.
        func = main.archive_folder_result if is_folder else main.archive_file_result
        self._thread, self._worker = run_in_background(
            self, func, self._on_archive_done, self._on_archive_failed, args=(path, category, source_action)
        )

    def _on_archive_done(self, result):
        self.main_window.set_busy(False)
        path, category, _is_folder = self._pending_archive
        self._pending_archive = None

        if not result["success"]:
            if result["reason"] == "CANCELLED":
                QMessageBox.information(self, "Add item", _CANCELLED)
                return
            message = _in_use_message(result["path"]) if result["reason"] == "SOURCE_IN_USE" else result["message"]
            QMessageBox.warning(self, "Add item", message)
            return

        if result["reason"] == "DUPLICATE":
            show_duplicate_info(self, result["duplicate"])
            self.main_window.refresh_all()
            return

        base_name = os.path.basename(os.path.normpath(path))
        skipped_cloud_files = result.get("skipped_cloud_files") or []
        skipped_line = ""
        if skipped_cloud_files:
            skipped_names = "\n".join(f"  - {os.path.basename(p)}" for p in skipped_cloud_files)
            skipped_line = (
                f"\n\n{len(skipped_cloud_files)} file(s) were skipped because they are in OneDrive and not downloaded to this PC:\n"
                f"{skipped_names}\n"
                "To include them, download them (right-click -> \"Always keep on this device\") and add them again."
            )
        if "original_size" in result:
            smart_line = ""
            if result.get("smart_compression_used"):
                smart_line = f"Smart Compression: {result['strategy']}\n"
            QMessageBox.information(
                self,
                "Add item",
                f"'{base_name}' archived and verified successfully.\n"
                f"Original size: {format_bytes(result['original_size'])}\n"
                f"Compressed size: {format_bytes(result['compressed_size'])}\n"
                f"Saved: {format_bytes(result['savings'])}\n"
                f"{smart_line}"
                f"{skipped_line}",
            )
        else:
            QMessageBox.information(self, "Add item", f"'{base_name}' archived and verified successfully.{skipped_line}")

        self.main_window.refresh_all()

    def _on_archive_failed(self, message):
        self.main_window.set_busy(False)
        self._pending_archive = None
        QMessageBox.critical(self, "Add item", f"Operation failed:\n{message}")

    # --------------------------------------------------------------- actions

    def _checkout(self):
        if self.main_window.is_busy():
            return
        item_id = self._selected_item_id()
        if item_id is None:
            QMessageBox.information(self, "Checkout", "Select an item first.")
            return
        if self._selected_status() != "ARCHIVED":
            QMessageBox.information(self, "Checkout", "Only archived items can be checked out.")
            return

        self.main_window.set_busy(True, "Checking out...", cancellable=True)
        self._thread, self._worker = run_in_background(
            self, main.checkout_result, self._on_checkout_done, self._on_action_failed, args=(item_id,)
        )

    def _on_checkout_done(self, result):
        self.main_window.set_busy(False)
        if not result["success"] and result["reason"] == "CANCELLED":
            QMessageBox.information(self, "Checkout", "Cancelled. The item is still in its archive.")
            return
        if not result["success"]:
            QMessageBox.warning(self, "Checkout", _CHECKOUT_FAILURES.get(
                result["reason"], "Checkout failed. Check available disk space and try again."))
            return
        QMessageBox.information(self, "Checkout", f"Item checked out to:\n{result['path']}")
        self.main_window.refresh_all()

    def _checkin(self):
        if self.main_window.is_busy():
            return
        item_id = self._selected_item_id()
        if item_id is None:
            QMessageBox.information(self, "Checkin", "Select an item first.")
            return
        if self._selected_status() != "CHECKED_OUT":
            QMessageBox.information(self, "Checkin", "Only checked-out items can be checked in.")
            return

        self.main_window.set_busy(True, "Checking in...", cancellable=True)
        self._thread, self._worker = run_in_background(
            self, main.checkin_result, self._on_checkin_done, self._on_action_failed, args=(item_id,)
        )

    def _on_checkin_done(self, result):
        self.main_window.set_busy(False)
        if not result["success"] and result["reason"] == "CANCELLED":
            QMessageBox.information(self, "Checkin", "Cancelled. The checked-out copy and the archived version "
                                                     "are exactly as they were.")
            return
        if not result["success"]:
            QMessageBox.warning(
                self,
                "Checkin",
                "Checkin failed - the checked-out copy was left exactly as it is.\n"
                "If it is open in another program, close it and try again.",
            )
            self.main_window.refresh_all()
            return
        if result["reason"] == "WORKSPACE_COPY_MISSING":
            QMessageBox.warning(
                self,
                "Checkin",
                "The checked-out copy was not found - it was probably deleted.\n"
                "The item was returned to the version saved in the archive. "
                "Changes made to the missing copy were not saved.",
            )
        else:
            QMessageBox.information(self, "Checkin", "Item checked in and verified successfully.")
        self.main_window.refresh_all()

    def _verify_selected(self):
        if self.main_window.is_busy():
            return
        item_id = self._selected_item_id()
        if item_id is None:
            QMessageBox.information(self, "Verify", "Select an item first.")
            return

        item = main.get_item(item_id)
        if item is None:
            QMessageBox.warning(self, "Verify", "Item no longer exists.")
            self.main_window.refresh_all()
            return
        archive_id = item.archive_id

        def verify():
            status = main.verify_archive_by_id(archive_id)
            return status, archive_id, (main.unrecorded_entries(archive_id) if status == "CHANGED" else [])

        self.main_window.set_busy(True, "Verifying archive...")
        self._thread, self._worker = run_in_background(self, verify, self._on_verify_done, self._on_action_failed)

    def _on_verify_done(self, result):
        status, archive_id, unrecorded = result
        self.main_window.set_busy(False)
        icon = QMessageBox.information if status == "VERIFIED" else QMessageBox.warning
        icon(self, "Verify", f"Archive integrity status: {status}")
        self.main_window.refresh_all()
        if unrecorded:
            self._offer_repair(archive_id, unrecorded)

    def _offer_repair(self, archive_id, unrecorded):
        """Verifying never removes anything. Entries the catalog does not know are removed only if the user says so."""
        answer = QMessageBox.question(
            self, "Verify",
            f"This archive holds {len(unrecorded)} entries that the catalog does not know.\n\n"
            "They are either left over from an interrupted save, or they are items that were archived after "
            "the catalog was restored from an older copy (a backup, or a sync tool).\n\n"
            "Remove them from the archive? They will be deleted permanently.\n"
            "If the catalog may have been restored or rolled back, choose No.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.main_window.set_busy(True, "Repairing archive...")
        self._thread, self._worker = run_in_background(
            self, main.repair_archive, self._on_repair_done, self._on_action_failed, args=(archive_id,))

    def _on_repair_done(self, result):
        self.main_window.set_busy(False)
        if result["success"]:
            QMessageBox.information(self, "Verify", f"{result['removed']} entries were removed. "
                                                    f"Archive integrity status: {result['status']}")
        else:
            QMessageBox.warning(self, "Verify", f"The archive could not be repaired. Archive integrity status: {result['status']}")
        self.main_window.refresh_all()

    def _on_action_failed(self, message):
        self.main_window.set_busy(False)
        QMessageBox.critical(self, "Operation failed", message)
