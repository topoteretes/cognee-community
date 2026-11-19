"""Unit tests for the OpenProject dlt connector (merge + _deleted tombstone pattern).

DB-free tests for work-package rendering, active-status detection, and error
classification. Run offline without a live OpenProject instance.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognee_community_connector_openproject import openproject_source
from cognee_community_connector_openproject.openproject import (
    OPENPROJECT_SOURCE_NAME,
    _is_active,
    _wp_to_row,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _GoneError(Exception):
    """Simulates a 404 response."""

    response = SimpleNamespace(status_code=404)


class _TransientError(Exception):
    """Simulates a 500 response."""

    response = SimpleNamespace(status_code=500)


def test_source_factory_accepts_client_injection() -> None:
    def fake_client(*_args, **_kwargs):
        return []

    source = openproject_source(
        base_url="https://test.example.com",
        api_key="test-key",
        client=fake_client,
    )
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == OPENPROJECT_SOURCE_NAME


def test_source_factory_requires_url() -> None:
    import os

    os.environ.pop("OPENPROJECT_BASE_URL", None)
    os.environ.pop("OPENPROJECT_API_KEY", None)
    with pytest.raises(ValueError, match=r"base URL required"):
        openproject_source(base_url=None, api_key=None, client=None)


def test_source_factory_requires_key() -> None:
    import os

    os.environ.pop("OPENPROJECT_API_KEY", None)
    with pytest.raises(ValueError, match=r"API key required"):
        openproject_source(base_url="https://test.example.com", api_key=None, client=None)


def test_wp_to_row_builds_prose_body() -> None:
    work_packages = _load_fixture("work_packages_page1.json")
    row = _wp_to_row(work_packages[0])

    assert row["id"] == "1001"
    assert row["subject"] == "Implement authentication middleware"
    assert row["type"] == "Task"
    assert row["status"] == "In progress"
    assert row["priority"] == "High"
    assert row["assignee"] == "Alice Developer"
    assert "JWT tokens" in row["text"]
    assert "role-based access control" in row["text"]
    assert row["_deleted"] is False


def test_wp_to_row_handles_terminal_status() -> None:
    work_packages = _load_fixture("work_packages_page1.json")
    row = _wp_to_row(work_packages[2])  # Closed bug

    assert row["id"] == "1003"
    assert row["status"] == "Closed"
    assert row["priority"] == "Urgent"
    assert "login page CSS" in row["text"]


def test_active_status_detection() -> None:
    work_packages = _load_fixture("work_packages_page1.json")

    # "In progress" → active
    assert _is_active(work_packages[0]) is True

    # "New" → active
    assert _is_active(work_packages[1]) is True

    # "Closed" → NOT active
    assert _is_active(work_packages[2]) is False


def test_deleted_flag_is_false_by_default() -> None:
    work_packages = _load_fixture("work_packages_page1.json")
    for wp in work_packages:
        row = _wp_to_row(wp)
        assert row["_deleted"] is False
        assert row["source"] == OPENPROJECT_SOURCE_NAME
