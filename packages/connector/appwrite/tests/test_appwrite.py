"""Unit tests for the Appwrite data-source connector."""

from unittest.mock import MagicMock

import httpx
import pytest

from cognee_community_connector_appwrite.appwrite import (
    APPWRITE_SOURCE_NAME,
    AppwriteClient,
    _document_to_row,
    _render_document_content,
    appwrite_source,
)


def test_render_document_content_and_metadata():
    """Verify document body formatting and sensitive field filtering."""
    doc = {
        "$id": "doc123",
        "$createdAt": "2026-10-01T10:00:00Z",
        "$updatedAt": "2026-10-02T12:00:00Z",
        "$permissions": ['read("any")'],
        "title": "Onboarding Guide",
        "content": "Welcome to our knowledge base.",
        "author": "Alice Smith",
        "category": "Documentation",
        "password": "secret_cleartext_password",
        "api_key": "secret_key_12345",
    }

    content = _render_document_content(doc, "Onboarding Guide")
    assert "# Onboarding Guide" in content
    assert "Welcome to our knowledge base." in content
    assert "author: Alice Smith" in content
    assert "category: Documentation" in content
    assert "$createdAt: 2026-10-01T10:00:00Z" in content
    # Ensure sensitive fields are excluded
    assert "secret_cleartext_password" not in content
    assert "secret_key_12345" not in content
    assert "$permissions" not in content


def test_document_to_row_structure():
    """Verify standardized Cognee document row formatting."""
    doc = {
        "$id": "post_999",
        "title": "Architecture Overview",
        "content": "System architecture diagram and explanation.",
    }
    row = _document_to_row(
        endpoint="https://cloud.appwrite.io/v1",
        database_id="main_db",
        collection_id="articles",
        doc=doc,
    )

    assert row["id"] == "appwrite:main_db:articles:post_999"
    assert (
        row["url"]
        == "https://cloud.appwrite.io/v1/databases/main_db/collections/articles/documents/post_999"
    )
    assert row["title"] == "Architecture Overview"
    assert "# Architecture Overview" in row["content"]


def test_client_pagination_streaming():
    """Verify offset-based pagination streaming across pages."""
    mock_http = MagicMock()

    page1_docs = [{"$id": f"doc_{i}", "title": f"Doc {i}"} for i in range(1, 3)]
    page2_docs = [{"$id": f"doc_{i}", "title": f"Doc {i}"} for i in range(3, 5)]

    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {"total": 4, "documents": page1_docs}

    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {"total": 4, "documents": page2_docs}

    mock_http.request.side_effect = [resp1, resp2]

    client = AppwriteClient(
        endpoint="https://cloud.appwrite.io/v1",
        project_id="proj_test",
        api_key="key_test",
        http_client=mock_http,
    )

    docs = list(client.iter_documents("db1", "col1"))
    assert len(docs) == 4
    assert docs[0]["$id"] == "doc_1"
    assert docs[3]["$id"] == "doc_4"
    assert mock_http.request.call_count == 2


def test_client_retry_on_rate_limit_and_transient_error(monkeypatch):
    """Verify retry logic on 429 status code with Retry-After header."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    mock_http = MagicMock()

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "2"}
    http_err = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    resp_ok = MagicMock()
    resp_ok.raise_for_status.return_value = None
    resp_ok.json.return_value = {
        "total": 1,
        "documents": [{"$id": "doc_retry", "title": "Recovered"}],
    }

    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = http_err

    mock_http.request.side_effect = [resp_fail, resp_ok]

    client = AppwriteClient(
        endpoint="https://cloud.appwrite.io/v1",
        project_id="proj_test",
        api_key="key_test",
        http_client=mock_http,
    )

    docs = list(client.iter_documents("db1", "col1"))
    assert len(docs) == 1
    assert docs[0]["$id"] == "doc_retry"
    assert mock_http.request.call_count == 2


def test_client_close_resource():
    """Verify that close() delegates to underlying HTTP client when owned."""
    client = AppwriteClient(endpoint="https://cloud.appwrite.io/v1")
    assert hasattr(client.client, "close")
    client.close()


def test_appwrite_source_snapshot_and_cleanup():
    """Verify appwrite_source sets DOCUMENT_SOURCE_ATTR and yields formatted rows."""
    mock_client = MagicMock()
    mock_client.endpoint = "https://cloud.appwrite.io/v1"
    mock_client.iter_documents.return_value = iter(
        [
            {"$id": "d1", "title": "First", "content": "Hello"},
            {"$id": "d2", "title": "Second", "content": "World"},
        ]
    )

    source = appwrite_source(
        database_id="db1",
        collection_ids=["col1"],
        client=mock_client,
    )

    assert getattr(source, "cognee_document_source", None) == APPWRITE_SOURCE_NAME

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())
    assert len(rows) == 2
    assert rows[0]["id"] == "appwrite:db1:col1:d1"
    assert rows[1]["id"] == "appwrite:db1:col1:d2"


def test_appwrite_source_missing_credentials_raises():
    """Verify ValueError when required project_id or api_key are missing."""
    with pytest.raises(ValueError, match="Appwrite Project ID required"):
        appwrite_source(
            database_id="db1",
            collection_ids=["col1"],
            project_id=None,
            api_key="valid_key",
        )

    with pytest.raises(ValueError, match="Appwrite API Key required"):
        appwrite_source(
            database_id="db1",
            collection_ids=["col1"],
            project_id="valid_project",
            api_key=None,
        )


def test_sensitive_field_sanitization_in_nested_dict():
    """Verify nested dictionaries have sensitive fields removed."""
    doc = {
        "$id": "nested_1",
        "title": "Nested Doc",
        "metadata": {
            "department": "Engineering",
            "password": "super_secret_value",
            "token": "secret_jwt_token",
        },
    }
    content = _render_document_content(doc, "Nested Doc")
    assert "Engineering" in content
    assert "super_secret_value" not in content
    assert "secret_jwt_token" not in content
