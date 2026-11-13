import pytest

from cognee_community_connector_rss.rss import (
    RSS_SOURCE_NAME,
    parse_feed_content,
    rss_source,
)

SAMPLE_RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Tech Changelog</title>
    <link>https://example.com</link>
    <description>Latest tech news</description>
    <item>
      <title>v2.0 Release Notes</title>
      <link>https://example.com/v2</link>
      <guid>https://example.com/v2</guid>
      <pubDate>Mon, 06 May 2024 12:00:00 GMT</pubDate>
      <author>dev@example.com</author>
      <description><![CDATA[<p>We launched <b>version 2.0</b> with major speedups!</p>]]></description>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Engineering Blog</title>
  <link href="https://example.com/blog"/>
  <updated>2024-05-01T10:00:00Z</updated>
  <entry>
    <title>Scaling Graph Databases</title>
    <link href="https://example.com/blog/scaling-graph"/>
    <id>tag:example.com,2024:blog-1</id>
    <published>2024-05-01T10:00:00Z</published>
    <author><name>Jane Doe</name></author>
    <summary>A deep dive into distributed graph partitioning.</summary>
  </entry>
</feed>
"""

SAMPLE_JSON_FEED = """{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "JSON Feed Example",
  "items": [
    {
      "id": "1",
      "title": "Welcome to JSON Feed",
      "url": "https://example.com/item1",
      "content_text": "This is a plain text post."
    }
  ]
}"""


def test_parse_rss_2():
    entries = parse_feed_content(SAMPLE_RSS_2)
    assert len(entries) == 1
    assert entries[0]["title"] == "v2.0 Release Notes"
    assert "We launched version 2.0 with major speedups!" in entries[0]["content"]
    assert entries[0]["feed_title"] == "Tech Changelog"


def test_parse_atom():
    entries = parse_feed_content(SAMPLE_ATOM)
    assert len(entries) == 1
    assert entries[0]["title"] == "Scaling Graph Databases"
    assert entries[0]["author"] == "Jane Doe"
    assert "distributed graph partitioning" in entries[0]["summary"]


def test_parse_json_feed():
    entries = parse_feed_content(SAMPLE_JSON_FEED)
    assert len(entries) == 1
    assert entries[0]["title"] == "Welcome to JSON Feed"
    assert entries[0]["content"]


def test_rss_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")
    db_path = tmp_path / "rss.db"
    pipeline = dlt.pipeline(
        pipeline_name="rss_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="rss_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = rss_source(SAMPLE_RSS_2)
    assert getattr(source, "cognee_document_source", None) == RSS_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT title, link FROM rss_feed_entries")
        assert len(rows) == 1
        assert rows[0][0] == "v2.0 Release Notes"
