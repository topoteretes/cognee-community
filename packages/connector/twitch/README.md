# Twitch connector for Cognee

Turn your followed Twitch channels and their recent VOD/clip metadata into
searchable Cognee documents. One document is keyed by each Twitch channel, VOD
or clip ID. This connector reads metadata, not video/audio or chat transcripts.

## Setup

1. Register an application at <https://dev.twitch.tv/console/apps> and copy its
   **Client ID**. Configure your OAuth redirect URL.
2. Obtain a **user access token** for that app with `user:read:follows` using
   [Twitch's user OAuth flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/).
   An app access token cannot read your follows. No client secret is needed by
   the connector. Never commit tokens to source control.
3. Install this package and configure your usual Cognee LLM/embedding credentials:

   ```sh
   cd packages/connector/twitch
   uv sync --group dev
   export TWITCH_CLIENT_ID='your-app-client-id'
   export TWITCH_ACCESS_TOKEN='your-user-access-token'
   export LLM_API_KEY='your-llm-key'
   uv run python examples/example.py 'Which channels cover Rust?'
   ```

The package requires Cognee >=1.6.6 and checks for document sync v2 support at
runtime. This provides table-scoped cleanup and per-document node sets.

## Usage

```python
import os
import cognee
from cognee_community_connector_twitch import twitch_source

source = twitch_source(
    access_token=os.environ["TWITCH_ACCESS_TOKEN"],
    client_id=os.environ["TWITCH_CLIENT_ID"],
    resource_name="my_twitch",  # stable and unique per account within a dataset
    lookback_days=30,
)
await cognee.remember(
    source,
    dataset_name="twitch",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
answer = await cognee.recall("Which VODs discuss Python?", datasets=["twitch"])
```

Keep the dataset name, resource name, and DLT state directory stable across runs.
Use a separate resource name per account. Switching the token to a different user
under an existing resource raises an error before making changes. Keep `merge`:
`replace` would discard unchanged documents on an incremental run.

Each row carries `cognee_node_set=["channel:<login>"]`; Cognee namespaces it as
`twitch:channel:<login>`. Channels include descriptions, current stream titles,
categories, tags and language. VODs include titles, descriptions, timestamps,
durations and language. Clips include titles, timestamps, durations, language
and category IDs. Helix does not expose VOD categories or clip descriptions/tags;
the connector leaves those fields empty instead of using a channel's current
category as historical metadata.

## Sync and deletion behavior

- The initial run fetches VODs/clips created in the last `lookback_days` (30 by
  default). A newly followed channel receives the same initial window.
- Every run validates the user through `/helix/users` and completely re-lists
  follows. Channel metadata is refreshed; identical document rows are not emitted.
- Clips use explicit `started_at` and `ended_at` bounds. Busy windows are split
  before the API's approximate 1,000-result ceiling; a saturated one-second
  window fails rather than silently dropping clips.
- The VOD endpoint has **no time filter**. VODs are paginated newest-first until
  the saved watermark. Both paths overlap the last sync by five minutes and
  deduplicate by ID. Content published with a creation time older than this overlap
  can be missed; this is not a complete historical archive.
- Previously stored VODs/clips are also checked by ID in batches of 100. Missing
  IDs emit `_deleted` hard-delete rows. An all-missing VOD batch returns HTTP 404
  in Helix and is handled as a confirmed deletion. Unfollowing removes the channel
  and all its stored children. Aging beyond the initial window alone never deletes
  a document. Previously stored metadata changes are picked up by these ID checks.
- All API reads must succeed before any rows are emitted. State advances only
  after the iterator finishes; DLT commits it with the load. Failed/interrupted
  extraction can be retried without advancing the cursor or publishing cleanup.
- DLT merge removes tombstones from staging; Cognee's document orphan cleanup
  forgets removed documents, including the final document in a source table.

Twitch has no snapshot isolation: a follow or video change during a run may be
observed on the next run. API calls scale with follows and retained IDs; this
connector does not download media. Increasing `lookback_days` affects new channels,
not channels with an existing cursor. Use a new dataset/resource for a fresh import.

## Errors and rate limits

`TwitchAuthError` explains invalid/expired tokens, missing follow scope and Client
ID mismatch. Refresh your token outside the connector and rerun with the same
resource name. Tokens are neither logged nor stored in DLT state.

Pagination follows `after` cursors and rejects repeated cursors. HTTP 429 and 5xx
responses receive at most three retries. `Retry-After`, `Ratelimit-Reset`, and
`Ratelimit-Remaining: 0` are honored, with exponential backoff when no delay is
given. A single wait above 60 seconds or total waits above 120 seconds raise
`TwitchRateLimitError`; rerun later. HTTP requests have a 30-second timeout.
Optional `check_active` is called before requests and during waits and row
publication. Injected `http_client` instances remain caller-owned.

API details: [Helix reference](https://dev.twitch.tv/docs/api/reference/).

## Tests

```sh
uv sync --group dev
uv run pytest tests -ra
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests use HTTPX's fake transport and local databases. They require no Twitch,
LLM or embedding network calls. The integration tests exercise real DLT merge,
persistent cursors, hard deletes, Cognee document conversion/node sets, and
`remember`/`recall` with SQLite, LanceDB and Ladybug. Ladybug requires its native
JSON extension (installed during setup on a fresh machine). Restricted test
environments can set `TWITCH_TEST_LADYBUG_HOME` to a directory with a preinstalled
Ladybug extension cache.
