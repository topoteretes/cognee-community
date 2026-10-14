# cognee-community-connector-servicenow

A ServiceNow data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your IT history into memory — "has this VPN error happened before, and what fixed it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. It ingests published
**knowledge articles** (`kb_knowledge`) and **incidents**, **problems** and **change
requests**, one document per record, with reference fields rendered by name. Work notes
and comments are opt-in. Documents go through cognee's normal cognify entity extraction
(document mode).

## Requirements

Pins cognee 1.6.2. Document mode (`DOCUMENT_SOURCE_ATTR`) arrived in 1.4.0, and 1.6.x
adds `PIPELINE_SCOPE_ATTR`, which this source sets so its sync state is kept per
instance and cognee dataset.

## Install

From this monorepo:

```bash
cd packages/connector/servicenow && uv sync
```

## Setup

1. Use your instance, or a free [Personal Developer Instance](https://developer.servicenow.com)
   (it comes with demo incidents, problems, changes and knowledge articles).
2. Create a read-only integration user that can read the synced tables (and
   `sys_journal_field` if you turn on journal entries).
3. Export the instance and credentials, plus your `LLM_API_KEY`:

```bash
export SERVICENOW_INSTANCE_URL=https://dev12345.service-now.com
export SERVICENOW_USERNAME=cognee.reader
export SERVICENOW_PASSWORD=...
export LLM_API_KEY=sk-...
```

## Usage

```python
import cognee
from cognee_community_connector_servicenow import servicenow_source

await cognee.remember(
    servicenow_source(
        tables={  # optional; these are the defaults
            "kb_knowledge": "workflow_state=published",
            "incident": "",  # or an encoded query, e.g. "active=true"
            "problem": "",
            "change_request": "",
        },
        include_journal=False,  # True adds work notes and comments
    ),
    dataset_name="servicenow",
    write_disposition="merge",  # required, see below
    self_improvement=False,
)
```

See `examples/example.py` for a runnable version.

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`: this source only emits records that
changed and deletes by emitting `_deleted` tombstone rows, which only `merge` applies.

- **Incremental sync without date queries.** Each run lists every in-scope record's
  `sys_id` and raw `sys_updated_on` (two fields, UTC) and compares them with the previous
  run, kept in dlt state. Only new or changed records are fetched in full, in batches of
  100. This avoids encoded date filters, whose `gs.dateGenerate` values are interpreted
  in the integration user's time zone.
- **Deletions.** A record missing from the listing — deleted, or out of scope (a retired
  knowledge article, an incident no longer matching the table's query) — is tombstoned and
  cognee's orphan cleanup forgets it. Removing a table from `tables` forgets its records.
- **Failures.** 429 and 5xx responses are retried (honouring `Retry-After`); any other
  failed request aborts the run, so a broken listing never looks like a deletion.

**Limits:** the listing reads two fields of every in-scope record each run and the state
keeps one timestamp per record, so narrow very large tables with a query. Journal entries
need read access to `sys_journal_field`.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake `requests` session (no instance needed). They cover rendering,
offset paging, retries, the listing/diff sync, batching, journal entries, scope changes,
deletions, and full dlt `merge` runs into a temporary SQLite destination.
