"""Unit tests for the ClickHouse dlt connector.

Two layers, all runnable in CI without a live ClickHouse server:

* DB-free tests for identifier validation, value rendering, row→id folding,
  key resolution, and the generic document DataItem tagging that routes rows
  through normal cognify.
* dlt-pipeline tests (a fake ``clickhouse_connect`` client, temp sqlite
  destination) covering the acceptance criteria: the incremental cursor emits
  only the delta, tied cursor values are never dropped, and a row that vanished
  upstream drops out of the merge (forget-on-delete).

The fake implements only the query shapes the connector issues, and asserts on the
SQL text, so a regression in the pushed-down predicate fails loudly instead of
silently reading the whole table.
"""

import datetime
import decimal
import uuid
from types import SimpleNamespace

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion layer
# (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_clickhouse.clickhouse import (
    CLICKHOUSE_SOURCE_NAME,
    _advance_cursor,
    _cursor_type,
    _id_from_key_value,
    _ident,
    _ident_list,
    _key_predicate,
    _row_key,
    _scalar,
    _selected_columns,
    clickhouse_source,
    sync_rows,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeResult:
    """What ``clickhouse_connect.Client.query`` returns."""

    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeTable:
    """An in-memory table with the metadata the connector introspects."""

    def __init__(self, columns, rows, *, primary_key=(), sorting_key=(), comment=""):
        self.columns = columns
        self.rows = rows
        self.primary_key = list(primary_key)
        self.sorting_key = list(sorting_key)
        self.comment = comment

    def column_type(self, name):
        for column, type_name in self.columns.items():
            if column == name:
                return type_name
        raise KeyError(name)


class FakeClickHouseClient:
    """Stand-in for ``clickhouse_connect.Client`` over in-memory tables.

    Implements the four query shapes the connector issues and records every SQL
    string it saw, so a test can assert the cursor really is pushed down.
    """

    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def _table(self, database, name):
        key = f"{database}.{name}"
        if key not in self.tables:
            raise AssertionError(f"unexpected table {key}")
        return self.tables[key]

    def query(self, sql, parameters=None):
        self.calls.append(sql)
        parameters = parameters or {}

        if "system.columns" in sql:
            if "is_in_primary_key = 1" in sql:
                marker = "is_in_primary_key"
            elif "is_in_sorting_key = 1" in sql:
                marker = "is_in_sorting_key"
            else:
                marker = None
            table = self._table(parameters["db"], parameters["tbl"])
            rows = [
                {"name": column, "type": type_name} for column, type_name in table.columns.items()
            ]
            if marker == "is_in_primary_key":
                rows = [row for row in rows if row["name"] in table.primary_key]
            elif marker == "is_in_sorting_key":
                rows = [row for row in rows if row["name"] in table.sorting_key]
            return FakeResult(["name", "type"], [[row["name"], row["type"]] for row in rows])

        if "system.tables" in sql:
            table = self._table(parameters["db"], parameters["tbl"])
            return FakeResult(["comment"], [[table.comment]])

        raise AssertionError(f"unexpected query: {sql}")

    def evaluate(self, resource, **kwargs):
        """Drive the resource body to exhaustion, returning what it yields.

        Column existence and key resolution need live ``system`` reads, so they can
        only happen when the resource is first evaluated. These tests assert the
        connector's own validation rather than dlt's, so they evaluate the body
        directly instead of standing up a pipeline.

        ``dlt.resource`` wraps the body in a pipe that re-raises a connector error as
        ``ResourceExtractionError``; the cause is unwrapped so a test can assert on
        the connector's own message.
        """
        import dlt

        # dlt only exposes the undecorated generator through the resource's pipe
        # machinery, which needs a real extraction context. Calling the underlying
        # function directly is what dlt itself does inside one.
        body = getattr(resource, "_obj", None) or resource
        try:
            return list(body(**kwargs))
        except dlt.extract.exceptions.ResourceExtractionError as exc:
            raise exc.__cause__ or exc from None

    def row_queries(self):
        """The SELECTs issued against real tables (not system tables)."""
        return [sql for sql in self.calls if "system." not in sql]


def _ts(text):
    """Parse a ``YYYY-MM-DD HH:MM:SS`` stamp the way clickhouse-connect returns one.

    Written out rather than inlined as ``datetime(...)`` because a bare date needs an
    explicit day, and these fixtures read far better as timestamps.
    """
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _events_table(rows=None, *, comment="Raw product analytics events."):
    return FakeTable(
        {
            "event_id": "UInt64",
            "kind": "LowCardinality(String)",
            "payload": "String",
            "tags": "Array(String)",
            "props": "Map(String, String)",
            "updated_at": "DateTime64(3)",
        },
        rows
        if rows is not None
        else [
            {
                "event_id": 1,
                "kind": "signup",
                "payload": "Alphacorp onboarded",
                "tags": ["alpha"],
                "props": {"plan": "enterprise"},
                "updated_at": _ts("2026-01-01 10:00:00"),
            },
            {
                "event_id": 2,
                "kind": "login",
                "payload": "Bravocorp 2FA failure",
                "tags": ["bravo"],
                "props": {"plan": "free"},
                "updated_at": _ts("2026-01-02 11:30:00"),
            },
        ],
        primary_key=["event_id"],
        comment=comment,
    )


