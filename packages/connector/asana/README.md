# cognee-community-connector-asana

An Asana data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Asana projects into memory and ask questions about your tasks.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Tasks (with their comments and
subtasks) and project descriptions are ingested as **normal documents**, so they go
through cognee's cognify entity extraction. Re-running syncs only what changed, and anything
deleted in Asana is forgotten on the next sync.

## Install

```bash
uv pip install cognee-community-connector-asana
# or, from this monorepo:
cd packages/connector/asana && uv sync --all-extras
```

The package pins `cognee==1.6.2`: it needs document-mode routing and the table-scoped
orphan cleanup that ships in cognee 1.6.x.

## Setup

1. In Asana, open **Settings > Apps > Developer apps** (or
   https://app.asana.com/0/my-apps) and create a **personal access token**.
2. Export it as `ASANA_ACCESS_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any
   other cognee run.
3. Find the gid of each project to ingest: it is the number in the project URL,
   `https://app.asana.com/0/<project_gid>/...`.

The connector is read-only. It only issues `GET` requests.

## Usage

```python
import cognee
from cognee_community_connector_asana import asana_source

await cognee.remember(
    asana_source(project_gids=["1201234567890123"]),  # ASANA_ACCESS_TOKEN from env
    dataset_name="asana",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited: orphan cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What is blocking the website launch?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["asana"],
)
```

Re-running `remember(...)` with the same dataset syncs only what changed since the last run
and forgets what was deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop every document that is not re-emitted on the second sync.

### Selecting what to ingest

| Argument | Meaning |
|---|---|
| `project_gids=[...]` | Ingest these projects. |
| `workspace_gid="..."` | Ingest every project of the workspace. Used only when `project_gids` is omitted. |
| `include_completed=False` | Only incomplete tasks. A task is forgotten once it is completed. Default `True`. |
| `token="..."` | Access token. Defaults to the `ASANA_ACCESS_TOKEN` environment variable. |

A project gid that no longer exists (or that the token can no longer see) makes Asana
answer with an error, and the run aborts without deleting anything. That is deliberate: a
permissions hiccup must not look like "every task was deleted". To forget a deleted
project, remove its gid from `project_gids` and sync again.

Pass all projects for a dataset in **one** `asana_source(...)` call. The source keeps one set
of sync state per dataset, so two calls with different projects into the same dataset would
each forget the other's tasks. Removing a project from the selection forgets its documents.

### What a document looks like

One document per task, plus one per project (its name and description):

```text
# Write launch post

Completed: no
Assignee: Ada Lovelace
Due: 2026-10-20
Project: Website launch (section: In progress)
Priority: High

Draft the post, then review with marketing.

Subtasks:
- [x] Outline
- [ ] Screenshots
  Assignee: Grace Hopper
  Due: 2026-10-18
  Notes: Desktop and mobile, light theme only.

Comments:
- Grace Hopper: Use the new palette for the header image.
```

A task that sits in two selected projects is one document. Timestamps and counters are kept
out of the text on purpose: an unchanged task keeps its content hash and is not re-cognified.

## How incremental sync works

Per project, two signals are kept in dlt resource state:

- **A `modified_since` cursor.** `GET /tasks?project=...&modified_since=<cursor>` returns
  tasks whose own fields changed, and tasks that gained or lost a comment. The listing
  starts 60 seconds before the cursor so that a task stamped in the same millisecond as
  the cursor, after the previous listing ran, is not missed. Tasks already seen inside that
  window are remembered and skipped, so the overlap costs no extra task fetches.
- **An Events API sync token.** Some changes do not move a task's `modified_at`: editing an
  existing comment, and changing or deleting a subtask. Those only show up in
  `GET /events`, so the connector polls it and re-renders the tasks the events point at.

Renaming the project or one of its sections changes no task, but every task document names
its project and section. When the events report such a rename, the connector re-renders
every task of that project on that run (same cost as below).

Each changed task costs three requests (task, comments, subtasks). A run where nothing
changed costs four requests per project and fetches no task.

### Syncing less often than once a day

Asana's sync tokens expire after about 24 hours. If more time than that passes between two
syncs, Asana answers `412` on `/events` and the events in between are lost. Nothing else
can tell which comments were edited or which subtasks were renamed in the gap, so **that
run re-renders every task of that project**, as a first sync does.

What that costs, for a project with `N` tasks:

- `3 x N` requests for the tasks, plus two listing requests per 100 tasks and two more per
  project. A 500-task project is about 1,500 requests.
- On Asana's free plan (150 requests per minute) that is roughly 50 tasks per minute, so
  about 10 minutes for 500 tasks. The connector waits out `429` responses, so the run gets
  slower rather than failing.
- No extra LLM cost: a task whose text did not change keeps its content hash and is not
  re-cognified.

To stay incremental, sync each dataset at least once a day.

## How forget-on-delete works

Every run lists the gids currently in each selected project and compares them with the ids
emitted by earlier runs. Ids that are gone are emitted with the `_deleted` hard-delete
marker, dlt removes those rows on `merge`, and cognee's `orphan_cleanup` removes them from
the graph and vector stores.

All listings are completed before the first row is emitted, and state is saved only after
the last one. If a listing fails (after retries), the run aborts: nothing is deleted and no
cursor or token moves, so the next run starts from the same point.

## Limits

- Subtasks are folded into the parent task's document (title, completion, assignee, due
  date and notes). They are not documents of their own, comments on subtasks are not
  ingested, and subtasks of subtasks are not read.
