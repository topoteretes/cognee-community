"""Hermetic unit tests for the Atlassian Jira Cognee Community Connector."""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from cognee_community_connector_jira.jira import JiraClient, jira_data, _parse_adf_text

MOCK_ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Fix latency spike in user authentication gateway."}
            ]
        }
    ]
}

MOCK_JIRA_SEARCH_RESPONSE = {
    "issues": [
        {
            "key": "CORE-1042",
            "fields": {
                "summary": "Fix latency spike in user authentication gateway",
                "description": MOCK_ADF_DESCRIPTION,
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Bug"},
                "project": {"name": "Core Backend", "key": "CORE"},
                "assignee": {"displayName": "John Doe"},
                "reporter": {"displayName": "Sarah Connor"},
                "labels": ["performance", "backend", "auth"],
                "created": "2026-11-01T09:00:00.000+0000",
                "updated": "2026-11-03T15:00:00.000+0000"
            }
        }
    ]
}

MOCK_COMMENTS_RESPONSE = {
    "comments": [
        {
            "id": "10001",
            "author": {"displayName": "John Doe"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Identified connection pool exhaustion in Redis cache layer."}
                        ]
                    }
                ]
            },
            "created": "2026-11-03T15:10:00.000+0000"
        }
    ]
}

def test_parse_adf_text():
    """Verifies that ADF nested nodes are parsed recursively into plain text."""
    plain_str = _parse_adf_text("Plain text description")
    assert plain_str == "Plain text description"

    adf_parsed = _parse_adf_text(MOCK_ADF_DESCRIPTION)
    assert "Fix latency spike in user authentication gateway" in adf_parsed

def test_fetch_issues_data_and_formatting():
    """Verifies that Jira issues format into structured Cognee Markdown documents."""
    client = JiraClient(domain="acme", email="dev@acme.com", api_token="sec_token")

    with patch.object(client, "search_issues", return_value=MOCK_JIRA_SEARCH_RESPONSE), \
         patch.object(client, "get_issue_comments", return_value=MOCK_COMMENTS_RESPONSE["comments"]):

        pack = client.fetch_issues_data(projects=["CORE"])

        assert pack["issues_count"] == 1
        iss = pack["issues"][0]
        assert iss["key"] == "CORE-1042"
        assert iss["project"] == "Core Backend"
        assert iss["status"] == "In Progress"
        assert iss["priority"] == "High"
        assert iss["assignee"] == "John Doe"
        assert iss["reporter"] == "Sarah Connor"
        assert "performance" in iss["labels"]

        # Check document text
        assert "# Jira Issue [CORE-1042]: Fix latency spike in user authentication gateway" in iss["text"]
        assert "- **Project**: Core Backend" in iss["text"]
        assert "Fix latency spike in user authentication gateway." in iss["text"]
        assert "Identified connection pool exhaustion in Redis cache layer." in iss["text"]

def test_auth_headers_generation():
    """Verifies generation of Basic Auth and Bearer token headers."""
    # Test Basic Auth
    client_basic = JiraClient(domain="test", email="user@test.com", api_token="tok123")
    headers_basic = client_basic._build_auth_header()
    assert "Authorization" in headers_basic
    assert headers_basic["Authorization"].startswith("Basic ")

    # Test Bearer Token
    client_bearer = JiraClient(domain="test", token="bearer_secret_xyz")
    headers_bearer = client_bearer._build_auth_header()
    assert headers_bearer["Authorization"] == "Bearer bearer_secret_xyz"

def test_incremental_cursor_calculation():
    """Verifies that the highest updated timestamp is stored as cursor."""
    client = JiraClient(domain="test")

    with patch.object(client, "search_issues", return_value=MOCK_JIRA_SEARCH_RESPONSE), \
         patch.object(client, "get_issue_comments", return_value=[]):

        pack = client.fetch_issues_data(since_timestamp="2026-11-01T00:00:00.000+0000")
        assert pack["cursor"] == "2026-11-03T15:00:00.000+0000"

def test_rate_limit_retry_after_handling():
    """Verifies retry and backoff when Jira Cloud returns HTTP 429."""
    client = JiraClient(domain="test")

    err_429 = urllib.error.HTTPError(
        url="https://test.atlassian.net/rest/api/3/search",
        code=429,
        msg="Too Many Requests",
        hdrs={"Retry-After": "1"},
        fp=None
    )

    with patch("urllib.request.urlopen") as mock_open, patch("time.sleep") as mock_sleep:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"issues": []}'
        mock_resp.__enter__.return_value = mock_resp
        mock_open.side_effect = [err_429, mock_resp]

        res = client.search_issues()
        assert res == {"issues": []}
        assert mock_open.call_count == 2
        mock_sleep.assert_called_with(1)

def test_jira_data_generator():
    """Verifies that jira_data yields structured documents compatible with Cognee."""
    mock_pack = {
        "cursor": "2026-11-03T15:00:00.000+0000",
        "issues_count": 1,
        "issues": [
            {
                "id": "jira_issue_CORE-101",
                "key": "CORE-101",
                "summary": "Implement Kafka Ingestion",
                "text": "# Jira Issue [CORE-101]: Implement Kafka Ingestion\n"
            }
        ]
    }

    mock_client = MagicMock()
    mock_client.fetch_issues_data.return_value = mock_pack

    docs = list(jira_data(domain="test", client=mock_client))
    assert len(docs) == 1
    assert docs[0]["id"] == "jira_issue_CORE-101"
    assert docs[0]["type"] == "jira_issue"
    assert "CORE-101" in docs[0]["title"]

def test_auth_failure_raises_permission_error():
    """Verifies that HTTP 401 raises PermissionError with clear message."""
    client = JiraClient(domain="test", email="bad@atlassian.com", api_token="invalid")
    err_401 = urllib.error.HTTPError(
        url="https://test.atlassian.net/rest/api/3/search",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err_401):
        with pytest.raises(PermissionError, match="Jira authentication failed"):
            client.search_issues()

def test_resource_not_found_raises_value_error():
    """Verifies that HTTP 404 raises ValueError."""
    client = JiraClient(domain="test")
    err_404 = urllib.error.HTTPError(
        url="https://test.atlassian.net/rest/api/3/issue/NONEXIST-999",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err_404):
        with pytest.raises(ValueError, match="Jira resource not found"):
            client._http_get("issue/NONEXIST-999")

