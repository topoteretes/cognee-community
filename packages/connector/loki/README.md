# cognee-community-connector-loki

A Grafana Loki data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your logs into memory, so an agent can answer "what happened before checkout started failing" or
"have we seen this error before".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get
**incremental sync** (each run reads only the time windows that closed since the last one) and
**forget-on-expiry** (windows older than `keep_for_days` are purged from memory) with no core
changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup.
- A Loki 2.x or 3.x reachable over HTTP: self-hosted, or Grafana Cloud Logs.

## Install

```bash
uv pip install cognee-community-connector-loki
# or, from this monorepo:
cd packages/connector/loki && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_loki import loki_source

await cognee.remember(
    loki_source(
        base_url="http://localhost:3100",
        selectors=['{service_name="checkout", level=~"error|warn"}'],
        resource_name="loki_prod",
    ),
    dataset_name="logs",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by document id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What went wrong in checkout before the payment timeouts?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["logs"],
)
```

See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced logs on the second sync.

`resource_name` is required and names one Loki connection: it keys the sync state and the
cleanup of expired documents. Give every Loki instance its own name. Pass `check_active` to let
a host stop a sync whose access was revoked; it is called before every row.

### Connecting

| Setup | Arguments |
| --- | --- |
| Local Loki | `base_url="http://localhost:3100"` |
| Grafana Cloud | `base_url` from the Loki data source page, `username` = its user id, `password` = an access policy token with the `logs:read` scope |
| Behind an auth proxy | `token="..."`, sent as `Authorization: Bearer` |
| Multi-tenant Loki | add `tenant_id="..."`, sent as `X-Scope-OrgID` |

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `selectors` | required | LogQL log queries; each keeps its own cursor |
| `bucket_minutes` | 5 | the time window one document covers, per stream |
| `lookback_hours` | 1 | how far back the first run reads |
| `keep_for_days` | 7 | documents whose window started longer ago are forgotten; `None` keeps them |
| `query_window_minutes` | 60 | time span of one request |
| `overlap_buckets` | 1 | buckets read again each run, to catch late lines |
| `page_limit` | 5000 | lines per request (Loki's default `max_entries_limit_per_query`) |
| `max_lines` | 100000 | line budget of one run; a larger backfill finishes over several runs |
| `max_lines_per_bucket` | 1000 | lines kept in one document; the rest are counted, not shown |

## What gets ingested

One document per stream per time bucket. One document per line would flood the graph with tiny
nodes; a bucket keeps the lines around an error together, which is the context an agent needs.

```text
# checkout logs 2026-01-01 00:00 UTC

Selector: {service_name="checkout", level=~"error|warn"}
Labels: level="error", service_name="checkout"
Window: 2026-01-01T00:00:00.000Z to 2026-01-01T00:05:00.000Z
Lines: 2

2026-01-01T00:01:00.000Z timeout calling payments
2026-01-01T00:02:00.000Z {"msg": "retry failed", "attempt": 3}
```

Lines are kept as they are, so JSON and logfmt fields stay readable to cognify. Structured
metadata (a trace id, for example) and labels from a parser stage (`| json`, `| logfmt`) are
appended to their line rather than splitting the stream, so a bucket still holds every line of
its stream.

## How sync + forget-on-expiry work

Each run reads only whole buckets, up to the last bucket boundary before now, so a document
never misses lines of a window that was still open. A per-selector cursor (the end of the last
window read, in nanoseconds) is persisted in dlt's per-resource state; the next run starts there,
minus `overlap_buckets`, so lines that reached Loki late still land in their bucket. A bucket
whose rendered text did not change is not emitted again, so the overlap costs no reprocessing.

`query_range` returns at most `page_limit` lines per request, and a full answer is not cut at
the oldest lines across all streams, so it can't be paged by its newest timestamp. An answer
below the limit is complete; a full one is read again as two halves of its time range, until
every part fits. Long ranges are also split into `query_window_minutes` requests so a backfill
never trips Loki's `max_query_length`.

Loki deletes logs by retention, not by event, so there is nothing to listen for. Instead each
run emits the documents whose window started more than `keep_for_days` ago with the `_deleted`
hard-delete marker; dlt drops them on `merge` and cognee's `orphan_cleanup` removes them from
the graph + vector + relational stores. Set `keep_for_days` at or below your Loki retention so
memory never outlives the logs it was built from. Removing a selector forgets its documents too.

## Limitations and tips

- Keep selectors narrow. A broad selector over many streams produces many documents per bucket
  and spends the LLM budget of cognify on noise; filter on level or a line pattern.
- Only log queries are accepted. A metric query (`rate(...)`, `count_over_time(...)`) is
  rejected with a clear error.
- If more than `page_limit` lines share one nanosecond, the time range can't be split further
  and the extra lines are skipped with a warning; raise `page_limit` if you see it.
- Logs can carry personal data and secrets. With cognee's access control (on by default) only
  the user who ran the sync, and users granted permission on the dataset, can read it; keep the
  logs in their own dataset.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests fake Loki's `query_range` (no live server) and include an offline end-to-end run
through a real `dlt` merge, proving an expired bucket is removed from the destination.
