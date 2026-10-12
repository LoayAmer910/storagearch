from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton, QTabWidget

from core import api as main
from gui import gc_guard
from gui import settings as settings_module
from gui.categories_tab import CategoriesTab
from gui.dashboard_tab import DashboardTab
from gui.dialogs import PendingCheckoutsDialog
from gui.items_tab import ItemsTab
from gui.settings_tab import SettingsTab
from gui.workers import run_in_background


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        gc_guard.install(QApplication.instance())  # garbage (Qt objects too) is freed only on this thread
        self.setWindowTitle("StorageArch")
        self.resize(1000, 640)

        self.settings = settings_module.load_settings()
        self._busy = False
        self._closing_in_progress = False
        self._returning_items = False

        self.dashboard_tab = DashboardTab(self)
        self.categories_tab = CategoriesTab(self)
        self.items_tab = ItemsTab(self)
        self.settings_tab = SettingsTab(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.categories_tab, "Categories")
        self.tabs.addTab(self.items_tab, "Items")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage("Ready")
        # While a long operation runs: how far it is, and a way to stop it.
        self._busy_message = ""
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_operation)
        self.cancel_button.hide()
        self.statusBar().addPermanentWidget(self.cancel_button)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(200)
        self._progress_timer.timeout.connect(self._show_progress)

        self.refresh_all()

    # ------------------------------------------------------------- helpers

    def save_settings(self):
        settings_module.save_settings(self.settings)

    def refresh_all(self):
        self.dashboard_tab.refresh()
        self.categories_tab.refresh()
        self.items_tab.refresh()

    def open_items_for_category(self, category_name):
        self.tabs.setCurrentWidget(self.items_tab)
        self.items_tab.filter_by_category(category_name)

    def is_busy(self):
        return self._busy

    def set_busy(self, busy, message=None, cancellable=False):
        """`cancellable`: the operation is one the core can stop (archive, check out, check in) - the Cancel
        button and the progress count are shown for it."""
        self._busy = busy
        self.tabs.setEnabled(not busy)
        self._busy_message = (message or "") if busy else ""
        self.statusBar().showMessage(message if busy else "Ready")
        self.cancel_button.setVisible(bool(busy and cancellable))
        self.cancel_button.setEnabled(True)
        if busy and cancellable:
            self._progress_timer.start()
        else:
            self._progress_timer.stop()

    def _show_progress(self):
        try:
            progress = main.operation_progress()
        except Exception:  # noqa: BLE001 - the count is a nicety; it must never disturb the operation
            return
        if not self._busy or not progress or progress["total"] < 2:
            return
        self.statusBar().showMessage(
            f"{self._busy_message}  {progress['phase']} {progress['done']} of {progress['total']}")

    def _cancel_operation(self):
        try:
            accepted = main.cancel_current_operation()
        except Exception:  # noqa: BLE001
            accepted = False
        if accepted:
            self.cancel_button.setEnabled(False)
            self._busy_message = "Cancelling..."
            self._progress_timer.stop()
            self.statusBar().showMessage("Cancelling...")

    # --------------------------------------------------------------- close

    def _return_items(self, item_ids):
        """Checks the items in on a background thread while this thread keeps the window alive (a large item
        takes a while, and a window that stops answering looks crashed). Returns the ids that were not returned."""
        outcome = {}
        waiting = QEventLoop(self)

        def work():
            failures = []
            for item_id in item_ids:
                try:
                    if not main.checkin(item_id):
                        failures.append(item_id)
                except Exception:  # noqa: BLE001
                    failures.append(item_id)
            return failures

        def done(failures):
            outcome["failures"] = failures
            waiting.quit()

        self._returning_items = True
        self.set_busy(True, f"Returning {len(item_ids)} item(s) before closing...")
        try:
            self._return_job = run_in_background(self, work, done, lambda _message: done(list(item_ids)))
            waiting.exec()
        finally:
            self.set_busy(False)
            self._returning_items = False
        return outcome.get("failures", list(item_ids))

    def closeEvent(self, event):
        if self._closing_in_progress:
            event.accept()
            return

        if self._returning_items:  # a second click on the X while the items are being returned
            event.ignore()
            return

        if self.is_busy():
            QMessageBox.information(
                self, "StorageArch", "An operation is still running. Please wait for it to finish before closing."
            )
            event.ignore()
            return

        try:
            pending = main.items_pending_return()
        except Exception:  # noqa: BLE001 - never block shutdown on a read failure
            pending = []

        if not pending:
            event.accept()
            return

        dialog = PendingCheckoutsDialog(self, pending_items=pending)
        if dialog.exec() != PendingCheckoutsDialog.Accepted:
            event.ignore()
            return

        action = dialog.result_action()
        if action == PendingCheckoutsDialog.LEAVE_CHECKED_OUT:
            event.accept()
            return

        if action == PendingCheckoutsDialog.RETURN_SELECTED:
            failures = self._return_items(dialog.selected_item_ids())
            if failures:
                QMessageBox.warning(
                    self,
                    "StorageArch",
                    f"{len(failures)} item(s) could not be returned and remain checked out.",
                )
            self._closing_in_progress = True
            event.accept()
            return

        event.ignore()
