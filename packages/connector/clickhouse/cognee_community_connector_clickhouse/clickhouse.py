"""ClickHouse connector for cognee — a ``dlt`` resource that syncs tables into memory.

Pulls rows out of ClickHouse tables into cognee memory, incrementally and with
forget-on-delete. It builds entirely on cognee's existing DLT ingestion subsystem,
so no core change is needed::

    import cognee
    from cognee_community_connector_clickhouse import clickhouse_source

    await cognee.remember(
        clickhouse_source(
            host="localhost",
            user="default",
            password="...",
            database="analytics",
            tables=["events"],
            key_columns={"events": "event_id"},
            cursor_columns={"events": "updated_at"},
        ),
        dataset_name="my_clickhouse",
        write_disposition="merge",  # REQUIRED: upsert by row key
    )

Design
------
* **Auth** — a ClickHouse user and password, over the native HTTP interface
  (``http://host:8123`` or ``https://host:8443``). Read-only: the connector only
  issues ``SELECT``s, including against ``system.tables`` / ``system.columns``.
* **Identity** — ClickHouse has no primary key in the SQL sense, so the row key is
  resolved explicitly: ``key_columns`` when given, otherwise the table's PRIMARY
  KEY, otherwise its sorting key, otherwise a column literally named ``id``. A
  composite key is accepted and is folded into one deterministic ``id`` value,
  because cognee's row identity downstream is a single column.
* **Ingestion path** — the resource declares
  ``cognee_document_source = "clickhouse"`` (``DOCUMENT_SOURCE_ATTR``), so
  ``resolve_dlt_sources`` tags each row ``system_metadata["source"] = "clickhouse"``
  and every row becomes a text document that flows through normal cognify entity
  extraction. That is the right treatment for ClickHouse rows, whose columns are
  usually a mix of prose-ish strings and wide, often nested, ClickHouse-native
  types that carry no useful relational schema to extract deterministically.
* **Row text** — each row renders to the table's comment, the table's column list,
  and the row's own values, mirroring cognee's own dlt schema-context text. Only
  values the connector read are emitted: a row's ``data_id`` is derived from a
  content hash over every emitted column, so anything that changes per run (a
  ``now()``, a query timestamp) would re-cognify the whole table on every sync.
* **Incremental cursor** — ``cursor_columns`` names a per-table monotonic column
  (typically ``updated_at``, a ``DateTime64``, or a version counter). The filter is
  pushed down as ``cursor >= {last cursor}`` and the high-water mark is persisted in
  dlt's per-resource state, so a re-run fetches and re-embeds only the delta.
* **Forget-on-delete** — ClickHouse reports deletions only through mutations and
  lightweight deletes, neither of which is a reliable feed, so this connector does
  not depend on them. Each run diffs a cheap key-only sweep against the keys seen on
  the previous run and emits the ``_deleted`` hard-delete markers that dlt removes
  from the destination; cognee's existing ``orphan_cleanup`` then purges the rows
  from the graph, vector, and relational stores.

Why ``>=`` and not ``>``
------------------------
The cursor is compared with ``>=``, not ``>``, and the boundary row is
deduplicated by ``id`` within the run. ClickHouse cursor columns tie constantly —
``DateTime64(3)`` stamps collide, and version counters repeat across a batch — and a
strict ``>`` silently drops *every* row sharing the previous run's maximum value,
which is data loss that no test against a fake server would reveal. Re-reading the
boundary row is free: ``merge`` upserts by key, the values are unchanged, so the
content-hash ``data_id`` is unchanged and nothing is re-cognified.

Failure posture
---------------
Deletion detection trusts the key sweep to enumerate every current row. A sweep
that comes back empty for a table that previously had rows almost always means a
transient failure (a dropped connection, a typo'd or renamed database, a table
being rebuilt) rather than a genuine wipe, so deletion is skipped for that table and
its key state is preserved. Treating it as "everything was deleted" would purge the
table from memory and overwrite the key state, making the loss permanent.

Known limitations
-----------------
* A row whose ``cursor_column`` is NULL is ingested when first seen, but later
  edits to it are invisible: ``>=`` can only match rows that carry the value. Give
  the cursor column a ``DEFAULT``/materialized value, or omit ``cursor_columns`` and
  rely on the key sweep to re-read changed rows every run.
* Wiping an entire table upstream does not forget anything, because that is
  indistinguishable from the transient-failure case above. It self-heals: the next
  run that sees any row reconciles normally.
* The key sweep and the persisted key set are both O(rows) per run. That is cheap
  for tables up to millions of rows when the key is the sorting key (ClickHouse
  answers it from the primary index without reading the data), and wasteful for
  larger key sets. Pass ``detect_deletions=False`` to skip it, at the cost of
  missing deletions and back-dated inserts.
* Named tuples, arrays, and maps render as JSON. Deeply nested values make for
  unreadable — and very large — row text; name the columns you care about in
  ``columns`` rather than selecting everything.
"""

