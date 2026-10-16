"""Example script demonstrating Brex spend data ingestion and search in cognee.

This script demonstrates:
1. Connecting with Brex Bearer API Token.
2. Ingesting expense memos into the document path (searchable via LLM knowledge graph).
3. Ingesting structured expenses and budgets incrementally with forget-on-delete.
4. Searching corporate expenses using cognee.search().
"""

import asyncio
import os

import cognee

from cognee_community_connector_brex import brex_source


async def main() -> None:
    print("=== Cognee Brex Connector Example ===")

    # 1. Retrieve API token from environment variable
    token = os.getenv("BREX_API_TOKEN", "b_usr_sample_token_123")

    # 2. Configure the Brex source with desired resources
    source = brex_source(
        token=token,
        include=["expenses", "memos", "budgets"],
    )

    # 3. Ingest into Cognee memory
    print("Ingesting Brex corporate expenses and budgets into memory...")
    await cognee.remember(
        source,
        dataset_name="brex_spend",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # Allows full orphan-cleanup evaluation
    )

    # 4. Search corporate expenses and memos
    query = (
        "What was the business purpose for our recent Datadog and AWS "
        "cloud infrastructure expenses?"
    )
    print(f"\nSearching for: '{query}'")
    results = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["brex_spend"],
    )

    print("\nSearch Results:")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
