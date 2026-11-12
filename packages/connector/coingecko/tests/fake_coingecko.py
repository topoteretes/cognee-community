"""In-memory mock for CoinGecko API tests without network dependencies."""

from typing import Any


class FakeCoinGeckoClient:
    """Mock client returning deterministic CoinGecko payloads."""

    def __init__(self, fail_with_rate_limit: bool = False) -> None:
        self.fail_with_rate_limit = fail_with_rate_limit
        self.calls: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def get_simple_price(
        self,
        coin_ids: list[str],
        vs_currencies: list[str],
        include_market_cap: bool = True,
        include_24hr_vol: bool = True,
        include_24hr_change: bool = True,
    ) -> dict[str, Any]:
        self.calls.append({
            "endpoint": "simple_price",
            "coin_ids": coin_ids,
            "vs_currencies": vs_currencies,
        })
        results: dict[str, Any] = {}
        mock_table = {
            "bitcoin": {
                "usd": 92500.0,
                "usd_market_cap": 1820000000000.0,
                "usd_24h_vol": 35000000000.0,
                "usd_24h_change": 2.45,
            },
            "ethereum": {
                "usd": 3400.0,
                "usd_market_cap": 410000000000.0,
                "usd_24h_vol": 18000000000.0,
                "usd_24h_change": -0.85,
            },
            "solana": {
                "usd": 210.0,
                "usd_market_cap": 98000000000.0,
                "usd_24h_vol": 6500000000.0,
                "usd_24h_change": 4.12,
            },
        }
        for cid in coin_ids:
            if cid in mock_table:
                results[cid] = mock_table[cid]
            else:
                results[cid] = {
                    "usd": 1.0,
                    "usd_market_cap": 1000000.0,
                    "usd_24h_vol": 50000.0,
                    "usd_24h_change": 0.0,
                }
        return results

    def get_coin_detail(self, coin_id: str) -> dict[str, Any]:
        self.calls.append({"endpoint": "coin_detail", "coin_id": coin_id})
        return {
            "id": coin_id,
            "name": coin_id.capitalize(),
            "symbol": coin_id[:3].upper(),
            "description": {
                "en": f"{coin_id.capitalize()} is a decentralized blockchain network asset."
            },
        }
