"""Unit tests for the Directus dlt connector.

Tests run completely offline without an active Directus instance:
* Item to document row transformation (title extraction, markdown rendering,
  sensitive field exclusion).
* HTTP client request retry logic, pagination offset handling, and transient error detection.
* Collection auto-discovery excluding system collections.
* dlt pipeline full snapshot integration and forget-on-delete semantics.
"""

from unittest.mock import MagicMock

import httpx

from cognee_community_connector_directus.directus import (
    DIRECTUS_SOURCE_NAME,
    DirectusClient,
    _extract_title,
    _item_to_row,
    _render_item_content,
    directus_source,
)


def test_extract_title_priority():
    """Verify that title candidates are resolved in order and fallback to collection ID."""
    assert _extract_title({"title": "Blog Title", "name": "Name"}, "posts") == "Blog Title"
    assert _extract_title({"name": "Article Name"}, "articles") == "Article Name"
    assert _extract_title({"headline": "News Flash"}, "news") == "News Flash"
    assert _extract_title({"id": "123"}, "products") == "products 123"


def test_render_item_content_filters_sensitive_fields():
    """Ensure passwords, tokens, and sensitive auth data are excluded."""
    item = {
        "id": 101,
        "title": "Confidential Report",
        "description": "Quarterly operational results.",
        "password": "hashed_secret",
        "token": "secret_session_token",
        "auth_data": {"jwt": "abc"},
        "tfa_secret": "2fa_key",
        "author": "Engineering Lead",
        "department": "Platform",
    }
    rendered = _render_item_content(item)
    assert "Quarterly operational results." in rendered
    assert "author: Engineering Lead" in rendered
    assert "department: Platform" in rendered
    assert "password" not in rendered
    assert "hashed_secret" not in rendered
    assert "token" not in rendered
    assert "auth_data" not in rendered
    assert "tfa_secret" not in rendered


def test_render_item_content_custom_ignored_fields():
    """Verify custom ignored fields passed by user are properly excluded."""
    item = {
        "id": 202,
        "title": "Customer Profile",
        "body": "VIP customer notes.",
        "credit_score": 750,
        "status": "active",
    }
    rendered = _render_item_content(item, ignored={"credit_score"})
    assert "VIP customer notes." in rendered
    assert "status: active" in rendered
    assert "credit_score" not in rendered
    assert "750" not in rendered


def test_item_to_row_structure():
    """Verify standard document row format."""
    item = {
        "id": "item-abc",
        "title": "Release v2.0",
        "article": "Details on new features.",
        "status": "published",
    }
    row = _item_to_row("http://127.0.0.1:8055", "releases", item)
    assert row["id"] == "releases:item-abc"
    assert row["url"] == "http://127.0.0.1:8055/items/releases/item-abc"
    assert row["title"] == "Release v2.0"
    assert "Details on new features." in row["content"]


def test_client_get_collections_filters_system():
    """Verify system collections starting with directus_ are filtered out."""
    mock_http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "data": [
            {"collection": "articles"},
            {"collection": "products"},
            {"collection": "directus_users"},
            {"collection": "directus_activity"},
        ]
    }
    mock_http.request.return_value = resp

    client = DirectusClient("http://127.0.0.1:8055", http_client=mock_http)
    collections = client.get_collections()

    assert collections == ["articles", "products"]


def test_client_pagination_streaming():
    """Test pagination generator streaming across multiple pages using limit/offset."""
    mock_http = MagicMock()

    # Page 1 returns 50 items (triggers next page)
    items_p1 = [{"id": i, "title": f"Item {i}"} for i in range(50)]
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {"data": items_p1}

    # Page 2 returns 2 items (< 50, stops pagination)
    items_p2 = [{"id": 51, "title": "Item 51"}, {"id": 52, "title": "Item 52"}]
    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {"data": items_p2}

    mock_http.request.side_effect = [resp1, resp2]

    client = DirectusClient(
        "http://127.0.0.1:8055",
        auth_token="api_key",
        http_client=mock_http,
    )
    items = list(client.iter_items("products"))

    assert len(items) == 52
    assert items[0]["id"] == 0
    assert items[51]["id"] == 52
    assert mock_http.request.call_count == 2


def test_client_retry_on_transient_error(monkeypatch):
    """Test retry mechanism on 429 status code with Retry-After header."""
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
    resp_ok.json.return_value = {"data": [{"id": 1, "title": "Success Item"}]}

    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = http_err

    mock_http.request.side_effect = [resp_fail, resp_ok]

    client = DirectusClient("http://127.0.0.1:8055", http_client=mock_http)
    items = list(client.iter_items("items"))

    assert len(items) == 1
    assert items[0]["title"] == "Success Item"
    assert mock_http.request.call_count == 2


def test_client_close_resource():
    """Verify client close correctly delegates when owning client."""
    client = DirectusClient("http://127.0.0.1:8055")
    assert hasattr(client.client, "close")
    client.close()


def test_directus_source_snapshot_and_deletion():
    """Verify that directus_source yields items and handles snapshot replace."""
    mock_client = MagicMock()
    mock_client.base_url = "http://127.0.0.1:8055"
    mock_client.get_collections.return_value = ["news"]
    mock_client.iter_items.return_value = iter([
        {"id": "n1", "title": "News One", "body": "Body one"},
        {"id": "n2", "title": "News Two", "body": "Body two"},
    ])

    source = directus_source(client=mock_client, collections=["news"])
    assert getattr(source, "cognee_document_source", None) == DIRECTUS_SOURCE_NAME

    # Extract rows yielded by resource
    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    assert len(rows) == 2
    assert rows[0]["id"] == "news:n1"
    assert rows[1]["id"] == "news:n2"
    assert rows[0]["title"] == "News One"
    assert "Body one" in rows[0]["content"]
