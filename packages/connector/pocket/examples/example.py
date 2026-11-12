"""Example of ingesting Pocket reading lists into cognee memory."""

from unittest.mock import MagicMock
from cognee_community_connector_pocket import (
    iter_pocket_api_articles,
    iter_pocket_export_articles,
    pocket_source,
    pocket_export_source,
)


def mock_pocket_client():
    return {
        "status": 1,
        "list": {
            "2001": {
                "item_id": "2001",
                "resolved_title": "The Anatomy of Autonomous AI Agents",
                "resolved_url": "https://example.com/ai-agents-deep-dive",
                "excerpt": "A comprehensive analysis of multi-tier memory and tools in autonomous agents.",
                "authors": {"1": {"name": "Alan Turing"}},
                "tags": {"ai": {}, "research": {}},
                "time_added": "1730500000",
                "status": "0",
                "word_count": "3200",
            },
            "2002": {
                "item_id": "2002",
                "resolved_title": "Postgres vs SQLite for Local AI Storage",
                "resolved_url": "https://example.com/database-comparison",
                "excerpt": "Comparing latency and ACID guarantees for embedded vector search.",
                "authors": [{"name": "Grace Hopper"}],
                "tags": {"databases": {}},
                "time_added": "1730501000",
                "status": "1",
                "word_count": "1850",
            },
        },
    }


def main():
    print("Testing Pocket data extractors with mock response...")

    # 1. API Extractor Demo
    articles = list(iter_pocket_api_articles(client=mock_pocket_client))
    print(f"Extracted {len(articles)} article(s) from Pocket API:")
    for a in articles:
        print(f" - [{a['id']}] {a['title']}")
        print(f"   URL: {a['url']} | Status: {a['status']} | Tags: {a['tags']}")
        print(f"   Excerpt: {a['content'][:100]}...\n")


if __name__ == "__main__":
    main()
