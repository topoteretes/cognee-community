"""Tests for the Databricks REST client.

A fake transport is mounted on dlt's real retrying session, so retries, paging
and error handling run exactly as in production, without any network calls.
The error bodies are the ones a real Free Edition workspace returned.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import BaseAdapter

from cognee_community_connector_databricks import (
    DatabricksAuthError,
    DatabricksConnectorError,
    DatabricksNotFoundError,
)
from cognee_community_connector_databricks.databricks import (
    _WORKSPACE_HEADER,
    _clean_host,
    _clean_token,
    _DatabricksClient,
    _retrying_session,
)

HOST = "https://dbc-1234-abcd.cloud.databricks.com"
TOKEN = "dapi-test-token-123"
WORKSPACE = "7474656264538481"
PRIVATE_PATH = "/Users/someone@example.com/cognee_demo/notebook"

# What a real workspace answered for a path that does not exist: the message
# repeats the path, with the user's email in it.
NOT_FOUND = (
    404,
    {
        "error_code": "RESOURCE_DOES_NOT_EXIST",
        "message": f"Path ({PRIVATE_PATH}) doesn't exist.",
    },
    None,
)


class FakeDatabricks(BaseAdapter):
    """Replies with canned responses, in order, and records what was sent."""

    def __init__(self, *replies):
        super().__init__()
        self.replies = list(replies)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        status, body, headers = self.replies.pop(0)
        response = requests.Response()
        response.status_code = status
        response._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        response.headers.update({_WORKSPACE_HEADER: WORKSPACE, **(headers or {})})
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def make_client(*replies, check_active=None):
    fake = FakeDatabricks(*replies)
    # Real retry logic, with the waits between attempts turned off.
    session = _retrying_session(request_backoff_factor=0, request_max_retry_delay=0)
    session.mount("https://", fake)
    return _DatabricksClient(HOST, TOKEN, session=session, check_active=check_active), fake


def ok(body):
    return (200, body, None)


def query(request):
    return {key: values[0] for key, values in parse_qs(urlparse(request.url).query).items()}


def test_get_sends_the_token_and_params_and_returns_the_json():
    client, fake = make_client(ok({"objects": [{"object_id": 1}]}))

    assert client.get("/api/2.0/workspace/list", {"path": "/Shared"}) == {
        "objects": [{"object_id": 1}]
    }
    request = fake.sent[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.url.startswith(f"{HOST}/api/2.0/workspace/list")
    assert query(request) == {"path": "/Shared"}


def test_the_workspace_id_is_read_from_the_response_header():
    client, _ = make_client(ok({}))

    client.get("/api/2.0/workspace/list", {"path": "/"})

    assert client.workspace_id == WORKSPACE


def test_pages_follow_the_next_page_token():
    client, fake = make_client(
        ok({"tables": [{"name": "a"}, {"name": "b"}], "next_page_token": "T1"}),
        ok({"tables": [{"name": "c"}], "next_page_token": "T2"}),
        ok({"tables": [{"name": "d"}]}),
    )

    tables = list(client.pages("/api/2.1/unity-catalog/tables", {"schema_name": "s"}, "tables"))

    assert [table["name"] for table in tables] == ["a", "b", "c", "d"]
    assert [query(request).get("page_token") for request in fake.sent] == [None, "T1", "T2"]
    assert all(query(request)["schema_name"] == "s" for request in fake.sent)


def test_an_empty_listing_without_its_list_reads_as_no_items():
    # A real empty folder or schema answers {}.
    client, _ = make_client(ok({}))

    assert list(client.pages("/api/2.1/unity-catalog/tables", {}, "tables")) == []


@pytest.mark.parametrize("broken", [None, {"name": "a"}, "tables"])
def test_a_listing_whose_list_is_not_a_list_raises(broken):
    client, _ = make_client(ok({"tables": broken}))

    with pytest.raises(DatabricksConnectorError, match="not a list"):
        list(client.pages("/api/2.1/unity-catalog/tables", {}, "tables"))


def test_a_failure_on_a_later_page_raises():
    client, _ = make_client(
        ok({"tables": [{"name": "a"}], "next_page_token": "T1"}),
        (400, {"error_code": "INVALID_PARAMETER_VALUE", "message": "bad token"}, None),
    )

    with pytest.raises(DatabricksConnectorError, match="INVALID_PARAMETER_VALUE"):
        list(client.pages("/api/2.1/unity-catalog/tables", {}, "tables"))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limits_and_server_errors_are_retried(status):
    client, fake = make_client(
        (status, {"error_code": "REQUEST_LIMIT_EXCEEDED"}, None), ok({"objects": []})
    )

    assert client.get("/api/2.0/workspace/list", {"path": "/"}) == {"objects": []}
    assert len(fake.sent) == 2


def test_a_rate_limit_that_persists_raises_after_the_retries():
    replies = [(429, {"error_code": "REQUEST_LIMIT_EXCEEDED"}, None)] * 10
    client, _ = make_client(*replies)

    with pytest.raises(DatabricksConnectorError, match="429"):
        client.get("/api/2.0/workspace/list", {"path": "/"})


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error_code": 401, "message": "Credential was not sent."}),
        (403, {"error_code": 403, "message": "Invalid access token."}),
    ],
)
def test_a_refused_token_raises_an_auth_error(status, body):
    client, _ = make_client((status, body, None))

    with pytest.raises(DatabricksAuthError, match=f"HTTP {status}") as excinfo:
        client.get("/api/2.0/workspace/list", {"path": "/"})
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "code", ["RESOURCE_DOES_NOT_EXIST", "TABLE_DOES_NOT_EXIST", "SCHEMA_DOES_NOT_EXIST"]
)
def test_a_missing_object_raises_not_found_by_its_code(code):
    client, _ = make_client((404, {"error_code": code, "message": f"{PRIVATE_PATH} is gone"}, None))

    with pytest.raises(DatabricksNotFoundError, match=code):
        client.get("/api/2.0/workspace/get-status", {"path": PRIVATE_PATH})


def test_errors_never_repeat_the_message_or_the_path():
    client, _ = make_client(NOT_FOUND)

    with pytest.raises(DatabricksNotFoundError) as excinfo:
        client.get("/api/2.0/workspace/get-status", {"path": PRIVATE_PATH})
    text = str(excinfo.value)
    assert "someone@example.com" not in text
    assert "cognee_demo" not in text


def test_a_404_with_another_code_is_not_read_as_not_found():
    # Only a code that says the object is missing may lead to forgetting it.
    client, _ = make_client((404, {"error_code": "FEATURE_DISABLED"}, None))

    with pytest.raises(DatabricksConnectorError) as excinfo:
        client.get("/api/2.0/workspace/move", {})
    assert not isinstance(excinfo.value, DatabricksNotFoundError)
    assert "FEATURE_DISABLED" in str(excinfo.value)


@pytest.mark.parametrize("body", ["<html>maintenance</html>", ["a", "list"]])
def test_a_200_that_is_not_a_json_object_raises(body):
    client, _ = make_client(ok(body))

    with pytest.raises(DatabricksConnectorError, match="not JSON"):
        client.get("/api/2.0/workspace/list", {"path": "/"})


def test_check_active_runs_before_every_request_and_page():
    calls = []
    client, fake = make_client(
        ok({}),
        ok({"tables": [{"name": "a"}], "next_page_token": "T1"}),
        ok({"tables": [{"name": "b"}]}),
        check_active=lambda: calls.append(len(fake.sent)),
    )

    client.get("/api/2.0/workspace/list", {"path": "/"})
    list(client.pages("/api/2.1/unity-catalog/tables", {}, "tables"))

    # Before request 1, before page 1, before page 2 (and once more to find
    # there is no page 3), never after the fact.
    assert calls[:3] == [0, 1, 2]


def test_a_revoked_host_stops_before_the_request_is_sent():
    def revoked():
        raise PermissionError("revoked")

    client, fake = make_client(ok({}), check_active=revoked)

    with pytest.raises(PermissionError):
        client.get("/api/2.0/workspace/list", {"path": "/"})
    assert fake.sent == []


@pytest.mark.parametrize(
    "typed",
    [
        "https://dbc-1234-abcd.cloud.databricks.com",
        "https://dbc-1234-abcd.cloud.databricks.com/",
        "dbc-1234-abcd.cloud.databricks.com",
        " https://dbc-1234-abcd.cloud.databricks.com/?o=7474656264538481#workspace ",
        "https://dbc-1234-abcd.cloud.databricks.com/browse/folders/123?o=1",
    ],
)
def test_the_workspace_url_keeps_only_the_host(typed):
    assert _clean_host(typed) == HOST


@pytest.mark.parametrize("typed", ["", "   ", None, "http://dbc-1234.cloud.databricks.com"])
def test_a_missing_or_plain_http_workspace_url_is_refused(typed):
    with pytest.raises(ValueError):
        _clean_host(typed)


@pytest.mark.parametrize("token", ["", "  ", None, "dapi abc", "dapié"])
def test_a_malformed_token_is_refused_without_echoing_it(token):
    with pytest.raises(DatabricksAuthError) as excinfo:
        _clean_token(token)
    if token and token.strip():
        assert token.strip() not in str(excinfo.value)
