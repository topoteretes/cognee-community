"""Unit tests for the Substack dlt connector.

All runnable in CI without network access:

* DB-free tests for feed-URL resolution, HTML->markdown, RSS parsing, the
  paywall/partial flag, retry classification and the document DataItem tagging.
* dlt-pipeline tests (injected feed, temp sqlite destination) covering the
  acceptance criteria: ingest, edit on re-sync, and forget-on-delete when a post
  drops out of the feed.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_substack import substack as mod
from cognee_community_connector_substack.substack import (
    SUBSTACK_SOURCE_NAME,
    _fetch_feed,
    _html_to_markdown,
    _is_transient,
    _parse_feed,
    _retry_after,
    resolve_feed_url,
    substack_source,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _item(guid, title, html, pub="Mon, 01 Jan 2024 10:00:00 GMT", encoded=True):
    body = f"<content:encoded><![CDATA[{html}]]></content:encoded>" if encoded else ""
    return (
        f"<item><title>{title}</title>"
        f"<link>https://example.substack.com/p/{guid}</link>"
        f'<guid isPermaLink="false">{guid}</guid>'
        f"<pubDate>{pub}</pubDate><dc:creator>Jane</dc:creator>"
        f"<description>subtitle</description>{body}</item>"
    )


def _feed(*items):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Example</title>'
        + "".join(items)
        + "</channel></rss>"
    ).encode()


# ---------------------------------------------------------------------------
# Feed URL resolution
# ---------------------------------------------------------------------------


def test_resolve_feed_url_variants():
    expected = "https://platformer.substack.com/feed"
    assert resolve_feed_url("platformer") == expected
    assert resolve_feed_url("platformer.substack.com") == expected
    assert resolve_feed_url("https://platformer.substack.com/feed") == expected
    assert resolve_feed_url("https://platformer.substack.com/") == expected
    assert resolve_feed_url("https://news.example.com") == "https://news.example.com/feed"


def test_resolve_feed_url_rejects_empty():
    with pytest.raises(ValueError):
        resolve_feed_url("   ")


# ---------------------------------------------------------------------------
# HTML -> markdown
# ---------------------------------------------------------------------------


def test_html_to_markdown_basic_structure():
    html = "<h2>Hello</h2><p>World <b>bold</b></p><ul><li>one</li><li>two</li></ul>"
    assert _html_to_markdown(html) == "## Hello\n\nWorld bold\n\n- one\n- two"


def test_html_to_markdown_ordered_list_and_quote():
    html = "<ol><li>a</li><li>b</li></ol><blockquote>quoted</blockquote>"
    assert _html_to_markdown(html) == "1. a\n2. b\n\n> quoted"


def test_html_to_markdown_drops_script_and_style():
    html = "<style>p{color:red}</style><p>keep</p><script>alert(1)</script>"
    assert _html_to_markdown(html) == "keep"


def test_html_to_markdown_empty():
    assert _html_to_markdown("") == ""


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------


def test_parse_feed_builds_rows():
    rows = _parse_feed(_feed(_item("101", "First", "<p>body one</p>")))

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "101"
    assert row["url"] == "https://example.substack.com/p/101"
    assert row["title"] == "First"
    assert row["author"] == "Jane"
    assert row["published"].startswith("2024-01-01T10:00:00")
    assert row["content"] == "body one"
    assert row["is_partial"] is False


def test_parse_feed_dedupes_by_id():
    rows = _parse_feed(_feed(_item("1", "A", "<p>x</p>"), _item("1", "A", "<p>x</p>")))
    assert [r["id"] for r in rows] == ["1"]


def test_parse_feed_flags_paywalled_post_as_partial():
    html = "<p>Teaser.</p><p>This post is for paid subscribers</p>"
    row = _parse_feed(_feed(_item("2", "Paid", html)))[0]

    assert row["is_partial"] is True
    assert "Teaser." in row["content"]
    assert "Preview only" in row["content"]


def test_parse_feed_missing_content_encoded_is_partial():
    row = _parse_feed(_feed(_item("3", "No body", "", encoded=False)))[0]
    assert row["is_partial"] is True
    assert row["content"].endswith("behind a paywall.]_")


def test_parse_feed_rejects_non_xml_and_non_rss():
    with pytest.raises(ValueError):
        _parse_feed(b"<html><body>not a feed")
    with pytest.raises(ValueError):
        _parse_feed(b"<rss version='2.0'></rss>")  # no <channel>


def test_parse_feed_item_without_identity_raises():
    bad = "<item><title>No id</title></item>"
    with pytest.raises(ValueError):
        _parse_feed(_feed(bad))


# ---------------------------------------------------------------------------
# DataItem tagging
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "101",
            "url": "https://example.substack.com/p/101",
            "title": "First",
            "content": "body one",
        },
        content_hash="abc123",
        table_name="substack_posts",
    )
    data_id = uuid5(NAMESPACE_OID, "101")

    item = _build_document_data_item(row, data_id, "substack")

    assert item.system_metadata["source"] == "substack"
    assert item.system_metadata["url"] == "https://example.substack.com/p/101"
    assert item.system_metadata["external_id"] == "101"
    assert item.data_id == data_id
    assert item.data.startswith("# First")
    assert "body one" in item.data


def test_substack_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    # No network at construction time: the feed is only fetched when the resource runs.
    source = substack_source("example", fetch=lambda url: _feed())
    assert SUBSTACK_SOURCE_NAME == "substack"
    assert document_source_tag(source) == "substack"


# ---------------------------------------------------------------------------
# HTTP retry / error handling
# ---------------------------------------------------------------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_error_classification():
    request = httpx.Request("GET", "https://x/feed")

    def status_error(code):
        return httpx.HTTPStatusError("e", request=request, response=httpx.Response(code))

    assert _is_transient(httpx.ReadTimeout("t")) is True
    assert _is_transient(status_error(429)) is True
    assert _is_transient(status_error(503)) is True
    assert _is_transient(status_error(404)) is False
    assert _is_transient(ValueError()) is False


def test_retry_after_prefers_header():
    assert _retry_after({"Retry-After": "7"}, attempt=3) == 7.0
    assert _retry_after({}, attempt=3) == 8.0
    assert _retry_after(None, attempt=0) == 1.0


def test_fetch_retries_rate_limit_then_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr(mod.time, "sleep", slept.append)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, content=b"<ok/>")

    assert _fetch_feed("https://x/feed", client=_client(handler)) == b"<ok/>"
    assert calls["n"] == 2
    assert slept == [3.0]


def test_fetch_does_not_retry_permanent_errors(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        _fetch_feed("https://x/feed", client=_client(handler))
    assert calls["n"] == 1


def test_fetch_gives_up_after_max_retries(monkeypatch):
    slept = []
    monkeypatch.setattr(mod.time, "sleep", slept.append)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _fetch_feed("https://x/feed", client=_client(handler))
    assert calls["n"] == mod._MAX_RETRIES
    assert len(slept) == mod._MAX_RETRIES - 1


# ---------------------------------------------------------------------------
# dlt pipeline: ingest + full-snapshot forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, feed_bytes):
    """Run substack_source through a dlt pipeline into a temp sqlite destination."""
    db_path = (tmp_path / "substack.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="substack_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="substack_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(substack_source("example", fetch=lambda url: feed_bytes))
    return pipeline


def _read_posts(pipeline):
    """Return {id: row-dict} for the substack_posts table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT id, title, content, is_partial FROM substack_posts"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {
        r[0]: {"id": r[0], "title": r[1], "content": r[2], "is_partial": bool(r[3])}
        for r in rows
    }


