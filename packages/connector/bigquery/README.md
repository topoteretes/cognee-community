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

- Python 3.10–3.14
- `cognee>=1.6.3,<2`, `dlt[sqlalchemy]`, `google-cloud-bigquery`

cognee 1.6.3 has `PIPELINE_SCOPE_ATTR`, which this source sets so its sync state lives
in its own dlt pipeline per cognee dataset (as cognee's built-in Gmail and Google Drive
connectors do) instead of the shared `ingest_dlt_source` pipeline, and
`DOCUMENT_SYNC_VERSION`, which the source checks when it is built.

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
3. Pass the key path and the project to `bigquery_source` (`examples/example.py` reads
   them from these environment variables), and export your `LLM_API_KEY`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
export BIGQUERY_PROJECT=my-project
export LLM_API_KEY=sk-...
```

Without `credentials_path` the connector uses Application Default Credentials
(`gcloud auth application-default login`, or `GOOGLE_APPLICATION_CREDENTIALS`, which
Google's client library reads itself).

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

| Argument | Meaning | Default |
|---|---|---|
| `project`, `credentials_path` | Project and service-account key | the key's project; Application Default Credentials |
| `datasets`, `tables` | Narrow the metadata sync | the whole project |
| `row_syncs` | Tables whose rows become documents, a list with one `RowSync` each | none |
| `maximum_bytes_billed` | Per-query byte cap | 1 GB |
| `resource_name` | Prefix of the two tables, which also scopes the sync state (keyword-only) | `bigquery_<project>` |
| `check_active` | Host checkpoint, called before every BigQuery API call and before every row is published | none |
| `service` (alias `client`) | Pre-built `google.cloud.bigquery.Client` (tests, proxies); not closed by the source | one per run, closed afterwards |

The tables are `bigquery_<project>_metadata` and `bigquery_<project>_rows`, so each
project keeps its own tables and sync state when several share one dataset.

After a run, `source.cognee_sync_stats` holds counts only, summed over metadata and rows:
`scanned`, `skipped` (unchanged), `changed`, `deleted`, `failed` (rows with a NULL key),
`failed_auth` and `failed_rate_limit` (1 when the run stopped for that reason),
`requests` (API calls), and `complete` (1 once both finished).

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
- **Only changed documents are emitted.** A hash of each metadata document is kept in
  dlt state, and so is the hash of each row at the last cursor value (the rows the
  `>=` query reads again), so an unchanged project or table emits nothing.
- **Cost guard**: every query is dry-run first. If the estimate is above
  `maximum_bytes_billed` (default 1 GB) the sync stops before anything is billed, and
  the same cap is set on the real query so BigQuery enforces it too.
- **Failures.** Errors other than "not found" (permissions, network) abort the run, so
  a failed listing never looks like a deletion. Google's client library retries
  transient errors for up to 10 minutes per call (40 for a query job) by default, and
  each wait sleeps inside cognee's staging lock that other ingestions wait on, so every
  call, with its retries, is capped at 60 seconds. A 401 or 403 sets `failed_auth`, and
  a 429 or a quota 403 sets `failed_rate_limit`. A key file that fails to load is
  reported by its path and the kind of error only.
- **Text.** Control characters and invalid Unicode are dropped from documents, so a
  stray NUL in a row can't fail the load.
- **Safety net.** If listing a whole project that had datasets returns none (a key for
  another project looks like this), the run stops instead of forgetting everything.
  To forget them, name the selection with `datasets=` or `tables=`.

**Limits:** the row deletion sweep keeps each synced table's keys in dlt state, so row
sync is meant for tables up to roughly a few hundred thousand rows. Metadata sync has
no such limit. Rows whose `key_column` is NULL are skipped with a warning. Row sync doesn't support tables
that require a partition filter (`require_partition_filter`), because its full reads and
key-only deletion query have no partition filter; their metadata still syncs. A row is only picked up incrementally if its `cursor_column` changes
when the row changes.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake BigQuery client (no credentials needed). They cover metadata and
row rendering, scope selection, the cursor query, the key sweep, the byte cap, error
handling and the bounded retries, that the key never shows up in errors or logs for
401, 403, 404, 429 and 5xx, and full dlt `merge` runs into a temporary SQLite
destination (first sync, edits re-sync, dropped tables and deleted rows are removed,
two projects in one dataset).
