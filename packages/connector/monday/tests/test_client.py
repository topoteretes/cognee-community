"""Tests for the monday.com GraphQL client.

A fake transport is mounted on dlt's real retrying session, so retries, paging
and error handling run exactly as in production, without any network calls.
"""

import json

import pytest
import requests
from requests.adapters import BaseAdapter

from cognee_community_connector_monday.monday import (
    API_VERSION,
    MondayAPIError,
    MondayAuthError,
    _clean_token,
    _MondayClient,
    _retrying_session,
)

TOKEN = "test-token-123"
ITEMS_QUERY = "query($cursor: String) { boards { items_page(cursor: $cursor) { cursor } } }"
CURSOR_PATH = "data.boards[0].items_page.cursor"
ITEMS_PATH = "data.boards[0].items_page.items"


class FakeMonday(BaseAdapter):
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
        response._content = json.dumps(body).encode()
        response.headers.update(headers or {})
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def make_client(*replies):
    fake = FakeMonday(*replies)
    # Real retry logic, with the waits between attempts turned off.
    session = _retrying_session(request_backoff_factor=0, request_max_retry_delay=0)
    session.mount("https://", fake)
    return _MondayClient(TOKEN, session=session), fake


def ok(data):
    return (200, {"data": data}, None)


def page(cursor, *ids):
    return ok({"boards": [{"items_page": {"cursor": cursor, "items": [{"id": i} for i in ids]}}]})


def sent_json(request):
    return json.loads(request.body)


def test_query_sends_token_version_and_variables():
    client, fake = make_client(ok({"me": {"id": "1"}}))

    assert client.query("{ me { id } }", {"a": 1}) == {"me": {"id": "1"}}

    request = fake.sent[0]
    assert request.headers["Authorization"] == TOKEN
    assert request.headers["API-Version"] == API_VERSION
    assert sent_json(request) == {"query": "{ me { id } }", "variables": {"a": 1}}


def test_paginate_writes_the_cursor_into_the_variables():
    client, fake = make_client(page("C1", "1", "2"), page("C2", "3"), page(None, "4"))

    items = list(client.paginate(ITEMS_QUERY, {"board": 7}, CURSOR_PATH, ITEMS_PATH))

    assert [item["id"] for item in items] == ["1", "2", "3", "4"]
    assert [sent_json(r)["variables"] for r in fake.sent] == [
        {"board": 7},
        {"board": 7, "cursor": "C1"},
        {"board": 7, "cursor": "C2"},
    ]


def test_http_429_is_retried():
    client, fake = make_client((429, {"errors": [{"message": "slow down"}]}, None), ok({"x": 1}))

    assert client.query("{ x }") == {"x": 1}
    assert len(fake.sent) == 2


def test_rate_limit_inside_a_200_is_retried():
    limited = {
        "errors": [
            {
                "message": "Minute limit rate exceeded",
                "extensions": {"code": "RATE_LIMIT_EXCEEDED", "retry_in_seconds": 0},
            }
        ]
    }
    client, fake = make_client((200, limited, None), ok({"x": 1}))

    assert client.query("{ x }") == {"x": 1}
    assert len(fake.sent) == 2


def test_rejected_token_raises_auth_error_without_echoing_it():
    body = {
        "errors": [{"message": "Not authenticated", "extensions": {"code": "NOT_AUTHENTICATED"}}]
    }
    client, _ = make_client((401, body, None))

    with pytest.raises(MondayAuthError) as excinfo:
        client.query("{ me { id } }")
    assert TOKEN not in str(excinfo.value)


def test_auth_error_code_inside_a_200_raises_auth_error():
    body = {"errors": [{"message": "Unauthorized", "extensions": {"code": "UNAUTHORIZED"}}]}
    client, _ = make_client((200, body, None))

    with pytest.raises(MondayAuthError):
        client.query("{ me { id } }")


def test_graphql_error_inside_a_200_raises_and_is_not_retried():
    body = {"errors": [{"message": 'Cannot query field "nosuchfield" on type "Board".'}]}
    client, fake = make_client((200, body, None))

    with pytest.raises(MondayAPIError, match="nosuchfield"):
        client.query("{ boards { nosuchfield } }")
    assert len(fake.sent) == 1


def test_partial_data_with_errors_still_raises():
    body = {"data": {"boards": []}, "errors": [{"message": "Item not found"}]}
    client, _ = make_client((200, body, None))

    with pytest.raises(MondayAPIError):
        client.query("{ boards { id } }")


def test_old_style_error_code_raises():
    body = {"error_code": "ComplexityException", "error_message": "Max complexity exceeded"}
    client, _ = make_client((200, body, None))

    with pytest.raises(MondayAPIError, match="ComplexityException"):
        client.query("{ boards { id } }")


def test_response_without_data_raises():
    client, _ = make_client((200, {}, None))

    with pytest.raises(MondayAPIError):
        client.query("{ boards { id } }")


def test_server_error_that_persists_raises():
    client, fake = make_client(*[(502, {}, None)] * 5)

    with pytest.raises(MondayAPIError, match="502"):
        client.query("{ x }")
    assert len(fake.sent) == 5


def test_error_on_a_later_page_stops_the_walk():
    # A listing that fails half way must raise, never look like a shorter list.
    client, _ = make_client(page("C1", "1"), (200, {"errors": [{"message": "boom"}]}, None))

    with pytest.raises(MondayAPIError, match="boom"):
        list(client.paginate(ITEMS_QUERY, {}, CURSOR_PATH, ITEMS_PATH))


@pytest.mark.parametrize("bad", [None, "", "   ", "abc def", "tökén"])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(MondayAuthError) as excinfo:
        _clean_token(bad)
    if bad and bad.strip():
        assert bad.strip() not in str(excinfo.value)


def test_token_whitespace_is_stripped():
    assert _clean_token("  abc  \n") == "abc"
