"""Brex connector demo — Ingest corporate card expenses, memos & budgets into Cognee.

Pulls Brex corporate card transactions, employee memos, and corporate budgets
into memory, incrementally, with automatic forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Generate an API token in Brex:
   Go to Brex Dashboard > Settings > Developer > Create Token.

2. Set environment variables:
   export BREX_API_KEY="your-brex-api-key"

3. Run the script:
   python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_brex import brex_source

DATASET_NAME = "brex_financials"


async def main():
    api_key = os.getenv("BREX_API_KEY")
    if not api_key:
        print("Please set BREX_API_KEY before running this example.")
        return

    print("=== Step 1: Initial Ingestion of Brex Expenses & Budgets ===")
    source = brex_source(
        api_key=api_key,
        include_expenses=True,
        include_budgets=True,
    )

    print("Adding Brex data source to Cognee...")
    await cognee.add(source, dataset_name=DATASET_NAME)

    print("Cognifying expenses and budgets into Knowledge Graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Initial sync and indexing complete!")

    print("\n=== Step 2: Semantic Search across Spend Memos & Budgets ===")
    search_query = (
        "What subscriptions or infrastructure expenses were charged against engineering budgets?"
    )
    search_results = await cognee.search(
        search_type="INSIGHTS",
        query_text=search_query,
        dataset_name=DATASET_NAME,
    )
    print(f"Search Results for '{search_query}':")
    for res in search_results:
        print(f"- {res}")

    print("\n=== Step 3: Incremental Sync (New Expenses & Posted Memos) ===")
    incremental_source = brex_source(
        api_key=api_key,
        include_expenses=True,
        include_budgets=True,
    )
    await cognee.add(incremental_source, dataset_name=DATASET_NAME)
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Incremental sync completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
