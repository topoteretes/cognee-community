# cognee-community-connector-stack-overflow

A Stack Overflow data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync questions and answers into memory — "ask my Q&A".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path — so you get **incremental re-sync** (upsert by question id, `merge` write
disposition, last-activity-date cursor) and **forget-on-delete** (questions removed from Stack
Overflow are emitted as hard-deletes and purged from memory on the next sync) with no core
changes.

## Install

```bash
pip install cognee-community-connector-stack-overflow
# or, from this monorepo:
cd packages/connector/stack-overflow && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_stack_overflow import stack_overflow_source

await cognee.remember(
    stack_overflow_source(
        tags=["python", "dlt"],
        api_key="your_stack_apps_key",   # optional; reads STACKOVERFLOW_API_KEY env var
        include_answers=True,
    ),
    dataset_name="stackoverflow_python",
    primary_key="question_id",
    write_disposition="merge",  # incremental upsert by question id
    max_rows_per_table=0,       # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="How do I use dlt to ingest incremental data?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["stackoverflow_python"],
)
```

Re-running `remember(...)` with the same dataset syncs only questions changed since the last
run and forgets questions that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced dataset on the second sync.

## How sync + forget-on-delete work

Incremental sync uses the question's `last_activity_date` Unix timestamp, persisted in dlt's
per-resource state. On each re-run only questions with activity since that cursor are fetched
and re-embedded. Deleted/migrated questions are detected by sweeping the previously-known id
set against the `/questions/{ids}` endpoint; missing ids are emitted as hard-delete markers,
dlt removes those rows on `merge`, and cognee's `orphan_cleanup` purges them from the graph,
vector, and relational stores.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tags` | `list[str] \| None` | `None` | Stack Overflow tags to filter by (AND). E.g. `["python", "pandas"]`. |
| `user_id` | `int \| None` | `None` | Fetch only questions posted by this user id. |
| `api_key` | `str \| None` | `None` | Stack Apps API key. Falls back to `STACKOVERFLOW_API_KEY` env var. |
| `include_answers` | `bool` | `True` | Include accepted + top answers in the `content` field. |

## Setup

1. (Optional) Register a Stack Apps application at https://stackapps.com/apps/oauth/register
   to get an API key and raise the daily quota from 300 to 10 000 requests.
2. Export `STACKOVERFLOW_API_KEY=your_key` or pass it directly.
3. Set your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Stack Exchange API (no live key required) and include an end-to-end run
that drives the source through a real `dlt` merge to prove a delete marker physically removes
the row.
