"""A fake Snowflake session for tests: answers SQL through a function.

The SHOW outputs below have the same columns and value shapes a real trial
account returned (connector 4.8), trimmed to what the connector reads.
"""

import json


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        answer = self.conn.answers(sql)
        if isinstance(answer, Exception):
            raise answer
        self.rows = answer
        self.description = [("column",)]

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, answers):
        self.answers = answers
        self.sql = []
        self.closed = False

    def cursor(self, cursor_class=None):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def column(table, name, kind="TEXT", comment="", required=False, **extra):
    data_type = {"type": kind, "nullable": not required, **extra}
    return {
        "table_name": table,
        "schema_name": "SALES",
        "column_name": name,
        "data_type": json.dumps(data_type),
        "null?": "NOT_NULL" if required else "true",
        "comment": comment,
        "database_name": "SALES_DB",
    }


def sales_schema():
    """A schema like the demo account's: customers, orders, and an order link."""
    return {
        "schemas": [
            {"name": "SALES", "comment": "Customers and their orders"},
            {"name": "PUBLIC", "comment": ""},
        ],
        "tables": [
            {
                "name": "CUSTOMERS",
                "comment": "One row per paying customer.",
                "rows": 3,
                "bytes": 1536,
                "created_on": "2026-10-19 20:17:32",
                "change_tracking": "OFF",
            },
            {
                "name": "ORDERS",
                "comment": "Every order placed by a customer.",
                "rows": 3,
                "bytes": 1536,
                "created_on": "2026-10-19 20:17:32",
                "change_tracking": "ON",
            },
        ],
        "columns": [
            column(
                "CUSTOMERS",
                "CUSTOMER_ID",
                "FIXED",
                "Stable customer id",
                True,
                precision=38,
                scale=0,
            ),
            column("CUSTOMERS", "NAME", "TEXT", "Company name", length=16777216),
            column("ORDERS", "ORDER_ID", "FIXED", "Order number", True, precision=38, scale=0),
            column("ORDERS", "CUSTOMER_ID", "FIXED", "", True, precision=38, scale=0),
            column(
                "ORDERS", "AMOUNT_USD", "FIXED", "Order value in US dollars", precision=12, scale=2
            ),
            column("ORDERS", "STATUS", "TEXT", "open, shipped or refunded", length=20),
            column("ORDERS", "UPDATED_AT", "TIMESTAMP_NTZ", "Last change", True),
        ],
        "primary_keys": [
            {"table_name": "CUSTOMERS", "column_name": "CUSTOMER_ID", "key_sequence": 1},
            {"table_name": "ORDERS", "column_name": "ORDER_ID", "key_sequence": 1},
        ],
        "foreign_keys": [
            {
                "fk_name": "FK_ORDERS_CUSTOMER",
                "fk_table_name": "ORDERS",
                "fk_column_name": "CUSTOMER_ID",
                "pk_database_name": "SALES_DB",
                "pk_schema_name": "SALES",
                "pk_table_name": "CUSTOMERS",
                "pk_column_name": "CUSTOMER_ID",
                "key_sequence": 1,
            }
        ],
    }


def metadata_session(schemas):
    """A session answering the SHOW commands for ``{(database, schema): sales_schema()}``."""

    def answers(sql):
        for (database, schema), meta in schemas.items():
            where = f'"{database}"."{schema}"'
            if sql == f'SHOW SCHEMAS IN DATABASE "{database}"':
                return meta["schemas"]
            if sql == f"SHOW TABLES IN SCHEMA {where}":
                return meta["tables"]
            if sql == f"SHOW COLUMNS IN SCHEMA {where}":
                return meta["columns"]
            if sql == f"SHOW PRIMARY KEYS IN SCHEMA {where}":
                return meta["primary_keys"]
            if sql == f"SHOW IMPORTED KEYS IN SCHEMA {where}":
                return meta["foreign_keys"]
        raise AssertionError(f"unexpected SQL: {sql}")

    return FakeConnection(answers)


class FakeAccount:
    """An in-memory Snowflake account for the row sync.

    It answers the SHOW commands from a ``sales_schema()``-like dict, keeps rows
    per table, and moves a table's SYSTEM$LAST_CHANGE_COMMIT_TIME on every
    insert, update and delete, the way a real account did.
    """

    def __init__(self, database="SALES_DB", schema="SALES", meta=None):
        self.database, self.schema = database, schema
        self.meta = meta or sales_schema()
        self.rows: dict[str, dict] = {table["name"]: {} for table in self.meta["tables"]}
        self.commits: dict[str, int] = dict.fromkeys(self.rows, 1)
        self.sql: list[str] = []
        self.failing: dict[str, Exception] = {}
        self._clock = 100

    # -- building and changing data ------------------------------------------
    def key_of(self, table):
        return [k["column_name"] for k in self.meta["primary_keys"] if k["table_name"] == table]

    def _commit(self, table):
        self._clock += 1
        self.commits[table] = self._clock

    def insert(self, table, **row):
        key = tuple(row.get(column) for column in self.key_of(table) or sorted(row))
        self.rows[table][key] = row
        self._commit(table)

    def update(self, table, key, **changes):
        self.rows[table][key].update(changes)
        self._commit(table)

    def delete(self, table, key):
        del self.rows[table][key]
        self._commit(table)

    def drop(self, table):
        self.meta["tables"] = [t for t in self.meta["tables"] if t["name"] != table]
        del self.rows[table]

    # -- the two query functions the sync takes --------------------------------
    def run(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SELECT SYSTEM$LAST_CHANGE_COMMIT_TIME"):
            table = params[0].split(".")[-1].strip('"')
            return [{"LAST_CHANGE": self.commits[table]}]
        return metadata_session({(self.database, self.schema): self.meta}).answers(sql)

    def fetch(self, sql):
        self.sql.append(sql)
        table = sql.split(" FROM ")[1].split(" ")[0].split(".")[-1].strip('"')
        if table in self.failing:
            raise self.failing[table]
        rows = [dict(row) for _, row in sorted(self.rows[table].items(), key=lambda kv: str(kv[0]))]
        yield from rows
