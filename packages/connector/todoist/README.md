# cognee-community-connector-todoist

[Todoist](https://todoist.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee) — sync your projects, tasks,
and comments into memory, incrementally and with forget-on-delete. "Ask my
todo list."

Part of the cognee hackathon connector push (topoteretes/cognee#4815).

## Features

- **Incremental sync** — the first run is a full backfill; every later run
  posts the recorded Sync API `sync_token` and fetches exactly what changed.
- **Forget-on-delete** — deleted projects/tasks/comments disappear from the
  graph on the next sync. Deletions surface via Sync-API tombstones
  (`is_deleted=1` rows in the delta) *and* the activities feed (which also
  cascades a deleted project to its tracked tasks).
- **Document ingestion** — each row becomes a normal text document that flows
  through cognee's cognify entity-extraction pipeline (same treatment as the
  Notion / Google Drive connectors), so tasks are searchable and linkable,
  not just key-value rows.
- **Stable content hashes** — volatile metadata (due date, priority, labels,
  ordering) is deliberately excluded from document content, so editing them
  does not trigger a pointless re-cognify.
- **Retries with backoff** — rate-limit (429), server (5xx), timeout, and
  network errors are retried; auth failures fail fast.

## Installation

```bash
pip install "cognee[todoist]"
# or, from this repository:
uv pip install ./packages/connector/todoist
```

## Setup

1. Create an API token at <https://app.todoist.com/app/settings/integrations>
   (Settings → Integrations → Developer).
2. Export it:

   ```bash
   export TODOIST_API_TOKEN="..."
   ```

## Usage

```python
import cognee
from cognee_community_connector_todoist import todoist_source

await cognee.remember(
    todoist_source(),
    dataset_name="my_todoist",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see below
    max_rows_per_table=0,  # REQUIRED for a real account
)

answer = await cognee.recall("What do I need to do this week?")
```

> **`write_disposition="merge"` is mandatory.** The add pipeline defaults to
> `"replace"` (drop + reload the table each run); on the second, incremental
> sync that would wipe everything but the small delta.

> **`max_rows_per_table=0`** lifts cognee's 50-row read cap so orphan cleanup
> compares against the *whole* synced corpus.

A runnable version lives in [`examples/example.py`](examples/example.py).

## What gets ingested

| Entity   | Kind       | Document                                        |
| -------- | ---------- | ----------------------------------------------- |
| Project  | `project`  | name + description                              |
| Task     | `task`     | task name + description                         |
| Comment  | `comment`  | comment body (parent kind noted in the title)   |

Due dates, priorities, labels, and ordering are intentionally **not** part of
the document text (they would churn the content hash without adding meaning).

## Deleting data

- Delete a task/comment/project in Todoist → it is removed from cognee's
  graph, vector, and relational stores on the next sync (dlt hard-delete +
  cognee's orphan cleanup).
- Delete the whole dataset locally with `cognee.forget(...)` /
  `cognee.prune()`.

## Tests

```bash
uv run pytest packages/connector/todoist/tests -q
```

All tests run offline: Todoist API access is faked, and the dlt pipeline runs
against a temporary SQLite destination.

## Notes & limitations

- The activities deletion feed is the source of truth for "vanished" objects.
  Todoist keeps activities for a limited window; if a device stays offline for
  longer than that, delete and rebuild the dataset to reconcile.
- The connector only issues reads against the Sync API and never mutates your
  Todoist data.
