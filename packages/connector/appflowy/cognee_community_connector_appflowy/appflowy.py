"""AppFlowy knowledge base, workspace & database connector for Cognee.

Ingests workspaces, hierarchical document trees, Markdown page content,
and database views (tables/kanban) from AppFlowy into structured documents for Cognee memory.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Iterable

import dlt
import httpx
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

APPFLOWY_SOURCE_NAME = "appflowy"
APPFLOWY_TABLE_NAME = "appflowy_knowledge"

DEFAULT_CLOUD_BASE_URL = "https://api.appflowy.cloud/api/v1"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


class AppFlowyAPIError(Exception):
    """Base exception for AppFlowy connector errors."""


class AppFlowyAuthError(AppFlowyAPIError):
    """Raised when AppFlowy rejects authentication credentials (401/403)."""


class AppFlowyRateLimitError(AppFlowyAPIError):
    """Raised when AppFlowy throttles requests (429)."""


def _redact_token(text: str, token: str | None) -> str:
    """Mask credential strings in error messages to prevent leakage."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED_APPFLOWY_TOKEN]")


class AppFlowyConfig(BaseModel):
    """Configuration for AppFlowy data-source connector."""

    api_key: str = Field(
        ...,
        description="AppFlowy Cloud or self-hosted API token",
    )
    base_url: str = Field(
        default=DEFAULT_CLOUD_BASE_URL,
        description="AppFlowy API base endpoint URL",
    )
    workspace_ids: list[str] | None = Field(
        default=None,
        description="Specific workspace IDs to ingest (None for all accessible)",
    )
    include_documents: bool = Field(
        default=True,
        description="Whether to ingest documents, pages, and hierarchical text content",
    )
    include_databases: bool = Field(
        default=True,
        description="Whether to ingest database tables, Kanban views, and record rows",
    )
    limit: int = Field(
        default=50,
        description="Pagination limit for database records and documents",
    )
    request_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )


class AppFlowyClient:
    """Synchronous HTTP client for interacting with AppFlowy REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_CLOUD_BASE_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._external_client = client

    def _get_client(self) -> httpx.Client:
        if self._external_client is not None:
            return self._external_client
        return httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "cognee-community-connector-appflowy/0.1.0",
            },
            timeout=self.timeout,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        client = self._get_client()

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.request(
                    method=method,
                    url=url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "application/json",
                    },
                    params=params,
                )

                if response.status_code in (401, 403):
                    msg = (
                        f"AppFlowy authentication failed ({response.status_code}): {response.text}"
                    )
                    raise AppFlowyAuthError(_redact_token(msg, self.api_key))

                if response.status_code == 429:
                    if attempt == _MAX_RETRIES:
                        raise AppFlowyRateLimitError(
                            "AppFlowy rate limit exceeded (429) after maximum retries"
                        )
                    backoff = _BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "Rate limited by AppFlowy (429). Retrying in %.2fs (attempt %d/%d)",
                        backoff,
                        attempt,
                        _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    if attempt == _MAX_RETRIES:
                        msg = f"AppFlowy server error ({response.status_code}): {response.text}"
                        raise AppFlowyAPIError(_redact_token(msg, self.api_key))
                    backoff = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "AppFlowy server error (%d). Retrying in %.2fs...",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {"data": data}

            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    msg = f"AppFlowy connection error: {exc}"
                    raise AppFlowyAPIError(_redact_token(msg, self.api_key)) from exc
                time.sleep(_BASE_BACKOFF * attempt)

        raise AppFlowyAPIError("Failed to communicate with AppFlowy after retries")

    def get_workspaces(self) -> list[dict[str, Any]]:
        """Fetch accessible workspaces."""
        res = self._request("GET", "/workspace")
        data = res.get("data", res.get("workspaces", []))
        return data if isinstance(data, list) else []

    def get_documents(self, workspace_id: str) -> list[dict[str, Any]]:
        """Fetch document tree for a workspace."""
        res = self._request("GET", f"/workspace/{workspace_id}/document")
        data = res.get("data", res.get("documents", []))
        return data if isinstance(data, list) else []

    def get_document_content(self, workspace_id: str, document_id: str) -> dict[str, Any]:
        """Fetch document content body and metadata."""
        return self._request("GET", f"/workspace/{workspace_id}/document/{document_id}")

    def get_databases(self, workspace_id: str) -> list[dict[str, Any]]:
        """Fetch databases and views for a workspace."""
        res = self._request("GET", f"/workspace/{workspace_id}/database")
        data = res.get("data", res.get("databases", []))
        return data if isinstance(data, list) else []

    def get_database_rows(
        self,
        workspace_id: str,
        database_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch rows and records for an AppFlowy database."""
        res = self._request(
            "GET",
            f"/workspace/{workspace_id}/database/{database_id}/row",
            params={"limit": limit},
        )
        data = res.get("data", res.get("rows", []))
        return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Markdown Formatting Helpers
