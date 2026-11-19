"""Table description documents, built from metadata read without a warehouse."""

import copy

import pytest
from fake_snowflake import FakeConnection, metadata_session, sales_schema
from snowflake.connector.errors import ProgrammingError

from cognee_community_connector_snowflake.snowflake import (
    SnowflakeConnectorError,
    _column_type,
    _describe_tables,
    _parse_table,
    _TableRef,
)

ORDERS = _TableRef("SALES_DB", "SALES", "ORDERS")
CUSTOMERS = _TableRef("SALES_DB", "SALES", "CUSTOMERS")


def describe(*refs, schema=None):
    conn = metadata_session({("SALES_DB", "SALES"): schema or sales_schema()})
    rows, missing = _describe_tables(conn, list(refs))
    return {row["title"]: row for row in rows}, missing, conn


def test_a_table_becomes_a_document_about_what_it_holds():
    rows, missing, _ = describe(ORDERS)

    assert missing == []
    assert rows["SALES_DB.SALES.ORDERS"] == {
        "id": "snowflake:table:SALES_DB.SALES.ORDERS",
        "title": "SALES_DB.SALES.ORDERS",
        "content": "\n".join(
            [
                "Snowflake table SALES_DB.SALES.ORDERS",
                "Every order placed by a customer.",
                "Schema SALES_DB.SALES: Customers and their orders",
                "",
                "Primary key: ORDER_ID",
                "",
                "Columns:",
                "ORDER_ID (NUMBER(38,0), required): Order number",
                "CUSTOMER_ID (NUMBER(38,0), required)",
                "AMOUNT_USD (NUMBER(12,2)): Order value in US dollars",
                "STATUS (VARCHAR(20)): open, shipped or refunded",
                "UPDATED_AT (TIMESTAMP_NTZ, required): Last change",
                "",
                "References:",
                "CUSTOMER_ID references SALES_DB.SALES.CUSTOMERS (CUSTOMER_ID)",
            ]
        ),
        "_deleted": False,
    }


def test_a_schema_is_described_with_one_round_of_show_calls():
    _, _, conn = describe(ORDERS, CUSTOMERS)

    assert len(conn.sql) == 5
    assert all(sql.startswith("SHOW ") for sql in conn.sql)


def test_volatile_metadata_never_changes_the_text():
    first, _, _ = describe(ORDERS)
    changed = sales_schema()
    for table in changed["tables"]:
        table.update(rows=99999, bytes=123456789, created_on="2030-01-01", change_tracking="OFF")

    second, _, _ = describe(ORDERS, schema=changed)

    assert first == second


def test_a_table_missing_from_its_listed_schema_is_reported():
    rows, missing, _ = describe(ORDERS, _TableRef("SALES_DB", "SALES", "DROPPED"))

    assert list(rows) == ["SALES_DB.SALES.ORDERS"]
    assert missing == [_TableRef("SALES_DB", "SALES", "DROPPED")]


def test_a_schema_that_fails_to_list_raises_instead_of_reporting_tables_missing():
    error = ProgrammingError(
        msg="Object 'SALES_DB.SALES' does not exist or not authorized.",
        errno=2003,
        send_telemetry=False,
    )
    conn = FakeConnection(lambda sql: error)

    with pytest.raises(SnowflakeConnectorError, match="002003"):
        _describe_tables(conn, [ORDERS])


def test_a_bare_table_renders_without_empty_sections():
    schema = sales_schema()
    schema["tables"] = [{"name": "NOTES", "comment": ""}]
    schema["columns"] = [
        {"table_name": "NOTES", "column_name": "BODY", "data_type": '{"type":"TEXT"}'}
    ]
    schema["primary_keys"], schema["foreign_keys"] = [], []
    schema["schemas"] = [{"name": "SALES", "comment": ""}]

    rows, _, _ = describe(_TableRef("SALES_DB", "SALES", "NOTES"), schema=schema)

    assert rows["SALES_DB.SALES.NOTES"]["content"] == (
        "Snowflake table SALES_DB.SALES.NOTES\n\nColumns:\nBODY (VARCHAR)"
    )


def test_composite_keys_keep_their_declared_order():
    schema = copy.deepcopy(sales_schema())
    schema["primary_keys"] = [
        {"table_name": "ORDERS", "column_name": "LINE_NO", "key_sequence": 2},
        {"table_name": "ORDERS", "column_name": "ORDER_ID", "key_sequence": 1},
    ]
    second_part = dict(schema["foreign_keys"][0], fk_column_name="REGION", pk_column_name="REGION")
    second_part["key_sequence"] = 2
    schema["foreign_keys"].insert(0, second_part)

    rows, _, _ = describe(ORDERS, schema=schema)
    content = rows["SALES_DB.SALES.ORDERS"]["content"]

    assert "Primary key: ORDER_ID, LINE_NO" in content
    assert (
        "CUSTOMER_ID, REGION references SALES_DB.SALES.CUSTOMERS (CUSTOMER_ID, REGION)" in content
    )


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("sales_db.sales.orders", _TableRef("SALES_DB", "SALES", "ORDERS")),
        ("  Sales_Db . Sales . Orders ", _TableRef("SALES_DB", "SALES", "ORDERS")),
        ('SALES_DB.SALES."orders"', _TableRef("SALES_DB", "SALES", "orders")),
        ('"my.db".SALES."Order ""Lines"""', _TableRef("my.db", "SALES", 'Order "Lines"')),
    ],
)
def test_table_names_follow_snowflake_quoting_rules(typed, expected):
    assert _parse_table(typed) == expected


@pytest.mark.parametrize("typed", ["ORDERS", "SALES.ORDERS", "A.B.C.D", 'A.B."C', "A..C", ""])
def test_a_table_name_that_is_not_database_schema_table_is_refused(typed):
    with pytest.raises(ValueError, match=r"DATABASE\.SCHEMA\.TABLE"):
        _parse_table(typed)


def test_quoted_names_are_quoted_again_in_sql():
    assert _parse_table('"my.db".SALES."Order ""Lines"""').sql == (
        '"my.db"."SALES"."Order ""Lines"""'
    )


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        ('{"type":"FIXED","precision":38,"scale":0}', "NUMBER(38,0)"),
        ('{"type":"FIXED","precision":12,"scale":2}', "NUMBER(12,2)"),
        ('{"type":"TEXT","length":16777216}', "VARCHAR"),
        ('{"type":"TEXT","length":20}', "VARCHAR(20)"),
        ('{"type":"REAL"}', "FLOAT"),
        ('{"type":"TIMESTAMP_NTZ","precision":0,"scale":9}', "TIMESTAMP_NTZ"),
        ('{"type":"VARIANT"}', "VARIANT"),
        ("not json", "not json"),
        (None, "unknown"),
    ],
)
def test_column_types_read_like_sql(data_type, expected):
    assert _column_type(data_type) == expected