def _config(table, *, database="analytics", name="events", cursor_column="updated_at"):
    """The per-table config ``clickhouse_source`` builds, for sync_rows directly."""
    return {
        f"{database}.{name}": {
            "database": database,
            "table": name,
            "key_columns": list(table.primary_key),
            "cursor_column": cursor_column,
            "cursor_sql_type": _cursor_type(table.column_type(cursor_column))
            if cursor_column
            else None,
            "columns": dict(table.columns),
            "comment": table.comment,
        }
    }


# ---------------------------------------------------------------------------
# Identifiers (the injection surface)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "events",
        "_private",
        "Events2",
        "my_table_1",
    ],
)
def test_ident_accepts_plain_names(name):
    assert _ident(name, kind="table") == name


@pytest.mark.parametrize(
    "name",
    [
        "events; DROP TABLE x",  # statement separator
        "events UNION ALL SELECT 1",
        "ev`ents",  # backtick quoting
        'ev"ents',  # double-quote quoting
        "ev ents",  # space
        "ev\nents",  # newline
        "2events",  # leading digit
        "",  # empty
        "events--x",  # comment marker
        "events/*x*/",
        "../../etc/passwd",  # path traversal
        "events\x00",  # NUL byte
        None,  # wrong type
        123,  # wrong type
    ],
)
def test_ident_rejects_anything_outside_the_allowlist(name):
    with pytest.raises(ValueError, match="Unsafe ClickHouse table name"):
        _ident(name, kind="table")


def test_ident_error_names_the_kind():
    with pytest.raises(ValueError, match="Unsafe ClickHouse column name"):
        _ident("bad name", kind="column")


def test_ident_list_wraps_a_bare_string_and_preserves_order():
    assert _ident_list("event_id", kind="column") == ["event_id"]
    assert _ident_list(["b", "a"], kind="column") == ["b", "a"]


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------


def test_scalar_renders_the_types_clickhouse_hands_back():
    assert _scalar("text") == "text"
    assert _scalar(42) == "42"
    assert _scalar(4.5) == "4.5"
    assert _scalar(True) == "true"
    assert _scalar(False) == "false"
    assert _scalar(None) == ""  # NULL is empty, not the string "None"
    assert _scalar(decimal.Decimal("1.25")) == "1.25"
    assert _scalar(uuid.UUID(int=1)) == str(uuid.UUID(int=1))
    assert _scalar(_ts("2026-01-01 00:00:00")) == "2026-01-01 00:00:00"
    assert _scalar(b"raw") == "raw"
    assert _scalar(b"\xff\xfe") == "\ufffd\ufffd"  # undecodable bytes, not a crash


def test_scalar_renders_containers_as_json_not_python_repr():
    # JSON is machine-shaped, which is what cognee cognifies. Python repr with
    # single quotes is neither valid JSON nor stable across types.
    assert _scalar({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'  # key-sorted
    assert _scalar(["x", "y"]) == '["x", "y"]'


def test_row_key_is_readable_for_one_column_and_unambiguous_for_several():
    assert _row_key({"event_id": 7}, ["event_id"]) == "7"
    # Concatenation would make these two collide; a JSON array does not.
    assert _row_key({"a": "ab", "b": "c"}, ["a", "b"]) != _row_key(
        {"a": "a", "b": "bc"}, ["a", "b"]
    )


def test_row_key_folds_a_composite_key_deterministically():
    first = _row_key({"tenant": "acme", "user": "u1"}, ["tenant", "user"])
    second = _row_key({"tenant": "acme", "user": "u1"}, ["tenant", "user"])
    assert first == second
    assert "acme" in first and "u1" in first


def test_id_from_key_value_matches_the_row_id_a_full_read_produces():
    # The sweep returns bare keys; they must fold to the same id the row read
    # produces, or every row would look new to the corpus on every sync.
    from_row = _id_from_key_value("analytics", "events", ["event_id"], 5)
    rendered = SimpleNamespace()
    del rendered
    from cognee_community_connector_clickhouse.clickhouse import _row_id

    assert from_row == _row_id("analytics", "events", {"event_id": 5}, ["event_id"])
    assert from_row == "analytics.events:5"


def test_ids_are_namespaced_by_table_so_two_tables_cannot_collide():
    left = _id_from_key_value("analytics", "events", ["event_id"], 1)
    right = _id_from_key_value("analytics", "users", ["event_id"], 1)
    assert left != right


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_advance_cursor_keeps_the_larger_value():
    assert _advance_cursor(10, 20) == 20
    assert _advance_cursor(20, 10) == 20
    assert _advance_cursor(None, 5) == 5
    assert _advance_cursor(5, None) == 5  # a NULL cursor must not rewind the mark


def test_advance_cursor_survives_incomparable_types():
    # A mixed-type cursor column must not abort the sync mid-stream; keeping the
    # old mark just re-reads a wider window next run.
    assert _advance_cursor(10, "2026-01-01") == 10
    assert _advance_cursor("2026-01-01", 10) == "2026-01-01"


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        ("DateTime64(3)", "DateTime64"),
        ("DateTime64", "DateTime64"),
        ("DateTime", "DateTime"),
        ("Date", "Date"),
        ("UInt64", "UInt64"),
        ("Int32", "Int32"),
        ("Decimal(18, 4)", "Decimal"),
        ("String", "String"),
        # A LowCardinality wrapper decorates the scalar without changing its order,
        # so the high-water mark belongs to the String underneath.
        ("LowCardinality(String)", "String"),
    ],
)
def test_cursor_type_accepts_scalars_and_strips_parameters(column_type, expected):
    assert _cursor_type(column_type) == expected


