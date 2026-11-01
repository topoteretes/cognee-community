"""Unit tests for the AppFlowy knowledge base, workspace & database data-source connector.

All tests run completely offline without live server access or network requests,
using synthetic AppFlowy API fixtures and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_appflowy.appflowy import (
    APPFLOWY_SOURCE_NAME,
    APPFLOWY_TABLE_NAME,
    AppFlowyAPIError,
    AppFlowyAuthError,
    AppFlowyClient,
    AppFlowyConfig,
    AppFlowyRateLimitError,
    _build_database_markdown,
    _build_document_markdown,
    _build_workspaces_markdown,
    _redact_token,
    appflowy_connector,
    appflowy_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_WORKSPACES = {
    "data": [
        {
            "id": "ws_101",
            "name": "Engineering Wiki",
            "owner_name": "DevOps Lead",
            "member_count": 12,
            "created_at": "2024-01-15T09:00:00Z",
        },
        {
            "id": "ws_102",
            "name": "Product Roadmap",
            "owner_name": "Product VP",
            "member_count": 8,
            "created_at": "2024-02-01T10:30:00Z",
        },
    ]
}

SAMPLE_DOCUMENTS = {
    "data": [
        {
            "id": "doc_201",
            "name": "Production Deployment RFC",
            "parent_id": "root",
            "created_at": "2024-03-01T12:00:00Z",
            "updated_at": "2024-03-10T15:00:00Z",
        }
    ]
}

SAMPLE_DOC_CONTENT = {
    "content": "## Deployment Checklist\n1. Run migrations.\n2. Verify blue-green switch."
}

SAMPLE_DATABASES = {
    "data": [
        {
            "id": "db_301",
            "name": "Sprint 42 Backlog",
            "view_type": "kanban",
            "fields": [
                {"name": "Task Title", "type": "text"},
                {"name": "Status", "type": "select"},
                {"name": "Priority", "type": "select"},
            ],
        }
    ]
}

SAMPLE_ROWS = {
    "data": [
        {
            "id": "row_401",
            "cells": {
                "Task Title": "Implement Auth0 SSO",
                "Status": "In Progress",
                "Priority": "P0",
            },
        }
    ]
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic AppFlowy API responses."""

    def __init__(self, status_code: int = 200, error_text: str | None = None) -> None:
        self.status_code = status_code
        self.error_text = error_text
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            text = self.error_text or "Simulated AppFlowy error"
            return httpx.Response(status_code=self.status_code, text=text)

        path = request.url.path
        if "/workspace/" in path and "/document/" in path:
            return httpx.Response(200, json=SAMPLE_DOC_CONTENT)
        if "/workspace/" in path and "/document" in path:
            return httpx.Response(200, json=SAMPLE_DOCUMENTS)
        if "/workspace/" in path and "/database/" in path and "/row" in path:
            return httpx.Response(200, json=SAMPLE_ROWS)
        if "/workspace/" in path and "/database" in path:
            return httpx.Response(200, json=SAMPLE_DATABASES)
        if path.endswith("/workspace"):
            return httpx.Response(200, json=SAMPLE_WORKSPACES)

        return httpx.Response(200, json={"data": []})


# ---------------------------------------------------------------------------
# Helper & Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    token = "af_pat_super_secret_key_12345"
    msg = f"Failed to authenticate with token {token} on AppFlowy Cloud"
    redacted = _redact_token(msg, token)

    assert token not in redacted
    assert "[REDACTED_APPFLOWY_TOKEN]" in redacted


def test_redact_token_short_or_none() -> None:
    assert _redact_token("error msg", None) == "error msg"
    assert _redact_token("error msg", "12") == "error msg"


# ---------------------------------------------------------------------------
# AppFlowyClient Transport & Auth Tests
# ---------------------------------------------------------------------------


def test_client_custom_base_url() -> None:
    client = AppFlowyClient(
        api_key="token-test",
        base_url="http://self-hosted-appflowy.internal:8000/api/v1/",
    )
    assert client.base_url == "http://self-hosted-appflowy.internal:8000/api/v1"


def test_client_fetches_workspaces() -> None:
    transport = MockTransport()
    client = AppFlowyClient(api_key="token-test", client=httpx.Client(transport=transport))

    workspaces = client.get_workspaces()
    assert len(workspaces) == 2
    assert workspaces[0]["id"] == "ws_101"
    assert workspaces[0]["name"] == "Engineering Wiki"
    assert workspaces[1]["id"] == "ws_102"


def test_client_fetches_documents_and_content() -> None:
    transport = MockTransport()
    client = AppFlowyClient(api_key="token-test", client=httpx.Client(transport=transport))

    docs = client.get_documents("ws_101")
    assert len(docs) == 1
    assert docs[0]["name"] == "Production Deployment RFC"

    content = client.get_document_content("ws_101", "doc_201")
    assert "Deployment Checklist" in content["content"]


