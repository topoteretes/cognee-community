# cognee-community-connector-sanity

A [Sanity](https://www.sanity.io) data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the published content of a Sanity dataset into memory, so an agent can answer questions
from your articles, docs and product pages.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's DLT ingestion
path, so you get **incremental re-sync** (only documents that changed are re-processed) and
**forget-on-delete** (documents deleted or unpublished in Sanity leave memory on the next sync).

## Requirements

- Python 3.10 to 3.13.
- cognee 1.6.3 or later (below 2).
- A Sanity project. Private datasets need an API token with Viewer access; public ones can be
  read without a token.

## Install

```bash
uv pip install cognee-community-connector-sanity
# or, from this monorepo:
cd packages/connector/sanity && uv sync
```

## Setup

1. In [sanity.io/manage](https://www.sanity.io/manage), open your project and copy its
   **project id**.
2. Under **API → Tokens**, add a token with **Viewer** access. The connector only reads.
3. Decide which document types to sync, like `article` and `author`.

## Usage

```python
import os

import cognee
from cognee_community_connector_sanity import sanity_source

source = sanity_source(
    project_id="abc123xy",
    dataset="production",
    document_types=["article", "author"],
    token=os.environ["SANITY_TOKEN"],
)

await cognee.remember(
    source,
    dataset_name="sanity_content",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by document id
    max_rows_per_table=0,  # no row cap: a dataset easily exceeds the default 50
)

answer = await cognee.recall("Who owns the billing service?", datasets=["sanity_content"])
```

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would wipe the synced documents on the second sync.

Settings are arguments only. `examples/example.py` reads them from `SANITY_PROJECT_ID`,
`SANITY_DOCUMENT_TYPES`, `SANITY_TOKEN` and `SANITY_DATASET`.

| Argument | Default | What it does |
|---|---|---|
| `document_types` | required | The types to sync. A dataset also holds system and plugin documents, so there is no "everything". |
| `dataset` | `production` | The dataset to read. |
| `groq_filter` | none | An extra GROQ condition, like `'language == "en"'`. |
| `include_schema` | `True` | Also ingest the deployed Studio schema for the selected types (needs a token). |
| `allow_empty_listing` | `False` | Let a run that lists no documents forget everything synced before (see below). |
| `resource_name` | `sanity_<project>_<dataset>` | Table and sync-state name: lowercase letters, digits and underscores. A state written for another project or dataset is refused. |
| `check_active` | none | For hosts: run before every request and every row; raising stops the sync. |

After a run, `source.cognee_sync_stats` holds counts only: `listed`, `changed`,
`reference_updates`, `deleted`, `schema` and `failed`, plus `failed_auth` or
`failed_rate_limit` when a run stopped for that.

## What gets ingested

One document per published document of the selected types: its fields as `Field: value` lines
(sorted by name, so the text only changes when the content does), Portable Text as paragraphs
(list items and link targets kept), references as the referenced document's name or title, and
images as their alt text and caption. Each document gets a node set named after its type. If
the project has deployed its Studio schema, one more document describes the selected types
and their fields.

## How sync and forget-on-delete work

- **Published only.** Queries use the `published` perspective, so drafts and release versions
  never reach cognee. Unpublishing a document forgets it.
- **What changed.** Each run lists `_id` and `_rev` of the selected documents and fetches only
  new or changed revisions. `_rev` changes on every write. `_updatedAt` was not used as a cursor:
  it has second precision, and documents written in one transaction share it.
- **References.** Renaming an author changes neither the `_rev` nor the `_updatedAt` of the
  articles that point to it, so the connector tracks the revisions of referenced documents and
  re-renders the documents that point to one that changed or was deleted.
- **Deletes.** Documents missing from the complete listing are emitted with the `_deleted`
  hard-delete marker; dlt drops them on `merge` and cognee's orphan cleanup removes them from
  the graph and vector stores. Dropping a type from `document_types` forgets its documents too.
- **Errors never delete.** Every request runs before the first row, so a failed run yields
  nothing and keeps its state. 429, 5xx and connection errors are retried; a rate limit that
  outlasts the retries ends the run cleanly and the next sync starts again. 401 and 403 raise.
- **An empty listing never deletes on its own.** A private dataset read without a working
  token answers with no documents rather than an error, and so does a mistyped document
  type. If a run lists nothing but earlier runs synced documents, it raises and forgets
  nothing. When every document really was deleted, run once with `allow_empty_listing=True`.
- **The schema.** It is a system document, readable only with a token, even in a public
  dataset. A run that cannot read it keeps the synced schema document; turning
  `include_schema` off is what forgets it.

## Privacy

- **Read:** the published documents of the selected types, the names or titles of documents
  they reference, and the deployed schema. Nothing is ever written to Sanity.
- **Where it goes:** the text becomes cognee documents and is sent to your configured LLM and
  embedding providers during cognify. dlt state keeps document ids and revisions, not content.
  Logs and `cognee_sync_stats` hold counts only, and the client never shows the token.
- **Personal data:** every field of the selected types is ingested. If a type holds personal
  data (an author's email, say), leave that type out of `document_types`.
- **Deletion:** documents deleted or unpublished in Sanity, or whose type is dropped from
  `document_types`, are forgotten on the next sync and don't come back.
  `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings the forgotten documents back.

## Limitations

- Images and files are not ingested, only their alt text and captions.
- References are resolved one level deep.
- Each run lists the ids of all selected documents: one request per 1,000 documents.
- A run fetches every new or changed document before yielding the first row, so a first sync
  of a large dataset holds all of it in memory and starts over if it fails part-way.

## Testing

```bash
uv run pytest
```

The tests fake the Sanity API (no token needed) and include a real `dlt` merge and a full
cognee run with a mocked LLM, checking that a deleted document and a renamed author's old name
leave the graph.
