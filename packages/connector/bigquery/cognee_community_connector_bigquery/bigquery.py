"""BigQuery connector for cognee: a ``dlt`` source that turns a warehouse into memory.

One document per dataset, table and view (description, schema, column types, labels,
view SQL) and, opt-in per table, one per row, synced incrementally with
forget-on-delete. Hand the source to :func:`cognee.remember` with
``write_disposition="merge"``::

    await cognee.remember(
        bigquery_source(project="my-project", datasets=["sales"]),
        dataset_name="bigquery",
        write_disposition="merge",
    )

The README describes what is stored, how changes and deletions are found, what
queries cost, and what a run does when BigQuery fails part of the way through.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("bigquery_connector")

BIGQUERY_SOURCE_NAME = "bigquery"

# Default per-query cap: 1 GB, well inside BigQuery's free monthly query tier.
DEFAULT_MAXIMUM_BYTES_BILLED = 1_000_000_000

# The client library retries for up to 10 minutes per call (40 for query jobs), and
# every wait sleeps inside cognee's staging lock, which other dlt ingestions in the
# process wait on. Each call, with its retries, gets at most this long instead.
_CALL_TIMEOUT_SECONDS = 60.0
# Error reasons BigQuery sends with a 403 when a quota or rate limit was hit.
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "quotaExceeded", "backendError"}

_STAT_KEYS = (
    "scanned",
    "skipped",
    "changed",
    "deleted",
    "failed",
    "failed_auth",
    "failed_rate_limit",
    "requests",
)

# Column types a row cursor may use, mapped to the BigQuery query-parameter type.

_CURSOR_PARAM_TYPES = {
    "TIMESTAMP": "TIMESTAMP",
    "DATETIME": "DATETIME",
    "DATE": "DATE",
    "INTEGER": "INT64",
    "INT64": "INT64",
}

_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}
# C0/C1 control characters (tab and newline kept) and lone surrogates: a NUL makes
# Postgres reject the load and a lone surrogate fails the dlt extract.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


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
    service: Any = None,
    *,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields BigQuery metadata (and opt-in rows) as documents.

    Args:
        project: GCP project to read. Defaults to the credentials' project.
        datasets: Restrict metadata to these dataset ids (a list). ``None`` (and no
            ``tables``) reads every dataset in the project.
        tables: Restrict metadata to these ``dataset.table`` ids (a list).
        row_syncs: Tables whose rows are ingested too, a list of :class:`RowSync`.
        credentials_path: Service-account key file. When omitted, Application Default
            Credentials are used (which also honour ``GOOGLE_APPLICATION_CREDENTIALS``).
        maximum_bytes_billed: Per-query byte cap for row queries.
        service: Pre-built ``google.cloud.bigquery.Client`` (tests, proxies). It is used
            as is and not closed; when omitted, each run builds and closes its own.
        resource_name: Prefix of the two dlt tables (``<name>_metadata`` and
            ``<name>_rows``), which also scopes the sync state. Defaults to
            ``bigquery_<project>``.
        check_active: Optional host checkpoint, called before every BigQuery API call
            and before every row is published; raising from it stops the run.
        client: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only, summed over metadata and
        rows: ``scanned``, ``changed``, ``deleted``, ``failed`` (rows with a NULL key),
        ``failed_auth`` and ``failed_rate_limit`` (1 when the run stopped for that
        reason), ``requests`` (API calls), and ``complete`` (1 once both finished).
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The BigQuery connector needs cognee's document sync cleanup (cognee>=1.6.3)."
        )
    for argument, value in (("datasets", datasets), ("tables", tables)):
        if value is not None and (
            isinstance(value, str) or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"{argument} must be a list of strings, not a single string.")
    if row_syncs is not None and (
        isinstance(row_syncs, (str, RowSync))
        or not all(isinstance(spec, RowSync) for spec in row_syncs)
    ):
        raise ValueError("row_syncs must be a list of RowSync(...).")
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
    name = resource_name if resource_name is not None else _default_resource_name(project)
    _check_resource_name(name)
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
            run.publish()  # a failed run still reports its requests and failure reason
        run.finish(name)

    @dlt.resource(
        name=f"{name}_metadata",
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def bigquery_metadata():
        def sync(bq, state, part):
            return sync_metadata(bq, project, scope, state, part, check_active)

        yield from _run("metadata", sync)

    @dlt.resource(
        name=f"{name}_rows",
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
    # The table prefix also scopes the dlt state (cursor, known ids and keys), so the
    # state and the tables it describes always resolve to the same pipeline. It comes
    # from the project, not the selection, so narrowing datasets/tables or removing a
    # RowSync tombstones what dropped out instead of orphaning it.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    return source


def _default_resource_name(project: str) -> str:
    """``bigquery_<project>``, e.g. ``bigquery_my_project`` for ``my-project``."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(project).lower()).strip("_")
    return f"{BIGQUERY_SOURCE_NAME}_{slug}"[:48].rstrip("_")


