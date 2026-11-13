# cognee-community-connector-substack

A Substack data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a Substack newsletter into memory via the public RSS feed — "ask my newsletter".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Posts
are ingested as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode marker.

## How it works

- **Auth**: None required — Substack's RSS feed is public.
- **Ingest**: Each post is fetched from the publication's RSS feed and rendered as plain text.
- **Incremental sync**: The full feed is re-fetched each run (`write_disposition="replace"`).
  Unchanged posts have a stable `content_hash`, so they are not re-ingested.
- **Forget-on-delete**: Posts removed from the feed (unpublished / deleted) fall out of the
  snapshot; cognee's `orphan_cleanup` removes them from the graph and vector stores on the
  next sync — no custom diff/cursor logic needed.
- **Paywalled posts**: Subscriber-only posts have truncated `content:encoded` in the feed.
  The connector detects this and marks such posts with `is_partial=True` rather than
  silently ingesting clipped text.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it.

## Install

```bash
pip install cognee-community-connector-substack
# or, from this monorepo:
cd packages/connector/substack && uv sync --all-extras
```

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_substack import substack_source

async def main():
    # Pass the subdomain, full domain, or URL — all are accepted:
    #   "example"                         → https://example.substack.com/feed
    #   "example.substack.com"            → https://example.substack.com/feed
    #   "https://example.substack.com"    → https://example.substack.com/feed
    source = substack_source("example.substack.com")

    await cognee.remember(source, dataset_name="substack")

    results = await cognee.search(
        query_text="Summarize the newsletter posts about AI.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["substack"],
    )
    for r in results:
        print(r)

asyncio.run(main())
```

### Scoping ingestion

```python
# Limit to the 10 most recent posts (useful for first-run / testing):
source = substack_source("example.substack.com", max_posts=10)
```

### Bandwidth saving with conditional GET

```python
# Re-use ETag / Last-Modified from a previous run to skip re-downloading
# when the feed has not changed:
source = substack_source(
    "example.substack.com",
    http_etag="W/\"abc123\"",
    http_modified="Sat, 04 Oct 2025 12:00:00 GMT",
)
```

## Running the example

```bash
cd packages/connector/substack
pip install -e ".[dev]"
python examples/ingest_substack.py
```

## Running the tests

```bash
cd packages/connector/substack
pip install -e ".[dev]"
pytest tests/ -v
```

## Package layout

```
packages/connector/substack/
├── README.md
├── cognee_community_connector_substack/
│   ├── __init__.py
│   └── substack.py
├── examples/
│   └── ingest_substack.py
├── tests/
│   └── test_substack.py
└── pyproject.toml
```

## Post row shape

Each post is yielded as a flat dict with the following fields:

| Field          | Type        | Description                                              |
|----------------|-------------|----------------------------------------------------------|
| `id`           | `str`       | Stable post identifier (guid or URL)                     |
| `title`        | `str`       | Post title                                               |
| `url`          | `str`       | Canonical post URL                                       |
| `content`      | `str`       | Plain-text content (HTML stripped)                       |
| `pub_date`     | `str\|None` | Publication date (ISO-8601 UTC) or None                  |
| `author`       | `str\|None` | Author name if present in feed                           |
| `is_partial`   | `bool`      | `True` when content is a subscriber-only preview         |
| `content_hash` | `str`       | SHA-256 of content, used for change-detection            |
