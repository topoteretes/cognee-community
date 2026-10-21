# cognee-community-connector-snowflake

A Snowflake data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync selected tables into memory, incrementally, and forget rows when they are deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each table becomes a
**normal document** describing what it holds (its comments, columns, primary key and
foreign keys), and each row becomes a short document that names the tables its foreign
keys point to, so cognee links an order to its customer. Tables that did not change since
the last sync are skipped **without waking the warehouse**.

## Requirements

- cognee **1.6.4** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.11 to 3.13.
- `snowflake-connector-python` 3.12 to 4.x (installed with the package).
- A Snowflake account (the 30 day trial works) and a warehouse for reading rows.

## Install

```bash
uv pip install cognee-community-connector-snowflake
# or, from this monorepo:
cd packages/connector/snowflake && uv sync
```

## Setup

The connector logs in with a **key pair**, never a password.

1. **Make a key pair** on your machine. Keep `rsa_key.p8` private.

   ```bash
   mkdir -p ~/.snowflake && cd ~/.snowflake
   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   ```

   For an encrypted key, replace `-nocrypt` with `-v2 aes256` and set
   `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`.

2. **Register the public key on your user** in a Snowsight worksheet, with the key from
   `rsa_key.pub` as one line, without the `-----BEGIN/END` lines:

   ```sql
   ALTER USER "your_user" SET RSA_PUBLIC_KEY='MIIBIjANBgkqh...';
   ```

   Keep the double quotes if your user name has lowercase letters (trial sign-ups do);
   without them Snowflake looks for the upper-cased name and says the user does not exist.

3. **Find your login name.** Key-pair login uses the *login name*, which can differ from
   the user name shown in Snowsight. On trial accounts it is your email address:

   ```sql
   DESC USER "your_user" ->> SELECT "property", "value" FROM $1
     WHERE "property" IN ('LOGIN_NAME', 'RSA_PUBLIC_KEY_FP');
   ```

4. **Set the environment** and check the login once:

   ```bash
   export SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME      # Snowsight: your name > Account > View account details
   export SNOWFLAKE_USER=you@example.com             # the LOGIN_NAME from step 3
   export SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/rsa_key.p8
   export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
   uv run python examples/check_connection.py
   ```

   It prints your user, role, warehouse and the key's fingerprint, which should match
   `RSA_PUBLIC_KEY_FP` from step 3. A login error says which step to look at.

5. **Optional, recommended:** turn on change tracking for large tables, so a sync reads
   only the rows that changed instead of the whole table:

   ```sql
   ALTER TABLE SALES_DB.PUBLIC.ORDERS SET CHANGE_TRACKING = TRUE;
   ```

6. Set your `LLM_API_KEY` (or other LLM settings) like any other cognee run.

The role needs `USAGE` on the warehouse, database and schema, and `SELECT` on the tables.

## Usage

```python
import cognee
from cognee_community_connector_snowflake import snowflake_source

await cognee.remember(
    snowflake_source(tables=["SALES_DB.PUBLIC.CUSTOMERS", "SALES_DB.PUBLIC.ORDERS"]),
    dataset_name="snowflake",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # cognee's staging cap is 50; the connector has its own
)

answer = await cognee.recall("Which customers have open orders?", datasets=["snowflake"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `tables` | `SNOWFLAKE_TABLES` (comma-separated) | required, written `DATABASE.SCHEMA.TABLE` as in SQL |
| `account`, `user` | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER` | required; `user` is the login name |
| `private_key_path` or `private_key` | `SNOWFLAKE_PRIVATE_KEY_PATH`, `SNOWFLAKE_PRIVATE_KEY` | required, PEM |
| `private_key_passphrase` | `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | for encrypted keys |
| `role`, `warehouse` | `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE` | the user's defaults |
| `keys` | – | key columns for tables without a declared primary key |
| `filters` | – | a SQL condition per table, like `{"SALES_DB.PUBLIC.ORDERS": "REGION = 'EU'"}` |
| `max_rows` | – | `10000` rows per table; `0` turns the cap off |
| `full_refresh` | – | `False`; `True` reads every table again |
| `resource_name` | – | `snowflake_tables`; give each account its own name when several sync into one dataset. A name that already holds another account's sync is refused, so one account can never forget another's rows |
| `check_active` | – | optional host checkpoint, run before every query and every row |

After a run, `source.cognee_sync_stats` holds counts only (`rows`, `unchanged`,
`from_changes`, `deleted`, `failed`, `no_key`), never names or values.

## What cognee receives

A document per table, built only from what people wrote and the table's shape, so it
changes only when its meaning does:

```text
Snowflake table SALES_DB.PUBLIC.ORDERS
Every order placed by a customer. Refunded orders keep their row with status refunded.
Schema SALES_DB.PUBLIC: Customers and their orders

Primary key: ORDER_ID

Columns:
ORDER_ID (NUMBER(38,0), required): Order number
CUSTOMER_ID (NUMBER(38,0), required): References CUSTOMERS.CUSTOMER_ID
AMOUNT_USD (NUMBER(12,2)): Order value in US dollars, before tax

