# cognee-community-connector-clickhouse

A ClickHouse data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync ClickHouse tables into memory — "ask my ClickHouse".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`,
reusing cognee's existing DLT ingestion path
(`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by row key, `merge` write disposition, a monotonic
cursor column in dlt resource state) and **forget-on-delete** (rows removed upstream
are emitted as hard-delete markers and purged from memory on the next sync) with no
core change to cognee.

Rows are ingested as **normal documents** — each becomes a text document that flows
through cognee's cognify entity-extraction pipeline — via cognee's document-mode
marker. A ClickHouse row mixes prose-ish strings with wide, often nested, ClickHouse
-native types that carry no useful relational schema to extract deterministically,
so treating the row as text is the right call.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. That first shipped in cognee 1.4.0. The `cognee==` pin in
> `pyproject.toml` is the version this connector is tested against; move it forward in
> step with the other connectors.

It also uses `PIPELINE_SCOPE_ATTR` (cognee 1.6.x) to namespace dlt's pipeline state to
one ClickHouse connection, so two sources cannot drive each other's cursor.

## Install

```bash
uv pip install cognee-community-connector-clickhouse
# or, from this monorepo:
cd packages/connector/clickhouse && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_clickhouse import clickhouse_source

await cognee.remember(
    clickhouse_source(
        host="localhost",
        user="default",
        password="...",
        database="analytics",
        tables=["events"],
        key_columns={"events": "event_id"},
        cursor_columns={"events": "updated_at"},
    ),
    dataset_name="my_clickhouse",
    write_disposition="merge",  # REQUIRED — see the note below
)

answer = await cognee.search(
    query_text="Which products had failed logins last week?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my_clickhouse"],
)
```

Re-running `remember(...)` with the same dataset syncs only rows changed since the
last run and forgets rows deleted upstream. See `examples/example.py` for the full
flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to
> `"replace"`, which rewrites staging on every run and drops the corpus the
> incremental cursor and delete detection diff against.

## Choosing what to ingest

| Argument | Effect |
| --- | --- |
| `tables` | `["events"]` or `["analytics.events"]`. Names without a dot are qualified with `database`. |
| `key_columns` | `{"events": "event_id"}`, or a list for a composite key. See **Row identity** below. |
| `cursor_columns` | `{"events": "updated_at"}`. The column the incremental cursor rides on. |
| `title_column` | Column used as the document heading. Defaults to a `database.table <key>` label. |
| `columns` | Restrict which columns are read. The key and cursor columns are always included. |
| `where` | Extra SQL predicate, e.g. `"env = 'prod'"`. Applied to the row reads *and* the key sweep. |
| `include_table_comments` | Prefix each row's text with its table's `COMMENT`. Default `True`. |
| `detect_deletions` | Set `False` to skip the per-run key sweep. |

`where` narrows the sweep as well as the reads, so a row that falls out of the filter
is treated as absent and forgotten — which is what you want for a soft-delete flag.

## Row identity

ClickHouse has no primary key in the SQL sense, so the connector resolves one:

1. `key_columns`, if you pass it
2. the table's `PRIMARY KEY` (from `system.columns.is_in_primary_key`)
3. its sorting key (`is_in_sorting_key`) — ClickHouse's closest analogue, and what the
   engine reads first
4. a column literally named `id`

If none of those resolve, the sync fails with an actionable error rather than
guessing. A composite key is accepted and folded into one deterministic `id`
(`analytics.users:["acme","u1"]`), because cognee's row identity downstream is a
single column. Every `id` is namespaced by `database.table`, so two tables cannot
collide in the shared staging table.

Row ids look like `analytics.events:42`. They are stable, so an unchanged row keeps
its content-hash `data_id` and is not re-embedded or re-cognified.

## How sync + forget-on-delete work

**Incremental.** `cursor_columns` names a per-table monotonic column (`updated_at`, a
`DateTime64`, a version counter). The filter is pushed down as
`cursor >= {high-water mark}`, and the mark is persisted in dlt's per-resource state.

**Forget-on-delete.** ClickHouse reports deletions only through mutations and
lightweight deletes, neither of which is a reliable feed, so the connector does not
depend on them. Each run diffs a cheap key-only sweep (`SELECT <key> FROM db.table`)
against the keys seen on the previous run, also in resource state. Vanished rows are
emitted with the `_deleted` hard-delete marker, dlt drops them on `merge`, and
cognee's `orphan_cleanup` removes them from the graph, vector, and relational stores.

**A row new to the corpus is fetched regardless of its cursor value.** ClickHouse sees
plenty of back-dated writes — late batch loads, materialized views catching up — and a
`>=` cursor cannot see them. After the delta pass, keys in the sweep that were not
known and not already read are fetched by key, so a back-dated insert is not lost.

### Why `>=` and not `>`

