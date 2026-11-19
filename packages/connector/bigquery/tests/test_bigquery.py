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
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import (
    Forbidden,
    InternalServerError,
    NotFound,
    RetryError,
    TooManyRequests,
    Unauthorized,
)
from google.cloud.bigquery import SchemaField

import cognee_community_connector_bigquery.bigquery as bq
from cognee_community_connector_bigquery.bigquery import (
    BIGQUERY_SOURCE_NAME,
    BigQuerySyncError,
    RowSync,
    _build_scope,
    bigquery_source,
    format_value,
    sync_metadata,
    sync_rows,
    table_to_row,
)

PROJECT = "proj"
METADATA, ROWS = "bigquery_proj_metadata", "bigquery_proj_rows"  # default table names

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

    def result(self, **kwargs):
        self.result_kwargs = kwargs
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
        self.call_kwargs = []  # (method, kwargs) as the connector passed them
        self.jobs = []  # the query jobs handed out
        self.closed = False

    def close(self):
        self.closed = True

    def list_datasets(self, project=None, **kwargs):
        self.call_kwargs.append(("list_datasets", kwargs))
        return [SimpleNamespace(dataset_id=d) for d in self.datasets]

    def get_dataset(self, ref, **kwargs):
        self.call_kwargs.append(("get_dataset", kwargs))
        dataset_id = ref.split(".")[-1]
        if dataset_id not in self.datasets:
            raise NotFound(f"Dataset {ref} not found")
        return self.datasets[dataset_id]

    def list_tables(self, ref, **kwargs):
        self.call_kwargs.append(("list_tables", kwargs))
        dataset_id = ref.split(".")[-1]
        return [
            SimpleNamespace(table_id=key.split(".")[1])
            for key in self.tables
            if key.startswith(f"{dataset_id}.")
        ]

    def get_table(self, ref, **kwargs):
        self.call_kwargs.append(("get_table", kwargs))
        key = ".".join(ref.split(".")[-2:])
        if key not in self.tables:
            raise NotFound(f"Table {ref} not found")
        return self.tables[key]

    def query(self, sql, job_config=None, **kwargs):
        self.call_kwargs.append(("query", kwargs))
        params = {p.name: p.value for p in job_config.query_parameters}
        self.queries.append((sql, params, bool(job_config.dry_run)))
        if job_config.dry_run:
            self.jobs.append(FakeQueryJob([], self.bytes_per_query))
            return self.jobs[-1]
        key = ".".join(re.search(r"FROM `([^`]+)`", sql).group(1).split(".")[-2:])
        rows = self.rows.get(key, [])
        where = re.search(r"WHERE `(\w+)` (>=|>) @since", sql)
        if where:
            column, op = where.groups()
            since = params["since"]
            rows = [r for r in rows if (r[column] >= since if op == ">=" else r[column] > since)]
        self.jobs.append(FakeQueryJob(rows, self.bytes_per_query))
        return self.jobs[-1]


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
        "updated_at": datetime(2024, 1, day, tzinfo=timezone.utc),
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
    table = _table(
        "sales", "orders", ORDERS_SCHEMA, num_rows=10, modified=datetime.now(timezone.utc)
    )
    assert set(table_to_row(table)) == {"id", "title", "content", "url", "_deleted"}
    assert "10" not in table_to_row(table)["content"]


def test_format_value_types():
    assert (
        format_value(datetime(2024, 1, 2, 3, 4, tzinfo=timezone.utc)) == "2024-01-02T03:04:00+00:00"
    )
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
    # Same project -> same state, whatever is selected, so narrowing the scope
    # tombstones what dropped out instead of orphaning it in another state. The
    # tables and the state scope share one name.
    assert getattr(narrow, PIPELINE_SCOPE_ATTR) == getattr(wide, PIPELINE_SCOPE_ATTR)
    assert getattr(narrow, PIPELINE_SCOPE_ATTR) == "bigquery_proj"
    assert list(narrow.resources) == [METADATA, ROWS]
    assert pipeline_name_for_source(narrow, "a") != pipeline_name_for_source(narrow, "b")
    assert pipeline_name_for_source(narrow, "a") != "ingest_dlt_source"  # not the shared one
    assert bq._default_resource_name("my-project-123") == "bigquery_my_project_123"
    named = bigquery_source(client=_orders_client(), resource_name="bq_prod")
    assert list(named.resources) == ["bq_prod_metadata", "bq_prod_rows"]


