"""Runnable example for the JSON Feed (jsonfeed.org) data-source connector.

Demonstrates parsing JSON Feed v1.1 payloads, cleaning HTML to Markdown,
handling media attachments (podcasts), and generating structured Cognee documents.
"""

from cognee_community_connector_jsonfeed import (
    clean_html_to_text,
    format_feed_item_markdown,
    jsonfeed_source,
    parse_json_feed_payload,
)

SAMPLE_FEED = {
    "version": "https://jsonfeed.org/version/1.1",
    "title": "Frontier AI & Systems Engineering",
    "home_page_url": "https://frontier.example.com",
    "feed_url": "https://frontier.example.com/feed.json",
    "description": "Engineering research on multi-agent autonomy and verifiable inference.",
    "authors": [
        {"name": "Dr. Sarah Lin", "url": "https://sarahlin.example.com"}
    ],
    "items": [
        {
            "id": "post-101",
            "url": "https://frontier.example.com/posts/agentic-graphs",
            "title": "Autonomous Agent Knowledge Graphs in Production",
            "content_html": "<p>When deploying swarms of autonomous agents, <strong>shared knowledge graphs</strong> prevent duplicate computation.</p><p>Key advantages:<ul><li>Cross-agent memory consolidation</li><li>Verifiable state transitions</li></ul></p>",
            "date_published": "2026-10-25T12:00:00Z",
            "tags": ["AI", "Agents", "Knowledge-Graph"],
            "attachments": [
                {
                    "url": "https://frontier.example.com/audio/ep101.mp3",
                    "mime_type": "audio/mpeg",
                    "title": "Audio Overview of Knowledge Graphs",
                    "duration_in_seconds": 960,
                }
            ],
        },
        {
            "id": "post-102",
            "url": "https://frontier.example.com/posts/zero-loss-arbitrage",
            "title": "Formal Verification of L2 Arbitrage Solvers",
            "content_text": "Using ZK proofs and SMT solvers to verify that user trades experience zero slippage and MEV protection on Base.",
            "date_published": "2026-10-26T15:30:00Z",
            "tags": ["DeFi", "Security"],
        },
    ],
}


def main():
    print("=" * 70)
    print(" JSON Feed (jsonfeed.org) Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Test HTML cleaning
    print("\n1. Testing HTML to Clean Markdown Conversion:")
    sample_html = "<p>Welcome to <strong>JSON Feed</strong>! Visit <a href='https://jsonfeed.org'>here</a>.<br>Features:<ul><li>Clean JSON syntax</li><li>Extensible attachments</li></ul></p>"
    cleaned = clean_html_to_text(sample_html)
    print(f"   Cleaned Output:\n{cleaned}\n")

    # 2. Parse feed payload
    print("2. Parsing JSON Feed Metadata and Items:")
    meta, items = parse_json_feed_payload(SAMPLE_FEED)
    print(f"   Feed Title:   {meta['title']}")
    print(f"   Feed URL:     {meta['feed_url']}")
    print(f"   Total Items:  {len(items)}")

    # 3. Format markdown document preview
    print("\n3. Rendered Cognee Document Preview:")
    print("-" * 50)
    first_doc = format_feed_item_markdown(meta, items[0])
    print(first_doc)
    print("-" * 50)

    # 4. Initialize DLT source with snapshot
    try:
        print("\n4. Initializing DLT source...")
        source = jsonfeed_source(
            snapshot_data=SAMPLE_FEED,
            filter_tags=["AI"],
        )
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ JSON Feed connector demo completed successfully!")


if __name__ == "__main__":
    main()
