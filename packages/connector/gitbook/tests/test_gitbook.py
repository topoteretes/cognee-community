from unittest.mock import Mock, patch

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_gitbook.gitbook import (
    GITBOOK_SOURCE_NAME,
    _flatten_document,
    _page_to_row,
    gitbook_source,
)


class MockResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


def test_gitbook_source_marks_documents():
    source = gitbook_source("test-token", "test-space")

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == GITBOOK_SOURCE_NAME
def test_flatten_document():
    nodes = [
        {
            "type": "paragraph",
            "nodes": [
                {
                    "object": "text",
                    "leaves": [{"text": "Hello GitBook"}],
                }
            ],
        },
        {
            "type": "heading-2",
            "nodes": [
                {
                    "object": "text",
                    "leaves": [{"text": "Introduction"}],
                }
            ],
        },
        {
            "type": "list-unordered",
            "nodes": [
                {
                    "type": "list-item",
                    "nodes": [
                        {
                            "type": "paragraph",
                            "nodes": [
                                {
                                    "object": "text",
                                    "leaves": [{"text": "First point"}],
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "list-item",
                    "nodes": [
                        {
                            "type": "paragraph",
                            "nodes": [
                                {
                                    "object": "text",
                                    "leaves": [{"text": "Second point"}],
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    ]

    result = _flatten_document(nodes)

    assert result == (
        "Hello GitBook\n\n"
        "## Introduction\n\n"
        "- First point\n"
        "- Second point"
    )



def test_page_to_row():
    page = {
        "id": "page-123",
        "title": "Getting Started",
        "urls": {
            "app": "https://app.gitbook.com/s/test/getting-started",
        },
        "path": "getting-started",
        "createdAt": "2026-09-05T07:00:00Z",
        "updatedAt": "2026-09-05T08:00:00Z",
        "document": {
            "nodes": [
                {
                    "type": "paragraph",
                    "nodes": [
                        {
                            "object": "text",
                            "leaves": [{"text": "Welcome to GitBook"}],
                        }
                    ],
                },
                {
                    "type": "heading-2",
                    "nodes": [
                        {
                            "object": "text",
                            "leaves": [{"text": "Installation"}],
                        }
                    ],
                },
            ]
        },
    }

    result = _page_to_row(page)

    assert result["id"] == "page-123"
    assert result["title"] == "Getting Started"
    assert result["url"] == "https://app.gitbook.com/s/test/getting-started"
    assert result["path"] == "getting-started"
    assert result["created_at"] == "2026-09-05T07:00:00Z"
    assert result["updated_at"] == "2026-09-05T08:00:00Z"

    assert result["content"] == (
        "Welcome to GitBook\n\n"
        "## Installation"
    )
def test_flatten_document_nested_list():
    nodes = [
        {
            "type": "list-unordered",
            "nodes": [
                {
                    "type": "list-item",
                    "nodes": [
                        {
                            "type": "paragraph",
                            "nodes": [
                                {
                                    "object": "text",
                                    "leaves": [{"text": "Parent item"}],
                                }
                            ],
                        },
                        {
                            "type": "list-unordered",
                            "nodes": [
                                {
                                    "type": "list-item",
                                    "nodes": [
                                        {
                                            "type": "paragraph",
                                            "nodes": [
                                                {
                                                    "object": "text",
                                                    "leaves": [{"text": "Nested item"}],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    result = _flatten_document(nodes)

    assert result == "- Parent item\n  - Nested item"

def test_gitbook_pages_ingest():
    content_response = Mock()
    content_response.json.return_value = {
        "id": "revision-123",
    }

    pages_response = Mock()
    pages_response.json.return_value = {
        "pages": [
            {
                "id": "page-123",
                "title": "Getting Started",
                "urls": {
                    "app": "https://app.gitbook.com/s/test/getting-started",
                },
                "path": "getting-started",
            }
        ]
    }

    page_response = Mock()
    page_response.json.return_value = {
        "id": "page-123",
        "title": "Getting Started",
        "urls": {
            "app": "https://app.gitbook.com/s/test/getting-started",
        },
        "path": "getting-started",
        "document": {
            "nodes": [
                {
                    "type": "paragraph",
                    "nodes": [
                        {
                            "object": "text",
                            "leaves": [{"text": "Welcome to GitBook"}],
                        }
                    ],
                }
            ]
        },
    }

    with patch(
        "requests.get",
        side_effect=[
            content_response,
            pages_response,
            page_response,
        ],
    ) as mock_get:
        source = gitbook_source("test-token", "test-space")
        rows = list(source)

    assert rows == [
        {
            "id": "page-123",
            "title": "Getting Started",
            "content": "Welcome to GitBook",
            "url": "https://app.gitbook.com/s/test/getting-started",
            "path": "getting-started",
            "created_at": None,
            "updated_at": None,
        }
    ]

    assert mock_get.call_count == 3
def test_gitbook_pages_skips_unchanged_revision(monkeypatch):
    calls = []

    resource_state = {
        "last_revision_id": "revision-123",
    }

    def mock_resource_state():
        return resource_state

    monkeypatch.setattr(
        "dlt.current.resource_state",
        mock_resource_state,
    )

    def mock_get(url, headers):
        calls.append(url)

        if url.endswith("/content"):
            return MockResponse({"id": "revision-123"})

        raise AssertionError(f"Unexpected API call: {url}")

    source = gitbook_source(
        "test-token",
        "test-space",
        request_get=mock_get,
    )

    rows = list(source)

    assert rows == []
    assert calls == [
        "https://api.gitbook.com/v1/spaces/test-space/content",
    ]

def test_gitbook_pages_saves_revision_cursor(monkeypatch):
    resource_state = {}

    def mock_resource_state():
        return resource_state

    monkeypatch.setattr(
        "dlt.current.resource_state",
        mock_resource_state,
    )

    def mock_get(url, headers):
        if url.endswith("/content"):
            return MockResponse({"id": "revision-456"})

        if url.endswith("/revisions/revision-456/pages"):
            return MockResponse({
                "pages": [
                    {"id": "page-1"},
                ]
            })

        if url.endswith("/revisions/revision-456/page/page-1"):
            return MockResponse({
                "id": "page-1",
                "title": "Test Page",
                "document": {
                    "nodes": [],
                },
            })

        raise AssertionError(f"Unexpected API call: {url}")

    source = gitbook_source(
        "test-token",
        "test-space",
        request_get=mock_get,
    )

    rows = list(source)

    assert rows[0]["id"] == "page-1"
    assert resource_state["last_revision_id"] == "revision-456"
    assert resource_state["known_ids"] == ["page-1"]

def test_gitbook_pages_emits_deleted_pages(monkeypatch):
    resource_state = {
        "last_revision_id": "old-revision",
        "known_ids": ["page-1", "page-2"],
    }

    def mock_resource_state():
        return resource_state

    monkeypatch.setattr(
        "dlt.current.resource_state",
        mock_resource_state,
    )

    def mock_get(url, headers):
        if url.endswith("/content"):
            return MockResponse({"id": "new-revision"})

        if url.endswith("/revisions/new-revision/pages"):
            return MockResponse({
                "pages": [
                    {"id": "page-1"},
                ]
            })

        if url.endswith("/revisions/new-revision/page/page-1"):
            return MockResponse({
                "id": "page-1",
                "title": "Existing Page",
                "document": {
                    "nodes": [],
                },
            })

        raise AssertionError(f"Unexpected API call: {url}")

    source = gitbook_source(
        "test-token",
        "test-space",
        request_get=mock_get,
    )

    rows = list(source)

    deleted_rows = [
        row for row in rows
        if row.get("_deleted") is True
    ]

    assert deleted_rows == [
        {
            "id": "page-2",
            "_deleted": True,
        }
    ]
