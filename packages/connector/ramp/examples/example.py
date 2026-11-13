"""Ramp connector demo — Ingest corporate card transactions, memos & receipts into Cognee.

Pulls corporate card spend, employee business purpose memos, and OCR receipt items
into memory, incrementally, with automatic forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Configure Ramp API credentials:
   Option A (OAuth Client Credentials):
     export RAMP_CLIENT_ID="your-ramp-client-id"
     export RAMP_CLIENT_SECRET="your-ramp-client-secret"
   Option B (Direct API Access Token):
     export RAMP_ACCESS_TOKEN="your-ramp-access-token"

2. Run the script:
   python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_ramp import ramp_source

DATASET_NAME = "ramp_expenses"


async def main():
    client_id = os.getenv("RAMP_CLIENT_ID")
    client_secret = os.getenv("RAMP_CLIENT_SECRET")
    access_token = os.getenv("RAMP_ACCESS_TOKEN")

    if not access_token and not (client_id and client_secret):
        print("Please set RAMP_ACCESS_TOKEN or RAMP_CLIENT_ID/RAMP_CLIENT_SECRET.")
        return

    print("=== Step 1: Initial Ingestion of Ramp Transactions & Receipts ===")
    source = ramp_source(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        include_receipts=True,
    )

    print("Adding Ramp data source to Cognee...")
    await cognee.add(source, dataset_name=DATASET_NAME)

    print("Cognifying expenses into Knowledge Graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Initial sync and indexing complete!")

    print("\n=== Step 2: Semantic Search across Expense Memos & Receipts ===")
    search_query = "What cloud hosting and development software expenses did team members submit?"
    search_results = await cognee.search(
        search_type="INSIGHTS",
        query_text=search_query,
        dataset_name=DATASET_NAME,
    )
    print(f"Search Results for '{search_query}':")
    for res in search_results:
        print(f"- {res}")

    print("\n=== Step 3: Incremental Sync (New Transactions & Added Memos) ===")
    incremental_source = ramp_source(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        lookback_days=30,
    )
    await cognee.add(incremental_source, dataset_name=DATASET_NAME)
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Incremental sync completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
