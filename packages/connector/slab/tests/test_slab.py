"""Unit tests for the Slab knowledge base dlt connector."""

from typing import Any

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_slab.slab import (
    DEFAULT_BASE_URL,
    SLAB_SOURCE_NAME,
    SlabAPIError,
    _is_transient,
    _iter_slab_posts,
    _iter_slab_rows,
    _post_to_row,
    slab_source,
)


def test_post_to_row_standard():
    post = {
        "id": "abc-123",
        "title": "Data Pipeline Best Practices",
        "content": "Always validate schemas and handle retries with jitter.",
        "insertedAt": "2026-10-01T10:00:00Z",
        "updatedAt": "2026-10-15T14:30:00Z",
        "owner": {"id": "u1", "name": "Alice Developer"},
        "topics": [{"id": "t1", "name": "Backend"}, {"id": "t2", "name": "Data"}],
        "comments": [
            {
                "id": "c1",
                "content": "Great documentation!",
                "insertedAt": "2026-10-15T16:00:00Z",
                "author": {"id": "u2", "name": "Bob Reviewer"},
            }
        ],
    }

    row = _post_to_row(post)
    assert row["id"] == "slab_post_abc-123"
    assert row["title"] == "Data Pipeline Best Practices"
    assert row["url"] == "https://slab.com/posts/abc-123"
    assert row["updated_at"] == "2026-10-15T14:30:00Z"
    assert row["topics"] == ["Backend", "Data"]
    assert row["author"] == "Alice Developer"

    content = row["content"]
    assert "# Data Pipeline Best Practices" in content
    assert "**Author:** Alice Developer" in content
    assert "**Topics:** Backend, Data" in content
    assert "Always validate schemas and handle retries with jitter." in content
    assert "### Comments" in content
    assert "- **Bob Reviewer** (2026-10-15T16:00:00Z): Great documentation!" in content


def test_post_to_row_missing_topics_and_comments():
    post = {
        "id": "p-clean",
        "title": "Standalone Note",
        "content": "Quick memo regarding release dates.",
        "updatedAt": "2026-10-10T12:00:00Z",
        "owner": {"name": "Charlie"},
        "topics": [],
        "comments": [],
    }

    row = _post_to_row(post)
    assert row["id"] == "slab_post_p-clean"
    assert "**Topics:**" not in row["content"]
    assert "### Comments" not in row["content"]
    assert "Quick memo regarding release dates." in row["content"]


def test_post_to_row_deactivated_comment_author_fallback():
    post = {
        "id": "p-deact",
        "title": "Legacy System Doc",
        "content": "Architecture of legacy services.",
        "owner": None,
        "comments": [
            {
                "id": "c-deact",
                "content": "This cluster was decommissioned.",
                "insertedAt": "2026-09-01T08:00:00Z",
                "author": None,
            }
        ],
    }

    row = _post_to_row(post)
    assert row["author"] == "Team Member"
    assert (
        "- **Team Member** (2026-09-01T08:00:00Z): This cluster was decommissioned."
        in (row["content"])
    )


def test_post_to_row_empty_and_whitespace_comment_filtering():
    post = {
        "id": "p-whitespace",
        "title": "Draft Proposal",
        "content": "Some proposal draft.",
        "comments": [
            {"id": "c1", "content": "   ", "author": {"name": "Dave"}},
            {"id": "c2", "content": "", "author": {"name": "Eve"}},
            {"id": "c3", "content": "Valid feedback.", "author": {"name": "Frank"}},
        ],
    }

    row = _post_to_row(post)
    content = row["content"]
    assert "Valid feedback." in content
    assert "Dave" not in content
    assert "Eve" not in content


def test_post_to_row_empty_content_stub():
    post = {
        "id": "stub-1",
        "title": "Work in Progress",
        "content": None,
        "owner": {"name": "Grace"},
    }

    row = _post_to_row(post)
    assert "# Work in Progress" in row["content"]
    assert "*(No content)*" in row["content"]


def test_post_to_row_embedded_code_blocks_preservation():
    code_content = """Here is the deployment script:

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "Deploying Cognee connector..."
python -m cognee.sync --source slab
```

And inline `CONFIG_PATH` variable."""

    post = {
        "id": "code-1",
        "title": "Deployment Instructions",
        "content": code_content,
        "owner": {"name": "DevOps Lead"},
    }

    row = _post_to_row(post)
    assert "```bash" in row["content"]
    assert "python -m cognee.sync --source slab" in row["content"]
    assert "`CONFIG_PATH`" in row["content"]


