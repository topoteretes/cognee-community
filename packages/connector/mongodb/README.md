# cognee-community-connector-mongodb

A MongoDB data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a collection into memory — "ask my database".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`,
reusing cognee's existing DLT ingestion path
(`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by document `_id`, `merge` write disposition, a
`updatedAt` high-water mark in dlt resource state) and **forget-on-delete**
(documents removed upstream are emitted as hard-delete markers and purged from memory
on the next sync) with no core change to cognee.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. That is not in cognee 1.3.0; it first shipped in 1.4.0. The
> `cognee==` pin in `pyproject.toml` is the version this connector is tested against —
> move it forward in step with the other connectors.

## Install

```bash
uv pip install cognee-community-connector-mongodb
# or, from this monorepo:
cd packages/connector/mongodb && uv sync
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
    write_disposition="merge",  # REQUIRED — see note below
)

answer = await cognee.search(
    query_text="Which tickets mention SSO login failures?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my_tickets"],
)
```

Re-running `remember(...)` with the same dataset syncs only the documents changed
since the last run and forgets documents deleted upstream. See `examples/example.py`
for the full flow, including the seed data and index creation.

> **`write_disposition="merge"` is required.** The add pipeline defaults to
> `"replace"`, which rewrites staging on every run and drops the corpus the
> incremental cursor diffs against.

> `max_rows_per_table` needs no tuning here. For document-mode sources cognee
> already reads back the whole synced corpus so orphan cleanup compares against all
> of it.

## Mapping a schemaless collection

MongoDB has no schema, so the document-to-node mapping is explicit rather than inferred:

| Argument | Effect |
| --- | --- |
| `text_fields` | Fields, in order, that make up the text cognee cognifies. Missing or empty fields are skipped; containers are rendered as JSON. |
| `title_field` | Field used as the document heading (cognee prefixes it as `# <title>`). |
| `projection` | Mongo projection for the document reads. Forced to carry `cursor_field`; an exclusion projection that drops it is rejected. |
| `query_filter` | Mongo filter restricting which documents sync. |
| `cursor_field` | Field carrying the incremental high-water mark (default `updatedAt`). |
| `detect_deletions` | Set `False` to skip the per-run id sweep. |

Naming `text_fields` is strongly recommended. Unnamed fields are dropped, which keeps
a metadata-only write (a view counter, a `lastSeenAt` bump) from changing the text and
churning the content-hash `data_id` downstream — which would re-embed and re-cognify
the document for no reason. With no `text_fields` the connector falls back to every
top-level scalar except `_id`, `cursor_field`, and `title_field`, rendered as
`key: value` lines; containers are skipped in that mode, so name them in
`text_fields` if they carry the content.

`query_filter` is applied to the id sweep as well as the document reads, so a document
that falls out of the filter is treated as absent and is forgotten — which is usually
what you want for a soft-delete flag such as `{"status": {"$ne": "archived"}}`.

## How sync + forget-on-delete work

Incremental sync compares `cursor_field` (default `updatedAt`) server-side with `$gt`
and persists the high-water mark in dlt's per-resource state. A document that is new to
the corpus is fetched even when its cursor value is old, so a restored or back-dated
document is not missed.

MongoDB reports deletions only through change streams, which need a replica set and a
retained oplog, so this connector does not depend on them. Each run instead diffs a
cheap `_id`-only sweep against the ids seen on the previous run, also kept in resource
state. Vanished documents are emitted with the `_deleted` hard-delete marker, dlt drops
them on `merge`, and cognee's `orphan_cleanup` removes them from the graph, vector, and
relational stores.

Index the cursor field so the delta read stays an index scan:

```js
db.tickets.createIndex({ updatedAt: 1 })
```

## Failure posture and known limits

- **A transient sweep failure cannot purge your dataset.** An empty id sweep over a
  previously populated corpus is treated as a failure, not a mass deletion: deletion is
  skipped for that run and the id state is preserved.
- **Wiping the whole collection upstream does not forget anything**, because that is
  indistinguishable from the failure case above. It self-heals — the next run that sees
  any document reconciles normally.
- **A document with no `cursor_field` is ingested when first seen, but later edits to it
  are invisible**, since `$gt` can only match documents that carry the field. Set
  `cursor_field="_id"` for insert-only collections, or have writers maintain a
  timestamp.
- **Both the id sweep and the persisted id set are O(documents) per run.** That is cheap
  for tens of thousands of documents and wasteful for millions; pass
  `detect_deletions=False` there, accepting that deletions and back-dated inserts are
  then never detected.
- **A mixed-type `cursor_field`** (an int timestamp beside an ISO string) keeps the
  previous high-water mark instead of aborting the sync, so the next run re-reads a
  slightly wider window.

## Setup

1. Have a MongoDB instance reachable by connection URI. Pass it as `uri=` or set
   `MONGODB_URI`. Access is read-only — the connector only issues `find`.
2. Create the cursor-field index shown above.
3. Ensure the documents you sync carry `cursor_field`.
4. Set your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

No server and no live credentials are required. Coverage:

- the ingest path, the schemaless mapping (including JSON rendering of containers and
  the title de-duplication)
- the incremental cursor: `$gt` pushed down to the server, the new-document-with-an-old-
  cursor case, no double emission, and mixed cursor types
- projection handling, including the forced cursor field and the rejected exclusion
  projection
- forget-on-delete: the hard-delete markers, the empty-sweep guard, `query_filter`
  narrowing, and `detect_deletions=False`
- a full edit / insert / delete cycle against `mongomock`, an independent implementation
  of MongoDB query semantics, so the `$gt` / `$in` / projection shapes are checked
  against something other than the tests' own fake
- an end-to-end run through a **real dlt merge**, proving a `_deleted` marker physically
  removes the row, plus a check that the cursor and id state survive across pipeline runs
- `tests/test_mongodb_forget.py`: the full path through cognee with the LLM and
  embeddings mocked — a deleted document's entity disappears from the graph, and an
  unchanged re-sync leaves the graph untouched

### Validating against a real mongod

The unit tests fake the query layer. To confirm the delta read really is an index scan
and that the `_id` sweep is a covered scan, run the same cycle against a real server:

```bash
docker run -d -p 27017:27017 --name cognee-mongo mongo:8
python - <<'PY'
from pymongo import MongoClient

collection = MongoClient("mongodb://localhost:27017")["support"]["tickets"]
collection.create_index([("updatedAt", 1)])

def winning(query, projection=None):
    plan = collection.find(query, projection).explain()["queryPlanner"]["winningPlan"]
    print(plan)

# The incremental delta read is an IXSCAN bounded by the cursor...
winning({"updatedAt": {"$gt": 1}})
# ...and the id sweep is covered, so it never reads the documents themselves.
winning({}, {"_id": 1})
PY
```