@pytest.mark.parametrize(
    "column_type",
    [
        "Array(String)",
        "Map(String, String)",
        "Tuple(UInt8, String)",
        # Nullable has no single maximum, so a NULL row could never be compared
        # against the mark — every such row would be dropped after the first sync.
        "Nullable(String)",
        "Nullable(DateTime64(3))",
    ],
)
def test_cursor_type_rejects_types_with_no_total_order(column_type):
    with pytest.raises(ValueError, match="not supported"):
        _cursor_type(column_type)


def test_selected_columns_forces_the_key_and_cursor():
    # Without these the connector cannot build an id or move the high-water mark,
    # so the delta would grow without bound every run.
    assert _selected_columns(None, ["event_id", "updated_at"]) == ["event_id", "updated_at"]
    assert _selected_columns(["payload"], ["event_id", "updated_at"]) == [
        "event_id",
        "updated_at",
        "payload",
    ]
    # Naming a required column explicitly does not duplicate it.
    assert _selected_columns(["event_id"], ["event_id"]) == ["event_id"]


# ---------------------------------------------------------------------------
# Key predicates
# ---------------------------------------------------------------------------


def test_key_predicate_binds_every_value_under_its_own_parameter_name():
    # Repeating one placeholder per value would bind them all to the same value and
    # collapse the predicate to a single key.
    # ClickHouse only accepts the tuple form for two or more columns, so a
    # single-column key must use the flat form.
    sql, parameters = _key_predicate(["event_id"], {"event_id": "UInt64"}, [1, 2, 3])
    assert (
        sql == "event_id IN ({k_event_id_0:UInt64}, {k_event_id_1:UInt64}, {k_event_id_2:UInt64})"
    )
    assert parameters == {"k_event_id_0": 1, "k_event_id_1": 2, "k_event_id_2": 3}
    assert len(set(parameters.values())) == 3


def test_key_predicate_uses_tuple_form_for_a_composite_key():
    sql, parameters = _key_predicate(
        ["tenant_id", "user_id"],
        {"tenant_id": "String", "user_id": "String"},
        [["acme", "u1"], ["globex", "u2"]],
    )
    assert sql.startswith("(tenant_id, user_id) IN ((")
    assert parameters["k_tenant_id_0"] == "acme"
    assert parameters["k_user_id_0"] == "u1"
    assert parameters["k_tenant_id_1"] == "globex"
    assert parameters["k_user_id_1"] == "u2"


def test_key_predicate_binds_with_the_real_column_type_not_a_string():
    # Binding an Int64 key as a String would make the server compare like with like
    # only by coercion.
    _, parameters = _key_predicate(["n"], {"n": "UInt64"}, [7])
    assert parameters == {"k_n_0": 7}


# ---------------------------------------------------------------------------
# Row → document DataItem (DB-free)
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_the_source_as_clickhouse():
    row = SimpleNamespace(
        row_data={
            "id": "analytics.events:1",
            "database": "analytics",
            "table": "events",
            "title": "analytics.events 1",
            "content": "Table: analytics.events\nComment: Raw events.\n\nRow Data:\n  kind: signup",
        },
        content_hash="abc123",
        table_name="clickhouse_rows",
    )

    item = _build_document_data_item(row, uuid.uuid4(), CLICKHOUSE_SOURCE_NAME)

    # source="clickhouse" (not "dlt") is what routes the row through normal cognify.
    assert item.system_metadata["source"] == "clickhouse"
    assert item.system_metadata["external_id"] == "analytics.events:1"
    assert item.system_metadata["table_name"] == "clickhouse_rows"
    assert item.data.startswith("# analytics.events 1")
    assert "Raw events." in item.data


