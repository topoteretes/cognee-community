# cognee-community-connector-supabase

A read-only Supabase PostgreSQL connector for [cognee](https://github.com/topoteretes/cognee).
It incrementally syncs explicitly selected tables and columns, turns each row into a normal
cognee document, and forgets rows that were deleted upstream.

The connector uses existing cognee and `dlt` ingestion primitives; it does not add a parallel
storage or ingestion path.

Compatibility target: **Cognee 1.6.3** (pinned), Python 3.11–3.13, dlt 1.30.0 in the
validation environment. Cognee 1.4.2 is no longer supported: it lacks dataset-specific
pipeline scope and confirmed-empty, table-scoped document reconciliation. The lock file
updates Cognee and the dependencies required by it; unrelated adapters are unchanged.

## What it does

- Discovers Supabase projects through OAuth 2.0 Authorization Code + PKCE.
- Discovers tables, columns, and primary keys from one PostgreSQL schema.
- Requires an allow-list of tables and columns. It never defaults to exporting the database.
- Uses one monotonic cursor (normally `updated_at`) per table for incremental extraction.
- Uses stable IDs derived from project, schema, table, and primary key.
- Detects hard deletes with a read-only primary-key sweep and emits `dlt` hard-delete markers.
- Runs PostgreSQL sessions as read-only and rejects roles with write privileges on selected tables.
- Routes rows through cognee's document ingestion path with a project-scoped source marker.

## Install

```bash
uv pip install cognee-community-connector-supabase
# or, from this repository:
cd packages/connector/supabase && uv sync
```

## Credentials: two separate paths

Supabase OAuth and PostgreSQL credentials are intentionally separate:

1. **OAuth access token (optional):** can list projects with the `projects:read` scope.
2. **Read-only database URL (required for sync):** reads the selected PostgreSQL rows.

Supabase OAuth does not return a project's existing database password. Do not use the service-role
key or an application owner's database credentials for ingestion.

### Create a dedicated read-only role

Run this once as a database administrator, replacing the password and schema if needed:

```sql
create role cognee_reader login password 'use-a-secret-manager';
grant connect on database postgres to cognee_reader;
grant usage on schema public to cognee_reader;
grant select on public.customers, public.orders to cognee_reader;
alter role cognee_reader set default_transaction_read_only = on;
```

Do not grant `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, table ownership, or replication privileges.
Use a direct connection or Supabase's session pooler. Transaction-mode poolers cannot reliably
preserve session-level read-only settings.
Keep grants and RLS policies stable during a sync. The deletion sweep reconciles rows visible
to this role; an RLS policy hiding rows is indistinguishable from deletion. Privilege or SQL
errors abort extraction rather than producing a successful empty snapshot.

## Discover and select data

```python
import os

from cognee_community_connector_supabase import discover_supabase_schema

schema = discover_supabase_schema(os.environ["SUPABASE_DATABASE_URL"])
for table_name, details in schema.items():
    print(table_name, details["columns"], details["primary_key"])
```

Discovery returns metadata only. Sync still requires an explicit selection:

```python
import cognee
from cognee_community_connector_supabase import supabase_source

source = supabase_source(
    os.environ["SUPABASE_DATABASE_URL"],
    project_ref=os.environ["SUPABASE_PROJECT_REF"],
    tables=["customers", "orders"],
    columns={
        "customers": ["id", "name", "company", "updated_at"],
        "orders": ["id", "customer_id", "status", "total", "updated_at"],
    },
    cursor_columns={"customers": "updated_at", "orders": "updated_at"},
)

await cognee.remember(
    source,
    dataset_name="supabase_crm",
    primary_key="id",
    write_disposition="merge",  # required for incremental sync and hard deletes
    max_rows_per_table=0,  # keep the full staging corpus visible to orphan cleanup
)
```

Run the same call again to sync rows at or above the saved cursor. The inclusive boundary is
replayed, so equal-timestamp inserts, updates and reversions are not lost to PK deduplication;
merge keeps repeated rows idempotent. An unchanged run may therefore produce a dlt load.
The connector also scans only
the selected tables' primary-key columns to detect deletions. Changing a table's selected columns,
primary key, or cursor changes its state fingerprint and triggers a backfill. After a successful
scan, inactive configurations' cursor watermarks are retired within that source scope. Switching
A → B → A therefore backfills A again, removes fields no longer selected from retained rows, and
reselecting a previously removed table restores historical rows. Backfills still respect any
explicit `initial_values` lower bound. Active cursors and the deletion baseline are preserved.

Every document contains deterministic JSON like this:

```json
{
  "source": "supabase",
  "project_ref": "abcdefgh",
  "schema": "public",
  "table": "customers",
  "primary_key": {"id": 42},
  "row": {"id": 42, "name": "Ada", "updated_at": "2026-08-20T08:00:00Z"}
}
```

### OAuth project discovery

Create an OAuth application in the Supabase dashboard and register an exact redirect URI. Keep the
client secret on a trusted backend. Generate the authorization request, retain its `state` and
`code_verifier`, verify `state` on callback, then exchange the code:

```python
from cognee_community_connector_supabase import (
    create_supabase_authorization_url,
    discover_supabase_projects,
    exchange_supabase_oauth_code,
)

authorization = create_supabase_authorization_url(client_id, redirect_uri)
print(authorization.url)  # redirect the user here

# In the callback, first compare the returned state using secrets.compare_digest.
tokens = exchange_supabase_oauth_code(
    client_id,
    client_secret,
    redirect_uri,
    returned_code,
    authorization.code_verifier,
)
projects = discover_supabase_projects(tokens.access_token)
```

Store refresh tokens encrypted and never log tokens, client secrets, or database URLs.
An expired access token raises a sanitized HTTP 401 error. Call `refresh_supabase_oauth_token`
explicitly and persist the returned rotated refresh token before retrying discovery. The connector
does not automatically refresh credentials or implement OAuth callback/state storage. Transport
and invalid-JSON errors suppress underlying exception chains; SQL/dlt exceptions are not promised
to be credential-safe, so do not expose raw tracebacks from ingestion to end users.

## Sync and deletion semantics

The cursor query fetches changed rows. A separate primary-key-only sweep compares the current keys
with connector state and emits tombstones for missing rows. Cognee 1.6.3 reconciles successfully
read empty staging tables, so deleting the last row needs no artificial sync-anchor document.
All selected tables must finish scanning before any deletion is emitted. Extraction exceptions
leave the previous committed dlt state and staging intact in the tested extraction-failure cases.
The load-retry test injects failure **before loading starts**, then retries the pending package.
It does not test interruption after partial destination writes or after graph cleanup has begun;
complete failure recovery is not established by this suite.
When retrying a pending load, dlt may drain its package without extracting the newly supplied
source on the first retry. Run sync again to read changes made since that package was extracted;
the recovery test exercises both calls. Do not reset the pipeline to bypass this recovery step.

The exact project reference, schema and `source_name` define one sync scope. They namespace the
dlt source, cursor/deletion state and staging table. `cognee_pipeline_scope` lets Cognee namespace
the pipeline by this scope **and destination dataset**. Row IDs retain project/schema/table/PK;
Cognee derives document identity using the staging table and dataset. Two source names therefore
have independent lifecycles even if their selections overlap. Keep scope values stable, and do
not reuse one scope for different upstream databases. Raw dlt users must likewise keep a separate
pipeline per destination dataset; the Cognee scope attribute is interpreted by Cognee, not dlt.

### Upgrading the unmerged 0.1.0 implementation

The old `supabase_deletion_sweep` state and `supabase_rows` table may mix projects. This release
does **not** copy that ambiguous baseline into the new scope, delete old state, or purge old
documents. New scopes perform a full backfill (unless `initial_values` explicitly limits it).
Existing scoped state is reused on subsequent runs and after pipeline restoration.

For the follow-up configuration-switch fix, only cursor resource names move to `v2`. This forces
one backfill on upgrade even if old inactive configurations retained stale watermarks; the scoped
staging table, document identities and `known_ids` are unchanged. Old scoped incremental watermarks
are then retired after a successful scan. The unscoped legacy state described above stays untouched.

For an existing installation, stop old sync jobs, retain a backup, and first import into a **new
dataset name** using 1.6.3. Compare the new documents with the selected source before switching
readers. Retire old documents/datasets only after their ownership has been checked. In-place
automatic migration is not provided: using the old dataset retains legacy documents/anchors and
can show duplicates because the staging table identity changed. Never clear all dlt state to
work around this. Changing `source_name` likewise starts a separate scope and retains the old one.

Primary keys and cursors must be stable and non-null. Cursors should be monotonic and should advance
on every meaningful update (a database trigger for `updated_at` is recommended). Deletes are found
on the next sweep, not through PostgreSQL replication, so no replication or write privileges are
needed. Large tables still incur a primary-key-only scan per sync.
The deletion baseline is stored in `dlt` resource state, so projects with very large key sets should
budget state storage accordingly. This first version intentionally avoids logical replication/CDC,
which would require elevated PostgreSQL ownership or replication privileges.
Updates with a cursor **below** the saved watermark need an explicit backfill. The row query and
key sweep use separate read transactions; concurrent upstream writes are not a single consistent
snapshot. Quiesce writers for strict reconciliation, and do not claim CDC/transactional snapshot
guarantees. `initial_values` can intentionally omit old rows and should not be changed as a reset.
With the SQLite Cognee backend avoid dataset name `main` (SQLite's reserved schema name); use a
name such as `supabase_crm`. This is a core staging/read-back limitation, outside this connector.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests -m "not integration"
```

The default suite runs real dlt merges/state recovery with SQLite and mocked OAuth responses.
It is not an end-to-end Cognee test. `test_supabase_integration.py` is the legacy optional metadata
and source-construction smoke test; it does not extract data or prove graph cleanup.

State regressions include restoring a persisted pre-v2 cursor layout built from real dlt cursor
values, upgrading with and without an `initial_values` bound, and injecting an extraction failure
after the real deletion sweep has retired inactive cursors. They check rollback, restoration,
retry, repeated syncs and preservation of another project. The upgrade fixture reconstructs the
old state layout; it does not run an old connector binary. These are dlt/SQLite staging tests,
not evidence of atomic recovery across destination loading and graph processing.

For real PostgreSQL and Cognee storage tests, start a **disposable, loopback-only** PostgreSQL
cluster, then set (PowerShell):

```bash
$env:SUPABASE_TEST_DISPOSABLE = "1"
$env:SUPABASE_TEST_ADMIN_URL = "postgresql+psycopg://test_admin@127.0.0.1:55455/postgres"
uv run --with pytest --with pytest-asyncio pytest tests/test_postgres.py tests/test_cognee_e2e.py -v
```

The fixture creates a uniquely named database and reader role, grants SELECT only on synthetic
tables, and drops both in teardown. Preparation uses a different connection from the connector.
Never point this fixture at an existing application database or production cluster.

`test_postgres.py` verifies write rejection, column selection and the full staging deletion cycle.
`test_cognee_e2e.py` runs the connector and Cognee in a subprocess with isolated SQLite, Ladybug,
LanceDB and CHUNKS search, checking Data/graph/vector/search after updates and deletions while
preserving another project and dataset. Graph extraction, embeddings and tokenizer use
deterministic doubles; it does not validate live LLM quality, Supabase-hosted OAuth,
or GRAPH_COMPLETION responses. The subprocess does not inherit provider credentials. A missing
PostgreSQL opt-in produces a skip, not a successful E2E result.

The `Supabase Connector Tests` workflow runs on relevant pull requests, pushes to `main`/`dev`,
and manual dispatch. It uses Python 3.12 on Ubuntu, the committed runtime lock, pinned test tools,
and a disposable PostgreSQL 18 service bound to runner loopback. It requires no repository
secrets or model credentials and runs unit/state tests separately from the two storage tests.
The storage step fails if either test is skipped. PostgreSQL trust authentication is confined to
this disposable synthetic-data service; it is not a production configuration example. Model
extraction, embeddings and tokenizer remain doubles in CI. Local Windows results and workflow
static checks do not establish that the Linux GitHub Actions job has passed; that requires an
actual run after publishing the branch.
