"""Tests for the Discourse connector (mock-client based)."""

from __future__ import annotations

from typing import Any

import pytest

from cognee_community_connector_discourse import discourse_source


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
    source = discourse_source(
        base_url="https://forum.example.com",
        client=fake,
    )
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "discourse"


def test_source_factory_requires_base_url() -> None:
    import os

    os.environ.pop("DISCOURSE_BASE_URL", None)
    with pytest.raises(ValueError, match=r"Discourse base URL required"):
        discourse_source(base_url=None, client=None)


def test_topic_to_row_builds_prose_body() -> None:
    from cognee_community_connector_discourse.discourse import _topic_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        return {"markdown": "# Welcome\n\nThis is a forum post.\n\nThanks!"}

    topic = {
        "id": 42,
        "title": "How to configure the plugin",
        "slug": "how-to-configure-plugin",
        "category_id": 3,
        "tags": ["help", "configuration"],
        "created_at": "2026-01-15T10:00:00Z",
        "bumped_at": "2026-01-16T14:30:00Z",
        "posts_count": 5,
        "views": 128,
        "like_count": 3,
        "posters": [{"user": {"username": "forum_admin"}}],
    }

    row = _topic_to_row(fake_client, topic)
    assert row["id"] == 42
    assert row["title"] == "How to configure the plugin"
    assert "help, configuration" in row["text"]
    assert "# Welcome" in row["text"]
    assert "This is a forum post." in row["text"]
    assert "Posted by: forum_admin" in row["text"]


def test_topic_to_row_handles_missing_raw() -> None:
    from cognee_community_connector_discourse.discourse import _topic_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        raise RuntimeError("raw unavailable")

    topic = {
        "id": 99,
        "title": "Locked thread",
        "slug": "locked",
        "created_at": "2026-01-01T00:00:00Z",
        "bumped_at": "2026-01-01T00:00:00Z",
    }

    row = _topic_to_row(fake_client, topic)
    assert row["id"] == 99
    assert "Locked thread" in row["text"]
    # Should still produce a valid row even without raw content
    assert row["text"].strip() != ""
