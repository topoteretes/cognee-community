"""Example demonstrating Honeycomb data-source connector for Cognee.

This script demonstrates:
1. Configuring the Honeycomb source to ingest datasets, columns, boards, triggers,
   SLOs, and markers.
2. Ingesting Honeycomb observability metadata into Cognee cognitive memory.
3. Querying the knowledge graph for SLO targets and firing trigger conditions.
"""

import asyncio
import os

import cognee

from cognee_community_connector_honeycomb import HoneycombConfig, honeycomb_source

DATASET_NAME = "honeycomb_observability_memory"


async def main():
    # 1. Configure the Honeycomb source
    config = HoneycombConfig(
        api_key=os.environ.get("HONEYCOMB_API_KEY", "test-api-key"),
        base_url=os.environ.get("HONEYCOMB_BASE_URL", "https://api.honeycomb.io"),
        dataset_slugs=["production-web", "payment-service"],
        include_boards=True,
        include_triggers=True,
        include_slos=True,
        include_markers=True,
        write_disposition="replace",
    )

    source = honeycomb_source(config=config)

    # 2. Ingest Honeycomb observability into Cognee
    print("Syncing Honeycomb datasets, boards, triggers, and SLOs into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    # 3. Query the knowledge graph for SLO targets and alerts
    query = (
        "What are our active SLO targets and burn alert thresholds for the payment-service, "
        "and were there any recent deployment markers?"
    )
    print(f"\nQuerying knowledge graph: '{query}'\n")

    search_results = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )

    print("Agent Search Results:")
    print(search_results)


if __name__ == "__main__":
    asyncio.run(main())
