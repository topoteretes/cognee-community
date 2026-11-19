"""Unit tests for the JSON Feed (jsonfeed.org) data-source connector."""

from pathlib import Path
import pytest

from cognee_community_connector_jsonfeed import (
    JSONFEED_SOURCE_NAME,
    JSONFEED_TABLE_NAME,
    clean_html_to_text,
    format_feed_item_markdown,
    jsonfeed_source,
    parse_json_feed_payload,
)
from cognee_community_connector_jsonfeed.jsonfeed import generate_item_id

SAMPLE_JSON_FEED = {
    "version": "https://jsonfeed.org/version/1.1",
    "title": "Engineering & Architecture Blog",
    "home_page_url": "https://engineering.example.com/",
    "feed_url": "https://engineering.example.com/feed.json",
    "description": "Deep-dives into distributed systems, AI agents, and infrastructure.",
    "authors": [
        {"name": "Alice Dev", "url": "https://alice.example.com"}
    ],
    "items": [
        {
            "id": "post-2026-001",
            "url": "https://engineering.example.com/posts/scaling-graph-memory",
            "title": "Scaling Graph Memory for Autonomous Agents",
            "content_html": "<p>Autonomous agents require persistent, temporal knowledge graphs to maintain long-term context.</p><p>Key principles include:<ul><li>Entity deduplication</li><li>Content-hash stability</li></ul></p>",
            "summary": "Best practices for maintaining temporal knowledge graphs in agent architectures.",
            "date_published": "2026-10-25T10:00:00Z",
            "date_modified": "2026-10-26T12:00:00Z",
            "authors": [{"name": "Alice Dev"}],
            "tags": ["AI", "Architecture", "Knowledge-Graph"],
            "attachments": [
                {
                    "url": "https://engineering.example.com/podcasts/ep42.mp3",
                    "mime_type": "audio/mpeg",
                    "title": "Episode 42 Audio Discussion",
                    "size_in_bytes": 10485760,
                    "duration_in_seconds": 1800,
                }
            ],
        },
        {
            "id": "post-2026-002",
            "url": "https://engineering.example.com/posts/zero-trust-cloud",
            "title": "Zero Trust Cloud Networking in Kubernetes",
            "content_text": "Zero trust architecture enforces mutual TLS between every microservice container using Envoy and Cilium.",
            "date_published": "2026-10-27T08:30:00Z",
            "tags": ["Security", "Kubernetes"],
        },
        {
            # Item with missing explicit ID to test fallback generation
            "url": "https://engineering.example.com/posts/micro-arbitrage",
            "title": "Micro-Arbitrage on L2 Networks",
            "content_text": "Evaluating high-speed transaction ordering and slippage bounds on Base Mainnet.",
            "date_published": "2026-10-28T14:15:00Z",
            "tags": ["Web3", "Finance"],
        },
    ],
}


def test_clean_html_to_text():
    """Verify HTML cleaning, heading resolution, entity unescaping, and tag stripping."""
    html_input = "<h3>Deep Learning</h3><p>We use <strong>transformers</strong> &amp; <em>diffusion models</em>.</p><br><ul><li>Item 1</li><li>Item 2</li></ul>"
    cleaned = clean_html_to_text(html_input)
    assert "### Deep Learning" in cleaned
    assert "We use transformers & diffusion models." in cleaned
    assert "- Item 1" in cleaned
    assert "- Item 2" in cleaned
    assert "<p>" not in cleaned
    assert "&amp;" not in cleaned


def test_generate_item_id():
    """Verify item ID preservation and fallback generation."""
    explicit_item = {"id": "custom-uuid-1234", "title": "Test"}
    assert generate_item_id(explicit_item) == "custom-uuid-1234"

    fallback_item = {
        "title": "No ID Post",
        "url": "https://example.com/post-no-id",
        "date_published": "2026-10-28T00:00:00Z",
    }
    fallback_id = generate_item_id(fallback_item, feed_url="https://example.com/feed.json")
    assert fallback_id.startswith("jsonfeed_item_")
    # Deterministic check
    assert fallback_id == generate_item_id(fallback_item, feed_url="https://example.com/feed.json")


def test_format_feed_item_markdown():
    """Verify Markdown rendering, metadata blocks, and media attachment formatting."""
    item = SAMPLE_JSON_FEED["items"][0]
    markdown = format_feed_item_markdown(SAMPLE_JSON_FEED, item)

    assert "# [Engineering & Architecture Blog] Scaling Graph Memory for Autonomous Agents" in markdown
    assert "**Published Date:** 2026-10-25T10:00:00Z" in markdown
    assert "**Last Modified:** 2026-10-26T12:00:00Z" in markdown
    assert "**Author(s):** Alice Dev" in markdown
    assert "## Media Attachments" in markdown
    assert "[Episode 42 Audio Discussion](https://engineering.example.com/podcasts/ep42.mp3)" in markdown
    assert "`audio/mpeg` (1800s)" in markdown
    assert "Entity deduplication" in markdown


def test_parse_json_feed_payload():
    """Verify parsing of valid JSON Feed payload dict."""
    meta, items = parse_json_feed_payload(SAMPLE_JSON_FEED)
    assert meta["title"] == "Engineering & Architecture Blog"
    assert len(items) == 3


def test_jsonfeed_source_filtering_and_limits():
    """Verify tag filtering, max items limit, and document generation."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed in test environment")

    # Filter for Security items only
    source = jsonfeed_source(
        snapshot_data=SAMPLE_JSON_FEED,
        filter_tags=["Security"],
    )
    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == JSONFEED_TABLE_NAME

    items = list(resource)
    assert len(items) == 1
    assert items[0]["id"] == "post-2026-002"
    assert "Security" in items[0]["tags"]
    assert "feed:Engineering & Architecture Blog" in items[0]["cognee_node_set"]


def test_missing_snapshot_file_error():
    """Verify FileNotFoundError is raised for non-existent local file."""
    with pytest.raises(FileNotFoundError):
        jsonfeed_source(snapshot_data="/tmp/non_existent_feed_9999.json")
