"""100% offline unit tests for the Databricks data connector."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_databricks.databricks import (
    DOCUMENT_SOURCE_ATTR,
    DatabricksClient,
    _fetch_delta_history,
    _get_workspace_id,
    _iter_notebook_rows,
    _iter_query_rows,
    databricks_source,
)


def _make_mock_response(
    status_code: int, json_data: dict[str, Any] | None = None
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    if json_data is not None:
        import json

        resp._content = json.dumps(json_data).encode("utf-8")
        resp.headers["Content-Type"] = "application/json"
    return resp


def test_missing_required_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate that missing host or token raises ValueError."""
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="host is required"):
        databricks_source(host=None, token="dapi_token")

    with pytest.raises(ValueError, match="token is required"):
        databricks_source(host="https://dbc.cloud.databricks.com", token=None)


def test_get_workspace_id() -> None:
    """Verify clean workspace ID extraction from host URLs."""
    assert _get_workspace_id("https://dbc-12345678.cloud.databricks.com") == "dbc-12345678"
    assert _get_workspace_id("https://adb-998877.azuredatabricks.net") == "adb-998877"


def test_notebook_ingestion_and_document_mode(
    sample_host: str,
    sample_token: str,
    mock_workspace_items: dict[str, Any],
    mock_notebook_content_b64: str,
) -> None:
    """Validate notebook extraction, base64 source decoding, and document mode."""
    client = DatabricksClient(sample_host, sample_token)
    client.list_workspace = MagicMock(return_value=mock_workspace_items["objects"][:1])
    client.export_notebook = MagicMock(return_value="print('Ingesting pipeline')")

    source = databricks_source(
        host=sample_host,
        token=sample_token,
        include=["notebooks"],
        client=client,
    )

    notebook_res = source.resources["databricks_notebooks"]
    assert getattr(notebook_res, DOCUMENT_SOURCE_ATTR, None) == "databricks_notebook"

    rows = list(notebook_res)
    assert len(rows) == 1

    row = rows[0]
    assert row["id"] == "databricks:dbc-12345678:notebook:987654321"
    assert row["title"] == "etl_pipeline"
    assert row["content"] == "print('Ingesting pipeline')"
    assert row["_deleted"] is False


def test_notebook_rename_preserves_object_id(
    sample_host: str,
    sample_token: str,
) -> None:
    """Validate identity stability when a notebook is renamed or moved."""
    client = DatabricksClient(sample_host, sample_token)
    state: dict[str, Any] = {}

    # Initial state
    nb_orig = [{"object_id": 987654321, "path": "/Shared/old_name", "object_type": "NOTEBOOK"}]
    client.list_workspace = MagicMock(return_value=nb_orig)
    client.export_notebook = MagicMock(return_value="source_code")

    rows1 = list(_iter_notebook_rows(client, "dbc-123", ["/Shared"], state))
    assert rows1[0]["id"] == "databricks:dbc-123:notebook:987654321"

    # Renamed notebook (same object_id, different path)
    nb_renamed = [{"object_id": 987654321, "path": "/Shared/new_name", "object_type": "NOTEBOOK"}]
    client.list_workspace = MagicMock(return_value=nb_renamed)

    rows2 = list(_iter_notebook_rows(client, "dbc-123", ["/Shared"], state))
    assert rows2[0]["id"] == "databricks:dbc-123:notebook:987654321"
    assert rows2[0]["title"] == "new_name"


def test_notebook_deletion_tombstone_emission(
    sample_host: str,
    sample_token: str,
) -> None:
    """Validate tombstone emission when a previously known notebook is deleted."""
    client = DatabricksClient(sample_host, sample_token)
    # State holds known notebooks
    state = {"known_notebook_ids": ["987654321", "deleted_notebook_id"]}

    # Current listing only has 987654321
    current_items = [{"object_id": 987654321, "path": "/Shared/live_nb", "object_type": "NOTEBOOK"}]
    client.list_workspace = MagicMock(return_value=current_items)
    client.export_notebook = MagicMock(return_value="live_content")

    rows = list(_iter_notebook_rows(client, "dbc-123", ["/Shared"], state))

    # Should yield 1 live row + 1 deletion tombstone
    assert len(rows) == 2

    live_row = next(r for r in rows if not r["_deleted"])
    assert live_row["id"] == "databricks:dbc-123:notebook:987654321"

    tombstone = next(r for r in rows if r["_deleted"])
    assert tombstone["id"] == "databricks:dbc-123:notebook:deleted_notebook_id"
    assert tombstone["_deleted"] is True
    assert state["known_notebook_ids"] == ["987654321"]