# ---------------------------------------------------------------------------


def _build_workspaces_markdown(workspaces: list[dict[str, Any]], base_url: str) -> str:
    """Format workspaces overview into a clean Markdown document."""
    lines = [
        "# AppFlowy Workspaces & Knowledge Base Overview",
        "",
        f"**Instance Endpoint**: `{base_url}`",
        f"**Total Accessible Workspaces**: {len(workspaces)}",
        "",
        "## Workspace Directory",
    ]

    for ws in workspaces:
        ws_id = ws.get("id", ws.get("workspace_id", "unknown"))
        ws_name = ws.get("name", "Untitled Workspace")
        owner = ws.get("owner_name", ws.get("owner", "n/a"))
        created_at = ws.get("created_at", "n/a")
        member_count = ws.get("member_count", 1)

        lines.extend(
            [
                f"### Workspace: `{ws_name}`",
                f"- **Workspace ID**: `{ws_id}`",
                f"- **Owner**: {owner}",
                f"- **Member Count**: {member_count}",
                f"- **Created At**: {created_at}",
                "",
            ]
        )

    return "\n".join(lines)


def _build_document_markdown(
    workspace_name: str,
    doc: dict[str, Any],
    content_data: dict[str, Any],
) -> str:
    """Format an AppFlowy document page into Markdown for Cognee."""
    doc_id = doc.get("id", "unknown")
    doc_title = doc.get("name", doc.get("title", "Untitled Document"))
    parent_id = doc.get("parent_id", "root")
    created_at = doc.get("created_at", "n/a")
    updated_at = doc.get("updated_at", "n/a")

    body_text = (
        content_data.get("content", "")
        or content_data.get("data", {}).get("content", "")
        or content_data.get("text", "")
    )
    if not body_text:
        body_text = "*[No written content in this document]*"

    lines = [
        f"# AppFlowy Document: {doc_title}",
        "",
        f"**Workspace**: `{workspace_name}`",
        f"**Document ID**: `{doc_id}`",
        f"**Parent ID**: `{parent_id}`",
        f"**Created At**: {created_at}",
        f"**Updated At**: {updated_at}",
        "",
        "## Document Content",
        body_text.strip(),
        "",
    ]
    return "\n".join(lines)


