"""Unit tests for the Polar data connector."""

import pytest
from fake_polar import FAKE_TOKEN, ORG_ID, create_fake_client

from cognee_community_connector_polar.polar import (
    PolarClient,
    PolarConnectorError,
    RenderOptions,
    render_benefit,
    render_customer,
    render_product,
    render_subscription,
    sync_polar_records,
)


def test_polar_client_auth_success():
    client = create_fake_client()
    data = client.request("customers")
    assert "items" in data
    assert len(data["items"]) == 2


def test_polar_client_unauthorized():
    client = create_fake_client(token="expired_token")
    with pytest.raises(PolarConnectorError) as exc_info:
        client.request("customers")
    assert exc_info.value.status_code == 401


def test_polar_client_forbidden_scope():
    client = create_fake_client(token="forbidden_token")
    with pytest.raises(PolarConnectorError) as exc_info:
        client.request("customers")
    assert exc_info.value.status_code == 403


def test_render_customer_masks_email_by_default():
    cust = {
        "id": "c_123",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "organization_id": "org_1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    rendered = render_customer(cust)
    assert rendered["id"] == "polar:customer:c_123"
    assert "Jane Doe" in rendered["text"]
    assert "[REDACTED]" in rendered["text"]
    assert "jane@example.com" not in rendered["text"]


def test_render_customer_allows_email_when_explicit():
    cust = {
        "id": "c_123",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "organization_id": "org_1",
    }
    opts = RenderOptions(include_customer_email=True)
    rendered = render_customer(cust, options=opts)
    assert "jane@example.com" in rendered["text"]


def test_render_subscription_cancellation_tracking():
    sub = {
        "id": "sub_xyz",
        "customer_id": "cust_1",
        "product_id": "prod_pro",
        "status": "canceled",
        "current_period_start": "2026-10-01T00:00:00Z",
        "current_period_end": "2026-11-01T00:00:00Z",
        "cancel_at_period_end": True,
        "started_at": "2026-10-01T00:00:00Z",
        "ended_at": "2026-10-25T12:00:00Z",
    }
    rendered = render_subscription(sub)
    assert rendered["id"] == "polar:subscription:sub_xyz"
    assert rendered["metadata"]["status"] == "canceled"
    assert rendered["metadata"]["cancel_at_period_end"] is True
    assert "Cancellation Scheduled: Yes" in rendered["text"]
    assert "Ended At: 2026-10-25T12:00:00Z" in rendered["text"]


def test_render_product():
    prod = {
        "id": "prod_1",
        "name": "Enterprise AI Memory",
        "description": "Full access to high-performance graph index",
        "is_recurring": True,
        "is_archived": False,
        "organization_id": "org_1",
    }
    rendered = render_product(prod)
    assert rendered["id"] == "polar:product:prod_1"
    assert rendered["metadata"]["is_recurring"] is True
    assert "Enterprise AI Memory" in rendered["text"]
    assert "Full access to high-performance graph index" in rendered["text"]


def test_render_benefit():
    benefit = {
        "id": "ben_1",
        "type": "github_repo",
        "description": "Private GitHub Repository Access",
        "organization_id": "org_1",
        "selectable": True,
    }
    rendered = render_benefit(benefit)
    assert rendered["id"] == "polar:benefit:ben_1"
    assert rendered["metadata"]["benefit_type"] == "github_repo"
    assert "Private GitHub Repository Access" in rendered["text"]


def test_sync_polar_records_end_to_end():
    client = create_fake_client()
    records = list(sync_polar_records(client, organization_id=ORG_ID))

    # 2 customers + 2 subscriptions + 2 products + 1 benefit = 7 records
    assert len(records) == 7

    types = {r["type"] for r in records}
    assert types == {"customer", "subscription", "product", "benefit"}

    ids = {r["id"] for r in records}
    assert "polar:customer:cust_001" in ids
    assert "polar:subscription:sub_101" in ids
    assert "polar:product:prod_pro" in ids
    assert "polar:benefit:ben_001" in ids


def test_sandbox_routing():
    client = PolarClient(access_token="test_tok", is_sandbox=True)
    assert client.base_url == "https://sandbox-api.polar.sh/v1"

    prod_client = PolarClient(access_token="test_tok", is_sandbox=False)
    assert prod_client.base_url == "https://api.polar.sh/v1"
