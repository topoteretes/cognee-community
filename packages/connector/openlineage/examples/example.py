"""Example demonstrating OpenLineage / Marquez data-source connector for Cognee.

This script demonstrates:
1. Configuring the OpenLineage source pointing to a Marquez backend.
2. Ingesting data pipeline jobs, datasets, and run histories into Cognee memory.
3. Querying the knowledge graph for root cause diagnosis and lineage impact analysis.
"""

import asyncio
import os

import cognee

from cognee_community_connector_openlineage import openlineage_source

DATASET_NAME = "pipeline_lineage_memory"


async def main():
    # 1. Configure the OpenLineage source
    # Can connect to an active Marquez instance or OpenLineage HTTP proxy
    source = openlineage_source(
        endpoint_url=os.environ.get("OPENLINEAGE_URL", "http://localhost:5000"),
        api_key=os.environ.get("OPENLINEAGE_API_KEY"),
        namespaces=["analytics", "finance"],
        include_facets=True,
        max_runs_per_job=5,
    )

    # 2. Ingest lineage topology into Cognee
    print("Syncing OpenLineage pipeline topologies and dataset schemas into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    # 3. Query the knowledge graph for cross-pipeline provenance and dependency impact
    query = (
        "Which upstream jobs write to the retention_metrics dataset, "
        "and did any recent runs fail with memory or connection errors?"
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