def test_iter_slab_posts_organization_pagination(fake_slab, slab_client):
    # Add 3 more posts so there are 5 posts total
    for i in range(1, 4):
        fake_slab.posts.append(
            {
                "id": f"extra-{i}",
                "title": f"Extra Post {i}",
                "content": f"Content for extra post {i}",
                "insertedAt": f"2026-10-0{i}T10:00:00Z",
                "updatedAt": f"2026-10-1{i}T10:00:00Z",
                "owner": {"name": "Tester"},
                "topics": [],
                "comments": [],
            }
        )

    # Page size 2 requires 3 pages
    posts = list(_iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=None, page_size=2))
    assert len(posts) == 5
    assert [p["id"] for p in posts] == ["post-101", "post-102", "extra-1", "extra-2", "extra-3"]


def test_iter_slab_posts_topic_filtering(fake_slab, slab_client):
    posts = list(
        _iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=["topic-eng"], page_size=10)
    )
    assert len(posts) == 1
    assert posts[0]["id"] == "post-101"
    assert posts[0]["title"] == "Architecture Overview"


def test_multi_topic_deduplication(fake_slab, slab_client):
    # Make post-101 belong to both topic-eng and topic-hr
    fake_slab.topics["topic-hr"]["post_ids"].append("post-101")

    posts = list(
        _iter_slab_posts(
            slab_client,
            DEFAULT_BASE_URL,
            topic_ids=["topic-eng", "topic-hr"],
            page_size=10,
        )
    )
    # Post-101 should only be returned once despite being in both topics
    post_ids = [p["id"] for p in posts]
    assert post_ids.count("post-101") == 1
    assert len(posts) == 2


def test_graphql_relay_cursor_exhaustion(fake_slab, slab_client):
    fake_slab.posts = []
    posts = list(_iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=None, page_size=10))
    assert posts == []


def test_graphql_partial_error_recovery(fake_slab, slab_client):
    fake_slab.return_partial_errors = True
    posts = list(_iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=None, page_size=10))
    assert len(posts) == 2
    assert posts[0]["id"] == "post-101"


def test_graphql_fatal_error_raises(fake_slab, slab_client):
    fake_slab.return_fatal_errors = True
    with pytest.raises(SlabAPIError, match="Slab GraphQL execution errors"):
        list(_iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=None))


def test_transient_error_retry_and_recovery(fake_slab, slab_client):
    fake_slab.rate_limit_hits = 1
    fake_slab.server_error_hits = 1

    posts = list(_iter_slab_posts(slab_client, DEFAULT_BASE_URL, topic_ids=None, page_size=10))
    assert len(posts) == 2
    assert fake_slab.rate_limit_hits == 0
    assert fake_slab.server_error_hits == 0


def test_error_classification():
    assert _is_transient(httpx.ReadTimeout("Timeout")) is True
    assert _is_transient(httpx.ConnectError("Connection refused")) is True
    assert _is_transient(ValueError("Generic error")) is False


def test_parameter_validation_missing_token():
    with pytest.raises(ValueError, match="Slab API token is required"):
        slab_source(api_token="", client=None)


def test_incremental_sync_skips_older_posts(fake_slab, slab_client):
    state: dict[str, Any] = {
        "slab_seen_ids": ["slab_post_post-101"],
        "slab_post_cursor": "2026-10-11T00:00:00Z",
    }

    # post-101 updatedAt is 2026-10-10, so it should be skipped
    # post-102 updatedAt is 2026-10-12, so it should be yielded
    rows = list(
        _iter_slab_rows(
            client=slab_client,
            base_url=DEFAULT_BASE_URL,
            incremental=True,
            resource_state=state,
        )
    )

    row_ids = [r["id"] for r in rows if "_deleted" not in r]
    assert row_ids == ["slab_post_post-102"]
    assert state["slab_post_cursor"] == "2026-10-12T11:00:00Z"


def test_forget_on_delete_tombstone_emission(fake_slab, slab_client):
    state: dict[str, Any] = {
        "slab_seen_ids": ["slab_post_post-101", "slab_post_post-102", "slab_post_deleted-post"],
        "slab_post_cursor": "",
    }

    # deleted-post is no longer returned upstream
    rows = list(
        _iter_slab_rows(
            client=slab_client,
            base_url=DEFAULT_BASE_URL,
            incremental=False,
            resource_state=state,
        )
    )

    tombstones = [r for r in rows if r.get("_deleted") is True]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "slab_post_deleted-post"
    assert tombstones[0]["_deleted"] is True
    # State should now reflect only the live posts
    assert state["slab_seen_ids"] == ["slab_post_post-101", "slab_post_post-102"]


def test_slab_source_document_mode_attributes(slab_client):
    resource = slab_source(api_token="test_tok", client=slab_client)
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == SLAB_SOURCE_NAME
    assert resource.cognee_pipeline_scope == SLAB_SOURCE_NAME
