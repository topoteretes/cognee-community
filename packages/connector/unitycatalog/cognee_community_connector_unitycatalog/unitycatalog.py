"""DLT source for Databricks Unity Catalog lakehouse governance, schemas, tables, and AI models.

Fetches Unity Catalog metadata hierarchies (catalogs, schemas, tables with column types,
and registered AI/ML models), rendering them into structured markdown documents for Cognee's
ingestion and knowledge graph extraction pipeline.

Uses ``write_disposition="replace"`` and ``DOCUMENT_SOURCE_ATTR = "unitycatalog"`` so deleted or
decommissioned tables and models automatically drop out of Cognee's memory via orphan cleanup.
"""

from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("unitycatalog_connector")
except ImportError:
    import logging

    logger = logging.getLogger("unitycatalog_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_unitycatalog.client import UnityCatalogClient

UNITYCATALOG_CATALOGS_TABLE_NAME = "unitycatalog_catalogs"
UNITYCATALOG_SCHEMAS_TABLE_NAME = "unitycatalog_schemas"
UNITYCATALOG_TABLES_TABLE_NAME = "unitycatalog_tables"
UNITYCATALOG_MODELS_TABLE_NAME = "unitycatalog_models"
UNITYCATALOG_SOURCE_NAME = "unitycatalog"

_EXTRA_HINT = (
    'The Unity Catalog connector requires the "unitycatalog" dependencies: '
    'pip install "cognee-community-connector-unitycatalog" (provides dlt and httpx).'
)


def unitycatalog_source(
    base_url: str | None = None,
    token: str | None = None,
    catalogs: list[str] | None = None,
    schemas: list[str] | None = None,
    include_tables: bool = True,
    include_models: bool = True,
    verify_ssl: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Unity Catalog lakehouse metadata as documents.

    Args:
        base_url: Databricks or Unity Catalog server URL (e.g. ``https://adb-1234.azuredatabricks.net``).
            Falls back to ``DATABRICKS_HOST`` environment variable.
        token: Databricks PAT or Unity Catalog access token.
            Falls back to ``DATABRICKS_TOKEN`` environment variable.
        catalogs: Optional filter to restrict ingestion to specific catalog names.
        schemas: Optional filter to restrict ingestion to specific schema names
            (e.g. ``["finance", "ml_models"]``).
        include_tables: Whether to ingest tables, views, and column definitions (default: True).
        include_models: Whether to ingest registered AI/ML models (default: True).
        verify_ssl: Whether to verify SSL certificates (default: True).
        client: Pre-instantiated ``UnityCatalogClient`` (primarily for test dependency injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        client = UnityCatalogClient(
            base_url=base_url,
            token=token,
            verify_ssl=verify_ssl,
        )

    server_base = client.base_url

    @dlt.resource(
        name=UNITYCATALOG_CATALOGS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def unitycatalog_catalogs():
        cats = client.get_catalogs()
        if catalogs:
            target_cats = set(catalogs)
            cats = [c for c in cats if c.get("name") in target_cats]

        count = 0
        for cat in cats:
            count += 1
            yield _catalog_to_document(cat, server_base)
        logger.info("Unity Catalog: synced %d catalog(s).", count)

    @dlt.resource(
        name=UNITYCATALOG_SCHEMAS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def unitycatalog_schemas():
        cats = client.get_catalogs()
        if catalogs:
            target_cats = set(catalogs)
            cats = [c for c in cats if c.get("name") in target_cats]

        total_schemas = 0
        for cat in cats:
            cat_name = cat.get("name", "")
            if not cat_name:
                continue
            try:
                schems = client.get_schemas(cat_name)
                if schemas:
                    target_schems = set(schemas)
                    schems = [s for s in schems if s.get("name") in target_schems]
                for s in schems:
                    total_schemas += 1
                    yield _schema_to_document(s, server_base)
            except Exception as exc:
                logger.warning(
                    "Unity Catalog: error listing schemas in catalog '%s': %s", cat_name, exc
                )

        logger.info("Unity Catalog: synced %d schema(s).", total_schemas)

    @dlt.resource(
        name=UNITYCATALOG_TABLES_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def unitycatalog_tables():
        if not include_tables:
            return

        cats = client.get_catalogs()
        if catalogs:
            target_cats = set(catalogs)
            cats = [c for c in cats if c.get("name") in target_cats]

        total_tables = 0
        for cat in cats:
            cat_name = cat.get("name", "")
            if not cat_name:
                continue
            try:
                schems = client.get_schemas(cat_name)
                if schemas:
                    target_schems = set(schemas)
                    schems = [s for s in schems if s.get("name") in target_schems]

                for s in schems:
                    s_name = s.get("name", "")
                    if not s_name:
                        continue
                    try:
                        tbls = client.get_tables(cat_name, s_name)
                        for t in tbls:
                            total_tables += 1
                            yield _table_to_document(t, server_base)
                    except Exception as exc:
                        logger.warning(
                            "Unity Catalog: error listing tables in '%s.%s': %s",
                            cat_name,
                            s_name,
                            exc,
                        )

            except Exception as exc:
                logger.warning("Unity Catalog: error inspecting catalog '%s': %s", cat_name, exc)

        logger.info("Unity Catalog: synced %d table(s).", total_tables)

    @dlt.resource(
        name=UNITYCATALOG_MODELS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def unitycatalog_models():
        if not include_models:
            return

        cats = client.get_catalogs()
        if catalogs:
            target_cats = set(catalogs)
            cats = [c for c in cats if c.get("name") in target_cats]

        total_models = 0
        for cat in cats:
            cat_name = cat.get("name", "")
            if not cat_name:
                continue
            try:
                schems = client.get_schemas(cat_name)
                if schemas:
                    target_schems = set(schemas)
                    schems = [s for s in schems if s.get("name") in target_schems]

                for s in schems:
                    s_name = s.get("name", "")
                    if not s_name:
                        continue
                    try:
                        mods = client.get_registered_models(cat_name, s_name)
                        for m in mods:
                            total_models += 1
                            yield _model_to_document(m, server_base)
                    except Exception as exc:
                        logger.warning(
                            "Unity Catalog: error listing models in '%s.%s': %s",
                            cat_name,
                            s_name,
                            exc,
                        )

            except Exception as exc:
                logger.warning("Unity Catalog: error inspecting catalog '%s': %s", cat_name, exc)

        logger.info("Unity Catalog: synced %d model(s).", total_models)

    @dlt.source(name=UNITYCATALOG_SOURCE_NAME)
    def _unitycatalog():
        resources = [unitycatalog_catalogs, unitycatalog_schemas]
        if include_tables:
            resources.append(unitycatalog_tables)
        if include_models:
            resources.append(unitycatalog_models)
        return resources

    source = _unitycatalog()
    setattr(source, DOCUMENT_SOURCE_ATTR, UNITYCATALOG_SOURCE_NAME)
    return source


def _catalog_to_document(cat: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert a Unity Catalog catalog entity into a structured markdown document."""
    name = cat.get("name", "unknown")
    comment = cat.get("comment", "No description provided.")
    owner = cat.get("owner", "N/A")
    storage_root = cat.get("storage_root", "N/A")

    lines = [
        f"# Unity Catalog: {name}",
        "",
        "## Catalog Metadata",
        f"- **Catalog Name**: `{name}`",
        f"- **Owner**: `{owner}`",
        f"- **Storage Root**: `{storage_root}`",
        "",
        "## Overview",
        f"{comment}",
    ]

    content = "\n".join(lines)
    return {
        "id": f"unitycatalog_cat_{name}",
        "url": f"{base_url}/explore/data/{name}",
        "title": f"Unity Catalog: {name}",
        "content": content,
    }


def _schema_to_document(schema: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert a Unity Catalog schema into a structured markdown document."""
    name = schema.get("name", "unknown")
    cat_name = schema.get("catalog_name", "unknown")
    full_name = schema.get("full_name") or f"{cat_name}.{name}"
    comment = schema.get("comment", "No description provided.")
    owner = schema.get("owner", "N/A")

    lines = [
        f"# Unity Catalog Schema: {full_name}",
        "",
        "## Schema Metadata",
        f"- **Schema Name**: `{name}`",
        f"- **Parent Catalog**: `{cat_name}`",
        f"- **Full Name**: `{full_name}`",
        f"- **Owner**: `{owner}`",
        "",
        "## Description",
        f"{comment}",
    ]

    content = "\n".join(lines)
    return {
        "id": f"unitycatalog_schema_{cat_name}_{name}",
        "url": f"{base_url}/explore/data/{cat_name}/{name}",
        "title": f"Unity Catalog Schema: {full_name}",
        "content": content,
    }


def _table_to_document(table: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert a table/view with columns into a structured markdown document."""
    name = table.get("name", "unknown")
    cat_name = table.get("catalog_name", "unknown")
    schema_name = table.get("schema_name", "unknown")
    full_name = table.get("full_name") or f"{cat_name}.{schema_name}.{name}"
    table_type = table.get("table_type", "MANAGED")
    data_format = table.get("data_source_format", "DELTA")
    comment = table.get("comment", "No description provided.")
    owner = table.get("owner", "N/A")
    columns = table.get("columns") or []

    lines = [
        f"# Unity Catalog Table: {full_name}",
        "",
        "## Table Metadata",
        f"- **Table Name**: `{name}`",
        f"- **Catalog**: `{cat_name}`",
        f"- **Schema**: `{schema_name}`",
        f"- **Full Qualified Name**: `{full_name}`",
        f"- **Table Type**: `{table_type}`",
        f"- **Data Format**: `{data_format}`",
        f"- **Owner**: `{owner}`",
        "",
        "## Overview",
        f"{comment}",
        "",
        "## Column Schema",
    ]

    if columns:
        for col in columns:
            col_name = col.get("name", "unknown")
            col_type = col.get("type_text") or col.get("type_name") or "unknown"
            col_comment = col.get("comment", "")
            desc = f" - *{col_comment}*" if col_comment else ""
            lines.append(f"- `{col_name}` (`{col_type}`){desc}")
    else:
        lines.append("- *No column definitions provided.*")

    content = "\n".join(lines)
    return {
        "id": f"unitycatalog_tbl_{cat_name}_{schema_name}_{name}",
        "url": f"{base_url}/explore/data/{cat_name}/{schema_name}/{name}",
        "title": f"Unity Catalog Table: {full_name} ({data_format})",
        "content": content,
    }


def _model_to_document(model: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert a registered AI/ML model into a structured markdown document."""
    name = model.get("name", "unknown")
    cat_name = model.get("catalog_name", "unknown")
    schema_name = model.get("schema_name", "unknown")
    full_name = model.get("full_name") or f"{cat_name}.{schema_name}.{name}"
    comment = model.get("comment", "No description provided.")
    aliases = model.get("aliases") or []
    tags = model.get("tags") or {}

    lines = [
        f"# Unity Catalog Registered AI Model: {full_name}",
        "",
        "## Model Metadata",
        f"- **Model Name**: `{name}`",
        f"- **Catalog**: `{cat_name}`",
        f"- **Schema**: `{schema_name}`",
        f"- **Full Qualified Name**: `{full_name}`",
        "",
        "## Description",
        f"{comment}",
    ]

    if aliases:
        lines.extend(
            [
                "",
                "## Model Aliases",
            ]
        )
        for alias in aliases:
            a_name = alias.get("alias_name", "")
            a_ver = alias.get("version_num", "")
            lines.append(f"- **{a_name}**: Version {a_ver}")

    if tags:
        lines.extend(
            [
                "",
                "## Tags & Performance Attributes",
            ]
        )
        for t_k, t_v in tags.items():
            lines.append(f"- **{t_k}**: `{t_v}`")

    content = "\n".join(lines)
    return {
        "id": f"unitycatalog_mdl_{cat_name}_{schema_name}_{name}",
        "url": f"{base_url}/explore/data/models/{cat_name}/{schema_name}/{name}",
        "title": f"Unity Catalog Model: {full_name}",
        "content": content,
    }
