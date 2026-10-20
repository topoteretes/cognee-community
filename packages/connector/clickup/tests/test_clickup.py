from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_clickup.clickup import (
    CLICKUP_TABLE_NAME,
    DOCUMENT_SOURCE_ATTR,
    _format_task_markdown,
    clickup_source,
)


@pytest.fixture
def sample_task():
    return {
        "id": "c101",
        "name": "Implement OAuth2 Token Refresh",
        "status": {"status": "in progress"},
        "priority": {"priority": "high"},
        "url": "https://app.clickup.com/t/c101",
        "assignees": [
            {"username": "dev_alex", "email": "alex@example.com"},
            {"username": "lead_sam", "email": "sam@example.com"},
        ],
        "tags": [{"name": "backend"}, {"name": "security"}],
        "list": {"name": "Sprint Backlog"},
        "space": {"id": "sp_99"},
        "description": "Ensure expiring access tokens automatically request a new bearer token.",
        "checklists": [
            {
                "name": "Acceptance Criteria",
                "items": [
                    {"name": "Unit test refresh grant", "resolved": True},
                    {"name": "Handle invalid_grant error", "resolved": False},
                ],
            }
        ],
    }


def test_missing_api_token_raises_error():
    """Verify that omitting the API token raises ValueError."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="ClickUp API token required"):
            clickup_source(api_token=None)


def test_task_markdown_formatting_deterministic(sample_task):
    """Verify Markdown rendering is deterministic, structured, and contains all key metadata."""
    md1 = _format_task_markdown(sample_task)
    md2 = _format_task_markdown(sample_task)

    assert md1 == md2  # Determinism guarantee
    assert "# ClickUp Task: Implement OAuth2 Token Refresh" in md1
    assert "- **Task ID**: c101" in md1
    assert "- **Status**: in progress" in md1
    assert "- **Priority**: high" in md1
    assert "dev_alex" in md1
    assert "lead_sam" in md1
    assert "backend, security" in md1
    assert "[x] Unit test refresh grant" in md1
    assert "[ ] Handle invalid_grant error" in md1
    assert "Ensure expiring access tokens" in md1


def test_clickup_source_extracts_tasks(sample_task):
    """Verify that clickup_source runs full snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_request(method, url, headers, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if "/team" in url and "/task" not in url:
            resp.json.return_value = {"teams": [{"id": "team_1", "name": "Acme Corp"}]}
        elif "/task" in url:
            if params and params.get("page", 0) == 0:
                resp.json.return_value = {"tasks": [sample_task]}
            else:
                resp.json.return_value = {"tasks": []}
        return resp

    mock_client.request.side_effect = mock_request

    source = clickup_source(api_token="test_pk_token", team_id="team_1", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "clickup"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == CLICKUP_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 1
    assert items[0]["id"] == "c101"
    assert items[0]["name"] == "Implement OAuth2 Token Refresh"
    assert "# ClickUp Task:" in items[0]["body"]
