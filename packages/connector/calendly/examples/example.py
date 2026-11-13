"""Calendly connector demo — Ingest scheduled meetings & attendee questions into Cognee.

Pulls Calendly events and invitee responses into memory, incrementally,
with automatic forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Generate a Personal Access Token in Calendly:
   Go to Account > Integrations > API & Webhooks > Generate New Token.

2. Set environment variables:
   export CALENDLY_API_KEY="your-calendly-personal-access-token"
   export LLM_API_KEY="your-llm-key"

3. Run the script:
   python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_calendly import calendly_source

DATASET_NAME = "calendly_meetings"


async def main():
    api_key = os.getenv("CALENDLY_API_KEY")
    if not api_key:
        print("Please set CALENDLY_API_KEY before running this example.")
        return

    print("=== Step 1: Initial Ingestion of Calendly Scheduled Events ===")
    source = calendly_source(
        api_key=api_key,
        status="active",
        include_invitee_qa=True,
    )

    print("Adding Calendly data source to Cognee...")
    await cognee.add(source, dataset_name=DATASET_NAME)

    print("Cognifying meetings into Knowledge Graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Initial sync and indexing complete!")

    print("\n=== Step 2: Semantic Search across Meetings & Attendee Q&A ===")
    search_query = "What goals or topics did clients mention in their meeting booking form?"
    search_results = await cognee.search(
        search_type="INSIGHTS",
        query_text=search_query,
        dataset_name=DATASET_NAME,
    )
    print(f"Search Results for '{search_query}':")
    for res in search_results:
        print(f"- {res}")

    print("\n=== Step 3: Incremental Sync (New Meetings & Updates) ===")
    incremental_source = calendly_source(
        api_key=api_key,
        status="active",
        include_invitee_qa=True,
    )
    await cognee.add(incremental_source, dataset_name=DATASET_NAME)
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Incremental sync completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
