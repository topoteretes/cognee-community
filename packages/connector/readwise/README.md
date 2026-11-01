# cognee-community-connector-readwise

A Readwise data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your book, article and podcast highlights into memory — "ask my highlights".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each highlight (and each
book-level note) is ingested as a **normal document**: it flows through cognee's cognify
entity-extraction pipeline via cognee's document-mode marker.

## Requirements

Needs a cognee release that ships "document-mode"
(`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR`), i.e. **cognee >= 1.4.0**.

## Install

```bash
uv pip install cognee-community-connector-readwise
# or, from this monorepo:
cd packages/connector/readwise && uv sync --all-extras
```

## Setup

1. Copy your access token from <https://readwise.io/access_token>.
2. Export it as `READWISE_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any
   other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_readwise import readwise_source

await cognee.remember(
    readwise_source(),  # READWISE_TOKEN from env, or token=...
    dataset_name="readwise",
    write_disposition="merge",  # REQUIRED, see below
)

answer = await cognee.search(
    query_text="What are the main ideas across my highlights?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["readwise"],
)
```

Choose what to ingest:

| Argument | Meaning |
| --- | --- |
| `token` | Access token. Defaults to `READWISE_TOKEN`. |
| `categories` | Only these categories: `books`, `articles`, `tweets`, `podcasts`, `supplementals`. |
| `book_ids` | Only these Readwise `user_book_id` values. |
| `updated_after` | ISO 8601 time. Skips older highlights on the **first** run only. |

See `examples/example.py` for a runnable end-to-end script.

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which drops and
> reloads the table each run. An incremental run only sees what changed, so `replace` would
> forget everything else.

## How incremental sync + forget-on-delete work

* **One document per highlight** (`id = highlight:<id>`), carrying the book title, author,
  type, source link, your note and tags. A non-empty book-level note is its own document
  (`id = note:<user_book_id>`). Readwise returns only the *updated* highlights of a book on
  an incremental call, so a document per highlight stays correct where a document per book
  would not.
* **Incremental cursor.** The first run backfills everything and stores the UTC time the run
  *started*. Later runs call `GET /api/v2/export/?updatedAfter=<that time>` and fetch only
  what changed. The cursor sits in dlt resource state and moves only after a fully successful
  run, so a failed run is retried from the same point.
* **Forget-on-delete.** Requests send `includeDeleted=true`. Highlights or books that Readwise
  reports as deleted are emitted as `{"id": ..., "_deleted": True}` tombstones. dlt removes
  those rows on `merge`, and cognee's existing `orphan_cleanup` removes them from the graph
  and vector stores on the next sync.
* **Rate limits.** The export endpoint allows about 20 requests/minute. `429`, `5xx` and
  network errors are retried with backoff (honoring `Retry-After`). A bad token raises
  `PermissionError`. An error aborts the run so a partial read never causes false deletions.

### Limitation

If you clear a book-level note upstream, the old `note:` document stays until the book is
deleted. Highlights have no such gap.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake HTTP client (no token, no network) and a temporary SQLite destination.
They cover document building, paging, retries, the incremental cursor, categories/ids
filters, and forget-on-delete for highlights and books.
