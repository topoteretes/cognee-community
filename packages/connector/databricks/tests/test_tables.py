"""Unity Catalog tables: what a sync renders, re-reads, skips and forgets."""

import pytest
from fake_databricks import FakeWorkspace

from cognee_community_connector_databricks import (
    DatabricksAuthError,
    DatabricksConnectorError,
    DatabricksPermissionError,
)
from cognee_community_connector_databricks.databricks import _sync_tables

SCHEMA = "workspace.sales"
CUSTOMERS, ORDERS, OPEN_ORDERS = (
    f"{SCHEMA}.{name}" for name in ("customers", "orders", "open_orders")
)


@pytest.fixture
def workspace():
    fake = FakeWorkspace()
    fake.schema(SCHEMA, "Customers and their orders")
    fake.table(
        CUSTOMERS,
        [("customer_id", "bigint", "Customer number"), ("name", "string", "Company name")],
        comment="One row per paying customer.",
        primary_key=["customer_id"],
    )
    fake.table(
        ORDERS,
        [
            ("order_id", "bigint", "Order number"),
            ("customer_id", "bigint", ""),
            ("amount_usd", "decimal(12,2)", "Order value in US dollars"),
        ],
        comment="Every order placed by a customer.",
        primary_key=["order_id"],
        foreign_keys=[(["customer_id"], CUSTOMERS, ["customer_id"])],
    )
    fake.table(
        OPEN_ORDERS,
        [("order_id", "bigint", ""), ("amount_usd", "decimal(12,2)", "")],
        comment="Orders not shipped yet",
        view=f"SELECT order_id, amount_usd FROM {ORDERS} WHERE status = 'open'",
    )
    return fake


def sync(workspace, state, scopes=(SCHEMA,), **kwargs):
    stats, failed = {}, set()
    workspace.calls.clear()
    rows = list(_sync_tables(workspace, list(scopes), state, stats, failed, **kwargs))
    return rows, stats, failed


def titles(rows):
    return sorted(row["title"] for row in rows if not row["_deleted"])


def forgotten(rows):
    return sorted(row["id"] for row in rows if row["_deleted"])


def doc_id(workspace, name):
    return f"databricks:table:{workspace.tables[name]['table_id']}"


def content(rows, title):
    return next(row["content"] for row in rows if row.get("title") == title)


# -- rendering ------------------------------------------------------------------


def test_first_sync_renders_every_table_and_view(workspace):
    rows, stats, failed = sync(workspace, {})

    assert titles(rows) == [CUSTOMERS, OPEN_ORDERS, ORDERS]
    assert stats == {"tables": 3, "tables_changed": 3}
    assert failed == set()
    assert {row["id"] for row in rows} == {
        doc_id(workspace, n) for n in (CUSTOMERS, ORDERS, OPEN_ORDERS)
    }


def test_a_table_document_reads_like_the_table(workspace):
    rows, _, _ = sync(workspace, {})

    assert content(rows, ORDERS) == (
        "Databricks table workspace.sales.orders\n\n"
        "Every order placed by a customer.\n\n"
        "Schema workspace.sales: Customers and their orders\n\n"
        "Owner: someone@example.com\n\n"
        "Columns:\n"
        "order_id (bigint, required): Order number\n"
        "customer_id (bigint)\n"
        "amount_usd (decimal(12,2)): Order value in US dollars\n\n"
        "Primary key: order_id\n\n"
        "References:\n"
        "customer_id references workspace.sales.customers (customer_id)"
    )


def test_a_view_document_carries_its_definition(workspace):
    rows, _, _ = sync(workspace, {})

    text = content(rows, OPEN_ORDERS)
    assert text.startswith("Databricks view workspace.sales.open_orders")
    assert text.endswith(
        "View definition:\n```sql\n"
        "SELECT order_id, amount_usd FROM workspace.sales.orders WHERE status = 'open'\n```"
    )


