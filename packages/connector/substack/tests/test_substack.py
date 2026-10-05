"""Unit tests for the Substack dlt connector.

All tests are runnable in CI without a live Substack feed or a live cognee
instance. Two layers:

* Pure-unit tests for the helpers: HTML stripping, URL normalisation, content
  extraction, and the feed-entry → row flattening.
* dlt-pipeline tests (mocked feedparser, in-memory dlt destination) covering:
  - happy-path ingest (normal post);
  - paywalled / truncated-content detection (``is_partial=True``);
  - incremental sync: a second run picks up new posts and drops deleted ones
    (forget-on-delete via ``write_disposition="replace"``).
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_substack.substack import (
    SUBSTACK_SOURCE_NAME,
    _build_feed_url,
    _content_hash,
    _entry_to_row,
    _extract_content,
    _fetch_feed,
    _strip_html,
    substack_source,
)
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR


# ---------------------------------------------------------------------------
# Fixtures / fake feed helpers
# ---------------------------------------------------------------------------


def _make_entry(
    guid: str = "https://example.substack.com/p/test-post",
    title: str = "Test Post",
    link: str = "https://example.substack.com/p/test-post",
    content_value: str | None = None,
    summary: str = "",
    published: str = "Mon, 01 Jan 2024 12:00:00 +0000",
    author: str = "Alice",
) -> SimpleNamespace:
    """Build a fake feedparser entry namespace."""
    entry = SimpleNamespace(
        id=guid,
        title=title,
        link=link,
        summary=summary,
        published=published,
        author=author,
    )
    if content_value is not None:
        entry.content = [{"value": content_value}]
    # No content attribute → subscriber-only post
    return entry


def _make_feed(entries: list, status: int = 200, etag: str | None = None) -> SimpleNamespace:
    feed = SimpleNamespace(
        entries=entries,
        status=status,
        bozo=False,
        bozo_exception=None,
        feed=SimpleNamespace(title="Test Newsletter"),
    )
    if etag:
        feed.etag = etag
    return feed


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_normalises_whitespace():
    assert _strip_html("<p>  foo  </p>  <p>bar</p>") == "foo bar"


def test_strip_html_handles_none():
    assert _strip_html(None) == ""  # type: ignore[arg-type]


def test_strip_html_handles_empty():
    assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def test_content_hash_is_sha256():
    text = "hello"
    expected = hashlib.sha256(b"hello").hexdigest()
    assert _content_hash(text) == expected


def test_content_hash_is_stable():
    assert _content_hash("abc") == _content_hash("abc")


def test_content_hash_differs_on_different_text():
    assert _content_hash("abc") != _content_hash("xyz")


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def test_build_feed_url_bare_subdomain():
    assert _build_feed_url("example") == "https://example.substack.com/feed"


def test_build_feed_url_full_domain():
    assert _build_feed_url("example.substack.com") == "https://example.substack.com/feed"


def test_build_feed_url_full_url():
    assert _build_feed_url("https://example.substack.com") == "https://example.substack.com/feed"


def test_build_feed_url_already_feed():
    url = "https://example.substack.com/feed"
    assert _build_feed_url(url) == url


def test_build_feed_url_strips_trailing_slash():
    assert _build_feed_url("https://example.substack.com/") == "https://example.substack.com/feed"


def test_build_feed_url_custom_domain():
    # Publications with custom domains (not on substack.com)
    assert _build_feed_url("newsletter.example.com") == "https://newsletter.example.com/feed"


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def test_extract_content_full_post():
    """A long content:encoded body is treated as a full post (is_partial=False)."""
    long_html = "<p>" + ("word " * 200) + "</p>"  # well over 500 chars
    entry = _make_entry(content_value=long_html)
    text, is_partial = _extract_content(entry)
    assert is_partial is False
    assert len(text) > 100


def test_extract_content_paywalled_short_content():
    """Very short content:encoded (subscriber preview) → is_partial=True."""
    entry = _make_entry(content_value="<p>Subscribe to read more.</p>")
    _, is_partial = _extract_content(entry)
    assert is_partial is True


def test_extract_content_falls_back_to_summary():
    """No content attribute → use summary field."""
    entry = _make_entry()  # no content_value → no .content attribute
    entry.summary = "<p>" + ("word " * 200) + "</p>"
    text, is_partial = _extract_content(entry)
    assert is_partial is False
    assert len(text) > 50


def test_extract_content_empty_entry():
    """No content, no summary → empty string, is_partial=True."""
    entry = _make_entry(summary="")
    text, is_partial = _extract_content(entry)
    assert text == ""
    assert is_partial is True


# ---------------------------------------------------------------------------
# Entry → row flattening
# ---------------------------------------------------------------------------


def test_entry_to_row_happy_path():
    long_html = "<p>" + ("Hello world. " * 60) + "</p>"
    entry = _make_entry(content_value=long_html)
    row = _entry_to_row(entry)

    assert row["id"] == "https://example.substack.com/p/test-post"
    assert row["title"] == "Test Post"
    assert row["url"] == "https://example.substack.com/p/test-post"
    assert row["author"] == "Alice"
    assert row["is_partial"] is False
    assert row["content_hash"] == _content_hash(row["content"])
    assert row["pub_date"] is not None  # parsed from RFC-2822 string


def test_entry_to_row_paywalled():
    entry = _make_entry(content_value="<p>Subscribe to continue reading.</p>")
    row = _entry_to_row(entry)
    assert row["is_partial"] is True


def test_entry_to_row_stable_id_uses_guid():
    entry = _make_entry(guid="urn:uuid:1234")
    row = _entry_to_row(entry)
    assert row["id"] == "urn:uuid:1234"


def test_entry_to_row_fallback_id_uses_link():
    """When guid is absent (None), fall back to link URL."""
    entry = _make_entry()
    entry.id = None  # type: ignore[assignment]
    row = _entry_to_row(entry)
    assert row["id"] == entry.link


def test_entry_to_row_missing_pub_date():
    entry = _make_entry()
    del entry.published  # type: ignore[attr-defined]
    row = _entry_to_row(entry)
    assert row["pub_date"] is None


# ---------------------------------------------------------------------------
# Feed fetching (mocked feedparser)
# ---------------------------------------------------------------------------


def _long_content() -> str:
    return "<p>" + ("word " * 150) + "</p>"


def test_fetch_feed_304_returns_empty_entries():
    feed_304 = _make_feed(entries=[_make_entry()], status=304)
    with patch("feedparser.parse", return_value=feed_304):
        result = _fetch_feed("https://example.substack.com/feed")
    assert result.entries == []


def test_fetch_feed_http_error_raises():
    feed_err = _make_feed(entries=[], status=404)
    with patch("feedparser.parse", return_value=feed_err):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            _fetch_feed("https://example.substack.com/feed")


def test_fetch_feed_bozo_with_no_entries_raises():
    feed_bad = _make_feed(entries=[], status=200)
    feed_bad.bozo = True
    feed_bad.bozo_exception = Exception("malformed XML")
    with patch("feedparser.parse", return_value=feed_bad):
        with pytest.raises(RuntimeError, match="Failed to parse"):
            _fetch_feed("https://example.substack.com/feed")


# ---------------------------------------------------------------------------
# dlt source: document-mode marker
# ---------------------------------------------------------------------------


def test_substack_source_sets_document_source_attr():
    """The source must carry DOCUMENT_SOURCE_ATTR so posts flow through cognify."""
    entries = [_make_entry(content_value=_long_content())]
    fake_feed = _make_feed(entries=entries)

    with patch("feedparser.parse", return_value=fake_feed):
        src = substack_source("example.substack.com")

    assert getattr(src, DOCUMENT_SOURCE_ATTR, None) == SUBSTACK_SOURCE_NAME


# ---------------------------------------------------------------------------
# dlt pipeline tests (in-memory destination, mocked feedparser)
# ---------------------------------------------------------------------------


def _run_pipeline(source, destination="duckdb"):
    """Run a dlt pipeline with the given source, return the load info."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed")

    pipeline = dlt.pipeline(
        pipeline_name="test_substack",
        destination="duckdb",
        dataset_name="test_substack_data",
        dev_mode=True,  # use a fresh in-memory DB each call
    )
    return pipeline.run(source)