def _check_resource_name(name: Any) -> None:
    """A non-empty name that dlt keeps as it is, short enough for its table names.

    It prefixes the tables and scopes the state: two names dlt normalizes alike
    ("BigQuery-Prod", "bigquery_prod") would share tables.
    """
    if not isinstance(name, str) or not name or len(name) > 48:
        raise ValueError("resource_name must be a non-empty string of at most 48 characters.")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _).")


class BigQuerySyncError(RuntimeError):
    """The run cannot be trusted (an emptied project listing) and stops."""


class _CheckedClient:
    """Proxy that asks the host checkpoint, counts calls and bounds retries per call.

    API calls get a retry policy capped at ``_CALL_TIMEOUT_SECONDS`` (and a query job's
    re-runs too), and an auth or rate-limit failure is recorded in the stats before it
    propagates.
    """

    _RETRYING_CALLS = frozenset({"list_datasets", "get_dataset", "list_tables", "get_table"})

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
            if name in self._RETRYING_CALLS or name == "query":
                kwargs.setdefault("retry", _bounded_retry())
                kwargs.setdefault("timeout", _CALL_TIMEOUT_SECONDS)
            if name == "query":
                kwargs.setdefault("job_retry", _bounded_retry(job=True))
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                _record_failure(exc, self._stats)
                raise

        return call


def _bounded_retry(job: bool = False) -> Any:
    """The client library's default retry policy, capped at ``_CALL_TIMEOUT_SECONDS``."""
    from google.cloud.bigquery.retry import DEFAULT_JOB_RETRY, DEFAULT_RETRY

    policy = DEFAULT_JOB_RETRY if job else DEFAULT_RETRY
    if hasattr(policy, "with_timeout"):
        return policy.with_timeout(_CALL_TIMEOUT_SECONDS)
    return policy.with_deadline(_CALL_TIMEOUT_SECONDS)  # google-api-core < 2.16


def _record_failure(exc: BaseException, stats: dict) -> None:
    """Count an auth or rate-limit failure (a spent retry budget included) in the stats."""
    from google.api_core import exceptions

    if isinstance(exc, exceptions.RetryError) and exc.cause is not None:
        exc = exc.cause
    reasons = {error.get("reason") for error in getattr(exc, "errors", None) or []}
    if isinstance(exc, exceptions.TooManyRequests) or (
        isinstance(exc, exceptions.Forbidden) and reasons & _RATE_LIMIT_REASONS
    ):
        stats["failed_rate_limit"] = 1
    elif isinstance(exc, exceptions.Unauthorized | exceptions.Forbidden):
        stats["failed_auth"] = 1


class _RunStats:
    """Per-resource counts, published as flat totals on the source after each step."""

    def __init__(self, stats: dict, names: tuple[str, ...]):
        self.stats = stats
        self.parts: dict[str, dict] = {name: {} for name in names}

    def start(self, name: str) -> dict:
        part = self.parts[name]
        part.clear()
        part.update(dict.fromkeys(_STAT_KEYS, 0), complete=0)
        self.publish()
        return part

    def finish(self, name: str) -> None:
        self.parts[name]["complete"] = 1
        self.publish()

    def publish(self) -> None:
        totals = {key: sum(part.get(key, 0) for part in self.parts.values()) for key in _STAT_KEYS}
        self.stats.clear()
        self.stats.update(totals)
        self.stats["complete"] = int(all(part.get("complete") for part in self.parts.values()))


