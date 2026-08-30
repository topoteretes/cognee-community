"""Unit tests for the RSS / Atom dlt connector.

Runnable in CI without network: feeds are injected as in-memory feedparser
results. Covers RSS + Atom flattening, since-cursor filtering, document-source
tagging, and full-snapshot forget-on-delete.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_rss.rss import (
    RSS_SOURCE_NAME,
    _entry_to_row,
    _is_unusable,
    rss_source,
)

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <guid>https://example.com/a</guid>
      <title>Alpha</title>
      <link>https://example.com/a</link>
      <description>alpha body</description>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>https://example.com/b</guid>
      <title>Beta</title>
      <link>https://example.com/b</link>
      <description>beta body</description>
      <pubDate>Thu, 01 Feb 2024 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <id>https://example.com/c</id>
    <title>Gamma</title>
    <link href="https://example.com/c"/>
    <updated>2024-03-01T00:00:00Z</updated>
    <summary>gamma body</summary>
  </entry>
</feed>
"""

BAD_XML = "<not-a-feed>"


def _parse(xml: str):
    feedparser = pytest.importorskip("feedparser")
    return feedparser.parse(xml)


def test_entry_to_row_from_rss():
    parsed = _parse(RSS_XML)
    row = _entry_to_row(parsed.entries[0], feed_url="https://example.com/feed.xml")
    assert row is not None
    assert row["id"] == "https://example.com/a"
    assert row["title"] == "Alpha"
    assert row["url"] == "https://example.com/a"
    assert "alpha body" in row["content"]
    assert row["published"].startswith("2024-01-01")
    assert row["feed_url"] == "https://example.com/feed.xml"


def test_entry_to_row_from_atom():
    parsed = _parse(ATOM_XML)
    row = _entry_to_row(parsed.entries[0], feed_url="https://example.com/atom.xml")
    assert row is not None
    assert row["id"] == "https://example.com/c"
    assert row["title"] == "Gamma"
    assert "gamma body" in row["content"]


def test_entry_without_id_is_skipped():
    assert _entry_to_row({"title": "no id"}, feed_url="https://example.com/feed.xml") is None


def test_malformed_feed_is_unusable():
    parsed = _parse(BAD_XML)
    assert _is_unusable(parsed) is True
    assert _is_unusable(_parse(RSS_XML)) is False


def test_rss_source_requires_a_url():
    with pytest.raises(ValueError, match="at least one feed URL"):
        rss_source([])


def test_rss_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = rss_source(["https://example.com/feed.xml"], fetch=lambda url: _parse(RSS_XML))
    assert RSS_SOURCE_NAME == "rss"
    assert document_source_tag(source) == "rss"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "https://example.com/a",
            "url": "https://example.com/a",
            "title": "Alpha",
            "content": "alpha body",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "https://example.com/a")
    item = _build_document_data_item(row, data_id, "rss")
    assert item.external_metadata["source"] == "rss"
    assert item.external_metadata["url"] == "https://example.com/a"
    assert item.external_metadata["external_id"] == "https://example.com/a"
    assert item.data.startswith("# Alpha")
    assert "alpha body" in item.data


def _run_sync(dlt, tmp_path, xml_by_url):
    db_path = (tmp_path / "rss.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="rss_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="rss_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        rss_source(
            xml_by_url.keys(),
            fetch=lambda url: _parse(xml_by_url[url]),
        )
    )
    return pipeline


def _read_entries(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM rss_entries") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_rss_and_atom(dlt_mod, tmp_path):
    pipeline = _run_sync(
        dlt_mod,
        tmp_path,
        {
            "https://example.com/feed.xml": RSS_XML,
            "https://example.com/atom.xml": ATOM_XML,
        },
    )
    rows = _read_entries(pipeline)
    assert set(rows) == {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    }
    assert "alpha body" in rows["https://example.com/a"]["content"]
    assert "gamma body" in rows["https://example.com/c"]["content"]


def test_since_cursor_skips_older_entries(dlt_mod, tmp_path):
    db_path = (tmp_path / "rss.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="rss_since",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="rss_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        rss_source(
            ["https://example.com/feed.xml"],
            since=datetime(2024, 1, 15, tzinfo=timezone.utc),
            fetch=lambda url: _parse(RSS_XML),
        )
    )
    rows = _read_entries(pipeline)
    assert set(rows) == {"https://example.com/b"}


def test_removed_entry_is_forgotten_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, {"https://example.com/feed.xml": RSS_XML})

    # Drop Alpha from the feed. Replace load must forget it.
    remaining = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <guid>https://example.com/b</guid>
      <title>Beta</title>
      <link>https://example.com/b</link>
      <description>beta body</description>
      <pubDate>Thu, 01 Feb 2024 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    pipeline = _run_sync(dlt_mod, tmp_path, {"https://example.com/feed.xml": remaining})
    rows = _read_entries(pipeline)
    assert "https://example.com/a" not in rows
    assert "https://example.com/b" in rows


def test_unusable_feed_is_skipped_without_dropping_others(dlt_mod, tmp_path):
    pipeline = _run_sync(
        dlt_mod,
        tmp_path,
        {
            "https://example.com/bad.xml": BAD_XML,
            "https://example.com/feed.xml": RSS_XML,
        },
    )
    rows = _read_entries(pipeline)
    assert "https://example.com/a" in rows
    assert "https://example.com/b" in rows
