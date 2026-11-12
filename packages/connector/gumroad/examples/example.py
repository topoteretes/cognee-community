"""Gumroad connector demo: sync digital storefront products into Cognee memory.

Pulls products, pricing, descriptions, tiers, and URLs into Cognee.
``gumroad_source`` returns a ``dlt`` source passed to ``cognee.remember``.

Rows are processed into documents, transforming digital products into structured
entities and relationships during Cognee's cognify phase.

Quick start:
------------
1. Install dependencies:
       uv add cognee-community-connector-gumroad

2. Set your Gumroad access token:
       export GUMROAD_ACCESS_TOKEN="your_access_token_here"

3. Run the example:
       python examples/example.py
"""

import asyncio
import os
import sys

try:
    import cognee

    from cognee_community_connector_gumroad import gumroad_source
except ImportError:
    # Allow running from package directory directly
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from cognee_community_connector_gumroad import gumroad_source


async def main() -> None:
    print("=" * 60)
    print("Gumroad Creator Storefront Connector for Cognee")
    print("=" * 60)

    access_token = os.getenv("GUMROAD_ACCESS_TOKEN")
    if not access_token:
        print("[!] No GUMROAD_ACCESS_TOKEN found in environment. Using demo configuration.")

    # Initialize Gumroad dlt source
    source = gumroad_source(access_token=access_token)

    print("Ingesting storefront catalog into Cognee dataset 'storefront_catalog'...")
    await cognee.remember(
        source,
        dataset_name="storefront_catalog",
    )

    print("Cognifying dataset to build creator knowledge graph...")
    await cognee.cognify(datasets=["storefront_catalog"])

    print("Querying creator knowledge graph:")
    results = await cognee.search(
        "What products and pricing tiers are available in the storefront?",
        query_type="INSIGHTS",
    )
    for idx, insight in enumerate(results, 1):
        print(f"\n[Insight #{idx}]:\n{insight}")


if __name__ == "__main__":
    asyncio.run(main())
