# cognee-community-connector-shortcut

A Shortcut data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Shortcut workspace into memory and ask questions about your stories.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Stories (with their comments),
epics and iterations are ingested as **normal documents**, so they go through cognee's
cognify entity extraction. Re-running syncs only what changed, and anything deleted in
Shortcut is forgotten on the next sync.

## Install

```bash
uv pip install cognee-community-connector-shortcut
# or, from this monorepo:
cd packages/connector/shortcut && uv sync --all-extras
```

The package pins `cognee==1.6.2`: it needs document-mode routing and the table-scoped
orphan cleanup that ships in cognee 1.6.x.

## Setup

1. In Shortcut, open **Settings > API Tokens** and create a token. A **read-only** token is
   enough.
2. Export it as `SHORTCUT_API_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any
   other cognee run.

The connector only reads. It issues `GET` requests and `POST /api/v3/stories/search`, which
is Shortcut's story query and changes nothing.

## Usage

```python
import cognee
from cognee_community_connector_shortcut import shortcut_source

await cognee.remember(
    shortcut_source(),  # SHORTCUT_API_TOKEN from env, whole workspace
    dataset_name="shortcut",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited: orphan cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What is blocking the Q4 launch?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["shortcut"],
)
```

Re-running `remember(...)` with the same dataset syncs only what changed since the last run
and forgets what was deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop every document that is not re-emitted on the second sync.

### Selecting what to ingest

| Argument | Meaning |
|---|---|
| *(none)* | The whole workspace. |
| `group_ids=["<uuid>"]` | Only stories, epics and iterations of these teams (the API calls a team a "group"). |
| `epic_ids=[16, 42]` | Only these epics and the stories in them. Iterations are not narrowed by this. |
| `include_archived=False` | Leave out archived stories and epics. One is forgotten once it is archived. Default `True`: archived stories are kept and marked `Archived: yes`. |
| `token="..."` | API token. Defaults to the `SHORTCUT_API_TOKEN` environment variable. |

`group_ids` and `epic_ids` combine with AND: a story must be in one of the teams and in one
of the epics.

Use **one** `shortcut_source(...)` selection per dataset. The source keeps one set of sync
state per dataset, so two calls with different selections into the same dataset would each
forget the other's stories. Narrowing the selection forgets what fell out of it.

### What a document looks like

One document per story, one per epic and one per iteration:

```text
# Publish the Q4 pricing page

Type: feature
State: In Progress
Owners: Ada Lovelace
Epic: Q4 launch
Iteration: Sprint 12
Labels: marketing
Deadline: 2026-10-20

Marketing needs the new pricing page live before the Berlin launch event.

