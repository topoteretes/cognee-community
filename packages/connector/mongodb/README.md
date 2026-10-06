# cognee-community-connector-mongodb

A MongoDB data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a collection into memory — "ask my database".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by document id, `merge` write disposition, `updatedAt` cursor)
and **forget-on-delete** (documents removed upstream are emitted as hard-deletes and purged
from memory on the next sync) with no core changes.

## Install

```bash
uv pip install cognee-community-connector-mongodb
# or, from this monorepo:
cd packages/connector/mongodb && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_mongodb import mongodb_source

await cognee.remember(
    mongodb_source(
        uri="mongodb://localhost:27017",
        database="support",
        collection="tickets",
        text_fields=["subject", "body"],
        title_field="subject",
    ),
    dataset_name="my_tickets",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by document id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which tickets mention SSO login failures?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my_tickets"],
)
```

Re-running `remember(...)` with the same dataset syncs only documents changed since the last
run and forgets documents that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced collection on the second sync.

## Mapping a schemaless collection

MongoDB has no schema, so the document-to-node mapping is explicit rather than inferred:

| Argument | Effect |
| --- | --- |
| `text_fields` | Fields, in order, that make up the text cognee cognifies. Missing or empty fields are skipped. |
| `title_field` | Field used as the row title. |
| `projection` | Optional Mongo projection applied to the document reads. |
| `query_filter` | Optional Mongo filter restricting which documents sync. |

Naming `text_fields` is strongly recommended. Anything not named is dropped, which keeps a
metadata-only write (a view counter, a `lastSeenAt` bump) from changing the text and churning
the content-hash `data_id` downstream. With no `text_fields` the connector falls back to every
top-level scalar except `_id` and the cursor field, rendered as `key: value` lines.

`query_filter` is applied to the id sweep as well as the document reads, so a document that
falls out of the filter is treated as absent and is forgotten — which is usually what you want
for a soft-delete flag such as `{"status": {"$ne": "archived"}}`.

## How sync + forget-on-delete work

Incremental sync compares `cursor_field` (default `updatedAt`) server-side with `$gt` and
persists the high-water mark in dlt's per-resource state. Documents that are new to the corpus
are fetched even when their cursor value is old, so a restored or back-dated document is not
missed.

MongoDB exposes deletions only through change streams, which require a replica set and a
retained oplog, so this connector does not depend on them. Each run instead does a lightweight
`_id`-only sweep (a covered index scan) and compares it to the previous run's ids, also in
resource state; vanished documents are emitted with the `_deleted` hard-delete marker, dlt
drops them on `merge`, and cognee's `orphan_cleanup` removes them from the graph + vector +
relational stores. If a sweep comes back empty while documents were previously known, deletion
is skipped for that run — a transient failure must not be able to purge the whole dataset.

For best performance index the cursor field:

```js
db.tickets.createIndex({ updatedAt: 1 })
```

## Setup

1. Have a MongoDB instance reachable by connection URI. Pass it as `uri` or set `MONGODB_URI`.
   Access is read-only — the connector only issues `find`.
2. Ensure the documents you want to sync incrementally carry a `cursor_field`. A document
   without one is ingested when first seen, but later *edits* to it cannot be detected, since
   the `$gt` filter can only match documents that carry the field. Set `cursor_field="_id"`
   for insert-only collections.
3. Set your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

No server and no live credentials are required. Coverage:

- the ingest path, the incremental cursor, and the new-document-with-an-old-cursor case
- a full edit / insert / delete cycle against `mongomock`, an independent implementation of
  MongoDB query semantics, so the `$gt` / `$in` / projection shapes are checked against
  something other than the test's own fake
- an end-to-end run through a **real dlt merge**, proving a `_deleted` marker physically
  removes the row — the step cognee's `orphan_cleanup` then reconciles against

The final link (`orphan_cleanup` purging the graph and vector stores) is cognee's own tested
behavior and needs a live LLM, so it is not re-tested here.