def _build_database_markdown(
    workspace_name: str,
    db: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    """Format an AppFlowy database (table/kanban/calendar) into Markdown."""
    db_id = db.get("id", "unknown")
    db_name = db.get("name", "Untitled Database")
    view_type = db.get("view_type", db.get("type", "table"))
    fields = db.get("fields", db.get("columns", []))

    lines = [
        f"# AppFlowy Database: {db_name}",
        "",
        f"**Workspace**: `{workspace_name}`",
        f"**Database ID**: `{db_id}`",
        f"**View Type**: `{view_type}`",
        f"**Total Records**: {len(rows)}",
        "",
        "## Field Schema",
    ]

    for f in fields:
        f_name = f.get("name", "Unnamed Field")
        f_type = f.get("type", "text")
        lines.append(f"- **{f_name}** (`{f_type}`)")

    lines.extend(["", "## Database Records"])

    for idx, r in enumerate(rows, 1):
        row_id = r.get("id", f"row_{idx}")
        cells = r.get("cells", r.get("data", {}))
        lines.append(f"### Record {idx} (`{row_id}`)")

        if isinstance(cells, dict):
            for k, v in cells.items():
                lines.append(f"- **{k}**: {v}")
        else:
            lines.append(f"- **Raw Value**: {cells}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DLT Source Pipeline
# ---------------------------------------------------------------------------


def appflowy_source(
    config: AppFlowyConfig | None = None,
    api_key: str | None = None,
    base_url: str = DEFAULT_CLOUD_BASE_URL,
    workspace_ids: list[str] | None = None,
    include_documents: bool = True,
    include_databases: bool = True,
    client: AppFlowyClient | None = None,
) -> Any:
    """DLT data source for ingesting AppFlowy workspaces, documents, and databases."""
    if config is not None:
        cfg = config
    elif api_key is not None:
        cfg = AppFlowyConfig(
            api_key=api_key,
            base_url=base_url,
            workspace_ids=workspace_ids,
            include_documents=include_documents,
            include_databases=include_databases,
        )
    else:
        raise ValueError("Either 'config' or 'api_key' must be provided.")

    af_client = client or AppFlowyClient(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.request_timeout,
    )

    @dlt.resource(name=APPFLOWY_TABLE_NAME, write_disposition="replace")
    def appflowy_resource() -> Iterable[dict[str, Any]]:
        workspaces = af_client.get_workspaces()

        if cfg.workspace_ids:
            workspaces = [
                ws
                for ws in workspaces
                if str(ws.get("id", ws.get("workspace_id"))) in cfg.workspace_ids
            ]

        # 1. Ingest Workspaces Overview
        if workspaces:
            ws_md = _build_workspaces_markdown(workspaces, cfg.base_url)
            ws_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, "appflowy:workspaces_overview"))
            yield {
                "id": ws_doc_id,
                "text": ws_md,
                "metadata": {
                    "source": APPFLOWY_SOURCE_NAME,
                    "report_type": "workspaces_overview",
                    "total_workspaces": len(workspaces),
                },
            }

        # 2. Ingest Per-Workspace Documents and Databases
        for ws in workspaces:
            ws_id = str(ws.get("id", ws.get("workspace_id", "default")))
            ws_name = ws.get("name", "Untitled Workspace")

            # Document hierarchy & content
            if cfg.include_documents:
                documents = af_client.get_documents(ws_id)
                for doc in documents:
                    doc_id = str(doc.get("id"))
                    content_res = af_client.get_document_content(ws_id, doc_id)
                    doc_md = _build_document_markdown(ws_name, doc, content_res)
                    doc_node_id = str(
                        uuid.uuid5(uuid.NAMESPACE_OID, f"appflowy:{ws_id}:doc:{doc_id}")
                    )
                    yield {
                        "id": doc_node_id,
                        "text": doc_md,
                        "metadata": {
                            "source": APPFLOWY_SOURCE_NAME,
                            "report_type": "document_content",
                            "workspace_id": ws_id,
                            "workspace_name": ws_name,
                            "document_id": doc_id,
                        },
                    }

            # Database views & records
            if cfg.include_databases:
                databases = af_client.get_databases(ws_id)
                for db in databases:
                    db_id = str(db.get("id"))
                    rows = af_client.get_database_rows(ws_id, db_id, limit=cfg.limit)
                    db_md = _build_database_markdown(ws_name, db, rows)
                    db_node_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"appflowy:{ws_id}:db:{db_id}"))
                    yield {
                        "id": db_node_id,
                        "text": db_md,
                        "metadata": {
                            "source": APPFLOWY_SOURCE_NAME,
                            "report_type": "database_catalog",
                            "workspace_id": ws_id,
                            "workspace_name": ws_name,
                            "database_id": db_id,
                            "total_records": len(rows),
                        },
                    }

    @dlt.source(name=APPFLOWY_SOURCE_NAME)
    def _source() -> Any:
        return appflowy_resource

    source_inst = _source()
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, APPFLOWY_SOURCE_NAME)
    return source_inst


async def appflowy_connector(
    config: AppFlowyConfig,
    client: AppFlowyClient | None = None,
) -> list[dict[str, Any]]:
    """Convenience async wrapper returning ingested documents directly for cognee.add()."""
    source = appflowy_source(config=config, client=client)
    documents: list[dict[str, Any]] = []

    for resource_func in source.selected_resources.values():
        for item in resource_func():
            documents.append(item)

    return documents