def _digest(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def _unchanged(row: dict, digests: dict) -> bool:
    """Whether ``row`` renders exactly as last synced; records its hash either way."""
    digest = _digest(row)
    same = digests.get(row["id"]) == digest
    digests[row["id"]] = digest
    return same


def _checked(row: dict, check_active: Callable[[], None] | None) -> dict:
    """Ask the host checkpoint before a row is published."""
    if check_active is not None:
        check_active()
    return row


def _make_client(project: str | None, credentials_path: str | None) -> Any:
    """Build a BigQuery client from a key file, or from Application Default Credentials."""
    from google.cloud import bigquery

    if not credentials_path:
        return bigquery.Client(project=project)
    try:
        return bigquery.Client.from_service_account_json(credentials_path, project=project)
    except (OSError, ValueError) as exc:
        # Report the path and the kind of error only: a parse error keeps the file's
        # text (the private key) on the exception.
        raise ValueError(
            f"Could not load the service-account key at {credentials_path} "
            f"({type(exc).__name__}). Check that it is the JSON key file of a service account."
        ) from None


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
    digests = dict(state.get("digests", {}))
    emitted = 0

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
            if _unchanged(row, digests):
                continue
            emitted += 1
            yield _checked(row, check_active)

    deleted = known_ids - current_ids
    for doc_id in sorted(deleted):
        yield _checked({"id": doc_id, "_deleted": True}, check_active)

    state["known_ids"] = sorted(current_ids)
    state["digests"] = {doc_id: d for doc_id, d in digests.items() if doc_id in current_ids}
    state["whole_project"] = scope is None
    for key, value in (
        ("scanned", len(current_ids)),
        ("skipped", len(current_ids) - emitted),
        ("changed", emitted),
        ("deleted", len(deleted)),
    ):
        stats[key] = stats.get(key, 0) + value
    logger.info(
        "BigQuery: %d metadata document(s), %d changed, %d deletion(s).",
        len(current_ids),
        emitted,
        len(deleted),
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
        "title": _clean(full_id),
        "content": _clean("\n".join(lines)),
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
        "title": _clean(full_id),
        "content": _clean("\n".join(lines)),
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
            # arrived after the previous run. The rows at that value which were
            # already synced are recognised by their hash and not emitted again.
            sql += f" WHERE {_quote_ident(spec.cursor_column)} >= @since"
            params.append(_query_parameter("since", param_type, last_cursor))

    # Every fetched row is at or after last_cursor (see the WHERE above), so the
    # largest value fetched is the new cursor.
    newest: Any = None
    fetched_keys: set[str] = set()
    changed = 0
    unchanged = 0
    skipped = 0
    boundary = table_state.get("boundary", {})  # key -> hash, rows at the last cursor value
    seen: list[tuple[str, Any, str]] = []
    for row in _run_query(client, sql, params, maximum_bytes_billed):
        if row[spec.key_column] is None:
            # A NULL key can't identify a document: several such rows would
            # collapse into one id and the deletion sweep couldn't track them.
            skipped += 1
            continue
        key = str(row[spec.key_column])
        fetched_keys.add(key)
        value = row[spec.cursor_column] if spec.cursor_column else None
        if value is not None and (newest is None or value > newest):
            newest = value
        document = row_to_document(full_id, table.table_id, spec, row, columns)
        digest = _digest(document)
        seen.append((key, value, digest))
        if boundary.get(key) == digest:
            unchanged += 1
            continue
        yield _checked(document, check_active)
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
        ("skipped", unchanged),
        ("changed", changed),
        ("deleted", len(deleted)),
        ("failed", skipped),
    ):
        stats[key] = stats.get(key, 0) + value
    table_state["keys"] = sorted(current_keys)
    if newest is not None:
        table_state["cursor"] = _encode_cursor(newest)
        # The next run reads these rows again (>=); their hashes tell it which are new.
        table_state["boundary"] = {key: digest for key, value, digest in seen if value == newest}
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
        "title": _clean(title),
        "content": _clean("\n".join(lines)),
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
    yield from job.result(retry=_bounded_retry())


def _clean(text: str) -> str:
    return _UNSAFE_CHARS.sub("", str(text))


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