def test_build_document_data_item_handles_an_untitled_row():
    row = SimpleNamespace(
        row_data={"id": "analytics.events:2", "title": "", "content": "just a payload"},
        content_hash="abc",
        table_name="clickhouse_rows",
    )
    item = _build_document_data_item(row, uuid.uuid4(), CLICKHOUSE_SOURCE_NAME)
    assert item.data == "just a payload"


# ---------------------------------------------------------------------------
# sync_rows — backfill / incremental / deletion (fake client, no dlt)
# ---------------------------------------------------------------------------


class FakeQueryClient(FakeClickHouseClient):
    """A fake that also answers the row SELECTs the connector issues."""

    def query(self, sql, parameters=None):
        self.calls.append(sql)
        parameters = parameters or {}

        if "system." in sql:
            return super().query(sql, parameters)

        body = sql[len("SELECT ") :]
        select_part, _, rest = body.partition(" FROM ")
        database, _, remainder = rest.partition(".")
        table_name, _, tail = remainder.partition(" ")
        table = self._table(database, table_name)
        columns = [column.strip() for column in select_part.split(",")]

        rows = list(table.rows)
        tail = tail.strip()
        if tail.startswith("WHERE "):
            clause = tail[len("WHERE ") :].split(" ORDER BY ")[0]
            rows = [row for row in rows if self._matches(row, clause, parameters)]
        order = tail.split(" ORDER BY ")[1] if "ORDER BY" in tail else None
        if order:
            column = order.replace(" ASC", "").strip()
            rows = sorted(rows, key=lambda row: row[column])

        return FakeResult(columns, [[row.get(column) for column in columns] for row in rows])

    def _matches(self, row, clause, parameters):
        for term in _split_conjuncts(clause):
            if ">=" in term and "{cursor:" in term:
                column = term.split(">=")[0].strip()
                value = row.get(column)
                # A NULL cursor can never satisfy >=, which is why the connector
                # rejects a nullable cursor column outright.
                if value is None or value < parameters["cursor"]:
                    return False
            elif " IN (" in term:
                cells = _in_columns(term)
                if not cells:
                    return False
                # Each bound group is one tuple; the row matches if it equals any.
                groups = {}
                for name, value in parameters.items():
                    if not name.startswith("k_"):
                        continue
                    column, _, index = name[2:].rpartition("_")
                    groups.setdefault(int(index), {})[column] = value
                if not groups:
                    return False
                actual = tuple(row.get(cell) for cell in cells)
                if actual not in {
                    tuple(values[cell] for cell in cells) for values in groups.values()
                }:
                    return False
            elif "=" in term:
                # The connector wraps a caller's predicate in parens, and a string
                # literal can itself contain parens, so strip only a balanced outer
                # pair rather than every leading/trailing bracket.
                column, _, expected = _strip_outer_parens(term).partition("=")
                if str(row.get(column.strip())) != expected.strip().strip("'\""):
                    return False
            else:
                raise AssertionError(f"unmodelled predicate: {term!r}")
        return True


def _split_conjuncts(clause):
    """Split a WHERE clause on AND, respecting the parentheses around a tuple IN."""
    terms = []
    current = ""
    depth = 0
    for char in clause:
        current += char
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and current.endswith(" AND "):
            terms.append(current[: -len(" AND ")])
            current = ""
    if current.strip():
        terms.append(current)
    return [term.strip() for term in terms if term.strip()]


def _strip_outer_parens(text):
    """Remove one balanced outer ``(...)`` pair, if the text is exactly wrapped."""
    if not (text.startswith("(") and text.endswith(")")):
        return text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                # The closing paren ends the wrapper, not an inner group.
                return text[1:index] if index == len(text) - 1 else text
    return text


def _in_columns(term):
    """Return the key columns named by an IN predicate."""
    left = term.split(" IN ")[0].strip()
    if left.startswith("(") and left.endswith(")"):
        return [column.strip() for column in left[1:-1].split(",")]
    return [left]


def _run(client, state, **kwargs):
    return list(
        sync_rows(client, state, tables=_config(client.tables["analytics.events"]), **kwargs)
    )


def test_backfill_yields_every_row_and_records_cursor_and_keys():
    client = FakeQueryClient({"analytics.events": _events_table()})
    state = {}

    rows = _run(client, state)

    assert [row["id"] for row in rows] == ["analytics.events:1", "analytics.events:2"]
    assert all(row["_deleted"] is False for row in rows)
    # The high-water mark and the key set are captured for the next run.
    assert state["cursors"]["analytics.events"] == _ts("2026-01-02 11:30:00")
    assert state["known_ids"] == ["analytics.events:1", "analytics.events:2"]


