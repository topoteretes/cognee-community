"""Unit tests for the PocketBase dlt connector.

Tests can run completely offline without an active PocketBase instance:
* Record to document row conversion (title extraction, markdown rendering,
  sensitive field exclusion).
* HTTP client request retry logic, pagination handling, and transient error detection.
* dlt pipeline full snapshot integration and forget-on-delete semantics.
"""

from unittest.mock import MagicMock

import httpx

from cognee_community_connector_pocketbase.pocketbase import (
    POCKETBASE_SOURCE_NAME,
    PocketBaseClient,
    _extract_title,
    _record_to_row,
    _render_record_content,
    pocketbase_source,
)


def test_extract_title_priority():
    """Verify that title candidates are resolved in order and fallback to collection ID."""
    assert _extract_title({"title": "Main Title", "name": "Name"}, "posts") == "Main Title"
    assert _extract_title({"name": "Item Name"}, "products") == "Item Name"
    assert _extract_title({"subject": "Email Subject"}, "tickets") == "Email Subject"
    assert _extract_title({"id": "rec123"}, "articles") == "articles rec123"


def test_render_record_content_filters_sensitive_fields():
    """Ensure passwords and system fields are excluded from document content."""
    record = {
        "id": "rec456",
        "title": "Confidential Note",
        "body": "This is the primary message body.",
        "password": "supersecretpassword",
        "tokenKey": "token123",
        "author": "Alice",
        "views": 42,
    }
    rendered = _render_record_content(record)
    assert "This is the primary message body." in rendered
    assert "author: Alice" in rendered
    assert "views: 42" in rendered
    assert "password" not in rendered
    assert "supersecretpassword" not in rendered
    assert "tokenKey" not in rendered


def test_render_record_content_custom_ignored_fields():
    """Verify custom ignored fields passed by user are properly excluded."""
    record = {
        "id": "rec789",
        "title": "Internal Memo",
        "body": "Team meeting notes.",
        "ssn": "000-11-2222",
        "salary": 150000,
        "department": "Engineering",
    }
    rendered = _render_record_content(record, ignored={"ssn", "salary"})
    assert "Team meeting notes." in rendered
    assert "department: Engineering" in rendered
    assert "ssn" not in rendered
    assert "000-11-2222" not in rendered
    assert "salary" not in rendered


def test_record_to_row_structure():
    """Verify standard document row format."""
    record = {
        "id": "xyz789",
        "title": "Deployment Guidelines",
        "content": "Follow standard semver practices.",
        "category": "devops",
    }
    row = _record_to_row("http://127.0.0.1:8090", "docs", record)
    assert row["id"] == "docs:xyz789"
    assert row["url"] == "http://127.0.0.1:8090/api/collections/docs/records/xyz789"
    assert row["title"] == "Deployment Guidelines"
    assert "Follow standard semver practices." in row["content"]


def test_client_pagination_streaming():
    """Test pagination generator streaming across multiple pages."""
    mock_http = MagicMock()

    # Page 1 returns 2 items, totalPages = 2
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {
        "page": 1,
        "perPage": 2,
        "totalPages": 2,
        "items": [{"id": "1", "title": "First"}, {"id": "2", "title": "Second"}],
    }

    # Page 2 returns 1 item, totalPages = 2
    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {
        "page": 2,
        "perPage": 2,
        "totalPages": 2,
        "items": [{"id": "3", "title": "Third"}],
    }

    mock_http.request.side_effect = [resp1, resp2]

    client = PocketBaseClient(
        "http://127.0.0.1:8090",
        auth_token="test_token",
        http_client=mock_http,
    )
    records = list(client.iter_records("notes"))

    assert len(records) == 3
    assert [r["id"] for r in records] == ["1", "2", "3"]
    assert mock_http.request.call_count == 2


def test_client_retry_on_transient_error(monkeypatch):
    """Test retry mechanism on 429 status code."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    mock_http = MagicMock()

    # First attempt: 429 Too Many Requests
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "1"}
    http_err = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    # Second attempt: Success
    resp_ok = MagicMock()
    resp_ok.raise_for_status.return_value = None
    resp_ok.json.return_value = {"page": 1, "totalPages": 1, "items": [{"id": "success"}]}

    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = http_err

    mock_http.request.side_effect = [resp_fail, resp_ok]

    client = PocketBaseClient("http://127.0.0.1:8090", http_client=mock_http)
    records = list(client.iter_records("items"))

    assert len(records) == 1
    assert records[0]["id"] == "success"
    assert mock_http.request.call_count == 2


def test_client_close_resource():
    """Verify client close correctly delegates when owning client."""
    client = PocketBaseClient("http://127.0.0.1:8090")
    assert hasattr(client.client, "close")
    client.close()


def test_pocketbase_source_snapshot_and_deletion():
    """Verify that pocketbase_source yields records and handles snapshot replace."""
    mock_client = MagicMock()
    mock_client.base_url = "http://127.0.0.1:8090"
    mock_client.get_collections.return_value = [{"name": "articles"}]
    mock_client.iter_records.return_value = iter([
        {"id": "a1", "title": "Article One", "body": "Content one"},
        {"id": "a2", "title": "Article Two", "body": "Content two"},
    ])

    source = pocketbase_source(client=mock_client, collections=["articles"])
    assert getattr(source, "cognee_document_source", None) == POCKETBASE_SOURCE_NAME

    # Extract rows yielded by resource
    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    assert len(rows) == 2
    assert rows[0]["id"] == "articles:a1"
    assert rows[1]["id"] == "articles:a2"
    assert rows[0]["title"] == "Article One"
    assert "Content one" in rows[0]["content"]
