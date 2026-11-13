"""The direct connection against a real Postgres server (Redshift speaks its protocol).

SQLite is lenient about types; Postgres is not. This runs the connector's SQL
with real ``timestamp``, ``numeric`` and ``boolean`` columns and real
server-side parameter binding, which is what a Redshift cluster does too.
``svv_all_tables`` / ``svv_all_columns`` are recreated as views over the
Postgres catalog, with ``COMMENT ON`` as their remarks.

Skipped unless ``REDSHIFT_TEST_POSTGRES_DSN`` is set, for example::

    docker run -d --rm -e POSTGRES_PASSWORD=test -e POSTGRES_DB=dev \\
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    export REDSHIFT_TEST_POSTGRES_DSN=postgresql://postgres:test@127.0.0.1:55432/dev
"""

import os

import pytest

from cognee_community_connector_redshift import DirectConnection, RedshiftQuery, RedshiftTable
from cognee_community_connector_redshift.redshift import _normalize_selections, sync_rows

DSN = os.getenv("REDSHIFT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="REDSHIFT_TEST_POSTGRES_DSN is not set")

SCHEMA = "cognee_redshift_test"

CATALOG = """
CREATE OR REPLACE VIEW svv_all_tables AS
SELECT current_database()::text AS database_name, n.nspname::text AS schema_name,
       c.relname::text AS table_name,
       CASE c.relkind WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END AS table_type,
       obj_description(c.oid, 'pg_class') AS remarks
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v');

CREATE OR REPLACE VIEW svv_all_columns AS
SELECT current_database()::text AS database_name, n.nspname::text AS schema_name,
       c.relname::text AS table_name, a.attname::text AS column_name,
       a.attnum::int AS ordinal_position,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       col_description(c.oid, a.attnum) AS remarks
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'v');
"""


def _admin(sql, params=None):
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as db:
        db.execute(sql, params)


@pytest.fixture
def orders():
    _admin(CATALOG)
    _admin(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    _admin(f"CREATE SCHEMA {SCHEMA}")
    _admin(
        f"""
        CREATE TABLE {SCHEMA}.orders (
            id BIGINT, region TEXT, customer TEXT, amount NUMERIC(10, 2),
            paid BOOLEAN, note TEXT, updated_at TIMESTAMP
        );
        COMMENT ON TABLE {SCHEMA}.orders IS 'Web shop orders.';
        COMMENT ON COLUMN {SCHEMA}.orders.amount IS 'Order total in EUR.';
        INSERT INTO {SCHEMA}.orders VALUES
            (1, 'eu', 'Acme', 120.50, true, '50% upfront', '2026-01-01 10:00:00.250'),
            (2, 'us', 'Globex', 80.00, false, NULL, '2026-01-02 10:00:00'),
            (3, 'eu', 'Initech', 15.00, true, 'test order', '2026-01-02 10:00:00');
        """
    )
    yield RedshiftTable(
        f"{SCHEMA}.orders",
        primary_key=["region", "id"],
        cursor_column="updated_at",
        # A literal % in the filter must survive the driver's placeholder syntax.
        where="note IS NULL OR note NOT LIKE '%test%'",
    )
    _admin(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _sync(selections, state):
    import psycopg

    connection = DirectConnection(lambda: psycopg.connect(DSN))
    stats: dict = {}
    try:
        emitted = list(
            sync_rows(connection, _normalize_selections(selections, "redshift"), state, stats=stats)
        )
    finally:
        connection.close()
    rows = {row["id"]: row for _table, row in emitted if not row["_deleted"]}
    deleted = {row["id"] for _table, row in emitted if row["_deleted"]}
    return rows, deleted, stats


def test_typed_columns_cursor_and_deletes_on_postgres(orders):
    state: dict = {}
    rows, _, _ = _sync([orders], state)

    first = f'redshift:{SCHEMA}.orders:["eu", "1"]'
    assert set(rows) == {
        first,
        f'redshift:{SCHEMA}.orders:["us", "2"]',
        f"redshift:table:{SCHEMA}.orders",
    }
    content = rows[first]["content"]
    assert "amount: 120.50" in content
    assert "paid: true" in content
    assert "updated_at: 2026-01-01 10:00:00.250000" in content
    card = rows[f"redshift:table:{SCHEMA}.orders"]["content"]
    assert "Description: Web shop orders." in card
    assert "- amount (numeric(10,2)): Order total in EUR." in card
    # The server's own text form of the timestamp, compared back as a timestamp.
    assert state["selections"][f"{SCHEMA}.orders"]["cursor"] == "2026-01-02 10:00:00"

    _admin(f"UPDATE {SCHEMA}.orders SET paid = true, updated_at = '2026-01-05' WHERE id = 2")
    _admin(f"INSERT INTO {SCHEMA}.orders VALUES (4, 'eu', 'Late', 1, true, NULL, '2025-01-01')")
    _admin(f"DELETE FROM {SCHEMA}.orders WHERE id = 1")
    rows, deleted, stats = _sync([orders], state)

    assert "paid: true" in rows[f'redshift:{SCHEMA}.orders:["us", "2"]']["content"]
    assert f'redshift:{SCHEMA}.orders:["eu", "4"]' in rows
    assert stats["late"] == 1
    assert deleted == {first}
    assert state["selections"][f"{SCHEMA}.orders"]["cursor"] == "2026-01-05 00:00:00"


def test_query_with_casts_on_postgres(orders):
    query = RedshiftQuery(
        name="revenue_by_region",
        sql=(
            f"SELECT region, SUM(amount)::numeric(12, 2) AS revenue, MAX(updated_at) AS updated_at "
            f"FROM {SCHEMA}.orders WHERE customer <> 'a:b' GROUP BY region"
        ),
        primary_key="region",
        cursor_column="updated_at",
    )
    state: dict = {}
    rows, _, _ = _sync([query], state)

    assert "revenue: 135.50" in rows["redshift:revenue_by_region:eu"]["content"]
    rows, _, _ = _sync([query], state)
    assert set(rows) == {"redshift:revenue_by_region:us", "redshift:revenue_by_region:eu"}
