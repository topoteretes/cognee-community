"""Standalone smoke test against a real ClickHouse server — no cognee/LLM needed.

Proves the connector's actual logic (sync_rows, build_clickhouse_client,
sync_table_comments) works against a real ClickHouse instance, not just the
fake client used in unit tests.
"""

import os
import time

from cognee_community_connector_clickhouse.clickhouse import (
    build_clickhouse_client,
    sync_rows,
    sync_table_comments,
)

HOST = os.environ["CLICKHOUSE_HOST"]
PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
USER = os.environ.get("CLICKHOUSE_USER", "default")
PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
DATABASE = os.environ["CLICKHOUSE_DATABASE"]
TABLE = os.environ["CLICKHOUSE_TABLE"]
CURSOR_COLUMN = os.environ["CLICKHOUSE_CURSOR_COLUMN"]


def show(label, rows):
    print(f"\n--- {label} ---")
    for row in rows:
        print(row)
    print(f"({len(rows)} row(s))")


def main():
    client = build_clickhouse_client(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE
    )
    state = {}

    rows = list(sync_rows(client, state, table=TABLE, id_column="id", cursor_column=CURSOR_COLUMN))
    show("Sync #1 (backfill)", rows)
    print("State after sync #1:", state)

    rows = list(sync_rows(client, state, table=TABLE, id_column="id", cursor_column=CURSOR_COLUMN))
    show("Sync #2 (no changes)", rows)
    assert len(rows) == 0, "Expected 0 rows when nothing changed!"

    client.command(f"INSERT INTO {DATABASE}.{TABLE} VALUES (4, 'New ticket', 'open', now())")
    rows = list(sync_rows(client, state, table=TABLE, id_column="id", cursor_column=CURSOR_COLUMN))
    show("Sync #3 (after insert)", rows)
    assert any(r["id"] == "4" and not r["_deleted"] for r in rows), "New row not picked up!"

    client.command(f"ALTER TABLE {DATABASE}.{TABLE} DELETE WHERE id = 2")
    time.sleep(3)
    rows = list(sync_rows(client, state, table=TABLE, id_column="id", cursor_column=CURSOR_COLUMN))
    show("Sync #4 (after delete)", rows)
    assert any(r["id"] == "2" and r["_deleted"] for r in rows), "Deletion not detected!"

    comments = list(sync_table_comments(client, database=DATABASE, table=TABLE))
    show("Table comments", comments)

    print("\nAll live smoke-test checks passed.")


if __name__ == "__main__":
    main()
