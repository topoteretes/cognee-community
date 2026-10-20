# cognee-community-connector-airtable

An Airtable data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a base into memory — "ask my Airtable".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Records are rendered to
markdown (fields, plus their comments, plus the table's field schema) and ingested as
**normal documents**.

## Requirements

> **One-time setup: every synced table needs a modified-time field.**
>
> Airtable does not expose a last-modified timestamp on the record object, so incremental
> sync needs a field that carries it. Add a **`lastModifiedTime`** field to each table you
> sync:
>
> 1. Open the table in Airtable → **+** (add field).
> 2. Field type: **Last modified time**.
> 3. Name it `lastModifiedTime` (or pass `modified_field="<your name>"`).
>
> Airtable's *Last modified time* field type is maintained by Airtable itself — no formula,
> no automation, nothing to keep in sync by hand. Records created before the field existed
> have an empty value; the connector re-ingests those rather than skipping them (and logs a
> warning), so backfill the column once if you care about a quiet first run.
>
> The base also needs a personal access token with the **`data.records:read`** scope, plus
> **`data.recordComments:read`** if you want comments ingested (the default). Reading the
> field schema additionally uses **`schema.bases:read`**; without it the `schema` column is
> simply empty and the sync still works. Pass `include_schema=False` to skip the schema
> read entirely.

## Install

```bash
uv pip install cognee-community-connector-airtable
# or, from this monorepo:
cd packages/connector/airtable && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_airtable import airtable_source

await cognee.remember(
    airtable_source(
        base_id="appXXXXXXXXXXXXXX",   # or AIRTABLE_BASE_ID
        table_ids=["tblOrders"],       # omit to sync every table in the base
    ),                                 # token from AIRTABLE_API_KEY, or pass token=...
    dataset_name="airtable",
)

answer = await cognee.search(
    query_text="Which orders mention the delayed shipment?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["airtable"],
)
```

See `examples/example.py` for the full flow, including the second, incremental run.

## How sync + forget-on-delete work

Each run sweeps the configured tables and compares every record against the cursor:

* **Incremental** — the record's `lastModifiedTime` value is the cursor, persisted in
  dlt's per-resource state. A record already known and not newer than the cursor is
  skipped, so an unchanged base is a no-op. A record **not** in the known set is always
  emitted, whatever its timestamp, so records restored or moved in with an old modified
  time are not lost.
* **Forget-on-delete** — the sweep enumerates the *current* record ids, and that set drives
  deletion detection. Records that disappeared upstream are emitted once as
  `_deleted=True` hard-delete markers, which dlt removes from the destination and cognee's
  `orphan_cleanup` then forgets from the graph and vector stores.
* **Idempotent upserts** — `primary_key="id"` + `write_disposition="merge"`, so re-syncing
  the same record replaces it instead of duplicating it.
* **A transient empty sweep is not a wipe** — if the sweep returns no records while
  previous runs knew some, deletion is skipped and the state is preserved, so a network
  blip or a momentarily-empty page can never purge the dataset.
* **Comments are an enhancement, never a blocker** — a comments read that fails drops the
  comments, not the record. A record listing that fails still aborts the run (a partial
  sweep must not drive deletions).

The `schema` column repeats the table's `{field name: field type}` map on every record, so
the field schema is always available in memory alongside the text.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Airtable API (no token, no network) and cover offset pagination, backfill
and incremental sync, the no-change no-op, restored records below the cursor, deletion
markers, the transient-empty-sweep guard, comments/schema shaping, retry behaviour on 429,
and the resource's merge + hard-delete configuration.
