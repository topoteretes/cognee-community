"""Example usage of ArcadeDB hybrid adapter with Cognee.

This example demonstrates using ArcadeDB as both the graph and vector
database for Cognee's knowledge graph pipeline.

Prerequisites:
    - ArcadeDB running with Bolt (7687) and HTTP (2480) ports
    - pip install cognee cognee-community-hybrid-adapter-arcadedb
"""

import asyncio

import cognee
from cognee_community_hybrid_adapter_arcadedb import ArcadeDBAdapter


async def main():
    # Configure Cognee to use ArcadeDB for both graph and vector
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "arcadedb",
            "graph_database_url": "bolt://localhost:7687",
            "graph_database_username": "root",
            "graph_database_password": "arcadedb_password",
        }
    )

    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "arcadedb",
            "vector_db_url": "localhost",
        }
    )

    # Reset any previous data
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # Add some text data
    await cognee.add(
        "ArcadeDB is a multi-model database supporting graph, document, "
        "key-value, time-series, and vector data models in a single engine.",
        dataset_name="arcadedb_docs",
    )

    # Process and build the knowledge graph with vectors
    await cognee.cognify()

    # Search using vector similarity
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text="What data models does ArcadeDB support?",
    )

    for result in results:
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
