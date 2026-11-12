# CoinGecko Connector for Cognee

A high-performance public market data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing cryptocurrency price feeds, market capitalization, 24-hour trading volumes, and asset descriptions directly into Cognee's AI knowledge graph.

## Features

- **Public & Pro Support**: Operates out of the box with CoinGecko's free public REST API (`api.coingecko.com/api/v3`) or with Demo/Pro API keys via header authentication (`x-cg-demo-api-key` or `x-cg-pro-api-key`).
- **Resilient Rate Limiting**: Built-in exponential backoff and jitter handling for HTTP 429 rate limit responses.
- **Rich Semantic Documents**: Generates structured entity summaries combining market metrics and textual descriptions, formatted for optimal knowledge graph extraction and vector embedding in Cognee.
- **Native dlt Integration**: Conforms to standard `dlt.source` specifications with deterministic primary keys (`coingecko:{coin_id}:{vs_currency}`) and `merge` write disposition to prevent duplicate graph entities.

## Installation

Add the connector to your Cognee environment:

```bash
pip install cognee-community-connector-coingecko
```

Or using `uv`:

```bash
uv add cognee-community-connector-coingecko
```

## Quick Start

### 1. Ingesting Crypto Market Data into Cognee

```python
import asyncio
import cognee
from cognee_community_connector_coingecko import coingecko_source

async def main():
    # Initialize cognee
    await cognee.prune.prune_data()
    await cognee.prune.prune_system()

    # Ingest market data for Bitcoin, Ethereum, and Solana
    print("Ingesting CoinGecko market data...")
    await cognee.remember(
        coingecko_source(
            coin_ids=["bitcoin", "ethereum", "solana"],
            vs_currencies=["usd"],
            include_details=True,
        ),
        dataset_name="crypto_market",
    )

    # Cognify knowledge graph
    print("Processing knowledge graph...")
    await cognee.cognify(datasets=["crypto_market"])

    # Query the knowledge graph
    results = await cognee.search(
        "What is the current market cap and price trend for Bitcoin and Solana?",
        query_type="INSIGHTS",
    )
    for result in results:
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. Standalone Market Data Extraction

You can also use the generator directly without running a full Cognee pipeline:

```python
from cognee_community_connector_coingecko import CoinGeckoClient, fetch_coingecko_market_data

client = CoinGeckoClient()

for record in fetch_coingecko_market_data(
    coin_ids=["bitcoin", "ethereum"],
    vs_currencies=["usd", "eur"],
    client=client,
):
    print(f"[{record['symbol']}] Price: {record['current_price']} {record['vs_currency'].upper()} | 24h Vol: {record['total_volume_24h']}")
```

## Data Schema

Each ingested record produces a row in the `coingecko_market_data` resource table with the following schema:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Primary key formatted as `coingecko:<coin_id>:<vs_currency>` |
| `coin_id` | `str` | Coin identifier (e.g. `bitcoin`, `ethereum`) |
| `name` | `str` | Formal asset name (e.g. `Bitcoin`, `Ethereum`) |
| `symbol` | `str` | Uppercase ticker symbol (e.g. `BTC`, `ETH`) |
| `vs_currency` | `str` | Target comparison currency (e.g. `usd`, `eur`) |
| `current_price` | `float` | Current exchange rate |
| `market_cap` | `float` | Current total market capitalization |
| `total_volume_24h` | `float` | 24-hour aggregate trading volume |
| `price_change_percentage_24h` | `float` | 24-hour relative price percentage change |
| `timestamp` | `int` | Unix epoch timestamp of data extraction |
| `text` | `str` | Natural language summary for embedding and graph entity synthesis |

## Parameters

`coingecko_source` accepts:

- `coin_ids` (`List[str]`, optional): Coin IDs to query (defaults to `["bitcoin", "ethereum", "solana"]`).
- `vs_currencies` (`List[str]`, optional): Comparison currencies (defaults to `["usd"]`).
- `api_key` (`str`, optional): CoinGecko Demo or Pro API key.
- `is_pro` (`bool`, optional): Set to `True` if using a CoinGecko Pro plan endpoint (`pro-api.coingecko.com`). Defaults to `False`.
- `include_details` (`bool`, optional): Fetches rich coin descriptions from `/coins/{id}`. Defaults to `True`.

## License

Apache 2.0
