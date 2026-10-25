"""BigCommerce e-commerce store catalog, inventory, and orders connector for Cognee.

Ingests products, pricing, stock levels, variants, and order fulfillment states
from BigCommerce stores into Cognee memory for automated retail and customer service AI agents.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Generator, List, Optional
import dlt
import httpx
from bs4 import BeautifulSoup

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)


def _html_to_clean_text(html_str: Optional[str]) -> str:
    """Strip HTML markup from BigCommerce product description into clean text."""
    if not html_str:
        return ""
    soup = BeautifulSoup(html_str, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+([.,!?;:])", r"\1", text)


class BigCommerceClient:
    """Client for querying the BigCommerce REST API."""

    def __init__(
        self,
        store_hash: str,
        access_token: str,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.store_hash = store_hash
        self.access_token = access_token
        self.base_url = f"https://api.bigcommerce.com/stores/{store_hash}"
        headers = {
            "X-Auth-Token": access_token,
            "User-Agent": "CogneeCommunityBigCommerceConnector/0.1.0",
            "Accept": "application/json",
        }
        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers=headers,
        )

    def get_products(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve products catalog from BigCommerce v3 API."""
        url = f"{self.base_url}/v3/catalog/products"
        params = {"limit": limit, "include": "variants"}
        try:
            resp = self._http_client.get(url, params=params)
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as exc:
            logger.error("Failed to fetch BigCommerce products for store %s: %s", self.store_hash, exc)
            raise

    def get_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent orders from BigCommerce v2/v3 API."""
        url = f"{self.base_url}/v2/orders"
        params = {"limit": limit}
        try:
            resp = self._http_client.get(url, params=params)
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.warning("Failed to fetch BigCommerce orders: %s", exc)
            return []


def _product_to_document(product: Dict[str, Any], store_hash: str) -> Optional[Dict[str, Any]]:
    """Convert a BigCommerce catalog product into a structured Markdown document."""
    prod_id = product.get("id")
    name = product.get("name")
    if not prod_id or not name:
        return None

    sku = product.get("sku") or f"PROD-{prod_id}"
    price = float(product.get("price", 0.0))
    inventory = product.get("inventory_level", 0)
    availability = product.get("availability", "available")
    raw_desc = product.get("description", "")
    clean_desc = _html_to_clean_text(raw_desc)
    custom_url = product.get("custom_url", {}).get("url") or f"/products/{prod_id}"

    # Extract variants
    variants = product.get("variants", [])
    variant_lines = []
    for var in variants:
        v_sku = var.get("sku") or "N/A"
        v_price = var.get("price") or price
        v_inv = var.get("inventory_level", 0)
        variant_lines.append(f"- **SKU {v_sku}:** ${float(v_price):.2f} (Stock: {v_inv})")

    content = f"""# Product: {name} (SKU: {sku})
**Product ID:** `{prod_id}`
**SKU:** `{sku}`
**Price:** ${price:.2f}
**Stock Level:** {inventory} units ({availability})
**Store Link:** [{name}]({custom_url})

## Product Description:
{clean_desc if clean_desc else '*(No product description recorded)*'}

## Available Variants & Stock:
{chr(10).join(variant_lines) if variant_lines else '- Single default variant only.'}

---
*Ingested from BigCommerce Store `{store_hash}` via Cognee community connector.*"""

    return {
        "id": f"bc_prod_{prod_id}",
        "url": custom_url,
        "title": f"Product: {name} (${price:.2f})",
        "content": content,
    }


def _order_to_document(order: Dict[str, Any], store_hash: str) -> Optional[Dict[str, Any]]:
    """Convert a BigCommerce order into a structured Markdown document."""
    order_id = order.get("id")
    if not order_id:
        return None

    status = order.get("status", "Unknown")
    total = float(order.get("total_inc_tax", 0.0))
    items_total = order.get("items_total", 0)
    date_created = order.get("date_created", "Unknown date")
    payment_method = order.get("payment_method", "Not specified")

    content = f"""# BigCommerce Order #{order_id} ({status})
**Order ID:** `#{order_id}`
**Status:** **{status}**
**Order Total:** ${total:.2f} (Tax Included)
**Total Items:** {items_total}
**Date Created:** {date_created}
**Payment Method:** {payment_method}

---
*Ingested from BigCommerce Store `{store_hash}` via Cognee community connector.*"""

    return {
        "id": f"bc_order_{order_id}",
        "url": f"https://store-{store_hash}.mybigcommerce.com/manage/orders/{order_id}",
        "title": f"Order #{order_id} - {status} (${total:.2f})",
        "content": content,
    }


@dlt.source(name="bigcommerce_source")
def bigcommerce_source(
    store_hash: str,
    access_token: Optional[str] = None,
    include_orders: bool = True,
    limit: int = 50,
    client: Optional[BigCommerceClient] = None,
) -> Any:
    """DLT data-source for ingesting BigCommerce store catalog and orders into Cognee."""
    bc_client = client or BigCommerceClient(store_hash=store_hash, access_token=access_token or "")

    @dlt.resource(
        name="bigcommerce_store_data",
        write_disposition="replace",
        primary_key="id",
    )
    def bigcommerce_store_data() -> Generator[Dict[str, Any], None, None]:
        # 1. Ingest products
        products = bc_client.get_products(limit=limit)
        for prod in products:
            doc = _product_to_document(prod, store_hash=store_hash)
            if doc:
                yield doc

        # 2. Ingest orders if enabled
        if include_orders:
            orders = bc_client.get_orders(limit=limit)
            for ord_data in orders:
                ord_doc = _order_to_document(ord_data, store_hash=store_hash)
                if ord_doc:
                    yield ord_doc

    resource = bigcommerce_store_data()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "bigcommerce")
    return resource
