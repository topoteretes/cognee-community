"""Tests for Todoist render and reconciliation transforms."""

import pytest
from todoist_api_python.models import Task, Project, Comment

from cognee_community_connector_todoist.render import render_task, render_project
from cognee_community_connector_todoist.reconcile import (
    filter_volatile_fields,
    TASK_VOLATILE_FIELDS,
    PROJECT_VOLATILE_FIELDS,
    COMMENT_VOLATILE_FIELDS,
)


class TestRenderTask:
    """Tests for task → Markdown rendering."""

    def test_basic_task_renders_title_and_content(self):
        task = Task(
            id="123",
            content="Buy groceries",
            description="Milk, eggs, bread",
            checked=False,
        )

        markdown = render_task(task)

        assert "# Buy groceries" in markdown
        assert "Milk, eggs, bread" in markdown
        assert "View in Todoist" in markdown

    def test_completed_task_shows_checkmark(self):
        task = Task(
            id="123",
            content="Finished task",
            description="",
            checked=True,
        )

        markdown = render_task(task)

        assert "✅" in markdown
        assert "# ✅ Finished task" in markdown

    def test_task_with_priority_and_due_shows_metadata(self):
        task = Task(
            id="123",
            content="Important task",
            description="",
            checked=False,
            priority=4,
            due="2026-12-01",
            labels=["work", "urgent"],
            project_id="456",
        )

        markdown = render_task(task)

        assert "Priority: 4" in markdown
        assert "Due: 2026-12-01" in markdown
        assert "Labels: work, urgent" in markdown
        assert "Project ID: 456" in markdown

    def test_task_without_optional_fields_renders_cleanly(self):
        task = Task(
            id="123",
            content="Simple task",
            description="",
            checked=False,
            priority=1,
            due=None,
            labels=[],
            project_id=None,
        )

        markdown = render_task(task)

        assert "# Simple task" in markdown
        assert "Priority:" not in markdown  # Priority 1 = default, not shown
        assert "Due:" not in markdown
        assert "Labels:" not in markdown
        assert "Project ID:" not in markdown


class TestRenderProject:
    """Tests for project → Markdown rendering."""

    def test_basic_project_renders_name(self):
        project = Project(
            id="456",
            name="Personal",
            comment_count=0,
            is_favorite=False,
        )

        markdown = render_project(project)

        assert "# Project: Personal" in markdown
        assert "View in Todoist" in markdown

    def test_favorite_project_shows_star(self):
        project = Project(
            id="456",
            name="Work",
            comment_count=10,
            is_favorite=True,
        )

        markdown = render_project(project)

        assert "★ Favorite" in markdown
        assert "Comments: 10" in markdown


class TestFilterVolatileFields:
    """Tests for volatile field filtering (stable content hash)."""

    def test_task_volatile_fields_removed(self):
        task_dict = {
            "id": "123",
            "content": "Task",
            "child_order": 1,
            "day_order": 2,
            "order_key": "abc",
            "completed_count": 0,
            "added_at": "2026-01-01",
            "added_by_uid": "user1",
            "stable_field": "keep",
        }

        filtered = filter_volatile_fields(task_dict, TASK_VOLATILE_FIELDS)

        assert "child_order" not in filtered
        assert "day_order" not in filtered
        assert "order_key" not in filtered
        assert "completed_count" not in filtered
        assert "added_at" not in filtered
        assert "added_by_uid" not in filtered
        assert "stable_field" in filtered
        assert "content" in filtered

    def test_project_volatile_fields_removed(self):
        project_dict = {
            "id": "456",
            "name": "Project",
            "child_order": 1,
            "color": "blue",
            "view_style": "list",
            "stable_field": "keep",
        }

        filtered = filter_volatile_fields(project_dict, PROJECT_VOLATILE_FIELDS)

        assert "child_order" not in filtered
        assert "color" not in filtered
        assert "view_style" not in filtered
        assert "stable_field" in filtered
        assert "name" in filtered

    def test_comment_volatile_fields_removed(self):
        comment_dict = {
            "id": "789",
            "content": "Comment",
            "posted_at": "2026-01-01",
            "posted_by_uid": "user1",
            "stable_field": "keep",
        }

        filtered = filter_volatile_fields(comment_dict, COMMENT_VOLATILE_FIELDS)

        assert "posted_at" not in filtered
        assert "posted_by_uid" not in filtered
        assert "stable_field" in filtered
        assert "content" in filtered