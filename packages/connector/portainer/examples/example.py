"""Example demonstrating Portainer container & cluster telemetry ingestion into Cognee.

This script fetches environments, deployment stacks, and container states from
a Portainer instance, indexes them into Cognee memory via cognify(), and executes
natural-language infrastructure queries.
"""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_portainer import (
    PortainerConfig,
    portainer_connector,
)

DATASET_NAME = "infrastructure_topology"


async def main() -> None:
    # 1. Initialize connector configuration
    config = PortainerConfig(
        api_url=os.getenv("PORTAINER_URL", "https://portainer.local:9443"),
        api_key=os.getenv("PORTAINER_API_KEY", "ptr_test_api_key"),
        include_containers=True,
        include_stacks=True,
    )

    print("Fetching Portainer environment & container telemetry...")
    documents = await portainer_connector(config)
    print(f"Retrieved {len(documents)} structured infrastructure documents.")

    # 2. Ingest documents into Cognee dataset
    print(f"\nAdding documents to Cognee dataset '{DATASET_NAME}'...")
    await cognee.add(documents, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph and vector indices
    print("Cognifying dataset to build container topology memory...")
    await cognee.cognify(dataset_name=DATASET_NAME)

    # 4. Search and reason over container topology
    queries = [
        "What containers are currently running with exposed ports?",
        "Show me the compose definition and services for the redis broker stack.",
        "Which environments are operational and what Docker versions are running?",
    ]

    for query in queries:
        print(f"\nQuery: '{query}'")
        results = await cognee.search(query, dataset_name=DATASET_NAME)
        for res in results:
            print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
