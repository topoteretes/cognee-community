# cognee-community-connector-ticktick

A TickTick data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your tasks and projects into memory — "ask my TickTick".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Tasks and projects are ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path),
via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The package pins
> `cognee==1.4.0` (or later builds that include document-mode).

Python `>=3.11,<=3.13`.

## Install

```bash
uv pip install cognee-community-connector-ticktick
# or, from this monorepo:
cd packages/connector/ticktick && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_ticktick import ticktick_source

await cognee.remember(
    ticktick_source(access_token="…"),  # or TICKTICK_ACCESS_TOKEN from env
    dataset_name="ticktick",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What are my open tasks?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["ticktick"],
)
```

Scope what you ingest with `selected_project_ids=["inbox", "…"]`; omit to sync
every project the token can see plus Inbox. See `examples/example.py` for the
full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging
with exactly the items currently visible for the selected scope on each run.
TickTick has no delete feed and no listing filter on `modifiedTime`, so a deleted
task simply drops out of the snapshot and cognee's existing `orphan_cleanup`
removes it from the graph and vector stores.

Unchanged items keep a stable content-hash `data_id` (rendering excludes volatile
fields like `modifiedTime` / `sortOrder` / `etag`), so they are not re-ingested or
re-cognified. A mid-run API error aborts the run (leaving memory untouched)
rather than letting a partial snapshot forget live items.

### Completed-task safety

`GET /project/{id}/data` returns **open** tasks only. Completed tasks are fetched
via `POST /task/completed` with `projectIds` + a `completedTime` window. That
endpoint caps responses at **200**. The connector recursively bisects any full
window until results fit under the cap. If a window cannot be narrowed further
but still returns 200, the run **aborts** — never publishes a truncated snapshot
that would wrongly delete tasks from memory.

### Inbox

Inbox is **not** returned by `GET /project`. Pass `"inbox"` in
`selected_project_ids` (or omit the list to include everything). The connector
calls `GET /project/inbox/data` and resolves the real per-user id
(`inbox<userId>`) from task `projectId` values when querying completed tasks.

## OAuth setup

1. Register an app at [TickTick Developer Center](https://developer.ticktick.com/manage).
2. Set the redirect URI to `http://localhost:8080/callback` (for local use).
3. Request scope `tasks:read` (read-only).
4. Either:

   **a. Pass a token you already have**

   ```bash
   export TICKTICK_ACCESS_TOKEN="…"
   ```

   **b. Run the one-time browser helper** (caches to `.ticktick-token`)

   ```python
   from cognee_community_connector_ticktick import get_ticktick_token

   token = get_ticktick_token(
       client_id="…",  # or TICKTICK_CLIENT_ID
       client_secret="…",  # or TICKTICK_CLIENT_SECRET
   )
   ```

TickTick documents only `grant_type=authorization_code` (no refresh token). When
the token expires, delete `.ticktick-token` (or call `get_ticktick_token(..., force=True)`)
and reconnect.

## API limitations

| Limitation | How the connector handles it |
|---|---|
| No `modifiedTime` listing filter | Full snapshot every run; content-hash avoids re-cognify |
| No delete / change feed | Absence from snapshot → `orphan_cleanup` |
| `/project/{id}/data` = open tasks only | Also call `POST /task/completed` |
| Completed listing caps at 200 | Recursive time-window bisection; abort if unsplittable |
| Inbox missing from `GET /project` | `GET /project/inbox/data` + `inbox<userId>` resolution |
| Comments not in the core Open API | Deferred (not in v1) |

## Testing

```bash
cd packages/connector/ticktick
uv sync --all-extras
uv run pytest tests/ -v
```

The tests mock the TickTick API (no live token) and cover rendering, Inbox
resolution, completed-task window bisection, full-snapshot forget-on-delete
(edit / vanish on re-sync), and document-mode wiring. They require a cognee
build that includes document-mode (see **Requirements**).
