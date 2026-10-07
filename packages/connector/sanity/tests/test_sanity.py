"""Tests for the Sanity connector (mock-client based)."""

from __future__ import annotations

from typing import Any

import pytest

from cognee_community_connector_sanity import sanity_source


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        key = f"{method} {path}"
        if key not in self.responses:
            raise KeyError(f"No canned response for {key}")
        return self.responses[key]


def test_source_factory_accepts_client_injection() -> None:
    fake = _FakeClient()
    source = sanity_source(
        project_id="test", api_token="test", client=fake
    )
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "sanity"


def test_source_factory_requires_credentials() -> None:
    import os

    os.environ.pop("SANITY_PROJECT_ID", None)
    os.environ.pop("SANITY_API_TOKEN", None)
    with pytest.raises(ValueError, match="Sanity project_id and api_token required"):
        sanity_source(project_id=None, api_token=None, client=None)


def test_doc_to_row_extracts_title_and_text() -> None:
    from cognee_community_connector_sanity.sanity import _doc_to_row

    doc = {
        "_id": "doc-123",
        "_type": "post",
        "_createdAt": "2026-01-01T00:00:00Z",
        "_updatedAt": "2026-01-02T00:00:00Z",
        "_rev": "rev-abc",
        "title": "Hello World",
        "body": [
            {
                "_type": "block",
                "children": [
                    {"_type": "span", "text": "This is a test post body."}
                ],
            }
        ],
    }
    row = _doc_to_row(doc)
    assert row["_id"] == "doc-123"
    assert row["_type"] == "post"
    assert row["title"] == "Hello World"
    assert "Hello World" in row["text"]
    assert "This is a test post body." in row["text"]


def test_extract_portable_text_from_blocks() -> None:
    from cognee_community_connector_sanity.sanity import _extract_portable_text

    value = [
        {
            "_type": "block",
            "children": [{"_type": "span", "text": "Line one"}],
        },
        {
            "_type": "block",
            "children": [{"_type": "span", "text": "Line two"}],
        },
    ]
    text = _extract_portable_text(value)
    assert "Line one" in text
    assert "Line two" in text
