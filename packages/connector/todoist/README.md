# cognee-community-connector-todoist

Sync Todoist projects, tasks, and comments into cognee with Todoist's
incremental Sync API.

## Install

```bash
uv pip install cognee-community-connector-todoist
```

## Setup and use

Get an API token from **Todoist → Settings → Integrations → Developer**, then
set `TODOIST_API_TOKEN`. Set `LLM_API_KEY` as required by cognee.

```python
import cognee
from cognee_community_connector_todoist import todoist_source

await cognee.remember(
    todoist_source(include_projects=True, include_tasks=True, include_comments=True),
    dataset_name="todoist",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
    incremental_loading=False,
)
```

The source uses `TODOIST_API_TOKEN` by default; you can pass `token="..."`
directly instead. Set `include_projects`, `include_tasks`, or
`include_comments` to `False` to limit the content fetched. Keep this selection
the same for every run that reuses a saved DLT pipeline state. Todoist's
`sync_token` advances across the requested resource types, so changing the
selection can skip existing records of a newly enabled type. Start a fresh DLT
pipeline state to change the selection. See
[`examples/example.py`](examples/example.py) for a runnable sync and search.

## Sync behavior

The first run requests the selected resources with `sync_token="*"`. DLT
persists Todoist's returned token as resource state, so later runs request only
the changed resources. Todoist deletion flags are passed to DLT as hard-delete
markers; cognee's normal orphan cleanup then removes those records from memory.
Task completion is kept separate from deletion: the task's `checked` value is
stored when Todoist returns it, and only `is_deleted` triggers removal. The
Sync API's full sync covers active resources; completed-task history is not
backfilled by this connector. Keep `write_disposition="merge"` and
`max_rows_per_table=0` for incremental sync and complete deletion reconciliation.
Todoist account plan limits may restrict comments. A large initial full sync can
be delayed; running the source again uses its saved token to fetch newer changes.

Tests use mocked API responses and do not require Todoist credentials.
