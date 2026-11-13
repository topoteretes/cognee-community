"""Unit tests for the Reddit data-source connector.

Hermetic tests runnable in CI without live network access or Reddit credentials.
"""

from typing import Any
import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
from cognee_community_connector_reddit.reddit import (
    REDDIT_SOURCE_NAME,
    REDDIT_TABLE_NAME,
    RedditClient,
    _build_post_row,
    _build_post_row_with_comments,
    _render_comment_tree,
    reddit_source,
)


class FakeHttpClient:
    """Mock HTTP client returning predetermined Reddit API responses."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = routes or {}
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, Any] | None = None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        for path_pattern, response_data in self.routes.items():
            if path_pattern in url:
                class MockResponse:
                    status_code = 200
                    headers = {}

                    def json(self):
                        return response_data

                    def raise_for_status(self):
                        pass

                return MockResponse()

        raise RuntimeError(f"Unmocked URL called: {url}")


def test_render_comment_tree_simple_and_nested():
    """Test rendering top-level and nested comment trees with indentations."""
    comments = [
        {
            "kind": "t1",
            "data": {
                "author": "alice",
                "body": "First comment on the post",
                "score": 10,
                "replies": {
                    "data": {
                        "children": [
                            {
                                "kind": "t1",
                                "data": {
                                    "author": "bob",
                                    "body": "Reply from Bob",
                                    "score": 5,
                                    "replies": "",
                                },
                            }
                        ]
                    }
                },
            },
        },
        {
            "kind": "t1",
            "data": {
                "author": "[deleted]",
                "body": "[removed]",
                "score": 0,
                "replies": "",
            },
        },
    ]

    rendered = _render_comment_tree(comments, depth=0, max_depth=3)
    assert "- **u/alice** (10 points):" in rendered
    assert "First comment on the post" in rendered
    assert "  - **u/bob** (5 points):" in rendered
    assert "Reply from Bob" in rendered
    # Removed / deleted comments should be skipped
    assert "[removed]" not in rendered


def test_render_comment_tree_depth_limit():
    """Test recursion cut-off when max_depth is exceeded."""
    nested = {
        "kind": "t1",
        "data": {
            "author": "user3",
            "body": "Too deep",
            "score": 1,
            "replies": "",
        },
    }
    level1 = {
        "kind": "t1",
        "data": {
            "author": "user1",
            "body": "Level 0",
            "score": 1,
            "replies": {"data": {"children": [nested]}},
        },
    }

    # With max_depth=0, nested replies should be omitted
    rendered = _render_comment_tree([level1], depth=0, max_depth=0)
    assert "Level 0" in rendered
    assert "Too deep" not in rendered


def test_build_post_row_with_comments():
    """Test document row formatting for post content and discussion."""
    post_data = {
        "id": "abc1234",
        "title": "Announcing Cognee Reddit Connector",
        "author": "dev_user",
        "subreddit": "Python",
        "selftext": "We just built a native Reddit connector for Cognee!",
        "permalink": "/r/Python/comments/abc1234/announcing/",
        "score": 42,
    }
    comments = [
        {
            "kind": "t1",
            "data": {
                "author": "community_member",
                "body": "Looks awesome! How does the memory sync work?",
                "score": 8,
                "replies": "",
            },
        }
    ]

    row = _build_post_row_with_comments(post_data, comments, max_comment_depth=3)
    assert row["id"] == "reddit_abc1234"
    assert row["title"] == "r/Python: Announcing Cognee Reddit Connector"
    assert "https://www.reddit.com/r/Python/comments/abc1234/announcing/" in row["url"]
    assert "# Announcing Cognee Reddit Connector" in row["content"]
    assert "**Author:** u/dev_user" in row["content"]
    assert "We just built a native Reddit connector" in row["content"]
    assert "## Discussion & Comments" in row["content"]
    assert "u/community_member" in row["content"]


def test_reddit_source_creation_and_attributes():
    """Test that reddit_source generates properly tagged DLT source and resource."""
    fake_post = {
        "id": "xyz987",
        "title": "Mock Post",
        "author": "tester",
        "subreddit": "technology",
        "selftext": "Mock body content",
        "permalink": "/r/technology/comments/xyz987/mock/",
        "score": 15,
    }
    routes = {
        "r/technology/hot": {
            "data": {
                "children": [{"kind": "t3", "data": fake_post}],
                "after": None,
            }
        },
        "r/technology/comments/xyz987/mock": [
            {"data": {"children": [{"kind": "t3", "data": fake_post}]}},
            {"data": {"children": []}},
        ],
    }

    fake_http = FakeHttpClient(routes)
    client = RedditClient(http_client=fake_http, user_agent="test-agent")

    source = reddit_source(subreddits=["technology"], client=client, limit=1)

    # Validate source configuration
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == REDDIT_SOURCE_NAME
    assert REDDIT_TABLE_NAME in source.resources

    resource = source.resources[REDDIT_TABLE_NAME]
    assert resource.write_disposition == "replace"

    items = list(resource)
    assert len(items) == 1
    assert items[0]["id"] == "reddit_xyz987"
    assert items[0]["title"] == "r/technology: Mock Post"
