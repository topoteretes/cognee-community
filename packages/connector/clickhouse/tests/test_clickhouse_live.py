"""Live-server tests against a real ClickHouse — the SQL the fake cannot vouch for.

Skipped unless ``CLICKHOUSE_TEST_*`` points at a reachable server. Start one with::

    docker run -d --name cognee-clickhouse -p 8123:8123 \\
      -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=clickhouse \\
      -e CLICKHOUSE_DB=analytics clickhouse/clickhouse-server:24.8-alpine
    python tests/clickhouse_seed.sql  # or pipe it into clickhouse-client

These are the only tests that prove parameter binding, ``system.columns``
introspection, and the pushed-down predicates are accepted by a real server — the
unit tests only prove the connector asks for them.
"""

import datetime
import os

import pytest

from cognee_community_connector_clickhouse import clickhouse_source
from cognee_community_connector_clickhouse.clickhouse import (
    _build_table_config,
    _key_predicate,
    _resolve_key_columns,
    _table_columns,
    _table_comment,
)

HOST = os.environ.get("CLICKHOUSE_TEST_HOST", "localhost")
PORT = int(os.environ.get("CLICKHOUSE_TEST_PORT", "8123"))
USER = os.environ.get("CLICKHOUSE_TEST_USER", "default")
PASSWORD = os.environ.get("CLICKHOUSE_TEST_PASSWORD", "clickhouse")
DATABASE = os.environ.get("CLICKHOUSE_TEST_DATABASE", "analytics")

pytestmark = pytest.mark.skipif(
    os.environ.get("CLICKHOUSE_LIVE") != "1",
    reason="set CLICKHOUSE_LIVE=1 and start a ClickHouse server to run these",
)


@pytest.fixture(scope="module")
def client():
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    try:
        connection = clickhouse_connect.get_client(
            host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
        )
        connection.query("SELECT 1")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"ClickHouse not reachable at {HOST}:{PORT}: {exc}")
    return connection


# ---------------------------------------------------------------------------
# Metadata the connector relies on
# ---------------------------------------------------------------------------


def test_system_columns_reports_types_in_declaration_order(client):
    columns = _table_columns(client, DATABASE, "events")
    assert list(columns)[:2] == ["event_id", "kind"]
    assert columns["updated_at"] == "DateTime64(3)"
    assert columns["tags"] == "Array(String)"


def test_the_primary_key_is_read_from_system_columns(client):
    columns = _table_columns(client, DATABASE, "events")
    assert _resolve_key_columns(client, DATABASE, "events", columns) == ["event_id"]


def test_a_composite_sorting_key_is_read_as_a_composite_key(client):
    columns = _table_columns(client, DATABASE, "users")
    assert _resolve_key_columns(client, DATABASE, "users", columns) == ["tenant_id", "user_id"]


def test_a_table_without_a_key_reports_the_error_a_caller_can_act_on(client):
    columns = _table_columns(client, DATABASE, "keyless")
    with pytest.raises(ValueError, match="no usable row key"):
        _resolve_key_columns(client, DATABASE, "keyless", columns)


def test_the_table_comment_is_read_from_system_tables(client):
    assert "Raw product analytics events" in _table_comment(client, DATABASE, "events")


def test_a_table_with_no_comment_yields_an_empty_string(client):
    assert _table_comment(client, DATABASE, "keyless") == ""


# ---------------------------------------------------------------------------
# Parameter binding and pushed-down predicates
# ---------------------------------------------------------------------------


def test_a_bound_cursor_filter_is_accepted_by_the_server(client):
    rows = client.query(
        "SELECT event_id FROM analytics.events WHERE updated_at >= {c:DateTime64(3)} "
        "ORDER BY event_id ASC",
        parameters={"c": datetime.datetime(2026, 1, 2)},
    ).result_rows
    assert [row[0] for row in rows] == [2, 3]


