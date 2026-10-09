"""DLT source for Apache Iceberg tables (full-snapshot sync + forget-on-delete).

Fetches Apache Iceberg table schemas, partition specifications, snapshot histories,
and properties from catalogs, renders them into structured markdown documents,
and yields them as a dlt resource for cognee's ingestion pipeline.

Like the Notion connector, tables are ingested as documents: the source declares
``cognee_document_source = "iceberg"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "iceberg"``. Each table flows through the standard
cognify entity-extraction pipeline into the knowledge graph and vector store.

The source executes a full snapshot sync: ``write_disposition="replace"`` replaces
staging with exactly the tables currently visible in the selected namespaces.
Dropped or unshared tables drop out of staging, and cognee's orphan cleanup
reconciles them out of graph memory. Unchanged tables maintain a stable
content-hash data_id, preventing redundant re-cognification.
"""

import time
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("iceberg_connector")
except ImportError:
    import logging

    logger = logging.getLogger("iceberg_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ICEBERG_TABLE_NAME = "iceberg_tables"
ICEBERG_SOURCE_NAME = "iceberg"

_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Apache Iceberg connector requires "pyiceberg" and "dlt": '
    'pip install "cognee-community-connector-iceberg"'
)


def iceberg_source(
    catalog_properties: dict[str, str] | None = None,
    catalog_name: str = "default",
    namespaces: list[str | tuple[str, ...]] | None = None,
    table_names: list[str | tuple[str, ...]] | None = None,
    catalog: Any = None,
    include_snapshots: bool = True,
):
    """Create a dlt source that yields Apache Iceberg table metadata as markdown documents.

    Args:
        catalog_properties: Properties dict to configure the catalog via ``load_catalog``
            (e.g., uri, warehouse, token, type="rest").
        catalog_name: Name of the catalog instance. Defaults to "default".
        namespaces: Restrict sync to tables within these namespaces.
        table_names: Restrict sync to these specific table identifiers.
        catalog: Pre-built ``pyiceberg.catalog.Catalog`` (useful for testing);
            when omitted, a catalog is instantiated from ``catalog_properties``.
        include_snapshots: Whether to include recent snapshot commit logs in the documentation.

    Returns:
        A dlt source ready for ``cognee.add(...)`` or ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if catalog is None:
        try:
            from pyiceberg.catalog import load_catalog
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        properties = catalog_properties or {}
        catalog = load_catalog(catalog_name, **properties)

    @dlt.resource(name=ICEBERG_TABLE_NAME, primary_key="id", write_disposition="replace")
    def iceberg_tables():
        count = 0
        for table in _iter_tables(catalog, namespaces=namespaces, table_names=table_names):
            count += 1
            yield _table_to_row(table, include_snapshots=include_snapshots)
        logger.info("Iceberg: synced %d table(s).", count)

    @dlt.source(name=ICEBERG_SOURCE_NAME)
    def _iceberg():
        return iceberg_tables

    source = _iceberg()
    setattr(source, DOCUMENT_SOURCE_ATTR, ICEBERG_SOURCE_NAME)
    return source


def _iter_tables(catalog: Any, namespaces: list[Any] | None, table_names: list[Any] | None):
    """Yield table instances for the configured scope."""
    if table_names:
        for identifier in table_names:
            try:
                table = _request(catalog.load_table, identifier)
                yield table
            except Exception as exc:
                if _is_gone(exc):
                    logger.warning("Iceberg: table %s is gone, skipping: %s", identifier, exc)
                    continue
                raise
        return

    # Enumerate target namespaces
    target_namespaces = []
    if namespaces:
        target_namespaces = namespaces
    else:
        try:
            target_namespaces = _request(catalog.list_namespaces)
        except Exception as exc:
            if not _is_transient(exc):
                logger.error("Failed to list namespaces from catalog: %s", exc)
            raise

    for ns in target_namespaces:
        try:
            tbl_identifiers = _request(catalog.list_tables, ns)
        except Exception as exc:
            if _is_gone(exc):
                logger.warning("Iceberg: namespace %s is gone, skipping: %s", ns, exc)
                continue
            raise

        for identifier in tbl_identifiers:
            try:
                table = _request(catalog.load_table, identifier)
                yield table
            except Exception as exc:
                if _is_gone(exc):
                    logger.warning("Iceberg: table %s is gone, skipping: %s", identifier, exc)
                    continue
                raise


def _table_to_row(table: Any, include_snapshots: bool = True) -> dict[str, Any]:
    """Flatten an Iceberg table model into a structured document row."""
    if isinstance(table.identifier, tuple):
        identifier_str = ".".join(table.identifier)
    else:
        identifier_str = str(table.identifier)

    schema_md = _render_schema(getattr(table, "schema", lambda: None)())
    spec_md = _render_partition_spec(getattr(table, "spec", lambda: None)())
    props_md = _render_properties(getattr(table, "properties", {}))
    snapshots_fn = getattr(table, "snapshots", lambda: [])
    snapshots_md = _render_snapshots(snapshots_fn()) if include_snapshots else ""

    meta = getattr(table, "metadata", None)
    format_version = getattr(meta, "format_version", 2) if meta else 2
    cur_snap_fn = getattr(table, "current_snapshot_id", lambda: "None")
    cur_snap_id = cur_snap_fn() or "None"

    content_parts = [
        f"# Iceberg Table: {identifier_str}\n",
        f"- **Identifier**: `{identifier_str}`",
        f"- **Format Version**: {format_version}",
        f"- **Current Snapshot ID**: `{cur_snap_id}`\n",
        "## Schema",
        schema_md,
        "\n## Partitioning",
        spec_md,
    ]

    if snapshots_md:
        content_parts.extend(["\n## Recent Commit & Snapshot History", snapshots_md])

    if props_md:
        content_parts.extend(["\n## Table Properties", props_md])

    return {
        "id": identifier_str,
        "url": f"iceberg://{identifier_str}",
        "title": f"Apache Iceberg Table: {identifier_str}",
        "content": "\n".join(content_parts),
    }


def _render_schema(schema: Any) -> str:
    """Render an Iceberg Schema into a clean markdown table."""
    if not schema or not hasattr(schema, "fields"):
        return "_No schema definition available._"

    lines = [
        "| Column ID | Field Name | Type | Required | Doc |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for field in schema.fields:
        doc = field.doc or ""
        req = "Yes" if getattr(field, "required", False) else "No"
        row = f"| {field.field_id} | `{field.name}` | `{field.field_type}` | {req} | {doc} |"
        lines.append(row)
    return "\n".join(lines)


def _render_partition_spec(spec: Any) -> str:
    """Render an Iceberg PartitionSpec into markdown."""
    if not spec or not hasattr(spec, "fields") or not spec.fields:
        return "_Table is unpartitioned._"

    lines = [
        "| Source ID | Partition Field | Transform |",
        "| :--- | :--- | :--- |",
    ]
    for field in spec.fields:
        lines.append(f"| {field.source_id} | `{field.name}` | `{field.transform}` |")
    return "\n".join(lines)


def _render_snapshots(snapshots: list[Any]) -> str:
    """Render recent Iceberg snapshots into markdown."""
    if not snapshots:
        return "_No snapshots recorded._"

    lines = [
        "| Snapshot ID | Timestamp (UTC) | Operation | Summary Records Added |",
        "| :--- | :--- | :--- | :--- |",
    ]
    # Show last 10 snapshots in reverse chronological order
    for snap in list(reversed(snapshots))[:10]:
        snap_id = snap.snapshot_id
        ts_ms = snap.timestamp_ms
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_ms / 1000.0)) if ts_ms else "N/A"
        summary = snap.summary or {}
        operation = summary.get("operation", "unknown")
        added_records = summary.get("added-records", "0")
        lines.append(f"| `{snap_id}` | {ts_str} | `{operation}` | {added_records} |")
    return "\n".join(lines)


def _render_properties(properties: dict[str, Any]) -> str:
    """Render table properties into markdown."""
    if not properties:
        return "_No custom properties._"

    lines = ["| Property Key | Value |", "| :--- | :--- |"]
    for k, v in sorted(properties.items()):
        # Avoid leaking secret tokens or keys
        if any(secret_term in k.lower() for secret_term in ["token", "secret", "password", "key"]):
            continue
        lines.append(f"| `{k}` | `{v}` |")
    return "\n".join(lines)


def _request(method: Any, *args, **kwargs) -> Any:
    """Call a catalog method with exponential backoff on transient errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = float(2**attempt)
            logger.warning(
                "Iceberg: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """Determine whether an error is transient and retryable."""
    error_str = str(exc).lower()
    retry_terms = [
        "timeout",
        "connection refused",
        "503",
        "502",
        "504",
        "rate limit",
    ]
    return any(term in error_str for term in retry_terms)


def _is_gone(exc: Exception) -> bool:
    """Determine whether an entity is permanently removed or unshared."""
    error_type = exc.__class__.__name__
    return error_type in ["NoSuchTableError", "NoSuchNamespaceError", "NoSuchPropertyException"]
