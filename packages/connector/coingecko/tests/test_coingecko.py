"""Unit tests for the CoinGecko connector.

All tests run hermetically and offline using FakeCoinGeckoClient or mocked urllib requests.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_coingecko.coingecko import (
    COINGECKO_PRO_API_BASE,
    COINGECKO_PUBLIC_API_BASE,
    CoinGeckoAPIError,
    CoinGeckoClient,
    coingecko_source,
    fetch_coingecko_market_data,
)

try:
    from .fake_coingecko import FakeCoinGeckoClient
except ImportError:
    from fake_coingecko import FakeCoinGeckoClient


def test_client_initialization_defaults():
    client = CoinGeckoClient()
    assert client.base_url == COINGECKO_PUBLIC_API_BASE
    assert client.api_key is None
    assert not client.is_pro
    headers = client._get_headers()
    assert "x-cg-demo-api-key" not in headers
    assert "x-cg-pro-api-key" not in headers
    assert headers["Accept"] == "application/json"


def test_client_headers_with_demo_key():
    client = CoinGeckoClient(api_key="CG-demo12345", is_pro=False)
    headers = client._get_headers()
    assert headers["x-cg-demo-api-key"] == "CG-demo12345"
    assert "x-cg-pro-api-key" not in headers
    assert client.base_url == COINGECKO_PUBLIC_API_BASE


def test_client_headers_with_pro_key():
    client = CoinGeckoClient(api_key="CG-pro99999", is_pro=True)
    headers = client._get_headers()
    assert headers["x-cg-pro-api-key"] == "CG-pro99999"
    assert "x-cg-demo-api-key" not in headers
    assert client.base_url == COINGECKO_PRO_API_BASE


def test_client_ping_success():
    client = CoinGeckoClient()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({"gecko_says": "(V3) To the Moon!"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert client.ping() is True


def test_client_ping_failure():
    client = CoinGeckoClient(max_retries=1)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network down")):
        assert client.ping() is False


def test_client_get_simple_price_query():
    client = CoinGeckoClient()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({
        "bitcoin": {"usd": 90000.0, "usd_market_cap": 1800000000.0}
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res = client.get_simple_price(["bitcoin"], ["usd"])
        assert "bitcoin" in res
        req = mock_urlopen.call_args[0][0]
        assert "ids=bitcoin" in req.full_url
        assert "vs_currencies=usd" in req.full_url


def test_client_http_error_handling():
    client = CoinGeckoClient(max_retries=1)
    http_error = urllib.error.HTTPError(
        url="https://api.coingecko.com/api/v3/coins/invalid",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"error": "coin not found"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(CoinGeckoAPIError) as exc_info:
            client.get("/coins/invalid")
        assert "HTTP 404" in str(exc_info.value)


def test_fetch_market_data_schema_and_content():
    fake_client = FakeCoinGeckoClient()
    coins = ["bitcoin", "ethereum"]
    vs_currencies = ["usd"]

    records = list(fetch_coingecko_market_data(
        coin_ids=coins,
        vs_currencies=vs_currencies,
        client=fake_client,
        include_details=True,
    ))

    assert len(records) == 2
    btc = records[0]
    assert btc["id"] == "coingecko:bitcoin:usd"
    assert btc["coin_id"] == "bitcoin"
    assert btc["name"] == "Bitcoin"
    assert btc["symbol"] == "BIT"
    assert btc["vs_currency"] == "usd"
    assert btc["current_price"] == 92500.0
    assert btc["market_cap"] == 1820000000000.0
    assert btc["total_volume_24h"] == 35000000000.0
    assert btc["price_change_percentage_24h"] == 2.45
    assert isinstance(btc["timestamp"], int)
    assert "Asset: Bitcoin (BIT)" in btc["text"]
    assert "Current Price (USD): 92500.0" in btc["text"]
    assert "Description:" in btc["text"]

    eth = records[1]
    assert eth["id"] == "coingecko:ethereum:usd"
    assert eth["current_price"] == 3400.0


def test_fetch_market_data_multi_currency():
    fake_client = FakeCoinGeckoClient()
    records = list(fetch_coingecko_market_data(
        coin_ids=["solana"],
        vs_currencies=["usd", "eur"],
        client=fake_client,
        include_details=False,
    ))

    assert len(records) == 2
    ids = {r["id"] for r in records}
    assert "coingecko:solana:usd" in ids
    assert "coingecko:solana:eur" in ids


def test_fetch_market_data_detail_fallback_on_error():
    fake_client = FakeCoinGeckoClient()
    fake_client.get_coin_detail = MagicMock(side_effect=Exception("Metadata service unavailable"))

    records = list(fetch_coingecko_market_data(
        coin_ids=["bitcoin"],
        vs_currencies=["usd"],
        client=fake_client,
        include_details=True,
    ))

    assert len(records) == 1
    rec = records[0]
    assert rec["coin_id"] == "bitcoin"
    assert rec["name"] == "Bitcoin"
    assert rec["symbol"] == "BITCOIN"


def test_coingecko_source_structure():
    source = coingecko_source(coin_ids=["bitcoin"], vs_currencies=["usd"])
    assert source.name == "coingecko"
    resources = list(source.resources.values())
    assert len(resources) == 1
    res = resources[0]
    assert res.name == "coingecko_market_data"
    assert res._hints.get("primary_key") == "id"
    assert res.write_disposition == "merge"
