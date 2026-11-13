"""Tests for the Novu HTTP client (validation, headers, retries, errors)."""

from unittest.mock import MagicMock

import pytest
from requests.exceptions import ConnectionError as ReqConnError

from cognee_community_connector_novu.novu import (
    NovuAccessError,
    NovuAPIError,
    NovuAuthError,
    NovuClient,
    NovuNotFoundError,
    NovuRateLimitedError,
)


def test_client_init_valid():
    client = NovuClient("test_token_123", base_url="https://api.novu.co")
    assert client.api_key == "test_token_123"
    assert client.base_url == "https://api.novu.co"


@pytest.mark.parametrize("bad_token", ["", "   ", "token with space", "emoji🔑", "line\nbreak"])
def test_client_rejects_malformed_token(bad_token: str):
    with pytest.raises(ValueError, match="api_key"):
        NovuClient(bad_token)


@pytest.mark.parametrize("bad_url", ["", "   ", "not-a-url", "ftp://", "://broken"])
def test_client_rejects_bad_base_url(bad_url: str):
    with pytest.raises(ValueError, match="base_url"):
        NovuClient("test_token", base_url=bad_url)


def test_client_sends_authorization_header():
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": []}
    mock_session.request.return_value = mock_response

    client = NovuClient("secret_key_123", session=mock_session)
    client.request("GET", "v1/workflows")

    mock_session.request.assert_called_once()
    _, kwargs = mock_session.request.call_args
    assert kwargs["headers"]["Authorization"] == "ApiKey secret_key_123"
    assert kwargs["headers"]["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (401, NovuAuthError),
        (403, NovuAccessError),
        (404, NovuNotFoundError),
        (429, NovuRateLimitedError),
        (500, NovuAPIError),
    ],
)
def test_client_maps_http_error_statuses(status: int, exc_type: type[Exception]):
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.text = f"Simulated {status}"
    mock_session.request.return_value = mock_response

    client = NovuClient("test_token", session=mock_session, retries=0)
    with pytest.raises(exc_type):
        client.request("GET", "v1/workflows")


def test_client_retries_on_502_and_succeeds():
    mock_session = MagicMock()
    resp_fail = MagicMock()
    resp_fail.status_code = 502
    resp_fail.headers = {}
    resp_fail.text = "Bad Gateway"

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {"data": [{"_id": "wf_1"}]}

    mock_session.request.side_effect = [resp_fail, resp_ok]

    client = NovuClient("test_token", session=mock_session, retries=2, backoff_factor=0.01)
    res = client.request("GET", "v1/workflows")
    assert res == {"data": [{"_id": "wf_1"}]}
    assert mock_session.request.call_count == 2


def test_client_retries_on_connection_error_until_exhausted():
    mock_session = MagicMock()
    mock_session.request.side_effect = ReqConnError("Failed to resolve host")

    client = NovuClient("test_token", session=mock_session, retries=2, backoff_factor=0.01)
    with pytest.raises(NovuAPIError, match="failed after 2 retries"):
        client.request("GET", "v1/workflows")
    assert mock_session.request.call_count == 3