def test_statistics_from_the_data_never_reach_a_document(workspace):
    rows, _, _ = sync(workspace, {})

    # Every listed table carries a column maximum among its properties.
    assert all("12000.00" not in row["content"] for row in rows)


def test_a_secret_in_a_view_definition_is_scrubbed(workspace):
    workspace.alter(OPEN_ORDERS, view_definition="SELECT * FROM t WHERE api_key = 'abcd1234efgh'")

    rows, _, _ = sync(workspace, {})

    assert "abcd1234efgh" not in content(rows, OPEN_ORDERS)


# -- change detection -------------------------------------------------------------


def test_unchanged_tables_send_nothing_and_read_no_detail(workspace):
    state = {}
    sync(workspace, state)

    rows, stats, _ = sync(workspace, state)

    assert rows == []
    assert workspace.details_read() == []
    assert stats == {"tables": 3}


def test_a_comment_change_rereads_only_that_table(workspace):
    state = {}
    sync(workspace, state)
    workspace.alter(CUSTOMERS, comment="One row per paying customer, with their account manager.")

    rows, stats, _ = sync(workspace, state)

    assert titles(rows) == [CUSTOMERS]
    assert workspace.details_read() == [CUSTOMERS]
    assert "with their account manager" in content(rows, CUSTOMERS)
    assert stats["tables_changed"] == 1


def test_a_new_key_is_picked_up_from_the_detail(workspace):
    state = {}
    sync(workspace, state)
    workspace.alter(
        OPEN_ORDERS,
        table_constraints=[{"primary_key_constraint": {"child_columns": ["order_id"]}}],
    )

    rows, _, _ = sync(workspace, state)

    assert "Primary key: order_id" in content(rows, OPEN_ORDERS)


def test_full_refresh_sends_every_table_again(workspace):
    state = {}
    sync(workspace, state)

    rows, _, _ = sync(workspace, state, full_refresh=True)

    assert titles(rows) == [CUSTOMERS, OPEN_ORDERS, ORDERS]


def test_a_renamed_table_keeps_its_document(workspace):
    state = {}
    sync(workspace, state)
    old_id = doc_id(workspace, ORDERS)
    workspace.rename_table(ORDERS, f"{SCHEMA}.purchases")

    rows, _, _ = sync(workspace, state)

    assert forgotten(rows) == []
    assert [row["id"] for row in rows] == [old_id]
    assert content(rows, f"{SCHEMA}.purchases").startswith(
        "Databricks table workspace.sales.purchases"
    )


# -- forgetting -------------------------------------------------------------------


def test_a_dropped_table_is_forgotten_once(workspace):
    state = {}
    sync(workspace, state)
    gone = doc_id(workspace, ORDERS)
    workspace.drop(ORDERS)

    rows, stats, _ = sync(workspace, state)
    again, _, _ = sync(workspace, state)

    assert forgotten(rows) == [gone]
    assert stats["tables_deleted"] == 1
    assert forgotten(again) == []


def test_a_table_recreated_under_the_same_name_forgets_the_old_one(workspace):
    state = {}
    sync(workspace, state)
    old = doc_id(workspace, ORDERS)
    workspace.drop(ORDERS)
    workspace.table(ORDERS, [("order_id", "bigint", "")])

    rows, _, _ = sync(workspace, state)

    assert forgotten(rows) == [old]
    assert titles(rows) == [ORDERS]


def test_a_dropped_schema_forgets_its_tables(workspace):
    state = {}
    sync(workspace, state)
    ids = sorted(doc_id(workspace, n) for n in (CUSTOMERS, ORDERS, OPEN_ORDERS))
    workspace.drop_schema(SCHEMA)

    rows, _, failed = sync(workspace, state)

    assert forgotten(rows) == ids
    assert failed == set()


