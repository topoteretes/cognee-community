"""The two transports: the Data API (botocore Stubber) and the DB-API parameter binding.

The Stubber wraps a real ``redshift-data`` client, so every request is
validated against the AWS API model: a wrong parameter name or shape fails
here, not against a live cluster.
"""

import boto3
import pytest
from botocore.stub import Stubber

from cognee_community_connector_redshift import (
    DataApiConnection,
    DirectConnection,
    RedshiftQueryError,
)
from cognee_community_connector_redshift.connection import STATEMENT_NAME, _bind


@pytest.fixture
def client():
    return boto3.client(
        "redshift-data",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _connection(client, **kwargs):
    kwargs.setdefault("database", "dev")
    kwargs.setdefault("cluster_identifier", "analytics")
    kwargs.setdefault("db_user", "cognee_reader")
    return DataApiConnection(client=client, sleep=lambda _seconds: None, **kwargs)


COLUMNS = [{"name": "id", "typeName": "int4"}, {"name": "subject", "typeName": "varchar"}]


def test_query_runs_polls_and_follows_every_page(client):
    connection = _connection(client)
    with Stubber(client) as stub:
        stub.add_response(
            "execute_statement",
            {"Id": "stmt-1"},
            {
                "Sql": "SELECT * FROM t WHERE updated_at >= :cognee_cursor",
                "Database": "dev",
                "ClusterIdentifier": "analytics",
                "DbUser": "cognee_reader",
                "StatementName": STATEMENT_NAME,
                "Parameters": [{"name": "cognee_cursor", "value": "2026-01-01 00:00:00"}],
            },
        )
        stub.add_response("describe_statement", {"Id": "stmt-1", "Status": "STARTED"})
        stub.add_response("describe_statement", {"Id": "stmt-1", "Status": "FINISHED"})
        stub.add_response(
            "get_statement_result",
            {
                "Records": [[{"longValue": 1}, {"stringValue": "Login fails"}]],
                "ColumnMetadata": COLUMNS,
                "TotalNumRows": 2,
                "NextToken": "page-2",
            },
            {"Id": "stmt-1"},
        )
        stub.add_response(
            "get_statement_result",
            {
                "Records": [[{"longValue": 2}, {"isNull": True}]],
                "ColumnMetadata": COLUMNS,
                "TotalNumRows": 2,
            },
            {"Id": "stmt-1", "NextToken": "page-2"},
        )

        rows = list(
            connection.query(
                "SELECT * FROM t WHERE updated_at >= :cognee_cursor",
                {"cognee_cursor": "2026-01-01 00:00:00"},
            )
        )
        stub.assert_no_pending_responses()

    assert rows == [{"id": 1, "subject": "Login fails"}, {"id": 2, "subject": None}]


def test_field_types_are_converted(client):
    connection = _connection(client)
    with Stubber(client) as stub:
        stub.add_response("execute_statement", {"Id": "s"})
        stub.add_response("describe_statement", {"Id": "s", "Status": "FINISHED"})
        stub.add_response(
            "get_statement_result",
            {
                "Records": [
                    [
                        {"booleanValue": True},
                        {"doubleValue": 1.5},
                        {"stringValue": "2026-01-01 10:00:00"},
                        {"blobValue": b"\x00\x01"},
                    ]
                ],
                "ColumnMetadata": [{"name": n} for n in ("flag", "ratio", "at", "raw")],
                "TotalNumRows": 1,
            },
        )
        (row,) = connection.query("SELECT 1")

    # Binary data is not text: it is dropped rather than ingested as bytes.
    assert row == {"flag": True, "ratio": 1.5, "at": "2026-01-01 10:00:00", "raw": None}


def test_failed_statement_raises_with_redshifts_error(client):
    connection = _connection(client)
    with Stubber(client) as stub:
        stub.add_response("execute_statement", {"Id": "s"})
        stub.add_response(
            "describe_statement",
            {"Id": "s", "Status": "FAILED", "Error": 'ERROR: relation "public.x" does not exist'},
        )
        with pytest.raises(RedshiftQueryError, match="does not exist"):
            list(connection.query("SELECT * FROM public.x"))


def test_slow_statement_is_cancelled(client):
    connection = _connection(client, timeout=0)
    with Stubber(client) as stub:
        stub.add_response("execute_statement", {"Id": "s"})
        stub.add_response("describe_statement", {"Id": "s", "Status": "STARTED"})
        stub.add_response("cancel_statement", {"Status": True}, {"Id": "s"})
        with pytest.raises(RedshiftQueryError, match="cancelled"):
            list(connection.query("SELECT 1"))
        stub.assert_no_pending_responses()


def test_serverless_with_secret_and_no_parameters(client):
    connection = _connection(
        client,
        cluster_identifier=None,
        db_user=None,
        workgroup_name="analytics-wg",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:redshift-abc",
    )
    with Stubber(client) as stub:
        stub.add_response(
            "execute_statement",
            {"Id": "s"},
            {
                "Sql": "SELECT 1",
                "Database": "dev",
                "WorkgroupName": "analytics-wg",
                "SecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:redshift-abc",
                "StatementName": STATEMENT_NAME,
            },
        )
        stub.add_response("describe_statement", {"Id": "s", "Status": "FINISHED"})
        stub.add_response(
            "get_statement_result", {"Records": [], "ColumnMetadata": [], "TotalNumRows": 0}
        )
        assert list(connection.query("SELECT 1")) == []
    assert connection.database == "dev"


def test_settings_come_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("REDSHIFT_DATABASE", "warehouse")
    monkeypatch.setenv("REDSHIFT_WORKGROUP_NAME", "wg")
    connection = DataApiConnection(client=client)
    assert connection.database == "warehouse"
    assert connection._target == {"Database": "warehouse", "WorkgroupName": "wg"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cluster_identifier": "c"}, "database"),
        ({"database": "dev"}, "exactly one"),
        ({"database": "dev", "cluster_identifier": "c", "workgroup_name": "w"}, "exactly one"),
        (
            {"database": "dev", "cluster_identifier": "c", "db_user": "u", "secret_arn": "a"},
            "not both",
        ),
        ({"database": "dev", "workgroup_name": "w", "db_user": "u"}, "provisioned"),
    ],
)
def test_invalid_settings_fail_early(kwargs, message, monkeypatch):
    for name in ("DATABASE", "CLUSTER_IDENTIFIER", "WORKGROUP_NAME", "DB_USER", "SECRET_ARN"):
        monkeypatch.delenv(f"REDSHIFT_{name}", raising=False)
    with pytest.raises(ValueError, match=message):
        DataApiConnection(**kwargs)


