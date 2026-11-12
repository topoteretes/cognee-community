"""Gumroad creator storefront data-source connector for cognee.

Extracts digital product catalogs, pricing, variants, descriptions, and tags
from Gumroad into cognee knowledge graphs for creator copilots and customer AI agents.

Usage::

    import cognee
    from cognee_community_connector_gumroad import gumroad_source

    await cognee.remember(
        gumroad_source(
            access_token="...",
        ),
        dataset_name="creator_storefront",
    )
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)

GUMROAD_API_BASE = "https://api.gumroad.com/v2"


class GumroadAPIError(Exception):
    """Raised when the Gumroad API returns an error or rate limit."""

    pass


def _strip_html(html_text: str | None) -> str:
    """Removes HTML markup and normalizes whitespace."""
    if not html_text:
        return ""
    text = re.sub(r"<(?:p|div|br|li|h[1-6])[^>]*>", " ", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())


class GumroadClient:
    """HTTP client for querying Gumroad REST API endpoints."""

    def __init__(
        self,
        access_token: str | None = None,
        base_url: str = GUMROAD_API_BASE,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-gumroad/0.1.0",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query_string = ""
        if params:
            query_string = "?" + urllib.parse.urlencode(params)
        url = f"{self.base_url}{endpoint}{query_string}"

        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, headers=self._get_headers())
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    if response.status == 200:
                        raw = response.read().decode("utf-8")
                        payload = json.loads(raw)
                        if not payload.get("success", False):
                            msg = payload.get("message", "Unknown Gumroad API error")
                            raise GumroadAPIError(f"Gumroad API returned unsuccessful: {msg}")
                        return payload
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    sleep_time = (2 ** attempt) * 1.5
                    logger.warning(
                        "Gumroad rate limit hit (HTTP 429). "
                        "Backing off for %.1fs (attempt %d/%d)",
                        sleep_time,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(sleep_time)
                    continue
                else:
                    body = e.read().decode("utf-8", errors="replace")
                    raise GumroadAPIError(f"Gumroad API returned HTTP {e.code}: {body}") from e
            except Exception as e:
                last_error = e
                sleep_time = (2 ** attempt) * 1.0
                time.sleep(sleep_time)

        raise GumroadAPIError(
            f"Failed to fetch {url} after {self.max_retries} retries: {last_error}"
        )

    def ping(self) -> bool:
        """Pings the /user endpoint to verify token validity."""
        try:
            res = self.get("/user")
            return res.get("success", False) is True
        except Exception:
            return False

    def get_products(self) -> list[dict[str, Any]]:
        """Retrieves published products from the seller's catalog."""
        res = self.get("/products")
        return res.get("products", [])


def fetch_gumroad_products(
    client: GumroadClient | None = None,
    access_token: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Generator yielding normalized product records for Cognee memory graphs."""
    if client is None:
        client = GumroadClient(access_token=access_token)

    products = client.get_products()
    now_ts = int(time.time())

    for prod in products:
        prod_id = str(prod.get("id", ""))
        name = str(prod.get("name", "Untitled Product"))
        raw_desc = prod.get("description", "")
        clean_desc = _strip_html(raw_desc)
        formatted_price = str(prod.get("formatted_price", ""))
        currency = str(prod.get("currency", "usd")).lower()
        cents = prod.get("price", 0)
        decimal_price = float(cents) / 100.0 if isinstance(cents, (int, float)) else 0.0
        short_url = str(prod.get("short_url") or prod.get("url") or "")
        tags = prod.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        published = bool(prod.get("published", True))

        variants_data = prod.get("variants", [])
        variant_names = []
        if isinstance(variants_data, list):
            for v in variants_data:
                if isinstance(v, dict) and "title" in v:
                    variant_names.append(v["title"])

        # Format semantic text representation for LLM and Cognee Knowledge Graph
        summary_lines = [
            f"Product: {name}",
            f"Price: {formatted_price or f'${decimal_price:.2f} {currency.upper()}'}",
            f"Store URL: {short_url}",
            f"Published: {'Yes' if published else 'No'}",
        ]
        if tags:
            summary_lines.append(f"Tags: {', '.join(str(t) for t in tags)}")
        if variant_names:
            summary_lines.append(f"Available Tiers/Variants: {', '.join(variant_names)}")
        if clean_desc:
            summary_lines.append(f"Description:\n{clean_desc}")

        summary_text = "\n".join(summary_lines)

        yield {
            "id": f"gumroad:product:{prod_id}",
            "product_id": prod_id,
            "name": name,
            "description": clean_desc,
            "price_cents": cents,
            "price_decimal": decimal_price,
            "formatted_price": formatted_price,
            "currency": currency,
            "url": short_url,
            "tags": tags,
            "published": published,
            "timestamp": now_ts,
            "text": summary_text,
        }


def gumroad_source(
    access_token: str | None = None,
    client: GumroadClient | None = None,
):
    """Factory creating a Cognee/dlt source yielding Gumroad product documents."""
    import dlt

    if client is None:
        client = GumroadClient(access_token=access_token)

    @dlt.source(name="gumroad")
    def _source():
        @dlt.resource(
            name="gumroad_products",
            primary_key="id",
            write_disposition="merge",
        )
        def _resource():
            yield from fetch_gumroad_products(client=client)

        return _resource()

    return _source()
