# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync arXiv paper metadata and abstracts into memory — "ask my arXiv library".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. arXiv
papers are rendered to markdown and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline), via cognee's document-mode marker.

## Requirements

This connector requires cognee 1.4.0+ (document-mode support).

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

await cognee.remember(
    arxiv_source(
        categories=["cs.AI", "cs.CL"],
        max_results=50,
        submitted_after="2026-01-01",  # incremental sync
    ),
    dataset_name="arxiv",
)

answer = await cognee.search(
    query_text="What are the main research themes?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["arxiv"],
)
```

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str \| None` | Free-text search query |
| `categories` | `list[str] \| None` | arXiv categories, e.g. `["cs.AI", "cs.CL"]` |
| `author` | `str \| None` | Author name to search for |
| `max_results` | `int` | Maximum papers to fetch (default 100) |
| `submitted_after` | `str \| None` | Only papers after this date (YYYY-MM-DD) |
| `client` | `Any` | Pre-built HTTP client (for testing) |

## How sync works

The source uses `write_disposition="merge"` with `primary_key="arxiv_id"`, so
incremental syncs only add new or changed papers. Use `submitted_after` to
limit results to papers published after a specific date — perfect for
incremental sync runs.

## Rate limiting

arXiv's API enforces a rate limit of roughly one request every 3 seconds.
The connector enforces this delay internally between paginated requests.

## Auth

No authentication required. arXiv's export API is open.
