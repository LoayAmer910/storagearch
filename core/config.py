"""Where the data lives: one small JSON file (`storagearch_config.json`, the same file and keys as before, so
existing installations keep their data) in the app's folder - see app_dir(). Paths are made absolute when
loaded, so a later change of the working directory cannot move the data; a location inside that folder is
written relative to it, so the folder can be moved as a whole."""
import json
import os
import sys
import tempfile
import threading

CONFIG_FILE = "storagearch_config.json"
DEFAULT_DATA_ROOT = "SmartArchiveData"

_lock = threading.Lock()
_state = {"data_root": os.path.abspath(DEFAULT_DATA_ROOT), "workspace_root": None, "config_dir": os.getcwd()}


HOME_VARIABLE = "STORAGEARCH_HOME"
USER_FOLDER = "StorageArch"


def app_dir():
    """The folder of the config file and, by default, of the data.

    The packaged app: the folder of its exe file - not the folder it happened to be started from (a shortcut,
    a command line), which made the data seem to vanish. When that folder cannot be written to (Program
    Files), the user's own StorageArch folder. Run from source: the current folder, as before.
    The STORAGEARCH_HOME variable names the folder outright (tests of the packaged build use it)."""
    named = os.environ.get(HOME_VARIABLE, "").strip()
    if named:
        return os.path.abspath(named)
    if not getattr(sys, "frozen", False):
        return os.getcwd()
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if _writable(exe_dir):
        return exe_dir
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), USER_FOLDER)


def _writable(folder):
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def load(config_dir=None):
    """Reads the config from `config_dir` (default: app_dir()) and returns a copy of it."""
    config_dir = os.path.abspath(config_dir or app_dir())
    os.makedirs(config_dir, exist_ok=True)
    state = {"data_root": os.path.join(config_dir, DEFAULT_DATA_ROOT), "workspace_root": None, "config_dir": config_dir}
    path = os.path.join(config_dir, CONFIG_FILE)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if isinstance(data.get("data_root"), str) and data["data_root"].strip():
                    state["data_root"] = os.path.join(config_dir, data["data_root"])
                if isinstance(data.get("workspace_root"), str) and data["workspace_root"].strip():
                    state["workspace_root"] = os.path.join(config_dir, data["workspace_root"])
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    with _lock:
        _state.clear()
        _state.update(state)
    return current()


def save(data_root=None, workspace_root=None):
    """Changes and writes the config atomically. workspace_root="" resets the workspace to the default."""
    with _lock:
        if data_root is not None:
            _state["data_root"] = os.path.abspath(data_root)
        if workspace_root is not None:
            _state["workspace_root"] = os.path.abspath(workspace_root) if workspace_root else None
        data = {"data_root": _portable(_state["data_root"], _state["config_dir"]),
                "workspace_root": _portable(_state["workspace_root"], _state["config_dir"])}
        path = os.path.join(_state["config_dir"], CONFIG_FILE)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
    return current()


def _portable(path, config_dir):
    """A location inside the folder of the config file is written relative to it, so that folder can be
    moved or renamed as a whole; any other location is written in full."""
    if path is None:
        return None
    try:
        relative = os.path.relpath(path, config_dir)
    except ValueError:  # another drive
        return path
    return path if relative == os.curdir or relative.startswith(os.pardir) or os.path.isabs(relative) else relative


def current():
    with _lock:
        return {"data_root": _state["data_root"], "workspace_root": _state["workspace_root"]}


def data_root():
    with _lock:
        return _state["data_root"]


def archives_dir():
    return os.path.join(data_root(), "archives")


def workspace_dir():
    with _lock:
        return _state["workspace_root"] or os.path.join(_state["data_root"], "workspace")


def temp_dir():
    return os.path.join(data_root(), "temp")


def log_dir():
    return os.path.join(data_root(), "logs")


def db_path():
    return os.path.join(data_root(), "smartarchive.db")
