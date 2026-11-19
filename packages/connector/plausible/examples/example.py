"""Plausible Analytics connector example — turn website metrics into AI memory.

Syncs website traffic, top content pages, acquisition channels, and conversion goals
into Cognee's persistent memory using the Plausible Analytics connector.

The connector yields Markdown reports ingested through Cognee's document-mode
pipeline (``DOCUMENT_SOURCE_ATTR = "plausible"``), creating an interconnected
knowledge graph of websites, pages, referrers, and goals.

────────────────────────────────────────────────────────────────────────────
Quickstart:
────────────────────────────────────────────────────────────────────────────
1. Obtain your Plausible API key from Plausible Settings -> API Keys.
2. Export your credentials:

       export PLAUSIBLE_API_KEY="plausible_api_..."
       export PLAUSIBLE_SITE_ID="yourdomain.com"
       export LLM_API_KEY="sk-..."

3. Run this script:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_plausible import plausible_source

DATASET_NAME = "plausible_analytics"


async def main() -> None:
    api_key = os.environ.get("PLAUSIBLE_API_KEY")
    site_id = os.environ.get("PLAUSIBLE_SITE_ID", "example.com")

    if not api_key:
        print(
            "Please set PLAUSIBLE_API_KEY and PLAUSIBLE_SITE_ID to run live ingestion.\n"
            "Usage:\n"
            "  export PLAUSIBLE_API_KEY='your_token'\n"
            "  export PLAUSIBLE_SITE_ID='yourdomain.com'\n"
        )
        return

    print(f"Connecting to Plausible Analytics for site: '{site_id}' ...")

    # Instantiate the dlt source (30-day window with breakdowns and goals)
    source = plausible_source(
        site_id=site_id,
        api_key=api_key,
        period="30d",
        include_breakdowns=True,
        include_goals=True,
    )

    print(f"Ingesting Plausible analytics into Cognee dataset '{DATASET_NAME}' ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("\nRunning test recall query on website memory ...")
    query = "What are our top visited pages and main traffic referral sources?"
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )

    print(f"\nQuestion: {query}")
    print(f"Answer:\n{answer}\n")
    print("Done! Web analytics metrics are now integrated into AI agent memory.")


if __name__ == "__main__":
    asyncio.run(main())
