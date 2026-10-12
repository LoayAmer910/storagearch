"""StorageArch - starts the desktop app (the GUI on the core)."""
import logging
import multiprocessing
import os
import sys
import traceback


def _install_crash_hook(api):
    default_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        try:
            frames = traceback.extract_tb(exc_tb)
            last = frames[-1] if frames else None
            module = os.path.basename(last.filename) if last and last.filename else None
            api.cloud_report_crash(exc_value, component=os.path.splitext(module)[0] if module else None,
                                   function_name=last.name if last else None,
                                   safe_code_location=f"{module}:{last.lineno}" if module else None)
        except Exception:  # noqa: BLE001 - the crash hook must never crash itself
            pass
        try:  # kept on this computer, in the log file of the data folder
            logging.getLogger("storagearch").critical("unhandled error", exc_info=(exc_type, exc_value, exc_tb))
        except Exception:  # noqa: BLE001
            pass
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def start():
    multiprocessing.freeze_support()
    from core import api

    _install_crash_hook(api)
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv)
    try:
        api.initialize()
    except api.AlreadyRunningError:
        QMessageBox.information(None, "StorageArch", "StorageArch is already open and working on this data folder.\n"
                                                     "Use the window that is already open.")
        return 1

    from gui.main_window import MainWindow

    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        api.shutdown_cloud()


if __name__ == "__main__":
    sys.exit(start())
