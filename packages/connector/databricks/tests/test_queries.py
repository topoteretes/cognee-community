"""Named queries: what runs, how a result reads, and what is kept or forgotten."""

import sqlite3

import dlt
import pytest
from fake_databricks import FakeWorkspace

from cognee_community_connector_databricks import DatabricksConnectorError, databricks_source
from cognee_community_connector_databricks.databricks import (
    _named_queries,
    _NamedQueries,
    _sync_queries,
)

OPEN_ORDERS = "SELECT order_id, name, amount_usd FROM workspace.sales.orders ORDER BY order_id"
COLUMNS = ["order_id", "name", "amount_usd"]
ROWS = [["101", "Contoso", "12000.00"], ["104", "Contoso", "1500.00"]]


@pytest.fixture
def workspace():
    fake = FakeWorkspace()
    fake.results[OPEN_ORDERS] = (COLUMNS, ROWS)
    return fake


def named(**queries):
    return _NamedQueries(queries or {"open_orders": OPEN_ORDERS}, "wh-1", 100, 30)


def sync(workspace, state, queries=None, **kwargs):
    stats, failed = {}, set()
    workspace.calls.clear()
    rows = list(_sync_queries(workspace, queries or named(), state, stats, failed, **kwargs))
    return rows, stats, failed


def test_a_query_result_reads_as_its_sql_and_a_table(workspace):
    rows, stats, _ = sync(workspace, {})

    assert rows == [
        {
            "id": "databricks:query:open_orders",
            "title": "Query open_orders",
            "content": (
                "Databricks query open_orders\n\n"
                f"SQL:\n```sql\n{OPEN_ORDERS}\n```\n\n"
                "Result: 2 rows\n"
                "| order_id | name | amount_usd |\n"
                "| --- | --- | --- |\n"
                "| 101 | Contoso | 12000.00 |\n"
                "| 104 | Contoso | 1500.00 |"
            ),
            "_deleted": False,
        }
    ]
    assert stats == {"queries": 1, "queries_changed": 1}


def test_a_query_runs_capped_on_the_named_warehouse_and_is_cancelled_at_the_timeout(workspace):
    sync(workspace, {}, _NamedQueries({"open_orders": OPEN_ORDERS}, "wh-1", 1, 20))

    ((_, body),) = workspace.calls
    assert body["warehouse_id"] == "wh-1"
    assert body["row_limit"] == 1
    assert body["byte_limit"] == 1_000_000
    assert (body["wait_timeout"], body["on_wait_timeout"]) == ("20s", "CANCEL")


def test_a_cut_result_says_so(workspace):
    rows, _, _ = sync(workspace, {}, _NamedQueries({"open_orders": OPEN_ORDERS}, "wh-1", 1, 30))

    assert "Result: the first 1 rows, more not shown" in rows[0]["content"]
    assert "104" not in rows[0]["content"]


def test_a_result_in_several_chunks_is_read_to_the_end(workspace):
    workspace.chunk_rows = 1

    rows, _, _ = sync(workspace, {})

    assert "| 104 | Contoso | 1500.00 |" in rows[0]["content"]
    assert [p for p, _ in workspace.calls if "/chunks/" in p]


def test_an_unchanged_result_is_not_sent_again(workspace):
    state = {}
    sync(workspace, state)

    rows, stats, _ = sync(workspace, state)

    assert rows == []
    assert stats == {"queries": 1}


def test_a_changed_result_is_sent_again(workspace):
    state = {}
    sync(workspace, state)
    workspace.results[OPEN_ORDERS] = (COLUMNS, ROWS[:1])

    rows, _, _ = sync(workspace, state)

    assert "Result: 1 row\n" in rows[0]["content"]


def test_full_refresh_sends_every_result_again(workspace):
    state = {}
    sync(workspace, state)

    rows, _, _ = sync(workspace, state, full_refresh=True)

    assert len(rows) == 1


def test_a_query_no_longer_named_is_forgotten(workspace):
    state = {}
    sync(workspace, state)

    rows, stats, _ = sync(workspace, state, _NamedQueries({}, "wh-1", 100, 30))

    assert rows == [{"id": "databricks:query:open_orders", "_deleted": True}]
    assert stats == {"queries_deleted": 1}


@pytest.mark.parametrize("outcome", ["FAILED", "CANCELED"])
def test_a_query_that_fails_or_times_out_keeps_its_last_result(workspace, outcome):
    state = {}
    sync(workspace, state)
    workspace.results[OPEN_ORDERS] = outcome

    rows, _, failed = sync(workspace, state)

    assert rows == []
    assert failed == {"query:open_orders"}
    assert "open_orders" in state["queries"]


def test_a_secret_or_a_pipe_in_a_result_is_made_safe(workspace):
    workspace.results[OPEN_ORDERS] = (["note"], [["password = 'hunter2hunter2'"], ["a | b"]])

    rows, _, _ = sync(workspace, {})

    assert "hunter2" not in rows[0]["content"]
    assert "| a \\| b |" in rows[0]["content"]


def test_queries_alone_are_a_selection_and_run_through_the_source(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    database = tmp_path / "staging.db"
    pipeline = dlt.pipeline(
        "databricks_queries",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database}"),
        dataset_name="main",
    )
    resource = databricks_source(
        queries={"open_orders": OPEN_ORDERS}, warehouse_id="wh-1", client=workspace
    )

    pipeline.run(resource)

    with sqlite3.connect(database) as conn:
        staged = [row[0] for row in conn.execute("SELECT id FROM databricks_workspace")]
    assert staged == ["databricks:query:open_orders"]
    assert resource.cognee_sync_stats == {"queries": 1, "queries_changed": 1}


def test_the_warehouse_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-env")

    assert _named_queries({"q": "SELECT 1"}, None, 100, 30).warehouse_id == "wh-env"


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE workspace.sales.orders",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "  -- a comment\n  DELETE FROM t",
    ],
)
def test_only_read_only_queries_are_accepted(sql):
    with pytest.raises(ValueError, match="read-only"):
        _named_queries({"q": sql}, "wh-1", 100, 30)


@pytest.mark.parametrize(
    "sql",
    ["select 1", "  WITH x AS (SELECT 1) SELECT * FROM x", "-- note\nSHOW TABLES", "DESCRIBE t"],
)
def test_read_only_queries_are_accepted(sql):
    assert _named_queries({"q": sql}, "wh-1", 100, 30).queries == {"q": sql}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"queries": {"bad name": "SELECT 1"}}, "Query names"),
        ({"queries": {"q": "SELECT 1"}, "warehouse_id": None}, "SQL warehouse"),
        ({"max_rows": 0}, "at least 1"),
        ({"timeout": 60}, "5 to 50"),
    ],
)
def test_bad_query_settings_are_refused_when_the_source_is_built(monkeypatch, kwargs, message):
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    settings = {"queries": {}, "warehouse_id": "wh-1", "max_rows": 100, "timeout": 30, **kwargs}

    with pytest.raises(ValueError, match=message):
        _named_queries(
            settings["queries"], settings["warehouse_id"], settings["max_rows"], settings["timeout"]
        )


def test_a_query_error_from_the_api_fails_only_that_query(workspace, monkeypatch):
    def broken(path, body):
        raise DatabricksConnectorError("Databricks returned HTTP 400 (INVALID_PARAMETER_VALUE).")

    monkeypatch.setattr(workspace, "post", broken)

    rows, _, failed = sync(workspace, {})

    assert rows == []
    assert failed == {"query:open_orders"}