from __future__ import annotations

import datetime
import decimal
import json
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

try:  # cognee >= 1.6.0 namespaces a connector's dlt state per connection.
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR
except ImportError:  # pragma: no cover - older cores fall back to a shared namespace
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

logger = get_logger("clickhouse_connector")

# dlt resource / staging-table name for ClickHouse rows.
CLICKHOUSE_TABLE_NAME = "clickhouse_rows"
CLICKHOUSE_SOURCE_NAME = "clickhouse"

# Cap on how many keys go into one ``IN`` predicate, so a large first-sight batch
# cannot build an unbounded query.
_KEY_FETCH_CHUNK = 500

_EXTRA_HINT = (
    "The ClickHouse connector needs dlt and clickhouse-connect: "
    'pip install "cognee-community-connector-clickhouse".'
)

# ClickHouse identifiers accepted by this connector. ClickHouse itself permits
# backtick-quoted identifiers with arbitrary characters; we reject them because the
# names are interpolated into SQL this connector builds, and a strict allowlist is
# the only way to keep a table or column name from becoming an injection point.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Scalar column type families the cursor high-water mark can carry, longest first so
# ``DateTime64`` is not truncated to ``Date``. Everything else is rejected: there is
# no meaningful total order to persist a high-water mark over.
_CURSOR_TYPE_PREFIXES = (
    "DateTime64",
    "DateTime32",
    "DateTime",
    "FixedString",
    "Date32",
    "Date",
    "String",
    "UUID",
    "IPv4",
    "IPv6",
    "Bool",
    "Int256",
    "Int128",
    "UInt256",
    "UInt128",
    "UInt64",
    "UInt32",
    "UInt16",
    "UInt8",
    "Int64",
    "Int32",
    "Int16",
    "Int8",
    "Float64",
    "Float32",
    "Decimal",
    "Enum16",
    "Enum8",
    "Enum",
)

# Wrapper types that decorate a scalar without changing its ordering.
_CURSOR_WRAPPER_RE = re.compile(r"^(?:LowCardinality)\(")

# Types with no single total order to keep a high-water mark over.
_CURSOR_UNSUPPORTED = ("Nullable", "Array", "Map", "Tuple", "Nested", "AggregateFunction")


def _is_scalar_cursor_type(base: str) -> bool:
    """True for a scalar type family the cursor can carry.

    ``Nullable`` is rejected: a NULL cursor value cannot be compared against the
    mark, so accepting one would silently drop every NULL row after the first sync.
    """
    if any(base.startswith(prefix) for prefix in _CURSOR_UNSUPPORTED):
        return False
    return any(base.startswith(prefix) for prefix in _CURSOR_TYPE_PREFIXES)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
