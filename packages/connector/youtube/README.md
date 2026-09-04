# cognee-community-connector-youtube

[YouTube](https://youtube.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee) — sync a channel's public
uploads (titles, descriptions, metadata) into memory, incrementally and with
forget-on-delete. "Ask my channel."

Part of the cognee hackathon connector push (topoteretes/cognee#4808).

## Features

- **Incremental sync** — the first run is a full backfill; every later run
  re-emits only uploads published after the newest `publishedAt` already
  synced.
- **Forget-on-delete** — the YouTube Data API has no deletion feed, so each
  run diffs the full video-id set against the previous run (kept in dlt
  resource state). Vanished videos — deleted, or made private/unlisted — are
  hard-deleted and then purged by cognee's orphan cleanup.
- **Deterministic scope** — a channel's uploads playlist is derived directly
  (`UC…` → `UU…`), no extra API call; additional playlists can be listed via
  `playlist_ids`.
- **Document ingestion** — each row becomes a normal text document that flows
  through cognee's cognify entity-extraction pipeline (same treatment as the
  Notion / Google Drive connectors).
- **Retries with backoff** — rate-limit (429), server (5xx), timeout, and
  network errors are retried; quota/auth failures (403) fail fast.

## Installation

```bash
pip install "cognee[youtube]"
# or, from this repository:
uv pip install ./packages/connector/youtube
```

## Setup

1. Create an API key in [Google Cloud Console](https://console.cloud.google.com/)
   (enable the *YouTube Data API v3*, then Credentials → API key).
2. Export it:

   ```bash
   export YOUTUBE_API_KEY="..."
   export YOUTUBE_CHANNEL_ID="UC..."   # optional
   ```

## Usage

```python
import cognee
from cognee_community_connector_youtube import youtube_source

await cognee.remember(
    youtube_source(channel_id="UC...", playlist_ids=["PL..."]),
    dataset_name="my_channel",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see below
    max_rows_per_table=0,  # REQUIRED for a real channel
)

answer = await cognee.recall("What did I publish about feature X?")
```

> **`write_disposition="merge"` is mandatory.** The add pipeline defaults to
> `"replace"` (drop + reload the table each run); on the second, incremental
> sync that would wipe everything but the small delta.

> **`max_rows_per_table=0`** lifts cognee's 50-row read cap so orphan cleanup
> compares against the *whole* synced corpus.

A runnable version lives in [`examples/example.py`](examples/example.py).

## What gets ingested

| Entity | Kind    | Document                                            |
| ------ | ------- | --------------------------------------------------- |
| Video  | `video` | title + description + publish date                  |

## Captions (deliberately out of scope)

The issue's "watch out for" applies: `captions.download` requires OAuth even
for public videos, which API-key auth cannot provide. This connector therefore
ingests video metadata and descriptions; caption fetch is left for a
follow-up with an OAuth path.

## Deleting data

- Delete a video (or make it private/unlisted) upstream → it is removed from
  cognee's graph, vector, and relational stores on the next sync (dlt
  hard-delete + cognee's orphan cleanup).
- Delete the whole dataset locally with `cognee.forget(...)` /
  `cognee.prune()`.

## Tests

```bash
uv run pytest packages/connector/youtube/tests -q
```

All tests run offline: the YouTube API is faked, and the dlt pipeline runs
against a temporary SQLite destination.

## Notes & limitations

- With an API key only **public** videos are visible (a YouTube platform
  constraint). OAuth-based private access is out of scope for this connector.
- YouTube metadata (title/description) edits do not bump `publishedAt`, so the
  incremental cursor surfaces *new uploads*; to pick up description edits,
  delete the dataset and re-backfill (or run with a fresh state).
- List calls cost 1 quota unit each; a full sweep of a large channel re-lists
  every page each run.
