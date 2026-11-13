"""Hermetic unit tests for BigCommerce connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_bigcommerce.bigcommerce import (
    BigCommerceClient,
    _html_to_clean_text,
    _order_to_document,
    _product_to_document,
    bigcommerce_source,
)

SAMPLE_PRODUCT: Dict[str, Any] = {
    "id": 111,
    "name": "Ergonomic Mechanical Keyboard",
    "sku": "KB-MECH-PRO",
    "price": 149.99,
    "inventory_level": 42,
    "availability": "available",
    "description": "<p>Ultra-quiet <strong>mechanical switches</strong> with RGB backlighting.</p><p>Includes wrist rest.</p>",
    "custom_url": {"url": "/products/ergonomic-keyboard"},
    "variants": [
        {"sku": "KB-MECH-BLUE", "price": 149.99, "inventory_level": 20},
        {"sku": "KB-MECH-BROWN", "price": 159.99, "inventory_level": 22},
    ],
}

SAMPLE_ORDER: Dict[str, Any] = {
    "id": 1001,
    "status": "Shipped",
    "total_inc_tax": "164.99",
    "items_total": 1,
    "date_created": "Thu, 22 Oct 2026 19:51:36 +0000",
    "payment_method": "Credit Card",
}


class FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class FakeBigCommerceHttpClient:
    def __init__(self, products: List[Dict[str, Any]], orders: List[Dict[str, Any]]) -> None:
        self.products = products
        self.orders = orders

    def get(self, url: str, params: Any = None) -> FakeResponse:
        if "/catalog/products" in url:
            return FakeResponse({"data": self.products})
        if "/v2/orders" in url:
            return FakeResponse(self.orders)
        return FakeResponse({}, status_code=404)


def test_html_to_clean_text():
    """Test converting product HTML description to clean readable text."""
    html_sample = "<p>Premium <strong>Noise Cancelling</strong> Headphones, <em>Wireless</em>!</p>"
    clean = _html_to_clean_text(html_sample)
    assert clean == "Premium Noise Cancelling Headphones, Wireless!"


def test_product_to_document():
    """Test converting a BigCommerce product and its variants into a Markdown document."""
    doc = _product_to_document(SAMPLE_PRODUCT, store_hash="store-test123")
    assert doc is not None
    assert doc["id"] == "bc_prod_111"
    assert doc["title"] == "Product: Ergonomic Mechanical Keyboard ($149.99)"
    assert doc["url"] == "/products/ergonomic-keyboard"

    content = doc["content"]
    assert "# Product: Ergonomic Mechanical Keyboard (SKU: KB-MECH-PRO)" in content
    assert "**Price:** $149.99" in content
    assert "**Stock Level:** 42 units" in content
    assert "Ultra-quiet mechanical switches with RGB backlighting." in content
    assert "- **SKU KB-MECH-BLUE:** $149.99 (Stock: 20)" in content
    assert "- **SKU KB-MECH-BROWN:** $159.99 (Stock: 22)" in content


def test_order_to_document():
    """Test converting an order into a Markdown document."""
    doc = _order_to_document(SAMPLE_ORDER, store_hash="store-test123")
    assert doc is not None
    assert doc["id"] == "bc_order_1001"
    assert doc["title"] == "Order #1001 - Shipped ($164.99)"

    content = doc["content"]
    assert "# BigCommerce Order #1001 (Shipped)" in content
    assert "**Status:** **Shipped**" in content
    assert "**Order Total:** $164.99 (Tax Included)" in content


def test_bigcommerce_source_dlt_execution():
    """Test full DLT source execution with mocked BigCommerce client."""
    fake_client = FakeBigCommerceHttpClient(products=[SAMPLE_PRODUCT], orders=[SAMPLE_ORDER])
    client = BigCommerceClient(store_hash="store-test123", access_token="mock_token", http_client=fake_client)

    resource = bigcommerce_source(store_hash="store-test123", include_orders=True, client=client)
    items = list(resource)

    # 1 product + 1 order = 2 items
    assert len(items) == 2
    assert items[0]["id"] == "bc_prod_111"
    assert items[1]["id"] == "bc_order_1001"
