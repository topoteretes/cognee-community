# Readwise connector for cognee

Sync your **Readwise** library — books/articles, highlights, and notes — into
cognee's memory, so you can ask questions like *"what did I highlight about
deep work last year?"* Highlights are pre-curated: someone already decided each
one mattered, so they carry unusually high signal per token.

Implements
[`topoteretes/cognee#4812`](https://github.com/topoteretes/cognee/issues/4812).

## Features

- **Access-token auth** — pass `token=` or set `READWISE_API_KEY`
  (get one at https://readwise.io/access_token).
- **Selectable content** — restrict ingestion with `book_ids=[...]` and/or
  `category="books"` / `"articles"` / ...
- **Incremental sync** — the first run backfills everything; later runs fetch
  only highlights updated since the last run via Readwise's `updatedAfter`
  filter (cursor kept in dlt state).
- **Forget-on-delete** — delete a book or highlight in Readwise and the next
  sync removes it from the graph (hard-delete markers + `merge`, reconciled
  by cognee's existing `orphan_cleanup`).

## Install

```bash
pip install cognee[readwise]
# or, from this repo:
pip install -e packages/connector/readwise
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_readwise import readwise_source

async def main():
    # READWISE_API_KEY must be set (or pass token="...").
    # Configure cognee (LLM provider, etc.) per the main README first.
    await cognee.remember(
        readwise_source(),              # or readwise_source(book_ids=[...], category="books")
        dataset_name="readwise",
        write_disposition="merge",      # REQUIRED — see note below
    )

    answer = await cognee.search("What do my highlights say about focus?", query_type="GRAPH_COMPLETION")
    print("\n".join(answer))

asyncio.run(main())
```

A runnable example lives in [`examples/example.py`](examples/example.py).

> **Why `write_disposition="merge"`?** The add pipeline defaults to
> `"replace"` (drop + reload the table each run); on the second, *incremental*
> sync that would wipe your whole synced library. Always pass `"merge"`.
> (No `max_rows_per_table` flag is needed: document-mode sources always read
> back the whole row set, so orphan-cleanup compares the full corpus.)

## How it works

Two dlt resources, both in document mode (`cognee_document_source =
"readwise"`, like the Notion connector, so rows flow through cognee's normal
cognify entity-extraction pipeline):

| resource | table | content |
|---|---|---|
| `readwise_books` | one row per book/article | short "source" document (title, author, category) |
| `readwise_highlights` | one row per highlight | highlight text + your note, titled by source |

Each run:

1. **Books** — re-fetches the full book listing (it is small). Books that
   vanished upstream are emitted with the `_deleted` hard-delete marker, and
   every highlight previously seen under them is emitted as `_deleted` too.
2. **Highlights** — fetches only rows updated since the last run via
   `updatedAfter` (full backfill on the first run). Books whose
   `num_highlights` shrank get their highlights re-listed so individually
   deleted highlights are caught.

dlt removes `_deleted` rows from its destination on `merge`; on the next
`remember`, cognee's `orphan_cleanup` purges them from the graph, vector, and
relational stores — that is the "forget on delete" path.

## Running the tests

```bash
pip install -e .[dev]
pytest
```

Tests use a fake Readwise client (no network, no token); the dlt wiring tests
run a real merge against SQLite.

## Privacy

This connector reads your reading highlights. It is **opt-in**: nothing is
fetched until you explicitly construct a source and call `remember`. Scope
with `book_ids` / `category`, keep the token private, and prefer a dedicated
dataset so `cognee.forget` removes the library in one call.
