"""local_cloud.local_state - where the cloud client keeps its own small state (never in the archive database), and a
missing or damaged config always means "a fresh install"."""
import os

import pytest

from local_cloud import local_state


def test_without_the_test_override_state_lives_in_the_users_local_app_data(monkeypatch, tmp_path):
    monkeypatch.delenv("STORAGEARCH_CLOUD_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))  # a stand-in profile, never the real one
    assert local_state.base_dir() == str(tmp_path / "Local" / "StorageArch")
    assert os.path.isdir(tmp_path / "Local" / "StorageArch")


def test_without_local_app_data_the_home_folder_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("STORAGEARCH_CLOUD_HOME")
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path / "home"))
    assert local_state.base_dir() == str(tmp_path / "home" / "StorageArch")


def test_every_state_file_is_inside_the_cloud_folder():
    base = local_state.base_dir()
    for path_fn in (local_state.config_path, local_state.credentials_path, local_state.telemetry_queue_path,
                    local_state.feedback_queue_path, local_state.crash_queue_path, local_state.remote_config_lkg_path):
        assert os.path.dirname(path_fn()) == base


def test_one_session_id_per_run(monkeypatch):
    monkeypatch.setattr(local_state, "_session_id", None)
    first = local_state.session_id()
    assert local_state.session_id() == first and len(first) == 36


def test_a_saved_config_is_loaded_back_and_unknown_keys_are_dropped():
    local_state.save_config(dict(local_state.load_config(), telemetry_enabled=True, injected="x"))
    config = local_state.load_config()
    assert config["telemetry_enabled"] is True and "injected" not in config


@pytest.mark.parametrize("content", ["[1, 2, 3]", "{ damaged"], ids=["not_an_object", "damaged"])
def test_a_config_that_is_not_an_object_means_a_fresh_install(content):
    with open(local_state.config_path(), "w", encoding="utf-8") as f:
        f.write(content)
    assert local_state.load_config() == local_state._DEFAULT_CONFIG


def test_resetting_removes_every_state_file_and_the_session(monkeypatch):
    paths = [local_state.config_path(), local_state.credentials_path(), local_state.telemetry_queue_path(),
             local_state.feedback_queue_path(), local_state.crash_queue_path(), local_state.remote_config_lkg_path()]
    for path in paths:
        with open(path, "w") as f:
            f.write("x")
    local_state.session_id()
    local_state.reset_local_state()
    assert not any(os.path.exists(p) for p in paths) and local_state._session_id is None


def test_a_state_file_that_cannot_be_removed_does_not_stop_the_reset(monkeypatch):
    with open(local_state.config_path(), "w") as f:
        f.write("x")

    def refuse(path):
        raise PermissionError("in use")

    monkeypatch.setattr(local_state.os, "remove", refuse)
    local_state.reset_local_state()  # must not raise
    assert local_state._session_id is None
