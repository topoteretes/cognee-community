# cognee-community-connector-redshift

An Amazon Redshift data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync selected tables, views and SQL query results into memory, incrementally, and forget rows
when they are deleted upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each row becomes a short
**document** (`column: value` lines) that flows through cognify, and each table gets a
**table card** with its comment and its column names, types and comments, so the LLM knows
what the columns mean. A table's rows and its card share a node set
(`redshift:table:public.tickets`), so they are linked in the graph.

## Requirements

- cognee **1.6.4** (pinned). The connector relies on document mode with table-scoped cleanup
  and per-row node sets (`DOCUMENT_SYNC_VERSION` 2).
- Python 3.11 to 3.13.
- A provisioned Redshift cluster or a Redshift Serverless workgroup.

## Install

```bash
uv pip install cognee-community-connector-redshift
# for DirectConnection (a driver connection, e.g. through an SSH tunnel):
uv pip install "cognee-community-connector-redshift[direct]"
# or, from this monorepo:
cd packages/connector/redshift && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_redshift import (
    DataApiConnection,
    RedshiftQuery,
    RedshiftTable,
    redshift_source,
)

source = redshift_source(
    [
        RedshiftTable(
            "public.support_tickets",
            primary_key="ticket_id",
            cursor_column="updated_at",
            columns=["subject", "status", "priority", "customer"],  # optional allowlist
            where="deleted_at IS NULL",  # optional; rows that stop matching are forgotten
        ),
        RedshiftQuery(
            name="revenue_by_customer",
            sql="SELECT customer_id, SUM(amount) AS revenue, MAX(paid_at) AS paid_at "
            "FROM billing.invoices GROUP BY customer_id",
            primary_key="customer_id",
            cursor_column="paid_at",
        ),
    ],
    connection=DataApiConnection(database="dev", workgroup_name="analytics"),
)

await cognee.remember(
    source,
    dataset_name="redshift",
    primary_key="id",
    write_disposition="merge",  # required: incremental runs emit only changes and deletions
)

answer = await cognee.recall(
    "Which customers have open high-priority tickets?", datasets=["redshift"]
)
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py` for
the full flow.

| Argument | Meaning |
|---|---|
| `RedshiftTable(name, primary_key, cursor_column=None, columns=None, where=None)` | A table or view. `name` is `schema.table` (lower case unless it was created quoted). `primary_key` is one column or a list. |
| `RedshiftQuery(name, sql, primary_key, cursor_column=None)` | A `SELECT` whose result is synced like a table. `name` identifies its documents. |
| `connection` | `DataApiConnection(...)` (default, from `REDSHIFT_*` env vars) or `DirectConnection(...)`. |
| `include_table_cards` | Also ingest one card per table. Default `True`. |
| `full_refresh` | Read every row this run, not just rows past the cursor. |
| `resource_name` | Name for this connection's dlt state and staging tables. Default `redshift`; give each cluster its own when several sync into one dataset. |

After a run, `source.cognee_sync_stats` holds counts only (rows, deleted, late, skipped,
duplicates, selections), never row content.

## Setup

### 1. Choose how to authenticate

`DataApiConnection` takes its settings as arguments or from the environment. AWS credentials
come from the normal boto3 chain (env vars, `~/.aws`, an instance, task or Lambda role).

| Target | Arguments | Environment |
|---|---|---|
| Serverless, IAM identity | `database`, `workgroup_name` | `REDSHIFT_DATABASE`, `REDSHIFT_WORKGROUP_NAME` |
| Provisioned, temporary credentials for a database user | `database`, `cluster_identifier`, `db_user` | `REDSHIFT_DATABASE`, `REDSHIFT_CLUSTER_IDENTIFIER`, `REDSHIFT_DB_USER` |
| Provisioned, IAM identity | `database`, `cluster_identifier` | as above, without `REDSHIFT_DB_USER` |
| Either, database user and password in Secrets Manager | add `secret_arn` | `REDSHIFT_SECRET_ARN` |

Set the region with `region_name=` or `AWS_REGION`. No password is ever passed to the
connector: the Data API gets temporary credentials or reads the secret itself.

### 2. Allow the Data API calls

