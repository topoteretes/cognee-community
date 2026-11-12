"""CoinGecko connector demo: sync cryptocurrency market data into Cognee memory.

Pulls real-time price quotes, market capitalizations, 24h trading volumes,
and asset descriptions into Cognee. ``coingecko_source`` returns a ``dlt``
source that can be passed directly to ``cognee.remember``.

Rows are ingested as documents, transforming live crypto metrics into structured
entities and relationships during Cognee's cognify phase.

Quick start:
------------
1. Install dependencies:
       uv add cognee-community-connector-coingecko

2. Run the example:
       python examples/example.py
"""

import asyncio
import os
import sys

try:
    import cognee

    from cognee_community_connector_coingecko import coingecko_source
except ImportError:
    # Allow running from package directory directly
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from cognee_community_connector_coingecko import coingecko_source


async def main() -> None:
    print("=" * 60)
    print("CoinGecko Connector Example for Cognee")
    print("=" * 60)

    # Assets to track
    target_coins = ["bitcoin", "ethereum", "solana"]
    target_currencies = ["usd"]

    print(f"Tracking assets: {target_coins} in {target_currencies}...")

    # Optional CoinGecko API key (for demo or pro accounts)
    api_key = os.getenv("COINGECKO_API_KEY")

    # Ingest market data into Cognee
    source = coingecko_source(
        coin_ids=target_coins,
        vs_currencies=target_currencies,
        api_key=api_key,
        include_details=True,
    )

    print("Ingesting market data into Cognee dataset 'crypto_intelligence'...")
    await cognee.remember(
        source,
        dataset_name="crypto_intelligence",
    )

    print("Cognifying dataset to build knowledge graph...")
    await cognee.cognify(datasets=["crypto_intelligence"])

    print("Querying Cognee knowledge graph:")
    results = await cognee.search(
        "Compare Bitcoin, Ethereum, and Solana market dynamics and trading volume",
        query_type="INSIGHTS",
    )
    for idx, insight in enumerate(results, 1):
        print(f"\n[Insight #{idx}]:\n{insight}")


if __name__ == "__main__":
    asyncio.run(main())