def _ident(name: str, *, kind: str) -> str:
    """Validate a ClickHouse identifier, or raise with an actionable message.

    Database, table, and column names reach this connector as plain strings and are
    interpolated into the SQL it builds. Restricting them to the unquoted subset
    ClickHouse reads unquoted keeps that interpolation safe and turns a hostile or
    merely exotic name into a clear error instead of a syntax error or a query that
    does something other than what the caller asked for.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(
            f"Unsafe ClickHouse {kind} name {name!r}. Only letters, digits, and "
            "underscores are allowed, and the name may not start with a digit. "
            "Backtick-quoted identifiers are not supported — rename the object."
        )
    return name


def _ident_list(names: Any, *, kind: str) -> list[str]:
    """Validate an iterable of identifiers into a list, preserving order."""
    if isinstance(names, str):
        names = [names]
    return [_ident(name, kind=kind) for name in names]


def _qualified(database: str, table: str) -> str:
    """Return the ``database.table`` name used for ids and log messages."""
    return f"{database}.{table}"


def _scope_name(host: str, port: int, secure: bool, database: str | None) -> str:
    """Build the per-connection namespace for dlt's pipeline state.

    Without this every ClickHouse source shares one ``ingest_dlt_source`` state
    directory, so syncing two databases into one cognee install would let one
    source's cursor and key set drive the other's. cognee hashes this together with
    the dataset name into the pipeline name.
    """
    scheme = "https" if secure else "http"
    return f"clickhouse:{scheme}://{host}:{port}/{database or 'default'}"


# ---------------------------------------------------------------------------
# Client / query layer
# ---------------------------------------------------------------------------
def _make_client(
    host: str,
    port: int,
    user: str,
    password: str,
    *,
    secure: bool,
    database: str | None,
) -> Any:
    """Build a ``clickhouse_connect`` client authenticated with user + password."""
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        secure=secure,
        database=database,
    )


def _query(client: Any, sql: str, parameters: dict | None = None) -> tuple[list[str], list[tuple]]:
    """Run a query and return ``(column_names, rows)``.

    Wrapped in one place so the fake client the tests use only has to mimic
    ``clickhouse_connect.Client.query``'s contract.
    """
    result = client.query(sql, parameters=parameters or {})
    names = list(getattr(result, "column_names", None) or [])
    rows = getattr(result, "result_rows", None)
    if rows is None:
        rows = result
    return names, [tuple(row) for row in rows]


def _query_dicts(client: Any, sql: str, parameters: dict | None = None) -> list[dict[str, Any]]:
    """Run a query and return its rows as dicts keyed by column name."""
    names, rows = _query(client, sql, parameters)
    if not names:
        return []
    return [dict(zip(names, row, strict=True)) for row in rows]


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------
def _scalar(value: Any) -> str:
    """Render one cell as text for the row's content.

    Containers become JSON rather than a Python ``repr`` so the text cognee
    cognifies is machine-shaped. ``default=str`` covers the types JSON cannot
    encode on its own (Decimal, UUID, bytes, datetimes).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _row_key(row: dict[str, Any], key_columns: list[str]) -> str:
    """Fold a row's key columns into one deterministic identity string.

    A single key column renders as its own text so the ids stay readable. Several
    render as a JSON array, which is unambiguous where concatenating the values
    would not be (``("ab", "c")`` vs ``("a", "bc")``).
    """
    if len(key_columns) == 1:
        return _scalar(row.get(key_columns[0]))
    return json.dumps(
        [row.get(column) for column in key_columns],
        ensure_ascii=False,
        default=str,
    )


def _row_id(database: str, table: str, row: dict[str, Any], key_columns: list[str]) -> str:
    """Return a globally unique row id, prefixed with the table it came from.

    Every table lands in one staging table, so the key on its own would collide
    across tables and ``merge`` would upsert two different rows over each other.
    """
    return f"{_qualified(database, table)}:{_row_key(row, key_columns)}"


def _id_from_key_value(
    database: str,
    table: str,
    key_columns: list[str],
    value: Any,
) -> str:
    """Build a row id from a raw key tuple, as the key-only sweep returns it."""
    if len(key_columns) == 1:
        row = dict.fromkeys(key_columns, value)
    else:
        row = dict(zip(key_columns, value, strict=True))
    return _row_id(database, table, row, key_columns)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def _table_columns(client: Any, database: str, table: str) -> dict[str, str]:
    """Return ``{column_name: type}`` for a table, in declaration order."""
    rows = _query_dicts(
        client,
        "SELECT name, type FROM system.columns "
        "WHERE {db:String} = database AND {tbl:String} = table ORDER BY position",
        {"db": database, "tbl": table},
    )
    return {row["name"]: row["type"] for row in rows}


def _table_comment(client: Any, database: str, table: str) -> str:
    """Return a table's ``COMMENT``, or ``""`` when it has none."""
    rows = _query_dicts(
        client,
        "SELECT comment FROM system.tables "
        "WHERE {db:String} = database AND {tbl:String} = name LIMIT 1",
        {"db": database, "tbl": table},
    )
    return (rows[0].get("comment") or "") if rows else ""


