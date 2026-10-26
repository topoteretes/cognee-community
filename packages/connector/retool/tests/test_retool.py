"""Unit and integration tests for the Retool data-source connector."""

import httpx
import pytest
from cognee_community_connector_retool.retool import (
    RetoolAPIError,
    RetoolAuthError,
    RetoolClient,
    RetoolRateLimitedError,
    _iter_rows,
    _render_app_markdown,
    _render_resource_markdown,
    _render_workflow_markdown,
    retool_source,
)


def test_client_empty_token_raises():
    with pytest.raises(RetoolAuthError):
        RetoolClient(token="   ")


def test_client_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        assert auth == "Bearer test_secret_token"
        return httpx.Response(200, json=[{"id": "app_1", "name": "App 1"}])

    transport = httpx.MockTransport(handler)
    client = RetoolClient(token="test_secret_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.retool.com/v1")
    client._owns_client = True

    data = client.get("/apps")
    assert len(data) == 1
    assert data[0]["name"] == "App 1"
    client.close()


def test_client_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = RetoolClient(token="invalid_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.retool.com/v1")
    client._owns_client = True

    with pytest.raises(RetoolAuthError):
        client.get("/apps")
    client.close()


def test_client_429_long_retry_after_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": "Too Many Requests"})

    transport = httpx.MockTransport(handler)
    client = RetoolClient(token="test_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.retool.com/v1")
    client._owns_client = True

    with pytest.raises(RetoolRateLimitedError):
        client.get("/apps")
    client.close()


def test_client_check_active_cancelled():
    def _cancel():
        raise RetoolAPIError("Run cancelled by caller")

    client = RetoolClient(token="test_token", check_active=_cancel)
    with pytest.raises(RetoolAPIError, match="Run cancelled"):
        client.get("/apps")


def test_client_budget_exceeded():
    client = RetoolClient(token="test_token", max_requests=1)
    client.calls.append("/apps")
    with pytest.raises(RetoolAPIError, match="budget exceeded"):
        client.get("/apps")


def test_render_app_markdown():
    app = {
        "id": "app_100",
        "name": "User Dashboard",
        "description": "Customer operations dashboard",
        "folder": "Operations",
        "currentVersion": "2.1.0",
        "updatedAt": "2026-10-25T12:00:00Z",
        "components": ["userTable", "filterDropdown"],
        "queries": ["fetchUsers", "updateUserStatus"],
    }
    doc_id, title, content = _render_app_markdown(app)
    assert doc_id == "retool:app:app_100"
    assert "User Dashboard" in title
    assert "userTable" in content
    assert "fetchUsers" in content
    assert "Customer operations dashboard" in content


def test_render_workflow_markdown():
    wf = {
        "id": "wf_200",
        "name": "Sync Webhook",
        "description": "Sync customer webhooks",
        "folder": "Automations",
        "triggers": ["webhook", "cron"],
        "updatedAt": "2026-10-25T12:00:00Z",
        "blocks": [{"name": "processEvent", "type": "code"}],
    }
    doc_id, title, content = _render_workflow_markdown(wf)
    assert doc_id == "retool:workflow:wf_200"
    assert "Sync Webhook" in title
    assert "processEvent" in content
    assert "webhook, cron" in content


def test_render_resource_markdown():
    res = {
        "id": "res_300",
        "displayName": "Stripe Analytics DB",
        "type": "postgres",
        "environment": "production",
        "description": "Read-only replica for analytics",
    }
    doc_id, title, content = _render_resource_markdown(res)
    assert doc_id == "retool:resource:res_300"
    assert "Stripe Analytics DB" in title
    assert "postgres" in content
    assert "production" in content


def test_source_invalid_resource_raises():
    with pytest.raises(ValueError, match="Unknown Retool resource"):
        retool_source(token="test", resources=["invalid_resource"])


def test_source_yields_all_selected_items(retool):
    retool.add_app("app_1", "Customer Ops")
    retool.add_workflow("wf_1", "Alert Webhook")
    retool.add_resource("res_1", "Main DB")

    state = {}
    stats = {}
    rows = list(_iter_rows(retool, ("apps", "workflows", "resources"), state=state, stats=stats))

    assert len(rows) == 3
    assert stats["emitted"] == 3
    ids = {r["id"] for r in rows}
    assert "retool:app:app_1" in ids
    assert "retool:workflow:wf_1" in ids
    assert "retool:resource:res_1" in ids
    assert all(not r["_deleted"] for r in rows)


def test_source_incremental_sync_skips_unchanged(retool):
    app = retool.add_app("app_1", "Inventory App")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(retool, ("apps",), state=state, stats=stats1))
    assert len(rows1) == 1
    assert stats1["emitted"] == 1

    # Second sync without changes emits 0 rows
    stats2 = {}
    rows2 = list(_iter_rows(retool, ("apps",), state=state, stats=stats2))
    assert len(rows2) == 0
    assert stats2["unchanged"] == 1

    # Modify app and re-sync
    app["name"] = "Inventory App v2"
    app["updatedAt"] = "2026-10-26T12:00:00Z"

    stats3 = {}
    rows3 = list(_iter_rows(retool, ("apps",), state=state, stats=stats3))
    assert len(rows3) == 1
    assert stats3["emitted"] == 1
    assert "Inventory App v2" in rows3[0]["title"]


def test_source_forgets_deleted_items(retool):
    app1 = retool.add_app("app_1", "Billing Portal")
    app2 = retool.add_app("app_2", "Legacy Portal")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(retool, ("apps",), state=state, stats=stats1))
    assert len(rows1) == 2

    # Delete legacy portal
    del retool.apps["app_2"]

    stats2 = {}
    rows2 = list(_iter_rows(retool, ("apps",), state=state, stats=stats2))

    assert len(rows2) == 1
    assert stats2["deleted"] == 1
    assert rows2[0]["id"] == "retool:app:app_2"
    assert rows2[0]["_deleted"] is True
