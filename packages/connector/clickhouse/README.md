# cognee-community-connector-clickhouse

A ClickHouse data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a ClickHouse table into memory — "ask my warehouse".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by row id, `merge` write disposition, a monotonic-column
cursor) and **forget-on-delete** (rows removed from the table are emitted as hard-deletes and
purged from memory on the next sync) with no core changes.

## Install

```bash
uv pip install cognee-community-connector-clickhouse
# or, from this monorepo:
cd packages/connector/clickhouse && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_clickhouse import clickhouse_source

await cognee.remember(
    clickhouse_source(
        host="localhost",
        port=8123,
        user="default",
        password="...",
        database="analytics",
        table="support_tickets",
        cursor_column="updated_at",  # any monotonic column: timestamp or auto-increment id
    ),
    dataset_name="support_tickets",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What tickets are still open about login failures?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["support_tickets"],
)
```

Re-running `remember(...)` with the same dataset syncs only rows changed since the last run
and forgets rows that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced table on the second sync.

## How sync + forget-on-delete work

Incremental sync pushes `WHERE cursor_column > last_cursor` down to ClickHouse, using
whatever monotonic column you name (a timestamp, an auto-increment id, ...); the cursor is
persisted in dlt's per-resource state. ClickHouse has no deletion feed, so each run also does
a lightweight `SELECT id_column FROM table` sweep (cheap — ClickHouse only reads that one
column off disk) and compares it to the previous run's id set (also in resource state);
vanished rows are emitted with the `_deleted` hard-delete marker, dlt drops them on `merge`,
and cognee's `orphan_cleanup` removes them from the graph + vector + relational stores. A row
that's new to the corpus but happens to sit below the cursor (a backdated insert, a restore)
is still caught, via the same id sweep, and fetched in a narrow follow-up query.

The connector always normalizes the primary-key column to `str()` on the way out — for both
live rows and delete markers — so dlt's merge key has a stable type regardless of whether the
underlying ClickHouse column is a `UInt64`, `String`, or `UUID`.

### Table comments (provisional)

`include_table_comments=True` (the default) also syncs each table's and column's `COMMENT`
(from `system.tables` / `system.columns`) into a **separate** dlt resource /
destination table, `clickhouse_table_comments`, as one synthetic summary row. This shape was
picked because it doesn't bloat every content row with metadata that never changes per-row,
and doesn't mix schema comments into the merge/hard-delete lifecycle of actual data rows — but
it hasn't been validated against how cognee's document/graph extraction actually wants schema
context shaped. See the large comment above `sync_table_comments` in
[`clickhouse.py`](cognee_community_connector_clickhouse/clickhouse.py) before relying on this
in production; it's flagged there for maintainer review and easy to change since it's fully
decoupled from the row sync.

## Setup

1. Have a ClickHouse server reachable over HTTP(S) (default port `8123`, or `8443` for TLS)
   with a username/password.
2. Pick a monotonic `cursor_column` on the table you want to sync — an `updated_at`
   timestamp or an auto-increment id both work.
3. Pass `host`, `port`, `user`, `password`, `database`, `table`, and `cursor_column`
   (or set the matching `CLICKHOUSE_*` environment variables), plus your `LLM_API_KEY` like
   any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the ClickHouse client (no live server) and include an offline end-to-end run
that drives the source through a real `dlt` merge to prove a delete marker physically removes
the row — exactly what cognee's `orphan_cleanup` reconciles against.