- Only plain-text notes and comments are read; attachments are not.
- Events can take a few seconds to appear in Asana's feed. A change made moments before a
  sync may be picked up by the following one.
- A first sync, and a sync more than about a day after the previous one, fetch every task
  (see "Syncing less often than once a day").
- Deletions propagate on a foreground `remember(...)`. cognee skips orphan cleanup for
  `run_in_background=True`.

## Verified against a real workspace vs. taken from the docs

Checked against a free Asana workspace on 2026-10-09:

- Adding a comment bumps the task's `modified_at`; `modified_since` returns it.
- Editing a comment does **not** bump `modified_at`. The event arrives with `parent: null`;
  the task is identified through `resource.target`, requested with `opt_fields`.
- Renaming or deleting a subtask does not bump the parent, and the event carries no parent.
  Adding a subtask does bump the parent.
- A deleted task leaves the project listing immediately and its `GET` returns 404.
- `modified_since` is inclusive.
- `/events` without a token, or with an invalid one, returns 412 with
  `{"errors": [...], "sync": "<token>"}`; a normal response has `data`, `sync`, `has_more`.
- An invalid `offset` returns 400.
- `completed_since=now` lists only incomplete tasks.
- The connector itself, run through a dlt pipeline: first sync, a no-change sync (no task
  re-fetched), a comment-only edit, a deleted task, a subtask whose notes were edited, a
  renamed subtask, a deleted subtask, and a renamed section (every task re-rendered with the
  new name, then incremental again).
- A sweep spread over several pages (page size forced to 2; three pages for six tasks),
  following only the returned `offset`.
- A task in two selected projects comes out as one row and is fetched once.
- `workspace_gid` selection yields the same documents as listing the projects explicitly.
- Custom fields (`Priority`, `Status`) are rendered from `display_value`.
- Renaming a section or the project arrives as a `changed` event with `change.field` set to
  `name`, and bumps no task's `modified_at`. Editing the project description arrives as a
  `changed` event with field `notes`.
- `examples/example.py` end to end, twice: ingest and search, then a comment edit and a
  task deletion upstream. The second run re-synced one task and cognee's orphan cleanup
  removed the deleted one (6 documents, then 5). LLM: Groq `openai/gpt-oss-120b` through
  cognee's `custom` provider, with local `fastembed` embeddings.

Taken from Asana's documentation, not reproduced live:

- Sync tokens expiring after about 24 hours, and pagination offsets expiring. (A missing
  or invalid token and an invalid offset were reproduced; waiting out a real expiry was not.)
- `429` responses with `Retry-After` (the rate limit was never hit).

Both are covered by the mocked tests.

## Testing

```bash
uv run pytest tests/ -q
```

The tests mock the Asana API (no token, no network). They cover rendering, the cursor and
its overlap, the events paths (edited comment, renamed and deleted subtask, 412), deletion, pagination and
retries, the failure paths that must delete nothing, and a real `dlt` merge into a temp
sqlite destination across two runs.
