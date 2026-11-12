"""CoinGecko public market data-source connector for cognee.

Extracts real-time and historical cryptocurrency market capitalization, prices,
trading volumes, and asset descriptions into cognee memory graphs.

Usage::

    import cognee
    from cognee_community_connector_coingecko import coingecko_source

    await cognee.remember(
        coingecko_source(
            coin_ids=["bitcoin", "ethereum", "solana"],
            vs_currencies=["usd"],
            include_market_data=True,
        ),
        dataset_name="crypto_market",
        primary_key="id",
        write_disposition="merge",
    )
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)

COINGECKO_PUBLIC_API_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_PRO_API_BASE = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoAPIError(Exception):
    """Raised when the CoinGecko API returns an unexpected error or rate limit."""
    pass


class CoinGeckoClient:
    """HTTP client for querying the CoinGecko REST API with backoff."""

    def __init__(
        self,
        api_key: str | None = None,
        is_pro: bool = False,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.is_pro = is_pro
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_url = COINGECKO_PRO_API_BASE if is_pro else COINGECKO_PUBLIC_API_BASE

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-coingecko/0.1.0",
        }
        if self.api_key:
            header_key = "x-cg-pro-api-key" if self.is_pro else "x-cg-demo-api-key"
            headers[header_key] = self.api_key
        return headers

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
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
                        return json.loads(raw)
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    sleep_time = (2 ** attempt) * 1.5
                    logger.warning(
                        "CoinGecko rate limit hit (HTTP 429). "
                        "Backing off for %.1fs (attempt %d/%d)",
                        sleep_time,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(sleep_time)
                    continue
                else:
                    body = e.read().decode("utf-8", errors="replace")
                    raise CoinGeckoAPIError(f"CoinGecko API returned HTTP {e.code}: {body}") from e
            except Exception as e:
                last_error = e
                sleep_time = (2 ** attempt) * 1.0
                time.sleep(sleep_time)

        raise CoinGeckoAPIError(
            f"Failed to fetch {url} after {self.max_retries} retries: {last_error}"
        )

    def ping(self) -> bool:
        """Pings the API to check availability."""
        try:
            res = self.get("/ping")
            return res.get("gecko_says") is not None
        except Exception:
            return False

    def get_simple_price(
        self,
        coin_ids: list[str],
        vs_currencies: list[str],
        include_market_cap: bool = True,
        include_24hr_vol: bool = True,
        include_24hr_change: bool = True,
    ) -> dict[str, Any]:
        params = {
            "ids": ",".join(coin_ids),
            "vs_currencies": ",".join(vs_currencies),
            "include_market_cap": "true" if include_market_cap else "false",
            "include_24hr_vol": "true" if include_24hr_vol else "false",
            "include_24hr_change": "true" if include_24hr_change else "false",
        }
        return self.get("/simple/price", params)

    def get_coin_detail(self, coin_id: str) -> dict[str, Any]:
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
        }
        return self.get(f"/coins/{coin_id}", params)


def fetch_coingecko_market_data(
    coin_ids: list[str],
    vs_currencies: list[str] | None = None,
    client: CoinGeckoClient | None = None,
    include_details: bool = True,
) -> Generator[dict[str, Any], None, None]:
    """Generator yielding normalized market records for Cognee ingestion."""
    if vs_currencies is None:
        vs_currencies = ["usd"]
    if client is None:
        client = CoinGeckoClient()

    prices_data = client.get_simple_price(
        coin_ids=coin_ids,
        vs_currencies=vs_currencies,
        include_market_cap=True,
        include_24hr_vol=True,
        include_24hr_change=True,
    )

    now_ts = int(time.time())

    for coin_id in coin_ids:
        coin_prices = prices_data.get(coin_id, {})
        details = {}
        coin_name = coin_id.capitalize()
        symbol = coin_id.upper()
        description = ""

        if include_details:
            try:
                details = client.get_coin_detail(coin_id)
                coin_name = details.get("name", coin_name)
                symbol = details.get("symbol", symbol).upper()
                desc_obj = details.get("description", {})
                if isinstance(desc_obj, dict):
                    description = desc_obj.get("en", "")
                else:
                    description = str(desc_obj)
            except Exception as e:
                logger.warning("Could not fetch detailed metadata for %s: %s", coin_id, e)

        for vs in vs_currencies:
            vs_lower = vs.lower()
            price = coin_prices.get(vs_lower)
            market_cap = coin_prices.get(f"{vs_lower}_24h_change")  # fallback / direct
            market_cap = coin_prices.get(f"{vs_lower}_market_cap", 0.0)
            vol_24h = coin_prices.get(f"{vs_lower}_24h_vol", 0.0)
            change_24h = coin_prices.get(f"{vs_lower}_24h_change", 0.0)

            # Produce rich semantic representation for Knowledge Graph entity construction
            summary = (
                f"Asset: {coin_name} ({symbol})\n"
                f"Current Price ({vs.upper()}): {price}\n"
                f"24h Market Cap: {market_cap:,.2f} {vs.upper()}\n"
                f"24h Trading Volume: {vol_24h:,.2f} {vs.upper()}\n"
                f"24h Price Change: {change_24h:.2f}%\n"
            )
            if description:
                summary += f"Description: {description[:300]}..."

            yield {
                "id": f"coingecko:{coin_id}:{vs_lower}",
                "coin_id": coin_id,
                "name": coin_name,
                "symbol": symbol,
                "vs_currency": vs_lower,
                "current_price": price,
                "market_cap": market_cap,
                "total_volume_24h": vol_24h,
                "price_change_percentage_24h": change_24h,
                "timestamp": now_ts,
                "text": summary,
            }


def coingecko_source(
    coin_ids: list[str] | None = None,
    vs_currencies: list[str] | None = None,
    api_key: str | None = None,
    is_pro: bool = False,
    include_details: bool = True,
):
    """Factory creating a Cognee/dlt source yielding CoinGecko market records."""
    import dlt

    if coin_ids is None:
        coin_ids = ["bitcoin", "ethereum", "solana"]
    if vs_currencies is None:
        vs_currencies = ["usd"]

    client = CoinGeckoClient(api_key=api_key, is_pro=is_pro)

    @dlt.source(name="coingecko")
    def _source():
        @dlt.resource(
            name="coingecko_market_data",
            primary_key="id",
            write_disposition="merge",
        )
        def _resource():
            yield from fetch_coingecko_market_data(
                coin_ids=coin_ids,
                vs_currencies=vs_currencies,
                client=client,
                include_details=include_details,
            )

        return _resource()

    return _source()