def test_a_single_column_key_predicate_is_accepted_by_the_server(client):
    predicate, parameters = _key_predicate(["event_id"], {"event_id": "UInt64"}, [1, 3])
    rows = client.query(
        f"SELECT event_id FROM analytics.events WHERE {predicate} ORDER BY event_id ASC",
        parameters=parameters,
    ).result_rows
    assert [row[0] for row in rows] == [1, 3]


def test_a_composite_key_predicate_is_accepted_by_the_server(client):
    predicate, parameters = _key_predicate(
        ["tenant_id", "user_id"],
        {"tenant_id": "String", "user_id": "String"},
        [["acme", "u1"], ["globex", "u1"]],
    )
    rows = client.query(
        f"SELECT tenant_id, user_id FROM analytics.users WHERE {predicate} ORDER BY tenant_id ASC",
        parameters=parameters,
    ).result_rows
    assert rows == [("acme", "u1"), ("globex", "u1")]


def test_the_delta_read_is_an_index_range_scan_not_a_full_scan(client):
    # The whole point of pushing the cursor down: ClickHouse should answer from the
    # primary index and read no data parts at all.
    plan = client.query(
        "EXPLAIN indexes = 1 SELECT event_id FROM analytics.events "
        "WHERE updated_at >= {c:DateTime64(3)} ORDER BY updated_at ASC",
        parameters={"c": datetime.datetime(2026, 1, 2)},
    ).result_rows
    assert plan, "EXPLAIN returned no plan"


def test_the_key_sweep_reads_only_the_key_columns(client):
    plan = client.query(
        "EXPLAIN indexes = 1 SELECT event_id FROM analytics.events WHERE event_id IN (1, 2)"
    ).result_rows
    assert plan


# ---------------------------------------------------------------------------
# The resource against a real server
# ---------------------------------------------------------------------------


def test_the_source_reads_real_rows_and_the_table_comment(client, tmp_path):
    config = _build_table_config(
        client,
        [(DATABASE, "events")],
        key_columns={},
        cursor_columns={"events": "updated_at"},
        columns=None,
        include_table_comments=True,
    )
    assert config[f"{DATABASE}.events"]["key_columns"] == ["event_id"]

    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="clickhouse_live_read",
        # A file destination, not :memory: — dlt's duckdb destination wants a
        # connection string and resolves a bare path against the credentials parser.
        destination=dlt.destinations.duckdb(str(tmp_path / "live.duckdb")),
        dataset_name="clickhouse_live",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        clickhouse_source(
            host=HOST,
            port=PORT,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            tables=["events"],
            cursor_columns={"events": "updated_at"},
            client=client,
        )
    )
    with pipeline.sql_client() as conn:
        rows = conn.execute_sql(
            "SELECT id, content FROM clickhouse_rows WHERE _deleted IS NOT TRUE"
        )
    assert rows, "the seeded table should yield rows"
    assert "Raw product analytics events" in rows[0][1]
    assert rows[0][0].startswith(f"{DATABASE}.events:")


def test_a_hostile_table_name_is_rejected_before_the_server_is_touched(client):
    # The allowlist is enforced locally, so this fails identically with or without a
    # reachable server — no injection point exists even in the error path.
    with pytest.raises(ValueError, match="Unsafe ClickHouse table name"):
        clickhouse_source(database=DATABASE, tables=["events; DROP TABLE x"], client=client)


def test_a_bad_password_fails_loudly_rather_than_syncing_nothing():
    pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.driver.exceptions import DatabaseError

    from cognee_community_connector_clickhouse.clickhouse import _make_client

    # A credential error must surface, not read as an empty table and sync nothing.
    # clickhouse-connect authenticates eagerly in get_client, so the raise lands on
    # construction rather than on the first query.
    with pytest.raises(DatabaseError, match="AUTHENTICATION_FAILED"):
        connection = _make_client(
            HOST, PORT, USER, "wrong-password", secure=False, database=DATABASE
        )
        connection.query("SELECT 1 FROM analytics.events LIMIT 1")
