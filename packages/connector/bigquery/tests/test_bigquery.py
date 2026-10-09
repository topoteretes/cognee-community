"""Unit tests for the BigQuery dlt connector.

Two layers, all runnable without GCP credentials:

* DB-free tests for metadata/row rendering, scope handling, the cursor query,
  the deletion sweep and the byte cap, driven through ``sync_metadata`` /
  ``sync_rows`` with a fake client and a plain dict as state.
* dlt-pipeline tests (fake client, temp sqlite destination, ``merge`` like
  ``cognee.remember(..., write_disposition="merge")``) covering the acceptance
  criteria: first sync, edits re-sync, and dropped tables/rows are forgotten.
"""

import re
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.cloud.bigquery import SchemaField

from cognee_community_connector_bigquery.bigquery import (
    BIGQUERY_SOURCE_NAME,
    RowSync,
    _build_scope,
    bigquery_source,
    format_value,
    sync_metadata,
    sync_rows,
    table_to_row,
)

PROJECT = "proj"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _dataset(dataset_id, description=None, labels=None):
    return SimpleNamespace(
        project=PROJECT,
        dataset_id=dataset_id,
        friendly_name=None,
        description=description,
        location="US",
        labels=labels or {},
    )


def _table(dataset_id, table_id, schema, description=None, table_type="TABLE", **extra):
    fields = {
        "project": PROJECT,
        "dataset_id": dataset_id,
        "table_id": table_id,
        "table_type": table_type,
        "friendly_name": None,
        "description": description,
        "labels": {},
        "time_partitioning": None,
        "clustering_fields": None,
        "schema": schema,
        "view_query": None,
        "mview_query": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


ORDERS_SCHEMA = [
    SchemaField("order_id", "INTEGER", mode="REQUIRED", description="Unique order id."),
    SchemaField("status", "STRING", description="Order status."),
    SchemaField("updated_at", "TIMESTAMP"),
]


class FakeQueryJob:
    def __init__(self, rows, total_bytes_processed):
        self._rows = rows
        self.total_bytes_processed = total_bytes_processed

    def result(self):
        return iter(self._rows)


class FakeBigQueryClient:
    """In-memory stand-in for google.cloud.bigquery.Client.

    ``query`` understands only the two shapes the connector issues: a SELECT from
    one backticked table, optionally with ``WHERE `col` >= @since`` (or ``>``).
    """

    project = PROJECT

    def __init__(self, datasets, tables, rows=None, bytes_per_query=1_000):
        self.datasets = datasets  # {dataset_id: dataset}
        self.tables = tables  # {"dataset.table": table}
        self.rows = rows or {}  # {"dataset.table": [row dict, ...]}
        self.bytes_per_query = bytes_per_query
        self.queries = []  # (sql, {param: value}, dry_run)

    def list_datasets(self, project=None):
        return [SimpleNamespace(dataset_id=d) for d in self.datasets]

    def get_dataset(self, ref):
        dataset_id = ref.split(".")[-1]
        if dataset_id not in self.datasets:
            raise NotFound(f"Dataset {ref} not found")
        return self.datasets[dataset_id]

    def list_tables(self, ref):
        dataset_id = ref.split(".")[-1]
        return [
            SimpleNamespace(table_id=key.split(".")[1])
            for key in self.tables
            if key.startswith(f"{dataset_id}.")
        ]

    def get_table(self, ref):
        key = ".".join(ref.split(".")[-2:])
        if key not in self.tables:
            raise NotFound(f"Table {ref} not found")
        return self.tables[key]

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in job_config.query_parameters}
        self.queries.append((sql, params, bool(job_config.dry_run)))
        if job_config.dry_run:
            return FakeQueryJob([], self.bytes_per_query)
        key = ".".join(re.search(r"FROM `([^`]+)`", sql).group(1).split(".")[-2:])
        rows = self.rows.get(key, [])
        where = re.search(r"WHERE `(\w+)` (>=|>) @since", sql)
        if where:
            column, op = where.groups()
            since = params["since"]
            rows = [r for r in rows if (r[column] >= since if op == ">=" else r[column] > since)]
        return FakeQueryJob(rows, self.bytes_per_query)


def _orders_client(rows=None, **kwargs):
    return FakeBigQueryClient(
        datasets={"sales": _dataset("sales", description="Sales data.")},
        tables={"sales.orders": _table("sales", "orders", ORDERS_SCHEMA, "One row per order.")},
        rows={"sales.orders": rows or []},
        **kwargs,
    )


