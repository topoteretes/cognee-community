"""How the connector reaches Redshift: the Data API over HTTPS, or a direct driver connection.

Both connections expose the same small surface the sync needs::

    rows = connection.query(sql, {"cognee_cursor": "2026-01-01 00:00:00"})
    for row in rows:  # dicts of column name -> plain Python value
        ...

SQL uses ``:name`` placeholders, the Data API's own syntax. Values are passed
as strings, never formatted into the SQL; Redshift converts them to the type
of the column they are compared with.
"""

import datetime
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("redshift_connector")

DATABASE_ENV = "REDSHIFT_DATABASE"
CLUSTER_ENV = "REDSHIFT_CLUSTER_IDENTIFIER"
WORKGROUP_ENV = "REDSHIFT_WORKGROUP_NAME"
DB_USER_ENV = "REDSHIFT_DB_USER"
SECRET_ARN_ENV = "REDSHIFT_SECRET_ARN"

# Shows up in SYS_QUERY_HISTORY, so admins can tell where a statement came from.
STATEMENT_NAME = "cognee-redshift-connector"

# The connector only names its own parameters with this prefix, so a ``:word``
# inside a custom query (a string literal, a ``::cast``) is never rewritten.
_PARAMETER = re.compile(r":(cognee_\w+)")
_TERMINAL_STATES = {"FINISHED", "FAILED", "ABORTED"}
_FETCH_SIZE = 1000


class RedshiftQueryError(RuntimeError):
    """A statement failed, was aborted or timed out in Redshift."""