def test_source_validates_configuration():
    client = _orders_client()
    with pytest.raises(ValueError, match="datasets must be a list"):
        bigquery_source(client=client, datasets="sales")
    with pytest.raises(ValueError, match="tables must be a list"):
        bigquery_source(client=client, tables="sales.orders")
    with pytest.raises(ValueError, match="row_syncs must be a list"):
        bigquery_source(client=client, row_syncs=ORDERS_SYNC)
    with pytest.raises(ValueError, match="snake_case"):
        bigquery_source(client=client, resource_name="BigQuery-Prod")


def test_source_refuses_a_cognee_without_document_sync(monkeypatch):
    monkeypatch.delattr(bq.dlt_utils, "DOCUMENT_SYNC_VERSION")
    with pytest.raises(RuntimeError, match="document sync cleanup"):
        bigquery_source(client=_orders_client())


def test_every_call_and_query_job_gets_a_bounded_retry():
    fake = _orders_client(rows=[_order(1, "new", 1)])
    client = bq._CheckedClient(fake, None, {})
    list(sync_metadata(client, PROJECT, None, {}))
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, {}, {}))

    assert {method for method, _ in fake.call_kwargs} == {
        "list_datasets",
        "get_dataset",
        "list_tables",
        "get_table",
        "query",
    }
    for method, kwargs in fake.call_kwargs:
        assert kwargs["timeout"] == 60.0
        assert kwargs["retry"].timeout == 60.0  # the library default is 600 s
        if method == "query":
            assert kwargs["job_retry"].timeout == 60.0  # the library default is 2400 s
    assert fake.jobs[-1].result_kwargs["retry"].timeout == 60.0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (Unauthorized("bad credentials"), "failed_auth"),
        (Forbidden("Access Denied"), "failed_auth"),
        (Forbidden("Quota", errors=[{"reason": "rateLimitExceeded"}]), "failed_rate_limit"),
        (TooManyRequests("slow down"), "failed_rate_limit"),
        (RetryError("gave up", cause=TooManyRequests("slow down")), "failed_rate_limit"),
    ],
)
def test_auth_and_rate_limit_failures_are_counted(error, reason):
    fake = _orders_client()

    def fail(ref, **kwargs):
        raise error

    fake.get_table = fail
    stats = {}
    with pytest.raises(type(error)):
        list(sync_metadata(bq._CheckedClient(fake, None, stats), PROJECT, None, {}))
    other = {"failed_auth", "failed_rate_limit"} - {reason}
    assert stats[reason] == 1 and not any(stats.get(key) for key in other)


def test_a_broken_key_file_is_reported_without_its_contents(tmp_path):
    import traceback

    canary = "MIIEvLeakCanary0123456789"
    key = tmp_path / "key.json"
    key.write_text(f"private_key={canary}")  # not JSON
    with pytest.raises(ValueError, match="Could not load the service-account key") as failure:
        bq._make_client("proj", str(key))
    assert canary not in "".join(traceback.format_exception(failure.value))
    # The parse error keeps the file's text on the exception (JSONDecodeError.doc),
    # so it must not travel with the error a host may log or inspect.
    assert failure.value.__cause__ is None and failure.value.__suppress_context__


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
    def forbidden(ref, **kwargs):
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
    assert incremental[0][1] == {"since": datetime(2024, 1, 2, tzinfo=timezone.utc)}
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


def test_rows_with_a_null_key_are_skipped():
    client = _orders_client(rows=[_order(1, "new", 1), _order(None, "orphan", 2)])
    state = {}

    rows = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, state))

    assert [r["id"] for r in rows] == ["proj.sales.orders:1"]
    assert state["tables"]["proj.sales.orders"]["keys"] == ["1"]  # no "None" key tracked


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

    metadata = _read(pipeline, METADATA)
    assert set(metadata) == {"proj.sales", "proj.sales.orders"}
    assert "Unique order id." in metadata["proj.sales.orders"]
    assert set(_read(pipeline, ROWS)) == {"proj.sales.orders:1"}


