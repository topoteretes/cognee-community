"""BigQuery connector for cognee — a ``dlt`` source that turns a warehouse into memory.

Sync BigQuery metadata (and, opt-in, selected table rows) into cognee,
incrementally and with forget-on-delete::

    import cognee
    from cognee_community_connector_bigquery import RowSync, bigquery_source

    await cognee.remember(
        bigquery_source(
            project="my-project",
            datasets=["analytics"],
            row_syncs=[RowSync(table="analytics.customers", key_column="id",
                               cursor_column="updated_at")],
        ),
        dataset_name="bigquery",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. cognee applies the caller's
   write disposition to every table of the source, and this source deletes by
   emitting ``_deleted`` tombstone rows, which only ``merge`` turns into
   deletions. With the default ``replace`` the tombstones would be loaded as
   empty rows and incremental row syncs would drop unchanged rows.

Design
------
* **Auth** — a service-account key (``credentials_path`` or
  ``GOOGLE_APPLICATION_CREDENTIALS``); without one, Application Default
  Credentials. The account needs only *BigQuery Data Viewer* and *BigQuery Job
  User*. The connector only reads.
* **Metadata first** — one document per dataset and per table/view: its
  description, labels, partitioning/clustering, view SQL and every column with
  its type, mode and description (nested ``RECORD`` fields included). For a
  memory layer this is usually worth more than the rows. Metadata comes from
  ``list``/``get`` API calls, which bill no query bytes, so it is re-read in full
  each run; documents whose text did not change keep a stable content hash and
  are not re-cognified.
* **Rows, opt-in** — per table, via :class:`RowSync`: one document per row, keyed
  by a column you name. With a ``cursor_column`` (TIMESTAMP, DATETIME, DATE or
  INTEGER that grows when a row changes) later runs query only rows at or after
  the last value seen; the cursor is kept in dlt's per-resource state.
* **Forget-on-delete** — a table, view or dataset that disappears from the
  listing, and a row whose key disappears from a key-only sweep query (which
  BigQuery bills for the key column only), is emitted with the ``_deleted``
  hard-delete marker. dlt drops it on ``merge`` and cognee's orphan cleanup
  removes it from the graph and vector stores.
* **Cost guard** — every query is dry-run first; if the estimate exceeds
  ``maximum_bytes_billed`` the sync stops before any bytes are billed, and the
  same cap is set on the real job so BigQuery enforces it too.

Limitations
-----------
* The row deletion sweep keeps the current keys of each synced table in dlt
  state, so row sync is meant for tables up to roughly a few hundred thousand
  rows. Metadata sync has no such limit.
* A row whose cursor column is not updated on change is not picked up until its
  key is re-read by a full sync (drop the dataset's dlt state or omit
  ``cursor_column``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("bigquery_connector")

BIGQUERY_SOURCE_NAME = "bigquery"
METADATA_TABLE_NAME = "bigquery_metadata"
ROWS_TABLE_NAME = "bigquery_rows"

# Default per-query cap: 1 GB, well inside BigQuery's free monthly query tier.
DEFAULT_MAXIMUM_BYTES_BILLED = 1_000_000_000

# Column types a row cursor may use, mapped to the BigQuery query-parameter type.
_STAT_KEYS = ("scanned", "changed", "deleted", "failed", "requests")

_CURSOR_PARAM_TYPES = {
    "TIMESTAMP": "TIMESTAMP",
    "DATETIME": "DATETIME",
    "DATE": "DATE",
    "INTEGER": "INT64",
    "INT64": "INT64",
}

_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}


@dataclass(frozen=True)
class RowSync:
    """Opt-in row ingestion for one table.

    Attributes:
        table: ``dataset.table`` inside the source's project.
        key_column: Column that uniquely identifies a row. It becomes part of the
            document id and drives the deletion sweep.
        cursor_column: TIMESTAMP, DATETIME, DATE or INTEGER column that increases
            whenever a row changes. When set, runs after the first query only rows
            at or after the last value seen. When omitted, every run re-reads the
            whole table.
        columns: Columns rendered into each row document; ``None`` renders all.
        title_column: Column used as the document title; defaults to
            ``"<table> <key>"``.
    """

    table: str
    key_column: str
    cursor_column: str | None = None
    columns: Sequence[str] | None = None
    title_column: str | None = None


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def bigquery_source(
    project: str | None = None,
    datasets: Sequence[str] | None = None,
    tables: Sequence[str] | None = None,
    row_syncs: Sequence[RowSync] | None = None,
    credentials_path: str | None = None,
    maximum_bytes_billed: int = DEFAULT_MAXIMUM_BYTES_BILLED,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Create a dlt source that yields BigQuery metadata (and opt-in rows) as documents.

    Args:
        project: GCP project to read. Defaults to the credentials' project.
        datasets: Restrict metadata to these dataset ids. ``None`` (and no
            ``tables``) reads every dataset in the project.
        tables: Restrict metadata to these ``dataset.table`` ids.
        row_syncs: Tables whose rows are ingested too, one :class:`RowSync` each.
        credentials_path: Service-account key file. Falls back to
            ``GOOGLE_APPLICATION_CREDENTIALS``, then Application Default Credentials.
        maximum_bytes_billed: Per-query byte cap for row queries.
        check_active: Optional host checkpoint, called before every BigQuery API call
            and before every row is published; raising from it stops the run.
        service: Pre-built ``google.cloud.bigquery.Client`` (tests, proxies). It is used
            as is and not closed; when omitted, each run builds and closes its own.
        client: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only, summed over metadata and
        rows: ``scanned``, ``changed``, ``deleted``, ``failed`` (rows with a NULL key),
        ``requests`` (API calls), and ``complete`` (1 once both finished).
    """
    if service is not None and client is not None:
        raise ValueError("Pass service= or client=, not both.")
    injected = service if service is not None else client
    if project is None:
        if injected is not None:
            project = injected.project
        else:
            probe = _make_client(None, credentials_path)
            project = probe.project
            probe.close()
    if not project:
        raise ValueError("BigQuery project required: pass project= or use credentials with one.")
    scope = _build_scope(datasets, tables)
    row_specs = list(row_syncs or [])
    stats: dict[str, int] = {}
    run = _RunStats(stats, ("metadata", "rows"))

    def _run(name: str, sync: Callable[[Any, dict, dict], Iterator[dict]]) -> Iterator[dict]:
        part = run.start(name)
        state = dlt.current.resource_state()
        bq = injected if injected is not None else _make_client(project, credentials_path)
        try:
            yield from sync(_CheckedClient(bq, check_active, part), state, part)
        finally:
            if injected is None:
                bq.close()
        run.finish(name)

    @dlt.resource(
        name=METADATA_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def bigquery_metadata():
        def sync(bq, state, part):
            return sync_metadata(bq, project, scope, state, part, check_active)

        yield from _run("metadata", sync)

    @dlt.resource(
        name=ROWS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def bigquery_rows():
        def sync(bq, state, part):
            return sync_rows(
                bq, project, row_specs, maximum_bytes_billed, state, part, check_active
            )

        yield from _run("rows", sync)

    @dlt.source(name=BIGQUERY_SOURCE_NAME)
    def _bigquery():
        # The rows resource runs even with no row_syncs, so rows of a table that
        # was removed from the configuration are still forgotten.
        return [bigquery_metadata, bigquery_rows]

    source = _bigquery()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, BIGQUERY_SOURCE_NAME)
    # One dlt state (cursor, known ids and keys) per project and cognee dataset.
    # Scoped by project, not by the selection, so narrowing datasets/tables or
    # removing a RowSync tombstones what dropped out instead of orphaning it.
    setattr(source, PIPELINE_SCOPE_ATTR, f"{BIGQUERY_SOURCE_NAME}:{project}")
    source.cognee_sync_stats = stats
    return source


class BigQuerySyncError(RuntimeError):
    """The run cannot be trusted (an emptied project listing) and stops."""


class _CheckedClient:
    """Proxy that asks the host checkpoint and counts calls before each client method."""

    def __init__(self, client: Any, check_active: Callable[[], None] | None, stats: dict):
        self._client = client
        self._check_active = check_active
        self._stats = stats

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self._check_active is not None:
                self._check_active()
            self._stats["requests"] = self._stats.get("requests", 0) + 1
            return attr(*args, **kwargs)

        return call


class _RunStats:
    """Per-resource counts, published as flat totals on the source after each step."""

    def __init__(self, stats: dict, names: tuple[str, ...]):
        self.stats = stats
        self.parts: dict[str, dict] = {name: {} for name in names}

    def start(self, name: str) -> dict:
        part = self.parts[name]
        part.clear()
        part.update(dict.fromkeys(_STAT_KEYS, 0), complete=0)
        self._publish()
        return part

    def finish(self, name: str) -> None:
        self.parts[name]["complete"] = 1
        self._publish()

    def _publish(self) -> None:
        totals = {key: sum(part.get(key, 0) for part in self.parts.values()) for key in _STAT_KEYS}
        self.stats.clear()
        self.stats.update(totals)
        self.stats["complete"] = int(all(part.get("complete") for part in self.parts.values()))


def _checked(row: dict, check_active: Callable[[], None] | None) -> dict:
    """Ask the host checkpoint before a row is published."""
    if check_active is not None:
        check_active()
    return row


def _make_client(project: str | None, credentials_path: str | None) -> Any:
    """Build a BigQuery client from a key file, or from Application Default Credentials."""
    from google.cloud import bigquery

    key_path = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_path:
        return bigquery.Client.from_service_account_json(key_path, project=project)
    return bigquery.Client(project=project)


def _build_scope(
    datasets: Sequence[str] | None, tables: Sequence[str] | None
) -> dict[str, list[str] | None] | None:
    """Map dataset id → selected table ids (``None`` = every table in it).

    Returns ``None`` when nothing is selected, meaning every dataset in the project.
    """
    if not datasets and not tables:
        return None
    scope: dict[str, list[str] | None] = dict.fromkeys(datasets or [])
    for table in tables or []:
        dataset_id, _, table_id = table.partition(".")
        if not dataset_id or not table_id or "." in table_id:
            raise ValueError(f"tables entries must look like 'dataset.table', got {table!r}.")
        if dataset_id in scope and scope[dataset_id] is None:
            continue  # the whole dataset is already selected
        scope.setdefault(dataset_id, []).append(table_id)
    return scope


# ---------------------------------------------------------------------------
# Metadata: datasets, tables and views
# ---------------------------------------------------------------------------
def sync_metadata(
    client: Any,
    project: str,
    scope: dict[str, list[str] | None] | None,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield one document row per dataset and table, then tombstones for vanished ones.

    Everything in scope is re-read each run (API calls bill no query bytes), so a
    dataset or table missing from this run's listing is gone, and its id from the
    previous run is emitted with ``_deleted=True``. Errors other than "not found"
    propagate and abort the run, leaving state and memory untouched.
    """
    stats = {} if stats is None else stats
    known_ids = set(state.get("known_ids", []))
    current_ids: set[str] = set()

    if scope is None:
        dataset_ids = sorted(item.dataset_id for item in client.list_datasets(project=project))
        if not dataset_ids and known_ids and state.get("whole_project"):
            # Listing a whole project that had datasets and getting none back is far
            # likelier a credentials or project mix-up than a project emptied overnight.
            raise BigQuerySyncError(
                f"BigQuery listed no datasets in {project}, but the last run synced "
                f"{len(known_ids)} documents from it. Refusing to forget them; check the "
                "credentials and project. To forget them, pass datasets= or tables=."
            )
    else:
        dataset_ids = sorted(scope)

    for dataset_id in dataset_ids:
        dataset = _get_or_none(client.get_dataset, f"{project}.{dataset_id}")
        if dataset is None:
            logger.warning("BigQuery: dataset %s.%s not found, skipping.", project, dataset_id)
            continue
        selected = scope.get(dataset_id) if scope is not None else None
        if selected is None:
            table_ids = sorted(
                item.table_id for item in client.list_tables(f"{project}.{dataset_id}")
            )
        else:
            table_ids = sorted(set(selected))

        table_rows = []
        for table_id in table_ids:
            table = _get_or_none(client.get_table, f"{project}.{dataset_id}.{table_id}")
            if table is None:
                logger.warning(
                    "BigQuery: table %s.%s.%s not found, skipping.", project, dataset_id, table_id
                )
                continue
            table_rows.append(table_to_row(table))

        dataset_row = dataset_to_row(dataset, [row["id"] for row in table_rows])
        for row in [dataset_row, *table_rows]:
            current_ids.add(row["id"])
            yield _checked(row, check_active)

    deleted = known_ids - current_ids
    for doc_id in sorted(deleted):
        yield _checked({"id": doc_id, "_deleted": True}, check_active)

    state["known_ids"] = sorted(current_ids)
    state["whole_project"] = scope is None
    for key, value in (("scanned", len(current_ids)), ("changed", len(current_ids))):
        stats[key] = stats.get(key, 0) + value
    stats["deleted"] = stats.get("deleted", 0) + len(deleted)
    logger.info(
        "BigQuery: %d metadata document(s), %d deletion(s).", len(current_ids), len(deleted)
    )


def _get_or_none(getter: Any, ref: str) -> Any:
    """Call a BigQuery ``get_*`` method, returning None only when the object is gone."""
    from google.api_core.exceptions import NotFound

    try:
        return getter(ref)
    except NotFound:
        return None


def dataset_to_row(dataset: Any, table_ids: list[str]) -> dict:
    """Render a dataset into a document row (only stable, human-meaningful fields)."""
    full_id = f"{dataset.project}.{dataset.dataset_id}"
    lines = [f"BigQuery dataset `{full_id}`."]
    if dataset.friendly_name:
        lines.append(f"Name: {dataset.friendly_name}")
    if dataset.description:
        lines.append(f"Description: {dataset.description}")
    if dataset.location:
        lines.append(f"Location: {dataset.location}")
    if dataset.labels:
        lines.append(f"Labels: {_render_labels(dataset.labels)}")
    if table_ids:
        lines.append("Tables and views: " + ", ".join(f"`{table_id}`" for table_id in table_ids))
    return {
        "id": full_id,
        "title": full_id,
        "content": "\n".join(lines),
        "url": (
            "https://console.cloud.google.com/bigquery"
            f"?p={dataset.project}&d={dataset.dataset_id}&page=dataset"
        ),
        "_deleted": False,
    }


def table_to_row(table: Any) -> dict:
    """Render a table or view into a document row.

    Volatile fields (row count, size, modified time) are left out so a data load
    that does not change the schema or descriptions keeps the same content hash.
    """
    full_id = f"{table.project}.{table.dataset_id}.{table.table_id}"
    kind = (table.table_type or "TABLE").replace("_", " ").lower()
    lines = [f"BigQuery {kind} `{full_id}` in dataset `{table.project}.{table.dataset_id}`."]
    if table.friendly_name:
        lines.append(f"Name: {table.friendly_name}")
    if table.description:
        lines.append(f"Description: {table.description}")
    if table.labels:
        lines.append(f"Labels: {_render_labels(table.labels)}")
    if table.time_partitioning:
        field = table.time_partitioning.field or "ingestion time"
        lines.append(f"Partitioned by: {field} ({table.time_partitioning.type_})")
    if table.clustering_fields:
        lines.append("Clustered by: " + ", ".join(table.clustering_fields))
    if table.schema:
        lines.append("Columns:")
        lines.extend(_render_fields(table.schema))
    view_sql = table.view_query or table.mview_query
    if view_sql:
        lines.append(f"View definition:\n```sql\n{view_sql.strip()}\n```")
    return {
        "id": full_id,
        "title": full_id,
        "content": "\n".join(lines),
        "url": (
            "https://console.cloud.google.com/bigquery"
            f"?p={table.project}&d={table.dataset_id}&t={table.table_id}&page=table"
        ),
        "_deleted": False,
    }


def _render_fields(fields: Sequence[Any], prefix: str = "", depth: int = 0) -> list[str]:
    """Render schema fields as an indented list, recursing into RECORD fields."""
    lines = []
    for field in fields:
        name = f"{prefix}{field.name}"
        line = f"{'  ' * depth}- {name} ({field.field_type}, {field.mode or 'NULLABLE'})"
        if field.description:
            line += f": {field.description}"
        lines.append(line)
        if field.fields:
            lines.extend(_render_fields(field.fields, prefix=f"{name}.", depth=depth + 1))
    return lines


def _render_labels(labels: Mapping[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(labels.items()))


# ---------------------------------------------------------------------------
# Rows (opt-in)
# ---------------------------------------------------------------------------
def sync_rows(
    client: Any,
    project: str,
    row_specs: list[RowSync],
    maximum_bytes_billed: int,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows of every configured table, then tombstones for deleted keys.

    Tables that were synced before but are no longer configured have all their
    rows tombstoned, so dropping a RowSync forgets its rows.
    """
    stats = {} if stats is None else stats
    tables_state = state.setdefault("tables", {})
    configured = {_qualify(project, spec.table) for spec in row_specs}
    for full_id in sorted(set(tables_state) - configured):
        keys = tables_state.pop(full_id).get("keys", [])
        for key in keys:
            yield _checked({"id": _row_id(full_id, key), "_deleted": True}, check_active)
        stats["deleted"] = stats.get("deleted", 0) + len(keys)
    for spec in row_specs:
        full_id = _qualify(project, spec.table)
        yield from _sync_table_rows(
            client,
            full_id,
            spec,
            maximum_bytes_billed,
            tables_state.setdefault(full_id, {}),
            stats,
            check_active,
        )


def _sync_table_rows(
    client: Any,
    full_id: str,
    spec: RowSync,
    maximum_bytes_billed: int,
    table_state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    stats = {} if stats is None else stats
    known_keys = set(table_state.get("keys", []))
    table = _get_or_none(client.get_table, full_id)
    if table is None:
        # The table itself was dropped: every row synced from it is gone.
        logger.warning("BigQuery: row table %s not found; forgetting its rows.", full_id)
        for key in sorted(known_keys):
            yield _checked({"id": _row_id(full_id, key), "_deleted": True}, check_active)
        stats["deleted"] = stats.get("deleted", 0) + len(known_keys)
        table_state.clear()
        return

    columns = _select_columns(table, spec)
    from_clause = f"FROM {_quote_table(full_id)}"
    sql = f"SELECT {', '.join(_quote_ident(c) for c in columns)} {from_clause}"
    params = []
    last_cursor = table_state.get("cursor")
    if spec.cursor_column:
        param_type = _CURSOR_PARAM_TYPES[_field_type(table, spec.cursor_column)]
        if last_cursor is not None:
            # >= rather than >: rows sharing the last cursor value may have
            # arrived after the previous run. Re-emitting one is a no-op upsert.
            sql += f" WHERE {_quote_ident(spec.cursor_column)} >= @since"
            params.append(_query_parameter("since", param_type, last_cursor))

    # Every fetched row is at or after last_cursor (see the WHERE above), so the
    # largest value fetched is the new cursor.
    newest: Any = None
    fetched_keys: set[str] = set()
    changed = 0
    skipped = 0
    for row in _run_query(client, sql, params, maximum_bytes_billed):
        if row[spec.key_column] is None:
            # A NULL key can't identify a document: several such rows would
            # collapse into one id and the deletion sweep couldn't track them.
            skipped += 1
            continue
        key = str(row[spec.key_column])
        fetched_keys.add(key)
        if spec.cursor_column:
            value = row[spec.cursor_column]
            if value is not None and (newest is None or value > newest):
                newest = value
        yield _checked(row_to_document(full_id, table.table_id, spec, row, columns), check_active)
        changed += 1

    if spec.cursor_column and last_cursor is not None:
        # Incremental run: the query saw only changed rows, so read the full key
        # set separately. Selecting one column bills only that column's bytes.
        key_sql = f"SELECT {_quote_ident(spec.key_column)} {from_clause}"
        current_keys = {
            str(row[spec.key_column])
            for row in _run_query(client, key_sql, [], maximum_bytes_billed)
            if row[spec.key_column] is not None
        }
    else:
        current_keys = fetched_keys

    if skipped:
        logger.warning(
            "BigQuery: %s — skipped %d row(s) with a NULL %s.", full_id, skipped, spec.key_column
        )
    deleted = known_keys - current_keys
    for key in sorted(deleted):
        yield _checked({"id": _row_id(full_id, key), "_deleted": True}, check_active)

    for key, value in (
        ("scanned", len(current_keys)),
        ("changed", changed),
        ("deleted", len(deleted)),
        ("failed", skipped),
    ):
        stats[key] = stats.get(key, 0) + value
    table_state["keys"] = sorted(current_keys)
    if newest is not None:
        table_state["cursor"] = _encode_cursor(newest)
    logger.info("BigQuery: %s — %d changed row(s), %d deletion(s).", full_id, changed, len(deleted))


def row_to_document(
    full_id: str, table_id: str, spec: RowSync, row: Mapping[str, Any], columns: list[str]
) -> dict:
    """Render one table row as a document row: ``column: value`` lines."""
    key = str(row[spec.key_column])
    title_value = row[spec.title_column] if spec.title_column else None
    title = str(title_value) if title_value is not None else f"{table_id} {key}"
    lines = [f"Row of BigQuery table `{full_id}` where {spec.key_column} = {key}."]
    for column in columns:
        value = row[column]
        if value is not None:
            lines.append(f"{column}: {format_value(value)}")
    return {
        "id": _row_id(full_id, key),
        "title": title,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def format_value(value: Any) -> str:
    """Render a BigQuery cell value as stable text."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _select_columns(table: Any, spec: RowSync) -> list[str]:
    """Columns to read: the requested ones plus key/cursor/title, validated against the schema."""
    schema_names = [field.name for field in table.schema]
    wanted = list(spec.columns) if spec.columns else schema_names
    extras = [spec.key_column, spec.cursor_column, spec.title_column]
    columns = list(dict.fromkeys([*wanted, *(c for c in extras if c)]))
    unknown = [c for c in columns if c not in schema_names]
    if unknown:
        raise ValueError(f"Columns {unknown} are not in the schema of {spec.table}.")
    if spec.cursor_column and _field_type(table, spec.cursor_column) not in _CURSOR_PARAM_TYPES:
        raise ValueError(
            f"cursor_column {spec.cursor_column!r} must be TIMESTAMP, DATETIME, DATE or INTEGER."
        )
    return columns


def _field_type(table: Any, column: str) -> str:
    return next(field.field_type for field in table.schema if field.name == column)


def _run_query(
    client: Any, sql: str, params: list, maximum_bytes_billed: int
) -> Iterator[Mapping[str, Any]]:
    """Dry-run a query against the byte cap, then run it with the cap enforced."""
    from google.cloud import bigquery

    dry_run = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params
        ),
    )
    if dry_run.total_bytes_processed > maximum_bytes_billed:
        raise ValueError(
            f"BigQuery query would process {dry_run.total_bytes_processed} bytes, above "
            f"maximum_bytes_billed={maximum_bytes_billed}. Narrow the RowSync columns, add a "
            f"cursor_column, or raise the cap. Query: {sql}"
        )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=params, maximum_bytes_billed=maximum_bytes_billed
        ),
    )
    yield from job.result()


def _query_parameter(name: str, param_type: str, stored: str | int) -> Any:
    """Build a typed query parameter from a cursor value kept in dlt state."""
    from google.cloud import bigquery

    if param_type in ("TIMESTAMP", "DATETIME"):
        value: Any = datetime.fromisoformat(str(stored))
    elif param_type == "DATE":
        value = date.fromisoformat(str(stored))
    else:
        value = int(stored)
    return bigquery.ScalarQueryParameter(name, param_type, value)


def _encode_cursor(value: Any) -> str | int:
    """JSON-safe form of a cursor value for dlt state."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return int(value)


def _qualify(project: str, table: str) -> str:
    dataset_id, _, table_id = table.partition(".")
    if not dataset_id or not table_id or "." in table_id:
        raise ValueError(f"RowSync.table must look like 'dataset.table', got {table!r}.")
    return f"{project}.{dataset_id}.{table_id}"


def _quote_table(full_id: str) -> str:
    if "`" in full_id:
        raise ValueError(f"Invalid BigQuery table id {full_id!r}.")
    return f"`{full_id}`"


def _quote_ident(column: str) -> str:
    # Columns are validated against the table schema before they reach SQL.
    return f"`{column}`"


def _row_id(full_id: str, key: str) -> str:
    return f"{full_id}:{key}"
