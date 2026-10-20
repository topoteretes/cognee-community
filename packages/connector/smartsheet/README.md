# cognee-community-connector-smartsheet

A Smartsheet data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your sheets into memory — "ask my sheets".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Sheet
rows are rendered to readable markdown documents and ingested as **normal documents**
(they flow through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path), via cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-smartsheet
# or, from this monorepo:
cd packages/connector/smartsheet && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_smartsheet import smartsheet_source

await cognee.remember(
    smartsheet_source(token="..."),  # or SMARTSHEET_TOKEN
    dataset_name="sheets",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
)

answer = await cognee.search(
    query_text="Which launch tasks are still in progress and who owns them?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["sheets"],
)
```

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

**Auth:** a Smartsheet API access token (**Account → Apps & Integrations → API
Access**), sent as `Authorization: Bearer` on every request. Pass it via `token=` or the
`SMARTSHEET_TOKEN` environment variable. Every request is a `GET`.

**Rows are columnar, not documents** — the issue's watch-out — so the connector renders
each row into a real document: the row's *primary column* value becomes the title
(prefixed with the sheet name), every non-empty cell becomes a `Column: value` line via
the sheet's column map, and row discussions plus attachment metadata are folded in.
`text/plain` and `text/csv` attachment bodies under 1 MB are inlined; other file types
are recorded by name, type, and size. Scoping is via `sheet_ids=[...]`; omit it to sync
every sheet the token can see.

**Incremental sync** is two-level, mirroring Smartsheet's structure: the account sheet
listing carries each sheet's `modifiedAt`, so a sheet that did not change since the
last run is skipped without fetching rows; inside a changed sheet, only rows whose
`modifiedAt` is newer than the stored per-row timestamp are re-emitted. Smartsheet has
no server-side `modifiedSince` filter on these endpoints, so the timestamps are
compared client-side (same result). Cursors live in dlt's per-resource state, so
re-running `remember` resumes where it left off. Documents are content-hashed by
cognee, so unchanged rows are never re-cognified.

**Forget-on-delete:** each run sweeps every successfully fetched sheet's row ids — a
row that vanished upstream is emitted with an `_deleted` hard-delete marker. Sheets
that vanish from the account listing (or from an explicit `sheet_ids` selection)
tombstone all their rows. dlt removes marked rows on merge, and cognee's existing
`orphan_cleanup` purges them from the graph, vector, and relational stores.

**Failure posture:** a sheet that fails to fetch is skipped for the run (with a
warning) — its rows are never tombstoned on unseen evidence, and its cursor is kept. A
sheet-listing failure skips the whole sync rather than masquerading as "all sheets
deleted". A row whose discussions or attachments fail to fetch still syncs its cells.

## Limitations

- Only `text/plain` / `text/csv` / TSV attachment bodies are inlined (1 MB cap); other
  types (Office, PDF, images) are recorded by name, type, and size.
- Sheet-level discussions not attached to a row are out of scope in v1.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Smartsheet API (no network, no credentials) and cover document
rendering, the two-level incremental cursor, pagination of the account listing and
large sheets, sweep-based forget-on-delete, failure handling, and an end-to-end
forget-on-delete through a real dlt merge.
