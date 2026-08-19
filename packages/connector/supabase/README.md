# cognee-community-connector-supabase

A read-only Supabase PostgreSQL connector for [cognee](https://github.com/topoteretes/cognee).
It incrementally syncs explicitly selected tables and columns, turns each row into a normal
cognee document, and forgets rows that were deleted upstream.

The connector uses existing cognee and `dlt` ingestion primitives; it does not add a parallel
storage or ingestion path.

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
grant select on all tables in schema public to cognee_reader;
alter default privileges in schema public grant select on tables to cognee_reader;
alter role cognee_reader set default_transaction_read_only = on;
```

Do not grant `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, table ownership, or replication privileges.
Use a direct connection or Supabase's session pooler. Transaction-mode poolers cannot reliably
preserve session-level read-only settings.

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

Run the same call again to sync only rows whose cursor advanced. The connector also scans only
the selected tables' primary-key columns to detect deletions. Changing a table's selected columns,
primary key, or cursor changes its state fingerprint and safely triggers a backfill.

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

## Sync and deletion semantics

The cursor query fetches changed rows. A separate primary-key-only sweep compares the current keys
with connector state and emits tombstones for missing rows. If a corpus transitions from non-empty
to empty, the connector temporarily keeps one metadata-only sync anchor in staging; this gives
cognee a non-empty reconciliation set so all old business documents are forgotten. The anchor is
removed automatically when rows return.

Primary keys and cursors must be stable and non-null. Cursors should be monotonic and should advance
on every meaningful update (a database trigger for `updated_at` is recommended). Deletes are found
on the next sweep, not through PostgreSQL replication, so no replication or write privileges are
needed. Large tables still incur a primary-key-only scan per sync.
The deletion baseline is stored in `dlt` resource state, so projects with very large key sets should
budget state storage accordingly. This first version intentionally avoids logical replication/CDC,
which would require elevated PostgreSQL ownership or replication privileges.

## Testing

```bash
uv run pytest tests
```

The default suite uses SQLite and mocked HTTP responses; it needs no Supabase credentials. To run
the opt-in live smoke test:

```bash
set SUPABASE_TEST_DATABASE_URL=postgresql://...
set SUPABASE_TEST_PROJECT_REF=...
set SUPABASE_TEST_TABLE=...
set SUPABASE_TEST_CURSOR=updated_at
uv run pytest -m integration tests/test_supabase_integration.py
```

The live role must satisfy the same read-only checks. The smoke test only discovers metadata and
extracts configured columns; it never mutates the Supabase project.
