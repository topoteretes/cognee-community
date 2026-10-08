"""Unit tests for the Outline Knowledge Base data-source connector."""

from unittest.mock import MagicMock

import httpx
import pytest

from cognee_community_connector_outline.outline import (
    OUTLINE_SOURCE_NAME,
    OutlineClient,
    _document_to_row,
    _render_document_content,
    outline_source,
)


def test_render_document_content_and_metadata():
    """Verify Outline native markdown text and metadata attributes are preserved."""
    doc = {
        "id": "doc_abc123",
        "title": "Engineering Runbook",
        "text": "## Deployment Checklist\n\n1. Run migrations\n2. Verify health checks",
        "collectionId": "col_xyz987",
        "createdAt": "2026-10-01T08:00:00Z",
        "updatedAt": "2026-10-02T09:30:00Z",
        "metadata": {
            "tier": "mission-critical",
            "token": "super_secret_token",
        },
    }

    content = _render_document_content(doc)
    assert "## Deployment Checklist" in content
    assert "1. Run migrations" in content
    assert "collectionId: col_xyz987" in content
    assert "createdAt: 2026-10-01T08:00:00Z" in content
    assert "tier" in content
    # Sensitive field should be redacted
    assert "super_secret_token" not in content


def test_document_to_row_structure():
    """Verify standardized Cognee document row structure."""
    doc = {
        "id": "doc_456",
        "title": "Architecture Design",
        "text": "System architecture overview and service map.",
        "url": "https://app.getoutline.com/doc/architecture-design-doc_456",
    }
    row = _document_to_row(
        base_url="https://app.getoutline.com/api",
        doc=doc,
    )

    assert row["id"] == "outline:doc:doc_456"
    assert row["url"] == "https://app.getoutline.com/doc/architecture-design-doc_456"
    assert row["title"] == "Architecture Design"
    assert "System architecture overview and service map." in row["content"]


def test_client_pagination_streaming():
    """Verify offset and limit pagination over Outline document listings."""
    mock_http = MagicMock()

    page1_docs = [{"id": f"doc_{i}", "title": f"Doc {i}", "text": "Body"} for i in range(1, 3)]
    page2_docs = [{"id": f"doc_{i}", "title": f"Doc {i}", "text": "Body"} for i in range(3, 5)]

    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {
        "data": page1_docs,
        "pagination": {"limit": 2, "offset": 0, "nextPath": "/api/documents.list?offset=2"},
    }

    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {
        "data": page2_docs,
        "pagination": {"limit": 2, "offset": 2, "nextPath": None},
    }

    mock_http.post.side_effect = [resp1, resp2]

    client = OutlineClient(
        base_url="https://app.getoutline.com/api",
        api_token="test_token",
        http_client=mock_http,
    )

    docs = list(client.iter_documents(collection_id="col_1"))
    assert len(docs) == 4
    assert docs[0]["id"] == "doc_1"
    assert docs[3]["id"] == "doc_4"
    assert mock_http.post.call_count == 2


def test_client_retry_on_rate_limit_and_transient_error(monkeypatch):
    """Verify retry logic on HTTP 429 with Retry-After header."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    mock_http = MagicMock()

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "2"}
    http_err = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    resp_ok = MagicMock()
    resp_ok.raise_for_status.return_value = None
    resp_ok.json.return_value = {
        "data": [{"id": "doc_retry", "title": "Recovered", "text": "Content"}],
        "pagination": {"nextPath": None},
    }

    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = http_err

    mock_http.post.side_effect = [resp_fail, resp_ok]

    client = OutlineClient(
        base_url="https://app.getoutline.com/api",
        api_token="test_token",
        http_client=mock_http,
    )

    docs = list(client.iter_documents())
    assert len(docs) == 1
    assert docs[0]["id"] == "doc_retry"
    assert mock_http.post.call_count == 2


def test_client_close_resource():
    """Verify close() cleans up owned HTTP client connections."""
    client = OutlineClient(base_url="https://app.getoutline.com/api")
    assert hasattr(client.client, "close")
    client.close()


def test_outline_source_snapshot_and_cleanup():
    """Verify outline_source sets DOCUMENT_SOURCE_ATTR and yields formatted rows."""
    mock_client = MagicMock()
    mock_client.base_url = "https://app.getoutline.com/api"
    mock_client.iter_documents.return_value = iter(
        [
            {"id": "doc_1", "title": "First", "text": "Hello world"},
            {"id": "doc_2", "title": "Second", "text": "Another document"},
        ]
    )

    source = outline_source(
        collection_ids=["col_1"],
        client=mock_client,
    )

    assert getattr(source, "cognee_document_source", None) == OUTLINE_SOURCE_NAME

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())
    assert len(rows) == 2
    assert rows[0]["id"] == "outline:doc:doc_1"
    assert rows[1]["id"] == "outline:doc:doc_2"


def test_outline_source_missing_token_raises():
    """Verify ValueError when API token is missing."""
    with pytest.raises(ValueError, match="Outline API token required"):
        outline_source(
            base_url="https://app.getoutline.com/api",
            api_token=None,
        )


def test_sensitive_field_sanitization_in_custom_metadata():
    """Verify nested dictionaries have sensitive fields removed."""
    doc = {
        "id": "doc_sec",
        "title": "Secret Meta Doc",
        "text": "Some text",
        "metadata": {
            "department": "Security",
            "keys": {
                "token": "secret_token_val",
                "password": "super_secret_password",
                "valid_key": "safe_val",
            },
        },
    }
    content = _render_document_content(doc)
    assert "Security" in content
    assert "safe_val" in content
    assert "secret_token_val" not in content
    assert "super_secret_password" not in content
