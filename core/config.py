"""Where the data lives: one small JSON file (`storagearch_config.json`, in the folder the app starts in -
the same file and keys as before, so existing installations keep their data). Paths are made absolute
when loaded, so a later change of the working directory cannot move the data."""
import json
import os
import threading

CONFIG_FILE = "storagearch_config.json"
DEFAULT_DATA_ROOT = "SmartArchiveData"

_lock = threading.Lock()
_state = {"data_root": os.path.abspath(DEFAULT_DATA_ROOT), "workspace_root": None, "config_dir": os.getcwd()}


def load(config_dir=None):
    """Reads the config from `config_dir` (default: the current folder) and returns a copy of it."""
    config_dir = os.path.abspath(config_dir or os.getcwd())
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
        data = {"data_root": _state["data_root"], "workspace_root": _state["workspace_root"]}
        path = os.path.join(_state["config_dir"], CONFIG_FILE)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)
    return current()


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
