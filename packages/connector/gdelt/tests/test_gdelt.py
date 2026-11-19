"""Tests for GDELT DOC 2.0 global news metadata connector for cognee."""

import pytest
from pathlib import Path
from cognee_community_connector_gdelt.gdelt import (
    DOCUMENT_SOURCE_ATTR,
    normalize_gdelt_timestamp,
    format_article_document,
    parse_gdelt_articles,
    gdelt_source,
)


@pytest.fixture
def mock_gdelt_payload():
    return {
        "articles": [
            {
                "url": "https://www.reuters.com/technology/ai-autonomous-agents-settlement-rails-2026-10-24/",
                "title": "Autonomous AI Agent Swarms Adopt Base USDC for Zero-Capital Instant Settlements",
                "seendate": "20261024T143000Z",
                "domain": "reuters.com",
                "sourcecountry": "US",
                "language": "English",
            },
            {
                "url": "https://www.bbc.com/news/technology-autonomous-sentinels-2026",
                "title": "Decentralised Software Bounties Automated by Multi-Agent Watchdogs",
                "seendate": "20261024T150000Z",
                "domain": "bbc.com",
                "sourcecountry": "UK",
                "language": "English",
            },
        ]
    }


def test_normalize_gdelt_timestamp():
    assert normalize_gdelt_timestamp("20261024T143000Z") == "2026-10-24 14:30:00 UTC"
    assert normalize_gdelt_timestamp("20261024143000") == "2026-10-24 14:30:00 UTC"
    assert normalize_gdelt_timestamp("20261024") == "2026-10-24 UTC"
    assert normalize_gdelt_timestamp(None) == "Unknown Date"


def test_format_article_document(mock_gdelt_payload):
    art = mock_gdelt_payload["articles"][0]
    md = format_article_document(art, query="autonomous agents")
    assert "[GDELT News Article] Autonomous AI Agent Swarms" in md
    assert "Headline:** Autonomous AI Agent Swarms" in md
    assert "Publisher Domain:** `reuters.com`" in md
    assert "Source Country:** `US`" in md
    assert "2026-10-24 14:30:00 UTC" in md
    assert "[reuters.com](https://www.reuters.com" in md
    assert "Matching Query:** `autonomous agents`" in md
    assert "fair use boundaries" in md


def test_parse_gdelt_articles(mock_gdelt_payload):
    docs = parse_gdelt_articles(mock_gdelt_payload, query="autonomous agents")
    assert len(docs) == 2

    reuters_doc = docs[0]
    assert "reuters.com" in reuters_doc["id"]
    assert "gdelt:query:autonomous_agents" in reuters_doc["node_sets"]
    assert "gdelt:domain:reuters.com" in reuters_doc["node_sets"]
    assert "gdelt:country:us" in reuters_doc["node_sets"]
    assert reuters_doc["metadata"]["source"] == DOCUMENT_SOURCE_ATTR


def test_missing_snapshot_file_error():
    with pytest.raises(FileNotFoundError):
        gdelt_source(snapshot_data="/non/existent/gdelt_snapshot.json")


def test_gdelt_source_with_snapshot(mock_gdelt_payload):
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt is not installed in the test environment")

    source = gdelt_source(snapshot_data=mock_gdelt_payload)
    assert source is not None
