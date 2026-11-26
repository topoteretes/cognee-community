"""Pytest configuration and fixtures for Todoist connector tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from todoist_api_python.models import Task, Project, Comment


@pytest.fixture
def mock_task():
    """Create a mock Task object."""
    return Task(
        id="123",
        content="Test task",
        description="Test description",
        checked=False,
        is_deleted=False,
        project_id="456",
        priority=1,
        due=None,
        labels=["test"],
        added_at="2026-01-01T00:00:00Z",
        added_by_uid="user1",
        assigned_by_uid=None,
        user_id="user1",
        child_order=1,
        day_order=1,
        order_key="abc",
        completed_count=0,
        sync_id="sync1",
    )


@pytest.fixture
def mock_project():
    """Create a mock Project object."""
    return Project(
        id="456",
        name="Test Project",
        comment_count=0,
        is_favorite=False,
        color="blue",
        view_style="list",
        parent_id=None,
        child_order=1,
        sync_id="sync1",
    )


@pytest.fixture
def mock_comment():
    """Create a mock Comment object."""
    return Comment(
        id="789",
        content="Test comment",
        task_id="123",
        posted_by_uid="user1",
        posted_at="2026-01-01T00:00:00Z",
        sync_id="sync1",
    )


@pytest.fixture
def mock_todoist_api():
    """Mock the TodoistAPIAsync for integration tests."""
    with pytest.MonkeyPatch().context() as mp:
        mock_api = AsyncMock()
        mp.setattr("cognee_community_connector_todoist.client.TodoistAPIAsync", lambda token: mock_api)
        yield mock_api