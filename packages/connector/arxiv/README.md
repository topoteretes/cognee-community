# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync paper metadata and abstracts into memory — "ask my arXiv library".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
arXiv papers are ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

- cognee >= 1.4.0 (document-mode support)

## Install

```bash
pip install cognee-community-connector-arxiv
# or, from this monorepo:
cd packages/connector/arxiv && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_arxiv import arxiv_source

# Ingest recent AI papers.
source = arxiv_source(
    categories=["cs.AI", "cs.LG"],
    max_results=200,
    date_from="2026-01-01",
)

await cognee.remember(source, dataset_name="arxiv")

answer = await cognee.search(
    query_text="What are the latest trends in AI alignment?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["arxiv"],
)
```

Filter by author, date range, or category:

```python
# Papers by a specific author.
arxiv_source(author="LeCun", max_results=50)

# Papers in a specific category from a date range.
arxiv_source(categories=["astro-ph.CO"], date_from="2026-06-01", date_to="2026-09-01")
```

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with exactly the papers matching the current query on each run.  Unchanged papers
keep a stable content-hash `data_id`, so they are not re-ingested or re-cognified.
A paper that drops out of the query (e.g. because you changed the category filter)
is removed by cognee's `orphan_cleanup` on the next sync.

The arXiv API is rate-limited to ~1 request per 3 seconds; the connector
respects this limit and retries on transient errors.

## API

| Parameter     | Type          | Default | Description |
|---------------|---------------|---------|-------------|
| `categories`  | `list[str]`   | `None`  | arXiv category ids, e.g. `["cs.AI"]` |
| `author`      | `str`         | `None`  | Author name filter |
| `max_results` | `int`         | `500`   | Max papers to fetch (max 2000) |
| `date_from`   | `str` (ISO)   | `None`  | Earliest submission date |
| `date_to`     | `str` (ISO)   | `None`  | Latest submission date |

## Testing

```bash
cd packages/connector/arxiv
pip install pytest httpx
pytest tests/ -v
```

Tests mock the arXiv API (no live requests) and cover XML parsing, query
building, date filtering, pagination, and error handling.