Attach a policy like this to the IAM identity that runs the sync, keeping only the
credentials statement you use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunStatements",
      "Effect": "Allow",
      "Action": "redshift-data:ExecuteStatement",
      "Resource": [
        "arn:aws:redshift-serverless:REGION:ACCOUNT:workgroup/WORKGROUP_ID",
        "arn:aws:redshift:REGION:ACCOUNT:cluster:CLUSTER"
      ]
    },
    {
      "Sid": "ReadOwnStatements",
      "Effect": "Allow",
      "Action": [
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult",
        "redshift-data:CancelStatement"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ServerlessCredentials",
      "Effect": "Allow",
      "Action": "redshift-serverless:GetCredentials",
      "Resource": "arn:aws:redshift-serverless:REGION:ACCOUNT:workgroup/WORKGROUP_ID"
    },
    {
      "Sid": "ClusterCredentialsForDbUser",
      "Effect": "Allow",
      "Action": "redshift:GetClusterCredentials",
      "Resource": [
        "arn:aws:redshift:REGION:ACCOUNT:dbuser:CLUSTER/cognee_reader",
        "arn:aws:redshift:REGION:ACCOUNT:dbname:CLUSTER/dev"
      ]
    }
  ]
}
```

Use `redshift:GetClusterCredentialsWithIAM` for a cluster with your IAM identity, or
`secretsmanager:GetSecretValue` on the secret when you pass `secret_arn`.

### 3. Grant read access in the database

Give the database user only what it should ingest:

```sql
GRANT USAGE ON SCHEMA public TO cognee_reader;
GRANT SELECT ON public.support_tickets TO cognee_reader;
COMMENT ON TABLE public.support_tickets IS 'Tickets from the help desk and email.';
COMMENT ON COLUMN public.support_tickets.priority IS 'p1 = outage, p4 = question';
```

Comments are optional, but they end up in the table card and help the LLM a lot.

## Reaching a cluster inside a VPC

Redshift usually sits in a private subnet. There are two ways in.

**The Data API (default, no tunnel).** `DataApiConnection` calls the `redshift-data` HTTPS
endpoint of the AWS region, and AWS runs the statement inside your VPC. The cluster needs no
public access, no inbound security-group rule and no bastion. The machine running the sync
only needs to reach AWS APIs. If it has no internet access either (a private subnet), add an
interface VPC endpoint for `com.amazonaws.REGION.redshift-data` (and for `secretsmanager` if
you use a secret).

**A tunnel and a driver.** When the Data API is not an option, connect with
`DirectConnection` through a local port forward. Two common ways:

1. **SSH through a bastion host** in a public subnet that can reach the cluster on port 5439:

   ```bash
   ssh -N -L 5439:my-cluster.abc123.us-east-1.redshift.amazonaws.com:5439 ec2-user@BASTION_IP
   ```

2. **AWS Systems Manager**, with no open SSH port, from any instance that has the SSM agent
   and can reach the cluster:

   ```bash
   aws ssm start-session --target i-0123456789abcdef0 \
     --document-name AWS-StartPortForwardingSessionToRemoteHost \
     --parameters '{"host":["my-cluster.abc123.us-east-1.redshift.amazonaws.com"],"portNumber":["5439"],"localPortNumber":["5439"]}'
   ```

Leave the tunnel running and point the driver at `localhost`. `DirectConnection` passes its
keyword arguments to `redshift_connector.connect`, so a password or IAM both work:

```python
from cognee_community_connector_redshift import DirectConnection

