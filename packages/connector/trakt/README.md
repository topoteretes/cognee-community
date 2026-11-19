# cognee-community-connector-trakt

A Trakt data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the movies and shows you watched, your ratings, your watchlist and your
personal lists into memory, so you can ask "what shows was I watching recently,
and where did I leave off?" — or "which of the movies I rated 9 did I never
actually finish?"

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each watched
movie, watched episode, show (with its episode timeline), personal list and
rating of an unwatched item becomes one document and goes through cognee's
normal cognify pipeline via the document-mode marker, the same way the Notion
and Telegram connectors work.

## Requirements

Python 3.10 to 3.13 and cognee 1.6.3 or later (below 2). Older 1.4 releases skip
orphan cleanup once a table becomes empty, so the last deleted record would stay
in memory. 1.6.3 handles that case.

## Install

```bash
uv pip install cognee-community-connector-trakt
# or, from this monorepo:
cd packages/connector/trakt && uv sync
```

## Setup: the OAuth PIN flow

1. Create a Trakt API app at
   [app.trakt.tv/settings/apps](https://app.trakt.tv/settings/apps/api/new).
   Use the redirect URI `urn:ietf:wg:oauth:2.0:oob` (out of band). Copy the
   **client id** and **client secret**.
2. Turn a PIN into a token with the built-in helper:

   ```python
   from cognee_community_connector_trakt import trakt_pin_flow

   trakt_pin_flow("YOUR_CLIENT_ID", "YOUR_CLIENT_SECRET")
   ```

   It prints the authorize URL
   (`https://trakt.tv/oauth/authorize?response_type=pin&client_id=...`) — open
   it, approve the app, copy the PIN back into the prompt — and then the token
   JSON: `access_token`, `refresh_token`, `expires_in`, `created_at`.
3. Pass the token (and, to enable automatic refresh, the refresh material) to
   the source. The example reads them from `TRAKT_ACCESS_TOKEN`,
   `TRAKT_REFRESH_TOKEN`, `TRAKT_CLIENT_SECRET`, `TRAKT_TOKEN_CREATED_AT` and
   `TRAKT_TOKEN_EXPIRES_IN`.
4. Set `LLM_API_KEY` as for any cognee run.

The token and the client secret are only ever sent to `api.trakt.tv` (the
secret only to `trakt.tv/oauth/token`); they are never logged, never in an
error and never in a repr.

## Usage

```python
import os

import cognee
from cognee_community_connector_trakt import trakt_source

await cognee.remember(
    trakt_source(
        client_id=os.environ["TRAKT_CLIENT_ID"],
        access_token=os.environ["TRAKT_ACCESS_TOKEN"],
        # optional, enables automatic refresh of an expired access token:
        refresh_token=os.environ.get("TRAKT_REFRESH_TOKEN"),
        client_secret=os.environ.get("TRAKT_CLIENT_SECRET"),
        token_created_at=int(os.environ["TRAKT_TOKEN_CREATED_AT"]),
        token_expires_in=int(os.environ["TRAKT_TOKEN_EXPIRES_IN"]),
    ),
    dataset_name="trakt",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "What shows was I watching recently, and where did I leave off?",
    datasets=["trakt"],
)
```

`write_disposition="merge"` is required: incremental runs emit only the
sections that changed (plus deletions), so the pipeline default (`replace`)
would wipe earlier syncs on the second run. The source refuses a non-merge
disposition before reading anything.

## What gets synced

| Document | Key | Content |
|---|---|---|
| Watched movie | `movie:<trakt id>` | title/year, runtime, released, my rating, plays + last watched, my comment, watchlist marker, my lists, overview |
| Watched episode | `episode:<trakt id>` | show, `S2E4 "Title"`, plays + last watched, my rating |
| Watched show | `show:<trakt id>` | overview + the full watched-episode timeline, sorted by watch date |
| Personal list | `list:<slug>` | ordered roster (rank, added date) of all media types |
| Rating of an unwatched item | `<kind>:<trakt id>` | the rating, so it is never lost |

Episode and show documents carry `cognee_node_set`s (`show:<slug>`,
`season:<slug>-s<N>`; movies and lists link through `movie:`/`list:` sets), so
every episode of a show links to it in cognee's graph without LLM extraction.

## How sync works

| Concern | Behaviour |
|---|---|
| Incremental | Each run calls `GET /sync/last_activities` and compares the per-section timestamps with the snapshot in dlt state. Only sections whose timestamp moved are re-pulled (the first run pulls everything). Ratings, comments, watchlist and lists also appear *inside* movie/show documents, so when one of those moves, the watched listings are re-pulled too and the documents re-render — deterministically, so unchanged items keep their content hash and are not re-cognified. |
| Episode identity | The watched-shows response carries season/episode numbers but no ids or titles, so for each watched show the connector reads `GET /shows/:id/seasons?extended=episodes` once (one request per show) to map every watched episode to its Trakt id and title. The episode rating you left on `/sync/ratings/episodes` lands in the same document through that id. |
| Forget-on-delete | The watched listings are the whole truth about history: an item known from state but absent from a re-pulled listing is emitted with the `_deleted` hard-delete marker and cognee's `orphan_cleanup` forgets it. An unchanged section is never re-listed, so its items are never deleted. A key is deleted only when no other section still claims it — a movie you unwatched but still rated keeps its rating document. |
| Rate limits | Trakt answers `429` with a `Retry-After` header. Waits up to 60s are slept through (up to 5 attempts); longer ones end the run cleanly: no rows, no state change, `cognee_sync_stats["failed_rate_limit"] == 1`, and the next sync resumes. `max_requests=` optionally budgets the run's GET calls the same way. Pagination follows the `X-Pagination-Page-Count` response header, never the requested limit. |
| Token refresh | When `created_at + expires_in` has passed (with a minute of margin), the client refreshes the access token itself using the refresh token, and the new material is persisted in the sync state so later runs keep refreshing. Current Trakt tokens are short-lived (the docs say ~7 days), so pass the refresh material. |
| Several accounts | The sync state records the Trakt username it belongs to and a run by a different user is refused. Pass `username=` to also give the account its own table (`trakt_<username>`) and pipeline scope; keep it fixed across runs. |
| Stats | `source.cognee_sync_stats` holds counts only: `items`, `deletions`, `skipped`, `requests`, `failed`, `failed_auth`, `failed_rate_limit` (and `failed_budget`). |

## Limitations (honest ones)

- **Watched listings are re-listed fully** whenever they (or anything embedded
  in their documents) changed; Trakt offers no per-item delta for history. For
  typical accounts this is a handful of requests; the `last_activities` gate
  keeps untouched runs at exactly two requests.
- **Large histories**: a run is not resumable mid-listing (state is written
  only after the last row, so a stopped run replays from scratch). An account
  with hundreds of watched shows needs one `/shows/:id/seasons` call per show
  on watched-show pulls; with Trakt's ~500 GETs / 5 minutes that can exceed a
  single window and the run stops cleanly, to be replayed (and rate-limited)
  again. Sync such accounts rarely or raise `max_requests`.
- **Comments** are embedded in the movie/show documents of watched items;
  comments on items you never watched are not synced.
- **Hidden items, collection, favorites and playback progress** are not synced.
- **Watchlist-only items** produce no documents of their own (the watchlist
  marker lives inside the watched item's document); a watchlisted movie that
  was never watched or rated is invisible to memory.
- **Show metadata changes** (a renamed show, say) do not move any synced
  activity timestamp, so the show document is refreshed only when something
  else touches the show.
- If a show's seasons endpoint cannot be read, its episodes fall back to
  composite `episode:<show id>-s<season>e<number>` keys, which do not merge
  with episode ratings.

## How to run the tests

```bash
cd packages/connector/trakt
uv sync
uv run pytest tests/ -q
uv run ruff check .
```

The tests run against a fake Trakt API (`tests/fake_trakt.py`) and a stubbed
HTTP opener — no token, no network. One test suite drives a real dlt merge
into SQLite to prove deletions physically reach a destination.
