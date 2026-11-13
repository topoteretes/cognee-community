"""Unit and integration tests for the BigQuery dlt connector.

All tests run in CI without live Google Cloud credentials.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_bigquery.bigquery import (
    BIGQUERY_SOURCE_NAME,
    _deleted_metadata_row,
    _render_schema_field,
    _render_table_metadata,
    _table_to_metadata_row,
    bigquery_source,
    sync_query_or_table_rows,
    sync_table_metadata,
)


# ---------------------------------------------------------------------------
# Test Doubles / Fakes
# ---------------------------------------------------------------------------
class FakeSchemaField:
    def __init__(
        self,
        name: str,
        field_type: str = "STRING",
        mode: str = "NULLABLE",
        description: str | None = None,
        fields: list[Any] | None = None,
    ):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.description = description
        self.fields = fields or []


class FakeTable:
    def __init__(
        self,
        project: str,
        dataset_id: str,
        table_id: str,
        description: str | None = None,
        schema: list[Any] | None = None,
        num_rows: int = 100,
        modified: datetime | None = None,
        labels: dict[str, str] | None = None,
    ):
        self.project = project
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.full_table_id = f"{project}.{dataset_id}.{table_id}"
        self.table_type = "TABLE"
        self.description = description
        self.schema = schema or []
        self.num_rows = num_rows
        self.created = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        self.modified = modified or datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        self.labels = labels or {}


class FakeBigQueryClient:
    def __init__(
        self,
        project: str = "test-project",
        tables: list[FakeTable] | None = None,
        query_results: list[dict[str, Any]] | None = None,
    ):
        self.project = project
        self.tables = {t.table_id: t for t in (tables or [])}
        self.query_results = query_results or []

    def list_tables(self, dataset_id: str):
        return [SimpleNamespace(table_id=tid) for tid in self.tables]

    def get_table(self, table_item: Any):
        tid = getattr(table_item, "table_id", str(table_item))
        return self.tables[tid]

    def query(self, sql: str):
        return SimpleNamespace(result=lambda: self.query_results)


# ---------------------------------------------------------------------------
# Unit tests: Metadata and Schema Rendering
# ---------------------------------------------------------------------------
def test_render_schema_field_flat_and_nested():
    simple_field = FakeSchemaField(
        name="user_id", field_type="INT64", mode="REQUIRED", description="User ID"
    )
    rendered = _render_schema_field(simple_field)
    assert len(rendered) == 1
    assert rendered[0] == "- `user_id` (INT64, REQUIRED): User ID"

    nested_field = FakeSchemaField(
        name="address",
        field_type="RECORD",
        mode="NULLABLE",
        description="User address",
        fields=[
            FakeSchemaField("city", "STRING", "NULLABLE", "City name"),
            FakeSchemaField("zip", "STRING", "REQUIRED"),
        ],
    )
    rendered_nested = _render_schema_field(nested_field)
    assert len(rendered_nested) == 3
    assert rendered_nested[0] == "- `address` (RECORD, NULLABLE): User address"
    assert rendered_nested[1] == "  - `city` (STRING, NULLABLE): City name"
    assert rendered_nested[2] == "  - `zip` (STRING, REQUIRED)"


def test_render_table_metadata():
    table = FakeTable(
        project="analytics-corp",
        dataset_id="warehouse",
        table_id="orders",
        description="Daily sales orders",
        schema=[
            FakeSchemaField("order_id", "STRING", "REQUIRED", "Order unique key"),
            FakeSchemaField("amount", "FLOAT64", "NULLABLE", "Transaction total"),
        ],
        num_rows=2500,
        labels={"tier": "gold"},
    )
    md = _render_table_metadata(table)
    assert "# BigQuery Table: `analytics-corp.warehouse.orders`" in md
    assert "**Description**: Daily sales orders" in md
    assert "**Type**: TABLE" in md
    assert "**Row Count**: 2,500" in md
    assert "**Labels**: tier=gold" in md
    assert "- `order_id` (STRING, REQUIRED): Order unique key" in md
    assert "- `amount` (FLOAT64, NULLABLE): Transaction total" in md


def test_table_to_metadata_row():
    table = FakeTable("p", "d", "t")
    row = _table_to_metadata_row(table)
    assert row["id"] == "bigquery://p/d/t/metadata"
    assert row["title"] == "BigQuery Table: p.d.t"
    assert row["_deleted"] is False
    assert "https://console.cloud.google.com/bigquery" in row["url"]


def test_deleted_metadata_row():
    tombstone = _deleted_metadata_row("p", "d", "t")
    assert tombstone["id"] == "bigquery://p/d/t/metadata"
    assert tombstone["_deleted"] is True


def test_bigquery_source_validation():
    with pytest.raises(ValueError, match="Must provide either 'dataset_id' or 'query'"):
        bigquery_source()


def test_bigquery_source_document_marker():
    client = FakeBigQueryClient()
    source = bigquery_source(dataset_id="analytics", client=client)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == BIGQUERY_SOURCE_NAME


# ---------------------------------------------------------------------------
# Incremental & Forget-on-Delete State Machine Tests
# ---------------------------------------------------------------------------
def test_sync_table_metadata_incremental():
    t1 = FakeTable("p", "d", "t1", modified=datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    t2 = FakeTable("p", "d", "t2", modified=datetime(2026, 1, 1, 11, 0, tzinfo=UTC))
    client = FakeBigQueryClient(tables=[t1, t2])
    state: dict[str, Any] = {}

    # Initial sync
    initial_rows = list(sync_table_metadata(client, "d", state))
    assert len(initial_rows) == 2
    assert state["known_table_ids"] == ["t1", "t2"]
    assert "2026-01-01T11:00:00" in state["last_when"]

    # Re-sync without changes yields nothing
    re_sync_rows = list(sync_table_metadata(client, "d", state))
    assert len(re_sync_rows) == 0

    # Modify t2
    t2.modified = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    updated_rows = list(sync_table_metadata(client, "d", state))
    assert len(updated_rows) == 1
    assert updated_rows[0]["table_id"] == "t2"


def test_sync_table_metadata_forget_on_delete():
    t1 = FakeTable("p", "d", "t1")
    t2 = FakeTable("p", "d", "t2")
    client = FakeBigQueryClient(tables=[t1, t2])
    state: dict[str, Any] = {}

    list(sync_table_metadata(client, "d", state))
    assert state["known_table_ids"] == ["t1", "t2"]

    # Drop t2 upstream in BigQuery
    del client.tables["t2"]

    next_sync_rows = list(sync_table_metadata(client, "d", state))
    # Should yield a deletion tombstone for t2
    assert len(next_sync_rows) == 1
    assert next_sync_rows[0]["id"] == "bigquery://test-project/d/t2/metadata"
    assert next_sync_rows[0]["_deleted"] is True
    assert state["known_table_ids"] == ["t1"]


def test_sync_query_or_table_rows_incremental_and_soft_delete():
    query_data = [
        {"id": 1, "name": "Alice", "updated_at": "2026-01-01T10:00:00", "is_deleted": False},
        {"id": 2, "name": "Bob", "updated_at": "2026-01-01T11:00:00", "is_deleted": False},
        {"id": 3, "name": "Charlie", "updated_at": "2026-01-01T12:00:00", "is_deleted": True},
    ]
    client = FakeBigQueryClient(query_results=query_data)
    state: dict[str, Any] = {}

    rows = list(
        sync_query_or_table_rows(
            client,
            state,
            query="SELECT * FROM table",
            incremental_column="updated_at",
            primary_key="id",
            soft_delete_column="is_deleted",
        )
    )

    assert len(rows) == 3
    assert rows[0]["_deleted"] is False
    assert rows[1]["_deleted"] is False
    assert rows[2]["_deleted"] is True  # Charlie soft-deleted
    assert state["query_cursor"] == "2026-01-01T12:00:00"


# ---------------------------------------------------------------------------
# End-to-end dlt pipeline integration test
# ---------------------------------------------------------------------------
def test_dlt_pipeline_sync_and_forget(tmp_path):
    pytest.importorskip("dlt")
    import dlt

    t1 = FakeTable("test-project", "d", "users", description="User accounts")
    t2 = FakeTable("test-project", "d", "logs", description="System logs")
    client = FakeBigQueryClient(tables=[t1, t2])

    db_path = (tmp_path / "bigquery.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_bq_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="test_bq_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = bigquery_source(dataset_id="d", client=client)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    # Verify rows in sqlite destination
    with (
        pipeline.sql_client() as sql_c,
        sql_c.execute_query("SELECT id FROM bigquery_documents") as cursor,
    ):
        rows = cursor.fetchall()
        loaded_ids = {r[0] for r in rows}
        assert "bigquery://test-project/d/users/metadata" in loaded_ids
        assert "bigquery://test-project/d/logs/metadata" in loaded_ids

    # Drop 'logs' upstream and re-sync
    del client.tables["logs"]
    source_resync = bigquery_source(dataset_id="d", client=client)
    info2 = pipeline.run(source_resync)
    assert info2.has_failed_jobs is False

    # Under merge disposition with hard_delete=True, 'logs' should be removed
    with (
        pipeline.sql_client() as sql_c,
        sql_c.execute_query("SELECT id FROM bigquery_documents") as cursor,
    ):
        rows2 = cursor.fetchall()
        remaining_ids = {r[0] for r in rows2}
        assert "bigquery://test-project/d/users/metadata" in remaining_ids
        assert "bigquery://test-project/d/logs/metadata" not in remaining_ids