def _order(order_id, status, day):
    return {
        "order_id": order_id,
        "status": status,
        "updated_at": datetime(2024, 1, day, tzinfo=UTC),
    }


ORDERS_SYNC = RowSync(table="sales.orders", key_column="order_id", cursor_column="updated_at")

# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_table_document_renders_schema_and_descriptions():
    schema = [
        SchemaField("id", "INTEGER", mode="REQUIRED", description="Customer id."),
        SchemaField(
            "address",
            "RECORD",
            description="Postal address.",
            fields=[SchemaField("city", "STRING", description="City name.")],
        ),
    ]
    table = _table(
        "crm",
        "customers",
        schema,
        description="One row per customer.",
        labels={"team": "growth", "env": "prod"},
        time_partitioning=SimpleNamespace(field="created_at", type_="DAY"),
        clustering_fields=["region"],
    )

    row = table_to_row(table)

    assert row["id"] == "proj.crm.customers"
    assert row["_deleted"] is False
    content = row["content"]
    assert "Description: One row per customer." in content
    assert "Labels: env=prod, team=growth" in content  # sorted, stable
    assert "Partitioned by: created_at (DAY)" in content
    assert "Clustered by: region" in content
    assert "- id (INTEGER, REQUIRED): Customer id." in content
    assert "  - address.city (STRING, NULLABLE): City name." in content


def test_view_document_includes_sql():
    view = _table(
        "crm", "active", [], table_type="VIEW", view_query="SELECT * FROM crm.customers\n"
    )
    content = table_to_row(view)["content"]
    assert content.startswith("BigQuery view `proj.crm.active`")
    assert "```sql\nSELECT * FROM crm.customers\n```" in content


def test_table_document_leaves_out_volatile_fields():
    # Row counts and modified times change on every load; keeping them out of the
    # text keeps the content hash (and so the cognee data_id) stable.
    table = _table("sales", "orders", ORDERS_SCHEMA, num_rows=10, modified=datetime.now(UTC))
    assert set(table_to_row(table)) == {"id", "title", "content", "url", "_deleted"}
    assert "10" not in table_to_row(table)["content"]


def test_format_value_types():
    assert format_value(datetime(2024, 1, 2, 3, 4, tzinfo=UTC)) == "2024-01-02T03:04:00+00:00"
    assert format_value(date(2024, 1, 2)) == "2024-01-02"
    assert format_value({"b": 1, "a": [2]}) == '{"a": [2], "b": 1}'
    assert format_value(b"\x00\x01") == "<2 bytes>"
    assert format_value(3.5) == "3.5"


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = bigquery_source(client=_orders_client())
    assert BIGQUERY_SOURCE_NAME == "bigquery"
    assert document_source_tag(source) == "bigquery"


def test_source_scopes_its_dlt_state_by_project():
    # cognee names the dlt pipeline from this scope (plus the cognee dataset), so
    # the cursor and known ids survive syncing other datasets on the same machine.
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, pipeline_name_for_source

    narrow = bigquery_source(client=_orders_client(), tables=["sales.orders"])
    wide = bigquery_source(client=_orders_client(), datasets=["sales"], row_syncs=[ORDERS_SYNC])
    # Same project → same state, whatever is selected, so narrowing the scope
    # tombstones what dropped out instead of orphaning it in another state.
    assert (
        getattr(narrow, PIPELINE_SCOPE_ATTR)
        == getattr(wide, PIPELINE_SCOPE_ATTR)
        == "bigquery:proj"
    )
    assert pipeline_name_for_source(narrow, "a") != pipeline_name_for_source(narrow, "b")
    assert pipeline_name_for_source(narrow, "a") != "ingest_dlt_source"  # not the shared one


# ---------------------------------------------------------------------------
# Metadata sync (DB-free)
# ---------------------------------------------------------------------------


def test_metadata_scope_lists_everything_by_default():
    client = FakeBigQueryClient(
        datasets={"a": _dataset("a"), "b": _dataset("b")},
        tables={"a.t1": _table("a", "t1", []), "b.t2": _table("b", "t2", [])},
    )
    ids = [row["id"] for row in sync_metadata(client, PROJECT, None, {})]
    assert ids == ["proj.a", "proj.a.t1", "proj.b", "proj.b.t2"]


