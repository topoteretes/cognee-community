# cognee-community-connector-databricks

A Databricks data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync notebooks, Unity Catalog tables and the results of named queries into memory,
incrementally, and forget what is deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Notebooks and tables live
behind different APIs, and the connector joins them through Unity Catalog **lineage**:
each notebook document names the tables it reads and writes, and each table document
names the notebooks that build and read it. So cognee can answer "which notebook builds
this table?" and "what reads this table if I change it?".

## Requirements

- cognee **1.6.3** or newer (below 2). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.10 to 3.14.
- A Databricks workspace with Unity Catalog (the free [Free Edition](https://www.databricks.com/learn/free-edition)
  works) and a personal access token.
- No other libraries: the client is built on dlt's own REST toolkit.

## Install

```bash
uv pip install cognee-community-connector-databricks
# or, from this monorepo:
cd packages/connector/databricks && uv sync
```

## Setup

1. **Create a personal access token.** In the workspace, open your profile picture (top
   right) > **Settings** > **Developer** > **Access tokens** > **Generate new token**, and
   copy it. Give it a lifetime; the connector only reads.

2. **Set the environment:**

   ```bash
   export DATABRICKS_HOST=https://dbc-1234abcd-5678.cloud.databricks.com   # the browser URL works too
   export DATABRICKS_TOKEN=dapi...
   export DATABRICKS_PATHS=~/analytics,/Shared/reports    # notebook folders; ~ is your home folder
   export DATABRICKS_TABLES=main.sales                    # a catalog, schema or table
   export DATABRICKS_WAREHOUSE_ID=1234abcd5678            # only for named queries
   ```

3. **Check it once:**

   ```bash
   uv run python examples/check_connection.py
   ```

   It prints what each folder and table selection holds, whether lineage answers, and the
   warehouse's state (read without starting it), and says in plain words what is wrong.

4. Set your `LLM_API_KEY` (or other LLM settings) like any other cognee run.

The token's user needs `CAN READ` on the folders, and `USE CATALOG`, `USE SCHEMA` and
`SELECT` (or `BROWSE`) on the tables. Named queries also need `CAN USE` on the warehouse.

## Usage

```python
import cognee
from cognee_community_connector_databricks import databricks_source

await cognee.remember(
    databricks_source(paths=["~/analytics"], tables=["main.sales"]),
    dataset_name="databricks",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # cognee's staging cap is 50; a workspace has more
)

answer = await cognee.recall("Which notebook builds the revenue table?", datasets=["databricks"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `paths` | `DATABRICKS_PATHS` (comma-separated) | folders whose notebooks are synced, with every folder below them |
| `tables` | `DATABRICKS_TABLES` (comma-separated) | a catalog (`main`), a schema (`main.sales`) or a table (`main.sales.orders`) |
| `host`, `token` | `DATABRICKS_HOST`, `DATABRICKS_TOKEN` | required |
| `lineage` | – | `True`; `False` lists the synced tables a notebook's code mentions instead |
| `queries` | – | none; named read-only queries like `{"open_orders": "SELECT ..."}` |
| `warehouse_id` | `DATABRICKS_WAREHOUSE_ID` | required with `queries` |
| `max_query_rows`, `query_timeout` | – | `100` rows, `30` seconds (5 to 50) |
| `full_refresh` | – | `False`; `True` exports and re-sends everything |
| `resource_name` | – | `databricks_workspace`; give each workspace its own name when several sync into one dataset. A name that already holds another workspace's sync is refused |
| `check_active` | – | optional host checkpoint, run before every API call and every row |

At least one of `paths`, `tables` and `queries` is needed: an empty selection is refused,
because it would forget everything synced before.

After a run, `source.cognee_sync_stats` holds counts only (`notebooks`, `tables`,
`queries`, each with `_changed` and `_deleted`, plus `redacted` and `failed`), never
names or content.

## What cognee receives

These are shortened from a real sync of a Free Edition workspace. A document per notebook,
with its markdown as prose and its code as code blocks:

````text
Databricks notebook build_revenue
Folder: ~/cognee_demo
Language: Python

Reads tables: workspace.cognee_demo.customers, workspace.cognee_demo.orders
Writes tables: workspace.cognee_demo.revenue_by_customer

# Build revenue by customer
Joins orders to customers and writes one row per customer with their total spend.

```python
from pyspark.sql import functions as F

API_KEY = "[REDACTED]"  # fake secret, to test scrubbing
orders = spark.table("workspace.cognee_demo.orders")
```
````

A document per table or view, from what people wrote and the table's shape:

```text
Databricks table workspace.cognee_demo.orders

Every order placed by a customer. Refunded orders keep their row with status refunded.

Schema workspace.cognee_demo: Customers and their orders

Owner: someone@example.com

Columns:
order_id (bigint, required): Order number
customer_id (bigint, required): References customers.customer_id
amount_usd (decimal(12,2)): Order value in US dollars, before tax

Primary key: order_id

References:
customer_id references workspace.cognee_demo.customers (customer_id)

Lineage:
Feeds: workspace.cognee_demo.open_orders, workspace.cognee_demo.revenue_by_customer
Read by: notebook build_revenue (~/cognee_demo), notebook open_orders_report (~/cognee_demo)
```

And a document per named query, with its result:

````text
Databricks query orders_with_managers

SQL:
```sql
SELECT o.order_id, c.name, c.account_manager, o.product, o.amount_usd, o.status FROM workspace.cognee_demo.orders o JOIN workspace.cognee_demo.customers c USING (customer_id) ORDER BY o.order_id
```

Result: 3 rows
| order_id | name | account_manager | product | amount_usd | status |
| --- | --- | --- | --- | --- | --- |
| 101 | Contoso | Ayesha Khan | GPU credits | 12000.00 | shipped |
| 102 | Fabrikam | Bilal Ahmed | Support plan | 2400.00 | shipped |
| 104 | Contoso | Ayesha Khan | Training | 1500.00 | open |
````

## How sync and forget-on-delete work

These were checked against a real Free Edition workspace:

- **Notebooks.** Every sync lists the selected folders in full (the listing is one folder
  deep, so the connector walks the tree, skipping hidden folders like `.db_internal`). The
  listing gives each notebook's permanent id and its modified time, so only notebooks
  whose time moved are exported. Export format `SOURCE` is code and markdown only: cell
  outputs, which can hold real rows and credentials, are never read. A notebook moved or
  renamed keeps its id and its document.
- **Tables.** Every sync lists the selected catalogs and schemas. The listing carries
  each table's permanent id (kept through a rename), columns, comment, owner and view
  definition, but not its keys: a table's detail is read only when its `updated_at` moved,
  which it does on any metadata change (`COMMENT ON`, a new constraint) but not on data
  changes. A table's document is rendered on every sync and only sent on when its text
  changed.
- **Lineage** is read per table from the lineage API, without a warehouse. A notebook is
  exported again when its lineage changed even if its code did not (a notebook that ran
  for the first time), and a table's document changes when the notebooks around it do.
- **Named queries** run on every sync, because only running a query shows whether its
  result changed. A result is sent on only when it changed.
- **Forgetting.** A notebook or table missing from a complete listing is looked up once
  more by its last path or name. It is forgotten only when Databricks says it does not
  exist, or something else is there now. A query no longer named is forgotten too.
  Forgotten documents are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column: dlt removes them on `merge`, and cognee's orphan cleanup removes them from the
  graph and vector stores.
- **Errors never delete.**
  - A folder, schema or query that fails keeps its saved state and is retried on the next
    run, while the others finish. If a subfolder fails, its whole folder fails, so a
    partial walk never makes notebooks look deleted.
  - A table whose lineage could not be read fails its schema for that run, so no document
    loses its links for one sync.
  - A permission error on one object (HTTP 403 with a code like `PERMISSION_DENIED`) fails
    that object, not the sync. A refused token (401, or 403 with no named code) stops the
    whole sync.
  - The run raises only when everything selected failed, and then nothing is forgotten.

### Lineage, explained

Unity Catalog records lineage when code runs: the lineage API showed a notebook run about
two minutes after it finished. It lists the notebooks that wrote a table (with the tables
they read) and the notebooks that read it, including notebooks that only read. So:

- A notebook that never ran has no lineage. Its document lists the synced tables its code
  mentions instead (`Mentions tables: ...`), and switches to `Reads tables` and
  `Writes tables` once it runs.
- Lineage names notebooks by id. Notebooks in the synced folders are named; notebooks
  elsewhere are only counted ("1 notebook outside the synced folders"), and notebooks of
  other workspaces are left out.
- Lineage keeps listing a notebook after it is deleted (checked: still listed minutes
  later). Notebooks the connector forgot, deleted or moved out of the synced folders,
  are left out of table documents; a notebook restored from the Trash comes back. A
  notebook that was never in the synced folders and was deleted since may still be
  counted, since lineage gives only its id.
- The `system.access.table_lineage` table stayed empty on a Free Edition workspace, so the
  connector uses the lineage API.

## Privacy

This connector reads code and data descriptions, and with named queries, data. Be clear
about what it does:

**What is read**

- From the selected folders only: each notebook's source, never its cell outputs.
- From the selected catalogs and schemas: table and view names, comments, column names,
  types and comments, keys, owners and view definitions. Never table properties: they
  hold column statistics, which are real values from the data. No rows are read.
- Lineage between the synced tables and notebooks.
- With named queries only: the rows those queries return, up to `max_query_rows` and 1 MB.
- The connector only reads. It never creates, changes, runs or deletes anything in the
  workspace, apart from the named queries you give it.

**What is changed before it leaves the connector**

- Anything that looks like a credential in a notebook, view definition or query result is
  replaced with `[REDACTED]`: Databricks, AWS, OpenAI-style, GitHub, Slack and Google keys,
  quoted values given to names like `password` or `api_key`, `Bearer` tokens, passwords in
  connection strings and private keys. `dbutils.secrets.get(...)` is left as it is, since
  it holds no secret. A credential written in plain prose is not caught.
- Folders under `/Users/<email>/` are shown as `~/`, so a user's email never appears in a
  notebook document. A table's owner, often an email address, is included on purpose:
  it answers "who owns this table?".

**Where it goes**

- The documents above go to your configured LLM and embedding providers during cognify,
  like any document you add to cognee. Use local models if that matters to you.
- dlt state stores the workspace id, per notebook its id, path, modified time and a short
  hash of its lineage, the ids of notebooks it forgot, per table its id, name, updated
  time, keys and a hash of its document, and per query a hash of its result. Paths under `/Users/` contain that user's
  email. It stores no notebook source, column values or query results.
- Logs and `cognee_sync_stats` contain counts and the connector's own messages. Error
  messages carry the HTTP status and Databricks' error code only, never Databricks' own
  message, which repeats the request's path. The token never appears in a log or an error.

**Who can read it**

- Anyone who can read the cognee dataset can ask about these notebooks, tables and query
  results, whatever their own Databricks access is. With cognee's access control on (the
  default), that is the user who ran the sync and anyone the dataset is shared with, so
  sync into a dataset only the people allowed to see these folders and tables can read.

**How deletion is honoured**

- Deleted notebooks, dropped tables and queries no longer named are forgotten on the
  **next sync**. Run syncs in the foreground: forgetting happens as part of `remember`.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- Lineage only knows notebooks that ran, and shows a run about two minutes after it.
- A notebook without lineage lists the synced tables its code mentions by full name; a
  table named without its catalog, or built from a variable, is not found. Those
  mentions are refreshed when the notebook changes.
- Opening a notebook in Databricks' notebook editor can save it and move its modified
  time without any edit (seen on a Free Edition workspace). The notebook is then exported
  again, but cognee does not process a document again when its text did not change.
- Workspace files (`.py`, `.sql` files that are not notebooks), dashboards and jobs are
  not synced.
- Named queries run on every sync and wake the warehouse each time. Give them an
  `ORDER BY`, so their document only changes when their result does.
- A query is guarded to start with `SELECT`, `WITH`, `SHOW` or `DESCRIBE`; that guards
  against a typo, not a determined user. The token's permissions decide what can run.

## Testing

```bash
uv run pytest tests/
```

No Databricks workspace or API key is needed. `tests/fake_databricks.py` is an in-memory
workspace that behaves like the real one: folder listings are one level deep and answer
`{}` when empty, editing a notebook keeps its id, table listings carry no keys, a rename
keeps a table's id, lineage is recorded the way Unity Catalog records it, and statements
answer in chunks.

- `test_client.py` covers the REST client: paging, retries on 429 and 5xx, refused tokens
  told apart from a missing permission, not-found codes, error messages without
  Databricks' own text, and `check_active` before every request.
- `test_notebooks.py` covers the walk, change detection, moves, renames and deletes, a
  listing contradicted by a lookup, failures that must not delete, rendering every
  notebook language, folders without emails, and secret scrubbing.
- `test_tables.py` covers table and view documents, reading keys only on a change,
  renames, drops, a recreated table, dropped schemas, catalog, schema and table
  selections, and failures that must not delete.
- `test_lineage.py` covers both directions of the join, notebooks outside the synced
  folders or in another workspace, the fallback for notebooks that never ran, a first
  run, a renamed notebook, a failing lineage call, and lineage turned off.
- `test_queries.py` covers named queries: the request's caps and timeout, results in
  chunks, changed and unchanged results, failures, read-only checks and settings.
- `test_source.py` runs the source through a real dlt pipeline: saved state between
  runs, a deleted notebook leaving staging, a second workspace under the same
  `resource_name` being refused, two workspaces with their own names staying apart, the
  host's checkpoint, and selections from the environment.
- `test_check.py` covers `check_connection`.
- `test_forget.py` runs the real cognee pipeline (LLM and embeddings mocked) over three
  syncs: a deleted notebook, a dropped table and the old text of an edited notebook leave
  the graph, the new text enters it, and a sync where a dropped table is the only change
  still forgets. A deleted item's Data record must be removed, not replaced by an emptied
  document, and the documents that stay keep theirs, so the test fails if either the
  `_deleted` rows or the `hard_delete` column are taken away.