connection = DirectConnection(
    host="localhost",
    port=5439,
    database="dev",
    user="cognee_reader",
    password="...",
    # TLS still verifies the cluster's certificate. Over a tunnel the host
    # name is localhost, so check the CA but not the name:
    ssl=True,
    sslmode="verify-ca",
)
# Or IAM: DirectConnection(iam=True, host="localhost", port=5439, database="dev",
#                          db_user="cognee_reader", cluster_identifier="my-cluster",
#                          region="us-east-1")
```

`DirectConnection(connect=..., paramstyle=...)` also takes any DB-API connection factory.

## How sync and forget-on-delete work

- **Cursor.** With `cursor_column` (a timestamp such as `updated_at`), each run reads the rows
  with `cursor >= last value`, ordered by the cursor, and remembers the last value it saw. The
  value is passed as a parameter, never formatted into the SQL. `>=` re-reads rows that share
  the last timestamp instead of skipping rows committed in the same second; re-reading an
  unchanged row does not change its document. Without a cursor, every run reads the whole
  selection.
- **Deleted rows.** Redshift has no change feed. So each incremental run also reads the
  primary keys of the selection (one column, cheap on a columnar store). Keys that
  disappeared are emitted as `_deleted` tombstones: dlt drops the rows on merge, and cognee's
  orphan cleanup removes them from the graph and vector stores.
- **Late rows.** Keys that appear below the cursor (a backfill, or a row that only now matches
  `where`) are fetched by key, so they are not lost.
- **Soft deletes.** Put them in `where` (`"deleted_at IS NULL"`). A row that stops matching is
  forgotten like a deleted one.
- **Dropped tables.** A table that was synced before and no longer appears in
  `svv_all_tables` (dropped, or the user lost access) is forgotten with its card. A table
  that was never synced and cannot be found is an error, so a typo never ingests nothing
  quietly.
- **Changed selection.** Removing a selection forgets its rows. Changing its columns, key,
  cursor or `where` re-reads it once.
- **Errors never delete.** A statement that fails, is aborted or times out (`timeout=`,
  default 15 minutes, then it is cancelled) aborts the run before dlt commits rows or state.
  The next run starts from the same cursor.
- **Duplicate keys.** Redshift does not enforce primary keys. When two rows share one, the
  first is kept and a warning is logged.

## Privacy

**What is read**

- The columns you select (all by default) of the rows you select, and the catalog entries of
  selected tables: table and column names, types and comments. Binary columns are skipped.
- The connector only runs `SELECT` statements. It never writes to Redshift.

**Where it goes**

- Rows and table cards become cognee documents. During cognify they are sent to your
  configured LLM and embedding providers, like any document you add to cognee. Narrow the
  data with `columns=` and `where=`, or use local models.
- dlt state stores, per selection, the cursor value and the primary keys of synced rows, so
  deletions can be detected. It does not store row content.
- Logs and `cognee_sync_stats` contain counts and table names only.

**How deletion is honoured**

- Deleted rows, rows that leave `where`, dropped tables and removed selections are forgotten
  on the **next sync**, as part of `remember`.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- An update that does not change the cursor column is not seen until a `full_refresh=True`
  run. The same goes for updates to rows whose cursor is NULL (new rows are still found
  through the key list).
- The key list of each selection is kept in dlt state and each incremental run reads all keys.
  This suits tables of up to a few million rows; for bigger fact tables, sync an aggregate
  with `RedshiftQuery` or narrow the rows with `where`.
- Each row is its own document and goes through the LLM. Pick the tables where text matters
  (tickets, accounts, notes), not raw event streams.
- dlt keeps the cursors outside cognee's data. If you prune cognee's data but keep the dlt
  pipeline directory, run once with `full_refresh=True`.
- Table cards come from `svv_all_tables` and `svv_all_columns`. Queries have no card.

## Testing

```bash
uv run pytest tests/
```

No AWS account or API key is needed.

- `test_redshift_sync.py` runs the sync against a SQLite-backed stand-in for Redshift
  (`tests/fake_redshift.py`; schema-qualified tables plus `svv_all_*` catalog tables), through
  the connector's own `DirectConnection`. It covers rendering, cards, column allowlists, NULL
  and duplicate keys, composite keys, queries, parameters instead of SQL formatting, the
  cursor (ties at the boundary, late rows, full refresh, changed selections), deleted rows,
  soft deletes, dropped tables, removed selections, and failed statements that change nothing.
- `test_redshift_connection.py` stubs the Data API with botocore's `Stubber`, which validates
  every request against the AWS API model: statement polling, `NextToken` paging, value
  types, failed and cancelled statements, Serverless with a secret, and settings validation.
- `test_redshift_source.py` runs the source through a real dlt pipeline and checks that
  tombstones remove staged rows and that only `merge` is accepted.
- `test_redshift_cognee.py` runs the real cognee pipeline (LLM and embeddings mocked) and
  checks that deleted rows and dropped tables leave the graph while other tables stay.
- `test_redshift_postgres.py` runs the SQL against a real Postgres server, which speaks the
  same protocol, with typed columns and server-side parameters. It is skipped unless
  `REDSHIFT_TEST_POSTGRES_DSN` is set:

  ```bash
  docker run -d --rm -e POSTGRES_PASSWORD=test -e POSTGRES_DB=dev \
      -p 127.0.0.1:55432:5432 postgres:16-alpine
  REDSHIFT_TEST_POSTGRES_DSN=postgresql://postgres:test@127.0.0.1:55432/dev uv run pytest tests/
  ```
