"""Greenhouse connector demo — Ingest recruiting jobs, descriptions & scorecards into Cognee.

Pulls Greenhouse recruiting job requisitions, job post descriptions, and candidate
interview evaluation scorecards into memory, incrementally, with automatic forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Generate a Harvest API key in Greenhouse:
   Go to Greenhouse > Configure > Dev Center > API Credential Management > Create New API Key.
   Select permissions: Jobs (GET), Job Posts (GET), Scorecards (GET).

2. Set environment variables:
   export GREENHOUSE_HARVEST_API_KEY="your-harvest-api-key"

3. Run the script:
   python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_greenhouse import greenhouse_source

DATASET_NAME = "greenhouse_recruiting"


async def main():
    api_key = os.getenv("GREENHOUSE_HARVEST_API_KEY")
    if not api_key:
        print("Please set GREENHOUSE_HARVEST_API_KEY before running this example.")
        return

    print("=== Step 1: Initial Ingestion of Greenhouse Jobs ===")
    # Notice: include_interview_feedback is strictly opt-in to safeguard candidate personal data
    source = greenhouse_source(
        api_key=api_key,
        job_status="open",
        include_job_posts=True,
        include_interview_feedback=False,
    )

    print("Adding Greenhouse data source to Cognee...")
    await cognee.add(source, dataset_name=DATASET_NAME)

    print("Cognifying recruiting jobs into Knowledge Graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Initial sync and indexing complete!")

    print("\n=== Step 2: Semantic Search across Job Descriptions & Requirements ===")
    search_query = (
        "What backend and AI distributed systems roles are currently open across departments?"
    )
    search_results = await cognee.search(
        search_type="INSIGHTS",
        query_text=search_query,
        dataset_name=DATASET_NAME,
    )
    print(f"Search Results for '{search_query}':")
    for res in search_results:
        print(f"- {res}")

    print("\n=== Step 3: Incremental Sync (New Jobs & Updated Descriptions) ===")
    incremental_source = greenhouse_source(
        api_key=api_key,
        job_status="open",
        include_job_posts=True,
    )
    await cognee.add(incremental_source, dataset_name=DATASET_NAME)
    await cognee.cognify(dataset_name=DATASET_NAME)
    print("Incremental sync completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
