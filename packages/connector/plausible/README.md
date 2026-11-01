# cognee-community-connector-plausible

A Plausible Analytics data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync website traffic metrics, top pages, referral sources, and conversion goals into AI agent memory.

It exposes a `dlt` source passed to `cognee.remember(...)` / `cognee.add(...)`. Web analytics data is rendered into high-signal Markdown documents and ingested through Cognee's `cognify()` entity-extraction pipeline (via document-mode `DOCUMENT_SOURCE_ATTR`), building an interconnected knowledge graph of your web properties and performance.

## Features

- **Traffic & Performance Overviews**: Ingests total visitors, pageviews, bounce rate, and average visit duration over configurable periods (`30d`, `7d`, `day`, `month`, etc.).
- **Content & Page Insights**: Ingests high-traffic URLs and landing pages to allow agents to answer questions about popular documentation or feature pages.
- **Acquisition Channels**: Ingests referral sources and channels (e.g. GitHub, Google, Twitter, direct).
- **Goal Conversions**: Ingests completed conversion goals and event counts.
- **Cloud & Self-Hosted Support**: Works with Plausible Cloud (`https://plausible.io`) and self-hosted Plausible Community Edition instances.
- **Full-Snapshot Sync & Forget-on-Delete**: Configured with `write_disposition="replace"` so stale metrics and removed sites are automatically purged from the knowledge graph via Cognee's orphan cleanup.

## Requirements

Requires a Cognee release supporting document mode (`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR`), shipped in Cognee 1.4.0+.

## Installation

```bash
uv pip install cognee-community-connector-plausible
# or, from this monorepo:
cd packages/connector/plausible && uv sync --all-extras
```

## Quickstart

```python
import os
import asyncio
import cognee
from cognee_community_connector_plausible import plausible_source


async def main():
    # Configure Plausible API token and Site ID
    source = plausible_source(
        site_id="mysite.com",
        api_key=os.environ.get("PLAUSIBLE_API_KEY"),
        period="30d",
    )

    # Ingest into Cognee memory
    await cognee.remember(source, dataset_name="web_analytics")

    # Query the knowledge graph
    answer = await cognee.search(
        query_text="What are our top visited pages and main referral sources?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["web_analytics"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration Options

`plausible_source(...)` accepts the following arguments:

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `site_id` | `str` | `None` | Plausible site domain (e.g. `"example.com"`). Falls back to `PLAUSIBLE_SITE_ID`. |
| `api_key` | `str` | `None` | Plausible API token. Falls back to `PLAUSIBLE_API_KEY`. |
| `period` | `str` | `"30d"` | Time period to query (`"12mo"`, `"6mo"`, `"month"`, `"30d"`, `"7d"`, `"day"`). |
| `base_url` | `str` | `"https://plausible.io"` | Custom endpoint for self-hosted Plausible CE instances. |
| `include_breakdowns` | `bool` | `True` | Whether to fetch top pages, sources, and countries. |
| `include_goals` | `bool` | `True` | Whether to fetch conversion goals and custom events. |
| `client` | `Any` | `None` | Pre-built HTTP client (used for testing and dependency injection). |

## How Sync and Forget-on-Delete Work

The connector operates as a **full snapshot** (`write_disposition="replace"`):
1. Each run collects current metrics and compiles structured Markdown documents.
2. Unchanged documents maintain stable deterministic content IDs (`plausible:<site_id>:<period>:<type>`), avoiding duplicate extraction or unnecessary LLM calls.
3. If a site or time period is removed or replaced, Cognee's built-in `orphan_cleanup` purges the obsolete entities and vectors from graph and vector databases.
4. Upstream transient failures abort the sync safely before altering existing knowledge graph state.

## Testing

Run the offline test suite:

```bash
uv run pytest tests/
```

All unit tests run completely offline using mocked HTTP fixtures (covering authentication, rate limiting, breakdown parsing, and error recovery).