def test_a_scope_removed_from_the_selection_forgets_its_tables(workspace):
    workspace.schema("workspace.hr", "People")
    workspace.table("workspace.hr.staff", [("id", "bigint", "")])
    state = {}
    sync(workspace, state, scopes=(SCHEMA, "workspace.hr"))
    staff = doc_id(workspace, "workspace.hr.staff")

    rows, _, _ = sync(workspace, state, scopes=(SCHEMA,))

    assert forgotten(rows) == [staff]


def test_a_table_the_listing_misses_but_that_is_still_there_is_kept(workspace):
    state = {}
    sync(workspace, state)
    original = workspace.pages

    def listing_without_orders(path, params, items_key):
        return (t for t in original(path, params, items_key) if t.get("full_name") != ORDERS)

    workspace.pages = listing_without_orders

    rows, _, _ = sync(workspace, state)

    assert forgotten(rows) == []


# -- selection ----------------------------------------------------------------------


def test_a_catalog_selects_every_schema_but_information_schema(workspace):
    workspace.schema("workspace.hr", "People")
    workspace.table("workspace.hr.staff", [("id", "bigint", "")])

    rows, _, _ = sync(workspace, {}, scopes=("workspace",))

    assert titles(rows) == ["workspace.hr.staff", CUSTOMERS, OPEN_ORDERS, ORDERS]


def test_one_table_can_be_selected_on_its_own(workspace):
    state = {}
    rows, _, _ = sync(workspace, state, scopes=(ORDERS,))
    assert titles(rows) == [ORDERS]

    workspace.drop(ORDERS)
    rows, _, _ = sync(workspace, state, scopes=(ORDERS,))
    assert len(forgotten(rows)) == 1


def test_a_table_under_two_selected_scopes_is_synced_once(workspace):
    rows, stats, _ = sync(workspace, {}, scopes=(SCHEMA, ORDERS))

    assert titles(rows) == [CUSTOMERS, OPEN_ORDERS, ORDERS]
    assert stats["tables"] == 3


# -- failures never delete ------------------------------------------------------------


def test_a_scope_that_fails_to_list_keeps_its_state_and_forgets_nothing(workspace):
    workspace.schema("workspace.hr", "People")
    workspace.table("workspace.hr.staff", [("id", "bigint", "")])
    state = {}
    sync(workspace, state, scopes=(SCHEMA, "workspace.hr"))
    workspace.drop("workspace.hr.staff")
    workspace.alter(ORDERS, comment="Changed.")
    workspace.failing["hr"] = DatabricksConnectorError("Databricks returned HTTP 500.")

    rows, _, failed = sync(workspace, state, scopes=(SCHEMA, "workspace.hr"))

    assert failed == {"workspace.hr"}
    assert forgotten(rows) == []
    assert titles(rows) == [ORDERS]


def test_losing_permission_on_a_missing_table_forgets_nothing(workspace):
    state = {}
    sync(workspace, state)
    workspace.drop(ORDERS)
    workspace.failing[f"/tables/{ORDERS}"] = DatabricksPermissionError(
        "Databricks does not let this user read it (HTTP 403, PERMISSION_DENIED)."
    )

    rows, _, failed = sync(workspace, state)

    assert forgotten(rows) == []
    assert failed == {SCHEMA}


def test_a_refused_token_stops_the_whole_sync(workspace):
    workspace.failing["/schemas/workspace.sales"] = DatabricksAuthError(
        "Databricks refused the token."
    )

    with pytest.raises(DatabricksAuthError):
        sync(workspace, {})


def test_logs_carry_counts_only(workspace, monkeypatch):
    import cognee_community_connector_databricks.databricks as module

    messages = []
    monkeypatch.setattr(
        module.logger, "warning", lambda message, *args: messages.append(message % args)
    )
    workspace.failing["sales"] = DatabricksConnectorError("Databricks returned HTTP 500.")

    sync(workspace, {})

    assert messages
    assert all("sales" not in message and "workspace" not in message for message in messages)
