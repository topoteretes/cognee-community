# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync arXiv papers into memory — "ask my arXiv".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
arXiv papers are rendered to markdown and ingested as **normal documents** (they
flow through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path), via cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-arxiv
# or, from this monorepo:
cd packages/connector/arxiv && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_arxiv import arxiv_source

# Fetch recent AI papers by category.
await cognee.remember(
    arxiv_source(categories=["cs.AI", "cs.CL"], start_date="2026-08-01"),
    dataset_name="arxiv",
)

# Search by free-text query.
await cognee.remember(
    arxiv_source(query="agent memory retrieval augmented generation"),
    dataset_name="arxiv-rag",
)

answer = await cognee.search(
    query_text="What are the latest techniques in agent memory?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["arxiv-rag"],
)
```

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with exactly the papers matching the current query on each run. Papers that no
longer match (e.g. withdrawn, or fall outside the date range) drop out of the
snapshot, and cognee's existing `orphan_cleanup` removes them from the graph and
vector stores. Unchanged papers keep a stable content-hash `data_id`, so they are
not re-ingested or re-cognified.

## Rate limiting

arXiv enforces ~1 request per 3 seconds. The connector automatically enforces a
3.5 s delay between paginated requests to stay safely under the limit.

## Testing

```bash
cd packages/connector/arxiv
uv run pytest tests/ -v
```

The tests mock the arXiv API (no network) and cover paper rendering, pagination,
date filtering, and full-snapshot forget-on-delete.