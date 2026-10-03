"""gui.settings - the window's own settings file: defaults, saving, and a damaged or foreign file never breaks startup."""
import json
import os

from core import config
from gui import settings

DEFAULT_SIZE = 2 * 1024 ** 3


def _settings_file():
    return os.path.join(config.data_root(), "gui_settings.json")


def test_without_a_settings_file_the_defaults_are_used(app):
    assert not os.path.exists(_settings_file())
    assert settings.load_settings() == {"default_max_archive_size": DEFAULT_SIZE}


def test_saved_settings_are_loaded_back(app):
    settings.save_settings({"default_max_archive_size": 5 * 1024 ** 3})
    assert settings.load_settings() == {"default_max_archive_size": 5 * 1024 ** 3}
    assert not os.path.exists(_settings_file() + ".tmp"), "the temporary file must be replaced, not left behind"


def test_unknown_keys_are_ignored_and_missing_keys_get_their_default(app):
    with open(_settings_file(), "w", encoding="utf-8") as f:
        json.dump({"from_a_newer_version": True}, f)
    assert settings.load_settings() == {"default_max_archive_size": DEFAULT_SIZE}


def test_a_damaged_settings_file_falls_back_to_the_defaults(app):
    with open(_settings_file(), "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert settings.load_settings() == {"default_max_archive_size": DEFAULT_SIZE}


def test_saving_creates_the_data_folder_when_it_is_missing(app, tmp_path, monkeypatch):
    new_root = tmp_path / "moved" / "data"
    monkeypatch.setattr(config, "data_root", lambda: str(new_root))
    settings.save_settings({"default_max_archive_size": DEFAULT_SIZE})
    assert (new_root / "gui_settings.json").is_file()
