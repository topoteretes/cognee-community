"""The row sync: which tables are read, what each row becomes, and what is forgotten."""

import datetime
import decimal

import pytest
from fake_snowflake import FakeAccount
from snowflake.connector.errors import DatabaseError, ProgrammingError

from cognee_community_connector_snowflake.snowflake import (
    SnowflakeAuthError,
    SnowflakeConnectorError,
    _format_value,
    _friendly_error,
    _iter_rows,
    _TableRef,
)

ORDERS = _TableRef("SALES_DB", "SALES", "ORDERS")
CUSTOMERS = _TableRef("SALES_DB", "SALES", "CUSTOMERS")
WHEN = datetime.datetime(2026, 10, 20, 9, 0)


@pytest.fixture
def account():
    fake = FakeAccount()
    fake.insert("CUSTOMERS", CUSTOMER_ID=1, NAME="Contoso")
    fake.insert("CUSTOMERS", CUSTOMER_ID=2, NAME="Fabrikam")
    fake.insert(
        "ORDERS",
        ORDER_ID=101,
        CUSTOMER_ID=1,
        AMOUNT_USD=decimal.Decimal("12000.00"),
        STATUS="shipped",
        UPDATED_AT=WHEN,
    )
    fake.insert(
        "ORDERS",
        ORDER_ID=102,
        CUSTOMER_ID=2,
        AMOUNT_USD=None,
        STATUS="open",
        UPDATED_AT=WHEN,
    )
    return fake


def sync(account, state, refs=(ORDERS, CUSTOMERS), **kwargs):
    stats = {}
    account.sql.clear()
    docs = list(_iter_rows(account.run, account.fetch, list(refs), state, stats, **kwargs))
    return docs, stats


def rows_of(docs):
    return {
        d["title"]: d for d in docs if d["id"].startswith("snowflake:row:") and not d["_deleted"]
    }


def forgotten(docs):
    return sorted(d["id"] for d in docs if d["_deleted"])


def reads(account):
    return [sql for sql in account.sql if sql.startswith("SELECT * FROM")]


def test_first_sync_describes_tables_and_turns_rows_into_documents(account):
    docs, stats = sync(account, {})

    assert sorted(rows_of(docs)) == ["CUSTOMERS 1", "CUSTOMERS 2", "ORDERS 101", "ORDERS 102"]
    assert {d["id"] for d in docs if d["id"].startswith("snowflake:table:")} == {
        "snowflake:table:SALES_DB.SALES.ORDERS",
        "snowflake:table:SALES_DB.SALES.CUSTOMERS",
    }
    assert stats == {"rows": 4}


def test_a_row_document_reads_like_the_row(account):
    docs, _ = sync(account, {})

    assert rows_of(docs)["ORDERS 101"] == {
        "id": "snowflake:row:SALES_DB.SALES.ORDERS:101",
        "title": "ORDERS 101",
        "content": "\n".join(
            [
                "Row of Snowflake table SALES_DB.SALES.ORDERS",
                "Table: Every order placed by a customer.",
                "ORDER_ID: 101",
                "CUSTOMER_ID: 1 (references SALES_DB.SALES.CUSTOMERS)",
                "AMOUNT_USD: 12000.00",
                "STATUS: shipped",
                "UPDATED_AT: 2026-10-20 09:00:00",
            ]
        ),
        "_deleted": False,
    }
    # An empty value is left out rather than written as "None".
    assert "AMOUNT_USD" not in rows_of(docs)["ORDERS 102"]["content"]


def test_an_unchanged_table_is_not_read_at_all(account):
    state = {}
    sync(account, state)

    docs, stats = sync(account, state)

    assert rows_of(docs) == {}
    assert reads(account) == []
    assert stats == {"unchanged": 2}


def test_only_the_changed_table_is_read_again(account):
    state = {}
    sync(account, state)
    account.update("ORDERS", (101,), STATUS="refunded")

    docs, stats = sync(account, state)

    assert reads(account) == ['SELECT * FROM "SALES_DB"."SALES"."ORDERS" ORDER BY "ORDER_ID"']
    assert "STATUS: refunded" in rows_of(docs)["ORDERS 101"]["content"]
    assert stats["unchanged"] == 1


def test_a_deleted_row_is_forgotten_once(account):
    state = {}
    sync(account, state)
    account.delete("ORDERS", (102,))

    docs, stats = sync(account, state)

    assert forgotten(docs) == ["snowflake:row:SALES_DB.SALES.ORDERS:102"]
    assert stats["deleted"] == 1
    assert state["tables"]["SALES_DB.SALES.ORDERS"]["keys"] == ["101"]
    assert forgotten(sync(account, state)[0]) == []


def test_a_new_row_is_added(account):
    state = {}
    sync(account, state)
    account.insert("ORDERS", ORDER_ID=103, CUSTOMER_ID=1, STATUS="open", UPDATED_AT=WHEN)

    docs, _ = sync(account, state)

    assert "ORDERS 103" in rows_of(docs)


def test_a_dropped_table_forgets_its_description_and_rows(account):
    state = {}
    sync(account, state)
    account.drop("ORDERS")

    docs, _ = sync(account, state)

    assert forgotten(docs) == [
        "snowflake:row:SALES_DB.SALES.ORDERS:101",
        "snowflake:row:SALES_DB.SALES.ORDERS:102",
        "snowflake:table:SALES_DB.SALES.ORDERS",
    ]
    assert "SALES_DB.SALES.ORDERS" not in state["tables"]


