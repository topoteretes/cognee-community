# cognee-community-connector-readwise

A Readwise data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your reading highlights and notes into memory — "ask my library".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each book,
article, tweet, or podcast episode becomes a **document** containing all its
highlights and notes in reading order, ingested through cognee's cognify
entity-extraction pipeline.

## What gets ingested

Each Readwise *book* (the source of a highlight — could be a book, article,
tweet, or podcast episode) becomes one cognee document containing:

- Title, author, category, source URL
- All highlights in reading order (sorted by location)
- Per-highlight notes
- Tags on the book and individual highlights

Grouping highlights with their source gives cognee the context to answer
questions like *"What did I highlight about attention?"* rather than surfacing
isolated quotes.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** —
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the
> `resolve_dlt_sources` routing. The `cognee==` pin in `pyproject.toml` is a
> placeholder; update it to the first release that includes document-mode.

## Install

```bash
pip install cognee-community-connector-readwise
# or, from this monorepo:
cd packages/connector/readwise && uv sync --all-extras
```

## Setup

1. Get your Readwise access token at **https://readwise.io/access_token**
2. Export it before running:

```bash
export READWISE_TOKEN="your-token-here"
export LLM_API_KEY="sk-..."
```

## Usage

```python
import cognee
from cognee_community_connector_readwise import readwise_source

await cognee.remember(
    readwise_source(),          # READWISE_TOKEN from env, or pass token=...
    dataset_name="readwise",
)

answer = await cognee.search(
    query_text="What did I highlight about deep work?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["readwise"],
)
```

Restrict to specific categories with `categories=["books", "articles"]`.
See `examples/example.py` for the full runnable flow.

## How sync + forget-on-delete work

**Incremental sync** — Readwise exposes an `updatedAfter` filter on both the
books and highlights endpoints. Each sync fetches only records modified since
the last successful run. The cursor is stored in dlt state and advanced only
after successful ingestion.

**Forget-on-delete** — Readwise has no delete feed. The connector performs a
lightweight ID reconciliation on each incremental sync: IDs previously synced
but absent from the current full listing are tombstoned (`_deleted=True`) and
cognee's `orphan_cleanup` removes them from the graph and vector stores.

**Safety guarantee** — if the full-listing call fails, no tombstones are
emitted. A book missing because of an API error is never misinterpreted as
deleted.

## Rate limits

The highlights and books LIST endpoints are capped at 20 requests/minute. The
`Retry-After` header on 429 responses is honoured with bounded exponential
backoff for transient failures.

## Testing

```bash
cd packages/connector/readwise
uv run pytest tests/ -v
```

All tests use a `FakeReadwiseClient` — no live token required. They cover:
- Book + highlight rendering, notes, tags, location sorting
- Incremental cursor: only updated books re-fetched
- First-run: no tombstones attempted
- Forget-on-delete: absent books tombstoned on resync
- Reconciliation safety: no tombstones on listing failure
- Pagination following `next` URLs
- Category filtering
- document-source marker routing
- Empty account: no errors