class DataApiConnection:
    """Run statements through the Redshift Data API (``boto3`` ``redshift-data``).

    The Data API is an HTTPS endpoint, so the cluster can stay in a private
    VPC with no inbound route, tunnel or driver. AWS credentials come from
    the usual boto3 chain (environment, profile, instance or task role).

    Provisioned clusters take ``cluster_identifier`` and either ``db_user``
    (temporary credentials through ``redshift:GetClusterCredentials``),
    ``secret_arn`` (a Secrets Manager secret with a database user and
    password), or neither (your IAM identity becomes the database user).
    Serverless workgroups take ``workgroup_name`` and optionally
    ``secret_arn``.

    Every argument falls back to a ``REDSHIFT_*`` environment variable. The
    region is ``region_name``, else ``AWS_REGION``, else boto3's own choice
    (``AWS_DEFAULT_REGION`` or your profile).
    """

    def __init__(
        self,
        *,
        database: str | None = None,
        cluster_identifier: str | None = None,
        workgroup_name: str | None = None,
        db_user: str | None = None,
        secret_arn: str | None = None,
        region_name: str | None = None,
        timeout: float = 900.0,
        poll_interval: float = 0.5,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        database = database or os.getenv(DATABASE_ENV)
        cluster_identifier = cluster_identifier or os.getenv(CLUSTER_ENV)
        workgroup_name = workgroup_name or os.getenv(WORKGROUP_ENV)
        db_user = db_user or os.getenv(DB_USER_ENV)
        secret_arn = secret_arn or os.getenv(SECRET_ARN_ENV)

        if not database:
            raise ValueError(f"Redshift database is missing. Pass database= or set {DATABASE_ENV}.")
        if bool(cluster_identifier) == bool(workgroup_name):
            raise ValueError(
                "Pass exactly one of cluster_identifier= (provisioned cluster) or "
                f"workgroup_name= (Serverless), or set {CLUSTER_ENV} or {WORKGROUP_ENV}."
            )
        if db_user and secret_arn:
            raise ValueError("Pass db_user= or secret_arn=, not both.")
        if db_user and workgroup_name:
            raise ValueError(
                "db_user= only works with a provisioned cluster. Serverless maps your IAM "
                "identity to a database user, or takes secret_arn=."
            )

        self.database = database
        self._target: dict[str, str] = {"Database": database}
        if cluster_identifier:
            self._target["ClusterIdentifier"] = cluster_identifier
        if workgroup_name:
            self._target["WorkgroupName"] = workgroup_name
        if db_user:
            self._target["DbUser"] = db_user
        if secret_arn:
            self._target["SecretArn"] = secret_arn
        # boto3 itself only reads AWS_DEFAULT_REGION and the profile, so honour
        # the AWS_REGION that the AWS CLI docs and most tooling set as well.
        self._region_name = region_name or os.getenv("AWS_REGION") or None
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._client = client
        self._sleep = sleep

    def query(self, sql: str, params: Mapping[str, str] | None = None) -> Iterator[dict]:
        """Run one statement and yield its rows, following every result page."""
        client = self._get_client()
        request: dict[str, Any] = {**self._target, "Sql": sql, "StatementName": STATEMENT_NAME}
        if params:
            request["Parameters"] = [{"name": k, "value": v} for k, v in params.items()]
        statement_id = client.execute_statement(**request)["Id"]
        self._wait(client, statement_id)

        next_token = None
        names: list[str] | None = None
        while True:
            page_request = {"Id": statement_id}
            if next_token:
                page_request["NextToken"] = next_token
            page = client.get_statement_result(**page_request)
            if names is None:
                names = [column["name"] for column in page.get("ColumnMetadata", [])]
            for record in page.get("Records", []):
                yield dict(zip(names, (_field_value(field) for field in record), strict=True))
            next_token = page.get("NextToken")
            if not next_token:
                return

    def close(self) -> None:
        """Nothing to release: each statement is its own HTTPS call."""

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            # Standard mode retries throttling and transient errors with backoff.
            config = Config(retries={"mode": "standard", "max_attempts": 8})
            self._client = boto3.client(
                "redshift-data", region_name=self._region_name, config=config
            )
        return self._client

    def _wait(self, client: Any, statement_id: str) -> None:
        """Poll until the statement finishes; raise if it fails or takes too long."""
        deadline = time.monotonic() + self._timeout
        delay = self._poll_interval
        while True:
            description = client.describe_statement(Id=statement_id)
            status = description["Status"]
            if status == "FINISHED":
                return
            if status in _TERMINAL_STATES:
                raise RedshiftQueryError(
                    f"Redshift statement {status.lower()}: {description.get('Error') or status}"
                )
            if time.monotonic() >= deadline:
                client.cancel_statement(Id=statement_id)
                raise RedshiftQueryError(
                    f"Redshift statement did not finish within {self._timeout:.0f}s and was "
                    "cancelled. Raise timeout= or narrow the table with columns= or where=."
                )
            self._sleep(delay)
            delay = min(delay * 2, 5.0)


class DirectConnection:
    """Run statements over a database driver connection (DB-API 2).

    Use this when the Data API is not an option, for example when the
    cluster is only reachable through an SSH tunnel or a VPN. With no
    ``connect`` callable, ``redshift_connector.connect(**connect_kwargs)`` is
    used, so IAM (``iam=True``) and database passwords both work::

        DirectConnection(host="localhost", port=5439, database="dev",
                         user="awsuser", password="...")

    ``connect`` takes any zero-argument callable that returns a DB-API
    connection, with ``paramstyle`` naming its placeholder style (``format``
    for redshift_connector and psycopg, ``named`` for sqlite3).
    """

    def __init__(
        self,
        connect: Callable[[], Any] | None = None,
        *,
        paramstyle: str = "format",
        **connect_kwargs: Any,
    ):
        if paramstyle not in ("format", "pyformat", "named", "qmark"):
            raise ValueError(f"Unsupported paramstyle {paramstyle!r}.")
        if connect is None:
            try:
                import redshift_connector
            except ImportError as exc:
                raise ImportError(
                    "DirectConnection needs redshift_connector: "
                    'pip install "cognee-community-connector-redshift[direct]"'
                ) from exc

            def connect():
                return redshift_connector.connect(**connect_kwargs)

        elif connect_kwargs:
            raise ValueError("Pass connect= or driver keyword arguments, not both.")
        self._connect = connect
        self._paramstyle = paramstyle
        self._connection: Any = None
        self._database: str | None = None

    @property
    def database(self) -> str:
        if self._database is None:
            row = next(self.query("SELECT current_database() AS name"))
            self._database = str(row["name"])
        return self._database

    def query(self, sql: str, params: Mapping[str, str] | None = None) -> Iterator[dict]:
        """Run one statement and yield its rows in batches from the driver."""
        if self._connection is None:
            self._connection = self._connect()
        cursor = self._connection.cursor()
        try:
            if params:
                cursor.execute(*_bind(sql, params, self._paramstyle))
            else:
                cursor.execute(sql)
            names = [column[0] for column in cursor.description]
            while True:
                batch = cursor.fetchmany(_FETCH_SIZE)
                if not batch:
                    return
                for record in batch:
                    yield dict(zip(names, (_plain(value) for value in record), strict=True))
        finally:
            cursor.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _bind(sql: str, params: Mapping[str, str], paramstyle: str) -> tuple[str, Any]:
    """Rewrite the connector's ``:cognee_*`` placeholders for a DB-API driver."""
    if paramstyle == "named":
        return sql, dict(params)
    if paramstyle == "pyformat":
        return _PARAMETER.sub(r"%(\1)s", sql.replace("%", "%%")), dict(params)
    values: list[str] = []

    def placeholder(match: re.Match) -> str:
        values.append(params[match.group(1)])
        return "?" if paramstyle == "qmark" else "%s"

    # format style treats a literal % (LIKE 'a%') as a placeholder unless doubled.
    text = sql if paramstyle == "qmark" else sql.replace("%", "%%")
    return _PARAMETER.sub(placeholder, text), tuple(values)


def _field_value(field: Mapping[str, Any]) -> Any:
    """Convert one Data API ``Field`` to a plain value."""
    if field.get("isNull"):
        return None
    for kind in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if kind in field:
            return field[kind]
    if "blobValue" in field:
        return _plain(field["blobValue"])
    return None


def _plain(value: Any) -> Any:
    """Driver values to str / int / float / bool / None. Binary data is not text: dropped."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return str(value)
