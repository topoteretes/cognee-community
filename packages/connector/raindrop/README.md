# cognee-community-connector-raindrop

A [Raindrop.io](https://raindrop.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync your bookmarks, notes, and highlights
into memory, so you can ask questions about what you have saved and read.

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`. Each
bookmark is rendered to one markdown document and ingested as a **normal document** (it flows
through cognee's cognify entity-extraction pipeline), via cognee's document-mode marker.

## What gets ingested

One document per bookmark, containing:

- title, link, type, tags, and the date it was saved
- its **collection path** (for example `Research/AI/Papers`), so collections show up as context
- the description (excerpt) and your personal note
- every **highlight** (annotation) with its note

Optionally, the **linked page text** too. That is off by default because it multiplies ingest
cost (see below).

## Install

```bash
uv pip install cognee-community-connector-raindrop
# or, from this monorepo:
cd packages/connector/raindrop && uv sync
```

## Setup

1. Open <https://app.raindrop.io/settings/integrations>, create an app, and copy its
   **Test token**. An OAuth access token for your own app works the same way.
2. Set it as `RAINDROP_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any other
   cognee run.

## Usage

```python
import cognee
from cognee_community_connector_raindrop import raindrop_source

await cognee.remember(
    raindrop_source(),  # RAINDROP_TOKEN from env, or pass token=...
    dataset_name="raindrop",
    write_disposition="merge",  # required, see the note below
)

answer = await cognee.search(
    query_text="What did I highlight about transformers?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["raindrop"],
)
```

See [`examples/example.py`](examples/example.py) for the full runnable flow.

### Choosing what to ingest

| Option | Default | What it does |
| --- | --- | --- |
| `collection_ids` | `None` (everything outside Trash) | Only sync these collections. `-1` is Unsorted. |
| `include_nested` | `True` | With `collection_ids`, also sync their nested collections. |
| `include_highlights` | `True` | Fold highlights and their notes into each bookmark. |
| `fetch_page_content` | `False` | Also fetch and ingest each bookmark's linked page text. |
| `max_page_chars` | `10000` | Page text kept per bookmark when `fetch_page_content` is on. |

> **Pass `write_disposition="merge"`.** cognee defaults to `"replace"`, which overrides the
> connector's own setting. Each run only emits new or changed bookmarks, so `"replace"` would
> leave staging with just that delta and cognee would forget every unchanged bookmark.

## How sync and forget-on-delete work

- **Incremental.** Each run lists the bookmarks in scope (one request per 50 bookmarks; the
  list already carries highlights) and fingerprints each rendered document. Only bookmarks whose
  fingerprint changed since the last run are sent to cognee, so unchanged bookmarks are never
  re-embedded. The fingerprints live in dlt's resource state.
- **Why a fingerprint and not `lastUpdate`.** Raindrop's API has no `lastUpdate` sort, and its
  `lastUpdate:` search filter only has day granularity. Comparing the rendered document is exact,
  and it also catches changes that never touch a bookmark's `lastUpdate`, such as renaming the
  collection it lives in.
- **Forget-on-delete.** Raindrop has no deletion feed. A bookmark that was synced last run but
  is missing now (moved to Trash, or deleted for good) is emitted with a `_deleted` hard-delete
  marker. dlt removes it on `merge`, and cognee's `orphan_cleanup` then removes it from the graph,
  vector, and relational stores. Restoring it from Trash ingests it again.
- **Safe failures.** Rate limits (429) and server errors are retried, honoring `Retry-After` /
  `X-RateLimit-Reset`. A permanent error aborts the run without touching memory. If a listing
  comes back empty while bookmarks were known, deletion is skipped for that run instead of
  wiping the dataset.

### Linked page content

With `fetch_page_content=True`, each new or changed bookmark's page is fetched once (10s timeout,
2 MB read cap, HTML and plain text only, trimmed to `max_page_chars`) and appended to its
document. The Raindrop token is never sent to those sites. A page that fails to load is skipped
with a warning rather than failing the sync. Page text is refreshed when the bookmark itself
changes, not when the website changes.

## Testing

```bash
uv run pytest tests/
```

The tests need no live token. They serve a fake Raindrop API through `httpx.MockTransport` and
cover rendering, pagination, retries, the incremental fingerprint cursor, tombstones, the
empty-sweep guard, page fetching, and an end-to-end cognee run (LLM and embeddings mocked) where
a trashed bookmark disappears from the graph.