def test_metadata_scope_combines_datasets_and_tables():
    client = FakeBigQueryClient(
        datasets={"a": _dataset("a"), "b": _dataset("b")},
        tables={
            "a.t1": _table("a", "t1", []),
            "b.t2": _table("b", "t2", []),
            "b.t3": _table("b", "t3", []),
        },
    )
    scope = _build_scope(["a"], ["b.t2"])
    assert scope == {"a": None, "b": ["t2"]}
    ids = [row["id"] for row in sync_metadata(client, PROJECT, scope, {})]
    assert ids == ["proj.a", "proj.a.t1", "proj.b", "proj.b.t2"]


def test_dropped_table_is_tombstoned_and_listed_tables_stay():
    client = _orders_client()
    client.tables["sales.returns"] = _table("sales", "returns", [])
    state = {}
    list(sync_metadata(client, PROJECT, None, state))

    del client.tables["sales.returns"]
    rows = list(sync_metadata(client, PROJECT, None, state))

    assert {"id": "proj.sales.returns", "_deleted": True} in rows
    assert "proj.sales.orders" in state["known_ids"]
    assert "proj.sales.returns" not in state["known_ids"]


def test_missing_configured_table_is_skipped_but_other_errors_abort():
    client = _orders_client()
    # A configured table that no longer exists is skipped (so it gets forgotten) ...
    ids = [r["id"] for r in sync_metadata(client, PROJECT, {"sales": ["gone", "orders"]}, {})]
    assert ids == ["proj.sales", "proj.sales.orders"]

    # ... but a permission error must abort the run instead of looking like a deletion.
    def forbidden(ref):
        raise Forbidden("no access")

    client.get_table = forbidden
    with pytest.raises(Forbidden):
        list(sync_metadata(client, PROJECT, None, {"known_ids": ["proj.sales.orders"]}))


def test_invalid_table_scope_is_rejected():
    with pytest.raises(ValueError, match=r"'dataset\.table'"):
        bigquery_source(client=_orders_client(), tables=["orders"])


# ---------------------------------------------------------------------------
# Row sync (DB-free)
# ---------------------------------------------------------------------------


def test_row_documents_render_selected_columns():
    client = _orders_client(rows=[_order(1, "shipped", 2)])
    spec = RowSync(table="sales.orders", key_column="order_id", columns=["status"])

    rows = list(sync_rows(client, PROJECT, [spec], 10**9, {}))

    assert rows == [
        {
            "id": "proj.sales.orders:1",
            "title": "orders 1",
            "content": (
                "Row of BigQuery table `proj.sales.orders` where order_id = 1.\n"
                "status: shipped\norder_id: 1"
            ),
            "_deleted": False,
        }
    ]
    # Only the requested columns (+ the key) are read, which keeps the bytes billed down.
    real_sql = [sql for sql, _, dry in client.queries if not dry]
    assert real_sql == ["SELECT `status`, `order_id` FROM `proj.sales.orders`"]


def test_cursor_limits_later_runs_to_changed_rows():
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    state = {}
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))
    table_state = state["tables"]["proj.sales.orders"]
    assert table_state["cursor"] == "2024-01-02T00:00:00+00:00"

    client.rows["sales.orders"] = [_order(1, "new", 1), _order(2, "shipped", 3)]
    client.queries.clear()
    rows = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    # Only order 2 changed, and the query asked only for rows at/after the cursor.
    assert [r["id"] for r in rows] == ["proj.sales.orders:2"]
    assert "status: shipped" in rows[0]["content"]
    incremental = [q for q in client.queries if not q[2] and "@since" in q[0]]
    assert incremental[0][1] == {"since": datetime(2024, 1, 2, tzinfo=UTC)}
    assert table_state["cursor"] == "2024-01-03T00:00:00+00:00"


def test_row_sharing_the_last_cursor_value_is_not_missed():
    # A row committed after the previous run but stamped with the same cursor
    # value must still be fetched; a strict > comparison would skip it forever.
    client = _orders_client(rows=[_order(1, "new", 2)])
    state = {}
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    client.rows["sales.orders"].append(_order(2, "new", 2))
    rows = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    assert "proj.sales.orders:2" in [r["id"] for r in rows]


