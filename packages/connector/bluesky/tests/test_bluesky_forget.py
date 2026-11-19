"""Tests for forget-on-delete and tombstone emission."""

from fake_bluesky import FakeBlueskyClient

from cognee_community_connector_bluesky.bluesky import _iter_bluesky_rows


def test_tombstone_emitted_when_post_deleted():
    fake = FakeBlueskyClient()
    state = {}

    # Run 1: two posts
    rows1 = list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state=state,
        )
    )
    assert len(rows1) == 2

    # Delete first post upstream
    fake.authors["bsky.app"].pop(0)

    # Run 2: deleted post is emitted as tombstone
    rows2 = list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state=state,
        )
    )
    assert len(rows2) == 1
    tombstone = rows2[0]
    assert tombstone["_deleted"] is True
    assert "3k12345" in tombstone["id"]


def test_tombstone_emitted_when_actor_deselected():
    fake = FakeBlueskyClient()
    state = {}

    # Run 1: bsky.app
    list(
        _iter_bluesky_rows(
            client=fake,
            actors=["bsky.app"],
            queries=[],
            limit_per_feed=50,
            previous_state=state,
        )
    )

    # Run 2: empty actors, query GraphRAG instead (bsky.app posts deselected)
    rows2 = list(
        _iter_bluesky_rows(
            client=fake,
            actors=[],
            queries=["GraphRAG"],
            limit_per_feed=50,
            previous_state=state,
        )
    )
    # 1 new query post + 2 tombstones for deselected posts
    tombstones = [r for r in rows2 if r["_deleted"]]
    new_posts = [r for r in rows2 if not r["_deleted"]]

    assert len(tombstones) == 2
    assert len(new_posts) == 1