Comments:
- Grace Hopper: Blocked: waiting on legal to approve the refund policy wording.
```

An epic document is its state, deadline and description; an iteration document is its
status, dates and description. Timestamps and counters are kept out of the text on purpose:
an unchanged story keeps its content hash and is not re-cognified.

## How incremental sync works

Three things are kept in dlt resource state: an `updated_at` cursor, the ids emitted so far,
and a short fingerprint per story.

- **The `updated_at` cursor.** Each run asks `POST /stories/search` for stories with
  `updated_at_start` set to 60 seconds before the newest `updated_at` seen so far, and
  fetches only those in full with `GET /stories/{id}`. Comments come embedded in the story,
  and adding, editing or deleting one moves the story's `updated_at`. Stories inside the
  60-second overlap that were already rendered are remembered and skipped.
- **One-second timestamps.** Shortcut reports `updated_at` in whole seconds, so two edits
  within one second look the same. A story is only remembered as "already rendered" if it
  was fetched at least two seconds after its `updated_at`, judged by the `Date` header of
  Shortcut's own response. Otherwise it is fetched once more on the next run.
- **The header fingerprint.** A story document shows its workflow state, owners, epic,
  iteration and labels by name. Renaming or deleting an epic, iteration, label or state, or
  renaming a member, does not move any story's `updated_at`. The story listing carries the
  ids behind those names for every story, so the connector hashes the header lines each
  story would show and re-renders a story when that hash differs from the stored one.

Epics come complete in one list request and are re-emitted every run. The iteration list
has no descriptions, so an iteration costs one request, but only when the list shows that it
is new, was edited, or changed status.

A run where nothing changed costs six requests and fetches no story and no iteration. Each
changed story or iteration costs one more.

### What a rename costs

Renaming something that many stories show re-renders all of them on the next run, one
`GET /stories/{id}` each: every story in a renamed workflow state, every story carrying a
renamed label. Shortcut allows 200 requests per minute, so 1,000 affected stories take at
least five minutes. There is no extra LLM cost beyond those stories: only documents whose
text changed are re-cognified.

### Large workspaces

`POST /stories/search` has no pagination. No result limit was found (see below), but none is
documented either, so a listing that returns 2,500 stories or more is not trusted: its date
window is cut at the median timestamp of what came back and both parts are asked for again,
until every part is under 2,500. If a single second still holds that many stories the run
stops with an error instead of risking a truncated list; narrowing the selection with
`group_ids` or `epic_ids` gets past it.

The guard has a price on a workspace above 2,500 stories: on every run the first, oversized
response of the story listing is thrown away and the range is asked for again in parts. On
the 2,611-story test workspace that listing took 3 requests instead of 1, the discarded one
being the slowest at about 3 seconds. The parts are not cached between runs.

A first sync fetches every story once: about 13 minutes per 2,600 stories at 200 requests
per minute.

## How forget-on-delete works

Every run lists all in-scope stories by `created_at` and compares the ids, plus the current
epic and iteration ids, with the ids emitted by earlier runs. Ids that are gone are emitted
with the `_deleted` hard-delete marker, dlt removes those rows on `merge`, and cognee's
`orphan_cleanup` removes them from the graph and vector stores.

All listings and lookups are completed before the first row is emitted, and state is saved
only after the last one. If any request fails (after retries), the run aborts: nothing is
deleted and the cursor does not move, so the next run starts from the same point.

## Limits

- Story tasks (checklists), attachments, linked files and story relationships are not
  ingested. Comments are rendered flat, in order, without their reply threading.
- A renamed member updates the stories they own. A comment keeps its author's old name
  until that story changes for another reason.
- Epic comments and iteration-level discussion are not read.
- Deletions propagate on a foreground `remember(...)`. cognee skips orphan cleanup for
  `run_in_background=True`.

## Verified against a real workspace vs. taken from the docs

Checked against a free Shortcut workspace on 2026-10-09 and 2026-10-10:

- **No result limit on `POST /stories/search` up to 2,609 stories.** With 2,609 stories in
  the workspace, one request returned all 2,609 (also checked at 1,109). Nothing larger was
  tested, so this is not a claim that the endpoint is unlimited. The ranked
  `GET /search/stories` endpoint, which stops at 1,000 results, is not used.
- The endpoint answers `201`, accepts `updated_at_start` on its own, rejects `page_size`
  (`disallowed-key`), and returns an empty list for an empty body.
- Date filters include both ends and compare below the one-second resolution that
  timestamps are displayed in. A story stamped exactly on a second came back from both of
  two adjacent windows; the connector keeps it once.
- The window splitting, run with the limit lowered to 1,000 and to 300 on 2,611 stories,
  returned exactly the same ids as the unsplit listing.
- Adding, editing and deleting a comment each move the story's `updated_at`. A deleted
  comment stays in `comments` with `deleted: true` and no text.
- An archived story stays in the listing with `archived: true` when the `archived` filter is
  left out. A deleted story disappears from it and its `GET` returns 404.
- Renaming an epic, iteration or label, and deleting an epic, iteration or label, leave the
  `updated_at` of the stories that used it untouched.
- Editing an iteration's description, name or dates moves its `updated_at` in
  `GET /iterations`; adding a story to it does not.
- A read-only token can run every request the connector makes.
- Responses carry a `Date` header.
- The connector itself, through a dlt pipeline: first sync, no-change syncs (no story
  fetched), an edited description, a comment added, edited and deleted, an epic renamed, a
  label deleted, an iteration deleted, a story created and then deleted. Separately for
  iterations: created, a no-change sync (not fetched, document kept), description edited,
  deleted.

- `examples/example.py` end to end, twice, scoped to one epic: ingest and search, then a
  comment edit, an epic rename and a story deletion upstream. The second run re-synced one
  story and cognee's orphan cleanup removed the deleted one (3 documents, then 2). LLM: Groq
  `openai/gpt-oss-120b` through cognee's `custom` provider, with local `fastembed`
  embeddings.

Taken from Shortcut's documentation, not reproduced live:

- The limit of 200 requests per minute and the `429` response with `Retry-After`. A burst of
  400 requests in 30 seconds was answered without a single `429`, so the retry path is
  covered by the mocked tests only.

## Testing

```bash
uv run pytest tests/ -q
```

The tests mock the Shortcut API (no token, no network). They cover rendering, the header
fingerprint, the date-window splitting, the cursor and its overlap, one-second timestamps,
renames and deletions of epics, iterations, labels, states and owners, iterations fetched
only on change, story deletion, retries, the failure paths that must delete nothing (during
a listing and in the middle of rendering), and a real `dlt` merge into a temp sqlite
destination across three runs.
