# cognee-community-connector-coda

A Coda data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your
docs into memory, so you can ask "what is blocking the launch, and who owns it?" across a
spec page and a task table at once.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every page becomes one
document (exported as markdown) and every table one more (its rows written as labeled
blocks), all flowing through cognee's normal cognify entity extraction.

## Install

```bash
uv pip install cognee-community-connector-coda
# or, from this monorepo:
cd packages/connector/coda && uv sync
```

## Setup

1. Create an API token under **My account** in Coda. You can restrict it to read-only
   access and to the docs you want to sync, which is all this connector needs.
2. Pass it as `token=`. The example reads it from `CODA_API_TOKEN`, together with your
   `LLM_API_KEY` like any other cognee run:

```bash
export CODA_API_TOKEN="..."
```

Coda's API is now served by Superhuman Docs. The connector uses `https://coda.io/apis/v1`
by default; pass `base_url="https://docs.superhuman.com/apis/v1"` to use the new address
instead (the example reads `CODA_API_BASE_URL`).

## Usage

```python
import os

import cognee
from cognee_community_connector_coda import coda_source

await cognee.remember(
    coda_source(doc_ids=["<doc id>"], token=os.environ["CODA_API_TOKEN"]),
    dataset_name="coda",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("What is blocking the launch?", datasets=["coda"])
```

The doc id is the part after `_d` in a doc's URL (`coda.io/d/Launch-plan_dAbCDeFGH` has the
id `AbCDeFGH`). Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `doc_ids` | all your docs | Docs to sync. Without it, every doc the token can see, narrowed by `workspace_id` and `folder_id`. A listed doc that cannot be read on its first sync raises. |
| `workspace_id` | none | Only docs of this workspace. |
| `folder_id` | none | Only docs of this folder. |
| `include_tables` | `True` | Ingest each table as one document. Views are always skipped. |
| `include_hidden_pages` | `False` | Also ingest hidden pages. |
| `include_sync_pages` | `False` | Also ingest sync pages, which mirror a page of another doc. |
| `base_url` | `https://coda.io/apis/v1` | API address. |
| `max_requests` | `1000` | Request budget of one run (a page export counts as one). A bigger account finishes over several runs. |
| `resource_name` | derived | Staging table, which scopes this sync's state and cleanup. Derived from the doc, the workspace and folder, or `coda_my_docs`; **required** when `doc_ids` lists several docs, since two sources sharing one would delete each other's documents. |

## What a table looks like

Tables and prose live in the same doc, so each table becomes **one document** rather than
one node per row: rows keep each other's context, they still go through entity extraction,
and a changed table costs one cognify instead of one per row.

```
Doc: Launch plan
Table: Tasks

## Row: Fix export
- Owner: Priya
- Status: Blocked
- Tags: api, v2
```

Views are skipped because they show the rows of a table that is already synced. Embed
pages carry no content of their own and are skipped too.

## How sync and forget-on-delete work

- **Incremental.** Coda has no "changed since" filter, so every run lists the pages and
  tables of each doc and compares their `updatedAt` with the last run. The table listing
  carries no `updatedAt`, so each table's own endpoint is read too (one request per table
  per run). Only changed pages
  are exported again (Coda's export runs in the background, so the connector starts it,
  waits for it and downloads the result), and only changed tables have their rows read
  again. Unchanged documents keep their id and are not processed again. Renaming a doc
  re-renders its pages and tables, since each document names its doc.
- **Forget-on-delete.** A page or table that disappears from its doc, a doc that answers
  404 or 410, a doc that leaves the `/docs` listing, and a doc you remove from `doc_ids`
  are removed from memory on the next sync. Removed and deselected docs are decided right
  after the doc listing, so a run cut short by the budget still forgets them.
- **Access removed is not deletion.** A doc or page that answers 403 (the token lost
  access) is left unchanged and counted in `unreadable`. A `/docs` listing that comes back
  empty while docs were synced before forgets nothing until the next run confirms it, an
  empty page listing keeps the doc's pages (every Coda doc has at least one page), and an
  answer without its `items` list raises.
- **A run is bounded.** 429 and 5xx are retried, honoring `Retry-After`. A `Retry-After`
  over 60 seconds, a 429 that outlasts the retries, or more than five minutes of retry
  waits stops the run cleanly with `failed` and `failed_rate_limit`; a spent
  `max_requests` budget is a planned pause and sets only `failed_budget`. Either way the
  finished docs are kept and the next run starts after the last one. A rejected token
  always raises, and any other error raises so dlt keeps the previous state.

`cognee_sync_stats` counts `emitted`, `unchanged`, `deleted` and `gone` documents,
`exported` pages, `unreadable` docs, and `failed` plus `failed_*` for a stopped run.

The export download link is a short-lived signed URL; the token is never sent to it.

## Privacy

This reads the content of every selected doc. Nothing is fetched until you call
`remember`, and anyone with read access to the target dataset can read what was ingested,
so keep Coda in its own dataset. The token stays in memory and is never written into
documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Coda account. They run the client against a mocked HTTP transport
(the page export flow, retries and their limits, no token sent to the download link),
check page and table rendering, drive the sync against a fake Coda API (changed and
unchanged pages and tables, renames, deletions, access removed, empty answers, rate limit
and budget stops, `check_active`) and through a real dlt pipeline (two sources in one
dataset, failed runs), and run `cognee.remember` end to end to prove a deleted page leaves
the graph.
