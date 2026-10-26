"""Unit and integration tests for the Tailscale data-source connector."""

import httpx
import pytest
from cognee_community_connector_tailscale.tailscale import (
    TailscaleAPIError,
    TailscaleAuthError,
    TailscaleClient,
    TailscaleRateLimitedError,
    _iter_rows,
    _render_acl_markdown,
    _render_device_markdown,
    _render_route_markdown,
    tailscale_source,
)


def test_client_empty_key_raises():
    with pytest.raises(TailscaleAuthError):
        TailscaleClient(api_key="   ")


def test_client_sends_bearer_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer tskey-api-test"
        return httpx.Response(200, json={"devices": [{"id": "node_1", "name": "bastion"}]})

    transport = httpx.MockTransport(handler)
    client = TailscaleClient(api_key="tskey-api-test")
    client._client = httpx.Client(transport=transport, base_url="https://api.tailscale.com/api/v2")
    client._owns_client = True

    data = client.get("/tailnet/-/devices")
    assert "devices" in data
    assert len(data["devices"]) == 1
    assert data["devices"][0]["id"] == "node_1"
    client.close()


def test_client_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = TailscaleClient(api_key="invalid_key")
    client._client = httpx.Client(transport=transport, base_url="https://api.tailscale.com/api/v2")
    client._owns_client = True

    with pytest.raises(TailscaleAuthError):
        client.get("/tailnet/-/devices")
    client.close()


def test_client_429_long_retry_after_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "Rate limit exceeded"})

    transport = httpx.MockTransport(handler)
    client = TailscaleClient(api_key="test_key")
    client._client = httpx.Client(transport=transport, base_url="https://api.tailscale.com/api/v2")
    client._owns_client = True

    with pytest.raises(TailscaleRateLimitedError):
        client.get("/tailnet/-/devices")
    client.close()


def test_client_check_active_cancelled():
    def _cancel():
        raise TailscaleAPIError("Run cancelled by caller")

    client = TailscaleClient(api_key="test_key", check_active=_cancel)
    with pytest.raises(TailscaleAPIError, match="Run cancelled"):
        client.get("/tailnet/-/devices")


def test_client_budget_exceeded():
    client = TailscaleClient(api_key="test_key", max_requests=1)
    client.calls.append("/tailnet/-/devices")
    with pytest.raises(TailscaleAPIError, match="budget exceeded"):
        client.get("/tailnet/-/devices")


def test_render_device_markdown():
    dev = {
        "id": "node_prod_100",
        "name": "bastion-vpn.ts.net",
        "hostname": "bastion-vpn",
        "os": "linux",
        "addresses": ["100.64.0.1", "fd7a:115c:a1e0::1"],
        "tags": ["tag:server", "tag:gateway"],
        "authorized": True,
        "lastSeen": "2026-10-25T12:00:00Z",
        "clientVersion": "1.74.0",
        "advertisedRoutes": ["10.0.0.0/16"],
    }
    doc_id, title, content = _render_device_markdown(dev)
    assert doc_id == "tailscale:device:node_prod_100"
    assert "bastion-vpn.ts.net" in title
    assert "linux" in content
    assert "100.64.0.1" in content
    assert "tag:server, tag:gateway" in content
    assert "10.0.0.0/16" in content


def test_render_acl_markdown():
    acl = {
        "acls": [{"action": "accept", "src": ["tag:server"], "dst": ["tag:db:5432"]}],
        "groups": {"group:admins": ["admin@example.com"]},
        "tagOwners": {"tag:server": ["group:admins"]},
    }
    doc_id, title, content = _render_acl_markdown(acl, "example-tailnet")
    assert doc_id == "tailscale:acl:example-tailnet"
    assert "example-tailnet" in title
    assert "tag:server" in content
    assert "group:admins" in content


def test_render_route_markdown():
    route = {
        "id": "rt_sub_1",
        "prefix": "192.168.1.0/24",
        "nodeName": "edge-router",
        "enabled": True,
        "advertised": True,
    }
    doc_id, title, content = _render_route_markdown(route)
    assert doc_id == "tailscale:route:rt_sub_1"
    assert "192.168.1.0/24" in title
    assert "edge-router" in content
    assert "Subnet Route Prefix" in content


def test_source_yields_all_selected_items(tailscale):
    tailscale.add_device("node_1", "Node One")
    tailscale.add_route("rt_1", "10.0.0.0/24")

    state = {}
    stats = {}
    items = list(_iter_rows(tailscale, "-", ("devices", "acls", "routes"), state, stats))
    # 1 device + 1 acl + 1 route = 3
    assert len(items) == 3
    assert stats["emitted"] == 3
    assert stats["unchanged"] == 0
    assert stats["deleted"] == 0


def test_incremental_sync_skips_unchanged_content(tailscale):
    tailscale.add_device("node_1", "Node One")

    state = {}
    stats = {}
    list(_iter_rows(tailscale, "-", ("devices",), state, stats))
    assert stats["emitted"] == 1

    # Second sync: same content, zero rows emitted
    stats.clear()
    items_second = list(_iter_rows(tailscale, "-", ("devices",), state, stats))
    assert len(items_second) == 0
    assert stats["emitted"] == 0
    assert stats["unchanged"] == 1


def test_updated_item_reemits_new_version(tailscale):
    tailscale.add_device("node_1", "Node One")
    state = {}
    stats = {}
    list(_iter_rows(tailscale, "-", ("devices",), state, stats))

    # Update advertisedRoutes
    tailscale.devices["node_1"]["advertisedRoutes"] = ["172.16.0.0/16"]
    stats.clear()
    updated = list(_iter_rows(tailscale, "-", ("devices",), state, stats))
    assert len(updated) == 1
    assert stats["emitted"] == 1
    assert "172.16.0.0/16" in updated[0]["content"]


def test_deleted_item_emits_hard_delete_tombstone(tailscale):
    tailscale.add_device("node_1", "Node 1")
    tailscale.add_device("node_2", "Node 2")
    state = {}
    stats = {}
    list(_iter_rows(tailscale, "-", ("devices",), state, stats))

    # Delete node_2
    del tailscale.devices["node_2"]
    stats.clear()
    deleted = list(_iter_rows(tailscale, "-", ("devices",), state, stats))
    assert len(deleted) == 1
    assert deleted[0]["id"] == "tailscale:device:node_2"
    assert deleted[0]["_deleted"] is True
    assert stats["deleted"] == 1
