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
