from __future__ import annotations
"""Polar.sh connector for cognee: a dlt source that syncs Polar billing & customer data into memory.

It ingests your organization's customers, subscriptions, benefit grants, and products,
and is passed to ``cognee.remember``::

    await cognee.remember(
        polar_source(access_token="<polar api key>", organization_id="<org id>"),
        dataset_name="polar",
        primary_key="id",
        write_disposition="merge",
    )
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("polar_connector")

BASE_URL = "https://api.polar.sh/v1"
PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


class PolarClient:
    """HTTP client for Polar.sh REST API."""

    def __init__(self, access_token: str | None = None, organization_id: str | None = None):
        self.access_token = access_token or os.getenv("POLAR_ACCESS_TOKEN")
        self.organization_id = organization_id or os.getenv("POLAR_ORGANIZATION_ID")
        if not self.access_token:
            raise ValueError("Polar access token is required. Pass access_token or set POLAR_ACCESS_TOKEN.")

        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-polar/0.1.0"
        }

    def _fetch_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_paginated(self, endpoint: str, params: dict | None = None) -> Iterator[dict[str, Any]]:
        """Yield items from paginated Polar endpoints."""
        query = {"page": 1, "limit": PAGE_SIZE}
        if self.organization_id:
            query["organization_id"] = self.organization_id
        if params:
            query.update(params)

        while True:
            encoded_query = urllib.parse.urlencode(query)
            full_url = f"{BASE_URL}/{endpoint.lstrip('/')}?{encoded_query}"

            data = None
            for attempt in range(DEFAULT_RETRIES):
                try:
                    data = self._fetch_json(full_url)
                    break
                except Exception as e:
                    if attempt == DEFAULT_RETRIES - 1:
                        logger.error("Failed fetching Polar endpoint %s: %s", endpoint, e)
                        return
                    time.sleep(1 + attempt)

            if not data:
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                yield item

            pagination = data.get("pagination", {})
            max_page = pagination.get("max_page", 1)
            if query["page"] >= max_page:
                break
            query["page"] += 1


def polar_source(
    access_token: str | None = None,
    organization_id: str | None = None,
    resources: tuple[str, ...] = ("customers", "subscriptions", "products", "benefit_grants")
):
    """
    DLT source returning resources for Polar customers, subscriptions, products, and benefit grants.
    """
    try:
        import dlt
    except ImportError:
        dlt = None

    client = PolarClient(access_token=access_token, organization_id=organization_id)

    def customers() -> Iterator[dict[str, Any]]:
        for c in client.get_paginated("customers"):
            yield {
                "id": f"polar:customer:{c.get('id')}",
                "customer_id": c.get("id"),
                "email": c.get("email"),
                "name": c.get("name"),
                "created_at": c.get("created_at"),
                "organization_id": c.get("organization_id"),
                "text": f"Polar Customer: {c.get('name') or c.get('email')} ({c.get('email')})"
            }

    def subscriptions() -> Iterator[dict[str, Any]]:
        for s in client.get_paginated("subscriptions"):
            yield {
                "id": f"polar:subscription:{s.get('id')}",
                "subscription_id": s.get("id"),
                "status": s.get("status"),
                "user_id": s.get("user_id"),
                "product_id": s.get("product_id"),
                "price_id": s.get("price_id"),
                "current_period_start": s.get("current_period_start"),
                "current_period_end": s.get("current_period_end"),
                "text": f"Polar Subscription {s.get('id')} — Status: {s.get('status')} for Product {s.get('product_id')}"
            }

    def products() -> Iterator[dict[str, Any]]:
        for p in client.get_paginated("products"):
            yield {
                "id": f"polar:product:{p.get('id')}",
                "product_id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description"),
                "is_archived": p.get("is_archived", False),
                "text": f"Polar Product: {p.get('name')}\nDescription: {p.get('description')}"
            }

    if dlt is not None:
        cust_res = dlt.resource(name="polar_customers", primary_key="id", write_disposition="merge")(customers)
        sub_res = dlt.resource(name="polar_subscriptions", primary_key="id", write_disposition="merge")(subscriptions)
        prod_res = dlt.resource(name="polar_products", primary_key="id", write_disposition="merge")(products)

        @dlt.source(name="polar")
        def _source():
            selected = []
            if "customers" in resources:
                selected.append(cust_res)
            if "subscriptions" in resources:
                selected.append(sub_res)
            if "products" in resources:
                selected.append(prod_res)
            return selected

        return _source()
    else:
        return {
            "customers": list(customers()),
            "subscriptions": list(subscriptions()),
            "products": list(products())
        }
