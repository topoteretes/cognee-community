import pytest

from cognee_community_connector_substack.substack import (
    SUBSTACK_SOURCE_NAME,
    _normalize_feed_url,
    parse_substack_feed,
    substack_source,
)

SAMPLE_SUBSTACK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>The Pragmatic Architect</title>
    <link>https://pragmatic.substack.com</link>
    <description>Deep dives on modern architecture.</description>
    <item>
      <title>Building Cognitive Memory Layers</title>
      <link>https://pragmatic.substack.com/p/building-cognitive-memory-layers</link>
      <guid isPermaLink="false">post_999</guid>
      <pubDate>Thu, 28 Mar 2024 15:00:00 GMT</pubDate>
      <dc:creator>Gergely Orosz</dc:creator>
      <content:encoded><![CDATA[<p>Cognitive architectures require knowledge graphs.</p><h3>Why Graphs Matter</h3><p>Relational stores alone cannot capture flexible entity relationships.</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""


def test_normalize_feed_url():
    name, url = _normalize_feed_url("pragmatic")
    assert name == "pragmatic"
    assert url == "https://pragmatic.substack.com/feed"

    name2, url2 = _normalize_feed_url("https://example.substack.com")
    assert name2 == "example"
    assert url2 == "https://example.substack.com/feed"


def test_parse_substack_feed():
    posts = parse_substack_feed(SAMPLE_SUBSTACK_XML)
    assert len(posts) == 1
    p = posts[0]
    assert p["publication"] == "The Pragmatic Architect"
    assert p["title"] == "Building Cognitive Memory Layers"
    assert p["author"] == "Gergely Orosz"
    assert "Cognitive architectures require knowledge graphs." in p["content"]
    assert "### Why Graphs Matter" in p["content"]


def test_substack_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")

    def mock_fetch(pub):
        return SAMPLE_SUBSTACK_XML

    db_path = tmp_path / "substack.db"
    pipeline = dlt.pipeline(
        pipeline_name="substack_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="substack_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = substack_source("pragmatic", fetch_fn=mock_fetch)
    assert getattr(source, "cognee_document_source", None) == SUBSTACK_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT publication, title, author FROM substack_posts")
        assert len(rows) == 1
        assert rows[0][0] == "The Pragmatic Architect"
        assert rows[0][1] == "Building Cognitive Memory Layers"
