# cognee-community-connector-rss

An RSS / Atom data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync blogs, changelogs, newsletters, and release feeds into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Feed entries are ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline), via cognee's document-mode marker.

No authentication is required.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The `cognee==` pin in
> `pyproject.toml` matches the Notion connector (1.4.0).

## Install

```bash
uv pip install cognee-community-connector-rss
# or, from this monorepo:
cd packages/connector/rss && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_rss import rss_source

await cognee.remember(
    rss_source(["https://example.com/feed.xml"]),
    dataset_name="rss",
)

answer = await cognee.search(
    query_text="What shipped this week?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["rss"],
)
```

Pass as many feed URLs as you want. Both RSS 2.0 and Atom are accepted. See
`examples/example.py` for the full flow.

Optional `since=` skips entries whose `published`/`updated` is older than the
cursor. Prefer omitting it: the default full snapshot is what makes
forget-on-delete work.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with exactly the entries currently in the feeds. An item removed upstream drops
out of the snapshot and cognee's existing `orphan_cleanup` removes it from the
graph and vector stores. Unchanged entries keep a stable `id` (`guid` / `id` /
`link`), so they are not re-ingested. A feed that is completely unusable is
skipped with a warning so a second healthy feed in the same source is not lost.

## Testing

```bash
uv run pytest tests/
```

The tests parse in-memory RSS and Atom XML (no network) and cover ingest,
incremental `since`, and full-snapshot forget-on-delete.
