"""Ingest Docker Compose multi-container topologies into cognee memory via dlt.

Demonstrates:
1. Ingesting a ``docker-compose.yml`` file containing services, images, ports, and dependencies.
2. Building an AI agent knowledge graph with ``cognee.cognify()``.
3. Asking complex questions about container infrastructure and port mapping with ``cognee.search()``.
4. Demonstrating forget-on-delete / orphan cleanup when a service is decommissioned.

Requires:
    pip install "cognee[dlt]"
    export LLM_API_KEY="your-llm-key"
"""

import asyncio
import os

import cognee
from cognee_community_connector_docker_compose import docker_compose_source

DATASET = "app_infrastructure_topology"
CURRENT_DIR = os.path.dirname(__file__)
COMPOSE_PATH = os.path.join(CURRENT_DIR, "test_data", "docker-compose.yml")

QUESTION = "Which services depend on postgres, and what port is postgres mapped to?"


async def main() -> None:
    print("1. Initializing clean memory state...")
    await cognee.forget(everything=True)

    print("2. Ingesting Docker Compose topology snapshot...")
    await cognee.remember(
        docker_compose_source(COMPOSE_PATH),
        dataset_name=DATASET,
        max_rows_per_table=0,  # Ingest entire topology snapshot
    )

    print("3. Building knowledge graph from container service topology...")
    await cognee.cognify()

    print(f"\n4. Querying agent memory: '{QUESTION}'")
    answer = await cognee.search(QUESTION)
    print("\nAgent Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
