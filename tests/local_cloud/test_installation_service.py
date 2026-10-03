"""local_cloud.installation_service - registering with a Beta code and disconnecting: every failure is a reason, never an
exception, and nothing is half-saved."""
import pytest

from local_cloud import cloud_client, dpapi_store, installation_service as inst, local_state


def test_a_valid_code_registers_and_stores_the_credentials(server):
    assert inst.register("  VALID-CODE  ") == (True, None)
    assert inst.is_registered() and inst.get_installation_id() == "11111111-2222-4333-8444-555555555555"
    assert local_state.load_config()["registered_at_utc"].endswith("Z")


@pytest.mark.parametrize("code", ["", "   ", None, 12345])
def test_a_missing_code_is_refused_without_asking_the_server(server, code):
    assert inst.register(code) == (False, "invalid_beta_code")
    assert server.posts("/api/v1/installations/register") == []


def test_a_wrong_code_reports_the_servers_reason(server):
    assert inst.register("WRONG") == (False, "invalid_beta_code") and not inst.is_registered()


def test_a_server_error_without_a_reason_reports_its_status(server):
    server.mode = "garbage"  # a 200 whose body is not JSON: no credentials in it
    assert inst.register("VALID-CODE") == (False, "malformed_response")
    server.mode = "error500"
    assert inst.register("VALID-CODE") == (False, "internal")


def test_a_client_failure_is_a_network_error(monkeypatch):
    def broken(code):
        raise RuntimeError("socket layer broke")

    monkeypatch.setattr(cloud_client, "register", broken)
    assert inst.register("VALID-CODE") == (False, "network_error")


def test_credentials_that_cannot_be_stored_leave_the_install_unregistered(server, monkeypatch):
    def refuse(*args):
        raise OSError("profile is read-only")

    monkeypatch.setattr(dpapi_store, "save_credentials", refuse)
    assert inst.register("VALID-CODE") == (False, "credential_storage_failed")
    assert not inst.is_registered() and local_state.load_config()["installation_id"] is None


def test_disconnecting_forgets_the_installation(registered):
    assert inst.disconnect() is True
    assert not inst.is_registered() and local_state.load_config()["installation_id"] is None
