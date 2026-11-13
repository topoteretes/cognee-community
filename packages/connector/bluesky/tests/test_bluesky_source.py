"""Tests for Bluesky data-source row generation and formatting."""

import pytest
from fake_bluesky import FakeBlueskyClient

from cognee_community_connector_bluesky.bluesky import (
    _format_post_content,
    _iter_bluesky_rows,
    bluesky_source,
)


def test_format_post_content():
    content = _format_post_content(
        uri="at://did:plc:123/app.bsky.feed.post/456",
        handle="alice.bsky.social",
        display_name="Alice",
        created_at="2026-10-15T12:00:00Z",
        text="Hello federated world!",
        url="https://bsky.app/profile/alice.bsky.social/post/456",
    )
    assert "# Bluesky Post by Alice (@alice.bsky.social)" in content
    assert "**Posted:** 2026-10-15T12:00:00Z" in content
    assert "https://bsky.app/profile/alice.bsky.social/post/456" in content
    assert "Hello federated world!" in content


def test_iter_rows_author_feed():
    fake = FakeBlueskyClient()
    rows = list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state={},
        )
    )
    assert len(rows) == 2
    row = rows[0]
    assert row["actor_handle"] == "bsky.app"
    assert row["author_name"] == "Bluesky Official"
    assert "bsky:at://did:plc:z72i7" in row["id"]
    assert row["_deleted"] is False


def test_iter_rows_search_query():
    fake = FakeBlueskyClient()
    rows = list(
        _iter_bluesky_rows(
            client=fake,
            actors=[],
            queries=["GraphRAG"],
            limit_per_feed=50,
            previous_state={},
        )
    )
    assert len(rows) == 1
    assert "AI Researcher" in rows[0]["content"]
    assert "GraphRAG" in rows[0]["content"]


def test_iter_rows_skips_unchanged():
    fake = FakeBlueskyClient()
    state = {}
    run1 = list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state=state,
        )
    )
    assert len(run1) == 2

    run2 = list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state=state,
        )
    )
    assert len(run2) == 0


def test_source_rejects_empty_selection():
    with pytest.raises(ValueError):
        bluesky_source(actors=[], queries=[])