ClickHouse cursor columns tie constantly: `DateTime64(3)` stamps collide and version
counters repeat across a batch. A strict `>` against the previous run's maximum would
silently drop **every** row sharing that value — data loss that no test against a fake
server would reveal. `>=` re-reads the boundary row instead, which is free: `merge`
upserts by key, the values are unchanged, so the content hash — and the `data_id` —
are unchanged and nothing is re-cognified.

## Failure posture and known limits

- **A transient sweep failure cannot purge your table.** A key sweep that comes back
  empty for a table that previously had rows is treated as a failure, not a mass
  deletion: deletion is skipped for that run and the key state is preserved.
- **Wiping a whole table upstream does not forget anything**, because that is
  indistinguishable from the failure case above. It self-heals — the next run that
  sees any row reconciles normally.
- **A NULL cursor value is invisible after the first sync.** `>=` can only match rows
  that carry the value, so a `Nullable` cursor column is rejected outright rather than
  silently dropping those rows. Give the cursor column a materialized or `DEFAULT`
  value instead.
- **The cursor column is not automatically a pruning key.** ClickHouse only prunes
  granules on the primary index, so if `updated_at` is not in your `ORDER BY`, the
  delta read filters after reading the parts. It is still correct and still far
  cheaper downstream (only the delta is embedded and cognified). Include the cursor in
  the sorting key if you want the read itself to be a range scan.
- **Both the key sweep and the persisted key set are O(rows) per run.** Cheap up to
  millions of rows when the key is the sorting key (ClickHouse answers from the
  primary index), wasteful beyond that. Pass `detect_deletions=False` there.
- **Nested values render as JSON and can get large.** `Array`/`Map`/`Tuple` columns
  become JSON in the row text, which is readable but bulky. Name the columns you care
  about in `columns` rather than selecting everything.
- **Identifiers are restricted.** Database, table, and column names must match
  `[A-Za-z_][A-Za-z0-9_]*`; backtick-quoted identifiers are rejected. The names are
  interpolated into SQL this connector builds, and an allowlist is the only way to keep
  a table name from being an injection point.

## Setup

1. Have a ClickHouse instance reachable over its HTTP interface. Pass `host=`, `port=`
   (default 8123, or 8443 with `secure=True`), `user=`, and `password=`, or set
   `CLICKHOUSE_HOST` / `CLICKHOUSE_PORT` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` /
   `CLICKHOUSE_DATABASE` / `CLICKHOUSE_SECURE`. Access is read-only: the connector only
   issues `SELECT`s, including against `system.tables` and `system.columns`, so the user
   needs `SELECT` on those plus the tables you sync.
2. Pick a cursor column and, if the table has no usable key, name one with
   `key_columns=`.
3. Set your `LLM_API_KEY` like any other cognee run.

`COMMENT` on your tables is worth setting: the connector folds it into every row's
text, so it becomes searchable alongside the data.

## Testing

```bash
uv run pytest tests/            # 86 tests, no server and no credentials needed
CLICKHOUSE_LIVE=1 uv run pytest tests/   # adds 14 live-server tests
```

No live server is required by default. Coverage:

- identifier validation, including every injection shape the allowlist has to reject
- value rendering (NULL, bool, Decimal, UUID, bytes, and ClickHouse containers as JSON)
- key resolution: explicit → `PRIMARY KEY` → sorting key → `id` → actionable error
- the incremental cursor: pushdown into SQL, backfill vs. delta, **tied cursor
  values**, a back-dated insert, no double emission, an incomparable cursor type
- forget-on-delete: the hard-delete markers, the empty-sweep guard, `where` narrowing,
  `detect_deletions=False`
- a full edit / insert / delete cycle through a **real dlt merge**, proving a
  `_deleted` marker physically removes the row, plus that the cursor and key state
  survive across pipeline runs
- `tests/test_clickhouse_forget.py`: the full path through cognee with the LLM and
  embeddings mocked — a deleted row's entity disappears from the graph, and an
  unchanged re-sync leaves the graph untouched
- `tests/test_clickhouse_live.py`: against a real server, `system.columns` /
  `system.tables` introspection, server-side parameter binding, and both key-predicate
  forms

### Validating against a real ClickHouse

The unit tests fake the query layer. To exercise the real SQL, start a server and the
live suite:

```bash
docker run -d --name cognee-clickhouse -p 8123:8123 \
  -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=clickhouse \
  -e CLICKHOUSE_DB=analytics clickhouse/clickhouse-server:24.8-alpine

# seed: see the DDL in tests/test_clickhouse_live.py for the shapes relied on
docker exec -i cognee-clickhouse clickhouse-client --password clickhouse < seed.sql

CLICKHOUSE_LIVE=1 uv run pytest tests/
```

Then run `examples/example.py` with a real `LLM_API_KEY` to see the whole chain —
ingest, search, incremental re-sync, and forget-on-delete — against a live server.