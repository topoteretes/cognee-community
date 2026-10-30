# cognee-community-connector-confluence

A Confluence data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Confluence Cloud wiki into memory — "ask my wiki".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by page id, `merge` write disposition, last-version-timestamp
cursor) and **forget-on-delete** (pages removed from a space are emitted as hard-deletes and
purged from memory on the next sync) with no core changes.

## Install

```bash
uv pip install cognee-community-connector-confluence
# or, from this monorepo:
cd packages/connector/confluence && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_confluence import confluence_source

await cognee.remember(
    confluence_source(
        base_url="https://your-domain.atlassian.net",
        email="you@example.com",
        api_token="...",
        space_keys=["ENG"],
    ),
    dataset_name="my_wiki",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by page id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What does our onboarding doc say about access requests?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my_wiki"],
)
```

Re-running `remember(...)` with the same dataset syncs only pages changed since the last run
and forgets pages that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced space on the second sync.

## How sync + forget-on-delete work

Incremental sync uses the page's last-version timestamp (`version.createdAt`), persisted in
dlt's per-resource state. Confluence has no deletion feed, so each run does a lightweight id
sweep of the space(s) and compares it to the previous run's ids (also in resource state);
vanished pages are emitted with the `_deleted` hard-delete marker, dlt drops them on `merge`,
and cognee's `orphan_cleanup` removes them from the graph + vector + relational stores. Footer
comments are folded into their page's text.

## Setup

1. Create a Confluence Cloud **API token** at
   https://id.atlassian.com/manage-profile/security/api-tokens.
2. Pass `base_url`, `email`, and `api_token` (sent as HTTP Basic auth; read-only `GET` only),
   plus your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Confluence API (no live token) and include an offline end-to-end run that
drives the source through a real `dlt` merge to prove a delete marker physically removes the
row — exactly what cognee's `orphan_cleanup` reconciles against.
