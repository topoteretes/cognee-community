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
2. Export it, together with your `LLM_API_KEY` like any other cognee run:

```bash
export CODA_API_TOKEN="..."
```

Coda's API is now served by Superhuman Docs. The connector uses `https://coda.io/apis/v1`
by default; set `CODA_API_BASE_URL=https://docs.superhuman.com/apis/v1` to use the new
address instead.

## Usage

```python
import cognee
from cognee_community_connector_coda import coda_source

await cognee.remember(
    coda_source(doc_ids=["<doc id>"]),  # CODA_API_TOKEN from env
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
| `doc_ids` | all your docs | Docs to sync. Without it, every doc the token can see, narrowed by `workspace_id` and `folder_id`. |
| `workspace_id` | none | Only docs of this workspace. |
| `folder_id` | none | Only docs of this folder. |
| `include_tables` | `True` | Ingest each table as one document. Views are always skipped. |
| `include_hidden_pages` | `False` | Also ingest hidden pages. |
| `include_sync_pages` | `False` | Also ingest sync pages, which mirror a page of another doc. |
| `base_url` | `https://coda.io/apis/v1` | API address (`CODA_API_BASE_URL`). |
| `resource_name` | `coda_documents` | Staging table. Use a different name per sync scope that shares a dataset. |

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
  tables of each doc and compares their `updatedAt` with the last run. Only changed pages
  are exported again (Coda's export runs in the background, so the connector starts it,
  waits for it and downloads the result), and only changed tables have their rows read
  again. Unchanged documents keep their id and are not processed again. Renaming a doc
  re-renders its pages and tables, since each document names its doc.
- **Forget-on-delete.** A page or table that disappears from its doc, and a doc that is
  deleted, no longer shared with the token, or no longer selected, are removed from memory
  on the next sync.
- **Failures.** A rejected token always raises. Other errors raise after retries (429 and
  5xx), so dlt keeps the previous state and a failed listing never looks like a deletion.

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
(the page export flow, retries, no token sent to the download link), check page and table
rendering, drive the sync against a fake Coda API through a real dlt pipeline (changed and
unchanged pages and tables, renames, deletions, failed runs), and run `cognee.remember`
end to end to prove a deleted page leaves the graph.
