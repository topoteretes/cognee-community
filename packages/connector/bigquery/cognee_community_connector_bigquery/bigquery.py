"""BigQuery data-source connector for cognee -- turns BigQuery into AI memory.

Syncs BigQuery dataset/table schemas, descriptions, and query results into cognee,
incrementally and with forget-on-deletion.

Designed to follow the standard cognee-community connector pattern (like Notion,
Google Drive, and Confluence), using dlt document mode so that schemas and records
flow directly into cognee's knowledge graph and vector memory.

Usage:
    import cognee
    from cognee_community_connector_bigquery import bigquery_source

    # 1. Sync table and column descriptions (metadata)
    await cognee.remember(
        bigquery_source(
            dataset_id="analytics",
            credentials_path="/path/to/service-account.json",
        ),
        dataset_name="data_catalog",
    )

    # 2. Sync query results or table rows incrementally
    await cognee.remember(
        bigquery_source(
            dataset_id="analytics",
            table_names=["orders"],
            include_rows=True,
            incremental_column="updated_at",
            primary_key="order_id",
        ),
        dataset_name="orders_memory",
    )
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("bigquery_connector")

BIGQUERY_SOURCE_NAME = "bigquery"
BIGQUERY_TABLE_NAME = "bigquery_documents"

_EXTRA_HINT = (
    "The BigQuery connector requires google-cloud-bigquery and google-auth. "
    "Install with: pip install google-cloud-bigquery google-auth"
)


# ---------------------------------------------------------------------------
# Auth / client construction
# ---------------------------------------------------------------------------
def build_bigquery_client(
    *,
    credentials_path: str | None = None,
    credentials_info: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> Any:
    """Build an authenticated Google Cloud BigQuery client."""
    try:
        from google.auth import default as google_default_auth
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_path = (
        credentials_path
        or os.getenv("BIGQUERY_CREDENTIALS_PATH")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    resolved_project = (
        project_id
        or os.getenv("BIGQUERY_PROJECT_ID")
        or os.getenv("GCP_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )

    if resolved_path and os.path.exists(resolved_path):
        creds = service_account.Credentials.from_service_account_file(resolved_path)
        project = resolved_project or creds.project_id
        return bigquery.Client(credentials=creds, project=project)

    if credentials_info:
        creds = service_account.Credentials.from_service_account_info(credentials_info)
        project = resolved_project or creds.project_id
        return bigquery.Client(credentials=creds, project=project)

    try:
        creds, adc_project = google_default_auth()
        project = resolved_project or adc_project
        return bigquery.Client(credentials=creds, project=project)
    except Exception as exc:
        raise ValueError(
            "BigQuery authentication failed: provide credentials_path, credentials_info, "
            "or configure Application Default Credentials."
        ) from exc


# ---------------------------------------------------------------------------
# Metadata rendering
# ---------------------------------------------------------------------------
def _render_schema_field(field: Any, indent: int = 0) -> list[str]:
    """Render a BigQuery SchemaField and its nested fields to markdown lines."""
    indent_str = "  " * indent
    name = getattr(field, "name", str(field))
    field_type = getattr(field, "field_type", "STRING")
    mode = getattr(field, "mode", "NULLABLE")
    desc = getattr(field, "description", None)

    line = f"{indent_str}- `{name}` ({field_type}, {mode})"
    if desc:
        line += f": {desc}"
    lines = [line]

    subfields = getattr(field, "fields", None) or ()
    for sub in subfields:
        lines.extend(_render_schema_field(sub, indent + 1))
    return lines


def _render_table_metadata(table: Any) -> str:
    """Render BigQuery Table metadata and schema into a structured markdown document."""
    project = getattr(table, "project", "")
    dataset_id = getattr(table, "dataset_id", "")
    table_id = getattr(table, "table_id", "")
    full_table_id = getattr(table, "full_table_id", f"{project}.{dataset_id}.{table_id}".strip("."))
    table_type = getattr(table, "table_type", "TABLE")
    description = getattr(table, "description", None)
    num_rows = getattr(table, "num_rows", None)
    created = getattr(table, "created", None)
    modified = getattr(table, "modified", None)
    labels = getattr(table, "labels", None) or {}

    lines = [f"# BigQuery Table: `{full_table_id}`", ""]
    if description:
        lines.extend([f"**Description**: {description}", ""])
    lines.append(f"**Type**: {table_type}")
    if num_rows is not None:
        lines.append(f"**Row Count**: {num_rows:,}")
    if created:
        created_str = created.isoformat() if hasattr(created, "isoformat") else str(created)
        lines.append(f"**Created**: {created_str}")
    if modified:
        mod_str = modified.isoformat() if hasattr(modified, "isoformat") else str(modified)
        lines.append(f"**Last Modified**: {mod_str}")
    if labels:
        lines.append(f"**Labels**: {', '.join(f'{k}={v}' for k, v in labels.items())}")

    lines.extend(["", "## Columns"])
    schema = getattr(table, "schema", None) or ()
    if schema:
        for field in schema:
            lines.extend(_render_schema_field(field))
    else:
        lines.append("*(No schema information available)*")

    return "\n".join(lines)


def _table_to_metadata_row(table: Any) -> dict[str, Any]:
    project = getattr(table, "project", "default")
    dataset_id = getattr(table, "dataset_id", "default")
    table_id = getattr(table, "table_id", "default")
    doc_id = f"bigquery://{project}/{dataset_id}/{table_id}/metadata"

    modified = getattr(table, "modified", None)
    version_when = modified.isoformat() if hasattr(modified, "isoformat") else str(modified or "")
    url = (
        f"https://console.cloud.google.com/bigquery?project={project}"
        f"&ws=!1m5!1m4!4m3!1s{project}!2s{dataset_id}!3s{table_id}"
    )

    return {
        "id": doc_id,
        "title": f"BigQuery Table: {project}.{dataset_id}.{table_id}",
        "dataset_id": dataset_id,
        "table_id": table_id,
        "content": _render_table_metadata(table),
        "url": url,
        "version_when": version_when,
        "_deleted": False,
    }


def _deleted_metadata_row(project: str, dataset_id: str, table_id: str) -> dict[str, Any]:
    return {
        "id": f"bigquery://{project}/{dataset_id}/{table_id}/metadata",
        "_deleted": True,
    }


# ---------------------------------------------------------------------------
# Sync engines
# ---------------------------------------------------------------------------
def sync_table_metadata(
    client: Any,
    dataset_id: str,
    state: dict[str, Any],
    *,
    table_names: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield table schema/description documents, plus hard-delete markers for dropped tables."""
    known_table_ids: set[str] = set(state.get("known_table_ids", []))
    last_when: str = state.get("last_when", "")
    newest_when = last_when
    current_table_ids: set[str] = set()

    try:
        tables_iter = client.list_tables(dataset_id)
    except Exception as exc:
        logger.error("BigQuery: failed to list tables for dataset '%s': %s", dataset_id, exc)
        raise

    for table_item in tables_iter:
        tid = getattr(table_item, "table_id", str(table_item))
        if table_names and tid not in table_names:
            continue
        current_table_ids.add(tid)

        table = client.get_table(table_item) if hasattr(client, "get_table") else table_item
        modified = getattr(table, "modified", None)
        when = modified.isoformat() if hasattr(modified, "isoformat") else str(modified or "")

        if tid in known_table_ids and when and when <= last_when:
            continue

        if when and when > newest_when:
            newest_when = when

        yield _table_to_metadata_row(table)

    if known_table_ids and not current_table_ids and not table_names:
        logger.warning(
            "BigQuery: table sweep returned 0 tables for dataset '%s' but %d were known; "
            "skipping deletion to avoid mass forget-on-delete on transient error.",
            dataset_id,
            len(known_table_ids),
        )
        state["last_when"] = newest_when
        return

    deleted = known_table_ids - current_table_ids
    project = getattr(client, "project", "default")
    for tid in sorted(deleted):
        yield _deleted_metadata_row(project, dataset_id, tid)

    state["known_table_ids"] = sorted(current_table_ids)
    state["last_when"] = newest_when


