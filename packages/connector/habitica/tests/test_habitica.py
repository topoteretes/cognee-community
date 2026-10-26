import pytest
from unittest.mock import MagicMock
from cognee_community_connector_habitica.habitica import (
    habitica_source,
    fetch_habitica_data,
    format_habitica_task_markdown,
    HABITICA_API_URL
)

MOCK_TASKS = [
    {
        "id": "task-uuid-1",
        "text": "Morning Yoga",
        "notes": "20 minutes vinyasa flow",
        "type": "habit",
        "value": 3.5,
        "priority": 1.5,
        "up": True,
        "down": False,
        "counterUp": 14,
        "counterDown": 0,
        "tags": ["fitness", "morning"]
    },
    {
        "id": "task-uuid-2",
        "text": "Review Pull Requests",
        "notes": "Review open PRs in Cognee Community",
        "type": "daily",
        "completed": True,
        "streak": 5,
        "priority": 2,
        "checklist": [
            {"id": "c1", "text": "Check CI builds", "completed": True},
            {"id": "c2", "text": "Approve connectors", "completed": True}
        ]
    }
]

def test_missing_credentials_raises():
    with pytest.raises(ValueError, match="Habitica user_id and api_token must be provided"):
        habitica_source(user_id="", api_token="")

def test_format_habit_markdown():
    task = MOCK_TASKS[0]
    md = format_habitica_task_markdown(task)
    assert "# Morning Yoga" in md
    assert "- **Task ID**: `task-uuid-1`" in md
    assert "- **Type**: `habit`" in md
    assert "20 minutes vinyasa flow" in md
    assert "- **Positive Streak**: 14" in md
    assert "- **Tags**: fitness, morning" in md

def test_format_daily_task_with_checklist():
    task = MOCK_TASKS[1]
    md = format_habitica_task_markdown(task)
    assert "# Review Pull Requests" in md
    assert "- **Status**: Completed" in md
    assert "- **Current Streak**: 5 days" in md
    assert "[x] Check CI builds" in md
    assert "[x] Approve connectors" in md

def test_fetch_habitica_data_mocked():
    session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": True, "data": MOCK_TASKS}
    session.get.return_value = mock_resp

    data = fetch_habitica_data(user_id="user-123", api_token="tok-456", session=session)
    assert len(data) == 2
    assert data[0]["text"] == "Morning Yoga"

def test_habitica_source_documents():
    session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": True, "data": MOCK_TASKS}
    session.get.return_value = mock_resp

    docs = habitica_source(user_id="user-123", api_token="tok-456", session=session)
    assert len(docs) == 2
    doc1 = docs[0]
    assert doc1["title"] == "Habitica Task: Morning Yoga"
    assert doc1["metadata"]["source"] == "habitica"
    assert doc1["metadata"]["task_id"] == "task-uuid-1"
    assert len(doc1["id"]) == 64  # SHA-256 hash length