def _resolve_key_columns(
    client: Any,
    database: str,
    table: str,
    columns: dict[str, str],
) -> list[str]:
    """Resolve a table's identity columns.

    Preference order: what the caller asked for, the table's PRIMARY KEY, its
    sorting key, then a column named ``id``. The sorting key is ClickHouse's closest
    analogue to a primary key — it is what makes a row addressable and what the
    engine reads first — and it is stable across merges, which is what upsert-by-key
    needs.
    """
    keys = [
        row["name"]
        for row in _query_dicts(
            client,
            "SELECT name FROM system.columns "
            "WHERE {db:String} = database AND {tbl:String} = table "
            "AND is_in_primary_key = 1 ORDER BY position",
            {"db": database, "tbl": table},
        )
    ]
    if not keys:
        keys = [
            row["name"]
            for row in _query_dicts(
                client,
                "SELECT name FROM system.columns "
                "WHERE {db:String} = database AND {tbl:String} = table "
                "AND is_in_sorting_key = 1 ORDER BY position",
                {"db": database, "tbl": table},
            )
        ]
    # An expression sorting key (``ORDER BY _partition_date``) has no stored column
    # to key on, and a virtual one cannot be selected back out of the table.
    keys = [key for key in keys if key in columns]
    if not keys and "id" in columns:
        keys = ["id"]
    if not keys:
        raise ValueError(
            f"ClickHouse table {_qualified(database, table)!r} has no usable row key: "
            "it declares no PRIMARY KEY or sorting key over stored columns. Pass "
            "key_columns={...} to tell the connector which columns identify a row."
        )
    return keys


def _cursor_type(column_type: str) -> str:
    """Return the ClickHouse type a cursor value binds as, or raise.

    Only scalar types are accepted: the connector keeps a high-water mark per table
    and compares rows against it, which needs a total order. A nullable or complex
    type has no single meaningful maximum.
    """
    base = column_type.strip()
    # Unwrap decorators before dropping parameters, so ``LowCardinality(String)`` is
    # judged on String rather than on the wrapper. Each round strips one wrapper
    # layer and that layer's own parameter list.
    while _CURSOR_WRAPPER_RE.match(base):
        # The wrapper regex consumes its own opening paren, so the matching close
        # paren is whatever follows the inner type. Drop through it explicitly.
        base = base[_CURSOR_WRAPPER_RE.match(base).end() :]
        base = re.sub(r"\).*$", "", base).strip()
    base = re.sub(r"\(.*\)$", "", base).strip()

    if not _is_scalar_cursor_type(base):
        raise ValueError(
            f"Cursor column type {column_type!r} is not supported. Use a monotonic "
            "scalar column — DateTime64, a UInt64 version counter, and so on."
        )
    return base


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------
def _advance_cursor(newest: Any, value: Any) -> Any:
    """Return the newer of two cursor values, tolerating incomparable types.

    A table whose cursor column changes type mid-life (or a cursor set by hand in
    state) would otherwise raise ``TypeError`` mid-stream and abort the whole sync.
    Keeping the old high-water mark is the safe answer: the next run re-reads a
    slightly wider window instead of losing rows.
    """
    if value is None:
        return newest
    if newest is None:
        return value
    try:
        return max(newest, value)
    except TypeError:
        logger.warning(
            "ClickHouse: cursor values are not mutually comparable (%r vs %r); "
            "keeping the previous high-water mark.",
            type(newest).__name__,
            type(value).__name__,
        )
        return newest


def _selected_columns(
    columns: list[str] | None,
    required: list[str],
) -> list[str]:
    """Return the columns to SELECT: the caller's, forced to carry ``required``.

    Without the key and cursor columns in the SELECT list the connector cannot
    build a row id or advance the high-water mark, so the cursor would never move
    and every run would re-read a monotonically growing delta.
    """
    if not columns:
        return _dedupe(required)
    return _dedupe([*required, *(column for column in columns if column not in required)])


def _dedupe(names: Iterable[str]) -> list[str]:
    """Return ``names`` with duplicates dropped, order preserved."""
    return list(dict.fromkeys(names))


