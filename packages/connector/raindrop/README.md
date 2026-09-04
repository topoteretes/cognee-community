# cognee-community-connector-raindrop

[Raindrop.io](https://raindrop.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee) — sync your collections,
bookmarks, and highlights into memory, incrementally and with
forget-on-delete. "Ask my bookmarks."

Part of the cognee hackathon connector push (topoteretes/cognee#4813).

## Features

- **Incremental sync** — the first run is a full backfill; every later run
  lists raindrops newest-change-first (`sort=-lastUpdate`) and re-emits only
  those updated after the recorded cursor, with an early stop once a whole
  page is older.
- **Forget-on-delete** — Raindrop has no deletion feed, so each run does a
  full id sweep and diffs it against the previous run (kept in dlt resource
  state). Vanished bookmarks/collections — and the annotations of vanished
  bookmarks, plus annotations removed from a re-fetched one — are hard-deleted
  and then purged by cognee's orphan cleanup.
- **Document ingestion** — each row becomes a normal text document that flows
  through cognee's cognify entity-extraction pipeline (same treatment as the
  Notion / Google Drive connectors), so bookmarks are searchable and linkable,
  not just key-value rows.
- **Opt-in page fetching** — fetching the linked page's text multiplies ingest
  cost, so it is off by default (`fetch_page_content=True` to enable) and
  degrades gracefully to the API-provided excerpt when a page can't be
  fetched.
- **Stable content hashes** — volatile bookkeeping fields (collection refs,
  covers, domains) are excluded from document text, so they don't churn the
  content-hash `data_id`.
- **Retries with backoff** — rate-limit (429), server (5xx), timeout, and
  network errors are retried; auth failures fail fast.

## Installation

```bash
pip install "cognee[raindrop]"
# or, from this repository:
uv pip install ./packages/connector/raindrop
```

## Setup

1. Create a test token at <https://app.raindrop.io/#/settings/integrations>
   (Settings → Integrations → For Developer), or use an OAuth app token.
2. Export it:

   ```bash
   export RAINDROP_API_TOKEN="..."
   ```

## Usage

```python
import cognee
from cognee_community_connector_raindrop import raindrop_source

await cognee.remember(
    raindrop_source(),
    dataset_name="my_bookmarks",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see below
    max_rows_per_table=0,  # REQUIRED for a real account
)

answer = await cognee.recall("What do I have about machine learning?")
```

> **`write_disposition="merge"` is mandatory.** The add pipeline defaults to
> `"replace"` (drop + reload the table each run); on the second, incremental
> sync that would wipe everything but the small delta.

> **`max_rows_per_table=0`** lifts cognee's 50-row read cap so orphan cleanup
> compares against the *whole* synced corpus.

With page content (opt-in — one HTTP request per changed bookmark, re-fetched
on every change):

```python
raindrop_source(fetch_page_content=True)
```

A runnable version lives in [`examples/example.py`](examples/example.py).

## What gets ingested

| Entity      | Kind         | Document                                              |
| ----------- | ------------ | ----------------------------------------------------- |
| Collection  | `collection` | title + description                                   |
| Bookmark    | `bookmark`   | title + Raindrop excerpt + tags (+ opt-in page text)   |
| Highlight   | `annotation` | highlight text + note (row ids prefixed `hl-`)         |

## Deleting data

- Delete a bookmark/collection in Raindrop.io → it is removed from cognee's
  graph, vector, and relational stores on the next sync (dlt hard-delete +
  cognee's orphan cleanup).
- Delete the whole dataset locally with `cognee.forget(...)` /
  `cognee.prune()`.

## Tests

```bash
uv run pytest packages/connector/raindrop/tests -q
```

All tests run offline: Raindrop API access is faked, and the dlt pipeline runs
against a temporary SQLite destination.

## Notes & limitations

- Page-text extraction is deliberately dependency-free and heuristic (title,
  meta description, `<p>` text); heavily client-rendered pages may yield
  little beyond their meta description.
- With `fetch_page_content=True`, page text is re-fetched whenever the
  bookmark itself changes, which can churn the content hash — that is the
  documented cost of opting in.
- The connector only issues reads against the Raindrop API and never mutates
  your data.
