"""Tests for the Otter.ai connector (mock-client based)."""

from __future__ import annotations

from typing import Any

import pytest

from cognee_community_connector_otter import otter_source


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        key = f"{method} {path}"
        if key not in self.responses:
            raise KeyError(f"No canned response for {key}")
        return self.responses[key]


def test_source_factory_accepts_client_injection() -> None:
    fake = _FakeClient()
    source = otter_source(api_key="test", client=fake)
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "otter"


def test_source_factory_requires_credentials() -> None:
    import os

    os.environ.pop("OTTER_API_KEY", None)
    with pytest.raises(ValueError, match=r"Otter\.ai API key required"):
        otter_source(api_key=None, client=None)


def test_conv_to_row_builds_prose_body() -> None:
    from cognee_community_connector_otter.otter import _conv_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        return {
            "data": {
                "relationships": {"transcript": {"content": "Sam 00:15\nWelcome to the meeting!\n"}}
            }
        }

    conv = {
        "id": "conv-123",
        "title": "Product Launch Review",
        "url": "https://otter.ai/u/conv-123",
        "created_at": "2026-01-15T10:00:00Z",
        "owner": {"name": "Jane Doe", "email": "jane@example.com"},
        "abstract_summary": "Discussed Q1 launch timeline and risks.",
        "calendar_guests": [{"name": "John"}, {"name": "Alex"}],
    }

    row = _conv_to_row(fake_client, conv)
    assert row["id"] == "conv-123"
    assert row["title"] == "Product Launch Review"
    assert "Welcome to the meeting" in row["text"]
    assert "Q1 launch timeline" in row["text"]
    assert "John, Alex" in row["text"]


def test_conv_to_row_handles_missing_transcript() -> None:
    from cognee_community_connector_otter.otter import _conv_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        raise RuntimeError("transcript unavailable")

    conv = {
        "id": "conv-456",
        "title": "Standup",
        "created_at": "2026-01-15T09:00:00Z",
    }

    row = _conv_to_row(fake_client, conv)
    assert row["id"] == "conv-456"
    assert "Standup" in row["text"]
    # Should still produce a valid row without transcript
    assert row["text"].strip() != ""