def test_row_content_carries_the_table_comment_columns_and_values():
    client = FakeQueryClient({"analytics.events": _events_table()})

    row = _run(client, {})[0]

    assert "Table: analytics.events" in row["content"]
    assert "Comment: Raw product analytics events." in row["content"]
    assert "event_id: UInt64" in row["content"]  # column list
    assert "payload: Alphacorp onboarded" in row["content"]  # row values
    # A List(String) column renders as JSON, not a Python repr with single quotes.
    assert 'tags: ["alpha"]' in row["content"]
    assert 'props: {"plan": "enterprise"}' in row["content"]
    assert row["title"] == "analytics.events 1"


def test_incremental_yields_only_rows_at_or_after_the_cursor():
    client = FakeQueryClient({"analytics.events": _events_table()})
    # Both existing rows are already known, so only the cursor pass reports changes.
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }
    client.tables["analytics.events"].rows.append(
        {
            "event_id": 3,
            "kind": "purchase",
            "payload": "Alphacorp upgraded",
            "tags": [],
            "props": {},
            "updated_at": _ts("2026-01-05 00:00:00"),
        }
    )

    rows = _run(client, state)

    # Event 2 sits exactly on the cursor, so >= re-reads it (deduped by id, and its
    # values are unchanged so nothing is re-cognified downstream). Event 1 is below
    # the mark and already known, so it is not re-ingested. Event 3 is new.
    assert [row["id"] for row in rows if not row["_deleted"]] == [
        "analytics.events:2",
        "analytics.events:3",
    ]


def test_incremental_pushes_the_cursor_down_into_the_sql():
    client = FakeQueryClient({"analytics.events": _events_table()})
    state = {"cursors": {"analytics.events": _ts("2026-01-02 00:00:00")}, "known_ids": []}

    _run(client, state)

    delta_sql = next(sql for sql in client.row_queries() if "{cursor:" in sql)
    assert "updated_at >= {cursor:DateTime64}" in delta_sql
    assert "ORDER BY updated_at ASC" in delta_sql


def test_tied_cursor_values_are_all_ingested():
    # The reason the cursor uses >= and not > : three rows share version 7, and a
    # strict > against a mark of 7 would drop every one of them.
    batched = FakeTable(
        {"batch_id": "UInt64", "seq": "UInt64", "note": "String", "version": "UInt64"},
        [
            {"batch_id": 1, "seq": 1, "note": "first", "version": 7},
            {"batch_id": 1, "seq": 2, "note": "second", "version": 7},
            {"batch_id": 1, "seq": 3, "note": "third", "version": 7},
        ],
        primary_key=["batch_id", "seq"],
        comment="A batch load.",
    )
    client = FakeQueryClient({"analytics.batched": batched})
    state = {
        "cursors": {"analytics.batched": 7},
        "known_ids": ["analytics.batched:[1, 1]", "analytics.batched:[1, 2]"],
    }

    rows = list(
        sync_rows(
            client,
            state,
            tables=_config(batched, name="batched", cursor_column="version"),
        )
    )

    # All three rows carry version 7, so a strict `>` against a mark of 7 would
    # return none of them. All three come back: seq 3 because it is genuinely new,
    # seq 1 and 2 because they sit on the boundary. The two known rows are unchanged,
    # so their content hash — and therefore their data_id — is unchanged and cognee
    # does not re-embed or re-cognify them.
    assert [row["id"] for row in rows if not row["_deleted"]] == [
        "analytics.batched:[1, 1]",
        "analytics.batched:[1, 2]",
        "analytics.batched:[1, 3]",
    ]
    assert state["cursors"]["analytics.batched"] == 7
    # The tie does not move the mark backwards or forwards past the shared value.
    assert all(row["_deleted"] is False for row in rows)


def test_a_row_is_not_emitted_twice_in_one_run():
    client = FakeQueryClient({"analytics.events": _events_table()})
    # The cursor pass and the back-dated pass can both return the same row.
    state = {
        "cursors": {"analytics.events": _ts("2026-01-01 00:00:00")},
        "known_ids": ["analytics.events:1"],
    }

    rows = _run(client, state)

    ids = [row["id"] for row in rows if not row["_deleted"]]
    assert len(ids) == len(set(ids))


def test_a_row_new_to_the_corpus_is_ingested_despite_an_old_cursor():
    # A back-dated insert (a late batch load) carries a cursor below the mark, so
    # the cursor pass cannot see it. The key sweep has to.
    table = _events_table()
    table.rows.append(
        {
            "event_id": 3,
            "kind": "backfill",
            "payload": "arrived late",
            "tags": [],
            "props": {},
            "updated_at": _ts("2025-01-01 00:00:00"),
        }
    )
    client = FakeQueryClient({"analytics.events": table})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }

    rows = _run(client, state)

    assert "analytics.events:3" in [row["id"] for row in rows if not row["_deleted"]]
    assert state["known_ids"] == [
        "analytics.events:1",
        "analytics.events:2",
        "analytics.events:3",
    ]


