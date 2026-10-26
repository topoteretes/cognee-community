"""In-memory stand-in for the Tailscale API, shared by test modules."""

import pytest
from cognee_community_connector_tailscale.tailscale import (
    TailscaleAPIError,
    TailscaleAuthError,
    TailscaleQuotaError,
    TailscaleRateLimitedError,
)


class FakeTailscale:
    """Implements ``get`` for Tailscale endpoints: /devices, /acl, /routes."""

    def __init__(self):
        self.devices: dict[str, dict] = {}
        self.acl: dict = {
            "acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}],
            "groups": {"group:eng": ["alice@example.com", "bob@example.com"]},
            "tagOwners": {"tag:prod": ["group:eng"]},
        }
        self.routes: dict[str, dict] = {}
        self.calls: list[str] = []
        self.bad_token = False
        self.rate_limited = False
        self.quota_exhausted = False
        self.server_error = False

    def add_device(self, d_id: str, name: str, description: str = "", hostname: str = "prod-node", os: str = "linux", addresses=None, tags=None):
        dev = {
            "id": d_id,
            "name": name,
            "description": description,
            "hostname": hostname,
            "os": os,
            "addresses": addresses or ["100.64.0.1"],
            "tags": tags or ["tag:prod"],
            "authorized": True,
            "lastSeen": "2026-10-25T10:00:00.000Z",
            "clientVersion": "1.74.0",
        }
        self.devices[d_id] = dev
        return dev

    def add_route(self, r_id: str, prefix: str, node: str = "gateway-1"):
        route = {
            "id": r_id,
            "prefix": prefix,
            "nodeName": node,
            "enabled": True,
            "advertised": True,
        }
        self.routes[r_id] = route
        return route

    def get(self, path: str, params=None):
        self.calls.append(path)
        if self.bad_token:
            raise TailscaleAuthError("Tailscale rejected the API key (HTTP 401)")
        if self.rate_limited:
            raise TailscaleRateLimitedError("Tailscale rate limit exceeded (HTTP 429)")
        if self.quota_exhausted:
            raise TailscaleQuotaError("Tailscale quota exhausted")
        if self.server_error:
            raise TailscaleAPIError("Tailscale server error: HTTP 500", status=500)

        clean = path.strip("/")
        parts = clean.split("/")

        if len(parts) >= 3 and parts[0] == "tailnet":
            tailnet = parts[1]
            resource = parts[2]
            if resource == "devices":
                return {"devices": list(self.devices.values())}
            if resource == "acl":
                return dict(self.acl)
            if resource == "routes":
                return {"routes": list(self.routes.values())}
            raise TailscaleAPIError(f"Endpoint not found: {path}", status=404)

        raise TailscaleAPIError(f"Unknown path: {path}", status=404)


@pytest.fixture
def tailscale():
    return FakeTailscale()
