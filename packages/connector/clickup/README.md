# cognee-community-connector-clickup

A ClickUp data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
ClickUp Workspace into memory — "ask my ClickUp".

It exposes a `dlt` source you pass directly to `cognee.remember(...)`. Tasks (with comments) and
Docs are rendered to Markdown and ingested as **normal documents**, so they flow through cognee's
`cognify` entity-extraction and knowledge-graph pipeline rather than the raw relational schema
path.

## Requirements

- **Python**: `>=3.10, <3.15`
- **Cognee**: `>=1.4.0` (requires document-mode `DOCUMENT_SOURCE_ATTR` support)
- **ClickUp**: any plan, with a Personal API token

## Install

```bash
uv pip install cognee-community-connector-clickup
# or, from this monorepo:
cd packages/connector/clickup && uv sync --all-extras
```

## Setup

1. In ClickUp, click your avatar → **Settings** → **Apps** (ClickUp API).
2. Under **API Token**, click **Generate** and copy the token (it starts with `pk_`).
3. Export it, plus your LLM key like any other cognee run:

```bash
export CLICKUP_API_TOKEN="pk_..."
export LLM_API_KEY="sk-..."
```

The token is sent as-is in the `Authorization` header. It can read everything your user can see,
so the selection options below are how you limit what gets ingested.

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_clickup import clickup_source


