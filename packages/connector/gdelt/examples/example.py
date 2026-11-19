#!/usr/bin/env python3
"""Example demonstration of GDELT DOC 2.0 global news metadata connector for cognee."""

from cognee_community_connector_gdelt.gdelt import (
    format_article_document,
    parse_gdelt_articles,
    gdelt_source,
)

SAMPLE_GDELT_DATA = {
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
            "url": "https://techcrunch.com/2026/10/24/quant-swarms-100-percent-win-rate/",
            "title": "How Multi-Agent Swarms Use Kelly-Expected-Value To Regularize Market Volatility",
            "seendate": "20261024T160000Z",
            "domain": "techcrunch.com",
            "sourcecountry": "US",
            "language": "English",
        },
    ]
}


def main():
    print("=" * 70)
    print(" GDELT DOC 2.0 Global News Metadata Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse articles
    docs = parse_gdelt_articles(SAMPLE_GDELT_DATA, query="autonomous agents")
    print(f"\n1. Extracted Articles: {len(docs)}")
    for doc in docs:
        print(f"   - ID: {doc['id']} | Node sets: {doc['node_sets']}")

    # 2. Render Article Markdown Preview
    print("\n2. Article Markdown Preview:")
    print("-" * 50)
    print(format_article_document(SAMPLE_GDELT_DATA["articles"][0], query="autonomous agents"))
    print("-" * 50)

    # 3. Initialize DLT Source
    print("\n3. Initializing DLT source...")
    try:
        source = gdelt_source(snapshot_data=SAMPLE_GDELT_DATA)
        print("   ✅ DLT source successfully created.")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ GDELT connector demo completed successfully!")


if __name__ == "__main__":
    main()
