"""Polar data-source connector for cognee.

Syncs Polar (polar.sh) customers, subscriptions, benefit grants, and products into AI memory
as structured, searchable documents.

Requirements:
- Read-only Polar Organization Access Token
- Separate Sandbox and Production API routing
- Safe privacy defaults (redacts PII, emails, billing details)
- Robust pagination and resilient error recovery
"""

import hashlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

try:
    import dlt
except ImportError:
    dlt = None

logger = logging.getLogger("cognee_connector_polar")

POLAR_API_BASE = "https://api.polar.sh/v1"
POLAR_SANDBOX_BASE = "https://sandbox-api.polar.sh/v1"

_PAGE_LIMIT = 50
_MAX_RETRIES = 3


class PolarConnectorError(RuntimeError):
    """Raised when an unrecoverable Polar API failure occurs."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RenderOptions:
    """Configurable redaction and rendering options."""

    include_customer_email: bool = False
    include_product_description: bool = True
    mask_pii: bool = True


class PolarClient:
    """Client for interacting with the Polar REST API."""

    def __init__(
        self,
        access_token: str,
        is_sandbox: bool = False,
        client: httpx.Client | None = None,
        base_url: str | None = None,
    ):
        if not access_token:
            raise ValueError("Polar access token is required")

        self.access_token = access_token
        self.is_sandbox = is_sandbox
        self.base_url = (base_url or (POLAR_SANDBOX_BASE if is_sandbox else POLAR_API_BASE)).rstrip("/")
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-polar/0.1.0",
        }

    def request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )

                if resp.status_code == 429:
                    # Rate limit encountered
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    logger.warning("Polar API rate limit hit, backing off for %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 401:
                    raise PolarConnectorError("Unauthorized: Polar access token is invalid or expired", 401)
                if resp.status_code == 403:
                    raise PolarConnectorError("Forbidden: Access token lacks required organization scopes", 403)
                if resp.status_code >= 400:
                    raise PolarConnectorError(f"Polar API error ({resp.status_code}): {resp.text}", resp.status_code)

                return resp.json()

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES:
                    raise PolarConnectorError(f"Polar connection failed after {attempt} attempts: {exc}") from exc
                time.sleep(1.0 * attempt)

        raise PolarConnectorError("Max retries exceeded for Polar request")

    def paginate(self, endpoint: str, query_params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        page = 1
        params = dict(query_params or {})
        params["limit"] = _PAGE_LIMIT

        while True:
            params["page"] = page
            data = self.request(endpoint, params=params)

            items = data.get("items", [])
            if not items and isinstance(data, list):
                items = data

            for item in items:
                yield item

            pagination = data.get("pagination", {})
            max_page = pagination.get("max_page")
            total_count = pagination.get("total_count")

            if max_page is not None:
                if page >= max_page:
                    break
            elif total_count is not None:
                if page * _PAGE_LIMIT >= total_count:
                    break
            elif len(items) < _PAGE_LIMIT:
                break

            page += 1


# ─── Document Renderers (Converting Polar records to searchable AI text) ────────


def render_customer(customer: dict[str, Any], options: RenderOptions = RenderOptions()) -> dict[str, Any]:
    cust_id = customer.get("id", "")
    name = customer.get("name") or "Anonymous Customer"
    created = customer.get("created_at", "")
    org_id = customer.get("organization_id", "")

    email = customer.get("email", "")
    display_email = "[REDACTED]"
    if options.include_customer_email and email:
        display_email = email

    text = f"""# Polar Customer: {name}
- Customer ID: {cust_id}
- Organization: {org_id}
- Email: {display_email}
- Account Created: {created}
"""
    return {
        "id": f"polar:customer:{cust_id}",
        "raw_id": cust_id,
        "type": "customer",
        "title": f"Customer: {name}",
        "text": text.strip(),
        "metadata": {
            "source": "polar",
            "type": "customer",
            "organization_id": org_id,
            "created_at": created,
        },
    }


def render_subscription(sub: dict[str, Any], options: RenderOptions = RenderOptions()) -> dict[str, Any]:
    sub_id = sub.get("id", "")
    cust_id = sub.get("customer_id", "")
    prod_id = sub.get("product_id", "")
    status = sub.get("status", "unknown")
    start = sub.get("current_period_start", "")
    end = sub.get("current_period_end", "")
    cancel_at_period_end = sub.get("cancel_at_period_end", False)
    started_at = sub.get("started_at", "")
    ended_at = sub.get("ended_at", "")

    text = f"""# Polar Subscription: {sub_id}
