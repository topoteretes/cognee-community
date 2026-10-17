"""DLT source for Amazon Redshift tables and queries (timestamp cursor + forget-on-delete).

Syncs selected Redshift tables, views and SQL queries into cognee, plus a
"table card" per table with its comment and its column comments::

    import cognee
    from cognee_community_connector_redshift import RedshiftTable, redshift_source

    await cognee.remember(
        redshift_source(
            [RedshiftTable("public.tickets", primary_key="id", cursor_column="updated_at")]
        ),
        dataset_name="redshift",
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )

Design
------
* **Transport** — the Redshift Data API by default (``DataApiConnection``).
  It is an HTTPS endpoint, so a cluster inside a private VPC needs no
  tunnel, inbound route or driver, and IAM, temporary database credentials
  and Secrets Manager all work. ``DirectConnection`` is the fallback for a
  driver connection through an SSH tunnel.
* **Documents** — each row becomes a short text document (one ``column:
  value`` line per column) that flows through normal cognify. A table and
  its rows share a node set (``redshift:table:public.tickets``), so they are
  linked in the graph. Each table also gets a card with its comment, its
  columns and their types and comments, which tells the LLM what the
  columns mean. One staging table per selection, so cleanup stays scoped
  to it.
* **Incremental sync** — ``cursor_column`` is a timestamp (or any
  increasing value). A run reads rows with ``cursor >= last value``,
  ordered by the cursor, and keeps the last value seen. ``>=`` re-reads the
  rows at the boundary instead of skipping rows that share it; re-reading
  an unchanged row is harmless, its document does not change. Without a
  cursor every run reads the whole selection.
* **Forget-on-delete** — Redshift has no change feed, so each incremental
  run also reads the primary keys of the selection (one column, cheap on a
  columnar store). Keys that disappeared are emitted as ``_deleted``
  tombstones, so dlt drops the rows and cognee's orphan cleanup removes
  them from the graph and vector stores. Keys that appeared below the
  cursor (a late load) are fetched by key. A ``where`` filter defines what
  is selected, so a row that stops matching it (a soft delete) is forgotten
  too. A table dropped upstream, or one removed from the selection, is
  forgotten whole.
* **Errors never delete** — a failed or timed-out statement aborts the run
  before dlt commits rows or state, so nothing is forgotten and the next
  run starts from the same cursor.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

from .connection import DataApiConnection, DirectConnection

logger = get_logger("redshift_connector")

REDSHIFT_SOURCE_NAME = "redshift"
DEFAULT_RESOURCE_NAME = "redshift"

# Result columns the connector adds next to the selected ones. Keys and the
# cursor are cast to text by Redshift, so both transports see the same string.
_KEY_ALIAS = "cognee__key_"
_CURSOR_ALIAS = "cognee__cursor"
_TEXT = "VARCHAR(65535)"
# Keys fetched per statement when late rows are read by key.
_KEY_BATCH = 100

# Bump when rendering or the state layout changes, so the next run re-reads
# every row instead of keeping documents rendered by an older connector.
_STATE_VERSION = 1


@dataclass(frozen=True)
class RedshiftTable:
    """A table or view to sync.

    Args:
        name: ``schema.table``; a bare name means the ``public`` schema. Use
            the name as Redshift stores it (lower case unless it was quoted).
        primary_key: Column(s) that identify a row. Redshift does not enforce
            primary keys, so pick columns that really are unique.
        cursor_column: A timestamp (or other increasing) column that changes
            whenever the row changes, such as ``updated_at``. Without it every
            run reads the whole table.
        columns: Columns to ingest (the key and cursor are always read).
            Defaults to every column.
        where: A SQL condition that selects the rows to ingest, such as
            ``"deleted_at IS NULL"``. A row that stops matching is forgotten.
    """

    name: str
    primary_key: str | Sequence[str]
    cursor_column: str | None = None
    columns: Sequence[str] | None = None
    where: str | None = None


@dataclass(frozen=True)
class RedshiftQuery:
    """The result of a SQL query to sync, such as a join or an aggregate.

    Args:
        name: A short stable name for the query; it identifies its documents.
        sql: A ``SELECT`` statement. It runs as a subquery, so do not end it
            with ``;`` and do not use ``:cognee_*`` names in it.
        primary_key: Result column(s) that identify a result row.
        cursor_column: An increasing result column, as for ``RedshiftTable``.
    """

    name: str
    sql: str
    primary_key: str | Sequence[str]
    cursor_column: str | None = None


@dataclass(frozen=True)
class _Selection:
    """One table or query, normalized into what the SQL builders need."""

    label: str
    staging_table: str
    relation: str
    schema: str | None
    table: str | None
    primary_key: tuple[str, ...]
    cursor_column: str | None
    columns: tuple[str, ...] | None
    where: str | None

    @property
    def signature(self) -> str:
        """Changes when the selection would read different rows or columns."""
        payload = [
            _STATE_VERSION,
            self.relation,
            self.primary_key,
            self.cursor_column,
            self.columns,
            self.where,
        ]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]

    @property
    def node_set(self) -> str:
        return f"table:{self.label}" if self.table else f"query:{self.label}"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def redshift_source(
    selections: Sequence[RedshiftTable | RedshiftQuery],
    *,
    connection: DataApiConnection | DirectConnection | None = None,
    include_table_cards: bool = True,
    full_refresh: bool = False,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    check_active: Callable[[], None] | None = None,
):
    """Create a dlt source that yields Redshift rows and table cards as documents.

    Args:
        selections: The tables, views and queries to ingest.
        connection: How to reach Redshift. Defaults to a ``DataApiConnection``
            configured from the ``REDSHIFT_*`` environment variables.
        include_table_cards: Also ingest one document per table with its
            comment and its columns, types and column comments.
        full_refresh: Read every row this run instead of only rows past the
            cursor, for example after updates that did not touch the cursor.
        resource_name: Stable name for this connection's dlt state and staging
            tables. Give each cluster its own name when several sync into one
            dataset.
        check_active: Called between rows; raises to stop a sync whose access
            was revoked mid-run (for hosts).

    Returns:
        A dlt source for ``cognee.remember(...)``. Pass
        ``write_disposition="merge"``: incremental runs emit only changes and
        tombstones, and any other disposition is refused.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Redshift connector requires a cognee build with table-scoped document "
            "cleanup and per-row node sets (DOCUMENT_SYNC_VERSION 2, cognee 1.6.4)."
        )
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("resource_name must be lower case letters, digits and underscores.")

    normalized = _normalize_selections(selections, resource_name)
    connection = connection or DataApiConnection()
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def redshift_rows():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of an incremental read would empty every table but the
        # changed rows, so refuse it before reading anything.
        disposition = source.resources[resource_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'redshift_source needs write_disposition="merge", got {disposition!r}. '
                "Its incremental runs emit only changes and deletions."
            )
        try:
            rows = sync_rows(
                connection,
                normalized,
                dlt.current.resource_state(),
                resource_name=resource_name,
                include_table_cards=include_table_cards,
                full_refresh=full_refresh,
                stats=stats,
            )
            for table_name, row in guarded_rows(rows, check_active):
                yield dlt.mark.with_table_name(row, table_name)
        finally:
            connection.close()

    @dlt.source(name=REDSHIFT_SOURCE_NAME)
    def _redshift():
        return redshift_rows

    source = _redshift()
    # Opt into the document path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, REDSHIFT_SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so its cursors are not
    # shared with unrelated dlt sources.
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name)
    # Counts of the last run, for hosts that record how a sync went. Never
    # row content.
    source.cognee_sync_stats = stats
    return source