def test_a_vanished_row_becomes_a_hard_delete_marker():
    table = _events_table()
    table.rows = [table.rows[0]]  # event 2 deleted upstream
    client = FakeQueryClient({"analytics.events": table})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }

    rows = _run(client, state)

    assert rows == [{"id": "analytics.events:2", "_deleted": True}]
    assert state["known_ids"] == ["analytics.events:1"]


def test_an_empty_sweep_does_not_mass_delete_and_preserves_state():
    # A sweep that returns nothing while rows were known is a transient failure
    # (dropped connection, renamed database), not a wipe. Purging here would make
    # the loss permanent by also overwriting the key state.
    table = _events_table()
    table.rows = []
    client = FakeQueryClient({"analytics.events": table})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }

    rows = _run(client, state)

    assert rows == []
    assert state["known_ids"] == ["analytics.events:1", "analytics.events:2"]


def test_deletion_detection_can_be_turned_off():
    table = _events_table()
    table.rows = [table.rows[0]]
    client = FakeQueryClient({"analytics.events": table})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }

    rows = _run(client, state, detect_deletions=False)

    # No key sweep runs, so no delete marker is emitted for the vanished row 2 and
    # the stale key stays in state — which is the documented cost of turning the
    # sweep off.
    assert rows == []
    assert state["known_ids"] == ["analytics.events:1", "analytics.events:2"]


def test_deletion_detection_off_still_ingests_a_changed_row():
    table = _events_table()
    table.rows = [table.rows[0]]
    client = FakeQueryClient({"analytics.events": table})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-01 10:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2"],
    }

    rows = _run(client, state, detect_deletions=False)

    assert [row["id"] for row in rows] == ["analytics.events:1"]


def test_the_key_sweep_selects_only_the_key_columns():
    # A full-column sweep would read the whole table every run; the key-only sweep
    # is answerable from the primary index.
    client = FakeQueryClient({"analytics.events": _events_table()})

    _run(client, {})

    sweep = client.row_queries()[0]
    assert sweep.startswith("SELECT event_id FROM analytics.events")
    assert "payload" not in sweep


def test_two_tables_are_synced_with_independent_cursors():
    events = _events_table()
    users = FakeTable(
        {"tenant_id": "String", "user_id": "String", "email": "String"},
        [{"tenant_id": "acme", "user_id": "u1", "email": "ada@acme.example"}],
        primary_key=["tenant_id", "user_id"],
        comment="Users.",
    )
    client = FakeQueryClient({"analytics.events": events, "analytics.users": users})
    state = {}

    tables = {**_config(events), **_config(users, name="users", cursor_column=None)}
    rows = list(sync_rows(client, state, tables=tables))

    live = [row["id"] for row in rows if not row["_deleted"]]
    assert "analytics.events:1" in live
    assert 'analytics.users:["acme", "u1"]' in live
    # A table with no cursor keeps no mark.
    assert "analytics.users" not in state["cursors"]
    assert state["cursors"]["analytics.events"] == _ts("2026-01-02 11:30:00")


def test_a_deleted_row_in_one_table_does_not_touch_the_other():
    events = _events_table()
    events.rows = [events.rows[0]]
    users = FakeTable(
        {"tenant_id": "String", "user_id": "String"},
        [{"tenant_id": "acme", "user_id": "u1"}],
        primary_key=["tenant_id", "user_id"],
    )
    client = FakeQueryClient({"analytics.events": events, "analytics.users": users})
    state = {
        "cursors": {"analytics.events": _ts("2026-01-02 00:00:00")},
        "known_ids": ["analytics.events:1", "analytics.events:2", 'analytics.users:["acme", "u1"]'],
    }

    tables = {**_config(events), **_config(users, name="users", cursor_column=None)}
    rows = list(sync_rows(client, state, tables=tables))

    deleted = [row["id"] for row in rows if row["_deleted"]]
    assert deleted == ["analytics.events:2"]
    assert 'analytics.users:["acme", "u1"]' in state["known_ids"]


