"""Unit tests for the WordPress connector.

Tests run fully mocked without live WordPress credentials or network calls:
- URL normalization (self-hosted vs WordPress.com)
- HTML cleaning and entity unescaping
- Item to cognee document row conversion (posts, pages, comments)
- Transient error classification and retries
- Full initial sync (backfill)
- Incremental sync with modified_after cursor
- Forget-on-delete tombstone emission
- Custom post type ingestion
- Source document marker wiring
"""

import pytest

from cognee_community_connector_wordpress.wordpress import (
    WORDPRESS_SOURCE_NAME,
    _clean_html,
    _is_transient,
    _item_to_row,
    _normalize_api_url,
    sync_wordpress,
    wordpress_source,
)

BASE_URL = "https://example.com"
API_URL = "https://example.com/wp-json/wp/v2"


# ---------------------------------------------------------------------------
# Fakes & Fixtures
# ---------------------------------------------------------------------------
def _post(post_id: int, title: str, content: str, modified: str = "2026-01-01T12:00:00"):
    return {
        "id": post_id,
        "title": {"rendered": title},
        "content": {"rendered": content},
        "excerpt": {"rendered": ""},
        "link": f"https://example.com/p/{post_id}",
        "modified": modified,
        "date": "2026-01-01T10:00:00",
    }


def _comment(
    comment_id: int, post_id: int, author: str, content: str, date: str = "2026-01-01T12:00:00"
):
    return {
        "id": comment_id,
        "post": post_id,
        "author_name": author,
        "content": {"rendered": content},
        "link": f"https://example.com/p/{post_id}#comment-{comment_id}",
        "date": date,
    }


class FakeResponse:
    def __init__(self, data, headers=None, status_code=200):
        self._data = data
        self.headers = headers or {"X-WP-TotalPages": "1"}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

    def json(self):
        return self._data


class FakeWordPressSession:
    def __init__(self, posts=None, pages=None, comments=None, custom=None):
        self.items = {
            "posts": list(posts or []),
            "pages": list(pages or []),
            "comments": list(comments or []),
        }
        if custom:
            self.items.update(custom)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))

        endpoint = url.split("/")[-1]
        all_for_endpoint = self.items.get(endpoint, [])

        # If _fields=id is requested, simulate lightweight ID sweep
        if params.get("_fields") == "id":
            return FakeResponse([{"id": item["id"]} for item in all_for_endpoint])

        # Filter by modified_after or after
        filtered = []
        mod_after = params.get("modified_after")
        after = params.get("after")

        for item in all_for_endpoint:
            if mod_after:
                item_mod = item.get("modified") or item.get("date") or ""
                if item_mod <= mod_after:
                    continue
            if after:
                item_date = item.get("date") or item.get("modified") or ""
                if item_date <= after:
                    continue
            filtered.append(item)

        return FakeResponse(filtered)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------
def test_normalize_api_url():
    assert _normalize_api_url("https://example.com") == "https://example.com/wp-json/wp/v2"
    assert _normalize_api_url("https://example.com/") == "https://example.com/wp-json/wp/v2"
    assert _normalize_api_url("https://example.com/wp-json") == "https://example.com/wp-json/wp/v2"
    assert (
        _normalize_api_url("https://example.com/wp-json/wp/v2")
        == "https://example.com/wp-json/wp/v2"
    )
    assert (
        _normalize_api_url("https://mysite.wordpress.com")
        == "https://public-api.wordpress.com/wp/v2/sites/mysite.wordpress.com"
    )


def test_clean_html():
    raw = "<p>Hello <strong>World</strong> &amp; friends!</p><script>alert(1)</script>"
    cleaned = _clean_html(raw)
    assert cleaned == "Hello World & friends!"
    assert "script" not in cleaned
    assert "alert" not in cleaned
    assert _clean_html("") == ""
    assert _clean_html(None) == ""


def test_item_to_row_post():
    raw_item = _post(42, "My Post &amp; News", "<p>Paragraph one.</p>")
    row = _item_to_row(raw_item, "posts")

    assert row["id"] == "posts:42"
    assert row["title"] == "My Post & News"
    assert row["content"] == "Paragraph one."
    assert row["url"] == "https://example.com/p/42"
    assert row["_deleted"] is False


def test_item_to_row_comment():
    raw_comment = _comment(101, 42, "Alice", "<p>Great post!</p>")
    row = _item_to_row(raw_comment, "comments")

    assert row["id"] == "comments:101"
    assert "Alice" in row["title"]
    assert "42" in row["title"]
    assert row["content"] == "Great post!"
    assert row["_deleted"] is False


