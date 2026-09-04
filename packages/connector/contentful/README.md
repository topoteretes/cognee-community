# cognee-community-connector-contentful

[Contentful](https://contentful.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee) — sync your content model,
entries, and assets into memory, incrementally and with forget-on-delete.
"Ask my CMS."

Part of the cognee hackathon connector push (topoteretes/cognee#4787).

## Features

- **Incremental sync** — the first run is a full backfill; every later run
  lists objects `order=-sys.updatedAt` and re-emits only those updated after
  the recorded cursor.
- **Content model included** — content types are ingested as their own
  documents (name, display field, every field with its type), so entries have
  their schema context next to them in memory, exactly as the issue asks.
- **Forget-on-delete** — the Delivery API has no deletion feed, so each run
  diffs the full id set of every kind against the previous run (kept in dlt
  resource state). Vanished objects are hard-deleted and then purged by
  cognee's orphan cleanup.
- **Document ingestion** — each row becomes a normal text document that flows
  through cognee's cognify entity-extraction pipeline (same treatment as the
  Notion / Google Drive connectors). Entry fields are flattened to text:
  strings, lists, rich-text nodes, and `[linkType: id]` placeholders — links
  are never dereferenced, so editing one entry never pulls the whole linked
  graph.
- **Retries with backoff** — rate-limit (429), server (5xx), timeout, and
  network errors are retried; auth failures fail fast.

## Installation

```bash
pip install "cognee[contentful]"
# or, from this repository:
uv pip install ./packages/connector/contentful
```

## Setup

1. In Contentful: **Settings → API keys → Add API key**; copy the Space ID and
   the *Content Delivery API — access token*.
2. Export them:

   ```bash
   export CONTENTFUL_SPACE_ID="..."
   export CONTENTFUL_TOKEN="..."
   ```

## Usage

```python
import cognee
from cognee_community_connector_contentful import contentful_source

await cognee.remember(
    contentful_source(space_id="...", token="...", environment="master"),
    dataset_name="my_cms",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see below
    max_rows_per_table=0,  # REQUIRED for a real space
)

answer = await cognee.recall("What do we publish about pricing?")
```

> **`write_disposition="merge"` is mandatory.** The add pipeline defaults to
> `"replace"` (drop + reload the table each run); on the second, incremental
> sync that would wipe everything but the small delta.

> **`max_rows_per_table=0`** lifts cognee's 50-row read cap so orphan cleanup
> compares against the *whole* synced corpus.

A runnable version lives in [`examples/example.py`](examples/example.py).

## What gets ingested

| Entity        | Kind           | Document                                              |
| ------------- | -------------- | ----------------------------------------------------- |
| Content type  | `content_type` | name, display field, every field with its type         |
| Entry         | `entry`        | flattened `fields` (rich text rendered, links as refs) |
| Asset         | `asset`        | title, description, file type/name, https file URL     |

## Deleting data

- Delete an entry/asset/content type in Contentful → it is removed from
  cognee's graph, vector, and relational stores on the next sync (dlt
  hard-delete + cognee's orphan cleanup).
- Delete the whole dataset locally with `cognee.forget(...)` /
  `cognee.prune()`.

## Tests

```bash
uv run pytest packages/connector/contentful/tests -q
```

All tests run offline: the Contentful API is faked, and the dlt pipeline runs
against a temporary SQLite destination.

## Notes & limitations

- The connector reads from the read-only Content Delivery API and never
  mutates your space.
- Rich-text rendering extracts text nodes only (no embeds/tables markup).
- Linked entries are rendered as `[linkType: id]` placeholders, not resolved.
