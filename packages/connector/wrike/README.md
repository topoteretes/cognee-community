# cognee-community-connector-wrike

A Wrike data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync Wrike folders, projects and tasks into memory — "ask my Wrike".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). Every task and
folder becomes a **document** that goes through normal cognify, with **incremental re-sync**
(`updatedDate` cursor, `merge` write disposition) and **forget-on-delete** (tasks deleted, moved
out of scope or removed with their folder are forgotten on the next sync).

## Requirements

`cognee==1.6.4`, pinned in `pyproject.toml`. It ships the document-source marker, per-dataset
pipeline state, and orphan cleanup that forgets a table's last document.

## Install

```bash
uv pip install cognee-community-connector-wrike
# or, from this monorepo:
cd packages/connector/wrike && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_wrike import wrike_source

await cognee.remember(
    wrike_source(folder_ids=["MQAAAAERJAK1"]),  # WRIKE_ACCESS_TOKEN from env
    dataset_name="wrike",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see note below
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
    self_improvement=False,  # skip the improve() LLM pass on every re-sync
)

answer = await cognee.recall("What was decided about the database schema?", datasets=["wrike"])
```

`folder_ids` takes folders, projects or spaces and includes everything below them; omit it to
sync everything the token can see. Re-running `remember(...)` with the same dataset syncs only
what changed since the last run. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`, which
> would drop the synced data and re-extract everything on every run.

Folder ids are Wrike API ids, not the numbers in Wrike's URLs. List them with:

```bash
curl -s -H "Authorization: bearer $WRIKE_ACCESS_TOKEN" https://www.wrike.com/api/v4/folders \
  | python3 -c 'import json,sys; [print(f["id"], f["title"]) for f in json.load(sys.stdin)["data"] if f["scope"] == "WsFolder"]'
```

## How sync + forget-on-delete work

The cursor (the time the last run started) and the ids already ingested are kept in dlt's
per-resource state, separately for each dataset. Every run lists the ids of all tasks in scope
(one light request per 1,000 tasks) and fetches full content only for tasks changed since the
cursor, new to the listing, or shown under a folder path or parent-task title that changed (Wrike
does not mark the tasks below a renamed or moved folder, or the subtasks of a renamed task, as
updated); the first run fetches everything. Anything ingested but no longer
listed is emitted as a `_deleted` hard-delete row, which dlt drops on `merge` and cognee's
`orphan_cleanup` removes from the graph, vector and relational stores. That covers a task that
was deleted (even purged from the Recycle Bin), moved out of the selected folders, removed with
its folder, or is no longer visible to the token. The Recycle Bin alone cannot: a deleted
folder's tasks even keep the folder as their parent.

Comments are part of their task's document; adding, editing or deleting one updates the task.
Comments are read one request per task (`/tasks/{ids}/comments` takes one id, and the
account-wide `/comments` accepts at most a 7-day range), so a first run on a large account takes a
while: Wrike allows about 400 requests a minute, and rate-limited requests are retried.
State is saved only at the end of a successful run: a failed run raises, deletes nothing, and the
next run starts from the same cursor. `full_resync=True` re-fetches and re-renders everything.

## What is ingested

One document per task and per folder. A task's document holds its folder path or parent task,
status, assignees, importance, dates, custom fields, description and comments:

```
# Set up database

Folder: Engineering / Backend
Status: In Progress
Assignees: Ana Lee
Importance: Normal
Priority: High

Create Postgres schema for users and orders.

Comments:
- Ana Lee (2026-10-12): Use UUID primary keys please.
```

Wrike returns statuses, people and custom fields as ids; they are stored by name. Custom fields
are shown as Wrike shows them: contacts as names, currency with its code, percentages with `%`,
durations in hours, checkboxes as Yes/No, rich text as plain text. Descriptions and comments are
converted from HTML to text. The same task always renders to the same text, so re-syncing an
unchanged task does not re-cognify it.

## Setup

**Permanent token** (simplest):

1. In Wrike, open **Apps & Integrations → API** (https://www.wrike.com/frontend/apps/index.html#/api),
   create an app and create a permanent access token.
2. Set it as `WRIKE_ACCESS_TOKEN` (or pass `access_token=...`), plus your `LLM_API_KEY` like any
   other cognee run. If your Wrike address bar does not show `www.wrike.com`, set that host
   as `WRIKE_HOST` (e.g. `app-eu.wrike.com`).

**OAuth 2.0** (Wrike's recommended method for production):

1. In the same app, note the client id and secret and add `http://localhost` as a redirect URL.
2. Open `https://login.wrike.com/oauth2/authorize/v4?client_id=<id>&response_type=code&redirect_uri=http://localhost`,
   approve, and copy `code` from the URL the browser lands on (the page itself will not load).
3. Exchange the code for tokens within a few minutes (a code works once):

   ```bash
   (umask 077; curl -s -X POST https://login.wrike.com/oauth2/token \
     -d client_id=<id> -d client_secret=<secret> -d grant_type=authorization_code \
     -d code=<code> -d redirect_uri=http://localhost -o wrike_token.json)
   ```

4. Pass `client_id`, `client_secret` and `token_path="wrike_token.json"` (or set
   `WRIKE_CLIENT_ID`, `WRIKE_CLIENT_SECRET`, `WRIKE_TOKEN_PATH`). Each run refreshes the token
   and writes the new one back to that file before syncing, because Wrike invalidates the old
   refresh token on every refresh. The data centre comes from the token response.

`WRIKE_FOLDER_IDS` (comma-separated) can replace `folder_ids`. The connector only reads from
Wrike; its only write is to the token file. It is strictly opt-in — nothing is fetched until you
construct a source and call `remember`. Keep the token and token file private, and use a
dedicated dataset so you can wipe it with a single `cognee.forget`.

## Limitations

- Renaming a status, a person or a custom field updates the documents that show it when they
  next change, or on `full_resync=True`.
- Attachments are not ingested. *Link to database* custom fields are left out: the API returns
  only record ids, which it cannot resolve to names.
- Two syncs must not share one OAuth token file at the same time: the first refresh invalidates
  the token the second one read.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Wrike API (no live token). They cover the client (paging, retries, OAuth
rotation), rendering of every custom field type, scope, the incremental cursor and every delete
path against an in-memory Wrike account, and an end-to-end run through `cognee.remember` with a
mocked LLM that proves a deleted task's entity leaves the graph.
