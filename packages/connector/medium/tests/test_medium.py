"""Unit tests for the Medium data-source connector.

Hermetic tests runnable in CI without live network access or Medium credentials.
"""

from typing import Any
import pytest

from cognee_community_connector_medium.medium import (
    DOCUMENT_SOURCE_ATTR,
    MEDIUM_SOURCE_NAME,
    MEDIUM_TABLE_NAME,
    MediumClient,
    _canonicalize_url,
    _generate_doc_id,
    _parse_article_html,
    _parse_feed_items,
    medium_source,
)


class FakeMediumHttpClient:
    """Mock HTTP client returning synthetic Medium feeds and article HTML."""

    def __init__(self, routes: dict[str, str] | None = None):
        self.routes = routes or {}

    def get(self, url: str, **kwargs):
        for pattern, response_text in self.routes.items():
            if pattern in url:
                class MockResponse:
                    status_code = 200
                    text = response_text
                    headers = {}

                    def raise_for_status(self):
                        pass

                return MockResponse()

        raise RuntimeError(f"Unmocked URL called: {url}")


SAMPLE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Stories by AI Researcher on Medium</title>
    <link>https://medium.com/@airesearcher</link>
    <item>
      <title>Building Autonomous Agents in 2026</title>
      <link>https://medium.com/@airesearcher/building-autonomous-agents-2026-abc123?source=rss</link>
      <dc:creator>Dr. Elena Rostova</dc:creator>
      <pubDate>Fri, 24 Oct 2026 14:00:00 GMT</pubDate>
      <category>Artificial Intelligence</category>
      <category>Agentic Systems</category>
      <content:encoded><![CDATA[<p>Autonomous agents require robust episodic memory.</p><pre><code>agent = Agent(memory=cognee)</code></pre>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

SAMPLE_ARTICLE_HTML = """<!DOCTYPE html>
<html>
  <head>
    <title>Graph Memory for AI Agents - Medium</title>
    <meta name="author" content="Marcus Vance">
  </head>
  <body>
    <article>
      <h1>Graph Memory for AI Agents</h1>
      <p>Knowledge graphs provide verifiable reasoning grounds for LLMs.</p>
      <pre>cognee.cognify()</pre>
    </article>
  </body>
</html>
"""


def test_canonicalize_url():
    """Test stripping query params to produce clean canonical URLs."""
    raw = "https://medium.com/@author/story-123?source=rss------5"
    assert _canonicalize_url(raw) == "https://medium.com/@author/story-123"
    assert _canonicalize_url("https://medium.com/p/456") == "https://medium.com/p/456"


def test_deterministic_doc_id():
    """Test that same canonical URL produces stable document ID."""
    url = "https://medium.com/@author/story-123"
    id1 = _generate_doc_id(url)
    id2 = _generate_doc_id(url)
    assert id1.startswith("medium_")
    assert id1 == id2


def test_parse_feed_items():
    """Test extracting structured documents from Medium RSS XML."""
    items = _parse_feed_items(SAMPLE_FEED_XML)
    assert len(items) == 1
    doc = items[0]
    assert doc["title"] == "Building Autonomous Agents in 2026"
    assert "https://medium.com/@airesearcher/building-autonomous-agents-2026-abc123" in doc["url"]
    assert "Dr. Elena Rostova" in doc["content"]
    assert "Artificial Intelligence, Agentic Systems" in doc["content"]
    assert "```\nagent = Agent(memory=cognee)\n```" in doc["content"]


def test_parse_article_html():
    """Test extracting clean title, author, and markdown from article HTML."""
    url = "https://medium.com/@marcus/graph-memory"
    doc = _parse_article_html(SAMPLE_ARTICLE_HTML, url)
    assert doc is not None
    assert doc["title"] == "Graph Memory for AI Agents"
    assert "Marcus Vance" in doc["content"]
    assert "Knowledge graphs provide verifiable reasoning grounds" in doc["content"]
    assert "```\ncognee.cognify()\n```" in doc["content"]


def test_medium_source_dlt_configuration():
    """Test that medium_source sets up correct DLT resource and document mode."""
    routes = {
        "feed/@testauthor": SAMPLE_FEED_XML,
        "test-article": SAMPLE_ARTICLE_HTML,
    }
    client = MediumClient(http_client=FakeMediumHttpClient(routes))

    source = medium_source(
        authors=["@testauthor"],
        article_urls=["https://medium.com/@testauthor/test-article"],
        client=client,
    )

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == MEDIUM_SOURCE_NAME
    assert MEDIUM_TABLE_NAME in source.resources

    resource = source.resources[MEDIUM_TABLE_NAME]
    assert resource.write_disposition == "replace"

    items = list(resource)
    assert len(items) == 2
    titles = [it["title"] for it in items]
    assert "Building Autonomous Agents in 2026" in titles
    assert "Graph Memory for AI Agents" in titles