def _normalize_selections(
    selections: Sequence[RedshiftTable | RedshiftQuery], resource_name: str
) -> tuple[_Selection, ...]:
    if isinstance(selections, (RedshiftTable, RedshiftQuery)):
        selections = [selections]
    if not selections:
        # An empty selection would forget everything synced before, so refuse it.
        raise ValueError("Select at least one RedshiftTable or RedshiftQuery.")

    normalized: list[_Selection] = []
    for selection in selections:
        primary_key = _names(selection.primary_key, "primary_key")
        cursor = selection.cursor_column.strip() if selection.cursor_column else None
        if isinstance(selection, RedshiftTable):
            schema, _, table = selection.name.strip().rpartition(".")
            schema = schema or "public"
            if not table or "." in schema:
                raise ValueError(f"Table name {selection.name!r} must be 'schema.table'.")
            label = f"{schema}.{table}"
            columns = None
            if selection.columns is not None:
                wanted = [*primary_key, *([cursor] if cursor else []), *selection.columns]
                columns = tuple(dict.fromkeys(_names(wanted, "columns")))
            normalized.append(
                _Selection(
                    label=label,
                    staging_table=_staging_table(resource_name, label),
                    relation=f"{_quote(schema)}.{_quote(table)}",
                    schema=schema,
                    table=table,
                    primary_key=primary_key,
                    cursor_column=cursor,
                    columns=columns,
                    where=selection.where.strip() if selection.where else None,
                )
            )
        elif isinstance(selection, RedshiftQuery):
            name = selection.name.strip()
            sql = selection.sql.strip().rstrip(";").strip()
            if not name or not sql:
                raise ValueError("RedshiftQuery needs a name and sql.")
            normalized.append(
                _Selection(
                    label=name,
                    staging_table=_staging_table(resource_name, f"query_{name}"),
                    relation=f"({sql}) AS cognee__query",
                    schema=None,
                    table=None,
                    primary_key=primary_key,
                    cursor_column=cursor,
                    columns=None,
                    where=None,
                )
            )
        else:
            raise TypeError(f"Expected RedshiftTable or RedshiftQuery, got {selection!r}.")

    for attribute in ("label", "staging_table"):
        values = [getattr(selection, attribute) for selection in normalized]
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ValueError(f"Selections must be unique; {duplicates} appear more than once.")
    return tuple(normalized)


