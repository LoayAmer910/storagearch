"""StorageArch - starts the desktop app (the GUI on the core)."""
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
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def start():
    multiprocessing.freeze_support()
    from core import api

    _install_crash_hook(api)
    api.initialize()

    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        api.shutdown_cloud()


if __name__ == "__main__":
    sys.exit(start())
