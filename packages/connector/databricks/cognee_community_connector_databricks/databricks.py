"""Databricks data-source connector for cognee.

Syncs Databricks workspace assets into cognee memory across three distinct resources:
1. Notebooks — Markdown & code documents ingested into cognee's cognify/graph pipeline.
2. Unity Catalog Tables — Structured relational metadata (schemas, columns, comments)
   with change detection via Delta history.
3. SQL Queries — Opt-in executed SQL queries with chunked pagination.

Design & Guardrails
-------------------
* **Stable Identity** — Notebooks use Databricks ``object_id`` (not path) so that
  renamed or moved notebooks retain their graph identity.
* **Delta History vs Metadata** — Table metadata is fetched from Unity Catalog,
  while data mutations are tracked via ``DESCRIBE HISTORY`` (inspecting WRITE,
  UPDATE, MERGE, DELETE while ignoring maintenance operations like OPTIMIZE/VACUUM).
* **Safe Deletion Reconciliation** — Tombstones (``_deleted: True``) are ONLY emitted
  when a complete inventory of the configured scope succeeds. If any folder or schema
  listing fails (due to permissions, network, or rate limits), deletion reconciliation
  is aborted and existing state is preserved.
* **Explicit SQL Execution** — SQL queries are strictly opt-in and explicitly configured;
  code discovered inside notebooks is never executed automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("databricks_connector")
except ImportError:
    import logging

    logger = logging.getLogger("databricks_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

DEFAULT_WORKSPACE_PATHS = ["/Shared"]
DATA_MUTATION_OPERATIONS = {"WRITE", "UPDATE", "DELETE", "MERGE"}


class DatabricksClient:
    """Lightweight REST client for Databricks Workspace, Unity Catalog, and SQL APIs."""

    def __init__(
        self,
        host: str,
        token: str,
        session: requests.Session | None = None,
        max_retries: int = 3,
    ) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.host}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=30,
                )
                if resp.status_code == 401:
                    raise RuntimeError(
                        "Databricks authentication failed (HTTP 401). "
                        "Please verify that your personal access token is valid."
                    )
                if resp.status_code == 403:
                    raise RuntimeError(
                        "Databricks access forbidden (HTTP 403). "
                        "Please verify workspace and Unity Catalog permissions."
                    )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue

                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"Databricks request to {path} failed: {exc}") from exc
                time.sleep(0.5 * (2**attempt))

        return {}

    def list_workspace(self, path: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/2.0/workspace/list", params={"path": path})
        return data.get("objects", [])

    def export_notebook(self, path: str, format_type: str = "SOURCE") -> str:
        data = self._request(
            "GET",
            "/api/2.0/workspace/export",
            params={"path": path, "format": format_type},
        )
        content_b64 = data.get("content", "")
        if not content_b64:
            return ""
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")

    def list_catalogs(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/2.1/unity-catalog/catalogs")
        return data.get("catalogs", [])

    def list_schemas(self, catalog_name: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/2.1/unity-catalog/schemas",
            params={"catalog_name": catalog_name},
        )
        return data.get("schemas", [])

    def list_tables(self, catalog_name: str, schema_name: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/2.1/unity-catalog/tables",
            params={"catalog_name": catalog_name, "schema_name": schema_name},
        )
        return data.get("tables", [])

    def execute_statement(self, statement: str, warehouse_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/2.0/sql/statements",
            json_data={
                "statement": statement,
                "warehouse_id": warehouse_id,
                "wait_timeout": "30s",
            },
        )

    def get_statement(self, statement_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/2.0/sql/statements/{statement_id}")

    def get_statement_chunk(self, statement_id: str, chunk_index: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}",
        )


def _get_workspace_id(host: str) -> str:
    """Extract a clean workspace identifier from the host URL."""
    parsed = urlparse(host)
    netloc = parsed.netloc or host
    return netloc.split(".")[0].replace("https://", "").replace("http://", "")


def _iter_notebooks_recursive(
    client: DatabricksClient,
    folder_path: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Recursively list all notebook objects under a workspace path.

    Returns:
        (notebooks_list, success_flag). If any listing fails, success_flag is False.
    """
    notebooks: list[dict[str, Any]] = []
    stack = [folder_path]

    while stack:
        curr_path = stack.pop()
        try:
            items = client.list_workspace(curr_path)
        except Exception as exc:
            logger.warning("Failed to list workspace path '%s': %s", curr_path, exc)
            return [], False

        for item in items:
            obj_type = item.get("object_type")
            if obj_type == "NOTEBOOK":
                notebooks.append(item)
            elif obj_type == "DIRECTORY":
                stack.append(item["path"])

    return notebooks, True