def _chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield ``items`` in lists of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _key_predicate(
    key_columns: list[str],
    key_types: dict[str, str],
    values: list[Any],
) -> tuple[str, dict[str, Any]]:
    """Build an ``IN`` predicate over the key columns, with bound parameters.

    ClickHouse accepts a tuple form — ``(a, b) IN ((1, 'x'), (2, 'y'))`` — so one
    code path serves a single key and a composite one.

    Each value gets its own parameter name, because the server substitutes a
    parameter by name: repeating one placeholder per value would bind them all to
    the same value and collapse the predicate to a single key. Values are bound
    with their real column type rather than as strings so the server compares like
    with like instead of coercing.
    """
    composite = len(key_columns) > 1
    parameters: dict[str, Any] = {}
    groups: list[str] = []
    for index, value in enumerate(values):
        cells = list(value) if isinstance(value, (list, tuple)) else [value]
        placeholders = []
        for column, cell in zip(key_columns, cells, strict=True):
            name = f"k_{column}_{index}"
            parameters[name] = cell
            placeholders.append(f"{{{name}:{key_types.get(column, 'String')}}}")
        groups.append("(" + ", ".join(placeholders) + ")" if composite else placeholders[0])

    # A single-column key uses the flat form ``col IN (a, b)``; the tuple form is only
    # valid — and only needed — for two or more columns.
    if not composite:
        return f"{key_columns[0]} IN ({', '.join(groups)})", parameters
    return f"({', '.join(key_columns)}) IN ({', '.join(groups)})", parameters


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------
def _render_row(
    row: dict[str, Any],
    *,
    database: str,
    table: str,
    comment: str,
    columns: dict[str, str],
    key_columns: list[str],
    title_column: str | None,
) -> dict[str, Any]:
    """Render one ClickHouse row into a document-mode dlt row.

    The document-mode row contract is ``{id, title, content}`` plus provenance; only
    identity, provenance, and the rendered text are kept. Keeping the row narrow is
    what makes an unchanged row keep a stable content-hash ``data_id`` downstream,
    and therefore not be re-embedded or re-cognified on a no-op sync.
    """
    row_id = _row_id(database, table, row, key_columns)

    if title_column and title_column in row:
        title = _scalar(row.get(title_column))
    else:
        title = f"{_qualified(database, table)} {_row_key(row, key_columns)}"

    lines = [f"Table: {_qualified(database, table)}"]
    if comment:
        lines.append(f"Comment: {comment}")
    lines.append("")
    lines.append("Columns:")
    lines.extend(f"  - {name}: {column_type}" for name, column_type in columns.items())
    lines.append("")
    lines.append("Row Data:")
    for name, value in row.items():
        lines.append(f"  {name}: {_scalar(value)}")

    return {
        "id": row_id,
        "database": database,
        "table": table,
        "title": title,
        "content": "\n".join(lines),
        # Present on every live row so dlt infers the column; deletions are emitted
        # separately with _deleted=True.
        "_deleted": False,
    }


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build the hard-delete marker row for a row that vanished upstream."""
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Per-table sync
# ---------------------------------------------------------------------------
def _sweep_keys(
    client: Any,
    database: str,
    table: str,
    key_columns: list[str],
    where: str | None,
) -> list[Any]:
    """Return the keys currently present in a table, as id-shaped values.

    Selecting only the key columns keeps this an index scan; when the key is the
    sorting key ClickHouse answers it from the primary index without touching the
    data at all.
    """
    sql = f"SELECT {', '.join(key_columns)} FROM {database}.{table}"
    if where:
        # Parenthesized so a compound caller predicate ("a = 1 OR b = 2") cannot
        # swallow the conjunct this query would otherwise add.
        sql += f" WHERE ({where})"
    rows = _query(client, sql)[1]
    if len(key_columns) == 1:
        return [row[0] for row in rows]
    return [list(row) for row in rows]


def _fetch_rows(
    client: Any,
    database: str,
    table: str,
    select_columns: list[str],
    where: str | None,
    cursor_column: str | None,
    cursor_value: Any,
    cursor_sql_type: str | None,
    key_columns: list[str] | None = None,
    key_types: dict[str, str] | None = None,
    key_values: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """SELECT rows, optionally bounded by the cursor or by an explicit key set."""
    sql = f"SELECT {', '.join(select_columns)} FROM {database}.{table}"
    parameters: dict[str, Any] = {}

    predicates = []
    if where:
        predicates.append(f"({where})")
    if cursor_column is not None:
        # >= , never > : see the module docstring on tied cursor values.
        predicates.append(f"{cursor_column} >= {{cursor:{cursor_sql_type}}}")
        parameters["cursor"] = cursor_value
    if key_values:
        predicate, key_parameters = _key_predicate(key_columns or [], key_types or {}, key_values)
        predicates.append(predicate)
        parameters.update(key_parameters)

    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    if cursor_column is not None:
        sql += f" ORDER BY {cursor_column} ASC"

    return _query_dicts(client, sql, parameters)


def sync_rows(
    client: Any,
    state: dict,
    *,
    tables: dict[str, dict[str, Any]],
    where: str | None = None,
    detect_deletions: bool = True,
    title_column: str | None = None,
    columns: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed rows across the configured tables, then hard-delete markers.

    ``tables`` maps ``"database.table"`` to ``{database, table, key_columns,
    cursor_column, cursor_sql_type, columns, comment}``. ``state`` is dlt's
    per-resource state dict and carries the per-table ``cursors`` high-water marks
    and the ``known_ids`` key set across runs; it is mutated in place.
    """
    known_ids: set[str] = set(state.get("known_ids") or [])
    cursors: dict[str, Any] = dict(state.get("cursors") or {})
    seen_this_run: set[str] = set()
    next_cursors: dict[str, Any] = {}

    changed = 0
    deleted_total = 0

    for qualified, config in tables.items():
        database = config["database"]
        table = config["table"]
        key_columns: list[str] = config["key_columns"]
        table_columns: dict[str, str] = config["columns"]
        cursor_column: str | None = config["cursor_column"]
        cursor_sql_type: str | None = config["cursor_sql_type"]
        last_cursor = cursors.get(qualified)

        required = [*key_columns, *([cursor_column] if cursor_column else [])]
        # With no explicit projection, read the whole row — a caller who names no
        # columns means "all of them", not "just the key and the cursor".
        select_columns = _selected_columns(columns or list(table_columns), required)

        # Row ids keyed to the raw key value the sweep returned, so the back-dated
        # pass can re-query by key without parsing an id back apart.
        current: dict[str, Any] = {}
        if detect_deletions:
            for value in _sweep_keys(client, database, table, key_columns, where):
                current[_id_from_key_value(database, table, key_columns, value)] = value

        # A one-element box so the high-water mark advances across the loop below
        # without binding the loop variables into a closure.
        cursor_box = [last_cursor]

        # First run: backfill every row in scope. The cursor stays at None unless a
        # row carries it, so the next run re-reads the window rather than trusting a
        # cursor nothing wrote.
        if last_cursor is None:
            fetched = _fetch_rows(client, database, table, select_columns, where, None, None, None)
        else:
            fetched = _fetch_rows(
                client,
                database,
                table,
                select_columns,
                where,
                cursor_column,
                last_cursor,
                cursor_sql_type,
            )

            # A row new to the corpus is fetched regardless of its cursor value, so a
            # back-dated insert (a late batch load, a materialized view catching up)
            # is not lost. The cursor pass above already covered the common case; this
            # picks up the rest.
            if detect_deletions:
                missing = [
                    current[row_id] for row_id in sorted(set(current) - known_ids - seen_this_run)
                ]
                for chunk in _chunked(missing, _KEY_FETCH_CHUNK):
                    fetched.extend(
                        _fetch_rows(
                            client,
                            database,
                            table,
                            select_columns,
                            where,
                            None,
                            None,
                            None,
                            key_columns=key_columns,
                            key_types=table_columns,
                            key_values=chunk,
                        )
                    )

        for row in fetched:
            rendered = _render_row(
                row,
                database=database,
                table=table,
                comment=config["comment"],
                columns=table_columns,
                key_columns=key_columns,
                title_column=title_column,
            )
            if rendered["id"] in seen_this_run:
                # The >= boundary re-reads the last row of the previous run on every
                # sync; its values are unchanged, so dropping the duplicate keeps one
                # row per id without changing anything cognee would store.
                continue
            seen_this_run.add(rendered["id"])
            if cursor_column is not None:
                cursor_box[0] = _advance_cursor(cursor_box[0], row.get(cursor_column))
            changed += 1
            yield rendered

        if cursor_column is not None:
            next_cursors[qualified] = cursor_box[0]

        # Deletions. An empty sweep over a table that previously had rows is treated
        # as a transient failure rather than a mass deletion, so its key state survives.
        if not detect_deletions:
            continue

        prefix = f"{qualified}:"
        known_for_table = {row_id for row_id in known_ids if row_id.startswith(prefix)}

        if known_for_table and not current:
            logger.warning(
                "ClickHouse: key sweep for %s returned 0 rows but %d were known; "
                "skipping deletion this run so a transient failure cannot purge the table.",
                qualified,
                len(known_for_table),
            )
            continue

        deleted = known_for_table - set(current)
        for row_id in sorted(deleted):
            yield _deleted_row(row_id)
            deleted_total += 1

        known_ids = (known_ids - deleted) | set(current)

    state["cursors"] = next_cursors
    state["known_ids"] = sorted(known_ids)
    logger.info(
        "ClickHouse: %d changed row(s), %d deletion(s) across %d table(s).",
        changed,
        deleted_total,
        len(tables),
    )