def test_description_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    client = _orders_client()
    _run_sync(dlt_mod, tmp_path, client)

    client.tables["sales.orders"].description = "Orders, including cancelled ones."
    pipeline = _run_sync(dlt_mod, tmp_path, client)

    content = _read(pipeline, METADATA)["proj.sales.orders"]
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
    assert "proj.sales.returns" not in _read(pipeline, METADATA)
    assert set(_read(pipeline, ROWS)) == {"proj.sales.orders:2"}


# ---------------------------------------------------------------------------
# Host hooks and safety net
# ---------------------------------------------------------------------------
def test_service_and_client_are_one_client():
    with pytest.raises(ValueError, match="not both"):
        bigquery_source(service=_orders_client(), client=_orders_client())


def test_emptied_project_listing_forgets_nothing():
    client = _orders_client()
    state = {}
    list(sync_metadata(client, PROJECT, None, state))

    client.datasets = {}  # e.g. credentials for another project
    with pytest.raises(BigQuerySyncError, match="Refusing to forget"):
        list(sync_metadata(client, PROJECT, None, state))
    assert len(state["known_ids"]) == 2

    # Naming the selection explicitly forgets what is gone.
    rows = list(sync_metadata(client, PROJECT, {"sales": None}, state))
    assert len(rows) == 2 and all(row["_deleted"] for row in rows)


def test_unchanged_metadata_and_re_read_cursor_rows_are_not_re_emitted():
    client = _orders_client(rows=[_order(1, "new", 1), _order(2, "new", 2)])
    metadata, rows = {}, {}
    list(sync_metadata(client, PROJECT, None, metadata))
    list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, rows))
    # Only rows at the cursor value are re-read, so only their hashes are kept.
    assert set(rows["tables"]["proj.sales.orders"]["boundary"]) == {"2"}

    stats = {}
    assert list(sync_metadata(client, PROJECT, None, metadata, stats)) == []
    # >= reads order 2 (at the cursor) again; its hash shows it is unchanged.
    assert list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, rows, stats)) == []
    assert (stats["skipped"], stats["changed"]) == (3, 0)  # dataset, table, order 2

    # A row at the same cursor value that is new, or edited without the cursor moving.
    client.rows["sales.orders"].append(_order(3, "new", 2))
    client.rows["sales.orders"][1]["status"] = "shipped"
    emitted = list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, rows))
    assert [row["id"] for row in emitted] == ["proj.sales.orders:2", "proj.sales.orders:3"]

    client.tables["sales.orders"].description = "Orders, refunds included."
    assert [row["id"] for row in sync_metadata(client, PROJECT, None, metadata)] == [
        "proj.sales.orders"
    ]


def test_stats_count_rows_and_null_keys():
    rows = [_order(1, "new", 1), _order(None, "new", 1)]
    stats = {}
    list(sync_rows(_orders_client(rows=rows), PROJECT, [ORDERS_SYNC], 10**9, {}, stats))
    assert (stats["scanned"], stats["changed"], stats["failed"]) == (1, 1, 1)


def test_check_active_is_asked_per_call_and_per_row():
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) == 4:  # get_table, dry run, query; the first row is refused
            raise PermissionError("revoked")

    client = bq._CheckedClient(_orders_client(rows=[_order(1, "new", 1)]), check_active, {})
    with pytest.raises(PermissionError):
        list(sync_rows(client, PROJECT, [ORDERS_SYNC], 10**9, {}, {}, check_active))
    assert len(calls) == 4


