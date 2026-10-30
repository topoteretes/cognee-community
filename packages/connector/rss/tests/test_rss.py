"""Unit tests for the RSS / Atom dlt connector.

Two layers, all runnable in CI without network access:

* DB-free tests for entry parsing (RSS + Atom), HTML-to-text rendering,
  malformed-feed handling, and the generic document DataItem tagging
  (``source="rss"``) that routes entries through normal cognify.
* dlt-pipeline tests (in-memory feed fixtures, temp sqlite destination) covering
  the acceptance criteria: re-sync reflects edits, and entries dropped from the
  feed fall out of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_rss.rss import (
    RSS_SOURCE_NAME,
    _entry_content,
    _entry_id,
    _entry_to_row,
    _html_to_text,
    _iter_entries,
    _resolve_feed_urls,
)

FEED_URL = "https://example.com/feed.xml"


# ---------------------------------------------------------------------------
# Feed fixtures / fake fetcher
# ---------------------------------------------------------------------------


def _rss_feed(items) -> bytes:
    """Build an RSS 2.0 feed. items: iterable of (guid, title, link, description)."""
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<guid>{guid}</guid><description>{desc}</description></item>"
        for guid, title, link, desc in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Test Feed</title>' + body + "</channel></rss>"
    ).encode()


def _atom_feed(entries) -> bytes:
    """Build an Atom feed. entries: iterable of (id, title, link, content)."""
    body = "".join(
        f'<entry><title>{title}</title><link href="{link}"/>'
        f"<id>{eid}</id><content>{content}</content></entry>"
        for eid, title, link, content in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>' + body + "</feed>"
    ).encode()


class FakeFetcher:
    """Stand-in for the httpx fetcher backed by in-memory feed bytes."""

    def __init__(self, feeds: dict):
        self._feeds = feeds

    def __call__(self, url: str) -> bytes:
        return self._feeds[url]


# ---------------------------------------------------------------------------
# Config resolution (DB-free)
# ---------------------------------------------------------------------------


def test_resolve_feed_urls_accepts_string_list_and_env(monkeypatch):
    assert _resolve_feed_urls("https://a/feed") == ["https://a/feed"]
    assert _resolve_feed_urls(["https://a", "https://b"]) == ["https://a", "https://b"]
    monkeypatch.setenv("RSS_FEED_URLS", "https://a\nhttps://b, https://c")
    assert _resolve_feed_urls(None) == ["https://a", "https://b", "https://c"]


def test_resolve_feed_urls_empty_when_unset(monkeypatch):
    monkeypatch.delenv("RSS_FEED_URLS", raising=False)
    assert _resolve_feed_urls(None) == []
    assert _resolve_feed_urls("") == []


# ---------------------------------------------------------------------------
# Entry parsing / rendering (DB-free)
# ---------------------------------------------------------------------------


def test_entry_id_prefers_guid_then_link():
    assert _entry_id({"id": "g1", "link": "https://x/1"}) == "g1"
    assert _entry_id({"link": "https://x/1"}) == "https://x/1"
    assert _entry_id({}) == ""


def test_html_to_text_strips_tags_and_unescapes():
    out = _html_to_text("<p>Hello &amp; <b>world</b></p><p>line two</p>")
    assert "Hello & world" in out
    assert "line two" in out
    assert "<" not in out


def test_html_to_text_empty():
    assert _html_to_text("") == ""
    assert _html_to_text(None) == ""


def test_entry_content_prefers_atom_content_then_summary():
    atom = {"content": [{"value": "<p>atom body</p>"}], "summary": "ignored"}
    assert _entry_content(atom) == "atom body"
    rss = {"summary": "<p>rss body</p>"}
    assert _entry_content(rss) == "rss body"


def test_entry_to_row_keeps_only_identity_and_text():
    row = _entry_to_row(
        {"id": "g1", "link": "https://x/1", "title": "Post", "summary": "<p>body</p>"}
    )
    assert row == {"id": "g1", "url": "https://x/1", "title": "Post", "content": "body"}


def test_entry_without_id_is_skipped():
    assert _entry_to_row({"title": "no id or link"}) is None


def test_iter_entries_parses_rss():
    fetch = FakeFetcher({FEED_URL: _rss_feed([("g1", "One", "https://x/1", "first")])})
    rows = list(_iter_entries(FEED_URL, fetch))
    assert [r["id"] for r in rows] == ["g1"]
    assert rows[0]["title"] == "One"
    assert "first" in rows[0]["content"]


def test_iter_entries_parses_atom():
    fetch = FakeFetcher({FEED_URL: _atom_feed([("a1", "Atom One", "https://x/a1", "atom body")])})
    rows = list(_iter_entries(FEED_URL, fetch))
    assert [r["id"] for r in rows] == ["a1"]
    assert "atom body" in rows[0]["content"]


def test_iter_entries_malformed_but_parseable_yields_entries():
    # A feed with a stray unclosed tag: feedparser flags bozo but still parses.
    broken = (
        b'<rss version="2.0"><channel><item><title>Ok</title>'
        b"<guid>g1</guid><description>body</description></item></channel></rss>"
    )
    rows = list(_iter_entries(FEED_URL, FakeFetcher({FEED_URL: broken})))
    assert [r["id"] for r in rows] == ["g1"]


def test_iter_entries_empty_garbage_raises():
    # No parseable entries → treat as a failed fetch, do not commit an empty
    # snapshot (which under replace would forget every live entry).
    with pytest.raises(ValueError):
        list(_iter_entries(FEED_URL, FakeFetcher({FEED_URL: b"not xml at all"})))


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "g1",
            "url": "https://x/1",
            "title": "Post",
            "content": "body text",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "g1")

    item = _build_document_data_item(row, data_id, "rss")

    # cognee renamed this field (external_metadata -> system_metadata in 1.6.0);
    # read whichever the installed version exposes so the smoke test tracks the
    # >=1.4.0 pin rather than one exact release.
    meta = getattr(item, "system_metadata", None) or getattr(item, "external_metadata", None)
    # source="rss" (not "dlt") is what routes the entry through normal cognify.
    assert meta["source"] == "rss"
    assert meta["url"] == "https://x/1"
    assert meta["external_id"] == "g1"
    assert item.data_id == data_id
    assert item.data.startswith("# Post")
    assert "body text" in item.data


def test_rss_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_rss.rss import rss_source

    source = rss_source(feed_urls=[FEED_URL], fetcher=FakeFetcher({FEED_URL: _rss_feed([])}))
    assert RSS_SOURCE_NAME == "rss"
    assert document_source_tag(source) == "rss"


def test_rss_source_requires_urls(monkeypatch):
    monkeypatch.delenv("RSS_FEED_URLS", raising=False)
    with pytest.raises(ValueError):
        rss_source_missing = pytest.importorskip("cognee_community_connector_rss.rss").rss_source
        rss_source_missing()


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt + feedparser)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


@pytest.fixture(autouse=True)
def _need_feedparser():
    pytest.importorskip("feedparser")


def _run_sync(dlt, tmp_path, feeds: dict):
    """Run rss_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_rss.rss import rss_source

    db_path = (tmp_path / "rss.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="rss_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="rss_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(rss_source(feed_urls=list(feeds), fetcher=FakeFetcher(feeds)))
    return pipeline


def _read_entries(pipeline):
    """Return {id: row-dict} for the rss_entries table (positional read)."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM rss_entries") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_first_sync_loads_entries(dlt_mod, tmp_path):
    feeds = {
        FEED_URL: _rss_feed(
            [("g1", "One", "https://x/1", "alpha body"), ("g2", "Two", "https://x/2", "beta body")]
        )
    }
    pipeline = _run_sync(dlt_mod, tmp_path, feeds)

    rows = _read_entries(pipeline)
    assert set(rows) == {"g1", "g2"}
    assert "alpha body" in rows["g1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, {FEED_URL: _rss_feed([("g1", "One", "https://x/1", "v1 body")])})

    pipeline = _run_sync(
        dlt_mod, tmp_path, {FEED_URL: _rss_feed([("g1", "One", "https://x/1", "v2 body")])}
    )

    rows = _read_entries(pipeline)
    assert "v2 body" in rows["g1"]["content"]
    assert "v1 body" not in rows["g1"]["content"]


def test_dropped_entry_is_removed_on_resync(dlt_mod, tmp_path):
    feeds = {
        FEED_URL: _rss_feed([("g1", "One", "https://x/1", "a"), ("g2", "Two", "https://x/2", "b")])
    }
    _run_sync(dlt_mod, tmp_path, feeds)

    # g1 no longer present in the feed → absent from the replace load → orphan
    # cleanup forgets it downstream.
    pipeline = _run_sync(
        dlt_mod, tmp_path, {FEED_URL: _rss_feed([("g2", "Two", "https://x/2", "b")])}
    )

    rows = _read_entries(pipeline)
    assert "g1" not in rows
    assert "g2" in rows