def test_where_narrows_the_reads_and_the_sweep():
    table = _events_table()
    table.rows.append(
        {
            "event_id": 3,
            "kind": "signup",
            "payload": "test env",
            "tags": [],
            "props": {},
            "updated_at": _ts("2026-01-09 00:00:00"),
        }
    )
    table.rows.append(
        {
            "event_id": 4,
            "kind": "signup",
            "payload": "prod env",
            "tags": [],
            "props": {},
            "updated_at": _ts("2026-01-09 00:00:00"),
        }
    )
    client = FakeQueryClient({"analytics.events": table})

    rows = list(sync_rows(client, {}, tables=_config(table), where="payload = 'prod env'"))

    assert [row["id"] for row in rows if not row["_deleted"]] == ["analytics.events:4"]
    # The sweep is narrowed too, so a row the filter excludes is treated as absent —
    # which is what makes a soft-delete flag ("env = 'prod'") forget properly.
    assert all("(payload = 'prod env')" in sql for sql in client.row_queries())


# ---------------------------------------------------------------------------
# Source factory — dlt wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_resource_is_wired_for_merge_with_a_hard_delete_column(dlt_mod):
    client = FakeQueryClient({"analytics.events": _events_table()})
    resource = clickhouse_source(database="analytics", tables=["events"], client=client)

    assert resource.name == "clickhouse_rows"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_resource_is_marked_for_the_document_ingestion_path(dlt_mod):
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, document_source_tag

    client = FakeClickHouseClient({"analytics.events": _events_table()})
    resource = clickhouse_source(database="analytics", tables=["events"], client=client)

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == CLICKHOUSE_SOURCE_NAME
    assert document_source_tag(resource) == "clickhouse"


def test_resource_state_is_scoped_to_the_connection(dlt_mod):
    # Two ClickHouse sources in one cognee install must not drive each other's
    # cursor and key set through a shared dlt pipeline state directory.
    client = FakeClickHouseClient({"analytics.events": _events_table()})
    resource = clickhouse_source(
        host="ch.internal", port=8123, database="analytics", tables=["events"], client=client
    )
    scope = resource.cognee_pipeline_scope

    assert "clickhouse:" in scope
    assert "ch.internal" in scope
    assert "analytics" in scope


def test_source_requires_a_host_when_no_client_is_injected(dlt_mod, monkeypatch):
    # Cleared explicitly: the live suite sets CLICKHOUSE_HOST, and this asserts the
    # connector's own requirement rather than the environment's.
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    with pytest.raises(ValueError, match="host required"):
        clickhouse_source(tables=["events"])


def test_source_requires_tables(dlt_mod):
    client = FakeClickHouseClient({})
    with pytest.raises(ValueError, match="tables= is required"):
        clickhouse_source(database="analytics", client=client)


def test_a_bare_table_name_without_a_database_is_rejected(dlt_mod):
    client = FakeClickHouseClient({})
    with pytest.raises(ValueError, match="no database"):
        clickhouse_source(tables=["events"], client=client)


def test_a_hostile_table_name_is_rejected_before_any_query(dlt_mod):
    client = FakeClickHouseClient({"analytics.events": _events_table()})
    with pytest.raises(ValueError, match="Unsafe ClickHouse table name"):
        clickhouse_source(database="analytics", tables=["events; DROP TABLE x"], client=client)
    assert client.calls == []  # nothing was sent


def _evaluate(client, **kwargs):
    """Evaluate the resource body, returning the rows it yields."""
    resource = clickhouse_source(**kwargs, client=client)
    return client.evaluate(resource)


def test_a_table_without_columns_is_reported_clearly(dlt_mod):
    client = FakeQueryClient({"analytics.events": FakeTable({}, [])})
    with pytest.raises(ValueError, match="has no columns"):
        _evaluate(client, database="analytics", tables=["events"])


def test_an_unknown_key_column_is_rejected(dlt_mod):
    client = FakeQueryClient({"analytics.events": _events_table()})
    with pytest.raises(ValueError, match="Key column 'nope' is not a column"):
        _evaluate(client, database="analytics", tables=["events"], key_columns={"events": "nope"})


def test_an_unknown_cursor_column_is_rejected(dlt_mod):
    client = FakeQueryClient({"analytics.events": _events_table()})
    with pytest.raises(ValueError, match="Cursor column 'nope' is not a column"):
        _evaluate(
            client, database="analytics", tables=["events"], cursor_columns={"events": "nope"}
        )


def test_an_unsupported_cursor_type_is_rejected(dlt_mod):
    client = FakeQueryClient({"analytics.events": _events_table()})
    with pytest.raises(ValueError, match="not supported"):
        # payload is String, but a cursor must have a total order; use a table whose
        # declared cursor column is an Array to prove the type gate fires.
        _evaluate(
            client,
            database="analytics",
            tables=["events"],
            cursor_columns={"events": "tags"},
        )


def test_an_unknown_projection_column_is_rejected(dlt_mod):
    client = FakeQueryClient({"analytics.events": _events_table()})
    with pytest.raises(ValueError, match="Column 'nope' is not a column"):
        _evaluate(client, database="analytics", tables=["events"], columns=["nope"])


