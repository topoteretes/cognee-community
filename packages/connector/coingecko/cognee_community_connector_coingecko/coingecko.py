import os
import hashlib
from typing import List, Dict, Any, Optional
import requests
from datetime import datetime

COINGECKO_PUBLIC_API_URL = "https://api.coingecko.com/api/v3"
COINGECKO_PRO_API_URL = "https://pro-api.coingecko.com/api/v3"

def fetch_coingecko_markets(
    vs_currency: str = "usd",
    ids: Optional[List[str]] = None,
    category: Optional[str] = None,
    order: str = "market_cap_desc",
    per_page: int = 100,
    page: int = 1,
    sparkline: bool = False,
    api_key: Optional[str] = None,
    is_pro: bool = False,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches cryptocurrency market data from CoinGecko API.
    Supports both free tier (no auth required) and Demo/Pro API keys.
    """
    http = session or requests.Session()
    base_url = COINGECKO_PRO_API_URL if is_pro else COINGECKO_PUBLIC_API_URL
    url = f"{base_url.rstrip('/')}/coins/markets"

    params: Dict[str, Any] = {
        "vs_currency": vs_currency,
        "order": order,
        "per_page": min(per_page, 250),
        "page": page,
        "sparkline": "true" if sparkline else "false"
    }
    if ids:
        params["ids"] = ",".join(ids)
    if category:
        params["category"] = category

    headers = {
        "Accept": "application/json",
        "User-Agent": "cognee-coingecko-connector/0.1.0"
    }
    if api_key:
        if is_pro:
            headers["x-cg-pro-api-key"] = api_key
        else:
            headers["x-cg-demo-api-key"] = api_key

    response = http.get(url, params=params, headers=headers, timeout=25)
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        return data
    return []

def format_coin_market_markdown(coin: Dict[str, Any]) -> str:
    """
    Renders CoinGecko market snapshot into structured Markdown for Cognee semantic extraction.
    """
    coin_id = coin.get("id", "unknown")
    name = coin.get("name", "Unknown Coin")
    symbol = coin.get("symbol", "").upper()
    current_price = coin.get("current_price")
    market_cap = coin.get("market_cap")
    market_cap_rank = coin.get("market_cap_rank", "N/A")
    total_volume = coin.get("total_volume")
    high_24h = coin.get("high_24h")
    low_24h = coin.get("low_24h")
    price_change_24h = coin.get("price_change_24h")
    price_change_percentage_24h = coin.get("price_change_percentage_24h")
    circulating_supply = coin.get("circulating_supply")
    total_supply = coin.get("total_supply")
    ath = coin.get("ath")
    last_updated = coin.get("last_updated", "")

    lines = [
        f"# Crypto Asset: {name} ({symbol})",
        "",
        f"**Asset ID:** {coin_id}",
        f"**Market Cap Rank:** #{market_cap_rank}",
        f"**Last Updated:** {last_updated}",
        "",
        "## Market Snapshot",
        f"- **Current Price:** ${current_price:,.4f}" if isinstance(current_price, (int, float)) else f"- **Current Price:** {current_price}",
        f"- **Market Capitalization:** ${market_cap:,.2f}" if isinstance(market_cap, (int, float)) else f"- **Market Capitalization:** {market_cap}",
        f"- **24h Trading Volume:** ${total_volume:,.2f}" if isinstance(total_volume, (int, float)) else f"- **24h Trading Volume:** {total_volume}",
        f"- **24h High:** ${high_24h:,.4f}" if isinstance(high_24h, (int, float)) else f"- **24h High:** {high_24h}",
        f"- **24h Low:** ${low_24h:,.4f}" if isinstance(low_24h, (int, float)) else f"- **24h Low:** {low_24h}",
        f"- **24h Price Change:** {price_change_percentage_24h}%" if price_change_percentage_24h is not None else "- **24h Price Change:** N/A",
        "",
        "## Supply & Economics",
        f"- **Circulating Supply:** {circulating_supply:,.2f} {symbol}" if isinstance(circulating_supply, (int, float)) else f"- **Circulating Supply:** {circulating_supply}",
        f"- **Total Supply:** {total_supply:,.2f} {symbol}" if isinstance(total_supply, (int, float)) else f"- **Total Supply:** {total_supply}",
        f"- **All-Time High (ATH):** ${ath:,.4f}" if isinstance(ath, (int, float)) else f"- **All-Time High (ATH):** {ath}"
    ]

    return "\n".join(lines)

def filter_incremental_snapshots(
    coins: List[Dict[str, Any]],
    last_updated_cursor: Optional[str]
) -> List[Dict[str, Any]]:
    """
    Filters market snapshots to only include records updated strictly after the cursor timestamp.
    Cursor format expected: ISO 8601 (e.g. '2026-10-26T10:00:00.000Z')
    """
    if not last_updated_cursor:
        return coins

    filtered = []
    for coin in coins:
        coin_time = coin.get("last_updated")
        if not coin_time:
            filtered.append(coin)
            continue
        try:
            # ISO timestamp string comparison works reliably for standard ISO formats
            if coin_time > last_updated_cursor:
                filtered.append(coin)
        except Exception:
            filtered.append(coin)

    return filtered

class CoinGeckoConnector:
    """
    Connector to ingest CoinGecko cryptocurrency market snapshots into Cognee.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        is_pro: bool = False,
        vs_currency: str = "usd"
    ):
        self.api_key = api_key
        self.is_pro = is_pro
        self.vs_currency = vs_currency

    def fetch_documents(
        self,
        ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
        cursor: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieves cryptocurrency market data and formats as Cognee documents.
        """
        raw_coins = fetch_coingecko_markets(
            vs_currency=self.vs_currency,
            ids=ids,
            category=category,
            per_page=per_page,
            page=page,
            api_key=self.api_key,
            is_pro=self.is_pro
        )

        fresh_coins = filter_incremental_snapshots(raw_coins, cursor)

        documents = []
        for coin in fresh_coins:
            coin_id = coin.get("id", "coin")
            last_updated = coin.get("last_updated", "")
            snapshot_key = f"{coin_id}_{last_updated}"
            doc_id = hashlib.sha256(snapshot_key.encode("utf-8")).hexdigest()

            documents.append({
                "id": doc_id,
                "text": format_coin_market_markdown(coin),
                "metadata": {
                    "source": "coingecko",
                    "coin_id": coin_id,
                    "symbol": coin.get("symbol", "").upper(),
                    "name": coin.get("name", ""),
                    "market_cap_rank": coin.get("market_cap_rank"),
                    "last_updated": last_updated
                }
            })

        return documents