def _names(value: str | Sequence[str], argument: str) -> tuple[str, ...]:
    names = (value,) if isinstance(value, str) else tuple(value)
    cleaned = tuple(name.strip() for name in names)
    if not cleaned or not all(cleaned):
        raise ValueError(f"{argument} needs at least one column name.")
    return cleaned


def _staging_table(resource_name: str, label: str) -> str:
    return f"{resource_name}_{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}"


def _cards_table(resource_name: str) -> str:
    return f"{resource_name}_table_cards"


# ---------------------------------------------------------------------------
# Sync (pure given a connection + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_rows(
    connection: Any,
    selections: Sequence[_Selection],
    state: dict,
    *,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    include_table_cards: bool = True,
    full_refresh: bool = False,
    stats: dict | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield ``(staging_table, row)`` for every changed row, card and tombstone.

    ``state`` stands in for dlt's resource state. It is only written after
    the last statement has succeeded, and dlt only commits it together with
    the rows, so a failed run changes nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(rows=0, deleted=0, late=0, skipped=0, duplicates=0, selections=0)

    previous: dict[str, dict] = state.get("selections", {})
    current: dict[str, dict] = {}
    cards_table = _cards_table(resource_name)
    previous_cards: set[str] = set(state.get("cards", []))
    current_cards: set[str] = set()
    tombstones: list[tuple[str, str]] = []

    # Selections removed from the config: their rows are out of scope now.
    labels = {selection.label for selection in selections}
    for label, selection_state in previous.items():
        if label not in labels:
            _forget(resource_name, label, selection_state, tombstones, stats)

    tables = [selection for selection in selections if selection.table]
    catalog = _read_catalog(connection, tables) if tables else {}

    for selection in selections:
        selection_state = previous.get(selection.label, {})
        if selection.table:
            entry = catalog.get((selection.schema, selection.table))
            if entry is None:
                if not selection_state:
                    raise ValueError(
                        f"Redshift table {selection.label} was not found, or this user cannot "
                        "see it. Check the name (lower case unless it was created quoted) "
                        "and the user's privileges."
                    )
                # It was synced before, so it was dropped or its access revoked.
                logger.warning(
                    "Redshift: table %s no longer exists; forgetting it.", selection.label
                )
                _forget(resource_name, selection.label, selection_state, tombstones, stats)
                continue
            if include_table_cards:
                current_cards.add(selection.label)
                yield cards_table, _card_row(resource_name, selection, entry)

        new_state = yield from _sync_selection(
            connection, selection, selection_state, resource_name, full_refresh, tombstones, stats
        )
        current[selection.label] = new_state
        stats["selections"] += 1

    for label in sorted(previous_cards - current_cards):
        stats["deleted"] += 1
        tombstones.append((cards_table, _card_id(resource_name, label)))

    for staging_table, row_id in tombstones:
        yield staging_table, {"id": row_id, "_deleted": True}

    state["selections"] = current
    state["cards"] = sorted(current_cards)
    logger.info("Redshift: sync stats %s", stats)


