"""ClickHouse connector for cognee — a ``dlt`` source that turns a table into memory.

Pull rows from a ClickHouse table into cognee, incrementally and with
forget-on-deletion.  Like the sibling Confluence connector this builds
entirely on the existing DLT ingestion subsystem; the source produced here is
handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_clickhouse import clickhouse_source

    await cognee.remember(
        clickhouse_source(
            host="localhost",
            port=8123,
            user="default",
            password="...",
            database="analytics",
            table="support_tickets",
            cursor_column="updated_at",
        ),
        dataset_name="support_tickets",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — username + password over HTTP(S), via the official
  ``clickhouse-connect`` client (``clickhouse_connect.get_client(...)``).
* **Primary key** — ``id_column`` (default ``"id"``).  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.  The column's
  value is always normalized to ``str()`` on the way out — for both live rows
  and hard-delete markers — so the merge key has a stable type regardless of
  whether the underlying ClickHouse column is a ``UInt64``, ``String``, or
  ``UUID`` (this mirrors ``_page_to_row``'s ``str(page.get("id"))`` in the
  Confluence connector, for the same reason: dlt's merge match is exact-typed).
* **Incremental cursor** — a monotonic column you name (``cursor_column``,
  e.g. an ``updated_at`` timestamp or an auto-increment id).  Each run pushes
  ``WHERE cursor_column > last_cursor`` down to ClickHouse and fetches only
  the rows changed since the last sync; the cursor is persisted in dlt's
  per-resource state, so re-running ``remember`` resumes where it left off.
* **Forget-on-delete** — ClickHouse has no deletion feed, so each run also
  does a lightweight ``SELECT id_column FROM table`` sweep (cheap: ClickHouse
  reads only that one column off disk, no wide-row materialization) and
  compares it against the id set seen on the previous run (also kept in
  resource state).  Rows that vanished are emitted with the ``_deleted``
  hard-delete marker; dlt removes those rows on ``merge`` and cognee's
  existing ``orphan_cleanup`` then purges them from the graph + vector +
  relational stores.
* **New rows below the cursor** — a row can become new to the corpus without
  tripping the cursor filter (inserted with a backdated ``cursor_column``
  value, restored, or moved into scope). The id sweep above also computes
  ``current_ids - known_ids``; any such new ids get a second, narrow
  ``WHERE id_column IN (...)`` fetch so they are not silently skipped. This
  mirrors Confluence's ``page_id not in known_ids`` fallback.

.. important::
   ``write_disposition="merge"`` is required at the resource level (already
   set here) — the add pipeline defaults to ``"replace"``, which would wipe
   the synced table on the second sync.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50).  For a real table pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

Table comments (provisional — needs maintainer sign-off)
----------------------------------------------------------
See the big comment above ``sync_table_comments`` below: table/column
``COMMENT``s (from ``system.tables`` / ``system.columns``) are synced as a
*separate* dlt resource/destination table (``clickhouse_table_comments``),
not folded into the row data. This was the least-bad of a few options and is
flagged there for review; it is easy to swap for a different shape later
since it is fully decoupled from ``sync_rows``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("clickhouse_connector")

_EXTRA_HINT = (
    'The ClickHouse connector requires the "clickhouse" extra: '
    'pip install "cognee[clickhouse]" (provides dlt and clickhouse-connect).'
)


# ---------------------------------------------------------------------------
# Auth / client construction
# ---------------------------------------------------------------------------
def build_clickhouse_client(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> Any:
    """Build an authenticated ``clickhouse-connect`` client.

    ``clickhouse-connect`` is imported lazily so it stays an optional
    dependency (``pip install "cognee[clickhouse]"``).
    """
    try:
        import clickhouse_connect
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(_EXTRA_HINT) from exc

    # clickhouse-connect's client kwarg is `username`, not `user` — the
    # connector's own public keyword is `user` to match the rest of the
    # cognee-community connectors (gmail/confluence use `user`-ish plain
    # auth kwargs), so the rename happens only at this boundary.
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        database=database,
    )


# ---------------------------------------------------------------------------
# Row / marker shaping
# ---------------------------------------------------------------------------
def _row_to_dict(column_names: list[str], row: tuple, id_column: str) -> dict[str, Any]:
    """Flatten a ClickHouse result row into a dlt row dict.

    The id column is normalized to ``str()`` (see the module docstring) so the
    merge key's type is stable no matter what ClickHouse type backs it.
    """
    record = dict(zip(column_names, row, strict=True))
    record[id_column] = str(record[id_column])
    # Hard-delete marker (always False for live rows). Vanished rows are
    # emitted separately with _deleted=True by _deleted_row below.
    record["_deleted"] = False
    return record


def _deleted_row(id_column: str, row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a row by id."""
    return {id_column: str(row_id), "_deleted": True}