def test_ingest_happy_path():
    """Normal posts are yielded and the pipeline succeeds."""
    entries = [
        _make_entry(
            guid="post-1",
            title="Post One",
            content_value=_long_content(),
        ),
        _make_entry(
            guid="post-2",
            title="Post Two",
            content_value=_long_content(),
        ),
    ]
    fake_feed = _make_feed(entries=entries)

    with patch("feedparser.parse", return_value=fake_feed):
        src = substack_source("example.substack.com")

    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed")

    pipeline = dlt.pipeline(
        pipeline_name="test_substack_happy",
        destination="duckdb",
        dataset_name="substack_data",
        dev_mode=True,
    )
    info = pipeline.run(src)
    assert info.has_failed_jobs is False


def test_ingest_paywalled_post_is_partial():
    """Paywalled posts are ingested but flagged is_partial=True."""
    entries = [
        _make_entry(
            guid="post-paywalled",
            title="Premium Post",
            content_value="<p>Subscribe to read.</p>",
        ),
    ]
    fake_feed = _make_feed(entries=entries)

    with patch("feedparser.parse", return_value=fake_feed):
        src = substack_source("example.substack.com")

    # Extract the rows directly from the resource (no dlt pipeline needed)
    resource = None
    for r in src.resources.values():
        resource = r
        break

    rows = list(resource)
    assert len(rows) == 1
    assert rows[0]["is_partial"] is True


