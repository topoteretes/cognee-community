import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_coingecko import (
    fetch_coingecko_markets,
    format_coin_market_markdown,
    filter_incremental_snapshots,
    CoinGeckoConnector
)

MOCK_COIN_DATA = [
    {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "current_price": 68500.50,
        "market_cap": 1350000000000,
        "market_cap_rank": 1,
        "total_volume": 35000000000,
        "high_24h": 69200.00,
        "low_24h": 67800.00,
        "price_change_24h": 700.50,
        "price_change_percentage_24h": 1.03,
        "circulating_supply": 19750000,
        "total_supply": 21000000,
        "ath": 73750.00,
        "last_updated": "2026-10-26T12:00:00.000Z"
    },
    {
        "id": "ethereum",
        "symbol": "eth",
        "name": "Ethereum",
        "current_price": 2550.25,
        "market_cap": 310000000000,
        "market_cap_rank": 2,
        "total_volume": 18000000000,
        "high_24h": 2600.00,
        "low_24h": 2510.00,
        "price_change_24h": 40.25,
        "price_change_percentage_24h": 1.60,
        "circulating_supply": 120000000,
        "total_supply": 120000000,
        "ath": 4891.70,
        "last_updated": "2026-10-26T11:30:00.000Z"
    }
]

def test_format_coin_market_markdown():
    coin = MOCK_COIN_DATA[0]
    markdown = format_coin_market_markdown(coin)

    assert "# Crypto Asset: Bitcoin (BTC)" in markdown
    assert "**Asset ID:** bitcoin" in markdown
    assert "**Market Cap Rank:** #1" in markdown
    assert "$68,500.5000" in markdown
    assert "$1,350,000,000,000.00" in markdown
    assert "Circulating Supply:" in markdown

def test_filter_incremental_snapshots():
    # Only bitcoin was updated after 11:45
    cursor = "2026-10-26T11:45:00.000Z"
    filtered = filter_incremental_snapshots(MOCK_COIN_DATA, cursor)

    assert len(filtered) == 1
    assert filtered[0]["id"] == "bitcoin"

    # All returned when cursor is None
    all_coins = filter_incremental_snapshots(MOCK_COIN_DATA, None)
    assert len(all_coins) == 2

@patch("requests.Session.get")
def test_fetch_coingecko_markets(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_COIN_DATA
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    results = fetch_coingecko_markets(vs_currency="usd", ids=["bitcoin", "ethereum"])

    assert len(results) == 2
    assert results[0]["symbol"] == "btc"
    assert mock_get.called

@patch("requests.Session.get")
def test_coingecko_connector_fetch_documents(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_COIN_DATA
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    connector = CoinGeckoConnector()
    docs = connector.fetch_documents(ids=["bitcoin"])

    assert len(docs) == 2
    assert docs[0]["metadata"]["source"] == "coingecko"
    assert docs[0]["metadata"]["symbol"] == "BTC"
    assert "id" in docs[0]
    assert len(docs[0]["id"]) == 64  # SHA256 length
