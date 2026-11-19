from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_linear.linear import (
    DOCUMENT_SOURCE_ATTR,
    LINEAR_TABLE_NAME,
    _format_issue_markdown,
    linear_source,
)


@pytest.fixture
def sample_issue():
    return {
        "id": "iss_abc123",
        "identifier": "ENG-404",
        "title": "Resolve distributed database deadlocks on batch writes",
        "description": "Observed intermittent write lock contention during high-volume ingestion.",
        "priority": 1,
        "priorityLabel": "Urgent",
        "url": "https://linear.app/myorg/issue/ENG-404",
        "createdAt": "2026-10-18T10:00:00.000Z",
        "updatedAt": "2026-10-19T14:30:00.000Z",
        "state": {
            "name": "In Progress",
            "type": "started",
        },
        "assignee": {
            "name": "Alex Mercer",
            "email": "alex@company.com",
        },
        "team": {
            "name": "Core Platform",
            "key": "ENG",
        },
        "labels": {
            "nodes": [
                {"name": "database"},
                {"name": "performance"},
            ]
        },
    }


def test_missing_api_key_raises_error():
    """Verify that omitting the Linear API key raises ValueError."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="Linear API key required"),
    ):
        linear_source(api_key=None)


def test_issue_markdown_formatting_deterministic(sample_issue):
    """Verify Markdown rendering is deterministic, structured, and contains all key metadata."""
    md1 = _format_issue_markdown(sample_issue)
    md2 = _format_issue_markdown(sample_issue)

    assert md1 == md2  # Determinism guarantee
    assert "# Linear Issue: [ENG-404] Resolve distributed database deadlocks" in md1
    assert "- **Identifier**: ENG-404" in md1
    assert "- **Status**: In Progress (started)" in md1
    assert "- **Priority**: Urgent (Level 1)" in md1
    assert "- **Team**: Core Platform (ENG)" in md1
    assert "- **Assignee**: Alex Mercer (alex@company.com)" in md1
    assert "- **Labels**: database, performance" in md1
    assert "Observed intermittent write lock contention" in md1


def test_linear_source_extracts_issues(sample_issue):
    """Verify that linear_source runs snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_post(url, headers, json, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "issues": {
                    "pageInfo": {
                        "hasNextPage": False,
                        "endCursor": None,
                    },
                    "nodes": [sample_issue],
                }
            }
        }
        return resp

    mock_client.post.side_effect = mock_post

    source = linear_source(api_key="lin_api_test_secret", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "linear"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == LINEAR_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 1
    assert items[0]["id"] == "iss_abc123"
    assert items[0]["identifier"] == "ENG-404"
    assert items[0]["priority"] == 1
    assert "# Linear Issue: [ENG-404]" in items[0]["body"]