def test_first_sync_loads_posts(dlt_mod, tmp_path):
    feed = _feed(_item("1", "Alpha", "<p>alpha body</p>"), _item("2", "Beta", "<p>beta</p>"))

    rows = _read_posts(_run_sync(dlt_mod, tmp_path, feed))

    assert set(rows) == {"1", "2"}
    assert rows["1"]["title"] == "Alpha"
    assert "alpha body" in rows["1"]["content"]
    assert rows["1"]["is_partial"] is False


def test_paywalled_post_is_ingested_but_flagged(dlt_mod, tmp_path):
    feed = _feed(_item("9", "Paid", "<p>Teaser</p><p>For paid subscribers only</p>"))

    rows = _read_posts(_run_sync(dlt_mod, tmp_path, feed))

    assert rows["9"]["is_partial"] is True
    assert "Teaser" in rows["9"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, _feed(_item("1", "Alpha", "<p>v1</p>")))

    pipeline = _run_sync(dlt_mod, tmp_path, _feed(_item("1", "Alpha", "<p>v2</p>")))

    rows = _read_posts(pipeline)
    assert "v2" in rows["1"]["content"]
    assert "v1" not in rows["1"]["content"]


def test_deleted_post_is_removed_on_resync(dlt_mod, tmp_path):
    both = _feed(_item("1", "Alpha", "<p>a</p>"), _item("2", "Beta", "<p>b</p>"))
    _run_sync(dlt_mod, tmp_path, both)

    # Post 1 unpublished upstream: it just vanishes from the feed.
    pipeline = _run_sync(dlt_mod, tmp_path, _feed(_item("2", "Beta", "<p>b</p>")))

    rows = _read_posts(pipeline)
    # Absent from staging -> cognee's orphan cleanup forgets it downstream.
    assert "1" not in rows
    assert "2" in rows


def test_new_post_is_picked_up_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, _feed(_item("1", "Alpha", "<p>a</p>")))

    pipeline = _run_sync(
        dlt_mod,
        tmp_path,
        _feed(_item("2", "Newer", "<p>n</p>", pub="Tue, 02 Jan 2024 10:00:00 GMT"),
              _item("1", "Alpha", "<p>a</p>")),
    )

    assert set(_read_posts(pipeline)) == {"1", "2"}


# ---------------------------------------------------------------------------
# Safety: a failed/empty fetch must not wipe memory under replace
# ---------------------------------------------------------------------------


def test_fetch_failure_aborts_and_keeps_previous_snapshot(dlt_mod, tmp_path):
    pipeline = _run_sync(dlt_mod, tmp_path, _feed(_item("1", "Alpha", "<p>a</p>")))

    def boom(url):
        raise httpx.ConnectError("network down")

    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        pipeline.run(substack_source("example", fetch=boom))

    assert set(_read_posts(pipeline)) == {"1"}


def test_empty_feed_aborts_instead_of_forgetting_everything(dlt_mod, tmp_path):
    pipeline = _run_sync(dlt_mod, tmp_path, _feed(_item("1", "Alpha", "<p>a</p>")))

    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        pipeline.run(substack_source("example", fetch=lambda url: _feed()))

    assert set(_read_posts(pipeline)) == {"1"}