def sync_query_or_table_rows(
    client: Any,
    state: dict[str, Any],
    *,
    query: str | None = None,
    dataset_id: str | None = None,
    table_name: str | None = None,
    incremental_column: str | None = None,
    primary_key: str | None = None,
    soft_delete_column: str | None = None,
    max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield rows from a query or table as documents, tracking incremental cursors."""
    sql = query
    if not sql:
        if not dataset_id or not table_name:
            return
        project = getattr(client, "project", "")
        table_ref = (
            f"`{project}.{dataset_id}.{table_name}`" if project else f"`{dataset_id}.{table_name}`"
        )
        sql = f"SELECT * FROM {table_ref}"

    cursor_key = f"cursor_{dataset_id}_{table_name}" if table_name else "query_cursor"
    last_cursor = state.get(cursor_key)

    if incremental_column and last_cursor is not None:
        connector = "AND" if "WHERE" in sql.upper() else "WHERE"
        sql = f"{sql} {connector} {incremental_column} > '{last_cursor}'"

    if incremental_column and "ORDER BY" not in sql.upper():
        sql = f"{sql} ORDER BY {incremental_column} ASC"

    if max_rows is not None and "LIMIT" not in sql.upper():
        sql = f"{sql} LIMIT {max_rows}"

    query_job = client.query(sql)
    results = query_job.result() if hasattr(query_job, "result") else query_job

    newest_cursor = last_cursor
    for idx, row in enumerate(results):
        if hasattr(row, "items"):
            row_dict = dict(row.items())
        elif isinstance(row, dict):
            row_dict = dict(row)
        elif hasattr(row, "_asdict"):
            row_dict = row._asdict()
        else:
            row_dict = {"value": str(row)}

        if incremental_column and incremental_column in row_dict:
            val = row_dict[incremental_column]
            val_str = val.isoformat() if hasattr(val, "isoformat") else str(val)
            if newest_cursor is None or val_str > newest_cursor:
                newest_cursor = val_str

        is_deleted = False
        if soft_delete_column and soft_delete_column in row_dict:
            is_deleted = bool(row_dict[soft_delete_column])

        row_id_val = row_dict.get(primary_key) if primary_key else None
        target_name = table_name or "query"
        project_name = getattr(client, "project", "default")
        dataset_part = dataset_id or "q"
        if row_id_val is not None:
            row_id = f"bigquery://{project_name}/{dataset_part}/{target_name}/{row_id_val}"
        else:
            row_id = f"bigquery://{project_name}/{dataset_part}/{target_name}/row_{idx}"

        if is_deleted:
            yield {"id": row_id, "_deleted": True}
        else:
            lines = [f"{k}: {v}" for k, v in row_dict.items() if k != "_deleted"]
            base_url = "https://console.cloud.google.com/bigquery"
            client_project = getattr(client, "project", "")
            yield {
                "id": row_id,
                "title": f"BigQuery Record: {target_name} ({row_id_val or idx})",
                "content": "\n".join(lines),
                "url": f"{base_url}?project={client_project}",
                "_deleted": False,
            }

    if newest_cursor is not None:
        state[cursor_key] = newest_cursor


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def bigquery_source(
    dataset_id: str | None = None,
    *,
    table_names: list[str] | None = None,
    query: str | None = None,
    include_metadata: bool = True,
    include_rows: bool = False,
    incremental_column: str | None = None,
    primary_key: str | None = None,
    soft_delete_column: str | None = None,
    max_rows_per_table: int | None = None,
    credentials_path: str | None = None,
    credentials_info: dict[str, Any] | None = None,
    project_id: str | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields BigQuery schemas and records for cognee memory.

    Hand the result to ``cognee.remember(...)`` or ``cognee.add(...)``.

    Args:
        dataset_id: BigQuery dataset to inspect.
        table_names: Optional subset of tables in ``dataset_id`` to sync.
        query: Custom SQL query to execute and ingest.
        include_metadata: Ingest table schemas and column descriptions (default True).
        include_rows: Ingest data rows from specified tables or query (default False).
        incremental_column: Partition or timestamp column for incremental sync.
        primary_key: Column name holding the row unique ID.
        soft_delete_column: Column name flagging deleted records (emits tombstone).
        max_rows_per_table: Row limit per table when syncing rows.
        credentials_path: Path to Google Service Account JSON file.
        credentials_info: Dict containing Service Account JSON contents.
        project_id: Google Cloud Project ID.
        client: Pre-built BigQuery client instance (for testing).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The BigQuery connector requires dlt: pip install "cognee[dlt]"') from exc

    if not dataset_id and not query:
        raise ValueError("Must provide either 'dataset_id' or 'query' to bigquery_source.")

    bq_client = client or build_bigquery_client(
        credentials_path=credentials_path,
        credentials_info=credentials_info,
        project_id=project_id,
    )

    @dlt.resource(
        name=BIGQUERY_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def bigquery_documents():
        state = dlt.current.resource_state()

        if include_metadata and dataset_id:
            yield from sync_table_metadata(
                bq_client,
                dataset_id,
                state,
                table_names=table_names,
            )

        if query:
            yield from sync_query_or_table_rows(
                bq_client,
                state,
                query=query,
                incremental_column=incremental_column,
                primary_key=primary_key,
                soft_delete_column=soft_delete_column,
                max_rows=max_rows_per_table,
            )
        elif include_rows and dataset_id:
            target_tables = table_names or [
                getattr(t, "table_id", str(t)) for t in bq_client.list_tables(dataset_id)
            ]
            for t_name in target_tables:
                yield from sync_query_or_table_rows(
                    bq_client,
                    state,
                    dataset_id=dataset_id,
                    table_name=t_name,
                    incremental_column=incremental_column,
                    primary_key=primary_key,
                    soft_delete_column=soft_delete_column,
                    max_rows=max_rows_per_table,
                )

    resource = bigquery_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, BIGQUERY_SOURCE_NAME)
    return resource