def test_a_table_with_no_usable_key_is_reported_clearly(dlt_mod):
    keyless = FakeTable({"a": "String", "b": "String"}, [])
    client = FakeQueryClient({"analytics.keyless": keyless})
    with pytest.raises(ValueError, match="no usable row key"):
        _evaluate(client, database="analytics", tables=["keyless"])


def test_the_key_falls_back_to_the_sorting_key_then_to_id(dlt_mod):
    # No PRIMARY KEY declared, so the sorting key — ClickHouse's closest analogue,
    # and what the engine reads first — identifies the row.
    sorting_only = FakeTable(
        {"sku": "String", "qty": "UInt32"},
        [{"sku": "widget", "qty": 3}],
        sorting_key=["sku"],
        comment="",
    )
    client = FakeQueryClient({"analytics.sorting_only": sorting_only})

    rows = _evaluate(client, database="analytics", tables=["sorting_only"])

    assert [row["id"] for row in rows] == ["analytics.sorting_only:widget"]


def test_a_declared_key_overrides_the_inferred_one(dlt_mod):
    table = _events_table()
    client = FakeQueryClient({"analytics.events": table})

    rows = _evaluate(
        client, database="analytics", tables=["events"], key_columns={"events": "kind"}
    )

    # event 1 and 2 both have kind signup/login, so keying on kind is honoured as
    # asked — the caller decides identity, not the connector.
    assert [row["id"] for row in rows] == ["analytics.events:signup", "analytics.events:login"]


def test_the_source_reads_the_host_from_the_environment(dlt_mod, monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.internal")
    client = FakeClickHouseClient({"analytics.events": _events_table()})
    # No host= and no CLICKHOUSE_HOST problem: the env value satisfies the check.
    resource = clickhouse_source(database="analytics", tables=["events"], client=client)
    assert "ch.internal" in resource.cognee_pipeline_scope


def test_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    client = FakeClickHouseClient({"analytics.events": _events_table()})
    with pytest.raises(ImportError, match="cognee-community-connector-clickhouse"):
        clickhouse_source(database="analytics", tables=["events"], client=client)


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------


def _run_pipeline(dlt_mod, tmp_path, client, pipeline_name="clickhouse_test"):
    pipeline = dlt_mod.pipeline(
        pipeline_name=pipeline_name,
        destination=dlt_mod.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickhouse.db').as_posix()}"
        ),
        dataset_name="clickhouse_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        clickhouse_source(
            database="analytics",
            tables=["events"],
            key_columns={"events": "event_id"},
            cursor_columns={"events": "updated_at"},
            client=client,
        )
    )
    return pipeline


def _read_ids(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM clickhouse_rows WHERE _deleted IS NOT TRUE") as cursor,
    ):
        return sorted(row[0] for row in cursor.fetchall())


def test_sync_pipeline_loads_rows_and_the_table_comment(dlt_mod, tmp_path):
    client = FakeQueryClient({"analytics.events": _events_table()})

    pipeline = _run_pipeline(dlt_mod, tmp_path, client)

    assert _read_ids(pipeline) == ["analytics.events:1", "analytics.events:2"]
    with pipeline.sql_client() as conn:
        content = conn.execute_sql(
            "SELECT content FROM clickhouse_rows WHERE id = 'analytics.events:1'"
        )[0][0]
    assert "Raw product analytics events." in content


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(dlt_mod, tmp_path):
    table = _events_table()
    client = FakeQueryClient({"analytics.events": table})
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)
    assert len(_read_ids(pipeline)) == 2

    # Sync #2: event 2 deleted upstream, event 1 untouched. The connector emits a
    # hard-delete marker for event 2 and dlt's merge removes it from the destination
    # — which is what cognee's orphan_cleanup reconciles against.
    table.rows = [table.rows[0]]
    pipeline.run(
        clickhouse_source(
            database="analytics",
            tables=["events"],
            key_columns={"events": "event_id"},
            cursor_columns={"events": "updated_at"},
            client=client,
        )
    )

    assert _read_ids(pipeline) == ["analytics.events:1"]


def test_state_survives_across_pipeline_runs(dlt_mod, tmp_path):
    # The incremental cursor only works if dlt's resource state outlives one run.
    client = FakeQueryClient({"analytics.events": _events_table()})
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)
    before = len(client.row_queries())

    pipeline.run(
        clickhouse_source(
            database="analytics",
            tables=["events"],
            key_columns={"events": "event_id"},
            cursor_columns={"events": "updated_at"},
            client=client,
        )
    )

    # The second run read the delta rather than backfilling, and did not re-emit
    # every row, so staging still holds exactly the corpus.
    assert len(_read_ids(pipeline)) == 2
    assert len(client.row_queries()) > before
    delta = next(sql for sql in client.row_queries() if "updated_at >=" in sql)
    assert "ORDER BY updated_at ASC" in delta
