"""local_cloud.privacy - a payload carrying a forbidden field (a path, a file name, a secret) is a loud error, never
silently cleaned and sent."""
import pytest

from local_cloud import privacy


def test_an_allowed_payload_passes():
    privacy.assert_no_forbidden_keys({"event_type": "app_started", "architecture": "AMD64"})


@pytest.mark.parametrize("field", sorted(privacy.FORBIDDEN_FIELD_NAMES))
def test_every_forbidden_field_is_refused(field):
    with pytest.raises(ValueError, match=field):
        privacy.assert_no_forbidden_keys({"event_type": "app_started", field: "x"})


@pytest.mark.parametrize("payload", [
    {"event": {"details": {"file_path": "C:/x"}}},
    {"rows": [{"ok": 1}, {"inner": [{"username": "someone"}]}]},
    {"a": ({"hostname": "pc"},)},
])
def test_a_forbidden_field_is_found_however_deep_it_is(payload):
    with pytest.raises(ValueError, match="forbidden"):
        privacy.assert_no_forbidden_keys(payload)


def test_values_and_harmless_nested_fields_are_not_mistaken_for_forbidden_ones():
    privacy.assert_no_forbidden_keys({"note": "path", "rows": [{"kind": "file_path"}, ["username"]], "n": {"m": {"ok": 1}}})
