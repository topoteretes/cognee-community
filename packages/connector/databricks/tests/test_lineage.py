"""Lineage: notebooks name the tables they touch, tables name the notebooks that use them."""

import pytest
from fake_databricks import HOME, FakeWorkspace, source

from cognee_community_connector_databricks import DatabricksConnectorError
from cognee_community_connector_databricks.databricks import _sync_workspace

ROOT = f"{HOME}/analytics"
SCHEMA = "workspace.sales"
CUSTOMERS, ORDERS, REVENUE = (f"{SCHEMA}.{name}" for name in ("customers", "orders", "revenue"))


@pytest.fixture
def workspace():
    fake = FakeWorkspace()
    fake.schema(SCHEMA, "Customers and their orders")
    for name in (CUSTOMERS, ORDERS, REVENUE):
        fake.table(name, [("id", "bigint", "")])
    fake.notebook(
        f"{ROOT}/build_revenue",
        source(f"orders = spark.table('{ORDERS}')\ncustomers = spark.table('{CUSTOMERS}')"),
    )
    fake.notebook(f"{ROOT}/report", source(f"SELECT * FROM {ORDERS}", comment="--"), "SQL")
    fake.notebook(
        f"{ROOT}/never_ran",
        source(
            "df = spark.table('`workspace`.`sales`.`customers`')\n"
            "other = spark.table('workspace.hr.staff')\n"
            "partial = spark.table('sales.orders')"
        ),
    )
    fake.ran(f"{ROOT}/build_revenue", reads=[ORDERS, CUSTOMERS], writes=[REVENUE])
    fake.ran(f"{ROOT}/report", reads=[ORDERS])
    return fake


def sync(workspace, state, **kwargs):
    stats, failed = {}, set()
    workspace.calls.clear()
    rows = list(_sync_workspace(workspace, [ROOT], [SCHEMA], state, stats, failed, **kwargs))
    return rows, stats, failed


def content(rows, title):
    return next(row["content"] for row in rows if row.get("title") == title)


def titles(rows):
    return sorted(row["title"] for row in rows if not row["_deleted"])


def test_a_notebook_names_the_tables_it_reads_and_writes(workspace):
    rows, _, _ = sync(workspace, {})

    assert (
        "Reads tables: workspace.sales.customers, workspace.sales.orders\n"
        "Writes tables: workspace.sales.revenue"
    ) in content(rows, "build_revenue")


def test_a_notebook_that_only_reads_names_what_it_reads(workspace):
    rows, _, _ = sync(workspace, {})

    text = content(rows, "report")
    assert "Reads tables: workspace.sales.orders" in text
    assert "Writes tables" not in text


def test_a_table_names_where_it_comes_from_and_who_uses_it(workspace):
    rows, _, _ = sync(workspace, {})

    assert content(rows, ORDERS).endswith(
        "Lineage:\n"
        "Feeds: workspace.sales.revenue\n"
        "Read by: notebook build_revenue (~/analytics), notebook report (~/analytics)"
    )
    assert content(rows, REVENUE).endswith(
        "Lineage:\n"
        "Built from: workspace.sales.customers, workspace.sales.orders\n"
        "Written by: notebook build_revenue (~/analytics)"
    )


def test_a_notebook_outside_the_synced_folders_is_counted_not_named(workspace):
    workspace.ran("424242", reads=[ORDERS])

    rows, _, _ = sync(workspace, {})

    assert content(rows, ORDERS).endswith(", 1 notebook outside the synced folders")


def test_a_deleted_notebook_leaves_the_tables_it_read(workspace):
    state = {}
    sync(workspace, state)
    # Like a delete in the UI, which moves the notebook to the Trash folder.
    workspace.move(f"{ROOT}/report", f"{HOME}/Trash/report")

    rows, _, _ = sync(workspace, state)

    # Lineage still lists the deleted notebook, but the document no longer does.
    assert content(rows, ORDERS).endswith("Read by: notebook build_revenue (~/analytics)")

    workspace.move(f"{HOME}/Trash/report", f"{ROOT}/report")
    rows, _, _ = sync(workspace, state)
    assert "notebook report (~/analytics)" in content(rows, ORDERS)


def test_notebooks_of_another_workspace_are_left_out(workspace):
    workspace.ran("424242", reads=[ORDERS], workspace_id="9999")

    rows, _, _ = sync(workspace, {})

    assert "outside the synced folders" not in content(rows, ORDERS)


def test_a_notebook_without_lineage_lists_the_synced_tables_its_code_mentions(workspace):
    rows, _, _ = sync(workspace, {})

    text = content(rows, "never_ran")
    # Backticks are ignored; tables that are not synced, and partial names, are not listed.
    assert "Mentions tables: workspace.sales.customers\n" in text
    assert "Reads tables" not in text


def test_unchanged_lineage_sends_nothing(workspace):
    state = {}
    sync(workspace, state)

    rows, _, _ = sync(workspace, state)

    assert rows == []
    assert workspace.exports() == []


def test_a_first_run_resends_the_notebook_and_its_tables(workspace):
    state = {}
    sync(workspace, state)
    # The notebook's code did not change, so its modified_at did not move.
    workspace.ran(f"{ROOT}/never_ran", reads=[CUSTOMERS], writes=[REVENUE])

    rows, _, _ = sync(workspace, state)

    assert titles(rows) == ["never_ran", CUSTOMERS, REVENUE]
    assert workspace.exports() == [f"{ROOT}/never_ran"]
    assert "Reads tables: workspace.sales.customers" in content(rows, "never_ran")


def test_a_renamed_notebook_resends_the_tables_that_name_it(workspace):
    state = {}
    sync(workspace, state)
    workspace.move(f"{ROOT}/report", f"{ROOT}/daily_report")

    rows, _, _ = sync(workspace, state)

    assert titles(rows) == [ORDERS]
    assert "notebook daily_report (~/analytics)" in content(rows, ORDERS)


def test_a_lineage_call_that_fails_rewrites_and_forgets_nothing(workspace, monkeypatch):
    state = {}
    sync(workspace, state)
    workspace.ran(f"{ROOT}/never_ran", reads=[CUSTOMERS], writes=[REVENUE])
    original = workspace.get

    def failing_lineage(path, params=None):
        if path.endswith("table-lineage") and params["table_name"] == ORDERS:
            raise DatabricksConnectorError("Databricks returned HTTP 500.")
        return original(path, params)

    monkeypatch.setattr(workspace, "get", failing_lineage)

    rows, _, failed = sync(workspace, state)

    # With part of the lineage missing, no document is rewritten from it, and
    # the scope keeps its state for the next run.
    assert rows == []
    assert failed == {SCHEMA}

    monkeypatch.setattr(workspace, "get", original)
    rows, _, _ = sync(workspace, state)
    assert titles(rows) == ["never_ran", CUSTOMERS, REVENUE]


def test_lineage_can_be_turned_off(workspace):
    rows, _, _ = sync(workspace, {}, lineage=False)

    assert not [p for p, _ in workspace.calls if p.endswith("table-lineage")]
    assert "Mentions tables: workspace.sales.customers, workspace.sales.orders" in content(
        rows, "build_revenue"
    )
    assert "Lineage:" not in content(rows, ORDERS)
