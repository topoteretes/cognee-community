# cognee-community-connector-rollbar

A [Rollbar](https://rollbar.com) data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a project's error items, their resolution state and a short stack-trace excerpt into memory —
"what keeps breaking, and what did we already fix?".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only items whose document changed are re-processed) and
**forget-on-delete** (items whose occurrences you delete in Rollbar are purged from memory on the
next sync) with no core changes.

## Requirements

- cognee 1.6.4 or later (pinned to `1.6.4`), for per-row node sets (`DOCUMENT_SYNC_VERSION` 2).
- A Rollbar **project access token with the `read` scope**. An account access token that can read
  the project works too. Write scope is never needed. The listing, resolve, regression and deletion
  behaviour below was checked against a free Rollbar project.

## Install

```bash
uv pip install cognee-community-connector-rollbar
# or, from this monorepo:
cd packages/connector/rollbar && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_rollbar import rollbar_source

await cognee.remember(
    rollbar_source(123456, access_token="<read token>", account_slug="my-team"),
    dataset_name="rollbar_errors",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by item
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
    self_improvement=False,  # no whole-graph enrichment pass on every sync
)

answer = await cognee.search(
    query_text="Which errors are still active in production, and which were fixed in what version?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["rollbar_errors"],
)
```

`project_id` is the numeric id under **Settings → General**. Without `access_token` the connector
reads `ROLLBAR_ACCESS_TOKEN`. Choose what to sync with:

- `statuses=("active", "resolved", "muted")` — the default, the same statuses Rollbar lists
  without a filter. Add `"archived"` for archived group items.
- `environments=("production",)` — only these environments; all when empty.
- `levels=("error", "critical")` — only these levels; all when empty.

An item whose status leaves the selection (an active-only sync, and the item is resolved) is
forgotten. `account_slug` is the account name in your Rollbar URLs; it only builds each document's
link back to Rollbar, because Rollbar's API does not tell a project token its account name.
Re-running `remember(...)` with the same dataset syncs only what changed since the last run. See
`examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced items on the second sync.

## What gets ingested

One document per **item**, never per occurrence: an item is the group Rollbar keeps for every
occurrence of one error, and occurrences repeat thousands of times.

- **Item**: title, counter, environment, level, framework, status, the dates it was first seen,
  last activated and last resolved, the version it was resolved in, whether it is assigned, and
  its volume since it was last activated as an order of magnitude (`fewer than 10`, `10+`,
  `100+`, ...).
- **Excerpt** of the occurrence that activated the item (`activating_occurrence_id`, the first
  occurrence since the item was last resolved): the exception class and message, the innermost
  frames with their code line, a chained exception's causes, or the text of a message item, and
  the `code_version`.

The request, person, local variables, arguments, custom data and server details of an occurrence
never leave Rollbar, and a crash report's raw text is not ingested. Exception messages are your
application's text, so review what your errors carry before syncing them.

Each document is keyed by `projects/<project id>/items/<item id>` and placed in two node sets,
`rollbar:<project id>` and `rollbar:<project id>:<environment>`, so a search can be scoped to one
environment.

## How sync + forget-on-delete work

Rollbar's item listing has no time filter and items carry no modified time, so each run lists the
selected items in full (100 per page) and emits only the items whose rendered document changed,
using a fingerprint kept in dlt's per-resource state. The exact occurrence count, last-seen time
and latest occurrence id are left out of the document on purpose: they change with every new
occurrence and would re-process every active item on every sync. Resolving, muting, assigning or
regressing an item changes its document; another occurrence does not.

Rollbar has no API to delete an item: you delete its occurrences, and once all of them are gone
the item drops out of the listing. The connector mirrors what the API serves:

- an item the listing no longer holds is emitted with the `_deleted` hard-delete marker, dlt drops
  it on `merge`, and cognee's `orphan_cleanup` removes it from the graph + vector + relational
  stores. That covers deleting every occurrence, and merging the item into another one, which Rollbar's
  docs say takes it out of the listing too;
- an excerpt whose occurrence is gone (deleted, or expired by your plan's retention) answers 404,
  and the document is re-rendered without it on the next run.

Each run therefore costs one request per page of items plus one per item with an excerpt; the
read limit Rollbar reports is 50,000 requests a minute per token.

Pages are numbered while items keep changing, so an item can slip between two pages. Rollbar's
`ids` filter is ignored by the API, so when a run misses items it knew, it lists everything a
second time and forgets only what both listings miss.

A run never sleeps on the rate limit. When Rollbar answers 429 the run stops cleanly, keeps what
it emitted and reports it in `source.cognee_sync_stats` (`failed_rate_limit`); the next run
resumes. Server errors are retried with a short backoff. A listing that is interrupted, or comes
back in an unexpected shape, never deletes anything.

A missing or refused token raises a clear error before anything is synced, and so does a token
that reads another project. Losing access in the middle of a run stops it like a rate limit
(`failed_access`), keeping every document.

Keep `resource_name` (default `rollbar_<project id>`) and the dataset fixed across runs. Each
project gets its own name by default, so several projects can share one dataset.

## Setup

1. In Rollbar open your project → **Settings → Project Access Tokens** and create a token with
   only the **read** scope. Keep it private.
2. Note the project id under **Settings → General**, and the account name in your Rollbar URLs
   if you want links back to each item.
3. Pass the token as `access_token` (or set `ROLLBAR_ACCESS_TOKEN`) and the project id, plus your
   `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Rollbar API at the `httpx` transport (no live token) and include offline
end-to-end runs through a real `dlt` merge and through `cognee.add` and `cognify`, proving an
item whose occurrences were deleted is removed from the graph, not only from staging.
