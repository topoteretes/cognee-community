# cognee-community-connector-crisp

A Crisp data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Crisp conversations into memory — "ask my inbox".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Each conversation is rendered to a single markdown document (visitor context +
message transcript) and ingested as a **normal document** (it flows through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path),
via cognee's document-mode marker.

## Requirements

> This connector requires a cognee release that ships "document-mode"
> (`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the
> `resolve_dlt_sources` routing that reads it). **This is not in cognee 1.3.0.**
> The `cognee==` pin in `pyproject.toml` is a placeholder; set it to the first
> release that includes document-mode before publishing.

## Install

```bash
uv pip install cognee-community-connector-crisp
# or, from this monorepo:
cd packages/connector/crisp && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_crisp import crisp_source

await cognee.remember(
    crisp_source(),  # CRISP_IDENTIFIER/CRISP_KEY/CRISP_WEBSITE_ID from env
    dataset_name="crisp",
    max_rows_per_table=0,   # ingest every conversation (see note below)
)

answer = await cognee.search(
    query_text="What did customers ask about?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["crisp"],
)
```

Pass `since=<epoch-seconds>` to only sync conversations updated after a watermark
(incremental cursor). See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with exactly the conversations currently visible to the integration on each run.
Crisp has no delete feed and drops aged-out/deleted conversations from its
listing, so a deleted conversation simply falls out of the snapshot and cognee's
existing `orphan_cleanup` removes it from the graph + vector stores. Unchanged
conversations keep a stable content-hash `data_id`, so they are not re-ingested
or re-cognified. A render/API error aborts the run (leaving memory untouched)
rather than letting a partial snapshot forget live conversations.

Each conversation becomes **one** node (not one per message) — Crisp sessions are
short and numerous, so aggregating keeps the graph readable.

> **Note:** cognee's `ingest_dlt_source` reads at most `max_rows_per_table` rows
> from the dlt destination (default 50). For a real inbox pass
> `max_rows_per_table=0` (unlimited) so orphan cleanup compares against the
> *whole* snapshot rather than a truncated window. Use a dedicated
> `dataset_name` per workspace so cleanup only touches this source.

## Setup

1. Create a Crisp plugin (Marketplace → Plugins → New Plugin → Private) and grab
   a Development token keypair (identifier + key), plus your `website_id`.
2. Set `CRISP_IDENTIFIER`, `CRISP_KEY`, `CRISP_WEBSITE_ID` (or pass
   `identifier=`/`key=`/`website_id=`), plus your `LLM_API_KEY` like any other
   cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Crisp API (no live token) and cover message/conversation
rendering, pagination, the document-source tagging, and full-snapshot
forget-on-delete (vanish on re-sync). They require a cognee build that includes
document-mode (see **Requirements**).
