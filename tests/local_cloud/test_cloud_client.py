"""local_cloud.cloud_client - every request is bounded, names itself, and turns any answer (or none) into a result the
sender can act on."""
import pytest

from local_cloud import cloud_client
from local_cloud.cloud_client import CloudResult


@pytest.mark.parametrize("status, ok, retryable", [
    (200, True, False), (201, True, False), (400, False, False), (401, False, False), (429, False, True),
    (500, False, True), (CloudResult.TIMEOUT, False, True), (CloudResult.NETWORK_ERROR, False, True),
    ("something else", False, False),
])
def test_what_counts_as_success_and_what_is_worth_retrying(status, ok, retryable):
    result = CloudResult(status)
    assert (result.ok, result.retryable) == (ok, retryable)


def test_a_public_read_names_the_client_and_carries_no_installation_data(server):
    result = cloud_client.get_public("/api/v1/config")
    assert result.ok
    (request,) = server.requests
    assert request["headers"]["User-Agent"].startswith("StorageArch-Cloud-Client/")
    assert not any(h.startswith("X-StorageArch") for h in request["headers"])


def test_an_answer_that_is_not_json_has_no_body(server):
    server.mode = "garbage"
    result = cloud_client.get_public("/api/v1/config")
    assert result.status == 200 and result.body is None


def test_an_unreachable_server_is_a_retryable_result_not_an_exception():
    result = cloud_client.get_public("/api/v1/config")  # the root conftest points the client at a closed port
    assert result.status == CloudResult.TIMEOUT and result.retryable


def test_an_unsigned_post_carries_no_signature(server):
    result = cloud_client.post_unsigned("/api/v1/feedback", {"category": "bug"})
    assert result.ok
    (request,) = server.posts("/api/v1/feedback")
    assert request["body"] == {"category": "bug"}
    assert not any(h.startswith("X-StorageArch") for h in request["headers"])