# ---------------------------------------------------------------------------
# DB-API binding
# ---------------------------------------------------------------------------
SQL = "SELECT * FROM t WHERE note LIKE 'a%' AND x::text = 'b:c' AND at >= :cognee_cursor"


def test_bind_format_style_escapes_percent_and_keeps_casts():
    sql, values = _bind(SQL, {"cognee_cursor": "2026"}, "format")
    assert sql == "SELECT * FROM t WHERE note LIKE 'a%%' AND x::text = 'b:c' AND at >= %s"
    assert values == ("2026",)


def test_bind_other_styles():
    assert _bind(SQL, {"cognee_cursor": "2026"}, "pyformat")[0].endswith("%(cognee_cursor)s")
    assert _bind(SQL, {"cognee_cursor": "2026"}, "qmark") == (
        SQL.replace(":cognee_cursor", "?"),
        ("2026",),
    )
    assert _bind(SQL, {"cognee_cursor": "2026"}, "named") == (SQL, {"cognee_cursor": "2026"})


def test_bind_orders_values_by_position():
    sql, values = _bind(
        "a = :cognee_key_0_1 AND b = :cognee_key_0_0",
        {"cognee_key_0_0": "x", "cognee_key_0_1": "y"},
        "format",
    )
    assert sql == "a = %s AND b = %s"
    assert values == ("y", "x")


def test_direct_connection_needs_one_way_to_connect():
    with pytest.raises(ValueError, match="not both"):
        DirectConnection(lambda: None, host="localhost")
    with pytest.raises(ValueError, match="paramstyle"):
        DirectConnection(lambda: None, paramstyle="numeric")


def test_direct_connection_uses_redshift_connector_by_default(monkeypatch):
    import redshift_connector

    calls = []
    monkeypatch.setattr(redshift_connector, "connect", lambda **kw: calls.append(kw))
    connection = DirectConnection(host="localhost", port=5439, database="dev", iam=True)
    connection._connect()
    assert calls == [{"host": "localhost", "port": 5439, "database": "dev", "iam": True}]