def test_is_transient():
    import requests

    resp_500 = FakeResponse({}, status_code=500)
    resp_429 = FakeResponse({}, status_code=429)
    resp_404 = FakeResponse({}, status_code=404)

    assert _is_transient(requests.HTTPError(response=resp_500)) is True
    assert _is_transient(requests.HTTPError(response=resp_429)) is True
    assert _is_transient(requests.HTTPError(response=resp_404)) is False
    assert _is_transient(requests.Timeout()) is True
    assert _is_transient(requests.ConnectionError()) is True
    assert _is_transient(ValueError("error")) is False


def test_full_initial_sync():
    posts = [_post(1, "Post 1", "Body 1", "2026-01-01T12:00:00")]
    pages = [_post(2, "Page 1", "Page Body", "2026-01-02T12:00:00")]
    comments = [_comment(3, 1, "Bob", "Comment text", "2026-01-03T12:00:00")]

    session = FakeWordPressSession(posts=posts, pages=pages, comments=comments)
    state = {}

    rows = list(sync_wordpress(session, API_URL, state, ["posts", "pages", "comments"]))

    assert len(rows) == 3
    ids = {r["id"] for r in rows}
    assert ids == {"posts:1", "pages:2", "comments:3"}
    assert all(r["_deleted"] is False for r in rows)

    # Check updated resource state
    assert set(state["known_ids"]) == {"posts:1", "pages:2", "comments:3"}
    assert state["last_modified"] == "2026-01-03T12:00:00"


def test_incremental_sync_skips_unchanged():
    posts = [
        _post(1, "Post 1", "Body 1", "2026-01-01T12:00:00"),
        _post(2, "Post 2", "Body 2", "2026-01-05T12:00:00"),
    ]
    session = FakeWordPressSession(posts=posts)
    state = {
        "known_ids": ["posts:1"],
        "last_modified": "2026-01-01T12:00:00",
    }

    rows = list(sync_wordpress(session, API_URL, state, ["posts"]))

    # Only Post 2 was modified after 2026-01-01T12:00:00
    assert len(rows) == 1
    assert rows[0]["id"] == "posts:2"
    assert rows[0]["_deleted"] is False
    assert state["last_modified"] == "2026-01-05T12:00:00"
    assert set(state["known_ids"]) == {"posts:1", "posts:2"}


def test_forget_on_delete():
    # Previous run knew posts 1 and 2
    state = {
        "known_ids": ["posts:1", "posts:2"],
        "last_modified": "2026-01-01T12:00:00",
    }

    # Now post 1 is deleted from WordPress; only post 2 remains
    session = FakeWordPressSession(posts=[_post(2, "Post 2", "Body 2", "2026-01-01T12:00:00")])

    rows = list(sync_wordpress(session, API_URL, state, ["posts"]))

    # Post 1 must be emitted as deleted tombstone
    deleted_rows = [r for r in rows if r.get("_deleted") is True]
    assert len(deleted_rows) == 1
    assert deleted_rows[0]["id"] == "posts:1"

    # Known IDs updated
    assert state["known_ids"] == ["posts:2"]


def test_custom_post_type_sync():
    products = [_post(10, "Product A", "A cool widget", "2026-01-04T10:00:00")]
    session = FakeWordPressSession(custom={"products": products})
    state = {}

    rows = list(sync_wordpress(session, API_URL, state, ["products"]))
    assert len(rows) == 1
    assert rows[0]["id"] == "products:10"
    assert rows[0]["title"] == "Product A"
    assert "products:10" in state["known_ids"]


def test_transient_sweep_failure_does_not_purge():
    state = {
        "known_ids": ["posts:1", "posts:2"],
        "last_modified": "2026-01-01T12:00:00",
    }
    # Session returns empty items
    session = FakeWordPressSession(posts=[])

    rows = list(sync_wordpress(session, API_URL, state, ["posts"]))

    # To avoid wiping out the whole corpus on a transient listing failure,
    # no deletions should be yielded and state preserved
    assert len(rows) == 0
    assert state["known_ids"] == ["posts:1", "posts:2"]


def test_wordpress_source_declares_document_marker(monkeypatch):
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = wordpress_source(
        base_url="https://example.com",
        username="admin",
        app_password="pwd",
        session=FakeWordPressSession(),
    )
    assert WORDPRESS_SOURCE_NAME == "wordpress"
    assert document_source_tag(source) == "wordpress"
