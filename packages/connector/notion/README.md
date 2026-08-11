# cognee-community-connector-notion

A Notion data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Notion workspace into memory — "ask my Notion".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Notion
pages are rendered to markdown and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The `cognee==` pin in
> `pyproject.toml` is a placeholder; set it to the first release that includes
> document-mode before publishing.

## Install

```bash
uv pip install cognee-community-connector-notion
# or, from this monorepo:
cd packages/connector/notion && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_notion import notion_source

await cognee.remember(
    notion_source(),  # NOTION_API_KEY from env, or pass token=...
    dataset_name="notion",
)

answer = await cognee.search(
    query_text="Summarize my project notes.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["notion"],
)
```

Scope what you ingest with `page_ids=[...]` or `database_ids=[...]`; omit both to sync
every page the integration can see. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the pages currently visible to the integration on each run. Notion has no delete
feed and hides archived/trashed/unshared pages from listings, so a deleted page simply
drops out of the snapshot and cognee's existing `orphan_cleanup` removes it from the graph
and vector stores. Unchanged pages keep a stable content-hash `data_id`, so they are not
re-ingested or re-cognified. A render error aborts the run (leaving memory untouched)
rather than letting a partial snapshot forget live pages.

## Setup

1. Create a Notion internal integration and share the pages/databases you want with it.
2. Set the token as `NOTION_API_KEY` (or pass `token=...`), plus your `LLM_API_KEY` like
   any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Notion API (no live token) and cover page rendering, pagination,
transient/gone handling, and full-snapshot forget-on-delete (edit / archive / vanish on
re-sync). They require a cognee build that includes document-mode (see **Requirements**).
