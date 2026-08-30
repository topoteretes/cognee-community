# cognee-community-connector-hacker-news

A Hacker News data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync stories and comments for tracked topics into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Items are ingested as **normal documents** via cognee's document-mode marker.

No authentication is required. Search goes through the public
[HN Algolia API](https://hn.algolia.com/api) (the Firebase item API is a poor
fit for topic filters).

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. The `cognee==` pin in `pyproject.toml` matches the
> Notion connector (1.4.0).

## Install

```bash
uv pip install cognee-community-connector-hacker-news
# or, from this monorepo:
cd packages/connector/hacker-news && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_hacker_news import hacker_news_source

await cognee.remember(
    hacker_news_source(["llm", "vector database"]),
    dataset_name="hacker_news",
)

answer = await cognee.search(
    query_text="What are people saying about vector databases?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["hacker_news"],
)
```

Default tags are `(story,comment)` so a topic pulls both posts and thread
replies. Pass `tags="story"` to skip comments. Optional `since=` walks only
newer `created_at_i` hits; omit it if you want the full snapshot so
forget-on-delete stays correct. See `examples/example.py`.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with the current Algolia hits for your queries. A deleted (or no longer
matching) item drops out of the snapshot and cognee's `orphan_cleanup` removes
it from the graph and vector stores. Unchanged `objectID` values keep a stable
row id, so they are not re-ingested.

## Testing

```bash
uv run pytest tests/
```

Tests inject in-memory Algolia pages (no network) and cover ingest, pagination,
the `since` cursor, and forget-on-delete.