def _iter_notebook_rows(
    client: DatabricksClient,
    workspace_id: str,
    workspace_paths: list[str],
    state: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield changed notebooks and tombstones for deleted notebooks."""
    last_sync = state.get("last_sync_notebooks", 0)
    # Overlap buffer (5 minutes) to protect against timestamp boundary issues
    overlap_window = 300_000
    effective_since = max(0, last_sync - overlap_window) if last_sync else 0

    known_notebook_ids: set[str] = set(state.get("known_notebook_ids", []))
    current_inventory_ids: set[str] = set()
    all_traversals_succeeded = True
    max_modified_seen = last_sync

    for folder in workspace_paths:
        notebook_items, ok = _iter_notebooks_recursive(client, folder)
        if not ok:
            all_traversals_succeeded = False
            continue

        for nb in notebook_items:
            obj_id = str(nb.get("object_id") or nb.get("path"))
            current_inventory_ids.add(obj_id)
            modified_at = nb.get("modified_at", 0)

            if modified_at and modified_at > max_modified_seen:
                max_modified_seen = modified_at

            # Yield notebook if modified since previous sync
            if not last_sync or modified_at >= effective_since:
                content = ""
                try:
                    content = client.export_notebook(nb["path"])
                except Exception as exc:
                    logger.warning("Failed to export notebook '%s': %s", nb["path"], exc)
                    continue

                yield {
                    "id": f"databricks:{workspace_id}:notebook:{obj_id}",
                    "title": nb["path"].split("/")[-1],
                    "path": nb["path"],
                    "language": nb.get("language", "PYTHON"),
                    "content": content,
                    "url": f"{client.host}/#workspace{nb['path']}",
                    "modified_at": modified_at,
                    "_deleted": False,
                }

    # Reconcile deletions only when all scope traversals succeeded completely
    if all_traversals_succeeded:
        deleted_ids = known_notebook_ids - current_inventory_ids
        for del_id in deleted_ids:
            yield {
                "id": f"databricks:{workspace_id}:notebook:{del_id}",
                "_deleted": True,
            }
        state["known_notebook_ids"] = list(current_inventory_ids)
    else:
        logger.warning("Workspace traversal was incomplete; skipping deletion reconciliation.")

    state["last_sync_notebooks"] = max_modified_seen


def _fetch_delta_history(
    client: DatabricksClient,
    warehouse_id: str,
    full_table_name: str,
) -> tuple[int | None, int | None]:
    """Execute DESCRIBE HISTORY to detect data mutations (WRITE, MERGE, etc.)."""
    try:
        stmt = f"DESCRIBE HISTORY {full_table_name} LIMIT 20"
        resp = client.execute_statement(stmt, warehouse_id)
        statement_id = resp.get("statement_id")
        if not statement_id:
            return None, None

        status = resp.get("status", {}).get("state")
        while status in ("PENDING", "RUNNING"):
            time.sleep(0.5)
            check = client.get_statement(statement_id)
            status = check.get("status", {}).get("state")
            if status == "SUCCEEDED":
                resp = check
                break

        if status != "SUCCEEDED":
            return None, None

        manifest = resp.get("manifest", {})
        columns = [c["name"].lower() for c in manifest.get("schema", {}).get("columns", [])]
        data_array = resp.get("result", {}).get("data_array", [])

        if "operation" in columns and "version" in columns:
            op_idx = columns.index("operation")
            ver_idx = columns.index("version")
            ts_idx = columns.index("timestamp") if "timestamp" in columns else -1

            for row in data_array:
                op = str(row[op_idx]).upper()
                if op in DATA_MUTATION_OPERATIONS:
                    ver = int(row[ver_idx])
                    ts = int(row[ts_idx]) if ts_idx >= 0 and str(row[ts_idx]).isdigit() else None
                    return ver, ts
    except Exception as exc:
        logger.debug("DESCRIBE HISTORY failed for %s: %s", full_table_name, exc)

    return None, None


def _iter_table_rows(
    client: DatabricksClient,
    workspace_id: str,
    catalogs_filter: list[str] | None,
    warehouse_id: str | None,
    state: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield table metadata rows and deletion tombstones."""
    known_tables: set[str] = set(state.get("known_table_ids", []))
    current_inventory: set[str] = set()
    all_catalogs_succeeded = True

    try:
        available_catalogs = client.list_catalogs()
    except Exception as exc:
        logger.warning("Failed to list Unity Catalogs: %s", exc)
        return

    for cat_obj in available_catalogs:
        cat_name = cat_obj.get("name")
        if not cat_name or (catalogs_filter and cat_name not in catalogs_filter):
            continue

        try:
            schemas = client.list_schemas(cat_name)
        except Exception as exc:
            logger.warning("Failed to list schemas for catalog '%s': %s", cat_name, exc)
            all_catalogs_succeeded = False
            continue

        for sch_obj in schemas:
            sch_name = sch_obj.get("name")
            if not sch_name or sch_name.startswith("information_schema"):
                continue

            try:
                tables = client.list_tables(cat_name, sch_name)
            except Exception as exc:
                logger.warning("Failed to list tables in %s.%s: %s", cat_name, sch_name, exc)
                all_catalogs_succeeded = False
                continue

            for tbl in tables:
                tbl_name = tbl.get("name")
                if not tbl_name:
                    continue

                full_name = f"{cat_name}.{sch_name}.{tbl_name}"
                table_row_id = f"databricks:{workspace_id}:table:{full_name}"
                current_inventory.add(full_name)

                delta_ver, delta_ts = None, None
                if warehouse_id:
                    delta_ver, delta_ts = _fetch_delta_history(client, warehouse_id, full_name)

                yield {
                    "id": table_row_id,
                    "catalog": cat_name,
                    "schema": sch_name,
                    "table_name": tbl_name,
                    "table_type": tbl.get("table_type", "MANAGED"),
                    "comment": tbl.get("comment", ""),
                    "columns": [
                        {
                            "name": c.get("name"),
                            "type": c.get("type_name", "STRING"),
                            "comment": c.get("comment", ""),
                        }
                        for c in tbl.get("columns", [])
                    ],
                    "delta_version": delta_ver,
                    "last_altered": delta_ts,
                    "_deleted": False,
                }

    if all_catalogs_succeeded:
        deleted_tables = known_tables - current_inventory
        for del_tbl in deleted_tables:
            yield {
                "id": f"databricks:{workspace_id}:table:{del_tbl}",
                "_deleted": True,
            }
        state["known_table_ids"] = list(current_inventory)
    else:
        logger.warning("Catalog inventory was incomplete; skipping table deletion cleanup.")


def _iter_query_rows(
    client: DatabricksClient,
    workspace_id: str,
    query_configs: list[dict[str, Any]],
    state: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Execute explicitly declared SQL queries and stream result rows across chunks."""
    known_query_keys: set[str] = set(state.get("known_query_keys", []))
    current_query_keys: set[str] = set()

    for q_cfg in query_configs:
        q_name = q_cfg["name"]
        statement = q_cfg["statement"]
        warehouse_id = q_cfg["warehouse_id"]
        pk_field = q_cfg.get("primary_key")

        try:
            resp = client.execute_statement(statement, warehouse_id)
        except Exception as exc:
            logger.warning("Query '%s' failed to execute: %s", q_name, exc)
            continue

        stmt_id = resp.get("statement_id")
        if not stmt_id:
            continue

        status = resp.get("status", {}).get("state")
        while status in ("PENDING", "RUNNING"):
            time.sleep(0.5)
            check = client.get_statement(stmt_id)
            status = check.get("status", {}).get("state")
            if status == "SUCCEEDED":
                resp = check
                break

        if status != "SUCCEEDED":
            logger.warning("Query '%s' ended in state %s", q_name, status)
            continue

        manifest = resp.get("manifest", {})
        col_names = [c["name"].lower() for c in manifest.get("schema", {}).get("columns", [])]
        total_chunks = manifest.get("total_chunk_count", 1)

        for chunk_idx in range(total_chunks):
            if chunk_idx == 0:
                chunk_data = resp.get("result", {}).get("data_array", [])
            else:
                chunk_resp = client.get_statement_chunk(stmt_id, chunk_idx)
                chunk_data = chunk_resp.get("data_array", [])

            for row_values in chunk_data:
                row_dict = dict(zip(col_names, row_values, strict=False))

                if pk_field and pk_field.lower() in row_dict:
                    row_key = str(row_dict[pk_field.lower()])
                else:
                    row_key = hashlib.sha256(
                        json.dumps(row_dict, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()[:16]

                full_id = f"databricks:{workspace_id}:query:{q_name}:{row_key}"
                current_query_keys.add(full_id)

                row_dict["id"] = full_id
                row_dict["query_name"] = q_name
                row_dict["_deleted"] = False
                yield row_dict

    deleted_query_keys = known_query_keys - current_query_keys
    for del_key in deleted_query_keys:
        yield {"id": del_key, "_deleted": True}

    state["known_query_keys"] = list(current_query_keys)


def databricks_source(
    host: str | None = None,
    token: str | None = None,
    *,
    include: list[str] | None = None,
    workspace_paths: list[str] | None = None,
    catalogs: list[str] | None = None,
    queries: list[dict[str, Any]] | None = None,
    warehouse_id: str | None = None,
    write_disposition: str = "merge",
    client: Any = None,
):
    """Return a dlt source configured for Databricks notebooks, tables, and queries.

    Args:
        host: Databricks workspace host (e.g. 'https://dbc-1234.cloud.databricks.com').
            Falls back to ``DATABRICKS_HOST`` environment variable.
        token: Personal access token. Falls back to ``DATABRICKS_TOKEN``.
        include: List of resource names to include: ``["notebooks", "tables", "queries"]``.
            Defaults to all configured resources.
        workspace_paths: Workspace paths to traverse for notebooks (default: ``["/Shared"]``).
        catalogs: List of Unity Catalog catalog names to sync (default: all available).
        queries: Optional list of explicit queries to run:
            ``[{"name": "...", "statement": "SELECT ...", "warehouse_id": "..."}]``.
        warehouse_id: Default SQL warehouse ID for Delta history checks and queries.
        write_disposition: dlt write disposition (default: ``"merge"``).
        client: Pre-built ``DatabricksClient`` instance (test injection point).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Databricks connector requires dlt: "
            'pip install "cognee-community-connector-databricks"'
        ) from exc

    resolved_host = host or os.getenv("DATABRICKS_HOST")
    if not resolved_host:
        raise ValueError(
            "host is required (pass it explicitly or set the DATABRICKS_HOST environment variable)."
        )

    resolved_token = token or os.getenv("DATABRICKS_TOKEN")
    if not resolved_token:
        raise ValueError(
            "token is required "
            "(pass it explicitly or set the DATABRICKS_TOKEN environment variable)."
        )

    active_client = client or DatabricksClient(resolved_host, resolved_token)
    workspace_id = _get_workspace_id(resolved_host)
    selected_include = set(include or ["notebooks", "tables", "queries"])
    paths = workspace_paths or DEFAULT_WORKSPACE_PATHS

    resources = []

    # 1. Notebooks Resource (Document Mode)
    if "notebooks" in selected_include:

        @dlt.resource(
            name="databricks_notebooks",
            primary_key="id",
            write_disposition=write_disposition,
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def databricks_notebooks():
            yield from _iter_notebook_rows(
                active_client,
                workspace_id,
                paths,
                dlt.current.resource_state(),
            )

        nb_res = databricks_notebooks()
        setattr(nb_res, DOCUMENT_SOURCE_ATTR, "databricks_notebook")
        resources.append(nb_res)

    # 2. Tables Resource (Structured Relational Mode)
    if "tables" in selected_include:

        @dlt.resource(
            name="databricks_tables",
            primary_key="id",
            write_disposition=write_disposition,
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def databricks_tables():
            yield from _iter_table_rows(
                active_client,
                workspace_id,
                catalogs,
                warehouse_id,
                dlt.current.resource_state(),
            )

        resources.append(databricks_tables())

    # 3. Queries Resource (Structured SQL Results Mode)
    if "queries" in selected_include and queries:

        @dlt.resource(
            name="databricks_queries",
            primary_key="id",
            write_disposition=write_disposition,
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def databricks_queries():
            yield from _iter_query_rows(
                active_client,
                workspace_id,
                queries,
                dlt.current.resource_state(),
            )

        resources.append(databricks_queries())

    @dlt.source(name="databricks")
    def _databricks():
        return resources

    return _databricks()
