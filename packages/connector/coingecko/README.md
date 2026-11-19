# CoinGecko Connector for Cognee

A high-performance data connector to ingest real-time and historical cryptocurrency market snapshots into **Cognee** knowledge graphs and semantic memory layers.

## Features
- **Public Tier Zero-Auth:** Ingest cryptocurrency data without requiring any API keys or paid accounts.
- **Demo & Pro Key Support:** Optional support for CoinGecko Demo and Pro tier API keys with elevated rate limits.
- **Deterministic Keying:** Generates stable SHA-256 document identifiers keyed by coin ID and snapshot timestamp.
- **Incremental Sync:** Cursors filter market records by `last_updated` ISO timestamps to minimize redundant token consumption and graph updates.
- **Rich Semantic Markdown:** Formats prices, trading volumes, market capitalizations, price changes, and circulating supplies into structured markdown suitable for LLM RAG pipelines.

## Installation

```bash
pip install cognee-community-connector-coingecko
```

## Quick Start

```python
from cognee_community_connector_coingecko import CoinGeckoConnector

connector = CoinGeckoConnector(vs_currency="usd")

# Ingest top cryptocurrency assets
documents = connector.fetch_documents(
    ids=["bitcoin", "ethereum", "solana"],
    per_page=10
)

for doc in documents:
    print(f"Document ID: {doc['id']}")
    print(doc["text"])
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```