def test_client_fetches_databases_and_rows() -> None:
    transport = MockTransport()
    client = AppFlowyClient(api_key="token-test", client=httpx.Client(transport=transport))

    dbs = client.get_databases("ws_101")
    assert len(dbs) == 1
    assert dbs[0]["name"] == "Sprint 42 Backlog"

    rows = client.get_database_rows("ws_101", "db_301")
    assert len(rows) == 1
    assert rows[0]["cells"]["Task Title"] == "Implement Auth0 SSO"


def test_client_handles_auth_error_401() -> None:
    token = "invalid-secret-af-token"
    transport = MockTransport(
        status_code=401,
        error_text=f"Unauthorized access for token: {token}",
    )
    client = AppFlowyClient(api_key=token, client=httpx.Client(transport=transport))

    with pytest.raises(AppFlowyAuthError) as exc_info:
        client.get_workspaces()

    assert token not in str(exc_info.value)
    assert "[REDACTED_APPFLOWY_TOKEN]" in str(exc_info.value)


def test_client_handles_rate_limit_429() -> None:
    transport = MockTransport(status_code=429)
    client = AppFlowyClient(api_key="token-test", client=httpx.Client(transport=transport))

    with pytest.raises(AppFlowyRateLimitError):
        client.get_workspaces()


def test_client_handles_server_error_500() -> None:
    transport = MockTransport(status_code=500)
    client = AppFlowyClient(api_key="token-test", client=httpx.Client(transport=transport))

    with pytest.raises(AppFlowyAPIError):
        client.get_workspaces()


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_workspaces_markdown() -> None:
    workspaces = SAMPLE_WORKSPACES["data"]
    md = _build_workspaces_markdown(workspaces, "https://api.appflowy.cloud/api/v1")

    assert "# AppFlowy Workspaces & Knowledge Base Overview" in md
    assert "Engineering Wiki" in md
    assert "Product Roadmap" in md
    assert "DevOps Lead" in md
    assert "Member Count**: 12" in md


def test_build_document_markdown() -> None:
    doc = SAMPLE_DOCUMENTS["data"][0]
    content_data = SAMPLE_DOC_CONTENT
    md = _build_document_markdown("Engineering Wiki", doc, content_data)

    assert "# AppFlowy Document: Production Deployment RFC" in md
    assert "Workspace**: `Engineering Wiki`" in md
    assert "Deployment Checklist" in md
    assert "Verify blue-green switch" in md


def test_build_database_markdown() -> None:
    db = SAMPLE_DATABASES["data"][0]
    rows = SAMPLE_ROWS["data"]
    md = _build_database_markdown("Engineering Wiki", db, rows)

    assert "# AppFlowy Database: Sprint 42 Backlog" in md
    assert "View Type**: `kanban`" in md
    assert "Task Title" in md
    assert "Implement Auth0 SSO" in md
    assert "In Progress" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_appflowy_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = AppFlowyClient("token-test", client=httpx.Client(transport=transport))

    config = AppFlowyConfig(api_key="token-test")
    source = appflowy_source(config=config, client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "appflowy"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == APPFLOWY_SOURCE_NAME


def test_appflowy_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = AppFlowyClient("token-test", client=httpx.Client(transport=transport))

    config = AppFlowyConfig(
        api_key="token-test",
        include_documents=True,
        include_databases=True,
    )
    source = appflowy_source(config=config, client=fake_client)

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == APPFLOWY_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    # 1 overview + 2 workspaces * (1 doc + 1 db) = 1 + 4 = 5 documents
    assert len(rows) == 5

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert "workspaces_overview" in report_types
    assert "document_content" in report_types
    assert "database_catalog" in report_types

    for r in rows:
        assert r["metadata"]["source"] == APPFLOWY_SOURCE_NAME
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_appflowy_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = AppFlowyClient("token-test", client=httpx.Client(transport=transport))

    config = AppFlowyConfig(api_key="token-test")

    source_1 = appflowy_source(config=config, client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = appflowy_source(config=config, client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure repeat runs update existing nodes without duplicates
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_appflowy_source_filters_by_workspace_id() -> None:
    transport = MockTransport()
    fake_client = AppFlowyClient("token-test", client=httpx.Client(transport=transport))

    config = AppFlowyConfig(
        api_key="token-test",
        workspace_ids=["ws_101"],
        include_documents=True,
        include_databases=False,
    )
    source = appflowy_source(config=config, client=fake_client)

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    # 1 overview + 1 doc = 2
    assert len(rows) == 2
    assert rows[0]["metadata"]["report_type"] == "workspaces_overview"
    assert rows[1]["metadata"]["report_type"] == "document_content"
    assert rows[1]["metadata"]["workspace_id"] == "ws_101"


@pytest.mark.asyncio
async def test_appflowy_connector_async_wrapper() -> None:
    transport = MockTransport()
    fake_client = AppFlowyClient("token-test", client=httpx.Client(transport=transport))

    config = AppFlowyConfig(
        api_key="token-test",
        workspace_ids=["ws_101"],
        include_documents=True,
        include_databases=True,
    )

    documents = await appflowy_connector(config, client=fake_client)
    # 1 overview + 1 doc + 1 db = 3
    assert len(documents) == 3
    for doc in documents:
        assert "id" in doc
        assert "text" in doc
        assert doc["metadata"]["source"] == "appflowy"