def test_deleted_row_is_tombstoned_via_key_sweep():
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    state = {}
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    client.rows["sales.orders"] = [_order(2, "new", 2)]  # order 1 deleted upstream
    client.queries.clear()
    rows = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    assert {"id": "proj.sales.orders:1", "_deleted": True} in rows
    # The sweep reads only the key column.
    real_sql = [sql for sql, _, dry in client.queries if not dry]
    assert "SELECT `order_id` FROM `proj.sales.orders`" in real_sql
    assert state["tables"]["proj.sales.orders"]["keys"] == ["2"]


def test_dropped_row_table_forgets_all_its_rows():
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    state = {}
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    del client.tables["sales.orders"]
    rows = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    assert rows == [
        {"id": "proj.sales.orders:1", "_deleted": True},
        {"id": "proj.sales.orders:2", "_deleted": True},
    ]
    assert state["tables"]["proj.sales.orders"] == {}


def test_removed_row_sync_forgets_its_rows():
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    state = {}
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    rows = list(sync_rows(client, PROJECT, [], 10**9, state))  # RowSync removed

    assert rows == [
        {"id": "proj.sales.orders:1", "_deleted": True},
        {"id": "proj.sales.orders:2", "_deleted": True},
    ]
    assert state["tables"] == {}


def test_byte_cap_stops_before_running_the_query():
    client = _orders_client(rows=[_order(1, "new", 1)], bytes_per_query=5_000)

    with pytest.raises(ValueError, match="maximum_bytes_billed=1000"):
        list(sync_rows(client, PROJECT, [ORDERS_SYNC], 1_000, {}))

    # Only the dry run happened: nothing was billed.
    assert [dry for _, _, dry in client.queries] == [True]


def test_row_sync_rejects_unknown_columns_and_bad_cursor_types():
    client = _orders_client()
    with pytest.raises(ValueError, match="not in the schema"):
        list(sync_rows(client, PROJECT, [RowSync("sales.orders", "missing")], 10**9, {}))
    with pytest.raises(ValueError, match="must be TIMESTAMP"):
        bad_cursor = RowSync("sales.orders", "order_id", cursor_column="status")
        list(sync_rows(client, PROJECT, [bad_cursor], 10**9, {}))


# ---------------------------------------------------------------------------
# dlt pipeline: merge + _deleted hard delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, client, **kwargs):
    """Run bigquery_source the way cognee.remember(write_disposition="merge") does."""
    db_path = (tmp_path / "bigquery.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="bigquery_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="bigquery_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        bigquery_source(client=client, **kwargs), write_disposition="merge", primary_key="id"
    )
    return pipeline


def _read(pipeline, table):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_first_sync_loads_metadata_and_rows(dlt_mod, tmp_path):
    client = _orders_client(rows=[_order(1, "new", 1)])

    pipeline = _run_sync(dlt_mod, tmp_path, client, row_syncs=[ORDERS_SYNC])

    metadata = _read(pipeline, "bigquery_metadata")
    assert set(metadata) == {"proj.sales", "proj.sales.orders"}
    assert "Unique order id." in metadata["proj.sales.orders"]
    assert set(_read(pipeline, "bigquery_rows")) == {"proj.sales.orders:1"}


def test_description_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    client = _orders_client()
    _run_sync(dlt_mod, tmp_path, client)

    client.tables["sales.orders"].description = "Orders, including cancelled ones."
    pipeline = _run_sync(dlt_mod, tmp_path, client)

    content = _read(pipeline, "bigquery_metadata")["proj.sales.orders"]
    assert "Orders, including cancelled ones." in content
    assert "One row per order." not in content


def test_dropped_table_and_deleted_row_are_removed_on_resync(dlt_mod, tmp_path):
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    client.tables["sales.returns"] = _table("sales", "returns", [])
    _run_sync(dlt_mod, tmp_path, client, row_syncs=[ORDERS_SYNC])

    del client.tables["sales.returns"]
    client.rows["sales.orders"] = [_order(2, "new", 2)]
    pipeline = _run_sync(dlt_mod, tmp_path, client, row_syncs=[ORDERS_SYNC])

    # Tombstoned rows are hard-deleted by the merge, so cognee's read-back no
    # longer sees them and orphan cleanup forgets them.
    assert "proj.sales.returns" not in _read(pipeline, "bigquery_metadata")
    assert set(_read(pipeline, "bigquery_rows")) == {"proj.sales.orders:2"}
