# cognee-community-connector-bigquery

A BigQuery data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync what your warehouse knows about itself into memory — "which table has customer
churn, and what does `status = 3` mean?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. By default it ingests
**metadata**: one document per dataset and per table or view, with its description,
labels, partitioning, clustering, view SQL and every column's type and description
(nested `RECORD` fields included). For a memory layer this is usually worth more than
the rows. You can opt in to ingesting **rows** of specific tables too. Documents go
through cognee's normal cognify entity extraction (document mode), not the
deterministic dlt-row path.

## Requirements

Pins cognee 1.6.2. Document mode (`DOCUMENT_SOURCE_ATTR`) arrived in 1.4.0, and 1.6.x
adds `PIPELINE_SCOPE_ATTR`, which this source sets so its sync state (cursor, known ids
and keys) is kept per project and cognee dataset. On 1.4.0 every dlt source shares one
pipeline state, so syncing a second dataset on the same machine resets the cursor and
the deletion sweep.

## Install

From this monorepo:

```bash
cd packages/connector/bigquery && uv sync
```

## Setup

1. In the Google Cloud console, open BigQuery for your project. The free
   [BigQuery sandbox](https://cloud.google.com/bigquery/docs/sandbox) works and needs no
   credit card.
2. Create a service account with the roles **BigQuery Data Viewer** and **BigQuery Job
   User**, and download a JSON key for it. The connector only reads.
3. Export the key path, the project, and your `LLM_API_KEY` like any other cognee run:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
export BIGQUERY_PROJECT=my-project
export LLM_API_KEY=sk-...
```

Without a key file the connector falls back to Application Default Credentials
(`gcloud auth application-default login`).

## Usage

```python
import cognee
from cognee_community_connector_bigquery import RowSync, bigquery_source

await cognee.remember(
    bigquery_source(
        project="my-project",
        datasets=["analytics"],  # omit to read every dataset
        row_syncs=[  # optional: rows too
            RowSync(
                table="analytics.customers",
                key_column="customer_id",
                cursor_column="updated_at",  # TIMESTAMP/DATETIME/DATE/INTEGER
                columns=["name", "plan", "status"],
            ),
        ],
    ),
    dataset_name="bigquery",
    write_disposition="merge",  # required, see below
    self_improvement=False,  # keep repeat syncs cheap
)

answer = await cognee.search(
    query_text="Which tables describe customer plans?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["bigquery"],
)
```

`tables=["dataset.table", ...]` narrows metadata to specific tables, and can be
combined with `datasets`. See `examples/example.py` for a runnable version.

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`. cognee applies the caller's write
disposition to every table the source produces, and this source deletes by emitting
`_deleted` tombstone rows, which only `merge` turns into deletions.

- **Metadata** is re-read in full on every run. It comes from BigQuery's list/get API
  calls, which bill no query bytes. Row counts, sizes and modified times are left out
  of the text, so a table whose schema and descriptions did not change keeps the same
  content hash and is not re-cognified. A dataset, table or view missing from the
  listing is emitted as a tombstone and cognee's orphan cleanup forgets it.
- **Rows** (opt-in, per `RowSync`) become one document each, keyed by `key_column`.
  With a `cursor_column`, runs after the first query only rows at or after the last
  value seen (kept in dlt state), so unchanged rows are not re-read. To find deleted
  rows, incremental runs also read the key column alone, which BigQuery bills for that
  column only. Keys that disappeared are tombstoned. If the whole table is dropped, or you remove
  its `RowSync`, all of its rows are forgotten. Without a `cursor_column`, every run re-reads the
  whole table.
- **Cost guard**: every query is dry-run first. If the estimate is above
  `maximum_bytes_billed` (default 1 GB) the sync stops before anything is billed, and
  the same cap is set on the real query so BigQuery enforces it too.
- Errors other than "not found" (permissions, network) abort the run, so a failed
  listing never looks like a deletion.

**Limits:** the row deletion sweep keeps each synced table's keys in dlt state, so row
sync is meant for tables up to roughly a few hundred thousand rows. Metadata sync has
no such limit. A row is only picked up incrementally if its `cursor_column` changes
when the row changes.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake BigQuery client (no credentials needed). They cover metadata and
row rendering, scope selection, the cursor query, the key sweep, the byte cap, error
handling, and full dlt `merge` runs into a temporary SQLite destination (first sync,
edits re-sync, dropped tables and deleted rows are removed).
