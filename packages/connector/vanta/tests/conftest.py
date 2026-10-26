"""In-memory stand-in for the Vanta API, shared by test modules."""

import pytest
from cognee_community_connector_vanta.vanta import (
    VantaAPIError,
    VantaAuthError,
    VantaQuotaError,
    VantaRateLimitedError,
)


class FakeVanta:
    """Implements ``get`` for Vanta endpoints: /controls, /policies, /vendors."""

    def __init__(self):
        self.controls: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.vendors: dict[str, dict] = {}
        self.calls: list[str] = []
        self.bad_token = False
        self.rate_limited = False
        self.quota_exhausted = False
        self.server_error = False

    def add_control(self, c_id: str, name: str, description: str = "", category: str = "Access Control", frameworks=None):
        ctrl = {
            "id": c_id,
            "name": name,
            "description": description,
            "category": category,
            "frameworks": frameworks or ["SOC 2", "ISO 27001"],
            "status": "active",
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.controls[c_id] = ctrl
        return ctrl

    def add_policy(self, p_id: str, title: str, description: str = "", category: str = "Information Security"):
        policy = {
            "id": p_id,
            "title": title,
            "description": description,
            "category": category,
            "status": "approved",
            "renewal_date": "2027-01-01",
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.policies[p_id] = policy
        return policy

    def add_vendor(self, v_id: str, name: str, risk_level: str = "Low", description: str = ""):
        vendor = {
            "id": v_id,
            "name": name,
            "risk_level": risk_level,
            "service_category": "Cloud Infrastructure",
            "review_status": "Approved",
            "security_notes": description,
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.vendors[v_id] = vendor
        return vendor

    def get(self, path: str, params=None):
        self.calls.append(path)
        if self.bad_token:
            raise VantaAuthError("Vanta rejected the API token (HTTP 401)")
        if self.rate_limited:
            raise VantaRateLimitedError("Vanta rate limit exceeded (HTTP 429)")
        if self.quota_exhausted:
            raise VantaQuotaError("Vanta quota exhausted")
        if self.server_error:
            raise VantaAPIError("Vanta server error: HTTP 500", status=500)

        clean = path.strip("/")
        parts = clean.split("/")

        if len(parts) == 1:
            collection = parts[0]
            if collection == "controls":
                return list(self.controls.values())
            if collection == "policies":
                return list(self.policies.values())
            if collection == "vendors":
                return list(self.vendors.values())
            raise VantaAPIError(f"Endpoint not found: {path}", status=404)

        if len(parts) == 2:
            collection, item_id = parts
            if collection == "controls" and item_id in self.controls:
                return dict(self.controls[item_id])
            if collection == "policies" and item_id in self.policies:
                return dict(self.policies[item_id])
            if collection == "vendors" and item_id in self.vendors:
                return dict(self.vendors[item_id])
            raise VantaAPIError(f"Item not found: {path}", status=404)

        raise VantaAPIError(f"Unknown path: {path}", status=404)


@pytest.fixture
def vanta():
    return FakeVanta()