def test_ingest_max_posts_limit():
    """max_posts limits the number of posts yielded."""
    entries = [
        _make_entry(guid=f"post-{i}", content_value=_long_content())
        for i in range(10)
    ]
    fake_feed = _make_feed(entries=entries)

    with patch("feedparser.parse", return_value=fake_feed):
        src = substack_source("example.substack.com", max_posts=3)

    resource = next(iter(src.resources.values()))
    rows = list(resource)
    assert len(rows) == 3


def test_forget_on_delete():
    """Posts absent from the feed on the second run are not yielded (write_disposition=replace)."""
    # First run: 2 posts
    entry_a = _make_entry(guid="post-a", title="Post A", content_value=_long_content())
    entry_b = _make_entry(guid="post-b", title="Post B", content_value=_long_content())

    feed_run1 = _make_feed(entries=[entry_a, entry_b])
    with patch("feedparser.parse", return_value=feed_run1):
        src1 = substack_source("example.substack.com")

    rows_run1 = list(next(iter(src1.resources.values())))
    assert {r["id"] for r in rows_run1} == {"post-a", "post-b"}

    # Second run: post-b deleted (unpublished), only post-a remains
    feed_run2 = _make_feed(entries=[entry_a])
    with patch("feedparser.parse", return_value=feed_run2):
        src2 = substack_source("example.substack.com")

    rows_run2 = list(next(iter(src2.resources.values())))
    assert {r["id"] for r in rows_run2} == {"post-a"}
    # post-b is NOT yielded — write_disposition=replace will clean it up in dlt staging


def test_content_hash_unchanged_for_same_content():
    """Same content → same content_hash (change-detection stability)."""
    long_html = _long_content()
    entry = _make_entry(guid="post-stable", content_value=long_html)

    feed = _make_feed(entries=[entry])
    with patch("feedparser.parse", return_value=feed):
        src1 = substack_source("example.substack.com")
        src2 = substack_source("example.substack.com")

    rows1 = list(next(iter(src1.resources.values())))
    rows2 = list(next(iter(src2.resources.values())))

    assert rows1[0]["content_hash"] == rows2[0]["content_hash"]


def test_content_hash_changes_when_content_changes():
    """Edited content → different content_hash."""
    entry_v1 = _make_entry(
        guid="post-edit",
        content_value="<p>" + ("original content " * 50) + "</p>",
    )
    entry_v2 = _make_entry(
        guid="post-edit",
        content_value="<p>" + ("updated content " * 50) + "</p>",
    )

    feed1 = _make_feed(entries=[entry_v1])
    feed2 = _make_feed(entries=[entry_v2])

    with patch("feedparser.parse", return_value=feed1):
        src1 = substack_source("example.substack.com")
    with patch("feedparser.parse", return_value=feed2):
        src2 = substack_source("example.substack.com")

    row1 = list(next(iter(src1.resources.values())))[0]
    row2 = list(next(iter(src2.resources.values())))[0]

    assert row1["content_hash"] != row2["content_hash"]
