"""Unit tests for the Apache Iceberg dlt connector.

Runnable in CI without requiring an active external lakehouse cluster:
* DB-free unit tests for schema, partition spec, snapshot, and metadata markdown rendering.
* Full-snapshot dlt pipeline sync tests verifying ingestion into staging.
* Reconciliation and forget-on-delete verification: dropped tables vanish from replacement sync.
"""

from types import SimpleNamespace

import pytest

from cognee_community_connector_iceberg.iceberg import (
    _is_gone,
    _is_transient,
    _render_partition_spec,
    _render_properties,
    _render_schema,
    _render_snapshots,
    _table_to_row,
    iceberg_source,
)

# ---------------------------------------------------------------------------
# Fixtures / In-Memory Catalog Fakes
# ---------------------------------------------------------------------------

class FakeField:
    def __init__(self, field_id, name, field_type, required=False, doc=""):
        self.field_id = field_id
        self.name = name
        self.field_type = field_type
        self.required = required
        self.doc = doc


class FakeSchema:
    def __init__(self, fields):
        self.fields = fields


class FakePartitionField:
    def __init__(self, source_id, name, transform):
        self.source_id = source_id
        self.name = name
        self.transform = transform


class FakePartitionSpec:
    def __init__(self, fields):
        self.fields = fields


class FakeSnapshot:
    def __init__(self, snapshot_id, timestamp_ms, operation="append", records="100"):
        self.snapshot_id = snapshot_id
        self.timestamp_ms = timestamp_ms
        self.summary = {"operation": operation, "added-records": records}


class FakeIcebergTable:
    def __init__(
        self,
        identifier,
        fields=None,
        partition_fields=None,
        snapshots=None,
        properties=None,
    ):
        self.identifier = tuple(identifier) if isinstance(identifier, list) else identifier
        self._schema = FakeSchema(fields or [])
        self._spec = FakePartitionSpec(partition_fields or [])
        self._snapshots = snapshots or []
        self.properties = properties or {}
        self.metadata = SimpleNamespace(format_version=2)

    def schema(self):
        return self._schema

    def spec(self):
        return self._spec

    def snapshots(self):
        return self._snapshots

    def current_snapshot_id(self):
        return self._snapshots[-1].snapshot_id if self._snapshots else None


class FakeIcebergCatalog:
    def __init__(self, tables_by_id):
        self._tables = tables_by_id

    def list_namespaces(self):
        namespaces = set()
        for ident in self._tables:
            if len(ident) > 1:
                namespaces.add(ident[:-1])
        return list(namespaces)

    def list_tables(self, namespace):
        return [ident for ident in self._tables if ident[:-1] == namespace]

    def load_table(self, identifier):
        ident = tuple(identifier) if isinstance(identifier, list) else identifier
        if ident in self._tables:
            return self._tables[ident]
        from pyiceberg.exceptions import NoSuchTableError
        raise NoSuchTableError(f"Table {ident} not found")


# ---------------------------------------------------------------------------
# Rendering Unit Tests (DB-Free)
# ---------------------------------------------------------------------------

def test_render_schema_formats_markdown_table():
    fields = [
        FakeField(1, "order_id", "string", required=True, doc="Primary order ID"),
        FakeField(2, "amount", "double", required=False, doc="Transaction amount"),
    ]
    md = _render_schema(FakeSchema(fields))
    assert "| `order_id` | `string` | Yes | Primary order ID |" in md
    assert "| `amount` | `double` | No | Transaction amount |" in md


def test_render_partition_spec_unpartitioned():
    assert _render_partition_spec(FakePartitionSpec([])) == "_Table is unpartitioned._"


def test_render_partition_spec_partitioned():
    pfields = [FakePartitionField(1, "event_day", "day")]
    md = _render_partition_spec(FakePartitionSpec(pfields))
    assert "| 1 | `event_day` | `day` |" in md


def test_render_snapshots_formatting():
    snaps = [FakeSnapshot(1001, 1700000000000, "append", "500")]
    md = _render_snapshots(snaps)
    assert "| `1001` |" in md
    assert "| `append` | 500 |" in md


def test_render_properties_redacts_secrets():
    props = {
        "owner": "data-team",
        "api_key": "sensitive_leaked_secret",
        "format": "parquet",
    }
    md = _render_properties(props)
    assert "| `owner` | `data-team` |" in md
    assert "| `format` | `parquet` |" in md
    assert "sensitive_leaked_secret" not in md


def test_table_to_row_structure():
    tbl = FakeIcebergTable(
        identifier=("lake", "finance", "transactions"),
        fields=[FakeField(1, "id", "long", required=True)],
        properties={"retention": "30d"}
    )
    row = _table_to_row(tbl)
    assert row["id"] == "lake.finance.transactions"
    assert row["url"] == "iceberg://lake.finance.transactions"
    assert "Apache Iceberg Table: lake.finance.transactions" in row["title"]
    assert "# Iceberg Table: lake.finance.transactions" in row["content"]
    assert "| `id` | `long` | Yes |" in row["content"]


# ---------------------------------------------------------------------------
# Error Handling Unit Tests
# ---------------------------------------------------------------------------

def test_error_classification():
    class NoSuchTableError(Exception):
        pass

    assert _is_gone(NoSuchTableError("Table dropped")) is True
    assert _is_transient(Exception("HTTP 503 Gateway Timeout")) is True
    assert _is_transient(Exception("Connection refused by catalog server")) is True
    assert _is_transient(ValueError("Bad configuration")) is False
    assert _is_gone(ValueError("Bad configuration")) is False


# ---------------------------------------------------------------------------
# dlt Pipeline Integration Tests (Mock Catalog + In-Memory Staging)
# ---------------------------------------------------------------------------

@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_iceberg_source_sync_and_forget_on_delete(dlt_mod, tmp_path):
    table_a = FakeIcebergTable(
        identifier=("db", "table_a"),
        fields=[FakeField(1, "col1", "int", required=True)]
    )
    table_b = FakeIcebergTable(
        identifier=("db", "table_b"),
        fields=[FakeField(1, "col2", "string", required=False)]
    )

    catalog_data = {
        ("db", "table_a"): table_a,
        ("db", "table_b"): table_b,
    }
    fake_catalog = FakeIcebergCatalog(catalog_data)

    db_path = (tmp_path / "iceberg_sync.duckdb").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="iceberg_test_pipeline",
        destination=dlt_mod.destinations.duckdb(db_path),
        dataset_name="iceberg_staging",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Initial sync with two tables
    source_1 = iceberg_source(catalog=fake_catalog, namespaces=[("db",)])
    pipeline.run(source_1)

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM iceberg_tables") as cur,
    ):
        rows = {r[0] for r in cur.fetchall()}
    assert "db.table_a" in rows
    assert "db.table_b" in rows

    # Second sync: table_b dropped from upstream catalog
    del catalog_data[("db", "table_b")]
    fake_catalog_after = FakeIcebergCatalog(catalog_data)

    source_2 = iceberg_source(catalog=fake_catalog_after, namespaces=[("db",)])
    pipeline.run(source_2)

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM iceberg_tables") as cur,
    ):
        rows_after = {r[0] for r in cur.fetchall()}
    assert "db.table_a" in rows_after
    assert "db.table_b" not in rows_after, (
        "table_b must be reconciled out of staging (forget-on-delete)"
    )