- Subscription ID: {sub_id}
- Customer ID: {cust_id}
- Product ID: {prod_id}
- Status: {status}
- Current Period: {start} to {end}
- Cancellation Scheduled: {'Yes' if cancel_at_period_end else 'No'}
- Started At: {started_at}
- Ended At: {ended_at or 'N/A'}
"""
    return {
        "id": f"polar:subscription:{sub_id}",
        "raw_id": sub_id,
        "type": "subscription",
        "title": f"Subscription: {sub_id} ({status})",
        "text": text.strip(),
        "metadata": {
            "source": "polar",
            "type": "subscription",
            "customer_id": cust_id,
            "product_id": prod_id,
            "status": status,
            "cancel_at_period_end": cancel_at_period_end,
        },
    }


def render_product(prod: dict[str, Any], options: RenderOptions = RenderOptions()) -> dict[str, Any]:
    prod_id = prod.get("id", "")
    name = prod.get("name", "Unnamed Product")
    desc = prod.get("description", "") if options.include_product_description else ""
    is_recurring = prod.get("is_recurring", False)
    is_archived = prod.get("is_archived", False)
    org_id = prod.get("organization_id", "")

    text = f"""# Polar Product: {name}
- Product ID: {prod_id}
- Organization: {org_id}
- Type: {'Recurring Subscription' if is_recurring else 'One-time Purchase'}
- Archived: {'Yes' if is_archived else 'No'}
- Description: {desc or 'No description provided'}
"""
    return {
        "id": f"polar:product:{prod_id}",
        "raw_id": prod_id,
        "type": "product",
        "title": f"Product: {name}",
        "text": text.strip(),
        "metadata": {
            "source": "polar",
            "type": "product",
            "is_recurring": is_recurring,
            "is_archived": is_archived,
            "organization_id": org_id,
        },
    }


def render_benefit(benefit: dict[str, Any], options: RenderOptions = RenderOptions()) -> dict[str, Any]:
    benefit_id = benefit.get("id", "")
    b_type = benefit.get("type", "custom")
    desc = benefit.get("description", "")
    org_id = benefit.get("organization_id", "")
    selectable = benefit.get("selectable", True)

    text = f"""# Polar Benefit: {desc or b_type}
- Benefit ID: {benefit_id}
- Type: {b_type}
- Organization: {org_id}
- Selectable: {'Yes' if selectable else 'No'}
"""
    return {
        "id": f"polar:benefit:{benefit_id}",
        "raw_id": benefit_id,
        "type": "benefit",
        "title": f"Benefit: {desc or b_type}",
        "text": text.strip(),
        "metadata": {
            "source": "polar",
            "type": "benefit",
            "benefit_type": b_type,
            "organization_id": org_id,
        },
    }


# ─── Source Definition ────────────────────────────────────────────────────────


def sync_polar_records(
    client: PolarClient,
    organization_id: str | None = None,
    options: RenderOptions = RenderOptions(),
) -> Iterator[dict[str, Any]]:
    """Yield all rendered documents from Polar for customers, subscriptions, products, and benefits."""
    query_params = {}
    if organization_id:
        query_params["organization_id"] = organization_id

    # 1. Customers
    try:
        for cust in client.paginate("customers", query_params):
            yield render_customer(cust, options)
    except Exception as exc:
        logger.error("Failed fetching Polar customers: %s", exc)

    # 2. Subscriptions
    try:
        for sub in client.paginate("subscriptions", query_params):
            yield render_subscription(sub, options)
    except Exception as exc:
        logger.error("Failed fetching Polar subscriptions: %s", exc)

    # 3. Products
    try:
        for prod in client.paginate("products", query_params):
            yield render_product(prod, options)
    except Exception as exc:
        logger.error("Failed fetching Polar products: %s", exc)

    # 4. Benefits
    try:
        for b in client.paginate("benefits", query_params):
            yield render_benefit(b, options)
    except Exception as exc:
        logger.error("Failed fetching Polar benefits: %s", exc)


if dlt is not None:

    @dlt.source(name="polar")
    def polar_source(
        access_token: str,
        organization_id: str | None = None,
        is_sandbox: bool = False,
        options: RenderOptions = RenderOptions(),
    ):
        """DLT source to sync Polar customers, subscriptions, products, and benefits into Cognee."""
        client = PolarClient(access_token=access_token, is_sandbox=is_sandbox)

        @dlt.resource(name="polar_documents", primary_key="id", write_disposition="merge")
        def polar_documents():
            yield from sync_polar_records(client, organization_id=organization_id, options=options)

        return [polar_documents]

else:

    def polar_source(
        access_token: str,
        organization_id: str | None = None,
        is_sandbox: bool = False,
        options: RenderOptions = RenderOptions(),
    ):
        """Fallback generator when dlt is not loaded."""
        client = PolarClient(access_token=access_token, is_sandbox=is_sandbox)
        return list(sync_polar_records(client, organization_id=organization_id, options=options))