async def main():
    await cognee.remember(
        clickup_source(),  # CLICKUP_API_TOKEN from env, or pass api_token=...
        dataset_name="clickup_memory",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Which tasks are blocked, and what was decided in their comments?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["clickup_memory"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

See `examples/example.py` for a full runnable script.

## Choosing what to ingest

```python
source = clickup_source(
    team_id="9001",  # Workspace id; optional if the token sees exactly one
    space_ids=["90120001"],  # only these Spaces...
    folder_ids=["90130002"],  # ...or Folders...
    list_ids=["90140003"],  # ...or Lists (tasks, and docs whose parent is selected)
    include_comments=True,  # fold every comment into its task (default True)
    include_closed=True,  # include closed tasks (default True)
    include_docs=True,  # also sync ClickUp Docs (default True)
    detect_deletions=True,  # forget deleted items on the next sync (default True)
    comment_refresh_hours=24,  # re-check comments on unchanged tasks (None to disable)
)
```

Ids are in each item's URL in ClickUp. If the token can access several Workspaces and `team_id`
is missing, the connector stops and lists them instead of guessing.

The source has two tables, both with `primary_key="id"` and `write_disposition="merge"`:

| Table | Row id | One row per |
|---|---|---|
| `clickup_tasks` | `clickup:task:<id>` | task or subtask |
| `clickup_docs` | `clickup:doc:<id>` | Doc (all its pages, nested) |

## What a task document contains

```markdown
**Status:** in progress | **Priority:** high | **Assignees:** Bob
**Tags:** backend, urgent
**Location:** Engineering / Backend / Sprint 42
**Creator:** Alice
**Created:** 2023-07-22 ... | **Updated:** ... | **Due:** ...
**URL:** https://app.clickup.com/t/86abc

## Custom Fields
- **Customer:** Acme
- **Severity:** High

## Description
(Markdown description)

## Checklists
### QA
- [x] Unit tests

## Comments
- **Carol** (2023-11-14 22:13:20 UTC): Looks good
```

Custom fields are decoded by type, so dropdowns and labels show option names rather than ids,
people fields show usernames, and dates, checkboxes, currency, ratings, locations and progress
are shown readably. Docs render each page as a section, with subpages nested one heading level
deeper.

## How sync works

### Hierarchy without re-walking

ClickUp nests Workspace → Space → Folder → List → Task, and each level is a separate call.
Tasks are read Workspace-wide through the filtered team-tasks endpoint
(`GET /team/{team_id}/task`), so the tree never has to be walked to find them. Each task already
carries its list and folder names, so only Space names need a lookup. They are fetched once, cached
in the dlt source state, and refreshed only when a task points at a Space the cache hasn't seen.

### Incremental sync

- **Tasks:** fetched with `date_updated_gt` set to the newest `date_updated` seen so far (Unix
  milliseconds), stored in `dlt.current.resource_state()`. ClickUp treats that filter as
  inclusive, so the task that set the cursor is recognised and skipped. A rerun with nothing
  changed only makes listing requests (four against a real Workspace) and fetches no comments or
  pages.
- **Docs:** ClickUp's v3 Docs API has no update filter, so the doc listing is compared with the
  `date_updated` stored per doc, and pages are fetched only for docs that changed.

Changing the selection (`team_id`, `space_ids`, `folder_ids`, `list_ids`, `include_closed`) resets
the task cursor, so newly selected tasks are backfilled.

### Comments

Adding a comment bumps the task's `date_updated`, so new comments arrive with the next incremental
sync. Deleting a comment does **not** (both behaviours were verified against the live API), so
the cursor alone would keep a deleted comment in memory forever, and edits are not guaranteed to
bump it either. To cover both, every
`comment_refresh_hours` (default 24) the connector re-reads the comments of all tasks in scope,
compares them with a fingerprint stored per task, and re-renders only the tasks whose comments
changed. A refresh costs about one request per task. It reuses the deletion sweep's listing, so it
adds no listing requests, and never runs more often than the interval.

### Forget-on-delete

ClickUp has no deletion feed. Each run re-lists the ids in scope (tasks: about one request per
100 tasks; docs: one per 100 docs) and emits `{"id": ..., "_deleted": True}` for known items that
are gone: deleted, archived, or no longer selected. dlt hard-deletes those rows on merge and
cognee's `orphan_cleanup` removes them from the graph, vector and relational stores. For very
large Workspaces you can skip the extra listing with `detect_deletions=False`.

**Failure safety:** state is written only after a whole run succeeds, so an interrupted run is
simply repeated. Any API error aborts the run instead of being read as "no tasks". If a listing
suddenly comes back empty while items are known, the deletion sweep is skipped rather than wiping
memory. An invalid token raises a clear error.

### Rate limits

ClickUp allows 100 requests per minute per token on most plans. HTTP 429 waits for
`Retry-After`, or until `X-RateLimit-Reset`. 5xx responses and dropped connections are retried
with exponential backoff. The first sync of a large Workspace makes one comment request per task
(more for tasks with over 25 comments), so expect it to take a while; later syncs only touch
changed tasks.

### Limitations

- Threaded comment replies are not fetched. ClickUp returns them through a separate endpoint per
  comment.
- Attachments are not downloaded.
- Docs are listed Workspace-wide. Scoping keeps only docs whose direct parent is one of the
  selected Spaces, Folders or Lists.

## Testing

Run the test suite locally (no ClickUp account needed):

```bash
uv run pytest tests/ -v
```

The tests use a `FakeClickUpSession` that mimics the v2 task API (100-task pages, scope filters,
`date_updated_gt`, 25-comment pages) and the v3 Docs API (cursor paging, nested pages), and cover:

- Auth header, token from env, Workspace selection, and invalid-token errors
- 429 `Retry-After` / `X-RateLimit-Reset` and 5xx/network backoff
- Task rendering (tags, location, subtasks, checklists, comments) and custom-field decoding
- Hierarchy caching: one walk, no re-walk on later syncs, refresh on an unseen Space
- Comment refresh: deleted and edited comments are re-rendered on schedule, unchanged ones are not
- Backfill pagination, the incremental `date_updated_gt` cursor, no-op reruns, and scope changes
- Docs: nested pages, change detection, deletion/archival, scoping, cursor paging
- Tombstones, the outage guard, and state staying untouched on failure
- `DOCUMENT_SOURCE_ATTR` (`document_source_tag(source) == "clickup"`)
- A dlt pipeline against an isolated SQLite staging database, proving `_deleted=True` rows are
  removed from both tables on merge
