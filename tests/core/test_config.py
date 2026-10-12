"""core.config - the configuration file: defaults, relative values, bad files."""
import os
import sys

from core import config


def test_config_defaults_relative_values_and_bad_files(tmp_path):
    assert config.load(str(tmp_path))["data_root"] == str(tmp_path / "SmartArchiveData")
    (tmp_path / config.CONFIG_FILE).write_text('{"data_root": "MyData", "workspace_root": "   "}', encoding="utf-8")
    state = config.load(str(tmp_path))
    assert state == {"data_root": str(tmp_path / "MyData"), "workspace_root": None}
    for garbage in ("{not json", "[1, 2]", '{"data_root": 5}', "\xff\xfe"):
        (tmp_path / config.CONFIG_FILE).write_bytes(garbage.encode("latin-1"))
        assert config.load(str(tmp_path))["data_root"] == str(tmp_path / "SmartArchiveData"), garbage


def test_config_save_round_trips_and_survives_a_change_of_working_directory(tmp_path, monkeypatch):
    config.load(str(tmp_path))
    config.save(data_root=str(tmp_path / "D"), workspace_root=str(tmp_path / "W"))
    assert not os.path.exists(str(tmp_path / config.CONFIG_FILE) + ".tmp")
    monkeypatch.chdir(tmp_path.parent)
    assert config.data_root() == str(tmp_path / "D") and config.workspace_dir() == str(tmp_path / "W")
    config.save(workspace_root="")
    assert config.workspace_dir() == str(tmp_path / "D" / "workspace")
    assert config.load(str(tmp_path)) == {"data_root": str(tmp_path / "D"), "workspace_root": None}


# --- where the app's folder is ---------------------------------------------------------------------------

def _packaged(monkeypatch, exe_folder):
    """The process looks like the packaged app whose exe file is in `exe_folder`."""
    os.makedirs(exe_folder, exist_ok=True)
    monkeypatch.delenv(config.HOME_VARIABLE, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", os.path.join(str(exe_folder), "StorageArch.exe"))


def test_run_from_source_the_data_is_in_the_current_folder_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv(config.HOME_VARIABLE, raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.app_dir() == str(tmp_path)
    assert config.load()["data_root"] == str(tmp_path / "SmartArchiveData")


def test_the_packaged_app_keeps_its_data_next_to_its_exe_wherever_it_is_started_from(tmp_path, monkeypatch):
    exe_folder, started_from = tmp_path / "StorageArch", tmp_path / "desktop"
    started_from.mkdir()
    _packaged(monkeypatch, exe_folder)
    monkeypatch.chdir(started_from)  # a shortcut with another "start in", or a command line somewhere else
    assert config.app_dir() == str(exe_folder)
    assert config.load()["data_root"] == str(exe_folder / "SmartArchiveData")
    config.save(workspace_root=str(exe_folder / "work"))
    assert (exe_folder / config.CONFIG_FILE).is_file() and os.listdir(started_from) == []


def test_a_packaged_app_in_a_folder_it_cannot_write_to_uses_the_users_own_folder(tmp_path, monkeypatch):
    exe_folder, user_data = tmp_path / "Program Files" / "StorageArch", tmp_path / "LocalAppData"
    _packaged(monkeypatch, exe_folder)
    monkeypatch.setenv("LOCALAPPDATA", str(user_data))
    monkeypatch.setattr(config, "_writable", lambda folder: False)
    assert config.app_dir() == str(user_data / "StorageArch")
    assert config.load()["data_root"] == str(user_data / "StorageArch" / "SmartArchiveData")
    assert (user_data / "StorageArch").is_dir() and os.listdir(exe_folder) == []


def test_a_folder_is_writable_only_if_a_file_can_really_be_made_in_it(tmp_path):
    assert config._writable(str(tmp_path)) is True and os.listdir(tmp_path) == []
    assert config._writable(str(tmp_path / "not there")) is False


def test_the_home_variable_names_the_folder_outright(tmp_path, monkeypatch):
    _packaged(monkeypatch, tmp_path / "exe")
    monkeypatch.setenv(config.HOME_VARIABLE, str(tmp_path / "named"))
    assert config.app_dir() == str(tmp_path / "named")
    assert config.load()["data_root"] == str(tmp_path / "named" / "SmartArchiveData")
