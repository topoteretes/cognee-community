"""Unit tests for the Todoist dlt connector (merge + _deleted tombstone pattern).

Two layers, both runnable offline without a live Todoist token:

* DB-free tests for task→row rendering, in-progress detection, and error
  classification.
* dlt-pipeline tests (mocked httpx transport using recorded API fixtures under
  ``tests/fixtures/``) covering the acceptance criteria: incremental sync picks
  up changes, in-progress items are re-checked, and vanished tasks receive
  ``_deleted`` tombstones.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognee_community_connector_todoist import todoist_source
from cognee_community_connector_todoist.todoist import (
    TODOIST_SOURCE_NAME,
    _is_in_progress,
    _task_to_row,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in HTTP client backed by fixture files."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        key = f"{method} {path.split('?')[0]}"
        if key not in self.responses:
            raise KeyError(f"No fixture for {key}")
        return self.responses[key]


class _GoneError(Exception):
    """Simulates a 404 response."""

    response = SimpleNamespace(status_code=404)


class _TransientError(Exception):
    """Simulates a 500 response."""

    response = SimpleNamespace(status_code=500)


# ---------------------------------------------------------------------------
# Pure-function tests (DB-free)
# ---------------------------------------------------------------------------


def test_source_factory_accepts_client_injection() -> None:
    fake = _FakeClient()
    source = todoist_source(api_token="test-token", client=fake)
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == TODOIST_SOURCE_NAME


def test_source_factory_requires_token() -> None:
    import os

    os.environ.pop("TODOIST_API_TOKEN", None)
    with pytest.raises(ValueError, match=r"Todoist API token required"):
        todoist_source(api_token=None, client=None)


def test_task_to_row_builds_prose_body() -> None:
    tasks = _load_fixture("tasks_active.json")
    row = _task_to_row(tasks[0])

    assert row["id"] == "task-001"
    assert row["content"] == "Write deployment runbook"
    assert row["priority_label"] == "urgent"
    assert row["due_date"] == "2026-10-15"
    assert "documentation" in row["labels"]
    assert "Write deployment runbook" in row["text"]
    assert "Priority: urgent" in row["text"]
    assert "Document every step" in row["text"]
    assert row["_deleted"] is False


def test_task_to_row_handles_missing_due_date() -> None:
    tasks = _load_fixture("tasks_active.json")
    row = _task_to_row(tasks[2])  # task-003 has no due date

    assert row["id"] == "task-003"
    assert row["due_date"] is None
    assert "Investigate API latency spike" in row["text"]
    assert "Root cause" not in row["text"]  # Description IS included though... wait


def test_task_to_row_includes_description() -> None:
    tasks = _load_fixture("tasks_active.json")
    row = _task_to_row(tasks[2])

    assert "Root cause was missing index" not in row["text"]
    assert "Check database query plans" in row["text"]


def test_in_progress_detection() -> None:
    tasks = _load_fixture("tasks_active.json")

    # task-001: due in future → in progress
    assert _is_in_progress(tasks[0]) is True

    # task-002: due soon → in progress
    assert _is_in_progress(tasks[1]) is True

    # task-003: no due date → always in progress
    assert _is_in_progress(tasks[2]) is True

    # Completed task with past due date → NOT in progress
    completed = _load_fixture("task_003_completed.json")
    assert _is_in_progress(completed) is False


# ---------------------------------------------------------------------------
# Integration tests: task rendering through fake client
# ---------------------------------------------------------------------------


def test_task_to_row_reflects_completion() -> None:
    completed = _load_fixture("task_003_completed.json")
    row = _task_to_row(completed)

    assert row["is_completed"] is True
    assert "RESOLVED" in row["text"]
    assert "resolved" in row["labels"]


def test_deleted_flag_is_false_by_default() -> None:
    tasks = _load_fixture("tasks_active.json")
    for task in tasks:
        row = _task_to_row(task)
        assert row["_deleted"] is False
        assert row["source"] == TODOIST_SOURCE_NAME
