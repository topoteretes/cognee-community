"""Hermetic unit tests for the Zendesk Cognee Community Connector."""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from cognee_community_connector_zendesk.zendesk import ZendeskClient, zendesk_data

MOCK_USERS = {
    "users": [
        {"id": 101, "name": "Alice Customer", "email": "alice@customer.com"},
        {"id": 202, "name": "Bob Support Agent", "email": "bob@company.com"}
    ]
}

MOCK_TICKETS = {
    "tickets": [
        {
            "id": 9001,
            "subject": "Cannot export monthly billing invoice",
            "description": "Clicking export invoice returns HTTP 500 error on checkout page.",
            "status": "open",
            "priority": "high",
            "type": "incident",
            "requester_id": 101,
            "assignee_id": 202,
            "tags": ["billing", "export", "enterprise"],
            "created_at": "2026-11-01T10:00:00Z",
            "updated_at": "2026-11-03T14:30:00Z"
        }
    ]
}

MOCK_COMMENTS = {
    "comments": [
        {
            "id": 501,
            "author_id": 101,
            "public": True,
            "body": "Still getting 500 when clicking download button.",
            "created_at": "2026-11-01T10:05:00Z"
        },
        {
            "id": 502,
            "author_id": 202,
            "public": False,
            "body": "Investigating billing microservice error logs on cluster.",
            "created_at": "2026-11-01T10:15:00Z"
        }
    ]
}

def test_fetch_tickets_data_and_formatting():
    """Verifies that ticket metadata, user names, and conversation threads format into rich docs."""
    client = ZendeskClient(subdomain="acme", email="admin@acme.com", api_token="secret_token")

    with patch.object(client, "get_users_map", return_value={101: MOCK_USERS["users"][0], 202: MOCK_USERS["users"][1]}), \
         patch.object(client, "get_tickets", return_value=MOCK_TICKETS["tickets"]), \
         patch.object(client, "get_ticket_comments", return_value=MOCK_COMMENTS["comments"]):

        pack = client.fetch_tickets_data()

        assert pack["tickets_count"] == 1
        ticket = pack["tickets"][0]
        assert ticket["ticket_id"] == 9001
        assert ticket["status"] == "OPEN"
        assert ticket["priority"] == "HIGH"
        assert ticket["requester"] == "Alice Customer"
        assert ticket["assignee"] == "Bob Support Agent"
        assert "billing" in ticket["tags"]

        # Check document text
        assert "# Zendesk Ticket #9001: Cannot export monthly billing invoice" in ticket["text"]
        assert "Alice Customer (alice@customer.com)" in ticket["text"]
        assert "Bob Support Agent" in ticket["text"]
        assert "Public Reply" in ticket["text"]
        assert "Internal Note" in ticket["text"]
        assert "Investigating billing microservice error logs" in ticket["text"]

def test_auth_headers_generation():
    """Verifies generation of Basic Auth and OAuth Bearer headers."""
    # Test Basic Auth with email/token
    client_basic = ZendeskClient(subdomain="test", email="user@test.com", api_token="tok123")
    headers_basic = client_basic._build_auth_header()
    assert "Authorization" in headers_basic
    assert headers_basic["Authorization"].startswith("Basic ")

    # Test OAuth Bearer token
    client_oauth = ZendeskClient(subdomain="test", oauth_token="oauth_secret_abc")
    headers_oauth = client_oauth._build_auth_header()
    assert headers_oauth["Authorization"] == "Bearer oauth_secret_abc"

def test_incremental_cursor_calculation():
    """Verifies that the highest updated_at timestamp is computed as the epoch cursor."""
    client = ZendeskClient(subdomain="test")

    with patch.object(client, "get_users_map", return_value={}), \
         patch.object(client, "get_tickets", return_value=MOCK_TICKETS["tickets"]), \
         patch.object(client, "get_ticket_comments", return_value=[]):

        pack = client.fetch_tickets_data(start_time_epoch=1700000000)
        assert pack["cursor_epoch"] > 1700000000

def test_rate_limit_retry_after_handling():
    """Verifies retry and backoff when Zendesk returns HTTP 429."""
    client = ZendeskClient(subdomain="test")

    err_429 = urllib.error.HTTPError(
        url="https://test.zendesk.com/api/v2/tickets.json",
        code=429,
        msg="Too Many Requests",
        hdrs={"Retry-After": "1"},
        fp=None
    )

    with patch("urllib.request.urlopen") as mock_open, patch("time.sleep") as mock_sleep:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"tickets": []}'
        mock_resp.__enter__.return_value = mock_resp
        mock_open.side_effect = [err_429, mock_resp]

        res = client.get_tickets()
        assert res == []
        assert mock_open.call_count == 2
        mock_sleep.assert_called_with(1)

def test_zendesk_data_generator():
    """Verifies that zendesk_data yields structured documents compatible with Cognee."""
    mock_pack = {
        "cursor_epoch": 1780000000,
        "tickets_count": 1,
        "tickets": [
            {
                "id": "zendesk_ticket_100",
                "ticket_id": 100,
                "subject": "Login Bug",
                "text": "# Zendesk Ticket #100: Login Bug\n"
            }
        ]
    }

    mock_client = MagicMock()
    mock_client.fetch_tickets_data.return_value = mock_pack

    docs = list(zendesk_data(subdomain="test", client=mock_client))
    assert len(docs) == 1
    assert docs[0]["id"] == "zendesk_ticket_100"
    assert docs[0]["type"] == "zendesk_ticket"
    assert "Login Bug" in docs[0]["title"]

def test_auth_failure_raises_permission_error():
    """Verifies that HTTP 401 raises PermissionError with clear message."""
    client = ZendeskClient(subdomain="test", email="bad@email.com", api_token="bad_tok")
    err_401 = urllib.error.HTTPError(
        url="https://test.zendesk.com/api/v2/tickets.json",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err_401):
        with pytest.raises(PermissionError, match="Zendesk authentication failed"):
            client.get_tickets()

def test_resource_not_found_raises_value_error():
    """Verifies that HTTP 404 raises ValueError."""
    client = ZendeskClient(subdomain="test")
    err_404 = urllib.error.HTTPError(
        url="https://test.zendesk.com/api/v2/tickets/999999/comments.json",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err_404):
        with pytest.raises(ValueError, match="Zendesk resource not found"):
            client._http_get("tickets/999999/comments.json")

