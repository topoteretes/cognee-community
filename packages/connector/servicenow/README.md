# cognee-community-connector-servicenow

A ServiceNow data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your IT history into memory — "has this VPN error happened before, and what fixed it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. It ingests published
**knowledge articles** (`kb_knowledge`) and **incidents**, **problems** and **change
requests**, one document per record, with reference fields rendered by name. Work notes
and comments are opt-in. Documents go through cognee's normal cognify entity extraction
(document mode).

## Requirements

- Python 3.10–3.14
- `cognee>=1.6.3,<2`, `dlt[sqlalchemy]`, `requests`

cognee 1.6.3 has `PIPELINE_SCOPE_ATTR`, which this source sets so its sync state is kept
per instance and cognee dataset, and `DOCUMENT_SYNC_VERSION`, which the source checks
when it is built.

## Install

From this monorepo:

```bash
cd packages/connector/servicenow && uv sync
```

## Setup

1. Use your instance, or a free [Personal Developer Instance](https://developer.servicenow.com)
   (it comes with demo incidents, problems, changes and knowledge articles). A developer
   instance hibernates when idle and is reclaimed after a longer period without use: wake
   it from the developer portal before a sync. A hibernating instance answers with a web
   page instead of JSON, and the connector stops with an error that says so.
2. Create a read-only integration user that can read the synced tables (and
   `sys_journal_field` if you turn on journal entries). Tick **Internal Integration
   User** (called _Web service access only_ on older releases) so multi-factor
   login, which the API cannot complete, does not apply to it. Newer instances
   also reject Basic auth on the REST API unless the user has the
   `snc_basic_auth_api_access` role, so grant it; without it every request
   returns `401 User is not authenticated`.
3. Pass the instance and credentials to `servicenow_source`. The source never reads the
   environment itself; the example below (and `examples/example.py`) reads them from
   environment variables:

```bash
export SERVICENOW_INSTANCE_URL=https://dev12345.service-now.com
export SERVICENOW_USERNAME=cognee.reader
export SERVICENOW_PASSWORD=...
export LLM_API_KEY=sk-...
```

## Usage

```python
import os

import cognee
from cognee_community_connector_servicenow import servicenow_source

await cognee.remember(
    servicenow_source(
        instance_url=os.environ["SERVICENOW_INSTANCE_URL"],
        username=os.environ["SERVICENOW_USERNAME"],
        password=os.environ["SERVICENOW_PASSWORD"],
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

| Argument | Meaning | Default |
|---|---|---|
| `instance_url`, `username`, `password` | Instance and integration user | required |
| `tables` | `{table: encoded_query}` to sync | published `kb_knowledge`, `incident`, `problem`, `change_request` |
| `include_journal` | Append work notes and comments | `False` |
| `resource_name` | dlt table name, which also scopes the sync state (keyword-only) | `servicenow_<host>` |
| `check_active` | Host checkpoint, called before every request and before every row is published | none |
| `service` (alias `session`) | Pre-built `requests`-compatible session (tests, proxies); not closed by the source | one per run, closed afterwards |

Each instance gets its own table and sync state (`servicenow_dev12345_service_now_com`
for the example above). Sub-production instances are usually clones of production and
share its `sys_id`s, so this keeps a test instance from overwriting or forgetting
production's records when both sync into one dataset.

After a run, `source.cognee_sync_stats` holds counts only: `scanned` (records listed),
`skipped` (unchanged), `changed`, `deleted`, `failed` (listing rows without a `sys_id`),
`failed_auth` and `failed_rate_limit` (1 when the run stopped for that reason),
`requests`, and `complete` (1 once the run finished).

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`: this source only emits records that
changed and deletes by emitting `_deleted` tombstone rows, which only `merge` applies.

- **Incremental sync without date queries.** Each run lists every in-scope record's
  `sys_id`, raw `sys_updated_on` (UTC) and `sys_mod_count` and compares them with the
  previous run, kept in dlt state. The update counter catches a second edit within the
  same second, which the one-second timestamp alone would miss. Only new or changed
  records are fetched in full, in batches of 100. This avoids encoded date filters, whose
  `gs.dateGenerate` values are interpreted in the integration user's time zone.
- **Deletions.** A record missing from the listing — deleted, or out of scope (a retired
  knowledge article, an incident no longer matching the table's query) — is tombstoned and
  cognee's orphan cleanup forgets it. Removing a table from `tables` forgets its records.
  The listing is used rather than `sys_audit_delete` because it needs no admin read
  access, covers tables without delete auditing, and also catches records that leave the
  scope without being deleted.
- **Failures.** 429 and 5xx responses and dropped connections are retried, up to three
  attempts per request, honouring `Retry-After` (capped at 60 seconds). A run waits at
  most 60 seconds in total, because retries sleep inside cognee's staging lock that
  other ingestions wait on; past that it stops with `failed_rate_limit`. Any failed
  request aborts the run, so a broken listing never looks like a deletion, and the next
  run starts over. A 401 error names the role and setting above, a 403 points at the
  table's read access, and a web page in place of JSON points at a hibernating
  instance. Error messages and logs carry the status and path only, never the password
  or the response body.
- **Text.** Control characters and invalid Unicode are dropped from documents, so a
  stray NUL in a work note can't fail the load.
- **Safety net.** ACLs hide rows silently, so a user who lost a role gets empty
  results, not errors. If every table comes back empty with the same queries as the
  last run, the run stops instead of forgetting everything (remove the tables from
  `tables` if they really are empty). One emptied table is a normal deletion. A
  listing that returns the same page twice stops the run instead of paging forever.

**Limits:** the listing reads three small fields of every in-scope record each run and
the state keeps one version string per record, so narrow very large tables with a query.
Journal entries need read access to `sys_journal_field`. Only published knowledge
articles are synced; drafts are skipped on purpose.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake `requests` session (no instance needed). They cover rendering,
offset paging, retries and the wait budget, error hints, the listing/diff sync
(including same-second edits), batching, journal entries, scope changes, deletions,
that the password never shows up in errors or logs for 401, 403, 404, 429 and 5xx, and
full dlt `merge` runs into a temporary SQLite destination, including two instances
that share `sys_id`s in one dataset.
