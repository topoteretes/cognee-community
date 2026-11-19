"""snowflake_source(): the factory users call, run through a real dlt pipeline."""

import datetime
import sqlite3

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_snowflake import FakeAccount, FakeConnection
from snowflake.connector.errors import ProgrammingError

from cognee_community_connector_snowflake import snowflake_source
from cognee_community_connector_snowflake.snowflake import _column, _query, _Session

ENV = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY",
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    "SNOWFLAKE_TABLES",
)
WHEN = datetime.datetime(2026, 10, 20, 9, 0)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))


@pytest.fixture
def account():
    fake = FakeAccount()
    fake.insert("CUSTOMERS", CUSTOMER_ID=1, NAME="Contoso")
    fake.insert("ORDERS", ORDER_ID=101, CUSTOMER_ID=1, STATUS="shipped", UPDATED_AT=WHEN)
    fake.insert("ORDERS", ORDER_ID=102, CUSTOMER_ID=1, STATUS="open", UPDATED_AT=WHEN)
    return fake


@pytest.fixture
def pipeline(tmp_path):
    database = tmp_path / "staging.db"
    destination = dlt.destinations.sqlalchemy(f"sqlite:///{database}")
    return dlt.pipeline("snowflake_test", destination=destination, dataset_name="main"), database


def run(pipeline, source):
    pipeline[0].run(source)
    return staged(pipeline, source.name)


def staged(pipeline, table="snowflake_tables"):
    with sqlite3.connect(pipeline[1]) as conn:
        return sorted(row[0] for row in conn.execute(f"SELECT id FROM {table}"))


def other_account():
    """A second account, with a table of its own."""
    other = FakeAccount(database="OTHER_DB", account="ORG.OTHER")
    other.insert("ORDERS", ORDER_ID=7, CUSTOMER_ID=1, STATUS="open", UPDATED_AT=WHEN)
    return other


TABLES = ["sales_db.sales.orders", "SALES_DB.SALES.CUSTOMERS"]


def test_resource_opts_into_document_mode_merge_and_pipeline_scope(account):
    resource = snowflake_source(TABLES, client=account)

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "snowflake"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "snowflake_tables"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


def test_a_pipeline_loads_table_and_row_documents(account, pipeline):
    ids = run(pipeline, snowflake_source(TABLES, client=account))

    assert ids == [
        "snowflake:row:SALES_DB.SALES.CUSTOMERS:1",
        "snowflake:row:SALES_DB.SALES.ORDERS:101",
        "snowflake:row:SALES_DB.SALES.ORDERS:102",
        "snowflake:table:SALES_DB.SALES.CUSTOMERS",
        "snowflake:table:SALES_DB.SALES.ORDERS",
    ]


def test_dlt_keeps_the_sync_state_between_runs(account, pipeline):
    run(pipeline, snowflake_source(TABLES, client=account))
    account.sql.clear()

    source = snowflake_source(TABLES, client=account)
    run(pipeline, source)

    assert source.cognee_sync_stats == {"unchanged": 2}
    assert not [sql for sql in account.sql if sql.startswith("SELECT * FROM")]


def test_a_deleted_row_leaves_the_staging_table(account, pipeline):
    run(pipeline, snowflake_source(TABLES, client=account))
    account.delete("ORDERS", (102,))

    ids = run(pipeline, snowflake_source(TABLES, client=account))

    assert "snowflake:row:SALES_DB.SALES.ORDERS:102" not in ids
    assert "snowflake:row:SALES_DB.SALES.ORDERS:101" in ids


def test_a_second_account_under_the_same_name_is_refused_and_forgets_nothing(account, pipeline):
    first = run(pipeline, snowflake_source(TABLES, client=account))
    other = other_account()

    with pytest.raises(Exception, match="another Snowflake account"):
        run(pipeline, snowflake_source(["OTHER_DB.SALES.ORDERS"], client=other))

    # Refused before any table was listed or read, and the first account keeps
    # every row: without the check, its tables would look deselected.
    assert len(other.sql) == 1
    assert staged(pipeline) == first


