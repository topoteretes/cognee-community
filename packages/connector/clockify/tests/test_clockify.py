import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_clockify import (
    fetch_clockify_workspaces,
    fetch_clockify_projects,
    fetch_clockify_time_entries,
    format_time_entry_markdown,
    ClockifyConnector
)

MOCK_WORKSPACES = [
    {"id": "ws_123", "name": "Antigravity Engineering Workspace"}
]

MOCK_PROJECTS = [
    {"id": "proj_abc", "name": "Cognee Memory Engine", "workspaceId": "ws_123"}
]

MOCK_TIME_ENTRIES = [
    {
        "id": "entry_999",
        "description": "Implemented Graph RAG semantic indexing module",
        "projectId": "proj_abc",
        "billable": True,
        "timeInterval": {
            "start": "2026-10-26T09:00:00Z",
            "end": "2026-10-26T12:30:00Z",
            "duration": "PT3H30M"
        },
        "tags": [{"name": "AI"}, {"name": "Architecture"}]
    }
]

def test_format_time_entry_markdown():
    entry = MOCK_TIME_ENTRIES[0]
    markdown = format_time_entry_markdown(entry, project_name="Cognee Memory Engine")

    assert "# Time Log: Implemented Graph RAG semantic indexing module" in markdown
    assert "**Entry ID:** entry_999" in markdown
    assert "**Project:** Cognee Memory Engine" in markdown
    assert "**Billable:** Yes" in markdown
    assert "PT3H30M" in markdown
    assert "AI, Architecture" in markdown

@patch("requests.Session.get")
def test_fetch_clockify_workspaces(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_WORKSPACES
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    workspaces = fetch_clockify_workspaces("fake_api_key")
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == "ws_123"

@patch("requests.Session.get")
def test_fetch_clockify_projects(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_PROJECTS
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    projects = fetch_clockify_projects("fake_api_key", "ws_123")
    assert len(projects) == 1
    assert projects[0]["name"] == "Cognee Memory Engine"

@patch("requests.Session.get")
def test_clockify_connector_fetch_documents(mock_get):
    # Setup mock to return workspaces, projects, time entries in order
    resp_ws = MagicMock()
    resp_ws.json.return_value = MOCK_WORKSPACES
    resp_ws.raise_for_status = MagicMock()

    resp_proj = MagicMock()
    resp_proj.json.return_value = MOCK_PROJECTS
    resp_proj.raise_for_status = MagicMock()

    resp_entries = MagicMock()
    resp_entries.json.return_value = MOCK_TIME_ENTRIES
    resp_entries.raise_for_status = MagicMock()

    mock_get.side_effect = [resp_ws, resp_proj, resp_entries]

    connector = ClockifyConnector(api_key="valid_test_key")
    docs = connector.fetch_documents()

    assert len(docs) == 1
    assert docs[0]["metadata"]["source"] == "clockify"
    assert docs[0]["metadata"]["project_name"] == "Cognee Memory Engine"
    assert docs[0]["metadata"]["billable"] is True
    assert len(docs[0]["id"]) == 64
