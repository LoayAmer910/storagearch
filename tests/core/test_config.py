"""core.config - the configuration file: defaults, relative values, bad files."""
import os

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
