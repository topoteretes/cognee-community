"""Unit tests for the ClickHouse connector (cognee_community_connector_clickhouse).

The ClickHouse wire protocol is fully mocked via ``FakeClickHouseClient`` — no
network traffic and no live server are required, so these run in CI. Coverage
mirrors the sibling Confluence connector's test suite:

  - full backfill yields every row and records the cursor + id set
  - incremental re-sync yields ONLY rows changed since the cursor
  - rows that vanish from the id sweep become hard-delete markers (forget-on-delete)
  - an empty sweep does not mass-delete and preserves prior state (transient-failure guard)
  - a row new to the corpus is ingested even if its cursor value is below the watermark
  - a real dlt merge removes the marked row (end-to-end forget-on-delete)

Plus a couple of cheap extras: the dlt resource is wired with merge + id PK +
the hard_delete column, and the (flagged-for-review) table-comments resource
produces the expected synthetic row.
"""

import re

import pytest

from cognee_community_connector_clickhouse.clickhouse import (
    clickhouse_source,
    sync_rows,
    sync_table_comments,
)

TABLE = "events"
ID_COLUMN = "id"
CURSOR_COLUMN = "updated_at"


# ---------------------------------------------------------------------------
# Fake ClickHouse client (stands in for clickhouse-connect's Client)
# ---------------------------------------------------------------------------
def _row(row_id, *, when, body=""):
    """Build a row as clickhouse-connect would hand it back: native types,
    ``id`` intentionally an int (not a str) to exercise the connector's
    str()-normalization of the merge key.
    """
    return {"id": row_id, "updated_at": when, "body": body}


class _Result:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeClickHouseClient:
    """Minimal stand-in for a ``clickhouse-connect`` client hitting one table."""

    def __init__(self, rows, *, table=TABLE, id_column=ID_COLUMN, cursor_column=CURSOR_COLUMN,
                 table_comment="", column_comments=None):
        self.rows = rows
        self.table = table
        self.id_column = id_column
        self.cursor_column = cursor_column
        self.table_comment = table_comment
        self.column_comments = column_comments or {}
        self.calls = []

    def _columns(self):
        return list(self.rows[0].keys()) if self.rows else [self.id_column, self.cursor_column]

    def _as_result(self, matched):
        cols = self._columns()
        return _Result(cols, [tuple(r[c] for c in cols) for r in matched])

    def query(self, sql, parameters=None):
        parameters = parameters or {}
        self.calls.append((sql, parameters))

        if f"SELECT `{self.id_column}` FROM `{self.table}`" in sql:
            return _Result([self.id_column], [(r[self.id_column],) for r in self.rows])

        if "system.tables" in sql:
            rows = [(self.table_comment,)] if self.table_comment else []
            return _Result(["comment"], rows)

        if "system.columns" in sql:
            return _Result(["name", "comment"], list(self.column_comments.items()))

        m = re.search(rf"WHERE `{self.id_column}` IN \(", sql)
        if m:
            wanted = set(parameters.values())
            matched = [r for r in self.rows if r[self.id_column] in wanted]
            return self._as_result(matched)

        if f"WHERE `{self.cursor_column}` > %(cursor)s" in sql:
            cursor = parameters["cursor"]
            matched = sorted(
                (r for r in self.rows if r[self.cursor_column] > cursor),
                key=lambda r: r[self.cursor_column],
            )
            return self._as_result(matched)

        if sql.startswith(f"SELECT * FROM `{self.table}` ORDER BY `{self.cursor_column}`"):
            matched = sorted(self.rows, key=lambda r: r[self.cursor_column])
            return self._as_result(matched)

        raise AssertionError(f"unexpected SQL: {sql!r} params={parameters!r}")


def _make_client(rows, **kwargs):
    return FakeClickHouseClient(rows, **kwargs)


def _sync(client, state):
    return list(
        sync_rows(client, state, table=TABLE, id_column=ID_COLUMN, cursor_column=CURSOR_COLUMN)
    )


# ---------------------------------------------------------------------------
# sync_rows — backfill / incremental / deletion
# ---------------------------------------------------------------------------
def test_backfill_yields_all_rows_and_records_cursor_and_ids():
    client = _make_client(
        [
            _row(1, when="2024-01-01T10:00:00.000Z", body="alpha"),
            _row(2, when="2024-01-02T10:00:00.000Z", body="beta"),
        ]
    )
    state = {}
    rows = _sync(client, state)

    assert {r["id"] for r in rows} == {"1", "2"}  # ids normalized to str
    assert all(r["_deleted"] is False for r in rows)
    assert {r["body"] for r in rows} == {"alpha", "beta"}
    # Cursor + id set captured for the next incremental run.
    assert state["last_cursor"] == "2024-01-02T10:00:00.000Z"
    assert state["known_ids"] == ["1", "2"]


