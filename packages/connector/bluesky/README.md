# Cognee Community Connector: Bluesky (AT Protocol)

A native data-source connector for [Cognee](https://github.com/topoteretes/cognee) to ingest public posts, author feeds, and keyword discussions from [Bluesky](https://bsky.app) (AT Protocol) into knowledge graphs.

## Features
- **Author Feeds & Keyword Discovery:** Ingest posts from specific handles (e.g. `bsky.app`, `dan.bsky.social`) or keyword queries (e.g. `GraphRAG`, `LLM memory`).
- **Zero Credentials Required:** Interfaces with public AT Protocol XRPC endpoints (`public.api.bsky.app`) with zero authentication keys required for public data.
- **Incremental Synchronization:** Content-hashed SHA-256 fingerprinting skips unchanged posts on subsequent syncs.
- **Forget-on-Delete:** Deletion reconciliation emits `_deleted: True` tombstones when posts are deleted upstream or feeds are deselected.
- **100% Hermetic & Offline:** Testable completely offline with zero live credentials required.

## Installation

```bash
pip install cognee-community-connector-bluesky
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_bluesky import bluesky_source


async def main():
    # Configure Bluesky source
    source = bluesky_source(
        actors=["bsky.app"],
        queries=["AI agents"],
        limit_per_feed=25,
    )

    # Ingest and cognify into memory
    await cognee.add(source)
    await cognee.cognify()

    # Query in natural language
    results = await cognee.search("What recent discussions about AI agents took place?")
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `actors` | `list[str]` | `None` | Bluesky user handles or DIDs (e.g. `["bsky.app"]`). |
| `queries` | `list[str]` | `None` | Keyword search queries. |
| `limit_per_feed` | `int` | `50` | Maximum posts to fetch per author or query. |
| `base_url` | `str` | `"https://public.api.bsky.app"` | XRPC API base endpoint. |
| `client` | `BlueskyClient` | `None` | Optional injected HTTP client instance for testing. |

## License
Apache-2.0
