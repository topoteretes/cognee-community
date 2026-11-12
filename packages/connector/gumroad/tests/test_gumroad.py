"""Unit tests for the Gumroad connector.

All tests run hermetically and offline without network access.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_gumroad.gumroad import (
    GUMROAD_API_BASE,
    GumroadAPIError,
    GumroadClient,
    _strip_html,
    fetch_gumroad_products,
    gumroad_source,
)

try:
    from .fake_gumroad import FakeGumroadClient
except ImportError:
    from fake_gumroad import FakeGumroadClient


def test_strip_html():
    assert _strip_html("<p>Hello <b>World</b>!</p>") == "Hello World!"
    assert _strip_html(None) == ""
    assert _strip_html("   No HTML here   ") == "No HTML here"


def test_client_initialization_defaults():
    client = GumroadClient()
    assert client.base_url == GUMROAD_API_BASE
    assert client.access_token is None
    headers = client._get_headers()
    assert "Authorization" not in headers
    assert headers["Accept"] == "application/json"


def test_client_headers_with_token():
    client = GumroadClient(access_token="gumroad_secret_token_123")
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer gumroad_secret_token_123"


def test_client_ping_success():
    client = GumroadClient(access_token="valid_token")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({
        "success": True,
        "user": {"name": "Apex Creator", "email": "creator@example.com"}
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert client.ping() is True


def test_client_ping_failure():
    client = GumroadClient(access_token="invalid_token", max_retries=1)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Auth failed")):
        assert client.ping() is False


def test_client_get_products_success():
    client = GumroadClient(access_token="valid_token")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({
        "success": True,
        "products": [
            {"id": "prod_1", "name": "Cheat Sheet", "price": 500}
        ]
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        products = client.get_products()
        assert len(products) == 1
        assert products[0]["id"] == "prod_1"


def test_client_http_error_handling():
    client = GumroadClient(max_retries=1)
    http_error = urllib.error.HTTPError(
        url="https://api.gumroad.com/v2/products",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"success": false, "message": "Invalid access token"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(GumroadAPIError) as exc_info:
            client.get("/products")
        assert "HTTP 401" in str(exc_info.value)


def test_fetch_gumroad_products_schema():
    fake_client = FakeGumroadClient()
    records = list(fetch_gumroad_products(client=fake_client))

    assert len(records) == 2

    playbook = records[0]
    assert playbook["id"] == "gumroad:product:prod_ai_playbook_001"
    assert playbook["product_id"] == "prod_ai_playbook_001"
    assert playbook["name"] == "Full-Stack AI Agent Playbook"
    assert playbook["description"] == "A comprehensive guide to deploying autonomous agents."
    assert playbook["price_cents"] == 1900
    assert playbook["price_decimal"] == 19.0
    assert playbook["formatted_price"] == "$19"
    assert playbook["currency"] == "usd"
    assert playbook["url"] == "https://gumroad.com/l/playbook"
    assert "ai" in playbook["tags"]
    assert playbook["published"] is True
    assert isinstance(playbook["timestamp"], int)
    assert "Product: Full-Stack AI Agent Playbook" in playbook["text"]
    assert "Price: $19" in playbook["text"]
    assert "Available Tiers/Variants: Standard Edition, Enterprise Edition" in playbook["text"]

    resume = records[1]
    assert resume["id"] == "gumroad:product:prod_ats_resume_002"
    assert resume["price_decimal"] == 9.0
    assert "career" in resume["tags"]


def test_gumroad_source_resource_hints():
    fake_client = FakeGumroadClient()
    source = gumroad_source(client=fake_client)
    assert source.name == "gumroad"
    resources = list(source.resources.values())
    assert len(resources) == 1
    res = resources[0]
    assert res.name == "gumroad_products"
    assert res._hints.get("primary_key") == "id"
    assert res.write_disposition == "merge"