def test_a_table_removed_from_the_selection_is_forgotten(account):
    state = {}
    sync(account, state)

    docs, _ = sync(account, state, refs=(CUSTOMERS,))

    assert "snowflake:table:SALES_DB.SALES.ORDERS" in forgotten(docs)
    assert len(forgotten(docs)) == 3


def test_a_table_without_a_key_is_described_but_its_rows_are_not_read(account):
    account.meta["primary_keys"] = [
        k for k in account.meta["primary_keys"] if k["table_name"] != "ORDERS"
    ]

    docs, stats = sync(account, {}, refs=(ORDERS,))

    assert [d["id"] for d in docs] == ["snowflake:table:SALES_DB.SALES.ORDERS"]
    assert stats == {"no_key": 1}


def test_keys_can_be_given_for_a_table_without_a_declared_key(account):
    account.meta["primary_keys"] = []

    docs, _ = sync(account, {}, refs=(ORDERS,), keys={"SALES_DB.SALES.ORDERS": ["ORDER_ID"]})

    assert sorted(rows_of(docs)) == ["ORDERS 101", "ORDERS 102"]


def test_changing_the_key_reads_the_table_again(account):
    state = {}
    sync(account, state, refs=(ORDERS,))

    sync(account, state, refs=(ORDERS,), keys={"SALES_DB.SALES.ORDERS": ["ORDER_ID", "STATUS"]})

    assert len(reads(account)) == 1


def test_full_refresh_reads_unchanged_tables(account):
    state = {}
    sync(account, state)

    docs, _ = sync(account, state, full_refresh=True)

    assert len(rows_of(docs)) == 4


def query_error(errno, text):
    return _friendly_error(ProgrammingError(msg=text, errno=errno, send_telemetry=False))


def test_a_failing_table_does_not_stop_the_others_and_keeps_its_state(account):
    state = {}
    sync(account, state)
    saved = dict(state["tables"]["SALES_DB.SALES.ORDERS"])
    account.update("ORDERS", (101,), STATUS="refunded")
    account.update("CUSTOMERS", (1,), NAME="Contoso Ltd")
    account.failing["ORDERS"] = query_error(608, "Warehouse 'WH' is suspended.")

    docs, stats = sync(account, state)

    assert sorted(rows_of(docs)) == ["CUSTOMERS 1", "CUSTOMERS 2"]
    assert "snowflake:table:SALES_DB.SALES.ORDERS" not in {d["id"] for d in docs}
    assert state["tables"]["SALES_DB.SALES.ORDERS"] == saved
    assert stats == {"rows": 2, "failed": 1}


def test_a_table_failing_half_way_counts_none_of_its_rows(account):
    original = account.fetch

    def fails_after_one_row(sql):
        rows = original(sql)
        yield next(rows)
        if "ORDERS" in sql:
            raise query_error(608, "Warehouse 'WH' is suspended.")
        yield from rows

    account.fetch = fails_after_one_row

    docs, stats = sync(account, {})

    assert sorted(rows_of(docs)) == ["CUSTOMERS 1", "CUSTOMERS 2"]
    assert stats == {"rows": 2, "failed": 1}


def test_when_every_table_fails_the_run_raises_and_forgets_nothing(account):
    state = {}
    sync(account, state)
    account.update("ORDERS", (101,), STATUS="refunded")
    account.update("CUSTOMERS", (1,), NAME="Contoso Ltd")
    account.failing = dict.fromkeys(["ORDERS", "CUSTOMERS"], query_error(608, "suspended"))

    with pytest.raises(SnowflakeConnectorError, match="Nothing was forgotten"):
        sync(account, state)
    assert set(state["tables"]) == {"SALES_DB.SALES.ORDERS", "SALES_DB.SALES.CUSTOMERS"}


def test_a_login_problem_stops_the_whole_run(account):
    account.failing["ORDERS"] = _friendly_error(
        DatabaseError(msg="JWT token is invalid.", errno=390144, send_telemetry=False)
    )

    with pytest.raises(SnowflakeAuthError):
        sync(account, {})


def test_a_schema_that_fails_to_list_forgets_nothing(account):
    state = {}
    sync(account, state)
    original = account.run

    def failing_run(sql, params=None):
        if sql.startswith("SHOW TABLES"):
            raise query_error(2003, "Schema does not exist or not authorized.")
        return original(sql, params)

    account.run = failing_run

    with pytest.raises(SnowflakeConnectorError):
        sync(account, state)
    assert set(state["tables"]) == {"SALES_DB.SALES.ORDERS", "SALES_DB.SALES.CUSTOMERS"}


def test_composite_keys_escape_the_separator(account):
    account.meta["primary_keys"] = [
        {"table_name": "ORDERS", "column_name": "STATUS", "key_sequence": 1},
        {"table_name": "ORDERS", "column_name": "ORDER_ID", "key_sequence": 2},
    ]
    account.update("ORDERS", (101,), STATUS="open|urgent")

    docs, _ = sync(account, {}, refs=(ORDERS,))

    assert "snowflake:row:SALES_DB.SALES.ORDERS:open\\|urgent|101" in {d["id"] for d in docs}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "true"),
        (decimal.Decimal("12000.00"), "12000.00"),
        (datetime.datetime(2026, 10, 20, 9, 0), "2026-10-20 09:00:00"),
        (datetime.date(2026, 10, 20), "2026-10-20"),
        (b"\x00\x01\x02", "(3 bytes of binary data)"),
        ({"b": 1, "a": [2]}, '{"a": [2], "b": 1}'),
        ("  several   spaces\nand lines ", "several spaces and lines"),
        (42, "42"),
    ],
)
def test_values_render_the_same_way_every_time(value, expected):
    assert _format_value(value) == expected
