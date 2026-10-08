# cognee-community-connector-basecamp

Sync a Basecamp account into cognee memory: **messages, to-dos, documents and
comments**, with incremental sync and forget-on-delete.

Each Basecamp item becomes a normal cognee document (it goes through the full
cognify pipeline), so you can ask things like "which launch tasks are still
open?" or "what did people say about checkout?" next to your other sources.

## Requirements

- Python 3.11 to 3.13
- cognee 1.6.3 (document mode plus the per-row node-set column)
- A Basecamp account (the free plan works) and a Launchpad OAuth app

## Install

```bash
pip install cognee-community-connector-basecamp
# or, from this monorepo:
cd packages/connector/basecamp && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_basecamp import basecamp_source

await cognee.remember(
    basecamp_source(
        account_id="1234567",
        access_token="...",
        user_agent="My Basecamp Sync (me@example.com)",
        # optional:
        # project_ids=["49193290"],          # default: all active projects
        # types=["Message", "Todo"],          # default: Message, Todo, Document, Comment
        # refresh_token="...", client_id="...", client_secret="...",
    ),
    dataset_name="basecamp",
    write_disposition="merge",  # required, see below
    max_rows_per_table=0,  # cognee reads only 50 rows per table by default
)

print(await cognee.recall("Which to-dos are still open?", datasets=["basecamp"]))
```

Every argument can also come from the environment: `BASECAMP_ACCOUNT_ID`,
`BASECAMP_ACCESS_TOKEN`, `BASECAMP_REFRESH_TOKEN`, `BASECAMP_CLIENT_ID`,
`BASECAMP_CLIENT_SECRET`, `BASECAMP_USER_AGENT`, `BASECAMP_PROJECT_IDS`
(comma separated) and `BASECAMP_FULL_SYNC_EVERY`.

**Pass `write_disposition="merge"` to `remember()`.** `remember()` defaults to
`"replace"`, which overrides the source's own setting and breaks both the
incremental sync and deletes. Don't pass `primary_key`; the source already uses
`id`, which is what cognee expects for document sources.

Each row is tagged with its project name as a node set, so recall can be scoped
to one project.

## How sync + forget-on-delete work

- **One endpoint.** Everything comes from `GET /projects/recordings.json`. It
  also returns completed to-dos, so finished work is synced too.
- **Incremental.** For each type the connector keeps the newest `updated_at` it
  has seen and reads newest-first until it reaches that point. Unchanged lists
  cost almost nothing: the first page's `etag` is sent back and Basecamp answers
  `304 Not Modified`.
- **Trashed items are forgotten** on the next sync. Trashing an item moves it to
  the trashed listing and bumps its `updated_at`, so the connector sees it and
  writes a delete marker; cognee's orphan cleanup then removes it from memory.
- **Purged items are forgotten on the next full sweep.** Basecamp deletes trashed
  items after 25 days, or right away when someone empties the trash. Once that
  happens they are in no listing at all, so a sync that runs later never sees
  the trash event. To catch these, every Nth run (default 10, and always the
  first run) lists everything that still exists and deletes anything missing.
  Force one with `full_sync=True`. If your team empties the trash often, sync
  regularly or lower `full_sync_every`.
- **Archived items are kept.** Archiving is not deleting; archived items stay in
  memory (marked "archived") and their comments stay with them.
- **A failed sync changes nothing.** Any API error stops the run. The cursor is
  only saved when the load succeeds, and cognee only forgets things after a
  successful run, so the next sync simply retries.

## Setup

1. Go to <https://launchpad.37signals.com/integrations> and register an app:
   pick **Basecamp 5** and set a redirect URI such as
   `http://localhost:8000/callback` (it doesn't need to be a running server).
2. Run the one-time helper to get tokens and your account id:

   ```bash
   export BASECAMP_CLIENT_ID="..." BASECAMP_CLIENT_SECRET="..."
   export BASECAMP_REDIRECT_URI="http://localhost:8000/callback"
   export BASECAMP_USER_AGENT="My Basecamp Sync (me@example.com)"
   uv run python examples/authorize.py
   ```

   Open the printed link, allow access, and paste the `code` from the address
   you land on. Tokens are saved to `~/.basecamp_tokens.json` (mode 600).
3. Access tokens last two weeks. Pass `refresh_token`, `client_id` and
   `client_secret` and the connector renews the token by itself when it expires.
4. Basecamp requires a `User-Agent` with a way to contact you; requests without
   one are rejected.

Run the full demo with `uv run python examples/example.py` (needs an LLM key).

## Limitations

- Only active projects are read (the API default). Archiving or trashing a whole
  project removes its items from memory on the next full sweep.
- Card tables, schedules, chat, uploads and check-in answers are not synced yet.
- Basecamp's rate limit is about 50 requests per 10 seconds. The connector
  retries 429 and 5xx responses using `Retry-After`; a 404 is never retried, and
  an inactive account (expired trial) stops the sync with a clear error.

## Testing

```bash
uv run pytest tests/
```

The tests use an in-memory fake of the Basecamp API (no network, no token),
modelled on what a live account returns: newest-first listings, `Link` paging,
`etag`/304, trashed and archived statuses, and purged items vanishing. They
cover the HTTP client (retries, 404s, token refresh), rendering, the incremental
cursor, trash and purge deletes, archived items, and a real dlt pipeline run
including a failed run that must change nothing.
