"""Tests for Todoist DLT source factory."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from cognee_community_connector_todoist import todoist_source
from cognee_community_connector_todoist.todoist import (
    _tasks_resource,
    _projects_resource,
    _comments_resource,
    DOCUMENT_SOURCE_ATTR,
)
from cognee_community_connector_todoist.client import SyncResult
from todoist_api_python.models import Task, Project, Comment


class TestTodoistSource:
    """Unit tests for the todoist_source factory."""

    def test_source_has_correct_name(self):
        source = todoist_source(api_token="test-token")
        assert source.name == "todoist"

    def test_source_yields_three_resources_by_default(self):
        # Mock the client to avoid network calls
        with patch("cognee_community_connector_todoist.todoist.TodoistSyncClient") as mock_client_class:
            mock_client = Mock()
            mock_client.sync.return_value = SyncResult(
                sync_token="new-token",
                items=[],
                projects=[],
                comments=[],
            )
            mock_client_class.return_value = mock_client

            source = todoist_source(api_token="test-token")
            resources = list(source)

            assert len(resources) == 3
            resource_names = [r.name for r in resources]
            assert "tasks" in resource_names
            assert "projects" in resource_names
            assert "comments" in resource_names

    def test_source_persists_sync_token(self):
        with patch("cognee_community_connector_todoist.todoist.TodoistSyncClient") as mock_client_class:
            mock_client = Mock()
            mock_client.sync.return_value = SyncResult(
                sync_token="persisted-token-123",
                items=[],
                projects=[],
                comments=[],
            )
            mock_client_class.return_value = mock_client

            # Simulate dlt state
            import dlt
            dlt.state.set("sync_token", "old-token")

            source = todoist_source(api_token="test-token")
            list(source)  # consume

            # Verify new token was stored
            assert dlt.state.get("sync_token") == "persisted-token-123"

    def test_source_respects_resource_types_filter(self):
        with patch("cognee_community_connector_todoist.todoist.TodoistSyncClient") as mock_client_class:
            mock_client = Mock()
            mock_client.sync.return_value = SyncResult(
                sync_token="token",
                items=[],
                projects=[],
                comments=[],
            )
            mock_client_class.return_value = mock_client

            source = todoist_source(api_token="test-token", resource_types=["items", "projects"])
            resources = list(source)

            assert len(resources) == 2
            resource_names = [r.name for r in resources]
            assert "tasks" in resource_names
            assert "projects" in resource_names
            assert "comments" not in resource_names


class TestTasksResource:
    """Tests for the tasks resource generator."""

    def test_active_task_yields_document_with_content(self):
        task = Task(
            id="123",
            content="Buy groceries",
            description="Milk, eggs, bread",
            checked=False,
            is_deleted=False,
            project_id="456",
            priority=4,
            due=None,
            labels=["shopping"],
        )

        result = SyncResult(
            sync_token="token",
            items=[task],
            projects=[],
            comments=[],
        )

        resource = _tasks_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "task:123"
        assert "Buy groceries" in row["content"]
        assert "Milk, eggs, bread" in row["content"]
        assert row["project_id"] == "project:456"
        assert row["source"] == DOCUMENT_SOURCE_ATTR
        assert row["_deleted"] is False

    def test_deleted_task_yields_deleted_marker(self):
        task = Task(
            id="123",
            content="Deleted task",
            description="",
            checked=False,
            is_deleted=True,
            project_id="456",
        )

        result = SyncResult(
            sync_token="token",
            items=[task],
            projects=[],
            comments=[],
        )

        resource = _tasks_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "task:123"
        assert row["_deleted"] is True
        assert "content" not in row  # Deleted rows only need ID + _deleted

    def test_completed_task_retained_not_deleted(self):
        """Completed (checked) tasks should be retained, not marked deleted."""
        task = Task(
            id="123",
            content="Finished task",
            description="",
            checked=True,  # Completed
            is_deleted=False,  # But NOT deleted
            project_id="456",
        )

        result = SyncResult(
            sync_token="token",
            items=[task],
            projects=[],
            comments=[],
        )

        resource = _tasks_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["_deleted"] is False
        assert "Finished task" in row["content"]


class TestProjectsResource:
    """Tests for the projects resource generator."""

    def test_active_project_yields_document(self):
        project = Project(
            id="456",
            name="Personal",
            comment_count=5,
            is_favorite=True,
            color="blue",
            view_style="list",
        )

        result = SyncResult(
            sync_token="token",
            items=[],
            projects=[project],
            comments=[],
        )

        resource = _projects_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "project:456"
        assert "Personal" in row["content"]
        assert row["source"] == DOCUMENT_SOURCE_ATTR
        assert row["_deleted"] is False

    def test_deleted_project_yields_deleted_marker(self):
        project = Project(
            id="456",
            name="Deleted Project",
            is_deleted=True,
        )

        result = SyncResult(
            sync_token="token",
            items=[],
            projects=[project],
            comments=[],
        )

        resource = _projects_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "project:456"
        assert row["_deleted"] is True


class TestCommentsResource:
    """Tests for the comments resource generator."""

    def test_comment_yields_document(self):
        comment = Comment(
            id="789",
            content="This is a comment",
            task_id="123",
            posted_by_uid="user1",
        )

        result = SyncResult(
            sync_token="token",
            items=[],
            projects=[],
            comments=[comment],
        )

        resource = _comments_resource(result)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "comment:789"
        assert row["content"] == "This is a comment"
        assert row["task_id"] == "task:123"
        assert row["source"] == DOCUMENT_SOURCE_ATTR
        assert row["_deleted"] is False