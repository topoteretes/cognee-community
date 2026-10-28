# cognee-community-connector-triggerdev

A [Trigger.dev](https://trigger.dev) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync one environment's schedules, error groups,
failed runs and deployments into memory, so an agent can answer "why did the invoice job fail
yesterday, and did a deploy cause it?".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's DLT ingestion
path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`): each record becomes a text
document that goes through normal cognify, a re-sync only sends records whose text changed, and
a schedule deleted in Trigger.dev is removed from memory on the next sync.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup and per-row node sets.
- A Trigger.dev environment's secret key.

## Install

```bash
uv pip install cognee-community-connector-triggerdev
# or, from this monorepo:
cd packages/connector/triggerdev && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_triggerdev import triggerdev_source

await cognee.remember(
    triggerdev_source(api_key="tr_prod_...", resource_name="triggerdev_prod"),
    dataset_name="triggerdev",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # read the whole table back so deleted rows can be forgotten
    self_improvement=False,  # no whole-graph enrichment pass on every sync
)

answer = await cognee.search(
    query_text="Which tasks failed this week, and what error did each one hit?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["triggerdev"],
)
```

> **`write_disposition="merge"` is required**: the add pipeline defaults to `"replace"`, which
> would wipe the synced records on the second sync.

`resource_name` is required and names one environment's sync: it keys the sync state and the
cleanup of deleted schedules. Give each environment (prod, staging, ...) its own name, even in
one dataset. A run refuses state that was written for another environment as soon as it sees one
of that environment's runs. Pass `check_active` to let a host stop a sync whose access was
revoked; it is called before every request.

Options:

| Option | Default | What it does |
| --- | --- | --- |
| `include` | all four kinds | Any of `"schedules"`, `"errors"`, `"runs"`, `"deployments"` |
| `run_statuses` | `FAILED`, `CRASHED`, `SYSTEM_FAILURE`, `INTERRUPTED` | Which runs become documents. `None` syncs every run, which on a busy project is a lot of documents (and LLM cost in cognify) |
| `lookback_days` | `7` | How far back errors, runs and deployments are read (1 to 90) |
| `max_requests` | `500` | Request budget of one run; a bigger environment finishes over several runs |
| `api_url` | `https://api.trigger.dev` | The API origin of a self-hosted Trigger.dev |

## What gets ingested

One document per record, tagged with the node set `triggerdev:task:<task id>` (deployments get
`triggerdev:deployments`), so a search can stay inside one task's memory.

- **Schedules**: task, cron expression and its plain-English description, timezone, active or
  inactive, declarative or imperative, external id and deduplication key.
- **Error groups**: task, error type, status (unresolved, resolved, ignored), first and last
  seen day, fingerprint and the message (cut at 2,000 characters).
- **Failed runs**: task, status, version, created, started and finished time, duration, tags,
  schedule, number of attempts, and the last attempt's error with the top 15 lines of its stack
  trace.
- **Deployments**: version, status, short code, created and deployed time, runtime, and the git
  branch, commit, commit message, author and pull request when the deploy recorded them.

Never ingested: run payloads, outputs and metadata (they hold your customers' data), costs,
idempotency keys and a deployment's git `remoteUrl` (it can carry a token). Nothing volatile is
rendered either (a schedule's next run, an error group's occurrence count, a live run's
duration), so an unchanged record renders to the same text and is never re-sent.

## How sync and forget-on-delete work

**Schedules** are listed in full on every run (`GET /api/v1/schedules`). A schedule missing from
a complete listing is fetched once more by id (`GET /api/v1/schedules/{id}`), and only a 404
forgets it: it is emitted with the `_deleted` hard-delete marker, dlt drops it on `merge`, and
cognee's `orphan_cleanup` removes it from the graph, vector and relational stores. A listing that
fails, or a page without `totalPages`, forgets nothing and fails the run instead. Taking
`"schedules"` out of `include` forgets them, since nothing would keep them current.

**Error groups, runs and deployments** are history (`GET /api/v1/errors`,
`GET /api/v1/runs`, `GET /api/v1/deployments`, each with `page[after]` cursors). Each run reads
the last `lookback_days` and sends only records whose text changed. A failed run that is new or
changed is retrieved once (`GET /api/v3/runs/{id}`) for its error. Records that age out of the
window stay in memory, and their fingerprints leave the sync state, so the state stays the size
of the window.

A run never sleeps on a rate limit. On HTTP 429, a spent `max_requests` budget or a key revoked
mid-run, it stops cleanly, keeps what it already sent and reports the reason in
`source.cognee_sync_stats` (`failed_rate_limit`, `failed_budget`, `failed_auth`); the next run
picks up from there. A key rejected on the first request fails loudly. A run that can't be
retrieved is counted in `failed` and retried on the next run. A listing the key may not read
(HTTP 403, e.g. a named key with a narrow preset) fails the run and names the endpoint: take
that kind out of `include` or widen the key's preset.

## Limitations

- Trigger.dev keeps logs for 1 day on Free, 7 on Hobby and 30 on Pro. Check how far back your
  plan lets you list runs before raising `lookback_days`.
- Changing `run_statuses` or `lookback_days` doesn't remove documents already stored. To start
  over, sync into a new dataset or delete this one.
- The environment check needs at least one run in the window; an environment with no runs is
  not checked.
- Error messages and stack traces are stored as Trigger.dev reports them. If your tasks put
  secrets or personal data into error messages, they end up in memory too.
- The dataset holds your jobs' errors and deploy history. With cognee's access control (on by
  default) only the user who ran the sync, and users granted permission on the dataset, can read
  it.

## Setup

1. In the Trigger.dev dashboard open the project, pick the environment, open **API keys** and
   copy its secret key (`tr_prod_...`, `tr_stg_...`, `tr_dev_...`). A named key with a read-only
   preset is the safer choice; the connector only reads.
2. Pass it as `api_key`, plus your `LLM_API_KEY` like any other cognee run. See
   `examples/example.py` for the full flow.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests need no Trigger.dev account. The fake in `tests/fake_triggerdev.py` answers at the HTTP
layer with the documented response shapes, and refuses unknown query parameters and out-of-range
page sizes, so the real client and every URL it builds are part of each test. The ingestion tests
run the source through `cognee.add` and check that a deleted schedule leaves memory and that a
failed listing forgets nothing.
