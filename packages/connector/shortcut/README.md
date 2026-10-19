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

The package needs `cognee>=1.6.2,<2`: document-mode routing, the table-scoped orphan
cleanup, `PIPELINE_SCOPE_ATTR` and `guarded_rows` all ship in cognee 1.6.2. The tests pass on
1.6.2 and 1.6.4.

## Setup

1. In Shortcut, open **Settings > API Tokens** and create a token. Shortcut tokens are not
   scoped: a token can do whatever its member can. Use a token from a member with a
   read-only (Observer) role.
2. Export it as `SHORTCUT_API_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any
   other cognee run.

The connector only reads. It issues `GET` requests and `POST /api/v3/stories/search`, which
is Shortcut's story query and changes nothing. The token is checked before the first
request: one with whitespace or control characters in it (a pasted newline, say) is refused
with a `ValueError` that does not repeat it.

Ingested text goes to the LLM configured for cognee: cognify sends each document to it for
entity extraction.

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
| `epic_ids=[16, 42]` | Only these epics, the stories in them, and the iterations those stories are planned in. |
| `include_archived=False` | Leave out archived stories and epics. One is forgotten once it is archived. Default `True`: archived stories are kept and marked `Archived: yes`. |
| `token="..."` | API token. Defaults to the `SHORTCUT_API_TOKEN` environment variable. |
| `resource_name="shortcut_documents"` | The dlt table name, which also scopes the sync state. Lowercase snake_case, at most 64 characters. See below. |
| `check_active=None` | Optional callable for a host: asked before every Shortcut request and every wait, and around every row; raising stops the sync with nothing deleted. |
| `service=None` | A pre-built `requests`-like session (mainly for tests); `session=` is accepted too. Without it, each run builds its own session and closes it. |

`group_ids` and `epic_ids` combine with AND: a story must be in one of the teams and in one
of the epics.

**Several sources in one dataset.** Each `resource_name` has its own table and its own sync
state, and a sync only ever forgets documents of its own table. Two `shortcut_source(...)`
calls into one dataset (two workspaces or tokens, or two selections) therefore need two
names, for example `resource_name="shortcut_web"` and `resource_name="shortcut_mobile"`. With
the same name, each run would forget the other's stories. Within one source, narrowing the
selection forgets what fell out of it.

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
an unchanged story keeps its content hash and is not re-cognified. Owners and labels are
sorted by name and comments by id, so the same story always renders the same text.

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
epic and iteration ids, with the ids emitted by earlier runs. The listing ends at Shortcut's
own clock (the `Date` header of its first answer), so a local clock that runs behind cannot
leave new stories out; without a `Date` header the local clock is used and a warning logged. Ids that are gone are emitted
with the `_deleted` hard-delete marker, dlt removes those rows on `merge`, and cognee's
`orphan_cleanup` removes them from the graph and vector stores.

All listings and lookups are completed before the first row is emitted, and state is saved
only after the last one. If a listing or lookup fails (after retries), or `check_active`
raises, the run aborts: nothing is deleted and the cursor does not move, so the next run
starts from the same point. An answer with something other than the expected JSON (an HTML
login page, say) also aborts the run, whether it came from a listing, a window of a split or
one story; it is never read as an empty window or an item to skip.

## When Shortcut fails part of the way through

Only the listings decide that a story or iteration is gone. One that is still listed but
whose own `GET` fails keeps its previous document and is fetched again on the next run; the
rest of the run goes on. Epics and labels come from their listings and have no request of
their own.

- **`403` or `404`** (hidden from this token, or a passing glitch): kept. If it answers that
  way in three runs in a row at the same `updated_at`, its document is forgotten and it is not
  asked about again until its `updated_at` moves. A `403` only counts toward that when another
  item of the same kind was fetched in the same run: if every story (or every iteration)
  answers `403`, the token has lost a permission, nothing is forgotten, a warning says so, and
  the stats count it as `failed_permission`.
- **A server error or no answer** that outlasts the retries: kept. Three of these in a row on
  fresh items means Shortcut itself is down, and the run stops cleanly (below).

Items kept from an earlier run are fetched after the fresh ones and never count toward that
streak, so items that keep failing cannot hold up the others.

Some failures stop the run **cleanly** instead of aborting it: a `401` after at least one item
was emitted, a rate limit that outlasts the retries or asks for a wait over 60 seconds, and
the outage above. The run then ends normally: what it emitted is loaded, nothing is deleted,
and the cursor stays where it was, so the next run lists the same window again and fetches
what this one did not reach, even if nothing changed upstream. Relisting costs nothing extra:
the sweep runs every time anyway, and the `updated_at` listing is one more search request.
A `401` before anything was emitted still raises, since that is a bad token.

Error messages name the status and the endpoint only (`Shortcut /stories/123: HTTP 404`).
Shortcut's own message is on the exception's `detail`; a response body or the token never is.

### Sync stats

`resource.cognee_sync_stats` holds the counts of the last run, as plain ints: `scanned`
(stories listed), `skipped` (unchanged), `failed` (item failures), `deleted`, `kept` (items
waiting for a retry) and `given_up`, plus `failed_auth`, `failed_rate_limit` or
`failed_unavailable` when the run stopped early for that reason, and `failed_permission`
(items answering `403` while nothing of their kind could be read).

## Limits

- Anyone who can read the cognee dataset can read what was ingested, including stories of
  every team the token can see. Give a dataset only to people who may read all of it.
- Two workspaces, tokens or selections in one dataset need different `resource_name`s (see
  above).
- Nothing is cut: every comment of a story is read. The story listing is checked for
  completeness by splitting any window of 2,500 stories or more (see "Large workspaces").
- Story tasks (checklists), attachments, linked files and story relationships are not
  ingested. Comments are rendered flat, in order, without their reply threading.
- A renamed member updates the stories they own. A comment keeps its author's old name
  until that story changes for another reason.
- Epic comments and iteration-level discussion are not read.
- A story or iteration that answers `403` or `404` in three runs in a row is forgotten until
  it changes.
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
- A token of a member with the read-only (Observer) role can run every request the
  connector makes.
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
only on change, story deletion, stories and iterations that fail on their own (kept and
retried, three unreadable ones not blocking a run and given up after three runs, retries
going last), the clean stops (`401` mid-run, rate limit, long `Retry-After`, outage) and
what the next run picks up, the sweep ending at Shortcut's clock, bodies of the wrong shape
(also on the split path), retries, the token never reaching a message or a log line, and the
failure paths that must delete nothing. A real `dlt` pipeline into a temp sqlite destination
checks edits, renames and deletes across runs (the deleted row is gone from the table), two
sources in one dataset staying apart (also for deletions), a story kept and retried, a `401`
mid-run resuming on the next run, and `check_active` stopping a run with nothing loaded.
Rendering is also checked from shuffled input.