def test_two_accounts_with_their_own_names_stay_apart_in_one_dataset(account, pipeline):
    other = snowflake_source(
        ["OTHER_DB.SALES.ORDERS"], client=other_account(), resource_name="snowflake_b"
    )
    run(pipeline, snowflake_source(TABLES, client=account, resource_name="snowflake_a"))
    run(pipeline, other)
    account.delete("ORDERS", (102,))

    run(pipeline, snowflake_source(TABLES, client=account, resource_name="snowflake_a"))

    assert "snowflake:row:SALES_DB.SALES.ORDERS:102" not in staged(pipeline, "snowflake_a")
    assert staged(pipeline, "snowflake_b") == [
        "snowflake:row:OTHER_DB.SALES.ORDERS:7",
        "snowflake:table:OTHER_DB.SALES.ORDERS",
    ]


def test_filters_and_keys_accept_names_as_typed(account, pipeline):
    account.meta["primary_keys"] = []
    source = snowflake_source(
        ["sales_db.sales.orders"],
        keys={"sales_db.sales.orders": ["order_id"]},
        filters={"Sales_Db.Sales.Orders": "STATUS = 'open'"},
        client=account,
    )

    ids = run(pipeline, source)

    assert "snowflake:row:SALES_DB.SALES.ORDERS:102" in ids
    assert "snowflake:row:SALES_DB.SALES.ORDERS:101" not in ids


def test_check_active_runs_before_every_query(account, pipeline):
    calls = []

    def check():
        calls.append(len(account.sql))

    run(pipeline, snowflake_source(TABLES, client=account, check_active=check))

    # One check before each query, plus one before each row is emitted.
    queries_checked = set(calls)
    assert set(range(len(account.sql))) <= queries_checked


def test_a_revoked_host_stops_the_sync_unchanged(account, pipeline):
    class RevokedError(Exception):
        pass

    def check():
        if account.sql:
            raise RevokedError

    with pytest.raises(Exception) as excinfo:
        run(pipeline, snowflake_source(TABLES, client=account, check_active=check))

    # dlt wraps errors from a resource; the host's own error is the cause.
    chain, error = [], excinfo.value
    while error is not None:
        chain.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    assert "RevokedError" in chain


def test_tables_come_from_the_environment_and_are_deduplicated(monkeypatch, account):
    monkeypatch.setenv("SNOWFLAKE_TABLES", "sales_db.sales.orders, SALES_DB.SALES.ORDERS,")

    assert snowflake_source(client=account) is not None


@pytest.mark.parametrize("empty", [None, [], [""], "  "])
def test_an_empty_table_list_is_refused_instead_of_forgetting_everything(account, empty):
    with pytest.raises(ValueError, match="tables is empty"):
        snowflake_source(empty, client=account)


def test_a_table_name_that_is_not_qualified_is_refused(account):
    with pytest.raises(ValueError, match=r"DATABASE\.SCHEMA\.TABLE"):
        snowflake_source(["ORDERS"], client=account)


def test_missing_settings_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="SNOWFLAKE_ACCOUNT"):
        snowflake_source(TABLES)


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("order_id", "ORDER_ID"), (" Order_Id ", "ORDER_ID"), ('"Mixed Case"', "Mixed Case")],
)
def test_key_columns_follow_snowflake_naming(typed, stored):
    assert _column(typed) == stored


def test_only_snowflake_errors_are_translated():
    conn = FakeConnection(lambda sql: KeyError("not from snowflake"))

    with pytest.raises(KeyError):
        _query(conn, "SELECT 1")

    error = ProgrammingError(msg="Object does not exist.", errno=2003, send_telemetry=False)
    with pytest.raises(Exception, match="002003"):
        _query(FakeConnection(lambda sql: error), "SELECT 1")


def test_the_session_checks_the_host_before_logging_in():
    def refuse():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        _Session(settings=None, check_active=refuse, connect=lambda **kwargs: None)