def _sync_selection(
    connection, selection, selection_state, resource_name, full_refresh, tombstones, stats
):
    """Sync one selection. Yields its changed rows and returns its new state."""
    known: set[str] = set(selection_state.get("keys", []))
    cursor = selection_state.get("cursor")
    incremental = bool(
        selection.cursor_column
        and cursor is not None
        and not full_refresh
        and selection_state.get("signature") == selection.signature
    )

    # Keys first: a row inserted after this read is fetched as a change or on
    # the next run; it can never be mistaken for a deletion.
    inventory = _read_keys(connection, selection) if incremental else None

    fetched: set[str] = set()
    duplicates_before = stats["duplicates"]
    sql = _rows_sql(selection, since=incremental)
    params = {"cognee_cursor": cursor} if incremental else None
    for row in connection.query(sql, params):
        value = row.get(_CURSOR_ALIAS)
        if value is not None:
            # Rows come ordered by the cursor, so the last value is the largest.
            cursor = value
        result = _row(resource_name, selection, row, fetched, stats)
        if result is not None:
            yield selection.staging_table, result

    if inventory is not None:
        # Keys below the cursor that were never synced: a late load, or rows
        # that only now match the where filter. Fetch them by key.
        late = sorted(inventory - known - fetched)
        for start in range(0, len(late), _KEY_BATCH):
            batch = late[start : start + _KEY_BATCH]
            sql, params = _rows_by_key_sql(selection, batch)
            for row in connection.query(sql, params):
                result = _row(resource_name, selection, row, fetched, stats)
                if result is not None:
                    stats["late"] += 1
                    yield selection.staging_table, result
        present = inventory | fetched
    else:
        # A full read lists every selected row, so it is the inventory.
        present = fetched

    if stats["duplicates"] > duplicates_before:
        logger.warning(
            "Redshift: %s has %d row(s) with a primary key already read; kept the first. "
            "Check that primary_key is unique.",
            selection.label,
            stats["duplicates"] - duplicates_before,
        )

    for key in sorted(known - present):
        stats["deleted"] += 1
        tombstones.append((selection.staging_table, _row_id(resource_name, selection.label, key)))

    return {
        "signature": selection.signature,
        "staging_table": selection.staging_table,
        "cursor": cursor if selection.cursor_column else None,
        "keys": sorted((known & present) | fetched),
    }


def _row(resource_name, selection, row, fetched, stats) -> dict | None:
    """Render one result row, or None when it cannot be identified."""
    key = _key(selection, row)
    if key is None:
        stats["skipped"] += 1
        return None
    if key in fetched:
        # Redshift does not enforce primary keys. Keep the first row, so dlt's
        # merge never has to pick between two rows with one id.
        stats["duplicates"] += 1
        return None
    fetched.add(key)
    stats["rows"] += 1
    return _row_document(resource_name, selection, row, key)


def _forget(resource_name, label, selection_state, tombstones, stats) -> None:
    """Tombstone every row a selection synced before."""
    for key in selection_state.get("keys", []):
        stats["deleted"] += 1
        tombstones.append((selection_state["staging_table"], _row_id(resource_name, label, key)))


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _key_columns(selection: _Selection) -> list[str]:
    return [
        f"CAST({_quote(column)} AS {_TEXT}) AS {_quote(f'{_KEY_ALIAS}{index}')}"
        for index, column in enumerate(selection.primary_key)
    ]


def _select(selection: _Selection, conditions: list[str], *, ordered: bool) -> str:
    columns = ["*"] if selection.columns is None else [_quote(c) for c in selection.columns]
    columns += _key_columns(selection)
    if selection.cursor_column:
        columns.append(
            f"CAST({_quote(selection.cursor_column)} AS {_TEXT}) AS {_quote(_CURSOR_ALIAS)}"
        )
    if selection.where:
        conditions = [f"({selection.where})", *conditions]
    sql = f"SELECT {', '.join(columns)} FROM {selection.relation}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if ordered and selection.cursor_column:
        sql += f" ORDER BY {_quote(selection.cursor_column)} ASC NULLS FIRST"
    return sql


def _rows_sql(selection: _Selection, *, since: bool) -> str:
    conditions = [f"{_quote(selection.cursor_column)} >= :cognee_cursor"] if since else []
    return _select(selection, conditions, ordered=True)


def _rows_by_key_sql(selection: _Selection, keys: list[str]) -> tuple[str, dict[str, str]]:
    params: dict[str, str] = {}
    matches = []
    for row_index, key in enumerate(keys):
        values = _key_values(selection, key)
        parts = []
        for column_index, (column, value) in enumerate(
            zip(selection.primary_key, values, strict=True)
        ):
            name = f"cognee_key_{row_index}_{column_index}"
            params[name] = value
            parts.append(f"CAST({_quote(column)} AS {_TEXT}) = :{name}")
        matches.append("(" + " AND ".join(parts) + ")")
    return _select(selection, ["(" + " OR ".join(matches) + ")"], ordered=False), params