def _to_storable_cursor(value: Any) -> Any:
    """Coerce a cursor value into something dlt's (JSON) resource state can hold.

    Native ClickHouse Python values coming back from clickhouse-connect
    include ``int``, ``float``, and ``str`` (all JSON-safe as-is) but also
    ``datetime.datetime`` / ``datetime.date`` / ``decimal.Decimal`` for
    DateTime/Date/Decimal columns, which are not JSON-serializable. Those are
    stored as their ``str()`` form; a plain client-side ``%(cursor)s`` bind
    (see ``sync_rows``) re-quotes that string on the next run, and ClickHouse
    implicitly casts the string literal back to compare against the typed
    column, so the round-trip is lossless for the comparison that matters.
    """
    if value is None or isinstance(value, (int, float, str)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_rows(
    client: Any,
    state: dict,
    *,
    table: str,
    id_column: str,
    cursor_column: str,
) -> Iterator[dict[str, Any]]:
    """Yield changed rows since the last run, plus hard-delete markers.

    One cheap id-only sweep (``SELECT id_column FROM table``) enumerates the
    *current* row ids: that set drives deletion detection (diffed against
    ``known_ids`` from the previous run) and also catches ids that are new to
    the corpus but sit below the cursor. Rows newer than the stored cursor —
    plus any such "new but old-cursor" rows — have their full column set
    fetched and emitted. The cursor (``last_cursor``) and the id set
    (``known_ids``) are advanced in ``state`` so the next run is a no-op when
    nothing changed.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    last_cursor = state.get("last_cursor")

    # --- cheap id-only sweep -------------------------------------------------
    id_result = client.query(f"SELECT `{id_column}` FROM `{table}`")
    current_id_values = [row[0] for row in id_result.result_rows]
    current_ids: set[str] = {str(v) for v in current_id_values}
    # Map back from the stringified id to the original (typed) value, so a
    # follow-up `WHERE id_column IN (...)` fetch can bind the real type
    # instead of a re-quoted string.
    id_lookup = {str(v): v for v in current_id_values}
    new_ids = current_ids - known_ids

    yielded_ids: set[str] = set()
    newest_cursor: Any = None
    changed = 0

    def _emit(result) -> Iterator[dict[str, Any]]:
        nonlocal newest_cursor, changed
        if not result.result_rows:
            return
        column_names = result.column_names
        id_index = column_names.index(id_column)
        cursor_index = column_names.index(cursor_column)
        for row in result.result_rows:
            row_id = str(row[id_index])
            if row_id in yielded_ids:
                # Already emitted via the other fetch below (a row can be both
                # "newer than the cursor" and "new to the corpus").
                continue
            yielded_ids.add(row_id)
            cursor_value = row[cursor_index]
            if newest_cursor is None or cursor_value > newest_cursor:
                newest_cursor = cursor_value
            changed += 1
            yield _row_to_dict(column_names, row, id_column)

    if last_cursor is None:
        # First run: full backfill.
        yield from _emit(client.query(f"SELECT * FROM `{table}` ORDER BY `{cursor_column}`"))
    else:
        # client-side `%(name)s` binding: clickhouse-connect infers the SQL
        # literal form (quoted string / numeric literal) from the Python
        # value's own type rather than requiring us to know the column's
        # ClickHouse type up front — cursor_column may be a timestamp, an
        # integer, or a string, and this connector is generic over all three.
        yield from _emit(
            client.query(
                f"SELECT * FROM `{table}` WHERE `{cursor_column}` > %(cursor)s "
                f"ORDER BY `{cursor_column}`",
                parameters={"cursor": last_cursor},
            )
        )
        if new_ids:
            new_id_values = [id_lookup[i] for i in new_ids]
            placeholders = ", ".join(f"%(id_{i})s" for i in range(len(new_id_values)))
            params = {f"id_{i}": v for i, v in enumerate(new_id_values)}
            yield from _emit(
                client.query(
                    f"SELECT * FROM `{table}` WHERE `{id_column}` IN ({placeholders})",
                    parameters=params,
                )
            )

    # Deletion detection relies on the sweep enumerating every current row. An
    # empty sweep while rows were previously known almost always means a
    # transient/failed listing (network blip, momentary empty result) rather
    # than a genuine wipe — treating it as "all deleted" would purge the whole
    # dataset and overwrite known_ids with [], making the loss permanent. Skip
    # deletion and preserve state in that case.
    if known_ids and not current_ids:
        logger.warning(
            "ClickHouse: id sweep of '%s' returned 0 rows but %d were known; skipping "
            "deletion this run to avoid a mass forget-on-delete on a transient sweep.",
            table,
            len(known_ids),
        )
        if newest_cursor is not None:
            state["last_cursor"] = _to_storable_cursor(newest_cursor)
        logger.info("ClickHouse: %d changed row(s), 0 deletion(s).", changed)
        return

    deleted = known_ids - current_ids
    for row_id in sorted(deleted):
        yield _deleted_row(id_column, row_id)

    state["known_ids"] = sorted(current_ids)
    if newest_cursor is not None:
        state["last_cursor"] = _to_storable_cursor(newest_cursor)
    logger.info("ClickHouse: %d changed row(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Table comments
# ---------------------------------------------------------------------------
# FLAG FOR MAINTAINER REVIEW — shape of table/column comment ingestion.
#
# ClickHouse lets you attach a COMMENT to a table and to each column
# (`system.tables.comment` / `system.columns.comment`). There's no obviously
# "right" way to fold that schema metadata into a row-oriented sync, so this
# picks ONE reasonable option and isolates it so it's cheap to change:
#
#   Chosen: a SEPARATE dlt resource / destination table
#   (`clickhouse_table_comments`), one synthetic row per sync summarizing the
#   table comment plus every column's comment. `write_disposition="replace"`
#   (a full, cheap snapshot each run — there's exactly one row) rather than
#   `merge`, since there's no per-comment identity worth tracking or
#   forgetting individually.
#
#   Rejected alternative A: attach the table comment to every content row
#   (denormalized). Rejected because it bloats every row with data that never
#   changes per-row, gets re-embedded on every content change for no reason,
#   and duplicates the same text across the whole table's rows in the graph.
#
#   Rejected alternative B: emit table comments as just another row in the
#   main `clickhouse_rows` resource under a synthetic id (e.g.
#   `"__schema__"`). Rejected because it collides with `id_column`'s real
#   type/namespace (an int id_column can't hold a string sentinel) and mixes
#   schema metadata into the same merge/hard-delete lifecycle as actual data
#   rows, which is the wrong semantics (a comment isn't "deleted" the way a
#   row is).
#
# None of these have been validated against how cognee's document/graph
# extraction actually wants schema context shaped — please confirm the right
# shape before relying on this in production.
def sync_table_comments(client: Any, *, database: str, table: str) -> Iterator[dict[str, Any]]:
    """Yield a single synthetic row summarizing the table's + columns' comments."""
    table_result = client.query(
        "SELECT comment FROM system.tables WHERE database = %(database)s AND name = %(table)s",
        parameters={"database": database, "table": table},
    )
    table_comment = ""
    if table_result.result_rows:
        table_comment = table_result.result_rows[0][0] or ""

    columns_result = client.query(
        "SELECT name, comment FROM system.columns "
        "WHERE database = %(database)s AND table = %(table)s AND comment != ''",
        parameters={"database": database, "table": table},
    )
    column_comments = dict(columns_result.result_rows)

    if not table_comment and not column_comments:
        return

    yield {
        "table": table,
        "table_comment": table_comment,
        "column_comments": column_comments,
    }


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
    table: str | None = None,
    id_column: str = "id",
    cursor_column: str | None = None,
    include_table_comments: bool = True,
    client: Any = None,
):
    """Return a ``dlt`` source that yields ClickHouse rows for ``remember``.

    Any argument left as ``None`` falls back to the matching ``CLICKHOUSE_*``
    environment variable. Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"`` and ``primary_key=id_column``.

    Args:
        host: ClickHouse server host.
        port: ClickHouse HTTP(S) port (e.g. 8123 plaintext, 8443 TLS).
        user: Username for HTTP basic auth.
        password: Password for HTTP basic auth.
        database: Database containing ``table``.
        table: Table to sync.
        id_column: Primary-key column name (default ``"id"``).
        cursor_column: Monotonic column used as the incremental cursor (e.g.
            an ``updated_at`` timestamp or an auto-increment id). Required.
        include_table_comments: Also sync table/column ``COMMENT``s as a
            separate resource (see the big comment above
            ``sync_table_comments``).
        client: Pre-built ``clickhouse-connect`` client. Mainly an injection
            point for tests; when omitted a client is built from the
            connection settings above.

    Returns:
        A ``dlt`` source (``clickhouse``) bundling the ``clickhouse_rows``
        resource (``primary_key=id_column``, ``write_disposition="merge"``, an
        ``_deleted`` hard-delete column) and, when ``include_table_comments``
        is true, the ``clickhouse_table_comments`` resource.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_table = table or os.getenv("CLICKHOUSE_TABLE")
    if not resolved_table:
        raise ValueError("table is required (pass it explicitly or set CLICKHOUSE_TABLE).")

    resolved_cursor_column = cursor_column or os.getenv("CLICKHOUSE_CURSOR_COLUMN")
    if not resolved_cursor_column:
        raise ValueError(
            "cursor_column is required (pass it explicitly or set CLICKHOUSE_CURSOR_COLUMN)."
        )

    resolved_database = database or os.getenv("CLICKHOUSE_DATABASE")
    if client is None and not resolved_database:
        raise ValueError("database is required (pass it explicitly or set CLICKHOUSE_DATABASE).")

    connection_kwargs = {
        "host": host or os.getenv("CLICKHOUSE_HOST", "localhost"),
        "port": int(port if port is not None else os.getenv("CLICKHOUSE_PORT", "8123")),
        "user": user or os.getenv("CLICKHOUSE_USER", "default"),
        "password": password or os.getenv("CLICKHOUSE_PASSWORD", ""),
        "database": resolved_database,
    }

    @dlt.resource(
        name="clickhouse_rows",
        primary_key=id_column,
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickhouse_rows():
        db_client = client or build_clickhouse_client(**connection_kwargs)
        resource_state = dlt.current.resource_state()
        yield from sync_rows(
            db_client,
            resource_state,
            table=resolved_table,
            id_column=id_column,
            cursor_column=resolved_cursor_column,
        )

    @dlt.resource(name="clickhouse_table_comments", write_disposition="replace")
    def clickhouse_table_comments():
        if not include_table_comments:
            return
        db_client = client or build_clickhouse_client(**connection_kwargs)
        yield from sync_table_comments(db_client, database=resolved_database, table=resolved_table)

    @dlt.source(name="clickhouse")
    def _clickhouse():
        if include_table_comments:
            return clickhouse_rows, clickhouse_table_comments
        return (clickhouse_rows,)

    return _clickhouse()