def test_incremental_yields_only_rows_modified_since_cursor():
    client = _make_client(
        [
            _row(1, when="2024-01-01T10:00:00.000Z", body="old"),
            _row(2, when="2024-02-01T10:00:00.000Z", body="new"),
        ]
    )
    state = {"known_ids": ["1", "2"], "last_cursor": "2024-01-15T00:00:00.000Z"}
    rows = _sync(client, state)

    assert [r["id"] for r in rows] == ["2"]  # only the row newer than the cursor
    assert state["last_cursor"] == "2024-02-01T10:00:00.000Z"


def test_deleted_row_emits_hard_delete_marker():
    # Row "2" was known last run but is gone from the id sweep now.
    client = _make_client([_row(1, when="2024-01-01T10:00:00.000Z")])
    state = {"known_ids": ["1", "2"], "last_cursor": "2024-01-01T10:00:00.000Z"}
    rows = _sync(client, state)

    assert rows == [{"id": "2", "_deleted": True}]
    assert state["known_ids"] == ["1"]  # sweep now reflects reality


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    # A sweep that returns zero rows while rows were known is treated as a
    # transient failure, NOT "everything deleted" — otherwise a benign blip
    # would wipe the whole dataset and overwrite known_ids permanently.
    client = _make_client([])
    state = {"known_ids": ["1", "2"], "last_cursor": "2024-01-01T10:00:00.000Z"}
    rows = _sync(client, state)

    assert rows == []  # no hard-delete markers emitted
    assert state["known_ids"] == ["1", "2"]  # prior id set preserved


def test_new_row_below_cursor_is_still_ingested():
    # A row new to the corpus is fetched regardless of its cursor value
    # (backdated insert / restored / moved into scope), while an
    # already-known unchanged row at the same old cursor value is skipped.
    client = _make_client(
        [
            _row(1, when="2024-01-01T10:00:00.000Z", body="known"),  # old + known
            _row(3, when="2024-01-01T10:00:00.000Z", body="moved-in"),  # old + new
            _row(4, when="2024-06-01T00:00:00.000Z", body="tie"),  # boundary tie + new
        ]
    )
    state = {"known_ids": ["1"], "last_cursor": "2024-06-01T00:00:00.000Z"}
    rows = _sync(client, state)

    assert sorted(r["id"] for r in rows) == ["3", "4"]  # row "1" skipped, new rows ingested


# ---------------------------------------------------------------------------
# sync_table_comments — flagged-for-review resource
# ---------------------------------------------------------------------------
def test_sync_table_comments_yields_single_summarizing_row():
    client = _make_client(
        [],
        table_comment="Raw event stream",
        column_comments={"id": "Primary key", "updated_at": "Last write time"},
    )
    rows = list(sync_table_comments(client, database="analytics", table=TABLE))

    assert rows == [
        {
            "table": TABLE,
            "table_comment": "Raw event stream",
            "column_comments": {"id": "Primary key", "updated_at": "Last write time"},
        }
    ]


def test_sync_table_comments_yields_nothing_when_no_comments_exist():
    client = _make_client([])
    assert list(sync_table_comments(client, database="analytics", table=TABLE)) == []


# ---------------------------------------------------------------------------
# clickhouse_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_clickhouse_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    source = clickhouse_source(
        database="analytics",
        table=TABLE,
        cursor_column=CURSOR_COLUMN,
        client=_make_client([]),
    )
    resource = source.resources["clickhouse_rows"]

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_clickhouse_source_requires_table():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="table is required"):
        clickhouse_source(
            database="analytics", cursor_column=CURSOR_COLUMN, client=_make_client([])
        )


def test_clickhouse_source_requires_cursor_column():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="cursor_column is required"):
        clickhouse_source(database="analytics", table=TABLE, client=_make_client([]))


def test_clickhouse_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee\\[clickhouse\\]"):
        clickhouse_source(
            database="analytics", table=TABLE, cursor_column=CURSOR_COLUMN, client=object()
        )


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_clickhouse_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "clickhouse.duckdb")),
        dataset_name="events_db",
    )

    # Sync #1: two live rows land in the destination.
    client1 = _make_client(
        [
            _row(1, when="2024-01-01T10:00:00.000Z", body="a"),
            _row(2, when="2024-01-02T10:00:00.000Z", body="b"),
        ]
    )
    pipeline.run(
        clickhouse_source(
            database="analytics",
            table=TABLE,
            cursor_column=CURSOR_COLUMN,
            client=client1,
            include_table_comments=False,
        )
    )
    with pipeline.sql_client() as sql_client:
        assert sql_client.execute_sql("SELECT count(*) FROM clickhouse_rows")[0][0] == 2

    # Sync #2: row "2" deleted upstream, row "1" unchanged. The connector emits
    # a hard-delete marker for "2"; dlt's merge removes it from the destination.
    client2 = _make_client([_row(1, when="2024-01-01T10:00:00.000Z", body="a")])
    pipeline.run(
        clickhouse_source(
            database="analytics",
            table=TABLE,
            cursor_column=CURSOR_COLUMN,
            client=client2,
            include_table_comments=False,
        )
    )
    with pipeline.sql_client() as sql_client:
        rows = sql_client.execute_sql("SELECT id FROM clickhouse_rows")
    assert [r[0] for r in rows] == ["1"]  # row "2" forgotten, row "1" retained