References:
CUSTOMER_ID references SALES_DB.PUBLIC.CUSTOMERS (CUSTOMER_ID)
```

And a document per row, with its table's description for context:

```text
Row of Snowflake table SALES_DB.PUBLIC.ORDERS
Table: Every order placed by a customer. Refunded orders keep their row with status refunded.
ORDER_ID: 101
CUSTOMER_ID: 1 (references SALES_DB.PUBLIC.CUSTOMERS)
AMOUNT_USD: 12000.00
STATUS: shipped
```

Empty values are left out, and values are written the same way on every sync (numbers,
dates, JSON), so an unchanged row is never processed again.

## How sync and forget-on-delete work

These were checked against a real Snowflake trial account:

- **Unchanged tables cost nothing.** `SHOW TABLES`, `SHOW COLUMNS`, `SHOW PRIMARY KEYS`,
  `SHOW IMPORTED KEYS` and `SYSTEM$LAST_CHANGE_COMMIT_TIME` run without a warehouse. The
  last of these moves on every insert, update and delete, so a table that did not change
  since the last sync is skipped. A sync where nothing changed ran on a suspended warehouse
  with auto resume off, and the warehouse stayed suspended.
- **Changed tables with change tracking** read only
  `CHANGES(INFORMATION => DEFAULT)` since the last sync: inserted and updated rows become
  documents, deleted rows are forgotten, and a row added and removed in between leaves no
  trace. Snowflake keeps that history for the table's retention time (1 day by default);
  after a longer gap the table is read in full instead.
- **Changed tables without change tracking** are read in full and compared by primary key,
  so rows that are gone are forgotten.
- **Filters** apply to both halves of an update, so a row updated out of a filter is
  forgotten and a row updated into it is added.
- **Dropped tables**, and tables removed from `tables`, are forgotten with all their rows.
- Forgotten rows are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column. dlt removes them on `merge`, and cognee's orphan cleanup removes them from the
  graph and vector stores.
- **Errors never delete.**
  - A schema that fails to list raises; it never makes its tables look dropped.
  - A table that fails keeps its saved state and is retried on the next run, while the
    other tables finish. The run raises only when no table could be synced, with the first
    table's reason, or on a login problem.
  - A table over `max_rows` is refused whole rather than synced in part, since the rows
    left out would look deleted.

### Why rows are documents and not relational rows

On cognee 1.6.4 a relational dlt source (one without the document-mode marker) only
reaches the graph on its **first** add: later merges, including deletes, do not change it,
and the documented rebuild (`incremental_loading=False, data_cache=False`) fails with
`IngestionError ... (415)`. The document path updates and forgets per row, like the Google
Calendar connector, so rows go down it. The cost is an LLM pass per changed row, which is
what `max_rows` and `filters` are for.

### Login errors, explained

| Snowflake says | Usually means |
|---|---|
| `390144` JWT token is invalid | `user` is not the login name, **or** the registered public key is another key. Compare `RSA_PUBLIC_KEY_FP` with the fingerprint `check_connection` prints. |
| `290404` / `250001` 404 Not Found | The account identifier is wrong. |
| `390189` | The role is not granted to this user. |
| `000608` | The warehouse is suspended and auto resume is off. |
| `002003` | A table or schema does not exist, or the role cannot see it. |

## Privacy

This connector reads business data. Be clear about what it does:

**What is read**

- From the selected tables only: their comments, column names, types and comments, primary
  and foreign keys, and the rows (all columns, after any filter you set).
- Nothing else in the account, and no query history.
- The connector only reads. It never creates, changes or deletes anything in Snowflake.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you, and use `filters` to leave out rows that should not leave Snowflake.
- dlt state stores the account name (`ORGANIZATION.ACCOUNT`) and, per table, its name, the
  last change time, the key columns, the filter and each row's primary key value. It does
  not store other column values.
- Logs and `cognee_sync_stats` contain counts and the connector's own error messages, never
  row values. The private key and passphrase never appear in a log or an error.

**How deletion is honoured**

- Deleted rows, rows that leave a filter, and dropped tables are forgotten on the **next
  sync**. Run syncs in the foreground: forgetting happens as part of `remember`.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- Every row is a document that cognee reads with an LLM, so large tables need a filter or a
  higher `max_rows`.
- A table without a declared primary key needs `keys=` for its rows to sync; otherwise only
  its description is synced.
- Views are not synced yet.
- Comment changes alone (`COMMENT ON ...`) do not move the last change time; the table
  description is still re-sent on every sync, and cognee only reprocesses it when its text
  changed.

## Testing

```bash
uv run pytest tests/
```

No Snowflake account or API key is needed. `tests/fake_snowflake.py` is an in-memory
Snowflake account: it answers the `SHOW` commands with the columns a real account returns,
moves a table's last change time on every write, and answers `CHANGES` with the same net
changes Snowflake reports.

- `test_auth.py` covers settings from the environment, account identifiers pasted from the
  browser, plain and encrypted keys, the key fingerprint, every login error above, and
  `check_connection`.
- `test_describe.py` covers the table document, one round of `SHOW` calls per schema,
  quoting rules, column types, and a schema that fails to list.
- `test_rows.py` covers row documents, skipping unchanged tables, full reads and key
  comparison, the change feed and its fallbacks, dropped and deselected tables, tables
  without keys, the row cap, filters, and a failing table that must not stop the others.
- `test_source.py` runs the source through a real dlt pipeline: saved state between runs, a
  deleted row leaving staging, names typed in any case, the host's checkpoint, a second
  account under the same `resource_name` being refused, and two accounts with their own
  names staying apart in one dataset.
- `test_forget.py` runs the real cognee pipeline (LLM and embeddings mocked) over three
  syncs: a deleted customer and the old value of an updated order leave the graph, the new
  value enters it, and a sync where a deletion is the only change still forgets.
