"""In-memory mock Polar API server for testing the Polar connector."""

import json
from typing import Any
import httpx

FAKE_TOKEN = "polar_oat_test_token_123456"
ORG_ID = "org_polar_alpha"


class FakePolarTransport(httpx.BaseTransport):
    """Custom HTTP transport simulating Polar API responses."""

    def __init__(self):
        super().__init__()
        self.customers = [
            {
                "id": "cust_001",
                "name": "Alice Developer",
                "email": "alice@example.com",
                "organization_id": ORG_ID,
                "created_at": "2026-10-01T12:00:00Z",
            },
            {
                "id": "cust_002",
                "name": "Bob Founder",
                "email": "bob@example.com",
                "organization_id": ORG_ID,
                "created_at": "2026-10-15T15:30:00Z",
            },
        ]
        self.subscriptions = [
            {
                "id": "sub_101",
                "customer_id": "cust_001",
                "product_id": "prod_pro",
                "status": "active",
                "current_period_start": "2026-10-01T00:00:00Z",
                "current_period_end": "2026-11-01T00:00:00Z",
                "cancel_at_period_end": False,
                "started_at": "2026-10-01T00:00:00Z",
                "ended_at": None,
            },
            {
                "id": "sub_102",
                "customer_id": "cust_002",
                "product_id": "prod_starter",
                "status": "canceled",
                "current_period_start": "2026-10-15T00:00:00Z",
                "current_period_end": "2026-11-15T00:00:00Z",
                "cancel_at_period_end": True,
                "started_at": "2026-10-15T00:00:00Z",
                "ended_at": "2026-10-28T09:00:00Z",
            },
        ]
        self.products = [
            {
                "id": "prod_pro",
                "name": "Pro Tier Plan",
                "description": "Unlimited memory pipelines and priority indexing",
                "is_recurring": True,
                "is_archived": False,
                "organization_id": ORG_ID,
            },
            {
                "id": "prod_starter",
                "name": "Starter Plan",
                "description": "Basic vector search",
                "is_recurring": True,
                "is_archived": False,
                "organization_id": ORG_ID,
            },
        ]
        self.benefits = [
            {
                "id": "ben_001",
                "type": "discord",
                "description": "Private VIP Discord Support Channel",
                "organization_id": ORG_ID,
                "selectable": True,
            }
        ]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(401, json={"error": "Unauthorized"})

        token = auth.split("Bearer ")[1].strip()
        if token == "expired_token":
            return httpx.Response(401, json={"error": "Token expired"})
        if token == "forbidden_token":
            return httpx.Response(403, json={"error": "Forbidden: missing scope"})

        path = request.url.path
        if path.endswith("/customers"):
            return httpx.Response(200, json={"items": self.customers, "pagination": {"total_count": len(self.customers), "max_page": 1}})
        elif path.endswith("/subscriptions"):
            return httpx.Response(200, json={"items": self.subscriptions, "pagination": {"total_count": len(self.subscriptions), "max_page": 1}})
        elif path.endswith("/products"):
            return httpx.Response(200, json={"items": self.products, "pagination": {"total_count": len(self.products), "max_page": 1}})
        elif path.endswith("/benefits"):
            return httpx.Response(200, json={"items": self.benefits, "pagination": {"total_count": len(self.benefits), "max_page": 1}})
        else:
            return httpx.Response(404, json={"error": "Endpoint not found"})


def create_fake_client(token: str = FAKE_TOKEN, is_sandbox: bool = False):
    from cognee_community_connector_polar.polar import PolarClient

    transport = FakePolarTransport()
    client = httpx.Client(transport=transport)
    return PolarClient(access_token=token, is_sandbox=is_sandbox, client=client)
