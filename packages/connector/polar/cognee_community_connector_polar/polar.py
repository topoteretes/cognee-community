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

PROD_BASE_URL = "https://api.polar.sh/v1"
SANDBOX_BASE_URL = "https://sandbox-api.polar.sh/v1"
PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


class PolarClient:
    """HTTP client for Polar.sh REST API."""

    def __init__(
        self,
        access_token: str | None = None,
        organization_id: str | None = None,
        sandbox: bool = False,
    ):
        self.access_token = access_token or os.getenv("POLAR_ACCESS_TOKEN")
        self.organization_id = organization_id or os.getenv("POLAR_ORGANIZATION_ID")
        self.sandbox = sandbox or os.getenv("POLAR_SANDBOX", "").lower() in ("true", "1", "yes")

        if not self.access_token:
            raise ValueError("Polar access token is required. Pass access_token or set POLAR_ACCESS_TOKEN.")

        self.base_url = SANDBOX_BASE_URL if self.sandbox else PROD_BASE_URL
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-polar/0.1.0",
        }

    def _fetch_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_paginated(self, endpoint: str, params: dict | None = None) -> Iterator[dict[str, Any]]:
        """Yield items from paginated Polar endpoints with retries and rate limit safety."""
        query = {"page": 1, "limit": PAGE_SIZE}
        if self.organization_id:
            query["organization_id"] = self.organization_id
        if params:
            query.update(params)

        while True:
            encoded_query = urllib.parse.urlencode(query)
            full_url = f"{self.base_url}/{endpoint.lstrip('/')}?{encoded_query}"

            data = None
            for attempt in range(DEFAULT_RETRIES):
                try:
                    data = self._fetch_json(full_url)
                    break
                except Exception as e:
                    if attempt == DEFAULT_RETRIES - 1:
                        logger.error("Failed fetching Polar endpoint %s after %d retries: %s", endpoint, DEFAULT_RETRIES, e)
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
    sandbox: bool = False,
    resources: tuple[str, ...] = ("customers", "subscriptions", "products", "benefit_grants"),
):
    """
    DLT source returning resources for Polar customers, subscriptions, products, and benefit grants.
    """
    try:
        import dlt
    except ImportError:
        dlt = None

    client = PolarClient(
        access_token=access_token,
        organization_id=organization_id,
        sandbox=sandbox,
    )

    def customers() -> Iterator[dict[str, Any]]:
        for c in client.get_paginated("customers"):
            # Exclude sensitive billing addresses and payment specifics by selecting safe context fields
            yield {
                "id": f"polar:customer:{c.get('id')}",
                "customer_id": c.get("id"),
                "email": c.get("email"),
                "name": c.get("name"),
                "created_at": c.get("created_at"),
                "organization_id": c.get("organization_id"),
                "text": f"Polar Customer: {c.get('name') or c.get('email')} ({c.get('email')})",
            }

    def subscriptions() -> Iterator[dict[str, Any]]:
        for s in client.get_paginated("subscriptions"):
            yield {
                "id": f"polar:subscription:{s.get('id')}",
                "subscription_id": s.get("id"),
                "status": s.get("status"),
                "user_id": s.get("user_id"),
                "customer_id": s.get("customer_id"),
                "product_id": s.get("product_id"),
                "price_id": s.get("price_id"),
                "cancel_at_period_end": s.get("cancel_at_period_end", False),
                "current_period_start": s.get("current_period_start"),
                "current_period_end": s.get("current_period_end"),
                "text": f"Polar Subscription {s.get('id')} — Status: {s.get('status')} for Product {s.get('product_id')}",
            }

    def products() -> Iterator[dict[str, Any]]:
        for p in client.get_paginated("products"):
            yield {
                "id": f"polar:product:{p.get('id')}",
                "product_id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description"),
                "is_archived": p.get("is_archived", False),
                "is_recurring": p.get("is_recurring", False),
                "text": f"Polar Product: {p.get('name')}\nDescription: {p.get('description')}",
            }

    def benefit_grants() -> Iterator[dict[str, Any]]:
        for b in client.get_paginated("customer-benefit-grants"):
            # Exclude secret keys or sensitive tokens from benefit properties
            yield {
                "id": f"polar:benefit_grant:{b.get('id')}",
                "benefit_grant_id": b.get("id"),
                "customer_id": b.get("customer_id"),
                "benefit_id": b.get("benefit_id"),
                "is_granted": b.get("is_granted", True),
                "granted_at": b.get("granted_at"),
                "revoked_at": b.get("revoked_at"),
                "text": f"Polar Benefit Grant {b.get('id')} — Granted: {b.get('is_granted', True)} for Customer {b.get('customer_id')}",
            }

    if dlt is not None:
        cust_res = dlt.resource(name="polar_customers", primary_key="id", write_disposition="merge")(customers)
        sub_res = dlt.resource(name="polar_subscriptions", primary_key="id", write_disposition="merge")(subscriptions)
        prod_res = dlt.resource(name="polar_products", primary_key="id", write_disposition="merge")(products)
        grant_res = dlt.resource(name="polar_benefit_grants", primary_key="id", write_disposition="merge")(benefit_grants)

        @dlt.source(name="polar")
        def _source():
            selected = []
            if "customers" in resources:
                selected.append(cust_res)
            if "subscriptions" in resources:
                selected.append(sub_res)
            if "products" in resources:
                selected.append(prod_res)
            if "benefit_grants" in resources:
                selected.append(grant_res)
            return selected

        return _source()
    else:
        result = {}
        if "customers" in resources:
            result["customers"] = list(customers())
        if "subscriptions" in resources:
            result["subscriptions"] = list(subscriptions())
        if "products" in resources:
            result["products"] = list(products())
        if "benefit_grants" in resources:
            result["benefit_grants"] = list(benefit_grants())
        return result