def _read_keys(connection, selection: _Selection) -> set[str]:
    sql = f"SELECT {', '.join(_key_columns(selection))} FROM {selection.relation}"
    if selection.where:
        sql += f" WHERE ({selection.where})"
    keys = set()
    for row in connection.query(sql):
        key = _key(selection, row)
        if key is not None:
            keys.add(key)
    return keys


def _read_catalog(connection, tables: Sequence[_Selection]) -> dict[tuple[str, str], dict]:
    """Comments, columns and existence of the selected tables, from SVV_ALL_*."""
    params = {"cognee_database": connection.database}
    matches = []
    for index, selection in enumerate(tables):
        params[f"cognee_schema_{index}"] = selection.schema
        params[f"cognee_table_{index}"] = selection.table
        matches.append(
            f"(schema_name = :cognee_schema_{index} AND table_name = :cognee_table_{index})"
        )
    where = "database_name = :cognee_database AND (" + " OR ".join(matches) + ")"

    catalog: dict[tuple[str, str], dict] = {}
    for row in connection.query(
        f"SELECT schema_name, table_name, table_type, remarks FROM svv_all_tables WHERE {where}",
        params,
    ):
        catalog[(row["schema_name"], row["table_name"])] = {
            "type": row.get("table_type"),
            "remarks": row.get("remarks"),
            "columns": [],
        }
    if not catalog:
        return catalog
    for row in connection.query(
        "SELECT schema_name, table_name, column_name, data_type, remarks FROM svv_all_columns "
        f"WHERE {where} ORDER BY schema_name, table_name, ordinal_position",
        params,
    ):
        entry = catalog.get((row["schema_name"], row["table_name"]))
        if entry is not None:
            entry["columns"].append(row)
    return catalog


# ---------------------------------------------------------------------------
# Identity and rendering
# ---------------------------------------------------------------------------
def _key(selection: _Selection, row: dict) -> str | None:
    values = [row.get(f"{_KEY_ALIAS}{index}") for index in range(len(selection.primary_key))]
    # The Data API cannot pass an empty string as a parameter, so an empty
    # key could never be fetched by key; treat it like a missing one.
    if any(value is None or value == "" for value in values):
        return None
    values = [str(value) for value in values]
    return values[0] if len(values) == 1 else json.dumps(values)


def _key_values(selection: _Selection, key: str) -> list[str]:
    return [key] if len(selection.primary_key) == 1 else json.loads(key)


def _row_id(resource_name: str, label: str, key: str) -> str:
    return f"{resource_name}:{label}:{key}"


def _card_id(resource_name: str, label: str) -> str:
    return f"{resource_name}:table:{label}"


def _row_document(resource_name: str, selection: _Selection, row: dict, key: str) -> dict:
    kind = "table" if selection.table else "query"
    key_text = ", ".join(
        f"{column} = {value}"
        for column, value in zip(selection.primary_key, _key_values(selection, key), strict=True)
    )
    lines = [f"Row of Redshift {kind} {selection.label} ({key_text})", ""]
    for column, value in row.items():
        if column.startswith(_KEY_ALIAS) or column == _CURSOR_ALIAS:
            continue
        if value is None or value == "":
            continue
        lines.append(f"{column}: {_text(value)}")
    return {
        "id": _row_id(resource_name, selection.label, key),
        "title": f"{selection.label} {key}",
        "content": "\n".join(lines),
        NODE_SET_COLUMN: [selection.node_set],
        "_deleted": False,
    }


def _card_row(resource_name: str, selection: _Selection, entry: dict) -> dict:
    kind = (entry.get("type") or "table").lower()
    lines = [f"Redshift {kind} {selection.label}"]
    if entry.get("remarks"):
        lines.append(f"Description: {entry['remarks']}")
    lines.append(f"Primary key: {', '.join(selection.primary_key)}")
    if selection.cursor_column:
        lines.append(f"Updated-at column: {selection.cursor_column}")
    if selection.where:
        lines.append(f"Ingested rows: {selection.where}")
    if entry["columns"]:
        lines.append("Columns:")
        for column in entry["columns"]:
            line = f"- {column['column_name']} ({column['data_type']})"
            if column.get("remarks"):
                line += f": {column['remarks']}"
            lines.append(line)
    return {
        "id": _card_id(resource_name, selection.label),
        "title": f"Table {selection.label}",
        "content": "\n".join(lines),
        NODE_SET_COLUMN: [selection.node_set],
        "_deleted": False,
    }


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