def _build_table_config(
    client: Any,
    requested: list[tuple[str, str]],
    *,
    key_columns: dict[str, str | list[str]],
    cursor_columns: dict[str, str],
    columns: list[str] | None,
    include_table_comments: bool,
) -> dict[str, dict[str, Any]]:
    """Introspect each table and resolve its keys, cursor, columns, and comment.

    Everything that can be validated is validated here, before a single row is read,
    so a typo in a column name or a key that does not exist fails the sync rather
    than quietly ingesting a narrower row (or nothing at all).
    """
    config: dict[str, dict[str, Any]] = {}
    for db_name, table_name in requested:
        qualified = _qualified(db_name, table_name)
        table_columns = _table_columns(client, db_name, table_name)
        if not table_columns:
            raise ValueError(
                f"ClickHouse table {qualified!r} has no columns — it does not exist, "
                "or the user cannot read system.columns for it."
            )

        declared = key_columns.get(qualified, key_columns.get(table_name))
        keys = (
            _ident_list(declared, kind="column")
            if declared
            else _resolve_key_columns(client, db_name, table_name, table_columns)
        )
        for key in keys:
            if key not in table_columns:
                raise ValueError(
                    f"Key column {key!r} is not a column of {qualified!r}. "
                    f"Available: {', '.join(table_columns)}"
                )

        cursor_column = cursor_columns.get(qualified, cursor_columns.get(table_name))
        cursor_column = _ident(cursor_column, kind="column") if cursor_column else None
        cursor_sql_type = None
        if cursor_column is not None:
            if cursor_column not in table_columns:
                raise ValueError(
                    f"Cursor column {cursor_column!r} is not a column of {qualified!r}. "
                    f"Available: {', '.join(table_columns)}"
                )
            cursor_sql_type = _cursor_type(table_columns[cursor_column])

        for column in _ident_list(columns, kind="column") if columns else []:
            if column not in table_columns:
                raise ValueError(
                    f"Column {column!r} is not a column of {qualified!r}. "
                    f"Available: {', '.join(table_columns)}"
                )

        config[qualified] = {
            "database": db_name,
            "table": table_name,
            "key_columns": keys,
            "cursor_column": cursor_column,
            "cursor_sql_type": cursor_sql_type,
            "columns": table_columns,
            "comment": _table_comment(client, db_name, table_name)
            if include_table_comments
            else "",
        }
    return config


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def clickhouse_source(
    *,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
    secure: bool = False,
    tables: list[str] | None = None,
    key_columns: dict[str, str | list[str]] | None = None,
    cursor_columns: dict[str, str] | None = None,
    title_column: str | None = None,
    columns: list[str] | None = None,
    where: str | None = None,
    include_table_comments: bool = True,
    detect_deletions: bool = True,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding ClickHouse rows for ``cognee.remember``.

    Args:
        host: ClickHouse host. Falls back to ``CLICKHOUSE_HOST``.
        port: HTTP port. Defaults to 8443 when ``secure`` else 8123; falls back to
            ``CLICKHOUSE_PORT``.
        user: ClickHouse user. Falls back to ``CLICKHOUSE_USER``.
        password: ClickHouse password. Falls back to ``CLICKHOUSE_PASSWORD``.
        database: Default database for unqualified names in ``tables``. Falls back to
            ``CLICKHOUSE_DATABASE``.
        secure: Use HTTPS instead of HTTP.
        tables: ``["events", "db.users"]`` — what to ingest. Names without a dot are
            qualified with ``database``.
        key_columns: Per-table identity, ``{"events": "event_id"}`` or
            ``{"db.users": ["tenant_id", "user_id"]}``. Omitted, the table's PRIMARY
            KEY, then its sorting key, then a column named ``id`` is used.
        cursor_columns: Per-table monotonic column, ``{"events": "updated_at"}``. The
            delta filter is pushed down as ``cursor >= {high-water mark}``. Omit for a
            table to re-read all of it every run.
        title_column: Column used as the document heading. Defaults to a
            ``database.table <key>`` label.
        columns: Restrict which columns are read. The key and cursor columns are
            always included.
        where: Extra SQL predicate narrowing the rows read, e.g.
            ``"env = 'prod'"``. Applied to both the row reads and the key sweep, so a
            row that falls out of it is treated as absent and forgotten. Trusted
            input: it is SQL, written by whoever calls this connector.
        include_table_comments: Prefix each row's text with its table's ``COMMENT``.
        detect_deletions: When False, skip the per-run key sweep. Cheaper, but
            deletions and back-dated inserts are then never detected.
        client: Pre-built ``clickhouse_connect`` client (mainly a test-injection
            point); when omitted one is built from the arguments above.

    Returns:
        A ``dlt`` resource (``clickhouse_rows``) configured with
        ``primary_key="id"``, ``write_disposition="merge"``, and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    host = host or os.environ.get("CLICKHOUSE_HOST")
    if client is None and not host:
        raise ValueError("ClickHouse host required: pass host= or set CLICKHOUSE_HOST.")

    if secure:
        port = port or int(os.environ.get("CLICKHOUSE_PORT") or 8443)
    else:
        port = port or int(os.environ.get("CLICKHOUSE_PORT") or 8123)

    if client is None:
        user = user or os.environ.get("CLICKHOUSE_USER")
        password = password if password is not None else os.environ.get("CLICKHOUSE_PASSWORD")
        database = database or os.environ.get("CLICKHOUSE_DATABASE")
        secure = secure or os.environ.get("CLICKHOUSE_SECURE", "").lower() in {"1", "true", "yes"}

    database = _ident(database, kind="database") if database else None

    requested: list[tuple[str, str]] = []
    for name in _ident_list(tables, kind="table") if tables else []:
        if "." in name:
            requested.append(tuple(name.split(".", 1)))  # type: ignore[arg-type]
        elif database:
            requested.append((database, name))
        else:
            raise ValueError(
                f"Table {name!r} has no database and no default database was given. "
                "Write it as 'database.table' or pass database=."
            )
    if not requested:
        raise ValueError("tables= is required: name the tables to ingest.")
    for db_name, table_name in requested:
        _ident(db_name, kind="database")
        _ident(table_name, kind="table")

    key_columns = key_columns or {}
    cursor_columns = cursor_columns or {}

    @dlt.resource(
        name=CLICKHOUSE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are removed
        # from the dlt destination on merge, which is what propagates an upstream
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickhouse_rows():
        handle = client
        if handle is None:
            handle = _make_client(
                host,
                port,
                user,
                password,
                secure=secure,
                database=database,
            )

        config = _build_table_config(
            handle,
            requested,
            key_columns=key_columns,
            cursor_columns=cursor_columns,
            columns=columns,
            include_table_comments=include_table_comments,
        )

        yield from sync_rows(
            handle,
            dlt.current.resource_state(),
            tables=config,
            where=where,
            detect_deletions=detect_deletions,
            title_column=title_column,
            columns=columns,
        )

    resource = clickhouse_rows
    # Opt into the document ingestion path: each row becomes a text document that
    # goes through normal cognify rather than the deterministic dlt-row path.
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, CLICKHOUSE_SOURCE_NAME)
    # Namespace dlt's pipeline state to this connection, so two ClickHouse sources in
    # one cognee install cannot drive each other's cursor and key set.
    setattr(resource, PIPELINE_SCOPE_ATTR, _scope_name(host or "injected", port, secure, database))
    return resource
