# cognee-community-connector-hackmd

A HackMD data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your
meeting notes, specs, RFCs and incident write-ups into memory, so you can ask "what did we
decide about the auth migration in last week's sync?" across a personal workspace and your
team workspaces at once.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every note becomes one
document (its workspace, folder path and tags, then its markdown), flowing through cognee's
normal cognify entity extraction.

## Install

```bash
uv pip install cognee-community-connector-hackmd
# or, from this monorepo:
cd packages/connector/hackmd && uv sync
```

## Setup

1. In HackMD, open **Settings > API** and click **Create API token**.
2. Pass it as `token=`. The example reads it from `HACKMD_API_TOKEN`, together with your
   `LLM_API_KEY` like any other cognee run:

```bash
export HACKMD_API_TOKEN="..."
```

A team workspace is selected by its path, the part after `/team/` in its URL
(`hackmd.io/team/my-team` has the path `my-team`).

## Usage

```python
import os

import cognee
from cognee_community_connector_hackmd import hackmd_source

await cognee.remember(
    hackmd_source(token=os.environ["HACKMD_API_TOKEN"], team_paths=["my-team"]),
    dataset_name="hackmd",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("Which RFC introduced rate limiting?", datasets=["hackmd"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `personal` | `True` | Sync the notes of the token's personal workspace. |
| `team_paths` | none | Team workspaces to sync, by path. A team that cannot be read on its first sync raises (most likely a typo). |
| `tags` | none | Only notes carrying at least one of these tags (exact match). |
| `folders` | none | Only notes inside a folder with one of these names, at any depth (`folders=["RFCs"]` also takes `RFCs / 2026`). |
| `base_url` | `https://api.hackmd.io/v1` | API address. |
| `max_requests` | `100` | Request budget of one run, one HackMD rate-limit window. A bigger workspace finishes over several runs. |
| `resource_name` | derived | Staging table, which scopes this sync's state and cleanup: lowercase letters, digits and underscores, up to 64. Derived from the workspaces (`hackmd_me`, `hackmd_me_my_team`, ...), plus a short hash of the filters when `tags` or `folders` is set, so two filtered syncs into one dataset never delete each other's notes. |

Selecting nothing (`personal=False` and no `team_paths`) raises, rather than reading as
"every note was deleted".

## What a note looks like

```
Workspace: Team my-team
Folder: RFCs / 2026
Tags: rfc, gateway

# RFC: Rate limiting

The API gateway gets a token bucket of 100 requests per minute.
```

The document's title is the note's title and its URL the note's link. Who edited it last
and when is left out of the text, so an edit by itself never re-processes a note whose
text did not change.

## HackMD's API quota

HackMD counts every request against a monthly quota, 400 calls a month per workspace on
the free plan and 10,000 on Prime, with a rate limit of 100 calls every 5 minutes. A
request to a team's path uses that team's quota. The connector keeps the count low:

- A run lists each selected workspace with **one request** (the listings are not
  paginated and carry each note's `lastChangedAt`). A run with no changes costs one
  request per workspace.
- A note's content is fetched (one request) only when its `lastChangedAt`, title, tags,
  folder or link changed since the last run.

A first sync therefore costs one request per workspace plus one per note. On the free
plan that is about 390 notes per workspace in the first month; a bigger workspace
finishes over several months, or on Prime.

## How sync and forget-on-delete work

- **Incremental.** A note whose rendered text came out the same as last time (for
  example a note edited and changed back) is not emitted again.
- **Forget-on-delete.** A note missing from its workspace's listing (deleted, which moves
  it to the trash, or moved to another workspace), a note that no longer matches `tags`
  or `folders`, and every note of a workspace that is no longer selected are removed from
  memory on the next sync. They are decided right after the listings, before any content
  is read, so a run cut short later still forgets them. A note moved between two selected
  workspaces is forgotten in one and added in the other.
- **Access removed is not deletion.** A team that answers 403 or 404 (the token lost
  access, or left the team) and a note that answers 403 are left unchanged and counted in
  `unreadable`. A listing that comes back empty while notes were synced before forgets
  nothing until the next run confirms it, and a listing that is not a list raises.
- **A run is bounded.** A used-up quota (HTTP 429 `quota_exceeded`) stops the run at once
  with `failed` and `failed_quota`, since a retry would only use more of it. Other 429s
  and 5xx are retried, honoring `Retry-After`; a `Retry-After` over 60 seconds, a 429
  that outlasts the retries, or more than five minutes of retry waits stops the run with
  `failed` and `failed_rate_limit`. A spent `max_requests` budget is a planned pause and
  sets only `failed_budget`. A token rejected after the run's first request stops it
  with `failed` and `failed_auth`; a token rejected on the first request raises. In every
  stop the finished notes are kept and the next run starts after the last one, and any
  other error raises so dlt keeps the previous state.

`cognee_sync_stats` always carries `scanned`, `emitted`, `unchanged`, `deleted` and `gone`
notes, `unreadable` workspaces and notes, `skipped` malformed listing entries, and
`failed` plus `failed_auth`, `failed_budget`, `failed_quota` and `failed_rate_limit`.

Note comments are not ingested: adding a comment does not move a note's `lastChangedAt`,
so keeping them current would cost a request per note on every run.

## Privacy

This reads every note the token can see in the selected workspaces, **private notes
included**. Nothing is fetched until you call `remember`, and anyone with read access to
the target dataset can read what was ingested, so keep HackMD in its own dataset. The
token is only sent to `base_url`, stays in memory, and is never written into documents,
state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no HackMD account. They run the client against a mocked HTTP transport
(retries and their limits, the quota answer, errors without the token or body), check note
rendering, drive the sync against a fake HackMD API shaped like the live one (changed,
touched, renamed, retagged and deleted notes, filters, deselected workspaces, a note moved
between workspaces, access removed, empty and malformed answers, quota, rate limit, revoked
token and budget stops, `check_active`) and through a real dlt pipeline (two filtered
sources in one dataset, failed runs), and run `cognee.remember` end to end to prove a
deleted note leaves the graph.
