"""local_cloud.remote_config_service - remote config is used only when it is signed, has exactly the approved keys and
is not older than what the app already has; anything else leaves the last known good copy untouched."""
import json
import os

import pytest

from local_cloud import local_state, metadata_signing, remote_config_service as rc

PATH = "/api/v1/config"


def _config(version=5, telemetry=True, disabled=("zstd",)):
    return {"config_version": version, "telemetry_globally_enabled": telemetry, "disabled_smart_strategies": list(disabled)}


def _signed(sign, config):
    return {"config": config, "signature": sign(metadata_signing.canonical_config_string(config))}


def test_a_signed_config_is_saved_as_the_last_known_good(answers, sign):
    answers[PATH] = (200, _signed(sign, _config()))
    assert rc.fetch_and_verify() == _config()
    saved = rc.get_last_known_good()
    assert {k: saved[k] for k in _config()} == _config() and saved["saved_at_utc"].endswith("Z")
    assert rc.disabled_strategies_set() == frozenset({"zstd"})


@pytest.mark.parametrize("answer", [
    ConnectionError("offline"),
    (503, {"error": "busy"}),
    (200, ["not", "a", "dict"]),
    (200, {"config": "text", "signature": "ab"}),
    (200, {"config": dict(_config(), extra_key=1), "signature": "ab"}),
    (200, {"config": dict(_config(), disabled_smart_strategies="zstd"), "signature": "ab"}),
    (200, {"config": _config(), "signature": "00" * 64}),
], ids=["offline", "server_error", "not_a_dict", "config_not_a_dict", "unapproved_key", "list_not_a_list", "bad_signature"])
def test_a_config_that_cannot_be_trusted_changes_nothing(answers, sign, answer):
    answers[PATH] = (200, _signed(sign, _config(version=3)))
    rc.fetch_and_verify()
    before = rc.get_last_known_good()
    answers[PATH] = answer
    assert rc.fetch_and_verify() is None
    assert rc.get_last_known_good() == before


def test_an_older_signed_config_never_replaces_a_newer_one(answers, sign):
    answers[PATH] = (200, _signed(sign, _config(version=9, disabled=())))
    rc.fetch_and_verify()
    answers[PATH] = (200, _signed(sign, _config(version=4, disabled=("zstd", "lzma"))))
    assert rc.fetch_and_verify() is None
    assert rc.get_last_known_good()["config_version"] == 9 and rc.disabled_strategies_set() == frozenset()


def test_without_a_saved_config_nothing_is_disabled():
    assert rc.get_last_known_good() is None and rc.disabled_strategies_set() == frozenset()


@pytest.mark.parametrize("content", ['{"config_version": 1}', "[1, 2]", "{ not json"], ids=["missing_keys", "list", "damaged"])
def test_a_damaged_saved_config_counts_as_none(content):
    with open(local_state.remote_config_lkg_path(), "w", encoding="utf-8") as f:
        f.write(content)
    assert rc.get_last_known_good() is None and rc.disabled_strategies_set() == frozenset()


def test_saving_replaces_the_file_whole(answers, sign):
    answers[PATH] = (200, _signed(sign, _config()))
    rc.fetch_and_verify()
    path = local_state.remote_config_lkg_path()
    assert not os.path.exists(path + ".tmp")
    assert json.load(open(path, encoding="utf-8"))["config_version"] == 5