def test_notebook_listing_failure_prevents_deletions(
    sample_host: str,
    sample_token: str,
) -> None:
    """Safety guardrail: transient listing failure must NOT delete existing notebooks."""
    client = DatabricksClient(sample_host, sample_token)
    state = {"known_notebook_ids": ["nb_1", "nb_2"]}

    # Simulation of API failure during traversal
    client.list_workspace = MagicMock(side_effect=RuntimeError("500 Internal Server Error"))

    rows = list(_iter_notebook_rows(client, "dbc-123", ["/Shared"], state))

    # No tombstones should be emitted
    assert len(rows) == 0
    # Existing known state must be retained
    assert state["known_notebook_ids"] == ["nb_1", "nb_2"]


def test_table_metadata_ingestion(
    sample_host: str,
    sample_token: str,
    mock_catalogs_data: dict[str, Any],
    mock_schemas_data: dict[str, Any],
    mock_tables_data: dict[str, Any],
) -> None:
    """Validate Unity Catalog tables, schema definition, and structured mode."""
    client = DatabricksClient(sample_host, sample_token)
    client.list_catalogs = MagicMock(return_value=mock_catalogs_data["catalogs"])
    client.list_schemas = MagicMock(return_value=mock_schemas_data["schemas"])
    client.list_tables = MagicMock(return_value=mock_tables_data["tables"])

    source = databricks_source(
        host=sample_host,
        token=sample_token,
        include=["tables"],
        client=client,
    )

    table_res = source.resources["databricks_tables"]
    # Relational structured contract: DOCUMENT_SOURCE_ATTR is NOT set
    assert getattr(table_res, DOCUMENT_SOURCE_ATTR, None) is None

    rows = list(table_res)
    assert len(rows) == 1

    row = rows[0]
    assert row["id"] == "databricks:dbc-12345678:table:main.analytics.dim_customers"
    assert row["catalog"] == "main"
    assert row["schema"] == "analytics"
    assert row["table_name"] == "dim_customers"
    assert len(row["columns"]) == 2
    assert row["columns"][0]["name"] == "customer_id"
    assert row["_deleted"] is False


def test_delta_history_change_tracking(
    sample_host: str,
    sample_token: str,
    mock_describe_history_response: dict[str, Any],
) -> None:
    """Validate DESCRIBE HISTORY detects mutations while skipping maintenance ops."""
    client = DatabricksClient(sample_host, sample_token)
    client.execute_statement = MagicMock(return_value=mock_describe_history_response)

    version, timestamp = _fetch_delta_history(client, "warehouse_123", "main.default.orders")

    # In mock fixture: MERGE op has version 42, OPTIMIZE op (41) is ignored
    assert version == 42
    assert timestamp == 1700000050000


def test_sql_statement_execution_chunks(
    sample_host: str,
    sample_token: str,
) -> None:
    """Validate chunked pagination for explicitly configured SQL queries."""
    client = DatabricksClient(sample_host, sample_token)

    # Initial execution response (chunk 0)
    client.execute_statement = MagicMock(
        return_value={
            "statement_id": "stmt-abc",
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "schema": {"columns": [{"name": "id"}, {"name": "val"}]},
                "total_chunk_count": 2,
            },
            "result": {"data_array": [[1, "chunk0"]]},
        }
    )

    # Chunk 1 response
    client.get_statement_chunk = MagicMock(return_value={"data_array": [[2, "chunk1"]]})

    queries = [
        {
            "name": "sample_query",
            "statement": "SELECT id, val FROM data",
            "warehouse_id": "wh_001",
            "primary_key": "id",
        }
    ]
    state: dict[str, Any] = {}

    rows = list(_iter_query_rows(client, "dbc-123", queries, state))
    assert len(rows) == 2

    assert rows[0]["id"] == "databricks:dbc-123:query:sample_query:1"
    assert rows[0]["val"] == "chunk0"

    assert rows[1]["id"] == "databricks:dbc-123:query:sample_query:2"
    assert rows[1]["val"] == "chunk1"


def test_client_auth_and_permission_errors(
    sample_host: str,
    sample_token: str,
) -> None:
    """Validate descriptive error handling on 401 and 403 HTTP responses."""
    session = requests.Session()
    client = DatabricksClient(sample_host, sample_token, session=session, max_retries=0)

    with (
        patch.object(session, "request", return_value=_make_mock_response(401)),
        pytest.raises(RuntimeError, match="authentication failed"),
    ):
        client.list_workspace("/Shared")

    with (
        patch.object(session, "request", return_value=_make_mock_response(403)),
        pytest.raises(RuntimeError, match="access forbidden"),
    ):
        client.list_catalogs()


def test_token_privacy_in_source(
    sample_host: str,
    sample_token: str,
) -> None:
    """Verify that the Personal Access Token is never leaked in string representation."""
    source = databricks_source(
        host=sample_host,
        token=sample_token,
        include=["notebooks"],
    )

    source_str = str(source)
    source_repr = repr(source)

    assert sample_token not in source_str
    assert sample_token not in source_repr
