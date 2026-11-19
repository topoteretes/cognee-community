# JSON Feed Connector for Cognee

The official `jsonfeed` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing modern blog posts, changelogs, product release notes, and newsletters published in [JSON Feed](https://www.jsonfeed.org/) format (v1.0 and v1.1) into AI agent memory and relationship graphs.

---

## Overview

Many modern developer blogs, technical changelogs, and content publications provide JSON Feed feeds (`feed.json`) alongside or in place of legacy XML/RSS. 

This connector reads JSON Feeds directly, seamlessly parsing rich fields (`content_text`, `content_html`, `summary`, `authors`, `tags`, and podcast/media `attachments`), converting HTML content into clean markdown, and routing documents into Cognee's ingestion pipeline (`cognify()`).

### Key Capabilities

- **JSON Feed 1.0 & 1.1 Support**: Parses spec-compliant feeds and automatically navigates multi-page feeds via `next_url`.
- **Text & HTML Prioritization**: Intelligently extracts `content_text` if provided; otherwise converts `content_html` into clean markdown with formatted lists, links, and stripped tags.
- **Deterministic ID Generation**: Preserves original item `id` strings or computes a deterministic SHA-256 hash fallback for malformed items.
- **Media Attachment Capture**: Extracts podcast audio, video, and PDF attachments (`url`, `mime_type`, `size_in_bytes`, `duration_in_seconds`).
- **Tag & Quantity Filtering**: Allows filtering posts by tag/category or setting a maximum item threshold (`max_items`).
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "jsonfeed"` so posts flow into Cognee's entity extraction and relationship graph.
- **Reconciliation & Orphan Cleanup**: Operates with `write_disposition="replace"`, allowing deleted or aged-out feed items to be cleanly removed by Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install "cognee-community-connector-jsonfeed"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/jsonfeed
pip install -e .
```

---

## Quickstart

### 1. Ingest JSON Feeds into Cognee

```python
import asyncio
import cognee
from cognee_community_connector_jsonfeed import jsonfeed_source


async def main():
    # 1. Initialize connector with target JSON Feed URLs
    source = jsonfeed_source(
        feed_urls=[
            "https://daringfireball.net/feeds/json",
            "https://inessential.com/feed.json",
        ],
        filter_tags=["Apple", "Tech"],
        max_pages=3,
    )

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Search agent memory
    results = await cognee.search(
        "What are the latest announcements regarding Web3 and autonomous AI agents?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Offline / Local Snapshot Ingestion

```python
source = jsonfeed_source(
    snapshot_data="./sample_feed.json",
    max_items=50,
)
```

---

## Document Schema & Metadata

Each feed item is formatted as a structured Markdown document:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Item persistent identifier or deterministic hash |
| `title` | `str` | Standardized header `[<Feed Title>] <Item Title>` |
| `url` | `str` | Canonical link to the post or external link |
| `feed_title` | `str` | Name of the source feed |
| `feed_url` | `str` | Source URL of the feed |
| `date_published` | `str` | RFC 3339 publication timestamp |
| `date_modified` | `str` | RFC 3339 modification timestamp |
| `tags` | `list[str]` | Item categories / tags |
| `cognee_node_set` | `list[str]` | Scoped tags (`feed:<title>`, `jsonfeed`, `blog`) |
| `content` | `str` | Clean Markdown document body with metadata and content |

---

## Running Tests

Run the hermetic test suite:

```bash
PYTHONPATH=packages/connector/jsonfeed pytest packages/connector/jsonfeed/tests/ -v
```