def test_source_closes_the_clients_it_builds_and_reports_stats(dlt_mod, tmp_path, monkeypatch):
    built = []

    def make_client(project, credentials_path):
        built.append(_orders_client(rows=[_order(1, "new", 1)]))
        return built[-1]

    monkeypatch.setattr(bq, "_make_client", make_client)
    db_path = (tmp_path / "own.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="bigquery_own",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="bigquery_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    source = bigquery_source(row_syncs=[ORDERS_SYNC])

    pipeline.run(source, write_disposition="merge", primary_key="id")

    assert built and all(client.closed for client in built)  # probe, metadata and rows
    stats = source.cognee_sync_stats
    assert (stats["complete"], stats["changed"]) == (1, 3)  # dataset, table and one row
    assert stats["requests"] > 0


def test_complete_waits_for_metadata_and_rows():
    stats = {}
    run = bq._RunStats(stats, ("metadata", "rows"))
    run.start("metadata")["scanned"] = 2
    run.finish("metadata")
    assert (stats["scanned"], stats["complete"]) == (2, 0)

    run.start("rows")
    run.finish("rows")
    assert stats["complete"] == 1


def _pipeline(dlt, tmp_path, name):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
        ),
        dataset_name="bigquery_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def test_two_projects_in_one_dataset_keep_their_own_tables_and_state(dlt_mod, tmp_path):
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source

    first = _orders_client(rows=[_order(1, "new", 1)])
    second = _orders_client(rows=[_order(1, "new", 1)])
    second.project = "other"

    def sync(client):
        source = bigquery_source(client=client, row_syncs=[ORDERS_SYNC])
        # Name the pipeline as cognee does: one per (dataset, scope).
        pipeline = _pipeline(dlt_mod, tmp_path, pipeline_name_for_source(source, "bigquery"))
        pipeline.run(source, write_disposition="merge", primary_key="id")
        return pipeline

    sync(first)
    sync(second)
    first.rows["sales.orders"] = []  # the order is deleted in the first project only
    pipeline = sync(first)
    other = sync(second)

    assert _read(pipeline, ROWS) == {}
    assert set(_read(other, "bigquery_other_rows")) == {"other.sales.orders:1"}


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_the_key_never_reaches_errors_or_logs(
    dlt_mod, tmp_path, monkeypatch, caplog, capsys, status
):
    import json

    from dlt.pipeline.exceptions import PipelineStepFailed
    from google.cloud import bigquery

    canary = "MIIEvLeakCanary0123456789"
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account", "private_key": canary}))
    errors = {
        401: Unauthorized("Request had invalid authentication credentials."),
        403: Forbidden("Access Denied: Project proj"),
        404: NotFound("Not found: Project proj"),
        429: TooManyRequests("Rate limit exceeded"),
        500: InternalServerError("Backend error"),
    }
    fake = _orders_client()

    def fail(*args, **kwargs):
        raise errors[status]

    fake.list_datasets = fail
    seen = []
    monkeypatch.setattr(
        bigquery.Client,
        "from_service_account_json",
        classmethod(lambda cls, path, project=None: seen.append(path) or fake),
    )
    source = bigquery_source(project=PROJECT, credentials_path=str(key))

    with pytest.raises(PipelineStepFailed) as failure:
        _pipeline(dlt_mod, tmp_path, f"bigquery_leak_{status}").run(
            source, write_disposition="merge", primary_key="id"
        )

    chain, error = [], failure.value
    while error is not None:
        chain.append(f"{error!r} {error}")
        error = error.__cause__ or error.__context__
    output = capsys.readouterr()
    assert seen and canary not in " ".join(chain)
    assert canary not in caplog.text + output.out + output.err
    assert source.cognee_sync_stats["complete"] == 0
    assert source.cognee_sync_stats["failed_auth"] == (1 if status in (401, 403) else 0)
    assert source.cognee_sync_stats["failed_rate_limit"] == (1 if status == 429 else 0)


def test_control_characters_and_lone_surrogates_are_dropped(dlt_mod, tmp_path):
    client = _orders_client(rows=[_order(1, "ne\x00w\x1b", 1)])
    client.tables["sales.orders"].description = "One row \ud800per order."
    pipeline = _run_sync(dlt_mod, tmp_path, client, row_syncs=[ORDERS_SYNC])

    assert "One row per order." in _read(pipeline, METADATA)["proj.sales.orders"]
    row = _read(pipeline, ROWS)["proj.sales.orders:1"]
    assert "new" in row and "\x00" not in row and "\x1b" not in row
