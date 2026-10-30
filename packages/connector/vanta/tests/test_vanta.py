"""Unit and integration tests for the Vanta data-source connector."""

import httpx
import pytest
from cognee_community_connector_vanta.vanta import (
    VantaAPIError,
    VantaAuthError,
    VantaClient,
    VantaRateLimitedError,
    _iter_rows,
    _render_control_markdown,
    _render_policy_markdown,
    _render_vendor_markdown,
    vanta_source,
)


def test_client_empty_token_raises():
    with pytest.raises(VantaAuthError):
        VantaClient(api_token="   ")


def test_client_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer vanta_test_token"
        return httpx.Response(200, json=[{"id": "ctrl_1", "name": "MFA"}])

    transport = httpx.MockTransport(handler)
    client = VantaClient(api_token="vanta_test_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.vanta.com/v1")
    client._owns_client = True

    data = client.get("/controls")
    assert len(data) == 1
    assert data[0]["id"] == "ctrl_1"
    client.close()


def test_client_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = VantaClient(api_token="invalid_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.vanta.com/v1")
    client._owns_client = True

    with pytest.raises(VantaAuthError):
        client.get("/controls")
    client.close()


def test_client_429_long_retry_after_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": "Rate limit exceeded"})

    transport = httpx.MockTransport(handler)
    client = VantaClient(api_token="test_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.vanta.com/v1")
    client._owns_client = True

    with pytest.raises(VantaRateLimitedError):
        client.get("/controls")
    client.close()


def test_client_check_active_cancelled():
    def _cancel():
        raise VantaAPIError("Run cancelled by caller")

    client = VantaClient(api_token="test_token", check_active=_cancel)
    with pytest.raises(VantaAPIError, match="Run cancelled"):
        client.get("/controls")


def test_client_budget_exceeded():
    client = VantaClient(api_token="test_token", max_requests=1)
    client.calls.append("/controls")
    with pytest.raises(VantaAPIError, match="budget exceeded"):
        client.get("/controls")


def test_render_control_markdown():
    ctrl = {
        "id": "ctrl_mfa_101",
        "name": "Enforce Multi-Factor Authentication",
        "description": "All employee accounts must require MFA.",
        "category": "Access Control",
        "frameworks": ["SOC 2", "ISO 27001"],
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_control_markdown(ctrl)
    assert doc_id == "vanta:control:ctrl_mfa_101"
    assert "Enforce Multi-Factor Authentication" in title
    assert "Access Control" in content
    assert "SOC 2, ISO 27001" in content
    assert "All employee accounts must require MFA." in content


def test_render_policy_markdown():
    policy = {
        "id": "pol_sec_200",
        "title": "Information Security Policy",
        "category": "Security Governance",
        "description": "Defines core security rules and data handling procedures.",
        "renewal_date": "2027-06-30",
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_policy_markdown(policy)
    assert doc_id == "vanta:policy:pol_sec_200"
    assert "Information Security Policy" in title
    assert "Security Governance" in content
    assert "2027-06-30" in content


def test_render_vendor_markdown():
    vendor = {
        "id": "ven_aws_300",
        "name": "Amazon Web Services",
        "risk_level": "High",
        "service_category": "Cloud Infrastructure",
        "description": "SOC 2 Type II report renewed annually.",
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_vendor_markdown(vendor)
    assert doc_id == "vanta:vendor:ven_aws_300"
    assert "Amazon Web Services" in title
    assert "High" in content
    assert "Cloud Infrastructure" in content


def test_source_yields_all_selected_items(vanta):
    vanta.add_control("ctrl_1", "Control One")
    vanta.add_policy("pol_1", "Policy One")
    vanta.add_vendor("ven_1", "Vendor One")

    state = {}
    stats = {}
    items = list(_iter_rows(vanta, ("controls", "policies", "vendors"), state, stats))
    assert len(items) == 3
    assert stats["emitted"] == 3
    assert stats["unchanged"] == 0
    assert stats["deleted"] == 0


def test_incremental_sync_skips_unchanged_content(vanta):
    vanta.add_control("ctrl_1", "Control One", "Description 1")
    vanta.add_policy("pol_1", "Policy One", "Description 2")

    state = {}
    stats = {}
    list(_iter_rows(vanta, ("controls", "policies"), state, stats))
    assert stats["emitted"] == 2

    # Second sync: same content, zero rows emitted
    stats.clear()
    items_second = list(_iter_rows(vanta, ("controls", "policies"), state, stats))
    assert len(items_second) == 0
    assert stats["emitted"] == 0
    assert stats["unchanged"] == 2


def test_updated_item_reemits_new_version(vanta):
    vanta.add_control("ctrl_1", "Control Initial", "Old description")
    state = {}
    stats = {}
    list(_iter_rows(vanta, ("controls",), state, stats))

    # Update description
    vanta.controls["ctrl_1"]["description"] = "Brand new updated description"
    stats.clear()
    updated = list(_iter_rows(vanta, ("controls",), state, stats))
    assert len(updated) == 1
    assert stats["emitted"] == 1
    assert "Brand new updated description" in updated[0]["content"]


def test_deleted_item_emits_hard_delete_tombstone(vanta):
    vanta.add_control("ctrl_1", "Control 1")
    vanta.add_control("ctrl_2", "Control 2")
    state = {}
    stats = {}
    list(_iter_rows(vanta, ("controls",), state, stats))

    # Delete ctrl_2
    del vanta.controls["ctrl_2"]
    stats.clear()
    deleted = list(_iter_rows(vanta, ("controls",), state, stats))
    assert len(deleted) == 1
    assert deleted[0]["id"] == "vanta:control:ctrl_2"
    assert deleted[0]["_deleted"] is True
    assert stats["deleted"] == 1
